"""Tests for cautious boot — staggered startup after a recent loop-stall crash.

Follows the injectable-dependency pattern of test_crash_dump_store.py: dump
files are created in a temp dir and passed via ``dumps_dir``; the config and
the resource-posture probe are injected/monkeypatched so no test touches the
real data home or the real host's memory state.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import resource_status
from kiro_crew.dashboard import cautious_boot, crash_dump_store
from kiro_crew.dashboard.cautious_boot import (
    MAX_DELAY_SECS,
    MILD_DELAY_SECS,
    RECENT_DUMP_MAX_AGE_SECS,
    CautiousBootDecision,
    _evaluate,
    initialize,
    pause_before,
)
from kiro_crew.dashboard.crash_dump_store import (
    DUMP_PREFIX,
    DUMP_SUFFIX,
    HEALTHY_MARKER_NAME,
    _pid_domain,
    _pid_start_id,
    dump_owner_reached_healthy,
    record_healthy_boot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DashCfg:
    def __init__(self, cautious: bool) -> None:
        self.cautious_boot = cautious


class _Cfg:
    """Minimal stand-in for KiroCrewConfig — only what _evaluate reads."""

    def __init__(self, cautious: bool = True) -> None:
        self.dashboard = _DashCfg(cautious)


def _fake_probe(posture: str):
    """Return a probe() replacement yielding a fixed posture."""

    class _Status:
        pass

    def probe(cfg=None):  # noqa: ANN001 — matches resource_status.probe
        s = _Status()
        s.posture = posture
        return s

    return probe


@pytest.fixture
def dumps_dir(tmp_path: Path) -> Path:
    d = tmp_path / "crash-dumps"
    d.mkdir()
    return d


def _create_stacked_dump(dumps_dir: Path, *, age_secs: float = 0.0) -> Path:
    """Create a dump with real stack content (a wedge), aged *age_secs*."""
    p = dumps_dir / f"{DUMP_PREFIX}20260810T010000Z{DUMP_SUFFIX}"
    p.write_text(
        "# Kiro Crew loop-stall crash dump — opened 20260810T010000Z\n"
        "# PID: 12345\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
        "Thread 0x00007f0000000000 (most recent call first):\n"
        '  File "example.py", line 1 in main\n'
    )
    if age_secs:
        old = time.time() - age_secs
        os.utime(p, (old, old))
    return p


def _create_header_only_dump(dumps_dir: Path) -> Path:
    """Create a header-only dump (clean exit — no wedge)."""
    p = dumps_dir / f"{DUMP_PREFIX}20260810T020000Z{DUMP_SUFFIX}"
    p.write_text(
        "# Kiro Crew loop-stall crash dump — opened 20260810T020000Z\n"
        "# PID: 12345\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
    )
    return p


@pytest.fixture(autouse=True)
def _reset_decision_cache():
    cautious_boot._reset_for_tests()
    yield
    cautious_boot._reset_for_tests()


# ---------------------------------------------------------------------------
# _evaluate — decision matrix
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_recent_dump_ample_host_mild_stagger(self, dumps_dir, monkeypatch):
        _create_stacked_dump(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_AMPLE))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.active
        assert d.delay_secs == MILD_DELAY_SECS

    @pytest.mark.parametrize(
        "posture",
        [resource_status.POSTURE_TIGHT, resource_status.POSTURE_CRITICAL],
    )
    def test_recent_dump_pressured_host_maximum_caution(self, dumps_dir, monkeypatch, posture):
        _create_stacked_dump(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(posture))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.active
        assert d.delay_secs == MAX_DELAY_SECS

    def test_unknown_posture_does_not_escalate(self, dumps_dir, monkeypatch):
        """An unreadable probe must not pick the maximum delay — only a
        positive tight/critical reading escalates."""
        _create_stacked_dump(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_UNKNOWN))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.active
        assert d.delay_secs == MILD_DELAY_SECS

    def test_old_dump_boots_normally(self, dumps_dir, monkeypatch):
        _create_stacked_dump(dumps_dir, age_secs=RECENT_DUMP_MAX_AGE_SECS + 60)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert not d.active
        assert d.delay_secs == 0.0

    def test_no_dump_boots_normally(self, dumps_dir):
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert not d.active

    def test_header_only_dump_is_not_a_crash(self, dumps_dir):
        """A header-only dump means the previous instance exited cleanly."""
        _create_header_only_dump(dumps_dir)
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert not d.active

    def test_config_off_boots_normally(self, dumps_dir, monkeypatch):
        _create_stacked_dump(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL))
        d = _evaluate(cfg=_Cfg(cautious=False), dumps_dir=dumps_dir)
        assert not d.active
        assert "disabled" in d.reason

    def test_unreadable_store_fails_open(self, monkeypatch):
        def _boom(dumps_dir=None):
            raise OSError("store unreadable")

        monkeypatch.setattr(cautious_boot, "newest_dump_with_stacks", _boom)
        d = _evaluate(cfg=_Cfg())
        assert not d.active
        assert "fail-open" in d.reason

    def test_probe_exception_fails_open(self, dumps_dir, monkeypatch):
        _create_stacked_dump(dumps_dir)

        def _boom(cfg=None):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(resource_status, "probe", _boom)
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert not d.active

    def test_default_config_attribute_missing_defaults_on(self, dumps_dir, monkeypatch):
        """A config object without the field (older overlay) defaults to ON."""

        class _Bare:
            dashboard = object()

        _create_stacked_dump(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_AMPLE))
        d = _evaluate(cfg=_Bare(), dumps_dir=dumps_dir)
        assert d.active


# ---------------------------------------------------------------------------
# initialize / pause_before — async plumbing
# ---------------------------------------------------------------------------


class TestInitializeAndPause:
    @pytest.mark.asyncio
    async def test_initialize_caches_and_logs_loudly(self, dumps_dir, monkeypatch, caplog):
        _create_stacked_dump(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_TIGHT))
        monkeypatch.setattr(
            cautious_boot,
            "_evaluate",
            lambda cfg=None, dumps_dir_=None: _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir),
        )
        with caplog.at_level(logging.WARNING, logger=cautious_boot.__name__):
            d = await initialize()
        assert d.active
        assert cautious_boot._decision is d
        assert any("Cautious boot ACTIVE" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_initialize_thread_exhaustion_fails_open(self, monkeypatch):
        async def _no_thread(fn, *a, **kw):
            raise RuntimeError("no worker thread")

        monkeypatch.setattr(cautious_boot.asyncio, "to_thread", _no_thread)
        d = await initialize()
        assert not d.active
        assert cautious_boot._decision is d

    @pytest.mark.asyncio
    async def test_pause_before_uninitialized_is_noop(self, monkeypatch):
        async def _fail_sleep(secs):
            raise AssertionError(f"pause_before slept ({secs}s) without initialize()")

        monkeypatch.setattr(cautious_boot.asyncio, "sleep", _fail_sleep)
        await pause_before("app backends")  # must not sleep

    @pytest.mark.asyncio
    async def test_pause_before_inactive_is_noop(self, monkeypatch):
        cautious_boot._decision = CautiousBootDecision(False, 0.0, "no dump")

        async def _fail_sleep(secs):
            raise AssertionError("pause_before slept while inactive")

        monkeypatch.setattr(cautious_boot.asyncio, "sleep", _fail_sleep)
        await pause_before("cron scheduler")

    @pytest.mark.asyncio
    async def test_pause_before_active_sleeps_decision_delay(self, monkeypatch):
        cautious_boot._decision = CautiousBootDecision(True, MAX_DELAY_SECS, "recent dump")
        slept: list[float] = []

        async def _record_sleep(secs):
            slept.append(secs)

        monkeypatch.setattr(cautious_boot.asyncio, "sleep", _record_sleep)
        await pause_before("MCP server probe")
        assert slept == [MAX_DELAY_SECS]

    @pytest.mark.asyncio
    async def test_pause_before_never_raises_into_boot(self, monkeypatch):
        """The pause is best-effort: even a pathological sleep failure must not
        propagate into (and abort) gateway startup — asyncio.sleep only raises
        CancelledError in practice, which must propagate; anything else is a
        no-op guarantee provided by reading an immutable decision."""
        cautious_boot._decision = CautiousBootDecision(True, 0.0, "recent dump")
        # Zero delay: still logs + sleeps(0) — just verify it completes.
        await pause_before("session restore")


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


class TestConfigKey:
    def test_dashboard_config_defaults_on(self):
        from kiro_crew.config.loader import DashboardConfig

        assert DashboardConfig().cautious_boot is True

    def test_loader_rejects_non_bool(self):
        from kiro_crew.config.loader import _safe_bool

        assert _safe_bool("yes", True) is True  # non-bool → default
        assert _safe_bool(False, True) is False
        assert _safe_bool(None, True) is True


# ---------------------------------------------------------------------------
# The previous instance's readiness — a stall during startup and a stall after
# hours of service must not be treated the same.
# ---------------------------------------------------------------------------


def _create_owned_dump(
    dumps_dir: Path, *, pid: int = 4242, domain: str = "host-a", start_id: str = "999"
) -> Path:
    """A stacked dump whose header carries the full identity triple."""
    p = dumps_dir / f"{DUMP_PREFIX}20260810T030000Z{DUMP_SUFFIX}"
    p.write_text(
        "# Kiro Crew loop-stall crash dump — opened 20260810T030000Z\n"
        f"# PID: {pid} @ {domain} start={start_id}\n"
        "# If thread stacks appear below, the event loop wedged and faulthandler fired.\n"
        "\n"
        "Thread 0x00007f0000000000 (most recent call first):\n"
        '  File "example.py", line 1 in main\n'
    )
    return p


def _write_marker(
    dumps_dir: Path, *, pid: int = 4242, domain: str = "host-a", start_id: str = "999"
) -> None:
    (dumps_dir / HEALTHY_MARKER_NAME).write_text(f"{pid} {domain} {start_id}\n", encoding="utf-8")


class TestPriorInstanceReachedServing:
    """The recovery boot must not be the slowest one."""

    def test_healthy_prior_instance_on_a_calm_host_boots_normally(self, dumps_dir, monkeypatch):
        _create_owned_dump(dumps_dir)
        _write_marker(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_AMPLE))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.active is False
        assert d.delay_secs == 0.0
        assert "reached a serving state" in d.reason

    @pytest.mark.parametrize(
        "posture", [resource_status.POSTURE_TIGHT, resource_status.POSTURE_CRITICAL]
    )
    def test_a_still_pressured_host_keeps_a_mild_stagger(self, dumps_dir, monkeypatch, posture):
        """Downgraded, not switched off: current pressure is its own signal."""
        _create_owned_dump(dumps_dir)
        _write_marker(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(posture))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.active is True
        assert d.delay_secs == MILD_DELAY_SECS

    def test_a_startup_wedge_still_gets_maximum_caution(self, dumps_dir, monkeypatch):
        """No marker — the battery is not exonerated, so nothing changes."""
        _create_owned_dump(dumps_dir)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.active is True
        assert d.delay_secs == MAX_DELAY_SECS

    def test_a_marker_from_another_process_is_not_this_dumps_evidence(self, dumps_dir, monkeypatch):
        """A recycled PID or a sibling gateway must not exonerate this battery."""
        _create_owned_dump(dumps_dir, pid=4242, start_id="999")
        _write_marker(dumps_dir, pid=4242, start_id="1000")
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.delay_secs == MAX_DELAY_SECS

    def test_a_marker_from_another_host_is_not_evidence(self, dumps_dir, monkeypatch):
        _create_owned_dump(dumps_dir, domain="host-a")
        _write_marker(dumps_dir, domain="host-b")
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.delay_secs == MAX_DELAY_SECS

    def test_an_unknown_start_identity_is_not_evidence(self, dumps_dir, monkeypatch):
        """A platform without a start identity falls back to today's behaviour."""
        _create_owned_dump(dumps_dir)
        _write_marker(dumps_dir, start_id="-")
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.delay_secs == MAX_DELAY_SECS

    def test_a_headerless_dump_is_not_evidence(self, dumps_dir, monkeypatch):
        """The legacy `# PID: n` header carries no identity; stay conservative."""
        _create_stacked_dump(dumps_dir)
        _write_marker(dumps_dir, pid=12345)
        monkeypatch.setattr(resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL))
        d = _evaluate(cfg=_Cfg(), dumps_dir=dumps_dir)
        assert d.delay_secs == MAX_DELAY_SECS


