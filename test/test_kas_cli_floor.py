"""The runtime floor that keeps a host on kiro-cli when its kiro-cli cannot serve KAS.

``kirocrew doctor`` reads ``kiro-cli acp --help`` to report whether the KAS engine
exists. The same probe gates boot: KAS configured on a kiro-cli without the engine
falls back to kiro-cli with a logged notice, and the dashboard's KAS row names what
the update must bring.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kiro_crew import kiro_cli
from kiro_crew.acp.kas_transport import KAS_RELAY_ENGINE, KAS_RELAY_ENGINE_FLAG
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk import backends as registry
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.platform import bootstrap

KAS = registry.ACP_BACKEND_KAS
KIRO = registry.ACP_BACKEND_KIRO

_OLD_HELP = "Usage: kiro-cli acp [OPTIONS]\n  --trust-all-tools\n"
_NEW_HELP = f"Usage: kiro-cli acp [OPTIONS]\n  {KAS_RELAY_ENGINE_FLAG} <ENGINE>  [possible values: v2, {KAS_RELAY_ENGINE}]\n"
_OTHER_ENGINE_HELP = (
    f"Usage: kiro-cli acp [OPTIONS]\n  {KAS_RELAY_ENGINE_FLAG} <ENGINE>  [possible values: v2]\n"
)


@pytest.fixture(autouse=True)
def _restore_registry():
    baseline = set(registry._baseline)
    selectable = set(registry._selectable)
    unservable = dict(registry._host_unservable)
    backend_install.clear_probe_cache()
    yield
    registry._baseline.clear()
    registry._baseline.update(baseline)
    registry._selectable.clear()
    registry._selectable.update(selectable)
    registry._host_unservable.clear()
    registry._host_unservable.update(unservable)
    backend_install.clear_probe_cache()


@pytest.fixture
def kas_selectable(monkeypatch):
    monkeypatch.setattr(registry, "_baseline", {KIRO, KAS})
    monkeypatch.setattr(registry, "_selectable", {KIRO, KAS})
    monkeypatch.setattr(registry, "_host_unservable", {})


@pytest.fixture
def pinned_cli(tmp_path, monkeypatch):
    binary = tmp_path / "kiro-cli"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))
    return binary


def _help(monkeypatch, text):
    calls = []

    def _fake(binary):
        calls.append(binary)
        return text

    monkeypatch.setattr(kiro_cli, "kas_relay_help", _fake)
    return calls


def _cfg(acp_backend=KAS, member_acp_backend=""):
    return SimpleNamespace(
        agent=SimpleNamespace(
            acp_backend=acp_backend,
            member_acp_backend=member_acp_backend,
            kas_bundle=False,
        )
    )


class TestUnsupportedReason:
    def test_help_without_the_engine_flag_is_a_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, _OLD_HELP)
        reason = acp_driver.kas_engine_unsupported_reason()
        assert reason == f"this kiro-cli has no {KAS_RELAY_ENGINE_FLAG} flag"

    def test_flag_without_the_kas_engine_is_a_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, _OTHER_ENGINE_HELP)
        reason = acp_driver.kas_engine_unsupported_reason()
        assert reason == f"this kiro-cli does not offer engine {KAS_RELAY_ENGINE}"

    def test_supported_cli_has_no_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, _NEW_HELP)
        assert acp_driver.kas_engine_unsupported_reason() is None

    def test_failed_probe_is_unknown_not_a_reason(self, monkeypatch, pinned_cli):
        _help(monkeypatch, None)
        assert acp_driver.kas_engine_unsupported_reason() is None

    def test_no_pinned_cli_never_spawns(self, monkeypatch):
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, False))
        calls = _help(monkeypatch, _OLD_HELP)
        assert acp_driver.kas_engine_unsupported_reason() is None
        assert calls == []

    def test_the_driver_keeps_no_help_cache(self):
        assert not hasattr(acp_driver, "_kas_help_cache")


class TestRegistryMark:
    def test_mark_drops_the_backend_and_survives_a_policy_recompute(self, kas_selectable):
        registry.mark_backend_unservable(KAS, "too old")
        assert KAS not in registry.selectable_backends()
        # A policy recompute that denies nothing must not restore it.
        assert registry.apply_selectable_denials(set()) == frozenset()
        assert KAS not in registry.selectable_backends()
        assert registry.host_unservable_reason(KAS) == "too old"

    def test_the_floor_backend_is_never_marked(self, kas_selectable):
        registry.mark_backend_unservable(KIRO, "nope")
        assert KIRO in registry.selectable_backends()
        assert registry.host_unservable_reason(KIRO) is None

    def test_resolving_a_marked_backend_degrades_without_the_generic_warning(
        self, kas_selectable, caplog
    ):
        registry.mark_backend_unservable(KAS, "too old")
        with caplog.at_level(logging.WARNING):
            assert registry.resolve_selected_backend(KAS) == KIRO
        assert "not selectable in this build" not in caplog.text


class TestBootFloor:
    def test_old_cli_falls_back_to_kiro_with_a_notice(
        self, monkeypatch, pinned_cli, kas_selectable, caplog
    ):
        _help(monkeypatch, _OLD_HELP)
        cfg = _cfg()
        with caplog.at_level(logging.WARNING, logger=bootstrap.logger.name):
            notice = bootstrap.apply_kas_cli_floor(cfg)
        assert cfg.agent.acp_backend == KIRO
        assert KAS not in registry.selectable_backends()
        assert notice is not None
        assert "Kiro Crew is using kiro-cli instead" in notice
        assert f"no {KAS_RELAY_ENGINE_FLAG} flag" in notice
        assert notice in caplog.text

    def test_bundle_route_never_probes_kiro_cli(self, monkeypatch, pinned_cli, kas_selectable):
        """agent.kas_bundle spawns Kiro Crew's own engine, so an old kiro-cli on PATH is
        not a fact about this host's KAS; the floor must not move the user off it."""
        calls = _help(monkeypatch, _OLD_HELP)
        cfg = _cfg(KAS)
        cfg.agent.kas_bundle = True
        assert bootstrap.apply_kas_cli_floor(cfg) is None
        assert cfg.agent.acp_backend == KAS
        assert KAS in registry.selectable_backends()
        assert calls == []

    def test_supported_cli_keeps_kas(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, _NEW_HELP)
        cfg = _cfg()
        assert bootstrap.apply_kas_cli_floor(cfg) is None
        assert cfg.agent.acp_backend == KAS
        assert KAS in registry.selectable_backends()

    def test_unknown_verdict_keeps_kas(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, None)
        cfg = _cfg()
        assert bootstrap.apply_kas_cli_floor(cfg) is None
        assert cfg.agent.acp_backend == KAS

    def test_member_backend_alone_triggers_the_floor(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, _OLD_HELP)
        cfg = _cfg(acp_backend=KIRO, member_acp_backend=KAS)
        assert bootstrap.apply_kas_cli_floor(cfg) is not None
        assert registry.resolve_selected_backend(KAS) == KIRO

    def test_kas_not_configured_never_probes(self, monkeypatch, pinned_cli, kas_selectable):
        calls = _help(monkeypatch, _OLD_HELP)
        assert bootstrap.apply_kas_cli_floor(_cfg(acp_backend=KIRO)) is None
        assert calls == []
        assert KAS in registry.selectable_backends()


