"""The channel transports' lifecycle after the orchestrator is built.

The connect-time ``channels`` governance gate every transport start passes, the
governed Slack socket connect, the live-config appliers (in-process restart of one
channel, the Slack hot-field applier), the boot-time re-hoist from the config
watcher, readiness badges, and the replay of the durable inbound spool.

The per-channel hoists, ``_register_config_appliers`` and
``_start_channel_transports`` stay in the facade: the hot-reload and readiness
audits parse them there.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        HOST_SESSION_KEY,
        ChannelDescriptor,
        ChannelHistory,
        ConfigChange,
        ConfigDeferred,
        GatewayOrchestrator,
        KiroCrewConfig,
        Path,
        PlatformCompositionError,
        SlackTransport,
        asyncio,
        audit_governance_degraded,
        builtin_channel_descriptors,
        governance_permits,
        inbound_spool,
        live,
        logger,
        maintenance_executor,
        registry,
        sel,
    )


def _channel_transport_permitted(member: str) -> bool:
    """Return True only if the ``channels`` scope POSITIVELY permits *member*.

    Gates each transport's STARTUP on the same ``channels`` ScopedMap the two
    OUTBOUND chokepoints consult: outbound-send
    (``mcp_core._vet_channel_governance``) and outbound cross-surface mirroring
    (``dashboard.chat_runner._resolve_mirror_target``).  So one ``channels``
    policy governs a transport consistently at connect and on every outbound
    path — this gate is the connect-time member.  INBOUND receive is gated
    separately and per-message by ``messaging.identity.channel_inbound_permitted``
    (called at the top of each dispatcher's ``handle_message``), so a deny added
    after connect stops dispatch without a restart.  *member* is the transport's
    ``channel_type`` (``slack`` / ``wecom`` / ``telegram`` / ``discord`` /
    ``webex``) — the IDENTICAL member id the outbound + inbound gates use, so one
    allowlist covers them all.  **Slack is NOT exempt**: it is gated in
    ``_connect_slack`` (which also drops the socket client on a deny so nothing
    can reconnect it), while the other four are gated in
    ``_start_channel_transports``.

    HOST-side resolution mirrors the canonical
    ``apps.manager._app_activation_denied`` template:

    * ``session_key=HOST_SESSION_KEY`` — starting a transport is an operator/host
      action, so it is governed by the policy ceiling AND any ``bind: {type:
      surface, id: host}`` profile.  An empty key would classify to surface
      ``unknown`` and silently ignore a host profile (or mis-classify it to
      ``slack``); ``apps/manager`` and ``slack/enterprise.py`` classify the same
      way.
    * Both the DENY and the ALLOW decision are audited here via
      ``sel().log_governance_decision`` — ``governance_permits`` audits only its
      own degrade, not a normal permit/deny, so the caller owns both.

    Default-build invariant: with no policy governing ``channels`` (the standard
    open-source case) ``governance_permits`` returns a permitting Decision, so
    every ENABLED transport starts.

    Connect-time + inbound: this gate is the CONNECT-time member. A separate
    per-message inbound gate (``messaging.identity.channel_inbound_permitted``,
    called at the top of each dispatcher's ``handle_message``) rechecks the same
    ``channels`` policy on every inbound message, so a host-profile deny added
    AFTER a transport connected stops dispatching without a restart. Together they
    cover connect + inbound; the outbound chokepoints cover sends.

    Audit + error posture (exact):

    * GOVERNED allow (a policy/profile governs ``channels`` → ``rule !=
      "default"``): **audit-or-deny**. The allow SEL is written with
      ``critical=True`` (synchronous + raising), so a persistence failure
      (unwritable SEL / full disk) propagates to the outer ``except`` and DENIES
      the start — a policy-governed transport never connects unaudited. This is
      why ``critical`` is required: the default background writer SWALLOWS disk
      failures, so a best-effort allow-audit would let the transport connect even
      when its audit record never landed.
    * UNGOVERNED allow (no policy governs ``channels`` — the default OSS build →
      ``rule == "default"`` / ``_PERMIT_NOT_GOVERNED``): **best-effort**
      (``critical=False``). OSS transport availability must never depend on SEL
      disk health when the operator configured no governance at all.
    * DENY: best-effort audit (the transport is not starting either way).
    * ERROR: **fail-closed**. A transport is an externally-reachable network
      surface, so it starts ONLY on a positive permit. We pass
      ``governance_permits(fail_closed=True)`` so an internal
      governance-evaluation error yields a DENYING Decision, and the outer
      ``except Exception`` ALSO denies (``return False`` + a ``failed_closed=True``
      degrade audit) — deny-by-default on any error, never an unaudited connect.
      This deliberately DIVERGES from ``apps.manager`` /
      ``mcp_core._vet_channel_governance`` (which fail open) because they gate
      in-process actions, not a network-reachable listener.  A
      ``PlatformCompositionError`` still propagates (a broken CPP composition must
      abort, not silently deny).
    """
    try:
        # A bare member id queries the ``channels`` ScopedMap ``members`` ruleset.
        # session_key=HOST_SESSION_KEY: honour a surface:host profile (empty key
        # → "unknown" would silently ignore it), matching apps/manager.
        # fail_closed=True: an internal governance error DENIES (network surface).
        decision = governance_permits(
            "channels", member, session_key=HOST_SESSION_KEY, fail_closed=True
        )
        if not getattr(decision, "permitted", False):
            logger.warning(
                "%s transport not started: denied by the channels governance policy (%s).",
                member,
                getattr(decision, "reason", "") or "denied",
            )
            # governance_permits does NOT audit a normal deny — the caller must.
            try:
                sel().log_governance_decision(
                    session_key=HOST_SESSION_KEY,
                    tool_name=f"start_transport:{member}",
                    scope="channels",
                    item=member,
                    outcome="denied",
                    rule=getattr(decision, "rule", ""),
                    layer=getattr(decision, "layer", ""),
                    reason=getattr(decision, "reason", ""),
                )
            except Exception:
                logger.debug("transport-start deny audit failed", exc_info=True)
            return False
        # Audit the ALLOWED decision too (a connect to an externally-reachable
        # surface is worth a positive audit trail). The disposition splits on
        # whether the ``channels`` scope was actually GOVERNED for this member:
        #   * GOVERNED allow (a policy AND/OR profile governs ``channels``):
        #     audit-or-deny. Pass critical=True so the SEL write is
        #     synchronous+raising; a persistence failure (unwritable SEL, full
        #     disk) propagates to the outer except and DENIES the start — never
        #     connect a policy-governed transport unaudited. (A background enqueue
        #     would swallow the disk failure, so critical is required to make
        #     audit-or-deny real, not just cover a synchronous raise.)
        #   * UNGOVERNED allow (no policy/profile governs ``channels``):
        #     best-effort (critical=False). OSS transport availability must NOT
        #     depend on SEL disk health when the operator has configured no
        #     governance for this scope.
        # Detect "governed" via the Decision's LAYER, not its rule. ``resolve()``
        # returns rule="rule2-intersect" for EVERY permit — including the case
        # where a policy exists but does not govern ``channels`` — so a rule-based
        # check would mis-treat that ungoverned case as governed. ``layer`` names
        # WHICH level actually carried the decision:
        #   * no policy at all   → governance_permits early-returns layer="" ;
        #   * policy, but channels ungoverned → resolve() sets layer="default" ;
        #   * channels governed  → layer is "policy" / "profile" / "both".
        # So "governed" is exactly layer ∈ {policy, profile, both}.
        governed = getattr(decision, "layer", "") in ("policy", "profile", "both")
        try:
            sel().log_governance_decision(
                session_key=HOST_SESSION_KEY,
                tool_name=f"start_transport:{member}",
                scope="channels",
                item=member,
                outcome="allowed",
                rule=getattr(decision, "rule", ""),
                layer=getattr(decision, "layer", ""),
                reason=getattr(decision, "reason", ""),
                critical=governed,
            )
        except PlatformCompositionError:
            raise
        except Exception:
            if governed:
                # audit-or-deny: a GOVERNED transport must never connect
                # unaudited. Re-raise so the outer fail-closed branch denies the
                # start (critical=True already forced a synchronous+raising write,
                # so this is a real persistence failure, not a swallowed enqueue).
                raise
            # UNGOVERNED allow: best-effort. An SEL ill-health (e.g. corrupt HMAC
            # key during sel() init/redaction) must NOT deny an ungoverned
            # transport — OSS availability does not depend on SEL disk health when
            # the operator configured no governance for this scope. Log and start.
            logger.warning(
                "%s transport: ungoverned allow could not be audited (best-effort); "
                "starting anyway",
                member,
                exc_info=True,
            )
        return True
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED (deliberate divergence from apps/manager + mcp_core, which
        # fail open): a transport is an externally-reachable network surface, so
        # an unexpected governance error must DENY the connect, not permit it.
        # Record the failed-closed degrade; wrap it so a late-import failure
        # cannot raise out of this branch and mask the deny.
        try:
            audit_governance_degraded(
                "start_transport",
                session_key=HOST_SESSION_KEY,
                scope="channels",
                failed_closed=True,
            )
        except Exception:
            logger.debug("transport-start governance degrade audit unavailable", exc_info=True)
        logger.warning(
            "%s transport not started: channels governance check errored; "
            "failing closed (deny-by-default for a network-exposed surface).",
            member,
            exc_info=True,
        )
        return False


def _push_observe_limits(history: ChannelHistory, max_entries: int, ttl_secs: int) -> None:
    """Push new observe-mode caps onto a live :class:`ChannelHistory`.

    Prefers the history's own setter when it has one; otherwise sets the two
    fields the constructor set, which every later trim and buffer allocation
    reads. Existing observe buffers keep their current size until the next
    ``set_observe`` upgrade -- trimming is the only consumer, so nothing is
    lost by applying the cap lazily.
    """
    setter = getattr(history, "set_observe_limits", None)
    if callable(setter):
        setter(max_entries, ttl_secs)
        return
    history._observe_max_entries = max_entries
    history._observe_ttl_secs = ttl_secs


async def _connect_slack(self: GatewayOrchestrator) -> bool:
    """Connect the Slack socket-mode client. Non-fatal on failure.

    Returns ``True`` if connected, ``False`` if Slack is disabled or the
    connect failed. A failure (network/proxy/timeout — e.g. a stale
    ``HTTPS_PROXY`` in the environment) must NOT crash the gateway: the
    dashboard, cron, and task runner keep running in dashboard-only mode.

    ponytail: no background retry of the initial connect — Slack DM stays
    disabled until the next gateway restart.

    Slack is a GOVERNED transport like every other channel: a ``channels``
    policy that denies ``slack`` must stop it from CONNECTING, not merely drop
    its inbound messages. The check runs off the loop (it walks the
    ProfileStore) and, on a deny, the socket client is dropped so nothing can
    later reconnect it. Default build (no ``channels`` policy) permits, so the
    connect path is byte-identical to today.
    """
    if not self._socket_client:
        return False
    loop = asyncio.get_running_loop()
    slack_permitted = await loop.run_in_executor(
        maintenance_executor(), _channel_transport_permitted, "slack"
    )
    if not slack_permitted:
        logger.info("slack transport not started: denied by channels governance policy")
        self._socket_client = None
        return False
    try:
        await self._socket_client.connect()
        print("👻 Kiro Crew gateway connected to Slack")
        return True
    except Exception as exc:
        # Keep a short reason for status surfaces (settings badge). Slack
        # API errors carry a stable code like "invalid_auth"; anything
        # else (network/proxy) falls back to the exception class name.
        reason = ""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                reason = str(resp.get("error", "") or "")
            except Exception:
                reason = ""
        self._slack_connect_error = (reason or type(exc).__name__)[:120]
        logger.warning(
            "Slack socket-mode connect failed — continuing in "
            "dashboard-only mode (Slack DM disabled this session)",
            exc_info=True,
        )
        print(
            "⚠️  Slack connect failed — running dashboard-only "
            "(check network/proxy; details in gateway.log)"
        )
        return False


async def _on_channel_config_change(self: GatewayOrchestrator, change: ConfigChange) -> None:
    """Restart every channel whose CONNECTION parameters changed -- and only those.

    A channel restarts when a path under its section names one of its
    descriptor's ``boot_keys`` (``registry.changed_boot_keys``). Live fields
    of the same section -- allow-lists, thresholds, render toggles -- are
    applied by that channel's own applier without a reconnect, so a change
    touching only them leaves the transport alone. Slack is not in this loop:
    its lifecycle is host-managed (``_connect_slack``), and its hot fields go
    through :meth:`_on_slack_config_change`.
    """
    if not self._channel_transports_started:
        # The boot loop has not started the channels yet: they start from the
        # hoist in __init__, so a restart here would race it. The change is
        # NOT dropped -- the watcher is armed at dashboard init, several
        # awaited steps before the transports start, so a CLI write in that
        # window is a real path. Deferring hands the paths to the watcher's
        # stale table: it re-runs this applier every tick against the CURRENT
        # snapshot, so the retry that lands once the transports are up sees
        # the document as it is then -- including the degraded check below,
        # which a replay outside ``_apply_one`` would have skipped -- and
        # consecutive writes accumulate into one union of paths.
        logger.debug("channel restart applier: transports not started yet; deferring")
        raise ConfigDeferred(())
    # A section the loader discarded diffs that channel's boot keys against
    # DEFAULTS; restarting from it would disable the channel. Those paths stay
    # pending and the watcher retries them once the document validates. (A
    # document torn as a whole never reaches this applier: the watcher keeps
    # the previous snapshot while the file does not parse.)
    degraded = change.new.degraded_sections
    bootable = registry.bootable(builtin_channel_descriptors())
    to_restart: list[tuple[str, frozenset[str]]] = []
    deferred: set[str] = set()
    for desc in bootable:
        if desc.channel_type in degraded:
            deferred.update(change.under(desc.channel_type))
            continue
        keys = registry.changed_boot_keys(desc, change.changed)
        if keys:
            to_restart.append((desc.channel_type, keys))
    if not to_restart:
        if deferred:
            raise ConfigDeferred(deferred)
        return
    # CLOSE inline, before the dispatch returns. A save handler that awaited
    # ``refresh_now`` must see inbound access closed when it answers: after
    # this loop the old client is shut (bounded, 2s per channel), its mirror
    # registration is gone, and no queued inbound traffic reaches a
    # transport whose config was just revoked.
    async with self._channel_restart_lock:
        for channel_type, _keys in to_restart:
            await self._close_channel_locked(channel_type)
    # Only the RECONNECT runs off the watcher's cycle: the applier is awaited
    # under ``ConfigWatch._cycle``'s lock, which every dashboard save also
    # waits on through ``refresh_now``, so a transport whose connect hangs
    # for its full timeout must not hold up an unrelated save or another
    # applier. The task is tracked so it is neither collected mid-flight nor
    # lost to shutdown; ``restart_channel`` finds nothing left to close and
    # brings the channel up from the current snapshot.
    task = asyncio.create_task(
        self._restart_changed_channels(to_restart), name="channel-restart-applier"
    )
    self._channel_restart_tasks.add(task)
    task.add_done_callback(self._channel_restart_tasks.discard)
    if deferred:
        # Only the degraded channels' paths are retried; the restarts above
        # are already scheduled and must not run again on the retry.
        raise ConfigDeferred(deferred)


async def _restart_changed_channels(
    self: GatewayOrchestrator, to_restart: list[tuple[str, frozenset[str]]]
) -> None:
    # Each channel is rebuilt from the watcher's CURRENT snapshot, never from
    # the change that scheduled this task: a later write (an allow-list
    # revocation, say) can land and be applied live while this task waits
    # on the restart lock, and a restart from the older document would put
    # the revoked principal back.
    for channel_type, keys in to_restart:
        logger.info(
            "config: %s connection parameter(s) changed (%s); restarting the channel",
            channel_type,
            ", ".join(sorted(keys)),
        )
        try:
            await self.restart_channel(channel_type)
        except Exception:
            logger.exception("config: restarting %s failed", channel_type)


async def channel_restarts_settled(self: GatewayOrchestrator) -> None:
    """Wait for every in-flight channel restart the applier scheduled."""
    while self._channel_restart_tasks:
        await asyncio.gather(*list(self._channel_restart_tasks), return_exceptions=True)


async def restart_channel(
    self: GatewayOrchestrator, channel_type: str, *, cfg: KiroCrewConfig | None = None
) -> object | None:
    """Close *channel_type*'s live transport and start it again from *cfg*.

    The in-process equivalent of a gateway restart for ONE channel, in the
    order boot uses: bounded close of the old handle (``registry.shutdown_tasks``),
    drop the handle and its legacy ``_<channel>_client`` mirror, re-run that
    channel's hoist against *cfg* plus a fresh credential read (off-loop:
    ``load_credentials`` reads the store), re-evaluate the ``channels``
    governance gate and the readiness badge, then ``desc.start(orch)`` and
    store the new handle. A channel whose new config disables it, leaves it
    uncredentialed, or is denied by policy ends closed with its badge
    explaining why, exactly as it would after a real restart.

    The channel's section on ``self._cfg`` is replaced with *cfg*'s, because
    the ``maybe_start_*`` factories and the dispatchers they build read
    their allow-lists and options from ``orch._cfg.<channel>``; without this
    the restarted transport would authorize against the boot-time list.

    The close, the hoist and the publish of the new handle run under
    ``_channel_restart_lock``; the connect between them does not, so a
    newer close is never held behind a slow connect and the per-channel
    restart generation decides whether the connected client is published
    or discarded. *cfg* defaults to the
    watcher's snapshot, then a load off the loop. Returns the new client
    handle, or ``None`` when the channel is (now) off.
    """
    desc = next(
        (
            d
            for d in registry.bootable(builtin_channel_descriptors())
            if d.channel_type == channel_type
        ),
        None,
    )
    if desc is None:
        raise ValueError(f"{channel_type!r} is not a restartable channel")
    async with self._channel_restart_lock:
        await self._close_channel_locked(channel_type)
        generation = self._channel_restart_gen.get(channel_type, 0)
        if cfg is None:
            cfg = live.snapshot() or await asyncio.to_thread(KiroCrewConfig.load)
        assert cfg is not None
        creds = await asyncio.to_thread(cfg.load_credentials)
        if cfg is not self._cfg:
            setattr(self._cfg, channel_type, getattr(cfg, channel_type))
        getattr(self, f"_hoist_{channel_type}")(cfg, creds)
        enabled = bool(getattr(self, f"_{channel_type}_enabled", False))
        loop = asyncio.get_running_loop()
        permitted = await loop.run_in_executor(
            maintenance_executor(),
            lambda: _channel_transport_permitted(channel_type) if enabled else False,
        )
        await loop.run_in_executor(maintenance_executor(), self._badge_unready_channels, (desc,))
        if not permitted:
            logger.info("restart %s: channel is off after reload", channel_type)
            return None
    # The CONNECT runs outside the lock. A disable landing while a slow
    # connect is in flight closes inline under the lock at once instead of
    # queuing behind the connect, and its close bumps the generation, so the
    # client this start produces is discarded below rather than stored.
    handles = await registry.start_channels(self, (desc,), {channel_type: True})
    client = handles.get(channel_type)
    if client is None:
        return None
    async with self._channel_restart_lock:
        if self._channel_restart_gen.get(channel_type, 0) != generation:
            # A newer close landed while this start was connecting: this
            # client was built from a superseded document, so it is torn
            # down rather than stored, and the newer restart brings up the
            # current one.
            for closing in registry.shutdown_tasks({channel_type: client}, timeout=2.0):
                try:
                    await closing
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("restart %s: closing a superseded client failed", channel_type)
            self._forget_superseded_start(channel_type, client)
            return None
        self._channel_handles[channel_type] = client
        return client


def _forget_superseded_start(self: GatewayOrchestrator, channel_type: str, client: object) -> None:
    """Take back what a start torn down as superseded had already published.

    A factory registers its transport for cross-surface delivery before it
    connects, and the registry mirrors the client onto ``_<channel>_client``
    when the start returns -- both ahead of the generation check. Left in
    place, a closed transport keeps answering ``get_channel_transport`` and
    proactive replies route into it and are lost. Only this start's own
    publications are removed, by identity: a newer start may have published
    since, and its transport and mirror must stay.
    """
    transports = getattr(self.dashboard_state, "channel_transports", None)
    if isinstance(transports, dict):
        current = transports.get(channel_type)
        dispatcher = getattr(current, "dispatcher", None)
        if current is not None and getattr(dispatcher, "client", None) is client:
            transports.pop(channel_type, None)
    if getattr(self, f"_{channel_type}_client", None) is client:
        setattr(self, f"_{channel_type}_client", None)


async def _close_channel_locked(self: GatewayOrchestrator, channel_type: str) -> None:
    """Close *channel_type*'s live client, bounded, and forget it everywhere.

    Caller holds ``_channel_restart_lock``. Pops the handle, drops the
    dashboard's mirror registration (a closed transport must not stay
    registered for cross-surface sends), closes the client under
    ``registry.shutdown_tasks``' 2s bound, clears the legacy
    ``_<channel>_client`` mirror, and bumps the channel's restart
    generation so a start already connecting from an older document
    discards its result.
    """
    old = self._channel_handles.pop(channel_type, None)
    transports = getattr(self.dashboard_state, "channel_transports", None)
    if isinstance(transports, dict):
        transports.pop(channel_type, None)
    if old is not None:
        for closing in registry.shutdown_tasks({channel_type: old}, timeout=2.0):
            try:
                await closing
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "restart %s: closing the previous client failed",
                    channel_type,
                    exc_info=True,
                )
    setattr(self, f"_{channel_type}_client", None)
    self._channel_restart_gen[channel_type] = self._channel_restart_gen.get(channel_type, 0) + 1


async def _on_slack_config_change(self: GatewayOrchestrator, change: ConfigChange) -> None:
    """Apply Slack's hot fields from a config reload without touching the socket.

    Slack stays host-managed and never restarts here: its socket client is
    owned by ``_connect_slack`` under the ``channels`` governance gate (a
    deny must DROP the client), and its tokens live in the credential store,
    not in ``config.json``, so no ``slack.*`` write can change the
    connection. Everything else Slack reads is reconciled in place:

    * ``slack.tracking_channels`` / ``slack.open_channels`` -> the
      orchestrator sets AND the ``handler`` module globals, mutated in place
      so the Slack-native modal (which edits the same set objects) and a CLI
      write converge on one set.
    * ``slack.channels`` / ``slack.dm_activation`` / ``messaging.*`` /
      ``trusted_bot_*`` / ``home_tab_sessions_per_kind`` /
      ``forward_to_agent_callback`` -> the shared config object every Slack
      read goes through (``handler.slack_cfg()``), updated section-by-section
      in place so ``orch._cfg`` and ``handler._orch_cfg`` cannot diverge.
    * ``slack.reactions`` -> the phase-emoji table.
    * ``slack.observe_*`` -> the live ``ChannelHistory`` caps; observe-mode
      registration follows the new channel activations.
    * ``slack.allowed_enterprise_ids`` -> ``enterprise.reload_allowed_team_ids``
      (off-loop), which keeps the validated read as the sole source and fails
      closed on a degraded file.

    Authorization fail-closed: when the ``slack`` section was DISCARDED by the
    loader (``degraded_sections``) nothing under it is applied and the previous
    sets stay in force; the paths are raised as :class:`ConfigDeferred` so the
    watcher retries them once the document validates.
    """
    from kiro_crew.slack import handler as slack_handler

    new = change.new
    slack_paths = change.under("slack")
    if slack_paths and "slack" in new.degraded_sections:
        # Deferred, not dropped: the watcher retries these paths once the
        # file validates, so a revocation written alongside a malformed
        # field lands with the repair instead of waiting for the next diff.
        raise ConfigDeferred(slack_paths)
    if "slack.tracking_channels" in slack_paths:
        tracking = {
            c["channel_id"]
            for c in new.slack.tracking_channels
            if isinstance(c, dict) and c.get("channel_id")
        }
        self._tracking_channels.clear()
        self._tracking_channels.update(tracking)
        slack_handler.set_tracking_channels(self._tracking_channels)
    if "slack.open_channels" in slack_paths:
        self._open_channels.clear()
        self._open_channels.update(new.slack.open_channels)
        slack_handler.set_open_channels(self._open_channels)
    authz_paths = sorted(
        p
        for p in slack_paths
        if p in ("slack.trusted_bot_ids", "slack.open_channels", "slack.tracking_channels")
    )
    if authz_paths:
        # Widening sets: the change itself is the auditable event, and the
        # per-message admission decision is audited where it is made.
        sel().log_api_access(
            caller="config",
            operation="slack.authorization_config_change",
            outcome="allowed",
            source="config",
            resources=",".join(authz_paths),
        )
    slack_handler.adopt_slack_config(new)
    if self._cfg is not slack_handler.get_orch_cfg():
        # Two config objects in play (the handler global was never installed,
        # or was installed with another object): keep both current.
        slack_handler.copy_slack_fields(new, self._cfg)
    if change.touched("slack.reactions"):
        unknown = slack_handler.refresh_phase_emojis(new.slack.reactions)
        if unknown:
            logger.warning(
                "Ignoring unknown slack.reactions keys: %s",
                ", ".join(repr(k) for k in unknown),
            )
    history = self.channel_history
    if history is not None:
        if change.touched("slack.observe_max_messages", "slack.observe_ttl_hours"):
            _push_observe_limits(
                history, new.observe_max_messages, int(new.observe_ttl_hours * 3600)
            )
        if change.touched("slack.channels"):
            from kiro_crew.config.loader import ACTIVATION_OBSERVE

            observing = {
                ch_id
                for ch_id, ch_cfg in new.slack_channels.items()
                if ch_cfg.activation == ACTIVATION_OBSERVE
            }
            for ch_id in observing - set(history._observe_channels):
                history.set_observe(ch_id)
            for ch_id in set(history._observe_channels) - observing:
                history.unset_observe(ch_id)
    if "slack.allowed_enterprise_ids" in slack_paths and self._slack_enabled:
        from kiro_crew.slack import enterprise as slack_enterprise

        await asyncio.to_thread(slack_enterprise.reload_allowed_team_ids)
    if "slack.command" in slack_paths:
        logger.warning(
            "slack.command changed in config; the slash command is registered in "
            "the Slack app manifest, so this takes effect on the next gateway start"
        )


async def _adopt_channel_sections_from_watcher(
    self: GatewayOrchestrator, boot: "tuple[ChannelDescriptor, ...]"
) -> dict[str, str]:
    """Re-hoist every bootable channel once, before the enabled census.

    ``_<channel>_enabled`` comes from the hoist, so a channel switched on in
    the boot window must be re-hoisted BEFORE the permitted map is computed
    or it is never started. Returns the credentials it loaded (one off-loop
    ``.env`` read; the watcher does not read that file) for the per-channel
    pass :meth:`_adopt_channel_section_from_watcher` runs ahead of each
    start.
    """
    creds = await asyncio.to_thread(self._cfg.load_credentials)
    for desc in boot:
        self._adopt_channel_section_from_watcher(desc, creds)
    return creds


def _adopt_channel_section_from_watcher(
    self: GatewayOrchestrator, desc: "ChannelDescriptor", creds: dict[str, str]
) -> None:
    """Re-hoist ONE bootable channel from the watcher's CURRENT snapshot.

    The transports build from ``self._cfg`` -- the document loaded at
    construction -- while the config watcher is armed at dashboard init,
    several awaited steps earlier. A write landing before a channel's
    dispatcher exists reaches no applier for it, and the deferred
    channel-restart applier only re-runs boot-key restarts, so a live field
    -- an allow-list revocation -- would otherwise be missing from that
    transport's first authorization state until the next edit.

    Called by the start loop immediately before ``desc.start``, and
    synchronous on purpose: every ``maybe_start_<channel>`` constructs its
    dispatcher (which subscribes to the watcher) before its first await, so
    from this read onward the channel's own applier receives every later
    change. Channels start sequentially, so a per-channel read is what keeps
    a revocation that lands while an EARLIER channel is still connecting from
    being missed by a later one. Degraded sections are left on the boot copy:
    fail-closed, like every applier.
    """
    snap = live.snapshot()
    if snap is None or snap is self._cfg or desc.channel_type in snap.degraded_sections:
        return
    setattr(self._cfg, desc.channel_type, getattr(snap, desc.channel_type))
    getattr(self, f"_hoist_{desc.channel_type}")(snap, creds)


def _schedule_inbound_replay(self: GatewayOrchestrator) -> None:
    """Serialize a new spool replay after any pass already in flight."""
    previous = self._inbound_replay_task
    spool = inbound_spool.spool_path()

    async def _run() -> None:
        if previous is not None and not previous.done():
            await asyncio.gather(previous, return_exceptions=True)
        await self._replay_spooled_inbound(spool=spool)

    self._inbound_replay_task = asyncio.create_task(_run())


async def _replay_spooled_inbound(self: GatewayOrchestrator, *, spool: Path | None = None) -> None:
    """Notice inbound messages the shutdown gate refused before this start.

    The spool is written only at the refusal point, so every entry is a turn
    that provably never opened; the pass sends each sender an accurate
    restart notice quoting their message and removes the entry only once the
    send is confirmed — see :mod:`kiro_crew.messaging.inbound_spool`.
    Entirely best-effort: this runs as a detached boot task, so an exception
    escaping here would be an unretrieved task exception rather than
    anything a user could act on.

    *spool* is the spool file to read, resolved by the scheduler on the loop;
    ``None`` lets the pass resolve it itself, which is only right when the
    caller awaits the pass inline.
    """
    try:
        transports = dict(getattr(self.dashboard_state, "channel_transports", None) or {})
        # Slack deliberately stays out of ``channel_transports``: its ordinary
        # proactive sends use the gateway client and its own authorization
        # path. Refused-turn replay still needs that live client, so overlay a
        # pass-local adapter built from the CURRENT owner roster. Keeping it
        # local prevents every other shared send ladder from discovering a
        # Slack transport it was never designed to route through.
        if self.slack is not None:
            transports["slack"] = SlackTransport(
                self.slack,
                allowed_users=self._allowed_users,
            )
        await inbound_spool.replay_spooled(transports=transports, path=spool)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("inbound spool: replay pass failed", exc_info=True)


def _badge_unready_channels(
    self: GatewayOrchestrator, bootable: "tuple[ChannelDescriptor, ...]"
) -> None:
    """Give an ENABLED channel that cannot start a reason the dashboard shows.

    Each ``maybe_start_*`` returns None when a credential is missing, which is
    correct but silent: it sets no ``<channel>_connect_error``, so
    ``DashboardState.channel_status`` reports ``{connected: False, error: ""}``
    -- byte-identical to a channel nobody configured. System > Services filters
    that shape out (otherwise a Slack-only install grows seven meaningless
    rows), so an operator who enabled Telegram and forgot the token saw a
    healthy page and a bot that never answered.

    Reported here rather than by widening that filter, because the filter is not
    what is wrong: "enabled but not started" is a real state that owes a REASON,
    and naming the missing credential is what the operator can act on. Derived
    from ``channel_readiness``, which is descriptor-driven, so the next channel
    is covered by adding its descriptor.

    Runs in an executor: ``load_credentials`` reads the credential store.
    Best-effort by construction -- a diagnostic badge must never be able to stop
    a transport from booting.
    """
    try:
        from kiro_crew.channels import channel_readiness

        state = self.dashboard_state
        if state is None:
            return
        bootable_types = {d.channel_type for d in bootable}
        creds = self._cfg.load_credentials()
        for row in channel_readiness(self._cfg, creds):
            if row.channel_type not in bootable_types or row.ready or not row.enabled:
                continue
            missing = [*row.missing_credentials, *row.missing_config]
            setattr(
                state,
                f"{row.channel_type}_connect_error",
                f"Enabled but not started: missing {', '.join(missing)}"[:120],
            )
    except Exception:
        logger.debug("channel readiness badge unavailable", exc_info=True)
