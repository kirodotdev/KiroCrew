"""The Determinism contract's opt-in fixtures (``manual_clock``,
``seeded_rng``, ``local_tz``) and the detectors behind the report-only floors: zone,
shared-pool and package-graph snapshots, and the network and process-kill audit.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.
"""

from __future__ import annotations

import os
import sys
import time

import pytest


@pytest.fixture
def manual_clock():
    """A fresh ``kiro_crew.testing.clock.ManualClock`` (Determinism contract D2, D3).

    Nothing is installed until the test asks:
    ``manual_clock.install(monkeypatch, subject_module)`` replaces that module's own
    ``time`` binding through the test's ``monkeypatch``, so the stdlib clock, the event
    loop and pytest-timeout keep real time.
    """
    from kiro_crew.testing.clock import ManualClock

    return ManualClock()


@pytest.fixture
def seeded_rng(request):
    """A ``random.Random`` seeded from this test's node id (Determinism contract D6).

    The seed is the same in every run and on every host, and it is printed in the
    report of a failing test, so the draws that failed can be replayed with
    ``kiro_crew.testing.ids.seeded_rng(nodeid, seed=<printed seed>)``.
    """
    from kiro_crew.testing import ids

    nodeid = _nodeid_without_xdist_group(request.node)
    seed = ids.seed_for(nodeid)
    request.node.add_report_section("setup", "seeded_rng", f"seed={seed}")
    return ids.seeded_rng(nodeid, seed=seed)


def _nodeid_without_xdist_group(node) -> str:
    """``node.nodeid`` without the ``@<group>`` suffix ``--dist loadgroup`` appends.

    xdist renames an ``xdist_group`` test's node id on the worker, so a seed taken
    from the raw id would differ between a ``-n0`` run and a CI shard.
    """
    nodeid = node.nodeid
    marker = node.get_closest_marker("xdist_group")
    if marker is not None:
        # The same resolution xdist uses to build the suffix.
        name = marker.args[0] if marker.args else marker.kwargs.get("name", "default")
        suffix = f"@{name}"
        if nodeid.endswith(suffix):
            return nodeid[: -len(suffix)]
    return nodeid


@pytest.fixture
def local_tz():
    """Set the process time zone for this test: ``local_tz("Pacific/Kiritimati")`` (D5).

    Sets ``TZ`` and calls ``time.tzset()``, then restores both at teardown, so a test
    about local time runs under the zone it names instead of the host's. ``tzset`` is
    POSIX-only, so the test is skipped where it is missing; inject the zone where the
    product allows it to cover Windows. An unknown zone name raises at the call.
    """
    import time as host_time

    if not hasattr(host_time, "tzset"):
        pytest.skip("time.tzset is POSIX-only; inject the zone to cover this platform")
    mp = pytest.MonkeyPatch()

    def set_zone(zone: str) -> str:
        import datetime
        import zoneinfo

        info = zoneinfo.ZoneInfo(zone)
        mp.setenv("TZ", zone)
        host_time.tzset()
        # A C library without that zone's data falls back to UTC silently; refuse
        # rather than run a local-time test in the wrong zone.
        reference = 1_700_000_000
        expected = info.utcoffset(datetime.datetime.fromtimestamp(reference, info))
        applied = host_time.localtime(reference).tm_gmtoff
        if expected is None or applied != int(expected.total_seconds()):
            raise RuntimeError(
                f"the C library did not apply TZ={zone!r} (offset {applied}s, want "
                f"{expected}); is the system tzdata installed?"
            )
        return zone

    try:
        yield set_zone
    finally:
        mp.undo()
        host_time.tzset()


def _runner_timeout_secs(item) -> float | None:
    """The timeout pytest-timeout enforces on ``item``, read through public pytest API.

    The same order the plugin resolves it in: a ``timeout`` marker, then
    ``--timeout``, then ``PYTEST_TIMEOUT``, then the ini value. ``None`` when the
    plugin is absent or the value is not a positive number.
    """
    raw = None
    marker = item.get_closest_marker("timeout")
    if marker is not None:
        raw = marker.kwargs.get("timeout", marker.args[0] if marker.args else None)
    if raw is None:
        raw = item.config.getoption("timeout", None)
    if raw is None:
        raw = os.environ.get("PYTEST_TIMEOUT")
    if raw is None:
        try:
            raw = item.config.getini("timeout") or None
        except ValueError:
            return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


#: The process's own clock and zone module under a name no test rebinds: an inner
#: session in ``test_xdist_escaped_failure_guard`` replaces the rootdir conftest's ``time``.
_host_time = time


