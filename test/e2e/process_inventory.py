"""Who owns the processes one gateway instance put on this machine.

The question this module answers is deliberately narrow: for ONE gateway
instance, reconcile two independent sources of truth and report where they
disagree.

* **The kernel** -- every pid inside that instance's own agent slice
  (``kirocrew-agents-<token>.slice``), read straight out of cgroup v2's
  ``cgroup.procs`` files. A process cannot lie about this and nothing in the
  product has to remember to write it.
* **The registry** -- the pid tracking files the product maintains under the
  instance's data home (``kiro_session_pids.txt`` for runtime roots,
  ``kiro_pids.txt`` for roots and descendants).

Three populations come out of that reconciliation, and each one names a
different defect:

``owned_alive``
    In the registry, and the process is still there with the same start
    identity. The healthy population. A session that is running should be
    here; nothing should be here once every session is closed.
``owned_dead``
    In the registry, but the process is gone (or its pid was recycled by an
    unrelated process). The registry has not forgotten a runtime that died --
    a stale entry, and for anything that later signals by pid, a hazard.
``unowned_alive``
    Alive inside the instance's slice, but in no registry entry. Nothing owns
    it, so nothing will ever clean it up. This is the leak in its purest form.

Why the pair and not one number: the two disagreements have opposite
remedies. An ``unowned_alive`` process must be killed and recorded; an
``owned_dead`` entry must be forgotten. A single "mismatch" count cannot drive
either.

Reading the cgroup tree rather than asking systemd is not an optimisation. The
``/sys/fs/cgroup`` files are plain reads available to any process in the user's
own subtree, whereas ``systemctl --user`` needs the session D-Bus socket, which
a confined caller frequently cannot open. A harness that probed systemd would
report "no systemd here" on a host whose slices are in front of it.

Pid recycling is handled the way the product handles it: an entry carries the
child's process-start identity, and a live pid whose identity does not match
the recorded one is a STRANGER, so it counts as ``owned_dead`` (the tracked
process is gone) and never as ``owned_alive``.

Scope is one instance, never the shared parent slice. A host routinely runs
several gateways at once, so "every pid in ``kirocrew-agents.slice``" is not
"my pids", and a harness that assumed otherwise would report a co-resident
gateway's healthy sessions as this one's leak.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: The user manager's cgroup subtree, where the product places the agent slice.
_USER_CGROUP_BASE = "/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"

#: Shared parent of every instance's agent slice. Matches
#: ``sandbox._CGROUP_AGENTS_SLICE``; restated so a rename there surfaces as a
#: named mismatch in :func:`assert_slice_contract` rather than silently
#: pointing this module at a directory nothing creates.
AGENTS_SLICE = "kirocrew-agents.slice"

#: Registry file holding one line per runtime ROOT, as
#: ``<gateway_pid>:<child_pid>:<start_token>``.
SESSION_PID_FILE = "kiro_session_pids.txt"

#: Registry file holding roots as a bare ``<pid>`` and descendants as
#: ``<child_pid>:<parent_pid>[:<start_token>]``.
PID_FILE = "kiro_pids.txt"

#: argv fragment that identifies a managed MCP stub process. The stub is
#: launched as ``python -m kiro_crew.mcp_gateway.stub --server <name> ...``, so
#: the module path is the stable token; the ``--server`` value names which one.
STUB_ARGV_TOKEN = "kiro_crew.mcp_gateway.stub"

#: argv fragment identifying the MCP gateway daemon, which owns the stubs.
GATEWAYD_ARGV_TOKEN = "kiro_crew.mcp_gateway.gatewayd"

#: Substrings that mark a file as the crew log, for the open-handle probe.
CREW_LOG_PATH_TOKENS = ("crew-log", "crewlog", "crew_log")


def _uid() -> int:
    return os.getuid()


def read_argv(pid: int, *, proc_root: Path = Path("/proc")) -> str:
    """One pid's command line as a space-joined string, or ``""`` if unreadable.

    An unreadable ``cmdline`` means the process exited between listing and
    reading, or belongs to another user. Both are "nothing to say about it"
    rather than an error: the caller already has the pid, and a summary is a
    diagnostic aid, not evidence.
    """
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def start_token(pid: int, *, proc_root: Path = Path("/proc")) -> str | None:
    """The pid's process-start identity, or ``None`` when it cannot be read.

    Field 22 of ``/proc/<pid>/stat`` is the process start time in clock ticks
    since boot. Together with the pid it identifies one process run, which is
    what makes a recycled pid detectable. Parsed from the LAST ``)`` because a
    process name may itself contain spaces and parentheses.
    """
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        tail = raw[raw.rindex(")") + 1 :].split()
        # stat fields are 1-based and the split drops pid and comm, so field 22
        # (starttime) sits at index 19 of what remains.
        return tail[19]
    except (ValueError, IndexError):
        return None


def pid_alive(pid: int, *, proc_root: Path = Path("/proc")) -> bool:
    return (proc_root / str(pid)).is_dir()


def ppid(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """The pid's parent, or ``None`` when unreadable. ``1`` means reparented."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return int(raw[raw.rindex(")") + 1 :].split()[1])
    except (ValueError, IndexError):
        return None


