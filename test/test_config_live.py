"""Tests for ``kiro_crew.config.live`` and the plumbing that feeds it.

Covers the diff semantics, the subscriber registry, one forced reload cycle
against a temp ``config.json``, the loader's writer -> ``notify_config_written``
hook, the ``restart=True`` schema metadata, and the PATCH/PUT handlers
computing ``restart_required`` from that metadata.
"""

from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from _hot_reload_helpers import write_config as _write

from kiro_crew.config import live
from kiro_crew.config.live import (
    ConfigChange,
    ConfigWatch,
    diff_config_docs,
    flatten_config,
)
from kiro_crew.config.loader import KiroCrewConfig, update_config_locked

# ── helpers ───────────────────────────────────────────────────────────────


@pytest.fixture
def cfg_file(tmp_path: Path):
    live.reset_for_tests()
    cfg = tmp_path / "config.json"
    _write(cfg, {"agent": {"model": "model-a", "log_level": "INFO"}})
    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg),
        patch("kiro_crew.config.loader.config_local_path", return_value=tmp_path / "local.json"),
    ):
        yield cfg
    live.reset_for_tests()


# ── flatten / diff ────────────────────────────────────────────────────────


class TestFlattenDiff:
    def test_flatten_leaves_and_lists(self) -> None:
        flat = flatten_config({"a": {"b": 1, "c": [1, 2]}, "d": {}, "e": "x"})
        assert flat == {"a.b": 1, "a.c": [1, 2], "d": {}, "e": "x"}

    def test_diff_reports_changed_added_removed(self) -> None:
        old = {"a": {"b": 1, "c": 2}, "gone": True}
        new = {"a": {"b": 1, "c": 3}, "added": {"x": 0}}
        assert diff_config_docs(old, new) == {"a.c", "gone", "added.x"}

    def test_diff_against_none_is_every_leaf(self) -> None:
        assert diff_config_docs(None, {"a": {"b": 1}, "c": 2}) == {"a.b", "c"}

    def test_touched_and_under_prefix_matching(self) -> None:
        ch = ConfigChange(old=None, new=None, changed=frozenset({"session.timeout_secs", "agents.default.model"}))  # type: ignore[arg-type]
        assert ch.touched("session")
        assert ch.touched("agents.default.model")
        assert not ch.touched("sessions")  # prefix must be a whole segment
        assert not ch.touched("agent")  # "agents" is not under "agent"
        assert ch.under("agents") == {"agents.default.model"}


# ── registry ──────────────────────────────────────────────────────────────


class TestRegistry:
    def test_bound_method_is_held_weakly(self) -> None:
        w = ConfigWatch(poll_interval_secs=0.05)

        class Holder:
            def on_change(self, change: ConfigChange) -> None:  # pragma: no cover
                pass

        h = Holder()
        sub = w.subscribe("agent", callback=h.on_change, name="holder")
        assert sub.callback() is not None
        del h
        gc.collect()
        assert sub.callback() is None
        assert list(w.subscriptions()) == []

    def test_cancel_removes(self) -> None:
        w = ConfigWatch()
        sub = w.subscribe(callback=lambda c: None, name="x")
        assert len(list(w.subscriptions())) == 1
        sub.cancel()
        assert list(w.subscriptions()) == []

    def test_non_callable_refused(self) -> None:
        with pytest.raises(TypeError):
            ConfigWatch().subscribe(callback="nope")  # type: ignore[arg-type]


# ── reload cycle ──────────────────────────────────────────────────────────


