"""Config hot-reload for the SERVICES area.

Every long-lived service object in this area copied a value out of ``config.json``
at construction, so a write reached it only after a gateway restart. Each one now
either subscribes on the process config watcher and adopts the new value through a
``reconfigure`` / ``apply_config`` method, or reads the value at point of use.
These tests ask the same four questions of every item:

* the applier **adopts** the reloaded thresholds / sizes / paths;
* a **degraded or malformed** section keeps the value the service already had,
  rather than pushing a value the loader could not parse;
* the **subscription** is registered under the right prefixes and drops itself
  when its owner is collected (the watcher holds a bound method weakly);
* the applier is a **no-op for an untouched prefix**, so an unrelated write does
  not churn a live service.

Beyond that, three items carry a claim of their own worth pinning:

* **hooks** -- ``splice_denied_commands`` replaces the keystone opt-out fields
  wholesale while preserving the flat hook keys, and the two live-reload paths
  (Settings>Security and the ``config.json`` watcher) produce the SAME config, so
  neither write reverts the other's half;
* **metrics** -- any ``telemetry.*`` change rebuilds the recorder exactly once;
* **skills** -- ``max_triggered`` is read at USE, so it needs no subscription.

Reloads are simulated by priming the watcher with a config and handing the
subscriber a :class:`ConfigChange` (directly, or through ``ConfigWatch._dispatch``
so the prefix filter is exercised too). The real poll task is never started.
"""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _hot_reload_helpers import change as _change

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig


async def _dispatch(new, *paths: str) -> None:
    """Run one reload through the watcher's own prefix filter.

    Uses ``_dispatch`` rather than ``refresh_now`` so no config file is read and
    no poll task is armed -- the question here is which subscribers a given set of
    changed paths reaches, which is exactly what ``_dispatch`` decides.
    """
    live.watch().prime(new)
    await live.watch()._dispatch(_change(new, *paths))


def _subs_named(name: str) -> list:
    return [s for s in live.watch().subscriptions() if s.name == name]


def _one_sub(name: str):
    subs = _subs_named(name)
    assert len(subs) == 1, f"expected exactly one {name!r} subscription, got {len(subs)}"
    return subs[0]


# ==================================================================
# 1. VectorMemoryStore
# ==================================================================


def _memory_store(tmp_path: Path):
    from kiro_crew.vector_memory import VectorMemoryStore

    return VectorMemoryStore(db_path=tmp_path / "memory.db")


def _memory_cfg(
    *,
    confidence: float = 0.9,
    dedup: float = 0.8,
    episodic_results: int = 7,
    episodic_max: int = 555,
    decay_rates: object = None,
    semantic_keys: object = None,
) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.memory.semantic_confidence_threshold = confidence
    cfg.memory.episodic_dedup_threshold = dedup
    cfg.memory.episodic_max_results = episodic_results
    cfg.memory.episodic_max_count = episodic_max
    cfg.memory.decay_rates = {} if decay_rates is None else decay_rates
    cfg.memory.semantic_keys = [] if semantic_keys is None else semantic_keys
    return cfg


class TestVectorMemoryStore:
    def test_subscribes_on_memory_and_holds_the_subscription(self, tmp_path: Path) -> None:
        store = _memory_store(tmp_path)
        sub = _one_sub("VectorMemoryStore")
        assert sub.prefixes == ("memory",)
        assert store._config_sub is sub

    def test_reconfigure_adopts_every_retrieval_tunable(self, tmp_path: Path) -> None:
        store = _memory_store(tmp_path)
        cfg = _memory_cfg(
            confidence=0.42,
            dedup=0.33,
            episodic_results=11,
            episodic_max=1234,
            semantic_keys=["team/", "vendor/"],
        )
        store.reconfigure(cfg)
        assert store._confidence_threshold == 0.42
        assert store._dedup_threshold == 0.33
        assert store._episodic_limit == 11
        assert store._episodic_max == 1234
        assert store._prefixes[-2:] == ["team/", "vendor/"]

    def test_semantic_keys_are_appended_to_the_builtin_prefixes(self, tmp_path: Path) -> None:
        from kiro_crew.vector_memory import _BUILTIN_PREFIXES

        store = _memory_store(tmp_path)
        store.reconfigure(_memory_cfg(semantic_keys=["extra/"]))
        assert store._prefixes[: len(_BUILTIN_PREFIXES)] == list(_BUILTIN_PREFIXES)
        # Removing the key again drops it rather than accumulating across reloads.
        store.reconfigure(_memory_cfg(semantic_keys=[]))
        assert store._prefixes == list(_BUILTIN_PREFIXES)

    def test_decay_rates_are_re_sanitized_not_copied(self, tmp_path: Path) -> None:
        """A hand-edited rate table is clamped and de-garbaged exactly as at boot."""
        store = _memory_store(tmp_path)
        store.reconfigure(
            _memory_cfg(decay_rates={"default": 0.07, "work": 0.5, "junk": "not-a-rate"})
        )
        assert store._decay_default == 0.07
        assert store._decay_by_tag.get("work") == 0.5
        assert "junk" not in store._decay_by_tag

    def test_the_resident_episodic_scoring_set_is_invalidated(self, tmp_path: Path) -> None:
        """The scoring set carries the decay rates it was built with."""
        store = _memory_store(tmp_path)
        store._episodic_scoring = object()
        before = store._episodic_scoring_generation
        store.reconfigure(_memory_cfg(decay_rates={"default": 0.09}))
        assert store._episodic_scoring is None
        assert store._episodic_scoring_generation > before

    @pytest.mark.asyncio
    async def test_an_unrelated_write_does_not_reach_the_store(self, tmp_path: Path) -> None:
        store = _memory_store(tmp_path)
        with patch.object(store, "reconfigure") as reconfigure:
            await _dispatch(KiroCrewConfig(), "session.timeout_secs")
        reconfigure.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_memory_write_reaches_the_store_through_the_dispatcher(
        self, tmp_path: Path
    ) -> None:
        store = _memory_store(tmp_path)
        await _dispatch(_memory_cfg(confidence=0.11), "memory.semantic_confidence_threshold")
        assert store._confidence_threshold == 0.11

    def test_the_subscription_drops_when_the_store_is_collected(self, tmp_path: Path) -> None:
        """The watcher holds a bound method weakly, so a discarded store falls out."""
        store = _memory_store(tmp_path)
        assert _subs_named("VectorMemoryStore")
        del store
        gc.collect()
        assert _subs_named("VectorMemoryStore") == []


