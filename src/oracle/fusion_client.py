"""
src/oracle/fusion_client.py
---------------------------
Oracle Fusion Cloud ERP REST API client.

Handles:
  - OAuth 2.0 Client Credentials token acquisition and automatic refresh
  - Base HTTPS session with proper Accept/Content-Type headers
  - GL journal retrieval via fscmRestApi
  - Account coding lookup
  - Ledger info retrieval
  - FBDI file upload via erpintegrations endpoint
  - XLA subledger entry retrieval via OTBI / BI Publisher REST

Oracle REST API reference:
  https://docs.oracle.com/en/cloud/saas/financials/24d/farfa/
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class OracleAPIError(Exception):
    """Raised when an Oracle REST API call returns a non-2xx status."""

    def __init__(self, method: str, url: str, status_code: int, body: str) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Oracle API {method} {url} → HTTP {status_code}: {body[:400]}"
        )


class OracleAuthError(Exception):
    """Raised when OAuth token acquisition fails."""


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------

class _TokenCache:
    """Stores a bearer token and its expiry timestamp."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def is_valid(self, buffer_seconds: int = 60) -> bool:
        return self._token is not None and time.time() < (self._expires_at - buffer_seconds)

    def set(self, token: str, expires_in: int) -> None:
        self._token = token
        self._expires_at = time.time() + expires_in

    @property
    def token(self) -> Optional[str]:
        return self._token


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class FusionClient:
    """
    Oracle Fusion Cloud REST API client.

    Supports OAuth 2.0 Client Credentials (preferred) or HTTP Basic Auth.
    All methods return parsed JSON dicts / lists; HTTP errors raise
    OracleAPIError.

    Parameters
    ----------
    host : str
        Oracle Fusion Cloud base URL (no trailing slash).
        e.g. ``https://yourinstance.fa.us2.oraclecloud.com``
    client_id : str, optional
        OAuth 2.0 client_id from IDCS / OCI IAM.
    client_secret : str, optional
        OAuth 2.0 client_secret.
    token_url : str, optional
        IDCS token endpoint, e.g.
        ``https://idcs-abc123.identity.oraclecloud.com/oauth2/v1/token``
    username : str, optional
        Basic auth username (fallback).
    password : str, optional
        Basic auth password (fallback).
    api_version : str
        fscmRestApi version segment; defaults to ``11.13.18.05``.
    connect_timeout : int
    read_timeout : int
    """

    _REST_BASE = "/fscmRestApi/resources/{version}"
    _INTEGRATION_BASE = "/erpintegrations"
    _OTBI_BASE = "/analytics/saw.dll"
    _BIP_BASE = "/xmlpserver/services/rest/v1/reports"

    def __init__(
        self,
        host: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        token_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_version: str = "11.13.18.05",
        connect_timeout: int = 10,
        read_timeout: int = 60,
    ) -> None:
        self.host = host.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.username = username
        self.password = password
        self.api_version = api_version
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

        self._token_cache = _TokenCache()
        self._session = self._build_session()

    # ------------------------------------------------------------------
    # Session / auth
    # ------------------------------------------------------------------

    def _build_session(self) -> Session:
        session = Session()
        # Retry on transient 5xx and connection errors
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PATCH"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-By": "oracle-gl-reconciliation-agent",
            }
        )
        return session

    def _get_oauth_token(self) -> str:
        """Acquire an OAuth 2.0 bearer token using Client Credentials grant."""
        if not self.token_url or not self.client_id or not self.client_secret:
            raise OracleAuthError(
                "OAuth configuration incomplete: token_url, client_id, and "
                "client_secret are all required for OAuth2 authentication."
            )

        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        response = requests.post(
            self.token_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=urlencode(
                {
                    "grant_type": "client_credentials",
                    "scope": "urn:opc:resource:consumer::all",
                }
            ),
            timeout=(self.connect_timeout, self.read_timeout),
        )

        if not response.ok:
            raise OracleAuthError(
                f"Token acquisition failed: HTTP {response.status_code} — "
                f"{response.text[:400]}"
            )

        payload = response.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 3600))
        if not token:
            raise OracleAuthError("Token response missing 'access_token' field.")

        self._token_cache.set(token, expires_in)
        logger.info("OAuth token acquired; expires in %ds", expires_in)
        return token

    def _auth_header(self) -> dict[str, str]:
        """Return the appropriate Authorization header."""
        if self.client_id and self.client_secret and self.token_url:
            if not self._token_cache.is_valid():
                self._get_oauth_token()
            return {"Authorization": f"Bearer {self._token_cache.token}"}

        if self.username and self.password:
            credentials = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            return {"Authorization": f"Basic {credentials}"}

        raise OracleAuthError(
            "No authentication method configured. Provide either OAuth2 "
            "credentials (client_id, client_secret, token_url) or basic auth "
            "(username, password)."
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _rest_base(self) -> str:
        return self._REST_BASE.format(version=self.api_version)

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = self._url(path)
        headers = self._auth_header()
        logger.debug("GET %s  params=%s", url, params)

        resp: Response = self._session.get(
            url,
            headers=headers,
            params=params,
            timeout=(self.connect_timeout, self.read_timeout),
        )
        self._raise_for_status("GET", url, resp)
        return resp.json()

    def _post(self, path: str, payload: Any, content_type: str = "application/json") -> Any:
        url = self._url(path)
        headers = {**self._auth_header(), "Content-Type": content_type}
        logger.debug("POST %s", url)

        if content_type == "application/json":
            resp = self._session.post(
                url,
                headers=headers,
                json=payload,
                timeout=(self.connect_timeout, self.read_timeout),
            )
        else:
            resp = self._session.post(
                url,
                headers=headers,
                data=payload,
                timeout=(self.connect_timeout, self.read_timeout),
            )
        self._raise_for_status("POST", url, resp)
        return resp.json()

    def _post_multipart(self, path: str, fields: dict, file_path: Path) -> Any:
        url = self._url(path)
        headers = self._auth_header()
        # Remove Content-Type so requests sets multipart boundary automatically
        headers.pop("Content-Type", None)

        with file_path.open("rb") as fh:
            files = {"file": (file_path.name, fh, "application/octet-stream")}
            resp = self._session.post(
                url,
                headers=headers,
                data=fields,
                files=files,
                timeout=(self.connect_timeout, self.read_timeout * 3),
            )
        self._raise_for_status("POST(multipart)", url, resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(method: str, url: str, resp: Response) -> None:
        if not resp.ok:
            raise OracleAPIError(method, url, resp.status_code, resp.text)

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_journals(
        self,
        ledger_id: int,
        period_name: str,
        status_filter: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """
        Retrieve GL journal headers for a given ledger and accounting period.

        Oracle REST endpoint:
          GET /fscmRestApi/resources/{version}/generalLedgerJournals

        Parameters
        ----------
        ledger_id : int
            GL_LEDGERS.LEDGER_ID
        period_name : str
            GL_PERIODS.PERIOD_NAME — e.g. ``Jan-25``
        status_filter : str, optional
            Filter by JournalStatus; common values: U (unposted), P (posted).
            If None, returns all statuses.
        limit : int
            Page size (max 500 per Oracle docs).
        offset : int
            Pagination offset.

        Returns
        -------
        list[dict]
            List of journal header dicts from the ``items`` array.
        """
        base = self._rest_base()
        path = f"{base}/generalLedgerJournals"

        # Build finder query string (Oracle REST uses finder= pattern)
        finder_parts = [
            f"LedgerId={ledger_id}",
            f"AccountingPeriod={period_name}",
        ]
        if status_filter:
            finder_parts.append(f"JournalStatus={status_filter}")

        params: dict[str, Any] = {
            "finder": f"JournalHeaderFinder;{','.join(finder_parts)}",
            "limit": limit,
            "offset": offset,
            "fields": (
                "JournalHeaderId,JournalName,JeCategory,JeSource,"
                "Status,PostingDate,AccountedCurrencyCode,"
                "TotalEnteredDebit,TotalEnteredCredit,"
                "TotalAcctDebit,TotalAcctCredit,"
                "Description,CreatedBy,CreationDate,LastUpdateDate"
            ),
        }

        data = self._get(path, params=params)
        items = data.get("items", [])
        logger.info(
            "get_journals: ledger=%s period=%s → %d journals", ledger_id, period_name, len(items)
        )
        return items

    def get_journal_lines(self, journal_header_id: int) -> list[dict]:
        """
        Retrieve all lines for a GL journal header.

        Oracle REST endpoint:
          GET /fscmRestApi/resources/{version}/generalLedgerJournals/{JournalHeaderId}/child/lines

        Returns
        -------
        list[dict]
            Journal lines with account coding, entered/accounted amounts,
            and description fields.
        """
        base = self._rest_base()
        path = (
            f"{base}/generalLedgerJournals/{journal_header_id}/child/lines"
        )
        params = {
            "limit": 1000,
            "fields": (
                "JournalLineId,JournalHeaderId,LineNumber,"
                "CodeCombinationId,AccountedDebit,AccountedCredit,"
                "EnteredDebit,EnteredCredit,Description,"
                "Segment1,Segment2,Segment3,Segment4,Segment5,"
                "Segment6,Segment7,Segment8,Segment9,Segment10,"
                "TaxCode,TaxRate"
            ),
        }
        data = self._get(path, params=params)
        lines = data.get("items", [])
        logger.debug("get_journal_lines: header=%d → %d lines", journal_header_id, len(lines))
        return lines

    def get_account_details(self, code_combination_id: int) -> dict:
        """
        Retrieve GL account segment values and validity from GL_CODE_COMBINATIONS.

        Oracle REST endpoint:
          GET /fscmRestApi/resources/{version}/ledgerAccountCombinations/{CodeCombinationId}

        Returns
        -------
        dict
            Account combination with all segment values, enabled flag,
            and summary/detail indicator.
        """
        base = self._rest_base()
        path = f"{base}/ledgerAccountCombinations/{code_combination_id}"
        params = {
            "fields": (
                "CodeCombinationId,Segment1,Segment2,Segment3,Segment4,"
                "Segment5,Segment6,Segment7,Segment8,Segment9,Segment10,"
                "Enabled,Summary,StartDateActive,EndDateActive,"
                "AccountType,Description"
            )
        }
        data = self._get(path, params=params)
        logger.debug("get_account_details: ccid=%d", code_combination_id)
        return data

    def get_ledger_info(self, ledger_id: int) -> dict:
        """
        Retrieve ledger metadata: currency, period type, chart of accounts.

        Oracle REST endpoint:
          GET /fscmRestApi/resources/{version}/ledgers/{LedgerId}

        Returns
        -------
        dict
            Ledger metadata including CurrencyCode, PeriodType,
            ChartOfAccountsId, LedgerName.
        """
        base = self._rest_base()
        path = f"{base}/ledgers/{ledger_id}"
        params = {
            "fields": (
                "LedgerId,LedgerName,CurrencyCode,PeriodType,"
                "ChartOfAccountsId,AcctgCalendarName,"
                "RetainedEarningsAccountValue,CumulativeTranslationAdjustmentAccountValue"
            )
        }
        data = self._get(path, params=params)
        logger.info("get_ledger_info: ledger=%d → %s", ledger_id, data.get("LedgerName"))
        return data

    def get_period_status(self, ledger_id: int, period_name: str) -> dict:
        """
        Retrieve period open/close status from GL_PERIOD_STATUSES.

        Oracle REST endpoint:
          GET /fscmRestApi/resources/{version}/accountingPeriods
        """
        base = self._rest_base()
        path = f"{base}/accountingPeriods"
        params = {
            "finder": f"AccountingPeriodFinder;LedgerId={ledger_id},PeriodName={period_name}",
            "fields": "PeriodName,PeriodStatus,PeriodYear,PeriodNum,StartDate,EndDate",
        }
        data = self._get(path, params=params)
        items = data.get("items", [])
        return items[0] if items else {}

    def submit_fbdi(self, file_path: Path, document_account: str = "fin$/generalLedger$/import$") -> dict:
        """
        Upload an FBDI file to Oracle ERP Cloud via the ERP Integrations REST API.

        Oracle REST endpoint:
          POST /erpintegrations

        The payload follows the Oracle ERP Integrations structure:
          OperationName: importBulkData
          DocumentContent: base64-encoded ZIP/CSV file
          DocumentAccount: UCM account path for GL Interface

        Parameters
        ----------
        file_path : Path
            Local path to the FBDI CSV or ZIP file.
        document_account : str
            UCM content server account path; defaults to GL import account.

        Returns
        -------
        dict
            Oracle response containing RequestId and Status.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"FBDI file not found: {file_path}")

        with file_path.open("rb") as fh:
            encoded_content = base64.b64encode(fh.read()).decode("utf-8")

        payload = {
            "OperationName": "importBulkData",
            "DocumentContent": encoded_content,
            "FileName": file_path.name,
            "ContentType": "text/csv" if file_path.suffix.lower() == ".csv" else "application/zip",
            "DocumentAccount": document_account,
            "JobPackageName": "/oracle/apps/ess/financials/generalLedger/programs/common",
            "JobDefName": "JournalImportLauncher",
        }

        logger.info("submit_fbdi: uploading %s (%d bytes)", file_path.name, file_path.stat().st_size)
        result = self._post(self._INTEGRATION_BASE, payload)
        logger.info("submit_fbdi: Oracle RequestId=%s", result.get("RequestId"))
        return result

    def get_xla_entries(
        self,
        journal_header_id: int,
        source_application_id: Optional[int] = None,
        event_class_code: Optional[str] = None,
    ) -> list[dict]:
        """
        Retrieve XLA subledger accounting entries linked to a GL journal.

        Uses the OTBI Analytics REST API to query XLA_AE_HEADERS and
        XLA_AE_LINES joined to XLA_EVENTS and XLA_TRANSACTION_ENTITIES.

        Oracle REST endpoint:
          GET /analytics/saw.dll?NQSQuery (OTBI logical SQL)
          or
          POST /xmlpserver/services/rest/v1/reports (BI Publisher)

        This implementation calls the OTBI Answers REST endpoint with a
        logical SQL query against the "GL - Journals Real Time" subject area.

        Parameters
        ----------
        journal_header_id : int
            GL_JE_HEADERS.JE_HEADER_ID
        source_application_id : int, optional
            XLA_AE_HEADERS.APPLICATION_ID — e.g. 222 = Payables, 222=AR etc.
        event_class_code : str, optional
            XLA_EVENTS.EVENT_CLASS_CODE — e.g. INVOICES, PAYMENTS

        Returns
        -------
        list[dict]
            List of XLA entry dicts with keys:
            ae_header_id, ae_line_num, application_id, event_id,
            event_class_code, entity_code, source_id_int_1,
            entered_dr, entered_cr, accounted_dr, accounted_cr,
            party_type_code, party_id
        """
        # OTBI logical SQL targeting the GL Journals Real Time subject area
        # XLA_AE_HEADERS.JE_HEADER_ID links to GL_JE_HEADERS.JE_HEADER_ID
        logical_sql = f"""
            SELECT
                "GL Journals"."Journal Line"."XLA AE Header ID" AS AeHeaderId,
                "GL Journals"."Journal Line"."XLA AE Line Num" AS AeLineNum,
                "GL Journals"."Journal Header"."Journal Header ID" AS JournalHeaderId,
                "GL Journals"."Journal Line"."XLA Application ID" AS ApplicationId,
                "GL Journals"."Journal Line"."XLA Event ID" AS EventId,
                "GL Journals"."Journal Line"."XLA Event Class Code" AS EventClassCode,
                "GL Journals"."Journal Line"."XLA Entity Code" AS EntityCode,
                "GL Journals"."Journal Line"."XLA Source Id Int 1" AS SourceIdInt1,
                "GL Journals"."Journal Line"."Accounted Debit" AS AccountedDebit,
                "GL Journals"."Journal Line"."Accounted Credit" AS AccountedCredit,
                "GL Journals"."Journal Line"."Entered Debit" AS EnteredDebit,
                "GL Journals"."Journal Line"."Entered Credit" AS EnteredCredit
            FROM "GL Journals"
            WHERE
                "GL Journals"."Journal Header"."Journal Header ID" = {journal_header_id}
            ORDER BY AeHeaderId, AeLineNum
        """.strip()

        path = self._OTBI_BASE
        params = {
            "Action": "Export",
            "Format": "json",
            "NQSQuery": logical_sql,
        }

        try:
            data = self._get(path, params=params)
            # OTBI returns rows in different shapes depending on version
            rows = data.get("rows", data.get("ResultSet", {}).get("Row", []))
            if isinstance(rows, dict):
                rows = [rows]
            logger.info(
                "get_xla_entries: journal_header_id=%d → %d XLA entries", journal_header_id, len(rows)
            )
            return rows
        except OracleAPIError as exc:
            # OTBI may be disabled or require different auth; fall back gracefully
            logger.warning(
                "get_xla_entries: OTBI query failed (%s). "
                "Returning empty list — check OTBI REST API configuration.",
                exc,
            )
            return []

    def get_subledger_transaction(self, application_id: int, source_id: int) -> dict:
        """
        Retrieve the originating subledger transaction record.

        Routes to the appropriate subledger REST resource based on
        application_id:
          200 = GL (no subledger)
          222 = Oracle Payables — /fscmRestApi/.../invoices/{id}
          222 = Oracle Receivables (AR) — separate application_id
          555 = Oracle Assets
          140 = Oracle Projects

        Parameters
        ----------
        application_id : int
            XLA_AE_HEADERS.APPLICATION_ID
        source_id : int
            XLA_TRANSACTION_ENTITIES.SOURCE_ID_INT_1 (PK of source table)

        Returns
        -------
        dict
            Subledger transaction details, or empty dict if unsupported.
        """
        base = self._rest_base()
        APP_ROUTES = {
            222: f"{base}/invoices/{source_id}",           # AP Payables
            222: f"{base}/receivablesInvoices/{source_id}", # AR (same ID, different path)
            555: f"{base}/assets/{source_id}",              # Fixed Assets
            140: f"{base}/projectCosts/{source_id}",        # Projects
        }

        # application_id 222 is both AP and AR in Fusion — differentiate by entity_code
        # In practice callers should check entity_code from get_xla_entries
        path = APP_ROUTES.get(application_id)
        if not path:
            logger.warning(
                "get_subledger_transaction: unsupported application_id=%d", application_id
            )
            return {"application_id": application_id, "source_id": source_id, "unsupported": True}

        try:
            return self._get(path)
        except OracleAPIError as exc:
            logger.warning("get_subledger_transaction: %s", exc)
            return {}

    def get_open_periods(self, ledger_id: int) -> list[dict]:
        """
        Return all open accounting periods for the ledger.

        Oracle REST endpoint:
          GET /fscmRestApi/resources/{version}/accountingPeriods
        """
        base = self._rest_base()
        path = f"{base}/accountingPeriods"
        params = {
            "finder": f"OpenPeriodFinder;LedgerId={ledger_id}",
            "fields": "PeriodName,PeriodStatus,PeriodYear,PeriodNum,StartDate,EndDate",
            "limit": 50,
        }
        data = self._get(path, params=params)
        return data.get("items", [])
