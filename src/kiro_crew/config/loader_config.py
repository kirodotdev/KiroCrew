"""``KiroCrewConfig``, the root config object: load, serialize, save, and resolution.

An owner of the :mod:`kiro_crew.config.loader` facade; it never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import kiro_crew.config.resolution as _resolution
from kiro_crew import model_registry, model_scope, platform_compat
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    resolve_cc_permission_mode,
)
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_for
from kiro_crew.config.integration_sections import AppSettingsConfig, AwsConfig
from kiro_crew.config.loader_agents import resolve_crew_identity
from kiro_crew.config.loader_builders import (
    _restore_stored_harness_fields,
    _restore_stored_stt_provider,
    _stored_agent_dict,
)
from kiro_crew.config.loader_credentials import (
    _JIRA_TOKEN_RE,
    CREDENTIAL_KEYS,
    EnvFileWideEncodingError,
    _shadow_checked_env_keys,
    _warned_env_keys,
    env_path,
    read_env_text,
    resolve_env_credentials,
    warn_undecodable_env,
)
from kiro_crew.config.loader_document import (
    ConfigReadError,
    _bind_content_digest,
    _config_write_lock,
    _invalidate_config_cache,
    _notify_live_watch,
    read_config_for_update,
    stamp_config_meta,
    unsandboxed_exec_declared,
    write_config_atomically,
)
from kiro_crew.config.loader_locations import (
    _session_work_dir,
    config_local_path,
    config_path,
    read_config_text,
)
from kiro_crew.config.loader_snapshots import (
    _read_hardened_agent_spec,
    dispatch_kiro_agent,
    publish_agent_alias_snapshot,
    publish_autocompact_pct,
    publish_config_timezone,
)
from kiro_crew.config.paths import config_package_dir, kiro_agents_dir
from kiro_crew.config.resolution import _subtract_overlay
from kiro_crew.config.sections import (
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    BACKGROUND_WORKER_AGENTS,
    DEFAULT_MODEL,
    AgentConfig,
    ChannelConfig,
    ComputerUseConfig,
    CronHistoryConfig,
    DashboardConfig,
    DecisionsConfig,
    DiscordConfig,
    ExternalRegistryConfig,
    FeishuConfig,
    HeartbeatConfig,
    IMessageConfig,
    InstancesConfig,
    KiroCrewAgentConfig,
    KnowledgeConfig,
    McpConfig,
    McpGatewayConfig,
    MemoryConfig,
    MemoryStoreConfig,
    MessagingConfig,
    MonitoringConfig,
    PowerConfig,
    PublishConfig,
    ResourceLimitsConfig,
    SessionConfig,
    SessionEnvironmentsConfig,
    SessionSummaryConfig,
    SkillsConfig,
    SlackConfig,
    SttConfig,
    TaskRunnerConfig,
    TeamsConfig,
    TelegramConfig,
    TelemetryConfig,
    TunnelConfig,
    WakaTimeConfig,
    WatchdogConfig,
    WebexConfig,
    WeComConfig,
    WeixinConfig,
    WhatsAppConfig,
    WorkLedgerConfig,
    WorkspaceConfig,
    _meta,
    coerce_effort,
)
from kiro_crew.effort import is_valid_effort, model_supports_effort
from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path

logger = logging.getLogger("kiro_crew.config.loader")


@dataclass
class KiroCrewConfig:
    agent: AgentConfig = field(
        default_factory=AgentConfig,
        metadata=_meta("Agent", "Agent runtime configuration."),
    )
    session: SessionConfig = field(
        default_factory=SessionConfig,
        metadata=_meta("Session", "Session management settings."),
    )
    taskrunner: TaskRunnerConfig = field(
        default_factory=TaskRunnerConfig,
        metadata=_meta("Task Runner", "Task runner configuration."),
    )
    messaging: MessagingConfig = field(
        default_factory=MessagingConfig,
        metadata=_meta("Messaging", "Channel-neutral messaging transport settings."),
    )
    cron_history: CronHistoryConfig = field(
        default_factory=CronHistoryConfig,
        metadata=_meta("Cron History", "Cron execution history storage limits."),
    )
    memory: MemoryConfig = field(
        default_factory=MemoryConfig,
        metadata=_meta("Memory", "Memory and embedding configuration."),
    )
    knowledge: KnowledgeConfig = field(
        default_factory=KnowledgeConfig,
        metadata=_meta("Knowledge", "Knowledge Library ingestion settings."),
    )
    skills: SkillsConfig = field(
        default_factory=SkillsConfig,
        metadata=_meta("Skills", "Skill loading and matching configuration."),
    )
    session_summary: SessionSummaryConfig = field(
        default_factory=SessionSummaryConfig,
        metadata=_meta(
            "Session Summary",
            "Intent-level session summaries for the chat right panel. Off by default.",
        ),
    )
    session_environments: SessionEnvironmentsConfig = field(
        default_factory=SessionEnvironmentsConfig,
        metadata=_meta(
            "Session Environments",
            "Coder deployment URL and workspace profiles for remote session "
            "environments. Configuration only: saving provisions nothing.",
        ),
    )
    telemetry: TelemetryConfig = field(
        default_factory=TelemetryConfig,
        metadata=_meta(
            "Telemetry",
            "Metrics telemetry (local-first JSONL sink). Off by default.",
        ),
    )
    stt: SttConfig = field(
        default_factory=SttConfig,
        metadata=_meta("STT", "Speech-to-text transcription settings."),
    )
    computer_use: ComputerUseConfig = field(
        default_factory=ComputerUseConfig,
        metadata=_meta(
            "Computer Use",
            "Desktop automation tree/screenshot budgets. The primary enable is NOT "
            "here — it lives on the keystone computer_use.json.",
        ),
    )
    aws: AwsConfig = field(
        default_factory=AwsConfig,
        metadata=_meta(
            "AWS",
            "AWS settings for agent sessions: which named profile a project's "
            "sessions start with.",
        ),
    )
    mcp_gateway: McpGatewayConfig = field(
        default_factory=McpGatewayConfig,
        metadata=_meta("MCP Gateway", "Sidecar MCP broker that shares backends across sessions."),
    )
    mcp: McpConfig = field(
        default_factory=McpConfig,
        metadata=_meta(
            "MCP",
            "How MCP servers are found and launched — applies with the broker off too.",
        ),
    )
    instances: InstancesConfig = field(
        default_factory=InstancesConfig,
        metadata=_meta(
            "Instances", "Multi-instance management — manage/switch remote Kiro Crews over SSH."
        ),
    )
    heartbeat: HeartbeatConfig = field(
        default_factory=HeartbeatConfig,
        metadata=_meta("Heartbeat", "Heartbeat background task queue delivery defaults."),
    )
    monitoring: MonitoringConfig = field(
        default_factory=MonitoringConfig,
        metadata=_meta(
            "Monitoring",
            "How a session's choice between the two monitoring paths is framed. "
            "Neither path is gated by this section; both are armable with it at "
            "its default.",
        ),
    )
    decisions: DecisionsConfig = field(
        default_factory=DecisionsConfig,
        metadata=_meta(
            "Decisions",
            "Decision seam — typed decisions asked of a System One model at "
            "named points. Off until the dashboard owner consents in Settings > "
            "Developer > Feature Previews (the keystone decisions_consent.json, "
            "not a key here); until then every point returns None with no "
            "network call and no log write.",
        ),
    )
    watchdog: WatchdogConfig = field(
        default_factory=WatchdogConfig,
        metadata=_meta("Watchdog", "ACP per-session watchdog / liveness-oracle windows."),
    )
    work_ledger: WorkLedgerConfig = field(
        default_factory=WorkLedgerConfig,
        metadata=_meta("Work ledger", "Work-ledger nesting: how many levels may conduct."),
    )
    power: PowerConfig = field(
        default_factory=PowerConfig,
        metadata=_meta("Power", "What Knowledge Library sweeps do while on battery."),
    )
    resource_limits: ResourceLimitsConfig = field(
        default_factory=ResourceLimitsConfig,
        metadata=_meta(
            "Resource Limits",
            "Kernel confinement ceilings for spawned agents (POSIX rlimits and "
            "cgroup v2 scope properties). Shared keys mean different things to "
            "the two mechanisms -- see the per-field help.",
        ),
    )

    slack: SlackConfig = field(
        default_factory=SlackConfig,
        metadata=_meta("Slack", "Slack integration settings.", tags=["slack"]),
    )
    publish: PublishConfig = field(
        default_factory=PublishConfig,
        metadata=_meta(
            "Publish", "Artifact publishing controls (destinations allowlist).", tags=["publish"]
        ),
    )
    wecom: WeComConfig = field(
        default_factory=WeComConfig,
        metadata=_meta("WeCom", "WeCom (企业微信) AI-bot integration settings.", tags=["wecom"]),
    )
    telegram: TelegramConfig = field(
        default_factory=TelegramConfig,
        metadata=_meta("Telegram", "Telegram Bot API integration settings.", tags=["telegram"]),
    )
    weixin: WeixinConfig = field(
        default_factory=WeixinConfig,
        metadata=_meta(
            "WeChat", "Weixin (iLink personal WeChat) integration settings.", tags=["weixin"]
        ),
    )
    whatsapp: WhatsAppConfig = field(
        default_factory=WhatsAppConfig,
        metadata=_meta(
            "WhatsApp",
            "WhatsApp (QR-linked personal account) integration settings.",
            tags=["whatsapp"],
        ),
    )
    feishu: FeishuConfig = field(
        default_factory=FeishuConfig,
        metadata=_meta(
            "Feishu",
            "Feishu (Lark/飞书) channel configuration.",
            tags=["feishu"],
        ),
    )
    discord: DiscordConfig = field(
        default_factory=DiscordConfig,
        metadata=_meta("Discord", "Discord bot integration settings.", tags=["discord"]),
    )
    webex: WebexConfig = field(
        default_factory=WebexConfig,
        metadata=_meta("Webex", "Webex Messaging integration settings.", tags=["webex"]),
    )
    wakatime: WakaTimeConfig = field(
        default_factory=WakaTimeConfig,
        metadata=_meta(
            "WakaTime", "WakaTime dev-time tracking integration settings.", tags=["wakatime"]
        ),
    )
    teams: TeamsConfig = field(
        default_factory=TeamsConfig,
        metadata=_meta("Teams", "Microsoft Teams integration settings.", tags=["teams"]),
    )
    imessage: IMessageConfig = field(
        default_factory=IMessageConfig,
        metadata=_meta(
            "iMessage",
            "iMessage integration settings (macOS only, local bridge, no bot token).",
            tags=["imessage"],
        ),
    )
    dashboard: DashboardConfig = field(
        default_factory=DashboardConfig,
        metadata=_meta("Dashboard", "Dashboard UI settings."),
    )
    tunnel: TunnelConfig = field(
        default_factory=TunnelConfig,
        metadata=_meta("Tunnel", "AEA tunnel settings for remote dashboard access."),
    )
    hooks: dict = field(
        default_factory=dict,
        metadata=_meta(
            "Hooks",
            "Script hook definitions keyed by hook ID.",
            # Declared so the schema can carry the restart mark: the gate follows
            # a change at once, but the static grants it withholds are written
            # when the agent spec is rebuilt, so a saved change reports that a
            # restart is needed instead of claiming it applied. The field stays a
            # plain dict, so every other hooks key still round-trips untouched.
            properties={
                "confirm_tools": {
                    "type": "array",
                    "x-meta": {
                        "label": "Confirm tools",
                        "help": (
                            "Tool patterns, in the auto_approve_tools spelling "
                            "(@server, @server/tool*, Running: @server/*, *), that always "
                            "ask before running. A matching MCP server stays mounted but "
                            "loses its blanket auto-approve grant, and a match outranks an "
                            "auto_approve_tools grant at the gate. The gate follows a change "
                            "at once; a grant already in the agent spec is withdrawn at the "
                            "next restart, when the spec is rebuilt."
                        ),
                        "restart": True,
                    },
                },
            },
        ),
    )
    slack_channels: dict[str, ChannelConfig] = field(
        default_factory=dict,
        metadata=_meta("Slack Channels", "Per-channel activation config."),
    )
    slack_dm_activation: str = field(
        default=ACTIVATION_ALWAYS,
        metadata=_meta("Slack DM Activation", "Default activation mode for DMs."),
    )
    observe_max_messages: int = field(
        default=200,
        metadata=_meta("Observe Max Messages", "Max messages per observe-mode channel."),
    )
    observe_ttl_hours: float = field(
        default=168.0,
        metadata=_meta("Observe TTL Hours", "Hours to keep observe history."),
    )
    agents: dict[str, KiroCrewAgentConfig] = field(
        default_factory=dict,
        metadata=_meta("Agents", "Named Kiro Crew agent definitions."),
    )
    default_agent: str = field(
        default="",
        metadata=_meta("Default Agent", "Active Kiro Crew agent name from the agents section."),
    )
    workspaces: dict[str, WorkspaceConfig] = field(
        default_factory=dict,
        metadata=_meta("Workspaces", "Named workspace definitions."),
    )
    default_workspace: str = field(
        default="default",
        metadata=_meta("Default Workspace", "Active workspace name."),
    )
    memory_stores: dict[str, MemoryStoreConfig] = field(
        default_factory=dict,
        metadata=_meta("Memory Stores", "Named memory store definitions."),
    )
    default_memory_store: str = field(
        default="default",
        metadata=_meta("Default Memory Store", "Fallback memory store name."),
    )
    apps: dict[str, AppSettingsConfig] = field(
        default_factory=dict,
        metadata=_meta("Apps", "Per-app user settings, keyed by the app's manifest name."),
    )
    auto_update: bool = field(
        default=True,
        metadata=_meta(
            "Auto Update",
            "Where the install can apply updates, true installs them once no work is "
            "running and restarts; false only notifies. Elsewhere it has no effect. "
            "On those same installs a policy minimum version applies regardless.",
        ),
    )
    #: Opt-in for the Connections gallery, which is merged but held for a later
    #: release. A real field rather than an unmodelled top-level key because the
    #: browser is the consumer: the frontend reads this flag live off ``GET
    #: /api/config/kirocrew`` (``useConnectionsUi.ts``), and that response drops
    #: every key the core does not model — ``_masked_config_dict`` cannot tell
    #: whether an unmodelled value is a secret, so it strips them all rather
    #: than leak one. Being schema-known is therefore what makes the flag
    #: reachable at all; it also stops ``validation`` reporting the operator's
    #: own documented setting as an "unrecognized top-level key".
    #:
    #: Parsed through ``_safe_bool`` and defaulting False: a value Kiro Crew
    #: cannot read must mean "keep the held surface hidden", never the reverse
    #: (same posture as ``computer_use.cursor_motion``). The frontend predicate
    #: is a strict ``=== true``, so coercing e.g. the string ``"true"`` would
    #: only make the two ends disagree about what was configured.
    connections_ui: bool = field(
        default=True,
        metadata=_meta(
            "Connections UI",
            "Show the Connections services gallery (set false to hide it).",
        ),
    )
    #: Top-level sections that were PRESENT on disk but not a JSON object, and
    #: were therefore coerced to defaults by :meth:`load`.
    #:
    #: The loader's whole contract is to degrade rather than raise, which is
    #: right for an ordinary consumer and dangerous for one reading a SECURITY
    #: value out of a section: a coerced-away section is indistinguishable from
    #: "the operator configured nothing", so a narrowing silently becomes
    #: allow-all.
    #:
    #: A consumer cannot recover this by re-reading the file, which is why the
    #: signal has to live here: ``load()`` runs a migration that REWRITES
    #: ``config.json`` in normalized form, so by the time any gate looks, the
    #: malformed section is gone from disk. The evidence only exists during the
    #: parse that discarded it.
    #:
    #: Excluded from serialization (``repr=False``, and the config writers work
    #: from explicit field lists) — it describes THIS read, not the operator's
    #: settings, and must never be written back into their config. The leading
    #: underscore keeps it out of the config schema/baseline machinery, which
    #: skips private fields (same convention as ``_extra_sections``); consumers
    #: read the :attr:`degraded_sections` property.
    _degraded_sections: frozenset[str] = field(
        default_factory=frozenset,
        repr=False,
        compare=False,
    )

    #: True when the load that built this instance found ``config.json`` present
    #: but unparseable, so every base setting here is a DEFAULT, not the user's.
    #: Describes this read, like ``_degraded_sections``; never serialized.
    #: :meth:`save` refuses such an instance even if the file has been repaired
    #: since, because publishing it would still replace the repaired settings.
    _base_unreadable: bool = field(default=False, repr=False, compare=False)

    #: True when the load that built this instance found ``config.local.json``
    #: present but unparseable, so nothing the overlay sets is here. Describes this
    #: read, like ``_base_unreadable``; never serialized. ``degraded_sections`` cannot
    #: answer this for one load: its whole-config marker lasts the process.
    _overlay_unreadable: bool = field(default=False, repr=False, compare=False)

    @property
    def degraded_sections(self) -> frozenset[str]:
        """Sections this load discarded (see ``_degraded_sections``)."""
        return self._degraded_sections

    timezone: str = field(
        default="",
        metadata=_meta(
            "Timezone",
            "IANA timezone name (e.g. 'America/Los_Angeles'). "
            "Used to display cron schedules in local time.",
        ),
    )
    snapshot_dir: str = field(
        default="",
        metadata=_meta(
            "Snapshot Directory",
            "Directory for kirocrew snapshot output. "
            "Defaults to ~/.kiro/crew/snapshots if empty.",
        ),
    )
    registries: list[ExternalRegistryConfig] = field(
        default_factory=list,
        metadata=_meta(
            "Registries",
            "External app registries (org-owned repos). " "Each entry: {name, repo, branch}.",
        ),
    )
    # Unknown top-level config.json sections captured verbatim at load() and
    # re-emitted by to_dict() so a section this core does not model (e.g. an
    # edition-contributed section written by a companion) is NOT silently
    # dropped on the first save()/PATCH round-trip. Excluded from the JSON
    # schema by the leading underscore (build_json_schema skips private fields);
    # populated only from disk. This is the data-preservation half of the
    # ConfigSchemaContributor seam — a companion writes its section, the core
    # round-trips it untouched.
    _extra_sections: dict = field(default_factory=dict)
    # Unknown keys found INSIDE a modelled section, captured at load() and
    # re-emitted by to_dict() for the same reason as _extra_sections one level
    # up: a build that stops modelling ``<section>.<key>`` (a rename, a removal,
    # an older config written by a newer edition) would otherwise erase the
    # operator's value on the next save() of any kind, with no backup on that
    # path. Shape: {section: {key: value}}. Populated only from disk, and only
    # ever fills a key the emitted section LACKS, so it cannot clobber a
    # live value. See resolution.capture_extra_section_keys for what counts as
    # unknown (a schema-modelled key that validation rejected stays dropped).
    _extra_keys: dict = field(default_factory=dict)

    def channel_config(self, channel_id: str) -> ChannelConfig:
        """Return the config for *channel_id*, falling back to defaults.

        DMs (channel IDs starting with ``D``) use ``slack_dm_activation``.
        Group channels use ``mention`` unless overridden in ``slack_channels``.
        """
        if channel_id in self.slack_channels:
            return self.slack_channels[channel_id]
        if channel_id.startswith("D"):
            return ChannelConfig(activation=self.slack_dm_activation)
        return ChannelConfig(activation=ACTIVATION_MENTION)

    @property
    def slack_enterprise_ids(self) -> set[str]:
        """Extra allowed enterprise IDs from ``slack.allowed_enterprise_ids``."""
        return set(self.slack.allowed_enterprise_ids)

    @classmethod
    def load(cls) -> KiroCrewConfig:
        """Load config from ~/.kiro/crew/config.json, falling back to defaults.

        If ``config.local.json`` exists alongside ``config.json``, it is
        deep-merged on top. User overrides in the local file win over whatever
        is written to ``config.json``, including a whole-document replace
        (``kirocrew config set --file``, or a dashboard import in Replace mode).

        The overlay is applied at load time but NOT persisted back by
        ``save()`` — only the base config is written to ``config.json``.
        """
        # The ordering ticket comes back from the resolve step, drawn BEFORE the
        # read, so a concurrent newer load cannot be overwritten by this one
        # finishing later (see publish_autocompact_pct) and this method adds no
        # filesystem I/O of its own on the event loop.
        cfg, _autocompact_ticket, _content_digest = cls._load_resolved()
        # Push the MCP search-path setting to its consumer. It is PUSHED rather
        # than read there because kiro_crew.env.mcp_search_path is reached from
        # the event loop by every MCP probe and by the agent-config resolver, so
        # a config read on that side would stat/read/validate config.json on the
        # loop. Done here rather than inside the load pipeline so EVERY load
        # publishes -- including one that found neither config file readable, which
        # must CLEAR an already-published snapshot rather than leave a deleted
        # directory resolving commands. Lazy import: env
        # must stay off this module's import graph.
        try:
            from kiro_crew.env import publish_config_path_dirs

            publish_config_path_dirs(cfg.mcp.extra_path_dirs)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; the
            # search path simply keeps its previous (or empty) contribution.
            logger.warning("Publishing mcp.extra_path_dirs failed: %s", e)
        # Publish the alias table for the same reason and in the same place: the
        # display-side resolver (:func:`resolve_effective_agent`) runs on the
        # event loop for every slots frame, so it must never reach for
        # config.json itself. Here rather than in the load pipeline so EVERY load
        # publishes -- including a degraded-defaults one, which must overwrite a
        # richer previous snapshot rather than leave the resolver honoring aliases
        # that do not load.
        try:
            publish_agent_alias_snapshot(cfg)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; the
            # resolver simply keeps reporting no divergence.
            logger.warning("Publishing agent alias snapshot failed: %s", e)
        # Same placement and same reason again: the compaction gate reads this
        # after every turn on the event loop, and publishing on EVERY return path
        # is what lets a CLI write reach a gateway that is already running.
        try:
            publish_autocompact_pct(cfg, _autocompact_ticket)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; the gate
            # keeps using the threshold it already had.
            logger.warning("Publishing autocompact threshold failed: %s", e)
        # Same placement and same reason once more: cron resolves this on the
        # event loop on every timer tick (see publish_config_timezone), and
        # publishing on EVERY return path is what lets a settings change reach a
        # gateway that is already running. Shares the autocompact ticket
        # deliberately -- both describe the same read of the same files, so they
        # must order identically against a concurrent load.
        try:
            publish_config_timezone(cfg, _autocompact_ticket)
        except Exception as e:  # pragma: no cover - defensive
            # A publish failure must never make the config unloadable; cron
            # keeps using the zone it already had.
            logger.warning("Publishing config timezone failed: %s", e)
        # The digest of the bytes this object was parsed from, recorded ON the object
        # so provenance travels with it and this method's signature stays as every
        # caller has it. ``None`` when the read could not name any bytes -- the files
        # were unreadable, or the document was unusable and field defaults stand in --
        # and an object built any other way has no attribute at all, which reads the
        # same. :func:`config_content_stamp` is what it is later compared against.
        _bind_content_digest(cfg, _content_digest)
        return cfg

    @classmethod
    def _load_resolved(cls) -> tuple[KiroCrewConfig, int, str | None]:
        """Resolve the config from disk (or defaults). See :meth:`load`.

        Split out so :meth:`load` owns the post-resolution publication. Runs the load
        pipeline -- :func:`read_config_document`, :func:`build_config`, then
        :func:`persist_write_back` -- and returns the
        config PLUS the ordering ticket drawn before the read, which is what lets
        :meth:`load` publish the compaction threshold in the correct order relative
        to a concurrent load without any filesystem I/O of its own on the event
        loop, and the digest of the bytes the config was parsed from.
        """
        # The pipeline sits above this module: it builds instances of this class.
        from kiro_crew.config.loader_pipeline import (
            build_config,
            persist_write_back,
            read_config_document,
        )

        doc = read_config_document()
        cfg = build_config(doc, cls)
        persist_write_back(cfg, doc)
        return cfg, doc.ticket, doc.content_digest

    def to_dict(self) -> dict:
        """Serialize config to the JSON structure used by config.json."""
        from dataclasses import asdict

        d: dict = {
            "agent": _stored_agent_dict(self.agent),
            "session": asdict(self.session),
            "memory": asdict(self.memory),
            "slack": asdict(self.slack),
            "publish": asdict(self.publish),
            "telegram": asdict(self.telegram),
            "discord": asdict(self.discord),
            "webex": asdict(self.webex),
            "wakatime": asdict(self.wakatime),
            "wecom": asdict(self.wecom),
            "weixin": asdict(self.weixin),
            "whatsapp": asdict(self.whatsapp),
            "feishu": asdict(self.feishu),
            "teams": asdict(self.teams),
            "imessage": asdict(self.imessage),
            "dashboard": asdict(self.dashboard),
            "tunnel": asdict(self.tunnel),
            "hooks": self.hooks,
            "agents": {name: asdict(agent_cfg) for name, agent_cfg in self.agents.items()},
            "default_agent": self.default_agent,
            "workspaces": {name: asdict(ws_cfg) for name, ws_cfg in self.workspaces.items()},
            "default_workspace": self.default_workspace,
            "memory_stores": {name: asdict(ms_cfg) for name, ms_cfg in self.memory_stores.items()},
            "default_memory_store": self.default_memory_store,
            "apps": {name: asdict(app_cfg) for name, app_cfg in self.apps.items()},
            "stt": asdict(self.stt),
            "computer_use": asdict(self.computer_use),
            "aws": asdict(self.aws),
            "instances": asdict(self.instances),
            "mcp_gateway": asdict(self.mcp_gateway),
            "mcp": asdict(self.mcp),
            "taskrunner": asdict(self.taskrunner),
            "watchdog": asdict(self.watchdog),
            "work_ledger": asdict(self.work_ledger),
            "power": asdict(self.power),
            "resource_limits": asdict(self.resource_limits),
            "messaging": asdict(self.messaging),
            "cron_history": asdict(self.cron_history),
            "knowledge": asdict(self.knowledge),
            "heartbeat": asdict(self.heartbeat),
            "monitoring": asdict(self.monitoring),
            "decisions": asdict(self.decisions),
            "skills": asdict(self.skills),
            "session_summary": asdict(self.session_summary),
            "session_environments": asdict(self.session_environments),
            "telemetry": asdict(self.telemetry),
            "snapshot_dir": self.snapshot_dir,
            "timezone": self.timezone,
            "auto_update": self.auto_update,
            # Emitted unconditionally, like every other modelled top-level
            # value: _KNOWN_CONFIG_SECTIONS must equal the key set to_dict()
            # writes (test_known_sections_equals_emitted_sections), and a key
            # listed there but not emitted would be excluded from
            # _extra_sections capture AND dropped here — losing the operator's
            # opt-in on the first save().
            "connections_ui": self.connections_ui,
        }
        # External registries (always serialized so save() round-trips the field)
        d["registries"] = [asdict(r) for r in self.registries]
        # ``mcp_gateway.stub_servers`` is the ROSTER in the file but the EFFECTIVE
        # set on the dataclass, so a straight ``asdict`` round-trip would rewrite
        # the file without the servers the operator opted out of -- turning a
        # reversible deviation into a permanent deletion, on any unrelated save().
        # Emit the roster the load actually read, and drop the private carrier so
        # it never appears as a config key.
        _gw_section = d.get("mcp_gateway")
        if isinstance(_gw_section, dict):
            _gw_section["stub_servers"] = list(self.mcp_gateway.stub_roster)
            _gw_section.pop("_stub_roster", None)
        # ``telegram.accounts`` is deprecated and inert. It is kept on disk ONLY
        # so an operator's named-account tokens survive the next save(); an
        # empty map protects nothing, and writing it back materializes a
        # deprecated key into every config Kiro Crew has ever saved -- which
        # validation then announces as a deprecation on every launch, to an
        # operator who never wrote it. Emit the map only when it holds accounts.
        _tg_section = d.get("telegram")
        if isinstance(_tg_section, dict) and not _tg_section.get("accounts"):
            _tg_section.pop("accounts", None)
        # Re-emit unknown/edition-contributed top-level sections captured at
        # load() so save()/PATCH does not silently drop them. A known section
        # never appears here (only keys absent from d are restored), so this can
        # never clobber a core section with a stale captured copy.
        for _k, _v in self._extra_sections.items():
            if _k not in d:
                d[_k] = _v
        # Preserve per-channel activation settings on round-trip
        slack_section = d.setdefault("slack", {})
        if self.slack_channels:
            slack_section["channels"] = {
                ch_id: asdict(cfg) for ch_id, cfg in self.slack_channels.items()
            }
        if self.slack_dm_activation != ACTIVATION_ALWAYS:
            slack_section["dm_activation"] = self.slack_dm_activation
        slack_section["observe_max_messages"] = self.observe_max_messages
        if self.slack.trusted_bot_ids:
            slack_section["trusted_bot_ids"] = sorted(self.slack.trusted_bot_ids)
        else:
            slack_section.pop("trusted_bot_ids", None)
        slack_section["observe_ttl_hours"] = self.observe_ttl_hours
        # Restore unknown keys captured INSIDE a modelled section (and inside the
        # named records of agents/workspaces/memory_stores). Last in the method
        # on purpose: every conditional and derived emission above has already
        # run, so the fill-only rule reads the final truth and a captured copy
        # can only ever FILL a gap, never overwrite a live value or undo a
        # deliberate deletion.
        _resolution.restore_extra_section_keys(d, self._extra_keys)
        return d

    def save(self) -> None:
        """Write current config to ~/.kiro/crew/config.json.

        Stamps a ``meta`` block with the current version and timestamp
        so we can tell which build last touched the file.

        Values that exist in ``config.local.json`` are stripped from the
        output to prevent overlay settings from leaking into the base file.

        **The write happens under the sidecar advisory lock** — the same
        ``<config>.lock`` that :func:`update_config_locked` holds.
        Without it, this whole-document rename could land INSIDE another
        writer's read-modify-write (a CLI ``update_config_locked`` holder, a
        boot refresh, a second gateway), and whichever renamed second
        published a document that never saw the other's change. The lock
        serializes the writes; it does NOT re-read the file — ``save()``
        still publishes this object's in-memory snapshot.

        **The staleness contract that follows.** Because the snapshot is
        taken at ``load()`` and only the rename is locked, any suspension
        point (an ``await``, a lock wait, a long computation) between the
        load and this call is a window in which a concurrent writer's change
        is silently overwritten by the eventual publish. ``save()`` is
        therefore ONLY for single-flow callers with no suspension between
        their load and their save — CLI one-shots and the boot default-config
        write. Every read-modify-write flow, and every dashboard/coroutine
        caller, belongs on :func:`update_config_locked` (via
        ``dashboard/chat_utils.run_config_write``), which holds the flock
        across the WHOLE read-mutate-write transaction. No coroutine calls
        ``save()`` inline (``TestNoInlineSaveOnTheEventLoop``) and no dashboard
        module reaches it at all, offloaded or not
        (``TestNoDashboardModuleSavesTheWholeConfig``).

        **Fails closed on an unreadable file.** If ``config.json`` exists but does
        not parse, this raises :class:`ConfigReadError` and writes nothing: an
        instance loaded from such a file holds defaults, not the user's settings.

        **Async callers must offload.** A contended POSIX ``flock`` blocks
        the calling thread for as long as the holder keeps it, which on the
        event-loop thread stalls the whole gateway.
        """

        d = self.to_dict()

        # Strip the no-backend exec permission when the operator never DECLARED it.
        #
        # ``sandbox_allow_unsandboxed_exec`` is the one field whose ABSENCE is
        # load-bearing: an absent key resolves through
        # ``unsandboxed_exec_platform_default()`` (allow where no backend can be
        # installed, fail-closed elsewhere), and absence is also what tells
        # ``kirocrew setup`` there is still a decision to surface and the SEL audit
        # that the platform rather than an operator permitted the spawn. Publishing
        # a whole-document snapshot materializes every field, so without this strip
        # any ``save()`` — the boot default-config write, a CLI one-shot — would
        # freeze the resolved value in as a DECLARATION: writing ``false`` on such a
        # host refuses every agent subprocess nobody asked to refuse, and writing
        # ``true`` fakes a consent that was never given, skipping the very prompt
        # that makes the posture explicit.
        #
        # Declaration is read from disk rather than tracked in memory because that is
        # the same question every other consumer asks
        # (``unsandboxed_exec_declared()``), so the writer cannot disagree with the
        # reader about one host. On a fresh install there is no file yet, which reads
        # as undeclared and is exactly right: the document this call creates should
        # not contain the key at all.
        if not unsandboxed_exec_declared():
            agent_section = d.get("agent")
            if isinstance(agent_section, dict):
                agent_section.pop("sandbox_allow_unsandboxed_exec", None)
        # The harness fields go back to disk exactly as ``config.json`` stores
        # them, never from the in-memory value, which is RESOLVED: "no choice"
        # (absent or "") reads as today's default, and a pick this deployment
        # cannot serve (a policy-denied ``kas``) reads as the Kiro CLI floor.
        # Publishing it would turn no choice into a choice, or overwrite the
        # user's pick with the floor. ``to_dict()`` keeps the resolved value for
        # every reader (``config get``, the dashboard response, the live
        # watcher); a pick reaches disk through the delta writers (``config
        # set``, the dashboard PATCH).
        _restore_stored_harness_fields(d)
        # A stored edition STT provider this snapshot degraded to ``off`` (it was
        # loaded before the edition registered it) is written back as stored.
        _restore_stored_stt_provider(d)
        # Strip overlay-owned values so they don't leak into config.json
        local_path = config_local_path()
        if local_path.is_file():
            try:
                raw_local = json.loads(read_config_text(local_path))
                if isinstance(raw_local, dict):
                    # Compare CANONICAL values for resource_limits.
                    # _subtract_overlay recognises an overlay-owned leaf only when
                    # the emitted value EQUALS the raw overlay value, and this
                    # section is normalized on load (512.5 -> 512, a refused value
                    # -> None). A raw comparison therefore stops matching and
                    # copies an overlay-owned limit into the base file, which is
                    # the leak the subtraction exists to prevent. Only the keys the
                    # overlay actually names are canonicalized: feeding the whole
                    # dataclass would add eight `None` leaves the operator never
                    # wrote and invite deletions they did not ask for.
                    rl_overlay = raw_local.get("resource_limits")
                    if isinstance(rl_overlay, dict):
                        canonical = asdict(ResourceLimitsConfig.from_raw(rl_overlay))
                        raw_local = {
                            **raw_local,
                            "resource_limits": {
                                k: canonical[k] for k in rl_overlay if k in canonical
                            },
                        }
                    d = _subtract_overlay(d, raw_local)
            except (json.JSONDecodeError, OSError):
                pass
        # An equal overlay value must not erase a choice the base file owns.
        _restore_stored_harness_fields(d)

        # Atomic + mode-preserving: a concurrent reader must never observe a
        # half-written config, and the write must not widen who can read a file
        # that may hold inline credentials. See write_config_atomically.
        #
        # Resolve a symlinked config BEFORE locking (same logic as
        # update_config_locked) so the sidecar sits beside the ACTUAL file the
        # rename will replace — a lock beside the symlink would not serialize
        # against a writer that resolved first.
        p = config_path()
        try:
            if p.is_symlink():
                p = p.resolve()
        except OSError:
            pass
        with _config_write_lock(p):
            # Never publish over a file that does not parse. load() answers such a
            # file with DEFAULTS, so this snapshot would replace every setting the
            # user has with them -- silently, and with the endpoint reporting
            # success. Same fail-closed rule as update_config_locked; a missing
            # file (the create-default callers) is still written.
            if self._base_unreadable and p.exists():
                raise ConfigReadError(
                    f"refusing to save over {p}: this snapshot was loaded while the "
                    "file did not parse, so it holds defaults, not the saved settings"
                )
            if p.exists():
                read_config_for_update(p)
            write_config_atomically(p, stamp_config_meta(d))
        # Drop the validated-data cache so the next load() re-reads this write.
        # mtime-keying already detects the change; this makes it immediate even
        # if the filesystem mtime resolution is coarse.
        _invalidate_config_cache()
        _notify_live_watch()

    @staticmethod
    def _resolve_agent_model() -> str:
        """Read model from installed agent config, falling back to bundled defaults.

        The installed spec is read through
        ``agent_discovery._read_agent_spec`` — the one hardened reader for
        agent configs — not a bare ``read_text``: the agents directory is
        user-writable and shared with other tools, so an oversized file must
        be refused at the read cap instead of slurped onto whatever surface
        asked for its effective model, and a symlink resolving into a
        sensitive path must not donate its target's JSON here.
        """
        agent_json = kiro_agents_dir() / "kirocrew.json"
        if agent_json.is_file():
            data = _read_hardened_agent_spec(agent_json)
            if data:
                model = data.get("model", "")
                if model:
                    return model
        # Bundled defaults.json
        bundled = config_package_dir() / "defaults.json"
        if bundled.is_file():
            try:
                bundled_data = json.loads(bundled.read_text(encoding="utf-8"))
                model = bundled_data.get("model", "")
                if model:
                    return model
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_MODEL

    def acp_effective_model(
        self,
        agent: str | None,
        model_override: str | None,
        global_model: str | None = None,
        namespace: str | None = None,
    ) -> str:
        """The model id the ACP factory selects — what its effort gate keys on.

        This IS the factory's selection, extracted so the spawn-side effort
        verdict (``kiro_crew.subagent._spawn_effective_model``) shares the code
        instead of mirroring it — a mirror that drifts reports a false
        ``effort_applied``/``effort_dropped`` receipt, worse than silence.

        Precedence: ``model_override`` (an explicit caller model or the value
        the session layer resolved) > a named agent's own kiro ``model`` pin
        (``kirocrew`` itself and the no-agent case use the global directly) >
        the collapsed global. ``global_model`` lets the factory pass its
        build-time collapsed ``agent.model``; when omitted it is recomputed
        the same way (``agent.model``, collapsed through
        :meth:`_resolve_agent_model` when it is the ``auto`` sentinel).

        The result is translated into the namespace of the backend that will
        actually be asked to run it, and the namespace is asked for as a
        CAPABILITY (``SessionCapabilities.model_id_namespace``) rather than
        inferred from the harness's name: a backend on its own provider namespace
        goes through ``to_provider_id`` into that namespace, and one on the native
        ``acp`` namespace goes through ``model_registry.to_acp_id`` (canonical keys
        become kiro ids). ``auto`` collapses to ``""`` either way.

        Keying the translation on the backend is what the warm-pool model-switch
        path already does (``session_allocation``).
        Hardcoding ``to_acp_id`` here meant a COLD start handed the claude
        adapter a kiro-namespaced id — which its ``set_config_option`` rejects,
        and which nothing withheld, because the pre-wire availability guard is
        deliberately kiro-only (the two backends advertise in different
        namespaces, so that check cannot be widened — see
        ``acp.client.model_is_unusable``). A warm claim of the same pinned model
        translated correctly, so the failure depended on whether a pooled
        process happened to exist. Not reachable from the default config:
        ``auto`` pins nothing, and the client skips the send entirely.

        ``to_acp_id``, NOT ``to_provider_id``, is the non-claude choice because
        kiro serves the registry aliases as distinct real models — see its
        docstring. ``""`` means nothing is pinned anywhere: the backend resolves
        the model itself and the effort overlay cannot be keyed.
        """
        if global_model is None:
            global_model = self.agent.model
            if global_model == DEFAULT_MODEL:
                global_model = self._resolve_agent_model()
        # Kiro's namespace is ``acp``, so a supplied member route is unambiguous.
        if namespace is None:
            namespace = capabilities_for(self.agent.acp_backend).model_id_namespace
        candidates: list[tuple[str, str]] = []
        if model_override:
            candidates.append((model_override, "model_override"))
        if agent and agent != "kirocrew":
            candidates.append((self._resolve_named_agent_model(agent), f"agent spec {agent}"))
        candidates.append((global_model, "agent.model"))
        # A pin says WHAT was picked, never for WHICH harness, so a backend switch
        # hands the next session a model the new harness never served. Scope each
        # precedence tier to the namespace that will run it and let an out-of-scope
        # tier defer to the next candidate. The surviving pin is scoped BEFORE
        # translation because translating a foreign id can make it look native.
        for candidate, pin_source in candidates:
            m = model_scope.scoped_pin(candidate, namespace, source=pin_source)
            if m:
                break
        else:
            return ""
        if m == DEFAULT_MODEL:
            # The empty string is the nothing-pinned spelling every tier below reads.
            return ""
        # ``to_acp_id``, not ``to_provider_id``, is the kiro-family choice because
        # kiro serves the registry aliases as distinct real models.
        translated = (
            model_registry.to_acp_id(m)
            if namespace == MODEL_NAMESPACE_ACP
            else model_registry.to_provider_id(m, namespace)
        )
        # Translation may RE-SPELL a pin, never RE-POINT it at another model. A
        # different id from a namespace whose vocabulary does not claim this pin
        # is an alias folding onto a substitute, and a pin the user picked
        # outranks a substitution they cannot see. A namespace the registry does
        # not model answers identically, so it re-points nothing.
        if translated != m and not model_registry.namespace_vocabulary(m, namespace):
            return m
        return translated

    def crew_pinned_effort(self, agent: str | None, crew_agent: str | None = None) -> str:
        """The reasoning effort THIS CREW pins, or ``""`` when it pins none.

        The tier between an explicit per-session override and the configured
        default: a crew that pins nothing resolves ``""`` and therefore inherits
        exactly what it inherited before this field existed.

        Keyed on :func:`resolve_crew_identity` — the same canonical
        ``config.agents`` key the provider factory and the warm-pool claim
        already agree on — so ONE lookup covers every surface that can start a
        session (dashboard, cron, Slack, webhook, spawn). That matters more here
        than for ``model``, whose crew pin arrives as the caller's
        ``model_override``: a schedule- or webhook-woken crew has no dashboard
        slot to carry an override, so a per-caller resolution would leave those
        crews — the ones a pin is most useful for — on the global default.
        """
        crew = self._crew_record(agent, crew_agent)
        return coerce_effort(crew.reasoning_effort) if crew is not None else ""

    def _crew_record(
        self, agent: str | None, crew_agent: str | None
    ) -> "KiroCrewAgentConfig | None":
        """The ``config.agents`` record this session runs as, or ``None``."""
        key = resolve_crew_identity(self, agent, crew_agent)
        return self.agents.get(key) if key else None

    def resolve_session_effort(self, agent: str | None, crew_agent: str | None = None) -> str:
        """The effort a NEW session resolves to, short of an explicit override.

        The crew's own pin first, then the role-aware default: a background
        worker agent (``kirocrew-lite`` / ``kirocrew-heartbeat``) takes the
        ``background`` role effort, everything else the chat default. A pin the
        operator typed on the crew therefore outranks BOTH defaults, including
        the role one — the pin is a choice, the role effort is a built-in.

        Shared with the crews API's readout deliberately. The pane's job is to
        say what a session will actually run at, and a second copy of this chain
        would drift from the one the factory applies — a crew bound to a
        background agent would be reported at the chat default while running at
        the role effort.

        The role check keys on the crew's BOUND ``kiro_agent``, not on ``agent``,
        because that parameter carries different things on different surfaces:
        the dashboard passes a kiro agent name, while Slack threads, cron jobs and
        spawned agents pass a CREW name (the convention
        :func:`resolve_crew_identity` documents). Keying on the raw value made an
        unpinned crew bound to a background worker run at the chat default
        whenever the surface named the crew — the same class of drift in the other
        direction. With no crew record to read, ``agent`` IS the agent name (the
        background/heartbeat session keys pass it directly) and is used as-is.

        The bound name goes through :func:`dispatch_kiro_agent` first: the role
        set holds DECLARED names, and a crew that recorded a worker's file name
        runs that worker all the same.
        """
        crew = self._crew_record(agent, crew_agent)
        if crew is not None:
            pinned = coerce_effort(crew.reasoning_effort)
            if pinned:
                return pinned
        template = dispatch_kiro_agent(crew.kiro_agent) if crew is not None else (agent or "")
        if template in BACKGROUND_WORKER_AGENTS:
            return self.agent.resolve_effort("background")
        return self.agent.reasoning_effort

    @staticmethod
    def _resolve_named_agent_model(agent: str, agents_dir: Path | None = None) -> str:
        """Return a named agent's own kiro ``model`` field, or ``""`` if none.

        Used by :meth:`SessionManager.get_or_create` so an explicit global
        ``agent.model`` does not override an agent that pins its own model — the
        global default must rank *below* a per-agent pin. Returns the kiro
        ``model`` slot only; ``""`` when the agent declares none, so the caller
        falls back to the global. ``agents_dir`` overrides the lookup directory
        (a dependency-injection seam for tests); defaults to ``kiro_agents_dir()``.

        Reads the ``agent_discovery.parsed_agent_specs`` snapshot -- the same
        stat-signature-revalidated cache behind ``agent_skill_globs`` -- rather
        than re-parsing every spec per call. This runs SYNCHRONOUSLY on the event
        loop from the provider factory (every session start, every background
        recycle), and a per-call scan of a ~125-file agents directory is ~125
        ``realpath`` calls plus twice as many ``is_sensitive_path`` round trips
        through the two-worker ``mc-pathres`` pool; when that pool is also
        serving the skill scanner's bulk traffic, those waits queue and their
        sum crosses the loop-stall watchdog. A warm call costs one ``scandir``.
        On the loop, cold or changed snapshots refresh in the ``mc-discovery``
        pool while this lookup serves the previous rows. Off-loop callers parse
        inline.

        An EMPTY snapshot on the loop -- nothing published yet, or just cleared --
        is answered ``""`` (no pin), and NOT from the named agent's own file: a
        spec read, however bounded, is filesystem IO on the event loop, and the
        loop-stall watchdog this snapshot exists for makes no exception for a
        small one. The one caller that resolves a pin on a cold snapshot, the
        persistent background session created at gateway start before the first
        refresh lands, is async and warms the snapshot first
        (:func:`kiro_crew.agent_discovery.warm_agent_specs`, awaited in
        ``_ensure_background`` before the provider factory runs), so this lookup
        then finds warm rows through the path above and ``_bg`` is created on its
        own pin instead of the chat model for the gateway's lifetime. A snapshot
        that holds rows is served as it stands, whether or not it names this
        agent.

        JSON-first precedence is kept: with two live specs of DIFFERENT stems
        both declaring this name, the ``.json`` one wins, as the unordered
        first-match scan this replaces guaranteed (``iter_agent_spec_files``
        lists JSON entries first). Never raises -- a failure to import, walk or
        parse is "no pin here", never an exception into model resolution.
        """
        if not agent:
            return ""
        base = agents_dir if agents_dir is not None else kiro_agents_dir()
        try:
            # Deferred import: agent_discovery imports kiro_crew.hooks, whose
            # closure reaches back into this module (see _project_declares_agent).
            from kiro_crew.agent_discovery import cached_agent_specs, parsed_agent_specs

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                rows = parsed_agent_specs(base, operation="load_config", source="unknown")
            else:
                rows = cached_agent_specs(base, operation="load_config", source="unknown")
        except Exception:
            return ""
        # Stable sort: JSON rows first, filename order preserved within each group.
        rows.sort(key=lambda row: row[1].suffix.lower() != ".json")
        for ad, af in rows:
            # Skip stray non-object JSON a user may have dropped in the dir.
            if isinstance(ad, dict) and (ad.get("name") == agent or af.stem == agent):
                return ad.get("model") or ""
        return ""

    def load_credentials(self, *, propagate: bool = True) -> dict[str, str]:
        """Load credentials from ~/.kiro/crew/.env and environment variables.

        .env format: KEY=VALUE (one per line, # comments, no quotes required).
        Environment variables override .env values. A credential whose value is
        a ``secret://NAME`` reference comes back as the vault secret it names
        (:func:`resolve_env_credentials`); one that cannot be resolved is left
        out, so it reads as unset.
        """
        creds: dict[str, str] = {}
        ep = env_path()
        if ep.exists():
            # Enforce restrictive permissions on the credential file. POSIX
            # only: on Windows mode bits are meaningless (a chmod there
            # toggles the read-only attribute and succeeds without narrowing
            # who can read), and the real owner-only lockdown --
            # ``platform_compat.restrict_to_owner`` -- is not applied on this
            # READ path. The reason is not cost: it is that a reader has no
            # business rewriting a
            # descriptor it did not create, and doing so here would apply the
            # DACL of whichever process happened to read the file next.
            # Windows enforcement therefore lives where the file is WRITTEN --
            # the setup wizard and the dashboard credential writers all apply
            # ``restrict_to_owner`` at write time.
            try:
                if platform_compat.IS_POSIX and ep.stat().st_mode & 0o077:
                    ep.chmod(0o600)
            except OSError:
                logger.warning("Cannot enforce permissions on %s", ep)
            try:
                env_text = read_env_text(ep)
            except EnvFileWideEncodingError as exc:
                warn_undecodable_env(ep, exc)
                env_text = ""
            for line in env_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()

            # Warn once per boot about keys not in the recognised allowlist.
            # These keys still propagate (operators use them for proxy/feature
            # settings), but the warning makes the behavior visible rather than
            # silently surprising.  The encrypted vault (PR 1+) will provide a
            # proper agent-isolated path for secrets. A PATH line is a supported
            # setting (it extends the PATH), so it is not one of these keys.
            unknown = {
                k
                for k in set(creds) - set(CREDENTIAL_KEYS) - _warned_env_keys
                if not platform_compat.env_key_allowed(k, ("PATH",))
            }
            if unknown:
                _warned_env_keys.update(unknown)
                for uk in sorted(unknown):
                    logger.warning(
                        "Unknown key %s in .env is not a recognised credential"
                        " -- it will propagate to child processes but is NOT"
                        " agent-isolated.",
                        uk,
                    )

            # The environment wins over .env (see the docstring), so a line whose
            # key the process already holds with a different value never reaches
            # the environment child processes inherit. Say so once per boot,
            # naming the key only: either value may be a secret. Each key is
            # checked only the first time a load sees it: after that,
            # os.environ may hold this function's own seed or a dashboard
            # write, and a mismatch is a .env edit waiting for a restart, not
            # an outside variable. An in-app restart re-execs with this
            # process's environment, seeds included, so the successor's first
            # load reports an edited line too: the edit really is not applied
            # until the gateway is stopped and started. Exempt:
            # - a PATH line, which extends the PATH instead;
            # - a ``secret://`` reference, because comparing it would take a
            #   vault read on this path and its secret may equal the variable.
            unchecked = {
                k: v
                for k, v in creds.items()
                if v
                and k not in _shadow_checked_env_keys
                and not platform_compat.env_key_allowed(k, ("PATH",))
            }
            _shadow_checked_env_keys.update(unchecked)
            shadowed = {
                k
                for k, v in unchecked.items()
                if os.environ.get(k, v) != v and not v.startswith("secret://")
            }
            if shadowed:
                for sk in sorted(shadowed):
                    logger.warning(
                        "Key %s in .env is not applied to the process environment:"
                        " it already holds a different value, which child"
                        " processes inherit instead. It was set before .env was"
                        " read: by the shell, service or container that started"
                        " the gateway, by Kiro Crew itself at startup (a CA"
                        " bundle, for example), or by the older .env an in-app"
                        " restart carried over. The .env line applies only when"
                        " the gateway starts without that value: stop and start"
                        " the gateway once it is gone. A value Kiro Crew sets"
                        " itself never yields to .env; set that variable in the"
                        " environment that starts the gateway instead.",
                        sk,
                    )

        for key in CREDENTIAL_KEYS:
            val = os.environ.get(key)
            if val:
                creds[key] = val

        # Propagate credentials into the process environment so spawned children
        # (sandboxed agents, MCP servers, cron-fired subprocesses) inherit them
        # via Popen's default env=os.environ.copy() — even when their view of
        # ~/.kiro/crew/.env is a bind-mounted empty file. setdefault() preserves
        # any value the caller already set explicitly.
        #
        # EXCEPTION: when the Docker entrypoint has deliberately scrubbed
        # credentials from the process environ (setting _KIROCREW_CREDS_SCRUBBED=1),
        # re-injecting them here would leak into /proc/<pid>/environ — the exact
        # attack surface the entrypoint closed. The scrub covers only credential
        # keys, so the skip is scoped to CREDENTIAL_KEYS: every other .env entry
        # (operator-added settings such as proxy or feature variables) still
        # propagates so children behave identically in and out of Docker.
        # Children that need the withheld credentials get them via their own
        # .env read or via an explicit env= kwarg on Popen (the sandbox and ACP
        # spawners already do this).
        if propagate:
            scrubbed = bool(os.environ.get("_KIROCREW_CREDS_SCRUBBED"))
            for k, v in creds.items():
                if not v:
                    continue
                if scrubbed and (k in CREDENTIAL_KEYS or _JIRA_TOKEN_RE.match(k)):
                    continue
                if platform_compat.env_key_allowed(k, ("PATH",)):
                    # A process always has a PATH, so setdefault would make this
                    # line inert. It extends the PATH instead, and nothing more.
                    # Lazy import: env must stay off this module's import graph.
                    from kiro_crew.env import extend_process_path

                    extend_process_path(v)
                    continue
                os.environ.setdefault(k, v)

        # Resolve AFTER propagating: a child inherits the ``.env`` line as
        # written, so a ``secret://`` reference reaches os.environ as the
        # reference and the vault secret stays in this process.
        return resolve_env_credentials(creds, home=ep.parent)

    def create_provider_factory(self) -> Callable:
        """Return a factory that creates LLMProvider instances from config.

        Kiro Crew is KiroACP-only: the sole provider is the ACP adapter driving
        the kiro-cli backend. The factory accepts an optional ``session_key`` to
        create a per-session subdirectory under ``workspace_root()``.
        """
        from kiro_crew.agent_sdk.drivers.acp import build_acp_provider
        from kiro_crew.session_work_dir import is_disposable_session_key

        model = self.agent.model
        if model == DEFAULT_MODEL:
            model = self._resolve_agent_model()

        sandbox = self.agent.sandbox
        tool_search = self.agent.tool_search
        tool_search_min_pct = self.agent.tool_search_min_pct
        tool_search_min_tokens = self.agent.tool_search_min_tokens

        # MCP gateway: resolve overlay + socket once, iff some server is stubbed
        # through the gateway. Routing is what puts a stub in the path, and the
        # stub is what carries both the render/callback path and any sharing —
        # so an empty stub set means no stub, no daemon, and no gateway in the
        # path at all (AcpClient falls through to per-session MCP). Sharing
        # (``enabled``) is not consulted here: it decides how a stubbed server's
        # backend is ACQUIRED, and on its own routes nothing.
        _gw = self.mcp_gateway
        if _gw.stub_servers:
            _gw_overlay = _gw.overlay_dir or str(default_overlay_dir())
            _gw_socket = _gw.socket_path or str(default_socket_path())
        else:
            _gw_overlay = None
            _gw_socket = None

        # Effort-drop warnings already emitted by this factory, keyed by
        # (resolved model, level) — see the gate below. Benign under threads:
        # a lost race duplicates one log line, never drops state.
        _effort_drop_warned: set[tuple[str, str]] = set()

        def _acp(
            session_key: str | None = None,
            agent: str | None = None,
            channel_id: str | None = None,
            model_override: str | None = None,
            cwd: str | None = None,
            extra_env: dict[str, str] | None = None,
            reasoning_effort_override: str | None = None,
            crew_agent: str | None = None,
            # The backend a caller asked for explicitly (``spawn_run(backend=...)``).
            # NAMED so it is never swallowed by the catch-all: the session would
            # silently start on the configured default. ``None`` = no override.
            backend_override: str | None = None,
            # Per-session opt-in for the claude backend's own permission
            # classifier. NAMED rather than left to ``**_kwargs`` on purpose: a
            # caller passing it into the catch-all would be swallowed here and
            # the session would spawn on the backend's default with no error.
            permission_mode: str | None = None,
            shared_scratch: Path | None = None,
            # The subagent manager's gate-exit start-clock reset for a DEDICATED
            # subagent process. NAMED for the same reason ``permission_mode``
            # is: swallowed by the catch-all, the dedicated path would silently
            # keep charging session-start-gate queue time to the startup
            # watchdog, which is the exact defect the callback exists to end.
            on_gate_acquired: Callable[..., None] | None = None,
            on_gate_queued: Callable[..., None] | None = None,
            # A Side Chat session allowed read-only tools. NAMED for the same
            # reason: swallowed by the catch-all, a claude side session would
            # start with the user's full tool set under READ_ONLY.
            side_read_only: bool = False,
            **_kwargs: object,
        ):  # return type inferred from build_acp_provider (SDK boundary: config may not name AcpProvider)
            wdir = Path(cwd) if cwd else _session_work_dir(session_key)
            # Canonical crew identity for the session (keys per-agent watchdog
            # windows on the handle) — one shared resolution rule, see
            # resolve_crew_identity.
            crew_agent = resolve_crew_identity(self, agent, crew_agent)
            # Resolve the model, highest tier first:
            #   1. model_override — the caller's explicit pick. The dashboard
            #      passes the slot's own model, else the Kiro Crew agent's
            #      configured default (see chat_runner._run_chat).
            #   2. the bound kiro agent's own pinned model, for a named agent.
            #      Custom agents MUST resolve here because the ACP
            #      session/set_mode path switches prompt/tools but not the model,
            #      so an unset model makes kiro fall back to cli.json's
            #      chat.defaultModel. Use _resolve_named_agent_model (the kiro
            #      model slot) to match this backend.
            #   3. ``model`` — the global agent.model default, already collapsed
            #      through _resolve_agent_model() at factory-build time. It
            #      applies to every agent, not just "kirocrew": an agent that
            #      pins nothing inherits the user's configured default instead of
            #      silently falling through to the backend's own choice.
            # "" at the end means nothing is pinned anywhere; AcpClient
            # normalizes "" to DEFAULT_MODEL, same as None.
            # Selection + the per-backend id translation live in
            # acp_effective_model — SHARED with the spawn-side effort verdict
            # (subagent.py) so the reported outcome cannot drift from what this
            # gate actually keys on. (Why the translation is keyed on the
            # backend, and why to_acp_id is the non-claude choice, is documented
            # on that method.)
            # Per-session backend selection -- ONE call to the selection gate's
            # per-session half (members.select_provider_backend: the caller's
            # explicit backend > member-DM auto-route > configured default). The
            # factory body carries no
            # branching of its own, so the kiro construction path gains no
            # second check (harness-parity H3/H13); resolve_selected_backend
            # inside the helper applies the same governance/selectability gate
            # as the persisted field, so a denied or unknown value degrades to
            # kiro -- the member thread then runs as plain chat and the mount
            # step logs why.
            # circular import: members sits above config in the layering.
            from kiro_crew.members import select_provider_backend

            _backend = select_provider_backend(
                session_key,
                self.agent.member_acp_backend,
                self.agent.acp_backend,
                backend_override=backend_override,
            )
            # Resolved BEFORE the model, and threaded into the resolution: the
            # model's namespace translation and its pin-scope check both have to
            # key on the backend this session actually gets, not on the
            # configured default. A member-DM thread auto-routed to another
            # harness runs a backend the config does not name, so keying either
            # on the configured field judges the pin for a namespace that
            # session never reaches.
            m = self.acp_effective_model(
                agent,
                model_override,
                global_model=model,
                namespace=capabilities_for(_backend).model_id_namespace,
            )
            # Thread the slot's effort into a per-model override so the kiro
            # cli.json overlay is written from it at spawn — without this, a
            # kiro cold start (or the handler's reset-then-respawn) would only
            # pick up effort already recovered from a pre-existing overlay,
            # never the freshly-set slot value. Mirrors the _claude_code path.
            _eff_per_model: dict[str, str] = {}
            # Everything below an explicit override — the crew's pin, then the
            # role-aware default — resolves in resolve_session_effort, which the
            # crews API also serves its readout from. An explicit override (the
            # dashboard slot's effort, or a sub-agent's resolved "subagent"
            # effort) still wins over all of it.
            _eff = reasoning_effort_override or self.resolve_session_effort(agent, crew_agent)
            # On a harness whose effort capability and vocabulary come from the
            # option it ADVERTISES, neither check below can answer here. This
            # factory runs before any session exists, the registry carries none of
            # the operator's own model ids, and Crew's ladder does not list every
            # level such a harness offers -- so both facts arrive with
            # ``session/new``, and ``AcpProvider._resolve_effort`` validates the
            # level against the advertised list once they do. The level is carried
            # forward for a member and judged there. Judging it HERE drops it on
            # every cold start, and the session then runs the adapter's own default
            # while the dashboard still shows the level the operator picked.
            _from_option = _backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION
            _registry_ok = is_valid_effort(_eff) and model_supports_effort(m)
            # With nothing pinned there is no model id to key the level under
            # yet: the backend picks the model and reports it on ``session/new``.
            # The level is carried unkeyed and the provider binds it to the
            # model the session actually runs (``AcpProvider._bind_unbound_effort``),
            # which is also where it is judged and, when refused, logged.
            _unbound_effort = ""
            if m and _eff and (_from_option or _registry_ok):
                _eff_per_model[m] = _eff
            elif not m and _eff and (_from_option or is_valid_effort(_eff)):
                _unbound_effort = _eff
            elif _eff and is_valid_effort(_eff):
                # Single-authority drop warning: a valid requested effort is
                # being dropped because the resolved model is not
                # effort-capable. Every surface (spawn, dashboard slot, cron)
                # funnels through this factory, so one log at the gate covers
                # them all and cannot drift from the decision it reports on.
                # Reporting-only — the overlay simply stays unwritten, exactly
                # as before.
                #
                # An EXPLICIT override always warns: a caller's own request
                # being dropped is the event this gate exists to surface, and
                # a config-default drop must not burn its dedupe key first
                # (Design review on this PR). Only the static config default
                # (base_effort with no override) dedupes per (model, level) —
                # it is one unchanging configuration fact that would otherwise
                # repeat on every provider construction (warm-pool fills and
                # recycles included); a config change rebuilds the factory and
                # re-arms it.
                _dedupe = not reasoning_effort_override
                if not _dedupe or (m, _eff) not in _effort_drop_warned:
                    if _dedupe:
                        _effort_drop_warned.add((m, _eff))
                    logger.warning(
                        "reasoning effort '%s' will not be applied (session %s) — "
                        "model '%s' does not support effort configuration",
                        _eff,
                        session_key or "?",
                        m,
                    )
            return build_acp_provider(
                work_dir=wdir,
                model=m,
                agent=agent,
                crew_agent=crew_agent,
                sandbox_mode=sandbox,
                session_key=session_key,
                channel_id=channel_id,
                extra_env=extra_env,
                acp_backend=_backend,
                effort_per_model=_eff_per_model,
                unbound_effort=_unbound_effort,
                tool_search=tool_search,
                tool_search_min_pct=tool_search_min_pct,
                tool_search_min_tokens=tool_search_min_tokens,
                mcp_gateway_overlay=_gw_overlay,
                mcp_gateway_socket=_gw_socket,
                permission_mode=resolve_cc_permission_mode(permission_mode, _backend),
                # A dedicated subagent process joins its parent's session tree:
                # the tree's work directory is mounted beside its own scratch
                # and is what its ``$KIROCREW_SCRATCH`` names (agent_scratch).
                shared_scratch=shared_scratch,
                on_gate_acquired=on_gate_acquired,
                on_gate_queued=on_gate_queued,
                side_read_only=side_read_only,
                # Only a work dir DERIVED from a one-run key is the provider's
                # to reclaim at shutdown; an explicit ``cwd`` is the caller's
                # directory whatever the key says (session_work_dir).
                disposable_work_dir=not cwd and is_disposable_session_key(session_key),
            )

        return _acp
