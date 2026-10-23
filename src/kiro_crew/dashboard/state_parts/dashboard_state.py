"""The ``DashboardState`` class: its defaults, constructor, properties and member bindings.

Also the default port and SSE interval, and the build info the constructor reads
(``set_build_info`` rebinds it here).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_crew.config.loader import DASHBOARD_PORT, config_dir
from kiro_crew.dashboard import pinned_session_order
from kiro_crew.dashboard.folder_repository import FOLDERS_FILE
from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator, QuestionCoordinator
from kiro_crew.dashboard.slot_registry import SlotRegistry
from kiro_crew.dashboard.state_parts import dashboard_services as _dashboard_services
from kiro_crew.dashboard.state_parts import own_methods
from kiro_crew.dashboard.state_parts import session_events as _session_events
from kiro_crew.dashboard.state_parts import sidebar_stores as _sidebar_stores
from kiro_crew.dashboard.state_parts import slot_directory as _slot_directory
from kiro_crew.dashboard.state_parts import slot_list_hold as _slot_list_hold
from kiro_crew.dashboard.state_parts import slots_frame as _slots_frame
from kiro_crew.dashboard.state_parts import slots_push as _slots_push
from kiro_crew.dashboard.state_parts import source_links as _source_links
from kiro_crew.dashboard.state_parts import status_payload as _status_payload
from kiro_crew.dashboard.state_parts.dashboard_services import (
    _new_dashboard_persistence,
    _new_notification_coordinator,
    _new_websocket_hub,
)
from kiro_crew.dashboard.state_parts.notification_log import _load_notifications
from kiro_crew.dashboard.state_parts.session_events import _record_crew_log_class
from kiro_crew.dashboard.state_parts.slots_frame import _SLOTS_BROADCAST_INTERVAL_S
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.messaging import turn_ceiling
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.notifications.push_store import PushSubscriptionStore
from kiro_crew.notifications.rate_limit import AppRateLimiter
from kiro_crew.notifications.resource_pressure import ResourcePressureNotifier
from kiro_crew.notifications.settings import ChannelSettings
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.safety_override import safety_override

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import (
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )
    from kiro_crew.dashboard.file_index import FileIndexRegistry
    from kiro_crew.dashboard.listener_guard import ListenerGuard
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.messaging.transport import MessagingTransport
    from kiro_crew.power import SleepInhibitor
    from kiro_crew.slack.outbound import PostedOptions

logger = logging.getLogger("kiro_crew.dashboard.state")


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


class DashboardState:
    """Shared state injected into all handlers via ``app["state"]``."""

    # Typed here for the owner functions that read them through ``self``.
    notification_bus: NotificationBus
    notification_channel_settings: ChannelSettings
    resource_pressure_notifier: ResourcePressureNotifier
    file_indexes: FileIndexRegistry
    _chat_pins_lock: LoopBoundLock
    _folders_lock: LoopBoundLock
    _folders_generation: int
    #: Set lazily by ``push_lineage_patch``: key -> (parent, lineage_pending).
    _lineage_sent: dict[str, tuple[Any, bool]]

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
    # True while startup is still discovering the complete slot set: lists carry
    # ``slotsPartial``. The hold fields below belong to state_parts/slot_list_hold.py.
    slot_list_rebuilding: bool = False
    _slot_list_channel_held_keys: "frozenset[str] | set[str]" = frozenset()
    _slot_list_released_keys: "frozenset[str] | set[str]" = frozenset()
    _slot_list_hold_channels: bool = False
    _slot_list_rebuild_finish_requested: bool = False
    _slot_list_rebuild_began: float = 0.0
    _slot_list_rebuild_deadline: "asyncio.TimerHandle | None" = None

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

    #: The person's manual order for every session (``dashboard.session_sort``
    #: "manual"), made on first use by :attr:`manual_session_order`. ``None``
    #: as a class default for the same reason as the pinned order's fields.
    _manual_session_order: "pinned_session_order.ManualOrder | None" = None

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
        # Both lazy-init in server_runtime/instances_lifecycle.py.
        self.instances_manager: Any = None  # SshTunnelManager
        self.instances_registry: Any = None  # InstancesRegistry
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
        # Secretary subsystem removed; kept as permanent None for apps/routes_enable.py
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
        self._notification_coordinator = _new_notification_coordinator(
            web_push_fanout=lambda note: self._fan_out_web_push(note)
        )
        # Web Push subscription store (browser push endpoints). State-owned like
        # the bus/limiter/settings so its lifecycle matches the gateway.
        self.push_subscription_store = PushSubscriptionStore()
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
        self._slot_list_channel_held_keys: set[str] = set()
        self._slot_list_released_keys: set[str] = set()
        self._slot_list_hold_channels = False
        # The slot each connected owner dashboard reports in the foreground, by
        # connection (``slot_retention.note_foreground``). The idle sweep never
        # archives a slot someone is looking at.
        self._foreground_slots: dict[int, str] = {}
        # Return-triggered warm task briefs: when each thread was last on screen
        # in an owner view (wall time, this process only), a read-saving "not
        # eligible before" cache, and the live tasks. The claim and the durable
        # away fallback live in the transcript.
        self._warm_task_brief_last_seen: dict[str, float] = {}
        self._warm_task_brief_not_before: dict[str, float] = {}
        self._warm_task_brief_tasks: dict[str, asyncio.Task[Any]] = {}
        self._warm_task_brief_model_active: set[str] = set()
        # Threads whose in-flight model call was left behind by a departure.
        self._warm_task_brief_abandoned: set[str] = set()
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
        # Drops the browser view's pre-exec stop; run by its cleanup hook.
        self._browser_view_unregister: Callable[[], None] | None = None

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
    # `acp/client_timeouts.py::_TOOL_STALL_TIMEOUT` is 600s and is armed once a tool call
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
    # at 12 (dashboard/handlers/terminal_session.py::_MAX_SESSIONS, 429 on excess) and
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

    _log = logging.getLogger("kiro_crew.dashboard.state")

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

    _CRON_FOLDERS_FILE = "cron_folders.json"

    @property
    def manual_session_order(self) -> pinned_session_order.ManualOrder:
        """The manual order in memory; ``chat_manual_order`` reads and writes it."""
        order = self._manual_session_order
        if order is None:
            order = self._manual_session_order = pinned_session_order.ManualOrder()
        return order

    # ── Chat message pin persistence ──

    _CHAT_PINS_FILE = "chat_pins.json"

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

    # state_parts/source_links.py
    source_link_urls = _source_links.source_link_urls
    _slot_chip_urls = staticmethod(_source_links._slot_chip_urls)
    note_source_activity = _source_links.note_source_activity
    _prune_source_activity = _source_links._prune_source_activity
    idle_source_link_urls = _source_links.idle_source_link_urls
    source_link_urls_for_slot = _source_links.source_link_urls_for_slot
    push_source_status = _source_links.push_source_status
    refresh_slot_source_status = _source_links.refresh_slot_source_status

    # state_parts/dashboard_services.py
    approval_timeout_for = _dashboard_services.approval_timeout_for
    effective_max_background_turns = _dashboard_services.effective_max_background_turns
    _background_turn_sema = _dashboard_services._background_turn_sema
    background_turn_stats = _dashboard_services.background_turn_stats
    run_background_turn = _dashboard_services.run_background_turn
    enable_yolo = _dashboard_services.enable_yolo
    disable_yolo = _dashboard_services.disable_yolo
    is_yolo_active = _dashboard_services.is_yolo_active
    request_approval = _dashboard_services.request_approval
    pending_coordinator_approvals = _dashboard_services.pending_coordinator_approvals
    linked_session_busy = _dashboard_services.linked_session_busy
    _audit_and_broadcast_approval = _dashboard_services._audit_and_broadcast_approval
    _audit_approval = _dashboard_services._audit_approval
    resolve_state_approval = _dashboard_services.resolve_state_approval
    resolve_approval = _dashboard_services.resolve_approval
    resolve_slot_approval = _dashboard_services.resolve_slot_approval
    _redact_questions = _dashboard_services._redact_questions
    post_question_card = _dashboard_services.post_question_card
    mark_question_pending = _dashboard_services.mark_question_pending
    clear_question_pending = _dashboard_services.clear_question_pending
    _broadcast_question_retired = _dashboard_services._broadcast_question_retired
    _push_slots = _dashboard_services._push_slots
    recent_question_resolutions = _dashboard_services.recent_question_resolutions
    request_question = _dashboard_services.request_question
    resolve_question = _dashboard_services.resolve_question
    open_agent_ask = _dashboard_services.open_agent_ask
    wait_agent_ask = _dashboard_services.wait_agent_ask
    withdraw_agent_ask = _dashboard_services.withdraw_agent_ask
    cancel_questions_for_slot = _dashboard_services.cancel_questions_for_slot
    start_flush_loop = _dashboard_services.start_flush_loop
    _flush_loop = _dashboard_services._flush_loop
    flush_slot_now = _dashboard_services.flush_slot_now
    _flush_dirty_slots = _dashboard_services._flush_dirty_slots
    _persist_open_slots = _dashboard_services._persist_open_slots
    notify = _dashboard_services.notify
    _fan_out_web_push = _dashboard_services._fan_out_web_push
    _send_web_push_all = _dashboard_services._send_web_push_all
    _deliver_note = _dashboard_services._deliver_note
    register_sse = _dashboard_services.register_sse
    unregister_sse = _dashboard_services.unregister_sse
    _rewrite_notifications_async = _dashboard_services._rewrite_notifications_async
    delete_notification = _dashboard_services.delete_notification
    ack_notification = _dashboard_services.ack_notification
    unack_notification = _dashboard_services.unack_notification
    resolve_skill_review_notifications = _dashboard_services.resolve_skill_review_notifications
    clear_notifications = _dashboard_services.clear_notifications
    ensure_context_snapshots_loaded = _dashboard_services.ensure_context_snapshots_loaded
    context_snapshot_for = _dashboard_services.context_snapshot_for
    _persist_context_snapshots = _dashboard_services._persist_context_snapshots

    # state_parts/session_events.py
    register_channel_transport = _session_events.register_channel_transport
    get_channel_transport = _session_events.get_channel_transport
    channel_status = _session_events.channel_status
    wire_session_compact_callback = _session_events.wire_session_compact_callback
    _notify_channel_compaction = _session_events._notify_channel_compaction
    wire_session_bind_listener = _session_events.wire_session_bind_listener
    wire_session_unbind_listener = _session_events.wire_session_unbind_listener
    _spawn_unbind_notice = _session_events._spawn_unbind_notice
    _on_unbind_notice_done = _session_events._on_unbind_notice_done
    _notify_inbound_unbind = _session_events._notify_inbound_unbind
    _unbind_notice_title = _session_events._unbind_notice_title
    wire_session_recycle_callback = _session_events.wire_session_recycle_callback

    # state_parts/status_payload.py
    served_bundle_id = staticmethod(_status_payload.served_bundle_id)
    _count_lessons = _status_payload._count_lessons
    status_snapshot = _status_payload.status_snapshot

    # state_parts/slot_directory.py
    get_slot = _slot_directory.get_slot
    running_session_keys = _slot_directory.running_session_keys
    spend_slot_by_session = _slot_directory.spend_slot_by_session
    native_subagent_snapshots = _slot_directory.native_subagent_snapshots
    has_slot = _slot_directory.has_slot
    slot_exists = _slot_directory.slot_exists
    get_linked_slot = _slot_directory.get_linked_slot
    resolve_slot = _slot_directory.resolve_slot
    link_slack = _slot_directory.link_slack
    _link_slack_persisted = _slot_directory._link_slack_persisted
    get_or_create_slot = _slot_directory.get_or_create_slot
    live_slot_count = _slot_directory.live_slot_count
    creator_slot_count = _slot_directory.creator_slot_count
    begin_slot_construction = _slot_directory.begin_slot_construction
    end_slot_construction = _slot_directory.end_slot_construction
    reseed_slot_counter = _slot_directory.reseed_slot_counter
    _broadcast_chat_message = _slot_directory._broadcast_chat_message
    _record_member_row = _slot_directory._record_member_row

    # state_parts/sidebar_stores.py
    load_folders = _sidebar_stores.load_folders
    save_folders = _sidebar_stores.save_folders
    load_cron_folders = _sidebar_stores.load_cron_folders
    _persist_cron_folders = _sidebar_stores._persist_cron_folders
    save_cron_folders = _sidebar_stores.save_cron_folders
    create_cron_folder = _sidebar_stores.create_cron_folder
    rename_cron_folder = _sidebar_stores.rename_cron_folder
    delete_cron_folder = _sidebar_stores.delete_cron_folder
    read_pinned_session_order = _sidebar_stores.read_pinned_session_order
    adopt_pinned_session_order = _sidebar_stores.adopt_pinned_session_order
    bump_pinned_order_rev = _sidebar_stores.bump_pinned_order_rev
    save_pinned_session_order = _sidebar_stores.save_pinned_session_order
    pinned_session_flags = _sidebar_stores.pinned_session_flags
    pin_rank = _sidebar_stores.pin_rank
    load_chat_pins = _sidebar_stores.load_chat_pins
    save_chat_pins = _sidebar_stores.save_chat_pins
    remove_chat_pins_for_slots = _sidebar_stores.remove_chat_pins_for_slots
    folders_generation = _sidebar_stores.folders_generation
    mutate_folders = _sidebar_stores.mutate_folders
    read_folders = _sidebar_stores.read_folders
    hold_folders = _sidebar_stores.hold_folders
    _write_folders_confirmed = _sidebar_stores._write_folders_confirmed
    folder_breadcrumb = _sidebar_stores.folder_breadcrumb
    read_durable_tags_snapshot = _sidebar_stores.read_durable_tags_snapshot
    load_tags = _sidebar_stores.load_tags
    save_tags = _sidebar_stores.save_tags
    save_tags_snapshot = _sidebar_stores.save_tags_snapshot
    save_tag_boards = _sidebar_stores.save_tag_boards
    save_tag_boards_snapshot = _sidebar_stores.save_tag_boards_snapshot
    _partition_preserving = staticmethod(_sidebar_stores._partition_preserving)
    _atomic_write_json_strict = staticmethod(_sidebar_stores._atomic_write_json_strict)

    # state_parts/slots_frame.py
    _channel_link_is_live = _slots_frame._channel_link_is_live
    _slot_links = _slots_frame._slot_links
    serialize_slot = _slots_frame.serialize_slot
    serialize_slots = _slots_frame.serialize_slots
    serialize_slot_views = _slots_frame.serialize_slot_views
    _reproject_slots = _slots_frame._reproject_slots
    _drop_orphaned_mcp_report = _slots_frame._drop_orphaned_mcp_report
    _emit_member_slot_transitions = _slots_frame._emit_member_slot_transitions
    _lineage_rows = _slots_frame._lineage_rows

    # state_parts/slots_push.py
    suspend_slots_push = _slots_push.suspend_slots_push
    _deferred_slots_flush = _slots_push._deferred_slots_flush
    push_slots_update = _slots_push.push_slots_update
    _running_loop = staticmethod(_slots_push._running_loop)
    bind_serving_loop = _slots_push.bind_serving_loop
    _schedule_trailing_flush = _slots_push._schedule_trailing_flush
    _trailing_slots_flush = _slots_push._trailing_slots_flush
    _take_slots_audience = _slots_push._take_slots_audience
    _has_legacy_slots_audience = _slots_push._has_legacy_slots_audience
    _do_slots_broadcast = _slots_push._do_slots_broadcast
    push_slot_title = _slots_push.push_slot_title
    push_slot_patch = _slots_push.push_slot_patch
    push_lineage_patch = _slots_push.push_lineage_patch
    push_slot_removed = _slots_push.push_slot_removed
    _has_slot_patch_clients = _slots_push._has_slot_patch_clients
    _send_slot_patch = _slots_push._send_slot_patch
    ensure_dynamic_card_store = _slots_push.ensure_dynamic_card_store
    set_dynamic_cards_enabled = _slots_push.set_dynamic_cards_enabled
    notify_dashboard_card = _slots_push.notify_dashboard_card
    push_session_summary = _slots_push.push_session_summary
    push_artifact_update = _slots_push.push_artifact_update
    push_refresh = _slots_push.push_refresh
    push_update_progress = _slots_push.push_update_progress
    clear_update_progress = _slots_push.clear_update_progress
    _broadcast = _slots_push._broadcast
    _spawn_ws_send = _slots_push._spawn_ws_send
    _drop_if_stalled = _slots_push._drop_if_stalled
    _on_ws_send_done = _slots_push._on_ws_send_done
    _ws_client_allowed = _slots_push._ws_client_allowed
    _serialize_for_client = _slots_push._serialize_for_client
    _serialize_subagent_batch = _slots_push._serialize_subagent_batch
    _send_ws_all = _slots_push._send_ws_all
    _send_ws_owners = _slots_push._send_ws_owners
    broadcast_ws = _slots_push.broadcast_ws
    broadcast_context_usage = _slots_push.broadcast_context_usage
    deliver_ws_owners = _slots_push.deliver_ws_owners
    broadcast_ws_owners = _slots_push.broadcast_ws_owners
    ws_client_count = _slots_push.ws_client_count
    dashboard_user_ws_count = _slots_push.dashboard_user_ws_count
    broadcast_browser_event = _slots_push.broadcast_browser_event
    register_ws = _slots_push.register_ws
    send_members_subscribed = _slots_push.send_members_subscribed
    unregister_ws = _slots_push.unregister_ws
    _remove_ws = _slots_push._remove_ws
    subscribe_logs = _slots_push.subscribe_logs
    unsubscribe_logs = _slots_push.unsubscribe_logs
    subscribe_subagents = _slots_push.subscribe_subagents
    unsubscribe_subagents = _slots_push.unsubscribe_subagents
    broadcast_ws_subagent_subscribers = _slots_push.broadcast_ws_subagent_subscribers
    close_all_ws = _slots_push.close_all_ws

    # The partial slot-list hold state machine (state_parts/slot_list_hold.py).
    begin_slot_list_rebuild = _slot_list_hold.begin_slot_list_rebuild
    replace_channel_slot_holds = _slot_list_hold.replace_channel_slot_holds
    slot_list_held_keys = _slot_list_hold.slot_list_held_keys
    release_deleted_slot_keys = _slot_list_hold.release_deleted_slot_keys
    slot_list_hold_channels = _slot_list_hold.slot_list_hold_channels
    arm_slot_list_rebuild_deadline = _slot_list_hold.arm_slot_list_rebuild_deadline
    _expire_slot_list_rebuild = _slot_list_hold._expire_slot_list_rebuild
    _persistent_slot_list_hold = _slot_list_hold._persistent_slot_list_hold
    _complete_slot_list_rebuild_if_ready = _slot_list_hold._complete_slot_list_rebuild_if_ready
    finish_slot_list_rebuild = _slot_list_hold.finish_slot_list_rebuild


own_methods(DashboardState)