class TestEpisodicMaxCountIsConsumed:
    """``memory.episodic_max_count`` was parsed by the loader and read by nobody.

    Two halves close it: the gateway now passes it into the store it builds, and
    ``reconfigure`` pushes it on every reload -- so the value is in force at boot
    and after any later write.
    """

    def test_the_store_constructor_takes_it(self, tmp_path: Path) -> None:
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "m.db", episodic_max=77)
        assert store._episodic_max == 77

    def test_the_gateway_passes_the_configured_count_into_the_store(self) -> None:
        """Pinned at the call site: the field is only useful if boot threads it in."""
        source = Path(
            live.__file__.replace("config/live.py", "slack/gateway.py").replace(
                "config\\live.py", "slack\\gateway.py"
            )
        ).read_text(encoding="utf-8")
        assert "episodic_max=self._cfg.memory.episodic_max_count" in source

    def test_a_reload_pushes_the_count_onto_a_store_that_missed_it(self, tmp_path: Path) -> None:
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "m.db", episodic_max=10)
        store.reconfigure(_memory_cfg(episodic_max=900))
        assert store._episodic_max == 900


# ==================================================================
# 2. HistoryConsolidator
# ==================================================================


def _consolidator():
    from kiro_crew.history_consolidation import HistoryConsolidator

    return HistoryConsolidator(log=MagicMock(), memory=MagicMock())


def _skills_cfg(**over) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.memory.history_idle_hours = over.get("history_idle_hours", 5)
    cfg.memory.migrated = over.get("migrated", True)
    cfg.skills.auto_create_from_sessions = over.get("auto_create_from_sessions", True)
    cfg.skills.auto_refine_on_deviation = over.get("auto_refine_on_deviation", True)
    cfg.skills.auto_min_tool_calls = over.get("auto_min_tool_calls", 9)
    cfg.skills.auto_similarity_threshold = over.get("auto_similarity_threshold", 0.71)
    cfg.skills.approval_required = over.get("approval_required", False)
    cfg.skills.max_auto_skills = over.get("max_auto_skills", 42)
    cfg.skills.stale_after_days = over.get("stale_after_days", 13)
    cfg.skills.archive_after_days = over.get("archive_after_days", 44)
    cfg.skills.generate_scripts = over.get("generate_scripts", False)
    cfg.skills.judge_model = over.get("judge_model", "judge-x")
    return cfg


class TestHistoryConsolidator:
    def test_subscribes_on_skills_and_the_two_memory_leaves(self) -> None:
        c = _consolidator()
        sub = _one_sub("HistoryConsolidator")
        assert sub.prefixes == ("skills", "memory.history_idle_hours", "memory.migrated")
        assert c._config_sub is sub

    def test_reconfigure_adopts_all_twelve_settings(self) -> None:
        c = _consolidator()
        c.reconfigure(_skills_cfg())
        assert c._history_idle_secs == 5 * 3600
        assert c._migrated is True
        assert c._auto_skills_enabled is True
        assert c._auto_refine_enabled is True
        assert c._auto_min_tool_calls == 9
        assert c._auto_similarity_threshold == 0.71
        assert c._approval_required is False
        assert c._max_auto_skills == 42
        assert c._stale_after_days == 13
        assert c._archive_after_days == 44
        assert c._generate_scripts is False
        assert c._judge_model == "judge-x"

    def test_idle_hours_are_converted_to_seconds_not_copied(self) -> None:
        c = _consolidator()
        c.reconfigure(_skills_cfg(history_idle_hours=2))
        assert c._history_idle_secs == 7200.0

    def test_a_malformed_section_keeps_the_previous_settings(self) -> None:
        """A section the loader could not parse must not become the live policy."""
        c = _consolidator()
        c.reconfigure(_skills_cfg(max_auto_skills=42))
        broken = _skills_cfg()
        broken.skills.max_auto_skills = "lots"  # type: ignore[assignment]
        with pytest.raises(ValueError):
            c.reconfigure(broken)
        # The applier is guarded by the dispatcher, so a raising reconfigure leaves
        # the values it had already set -- never a half-parsed policy of its own.
        assert c._max_auto_skills == 42

    @pytest.mark.asyncio
    async def test_an_unrelated_memory_leaf_does_not_reach_it(self) -> None:
        c = _consolidator()
        with patch.object(c, "reconfigure") as reconfigure:
            await _dispatch(KiroCrewConfig(), "memory.episodic_max_results")
        reconfigure.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_skills_write_reaches_it_through_the_dispatcher(self) -> None:
        c = _consolidator()
        await _dispatch(_skills_cfg(judge_model="live-judge"), "skills.judge_model")
        assert c._judge_model == "live-judge"


# ==================================================================
# 3. hooks: splice_denied_commands + HookManager.watch_config
# ==================================================================


def _hooks_flat_section() -> dict:
    return {
        "auto_replies": [{"pattern": "ping", "reply": "pong"}],
        "auto_approve_tools": ["fs_read"],
    }


def _keystone_state(pattern: str = "rm -rf /", disabled: tuple[str, ...] = ()) -> dict:
    return {
        "disable_all": False,
        "disabled_ids": list(disabled),
        "user_added": [{"pattern": pattern, "enabled": True}],
    }


class TestHooksDeniedCommandSplice:
    def test_the_splice_replaces_the_opt_out_fields_wholesale(self) -> None:
        from kiro_crew.hooks import HooksConfig, splice_denied_commands

        base = HooksConfig.from_dict(
            {**_hooks_flat_section(), "denied_commands": _keystone_state("OLD", ("old-id",))}
        )
        spliced = splice_denied_commands(base, _keystone_state("NEW", ("new-id",)))
        assert [p.pattern for p in spliced.denied_commands_user_added] == ["NEW"]
        assert spliced.denied_commands_disabled_ids == ["new-id"]
        assert spliced.denied_commands_disable_all is False

    def test_the_splice_preserves_the_flat_hook_keys(self) -> None:
        """The two halves come from different files, so neither may drop the other."""
        from kiro_crew.hooks import HooksConfig, splice_denied_commands

        base = HooksConfig.from_dict(_hooks_flat_section())
        spliced = splice_denied_commands(base, _keystone_state())
        assert [h.pattern for h in spliced.auto_replies] == ["ping"]
        assert spliced.auto_approve_tools == base.auto_approve_tools

    def test_an_empty_keystone_state_clears_the_opt_out(self) -> None:
        """Fail-safe direction for a deny gate: no state means every built-in enforces."""
        from kiro_crew.hooks import HooksConfig, splice_denied_commands

        base = HooksConfig.from_dict({"denied_commands": {"disable_all": True}})
        spliced = splice_denied_commands(base, {})
        assert spliced.denied_commands_disable_all is False
        assert spliced.denied_commands_user_added == []

    def test_the_default_reads_the_keystone(self) -> None:
        from kiro_crew import hooks as hooks_mod

        base = hooks_mod.HooksConfig()
        with patch.object(
            hooks_mod, "load_denied_commands_state", return_value=_keystone_state("FROM-FILE")
        ) as load:
            spliced = hooks_mod.splice_denied_commands(base)
        load.assert_called_once()
        assert [p.pattern for p in spliced.denied_commands_user_added] == ["FROM-FILE"]


