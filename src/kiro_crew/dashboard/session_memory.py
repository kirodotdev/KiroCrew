"""Per-session and per-task memory accounting for the dashboard.

Answers "which session/task is using my RAM", the way a task manager does. The
measurement primitives already existed but were never surfaced:

* ``acp.runtime._get_rss_tree_mb`` sums a runtime's whole descendant tree. Summing
  only the runtime pid misses everything: that pid is the sandbox launcher parent
  (small, parked in ``waitpid``) while the kiro-cli that accumulates GBs is a
  child. It was called only to decide runtime recycling, and only for the shared
  ``_bg`` runtime — chat-session runtimes were never measured at all. This module
  hands it the descendant set it has already walked, so the tree is not walked a
  second time for the total.
* ``subagent.SubagentManager`` samples per-task RSS/CPU on its reaper sweep for
  learned sizing, and nothing read those numbers back out.

This module owns the ``/proc`` work (so ``SessionManager`` stays free of it, which
also keeps ``session.py`` from importing ``subagent.py`` — that edge already runs
the other way) and holds the two pieces of state a point-in-time sampler cannot
derive from one observation: the CPU jiffy baseline, and a short load history.

**RSS is an upper bound.** Summing per-process RSS counts shared pages (libc, the
Python runtime) once per process in the tree. PSS would attribute them
proportionally, but ``/proc/<pid>/smaps_rollup`` requires ``PTRACE_MODE_READ`` and
is denied for sandboxed children, so RSS is what is actually obtainable here.
Callers must present it as a ceiling, not an exact figure.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Callable, Optional

from kiro_crew.acp.runtime import _get_rss_tree_mb, _iter_descendant_pids
from kiro_crew.dashboard.handlers_system import _get_static_system_info
from kiro_crew.dashboard.state import NEW_SESSION_TITLE
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.platform_compat import proc_child_map, process_matches
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.subagent import _CLK_TCK, _subtree_cpu_jiffies

if TYPE_CHECKING:  # pragma: no cover — typing only
    from kiro_crew.crew_log.session_tree import SessionTree, TreeNode
    from kiro_crew.session import SessionManager
    from kiro_crew.subagent import SubagentManager

logger = logging.getLogger(__name__)

# Samples retained for the load sparkline. At the dashboard's poll cadence this
# is a rolling window, not a durable series: it is deliberately in-memory only —
# a memory-usage graph is not worth a disk write per poll, and it re-fills within
# one window after a restart.
_HISTORY_LEN = 60

# Marker identifying an MCP stub process inside a session's tree. Stubs are the
# per-server shims a session spawns, so their count is the useful "how many MCP
# servers is this session carrying" signal. Imported rather than spelled out so
# it cannot drift from the launch line the rewriter emits.
_STUB_MARKER = STUB_MODULE


def _spend_for_session(
    spend: dict,
    key: object,
    spend_slot_by_session: Optional[dict[str, str]],
) -> Optional[dict]:
    """The spend row for a session key, or None when nothing was recorded.

    Two lookups, in order:

    1. **Direct.** ``slot_spend`` already files an ordinary dashboard slot under
       its session-key form, so most rows hit here.
    2. **Via the slot alias.** A slot bound to a channel or cron conversation runs
       its turns under ``linked_session_key`` while its usage rows still carry the
       dashboard slot key, so the two keys are unrelated strings and the direct
       lookup cannot match. ``spend_slot_by_session`` supplies that slot key, and
       ``spend_key_for_slot`` — the one owner of the shard-key rule — converts it.

    Returning None is meaningful: the payload contract says ``credits``/``turns``
    of null mean "no measured turn in the window", which is not zero.

    Every value crossing in from the caller is type-checked, not merely truth-
    checked. ``spend_slot_by_session`` is supplied by the handler and is a
    ``MagicMock`` in much of the existing suite; a mock's ``.get()`` returns
    another truthy mock, so a bare ``if not slot_key`` admits it and the regex in
    ``spend_key_for_slot`` raises ``TypeError``. The rest of this module already
    guards with ``isinstance`` for the same reason.
    """
    if not isinstance(key, str):
        return None
    row = spend.get(key) if isinstance(spend, dict) else None
    if isinstance(row, dict):
        return row
    if not isinstance(spend_slot_by_session, dict):
        return None
    slot_key = spend_slot_by_session.get(key)
    if not isinstance(slot_key, str) or not slot_key:
        return None
    from kiro_crew.dashboard.handlers.usage import spend_key_for_slot

    aliased = spend.get(spend_key_for_slot(slot_key))
    return aliased if isinstance(aliased, dict) else None


def _bare_slot_key(key: str) -> str:
    """The slot key a crew log records for session *key*.

    A dashboard session's key is ``dashboard:{slot.key}`` (see
    :func:`session_title`), and its crew log -- and any child's ``parent.slot``
    citing it -- carries the bare ``slot.key``. Every other session key is its
    own slot spelling.
    """
    if key.startswith("dashboard:"):
        return key[len("dashboard:") :]
    return key


def live_sids(rows: Iterable[dict[str, object]]) -> list[str]:
    """The ACP session ids of *rows*, for the scan's ``preferred`` list.

    The rows on screen are what a lineage scan is folded for, so their own logs
    are admitted before the store's order (see :class:`SessionTree`). A row
    carrying no ``sid`` contributes nothing rather than an empty string, which
    would address no unit.
    """
    return [sid for sid in (row.get("sid") for row in rows) if isinstance(sid, str) and sid]


def lineage_parents(
    rows: list[dict[str, object]],
    nodes: "Mapping[str, TreeNode]",
    spend_slot_by_session: Optional[dict[str, str]] = None,
) -> dict[str, Optional[dict[str, object]]]:
    """Per live row, the ``parent`` it carries on the wire -- ``{slot, key}`` or ``None``.

    ONE implementation of the join, used by both the Sessions table's memory payload
    and the dashboard slot payload the chat sidebar's conductor lane nests on
    (``state._attach_slot_parents``). Two implementations would let the two views nest
    the same gateway differently, which is the one thing a reader comparing them cannot
    recover from: neither view says which is right.

    The join is by SLOT, never by pid or title, and a row is reachable under
    three spellings of its slot because three writers spell it differently. A
    crew log names a slot the way ``session_create`` attributed it -- the bare
    ``slot.key`` for a dashboard session -- while the row's key is the full
    ``dashboard:`` session key. A slot bound to a channel or cron conversation
    runs its turns under ``linked_session_key`` while its log still carries the
    dashboard slot key, so *spend_slot_by_session* (the alias
    :func:`_spend_for_session` already bridges for credits) is the third.

    *nodes* is the projection's fold, and each caller passes the one it reads: the
    memory payload takes :meth:`SessionMemorySampler._lineage` (whose companion flag
    is specifically ``over_cap``), and the slot payload takes
    ``projection().nodes()`` directly. The join itself needs no completeness flag, so
    it takes the nodes alone rather than a whole reading -- which is also what keeps it
    from deciding a question that belongs to its caller.

    An empty *nodes* means no row has a creator (the crew log is off, or nothing
    on disk cites one), and the storage package stays UNIMPORTED on that path:
    ``parent_payload`` is imported below the early return, so a flag-off boot
    never loads it. Existing tests pin that.
    """
    if not nodes:
        return {}
    from kiro_crew.crew_log.session_tree import parent_payload

    def slot_spellings(row_key: str) -> list[str]:
        spellings = [row_key, _bare_slot_key(row_key)]
        if isinstance(spend_slot_by_session, dict):
            aliased = spend_slot_by_session.get(row_key)
            if isinstance(aliased, str) and aliased:
                spellings.append(aliased)
        return spellings

    live_key_of: dict[str, str] = {}
    for row in rows:
        row_key = row.get("key")
        if isinstance(row_key, str) and row_key:
            for spelling in slot_spellings(row_key):
                live_key_of.setdefault(spelling, row_key)

    out: dict[str, Optional[dict[str, object]]] = {}
    for row in rows:
        key = row.get("key")
        if not isinstance(key, str) or not key:
            continue
        node = next((nodes[s] for s in slot_spellings(key) if s in nodes), None)
        out[key] = parent_payload(node, live_key_of, key)
    return out


def session_title(key: str, get_slot: Callable[[str], object]) -> dict[str, object]:
    """Resolve a session key to a human title for display.

    A session key is opaque (``dashboard:chat-69-1785905004``); the chat it belongs
    to already has an LLM-generated title, and a list keyed by the raw id is
    unreadable. The mapping is exact — ``state.py`` documents that a slot key
    *becomes* the session key as ``dashboard:{slot.key}``.

    Reads ``slot.display_title`` rather than the persisted value, and redacts here.
    The redaction is load-bearing, not belt-and-braces: most writers of
    ``slot.title`` do redact (the LLM path in
    ``chat_title._generate_title_via_kiro``, the explicit pin in
    ``chat_handlers``, the restore path in ``chat_persistence``,
    ``channel_slots``), but the resume path at ``chat_handlers.py:2645`` assigns a
    client-supplied ``body["title"]`` with no scan at all. Redacting at
    serialization is also what ``running_agents_for`` does for task text — the same
    payload must not treat titles more loosely than task text.

    ``untitled`` is True while a chat has no generated title yet — the caller must
    then disambiguate with ``slot_key``, or every new session renders identically.
    """
    if key == BACKGROUND_KEY:
        return {"title": "Background", "slot_key": "", "untitled": False}
    if not key.startswith("dashboard:"):
        # Slack / cron / app sessions: the key already reads as a name, and there
        # is no chat window to open for them.
        return {"title": key, "slot_key": "", "untitled": False}
    slot_key = key[len("dashboard:") :]
    slot = get_slot(slot_key)
    title = getattr(slot, "display_title", "") if slot is not None else ""
    if not isinstance(title, str) or not title:
        # Slot already evicted (session outlived its dashboard slot).
        return {"title": NEW_SESSION_TITLE, "slot_key": slot_key, "untitled": True}
    untitled = title == NEW_SESSION_TITLE
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    return {"title": title, "slot_key": slot_key, "untitled": untitled}


class SessionMemorySampler:
    """Samples per-session memory, holding only the state a single observation
    cannot provide (CPU baseline + load history)."""

    def __init__(self, history_len: int = _HISTORY_LEN) -> None:
        # pid -> (last jiffies, monotonic ts). CPU is a rate, so it needs two
        # observations; the first sample per pid can only seed the baseline.
        self._cpu_prev: dict[int, tuple[int, float]] = {}
        self._history: deque[tuple[float, float]] = deque(maxlen=history_len)
        # The session tree's scanner and its head cache, built on the first scan
        # that is allowed to run (see ``_lineage``). Owned here so the cache lives
        # as long as the sampler does, one per gateway.
        self._tree: Optional["SessionTree"] = None

    def _lineage(self, rows: list[dict[str, object]]) -> "tuple[dict[str, TreeNode], bool, int]":
        """Who opened whom; whether the store held more session logs than a scan
        admits; and that cap (``TREE_UNIT_CAP``), so the payload can say "N+"
        without this module importing the storage package on a flag-off boot.
        ``({}, False, 0)`` with the crew log off.

        Reads the session-tree PROJECTION, which is folded in memory and advanced by
        the emitter at commit time. So this is a dictionary lookup rather than a scan,
        and it costs the same on a store of four thousand session logs as on one --
        which is what the ``lineage`` entry in ``timings_ms`` reports.

        Blocking only ONCE per process, on the projection's cold start -- a
        checkpoint load plus a tail replay proportional to the delta. It is still
        called from :meth:`_blocking_sample` rather than the coroutine for exactly
        that reason: the first call in a process can touch the disk.

        Reports ``over_cap`` specifically, not the projection's broader
        ``incomplete``: the payload's field is named ``lineage_over_cap`` and a page
        saying "the store is larger than the cap" must not also light up for a
        transient read fault.

        The crew log is optional behind ``KIROCREW_CREW_LOG`` and this module is on
        the dashboard's boot path, so the projection is imported here, lazily, and
        only once the flag says there is a store to read -- the same split the
        emitter and the crew-log routes keep, pinned by the tests that launch with
        the flag unset and assert the package never loaded. Asking the emitter is
        the one import that is safe: it is pure glue and loads nothing until a write
        or a read reaches storage.
        """
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return {}, False, 0
        from kiro_crew.crew_log.session_tree import TREE_UNIT_CAP
        from kiro_crew.crew_log.session_tree_projection import projection

        tree = projection()
        tree.ensure_seeded(tuple(live_sids(rows)))
        return tree.nodes(), tree.over_cap, TREE_UNIT_CAP

    # ── history ────────────────────────────────────────────────────────────
    def record_total(self, total_mb: float, *, now: Optional[float] = None) -> None:
        """Append one total-footprint sample to the rolling window."""
        self._history.append((now if now is not None else time.time(), total_mb))

    def series(self) -> list[dict[str, float]]:
        """The rolling window, oldest first, as ``{"t", "mb"}`` points."""
        return [{"t": ts, "mb": round(mb, 1)} for ts, mb in self._history]

    # ── sampling ───────────────────────────────────────────────────────────
    def _cpu_cores(
        self, pid: int, now: float, *, pids: Optional[list[int]] = None
    ) -> Optional[float]:
        """Cores used since the previous sample for this pid, or None with no
        baseline yet (first observation) -- reported as unknown, never as 0.0.

        ``pids`` is the subtree the caller has already walked, handed on so the
        CPU total is summed over it rather than reached by enumerating the tree
        again. Every figure on the row then describes the same set of processes.
        """
        if sys.platform != "linux":
            return None
        jiffies = _subtree_cpu_jiffies(pid, pids=pids)
        prev = self._cpu_prev.get(pid)
        self._cpu_prev[pid] = (jiffies, now)
        if prev is None:
            return None
        prev_jiffies, prev_ts = prev
        dt = now - prev_ts
        if dt <= 0 or jiffies < prev_jiffies:
            return None
        return (jiffies - prev_jiffies) / (_CLK_TCK * dt)

    def _sample_pid(
        self, pid: int, now: float, *, children: Optional[dict[int, list[int]]] = None
    ) -> dict[str, object]:
        """Blocking per-pid sample. MUST run off the event loop — a session tree
        can be dozens of processes, i.e. dozens of ``/proc`` reads.

        ONE enumeration of the subtree, and every figure on the row is totalled
        over the set it returns: the RSS sum, the process count, the stub count
        and the CPU jiffies. Any of the four could reach its own answer by
        enumerating the tree for itself, and none of them does.

        ``children`` is the host's parent map, built once for the whole poll, and
        it is where the enumeration cost went. Asking the kernel per root reads
        one ``children`` file per THREAD of every process visited: measured on a
        1713-process host sampling 40 session trees of 734 processes, that is
        11296 reads taking 432ms, and the CPU reading's own walk took another
        452ms, against 1717 ``stat`` reads taking 70ms for the map that answers
        every root. Both routes returned the same 40 trees. Without the map (off
        Linux, or ``/proc`` unlistable) the kernel route is walked as before.

        Each process is still READ exactly as it was -- ``VmRSS`` from
        ``status``, utime+stime from ``stat`` -- so no figure on the page changes
        its meaning. What changes is that they now describe one set of processes
        observed once, which is what the shared walker's own docstring says a
        single frontier is for.

        A command line is matched through ``platform_compat.process_matches``,
        which is the helper the cross-platform table names for that question, so
        the stub count asks it the one way this repository asks it.

        **Every figure here is UNCAPPED, and that is the ruling, not an
        oversight.** The shared walker bounds its own frontier at
        ``platform_compat._SUBTREE_MAX_PROCS``; that ceiling guards a looping or
        pathological ``/proc`` graph, which this walk is already immune to for a
        different reason -- it visits each pid at most once. Adopting the number
        here would therefore bound the FIGURE rather than the work, and a bounded
        figure is the worse of the two failures: a truncated count reaches the
        card as a plain integer no consumer can tell from a complete one, so the
        surface presents it as exact, while ``None`` is this payload's way of
        saying UNMEASURABLE. A count that is slow is recoverable; a count that is
        wrong and looks authoritative is not. Should the walk's cost ever need a
        bound, the bound that keeps that meaning intact is a work or time budget
        that yields ``None`` on exhaustion -- never a ceiling that truncates.
        """
        procs: Optional[int] = None
        stubs: Optional[int] = None
        if sys.platform == "linux":
            tree = _iter_descendant_pids(pid, children=children)
            rss_mb = _get_rss_tree_mb(pid, pids=tree)
            procs = len(tree)
            stubs = sum(1 for p in tree if process_matches(p, (_STUB_MARKER,)))
            cpu = self._cpu_cores(pid, now, pids=tree)
        else:
            # No descendant set to reuse: the other platforms reach the total
            # through their own snapshot or validated walk, not a pid list.
            rss_mb = _get_rss_tree_mb(pid)
            cpu = self._cpu_cores(pid, now)
        return {
            "rss_mb": round(rss_mb, 1) if rss_mb is not None else None,
            "procs": procs,
            "mcp": stubs,
            "cpu_cores": cpu,
        }

    def _blocking_sample(self, rows: list[dict[str, object]]) -> dict[str, object]:
        """Sample every distinct pid ONCE, then the machine-wide extras.

        One descendant pass per distinct pid, for the RSS total and the process
        metadata, is the cost bound this function exists to keep, and it can be
        lost in three ways. Per ROW: co-tenants of a
        multiplexed runtime share a pid, so a walk per row reads the same process
        tree N times. Per SAMPLE: the RSS total and the process metadata both come
        off the same set, so reaching them through two helpers walks the tree
        twice — ``_sample_pid`` walks once and hands the set over for exactly this
        reason. Per ROOT: asking the kernel for a tree costs one read per THREAD
        of every process in it, so the host's parent map is built ONCE here and
        every row's tree is derived from it -- one read per host process against
        11296 reads for 40 roots on the measured host. One session's tree is
        dozens of ``/proc`` reads and this whole call
        runs on a browser poll, so another pass over the same pids is the most
        expensive thing that can be added here and the easiest one to add by
        accident: a reader that wants the descendant set must take it from the
        sample, never walk again.

        The credits window and the lineage scan join this offloaded call rather
        than the async one: both touch the filesystem (the shard window, and a
        stat of every session log's directory) and would stall the event loop
        from the coroutine.
        """
        now = time.monotonic()
        out: dict[int, dict[str, object]] = {}
        # One pass over /proc for the whole poll; None off Linux and when /proc
        # cannot be listed, which each row then walks for itself as before.
        proc_started = time.perf_counter()
        children = proc_child_map()
        for row in rows:
            pid = row.get("pid")
            if not isinstance(pid, int):
                continue
            if pid in out:
                continue
            try:
                out[pid] = self._sample_pid(pid, now, children=children)
            except Exception:  # pragma: no cover — a dying pid must not fail the page
                logger.debug("session memory sample failed for pid %s", pid, exc_info=True)
        self._prune_cpu_baselines({r.get("pid") for r in rows})
        proc_ms = (time.perf_counter() - proc_started) * 1000.0
        # circular import: handlers/__init__ imports handlers.sessions,
        # which imports this module
        from kiro_crew.dashboard.handlers.usage import slot_spend

        spend_started = time.perf_counter()
        spend = slot_spend()
        spend_ms = (time.perf_counter() - spend_started) * 1000.0
        lineage_started = time.perf_counter()
        lineage = self._lineage(rows)
        lineage_ms = (time.perf_counter() - lineage_started) * 1000.0

        return {
            "per_pid": out,
            "spend": spend,
            # Who opened whom, folded from the crew logs, and the count of logs
            # the scan left unread past its cap. Here rather than in the coroutine
            # for the same reason as the two above: it lists and stats every
            # session log's directory.
            "lineage": lineage,
            # Where this call's wall time went, per phase, so a latency complaint
            # about this page can be attributed instead of guessed at. Measured
            # around the three blocking phases individually because they have
            # different costs and different fixes: a /proc walk per session tree, a
            # shard-window read, and a directory listing plus a stat per session
            # log. ``perf_counter`` rather than ``monotonic``: this measures short
            # durations, which is the counter's stated purpose.
            "timings_ms": {
                "proc": round(proc_ms, 1),
                "spend": round(spend_ms, 1),
                "lineage": round(lineage_ms, 1),
            },
        }

    def _prune_cpu_baselines(self, live_pids: set[object]) -> None:
        """Drop baselines for pids that are gone, so the dict cannot grow without
        bound across the gateway's lifetime."""
        for pid in [p for p in self._cpu_prev if p not in live_pids]:
            self._cpu_prev.pop(pid, None)

    async def sample(
        self,
        sessions: "SessionManager",
        subagents: "Optional[SubagentManager]" = None,
        get_slot: Optional[Callable[[str], object]] = None,
        spend_slot_by_session: Optional[dict[str, str]] = None,
    ) -> dict[str, object]:
        """Build the full payload: session rows, task rows, totals, history.

        Session sampling runs on the dedicated ``subprocess_executor`` (``mc-subproc``)
        rather than ``asyncio.to_thread``, which would use the DEFAULT executor —
        the pool the event loop also hands ``getaddrinfo`` and every other
        ``run_in_executor(None, ...)`` call. This sampling is browser-triggered on
        a 5s poll and spawns ``ps`` on platforms without ``/proc``, so parking it
        in the shared pool is what let a slow sample stall unrelated requests;
        task rows are read from samples the reaper already took, so they need no
        offload.
        ``get_slot`` resolves display titles (see :func:`session_title`); without
        it rows fall back to their raw keys.

        ``spend_slot_by_session`` maps a session key to the slot key its usage rows
        are filed under (``DashboardState.spend_slot_by_session``). It is only
        load-bearing for a slot bound to a channel or cron conversation, whose
        turns run under ``linked_session_key`` while its spend still carries the
        dashboard slot key — without it those rows report credits as unknown even
        though the spend exists. Omitting it degrades to the direct join.
        """
        total_started = time.perf_counter()
        rows = sessions.runtime_pids()
        samples = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), self._blocking_sample, rows
        )
        per_pid = samples["per_pid"]
        assert isinstance(per_pid, dict)
        spend = samples["spend"]
        assert isinstance(spend, dict)
        phase_ms = samples.get("timings_ms")
        if not isinstance(phase_ms, dict):  # pragma: no cover — defensive
            phase_ms = {}
        lineage_sample = samples["lineage"]
        assert isinstance(lineage_sample, tuple)
        lineage, lineage_over_cap, lineage_cap = lineage_sample
        assert isinstance(lineage, dict)
        assert isinstance(lineage_over_cap, bool)
        assert isinstance(lineage_cap, int)
        now_wall = time.time()

        # Which LIVE row a creator citation lands on, per row. The join lives in
        # ``lineage_parents`` because the dashboard slot payload needs the same
        # answer, and two implementations of it would let the Sessions table and
        # the sidebar's conductor lane nest the same gateway differently.
        parents = lineage_parents(rows, lineage, spend_slot_by_session)

        sessions_out: list[dict[str, object]] = []
        total_mb = 0.0
        counted_pids: set[int] = set()
        # How many live rows report each pid. A multiplexed runtime is measured
        # once but claimed by several sessions, and the card's own tooltip says a
        # ``shared`` row shows "that runtime's measurement divided between them".
        # Without this the promise was false: every co-tenant showed the FULL
        # runtime figure, so N sharers read as N times the memory that exists and
        # any of them could outrank a genuinely large exclusive session.
        sharers: dict[int, int] = {}
        for row in rows:
            row_pid = row.get("pid")
            if isinstance(row_pid, int):
                sharers[row_pid] = sharers.get(row_pid, 0) + 1
        for row in rows:
            pid = row.get("pid")
            sample = per_pid.get(pid) if isinstance(pid, int) else None
            rss = (sample or {}).get("rss_mb")
            cpu = (sample or {}).get("cpu_cores")
            # An even split is an attribution, not a measurement: per-session
            # usage inside one interpreter is not observable from /proc. It is
            # the honest option available, and the ``shared`` badge plus the
            # tooltip say so rather than presenting it as exclusive.
            share = sharers.get(pid, 1) if isinstance(pid, int) else 1
            rss_row = rss / share if isinstance(rss, float) and share > 1 else rss
            cpu_row = cpu / share if isinstance(cpu, float) and share > 1 else cpu
            created = row.get("created_at")
            key = row.get("key")
            spend_row = _spend_for_session(spend, key, spend_slot_by_session)
            named = (
                session_title(key, get_slot)
                if get_slot is not None and isinstance(key, str)
                else {"title": key, "slot_key": "", "untitled": False}
            )
            sessions_out.append(
                {
                    "key": key,
                    "title": named["title"],
                    "slot_key": named["slot_key"],
                    "untitled": named["untitled"],
                    "agent": row.get("agent"),
                    # The grouping dimension for the Sessions table, resolved by
                    # the same function the telemetry metrics use. Deriving it
                    # from the key shape in the frontend instead would create a
                    # second taxonomy that drifts from this one.
                    "channel": telemetry_channel_of(key if isinstance(key, str) else None),
                    "pid": pid,
                    "owns_runtime": row.get("owns_runtime"),
                    "prompts": row.get("prompts"),
                    "rss_mb": rss_row,
                    "procs": (sample or {}).get("procs"),
                    "mcp": (sample or {}).get("mcp"),
                    "cpu_cores": cpu_row,
                    # Cumulative over the credits window, not a rate: credits are
                    # only known per completed turn. null means this slot has no
                    # measured turn in the window, which is not the same as zero.
                    "credits": (round(float(spend_row["credits"]), 3) if spend_row else None),
                    "turns": (int(spend_row["turns"]) if spend_row else None),
                    "uptime_s": (
                        round(now_wall - created, 1) if isinstance(created, float) else None
                    ),
                    # The session that opened this one through session_create, as
                    # its own crew log records it; null for a session nobody
                    # created. ``key`` is the creator's live row when there is
                    # one, which is the edge the Sessions table nests on.
                    "parent": parents.get(key) if isinstance(key, str) else None,
                }
            )
            # Count each runtime ONCE, at its UNDIVIDED size. The split above is
            # per-row attribution; the host total is a measurement, so it must
            # not shrink just because several sessions share one runtime.
            if isinstance(pid, int) and pid not in counted_pids and isinstance(rss, float):
                counted_pids.add(pid)
                total_mb += rss

        tasks_out = subagents.task_memory_rows() if subagents is not None else []
        self.record_total(total_mb, now=now_wall)

        host_total_gb = _get_static_system_info().get("mem_total_gb")
        host_mb = float(host_total_gb) * 1024 if isinstance(host_total_gb, (int, float)) else None
        return {
            "sessions": sessions_out,
            "tasks": tasks_out,
            "totals": {
                "rss_mb": round(total_mb, 1),
                "runtimes": len(counted_pids),
                "host_mb": round(host_mb, 1) if host_mb else None,
                "host_pct": round(total_mb / host_mb * 100, 2) if host_mb else None,
                # Surfaced so the UI can label the number as a ceiling rather
                # than implying exact attribution.
                "rss_is_upper_bound": True,
                # Whether the store held more session logs than the lineage
                # scan admits (TREE_UNIT_CAP, sent beside it so the page can
                # say "N+"). Live rows' logs are admitted first, so what went
                # unread is closed sessions' logs; a live row loses its edge
                # only when it was restarted since it was opened (its parent
                # lives in its older, closed log) or the live rows alone exceed
                # the cap. Said on every payload because a store that large is
                # worth knowing about.
                # False within the cap or with the crew log off (cap 0 then).
                "lineage_over_cap": lineage_over_cap,
                "lineage_cap": lineage_cap,
            },
            "history": self.series(),
            # Per-phase wall time for THIS sample, additive to the payload so an
            # older client ignores it. ``total`` is measured on the coroutine and
            # so includes the executor hop and the row assembly, which the three
            # phase figures do not -- a gap between ``total`` and their sum is the
            # queueing this page shares with every other request, and is itself
            # the answer to a latency question the phases alone cannot settle.
            "timings_ms": {
                "proc": phase_ms.get("proc", 0.0),
                "spend": phase_ms.get("spend", 0.0),
                "lineage": phase_ms.get("lineage", 0.0),
                "total": round((time.perf_counter() - total_started) * 1000.0, 1),
            },
        }
