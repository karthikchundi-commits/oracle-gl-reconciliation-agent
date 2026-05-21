# Building an Agentic AI for Oracle Fusion Cloud GL Reconciliation — with Claude and GPT-4o

---

## The Problem with GL Reconciliation at Scale

Period-close in Oracle Fusion Cloud is one of those things that looks manageable on paper and falls apart in practice. The platform is well-built — the APIs are stable, the data model is clean, the XLA subledger accounting layer is well-documented. But the reconciliation process itself — finding unbalanced journals, tracing them back to their subledger source, drafting corrective entries, routing them for approval — is still almost entirely manual at most organizations I've worked with.

You query `GL_JE_HEADERS` to find journals where `TotalAcctDebit != TotalAcctCredit`. Then you navigate through `XLA_AE_HEADERS`, `XLA_EVENTS`, and `XLA_TRANSACTION_ENTITIES` to figure out whether the imbalance came from AP, AR, Fixed Assets, or something else. Then you write a corrective entry in the GL_INTERFACE FBDI format, validate the column layout against Oracle's specification, upload it, wait for Journal Import to run, confirm it posted, and route it for controller approval. Then you do all of that again for the next journal.

At single-entity scale with one ledger and one currency, this is tedious. At multi-ledger, multi-currency, post-merger scale — the kind of environment I ran into building the Deloitte SuperLedger integration architecture, where we were consolidating financial data from eight different ERP sources — this process consumes days and produces errors. Finance teams are making judgment calls at midnight before quarter-close, manually applying the same three or four root-cause patterns they've seen a hundred times before.

The "automation" that typically exists doesn't help much. RPA scripts from UiPath or Blue Prism will automate the screen navigation, but they have no reasoning capability and they break on every UI update. Scheduled batch jobs can flag imbalances, but they just generate a report that a human then has to interpret. What's missing is something that can *reason* about why a journal is unbalanced and *decide* what to do about it — and that's exactly where agentic AI fits.

---

## Why Agentic AI Fits ERP Better Than Traditional Automation

An agent, in the practical sense I'm using here, is an LLM that has access to tools — function calls it can invoke against external systems — and runs in a loop: observe, reason, act, observe again. The LLM decides which tools to call, in what order, and when it's done. It's not following a fixed script.

For most software domains this raises an immediate concern: the action space is too wide, the business rules are too ambiguous, and the cost of a mistake is too hard to measure. ERP is different, and Oracle ERP specifically is close to ideal for this use case. Here's why.

The business rules for GL reconciliation are well-codified and finite. A journal imbalance has five or six common root causes: FX rounding on multi-currency invoices, subledger-to-GL sync failures from a crashed `Transfer Journal Entries to GL` run, manual coding errors against invalid account combinations, accrual reversals posted to the wrong period, intercompany autobalancing failures. An agent can learn these rules from a system prompt and apply them consistently.

The API surface is stable. Oracle's `fscmRestApi` endpoints for GL journals, combined with OTBI for XLA data and `/erpintegrations` for FBDI submission, have been stable across release cycles in a way most SaaS APIs are not. Unlike UI automation that breaks on every screen update, REST API changes come with versioning and deprecation cycles.

The action space is bounded. The agent queries data, traces through XLA, drafts corrective FBDI entries, generates a report, and routes for approval — nothing else. It cannot post directly to the ledger; that requires human approval. Every action is reversible: FBDI imports can be rejected before posting, posted journals can be reversed. The cost of an error is measurable.

This is why reasoning capability matters so much here. A timing difference — where an AP invoice has posted but the corresponding payment hasn't cleared yet — requires no corrective action, just a note in the reconciliation report explaining why the imbalance is expected to resolve itself. A miscoded account requires a corrective journal. A subledger sync failure requires an Oracle Support ticket, not a manual fix. An RPA script cannot make that distinction. An LLM with access to the right data can.

Both Claude (Anthropic's `tool_use`) and GPT-4o (OpenAI's `function_calling`) support this pattern, and their implementations are functionally equivalent for this use case. The agent logic is identical — only the SDK calls differ. I built the tool schemas once and export them in both wire formats.

---

## Architecture

The data flow is straightforward:

```
Oracle Fusion Cloud REST API
         │
         ▼
   Agent Tool Layer
   ┌─────────────────────────────────┐
   │ get_unbalanced_journals          │
   │ get_journal_detail               │
   │ get_account_details              │
   │ find_source_transaction (XLA)    │
   │ draft_corrective_journal (FBDI)  │
   │ generate_reconciliation_report   │
   │ request_approval                 │
   └─────────────────────────────────┘
         │
         ▼
   LLM Reasoning Layer
   (Claude tool_use / GPT-4o function_calling)
         │
         ▼
   Corrective Action
   (FBDI file → ERP Integrations API)
         │
         ▼
   Human Approval
   (GL Controller → Oracle Journal Import)
```

