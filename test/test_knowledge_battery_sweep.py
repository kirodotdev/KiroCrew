"""``power.on_battery`` and the Knowledge Library's background sweep.

Covers:
- the ``power`` config section: default, normalization, load from config.json;
- ``KnowledgeWatcher._battery_budget`` across mode x power source;
- ``_scan`` returning before any work when paused, and spending the reduced budget.

The power source is ALWAYS faked: the real host answers differently on a laptop,
a desktop and CI.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.service_sections import (
    ON_BATTERY_MODES,
    ON_BATTERY_NORMAL,
    ON_BATTERY_PAUSED,
    ON_BATTERY_REDUCED,
    PowerConfig,
    normalize_on_battery,
)
from kiro_crew.knowledge import watcher as watcher_mod
from kiro_crew.knowledge.watcher import BATTERY_REDUCED_FLOOR_CHUNKS, KnowledgeWatcher
from kiro_crew.platform_compat import POWER_AC, POWER_BATTERY, POWER_UNKNOWN


class TestPowerConfig:
    def test_default_is_normal(self):
        assert PowerConfig().on_battery == ON_BATTERY_NORMAL
        assert KiroCrewConfig().power.on_battery == ON_BATTERY_NORMAL

    @pytest.mark.parametrize("mode", ON_BATTERY_MODES)
    def test_known_modes_survive(self, mode):
        assert normalize_on_battery(mode) == mode

    def test_case_and_whitespace_are_forgiven(self):
        assert normalize_on_battery("  Paused ") == ON_BATTERY_PAUSED

    @pytest.mark.parametrize("value", ["", "off", "inherit", None, 3, ["paused"]])
    def test_unknown_values_are_normal(self, value):
        # A typo must never silently stop background work.
        assert normalize_on_battery(value) == ON_BATTERY_NORMAL

    def test_loads_from_config_json(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"power": {"on_battery": "reduced"}}), encoding="utf-8")
        with patch("kiro_crew.config.loader.config_path", return_value=path):
            assert KiroCrewConfig.load().power.on_battery == ON_BATTERY_REDUCED

    def test_round_trips_through_to_dict(self):
        cfg = KiroCrewConfig()
        assert cfg.to_dict()["power"] == {"on_battery": ON_BATTERY_NORMAL}


class TestBatteryBudget:
    """The mode x power-source decision table."""

    def _budget(self, mode, power, sweep_budget=500):
        with (
            patch.object(KnowledgeWatcher, "_on_battery_mode", staticmethod(lambda: mode)),
            patch.object(watcher_mod, "power_source", lambda: power),
        ):
            return KnowledgeWatcher._battery_budget(sweep_budget)

    @pytest.mark.parametrize("power", [POWER_AC, POWER_BATTERY, POWER_UNKNOWN])
    def test_normal_ignores_the_power_source(self, power):
        assert self._budget(ON_BATTERY_NORMAL, power) == 500

    @pytest.mark.parametrize("mode", [ON_BATTERY_REDUCED, ON_BATTERY_PAUSED])
    def test_on_mains_nothing_changes(self, mode):
        assert self._budget(mode, POWER_AC) == 500

    @pytest.mark.parametrize("mode", [ON_BATTERY_REDUCED, ON_BATTERY_PAUSED])
    def test_unknown_power_never_throttles(self, mode):
        # The load-bearing rule: a host that cannot report its power source
        # must not turn into a silent, permanent work stoppage.
        assert self._budget(mode, POWER_UNKNOWN) == 500

    def test_paused_on_battery_skips_the_sweep(self):
        assert self._budget(ON_BATTERY_PAUSED, POWER_BATTERY) is None

    def test_reduced_on_battery_is_a_quarter(self):
        assert self._budget(ON_BATTERY_REDUCED, POWER_BATTERY) == 125

    def test_reduced_respects_the_floor(self):
        assert (
            self._budget(ON_BATTERY_REDUCED, POWER_BATTERY, sweep_budget=20)
            == BATTERY_REDUCED_FLOOR_CHUNKS
        )

    def test_the_floor_never_raises_a_smaller_budget(self):
        assert self._budget(ON_BATTERY_REDUCED, POWER_BATTERY, sweep_budget=5) == 5

    def test_reduced_bounds_an_unbounded_sweep(self):
        # 0 means "no ceiling"; carried through, 'reduced' would be a no-op.
        assert self._budget(ON_BATTERY_REDUCED, POWER_BATTERY, sweep_budget=0) == 125

    def test_normal_keeps_an_unbounded_sweep_unbounded(self):
        assert self._budget(ON_BATTERY_NORMAL, POWER_BATTERY, sweep_budget=0) == 0

    def test_an_unreadable_config_is_normal(self):
        with patch.object(watcher_mod.KiroCrewConfig, "load", side_effect=RuntimeError("boom")):
            assert KnowledgeWatcher._on_battery_mode() == ON_BATTERY_NORMAL

    def test_the_power_source_is_read_only_when_the_mode_throttles(self):
        probe = MagicMock(side_effect=AssertionError("normal must not probe the power source"))
        with (
            patch.object(
                KnowledgeWatcher, "_on_battery_mode", staticmethod(lambda: ON_BATTERY_NORMAL)
            ),
            patch.object(watcher_mod, "power_source", probe),
        ):
            assert KnowledgeWatcher._battery_budget(500) == 500


class TestScanHonoursThePolicy:
    def _watcher(self):
        pipeline = MagicMock()
        # None makes the re-embed tail return at once, so these tests exercise
        # the budget decision alone.
        pipeline.embedder = None
        return KnowledgeWatcher(store=MagicMock(), pipeline=pipeline)

    def test_paused_sweep_returns_before_touching_the_store(self):
        w = self._watcher()
        w._store_rows = MagicMock(side_effect=AssertionError("sweep must not query"))
        with (
            patch.object(KnowledgeWatcher, "_sweep_chunk_budget", staticmethod(lambda: 500)),
            patch.object(KnowledgeWatcher, "_battery_budget", classmethod(lambda cls, b: None)),
        ):
            asyncio.run(w._scan())
        w._store_rows.assert_not_called()

    def test_the_battery_decision_is_offered_the_configured_budget(self):
        w = self._watcher()
        w._store_rows = MagicMock(side_effect=AssertionError("sweep must not query"))
        offered = {}

        def _decide(cls, budget):
            offered["budget"] = budget
            return None  # pause, so the sweep stops here

        with (
            patch.object(KnowledgeWatcher, "_sweep_chunk_budget", staticmethod(lambda: 500)),
            patch.object(KnowledgeWatcher, "_battery_budget", classmethod(_decide)),
        ):
            asyncio.run(w._scan())
        assert offered["budget"] == 500

    def test_reduced_budget_is_what_the_sweep_spends(self):
        w = self._watcher()

        async def _rows(sql, params=()):
            # Only the folder-source query returns a row, isolating the folder cap.
            if "source_type IN" in sql:
                return [
                    {
                        "id": 1,
                        "uri": "/tmp/src",
                        "source_type": "local_folder",
                        "properties": "{}",
                        "sync_status": None,
                    }
                ]
            return []

        async def _noop(*_a, **_kw):
            return None

        w._store_rows = _rows
        w._merge_source_props = _noop
        w._maybe_dedup_sweep = _noop
        caps: list = []

        async def _scan_source(source, chunk_budget=None, **_kw):
            caps.append(chunk_budget)
            return {"chunks": 0}

        w._folder_watcher.scan_source = _scan_source
        with (
            patch.object(KnowledgeWatcher, "_sweep_chunk_budget", staticmethod(lambda: 500)),
            patch.object(KnowledgeWatcher, "_battery_budget", classmethod(lambda cls, b: 125)),
            patch.object(watcher_mod, "folder_chunk_budget", lambda props: None),
        ):
            asyncio.run(w._scan())
        assert caps == [125]
