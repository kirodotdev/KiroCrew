"""Inbound Slack routing for code channels: the seams ``slack/events.py`` calls.

Three entry points, each a pass-through while ``slack.code_channels`` is off:

* :func:`inbound_gate` -- early in ``_route_message``, before the channel's
  activation mode is applied. For a message in one of OUR code channels it
  decides everything a code channel does differently: Slack's origin echo is
  dropped, a message that @-mentions only other people is left to them, the
  sender is authorized against the ORIGIN conversation, ``!stop`` stops the
  bound session, and anything else is DIRECTED -- answered without a mention.
* :func:`dispatch` -- at the point ``_route_message`` would start a turn. A
  DIRECTED message goes to the channel's bound dashboard session; a mention
  outside a code channel may open one (:func:`maybe_open_code_channel`).
* :func:`handle_session_stopped` -- Slack's native Stop button
  (``agent_session_stopped``) for a code channel stops the bound session.

Every allow/deny decision here writes an SEL event.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from kiro_crew.sel import sel
from kiro_crew.slack.code_channels import recognition
from kiro_crew.slack.code_channels.binding import (
    BindingUnavailable,
    bound_slot,
    create_bound_session,
    resolve_session_spec,
)
from kiro_crew.slack.code_channels.classifier import MAX_CONTEXT_MESSAGES, classify
from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.creation import (
    channel_name_for,
    is_directed_human_prompt,
    is_origin_echo,
    session_id_for,
)
from kiro_crew.slack.code_channels.models import ChannelInfo, CodeChannelApiError, CreateRequest
from kiro_crew.slack.code_channels.recognition import ChannelKind
from kiro_crew.slack.code_channels.settings import (
    code_channel_defaults,
    code_channels_on,
    live_config,
)
from kiro_crew.slack.code_channels.store import (
    ROUTE_CODE_CHANNEL,
    Binding,
    Route,
    store,
)

logger = logging.getLogger(__name__)

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")

NOT_AUTHORIZED_TEXT = (
    "⛔ You are not authorized to use this bot. Ask the owner to add you to the allowlist."
)


class Gate(enum.Enum):
    PASS = "pass"  # not a code-channel matter: the normal path runs unchanged
    DROP = "drop"  # handled (or deliberately ignored) here: the caller returns
    DIRECTED = "directed"  # ours to answer, no mention needed


@dataclass(frozen=True)
class InboundResult:
    gate: Gate
    binding: Binding | None = None
    info: ChannelInfo | None = None


PASS = InboundResult(Gate.PASS)
DROP = InboundResult(Gate.DROP)


def _self_user_id() -> str:
    from kiro_crew.slack.enterprise import validated_self_user_id

    return validated_self_user_id()


def _self_bot_id() -> str:
    from kiro_crew.slack.enterprise import validated_self_bot_id

    return validated_self_bot_id()


def _client(orch: Any) -> CodeChannelsClient | None:
    return CodeChannelsClient.from_slack(getattr(orch, "slack", None))


def _dashboard_state() -> Any | None:
    from kiro_crew.slack.handler import get_dashboard_state

    return get_dashboard_state()


def addressed_to_us(text: str, self_user_id: str) -> bool:
    """No mention at all, or a mention of us: directed. Others only: not."""
    mentioned = set(_MENTION.findall(text or ""))
    if not mentioned:
        return True
    return bool(self_user_id) and self_user_id in mentioned


def strip_leading_mention(text: str) -> str:
    return re.sub(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>\s*", "", text or "")


def _origin_of(channel: str, binding: Binding | None, info: ChannelInfo | None) -> str:
    if binding is not None and binding.origin_channel_id:
        return binding.origin_channel_id
    if info is not None and info.origin_channel_id:
        return info.origin_channel_id
    return channel


def origin_permits(cfg: Any, origin_channel: str) -> bool:
    """The origin conversation admits a code channel's turns.

    Not when the bot is off there, and not in a review-mode channel: review mode
    holds every reply for the owner's approval, and a code channel publishes
    its replies as they come, so it would bypass that review.
    """
    from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW

    try:
        activation = cfg.channel_config(origin_channel).activation
    except Exception:
        return False
    return activation not in (ACTIVATION_OFF, ACTIVATION_REVIEW)


def _audit(sender: str, operation: str, outcome: str, resources: str, error: str = "") -> None:
    sel().log_api_access(
        caller=sender or "unknown",
        operation=operation,
        outcome=outcome,
        source="slack",
        resources=resources,
        error=error,
    )


async def inbound_gate(
    orch: Any,
    event: dict[str, Any],
    *,
    text: str,
    sender_id: str,
    authorized: bool,
    from_trusted_bot: bool,
) -> InboundResult:
    """Decide what a code channel does with *event* (see the module docstring).

    Slack delivers a mention twice (``message`` and ``app_mention``), and this
    runs before the gateway's own dedup, so a verdict is remembered per message:
    the second delivery gets the same answer with none of the side effects (no
    lookup, no audit row, no second ``!stop`` or rejection).
    """
    channel = str(event.get("channel") or "")
    key = (channel, str(event.get("ts") or ""))
    if key[1] and key in _gate_seen:
        return _gate_seen[key]
    result = await _inbound_gate(
        orch,
        event,
        text=text,
        sender_id=sender_id,
        authorized=authorized,
        from_trusted_bot=from_trusted_bot,
    )
    if key[1]:
        _gate_seen[key] = result
        while len(_gate_seen) > _GATE_SEEN_MAX:
            _gate_seen.popitem(last=False)
    return result


_gate_seen: OrderedDict[tuple[str, str], InboundResult] = OrderedDict()
_GATE_SEEN_MAX = 2048


def _origin_binding(channel: str, info: ChannelInfo | None) -> Binding | None:
    """A stand-in binding carrying *info*'s origin, for the echo check."""
    if info is None or not (info.origin_channel_id and info.origin_ts):
        return None
    return Binding(
        channel_id=channel,
        team_id="",
        session_key="",
        anchor_ts="",
        origin_channel_id=info.origin_channel_id,
        origin_ts=info.origin_ts,
    )


