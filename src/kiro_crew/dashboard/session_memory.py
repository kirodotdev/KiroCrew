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
import os
import sys
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from kiro_crew import resource_status
from kiro_crew.acp.runtime import _get_rss_tree_mb, _iter_descendant_pids
from kiro_crew.dashboard import creator_titles, memory_breakdown
from kiro_crew.dashboard.handlers_system import _get_static_system_info
from kiro_crew.dashboard.state import NEW_SESSION_TITLE
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.platform_compat import (
    darwin_child_pids,
    darwin_process_argv,
    proc_child_map,
    proc_cpu_nanos_for_pid,
    proc_phys_footprint_bytes_for_pid,
    proc_rss_bytes_for_pid,
    process_matches,
)
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

_NANOS_PER_SEC = 1_000_000_000


def _host_children() -> Optional[dict[int, list[int]]]:
    """The host's parent map for this poll, or None where there is none.

    Linux reads ``/proc`` once (:func:`proc_child_map`). macOS walks the kernel's
    own child lists down from launchd (:func:`_darwin_child_map`), in-process.
    Elsewhere (Windows) there is no map: trees are walked per root through the
    validated lineage route, and the figures that need a pid set read as unknown.
    """
    if sys.platform == "linux":
        return proc_child_map()
    if sys.platform == "darwin":
        return _darwin_child_map()
    return None


def _darwin_child_map() -> Optional[dict[int, list[int]]]:
    """Every live process's children on macOS, from launchd down.

    Every process descends from launchd (pid 1), so walking ``proc_listchildpids``
    from it reaches the whole host: one libproc call per process and no
    subprocess, measured at 1297 of the 1298 pids ``ps`` lists (the missing one is
    the kernel, pid 0). A child list that cannot be read is skipped; None only
    when launchd's own list cannot be, which leaves nothing to walk.
    """
    root = darwin_child_pids(1)
    if root is None:
        return None
    children: dict[int, list[int]] = {}
    seen = {1}
    stack: list[tuple[int, list[int]]] = [(1, root)]
    while stack:
        pid, kids = stack.pop()
        if kids:
            children[pid] = kids
        for kid in kids:
            if kid in seen:
                continue
            seen.add(kid)
            grandkids = darwin_child_pids(kid)
            if grandkids is not None:
                stack.append((kid, grandkids))
    return children


def _is_stub(pid: int) -> bool:
    """Whether *pid* is an MCP stub, from its argv.

    macOS reads argv in-process (``sysctl KERN_PROCARGS2``): ``process_matches``
    spawns ``ps`` per pid there, which a tree of dozens of processes on a 5s poll
    cannot afford.
    """
    if sys.platform == "darwin":
        argv = darwin_process_argv(pid)
        return argv is not None and any(_STUB_MARKER in arg for arg in argv)
    return process_matches(pid, (_STUB_MARKER,))


def _subtree_cpu_nanos(pids: list[int]) -> Optional[int]:
    """Total CPU time of *pids* in nanoseconds; None when the root is unreadable.

    A descendant that exited between the snapshot and this read adds nothing,
    which the caller's delta then reads as a drop and reports as unknown.
    """
    if not pids:
        return None
    root = proc_cpu_nanos_for_pid(pids[0])
    if root is None:
        return None
    return root + sum(proc_cpu_nanos_for_pid(p) or 0 for p in pids[1:])


def _tree_memory_mb(tree: list[int]) -> Optional[float]:
    """The memory of a walked tree, in MiB; None when its root cannot be read.

    An unreadable root is "unknown", the answer ``_get_rss_tree_mb`` gives for a
    pid it cannot find, never 0. A descendant that cannot be read adds nothing:
    one that exited after the walk, or one another user owns, whose footprint
    libproc refuses (a ``sudo`` child), so such a tree reads slightly low.
    """
    if not tree:
        return None
    root = _pid_memory_bytes(tree[0])
    if root is None:
        return None
    total = root + sum(_pid_memory_bytes(p) or 0 for p in tree[1:])
    return total / (1024.0 * 1024.0)