class TestRefreshNow:
    @pytest.mark.asyncio
    async def test_refresh_dispatches_only_matching_subscribers(self, cfg_file: Path) -> None:
        w = ConfigWatch(poll_interval_secs=0.05)
        w.prime(KiroCrewConfig.load())
        seen: dict[str, list[frozenset[str]]] = {"agent": [], "session": [], "all": []}
        w.subscribe("agent.model", callback=lambda c: seen["agent"].append(c.changed), name="a")
        w.subscribe("session", callback=lambda c: seen["session"].append(c.changed), name="s")

        async def _all(c: ConfigChange) -> None:
            seen["all"].append(c.changed)

        w.subscribe(callback=_all, name="all")

        _write(cfg_file, {"agent": {"model": "model-bbbb", "log_level": "INFO"}})
        change = await w.refresh_now()
        assert change is not None
        assert "agent.model" in change.changed
        assert change.new.agent.model == "model-bbbb"
        assert change.old is not None and change.old.agent.model == "model-a"
        assert seen["agent"] == [change.changed]
        assert seen["session"] == []
        assert seen["all"] == [change.changed]
        assert w.snapshot() is change.new

    @pytest.mark.asyncio
    async def test_unchanged_file_returns_none(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        assert await w.refresh_now() is None

    @pytest.mark.asyncio
    async def test_prime_prevents_first_tick_replay(self, cfg_file: Path) -> None:
        w = ConfigWatch(poll_interval_secs=0.05)
        calls: list[frozenset[str]] = []
        w.subscribe(callback=lambda c: calls.append(c.changed), name="all")
        await w.start(initial=KiroCrewConfig.load())
        try:
            await asyncio.sleep(0.2)
            assert calls == []
        finally:
            await w.stop()

    @pytest.mark.asyncio
    async def test_an_edit_made_between_boot_load_and_start_is_applied(
        self, cfg_file: Path
    ) -> None:
        """The boot config is primed without the file's fingerprint, so a write that
        landed while the gateway was still booting is diffed and dispatched on the
        first cycle instead of being paired with a fingerprint that already has it."""
        booted = KiroCrewConfig.load()
        _write(cfg_file, {"agent": {"model": "model-boot-edit", "log_level": "INFO"}})
        w = ConfigWatch(poll_interval_secs=0.05)
        got: list[frozenset[str]] = []
        w.subscribe("agent.model", callback=lambda c: got.append(c.changed), name="m")
        await w.start(initial=booted)
        try:
            for _ in range(40):
                if got:
                    break
                await asyncio.sleep(0.05)
            assert got and "agent.model" in got[0]
            assert w.snapshot() is not None and w.snapshot().agent.model == "model-boot-edit"
        finally:
            await w.stop()

    @pytest.mark.asyncio
    async def test_a_hold_defers_dispatch_until_the_transaction_releases(
        self, cfg_file: Path
    ) -> None:
        """A channel save writes config.json then .env and rolls the config back if
        the credential write fails. Under a hold nothing intermediate is applied;
        the release wakes the watcher on whatever the transaction committed."""
        w = ConfigWatch(poll_interval_secs=0.05)
        got: list[str] = []
        w.subscribe("agent.model", callback=lambda c: got.append(c.new.agent.model), name="m")
        await w.start(initial=KiroCrewConfig.load())
        try:
            with w.hold():
                _write(cfg_file, {"agent": {"model": "model-intermediate", "log_level": "INFO"}})
                w.notify_written()
                assert await w.refresh_now() is None  # a direct cycle is held too
                await asyncio.sleep(0.2)
                assert got == []
                # The "rollback": the transaction ends on a different value.
                _write(cfg_file, {"agent": {"model": "model-committed", "log_level": "INFO"}})
            for _ in range(40):
                if got:
                    break
                await asyncio.sleep(0.05)
            assert got == ["model-committed"]
        finally:
            await w.stop()

    @pytest.mark.asyncio
    async def test_a_hold_that_begins_during_the_load_discards_that_load(
        self, cfg_file: Path
    ) -> None:
        """The poll may already be reading the file when a transaction starts.

        That read can return the transaction's uncommitted intermediate state, so
        a hold that begins after the entry check but before adoption must still
        win: nothing from that load is adopted or dispatched, and the release
        forces a fresh load of what the transaction committed.
        """
        w = ConfigWatch(poll_interval_secs=0.05)
        got: list[str] = []
        w.subscribe("agent.model", callback=lambda c: got.append(c.new.agent.model), name="m")
        await w.start(initial=KiroCrewConfig.load())
        held = w.hold()
        real_load = ConfigWatch._load_with_doc

        def load_then_hold() -> Any:
            # The file already carries the intermediate value when the load runs;
            # the transaction's hold lands while that load is in flight.
            result = real_load()
            held.__enter__()
            return result

        try:
            _write(cfg_file, {"agent": {"model": "model-intermediate", "log_level": "INFO"}})
            with patch.object(ConfigWatch, "_load_with_doc", staticmethod(load_then_hold)):
                assert await w.refresh_now() is None
            assert got == [], "an in-flight load is not applied once a hold began"
            assert w.snapshot() is not None and w.snapshot().agent.model != "model-intermediate"
            _write(cfg_file, {"agent": {"model": "model-committed", "log_level": "INFO"}})
            held.__exit__(None, None, None)
            for _ in range(40):
                if got:
                    break
                await asyncio.sleep(0.05)
            assert got == ["model-committed"]
        finally:
            await w.stop()

    def test_the_module_level_hold_targets_the_process_watcher(self) -> None:
        live.reset_for_tests()
        w = live.watch()
        with live.hold():
            assert w._hold_depth == 1
            with live.hold():
                assert w._hold_depth == 2
        assert w._hold_depth == 0


class TestDegradedReloadKeepsLastValid:
    """The snapshot never carries a torn section's DEFAULTS: appliers refuse a
    degraded document, but ``current()`` and ``bind`` read the snapshot directly,
    and a default there widens a gate (forum activation ``always``, an empty
    allow-list). The values in force stay; the degradation flag stays too."""

    @pytest.mark.asyncio
    async def test_an_unreadable_document_keeps_the_values_in_force(self, cfg_file: Path) -> None:
        _write(
            cfg_file,
            {
                "agent": {"model": "model-a", "log_level": "INFO"},
                "telegram": {"forum_activation": "mention", "allowed_user_ids": [7]},
            },
        )
        w = ConfigWatch()
        good = KiroCrewConfig.load()
        assert good.telegram.forum_activation == "mention"
        w.prime(good)
        seen: list[frozenset[str]] = []
        w.subscribe("telegram", callback=lambda c: seen.append(c.changed), name="tg")

        cfg_file.write_text("{ not json", encoding="utf-8")
        import os

        st = cfg_file.stat()
        os.utime(cfg_file, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        assert await w.refresh_now() is None, "a torn document diffs empty against the kept values"
        snap = w.snapshot()
        assert snap is not None and snap is not good
        assert snap.telegram.forum_activation == "mention"
        assert list(snap.telegram.allowed_user_ids) == [7]
        assert snap.agent.model == "model-a"
        assert "*" in snap.degraded_sections, "the flag the fail-closed gates read is kept"
        assert seen == [], "no applier is handed the defaults"
        with patch("kiro_crew.config.live._WATCH", w):
            assert live.current(good, log_prefix="t") is snap

        # A repair to NEW values is a real change and dispatches them.
        _write(
            cfg_file,
            {
                "agent": {"model": "model-a", "log_level": "INFO"},
                "telegram": {"forum_activation": "off", "allowed_user_ids": [7]},
            },
        )
        change = await w.refresh_now()
        assert change is not None and change.new.telegram.forum_activation == "off"
        # The loader remembers the tear for the life of the process (its gates
        # fail closed on that); the VALUES are the repaired ones regardless.
        assert "*" in change.new.degraded_sections
        assert seen == [change.changed] and "telegram.forum_activation" in change.changed

    @pytest.mark.asyncio
    async def test_a_fail_closed_applier_lands_a_revocation_after_a_repaired_tear(
        self, cfg_file: Path
    ) -> None:
        """The loader keeps the whole-config flag for the life of the process once
        a file failed to parse, so every load after a transient typo carries it.
        The fail-closed appliers must not key on that flag: they would defer
        every tick until a restart while the dashboard reports each save as
        applied, and a revocation written after the typo would never land."""
        import os

        class Transport:
            def __init__(self) -> None:
                self.allowed: list[int] = [7, 9]

            def reconfigure(self, section: Any) -> None:
                self.allowed = list(section.allowed_user_ids)

        class Dispatcher:
            def __init__(self, w: ConfigWatch, t: Transport) -> None:
                self.transport = t
                self._sub = w.watch_section(self, "telegram", target="transport")

        _write(cfg_file, {"telegram": {"allowed_user_ids": [7, 9]}})
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        t = Transport()
        d = Dispatcher(w, t)
        cap: list[int] = []
        w.bind("agent.max_channels", cap.append)

        cfg_file.write_text("{ not json", encoding="utf-8")
        st = cfg_file.stat()
        os.utime(cfg_file, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        assert await w.refresh_now() is None
        assert t.allowed == [7, 9]
        # The repair revokes 9 and moves a bound cap: both land on the tick that
        # reads the repaired file, sticky flag notwithstanding.
        _write(cfg_file, {"telegram": {"allowed_user_ids": [7]}, "agent": {"max_channels": 3}})
        change = await w.refresh_now()
        assert change is not None and "*" in change.new.degraded_sections
        assert t.allowed == [7]
        assert cap == [3]
        assert not w._stale, "nothing is left deferred"
        del d

    @pytest.mark.asyncio
    async def test_a_load_the_file_moved_under_is_held_when_the_loader_flagged_it(
        self, cfg_file: Path
    ) -> None:
        """A write landing between the loader's read and the torn-file probe can
        leave the loader with the torn version and the probe with the whole one.
        Once the loader carries the whole-config flag, a read the file moved
        under is treated as torn: the previous values stay and the next tick
        reads the settled file."""
        _write(cfg_file, {"agent": {"model": "model-a", "log_level": "INFO"}})
        w = ConfigWatch()
        good = KiroCrewConfig.load()
        w.prime(good)
        real = ConfigWatch._load_with_doc

        def load_then_write() -> Any:
            cfg, doc, torn = real()
            # The loader read the good document; a write lands before the
            # fingerprint is re-read. Stand in for the torn read the loader
            # could have made instead: it would carry the whole-config flag.
            _write(cfg_file, {"agent": {"model": "model-b", "log_level": "INFO"}})
            from dataclasses import replace

            return replace(cfg, _degraded_sections=frozenset({"*", "*config.json"})), doc, torn

        _write(cfg_file, {"agent": {"model": "model-a", "log_level": "DEBUG"}})
        with patch.object(ConfigWatch, "_load_with_doc", staticmethod(load_then_write)):
            assert await w.refresh_now() is None
        snap = w.snapshot()
        assert (
            snap is not None and snap.agent.log_level == "INFO"
        ), "the moved-under read is not adopted"
        # The next tick reads the settled file.
        change = await w.refresh_now()
        assert change is not None and change.new.agent.model == "model-b"

    def test_the_helper_keys_on_the_file_not_the_sticky_flag(self) -> None:
        from dataclasses import replace

        from kiro_crew.config.live import _keep_last_valid

        prev = replace(
            KiroCrewConfig(), telegram=replace(KiroCrewConfig().telegram, forum_activation="off")
        )
        flagged = replace(KiroCrewConfig(), _degraded_sections=frozenset({"*", "*config.json"}))
        kept = _keep_last_valid(prev, flagged, torn_now=True)
        assert kept.telegram.forum_activation == "off"
        assert kept.degraded_sections == frozenset({"*", "*config.json"}), "the flag is kept"
        # A flagged load whose file parses NOW is a repaired document: adopted as is.
        assert _keep_last_valid(prev, flagged, torn_now=False) is flagged
        assert (
            _keep_last_valid(None, flagged, torn_now=True) is flagged
        ), "no previous: the load stands"

    def test_the_torn_probe_reads_the_files_the_loader_reads(self, cfg_file: Path) -> None:
        assert ConfigWatch._document_is_torn() is False
        cfg_file.write_text("{ not json", encoding="utf-8")
        assert ConfigWatch._document_is_torn() is True
        cfg_file.write_text("[1, 2]", encoding="utf-8")
        assert ConfigWatch._document_is_torn() is True, "a non-object top level is torn"
        cfg_file.unlink()
        assert ConfigWatch._document_is_torn() is False, "a missing file is not torn"


# ── loader -> notify hook ─────────────────────────────────────────────────


class TestLoaderNotifies:
    def test_update_config_locked_notifies_watcher(self, cfg_file: Path) -> None:
        calls: list[int] = []
        with patch(
            "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
        ):
            update_config_locked(cfg_file, mutate=lambda d: {**d, "x": 1})
        assert calls == [1]

    def test_skipped_write_does_not_notify(self, cfg_file: Path) -> None:
        calls: list[int] = []
        with patch(
            "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
        ):
            update_config_locked(cfg_file, mutate=lambda d: None)
        assert calls == []

    def test_save_notifies_watcher(self, cfg_file: Path) -> None:
        calls: list[int] = []
        cfg = KiroCrewConfig.load()
        with patch(
            "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
        ):
            cfg.save()
        assert calls == [1]

    def test_process_singleton_notify_is_noop_without_watcher(self) -> None:
        live.reset_for_tests()
        live.notify_config_written()  # must not raise
        assert live.snapshot() is None


# ── schema metadata ───────────────────────────────────────────────────────


class TestRequiresRestart:
    def test_hot_fields_and_ancestor_resolution(self) -> None:
        from kiro_crew.config.schema import requires_restart

        assert not requires_restart("agent.model")
        assert not requires_restart("agent.log_level")
        assert not requires_restart("agent.max_subagents")
        assert not requires_restart("nonexistent.path")

    def test_a_registration_at_or_under_a_restart_marked_path_is_refused(self) -> None:
        """``restart=True`` is the admission that no applier exists for a field, so
        the watcher refuses to register one there at construction time -- the
        owner's own tests trip it. A section-wide registration whose applier
        ignores the marked leaf stays allowed."""
        w = ConfigWatch()
        for pre in ("agent.jail", "agent.approval_mode", "agent.dangerously_skip_permissions"):
            with pytest.raises(ValueError, match="restart-marked"):
                w.subscribe(pre, callback=lambda c: None, name="bad")
            with pytest.raises(ValueError, match="restart-marked"):
                w.bind(pre, lambda v: None)
        assert list(w.subscriptions()) == []
        w.subscribe("agent", callback=lambda c: None, name="section-wide")  # allowed
        assert len(list(w.subscriptions())) == 1

    def test_schema_emits_requires_restart_only_when_true(self) -> None:
        from kiro_crew.config.schema import SCHEMA_REGISTRY, config_entry_to_dict

        by_path = {e.path: e for e in SCHEMA_REGISTRY}
        jail = config_entry_to_dict(by_path["agent.jail"])
        assert jail["requiresRestart"] is True
        model = config_entry_to_dict(by_path["agent.model"])
        assert "requiresRestart" not in model
        assert by_path["agent.model"].requires_restart is False


# ── handlers ──────────────────────────────────────────────────────────────


class TestHandlerRestartRequired:
    @pytest.mark.asyncio
    async def test_hot_apply_after_write_is_noop_when_unstarted(self) -> None:
        from kiro_crew.dashboard.handlers.core import _hot_apply_after_write

        live.reset_for_tests()
        await _hot_apply_after_write()  # nothing started: must not raise or load
        assert live.watch().snapshot() is None

    @pytest.mark.asyncio
    async def test_hot_apply_after_write_refreshes_started_watcher(self, cfg_file: Path) -> None:
        from kiro_crew.dashboard.handlers.core import _hot_apply_after_write

        w = live.watch()
        await w.start(initial=KiroCrewConfig.load())
        try:
            seen: list[str] = []
            w.subscribe("agent.log_level", callback=lambda c: seen.append(c.new.agent.log_level))
            _write(cfg_file, {"agent": {"model": "model-a", "log_level": "DEBUG"}})
            await _hot_apply_after_write()
            assert seen == ["DEBUG"]
        finally:
            await w.stop()


class TestLogLevelApplier:
    def test_apply_log_level_from_config(self) -> None:
        import logging

        from kiro_crew.dashboard.handlers.updates import apply_log_level_from_config

        root = logging.getLogger("kiro_crew")
        before = root.level
        try:
            new = KiroCrewConfig()
            new.agent.log_level = "WARNING"
            apply_log_level_from_config(
                ConfigChange(old=None, new=new, changed=frozenset({"agent.log_level"}))
            )
            assert root.level == logging.WARNING
            new.agent.log_level = "bogus"
            apply_log_level_from_config(
                ConfigChange(old=None, new=new, changed=frozenset({"agent.log_level"}))
            )
            assert root.level == logging.WARNING  # unknown name leaves it alone
        finally:
            root.setLevel(before)


class TestChannelManagerSetters:
    def test_the_channel_cap_is_never_set_below_one(self, tmp_path: Path) -> None:
        """The setters are plain assignments apart from this clamp, which is what
        keeps a hand-edited ``0`` from making every channel unopenable."""
        from kiro_crew.channel import ChannelManager

        mgr = ChannelManager(max_channels=2, max_agents=3, channels_dir=str(tmp_path))
        mgr.set_max_channels(0)
        assert mgr._max_channels == 1


# ── fingerprint: what makes the watcher decide to reload ──────────────────


class TestFingerprintChangeDetection:
    """The poll's whole decision rests on this tuple, so it is pinned directly.

    ``refresh_now`` forces past it, which is why the file-level tests above never
    exercise it: only the unforced path (``_cycle`` on a tick) consults it, and a
    fingerprint that failed to move is exactly the silent-inertness bug the
    watcher exists to fix.
    """

    def test_fingerprint_moves_when_the_file_is_edited(self, cfg_file: Path) -> None:
        before = ConfigWatch._current_fingerprint()
        _write(cfg_file, {"agent": {"model": "model-zz", "log_level": "INFO"}})
        assert ConfigWatch._current_fingerprint() != before

    def test_fingerprint_moves_on_a_same_length_edit(self, cfg_file: Path) -> None:
        """Size alone cannot carry the signal -- a same-size edit must still move it."""
        _write(cfg_file, {"agent": {"model": "aaaa"}})
        before = ConfigWatch._current_fingerprint()
        _write(cfg_file, {"agent": {"model": "bbbb"}})
        after = ConfigWatch._current_fingerprint()
        assert after != before

    def test_fingerprint_moves_when_the_file_is_deleted(self, cfg_file: Path) -> None:
        before = ConfigWatch._current_fingerprint()
        cfg_file.unlink()
        assert ConfigWatch._current_fingerprint() != before

    def test_fingerprint_is_stable_without_an_edit(self, cfg_file: Path) -> None:
        assert ConfigWatch._current_fingerprint() == ConfigWatch._current_fingerprint()

    @pytest.mark.asyncio
    async def test_an_equal_fingerprint_skips_the_load_entirely(self, cfg_file: Path) -> None:
        """The unforced tick must not even load when the fingerprint stands still."""
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load(), ConfigWatch._current_fingerprint())
        with patch.object(ConfigWatch, "_load_with_doc", side_effect=AssertionError("loaded")):
            assert await w._cycle() is None

    @pytest.mark.asyncio
    async def test_a_moved_fingerprint_reloads_without_a_kick(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load(), ConfigWatch._current_fingerprint())
        _write(cfg_file, {"agent": {"model": "model-tick", "log_level": "INFO"}})
        change = await w._cycle()  # no notify_written, no force
        assert change is not None and change.new.agent.model == "model-tick"


# ── dispatch: only touched prefixes, in registration order ────────────────


class TestDispatchOrderAndScope:
    @pytest.mark.asyncio
    async def test_dispatch_follows_registration_order(self, cfg_file: Path) -> None:
        """Order of dispatch is order of registration -- appliers are allowed to
        depend on it (a rebuild registered before the consumer that reads it)."""
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        order: list[str] = []
        for name in ("first", "second", "third"):
            w.subscribe(
                "agent",
                callback=lambda c, n=name: order.append(n),
                name=name,
            )
        _write(cfg_file, {"agent": {"model": "model-ordered", "log_level": "INFO"}})
        assert await w.refresh_now() is not None
        assert order == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_only_touched_prefixes_fire(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        fired: list[str] = []
        w.subscribe("agent.model", callback=lambda c: fired.append("model"), name="model")
        w.subscribe("agent.log_level", callback=lambda c: fired.append("level"), name="level")
        w.subscribe("session", callback=lambda c: fired.append("session"), name="session")
        w.subscribe(
            "agent.max_channels",
            "agent.log_level",
            callback=lambda c: fired.append("multi"),
            name="multi",
        )
        _write(cfg_file, {"agent": {"model": "model-a", "log_level": "DEBUG"}})
        assert await w.refresh_now() is not None
        # Only the two subscriptions naming agent.log_level, in their own order.
        assert fired == ["level", "multi"]

    @pytest.mark.asyncio
    async def test_a_sync_and_an_async_applier_both_run(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        ran: list[str] = []

        def _sync(c: ConfigChange) -> None:
            ran.append("sync")

        async def _async(c: ConfigChange) -> None:
            await asyncio.sleep(0)
            ran.append("async")

        w.subscribe("agent", callback=_sync, name="sync")
        w.subscribe("agent", callback=_async, name="async")
        _write(cfg_file, {"agent": {"model": "model-both", "log_level": "INFO"}})
        assert await w.refresh_now() is not None
        assert ran == ["sync", "async"]

    @pytest.mark.asyncio
    async def test_a_raising_applier_is_logged_by_name(self, cfg_file: Path, caplog) -> None:
        """Failure is loud: the applier's NAME reaches the log, the values do not."""
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        after: list[str] = []

        def _boom(c: ConfigChange) -> None:
            raise RuntimeError("applier exploded")

        async def _async_boom(c: ConfigChange) -> None:
            raise RuntimeError("async applier exploded")

        w.subscribe("agent", callback=_boom, name="sync-boom")
        w.subscribe("agent", callback=_async_boom, name="async-boom")
        w.subscribe("agent", callback=lambda c: after.append("ran"), name="survivor")
        _write(cfg_file, {"agent": {"model": "model-secret-value", "log_level": "INFO"}})
        with caplog.at_level("ERROR", logger="kiro_crew.config.live"):
            assert await w.refresh_now() is not None
        assert after == ["ran"]
        assert "sync-boom" in caplog.text
        assert "async-boom" in caplog.text
        assert "applier exploded" in caplog.text

    @pytest.mark.asyncio
    async def test_a_failed_applier_is_retried_on_the_next_quiet_tick(self, cfg_file: Path) -> None:
        """A one-shot applier failure must not leave its consumer stale until the
        same fields happen to change again: the watcher remembers what that
        applier missed and re-dispatches exactly those paths on the next tick,
        even though the file did not move."""
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        seen: list[frozenset[str]] = []
        fail = {"on": True}

        def _flaky(c: ConfigChange) -> None:
            if fail["on"]:
                raise RuntimeError("transient")
            seen.append(c.under("agent"))

        w.subscribe("agent.log_level", callback=_flaky, name="flaky")
        _write(cfg_file, {"agent": {"model": "model-a", "log_level": "DEBUG"}})
        assert await w.refresh_now() is not None
        assert seen == []
        # Nothing changed on disk; the retry alone brings the applier current.
        fail["on"] = False
        assert await w.refresh_now() is None
        assert seen == [frozenset({"agent.log_level"})]
        # Once adopted, a quiet tick dispatches nothing more.
        assert await w.refresh_now() is None
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_missed_paths_are_folded_in_for_that_applier_only(self, cfg_file: Path) -> None:
        """The paths one applier missed must not leak into the change a LATER
        applier sees, or an unrelated applier fires on fields that did not move."""
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        other_seen: list[frozenset[str]] = []
        fail = {"on": True}

        def _flaky(c: ConfigChange) -> None:
            if fail["on"]:
                raise RuntimeError("transient")

        w.subscribe("agent.log_level", callback=_flaky, name="flaky")
        w.subscribe("agent", callback=lambda c: other_seen.append(c.changed), name="other")
        _write(cfg_file, {"agent": {"model": "model-a", "log_level": "DEBUG"}})
        assert await w.refresh_now() is not None
        fail["on"] = False
        _write(cfg_file, {"agent": {"model": "model-b", "log_level": "DEBUG"}})
        assert await w.refresh_now() is not None
        assert other_seen[-1] == frozenset({"agent.model"}), "no leaked agent.log_level"

    @pytest.mark.asyncio
    async def test_a_later_change_folds_in_what_the_applier_missed(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        seen: list[frozenset[str]] = []
        fail = {"on": True}

        def _flaky(c: ConfigChange) -> None:
            if fail["on"]:
                raise RuntimeError("transient")
            seen.append(c.under("agent"))

        w.subscribe("agent", callback=_flaky, name="flaky")
        _write(cfg_file, {"agent": {"model": "model-a", "log_level": "DEBUG"}})
        assert await w.refresh_now() is not None
        fail["on"] = False
        _write(cfg_file, {"agent": {"model": "model-b", "log_level": "DEBUG"}})
        assert await w.refresh_now() is not None
        # One dispatch carries both the new edit and the path missed before it.
        assert seen == [frozenset({"agent.model", "agent.log_level"})]

    @pytest.mark.asyncio
    async def test_only_changed_paths_are_logged_never_values(self, cfg_file: Path, caplog) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        with caplog.at_level("INFO", logger="kiro_crew.config.live"):
            _write(cfg_file, {"agent": {"model": "sk-not-a-real-token", "log_level": "INFO"}})
            assert await w.refresh_now() is not None
        assert "agent.model" in caplog.text
        assert "sk-not-a-real-token" not in caplog.text


# ── lifecycle ─────────────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, cfg_file: Path) -> None:
        w = ConfigWatch(poll_interval_secs=30.0)
        await w.start(initial=KiroCrewConfig.load())
        try:
            first = w._task
            assert w.started
            await w.start(initial=KiroCrewConfig.load())
            assert w._task is first  # second call did not create a second task
        finally:
            await w.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_the_task_and_is_safe_twice(self, cfg_file: Path) -> None:
        w = ConfigWatch(poll_interval_secs=30.0)
        await w.start(initial=KiroCrewConfig.load())
        task = w._task
        assert task is not None
        await w.stop()
        assert task.cancelled() or task.done()
        assert not w.started
        await w.stop()  # no task left: must not raise

    @pytest.mark.asyncio
    async def test_start_without_initial_establishes_a_baseline(self, cfg_file: Path) -> None:
        w = ConfigWatch(poll_interval_secs=30.0)
        assert w.snapshot() is None
        await w.start()
        try:
            snap = w.snapshot()
            assert snap is not None and snap.agent.model == "model-a"
        finally:
            await w.stop()

    def test_interval_is_floored(self) -> None:
        assert ConfigWatch(poll_interval_secs=0.0).poll_interval_secs == 0.05
        assert ConfigWatch(poll_interval_secs=7.0).poll_interval_secs == 7.0

    @pytest.mark.asyncio
    async def test_notify_written_before_start_is_a_noop(self) -> None:
        w = ConfigWatch()
        w.notify_written()  # no loop, no wake event yet
        assert not w.started

    @pytest.mark.asyncio
    async def test_module_notify_kicks_the_process_watcher(self, cfg_file: Path) -> None:
        """``notify_config_written()`` is what every writer calls; it must reach the
        singleton's wake and force a reload even when the fingerprint reads equal."""
        w = live.watch()
        got: asyncio.Queue[str] = asyncio.Queue()
        w.subscribe("agent.model", callback=lambda c: got.put_nowait(c.new.agent.model), name="m")
        await w.start(initial=KiroCrewConfig.load())
        try:
            _write(cfg_file, {"agent": {"model": "model-kicked", "log_level": "INFO"}})
            live.notify_config_written()
            assert await asyncio.wait_for(got.get(), timeout=5.0) == "model-kicked"
        finally:
            await w.stop()

    @pytest.mark.asyncio
    async def test_a_forced_reload_survives_an_unmoved_fingerprint(self, cfg_file: Path) -> None:
        """A coarse filesystem clock can hide a write; the kick must not depend on it."""
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load(), ConfigWatch._current_fingerprint())
        cfg_file.write_text(
            json.dumps({"agent": {"model": "model-same-mtime", "log_level": "INFO"}}),
            encoding="utf-8",
        )
        with patch.object(
            ConfigWatch, "_current_fingerprint", staticmethod(lambda: w._fingerprint)
        ):
            change = await w.refresh_now()
        assert change is not None and change.new.agent.model == "model-same-mtime"

    def test_reset_for_tests_replaces_the_singleton(self) -> None:
        first = live.watch()
        assert live.watch() is first
        live.reset_for_tests()
        assert live.watch() is not first


# ── every write path kicks the watcher ────────────────────────────────────


class TestEveryWritePathNotifies:
    """One writer that forgets the kick is one setting that stays silently inert.

    ``update_config_locked`` and ``save()`` are covered above; these are the other
    three doors onto ``config.json`` -- a boot migration, the version re-stamp, and
    the agent module's own atomic writer (the per-channel savers and the STT PUT
    reach the file through that one, bypassing the loader's writers entirely).
    """

    def test_persist_config_migration_notifies(self, cfg_file: Path) -> None:
        from kiro_crew.config import loader as loader_mod

        calls: list[int] = []
        with (
            patch.object(loader_mod, "_apply_document_migrations", return_value=True),
            patch(
                "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
            ),
        ):
            wrote = loader_mod._persist_config_migration(
                cfg_file,
                frozenset({"agent.default_agent"}),
                default_kiro_agent="kirocrew",
            )
        assert wrote is True
        assert calls  # a boot migration is a config write like any other

    def test_persist_config_migration_does_not_notify_when_nothing_to_migrate(
        self, cfg_file: Path
    ) -> None:
        from kiro_crew.config import loader as loader_mod

        calls: list[int] = []
        with (
            patch.object(loader_mod, "_apply_document_migrations", return_value=False),
            patch(
                "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
            ),
        ):
            assert (
                loader_mod._persist_config_migration(
                    cfg_file, frozenset({"agent.default_agent"}), default_kiro_agent="kirocrew"
                )
                is False
            )
        assert calls == []

    def test_refresh_config_meta_stamp_notifies_when_it_rewrites(self, cfg_file: Path) -> None:
        from kiro_crew.config.loader import refresh_config_meta_stamp

        _write(cfg_file, {"agent": {"model": "model-a"}, "meta": {"lastTouchedVersion": "0.0.0"}})
        calls: list[int] = []
        with patch(
            "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
        ):
            wrote = refresh_config_meta_stamp()
        assert wrote is True
        # Kicked at least once. It is exactly twice today (update_config_locked
        # kicks, then the stamp kicks its own write); the count is not the
        # contract because the kick only sets a force flag -- a second one is a
        # no-op, and asserting a number would fail on a harmless refactor.
        assert calls

    def test_refresh_config_meta_stamp_does_not_notify_when_current(self, cfg_file: Path) -> None:
        from kiro_crew.config.loader import refresh_config_meta_stamp

        refresh_config_meta_stamp()  # stamp it once so the version now matches
        calls: list[int] = []
        with patch(
            "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
        ):
            assert refresh_config_meta_stamp() is False
        assert calls == []  # no rewrite, no mtime churn, no kick

    def test_agent_atomic_write_notifies_for_config_json(self, tmp_path: Path) -> None:
        from kiro_crew import agent as agent_mod

        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        calls: list[int] = []
        with (
            patch.object(agent_mod, "_mc_config_path", return_value=target),
            patch(
                "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
            ),
        ):
            agent_mod._atomic_json_write(target, {"agent": {"model": "m"}})
        assert calls == [1]

    def test_agent_atomic_write_does_not_notify_for_an_agent_spec(self, tmp_path: Path) -> None:
        from kiro_crew import agent as agent_mod

        spec = tmp_path / "kirocrew.json"
        calls: list[int] = []
        with (
            patch.object(agent_mod, "_mc_config_path", return_value=tmp_path / "config.json"),
            patch(
                "kiro_crew.config.live.notify_config_written", side_effect=lambda: calls.append(1)
            ),
        ):
            agent_mod._atomic_json_write(spec, {"name": "kirocrew"})
        assert calls == []


# ── the dashboard server's own appliers ───────────────────────────────────


class TestServerAppliers:
    """``_register_config_watch`` owns the appliers whose holder is the dashboard.

    They are driven the way a dispatcher's subscriber actually is -- a hand-built
    :class:`ConfigChange` handed straight to the registered callback -- so the test
    covers the applier's own decision (does it fire, what does it push) without
    booting an aiohttp server.
    """

    def _register(self, state) -> dict[str, Any]:
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_config_watch

        app = web.Application()
        _register_config_watch(app, state, None)
        return {s.name: s for s in app["config_watch_subscriptions"]}

    def _cfg(self, **agent_kw) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        for key, value in agent_kw.items():
            setattr(cfg.agent, key, value)
        return cfg

    def test_the_dashboard_registers_no_applier_for_owner_bound_leaves(self) -> None:
        """The workflow ceiling and the channel caps are bound by their OWNERS'
        constructors (``live.bind``), so the dashboard registers nothing for them."""
        subs = self._register(SimpleNamespace(workflow_service=None, channel_manager=None))
        assert not any("workflow_run_timeout" in n or "channel" in n for n in subs)

    @pytest.mark.asyncio
    async def test_the_channel_manager_binds_its_caps_in_its_constructor(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.channel import ChannelManager

        live.reset_for_tests()
        mgr = ChannelManager(
            broadcast_fn=lambda *_a, **_k: None,
            max_channels=2,
            max_agents=3,
            channels_dir=str(tmp_path),
        )
        names = {s.name for s in live.watch().subscriptions()}
        assert {"bind[agent.max_channels]", "bind[agent.max_channel_agents]"} <= names
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(max_channels=7, max_channel_agents=9),
                changed=frozenset({"agent.max_channels"}),
            )
        )
        assert (mgr._max_channels, mgr._max_agents) == (7, 3), "only the touched leaf moves"
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(max_channels=7, max_channel_agents=9),
                changed=frozenset({"agent.max_channel_agents"}),
            )
        )
        assert (mgr._max_channels, mgr._max_agents) == (7, 9)

    @pytest.mark.asyncio
    async def test_the_workflow_service_binds_its_ceiling_in_its_constructor(self) -> None:
        from kiro_crew.workflows.service import WorkflowService

        live.reset_for_tests()
        svc = WorkflowService(sessions=SimpleNamespace(), timeout_secs=600)
        assert "bind[agent.workflow_run_timeout_secs]" in {
            s.name for s in live.watch().subscriptions()
        }
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(workflow_run_timeout_secs=1800),
                changed=frozenset({"agent.workflow_run_timeout_secs"}),
            )
        )
        assert svc.timeout_secs == 1800
        await live.watch()._dispatch(
            ConfigChange(
                old=None,
                new=self._cfg(workflow_run_timeout_secs=900),
                changed=frozenset({"agent.max_channels"}),
            )
        )
        assert svc.timeout_secs == 1800, "an untouched leaf leaves the ceiling alone"

    def test_log_level_applier_is_registered_on_its_own_path(self) -> None:
        from kiro_crew.dashboard.handlers.updates import apply_log_level_from_config

        subs = self._register(SimpleNamespace(workflow_service=None, channel_manager=None))
        sub = subs["agent.log_level"]
        assert sub.prefixes == ("agent.log_level",)
        assert sub.callback() is apply_log_level_from_config

    @pytest.mark.asyncio
    async def test_the_provider_switch_runs_off_the_watcher_cycle_exactly_once(self) -> None:
        """``reload_provider_factory`` clears the session registry and then shuts
        the retired providers down one at a time, which can outlast the applier
        bound. Awaited inside the applier, a timeout would cancel it mid-shutdown
        and the retry would clear the sessions created with the new provider in
        between. So the applier schedules the switch as a tracked task and
        returns at once; the switch itself runs once, with the changed config."""
        release = asyncio.Event()
        entered = asyncio.Event()
        calls: list[Any] = []

        async def reload_provider_factory(*, cfg: Any) -> None:
            calls.append(cfg)
            entered.set()
            await release.wait()

        slot = SimpleNamespace(model="claude-x", _model_pick_gen=0)
        pushed: list[bool] = []
        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            sessions=SimpleNamespace(reload_provider_factory=reload_provider_factory),
            _slots={"s1": slot},
            push_slots_update=lambda: pushed.append(True),
            _background_tasks=set(),
        )
        apply = self._register(state)["agent.provider"].callback()
        cfg = KiroCrewConfig()
        with patch("kiro_crew.agent.rebuild_agent_config", lambda: None):
            await asyncio.wait_for(
                apply(
                    ConfigChange(
                        old=KiroCrewConfig(), new=cfg, changed=frozenset({"agent.provider"})
                    )
                ),
                timeout=1.0,
            )
            assert len(state._background_tasks) == 1, "the switch is a tracked task"
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            assert calls == [cfg], "the switch runs once, with the changed config"
            assert slot.model == "claude-x", "slot models clear only after the reload"
            release.set()
            await asyncio.gather(*state._background_tasks)
        assert calls == [cfg]
        assert slot.model == "" and slot._model_pick_gen == 1 and pushed == [True]
        assert state._background_tasks == set(), "the task drops out of the registry when done"
        # An unrelated leaf schedules nothing.
        await apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agent.log_level"})))
        assert state._background_tasks == set()

    @pytest.mark.asyncio
    async def test_the_provider_switch_installs_the_snapshot_current_at_install_time(
        self,
    ) -> None:
        """The task runs off the cycle, so by the time it installs, a later change
        may already be in force; installing the document it was scheduled with
        would revert that. It installs the watcher's current snapshot instead --
        unless that snapshot is torn, which holds defaults."""
        from dataclasses import replace

        calls: list[Any] = []

        async def reload_provider_factory(*, cfg: Any) -> None:
            calls.append(cfg)

        state = SimpleNamespace(
            workflow_service=None,
            channel_manager=None,
            sessions=SimpleNamespace(reload_provider_factory=reload_provider_factory),
            _slots={},
            push_slots_update=lambda: None,
            _background_tasks=set(),
        )
        apply = self._register(state)["agent.provider"].callback()
        scheduled = KiroCrewConfig()
        newer = replace(KiroCrewConfig(), agent=replace(KiroCrewConfig().agent, model="newer"))
        torn = replace(KiroCrewConfig(), _degraded_sections=frozenset({"agent"}))
        change = ConfigChange(
            old=KiroCrewConfig(), new=scheduled, changed=frozenset({"agent.provider"})
        )
        with patch("kiro_crew.agent.rebuild_agent_config", lambda: None):
            with patch("kiro_crew.config.live.snapshot", return_value=newer):
                await apply(change)
                await asyncio.gather(*state._background_tasks)
            with patch("kiro_crew.config.live.snapshot", return_value=torn):
                await apply(change)
                await asyncio.gather(*state._background_tasks)
        assert calls == [newer, scheduled]

    def test_every_applier_is_registered_and_scoped(self) -> None:
        subs = self._register(SimpleNamespace(workflow_service=None, channel_manager=None))
        assert set(subs) == {
            "agent.provider",
            "agent.role_models.background",
            "agent.log_level",
        }
        # None of them is a catch-all: an unrelated write must dispatch nothing.
        for sub in subs.values():
            assert sub.prefixes, f"{sub.name} would fire on every reload"

    def test_the_watcher_is_not_an_on_startup_hook_but_is_stopped_on_cleanup(self) -> None:
        """``no-new-work-on-gateway-boot-path``: ``on_startup`` runs before the socket
        binds, so the watcher is started post-bind by ``_kick_config_watch``; only the
        cleanup hook is registered at build time."""
        from aiohttp import web

        from kiro_crew.dashboard.server import _register_config_watch

        app = web.Application()
        _register_config_watch(app, SimpleNamespace(workflow_service=None), None)
        assert not any("config_watch" in cb.__name__ for cb in app.on_startup)
        assert any(cb.__name__ == "_config_watch_shutdown" for cb in app.on_cleanup)

    @pytest.mark.asyncio
    async def test_the_post_bind_kick_starts_the_watcher_as_a_tracked_task(
        self, cfg_file: Path
    ) -> None:
        from aiohttp import web

        from kiro_crew.dashboard.server import _kick_config_watch, _register_config_watch

        app = web.Application()
        initial = KiroCrewConfig.load()
        _register_config_watch(app, SimpleNamespace(workflow_service=None), initial)
        state = SimpleNamespace(_background_tasks=set())
        _kick_config_watch(app, state)
        assert len(state._background_tasks) == 1
        await asyncio.gather(*state._background_tasks)
        try:
            assert live.watch().started
            assert live.snapshot() is not None
        finally:
            await live.watch().stop()

    @pytest.mark.asyncio
    async def test_the_kick_primes_the_snapshot_before_yielding(self, cfg_file: Path) -> None:
        """A transport started right after the kick, before the background task has
        run, must already see the boot config: the per-turn readers fall back to a
        disk load on a ``None`` snapshot, and that fallback must be unreachable in a
        running gateway."""
        from aiohttp import web

        from kiro_crew.dashboard.server import _kick_config_watch, _register_config_watch

        app = web.Application()
        initial = KiroCrewConfig.load()
        _register_config_watch(app, SimpleNamespace(workflow_service=None), initial)
        state = SimpleNamespace(_background_tasks=set())
        _kick_config_watch(app, state)
        try:
            # No await between the kick and this read: the task has not run yet.
            assert not live.watch().started
            assert live.snapshot() is initial
            await asyncio.gather(*state._background_tasks)
            # The deferred start re-primes the same config; the first cycle's
            # reload-and-diff is unchanged.
            assert live.watch().started
            assert live.snapshot() is initial
        finally:
            await live.watch().stop()

    @pytest.mark.asyncio
    async def test_a_file_write_reaches_the_workflow_ceiling(self, cfg_file: Path) -> None:
        """End to end: config.json -> ConfigWatch -> the WorkflowService's own bind."""
        from kiro_crew.workflows.service import WorkflowService

        svc = WorkflowService(sessions=SimpleNamespace(), timeout_secs=600)
        w = live.watch()  # the constructor bound on the singleton
        w.prime(KiroCrewConfig.load())
        assert "bind[agent.workflow_run_timeout_secs]" in {s.name for s in w.subscriptions()}
        _write(cfg_file, {"agent": {"model": "model-a", "workflow_run_timeout_secs": 2400}})
        change = await w.refresh_now()
        assert change is not None and change.touched("agent.workflow_run_timeout_secs")
        assert svc.timeout_secs == 2400


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