async def _inbound_gate(
    orch: Any,
    event: dict[str, Any],
    *,
    text: str,
    sender_id: str,
    authorized: bool,
    from_trusted_bot: bool,
) -> InboundResult:
    cfg = live_config(orch)
    channel = str(event.get("channel") or "")
    if not code_channels_on(cfg) or not recognition.could_be_code_channel(channel):
        return PASS
    if from_trusted_bot or not is_directed_human_prompt(event):
        return PASS
    binding = store().get_binding(channel)
    kind, info = await recognition.classify_channel(_client(orch), channel, _self_user_id())
    if kind is not ChannelKind.OURS:
        if binding is not None and kind in (ChannelKind.ORDINARY, ChannelKind.OTHER_AGENT):
            # Slack does not list us as this channel's agent: stop answering here.
            logger.info("code channel %s is not assigned to us; unbinding", channel)
            await asyncio.to_thread(store().drop_binding, channel)
            _audit(
                sender_id, "slack.code_channel.unbind", "denied", f"channel={channel}", kind.value
            )
        # UNKNOWN (a failed lookup) and UNAVAILABLE are never read as ours: the
        # message takes the normal path, exactly as without code channels.
        return PASS
    # Slack's copy of the origin message is matched against the recorded origin,
    # or, before a binding exists (still being created, or opened from Slack's
    # UI), against the origin Slack reports for the channel.
    if is_origin_echo(event, binding or _origin_binding(channel, info)):
        return DROP
    if not addressed_to_us(text, _self_user_id()):
        return DROP
    origin = _origin_of(channel, binding, info)
    resources = f"channel={channel} origin={origin}"
    if not authorized:
        _audit(sender_id, "slack.code_channel.message", "denied", resources, "unauthorized sender")
        try:
            await orch.slack.post_ephemeral(channel, sender_id, NOT_AUTHORIZED_TEXT)
        except Exception:
            logger.debug("code channel: ephemeral rejection failed", exc_info=True)
        return DROP
    if not origin_permits(cfg, origin):
        _audit(
            sender_id,
            "slack.code_channel.message",
            "denied",
            resources,
            "origin activation does not admit a code channel",
        )
        return DROP
    _audit(sender_id, "slack.code_channel.message", "allowed", resources)
    if strip_leading_mention(text).strip().lower() == "!stop":
        await _stop_bound(
            orch, binding, sender_id, channel, source="slack.code_channel.stop_command"
        )
        return DROP
    return InboundResult(Gate.DIRECTED, binding=binding, info=info)


