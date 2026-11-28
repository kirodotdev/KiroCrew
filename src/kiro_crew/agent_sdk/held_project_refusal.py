"""The refusal of a Claude chat that an old adapter cannot start only because of held servers.

Raised by ``acp.client_claude_session`` below ``CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION``
and read by ``dashboard.project_mcp_cards``, which raises the held servers' cards. It
lives beside ``project_mcp_holds``, which decides what is held, so the dashboard
reads it through ``agent_sdk`` and never imports the ACP layer.
"""

from __future__ import annotations

from kiro_crew.acp.transport_errors import AcpToolGateUnroutable


class ClaudeHeldProjectServersUnroutable(AcpToolGateUnroutable):
    """A chat refused below the floor only because it holds project MCP servers.

    Approving those servers on their cards removes the one need the old adapter
    cannot meet, so the chat offers the cards for *folder* on *backend*. *lead* says
    what happened and *upgrade* is the install command. The message is the one a
    surface with no cards shows; :meth:`with_cards_shown` words it for a dashboard
    chat that shows the servers' cards or notices.
    """

    def __init__(
        self,
        message: str,
        *,
        backend: str,
        agent: str,
        folder: str,
        lead: str,
        upgrade: str,
    ) -> None:
        super().__init__(message, transient=False, user_worded=True)
        self.backend = backend
        self.agent = agent
        self.folder = folder
        self.lead = lead
        self.upgrade = upgrade

    @classmethod
    def for_surface_without_cards(
        cls, *, backend: str, agent: str, folder: str, lead: str, upgrade: str
    ) -> ClaudeHeldProjectServersUnroutable:
        """The refusal Slack and the other channels show: they raise no card."""
        # Only a dashboard Claude Code chat raises these cards (another harness
        # reads another MCP file), so the person is sent to one.
        message = (
            f"{lead} Allow them in a dashboard Claude Code chat on the same folder, or "
            f"upgrade: {upgrade}"
        )
        return cls(message, backend=backend, agent=agent, folder=folder, lead=lead, upgrade=upgrade)

    def with_cards_shown(self, *, every_server: bool) -> ClaudeHeldProjectServersUnroutable:
        """The same refusal, worded for a chat that shows the held servers.

        *every_server* says each held server has a card. Otherwise some have only
        a notice saying what to change, so the text points at both.
        """
        forward = (
            "Allow them on the cards above"
            if every_server
            else "Allow or fix the servers named above"
        )
        return ClaudeHeldProjectServersUnroutable(
            f"{self.lead} {forward}, then send your message again. Or upgrade: {self.upgrade}",
            backend=self.backend,
            agent=self.agent,
            folder=self.folder,
            lead=self.lead,
            upgrade=self.upgrade,
        )