# ── subscription lifecycle ────────────────────────────────────────────────


class TestSubscriptionLifecycle:
    """``cancel()`` is the removal verb (there is no ``close()``); a cancelled
    subscription must be inert AND safe to cancel again."""

    @pytest.mark.asyncio
    async def test_a_cancelled_subscription_stops_firing(self, cfg_file: Path) -> None:
        w = ConfigWatch()
        w.prime(KiroCrewConfig.load())
        fired: list[str] = []
        sub = w.subscribe("agent", callback=lambda c: fired.append("ran"), name="one")
        _write(cfg_file, {"agent": {"model": "model-1", "log_level": "INFO"}})
        assert await w.refresh_now() is not None
        assert fired == ["ran"]

        sub.cancel()
        _write(cfg_file, {"agent": {"model": "model-2", "log_level": "INFO"}})
        assert await w.refresh_now() is not None
        assert fired == ["ran"]  # no second call

    def test_cancel_is_idempotent(self) -> None:
        w = ConfigWatch()
        sub = w.subscribe(callback=lambda c: None, name="x")
        sub.cancel()
        sub.cancel()  # detached from the watch: must not raise
        assert list(w.subscriptions()) == []

    def test_cancelling_one_leaves_its_siblings(self) -> None:
        w = ConfigWatch()
        a = w.subscribe("agent", callback=lambda c: None, name="a")
        w.subscribe("agent", callback=lambda c: None, name="b")
        a.cancel()
        assert [s.name for s in w.subscriptions()] == ["b"]

    def test_a_prefixless_subscription_fires_on_everything(self) -> None:
        w = ConfigWatch()
        sub = w.subscribe(callback=lambda c: None, name="all")
        assert sub.prefixes == ()
        # An empty prefix tuple is the documented catch-all: _dispatch skips the
        # touched() filter entirely for it.
        change = ConfigChange(old=None, new=KiroCrewConfig(), changed=frozenset({"anything.at"}))
        assert not change.touched("agent")
        assert not sub.prefixes  # so the dispatcher does not consult touched()

    def test_a_free_function_is_held_strongly(self) -> None:
        """Only BOUND METHODS are weak -- a module function or closure has no
        owner to outlive, and dropping it would silently unregister every
        lambda-based applier the moment the caller stopped holding a name."""
        w = ConfigWatch()

        def _applier(change: ConfigChange) -> None:  # pragma: no cover
            pass

        sub = w.subscribe("agent", callback=_applier, name="free")
        del _applier
        gc.collect()
        assert sub.callback() is not None


