"""
examples/run_reconciliation.py
-------------------------------
CLI entry point for the Oracle GL Reconciliation Agent.

Loads configuration from .env, constructs the FusionClient and agent,
runs the reconciliation for the specified ledger and period, and renders
rich terminal output showing agent progress, tool calls, and final results.

Usage:
    python examples/run_reconciliation.py
    python examples/run_reconciliation.py --ledger-id 1001 --period Jan-25
    python examples/run_reconciliation.py --provider openai --period Mar-25
    python examples/run_reconciliation.py --dry-run   # skips Oracle API calls

Environment:
    All configuration read from .env at project root.
    CLI flags override .env values where provided.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Add project root to sys.path so imports work without pip install
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
from rich.table import Table
from rich import box

from config.settings import load_settings
from src.agents.factory import AgentFactory
from src.oracle.fusion_client import FusionClient, OracleAuthError, OracleAPIError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.WARNING,  # Suppress noisy debug output by default
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Show info from our own modules
logging.getLogger("src").setLevel(logging.INFO)
logging.getLogger("config").setLevel(logging.INFO)

console = Console()


# ---------------------------------------------------------------------------
# Rich patch: intercept tool calls and print them as they happen
# ---------------------------------------------------------------------------

class VerboseFusionClient(FusionClient):
    """
    FusionClient subclass that prints each REST call to the terminal.
    Used in the CLI for visibility into what the agent is doing.
    """

    def _get(self, path: str, params=None):
        short_path = path.split("/")[-1][:60]
        console.print(
            f"  [dim]→ GET [cyan]{short_path}[/cyan]"
            + (f" ({len(params)} params)" if params else ""),
            highlight=False,
        )
        return super()._get(path, params)

    def _post(self, path: str, payload, content_type="application/json"):
        short_path = path.split("/")[-1][:60]
        console.print(
            f"  [dim]→ POST [cyan]{short_path}[/cyan]",
            highlight=False,
        )
        return super()._post(path, payload, content_type)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Oracle GL Reconciliation Agent — detect and correct GL imbalances",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python examples/run_reconciliation.py --ledger-id 1001 --period Jan-25
  python examples/run_reconciliation.py --provider claude --period Mar-25
  python examples/run_reconciliation.py --dry-run
        """,
    )
    parser.add_argument(
        "--ledger-id",
        type=int,
        default=None,
        help="GL_LEDGERS.LEDGER_ID (overrides ORACLE_LEDGER_ID in .env)",
    )
    parser.add_argument(
        "--period",
        type=str,
        default=None,
        help="Accounting period name e.g. Jan-25 (overrides ORACLE_PERIOD_NAME in .env)",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "openai"],
        default=None,
        help="LLM provider (overrides AGENT_PROVIDER in .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run with mocked Oracle API responses — no real Oracle connection needed. "
            "Useful for testing the agent logic without ERP Cloud access."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show DEBUG-level Oracle REST API calls",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Dry-run mock (no Oracle connection required)
# ---------------------------------------------------------------------------

