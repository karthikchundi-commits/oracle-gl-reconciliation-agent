"""
src/agents/claude_agent.py
--------------------------
Claude (Anthropic) implementation of the GL Reconciliation Agent.

Uses the Anthropic Messages API with tool_use content blocks.
Implements a full multi-turn agentic loop:
  1. Send system prompt + user task to claude-opus-4-5 (or configured model)
  2. Parse tool_use blocks from the response
  3. Execute each tool via _execute_tool (base class dispatch)
  4. Build tool_result content blocks and append to the conversation
  5. Call the LLM again with the updated conversation
  6. Repeat until stop_reason == 'end_turn'
  7. Extract final text and build ReconciliationResult

Anthropic tool_use API reference:
  https://docs.anthropic.com/en/docs/build-with-claude/tool-use

Message structure:
  user message  → {role: "user",      content: str | list[ContentBlock]}
  assistant msg → {role: "assistant", content: list[ContentBlock]}
  tool_result   → added to NEXT user message as content blocks

ContentBlock types:
  text       → {type: "text",     text: str}
  tool_use   → {type: "tool_use", id: str, name: str, input: dict}
  tool_result→ {type: "tool_result", tool_use_id: str, content: str}
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from config.settings import AgentSettings, NotificationSettings, OracleSettings
from src.agents.base_agent import BaseGLReconciliationAgent, ReconciliationResult
from src.agents.tools import CLAUDE_TOOLS
from src.oracle.fusion_client import FusionClient

logger = logging.getLogger(__name__)


class ClaudeGLAgent(BaseGLReconciliationAgent):
    """
    Oracle GL Reconciliation Agent powered by Anthropic Claude.

    Implements the full tool_use agentic loop using the Anthropic
    Messages API.  All tool definitions are provided in CLAUDE_TOOLS
    (Anthropic input_schema format).

    Parameters
    ----------
    fusion_client : FusionClient
    oracle_settings : OracleSettings
    agent_settings : AgentSettings
        Must have provider == 'claude' and a valid anthropic_api_key.
    notification_settings : NotificationSettings
    """

    def __init__(
        self,
        fusion_client: FusionClient,
        oracle_settings: OracleSettings,
        agent_settings: AgentSettings,
        notification_settings: NotificationSettings,
    ) -> None:
        super().__init__(
            fusion_client=fusion_client,
            oracle_settings=oracle_settings,
            agent_settings=agent_settings,
            notification_settings=notification_settings,
        )
        if not agent_settings.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY must be set in .env when using provider=claude."
            )
        self._anthropic = anthropic.Anthropic(api_key=agent_settings.anthropic_api_key)
        self._model = agent_settings.claude_model
        self._max_rounds = agent_settings.max_tool_rounds
        self._temperature = agent_settings.temperature

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, messages: list[dict], tools: list[dict]) -> Any:
        """
        Call the Anthropic Messages API.

        Parameters
        ----------
        messages : list[dict]
            Conversation in Anthropic format (role/content pairs).
        tools : list[dict]
            Tool definitions in Anthropic tool_use format (with input_schema).

        Returns
        -------
        anthropic.types.Message
            Raw Anthropic response object.
        """
        response = self._anthropic.messages.create(
            model=self._model,
            max_tokens=8192,
            temperature=self._temperature,
            system=self._build_system_prompt(),
            messages=messages,
            tools=tools,
        )
        logger.debug(
            "Claude response: stop_reason=%s  usage=%s",
            response.stop_reason,
            response.usage,
        )
        return response

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    def run(self, ledger_id: int, period_name: str) -> ReconciliationResult:
        """
        Execute a full GL reconciliation using Claude's tool_use loop.

        The initial user message instructs Claude to reconcile the specified
        ledger and period using the available tools.  Claude will autonomously
        decide which tools to call, in what order, and how many times.

        Parameters
        ----------
        ledger_id : int
        period_name : str

        Returns
        -------
        ReconciliationResult
        """
        self._reset_run_state()
        logger.info(
            "ClaudeGLAgent.run: ledger_id=%d  period=%s  model=%s",
            ledger_id,
            period_name,
            self._model,
        )

        # Initial user task message
        initial_message = (
            f"Please perform a complete GL reconciliation for Oracle Fusion Cloud.\n\n"
            f"Ledger ID: {ledger_id}\n"
            f"Accounting Period: {period_name}\n\n"
            f"Steps to follow:\n"
            f"1. Call get_unbalanced_journals to find all imbalanced journals in this period.\n"
            f"2. For each imbalanced journal, call get_journal_detail to examine all lines.\n"
            f"3. Call find_source_transaction to trace each imbalance to its subledger source.\n"
            f"4. Analyze the root cause and draft a corrective journal using draft_corrective_journal.\n"
            f"5. Call request_approval with a clear summary for the GL controller.\n"
            f"6. After processing all imbalanced journals, call generate_reconciliation_report.\n"
            f"7. Provide a final concise summary of everything found and corrected.\n\n"
            f"Approver email: {self.notification_settings.approver_email}"
        )

        messages: list[dict] = [
            {"role": "user", "content": initial_message}
        ]

        final_text = ""
        round_num = 0

        while round_num < self._max_rounds:
            round_num += 1
            logger.info("Agent round %d/%d", round_num, self._max_rounds)

            response = self._call_llm(messages, CLAUDE_TOOLS)

            # Build assistant message from response content
            assistant_content = []
            tool_use_blocks = []

            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                    final_text = block.text  # Keep last text as summary
                    logger.debug("Claude text: %s", block.text[:200])

                elif block.type == "tool_use":
                    tool_use_blocks.append(block)
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                    logger.info(
                        "Claude tool_use: name=%s  id=%s", block.name, block.id
                    )

            # Append assistant turn
            messages.append({"role": "assistant", "content": assistant_content})

            # If stop_reason is end_turn and no tool calls, we're done
            if response.stop_reason == "end_turn" and not tool_use_blocks:
                logger.info("Claude reached end_turn — reconciliation complete.")
                break

            # Execute all tool calls and build tool_result user message
            if tool_use_blocks:
                tool_results: list[dict] = []
                for tool_block in tool_use_blocks:
                    tool_result_content = self._execute_tool(
                        tool_name=tool_block.name,
                        tool_input=tool_block.input,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": tool_result_content,
                        }
                    )
                    logger.debug(
                        "Tool result for %s: %s",
                        tool_block.name,
                        tool_result_content[:300],
                    )

                # All tool results go in a single user message
                messages.append({"role": "user", "content": tool_results})

            # If no tool calls and stop_reason is 'tool_use' — should not happen
            # but guard against infinite loops
            if not tool_use_blocks and response.stop_reason == "tool_use":
                logger.warning(
                    "stop_reason=tool_use but no tool_use blocks found — breaking loop."
                )
                break

        else:
            logger.warning(
                "Max tool rounds (%d) reached — agent loop terminated.", self._max_rounds
            )
            final_text += (
                "\n[WARNING: Maximum tool rounds reached. Some journals may not have "
                "been fully processed. Increase MAX_TOOL_ROUNDS in .env if needed.]"
            )

        return self._build_result(
            ledger_id=ledger_id,
            period_name=period_name,
            agent_summary=final_text,
        )
