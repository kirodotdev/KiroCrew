"""The live ``/api/system`` memory probe publishes only what it read, and says so
when it read nothing.

The Linux branch of ``_collect_system_metrics`` reads ``/proc/meminfo`` on every
collection. Two shapes of that file carry no usable reading and must not be
turned into numbers:

1. a ``/proc/meminfo`` that parses but carries no ``MemTotal:`` line (or a zero
   one): a total defaulted to ``0`` would publish ``mem_total_gb: 0.0`` beside
   ``mem_used_gb = 0 - MemAvailable``, a NEGATIVE used figure, and overwrite a
   total the static probe did read;
2. a read or parse that raises, which the enclosing ``except Exception`` must
   not swallow without a trace.

The contract these tests lock in:

- the three live memory keys (``mem_total_gb``, ``mem_used_gb``,
  ``mem_free_gb``) are published together from one usable reading or not at
  all, on every platform -- never a fake ``0.0`` total, never a negative used
  figure, and a total the static probe DID read is left in place;
- every way the probe comes up empty leaves exactly one log record naming the
  cause (the raised exception via ``exc_info`` where there is one): WARNING the
  first time in a process, DEBUG after that, because a persistently unreadable
  ``/proc/meminfo`` would otherwise warn on every collection;
- a failed memory probe never drops the rest of the payload (CPU, IP, disk);
- the happy path is byte-for-byte what ``test_handlers_system_linux_mem.py``
  already pins, and logs nothing.

The static probe (``_get_static_system_info``) is a different code path and is
stubbed out here.
"""

from __future__ import annotations

import builtins
import logging
import subprocess
from io import StringIO
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import handlers_system

_GIB_KB = 1024 * 1024
_MEM_KEYS = ("mem_total_gb", "mem_used_gb", "mem_free_gb")

_HEALTHY = (
    f"MemTotal:       {100 * _GIB_KB} kB\n"
    f"MemFree:        {10 * _GIB_KB} kB\n"
    f"MemAvailable:   {65 * _GIB_KB} kB\n"
)
# MemAvailable present, MemTotal absent: the shape that produced -2.0 used.
_NO_MEMTOTAL = f"MemFree:        {1 * _GIB_KB} kB\nMemAvailable:   {2 * _GIB_KB} kB\n"
_ZERO_MEMTOTAL = f"MemTotal:       0 kB\nMemAvailable:   {2 * _GIB_KB} kB\n"
_BAD_MEMTOTAL = f"MemTotal:       lots kB\nMemAvailable:   {2 * _GIB_KB} kB\n"


def _fake_open(meminfo_text: str | None, exc: BaseException | None = None):
    """``open`` replacement: serves ``meminfo_text`` for /proc/meminfo (or raises
    ``exc`` there) and delegates every other path to the real ``open``."""
    real_open = builtins.open

    def _open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == "/proc/meminfo":
            if exc is not None:
                raise exc
            return StringIO(meminfo_text or "")
        return real_open(path, *args, **kwargs)

    return _open


@pytest.fixture
def unreported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test as a process that has not yet warned about the probe, and
    put the flag back afterwards so the order tests run in cannot leak a level."""
    monkeypatch.setattr(handlers_system, "_live_mem_probe_reported", False)


def _collect_linux(
    meminfo_text: str | None,
    exc: BaseException | None = None,
    static: dict[str, object] | None = None,
) -> dict[str, object]:
    """Run ``_collect_system_metrics`` hermetically on the Linux branch with an
    injected /proc/meminfo -- the seam ``test_handlers_system_linux_mem.py``
    uses: static info stubbed, CPU and local-IP probes pinned."""
    with (
        patch("sys.platform", "linux"),
        patch.object(handlers_system, "_get_static_system_info", return_value=dict(static or {})),
        patch.object(handlers_system, "_system_cpu_pct_from_proc_stat", return_value=5.0),
        patch("builtins.open", side_effect=_fake_open(meminfo_text, exc)),
        patch.object(handlers_system, "_local_ip", return_value="127.0.0.1"),
    ):
        return handlers_system._collect_system_metrics()


def _probe_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only this module's probe records: an unfiltered count would also see
    whatever another thread in the worker logged."""
    return [
        r
        for r in caplog.records
        if r.name == handlers_system.logger.name and "mem_total_gb" in r.getMessage()
    ]


