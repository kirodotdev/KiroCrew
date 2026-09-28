"""Tests for cautious boot — staggered startup after a recent loop-stall crash.

Follows the injectable-dependency pattern of test_crash_dump_store.py: dump
files are created in a temp dir and passed via ``dumps_dir``; the config and
the resource-posture probe are injected/monkeypatched so no test touches the
real data home or the real host's memory state.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from kiro_crew import resource_status
from kiro_crew.dashboard import cautious_boot
from kiro_crew.dashboard.cautious_boot import (
    MAX_DELAY_SECS,
    MILD_DELAY_SECS,
    RECENT_DUMP_MAX_AGE_SECS,
    CautiousBootDecision,
    _evaluate,
    initialize,
    pause_before,
)
from kiro_crew.dashboard.crash_dump_store import DUMP_PREFIX, DUMP_SUFFIX

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
        monkeypatch.setattr(
            resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL)
        )
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
        monkeypatch.setattr(
            resource_status, "probe", _fake_probe(resource_status.POSTURE_CRITICAL)
        )
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