class TestBackendPanelRow:
    def test_component_spells_the_transport_constants(self):
        assert backend_install.COMPONENT_KAS_ENGINE == (
            f"{backend_install.COMPONENT_KIRO_CLI} acp {KAS_RELAY_ENGINE_FLAG} {KAS_RELAY_ENGINE}"
        )

    def test_marked_kas_row_names_the_missing_engine(self, monkeypatch, kas_selectable):
        monkeypatch.setattr(backend_install.acp_driver, "kiro_cli_resolves", lambda: True)
        registry.mark_backend_unservable(KAS, "too old")
        state = backend_install.probe_backend(KAS)
        assert state.installed == backend_install.MISSING
        assert state.missing_components == (backend_install.COMPONENT_KAS_ENGINE,)
        assert backend_install.probe_backend(KIRO).installed == backend_install.INSTALLED

    def test_unmarked_kas_row_follows_kiro(self, monkeypatch, kas_selectable):
        monkeypatch.setattr(backend_install.acp_driver, "kiro_cli_resolves", lambda: True)
        assert backend_install.probe_backend(KAS).installed == backend_install.INSTALLED


class TestGatewayBootDoesNotWaitForTheFloor:
    """The floor spawns ``acp --help``; the gateway must bind without waiting for it."""

    def test_the_floor_is_scheduled_not_awaited(self, monkeypatch):
        from kiro_crew.slack import gateway

        release = threading.Event()
        seen = []

        def _slow(cfg, *, kas_defaulted=False):
            seen.append(cfg)
            release.wait(5)

        monkeypatch.setattr(gateway, "apply_kas_cli_floor", _slow)
        monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)
        cfg: Any = object()

        async def _boot():
            task = gateway._start_kas_cli_floor(cfg)
            # Returned while the probe is still blocked: nothing on the boot path waited.
            assert not task.done()
            assert gateway._KAS_CLI_FLOOR_TASK is task
            release.set()
            await task

        asyncio.run(_boot())
        assert seen == [cfg]

    def test_declaration_documents_are_read_only_off_loop(self, monkeypatch):
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.config import loader
        from kiro_crew.slack import gateway

        reads = []

        def _declared():
            reads.append(threading.get_ident())
            return False

        monkeypatch.setattr(loader, "acp_backend_declared", _declared)
        monkeypatch.setattr(loader, "member_acp_backend_declared", _declared)
        monkeypatch.setattr(gateway, "apply_kas_cli_floor", lambda cfg, **kwargs: None)
        monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)
        monkeypatch.setattr(kas_floor_wait, "_armed", None)

        async def _boot():
            loop_thread = threading.get_ident()
            task = gateway._start_kas_cli_floor(_cfg(KAS, KAS))
            try:
                assert reads == [], "boot read a config document before returning"
                await asyncio.wait_for(task, timeout=30)
                assert len(reads) == 2
                assert all(thread != loop_thread for thread in reads)
            finally:
                await asyncio.wait_for(task, timeout=30)

        asyncio.run(_boot())

    def test_a_failing_floor_is_logged_and_contained(self, monkeypatch, caplog):
        from kiro_crew.slack import gateway

        def _boom(_cfg, *, kas_defaulted=False):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(gateway, "apply_kas_cli_floor", _boom)
        monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)

        async def _boot():
            await gateway._start_kas_cli_floor(cast(Any, object()))

        with caplog.at_level(logging.WARNING):
            asyncio.run(_boot())
        assert "KAS kiro-cli floor check failed" in caplog.text


