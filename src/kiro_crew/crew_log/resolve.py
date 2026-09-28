"""Answer "which crew log unit does this slot's work belong to?".

:mod:`kiro_crew.crew_log.emit` keys every entry by an ACP session id,
because the turn path holds one: the runner has the client in hand and reads the
id straight off it. Several sites that produce facts about a session do NOT hold
that client -- a background model call charged to the session, a subagent spawned
by it, the agent's own task list -- and carry only the key naming the slot or the
session. This module is the one place that gap is closed.

**What the resolver may claim.** A slot owns exactly one ACP session id AT A
TIME, not for its whole life. A plain resume reuses the persisted id
(``AcpSessionHandle`` replays it into ``session/load``), but a reset, an
agent/model/effort switch, a compaction that recycles the session, and a provider
swap all tear the ACP session down, and the successor cold-starts a NEW id --
``session_lifecycle`` says so at the teardown itself. So the honest question this
module answers is "which unit is this slot's work landing in *now*", and the
answer is only valid at the moment it is asked. That is exactly the guarantee an
emit site needs, because every entry records what was observed at that site when
it was observed; it is NOT enough to reconstruct which unit some earlier fact
went to, and no caller should use it that way.

**Read-only, and synchronous.** The lookup is an exact registry read plus an
attribute read on the provider it returns, so this never awaits and never touches
disk. The persisted :class:`~kiro_crew.session_map.SessionMap` is deliberately
NOT consulted: its ``get`` repairs or removes an entry it finds stale, which is a
write, and this module is called from paths that must not mutate session state as
a side effect of describing it. It also answers only for ids that reached disk, so
it would miss exactly the live session the caller is asking about.

There is deliberately no slot-keyed entry point. An earlier draft had one that
read the slot's live ``_acp_client`` first and fell back to the registry, but the
registry answers the same id during a turn as between turns -- the provider it
holds IS the one the runner reads -- so the extra level had no caller and no
question only it could answer.

**Unknown is an answer.** Every function returns :data:`UNKNOWN` (the empty
string) when the key has no live ACP session -- a slot that has never run a turn,
one whose session was torn down and not yet re-created, a key naming nothing. The
emitter treats an empty session id as a no-op, so an unresolved key drops the
entry rather than filing it under a guess. A log that omits a fact is behind;
one that attributes a fact to the wrong session is wrong, and nothing downstream
can tell.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.crew_log.emit import session_id_of

__all__ = ["UNKNOWN", "unit_for_session_key"]

#: What every function here returns when the key has no live ACP session. The
#: emitter's own no-op guard is ``not session_id``, so this value flows straight
#: through as "do not write" without the caller needing a second branch.
UNKNOWN = ""


def unit_for_session_key(sessions: Any, session_key: str) -> str:
    """The crew log unit *session_key*'s provider is serving, or :data:`UNKNOWN`.

    For callers that hold a SessionManager and a session key but no dashboard
    state -- the subagent manager is the case that matters, since a subagent's
    parent is named to it as a key and it never sees the parent's slot.

    ``get_provider`` is an exact registry lookup, so a key naming nothing answers
    None and this answers :data:`UNKNOWN`. One retry is allowed, and only under a
    premise that makes it a lookup rather than a guess: a key containing no colon
    cannot already be a namespaced session key (``dashboard:``, ``slack:``,
    ``subagent:`` all carry one), so a bare slot name is retried in its dashboard
    form. A key that already carries a namespace is never rewritten -- doing so is
    how ``slack:<ts>`` becomes the nonexistent ``dashboard:slack:<ts>``.
    """
    if not session_key or sessions is None:
        return UNKNOWN
    get_provider = getattr(sessions, "get_provider", None)
    if not callable(get_provider):
        return UNKNOWN
    try:
        found = session_id_of(get_provider(session_key))
    except Exception:
        return UNKNOWN
    if found or ":" in session_key:
        return found
    try:
        return session_id_of(get_provider(f"dashboard:{session_key}"))
    except Exception:
        return UNKNOWN
