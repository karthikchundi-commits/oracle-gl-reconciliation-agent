"""
tests/test_agents.py
---------------------
Unit tests for agent components: tools definitions, factory, and base agent.

Does not call real LLM APIs — tests tool dispatch, tool schema validity,
and factory routing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.tools import (
    CLAUDE_TOOLS,
    OPENAI_TOOLS,
    ALL_TOOL_DEFINITIONS,
    TOOL_NAMES,
)
from src.agents.factory import AgentFactory


# ---------------------------------------------------------------------------
# Tool schema validation
# ---------------------------------------------------------------------------

class TestToolDefinitions:

    def test_claude_tools_have_required_fields(self):
        """Every Claude tool must have name, description, and input_schema."""
        for tool in CLAUDE_TOOLS:
            assert "name" in tool, f"Claude tool missing 'name': {tool}"
            assert "description" in tool, f"Claude tool {tool['name']} missing 'description'"
            assert "input_schema" in tool, f"Claude tool {tool['name']} missing 'input_schema'"
            assert tool["input_schema"]["type"] == "object"

    def test_openai_tools_have_required_fields(self):
        """Every OpenAI tool must have type='function' and function.name/description/parameters."""
        for tool in OPENAI_TOOLS:
            assert tool["type"] == "function"
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            assert func["parameters"]["type"] == "object"

    def test_all_tools_present(self):
        expected_names = {
            "get_unbalanced_journals",
            "get_journal_detail",
            "get_account_details",
            "find_source_transaction",
            "draft_corrective_journal",
            "generate_reconciliation_report",
            "request_approval",
        }
        assert set(TOOL_NAMES) == expected_names

    def test_claude_and_openai_tool_count_match(self):
        assert len(CLAUDE_TOOLS) == len(OPENAI_TOOLS)
        assert len(CLAUDE_TOOLS) == len(ALL_TOOL_DEFINITIONS)

    def test_claude_tool_names_match_openai_names(self):
        claude_names = {t["name"] for t in CLAUDE_TOOLS}
        openai_names = {t["function"]["name"] for t in OPENAI_TOOLS}
        assert claude_names == openai_names

    def test_get_unbalanced_journals_required_params(self):
        tool = next(t for t in ALL_TOOL_DEFINITIONS if t["name"] == "get_unbalanced_journals")
        params = tool["parameters"]
        assert "ledger_id" in params["properties"]
        assert "period_name" in params["properties"]
        assert "ledger_id" in params["required"]
        assert "period_name" in params["required"]

    def test_draft_corrective_journal_has_correction_lines(self):
        tool = next(t for t in ALL_TOOL_DEFINITIONS if t["name"] == "draft_corrective_journal")
        params = tool["parameters"]
        assert "correction_lines" in params["properties"]
        assert params["properties"]["correction_lines"]["type"] == "array"

    def test_request_approval_urgency_enum(self):
        tool = next(t for t in ALL_TOOL_DEFINITIONS if t["name"] == "request_approval")
        urgency = tool["parameters"]["properties"]["urgency"]
        assert "enum" in urgency
        assert "HIGH" in urgency["enum"]
        assert "CRITICAL" in urgency["enum"]

    def test_openai_tools_have_strict_true(self):
        for tool in OPENAI_TOOLS:
            assert tool["function"].get("strict") is True, (
                f"OpenAI tool {tool['function']['name']} should have strict=True "
                "for structured output validation"
            )

    def test_tool_parameters_are_valid_json_schema(self):
        """Verify all tool parameter schemas can be serialized (no circular refs)."""
        for tool in ALL_TOOL_DEFINITIONS:
            schema_str = json.dumps(tool["parameters"])
            reloaded = json.loads(schema_str)
            assert reloaded["type"] == "object"


# ---------------------------------------------------------------------------
# AgentFactory
# ---------------------------------------------------------------------------

class TestAgentFactory:

    @pytest.fixture
    def mock_dependencies(self, tmp_path):
        """Return minimal mock objects for factory creation."""
        oracle_cfg = MagicMock()
        oracle_cfg.ledger_id = 1001
        oracle_cfg.period_name = "Jan-25"

        agent_cfg = MagicMock()
        agent_cfg.output_dir = str(tmp_path)
        agent_cfg.max_tool_rounds = 10
        agent_cfg.temperature = 0.0
        agent_cfg.claude_model = "claude-opus-4-5"
        agent_cfg.openai_model = "gpt-4o"
        agent_cfg.anthropic_api_key = "sk-ant-test-key"
        agent_cfg.openai_api_key = "sk-test-key"

        notif_cfg = MagicMock()
        notif_cfg.approver_email = "test@example.com"
        notif_cfg.smtp_host = "smtp.example.com"
        notif_cfg.smtp_port = 587
        notif_cfg.smtp_use_tls = True
        notif_cfg.smtp_username = None
        notif_cfg.smtp_password = None
        notif_cfg.from_address = "agent@example.com"

        fusion_client = MagicMock()

        return fusion_client, oracle_cfg, agent_cfg, notif_cfg

    def test_creates_claude_agent(self, mock_dependencies):
        fusion_client, oracle_cfg, agent_cfg, notif_cfg = mock_dependencies
        with patch("src.agents.claude_agent.anthropic.Anthropic"):
            agent = AgentFactory.create(
                provider="claude",
                fusion_client=fusion_client,
                oracle_settings=oracle_cfg,
                agent_settings=agent_cfg,
                notification_settings=notif_cfg,
            )
        from src.agents.claude_agent import ClaudeGLAgent
        assert isinstance(agent, ClaudeGLAgent)

    def test_creates_openai_agent(self, mock_dependencies):
        fusion_client, oracle_cfg, agent_cfg, notif_cfg = mock_dependencies
        with patch("src.agents.openai_agent.openai.OpenAI"):
            agent = AgentFactory.create(
                provider="openai",
                fusion_client=fusion_client,
                oracle_settings=oracle_cfg,
                agent_settings=agent_cfg,
                notification_settings=notif_cfg,
            )
        from src.agents.openai_agent import OpenAIGLAgent
        assert isinstance(agent, OpenAIGLAgent)

    def test_case_insensitive_provider(self, mock_dependencies):
        fusion_client, oracle_cfg, agent_cfg, notif_cfg = mock_dependencies
        with patch("src.agents.claude_agent.anthropic.Anthropic"):
            agent = AgentFactory.create(
                provider="CLAUDE",
                fusion_client=fusion_client,
                oracle_settings=oracle_cfg,
                agent_settings=agent_cfg,
                notification_settings=notif_cfg,
            )
        from src.agents.claude_agent import ClaudeGLAgent
        assert isinstance(agent, ClaudeGLAgent)

    def test_invalid_provider_raises_value_error(self, mock_dependencies):
        fusion_client, oracle_cfg, agent_cfg, notif_cfg = mock_dependencies
        with pytest.raises(ValueError, match="Unknown provider"):
            AgentFactory.create(
                provider="gemini",
                fusion_client=fusion_client,
                oracle_settings=oracle_cfg,
                agent_settings=agent_cfg,
                notification_settings=notif_cfg,
            )


# ---------------------------------------------------------------------------
# BaseGLReconciliationAgent._execute_tool dispatch
# ---------------------------------------------------------------------------

class TestToolDispatch:
    """
    Tests tool dispatch in the base agent using a minimal concrete subclass.
    """

    @pytest.fixture
    def agent(self, tmp_path, mocker):
        """Create a minimal concrete agent with mocked FusionClient."""
        from src.agents.base_agent import BaseGLReconciliationAgent, ReconciliationResult

        class _ConcreteAgent(BaseGLReconciliationAgent):
            def run(self, ledger_id, period_name):
                pass
            def _call_llm(self, messages, tools):
                pass

        oracle_cfg = MagicMock()
        oracle_cfg.ledger_id = 1001
        oracle_cfg.period_name = "Jan-25"

        agent_cfg = MagicMock()
        agent_cfg.output_dir = str(tmp_path)
        agent_cfg.max_tool_rounds = 10
        agent_cfg.temperature = 0.0

        notif_cfg = MagicMock()
        notif_cfg.approver_email = "approver@test.com"
        notif_cfg.smtp_host = "smtp.test.com"
        notif_cfg.smtp_port = 587
        notif_cfg.smtp_use_tls = True
        notif_cfg.smtp_username = None
        notif_cfg.smtp_password = None
        notif_cfg.from_address = "agent@test.com"

        fusion_client = mocker.MagicMock()

        agent_instance = _ConcreteAgent(
            fusion_client=fusion_client,
            oracle_settings=oracle_cfg,
            agent_settings=agent_cfg,
            notification_settings=notif_cfg,
        )
        return agent_instance

    def test_dispatch_get_unbalanced_journals(self, agent, mocker):
        mocker.patch(
            "src.agents.base_agent.get_unbalanced_journals",
            return_value=[{"journal_header_id": 100432, "imbalance_amount": 124.50}],
        )
        mocker.patch.object(
            agent.client,
            "get_journals",
            return_value=[{"JournalHeaderId": 100432}],
        )
        result_str = agent._execute_tool(
            "get_unbalanced_journals",
            {"ledger_id": 1001, "period_name": "Jan-25"},
        )
        result = json.loads(result_str)
        assert result["count"] == 1
        assert result["unbalanced_journals"][0]["journal_header_id"] == 100432

    def test_dispatch_unknown_tool_returns_error_json(self, agent):
        result_str = agent._execute_tool("nonexistent_tool", {})
        result = json.loads(result_str)
        assert result["error"] is True
        assert "Unknown tool" in result["message"]

    def test_dispatch_request_approval_logs_when_smtp_unconfigured(self, agent):
        result_str = agent._execute_tool(
            "request_approval",
            {
                "journal_id": 100432,
                "approver_email": "ctrl@test.com",
                "summary": "Test summary",
                "urgency": "HIGH",
            },
        )
        result = json.loads(result_str)
        # smtp_host is mocked — will either send or fall through to simulated
        assert "to" in result
        assert result["to"] == "ctrl@test.com"