class TestHealthyMarkerRoundTrip:
    def test_this_process_recognises_its_own_marker(self, dumps_dir):
        pid = os.getpid()
        record_healthy_boot(dumps_dir)
        dump = _create_owned_dump(
            dumps_dir, pid=pid, domain=_pid_domain(), start_id=_pid_start_id(pid) or "-"
        )
        # Only meaningful where this platform HAS a start identity; where it
        # does not, the conservative False is the documented answer.
        expected = _pid_start_id(pid) is not None
        assert dump_owner_reached_healthy(dump, dumps_dir) is expected

    def test_a_missing_marker_reads_as_not_healthy(self, dumps_dir):
        dump = _create_owned_dump(dumps_dir)
        assert dump_owner_reached_healthy(dump, dumps_dir) is False

    def test_recording_never_raises_on_an_unwritable_store(self, tmp_path):
        record_healthy_boot(tmp_path / "does" / "not" / "exist")

    def test_a_truncated_marker_reads_as_not_healthy(self, dumps_dir):
        dump = _create_owned_dump(dumps_dir)
        (dumps_dir / HEALTHY_MARKER_NAME).write_text("4242 host-a\n", encoding="utf-8")
        assert dump_owner_reached_healthy(dump, dumps_dir) is False


class TestHealthyMarkerIsReadDefensively:
    """The marker lives in a directory the agent can write to.

    Everything here is about the READ, not the content: a wrong marker costs a
    slower boot, but a marker that never finishes being read costs the boot
    itself, on a path whose enclosing ``except`` cannot catch a hang.
    """

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
    def test_a_fifo_marker_does_not_hang_the_boot(self, dumps_dir):
        dump = _create_owned_dump(dumps_dir)
        os.mkfifo(dumps_dir / HEALTHY_MARKER_NAME)
        # No reader and no writer: read_text would block here forever.
        assert dump_owner_reached_healthy(dump, dumps_dir) is False

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is POSIX-only")
    def test_a_symlinked_marker_is_refused(self, dumps_dir, tmp_path):
        pid = os.getpid()
        elsewhere = tmp_path / "planted"
        line = f"{pid} {_pid_domain()} {_pid_start_id(pid) or '-'}" + chr(10)
        elsewhere.write_text(line, encoding="utf-8")
        (dumps_dir / HEALTHY_MARKER_NAME).symlink_to(elsewhere)
        dump = _create_owned_dump(
            dumps_dir, pid=pid, domain=_pid_domain(), start_id=_pid_start_id(pid) or "-"
        )
        # The content would otherwise be accepted; the link is what refuses it.
        assert dump_owner_reached_healthy(dump, dumps_dir) is False

    def test_a_directory_at_the_marker_name_reads_as_not_healthy(self, dumps_dir):
        dump = _create_owned_dump(dumps_dir)
        (dumps_dir / HEALTHY_MARKER_NAME).mkdir()
        assert dump_owner_reached_healthy(dump, dumps_dir) is False

    def test_an_oversized_marker_is_bounded_rather_than_read_whole(self, dumps_dir):
        dump = _create_owned_dump(dumps_dir)
        (dumps_dir / HEALTHY_MARKER_NAME).write_text("x" * 100_000, encoding="utf-8")
        assert len(crash_dump_store._read_healthy_marker(dumps_dir)) <= 256
        assert dump_owner_reached_healthy(dump, dumps_dir) is False