class TestDefaultNoticeWording:
    """A host that never chose KAS is not told it did."""

    def test_defaulted_kas_is_named_the_default(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, _OLD_HELP)
        notice = bootstrap.apply_kas_cli_floor(_cfg(), kas_defaulted=True)
        assert notice is not None
        assert notice.startswith("KAS is the default backend, but ")

    def test_chosen_kas_keeps_the_selected_wording(self, monkeypatch, pinned_cli, kas_selectable):
        _help(monkeypatch, _OLD_HELP)
        notice = bootstrap.apply_kas_cli_floor(_cfg())
        assert notice is not None
        assert notice.startswith("KAS is selected, but ")

    @pytest.mark.parametrize("main,member", [(KAS, ""), (KIRO, KAS), (KAS, KAS)])
    @pytest.mark.parametrize(
        "main_declared,member_declared",
        [(False, False), (True, False), (False, True), (True, True), (None, False), (False, None)],
    )
    def test_gateway_reuses_each_declaration_for_the_notice(
        self,
        monkeypatch,
        pinned_cli,
        kas_selectable,
        caplog,
        main,
        member,
        main_declared,
        member_declared,
    ):
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.config import loader
        from kiro_crew.slack import gateway

        _help(monkeypatch, _OLD_HELP)
        reads = []
        loop_thread = threading.get_ident()

        def _declared(role, value):
            assert threading.get_ident() != loop_thread
            reads.append(role)
            if value is None:
                raise OSError("declaration unreadable")
            return value

        monkeypatch.setattr(
            loader, "acp_backend_declared", lambda: _declared("main", main_declared)
        )
        monkeypatch.setattr(
            loader, "member_acp_backend_declared", lambda: _declared("member", member_declared)
        )
        monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)
        monkeypatch.setattr(kas_floor_wait, "_armed", None)

        async def _run():
            await asyncio.wait_for(gateway._start_kas_cli_floor(_cfg(main, member)), timeout=30)

        with caplog.at_level(logging.WARNING, logger=bootstrap.logger.name):
            asyncio.run(_run())
        expected_reads = ["main"] if main == KAS else []
        if member == KAS and not (main == KAS and main_declared is None):
            expected_reads.append("member")
        assert reads == expected_reads
        defaulted = (main != KAS or main_declared is False) and (
            member != KAS or member_declared is False
        )
        prefix = "KAS is the default backend, but " if defaulted else "KAS is selected, but "
        assert any(record.message.startswith(prefix) for record in caplog.records)