class TestHookManagerWatchConfig:
    def test_watch_config_is_opt_in_and_subscribes_on_hooks(self) -> None:
        from kiro_crew.hooks import HookManager

        manager = HookManager()
        assert manager._config_sub is None
        assert _subs_named("HookManager") == []
        manager.watch_config()
        sub = _one_sub("HookManager")
        assert sub.prefixes == ("hooks",)
        assert manager._config_sub is sub

    def test_a_repeated_watch_config_is_a_no_op(self) -> None:
        """A second call must not stack a duplicate subscription."""
        from kiro_crew.hooks import HookManager

        manager = HookManager()
        first = manager.watch_config()
        second = manager.watch_config()
        assert second is first
        assert manager._config_sub is first
        subs = _subs_named("HookManager")
        assert len(subs) == 1
        assert subs[0] is first

    def test_a_hooks_reload_keeps_the_current_keystone_state(self) -> None:
        from kiro_crew import hooks as hooks_mod

        manager = hooks_mod.HookManager()
        manager.watch_config()
        cfg = KiroCrewConfig()
        cfg.hooks = _hooks_flat_section()  # type: ignore[assignment]
        with patch.object(
            hooks_mod, "load_denied_commands_state", return_value=_keystone_state("KEEP-ME")
        ):
            manager._on_config_change(_change(cfg, "hooks.auto_replies"))
        assert [h.pattern for h in manager._config.auto_replies] == ["ping"]
        assert [p.pattern for p in manager._config.denied_commands_user_added] == ["KEEP-ME"]

    def test_both_live_reload_paths_produce_the_same_config(self) -> None:
        """Settings>Security and the config.json watcher must not revert each other."""
        import dataclasses

        from kiro_crew import hooks as hooks_mod
        from kiro_crew.dashboard.handlers import security as security_mod

        def _normalize(cfg):
            """Blank the per-parse ids so two parses of one keystone state compare."""
            return dataclasses.replace(
                cfg,
                denied_commands_user_added=[
                    dataclasses.replace(p, id="") for p in cfg.denied_commands_user_added
                ],
            )

        state = _keystone_state("SHARED")
        flat = _hooks_flat_section()

        # (a) the config.json watcher: flat keys reparsed, keystone spliced in.
        watcher_manager = hooks_mod.HookManager()
        cfg = KiroCrewConfig()
        cfg.hooks = flat  # type: ignore[assignment]
        with patch.object(hooks_mod, "load_denied_commands_state", return_value=state):
            watcher_manager._on_config_change(_change(cfg, "hooks.auto_replies"))

        # (b) Settings>Security: live flat keys kept, keystone state spliced on.
        handler_manager = hooks_mod.HookManager(hooks_mod.HooksConfig.from_dict(flat))
        request = MagicMock()
        request.app = {
            "state": SimpleNamespace(context_builder=SimpleNamespace(hooks=handler_manager))
        }
        security_mod._reload_live_hooks(request, state)

        assert _normalize(handler_manager._config) == _normalize(watcher_manager._config)

    def test_the_security_handler_is_a_no_op_without_a_context_builder(self) -> None:
        from kiro_crew.dashboard.handlers import security as security_mod

        request = MagicMock()
        request.app = {"state": SimpleNamespace(context_builder=None)}
        security_mod._reload_live_hooks(request, _keystone_state())  # must not raise

    @pytest.mark.asyncio
    async def test_an_unrelated_write_does_not_reload_hooks(self) -> None:
        from kiro_crew.hooks import HookManager

        manager = HookManager()
        manager.watch_config()
        with patch.object(manager, "reload") as reload:
            await _dispatch(KiroCrewConfig(), "session.timeout_secs")
        reload.assert_not_called()

    def test_a_widened_auto_approve_set_is_sel_audited_by_count(self) -> None:
        """config.json is agent-writable, so a reload that widens what runs without a
        prompt leaves an audit row -- counts and flag names, never tool names."""
        from kiro_crew import hooks as hooks_mod

        manager = hooks_mod.HookManager(
            hooks_mod.HooksConfig.from_dict({"auto_approve_tools": ["fs_read"]})
        )
        manager.watch_config()
        cfg = KiroCrewConfig()
        cfg.hooks = {  # type: ignore[assignment]
            "auto_approve_tools": ["fs_read", "execute_bash", "fs_write"],
            "auto_approve_subagent_spawn": True,
        }
        sel_obj = MagicMock()
        with (
            patch.object(hooks_mod, "sel", return_value=sel_obj),
            patch.object(hooks_mod, "load_denied_commands_state", return_value=_keystone_state()),
        ):
            manager._on_config_change(_change(cfg, "hooks.auto_approve_tools"))
        assert manager.auto_approve_subagent_spawn is True
        sel_obj.log_api_access.assert_called_once()
        kw = sel_obj.log_api_access.call_args.kwargs
        assert kw["operation"] == "hook_manager.reconfigure"
        assert kw["outcome"] == "auto_approve_changed"
        assert "added=2 removed=0" in kw["resources"]
        assert "subagent_spawn" in kw["resources"]
        assert "execute_bash" not in kw["resources"]

    def test_an_unchanged_auto_approve_set_leaves_no_audit_row(self) -> None:
        from kiro_crew import hooks as hooks_mod

        manager = hooks_mod.HookManager(hooks_mod.HooksConfig.from_dict(_hooks_flat_section()))
        manager.watch_config()
        cfg = KiroCrewConfig()
        cfg.hooks = _hooks_flat_section()  # type: ignore[assignment]
        sel_obj = MagicMock()
        with (
            patch.object(hooks_mod, "sel", return_value=sel_obj),
            patch.object(hooks_mod, "load_denied_commands_state", return_value=_keystone_state()),
        ):
            manager._on_config_change(_change(cfg, "hooks.auto_replies"))
        sel_obj.log_api_access.assert_not_called()


