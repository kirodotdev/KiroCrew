"""Rule 1b: an unset ``agent.kas_bundle`` in a packaged build moves KAS onto Crew's engine.

Only with Kiro Crew's own sign-in usable: kiro-cli's login is never copied, so a host
signed in only to kiro-cli keeps the relay and the dashboard asks for one sign-in. A
saved value always wins. A first switch that fails runs that chat on kiro-cli once; a
host the engine already ran on is fixed in place and never offered kiro-cli.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import kas_build_default, kas_route, runtime_spawn_entry
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.session_handle import AcpRuntimeDead, AcpRuntimeError
from kiro_crew.config import loader_document


@pytest.fixture
def packaged(monkeypatch, tmp_path):
    """A packaged build, nothing saved, the switch open, Crew signed in, a fresh data home."""
    state = {"vault": True, "declared": False, "preferred": True, "relay": True}
    monkeypatch.setattr(
        kas_build_default, "kas_bundle_preferred_by_build", lambda: state["preferred"]
    )
    monkeypatch.setattr(loader_document, "kas_bundle_declared", lambda: state["declared"])
    monkeypatch.setattr(
        kas_route, "automatic_switch_allowed", lambda pin="": pin != kas_route.STORED_BACKEND_KIRO
    )
    monkeypatch.setattr(kas_build_default, "_marker_path", lambda: tmp_path / "kas_engine_ran")
    monkeypatch.setattr(kas_route, "relay_can_run", lambda **_kw: state["relay"])

    def resolve(policy=None):
        source = kas_route.sign_in.Source.VAULT if state["vault"] else object()
        return SimpleNamespace(source=source)

    monkeypatch.setattr(kas_route.sign_in, "resolve", resolve)
    return state


class TestTheRule:
    def test_crew_signed_in_takes_the_engine(self, packaged):
        route = kas_route.resolve_kas_route(environ={})
        assert route.bundle and route.reason == kas_route.REASON_BUILD_DEFAULT
        assert route.automatic
        assert "this build's default" in route.describe()

    def test_crew_signed_out_keeps_the_rules_below(self, packaged):
        packaged["vault"] = False
        assert not kas_route.build_default_selects_bundle(environ={})

    def test_a_saved_value_turns_the_rule_off(self, packaged):
        packaged["declared"] = True
        assert not kas_route.build_default_selects_bundle(environ={})

    def test_a_source_checkout_does_not_prefer_the_engine(self, packaged):
        packaged["preferred"] = False
        assert not kas_route.build_default_selects_bundle(environ={})

    def test_a_kiro_cli_pick_stays_on_kiro_cli(self, packaged):
        assert not kas_route.build_default_selects_bundle(pin="kiro", environ={})

    def test_the_operator_binary_is_never_second_guessed(self, packaged):
        environ = {"KIROCREW_KIRO_BIN": "/opt/kiro-cli"}
        assert not kas_route.build_default_selects_bundle(environ=environ)

    def test_a_failed_first_switch_keeps_kiro_cli_for_the_cooldown(self, packaged, monkeypatch):
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock()
        clock.install(monkeypatch, kas_build_default)
        kas_build_default.note_first_switch_failed("node exited 1")
        assert not kas_route.build_default_selects_bundle(environ={})
        clock.advance(kas_build_default.FIRST_SWITCH_COOLDOWN_SECS + 1)
        assert kas_route.build_default_selects_bundle(environ={})

    def test_a_host_the_engine_ran_on_ignores_the_cooldown(self, packaged):
        kas_build_default.remember_engine_ran()
        kas_build_default.note_first_switch_failed("node exited 1")
        assert kas_route.build_default_selects_bundle(environ={})

    def test_a_host_the_engine_ran_on_keeps_it_when_crew_signs_out(self, packaged):
        """Fixed in place: the sign-in prompt shows, chats never move back to kiro-cli."""
        kas_build_default.remember_engine_ran()
        packaged["vault"] = False
        assert kas_route.build_default_selects_bundle(environ={})
        assert not kas_route.crew_sign_in_moves_to_bundle(environ={})

    def test_sign_in_surfaces_never_promise_kiro_cli_on_a_host_the_engine_ran_on(
        self, packaged, monkeypatch, tmp_path
    ):
        """No "Use the Kiro CLI account" offer when signing Crew out keeps the engine."""
        monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: "/usr/bin/kiro-cli")
        monkeypatch.setattr(kas_route, "relay_unusable_reason", lambda **_kw: "")
        assert kas_route.relay_runs_on_kiro_cli_sign_in(environ={}, home=tmp_path)
        kas_build_default.remember_engine_ran()
        assert not kas_route.relay_runs_on_kiro_cli_sign_in(environ={}, home=tmp_path)

    def test_a_failed_preview_start_keeps_the_relay_for_its_cooldown(self, packaged):
        """The preview's fallback respawn must not be put back on the failing engine."""
        kas_route.note_preview_bundle_failed("node exited 1")
        assert not kas_route.build_default_selects_bundle(environ={})