def _assert_rest_of_payload_present(data: dict[str, object]) -> None:
    """A failed memory probe must not take CPU, IP or disk down with it."""
    assert data["cpu_pct"] == 5.0
    assert data["ip"] == "127.0.0.1"
    assert "disk_total_gb" in data


@pytest.mark.usefixtures("unreported")
class TestLinuxUnusableMemTotal:
    """Shape 1: the file parses but there is no usable MemTotal."""

    @pytest.mark.parametrize(
        "meminfo", [_NO_MEMTOTAL, _ZERO_MEMTOTAL, ""], ids=["no-line", "zero", "empty-file"]
    )
    def test_omits_all_three_keys_and_warns(
        self, meminfo: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name):
            data = _collect_linux(meminfo)
        for key in _MEM_KEYS:
            assert key not in data, (key, data.get(key))
        records = _probe_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "no usable MemTotal line" in records[0].getMessage()
        assert records[0].exc_info is None  # nothing raised; nothing to attach
        _assert_rest_of_payload_present(data)

    def test_static_total_survives(self, caplog: pytest.LogCaptureFixture) -> None:
        """The payload starts as a copy of the static dict, so a live total
        defaulted to 0.0 would overwrite a real one. The static total must
        stand with only the live-derived used/free absent -- the documented
        'total but no used' frame the frontend already treats as ordinary."""
        with caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name):
            data = _collect_linux(_NO_MEMTOTAL, static={"mem_total_gb": 61.0})
        assert data["mem_total_gb"] == 61.0
        assert "mem_used_gb" not in data
        assert "mem_free_gb" not in data
        assert len(_probe_records(caplog)) == 1


@pytest.mark.usefixtures("unreported")
class TestLinuxReadOrParseRaises:
    """Shape 2: the open/read/parse raises inside the memory block."""

    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(13, "Permission denied", "/proc/meminfo"),
            FileNotFoundError(2, "No such file or directory", "/proc/meminfo"),
            OSError(5, "Input/output error"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_read_raises_logs_the_exception(
        self, exc: BaseException, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name):
            data = _collect_linux(None, exc=exc)
        for key in _MEM_KEYS:
            assert key not in data
        records = _probe_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "system-wide memory probe failed" in records[0].getMessage()
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc
        _assert_rest_of_payload_present(data)

    def test_malformed_line_drops_the_reading_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pins a residual: the parser is all-or-nothing, so ONE malformed line
        (here an unparseable MemTotal value) drops the whole reading. The
        ValueError it raises must reach the log, not the except's floor."""
        with caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name):
            data = _collect_linux(_BAD_MEMTOTAL)
        for key in _MEM_KEYS:
            assert key not in data
        records = _probe_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert isinstance(records[0].exc_info[1], ValueError)