class TestSpawnWaitsForADefaultedFloor:
    """A spawn racing the floor on a defaulted KAS waits for its verdict."""

    @pytest.fixture(autouse=True)
    def _disarm(self, _floor_monkeypatch):
        from kiro_crew.agent_sdk import kas_floor_wait

        _floor_monkeypatch.setattr(kas_floor_wait, "_armed", None)

    @staticmethod
    def _gated_floor(monkeypatch, *, declared):
        """A real floor on an old kiro-cli whose verdict waits on the returned event."""
        from kiro_crew.config import loader
        from kiro_crew.slack import gateway

        release = threading.Event()

        def _floor(cfg, *, kas_defaulted=False):
            assert release.wait(60), "the test never released the floor"
            return bootstrap.apply_kas_cli_floor(cfg, kas_defaulted=kas_defaulted)

        monkeypatch.setattr(loader, "acp_backend_declared", lambda: declared)
        monkeypatch.setattr(loader, "member_acp_backend_declared", lambda: declared)
        monkeypatch.setattr(gateway, "apply_kas_cli_floor", _floor)
        monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)
        return gateway, release

    @staticmethod
    async def _spawn(cfg, key="dashboard:chat"):
        """What a spawn path does: wait at the gate for its role, then pick the backend."""
        from kiro_crew.agent_sdk.kas_floor_wait import await_kas_floor_verdict
        from kiro_crew.members import is_member_session_key, select_provider_backend

        await await_kas_floor_verdict(member=is_member_session_key(key))
        return select_provider_backend(key, cfg.agent.member_acp_backend, cfg.agent.acp_backend)

    def test_spawn_before_the_verdict_waits_and_lands_on_kiro_cli(
        self, monkeypatch, pinned_cli, kas_selectable
    ):
        _help(monkeypatch, _OLD_HELP)
        gateway, release = self._gated_floor(monkeypatch, declared=False)
        cfg = _cfg()

        async def _run():
            floor = gateway._start_kas_cli_floor(cfg)
            spawn = asyncio.create_task(self._spawn(cfg))
            try:
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not spawn.done(), "the spawn went ahead before the floor's verdict"
            finally:
                release.set()
                await floor
            return await spawn

        assert asyncio.run(_run()) == KIRO

    def test_member_dm_on_a_defaulted_member_backend_waits_and_main_does_not(
        self, monkeypatch, pinned_cli, kas_selectable
    ):
        from kiro_crew.members import DM_SLOT_KEY_PREFIX

        _help(monkeypatch, _OLD_HELP)
        gateway, release = self._gated_floor(monkeypatch, declared=False)
        cfg = _cfg(acp_backend=KIRO, member_acp_backend=KAS)
        dm_key = f"dashboard:{DM_SLOT_KEY_PREFIX}helper"

        async def _run():
            floor = gateway._start_kas_cli_floor(cfg)
            member = asyncio.create_task(self._spawn(cfg, dm_key))
            main = asyncio.create_task(self._spawn(cfg))
            try:
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not member.done(), "the member DM went ahead before the floor's verdict"
                # The main role runs kiro-cli, so its spawn is not held by the floor.
                assert main.done()
            finally:
                release.set()
                await floor
            return main.result(), await member

        assert asyncio.run(_run()) == (KIRO, KIRO)

    @pytest.mark.parametrize("explicit_member", [False, True])
    def test_explicit_kas_does_not_wait(
        self, monkeypatch, pinned_cli, kas_selectable, explicit_member
    ):
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.config import loader
        from kiro_crew.members import DM_SLOT_KEY_PREFIX
        from kiro_crew.slack import gateway

        _help(monkeypatch, _OLD_HELP)
        # The lost-run bound must expire before the gate can release on timeout.
        monkeypatch.setattr(kas_floor_wait, "KAS_FLOOR_WAIT_SECS", 60.0)
        monkeypatch.setattr(gateway, "_KAS_CLI_FLOOR_TASK", None)
        cfg = _cfg(acp_backend=KAS, member_acp_backend=KAS)
        release_read = threading.Event()
        release_probe = threading.Event()

        async def _run():
            loop = asyncio.get_running_loop()
            reading = loop.create_future()
            probing = loop.create_future()

            def _main_declared():
                loop.call_soon_threadsafe(reading.set_result, None)
                assert release_read.wait(60), "declaration read was not released"
                return not explicit_member

            def _floor(cfg, *, kas_defaulted=False):
                loop.call_soon_threadsafe(probing.set_result, None)
                assert release_probe.wait(60), "probe was not released"
                return bootstrap.apply_kas_cli_floor(cfg, kas_defaulted=kas_defaulted)

            monkeypatch.setattr(loader, "acp_backend_declared", _main_declared)
            monkeypatch.setattr(loader, "member_acp_backend_declared", lambda: explicit_member)
            monkeypatch.setattr(gateway, "apply_kas_cli_floor", _floor)
            floor = gateway._start_kas_cli_floor(cfg)
            main = asyncio.create_task(self._spawn(cfg))
            member = asyncio.create_task(self._spawn(cfg, f"dashboard:{DM_SLOT_KEY_PREFIX}helper"))
            explicit, defaulted = (member, main) if explicit_member else (main, member)
            try:
                await asyncio.wait_for(reading, timeout=30)
                assert not main.done() and not member.done()
                release_read.set()
                await asyncio.wait_for(probing, timeout=30)
                result = await asyncio.wait_for(explicit, timeout=30)
                assert not floor.done(), "the explicit spawn waited for the probe"
                assert not defaulted.done(), "the defaulted role was released with the explicit one"
                return result
            finally:
                release_read.set()
                release_probe.set()
                await asyncio.wait_for(floor, timeout=30)
                await asyncio.wait_for(asyncio.gather(main, member), timeout=30)

        assert asyncio.run(_run()) == KAS

    def test_release_preserves_the_deadline_and_other_role(self):
        from kiro_crew.agent_sdk import kas_floor_wait

        async def _run():
            verdict = asyncio.get_running_loop().create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=True)
            armed = kas_floor_wait._armed
            assert armed is not None
            main = asyncio.create_task(kas_floor_wait.await_kas_floor_verdict())
            member = asyncio.create_task(kas_floor_wait.await_kas_floor_verdict(member=True))
            await asyncio.sleep(0)
            assert not main.done() and not member.done()
            try:
                kas_floor_wait.release_kas_floor_wait(verdict, main=True, member=False)
                await asyncio.wait_for(main, timeout=30)
                assert kas_floor_wait._armed is armed
                assert not member.done()
                assert not verdict.done()
                # Releasing an already released role is harmless.
                kas_floor_wait.release_kas_floor_wait(verdict, main=True, member=False)
            finally:
                verdict.set_result(None)
                await asyncio.wait_for(asyncio.gather(main, member), timeout=30)

        asyncio.run(_run())

    def test_another_floor_cannot_release_the_armed_roles(self):
        from kiro_crew.agent_sdk import kas_floor_wait

        async def _run():
            loop = asyncio.get_running_loop()
            earlier = loop.create_future()
            verdict = loop.create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=True)
            kas_floor_wait.release_kas_floor_wait(earlier, main=True, member=True)
            armed = kas_floor_wait._armed
            assert armed is not None
            assert armed[2] is not None and not armed[2].done()
            assert armed[3] is not None and not armed[3].done()
            verdict.set_result(None)
            earlier.set_result(None)

        asyncio.run(_run())

    @pytest.mark.parametrize("cancelled", [False, True])
    def test_a_failed_or_cancelled_verdict_releases_waiters(self, cancelled):
        from kiro_crew.agent_sdk import kas_floor_wait

        async def _run():
            verdict = asyncio.get_running_loop().create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=True)
            waiters = [
                asyncio.create_task(kas_floor_wait.await_kas_floor_verdict(member=member))
                for member in (False, True)
            ]
            await asyncio.sleep(0)
            assert all(not waiter.done() for waiter in waiters)
            if cancelled:
                verdict.cancel()
            else:
                verdict.set_exception(RuntimeError("probe failed"))
                verdict.exception()
            await asyncio.wait_for(asyncio.gather(*waiters), timeout=30)

        asyncio.run(_run())

    def test_a_foreign_loop_costs_no_wait(self):
        from kiro_crew.agent_sdk import kas_floor_wait

        async def _arm():
            verdict = asyncio.get_running_loop().create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=True)

        async def _wait():
            waiter = asyncio.create_task(kas_floor_wait.await_kas_floor_verdict())
            await asyncio.sleep(0)
            assert waiter.done()

        asyncio.run(_arm())
        asyncio.run(_wait())

    def test_a_landed_verdict_costs_no_wait(self):
        from kiro_crew.agent_sdk import kas_floor_wait

        async def _run():
            verdict = asyncio.get_running_loop().create_future()
            verdict.set_result(None)
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=False)
            waiter = asyncio.create_task(kas_floor_wait.await_kas_floor_verdict())
            await asyncio.sleep(0)
            assert waiter.done()

        asyncio.run(_run())

    def test_the_wait_is_bounded_by_the_probe_timeout(self, monkeypatch):
        from kiro_crew.agent_sdk import kas_floor_wait

        assert kas_floor_wait.KAS_FLOOR_WAIT_SECS == kiro_cli._KAS_HELP_PROBE_TIMEOUT_SECS
        monkeypatch.setattr(kas_floor_wait, "KAS_FLOOR_WAIT_SECS", 0.0)

        async def _run():
            verdict = asyncio.get_running_loop().create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=False)
            # Past its deadline: returns at once, and leaves the verdict running.
            waiter = asyncio.create_task(kas_floor_wait.await_kas_floor_verdict())
            await asyncio.sleep(0)
            assert waiter.done(), "the wait outlived its deadline"
            assert not verdict.done()

        asyncio.run(_run())


