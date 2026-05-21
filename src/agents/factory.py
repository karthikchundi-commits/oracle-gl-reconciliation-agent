"""
src/agents/factory.py
---------------------
AgentFactory — creates the correct agent implementation based on the
configured provider (claude | openai).

Usage:
    from src.agents.factory import AgentFactory
    agent = AgentFactory.create(
        provider="claude",
        fusion_client=client,
        oracle_settings=oracle_cfg,
        agent_settings=agent_cfg,
        notification_settings=notif_cfg,
    )
    result = agent.run(ledger_id=1001, period_name="Jan-25")
"""

from __future__ import annotations

from config.settings import AgentSettings, NotificationSettings, OracleSettings
from src.agents.base_agent import BaseGLReconciliationAgent
from src.oracle.fusion_client import FusionClient


class AgentFactory:
    """
    Static factory for constructing GL Reconciliation Agent instances.

    Decouples agent creation from the specific provider implementation,
    enabling the caller to switch between Claude and OpenAI by changing
    a single configuration value.
    """

    @staticmethod
    def create(
        provider: str,
        fusion_client: FusionClient,
        oracle_settings: OracleSettings,
        agent_settings: AgentSettings,
        notification_settings: NotificationSettings,
    ) -> BaseGLReconciliationAgent:
        """
        Instantiate and return the appropriate agent for the given provider.

        Parameters
        ----------
        provider : str
            Must be 'claude' or 'openai' (case-insensitive).
        fusion_client : FusionClient
            Authenticated Oracle Fusion Cloud REST client.
        oracle_settings : OracleSettings
        agent_settings : AgentSettings
        notification_settings : NotificationSettings

        Returns
        -------
        BaseGLReconciliationAgent
            Either ClaudeGLAgent or OpenAIGLAgent.

        Raises
        ------
        ValueError
            If provider is not 'claude' or 'openai'.
        """
        provider = provider.strip().lower()

        if provider == "claude":
            # Import here to avoid loading anthropic SDK when not needed
            from src.agents.claude_agent import ClaudeGLAgent
            return ClaudeGLAgent(
                fusion_client=fusion_client,
                oracle_settings=oracle_settings,
                agent_settings=agent_settings,
                notification_settings=notification_settings,
            )

        elif provider == "openai":
            from src.agents.openai_agent import OpenAIGLAgent
            return OpenAIGLAgent(
                fusion_client=fusion_client,
                oracle_settings=oracle_settings,
                agent_settings=agent_settings,
                notification_settings=notification_settings,
            )

        else:
            raise ValueError(
                f"Unknown provider: '{provider}'. "
                f"Supported providers are: 'claude', 'openai'. "
                f"Set AGENT_PROVIDER in your .env file."
            )