# ── the restart hint the dashboard answers with ───────────────────────────


class TestRestartHintIsSchemaDriven:
    """The PUT/PATCH handlers keep no list of boot-only keys; the schema is it.

    ``_changed_paths_need_restart`` is the whole computation behind the PUT's
    ``restart_required`` field, and the PUT's own vocabulary is checked against it
    so a newly added setting cannot quietly start claiming a restart it does not
    need (or hide one it does).
    """

    #: Every key ``PUT /api/config`` can apply, as ``agent.<key>``.
    PUT_KEYS = (
        "subagent_max_turns",
        "subagent_auto_max",
        "max_subagents",
        "conductor_skill",
    )

    def test_every_put_settable_field_is_hot(self) -> None:
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        for key in self.PUT_KEYS:
            assert not _changed_paths_need_restart([f"agent.{key}"]), key
        assert not _changed_paths_need_restart([f"agent.{k}" for k in self.PUT_KEYS])

    def test_a_restart_true_path_flips_the_hint(self) -> None:
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        for path in (
            "agent.jail",
            "agent.dangerously_skip_permissions",
            "dashboard.url",
            "dashboard.restore_sessions",
            "tunnel.enabled",
        ):
            assert _changed_paths_need_restart([path]), path
        # One restart-only path among live ones is enough to require a restart.
        assert _changed_paths_need_restart(["agent.max_subagents", "tunnel.enabled"])

    def test_an_unchanged_field_never_claims_a_restart(self) -> None:
        """The dashboard sends every setting on each save, so the handler feeds
        only the paths whose value actually MOVED -- an untouched restart=True
        field must not make a live edit report restart_required."""
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        applied = ["max_subagents", "jail"]
        agent = {"max_subagents": 8, "jail": True}
        before = {"max_subagents": 4, "jail": True}  # jail resent unchanged
        changed = [f"agent.{k}" for k in applied if agent.get(k) != before.get(k)]
        assert changed == ["agent.max_subagents"]
        assert not _changed_paths_need_restart(changed)

    def test_the_hint_and_the_schema_cannot_drift(self) -> None:
        """Whatever the schema marks, the handler agrees with -- no second list."""
        from kiro_crew.config.schema import SCHEMA_REGISTRY, requires_restart
        from kiro_crew.dashboard.handlers.core import _changed_paths_need_restart

        marked = {e.path for e in SCHEMA_REGISTRY if e.requires_restart}
        assert marked, "no restart=True fields in the schema at all"
        for path in marked:
            assert requires_restart(path)
            assert _changed_paths_need_restart([path]), path

        def _has_marked_ancestor(path: str) -> bool:
            parts = path.split(".")
            return any(".".join(parts[:i]) in marked for i in range(1, len(parts)))

        for entry in SCHEMA_REGISTRY:
            if entry.requires_restart or _has_marked_ancestor(entry.path):
                continue
            assert not _changed_paths_need_restart([entry.path]), entry.path

    def test_a_child_of_a_marked_field_inherits_the_mark(self) -> None:
        """``requires_restart`` resolves through ancestors, so a marked container
        covers keys the schema never enumerates (a stub-server name, a jail
        sub-key) -- otherwise a nested edit would report itself hot."""
        from kiro_crew.config.schema import requires_restart

        assert requires_restart("mcp_gateway.stub_servers")
        assert requires_restart("mcp_gateway.stub_servers.some-server")
        assert requires_restart("agent.jail.anything")
        assert not requires_restart("mcp_gateway")  # the parent itself is hot


