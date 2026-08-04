"""Tests for the Mochi activity budget: tiers, ledger, floor, model routing.

What is pinned here is the SEPARATION contract as much as the mechanics:
the behaviour mode (personality) and the activity tier (spend) are independent
axes, the floor defers rather than drops, and "unlimited" means the budget
system is disengaged entirely — original behaviour, not a huge budget.
"""

from __future__ import annotations

import asyncio

from kiro_crew.apps.builtins.mochi.activity_budget import (
    DEFAULT_TIER,
    TIER_UNLIMITED,
    TIERS,
    ActivityBudget,
    SpawnLedger,
    resolve_activity_budget,
)


class TestResolve:
    def test_default_is_balanced(self):
        assert DEFAULT_TIER == "balanced"
        assert resolve_activity_budget({}).tier == "balanced"

    def test_each_named_tier_resolves_to_itself(self):
        for name in TIERS:
            assert resolve_activity_budget({"activityTier": name}).tier == name

    def test_unlimited_returns_none_not_a_huge_budget(self):
        """None disengages the budget system: the poller falls back to the
        vendored constants, i.e. the original's exact behaviour. A huge budget
        would still replace the original's storm window with a 1-hour window —
        a behaviour change the tier promises not to make."""
        assert resolve_activity_budget({"activityTier": TIER_UNLIMITED}) is None

    def test_garbage_falls_back_to_default_instead_of_raising(self):
        """Runs inside the poller loop: a corrupt settings file must degrade to
        the default budget, not kill the autonomous side."""
        for garbage in ("turbo", 7, None, ["active"]):
            assert resolve_activity_budget({"activityTier": garbage}).tier == DEFAULT_TIER

    def test_tier_ordering_is_monotonic(self):
        """Cheaper tiers spawn less and batch at least as much — if a table
        edit ever breaks this, the tier names stop meaning anything."""
        eco, bal, act = TIERS["economy"], TIERS["balanced"], TIERS["active"]
        assert eco.max_spawns_per_hour < bal.max_spawns_per_hour < act.max_spawns_per_hour
        assert eco.watch_min_interval_ms > bal.watch_min_interval_ms > act.watch_min_interval_ms
        assert eco.max_watch_batch >= bal.max_watch_batch


class TestSpawnLedger:
    def test_counts_survive_a_new_instance(self, tmp_path):
        """The whole point of persisting: a restart cannot reset the meter."""
        clock = lambda: 3_600_000 * 100  # noqa: E731
        SpawnLedger(tmp_path, clock=clock).record_spawn()
        assert SpawnLedger(tmp_path, clock=clock).spawns_in_current_hour() == 1

    def test_hour_rollover_resets_the_current_hour_only(self, tmp_path):
        now = [3_600_000 * 100]
        ledger = SpawnLedger(tmp_path, clock=lambda: now[0])
        ledger.record_spawn()
        ledger.record_spawn()
        now[0] += 3_600_000
        assert ledger.spawns_in_current_hour() == 0
        assert ledger.spawns_in_last_hours(24) == 2

    def test_usage_summary_reads_the_same_buckets(self, tmp_path):
        now = [3_600_000 * 200]
        ledger = SpawnLedger(tmp_path, clock=lambda: now[0])
        for _ in range(3):
            ledger.record_spawn()
        now[0] += 3 * 3_600_000
        ledger.record_spawn()
        s = ledger.usage_summary()
        assert s == {"runsThisHour": 1, "runsToday": 4, "runs7d": 4}

    def test_corrupt_file_starts_fresh_instead_of_blocking_spawns(self, tmp_path):
        (tmp_path / "mochi-spawn-ledger.json").write_text("{not json")
        ledger = SpawnLedger(tmp_path)
        assert ledger.spawns_in_current_hour() == 0
        ledger.record_spawn()  # must not raise
        assert ledger.spawns_in_current_hour() == 1

    def test_old_buckets_are_pruned_on_write(self, tmp_path):
        now = [3_600_000 * 1000]
        ledger = SpawnLedger(tmp_path, clock=lambda: now[0])
        ledger.record_spawn()
        now[0] += 9 * 24 * 3_600_000  # beyond the keep window
        ledger.record_spawn()
        assert ledger.spawns_in_last_hours(10 * 24) == 1