class TestMarkerWriteDoesNotGateReadiness:
    """The write is dispatched, not awaited.

    ``start_dashboard`` returning is what publishes ``KIROCREW_READY``, so
    anything awaited after ``state.ready = True`` can hold readiness open. On
    a data home mounted over a stalled network share that is unbounded, and a
    supervisor waiting on the ready line respawns into the same hang.
    """

    @pytest.mark.asyncio
    async def test_a_stalled_write_does_not_block_the_caller(self, monkeypatch):
        from types import SimpleNamespace

        from kiro_crew.dashboard import server

        blocked = threading.Event()
        entered = threading.Event()

        def _never_finishes(*_a, **_k):
            entered.set()
            blocked.wait(timeout=10)

        monkeypatch.setattr(server, "record_healthy_boot", _never_finishes)
        state = SimpleNamespace(_background_tasks=set())
        try:
            # Reaching the next line at all is the assertion: an awaited write
            # would still be inside _never_finishes.
            server._dispatch_healthy_boot_marker(state)
            assert len(state._background_tasks) == 1
            task = next(iter(state._background_tasks))
            assert not task.done()
        finally:
            blocked.set()
            for pending in list(state._background_tasks):
                pending.cancel()

    @pytest.mark.asyncio
    async def test_a_completed_write_stops_being_tracked(self, monkeypatch, dumps_dir):
        from types import SimpleNamespace

        from kiro_crew.dashboard import server

        monkeypatch.setattr(server, "record_healthy_boot", lambda *_a, **_k: None)
        state = SimpleNamespace(_background_tasks=set())
        server._dispatch_healthy_boot_marker(state)
        task = next(iter(state._background_tasks))
        await task
        # The done callback discards it, so a long-lived gateway does not
        # accumulate one finished task per start.
        assert state._background_tasks == set()

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="symlink semantics are POSIX-only")
    def test_the_write_does_not_follow_a_planted_link(self, dumps_dir, tmp_path):
        """The read is hardened; the write must be too, or it truncates.

        A temp name derived from the PID is fully predictable, and PID 1 in a
        container is deterministic across restarts, so the link can be waiting
        before the process that would write through it exists.
        """
        victim = tmp_path / "governed.json"
        victim.write_text("policy that must survive", encoding="utf-8")
        marker = dumps_dir / HEALTHY_MARKER_NAME
        marker.symlink_to(victim)
        record_healthy_boot(dumps_dir)
        assert victim.read_text(encoding="utf-8") == "policy that must survive"