#: Turns the network floor from a report into a refusal: an off-loopback connect,
#: send or name lookup raises at the call and fails the test at teardown.
_NET_STRICT_ENV = "KIROCREW_NET_STRICT"

#: Turns the package-attribute floor from restore-and-report into restore-and-fail.
_PKG_ATTR_STRICT_ENV = "KIROCREW_PKG_ATTR_STRICT"


# The process time zone. Nothing pins it (a pinned zone would hide every test that reads
# the host's), so a test that changes it changes every later test on the worker. The
# floor reports the test that changed it; it does not put the zone back.
_TZ_STATE = pytest.StashKey[tuple]()


def _process_zone() -> tuple:
    return (
        os.environ.get("TZ"),
        tuple(_host_time.tzname),
        _host_time.timezone,
        _host_time.altzone,
        _host_time.daylight,
    )


def pytest_report_header(config: pytest.Config) -> str:
    tz = os.environ.get("TZ")
    return (
        f"time zone: {'/'.join(_host_time.tzname)} (TZ={tz!r}, utcoffset={-_host_time.timezone}s)"
    )


# The shared ``kiro_crew.executors`` pools. Work a test hands one and does not wait for
# runs inside whichever test comes next on the worker, against that test's home.
_POOL_STATE = pytest.StashKey[dict]()


def _pool_activity() -> dict[str, tuple[int, int]]:
    """``name -> (busy, queued)`` for every loaded shared pool that has work."""
    executors = sys.modules.get("kiro_crew.executors")
    if executors is None:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    active: dict[str, tuple[int, int]] = {}
    for name, pool in list(vars(executors).items()):
        if not isinstance(pool, ThreadPoolExecutor) or name == "_crew_log_pool":
            continue
        try:
            if pool._shutdown:
                continue  # its queue holds the wake-up sentinels, not work
            busy = len(pool._threads) - pool._idle_semaphore._value
            queued = pool._work_queue.qsize()
        except Exception:  # noqa: BLE001 - CPython internals; skip a pool that changed shape
            continue
        if busy > 0 or queued > 0:
            active[name] = (max(busy, 0), queued)
    return active


# ``kiro_crew`` package attributes. ``monkeypatch.setattr("kiro_crew.mod.name", ...)``
# resolves each dotted component as an ATTRIBUTE of its parent package, so a test that
# evicts ``kiro_crew.mod`` from ``sys.modules`` and imports it again without putting the
# parent's attribute back, or that replaces a module the others already hold, breaks
# later tests far from the cause (2,167 setup errors on one Windows worker). The floor
# restores what the test inherited and names the test that changed it.
class _PackageTable:
    """The inherited ``kiro_crew`` module graph: one row per bound submodule."""

    __slots__ = ("modules_len", "root", "root_items", "names", "objs", "parent_dicts", "childs")

    def __init__(self) -> None:
        self.modules_len = -1
        self.root = None
        self.root_items: list[tuple[str, object]] = []
        self.names: list[str] = []
        self.objs: list[object] = []
        self.parent_dicts: list[dict] = []
        self.childs: list[str] = []

    def rebuild(self) -> None:
        modules = sys.modules
        self.root = modules.get("kiro_crew")
        self.root_items = (
            [(k, v) for k, v in vars(self.root).items() if not k.startswith("__")]
            if self.root is not None
            else []
        )
        names, objs, parent_dicts, childs = [], [], [], []
        for name, module in list(modules.items()):
            if module is None or not name.startswith("kiro_crew."):
                continue
            parent_name, _, child = name.rpartition(".")
            parent_dict = getattr(modules.get(parent_name), "__dict__", None)
            # Only rows that are consistent now: a facade may export a name that
            # shadows a submodule on purpose, and that is not this floor's business.
            if parent_dict is None or parent_dict.get(child) is not module:
                continue
            names.append(name)
            objs.append(module)
            parent_dicts.append(parent_dict)
            childs.append(child)
        self.names, self.objs, self.parent_dicts, self.childs = names, objs, parent_dicts, childs
        self.modules_len = len(modules)

    def intact(self) -> bool:
        modules = sys.modules
        if modules.get("kiro_crew") is not self.root:
            return False
        if list(map(modules.get, self.names)) != self.objs:
            return False
        if list(map(dict.get, self.parent_dicts, self.childs)) != self.objs:
            return False
        root_dict = getattr(self.root, "__dict__", None)
        return root_dict is None or all(root_dict.get(k) is v for k, v in self.root_items)

    def restore(self) -> list[str]:
        """Put back every inherited binding, and return what had changed."""
        changed: list[str] = []
        modules = sys.modules
        if self.root is not None and modules.get("kiro_crew") is not self.root:
            changed.append("sys.modules['kiro_crew'] replaced")
            modules["kiro_crew"] = self.root
        for name, obj, parent_dict, child in zip(
            self.names, self.objs, self.parent_dicts, self.childs
        ):
            current = modules.get(name)
            if current is not obj:
                changed.append(
                    f"sys.modules[{name!r}] {'removed' if current is None else 'replaced'}"
                )
                modules[name] = obj
            if parent_dict.get(child) is not obj:
                changed.append(f"{name.rpartition('.')[0]}.{child} rebound")
                parent_dict[child] = obj
        root_dict = getattr(self.root, "__dict__", None)
        if root_dict is not None:
            for key, value in self.root_items:
                if root_dict.get(key) is not value:
                    changed.append(f"kiro_crew.{key} rebound")
                    root_dict[key] = value
        return changed


