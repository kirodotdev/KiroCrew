"""``POST /api/member/self/message``: a crewmate sends a message to another crewmate.

The route behind ``message_mate`` (``kirocrew-mate``). Design:
the RFC *mate messaging* (``rfc-mate-messaging``). It is MCP-only, under the
strict ``/api/member/self`` prefix (``dashboard.server_internal_paths``).

The sender is the crewmate whose own pinned thread the verified caller's slot
is (:func:`handlers.guide._resolve_agent_caller`, then the thread binding),
never a name from the body. This path is separate from session control: it
does not use ``session_send``, and it does not change the member-thread,
workspace or creator-ownership checks there.

Checks, in order: the caller, the ``agent.mate_messaging`` switch (read on each
call), the body, the target thread, then the rules in
:func:`kiro_crew.dashboard.mate_peer_messages.plan_send` (allowed pair, hop,
per-turn cap, rate, queue). Each attempt is written to the SEL audit log, and
each accepted message to the member event log of both crewmates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.dashboard.guide_runs import GuideError
from kiro_crew.dashboard.handlers.guide import (
    _audit,
    _deny,
    _json_body,
    _refusal,
    _resolve_agent_caller,
)
from kiro_crew.dashboard.handlers.member_self import _caller_member

logger = logging.getLogger(__name__)

#: The operation name in the SEL audit log.
OP_MESSAGE_MATE = "member.message_mate"

_BODY_FIELDS = frozenset({"to", "text"})


def _sender_slug(member: str, slot_key: str) -> str | None:
    """*member*'s slug when *slot_key* is the thread its binding names. Blocking IO."""
    slug = members_mod.member_slug(member)
    binding = members_mod.read_dm_binding(slug)
    if not binding or binding.get("member") != member or binding.get("slot_key") != slot_key:
        return None
    return slug


def _target_thread(target: str) -> tuple[str, str] | None:
    """``(slug, thread slot key)`` of configured crewmate *target*, else ``None``. Blocking IO."""
    from kiro_crew.config.loader import KiroCrewConfig

    if target == "default":
        return None
    config = KiroCrewConfig.load()
    if target not in config.agents:
        return None
    slug = members_mod.member_slug(target, config)
    binding = members_mod.read_dm_binding(slug)
    if not binding or binding.get("member") != target:
        return None
    key = binding.get("slot_key")
    return (slug, key) if isinstance(key, str) and key else None


def _log_events(sender: tuple[str, str], target: tuple[str, str], hop: int, chars: int) -> None:
    """One ``member/peer_message`` event in each crewmate's log. Blocking IO."""
    import time

    from kiro_crew import eventlog_hooks
    from kiro_crew.eventlog.types import MEMBER_PEER_MESSAGE

    ts = time.time()
    (sender_slug, sender_name), (target_slug, target_name) = sender, target
    eventlog_hooks.emit(
        sender_slug,
        sender_name,
        MEMBER_PEER_MESSAGE,
        {"ts": ts, "direction": "out", "peer": target_name, "hop": hop, "chars": chars},
    )
    eventlog_hooks.emit(
        target_slug,
        target_name,
        MEMBER_PEER_MESSAGE,
        {"ts": ts, "direction": "in", "peer": sender_name, "hop": hop, "chars": chars},
    )


def _refuse(sk: str, status: int, code: str, message: str) -> web.Response:
    _audit(sk, OP_MESSAGE_MATE, "denied", code)
    return _deny(status, code, message)


