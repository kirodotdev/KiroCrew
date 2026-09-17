"""Session health from STRUCTURED state: task rows, slot state, ACP liveness.

A slot is *stalled* when the UI would show it as working but nothing is making
progress and no wait explains the silence. A regex over ``gateway.log`` cannot
decide that -- two error shapes and a 10-minute window cannot tell a turn parked
on a permission prompt from a wedged one -- so the verdict comes from the state
the runtime already keeps, in this order of authority:

1. **Task rows** (``taskq.TaskStore``): queued / retry-wait / waiting /
   recovering counts and ages, per row. A queued task is not stalled; it is
   waiting for capacity, and its age is queue wait, not execution.
2. **Slot state** (``DashboardState._slots``): whether a turn is running, a
   permission future is open, a question is pending, a ``wait`` tool is
   parked, a recovery retry is in flight, children are running for it.
3. **ACP handle liveness** (``AcpSessionHandle``): ``awaiting_permission``,
   the in-flight tool call and its dispatch time, the consumer park, and the
   notification ingress counter -- the progress evidence the in-band watchdog
   itself reads.

Each running slot is classified as exactly one of ``running``, ``queued``,
``waiting_children``, ``waiting_permission``, ``waiting_dependency``,
``waiting_input``, ``recovering`` or ``stalled``, with the evidence that
decided it and the age of that state. Only ``stalled`` is a defect signal; the
waits are legitimate and are shown as waits. A stall is called only after
:data:`STALL_AFTER_SECS` without ANY progress marker moving -- a bounded time,
so a truly wedged turn is flagged, while a long tool call that keeps streaming
(or whose liveness oracle vouches for it) is not.

The log scan survives as a SECONDARY diagnostic (:func:`scan_log_for_stalls`):
a matching line can add evidence to a slot the structured view already shows
as running, and it is the only source for a gateway whose state objects are
not reachable (a standalone probe). It never overrides a structured wait.

Effective caps and the degrade reason come from registered sources
(:meth:`SessionHealthMonitor.register_cap_source` /
:meth:`register_pressure_source`) so the adaptive controller publishes them
without this module importing it. Metrics are sampled on every computation
(``kirocrew.taskq.depth{state}``, ``oldest_wait_secs``,
``effective_cap{lane_kind}``, ``pressure_reason{reason}``); every attribute is
a closed-set value.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Tuple

from kiro_crew.config.paths import config_dir
from kiro_crew.metrics.events import (
    TASKQ_DEPTH,
    TASKQ_EFFECTIVE_CAP,
    TASKQ_OLDEST_WAIT_SECS,
    TASKQ_PRESSURE_REASON,
    emit_counter,
    emit_histogram,
)

logger = logging.getLogger(__name__)

# ── classifications (closed set; API + metric values) ────────────────────────

HEALTH_RUNNING = "running"
HEALTH_QUEUED = "queued"
HEALTH_WAITING_CHILDREN = "waiting_children"
HEALTH_WAITING_PERMISSION = "waiting_permission"
HEALTH_WAITING_DEPENDENCY = "waiting_dependency"
HEALTH_WAITING_INPUT = "waiting_input"
HEALTH_RECOVERING = "recovering"
HEALTH_STALLED = "stalled"

CLASSIFICATIONS: tuple[str, ...] = (
    HEALTH_RUNNING,
    HEALTH_QUEUED,
    HEALTH_WAITING_CHILDREN,
    HEALTH_WAITING_PERMISSION,
    HEALTH_WAITING_DEPENDENCY,
    HEALTH_WAITING_INPUT,
    HEALTH_RECOVERING,
    HEALTH_STALLED,
)
WAITING_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        HEALTH_WAITING_CHILDREN,
        HEALTH_WAITING_PERMISSION,
        HEALTH_WAITING_DEPENDENCY,
        HEALTH_WAITING_INPUT,
    }
)

#: A running slot whose progress markers have not moved for this long, with no
#: wait reason and no liveness verdict vouching for it, is stalled. Ten
#: minutes: wide enough that a slow model turn or a long build step is not
#: flagged, narrow enough that a wedged turn is visible well inside the
#: hours-long turn ceiling that is the only other bound.
STALL_AFTER_SECS = 600.0

#: Legacy log-scan window (secondary diagnostic). Kept equal to the stall
#: threshold so the two sources agree on "recent".
STALL_WINDOW_SECONDS = 600
_LOG_TAIL_BYTES = 256 * 1024

#: Slot keys the health view never reports: internal background slots and cron
#: sessions, whose turns are not a user's waiting conversation.
_INTERNAL_PREFIXES = ("_", "cron_", "cron:")

#: Tools whose in-flight dispatch means the slot is waiting on children, not
#: computing. Closed set, matched on the canonical MCP tool name.
_CHILD_WAIT_TOOLS = frozenset({"spawn_sub_agents", "spawn_run", "wait"})

#: Slot retry counters that mean a recovery cycle is in flight this turn. Each
#: name is a ``_ChatSlot`` attribute chat_runner increments before re-queuing
#: and resets only when a turn lands.
_RECOVERY_COUNTERS: tuple[tuple[str, str], ...] = (
    ("_tool_stall_retries", "tool_stall"),
    ("_stale_recovery_retries", "stale_recover"),
    ("_acp_pipe_death_retries", "pipe_death"),
    ("_prompt_busy_retries", "prompt_busy"),
    ("_transient_5xx_retries", "transient_5xx"),
    ("_compaction_failed_retries", "compaction_failed"),
    ("_infra_retries", "infra_capacity"),
)

#: Task-row states that are waits, mapped to the health classification each is.
#: Exactly ``taskq.model.WAITING``: a LIVE run yielded its lane slot and its
#: runtime is still resident, which is what makes the row a wait rather than
#: work that has not started. ``waiting_infra`` is deliberately absent -- it
#: holds no runtime and is re-dispatched, so it belongs in
#: :data:`TASK_QUEUED_STATES` below and nowhere else.
_TASK_WAIT_STATES: dict[str, str] = {
    "waiting_children": HEALTH_WAITING_CHILDREN,
    "waiting_permission": HEALTH_WAITING_PERMISSION,
    "waiting_dependency": HEALTH_WAITING_DEPENDENCY,
    "waiting_input": HEALTH_WAITING_INPUT,
}

#: Task-row states that read as "waiting for capacity": nothing is executing
#: under the row, no runtime is held, and a dispatcher will pick it up on its
#: own. THE set, shared with ``dashboard.handlers.tasks``' depth summary rather
#: than restated there -- two lists that happen to match are how one surface
#: starts counting a state the other does not. Spelled in literals, because
#: this module duck-types the store and never imports ``kiro_crew.taskq``
#: (``test_dashboard_handlers_lazy_tasks.py`` pins that import graph);
#: ``test_api_tasks.py`` pins the spelling against the ``taskq.model``
#: constants. ``waiting_infra`` is here and not in ``_TASK_WAIT_STATES``: infra
#: capacity is exactly what the row waits for.
TASK_QUEUED_STATES: frozenset[str] = frozenset(
    {"queued", "admitted", "retry_wait", "waiting_infra"}
)
_TASK_RECOVERING_STATES: frozenset[str] = frozenset({"recovering"})

#: Liveness-oracle verdicts that vouch for a silent tool call. Read from the
#: handle when it publishes one; a handle that does not leaves the age rule.
_LIVE_VERDICTS = frozenset({"WORKING", "working"})

# ── legacy log scan (secondary diagnostic) ───────────────────────────────────

_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("subagent_timeout", re.compile(r"Injected timeout error for subagent \S+ into slot (\S+)")),
    ("prompt_stuck", re.compile(r"ACP error in slot (\S+): \[AcpPromptBusy\]")),
    ("prompt_stuck", re.compile(r"ACP error in slot (\S+):.*Prompt already in progress")),
]
_TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) ")


def _read_tail(path: Path, nbytes: int) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with path.open("rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _normalize_slot(raw: str) -> str:
    s = raw.rstrip(":;,.")
    return s.split("dashboard:", 1)[-1] if s.startswith("dashboard:") else s


def _line_age_seconds(line: str, file_mtime: float) -> float:
    m = _TS_RE.match(line)
    if not m:
        return float("inf")
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    line_sod = h * 3600 + mi * 60 + s
    mtime_dt = datetime.fromtimestamp(file_mtime)
    mtime_sod = mtime_dt.hour * 3600 + mtime_dt.minute * 60 + mtime_dt.second
    delta = mtime_sod - line_sod
    if delta < 0:
        delta += 86400
    return delta


def _is_internal(slot_key: str) -> bool:
    return slot_key.startswith(_INTERNAL_PREFIXES)


def scan_log_for_stalls(log_path: Path | None = None, now: float | None = None) -> Dict[str, dict]:
    """SECONDARY diagnostic: ``{slot_key: {reason, since_ts}}`` from the log tail.

    The pre-structured detector, unchanged in what it matches. It is consulted
    only to add evidence to a slot the structured view already shows as
    running, and as the sole source when no state objects are reachable.
    """
    if log_path is None:
        log_path = config_dir() / "gateway.log"
    if not log_path.exists():
        return {}
    if now is None:
        now = time.time()
    try:
        file_mtime = log_path.stat().st_mtime
    except OSError:
        return {}
    tail = _read_tail(log_path, _LOG_TAIL_BYTES)
    if not tail:
        return {}
    out: Dict[str, dict] = {}
    for line in tail.splitlines():
        age_from_mtime = _line_age_seconds(line, file_mtime)
        wall_age = (now - file_mtime) + age_from_mtime
        if wall_age > STALL_WINDOW_SECONDS:
            continue
        for reason, pat in _PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            slot = _normalize_slot(m.group(1))
            if _is_internal(slot):
                continue
            out[slot] = {"reason": reason, "since_ts": file_mtime - age_from_mtime}
    return out


# ── structured snapshot ──────────────────────────────────────────────────────


@dataclass
class SlotSnapshot:
    """The structured facts about one slot, captured ON THE LOOP.

    Plain values only, so the classification can run on a worker thread
    without touching live objects.
    """

    key: str
    running: bool
    pending_approval: bool = False
    question_pending: bool = False
    wait_state: bool = False
    children_running: int = 0
    deliveries_inflight: bool = False
    recovery_kinds: list[str] = field(default_factory=list)
    # ACP handle liveness
    handle_present: bool = False
    turn_active: bool = False
    awaiting_permission: bool = False
    tool_dispatched: bool = False
    inflight_tool_name: str = ""
    inflight_dispatch_age_secs: float | None = None
    parked_for_secs: float = 0.0
    liveness_verdict: str = ""
    # progress markers (any movement is progress)
    progress_marker: tuple[Any, ...] = ()
    last_message_ts: str = ""
    # Harness-native children (kiro-cli ``use_subagent`` / KAS subtasks) seen
    # this turn on the slot's handle: counted, never charged a slot or a row.
    native_children: int = 0


class UnchargedMirror:
    """Gateway-side mirror of ``HostBudget``'s uncharged-residency report.

    The daemon's ``HostBudget`` lives in the gatewayd process; the ACP handles
    live here. ``AcpSessionHandle.report_native_children`` is duck-typed on
    ``report_uncharged(kind, count, label=)``, so the same call feeds this
    mirror and the health payload renders ``uncharged["native_children"]``
    under the same key the daemon snapshot uses. Reporting only: nothing here
    admits, refuses or counts toward a cap.
    """

    _LABEL_CAP = 256

    def __init__(self) -> None:
        self._by_kind: dict[str, dict[str, int]] = {}

    def report_uncharged(self, kind: str, count: int, *, label: str = "") -> None:
        labels = self._by_kind.setdefault(str(kind), {})
        key = str(label or "")
        n = max(0, int(count))
        if n == 0:
            labels.pop(key, None)
        elif key in labels or len(labels) < self._LABEL_CAP:
            labels[key] = n
        if not labels:
            self._by_kind.pop(str(kind), None)

    def uncharged(self, kind: str) -> int:
        return sum(self._by_kind.get(str(kind), {}).values())

    def snapshot(self) -> dict[str, int]:
        return {kind: sum(labels.values()) for kind, labels in self._by_kind.items()}


_UNCHARGED_MIRROR = UnchargedMirror()


def uncharged_mirror() -> UnchargedMirror:
    """The process-wide mirror the slot snapshots report into."""
    return _UNCHARGED_MIRROR


@dataclass
class SlotHealth:
    key: str
    classification: str
    evidence: list[str]
    age_secs: float
    since_ts: float | None = None
    source: str = "slot"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _getattr_soft(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def snapshot_slot(
    slot: Any, *, children_running: int = 0, mono_now: float | None = None
) -> SlotSnapshot:
    """Capture one slot's structured facts. Every read is fail-soft."""
    mono_now = time.monotonic() if mono_now is None else mono_now
    key = str(_getattr_soft(slot, "key", "") or "")
    snap = SlotSnapshot(
        key=key,
        running=bool(_getattr_soft(slot, "turn_running", _getattr_soft(slot, "running", False))),
    )
    futures = _getattr_soft(slot, "_approval_futures", None) or {}
    try:
        snap.pending_approval = any(not f.done() for f in futures.values())
    except Exception:
        snap.pending_approval = False
    snap.question_pending = bool(_getattr_soft(slot, "_question_pending", False))
    snap.wait_state = bool(_getattr_soft(slot, "_wait_state", None))
    snap.children_running = int(children_running)
    snap.deliveries_inflight = bool(_getattr_soft(slot, "_subagent_deliveries_inflight", False))
    for attr, label in _RECOVERY_COUNTERS:
        val = _getattr_soft(slot, attr, 0)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            snap.recovery_kinds.append(f"{label}x{val}")
    messages = _getattr_soft(slot, "messages", None) or []
    try:
        n_messages = len(messages)
        snap.last_message_ts = str(messages[-1].get("ts") or "") if n_messages else ""
    except Exception:
        n_messages = 0
    handle = _getattr_soft(slot, "_acp_client", None)
    ingress = None
    text_chunks = None
    dispatch_ts = None
    if handle is not None:
        snap.handle_present = True
        active = _getattr_soft(handle, "is_turn_active", False)
        try:
            snap.turn_active = bool(active() if callable(active) else active)
        except Exception:
            snap.turn_active = False
        snap.awaiting_permission = bool(_getattr_soft(handle, "awaiting_permission", False))
        snap.tool_dispatched = bool(_getattr_soft(handle, "_tool_dispatched", False))
        inflight = _getattr_soft(handle, "_inflight_tool", None)
        if inflight is not None:
            snap.inflight_tool_name = str(_getattr_soft(inflight, "tool_name", "") or "")
            dispatch_ts = _getattr_soft(inflight, "dispatch_ts", None)
            if isinstance(dispatch_ts, (int, float)):
                snap.inflight_dispatch_age_secs = max(0.0, mono_now - float(dispatch_ts))
        parked = _getattr_soft(handle, "parked_for_secs", None)
        try:
            snap.parked_for_secs = float(parked()) if callable(parked) else 0.0
        except Exception:
            snap.parked_for_secs = 0.0
        verdict = _getattr_soft(handle, "last_liveness_verdict", "")
        snap.liveness_verdict = str(verdict or "")
        report = _getattr_soft(handle, "report_native_children", None)
        if callable(report):
            try:
                snap.native_children = int(report(_UNCHARGED_MIRROR) or 0)
            except Exception:
                snap.native_children = 0
        ingress = _getattr_soft(handle, "_ingress_seq", None)
        stats = _getattr_soft(handle, "last_prompt_stats", None)
        text_chunks = _getattr_soft(stats, "text_chunks", None) if stats is not None else None
    snap.progress_marker = (
        n_messages,
        snap.last_message_ts,
        ingress if isinstance(ingress, int) else None,
        text_chunks if isinstance(text_chunks, int) else None,
        dispatch_ts if isinstance(dispatch_ts, (int, float)) else None,
    )
    return snap