The seven tools form the complete action vocabulary for the agent — nothing more. `get_unbalanced_journals` queries Oracle for journals where `|TotalAcctDebit - TotalAcctCredit| > 0.01`. `get_journal_detail` pulls the full header and lines for a specific `JE_HEADER_ID`, enriching each line with account segment data from `GL_CODE_COMBINATIONS`. `get_account_details` validates a `CODE_COMBINATION_ID` — enabled status, summary flag, date range — before it appears in a corrective entry. `find_source_transaction` traces through `XLA_AE_HEADERS`, `XLA_EVENTS`, and `XLA_TRANSACTION_ENTITIES` to identify the originating subledger application and transaction. `draft_corrective_journal` generates a GL_INTERFACE FBDI CSV, enforcing that `ENTERED_DR` and `ENTERED_CR` are mutually exclusive per line and that the journal balances to the cent. `generate_reconciliation_report` produces an HTML summary with per-journal findings and KPIs. `request_approval` sends an HTML email to the GL controller with the FBDI file path.

The agentic loop: initial user message specifies `ledger_id` and `period_name`. Agent calls `get_unbalanced_journals`, examines each hit with `get_journal_detail` and `find_source_transaction`, reasons about root cause, calls `draft_corrective_journal` for actionable imbalances, routes each via `request_approval`, then closes with `generate_reconciliation_report` before reaching `stop_reason == 'end_turn'` (Claude) or `finish_reason == 'stop'` (GPT-4o).

One implementation detail worth noting: the FBDI validation logging uses a durable write pattern — conceptually equivalent to `PRAGMA AUTONOMOUS_TRANSACTION` in Oracle PL/SQL — so validation failures persist even when the outer operation rolls back.

The key Oracle APIs: `/fscmRestApi/resources/{version}/generalLedgerJournals` for GL data, OTBI Analytics Answers (`/analytics/saw.dll`) for XLA subledger joins not exposed through standard REST resources, and `/fscmRestApi/resources/{version}/erpintegrations` for FBDI submission.

---

## The Model-Agnostic Design

The architecture centers on an abstract base class, `BaseGLReconciliationAgent`, with exactly one abstract method that concrete implementations override:

```python
class BaseGLReconciliationAgent(ABC):

    @abstractmethod
    def _call_llm(self, messages: list[dict], tools: list[dict]) -> dict:
        """
        Call the LLM provider with the current conversation and tools.
        Implemented by ClaudeGLAgent and OpenAIGLAgent.
        """
        ...
```

All tool dispatch, all Oracle API interaction, all result accumulation — that all lives in the base class. The subclass only handles the provider-specific message format and SDK call.

The tool definitions are written once in a canonical format and exported in both wire formats from `tools.py`:

```python
# Public exports: use these directly in agent implementations
CLAUDE_TOOLS: list[dict] = [_to_claude_tool(t) for t in _ALL_TOOLS]
OPENAI_TOOLS: list[dict] = [_to_openai_tool(t) for t in _ALL_TOOLS]
```

The difference between them is purely syntactic. Anthropic wraps the JSON Schema under `input_schema`; OpenAI wraps it under `function.parameters` with a `type: "function"` envelope and a `strict: True` flag for structured output validation. The underlying parameter schemas are identical — the same `ledger_id`, `period_name`, `correction_lines`, and `urgency` fields with the same types and descriptions.

Provider selection is handled through a factory:

```python
agent = AgentFactory.create(provider='claude', fusion_client=client,
                            oracle_settings=oracle_cfg,
                            agent_settings=agent_cfg,
                            notification_settings=notif_cfg)
# or:
agent = AgentFactory.create(provider='openai', ...)
```

This matters for enterprise adoption in a way that's easy to underestimate. Half the firms in the Oracle ERP space have standardized on Azure OpenAI through existing Microsoft agreements. The other half are moving to Claude Enterprise for the extended context window and the Constitutional AI compliance story. Building the agent against a provider-specific API would cut the addressable user base in half. The factory pattern means the same codebase works for both, and switching is a single configuration value.

---

## What the Agent Actually Does — A Walkthrough

Let's walk through a realistic Q1 period-close scenario on a multi-ledger Oracle Fusion environment. The agent is pointed at the US Primary Ledger, `ledger_id=1001`, for `period_name=Jan-25`.

The agent calls `get_unbalanced_journals`. The tool queries `fscmRestApi` for all journals in the period and filters to those where `|TotalAcctDebit - TotalAcctCredit| > 0.01`. It comes back with three journals:

`AP_ACCRUAL_JAN25_BATCH_003` (JE_HEADER_ID 100432) — imbalance of $124.50, source: Payables, category: Accrual, status: Unposted.

`AR_RECEIPT_JAN25_0001` (JE_HEADER_ID 100433) — imbalance of $1.25, source: Receivables, category: Receipts, status: Unposted.

`GL_MANUAL_ADJ_JAN25_001` (JE_HEADER_ID 100434) — imbalance of $18,750.00, source: Manual, category: Adjustment, status: Unposted.