# ── Dispatch ──────────────────────────────────────────────────────────────


async def dispatch(
    orch: Any,
    result: InboundResult | None,
    *,
    event: dict[str, Any],
    clean_text: str,
    sender_id: str,
    was_mention: bool,
) -> bool:
    """Start the turn for a code-channel message; True when handled here.

    A mention may arrive as a plain ``message`` event first (the gateway keeps
    whichever delivery comes first), so "was it a mention" is read from the
    text as well as from the event type.
    """
    if result is not None and result.gate is Gate.DIRECTED:
        return await _dispatch_directed(orch, result, event, clean_text, sender_id)
    if result is None or result.gate is not Gate.PASS:
        return False
    self_user = _self_user_id()
    mentioned = bool(self_user) and self_user in _MENTION.findall(str(event.get("text") or ""))
    if was_mention or mentioned:
        return await maybe_open_code_channel(orch, event, clean_text, sender_id)
    return False


async def _post(orch: Any, channel: str, text: str, thread_ts: str | None) -> str:
    try:
        ts = await orch.slack.post_message(channel, text, thread_ts)
    except Exception:
        logger.debug("code channel post failed", exc_info=True)
        return ""
    return str(ts or "")


def _folder_name(state: Any, folder_id: str) -> str:
    for folder in getattr(state, "_folders", None) or ():
        if isinstance(folder, dict) and folder.get("id") == folder_id:
            return str(folder.get("name") or "")
    return ""


def _kickoff_text(title: str, state: Any, folder_id: str) -> str:
    where = _folder_name(state, folder_id) if folder_id else ""
    dashboard = f"the *{where}* folder of your dashboard" if where else "your dashboard"
    heading = f"🛠️ *{title}*\n" if title else ""
    return (
        f"{heading}This channel is one Kiro Crew session: reply here, no @mention "
        f"needed, and use Stop to interrupt me. It is also open in {dashboard}."
    )


#: One binder per channel at a time: a channel is bound to exactly one session.
_bind_locks: dict[str, asyncio.Lock] = {}
#: Origin session ids whose channel this process is creating right now. A message
#: in the new channel that lands before its binding waits for it, instead of
#: adopting the channel as one Slack's UI opened.
_creating: dict[str, asyncio.Event] = {}
CREATION_WAIT_SECS = 60.0


def _bind_lock(channel: str) -> asyncio.Lock:
    lock = _bind_locks.get(channel)
    if lock is None:
        lock = _bind_locks[channel] = asyncio.Lock()
    return lock


async def _bind_channel(
    orch: Any,
    *,
    channel: str,
    team_id: str,
    title: str,
    origin_channel_id: str,
    origin_ts: str,
    origin_thread_ts: str,
    created_by_us: bool,
    replace: bool = False,
) -> Binding:
    """Post the kickoff, create the dashboard session and record the binding.

    Callers hold :func:`_bind_lock`. Without *replace* the first binding wins:
    one already stored is returned and nothing new is created.
    """
    existing = store().get_binding(channel)
    if existing is not None and not replace:
        return existing
    state = _dashboard_state()
    if state is None:
        raise BindingUnavailable("no dashboard")
    cfg = live_config(orch)
    spec = await resolve_session_spec(
        state, cfg, code_channel_defaults(cfg), origin_thread_ts=origin_thread_ts
    )
    anchor = await _post(orch, channel, _kickoff_text(title, state, spec.folder_id), None)
    if not anchor:
        raise BindingUnavailable("kickoff post failed")
    slot = await create_bound_session(
        state, spec, title=title or "Slack code channel", channel_id=channel, anchor_ts=anchor
    )
    binding = Binding(
        channel_id=channel,
        team_id=team_id,
        session_key=slot.key,
        anchor_ts=anchor,
        project_dir=spec.project_dir,
        origin_channel_id=origin_channel_id,
        origin_ts=origin_ts,
        created_by_us=created_by_us,
        title=title,
    )
    write = store().replace_binding if replace else store().put_binding
    return await asyncio.to_thread(write, binding)