@dataclass
class HealthSnapshot:
    """Everything the classification needs, captured on the loop."""

    slots: list[SlotSnapshot] = field(default_factory=list)
    subagents_cap: int | None = None
    subagents_running: int | None = None
    subagents_window_queued: int | None = None
    wall_now: float | None = None
    mono_now: float | None = None


def _children_by_parent(subagents: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    running = _getattr_soft(subagents, "running", None)
    if not running:
        return out
    try:
        for info in running:
            parent = str(_getattr_soft(info, "parent_session_key", "") or "")
            if parent:
                out[parent] = out.get(parent, 0) + 1
    except Exception:
        return out
    return out


def snapshot_state(
    state: Any, *, now: float | None = None, mono_now: float | None = None
) -> HealthSnapshot:
    """Capture the dashboard state's structured facts (call ON the loop)."""
    wall = time.time() if now is None else now
    mono = time.monotonic() if mono_now is None else mono_now
    snap = HealthSnapshot(wall_now=wall, mono_now=mono)
    if state is None:
        return snap
    subagents = _getattr_soft(state, "subagents", None)
    children = _children_by_parent(subagents) if subagents is not None else {}
    slots = _getattr_soft(state, "_slots", None)
    if isinstance(slots, Mapping):
        for key, slot in list(slots.items()):
            skey = str(key)
            if _is_internal(skey):
                continue
            kids = children.get(skey, 0)
            if not kids:
                # Children are keyed by the EFFECTIVE session key; try it too.
                eff = _getattr_soft(slot, "linked_session_key", "") or ""
                kids = children.get(str(eff), 0) if eff else 0
            try:
                snap.slots.append(snapshot_slot(slot, children_running=kids, mono_now=mono))
            except Exception:
                logger.debug("health snapshot failed for slot %s", skey, exc_info=True)
    if subagents is not None:
        cap = _getattr_soft(subagents, "max_concurrent", None)
        run = _getattr_soft(subagents, "running_count", None)
        queue = _getattr_soft(subagents, "_queue", None)
        snap.subagents_cap = (
            int(cap) if isinstance(cap, int) and not isinstance(cap, bool) else None
        )
        snap.subagents_running = (
            int(run) if isinstance(run, int) and not isinstance(run, bool) else None
        )
        try:
            snap.subagents_window_queued = len(queue) if queue is not None else None
        except Exception:
            snap.subagents_window_queued = None
    return snap


# ── the monitor ──────────────────────────────────────────────────────────────

CapSource = Callable[[], Mapping[str, Any] | None]
PressureSource = Callable[[], str | None]


class SessionHealthMonitor:
    """Classifies snapshots, remembering per-slot progress across samples.

    The progress memo is the one piece of state: a slot's marker tuple and the
    monotonic time it was last seen to change. A marker that has not moved for
    :attr:`stall_after_secs` while no wait explains it is a stall. Memo entries
    are dropped for slots that disappear or stop running.
    """

    def __init__(
        self,
        *,
        stall_after_secs: float = STALL_AFTER_SECS,
        include_log_scan: bool = True,
    ) -> None:
        self.stall_after_secs = float(stall_after_secs)
        self.include_log_scan = include_log_scan
        self._progress: dict[str, tuple[tuple[Any, ...], float]] = {}
        self._cap_sources: dict[str, CapSource] = {}
        self._pressure_sources: list[PressureSource] = []
        self._lock = threading.Lock()

    # ── sources the controller registers ──

    def register_cap_source(self, lane_kind: str, source: CapSource) -> None:
        """Publish an effective cap under ``lane_kind`` (a closed-set name)."""
        with self._lock:
            self._cap_sources[str(lane_kind)] = source

    def unregister_cap_source(self, lane_kind: str) -> None:
        with self._lock:
            self._cap_sources.pop(str(lane_kind), None)

    def register_pressure_source(self, source: PressureSource) -> None:
        """Publish the current degrade reason (None when nothing is degraded)."""
        with self._lock:
            self._pressure_sources.append(source)

    def clear_sources(self) -> None:
        with self._lock:
            self._cap_sources.clear()
            self._pressure_sources.clear()

    # ── classification ──

    def _progress_age(self, snap: SlotSnapshot, mono_now: float) -> float:
        prev = self._progress.get(snap.key)
        if prev is None or prev[0] != snap.progress_marker:
            self._progress[snap.key] = (snap.progress_marker, mono_now)
            return 0.0
        return max(0.0, mono_now - prev[1])

    def classify_slot(
        self, snap: SlotSnapshot, *, mono_now: float, log_hit: Mapping[str, Any] | None = None
    ) -> SlotHealth | None:
        """One classification per running slot; None for an idle slot."""
        if not snap.running and not snap.turn_active:
            self._progress.pop(snap.key, None)
            return None
        age = self._progress_age(snap, mono_now)
        evidence: list[str] = []
        if snap.pending_approval or snap.awaiting_permission:
            evidence.append(
                "approval future open" if snap.pending_approval else "handle awaiting_permission"
            )
            return SlotHealth(snap.key, HEALTH_WAITING_PERMISSION, evidence, age)
        if snap.question_pending:
            return SlotHealth(snap.key, HEALTH_WAITING_INPUT, ["question card pending"], age)
        if snap.children_running and (
            snap.inflight_tool_name in _CHILD_WAIT_TOOLS or snap.deliveries_inflight
        ):
            evidence.append(f"{snap.children_running} child run(s) active")
            if snap.inflight_tool_name:
                evidence.append(f"in {snap.inflight_tool_name}")
            return SlotHealth(snap.key, HEALTH_WAITING_CHILDREN, evidence, age)
        if snap.wait_state:
            return SlotHealth(snap.key, HEALTH_WAITING_DEPENDENCY, ["wait tool parked"], age)
        if snap.recovery_kinds:
            return SlotHealth(
                snap.key,
                HEALTH_RECOVERING,
                [f"retry in flight: {k}" for k in snap.recovery_kinds],
                age,
            )
        if snap.liveness_verdict in _LIVE_VERDICTS:
            evidence.append(f"liveness oracle {snap.liveness_verdict}")
            return SlotHealth(snap.key, HEALTH_RUNNING, evidence, age)
        if age >= self.stall_after_secs:
            evidence.append(f"no progress marker moved for {int(age)}s")
            if snap.tool_dispatched and snap.inflight_dispatch_age_secs is not None:
                evidence.append(
                    f"tool {snap.inflight_tool_name or '?'} dispatched "
                    f"{int(snap.inflight_dispatch_age_secs)}s ago, no result"
                )
            if snap.parked_for_secs > 0:
                evidence.append(f"consumer parked {int(snap.parked_for_secs)}s")
            if log_hit:
                evidence.append(f"log:{log_hit.get('reason', '')}")
            return SlotHealth(
                snap.key, HEALTH_STALLED, evidence, age, since_ts=None, source="slot+handle"
            )
        if log_hit:
            # The structured view says running but the log carries a stall
            # signature within the window: the turn wedged before any marker
            # could stop moving (e.g. a prompt-busy error). Evidence-backed.
            return SlotHealth(
                snap.key,
                HEALTH_STALLED,
                [f"log:{log_hit.get('reason', '')}"],
                age,
                since_ts=log_hit.get("since_ts"),
                source="log",
            )
        if snap.tool_dispatched and snap.inflight_tool_name:
            evidence.append(f"tool {snap.inflight_tool_name} in flight")
        return SlotHealth(snap.key, HEALTH_RUNNING, evidence or ["progress within window"], age)

    # ── the computation ──

    def compute(
        self,
        snapshot: HealthSnapshot,
        *,
        taskq: Any = None,
        log_path: Path | None = None,
        include_log_scan: bool | None = None,
    ) -> dict[str, Any]:
        """Turn a snapshot (+ task store, + optional log tail) into the payload.

        Safe to call on a worker thread: the snapshot is plain data, the store
        is its own thread-safe reader, the log scan is file I/O.
        """
        wall_now = time.time() if snapshot.wall_now is None else snapshot.wall_now
        mono_now = time.monotonic() if snapshot.mono_now is None else snapshot.mono_now
        use_log = self.include_log_scan if include_log_scan is None else include_log_scan
        log_hits: Dict[str, dict] = {}
        if use_log:
            try:
                log_hits = scan_log_for_stalls(log_path=log_path, now=wall_now)
            except Exception:
                logger.debug("log scan failed", exc_info=True)

        slots_out: dict[str, dict[str, Any]] = {}
        stalled: dict[str, dict[str, Any]] = {}
        waiting: list[dict[str, Any]] = []
        recovering: list[dict[str, Any]] = []
        live_keys: set[str] = set()
        for snap in snapshot.slots:
            live_keys.add(snap.key)
            health = self.classify_slot(snap, mono_now=mono_now, log_hit=log_hits.get(snap.key))
            if health is None:
                continue
            slots_out[snap.key] = health.to_payload()
            if snap.native_children:
                slots_out[snap.key]["native_children"] = snap.native_children
            if health.classification == HEALTH_STALLED:
                reason = next(
                    (e[4:] for e in health.evidence if e.startswith("log:")), "no_progress"
                )
                stalled[snap.key] = {
                    "reason": reason,
                    "since_ts": (
                        health.since_ts
                        if health.since_ts is not None
                        else wall_now - health.age_secs
                    ),
                    "evidence": health.evidence,
                    "age_secs": health.age_secs,
                }
            elif health.classification in WAITING_CLASSIFICATIONS:
                waiting.append(
                    {
                        "kind": "slot",
                        "key": snap.key,
                        "reason": health.classification,
                        "age_secs": health.age_secs,
                        "evidence": health.evidence,
                    }
                )
            elif health.classification == HEALTH_RECOVERING:
                recovering.append(
                    {
                        "kind": "slot",
                        "key": snap.key,
                        "age_secs": health.age_secs,
                        "evidence": health.evidence,
                    }
                )
        # Log-only stalls: slots the structured view does not know (no state
        # reachable) keep the legacy reading, tagged with their source.
        if not snapshot.slots:
            for key, hit in log_hits.items():
                stalled[key] = {
                    "reason": hit["reason"],
                    "since_ts": hit["since_ts"],
                    "evidence": [f"log:{hit['reason']}"],
                    "age_secs": max(0.0, wall_now - float(hit["since_ts"])),
                }
        # Forget progress memos for slots that vanished.
        for key in list(self._progress):
            if key not in live_keys:
                self._progress.pop(key, None)

        queued = self._queue_section(taskq, wall_now, waiting, recovering)
        caps = self._effective_caps(snapshot)
        degrade = self._degrade_reason()

        counts = {
            HEALTH_RUNNING: sum(
                1 for s in slots_out.values() if s["classification"] == HEALTH_RUNNING
            ),
            HEALTH_QUEUED: queued["count"],
            "waiting": len(waiting),
            HEALTH_RECOVERING: len(recovering),
            HEALTH_STALLED: len(stalled),
        }
        self._sample_metrics(queued, caps, degrade)
        return {
            "generated_at": wall_now,
            "stalled": stalled,
            "slots": slots_out,
            "waiting": waiting,
            "recovering": recovering,
            "queued": queued,
            "effective_caps": caps,
            "degrade_reason": degrade,
            "uncharged": _UNCHARGED_MIRROR.snapshot(),
            "counts": counts,
            "stall_after_secs": self.stall_after_secs,
            "sources": {
                "slots": bool(snapshot.slots),
                "taskq": bool(queued.get("available")),
                "log": use_log,
            },
        }

    def _queue_section(
        self,
        taskq: Any,
        wall_now: float,
        waiting: list[dict[str, Any]],
        recovering: list[dict[str, Any]],
    ) -> dict[str, Any]:
        section: dict[str, Any] = {
            "available": False,
            "count": 0,
            "oldest_wait_secs": 0.0,
            "by_state": {},
        }
        if taskq is None:
            return section
        try:
            by_state = dict(taskq.count_by_state())
            oldest = float(taskq.oldest_wait_secs())
            rows = list(taskq.active_rows())
        except Exception:
            logger.debug("task store read failed in health", exc_info=True)
            return section
        section["available"] = True
        section["by_state"] = {str(k): int(v) for k, v in by_state.items()}
        section["count"] = sum(int(by_state.get(s, 0)) for s in TASK_QUEUED_STATES)
        section["oldest_wait_secs"] = oldest
        for row in rows:
            state = str(_getattr_soft(row, "state", ""))
            updated = _getattr_soft(row, "updated_at", 0.0) or 0.0
            age = max(0.0, wall_now - float(updated)) if updated else 0.0
            entry = {
                "kind": "task",
                "id": str(_getattr_soft(row, "id", "")),
                "task_kind": str(_getattr_soft(row, "kind", "")),
                "session_key": str(_getattr_soft(row, "session_key", "") or ""),
                "state": state,
                "attempts": int(_getattr_soft(row, "attempts", 0) or 0),
                "next_run_at": _getattr_soft(row, "next_run_at", None),
                "age_secs": age,
            }
            if state in _TASK_WAIT_STATES:
                entry["reason"] = _TASK_WAIT_STATES[state]
                waiting.append(entry)
            elif state in _TASK_RECOVERING_STATES:
                recovering.append(entry)
        return section

    def _effective_caps(self, snapshot: HealthSnapshot) -> dict[str, Any]:
        caps: dict[str, Any] = {}
        if snapshot.subagents_cap is not None:
            caps["subagents"] = {
                "effective": snapshot.subagents_cap,
                "running": snapshot.subagents_running,
                "window_queued": snapshot.subagents_window_queued,
            }
        with self._lock:
            sources = list(self._cap_sources.items())
        for lane, source in sources:
            try:
                value = source()
            except Exception:
                logger.debug("cap source %s failed", lane, exc_info=True)
                continue
            if isinstance(value, Mapping):
                merged = dict(caps.get(lane, {}))
                merged.update(value)
                caps[lane] = merged
        return caps

    def _degrade_reason(self) -> str | None:
        with self._lock:
            sources = list(self._pressure_sources)
        for source in sources:
            try:
                reason = source()
            except Exception:
                logger.debug("pressure source failed", exc_info=True)
                continue
            if reason:
                return str(reason)
        return None

    @staticmethod
    def _sample_metrics(
        queued: Mapping[str, Any], caps: Mapping[str, Any], degrade: str | None
    ) -> None:
        if queued.get("available"):
            for state, n in queued.get("by_state", {}).items():
                emit_histogram(TASKQ_DEPTH, float(n), {"state": state})
            emit_histogram(
                TASKQ_OLDEST_WAIT_SECS, float(queued.get("oldest_wait_secs", 0.0)), {}, unit="s"
            )
        for lane, entry in caps.items():
            eff = entry.get("effective") if isinstance(entry, Mapping) else None
            if isinstance(eff, (int, float)) and not isinstance(eff, bool):
                emit_histogram(TASKQ_EFFECTIVE_CAP, float(eff), {"lane_kind": lane})
        if degrade:
            emit_counter(TASKQ_PRESSURE_REASON, {"reason": degrade})


_default_monitor = SessionHealthMonitor()


def default_monitor() -> SessionHealthMonitor:
    return _default_monitor


def compute_session_health(
    state: Any = None,
    *,
    taskq: Any = None,
    log_path: Path | None = None,
    now: float | None = None,
    monitor: SessionHealthMonitor | None = None,
    snapshot: HealthSnapshot | None = None,
) -> Dict[str, Any]:
    """Snapshot + compute in one call (sync; for tests, doctor, standalone probes).

    The handler takes the snapshot itself on the loop and passes it as
    ``snapshot`` so only the computation runs off-loop; without one this
    convenience snapshots ``state`` inline. ``taskq`` defaults to the subagent
    manager's open store when ``state`` carries one.
    """
    mon = monitor or _default_monitor
    snap = snapshot if snapshot is not None else snapshot_state(state, now=now)
    if taskq is None and state is not None:
        taskq = _getattr_soft(_getattr_soft(state, "subagents", None), "_taskq", None)
    return mon.compute(snap, taskq=taskq, log_path=log_path)


__all__ = [
    "CLASSIFICATIONS",
    "HEALTH_QUEUED",
    "HEALTH_RECOVERING",
    "HEALTH_RUNNING",
    "HEALTH_STALLED",
    "HEALTH_WAITING_CHILDREN",
    "HEALTH_WAITING_DEPENDENCY",
    "HEALTH_WAITING_INPUT",
    "HEALTH_WAITING_PERMISSION",
    "STALL_AFTER_SECS",
    "STALL_WINDOW_SECONDS",
    "HealthSnapshot",
    "SessionHealthMonitor",
    "SlotHealth",
    "SlotSnapshot",
    "compute_session_health",
    "default_monitor",
    "scan_log_for_stalls",
    "snapshot_slot",
    "snapshot_state",
]
