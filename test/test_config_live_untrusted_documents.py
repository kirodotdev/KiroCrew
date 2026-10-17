"""What the live config watcher does with a document it cannot trust.

The point-of-use read's fallback chain when no snapshot is armed or the disk is unreadable,
and the bounds on dispatch: a hung applier times out, and a torn or unloadable document
reaches no applier and keeps the last good snapshot.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

from kiro_crew.config import live
from kiro_crew.config.live import ConfigChange, ConfigWatch
from kiro_crew.config.loader import KiroCrewConfig

# ── a document the loader cannot trust ────────────────────────────────────


class TestCurrentPointOfUseRead:
    """``live.current(fallback, log_prefix=...)`` -- the nine channel dispatchers'
    per-turn read, collapsed to one function so the fallback chain is written
    once instead of nine times."""

    def test_prefers_the_armed_snapshot_over_a_disk_load(self, cfg_file: Path) -> None:
        primed = KiroCrewConfig.load()
        live.watch().prime(primed)
        cfg_file.write_text(
            json.dumps({"agent": {"model": "a-different-model-on-disk"}}), encoding="utf-8"
        )
        boot_copy = KiroCrewConfig.load()
        assert live.current(boot_copy, log_prefix="test") is primed

    def test_falls_back_to_disk_when_unarmed(self, cfg_file: Path) -> None:
        assert live.snapshot() is None  # unstarted process watcher (fixture reset)
        cfg_file.write_text(json.dumps({"agent": {"model": "on-disk-model"}}), encoding="utf-8")
        boot_copy = KiroCrewConfig.load()
        result = live.current(boot_copy, log_prefix="test")
        assert result is not boot_copy
        assert result.agent.model == "on-disk-model"

    def test_falls_back_to_the_boot_copy_when_disk_is_unreadable(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert live.snapshot() is None

        def _boom() -> KiroCrewConfig:
            raise OSError("disk is gone")

        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_boom))
        boot_copy = object.__new__(KiroCrewConfig)  # a sentinel; identity is all this asserts
        assert live.current(boot_copy, log_prefix="test") is boot_copy


class TestApplierTimeout:
    """A hung awaitable applier is bounded, not just a raising one."""

    @pytest.mark.asyncio
    async def test_a_hanging_async_applier_times_out_and_is_retried(
        self, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live, "APPLIER_TIMEOUT_SECS", 0.05)
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())

        async def _hangs(change: ConfigChange) -> None:
            # flake-ok: the applier hangs on purpose so the patched 50 ms applier timeout cuts it short, moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the applier's own hang, which the patched applier timeout cuts short; not a barrier
            await asyncio.sleep(10)

        w.subscribe("agent.model", callback=_hangs, name="hangs-forever")
        cfg_file.write_text(json.dumps({"agent": {"model": "a-new-model"}}), encoding="utf-8")
        change = await w.refresh_now()
        assert change is not None and change.touched("agent.model")
        assert any(sub.name == "hangs-forever" for sub, _ in w._stale.values())

    @pytest.mark.asyncio
    async def test_a_prompt_async_applier_is_unaffected(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        adopted: list[str] = []

        async def _quick(change: ConfigChange) -> None:
            adopted.append(change.new.agent.model)

        w.subscribe("agent.model", callback=_quick, name="quick")
        cfg_file.write_text(json.dumps({"agent": {"model": "a-new-model"}}), encoding="utf-8")
        change = await w.refresh_now()
        assert change is not None
        assert adopted == ["a-new-model"]
        assert not w._stale

    """What the watcher does with a config it could not fully parse.

    ``load()`` does not raise on a torn or unparseable ``config.json`` -- it
    succeeds with the affected sections at their DEFAULTS and names them in
    ``degraded_sections``. So the watcher does dispatch, and it deliberately does
    not refuse on the applier's behalf: whether a default is safe is a property
    of the consumer, not of the file. A fail-closed applier (every channel
    allow-list) reads ``degraded_sections`` off the change and keeps its previous
    authorization state; a plain one (a timeout, a log level) adopts the default,
    which is the correct answer for it.

    A load that genuinely raises is the other case, and it keeps the previous
    snapshot with nothing dispatched (see
    ``test_a_raising_load_dispatches_nothing_and_keeps_the_snapshot``).
    """

    @pytest.mark.asyncio
    async def test_a_torn_config_degrades_rather_than_raising(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        good = KiroCrewConfig.load()
        w.prime(good)
        cfg_file.write_text("{not json at all", encoding="utf-8")
        assert await w.refresh_now() is None, "the values in force stay; nothing diffs"
        snap = w.snapshot()
        assert snap is not None and snap.degraded_sections, "a torn file is reported as degraded"
        assert snap.agent.model == good.agent.model, "the kept snapshot carries the values in force"
        assert w.last_error is None  # the load succeeded; it is the DOCUMENT that is bad

    @pytest.mark.asyncio
    async def test_a_torn_document_reaches_no_applier(self, cfg_file: Path) -> None:
        """An unparseable file loads as DEFAULTS; the watcher keeps the previous
        snapshot instead of adopting them, so no applier -- fail-closed or not --
        is ever handed the defaults, and a point-of-use read sees the values in
        force. The applier-level degraded guards stay as the backstop for a
        document the loader flags without the file being torn."""
        w = ConfigWatch()
        good = KiroCrewConfig.load()
        w.prime(good)
        seen: list[str] = []
        w.subscribe(callback=lambda c: seen.append(c.new.agent.model), name="any")
        cfg_file.write_text("{torn", encoding="utf-8")
        assert await w.refresh_now() is None
        assert seen == []
        snap = w.snapshot()
        assert snap is not None and snap.agent.model == good.agent.model
        assert snap.degraded_sections

    @pytest.mark.asyncio
    async def test_a_deferred_skip_is_retried_when_the_repair_diffs_empty(
        self, cfg_file: Path
    ) -> None:
        """The forgotten-skip hole: a fail-closed applier refuses the degraded
        document, the watcher adopts that document as its snapshot (the section
        at DEFAULTS), and the operator repairs the file to values that EQUAL
        those defaults -- an empty diff. Nothing would ever re-run the applier,
        and the pre-degradation authorization state would stay live for good.
        ``ConfigDeferred`` keeps the skipped paths stale until a clean load, and
        that load applies them even with nothing to diff."""
        from dataclasses import replace

        _write(cfg_file, {"wecom": {"allow_all_users": True}})
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        adopted: list[Any] = []

        def _fail_closed(c: ConfigChange) -> None:
            if c.new.degraded_sections & {"wecom", "*"}:
                raise live.ConfigDeferred(c.under("wecom"))
            adopted.append(c.new.wecom.allow_all_users)

        w.subscribe("wecom", callback=_fail_closed, name="wecom-fail-closed")
        # The loader's answer to a torn section: DEFAULTS (allow_all_users False,
        # a revocation the roster must eventually honour) plus the mark.
        degraded = replace(KiroCrewConfig(), _degraded_sections=frozenset({"wecom"}))
        repaired = KiroCrewConfig()  # byte-identical document, clean read
        assert degraded.to_dict() == repaired.to_dict()
        with patch.object(
            ConfigWatch, "_load", side_effect=[degraded, degraded, repaired, repaired]
        ):
            _write(cfg_file, {"edit": 1})  # a new fingerprint per edit
            change = await w.refresh_now()
            assert change is not None and "wecom.allow_all_users" in change.changed
            assert adopted == [], "a degraded section is never applied"
            assert w._stale, "the refused paths are recorded, not forgotten"
            # Still degraded on the next edit: retried quietly, still refused.
            _write(cfg_file, {"edit": 2})
            await w.refresh_now()
            assert adopted == []
            # The repair equals the degraded snapshot, so the reload diffs EMPTY
            # -- and the applier still runs, against the clean document.
            _write(cfg_file, {"edit": 3})
            assert await w.refresh_now() is None, "no diff against the degraded snapshot"
            assert adopted == [False]
            assert not w._stale
            # A further clean tick does not re-run a settled applier.
            assert await w.refresh_now() is None
            assert adopted == [False]

    @pytest.mark.asyncio
    async def test_a_deferred_skip_logs_once_then_quietly(
        self, cfg_file: Path, caplog: Any
    ) -> None:
        _write(cfg_file, {"wecom": {"allow_all_users": True}})
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())

        def _refuse(c: ConfigChange) -> None:
            raise live.ConfigDeferred(c.changed)

        w.subscribe("wecom", callback=_refuse, name="refuser")
        with caplog.at_level("DEBUG", logger="kiro_crew.config.live"):
            _write(cfg_file, {"edit": 1})
            await w.refresh_now()
            _write(cfg_file, {"edit": 2})
            await w.refresh_now()
        records = [r for r in caplog.records if "deferred" in r.getMessage()]
        assert [r.levelname for r in records] == ["WARNING", "DEBUG"]

    @pytest.mark.asyncio
    async def test_a_raising_load_dispatches_nothing_and_keeps_the_snapshot(
        self, cfg_file: Path
    ) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        before = w.snapshot()
        fired: list[str] = []
        w.subscribe(callback=lambda c: fired.append("ran"), name="any")
        with patch.object(ConfigWatch, "_load", side_effect=OSError("unreadable")):
            assert await w.refresh_now() is None
        assert fired == []
        assert w.snapshot() is before
        assert w.last_error is not None and "unreadable" in w.last_error

    def test_degraded_sections_ride_along_for_the_applier_to_refuse_on(self) -> None:
        """A fail-closed applier needs the degrade IN the change it is handed."""
        from dataclasses import replace

        cfg = replace(KiroCrewConfig(), _degraded_sections=frozenset({"wecom"}))
        change = ConfigChange(old=None, new=cfg, changed=frozenset({"wecom.allowed_users"}))
        assert "wecom" in change.new.degraded_sections

        applied: list[str] = []

        def _fail_closed(c: ConfigChange) -> None:
            if "wecom" in c.new.degraded_sections or "*" in c.new.degraded_sections:
                return  # keep the previous authorization state
            applied.append("adopted")

        _fail_closed(change)
        assert applied == []
        _fail_closed(
            ConfigChange(old=None, new=KiroCrewConfig(), changed=frozenset({"wecom.allowed_users"}))
        )
        assert applied == ["adopted"]
