"""Memory- and contention-aware worker budget for pytest-xdist ``-n auto``.

A plain module rather than a conftest, imported by the ROOTDIR ``conftest.py``,
which owns the two hook names. That split is what makes the budget reach all
testpath: living in ``test/conftest.py`` it was never loaded for the
in-package app suites, nor for any directory targeted explicitly, so those
invocations resolved
``-n auto`` to the raw core count and ignored every knob -- and nothing said so,
because an absent budget looks exactly like a budget that decided not to clamp.

It is a separate module, and not simply moved INTO the rootdir conftest, because
the module name ``conftest`` is ambiguous: with pytest's ``prepend`` import mode
``test/`` precedes the repository root on ``sys.path``, so ``import conftest``
from a test in ``test/`` binds ``test/conftest.py`` and can never reach the
rootdir file. A distinct name is reachable from both, and resolves to ONE module
object -- which matters, because the held-slot file descriptors are module state
and a second copy would release nothing.

Why a budget at all: two worktrees each running ``pytest -n auto`` on the same
10-core host took 10 workers EACH, swapped the machine to a load average of ~590,
and completed zero tests in 21 minutes. Memory, not cores, is the binding
constraint -- a worker costs ~1.5 GiB, most of it collecting the suite before it
runs anything -- so a machine that cannot back one worker per core must be told.
"""

from __future__ import annotations

import os
import pathlib
import socket
import warnings

from kiro_crew import platform_compat

