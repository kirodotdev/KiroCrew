"""Connected computers: the default-off feature preview and its live teardown.

* fresh install -- no preview entry means off on both machines.
* crafted request while off -- every gate raises the typed refusal; only an
  exact ``True`` turns it on.
* disabled during live execution -- the config watcher closes every connector
  socket and revokes the local grants, without touching files.
* re-enable -- revokes whatever was saved, then starts the local controller;
  revoked consent stays revoked.
* while it stays off -- nothing is scheduled, startup creates no controller,
  and shutdown stops nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.connected_computers import broker as broker_mod
from kiro_crew.connected_computers import controller as controller_mod
from kiro_crew.connected_computers import disable, flag
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.asyncio


def _cfg(previews: Any) -> Any:
    return SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))


class _Change:
    def __init__(self, old: Any, new: Any, touched: bool = True) -> None:
        self.old = old
        self.new = new
        self._touched = touched

    def touched(self, *_paths: str) -> bool:
        return self._touched


class _FakeBroker:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def shutdown_all(self, why: str) -> int:
        self.closed.append(why)
        return 1


class _FakeController:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def started(self) -> int:
        return self.calls.count("start")

    @property
    def offs(self) -> int:
        return self.calls.count("off")

    def start(self) -> None:
        self.calls.append("start")

    async def reconcile(self) -> None:
        self.calls.append("reconcile")

    async def off(self) -> list[Any]:
        self.calls.append("off")
        return []


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(disable, "_STARTUP_TASK", None)
    monkeypatch.setattr(disable, "_TASKS", set())
    monkeypatch.setattr(
        disable, "local_switch", lambda cfg=None: flag.local_switch(cfg) if cfg else True
    )
    b, c = _FakeBroker(), _FakeController()
    monkeypatch.setattr(broker_mod, "get_broker", lambda: b)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: c)
    return b, c


@pytest.fixture
def snapshot(monkeypatch):
    from kiro_crew.config import live

    def use(previews: Any) -> None:
        cfg = None if previews is None else _cfg(previews)
        monkeypatch.setattr(live, "snapshot", lambda: cfg)

    return use


class TestFreshInstallIsOff:
    @pytest.mark.parametrize("previews", [None, {}, {"other": True}])
    def test_flag_off_on_fresh_install(self, previews):
        cfg = _cfg(previews)
        assert flag.preview_on(cfg) is False
        assert flag.local_enabled(cfg) is False
        assert flag.remote_enabled(cfg) is False

    @pytest.mark.parametrize("value", ["true", 1, "1", "yes", [True]])
    def test_only_an_exact_true_turns_it_on(self, value):
        assert flag.preview_on(_cfg({flag.PREVIEW_NAME: value})) is False

    def test_exact_true_turns_the_local_side_on(self):
        assert flag.local_enabled(_cfg({flag.PREVIEW_NAME: True})) is True


class TestCraftedRequestWhileOff:
    def test_require_remote_refuses_with_typed_reason(self):
        with pytest.raises(flag.ConnectedComputersDisabled) as err:
            flag.require_remote(_cfg({}))
        assert err.value.code == "connected_computers_disabled"
        assert "turned off" in str(err.value)

    def test_require_local_refuses_with_typed_reason(self):
        with pytest.raises(flag.ConnectedComputersDisabled):
            flag.require_local(_cfg({flag.PREVIEW_NAME: False}))

    def test_remote_needs_the_existing_developer_gate_too(self, monkeypatch):
        import kiro_crew.session_environment as se

        monkeypatch.setattr(se, "session_environments_enabled", lambda: False)
        assert flag.remote_enabled(_cfg({flag.PREVIEW_NAME: True})) is False

    def test_unreadable_config_reads_as_off(self, monkeypatch):
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(live, "snapshot", lambda: None)

        def unreadable():
            raise OSError("config unavailable")

        monkeypatch.setattr(KiroCrewConfig, "load", unreadable)
        assert flag.preview_on() is False
        assert flag.local_switch() is None


async def _drain() -> None:
    await asyncio.wait_for(asyncio.gather(*list(disable._TASKS)), FALLBACK_TIMEOUT_SECS)


async def test_disabled_during_live_execution_stops_sockets_and_grants(fakes):
    b, c = fakes
    disable.apply(_Change(_cfg({flag.PREVIEW_NAME: True}), _cfg({})))
    await _drain()
    assert len(b.closed) == 1
    assert c.calls == ["off"]


async def test_unrelated_config_change_does_nothing(fakes):
    b, c = fakes
    disable.apply(_Change(_cfg({}), _cfg({flag.PREVIEW_NAME: True}), touched=False))
    assert disable._TASKS == set()
    assert (b.closed, c.calls) == ([], [])


@pytest.mark.parametrize("value", [False, True])
async def test_unchanged_value_does_nothing(fakes, value):
    b, c = fakes
    previews = {flag.PREVIEW_NAME: value}
    disable.apply(_Change(_cfg(previews), _cfg(dict(previews))))
    assert disable._TASKS == set()
    assert (b.closed, c.calls) == ([], [])


async def test_another_preview_toggled_while_off_schedules_nothing(fakes):
    """Flag-off parity: a gateway that never turned this on does no new work."""
    b, c = fakes
    disable.apply(_Change(_cfg({"crew_mode": False}), _cfg({"crew_mode": True})))
    assert disable._TASKS == set()
    assert (b.closed, c.calls) == ([], [])


async def test_reenable_revokes_saved_shares_before_it_starts(fakes):
    """A share left by an Off this gateway never saw does not come back on."""
    b, c = fakes
    disable.apply(_Change(_cfg({}), _cfg({flag.PREVIEW_NAME: True})))
    await _drain()
    assert c.calls == ["off", "start"]
    assert b.closed == []


async def test_reenable_does_nothing_when_the_switch_reads_off_again(monkeypatch, fakes):
    _b, c = fakes
    monkeypatch.setattr(
        disable,
        "local_switch",
        lambda cfg=None: flag.local_switch(cfg) if cfg is not None else None,
    )
    disable.apply(_Change(_cfg({}), _cfg({flag.PREVIEW_NAME: True})))
    await _drain()
    assert c.calls == []


@pytest.mark.parametrize("previews", [None, {}, {flag.PREVIEW_NAME: False}])
async def test_startup_while_off_creates_no_controller_and_reads_nothing(
    monkeypatch, fakes, snapshot, previews
):
    """Flag-off parity: no controller, no vault read, no task at startup."""
    _b, c = fakes

    def no_read() -> bool:
        raise AssertionError("startup with the switch off reads nothing more")

    def no_controller() -> Any:
        raise AssertionError("startup with the switch off creates no controller")

    monkeypatch.setattr(disable, "local_switch", no_read)
    monkeypatch.setattr(controller_mod, "get_controller", no_controller)
    snapshot(previews)
    disable.start_if_enabled()
    assert disable._STARTUP_TASK is None
    assert disable._TASKS == set()
    assert c.calls == []


async def test_startup_while_on_starts_and_keeps_saved_shares(fakes, snapshot):
    """A gateway restart with sharing on keeps its grants: nothing is revoked."""
    _b, c = fakes
    snapshot({flag.PREVIEW_NAME: True})
    disable.start_if_enabled()
    await _drain()
    assert c.calls == ["start"]


async def test_failed_startup_blocks_reenable(monkeypatch, fakes, snapshot):
    _b, c = fakes

    def failed_start() -> None:
        raise OSError("vault unavailable")

    monkeypatch.setattr(c, "start", failed_start)
    snapshot({flag.PREVIEW_NAME: True})
    disable.start_if_enabled()
    await _drain()
    await disable._start_local()
    assert c.calls == []


async def test_shutdown_while_off_stops_nothing(monkeypatch, fakes):
    b, c = fakes
    monkeypatch.setattr(broker_mod, "_BROKER", None)
    monkeypatch.setattr(controller_mod, "_CONTROLLER", None)
    await asyncio.wait_for(disable.shutdown(), FALLBACK_TIMEOUT_SECS)
    assert (b.closed, c.calls) == ([], [])


async def test_shutdown_joins_startup_before_stopping_controller(monkeypatch, fakes, snapshot):
    _b, c = fakes
    stopped = []

    async def stop_controller():
        assert c.started == 1
        stopped.append(True)

    monkeypatch.setattr(c, "shutdown", stop_controller, raising=False)
    monkeypatch.setattr(controller_mod, "_CONTROLLER", c)
    monkeypatch.setattr(broker_mod, "_BROKER", None)
    snapshot({flag.PREVIEW_NAME: True})
    disable.start_if_enabled()
    await asyncio.wait_for(disable.shutdown(), FALLBACK_TIMEOUT_SECS)
    assert stopped == [True]
    assert c.offs == 0


class TestRemoteRefusalNamesTheMissingGate:
    """A remote gateway with the preview on but no ``KIROCREW_DEV_MODE`` says so."""

    def test_flag_preview_off_keeps_the_settings_reason(self, monkeypatch):
        import kiro_crew.session_environment as se

        monkeypatch.setattr(se, "session_environments_enabled", lambda: True)
        assert flag.remote_refusal(_cfg({})) == flag.DISABLED_REASON

    def test_flag_developer_gate_off_names_the_env_var(self, monkeypatch):
        import kiro_crew.session_environment as se

        monkeypatch.setattr(se, "session_environments_enabled", lambda: False)
        reason = flag.remote_refusal(_cfg({flag.PREVIEW_NAME: True}))
        assert reason == flag.DEVELOPER_GATE_REASON
        assert se.DEVELOPER_FLAG in reason
        with pytest.raises(flag.ConnectedComputersDisabled) as err:
            flag.require_remote(_cfg({flag.PREVIEW_NAME: True}))
        assert str(err.value) == flag.DEVELOPER_GATE_REASON
        assert err.value.code == "connected_computers_disabled"

    def test_flag_both_on_has_no_refusal(self, monkeypatch):
        import kiro_crew.session_environment as se

        monkeypatch.setattr(se, "session_environments_enabled", lambda: True)
        assert flag.remote_refusal(_cfg({flag.PREVIEW_NAME: True})) is None
        flag.require_remote(_cfg({flag.PREVIEW_NAME: True}))


class TestComputerServersMeetTheGateOnlyWhileOn:
    """Flag-off parity: a person's own ``connected-computer-*`` server keeps its old
    answer, and a plain ``computer-*`` name (``computer-use``) is never caught."""

    @pytest.fixture
    def snapshot(self, monkeypatch):
        from kiro_crew.config import live

        def use(previews: Any) -> None:
            monkeypatch.setattr(live, "snapshot", lambda: _cfg(previews))

        return use

    def test_off_keeps_a_computer_named_server_auto_approved(self, snapshot):
        from kiro_crew.platform.governance import may_skip_gate

        snapshot({})
        assert may_skip_gate("@connected-computer-notes", None) is True
        assert may_skip_gate("@connected-computer-notes/write_note", None) is True

    def test_on_sends_every_computer_call_to_the_gate(self, snapshot):
        from kiro_crew.platform.governance import may_skip_gate

        snapshot({flag.PREVIEW_NAME: True})
        assert may_skip_gate("@connected-computer-notes", None) is False
        assert may_skip_gate("@connected-computer-notes/write_note", None) is False

    def test_on_leaves_a_plain_computer_named_server_alone(self, snapshot):
        from kiro_crew.platform.governance import may_skip_gate

        snapshot({flag.PREVIEW_NAME: True})
        assert may_skip_gate("@computer-use", None) is True
        assert may_skip_gate("@computer-notes/write_note", None) is True

    def test_no_snapshot_keeps_the_gate(self, monkeypatch):
        from kiro_crew.config import live
        from kiro_crew.platform.governance import may_skip_gate

        monkeypatch.setattr(live, "snapshot", lambda: None)
        assert may_skip_gate("@connected-computer-notes", None) is False

    @pytest.mark.parametrize("previews", [{}, {flag.PREVIEW_NAME: True}])
    def test_workspace_meets_the_gate_either_way(self, snapshot, previews):
        from kiro_crew.platform.governance import may_skip_gate

        snapshot(previews)
        assert may_skip_gate("@workspace", None) is False


class TestWhereTheSwitchIsRead:
    """No configuration passed: the live snapshot, else the file, else off / unknown."""

    @pytest.fixture
    def sources(self, monkeypatch):
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        state = SimpleNamespace(snapshot=None, loaded=None, load_error=None, loads=0)

        def load(cls: Any) -> Any:
            state.loads += 1
            if state.load_error is not None:
                raise state.load_error
            return state.loaded

        monkeypatch.setattr(live, "snapshot", lambda: state.snapshot)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(load))
        return state

    def test_the_live_snapshot_wins(self, sources):
        sources.snapshot = _cfg({flag.PREVIEW_NAME: True})
        assert flag.preview_on() is True
        assert flag.local_switch() is True
        assert sources.loads == 0

    def test_without_a_snapshot_the_file_is_read(self, sources):
        sources.loaded = _cfg({flag.PREVIEW_NAME: True})
        assert flag.preview_on() is True
        assert flag.local_enabled() is True
        assert flag.local_switch() is True
        assert sources.loads == 3

    def test_an_unreadable_file_is_off_for_new_work(self, sources):
        sources.load_error = OSError("config unreadable")
        assert flag.preview_on() is False
        assert flag.remote_enabled() is False
        with pytest.raises(flag.ConnectedComputersDisabled):
            flag.require_local()

    def test_an_unreadable_file_is_unknown_to_the_off_sweep(self, sources):
        sources.load_error = OSError("config unreadable")
        assert flag.local_switch() is None

    def test_a_given_configuration_is_read_as_is(self, sources):
        assert flag.local_switch(_cfg({flag.PREVIEW_NAME: True})) is True
        assert flag.local_switch(_cfg({flag.PREVIEW_NAME: False})) is False
        assert sources.loads == 0

    def test_on_with_the_developer_gate_serves(self, sources, monkeypatch):
        import kiro_crew.session_environment as se

        monkeypatch.setattr(se, "session_environments_enabled", lambda: True)
        sources.snapshot = _cfg({flag.PREVIEW_NAME: True})
        assert flag.remote_refusal() is None
        assert flag.remote_enabled() is True
        flag.require_remote()
        flag.require_local()

    @pytest.mark.parametrize("dashboard", [None, SimpleNamespace(), SimpleNamespace(x=1)])
    def test_a_configuration_without_previews_is_off(self, dashboard):
        assert flag.preview_on(SimpleNamespace(dashboard=dashboard)) is False


def test_the_disabled_refusal_defaults_to_the_settings_reason():
    err = flag.ConnectedComputersDisabled()
    assert str(err) == flag.DISABLED_REASON
    assert err.code == "connected_computers_disabled"


@pytest.mark.parametrize("edition_name", ["oss", "lts"])
@pytest.mark.parametrize("source", ["explicit", "snapshot", "file"])
def test_every_computer_preview_reader_honors_the_edition_lock(monkeypatch, edition_name, source):
    from kiro_crew import edition, edition_locks
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(edition, "EDITION", edition_name)
    monkeypatch.setattr(edition_locks, "_logged", set())
    # Keep the developer gate open so only the preview's own edition lock decides.
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: True)
    cfg = _cfg({flag.PREVIEW_NAME: True})
    monkeypatch.setattr(live, "snapshot", lambda: cfg if source == "snapshot" else None)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    passed = cfg if source == "explicit" else None
    expected = edition_name == "oss"
    assert flag.preview_on(passed) is expected
    assert flag.local_enabled(passed) is expected
    assert flag.local_switch(passed) is expected
    assert flag.remote_enabled(passed) is expected
    assert flag.live_preview_on() is (expected and source == "snapshot")
    assert flag.remote_refusal(passed) == (None if expected else flag.DISABLED_REASON)
    for gate in (flag.require_remote, flag.require_local):
        if expected:
            gate(passed)
        else:
            with pytest.raises(flag.ConnectedComputersDisabled):
                gate(passed)
    assert cfg.dashboard.feature_previews == {flag.PREVIEW_NAME: True}


def test_lts_config_hook_treats_stored_preview_changes_as_off(monkeypatch, fakes):
    from kiro_crew import edition, edition_locks

    monkeypatch.setattr(edition, "EDITION", "lts")
    monkeypatch.setattr(edition_locks, "_logged", set())
    on, off = _cfg({flag.PREVIEW_NAME: True}), _cfg({flag.PREVIEW_NAME: False})
    disable.apply(_Change(off, on))
    disable.apply(_Change(on, off))
    assert disable._TASKS == set()
    broker, controller = fakes
    assert broker.closed == []
    assert controller.calls == []


class TestConnectorWaitsWhileTheRemoteIsOff:
    """A remote switched off answers 503: the local grant waits, never ends (flag re-enable).

    No upgrade status ends a grant: a relay on the forwarded port can answer
    401/403 as easily as 503. Only the gateway's authenticated ``refusal`` does
    (``test_connected_computers_protected``).
    """

    class _Handshake(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(status)
            self.status = status

    def test_flag_a_switched_off_remote_is_recognised(self):
        exc = self._Handshake(controller_mod.REMOTE_OFF_STATUS)
        assert controller_mod._is_remote_off(exc) is True

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_b11_an_upgrade_status_is_not_the_remote_off_answer(self, status):
        assert controller_mod._is_remote_off(self._Handshake(status)) is False

    def test_b11_no_status_based_refusal_remains(self):
        assert not hasattr(controller_mod, "_is_auth_refusal")
        assert controller_mod._is_remote_off(OSError("unreachable")) is False


@pytest.mark.parametrize("unreadable", [False, True])
def test_shared_crew_excludes_every_connected_computer_entry(monkeypatch, unreadable):
    from kiro_crew.config import live
    from kiro_crew.shared_crew import preview

    def shared():
        if unreadable:
            raise OSError("shared state unavailable")
        return True

    cfg = _cfg({flag.PREVIEW_NAME: True})
    monkeypatch.setattr(preview, "preview_enabled", shared)
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    assert flag.preview_on(cfg) is False
    assert flag.live_preview_on() is False
    assert flag.local_enabled(cfg) is False
    assert flag.remote_enabled(cfg) is False
    assert flag.local_switch(cfg) is (None if unreadable else False)
    for gate in (flag.require_local, flag.require_remote):
        with pytest.raises(flag.ConnectedComputersDisabled):
            gate(cfg)


@pytest.mark.parametrize("unreadable", ["dashboard", "base", "overlay"])
async def test_authoritative_off_after_unknown_stops_sockets_and_revokes_grants(fakes, unreadable):
    broker, controller = fakes
    old = _cfg({flag.PREVIEW_NAME: True})
    if unreadable == "dashboard":
        old._read_degraded_sections = frozenset({"dashboard"})
    else:
        setattr(old, "_base_unreadable" if unreadable == "base" else "_overlay_unreadable", True)
    assert flag.local_switch(old) is None
    new = _cfg({flag.PREVIEW_NAME: False})
    assert flag.local_switch(new) is False
    disable.apply(_Change(old, new))
    await _drain()
    assert len(broker.closed) == 1
    assert controller.calls == ["off"]


async def test_unknown_to_on_recovery_does_not_invent_a_revoking_transition(fakes):
    broker, controller = fakes
    old = _cfg({})
    old._base_unreadable = True
    disable.apply(_Change(old, _cfg({flag.PREVIEW_NAME: True})))
    await _drain()
    assert broker.closed == []
    assert controller.calls == ["start", "reconcile"]
