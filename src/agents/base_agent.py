"""
src/agents/base_agent.py
------------------------
Abstract base class for the Oracle GL Reconciliation Agent.

Provides:
  - Shared tool dispatch (_execute_tool) routing to gl_queries / fbdi_generator
  - System prompt construction with Oracle ERP domain knowledge
  - ReconciliationResult dataclass
  - Abstract interface (_call_llm, run) implemented by ClaudeGLAgent / OpenAIGLAgent

The agentic loop architecture follows a standard tool-use pattern:
  1. Send system prompt + user task to LLM
  2. LLM responds with one or more tool_use / function_call blocks
  3. Execute each tool, collect results as tool_result / tool messages
  4. Append results to conversation and call LLM again
  5. Repeat until stop_reason == 'end_turn' (Claude) or finish_reason == 'stop' (OpenAI)
  6. Extract final text response and populate ReconciliationResult
"""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
import json
import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config.settings import AgentSettings, NotificationSettings, OracleSettings
from src.oracle.fbdi_generator import generate_corrective_journal, validate_fbdi
from src.oracle.fusion_client import FusionClient
from src.oracle.gl_queries import (
    find_source_transaction,
    get_journal_detail,
    get_unbalanced_journals,
    validate_account_coding,
)
from src.reports.reconciliation_report import generate_report

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationResult:
    """
    Structured output returned by the agent after a reconciliation run.

    Attributes
    ----------
    ledger_id : int
    period_name : str
    run_timestamp : str          ISO timestamp of the run start
    imbalances_found : int       Count of journals where |DR-CR| > 0.01
    journals_analyzed : int      Total journals inspected in the period
    corrective_journals_drafted : int
    report_path : Optional[str]  Absolute path to the HTML report
    fbdi_files : list[str]       Paths to generated FBDI CSV files
    status : str                 RESOLVED | PENDING_APPROVAL | REQUIRES_INVESTIGATION
    agent_summary : str          Final plain-text summary from the LLM
    findings : list[dict]        Per-journal finding dicts
    errors : list[str]           Non-fatal errors encountered during the run
    """
    ledger_id: int
    period_name: str
    run_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    imbalances_found: int = 0
    journals_analyzed: int = 0
    corrective_journals_drafted: int = 0
    report_path: Optional[str] = None
    fbdi_files: list[str] = field(default_factory=list)
    status: str = "PENDING_APPROVAL"
    agent_summary: str = ""
    findings: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ledger_id": self.ledger_id,
            "period_name": self.period_name,
            "run_timestamp": self.run_timestamp,
            "imbalances_found": self.imbalances_found,
            "journals_analyzed": self.journals_analyzed,
            "corrective_journals_drafted": self.corrective_journals_drafted,
            "report_path": self.report_path,
            "fbdi_files": self.fbdi_files,
            "status": self.status,
            "agent_summary": self.agent_summary,
            "findings": self.findings,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an Oracle ERP GL Reconciliation Agent specializing in Oracle Fusion Cloud \
Financials. Your role is to autonomously detect General Ledger journal imbalances, \
trace them to their subledger source, draft corrective FBDI journal entries, and route \
them for human approval.

## Oracle Fusion GL Schema Context

You have access to these Oracle GL/XLA entities via REST API tools:

**GL Layer:**
- GL_LEDGERS — ledger definitions; LEDGER_ID is the primary key used in all GL operations
- GL_JE_HEADERS — journal entry headers; JE_HEADER_ID, JE_CATEGORY, JE_SOURCE, STATUS
  STATUS values: U=Unposted, P=Posted, S=Selected (stuck in posting process), I=In error
- GL_JE_LINES — journal lines; CODE_COMBINATION_ID links to GL_CODE_COMBINATIONS
- GL_CODE_COMBINATIONS — chart of accounts segment combinations; CCID is the PK
  ENABLED_FLAG='Y' required; SUMMARY_FLAG='N' required for postable accounts
- GL_PERIODS — accounting calendar; PERIOD_NAME must match exactly (e.g. 'Jan-25')

**XLA Subledger Accounting Layer:**
- XLA_AE_HEADERS — subledger accounting event headers; links to GL via JE_HEADER_ID
  APPLICATION_ID: 200=GL, 222=Payables, 222=Receivables (differentiated by ENTITY_CODE),
  555=Fixed Assets, 140=Projects, 260=Purchasing
- XLA_AE_LINES — subledger accounting lines; ACCOUNTED_DR/ACCOUNTED_CR
- XLA_EVENTS — accounting events; EVENT_CLASS_CODE:
  INVOICES, PAYMENTS, CREDIT_MEMOS, RECEIPTS, DEBIT_MEMOS, ADJUSTMENTS,
  DEPRECIATION, RETIREMENTS, COST_ADJUSTMENTS
