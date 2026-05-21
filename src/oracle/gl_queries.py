"""
src/oracle/gl_queries.py
------------------------
Higher-level GL data retrieval functions that compose FusionClient calls
into structured result dicts for agent consumption.

All monetary comparison uses a tolerance of 0.01 to handle floating-point
representation of Oracle's NUMBER(38,10) columns after JSON serialization.

Oracle table references (informational — accessed via REST, not direct SQL):
  GL_JE_HEADERS          — journal headers
  GL_JE_LINES            — journal lines
  GL_CODE_COMBINATIONS   — account segment combinations (CCID index)
  GL_LEDGERS             — ledger definitions
  GL_PERIODS             — period calendar
  XLA_AE_HEADERS         — subledger accounting event headers
  XLA_AE_LINES           — subledger accounting event lines
  XLA_EVENTS             — accounting events (INVOICE_VALIDATED, etc.)
  XLA_TRANSACTION_ENTITIES — links XLA events to source transaction PKs
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.oracle.fusion_client import FusionClient

logger = logging.getLogger(__name__)

# Monetary comparison tolerance (mirrors Oracle's rounding in GL_JE_LINES)
_IMBALANCE_TOLERANCE = 0.01


# ---------------------------------------------------------------------------
# Imbalance detection
# ---------------------------------------------------------------------------

def get_unbalanced_journals(
    client: FusionClient,
    ledger_id: int,
    period_name: str,
    status_filter: Optional[str] = None,
) -> list[dict]:
    """
    Return all journals in the period where |total_dr - total_cr| > 0.01.

    Oracle GL guarantees that posted journals are balanced at the functional
    currency level; imbalances in this query set indicate:
      - Journals stuck in status 'S' (selected for posting) with errors
      - Unposted import batches with a data quality issue
      - Subledger-to-GL sync failures leaving orphan entries

    Parameters
    ----------
    client : FusionClient
    ledger_id : int
        GL_LEDGERS.LEDGER_ID
    period_name : str
        GL_PERIODS.PERIOD_NAME
    status_filter : str, optional
        'U' for unposted, 'P' for posted, None for all.

    Returns
    -------
    list[dict]
        Each dict contains:
          journal_header_id, journal_name, category, source, status,
          currency_code, total_dr, total_cr, imbalance_amount,
          posting_date, created_by, description
    """
    journals = client.get_journals(ledger_id, period_name, status_filter=status_filter)
    imbalanced: list[dict] = []

    for j in journals:
        # Oracle REST field names (PascalCase from fscmRestApi)
        header_id = j.get("JournalHeaderId")
        total_dr = float(j.get("TotalAcctDebit") or j.get("TotalEnteredDebit") or 0)
        total_cr = float(j.get("TotalAcctCredit") or j.get("TotalEnteredCredit") or 0)
        imbalance = round(abs(total_dr - total_cr), 6)

        if imbalance > _IMBALANCE_TOLERANCE:
            record = {
                "journal_header_id": header_id,
                "journal_name": j.get("JournalName"),
                "category": j.get("JeCategory"),
                "source": j.get("JeSource"),
                "status": j.get("Status"),
                "currency_code": j.get("AccountedCurrencyCode"),
                "total_dr": total_dr,
                "total_cr": total_cr,
                "imbalance_amount": imbalance,
                "posting_date": j.get("PostingDate"),
                "created_by": j.get("CreatedBy"),
                "description": j.get("Description"),
            }
            imbalanced.append(record)
            logger.info(
                "Imbalance detected: JE=%s id=%s  DR=%.2f CR=%.2f diff=%.4f",
                j.get("JournalName"),
                header_id,
                total_dr,
                total_cr,
                imbalance,
            )

    logger.info(
        "get_unbalanced_journals: %d of %d journals are imbalanced",
        len(imbalanced),
        len(journals),
    )
    return imbalanced


# ---------------------------------------------------------------------------
# Journal detail
# ---------------------------------------------------------------------------

def get_journal_detail(client: FusionClient, journal_header_id: int) -> dict:
    """
    Return a complete journal with all lines, account segments, and amounts.

    Fetches the journal header (from get_journals using finder by ID) and
    all child lines, then enriches each line with account description from
    GL_CODE_COMBINATIONS via get_account_details.

    Returns
    -------
    dict
        {
          "header": {journal header fields},
          "lines": [
            {
              "line_number": int,
              "code_combination_id": int,
              "account_segments": {Segment1..10},
              "account_description": str,
              "account_type": str,
              "entered_dr": float,
              "entered_cr": float,
              "accounted_dr": float,
              "accounted_cr": float,
              "description": str,
            },
            ...
          ],
          "total_entered_dr": float,
          "total_entered_cr": float,
          "total_accounted_dr": float,
          "total_accounted_cr": float,
          "imbalance": float,
        }
    """
    # Get header — use the list endpoint with JournalHeaderId finder
    base = client._rest_base()
    path = f"{base}/generalLedgerJournals/{journal_header_id}"
    header = client._get(path)

    # Get lines
    raw_lines = client.get_journal_lines(journal_header_id)

    enriched_lines: list[dict] = []
    total_entered_dr = 0.0
    total_entered_cr = 0.0
    total_accounted_dr = 0.0
    total_accounted_cr = 0.0

    for line in raw_lines:
        ccid = line.get("CodeCombinationId")
        account_info: dict[str, Any] = {}
        if ccid:
            try:
                account_info = client.get_account_details(int(ccid))
            except Exception as exc:
                logger.warning("Could not fetch account for CCID %s: %s", ccid, exc)

        entered_dr = float(line.get("EnteredDebit") or 0)
        entered_cr = float(line.get("EnteredCredit") or 0)
        accounted_dr = float(line.get("AccountedDebit") or 0)
        accounted_cr = float(line.get("AccountedCredit") or 0)

        total_entered_dr += entered_dr
        total_entered_cr += entered_cr
        total_accounted_dr += accounted_dr
        total_accounted_cr += accounted_cr

        enriched_lines.append(
            {
                "line_number": line.get("LineNumber"),
                "journal_line_id": line.get("JournalLineId"),
                "code_combination_id": ccid,
                "account_segments": {
                    f"segment{i}": line.get(f"Segment{i}")
                    for i in range(1, 11)
                    if line.get(f"Segment{i}") is not None
                },
                "account_description": account_info.get("Description"),
                "account_type": account_info.get("AccountType"),
                "account_enabled": account_info.get("Enabled"),
                "account_end_date": account_info.get("EndDateActive"),
                "entered_dr": entered_dr,
                "entered_cr": entered_cr,
                "accounted_dr": accounted_dr,
                "accounted_cr": accounted_cr,
                "description": line.get("Description"),
                "tax_code": line.get("TaxCode"),
            }
        )

    imbalance = round(abs(total_accounted_dr - total_accounted_cr), 6)

    return {
        "header": header,
        "lines": enriched_lines,
        "line_count": len(enriched_lines),
        "total_entered_dr": round(total_entered_dr, 2),
        "total_entered_cr": round(total_entered_cr, 2),
        "total_accounted_dr": round(total_accounted_dr, 2),
        "total_accounted_cr": round(total_accounted_cr, 2),
        "imbalance": imbalance,
    }


# ---------------------------------------------------------------------------
# XLA subledger tracing
# ---------------------------------------------------------------------------

def find_source_transaction(client: FusionClient, journal_header_id: int) -> dict:
    """
    Trace a GL journal back to its originating subledger transaction.

    Execution path:
      1. Call get_xla_entries to query XLA_AE_HEADERS + XLA_AE_LINES
         (joined to XLA_EVENTS, XLA_TRANSACTION_ENTITIES) via OTBI.
      2. Identify the application_id, event_class_code, and source_id.
      3. Optionally call get_subledger_transaction to fetch source record.

    XLA_AE_HEADERS.JE_HEADER_ID = GL_JE_HEADERS.JE_HEADER_ID  (the link)
    XLA_EVENTS.EVENT_CLASS_CODE describes the accounting event type:
      INVOICES, PAYMENTS, RECEIPTS, CREDIT_MEMOS, etc.
    XLA_TRANSACTION_ENTITIES.SOURCE_ID_INT_1 is the FK to the source table
    (AP_INVOICES_ALL.INVOICE_ID, AR_CASH_RECEIPTS_ALL.CASH_RECEIPT_ID, etc.)

    Returns
    -------
    dict
        {
          "journal_header_id": int,
          "xla_ae_header_id": int | None,
          "application_id": int | None,
          "event_id": int | None,
          "event_class_code": str | None,
          "entity_code": str | None,
          "source_id": int | None,
          "xla_line_count": int,
          "source_transaction": dict,   # from subledger REST (may be empty)
          "tracing_notes": str,
        }
    """
    xla_entries = client.get_xla_entries(journal_header_id)

    if not xla_entries:
        return {
            "journal_header_id": journal_header_id,
            "xla_ae_header_id": None,
            "application_id": None,
            "event_id": None,
            "event_class_code": None,
            "entity_code": None,
            "source_id": None,
            "xla_line_count": 0,
            "source_transaction": {},
            "tracing_notes": (
                "No XLA entries found. This journal may be a manual GL entry "
                "with no subledger source, or OTBI REST API is not configured."
            ),
        }

    # Use the first entry for header-level metadata
    first = xla_entries[0]
    ae_header_id = _coerce_int(first.get("AeHeaderId"))
    application_id = _coerce_int(first.get("ApplicationId"))
    event_id = _coerce_int(first.get("EventId"))
    event_class_code = first.get("EventClassCode")
    entity_code = first.get("EntityCode")
    source_id = _coerce_int(first.get("SourceIdInt1"))

    # Attempt to fetch the originating transaction from subledger REST
    source_transaction: dict = {}
    if application_id and source_id:
        try:
            source_transaction = client.get_subledger_transaction(application_id, source_id)
        except Exception as exc:
            logger.warning("Could not fetch source transaction: %s", exc)

    tracing_notes = (
        f"XLA link found: application_id={application_id}, "
        f"event_class={event_class_code}, entity={entity_code}, "
        f"source_id={source_id}. "
        f"Total XLA lines: {len(xla_entries)}."
    )

    return {
        "journal_header_id": journal_header_id,
        "xla_ae_header_id": ae_header_id,
        "application_id": application_id,
        "event_id": event_id,
        "event_class_code": event_class_code,
        "entity_code": entity_code,
        "source_id": source_id,
        "xla_line_count": len(xla_entries),
        "source_transaction": source_transaction,
        "tracing_notes": tracing_notes,
    }


# ---------------------------------------------------------------------------
# Account coding validation
# ---------------------------------------------------------------------------

def validate_account_coding(
    client: FusionClient,
    code_combination_id: int,
    ledger_id: int,
) -> dict:
    """
    Validate that an account combination is valid and active for the ledger.

    Checks performed (mirroring Oracle's GL_CODE_COMBINATIONS validation):
      1. Record exists (non-404 response)
      2. Enabled = 'Y'
      3. EndDateActive is null or in the future
      4. Account is a Detail (not Summary) account
      5. Summary flag = 'N' (summary accounts cannot receive journal entries)

    Parameters
    ----------
    code_combination_id : int
        GL_CODE_COMBINATIONS.CODE_COMBINATION_ID
    ledger_id : int
        GL_LEDGERS.LEDGER_ID (used for future chart-of-accounts cross-check)

    Returns
    -------
    dict
        {
          "code_combination_id": int,
          "is_valid": bool,
          "issues": list[str],
          "account_info": dict,
        }
    """
    issues: list[str] = []
    account_info: dict = {}

    try:
        account_info = client.get_account_details(code_combination_id)
    except Exception as exc:
        return {
            "code_combination_id": code_combination_id,
            "is_valid": False,
            "issues": [f"Account lookup failed: {exc}"],
            "account_info": {},
        }

    # Enabled check
    enabled = str(account_info.get("Enabled", "N")).upper()
    if enabled != "Y":
        issues.append("Account combination is disabled (GL_CODE_COMBINATIONS.ENABLED_FLAG = N).")

    # Summary account check — summary accounts cannot post journal entries
    is_summary = str(account_info.get("Summary", "N")).upper()
    if is_summary == "Y":
        issues.append(
            "Account is a summary account (SUMMARY_FLAG = Y). "
            "Journal entries must post to detail accounts only."
        )

    # Date range check — Oracle uses VARCHAR2 dates in DD-MON-RRRR format
    end_date_str = account_info.get("EndDateActive")
    if end_date_str:
        try:
            from datetime import date
            # Oracle REST returns ISO format in most endpoints
            end_date = date.fromisoformat(end_date_str[:10])
            if end_date < date.today():
                issues.append(
                    f"Account end date {end_date_str} is in the past "
                    "(GL_CODE_COMBINATIONS.END_DATE_ACTIVE has expired)."
                )
        except ValueError:
            logger.debug("Could not parse end date: %s", end_date_str)

    return {
        "code_combination_id": code_combination_id,
        "is_valid": len(issues) == 0,
        "issues": issues,
        "account_info": account_info,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_int(value: Any) -> Optional[int]:
    """Safely coerce a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
