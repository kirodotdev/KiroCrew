"""Each channel's boot state, the live config appliers, and the channel transports.

A hoist is the only place its channel's boot state is derived from the credential
store and that channel's config section, so ``restart_channel`` re-runs exactly
what boot ran. ``_register_config_appliers`` subscribes the channel restart and
Slack's hot fields to config changes, and ``_start_channel_transports`` starts
every non-Slack transport the ``channels`` scope allows.

Orchestrator fields read: ``_SLACK_APPLIER_PREFIXES``, ``_cfg``,
``_session_environments_ready``, ``_test_mode``.
Orchestrator fields written: ``_channel_handles``, ``_channel_transports_started``,
``_config_subs``, ``_discord_allowed_channel_ids``, ``_discord_allowed_thread_ids``,
``_discord_allowed_user_ids``, ``_discord_auto_thread``, ``_discord_bot_token``,
``_discord_client``, ``_discord_enabled``, ``_feishu_allow_group``,
``_feishu_allowed_group_ids``, ``_feishu_allowed_open_ids``, ``_feishu_app_id``,
``_feishu_app_secret``, ``_feishu_enabled``, ``_imessage_client``,
``_imessage_enabled``, ``_teams_allowed_emails``, ``_teams_app_id``,
``_teams_app_password``, ``_teams_client``, ``_teams_enabled``, ``_teams_tenant_id``,
``_telegram_allow_forum``, ``_telegram_allowed_forum_chat_ids``,
``_telegram_allowed_user_ids``, ``_telegram_bot_token``, ``_telegram_client``,
``_telegram_enabled``, ``_webex_allowed_emails``, ``_webex_bot_token``,
``_webex_client``, ``_webex_enabled``, ``_wecom_bot_id``, ``_wecom_enabled``,
``_wecom_secret``, ``_weixin_account_id``, ``_weixin_allowed_user_ids``,
``_weixin_base_url``, ``_weixin_client``, ``_weixin_dm_policy``, ``_weixin_enabled``,
``_weixin_token``, ``_whatsapp_client``, ``_whatsapp_enabled``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.slack.gateway import (
        CRED_DISCORD_BOT_TOKEN,
        CRED_FEISHU_APP_ID,
        CRED_FEISHU_APP_SECRET,
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_PASSWORD,
        CRED_MICROSOFT_APP_TENANT_ID,
        CRED_TELEGRAM_BOT_TOKEN,
        CRED_WEBEX_BOT_TOKEN,
        CRED_WECOM_BOT_ID,
        CRED_WECOM_SECRET,
        CRED_WEIXIN_TOKEN,
        ChannelDescriptor,
        ConfigChange,
        GatewayOrchestrator,
        KiroCrewConfig,
        Mapping,
        _channel_transport_permitted,
        _start_coder_retention,
        builtin_channel_descriptors,
        live,
        logger,
        maintenance_executor,
        registry,
        warn_if_channel_uncredentialed,
    )


# ── per-channel hoists ───────────────────────────────────────────────
# Each one is the ONLY place its channel's boot state is derived, so a
# restart re-derives exactly what boot derived. A hoist resets the legacy
# ``_<channel>_client`` mirror too: it runs only before a start (boot or
# restart), where the mirror is None by construction.
def _hoist_wecom(self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
    self._wecom_bot_id = creds.get(CRED_WECOM_BOT_ID, "")
    self._wecom_secret = creds.get(CRED_WECOM_SECRET, "")
    self._wecom_enabled = bool(cfg.wecom.enabled and self._wecom_bot_id and self._wecom_secret)


def _hoist_telegram(
    self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]
) -> None:
    # Telegram — the TELEGRAM_BOT_TOKEN credential (env/.env) overrides
    # cfg.telegram.bot_token; all other settings come from the typed
    # cfg.telegram dataclass (no ad-hoc config.json re-parse).
    self._telegram_bot_token = creds.get(CRED_TELEGRAM_BOT_TOKEN, "") or cfg.telegram.bot_token
    # telegram.accounts is deprecated and inert, and while it is set the channel
    # stays OFF rather than falling back to the top-level token. A config that
    # named accounts served ONLY those accounts — the top-level bot_token and
    # allowed_user_ids were shadowed — so serving them now would reopen a bot
    # the operator had stopped, under an allow-list they may have narrowed when
    # they migrated. Staying off preserves what the accounts block already did
    # and leaves re-enabling an explicit edit.
    self._telegram_enabled = bool(
        cfg.telegram.enabled and self._telegram_bot_token and not cfg.telegram.accounts
    )
    if cfg.telegram.accounts:
        logger.warning(
            "telegram.accounts is no longer served (%d account(s): %s) — multi-bot "
            "operation is withdrawn until a bot is a governable unit, and the "
            "Telegram channel stays OFF while telegram.accounts is set (these "
            "entries already shadowed the top-level token, so falling back to it "
            "would start a bot you had stopped). Remove the accounts block and put "
            "the one token you want served in telegram.bot_token; the entries are "
            "preserved in config until you do.",
            len(cfg.telegram.accounts),
            ", ".join(sorted(cfg.telegram.accounts)),
        )
    self._telegram_allowed_user_ids = list(cfg.telegram.allowed_user_ids)
    # Forum-topic gate (fail closed): serve supergroup forum Topics only when
    # allow_forum is set AND the supergroup's chat_id is allow-listed.
    self._telegram_allow_forum = bool(cfg.telegram.allow_forum)
    self._telegram_allowed_forum_chat_ids = list(cfg.telegram.allowed_forum_chat_ids)
    self._telegram_client = None


def _hoist_weixin(self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
    # Weixin (iLink personal WeChat) — the WEIXIN_TOKEN credential (env/.env)
    # overrides cfg.weixin.token. token + account_id come from the Settings
    # QR flow; deny-by-default DM policy from the typed cfg.weixin dataclass.
    self._weixin_token = creds.get(CRED_WEIXIN_TOKEN, "") or cfg.weixin.token
    self._weixin_account_id = cfg.weixin.account_id
    self._weixin_base_url = cfg.weixin.base_url
    self._weixin_dm_policy = cfg.weixin.dm_policy
    self._weixin_allowed_user_ids = list(cfg.weixin.allowed_user_ids)
    self._weixin_enabled = bool(
        cfg.weixin.enabled and self._weixin_token and self._weixin_account_id
    )
    self._weixin_client = None


def _hoist_whatsapp(
    self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]
) -> None:
    # WhatsApp (QR-linked personal account) — no credential: pairing state
    # lives in the channel's session DB, created by the Settings QR flow.
    # Enablement is config-only; maybe_start_whatsapp reports the missing
    # optional dependency or an unpaired session via the status badge.
    self._whatsapp_enabled = bool(cfg.whatsapp.enabled)
    self._whatsapp_client = None


def _hoist_feishu(self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
    # Feishu (Lark/飞书) — FEISHU_APP_ID / FEISHU_APP_SECRET (env/.env),
    # matching the Feishu developer console's own naming; everything else
    # from the typed cfg.feishu dataclass. Both are registered credentials,
    # so they are stripped from the agent subprocess environment by
    # sandbox._AGENT_DENIED_ENV_KEYS — the gateway is their only consumer.
    # Deny-by-default: an empty allowed_open_ids authorises nobody, and a
    # group chat needs BOTH allow_group and an allow-listed chat_id. The
    # client handle is owned by the channel registry (``kiro_crew.channels``),
    # which also closes it on shutdown.
    self._feishu_app_id = creds.get(CRED_FEISHU_APP_ID, "")
    self._feishu_app_secret = creds.get(CRED_FEISHU_APP_SECRET, "")
    self._feishu_enabled = bool(
        cfg.feishu.enabled and self._feishu_app_id and self._feishu_app_secret
    )
    self._feishu_allowed_open_ids = list(cfg.feishu.allowed_open_ids)
    self._feishu_allow_group = bool(cfg.feishu.allow_group)
    self._feishu_allowed_group_ids = list(cfg.feishu.allowed_group_ids)


def _hoist_discord(
    self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]
) -> None:
    # Discord — the DISCORD_BOT_TOKEN credential (env/.env) overrides
    # cfg.discord.bot_token; all other settings come from the typed
    # cfg.discord dataclass (mirrors the Telegram block above).
    self._discord_bot_token = creds.get(CRED_DISCORD_BOT_TOKEN, "") or cfg.discord.bot_token
    self._discord_enabled = bool(cfg.discord.enabled and self._discord_bot_token)
    self._discord_allowed_user_ids = [str(u) for u in cfg.discord.allowed_user_ids]
    self._discord_allowed_thread_ids = [str(t) for t in cfg.discord.allowed_thread_ids]
    self._discord_allowed_channel_ids = [str(c) for c in cfg.discord.allowed_channel_ids]
    self._discord_auto_thread = bool(cfg.discord.auto_thread)
    self._discord_client = None


def _hoist_webex(self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
    # Webex — the WEBEX_BOT_TOKEN credential (env/.env) overrides
    # cfg.webex.bot_token; all other settings come from the typed
    # cfg.webex dataclass (no ad-hoc config.json re-parse).
    self._webex_bot_token = creds.get(CRED_WEBEX_BOT_TOKEN, "") or cfg.webex.bot_token
    self._webex_enabled = bool(cfg.webex.enabled and self._webex_bot_token)
    self._webex_allowed_emails = list(cfg.webex.allowed_emails)
    self._webex_client = None


def _hoist_imessage(
    self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]
) -> None:
    # iMessage — no credential exists to hoist: the transport is the user's
    # own signed-in Messages.app, so enablement is the config flag alone.
    # Everything else is read from the typed cfg.imessage dataclass at start.
    self._imessage_enabled = bool(cfg.imessage.enabled)
    self._imessage_client = None


def _hoist_teams(self: GatewayOrchestrator, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
    # Teams — the MICROSOFT_APP_ID / MICROSOFT_APP_PASSWORD / _TENANT_ID
    # credentials (env/.env) override the typed cfg.teams fields; all other
    # settings come from the typed cfg.teams dataclass.
    self._teams_app_id = creds.get(CRED_MICROSOFT_APP_ID, "") or cfg.teams.app_id
    self._teams_app_password = creds.get(CRED_MICROSOFT_APP_PASSWORD, "") or cfg.teams.app_password
    self._teams_tenant_id = creds.get(CRED_MICROSOFT_APP_TENANT_ID, "") or cfg.teams.tenant_id
    self._teams_enabled = bool(
        cfg.teams.enabled and self._teams_app_id and self._teams_app_password
    )
    self._teams_allowed_emails = list(cfg.teams.allowed_emails)
    self._teams_client = None


#: Slack-owned top-level and section prefixes whose changes the Slack applier
#: reconciles. Everything Slack reads lives under ``slack.*`` in the
#: serialized document (``slack.channels``, ``slack.dm_activation`` and the
#: ``observe_*`` caps are emitted inside the ``slack`` section) plus the
#: transport switch under ``messaging``.
_SLACK_APPLIER_PREFIXES: tuple[str, ...] = ("slack", "messaging")


def _on_session_environment_config_change(self: GatewayOrchestrator, change: ConfigChange) -> None:
    if self._session_environments_ready and not self._test_mode:
        _start_coder_retention(change.new)


def _register_config_appliers(self: GatewayOrchestrator) -> None:
    """Subscribe this orchestrator's config appliers on the process watcher.

    Bound methods are held weakly by the watcher, so an orchestrator built
    and discarded by a test does not pin itself into the registry. The
    Subscription objects are kept on ``self`` so they live as long as the
    orchestrator does.
    """
    channel_prefixes = tuple(
        d.channel_type for d in registry.bootable(builtin_channel_descriptors())
    )
    self._config_subs = [
        live.subscribe(
            "session_environments.coder_url",
            callback=self._on_session_environment_config_change,
            name="GatewayOrchestrator.session_environments",
        ),
        live.subscribe(
            *channel_prefixes,
            callback=self._on_channel_config_change,
            name="GatewayOrchestrator.channel_restart",
        ),
        live.subscribe(
            *self._SLACK_APPLIER_PREFIXES,
            callback=self._on_slack_config_change,
            name="GatewayOrchestrator.slack",
        ),
        # Its own subscription: a deferral is merged per subscription, so a
        # discarded ``agent`` section is retried without keeping the Slack
        # paths stale beside it.
        live.subscribe(
            "agent.default_agent",
            callback=self._on_default_agent_config_change,
            name="GatewayOrchestrator.default_agent",
        ),
    ]


async def _start_channel_transports(
    self: GatewayOrchestrator, descriptors: "tuple[ChannelDescriptor, ...] | None" = None
) -> None:
    """Start each non-Slack transport, gated on the ``channels`` scope.

    Registry-driven (PR ③ of the channel-plugin RFC): the roster comes from
    :func:`kiro_crew.channels.builtin_channel_descriptors` and the loop
    lives in :mod:`kiro_crew.messaging.registry` — adding a channel does
    not edit this method. ``descriptors`` is injectable for tests.

    Every transport is a guarded no-op unless enabled + credentialed (its
    own ``maybe_start_*``), and is ADDITIONALLY gated on the ``channels``
    governance scope: a policy that denies the transport member keeps it from
    connecting at all, and its client stays ``None``. The member ids are
    IDENTICAL to the outbound chokepoints — outbound-send (``mcp_core``) and
    outbound cross-surface mirroring (``chat_runner``) — so one ``channels``
    allowlist governs a transport at connect time and on every outbound path
    (inbound receive is gated per-message by
    ``messaging.identity.channel_inbound_permitted``).

    Default-build invariant: with no policy governing ``channels`` (the
    standard OSS build) the gate permits, so every transport starts. Slack is
    a registry member too (``start=None``) but is gated in ``_connect_slack``
    rather than here,
    because it owns its own socket-client lifecycle (a deny must drop that
    client, not just skip a start call).

    Loop hygiene: ``_channel_transport_permitted`` reaches
    ``ProfileStore._ensure_fresh``, which stats/reads the profile files off
    disk — blocking I/O. This method runs on the gateway event loop (inside
    ``run()``), so the governance decisions are computed together in an
    executor BEFORE any transport is started; only the actual factory
    awaits stay on the loop.

    Enabled-only eval: the gate is queried ONLY for a transport whose
    ``_<member>_enabled`` is set (config-enabled + credentialed). A transport
    that is off never starts regardless of policy, so evaluating it would only
    emit a spurious deny-SEL for a channel that was never going to connect.
    A member not evaluated defaults to not-permitted (it is off anyway), so
    the no-policy default permits: every ENABLED transport resolves to permit
    and starts.
    """
    if descriptors is None:
        descriptors = builtin_channel_descriptors()
    boot = registry.bootable(descriptors)
    creds = await self._adopt_channel_sections_from_watcher(boot)
    enabled = {
        d.channel_type: bool(getattr(self, f"_{d.channel_type}_enabled", False)) for d in boot
    }
    # The enabled-only gate below never calls a factory whose flag is
    # False — for a disabled and an enabled-but-uncredentialed channel
    # alike — so a factory-level skip-reason log can never be reached.
    # Say WHY each channel is being skipped here, at the decision point.
    # Runs after KIROCREW_READY, outside the
    # boot-path window. Each row lists exactly the credential operands its
    # _<channel>_enabled predicate reads: telegram folds in the
    # deprecated-accounts stop (which already has its own warning at
    # config-load time, so pointing at the token would misname the actual
    # blocker), and teams deliberately omits the tenant id its predicate
    # never reads. whatsapp/imessage enablement is config-only (no
    # credential operand), so they have no row.
    uncredentialed_probe_rows: tuple[tuple[str, str, bool, tuple[tuple[str, bool], ...]], ...] = (
        (
            "wecom",
            "WeCom",
            self._cfg.wecom.enabled,
            (
                (CRED_WECOM_BOT_ID, bool(self._wecom_bot_id)),
                (CRED_WECOM_SECRET, bool(self._wecom_secret)),
            ),
        ),
        (
            "telegram",
            "Telegram",
            bool(self._cfg.telegram.enabled and not self._cfg.telegram.accounts),
            ((CRED_TELEGRAM_BOT_TOKEN, bool(self._telegram_bot_token)),),
        ),
        (
            "weixin",
            "WeChat",
            self._cfg.weixin.enabled,
            (
                (CRED_WEIXIN_TOKEN, bool(self._weixin_token)),
                ("weixin.account_id", bool(self._weixin_account_id)),
            ),
        ),
        (
            "feishu",
            "Feishu",
            self._cfg.feishu.enabled,
            (
                (CRED_FEISHU_APP_ID, bool(self._feishu_app_id)),
                (CRED_FEISHU_APP_SECRET, bool(self._feishu_app_secret)),
            ),
        ),
        (
            "discord",
            "Discord",
            self._cfg.discord.enabled,
            ((CRED_DISCORD_BOT_TOKEN, bool(self._discord_bot_token)),),
        ),
        (
            "webex",
            "Webex",
            self._cfg.webex.enabled,
            ((CRED_WEBEX_BOT_TOKEN, bool(self._webex_bot_token)),),
        ),
        (
            "teams",
            "Teams",
            self._cfg.teams.enabled,
            (
                (CRED_MICROSOFT_APP_ID, bool(self._teams_app_id)),
                (CRED_MICROSOFT_APP_PASSWORD, bool(self._teams_app_password)),
            ),
        ),
    )
    for channel_type, settings_name, cfg_enabled, presence in uncredentialed_probe_rows:
        warn_if_channel_uncredentialed(channel_type, settings_name, cfg_enabled, presence)
    loop = asyncio.get_running_loop()
    permitted = await loop.run_in_executor(
        maintenance_executor(),
        lambda: {m: (_channel_transport_permitted(m) if enabled[m] else False) for m in enabled},
    )
    # BEFORE starting: a channel that starts sets its own badge, so seeding
    # first lets a success overwrite this and leaves it only where the factory
    # bailed out early.
    await loop.run_in_executor(maintenance_executor(), self._badge_unready_channels, boot)
    # Second adoption, per channel, right before its factory: the first pass
    # above fed the enabled census; this one feeds each transport's first
    # authorization state with whatever landed while the channels ahead of it
    # were connecting. Synchronous by contract (see the hook's docstring).
    self._channel_handles = await registry.start_channels(
        self,
        descriptors,
        permitted,
        before_start=lambda desc: self._adopt_channel_section_from_watcher(desc, creds),
    )
    # A connection-parameter edit that landed while the channels were still
    # starting was deferred by the applier and sits in the watcher's stale
    # table; its next tick re-runs the applier against the current snapshot,
    # with the same per-channel restart the edit would have triggered a
    # moment later.
    self._channel_transports_started = True
    # Tell the sender of whatever the SHUTDOWN GATE refused on the way down
    # that it was never processed. Ordered AFTER the transports
    # because the notice is sent through the channel that received the
    # message, and detached from boot so a slow platform send cannot hold the
    # gateway's start open. The spool's location is resolved HERE, on the
    # loop, as the task is scheduled: the pass reads the spool on a worker
    # thread, and a path resolved there would name whatever data home the
    # environment holds at that later moment -- not the one this gateway
    # booted under. Under the test suite that moment is after the starting
    # test's pins are gone; five full runs left a lock file in the operator's
    # REAL data home that way.
    self._schedule_inbound_replay()
