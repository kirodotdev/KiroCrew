"""A failed total-memory probe must be diagnosable and distinguishable from 0 GB.

``_get_static_system_info`` reads ``/proc/meminfo`` on Linux under a broad
``except Exception``. A read or parse failure leaves ``mem_total_gb`` out of
the static dict, and the dict is cached for the life of the process, so a
single failed read decides what every later ``/api/status`` call reports
until restart. The contract these tests lock in:

- every way the probe can come up empty logs a WARNING naming the cause (the
  raised exception via ``exc_info`` where there is one), so the miss is
  diagnosable from the product's own logs;
- ``/api/status`` projects an unavailable total as ``null`` -- the same
  "unknown, never a fake 0" convention ``cron_jobs``/``lessons`` use -- so a
  reader can tell a failed probe apart from a host that really reports 0 GB;
- the happy path is untouched (a readable ``MemTotal:`` yields the real
  figure and logs nothing).

The static dict itself still OMITS the key when unavailable (``session_memory``
reads it with a bare ``.get`` and the ``/api/system`` assembler re-probes
live), so only the ``/status`` projection carries the ``null``.
"""

from __future__ import annotations

import builtins
import json
import logging
import time
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import handlers_system

_GIB_KB = 1024 * 1024
_MEMINFO_OK = f"MemTotal:       {61 * _GIB_KB} kB\nMemFree:  {10 * _GIB_KB} kB\n"


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
def fresh_static_cache():
    """Reset the once-per-process static-info cache around a test so each case
    runs the probe, then restore whatever was cached before."""
    saved = handlers_system._STATIC_SYSTEM_INFO
    handlers_system._STATIC_SYSTEM_INFO = None
    try:
        yield
    finally:
        handlers_system._STATIC_SYSTEM_INFO = saved


def _probe_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == handlers_system.logger.name and "mem_total_gb" in r.getMessage()
    ]


class TestLinuxProbeFailureIsLogged:
    """AC1: when the /proc/meminfo read fails, a diagnostic names the reason."""

    @pytest.mark.usefixtures("fresh_static_cache")
    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(13, "Permission denied", "/proc/meminfo"),
            FileNotFoundError(2, "No such file or directory", "/proc/meminfo"),
            OSError(5, "Input/output error"),
        ],
        ids=["permission-denied", "missing", "eio"],
    )
    def test_read_failure_logs_with_exception_and_omits_key(
        self, exc: BaseException, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=handlers_system.logger.name)
        with (
            patch("sys.platform", "linux"),
            patch("builtins.open", side_effect=_fake_open(None, exc)),
        ):
            info = handlers_system._get_static_system_info()

        assert "mem_total_gb" not in info, "a failed probe must not invent a figure"
        records = _probe_records(caplog)
        assert len(records) == 1, f"expected exactly one diagnostic, got {records!r}"
        (rec,) = records
        assert rec.levelno == logging.WARNING
        assert "/proc/meminfo" in rec.getMessage()
        # The failure REASON travels with the line: the record carries the
        # exception that was raised, not just a generic "failed".
        assert rec.exc_info is not None and rec.exc_info[1] is exc

    @pytest.mark.usefixtures("fresh_static_cache")
    def test_parse_failure_logs_with_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        """A malformed MemTotal value raises ValueError inside the parse; that
        is the same silent swallow as a read failure and must log the same way."""
        caplog.set_level(logging.DEBUG, logger=handlers_system.logger.name)
        with (
            patch("sys.platform", "linux"),
            patch("builtins.open", side_effect=_fake_open("MemTotal:  not-a-number kB\n")),
        ):
            info = handlers_system._get_static_system_info()

        assert "mem_total_gb" not in info
        (rec,) = _probe_records(caplog)
        assert rec.levelno == logging.WARNING
        assert rec.exc_info is not None and isinstance(rec.exc_info[1], ValueError)

    @pytest.mark.usefixtures("fresh_static_cache")
    def test_missing_memtotal_line_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """The third way the probe comes up empty raises nothing at all: the
        file reads fine but has no ``MemTotal:`` line, so the loop simply ends
        and an except-side log alone would never see it."""
        caplog.set_level(logging.DEBUG, logger=handlers_system.logger.name)
        with (
            patch("sys.platform", "linux"),
            patch("builtins.open", side_effect=_fake_open("MemFree:  1024 kB\nBuffers: 0 kB\n")),
        ):
            info = handlers_system._get_static_system_info()

        assert "mem_total_gb" not in info
        (rec,) = _probe_records(caplog)
        assert rec.levelno == logging.WARNING
        assert "MemTotal" in rec.getMessage()

    @pytest.mark.usefixtures("fresh_static_cache")
    def test_happy_path_unchanged_and_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """Unchanged: a readable MemTotal yields the real figure and logs nothing."""
        caplog.set_level(logging.DEBUG, logger=handlers_system.logger.name)
        with (
            patch("sys.platform", "linux"),
            patch("builtins.open", side_effect=_fake_open(_MEMINFO_OK)),
        ):
            info = handlers_system._get_static_system_info()

        assert info["mem_total_gb"] == 61.0
        assert _probe_records(caplog) == []