class TestTaskRunBootstrapWaitsForTheFloor:
    """The task-run runtime bootstrap is a spawn path too, so it waits at the gate."""

    def test_a_task_run_started_before_the_verdict_lands_on_kiro_cli(
        self, monkeypatch, pinned_cli, kas_selectable
    ):
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.members import select_provider_backend
        from kiro_crew.session_allocation_parts import companions

        _help(monkeypatch, _OLD_HELP)
        cfg = _cfg()
        picked = []

        class _Stop(Exception):
            pass

        def _factory(key, **_kwargs):
            picked.append(
                select_provider_backend(key, cfg.agent.member_acp_backend, cfg.agent.acp_backend)
            )
            raise _Stop

        service: Any = SimpleNamespace(
            _owner=SimpleNamespace(_provider_factory=_factory),
            _subagent_runtime_locks={},
            _subagent_runtimes={},
        )

        async def _run():
            verdict = asyncio.get_running_loop().create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=False)
            spawn = asyncio.create_task(
                companions._get_or_bootstrap_run_runtime(service, "task-run:abc")
            )
            for _ in range(5):
                await asyncio.sleep(0)
            assert picked == [], "the task run reached the factory before the floor's verdict"
            bootstrap.apply_kas_cli_floor(cfg)
            verdict.set_result(None)
            with pytest.raises(_Stop):
                await spawn

        monkeypatch.setattr(kas_floor_wait, "_armed", None)
        asyncio.run(_run())
        assert picked == [KIRO]


