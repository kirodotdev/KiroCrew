"""Fire one unattended nudge turn into a channel session.

A Slack thread directly, or a dispatcher-routed DM (Discord, Webex) through the
shared DM adapter, with the monitor accounting a structured completion reports.

Orchestrator fields read: ``_cfg``, ``autonudge_svc``, ``conv_log``, ``ctx_builder``,
``dashboard_state``, ``sessions``, ``slack``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    import contextlib
    import time

    from kiro_crew.slack.gateway import (
        APPROVAL_INTERACTIVE,
        STRUCTURAL_TERMINAL_REASON,
        AgentTurnUsage,
        AutoNudgeService,
        GatewayOrchestrator,
        InboundMessage,
        LLMEvent,
        MonitorActionDisposition,
        MonitorCompletionHook,
        MonitorDispatchResult,
        NudgeLoop,
        SessionBusyError,
        SessionClosingError,
        SilentRenderer,
        ToolApprovalPolicy,
        TurnDriver,
        _get_agent_for_session,
        _persist_turn_row,
        build_directive_consumer,
        build_tool_gate,
        channel_inbound_permitted,
        compose_nudge_body,
        disposition_for_stop_reason,
        is_monitor_completion_evidence,
        is_thread_incognito,
        is_thread_temporary,
        logger,
        nudge_cycle_header,
        provider_last_turn_usage,
        redact_credentials,
        redact_exfiltration_urls,
        render_for_slack,
        run_in_embed_pool,
        save_conversation_turn_off_loop,
        session_store_for_turn,
        stream_and_collect,
        turn_ceiling,
    )


# Per-turn hard deadline for an unattended AutoNudge turn in a channel session
# (Slack/Discord babysit loops). Mirrors HEARTBEAT_TASK_TIMEOUT_SECS / cron's
# _JOB_TIMEOUT_SECS: no human is present, so the turn MUST be bounded.
_NUDGE_TURN_TIMEOUT = 1800.0  # 30 min


def _delivery_result(
    wake_message: str | None,
    result: MonitorDispatchResult,
) -> bool | MonitorDispatchResult:
    """Keep legacy bool semantics while structured delivery stays typed."""
    if wake_message is not None:
        return result
    return result is MonitorDispatchResult.DISPATCHED


@dataclass(frozen=True)
class _DmDispatchAdapter:
    """What ONE dispatcher-routed DM channel supplies to the shared fire path.

    A channel whose nudge is delivered by synthesizing an inbound message and
    handing it to that channel's real dispatcher differs from its siblings in
    five places and nowhere else. Everything around those five -- the guard
    ladder, the envelope, the turn timeout, when a loop is retired, and how a
    result is spelled -- is the same reasoning for every such channel, so it
    lives once in :meth:`GatewayOrchestrator._fire_dm_nudge` instead of being
    re-derived per channel.

    ``channel`` keys ``dashboard_state.channel_transports`` and names the
    channel in logs. ``supports_monitor`` says whether the channel can carry a
    structured monitor wake: a channel without one is only ever asked for a
    plain nudge, so it never sees a ``wake_message``. ``authorize`` is the
    fire-time allow-list re-check, which each channel spells against a
    different object. ``resolve_conversation`` answers where the synthetic turn
    lands. ``build_inbound`` mints that channel's own inbound type.

    The three callables take the transport and dispatcher rather than closing
    over them because a channel's transport is resolved per fire, not once at
    construction: a gateway can start, stop and restart one channel while a
    loop stays armed across all of it.
    """

    channel: str
    supports_monitor: bool
    authorize: Callable[[Any, Any, str], bool]
    resolve_conversation: Callable[[Any, Any, str, str], Awaitable[str]]
    build_inbound: Callable[[str, str, str], Any]


async def _fire_slack_nudge(
    self: GatewayOrchestrator, loop: NudgeLoop, wake_message: str | None = None
) -> bool | MonitorDispatchResult:
    """Drive one unattended nudge turn in a Slack thread session.

    Mirrors the subagent-completion Slack injection: acquire the session,
    run the turn with auto-approval, post the reply into the originating
    thread, persist for dashboard replay. Returns True when the turn ran;
    False on skip (busy/unroutable/error) — the AutoNudge service re-arms
    with backoff on False.

    Everything after the provider returns -- the completion report, the usage
    row, the session release, the thread post and the replay save -- is one
    section registered with the AutoNudge shutdown drain, so a gateway stop
    that lands there waits for it instead of cancelling a reply the provider
    already produced. The provider wait itself stays cancellable.
    """
    with contextlib.ExitStack() as bookkeeping:
        return await self._fire_slack_inner_nudge(loop, wake_message, bookkeeping)


def _bookkeeping_section(
    self: GatewayOrchestrator,
) -> contextlib.AbstractContextManager[None]:
    """A section the AutoNudge shutdown drain waits for instead of cancelling.

    Only a running service has a drain to register with; without one the
    section is a no-op.
    """
    svc = self.autonudge_svc
    if not isinstance(svc, AutoNudgeService):
        return contextlib.nullcontext()
    return svc.drain_section()


async def _fire_slack_inner_nudge(
    self: GatewayOrchestrator,
    loop: NudgeLoop,
    wake_message: str | None,
    bookkeeping: contextlib.ExitStack,
) -> bool | MonitorDispatchResult:
    """The body of :meth:`_fire_slack_nudge`; *bookkeeping* holds its drain section."""
    key = loop.slot_key
    if self.sessions is None or self.slack is None:
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
    if wake_message is not None and not await channel_inbound_permitted("slack"):
        logger.warning(
            "AutoNudge: Slack inbound policy denied structured wake for loop %s",
            loop.id,
        )
        return MonitorDispatchResult.UNAVAILABLE
    if self.sessions.is_busy(key):
        logger.info("AutoNudge skip: slack session %s busy (loop %s)", key, loop.id)
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
    channel = self.sessions.get_channel(key)
    thread_ts = self.sessions.get_thread(key)
    if not thread_ts and key.startswith("slack:"):
        # Canonical keys embed the thread root ts.
        thread_ts = key.split(":", 1)[1]
    if not channel:
        logger.warning("AutoNudge: slack session %s unroutable — removing loop %s", key, loop.id)
        if self.autonudge_svc and wake_message is None:
            await self.autonudge_svc.remove(loop.id, stop_reason="slack_unroutable")
        return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
    if wake_message is None:
        # Snapshot message, sentinel AND config generation TOGETHER, before
        # the compose_nudge_body() await, so a concurrent PATCH during that
        # suspension cannot pair the old message with a new generation (which
        # would make the malformed verdict match the reconfigured loop and
        # wrongly stop it). The fence below compares this captured generation.
        _fired_message = loop.message
        _fired_sentinel = loop.stop_sentinel_path
        _fired_generation = loop.config_generation
        msg_body = await compose_nudge_body(_fired_message, _fired_sentinel, loop.slot_key)
        tagged = f"{nudge_cycle_header(loop)}\n{msg_body}"
    else:
        tagged = wake_message
        _fired_generation = loop.config_generation
    # Fail closed: an unattended turn MUST run under the HookManager
    # PreToolUse governance gate (mirrors cron's default approval path).
    # Without ctx_builder there are no hooks to enforce the gate — skip.
    if self.ctx_builder is None or self.ctx_builder.hooks is None:
        logger.warning(
            "AutoNudge: no hook manager available — refusing unattended "
            "slack nudge turn for %s (loop %s)",
            key,
            loop.id,
        )
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
    response: str | None = None
    _acquired = False
    _turn_started = False
    _driver_completion_hook: MonitorCompletionHook | None = None
    _completion_hook: MonitorCompletionHook | None = None
    _raw_dispositions: list[MonitorActionDisposition] = []
    _completion_reported = False
    _in_bookkeeping = False

    def _open_bookkeeping() -> None:
        # Called with no await since the provider returned, so shutdown never
        # finds this task between the reply and its registration.
        nonlocal _in_bookkeeping
        if not _in_bookkeeping:
            bookkeeping.enter_context(self._bookkeeping_section())
            _in_bookkeeping = True

    try:
        _memory_store = await session_store_for_turn(self.ctx_builder, key)
        if wake_message is None:
            client, is_new, _resumed = await self.sessions.get_or_create(key)
        else:
            client, is_new, _resumed = await self.sessions.get_or_create(key, wait_if_busy=False)
        _acquired = True
        _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
        # An auto-nudge cycle continues the NUDGED session's own conversation,
        # so it reads that session's silo — resolved from its recorded binding,
        # the same key its consolidations are filed under. Without it a
        # crew-bound conversation gets nudged with the operator's own memory in
        # the prompt, and the reply it produces is then filed into the crew's
        # store as if the crew had said it.
        full_msg, _ = await run_in_embed_pool(
            self.ctx_builder.build_message,
            tagged,
            is_new,
            key,
            memory_store=_memory_store,
            provider_type=_provider,
            context_provider=client,
            resumed=_resumed,
        )
        _completion_hook = self._monitor_completion_hook(loop)
        if wake_message is not None and _completion_hook is None:
            return MonitorDispatchResult.UNAVAILABLE
        # Clock started outside wait_for so BOTH the success path and the
        # TimeoutError branch below can report the real elapsed time. acp
        # never assigns TurnUsage.duration_ms, so the row needs this.
        _turn_t0 = time.monotonic()
        _turn_started = True
        if wake_message is None:

            def _capture_raw_completion(event: LLMEvent) -> None:
                if is_monitor_completion_evidence(
                    event.stop_reason,
                    synthetic=event.synthetic_completion,
                ):
                    _raw_dispositions.append(disposition_for_stop_reason(event.stop_reason))

            response = await asyncio.wait_for(
                stream_and_collect(
                    client,
                    full_msg,
                    retry_transient=False,
                    # Same governance contract as unattended cron turns: the
                    # HookManager PreToolUse gate decides tool approvals, and
                    # anything it can't decide goes to the deny-fast
                    # background-approval window (source "autonudge").
                    approval_policy=ToolApprovalPolicy.HOOK_BASED,
                    hooks=self.ctx_builder.hooks,
                    on_tool_approval=self._interactive_approval("autonudge", nudge_key=key),
                    on_complete=_capture_raw_completion,
                    undelivered_watch_key=key,
                ),
                timeout=_NUDGE_TURN_TIMEOUT,
            )
        else:

            async def _capture_completion(completion: Any) -> None:
                _raw_dispositions.append(completion.disposition)

            approval = self._interactive_approval("autonudge", nudge_key=key)
            sessions = self.sessions
            assert sessions is not None
            driver = TurnDriver(
                client,
                SilentRenderer(channel_type="slack"),
                approval_mode=APPROVAL_INTERACTIVE,
                decider=approval,
                tool_gate=build_tool_gate(
                    self.ctx_builder,
                    session_key=key,
                    agent=_get_agent_for_session(key) or "",
                ),
                directive_consumer=build_directive_consumer(
                    session_key=key,
                    sessions=self.sessions,
                ),
                monitor_completion=(
                    (
                        _driver_completion_hook := MonitorCompletionHook(
                            _completion_hook.monitor_id,
                            _completion_hook.fingerprint,
                            _capture_completion,
                            authorization_callback=_completion_hook.authorization_callback,
                            acceptance_callback=_completion_hook.mark_accepted,
                        )
                    )
                    if _completion_hook is not None
                    else None
                ),
                closing_gate=lambda: sessions.begin_turn(key),
                undelivered_watch_key=key,
            )
            response = await asyncio.wait_for(
                driver.run(full_msg),
                timeout=_NUDGE_TURN_TIMEOUT,
            )
            if _driver_completion_hook is None or not _driver_completion_hook.accepted:
                return MonitorDispatchResult.UNAVAILABLE
            assert _completion_hook is not None
            _completion_hook.mark_accepted()
        _open_bookkeeping()
        _turn_usage = provider_last_turn_usage(client)

        if _raw_dispositions:
            await self._report_monitor_completion(
                loop,
                _raw_dispositions[-1],
                _turn_usage,
                hook=_completion_hook,
            )
            _completion_reported = True
        # ── Per-turn usage row: attribute monitor spend. ──
        await _persist_turn_row(
            client,
            key,
            provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
            surface="monitor",
            agent_fallback=lambda: _get_agent_for_session(key),
            t0=_turn_t0,
            usage=_turn_usage,
        )
    except SessionBusyError:
        logger.info(
            "AutoNudge skip: slack session %s won by another turn (loop %s)",
            key,
            loop.id,
        )
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
    except SessionClosingError:
        logger.info(
            "AutoNudge: refusing Slack monitor turn for %s during shutdown",
            key,
        )
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
    except asyncio.CancelledError:
        if _turn_started and _raw_dispositions and not _completion_reported:
            _open_bookkeeping()
            _turn_usage = provider_last_turn_usage(client)
            await self._report_monitor_completion(
                loop,
                _raw_dispositions[-1],
                _turn_usage,
                hook=_completion_hook,
            )
        raise
    except asyncio.TimeoutError:
        # ── Timeout spend is REAL spend. ──
        # A timed-out nudge turn would otherwise fall through to the generic
        # handler below and write no row at all, silently dropping whatever
        # the cancelled turn had already cost. Record it, then bail. Runs
        # before the `finally` cancels/releases the session.
        #
        # No new schema field: the record has never carried a
        # success/failure outcome for ANY surface, so a timeout row is no
        # less honest than any other row.
        logger.warning(
            "AutoNudge: slack nudge turn timed out after %ss for %s (loop %s)",
            _NUDGE_TURN_TIMEOUT,
            key,
            loop.id,
        )
        _open_bookkeeping()
        _turn_usage = provider_last_turn_usage(client)
        if _raw_dispositions:
            await self._report_monitor_completion(
                loop,
                _raw_dispositions[-1],
                _turn_usage,
                hook=_completion_hook,
            )
        await _persist_turn_row(
            client,
            key,
            provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
            surface="monitor",
            agent_fallback=lambda: _get_agent_for_session(key),
            t0=_turn_t0,
            usage=_turn_usage,
        )
        if wake_message is None:
            return False
        if _driver_completion_hook is not None and _driver_completion_hook.accepted:
            return MonitorDispatchResult.DISPATCHED
        return MonitorDispatchResult.BUSY
    except Exception as exc:
        logger.exception("AutoNudge: slack nudge turn failed for %s (loop %s)", key, loop.id)
        await self._stop_message_loop_if_structural_terminal(
            loop, exc, wake_message, _fired_generation
        )
        if (
            wake_message is not None
            and _driver_completion_hook is not None
            and _driver_completion_hook.accepted
        ):
            assert _completion_hook is not None
            _completion_hook.mark_accepted()
            return MonitorDispatchResult.DISPATCHED
        if wake_message is None:
            return False
        return MonitorDispatchResult.BUSY
    finally:
        if _acquired:
            try:
                await self.sessions.cancel_current(key)
            except Exception:
                logger.debug("AutoNudge: cancel_current failed for %s", key, exc_info=True)
            try:
                self.sessions.release(key)
            except Exception:
                logger.exception("AutoNudge: failed to release session %s", key)
    # Post the response into the originating thread (best-effort — the
    # turn itself already ran, so failures here don't fail the cycle).
    try:
        if response:
            for part in render_for_slack(response):
                await self.slack.post_message(channel, part, thread_ts)
    except Exception:
        logger.exception("AutoNudge: slack posting failed for %s (turn ran)", key)
    # Persist for dashboard replay (mirrors subagent Slack injection).
    if self.conv_log and not (is_thread_temporary(key) or is_thread_incognito(key)):
        try:
            safe_nudge, _ = redact_exfiltration_urls(tagged)
            safe_nudge, _ = redact_credentials(safe_nudge)
            safe_response, _ = redact_exfiltration_urls(response or "")
            safe_response, _ = redact_credentials(safe_response)
            await save_conversation_turn_off_loop(
                self.conv_log,
                key,
                safe_nudge,
                safe_response,
                source_thread=key,
                source_user="autonudge",
                agent=_get_agent_for_session(key),
            )
        except Exception:
            logger.warning("AutoNudge: failed to persist nudge turn for %s", key, exc_info=True)
    return _delivery_result(wake_message, MonitorDispatchResult.DISPATCHED)


async def _fire_dm_nudge(
    self: GatewayOrchestrator,
    loop: NudgeLoop,
    adapter: _DmDispatchAdapter,
    wake_message: str | None = None,
) -> bool | MonitorDispatchResult:
    """Drive one unattended nudge turn in a dispatcher-routed DM session.

    Shared by every channel that delivers a nudge the way a real DM arrives:
    synthesize that channel's inbound type and hand it to the channel's own
    dispatcher, so busy/steer/queue handling, rendering, chunking and
    persistence behave like a user turn. ``interpret_commands=False`` keeps
    the nudge text from being read as a command.

    Four guards run before anything is delivered, and they are this caller's
    responsibility rather than the transport's precisely because a synthetic
    injection never passes through ``transport.receive``:

    1. The channel's transport and dispatcher are running. A gateway can
       have the channel disabled or still starting, which is a SKIP: the
       loop is fine and the next cycle may find the transport up.
    2. The binding key has the direct-message shape
       ``<channel>:{agent}:direct:{principal}[:genN]``. Any other shape
       cannot name a principal, so the loop can never fire and is retired.
    3. The principal is still on the inbound allow-list. The create
       endpoint checks it too, but an allow-list can SHRINK after a loop is
       armed, so the check is repeated here at fire time.
    4. The session generation still matches. A ``new``-style command mints a
       fresh key; firing into the rotated one would run in a session with
       none of the loop's context, and a stop issued from there could never
       find this loop. Retire instead of firing into the wrong generation.

    Then a busy session is a SKIP rather than a queue, so a cycle is not
    counted while the human's own turn is running.

    A retirement only happens for a plain nudge (``wake_message is None``).
    A structured monitor wake reports ``UNAVAILABLE`` and leaves the
    monitor's own lifecycle to decide, since the controller owns that
    record. Every outcome returns through :func:`_delivery_result`, so a
    caller asking for a plain nudge reads a bool and a caller carrying a
    wake reads the typed result.
    """
    key = loop.slot_key
    channel = adapter.channel
    if wake_message is not None and not adapter.supports_monitor:
        # Fail closed BEFORE delivering. A channel with no structured
        # dispatch cannot report whether the wake landed, so delivering it
        # and then answering UNAVAILABLE would show the text to the reader
        # while the controller treats the wake as undelivered and sends it
        # again. Refusing first keeps the two in agreement.
        logger.info(
            "AutoNudge: %s carries no monitor dispatch, refusing wake for loop %s",
            channel,
            loop.id,
        )
        return MonitorDispatchResult.UNAVAILABLE
    transports = getattr(self.dashboard_state, "channel_transports", None) or {}
    transport = transports.get(channel)
    dispatcher = transport.dispatcher if transport is not None else None
    if transport is None or dispatcher is None:
        logger.info(
            "AutoNudge skip: %s transport not running (loop %s)",
            channel,
            loop.id,
        )
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)

    async def _retire(stop_reason: str, reason: str, *args: Any) -> bool | MonitorDispatchResult:
        logger.warning("AutoNudge: " + reason, *args)
        if self.autonudge_svc and wake_message is None:
            await self.autonudge_svc.remove(loop.id, stop_reason=stop_reason)
        return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)

    parts = key.split(":")
    if len(parts) < 4 or parts[2] != "direct":
        return await _retire(
            "unsupported_key", "unsupported %s key %s, removing loop %s", channel, key, loop.id
        )
    principal = parts[3]
    if not adapter.authorize(transport, dispatcher, principal):
        return await _retire(
            "user_not_authorized", "%s user not authorized, removing loop %s", channel, loop.id
        )
    try:
        current_key = dispatcher.current_session_key(principal)
    except Exception:
        # A dispatcher that cannot answer is not evidence of rotation, so
        # treat the key as current and let the busy check and the dispatch
        # itself decide. Failing closed here would retire a healthy loop on
        # a transient lookup error.
        current_key = key
    if current_key != key:
        logger.info(
            "AutoNudge: %s session rotated (%s -> %s), removing loop %s",
            channel,
            key,
            current_key,
            loop.id,
        )
        if self.autonudge_svc and wake_message is None:
            await self.autonudge_svc.remove(loop.id, stop_reason="session_rotated")
        return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
    sessions = getattr(dispatcher, "sessions", None)
    if sessions is not None and sessions.is_busy(key):
        logger.info(
            "AutoNudge skip: %s session %s busy (loop %s)",
            channel,
            key,
            loop.id,
        )
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)

    if wake_message is None:
        msg_body = await compose_nudge_body(loop.message, loop.stop_sentinel_path, loop.slot_key)
        tagged = f"{nudge_cycle_header(loop)}\n{msg_body}"
    else:
        tagged = wake_message

    try:
        conversation_id = await adapter.resolve_conversation(transport, sessions, key, principal)
    except Exception:
        logger.exception(
            "AutoNudge: %s conversation lookup failed for %s (loop %s)",
            channel,
            key,
            loop.id,
        )
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)

    completion_hook: MonitorCompletionHook | None = None
    try:
        synthetic = adapter.build_inbound(principal, conversation_id, tagged)
        dispatch_kwargs: dict[str, Any] = {"interpret_commands": False}
        if adapter.supports_monitor:
            completion_hook = self._monitor_completion_hook(loop)
            if wake_message is not None and completion_hook is None:
                return MonitorDispatchResult.UNAVAILABLE
            if completion_hook is not None:
                dispatch_kwargs["monitor_completion"] = completion_hook
                dispatch_kwargs["monitor_session_key"] = key
        # This turn is GENERATED, not received, so it does not count against
        # the conversation's turn ceiling: the loop already carries its own
        # cycle cap and runtime budget, and spending the conversation's budget
        # on it would latch the conversation and then refuse the human's next
        # message. Marked here rather than passed down because this is the one
        # place that knows, and the channels' dispatch signatures in between
        # have no business carrying it.
        with turn_ceiling.generated_turn():
            dispatch_result = await asyncio.wait_for(
                dispatcher.handle_message(synthetic, **dispatch_kwargs),
                timeout=_NUDGE_TURN_TIMEOUT,
            )
        if wake_message is not None:
            return (
                dispatch_result
                if isinstance(dispatch_result, MonitorDispatchResult)
                else MonitorDispatchResult.UNAVAILABLE
            )
        if completion_hook is not None and isinstance(dispatch_result, MonitorDispatchResult):
            return dispatch_result is MonitorDispatchResult.DISPATCHED
        return True
    except Exception:
        logger.exception(
            "AutoNudge: %s nudge failed for %s (loop %s)",
            channel,
            key,
            loop.id,
        )
        if wake_message is None:
            return False
        if completion_hook is not None and completion_hook.accepted:
            return MonitorDispatchResult.DISPATCHED
        return MonitorDispatchResult.UNAVAILABLE


async def _fire_discord_nudge(
    self: GatewayOrchestrator, loop: NudgeLoop, wake_message: str | None = None
) -> bool | MonitorDispatchResult:
    """Drive one unattended nudge turn in a Discord DM session.

    The guard ladder and the delivery live in :meth:`_fire_dm_nudge`; this
    supplies only what is specific to Discord. Authorization is asked of the
    DISPATCHER here, which is the object holding Discord's inbound
    allow-list, and it mirrors the re-check ``on_interaction`` performs.
    """
    return await self._fire_dm_nudge(
        loop,
        _DmDispatchAdapter(
            channel="discord",
            supports_monitor=True,
            authorize=lambda _transport, dispatcher, principal: bool(
                dispatcher.is_authorized(principal)
            ),
            resolve_conversation=(
                lambda transport, _sessions, _key, principal: transport.resolve_conversation(
                    principal
                )
            ),
            build_inbound=lambda principal, conversation_id, text: InboundMessage(
                channel_type="discord",
                user_id=principal,
                conversation_id=conversation_id,
                text=text,
            ),
        ),
        wake_message,
    )


async def _fire_webex_nudge(self: GatewayOrchestrator, loop: NudgeLoop) -> bool:
    """Drive one unattended nudge turn in a Webex DM session.

    The guard ladder and the delivery live in :meth:`_fire_dm_nudge`; this
    supplies only what is specific to Webex. Two of those three pieces carry
    a reason worth keeping beside them.

    Authorization is asked of the TRANSPORT, not the dispatcher, because the
    Webex allow-list is held there.

    The room is read from the persisted origin link when there is one.
    Webex's ``resolve_conversation`` answers with the EMAIL, which its send
    path maps onto ``toPersonEmail``, so it delivers correctly but is a
    SECOND spelling of the same room. An origin bind is matched by VALUE
    (see ``_origin_mirror_link``), so a nudge that writes the link in that
    other spelling makes a later ``/unlink`` miss the binding. The persisted
    link therefore wins, and the email is the first-turn fallback, where no
    binding exists to disagree with yet.

    Webex carries no structured-monitor dispatch, so ``supports_monitor`` is
    False and this path is only ever asked for a plain nudge.
    """

    async def _resolve(transport: Any, sessions: Any, key: str, principal: str) -> str:
        existing = sessions.get_origin_link(key) if sessions is not None else None
        room = getattr(existing, "channel_id", "")
        if room:
            return str(room)
        return str(await transport.resolve_conversation(principal))

    def _build(principal: str, conversation_id: str, text: str) -> Any:
        # Imported HERE, not at module scope: this file is on the gateway
        # boot path, and it deliberately keeps every channel client behind
        # TYPE_CHECKING so enabling one channel does not cost every launch
        # the import of all of them. Reached only when a Webex loop fires.
        from kiro_crew.webex.client import WebexInbound
        from kiro_crew.webex.transport import ROOM_DIRECT

        return WebexInbound(
            person_email=principal,
            room_id=conversation_id,
            text=text,
            room_type=ROOM_DIRECT,
        )

    result = await self._fire_dm_nudge(
        loop,
        _DmDispatchAdapter(
            channel="webex",
            supports_monitor=False,
            authorize=lambda transport, _dispatcher, principal: bool(
                transport.is_authorized(principal)
            ),
            resolve_conversation=_resolve,
            build_inbound=_build,
        ),
    )
    # A plain nudge always normalizes to a bool through _delivery_result.
    assert isinstance(result, bool)
    return result


async def _stop_message_loop_if_structural_terminal(
    self: GatewayOrchestrator,
    loop: NudgeLoop,
    exc: BaseException,
    wake_message: str | None,
    fired_generation: int,
) -> bool:
    """Stop a MESSAGE loop whose fired turn raised a structural rejection.

    A malformed-request rejection ("Improperly formed request") is
    deterministic in the payload's SHAPE, so re-firing the identical nudge
    context can only reproduce it -- an undelivered cycle that the service
    would otherwise re-arm with backoff, forever, since undelivered cycles
    never reach ``max_cycles``. This is the CHANNEL-adapter counterpart to
    the dashboard fire path's pre-dispatch guard: a channel adapter runs its
    turn inline and holds the exception directly, so it reads the verdict off
    the exception (``AcpError.structural_terminal``) rather than through the
    slot flag the dashboard path relays. Scoped to message loops
    (``wake_message is None``): a structured monitor wake carries its own
    actionable context, not this repeated prompt, and keeps its own dispatch
    contract. Returns True when it stopped the loop. getattr-guarded: only
    AcpError carries the attribute.

    ``fired_generation`` is ``loop.config_generation`` captured at fire time.
    The stop is applied through ``AutoNudgeService.update(expected_generation
    =...)``, which compares it against the loop's CURRENT generation UNDER
    THE SERVICE LOCK: an inline channel turn can be slow, and a concurrent
    ``PATCH /api/autonudge/{id}`` can change the instruction (advancing the
    generation) WHILE the turn runs -- possibly back to the same text
    (A->B->A). The atomic fence refuses the stale stop with no TOCTOU window;
    a value compare on ``message`` could not tell a re-committed A apart.
    """
    if wake_message is not None:
        return False
    if not getattr(exc, "structural_terminal", False):
        return False
    if self.autonudge_svc is None:
        return False
    stopped_loop = await self.autonudge_svc.update(
        loop.id,
        active=False,
        stopped_reason=STRUCTURAL_TERMINAL_REASON,
        expected_generation=fired_generation,
    )
    if stopped_loop is None or stopped_loop.active:
        # The fence refused: the loop's config generation advanced under the
        # in-flight turn, so this malformed verdict belongs to an OLD
        # instruction and must not deactivate the reconfigured loop.
        logger.info(
            "AutoNudge: loop %s on %s not stopped — its config generation "
            "advanced while the malformed turn ran, so the rejection does "
            "not apply to the current instruction",
            loop.id,
            loop.slot_key,
        )
        return False
    logger.warning(
        "AutoNudge: loop %s on %s stopped — its delivered turn was rejected "
        "as structurally malformed, so re-firing the same context cannot "
        "help; the loop stays inactive and a later directive (after a fresh "
        "conversation) may re-arm it",
        loop.id,
        loop.slot_key,
    )
    return True


def _monitor_completion_hook(
    self: GatewayOrchestrator, loop: NudgeLoop
) -> MonitorCompletionHook | None:
    """Bind a structured loop's in-flight identity to controller accounting."""
    state = getattr(loop, "monitor", None)
    if state is None:
        return None
    service = self.autonudge_svc
    if service is None or not state.wake_in_flight or not state.last_wake_fingerprint:
        return None
    mark_accepted = getattr(service, "mark_monitor_turn_accepted", None)
    return MonitorCompletionHook(
        loop.id,
        state.last_wake_fingerprint,
        service.record_monitor_turn_completion,
        authorization_callback=getattr(service, "monitor_dispatch_is_authorized", None),
        acceptance_callback=(
            (lambda: mark_accepted(loop.id, state.last_wake_fingerprint))
            if mark_accepted is not None
            else None
        ),
    )


async def _report_monitor_completion(
    self: GatewayOrchestrator,
    loop: NudgeLoop,
    disposition: MonitorActionDisposition,
    usage: AgentTurnUsage | None,
    *,
    hook: MonitorCompletionHook | None = None,
) -> None:
    """Best-effort monitor accounting that cannot change delivery outcome."""
    if hook is None:
        hook = self._monitor_completion_hook(loop)
    if hook is None:
        return
    try:
        await hook.complete(disposition, usage)
    except Exception:
        logger.warning(
            "monitor turn completion callback failed for loop %s",
            loop.id,
            exc_info=True,
        )