class MockFusionClient(FusionClient):
    """
    Mock FusionClient that returns realistic Oracle GL data without any
    real HTTP calls.  Used for --dry-run mode and CI testing.
    """

    def __init__(self):
        # Bypass parent __init__ — no auth needed
        self.host = "https://mock.oracle.example.com"
        self.api_version = "11.13.18.05"
        self.oracle_settings = None
        self._token_cache = None

    def _get(self, path: str, params=None):
        return self._mock_response(path, params or {})

    def _post(self, path: str, payload, content_type="application/json"):
        return {"RequestId": "MOCK-REQ-001", "Status": "SUBMITTED"}

    def _mock_response(self, path: str, params: dict):
        p = path.lower()

        if "generalledgerjournals" in p and "child/lines" in p:
            return {
                "items": [
                    {
                        "JournalLineId": 1, "LineNumber": 1,
                        "CodeCombinationId": 10001,
                        "Segment1": "01", "Segment2": "6010", "Segment3": "000", "Segment4": "0000",
                        "EnteredDebit": 1425000.00, "EnteredCredit": 0,
                        "AccountedDebit": 1425000.00, "AccountedCredit": 0,
                        "Description": "AP Accrual - Accenture Federal INV-2025-01-88734",
                    },
                    {
                        "JournalLineId": 2, "LineNumber": 2,
                        "CodeCombinationId": 10002,
                        "Segment1": "01", "Segment2": "2100", "Segment3": "000", "Segment4": "0000",
                        "EnteredDebit": 0, "EnteredCredit": 1424875.50,
                        "AccountedDebit": 0, "AccountedCredit": 1424875.50,
                        "Description": "AP Control - Accenture Federal",
                    },
                ]
            }

        elif "generalledgerjournals" in p and any(c.isdigit() for c in p.split("/")[-1]):
            return {
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
                "Description": "AP Accrual batch Jan-25 Batch 3",
                "CreatedBy": "AP_INTEGRATION",
            }

        elif "generalledgerjournals" in p:
            finder = params.get("finder", "")
            return {
                "items": [
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
                        "Description": "AP Accrual batch Jan-25 Batch 3",
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
                        "TotalAcctCredit": 499998.75,
                        "Description": "AR Cash receipt Jan-25",
                        "CreatedBy": "AR_INTEGRATION",
                    },
                    {
                        "JournalHeaderId": 100434,
                        "JournalName": "GL_MANUAL_ADJ_JAN25_001",
                        "JeCategory": "Adjustment",
                        "JeSource": "Manual",
                        "Status": "U",
                        "PostingDate": "2025-01-28",
                        "AccountedCurrencyCode": "USD",
                        "TotalAcctDebit": 75000.00,
                        "TotalAcctCredit": 75000.00,  # balanced
                        "Description": "Manual adj - balanced",
                        "CreatedBy": "GL_ACCOUNTANT",
                    },
                ]
            }

        elif "ledgeraccountcombinations" in p:
            ccid = int(p.split("/")[-1]) if p.split("/")[-1].isdigit() else 10001
            accounts = {
                10001: {"CodeCombinationId": 10001, "Segment1": "01", "Segment2": "6010",
                        "Segment3": "000", "Segment4": "0000", "Enabled": "Y",
                        "Summary": "N", "AccountType": "E", "Description": "Professional Services Expense"},
                10002: {"CodeCombinationId": 10002, "Segment1": "01", "Segment2": "2100",
                        "Segment3": "000", "Segment4": "0000", "Enabled": "Y",
                        "Summary": "N", "AccountType": "L", "Description": "AP Trade Payables Control"},
            }
            return accounts.get(ccid, {"CodeCombinationId": ccid, "Enabled": "Y", "Summary": "N"})

        elif "ledgers/" in p:
            return {
                "LedgerId": 1001,
                "LedgerName": "US Primary Ledger",
                "CurrencyCode": "USD",
                "PeriodType": "Month",
                "ChartOfAccountsId": 101,
            }

        elif "accountingperiods" in p:
            return {"items": [{"PeriodName": "Jan-25", "PeriodStatus": "O"}]}

        elif "saw.dll" in p:
            # Mock OTBI XLA response
            return {
                "rows": [
                    {
                        "AeHeaderId": 88123,
                        "AeLineNum": 1,
                        "JournalHeaderId": 100432,
                        "ApplicationId": 222,
                        "EventId": 887234,
                        "EventClassCode": "INVOICES",
                        "EntityCode": "AP_INVOICES",
                        "SourceIdInt1": 88734,
                        "AccountedDebit": 1425000.00,
                        "AccountedCredit": 0,
                        "EnteredDebit": 1425000.00,
                        "EnteredCredit": 0,
                    }
                ]
            }

        return {}


# ---------------------------------------------------------------------------
# Rich print helpers
# ---------------------------------------------------------------------------

def print_banner(ledger_id: int, period: str, provider: str, dry_run: bool) -> None:
    provider_label = f"Claude" if provider == "claude" else "GPT-4o"
    dry_label = " [DRY RUN]" if dry_run else ""
    console.print(
        Panel.fit(
            f"[bold]Oracle GL Reconciliation Agent[/bold]{dry_label}  •  Provider: [cyan]{provider_label}[/cyan]\n"
            f"Ledger ID: [yellow]{ledger_id}[/yellow]  •  Period: [yellow]{period}[/yellow]",
            border_style="red",
            padding=(1, 4),
        )
    )
    console.print()