_PACKAGE_TABLE = _PackageTable()


def _bind_unbound_kiro_crew_modules() -> list[str]:
    """Bind back a loaded ``kiro_crew`` submodule its parent package lost."""
    bound: list[str] = []
    modules = sys.modules
    for name, module in list(modules.items()):
        if module is None or not name.startswith("kiro_crew."):
            continue
        parent_name, _, child = name.rpartition(".")
        parent_dict = getattr(modules.get(parent_name), "__dict__", None)
        if parent_dict is not None and child not in parent_dict:
            parent_dict[child] = module
            bound.append(f"{parent_name}.{child} was unbound")
    return bound


# Network and process-kill audit. One ``sys.addaudithook`` hook per process, installed
# from ``pytest_configure`` (an audit hook cannot be removed, and the rootdir conftest is
# re-executed by several tests, so nothing irreversible happens at import). It is
# active only while a test runs, in the worker process itself (never in a forked
# child), and it never raises unless a strict switch asks it to: an exception from an
# audit hook aborts the audited call.
class _AuditState:
    __slots__ = ("active", "nodeid", "pid", "parent_pid", "network_ok", "strict_net", "seen")

    def __init__(self) -> None:
        self.active = False
        self.nodeid = ""
        # nosemgrep: kirocrew.mock-pid-allocatable -- the audit state's own pid slot that setup fills from os.getpid() and not a mock pid
        self.pid = 0
        self.parent_pid = 0
        self.network_ok = False
        self.strict_net = False
        self.seen: dict[tuple[str, str], int] = {}


_AUDITED_EVENTS = frozenset(
    {
        "socket.connect",
        "socket.sendto",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "os.kill",
        "os.killpg",
        "subprocess.Popen",
    }
)

#: ``tempfile``'s own audit events, fired by every ``mkstemp``/``mkdtemp`` -- and so by
#: ``NamedTemporaryFile``/``TemporaryDirectory``, which are built on them -- with the
#: path about to be created. Watched for one question: which test made an entry the
#: session teardown later finds left behind. See ``_note_tmp_creator``.
_TMP_AUDITED_EVENTS = frozenset({"tempfile.mkstemp", "tempfile.mkdtemp"})

#: One lookup on the hot path: the hook sees every audit event in the process.
_HOOKED_EVENTS = _AUDITED_EVENTS | _TMP_AUDITED_EVENTS


class OffLoopbackNetworkRefused(ConnectionRefusedError):
    """Raised under ``KIROCREW_NET_STRICT=1`` when a test reaches past loopback."""