# ── the hint over a real request ──────────────────────────────────────────


class TestRestartRequiredOverHttp:
    """The same schema hint, but answered by the endpoint the dashboard calls.

    ``_changed_paths_need_restart`` is unit-tested above; this drives the two
    doors in front of it. That matters because the value the USER sees is the
    JSON field: the four keys ``PUT`` can apply are hot, the whole point of the
    watcher is that they never report a restart, and only a real request proves
    the response says so.

    ``PATCH`` is the other door and deliberately carries no ``restart_required``
    at all: every path in its allow-list is hot, so a hint there would be a
    field that is always ``False``.
    """

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_put("/api/config/kirocrew", handlers.api_kirocrew_config)
        app.router.add_patch("/api/config/kirocrew", handlers.api_kirocrew_config_patch)
        return app

    @pytest.fixture
    def client_ctx(self, cfg_file: Path):
        from unittest.mock import MagicMock

        from aiohttp.test_utils import TestClient, TestServer

        with patch("kiro_crew.dashboard.handlers.sel", return_value=MagicMock()):
            yield lambda: TestClient(TestServer(self._app()))

    @pytest.mark.asyncio
    async def test_put_a_hot_field_reports_no_restart(self, client_ctx) -> None:
        """The regression pin: a live field must not ask the user to restart."""
        async with client_ctx() as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
            assert resp.status == 200
            body = await resp.json()
        assert body["restart_required"] is False

    @pytest.mark.asyncio
    async def test_put_several_hot_fields_at_once_reports_no_restart(
        self, client_ctx, cfg_file: Path
    ) -> None:
        async with client_ctx() as c:
            resp = await c.put(
                "/api/config/kirocrew",
                json={"agent": {"max_subagents": 8, "subagent_max_turns": 50}},
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["restart_required"] is False
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]
        assert (saved["max_subagents"], saved["subagent_max_turns"]) == (8, 50)

    @pytest.mark.asyncio
    async def test_put_a_field_resent_unchanged_reports_no_restart(self, client_ctx) -> None:
        """The dashboard resends every setting on each save and enables Save when
        ANY one is dirty, so "was applied" is wider than "was changed" -- the hint
        must follow the narrower one. Marking the path ``restart=True`` is what
        makes the difference observable: the first PUT moves the value and reports
        a restart, the identical second one moves nothing and must not."""
        with patch(
            "kiro_crew.config.schema.requires_restart",
            side_effect=lambda p: p == "agent.max_subagents",
        ):
            async with client_ctx() as c:
                first = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
                assert first.status == 200
                assert (await first.json())["restart_required"] is True
                again = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
                assert again.status == 200
                body = await again.json()
        assert body["restart_required"] is False

    @pytest.mark.asyncio
    async def test_put_reports_a_restart_when_the_schema_marks_the_moved_path(
        self, client_ctx
    ) -> None:
        """The False above is the SCHEMA's answer, not a constant: mark the very
        same path ``restart=True`` and the same request flips to True."""
        with patch(
            "kiro_crew.config.schema.requires_restart",
            side_effect=lambda p: p == "agent.max_subagents",
        ):
            async with client_ctx() as c:
                resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 8}})
                assert resp.status == 200
                body = await resp.json()
        assert body["restart_required"] is True

    @pytest.mark.asyncio
    async def test_patch_a_hot_field_carries_no_restart_hint(self, client_ctx) -> None:
        async with client_ctx() as c:
            resp = await c.patch(
                "/api/config/kirocrew", json={"path": "agent.model", "value": "model-patched"}
            )
            assert resp.status == 200
            body = await resp.json()
        assert "restart_required" not in body
        assert body["agent"]["model"] == "model-patched"

    @pytest.mark.asyncio
    async def test_patch_hot_applies_through_a_started_watcher(self, client_ctx) -> None:
        """PATCH ends in ``_hot_apply_after_write``, so the new value is in force
        before the response -- an applier has already run by the time it returns."""
        w = live.watch()
        await w.start(initial=KiroCrewConfig.load())
        seen: list[str] = []
        w.subscribe("agent.model", callback=lambda ch: seen.append(ch.new.agent.model), name="m")
        try:
            async with client_ctx() as c:
                resp = await c.patch(
                    "/api/config/kirocrew", json={"path": "agent.model", "value": "model-hotpatch"}
                )
                assert resp.status == 200
            assert seen == ["model-hotpatch"]
        finally:
            await w.stop()


class TestOwnedAppliers:
    """The one-line registration shapes: ``watch_section``, ``watch_object``, ``bind``.

    Each holds its owner weakly, fires only under its prefixes and, for a
    section, fails closed on a degraded document -- so a new subsystem inherits
    the authorization guard instead of re-implementing it.
    """

    @staticmethod
    def _change(new: KiroCrewConfig, *paths: str) -> ConfigChange:
        return ConfigChange(old=None, new=new, changed=frozenset(paths))

    @staticmethod
    def _wecom_cfg(**kw: Any) -> KiroCrewConfig:
        from dataclasses import replace

        cfg = KiroCrewConfig()
        return replace(cfg, wecom=replace(cfg.wecom, **kw))

    class _Transport:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        def reconfigure(self, section: Any) -> None:
            self.seen.append(section)

    class _Dispatcher:
        def __init__(self, w: ConfigWatch, transport: Any) -> None:
            self.transport = transport
            self._sub = w.watch_section(self, "wecom", "messaging", target="transport")

    @pytest.mark.asyncio
    async def test_watch_section_hands_the_section_to_the_target_only_when_it_moved(
        self,
    ) -> None:
        w = ConfigWatch()
        t = self._Transport()
        d = self._Dispatcher(w, t)
        cfg = self._wecom_cfg(allow_all_users=True)
        await w._dispatch(self._change(cfg, "agent.model"))
        assert t.seen == [], "an untouched section is not re-applied"
        await w._dispatch(self._change(cfg, "wecom.allow_all_users"))
        assert t.seen == [cfg.wecom]
        assert d._sub.prefixes == ("wecom", "messaging")

    @pytest.mark.asyncio
    async def test_watch_section_no_ops_while_the_target_is_not_up(self) -> None:
        w = ConfigWatch()
        d = self._Dispatcher(w, None)
        await w._dispatch(self._change(self._wecom_cfg(), "wecom.allow_all_users"))
        t = self._Transport()
        d.transport = t
        await w._dispatch(self._change(self._wecom_cfg(), "wecom.allow_all_users"))
        assert len(t.seen) == 1, "resolved at dispatch time, so a late transport is served"

    @pytest.mark.asyncio
    async def test_watch_section_fails_closed_on_a_degraded_section(self, caplog: Any) -> None:
        from dataclasses import replace

        w = ConfigWatch()
        t = self._Transport()
        d = self._Dispatcher(w, t)  # held: the owner is weakly referenced
        cfg = replace(self._wecom_cfg(), _degraded_sections=frozenset({"wecom"}))
        with caplog.at_level("WARNING", logger="kiro_crew.config.live"):
            await w._dispatch(self._change(cfg, "wecom.allow_all_users"))
        assert "degraded" in caplog.text
        assert t.seen == [], "a discarded section never rebuilds authorization state"
        # A sibling section's degradation does not gate this one, nor does the
        # sticky whole-config flag of a repaired tear (see the object test above).
        for degraded in ({"slack"}, {"*", "*config.json"}):
            cfg = replace(self._wecom_cfg(), _degraded_sections=frozenset(degraded))
            await w._dispatch(self._change(cfg, "wecom.allow_all_users"))
        assert len(t.seen) == 2
        del d

    @pytest.mark.asyncio
    async def test_owned_appliers_drop_out_with_their_owner(self) -> None:
        w = ConfigWatch()
        t = self._Transport()
        d = self._Dispatcher(w, t)
        assert len(list(w.subscriptions())) == 1
        del d
        gc.collect()
        assert list(w.subscriptions()) == [], "a collected owner leaves no registry entry"
        await w._dispatch(self._change(self._wecom_cfg(), "wecom.allow_all_users"))
        assert t.seen == []

    @pytest.mark.asyncio
    async def test_watch_object_hands_the_whole_config_under_any_prefix(self) -> None:
        w = ConfigWatch()

        class Store:
            def __init__(self) -> None:
                self.cfgs: list[Any] = []
                self._sub = w.watch_object(self, "memory", "skills.max_skills")

            def reconfigure(self, cfg: Any) -> None:
                self.cfgs.append(cfg)

        s = Store()
        cfg = KiroCrewConfig()
        await w._dispatch(self._change(cfg, "agent.model"))
        await w._dispatch(self._change(cfg, "skills.max_skills"))
        await w._dispatch(self._change(cfg, "memory.decay_days"))
        assert s.cfgs == [cfg, cfg]
        assert s._sub.name == "Store.reconfigure"

    @pytest.mark.asyncio
    async def test_watch_object_fails_closed_on_a_degraded_section_it_reads(
        self, caplog: Any
    ) -> None:
        """``reconfigure(cfg)`` reads whole sections; a degraded one holds DEFAULTS,
        and handing those over would reset an approval mode or a limit. The
        owner keeps what it last adopted."""
        from dataclasses import replace

        w = ConfigWatch()

        class Manager:
            def __init__(self) -> None:
                self.cfgs: list[Any] = []
                self._sub = w.watch_object(self, "agent.subagent_max_turns", "subagent")

            def reconfigure(self, cfg: Any) -> None:
                self.cfgs.append(cfg)

        m = Manager()
        for degraded in ({"agent"}, {"subagent"}):
            cfg = replace(KiroCrewConfig(), _degraded_sections=frozenset(degraded))
            with caplog.at_level("WARNING", logger="kiro_crew.config.live"):
                await w._dispatch(self._change(cfg, "agent.subagent_max_turns"))
            assert "degraded" in caplog.text
        assert m.cfgs == []
        # A section it does not read from does not gate it, and neither does the
        # whole-config flag on its own: the watcher never dispatches a document
        # that is torn NOW, so on a dispatched change that flag is the loader's
        # process-long memory of a tear that has since been repaired.
        for degraded in ({"slack"}, {"*", "*config.json"}):
            cfg = replace(KiroCrewConfig(), _degraded_sections=frozenset(degraded))
            await w._dispatch(self._change(cfg, "agent.subagent_max_turns"))
            assert m.cfgs[-1] is cfg
        assert len(m.cfgs) == 2

    @pytest.mark.asyncio
    async def test_bind_maps_one_leaf_onto_a_setter(self) -> None:
        from dataclasses import replace

        w = ConfigWatch()
        got: list[int] = []
        sub = w.bind("agent.max_channel_agents", got.append)
        cfg = replace(KiroCrewConfig(), agent=replace(KiroCrewConfig().agent, max_channel_agents=7))
        await w._dispatch(self._change(cfg, "agent.max_channels"))
        assert got == [], "a sibling leaf does not fire the binding"
        await w._dispatch(self._change(cfg, "agent.max_channel_agents"))
        assert got == [7]
        assert sub.prefixes == ("agent.max_channel_agents",)

    @pytest.mark.asyncio
    async def test_bind_to_a_bound_method_dies_with_its_object(self) -> None:
        w = ConfigWatch()

        class Mgr:
            def __init__(self) -> None:
                self.cap = 0

            def set_cap(self, v: int) -> None:
                self.cap = v

        m = Mgr()
        w.bind("agent.max_channels", m.set_cap)
        await w._dispatch(self._change(KiroCrewConfig(), "agent.max_channels"))
        assert m.cap == KiroCrewConfig().agent.max_channels
        del m
        gc.collect()
        assert list(w.subscriptions()) == []

    @pytest.mark.asyncio
    async def test_bind_fails_closed_on_a_degraded_section(self, caplog: Any) -> None:
        """A torn ``agent`` section holds DEFAULTS for every leaf, and a bound
        setter handed the default would reset a cap or a ceiling until the file
        is repaired. The same deferral the object and section appliers use."""
        from dataclasses import replace

        w = ConfigWatch()
        got: list[int] = []
        w.bind("agent.max_channels", got.append)
        cfg = replace(KiroCrewConfig(), _degraded_sections=frozenset({"agent"}))
        with caplog.at_level("WARNING", logger="kiro_crew.config.live"):
            await w._dispatch(self._change(cfg, "agent.max_channels"))
        assert "degraded" in caplog.text
        assert got == [], "a degraded section never reaches the setter"
        assert w._stale, "the deferred leaf is retried, not forgotten"
        # A different section's degradation does not gate this leaf, nor does the
        # sticky whole-config flag of a repaired tear (see the object test above).
        for degraded in ({"slack"}, {"*", "*config.json"}):
            cfg = replace(KiroCrewConfig(), _degraded_sections=frozenset(degraded))
            await w._dispatch(self._change(cfg, "agent.max_channels"))
        assert got == [KiroCrewConfig().agent.max_channels] * 2