def _pid_memory_bytes(pid: int) -> Optional[int]:
    """One process's memory, read the way the session rows read it: the macOS
    footprint, else the per-pid resident set (Linux and Windows)."""
    footprint = proc_phys_footprint_bytes_for_pid(pid)
    if footprint is not None:
        return footprint
    return proc_rss_bytes_for_pid(pid)


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
    every spelling its own writers use, because they do not agree on one. A
    crew log names a slot the way ``session_create`` attributed it -- the key the
    creating caller authenticated as -- while the row's key is whatever its own
    payload is keyed by: the full ``dashboard:`` session key on the Sessions
    table, the bare ``slot.key`` on the slots payload. A slot bound to a channel
    or cron conversation runs its turns under ``linked_session_key``, so its
    session key and its slot key are unrelated strings and only one of them is in
    any given payload.

    *spend_slot_by_session* is that correspondence -- session key to slot key,
    the alias :func:`_spend_for_session` already bridges for credits -- and it is
    read in BOTH directions here, which is what lets one call serve both
    payloads. A Sessions-table row keyed by session finds its slot spelling
    through the map; a slots row keyed by slot finds its session spelling through
    the same map inverted. Each caller therefore passes the SAME map and gets the
    same edges, where a one-directional read left a channel-born conductor
    nesting its workers on one surface and orphaning them on the other.

    *nodes* is the projection's fold, and each caller passes the one it reads: the
    memory payload takes :meth:`SessionMemorySampler._lineage` (whose companion flag
    is specifically ``over_cap``), and the slot payload takes
    ``projection().nodes()`` directly. The join itself needs no completeness flag, so
    it takes the nodes alone rather than a whole reading -- which is also what keeps it
    from deciding a question that belongs to its caller.

    A row the fold holds NO NODE for falls back to the row's OWN ``created_by``, and
    that is the whole of how a just-dispatched session nests. Such a row is a child
    between ``session_create`` and its first turn: its crew log needs an ACP session id
    that arrives with that turn, so nothing in *nodes* can speak for it, and the fold
    alone leaves it at the top level for as long as runtime and MCP startup take.

    GATED on the row's ``lineage_minted``, which is the load-bearing half.
    ``created_by`` is written at birth and REHYDRATED from a metadata line an agent's
    file tools can edit, so alone it is a claim: a fenced agent that wrote a lead's key
    into an archived transcript would otherwise have that tab nested under the lead the
    moment it was revived. ``lineage_minted`` says this gateway process stamped the
    pair at mint -- the same witness the crew log requires before it will record a
    creator in ``session/opened.parent``. A restored slot answers false and nests
    nowhere, which is what base already did.

    A node, once it exists, WINS -- including a node whose ``parent_slot`` is ``None``.
    That absence is not a gap to fill: it is either a session nobody created or one a
    ``session/released`` deliberately let go, and reading ``created_by`` over it would
    put back an edge somebody took away, in the one view a person would look at to
    confirm the release. So the fallback is keyed on the node's EXISTENCE and never on
    the value inside it.

    The edge becomes a payload through the same :func:`parent_payload` a folded node
    goes through, on a node built here, so the two cannot nest the same gateway
    differently: one join, one rule for when the creator's live key is followed.

    *nodes* is handed to :func:`parent_payload` as well, which is what lets a row whose
    own creator is CLOSED nest under the nearest ancestor still open rather than drop to
    the top level -- the lead that owns a run stays the row its conductor's workers hang
    from after the conductor is closed. Such a row carries ``ancestor: True`` and keeps
    ``slot`` on the closed creator, so a surface reads the nesting off ``key`` and the
    citation off ``slot``. Both payloads get it from this one call, so neither view can
    place those workers where the other does not.

    A creator with no live row in this payload -- closed, or never seen running -- is
    the one case the payload cannot name from its own rows, so the edge then carries
    the creator's last-known ``title`` when :mod:`~kiro_crew.dashboard.creator_titles`
    remembers one, and omits it otherwise. The titles are remembered here, from every
    live row this join sees, which is why neither caller reads the disk for them.

    Only the slots payload carries these two fields (``slot_projection.serialize_slot``
    writes them). The Sessions table's rows come from the session manager's runtime
    map, so they have neither and this fallback is not reached there -- that view keeps
    the folded answer alone, which is the behaviour it had before.

    An empty *nodes* means the crew log is off, or nothing on disk cites a creator,
    and the storage package stays UNIMPORTED on that path: ``parent_payload`` is
    imported below the early return, so a flag-off boot never loads it. Existing tests
    pin that. The mint fallback is not reached there either, and that is the right
    answer rather than a gap: with no folded node anywhere, a creator row would have
    nothing for the child to nest UNDER.
    """
    if not nodes:
        return {}
    from kiro_crew.crew_log.session_tree import parent_payload

    # Built once per call rather than per row: the map is the whole live registry,
    # and the inverse is read for every row.
    forward, inverse = _alias_maps(spend_slot_by_session)

    def slot_spellings(row_key: str) -> list[str]:
        return _slot_spellings(row_key, forward, inverse)

    live_key_of: dict[str, str] = {}
    for row in rows:
        row_key = row.get("key")
        if isinstance(row_key, str) and row_key:
            spellings = slot_spellings(row_key)
            for spelling in spellings:
                live_key_of.setdefault(spelling, row_key)
            # Remembered while the session is LIVE, under every spelling a child's
            # citation can use, so the title is still there once it closes. Only the
            # slots rows carry ``title`` (already redacted by ``slot_projection``); the
            # Sessions table remembers its own in ``sample`` before calling this.
            creator_titles.remember(spellings, row.get("title"), placeholder=NEW_SESSION_TITLE)

    out: dict[str, Optional[dict[str, object]]] = {}
    for row in rows:
        key = row.get("key")
        if not isinstance(key, str) or not key:
            continue
        node = next((nodes[s] for s in slot_spellings(key) if s in nodes), None)
        if node is None:
            node = _minted_node(row, key)
        payload = parent_payload(node, live_key_of, key, nodes)
        if payload is not None:
            _name_closed_creator(payload, live_key_of)
        out[key] = payload
    return out


def _name_closed_creator(payload: dict[str, Any], live_key_of: Mapping[str, str]) -> None:
    """Add the CLOSED creator's last-known ``title`` to *payload*, IN PLACE, when known.

    Only for a creator with no live row: a live creator is named by its own row in the
    same payload, and two names for one session could disagree. "Not live" is read off
    the same *live_key_of* the join just used, under both spellings of the cited slot,
    so a creator this payload nests a child under never gets one -- whatever
    ``parent_payload`` decided about the edge (a cycle and a self-citation answer
    ``key: None`` for a creator that IS live, and they must not be named as closed).

    Omitted, never ``None``, when no title is remembered, so an edge this cannot name
    is the payload it has always been and the surface keeps its generic label.
    """
    cited = payload.get("slot")
    if not isinstance(cited, str) or not cited:
        return
    spellings = [cited, _bare_slot_key(cited)]
    if any(s in live_key_of for s in spellings):
        return
    title = creator_titles.last_known(spellings)
    if title is not None:
        payload["title"] = title


def _alias_maps(spend_slot_by_session: object) -> tuple[dict[str, str], dict[str, str]]:
    """Session key -> slot key as given, and slot key -> session key inverted.

    Last writer wins in the inverse, matching the forward map's own documented rule
    for two slots claiming one session identity. A slot has exactly one effective
    session key, so the collision this can produce is the same one the forward map
    already carries rather than a new one.
    """
    forward: dict[str, str] = {}
    inverse: dict[str, str] = {}
    if isinstance(spend_slot_by_session, dict):
        for session_key, slot_key in spend_slot_by_session.items():
            if isinstance(session_key, str) and isinstance(slot_key, str):
                if session_key and slot_key:
                    forward[session_key] = slot_key
                    inverse[slot_key] = session_key
    return forward, inverse


def _slot_spellings(
    row_key: str, forward: Mapping[str, str], inverse: Mapping[str, str]
) -> list[str]:
    """Every key a crew-log citation can use for the row keyed *row_key*."""
    spellings = [row_key, _bare_slot_key(row_key)]
    for extra in (forward.get(row_key), inverse.get(row_key)):
        if isinstance(extra, str) and extra:
            spellings.append(extra)
    return spellings


def remember_slot_title(row_key: str, title: object, spend_slot_by_session: object = None) -> None:
    """Remember one LIVE row's already-redacted *title* under every citation spelling.

    The per-slot counterpart of what :func:`lineage_parents` does for a whole payload,
    for a title published without a full frame (``push_slot_patch``): a rename that
    reaches the sidebar as a patch must also be the name a closed-creator label reads.
    """
    if not isinstance(row_key, str) or not row_key:
        return
    forward, inverse = _alias_maps(spend_slot_by_session)
    creator_titles.remember(
        _slot_spellings(row_key, forward, inverse), title, placeholder=NEW_SESSION_TITLE
    )


def _minted_node(row: dict[str, object], key: str) -> "TreeNode | None":
    """A stand-in node from the row's own mint witness, or ``None``.

    Reached only when no spelling of the row's key is in the fold, which is a child
    that has not opened a log yet.

    Both fields are required and both are judged HERE rather than at the call site, so
    one place decides what counts as a witnessed creator. ``lineage_minted`` false
    covers every row that is not one: a person's own tab, a fork, and -- the case that
    matters -- a slot restored after a restart, whose ``created_by`` came back from an
    agent-editable transcript and is therefore a claim rather than this gateway's own
    record.
    """
    if not row.get("lineage_minted"):
        return None
    creator = row.get("created_by")
    if not isinstance(creator, str) or not creator or creator == key:
        # A blank names nobody, and a row naming itself is not an edge.
        return None
    from kiro_crew.crew_log.session_tree import TreeNode as _TreeNode

    # ``cycle`` is false because a slot with no log of its own is cited by nothing, so
    # it can lie on no cycle of citations.
    return _TreeNode(slot=key, parent_slot=creator, cycle=False)


def session_title(key: str, get_slot: Callable[[str], object]) -> dict[str, object]:
    """Resolve a session key to a human title for display.

    A session key is opaque (``dashboard:chat-69-1785905004``); the chat it belongs
    to already has an LLM-generated title, and a list keyed by the raw id is
    unreadable. The mapping is exact — ``state_parts/slot_directory.py`` documents that a slot key
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