# ==================================================================
# 4. CronHistoryStore
# ==================================================================


def _cron_history_store(tmp_path: Path):
    from kiro_crew.cron_history import CronHistoryStore

    return CronHistoryStore(base_dir=tmp_path, _defer_prepare=True)


def _cron_history_cfg(
    *, summary: int = 900, trace_kb: int = 64, per_job: int = 30, index: int = 400
) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.cron_history.cron_summary_cap = summary
    cfg.cron_history.cron_trace_cap_kb = trace_kb
    cfg.cron_history.cron_max_records_per_job = per_job
    cfg.cron_history.cron_max_index_records = index
    return cfg


class TestCronHistoryStore:
    def test_subscribes_on_cron_history(self, tmp_path: Path) -> None:
        store = _cron_history_store(tmp_path)
        sub = _one_sub("CronHistoryStore")
        assert sub.prefixes == ("cron_history",)
        assert store._config_sub is sub

    def test_reconfigure_adopts_the_caps(self, tmp_path: Path) -> None:
        store = _cron_history_store(tmp_path)
        store.reconfigure(_cron_history_cfg())
        assert store._summary_cap == 900
        assert store._max_records_per_job == 30
        assert store._max_index_records == 400

    def test_the_trace_cap_is_re_multiplied_to_bytes(self, tmp_path: Path) -> None:
        """``_trace_cap`` is bytes and the config key is KB -- copying it would 1024x."""
        store = _cron_history_store(tmp_path)
        store.reconfigure(_cron_history_cfg(trace_kb=64))
        assert store._trace_cap == 64 * 1024

    @pytest.mark.asyncio
    async def test_a_cron_history_write_reaches_the_store(self, tmp_path: Path) -> None:
        store = _cron_history_store(tmp_path)
        await _dispatch(_cron_history_cfg(trace_kb=8), "cron_history.cron_trace_cap_kb")
        assert store._trace_cap == 8 * 1024

    @pytest.mark.asyncio
    async def test_an_unrelated_cron_write_does_not_reach_it(self, tmp_path: Path) -> None:
        store = _cron_history_store(tmp_path)
        with patch.object(store, "reconfigure") as reconfigure:
            await _dispatch(KiroCrewConfig(), "cron.max_jobs")
        reconfigure.assert_not_called()


# ==================================================================
# 5. metrics provider: any telemetry.* change rebuilds the recorder ONCE
# ==================================================================


class TestTelemetryRecorderRebuild:
    @pytest.fixture(autouse=True)
    def _no_registered_applier(self):
        from kiro_crew.metrics import provider as provider_mod

        saved = provider_mod._config_sub
        provider_mod._config_sub = None
        yield
        provider_mod._config_sub = None
        provider_mod._config_sub = saved

    def test_watch_config_subscribes_on_telemetry(self) -> None:
        from kiro_crew.metrics import provider as provider_mod

        provider_mod.watch_config()
        assert _one_sub("telemetry").prefixes == ("telemetry",)

    def test_watch_config_is_idempotent(self) -> None:
        """A re-entered boot path must not stack appliers that each rebuild."""
        from kiro_crew.metrics import provider as provider_mod

        provider_mod.watch_config()
        provider_mod.watch_config()
        provider_mod.watch_config()
        assert len(_subs_named("telemetry")) == 1

    @pytest.mark.asyncio
    async def test_a_telemetry_change_rebuilds_the_recorder_exactly_once(self) -> None:
        from kiro_crew.metrics import provider as provider_mod

        provider_mod.watch_config()
        with patch.object(provider_mod, "shutdown") as shutdown:
            await _dispatch(
                KiroCrewConfig(), "telemetry.retention_days", "telemetry.export_interval_seconds"
            )
        # One rebuild for the whole section, not one per changed field.
        shutdown.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_the_rebuild_runs_off_the_event_loop(self) -> None:
        """``shutdown`` flushes on the calling thread, so it must not run on the loop."""
        from kiro_crew.metrics import provider as provider_mod

        provider_mod.watch_config()
        with (
            patch.object(provider_mod, "shutdown") as shutdown,
            patch("asyncio.to_thread", new=AsyncMock()) as to_thread,
        ):
            await _dispatch(KiroCrewConfig(), "telemetry.enabled")
        to_thread.assert_awaited_once_with(shutdown)

    @pytest.mark.asyncio
    async def test_an_unrelated_write_does_not_rebuild(self) -> None:
        from kiro_crew.metrics import provider as provider_mod

        provider_mod.watch_config()
        with patch.object(provider_mod, "shutdown") as shutdown:
            await _dispatch(KiroCrewConfig(), "dashboard.bot_name")
        shutdown.assert_not_called()


# ==================================================================
# 6. dashboard chat entry-cache bounds memo
# ==================================================================


class TestChatEntryCacheBounds:
    @pytest.fixture(autouse=True)
    def _no_registered_applier(self):
        from kiro_crew.dashboard import chat_persistence as cp

        cp._entry_cache_config_sub = None
        yield
        cp._entry_cache_config_sub = None
        cp._entry_cache_bounds_cached = None
        cp._entry_cache_bounds_read_warned = False

    def test_watch_config_subscribes_on_both_bound_leaves(self) -> None:
        from kiro_crew.dashboard import chat_persistence as cp

        cp.watch_config()
        assert _one_sub("chat-entry-cache-bounds").prefixes == (
            "dashboard.chat_entry_cache_max_entries",
            "dashboard.chat_entry_cache_max_bytes",
        )

    def test_watch_config_is_idempotent(self) -> None:
        from kiro_crew.dashboard import chat_persistence as cp

        cp.watch_config()
        cp.watch_config()
        assert len(_subs_named("chat-entry-cache-bounds")) == 1

    @pytest.mark.asyncio
    async def test_a_bounds_write_invalidates_the_memo(self) -> None:
        from kiro_crew.dashboard import chat_persistence as cp

        cp.watch_config()
        cp._entry_cache_bounds_cached = (1, 2)
        cp._entry_cache_bounds_read_warned = True
        await _dispatch(KiroCrewConfig(), "dashboard.chat_entry_cache_max_bytes")
        assert cp._entry_cache_bounds_cached is None
        # The read-failure latch clears with it, so a NEW failure warns again.
        assert cp._entry_cache_bounds_read_warned is False

    @pytest.mark.asyncio
    async def test_the_next_read_re_resolves_from_the_new_config(self) -> None:
        from kiro_crew.dashboard import chat_persistence as cp

        cp.watch_config()
        cfg = KiroCrewConfig()
        cfg.dashboard.chat_entry_cache_max_entries = 11
        cfg.dashboard.chat_entry_cache_max_bytes = 2222
        with patch.object(cp.KiroCrewConfig, "load", return_value=cfg):
            cp._entry_cache_bounds_cached = (1, 2)
            await _dispatch(cfg, "dashboard.chat_entry_cache_max_entries")
            assert cp._entry_cache_bounds() == (11, 2222)

    @pytest.mark.asyncio
    async def test_an_unrelated_dashboard_write_keeps_the_memo(self) -> None:
        from kiro_crew.dashboard import chat_persistence as cp

        cp.watch_config()
        cp._entry_cache_bounds_cached = (5, 6)
        await _dispatch(KiroCrewConfig(), "dashboard.bot_name")
        assert cp._entry_cache_bounds_cached == (5, 6)


