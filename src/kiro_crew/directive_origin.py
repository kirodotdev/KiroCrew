"""Which directives only a person's turn may issue, and the gate that decides it.

The directive applier (:mod:`kiro_crew.dashboard.session_directive_apply`)
refuses these directives on a turn no person sent. The per-turn prompt guidance
(:mod:`kiro_crew.context_assembly.turn`) reads the same gate, so it never offers
a tool the applier will refuse. Prompt building must not import the dashboard
package, so the gate lives here, and the dashboard re-exports these objects.
"""

from __future__ import annotations

from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.session_surface import has_dashboard_surface

#: Directives that act on the person's own conversation: its project, its
#: context, its board tags.
USER_SURFACE_DIRECTIVES = frozenset({"set_project", "reset_conversation", "chat_tag"})

#: The provenance gate's set: the user-surface directives plus ``suggest_followup``,
#: whose surface gate alone only asks whether a tab exists -- which a cron or
#: sub-agent turn riding a user's slot satisfies.
USER_ORIGIN_DIRECTIVES = USER_SURFACE_DIRECTIVES | frozenset({"suggest_followup"})


def has_user_surface(session_key: str) -> bool:
    """Return whether *session_key* names a user-facing conversation."""
    return has_dashboard_surface(session_key) or is_channel_session_key(session_key)


def user_origin_gate_admits(kind: str, session_key: str, *, producer_is_user_facing: bool) -> bool:
    """Whether the provenance gate lets directive *kind* through for this turn.

    A kind outside :data:`USER_ORIGIN_DIRECTIVES` always passes. One inside it
    passes only on a turn a person sent, on a user-facing conversation.
    """
    if kind not in USER_ORIGIN_DIRECTIVES:
        return True
    return bool(producer_is_user_facing) and has_user_surface(session_key)