def _is_loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%", 1)[0]
    if host in ("", "0.0.0.0", "::", "localhost") or host.endswith(".localhost"):
        return True
    import ipaddress

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def _is_address_literal(host) -> bool:
    import ipaddress

    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        ipaddress.ip_address(str(host).strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _network_finding(event: str, args: tuple, state: _AuditState):
    import socket as _socket

    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
        host = args[0] if args else None
        if event == "socket.gethostbyaddr" and not _is_loopback_host(host):
            return ("network", f"reverse lookup {host!r}")
        if _is_loopback_host(host) or _is_address_literal(host):
            return None  # a numeric host resolves without asking anyone
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
        own = _socket.gethostname()
        if name in (own, own.split(".", 1)[0]):
            return ("network", f"own-hostname lookup {name!r}")
        return ("network", f"name lookup {name!r}")
    sock, address = args[0], args[1] if len(args) > 1 else None
    family = getattr(sock, "family", None)
    if family not in (_socket.AF_INET, _socket.AF_INET6) or not isinstance(address, tuple):
        return None
    if _is_loopback_host(address[0]):
        return None
    if event == "socket.sendto":
        kind = "sendto"
    elif getattr(sock, "type", None) == _socket.SOCK_DGRAM:
        kind = "udp-route-probe"  # a datagram connect picks a route and sends nothing
    else:
        kind = "connect"
    return ("network", f"{kind} {address[0]}:{address[1] if len(address) > 1 else '?'}")


def _kill_finding(event: str, args: tuple, state: _AuditState):
    if event == "subprocess.Popen":
        argv = args[1] if len(args) > 1 else None
        if isinstance(argv, bytes):
            argv = argv.decode("utf-8", "replace")
        if isinstance(argv, str):
            # Windows hands the audit event one command line, not a list.
            argv = [word.strip('"') for word in argv.split()]
        try:
            words = [str(word) for word in (argv or ())]
        except Exception:  # noqa: BLE001
            return None
        if not words or os.path.basename(words[0]).lower() not in ("taskkill", "taskkill.exe"):
            return None
        lowered = [word.lower() for word in words]
        if "/im" in lowered:
            return ("kill", f"taskkill by image name: {' '.join(words[1:])}")
        if "/pid" in lowered and lowered.index("/pid") + 1 < len(words):
            try:
                target = int(words[lowered.index("/pid") + 1])
            except ValueError:
                return None
            return _classify_kill_target(target, "taskkill", state)
        return None
    pid, sig = args[0], args[1] if len(args) > 1 else None
    if os.name != "nt" and sig == 0:
        return None  # a POSIX liveness probe signals nothing
    if os.name != "nt" and event == "os.kill" and isinstance(pid, int) and pid < -1:
        # ``kill(-pgid, sig)`` is a group signal: classify it as the killpg it is,
        # so signalling a child's own group is not read as a broadcast.
        event, pid = "os.killpg", -pid
    if event == "os.killpg":
        own_group = os.getpgrp() if hasattr(os, "getpgrp") else None
        if pid == own_group:
            return ("kill", f"killpg of the worker's own process group (signal {sig})")
        return _classify_kill_target(pid, f"killpg signal {sig}", state, group=True)
    if pid == state.pid and _handled_signal(sig):
        return None  # a test of the worker's own handler
    return _classify_kill_target(pid, f"signal {sig}", state)


def _handled_signal(sig) -> bool:
    """Whether the process has its own handler for *sig* (so a self-signal is not lethal)."""
    import signal as _signal

    try:
        handler = _signal.getsignal(sig)
    except (ValueError, TypeError, OSError):
        return False
    return handler is _signal.SIG_IGN or (
        callable(handler) and handler is not _signal.default_int_handler
    )


def _classify_kill_target(pid, what: str, state: _AuditState, *, group: bool = False):
    if not isinstance(pid, int):
        return None
    if pid <= 0:
        return ("kill", f"{what} to pid {pid} (a process group, possibly the worker's own)")
    if pid == state.pid:
        return ("kill", f"{what} to the worker itself")
    if pid == state.parent_pid:
        return ("kill", f"{what} to the xdist controller (pid {pid})")
    if group or not sys.platform.startswith("linux") or _is_descendant(pid, state.pid):
        return None
    owner = _another_run_member(pid, state)
    return None if owner is None else ("kill", f"{what} to pid {pid}, {owner}")


def _is_descendant(pid: int, ancestor: int) -> bool:
    """Whether *pid* descends from *ancestor*, read from ``/proc`` (Linux)."""
    current = pid
    for _ in range(16):
        try:
            with open(f"/proc/{current}/stat", "rb") as handle:
                stat = handle.read()
        except OSError:
            return True  # gone, or never existed: nothing reached a live foreign process
        try:
            fields = stat[stat.rindex(b")") + 2 :].split()
            if fields[0] == b"Z":
                return True  # a zombie: the signal reaches nothing that runs
            current = int(fields[1])
        except (ValueError, IndexError):
            return True
        if current == ancestor:
            return True
        if current <= 1:
            return False
    return False


def _another_run_member(pid: int, state: _AuditState) -> str | None:
    """What *pid* is, when it is a process this test had no business signalling (Linux).

    Another xdist worker is a child of the controller, so its parent in
    ``/proc/<pid>/stat`` is the worker's own parent. A process of another account is
    told by the owner of ``/proc/<pid>``. A process this worker spawned and lost from
    its parent chain (a ``setsid`` grandchild, an orphan) is neither, and so is not
    reported, nor is one that is gone.
    """
    try:
        owner = os.stat(f"/proc/{pid}").st_uid
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return None
    if hasattr(os, "getuid") and owner != os.getuid():
        return "a process of another account"
    try:
        parent = int(stat[stat.rindex(b")") + 2 :].split()[1])
    except (ValueError, IndexError):
        return None
    if parent == state.parent_pid:
        return "another child of this process's parent (under xdist, a sibling worker)"
    return None
