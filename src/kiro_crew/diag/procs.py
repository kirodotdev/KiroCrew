"""Read-only process-tree view of the Kiro Crew process family.

One debug question: *what is running under this install right now, who owns it,
and which of it is orphaned?* Today an agent answers that with an ad-hoc ``ps``
plus a hand-rolled ``/proc`` walk, re-deriving the family and orphan rules each
time -- and sometimes re-deriving them wrongly, because the authority on both
lives in the reaper.

This module reads. It never signals, never writes, and never kills: reclaim
stays entirely with :mod:`kiro_crew.session_scope_reap` and the
:mod:`kiro_crew.session_pid` sweeps. Every ownership and orphan judgement here
is delegated to the function the reaper itself uses, so the view and the reaper
can never disagree about what is ours and what has been abandoned.

Family membership (spec §5), in the order the reasons are attributed:

1. ``descendant`` -- reachable from the gateway process by parent edges.
2. ``marker`` -- carries the ``KIROCREW_SPAWNED`` environment marker.
3. ``marker-chain`` -- its ``ppid`` chain reaches a marker bearer, which is how
   an env-clearing grandchild (a ``chrome-headless`` renderer under a playwright
   daemon) is still recognised as ours.
4. ``cmdline`` -- argv names a family runtime (kiro-cli, kirocrew, kiro_crew,
   playwright, pytest, vitest, node/hyperframes).

Members reparented to init or to a ``systemd --user`` subreaper are *included*,
not filtered: a candidate orphan is the whole point of the view. Reparenting
alone is reported as ``reparented``, because it is only the reaper's candidate
filter -- a service systemd started has the user manager as its parent for its
whole life. ``orphan`` is the verdict, and it additionally requires ownership on
the reaper's terms (the marker, or a ppid chain to a bearer). An unreadable
marker fails closed.

Platform coverage follows the ``stall_enrichment`` precedent -- Linux reads
``/proc`` directly and spawns no subprocess. macOS uses the ``ps`` snapshot in
:mod:`kiro_crew.platform_compat`, which yields parent edges only, so every field
that snapshot cannot carry is reported as ``None`` rather than guessed. Windows
has neither source: the scan returns an empty roster whose ``degraded`` list
says so.

Environment exposure is a four-key allowlist (:data:`ENV_ALLOWLIST`) and nothing
else, and it is omitted altogether unless a caller asks for it. Command lines go
through the canonical credential redactor before they leave this module.

The module is deliberately PURE: it imports no recorder and holds no background
state. :func:`roster_diff` is the history hook the recorder drives; the caller
owns the ring, the cadence and the storage.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import session_pid
from kiro_crew.platform import context as platform_context

# Both of the above are imported as MODULES, not as the names they carry, and
# that is load-bearing twice over. It keeps every ownership and orphan rule
# reached through its owner (``session_pid._is_untracked_managed_agent_orphan``
# reads as the reaper's function, which is the point), and it keeps the
# redaction shim patchable: binding ``redact_via_context`` into this namespace
# at import time would make a test's patch of the shim invisible here, and the
# test that proves every emitted string is redacted works by patching it.

#: Production ``/proc`` root. Every reader takes it as a parameter so a whole
#: fake process table can be handed in from a fixture.
PROC_ROOT = Path("/proc")

#: The ONLY environment keys this module will ever report, in output order.
#: Chosen because each one answers a real debug question about a process --
#: which data home it was launched against, whether it belongs to a pod, and
#: where its scratch and temp writes land -- and none of them is a credential.
#: The gateway can read a same-uid ``/proc/<pid>/environ``; the agent sandbox
#: cannot, which is precisely why this view has to come from the gateway.
ENV_ALLOWLIST: tuple[str, ...] = (
    "KIROCREW_HOME",
    "KIROCREW_POD_ROOT",
    "TMPDIR",
    "KIROCREW_SCRATCH",
)

#: Process kinds, narrow to wide. Classification takes the FIRST match, so the
#: order inside :func:`_classify` matters: a gatewayd command line also matches
#: the generic family-name test.
KINDS: tuple[str, ...] = (
    "gateway",
    "gatewayd",
    "chat",
    "subagent",
    "cron",
    "mcp-server",
    "pod",
    "browser",
    "test",
    "other",
)

#: CPU fraction of one core at or above which a process counts as "pinned" for
#: the GIL hint. Below a full core there is headroom, so contention is not the
#: story the numbers tell.
_GIL_HINT_CPU_PCT = 85.0

#: Threads parked in a futex wait at or above which the hint fires. One waiter
#: is ordinary (a queue read); several at once while a core is pinned is the
#: shape of interpreter-lock contention.
_GIL_HINT_FUTEX_THREADS = 2

# -- retention bounds -------------------------------------------------------
# A cap on how many items are stored bounds memory only when every STORED FIELD
# is bounded too, so these come as a set. Every value below is externally
# controlled: argv, cwd and the environment all belong to the process being
# looked at, not to us, and a whole-host scan meets thousands of them.

#: Most nodes one roster retains. A real host measured 946 family members, so
#: this leaves several times that headroom while still refusing the runaway
#: case -- and a runaway spawn loop is exactly when this view gets run.
MAX_NODES = 4096

#: Longest retained command line. Linux allows a multi-megabyte argv, and the
#: scan holds one per process. Well past argv0 plus the early arguments that
#: :func:`_classify` and the family test actually read, so a truncated line
#: still classifies identically.
MAX_CMDLINE_CHARS = 2048

#: Longest retained path-like value: ``cwd`` and each allowlisted env value.
MAX_PATH_CHARS = 512

#: Longest retained owner label.
MAX_OWNER_CHARS = 128

#: Gateway entry shapes the reaper's own ``_GATEWAY_MARKERS`` does not cover.
#: Those markers name importable modules (``kiro_crew.cli``,
#: ``kiro_crew.__main__``), and the shipped gateway is started as
#: ``python -m kiro_crew gateway`` -- whose argv shows the PACKAGE, never the
#: ``__main__`` module it resolves to. A live gateway was classified ``other``
#: until these were added, which is the one node a tree view must never miss.
_GATEWAY_ENTRY_TOKENS: tuple[bytes, ...] = (
    b"-m kiro_crew ",
    b"kirocrew gateway",
    b"kiro_crew gateway",
)

#: Family argv substrings. Deliberately coarse -- this arm only ADDS candidates
#: whose membership is then reported honestly through ``family_reason``; it
#: never authorizes anything, because nothing in this module authorizes
#: anything.
_FAMILY_CMDLINE_TOKENS: tuple[bytes, ...] = (
    b"kiro-cli",
    b"kirocrew",
    b"kiro_crew",
    b"playwright",
    b"pytest",
    b"vitest",
    b"hyperframes",
)


# -- data model --------------------------------------------------------------


@dataclass(frozen=True)
class ThreadStats:
    """Per-process thread census. ``None`` means "could not be read"."""

    count: int | None = None
    running: int | None = None
    sleeping: int | None = None
    disk_sleep: int | None = None
    futex_wait: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "count": self.count,
            "running": self.running,
            "sleeping": self.sleeping,
            "disk_sleep": self.disk_sleep,
            "futex_wait": self.futex_wait,
        }


@dataclass(frozen=True)
class ProcNode:
    """One family member, as the kernel and the reaper describe it.

    ``cpu_pct`` and ``runq_wait_pct`` are deltas and are therefore ``None`` on a
    first scan: a cumulative counter read once is not a rate. Pass the previous
    roster to :func:`scan` to get them.
    """

    pid: int
    ppid: int | None
    kind: str
    owner: str | None = None
    former_owner: str | None = None
    age_secs: float | None = None
    state: str | None = None
    cpu_pct: float | None = None
    runq_wait_pct: float | None = None
    rss_kb: int | None = None
    swap_kb: int | None = None
    threads: ThreadStats = field(default_factory=ThreadStats)
    fds: int | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    cmdline: str = ""
    marker: bool | None = None
    family_reason: str = "other"
    reparented: bool = False
    orphan: bool = False
    orphan_rule: str = ""
    orphan_since: float | None = None
    orphan_unreachable: bool = False
    gil_saturated_hint: bool = False
    gil_hint_basis: str | None = None

    def as_dict(self, *, include_env: bool = False) -> dict[str, object]:
        """Serialize for a route. ``env`` appears only when asked for."""
        row: dict[str, object] = {
            "pid": self.pid,
            "ppid": self.ppid,
            "kind": self.kind,
            "owner": self.owner,
            "former_owner": self.former_owner,
            "age_secs": _round(self.age_secs, 1),
            "state": self.state,
            "cpu_pct": _round(self.cpu_pct, 1),
            "runq_wait_pct": _round(self.runq_wait_pct, 1),
            "rss_kb": self.rss_kb,
            "swap_kb": self.swap_kb,
            "threads": self.threads.as_dict(),
            "fds": self.fds,
            "cwd": self.cwd,
            "cmdline": self.cmdline,
            "marker": self.marker,
            "family_reason": self.family_reason,
            "reparented": self.reparented,
            "orphan": self.orphan,
            "orphan_rule": self.orphan_rule,
            "orphan_since": self.orphan_since,
            "orphan_unreachable": self.orphan_unreachable,
            "gil_saturated_hint": self.gil_saturated_hint,
            "gil_hint_basis": self.gil_hint_basis,
        }
        if include_env:
            row["env"] = dict(self.env)
        return row


@dataclass
class Roster:
    """One whole-family snapshot, plus the raw counters a later scan needs.

    ``cpu_ticks`` and ``runq_ns`` are cumulative kernel counters kept so the
    NEXT scan can turn them into rates. They are not part of any output.
    """

    ts: float = 0.0
    monotonic: float = 0.0
    platform: str = ""
    gateway_pid: int | None = None
    nodes: dict[int, ProcNode] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    degraded_counts: dict[str, int] = field(default_factory=dict)
    cpu_ticks: dict[int, int] = field(default_factory=dict)
    runq_ns: dict[int, int] = field(default_factory=dict)
    #: ``{pid: start identity}``, the process's ``starttime`` as a string. A pid
    #: is NOT an identity: a busy host recycles numbers between two scans, and
    #: without this a replacement process inherits its predecessor's counters
    #: (a garbage rate whenever the newcomer's cumulative total happens to be
    #: higher) and a real death plus a real birth both go unreported.
    starts: dict[int, str] = field(default_factory=dict)

    def note_repeated(self, category: str) -> None:
        """Count a per-process degradation instead of narrating it once per pid.

        A whole-host scan meets the same failure hundreds of times: on the host
        this was built against, 927 of 946 processes had an unreadable
        ``environ`` from inside an agent sandbox. Appending one line each made
        the degraded list the largest thing in the output, and a route capped at
        64 KB would have spent that cap on repetition rather than on the process
        tree the caller asked for. A count says strictly more in one line.
        """
        self.degraded_counts[category] = self.degraded_counts.get(category, 0) + 1

    def degraded_report(self) -> list[str]:
        """One-off notes, then one counted line per repeated category.

        The count carries no denominator on purpose. Categories are counted over
        different populations -- an argv truncation happens while reading the
        WHOLE host table, before family admission, while an unreadable environ
        is counted per admitted node -- so a single "of N" would be wrong for at
        least one of them, and wrong in the direction that reads as reassuring.
        """
        out = list(self.degraded)
        for category, count in sorted(self.degraded_counts.items()):
            out.append(f"{category}: {count} occurrence(s)")
        return out

    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.kind] = counts.get(node.kind, 0) + 1
        return counts

    def orphan_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.orphan)


# -- small helpers -----------------------------------------------------------


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _stat_fields(proc_root: Path, pid: int) -> list[str] | None:
    """``/proc/<pid>/stat`` fields from ``state`` onward, or ``None``.

    ``comm`` (field 2) is parenthesised and may itself contain spaces and
    ``)``, so the split is on the LAST ``)`` -- the same parse
    :mod:`kiro_crew.session_scope_reap` uses. Index 0 is then ``state``
    (field 3), so field *N* is index *N - 3*.
    """
    raw = _read_text(proc_root / str(pid) / "stat")
    if raw is None:
        return None
    try:
        return raw.rsplit(")", 1)[1].split()
    except IndexError:
        return None


def _status_map(proc_root: Path, pid: int) -> dict[str, str]:
    """``/proc/<pid>/status`` as a ``{label: value}`` map (empty on failure)."""
    raw = _read_text(proc_root / str(pid) / "status")
    if raw is None:
        return {}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        label, sep, value = line.partition(":")
        if sep:
            out[label.strip()] = value.strip()
    return out


def _kib_field(status: Mapping[str, str], label: str) -> int | None:
    """A ``VmRSS: 1234 kB`` style field as an int, or ``None`` when absent.

    Absent is the normal reading for a kernel thread, which has no address
    space at all -- so this must not report 0, which would read as "resident
    but empty".
    """
    raw = status.get(label)
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def _clock_ticks() -> int:
    """``SC_CLK_TCK``, or 0 when this host does not publish it.

    Windows has no ``os.sysconf`` at all. 0 is returned rather than a guessed
    100 because every consumer treats 0 as "cannot compute" and reports
    ``None`` -- a guessed tick rate would silently scale every cpu figure.
    """
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 0
    return ticks if ticks > 0 else 0


def _looks_like_python(cmdline: bytes) -> bool:
    """True when argv suggests a CPython process, which the GIL hint is about.

    Tokenised through the reaper's own :func:`_argv_tokens` / :func:`_basename_of`
    rather than a single space split, so a spaced interpreter path is read as one
    token. The console-script case matters: a venv entry point like ``pytest``
    names the script in argv0, not the interpreter, so a ``.py`` anywhere in argv
    is taken as the signal instead.
    """
    tokens = session_pid._argv_tokens(cmdline)
    if not tokens:
        return False
    basename = session_pid._basename_of(tokens[0]).lower()
    if basename.startswith((b"python", b"pypy")):
        return True
    if basename in (b"pytest", b"py.test"):
        return True
    return any(token.lower().endswith(b".py") for token in tokens)


def _redact(text: str) -> str:
    """Route *text* through the canonical credential redactor.

    A composition failure is deliberately NOT swallowed -- ``redact_via_context``
    re-raises it so a host that cannot compose its redactors fails closed instead
    of emitting raw command lines.
    """
    return platform_context.redact_via_context(text)


def _bounded(text: str, limit: int, roster: Roster, what: str) -> str:
    """*text* cut to *limit* characters, counting the cut so it is never silent.

    Applied where a value is STORED, not where it is rendered: a field the
    roster already holds unbounded has already cost the memory.
    """
    if len(text) <= limit:
        return text
    roster.note_repeated(f"{what} truncated to {limit} chars")
    return text[:limit]


def _decode_cmdline(raw: bytes, roster: Roster) -> str:
    """NUL-separated argv as one redacted, then bounded, printable line.

    ORDER IS THE SECURITY PROPERTY: redact first, bound second, never the
    reverse. An earlier revision cut the value before handing it to the redactor
    to save the redactor walking a long argv. That is a silent credential leak:
    a secret straddling the cut loses the pattern the redactor matches on, so
    the surviving prefix ships un-redacted. Bounding AFTER redaction can only
    ever drop text that has already been scrubbed.
    """
    if not raw:
        return ""
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    return _bounded(_redact(text), MAX_CMDLINE_CHARS, roster, "cmdline")


# -- per-process readers (Linux) ---------------------------------------------


def _thread_stats(proc_root: Path, pid: int, fallback_count: int | None) -> ThreadStats:
    """Thread census from ``/proc/<pid>/task/*``.

    ``futex_wait`` is counted from ``wchan``, which names the kernel function a
    sleeping thread is parked in; threads waiting on a futex are what
    interpreter-lock contention looks like from the kernel's side. When the task
    directory cannot be listed, only the count survives (from ``stat`` field 20)
    and the breakdown is ``None`` rather than zero -- an unread breakdown must
    not read as "no threads are waiting".
    """
    task_dir = proc_root / str(pid) / "task"
    try:
        tids = sorted(entry.name for entry in task_dir.iterdir() if entry.name.isdigit())
    except OSError:
        return ThreadStats(count=fallback_count)
    running = sleeping = disk_sleep = futex_wait = 0
    seen = 0
    for tid in tids:
        fields = _stat_fields(task_dir, int(tid))
        if fields:
            seen += 1
            state = fields[0]
            if state == "R":
                running += 1
            elif state in ("S", "I"):
                sleeping += 1
            elif state == "D":
                disk_sleep += 1
        wchan = _read_text(task_dir / tid / "wchan")
        if wchan and "futex" in wchan.lower():
            futex_wait += 1
    return ThreadStats(
        count=seen or fallback_count,
        running=running,
        sleeping=sleeping,
        disk_sleep=disk_sleep,
        futex_wait=futex_wait,
    )


def _fd_count(proc_root: Path, pid: int) -> int | None:
    try:
        return sum(1 for _ in (proc_root / str(pid) / "fd").iterdir())
    except OSError:
        return None


def _cwd_of(proc_root: Path, pid: int, roster: Roster) -> str | None:
    """The process's working directory, bounded and redacted, or ``None``.

    A fixture stores ``cwd`` as a plain file rather than the symlink production
    has, so both shapes are accepted.
    """
    link = proc_root / str(pid) / "cwd"
    try:
        target = os.readlink(link)
    except OSError:
        text = _read_text(link)
        if text is None:
            return None
        target = text.strip()
    if not target:
        return None
    return _bounded(
        _redact(target), MAX_PATH_CHARS, roster, "cwd"
    )  # redact first, bound second -- see _decode_cmdline


def _runq_wait_ns(proc_root: Path, pid: int) -> int | None:
    """Run-queue wait nanoseconds from ``/proc/<pid>/schedstat`` field 2.

    High run-queue wait means the host is oversubscribed and this process is
    waiting for a CPU. Low run-queue wait beside a high interpreter-lock wait
    means the opposite: a CPU was available and something else held the lock.
    Reporting the number is what lets those two be told apart.
    """
    raw = _read_text(proc_root / str(pid) / "schedstat")
    if raw is None:
        return None
    parts = raw.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _allowlisted_env(proc_root: Path, pid: int, roster: Roster) -> dict[str, str]:
    """The allowlisted environment values for *pid*, bounded and redacted.

    Reads ``/proc/<pid>/environ`` ONCE and keeps only :data:`ENV_ALLOWLIST`
    keys. Deliberately NOT routed through
    :func:`kiro_crew.session_pid._env_value`: that helper returns ``None`` off
    Linux whatever ``proc_root`` says, unlike the cmdline and marker readers
    beside it, which honour an explicit root on every host. Routing through it
    made this map come back EMPTY WITH NO NOTE on macOS and Windows, and made
    this module's tests accidentally Linux-only -- caught by the macOS and
    Windows CI shards, not locally.

    An unreadable environ is counted, never guessed, and is kept distinct from
    a key that is simply unset (which is just absent from the result).
    """
    try:
        blob = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        roster.note_repeated("environ unreadable")
        return {}
    wanted = {key.encode(): key for key in ENV_ALLOWLIST}
    out: dict[str, str] = {}
    for entry in blob.split(b"\x00"):
        name, sep, value = entry.partition(b"=")
        if not sep or name not in wanted:
            continue
        # Redact first, bound second -- see _decode_cmdline for why the reverse
        # order leaks a credential prefix.
        out[wanted[name]] = _bounded(
            _redact(value.decode("utf-8", "replace")), MAX_PATH_CHARS, roster, "env value"
        )
    return out


# -- classification ----------------------------------------------------------


def _classify(pid: int, cmdline: bytes, gateway_pid: int | None, owner: str | None) -> str:
    """The process kind, narrowest match first.

    Every argv test is the reaper's own, reached through
    :mod:`kiro_crew.session_pid` rather than spelled again here, so a fix to one
    of those tests reaches this view too.
    """
    flat = cmdline.replace(b"\x00", b" ")
    basename = session_pid._work_orphan_basename(cmdline)

    if gateway_pid is not None and pid == gateway_pid:
        return "gateway"
    if session_pid._GATEWAYD_MODULE in flat:
        return "gatewayd"
    if any(marker in flat for marker in session_pid._GATEWAY_MARKERS):
        return "gateway"
    if any(token in flat for token in _GATEWAY_ENTRY_TOKENS):
        return "gateway"
    if session_pid._BROWSER_DAEMON_ENTRY in flat or basename.startswith(
        (b"chrome", b"chromium", b"headless")
    ):
        return "browser"
    if session_pid._work_sweep_cmdline_is_test_runner(cmdline) or basename in (
        b"vitest",
        b"pytest",
    ):
        return "test"
    if b"mcp_cron" in flat or b"kirocrew cron" in flat:
        return "cron"
    if session_pid._is_orphan_mcp(cmdline) or session_pid._is_marked_mcp_launcher(cmdline):
        return "mcp-server"
    if b"kirocrew pod" in flat:
        return "pod"
    if session_pid._basename_names_a_harness(basename):
        # A subagent runtime is the same harness binary as a chat runtime; only
        # the owner label distinguishes them, and only when a caller supplied
        # one (the live session map lives in the SessionManager, not in a file).
        if owner and owner.lower().startswith("subagent"):
            return "subagent"
        return "chat"
    return "other"


def _matches_family_cmdline(cmdline: bytes) -> bool:
    if not cmdline:
        return False
    flat = cmdline.replace(b"\x00", b" ").lower()
    return any(token in flat for token in _FAMILY_CMDLINE_TOKENS)


# -- family assembly ---------------------------------------------------------


def _all_pids(proc_root: Path) -> list[int]:
    try:
        return sorted(int(e.name) for e in proc_root.iterdir() if e.name.isdigit())
    except OSError:
        return []


def _parent_edges(proc_root: Path, pids: Iterable[int]) -> dict[int, int]:
    """``{pid: ppid}`` over *pids*, skipping any process that vanished mid-walk."""
    edges: dict[int, int] = {}
    for pid in pids:
        fields = _stat_fields(proc_root, pid)
        if not fields or len(fields) < 2:
            continue
        try:
            edges[pid] = int(fields[1])
        except ValueError:
            continue
    return edges


def _descendants(root: int, edges: Mapping[int, int]) -> set[int]:
    """Every pid reachable from *root* by parent edges, *root* included."""
    children: dict[int, list[int]] = {}
    for pid, ppid in edges.items():
        children.setdefault(ppid, []).append(pid)
    seen = {root}
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _family_reasons(
    edges: Mapping[int, int],
    cmdlines: Mapping[int, bytes],
    markers: Mapping[int, bool | None],
    gateway_pid: int | None,
) -> dict[int, str]:
    """Which family rule admits each pid, or absence when none does.

    The marker-chain arm is the reaper's ownership rule
    (:func:`kiro_crew.session_scope_reap._scope_owned_pids`): a process with no
    marker of its own is still ours when its ``ppid`` chain reaches a marker
    bearer. It is applied here over the whole process table rather than over one
    cgroup scope, which is the only difference.
    """
    reasons: dict[int, str] = {}

    if gateway_pid is not None and gateway_pid in edges:
        for pid in _descendants(gateway_pid, edges):
            reasons[pid] = "descendant"

    marked = {pid for pid, value in markers.items() if value is True}
    for pid in marked:
        reasons.setdefault(pid, "marker")

    for pid in edges:
        if pid in reasons:
            continue
        cursor = pid
        seen: set[int] = set()
        while True:
            parent = edges.get(cursor)
            if parent is None or parent <= 1 or parent in seen:
                break
            if parent in marked:
                reasons[pid] = "marker-chain"
                break
            seen.add(parent)
            cursor = parent

    for pid, cmdline in cmdlines.items():
        if pid not in reasons and _matches_family_cmdline(cmdline):
            reasons[pid] = "cmdline"

    return reasons


# -- scan --------------------------------------------------------------------


def scan(
    prev: Roster | None = None,
    *,
    owner_map: Mapping[int, str] | None = None,
    history: Mapping[int, Mapping[str, object]] | None = None,
    proc_root: Path = PROC_ROOT,
    gateway_pid: int | None = None,
    platform_name: str | None = None,
    subreaper_pids_fn: Callable[[], set[int]] | None = None,
    tracked_pids_fn: Callable[[], set[int]] | None = None,
    tracked_owners_fn: Callable[[], dict[int, int]] | None = None,
    unreachable_orphan_fn: Callable[[int, bytes, set[int]], bool] | None = None,
    ps_snapshot_fn: Callable[[], dict[int, int] | None] | None = None,
    clk_tck: int | None = None,
) -> Roster:
    """One whole-family snapshot.

    Pass *prev* to get ``cpu_pct`` and ``runq_wait_pct``: both are deltas, so a
    single scan cannot produce them.

    *owner_map* is ``{pid: session label}``. It is injected rather than read
    because the live session-to-pid map lives in the gateway's
    ``SessionManager`` (``runtime_pids()``), not on disk; what the on-disk
    registry knows is which gateway TRACKS a pid, and that is what
    :func:`kiro_crew.session_pid.tracked_agent_pid_owners` supplies as the
    fallback. *history* carries ``former_owner`` and ``orphan_since`` for a
    caller that has kept earlier rosters -- this module keeps none.

    Every other keyword is a seam. The defaults are the production functions;
    tests replace them so the whole path runs against a fixture process table
    with no real ``/proc``, no ``ps`` and no signals.
    """
    platform = platform_name or sys.platform
    roster = Roster(ts=time.time(), monotonic=time.monotonic(), platform=platform)
    roster.gateway_pid = gateway_pid if gateway_pid is not None else os.getpid()

    if platform.startswith("win"):
        roster.degraded.append("windows: no /proc and no ps snapshot; all fields null")
        return roster
    if platform == "darwin":
        return _scan_darwin(roster, owner_map=owner_map, ps_snapshot_fn=ps_snapshot_fn)
    return _scan_linux(
        roster,
        prev=prev,
        owner_map=owner_map,
        history=history,
        proc_root=proc_root,
        subreaper_pids_fn=subreaper_pids_fn,
        tracked_pids_fn=tracked_pids_fn,
        tracked_owners_fn=tracked_owners_fn,
        unreachable_orphan_fn=unreachable_orphan_fn,
        clk_tck=clk_tck,
    )


def _resolve_owner(
    pid: int,
    owner_map: Mapping[int, str] | None,
    edges: Mapping[int, int],
    tracked_owners: Mapping[int, int],
) -> str | None:
    """The session label for *pid*, or the tracked-runtime attribution for it.

    A caller-supplied label wins, because only the gateway knows session keys.
    Failing that, the on-disk registry answers a narrower but still useful
    question: which gateway tracks this runtime, and -- for a descendant --
    which tracked runtime it hangs off. Those are reported as ``gateway:<pid>``
    and ``runtime:<pid>`` so no reader mistakes either for a session key.
    """
    if owner_map and pid in owner_map:
        return owner_map[pid]
    if pid in tracked_owners:
        return f"gateway:{tracked_owners[pid]}"
    cursor = pid
    seen: set[int] = set()
    while True:
        parent = edges.get(cursor)
        if parent is None or parent <= 1 or parent in seen:
            return None
        if owner_map and parent in owner_map:
            return owner_map[parent]
        if parent in tracked_owners:
            return f"runtime:{parent}"
        seen.add(parent)
        cursor = parent


def _scan_linux(
    roster: Roster,
    *,
    prev: Roster | None,
    owner_map: Mapping[int, str] | None,
    history: Mapping[int, Mapping[str, object]] | None,
    proc_root: Path,
    subreaper_pids_fn: Callable[[], set[int]] | None,
    tracked_pids_fn: Callable[[], set[int]] | None,
    tracked_owners_fn: Callable[[], dict[int, int]] | None,
    unreachable_orphan_fn: Callable[[int, bytes, set[int]], bool] | None,
    clk_tck: int | None = None,
) -> Roster:

    subreaper_pids_fn = subreaper_pids_fn or session_pid._accepted_subreaper_pids
    tracked_pids_fn = tracked_pids_fn or session_pid._tracked_agent_pids
    unreachable_orphan_fn = unreachable_orphan_fn or session_pid._is_untracked_managed_agent_orphan

    pids = _all_pids(proc_root)
    if not pids:
        roster.degraded.append(f"{proc_root}: no process entries readable")
        return roster

    edges = _parent_edges(proc_root, pids)
    # Read argv WHOLE, deliberately. An earlier revision cut it here to bound
    # this transient whole-table map, and that cut is the same silent credential
    # leak the retained fields had: a secret straddling it loses the pattern the
    # redactor matches on. Two reviews weighed the pair and the memory concern
    # was adjudicated disproportionate while the credential one was upheld, so
    # the transient buffer stays unbounded and every cut happens AFTER redaction.
    # What the roster RETAINS is still capped, by MAX_NODES and the field bounds.
    cmdlines = {pid: session_pid._pid_cmdline(pid, proc_root) for pid in edges}
    markers = {pid: session_pid._read_env_has_kirocrew_marker(pid, proc_root) for pid in edges}

    reasons = _family_reasons(edges, cmdlines, markers, roster.gateway_pid)
    if not reasons:
        roster.degraded.append("no family member identified")
        return roster

    try:
        subreapers = set(subreaper_pids_fn())
    except Exception:  # noqa: BLE001 -- a diagnostic read must never raise out
        subreapers = {1}
        roster.degraded.append("subreaper set unavailable; assumed init only")
    try:
        tracked = set(tracked_pids_fn())
    except Exception:  # noqa: BLE001
        tracked = set()
        roster.degraded.append("tracked-pid registry unreadable")
    tracked_owners = _tracked_owners(tracked_owners_fn, roster.degraded)

    clk_tck = _clock_ticks() if clk_tck is None else clk_tck
    dt = roster.monotonic - prev.monotonic if prev is not None else 0.0

    # Population cap. Admission is PRIORITISED rather than arbitrary: a
    # diagnostic that drops the orphans to respect a cap is worse than the
    # unbounded version it replaced, because finding them is the whole errand.
    # The gateway goes first, then every process reparented to a subreaper (each
    # one a candidate orphan), then ordinary members by pid so the choice is
    # reproducible across scans.
    def _admission_rank(pid: int) -> tuple[int, int]:
        if pid == roster.gateway_pid:
            return (0, pid)
        if edges.get(pid) in subreapers:
            return (1, pid)
        return (2, pid)

    order = sorted(reasons, key=_admission_rank)
    if len(order) > MAX_NODES:
        omitted = order[MAX_NODES:]
        order = order[:MAX_NODES]
        # Do NOT claim every candidate survived: prioritising them only helps
        # while they fit. Past that the cap eats candidates too, and saying
        # otherwise would be reassuring and false.
        omitted_candidates = sum(1 for pid in omitted if edges.get(pid) in subreapers)
        roster.degraded.append(
            f"node cap {MAX_NODES} reached: {len(omitted)} member(s) omitted, "
            f"of which {omitted_candidates} reparented candidate(s); admission "
            "prioritised the gateway, then reparented candidates, then pid order"
        )

    for pid in order:
        node = _build_linux_node(
            pid,
            proc_root=proc_root,
            cmdline=cmdlines.get(pid, b""),
            marker=markers.get(pid),
            family_reason=reasons[pid],
            edges=edges,
            owner_map=owner_map,
            tracked_owners=tracked_owners,
            history=history,
            subreapers=subreapers,
            tracked=tracked,
            unreachable_orphan_fn=unreachable_orphan_fn,
            prev=prev,
            dt=dt,
            clk_tck=clk_tck,
            roster=roster,
        )
        if node is not None:
            roster.nodes[pid] = node
    return roster


def _tracked_owners(
    tracked_owners_fn: Callable[[], dict[int, int]] | None,
    degraded: list[str],
) -> dict[int, int]:
    """``{tracked pid: owning gateway pid}``, or an empty map plus a note."""
    if tracked_owners_fn is None:

        tracked_owners_fn = getattr(session_pid, "tracked_agent_pid_owners", None)
        if tracked_owners_fn is None:
            degraded.append("session_pid.tracked_agent_pid_owners unavailable")
            return {}
    try:
        return dict(tracked_owners_fn())
    except Exception:  # noqa: BLE001
        degraded.append("tracked-pid owner map unreadable")
        return {}


def _build_linux_node(
    pid: int,
    *,
    proc_root: Path,
    cmdline: bytes,
    marker: bool | None,
    family_reason: str,
    edges: Mapping[int, int],
    owner_map: Mapping[int, str] | None,
    tracked_owners: Mapping[int, int],
    history: Mapping[int, Mapping[str, object]] | None,
    subreapers: set[int],
    tracked: set[int],
    unreachable_orphan_fn: Callable[[int, bytes, set[int]], bool],
    prev: Roster | None,
    dt: float,
    clk_tck: int,
    roster: Roster,
) -> ProcNode | None:
    fields = _stat_fields(proc_root, pid)
    if fields is None:
        return None  # exited between the walk and the read

    ppid = edges.get(pid)
    status = _status_map(proc_root, pid)
    owner = _resolve_owner(pid, owner_map, edges, tracked_owners)
    if owner is not None:
        owner = _bounded(owner, MAX_OWNER_CHARS, roster, "owner label")

    cpu_ticks = _stat_int(fields, 11, 12)
    if cpu_ticks is not None:
        roster.cpu_ticks[pid] = cpu_ticks
    runq = _runq_wait_ns(proc_root, pid)
    if runq is not None:
        roster.runq_ns[pid] = runq

    # Identity gate on both rates. A pid alone cannot authorize a subtraction:
    # if this number now names a DIFFERENT process than it did last scan, the
    # two cumulative readings belong to two processes and their difference is
    # meaningless. An unknown identity on either side also declines.
    start_id = _stat_int(fields, 19)
    if start_id is not None:
        roster.starts[pid] = str(start_id)
    same_process = (
        prev is not None
        and start_id is not None
        and prev.starts.get(pid) is not None
        and prev.starts.get(pid) == str(start_id)
    )
    prev_cpu = prev.cpu_ticks.get(pid) if (prev is not None and same_process) else None
    prev_runq = prev.runq_ns.get(pid) if (prev is not None and same_process) else None
    if prev is not None and not same_process and pid in prev.cpu_ticks:
        roster.note_repeated("pid recycled between scans; rates declined")

    cpu_pct = _rate_pct(prev_cpu, cpu_ticks, dt, clk_tck)
    runq_pct = _rate_pct(prev_runq, runq, dt, 1_000_000_000)

    threads = _thread_stats(proc_root, pid, _kib_field(status, "Threads") or _stat_int(fields, 17))

    # The kernel fact and the verdict are SEPARATE. Reparenting alone is the
    # reaper's candidate filter, not its conclusion: a gateway started by
    # ``systemd --user`` has the user manager as its parent for its whole life,
    # so a view that called reparenting "orphaned" would flag every
    # systemd-launched service on the host. Verified against this host, where
    # that draft reported 11 orphans and most were live services.
    #
    # The verdict adds ownership, on the reaper's own terms: the process carries
    # the KIROCREW_SPAWNED marker, or its ppid chain reaches a bearer. A
    # gateway is the SPAWNER and carries no marker, so it drops out; a leaked
    # runtime, MCP server or browser daemon under a marked tree does not. An
    # unreadable marker fails CLOSED -- unproven ownership is not an orphan --
    # and ``degraded`` records that the read failed.
    reparented = ppid is not None and ppid in subreapers
    ours = marker is True or family_reason in ("marker", "marker-chain")
    orphan = reparented and ours
    unreachable = False
    if reparented:
        try:
            unreachable = bool(unreachable_orphan_fn(pid, cmdline, tracked))
        except Exception:  # noqa: BLE001
            roster.note_repeated("orphan reachability check failed")

    past = dict(history.get(pid) or {}) if history else {}
    former = _as_str(past.get("former_owner"))
    if former is not None:
        # Bounded even though it arrives from a caller rather than from the
        # process table. The Opus lane weighed this and dropped it, reasoning a
        # recorder would only ever store an already-bounded owner label -- but
        # that recorder does not exist yet, and one `_bounded` call is cheaper
        # than depending on a module nobody has written behaving well.
        former = _bounded(former, MAX_OWNER_CHARS, roster, "former owner label")
    futex = threads.futex_wait or 0
    hint = bool(
        cpu_pct is not None
        and cpu_pct >= _GIL_HINT_CPU_PCT
        and futex >= _GIL_HINT_FUTEX_THREADS
        and _looks_like_python(cmdline)
    )

    return ProcNode(
        pid=pid,
        ppid=ppid,
        kind=_classify(pid, cmdline, roster.gateway_pid, owner),
        owner=owner,
        former_owner=former,
        age_secs=_pid_age(proc_root, fields, clk_tck),
        state=fields[0] if fields else None,
        cpu_pct=cpu_pct,
        runq_wait_pct=runq_pct,
        rss_kb=_kib_field(status, "VmRSS"),
        swap_kb=_kib_field(status, "VmSwap"),
        threads=threads,
        fds=_fd_count(proc_root, pid),
        cwd=_cwd_of(proc_root, pid, roster),
        env=_allowlisted_env(proc_root, pid, roster),
        cmdline=_decode_cmdline(cmdline, roster),
        marker=marker,
        family_reason=family_reason,
        reparented=reparented,
        orphan=orphan,
        orphan_rule=(
            "reparented to a session_pid._accepted_subreaper_pids target AND owned "
            "by marker or marker-chain"
            if orphan
            else ""
        ),
        orphan_since=_as_float(past.get("orphan_since")),
        orphan_unreachable=unreachable,
        gil_saturated_hint=hint,
        gil_hint_basis=(
            f"cpu {cpu_pct:.0f}% of one core with {futex} thread(s) in futex wait; "
            "kernel view only, not a GIL measurement"
            if hint and cpu_pct is not None
            else None
        ),
    )


def _stat_int(fields: list[str], *indices: int) -> int | None:
    """Sum the named ``stat`` indices, or ``None`` when any is unreadable."""
    total = 0
    for index in indices:
        if index >= len(fields):
            return None
        try:
            total += int(fields[index])
        except ValueError:
            return None
    return total


def _pid_age(proc_root: Path, fields: list[str], clk_tck: int) -> float | None:
    """Age in seconds from ``stat`` field 22 (``starttime``), or ``None``.

    ``starttime`` counts clock ticks from boot on the same base as
    ``CLOCK_BOOTTIME``, so the difference is the age. A fixture supplies
    ``uptime`` beside the pid directories; production reads the real clock.
    """
    start = _stat_int(fields, 19)
    if start is None or clk_tck <= 0:
        return None
    raw = _read_text(proc_root / "uptime")
    if raw is not None:
        try:
            return max(0.0, float(raw.split()[0]) - start / clk_tck)
        except (ValueError, IndexError):
            return None
    try:
        return max(0.0, time.clock_gettime(time.CLOCK_BOOTTIME) - start / clk_tck)
    except (OSError, AttributeError):
        return None


def _rate_pct(before: int | None, after: int | None, dt: float, per_second: int) -> float | None:
    """A cumulative counter pair as a percentage of wall time, or ``None``.

    ``None`` when there is no baseline, no elapsed time, or the counter went
    backwards -- which means the pid was recycled between scans, and a recycled
    pid's counters belong to a different process and must not be differenced.
    """
    if before is None or after is None or dt <= 0 or per_second <= 0 or after < before:
        return None
    return 100.0 * (after - before) / per_second / dt


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _darwin_parent_edges() -> dict[int, int] | None:
    """``{pid: ppid}`` on macOS, from the sanctioned ``ps`` snapshot.

    Deliberately NOT ``acp.runtime._ps_process_table``, which the spec first
    pointed at: the repo's agent-SDK import boundary is a blocking gate and
    forbids application code from importing the ACP layer, so that helper is
    unreachable from here however convenient it looks. CI caught it.
    :func:`kiro_crew.platform_compat._posix_process_snapshot` sits in a
    sanctioned layer and answers the same question.

    The trade is RSS: the ACP helper read ``rss=`` and this one reads
    ``lstart=``, so macOS nodes report ``rss_kb`` as ``None``. Reported as a
    missing field rather than papered over -- see the ``degraded`` note.
    """
    from kiro_crew import platform_compat

    snapshot = platform_compat._posix_process_snapshot()
    if snapshot is None:
        return None
    # `row.ppid` is read as a plain attribute ON PURPOSE. The row is a NamedTuple
    # that declares the field, so a getattr with a default here would convert a
    # renamed field into every edge silently reading None -- an empty family on
    # the whole platform, reported as a healthy scan.
    return {pid: row.ppid for pid, row in snapshot.items()}


def _scan_darwin(
    roster: Roster,
    *,
    owner_map: Mapping[int, str] | None,
    ps_snapshot_fn: Callable[[], dict[int, int] | None] | None,
) -> Roster:
    """macOS family, from a ``ps`` snapshot of parent edges.

    That snapshot carries parent edges and nothing this view can use besides,
    so family membership here is descent from the gateway alone -- there is no
    argv to match and no same-uid ``environ`` to read. Every field it cannot
    carry stays ``None``, and ``degraded`` says which ones and why.
    """
    parents = (ps_snapshot_fn or _darwin_parent_edges)()
    if parents is None:
        roster.degraded.append("darwin: ps snapshot unavailable")
        return roster
    roster.degraded.append(
        "darwin: ps snapshot carries pid/ppid only; state, threads, env, fds, "
        "cwd, cmdline, rss and cpu are null"
    )
    # Without the marker there is no ownership evidence, so no node here can earn
    # the orphan verdict -- and a reparented process is not a gateway descendant,
    # so it is absent from the roster entirely. Say so: an operator must not read
    # this platform's `orphans: 0` as a measured zero.
    roster.degraded.append(
        "darwin: orphan detection did not run; the ps snapshot carries no "
        "ownership marker, so orphans: 0 here means UNMEASURED, not none"
    )

    root = roster.gateway_pid
    if root is None:
        roster.degraded.append("darwin: no gateway pid to root the family at")
        return roster

    family = sorted(_descendants(root, parents))
    if len(family) > MAX_NODES:
        roster.degraded.append(
            f"node cap {MAX_NODES} reached: {len(family) - MAX_NODES} member(s) omitted"
        )
        family = family[:MAX_NODES]
    for pid in family:
        label = (owner_map or {}).get(pid)
        roster.nodes[pid] = ProcNode(
            pid=pid,
            ppid=parents.get(pid),
            kind="gateway" if pid == root else "other",
            owner=None if label is None else _bounded(label, MAX_OWNER_CHARS, roster, "owner"),
            family_reason="descendant",
        )
    return roster


# -- views -------------------------------------------------------------------


def tree(roster: Roster, fmt: str = "tree", **filters: object) -> dict[str, object]:
    """Render *roster* as a nested tree or a flat list.

    Filters: ``kind`` (one name or a list), ``owner`` (exact label),
    ``orphan_only`` (bool), ``include_env`` (bool, default off).

    A filter that keeps a child but drops its parent still emits the ancestor
    chain, each retained ancestor carrying ``matched: false``. Dropping the
    ancestors instead would reparent the match to the root and quietly destroy
    the one thing a tree is for.
    """
    include_env = bool(filters.get("include_env"))
    matched = _matching_pids(roster, filters)

    header: dict[str, object] = {
        "ts": roster.ts,
        "platform": roster.platform,
        "gateway_pid": roster.gateway_pid,
        "total": len(roster.nodes),
        "matched": len(matched),
        "orphans": roster.orphan_count(),
        "counts_by_kind": roster.counts_by_kind(),
        "env_included": include_env,
        "env_allowlist": list(ENV_ALLOWLIST),
        "degraded": roster.degraded_report(),
    }

    if fmt == "flat":
        header["nodes"] = [
            roster.nodes[pid].as_dict(include_env=include_env) for pid in sorted(matched)
        ]
        return header

    keep = set(matched)
    for pid in matched:
        cursor = roster.nodes[pid].ppid
        seen: set[int] = set()
        while cursor is not None and cursor in roster.nodes and cursor not in seen:
            keep.add(cursor)
            seen.add(cursor)
            cursor = roster.nodes[cursor].ppid

    children: dict[int | None, list[int]] = {}
    for pid in sorted(keep):
        parent = roster.nodes[pid].ppid
        anchor = parent if parent in keep else None
        children.setdefault(anchor, []).append(pid)

    # Built ITERATIVELY, not by recursion. Nesting by recursion costs one Python
    # frame per tree level, so a deep enough family chain raises RecursionError
    # inside a view whose whole job is to be readable when the host is unwell.
    # Rows are created flat and then linked by reference, which nests the same
    # structure at any depth.
    rows: dict[int, dict[str, object]] = {}
    for pid in sorted(keep):
        row = roster.nodes[pid].as_dict(include_env=include_env)
        row["matched"] = pid in matched
        rows[pid] = row
    for parent, kids in children.items():
        if parent is not None:
            rows[parent]["children"] = [rows[kid] for kid in kids]

    header["roots"] = [rows[pid] for pid in children.get(None, ())]
    return header


def _matching_pids(roster: Roster, filters: Mapping[str, object]) -> set[int]:
    wanted_kind = filters.get("kind")
    kinds: set[str] | None = None
    if isinstance(wanted_kind, str):
        kinds = {wanted_kind}
    elif isinstance(wanted_kind, (list, tuple, set)):
        kinds = {str(item) for item in wanted_kind}
    owner = filters.get("owner")
    orphan_only = bool(filters.get("orphan_only"))

    out: set[int] = set()
    for pid, node in roster.nodes.items():
        if kinds is not None and node.kind not in kinds:
            continue
        if owner is not None and node.owner != owner:
            continue
        if orphan_only and not node.orphan:
            continue
        out.add(pid)
    return out


def roster_diff(prev: Roster | None, cur: Roster) -> dict[str, object]:
    """What was born and what died between two rosters.

    With no *prev* there is no baseline, so nothing is reported as born: a first
    sample that announced the whole family as new would read as a process burst
    to any caller watching for one. ``baseline`` says that is what happened.
    """
    if prev is None:
        return {
            "baseline": True,
            "born": [],
            "died": [],
            "born_count": 0,
            "died_count": 0,
            "recycled_pids": [],
            "total": len(cur.nodes),
            "orphans": cur.orphan_count(),
        }

    def summarize(node: ProcNode) -> dict[str, object]:
        return {
            "pid": node.pid,
            "kind": node.kind,
            "owner": node.owner,
            "rss_kb": node.rss_kb,
        }

    # Keyed on (pid, start identity), never on pid alone. A recycled pid is one
    # process that DIED and a different one that was BORN; reporting it as
    # neither loses both events, and on a busy host that is routine rather than
    # exotic.
    def key(roster: Roster, pid: int) -> tuple[int, str | None]:
        return (pid, roster.starts.get(pid))

    prev_keys = {key(prev, pid) for pid in prev.nodes}
    cur_keys = {key(cur, pid) for pid in cur.nodes}
    born = [
        summarize(cur.nodes[pid]) for pid in sorted(cur.nodes) if key(cur, pid) not in prev_keys
    ]
    died = [
        summarize(prev.nodes[pid]) for pid in sorted(prev.nodes) if key(prev, pid) not in cur_keys
    ]
    recycled = sorted(
        pid for pid in set(prev.nodes) & set(cur.nodes) if key(prev, pid) != key(cur, pid)
    )
    return {
        "baseline": False,
        "born": born,
        "died": died,
        "born_count": len(born),
        "died_count": len(died),
        "recycled_pids": recycled,
        "total": len(cur.nodes),
        "orphans": cur.orphan_count(),
    }
