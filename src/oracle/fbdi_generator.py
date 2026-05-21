"""
src/oracle/fbdi_generator.py
-----------------------------
Oracle Journal Import FBDI (File-Based Data Import) generator.

Produces a CSV file conforming to the GL_INTERFACE table layout required
by Oracle Fusion Cloud Journal Import (Oracle standard ESS process:
JournalImportLauncher).

Oracle GL_INTERFACE column reference:
  https://docs.oracle.com/en/cloud/saas/financials/24d/oedmf/gl_interface.html

Required columns that must be non-null for Journal Import:
  STATUS               — always 'NEW' for import
  LEDGER_ID            — GL_LEDGERS.LEDGER_ID (NUMBER)
  ACCOUNTING_DATE      — GL_JE_HEADERS.DEFAULT_EFFECTIVE_DATE
  CURRENCY_CODE        — ISO 4217 currency, e.g. USD
  DATE_CREATED         — SYSDATE equivalent (ISO format)
  CREATED_BY           — integration user or application name
  ACTUAL_FLAG          — 'A' (Actual), 'B' (Budget), 'E' (Encumbrance)
  USER_JE_CATEGORY_NAME — GL_JE_CATEGORIES.USER_JE_CATEGORY_NAME
  USER_JE_SOURCE_NAME   — GL_JE_SOURCES.USER_JE_SOURCE_NAME
  SEGMENT1 .. SEGMENT10 — chart of account segment values
  ENTERED_DR / ENTERED_CR — entered currency amounts (mutually exclusive per line)

Optional but recommended:
  REFERENCE1           — batch name
  REFERENCE4           — journal header name
  REFERENCE5           — journal line description
  DESCRIPTION          — line-level description (also maps to JE_LINE description)
  GROUP_ID             — batch grouping for same-ledger imports
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GL_INTERFACE column specification
# Order MUST match Oracle's expected CSV column sequence for FBDI template
# ---------------------------------------------------------------------------

_GL_INTERFACE_COLUMNS = [
    "STATUS",
    "LEDGER_ID",
    "ACCOUNTING_DATE",
    "CURRENCY_CODE",
    "DATE_CREATED",
    "CREATED_BY",
    "ACTUAL_FLAG",
    "USER_JE_CATEGORY_NAME",
    "USER_JE_SOURCE_NAME",
    "CURRENCY_CONVERSION_TYPE",
    "CURRENCY_CONVERSION_DATE",
    "CURRENCY_CONVERSION_RATE",
    "SEGMENT1",
    "SEGMENT2",
    "SEGMENT3",
    "SEGMENT4",
    "SEGMENT5",
    "SEGMENT6",
    "SEGMENT7",
    "SEGMENT8",
    "SEGMENT9",
    "SEGMENT10",
    "ENTERED_DR",
    "ENTERED_CR",
    "ACCOUNTED_DR",
    "ACCOUNTED_CR",
    "REFERENCE1",     # Batch name
    "REFERENCE2",     # Batch description
    "REFERENCE3",     # Journal entry name
    "REFERENCE4",     # Journal entry description
    "REFERENCE5",     # Journal line description
    "REFERENCE6",
    "REFERENCE7",
    "REFERENCE8",
    "REFERENCE9",
    "REFERENCE10",
    "DESCRIPTION",
    "ATTRIBUTE1",
    "ATTRIBUTE2",
    "ATTRIBUTE3",
    "ATTRIBUTE4",
    "ATTRIBUTE5",
    "GROUP_ID",
]

_REQUIRED_COLUMNS = {
    "STATUS",
    "LEDGER_ID",
    "ACCOUNTING_DATE",
    "CURRENCY_CODE",
    "DATE_CREATED",
    "CREATED_BY",
    "ACTUAL_FLAG",
    "USER_JE_CATEGORY_NAME",
    "USER_JE_SOURCE_NAME",
    "SEGMENT1",
}


# ---------------------------------------------------------------------------
# Line-level dataclass
# ---------------------------------------------------------------------------

class CorrectionLine:
    """
    Represents a single GL_INTERFACE row for a corrective journal entry.

    segment_string : str
        Hyphen-delimited concatenated segment string, e.g. '01-6010-000-0000'.
        Will be split on '-' into SEGMENT1..SEGMENTn.
    dr : float
        Entered debit amount; 0 if this is a credit line.
    cr : float
        Entered credit amount; 0 if this is a debit line.
    description : str
        Line-level description — maps to GL_JE_LINES.DESCRIPTION.
    """

    def __init__(
        self,
        segment_string: str,
        dr: float = 0.0,
        cr: float = 0.0,
        description: str = "",
    ) -> None:
        if dr < 0 or cr < 0:
            raise ValueError("Debit and credit amounts must be non-negative.")
        if dr > 0 and cr > 0:
            raise ValueError(
                "A GL_INTERFACE line cannot have both a debit and a credit amount. "
                "Use separate lines for debit and credit entries."
            )
        self.segment_string = segment_string
        self.segments = self._parse_segments(segment_string)
        self.dr = round(dr, 2)
        self.cr = round(cr, 2)
        self.description = description

    @staticmethod
    def _parse_segments(segment_string: str) -> dict[str, str]:
        """Split 'XX-XXXX-XXX-XXXX' into {SEGMENT1: 'XX', SEGMENT2: 'XXXX', ...}."""
        parts = segment_string.split("-")
        segments: dict[str, str] = {}
        for i, part in enumerate(parts, start=1):
            if i > 10:
                break
            segments[f"SEGMENT{i}"] = part
        return segments


# ---------------------------------------------------------------------------
# Main generator function
# ---------------------------------------------------------------------------

def generate_corrective_journal(
    original_journal: dict,
    correction_lines: list[dict],
    period: str,
    output_dir: str = "output",
    created_by: str = "GL_RECON_AGENT",
    je_source: str = "Manual",
    je_category: str = "Adjustment",
) -> Path:
    """
    Generate a GL_INTERFACE FBDI CSV file for a corrective journal entry.

    Parameters
    ----------
    original_journal : dict
        The imbalanced journal dict from get_unbalanced_journals or
        get_journal_detail. Used for ledger_id, currency, accounting_date,
        and the original journal header ID (for REFERENCE1 cross-reference).
    correction_lines : list[dict]
        List of dicts, each with keys:
          - "account": str  — hyphen-delimited segment string
          - "dr": float     — entered debit (0 if credit line)
          - "cr": float     — entered credit (0 if debit line)
          - "description": str — line description
    period : str
        Accounting period name (e.g. "Jan-25") — used in file name only;
        accounting_date is derived from the original journal.
    output_dir : str
        Directory to write the output CSV file.
    created_by : str
        GL_INTERFACE.CREATED_BY value — typically the integration user.
    je_source : str
        GL_JE_SOURCES.USER_JE_SOURCE_NAME — e.g. "Manual", "Spreadsheet".
    je_category : str
        GL_JE_CATEGORIES.USER_JE_CATEGORY_NAME — e.g. "Adjustment", "Accrual".

    Returns
    -------
    Path
        Absolute path to the generated FBDI CSV file.

    Raises
    ------
    ValueError
        If correction_lines is empty, debits ≠ credits, or required account
        segment data is missing.
    """
    if not correction_lines:
        raise ValueError("correction_lines must contain at least two entries (DR and CR).")

    # Extract ledger context from the original journal
    # Supports both raw header dict (REST response) and enriched detail dict
    if "header" in original_journal:
        hdr = original_journal["header"]
    else:
        hdr = original_journal

    ledger_id = hdr.get("LedgerId") or hdr.get("ledger_id")
    original_header_id = hdr.get("JournalHeaderId") or hdr.get("journal_header_id")
    currency_code = (
        hdr.get("AccountedCurrencyCode")
        or hdr.get("currency_code")
        or "USD"
    )
    accounting_date = (
        hdr.get("PostingDate")
        or hdr.get("posting_date")
        or date.today().isoformat()
    )
    # Trim to date portion if datetime string
    if isinstance(accounting_date, str) and "T" in accounting_date:
        accounting_date = accounting_date[:10]

    if not ledger_id:
        raise ValueError("Cannot determine ledger_id from original_journal dict.")

    # Build CorrectionLine objects
    parsed_lines: list[CorrectionLine] = []
    for item in correction_lines:
        account = item.get("account") or item.get("segment_string", "")
        if not account:
            raise ValueError(f"Correction line missing 'account' field: {item}")
        parsed_lines.append(
            CorrectionLine(
                segment_string=account,
                dr=float(item.get("dr", 0) or 0),
                cr=float(item.get("cr", 0) or 0),
                description=item.get("description", ""),
            )
        )

    # Validate balance
    _assert_balanced(parsed_lines)

    # Ensure output directory exists
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # File naming: CORR_{original_header_id}_{period}.csv
    safe_period = period.replace("-", "").replace("/", "")
    file_name = f"CORR_{original_header_id}_{safe_period}.csv"
    file_path = out_path / file_name

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    batch_name = f"RECON_CORR_{original_header_id}_{safe_period}"
    je_name = f"Correction for JE {original_header_id} - {period}"

    with file_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_GL_INTERFACE_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for line in parsed_lines:
            row: dict[str, Any] = {col: "" for col in _GL_INTERFACE_COLUMNS}
            row.update(
                {
                    "STATUS": "NEW",
                    "LEDGER_ID": str(ledger_id),
                    "ACCOUNTING_DATE": accounting_date,
                    "CURRENCY_CODE": currency_code,
                    "DATE_CREATED": now_str,
                    "CREATED_BY": created_by,
                    "ACTUAL_FLAG": "A",
                    "USER_JE_CATEGORY_NAME": je_category,
                    "USER_JE_SOURCE_NAME": je_source,
                    "CURRENCY_CONVERSION_TYPE": "Corporate" if currency_code != "USD" else "",
                    "ENTERED_DR": str(line.dr) if line.dr else "",
                    "ENTERED_CR": str(line.cr) if line.cr else "",
                    "ACCOUNTED_DR": str(line.dr) if line.dr else "",
                    "ACCOUNTED_CR": str(line.cr) if line.cr else "",
                    "REFERENCE1": batch_name,
                    "REFERENCE4": je_name,
                    "REFERENCE5": line.description[:240] if line.description else "",
                    "DESCRIPTION": line.description[:240] if line.description else "",
                    "GROUP_ID": str(original_header_id),
                }
            )
            # Inject segment values
            row.update(line.segments)
            writer.writerow(row)

    logger.info(
        "generate_corrective_journal: wrote %d lines to %s",
        len(parsed_lines),
        file_path,
    )
    return file_path.resolve()


# ---------------------------------------------------------------------------
# FBDI validation
# ---------------------------------------------------------------------------

def validate_fbdi(file_path: Path) -> dict:
    """
    Validate a GL_INTERFACE FBDI CSV file before Oracle import.

    Checks:
      1. File exists and is non-empty
      2. Header row matches expected GL_INTERFACE columns
      3. All required columns are present and non-blank on each row
      4. Total ENTERED_DR == Total ENTERED_CR (journal must balance)
      5. No negative amounts
      6. ACTUAL_FLAG in ('A', 'B', 'E')
      7. STATUS == 'NEW' on all rows

    Parameters
    ----------
    file_path : Path
        Path to the FBDI CSV file to validate.

    Returns
    -------
    dict
        {
          "valid": bool,
          "row_count": int,
          "total_dr": float,
          "total_cr": float,
          "issues": list[str],
        }
    """
    file_path = Path(file_path)
    issues: list[str] = []

    if not file_path.exists():
        return {
            "valid": False,
            "row_count": 0,
            "total_dr": 0.0,
            "total_cr": 0.0,
            "issues": [f"File not found: {file_path}"],
        }

    if file_path.stat().st_size == 0:
        return {
            "valid": False,
            "row_count": 0,
            "total_dr": 0.0,
            "total_cr": 0.0,
            "issues": ["File is empty."],
        }

    total_dr = 0.0
    total_cr = 0.0
    row_count = 0
    valid_actual_flags = {"A", "B", "E"}

    with file_path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        file_columns = set(reader.fieldnames or [])

        # Check for required columns in header
        missing_required = _REQUIRED_COLUMNS - file_columns
        if missing_required:
            issues.append(f"Missing required columns: {sorted(missing_required)}")

        for row_num, row in enumerate(reader, start=2):
            row_count += 1

            # Required field presence
            for col in _REQUIRED_COLUMNS:
                if col in file_columns and not (row.get(col) or "").strip():
                    issues.append(f"Row {row_num}: required column {col} is blank.")

            # ACTUAL_FLAG value check
            actual_flag = (row.get("ACTUAL_FLAG") or "").strip().upper()
            if actual_flag and actual_flag not in valid_actual_flags:
                issues.append(
                    f"Row {row_num}: ACTUAL_FLAG='{actual_flag}' is invalid "
                    f"(must be A, B, or E)."
                )

            # STATUS check
            status = (row.get("STATUS") or "").strip().upper()
            if status and status != "NEW":
                issues.append(
                    f"Row {row_num}: STATUS='{status}' — import expects 'NEW'."
                )

            # Amount parsing and sign check
            dr_str = (row.get("ENTERED_DR") or "").strip()
            cr_str = (row.get("ENTERED_CR") or "").strip()

            dr = 0.0
            cr = 0.0
            if dr_str:
                try:
                    dr = float(dr_str)
                    if dr < 0:
                        issues.append(f"Row {row_num}: ENTERED_DR is negative ({dr}).")
                except ValueError:
                    issues.append(f"Row {row_num}: ENTERED_DR='{dr_str}' is not numeric.")

            if cr_str:
                try:
                    cr = float(cr_str)
                    if cr < 0:
                        issues.append(f"Row {row_num}: ENTERED_CR is negative ({cr}).")
                except ValueError:
                    issues.append(f"Row {row_num}: ENTERED_CR='{cr_str}' is not numeric.")

            if dr > 0 and cr > 0:
                issues.append(
                    f"Row {row_num}: both ENTERED_DR and ENTERED_CR are non-zero. "
                    "GL_INTERFACE requires these to be mutually exclusive per line."
                )

            total_dr += dr
            total_cr += cr

    # Balance check
    imbalance = round(abs(total_dr - total_cr), 6)
    if imbalance > 0.01:
        issues.append(
            f"Journal is out of balance: total DR={total_dr:.2f}, "
            f"total CR={total_cr:.2f}, difference={imbalance:.4f}."
        )

    return {
        "valid": len(issues) == 0,
        "row_count": row_count,
        "total_dr": round(total_dr, 2),
        "total_cr": round(total_cr, 2),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _assert_balanced(lines: list[CorrectionLine]) -> None:
    """Raise ValueError if lines do not balance to the cent."""
    total_dr = round(sum(l.dr for l in lines), 2)
    total_cr = round(sum(l.cr for l in lines), 2)
    if abs(total_dr - total_cr) > 0.01:
        raise ValueError(
            f"Corrective journal is out of balance: "
            f"DR={total_dr:.2f} CR={total_cr:.2f} "
            f"(difference={abs(total_dr - total_cr):.4f}). "
            "Ensure every debit line has a matching credit line."
        )