_MAX_WORKERS_ENV = "KIROCREW_MAX_TEST_WORKERS"
_SLOT_DIR_ENV = "KIROCREW_TEST_SLOT_DIR"
#: xdist's own ceiling for ``-n auto``. Read here because this hook replaces
#: xdist's default implementation rather than running alongside it.
_XDIST_ENV_CAP = "PYTEST_XDIST_AUTO_NUM_WORKERS"
_DEFAULT_WORKER_CAP = 32
_GIB = 1024**3
# The LIVE memory readings are taken in MiB, because in GiB anything under 1 GiB
# truncates to 0 -- which is the same value they use for "could not determine", and an
# unknown reading is deliberately skipped. See _host_available_mib.
_MIB = 1024**2
# Headroom to reserve per worker.
#
# A worker's cost has two parts. The FLOOR is collection: every xdist worker
# independently collects every testpath -- ~57,000 items -- for ~747 MiB of
# VmHWM before it runs a single test, 99% of it private, so there is no page
# sharing to exploit. On top of that a worker GROWS by roughly 25 MiB per 1,000
# tests it runs, and that growth does not saturate.
#
# The consequence is the whole reason this constant is 2 and must stay there:
# **per-worker footprint is inversely proportional to the worker count.** Fewer
# workers means more tests each, and the growth is per-test, so the projected
# peak is 747 + (57,000 / N) * 0.0255 MiB:
#
#     N=32 -> 792 MiB     N=8 -> 928 MiB     N=2 -> 1473 MiB     N=1 -> 2198 MiB
#
# Measuring on a wide run therefore makes this reservation look 2x too generous
# (a real -n8 worker peaks at 921-1154 MiB) while it is in fact slightly TIGHT
# for the case where the budget actually binds. A divisor sized on the -n8
# number would grant 6 workers on an 8 GiB laptop; those 6 would then run ~9,500
# tests each, want ~6 GiB between them, and swap the machine -- which is the
# incident this budget exists to prevent, reintroduced by "optimizing" it.
#
# So: do NOT lower this on the strength of a measurement taken at high
# parallelism. The number that matters is the footprint at the worker count the
# budget is about to grant, not the one your dev host runs at.
#
# Known limit, stated rather than hidden: at N=1 the projection exceeds 2 GiB, so
# the single-worker floor can outgrow its own reservation. Nothing here can fix
# that -- one worker is already the minimum -- and it is the case where the run is
# slow but survivable rather than parallel and fatal.
#
# This sizes for EXPECTED footprint: it cannot save a host from a genuinely
# leaking worker (one orphaned run was observed at 4.3 GiB RSS), a separate bug.
_GIB_PER_WORKER = 2
# Headroom to reserve per worker against the LIVE availability reading.
#
# Deliberately the same as the static divisor above, because both describe the
# same worker. The two readings differ in KIND -- total RAM is a worst-case bound
# that never moves, availability is already the current headroom -- but that
# argues about how much margin to add on top, and at 2 GiB there is none: it is
# ~1x the measured per-worker peak. Anything less admits more workers than the
# host has memory for at the moment it is asked.
_GIB_PER_WORKER_AVAILABLE = 2

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
    """Total physical RAM in GiB, or 0 when it cannot be determined.

    Routed through ``platform_compat`` rather than reading ``os.sysconf`` directly,
    because that function does not exist on Windows -- so a direct read returns 0
    there, which is this budget's "unknown", which :func:`_bounded_by` SKIPS. The
    result was that Windows had no memory bound at all, static or live.

    GiB, not MiB, because a machine with under 1 GiB of RAM in total is not a
    configuration this suite runs on, and the unit is pinned by existing tests. The
    LIVE reading is in MiB for the opposite reason -- see :func:`_host_available_mib`.
    """
    return platform_compat.host_total_mib() // 1024


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

    Bounding on this in ADDITION to total RAM is what makes the budget protective
    rather than decorative. The flock slots below already stop two pytest runs from
    oversubscribing each other, but they are blind to memory the host is using for
    anything else -- a build, a browser, a running gateway. Sizing 32 workers against
    total RAM on a machine with 2 GiB genuinely free is how a run starts swapping and
    then makes no progress at all, which is the incident the whole budget exists to
    prevent.

    Delegates to :func:`platform_compat.host_available_mib`, which answers on Linux,
    macOS and Windows. That breadth is the whole point of routing through it: a
    reading that only Linux can take makes this bound an identity function on the
    developer laptops the budget most needs to protect, and it fails silently --
    ``0`` means "unknown", :func:`_bounded_by` skips an unknown reading, and the run
    proceeds at the unbounded ceiling with nothing in the log.

    Kept as a named indirection rather than inlined at the call site because the
    budget's tests monkeypatch it to pin a host, and because it is the seam where a
    reading that turns out to be wrong for one platform can be corrected.
    """
    return platform_compat.host_available_mib()


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


def _warn_if_clamped(resolved: int, cap: int, unbudgeted: int) -> None:
    """Say why parallelism is lower than the core count, if it is.

    A run that quietly drops from 10 workers to 1 is indistinguishable from a hang:
    the suite takes forty minutes, nothing explains it, and the two fixes -- free
    some memory, or narrow the run -- are only obvious once the cause is named. So
    the budget explains itself whenever it binds.

    Two distinct causes, because the remedies differ: memory says the machine
    cannot back more workers right now, contention says another run holds them and
    capacity comes back on its own. Silent when neither binds, which is every CI
    runner (core-bound) and every idle workstation, so the common path pays nothing.
    """
    if cap < unbudgeted:  # a memory reading bound this run
        available = _host_available_mib()
        # Only report a reading we actually have; 0 means unknown, and the static
        # total-RAM bound is what bound us in that case.
        free = f"{available / 1024:.1f} GiB free" if available else "memory-bounded"
        warnings.warn(
            f"xdist worker budget: {cap} of {unbudgeted} workers ({free}, "
            f"{_host_total_gib()} GiB installed). Each worker needs about "
            f"{_GIB_PER_WORKER_AVAILABLE} GiB, mostly to collect the suite. A run this "
            "narrow is slow, not stuck -- free some memory, run a subset "
            "(pytest test/test_thing.py), or pass an explicit -n <N> to bypass "
            "this budget.",
            stacklevel=1,
        )
    if resolved < cap:  # slots were held by another run on this host
        warnings.warn(
            f"xdist worker budget: {resolved} of {cap} workers -- another run on this "
            "host holds the rest. Slow, not stuck; capacity returns when that run exits.",
            stacklevel=1,
        )


def resolve_workers() -> int:
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
    2. ``PYTEST_XDIST_AUTO_NUM_WORKERS``, xdist's OWN knob. Honoured as a ceiling
       because this hook, being ``firstresult``, runs instead of xdist's default
       implementation and would otherwise discard it silently -- and Kiro Crew itself
       seeds that variable with a memory-aware cap at every agent spawn boundary
       (``resource_status.inject_xdist_auto_cap``). A run that inherits a deliberate
       cap must not be handed more workers than it asked for.
    3. What is free on the host right now (see :func:`_live_memory_bounded_cap`). This
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
    cores = os.cpu_count() or 1
    # What the run would have got with no memory reading and no contention. Only used
    # to decide whether to SAY something, never to grant.
    unbudgeted = min(cores, max(1, _int_env(_MAX_WORKERS_ENV, _DEFAULT_WORKER_CAP)))
    capacity = _static_memory_bounded_capacity(cores)
    # Only a POSITIVE value is a ceiling. Unset, empty, non-numeric, zero and
    # negative all fall back to inert -- a typo must not silently serialize the
    # suite, which is what a negative value would do once floored at one worker.
    raw_env_cap = _int_env(_XDIST_ENV_CAP, 0)
    env_cap = raw_env_cap if raw_env_cap > 0 else unbudgeted
    cap = _live_memory_bounded_cap(min(capacity, unbudgeted, env_cap))
    resolved = _claim_worker_slots(capacity, cap)
    _warn_if_clamped(resolved, cap, unbudgeted)
    return resolved


def release_worker_slots() -> None:
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