- XLA_TRANSACTION_ENTITIES — SOURCE_ID_INT_1 is the FK to the source table
  (AP_INVOICES_ALL.INVOICE_ID, AR_CASH_RECEIPTS_ALL.CASH_RECEIPT_ID, etc.)

## Common Root Causes of GL Imbalances

1. **FX rounding** — Multi-currency invoices where the accounted_amount rounding
   differs between the AP distribution and the GL journal line. Corrective action:
   create a rounding adjustment to the AP/AR control account.

2. **Subledger-to-GL sync failure** — XLA_AE_HEADERS exists but GL_JE_LINES is
   incomplete; indicates Oracle background process (Transfer Journal Entries to GL)
   failed mid-run. Corrective action: notify Oracle Support; do not manually correct.

3. **Manual journal coding error** — Account segment combination invalid or inactive.
   Validate using get_account_details; correct by drafting a reversal and re-entry.

4. **Period-end accrual timing** — Accrual reversal posted in wrong period, leaving
   a one-sided entry. Trace via XLA event_class_code=ACCRUALS.

5. **Intercompany imbalance** — Balancing segment (typically SEGMENT1/company code)
   not offset within the same journal. Oracle autobalancing should handle this but
   may fail if the intercompany accounts are misconfigured.

## FBDI Journal Correction Rules

- GL_INTERFACE lines must have ENTERED_DR OR ENTERED_CR (never both on same line)
- STATUS must be 'NEW' for Journal Import to pick up the file
- ACTUAL_FLAG='A' for actual journals; 'B' for budget; 'E' for encumbrance
- USER_JE_SOURCE_NAME and USER_JE_CATEGORY_NAME must match Oracle lookup values exactly
- The corrective journal must balance: sum(ENTERED_DR) == sum(ENTERED_CR)
- Account combinations must be enabled and detail (not summary) accounts

## Agent Workflow

For each reconciliation run:
1. Call get_unbalanced_journals to identify all imbalanced journals in the period
2. For each imbalanced journal:
   a. Call get_journal_detail to examine all lines and amounts
   b. Call find_source_transaction to determine the XLA/subledger origin
   c. Analyze the root cause based on the data
   d. Call draft_corrective_journal with balanced correction lines
   e. Call request_approval with a clear summary for the controller
3. Call generate_reconciliation_report with all findings
4. Provide a concise final summary of actions taken