def instance_slice_token(home: Path) -> str:
    """The slice token the product derives for the instance rooted at *home*.

    Replicates ``sandbox._instance_slice_token``: the first 12 hex digits of
    the SHA-256 of the resolved data-home path. Replicated rather than imported
    because importing it would bind to the TEST process's data home, and the
    instance under observation is a different one. :func:`assert_slice_contract`
    checks this against the product's own answer, so a change to the derivation
    fails by name instead of silently pointing at a slice nobody writes to.
    """
    return hashlib.sha256(str(Path(home).resolve()).encode("utf-8")).hexdigest()[:12]


def agents_slice_parent(*, cgroup_base: str | None = None) -> Path | None:
    """The shared ``kirocrew-agents.slice`` cgroup directory, or ``None``.

    Mirrors ``sandbox._agents_slice_cgroup_dir``: the direct construction under
    ``kirocrew.slice`` first, then a ONE-level scan for a manager that laid the
    hierarchy out differently. Never a recursive walk -- the cgroup tree is
    large and a deep match would be as likely to find the wrong thing as the
    right one. ``None`` means the slice is not active, which is a legitimate
    state: the directory exists only while something holds it.

    The platform check applies only when the base is being DISCOVERED. An
    explicit ``cgroup_base`` is the caller pointing at a tree they laid out
    themselves, which is how the classifier is tested on a host that has no
    cgroup v2 at all; refusing it there would make the reconciliation testable
    on Linux only, and so untested wherever the harness above it cannot run.
    """
    if cgroup_base is None:
        if sys.platform != "linux":
            return None
        base = Path(_USER_CGROUP_BASE.format(uid=_uid()))
    else:
        base = Path(cgroup_base)
    direct = base / "kirocrew.slice" / AGENTS_SLICE
    if direct.is_dir():
        return direct
    try:
        for child in base.iterdir():
            candidate = child / AGENTS_SLICE
            if candidate.is_dir():
                return candidate
    except OSError:
        pass
    return None


def instance_slice_dir(home: Path, *, cgroup_base: str | None = None) -> Path | None:
    """The agent slice belonging to the instance at *home*, or ``None``.

    ``None`` when the parent slice is absent or the instance's own child slice
    has never been created. The second case is not a failure: a gateway that
    has spawned no agent process owns no scope, so there is no directory yet,
    and the correct inventory for it is empty rather than unknown.
    """
    parent = agents_slice_parent(cgroup_base=cgroup_base)
    if parent is None:
        return None
    child = parent / f"{AGENTS_SLICE[: -len('.slice')]}-{instance_slice_token(home)}.slice"
    return child if child.is_dir() else None


