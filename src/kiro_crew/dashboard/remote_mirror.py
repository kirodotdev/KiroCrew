"""In-band mirroring of a turn's WebSocket frames onto its SSE stream.

Why this exists
---------------
``POST /api/chat`` has two transports. The dashboard uses the WebSocket one: the
turn is dispatched detached and its frames (``tool_call``, ``tool_result``,
``chat_segment``, ``chat_done`` …) travel over the shared ``/api/ws``. The SSE
transport instead drains ``slot._pending``, which only ever holds *transcript
rows* — the appended user/assistant/chunk/thinking/error rows. Everything a turn
says about itself that is NOT a transcript row is therefore invisible to an SSE
reader.

That gap is fine for the historical SSE consumer (an OpenAI-compatible client
wants the text, not the tool cards). It is fatal for a *relay*: when a peer runs
a turn on behalf of a local session (see :mod:`kiro_crew.dashboard.remote_relay`),
the local transcript would render the streamed prose with none of the tool
activity around it, and would never learn the turn had ended.

So a relay reader opts in, per turn, with ``?relay=1``. For the life of that
request the slot is *mirrored*: every WebSocket frame scoped to it is ALSO
pushed onto its pending queue as a wire-only frame, so the one SSE stream
carries the complete vocabulary.

Why a denylist, not an allowlist
--------------------------------
The frames worth mirroring are "all of them except the few already present in
band". Enumerating the ~16 to include would silently drop any frame type added
later — the relay would lose fidelity with no test failing. Naming the 5
exclusions instead means a new frame type is mirrored by default, and the only
maintenance burden is the (rare) addition of another dual-written row.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Frame types that must NOT be mirrored, because the SSE stream already carries
#: the same information as a transcript row (or, for ``context_usage``, because
#: :meth:`DashboardState.broadcast_context_usage` already pushes its own wire
#: frame). Mirroring these would double every chunk and every message.
MIRROR_SKIP_EVENTS: frozenset[str] = frozenset(
    {
        "chat_message",  # the appended row itself is drained by the SSE reader
        "chat_chunk",  # ditto: appended with role "chunk"
        "chat_thinking",  # ditto: appended with role "thinking"
        "context_usage",  # dashboard_persistence pushes its own wire frame
        "chat_done",  # relay_remote_turn's own `finally` broadcasts it locally
    }
)

#: Prefix stamped on a mirrored frame's ``cls``/role so the relay reader can tell
#: it apart from a transcript row without knowing the frame vocabulary. Without
#: it the consumer would need the same ~16-name allowlist this module exists to
#: avoid, and a frame type whose name happened to collide with a transcript role
#: would be misread as prose.
MIRROR_CLS_PREFIX = "relay:"

#: Slot keys currently being relayed. Process-global on purpose: the mirror is a
#: property of an in-flight request, and the gateway is one process, so a set
#: here is both the smallest state and the easiest thing to assert on in a test.
_MIRRORED: set[str] = set()


def attach(slot_key: str) -> bool:
    """Start mirroring *slot_key*; the return says whether THIS call owns it.

    Idempotent by key, and the ownership token is what makes it safe: two
    overlapping relay readers on one slot both get a stream, and only the first
    one's :func:`detach` clears the flag, so neither can truncate the other.
    """
    if not slot_key or slot_key in _MIRRORED:
        return False
    _MIRRORED.add(slot_key)
    return True


def detach(slot_key: str, owned: bool) -> None:
    """Stop mirroring *slot_key*, but only for the caller that started it."""
    if owned:
        _MIRRORED.discard(slot_key)


def reset_for_tests() -> None:
    """Drop all mirror registrations (test hygiene only)."""
    _MIRRORED.clear()


def _slot_key_of(data: object) -> str:
    """The slot a frame is scoped to, or "" when it is not slot-scoped.

    ``slot_title`` and ``session_summary`` carry ``key`` rather than ``slot`` —
    the same special case ``ws_event_scope`` makes — so both spellings are read.
    """
    if not isinstance(data, dict):
        return ""
    for field in ("slot", "key"):
        value = data.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def mirror_frame(owner: Any, msg_type: str, data: object) -> None:
    """Push *data* onto the mirrored slot's pending queue, if it is mirrored.

    Best-effort by design: a frame that will not serialise is dropped rather
    than allowed to break the broadcast it is shadowing. Mirroring must never be
    able to fail a turn that would otherwise have succeeded.
    """
    if not _MIRRORED or msg_type in MIRROR_SKIP_EVENTS:
        return
    slot_key = _slot_key_of(data)
    if slot_key not in _MIRRORED:
        return
    slot = owner.get_slot(slot_key)
    if slot is None:
        return
    # Neither log below names the frame type. ``msg_type`` reaches here from
    # ``broadcast_ws``, and the mirror is a denylist, so every frame the gateway
    # ever broadcasts flows through this one pair of lines — CodeQL's
    # ``py/clear-text-logging-sensitive-data`` flags them for exactly that reach.
    # The type is a nice-to-have on two defensive DEBUG paths; "a frame was
    # dropped here" is the part worth having, and it costs nothing to be certain
    # no interpolated string can carry a credential into the log.
    try:
        encoded = json.dumps(data)
    except (TypeError, ValueError):
        logger.debug("Relay mirror skipped an unserialisable frame")
        return
    try:
        slot.push_wire_frame(MIRROR_CLS_PREFIX + msg_type, encoded)
    except Exception:  # pragma: no cover - defensive, see docstring
        logger.debug("Relay mirror could not queue a frame", exc_info=True)
