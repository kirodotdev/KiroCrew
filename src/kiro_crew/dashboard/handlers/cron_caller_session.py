"""Recognizing the session a member-scoped call comes from.

The lesson routes and several sibling handlers (memory, the agent panel, the
session and work ledgers) admit a call only from a session they recognize, in a
memory mode that allows it; :func:`_recognize_session` is that check.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from aiohttp import web

from kiro_crew.dashboard.handlers.cron_shared import _sel
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.validation import SLACK_THREAD_TS_RE

from ._shared import _probe_persisted_session


# Delete-route policy for the archived-session recovery path: only a temporary
# session is blocked from deleting; incognito may delete (an active user
# action), mirroring the live-slot ``_blocks_reads_session`` policy. The create
# route blocks every private mode via the canonical
# ``history.is_incognito_transcript`` classifier instead.
def _is_temporary_transcript(persisted_mode: str) -> bool:
    return persisted_mode == "temporary"


async def _headless_mode_refusal(
    state: DashboardState,
    sk: str,
    operation: str,
    blocks_mode: Callable[[str], bool],
) -> web.Response | None:
    """Enforce the admitted mode without consulting a replacement parent."""
    from kiro_crew.dashboard.handlers._shared import resolve_session_memory_mode
    from kiro_crew.workflow_memory import WorkflowMemoryError

    try:
        mode = await resolve_session_memory_mode(state, sk)
        if not blocks_mode(mode):
            return None
    except (OSError, ValueError, WorkflowMemoryError):
        pass  # Unknown birth policy is not permission to access memory.
    _sel().log_api_access(
        caller=sk,
        operation=operation,
        outcome="denied",
        source="dashboard",
        resources="restricted_session_mode",
    )
    return web.json_response(
        {
            "error": "Memory access is not allowed in this session mode.",
            "code": "restricted_session",
        },
        status=403,
    )


async def _recognize_session(
    state: DashboardState,
    sk: str,
    operation: str,
    *,
    blocks_persisted_mode: Callable[[str], bool],
) -> web.Response | None:
    """Session-recognition gate shared by the lessons and memory routes.

    Applies one slot / restricted-key / channel-namespace / persisted-JSONL
    cascade to every caller so the mutating routes cannot diverge.
    Returns a refusal :class:`web.Response`, or ``None`` when the session is
    recognised. Every decision — allow or deny — emits a SEL audit event
    under *operation*.

    ``blocks_persisted_mode`` is the per-route policy for the
    archived-session recovery path: writes block every private mode (the
    canonical ``history.is_incognito_transcript`` classifier); lesson delete
    blocks only ``temporary``. A ``None``
    (unreadable or ambiguous) persisted mode always fails closed regardless
    of policy. Every refusal body carries a machine-readable ``code`` field
    (``missing_session_key`` / ``unknown_session`` on the 400s,
    ``restricted_session`` on the 403), so clients dispatch on the
    identifier rather than the prose.
    """
    if not sk:
        _sel().log_api_access(
            caller="anonymous",
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="missing_session_key",
        )
        return web.json_response(
            {"error": "missing X-Session-Key", "code": "missing_session_key"},
            status=400,
        )
    if sk == "dashboard:ui":
        # Browser UI's static key — implicitly trusted, but the allow
        # decision itself is still an authorization outcome and must be
        # audited (every permission decision emits a SEL event).
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="dashboard_ui",
        )
        return None
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    is_subagent = sk.startswith("subagent:")
    # A child needs its live owner; neither a colliding dashboard slot nor a
    # retained transcript or restriction marker can replace that allocation.
    in_slots = not is_subagent and slot_name in state._slots
    in_restricted = not is_subagent and sk in state._restricted_keys
    # Headless callers have no slot. A namespace only selects this lookup:
    # recognition still requires the FULL key's live owner. Dashboard/archive
    # callers keep their persisted-mode check even if a provider remains alive.
    # Dedicated children use SessionManager; shared children own runtime handles
    # through SubagentManager. Neither a saved run nor its parent's PID suffices.
    # Private proof/store authorization and restricted-mode gates stay separate.
    sessions = getattr(state, "sessions", None)
    subagents = getattr(state, "subagents", None)
    in_live_session = (
        sk.startswith(("subagent:", "wf:", "wf-pool:", "wf-unpooled:", "wf-worker:", "wf-author:"))
        and sessions is not None
        and sessions.has_session(sk) is True
    ) or (subagents is not None and subagents.has_live_shared_session(sk) is True)
    if in_live_session:
        refusal = await _headless_mode_refusal(state, sk, operation, blocks_persisted_mode)
        if refusal is not None:
            return refusal
    # A channel-originated session (Slack, Telegram, Discord, Webex,
    # WeCom, …) is a legitimate established session: its key is namespaced
    # ``{channel}:{conversation_id}`` and the transport publishes
    # ``session_pid`` so the gateway resolves this X-Session-Key.
    # Recognise the WHOLE channel-namespace family via the canonical
    # ``is_channel_session_key`` — not just Slack. Two reasons this is the
    # right gate, both already true for Slack:
    #   * the first memory call in a fresh channel thread races the JSONL
    #     flush (which only lands after the LLM turn completes), so a
    #     namespace fast-path avoids a spurious HTTP 400 until the
    #     transcript is on disk; and
    #   * the ``_probe_persisted_session`` fallback below cannot
    #     rescue a channel key anyway — ``slot_name`` is
    #     ``sk.split(":", 1)[-1]`` (inner colons kept, channel prefix
    #     dropped) while the file is ``dashboard_<safe_key>.jsonl`` with
    #     colons folded to ``_``, so no probed name ever matches (and a
    #     colon is now rejected outright by ``_persisted_session_path``).
    # Accepting only ``slack:`` would fail learn_add with HTTP 400
    # "unknown session" from every OTHER channel (Telegram / Discord /
    # Webex / WeCom) even though the session is fully identified. The bare
    # Slack thread_ts shim covers native-Slack keys.
    # Incognito/temporary sessions are still blocked by each route's
    # live-slot policy check (Slack is the only channel with that concept),
    # so widening the namespace does not widen memory writes to ephemeral
    # sessions.
    is_channel_ns = is_channel_session_key(sk) or bool(SLACK_THREAD_TS_RE.match(sk))
    # Only consult the on-disk JSONL when the cheaper in-memory checks all
    # fail. ``_probe_persisted_session()`` performs synchronous filesystem
    # I/O (path resolution plus a bounded metadata head read), so it runs
    # via ``asyncio.to_thread`` — never on the event loop (AUTOSDE
    # ``no-blocking-call-on-event-loop``) — and only on this rare recovery
    # path, leaving the common live-slot path free of both I/O and a thread
    # hop. One composed call answers BOTH questions (does the session
    # exist, and may it touch memory) from a single path resolution, so the
    # two decisions can never be made about different files.
    if not (in_slots or in_restricted or is_channel_ns or in_live_session):
        if is_subagent:
            exists, persisted_mode = False, None
        else:
            exists, persisted_mode = await asyncio.to_thread(_probe_persisted_session, slot_name)
        if not exists:
            # Slot may have been evicted from memory (idle sweep,
            # gateway restart) while the MCP subprocess keeps its
            # original KIROCREW_SESSION_KEY. No session JSONL means
            # the key genuinely does not belong to any established
            # session. (Presence does NOT imply the session is
            # non-ephemeral — every memory_mode writes a transcript —
            # which is what ``persisted_mode`` below settles.)
            _sel().log_api_access(
                caller=sk,
                operation=operation,
                outcome="denied",
                source="dashboard",
                resources="unknown_session",
            )
            return web.json_response(
                {"error": "unknown session", "code": "unknown_session"},
                status=400,
            )
        if persisted_mode is None or blocks_persisted_mode(persisted_mode):
            # Archiving a tab drops the slot AND discards its
            # ``_restricted_keys`` entry while leaving the transcript —
            # and its ``memory_mode`` marker — on disk, so the two
            # in-memory checks above cannot see that this session is
            # ephemeral. The persisted mode is the only remaining
            # evidence. ``None`` means the header was unreadable, which
            # is NOT evidence that the call is allowed: append() writes
            # the metadata line at file creation, so a normal session
            # always has one. Fail closed.
            _sel().log_api_access(
                caller=sk,
                operation=operation,
                outcome="denied",
                source="dashboard",
                resources="restricted_session_block",
            )
            return web.json_response(
                {
                    "error": "Memory writes are not allowed in this session mode.",
                    # Machine-readable per the error-code contract; matches
                    # the code already used for this condition at
                    # handlers/memory.py's restricted-session refusal.
                    "code": "restricted_session",
                },
                status=403,
            )
        # JSONL-fallback is the sole reason the call is permitted.
        # Audit it as an allow decision so session-recovery
        # authorization is traceable alongside the deny path above.
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="jsonl_fallback_recovery",
        )
    elif in_slots:
        # Live in-memory slot — the common happy path. Audit so that
        # every permission decision on this branch is traceable.
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="live_slot",
        )
    elif in_restricted:
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="restricted_key",
        )
    elif in_live_session:
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="live_session",
        )
    else:  # is_channel_ns
        _sel().log_api_access(
            caller=sk,
            operation=operation,
            outcome="allowed",
            source="dashboard",
            resources="channel_namespace",
        )
    return None