def assert_slice_contract(home: Path, src: Path) -> None:
    """Fail unless the product derives the same slice name this module does.

    Runs ``sandbox._agents_slice_name()`` in a CHILD with ``KIROCREW_HOME`` set
    to *home*, because that is the only way to ask the product what it would do
    for a home other than this process's own -- setting the variable in-process
    would mutate memoised state the test process shares with everything else.

    Without this check the whole module degrades silently: a renamed slice or a
    changed token derivation would make :func:`instance_slice_dir` return
    ``None`` forever, every population would read empty, and the leak
    assertions would pass by measuring nothing.
    """
    probe = (
        "from kiro_crew import sandbox\n"
        "from kiro_crew.config.paths import config_dir\n"
        "print(sandbox._agents_slice_name())\n"
        "print(config_dir())\n"
    )
    env = {
        **os.environ,
        "KIROCREW_HOME": str(home),
        "PYTHONPATH": str(src) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    # cwd is the instance's own home, never the caller's: a child that writes
    # anything by a relative path must not land it in the repository checkout.
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            "could not ask the product for its agent slice name "
            f"(exit {result.returncode}): {result.stderr[-600:]}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise AssertionError(f"slice-name probe printed {lines!r}, expected two lines")
    product_name, product_home = lines[0].strip(), lines[1].strip()
    expected = f"{AGENTS_SLICE[: -len('.slice')]}-{instance_slice_token(home)}.slice"
    if product_name != expected:
        raise AssertionError(
            "the product's agent slice name and this harness's derivation have "
            f"diverged: product={product_name!r} harness={expected!r} "
            f"(product resolved its data home to {product_home!r}); update "
            "instance_slice_token/AGENTS_SLICE in this module to match"
        )


@dataclass(frozen=True)
class RegistryEntry:
    """One tracked pid, as the registry records it."""

    pid: int
    #: Recorded process-start identity, or ``None`` for a legacy two-field line.
    token: str | None
    #: Which file the entry came from, for diagnostics.
    source: str
    #: The gateway or parent pid the entry is filed under, when the line has one.
    owner_pid: int | None = None


def read_registry(home: Path) -> list[RegistryEntry]:
    """Every tracked pid under *home*, from both registry files.

    Tolerant by design: a malformed line is skipped rather than raised on. The
    files are appended to concurrently by a live gateway, so a torn final line
    is an ordinary sight and must not turn a measurement into an error.
    """
    entries: list[RegistryEntry] = []
    session_file = Path(home) / SESSION_PID_FILE
    for line in _lines(session_file):
        parts = line.split(":")
        # <gateway_pid>:<child_pid>[:<start_token>]
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        owner = int(parts[0]) if parts[0].isdigit() else None
        token = parts[2] if len(parts) > 2 and parts[2] else None
        entries.append(RegistryEntry(int(parts[1]), token, SESSION_PID_FILE, owner))
    for line in _lines(Path(home) / PID_FILE):
        parts = line.split(":")
        if parts[0].isdigit() and len(parts) == 1:
            # A bare root pid, tracked with no parent and no identity.
            entries.append(RegistryEntry(int(parts[0]), None, PID_FILE, None))
            continue
        # <child_pid>:<parent_pid>[:<start_token>]
        if len(parts) >= 2 and parts[0].isdigit():
            owner = int(parts[1]) if parts[1].isdigit() else None
            token = parts[2] if len(parts) > 2 and parts[2] else None
            entries.append(RegistryEntry(int(parts[0]), token, PID_FILE, owner))
    return entries


def _lines(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").split() if line.strip()]
    except OSError:
        return []


def slice_pids(slice_dir: Path) -> dict[int, str]:
    """Every pid inside *slice_dir*, mapped to the scope unit holding it.

    Covers the slice's own ``cgroup.procs`` as well as each ``*.scope`` child,
    because a process can sit directly in the slice when the scope wrapper is
    unavailable, and one that did would otherwise be invisible to a probe that
    only walked the scopes.
    """
    found: dict[int, str] = {}
    for pid in _read_procs(slice_dir / "cgroup.procs"):
        found[pid] = slice_dir.name
    try:
        children = sorted(p for p in slice_dir.iterdir() if p.is_dir())
    except OSError:
        return found
    for child in children:
        for pid in _read_procs(child / "cgroup.procs"):
            found.setdefault(pid, child.name)
    return found


def _read_procs(path: Path) -> list[int]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(line) for line in raw.split() if line.strip().isdigit()]


@dataclass(frozen=True)
class ProcessFact:
    """One process as the inventory saw it."""

    pid: int
    argv: str
    unit: str = ""
    parent: int | None = None
    #: Why the process landed in its population, when that needs saying.
    note: str = ""

    def summary(self) -> str:
        head = self.argv[:160] if self.argv else "(argv unreadable)"
        bits = [f"pid={self.pid}"]
        if self.parent is not None:
            bits.append(f"ppid={self.parent}")
        if self.unit:
            bits.append(f"unit={self.unit}")
        if self.note:
            bits.append(f"note={self.note}")
        return f"{' '.join(bits)} :: {head}"


@dataclass(frozen=True)
class Inventory:
    """The reconciliation of kernel truth against the registry.

    ``owned_alive`` / ``owned_dead`` / ``unowned_alive`` are the three
    populations described in the module docstring. ``slice_dir`` is ``None``
    when the instance owns no slice directory, which reads as an empty
    inventory and is recorded so a caller can tell "nothing running" from
    "nowhere to look".
    """

    owned_alive: tuple[ProcessFact, ...] = ()
    owned_dead: tuple[ProcessFact, ...] = ()
    unowned_alive: tuple[ProcessFact, ...] = ()
    slice_dir: Path | None = None
    registry_entries: int = 0
    scopes: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def in_slice(self) -> tuple[ProcessFact, ...]:
        """Live facts the kernel places INSIDE the instance's slice.

        ``owned_alive`` can also hold a tracked process that is alive but outside
        the slice (``unit`` empty) -- a spawn that ran unwrapped, or one the
        registry kept across a restart. Those are real and reported, but they are
        not slice members, so counting them in a stub tally or an RSS sum would
        attribute another cgroup's weight to this one.
        """
        return tuple(fact for fact in self.owned_alive + self.unowned_alive if fact.unit)

    @property
    def stub_pids(self) -> tuple[int, ...]:
        """Every live MCP stub in the slice, whoever owns it."""
        return tuple(fact.pid for fact in self.in_slice if STUB_ARGV_TOKEN in fact.argv)

    @property
    def live_pids(self) -> tuple[int, ...]:
        """Every live pid in the slice, whoever owns it."""
        return tuple(fact.pid for fact in self.in_slice)

    @property
    def tracked_outside_slice(self) -> tuple[ProcessFact, ...]:
        """Tracked, alive, and not a member of the instance's slice."""
        return tuple(fact for fact in self.owned_alive if not fact.unit)

    def counts(self) -> dict[str, int]:
        return {
            "owned_alive": len(self.owned_alive),
            "owned_dead": len(self.owned_dead),
            "unowned_alive": len(self.unowned_alive),
            "stubs": len(self.stub_pids),
            "scopes": self.scopes,
            "registry_entries": self.registry_entries,
        }

    def render(self, *, limit: int = 12) -> str:
        """A readable dump, for an assertion message.

        Every failing assertion in this harness prints one of these: a bare
        "expected 0, got 3" tells the next worker nothing about WHICH three
        processes survived, and the argv is what names the owner.
        """
        lines = [
            "process inventory: "
            + " ".join(f"{key}={value}" for key, value in self.counts().items()),
            f"  slice_dir={self.slice_dir if self.slice_dir else '(none)'}",
        ]
        for note in self.notes:
            lines.append(f"  note: {note}")
        for label, group in (
            ("unowned_alive", self.unowned_alive),
            ("owned_dead", self.owned_dead),
            ("owned_alive", self.owned_alive),
        ):
            if not group:
                continue
            lines.append(f"  {label} ({len(group)}):")
            for fact in group[:limit]:
                lines.append(f"    {fact.summary()}")
            if len(group) > limit:
                lines.append(f"    ... and {len(group) - limit} more")
        return "\n".join(lines)


def inventory(
    home: Path,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_base: str | None = None,
    ignore_pids: frozenset[int] = frozenset(),
) -> Inventory:
    """Reconcile the registry under *home* against its slice's live pids.

    ``ignore_pids`` drops pids the caller knows are not agent work -- the
    gateway process itself, and the test process's own tree when the harness
    happens to share the slice. Excluded rather than classified, because an
    inventory that counted the observer would never read zero.
    """
    slice_dir = instance_slice_dir(home, cgroup_base=cgroup_base)
    entries = read_registry(home)
    notes: list[str] = []
    if slice_dir is None:
        notes.append("instance has no agent slice directory; only registry entries are classified")
    live = slice_pids(slice_dir) if slice_dir is not None else {}
    live = {pid: unit for pid, unit in live.items() if pid not in ignore_pids}

    owned_alive: list[ProcessFact] = []
    owned_dead: list[ProcessFact] = []
    seen_registry: set[int] = set()
    for entry in entries:
        if entry.pid in ignore_pids or entry.pid in seen_registry:
            continue
        seen_registry.add(entry.pid)
        if not pid_alive(entry.pid, proc_root=proc_root):
            owned_dead.append(ProcessFact(entry.pid, "", note=f"gone; tracked in {entry.source}"))
            continue
        current = start_token(entry.pid, proc_root=proc_root)
        if entry.token and current and entry.token != current:
            owned_dead.append(
                ProcessFact(
                    entry.pid,
                    read_argv(entry.pid, proc_root=proc_root),
                    parent=ppid(entry.pid, proc_root=proc_root),
                    note=f"pid recycled; {entry.source} recorded start {entry.token}",
                )
            )
            continue
        owned_alive.append(
            ProcessFact(
                entry.pid,
                read_argv(entry.pid, proc_root=proc_root),
                unit=live.get(entry.pid, ""),
                parent=ppid(entry.pid, proc_root=proc_root),
                note="" if entry.pid in live else "tracked but outside the instance slice",
            )
        )

    unowned_alive = [
        ProcessFact(
            pid,
            read_argv(pid, proc_root=proc_root),
            unit=unit,
            parent=ppid(pid, proc_root=proc_root),
            note="in the slice, in no registry entry",
        )
        for pid, unit in sorted(live.items())
        if pid not in seen_registry
    ]

    scopes = 0
    if slice_dir is not None:
        try:
            scopes = sum(1 for p in slice_dir.iterdir() if p.is_dir())
        except OSError:
            scopes = 0

    return Inventory(
        owned_alive=tuple(owned_alive),
        owned_dead=tuple(owned_dead),
        unowned_alive=tuple(unowned_alive),
        slice_dir=slice_dir,
        registry_entries=len(seen_registry),
        scopes=scopes,
        notes=tuple(notes),
    )


def descendants_of(
    root: int, candidates: "set[int]", *, proc_root: Path = Path("/proc")
) -> set[int]:
    """Which of *candidates* are still descendants of *root*, transitively.

    Walks each candidate's ``ppid`` chain upward rather than building a children
    map downward, because the set being classified is already known and the
    chain is short. A chain that reaches ``1`` or ``0`` before finding *root*
    means the process was reparented, which is exactly the orphan condition a
    chaos run is looking for: the parent died and the child did not.

    Bounded so a ``ppid`` cycle from a torn read cannot spin forever.
    """
    kin: set[int] = set()
    for pid in candidates:
        walker: int | None = pid
        for _ in range(64):
            if walker is None or walker <= 1:
                break
            if walker == root:
                kin.add(pid)
                break
            walker = ppid(walker, proc_root=proc_root)
    return kin


def survivors(pids: "set[int]", *, proc_root: Path = Path("/proc")) -> set[int]:
    """Those of *pids* that are still alive."""
    return {pid for pid in pids if pid_alive(pid, proc_root=proc_root)}


def scope_backend_usable() -> tuple[bool, str]:
    """Whether a transient ``systemd --user`` scope can ACTUALLY be created here.

    Runs the real thing -- ``systemd-run --user --scope --collect --quiet true``
    -- instead of inspecting the environment for the conditions that usually
    imply it. The two disagree on a confined host: a sandbox can leave
    ``XDG_RUNTIME_DIR`` set and a ``systemd-run`` binary on PATH while denying
    the session bus socket, so an environment probe reports the backend as
    available and every spawn then silently runs unwrapped. A harness that
    trusted the environment would proceed, find no slice, and report a confusing
    failure where "unresolved" is the truth.

    ``--collect`` makes systemd garbage-collect the transient unit as soon as it
    exits, so the probe leaves nothing loaded behind. systemd never reclaims a
    unit on its own otherwise, and a probe that leaked one per run would be the
    same class of leak this module exists to find.
    """
    if sys.platform != "linux":
        return False, f"agent scopes are a Linux cgroup-v2 feature; host is {sys.platform}"
    binary = shutil.which("systemd-run")
    if binary is None:
        return False, "systemd-run is not on PATH"
    try:
        result = subprocess.run(
            [binary, "--user", "--scope", "--collect", "--quiet", "true"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run systemd-run: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, (detail[0][:200] if detail else f"systemd-run exited {result.returncode}")
    return True, ""


class SliceCleanupError(RuntimeError):
    """A test-created agent slice could not be stopped.

    Raised rather than logged because the residue is permanent: systemd keeps a
    slice loaded until someone stops it, so a failed stop leaves one unit behind
    per occurrence with nothing that ever retries. A test whose own cleanup
    leaked a unit has to say so.
    """


def stop_instance_slice(home: Path) -> str:
    """Stop the agent slice belonging to the instance at *home*.

    systemd does not reclaim a slice unit once it has been created, so a harness
    that spawns a gateway on a fresh data home adds one loaded unit per run and
    never removes it. Left alone that accumulates without bound in the operator's
    user manager -- the same unbounded-residue shape the inventory reports on --
    so every harness that boots a gateway owes this call on teardown.

    Issued through ``os.posix_spawn`` rather than ``subprocess``: the suite funnels
    ``Popen`` through a guard that refuses host-service verbs, and rightly so. This
    is cleanup of a unit this very run created, which is the documented exception,
    and spawning directly is how the repo's own opt-in fixture does it.

    Returns a one-line note on success. Raises :class:`SliceCleanupError` when the
    stop did not happen -- an absent ``systemctl``, a spawn that failed, or a
    non-zero exit status. A non-zero status is an ordinary teardown-time failure
    (a bus that went away, a timeout), so treating it as success is how the leak
    would accumulate silently.
    """
    slice_name = f"{AGENTS_SLICE[: -len('.slice')]}-{instance_slice_token(home)}.slice"
    posix_spawn = getattr(os, "posix_spawn", None)
    if posix_spawn is None:
        raise SliceCleanupError(
            "os.posix_spawn is unavailable on this host, so the test-created slice "
            f"{slice_name} cannot be stopped; agent slices are a Linux feature and "
            "this path is only reached where one was created"
        )
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise SliceCleanupError(
            f"systemctl is not on PATH, so the test-created slice {slice_name} is "
            "still loaded in the user manager"
        )
    try:
        pid = posix_spawn(
            systemctl, [systemctl, "--user", "stop", "-q", slice_name], dict(os.environ)
        )
        _, status = os.waitpid(pid, 0)
    except (OSError, ValueError) as exc:
        raise SliceCleanupError(f"could not stop {slice_name}: {exc}") from exc
    # WIFEXITED/WEXITSTATUS are POSIX-only. Where they are absent a raw 0 is the
    # only success value there is, which is also what the decoded form means.
    wifexited = getattr(os, "WIFEXITED", None)
    wexitstatus = getattr(os, "WEXITSTATUS", None)
    if wifexited is not None and wexitstatus is not None:
        succeeded = bool(wifexited(status)) and wexitstatus(status) == 0
    else:
        succeeded = status == 0
    if not succeeded:
        raise SliceCleanupError(
            f"systemctl --user stop {slice_name} did not succeed (wait status "
            f"{status}); the slice is still loaded in the user manager"
        )
    return f"stopped {slice_name}"


def crew_log_write_handles(pid: int, *, proc_root: Path = Path("/proc")) -> list[str]:
    """Crew-log files the process at *pid* holds open, as ``fd -> target``.

    Reads ``/proc/<pid>/fd`` directly rather than shelling out to ``lsof``: the
    symlink targets are the same information, the read needs no extra binary on
    the runner, and an unreadable entry is a race (the fd closed underneath the
    walk) rather than a finding.

    A DELETED target still counts. A handle on an unlinked file is precisely the
    shape that keeps a rotated log's bytes alive with no path to reach them, so
    dropping it would hide the case worth reporting.

    A token has to match a whole path COMPONENT, not appear anywhere in the
    string: the crew log lives in a directory of that name, and a loose
    substring test also matches any unrelated path that happens to contain the
    word -- a scratch directory named after a test, for instance -- which turns
    an unrelated open file into a reported finding.
    """
    found: list[str] = []
    fd_dir = proc_root / str(pid) / "fd"
    try:
        names = sorted(fd_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return found
    for entry in names:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        # A deleted target arrives as "<path> (deleted)"; strip the suffix so the
        # component match still sees the real path components.
        cleaned = target.removesuffix(" (deleted)")
        # Split on BOTH separators rather than through one path flavour: the value
        # comes from the host's own readlink, so a Windows reader returns
        # backslashes, and a posix-flavour split would see the whole string as one
        # component and match nothing.
        parts = {part.lower() for part in re.split(r"[\\/]+", cleaned) if part}
        if parts & set(CREW_LOG_PATH_TOKENS):
            found.append(f"{entry.name} -> {target}")
    return found
