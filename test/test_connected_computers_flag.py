"""Connected computers: the default-off feature preview and its live teardown.

* fresh install -- no preview entry means off on both machines.
* crafted request while off -- every gate raises the typed refusal; only an
  exact ``True`` turns it on.
* disabled during live execution -- the config watcher closes every connector
  socket and revokes the local grants, without touching files.
* re-enable -- starts the local controller only; revoked consent stays revoked.
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
        self.started = 0
        self.offs = 0

    def start(self) -> None:
        self.started += 1

    async def off(self) -> list[Any]:
        self.offs += 1
        return []


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(disable, "_STARTUP_TASK", None)
    monkeypatch.setattr(disable, "_TASKS", set())
    monkeypatch.setattr(disable, "local_switch", lambda: True)
    b, c = _FakeBroker(), _FakeController()
    monkeypatch.setattr(broker_mod, "get_broker", lambda: b)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: c)
    return b, c


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
        monkeypatch.setattr(flag, "_config", lambda cfg: None)
        assert flag.preview_on() is False


async def _drain() -> None:
    await asyncio.wait_for(asyncio.gather(*list(disable._TASKS)), FALLBACK_TIMEOUT_SECS)


async def test_disabled_during_live_execution_stops_sockets_and_grants(fakes):
    b, c = fakes
    disable.apply(_Change(_cfg({flag.PREVIEW_NAME: True}), _cfg({})))
    await _drain()
    assert len(b.closed) == 1
    assert c.offs == 1
    assert c.started == 0


async def test_unrelated_config_change_does_nothing(fakes):
    b, c = fakes
    disable.apply(_Change(_cfg({}), _cfg({flag.PREVIEW_NAME: True}), touched=False))
    await _drain()
    assert (b.closed, c.offs, c.started) == ([], 0, 0)


async def test_unchanged_value_does_nothing(fakes):
    b, c = fakes
    on = {flag.PREVIEW_NAME: True}
    disable.apply(_Change(_cfg(on), _cfg(dict(on))))
    await _drain()
    assert (b.closed, c.offs, c.started) == ([], 0, 0)


async def test_reenable_starts_only_and_restores_no_consent(fakes):
    b, c = fakes
    disable.apply(_Change(_cfg({}), _cfg({flag.PREVIEW_NAME: True})))
    await _drain()
    assert c.started == 1
    assert c.offs == 0
    assert b.closed == []


@pytest.mark.parametrize("switch", [False, None, True])
async def test_start_if_enabled_is_opt_in(monkeypatch, fakes, switch):
    _b, c = fakes
    monkeypatch.setattr(disable, "local_switch", lambda: switch)
    disable.start_if_enabled()
    await _drain()
    assert c.started == int(switch is True)
    assert c.offs == int(switch is False)


async def test_reenable_waits_for_startup_off(monkeypatch, fakes):
    _b, c = fakes
    off_entered, finish_off, enable_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_off():
        off_entered.set()
        await asyncio.wait_for(finish_off.wait(), FALLBACK_TIMEOUT_SECS)
        c.offs += 1
        return []

    async def enable():
        enable_entered.set()
        await disable._start_local()

    monkeypatch.setattr(c, "off", delayed_off)
    monkeypatch.setattr(disable, "local_switch", lambda: False)
    disable.start_if_enabled()
    await asyncio.wait_for(off_entered.wait(), FALLBACK_TIMEOUT_SECS)
    monkeypatch.setattr(disable, "local_switch", lambda: True)
    task = asyncio.create_task(enable())
    try:
        await asyncio.wait_for(enable_entered.wait(), FALLBACK_TIMEOUT_SECS)
        assert c.started == 0
        finish_off.set()
        await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
        assert (c.offs, c.started) == (1, 1)
    finally:
        finish_off.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)
        await _drain()


async def test_failed_startup_off_blocks_reenable(monkeypatch, fakes):
    _b, c = fakes

    async def failed_off():
        raise OSError("vault unavailable")

    monkeypatch.setattr(c, "off", failed_off)
    monkeypatch.setattr(disable, "local_switch", lambda: False)
    disable.start_if_enabled()
    await _drain()
    monkeypatch.setattr(disable, "local_switch", lambda: True)
    await disable._start_local()
    assert c.started == 0


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


async def test_shutdown_joins_startup_before_stopping_controller(monkeypatch, fakes):
    _b, c = fakes
    stopped = []

    async def stop_controller():
        assert c.offs == 1
        stopped.append(True)

    monkeypatch.setattr(disable, "local_switch", lambda: False)
    monkeypatch.setattr(c, "shutdown", stop_controller, raising=False)
    monkeypatch.setattr(controller_mod, "_CONTROLLER", c)
    monkeypatch.setattr(broker_mod, "_BROKER", None)
    disable.start_if_enabled()
    await asyncio.wait_for(disable.shutdown(), FALLBACK_TIMEOUT_SECS)
    assert stopped == [True]
    assert c.started == 0
