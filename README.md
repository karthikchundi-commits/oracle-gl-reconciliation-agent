# oracle-gl-reconciliation-agent

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![License MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Oracle Fusion Cloud](https://img.shields.io/badge/Oracle-Fusion%20Cloud-red.svg)
![Claude + GPT-4o](https://img.shields.io/badge/LLM-Claude%20%7C%20GPT--4o-purple.svg)

An agentic AI system that autonomously detects Oracle Fusion Cloud GL journal imbalances, traces them to subledger sources via XLA accounting event tables, drafts corrective FBDI journal files, and routes them for human approval — reducing period-close reconciliation time for enterprise finance teams by eliminating the manual triage cycle between GL accountants and subledger application owners. The agent operates through a structured tool-calling loop, invoking Oracle ERP Cloud REST APIs and OTBI reporting endpoints to gather evidence, reason over the data, and produce actionable corrective journals in the Oracle-standard GL_INTERFACE FBDI format.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   oracle-gl-reconciliation-agent                │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │  Claude API  │    │  OpenAI API  │    │  AgentFactory    │  │
│  │  (tool_use)  │    │ (func_call)  │    │  (provider sel.) │  │
│  └──────┬───────┘    └──────┬───────┘    └────────┬─────────┘  │
│         └────────────────────┴─────────────────────┘           │
│                              │                                  │
│                   ┌──────────▼──────────┐                      │
│                   │  BaseGLRecon Agent  │                      │
│                   │  (agentic loop)     │                      │
│                   └──────────┬──────────┘                      │
│                              │  tool dispatch                  │
│          ┌───────────────────┼───────────────────┐            │
│          │                   │                   │            │
│  ┌───────▼──────┐  ┌─────────▼──────┐  ┌────────▼────────┐  │
│  │  gl_queries  │  │ fbdi_generator │  │  recon_report   │  │
│  └───────┬──────┘  └─────────┬──────┘  └────────┬────────┘  │
│          │                   │                   │            │
│  ┌───────▼──────────────────────────────────────▼────────┐  │
│  │               FusionClient (REST API layer)            │  │
│  │  OAuth2 token mgmt · fscmRestApi · erpintegrations    │  │
│  └───────────────────────────┬────────────────────────────┘  │
└──────────────────────────────┼──────────────────────────────-─┘
                               │ HTTPS
         ┌─────────────────────▼──────────────────────┐
         │          Oracle Fusion Cloud ERP            │
         │                                            │
         │  GL_LEDGERS · GL_CODE_COMBINATIONS         │
         │  GL_JE_HEADERS · GL_JE_LINES               │
         │  XLA_AE_HEADERS · XLA_AE_LINES             │
         │  XLA_EVENTS · XLA_TRANSACTION_ENTITIES     │
         └────────────────────────────────────────────┘
```

---

## How It Works

```
Oracle ERP Cloud REST API
         │
         │  1. GET /fscmRestApi/resources/.../generalLedgerJournals
         │     Filter: ledger_id, period_name, status
         ▼
   Agent Tools Layer
         │
         │  2. Detect imbalanced journals (|DR - CR| > 0.01)
         │  3. Fetch journal lines + account coding
         │  4. Trace XLA subledger events to source transactions
         ▼
   LLM Reasoning (Claude tool_use / GPT-4o function_calling)
         │
         │  5. Classify root cause: coding error / timing / FX
         │  6. Draft corrective journal entry lines
         │  7. Build GL_INTERFACE FBDI file
         ▼
   Corrective Action
         │
         │  8. Submit FBDI via /erpintegrations endpoint
         │  9. Email approver with summary + attached FBDI
         │ 10. Generate HTML reconciliation report
         ▼
   ReconciliationResult (dataclass returned to caller)
```

---

## Supported Models

| Provider  | Model            | Notes                                      |
|-----------|------------------|--------------------------------------------|
| Anthropic | claude-opus-4-5  | Default; best multi-step tool orchestration |
| Anthropic | claude-sonnet-4-5 | Faster, cost-efficient for high volume     |
| OpenAI    | gpt-4o           | Default OpenAI; parallel tool calls        |
| OpenAI    | gpt-4o-mini      | Lower cost; adequate for simple periods    |

Set `AGENT_PROVIDER=claude` or `AGENT_PROVIDER=openai` in `.env`.

---

## Installation

```bash
git clone https://github.com/yourorg/oracle-gl-reconciliation-agent.git
cd oracle-gl-reconciliation-agent

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Configuration

```bash
cp .env.example .env
# Edit .env with your Oracle Cloud instance URL, OAuth credentials,
# ledger ID, and LLM API keys
```

Key `.env` fields:

| Variable               | Description                                      |
|------------------------|--------------------------------------------------|
| `ORACLE_HOST`          | e.g. `https://your-instance.oraclecloud.com`     |
| `ORACLE_CLIENT_ID`     | OAuth 2.0 client ID from IDCS / OCI IAM          |
| `ORACLE_CLIENT_SECRET` | OAuth 2.0 client secret                          |
| `ORACLE_LEDGER_ID`     | Primary ledger ID (from GL_LEDGERS)              |
| `ORACLE_PERIOD_NAME`   | e.g. `Jan-25`                                    |
| `AGENT_PROVIDER`       | `claude` or `openai`                             |
| `ANTHROPIC_API_KEY`    | From console.anthropic.com                       |
| `OPENAI_API_KEY`       | From platform.openai.com                         |

---

## Quick Start

```bash
python examples/run_reconciliation.py --ledger-id 1001 --period Jan-25
```

### Sample Terminal Output

```
╔══════════════════════════════════════════════════════════════════╗
║       Oracle GL Reconciliation Agent  •  Provider: Claude       ║
║       Ledger: US Primary Ledger (1001)  •  Period: Jan-25       ║
╚══════════════════════════════════════════════════════════════════╝

[12:03:01] Authenticating to Oracle Fusion Cloud...  ✓ Token acquired (expires 3600s)
[12:03:02] Agent starting reconciliation run for Jan-25

── TOOL CALL: get_unbalanced_journals ──────────────────────────────
  ledger_id=1001  period_name=Jan-25
  → Found 3 unbalanced journals

── TOOL CALL: get_journal_detail ───────────────────────────────────
  journal_header_id=100432
  → JE Name: AP_ACCRUAL_JAN25_BATCH_003
    Category: Accrual  Source: Payables
    Total DR: 1,425,000.00  Total CR: 1,424,875.50
    Imbalance: 124.50 USD

── TOOL CALL: find_source_transaction ──────────────────────────────
  journal_header_id=100432
  → XLA Event: INVOICE_VALIDATED  Event ID: 887234
    Source Dist: AP_INVOICE_DISTRIBUTIONS_ALL
    Invoice Num: INV-2025-01-88734  Vendor: Accenture Federal

── AGENT REASONING ─────────────────────────────────────────────────
  Root cause: Rounding error on multi-currency invoice conversion.
  AP_INVOICE_DISTRIBUTIONS_ALL has entered_amount=1424875.50 but
  accounted_amount rounded to 1425000.00 using stale FX rate.
  Corrective action: Dr 6010-AP-ACCRUAL 124.50 / Cr 2100-AP-CONTROL 124.50

── TOOL CALL: draft_corrective_journal ─────────────────────────────
  original_journal_id=100432
  correction_lines=[
    {account: "01-6010-000-0000", dr: 124.50, cr: 0, description: "FX rounding correction INV-2025-01-88734"},
    {account: "01-2100-000-0000", dr: 0, cr: 124.50, description: "FX rounding correction INV-2025-01-88734"}
  ]
  → FBDI file written: output/CORR_100432_Jan25.csv  (2 lines, balanced)

── TOOL CALL: request_approval ─────────────────────────────────────
  journal_id=100432  urgency=HIGH
  → Approval email sent to controller@company.com

── TOOL CALL: generate_reconciliation_report ───────────────────────
  → Report written: output/recon_report_Jan25.html

════════════════════════ RECONCILIATION COMPLETE ════════════════════
  Journals analyzed:          12
  Imbalances detected:         3
  Corrective journals drafted: 3
  Pending approval:            3
  Report:                      output/recon_report_Jan25.html
  Status:                      PENDING_APPROVAL
═════════════════════════════════════════════════════════════════════
```

---

## File Structure

```
oracle-gl-reconciliation-agent/
├── README.md
├── requirements.txt
├── .env.example
├── config/
│   ├── __init__.py
│   └── settings.py              # Pydantic BaseSettings
├── src/
│   ├── __init__.py
│   ├── oracle/
│   │   ├── __init__.py
│   │   ├── fusion_client.py     # REST API client + OAuth2
│   │   ├── gl_queries.py        # GL data retrieval + XLA tracing
│   │   └── fbdi_generator.py    # GL_INTERFACE FBDI file builder
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── tools.py             # Claude + OpenAI tool definitions
│   │   ├── base_agent.py        # Abstract agent + tool dispatch
│   │   ├── claude_agent.py      # Anthropic tool_use loop
│   │   ├── openai_agent.py      # OpenAI function_calling loop
│   │   └── factory.py           # Provider factory
│   └── reports/
│       ├── __init__.py
│       └── reconciliation_report.py  # Jinja2 HTML report
├── templates/
│   └── recon_report.html.j2     # Jinja2 template
├── examples/
│   └── run_reconciliation.py    # CLI entry point
├── tests/
│   ├── test_gl_queries.py
│   ├── test_fbdi_generator.py
│   └── test_agents.py
└── output/                      # Generated FBDI + reports (gitignored)
```

---

## Oracle ERP Cloud Setup

### Required Privileges

The integration user (OAuth client) needs the following Oracle Fusion Cloud data roles assigned in Security Console:

| Role                                   | Purpose                                    |
|----------------------------------------|--------------------------------------------|
| `General Accounting Manager`           | Read GL journals, submit journal import    |
| `Financial Application Administrator` | Access OTBI reports, BI Publisher REST     |
| `Payables Manager`                     | Read AP subledger XLA entries              |
| `Receivables Manager`                  | Read AR subledger XLA entries              |

### REST API Access

Enable REST API access in Setup > Manage Enterprise Settings:
- **REST API Framework**: Enabled
- **OTBI REST API**: Enabled  
- **ERP Integration Service**: Enabled (for FBDI upload)

### OAuth 2.0 Configuration

Register a Confidential Application in IDCS (or OCI IAM Domain):
1. Application type: **Confidential**
2. Grant type: **Client Credentials**
3. Scopes: `urn:opc:resource:consumer::all`
4. Copy Client ID + Secret to `.env`

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Add tests in `tests/`
4. Ensure `pytest` passes
5. Submit a pull request

---

## License

MIT License — see [LICENSE](LICENSE) for details.