async def _route_to_slot(orch: Any, binding: Binding, text: str, sender_id: str) -> bool:
    from kiro_crew.slack.handler import maybe_route_linked_thread

    return await maybe_route_linked_thread(
        text, binding.session_key, sender_id, binding.channel_id, orch.slack, binding.anchor_ts
    )


UNROUTED_TEXT = (
    "I could not reach this channel's session just now. Open it in your dashboard, "
    "or try again in a moment."
)
UNSUPPORTED_COMMAND_TEXT = (
    "That command does not run in a code channel. Use it in the channel's dashboard session."
)


async def _wait_for_creation(event: dict[str, Any], info: ChannelInfo | None) -> None:
    """If this process is still creating the channel, wait until it is bound."""
    if info is None or not (info.origin_channel_id and info.origin_ts):
        return
    sid = session_id_for(
        _self_bot_id(), str(event.get("team") or ""), info.origin_channel_id, info.origin_ts
    )
    pending = _creating.get(sid)
    if pending is not None:
        try:
            await asyncio.wait_for(pending.wait(), timeout=CREATION_WAIT_SECS)
        except asyncio.TimeoutError:
            logger.info("code channel creation still running after %ss", CREATION_WAIT_SECS)


async def _dispatch_directed(
    orch: Any, result: InboundResult, event: dict[str, Any], clean_text: str, sender_id: str
) -> bool:
    """Run a DIRECTED message in the channel's session; always handled here.

    A message the gate admitted is never handed back to the normal path, which
    would answer it in a thread session the channel does not belong to; when it
    cannot be delivered, the sender is told so in the channel.
    """
    channel = str(event.get("channel") or "")
    if _dashboard_state() is None:
        await _post(orch, channel, UNROUTED_TEXT, None)
        return True
    if store().get_binding(channel) is None:
        await _wait_for_creation(event, result.info)
    try:
        async with _bind_lock(channel):
            binding = store().get_binding(channel)
            if binding is None:
                # A code channel Slack's UI opened with us as its agent: adopt it.
                info = result.info
                binding = await _bind_channel(
                    orch,
                    channel=channel,
                    team_id=str(event.get("team") or ""),
                    title=(info.name if info else "") or "",
                    origin_channel_id=info.origin_channel_id if info else "",
                    origin_ts=info.origin_ts if info else "",
                    origin_thread_ts="",
                    created_by_us=False,
                )
                _audit(sender_id, "slack.code_channel.adopt", "allowed", f"channel={channel}")
            elif bound_slot(_dashboard_state(), binding) is None:
                # The session was closed in the dashboard: open a fresh one.
                binding = await _bind_channel(
                    orch,
                    channel=channel,
                    team_id=binding.team_id,
                    title=binding.title,
                    origin_channel_id=binding.origin_channel_id,
                    origin_ts=binding.origin_ts,
                    origin_thread_ts="",
                    created_by_us=binding.created_by_us,
                    replace=True,
                )
    except BindingUnavailable as exc:
        logger.info("code channel %s: no session to route to (%s)", channel, exc)
        await _post(orch, channel, UNROUTED_TEXT, None)
        return True
    except Exception:
        logger.exception("code channel %s: binding a session failed", channel)
        await _post(orch, channel, UNROUTED_TEXT, None)
        return True
    if not await _route_to_slot(orch, binding, clean_text, sender_id):
        await _post(orch, channel, UNSUPPORTED_COMMAND_TEXT, None)
    return True