class TestSharedBackgroundWaitsForTheFloor:
    """Background one-liners select the main backend after its floor verdict."""

    def test_shared_runtime_waits_before_backend_selection(
        self, monkeypatch, pinned_cli, kas_selectable
    ):
        from unittest.mock import AsyncMock, Mock

        from kiro_crew.acp import runtime
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        _help(monkeypatch, _OLD_HELP)
        monkeypatch.setattr(kas_floor_wait, "_armed", None)
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = KAS
        cfg.agent.member_acp_backend = KAS
        handle = object()
        shared = SimpleNamespace(
            pid=None,
            work_scratch_dir=None,
            acp_backend=KIRO,
            is_alive=lambda: True,
            spawn=AsyncMock(),
            create_session=AsyncMock(return_value=handle),
            kill=AsyncMock(),
        )
        constructor = Mock(return_value=shared)
        monkeypatch.setattr(runtime, "AcpRuntime", constructor)

        async def _run():
            manager = SessionManager(cfg)
            select_backend = Mock(wraps=manager._bg_backend_supports_runtime)
            monkeypatch.setattr(manager, "_bg_backend_supports_runtime", select_backend)
            verdict = asyncio.get_running_loop().create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=False)
            spawn = asyncio.create_task(manager.get_bg_session())
            try:
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not spawn.done(), "the background call bypassed the main floor"
                select_backend.assert_not_called()
                constructor.assert_not_called()
                shared.spawn.assert_not_awaited()
                assert not manager._bg_runtime_lock.locked()
                bootstrap.apply_kas_cli_floor(cfg)
                verdict.set_result(None)
                assert await asyncio.wait_for(spawn, timeout=30) is handle
                select_backend.assert_called_once_with()
                assert constructor.call_args.kwargs["acp_backend"] == KIRO
                shared.spawn.assert_awaited_once()
            finally:
                if not verdict.done():
                    verdict.set_result(None)
                await asyncio.wait_for(spawn, timeout=30)
                await asyncio.wait_for(manager.close_all(), timeout=30)

        asyncio.run(_run())