#: Consecutive faulted samples before the ownership SLI calls a fault confirmed.
#: TWO, not one: the claim set is read on the event loop and the slice is walked
#: in the executor, so the two halves describe different instants and ordinary
#: churn produces a one-sample fault -- a session spawning between them is in the
#: slice and not in the claims, one exiting between them is claimed and not alive.
#: Neither is a leak. A real leak does not heal, so it survives the next poll,
#: while churn does not; two samples is the smallest window that tells them apart.
#: At the page's 5s poll that is about ten seconds to a confirmed alarm.
_OWNERSHIP_CONFIRM_SAMPLES = 2

#: The manager accessors whose pids count as claimed, beyond the session rows
#: themselves. Named rather than inlined so a test can pin this list against
#: ``session_cleanup._active_pids``: a claim source present there and missing
#: here turns a well-owned runtime into a reported leak.
_CLAIM_SOURCES = ("_pool_pids", "_in_flight_pids", "_companion_runtime_pids")


def _claimed_pids(sessions: "SessionManager", rows: Iterable[Mapping[str, object]]) -> set[int]:
    """Every pid something claims as a runtime it owns.

    The SAME union the periodic sweep protects with
    (``session_cleanup._active_pids``): the session rows themselves, plus the
    warm pool, plus runtimes whose spawn is still in flight, plus companion
    (sub-agent) runtimes. Assembled from the manager's own accessors rather than
    by calling the sweep's private helper, so the two readers stay independent --
    but the sources must stay the same four, because a source missing HERE shows
    up as a leaked process that is in fact perfectly well owned.
    ``test_the_claim_union_matches_the_sweeps_own_sources`` pins the two equal.

    In-memory only: every accessor reads a dict the manager already holds, so
    this is safe on the event loop. The FILESYSTEM half of the ownership
    question (enumerating the slice, testing each pid) is deliberately not here
    -- it runs in :meth:`SessionMemorySampler._blocking_sample`.

    An accessor that is absent or raises is skipped rather than fatal: a claim
    source this cannot read would otherwise turn into a false leak report, which
    is the one way an SLI whose whole purpose is "non-zero means investigate"
    becomes noise. A partial claim set can still over-report, so the skip is
    logged.
    """
    claimed: set[int] = set()
    for row in rows:
        pid = row.get("pid")
        if isinstance(pid, int):
            claimed.add(pid)
    for pids in _claim_source_pids(sessions).values():
        claimed.update(pids)
    return claimed