class TestTheSignInOncePrompt:
    def test_kiro_cli_signed_in_and_crew_not_asks_for_one_sign_in(self, packaged, monkeypatch):
        packaged["vault"] = False
        monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: "/usr/bin/kiro-cli")
        monkeypatch.setattr(kas_route, "relay_unusable_reason", lambda **_kw: "")
        monkeypatch.setattr(kas_route, "crew_entry_skips_launcher", lambda *_a: False)
        assert kas_route.crew_sign_in_moves_to_bundle(environ={})

    def test_crew_signed_in_needs_no_prompt(self, packaged):
        assert not kas_route.crew_sign_in_moves_to_bundle(environ={})

    def test_no_usable_kiro_cli_is_the_blocking_sign_in_instead(self, packaged, monkeypatch):
        """No kiro-cli: the chat cannot start, which ``chat_needs_sign_in`` says."""
        packaged["vault"] = False
        monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: None)
        assert not kas_route.crew_sign_in_moves_to_bundle(environ={})


def _ctx(tmp_path):
    return SpawnContext(agent="a", work_dir=str(tmp_path), model=None, environ={}, home=tmp_path)


class _RelayReached(Exception):
    """Raised where the relay route starts, so a test can see it was taken."""


class TestTheSpawnPlan:
    @pytest.mark.asyncio
    async def test_the_spawn_takes_the_engine(self, packaged, monkeypatch, tmp_path):
        from kiro_crew.acp import kas_bundle

        bundle = SimpleNamespace(
            version="0.66.104",
            node_version="22",
            node_bin=tmp_path / "n",
            acp_server=tmp_path / "s",
        )
        monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
        monkeypatch.setattr(kas_harness, "_chat_sharing_preview_on", lambda: False)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda: bundle)
        monkeypatch.setattr(kas_bundle, "_probe_node", lambda node_bin, version: None)
        harness = kas_harness.KasHarness()
        plan = await harness.resolve_spawn(_ctx(tmp_path))
        assert plan.spawn_label == kas_harness.KAS_BUNDLE_SPAWN_LABEL
        assert harness.spawn_route_reason == kas_route.REASON_BUILD_DEFAULT
        assert harness.engine_fallback_notice == ""

    @pytest.mark.asyncio
    async def test_a_first_switch_that_cannot_plan_runs_on_kiro_cli(
        self, packaged, monkeypatch, tmp_path
    ):
        async def no_engine(self, ctx, route, **_kw):
            raise AcpRuntimeError("the extracted Node runtime exited 126")

        async def relay_bin(**_kw):
            raise _RelayReached

        monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
        monkeypatch.setattr(kas_harness, "_chat_sharing_preview_on", lambda: False)
        monkeypatch.setattr(kas_harness.KasHarness, "_bundle_spawn_plan", no_engine)
        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", relay_bin)
        harness = kas_harness.KasHarness()
        with pytest.raises(_RelayReached):
            await harness.resolve_spawn(_ctx(tmp_path))
        assert harness.engine_fallback_notice == kas_build_default.ENGINE_FALLBACK_NOTICE
        assert kas_build_default.first_switch_cooling_down()

    @pytest.mark.asyncio
    async def test_a_later_spawn_kept_on_kiro_cli_by_the_cooldown_carries_the_notice(
        self, packaged, monkeypatch, tmp_path
    ):
        # The chat may land on a spawn after the one that failed (a pre-started
        # session, another chat): it runs on kiro-cli for the same reason.
        async def relay_bin(**_kw):
            raise _RelayReached

        monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
        monkeypatch.setattr(kas_harness, "_chat_sharing_preview_on", lambda: False)
        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", relay_bin)
        kas_build_default.note_first_switch_failed("node exited 1")
        harness = kas_harness.KasHarness()
        with pytest.raises(_RelayReached):
            await harness.resolve_spawn(_ctx(tmp_path))
        assert harness.engine_fallback_notice == kas_build_default.ENGINE_FALLBACK_NOTICE

    @pytest.mark.asyncio
    async def test_a_relay_spawn_without_a_failed_first_switch_has_no_notice(
        self, packaged, monkeypatch, tmp_path
    ):
        async def relay_bin(**_kw):
            raise _RelayReached

        monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
        monkeypatch.setattr(kas_harness, "_chat_sharing_preview_on", lambda: False)
        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", relay_bin)
        monkeypatch.setattr(kas_route, "_crew_signed_in", lambda: False)
        harness = kas_harness.KasHarness()
        with pytest.raises(_RelayReached):
            await harness.resolve_spawn(_ctx(tmp_path))
        assert harness.engine_fallback_notice == ""

    @pytest.mark.asyncio
    async def test_a_host_the_engine_ran_on_is_never_moved_to_kiro_cli(
        self, packaged, monkeypatch, tmp_path
    ):
        async def no_engine(self, ctx, route, **_kw):
            raise AcpRuntimeError("the extracted Node runtime exited 126")

        kas_build_default.remember_engine_ran()
        monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
        monkeypatch.setattr(kas_harness, "_chat_sharing_preview_on", lambda: False)
        monkeypatch.setattr(kas_harness.KasHarness, "_bundle_spawn_plan", no_engine)
        with pytest.raises(AcpRuntimeError, match="exited 126"):
            await kas_harness.KasHarness().resolve_spawn(_ctx(tmp_path))


