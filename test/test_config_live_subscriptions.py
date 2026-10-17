"""Subscriptions on the live config watcher: cancelling one, the one-line registration shapes
``watch_section`` / ``watch_object`` / ``bind``, and the replay that reaches an owner which
registered after a reload had already been dispatched.
"""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import pytest
from _hot_reload_helpers import write_config as _write
from config_live_helpers import (  # noqa: F401 -- fixtures are used by name
    cfg_file_fixture,
)

from kiro_crew.config.live import ConfigChange, ConfigWatch
from kiro_crew.config.loader import KiroCrewConfig

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


# ── replay on registration ────────────────────────────────────────────────


class _ModelOwner:
    """An owner that, like a store built from a loaded config, applies its own copy."""

    def __init__(self, cfg: KiroCrewConfig) -> None:
        self.models: list[str] = []
        self.reconfigure(cfg)

    def reconfigure(self, cfg: KiroCrewConfig) -> None:
        self.models.append(cfg.agent.model)


class TestReplayOnRegistration:
    """A reload dispatched before a subscriber registered must still reach it.

    A reload adopts its config, then snapshots the registry. An owner that loaded
    its config, and registered only after such a reload snapshotted the registry,
    is never dispatched to, so without ``replay`` it keeps the older copy.
    """

    @staticmethod
    def _write_settled(path: Path, doc: dict) -> None:
        """Write *doc* and let the loader's migration write-back land now, so the
        file's fingerprint afterwards is the one the watcher records."""
        _write(path, doc)
        KiroCrewConfig.load()

    @pytest.mark.asyncio
    async def test_a_reload_that_missed_the_registration_is_replayed(self, cfg_file: Path) -> None:
        w = ConfigWatch(poll_interval_secs=0.05)
        self._write_settled(cfg_file, {"agent": {"model": "model-a", "log_level": "INFO"}})
        w.prime(KiroCrewConfig.load(), ConfigWatch._current_fingerprint())
        owned = KiroCrewConfig.load()  # the owner's own load, before the reload
        self._write_settled(cfg_file, {"agent": {"model": "model-bbbb", "log_level": "INFO"}})
        assert await w.refresh_now() is not None  # dispatched to nobody

        control = _ModelOwner(owned)
        w.watch_object(control, "agent", name="control")
        owner = _ModelOwner(owned)
        w.replay(w.watch_object(owner, "agent", name="owner"))

        assert control.models == ["model-a"], "without replay the reload is lost"
        assert owner.models == ["model-a", "model-bbbb"]

    @pytest.mark.asyncio
    async def test_a_snapshot_behind_the_file_is_not_replayed_but_queued(
        self, cfg_file: Path
    ) -> None:
        """When the file moved past the snapshot, the owner's own load may be the
        newer one, so the snapshot is not applied; the next tick delivers the file,
        even when that tick leaves the owner's prefix unchanged."""
        w = ConfigWatch(poll_interval_secs=0.05)
        self._write_settled(cfg_file, {"agent": {"model": "model-a", "log_level": "INFO"}})
        w.prime(KiroCrewConfig.load(), ConfigWatch._current_fingerprint())
        self._write_settled(cfg_file, {"agent": {"model": "model-bbbb", "log_level": "INFO"}})
        assert await w.refresh_now() is not None  # adopts model-bbbb
        stale = KiroCrewConfig()  # an owner holding a copy older than the snapshot
        stale.agent.model = "model-a"
        # The file moves again, on a field outside the owner's prefix.
        self._write_settled(cfg_file, {"agent": {"model": "model-bbbb", "log_level": "DEBUG"}})

        owner = _ModelOwner(stale)
        w.replay(w.watch_object(owner, "agent.model", name="owner"))
        assert owner.models == ["model-a"], "a snapshot behind the file is not applied"

        change = await w.refresh_now()
        assert change is not None and change.changed == {"agent.log_level"}
        assert owner.models == ["model-a", "model-bbbb"]

    def test_a_reload_landing_mid_replay_is_not_undone(self, cfg_file: Path) -> None:
        w = ConfigWatch(poll_interval_secs=0.05)
        self._write_settled(cfg_file, {"agent": {"model": "model-a", "log_level": "INFO"}})
        first = KiroCrewConfig.load()
        w.prime(first, ConfigWatch._current_fingerprint())
        second = KiroCrewConfig()
        second.agent.model = "model-cccc"

        class Racing(_ModelOwner):
            def reconfigure(self, cfg: KiroCrewConfig) -> None:
                super().reconfigure(cfg)
                if cfg is first:
                    # A reload adopted while the replay was applying the older one.
                    w._cfg = second

        owner = Racing(KiroCrewConfig())
        w.replay(w.watch_object(owner, "agent", name="owner"))
        assert owner.models[-1] == "model-cccc"

    def test_a_degraded_section_is_deferred_on_replay_not_logged_as_a_failure(
        self, cfg_file: Path, caplog
    ) -> None:
        """``watch_object`` fails closed: its applier raises ``ConfigDeferred`` while
        the owner's section is degraded. Replay must queue that as a deferral (the
        owner keeps its values, the paths go stale) and must not report it as an
        applier failure, which is what a generic ``except Exception`` would log."""
        from dataclasses import replace

        w = ConfigWatch(poll_interval_secs=0.05)
        self._write_settled(cfg_file, {"agent": {"model": "model-a", "log_level": "INFO"}})
        degraded = replace(KiroCrewConfig.load(), _degraded_sections=frozenset({"agent"}))
        w.prime(degraded, ConfigWatch._current_fingerprint())

        owner = _ModelOwner(KiroCrewConfig())
        caplog.set_level("ERROR", logger="kiro_crew.config.live")
        sub = w.watch_object(owner, "agent", name="owner")
        w.replay(sub)

        assert len(owner.models) == 1, "degraded defaults never reach reconfigure"
        assert w._stale.get(id(sub)), "the deferred paths are retried, not forgotten"
        assert "failed on replay" not in caplog.text

    def test_replay_is_a_no_op_before_anything_is_adopted(self) -> None:
        w = ConfigWatch(poll_interval_secs=0.05)
        owner = _ModelOwner(KiroCrewConfig())
        w.replay(w.watch_object(owner, "agent", name="owner"))
        assert len(owner.models) == 1
        assert w._stale == {}