def _claim_source_pids(sessions: "SessionManager") -> dict[str, set[int]]:
    """Each claim source's pids, by accessor name; an unreadable one is empty.

    The ownership SLI unions them (:func:`_claimed_pids`) and the memory
    breakdown keeps them apart, the warm pool being its own category.
    """
    out: dict[str, set[int]] = {}
    for accessor in _CLAIM_SOURCES:
        out[accessor] = set()
        probe = getattr(sessions, accessor, None)
        if probe is None:
            logger.debug("slice ownership: claim source %s absent", accessor)
            continue
        try:
            out[accessor] = {int(pid) for pid in probe()}
        except Exception:
            logger.debug("slice ownership: claim source %s failed", accessor, exc_info=True)
    return out


@dataclass(frozen=True)
class _BreakdownInputs:
    """What the memory breakdown needs from the event loop, all in-memory reads.

    ``daemon_pid`` is the MCP gateway daemon this gateway spawned, if it did;
    ``daemon_socket`` is the socket an adopted daemon is asked on
    (:func:`memory_breakdown.mcp_daemon_roots`).
    """

    warm_pool: frozenset[int]
    other_agents: frozenset[int]
    daemon_pid: Optional[int]
    daemon_socket: Optional[Path]
    gateway_pid: int
    host_mb: Optional[float]