class _Ctx:
    def __init__(self, data_dir, spawn=None):
        self.data_dir = data_dir
        self.spawn = spawn
        self.events = None
        self.cron = None
        self.storage = None


class TestFloorAndModel:
    def _runtime(self, tmp_path):
        from kiro_crew.apps.builtins.mochi import hooks

        return hooks.MochiRuntime(_Ctx(str(tmp_path)))

    def test_floor_defers_recently_checked_items_but_never_drops(self, tmp_path, monkeypatch):
        """Floor semantics: on economy (30min floor), an item checked 5 minutes
        ago is deferred even though its own 5-minute interval says due; an item
        never checked passes through (nothing to floor against)."""
        from kiro_crew.apps.builtins.mochi import hooks
        from kiro_crew.apps.builtins.mochi import watchlist_file as wf
        from kiro_crew.apps.builtins.mochi.settings import save_settings

        rt = self._runtime(tmp_path)
        save_settings(tmp_path, {"activityTier": "economy"})
        now = hooks._now_ms()
        fresh = {"id": "a", "lastCheckedAt": wf._iso(now - 5 * 60_000)}
        stale = {"id": "b", "lastCheckedAt": wf._iso(now - 60 * 60_000)}
        never = {"id": "c"}
        monkeypatch.setattr(wf, "read_watchlist", lambda *a, **k: {"items": []})
        monkeypatch.setattr(wf, "find_due_watch_items", lambda *a, **k: [fresh, stale, never])
        assert [i["id"] for i in rt.due_watch_items()] == ["b", "c"]

    def test_unlimited_applies_no_floor(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import hooks
        from kiro_crew.apps.builtins.mochi import watchlist_file as wf
        from kiro_crew.apps.builtins.mochi.settings import save_settings

        rt = self._runtime(tmp_path)
        save_settings(tmp_path, {"activityTier": "unlimited"})
        now = hooks._now_ms()
        fresh = {"id": "a", "lastCheckedAt": wf._iso(now - 1_000)}
        monkeypatch.setattr(wf, "read_watchlist", lambda *a, **k: {"items": []})
        monkeypatch.setattr(wf, "find_due_watch_items", lambda *a, **k: [fresh])
        assert [i["id"] for i in rt.due_watch_items()] == ["a"]

    def test_spawn_passes_bg_model_and_records_ledger(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.mochi import hooks
        from kiro_crew.apps.builtins.mochi.settings import save_settings

        save_settings(tmp_path, {"bgModel": "cheap-model-1"})
        calls = []

        class _Spawn:
            async def run(self, task, agent="", *, silent=False, model=""):
                calls.append({"agent": agent, "silent": silent, "model": model})
                return "sub-1"

        ctx = _Ctx(str(tmp_path), spawn=_Spawn())
        monkeypatch.setattr(hooks, "_runtime", hooks.MochiRuntime(_Ctx(str(tmp_path))))
        hooks._bind_spawn_from_context(ctx)
        try:
            spawn_id = asyncio.run(hooks._spawn_agent("do the plan"))
        finally:
            hooks.set_spawn_fn(None)
            monkeypatch.setattr(hooks, "_runtime", None)
        assert spawn_id == "sub-1"
        assert calls[0]["model"] == "cheap-model-1"
        assert calls[0]["agent"] == hooks.BG_AGENT_NAME
        ledger = SpawnLedger(tmp_path)
        assert ledger.spawns_in_current_hour() == 1


class TestSeparationOfAxes:
    def test_mode_does_not_appear_in_budget_resolution(self, tmp_path):
        """The personality axis must never leak into the spend axis: making the
        pet chattier must not silently cost more."""
        loud = resolve_activity_budget({"activityTier": "economy", "mode": "active"})
        quiet = resolve_activity_budget({"activityTier": "economy", "mode": "quiet"})
        assert loud == quiet

    def test_budget_module_never_reads_mode(self):
        import inspect

        from kiro_crew.apps.builtins.mochi import activity_budget

        src = inspect.getsource(activity_budget)
        assert '"mode"' not in src and "'mode'" not in src


class TestPollerBudgetPlumbing:
    def test_none_budget_uses_vendored_constants(self):
        """The unlimited tier's whole promise: with budget None the poller's
        watch block reads the ORIGINAL constants — verified structurally since
        the constant fallback lives inline in poll()."""
        import inspect

        from kiro_crew.apps.builtins.mochi import queue_poller as qp

        src = inspect.getsource(qp.QueuePoller)
        assert "WATCH_STORM_WINDOW_MS" in src
        assert "MAX_WATCH_SPAWNS_PER_WINDOW" in src
        assert "budget_provider" in src

    def test_budget_object_shape_matches_what_the_poller_reads(self):
        b = ActivityBudget(
            tier="t", max_spawns_per_hour=1, watch_min_interval_ms=2, max_watch_batch=3
        )
        assert b.max_spawns_per_hour == 1
        assert b.max_watch_batch == 3


class TestPresenceGate:
    """One heartbeat, two facts: shell running (gates autonomy) and pet
    visible (gates companion time)."""

    def _rt(self, tmp_path):
        from kiro_crew.apps.builtins.mochi import hooks

        return hooks.MochiRuntime(_Ctx(str(tmp_path)))

    def test_absent_by_default_present_after_beat(self, tmp_path):
        rt = self._rt(tmp_path)
        now = 1_000_000_000
        assert not rt.pet_present(now), "no heartbeat ever -> not present"
        rt.presence_beat(now)
        assert rt.pet_present(now + 60_000)
        assert not rt.pet_present(now + 120_000), "stale beat -> gone"

    def test_no_beat_ever_means_shell_absent(self, tmp_path):
        """The user's exact scenario: gateway serving a web-only dashboard,
        Electron shell never opened — autonomous work must be OFF from boot,
        not spinning against an environment that cannot show Mochi."""
        rt = self._rt(tmp_path)
        assert not rt.shell_present(1_000_000_000)

    def test_hidden_pet_keeps_the_shell_on_but_stops_companionship(self, tmp_path):
        """hideAll semantics from the original: window hidden, app running —
        the poller kept going, only the pet disappeared. A visible=False beat
        must therefore keep shell_present true while pet_present goes stale."""
        rt = self._rt(tmp_path)
        now = 1_000_000_000
        rt.presence_beat(now, visible=False)
        assert rt.shell_present(now + 60_000)
        assert not rt.pet_present(now + 60_000)

    def test_closed_shell_goes_absent_after_the_grace_window(self, tmp_path):
        rt = self._rt(tmp_path)
        now = 1_000_000_000
        rt.presence_beat(now, visible=True)
        assert rt.shell_present(now + 100_000)
        assert not rt.shell_present(now + 150_000)

    def test_return_resets_accrual_origin_so_the_gap_is_never_credited(self, tmp_path):
        """The original's closed app had no ticker, so time away was worth 0.
        Without the origin reset, the first tick after return would credit the
        whole away gap (or the 60s sleep heuristic) — a day away = free
        companion time."""
        rt = self._rt(tmp_path)
        rt.stats.load(1_000_000_000)  # populate stats (runtime does this in on_startup)
        start = rt.stats._stats["companionSeconds"]
        origin_before = rt.stats._uptime_start
        rt.stats.reset_uptime_origin(origin_before + 8 * 3_600_000)  # back after 8h
        rt.stats._tick_uptime(origin_before + 8 * 3_600_000 + 1)
        gained = rt.stats._stats["companionSeconds"] - start
        assert gained <= 1, f"away gap must not be credited, gained {gained}s"
