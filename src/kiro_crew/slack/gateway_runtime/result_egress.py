"""The delivery legs that render and redact an unattended result before egress.

A cron session's response to its own channel, a channel reply, a heartbeat or
background result to the dashboard slot or Slack, and the slot title it leaves.

Orchestrator fields read: ``_owner_id``, ``cron_svc``, ``dashboard_state``,
``sessions``, ``slack``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.slack.gateway import (
        _CRON_MSG_LIMIT,
        CHANNEL_ID_RE,
        MAX_PROMPT_BYTES,
        Any,
        ChannelLink,
        GatewayOrchestrator,
        KiroCrewConfig,
        _ChatSlot,
        _resolve_channel_target,
        build_options_blocks,
        channel_egress_parts,
        extract_options,
        is_tracked_channel,
        load_window_off_loop,
        logger,
        mint_options_token,
        redact_credentials,
        redact_exfiltration_urls,
        render_for_slack,
        sel,
        send_channel_parts,
    )


def _heartbeat_slack_parts(title: str, result_text: str) -> list[str]:
    """Render a heartbeat completion into postable Slack parts.

    Shared by all four heartbeat delivery branches so they cannot drift. Two
    things it fixes relative to the per-branch f-string it replaces:

    - **It splits.** Those branches posted one unsplit message, and Slack
      rejects anything past ~40,000 characters outright -- so a long heartbeat
      result was silently lost rather than truncated.
    - **It redacts around the transform.** ``_deliver_result`` redacts
      ``result_text`` at its head, but ``to_slack_mrkdwn`` strips ANSI escapes
      and that strip can reassemble a credential the escapes had broken up.
      Redacting again after conversion is what closes that.

    The ``💓 *title*`` caption goes through ``header=``, which redacts it without
    converting (it is already Slack mrkdwn) and charges it against the limit.
    """
    return render_for_slack(result_text, header=f"💓 *{title}*\n\n")


async def _deliver_cron_response(
    self: GatewayOrchestrator, parent_key: str, text: str, *, silent: bool = False
) -> bool:
    """Deliver a cron session's post-subagent response to its own channel.

    When a cron session spawns subagents via ``spawn_run``, the agent's
    synthesized response would otherwise only be appended to the dashboard
    notification body, making subagent delegation invisible in cron
    contexts. Two legs carry it, in this order:

    1. the channel that scheduled the job, resolved from the job's origin
       session key (:meth:`_cron_origin_key`) and delivered through the
       governed transport ladder;
    2. Slack, the channel/thread the cron originally posted in (stored on
       the session at delivery time), falling back to the owner's DM.

    The channel leg is tried FIRST and, when it delivers, it is the only leg:
    a job belongs to the conversation that scheduled it, so adding a Slack
    owner DM on top would notify one operator twice for one response. Slack
    remains the delivery for a Slack-origin, dashboard-origin or
    origin-less job, which is every job an install carries today. No-op when
    silent or when the text is blank. Returns True when a leg delivered; a
    False leaves the caller's dashboard notification as the only surface.
    """
    if silent or not text.strip():
        return False
    assert self.sessions is not None
    # Resolved before the Slack leg so a Slack-less install still delivers.
    delivered = await self._deliver_cron_to_channel(
        self._cron_origin_key(parent_key), text, actor_key=parent_key
    )
    # One surface per response. The channel that scheduled the job is the one
    # its owner is watching, so a Slack owner DM on top of it is a duplicate
    # rather than a second audience.
    if self.slack is None or delivered:
        return delivered
    channel = self.sessions.get_channel(parent_key)
    thread_ts = self.sessions.get_thread(parent_key)
    if not thread_ts:
        # A threaded cron does not claim its own inherited thread (that would
        # evict the human owner), so cron:<id> has no recorded outbound
        # anchor. Fall back to the job's own thread_ts so follow-ups thread
        # under the human's thread instead of posting at the channel top.
        thread_ts = self._cron_outbound_thread(parent_key)
    if not channel and self._owner_id:
        channel = await self._open_dm_with_retry(self._owner_id, parent_key)
        thread_ts = None  # a thread_ts from another channel is invalid in a DM
    if not channel:
        logger.warning("Cron %s: no channel resolved for subagent response", parent_key)
        # Not False: the channel leg above may already have delivered, and
        # reporting a drop would let the caller log one that did not happen.
        return delivered
    # render [OPTIONS: ...] tags as interactive buttons, matching
    # the interactive-handler / subagent-completion / dashboard-mirror paths.
    # Extracted from the raw text: the tag is a plain-text marker, so pulling
    # it off before conversion is what makes the controls independent of what
    # conversion (and its 39,000-char self-truncation) does to the tail.
    text, options = extract_options(text)
    # render_for_slack IS the redaction boundary here -- it normalises ANSI
    # first so a credential broken up by escapes cannot be reassembled by the
    # strip inside to_slack_mrkdwn, and redacts again after conversion.
    for part in render_for_slack(text, limit=_CRON_MSG_LIMIT):
        await self.slack.post_message(channel, part, thread_ts)
    if options:
        try:
            # Tokened like every other producer. An untokened control has no
            # asker to pin, so a click on it falls back to resolving the
            # thread -- which is exactly the reroute the pin exists to stop.
            _cron_token = await asyncio.to_thread(
                mint_options_token, self.dashboard_state, parent_key
            )
            option_blocks = build_options_blocks(options, staleness_token=_cron_token)
            option_ts = await self.slack.post_blocks(
                channel,
                option_blocks,
                "Options",
                thread_ts,
            )
            self._remember_options(
                parent_key, channel, option_ts, options, option_blocks, "Options"
            )
        except Exception:
            logger.debug("Cron %s: failed to post OPTIONS blocks", parent_key, exc_info=True)
    return True


async def _deliver_channel_reply(
    self: GatewayOrchestrator,
    parent_key: str,
    text: str,
    *,
    resolved_link: tuple[ChannelLink, bool] | None = None,
    caller: str = "subagent",
) -> bool:
    """Deliver unattended output to the non-Slack channel conversation behind a session.

    The transport leg of completion routing: resolves the conversation
    behind *parent_key*, vets the egress through the shared governed
    cross-surface ladder (``_resolve_channel_target`` — SEL-audited,
    fail-closed, capability-gated on ``supports_proactive_send``), then
    redacts, chunks, and sends via the registered ``MessagingTransport``.
    A link derived from the stored channel value carries the peer's USER
    id, so the postable conversation is resolved through
    ``transport.resolve_configured_target`` first.

    ``resolved_link`` lets the caller pass a target snapshotted BEFORE an
    injection retry loop — a timeout-path ``sessions.reset()`` evicts the
    in-memory origin link, so resolving only here would lose it.

    ``caller`` names the producer on the SEL trail and in the logs. Two
    surfaces share this leg (subagent completions and cron runs), and an
    allow-list decision recorded against the wrong one is an audit trail
    that points at a principal which never made the send.

    Returns True when the reply reached the channel; False degrades the
    caller to dashboard-notification-only. Never raises: a delivery
    failure must not break completion handling for the other paths.
    """
    if not text.strip() or self.dashboard_state is None:
        return False
    if resolved_link is None:
        resolved_link = self._channel_reply_link(parent_key)
    if resolved_link is None:
        return False
    link, needs_dm_resolution = resolved_link
    try:
        # Off-loop: the ladder's governance gate walks the profile
        # directory (iterdir + stat, with a possible reload), which is
        # unbounded on slow or networked storage.
        target = await asyncio.to_thread(
            _resolve_channel_target, self.dashboard_state, parent_key, link
        )
    except Exception:
        logger.exception("%s reply: channel target resolution failed for %s", caller, parent_key)
        return False
    if target is None:
        return False
    resolved, transport = target
    try:
        conversation_id = resolved.channel_id
        if needs_dm_resolution:
            # The stored value is the direct peer's user id, not a postable
            # conversation. resolve_configured_target("user:<id>") is the
            # transport contract for exactly this: it enforces the
            # transport's allow-list and returns the real send target
            # (learned conversation on Teams, DM-channel creation on
            # Discord, identity on Telegram) — or None when the peer has
            # no reachable conversation, which fails closed here.
            dm_target = await transport.resolve_configured_target(f"user:{resolved.channel_id}")
            # Audit the allow-list decision (allowed/denied) BEFORE
            # branching, matching chat_mirror's configured-target resolve:
            # a peer the resolver rejects is an authorization outcome and
            # must land in the SEL trail, not just degrade silently.
            sel().log_api_access(
                caller=caller,
                operation=f"{caller}.reply_target_resolve",
                outcome="allowed" if dm_target else "denied",
                source="gateway",
                resources=f"{parent_key} -> {link.channel_type}:user:{resolved.channel_id}",
            )
            if not dm_target or not dm_target[0]:
                return False
            conversation_id = dm_target[0]
        # Strip, display-redact, chunk and send through the shared
        # unattended-egress leg (``messaging.egress``): this is the chokepoint
        # every proactive channel egress passes (cron results, cron failure
        # and crash alerts, subagent completions), and none of them passes a
        # renderer. Stop on the first UNCONFIRMED part rather than pressing on.
        parts = await channel_egress_parts(text, transport.capabilities)
        if not await send_channel_parts(
            transport,
            conversation_id,
            parts,
            thread_id=resolved.thread_id,
            require_confirmation=True,
        ):
            logger.warning(
                "%s reply: %s returned no message id for %s; treating as undelivered",
                caller,
                link.channel_type,
                parent_key,
            )
            return False
    except Exception:
        logger.warning(
            "%s reply: %s delivery failed for %s",
            caller,
            link.channel_type,
            parent_key,
            exc_info=True,
        )
        return False
    logger.info(
        "%s reply → %s:%s (%d part(s))",
        caller,
        link.channel_type,
        conversation_id,
        len(parts),
    )
    return True


def _cron_outbound_thread(self: GatewayOrchestrator, parent_key: str) -> str | None:
    """The outbound Slack thread a ``cron:<id>`` run threads its follow-ups under.

    A cron that runs in an INHERITED/explicit ``job.thread_ts`` -- a human's
    own thread, keyed ``slack:<ts>`` -- is NOT claimed for ``cron:<id>``:
    ``set_thread`` is a non-self-derived ``set_slack_link`` claim that evicts
    the human's owner from the inbound index, so a run into a thread it did
    not post leaves the session map with no ``cron:<id>`` outbound anchor. The
    authoritative ``thread_ts`` lives on the job itself, so the delivery and
    approval readers resolve it from there to thread under the human's thread
    without the session map ever claiming it.

    Returns the job's ``thread_ts`` (as a string) for a ``cron:<id>`` key when
    the job exists and carries one, else ``None``. A cron that posts its own
    thread carries no ``job.thread_ts`` and resolves ``None``. Returns ``None``
    on a non-cron key, a missing job, or a corrupt/non-string field -- this is
    a delivery path and must never raise.
    """
    if not parent_key.startswith("cron:") or self.cron_svc is None:
        return None
    parts = parent_key.split(":", 2)
    if len(parts) < 2:
        return None
    job = self.cron_svc.get_job(parts[1])
    thread_ts = job.thread_ts if job else None
    return thread_ts if isinstance(thread_ts, str) and thread_ts else None


async def _persist_slot_title(self: GatewayOrchestrator, slot: "_ChatSlot") -> None:
    """Persist a dashboard slot's title so it survives a gateway restart.

    Best-effort and off the event loop (``set_title`` does a synchronous
    read + rewrite): a slow or failed write must never break heartbeat
    delivery. Mirrors the auto-research worker-slot titling path.
    """
    conv_log = getattr(self.dashboard_state, "conversation_log", None)
    if conv_log is None:
        return
    # Lazy import avoids a circular dependency (dashboard.chat_utils → gateway).
    from kiro_crew.dashboard.chat_utils import slot_history_key

    try:
        await asyncio.to_thread(conv_log.set_title, slot_history_key(slot), slot.title)
    except Exception:
        logger.warning("Heartbeat: failed to persist slot title for %s", slot.key, exc_info=True)


async def _resolve_loaded_slot(self: GatewayOrchestrator, slot_name: str) -> Any:
    """The dashboard slot *slot_name* names, with a pending window loaded off the loop.

    For a background delivery about to append to or prompt that tab. A tab
    restored as a sidebar row loads its window on a worker thread first, and
    the name is resolved again after the await, so a tab closed meanwhile
    reads as gone.
    """
    state = self.dashboard_state
    if state is None:
        return None
    slot = state.resolve_slot(slot_name)
    if slot is not None and getattr(slot, "window_pending", False) is True:
        await load_window_off_loop(slot)
        slot = state.resolve_slot(slot_name)
    return slot


async def _deliver_result(
    self: GatewayOrchestrator,
    title: str,
    task_summary: str,
    result_text: str,
    deliver: str,
) -> None:
    """Route a background result to the right surface.

    ``deliver`` values:
    - ``prompt:dashboard:<slot>`` → send as user prompt to dashboard slot (triggers agent turn)
    - ``dashboard:<slot>`` → inject into existing dashboard chat slot
    - ``dashboard``        → create new dashboard chat slot
    - ``slack:<chan>:<ts>`` → reply to Slack thread
    - ``slack:<chan>``     → new message in that Slack channel (later parts of a
      split report thread under the first one)
    - ``slack``            → new Slack DM only (no dashboard notification)
    - ``silent``           → log only
    - ``""`` (empty)       → routed per ``heartbeat.default_deliver`` config:
      ``slack`` (default) = Slack DM (if available) + dashboard notification;
      ``dashboard`` = dashboard slot + bell only (no Slack)
    """
    result_text, _ = redact_exfiltration_urls(result_text)
    result_text, _ = redact_credentials(result_text)
    task_summary, _ = redact_exfiltration_urls(task_summary)
    task_summary, _ = redact_credentials(task_summary)
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)

    # Tagless heartbeat completions route per the configured default
    # (heartbeat.default_deliver, default "slack" = backward compatible).
    # "dashboard" -> dashboard slot + bell only (no Slack); "slack" -> leave
    # empty so the default Slack-DM + dashboard branch below runs. An explicit
    # per-task <!-- deliver:... --> tag makes deliver non-empty and bypasses this.
    if not deliver:
        try:
            if KiroCrewConfig.load().heartbeat.default_deliver == "dashboard":
                deliver = "dashboard"
        except Exception:
            logger.debug("heartbeat default_deliver lookup failed", exc_info=True)
    body = f"{task_summary}\n\n{result_text}"

    # ── silent: log only ──
    if deliver == "silent":
        logger.info("%s (silent): %s", title, task_summary)
        return

    # ── prompt:dashboard:<slot> → send as user prompt to slot (triggers agent turn) ──
    if deliver.startswith("prompt:dashboard:"):
        slot_name = deliver.removeprefix("prompt:dashboard:")
        if not slot_name:
            logger.debug("Heartbeat prompt:dashboard: missing slot name, skipping")
            return
        if self.dashboard_state:
            slot = await self._resolve_loaded_slot(slot_name)
            if slot:
                # Truncate the variable-size *content* separately so the title/prefix
                # can never be sliced at a multi-byte boundary. errors='ignore'
                # (not 'replace') keeps the final byte size <= limit — U+FFFD
                # would be 3 bytes and push past the cap.
                prefix = f"{title}\n\n"
                prefix_bytes = len(prefix.encode("utf-8"))
                content_budget = max(0, MAX_PROMPT_BYTES - prefix_bytes)
                content_bytes = result_text.encode("utf-8")
                if len(content_bytes) > content_budget:
                    truncated = content_bytes[:content_budget].decode("utf-8", errors="ignore")
                    logger.warning(
                        "Heartbeat prompt truncated to %d bytes for slot %s",
                        MAX_PROMPT_BYTES,
                        slot_name,
                    )
                    prompt = prefix + truncated
                else:
                    prompt = prefix + result_text
                # Lazy import avoids circular dependency (chat → gateway)
                from kiro_crew.dashboard.chat import _run_chat

                sel().log_api_access(
                    caller="heartbeat",
                    operation="heartbeat_prompt_deliver",
                    outcome="approved",
                    source="gateway",
                    resources=f"requested={slot_name},resolved={slot.key}",
                )
                ran = slot.enqueue_or_run_prompt(prompt, _run_chat, self.dashboard_state)
                if ran:
                    # Only push UI updates when the prompt actually started —
                    # queued prompts produce no visible change until dequeued.
                    self.dashboard_state.push_slots_update()
                    self.dashboard_state.notify("heartbeat", title, body, meta={"slot": slot.key})
                else:
                    logger.info(
                        "Heartbeat prompt queued for busy slot %s (queue depth=%d)",
                        slot.key,
                        slot.queue_depth,
                    )
            else:
                sel().log_api_access(
                    caller="heartbeat",
                    operation="heartbeat_prompt_deliver",
                    outcome="not_found",
                    source="gateway",
                    resources=f"requested={slot_name}",
                )
                logger.warning("Heartbeat prompt target slot %s not found", slot_name)
        else:
            logger.debug("prompt:dashboard:%s ignored — no dashboard_state", slot_name)
        return

    # ── dashboard:<slot> → inject into specific slot ──
    if deliver.startswith("dashboard:"):
        slot_name = deliver.removeprefix("dashboard:")
        if self.dashboard_state:
            slot = await self._resolve_loaded_slot(slot_name)
            if slot:
                sel().log_api_access(
                    caller="heartbeat",
                    operation="heartbeat_inject_deliver",
                    outcome="approved",
                    source="gateway",
                    resources=f"requested={slot_name},resolved={slot.key}",
                )
                slot.append("assistant", f"{title}\n\n{result_text}", "msg msg-a")
                self.dashboard_state.push_slots_update()
                self.dashboard_state.notify("heartbeat", title, body, meta={"slot": slot.key})
            else:
                sel().log_api_access(
                    caller="heartbeat",
                    operation="heartbeat_inject_deliver",
                    outcome="not_found",
                    source="gateway",
                    resources=f"requested={slot_name}",
                )
                logger.warning("Heartbeat deliver target slot %s not found", slot_name)
        else:
            logger.debug("dashboard:%s ignored — no dashboard_state", slot_name)
        return

    # ── dashboard (no slot) → new slot ──
    if deliver == "dashboard":
        if self.dashboard_state:
            slot = self.dashboard_state.get_or_create_slot()
            # Heartbeat delivery appends only an assistant message, so the
            # interactive LLM auto-titler never fires for this slot
            # (_maybe_auto_title gates on user_count >= 1) and it would be
            # stuck on the "New Session…" placeholder forever. Seed a
            # meaningful title from the (already-redacted) task summary,
            # mirroring the cron/auto-research slot pattern: set the title,
            # lock _titled so display_title returns it, and persist it so it
            # survives a gateway restart (best-effort, off the event loop).
            seed = " ".join(task_summary.split())
            slot.title = f"💓 {seed}"[:80] if seed else title
            slot._titled = True
            await self._persist_slot_title(slot)
            slot.append("assistant", f"{title}\n\n{result_text}", "msg msg-a")
            self.dashboard_state.push_slot_title(slot.key, slot.title)
            self.dashboard_state.push_slots_update()
            self.dashboard_state.notify("heartbeat", title, body, meta={"slot": slot.key})
        return

    # ── slack (no thread) → new Slack DM only ──
    if deliver == "slack":
        if self.slack and self._owner_id:
            try:
                channel = await self.slack.open_dm(self._owner_id)
                if channel:
                    for post in _heartbeat_slack_parts(title, result_text):
                        await self.slack.post_message(channel, post)
            except Exception:
                logger.exception("Heartbeat Slack delivery failed")
        return

    # ── slack:<channel>[:<thread_ts>] → thread reply, else a new channel post ──
    if deliver.startswith("slack:"):
        parts = deliver.split(":", 2)
        try:
            # A channel id with no thread ts is a valid target: post there as a
            # new message. ``parts[1]`` must be non-empty though -- a truncated
            # tag ("slack:") would otherwise post to channel "", and the
            # resulting SlackApiError is swallowed by the except below, so the
            # report would go nowhere. That case keeps the owner-DM leg.
            # The ``slack:`` prefix guarantees a colon, so ``parts[1]`` exists.
            chan = parts[1]
            ts: str | None = parts[2] if len(parts) == 3 else None
            # The deliver tag is agent-writable: HEARTBEAT.md is not fenced by
            # ``security.paths._CREW_SECRET_LEAVES`` and prompt.md tells the
            # agent to append entries, so ``chan`` is untrusted input. An
            # unattended post reaches a NEW audience, which is what
            # ``api_send_message``'s tracked-channel allowlist governs.
            allowed = bool(chan and CHANNEL_ID_RE.match(chan) and is_tracked_channel(chan))
            owner_dm: str | None = None
            if self.slack and not allowed and self._owner_id:
                # Resolved only off the allowed path, where it serves twice: as
                # the fallback target, and to admit a ``slack:D...:<ts>``
                # origin-thread reply -- the owner's own DM channel is never a
                # tracked channel, so it cannot clear the check above.
                owner_dm = await self.slack.open_dm(self._owner_id)
                if chan and owner_dm and chan == owner_dm:
                    allowed = True
            # EVERY denied routing decision is audited, including the one with
            # an EMPTY channel (`deliver="slack:"`, which the agent can write).
            # Gating the log on `chan` meant that exact input produced a denial
            # with no SEL record at all -- the one shape that leaves no trace is
            # the one an agent can author, so it is the last that should be
            # exempt. `channel=` reads as "none supplied" in the record.
            if self.slack and not allowed:
                sel().log_api_access(
                    caller="heartbeat",
                    operation="heartbeat_channel_deliver",
                    outcome="denied",
                    source="gateway",
                    resources=f"channel={chan},thread_ts={ts or ''}",
                )
                logger.warning(
                    "Heartbeat deliver target %r is not a tracked channel — "
                    "falling back to the owner DM",
                    chan,
                )
            if self.slack and allowed:
                # An unattended post to an operator-named channel is egress to
                # a non-owner audience, so it lands on the audit trail like the
                # sibling dashboard-deliver branches.
                sel().log_api_access(
                    caller="heartbeat",
                    operation="heartbeat_channel_deliver",
                    outcome="approved",
                    source="gateway",
                    resources=f"channel={chan},thread_ts={ts or ''}",
                )
                for post in _heartbeat_slack_parts(title, result_text):
                    # A split report threads under its own first part rather
                    # than posting N top-level messages into the channel.
                    posted_ts = await self.slack.post_message(chan, post, ts)
                    ts = ts or posted_ts
            elif self.slack and owner_dm:
                for post in _heartbeat_slack_parts(title, result_text):
                    await self.slack.post_message(owner_dm, post)
        except Exception:
            logger.exception("Heartbeat Slack delivery failed")
        if self.dashboard_state:
            self.dashboard_state.notify("heartbeat", title, body)
        return

    # ── default: Slack DM + dashboard notification ──
    if self.slack and self._owner_id:
        try:
            channel = await self.slack.open_dm(self._owner_id)
            if channel:
                for post in _heartbeat_slack_parts(title, result_text):
                    await self.slack.post_message(channel, post)
        except Exception:
            logger.exception("Heartbeat Slack delivery failed")
    if self.dashboard_state:
        self.dashboard_state.notify("heartbeat", title, body)