# ==================================================================
# 7. SkillsLoader: extra_paths pushed, max_triggered read at use
# ==================================================================


def _skills_loader(tmp_path: Path, extra: list[str] | None = None, max_triggered: int = 3):
    from kiro_crew.skills import SkillsLoader

    cfg = KiroCrewConfig()
    cfg.skills.extra_paths = list(extra or [])
    cfg.skills.max_triggered = max_triggered
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False, config=cfg)


class TestSkillsLoader:
    def test_subscribes_only_on_extra_paths(self) -> None:
        """``max_triggered`` is read at use, so it needs no subscription."""
        loader = _skills_loader(Path("/nonexistent-for-subscription-check"))
        sub = _one_sub("SkillsLoader")
        assert sub.prefixes == ("skills.extra_paths",)
        assert loader._config_sub is sub

    def test_max_triggered_is_read_at_use_not_at_construction(self, tmp_path: Path) -> None:
        loader = _skills_loader(tmp_path, max_triggered=3)
        cfg = KiroCrewConfig()
        cfg.skills.max_triggered = 9
        live.watch().prime(cfg)
        assert loader._max_triggered_now() == 9
        # No push happened: the boot value is still the fallback.
        assert loader._max_triggered == 3

    def test_the_snapshot_is_read_rather_than_the_disk(self, tmp_path: Path) -> None:
        """This runs once per message, so a stat-and-parse here is the wrong trade."""
        loader = _skills_loader(tmp_path, max_triggered=3)
        cfg = KiroCrewConfig()
        cfg.skills.max_triggered = 6
        live.watch().prime(cfg)
        with patch("kiro_crew.skills.KiroCrewConfig.load") as load:
            assert loader._max_triggered_now() == 6
        load.assert_not_called()

    def test_without_a_snapshot_an_injected_config_is_honoured(self, tmp_path: Path) -> None:
        """The absent-key default is 0, which would suppress every skill."""
        loader = _skills_loader(tmp_path, max_triggered=4)
        assert live.snapshot() is None
        assert loader._max_triggered_now() == 4

    def test_a_malformed_snapshot_value_falls_back_to_the_boot_cap(self, tmp_path: Path) -> None:
        """A momentarily unreadable cap must not collapse to zero and hide everything."""
        loader = _skills_loader(tmp_path, max_triggered=4)
        cfg = KiroCrewConfig()
        cfg.skills.max_triggered = "many"  # type: ignore[assignment]
        live.watch().prime(cfg)
        assert loader._max_triggered_now() == 4

    @pytest.mark.asyncio
    async def test_the_applier_adopts_a_newly_added_root_screened_off_the_loop(
        self, tmp_path: Path
    ) -> None:
        loader = _skills_loader(tmp_path)
        added = tmp_path / "added"
        added.mkdir()
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(added)]
        real_to_thread = asyncio.to_thread
        seen: list[object] = []

        async def spy(fn, *args, **kw):
            seen.append(fn)
            return await real_to_thread(fn, *args, **kw)

        with patch("kiro_crew.skills.asyncio.to_thread", side_effect=spy):
            await loader._on_config_change(_change(cfg, "skills.extra_paths"))
        assert added.resolve() in loader._extra_paths
        # The resolve/is_dir screening stats every root; it belongs off the loop.
        assert seen == [loader._screen_extra_paths]

    def test_a_removed_root_is_dropped(self, tmp_path: Path) -> None:
        first = tmp_path / "first"
        first.mkdir()
        loader = _skills_loader(tmp_path, extra=[str(first)])
        assert first.resolve() in loader._extra_paths
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = []
        loader.reconfigure(cfg)
        assert first.resolve() not in loader._extra_paths

    def test_a_sensitive_root_is_refused_on_reload_as_at_boot(self, tmp_path: Path) -> None:
        """Fail closed per entry: a hand-added root cannot reach a credential dir."""
        loader = _skills_loader(tmp_path)
        secret = tmp_path / "secret"
        secret.mkdir()
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(secret)]
        with patch("kiro_crew.skills.is_sensitive_path", return_value=True):
            loader.reconfigure(cfg)
        assert secret.resolve() not in loader._extra_paths

    def test_a_missing_root_is_dropped_rather_than_admitted(self, tmp_path: Path) -> None:
        loader = _skills_loader(tmp_path)
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(tmp_path / "does-not-exist")]
        loader.reconfigure(cfg)
        assert loader._extra_paths == list(loader._edition_extra_paths)

    def test_edition_roots_survive_a_config_write_and_stay_last(self, tmp_path: Path) -> None:
        """Edition roots come from the platform context, so config must not drop them."""
        edition = tmp_path / "edition"
        edition.mkdir()
        configured = tmp_path / "configured"
        configured.mkdir()
        loader = _skills_loader(tmp_path)
        loader._edition_extra_paths = [edition.resolve()]
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(configured)]
        loader.reconfigure(cfg)
        assert loader._extra_paths == [configured.resolve(), edition.resolve()]

    def test_the_discovery_cache_is_cleared_so_the_next_listing_walks_the_new_roots(
        self, tmp_path: Path
    ) -> None:
        loader = _skills_loader(tmp_path)
        loader._iter_cache["k"] = (0.0, [])
        loader.reconfigure(KiroCrewConfig())
        assert loader._iter_cache == {}

    @pytest.mark.asyncio
    async def test_a_max_triggered_write_does_not_reach_the_loader(self, tmp_path: Path) -> None:
        loader = _skills_loader(tmp_path)
        with patch.object(loader, "reconfigure") as reconfigure:
            await _dispatch(KiroCrewConfig(), "skills.max_triggered")
        reconfigure.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_extra_paths_write_reaches_the_loader(self, tmp_path: Path) -> None:
        added = tmp_path / "later"
        added.mkdir()
        loader = _skills_loader(tmp_path)
        cfg = KiroCrewConfig()
        cfg.skills.extra_paths = [str(added)]
        await _dispatch(cfg, "skills.extra_paths")
        assert added.resolve() in loader._extra_paths


