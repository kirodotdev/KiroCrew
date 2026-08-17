"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import socket
import sys
import warnings

import pytest
from hypothesis import HealthCheck, settings

from kiro_crew.safety_override import reset_singleton as _reset_safety_override
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.handler import _PHASE_EMOJIS, _build_phase_emojis

# ── Hypothesis profiles ─────────────────────────────────────────────────
# Default (CI): fast iteration.  Run ``HYPOTHESIS_PROFILE=thorough python -m pytest``
# for deeper coverage.
settings.register_profile("default", max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None)
settings.register_profile("thorough", max_examples=100)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

# Ensure .hypothesis/tmp exists (build environment may not have it)
os.makedirs(os.path.join(os.path.dirname(__file__), "..", ".hypothesis", "tmp"), exist_ok=True)

_HAS_GIT = shutil.which("git") is not None

requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git not available")


def _can_create_symlink() -> bool:
    """PROBE, never a platform guess: can this process create a real symlink?

    Creating one on Windows needs ``SeCreateSymbolicLinkPrivilege``, held by an
    elevated or Developer-Mode account (GitHub's Windows runners do) and not by
    an ordinary one. Probing keeps the coverage wherever the privilege exists
    instead of blanket-skipping every Windows host — a bare
    ``skipif(IS_WINDOWS)`` would silently drop these assertions on CI, which is
    exactly where they need to run.

    Reserve this for tests about the SYMLINK MECHANISM itself. A test that only
    needs "a name meaning another directory" belongs on
    ``platform_compat.symlink_or_junction`` (junction on Windows, no privilege needed).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "target")
        os.mkdir(target)
        try:
            os.symlink(target, os.path.join(tmp, "link"))
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


_HAS_SYMLINKS = _can_create_symlink()

requires_symlinks = pytest.mark.skipif(
    not _HAS_SYMLINKS,
    reason="creating a symlink needs SeCreateSymbolicLinkPrivilege on Windows",
)


# ── Windows CI ──────────────────────────────────────────────────────────
# The backend runs natively on Windows (kiro_crew.platform_compat), but a
# handful of suites exercise POSIX-only-by-design features (OS-level
# sandbox, process groups / PGID semantics, PTY, AF_UNIX sockets -- see
# docs/guides/windows-install.md's per-feature table). Skip collecting them on
# Windows rather than marking test-by-test: several fail at import or
# fixture time on win32.
from kiro_crew import platform_compat  # noqa: E402

if platform_compat.IS_WINDOWS:
    # Read from windows-collect-ignore.txt rather than an inline list: the CI
    # reduced-scope selector (scripts/ci-surface-tests.py) has to apply the same
    # exclusion, because naming a file explicitly on the pytest command line
    # bypasses collect_ignore. One file, two readers, no drift.
    _ignore_listfile = os.path.join(os.path.dirname(__file__), "windows-collect-ignore.txt")
    with open(_ignore_listfile, encoding="utf-8") as _fh:
        collect_ignore = [
            name
            for name in (ln.split("#", 1)[0].strip() for ln in _fh)
            if name
        ]


def make_escaping_link(inside: pathlib.Path, outside: pathlib.Path) -> str:
    """Create a reparse link inside ``inside`` pointing at ``outside``.

    Returns the ``inside``-relative path of a file reached THROUGH the link, for
    tests asserting that a canonical-containment check (resolve +
    is_relative_to) catches a link escaping a sandbox root. ``outside`` must
    already contain a file named ``secret.py``.

    A file symlink needs SeCreateSymbolicLinkPrivilege on Windows, which an
    unelevated developer shell lacks (WinError 1314) even though CI runners hold
    it. A directory junction needs NO privilege and resolves through the same
    reparse machinery, so the containment assertion stays exercised locally
    instead of being skipped.
    """
    if platform_compat.IS_WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(outside), str(inside / "linked"))
        return "linked/secret.py"
    (inside / "link.py").symlink_to(outside / "secret.py")
    return "link.py"


def make_dir_link(link: pathlib.Path, target: pathlib.Path) -> None:
    """Create a reparse point at ``link`` that resolves to the directory ``target``.

    Same privilege reasoning as :func:`make_escaping_link`, for the tests that
    need a *directory* link rather than a path through one: a directory symlink
    needs SeCreateSymbolicLinkPrivilege on Windows (WinError 1314 in an
    unelevated shell), while a junction needs none and is followed by the same
    reparse machinery — ``rglob``, ``resolve`` and
    ``GetFinalPathNameByHandleW`` all traverse it identically. So the behaviour
    under test stays exercised on Windows instead of being skipped.
    """
    if platform_compat.IS_WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    link.symlink_to(target, target_is_directory=True)


#: ``pytest_collection_modifyitems`` -- which applies the
#: ``windows-expected-failures.txt`` skips -- lives in the ROOTDIR ``conftest.py``.
#: That list already names node ids under
#: ``src/kiro_crew/apps/builtins/auto_improvement/tests/``, and a hook rooted here never
#: runs when only those in-package tests are collected (which is exactly what CI's
#: reduced-scope Windows job does on a frontend-only diff), so the skips silently did
#: not apply where they were needed.
#:
#: ``collect_ignore`` above deliberately stays here: it names paths relative to its own
#: conftest's directory and every entry is a file under ``test/``, so it is correct
#: where it is.


@pytest.fixture(autouse=True)
def _windows_restrict_to_owner_stub(request, monkeypatch):
    """On Windows, no-op the icacls secret lockdown for hermetic tests.

    Many tests stub ``subprocess.run`` (or strip PATH) for hermeticity;
    ``restrict_to_owner``'s whoami/icacls spawns then fail and its
    DELIBERATE fail-loud OSError cascades into hundreds of unrelated
    tests. The real Windows implementation keeps direct coverage in
    test_platform_compat / test_spawn_audit (exempted here) and the
    POSIX chmod path keeps full coverage on the Linux matrix. Product
    call sites that bound the symbol by value (tips.py) are unaffected
    by this module-attr patch -- acceptable: they surface as at most a
    handful of failures, handled individually.
    """
    if not platform_compat.IS_WINDOWS or request.module.__name__ in (
        "test_platform_compat",
        "test_spawn_audit",
    ):
        yield
        return
    monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: None)
    yield


@pytest.fixture(autouse=True)
def _isolate_aim_skills_dir(monkeypatch):
    """Prevent SkillsLoader from discovering edition-contributed skill roots.

    SkillsLoader now sources extra skill roots from the CPP seam
    ``McpToolingProvider.extra_skills()`` (public Default ``[]``) rather than a
    hardcoded ``~/.aim/skills``. Pin the Default to ``[]`` so a developer with a
    composed companion (or leftover roots) can't inflate session context beyond
    _MAX_CONTEXT_CHARS and cause silent truncation / non-deterministic xdist
    failures.

    Does NOT request ``tmp_path``: this fixture only patches a method, and being
    autouse it made every one of the suite's ~26k tests allocate a temp directory it
    never touched -- the single largest fixed cost in the suite's setup path.
    """
    from kiro_crew.platform.defaults import DefaultMcpToolingProvider

    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])


def pytest_configure(config: pytest.Config) -> None:
    """Pre-import ``tracemalloc`` so pytest's unraisable hook can't crash on it.

    pytest's ``_pytest/unraisableexception`` plugin replaces ``sys.unraisablehook``
    and, when a leaked object (an un-awaited coroutine, an orphaned
    ``SessionManager._cleanup_loop`` task, etc.) is garbage-collected, calls
    ``tracemalloc_message()`` which runs ``import tracemalloc`` *from inside the
    GC callback*. If ``tracemalloc`` has not been imported yet, that first import
    lands in a partially-initialized state (a CPython circular-import artifact
    observed on 3.12) and raises ``AttributeError: partially initialized module
    'tracemalloc' has no attribute 'get_object_traceback'``. pytest then re-raises
    it as ``RuntimeError: Failed to process unraisable exception`` and reports it
    as an ERROR at the *next* test's setup — turning a benign "object was never
    awaited" warning into a hard build failure that lands on an innocent test.

    Importing the module eagerly here (once per xdist worker, before any test
    runs or any GC fires) makes the hook's ``import tracemalloc`` a no-op
    ``sys.modules`` hit against a fully-built module, so leaks degrade back to
    warnings instead of failing the suite. Touch ``get_object_traceback`` to
    force full initialization and to keep the import from reading as unused.
    """
    import tracemalloc

    assert hasattr(tracemalloc, "get_object_traceback")

    # ── Sandbox probe prewarm ───────────────────────────────────────────────
    # Production processes (gateway, gatewayd) call prewarm_backend() at boot so
    # the sandbox probe cache is populated off-loop before any on-loop handler
    # runs.  pytest-xdist workers have no equivalent boot path, so whichever
    # aiohttp-route test first exercises wrap_argv() on a cold worker hits the
    # "never probe on event loop" guard and gets a transient failure.  Mirror the
    # production prewarm here: one synchronous detect_backend() per worker
    # process, populating the cache before any test runs.
    #
    # Tests that intentionally reset_backend() in their own fixtures
    # (test_sandbox_backend_cache.py) are unaffected — they manage their own
    # cache state.
    try:
        from kiro_crew.sandbox import detect_backend

        detect_backend()
    except Exception:
        pass  # Probe failure must not break unrelated tests.


_MAX_WORKERS_ENV = "KIROCREW_MAX_TEST_WORKERS"
_SLOT_DIR_ENV = "KIROCREW_TEST_SLOT_DIR"
_DEFAULT_WORKER_CAP = 32
_GIB = 1024**3
# The LIVE memory readings are taken in MiB, because in GiB anything under 1 GiB
# truncates to 0 -- which is the same value they use for "could not determine", and an
# unknown reading is deliberately skipped. See _host_available_mib.
_MIB = 1024**2
# Headroom to reserve per worker. Measured peak RSS for a worker on a heavy
# 1,231-test subset is ~0.37 GiB (~0.50 GiB under --cov), so 2 GiB reserves
# roughly 4x typical and keeps the term from binding on ordinary hosts while
# still refusing to spawn, say, 32 workers on an 8 GiB machine. Note this sizes
# for EXPECTED footprint: it cannot save a host from a genuinely leaking worker
# (one orphaned run was observed at 4.3 GiB RSS), which is a separate bug.
_GIB_PER_WORKER = 2
# Headroom to reserve per worker against the LIVE availability reading, which needs a
# different margin from the static ceilings above and not by preference -- by kind.
# Total RAM and the cgroup limit are worst-case bounds that never move, so paying ~4x
# measured RSS there is free. `MemAvailable` is ALREADY the current headroom, so
# reserving 4x on top of it double-counts: on a host with 28 GiB free it would refuse
# to spawn more than 14 workers on 32 idle cores, halving parallelism to protect memory
# that was never at risk. 1 GiB is ~2x the measured peak under coverage (~0.50 GiB),
# which still refuses to start 32 workers on a host with 8 GiB genuinely free.
_GIB_PER_WORKER_AVAILABLE = 1

# Lock files this process holds for its whole lifetime -- the fds MUST stay open,
# because the lock lives exactly as long as the fd does.
_held_slots: list[int] = []


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _slot_root() -> pathlib.Path:
    override = os.environ.get(_SLOT_DIR_ENV)
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".cache" / "kirocrew" / "test-slots"


def _host_key() -> str:
    """Filesystem-safe identity for this machine, as a single path segment."""
    raw = socket.gethostname() or ""
    safe = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in raw)[:64]
    return safe.strip(".") or "unknown-host"


def _slot_dir() -> pathlib.Path:
    """Where concurrent pytest runs ON THIS HOST contend for worker capacity.

    Deliberately host-global and *not* derived from ``KIROCREW_HOME``: the point
    is that two worktrees -- which have different homes and know nothing about
    each other -- still coordinate over the one thing they truly share, the
    machine's cores and RAM.

    Scoped by hostname because ``~/.cache`` is frequently a network home shared
    by many machines, whose contention is not ours.
    """
    return _slot_root() / _host_key()


def _slot_path(slot_dir: pathlib.Path, index: int) -> pathlib.Path:
    return slot_dir / f"worker-{index:03d}.lock"


def _host_total_gib() -> int:
    """Total physical RAM in GiB, or 0 when it cannot be determined."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0
    if pages <= 0 or page_size <= 0:
        return 0
    return int(pages * page_size // _GIB)


def _cgroup_limit_files() -> list[str]:
    """Every memory-ceiling file that bounds THIS process, tightest-first-ish.

    The ceiling lives at the process's OWN cgroup, not at the root of the hierarchy.
    Under cgroup v2 the root has no ``memory.max`` file at all, so reading
    ``/sys/fs/cgroup/memory.max`` finds something only where a cgroup NAMESPACE has
    remapped the container's own cgroup onto the mount root -- the docker/podman
    default, but not what a systemd slice with ``MemoryMax=``, a ``cgroupns=host``
    Kubernetes pod, or LXC gives you. There the limit is at
    ``/sys/fs/cgroup/<relative path>/memory.max``, and a root-only read returns nothing,
    which is indistinguishable from "no limit".

    A cgroup is bounded by the tightest limit anywhere on its ancestor chain, so the own
    cgroup and every ancestor are candidates. The bare root paths stay LAST so a
    namespaced container -- and a host where ``/proc/self/cgroup`` cannot be read at
    all -- keeps the reading it would otherwise have had.
    """
    files: list[str] = []
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        controllers, relative = fields[1], fields[2]
        if not controllers:  # v2 spells its single entry "0::<path>"
            base, leaf = "/sys/fs/cgroup", "memory.max"
        elif "memory" in controllers.split(","):  # v1: "<id>:<controllers>:<path>"
            base, leaf = "/sys/fs/cgroup/memory", "memory.limit_in_bytes"
        else:
            continue
        parts = [part for part in relative.split("/") if part]
        while parts:
            files.append("/".join([base, *parts, leaf]))
            parts.pop()
    files.append("/sys/fs/cgroup/memory.max")
    files.append("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    return files


def _cgroup_limit_mib() -> int:
    """The cgroup's memory ceiling in MiB, or 0 when there is none.

    ``SC_PHYS_PAGES`` reports the MACHINE's RAM, which is the wrong number inside a
    container: a 2-CPU/8 GiB CI container on a 256 GiB host reads 256 GiB and sizes its
    worker budget against memory it will be OOM-killed for touching.

    Takes the TIGHTEST limit found on the process's cgroup chain (see
    :func:`_cgroup_limit_files`), not the first one: an inner cgroup can be looser than
    an ancestor, and the ancestor still binds.

    MiB, not GiB, for the reason spelled out on :func:`_host_available_mib`: in GiB a
    512 MiB container ceiling truncates to ``0``, which is this function's own "there is
    no limit" answer -- so the tightest ceiling of all would be read as no ceiling.

    ``max`` is cgroup v2's spelling for "no limit". v1 uses a very large sentinel
    instead, which no ``min()`` will ever pick, so it needs no special case.
    """
    tightest = 0
    for path in _cgroup_limit_files():
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            continue
        try:
            mib = int(raw) // _MIB
        except ValueError:
            continue
        if mib > 0 and (tightest == 0 or mib < tightest):
            tightest = mib
    return tightest


def _host_available_mib() -> int:
    """RAM in MiB that is actually free for a new worker, or 0 when unknown.

    ``MemAvailable`` is the kernel's own estimate of what a new allocation can use
    without swapping; it counts reclaimable page cache, which ``MemFree`` and
    ``SC_AVPHYS_PAGES`` both omit and would therefore understate badly on any host
    that has read files.

    Bounding on this in ADDITION to total RAM is what makes the budget protective
    rather than decorative. The flock slots below already stop two pytest runs from
    oversubscribing each other, but they are blind to memory the host is using for
    anything else -- a build, a browser, a running gateway. Sizing 32 workers against
    total RAM on a machine with 2 GiB genuinely free is how a run starts swapping and
    then makes no progress at all, which is the incident the whole budget exists to
    prevent.

    **MiB, not GiB, and the unit is load-bearing.** Truncating to whole GiB makes every
    reading under 1 GiB come back as ``0`` -- which is also this function's "could not
    determine" answer, and :func:`_memory_bounded_capacity` SKIPS an unknown reading. So
    in GiB the bound would drop out precisely on the starved host it exists to protect:
    860 MiB free would read as "unknown", the live term would be discarded, and the
    static total-RAM bound would happily allow 16 workers. In MiB the same host reads
    860, which floors the budget at one worker.

    Linux-only by construction. macOS has no equivalent single number (its compressor
    makes "available" a policy question rather than a reading), so this returns 0 there
    and the static bounds stand alone.
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("MemAvailable:"):
                    continue
                # "MemAvailable:   107374182 kB" -- the unit is always kB.
                return int(line.split()[1]) * 1024 // _MIB
    except (OSError, IndexError, ValueError):
        return 0
    return 0


def _bounded_by(limit: int, readings: tuple[tuple[int, int], ...]) -> int:
    """*limit*, reduced by each ``(MiB, GiB-per-worker)`` reading that is available.

    A reading of 0 means "could not determine" and is SKIPPED rather than treated as
    zero memory. That direction matters more than it looks: reading it as zero would
    collapse the run to a single worker on any platform without that reading -- macOS
    and Windows have no ``/proc/meminfo`` and no ``/sys/fs/cgroup`` -- and a run that
    silently drops to one worker looks like a hang, not like a bug.

    Which is exactly why the readings that can be genuinely SMALL are in MiB: in GiB a
    sub-1-GiB reading truncates to 0 and would be discarded as unknown, so the bound
    would vanish on the starved host it exists to protect.
    """
    for mib, per_worker_gib in readings:
        if mib > 0:
            limit = min(limit, max(1, mib // (per_worker_gib * 1024)))
    return max(1, limit)


def _static_memory_bounded_capacity(cores: int) -> int:
    """*cores*, reduced by the memory readings that are CONSTANT for the machine.

    Total RAM and the cgroup ceiling only. Both are properties of the host, so a number
    derived from them is stable across runs -- which is what makes it safe to use as the
    shared slot RANGE below. Sharing a range only works if every run computes the same
    one; a range that moves between runs is not a namespace, it is a race.

    This is also what keeps the memory budget genuinely SHARED rather than per-run. On a
    64-core / 32 GiB host the static bound is 16 workers, so there are 16 slots in
    total: a first run takes them all, and a second gets its floor of one. Put the same
    bound only on the per-run cap and both runs would take 16 each -- 32 workers against
    a 16-worker memory budget, which is the swapping incident the budget exists to
    prevent, reached from the opposite direction.

    Total RAM stays in GiB because a machine with under 1 GiB of RAM in total is not a
    configuration this suite runs on, and its unit is pinned by existing tests.
    """
    return _bounded_by(
        cores,
        (
            (_host_total_gib() * 1024, _GIB_PER_WORKER),
            (_cgroup_limit_mib(), _GIB_PER_WORKER),
        ),
    )


def _live_memory_bounded_cap(cap: int) -> int:
    """*cap*, reduced by what is free on the host RIGHT NOW.

    Deliberately applied to the per-run cap and NOT to the shared slot range. The
    reading is transient, and slots fill from index 0 upward, so a range shortened by a
    momentary dip excludes precisely the slots an earlier run left free -- collapsing a
    later run to one worker while most of the machine sits idle. A cap is the right
    place for a transient reading: it throttles THIS run without reshaping the namespace
    every other run has to agree on.
    """
    return _bounded_by(cap, ((_host_available_mib(), _GIB_PER_WORKER_AVAILABLE),))


def _claim_worker_slots(capacity: int, cap: int) -> int:
    """Take up to ``cap`` of the host's ``capacity`` worker slots and HOLD them.

    ``capacity`` is how many slots the HOST has -- its core count, a constant, never a
    transient memory reading -- and is the range probed; ``cap`` is the most any single
    run may take. Keeping these separate matters on a large host: with 64 cores and a
    cap of 32, a first run takes slots 0-31 and a second still finds 32-63 free and gets
    its full 32. Probing only ``cap`` slots would have collapsed that second run to one
    worker while half the machine sat idle.

    Each slot is an advisory lock on its own file, acquired non-blocking and
    never released until the process exits. That is the whole design: the kernel
    owns the lease, so capacity returns automatically when a run ends --
    including a run that is orphaned or terminated outright, which is exactly
    the state that caused the incident this budget exists to prevent.

    This deliberately replaces an earlier design where runs wrote reservation
    FILES describing themselves. Files outlive their owners, so that version
    needed PID-liveness probing, a staleness backstop, ownership proof against
    look-alike files, and boot/suspend forensics to decide when a reservation
    was defunct -- and every one of those cleanup paths produced a real bug. A
    held lock needs none of it.

    Returns the number of slots taken, at least 1: a run arriving at a genuinely
    full host proceeds single-worker rather than stalling.
    """
    if _held_slots:
        # Already claimed in this process. Re-locking would fail: flock treats
        # two fds on one file as independent even within the same process, so a
        # second pass would take nothing and collapse the run to one worker.
        return len(_held_slots)
    try:
        # Resolution itself must be inside the guard: Path.home() raises
        # RuntimeError when the home directory cannot be determined, and
        # gethostname() can raise OSError. Neither may break pytest startup.
        root = _slot_root()
        slot_dir = _slot_dir()
        # Refuse a symlink at either level: the root is caller-supplied via
        # KIROCREW_TEST_SLOT_DIR and could redirect our writes.
        if root.exists() and root.is_symlink():
            return min(capacity, cap)
        slot_dir.mkdir(parents=True, exist_ok=True)
        if slot_dir.is_symlink() or not slot_dir.is_dir():
            return min(capacity, cap)
    except (OSError, RuntimeError, ValueError):
        return min(capacity, cap)  # fail open to the unbudgeted ceiling

    taken = 0
    for index in range(capacity):
        if taken >= cap:
            break
        try:
            fd = os.open(str(_slot_path(slot_dir, index)), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            # The directory exists but this run cannot create a slot file in it: a
            # read-only bind mount, an exhausted quota, a leftover dir owned by another
            # account. `mkdir(exist_ok=True)` above does NOT catch that -- Linux reports
            # EEXIST before it checks write permission -- so this is where it surfaces.
            #
            # Fall through to the one-worker floor rather than failing open to the
            # unbudgeted ceiling. Fail-open is the wrong direction HERE specifically
            # because the failure is not local: if this run cannot take a lock then
            # neither can a concurrent one, so both would receive the full cap with no
            # coordination at all -- which is precisely the oversubscription this budget
            # exists to prevent (two runs, ten workers each, load average ~590, zero tests
            # completing in 21 minutes). One worker is slow; two unbudgeted runs make no
            # progress.
            #
            # Say so, though. Silently dropping to one worker is a suite that takes an
            # hour for a reason nobody can see, and the fix -- point
            # KIROCREW_TEST_SLOT_DIR somewhere writable -- is only obvious once the cause
            # is named.
            warnings.warn(
                "xdist worker budget: cannot create a slot file under "
                f"{slot_dir}, so this run falls back to a single worker. Point "
                f"{_SLOT_DIR_ENV} at a writable directory to restore parallelism.",
                stacklevel=2,
            )
            break
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            _held_slots.append(fd)  # keep the fd -- closing it drops the lock
            taken += 1
        else:
            os.close(fd)
    return max(1, taken)


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Budget the worker count for ``-n auto`` (and ``-n logical``).

    Two separate quantities.

    **Host capacity** -- how many slots exist to compete for: cores, bounded by the
    memory readings that are CONSTANT for the machine (total RAM and the cgroup ceiling;
    see :func:`_static_memory_bounded_capacity`). It has to be constant, because it is
    the range of slot indices probed and every run must compute the same range for
    sharing to mean anything. Cores alone are the wrong unit: a 10-core / 32 GiB laptop
    cannot back 32 multi-GiB workers, and once it starts swapping the run stops making
    progress at all. Putting the static bound HERE rather than on the cap is also what
    keeps the memory budget shared: 16 slots on a 32 GiB host means two runs share 16
    workers, not 16 each.

    **Per-run cap** -- the most this single run may take, the tightest of:

    1. ``KIROCREW_MAX_TEST_WORKERS``, default 32. The optimal worker count for this
       suite plateaus around 24-32 and then *regresses*: every extra worker re-imports
       the full app (aiohttp/boto3/numpy/pdfplumber/transcribe) and writes its own
       ``.coverage.*`` file to combine at the end. Measured on a 64-core host:
       156s @ 64 workers vs 92s @ 32 workers (-41%).
    2. What is free on the host right now (see :func:`_live_memory_bounded_cap`). This
       reading is transient, so it throttles this run only and never reshapes the shared
       range -- it says nothing about the machine, only about what else is running on it.

    Keeping them separate is what makes a big host behave: with 64 cores and a
    cap of 32, two runs get 32 workers each rather than the second collapsing
    while half the machine idles.

    Sharing is what stops the failure this hook was extended for. Two worktrees
    each running ``-n auto`` on a 10-core box previously took 10 workers *each*,
    and the resulting swap thrash produced a load average of ~590 with zero
    tests completing in 21 minutes. Now each run holds a lock per worker it
    intends to spawn (under ``~/.cache/kirocrew/test-slots/<hostname>``, root
    overridable with ``KIROCREW_TEST_SLOT_DIR``): a run alone takes the whole
    machine, and a later run takes only what is unlocked. The locks are held for
    the process's lifetime and released by the kernel when it exits, so an
    orphaned or terminated run frees its share with no cleanup logic at all.

    The cost is fairness, not safety, and only when the host is GENUINELY full:
    a late run arriving at a fully-locked machine drops to its floor of one
    worker -- slow, but never stalled, and never oversubscribing the host the
    way the incident did. While free capacity remains, a later run gets its full
    share.

    An explicit ``-n <N>`` on the command line always wins; this hook only fires
    for ``auto`` / ``logical``.
    """
    # The two memory bounds go to different places, and which one goes where is the
    # whole correctness argument. The STATIC bound shapes the shared slot range, so the
    # budget is shared between concurrent runs rather than granted to each of them. The
    # LIVE bound only throttles this run, because a transient reading must not reshape a
    # namespace every other run has to agree on -- slots fill from index 0, so a shrunken
    # range excludes exactly the slots an earlier run left free.
    capacity = _static_memory_bounded_capacity(os.cpu_count() or 1)
    cap = _live_memory_bounded_cap(
        min(capacity, max(1, _int_env(_MAX_WORKERS_ENV, _DEFAULT_WORKER_CAP)))
    )
    return _claim_worker_slots(capacity, cap)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drop this run's worker slots promptly.

    Not strictly required -- the kernel releases every lock when the process
    exits -- but it returns capacity at the end of the run rather than at
    interpreter teardown, and is a no-op in xdist workers, which hold no slots.
    """
    while _held_slots:
        fd = _held_slots.pop()
        platform_compat.release_lock(fd)
        try:
            os.close(fd)
        except OSError:
            pass


# ── xdist INTERNALERROR terminal report (issue #2803) ───────────────────
# When TWO pytest-timeout worker kills land in the same ``--dist loadgroup``
# shard, xdist's loadscope scheduler can die with ``KeyError:
# <WorkerController gwN>`` (a replaced node present in ``assigned_work`` but
# absent from ``registered_collections``). pytest then exits 3 WITHOUT a
# ``short test summary info`` section, so the red names no failing test at
# all. The upstream defect is xdist's to fix; what this repo preserves is the
# REPORT: record every crashed worker and the test it was running (the
# pytest-timeout victim), and replay them from ``pytest_internalerror`` --
# a hook that fires only on the already-broken path, so healthy runs pay
# nothing. The run still exits non-zero: nothing here suppresses the
# INTERNALERROR traceback or touches the exit status.
#
# State lives at module level on the controller only: ``pytest_testnodedown``
# and ``pytest_handlecrashitem`` are controller-side xdist hooks that never
# fire inside a worker, and ``pytest_internalerror`` only emits when a crash
# was recorded, so a non-xdist internal error is reported exactly as before.

_crashed_workers: list[tuple[str, str]] = []  # (worker id, error text)
_crash_victims: list[str] = []  # test nodeids running when their worker died


def _reset_xdist_crash_state() -> None:
    """Test seam: clear the recorded crashes (module state is process-global)."""
    _crashed_workers.clear()
    _crash_victims.clear()


def pytest_testnodedown(node, error) -> None:
    """Record a crashed worker (controller only; ``error`` is None on clean exit)."""
    if error is None:
        return
    worker_id = getattr(getattr(node, "gateway", None), "id", None) or "<unknown worker>"
    _crashed_workers.append((str(worker_id), str(error)))


def pytest_handlecrashitem(crashitem, report, sched) -> None:
    """Record the test a crashed worker was running -- the timeout victim."""
    _crash_victims.append(str(crashitem))


def _format_abandoned_run_report(
    crashes: list[tuple[str, str]], victims: list[str]
) -> str:
    """Build the terminal report for a run abandoned after worker crashes.

    Wording is deliberately non-causal: worker replacement is routine here
    (``--max-worker-restart=2`` exists because workers die under memory
    pressure), so a later INTERNALERROR is not necessarily caused by the
    recorded crashes. The block replays what was RECORDED earlier in this
    run and leaves attribution to the reader.
    """
    lines = [
        "",
        "=" * 72,
        f"xdist run ABANDONED: INTERNALERROR after {len(crashes)} crashed-worker "
        f"replacement{'s' if len(crashes) != 1 else ''}",
        "=" * 72,
        "pytest hit an INTERNALERROR after replacing crashed workers, so the",
        "normal 'short test summary info' section was never written. The worker",
        "crashes recorded earlier in this run are replayed here so this red",
        "stays diagnosable:",
        "",
        "Crashed workers:",
    ]
    for worker_id, error in crashes:
        # The error is commonly a remote traceback: its FIRST line is the
        # constant "Traceback (most recent call last):", so the last
        # non-empty line (the exception itself) is the informative one.
        stripped = [ln for ln in error.strip().splitlines() if ln.strip()]
        summary = stripped[-1].strip() if stripped else error
        lines.append(f"    {worker_id}: {summary}")
    if victims:
        lines.append("")
        lines.append("Tests running when their worker died (recorded earlier in this run):")
        for victim in victims:
            lines.append(f"    {victim}")
    else:
        lines.append("")
        lines.append("No in-flight test was recorded for the crashed workers.")
    lines.append("")
    lines.append("The run still fails (INTERNALERROR, non-zero exit); this block only")
    lines.append("preserves the report that the crash would otherwise erase.")
    lines.append("=" * 72)
    return "\n".join(lines)


def pytest_internalerror(excrepr, excinfo) -> None:
    """Replay recorded worker crashes when an INTERNALERROR kills the run.

    Written to ``sys.stderr`` directly rather than through the terminal
    reporter: this hook runs on a path where pytest's own reporting machinery
    has already failed, and stderr is the one sink that cannot depend on it.
    Returns ``None`` (never ``True``) so pytest still prints the
    ``INTERNALERROR>`` traceback and exits 3 -- the goal is a diagnosable red,
    not a green.
    """
    if not _crashed_workers:
        return
    print(
        _format_abandoned_run_report(list(_crashed_workers), list(_crash_victims)),
        file=sys.stderr,
        flush=True,
    )


@pytest.fixture(autouse=True)
def _reset_safety_override_between_tests():
    """Reset the SafetyOverride singleton between tests to prevent state leaking."""
    _reset_safety_override()
    yield
    _reset_safety_override()


@pytest.fixture(autouse=True)
def _reset_reasoning_effort_globals():
    """Snapshot + restore the process-global reasoning-effort allowlist around
    each test. The allowlist is union-only/monotonic by design (persistence
    safety), and several AcpSessionHandle tests drive synthetic effort levels
    through ``_sync_effort_levels`` -> ``update_reasoning_effort_values``;
    without this, a level like ``"extreme"`` leaks into the global and poisons
    validation tests sharing the xdist worker (e.g. test_chat_slot_reasoning_effort)."""
    import kiro_crew.dashboard.chat_persistence as _cp

    saved_values = set(_cp._reasoning_effort_values)
    saved_ordered = list(_cp._reasoning_effort_ordered)
    try:
        yield
    finally:
        _cp._reasoning_effort_values = saved_values
        _cp._reasoning_effort_ordered = saved_ordered


#: ``_isolation_root`` / ``_isolation_dirs`` / ``_isolate_kirocrew_home`` live in the
#: ROOTDIR ``conftest.py``, not here. The data home has to be pinned for every
#: testpath, including the ~108 test modules that ship inside the package under
#: ``src/kiro_crew/apps/builtins/*/tests/`` and never see this file. The fixtures
#: below still request ``_isolation_dirs`` and resolve it up the hierarchy.


@pytest.fixture(autouse=True)
def _isolate_kiro_window_cache():
    """Give every test an EMPTY ``model_registry._KIRO_WINDOWS``, then restore it.

    The kiro-list window cache is process-global module state with two ways to
    couple tests:

    * **Test-to-test leak** — a test that exercises ``/api/models`` (which calls
      ``refresh_kiro_windows``) or seeds the cache directly would otherwise leave
      entries behind, e.g. a GPT window seeded here makes a "non-registry model
      is unknown" test in another module wrongly see GPT as known.
    * **Import-time host leak** — ``model_registry`` calls ``_load_kiro_windows()``
      at import, which reads ``<config_dir>/model_windows.json``. On a developer
      box that file holds the operator's real cached windows (e.g. a locally
      served ``deepseek-3.2`` at a non-registry value), so a test asserting the
      static supplementary floor for that same id fails ONLY on that machine —
      green in CI (no such file), red locally. Snapshotting-then-restoring alone
      preserved that polluted baseline for the duration of each test body.

    Clearing before the test (and restoring the original snapshot after) makes
    every test start from the same empty cache regardless of what the host had on
    disk — so a local run matches CI. Tests that need entries seed them in their
    own body.
    """
    import kiro_crew.model_registry as _mr

    saved = dict(_mr._KIRO_WINDOWS)
    _mr._KIRO_WINDOWS.clear()
    try:
        yield
    finally:
        _mr._KIRO_WINDOWS.clear()
        _mr._KIRO_WINDOWS.update(saved)


@pytest.fixture(autouse=True)
def _isolate_message_entry_cache():
    """Give every test an EMPTY ``chat_persistence`` persisted-entry cache.

    The memoised message-entry builder keeps a process-global cache keyed on a
    content hash of the whole message, so two tests using the same message
    content share an entry. That is harmless while the builder is pure, and a
    silent trap the moment a test makes it impure: a test that monkeypatches
    ``chat_persistence.redact_credentials`` (or the uncached builder) and reuses
    content another test already cached is served the earlier, pre-patch entry.
    The assertion then passes against a value the patched code never produced —
    worst of all for a redaction test, which would go green having seen the
    redacted entry it was written to prove absent.

    Lives here rather than in the memoisation test module because the hazard runs
    the other way: the module that pollutes the cache is not the one that
    misreads it.

    The byte counter is part of the same state, so resetting only the dict would
    leave the memory ceiling mis-accounted and evict a healthy cache.
    """
    from kiro_crew.dashboard import chat_persistence as _cp

    _cp._entry_cache.clear()
    _cp._entry_cache_bytes = 0
    try:
        yield
    finally:
        _cp._entry_cache.clear()
        _cp._entry_cache_bytes = 0


@pytest.fixture(autouse=True)
def _disarm_agent_slice_memory_high():
    """Disarm the agent-slice ``MemoryHigh`` reconcile for every test.

    ``cgroup_scope_argv`` reconciles ``MemoryHigh`` on the shared agent slice
    via a real ``systemctl --user set-property`` before wrapping a spawn. On a
    Linux host WITH cgroup delegation the probe passes for real, so any test
    that reaches ``cgroup_scope_argv`` (spawn-audit, the real pids.max
    enforcement test, integration paths) would mutate the developer's live
    user manager — exactly the class of side effect the root conftest's
    host-service guard refuses (``set-property`` is a mutating verb), turning
    those tests into guard failures. Pre-disarm via the module's own kill
    switch and restore all four state globals after, so tests of the
    reconciler itself can re-arm explicitly in their own body.
    """
    import kiro_crew.sandbox as _sb

    saved_disabled = _sb._SLICE_MEMHIGH_DISABLED
    saved_applied = _sb._SLICE_MEMHIGH_APPLIED
    saved_events_seen = _sb._SLICE_MEMHIGH_EVENTS_SEEN
    saved_climb_warned = _sb._SLICE_MEMHIGH_CLIMB_WARNED
    _sb._SLICE_MEMHIGH_DISABLED = True
    try:
        yield
    finally:
        _sb._SLICE_MEMHIGH_DISABLED = saved_disabled
        _sb._SLICE_MEMHIGH_APPLIED = saved_applied
        _sb._SLICE_MEMHIGH_EVENTS_SEEN = saved_events_seen
        _sb._SLICE_MEMHIGH_CLIMB_WARNED = saved_climb_warned


@pytest.fixture(autouse=True)
def _reset_options_control_state():
    """Clear the per-message OPTIONS registries between tests.

    ``kiro_crew.slack.outbound`` holds two process-global maps keyed by
    ``(channel, ts)``: the per-message edit lock, and the once-only answer claim
    that stops a second Send click dispatching a duplicate turn. Both are
    correct as process state in the gateway, where a control's ts is unique and
    lives as long as the message does.

    Tests are the opposite: fixtures reuse a fixed pair like ``("CH1", "msg1")``
    across unrelated cases, so without this the first test to submit claims the
    control and every later test's click is silently dropped as a duplicate.
    Reset per test rather than making production defensive about it.
    """
    from kiro_crew.slack import outbound

    outbound._ANSWERED.clear()
    outbound._EDIT_LOCKS.clear()
    outbound._LOCK_USERS.clear()
    yield
    outbound._ANSWERED.clear()
    outbound._EDIT_LOCKS.clear()
    outbound._LOCK_USERS.clear()


#: ``_isolate_subagents_dir``, ``_no_model_download`` and
#: ``_isolate_agent_state_sidecar`` live in the ROOTDIR ``conftest.py``. Each one
#: protects a real HOST path (the subagent registry a running gateway sweeps as
#: orphans, a 610MB model download, the operator's agent-state sidecar), so by the
#: same test the data home meets they belong to the floor that every testpath sees --
#: not to this file, which the in-package suites never load.


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Ensure a USABLE (open) event loop exists for tests that call
    ``asyncio.get_event_loop().run_until_complete(...)`` (e.g. test_knowledge).

    Two failure modes this guards, both seen on the loaded CI farm under xdist:
      * no current loop set (``RuntimeError``) — Python 3.9 Semaphore default_factory; and
      * a current loop that is set but CLOSED — left behind by a prior test in the same
        worker that ran ``asyncio.run(...)`` (which on 3.12 closes its loop at teardown).
        ``get_event_loop()`` returns that closed loop WITHOUT raising, so the next
        ``run_until_complete`` blows up with ``RuntimeError: Event loop is closed``. We
        detect a closed/absent loop and install a fresh open one so each test starts clean.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _restore_default_child_watcher():
    """Restore a FRESH ThreadedChildWatcher after every test.

    Some tests install a real, non-default asyncio child watcher via the
    gateway's ``_install_child_watcher()`` -- notably
    ``test_cli.py::test_real_subprocess_works_after_install_on_linux``, which on
    Linux installs a ``PidfdChildWatcher`` and runs ``asyncio.run``. On exit,
    ``asyncio.run`` detaches the watcher's loop, leaving a loop-less watcher in
    the global policy. Two distinct failures follow from that leak, and which
    one bites depends purely on xdist sharding, so adding or removing unrelated
    tests can flip a green run red with no production-code change:

    * On 3.10 the leaked watcher's ``is_active()`` is False, so the NEXT
      subprocess-spawning test fails with "asyncio.get_child_watcher() is not
      activated, subprocess support is not available".
    * On 3.12 the leaked watcher is still ATTACHED to callbacks bound to a loop
      that later closes. ``set_event_loop`` calls ``watcher.attach_loop()``,
      which reaps already-exited children and fires their callbacks -- against
      the closed loop -- raising ``RuntimeError: Event loop is closed``. Since
      pytest-asyncio calls ``set_event_loop`` when setting up every test that
      needs a loop, ONE leaked watcher fails every later test in that worker.

    The condition is therefore derived from whether the watcher API EXISTS, not
    from a version number: child watchers were only DEPRECATED in 3.12 and are
    removed in 3.14. A previous ``sys.version_info >= (3, 12): return`` guard
    skipped this cleanup on exactly the version where the second failure mode
    lives, which is what turned one leaked watcher into thousands of cascading
    failures in a full parallel run.
    """
    yield
    get_watcher = getattr(asyncio, "get_child_watcher", None)
    set_watcher = getattr(asyncio, "set_child_watcher", None)
    threaded = getattr(asyncio, "ThreadedChildWatcher", None)
    if not (get_watcher and set_watcher and threaded):
        # 3.14+, or a platform with no child watchers at all -- nothing to do.
        return
    try:
        with warnings.catch_warnings():
            # 3.12 deprecates these; the call is still the only way to clear the
            # leak on 3.12, so silence the warning rather than skip the fix.
            warnings.simplefilter("ignore", DeprecationWarning)
            current = get_watcher()
            # Install a FRESH watcher when the current one is the wrong type OR
            # is still holding pid->callback entries: those callbacks are bound
            # to a loop that may already be closed, and matching on type alone
            # would leave them in place.
            if not isinstance(current, threaded) or getattr(current, "_callbacks", None):
                set_watcher(threaded())
    except Exception:  # noqa: BLE001 -- isolation cleanup must never fail a test
        # Test-isolation cleanup must never fail a test; worst case is the
        # pre-existing leak, which the next test's loop setup also tolerates.
        pass


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make git tests hermetic: pin identity AND neutralize host global/system config.

    Two independent host-environment bleeds must be closed for git-backed tests
    (git_coord scenarios) to be deterministic across machines:

    1. Identity — a host without a global ``user.name``/``user.email`` makes
       ``git commit`` fail. Pin it via the ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
       env vars.
    2. Global/system config — the host's ``~/.gitconfig`` (via
       ``core.excludesFile`` → e.g. ``~/.gitignore_global`` containing ``*.png``)
       silently makes ``git add -A`` skip files, so a "commit a binary file"
       test sees an empty tree and gets an empty sha. Point
       ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` at ``/dev/null`` so no
       host-level config (excludes, aliases, hooks, signing) leaks into tests.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    # Isolate from the host's global/system git config (Git >= 2.32). An empty
    # file (/dev/null) means git reads no global or system settings.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture(autouse=True)
def _no_load_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip system load checks in tests — avoids real asyncio.sleep delays."""
    from unittest.mock import AsyncMock

    try:
        monkeypatch.setattr("kiro_crew.task_executor._wait_for_load", AsyncMock())
    except AttributeError:
        pass  # load guard not present in this branch


@pytest.fixture(autouse=True)
def _enterprise_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a default validated team_id so _route_message doesn't reject messages."""
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_team_id", "TTEST")
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_enterprise_id", "ETEST")
    monkeypatch.setattr("kiro_crew.slack.enterprise._allowed_team_ids", {"TTEST"})


@pytest.fixture(autouse=True)
def _clean_emojis():
    """Reset _PHASE_EMOJIS to defaults before each test (suppresses local config)."""
    original = dict(_PHASE_EMOJIS)
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(_build_phase_emojis({})[0])
    yield
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(original)


@pytest.fixture(autouse=True)
def _clean_slack_thread_state():
    """Reset the ``handler`` module-global thread-state maps between tests.

    ``handler`` keeps process-global maps for per-thread privacy and routing
    state: ``_thread_temporary`` / ``_thread_incognito`` (drive
    ``_is_slack_restricted`` — the memory-write gate consulted by ``!title``,
    consolidation, etc.), ``_titled_threads`` (auto-title claim), and
    ``_thread_agents`` (per-thread agent override). Nothing clears these
    globally, so a test that marks a thread restricted — including one that
    drives the real ``handle_message_transport`` drain path against a
    ``MagicMock`` session map, whose ``_hydrate_conv_flags`` reads truthy mock
    flags and calls ``_mark_incognito`` / ``_mark_temporary`` — leaves e.g.
    ``"thread1"`` in ``_thread_incognito`` forever. Under ``pytest -n auto``
    (``--dist load`` interleaves tests across files on each worker) a later
    ``test_title_updates_conversation_log`` then sees
    ``_is_slack_restricted("thread1") is True`` and skips ``set_title``,
    failing with no production-code change — a classic order-dependent flake.
    Clearing before and after every test makes each hermetic regardless of
    scheduling. Idempotent with per-file fixtures that already clear a subset.
    """
    from kiro_crew.slack import handler as _h

    for _m in (_h._thread_temporary, _h._thread_incognito, _h._titled_threads, _h._thread_agents):
        _m.clear()
    yield
    for _m in (_h._thread_temporary, _h._thread_incognito, _h._titled_threads, _h._thread_agents):
        _m.clear()


#: ``_isolate_sel_default_dir`` lives in the ROOTDIR ``conftest.py`` too, and for a
#: sharper reason than the data home: SEL's writer is a DAEMON THREAD on a process
#: singleton, so it outlives the test that first called ``sel()`` and keeps writing to
#: the directory that test resolved.


class MockSlackClient(SlackClientOps):
    """In-memory mock for testing."""

    def __init__(self):
        self.actions: list[tuple[str, dict]] = []
        self._next_ts = 1000000
        self._fetch_message_result: str | None = None
        self._fetch_thread_replies_result: list[dict] = []

    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            ("post", {"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts,
                      "unfurl_links": unfurl_links, "unfurl_media": unfurl_media})
        )
        return ts

    async def post_blocks(self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "blocks",
                {
                    "channel": channel,
                    "blocks": blocks,
                    "text": text,
                    "thread_ts": thread_ts,
                    "ts": ts,
                    "unfurl_links": unfurl_links,
                    "unfurl_media": unfurl_media,
                },
            )
        )
        return ts

    async def update_message(self, channel, ts, text):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))

    async def delete_message(self, channel, ts):
        self.actions.append(("delete", {"channel": channel, "ts": ts}))

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("react", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("unreact", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def open_dm(self, user_id):
        self.actions.append(("open_dm", {"user_id": user_id}))
        return f"D{user_id}"

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.actions.append(("ephemeral", {"channel": channel, "user_id": user_id, "text": text, "blocks": blocks, "thread_ts": thread_ts}))

    async def views_publish(self, user_id, view):
        self.actions.append(("views_publish", {"user_id": user_id, "view": view}))

    async def views_open(self, trigger_id, view):
        self.actions.append(("views_open", {"trigger_id": trigger_id, "view": view}))

    async def views_update(self, view_id, view):
        self.actions.append(("views_update", {"view_id": view_id, "view": view}))

    async def upload_file(self, channel, thread_ts, file, filename, title):
        self.actions.append(
            (
                "upload_file",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "file": file,
                    "filename": filename,
                    "title": title,
                },
            )
        )

    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None):
        if not getattr(self, "_stream_enabled", False) or getattr(self, "_start_stream_fails", False):
            return None
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "start_stream",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": initial_text,
                    "ts": ts,
                },
            )
        )
        return ts

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.actions.append(
            (
                "append_task",
                {
                    "channel": channel,
                    "ts": ts,
                    "task_id": task_id,
                    "title": title,
                    "status": status,
                    "details": details,
                },
            )
        )
        return True

    async def stop_stream(self, channel, ts, final_text=None):
        self.actions.append(("stop_stream", {"channel": channel, "ts": ts, "text": final_text}))
        return True

    async def set_thread_title(self, channel, thread_ts, title):
        self.actions.append(
            ("set_thread_title", {"channel": channel, "thread_ts": thread_ts, "title": title})
        )

    async def set_thread_status(self, channel, thread_ts, status):
        self.actions.append(
            ("set_thread_status", {"channel": channel, "thread_ts": thread_ts, "status": status})
        )

    async def fetch_message(self, channel: str, ts: str) -> str | None:
        self.actions.append(("fetch_message", {"channel": channel, "ts": ts}))
        return self._fetch_message_result

    async def fetch_thread_replies(self, channel: str, thread_ts: str, limit: int = 200, warn_on_pagination: bool = True) -> list[dict]:
        self.actions.append(("fetch_thread_replies", {"channel": channel, "thread_ts": thread_ts, "limit": limit, "warn_on_pagination": warn_on_pagination}))
        return self._fetch_thread_replies_result