def _measure_breakdown(
    inputs: _BreakdownInputs,
    per_pid: Mapping[int, Mapping[str, object]],
    children: Optional[dict[int, list[int]]],
) -> Optional[dict[str, object]]:
    """The whole-product breakdown for this poll, or None when it failed. BLOCKING.

    The sessions figure is the rows' own total, summed from the samples the rows
    are built from, so the panel and the table cannot disagree about it. A failed
    breakdown leaves the page as it was, with the sessions total alone.
    """
    sessions_mb = 0.0
    for sample in per_pid.values():
        rss = sample.get("rss_mb")
        if isinstance(rss, float):
            sessions_mb += rss
    try:
        roots = memory_breakdown.Roots(
            sessions=frozenset(per_pid),
            warm_pool=inputs.warm_pool,
            other_agents=inputs.other_agents,
            mcp_gateway=memory_breakdown.mcp_daemon_roots(inputs.daemon_pid, inputs.daemon_socket),
        )
        return memory_breakdown.build(
            roots,
            sessions_mb=sessions_mb,
            gateway_pid=inputs.gateway_pid,
            children=children,
            memory_bytes=_pid_memory_bytes,
            tree_mb=_get_rss_tree_mb,
            desktop=memory_breakdown.own_desktop_shell_matcher(),
            host_mb=inputs.host_mb,
        )
    except Exception:
        logger.debug("memory breakdown failed", exc_info=True)
        return None