class _Runtime:
    """The runtime fields the start path reads, with a scripted start."""

    def __init__(self, reason, *, fail_times=0, bundle=True):
        self._harness = SimpleNamespace(
            spawn_route_reason=reason,
            bundle_engine_version="0.66.104" if bundle else "",
            engine_fallback_notice="",
        )
        self._remote_workspace = None
        self._kas_extract_broken_message = ""
        self.fail_times = fail_times
        self.starts = 0
        self.revived = 0

    def _broken_kas_server_path(self):
        return None

    async def _revive_after_failed_start(self):
        self.revived += 1
        return True

    async def _spawn_admitted_rederiving_once(self):
        self.starts += 1
        # A respawn after the first-switch fallback takes kiro-cli's relay.
        self._harness.spawn_route = kas_route.KasRoute(bundle=self.starts == 1)
        if self.starts <= self.fail_times:
            raise AcpRuntimeDead("node exited during initialize")


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []

    async def _wait(secs):
        slept.append(secs)

    monkeypatch.setattr(runtime_spawn_entry, "_retry_wait", _wait)
    return slept


class TestTheStartPath:
    @pytest.mark.asyncio
    async def test_a_first_switch_that_dies_runs_this_chat_on_kiro_cli(self, packaged, no_sleep):
        rt = _Runtime(kas_route.REASON_BUILD_DEFAULT, fail_times=1)
        await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
        assert rt.starts == 2 and rt.revived == 1 and no_sleep == []
        assert rt._harness.engine_fallback_notice == kas_build_default.ENGINE_FALLBACK_NOTICE
        assert kas_build_default.first_switch_cooling_down()

    @pytest.mark.asyncio
    async def test_a_host_with_no_usable_relay_is_fixed_in_place(self, packaged, no_sleep):
        """No kiro-cli to fall back to: retried in place, never told it uses Kiro CLI."""
        packaged["relay"] = False
        rt = _Runtime(kas_route.REASON_BUILD_DEFAULT, fail_times=1)
        await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
        assert rt.starts == 2 and no_sleep == [runtime_spawn_entry.ENGINE_RETRY_DELAY_SECS]
        assert rt._harness.engine_fallback_notice == ""
        assert not kas_build_default.first_switch_cooling_down()

    @pytest.mark.asyncio
    async def test_a_host_the_engine_ran_on_retries_in_place_then_gives_up_plainly(
        self, packaged, no_sleep
    ):
        kas_build_default.remember_engine_ran()
        rt = _Runtime(kas_route.REASON_BUILD_DEFAULT, fail_times=99)
        with pytest.raises(AcpRuntimeError) as err:
            await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
        assert str(err.value) == runtime_spawn_entry.ENGINE_DID_NOT_START
        assert rt.starts == runtime_spawn_entry.ENGINE_RETRIES + 1
        assert no_sleep == [runtime_spawn_entry.ENGINE_RETRY_DELAY_SECS] * 2
        assert rt._harness.engine_fallback_notice == ""

    @pytest.mark.asyncio
    async def test_a_retry_that_starts_records_the_engine_ran(self, packaged, no_sleep):
        rt = _Runtime(kas_route.REASON_CONFIGURED, fail_times=1)
        await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
        assert rt.starts == 2 and no_sleep == [runtime_spawn_entry.ENGINE_RETRY_DELAY_SECS]
        assert kas_build_default.engine_has_run()

    @pytest.mark.asyncio
    async def test_a_relay_start_is_not_retried(self, packaged, no_sleep):
        rt = _Runtime("", fail_times=1, bundle=False)
        with pytest.raises(AcpRuntimeDead):
            await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
        assert rt.starts == 1 and not kas_build_default.engine_has_run()

    @pytest.mark.asyncio
    async def test_a_first_start_that_works_marks_the_host(self, packaged, no_sleep):
        rt = _Runtime(kas_route.REASON_BUILD_DEFAULT)
        await runtime_spawn_entry._spawn_admitted_repairing_kas_extract(rt)
        assert kas_build_default.engine_has_run()