@pytest.fixture(autouse=True, scope="module")
def _fake_computer_use_backend():
    """Register the shipped FAKE computer-use backend for the whole suite.

    Computer use reads another application's accessibility tree, captures its
    window pixels, and synthesizes clicks/keystrokes into it. CI must never do
    any of that, so this is one of the TWO mechanisms that keep the native path
    unreachable in tests:

    1. this process-wide registration, so ``get_shared_backend()`` always
       returns ``FakeComputerUseBackend`` (``platform_id == "fake"``);
    2. structural — the package has no module-scope ``CDLL``/``find_library``, so
       importing it on a Linux runner loads nothing native.

    Both are asserted: ``test_computer_use_backend.py::
    test_ci_never_selects_a_native_backend`` pins (1) and
    ``test_computer_use_unsupported.py`` pins (2).

    MODULE-scoped, not function-scoped, deliberately: the registration is a
    single module-global assignment plus a singleton drop, and paying that (plus
    the ``kiro_crew.testing.fake_computer_use`` import) on all ~16k tests would
    be pure overhead. Any test that swaps the backend itself is responsible for
    restoring it (see that file's ``restore_registry`` fixture) — a
    function-scoped fixture here would paper over such a leak instead of letting
    it fail.
    """
    from kiro_crew.computer_use.backend import (
        register_computer_use_backend,
        reset_shared_backend,
    )
    from kiro_crew.testing.fake_computer_use import FakeComputerUseBackend

    register_computer_use_backend(FakeComputerUseBackend)
    reset_shared_backend()
    yield
    register_computer_use_backend(None)
    reset_shared_backend()