class TestOffLoopRunnerWaitsForTheFloor:
    """A runner on its own thread and event loop waits for the gateway's floor too."""

    def test_an_agent_runner_step_before_the_verdict_lands_on_kiro_cli(
        self, monkeypatch, pinned_cli, kas_selectable
    ):
        from kiro_crew.agent_sdk import kas_floor_wait
        from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
            SessionAgentRunner,
        )
        from kiro_crew.members import select_provider_backend

        _help(monkeypatch, _OLD_HELP)
        cfg = _cfg()
        picked = []
        results = []
        signals = {}

        def _signal(name):
            loop, fut = signals["loop"], signals[name]
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))

        class _Stop(Exception):
            pass

        def _resolve_factory():
            # What the runner's own config load does: resolve the unset backend,
            # then build a factory that keeps that answer.
            configured = registry.resolve_selected_backend(None)

            def _factory(key, **_kwargs):
                picked.append(select_provider_backend(key, "", configured))
                raise _Stop

            _signal("reached")
            return _factory

        real_wait = kas_floor_wait.await_kas_floor_verdict

        async def _entered(*, member=False):
            _signal("reached")
            await real_wait(member=member)

        monkeypatch.setattr(kas_floor_wait, "await_kas_floor_verdict", _entered)
        monkeypatch.setattr(kas_floor_wait, "_armed", None)
        runner = SessionAgentRunner()
        monkeypatch.setattr(runner, "_resolve_factory", _resolve_factory)

        def _step():
            try:
                results.append(runner.run("hello"))
            finally:
                _signal("finished")

        async def _run():
            loop = asyncio.get_running_loop()
            signals.update(loop=loop, reached=loop.create_future(), finished=loop.create_future())
            verdict = loop.create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=False)
            threading.Thread(target=_step, daemon=True).start()
            try:
                # Either the runner is now held at the gate, or it already loaded
                # its config: the factory's recorded backend below tells which.
                await asyncio.wait_for(signals["reached"], timeout=60)
                bootstrap.apply_kas_cli_floor(cfg, kas_defaulted=True)
            finally:
                verdict.set_result(None)
                await asyncio.wait_for(signals["finished"], timeout=60)

        asyncio.run(_run())
        assert picked == [KIRO]
        assert results and not results[0].ok

    def test_no_armed_gate_costs_the_runner_no_wait(self, monkeypatch):
        from kiro_crew.agent_sdk import kas_floor_wait

        monkeypatch.setattr(kas_floor_wait, "_armed", None)
        # Returns at once: nothing armed, so no loop is consulted.
        kas_floor_wait.wait_kas_floor_verdict_blocking()

    def test_a_caller_on_the_arming_loop_is_never_blocked(self, monkeypatch):
        from kiro_crew.agent_sdk import kas_floor_wait

        monkeypatch.setattr(kas_floor_wait, "_armed", None)

        async def _run():
            verdict = asyncio.get_running_loop().create_future()
            kas_floor_wait.arm_kas_floor_wait(verdict, main=True, member=False)
            try:
                # Blocking here would stall the verdict it waits for.
                kas_floor_wait.wait_kas_floor_verdict_blocking()
                assert not verdict.done()
            finally:
                verdict.set_result(None)

        asyncio.run(_run())
