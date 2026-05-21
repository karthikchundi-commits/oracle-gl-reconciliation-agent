"""
src/agents/openai_agent.py
--------------------------
OpenAI GPT-4o implementation of the GL Reconciliation Agent.

Uses the OpenAI Chat Completions API with function_calling (tools).
Implements a full multi-turn agentic loop:
  1. Send system message + user task to gpt-4o (or configured model)
  2. Parse tool_calls from the response message
  3. Execute each tool via _execute_tool (base class dispatch)
  4. Append tool result messages (role="tool") to the conversation
  5. Call the model again with the updated conversation
  6. Repeat until finish_reason == 'stop'
  7. Extract final text and build ReconciliationResult

OpenAI function_calling / tools API reference:
  https://platform.openai.com/docs/guides/function-calling

Message structure:
  system    → {role: "system",    content: str}
  user      → {role: "user",      content: str}
  assistant → {role: "assistant", content: str|None, tool_calls: list[ToolCall]|None}
  tool      → {role: "tool",      tool_call_id: str, content: str}

tool_call structure:
  {id: str, type: "function", function: {name: str, arguments: str}}
  Note: arguments is a JSON string — must be parsed with json.loads()
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import openai
from openai.types.chat import ChatCompletion, ChatCompletionMessage

from config.settings import AgentSettings, NotificationSettings, OracleSettings
from src.agents.base_agent import BaseGLReconciliationAgent, ReconciliationResult
from src.agents.tools import OPENAI_TOOLS
from src.oracle.fusion_client import FusionClient

logger = logging.getLogger(__name__)


class OpenAIGLAgent(BaseGLReconciliationAgent):
    """
    Oracle GL Reconciliation Agent powered by OpenAI GPT-4o.

    Implements the full function_calling agentic loop using the OpenAI
    Chat Completions API.  All tool definitions are provided in OPENAI_TOOLS
    (OpenAI tools/function format with strict=True).

    Parameters
    ----------
    fusion_client : FusionClient
    oracle_settings : OracleSettings
    agent_settings : AgentSettings
        Must have provider == 'openai' and a valid openai_api_key.
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
        if not agent_settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY must be set in .env when using provider=openai."
            )
        self._openai = openai.OpenAI(api_key=agent_settings.openai_api_key)
        self._model = agent_settings.openai_model
        self._max_rounds = agent_settings.max_tool_rounds
        self._temperature = agent_settings.temperature

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, messages: list[dict], tools: list[dict]) -> ChatCompletion:
        """
        Call the OpenAI Chat Completions API.

        Parameters
        ----------
        messages : list[dict]
            Conversation in OpenAI chat format (role/content dicts).
        tools : list[dict]
            Tool definitions in OpenAI function_calling format.

        Returns
        -------
        openai.types.chat.ChatCompletion
            Raw OpenAI response object.
        """
        response = self._openai.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            messages=messages,
            tools=tools,
            tool_choice="auto",  # Let model decide when to call tools
            parallel_tool_calls=True,  # Allow multiple tool calls per turn
        )
        choice = response.choices[0]
        logger.debug(
            "OpenAI response: finish_reason=%s  model=%s  usage=%s",
            choice.finish_reason,
            response.model,
            response.usage,
        )
        return response

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------

    def run(self, ledger_id: int, period_name: str) -> ReconciliationResult:
        """
        Execute a full GL reconciliation using GPT-4o's function_calling loop.

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
            "OpenAIGLAgent.run: ledger_id=%d  period=%s  model=%s",
            ledger_id,
            period_name,
            self._model,
        )

        # Build initial conversation
        system_message = {
            "role": "system",
            "content": self._build_system_prompt(),
        }
        user_message = {
            "role": "user",
            "content": (
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
            ),
        }

        messages: list[dict] = [system_message, user_message]

        final_text = ""
        round_num = 0

        while round_num < self._max_rounds:
            round_num += 1
            logger.info("Agent round %d/%d", round_num, self._max_rounds)

            response = self._call_llm(messages, OPENAI_TOOLS)
            choice = response.choices[0]
            assistant_message: ChatCompletionMessage = choice.message

            # Serialize assistant message back to dict for conversation history
            # (OpenAI SDK objects are not directly serializable as message dicts)
            assistant_dict: dict = {"role": "assistant"}
            if assistant_message.content:
                assistant_dict["content"] = assistant_message.content
                final_text = assistant_message.content
                logger.debug("GPT-4o text: %s", assistant_message.content[:200])
            else:
                assistant_dict["content"] = None

            if assistant_message.tool_calls:
                assistant_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_message.tool_calls
                ]

            messages.append(assistant_dict)

            # If finish_reason is 'stop' and no tool calls, we're done
            if choice.finish_reason == "stop" and not assistant_message.tool_calls:
                logger.info("GPT-4o finish_reason=stop — reconciliation complete.")
                break

            # Execute all tool calls
            if assistant_message.tool_calls:
                for tool_call in assistant_message.tool_calls:
                    func_name = tool_call.function.name
                    try:
                        func_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError as exc:
                        logger.error(
                            "Failed to parse arguments for tool %s: %s", func_name, exc
                        )
                        func_args = {}

                    logger.info("GPT-4o tool_call: name=%s  id=%s", func_name, tool_call.id)

                    tool_result_content = self._execute_tool(
                        tool_name=func_name,
                        tool_input=func_args,
                    )
                    logger.debug(
                        "Tool result for %s: %s", func_name, tool_result_content[:300]
                    )

                    # Each tool result is a separate message with role="tool"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result_content,
                        }
                    )

            # Guard: if model returned tool_calls but finish_reason is already 'stop'
            # (shouldn't happen but be safe)
            if choice.finish_reason == "stop" and assistant_message.tool_calls:
                logger.warning(
                    "finish_reason=stop with tool_calls present — executing tools "
                    "and continuing one more round."
                )
                continue

            # If finish_reason is neither 'stop' nor 'tool_calls', break
            if choice.finish_reason not in ("tool_calls", "stop", None):
                logger.warning(
                    "Unexpected finish_reason='%s' — stopping agent loop.",
                    choice.finish_reason,
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
