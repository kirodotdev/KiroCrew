"""A Kiro CLI pick saved while the gateway runs gets the start check a boot gives it.

A boot with Kiro Agent saved starts nothing and records kiro-cli's help verdict
(``platform.bootstrap.apply_kas_cli_floor``). A Kiro CLI pick moves onto KAS only
after a v3 start kiro-cli did not refuse, so a pick saved later runs that check
through the gateway's subscription (``process_entry._watch_saved_kiro_pick``)
instead of waiting for a restart.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from kiro_crew import kiro_cli
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.kas_transport import KAS_RELAY_ENGINE, KAS_RELAY_ENGINE_FLAG
from kiro_crew.agent_sdk import backends as registry

KAS = registry.ACP_BACKEND_KAS
KIRO = registry.ACP_BACKEND_KIRO

_NEW_HELP = f"Usage: kiro-cli acp [OPTIONS]\n  {KAS_RELAY_ENGINE_FLAG} <ENGINE>  [possible values: v2, {KAS_RELAY_ENGINE}]\n"
_REFUSAL = "error: V3 is currently not supported on this system.\n"


@pytest.fixture
def kas_selectable(_floor_monkeypatch):
    _floor_monkeypatch.setattr(registry, "_baseline", {KIRO, KAS})
    _floor_monkeypatch.setattr(registry, "_selectable", {KIRO, KAS})
    _floor_monkeypatch.setattr(registry, "_host_unservable", {})


@pytest.fixture
def pinned_cli(tmp_path, _floor_monkeypatch):
    binary = tmp_path / "kiro-cli"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    _floor_monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: str(binary))
    _floor_monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))
    return binary


def _help(monkeypatch, text):
    monkeypatch.setattr(kiro_cli, "kas_relay_help", lambda binary: text)


class TestAKiroCliPickSavedWhileTheGatewayRuns:
    """A Kiro CLI pick saved after boot lands where a boot with it stored would put it.

    A boot with Kiro Agent saved starts nothing and records kiro-cli's help verdict.
    A pick saved later goes through the real watcher: its ``agent.acp_backend``
    change reaches the floor's subscription, which runs the boot's start check for
    the pick. A refused or unknown start keeps the pick on kiro-cli's v2 engine, as
    after a restart, instead of running it on KAS on the help verdict alone.
    """

    _SAVED_PICK_TASK = "kas-cli-floor-saved-pick"

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path, _floor_monkeypatch, pinned_cli, kas_selectable):
        from unittest.mock import patch

        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.config import live
        from kiro_crew.slack import gateway

        live.reset_for_tests()
        _help(_floor_monkeypatch, _NEW_HELP)
        _floor_monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)
        _floor_monkeypatch.setattr(kas_floor_wait, "_armed", None)
        self.cfg_file = tmp_path / "config.json"
        self._write({"acp_backend": "kas"})
        with (
            patch("kiro_crew.config.loader.config_path", return_value=self.cfg_file),
            patch(
                "kiro_crew.config.loader.config_local_path", return_value=tmp_path / "local.json"
            ),
        ):
            yield
        live.reset_for_tests()

    def _write(self, agent: dict) -> None:
        from _hot_reload_helpers import write_config

        write_config(self.cfg_file, {"agent": agent})

    @staticmethod
    def _starts(monkeypatch, output: str | None, *, held: threading.Event | None = None):
        """The v3 start answers *output* (``None``: it never finished), after *held* is set."""
        calls: list[str] = []

        def _fake(binary, argv_tail):
            calls.append(binary)
            if held is not None:
                assert held.wait(60), "the test never released the start"
            return output

        monkeypatch.setattr(kiro_cli, "kas_relay_start_output", _fake)
        return calls

    async def _boot_then_save(
        self, agent: dict, *, changes: bool = True
    ) -> "asyncio.Task[None] | None":
        """Boot the floor on Kiro Agent, save *agent* through the watcher; the check task."""
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.slack import gateway

        boot = KiroCrewConfig.load()
        await asyncio.wait_for(gateway._start_kas_cli_floor(boot), timeout=30)
        watcher = live.watch()
        watcher.prime(boot)
        self._write(agent)
        change = await watcher.refresh_now()
        # A pick that already loads as KAS changes nothing the watcher compares.
        assert (change is not None and change.touched("agent.acp_backend")) is changes
        checks = [t for t in asyncio.all_tasks() if t.get_name() == self._SAVED_PICK_TASK]
        return checks[0] if checks else None

    def _save_and_settle(self, agent: dict, *, changes: bool = True) -> tuple[bool, str]:
        """``(a check ran, what agent.acp_backend loads as once it settled)``."""
        from kiro_crew.config.loader import KiroCrewConfig

        async def _run():
            check = await self._boot_then_save(agent, changes=changes)
            if check is not None:
                await asyncio.wait_for(check, timeout=30)
            return check is not None

        ran = asyncio.run(_run())
        return ran, KiroCrewConfig.load().agent.acp_backend

    def test_boot_with_kiro_agent_starts_nothing(self, monkeypatch):
        """The pinned rule: without a stored pick, kiro-cli's v3 engine is never started."""
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.slack import gateway

        calls = self._starts(monkeypatch, "")

        async def _run():
            await asyncio.wait_for(gateway._start_kas_cli_floor(KiroCrewConfig.load()), timeout=30)

        asyncio.run(_run())
        assert calls == []
        assert registry.kiro_cli_listed_backend() == KAS

    @pytest.mark.parametrize("output", [_REFUSAL, None], ids=["refused", "unknown"])
    def test_a_start_that_does_not_run_keeps_the_saved_pick_on_kiro_cli(self, monkeypatch, output):
        calls = self._starts(monkeypatch, output)
        ran, backend = self._save_and_settle({"acp_backend": "kiro"})
        assert ran, "no start check ran for the saved Kiro CLI pick"
        assert len(calls) == 1
        assert backend == KIRO

    def test_a_refused_start_calls_an_inherited_kas_the_default(self, monkeypatch, caplog):
        """Bots inherit KAS from the pick, nobody stored it: the notice says so, as at boot."""
        from kiro_crew.platform import bootstrap

        self._starts(monkeypatch, _REFUSAL)
        with caplog.at_level(logging.WARNING, logger=bootstrap.logger.name):
            ran, backend = self._save_and_settle({"acp_backend": "kiro"})
        assert ran and backend == KIRO
        assert "KAS is the default backend, but " in caplog.text
        assert "KAS is selected, but " not in caplog.text

    def test_a_start_that_runs_moves_the_saved_pick_onto_kas(self, monkeypatch):
        calls = self._starts(monkeypatch, "")
        ran, backend = self._save_and_settle({"acp_backend": "kiro"})
        assert ran and len(calls) == 1
        assert backend == KAS

    def test_a_pick_saved_over_a_value_that_degrades_to_kiro_cli_is_checked(self, monkeypatch):
        """An unknown harness loads as the Kiro CLI floor: before and after both read ``kiro``."""
        self._write({"acp_backend": "no-such-harness"})
        calls = self._starts(monkeypatch, "")
        ran, backend = self._save_and_settle({"acp_backend": "kiro"})
        assert ran and len(calls) == 1
        assert backend == KAS

    def test_a_new_spawn_waits_for_the_saved_picks_verdict(self, monkeypatch):
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.agent_sdk.kas_floor_wait import await_kas_floor_verdict
        from kiro_crew.config.loader import KiroCrewConfig

        # The bound must outlast any scheduling delay, so only the verdict releases.
        monkeypatch.setattr(
            kas_floor_wait, "KAS_FLOOR_WAIT_SECS", 600.0  # flake-ok: a bound that never fires
        )
        held = threading.Event()
        self._starts(monkeypatch, "", held=held)

        async def _run():
            check = await self._boot_then_save({"acp_backend": "kiro"})
            assert check is not None
            spawn = asyncio.create_task(await_kas_floor_verdict(member=False))
            try:
                await asyncio.sleep(0)
                assert not spawn.done(), "a spawn went ahead before the saved pick's verdict"
            finally:
                held.set()
                await asyncio.wait_for(check, timeout=30)
            await asyncio.wait_for(spawn, timeout=30)

        asyncio.run(_run())
        assert KiroCrewConfig.load().agent.acp_backend == KAS

    def test_a_start_verdict_on_record_moves_the_pick_at_once(self, monkeypatch):
        """A start recorded for this kiro-cli is the boot's answer too: nothing to ask."""
        from kiro_crew.acp.kas_engine_refusal import remember_start_verdict

        remember_start_verdict(str(kiro_cli.pin_kiro_cli()[0]), True)
        calls = self._starts(monkeypatch, _REFUSAL)
        ran, backend = self._save_and_settle({"acp_backend": "kiro"}, changes=False)
        assert not ran and calls == []
        assert backend == KAS

    def test_a_pick_on_the_bundle_route_is_not_checked(self, monkeypatch):
        calls = self._starts(monkeypatch, "")
        ran, backend = self._save_and_settle({"acp_backend": "kiro", "kas_bundle": True})
        assert not ran and calls == []
        assert backend == KIRO