The agent works through them one at a time. For JE 100432, `get_journal_detail` shows a debit to account 01-6010-000-0000 (Professional Services Expense) of $1,425,000.00 and a credit to 01-2100-000-0000 (AP Trade Payables Control) of $1,424,875.50. `find_source_transaction` returns: `application_id=222` (Payables), `event_class_code=INVOICES`, `source_id=88734`. The agent reasons: *"FX rounding imbalance — the accounted debit reflects the invoice at the transaction rate; the control account credit posted at a slightly different rounding. $124.50 difference is consistent with FX rounding on a large invoice. Draft a rounding adjustment."* It calls `draft_corrective_journal` with two balanced lines. The FBDI output:

```
STATUS,LEDGER_ID,ACCOUNTING_DATE,CURRENCY_CODE,ACTUAL_FLAG,USER_JE_CATEGORY_NAME,
USER_JE_SOURCE_NAME,SEGMENT1,SEGMENT2,SEGMENT3,SEGMENT4,ENTERED_DR,ENTERED_CR,
REFERENCE1,REFERENCE4,REFERENCE5
NEW,1001,2025-01-31,USD,A,Adjustment,Manual,01,2100,000,0000,124.50,,
RECON_CORR_100432_Jan25,Correction for JE 100432 - Jan-25,FX rounding adj - AP invoice 88734
NEW,1001,2025-01-31,USD,A,Adjustment,Manual,01,6800,000,0000,,124.50,
RECON_CORR_100432_Jan25,Correction for JE 100432 - Jan-25,FX rounding offset
```

For JE 100433, `find_source_transaction` returns `event_class_code=RECEIPTS` — an AR cash receipt posted mid-month. The $1.25 imbalance matches FX rounding on a foreign-currency receipt, but the AR subledger hasn't closed yet. The agent flags it: *"Timing difference — imbalance expected to resolve when AR period closure completes. No corrective action now."* Status: `REQUIRES_INVESTIGATION`.

For JE 100434, the manual adjustment, `find_source_transaction` returns no XLA entries — pure manual GL, no subledger source. `get_journal_detail` shows a debit to 02-8500-100-0000 (entity-02 cost center) on a ledger-01 journal. The agent recognizes the mismatched entity segment — entity-01 journal debiting an entity-02 cost center. It drafts a reversal of the incorrect line and a re-entry to the correct entity-01 cost center. Status: `PENDING_APPROVAL`, urgency: `HIGH`.

The agent then calls `generate_reconciliation_report` — HTML report, 3 journals analyzed, 2 corrective journals drafted, 1 flagged for investigation — and `request_approval` for the two actionable journals, emailing the controller the FBDI file paths and a plain-text imbalance summary.

---

## Testing Without a Live Oracle Instance

You don't need an Oracle Cloud subscription to run this. The `MockFusionClient` in `examples/run_reconciliation.py` returns realistic multi-journal GL data covering the exact scenario above — two AP/AR imbalances and one manual coding error — without making any real HTTP calls.

```bash
python examples/run_reconciliation.py --dry-run --provider claude
```

Swap `--provider openai` to test the GPT-4o path against the same mock data. The `--dry-run` flag swaps in `MockFusionClient` automatically; no `.env` configuration is required beyond a valid `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

The test suite has 62 unit tests covering imbalance detection logic, FBDI balance validation (including the mutual-exclusivity constraint on `ENTERED_DR`/`ENTERED_CR`), XLA tracing fallback behavior when OTBI returns no rows, and tool schema correctness for both provider formats:

```bash
pytest tests/ -v
```

If you want to run against a real Oracle Fusion instance, Oracle Cloud Free Trial gives you 30 days of access to a full Oracle Fusion Apps test environment — enough time to validate the agent against live `fscmRestApi` endpoints and real GL data. Configure your `.env` with the `ORACLE_HOST`, `CLIENT_ID`, `CLIENT_SECRET`, `TOKEN_URL`, and `ORACLE_LEDGER_ID` values from your environment, and point the agent at a period where you have existing journal data.

---

## What's Next

This is the first in a series of Oracle ERP Cloud agentic AI tools I'm building in public.

Next: an **AP Invoice Agent** (OCR → PO matching → FBDI submission to AP subledger), an **OIC Integration Health Agent** (failure classification + self-healing, extending the ML architecture from Deloitte SuperLedger), and an **ERP Spin-off Data Boundary Agent** built on the [erp-spinoff-data-boundary-toolkit](https://github.com/karthikchundi-commits/erp-spinoff-data-boundary-toolkit) for M&A carve-out data partitioning.

The broader thesis: Oracle ERP's REST API surface is one of the best production environments for agentic AI that currently exists. The action space is bounded, the business rules are codified, the APIs are stable, and the cost of an agent error is both measurable and reversible. Those properties are rare in software, and they make Oracle ERP a genuinely compelling production target rather than a demo environment.

The project is open source and ready to run today. Try it, open issues, and contribute.

**GitHub: [https://github.com/karthikchundi-commits/oracle-gl-reconciliation-agent](https://github.com/karthikchundi-commits/oracle-gl-reconciliation-agent)**

---

*Tags: Oracle, OracleERP, AgenticAI, ArtificialIntelligence, EnterpriseAI, Python, OpenSource*