class TestSiblingProbesLogTheSameWay:
    """The darwin and win32 probes sit beside the Linux one with the same
    omit-on-failure shape, so they carry the same one-line diagnostic."""

    @pytest.mark.usefixtures("fresh_static_cache")
    def test_darwin_sysctl_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=handlers_system.logger.name)
        boom = OSError(2, "No such file or directory", "/usr/sbin/sysctl")
        with (
            patch("sys.platform", "darwin"),
            patch.object(handlers_system.subprocess, "check_output", side_effect=boom),
        ):
            info = handlers_system._get_static_system_info()

        assert "mem_total_gb" not in info
        (rec,) = _probe_records(caplog)
        assert rec.levelno == logging.WARNING
        assert "hw.memsize" in rec.getMessage()
        assert rec.exc_info is not None and rec.exc_info[1] is boom

    @pytest.mark.usefixtures("fresh_static_cache")
    def test_win32_memory_status_failure_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=handlers_system.logger.name)
        with (
            patch("sys.platform", "win32"),
            patch.object(handlers_system.platform_compat, "system_memory", return_value=None),
        ):
            info = handlers_system._get_static_system_info()

        assert "mem_total_gb" not in info
        (rec,) = _probe_records(caplog)
        assert rec.levelno == logging.WARNING
        assert "GlobalMemoryStatusEx" in rec.getMessage()


class TestFailedProbeStaysCached:
    """PIN of deliberate behaviour: a failed probe is NOT retried.

    Static info is computed once per process (``_STATIC_SYSTEM_INFO``) and a
    failed probe is cached like a successful one: the second call does NOT
    re-read /proc/meminfo and the diagnostic fires exactly once, not per
    request. Adding a retry is a deliberate change that updates this test.
    """

    @pytest.mark.usefixtures("fresh_static_cache")
    def test_second_call_does_not_reprobe_and_logs_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=handlers_system.logger.name)
        opens: list[str] = []
        real_open = builtins.open

        def _counting_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if path == "/proc/meminfo":
                opens.append(path)
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, *args, **kwargs)

        with patch("sys.platform", "linux"), patch("builtins.open", side_effect=_counting_open):
            first = handlers_system._get_static_system_info()
            second = handlers_system._get_static_system_info()

        assert first is second
        assert opens == ["/proc/meminfo"], "the static probe runs once per process"
        assert len(_probe_records(caplog)) == 1


def _status_state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A real DashboardState with every inline source stubbed, mirroring the
    ``test_ws_offload`` status tests, so ``api_status`` runs hermetically."""
    from kiro_crew.dashboard import status_counts as sc_module
    from kiro_crew.dashboard.state import DashboardState

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    crons = MagicMock()
    crons.list_jobs.return_value = []
    crons.status.return_value = {}
    crons.count_enabled_from_disk.return_value = 0
    lessons = MagicMock()
    lessons.load_all.return_value = []
    state = DashboardState(
        sessions=MagicMock(count=0),
        crons=crons,
        lessons=lessons,
        start_time=time.time() - 60,
        subagents=MagicMock(count=0),
    )
    state._owner_hash = "owner-hash-fixture"
    # Warm the shared status-counts cache so no store is touched.
    monkeypatch.setattr(sc_module, "_counts_cache", (0, 0))
    monkeypatch.setattr(sc_module, "_counts_cache_ts", time.monotonic())
    monkeypatch.setattr(sc_module, "_counts_cache_failures", 0)
    monkeypatch.setattr(sc_module, "_counts_refresh_inflight", False)
    # The two off-loop helpers read config/procfs; pin them.
    monkeypatch.setattr(handlers_system, "_yolo_duration_fields", lambda: ("6h", True, []))
    monkeypatch.setattr(handlers_system, "_gateway_memory_fields", lambda: (0, 0))
    return state


async def _status_body(monkeypatch: pytest.MonkeyPatch, tmp_path, static_info: dict) -> dict:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    state = _status_state(monkeypatch, tmp_path)
    monkeypatch.setattr(handlers_system, "_get_static_system_info", lambda: static_info)
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", "/api/status", app=app)
    resp = await handlers_system.api_status(req)
    return json.loads(resp.body.decode())


class TestStatusProjection:
    """AC2: an unavailable total surfaces as unknown, distinguishable from 0."""

    @pytest.mark.asyncio
    async def test_unavailable_total_projects_null_not_zero(self, monkeypatch, tmp_path) -> None:
        # The failed-probe shape: the static dict simply lacks the key.
        body = await _status_body(
            monkeypatch, tmp_path, {"os": "Linux", "arch": "x86_64", "cpu_count": 8}
        )
        assert (
            "mem_total_gb" in body
        ), "the key stays present so readers see unknown, not a missing field"
        assert body["mem_total_gb"] is None
        assert body["mem_total_gb"] != 0

    @pytest.mark.asyncio
    async def test_measured_total_projects_unchanged(self, monkeypatch, tmp_path) -> None:
        body = await _status_body(monkeypatch, tmp_path, {"mem_total_gb": 61.4})
        assert body["mem_total_gb"] == 61.4

    @pytest.mark.asyncio
    async def test_genuine_zero_is_still_zero(self, monkeypatch, tmp_path) -> None:
        """A host that really measured 0 GB (rounding of a tiny VM) keeps 0:
        the fix distinguishes unknown from 0, it does not erase 0."""
        body = await _status_body(monkeypatch, tmp_path, {"mem_total_gb": 0.0})
        assert body["mem_total_gb"] == 0.0
