"""codex-acp's native-subagent wire: the capability that turns it on and the frames it sends.

codex-acp gives each native subagent its own ACP session only when the client
declares ``clientCapabilities.subagents``. Without it the adapter flattens the
child into generic "Start subagent" tool rows on the parent and sends none of
the child's own work. The handlers this declaration promises are
``session_handle_native_children._handle_announced_subagent`` (the lifecycle
frames) and the runtime's announced-child route (the child's own frames).

Adapters built on ACP SDK 1.x drop the key while parsing ``initialize``, so on
those the declaration is inert and the legacy activity rows are read instead
(``_handle_legacy_codex_subagent``). The adapter's other opt-in, JetBrains
AIR's private ``_meta`` capability, is not sent: it turns on the rest of AIR's
private contract as well.
"""

from __future__ import annotations

from kiro_crew.acp.types import ACP_CLIENT_CAPABILITIES

CODEX_CLIENT_CAPABILITIES: dict = {
    **ACP_CLIENT_CAPABILITIES,
    "subagents": {},
    # Opts into codex-acp's native compaction lifecycle frames, as the Codex row
    # of ``client_native_children.CLIENT_CAPABILITIES_BY_BACKEND`` does.
    "session": {"compaction": {}},
}

#: A native subagent's lifecycle, sent on its PARENT's session after
#: ``clientCapabilities.subagents``, keyed on ``subagentSessionId``.
UPDATE_SUBAGENT_SPAWNED = "subagent_spawned"
UPDATE_SUBAGENT_STATE = "subagent_state_update"
SUBAGENT_LIFECYCLE_UPDATES = frozenset({UPDATE_SUBAGENT_SPAWNED, UPDATE_SUBAGENT_STATE})
#: The ``subagent_state_update.state`` values that end a codex-acp 2.2.x child.
SUBAGENT_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "disconnected"})