def _breakdown_inputs(
    sources: Mapping[str, set[int]],
    tasks: Iterable[Mapping[str, object]],
    mcp_gateway: object,
    host_mb: Optional[float],
) -> _BreakdownInputs:
    """Collect the breakdown's roots on the event loop. In-memory reads only.

    ``mcp_gateway`` is the dashboard's ``GatewayManager`` (or None). Its process
    handle is read for the pid of a daemon it spawned; its socket path is where an
    adopted daemon is asked for its pid, later, off the loop.
    """
    other: set[int] = set(sources.get("_in_flight_pids", ()))
    other.update(sources.get("_companion_runtime_pids", ()))
    for task in tasks:
        pid = task.get("pid")
        if isinstance(pid, int):
            other.add(pid)
    daemon_pid: Optional[int] = None
    daemon_socket: Optional[Path] = None
    if mcp_gateway is not None:
        process = getattr(mcp_gateway, "_process", None)
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and getattr(process, "returncode", 0) is None:
            daemon_pid = pid
        socket_path = getattr(mcp_gateway, "socket_path", None)
        if isinstance(socket_path, (str, Path)):
            daemon_socket = Path(socket_path)
    return _BreakdownInputs(
        warm_pool=frozenset(sources.get("_pool_pids", ())),
        other_agents=frozenset(other),
        daemon_pid=daemon_pid,
        daemon_socket=daemon_socket,
        gateway_pid=os.getpid(),
        host_mb=host_mb,
    )


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
        # Consecutive polls whose ownership reading was faulted. Lives on the
        # sampler because confirmation needs memory the probe itself must not
        # have: ``slice_ownership`` is a pure function of one instant, and one
        # instant cannot tell a leak from churn.
        self._ownership_faults = 0

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
        the flag off and assert the package never loaded. Asking the emitter is
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

        Linux counts jiffies from ``/proc``. Elsewhere the counter is each
        process's CPU time in nanoseconds (``proc_cpu_nanos_for_pid``), which
        needs the walked subtree; without one the figure is unknown.
        """
        ticks: Optional[int]
        if sys.platform == "linux":
            ticks = _subtree_cpu_jiffies(pid, pids=pids)
            per_sec: float = _CLK_TCK
        elif pids is not None:
            ticks = _subtree_cpu_nanos(pids)
            per_sec = _NANOS_PER_SEC
        else:
            return None
        if ticks is None:
            self._cpu_prev.pop(pid, None)
            return None
        prev = self._cpu_prev.get(pid)
        self._cpu_prev[pid] = (ticks, now)
        if prev is None:
            return None
        prev_ticks, prev_ts = prev
        dt = now - prev_ts
        if dt <= 0 or ticks < prev_ticks:
            return None
        return (ticks - prev_ticks) / (per_sec * dt)

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
        every root. Both routes returned the same 40 trees. Without the map (on
        Windows, or ``/proc`` unlistable) each root is walked for itself.

        Each process is still READ exactly as it was -- ``VmRSS`` from
        ``status``, utime+stime from ``stat`` -- so no figure on the page changes
        its meaning. What changes is that they now describe one set of processes
        observed once, which is what the shared walker's own docstring says a
        single frontier is for.

        A stub is recognised from its command line (:func:`_is_stub`): through
        ``platform_compat.process_matches``, the helper the cross-platform table
        names for that question, except on macOS, where that helper spawns ``ps``
        per pid and argv is read in-process instead.

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
            stubs = sum(1 for p in tree if _is_stub(p))
            cpu = self._cpu_cores(pid, now, pids=tree)
        elif children is not None:
            # macOS: the memory total is summed over the same walked tree, so the
            # four figures describe one process set and no ``ps`` is spawned.
            tree = _iter_descendant_pids(pid, children=children)
            rss_mb = _tree_memory_mb(tree)
            procs = len(tree)
            stubs = sum(1 for p in tree if _is_stub(p))
            cpu = self._cpu_cores(pid, now, pids=tree)
        else:
            # No parent map (Windows): the total comes from the validated
            # lineage walk, which yields no pid set to count or time.
            rss_mb = _get_rss_tree_mb(pid)
            cpu = self._cpu_cores(pid, now)
        return {
            "rss_mb": round(rss_mb, 1) if rss_mb is not None else None,
            "procs": procs,
            "mcp": stubs,
            "cpu_cores": cpu,
        }

    def _blocking_sample(
        self,
        rows: list[dict[str, object]],
        claimed_pids: Iterable[int] = (),
        breakdown: "Optional[_BreakdownInputs]" = None,
    ) -> dict[str, object]:
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

        ``breakdown`` asks for the whole-product memory breakdown on the same
        parent map (:mod:`kiro_crew.dashboard.memory_breakdown`); it reads every
        process it attributes, so it runs here with the rest of the host reads.
        """
        now = time.monotonic()
        out: dict[int, dict[str, object]] = {}
        # One pass over the process table for the whole poll; None on Windows and
        # when /proc cannot be listed, which each row then walks for itself.
        proc_started = time.perf_counter()
        children = _host_children()
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
        breakdown_started = time.perf_counter()
        breakdown_out = (
            _measure_breakdown(breakdown, out, children) if breakdown is not None else None
        )
        breakdown_ms = (time.perf_counter() - breakdown_started) * 1000.0
        # circular import: handlers/__init__ imports handlers.sessions,
        # which imports this module
        from kiro_crew.dashboard.handlers.usage import slot_spend

        spend_started = time.perf_counter()
        spend = slot_spend()
        spend_ms = (time.perf_counter() - spend_started) * 1000.0
        lineage_started = time.perf_counter()
        lineage = self._lineage(rows)
        lineage_ms = (time.perf_counter() - lineage_started) * 1000.0
        # The ownership SLI's filesystem half: enumerating the slice walks every
        # descendant cgroup's ``cgroup.procs``, and each claimed pid is tested for
        # liveness. Both belong on this side of the executor hop for the reason
        # the whole function exists -- the coroutine that calls it runs on the
        # gateway's event loop, on a 5s browser poll.
        ownership_started = time.perf_counter()
        slice_ownership = resource_status.slice_ownership(claimed_pids).as_dict()
        ownership_ms = (time.perf_counter() - ownership_started) * 1000.0

        return {
            "per_pid": out,
            "spend": spend,
            # Who opened whom, folded from the crew logs, and the count of logs
            # the scan left unread past its cap. Here rather than in the coroutine
            # for the same reason as the two above: it lists and stats every
            # session log's directory.
            "lineage": lineage,
            "slice_ownership": slice_ownership,
            "breakdown": breakdown_out,
            # Where this call's wall time went, per phase, so a latency complaint
            # about this page can be attributed instead of guessed at. Measured
            # around the blocking phases individually because they have
            # different costs and different fixes: a /proc walk per session tree, a
            # shard-window read, a directory listing plus a stat per session
            # log, and a cgroup subtree walk. ``perf_counter`` rather than
            # ``monotonic``: this measures short durations, which is the
            # counter's stated purpose.
            "timings_ms": {
                "proc": round(proc_ms, 1),
                "spend": round(spend_ms, 1),
                "lineage": round(lineage_ms, 1),
                "slice_ownership": round(ownership_ms, 1),
                "breakdown": round(breakdown_ms, 1),
            },
        }

    def _confirm_ownership(self, reading: object) -> dict[str, object]:
        """Add ``confirmed`` to an ownership reading, and gate ``healthy`` on it.

        The counts pass through as measured -- they are what the probe saw. What
        this decides is whether they have been seen for long enough to mean
        anything, because the reading's two halves describe different instants
        (see :data:`_OWNERSHIP_CONFIRM_SAMPLES`) and a single faulted sample is
        as likely to be a session starting or stopping mid-poll as a real leak.

        ``confirmed`` is the field a UI or an alert should read. A clean sample
        resets the streak, so churn's one-off faults cannot accumulate across
        unrelated polls into a confirmation they never earned.

        An UNREADABLE slice is neither: it accumulates nothing, and reports
        ``confirmed`` false with ``healthy`` false, because a host whose slice
        cannot be enumerated has not been shown clean and has not been shown
        faulted either.
        """
        if not isinstance(reading, dict):  # pragma: no cover - defensive
            return {"readable": False, "confirmed": False, "healthy": False}
        out = dict(reading)
        if not out.get("readable"):
            self._ownership_faults = 0
            out["confirmed"] = False
            out["healthy"] = False
            return out
        faulted = bool(out.get("unowned_alive")) or bool(out.get("owned_dead"))
        self._ownership_faults = self._ownership_faults + 1 if faulted else 0
        confirmed = self._ownership_faults >= _OWNERSHIP_CONFIRM_SAMPLES
        out["confirmed"] = confirmed
        # A suspected fault reads healthy: the alarm is ``confirmed``, and saying
        # "unhealthy" on a reading this module itself calls transient is the cry
        # of wolf the streak exists to prevent.
        out["healthy"] = not confirmed
        return out

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
        mcp_gateway: object = None,
        breakdown: bool = False,
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

        ``breakdown`` asks for the whole-product memory breakdown in
        ``totals.breakdown`` (null otherwise). It reads every process the product
        runs, so only the Sessions tab's route asks for it; a caller that reads
        the session rows alone does not pay for it. ``mcp_gateway`` is the
        dashboard's ``GatewayManager``, which lets it find the MCP gateway daemon;
        without it that category reads as empty.
        """
        total_started = time.perf_counter()
        rows = sessions.runtime_pids()
        # The claim union is assembled HERE because every source is an in-memory
        # dict on the manager; the ownership probe's filesystem half (enumerating
        # the slice, testing each pid) rides the executor hop below. The sources
        # are read once and serve both the union and the breakdown's roots.
        sources = _claim_source_pids(sessions)
        claimed_pids = {pid for row in rows if isinstance(pid := row.get("pid"), int)}
        for source_pids in sources.values():
            claimed_pids.update(source_pids)
        tasks_out = subagents.task_memory_rows() if subagents is not None else []
        host_total_gb = _get_static_system_info().get("mem_total_gb")
        host_mb = float(host_total_gb) * 1024 if isinstance(host_total_gb, (int, float)) else None
        inputs = _breakdown_inputs(sources, tasks_out, mcp_gateway, host_mb) if breakdown else None
        samples = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), self._blocking_sample, rows, claimed_pids, inputs
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

        # Each row's display name, resolved BEFORE the lineage join so it can be
        # remembered first: a creator that closes later is named by the title it had
        # here (see ``creator_titles``). Remembered under the session key and the slot
        # key ``session_title`` resolved, the two spellings a child's citation uses.
        named_by_key: dict[str, dict[str, object]] = {}
        for row in rows:
            key = row.get("key")
            if not isinstance(key, str):
                continue
            named = (
                session_title(key, get_slot)
                if get_slot is not None
                else {"title": key, "slot_key": "", "untitled": False}
            )
            named_by_key[key] = named
            if get_slot is not None and not named["untitled"]:
                slot_key = named["slot_key"]
                spellings = [key, _bare_slot_key(key)]
                if isinstance(slot_key, str) and slot_key:
                    spellings.append(slot_key)
                creator_titles.remember(spellings, named["title"], placeholder=NEW_SESSION_TITLE)

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
                named_by_key.get(key, {"title": key, "slot_key": "", "untitled": False})
                if isinstance(key, str)
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
                    # How many live sessions this runtime serves, 1 when
                    # exclusive. Published because ``owns_runtime`` answers a
                    # different question -- it is false only on the JOINERS, so a
                    # consumer reading it as "shared?" misses the founder -- and
                    # because an aggregate over rows needs a per-runtime key to
                    # add ``procs``/``mcp`` once instead of once per co-tenant.
                    "sharers": share,
                    "prompts": row.get("prompts"),
                    "rss_mb": rss_row,
                    # NOT divided, unlike rss/cpu two lines away. These count
                    # real objects on the runtime -- descendant processes and MCP
                    # stubs -- and a third of three processes is not a third of a
                    # measurement, it is a number that describes nothing. Each
                    # row states the runtime's true figure; ``sharers`` is what
                    # lets a consumer add it once per runtime.
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

        self.record_total(total_mb, now=now_wall)

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
                # Ownership health of the agent slice: agent processes nothing
                # claims, and claims on pids that are not running. Either above
                # zero is an alarm; see ``resource_status.SliceOwnership``.
                # Published here because this payload is already the page's one
                # per-poll read of live runtime state, so the SLI costs no second
                # poll and lands beside the rows it is about.
                "slice_ownership": self._confirm_ownership(samples["slice_ownership"]),
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
                # Everything the product runs on this host, by category, so the
                # page's total is the figure the OS reports for the app rather
                # than the sessions' share of it. ``rss_mb`` above stays the
                # sessions' own total. Null when the breakdown could not be taken.
                "breakdown": samples.get("breakdown"),
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
                "breakdown": phase_ms.get("breakdown", 0.0),
                "total": round((time.perf_counter() - total_started) * 1000.0, 1),
            },
        }
