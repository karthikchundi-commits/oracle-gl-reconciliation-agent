"""
tests/test_fbdi_generator.py
-----------------------------
Unit tests for src/oracle/fbdi_generator.py.

Tests the FBDI CSV generation, balance validation, and segment parsing.
All tests write to a temporary directory (pytest tmp_path fixture).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.oracle.fbdi_generator import (
    CorrectionLine,
    generate_corrective_journal,
    validate_fbdi,
    _GL_INTERFACE_COLUMNS,
    _REQUIRED_COLUMNS,
)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_ORIGINAL_JOURNAL = {
    "header": {
        "JournalHeaderId": 100432,
        "JournalName": "AP_ACCRUAL_JAN25_BATCH_003",
        "JeCategory": "Accrual",
        "JeSource": "Payables",
        "Status": "U",
        "PostingDate": "2025-01-31",
        "AccountedCurrencyCode": "USD",
        "LedgerId": 1001,
        "TotalAcctDebit": 1425000.00,
        "TotalAcctCredit": 1424875.50,
    }
}

BALANCED_CORRECTION_LINES = [
    {
        "account": "01-6010-000-0000",
        "dr": 124.50,
        "cr": 0,
        "description": "FX rounding correction INV-2025-01-88734",
    },
    {
        "account": "01-2100-000-0000",
        "dr": 0,
        "cr": 124.50,
        "description": "FX rounding correction INV-2025-01-88734",
    },
]


# ---------------------------------------------------------------------------
# CorrectionLine
# ---------------------------------------------------------------------------

class TestCorrectionLine:

    def test_parses_segments_correctly(self):
        line = CorrectionLine("01-6010-000-0000", dr=100.00)
        assert line.segments["SEGMENT1"] == "01"
        assert line.segments["SEGMENT2"] == "6010"
        assert line.segments["SEGMENT3"] == "000"
        assert line.segments["SEGMENT4"] == "0000"

    def test_rejects_both_dr_and_cr(self):
        with pytest.raises(ValueError, match="both a debit and a credit"):
            CorrectionLine("01-6010-000-0000", dr=100.00, cr=50.00)

    def test_rejects_negative_amounts(self):
        with pytest.raises(ValueError, match="non-negative"):
            CorrectionLine("01-6010-000-0000", dr=-100.00)

    def test_rounds_to_two_decimal_places(self):
        line = CorrectionLine("01-6010-000-0000", dr=124.4999999)
        assert line.dr == 124.50

    def test_handles_up_to_ten_segments(self):
        line = CorrectionLine("01-02-03-04-05-06-07-08-09-10", dr=1.00)
        assert line.segments["SEGMENT10"] == "10"

    def test_ignores_segments_beyond_ten(self):
        line = CorrectionLine("01-02-03-04-05-06-07-08-09-10-11", dr=1.00)
        assert "SEGMENT11" not in line.segments


# ---------------------------------------------------------------------------
# generate_corrective_journal
# ---------------------------------------------------------------------------

class TestGenerateCorrective:

    def test_creates_csv_file(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        assert out.exists()
        assert out.suffix == ".csv"

    def test_file_name_includes_journal_id_and_period(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        assert "100432" in out.name
        assert "Jan25" in out.name

    def test_csv_has_correct_header(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        with out.open("r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for col in _REQUIRED_COLUMNS:
                assert col in reader.fieldnames, f"Missing column: {col}"

    def test_csv_has_correct_row_count(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        with out.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2

    def test_csv_status_is_new(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        with out.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            assert row["STATUS"] == "NEW"

    def test_csv_actual_flag_is_a(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        with out.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            assert row["ACTUAL_FLAG"] == "A"

    def test_csv_debit_and_credit_correct(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        with out.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        # First line: DR=124.50, CR=blank
        assert float(rows[0]["ENTERED_DR"]) == 124.50
        assert rows[0]["ENTERED_CR"] == ""
        # Second line: DR=blank, CR=124.50
        assert rows[1]["ENTERED_DR"] == ""
        assert float(rows[1]["ENTERED_CR"]) == 124.50

    def test_csv_segment_values_correct(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        with out.open("r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["SEGMENT1"] == "01"
        assert rows[0]["SEGMENT2"] == "6010"
        assert rows[1]["SEGMENT2"] == "2100"

    def test_raises_when_lines_empty(self, tmp_path):
        with pytest.raises(ValueError, match="at least two entries"):
            generate_corrective_journal(
                original_journal=SAMPLE_ORIGINAL_JOURNAL,
                correction_lines=[],
                period="Jan-25",
                output_dir=str(tmp_path),
            )

    def test_raises_when_lines_unbalanced(self, tmp_path):
        unbalanced = [
            {"account": "01-6010-000-0000", "dr": 200.00, "cr": 0, "description": "DR"},
            {"account": "01-2100-000-0000", "dr": 0, "cr": 100.00, "description": "CR"},
        ]
        with pytest.raises(ValueError, match="out of balance"):
            generate_corrective_journal(
                original_journal=SAMPLE_ORIGINAL_JOURNAL,
                correction_lines=unbalanced,
                period="Jan-25",
                output_dir=str(tmp_path),
            )

    def test_supports_flat_journal_dict(self, tmp_path):
        """Should work when original_journal is the flat header (not nested under 'header')."""
        flat_journal = {
            "JournalHeaderId": 100432,
            "LedgerId": 1001,
            "PostingDate": "2025-01-31",
            "AccountedCurrencyCode": "USD",
        }
        out = generate_corrective_journal(
            original_journal=flat_journal,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        assert out.exists()


# ---------------------------------------------------------------------------
# validate_fbdi
# ---------------------------------------------------------------------------

class TestValidateFbdi:

    def test_valid_file_returns_no_issues(self, tmp_path):
        out = generate_corrective_journal(
            original_journal=SAMPLE_ORIGINAL_JOURNAL,
            correction_lines=BALANCED_CORRECTION_LINES,
            period="Jan-25",
            output_dir=str(tmp_path),
        )
        result = validate_fbdi(out)
        assert result["valid"] is True
        assert result["issues"] == []
        assert result["row_count"] == 2
        assert result["total_dr"] == 124.50
        assert result["total_cr"] == 124.50

    def test_missing_file_returns_invalid(self, tmp_path):
        result = validate_fbdi(tmp_path / "nonexistent.csv")
        assert result["valid"] is False
        assert "not found" in result["issues"][0]

    def test_empty_file_returns_invalid(self, tmp_path):
        empty = tmp_path / "empty.csv"
        empty.write_text("", encoding="utf-8")
        result = validate_fbdi(empty)
        assert result["valid"] is False
        assert "empty" in result["issues"][0].lower()

    def test_detects_imbalanced_journal(self, tmp_path):
        """Write a CSV with unequal totals and expect balance error."""
        csv_path = tmp_path / "bad.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_GL_INTERFACE_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerow({
                "STATUS": "NEW",
                "LEDGER_ID": "1001",
                "ACCOUNTING_DATE": "2025-01-31",
                "CURRENCY_CODE": "USD",
                "DATE_CREATED": "2025-01-31",
                "CREATED_BY": "TEST",
                "ACTUAL_FLAG": "A",
                "USER_JE_CATEGORY_NAME": "Adjustment",
                "USER_JE_SOURCE_NAME": "Manual",
                "SEGMENT1": "01",
                "ENTERED_DR": "200.00",
                "ENTERED_CR": "",
            })
            writer.writerow({
                "STATUS": "NEW",
                "LEDGER_ID": "1001",
                "ACCOUNTING_DATE": "2025-01-31",
                "CURRENCY_CODE": "USD",
                "DATE_CREATED": "2025-01-31",
                "CREATED_BY": "TEST",
                "ACTUAL_FLAG": "A",
                "USER_JE_CATEGORY_NAME": "Adjustment",
                "USER_JE_SOURCE_NAME": "Manual",
                "SEGMENT1": "01",
                "ENTERED_DR": "",
                "ENTERED_CR": "100.00",
            })

        result = validate_fbdi(csv_path)
        assert result["valid"] is False
        assert any("balance" in i.lower() or "imbalance" in i.lower() for i in result["issues"])

    def test_detects_invalid_actual_flag(self, tmp_path):
        csv_path = tmp_path / "bad_flag.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_GL_INTERFACE_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerow({
                "STATUS": "NEW",
                "LEDGER_ID": "1001",
                "ACCOUNTING_DATE": "2025-01-31",
                "CURRENCY_CODE": "USD",
                "DATE_CREATED": "2025-01-31",
                "CREATED_BY": "TEST",
                "ACTUAL_FLAG": "X",  # invalid
                "USER_JE_CATEGORY_NAME": "Adjustment",
                "USER_JE_SOURCE_NAME": "Manual",
                "SEGMENT1": "01",
                "ENTERED_DR": "100.00",
                "ENTERED_CR": "",
            })
            writer.writerow({
                "STATUS": "NEW",
                "LEDGER_ID": "1001",
                "ACCOUNTING_DATE": "2025-01-31",
                "CURRENCY_CODE": "USD",
                "DATE_CREATED": "2025-01-31",
                "CREATED_BY": "TEST",
                "ACTUAL_FLAG": "X",
                "USER_JE_CATEGORY_NAME": "Adjustment",
                "USER_JE_SOURCE_NAME": "Manual",
                "SEGMENT1": "01",
                "ENTERED_DR": "",
                "ENTERED_CR": "100.00",
            })

        result = validate_fbdi(csv_path)
        assert result["valid"] is False
        assert any("ACTUAL_FLAG" in i for i in result["issues"])
