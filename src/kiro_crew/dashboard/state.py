"""Dashboard shared state — ChatSlot and DashboardState."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
import weakref
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.loader import (
    DASHBOARD_PORT,
    _raw_config,
    config_dir,
    resolve_effective_agent,
)
from kiro_crew.constants import (  # noqa: F401 -- DENY_CAUSE_* / STEER_NOTICE_BOUND_SECS re-exported
    DENY_CAUSE_APPROVAL_NO_BUDGET,
    DENY_CAUSE_APPROVAL_TIMEOUT,
    DENY_CAUSE_APPROVAL_UNDELIVERABLE,
    DENY_CAUSE_BATCH_CASCADE,
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_INVALID_NAME,
    DENY_CAUSE_POLICY,
    DENY_CAUSE_SURFACE_POLICY,
    OPTIONS_RE_LINE,
    STEER_NOTICE_BOUND_SECS,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
)
from kiro_crew.dashboard import pinned_session_order
from kiro_crew.dashboard.chat_compaction_notice import deliver_channel_compaction_notice
from kiro_crew.dashboard.chat_tag_grants import seed_default_grants, seed_status_identity_rows
from kiro_crew.dashboard.dashboard_persistence import DashboardPersistenceCoordinator
from kiro_crew.dashboard.folder_repository import FOLDERS_FILE, FolderRepository
from kiro_crew.dashboard.interaction_coordinator import (
    ApprovalCoordinator,
    QuestionCoordinator,
)
from kiro_crew.dashboard.notification_coordinator import NotificationCoordinator
from kiro_crew.dashboard.recovery_replays import RecoveryReplays
from kiro_crew.dashboard.session_pulse_counter import increment_user_session_count_off_loop
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.dashboard.slot_buffers import SlotBufferCoordinator
from kiro_crew.dashboard.slot_projection import SlotProjection
from kiro_crew.dashboard.slot_queue_repository import (
    EMPTY_QUEUE_SIGNATURE,
    SlotQueueRepository,
    durable_queue_entries,
    durable_queue_view,
    queue_persist_signature,
)
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.system_notices import is_system_notice
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG, WebSocketHub, WsPayload
from kiro_crew.deny_guidance import remediation_for
from kiro_crew.deny_notice import (  # noqa: F401 -- re-exported for dashboard importers
    _DENY_CAUSE_TEXT,
    build_refusal_steer_notice,
    steer_refusal_notice,
)
from kiro_crew.history import (
    MEMORY_MODES,
    latest_transcript_ts,
    mint_row_mid,
    monotonic_transcript_ts,
)
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    UNBIND_REASON_DASHBOARD_UNLINK,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_ORIGIN_REBIND,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    UNBIND_REASON_USER_UNLINK,
    ChannelLink,
    binding_token,
    channel_namespace_of,
    is_channel_session_key,
    split_namespaced_channel_id,
)
from kiro_crew.messaging.renderer import display_safe
from kiro_crew.notifications.bus import (
    NotificationBus,
    NotificationValidationError,
    normalize_note,
    payload_from_legacy,
)
from kiro_crew.notifications.rate_limit import AppRateLimiter
from kiro_crew.notifications.resource_pressure import ResourcePressureNotifier
from kiro_crew.notifications.settings import ChannelSettings
from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.release_channel import channel as _release_channel_of_build
from kiro_crew.safety_override import cached_disabled_approval_modes, safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.security.credential_sources import CredentialEvidence
from kiro_crew.sel import sel
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_COMPACTED,
    COMPACT_OUTCOME_RECYCLED,
    COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE,
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )
    from kiro_crew.dashboard.listener_guard import ListenerGuard  # noqa: F401
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog  # noqa: F401
    from kiro_crew.messaging.transport import MessagingTransport  # noqa: F401
    from kiro_crew.power import SleepInhibitor  # noqa: F401
    from kiro_crew.slack.outbound import PostedOptions  # noqa: F401
    from kiro_crew.subagent import SubagentDelivery  # noqa: F401
    from kiro_crew.trust_patterns import GrantDeadline  # noqa: F401

# ``DashboardState`` reads these while it is defined; every other re-export follows the class.
# One name per line (isort would merge them into one wrapped import).
# isort: off
from kiro_crew.dashboard.state_parts.slot_directory import NATIVE_SUBAGENT_TERMINAL_KEEP
from kiro_crew.dashboard.state_parts.slot_directory import NATIVE_SUBAGENT_TERMINAL_TTL_SECS
from kiro_crew.dashboard.state_parts.slots_frame import _SLOTS_BROADCAST_INTERVAL_S

# isort: on

logger = logging.getLogger(__name__)


class DashboardState:
    """Shared state injected into all handlers via ``app["state"]``."""

    # Class-level defaults, NOT just __init__ assignments. push_slots_update and
    # _persist_open_slots read these on every call, and a partially-constructed
    # state built with DashboardState.__new__(DashboardState) — the pattern used
    # by several endpoint test suites, which set only the attributes the handler
    # under test touches — never runs __init__. Without a class default those
    # reads raise AttributeError. __init__ still assigns per-instance values
    # below; these only supply the "nothing suspended, not restoring" baseline.
    _slots_push_suspend: int = 0
    _slots_push_pending: bool = False
    _slots_push_overlapped: bool = False
    restoring_open_slots: bool = False
    # False until the startup open-tab restore has run once. The periodic flush
    # loop is armed BEFORE that restore, so a flush firing in the gap sees a
    # ``_slots`` the restore has not populated yet — empty, or holding only a
    # tab created during boot — and pruning open_slots.json down to it loses the
    # seeded tabs the restore has yet to read, sending every session into
    # "older sessions" after the next restart. While this is False,
    # _persist_open_slots MERGES the live keys into the existing on-disk seed
    # (never shrinking it) and _persist_context_snapshots writes without
    # pruning; both resume pruning once this flips True. A surface that runs no
    # restore stays in merge mode, so its writes are never suppressed — see
    # _persist_open_slots / _persist_context_snapshots.
    open_slots_restored: bool = False
    # push_slots_update() coalescing state, on that same read path. The lock
    # defaults to None rather than to a shared Lock(): a None lock means "no
    # coalescing", so a __new__-built state broadcasts straight through instead
    # of every instance in the process contending on one class-level mutex.
    # __init__ installs the real per-instance lock.
    _slots_broadcast_lock: "threading.Lock | None" = None
    _slots_broadcast_timer: "asyncio.TimerHandle | None" = None
    _slots_broadcast_last: float = 0.0
    # Current coalescing window, sized from the last broadcast's frame (see
    # ``_slots_broadcast_interval_for``). Written and read under the lock.
    _slots_broadcast_interval: float = _SLOTS_BROADCAST_INTERVAL_S
    # Who the next coalesced slots broadcast is owed to, written under
    # ``_slots_broadcast_lock``. A ``push_slots_update(legacy_only=True)`` owes
    # the full list only to consumers that cannot apply a ``slot_patch`` frame;
    # any ordinary push owes it to everyone and wins. Both False (the state of a
    # direct ``_do_slots_broadcast`` call) means everyone.
    _slots_push_all_owed: bool = False
    _slots_push_legacy_owed: bool = False
    # The person's pinned-session order (``pinned_session_order``). A class
    # default for the same reason as the push fields above: serialize_slot
    # reads it on the slots-push hot path, which also runs on a
    # ``__new__``-built state. The empty tuple means "never reordered".
    _pinned_session_order: "list[str] | tuple[str, ...]" = ()
    #: Whether the stored order has been read. The read runs after the
    #: gateway starts serving (see ``load_pinned_order_after_listen``), and
    #: every writer reads it first when it has not landed yet.
    _pinned_session_order_loaded: bool = False
    #: Whether an order was ever stored, even an empty one. The browser's
    #: one-time hand-off of its old local order lands only while this is False.
    _pinned_session_order_stored: bool = False
    #: Revision of the in-memory order, raised on every change. Each slot row
    #: and each reorder answer carries it, so a browser can tell a frame
    #: serialized before a write from one serialized after it.
    _pinned_order_rev: int = 0
    # The one loop this dashboard is served on. Every surface that hands work in
    # from a foreign thread -- the coalesced slots broadcast, an off-loop
    # websocket send, the log handler's fan-out -- resolves it through
    # :attr:`serving_loop` rather than keeping a copy of its own: two copies are
    # two answers to one question and can disagree, and a caller that finds its
    # own copy unset drops the work silently. Bound at app startup; the property
    # latches lazily so a ``__new__``-built state still resolves one.
    _serving_loop: "asyncio.AbstractEventLoop | None" = None
    # Keys the last open-tab restore could not read (not keys it proved absent).
    # _persist_open_slots folds these back into the snapshot so a transient read
    # failure cannot erase the reopen seed. The class-level baseline is an
    # IMMUTABLE frozenset on purpose: a bare set() here would be one object
    # shared by every __new__-built instance. __init__ and the restore each
    # assign a fresh set(), so mutation only ever touches an instance attribute.
    unrestored_slot_keys: "frozenset[str] | set[str]" = frozenset()
    # Whether the last open-tab restore could READ the registry at all. False means
    # every generation that might hold the set was refused by the OS, so this process
    # has no basis for treating ``_slots`` as the authoritative open-tab set and
    # ``open_slots_restored`` stays down for its whole life. Defaults True so a
    # surface that never runs a restore is not held in merge mode forever.
    open_slots_knowable: bool = True
    # What this boot's open-tab restore could not SHOW, as ``{"count", "keys"}``, for
    # the dashboard's own notice. A separate field from ``unrestored_slot_keys``
    # because the two answer different questions and change at different times: the
    # keys above are live reopen-seed state the persist writers fold in on every
    # flush, while this is one frozen reading taken when the restore finished. Reading
    # the live set instead would make the notice say a different number each time it
    # was asked, and say nothing at all once a later restore cleared it.
    #
    # ``None`` means no restore has reported, which a reader must not render as zero:
    # "no tabs were dropped" and "nobody has looked yet" are different answers.
    unrestored_slot_notice: "dict[str, Any] | None" = None
    crew: Any = None  # Crew Mode control plane (set by gateway; None = unavailable)
    # Gateway-owned restore/open task. The class default keeps lightweight
    # ``__new__`` fixtures on the already-ready baseline; a real gateway
    # publishes its task immediately before READY so chat admission can wait
    # without mistaking a transient preparation fence for a failed turn.
    memory_startup_task: "asyncio.Task[None] | None" = None
    resume_channel_agents: "Callable[[], None] | None" = None

    def __init__(
        self,
        sessions: SessionManager,
        crons: CronService,
        lessons: LessonStore,
        start_time: float,
        subagents: SubagentManager | None = None,
        context_builder: ContextBuilder | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        task_runner: TaskRunner | None = None,
        slack_client: Any = None,
        owner_id: str = "",
    ):
        self.sessions = sessions
        # The decisions seam's LLM lane needs ONE callable that runs a prompt on a
        # tool-less background session, and ``decisions/`` deliberately imports
        # nothing above itself -- so the wiring happens here, where the session
        # manager first exists. Here rather than in ``server.py`` because both the
        # gateway and the standalone dashboard construct this object, and a lane
        # wired on only one of those paths is a lane that answers on one boot and
        # refuses on the other. Guarded: a lane that cannot be wired must not stop
        # the dashboard from booting, and an unwired lane already has a defined
        # behaviour -- every judged tick fires exactly as the ungated timer does.
        try:
            from kiro_crew.decisions import impl_llm

            impl_llm.set_runner_factory(
                lambda model: impl_llm.build_session_runner(sessions, model=model)
            )
        except Exception:
            logger.debug("decisions: LLM lane runner not registered", exc_info=True)
        self.crons = crons
        self.lessons = lessons
        self.start_time = start_time
        # Published only at the final boot-to-ready boundary in server.py.
        # The socket binds earlier, so /api/ready can truthfully return 503
        # while session restoration and tunnel setup finish. The gateway may
        # defer restored channel agents until its memory task completes.
        self.ready: bool = False
        self.memory_startup_task: "asyncio.Task[None] | None" = None
        # Wired by server.py after the gateway-owned prerequisite service is
        # constructed. The central chat runner reads this latch so every turn
        # entry path is protected, including task/workflow continuations.
        self.kiro_prerequisite_service: Any = None
        self.subagents = subagents
        self.channel_manager: Any = None  # lazy-init in server.py
        # A gateway launch defers legacy channel-agent relaunch until memory
        # preparation settles. Standalone dashboard callers keep the immediate
        # start behavior and leave this callback unset.
        self.resume_channel_agents: "Callable[[], None] | None" = None
        self.tunnel_manager: Any = None  # lazy-init in server.py (TunnelManager)
        self.instances_manager: Any = None  # lazy-init in server.py (SshTunnelManager)
        self.instances_registry: Any = None  # lazy-init in server.py (InstancesRegistry)
        # Cloud provisioning launch jobs (lazy-init in handlers_cloud).
        self.cloud_launch_store: Any = None  # LaunchJobStore
        self.cloud_launch_cancels: Any = None  # dict[str, threading.Event]
        self.cloud_launch_engine: Any = (
            None  # test-injected LaunchEngine (None -> RealLaunchEngine)
        )
        self.cloud_launch_sync: bool = False  # tests set True to run launches inline
        self.cloud_launch_reaped: bool = False  # orphan reap is once per process
        self.cloud_launch_lock: Any = None  # asyncio.Lock serializing launch creation
        # MCP gateway control plane — wired by GatewayOrchestrator AFTER
        # dashboard init (the broker starts before dashboard_state exists).
        # Read by the /api/mcp-gateway/* handlers off request.app['state'].
        self._mcp_gateway_manager: Any = None  # GatewayManager | None
        self._mcp_gateway_apply: Any = None  # async (enabled: bool) -> dict
        self._mcp_gateway_apply_stub: Any = None  # async () -> dict
        self._mcp_resolve_refresh: Any = None  # async () -> dict
        # Read by the restart handler: stops the broker this gateway owns before
        # the exec, so a successor never meets a daemon still owned by this pid.
        self._mcp_gateway_stop: Any = None  # async () -> None
        # Secretary subsystem removed; kept as permanent None for apps/routes.py
        # builtin-service restart lookup (getattr-based, no-op when None).
        self._secretary_restart: Any = None  # restart callback (always None — service removed)
        self.workflow_service: Any = None  # published only after complete recovery
        self.workflow_startup_status = "pending"
        self.workflow_startup_stopping = False
        self.workflow_startup_task: asyncio.Task[None] | None = None
        self.context_builder = context_builder
        self.conversation_log = conversation_log
        # Set except while the startup crewmate prune is pending: the gateway
        # clears it before the listener binds (``_register_crewmate_prune_gate``)
        # and sets it once the pass has returned; that function's middleware
        # holds every mutating request, and every read of the member roster,
        # on it, so no session can bind an agent and no member log can be
        # folded between the prune's history check of a candidate and its
        # delete. Set by default so every other entry point -- tests, the CLI
        # -- never waits.
        self.crewmate_prune_settled = asyncio.Event()
        self.crewmate_prune_settled.set()
        # Read by the prune's worker thread: once set, the pass judges no
        # further candidate and deletes no further row (checked again inside
        # the config lock, before the delete). ``await_crewmate_prune_settled``
        # sets it when the pass outlives its budget, then keeps waiting for
        # ``crewmate_prune_settled`` -- a writer starts only after the pass has
        # returned, never beside a pass that can still delete.
        self.crewmate_prune_abandon = threading.Event()
        self.consolidator = consolidator
        self.task_runner = task_runner
        self.slack_client = slack_client
        # One lock per effective session key serialises the whole Slack link
        # attempt. Weak values: the holder and every waiter keep the lock alive
        # through their own reference, and the entry goes with the last of them.
        self._slack_link_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )
        # True only when the Slack socket-mode connect actually succeeded this
        # session. slack_client being set proves tokens existed at boot, not
        # that they are valid — the gateway records the real outcome after
        # _connect_slack(). Read by the Slack settings status badge.
        self.slack_socket_connected: bool = False
        # Short reason from the failed connect attempt (e.g. "invalid_auth"),
        # empty when connected or never attempted. Read by the settings badge.
        self.slack_connect_error: str = ""
        # True once the Discord channel's Gateway WebSocket transport started
        # this session (set by maybe_start_discord). Read by the Discord
        # settings status badge.
        self.discord_connected: bool = False
        # Short reason when the Discord channel failed to start, empty when
        # running or never attempted. Read by the settings badge.
        self.discord_connect_error: str = ""
        # True once the Telegram channel's long-polling transport started this
        # session (set by maybe_start_telegram). Read by the Telegram settings
        # status badge.
        self.telegram_connected: bool = False
        # Short reason when the Telegram channel failed to start, empty when
        # running or never attempted. Read by the settings badge.
        self.telegram_connect_error: str = ""
        # True only while the Webex device WebSocket is connected + authorized
        # this session (kept truthful by WebexClient.on_state_change). Read by
        # the Webex settings status badge.
        self.webex_connected: bool = False
        # Short reason from the most recent Webex connection failure, empty
        # when connected or never attempted. Read by the settings badge.
        self.webex_connect_error: str = ""
        # True only while the iMessage watch is live on the local bridge (kept
        # truthful by IMessageClient.on_state_change). Read by the iMessage
        # settings status badge.
        self.imessage_connected: bool = False
        # Short reason the iMessage channel is not running — a missing imsg
        # binary, a Messages database the process cannot read (Full Disk
        # Access), or a non-macOS host. Empty when connected or never attempted.
        self.imessage_connect_error: str = ""
        # True only while the WeCom (企业微信) channel's WebSocket is connected
        # + subscribed (kept live by WeComClient.on_status, wired in
        # maybe_start_wecom). Read by the WeCom settings status badge.
        self.wecom_connected: bool = False
        # Short reason from the most recent WeCom connection failure (connect
        # error, immediate close on bad credentials, or server kick), empty
        # when connected or never attempted. Read by the settings badge.
        self.wecom_connect_error: str = ""
        # True only while the Feishu (飞书/Lark) channel's WebSocket receiver
        # thread is alive (kept truthful by LarkClient.on_state_change, wired in
        # maybe_start_feishu). Read by the Feishu settings status badge.
        #
        # Receiver-liveness, NOT a credential probe: lark-oapi owns the socket
        # and exposes no connect/subscribe transition to hook, and a REST
        # tenant-token probe would have to pick a domain (open.feishu.cn vs
        # open.larksuite.com), reporting a false failure for whichever tenant it
        # guessed wrong. Liveness still catches rejected credentials, because
        # lark's ws.start() RETURNS within seconds when the app is refused —
        # which flips this to False with the reason attached.
        self.feishu_connected: bool = False
        # Short reason the Feishu channel is not running — a missing lark-oapi
        # extra, rejected app credentials, or a receiver thread that died. Empty
        # when connected or never attempted. Read by the settings badge.
        self.feishu_connect_error: str = ""
        # True only while the Teams channel's credentials validated this
        # session (kept truthful by TeamsClient.on_state_change). Read by the
        # Teams settings status badge.
        self.teams_connected: bool = False
        # Short reason from the most recent Teams credential/connection failure,
        # empty when connected or never attempted. Read by the settings badge.
        self.teams_connect_error: str = ""
        # Late-bound inbound webhook handler for the Teams channel. The route
        # POST /api/messaging/teams is registered at app-build time (aiohttp
        # freezes routes at startup); maybe_start_teams sets this to the built
        # client's on_activity once credentials are present. None => 503.
        self.teams_on_activity: Any = None
        # True only while the Weixin (personal WeChat over iLink) channel's
        # long-poll loop is running (set in maybe_start_weixin). Read by the
        # WeChat settings status badge — a credential present at boot is NOT
        # enough to report "connected".
        self.weixin_connected: bool = False
        # Short reason from the most recent Weixin start failure, empty when
        # connected or never attempted. Read by the settings badge.
        self.weixin_connect_error: str = ""
        # True only while the WhatsApp (QR-linked personal account) client's
        # event loop is running (set in maybe_start_whatsapp). Read by the
        # WhatsApp settings status badge — a paired session DB on disk is NOT
        # enough to report "connected".
        self.whatsapp_connected: bool = False
        # Short reason from the most recent WhatsApp start failure, empty when
        # connected or never attempted. Read by the settings badge.
        self.whatsapp_connect_error: str = ""
        # Live channel transports (Telegram/WeCom/...) for channel-neutral
        # cross-surface mirror delivery — registered at boot by each channel's
        # gateway via ``register_channel_transport``. Slack keeps its dedicated
        # ``slack_client`` above (rich streaming mirror), so it is not stored here.
        self.channel_transports: dict[str, "MessagingTransport"] = {}
        self.owner_id = owner_id
        self._owner_hash: str | None = None
        # Branch+commit are resolved once by the CLI entrypoint (set_build_info,
        # pre-loop, post-detection); status_snapshot() reads this attribute so
        # subprocess never runs on the event loop. See module-level _build_info.
        self._build_info: tuple[str, str] = _build_info
        self.messages_received = 0
        # Broadcast: each SSE client gets its own queue; _notify_event wakes all
        self._sse_queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._notify_event = asyncio.Event()
        # Depth + pending/overlap flags for suspend_slots_push(); see that method.
        self._slots_push_suspend = 0
        self._slots_push_pending = False
        self._slots_push_overlapped = False
        # Time-based coalescing state for push_slots_update(). Guarded by a
        # threading.Lock because callers are not all on the event loop.
        self._slots_broadcast_lock = threading.Lock()
        # True while the startup open-tab restore is in flight. Suppresses the
        # open_slots.json snapshot so a periodic flush cannot overwrite the file
        # being restored from with a half-populated slot set — see
        # _persist_open_slots.
        self.restoring_open_slots = False
        # Cleared until the open-tab restore has run once this boot; see the
        # class-level default and _persist_open_slots.
        self.open_slots_restored = False
        # Per-instance (see the class-level frozenset baseline for why).
        self.unrestored_slot_keys: set[str] = set()
        self._notification_log: list[dict[str, Any]] = _load_notifications()
        self._unread_count: int = 0
        self._notification_coordinator = _new_notification_coordinator()
        # Notification bus (schema v2) — notify() adapts legacy calls onto it;
        # _deliver_note is the delivery sink (log, count, broadcast, persist).
        self.notification_bus = NotificationBus(sink=self._deliver_note)
        # Future of the most recent delivery-sink persist job (None when the
        # last persist ran inline). The app push handler awaits it to give a
        # durability guarantee; legacy producers ignore it (best-effort).
        self.last_notification_persist: asyncio.Future[bool] | None = None
        # Per-app push rate limiter (RFC Phase 2). State-owned (not a module
        # global) so its lifecycle matches the gateway instance and tests get
        # isolation for free.
        self.notification_rate_limiter = AppRateLimiter()
        # Per-channel user settings (RFC Phase 3): mute + priority override,
        # applied at the delivery sink so the bus stays pure.
        self.notification_channel_settings = ChannelSettings()
        # Resource-pressure producer: samples host posture (driven from the
        # event-loop heartbeat) and pushes episode-deduped notes to
        # system.resources. State-owned like the bus/limiter/settings so its
        # lifecycle matches the gateway instance.
        self.resource_pressure_notifier = ResourcePressureNotifier(self.notification_bus)
        # Channel turn-ceiling producer. Registered HERE, once, beside the bus it
        # delivers through, rather than injected per channel: a channel that
        # forgot the wire would be a channel whose pauses are invisible to the
        # operator, and invisibility is the defect the ceiling exists to remove.
        # `notify` is synchronous and never raises, which is what the ceiling
        # needs -- it runs inside a pre-stream gate whose only job is to refuse
        # the turn.
        turn_ceiling.set_notification_sink(
            lambda session_key, surface: self.notify(
                "agent",
                "Conversation paused: turn limit",
                f"A {surface} conversation reached its turn ceiling and is paused. "
                "Reset it from the dashboard to continue.",
                meta={"session_key": session_key, "surface": surface},
            )
        )
        self._slots: dict[str, _ChatSlot] = {}
        self._slot_registry = SlotRegistry()
        # Process-local guard for copies queued before a durable consumption
        # stamp lands, and for a hydration that read the hold before it. An
        # entry is kept once its stamp is proven, marked stamped so eviction
        # takes it before any still-unstamped guard; the registry is
        # count-bounded by _MAX_CONSUMED_MERGE_CARDS.
        self._consumed_merge_cards: dict[str, _ConsumedMergeCard] = {}
        # Process-local Spec Builder outbox claims, keyed by directory + delivery.
        # Directory scope matters because aliases use different slots for the same
        # files; durable status remains owned by the app's decision ledger.
        self._spec_decision_deliveries_inflight: set[tuple[str, str]] = set()
        # Consumed claims whose durable finalization failed remain blocked from
        # redispatch while a later Spec Builder detail poll retries the ledger write.
        self._spec_decision_deliveries_consumed: set[tuple[str, str]] = set()
        # Sandbox kind -> monotonic time of the last notification for it. Keyed on
        # the sandbox layer's own closed kind set (three values), NEVER on request
        # data: auto-speak synthesises one request per sentence, so a key carrying
        # a caller-chosen slot would grow for the process lifetime on a host that
        # refuses every one of them. Bounded at three entries by construction, so
        # it needs no size cap and no eviction pass.
        self._voice_sandbox_notified: dict[str, float] = {}
        # Slot keys that EXIST but are deliberately absent from ``_slots`` while
        # they are being built (see ``session_transfer``'s import path, which
        # retracts a slot so it is unreachable until its transcript and context
        # are in place). They still consume memory, so every cap must count them:
        # ``len(_slots)`` alone undercounts by however many imports are in flight,
        # and each concurrent import would then be waved past a full-slot cap.
        self._slots_under_construction: set[str] = set()
        # The slot each connected owner dashboard reports in the foreground, by
        # connection (``slot_retention.note_foreground``). The idle sweep never
        # archives a slot someone is looking at.
        self._foreground_slots: dict[int, str] = {}
        # Return-triggered warm task briefs keep only presence episodes and live
        # generation tasks in memory. The durable claim itself lives beside the
        # transcript, so a gateway restart can deduplicate but can never infer a
        # leave that this process did not observe.
        self._warm_task_brief_away: dict[str, tuple[float, int]] = {}
        self._warm_task_brief_generation = 0
        self._warm_task_brief_slot_generation: dict[str, int] = {}
        self._warm_task_brief_tasks: dict[str, asyncio.Task[Any]] = {}
        self._warm_task_brief_model_active: set[str] = set()
        self._slack_to_slot: dict[str, str] = {}  # Slack session_key → slot name
        # Live OPTIONS controls, keyed by the SESSION KEY that owns them.
        #
        # Deliberately here and not on ``_ChatSlot``: a plain Slack thread often
        # has no dashboard slot at all, and a slot-held record is simply dropped
        # for those sessions — the whole expiry lifecycle never engages and the
        # stale click it exists to prevent stays possible. Keying by
        # session key makes the slotless case ordinary rather than special.
        #
        # One store, not a slot field plus a fallback: a slot can come into
        # existence at any moment (the channel surface reconciler creates one), so
        # a fallback map would go invisible the instant one appeared. It also
        # avoids the two-store divergence that lets a record be filed under one
        # index and cleared under another.
        self._slack_options_by_key: dict[str, tuple[PostedOptions, ...]] = {}
        self._slot_counter = 0
        # slot key → last context-meter reading, for seeding the bar when a
        # session is reopened after its ACP session is gone. Readings this
        # process took live here immediately; `_loaded` tracks whether the
        # file written by an earlier process has been merged in yet, and
        # `_dirty` whether the off-loop flush still owes a write. The map is
        # touched from the event loop (broadcast/read), the flush executor,
        # and the shutdown thread, so EVERY access — including the flags —
        # holds `_context_snapshots_lock`. File IO happens outside the lock:
        # the flush serializes under it, writes without it.
        self._context_snapshots: dict[str, dict] = {}
        self._context_snapshots_loaded = False
        self._context_snapshots_dirty = False
        self._context_snapshots_lock = threading.Lock()
        # Serializes whole flushes (dirty-check through file write). Two flush
        # paths exist — the periodic executor pass and the shutdown save — and
        # the data lock above deliberately excludes the file write, so without
        # this an overlapping pair can land writes out of order: the slower
        # flush writes an OLDER serialization last, rolling the file back, and
        # the already-cleared dirty flag means nothing corrects it until a new
        # reading arrives. Only flush threads contend here; the event loop
        # never acquires it.
        self._context_snapshots_flush_lock = threading.Lock()
        self._folders: list[dict[str, Any]] = []  # project folder definitions
        self._cron_folders: list[dict[str, Any]] = []  # cron job folder groupings
        # Malformed cron_folders.json entries dropped at load time, kept verbatim
        # so save_cron_folders round-trips them back instead of erasing bytes it
        # could not parse (mirrors the hooks store's unparsed-entry preservation).
        self._unparsed_cron_folder_entries: list[Any] = []
        self._chat_pins: list[dict[str, Any]] = []  # pinned chat messages
        # Malformed chat_pins.json entries dropped at load time, kept verbatim
        # so save_chat_pins round-trips them back instead of erasing bytes a
        # hand-edit left in a shape the loader could not validate (mirrors the
        # cron-folder unparsed-entry preservation).
        self._unparsed_chat_pin_entries: list[Any] = []
        # Serializes pin mutation + persistence so concurrent requests cannot
        # interleave snapshots and replace chat_pins.json out of order.
        # LoopBoundLock, not asyncio.Lock: DashboardState outlives any
        # single event loop (in-process gateway restart, test loops).
        self._chat_pins_lock = LoopBoundLock()
        # Serializes read-modify-write of the folder store; see
        # mutate_folders(). Constructed here rather than lazily so two
        # concurrent first-callers cannot each make their own lock and
        # serialize against nothing. LoopBoundLock binds no loop at
        # construction, so building it off-loop is safe — and it stays valid
        # across the loop changes this long-lived state survives.
        self._folders_lock = LoopBoundLock()
        # Identifies the current folder-TREE snapshot; bumped by mutate_folders
        # (the store's only writer) on each confirmed write. See
        # folders_generation() for what the client does with it.
        self._folders_generation = 0
        # Tag vocabulary: list of {id, name, color, order}. User-managed.
        self._tags: list[dict[str, Any]] = []
        # Malformed tags.json entries dropped at load time, kept verbatim so a
        # save round-trips them back instead of erasing bytes a hand-edit left
        # in a shape the loader could not validate. This store's save path runs
        # DURING load (seed/back-fill), so without preservation a single
        # hand-edited-but-malformed row is wiped at boot with no user action.
        self._unparsed_tag_entries: list[Any] = []
        # True once load_tags() parsed tags.json successfully (or seeded a
        # fresh install). False means the vocabulary state is UNKNOWN (parse
        # or I/O failure) — restore-time pruning must fail open then, because
        # a legitimately-empty vocabulary (user deleted every tag) must still
        # prune dangling ids while an unreadable one must not wipe anything.
        self._tags_authoritative: bool = False
        # Sidebar columns — flat list of {id, name, tag_ids, mode, order, include_untagged}
        self._tag_boards: list[dict[str, Any]] = []
        # Malformed tag-board (sidebar column) entries dropped at load time,
        # kept verbatim so a save round-trips them back rather than erasing a
        # hand-edited-but-typo'd column.
        self._unparsed_tag_board_entries: list[Any] = []
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._dynamic_cards: Any = None
        # Gateway replacement is process-wide, not an ordinary repeatable
        # background mutation.  The task latch coalesces duplicate /api/restart
        # clicks during the response-drain window; the in-progress latch also
        # serializes restart requests arriving through update and other server
        # paths.  Both are cleared when a mocked/failed exec returns, while a
        # successful exec replaces this state with the successor process.
        self._gateway_restart_task: asyncio.Task[None] | None = None
        self._gateway_restart_in_progress: bool = False
        # FIX 2: unattended-turn concurrency cap. Semaphore is created lazily
        # (see _background_turn_sema) because this object outlives / predates
        # the event loop in some hosts. The counters exist so a queued fleet is
        # observable — see background_turn_stats().
        self._bg_turn_sema: asyncio.Semaphore | None = None
        self._bg_turn_cap: int = 0
        self._bg_turns_running: int = 0
        self._bg_turns_waiting: int = 0
        self.no_crons: bool = False  # --no-crons flag: cron execution disabled
        self._hook_store: Any = None  # Lazy-init ScriptHookStore
        # Task refine state (background LLM spec generation)
        self._refine_status: str = "idle"  # idle, running, done, error, cancelled
        self._refine_text: str = ""
        self._refine_error: str = ""
        self._terminal_sessions: dict[str, Any] = {}  # PTY sessions for CLI panel
        self._terminal_reaper: asyncio.Task | None = None  # type: ignore[type-arg]
        self._browser_snapshot_pruner: asyncio.Task | None = None  # type: ignore[type-arg]
        self._browser_install_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # The one browser install job (browser_cli.install_job.BrowserInstallJob)
        # and the InstallScope owning its subprocesses. In memory only: a gateway
        # start has no job, so a "running" state never outlives its process.
        self._browser_install_job: Any = None
        self._browser_install_scope: Any = None
        self._terminal_title_poller: asyncio.Task | None = None  # type: ignore[type-arg]
        # Background reconciler that surfaces channel-originated sessions
        # (slack:<ts>, discord:…) as chat slots. Held to prevent GC.
        self._channel_slot_reconciler: asyncio.Task | None = None  # type: ignore[type-arg]
        self._loop_heartbeat: asyncio.Task | None = None  # type: ignore[type-arg]
        # Off-loop event-loop stall watchdog; armed under the real gateway
        # entrypoint (faulthandler enabled) and stopped on shutdown. Annotated
        # here so the assignment in start_dashboard type-checks under mypy strict.
        self._loop_watchdog: "LoopStallWatchdog | None" = None
        # Listener guard: rebinds the TCP site when its LISTEN socket dies (the
        # Windows proactor accept-failure path) and carries the non-zero exit
        # status the gateway uses when it cannot. Armed after the site binds,
        # detached on cleanup; annotated here for mypy.
        self._listener_guard: "ListenerGuard | None" = None
        # Guard for the SECOND loopback family's listener (see
        # server._arm_secondary_listener_guard). Its own slot rather than sharing
        # the one above: guards chain on the loop's exception handler, so both
        # must be held, and both must be detached in the reverse of the order
        # they were armed.
        self._secondary_listener_guard: "ListenerGuard | None" = None
        # Which listener sidecar each guarded listener owns, keyed "primary" /
        # "secondary", as (port, address, secret). Written after publication and
        # read by the guards' lifecycle hooks, which withdraw the claim while the
        # address is not held and re-publish it once a rebind lands.
        self._listener_sidecars: dict[str, tuple[int, str, str]] = {}
        # Prevent-sleep inhibitor + its poll task. Held to prevent GC and
        # released/cancelled on shutdown; annotated here so the assignments in
        # start_dashboard type-check under mypy.
        self._sleep_inhibitor: "SleepInhibitor | None" = None
        self._prevent_sleep_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Drops the inhibitor's pre-exec stop; run by the cleanup hook.
        self._prevent_sleep_unregister: Callable[[], None] | None = None

        # Knowledge Library
        self._knowledge_store: "KnowledgeStore | None" = None  # Lazy-initialized on first access
        self._knowledge_watcher: asyncio.Task | None = None  # type: ignore[type-arg]
        # Slack channel name resolver (lazy-initialized on first /api/slack/channels hit)
        self._channel_resolver: Any = None
        self._refine_input: str = ""
        self._refine_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._refine_session_key: str = ""
        # slack_client is set via constructor param above; gateway may override later
        self._refine_answer_future: asyncio.Future | None = None  # type: ignore[type-arg]
        # WebSocket clients (multiplexed real-time connection)
        self._ws_clients: list[web.WebSocketResponse] = []
        self._owner_ws_clients: set[web.WebSocketResponse] = set()
        # Slot key -> monotonic time of its last turn end or owner open; what
        # keeps a slot's chips on the fast refresh tier (see
        # ``idle_source_link_urls``).
        self._source_active_at: dict[str, float] = {}
        self._ws_log_subscribers: set[web.WebSocketResponse] = set()
        self._ws_subagent_subscribers: set[web.WebSocketResponse] = set()
        self._websocket_hub = _new_websocket_hub(self)
        self._approval_coordinator = ApprovalCoordinator()
        self._question_coordinator = QuestionCoordinator()
        # Pending tool approvals: id → asyncio.Future[bool]
        self._pending_approvals: dict[str, dict] = {}
        self._approval_futures: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        # Pending agent questions (ask_question MCP tool): ask_id → payload /
        # Future[dict]. Distinct from _approval_futures because the resolution
        # value is the user's answer map, not an allow/deny boolean, and the
        # question card is addressed to one slot rather than the whole gateway.
        self._pending_questions: dict[str, dict] = {}
        self._question_futures: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        # Recent endings let reconnecting owner windows distinguish submitted answers.
        self._recent_question_resolutions: dict[str, tuple[str, float]] = {}
        # Blocking MCP ``ask_question`` calls, keyed by ask_id: which session owns
        # each (a wait from any other session is refused), its redacted
        # questions, deadline and why it ended. See QuestionCoordinator.
        self._agent_asks: dict[str, dict] = {}
        self._flush_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._persistence_coordinator = _new_dashboard_persistence()
        # Update progress tracking (shared across all connected clients)
        self._update_progress: dict[str, str] | None = None  # {step, detail}
        # Restricted (incognito/temporary): session keys with memory writes disabled
        self._restricted_keys: set[str] = set()
        # Ephemeral: session keys with no memory writes at all
        self._ephemeral_keys: set[str] = set()
        # Per-project file index registry (shared across slots)
        from kiro_crew.dashboard.file_index import FileIndexRegistry

        self.file_indexes = FileIndexRegistry()
        # Last-seen {slot_key -> driving member name} for member-driven slots,
        # diffed on each slots broadcast to emit slot/opened + slot/closed to
        # the per-member event log. Best-effort, additive.
        # slot key -> the member's identity MATERIAL, as a (slug, store) pair:
        # a pinned DM slot yields (slug, "") and an ordinary chat slot bound to
        # a member's private store yields ("", store), which the worker resolves
        # to a slug. Neither is resolved here, because that reads the config and
        # this runs on the serving loop.
        self._member_driven_slots_seen: dict[str, tuple[str, str]] = {}
        #: Corrections the checkpoint above owes, for appends that did NOT reach
        #: the log. Queue acceptance is not the same claim as a completed write, so
        #: the worker reports back here and the next broadcast applies these before
        #: it compares.
        #:
        #: A MAPPING rather than a set of keys, because the two directions need
        #: opposite corrections and a key alone can only express one of them. A
        #: failed OPEN must leave the key absent from the checkpoint, so the next
        #: comparison sees it in `current` and re-emits the open: value ``None``.
        #: A failed CLOSE must put the key BACK, so the next comparison sees it in
        #: the checkpoint and not in `current` and re-emits the close: value is the
        #: identity it was closed with. Dropping the key, which is all a set can
        #: say, is a no-op for a close -- the checkpoint has already moved past it.
        self._member_slots_unconfirmed: dict[str, tuple[str, str] | None] = {}
        # Failed SLOT_OPENED transitions, kept VERBATIM for re-emission.
        #
        # The map above corrects the checkpoint so the next comparison recomputes a
        # transition, which is enough for a close but cannot express an open: popping
        # the key only recomputes the open while the slot is STILL open. If it closed
        # in the same window, `current` lacks the key too, the comparison comes out
        # empty, and neither the open nor the close ever reaches the ledger -- the
        # slot's whole episode disappears. Retrying the open verbatim does not depend
        # on the slot's present state, and re-emitting it BEFORE any newly computed
        # close keeps the pair in the order the log has to record them.
        self._member_slots_retry: list[tuple[str, tuple[str, str], str, dict]] = []
        # Wire the per-member event log's broadcast sink. Lazy import + blanket
        # guard: the service module is filled in concurrently and may raise.
        try:
            from kiro_crew.eventlog.service import get_service

            get_service().attach_broadcast(self.broadcast_ws)
        except Exception:
            logger.debug("eventlog attach_broadcast failed", exc_info=True)
        # Runtime services share the gateway's policy, never a model-supplied mode.
        from kiro_crew.dashboard.handlers._shared import (
            live_session_memory_mode,
            require_live_session_memory_mode,
            resolve_session_memory_mode,
        )

        if self.subagents is not None:
            self.subagents._memory_mode_for_session = lambda key: require_live_session_memory_mode(
                self, key
            )
            # The spawn gate resolves a parent's project-scope agent spec in the
            # directory that parent's kiro-cli runs in.
            from kiro_crew.dashboard.handlers._shared import session_project_for_spawn_gate
            from kiro_crew.subagent import set_parent_project_resolver

            set_parent_project_resolver(lambda key: session_project_for_spawn_gate(self, key))
        if self.context_builder is not None:
            self.context_builder.live_memory_mode_for_session = (
                lambda key: live_session_memory_mode(self, key)
            )
            self.context_builder.memory_mode_for_session = lambda key: resolve_session_memory_mode(
                self, key
            )

    def register_channel_transport(self, transport: "MessagingTransport") -> None:
        """Register a live channel transport for cross-surface mirror delivery.

        Called by each channel's gateway at boot, keyed by ``channel_type`` so
        the dashboard turn path can resolve the transport for a session's
        outbound mirror link and deliver a reply via ``send_message``.
        """
        ct = getattr(transport, "channel_type", "")
        if transport is not None and ct:
            self.channel_transports[ct] = transport
            dispatcher = getattr(transport, "dispatcher", None)
            if dispatcher is not None:
                dispatcher.dashboard_state = self

    def get_channel_transport(self, channel_type: str) -> "MessagingTransport | None":
        """Return the registered transport for *channel_type*, or None."""
        return self.channel_transports.get(channel_type)

    def channel_status(self) -> dict[str, dict[str, Any]]:
        """Per-channel ``{connected, error}``, keyed by ``channel_type``.

        Read off the same ``<channel>_connected`` / ``<channel>_connect_error``
        attributes each channel's own settings endpoint reports, so one page cannot
        disagree with another about whether a channel came up. A channel with no
        attributes yet reads as not connected with no reason, which is the honest
        answer for one that never started.

        The error string is bounded here as well as at each settings endpoint: this
        payload is polled, and a channel that reconnects in a loop would otherwise
        publish an unbounded reason on every tick.
        """
        # Imported here rather than at module scope: `channels` imports every
        # channel package, and those import this module through the gateway.
        try:
            from kiro_crew.channels import builtin_channel_descriptors

            names = [d.channel_type for d in builtin_channel_descriptors()]
        except Exception:
            logger.debug("channel status: roster unavailable", exc_info=True)
            return {}
        out: dict[str, dict[str, Any]] = {}
        for name in names:
            if name == "slack":
                connected = self.slack_client is not None and self.slack_socket_connected
            else:
                connected = bool(getattr(self, f"{name}_connected", False))
            out[name] = {
                "connected": connected,
                "error": str(getattr(self, f"{name}_connect_error", ""))[:120],
            }
        return out

    def wire_session_compact_callback(self) -> None:
        """Register the dashboard's compaction callback on the session manager."""

        async def _on_compacted(
            key: str,
            pct: float,
            *,
            success: bool,
            outcome: str = COMPACT_OUTCOME_COMPACTED,
        ) -> None:
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            slot_key = dashboard_slot_key(key)
            if slot_key:
                # A channel-born session with an open tab is readable on BOTH
                # surfaces, and the user may be looking at either one, so both
                # get the notice: silently summarized history is the confusing
                # outcome this notice exists to prevent.
                if is_channel_session_key(key):
                    await self._notify_channel_compaction(
                        key, pct, success=success, outcome=outcome
                    )
            else:
                # No tab to append to, so the notice would be dropped and the
                # user would see summarized history with no explanation. Route
                # it to its own conversation instead.
                await self._notify_channel_compaction(key, pct, success=success, outcome=outcome)
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            if outcome == COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS:
                template = _AUTO_COMPACT_WAITING_NOTICE
            elif outcome == COMPACT_OUTCOME_CANCELLED:
                template = _AUTO_COMPACT_CANCELLED_NOTICE
            elif not success:
                template = _AUTO_COMPACT_FAILED_NOTICE
            elif outcome == COMPACT_OUTCOME_RESTARTED_UNCOMPACTABLE:
                template = _AUTO_RESTART_UNCOMPACTABLE_NOTICE
            elif outcome == COMPACT_OUTCOME_RECYCLED:
                template = _AUTO_RECYCLE_NOTICE
            else:
                template = _AUTO_COMPACT_NOTICE
            message = template.format(pct=pct)
            meta: dict[str, Any] = {"kind": "compaction"}
            record = _compaction_keep_record(key)
            if record is not None:
                # The SAME field the two other decision receipts ride
                # (``meta.decisions_strip``), so the reserved-key protection, the
                # history reload and the live websocket door are all the ones already
                # in place. The frontend dispatches on the record's own ``point``, so
                # a reader that predates this one draws nothing rather than guessing.
                meta["decisions_strip"] = record
            try:
                # Tag kind="compaction" so this proactive auto-compact notice
                # (fired at session.autocompact_pct) is skipped by the dashboard's
                # follow-up [OPTIONS:] backward scan — same invariant as
                # chat_utils._append_compaction_notice. meta.kind covers history
                # reload; slot.append carries the meta on the live broadcast too.
                # (Routing through the chat_utils chokepoint would create a
                # state<->chat_utils import cycle; the notice is a hardcoded
                # template with no LLM content, so its redaction pass is moot.)
                slot.append("assistant", message, "msg msg-a", meta=meta)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append compact notice to slot %s", slot_key
                )
            if success:
                # Reset the context bar — successful compact dropped usage.
                # reset lets the frontend drop its stored token counts too
                # (the "X / Y tokens" tooltip), which no longer describe the
                # compacted session.
                try:
                    self.broadcast_context_usage(
                        slot_key, {"slot": slot_key, "pct": 0.0, "reset": True}
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Failed to broadcast context_usage for slot %s", slot_key
                    )

        self.sessions.set_compact_callback(_on_compacted)

        def _on_compacting_changed(key: str, on: bool) -> None:
            # The slot learns the compaction is RUNNING, not only how it ended:
            # the composer shows it and the Stop button warns on it.
            # Synchronous, from the tick that committed the membership change,
            # so the broadcast that follows agrees with ``is_compacting``.
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            slot_key = dashboard_slot_key(key)
            slot = self.get_slot(slot_key) if slot_key else None
            if slot is None or slot._compacting == on:
                return
            slot._compacting = on
            if not on:
                # The decline marker was this compaction's; the next one, even
                # inside the window, owes its own first refusal.
                slot._stop_declined_at = 0.0
            self.push_slots_update()

        setter = getattr(self.sessions, "set_compacting_callback", None)
        if callable(setter):
            setter(_on_compacting_changed)

    async def _notify_channel_compaction(
        self,
        key: str,
        pct: float,
        *,
        success: bool,
        outcome: str = COMPACT_OUTCOME_COMPACTED,
    ) -> None:
        """Deliver the auto-compact notice to a channel-originated session.

        Isolated from the dashboard leg: a channel that is unreachable, ungoverned
        or unregistered must not turn a successful compaction into an exception on
        the session manager's background task.
        """
        try:
            await deliver_channel_compaction_notice(
                self, key, pct, success=success, outcome=outcome
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to deliver channel compact notice for %s", key
            )

    def wire_session_bind_listener(self) -> None:
        """Register the crew-log class record for a COMMITTED channel binding.

        The session map sees the binding and nothing else about the session; the
        memory mode and the owning app live on the slot. This is where those halves
        meet, the same division as :meth:`wire_session_unbind_listener`.

        Runs SYNCHRONOUSLY, unlike the unbind notice, and that difference is the point
        rather than an oversight. The notice has to reach a transport, so it hops to
        the gateway loop; this has to be recorded BEFORE anything can be routed through
        the binding, and it is the map's own lock -- held across this call -- that
        guarantees it. Hopping to the loop would put the record after traffic could
        arrive. Every step here is cheap and non-blocking: reading slot attributes,
        one probe of the map (whose lock is reentrant, so the same thread re-enters it
        safely), and an append handed to the crew log's writer without waiting.
        """

        def _on_bind(key: str) -> None:
            slot = self._slots.get(key.partition(":")[2] or key)
            if slot is None:
                # No live slot: nothing is authoring into a crew log under this key
                # right now, so there is no class to record. A later turn opens the log
                # and states the class it finds then.
                return
            self.note_crew_log_class(slot)

        self.sessions.set_bind_listener(_on_bind)

    def note_crew_log_class(self, slot: Any) -> None:
        """Record *slot*'s current class in its crew log, if it has moved.

        THE recorder. Every surface that commits a change to a session's class reaches
        it -- the session map's bind announcement, and the paths that set a link on a
        slot directly -- so there is one place the fact is written and one place to
        read to know when it is written. ``test_crew_log_class_recorder.py`` derives the
        call-site list from the source and fails if a new one appears outside it.

        Best-effort by contract, because a binding must not fail for want of a record.
        What makes that safe is the far end rather than optimism: the append is handed
        to the crew log's writer without waiting, a write the writer permanently loses
        is itself recorded, and the class fold reads a dropped write as a hole -- so a
        lost record costs a cross-session read a refusal, never a silent grant.

        The module-level :func:`note_crew_log_class` is what the surfaces outside this
        class call, and it tolerates a state object that does not have this method at
        all. That is not defensive padding: the link-setting paths are reached in tests
        by state DOUBLES, and a record is not worth turning an injection into a failure.
        """
        _record_crew_log_class(self, slot)

    def wire_session_unbind_listener(self) -> None:
        """Register the channel notice for a removed inbound resume binding.

        The session map audits every removal itself; what it cannot do is reach
        the conversation, because that means resolving a transport. This is where
        those halves meet. Called from async gateway startup, which is what makes
        the loop capture below correct: the listener itself runs on whatever thread
        performed the clear, so the loop has to be bound here.
        """
        loop = asyncio.get_event_loop()

        def _on_unbind(key: str, link: ChannelLink, reason: str) -> None:
            if reason == UNBIND_REASON_USER_UNLINK:
                # The in-channel unlink command has already replied in this very
                # conversation, so a notice here would be an echo of it.
                return
            if loop.is_closed():
                # The gateway is shutting down; there is nothing left to deliver
                # on. The SEL event already recorded the removal.
                logger.debug("Gateway loop closed; dropping inbound-unbind notice for %s", key)
                return
            try:
                # ``call_soon_threadsafe`` rather than a call-time
                # ``get_running_loop``: SessionMap is synchronous and a clear can
                # arrive on a worker thread, where there is no running loop and the
                # notice would be dropped. The loop captured at wire time is the
                # gateway's own. Stays SYNC and returns at once — the map holds its
                # lock across this call.
                loop.call_soon_threadsafe(self._spawn_unbind_notice, key, link, reason)
            except RuntimeError:
                # Raced a shutdown between the is_closed check and the call.
                logger.debug("Gateway loop gone; dropping inbound-unbind notice for %s", key)

        self.sessions.set_unbind_listener(_on_unbind)

    def _spawn_unbind_notice(self, key: str, link: ChannelLink, reason: str) -> None:
        """Start the notice task on the gateway loop, retaining a strong reference.

        Runs ON the loop (``call_soon_threadsafe`` target), so creating the task is
        safe here. Tracked in ``_background_tasks`` for the same reason
        :meth:`_spawn_ws_send` does it: the loop holds only a weak reference, so an
        untracked task can be collected mid-send and the notice silently vanishes.
        """
        task = asyncio.ensure_future(self._notify_inbound_unbind(key, link, reason))
        self._background_tasks.add(task)
        task.add_done_callback(self._on_unbind_notice_done)

    def _on_unbind_notice_done(self, task: "asyncio.Task") -> None:  # type: ignore[type-arg]
        """Release the finished notice task and consume any exception it stored.

        ``_notify_inbound_unbind`` swallows its own delivery failures, so an
        exception here is unexpected; reading it keeps asyncio from logging a bare
        "exception was never retrieved" at GC time.
        """
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("inbound-unbind notice task failed: %s", exc)

    async def _notify_inbound_unbind(self, key: str, link: ChannelLink, reason: str) -> None:
        """Tell the conversation behind *link* that it is no longer attached.

        Rides the governed cross-surface ladder rather than the transport directly,
        so the send is capability-checked and governance-vetted like every other
        outbound notice. Best-effort: the binding is already gone and audited, so an
        unreachable, ungoverned or unregistered channel is logged and dropped
        rather than raised on a background task.
        """
        # Lazy: chat_runner imports this module at scope, so a top-level import
        # here would close the cycle.
        from kiro_crew.dashboard.chat_runner import _resolve_channel_target

        try:
            # Off-loop: the ladder's governance gate walks the profile directory,
            # which is unbounded on slow storage.
            target = await asyncio.to_thread(_resolve_channel_target, self, key, link)
            if target is None:
                # The docstring's "logged and dropped" promise, kept: an
                # unreachable, ungoverned or unregistered channel means the user
                # was NOT told their conversation lost its way back, and a silent
                # return here leaves that undeliverable notice invisible to the
                # operator too (the SEL event records the removal, not the
                # delivery failure).
                logging.getLogger(__name__).warning(
                    "inbound-unbind notice for %s undeliverable: no governed %s transport",
                    key,
                    link.channel_type,
                )
                return
            resolved, transport = target
            notice = _INBOUND_UNBIND_NOTICE.format(
                title=self._unbind_notice_title(key),
                why=_INBOUND_UNBIND_WHY.get(reason, _INBOUND_UNBIND_WHY_DEFAULT),
            )
            # The title is user-controlled (a rename, or an LLM-authored one), so
            # the rendered notice goes through the SHARED outbound display sink —
            # display canonicalization, exfiltration URLs, credentials, then
            # mention defang — rather than a second copy of that order here.
            await transport.send_message(
                resolved.channel_id,
                display_safe(notice),
                thread_id=resolved.thread_id,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to deliver inbound-unbind notice for %s", key, exc_info=True
            )

    def _unbind_notice_title(self, key: str) -> str:
        """Name the detached session the way the user saw it, falling back to *key*.

        A title only exists while a slot is displaying the session; the raw key
        still identifies it, so nothing beyond the in-memory slot is worth a lookup.
        """
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        slot_key = dashboard_slot_key(key)
        slot = self.get_slot(slot_key) if slot_key else None
        if slot is None:
            return key
        return slot.display_title or key

    def wire_session_recycle_callback(self) -> None:
        """Register the dashboard's recycle-notification callback.

        Fired when the watchdog recycles a session (e.g. RSS threshold). Posts a
        notice into the slot so the user understands why their session reset.
        """

        async def _on_recycled(key: str, *, reason: str) -> None:
            from kiro_crew.dashboard.chat_utils import (
                _broadcast_expired_oauth_banners,
                dashboard_slot_key,
            )

            # A channel-born session's key is the channel's own even while its
            # tab is open, so ask which tab displays it rather than reading the
            # key's prefix — otherwise that tab resets with no explanation.
            slot_key = dashboard_slot_key(key)
            if not slot_key:
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            message = _SESSION_RECYCLED_NOTICE.format(reason=reason)
            try:
                # Tag kind="compaction" so the dashboard's follow-up [OPTIONS:]
                # backward scan skips this proactive system notice, matching the
                # auto-compact notice invariant. `notice` marks it as borrowing
                # the tag: it reports no compaction, so `is_turn_interrupted`
                # must not read it as a `/compact` request's result.
                slot.append(
                    "assistant",
                    message,
                    "msg msg-a",
                    meta={"kind": "compaction", "notice": "session_recycled"},
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append recycle notice to slot %s", slot_key
                )
            # The recycle ended the child that owned any open MCP OAuth
            # banner's loopback listener; push the read gate's verdict so an
            # open tab withdraws the dead Authorize link without waiting for
            # its next refetch.
            try:
                _broadcast_expired_oauth_banners(self, slot)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to broadcast OAuth banner expiry for slot %s", slot_key
                )

        self.sessions.set_recycle_callback(_on_recycled)

        def _on_stuck_turn(key: str, parked_secs: float) -> None:
            """Surface a stuck turn in the chat where it is happening.

            Same delivery choice as the recycle notice above, for the same
            reason: the person who needs to know is whoever is watching that
            session, so the notice goes to that transcript rather than to a DM or
            a global feed. A WARNING in the journal is not reaching a user.

            Sync, unlike ``_on_recycled``: the hook that fires this is not
            awaiting anything, and appending to a slot needs no I/O.
            """
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            # A channel-born session's key is the channel's own even while its tab
            # is open, so ask which tab displays it (see _on_recycled).
            slot_key = dashboard_slot_key(key)
            if not slot_key:
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            message = stuck_turn_notice(parked_secs)
            try:
                # kind="compaction" for the same reason as the recycle notice: it
                # keeps the dashboard's follow-up [OPTIONS:] backward scan from
                # treating a proactive system notice as the turn's own output.
                # `notice` marks the borrowed tag: a stuck turn is the OPPOSITE
                # of a completed one, so `is_turn_interrupted` must not read
                # this row as a `/compact` request's result.
                slot.append(
                    "assistant",
                    message,
                    "msg msg-a",
                    meta={"kind": "compaction", "notice": "stuck_turn"},
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append stuck-turn notice to slot %s", slot_key
                )

        self.sessions.on_stuck_turn = _on_stuck_turn

    @staticmethod
    def served_bundle_id(index: Path | None = None) -> str:
        """A short content hash of the SERVED frontend bundle's entry point.

        The WS status frame already lets the SPA reload itself across a gateway
        UPGRADE (it compares ``version`` between pushes), but a rebuild of the
        same version — the normal state of a git-checkout dev install, whose
        in-app update pulls, rebuilds, and restarts without the version moving —
        changes neither ``version`` nor ``commit``-visible fields in a way every
        open tab observes. This id is the field that does move: it hashes the
        entry ``index.html`` under ``static/dist`` (whose hashed asset names
        change on every rebuild), so any tab holding a stale bundle can detect
        the swap on its next status frame and reload.

        Cached by ``(mtime_ns, size)``: the snapshot sits on the hot status
        path (every ``/api/status`` poll and WS push), so the file is re-read
        only when a rebuild actually replaced it. Empty string when there is no
        served bundle (source tree without a built frontend, unit tests) — the
        SPA treats empty as UNKNOWN and never reloads over it.

        ``index`` is injectable for tests; the default is the entry point the
        gateway actually serves (``server.py``'s ``_DIST_DIR``).
        """
        if index is None:
            index = _SERVED_INDEX
        try:
            st = index.stat()
        except OSError:
            return ""
        key = (st.st_mtime_ns, st.st_size)
        cached = _BUNDLE_ID_CACHE.get("v")
        if cached and cached[0] == key:
            return cached[1]
        try:
            digest = hashlib.sha256(index.read_bytes()).hexdigest()[:16]
        except OSError:
            return ""
        _BUNDLE_ID_CACHE["v"] = (key, digest)
        return digest

    def _count_lessons(self) -> int | None:
        """Count available Global lessons without blocking the recovery dashboard."""
        from kiro_crew.memory_startup import MemoryStartupUnavailable

        try:
            count = len(self.lessons.load_all())
            if self.context_builder:
                vs = self.context_builder.memory.vector_store
                if vs:
                    # COUNT(*) keeps status polling from materializing lessons.
                    count += vs.count_lessons()
            return count
        except MemoryStartupUnavailable:
            # The selected store's Recovery view supplies the diagnostic. Keep
            # the dashboard shell usable and do not misreport unavailable as 0.
            return None

    def status_snapshot(
        self,
        *,
        cron_jobs: int | None = None,
        lessons: int | None = None,
        update_available: bool | None = None,
        update_can_apply: bool = False,
        update_check_status: str = "unchecked",
        update_command: str = "",
        update_latest_version: str = "",
        update_latest_version_display: str = "",
        update_channel: str = "",
        update_channel_move_pending: bool = False,
        update_managed_by: str = "",
        update_commits_ahead: int = 0,
        update_commits_behind: int = 0,
        update_last_checked_at: float | None = None,
        update_check_interval_secs: int = 43200,
        update_required: bool = False,
        update_min_version: str = "",
        update_can_arm: bool = False,
        update_auto_effect: str = "unknown",
        update_bundled_by_app: bool = False,
        version_display: str = "",
        bundle_id: str = "",
    ) -> dict[str, Any]:
        """Core status fields shared by /api/status, SSE, and WebSocket pushes.

        ``cron_jobs`` and ``lessons`` are supplied by the caller and never
        computed here: the only production caller is
        ``status_counts.cached_status_snapshot``, which loads both counts off
        the event loop through the shared count cache. Computing them inline
        would run ``_count_lessons`` (a JSONL read plus a SQLite ``COUNT(*)``)
        and ``crons.count_enabled_from_disk`` (a ``crons.json`` parse) on the
        loop thread -- the ``no-blocking-call-on-event-loop`` freeze class this
        emitter path exists to avoid. ``None`` means the count is unknown and
        renders as a loading skeleton, never an authoritative 0.
        """
        uptime = int(time.time() - self.start_time)
        branch, commit = self._build_info
        return {
            "uptime": _fmt_duration(uptime),
            # Auto-approve modes forbidden by the ``approval_modes`` policy
            # scope. In the shared snapshot so HTTP/SSE/WS frames all agree —
            # the picker hides these and the value must survive a WS push.
            "disabled_approval_modes": cached_disabled_approval_modes(),
            "start_time": self.start_time,
            "sessions": self.sessions.count,
            "messages": self.messages_received,
            "cron_jobs": cron_jobs,
            "lessons": lessons,
            "subagents": self.subagents.count if self.subagents else 0,
            "update_available": update_available,
            # Can THIS install replace its own code without the user leaving the
            # app? Only a git checkout can (``POST /api/update`` is git fetch +
            # reset). Shipped alongside the availability flag so the dashboard can
            # offer an Update button that will actually work, instead of one that
            # 409s on a wheel install — it must not have to run a fresh check just
            # to learn the layout.
            "update_can_apply": update_can_apply,
            # Where the check itself got to: "unchecked", "checking", "succeeded",
            # "failed" or "deferred". ``update_available`` is only authoritative on
            # "succeeded", and is null otherwise — without this pair the UI cannot
            # tell "checked and current" from "never checked", and painting a green
            # "Up to date" pill next to a red "couldn't check" line is the exact
            # half-truth the update contract exists to prevent.
            "update_check_status": update_check_status,
            # The upgrade command for an install that cannot replace itself, so the
            # 12-hourly BACKGROUND check can light the nav badge and still land the
            # user on something actionable. Deriving it only from a manual check
            # left the badge pointing at an Update button that 409s.
            "update_command": update_command,
            # The candidate release's version string ("" until a check finds a
            # newer build). The proactive update popup keys its per-version
            # snooze/skip on this, so it rides the hot-path subset; the
            # changelog text deliberately does not.
            "update_latest_version": update_latest_version,
            # DISPLAY-ONLY fold of the candidate above (clean base on the
            # stable channel); the popup's snooze/skip records key on the raw
            # value. Empty string on emitters that don't pass it.
            "update_latest_version_display": update_latest_version_display,
            # The release channel this INSTALL follows (the ``channel`` file
            # cli.sh wrote), empty when the layout has no channel at all (a git
            # checkout tracks a remote; a desktop bundle or container is updated
            # by something else). Distinct from ``release_channel`` below, which
            # is derived from the running version string and answers "which lane
            # were these BYTES built on". The two diverge for the whole window
            # between switching channels and the new lane's build landing, so the
            # switcher must key on this one or it would snap back on every poll.
            "update_channel": update_channel,
            # Is the running build ahead of everything ``update_channel``
            # publishes? True means that lane never shipped these bytes, so the
            # install is not on it yet and only re-running the installer moves
            # it. Deliberately NOT derived by comparing ``update_channel``
            # against ``release_channel`` below: promotion re-points the soaked
            # candidate's bytes without re-stamping them, so a promoted stable
            # build reports ``release_channel: insider`` while being a stable
            # install with nothing pending. False on every layout with no feed
            # answer.
            "update_channel_move_pending": update_channel_move_pending,
            # Who manages updates on this host: "" (self-managed), or the
            # mechanism that owns them (e.g. "command" for a policy-pinned
            # provider). The panel keys its update copy on this — a
            # command-managed host must not render self-managed installer
            # instructions its policy exists to bypass.
            "update_managed_by": update_managed_by,
            # Commit distance from a git checkout's upstream, both directions.
            # DIVERGED (both > 0) reports ``update_available: False`` exactly
            # like a current checkout — the destructive apply paths must never
            # be offered local commits — so without the counts the badge cannot
            # tell the two apart. 0/0 on non-git layouts and before any check.
            "update_commits_ahead": update_commits_ahead,
            "update_commits_behind": update_commits_behind,
            "update_can_arm": update_can_arm,
            # What an available update leads to on this install; see
            # ``update_capability.auto_update_effect``.
            "update_auto_effect": update_auto_effect,
            # Whether the desktop app bundles this gateway; see
            # ``update_capability.bundled_by_desktop_app``.
            "update_bundled_by_app": update_bundled_by_app,
            "update_last_checked_at": update_last_checked_at,
            "update_check_interval_secs": update_check_interval_secs,
            # Mandatory-update verdict (enterprise governance pin OR the release
            # feed's breaking-change floor) plus the floor that triggered it.
            # The proactive update modal reads these off the status frame and
            # drops its snooze/skip affordances while required — it must not
            # depend on the user opening Settings to learn the update is
            # mandatory.
            "update_required": update_required,
            "update_min_version": update_min_version,
            # The RUNNING build's version folded for display (clean base on
            # the stable channel; see `_display_version` in
            # handlers/updates.py). DISPLAY-ONLY sibling of the raw `version`
            # the WS frame appends after this snapshot — that one is what the
            # SPA compares across pushes to force a reload over a gateway
            # upgrade, so it must never be folded. Empty string on emitters
            # that don't pass it (they render the raw version, as before).
            "version_display": version_display,
            "no_crons": self.no_crons,
            "branch": branch,
            "commit": commit,
            # Content hash of the SERVED frontend bundle (see
            # ``served_bundle_id``). The SPA compares it across status pushes
            # and reloads when it moves — the signal ``version`` cannot give
            # for a same-version rebuild (a git checkout's in-app update), and
            # one that reaches every open tab, not just the one that clicked
            # Update. Empty when no built bundle is served (dev source tree),
            # which the SPA treats as unknown, never as a change.
            # Read off the loop by ``cached_status_snapshot``.
            "bundle_id": bundle_id,
            # Which release lane these bytes came from: "nightly", "insider" or
            # "stable". Shipped as a RESOLVED ANSWER rather than leaving the
            # dashboard to parse `version` itself, because the rule is not
            # obvious (the same release is stamped as SemVer for desktop and
            # PEP 440 for wheels, and neither PEP 440 prerelease spelling
            # contains a `-`) and a frontend mirror of it would drift silently.
            # The dashboard uses this to give prerelease users an obvious way to
            # report a bug; see release_channel.py for the full rule.
            "release_channel": _release_channel_of_build(),
            # True only when Socket Mode actually connected this session, not
            # merely that tokens were present at boot. slack_client is set
            # whenever tokens existed, even if connect() then failed
            # (invalid_auth, a network error), so keying the status badge on it
            # alone painted a green "Connected" over a Slack that never came up.
            # Require BOTH a wired client and the real connect outcome the
            # gateway records after _connect_slack. This is the same field
            # /api/slack/config already reports to the settings badge.
            "slack_connected": (self.slack_client is not None and self.slack_socket_connected),
            # Every OTHER channel's live state, from the same flags each channel's
            # settings badge reads. Only `slack_connected` reached this payload
            # before, so System > Services was silent about a Telegram or Discord
            # channel that failed to start — the operator saw a healthy page and a
            # bot that never answered. Derived by roster loop, so the next channel
            # is covered without touching this dict.
            "channels": self.channel_status(),
            # Governance enforcement health: "active" (enforcing),
            # "disabled" (permissive default / not restricting), "degraded" (a
            # fail-closed trip, integrity mismatch, or unverified policy this
            # session), or "unknown" (policy not yet loaded).  Pure in-memory read.
            "governance": _governance_status(),
            # FIX 2: cap / in-flight / queued counts for unattended app-owned
            # turns. Published so a fleet parked behind the cap is visibly
            # throttled rather than looking like a set of hung workers.
            "background_turns": self.background_turn_stats(),
        }

    _APPROVAL_TIMEOUT = 7200  # 2 hours — triggers pause (not skip/fail) via deny path
    # Background sources (cron, heartbeat, taskrunner) have no human responder, so
    # waiting the full human window would burn 2h on every unattended approval. They
    # wait only this short window and then deny-fast, letting the turn proceed/fail
    # rather than hang.
    _BACKGROUND_APPROVAL_TIMEOUT_SECS = 180  # 3 minutes — deny-fast for unattended runs
    # Agent questions block a live MCP tool call, so the ceiling is bounded by
    # how long the agent transport will hold that call open — far shorter than
    # the 2h approval window. Callers pick a value inside these bounds.
    _QUESTION_TIMEOUT_DEFAULT = 300  # 5 minutes
    # Hard ceiling set by the ACP tool-stall watchdog, NOT by the `wait` tool.
    # `acp/client.py::_TOOL_STALL_TIMEOUT` is 600s and is armed once a tool call
    # is dispatched; a blocked ask_question emits no progress frames, so a window
    # at or beyond 600s lets the watchdog declare the turn dead and kill it —
    # after which an answer has no turn left to return to. 540s keeps a 60s
    # margin below the watchdog. `wait` can afford 1800s because it is a
    # different mechanism; copying that number here was the bug.
    _QUESTION_TIMEOUT_MAX = 540  # 9 minutes — 60s under the 600s tool-stall watchdog
    # The blocking MCP ``ask_question`` is not bound by the cap above: its tool
    # call waits in short slices and pings /api/session-keepalive between them,
    # which resets the stall watchdog the same way the ``wait`` tool does. So
    # the window is sized for a person, not for a transport. A tool call that
    # stops polling for _AGENT_ASK_STALE_SECS is presumed dead and its card is
    # withdrawn, so a crashed subprocess cannot leave a card collecting answers.
    _AGENT_ASK_WINDOW_DEFAULT = 1800  # 30 minutes
    _AGENT_ASK_STALE_SECS = 120
    _FLUSH_INTERVAL = 5  # seconds between dirty-slot flushes

    # ── FIX 2: bounded concurrency for unattended, app-owned turns ──────────
    # Nothing capped chat slots or concurrent turns. The nearest analogue caps
    # at 12 (dashboard/handlers/terminal.py::_MAX_SESSIONS, 429 on excess) and
    # the only real ceiling was asyncio.Semaphore(4) on agent cold starts plus
    # host memory — so an app that arms N worker slots could put N turns on the
    # runtime at once and exhaust it. Shape copied from
    # apps/builtins/code_review_sage/sage_lib/review_pool.py (default +
    # ``MAX_CONCURRENT_CEIL`` clamp): configurable, but never unbounded.
    MAX_BACKGROUND_TURNS = 4  # default in-flight unattended turns
    MAX_BACKGROUND_TURNS_CEIL = 16  # hard ceiling — config can raise up to here
    # Longest a queued turn may sit waiting for a permit. Needed because the
    # queue wait happens INSIDE the coroutine ``spawn_guarded_turn`` already
    # bounds at ``CHAT_TURN_TIMEOUT`` (14400s), so an unbounded wait would let a
    # fully-saturated cap consume a turn's whole ceiling and then kill it with
    # "turn exceeded the 14400s ceiling" — a true statement that names the wrong
    # cause. 1800s never trips under ordinary throttling and leaves three and a half
    # hours of the ceiling for the turn itself; on expiry the turn fails with a
    # message that says what actually happened.
    _BACKGROUND_QUEUE_WAIT_SECS = 1800

    _log = logging.getLogger(__name__)

    def approval_timeout_for(self, slot: "_ChatSlot") -> float:
        """Approval window for an interactive tool prompt raised inside *slot*.

        FIX 1. The dashboard runner waits on its OWN per-slot future rather than
        going through :meth:`request_approval`, so it never reached the
        deny-fast background branch: every unattended app worker that tripped
        one untrusted tool held its slot for the full
        ``_APPROVAL_TIMEOUT`` (2h) and then denied anyway — two hours of a
        worker's life spent parked, with nothing on screen to explain it.

        Returning the SAME two constants ``request_approval`` uses is the point:
        the previous bug was a hardcoded ``7200.0`` at the call site, which
        could not track either constant. See :attr:`_ChatSlot.unattended` for
        why app-ownership is the detector.
        """
        if slot.unattended:
            return float(self._BACKGROUND_APPROVAL_TIMEOUT_SECS)
        return float(self._APPROVAL_TIMEOUT)

    def effective_max_background_turns(self) -> int:
        """Configured cap on concurrent unattended turns.

        Reads ``config.json → dashboard.max_background_turns`` (same
        ``_raw_config`` route ``sandbox.py`` and ``mcp_gateway/pool.py`` use for
        their tunables) and clamps to ``[1, MAX_BACKGROUND_TURNS_CEIL]`` so an
        operator can widen the fleet without editing code but can never remove
        the bound. Unreadable/garbage config falls back to the default rather
        than failing a turn.
        """
        try:
            raw = (_raw_config().get("dashboard") or {}).get(
                "max_background_turns", self.MAX_BACKGROUND_TURNS
            )
            val = int(raw)
        except Exception:
            self._log.debug("background-turn cap config unavailable; using default", exc_info=True)
            val = self.MAX_BACKGROUND_TURNS
        return max(1, min(val, self.MAX_BACKGROUND_TURNS_CEIL))

    def _background_turn_sema(self) -> asyncio.Semaphore:
        """The cap's semaphore, created on first use and resized when idle.

        Lazy because ``DashboardState`` is constructed before the event loop in
        some hosts (tests, CLI) and ``asyncio.Semaphore`` binds to the running
        loop. Resized only while nothing is in flight: in-flight holders own
        permits on the object they entered, so swapping under them would let the
        cap be exceeded by the difference.
        """
        eff = self.effective_max_background_turns()
        if self._bg_turn_sema is None:
            self._bg_turn_sema = asyncio.Semaphore(eff)
            self._bg_turn_cap = eff
        elif eff != self._bg_turn_cap and not (self._bg_turns_running or self._bg_turns_waiting):
            self._bg_turn_sema = asyncio.Semaphore(eff)
            self._bg_turn_cap = eff
        return self._bg_turn_sema

    def background_turn_stats(self) -> dict[str, int]:
        """Cap / in-flight / queued counts — the cap's observability surface.

        Surfaced in the status payload and asserted by tests, so "the fleet is
        queued behind the cap" is a readable state rather than an invisible
        stall that looks like a hung worker.
        """
        return {
            "cap": self._bg_turn_cap or self.effective_max_background_turns(),
            "running": self._bg_turns_running,
            "waiting": self._bg_turns_waiting,
        }

    async def run_background_turn(self, slot: "_ChatSlot", coro: Any) -> Any:
        """Await *coro* under the unattended-turn cap.

        QUEUES rather than rejects at the cap: a rejected crew turn loses the
        issue it was mid-way through, while a queued one only starts late. An
        attended slot is passed straight through, so this wrapper is inert for
        every human session and adds no semaphore traffic to the interactive
        path.
        """
        if not slot.unattended:
            return await coro
        sema = self._background_turn_sema()
        queued = sema.locked()
        if queued:
            self._bg_turns_waiting += 1
            # info, not debug: this is the difference between "the fleet is
            # throttled" and "a worker is hung", and it is the only signal a
            # queued turn emits before it starts.
            self._log.info(
                "background turn queued behind the cap: slot=%s cap=%d running=%d waiting=%d",
                slot.key,
                self._bg_turn_cap,
                self._bg_turns_running,
                self._bg_turns_waiting,
            )
        try:
            await asyncio.wait_for(sema.acquire(), timeout=self._BACKGROUND_QUEUE_WAIT_SECS)
        except asyncio.TimeoutError:
            coro.close()
            self._log.warning(
                "background turn abandoned after waiting %ds for a permit: slot=%s cap=%d",
                self._BACKGROUND_QUEUE_WAIT_SECS,
                slot.key,
                self._bg_turn_cap,
            )
            raise TimeoutError(
                f"queued {self._BACKGROUND_QUEUE_WAIT_SECS}s behind the background-turn "
                f"cap ({self._bg_turn_cap} concurrent) without a free slot"
            ) from None
        except BaseException:
            # Cancelled while queued: the turn never ran, so close its coroutine
            # rather than leaving an un-awaited coroutine warning behind.
            coro.close()
            raise
        finally:
            if queued:
                self._bg_turns_waiting -= 1
        self._bg_turns_running += 1
        try:
            return await coro
        finally:
            self._bg_turns_running -= 1
            sema.release()

    @property
    def knowledge_store(self):  # type: ignore[override]
        """Lazy-init KnowledgeStore on first access."""
        if self._knowledge_store is None:
            db_dir = os.path.join(str(config_dir()), "workspace", "knowledge")
            mkdirs_owner_only(db_dir)
            self._knowledge_store = KnowledgeStore.open_recovering(
                os.path.join(db_dir, "knowledge.db")
            )
            # The log alone is not enough: an empty library with no word on
            # screen reads as data loss. The bell feed is where the user looks.
            for warning in self._knowledge_store.warnings:
                self.notify("agent", "Knowledge library was reset", warning, url="/knowledge")
        return self._knowledge_store

    def enable_yolo(self, *, from_config: bool = False) -> None:
        """Activate safety override (delegates to safety_override module)."""
        source = "config" if from_config else "dashboard"
        safety_override().activate(source)

    def disable_yolo(self) -> None:
        """Deactivate safety override (delegates to safety_override module)."""
        safety_override().deactivate("dashboard")

    def is_yolo_active(self) -> bool:
        """Return whether safety override is active (delegates to safety_override module)."""
        return safety_override().is_active()

    @property
    def _yolo(self) -> bool:
        """Backward-compat property for code reading _yolo directly."""
        return safety_override().is_active()

    @_yolo.setter
    def _yolo(self, value: bool) -> None:
        """Backward-compat setter for tests that assign state._yolo = True/False."""
        if value:
            safety_override().activate("dashboard")
        else:
            safety_override().deactivate("dashboard")

    async def request_approval(
        self,
        approval_id: str,
        source: str,
        tool: str,
        *,
        tool_input: str = "",
        tool_purpose: str = "",
        slot: str = "",
        is_background: bool = False,
    ) -> bool:
        """Request interactive approval and deny on timeout or cancellation."""
        return await _approvals_for(self).request(
            self,
            approval_id,
            source,
            tool,
            tool_input=tool_input,
            tool_purpose=tool_purpose,
            slot=slot,
            is_background=is_background,
            redact_url=redact_exfiltration_urls,
            redact_secret=redact_credentials,
        )

    def pending_coordinator_approvals(self, slot_key: str) -> list[dict]:
        """Live coordinator approvals owned by *slot_key*, oldest first.

        A record counts only while its state-level future is still open: a
        resolved or expired approval whose ``finally`` has not yet popped the
        record must not keep the slot in the Needs Approval lane. An approval
        with no owning slot belongs to no slot and is never returned.
        """
        if not slot_key:
            return []
        records = getattr(self, "_pending_approvals", None) or {}
        futures = getattr(self, "_approval_futures", None) or {}
        pending: list[dict] = []
        for approval_id, record in records.items():
            if record.get("slot") != slot_key:
                continue
            future = futures.get(approval_id)
            if future is None or future.done():
                continue
            pending.append(record)
        return pending

    def _audit_and_broadcast_approval(
        self,
        session_key: str,
        approval_id: str,
        approved: bool,
        decision: str = "",
    ) -> None:
        """Audit and broadcast one approval decision."""
        _approvals_for(self).audit_and_broadcast(
            self,
            session_key,
            approval_id,
            approved,
            decision,
            audit_provider=sel,
        )

    def _audit_approval(
        self, session_key: str, approval_id: str, approved: bool, decision: str = ""
    ) -> None:
        """Audit one approval outcome with no broadcast."""
        _approvals_for(self).audit(
            self, session_key, approval_id, approved, decision, audit_provider=sel
        )

    def resolve_state_approval(self, approval_id: str, approved: bool) -> bool:
        """Resolve only a state-level background approval."""
        return _approvals_for(self).resolve_state(self, approval_id, approved)

    def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        rejected_once: bool = False,
    ) -> bool:
        """Resolve one state- or slot-level approval without widening authority."""
        return _approvals_for(self).resolve(
            self,
            approval_id,
            approved,
            rejected_once=rejected_once,
            permission_marker=_permission_marker(),
        )

    def resolve_slot_approval(
        self,
        slot: Any,
        approval_id: str,
        approved: bool,
        *,
        rejected_once: bool = False,
        expected_future: asyncio.Future[str] | None = None,
    ) -> bool:
        """Resolve one approval on *slot*'s own future, skipping state-level approvals.

        *expected_future* pins the one request the caller judged: the call fails
        when the slot now holds a different future under the same id.
        """
        return _approvals_for(self).resolve_on_slot(
            self,
            slot,
            approval_id,
            approved,
            rejected_once=rejected_once,
            permission_marker=_permission_marker(),
            expected_future=expected_future,
        )

    def _redact_questions(self, questions: list[dict]) -> list[dict]:
        """Redact questions and reject text collisions introduced by redaction."""
        return _questions_for(self).redact_questions(
            questions,
            redact_url=redact_exfiltration_urls,
            redact_secret=redact_credentials,
        )

    async def post_question_card(
        self, slot_key: str, questions: list[dict], *, native: bool = False
    ) -> int:
        """Post a non-blocking owner-only question card.

        ``native`` marks kiro-cli's own mid-turn ``AskUserQuestion`` card; see
        ``QuestionCoordinator.post_card``.
        """
        return await _questions_for(self).post_card(self, slot_key, questions, native=native)

    def mark_question_pending(
        self,
        slot_key: str,
        *,
        blocking: bool,
        card_id: str,
        questions: list[dict] | None = None,
        native: bool = False,
    ) -> None:
        """Record one unanswered question and push the slot status."""
        _questions_for(self).mark_pending(
            self,
            slot_key,
            blocking=blocking,
            card_id=card_id,
            questions=questions,
            native=native,
        )
        slot = self._slots.get(slot_key)
        if slot is not None:
            self.notify_dashboard_card(slot, "question")

    def clear_question_pending(
        self,
        slot_key: str,
        *,
        blocking: bool | None = None,
        card_id: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Retire only question records matching both supplied filters."""
        return _questions_for(self).clear_pending(
            self,
            slot_key,
            blocking=blocking,
            card_id=card_id,
            reason=reason,
        )

    def _broadcast_question_retired(
        self,
        slot_key: str,
        cards: list[str] | list[tuple[str, bool]],
        *,
        reason: str | None = None,
    ) -> None:
        """Tell owner clients that question cards are no longer actionable."""
        normalized = [(card, False) if isinstance(card, str) else card for card in cards]
        _questions_for(self).broadcast_retired(self, slot_key, normalized, reason=reason)

    def _push_slots(self) -> None:
        """Push question status without failing the question lifecycle."""
        _questions_for(self).push_slots(self)

    def recent_question_resolutions(self) -> dict[str, str]:
        """Return bounded recent blocking ask endings for reconnect repair."""
        return _questions_for(self).recent_resolutions(self)

    async def request_question(
        self,
        ask_id: str,
        slot_key: str,
        questions: list[dict],
        timeout: int | None = None,
    ) -> dict[str, str] | None:
        """Run the legacy blocking owner question round-trip."""
        return await _questions_for(self).request(
            self,
            ask_id,
            slot_key,
            questions,
            timeout,
        )

    def resolve_question(
        self, ask_id: str, answers: dict[str, str] | None, *, reason: str | None = None
    ) -> bool:
        """Resolve one blocking question future if it is still pending.

        ``reason`` says why a ``None`` answer ended the wait (``dismissed``,
        ``composer``, ...); only a blocking MCP ask reports it back to the agent.
        """
        return _questions_for(self).resolve(self, ask_id, answers, reason=reason)

    async def open_agent_ask(
        self, ask_id: str, slot_key: str, session_key: str, questions: list[dict]
    ) -> int | None:
        """Open a blocking MCP ask, or return ``None`` when live asks fill the cap."""
        return await _questions_for(self).open_agent_ask(
            self, ask_id, slot_key, session_key, questions
        )

    async def wait_agent_ask(self, ask_id: str, session_key: str, slice_secs: float) -> dict | None:
        """Wait one slice for an agent ask; ``None`` when the caller does not own it."""
        return await _questions_for(self).wait_agent_ask(self, ask_id, session_key, slice_secs)

    def withdraw_agent_ask(self, ask_id: str, session_key: str) -> bool:
        """Withdraw an agent ask whose tool call was cancelled."""
        return _questions_for(self).withdraw_agent_ask(self, ask_id, session_key)

    def cancel_questions_for_slot(self, slot_key: str) -> int:
        """Unblock every blocking question owned by one slot."""
        return _questions_for(self).cancel_for_slot(self, slot_key)

    def start_flush_loop(self) -> None:
        _persistence_for(self).start_flush_loop(self)

    async def _flush_loop(self) -> None:
        await _persistence_for(self)._flush_loop(self)

    def flush_slot_now(self, slot: _ChatSlot) -> None:
        _persistence_for(self).flush_slot_now(self, slot)

    def _flush_dirty_slots(self) -> None:
        _persistence_for(self)._flush_dirty_slots(self)

    def _persist_open_slots(self) -> None:
        _persistence_for(self)._persist_open_slots(self)

    def notify(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        meta: dict | None = None,
        url: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        channel: str | None = None,
        member: dict[str, str] | None = None,
        publisher_session: str | None = None,
    ) -> None:
        """Validate and deliver a legacy notification without raising.

        ``channel`` overrides the system channel ``kind`` maps to (see
        :func:`payload_from_legacy`); ``kind`` still reaches the frontend.
        ``member`` attributes the note to the crewmate that published it.
        ``publisher_session`` is the attested session publishing it; its mute applies.
        """
        _notifications_for(self).notify(
            self,
            kind,
            title,
            body,
            meta=meta,
            url=url,
            actions=actions,
            channel=channel,
            member=member,
            publisher_session=publisher_session,
        )

    def _deliver_note(self, note: dict[str, Any]) -> None:
        """Deliver one bus-validated note to memory, clients, and disk."""
        _notifications_for(self).deliver(self, note)

    def register_sse(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new SSE client and return its dedicated queue."""
        return _notifications_for(self).register_sse(self)

    def unregister_sse(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an SSE client queue on disconnect."""
        _notifications_for(self).unregister_sse(self, q)

    async def _rewrite_notifications_async(self) -> None:
        """Rewrite notification storage after earlier FIFO append jobs."""
        await _notifications_for(self).rewrite(self)

    async def delete_notification(self, ts: str) -> bool:
        """Remove a single notification by timestamp and persist to disk."""
        return await _notifications_for(self).delete(self, ts)

    async def ack_notification(self, ts: str) -> bool:
        """Mark a notification as acknowledged and persist it."""
        return await _notifications_for(self).set_acknowledged(self, ts, acknowledged=True)

    async def unack_notification(self, ts: str) -> bool:
        """Mark a notification as unread and persist it."""
        return await _notifications_for(self).set_acknowledged(self, ts, acknowledged=False)

    async def resolve_skill_review_notifications(self, slug: str, consumed_at: str) -> int:
        """Acknowledge the consumed generation of one skill review."""
        return await _notifications_for(self).resolve_skill_reviews(self, slug, consumed_at)

    async def clear_notifications(self) -> None:
        """Clear memory and clients before awaiting the durable rewrite."""
        await _notifications_for(self).clear(self)

    def get_slot(self, name: str) -> _ChatSlot | None:
        """Look up a slot by name without creating it. Returns None if absent.

        Also returns ``None`` for a slot still marked under construction. The
        hydrate loop is synchronous, but the import path does async Layer B
        write/join and a durable save after the loop; it RETRACTS the slot from
        ``_slots`` across that tail as the primary protection, so a lookup finds
        nothing then anyway. This construction-mark check is the belt-and-braces
        layer for any construction path that keeps the slot registered while it
        awaits: acquisition must not hand out a not-yet-finalized session.
        ``serialize_slots`` hides it from the payload; resume dedup reads
        ``_slots`` directly (``_live_slot_resume_response``), so a resuming slot
        stays discoverable for dedup while acquisition through this door is
        refused.
        """
        slot = _registry_for(self).get_slot(self, name)
        if slot is not None and slot.key in getattr(self, "_slots_under_construction", ()):
            return None
        return slot

    def running_session_keys(self) -> frozenset[str]:
        """Return effective session keys whose current slots are running."""
        from kiro_crew.dashboard.chat_utils import effective_session_key

        return _registry_for(self).running_session_keys(self, effective_session_key)

    def spend_slot_by_session(self) -> dict[str, str]:
        """Map live effective session identities to their spend-owning slot keys."""
        from kiro_crew.dashboard.chat_utils import effective_session_key

        return _registry_for(self).spend_slot_by_session(self, effective_session_key)

    def native_subagent_snapshots(
        self,
        terminal_limit: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
        ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    ) -> list[dict[str, object]]:
        """Return bounded native running and terminal cards for WS replay.

        DashboardState owns the slot record shape. The WebSocket layer consumes
        these transport-ready snapshots without reaching into private slot data.
        """
        now = time.time()
        running: list[dict[str, object]] = []
        done: list[dict[str, object]] = []
        for slot in list(self._slots.values()):
            output = slot._native_subagent_output
            for info in list(slot._native_subagent_tracker.values()):
                card_id = str(info.get("id") or "")
                if not card_id:
                    continue
                base: dict[str, object] = {
                    "id": card_id,
                    "slot": slot.key,
                    "task": str(info.get("task") or ""),
                    "agent": str(info.get("agent") or ""),
                }
                if info.get("done"):
                    done_at = float(info.get("done_at") or 0.0)
                    if done_at and (now - done_at) > ttl_secs:
                        continue
                    if info.get("stopped"):
                        outcome = "stopped"
                    elif info.get("error"):
                        outcome = "failed"
                    else:
                        outcome = "completed"
                    done.append(
                        {
                            **base,
                            "done": True,
                            "elapsed": float(info.get("elapsed") or 0.0),
                            "error": info.get("error"),
                            "stopped": bool(info.get("stopped")),
                            "outcome": outcome,
                            "result": str(info.get("result") or ""),
                            "done_at": done_at,
                        }
                    )
                else:
                    running.append(
                        {
                            **base,
                            "done": False,
                            "streaming": native_subagent_output_tail(output.get(card_id, [])),
                            "last_tool": str(info.get("last_tool") or ""),
                            "started": float(info.get("started") or now),
                        }
                    )
        if terminal_limit >= 0 and len(done) > terminal_limit:

            def snapshot_done_at(snapshot: dict[str, object]) -> float:
                value = snapshot.get("done_at")
                return float(value) if isinstance(value, (int, float)) else 0.0

            done.sort(key=snapshot_done_at, reverse=True)
            done = done[:terminal_limit]
        return running + done

    def has_slot(self, name: str) -> bool:
        """Check if a slot exists by name."""
        return _registry_for(self).has_slot(self, name)

    def slot_exists(self, name: str) -> bool:
        """Whether *name* names a session that is open, INCLUDING one still being built.

        The existence question, as opposed to the acquisition one :meth:`get_slot`
        answers. ``get_slot`` hides an under-construction slot so nobody acquires a
        half-finished session, and the import path even retracts its slot from
        ``_slots`` across its async tail while leaving the construction mark set. A
        caller asking "has this session ENDED" must read both as open: a worker that is
        rehydrating or resuming has not closed, and treating it as closed would let the
        work-ledger gate record a permanent "worker gone" for a live worker.

        Two more states are open for the same reason, though no slot object exists for
        either. A key the last open-tab restore could not READ (``unrestored_slot_keys``)
        is a session whose metadata read failed transiently, not one proven gone -- the
        restart-restore snapshot keeps it for exactly that reason. And while a restore is
        in flight (``restoring_open_slots``), a tab it has not reached yet has no slot, so
        absence proves nothing about any name until the restore ends.
        """
        if _registry_for(self).has_slot(self, name):
            return True
        if name in (getattr(self, "_slots_under_construction", None) or ()):
            return True
        if name in (getattr(self, "unrestored_slot_keys", None) or ()):
            return True
        return bool(getattr(self, "restoring_open_slots", False))

    def get_linked_slot(self, session_key: str) -> "_ChatSlot | None":
        """Resolve a Slack link and clean up a stale reverse-index entry."""
        return _registry_for(self).get_linked_slot(self, session_key)

    def resolve_slot(self, name: str) -> _ChatSlot | None:
        """Resolve an exact key or the newest timestamped bare ``chat-N`` key."""
        return _registry_for(self).resolve_slot(self, name, _CHAT_N_RE.fullmatch)

    def link_slack(self, slot_name: str, thread_ts: str, channel_id: str) -> None:
        """Update a slot's Slack link state and persist to SessionStore."""
        slot = self._slots.get(slot_name)
        if not slot:
            logger.warning("slack link skipped: no live slot %r (chat closed?)", slot_name)
            return
        # A thread handoff is ONE action with TWO persisted writes: the previous
        # owner's link is cleared and this slot's is claimed. Each write rewrites
        # the whole session map, so as two separate writes they are separately
        # interruptible — a failure or a concurrent writer in between leaves the
        # thread with no owner (the clear landed, the claim did not) or with two
        # (the reverse). Batching makes the pair one critical section and one
        # write, matching the same guarantee ``SessionMap.set_slack_link``
        # already gives its own eviction-and-claim.
        with self.sessions.batched_save() if self.sessions else contextlib.nullcontext():
            self._link_slack_persisted(slot, slot_name, thread_ts, channel_id)
        self.push_slots_update()

    def _link_slack_persisted(
        self, slot: Any, slot_name: str, thread_ts: str, channel_id: str
    ) -> None:
        """The link handoff itself: in-memory indexes plus both persisted writes.

        Split out only so :meth:`link_slack` can wrap the whole sequence in one
        ``batched_save``; the dashboard push stays OUTSIDE that block because it
        is not a map mutation.
        """
        # Remove stale mapping if slot was previously linked to a different thread
        old_ts = slot._slack_thread_ts
        if old_ts and old_ts != thread_ts:
            self._slack_to_slot.pop(old_ts, None)
        # Clear persisted link of old slot if this thread was previously owned by another slot
        old_owner = self._slack_to_slot.get(thread_ts)
        if old_owner and old_owner != slot_name:
            old_slot = self._slots.get(old_owner)
            if old_slot:
                old_slot._slack_linked = False
                old_slot._slack_thread_ts = ""
                old_slot._slack_channel = ""
            if self.sessions:
                from kiro_crew.dashboard.chat_utils import (
                    _history_key_for,
                    effective_session_key,
                )

                # The previous owner's slot may already be gone; fall back to
                # deriving its key from the name in that case.
                old_key = (
                    effective_session_key(old_slot) if old_slot else _history_key_for(old_owner)
                )
                self.sessions.set_slack_link(old_key, "", "")
        slot._slack_linked = True
        slot._slack_channel = channel_id
        slot._slack_thread_ts = thread_ts
        self._slack_to_slot[thread_ts] = slot_name
        # Persist so link survives gateway restarts
        if self.sessions:
            from kiro_crew.dashboard.chat_utils import effective_session_key

            self.sessions.set_slack_link(effective_session_key(slot), thread_ts, channel_id)

    def get_or_create_slot(
        self,
        name: str | None = None,
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str | None = None,
        ephemeral: bool | None = None,
        app: str = "",
        linked_session_key: str = "",
        channel_origin: bool = False,
        origin: str | None = None,
        *,
        # Opt-in for the survey's "new user" session counter, DISTINCT from the
        # origin tag: ``SlotOrigin.USER`` carries the ``slots:user`` privacy
        # semantics and is deliberately set by non-human paths too (the
        # session-control create verb mints USER slots so agent-created
        # sessions stay app-invisible), so origin alone cannot mean "a person
        # started this chat". Only the human request-layer paths (chat-send
        # auto-create, new-chat tab, fork) pass True. Default False is the
        # chosen fail direction, matching
        # the counter's own philosophy below: a future missed opt-in only
        # delays the survey (lost signal); defaulting to count would let
        # unattended agent activity satisfy the gate (corrupted signal).
        count_user_session: bool = False,
    ) -> _ChatSlot:
        """Return existing slot or create a new one.

        *linked_session_key* binds a new slot to the session its conversation
        actually runs on (a channel thread, a cron job). It must be supplied
        here rather than assigned afterwards: the Slack-link hydration below
        reads the persisted link off the slot's effective session key, so a
        binding applied later would hydrate against the wrong key and leave a
        channel-born tab looking unlinked.
        """
        existing, creation = _registry_for(self).prepare_creation(
            self,
            name,
            mode=mode,
            memory_mode=memory_mode,
            normalize_key=lambda value: _normalize_slot_key(value),
            mint_key=lambda prefix, index, timestamp: _mint_slot_key(prefix, index, timestamp),
            timestamp_provider=lambda: time.time(),
        )
        if existing is not None:
            # An under-construction slot is registered (so a same-key resume
            # dedups against it) but not yet a live session: the import path holds
            # construction across its async Layer B finalization tail. Refuse to
            # hand it to an acquirer that would treat it as resumable before that
            # lands. Create-or-send callers (``api_chat``) handle ValueError as a
            # 409 -- retry once the build finishes.
            if existing.key in getattr(self, "_slots_under_construction", ()):
                raise ValueError(
                    f"slot {existing.key} is still being built; retry once it is ready"
                )
            return existing
        assert creation is not None
        name = creation.key
        # Refuse to MINT on a key that is under construction. The existing-branch
        # guard above only fires when the slot is in ``_slots``; the import path
        # RETRACTS its slot from ``_slots`` for its async Layer B tail while
        # leaving the construction mark set, so a create on that (predictable,
        # ``chat-N-<ts>``-shaped) key would otherwise miss the guard, take this
        # mint path, and produce a second slot sharing the effective session key
        # Layer B was just joined to. Keying on the construction mark rather than
        # ``_slots`` membership covers both the registered and the retracted
        # window. The constructor itself does not trip this: import mints a fresh
        # key (name=None) that is not yet under construction, and only begins
        # construction after this returns. Depends on
        # ``_materialise_slot_from_history`` leaving the construction mark set on
        # its success path (see the comment there); do not change one side alone.
        if name in getattr(self, "_slots_under_construction", ()):
            raise ValueError(f"slot {name} is still being built; retry once it is ready")
        requested_name = creation.requested_name
        minted_new = creation.minted_new
        slot = _ChatSlot(
            name,
            agent=agent,
            workspace=workspace,
            model=model,
            mode=mode,
            memory_mode=memory_mode or "persistent",
        )
        if requested_name and requested_name != name:
            # The caller asked for a human-readable name (e.g. "Artifact: My
            # Doc"); the key had to be folded, but the pretty form makes a
            # better initial title than the "New session" placeholder. Titles
            # are dashboard-surfaced, so apply the same redaction as explicit
            # title pinning in api_chat_slot_create. ``_titled`` stays False —
            # auto-title and explicit pinning can still override.
            pretty_title, _ = redact_exfiltration_urls(requested_name)
            pretty_title, _ = redact_credentials(pretty_title)
            slot.title = pretty_title
        slot._tab_id = uuid.uuid4().hex[:12]
        slot._on_message = self._broadcast_chat_message
        slot._on_row = self._record_member_row
        slot._on_card_event = self.notify_dashboard_card
        slot._on_question_retired = self._broadcast_question_retired
        slot._coordinator_approvals = self.pending_coordinator_approvals
        slot._app = app
        # ``origin`` must be declared by the layer that actually knows it, and
        # an undeclared non-app slot stays UNTAGGED ("") rather than being
        # called USER.
        #
        # Deriving USER here would be fail-OPEN: this function cannot tell a
        # person typing in the dashboard from a background injection, so every
        # untagged caller — cron result injection, workflow inject, Slack, the
        # OpenAI-compatible endpoint — would read as USER and hand that private
        # content to an app holding `slots:user`. Only the request layer
        # knows whether an app token was presented, so USER/APP is decided
        # there (see chat_handlers) and background callers declare CRON/SYSTEM.
        #
        # "" is invisible to every cross-slot scope (the gate compares against
        # SlotOrigin.USER), so a caller that forgets to declare loses
        # visibility instead of leaking — the direction this has to fail in.
        slot._origin = origin or (SlotOrigin.APP if app else "")
        if minted_new and count_user_session and slot._origin == SlotOrigin.USER:
            # Count only genuine, newly-minted user chats toward the survey's
            # "new user" window (session_pulse_counter). `minted_new` excludes
            # restore/rehydrate (which passes the persisted key as name) and
            # get-existing, so a restart never re-counts already-seen sessions.
            # `count_user_session` carries the human-started signal: only the
            # request-layer paths a person actually drives (chat-send
            # auto-create, new-chat tab, fork) opt in, so agent-minted USER
            # slots (the session-control create verb) do not satisfy the
            # survey gate on their own. The
            # origin conjunct stays as the invariant floor: a caller can never
            # count a non-USER slot, flag or not. Best-effort:
            # the helper swallows its own I/O errors and never raises into
            # slot creation.
            #
            # Off the loop, because this method is synchronous and every
            # request-layer birth runs it on the gateway loop -- the counter's
            # read + mkdir + tempfile write + replace would stall it on slow
            # storage. The offload is the counter's, not this allocation's: this
            # block must not become a suspension point, or callers could observe
            # a half-configured slot.
            increment_user_session_count_off_loop()
        if memory_mode and memory_mode != "persistent":
            self._restricted_keys.add(f"dashboard:{name}")
        if ephemeral:
            self._ephemeral_keys.add(f"dashboard:{name}")
        # Hydrate only a complete, genuine Slack link. Other transports still
        # write their namespaced origin id through the legacy channel field;
        # those are projected separately via ``links`` and must never make the
        # destructive Slack actions appear.
        if channel_origin:
            # Additive: never cleared, because get_or_create_slot also returns
            # EXISTING slots and a later plain call must not downgrade a tab
            # that a channel path already claimed.
            slot.channel_origin = True
        if linked_session_key:
            slot.linked_session_key = linked_session_key
            # Every path that sets a link records the class it just changed. Free when
            # the slot has no live session yet, which is the common case here.
            self.note_crew_log_class(slot)
        elif self.sessions and not app:
            # No caller-supplied binding, but a channel-stem name means this slot
            # displays a conversation that runs on the channel's own session.
            # Resolving it HERE rather than in each caller is what makes the
            # binding correct by construction: the History resume path builds the
            # slot without one, and an unbound channel tab silently answers from a
            # dashboard-only session whose replies never reach the thread.
            #
            # Only ever adopts a key the session map actually holds, so a slot
            # whose name merely looks channel-shaped stays unbound. Validated the
            # same way ``surface_channel_session`` validates its own argument:
            # only a real channel key may become a binding, so a malformed map
            # answer leaves the slot unbound (a supported state) rather than
            # routing the user's replies to a session no channel reads.
            #
            # Never for an APP-owned slot: a channel thread is the person's
            # conversation, and the name is app-supplied, so resolving it here
            # would let an app that knows a stem mint a slot bound to — and
            # writing metadata into — a transcript it does not own.
            if is_channel_session_key(name):
                resolved = self.sessions.channel_key_for_stem(name)
                if isinstance(resolved, str) and is_channel_session_key(resolved):
                    slot.linked_session_key = resolved
                    self.note_crew_log_class(slot)
        try:
            if self.sessions:
                from kiro_crew.dashboard.chat_utils import effective_session_key

                _ts, _ch = self.sessions.get_slack_link(effective_session_key(slot))
                slot._slack_linked = _is_genuine_slack_link(_ts, _ch)
                if slot._slack_linked:
                    namespaced = split_namespaced_channel_id(_ch)
                    slot._slack_channel = namespaced[1] if namespaced else (_ch or "")
                    slot._slack_thread_ts = _ts or ""
                    # Rebuild the thread -> slot index too, not just the fields:
                    # inbound replies resolve through the index, so restoring
                    # the fields alone leaves a mirrored session delivering to
                    # Slack but not back to its tab after a restart.
                    #
                    # Index ONLY a genuine mirror-OUT. A channel-born session's
                    # ``slack_thread_ts`` is a SELF-reference -- the thread the
                    # session lives IN, not one it mirrors TO -- and indexing
                    # that would make every inbound Slack message resolve to a
                    # "linked" slot and run through the dashboard chat runner
                    # instead of the Slack transport, silently changing the
                    # execution engine and approval semantics of all Slack
                    # traffic.
                    #
                    # Both tests are load-bearing and neither is a name
                    # heuristic. A channel slot whose stem RESOLVED is caught by
                    # ``linked_session_key``; one whose stem did NOT resolve
                    # (leaving that field empty) is caught by comparing the link
                    # against the slot's own filename stem, because a channel
                    # slot is named for the very thread it lives in. A dashboard
                    # slot that merely happens to be named ``slack_...`` matches
                    # neither test and is still indexed.
                    from kiro_crew.history import _safe_key
                    from kiro_crew.messaging.link import canonical_key

                    _self_ref = False
                    if _ts:
                        _self_ref = _safe_key(canonical_key(_ts)) == name
                    if _ts and not slot.linked_session_key and not _self_ref:
                        self._slack_to_slot[_ts] = name
        except Exception:
            pass
        _registry_for(self).put_slot(self, name, slot)
        # Publish the updated key set to SessionManager and the surface
        # registry NOW, not just on the HTTP slot endpoints. Slots born here
        # programmatically (auto-research campaign workers, cron/workflow
        # inject, task runner, spec builder) never pass through those
        # endpoints, so ``_active_dashboard_slots`` stayed stale and the idle
        # sweep's orphan branch reaped their live sessions as "slot gone" —
        # killing the companion subagent runtime (and any subagent mid-prompt
        # on it) along the way. Guarded: tests build this state without a
        # SessionManager, and a sync failure must never break slot creation.
        if self.sessions:
            try:
                from kiro_crew.dashboard.chat_utils import _sync_dashboard_slots

                _sync_dashboard_slots(self)
            except Exception:
                logger.warning(
                    "get_or_create_slot: active-slot sync failed for %s", name, exc_info=True
                )
        self.push_slots_update()
        return slot

    def live_slot_count(self) -> int:
        """Count published and allocated-but-unpublished slots."""
        return _registry_for(self).live_slot_count(self)

    def creator_slot_count(self, creator_key: str) -> int:
        """Count live slots attributed to one non-empty creator key."""
        return _registry_for(self).creator_slot_count(self, creator_key)

    def begin_slot_construction(self, key: str) -> None:
        """Mark a slot key as allocated but not yet published."""
        _registry_for(self).begin_slot_construction(self, key)

    def end_slot_construction(self, key: str) -> None:
        """Release an allocated-but-unpublished slot marker."""
        _registry_for(self).end_slot_construction(self, key)

    def reseed_slot_counter(self) -> None:
        """Advance the mint counter past every parseable current slot key."""
        _registry_for(self).reseed_slot_counter(
            self,
            slot_index_from_key=lambda name: _slot_index_from_key(name),
            logger_provider=lambda: logger,
        )

    def _broadcast_chat_message(self, slot_key: str, msg: dict) -> None:
        """Push a chat message to all SSE clients via the global stream."""
        workspace = None
        if msg.get("role", "") != "user" and msg.get("content", ""):
            workspace = getattr(self.get_slot(slot_key), "workspace", None)
        self._broadcast(chat_message_note(slot_key, msg, workspace=workspace))

    def _record_member_row(self, slot_key: str, msg: dict) -> None:
        """Append one ``member/message`` for a row landing in a member DM slot.

        Wired as ``Slot._on_row``, which fires for EVERY live appended row, and
        deliberately NOT off ``_broadcast_chat_message``: that method is the SSE
        delivery path and is skipped for a ``user`` row the dashboard composer
        already echoed, and for any row on a slot with an HTTP stream reader
        attached. The roster's ``last_active_ts`` is folded from these events, so
        a recorder reachable only through the delivery path cannot see the one
        action the user most expects to reorder the roster: typing into a
        crewmate's DM.

        Nothing outside a member DM slot is recorded: ``member_slug_for_slot``
        answers ``None`` for every other key.

        Best-effort throughout, deliberately: a logging fault must not break a
        message the user has already sent.
        """
        role = msg.get("role", "")
        content = msg.get("content", "")
        try:
            from kiro_crew import eventlog_hooks
            from kiro_crew.eventlog.types import MEMBER_MESSAGE

            _mslug = eventlog_hooks.member_slug_for_slot(slot_key)
            if _mslug is None:
                return
            _raw_ts = msg.get("ts", "")
            try:
                _ev_ts = float(_raw_ts)
            except (TypeError, ValueError):
                _ev_ts = time.time()

            # Same redaction chain the members roster read uses, run
            # before the length cap so a credential split by truncation
            # cannot leak. The payload is built by the one shared spelling
            # (`member_message_payload` -> `speech_preview`) so the folded
            # preview equals what `GET /api/members` reads back.
            def _sanitize_preview(text: str) -> str:
                text, _ = redact_exfiltration_urls(text)
                text, _ = redact_credentials(text)
                return text

            _payload = eventlog_hooks.member_message_payload(
                role, content, msg.get("meta"), _ev_ts, sanitize=_sanitize_preview
            )

            # Off the event loop: emit opens the member log and does a
            # synchronous os.fsync append. This callback runs loop-side, so
            # hand the write to a worker thread (fire-and-forget, best-effort
            # like the rest of this block) rather than stalling every gateway
            # task on the fsync.
            def _emit_message() -> None:
                eventlog_hooks.emit(
                    _mslug,
                    None,
                    MEMBER_MESSAGE,
                    _payload,
                )

            # Queued on the ordered executor either way -- see the slot
            # emit for why a no-loop caller queues rather than writing
            # inline.
            #
            # The return is deliberately not read, and this is the ONE thing
            # that makes it safe: nothing here records that the event was
            # written. A refused or failed append costs this path exactly the
            # event it was given, which the queue's own ceiling documents and
            # reports. The slot path above cannot do the same because it keeps
            # a checkpoint, and a checkpoint that outlives a lost append turns
            # one missing event into a view that never recovers.
            eventlog_hooks.submit(_emit_message)
        except Exception:
            logger.debug("member/message event-log hook failed", exc_info=True)

    # ── Folder persistence ──

    _FOLDERS_FILE = FOLDERS_FILE
    _TAGS_FILE = "tags.json"
    _TAG_BOARDS_FILE = "tag_boards.json"

    # Seed vocabulary created on first run when tags.json is missing or empty.
    # status=True tags are mutually-exclusive workflow states. Drag-between-columns
    # strips all status tags from a card and applies the destination column's
    # status tag. Non-status tags survive the drag.
    _DEFAULT_TAGS: list[dict[str, Any]] = [
        {"id": "planned", "name": "Planned", "color": "#6b7280", "order": 0, "status": True},
        {"id": "todo", "name": "ToDo", "color": "#3b82f6", "order": 1, "status": True},
        {
            "id": "implementation",
            "name": "Implementation",
            "color": "#8b5cf6",
            "order": 2,
            "status": True,
        },
        {"id": "review", "name": "Review", "color": "#f59e0b", "order": 3, "status": True},
        {"id": "done", "name": "Done", "color": "#10b981", "order": 4, "status": True},
    ]

    def load_folders(self) -> None:
        """Load usable folder definitions without replacing good state on failure."""
        path = config_dir() / self._FOLDERS_FILE
        self._folders = _FOLDER_REPOSITORY.load(path, self._folders)

    def save_folders(self) -> None:
        """Persist folder definitions for synchronous boot-time callers."""
        path = config_dir() / self._FOLDERS_FILE
        _FOLDER_REPOSITORY.save(path, self._folders, self._atomic_write_json)

    _CRON_FOLDERS_FILE = "cron_folders.json"

    def load_cron_folders(self) -> None:
        """Load cron folder definitions from disk.

        Validates the loaded shape: the file must contain a JSON array of
        folder objects. A non-list root (a hand-edited ``{}``, a string) is
        ignored wholesale — it would crash frontend grouping
        (``folders.map is not a function``). Individual malformed entries are
        dropped from the active list but kept verbatim in
        ``_unparsed_cron_folder_entries`` so the next ``save_cron_folders``
        round-trips them back to disk rather than silently erasing a user's
        hand-edited-but-typo'd folder (mirrors the hooks store's contract).
        """
        path = config_dir() / self._CRON_FOLDERS_FILE
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, list):
                    logger.warning(
                        "Ignoring %s: expected a JSON array, got %s",
                        self._CRON_FOLDERS_FILE,
                        type(loaded).__name__,
                    )
                    return

                def _is_valid(f: Any) -> bool:
                    return (
                        isinstance(f, dict)
                        and isinstance(f.get("id"), str)
                        and bool(f.get("id"))
                        and isinstance(f.get("name"), str)
                        and bool(f.get("name"))
                        and isinstance(f.get("order"), (int, float))
                        and not isinstance(f.get("order"), bool)
                    )

                valid, unparsed = self._partition_preserving(
                    loaded, _is_valid, "entr(ies)", self._CRON_FOLDERS_FILE
                )
                self._cron_folders = valid
                self._unparsed_cron_folder_entries = unparsed
        except Exception:
            logger.warning("Failed to load cron folders", exc_info=True)

    def _persist_cron_folders(self, folders: list[dict[str, Any]]) -> None:
        """Atomically write ``folders`` to the cron-folders file.

        Takes the list to persist explicitly so a caller can save a candidate
        list before committing it to ``_cron_folders`` (see ``create_cron_folder``),
        keeping in-memory state and disk from diverging mid-operation. This is the
        single persist chokepoint for all folder mutations: ``create_cron_folder``
        passes a candidate list here before committing it, while ``save_cron_folders``
        (used by ``rename_cron_folder`` and ``delete_cron_folder``) passes the
        already-mutated ``_cron_folders`` to commit an in-place edit. Any
        malformed entries preserved at load time (``_unparsed_cron_folder_entries``)
        are appended, so a save cannot erase bytes a hand-edit left in a shape
        the loader could not validate.
        """
        path = config_dir() / self._CRON_FOLDERS_FILE
        unparsed = getattr(self, "_unparsed_cron_folder_entries", [])
        self._atomic_write_json_strict(path, [*folders, *unparsed])

    def save_cron_folders(self) -> None:
        """Persist cron folder definitions to disk (atomic write).

        Writes the active folders plus any malformed entries preserved at load
        time (``_unparsed_cron_folder_entries``), so a save triggered by an
        unrelated folder operation cannot erase bytes a hand-edit left in a
        shape this loader could not validate. Raises on I/O failure so callers
        can surface a 500 to the client rather than silently losing the write.
        """
        self._persist_cron_folders(self._cron_folders)

    def create_cron_folder(self, name: str, folder_id: str) -> dict:
        """Create a new cron folder and persist.

        Returns the created folder dict. Raises ValueError if ``folder_id``
        collides with an existing folder (``rename``/``delete`` act on the
        first id match, so a duplicate would strand the shadowed folder as
        un-renameable/un-deletable). Raises on persistence failure (callers
        should surface a 500).

        The folder is persisted BEFORE it is exposed in ``_cron_folders``: a
        concurrent ``GET /api/cron-folders`` reads the live list, so appending
        first and saving second would let a reader observe (and the frontend
        render) a folder that a failed save then removes — a transient "ghost"
        folder inconsistent with disk. Building the candidate list, persisting
        it, and only then committing the reference means a reader sees either
        the pre-create list or the durably-saved one, never an intermediate.
        """
        if any(f["id"] == folder_id for f in self._cron_folders):
            raise ValueError("cron folder id collision")
        order = max((f["order"] for f in self._cron_folders), default=-1) + 1
        folder = {"id": folder_id, "name": name, "order": order}
        candidate = [*self._cron_folders, folder]
        self._persist_cron_folders(candidate)
        self._cron_folders = candidate
        return folder

    def rename_cron_folder(self, folder_id: str, name: str) -> dict | None:
        """Rename a cron folder and persist.

        Returns the updated folder dict, or None if folder_id not found.
        Raises on persistence failure (callers should surface a 500);
        original name is restored on failure.
        """
        for folder in self._cron_folders:
            if folder["id"] == folder_id:
                old_name = folder["name"]
                folder["name"] = name
                try:
                    self.save_cron_folders()
                except Exception:
                    folder["name"] = old_name
                    raise
                return folder
        return None

    def delete_cron_folder(self, folder_id: str) -> bool:
        """Remove a cron folder and clear its assignment on all jobs.

        Returns True if the folder existed, False otherwise.
        Raises on persistence failure (callers should surface a 500).

        Ordering: the folder removal is the single authoritative write —
        it is removed from memory and persisted FIRST (rolled back in
        memory if the save fails, keeping memory consistent with disk).
        Job ``folder_id`` clears happen afterwards as best-effort cleanup:
        a dangling ``folder_id`` is benign (grouping renders unknown ids
        in the Ungrouped bucket, and a job's next folder move overwrites
        it), so a crash or per-job failure between writes can never strand
        jobs in a half-deleted state — the folder is either fully present
        or fully gone.
        """
        if not any(f["id"] == folder_id for f in self._cron_folders):
            return False
        # Remove the folder definition and persist — the one write that
        # decides whether the delete happened.
        snapshot = list(self._cron_folders)
        self._cron_folders = [f for f in self._cron_folders if f["id"] != folder_id]
        try:
            self.save_cron_folders()
        except Exception:
            self._cron_folders = snapshot
            raise
        # Best-effort: clear the now-dangling folder_id on affected jobs.
        # Failures are logged and tolerated — consumers treat an unknown
        # folder_id as ungrouped, so a leftover id has no user-visible
        # effect and self-heals on the job's next folder assignment.
        for job in self.crons.list_jobs(include_disabled=True):
            if job.folder_id == folder_id:
                try:
                    self.crons.update_job(job.id, folder_id="")
                except Exception:
                    logger.warning(
                        "Failed to clear folder_id on job %s after folder delete "
                        "(benign: unknown ids render as ungrouped)",
                        job.id,
                        exc_info=True,
                    )
        return True

    # ── Pinned session order ──

    def read_pinned_session_order(self) -> tuple[list[str], bool] | None:
        """Read the stored pinned-session order. Blocking; run off the loop.

        Returns ``(keys, stored)``: ``stored`` says whether an order file
        exists at all, which an empty ``keys`` cannot tell. A person who
        reordered and then unpinned everything has a stored empty order; one
        who never reordered has none. Returns ``None`` on a transient
        ``OSError`` (logged): the order is a display preference, so an
        unreadable file must not replace a good order with an empty one.
        """
        path = pinned_session_order.store_path(config_dir())
        try:
            return pinned_session_order.load_stored(path)
        except OSError:
            logger.warning(
                "could not read %s; keeping the current pinned order",
                pinned_session_order.PINNED_ORDER_FILE,
                exc_info=True,
            )
            return None

    def adopt_pinned_session_order(self, read: tuple[list[str], bool] | None) -> bool:
        """Take a read's order as the in-memory one unless one is already loaded.

        Runs on the loop. The gateway reads the file after it starts serving,
        so a reorder can land first; that write is newer than what was read,
        and this returns ``False`` without touching it. A failed read
        (``None``) leaves the order unloaded so the next writer reads again.
        """
        if self._pinned_session_order_loaded or read is None:
            return False
        self._pinned_session_order, self._pinned_session_order_stored = read
        self._pinned_session_order_loaded = True
        self.bump_pinned_order_rev()
        return True

    def bump_pinned_order_rev(self) -> None:
        """Raise the order revision after the in-memory order changed."""
        self._pinned_order_rev = pinned_session_order.next_revision(
            self._pinned_order_rev, time.time_ns() // 1000
        )

    def save_pinned_session_order(self, keys: list[str]) -> None:
        """Write *keys* as the pinned-session order. Blocking; run off the loop."""
        pinned_session_order.save(pinned_session_order.store_path(config_dir()), keys)

    def pinned_session_flags(self) -> dict[str, bool]:
        """Every live slot key mapped to its pinned flag."""
        return {key: bool(slot.pinned) for key, slot in self._slots.items()}

    def pin_rank(self, slot: Any) -> int | None:
        """*slot*'s position in the pinned order, or ``None`` when it has none.

        Only a pinned slot has a rank, so a key left behind in the stored list
        by an unpin that could not be written ranks nothing.
        """
        if not slot.pinned:
            return None
        try:
            return self._pinned_session_order.index(slot.key)
        except ValueError:
            return None

    # ── Chat message pin persistence ──

    _CHAT_PINS_FILE = "chat_pins.json"

    def load_chat_pins(self) -> None:
        """Load pinned chat messages from disk, dropping malformed records.

        A pin without a ``mid`` field is preserved as-is; new pins always carry
        ``mid``.

        Individual malformed entries are dropped from the active list but kept
        verbatim in ``_unparsed_chat_pin_entries`` so the next ``save_chat_pins``
        round-trips them back to disk rather than silently erasing a user's
        hand-edited-but-typo'd pin (mirrors the cron-folder contract).

        Error classification:
        - Missing file: normal (first run) → empty list.
        - Malformed JSON / invalid shape: tolerated for compatibility → empty list.
        - Transient I/O errors (PermissionError, OSError): MUST NOT replace
          valid in-memory state — re-raise so callers know load failed.
        """
        path = config_dir() / self._CHAT_PINS_FILE
        try:
            if not path.exists():
                self._chat_pins = []
                self._unparsed_chat_pin_entries = []
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            # Malformed content — treat as empty (data corruption).
            logger.warning("chat_pins.json has malformed content: %s", exc)
            self._chat_pins = []
            # Whole-file parse failure: nothing was parsed, so there are no
            # per-entry bytes to preserve. Do NOT clear a prior load's
            # preserved entries against an unreadable read.
            return
        except OSError:
            # Transient I/O error — do NOT clobber valid in-memory state.
            logger.warning("Transient I/O error reading chat_pins.json", exc_info=True)
            raise

        if not isinstance(raw, list):
            logger.warning("chat_pins.json is not a list (%s); ignoring", type(raw).__name__)
            self._chat_pins = []
            return

        # Reuse the create-time ingress caps (source of truth: chat_pins.py) so a
        # hand-edited chat_pins.json cannot smuggle an over-long mid/message_ts/
        # preview past a loader that only checked type + non-emptiness. Imported
        # here (not at module top) because chat_pins.py imports this module.
        from kiro_crew.dashboard.chat_pins import (
            _MAX_MESSAGE_TS_CHARS,
            _MAX_MID_CHARS,
            _MAX_PREVIEW_INPUT_CHARS,
        )

        def _is_valid(pin: Any) -> bool:
            return (
                isinstance(pin, dict)
                and all(
                    isinstance(pin.get(key), str) and pin.get(key) for key in ("id", "slot_key")
                )
                # Require at least one identity field: mid or message_ts
                and bool(
                    (isinstance(pin.get("mid"), str) and pin.get("mid"))
                    or (isinstance(pin.get("message_ts"), str) and pin.get("message_ts"))
                )
                and isinstance(pin.get("preview"), str)
                and isinstance(pin.get("pinned_at"), str)
                and bool(pin.get("pinned_at"))
                # Enforce the create-time length caps at load, so an over-long
                # record is partitioned into _unparsed rather than served. Guard
                # each optional identity with isinstance before len(): a
                # hand-edited record can carry a non-string mid/message_ts (e.g.
                # a JSON number) while still being valid via the other identity,
                # and len() on a non-string would crash the whole loader. preview
                # is already isinstance-checked as a str above.
                and (not isinstance(pin.get("mid"), str) or len(pin["mid"]) <= _MAX_MID_CHARS)
                and (
                    not isinstance(pin.get("message_ts"), str)
                    or len(pin["message_ts"]) <= _MAX_MESSAGE_TS_CHARS
                )
                and len(pin.get("preview") or "") <= _MAX_PREVIEW_INPUT_CHARS
            )

        valid, unparsed = self._partition_preserving(
            raw, _is_valid, "chat pin record(s)", self._CHAT_PINS_FILE
        )
        self._chat_pins = valid
        self._unparsed_chat_pin_entries = unparsed

    def save_chat_pins(self) -> None:
        """Persist pinned chat messages with an atomic, owner-only file replacement.

        Writes the active pins plus any malformed entries preserved at load
        time (``_unparsed_chat_pin_entries``), so a save triggered by an
        unrelated pin operation cannot erase bytes a hand-edit left in a shape
        this loader could not validate.
        """
        path = config_dir() / self._CHAT_PINS_FILE
        unparsed = getattr(self, "_unparsed_chat_pin_entries", [])
        atomic_write(
            path,
            json.dumps([*self._chat_pins, *unparsed]),
            fsync=True,
            mode=0o600,
        )

    async def remove_chat_pins_for_slots(self, slot_keys: set[str]) -> int:
        """Explicitly remove pins for the supplied dashboard slot keys."""
        keys = {key for key in slot_keys if key}
        if not keys:
            return 0
        async with self._chat_pins_lock:
            previous = self._chat_pins
            remaining = [pin for pin in previous if pin.get("slot_key") not in keys]
            removed = len(previous) - len(remaining)
            if not removed:
                return 0
            self._chat_pins = remaining
            try:
                await asyncio.to_thread(self.save_chat_pins)
            except Exception:
                self._chat_pins = previous
                raise
            return removed

    def folders_generation(self) -> int:
        """Monotonic counter identifying the current folder-tree snapshot.

        Rides the ``slots`` WS frame so an already-open tab can tell that the
        folder store actually CHANGED and invalidate its cached
        ``['chat-folders']`` query. Without it a folder created by anything other
        than this tab's own mutation — an agent, a second tab, another device —
        stayed invisible until a reload: the query carries the app-wide
        ``staleTime: Infinity``, so nothing expires it, and the tree the frame
        already carries cannot be written into the cache directly (that would
        clobber an in-flight optimistic edit and, lacking ``history_count``, mark
        the query fresh so the real GET never runs). A generation is the small
        signal that lets the client re-GET instead of guess.

        Read through ``getattr`` because this is reachable from the slots-push
        hot path, which also runs on a ``__new__``-built state that never ran
        ``__init__`` (several endpoint suites build their fixture that way).
        """
        return int(getattr(self, "_folders_generation", 0) or 0)

    async def mutate_folders(
        self,
        mutate: Callable[[list[dict[str, Any]]], tuple[bool, _T]],
        on_committed: Callable[[], None] | None = None,
        prepare: Callable[[], Awaitable[None]] | None = None,
    ) -> _T:
        """Serialize a folder mutation and confirm its off-loop persistence.

        ``on_committed`` runs under the repository lock only after the write
        is proven, so callers can attach side effects that must not outlive a
        rolled-back or no-op transaction. ``prepare`` is awaited under the
        same lock before *mutate* (see :meth:`FolderRepository.mutate`).
        """

        def _mark_committed() -> None:
            # This runs under the repository lock and only after the write is
            # confirmed.  A failed/no-op transaction must not make clients
            # re-fetch a tree that never changed, and concurrent commits must
            # not collapse two monotonic generation bumps into one.
            self._folders_generation = self.folders_generation() + 1
            if on_committed is not None:
                on_committed()

        return await _FOLDER_REPOSITORY.mutate(
            lambda: self._folders,
            self._folders_lock,
            mutate,
            lambda: config_dir() / self._FOLDERS_FILE,
            self._write_folders_confirmed,
            _mark_committed,
            prepare,
        )

    async def read_folders(self, read: Callable[[list[dict[str, Any]]], _T]) -> _T:
        """Run a synchronous reader against committed folder state."""
        return await _FOLDER_REPOSITORY.read(lambda: self._folders, self._folders_lock, read)

    async def hold_folders(self, section: Callable[[list[dict[str, Any]]], Awaitable[_T]]) -> _T:
        """Hold the folder store lock across an awaitable, read-only section.

        The section sees a snapshot of the committed folders and may hop off
        the loop; no folder mutation can commit until it returns. This is how
        a delete elsewhere excludes a concurrent folder pin (the agent
        templates guard) without doing its file work on the loop.
        """
        return await _FOLDER_REPOSITORY.hold(lambda: self._folders, self._folders_lock, section)

    def _write_folders_confirmed(self, path: Path, snapshot: list[dict[str, Any]]) -> None:
        """Persist the complete folder value and prove it landed."""
        _FOLDER_REPOSITORY.write_confirmed(path, snapshot, self._atomic_write_json)

    def folder_breadcrumb(self, folder_id: str, sep: str = " › ") -> str:
        """Render a cycle-safe root-to-leaf folder breadcrumb."""
        return _FOLDER_REPOSITORY.breadcrumb(self._folders, folder_id, sep)

    def read_durable_tags_snapshot(self) -> _DurableTagSnapshot | None:
        """Read committed tags through the bounded no-link file authority.

        ``None`` means the durable state could not be established. A missing
        file is distinguished by a follow-up ``lstat``: only
        ``FileNotFoundError`` is positive absence; every existing, oversized,
        malformed, or unreadable shape remains ambiguous. Active-row parsing
        and legacy status backfill share this state's canonical vocabulary
        rules so mutation reconciliation cannot drift into a second schema.
        """
        path = config_dir() / self._TAGS_FILE
        try:
            encoded = safe_read_file_bytes_nolink(str(path), within_root=str(path.parent))
        except FileTooLargeError:
            return None
        if encoded is None:
            try:
                path.lstat()
            except FileNotFoundError:
                return _DurableTagSnapshot(False, [], [])
            except OSError:
                return None
            return None
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, list):
            return None
        active, unparsed = self._partition_preserving(
            raw,
            lambda row: isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and bool(row["id"]),
            "tag entr(ies)",
            self._TAGS_FILE,
        )
        tags = [dict(row) for row in active]
        default_ids = {row["id"] for row in self._DEFAULT_TAGS}
        for row in tags:
            row.setdefault("status", row.get("id") in default_ids)
        return _DurableTagSnapshot(True, tags, unparsed)

    def load_tags(self) -> None:
        """Load tag vocabulary and sidebar columns from disk; seed defaults if missing.

        Only seed when ``tags.json`` does not exist. An explicitly-empty file
        is left as-is (so a user who deletes every tag stays at zero tags
        across restarts), and a parse failure is left untouched (so a
        transient I/O error never silently overwrites saved data).
        """
        # Claim this process's tag-revision epoch here: load_tags runs off the
        # event loop at startup (asyncio.to_thread) and before any slot is
        # restored, so the claim's disk read/write never lands on the loop.
        ensure_tags_revision_epoch()
        tags_path = config_dir() / self._TAGS_FILE
        file_existed = tags_path.exists()
        try:
            vocab_ok = True
            if file_existed:
                raw = json.loads(tags_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    # Keep rows the active list dropped verbatim so the seed/
                    # back-fill save below (and every later save) round-trips
                    # them back instead of erasing a hand-edited-but-typo'd row
                    # at boot with no user action. An ``id`` that is not a
                    # non-empty string is such a row: every reader keys on the
                    # id (set membership, dict keys, ``str(t["id"])``), and an
                    # unhashable or non-string one would raise there, so it is
                    # preserved on disk but not activated.
                    self._tags, unparsed = self._partition_preserving(
                        raw,
                        lambda t: isinstance(t, dict)
                        and isinstance(t.get("id"), str)
                        and bool(t["id"]),
                        "tag entr(ies)",
                        self._TAGS_FILE,
                    )
                    self._unparsed_tag_entries = unparsed
                else:
                    # Valid JSON but not a list (e.g. {}): the vocabulary
                    # state is UNKNOWN, same as a parse failure — do not let
                    # restore-time pruning wipe assignments against it.
                    vocab_ok = False
                    logger.warning("tags.json is not a list; treating vocabulary as unknown")
            # Authoritative only when the file is missing (fresh install,
            # seeded below) or parsed as a list — INCLUDING a legitimately-
            # empty [] — so restore-time pruning of dangling ids is safe.
            self._tags_authoritative = vocab_ok
        except Exception:
            logger.warning("Failed to load tags", exc_info=True)
            # Treat a parse error like a present file: do not re-seed.
            file_existed = True
            # Vocabulary state unknown — restore-time pruning must fail open.
            self._tags_authoritative = False
        # Back-fill the status flag for legacy tags saved before the field existed.
        # The 5 seed ids are canonical status tags; everything else defaults to False.
        seed_ids = {t["id"] for t in self._DEFAULT_TAGS}
        mutated = False
        for t in self._tags:
            if "status" not in t:
                t["status"] = t.get("id") in seed_ids
                mutated = True
        seeded_default_vocab = False
        if not file_existed and not self._tags:
            # Fresh install (no tags.json on disk) — seed the default vocabulary.
            self._tags = [dict(t) for t in self._DEFAULT_TAGS]
            mutated = True
            seeded_default_vocab = True

        # One-time seed of the agent tag-write grants store, from TRUSTED CODE
        # CONSTANTS only: the default workflow-state tag ids. Never derived
        # from tags.json (agent-writable — promoting its fields into the
        # protected store would launder a forged grant through the upgrade;
        # a forgery hazard). Custom grants are minted by the dashboard CRUD.
        # Default grants are minted ONLY on the boot that also seeds the
        # default vocabulary: an UPGRADED install may have deleted those tags,
        # and granting their ids anyway would let an agent restore the id in
        # agent-writable tags.json and inherit the authority after restart
        # (a forgery hazard). Everyone else gets an EMPTY store.
        #
        # ORDERING (crash atomicity, same rule as the create endpoint): the
        # grants are seeded BEFORE the vocabulary commit. A crash between the
        # two then leaves grant rows for ids no vocabulary entry references —
        # inert, and the next boot (still a fresh install: no tags.json) seeds
        # the vocabulary while ``seed_default_grants`` keeps the store that
        # already verifies. The reverse order leaves a durable seeded
        # vocabulary whose next boot reads as an upgraded install and seeds an
        # EMPTY store, demoting every default workflow state to human-only.
        try:
            seed_default_grants(
                [t["id"] for t in self._DEFAULT_TAGS if t.get("status")]
                if seeded_default_vocab
                else []
            )
        except Exception:
            logger.warning("agent-tag grant seed failed", exc_info=True)
        if mutated:
            self.save_tags()

        # Upgraded installs (store present or vocabulary pre-existing) still
        # need STATUS IDENTITY for the default workflow-state ids: without a
        # row, set_state reads a default status tag as non-status and skips
        # exclusive-peer stripping, so two workflow states persist. Identity
        # rows are policy "none" — they constrain and grant nothing, so a
        # restored id in agent-writable tags.json inherits no authority.
        # Ids from code constants only; existing rows are never touched.
        try:
            seed_status_identity_rows([t["id"] for t in self._DEFAULT_TAGS if t.get("status")])
        except Exception:
            logger.warning("status-identity row seed failed", exc_info=True)

        # Column layout: flat list of {id, name, tag_ids, mode, order}.
        # Empty list = single implicit "all sessions" column (legacy UX).
        columns_path = config_dir() / self._TAG_BOARDS_FILE
        try:
            if columns_path.exists():
                raw = json.loads(columns_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    # Preserve dropped columns verbatim so a later save
                    # round-trips them rather than erasing a hand-edited-but-
                    # typo'd column. Same id rule as the tag rows above.
                    self._tag_boards, unparsed_cols = self._partition_preserving(
                        raw,
                        lambda c: isinstance(c, dict)
                        and isinstance(c.get("id"), str)
                        and bool(c["id"]),
                        "sidebar column(s)",
                        self._TAG_BOARDS_FILE,
                    )
                    self._unparsed_tag_board_entries = unparsed_cols
                    # Prune column tag_ids missing from the vocabulary: tag
                    # deletion commits the vocab write first (crash-atomic),
                    # so a crash mid-delete can leave dangling ids here. The
                    # column API rejects unknown ids, so a dangling id left
                    # in place would make that column's filter permanently
                    # un-editable (the popover echoes the full list back).
                    # Same fail-open rule as the slot-restore prune: only
                    # prune when the vocabulary is authoritative.
                    if self._tags_authoritative:
                        known = {t.get("id") for t in self._tags}
                        for col in self._tag_boards:
                            tag_ids = col.get("tag_ids")
                            if isinstance(tag_ids, list):
                                col["tag_ids"] = [t for t in tag_ids if t in known]
        except Exception:
            logger.warning("Failed to load sidebar columns", exc_info=True)

    def save_tags(self) -> None:
        """Persist tag vocabulary to disk (atomic write).

        Appends any malformed entries preserved at load time
        (``_unparsed_tag_entries``) so a save — including the seed/back-fill
        save that runs during ``load_tags`` itself — cannot erase bytes a
        hand-edit left in a shape the loader could not validate.
        """
        unparsed = getattr(self, "_unparsed_tag_entries", [])
        self._atomic_write_json(config_dir() / self._TAGS_FILE, [*self._tags, *unparsed])

    def save_tags_snapshot(self, snapshot: list[dict]) -> None:
        """Persist a pre-captured tag snapshot to disk (strict -- raises on failure).

        Used by the serialized tag-write path in chat_tags.py: the snapshot is
        captured on the event loop under the tags write lock, then this write
        runs in a worker thread. Lives here (not in chat_tags.py) so the file
        location resolves through this module's ``config_dir`` exactly like
        ``save_tags`` -- keeping tests that patch it working unchanged.

        Malformed entries preserved at load time (``_unparsed_tag_entries``)
        are appended so this write path preserves them too.

        Raises on I/O failure so callers can roll back in-memory state and
        surface HTTP 5xx rather than silently losing data.
        """
        unparsed = getattr(self, "_unparsed_tag_entries", [])
        self._atomic_write_json_strict(config_dir() / self._TAGS_FILE, [*snapshot, *unparsed])

    def save_tag_boards(self) -> None:
        """Persist sidebar column layout to disk (atomic write).

        Appends any malformed columns preserved at load time
        (``_unparsed_tag_board_entries``) so a save cannot erase bytes a
        hand-edit left in a shape the loader could not validate.
        """
        unparsed = getattr(self, "_unparsed_tag_board_entries", [])
        self._atomic_write_json(
            config_dir() / self._TAG_BOARDS_FILE,
            [*self._tag_boards, *unparsed],
        )

    def save_tag_boards_snapshot(self, snapshot: list[dict]) -> None:
        """Persist a pre-captured boards snapshot to disk (strict -- raises on failure).

        Used by the tag-delete path in chat_tags.py: the snapshot is captured
        on the event loop under the tags write lock, then this write runs in a
        worker thread. Lives here (not in chat_tags.py) so the file location
        resolves through this module's ``config_dir`` exactly like
        ``save_tag_boards`` -- keeping tests that patch it working unchanged.

        Malformed columns preserved at load time
        (``_unparsed_tag_board_entries``) are appended so this write path
        preserves them too.

        Raises on I/O failure so callers can roll back in-memory state and
        surface HTTP 5xx rather than silently losing data.
        """
        unparsed = getattr(self, "_unparsed_tag_board_entries", [])
        self._atomic_write_json_strict(config_dir() / self._TAG_BOARDS_FILE, [*snapshot, *unparsed])

    @staticmethod
    def _partition_preserving(
        raw: list[Any],
        predicate: Callable[[Any], bool],
        noun: str,
        source_file: str,
    ) -> tuple[list[Any], list[Any]]:
        """Split ``raw`` into ``(active, unparsed)`` by ``predicate``.

        The shared "partition malformed rows at load, keep them verbatim so a
        later save round-trips them back" mechanic behind ``load_cron_folders``,
        ``load_chat_pins``, ``load_tags`` and the tag-board load.
        A row is active when ``predicate`` returns truthy; every other row is
        collected into ``unparsed`` so the caller's save path can re-append it
        (``[*active, *unparsed]``) at write time rather than silently erasing
        bytes a hand-edit left in a shape the loader could not validate.

        When any row is preserved, logs the shared preserving-N warning at
        WARNING level. ``noun`` supplies the per-store wording (``"entr(ies)"``,
        ``"chat pin record(s)"``, ``"tag entr(ies)"``, ``"sidebar column(s)"``)
        and ``source_file`` names the file, so the emitted messages stay
        identical to the hand-rolled copies this replaced.
        """
        active: list[Any] = []
        unparsed: list[Any] = []
        for row in raw:
            (active if predicate(row) else unparsed).append(row)
        if unparsed:
            logger.warning(
                "Preserving %d malformed %s while loading %s " "(kept verbatim, not active)",
                len(unparsed),
                noun,
                source_file,
            )
        return active, unparsed

    @staticmethod
    def _atomic_write_json_strict(path: Path, data: Any) -> None:
        """Atomic JSON write that RAISES on failure (no swallowing).

        Used by persistence helpers where the caller needs to know about
        write failures (e.g. to return HTTP 500).

        Delegates to :func:`atomic_write`, which re-raises after cleaning up
        its temp file, so the no-swallowing contract above is unchanged. It
        also carries the Windows ``os.replace`` sharing-violation retry that
        this hand-rolled copy lacked.

        Content stays ``bytes`` rather than ``str`` on purpose: text mode
        applies universal-newline translation, which would rewrite any ``\n``
        inside the JSON on Windows.

        ``mode=0o600`` is explicit because the hand-rolled version created its
        temp with ``tempfile.mkstemp`` and never widened it, so folders.json,
        tags.json, tag_boards.json and cron_folders.json all publish at 0o600
        today. The helper otherwise falls back to the umask default, normally
        0o644, which would widen all four.
        """
        atomic_write(path, json.dumps(data).encode(), fsync=True, mode=0o600)

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        """Atomic JSON write used by folder/tag persistence helpers.

        Delegates to _atomic_write_json_strict but swallows errors (logs a
        warning instead of raising).
        """
        try:
            DashboardState._atomic_write_json_strict(path, data)
        except Exception:
            logger.warning("Failed to write %s", path.name, exc_info=True)

    def source_link_urls(self) -> list[str]:
        """URLs of the sidebar-visible PR/MR chips across all slots.

        Only the links each slot actually serializes (the first
        ``_SERIALIZED_SOURCE_LINKS_PER_SLOT``) are returned — these are the
        chips whose check status the periodic owner-WS refresh keeps fresh.
        Reads the per-slot revision cache, so this is cheap to call on a timer.

        Issue links are excluded: the check-status path reaches ``gh pr view``
        and has no meaning for an issue.

        Returns nothing while the chips are switched off. This is the point of
        gating here rather than only in the payload: the periodic refresh this
        feeds spawns a credentialed provider subprocess per round, and a user who
        turned the chips off should stop paying for status nobody renders.
        """
        from kiro_crew.dashboard.handlers.source_providers import (
            session_card_source_links_enabled,
        )

        if not session_card_source_links_enabled():
            return []
        urls: list[str] = []
        for s in self._slots.values():
            urls.extend(self._slot_chip_urls(s))
        return urls

    @staticmethod
    def _slot_chip_urls(slot: _ChatSlot) -> list[str]:
        """The PR/MR chip URLs one slot serializes (sidebar budget, no issues)."""
        return [
            link["url"]
            for link in _budgeted_source_links(slot._pr_source_links())
            if link.get("kind", "change") == "change"
        ]

    def note_source_activity(self, key: str) -> None:
        """Keep this slot's chips on the fast refresh tier for a while.

        Called when a turn ends on the slot and when the owner opens it. Spends
        no credentials itself: it only decides which tier the owner's own
        periodic driver puts the slot's chips on.
        """
        now = time.monotonic()
        stamps = self._prune_source_activity(now)
        if key in self._slots:
            stamps[key] = now

    def _prune_source_activity(self, now: float) -> dict[str, float]:
        """The activity stamps, minus closed slots and lapsed windows.

        Run on every write as well as every read, so the map never holds more
        than one entry per live slot however its readers are scheduled.
        """
        stamps = getattr(self, "_source_active_at", None)
        if stamps is None:
            stamps = self._source_active_at = {}
        for key in [
            key
            for key, at in stamps.items()
            if key not in self._slots or now - at >= _SOURCE_ACTIVE_WINDOW_SECS
        ]:
            del stamps[key]
        return stamps

    def idle_source_link_urls(self) -> set[str]:
        """Chip URLs that only slots nobody is using link to.

        A slot is in use while a turn runs on it, and for
        ``_SOURCE_ACTIVE_WINDOW_SECS`` after :meth:`note_source_activity`. Every
        other slot is idle -- in particular every tab restored from
        ``open_slots.json`` at boot, until it is opened or runs a turn. The
        periodic driver re-reads these on the slower idle tier, so a long
        sidebar history stops costing a provider burst every chip TTL. A URL
        that any in-use slot also links stays on the fast tier.
        """
        stamps = self._prune_source_activity(time.monotonic())
        active: set[str] = set()
        idle: set[str] = set()
        for key, slot in self._slots.items():
            in_use = key in stamps or bool(getattr(slot, "turn_running", False))
            (active if in_use else idle).update(self._slot_chip_urls(slot))
        return idle - active

    def source_link_urls_for_slot(self, key: str) -> list[str]:
        """Sidebar-visible PR/MR chip URLs for one slot (same cap and kind filter).

        Empty while the chips are switched off, for the same reason
        :meth:`source_link_urls` is: this feeds the turn-boundary status refresh.
        """
        from kiro_crew.dashboard.handlers.source_providers import (
            session_card_source_links_enabled,
        )

        if not session_card_source_links_enabled():
            return []
        slot = self._slots.get(key)
        if slot is None:
            return []
        return self._slot_chip_urls(slot)

    def push_source_status(self, delta: dict) -> None:
        """Push a single PR/MR status delta to owner websockets only.

        Chip status is credential-backed provider data, so this never reaches
        non-owner or app-token clients. Fire-and-forget: the panel's own poll
        remains the safety net if a client misses the event.
        """
        if not self._owner_ws_clients:
            return
        self._send_ws_owners(json.dumps({"type": "source_status", "data": delta}))

    def refresh_slot_source_status(self, key: str) -> None:
        """Re-read this slot's PR/MR status now — called at agent turn boundaries.

        A turn that just ran ``gh pr create``, pushed a revision, or drove a
        review round is exactly when a PR's lifecycle moved, and nothing else in
        the system invalidates the status caches on that event: the chips would
        wait out the periodic rotation and the detail panel would not refetch at
        all. Fires ONLY when an OWNER window is open: the read runs the
        operator's credentials, so a non-owner window must not drive it (a
        non-owner renders the owner-populated caches read-only). With no owner
        window open there is nobody entitled to a credentialed read, so no
        provider subprocess is spawned. Rate-floored inside
        ``request_check_refresh_now``.
        """
        # Both the status read and the visibility revalidation below run the
        # operator's `gh`/`glab` credentials, so this turn-boundary refresh runs
        # ONLY when an OWNER window is open. A non-owner dashboard window never
        # drives it: the owner's own connection keeps the caches warm
        # and non-owner windows render the result read-only via the fail-closed
        # is_repo_public gate. With no owner window open there is nobody entitled
        # to drive a credentialed read, so this is a no-op.
        #
        # The slot just ran a turn, so it is in use whoever is watching: its
        # chips stay on the fast tier of the periodic driver for a while.
        self.note_source_activity(key)
        if not self._owner_ws_clients:
            return
        try:
            urls = self.source_link_urls_for_slot(key)
            if not urls:
                return
            from kiro_crew.dashboard.handlers.source_providers import (
                request_check_refresh_now,
                schedule_visibility_refresh,
            )

            request_check_refresh_now(urls, self.push_slots_update)
            # force=True: the status read above bypasses the TTL, so visibility
            # MUST be revalidated in lockstep — otherwise fresh (now-private)
            # status could be projected against a still-cached-public visibility
            # entry, leaking private status to a non-owner. Runs
            # AFTER the status schedule: both are fire-and-forget schedulers, and
            # ordering the status call first preserves the turn-boundary contract
            # while the downstream render gate (`_project_source_links` ->
            # is_repo_public) is what actually withholds status until visibility
            # reconfirms.
            schedule_visibility_refresh(urls, self.push_slots_update, force=True)
        except Exception:
            logger.debug("turn-boundary source status refresh failed", exc_info=True)

    def _channel_link_is_live(self, link: ChannelLink) -> bool:
        """Is a proactive-capable transport registered for this channel?

        Deliberately an IN-MEMORY check only. This runs per linked slot inside
        ``serialize_slots``, which sits on the ``push_slots_update`` websocket
        broadcast path, so it must not touch the filesystem: the full governed
        ladder (``chat_runner._resolve_channel_target``) calls
        ``governance_permits``, which walks the profile directory (``iterdir`` +
        ``stat``, with a possible reload) — a slow filesystem there would block
        the event loop on every push and can drive watchdog restarts.

        Governance stays enforced at the async SEND boundary (
        ``_resolve_mirror_target`` in the turn path and in the mirror-link
        reminder handler). A link may therefore read ``live: true`` here and
        still be refused at send time; that asymmetry is deliberate and safe —
        the menu affordance is optimistic, the side effect is gated.
        """
        if link.channel_type == SLACK_NAMESPACE or not link.channel_id:
            return False
        transport = self.get_channel_transport(link.channel_type)
        if transport is None:
            return False
        return bool(
            getattr(
                getattr(transport, "capabilities", None),
                "supports_proactive_send",
                False,
            )
        )

    def _slot_links(self, slot: _ChatSlot) -> tuple[list[dict[str, Any]], bool, str, str]:
        """Build the redacted channel-neutral link projection for one slot."""
        # circular import: chat imports state at module scope.
        from kiro_crew.dashboard.chat_utils import (
            effective_session_key,
            mirror_is_paused,
            slack_mirror_is_paused,
        )

        session_key = effective_session_key(slot)
        # Resolved once per slot rather than per row: all three are storage reads,
        # and a session holds at most one Slack thread, one born-in conversation
        # and one mirror binding.
        slack_paused = slack_mirror_is_paused(self, session_key)
        mirror_paused = mirror_is_paused(self, session_key)
        origin_paused = mirror_is_paused(self, session_key, origin=True)
        mirror: ChannelLink | None = None
        persisted_ts: str | None = None
        persisted_channel: str | None = None
        try:
            candidate = self.sessions.get_mirror_link(session_key)
            if isinstance(candidate, ChannelLink):
                mirror = candidate
        except Exception:
            pass
        try:
            raw_ts, raw_channel = self.sessions.get_slack_link(session_key)
            persisted_ts = raw_ts if isinstance(raw_ts, str) else None
            persisted_channel = raw_channel if isinstance(raw_channel, str) else None
        except Exception:
            pass

        # Prefer persisted values, but retain explicit in-memory Slack links in
        # tests and during the short interval before persistence is observable.
        slack_ts = persisted_ts or slot._slack_thread_ts
        slack_channel = persisted_channel or slot._slack_channel
        namespaced_origin = split_namespaced_channel_id(persisted_channel)
        genuine_slack = _is_genuine_slack_link(slack_ts, slack_channel)
        # A Slack-BORN session's ``slack_thread_ts`` names the thread it LIVES
        # in, not a mirror target somewhere else: the Slack inbound handler
        # writes it every turn as the thread registry that routes replies back.
        # That makes it a self-reference, and the sidebar already draws an origin
        # glyph from the slot key -- so surfacing it as an outbound mirror badges
        # one conversation twice and offers a session its own origin thread as a
        # releasable mirror. A Slack-born session that genuinely mirrors to a
        # DIFFERENT thread still carries a different ts, so it is unaffected.
        slack_origin_self_link = (
            channel_namespace_of(session_key) == SLACK_NAMESPACE
            and bool(slack_ts)
            and session_key.endswith(slack_ts)
        )
        links: list[dict[str, Any]] = []
        # The per-binding nonces, read the way the bindings themselves are, so
        # the row's token and the map's compare-and-clear digest the same
        # material. Only a string counts: a session double that predates the
        # accessors (or answers them with a mock) reads as no nonce.
        mirror_nonce = _mirror_link_nonce(self, session_key)
        slack_nonce = _slack_link_nonce(self, session_key)

        def append_link(
            link: ChannelLink, direction: str, nonce: str = "", *, drives_session: bool
        ) -> None:
            """Append one row. *drives_session*: messages sent there land in THIS session.

            The inbound-routing fact is the server's to state, per row, because
            it is not readable from the row's other fields: a Slack thread is
            marked ``out`` (its inbound routing is Slack's own thread index, not
            the mirror's inbound marker) yet a reply in it resumes this session;
            a ``both`` mirror routes inbound by that marker; an ``out`` mirror
            only receives replies; and the conversation a session was born in
            is where its turns come from. Judged client-side from ``direction``
            plus the channel name, a paused Slack row reads as a one-way link --
            so the client reads this bit and special-cases nothing.
            """
            channel_type = (link.channel_type or "").lower()
            if not channel_type:
                return
            channel_id = link.channel_id or ""
            nested = split_namespaced_channel_id(channel_id)
            if nested and nested[0] == channel_type:
                channel_id = nested[1]
            normalized = ChannelLink(channel_type, channel_id, link.thread_id)
            # Real on EVERY row, origin included: the conversation a session was
            # born in can be disconnected too, so it stops syndicating there and
            # the session carries on in the dashboard. `direction` still records
            # the provenance the sidebar mark needs; it does not decide whether
            # the row has a control.
            #
            # Keyed to the row's SOURCE, not just its channel: a session born in
            # Discord that also mirrors to Telegram draws two non-Slack rows, and
            # if both read one value, muting either silently mutes the other.
            if channel_type == SLACK_NAMESPACE:
                paused = slack_paused
            elif direction == "origin":
                paused = origin_paused
            else:
                paused = mirror_paused
            links.append(
                {
                    "channel": channel_type,
                    "label": _link_label(channel_type),
                    "target": _redacted_link_target(channel_id),
                    # The row's identity for an unlink: the redacted `target`
                    # above is display only and drops the thread, so a Slack
                    # thread and its same-channel replacement would read alike;
                    # the binding's own nonce keeps a same-target replacement
                    # from reading alike too.
                    "binding": binding_token(normalized, nonce),
                    "direction": direction,
                    "drives_session": drives_session,
                    "live": self._channel_link_is_live(normalized),
                    "paused": paused,
                }
            )

        # Non-Slack transports currently leak their home conversation through
        # slack_channel_id. Surface that as a read-only origin, never a Slack
        # mirror. This prefix sniff is intentionally defensive for unknown
        # future channel types too.
        if namespaced_origin and namespaced_origin[0] != SLACK_NAMESPACE:
            # The conversation the session was born in: the channel dispatcher
            # routes its messages here on every inbound turn.
            append_link(
                ChannelLink(namespaced_origin[0], namespaced_origin[1]),
                "origin",
                drives_session=True,
            )

        if mirror is not None:
            if mirror.channel_type == SLACK_NAMESPACE:
                # get_mirror_link synthesizes Slack for the legacy fields. If
                # those fields actually hold a namespaced non-Slack origin, the
                # origin above is the only truthful representation.
                if not namespaced_origin and genuine_slack and not slack_origin_self_link:
                    append_link(
                        ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                        "out",
                        slack_nonce,
                        drives_session=True,
                    )
            else:
                # A resume binding (set by an in-channel `!sessions` pick) routes
                # BOTH ways: this session's replies go to that channel AND
                # messages from it are delivered back here. That is a materially
                # different thing for the user to see and release than an
                # outbound-only `!link` mirror, so it gets its own direction
                # rather than being flattened into "out". Slack is excluded by
                # the branch above — it carries inbound on its own thread index
                # and never sets the marker.
                inbound = False
                try:
                    inbound = bool(self.sessions.mirror_accepts_inbound(session_key))
                except Exception:
                    # Older/stubbed SessionManagers may not expose the accessor;
                    # degrade to the outbound reading rather than dropping the link.
                    inbound = False
                append_link(
                    mirror, "both" if inbound else "out", mirror_nonce, drives_session=inbound
                )
        elif genuine_slack and not slack_origin_self_link:
            # Defensive fallback for SessionManager test doubles or older
            # implementations that expose get_slack_link but not get_mirror_link.
            append_link(
                ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                "out",
                slack_nonce,
                drives_session=True,
            )

        if genuine_slack and slack_origin_self_link:
            # The conversation a session was BORN in gets a row too. Suppressing
            # it was the last place a channel appeared with no control at all:
            # you can stop a Slack-born session syndicating to its thread and
            # carry on in the dashboard, and a human reply in that thread brings
            # it back. It stays `origin` so the sidebar keeps showing where the
            # conversation came from — provenance is history and survives a
            # disconnect; only the delivery indicator reflects the mute.
            append_link(
                ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                "origin",
                slack_nonce,
                drives_session=True,
            )

        if genuine_slack and not slack_origin_self_link:
            slack_namespace = split_namespaced_channel_id(slack_channel)
            visible_slack_channel = slack_namespace[1] if slack_namespace else (slack_channel or "")
            # A Slack ROW accompanies `slack_linked=True` unconditionally. The
            # dashboard's channel control is built from `links` alone — it no
            # longer synthesizes a Slack row from this boolean, because a
            # synthesized row cannot know `paused` and so rendered a muted thread
            # as connected. That makes a True here with no row worse than a
            # cosmetic gap: the session IS linked and the menu would offer to
            # connect it. Guaranteed here rather than left to hold incidentally
            # across the branches above.
            if not any(
                row["channel"] == SLACK_NAMESPACE and row["direction"] != "origin" for row in links
            ):
                append_link(
                    ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                    "out",
                    slack_nonce,
                    drives_session=True,
                )
            return links, True, visible_slack_channel, slack_ts or ""
        return links, False, "", ""

    def serialize_slot(
        self,
        slot: _ChatSlot,
        *,
        include_check_status: bool = False,
        dashboard_user: bool = False,
    ) -> dict[str, Any]:
        """Serialize one slot with state-backed channel-link metadata."""
        payload = slot.to_dict(
            include_check_status=include_check_status, dashboard_user=dashboard_user
        )
        links, slack_linked, slack_channel, slack_thread_ts = self._slot_links(slot)
        payload.update(
            {
                "pin_rank": self.pin_rank(slot),
                "pin_rev": self._pinned_order_rev,
                "links": links,
                "slack_linked": slack_linked,
                "slack_channel": slack_channel,
                "slack_thread_ts": slack_thread_ts,
            }
        )
        return payload

    def serialize_slots(
        self, *, include_check_status: bool = False, dashboard_user: bool = False
    ) -> list:
        """Serialize slots, optionally including owner-only provider status.

        ``subagents_running`` and ``subagents_queued`` remain available to every
        authenticated caller.
        Credential-backed ``ci`` and ``state`` fields are omitted unless an
        authenticated owner boundary explicitly opts in — EXCEPT a link whose
        repository is known public, which any authenticated dashboard user
        (``dashboard_user=True``) may see because that lifecycle is already
        world-visible. Private/unknown repos and app tokens stay owner-only.
        """
        out = []
        subs = getattr(self, "subagents", None)
        # A slot that is registered but still under construction is not yet a
        # session: its transcript is mid-hydration and, for an import, its Layer B
        # join is not written. Omit it from the payload so the creation-time
        # broadcast never advertises a tab that resolves to nothing (a click would
        # cold-start a fresh context the pending join can never attach to). It
        # stays REGISTERED in ``_slots`` throughout, so a concurrent same-key
        # resume still resolves it and the idempotency guard holds; it simply is
        # not shown until the builder ends construction and pushes. Belt-and-
        # suspenders on ``__new__``-built states that never ran __init__:
        # treat a missing set as empty rather than AttributeError-ing this hot
        # path.
        # circular import: chat_utils imports this module at load time.
        from kiro_crew.dashboard.chat_utils import effective_session_key

        under_construction = getattr(self, "_slots_under_construction", None) or ()
        queued_by_slot = _published_queued_by_slot(subs)
        for s in self._slots.values():
            if s.key in under_construction:
                continue
            self._drop_orphaned_mcp_report(s)
            d = self.serialize_slot(
                s,
                include_check_status=include_check_status,
                dashboard_user=dashboard_user,
            )
            d["subagents_running"] = bool(
                subs and subs.running_agents_for(effective_session_key(s))
            )
            d["subagents_queued"] = queued_by_slot.get(s.key, 0)
            out.append(d)
        # The slot-key/session-key correspondence the lineage join needs, read the same
        # way ``/api/sessions/memory`` reads it for the Sessions table. Handed over
        # UNCALLED: resolving it is pure dict work over the live registry, but it belongs
        # inside that function's failure boundary, because a fault in it is a fault in
        # the nesting and must cost the nesting rather than the whole sidebar.
        # ``getattr`` because a stub state in the suite may not carry the method at all.
        _attach_slot_parents(out, getattr(self, "spend_slot_by_session", None))
        return out

    def serialize_slot_views(
        self, *, owner: bool
    ) -> tuple[list[dict], list[dict], list[dict] | None]:
        """One serialization pass, three audience views of the slot list.

        Returns ``(bare, dashboard_user, owner)``; ``owner`` is ``None`` when
        ``owner`` is False (no owner socket to build it for).

        The three views the broadcast ships differ ONLY in each slot's
        ``source_links`` field -- the audience gates are read nowhere else in the
        summary (see ``_ChatSlot.source_links_view``). Serializing the list three
        times therefore ran the per-slot projection body (last-message markdown
        strip, credential redaction, options parse) and the source-link
        budgeting three times for identical output, synchronously on the
        event loop: measured at ~200 ms per pass on a sidebar of ~80 tabs, so a
        broadcast stalled the loop for ~600 ms. Those stalls read as host pressure
        to the adaptive concurrency controller (``loop_lag >= 250 ms`` is a
        sufficient-alone decrease signal) and pinned the subagent cap at its floor
        on an idle machine. Serialize once, then re-project only the one field.

        The derived dicts are shallow copies: every other value is shared with
        the bare list. Nothing mutates a slot payload after serialization (the
        bare list is ``json.dumps``-ed for SSE and the WS frames are built from
        the lists as-is), so sharing is safe and avoids a deep copy per audience.
        A payload whose key has no live slot (a patched ``serialize_slots`` in
        tests, or a slot closed between the two loops) is carried over unchanged.
        """
        bare = self.serialize_slots()
        return (
            bare,
            self._reproject_slots(bare, dashboard_user=True),
            self._reproject_slots(bare, include_check_status=True) if owner else None,
        )

    def _reproject_slots(
        self,
        slots_data: list[dict],
        *,
        include_check_status: bool = False,
        dashboard_user: bool = False,
    ) -> list[dict]:
        out: list[dict] = []
        for payload in slots_data:
            slot = self._slots.get(payload.get("key", ""))
            if slot is None or "source_links" not in payload:
                out.append(payload)
                continue
            view = dict(payload)
            view["source_links"] = slot.source_links_view(
                include_check_status=include_check_status, dashboard_user=dashboard_user
            )
            out.append(view)
        return out

    def _drop_orphaned_mcp_report(self, slot: "_ChatSlot") -> None:
        """Drop a slot's MCP report unless it describes the slot's CURRENT session.

        A report describes exactly ONE session. Clearing it at each teardown was
        the wrong shape — review found path after path that skipped it (the reset
        funnel, the reload and reset-conversation routes, the queued discard, a
        channel handler, the cron reaper, the task runner, a project change) — and
        a LIVENESS check ("does a session exist?") still missed the last of them,
        because a reset RECREATES a session under the same key: the slot looked
        alive while the report described the session that had gone.

        So the question asked here is identity, not liveness — is the live session
        the one this payload was taken under? A mismatch is dropped by
        construction, which closes every one of those paths at once, including any
        future one, and demotes the remaining ``clear_mcp_report()`` calls to a
        courtesy that pushes the delta early rather than the thing correctness
        rests on.

        Validating HERE cannot be bypassed: this is the single projector both
        ``/api/chat/slots`` and the WebSocket snapshot go through.

        The key derivation mirrors ``chat_utils.effective_session_key`` — a
        channel-born slot's turns run on the channel's own session — and is
        inlined because ``chat_utils`` imports this module. One refinement over
        that mirror: an in-flight turn's OWN key wins over the slot's routing.
        The two diverge when the routing is reassigned on a live slot — a cron
        injection binds ``linked_session_key`` to ``cron:<id>`` with no
        ``running`` gate — and the report describes the session the turn is
        actually running on, not the one a FUTURE turn would route to.
        Resolving the routing there reads a different (or absent) provider,
        mismatches, and clears a live session's report mid-turn. Same rule as
        turn cancellation: address the turn, not the routing.
        """
        if slot.mcp_report_payload() is None:
            return
        session_key = (
            getattr(slot, "_active_turn_session_key", "")
            or getattr(slot, "linked_session_key", "")
            or f"dashboard:{slot.key}"
        )
        provider = self.sessions.get_provider(session_key)
        live_id = getattr(provider, "session_id", "") if provider is not None else ""
        if live_id != slot._mcp_report_session_id:
            slot.clear_mcp_report()

    @contextlib.contextmanager
    def suspend_slots_push(self) -> "Iterator[Callable[[str], None]]":
        """Coalesce every ``push_slots_update()`` inside the block into one at exit.

        ``get_or_create_slot`` broadcasts the FULL slot list on each call, so a bulk
        restore of N tabs serializes 1+2+…+N slots — O(N²) ``to_dict``/redaction
        work for intermediate states no client will ever render (measured ~1.3s at
        N=77, and it grows quadratically). Wrap the restore, emit one broadcast.

        The yielded callback accepts only an operation label and lets a successful
        request move the coalesced flush by the state-owned fixed delay past its
        response. Existing callers ignore it and retain the synchronous flush. A
        deferred callback that fires during an active suspension transfers its
        publication debt to that suspension rather than publishing inside it. If
        suspensions overlap before the debt reaches depth zero, the outermost exit
        publishes synchronously: one request cannot delay another context's
        publication contract.

        Depth-counted so nested use is safe (an inner block must not flush early),
        and ``@contextmanager``'s try/finally unwinds the depth even if the body
        raises. Only flushes if something actually asked to push. A flush that
        itself fails while the body's own exception is unwinding annotates its
        exception (`PEP 678`) so the buried original stays visible — the flush's
        exception otherwise replaces the body's in the caller's view. A deferred
        flush is honored only after a clean body exit; every exception keeps the
        old synchronous failure propagation.
        """
        deferred_operation: str | None = None

        def defer_flush(operation: str = "slots update") -> None:
            nonlocal deferred_operation
            deferred_operation = operation

        self._slots_push_suspend += 1
        if self._slots_push_suspend > 1:
            self._slots_push_overlapped = True
        try:
            yield defer_flush
        finally:
            self._slots_push_suspend -= 1
            overlapped = self._slots_push_overlapped
            if self._slots_push_suspend == 0:
                self._slots_push_overlapped = False
            if self._slots_push_suspend == 0 and self._slots_push_pending:
                self._slots_push_pending = False
                # Captured BEFORE the flush call: inside the `except` block below,
                # sys.exc_info() would already name the flush's own exception.
                unwinding_over = sys.exc_info()[1]
                if deferred_operation is not None and unwinding_over is None and not overlapped:
                    loop = self.serving_loop
                    if loop is not None and loop is self._running_loop():
                        try:
                            loop.call_later(
                                _DEFERRED_SLOTS_FLUSH_DELAY_S,
                                self._deferred_slots_flush,
                                deferred_operation,
                                1,
                            )
                            lock = self._slots_broadcast_lock
                            if lock is not None:
                                with lock:
                                    if self._slots_broadcast_timer is not None:
                                        self._slots_broadcast_timer.cancel()
                                        self._slots_broadcast_timer = None
                            return
                        except RuntimeError:
                            # A loop closing between lookup and scheduling cannot
                            # drop the announcement. Fall back to the old immediate
                            # flush and let its failure retain caller visibility.
                            pass
                try:
                    self.push_slots_update()
                except BaseException as flush_exc:
                    if unwinding_over is not None and unwinding_over is not flush_exc:
                        flush_exc.add_note(
                            "[slots-flush] the owed slots flush raised while unwinding "
                            f"over an in-flight {type(unwinding_over).__name__}; that "
                            "original exception is chained below as __context__"
                        )
                    raise

    def _deferred_slots_flush(self, operation: str, retries_remaining: int) -> None:
        """Publish one fresh snapshot, retrying a deferred failure once.

        This deliberately bypasses the leading/trailing coalescer at depth zero:
        returning from ``push_slots_update`` can mean that a bare trailing callback
        owns the real serialization, which would put its exception back under
        asyncio's generic callback handler. During an active suspension, however,
        the outermost context owns the safe flush, so transfer the publication debt
        to it through ``push_slots_update``. Before each direct attempt, an armed
        trailing callback is canceled under the coalescing lock and the new window
        is stamped; a failed attempt therefore leaves only its bounded retry armed.
        Each attempt re-serializes current state, so the retry reconciles every
        mutation that landed in the meantime.
        """
        if self._slots_push_suspend:
            self.push_slots_update()
            return

        try:
            lock = self._slots_broadcast_lock
            if lock is not None:
                with lock:
                    if self._slots_broadcast_timer is not None:
                        self._slots_broadcast_timer.cancel()
                        self._slots_broadcast_timer = None
                    self._slots_broadcast_last = _slots_broadcast_clock()
            self._do_slots_broadcast()
            return
        except Exception:
            logger.error(
                "Deferred slots publication failed for %s; retries remaining=%d",
                operation,
                retries_remaining,
                exc_info=True,
            )

        if retries_remaining <= 0:
            return
        loop = self.serving_loop
        if loop is None or loop is not self._running_loop():
            logger.error(
                "Deferred slots publication retry could not be scheduled for %s: "
                "serving loop is unavailable",
                operation,
            )
            return
        try:
            loop.call_later(
                _SLOTS_BROADCAST_INTERVAL_S,
                self._deferred_slots_flush,
                operation,
                retries_remaining - 1,
            )
        except RuntimeError:
            logger.error(
                "Deferred slots publication retry could not be scheduled for %s: "
                "serving loop is closing",
                operation,
                exc_info=True,
            )

    def push_slots_update(self, *, legacy_only: bool = False) -> None:
        """Push slots, keeping provider status confined to owner websockets.

        Coalesces on a leading plus trailing edge: the first call after an idle
        period broadcasts immediately, further calls inside the window are
        absorbed, and one trailing broadcast carries the final state. A single
        chat turn fires several of these and each one re-serializes every slot,
        so an uncoalesced burst redraws the whole sidebar once per mutation for
        what the user sees as one change. The trailing flush re-serializes at
        delivery time, so a coalesced frame is never a stale frame.

        The window is 200 ms for a small list and stretches with the size of the
        last frame (see ``_SLOTS_BROADCAST_BYTES_PER_S``), so a busy fleet of a
        hundred agents is paced at roughly a fixed byte rate per socket instead of
        five full lists a second.

        ``legacy_only`` owes the full list only to consumers that cannot apply a
        ``slot_patch`` frame (SSE readers, app tokens, a tab whose bundle
        predates the frame). :meth:`push_slot_patch` and
        :meth:`push_slot_removed` use it: the patch already reached every
        socket that declared the capability, so those sockets skip this
        broadcast. An ordinary call absorbed into the same window widens the
        broadcast back to everyone.
        """
        lock = self._slots_broadcast_lock
        if lock is not None:
            with lock:
                if legacy_only:
                    self._slots_push_legacy_owed = True
                else:
                    self._slots_push_all_owed = True
        if self._slots_push_suspend:
            # Inside suspend_slots_push(); remember that a push is owed and let the
            # outermost block emit a single coalesced broadcast on exit.
            self._slots_push_pending = True
            return

        now = _slots_broadcast_clock()
        broadcast_now = False
        if lock is None:
            # Partially-constructed state (built via __new__): no coalescing.
            self._do_slots_broadcast()
            return

        with lock:
            # Resolved once here, at the top of the lock, so the timer branch
            # below and any later cross-thread caller agree on one loop.
            serving = self.serving_loop
            interval = self._slots_broadcast_interval

            elapsed = now - self._slots_broadcast_last
            if elapsed >= interval:
                self._slots_broadcast_last = now
                if self._slots_broadcast_timer is not None:
                    self._slots_broadcast_timer.cancel()
                    self._slots_broadcast_timer = None
                broadcast_now = True
            elif self._slots_broadcast_timer is None:
                # Scheduling onto the serving loop is preferred over broadcasting
                # from a foreign thread; a closed loop falls back to an immediate send.
                loop = serving
                remaining = interval - elapsed
                try:
                    if loop is None:
                        self._slots_broadcast_last = now
                        broadcast_now = True
                    elif loop is self._running_loop():
                        self._slots_broadcast_timer = loop.call_later(
                            remaining, self._trailing_slots_flush
                        )
                    else:
                        loop.call_soon_threadsafe(self._schedule_trailing_flush, remaining)
                except RuntimeError:
                    self._slots_broadcast_last = now
                    broadcast_now = True

        # serialize_slots() and _broadcast() are the expensive half; running them
        # outside the lock stops a cross-thread caller from blocking behind them.
        if broadcast_now:
            self._do_slots_broadcast()

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        """Return the running loop, or None when called off the event loop."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def bind_serving_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the loop this dashboard is served on, before any request runs.

        Called from an app startup hook: that is the earliest point the loop
        exists, so every later reader finds it already bound instead of racing to
        latch a copy from whichever thread happens to arrive first.

        Deliberately does NOT seed the session-lineage projection. Seeding is bound to
        one store and re-runs whenever the data home changes, so a process that binds
        several states over several homes -- a test run, a pod host -- would queue one
        full cold scan per bind onto the shared maintenance pool and starve everything
        else waiting on it. The seed is requested lazily instead, by the first slots
        frame that finds the projection cold (see :func:`_attach_slot_parents`).
        """
        self._serving_loop = loop

    @property
    def serving_loop(self) -> "asyncio.AbstractEventLoop | None":
        """The loop to hand cross-thread work to, or None when it is unknowable.

        Prefers the loop bound at startup. When nothing bound one -- a
        ``__new__``-built state, a unit test, a process whose startup hook has not
        run -- it latches the running loop the first time it is read FROM that
        loop, so an off-loop caller arriving later still has a target. ``None``
        means this state has never seen a loop, and the caller owns the decision
        about what to do with the work rather than being handed a guess.
        """
        loop = self._serving_loop
        if loop is None:
            loop = self._running_loop()
            if loop is not None:
                self._serving_loop = loop
        return loop

    def _schedule_trailing_flush(self, delay: float) -> None:
        """Arm the trailing flush. Must run ON the event loop."""
        lock = self._slots_broadcast_lock
        if lock is None:
            return
        with lock:
            if self._slots_broadcast_timer is not None:
                return
            self._slots_broadcast_timer = asyncio.get_running_loop().call_later(
                delay, self._trailing_slots_flush
            )

    def _trailing_slots_flush(self) -> None:
        """Trailing-edge callback: broadcast whatever the state is now."""
        lock = self._slots_broadcast_lock
        if lock is not None:
            with lock:
                self._slots_broadcast_timer = None
                self._slots_broadcast_last = _slots_broadcast_clock()
        self._do_slots_broadcast()

    def _take_slots_audience(self) -> bool:
        """Consume the owed-audience flags; True when only legacy consumers are owed."""
        lock = self._slots_broadcast_lock
        if lock is None:
            return False
        with lock:
            legacy_only = self._slots_push_legacy_owed and not self._slots_push_all_owed
            self._slots_push_legacy_owed = False
            self._slots_push_all_owed = False
        return legacy_only

    def _has_legacy_slots_audience(self) -> bool:
        """True when some consumer can only learn slot changes from a full list.

        That is every SSE reader, and every open socket that did not declare
        the ``slot_patch`` capability at connect: an app token, a companion
        window, or a tab still running a bundle from before the frame existed.
        App sockets count even when their scope would filter the list to
        nothing, because asking the scope gate here would audit a denial per
        socket per metadata edit; the cost is one serialization that the
        pre-patch protocol paid on every edit anyway.
        """
        if getattr(self, "_sse_queues", None):
            return True
        return any(
            not ws.closed and not ws.get(SLOT_PATCH_WS_FLAG, False)
            for ws in list(getattr(self, "_ws_clients", None) or ())
        )

    def _do_slots_broadcast(self) -> None:
        """Serialize and broadcast the slot list. Bypasses coalescing."""
        from kiro_crew.dashboard.handlers.source_providers import (
            gitlab_hosts_generation,
        )
        from kiro_crew.platform.governance_profiles import (
            governance_answer_generation,
        )

        legacy_only = self._take_slots_audience()
        if legacy_only and not self._has_legacy_slots_audience():
            # Every consumer already applied the patch this broadcast was owed
            # for, so serializing the whole list would reach nobody.
            self._emit_member_slot_transitions()
            return

        yolo_active = self.is_yolo_active()  # expire first if needed
        # PUBLIC-repo chip status rides the general frame so any authenticated
        # dashboard user (SSE and WS both run on dashboard-user tokens) sees the
        # merged/closed/CI glyph for a repo whose lifecycle is already
        # world-visible. Private/unknown repos fall through to owner-only inside
        # serialize_slots, and app tokens are stripped of this status in
        # ``_serialize_for_client`` -> ``filter_slots_for_app`` before delivery,
        # so widening the general list here never leaks status to an app scope.
        # SSE carries the BARE list (see below); the WS dashboard-user frame
        # carries the enriched one. The general ``_slots_list`` feeds BOTH the SSE
        # queue (which has NO per-app filtering) and the WS frame, so enriching it
        # here leaks public-repo status onto ``/api/stream`` for any app token
        # allowed that route. Keep the broadcast list bare and put
        # the public-repo enrichment only on the WS path, where
        # ``_serialize_for_client`` re-filters app tokens.
        #
        # One serialization pass for all three audiences -- see
        # ``serialize_slot_views`` for why three passes stalled the event loop.
        owner_ws_clients = getattr(self, "_owner_ws_clients", None)
        if legacy_only and owner_ws_clients:
            owner_ws_clients = {
                ws for ws in owner_ws_clients if not ws.get(SLOT_PATCH_WS_FLAG, False)
            }
        slots_data, slots_data_ws, owner_slots = self.serialize_slot_views(
            owner=bool(owner_ws_clients)
        )
        # The evidenced way this broadcast fails is a non-serializable value in
        # slot state: the dump raises, and the bare TypeError
        # names neither the slot nor the field. Serialize up front and annotate
        # the failure with the offender so one traceback is enough to find it.
        # Both coalescing branches (leading edge and trailing timer) funnel
        # through this method, so both report identically by construction.
        # Diagnosis only: the exception still propagates unchanged.
        try:
            slots_json = json.dumps(slots_data)
        except (TypeError, ValueError) as exc:
            exc.add_note(_slots_serialization_note(slots_data))
            raise
        # Size the NEXT coalescing window from this frame, so a burst over a long
        # list is paced by bytes rather than by the fixed floor. The generic list
        # stands in for every audience's copy; they differ only in per-slot
        # enrichment. Character count, not encoded bytes: close enough for pacing.
        lock = self._slots_broadcast_lock
        if lock is not None:
            with lock:
                self._slots_broadcast_interval = _slots_broadcast_interval_for(len(slots_json))
        mgr = getattr(self, "channel_manager", None)
        ch_trusted = bool(mgr and any(ch.trusted for ch in mgr._channels.values()))
        # ONE read, shared by the generic and owner frames below. Two independent
        # reads could straddle a ceiling install or a profile reload and ship two
        # different tokens for one broadcast, which would make one of the two
        # audiences invalidate while the other did not.
        # test_public_repo_status_rides_general_frame_owner_gets_full asserts the two
        # frames' governanceGeneration values are equal, so a torn read reddens it.
        # Filesystem-free by contract: governance_answer_generation is two locked
        # integer reads. The profiles directory re-stat lives in poll_profiles_fresh,
        # which only the async watcher calls, and only off the event loop.
        answer_generation = governance_answer_generation()
        # Piggyback the allowlist generation so clients invalidate the cached
        # ['dashboardConfig'] query only when the GitLab-hosts allowlist actually
        # changed -- an event-driven refresh that replaces a constant 30s poll
        # (which multiplied audit-log writes across every same-key observer).
        #
        # Piggyback the folder tree (the in-memory ``_folders`` list, WITHOUT the
        # per-folder ``history_count`` that ``GET /api/chat/folders`` computes via
        # a synchronous session scan) so the sidebar can group sessions correctly
        # on the FIRST paint. Sessions arrive on this WS frame the instant the
        # socket connects; folders otherwise arrive only via a separate HTTP GET,
        # so the sidebar would render every session ungrouped (Unfiled bucket)
        # until that GET resolved, then visibly re-shuffle them into folders. The
        # HTTP query still runs to backfill ``history_count``; grouping no longer
        # waits on it. Slicing to the fields the client's grouping needs keeps
        # this hot-path frame small and never touches the filesystem.
        self._broadcast(
            {
                "_type": "slots",
                # BARE list for SSE and the generic-envelope fields below.
                "_slots_list": slots_data,
                # Enriched (public-repo status) list for the WS dashboard-user
                # frame ONLY. ``_broadcast`` builds the WS frame from this when
                # present; SSE never reads it, so status cannot reach the
                # unfiltered stream.
                "_slots_list_ws": slots_data_ws,
                "_yolo": yolo_active,
                "slots": slots_json,
                "channelTrusted": ch_trusted,
                "gitlabHostsGeneration": gitlab_hosts_generation(),
                # getattr, not self._folders: this read path runs on EVERY slots
                # push, including on a __new__-built DashboardState that seeded only
                # the push essentials and never ran __init__ (several endpoint
                # suites build their fixture that way). _folders is an __init__-only
                # assignment, so a bare attribute access would AttributeError there
                # — the exact break test_push_slots_update_survives_a_partially_
                # constructed_state pins against. An absent/None folder store is an
                # empty tree.
                #
                # Lightweight states can bypass load_folders(), so keep this hot
                # broadcast path tolerant of malformed in-memory values.
                "folders": _safe_folder_tree(getattr(self, "_folders", None)),
                # Lets the client distinguish "the tree changed" from "a session
                # blinked": this frame fires on routine slot activity, so the
                # tree alone is not a change signal.
                "foldersGeneration": self.folders_generation(),
                "governanceGeneration": answer_generation,
                # Read by ``_broadcast`` to skip sockets that already applied
                # the ``slot_patch`` this broadcast was owed for.
                "_legacy_only": legacy_only,
            }
        )
        # The owner frame is the owner's ONLY slots frame — `_send_ws_all` skips
        # owner sockets for `slots` (see there for why), so this envelope has to
        # carry every key the generic one does: `folders` seeds the first-paint
        # folder tree and `gitlabHostsGeneration` drives the dashboard-config
        # invalidation, so a subset here leaves the owner without them. Both frames
        # are built by `_slots_ws_frame`, so a key cannot reach one and not the
        # other.
        if owner_ws_clients and owner_slots is not None:
            self._send_ws_owners(
                _slots_ws_frame(
                    owner_slots,
                    yolo=yolo_active,
                    channel_trusted=ch_trusted,
                    gitlab_hosts_gen=gitlab_hosts_generation(),
                    folders=_safe_folder_tree(getattr(self, "_folders", None)),
                    folders_gen=self.folders_generation(),
                    governance_gen=answer_generation,
                ),
                **({"skip_slot_patch_clients": True} if legacy_only else {}),
            )

        self._emit_member_slot_transitions()

    def _emit_member_slot_transitions(self) -> None:
        """Log slot/opened and slot/closed for member-driven slots.

        Runs after every slots broadcast and after :meth:`push_slot_removed`,
        which is how a close reaches the log when no consumer needed the full
        list. It diffs against the last-seen set, so a second call for the same
        registry state emits nothing.
        """
        # Best-effort per-member event log: emit slot/opened and slot/closed
        # for slots DRIVEN by a member (created_by is a member NAME), diffed
        # against the last-seen set on this state object. Additive; never
        # affects the broadcast above.
        try:
            from kiro_crew import eventlog_hooks

            # The known-agent check must not read config.json here: this runs on
            # the gateway serving loop, so it reads the off-loop alias snapshot
            # (refreshed by every successful config load) instead of stat/read/
            # parsing config synchronously and stalling every task.
            from kiro_crew.eventlog.types import SLOT_CLOSED, SLOT_OPENED
            from kiro_crew.members import member_slug, slug_from_dm_slot_key

            # `_created_by` is `session_control`'s attribution and it holds the
            # creator's SLOT KEY, never an agent name -- so comparing it against the
            # alias snapshot could not match for ANY member-created slot, and every
            # member slot event was dropped on the normal path.
            #
            # A member drives two kinds of slot and both are resolved, because
            # covering only one leaves the other silently dropped:
            #   (a) its pinned DM slot, keyed `member-<slug>`, from which the slug
            #       is pure string work -- `slug_from_dm_slot_key` is the members
            #       module's single spelling of that strip, including the
            #       `.memory-<store>` suffix a reader must drop;
            #   (b) an ordinary chat slot bound to the member's private store, whose
            #       owner lives in the CONFIG. That read is deferred to the worker
            #       below for the same reason the log id already is: this function
            #       runs on the gateway serving loop and must not parse config here.
            # So the loop collects identity MATERIAL and the worker resolves it.
            current: dict[str, tuple[str, str]] = {}
            for _sk, _slot in list(self._slots.items()):
                _cb = getattr(_slot, "_created_by", "")
                if not _cb:
                    continue
                _slug = slug_from_dm_slot_key(_cb)
                if _slug:
                    current[_sk] = (_slug, "")
                    continue
                _creator = self._slots.get(_cb)
                _store = getattr(_creator, "memory_store", "") if _creator else ""
                if _store and _store != "default":
                    current[_sk] = ("", _store)
            prev = self._member_driven_slots_seen
            if self._member_slots_unconfirmed:
                # An append the worker could not complete is not recorded, whatever
                # the checkpoint says. Applying the corrections here is what makes
                # the comparison below recompute exactly those transitions and
                # nothing else. Drained rather than read, so a retry that fails
                # again queues a fresh correction instead of looping on a stale one.
                # Drained IN PLACE, and by repeated POP rather than copy-then-clear.
                # The worker closure captures this dictionary by reference when it is
                # created, so rebinding the attribute to a fresh one would leave an
                # in-flight worker writing its failures into an object nothing reads.
                # And a copy followed by clear() leaves a window: a failure the worker
                # records between the two is wiped without ever being applied, so the
                # comparison below recomputes nothing for it and the checkpoint then
                # advances past that transition, dropping it for good. Each pop either
                # returns an entry, which is therefore applied, or finds none and
                # leaves later ones for the next pass -- no entry is discarded unread.
                _lost: dict[str, tuple[str, str] | None] = {}
                while True:
                    try:
                        _lkey, _lval = self._member_slots_unconfirmed.popitem()
                    except KeyError:
                        break
                    _lost[_lkey] = _lval
                prev = dict(prev)
                for _lk, _lwho in _lost.items():
                    if _lwho is None:
                        prev.pop(_lk, None)
                    else:
                        prev[_lk] = _lwho
                self._member_driven_slots_seen = prev
            # Drained IN PLACE and BEFORE the comparison, for two reasons. In place,
            # because an in-flight worker holds this list by reference exactly as it
            # holds the map. Before, because a retry must not depend on the slots
            # having changed: a slot whose open failed and which is still open makes
            # `current == prev`, so anything inside the comparison below would never
            # run and the open would wait for an unrelated slot to move.
            _retry: list[tuple[str, tuple[str, str], str, dict]] = []
            while self._member_slots_retry:
                _retry.append(self._member_slots_retry.pop(0))
            if current != prev or _retry:
                # emit fsyncs, so collect the (member, type, data) tuples and
                # offload the writes: _do_slots_broadcast runs on the gateway
                # loop and a synchronous durability barrier per slot transition
                # would stall every concurrent session. The member's LOG ID is
                # resolved inside that worker, not here: a member may carry an
                # explicit `member_id` and only member_slug honours it, but it
                # reads the config, which this path must not do on the loop.
                # Retries FIRST: a re-emitted open has to reach the log ahead of the
                # close computed for the same key below, or the ledger records a close
                # with no open before it.
                _emits: list[tuple[str, tuple[str, str], str, dict]] = list(_retry)
                for _sk, _who in current.items():
                    if _sk not in prev:
                        _emits.append((_sk, _who, SLOT_OPENED, {"slot_key": _sk}))
                for _sk, _who in prev.items():
                    if _sk not in current:
                        _emits.append(
                            (
                                _sk,
                                _who,
                                SLOT_CLOSED,
                                {"slot_key": _sk, "reason": "closed"},
                            )
                        )

                def _emit_slots(
                    events: list[tuple[str, tuple[str, str], str, dict]] = _emits,
                    unconfirmed: dict[str, tuple[str, str] | None] = (
                        self._member_slots_unconfirmed
                    ),
                    retry: list[tuple[str, tuple[str, str], str, dict]] = (
                        self._member_slots_retry
                    ),
                ) -> None:
                    def _report(
                        _key: str, _slug_in: str, _store_in: str, _etype: str, _data: dict
                    ) -> None:
                        # A close goes through the checkpoint: putting the key BACK is
                        # what makes the next comparison recompute it.
                        if _etype == SLOT_CLOSED:
                            unconfirmed[_key] = (_slug_in, _store_in)
                            return
                        # An open is retried VERBATIM and the checkpoint is left
                        # alone. Popping the key instead only recomputes the open
                        # while the slot is still open; if it closed in this window
                        # the comparison computes nothing and the episode is lost for
                        # the life of the process. Leaving the checkpoint claiming the
                        # open means a later close is still computed normally, and
                        # this entry is re-emitted ahead of it.
                        retry.append((_key, (_slug_in, _store_in), _etype, _data))

                    for _key, (_slug_in, _store_in), _etype, _data in events:
                        try:
                            _slug = _slug_in
                            if not _slug and _store_in:
                                # Case (b): the config read this path may not do on
                                # the loop is safe here, in the worker.
                                from kiro_crew.config.loader import KiroCrewConfig

                                _rec = KiroCrewConfig.load().memory_stores.get(_store_in)
                                _owner = getattr(_rec, "owner_member", "") if _rec else ""
                                if _owner:
                                    _slug = member_slug(_owner)
                            # `name` is left empty on purpose: `emit` passes
                            # `name or slug` to `ensure`, whose _resolved_name looks
                            # the exact name up in the roster, so the name is
                            # resolved once per member rather than per event.
                            if not eventlog_hooks.emit(_slug, "", _etype, _data):
                                # Reported, not swallowed. The checkpoint already
                                # counts this transition as handed over, so without
                                # this the loss is permanent for the life of the
                                # process: the next broadcast compares against a
                                # checkpoint that claims the event was written.
                                _report(_key, _slug_in, _store_in, _etype, _data)
                        except Exception:
                            _report(_key, _slug_in, _store_in, _etype, _data)
                            logger.debug("slot event-log emit failed", exc_info=True)

                # The checkpoint advances only once the transitions are HANDED
                # OVER. `submit` is bounded and can refuse, and this checkpoint is
                # the only record of what is still unwritten: advancing it first
                # turns a refusal into permanent staleness, because the next
                # broadcast compares against `current` and computes no transitions
                # to retry. Leaving it at `prev` instead means the next broadcast
                # recomputes the same set and hands it over again.
                if _emits:
                    # No running-loop check: `submit` queues on the ordered
                    # executor whether or not a loop is running, and queuing
                    # BOTH paths is what keeps them in one order. Running a
                    # no-loop caller inline instead would let it reach the log
                    # ahead of an append already queued by a loop caller.
                    if eventlog_hooks.submit(_emit_slots):
                        self._member_driven_slots_seen = current
                else:
                    # A change with no open or close (a slot's store changed under
                    # the same key) has nothing to hand over, so holding the
                    # checkpoint back would recompute an empty set forever.
                    self._member_driven_slots_seen = current
        except Exception:
            logger.debug("slot open/close event-log hook failed", exc_info=True)

    def push_slot_title(self, key: str, title: str, *, full: bool = True) -> None:
        """Push a targeted title update for a single slot.

        By default also pushes a full slots update so the sidebar reflects the
        new title without callers needing to do both. Pass ``full=False`` for
        high-frequency streaming partials (word-by-word title reveal) to send
        only the lightweight ``slot_title`` event; finalize with a ``full=True``
        call once.
        """
        self._broadcast({"_type": "slot_title", "key": key, "title": title})
        if full:
            self.push_slots_update()

    def push_slot_patch(self, key: str, fields: Iterable[str]) -> None:
        """Publish a metadata edit to one slot without re-sending the slot list.

        Sockets that declared the ``slot_patch`` capability receive
        ``{"type": "slot_patch", "data": {"slots": [{"key", <field>: ...}]}}``,
        a row carrying only the named fields, and merge it into their copy of
        the row. Every other consumer gets the full list through
        ``push_slots_update(legacy_only=True)``, so an old tab kept open across
        a gateway restart sees the same thing it always did.

        The values come from the dashboard-user projection of the slot, so a
        patched field reads exactly as it would in the full frame (the title is
        redacted the same way). Only fields in :data:`_SLOT_PATCH_FIELDS` are
        accepted because ``source_links`` and other per-audience fields require
        a full frame. A slot that is gone or still under construction falls back
        to an ordinary full push.
        """
        field_names = tuple(fields)
        unsupported = sorted(set(field_names) - _SLOT_PATCH_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported slot patch fields: {', '.join(unsupported)}")
        slot = self._slots.get(key)
        under_construction = getattr(self, "_slots_under_construction", None) or ()
        if slot is None or key in under_construction:
            self.push_slots_update()
            return
        if self._has_legacy_slots_audience():
            self.push_slots_update(legacy_only=True)
        if not self._has_slot_patch_clients():
            return
        row = self.serialize_slot(slot, dashboard_user=True)
        patch: dict[str, Any] = {"key": key}
        for field in field_names:
            if field in row:
                patch[field] = row[field]
        self._send_slot_patch({"slots": [patch]})

    def push_lineage_patch(self) -> None:
        """Push every live slot whose ``parent`` moved, as one ``slot_patch`` frame.

        The session tree's change event lands here (see ``CrewLogPublisher``), so the
        sidebar nests a worker the moment its crew log says who opened it -- and un-nests
        it the moment that creator closes or releases it -- without re-reading the slot
        list. Each row carries ``parent`` and ``lineage_pending`` and nothing else, the
        same two values a full frame computes for it through
        :func:`_attach_slot_parents`, so a patch and a full frame never disagree.

        Rows whose two values match what this method last sent are left out, which is
        what makes an idle tree -- or a burst that moved nothing visible -- cost no
        frame. The memory is pruned to the live keys on each pass.
        """
        rows = self._lineage_rows()
        if not rows:
            return
        _attach_slot_parents(rows, getattr(self, "spend_slot_by_session", None))
        sent = getattr(self, "_lineage_sent", None)
        if sent is None:
            sent = {}
            self._lineage_sent = sent
        live = {row["key"] for row in rows}
        for key in [k for k in sent if k not in live]:
            del sent[key]
        changed: list[dict[str, Any]] = []
        for row in rows:
            value = (row.get("parent"), bool(row.get("lineage_pending")))
            if sent.get(row["key"]) == value:
                continue
            sent[row["key"]] = value
            changed.append({"key": row["key"], "parent": value[0], "lineage_pending": value[1]})
        if not changed:
            return
        if self._has_legacy_slots_audience():
            self.push_slots_update(legacy_only=True)
        if self._has_slot_patch_clients():
            self._send_slot_patch({"slots": changed})

    def _lineage_rows(self) -> list[dict[str, Any]]:
        """One row per live slot carrying exactly what the lineage join reads.

        The key, plus the two mint-witness fields ``serialize_slot`` writes for the
        full frame. A child dispatched and not yet run has no node in the fold, and
        the join nests it from ``created_by`` only when ``lineage_minted`` vouches for
        it, so a row with the key alone would answer ``parent: None`` for that child
        and a patch would detach a session the full frame nests.
        """
        under_construction = getattr(self, "_slots_under_construction", None) or ()
        rows: list[dict[str, Any]] = []
        for k, slot in list(self._slots.items()):
            if k in under_construction:
                continue
            rows.append(
                {
                    "key": k,
                    "created_by": getattr(slot, "_created_by", ""),
                    "lineage_minted": bool(getattr(slot, "_lineage_minted", False)),
                }
            )
        return rows

    def push_slot_removed(self, key: str) -> None:
        """Publish that slot *key* left the registry without re-sending the list.

        Patch-capable sockets receive ``{"slots": [...], "removed": [key]}``.
        The ``slots`` rows re-state the ``parent`` of every row the removal MOVED,
        because a removed conductor changes its workers' ``parent`` in the full
        frame; carrying those rows keeps the sidebar's nesting identical to what a
        full list would have produced. Everyone else gets the full list, as with
        :meth:`push_slot_patch`.

        Two shapes qualify, and the second is why the test is not ``key is None``.
        A worker with no open ancestor loses its key outright. A worker that HAS one
        is re-parented onto it and carries ``ancestor``, so its key is a live string
        -- the lead's -- and a null test would leave the client holding the closed
        conductor's key, a row the same frame just removed. Every one of those
        workers would then render top-level with the orphan glyph until a full frame
        happened along, which is the behaviour this method exists to prevent.

        A key that is registered again (a same-name replacement landed while the
        close was tearing down) is not removed: the full push describes it. The
        closed slot's dashboard card is still evicted, so the replacement never
        presents a card generated for another transcript.
        """
        if key in self._slots:
            # The replacement's own card, if it has one, is its own; a card whose
            # owner is not the live slot's identity was generated for the closed
            # transcript and goes with it, so a replacement never presents its
            # predecessor's card while it has none of its own.
            if self._dynamic_cards is not None:
                entry = self._dynamic_cards.publisher.entries.get(key)
                live = getattr(self._slots[key], "_dashboard_card_identity", None)
                if entry is not None and entry.owner != live:
                    self._dynamic_cards.publisher.forget(key)
                # A DERIVED card is retired on exactly the same rule and for exactly the
                # same reason: it was built for the closed transcript, so it goes with it
                # rather than being presented by the replacement as its own.
                held = self._dynamic_cards.derived.get(key)
                if held is not None and held["owner"] != live:
                    self._dynamic_cards.forget_derived(key)
            self.push_slots_update()
            return
        if self._dynamic_cards is not None:
            self._dynamic_cards.publisher.forget(key)
            self._dynamic_cards.forget_derived(key)
            # The retirement STAMP goes with the slot too. It is kept so a write arriving after a
            # removal can still be ordered against it, and a slot that is definitively gone has
            # no later write to order -- so keeping it past this point retains one string per
            # slot the gateway ever hosted, for nothing. This is the only place that knows the
            # removal is definitive rather than a replacement.
            self._dynamic_cards.forget_retired(key)
        if self._has_legacy_slots_audience():
            self.push_slots_update(legacy_only=True)
        if self._has_slot_patch_clients():
            rows = self._lineage_rows()
            _attach_slot_parents(rows, getattr(self, "spend_slot_by_session", None))
            restated = [
                {"key": row["key"], "parent": row["parent"]}
                for row in rows
                if isinstance(row.get("parent"), dict)
                and (row["parent"].get("key") is None or row["parent"].get("ancestor") is True)
                and not row.get("lineage_pending")
            ]
            self._send_slot_patch({"slots": restated, "removed": [key]})
        self._emit_member_slot_transitions()

    def _has_slot_patch_clients(self) -> bool:
        return any(
            not ws.closed and ws.get(SLOT_PATCH_WS_FLAG, False)
            for ws in list(getattr(self, "_ws_clients", None) or ())
        )

    def _send_slot_patch(self, data: dict[str, Any]) -> None:
        """Serialize one ``slot_patch`` frame and hand it to patch-capable sockets."""
        _websocket_for(self).send_ws_slot_patch(json.dumps({"type": "slot_patch", "data": data}))

    def ensure_dynamic_card_store(self) -> Any:
        """The card store, constructed if absent, with the MODEL path left off.

        Delegates to the module-level :func:`_new_card_store` rather than sharing a method with
        :meth:`set_dynamic_cards_enabled`, because that method is exercised against bare stub
        objects carrying only ``_dynamic_cards`` -- a sibling method call would fail on them,
        and the deferred-import assertion those tests make is about WHEN the import happens,
        which a module-level function keeps true for both callers.

        A derived card -- one the product assembles from a fold it already keeps -- costs no
        call and spends nothing from the model path's hourly budget, so it is deliberately not
        gated on the owner's opt-in to that cost. Constructing the container only inside
        :meth:`set_dynamic_cards_enabled` would make a free card's availability depend on
        TOGGLE HISTORY: never enabled means no store and therefore no card, while
        enabled-once-then-off leaves a store behind and the card appears. Same feature,
        opposite answers, decided by a switch neither answer is about.

        Constructing it here with ``enabled`` untouched keeps the model path exactly as opt-in
        as it was: no worker is started and no attempt is spent by existing.
        """
        return _new_card_store(self)

    def set_dynamic_cards_enabled(self, enabled: bool) -> None:
        """Post-bind activation; retain the producer and its budgets across toggles."""
        if self._dynamic_cards is None and not enabled:
            return
        _new_card_store(self).set_enabled(enabled)

    def notify_dashboard_card(self, slot: "_ChatSlot", reason: str) -> None:
        """Queue semantic work from a real event, never from a read/serialize."""
        loop = self.serving_loop
        if loop is None or loop.is_closed():
            return
        if loop is not self._running_loop():
            loop.call_soon_threadsafe(self.notify_dashboard_card, slot, reason)
            return
        try:
            if self._dynamic_cards is not None:
                self._dynamic_cards.notify(slot, reason)
        except Exception:
            logger.debug("Dashboard card event skipped", exc_info=True)

    def push_session_summary(self, key: str) -> None:
        """Broadcast that a session's intent summary was regenerated.

        Lets the summary panel invalidate immediately instead of polling, which
        matters because the summary is deliberately a pull-friendly artifact: a
        panel that polled would reintroduce the checking loop the feature exists
        to remove. Fire-and-forget — the client's own staleness window remains
        the safety net if the event is missed.
        """
        self._broadcast({"_type": "session_summary", "key": key})

    def push_artifact_update(self, slug: str, version: int, *, deleted: bool = False) -> None:
        """Broadcast an artifact content change to all connected clients.

        Emitted from the artifact mutation funnel (create / content update /
        revert / pull-latest / relocate / delete) so every open dashboard
        window — main, popouts, companion chat panels — can invalidate its
        artifact queries immediately instead of waiting for the 30s react-query
        staleness window. Fire-and-forget, best-effort: the
        staleness window remains the safety net if a client misses the event.
        """
        self._broadcast(
            {
                "_type": "artifact_update",
                "slug": slug,
                "version": version,
                "deleted": deleted,
            }
        )

    def push_refresh(self, *kinds: str) -> None:
        """Push a lightweight refresh hint for specific data types.

        The frontend receives ``event: refresh`` with ``data: kind1,kind2``
        and fetches fresh data only for those types.  This replaces blind
        polling — the server tells the client *when* to refresh, not the
        client guessing on a timer.

        Supported kinds: ``crons``, ``lessons``, ``agents``, ``history``,
        ``taskrunner``.
        """
        self._broadcast({"_type": "refresh", "kinds": ",".join(kinds)})

    def push_update_progress(self, step: str, detail: str = "") -> None:
        """Broadcast an update progress event to all connected clients.

        ``step`` is a short machine-readable phase name (e.g. ``pulling``,
        ``syncing``, ``building``, ``installing``, ``restarting``, ``failed``).
        ``detail`` is an optional human-readable message.
        """
        self._update_progress = {"step": step, "detail": detail}
        self._broadcast(
            {
                "_type": "update_progress",
                "step": step,
                "detail": detail,
            }
        )

    def clear_update_progress(self) -> None:
        """Reset update progress (e.g. after cancel or completion)."""
        self._update_progress = None

    def _broadcast(self, note: dict[str, Any]) -> None:
        """Send a message to all connected SSE and WS clients."""
        for q in self._sse_queues:
            try:
                q.put_nowait(note)
            except asyncio.QueueFull:
                pass
        self._notify_event.set()
        # WS broadcast — translate internal _type to WS message format
        if self._ws_clients:
            msg_type = note.get("_type", "notification")
            # Payload the scope gate inspects (slot / source keys), tracked per
            # branch so the chokepoint can filter correctly.
            ws_data: object
            if msg_type == "slots":
                # SSE (above) consumed the BARE ``_slots_list``. The WS frame is
                # built from the ENRICHED ``_slots_list_ws`` (public-repo status
                # for dashboard users) when present — app tokens are re-filtered
                # in ``_serialize_for_client`` so they never receive it, and SSE
                # never reads this key. Falls back to the bare list for callers
                # (targeted title pushes, tests) that send only ``_slots_list``.
                slots_list = (
                    note.get("_slots_list_ws")
                    or note.get("_slots_list")
                    or json.loads(note["slots"])
                )
                # ``data`` for slots carries the whole envelope so the
                # chokepoint can per-app filter and re-serialize it.
                ws_data = {
                    "slots": slots_list,
                    "yolo": note.get("_yolo", False),
                    "channelTrusted": note.get("channelTrusted", False),
                    # Consumed by ``_send_ws_all``; never serialized to a client.
                    "_legacy_only": bool(note.get("_legacy_only", False)),
                }
                # Built by `_slots_ws_frame`, NOT inline: the owner frame in
                # `_do_slots_broadcast` has to carry an identical key set, and it
                # cannot if each site names its own keys. See that function.
                ws_msg = _slots_ws_frame(
                    slots_list,
                    yolo=bool(ws_data["yolo"]),
                    channel_trusted=bool(ws_data["channelTrusted"]),
                    gitlab_hosts_gen=note.get("gitlabHostsGeneration"),
                    folders=note.get("folders"),
                    folders_gen=note.get("foldersGeneration"),
                    governance_gen=note.get("governanceGeneration"),
                )
            elif msg_type == "slot_title":
                ws_data = {"key": note["key"], "title": note["title"]}
                ws_msg = json.dumps({"type": "slot_title", "data": ws_data})
            elif msg_type == "refresh":
                ws_data = {"kinds": note["kinds"].split(",")}
                ws_msg = json.dumps({"type": "refresh", "data": ws_data})
            elif msg_type == "update_progress":
                ws_data = {"step": note["step"], "detail": note.get("detail", "")}
                ws_msg = json.dumps({"type": "update_progress", "data": ws_data})
            elif msg_type == "artifact_update":
                # Typed envelope (not the generic `notification` fallback) so
                # useWebSocket and future consumers get a self-documenting
                # event: {slug, version, deleted}.
                ws_data = {
                    "slug": note["slug"],
                    "version": note.get("version", 0),
                    "deleted": note.get("deleted", False),
                }
                ws_msg = json.dumps({"type": "artifact_update", "data": ws_data})
            elif msg_type == "session_summary":
                # Typed envelope, for the same reason as artifact_update above.
                # Without it this event falls into the generic `notification`
                # fallback, where two things go wrong: the client's
                # `case 'session_summary'` never matches (so the panel is never
                # invalidated and only a reload shows a new summary — defeating
                # the push-on-change design that lets the panel skip polling),
                # and the payload is dispatched as a Notification, putting one
                # entry with no `ts` in the bell feed.
                ws_data = {"key": note["key"]}
                ws_msg = json.dumps({"type": "session_summary", "data": ws_data})
            elif msg_type == "chat_message":
                # One serialiser, both doors — see chat_message_frame().
                # include_metadata=True because THIS door filters downstream:
                # _send_ws_all -> _ws_client_allowed (deny-by-default event
                # scope) decides per socket whether an app token may see this
                # slot at all. The SSE door has no such gate and decides for
                # itself; do not copy this True over there.
                chat_data = chat_message_frame(note, include_metadata=True)
                ws_data = chat_data
                ws_msg = json.dumps({"type": "chat_message", "data": chat_data})
            else:
                ws_data = note
                ws_msg = json.dumps({"type": "notification", "data": note})
            self._send_ws_all(msg_type, ws_data, ws_msg)

    def _spawn_ws_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        _websocket_for(self)._spawn_ws_send(ws, msg)

    def _drop_if_stalled(self, ws: web.WebSocketResponse) -> bool:
        return _websocket_for(self)._drop_if_stalled(ws)

    def _on_ws_send_done(self, task: asyncio.Task) -> None:
        _websocket_for(self)._on_ws_send_done(task)

    def _ws_client_allowed(self, ws: web.WebSocketResponse, msg_type: str, data: object) -> bool:
        return _websocket_for(self)._ws_client_allowed(ws, msg_type, data)

    def _serialize_for_client(
        self, ws: web.WebSocketResponse, msg_type: str, data: object, default_msg: str
    ) -> str:
        return _websocket_for(self)._serialize_for_client(ws, msg_type, data, default_msg)

    def _serialize_subagent_batch(
        self,
        ws: web.WebSocketResponse,
        msg_type: str,
        data: object,
        default_msg: str,
    ) -> str:
        return _websocket_for(self)._serialize_subagent_batch(ws, msg_type, data, default_msg)

    def _send_ws_all(self, msg_type: str, data: object, msg: str) -> None:
        _websocket_for(self)._send_ws_all(msg_type, data, msg)

    def _send_ws_owners(self, msg: str, *, skip_slot_patch_clients: bool = False) -> None:
        if skip_slot_patch_clients:
            _websocket_for(self)._send_ws_owners(msg, skip_slot_patch_clients=True)
        else:
            _websocket_for(self)._send_ws_owners(msg)

    def broadcast_ws(self, msg_type: str, data: WsPayload) -> None:
        _websocket_for(self).broadcast_ws(msg_type, data)

    def broadcast_context_usage(self, slot_key: str, payload: dict) -> None:
        _persistence_for(self).broadcast_context_usage(self, slot_key, payload)

    def ensure_context_snapshots_loaded(self) -> bool:
        return _persistence_for(self).ensure_context_snapshots_loaded(self)

    def context_snapshot_for(self, slot_key: str) -> dict | None:
        return _persistence_for(self).context_snapshot_for(self, slot_key)

    def _persist_context_snapshots(self) -> None:
        _persistence_for(self)._persist_context_snapshots(self)

    async def deliver_ws_owners(self, msg_type: str, data: WsPayload) -> int:
        return await _websocket_for(self).deliver_ws_owners(msg_type, data)

    def broadcast_ws_owners(self, msg_type: str, data: WsPayload) -> None:
        _websocket_for(self).broadcast_ws_owners(msg_type, data)

    def ws_client_count(self) -> int:
        return _websocket_for(self).ws_client_count()

    def dashboard_user_ws_count(self) -> int:
        return _websocket_for(self).dashboard_user_ws_count()

    def broadcast_browser_event(self, event_type: str, data: dict) -> None:
        _websocket_for(self).broadcast_browser_event(event_type, data)

    def register_ws(self, ws: web.WebSocketResponse, *, owner: bool = False) -> None:
        _websocket_for(self).register_ws(ws, owner=owner)

    async def send_members_subscribed(self, ws: web.WebSocketResponse) -> None:
        await _websocket_for(self).send_members_subscribed(ws)

    def unregister_ws(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).unregister_ws(ws)

    def _remove_ws(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self)._remove_ws(ws)

    def subscribe_logs(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).subscribe_logs(ws)

    def unsubscribe_logs(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).unsubscribe_logs(ws)

    def subscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).subscribe_subagents(ws)

    def unsubscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        _websocket_for(self).unsubscribe_subagents(ws)

    def broadcast_ws_subagent_subscribers(self, msg_type: str, data: WsPayload) -> None:
        _websocket_for(self).broadcast_ws_subagent_subscribers(msg_type, data)

    async def close_all_ws(self) -> None:
        await _websocket_for(self).close_all_ws()


def request_slot_origin(app: str, *, cron_creator: str = "") -> str:
    """Origin for a slot created while serving an HTTP request.

    The request layer is the only place that knows whether an app token was
    presented, which is what separates APP from USER. Call it with the
    request's app name (``request.get("app", "")``) — empty means the caller
    authenticated as the dashboard user, so the slot genuinely is USER.

    Background callers (cron, workflow, Slack, rehydrate) must NOT use this:
    they have no request and would mislabel their slot as a person's, which is
    exactly what `slots:user` grants an app access to. They declare their own
    origin, or leave it untagged.

    ``cron_creator`` is the attested ``cron:<job id>`` key of the caller when
    the request carried one (``handlers._shared.cron_slot_creator``), and it
    wins: a slot a cron opens is CRON, never USER, for the same reason the
    background callers above declare it, so ``slots:user`` never exposes a
    cron's work. It is empty for a person and for an app token.
    """
    if cron_creator:
        return SlotOrigin.CRON
    return SlotOrigin.APP if app else SlotOrigin.USER


# Running build's git (branch, short_commit). Resolved ONCE by the CLI gateway
# entrypoint via set_build_info() — AFTER KIROCREW_PROJECT_DIR is detected and
# BEFORE asyncio.run() starts the loop. Deliberately NOT resolved at import time:
# under systemd the entrypoint imports this module before main() detects the
# project dir, so an import-time git_build_info() would see no project dir and the
# lru_cache would then pin ("", "") forever. DashboardState (built on the loop) and
# status_snapshot() only READ this global — they never call git_build_info() — so
# no subprocess ever runs on the event loop.
_build_info: tuple[str, str] = ("", "")


def set_build_info(info: tuple[str, str]) -> None:
    """Record the running build's ``(branch, short_commit)`` for status payloads.

    Called once from the CLI gateway entrypoint (sync, pre-loop, post-detection).
    Defaults to ``("", "")`` for non-git / packaged installs, which the frontend
    renders by omitting the build-info rows.
    """
    global _build_info
    _build_info = info


# ── Constants ──


_DEFAULT_PORT = DASHBOARD_PORT
_SSE_INTERVAL_SECS = 5


# Every name the owners in state_parts/ define is re-exported here.
import importlib as _importlib  # noqa: E402
import sys as _sys  # noqa: E402

from kiro_crew import _facade_mirror  # noqa: E402
from kiro_crew.dashboard.state_parts.chat_slot import (  # noqa: E402,F401 -- re-exported; see state_parts
    _ChatSlot,
)
from kiro_crew.dashboard.state_parts.dashboard_services import (  # noqa: E402,F401 -- re-exported; see state_parts
    _approvals_for,
    _new_dashboard_persistence,
    _new_notification_coordinator,
    _new_websocket_hub,
    _notifications_for,
    _permission_marker,
    _persistence_for,
    _questions_for,
    _registry_for,
    _websocket_for,
)
from kiro_crew.dashboard.state_parts.injected_turns import (  # noqa: E402,F401 -- re-exported; see state_parts
    _REDIRECT_TARGET_RE,
    BUSY_RECOVERY_PREFIX,
    COMPACTION_RECOVERY_PREFIX,
    CONN_RECOVERY_PREFIX,
    CRON_NOTIFY_END,
    CRON_NOTIFY_PREFIX,
    CRON_NOTIFY_RE,
    EMPTY_RESPONSE_RECOVERY_PREFIX,
    HOOK_CONTINUATION_RECOVERY_PREFIX,
    HOOK_HALTED_RECOVERY_PREFIX,
    MANUAL_RESUME_RECOVERY_PREFIX,
    MONITOR_WAKE_PREFIX,
    POSTTOKEN_RECOVERY_PREFIX,
    PROMISE_ONLY_RECOVERY_PREFIX,
    REFUSAL_FALLBACK_RECOVERY_PREFIX,
    REFUSAL_INBAND_RECOVERY_PREFIX,
    REFUSAL_RECOVERY_PREFIX,
    STALE_RECOVERY_PREFIX,
    SUBAGENT_COMPLETION_PREFIXES,
    SUBAGENT_SYNTHESIS_PREFIX,
    SUBAGENT_SYNTHESIS_PROMPT,
    TOOL_STALL_RECOVERY_PREFIX,
    build_infra_retry_prompt,
    build_refusal_recovery_prompt,
    build_stale_recovery_prompt,
    build_tool_stall_recovery_prompt,
    extract_log_redirect_target,
    parse_hook_continuations,
    should_queue_hook_continuation,
    should_queue_refusal_recovery,
)
from kiro_crew.dashboard.state_parts.notification_log import (  # noqa: E402,F401 -- re-exported; see state_parts
    _MAX_PERSISTED_NOTIFICATIONS,
    _NOTIFICATIONS_FILE,
    _UNSERVABLE_NOTIFICATION_CAP_DIVISOR,
    _load_notifications,
    _maybe_trim_notifications,
    _note_ts_epoch,
    _notification_io_executor,
    _notification_io_pool_lock,
    _notifications_path,
    _persist_notification,
    _read_notification_lines,
    _redact_note_value,
    _rewrite_notifications,
    _servable_note,
    sweep_expired_notifications,
)
from kiro_crew.dashboard.state_parts.session_events import (  # noqa: E402,F401 -- re-exported; see state_parts
    _AUTO_COMPACT_CANCELLED_NOTICE,
    _AUTO_COMPACT_FAILED_NOTICE,
    _AUTO_COMPACT_NOTICE,
    _AUTO_COMPACT_WAITING_NOTICE,
    _AUTO_RECYCLE_NOTICE,
    _AUTO_RESTART_UNCOMPACTABLE_NOTICE,
    _INBOUND_UNBIND_NOTICE,
    _INBOUND_UNBIND_WHY,
    _INBOUND_UNBIND_WHY_DEFAULT,
    _RESTART_MEMORY_TAIL,
    _SESSION_RECYCLED_NOTICE,
    _STUCK_TURN_NOTICE,
    _record_crew_log_class,
    note_crew_log_class,
    stuck_turn_notice,
)
from kiro_crew.dashboard.state_parts.sidebar_stores import (  # noqa: E402,F401 -- re-exported; see state_parts
    _FOLDER_REPOSITORY,
    _T,
    _DurableTagSnapshot,
    _safe_folder_tree,
)
from kiro_crew.dashboard.state_parts.slot_directory import (  # noqa: E402,F401 -- re-exported; see state_parts
    _CHAT_N_RE,
    _SLOT_KEY_FILENAME_UNSAFE_RE,
    _SLOT_KEY_HASH_CHARS,
    _SLOT_KEY_KEEP_CHARS,
    _SLOT_KEY_MAX_CHARS,
    MAX_LIVE_SLOTS,
    MAX_SLOTS_PER_CREATOR,
    NATIVE_SUBAGENT_DONE_RESULT_CAP,
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
    NATIVE_SUBAGENT_OUTPUT_HARD,
    NATIVE_SUBAGENT_OUTPUT_TAIL,
    SlotOrigin,
    _ascii_slot_key,
    _is_genuine_slack_link,
    _mint_slot_key,
    _normalize_slot_key,
    _slot_index_from_key,
    native_subagent_output_tail,
)
from kiro_crew.dashboard.state_parts.slot_session import (  # noqa: E402,F401 -- re-exported; see state_parts
    _SLOT_KEY_TITLE_RE,
    NEW_SESSION_TITLE,
    VALID_MEMORY_MODES,
    CrewLogPrevious,
)
from kiro_crew.dashboard.state_parts.slot_sidebar import (  # noqa: E402,F401 -- re-exported; see state_parts
    _TAGS_REVISION_EPOCH_FILE,
    _TAGS_REVISION_EPOCH_UNPERSISTED,
    _TAGS_REVISION_SEQ_LOCK,
    _claim_tags_revision_epoch,
    ensure_tags_revision_epoch,
    mint_tags_revision,
)
from kiro_crew.dashboard.state_parts.slot_todo import (  # noqa: E402,F401 -- re-exported; see state_parts
    _fold_line_breaks,
    _todo_canonical_text,
)
from kiro_crew.dashboard.state_parts.slot_transcript import (  # noqa: E402,F401 -- re-exported; see state_parts
    _LAZY_WINDOW_LOCK,
    _MAX_SLOT_MESSAGES,
    _OPTIONS_RE,
    _PROMPT_ROLES,
    _QUESTION_RETIRING_ROLES,
    _on_event_loop,
    _parse_options,
    _redact,
    load_window_off_loop,
)
from kiro_crew.dashboard.state_parts.slot_turns import (  # noqa: E402,F401 -- re-exported; see state_parts
    _MAX_CONSUMED_MERGE_CARDS,
    _MAX_CONTEXT_PER_SOURCE,
    _MAX_PENDING_CONTEXT,
    _MAX_PENDING_SUBAGENT_DELIVERIES,
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
    _ConsumedMergeCard,
    _delivery_key,
    _log_task_exception,
    _note_authorized_elsewhere,
    context_entry_expired,
)
from kiro_crew.dashboard.state_parts.slots_frame import (  # noqa: E402,F401 -- re-exported; see state_parts
    _CHANNEL_LABELS,
    _SLOTS_BROADCAST_BYTES_PER_S,
    _SLOTS_BROADCAST_MAX_INTERVAL_S,
    _attach_slot_parents,
    _expected_binding,
    _lineage_seed_lock,
    _link_label,
    _mirror_link_nonce,
    _published_queued_by_slot,
    _redacted_link_target,
    _request_lineage_seed,
    _slack_link_nonce,
    _slots_broadcast_interval_for,
    _slots_serialization_note,
    _slots_ws_frame,
)
from kiro_crew.dashboard.state_parts.slots_push import (  # noqa: E402,F401 -- re-exported; see state_parts
    _DEFERRED_SLOTS_FLUSH_DELAY_S,
    _SLOT_PATCH_FIELDS,
    _new_card_store,
    _slots_broadcast_clock,
)
from kiro_crew.dashboard.state_parts.source_links import (  # noqa: E402,F401 -- re-exported; see state_parts
    _CHIP_STATUS_KEYS,
    _MAX_DISMISSED_SOURCE_LINKS,
    _MAX_SOURCE_LINKS_PER_SLOT,
    _NON_DURABLE_SOURCE_LINK_ROLES,
    _PUBLIC_STATUS_DENY_AUDIT,
    _PUBLIC_STATUS_GRANT_AUDIT,
    _PUBLIC_STATUS_GRANT_WINDOW_SECS,
    _SERIALIZED_SOURCE_LINKS_PER_SLOT,
    _SOURCE_ACTIVE_WINDOW_SECS,
    _audit_public_status_denied,
    _audit_public_status_grant,
    _budgeted_source_links,
    _cached_check_status,
    _drop_expired_public_status_audits,
    _project_source_links,
    _repo_is_public,
    _source_links_by_kind,
)
from kiro_crew.dashboard.state_parts.status_payload import (  # noqa: E402,F401 -- re-exported; see state_parts
    _BUNDLE_ID_CACHE,
    _SERVED_INDEX,
    _fmt_duration,
    _governance_status,
)
from kiro_crew.dashboard.state_parts.transcript_rows import (  # noqa: E402,F401 -- re-exported; see state_parts
    _TRANSIENT_ROLES,
    _TURN_CONTINUING_INJECT_KINDS,
    _TURN_INJECT_KINDS,
    _WIRE_ONLY_ROLES,
    QUIET_END_SERVER,
    QUIET_END_TOOL,
    TURN_OPENING_INJECT_KINDS,
    _compaction_keep_record,
    _is_turn_inject,
    _mark_permission_resolved,
    append_and_surface,
    chat_message_frame,
    chat_message_note,
    durable_row_count,
    is_quiet_end_row,
    is_stop_event_row,
    is_turn_interrupted,
    parse_cls_meta,
    row_mid,
)

# The owners in state_parts/ hold the moved code; this module stays the import
# path and the patch surface (see state_parts/__init__.py).
#: BASE imports this module binds for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    CredentialEvidence,
    DashboardPersistenceCoordinator,
    EMPTY_QUEUE_SIGNATURE,
    FolderRepository,
    MEMORY_MODES,
    NotificationCoordinator,
    NotificationValidationError,
    RecoveryReplays,
    SideState,
    SlotBufferCoordinator,
    SlotProjection,
    SlotQueueRepository,
    UNBIND_REASON_DASHBOARD_UNLINK,
    UNBIND_REASON_ENTRY_DELETED,
    UNBIND_REASON_ORIGIN_REBIND,
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    WebSocketHub,
    datetime,
    durable_queue_entries,
    durable_queue_view,
    fsync_dir,
    is_system_notice,
    latest_transcript_ts,
    mint_row_mid,
    monotonic_transcript_ts,
    normalize_note,
    owner_only_opener,
    payload_from_legacy,
    queue_persist_signature,
    remediation_for,
    resolve_effective_agent,
    strip_markdown_preview,
)
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.state_parts.transcript_rows",
    "kiro_crew.dashboard.state_parts.injected_turns",
    "kiro_crew.dashboard.state_parts.notification_log",
    "kiro_crew.dashboard.state_parts.source_links",
    "kiro_crew.dashboard.state_parts.slot_sidebar",
    "kiro_crew.dashboard.state_parts.slot_todo",
    "kiro_crew.dashboard.state_parts.slot_session",
    "kiro_crew.dashboard.state_parts.slot_turns",
    "kiro_crew.dashboard.state_parts.slot_ladder",
    "kiro_crew.dashboard.state_parts.slot_transcript",
    "kiro_crew.dashboard.state_parts.chat_slot",
    "kiro_crew.dashboard.state_parts.dashboard_services",
    "kiro_crew.dashboard.state_parts.session_events",
    "kiro_crew.dashboard.state_parts.status_payload",
    "kiro_crew.dashboard.state_parts.slot_directory",
    "kiro_crew.dashboard.state_parts.sidebar_stores",
    "kiro_crew.dashboard.state_parts.slots_frame",
    "kiro_crew.dashboard.state_parts.slots_push",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Globals one owner rebinds with ``global``: not bound here, served live from it.
_SERVED_LIVE = (
    "_TAGS_REVISION_EPOCH",
    "_TAGS_REVISION_SEQ",
    "_lineage_failure_warned",
    "_lineage_seed_in_flight",
    "_notification_io_pool",
)
#: Indexed name the facade does not bind -> the owner that answers a read of it.
#: The surface is every name this module binds plus those globals: a helper the
#: split introduced in an owner is not part of the facade.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__], _OWNERS, excluded=(), surface={*globals(), *_SERVED_LIVE}
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.state_parts.notification_log import (  # noqa: F401
        _notification_io_pool,
    )
    from kiro_crew.dashboard.state_parts.slot_sidebar import (  # noqa: F401
        _TAGS_REVISION_EPOCH,
        _TAGS_REVISION_SEQ,
    )
    from kiro_crew.dashboard.state_parts.slots_frame import (  # noqa: F401
        _lineage_failure_warned,
        _lineage_seed_in_flight,
    )