def print_result(result) -> None:
    console.print()
    console.rule("[bold red]RECONCILIATION COMPLETE[/bold red]")
    console.print()

    # KPI table
    table = Table(box=box.ROUNDED, show_header=False, border_style="dim")
    table.add_column("Metric", style="bold", width=36)
    table.add_column("Value", justify="right")

    table.add_row("Journals Analyzed", str(result.journals_analyzed))
    table.add_row("Imbalances Detected", f"[red]{result.imbalances_found}[/red]")
    table.add_row("Corrective Journals Drafted", str(result.corrective_journals_drafted))

    status_style = {
        "RESOLVED": "green",
        "PENDING_APPROVAL": "yellow",
        "REQUIRES_INVESTIGATION": "red",
    }.get(result.status, "white")
    table.add_row("Overall Status", f"[{status_style}]{result.status}[/{status_style}]")

    if result.report_path:
        table.add_row("HTML Report", result.report_path)
    for i, fbdi in enumerate(result.fbdi_files, 1):
        table.add_row(f"FBDI File #{i}", fbdi)

    console.print(table)
    console.print()

    if result.agent_summary:
        console.print(Rule("Agent Summary"))
        console.print(result.agent_summary)
        console.print()

    if result.findings:
        console.print(Rule("Findings"))
        findings_table = Table(box=box.SIMPLE, border_style="dim")
        findings_table.add_column("JE Header ID", style="cyan")
        findings_table.add_column("Journal Name")
        findings_table.add_column("Imbalance", justify="right", style="red")
        findings_table.add_column("Status")

        for f in result.findings:
            status_color = {
                "RESOLVED": "green",
                "PENDING_APPROVAL": "yellow",
                "REQUIRES_INVESTIGATION": "red",
            }.get(f.get("status", ""), "white")
            findings_table.add_row(
                str(f.get("journal_header_id", "")),
                str(f.get("journal_name", ""))[:50],
                f"{f.get('imbalance_amount', 0):.2f}",
                f"[{status_color}]{f.get('status', '')}[/{status_color}]",
            )
        console.print(findings_table)

    console.rule(style="dim")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger("src").setLevel(logging.DEBUG)
        logging.getLogger("config").setLevel(logging.DEBUG)

    # Load settings
    try:
        oracle_cfg, agent_cfg, notif_cfg = load_settings()
    except Exception as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        console.print("Run [bold]cp .env.example .env[/bold] and fill in your values.")
        sys.exit(1)

    # CLI overrides
    ledger_id = args.ledger_id or oracle_cfg.ledger_id
    period_name = args.period or oracle_cfg.period_name
    provider = args.provider or agent_cfg.provider

    print_banner(ledger_id, period_name, provider, args.dry_run)

    # Build FusionClient
    if args.dry_run:
        console.print("[yellow]DRY RUN[/yellow] — using mock Oracle API responses")
        console.print()
        fusion_client = MockFusionClient()
    else:
        console.print("[dim]Authenticating to Oracle Fusion Cloud...[/dim]", end=" ")
        try:
            fusion_client = FusionClient(
                host=oracle_cfg.host,
                client_id=oracle_cfg.client_id,
                client_secret=oracle_cfg.client_secret,
                token_url=oracle_cfg.token_url,
                username=oracle_cfg.username,
                password=oracle_cfg.password,
                api_version=oracle_cfg.api_version,
                connect_timeout=oracle_cfg.connect_timeout,
                read_timeout=oracle_cfg.read_timeout,
            )
            # Trigger auth validation
            fusion_client._auth_header()
            console.print("[green]✓ Connected[/green]")
        except OracleAuthError as exc:
            console.print(f"[red]Auth failed:[/red] {exc}")
            sys.exit(1)
        except Exception as exc:
            console.print(f"[red]Connection failed:[/red] {exc}")
            sys.exit(1)

    console.print()

    # Build agent
    try:
        agent = AgentFactory.create(
            provider=provider,
            fusion_client=fusion_client,
            oracle_settings=oracle_cfg,
            agent_settings=agent_cfg,
            notification_settings=notif_cfg,
        )
    except ValueError as exc:
        console.print(f"[red]Agent configuration error:[/red] {exc}")
        sys.exit(1)

    console.print(
        f"[dim]Starting reconciliation:[/dim] "
        f"[bold]ledger {ledger_id}[/bold] / [bold]{period_name}[/bold] "
        f"via [cyan]{provider.upper()}[/cyan]"
    )
    console.print()

    # Run reconciliation
    start_time = time.time()
    try:
        result = agent.run(ledger_id=ledger_id, period_name=period_name)
    except OracleAPIError as exc:
        console.print(f"[red]Oracle API error during reconciliation:[/red] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Reconciliation interrupted by user.[/yellow]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"[red]Unexpected error:[/red] {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - start_time
    console.print(f"\n[dim]Completed in {elapsed:.1f}s[/dim]")

    print_result(result)

    # Exit code reflects status
    exit_codes = {
        "RESOLVED": 0,
        "PENDING_APPROVAL": 0,      # Not an error — normal operating state
        "REQUIRES_INVESTIGATION": 1,
    }
    sys.exit(exit_codes.get(result.status, 0))


if __name__ == "__main__":
    main()