# ── Creation from a mention ───────────────────────────────────────────────


async def _thread_context(
    orch: Any, channel: str, thread_ts: str, msg_ts: str
) -> list[dict[str, str]]:
    """The origin thread's last few messages before this one, newest first.

    For the CLASSIFIER only, whose single output is stay-or-open: the text is
    redacted there and framed as data.
    """
    if not thread_ts:
        return []
    fetch = getattr(orch.slack, "fetch_thread_replies", None)
    if fetch is None:
        return []
    try:
        messages = await fetch(channel, thread_ts, limit=50, warn_on_pagination=False)
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("ts") == msg_ts:
            continue
        user = str(message.get("user") or message.get("bot_id") or "")[:64]
        out.append({"author": user, "text": str(message.get("text") or "")})
        if len(out) >= MAX_CONTEXT_MESSAGES:
            break
    return out


async def _seed_context(
    orch: Any, channel: str, thread_ts: str, msg_ts: str, session_key: str
) -> str:
    """The origin thread's earlier replies for the first turn, as UNTRUSTED data.

    Built by the same reader every Slack thread turn uses: each reply redacted,
    one matching a prompt-injection pattern withheld and audited, the rest
    fenced as data, never instructions.
    """
    if not thread_ts or thread_ts == msg_ts:
        return ""
    from kiro_crew.slack.thread_replies import replies_since_last_turn

    replies = await replies_since_last_turn(
        orch.slack, channel, thread_ts, msg_ts, session_key=session_key, first_turn=True
    )
    if not replies.text:
        return ""
    return (
        "[SLACK THREAD REPLIES — UNTRUSTED DATA]\n" f"{replies.text}\n" "[END SLACK THREAD REPLIES]"
    )


def _seed_prompt(request: str, origin_channel: str, context: str) -> str:
    lines = [request.strip(), "", "[Slack code channel]"]
    lines.append(
        f"This session is a Slack code channel opened from a thread in <#{origin_channel}>. "
        "Your replies are posted to that channel."
    )
    if context:
        lines.extend(["Earlier replies in that thread:", context])
    return "\n".join(lines)


async def maybe_open_code_channel(
    orch: Any, event: dict[str, Any], clean_text: str, sender_id: str
) -> bool:
    """Open a code channel for a mention that asks for multi-step work.

    True when a channel was opened (or had already been opened for this very
    message) and the work moved there; False to answer in place as usual.
    """
    cfg = live_config(orch)
    channel = str(event.get("channel") or "")
    msg_ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or "")
    team_id = str(event.get("team") or "")
    if not code_channels_on(cfg) or not is_directed_human_prompt(event):
        return False
    if store().get_binding(channel) is not None:
        return False  # already a code channel: dispatch() owns it
    if not origin_permits(cfg, channel):
        return False  # a review-mode channel keeps its review of every reply
    state = _dashboard_state()
    client = _client(orch)
    if state is None or client is None or not clean_text.strip():
        return False

    session_id = session_id_for(_self_bot_id(), team_id, channel, msg_ts)
    route = store().get_route(session_id)
    if route is None or not route.decision:
        context = await _thread_context(orch, channel, thread_ts, msg_ts)
        decision = await classify(getattr(orch, "sessions", None), clean_text, context)
        route = await asyncio.to_thread(
            store().record_decision,
            Route(
                session_id=session_id,
                origin_channel_id=channel,
                origin_ts=msg_ts,
                team_id=team_id,
                decision=decision.route,
                title=decision.title,
                created_at=time.time(),
            ),
        )
    if route.decision != ROUTE_CODE_CHANNEL:
        return False
    if route.channel_id and store().get_binding(route.channel_id) is not None:
        return True  # a re-delivered event: this message already has its channel
    if session_id in _creating:
        return True  # this message's channel is being opened right now

    resources = f"origin={channel} session_id={session_id[:12]}"
    request = CreateRequest(
        name=channel_name_for(route.title),
        session_id=session_id,
        origin_channel_id=channel,
        origin_message_ts=msg_ts,
        team_id=team_id,
    )
    done = _creating[session_id] = asyncio.Event()
    try:
        try:
            try:
                new_channel = await client.create(request)
            except CodeChannelApiError as exc:
                if not exc.maybe_applied:
                    raise
                # The create may have landed. Same session_id = same channel, so
                # asking once more reconciles instead of duplicating.
                new_channel = await client.create(request)
        except CodeChannelApiError as exc:
            _audit(sender_id, "slack.code_channel.create", "error", resources, exc.code)
            logger.info("code channel not opened (%s); answering in place", exc.code)
            return False
        _audit(
            sender_id, "slack.code_channel.create", "allowed", f"{resources} channel={new_channel}"
        )
        await asyncio.to_thread(store().record_destination, session_id, new_channel)
        try:
            async with _bind_lock(new_channel):
                binding = await _bind_channel(
                    orch,
                    channel=new_channel,
                    team_id=team_id,
                    title=route.title,
                    origin_channel_id=channel,
                    origin_ts=msg_ts,
                    origin_thread_ts=thread_ts or msg_ts,
                    created_by_us=True,
                )
        except Exception:
            logger.exception(
                "code channel %s opened but its session could not be bound", new_channel
            )
            await _post(
                orch,
                channel,
                f"I opened <#{new_channel}> but could not start its session. "
                "Mention me there to retry.",
                thread_ts or msg_ts,
            )
            return True
    finally:
        done.set()
        _creating.pop(session_id, None)
    context_text = await _seed_context(orch, channel, thread_ts, msg_ts, binding.session_key)
    if not await _route_to_slot(
        orch, binding, _seed_prompt(clean_text, channel, context_text), sender_id
    ):
        logger.warning("code channel %s: first turn could not be routed", new_channel)
    await _post(
        orch, channel, f"Opened <#{new_channel}> for this. I'll work there.", thread_ts or msg_ts
    )
    return True