Always be precise with monetary amounts. Always verify that corrective journals balance \
before submitting. When in doubt about root cause, set status=REQUIRES_INVESTIGATION \
rather than guessing. Document your reasoning in the finding descriptions.\
"""


# ---------------------------------------------------------------------------
# Abstract base agent
# ---------------------------------------------------------------------------

class BaseGLReconciliationAgent(ABC):
    """
    Abstract base class for the Oracle GL Reconciliation Agent.

    Concrete subclasses (ClaudeGLAgent, OpenAIGLAgent) implement _call_llm
    and run using their respective SDK.

    Parameters
    ----------
    fusion_client : FusionClient
        Authenticated Oracle Fusion Cloud REST client.
    oracle_settings : OracleSettings
    agent_settings : AgentSettings
    notification_settings : NotificationSettings
    """

    def __init__(
        self,
        fusion_client: FusionClient,
        oracle_settings: OracleSettings,
        agent_settings: AgentSettings,
        notification_settings: NotificationSettings,
    ) -> None:
        self.client = fusion_client
        self.oracle_settings = oracle_settings
        self.agent_settings = agent_settings
        self.notification_settings = notification_settings
        self._output_dir = Path(agent_settings.output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Accumulated findings during a run; populated by _execute_tool
        self._findings: list[dict] = []
        self._fbdi_files: list[str] = []
        self._journals_analyzed: int = 0
        self._report_path: Optional[str] = None

    @abstractmethod
    def run(self, ledger_id: int, period_name: str) -> ReconciliationResult:
        """
        Execute a full GL reconciliation for the given ledger and period.

        Returns a ReconciliationResult populated with all findings and outputs.
        """
        ...

    @abstractmethod
    def _call_llm(self, messages: list[dict], tools: list[dict]) -> dict:
        """
        Call the LLM provider with the current conversation and tools.

        Parameters
        ----------
        messages : list[dict]
            Conversation history in the provider's native message format.
        tools : list[dict]
            Tool definitions in the provider's native format.

        Returns
        -------
        dict
            Raw LLM response in the provider's native format.
        """
        ...

    def _build_system_prompt(self) -> str:
        """Return the system prompt with Oracle ERP domain context."""
        return _SYSTEM_PROMPT

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """
        Dispatch a tool call to the appropriate gl_queries / fbdi_generator function.

        All tool results are returned as JSON strings (the LLM receives string
        content in tool_result messages for both Claude and OpenAI).

        Parameters
        ----------
        tool_name : str
            Must be one of the names in TOOL_NAMES.
        tool_input : dict
            Keyword arguments for the tool function.

        Returns
        -------
        str
            JSON-serialized result dict or error message.
        """
        logger.info("Executing tool: %s  input=%s", tool_name, json.dumps(tool_input)[:200])

        try:
            result = self._dispatch(tool_name, tool_input)
            return json.dumps(result, default=str)
        except Exception as exc:
            logger.exception("Tool %s raised an exception: %s", tool_name, exc)
            error_payload = {
                "error": True,
                "tool": tool_name,
                "message": str(exc),
                "type": type(exc).__name__,
            }
            return json.dumps(error_payload)

    def _dispatch(self, tool_name: str, args: dict) -> Any:
        """Route tool_name to the concrete implementation."""

        if tool_name == "get_unbalanced_journals":
            ledger_id = int(args["ledger_id"])
            period_name = str(args["period_name"])
            results = get_unbalanced_journals(self.client, ledger_id, period_name)
            self._journals_analyzed = len(
                self.client.get_journals(ledger_id, period_name)
            )
            return {
                "unbalanced_journals": results,
                "count": len(results),
                "journals_analyzed": self._journals_analyzed,
            }

        elif tool_name == "get_journal_detail":
            header_id = int(args["journal_header_id"])
            detail = get_journal_detail(self.client, header_id)
            return detail

        elif tool_name == "get_account_details":
            ccid = int(args["code_combination_id"])
            info = self.client.get_account_details(ccid)
            # Also run validation
            validation = validate_account_coding(
                self.client, ccid, self.oracle_settings.ledger_id
            )
            return {"account_info": info, "validation": validation}

        elif tool_name == "find_source_transaction":
            header_id = int(args["journal_header_id"])
            return find_source_transaction(self.client, header_id)

        elif tool_name == "draft_corrective_journal":
            original_id = int(args["original_journal_id"])
            correction_lines = args["correction_lines"]
            reason = str(args.get("reason", ""))

            # Fetch the original journal header for FBDI context
            detail = get_journal_detail(self.client, original_id)

            fbdi_path = generate_corrective_journal(
                original_journal=detail,
                correction_lines=correction_lines,
                period=self.oracle_settings.period_name,
                output_dir=str(self._output_dir),
                created_by="GL_RECON_AGENT",
            )

            validation = validate_fbdi(fbdi_path)
            self._fbdi_files.append(str(fbdi_path))

            # Accumulate finding
            finding = {
                "journal_header_id": original_id,
                "journal_name": detail.get("header", {}).get("JournalName", f"JE-{original_id}"),
                "imbalance_amount": detail.get("imbalance", 0),
                "root_cause": reason,
                "corrective_action": f"Corrective journal drafted: {fbdi_path.name}",
                "fbdi_file": str(fbdi_path),
                "status": "PENDING_APPROVAL",
            }
            self._findings.append(finding)

            return {
                "fbdi_file": str(fbdi_path),
                "validation": validation,
                "finding_recorded": True,
                "reason": reason,
            }

        elif tool_name == "generate_reconciliation_report":
            findings = args.get("findings", self._findings)
            period_name = str(args.get("period_name", self.oracle_settings.period_name))
            ledger_name = str(args.get("ledger_name", f"Ledger {self.oracle_settings.ledger_id}"))

            report_path = generate_report(
                findings=findings,
                period_name=period_name,
                ledger_name=ledger_name,
                output_dir=str(self._output_dir),
            )
            self._report_path = str(report_path)

            return {
                "report_path": str(report_path),
                "finding_count": len(findings),
                "period_name": period_name,
                "ledger_name": ledger_name,
            }

        elif tool_name == "request_approval":
            return self._send_approval_request(
                journal_id=int(args["journal_id"]),
                approver_email=str(args.get("approver_email", self.notification_settings.approver_email)),
                summary=str(args.get("summary", "")),
                urgency=str(args.get("urgency", "MEDIUM")),
            )

        else:
            raise ValueError(
                f"Unknown tool: '{tool_name}'. "
                f"Valid tools are: get_unbalanced_journals, get_journal_detail, "
                f"get_account_details, find_source_transaction, draft_corrective_journal, "
                f"generate_reconciliation_report, request_approval."
            )

    # ------------------------------------------------------------------
    # Notification helper
    # ------------------------------------------------------------------

    def _send_approval_request(
        self,
        journal_id: int,
        approver_email: str,
        summary: str,
        urgency: str,
    ) -> dict:
        """
        Send an HTML approval request email via SMTP.
        Falls back to a logged notification if SMTP is not configured.
        """
        cfg = self.notification_settings
        urgency_prefix = {
            "LOW": "[GL Recon]",
            "MEDIUM": "[GL Recon]",
            "HIGH": "[ACTION REQUIRED - GL Recon]",
            "CRITICAL": "[CRITICAL - GL Recon]",
        }.get(urgency.upper(), "[GL Recon]")

        subject = f"{urgency_prefix} Corrective Journal Approval Required — JE {journal_id}"

        html_body = f"""\
