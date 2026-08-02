"""Shared channel turn pipeline — one copy of the dispatch skeleton.

This module owns the sequence every non-Slack channel dispatcher runs around
:class:`TurnDriver`:

    governance gate
    -> renderer.on_turn_start()          (typing indicator before cold start)
    -> sessions.get_or_create + set_channel
    -> publish_turn_identity
    -> ctx_builder.build_message         (off-loop, embeds block)
    -> TurnDriver.run                    (shared redaction + approval ladder)
    -> post-turn: record_success, persist, threshold notice, SEL audit
                                         (each guarded independently)
    -> finally: renderer.close() + release (release gated on acquire)

What stays per-channel is what actually differs between them: the wire
protocol, event normalization, ``authorize()`` semantics, rendering, command
vocabulary, and the ack strings. Channels inject those through
:class:`ChannelTurn` rather than subclassing, so a capability this protocol
lacks widens the protocol once instead of forking the pipeline.

Dependency direction is ``<channel> -> messaging`` (never the reverse), so this
module must not import any channel package.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging.driver import TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


@dataclass
class ChannelTurn:
    """Everything the pipeline needs that varies per channel.

    ``persist`` runs in a worker thread (it does blocking history I/O), so it
    must be a plain sync callable. ``notice`` and ``renderer`` are async.
    """

    channel_type: str
    """Governance member name, e.g. ``"weixin"``. Gates every inbound message.

    Invariant: this MUST equal the first ``:``-segment of ``session_key`` —
    the surface name is the routing authority for governance and (future)
    control-plane operations. Holds by construction today because every
    adopter builds its key via ``build_dm_session_key(channel_type, ...)``.
    """

    session_key: str
    """The session address. OPAQUE to this pipeline: it is passed through to
    ``sessions.*`` verbatim and never parsed, split, or rebuilt here. Keys are
    constructed channel-side via :func:`kiro_crew.messaging.link.build_dm_session_key`
    (``{surface}:{agent}:{chat_type}:{scope}[:genN]``). Keeping the pipeline
    address-agnostic is what lets the address grammar evolve (deeper scope
    paths, new surfaces) without touching dispatch.
    """

    conversation_id: str
    """Stable identity of the conversation on its transport (e.g.
    ``"weixin:{user_id}"``), used for session attribution/UI. Feeds the
    legacy ``channel_id=`` kwarg of ``sessions.get_or_create`` /
    ``set_channel`` — the old name survives at that API boundary only.
    Distinct from BOTH ``channel_type`` (the transport) and the app-platform
    ``channel:`` concept.
    """

    agent: str
    user_text: str
    renderer: Any
    """A :class:`kiro_crew.messaging.renderer.Renderer`."""

    approval_mode: str
    decider: Optional[Any] = None
    """``None`` for channels with no interactive buttons (deny-by-default for
    INTERACTIVE mode; ``auto``/``trust`` still work)."""

    persist: Optional[Callable[[str, str, bool], None]] = None
    """``(user_text, reply_text, is_new) -> None``, called off the event loop."""

    notice: Optional[Callable[[str, Any], Awaitable[None]]] = None
    """``(session_key, provider) -> None`` post-turn threshold handling."""

    after_persist: Optional[Callable[[], Awaitable[None]]] = None
    """Optional loop-side callback after persistence, such as dashboard surfacing."""

    audit_caller: str = ""
    """SEL audit caller label; defaults to ``<channel_type>:unknown``."""


async def inbound_permitted(channel_type: str) -> bool:
    """Per-message governance gate.

    Rechecked on every message (not just at connect) so a host-profile deny
    added while the transport is live stops dispatch without a restart. The
    pipeline calls this itself, so a channel cannot forget it.
    """
    if await channel_inbound_permitted(channel_type):
        return True
    logger.info("%s inbound dropped: denied by channels governance policy", channel_type)
    return False


def build_tool_gate(ctx_builder: Any, *, session_key: str, agent: str) -> Callable[[Any], str]:
    """PreToolUse security gate, channel-neutral (off ``ctx_builder.hooks``).

    Sensitive-path keystone + governance ceiling + deny-list. Returns ``"deny"``
    (un-overridable), ``"auto_approve"``, or ``""`` (passthrough). Built here so
    no channel package needs to import ``kiro_crew.slack``.
    """

    def _tool_gate(event: Any) -> str:
        result = ctx_builder.hooks.on_tool_call(
            getattr(event, "title", "") or "",
            session_key=session_key,
            agent=agent,
            tool_kind=getattr(event, "tool_kind", "") or "",
            raw_params=getattr(event, "raw_tool_params", None),
            command=getattr(event, "shell_command", None),
            is_shell=bool(getattr(event, "is_shell", False)),
        )
        if result.action == TOOL_DENY:
            return "deny"
        if result.action == TOOL_AUTO_APPROVE:
            return "auto_approve"
        return ""

    return _tool_gate


def build_auto_approve(ctx_builder: Any) -> Callable[[str], bool]:
    """Preserve the ``auto_approve_subagent_spawn`` hook for ``spawn_run``."""

    def _auto_approve(title: str) -> bool:
        return bool(
            ctx_builder
            and ctx_builder.hooks
            and ctx_builder.hooks.auto_approve_subagent_spawn
            and title == "spawn_run"
        )

    return _auto_approve


async def drive_turn(turn: ChannelTurn, *, sessions: Any, ctx_builder: Any) -> None:
    """Run one authorized inbound message end to end.

    Everything acquire-dependent runs INSIDE the try so ``finally`` always
    finalizes the turn (``renderer.close``), even when ``get_or_create`` raises
    on a cold-start failure. ``release()`` is gated on ``_acquired`` so a
    semaphore that was never held is never released.
    """
    renderer = turn.renderer
    session_key = turn.session_key
    _acquired = False
    # Enforced governance backstop. Channels SHOULD gate earlier (before any
    # side effect such as a command ack or a generation bump — see the weixin
    # dispatcher, which checks before parse_command), but the pipeline rechecks
    # so an adopter that forgets cannot execute a policy-denied turn. Denied
    # messages are dropped silently, before the typing indicator and before any
    # session is acquired.
    if not await inbound_permitted(turn.channel_type):
        return
    try:
        # Typing indicator first (before the potentially slow cold start);
        # on_turn_start is idempotent so the driver's later call no-ops.
        await renderer.on_turn_start()
        provider, is_new, resumed = await sessions.get_or_create(
            session_key, agent=turn.agent, channel_id=turn.conversation_id
        )
        _acquired = True
        if is_new:
            await sessions.set_channel(session_key, turn.conversation_id)
        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity.
        await publish_turn_identity(sessions, session_key)
        # Off-loop: build_message embeds the episodic query (blocking urllib).
        full_message, _ = await run_in_embed_pool(
            ctx_builder.build_message,
            turn.user_text,
            is_new,
            session_key,
            channel_id=turn.conversation_id,
            agent=turn.agent,
            resumed=resumed,
            runtime_source=turn.channel_type,
        )

        driver = TurnDriver(
            provider,
            renderer,
            approval_mode=turn.approval_mode,
            decider=turn.decider,
            auto_approve_tool=build_auto_approve(ctx_builder),
            tool_gate=build_tool_gate(ctx_builder, session_key=session_key, agent=turn.agent),
        )
        accumulated = await driver.run(full_message)

        # ── Post-turn bookkeeping. Each step is guarded independently so a
        # failure here cannot fall through to the except and re-record a turn
        # that actually succeeded. ──
        try:
            sessions.record_success(session_key)
        except Exception:
            logger.warning(
                "%s: record_success failed session=%s",
                turn.channel_type,
                session_key,
                exc_info=True,
            )
        if turn.persist is not None:
            try:
                await asyncio.to_thread(turn.persist, turn.user_text, accumulated, is_new)
            except Exception:
                logger.warning(
                    "%s: persist_turn failed session=%s",
                    turn.channel_type,
                    session_key,
                    exc_info=True,
                )
        if is_new and turn.after_persist is not None:
            try:
                await turn.after_persist()
            except Exception:
                logger.warning(
                    "%s: post-persist callback failed session=%s",
                    turn.channel_type,
                    session_key,
                    exc_info=True,
                )
        if turn.notice is not None:
            try:
                await turn.notice(session_key, provider)
            except Exception:
                logger.warning(
                    "%s: maybe_notice failed session=%s",
                    turn.channel_type,
                    session_key,
                    exc_info=True,
                )
        try:
            sel().log_api_access(
                caller=turn.audit_caller or f"{turn.channel_type}:unknown",
                operation="transport_dispatch.handle",
                outcome="success",
                source=turn.channel_type,
                resources=f"session={session_key}",
            )
        except Exception:
            logger.debug("%s: success audit failed", turn.channel_type, exc_info=True)
    except Exception:
        logger.exception("%s transport_dispatch: error handling message", turn.channel_type)
        if _acquired:
            await sessions.record_failure(session_key)
    finally:
        # Always finalize the turn, even if get_or_create raised before the
        # semaphore was held. Only release if we actually acquired it.
        #
        # ``renderer.close()`` is best-effort and must NEVER prevent the release
        # below. A renderer that fails to finalize -- a malformed vendor
        # response, a dropped socket mid-flush -- would otherwise leave the
        # semaphore held with no path to give it back. Because the semaphore is
        # keyed by SESSION, that does not just lose this turn: every later
        # message for that conversation blocks forever, and any queued turn
        # never drains. The channel looks permanently busy until the gateway
        # restarts.
        #
        # Discord already guards this in its own dispatcher, which is how the
        # hazard was found; the guard belongs here so every channel on the
        # shared pipeline inherits it instead of re-deriving it.
        try:
            await renderer.close()
        except Exception:
            logger.warning(
                "%s: renderer.close failed session=%s",
                turn.channel_type,
                session_key,
                exc_info=True,
            )
        if _acquired:
            sessions.release(session_key)