# ── Stop ──────────────────────────────────────────────────────────────────


async def _stop_bound(
    orch: Any,
    binding: Binding | None,
    sender_id: str,
    channel: str,
    *,
    source: str,
    say_idle: bool = True,
) -> str:
    state = _dashboard_state()
    slot = bound_slot(state, binding) if binding is not None else None
    if state is None or slot is None or not getattr(slot, "running", False):
        _audit(sender_id, source, "allowed", f"channel={channel} result=idle")
        # Nothing to stop, but Slack never clears "processing" by itself.
        from kiro_crew.slack.code_channels.status import set_status

        await set_status(channel, "active")
        if say_idle:
            await _post(orch, channel, "Nothing running.", None)
        return "idle"
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    result = await stop_slot_turn(state, slot, source="slack", escalate=False)
    info = str(result.get("info") or "stopping")
    _audit(sender_id, source, "allowed", f"channel={channel} slot={slot.key} result={info}")
    return info


async def handle_session_stopped(orch: Any, event: dict[str, Any], user: str) -> bool:
    """Slack's Stop button in a code channel; True when the channel is ours.

    Registered with ``agent_sessions.session_events.register_channel_stop_handler``,
    which has already checked the workspace, deduplicated the event and
    audited the presser; this adds the code channel's own rule (the ORIGIN
    conversation must still admit the bot) before stopping the bound session.
    """
    channel = str(event.get("channel") or "")
    if event.get("thread_ts"):
        return False  # a thread session, not a code channel
    if not code_channels_on(live_config(orch)):
        return False
    binding = store().get_binding(channel)
    if binding is None:
        return False
    from kiro_crew.slack.handler import is_allowed_user

    if not is_allowed_user(user):
        _audit(
            user, "slack.code_channel.stop", "denied", f"channel={channel}", "unauthorized sender"
        )
        return True
    if not origin_permits(live_config(orch), _origin_of(channel, binding, None)):
        _audit(
            user, "slack.code_channel.stop", "denied", f"channel={channel}", "origin activation=off"
        )
        return True
    await _stop_bound(
        orch, binding, user, channel, source="slack.code_channel.stop", say_idle=False
    )
    return True
