"""
tests/test_gl_queries.py
------------------------
Unit tests for src/oracle/gl_queries.py.

Uses pytest-mock to stub FusionClient methods — no real Oracle connection needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.oracle.gl_queries import (
    get_unbalanced_journals,
    get_journal_detail,
    find_source_transaction,
    validate_account_coding,
    _coerce_int,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client(mocker):
    """Return a MagicMock that looks like FusionClient."""
    client = mocker.MagicMock()
    client._rest_base.return_value = "/fscmRestApi/resources/11.13.18.05"
    return client


SAMPLE_JOURNALS = [
    {
        "JournalHeaderId": 100432,
        "JournalName": "AP_ACCRUAL_JAN25_BATCH_003",
        "JeCategory": "Accrual",
        "JeSource": "Payables",
        "Status": "U",
        "PostingDate": "2025-01-31",
        "AccountedCurrencyCode": "USD",
        "TotalAcctDebit": 1425000.00,
        "TotalAcctCredit": 1424875.50,
        "Description": "AP Accrual batch",
        "CreatedBy": "AP_INTEGRATION",
    },
    {
        "JournalHeaderId": 100433,
        "JournalName": "AR_RECEIPT_JAN25_0001",
        "JeCategory": "Receipts",
        "JeSource": "Receivables",
        "Status": "U",
        "PostingDate": "2025-01-15",
        "AccountedCurrencyCode": "USD",
        "TotalAcctDebit": 500000.00,
        "TotalAcctCredit": 500000.00,  # balanced
        "Description": "AR receipt",
        "CreatedBy": "AR_INTEGRATION",
    },
    {
        "JournalHeaderId": 100434,
        "JournalName": "MANUAL_ADJ_001",
        "JeCategory": "Adjustment",
        "JeSource": "Manual",
        "Status": "U",
        "PostingDate": "2025-01-28",
        "AccountedCurrencyCode": "USD",
        "TotalAcctDebit": 75000.00,
        "TotalAcctCredit": 74999.98,  # tiny imbalance > 0.01
        "Description": "Manual adj",
        "CreatedBy": "GL_ACCOUNTANT",
    },
]

SAMPLE_LINES = [
    {
        "JournalLineId": 1,
        "LineNumber": 1,
        "CodeCombinationId": 10001,
        "Segment1": "01", "Segment2": "6010", "Segment3": "000", "Segment4": "0000",
        "EnteredDebit": 1425000.00, "EnteredCredit": 0,
        "AccountedDebit": 1425000.00, "AccountedCredit": 0,
        "Description": "Expense line",
    },
    {
        "JournalLineId": 2,
        "LineNumber": 2,
        "CodeCombinationId": 10002,
        "Segment1": "01", "Segment2": "2100", "Segment3": "000", "Segment4": "0000",
        "EnteredDebit": 0, "EnteredCredit": 1424875.50,
        "AccountedDebit": 0, "AccountedCredit": 1424875.50,
        "Description": "AP control line",
    },
]

SAMPLE_ACCOUNT = {
    "CodeCombinationId": 10001,
    "Segment1": "01",
    "Segment2": "6010",
    "Enabled": "Y",
    "Summary": "N",
    "AccountType": "E",
    "Description": "Professional Services",
}


# ---------------------------------------------------------------------------
# get_unbalanced_journals
# ---------------------------------------------------------------------------

class TestGetUnbalancedJournals:

    def test_returns_only_imbalanced_journals(self, mock_client):
        mock_client.get_journals.return_value = SAMPLE_JOURNALS
        result = get_unbalanced_journals(mock_client, ledger_id=1001, period_name="Jan-25")

        # Only JE 100432 (diff=124.50) and 100434 (diff=0.02) should be returned
        assert len(result) == 2
        ids = {j["journal_header_id"] for j in result}
        assert 100432 in ids
        assert 100434 in ids
        assert 100433 not in ids  # balanced

    def test_imbalance_amount_precision(self, mock_client):
        mock_client.get_journals.return_value = SAMPLE_JOURNALS
        result = get_unbalanced_journals(mock_client, ledger_id=1001, period_name="Jan-25")

        ap_journal = next(j for j in result if j["journal_header_id"] == 100432)
        assert abs(ap_journal["imbalance_amount"] - 124.50) < 0.001

    def test_returns_empty_when_all_balanced(self, mock_client):
        balanced = [
            {**j, "TotalAcctDebit": 1000.00, "TotalAcctCredit": 1000.00}
            for j in SAMPLE_JOURNALS
        ]
        mock_client.get_journals.return_value = balanced
        result = get_unbalanced_journals(mock_client, ledger_id=1001, period_name="Jan-25")
        assert result == []

    def test_tolerates_tiny_difference_under_threshold(self, mock_client):
        """Difference of exactly 0.01 should NOT be flagged (> not >=)."""
        journals = [
            {
                **SAMPLE_JOURNALS[0],
                "TotalAcctDebit": 1000.01,
                "TotalAcctCredit": 1000.00,
            }
        ]
        mock_client.get_journals.return_value = journals
        result = get_unbalanced_journals(mock_client, ledger_id=1001, period_name="Jan-25")
        assert len(result) == 0

    def test_passes_status_filter_to_client(self, mock_client):
        mock_client.get_journals.return_value = []
        get_unbalanced_journals(mock_client, 1001, "Jan-25", status_filter="P")
        mock_client.get_journals.assert_called_once_with(1001, "Jan-25", status_filter="P")

    def test_result_contains_expected_fields(self, mock_client):
        mock_client.get_journals.return_value = [SAMPLE_JOURNALS[0]]
        result = get_unbalanced_journals(mock_client, 1001, "Jan-25")
        assert len(result) == 1
        rec = result[0]
        assert "journal_header_id" in rec
        assert "journal_name" in rec
        assert "category" in rec
        assert "source" in rec
        assert "total_dr" in rec
        assert "total_cr" in rec
        assert "imbalance_amount" in rec
        assert "currency_code" in rec


# ---------------------------------------------------------------------------
# get_journal_detail
# ---------------------------------------------------------------------------

class TestGetJournalDetail:

    def test_returns_header_and_lines(self, mock_client):
        mock_client._get.return_value = SAMPLE_JOURNALS[0]
        mock_client.get_journal_lines.return_value = SAMPLE_LINES
        mock_client.get_account_details.return_value = SAMPLE_ACCOUNT

        result = get_journal_detail(mock_client, 100432)

        assert "header" in result
        assert "lines" in result
        assert len(result["lines"]) == 2

    def test_computes_totals_and_imbalance(self, mock_client):
        mock_client._get.return_value = SAMPLE_JOURNALS[0]
        mock_client.get_journal_lines.return_value = SAMPLE_LINES
        mock_client.get_account_details.return_value = SAMPLE_ACCOUNT

        result = get_journal_detail(mock_client, 100432)

        assert result["total_accounted_dr"] == 1425000.00
        assert result["total_accounted_cr"] == 1424875.50
        assert abs(result["imbalance"] - 124.50) < 0.001

    def test_enriches_lines_with_account_info(self, mock_client):
        mock_client._get.return_value = SAMPLE_JOURNALS[0]
        mock_client.get_journal_lines.return_value = SAMPLE_LINES
        mock_client.get_account_details.return_value = SAMPLE_ACCOUNT

        result = get_journal_detail(mock_client, 100432)
        first_line = result["lines"][0]

        assert "account_description" in first_line
        assert "account_type" in first_line
        assert "account_segments" in first_line
        assert first_line["account_segments"]["segment1"] == "01"

    def test_handles_account_fetch_failure_gracefully(self, mock_client):
        mock_client._get.return_value = SAMPLE_JOURNALS[0]
        mock_client.get_journal_lines.return_value = SAMPLE_LINES
        mock_client.get_account_details.side_effect = Exception("CCID not found")

        # Should not raise — account_description will be None
        result = get_journal_detail(mock_client, 100432)
        assert len(result["lines"]) == 2
        assert result["lines"][0]["account_description"] is None


# ---------------------------------------------------------------------------
# find_source_transaction
# ---------------------------------------------------------------------------

class TestFindSourceTransaction:

    def test_extracts_xla_metadata(self, mock_client):
        mock_client.get_xla_entries.return_value = [
            {
                "AeHeaderId": 88123,
                "AeLineNum": 1,
                "ApplicationId": 222,
                "EventId": 887234,
                "EventClassCode": "INVOICES",
                "EntityCode": "AP_INVOICES",
                "SourceIdInt1": 88734,
                "AccountedDebit": 1425000.00,
                "AccountedCredit": 0,
            }
        ]
        mock_client.get_subledger_transaction.return_value = {
            "InvoiceId": 88734,
            "InvoiceNum": "INV-2025-01-88734",
        }

        result = find_source_transaction(mock_client, 100432)

        assert result["application_id"] == 222
        assert result["event_class_code"] == "INVOICES"
        assert result["entity_code"] == "AP_INVOICES"
        assert result["source_id"] == 88734
        assert result["xla_line_count"] == 1
        assert result["source_transaction"]["InvoiceId"] == 88734

    def test_returns_gracefully_when_no_xla_entries(self, mock_client):
        mock_client.get_xla_entries.return_value = []

        result = find_source_transaction(mock_client, 100432)

        assert result["xla_ae_header_id"] is None
        assert result["xla_line_count"] == 0
        assert "tracing_notes" in result
        assert "No XLA entries" in result["tracing_notes"]

    def test_handles_subledger_fetch_failure(self, mock_client):
        mock_client.get_xla_entries.return_value = [
            {
                "AeHeaderId": 88123,
                "AeLineNum": 1,
                "ApplicationId": 222,
                "EventId": 887234,
                "EventClassCode": "INVOICES",
                "EntityCode": "AP_INVOICES",
                "SourceIdInt1": 88734,
            }
        ]
        mock_client.get_subledger_transaction.side_effect = Exception("503 Service Unavailable")

        result = find_source_transaction(mock_client, 100432)
        # Should not raise — source_transaction is empty dict
        assert result["source_transaction"] == {}
        assert result["event_class_code"] == "INVOICES"


# ---------------------------------------------------------------------------
# validate_account_coding
# ---------------------------------------------------------------------------

class TestValidateAccountCoding:

    def test_valid_active_account(self, mock_client):
        mock_client.get_account_details.return_value = {
            "CodeCombinationId": 10001,
            "Enabled": "Y",
            "Summary": "N",
            "EndDateActive": None,
            "AccountType": "E",
        }
        result = validate_account_coding(mock_client, 10001, 1001)
        assert result["is_valid"] is True
        assert result["issues"] == []

    def test_disabled_account_flagged(self, mock_client):
        mock_client.get_account_details.return_value = {
            "Enabled": "N",
            "Summary": "N",
        }
        result = validate_account_coding(mock_client, 10001, 1001)
        assert result["is_valid"] is False
        assert any("disabled" in issue for issue in result["issues"])

    def test_summary_account_flagged(self, mock_client):
        mock_client.get_account_details.return_value = {
            "Enabled": "Y",
            "Summary": "Y",
        }
        result = validate_account_coding(mock_client, 10001, 1001)
        assert result["is_valid"] is False
        assert any("summary" in issue.lower() for issue in result["issues"])

    def test_expired_account_flagged(self, mock_client):
        mock_client.get_account_details.return_value = {
            "Enabled": "Y",
            "Summary": "N",
            "EndDateActive": "2020-12-31",
        }
        result = validate_account_coding(mock_client, 10001, 1001)
        assert result["is_valid"] is False
        assert any("end date" in issue.lower() for issue in result["issues"])

    def test_api_failure_returns_invalid(self, mock_client):
        mock_client.get_account_details.side_effect = Exception("HTTP 404")
        result = validate_account_coding(mock_client, 99999, 1001)
        assert result["is_valid"] is False
        assert len(result["issues"]) == 1
        assert "lookup failed" in result["issues"][0]


# ---------------------------------------------------------------------------
# _coerce_int helper
# ---------------------------------------------------------------------------

class TestCoerceInt:

    def test_integer_input(self):
        assert _coerce_int(42) == 42

    def test_string_integer(self):
        assert _coerce_int("100432") == 100432

    def test_none_returns_none(self):
        assert _coerce_int(None) is None

    def test_non_numeric_string_returns_none(self):
        assert _coerce_int("abc") is None

    def test_float_truncated(self):
        assert _coerce_int(3.9) == 3