@pytest.mark.usefixtures("unreported")
class TestOneWarningPerProcess:
    def test_repeat_failures_warn_once_then_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """/api/system is polled every 2 s while the Performance tab is open; a
        host whose /proc/meminfo stays unreadable gets one WARNING, then one
        DEBUG per poll -- still a record per failure, never a flood."""
        with caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name):
            _collect_linux(_NO_MEMTOTAL)
            _collect_linux(None, exc=OSError(5, "Input/output error"))
            _collect_linux(_NO_MEMTOTAL)
        levels = [r.levelno for r in _probe_records(caplog)]
        assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]

    def test_healthy_poll_does_not_consume_the_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A healthy reading logs nothing and leaves the one WARNING unspent for
        the first failure that follows it."""
        with caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name):
            _collect_linux(_HEALTHY)
            assert _probe_records(caplog) == []
            _collect_linux(_NO_MEMTOTAL)
        assert [r.levelno for r in _probe_records(caplog)] == [logging.WARNING]


@pytest.mark.usefixtures("unreported")
class TestHappyPathUnchanged:
    def test_real_figures_and_silence(self, caplog: pytest.LogCaptureFixture) -> None:
        """Same numbers test_handlers_system_linux_mem.py pins (used = total -
        MemAvailable), and not a single probe record."""
        with caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name):
            data = _collect_linux(_HEALTHY)
        assert data["mem_total_gb"] == 100.0
        assert data["mem_free_gb"] == 65.0
        assert data["mem_used_gb"] == 35.0
        assert _probe_records(caplog) == []


@pytest.mark.usefixtures("unreported")
class TestOtherPlatformsSameBlock:
    """The same try/except wraps the darwin and win32 branches; their empty-handed
    shapes land in the same helper. Neither can publish the Linux defect's fake
    0.0 / negative pair: darwin's ``_macos_memory_gb`` clamps used into
    ``[0, total]`` and win32 publishes nothing when ``system_memory()`` is None."""

    def test_windows_empty_probe_omits_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name),
            patch("sys.platform", "win32"),
            patch.object(handlers_system, "_get_static_system_info", return_value={}),
            patch.object(platform_compat, "system_memory", return_value=None),
            patch.object(handlers_system, "_local_ip", return_value="127.0.0.1"),
        ):
            data = handlers_system._collect_system_metrics()
        for key in _MEM_KEYS:
            assert key not in data
        records = _probe_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "GlobalMemoryStatusEx returned nothing" in records[0].getMessage()
        assert records[0].exc_info is None  # system_memory() folds failures into None

    def test_darwin_sysctl_failure_logs_the_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        exc = subprocess.CalledProcessError(1, ["sysctl", "-n", "hw.memsize"])
        with (
            caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name),
            patch("sys.platform", "darwin"),
            patch.object(handlers_system, "_get_static_system_info", return_value={}),
            patch.object(handlers_system, "_system_cpu_pct_from_proc_stat", return_value=5.0),
            patch.object(handlers_system.subprocess, "check_output", side_effect=exc),
            patch.object(handlers_system, "_local_ip", return_value="127.0.0.1"),
        ):
            data = handlers_system._collect_system_metrics()
        for key in _MEM_KEYS:
            assert key not in data
        records = _probe_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc

    def test_darwin_vm_stat_failure_publishes_no_total_either(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """sysctl answers, then vm_stat fails: the live total must not land in
        the payload beside a log line saying the live figures are unavailable,
        so the darwin branch may assign it only after both reads succeed."""
        exc = subprocess.CalledProcessError(1, ["vm_stat"])

        def _check_output(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            if cmd == [handlers_system._SYSCTL, "-n", "hw.memsize"]:
                return b"68719476736\n"  # 64 GiB
            if cmd == [handlers_system._VM_STAT]:
                raise exc
            raise AssertionError(f"unexpected subprocess in the memory block: {cmd!r}")

        with (
            caplog.at_level(logging.DEBUG, logger=handlers_system.logger.name),
            patch("sys.platform", "darwin"),
            patch.object(handlers_system, "_get_static_system_info", return_value={}),
            patch.object(handlers_system, "_system_cpu_pct_from_proc_stat", return_value=5.0),
            patch.object(handlers_system.subprocess, "check_output", side_effect=_check_output),
            patch.object(handlers_system, "_local_ip", return_value="127.0.0.1"),
        ):
            data = handlers_system._collect_system_metrics()
        for key in _MEM_KEYS:
            assert key not in data, (key, data.get(key))
        records = _probe_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc
