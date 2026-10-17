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
from typing import Any
from unittest.mock import patch

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

from kiro_crew.config import live
from kiro_crew.config.live import (
    ConfigChange,
    ConfigWatch,
    diff_config_docs,
    flatten_config,
)
from kiro_crew.config.loader import KiroCrewConfig, update_config_locked
from kiro_crew.testing.wait import async_wait_until, await_until

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
            await async_wait_until(lambda: got, describe=lambda: "got not yet populated")
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
            await async_wait_until(lambda: got, describe=lambda: "got not yet populated")
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
            await async_wait_until(lambda: got, describe=lambda: "got not yet populated")
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
            assert await await_until(asyncio.ensure_future(got.get())) == "model-kicked"
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