# ==================================================================
# 8. SshTunnelManager.apply_config
# ==================================================================


def _tunnel_manager():
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    registry = MagicMock()
    registry.list.return_value = []
    return SshTunnelManager(registry)


def _instances_cfg(
    *,
    connect: float = 12.0,
    mint: float = 6.0,
    compression: bool = False,
    max_recovery: int = 7,
    backoff_max: float = 33.0,
    probe_fails: int = 5,
) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.instances.connect_timeout_secs = connect
    cfg.instances.mint_timeout_secs = mint
    cfg.instances.ssh_compression = compression
    cfg.instances.max_recovery_attempts = max_recovery
    cfg.instances.recover_backoff_max_secs = backoff_max
    cfg.instances.probe_failure_threshold = probe_fails
    return cfg


class TestSshTunnelManagerApplyConfig:
    """The hot-apply entry point is ``apply_config``, not ``reconfigure``.

    ``reconfigure(instance_id, apply)`` already existed on this class as the
    async per-instance barrier, so the config applier carries a different name.
    """

    def test_the_two_names_are_distinct_methods(self) -> None:
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        assert asyncio.iscoroutinefunction(SshTunnelManager.reconfigure)
        assert not asyncio.iscoroutinefunction(SshTunnelManager.apply_config)

    def test_subscribes_on_instances(self) -> None:
        manager = _tunnel_manager()
        sub = _one_sub("SshTunnelManager")
        assert sub.prefixes == ("instances",)
        assert manager._config_sub is sub

    def test_apply_config_adopts_every_transport_tunable(self) -> None:
        manager = _tunnel_manager()
        manager.apply_config(_instances_cfg().instances)
        assert manager._connect_timeout == 12.0
        assert manager._mint_timeout == 6.0
        assert manager._ssh_compression is False
        assert manager._max_recovery == 7
        assert manager._recover_backoff_max == 33.0
        assert manager._probe_fails == 5

    def test_the_probe_threshold_is_pushed_into_already_running_tunnels(self) -> None:
        """Each tunnel copied the threshold when built and compares it per probe."""
        manager = _tunnel_manager()
        live_a = SimpleNamespace(_probe_fails=2)
        live_b = SimpleNamespace(_probe_fails=2)
        manager._tunnels = {"a": live_a, "b": live_b}  # type: ignore[assignment]
        manager.apply_config(_instances_cfg(probe_fails=9).instances)
        assert live_a._probe_fails == 9
        assert live_b._probe_fails == 9

    def test_the_base_port_is_deliberately_left_alone(self) -> None:
        """Live tunnels hold ports from the old base; moving it would fragment the range."""
        manager = _tunnel_manager()
        allocator = manager._allocator
        before = allocator.base_port
        cfg = _instances_cfg()
        cfg.instances.tunnel_base_port = before + 500
        manager.apply_config(cfg.instances)
        assert manager._allocator is allocator
        assert allocator.base_port == before

    @pytest.mark.asyncio
    async def test_an_instances_write_reaches_the_manager_and_its_tunnels(self) -> None:
        manager = _tunnel_manager()
        tunnel = SimpleNamespace(_probe_fails=1)
        manager._tunnels = {"a": tunnel}  # type: ignore[assignment]
        await _dispatch(_instances_cfg(probe_fails=4), "instances.probe_failure_threshold")
        assert manager._probe_fails == 4
        assert tunnel._probe_fails == 4

    @pytest.mark.asyncio
    async def test_an_unrelated_write_does_not_reach_the_manager(self) -> None:
        manager = _tunnel_manager()
        with patch.object(manager, "apply_config") as apply_config:
            await _dispatch(KiroCrewConfig(), "dashboard.bot_name")
        apply_config.assert_not_called()


# ==================================================================
# 9. knowledge: LLMPool.reconfigure + the dashboard applier
# ==================================================================


def _pool(size: int = 2, **kw):
    from kiro_crew.knowledge.llm_pool import LLMPool

    return LLMPool(size, **kw)


