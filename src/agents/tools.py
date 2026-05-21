"""
src/agents/tools.py
-------------------
Agent tool / function definitions in both Anthropic (tool_use) and
OpenAI (function_calling) wire formats.

Each tool maps 1-to-1 to a function in gl_queries or fbdi_generator.
The tool name is the dispatch key used in BaseGLReconciliationAgent._execute_tool().

Anthropic format reference:
  https://docs.anthropic.com/en/docs/build-with-claude/tool-use

OpenAI format reference:
  https://platform.openai.com/docs/guides/function-calling
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Shared parameter schemas (JSON Schema draft-07 subset)
# Used verbatim in both Anthropic and OpenAI tool definitions.
# ---------------------------------------------------------------------------

_LEDGER_ID = {
    "type": "integer",
    "description": (
        "GL_LEDGERS.LEDGER_ID — the numeric primary key of the Oracle Fusion "
        "Cloud GL ledger to reconcile."
    ),
}

_PERIOD_NAME = {
    "type": "string",
    "description": (
        "GL_PERIODS.PERIOD_NAME — accounting period in Oracle format, "
        "e.g. 'Jan-25'. Must match exactly as stored in the ledger calendar."
    ),
}

_JOURNAL_HEADER_ID = {
    "type": "integer",
    "description": (
        "GL_JE_HEADERS.JE_HEADER_ID — the numeric primary key of the GL journal "
        "header to inspect."
    ),
}

_CODE_COMBINATION_ID = {
    "type": "integer",
    "description": (
        "GL_CODE_COMBINATIONS.CODE_COMBINATION_ID — the CCID (numeric PK) "
        "uniquely identifying a chart-of-accounts segment combination."
    ),
}

_CORRECTION_LINE_SCHEMA = {
    "type": "object",
    "description": "A single GL_INTERFACE line in the corrective journal entry.",
    "properties": {
        "account": {
            "type": "string",
            "description": (
                "Hyphen-delimited account segment string matching the ledger's "
                "chart of accounts, e.g. '01-6010-000-0000'. Segments are "
                "assigned to GL_INTERFACE.SEGMENT1..SEGMENT10 in order."
            ),
        },
        "dr": {
            "type": "number",
            "description": "Entered debit amount. Set to 0 for credit lines.",
        },
        "cr": {
            "type": "number",
            "description": "Entered credit amount. Set to 0 for debit lines.",
        },
        "description": {
            "type": "string",
            "description": (
                "Line-level description (max 240 chars). Maps to "
                "GL_JE_LINES.DESCRIPTION and GL_INTERFACE.REFERENCE5."
            ),
        },
    },
    "required": ["account", "dr", "cr"],
}


# ---------------------------------------------------------------------------
# 1. get_unbalanced_journals
# ---------------------------------------------------------------------------

_GET_UNBALANCED_JOURNALS = {
    "name": "get_unbalanced_journals",
    "description": (
        "Query Oracle Fusion Cloud GL to find all journal entries in the specified "
        "ledger and accounting period where the absolute difference between total "
        "accounted debits and total accounted credits exceeds $0.01. "
        "Returns a list of imbalanced journal headers with journal_header_id, "
        "journal_name, je_source, je_category, status, currency, total_dr, "
        "total_cr, and imbalance_amount. "
        "Call this first to identify which journals require investigation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ledger_id": _LEDGER_ID,
            "period_name": _PERIOD_NAME,
        },
        "required": ["ledger_id", "period_name"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# 2. get_journal_detail
# ---------------------------------------------------------------------------

_GET_JOURNAL_DETAIL = {
    "name": "get_journal_detail",
    "description": (
        "Retrieve complete details for a specific GL journal entry, including all "
        "journal lines with their account segment values, entered amounts, accounted "
        "amounts, line descriptions, and account validity status. "
        "Also returns the header-level total DR/CR and imbalance amount. "
        "Use this after identifying an imbalanced journal from get_unbalanced_journals "
        "to understand which specific lines are causing the imbalance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "journal_header_id": _JOURNAL_HEADER_ID,
        },
        "required": ["journal_header_id"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# 3. get_account_details
# ---------------------------------------------------------------------------

_GET_ACCOUNT_DETAILS = {
    "name": "get_account_details",
    "description": (
        "Look up the GL account combination details for a given CODE_COMBINATION_ID "
        "(CCID) from GL_CODE_COMBINATIONS. Returns all segment values, account type "
        "(Asset, Liability, Expense, Revenue, Equity), enabled flag, and date range. "
        "Use this to verify that an account is valid before including it in a "
        "corrective journal, or to understand the natural balance direction of an "
        "account when diagnosing whether a debit/credit is correct."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code_combination_id": _CODE_COMBINATION_ID,
        },
        "required": ["code_combination_id"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# 4. find_source_transaction
# ---------------------------------------------------------------------------

_FIND_SOURCE_TRANSACTION = {
    "name": "find_source_transaction",
    "description": (
        "Trace a GL journal back to its originating subledger transaction via the "
        "XLA (SLA — Subledger Accounting) layer. Queries XLA_AE_HEADERS, XLA_AE_LINES, "
        "XLA_EVENTS, and XLA_TRANSACTION_ENTITIES to identify the application_id "
        "(e.g. 222=Payables, 222=Receivables), event_class_code "
        "(e.g. INVOICES, PAYMENTS, RECEIPTS), event_id, and the source transaction "
        "primary key (SOURCE_ID_INT_1). "
        "Returns the source transaction record from the subledger REST API if available. "
        "Use this to determine the root cause of an imbalance — whether it originated "
        "in AP, AR, Fixed Assets, Projects, or is a manual GL entry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "journal_header_id": _JOURNAL_HEADER_ID,
        },
        "required": ["journal_header_id"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# 5. draft_corrective_journal
# ---------------------------------------------------------------------------

_DRAFT_CORRECTIVE_JOURNAL = {
    "name": "draft_corrective_journal",
    "description": (
        "Generate a corrective GL journal entry in Oracle FBDI (File-Based Data "
        "Import) format using the GL_INTERFACE table layout. The FBDI file is written "
        "to the output directory and can be submitted to Oracle via the ERP Integrations "
        "REST API. The corrective journal must balance (total DR == total CR). "
        "Each correction line specifies an account segment string, a debit or credit "
        "amount (mutually exclusive per line), and a description. "
        "Returns the path to the generated FBDI CSV file and a validation summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "original_journal_id": {
                "type": "integer",
                "description": (
                    "GL_JE_HEADERS.JE_HEADER_ID of the imbalanced journal being corrected. "
                    "Used to cross-reference the corrective entry back to the original."
                ),
            },
            "correction_lines": {
                "type": "array",
                "description": (
                    "List of journal lines for the corrective entry. "
                    "Must balance: sum(dr) == sum(cr). "
                    "Minimum two lines required."
                ),
                "items": _CORRECTION_LINE_SCHEMA,
                "minItems": 2,
            },
            "reason": {
                "type": "string",
                "description": (
                    "Human-readable explanation of why this corrective journal is needed "
                    "and what root cause it addresses. Included in the journal description "
                    "and reconciliation report."
                ),
            },
        },
        "required": ["original_journal_id", "correction_lines", "reason"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# 6. generate_reconciliation_report
# ---------------------------------------------------------------------------

_GENERATE_RECONCILIATION_REPORT = {
    "name": "generate_reconciliation_report",
    "description": (
        "Generate an HTML reconciliation report summarizing the agent's findings "
        "for the accounting period. Includes: summary counts (journals analyzed, "
        "imbalances found, corrective journals drafted), a detail table per imbalanced "
        "journal with root cause and corrective action, and an overall status "
        "(RESOLVED, PENDING_APPROVAL, or REQUIRES_INVESTIGATION). "
        "Returns the file path to the generated HTML report."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "description": (
                    "List of finding dicts, one per imbalanced journal. Each should include "
                    "journal_header_id, journal_name, imbalance_amount, root_cause, "
                    "corrective_action, fbdi_file (if drafted), and status."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "journal_header_id": {"type": "integer"},
                        "journal_name": {"type": "string"},
                        "imbalance_amount": {"type": "number"},
                        "root_cause": {"type": "string"},
                        "corrective_action": {"type": "string"},
                        "fbdi_file": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["RESOLVED", "PENDING_APPROVAL", "REQUIRES_INVESTIGATION"],
                        },
                    },
                    "required": ["journal_header_id", "journal_name", "imbalance_amount", "status"],
                },
            },
            "period_name": {
                "type": "string",
                "description": "Accounting period name for the report header, e.g. 'Jan-25'.",
            },
            "ledger_name": {
                "type": "string",
                "description": "Human-readable ledger name from GL_LEDGERS.NAME, e.g. 'US Primary Ledger'.",
            },
        },
        "required": ["findings", "period_name", "ledger_name"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# 7. request_approval
# ---------------------------------------------------------------------------

_REQUEST_APPROVAL = {
    "name": "request_approval",
    "description": (
        "Send an approval request email to the GL Controller or designated approver "
        "for a corrective journal entry. The email includes a summary of the imbalance "
        "found, the proposed corrective journal, and the FBDI file path. "
        "Returns confirmation that the email was sent (or a simulated confirmation "
        "if SMTP is not configured)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "journal_id": {
                "type": "integer",
                "description": "GL_JE_HEADERS.JE_HEADER_ID of the journal requiring approval.",
            },
            "approver_email": {
                "type": "string",
                "description": "Email address of the GL Controller or approver.",
            },
            "summary": {
                "type": "string",
                "description": (
                    "Plain-text summary of the imbalance, root cause, and proposed "
                    "correction for inclusion in the approval email body."
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                "description": (
                    "Urgency level — affects email subject line prefix and priority flag. "
                    "Use HIGH or CRITICAL when the period-close date is imminent."
                ),
            },
        },
        "required": ["journal_id", "approver_email", "summary", "urgency"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Assemble tool lists
# ---------------------------------------------------------------------------

_ALL_TOOLS = [
    _GET_UNBALANCED_JOURNALS,
    _GET_JOURNAL_DETAIL,
    _GET_ACCOUNT_DETAILS,
    _FIND_SOURCE_TRANSACTION,
    _DRAFT_CORRECTIVE_JOURNAL,
    _GENERATE_RECONCILIATION_REPORT,
    _REQUEST_APPROVAL,
]


def _to_claude_tool(tool_def: dict) -> dict:
    """
    Convert a canonical tool definition to Anthropic tool_use wire format.

    Anthropic format:
      {
        "name": str,
        "description": str,
        "input_schema": {JSON Schema object}
      }
    """
    return {
        "name": tool_def["name"],
        "description": tool_def["description"],
        "input_schema": tool_def["parameters"],
    }


def _to_openai_tool(tool_def: dict) -> dict:
    """
    Convert a canonical tool definition to OpenAI function_calling wire format.

    OpenAI format:
      {
        "type": "function",
        "function": {
          "name": str,
          "description": str,
          "parameters": {JSON Schema object},
          "strict": True  (enables structured output validation)
        }
      }
    """
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def["description"],
            "parameters": tool_def["parameters"],
            "strict": True,
        },
    }


# Public exports: use these directly in agent implementations
CLAUDE_TOOLS: list[dict] = [_to_claude_tool(t) for t in _ALL_TOOLS]
OPENAI_TOOLS: list[dict] = [_to_openai_tool(t) for t in _ALL_TOOLS]

# Also export the canonical list for testing / introspection
ALL_TOOL_DEFINITIONS: list[dict] = _ALL_TOOLS
TOOL_NAMES: list[str] = [t["name"] for t in _ALL_TOOLS]