@pytest.fixture(autouse=True)
def _reset_platform_context(monkeypatch):
    """Clear the process-global PlatformContext between tests.

    A test that composes a non-default context (e.g. an Amazon-overlay probe)
    must not leak it into the next test.  ``current_context()`` lazily rebuilds
    the standalone default on next access.

    Also pins ``KIROCREW_PROFILE=standalone`` by default so a dev box that has a
    real SSO-marker directory does not make ``boot_platform`` resolve the
    ``amazon`` profile and fail closed (no companion installed) for the many
    pre-existing tests that drive ``run_gateway`` / boot.  A test that wants the
    amazon profile overrides this env via its own ``monkeypatch.setenv`` (it
    runs after this autouse fixture), or composes the context directly via
    ``set_context`` without booting.
    """
    from kiro_crew.platform.bootstrap import _reset_boot_state
    from kiro_crew.platform.context import reset_context

    monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
    reset_context()
    _reset_boot_state()
    yield
    reset_context()
    _reset_boot_state()


@pytest.fixture
def short_sock_dir(tmp_path):
    """A temp dir short enough to hold an AF_UNIX socket path.

    ``sockaddr_un.sun_path`` caps a unix-socket path at ~104 bytes on macOS (108
    on Linux). pytest's ``tmp_path`` is derived from the platform temp root plus
    the test name, and on macOS that root is ``/private/var/folders/<...>/T``,
    which already blows the cap before a filename is appended — so binding under
    ``tmp_path`` fails with ``OSError: AF_UNIX path too long`` on a developer
    machine while passing in CI (Linux, short ``/tmp``).

    Yields a short-rooted dir instead, cleaned up afterwards. Falls back to
    ``tmp_path`` where no short root exists (notably Windows, where AF_UNIX tests
    are skipped anyway), so this never hard-fails on an unusual platform.
    """
    import tempfile

    short_root = "/tmp" if os.path.isdir("/tmp") else None
    if short_root is None:
        yield tmp_path
        return
    path = tempfile.mkdtemp(dir=short_root, prefix="kcsock-")
    try:
        yield pathlib.Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_release_feed_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the update check's network seam unreachable for the whole suite.

    ``handlers.updates._do_update_check`` now has a second branch: any install
    that is NOT a git checkout is compared against the release-channel feed on the
    CDN. Two ordinary things reach it without a test asking to — ``/api/status``
    fires ``_do_update_check`` as a background task once
    ``_UPDATE_CHECK_INTERVAL`` has elapsed (and ``_last_update_check`` starts at
    ``0.0``, so the first call always qualifies), and any direct call in a test
    env with no ``KIROCREW_PROJECT_DIR`` takes the feed branch by definition.

    Without this fixture the suite would make real HTTPS requests to
    ``updates.crew.kiro.dev`` — slow, flaky, offline-hostile, and CI traffic
    nobody asked for. Tests that WANT a feed response stub this same seam, which
    overrides the fixture for that test.

    The refusal is an ``AssertionError`` because that is the loudest signal
    available, but note ``_do_update_check``'s outer ``except Exception`` net will
    convert it into ``error="unknown"`` rather than failing the test — so this is
    a NETWORK guard first and a diagnostic second. A test that means to exercise
    the feed branch must stub the seam and assert on the result.
    """

    async def _refuse(url: str) -> tuple[int, bytes]:
        raise AssertionError(
            f"test reached the real release feed ({url}) — stub "
            "kiro_crew.dashboard.handlers.updates._fetch_feed_bytes instead"
        )

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.updates._fetch_feed_bytes", _refuse, raising=True
    )