class TestLLMPoolReconfigure:
    def test_subscribes_on_the_two_knowledge_leaves(self) -> None:
        pool = _pool()
        sub = _one_sub("LLMPool")
        assert sub.prefixes == (
            "knowledge.pool_idle_ttl_secs",
            "knowledge.extraction_pool_size",
        )
        assert pool._config_sub is sub

    @pytest.mark.asyncio
    async def test_the_idle_ttl_is_adopted_immediately(self) -> None:
        pool = _pool()
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"pool_idle_ttl_secs": 90}},
        ):
            await pool.reconfigure()
        assert pool._idle_ttl == 90

    @pytest.mark.asyncio
    async def test_a_tracking_pool_follows_the_configured_size(self) -> None:
        pool = _pool(2, use_config_pool_size=True)
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 5}},
        ):
            await pool.reconfigure()
        assert pool._pool_size == 5

    @pytest.mark.asyncio
    async def test_a_fixed_width_pool_is_never_resized(self) -> None:
        """The URL-fetch pool is width 1 by its caller's choice, not by this key."""
        pool = _pool(1, use_config_pool_size=False)
        assert pool._track_config_pool_size is False
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 8}},
        ):
            await pool.reconfigure()
        assert pool._pool_size == 1

    @pytest.mark.asyncio
    async def test_track_config_pool_size_can_be_opted_into_independently(self) -> None:
        """The extraction pool seeds its own width yet still follows later writes."""
        pool = _pool(3, use_config_pool_size=False, track_config_pool_size=True)
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 6}},
        ):
            await pool.reconfigure()
        assert pool._pool_size == 6

    @pytest.mark.asyncio
    async def test_a_malformed_value_keeps_the_documented_default(self) -> None:
        pool = _pool(2)
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"pool_idle_ttl_secs": "soon"}},
        ):
            await pool.reconfigure()
        from kiro_crew.knowledge.llm_pool import DEFAULT_IDLE_TTL_SECS

        assert pool._idle_ttl == DEFAULT_IDLE_TTL_SECS

    @pytest.mark.asyncio
    async def test_a_pre_start_resize_also_resizes_the_semaphore(self) -> None:
        """The extraction pool is built at route setup and started on the first
        ingest. A write in between must move the PERMITS with the width, or start()
        spawns 1 worker behind a 3-permit semaphore and over-admits acquire()."""
        pool = _pool(3, use_config_pool_size=False, track_config_pool_size=True)
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 1}},
        ):
            await pool.reconfigure()
        assert pool._pool_size == 1
        assert pool._semaphore._value == 1

    @pytest.mark.asyncio
    async def test_start_sizes_the_semaphore_from_the_width_it_spawns(self) -> None:
        """Whichever path set ``_pool_size`` before start(), the permit count equals
        the worker count start() actually creates."""
        pool = _pool(3, use_config_pool_size=False, track_config_pool_size=True)
        pool._pool_size = 1  # as a tracked reload would leave it
        pool._semaphore = asyncio.Semaphore(3)  # the stale constructor width

        async def fake_worker():
            return SimpleNamespace(shutdown=AsyncMock())

        with (
            patch("kiro_crew.knowledge.llm_pool._read_config", return_value={"knowledge": {}}),
            patch.object(pool, "_create_worker", side_effect=fake_worker),
        ):
            await pool.start()
        try:
            assert len(pool._workers) == 1
            assert pool._semaphore._value == 1
        finally:
            await pool.shutdown()

    @pytest.mark.asyncio
    async def test_an_unstarted_pool_arms_no_reaper(self) -> None:
        pool = _pool()
        assert pool._started is False
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"pool_idle_ttl_secs": 30}},
        ):
            await pool.reconfigure()
        assert pool._reaper_task is None

    @pytest.mark.asyncio
    async def test_a_started_pool_arms_a_reaper_the_ttl_had_never_created(self) -> None:
        pool = _pool()
        pool._started = True
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._read_config",
                return_value={"knowledge": {"pool_idle_ttl_secs": 30}},
            ),
            patch.object(pool, "_idle_reaper", new=AsyncMock()),
        ):
            await pool.reconfigure()
            assert pool._reaper_task is not None
            pool._reaper_task.cancel()

    @pytest.mark.asyncio
    async def test_a_zero_ttl_pool_recycles_for_a_new_width_when_idle(self) -> None:
        """No idle TTL means no reaper, so a width change must find its own boundary:
        an idle pool is recycled at once, and the next acquire respawns at the new
        width."""
        pool = _pool(2, track_config_pool_size=True)
        pool._started = True
        pool._idle_ttl = 0.0
        pool._workers = [MagicMock(shutdown=AsyncMock()), MagicMock(shutdown=AsyncMock())]
        pool._in_use = 0
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 5, "pool_idle_ttl_secs": 0}},
        ):
            await pool.reconfigure()
        assert pool._started is False, "the idle pool is recycled immediately"
        assert pool._pool_size == 5
        assert pool._semaphore._value == 5, "the respawn semaphore carries the new width"
        assert pool._resize_when_idle is False

    @pytest.mark.asyncio
    async def test_a_width_change_taken_under_a_positive_ttl_is_not_stranded_by_a_later_zero_ttl(
        self,
    ) -> None:
        """Write 1 widens the pool while the TTL is positive (left to the reaper);
        write 2 drops the TTL to zero before the reaper fired. The second write
        carries no width delta of its own, so the resize must be armed off the
        RUNNING width, or the pool stays at the old width until a restart."""
        pool = _pool(2, track_config_pool_size=True)
        pool._started = True
        pool._idle_ttl = 300.0
        pool._workers = [MagicMock(shutdown=AsyncMock()), MagicMock(shutdown=AsyncMock())]
        pool._in_use = 0
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 5, "pool_idle_ttl_secs": 300}},
        ):
            await pool.reconfigure()
        assert pool._started is True and pool._pool_size == 5, "left to the reaper"
        if pool._reaper_task is not None:
            pool._reaper_task.cancel()
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 5, "pool_idle_ttl_secs": 0}},
        ):
            await pool.reconfigure()
        assert pool._started is False, "the idle pool is recycled at the new width"
        assert pool._semaphore._value == 5

    @pytest.mark.asyncio
    async def test_a_zero_ttl_pool_defers_the_recycle_until_the_last_release(self) -> None:
        pool = _pool(2, track_config_pool_size=True)
        pool._started = True
        pool._idle_ttl = 0.0
        pool._workers = [MagicMock(shutdown=AsyncMock()), MagicMock(shutdown=AsyncMock())]
        pool._in_use = 1  # an ingest is in flight
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 3, "pool_idle_ttl_secs": 0}},
        ):
            await pool.reconfigure()
        assert pool._started is True, "a worker with a chunk in flight is never killed"
        assert pool._resize_when_idle is True
        pool.release(0)
        for _ in range(20):
            if not pool._started:
                break
            await asyncio.sleep(0)
        assert pool._started is False, "the last release recycles the pool"
        assert pool._semaphore._value == 3

    @pytest.mark.asyncio
    async def test_the_config_read_runs_off_the_event_loop(self) -> None:
        pool = _pool()
        with (
            patch("kiro_crew.knowledge.llm_pool._read_config", return_value={}) as read,
            patch("asyncio.to_thread", new=AsyncMock(return_value={})) as to_thread,
        ):
            await pool.reconfigure()
        to_thread.assert_awaited_once_with(read)

    @pytest.mark.asyncio
    async def test_a_knowledge_write_reaches_the_pool(self) -> None:
        """The applier adopts the document the watcher loaded, not a second read."""
        pool = _pool()
        cfg = KiroCrewConfig()
        cfg.knowledge.pool_idle_ttl_secs = 45
        with patch("kiro_crew.knowledge.llm_pool._read_config") as read:
            await _dispatch(cfg, "knowledge.pool_idle_ttl_secs")
        read.assert_not_called()
        assert pool._idle_ttl == 45

    @pytest.mark.asyncio
    async def test_an_unrelated_knowledge_leaf_does_not_reach_the_pool(self) -> None:
        pool = _pool()
        with patch.object(pool, "reconfigure", new=AsyncMock()) as reconfigure:
            await _dispatch(KiroCrewConfig(), "knowledge.auto_ingest_artifacts")
        reconfigure.assert_not_awaited()