async def api_member_self_message(request: web.Request) -> web.Response:
    """POST /api/member/self/message — ``{"to", "text"}`` → the queued message.

    The answer carries ``id``, ``to``, ``hop``, ``max_hops`` and ``queued``
    (the target thread was busy, so the message waits for its turn to end).
    """
    from kiro_crew.dashboard import mate_peer_messages as peers
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread

    try:
        slot_key, sk = await _resolve_agent_caller(request, OP_MESSAGE_MATE)
        body = await _json_body(request)
    except GuideError as exc:
        return _refusal(exc)
    state = request.app["state"]
    who = _caller_member(request, slot_key)
    if who is None:
        return _refuse(
            sk, 403, "not_a_crewmate", "only a crewmate, in its own chat, can message a crewmate"
        )
    sender = who[0]
    enabled, pairs = await asyncio.to_thread(peers.read_settings)
    if not enabled:
        return _refuse(
            sk,
            403,
            "mate_messaging_off",
            "crewmate messages are off; only the user can turn them on (agent.mate_messaging)",
        )
    unknown = sorted(set(body) - _BODY_FIELDS)
    if unknown:
        return _refuse(sk, 400, "invalid_body", f"unknown field '{unknown[0][:64]}'")
    to, text = body.get("to"), body.get("text")
    if not isinstance(to, str) or not to.strip():
        return _refuse(sk, 400, "invalid_target", "to must name a crewmate")
    if not isinstance(text, str):
        return _refuse(sk, 400, "invalid_text", "text must be a string")
    too_long = f"a crewmate message is at most {peers.MAX_MESSAGE_CHARS} characters"
    if len(text) > peers.MAX_MESSAGE_CHARS:
        return _refuse(sk, 400, "message_too_long", too_long)
    # One form for the rows and for the wake, so the user sees what the target reads.
    clean = peers.peer_text(text)
    if not clean:
        return _refuse(sk, 400, "empty_message", "the message is empty")
    if len(clean) > peers.MAX_MESSAGE_CHARS:
        return _refuse(sk, 400, "message_too_long", too_long)
    target = to.strip()
    if target == sender:
        return _refuse(sk, 400, "self_target", "a crewmate cannot send a message to itself")
    sender_slot = state.get_slot(slot_key)
    if sender_slot is None:
        return _refuse(sk, 409, "no_live_slot", "this session is not open in a dashboard tab")
    # The pair goes first, so a sender learns nothing about a crewmate it may not message.
    if not peers.pair_allowed(state, sender_slot, sender, target, pairs):
        return _refuse(
            sk,
            403,
            "pair_not_allowed",
            f"the user has not let {sender} start a message to {target[:128]} (no "
            f'"{sender}>{target[:128]}" entry in agent.mate_messaging_allow)',
        )

    def _resolve() -> tuple[str | None, tuple[str, str] | None]:
        return _sender_slug(sender, slot_key), _target_thread(target)

    try:
        sender_slug, found = await asyncio.to_thread(_resolve)
    except (members_mod.MemberSlugError, OSError):
        sender_slug, found = None, None
    if sender_slug is None:
        return _refuse(sk, 403, "not_a_crewmate", "this chat is not the crewmate's own thread")
    if state.get_slot(slot_key) is not sender_slot:
        return _refuse(sk, 409, "session_rebound", "this chat changed while the message was sent")
    target_slot = state.get_slot(found[1]) if found else None
    if (
        found is None
        or target_slot is None
        or not is_crewmate_thread(target_slot)
        or target_slot.agent != target
        or getattr(target_slot, "is_closing", False)
    ):
        return _refuse(
            sk,
            404,
            "target_not_found",
            f"no crewmate named {target[:128]} has an open thread on this gateway",
        )
    # From here to commit_send nothing awaits, so the rules see the state they change.
    queued = not peers.thread_idle(target_slot)
    try:
        hop = peers.plan_send(state, sender_slot, sender, target, target_slot.key, pairs)
        message_id = peers.commit_send(state, sender_slot, target_slot, sender, target, clean, hop)
    except peers.PeerRefusal as exc:
        return _refuse(sk, exc.status, exc.code, exc.message)
    try:
        await asyncio.to_thread(
            _log_events, (sender_slug, sender), (found[0], target), hop, len(clean)
        )
    except Exception:  # noqa: BLE001 - the event log never undoes a sent message
        logger.warning("crewmate message %s: event log write failed", message_id, exc_info=True)
    _audit(sk, OP_MESSAGE_MATE, "ok", f"to={target[:128]} hop={hop}")
    result: dict[str, Any] = {
        "ok": True,
        "id": message_id,
        "to": target,
        "hop": hop,
        "max_hops": peers.MAX_HOPS,
        "queued": queued,
    }
    return web.json_response(result)