<html><body>
<h2>Oracle GL Reconciliation Agent — Approval Request</h2>
<table border="1" cellpadding="6" style="border-collapse:collapse;">
  <tr><td><b>Journal Header ID</b></td><td>{journal_id}</td></tr>
  <tr><td><b>Urgency</b></td><td>{urgency}</td></tr>
  <tr><td><b>Generated</b></td><td>{datetime.now(timezone.utc).isoformat()} UTC</td></tr>
</table>
<h3>Summary</h3>
<pre style="background:#f4f4f4;padding:12px;">{summary}</pre>
<h3>Action Required</h3>
<p>Please review the attached FBDI file and approve or reject the corrective journal
in Oracle Fusion Cloud via: <b>Journals &gt; Import &gt; Manage Journal Import</b>.</p>
<hr/>
<p><small>Sent by oracle-gl-reconciliation-agent</small></p>
</body></html>
"""

        # Attempt real SMTP send
        if cfg.smtp_host and cfg.smtp_host != "smtp.yourcompany.com":
            try:
                msg = email.mime.multipart.MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = cfg.from_address or cfg.smtp_username or "gl-agent@noreply.com"
                msg["To"] = approver_email
                msg["X-Priority"] = "1" if urgency in ("HIGH", "CRITICAL") else "3"
                msg.attach(email.mime.text.MIMEText(html_body, "html"))

                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
                    if cfg.smtp_use_tls:
                        server.starttls()
                    if cfg.smtp_username and cfg.smtp_password:
                        server.login(cfg.smtp_username, cfg.smtp_password)
                    server.sendmail(msg["From"], [approver_email], msg.as_string())

                logger.info("Approval email sent to %s for JE %d", approver_email, journal_id)
                return {
                    "sent": True,
                    "to": approver_email,
                    "subject": subject,
                    "method": "smtp",
                }

            except Exception as exc:
                logger.warning("SMTP send failed: %s — logging notification instead.", exc)

        # Fallback: log-only notification
        logger.info(
            "APPROVAL NOTIFICATION (log-only): JE=%d  To=%s  Urgency=%s\n%s",
            journal_id,
            approver_email,
            urgency,
            summary,
        )
        return {
            "sent": False,
            "simulated": True,
            "to": approver_email,
            "subject": subject,
            "note": "SMTP not configured — notification logged only. "
                    "Set SMTP_HOST in .env to enable real email delivery.",
        }

    # ------------------------------------------------------------------
    # Result builder
    # ------------------------------------------------------------------

    def _build_result(
        self,
        ledger_id: int,
        period_name: str,
        agent_summary: str,
    ) -> ReconciliationResult:
        """Build the final ReconciliationResult from accumulated agent state."""
        all_pending = all(
            f.get("status") == "PENDING_APPROVAL" for f in self._findings
        )
        any_investigation = any(
            f.get("status") == "REQUIRES_INVESTIGATION" for f in self._findings
        )
        all_resolved = all(
            f.get("status") == "RESOLVED" for f in self._findings
        )

        if not self._findings:
            status = "RESOLVED"
        elif any_investigation:
            status = "REQUIRES_INVESTIGATION"
        elif all_resolved:
            status = "RESOLVED"
        else:
            status = "PENDING_APPROVAL"

        return ReconciliationResult(
            ledger_id=ledger_id,
            period_name=period_name,
            imbalances_found=len(self._findings),
            journals_analyzed=self._journals_analyzed,
            corrective_journals_drafted=len(self._fbdi_files),
            report_path=self._report_path,
            fbdi_files=list(self._fbdi_files),
            status=status,
            agent_summary=agent_summary,
            findings=list(self._findings),
        )

    def _reset_run_state(self) -> None:
        """Clear accumulated state before a new reconciliation run."""
        self._findings = []
        self._fbdi_files = []
        self._journals_analyzed = 0
        self._report_path = None