class TestKnowledgeRoutesApplier:
    @pytest.mark.asyncio
    async def test_the_subscription_is_held_on_the_app(self) -> None:
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        app: dict = {}
        await knowledge_mod._watch_knowledge_config(app)  # type: ignore[arg-type]
        sub = _one_sub("knowledge-routes")
        assert sub.prefixes == ("knowledge",)
        assert app["_knowledge_config_sub"] is sub

    @pytest.mark.asyncio
    async def test_an_embedder_setting_rebuilds_and_swaps_it_into_the_pipeline(self) -> None:
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        pipeline = SimpleNamespace(embedder=object())
        app: dict = {"knowledge_pipeline": pipeline}
        new_embedder = object()
        with patch.object(knowledge_mod, "_create_embedder", return_value=new_embedder):
            await knowledge_mod._apply_knowledge_config(
                app,  # type: ignore[arg-type]
                _change(KiroCrewConfig(), "knowledge.embed_timeout_secs"),
            )
        assert app["knowledge_embedder"] is new_embedder
        assert pipeline.embedder is new_embedder

    @pytest.mark.asyncio
    async def test_a_failed_rebuild_keeps_the_old_embedder(self) -> None:
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        old = object()
        app: dict = {"knowledge_embedder": old}
        with patch.object(knowledge_mod, "_create_embedder", side_effect=RuntimeError("boom")):
            await knowledge_mod._apply_knowledge_config(
                app,  # type: ignore[arg-type]
                _change(KiroCrewConfig(), "knowledge.embed_content_budget"),
            )
        assert app["knowledge_embedder"] is old

    @pytest.mark.asyncio
    async def test_turning_auto_ingest_off_stops_the_listener(self) -> None:
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        app: dict = {"artifact_knowledge_sync": object()}
        cfg = KiroCrewConfig()
        cfg.knowledge.auto_ingest_artifacts = False
        with patch.object(knowledge_mod, "_stop_artifact_ingest", new=AsyncMock()) as stop:
            await knowledge_mod._apply_knowledge_config(
                app,  # type: ignore[arg-type]
                _change(cfg, "knowledge.auto_ingest_artifacts"),
            )
        stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_turning_auto_ingest_on_registers_the_listener(self) -> None:
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        app: dict = {"artifact_knowledge_sync": None}
        cfg = KiroCrewConfig()
        cfg.knowledge.auto_ingest_artifacts = True
        with patch.object(knowledge_mod, "_start_artifact_ingest_async", new=AsyncMock()) as start:
            await knowledge_mod._apply_knowledge_config(
                app,  # type: ignore[arg-type]
                _change(cfg, "knowledge.auto_ingest_artifacts"),
            )
        start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_kinds_set_is_swapped_on_the_live_listener(self) -> None:
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        sync = SimpleNamespace(kinds={"markdown"})
        app: dict = {"artifact_knowledge_sync": sync}
        cfg = KiroCrewConfig()
        cfg.knowledge.auto_ingest_artifact_kinds = ["widget", "html"]
        await knowledge_mod._apply_knowledge_config(
            app,  # type: ignore[arg-type]
            _change(cfg, "knowledge.auto_ingest_artifact_kinds"),
        )
        assert sync.kinds == {"widget", "html"}

    @pytest.mark.asyncio
    async def test_an_untouched_knowledge_leaf_changes_nothing(self) -> None:
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        app: dict = {"artifact_knowledge_sync": SimpleNamespace(kinds={"markdown"})}
        with (
            patch.object(knowledge_mod, "_create_embedder") as create,
            patch.object(knowledge_mod, "_stop_artifact_ingest", new=AsyncMock()) as stop,
        ):
            await knowledge_mod._apply_knowledge_config(
                app,  # type: ignore[arg-type]
                _change(KiroCrewConfig(), "knowledge.max_results"),
            )
        create.assert_not_called()
        stop.assert_not_awaited()
        assert app["artifact_knowledge_sync"].kinds == {"markdown"}


# ==================================================================
# 11. mcp_gateway.resolve_once_refresh_hours: docstring matches behaviour
# ==================================================================


class TestResolveOnceRefreshWindowIsLive:
    def test_the_window_comes_from_the_live_snapshot(self) -> None:
        from kiro_crew.slack.gateway import GatewayOrchestrator

        gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
        cfg = KiroCrewConfig()
        cfg.mcp_gateway.resolve_once_refresh_hours = 6
        live.watch().prime(cfg)
        assert gw._mcp_resolve_refresh_secs() == 6 * 3600.0

    def test_a_later_write_moves_the_window_without_a_broker_restart(self) -> None:
        from kiro_crew.slack.gateway import GatewayOrchestrator

        gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
        first = KiroCrewConfig()
        first.mcp_gateway.resolve_once_refresh_hours = 24
        live.watch().prime(first)
        assert gw._mcp_resolve_refresh_secs() == 24 * 3600.0
        second = KiroCrewConfig()
        second.mcp_gateway.resolve_once_refresh_hours = 1
        live.watch().prime(second)
        assert gw._mcp_resolve_refresh_secs() == 3600.0

    def test_without_a_snapshot_it_falls_back_to_the_boot_copy_never_a_load(self) -> None:
        """Before the watcher primes, the method reads the boot ``_cfg``: a
        ``load()`` here would parse and validate the file on the event loop."""
        from kiro_crew.slack import gateway as gateway_mod

        gw = gateway_mod.GatewayOrchestrator.__new__(gateway_mod.GatewayOrchestrator)
        cfg = KiroCrewConfig()
        cfg.mcp_gateway.resolve_once_refresh_hours = 3
        gw._cfg = cfg
        with patch.object(gateway_mod.KiroCrewConfig, "load") as load:
            assert gw._mcp_resolve_refresh_secs() == 3 * 3600.0
        load.assert_not_called()

    def test_zero_hours_is_floored_by_the_loops_own_minimum_sleep(self) -> None:
        """``0`` means "always stale" for freshness, never a spin loop for the timer."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
        cfg = KiroCrewConfig()
        cfg.mcp_gateway.resolve_once_refresh_hours = 0
        live.watch().prime(cfg)
        assert gw._mcp_resolve_refresh_secs() == 0.0
        assert max(gw._mcp_resolve_refresh_secs(), gw._MCP_RESOLVE_MIN_SLEEP_SECS) == 300.0
