"""Choosing KAS runs Kiro Crew's own engine when kiro-cli's relay cannot run.

Pins the route rule in ``acp/kas_route.py`` and the harness's use of it: the
automatic switch to the bundle route (no kiro-cli, or a kiro-cli with no sign-in
Crew's sign-in cannot stand in for), the stored Kiro CLI pick that never leaves
kiro-cli, the switch being logged and published, and the gateway's one-at-a-time
engine install with its sandbox refusal and retry text. The decision is the
``docs/decisions/`` entry "Choosing KAS runs Kiro Crew's own engine and sign-in".
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.acp import kas_bundle, kas_engine_install, kas_route
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.kas import KAS_BUNDLE_SPAWN_LABEL, KasHarness
from kiro_crew.acp.kas_transport import build_kas_argv, build_kas_bundle_argv
from kiro_crew.acp.kiro_cli_missing import AcpKiroCliMissing
from kiro_crew.acp.session_handle import AcpRuntimeError

#: The real rule-2 predicate, read at import: the conftest floor replaces the
#: module attribute for every test, so a test of the rule itself calls this.
_REAL_SWITCH_ALLOWED = kas_route.automatic_switch_allowed


def _ctx(tmp_path: Path, environ: dict[str, str] | None = None) -> SpawnContext:
    return SpawnContext(
        agent="kirocrew", work_dir=tmp_path, model=None, environ=environ or {}, home=tmp_path
    )


def _installed(tmp_path: Path) -> kas_bundle.InstalledKasBundle:
    return kas_bundle.InstalledKasBundle(
        version=kas_bundle.KAS_PIN_VERSION,
        node_version="22.23.3",
        root=tmp_path,
        node_bin=tmp_path / "node" / "bin" / "node",
        acp_server=tmp_path / "bundle" / "acp-server.js",
    )


@pytest.fixture
def switch_open(monkeypatch):
    """Open the automatic switch the conftest floor keeps closed."""
    monkeypatch.setattr(kas_route, "automatic_switch_allowed", lambda: True)


# ── the rule ───────────────────────────────────────────────────────────────────


class TestRelayUnusableReason:
    def test_no_kiro_cli_is_the_bundle_route(self, tmp_path):
        reason = kas_route.relay_unusable_reason(
            kiro_bin=None,
            environ={},
            home=tmp_path,
            vault_identity=True,
            crew_entry_skips_launcher=True,
        )
        assert reason == kas_route.REASON_NO_KIRO_CLI

    def test_a_vault_identity_through_kiro_cli_chat_keeps_the_relay(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            kas_route,
            "kiro_cli_signed_out",
            lambda **k: pytest.fail("kiro-cli's own sign-in is irrelevant here"),
        )
        reason = kas_route.relay_unusable_reason(
            kiro_bin="/opt/kiro/kiro-cli",
            environ={},
            home=tmp_path,
            vault_identity=True,
            crew_entry_skips_launcher=True,
        )
        assert reason == ""

    @pytest.mark.parametrize(
        ("vault_identity", "skips"),
        [(False, True), (False, False), (True, False)],
        ids=["no-vault", "no-vault-launcher", "vault-but-launcher"],
    )
    def test_a_proven_signed_out_kiro_cli_is_the_bundle_route(
        self, tmp_path, monkeypatch, vault_identity, skips
    ):
        monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **k: True)
        reason = kas_route.relay_unusable_reason(
            kiro_bin="/opt/kiro/kiro-cli",
            environ={},
            home=tmp_path,
            vault_identity=vault_identity,
            crew_entry_skips_launcher=skips,
        )
        assert reason == kas_route.REASON_KIRO_CLI_SIGNED_OUT

    def test_an_operator_kiro_bin_is_never_second_guessed(self, tmp_path, monkeypatch):
        """``KIROCREW_KIRO_BIN`` names a binary that may sign in some other way (a
        wrapper, the E2E fake backend), so an empty kiro-cli store moves nothing."""
        monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **k: True)
        reason = kas_route.relay_unusable_reason(
            kiro_bin="/opt/fake/acp-backend",
            environ={"KIROCREW_KIRO_BIN": "/opt/fake/acp-backend"},
            home=tmp_path,
            vault_identity=False,
            crew_entry_skips_launcher=False,
        )
        assert reason == ""

    def test_an_unproven_sign_out_keeps_the_relay(self, tmp_path, monkeypatch):
        monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **k: False)
        reason = kas_route.relay_unusable_reason(
            kiro_bin="/opt/kiro/kiro-cli",
            environ={},
            home=tmp_path,
            vault_identity=False,
            crew_entry_skips_launcher=False,
        )
        assert reason == ""


class TestKiroCliSignedOut:
    def test_an_api_key_is_a_kiro_cli_sign_in(self, tmp_path):
        assert kas_route.kiro_cli_signed_out(environ={"KIRO_API_KEY": "k"}, home=tmp_path) is False

    @pytest.mark.parametrize(
        ("fingerprint", "definitive", "expected"),
        [("", True, True), ("", False, False), ("abc", True, False)],
        ids=["proven-empty", "unreadable", "signed-in"],
    )
    def test_only_a_definitive_empty_store_is_signed_out(
        self, tmp_path, monkeypatch, fingerprint, definitive, expected
    ):
        def fake_fingerprint(path, *, definitive=None):
            definitive.append(definitive_value)
            return fingerprint

        definitive_value = definitive
        monkeypatch.setattr(
            "kiro_crew.config.loader.read_env_file_credential", lambda *a, **k: None
        )
        monkeypatch.setattr("kiro_crew.kiro_prerequisite.identity_fingerprint", fake_fingerprint)
        assert kas_route.kiro_cli_signed_out(environ={}, home=tmp_path) is expected

    def test_a_relocated_store_proves_nothing(self, tmp_path, monkeypatch):
        """The fixed anchor is not the store a relocated kiro-cli signs in to."""
        monkeypatch.setattr(
            "kiro_crew.config.loader.read_env_file_credential", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite.identity_store_is_relocated", lambda *a: True
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite.identity_fingerprint",
            lambda *a, **k: pytest.fail("read the fixed anchor of a relocated store"),
        )
        assert kas_route.kiro_cli_signed_out(environ={}, home=tmp_path) is False

    def test_a_failed_read_proves_nothing(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise OSError("store unreadable")

        monkeypatch.setattr(
            "kiro_crew.config.loader.read_env_file_credential", lambda *a, **k: None
        )
        monkeypatch.setattr("kiro_crew.kiro_prerequisite.identity_fingerprint", boom)
        assert kas_route.kiro_cli_signed_out(environ={}, home=tmp_path) is False


class TestRuleTwoIsGlobalOnly:
    """Rule 2 reads the installation's ``agent.acp_backend`` alone, never a mate's
    own pin. The per-pin route is tracked at :attr:`TRACKED_BY`; this pins today's
    behaviour so a change to it is a deliberate one."""

    TRACKED_BY = "https://github.com/kirodotdev/KiroCrew/issues/26629"

    def test_the_switch_follows_the_stored_global_pick(self, monkeypatch):
        monkeypatch.setattr(kas_route, "_stored_as_kiro_pick", lambda: True)
        assert _REAL_SWITCH_ALLOWED() is False
        monkeypatch.setattr(kas_route, "_stored_as_kiro_pick", lambda: False)
        assert _REAL_SWITCH_ALLOWED() is True

    def test_it_takes_no_pin(self):
        import inspect

        assert list(inspect.signature(_REAL_SWITCH_ALLOWED).parameters) == []


class TestCrewEntrySkipsLauncher:
    def test_the_bundled_chat_entry_already_skips_the_launcher(self, monkeypatch):
        """The desktop runs ``kiro-cli-chat`` itself, which has no sibling to swap
        in; a vault sign-in still reaches the engine through it."""
        monkeypatch.setattr(kas_route.sys, "platform", "linux")
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.chat_sibling", lambda *a, **k: pytest.fail("no swap needed")
        )
        assert kas_route.crew_entry_skips_launcher("/app/kiro-cli/kiro-cli-chat", {}) is True

    def test_a_launcher_without_a_sibling_does_not(self, monkeypatch):
        monkeypatch.setattr(kas_route.sys, "platform", "linux")
        monkeypatch.setattr("kiro_crew.kiro_cli.chat_sibling", lambda *a, **k: None)
        assert kas_route.crew_entry_skips_launcher("/opt/kiro/kiro-cli", {}) is False


class TestRouteText:
    def test_the_automatic_route_says_why(self):
        route = kas_route.KasRoute(bundle=True, reason=kas_route.REASON_KIRO_CLI_SIGNED_OUT)
        assert route.automatic is True
        assert route.describe() == (
            "KAS runs on Kiro Crew's own engine because kiro-cli is not signed in"
        )

    def test_the_configured_route_is_not_automatic(self):
        route = kas_route.KasRoute(bundle=True, reason=kas_route.REASON_CONFIGURED)
        assert route.automatic is False
        assert "agent.kas_bundle is on" in route.describe()

    def test_the_relay(self):
        assert kas_route.RELAY.name == "relay"
        assert kas_route.RELAY.describe() == "KAS runs through kiro-cli's relay"


class TestTheSwitchIsLogged:
    def test_each_change_of_route_is_logged_once(self, caplog):
        auto = kas_route.KasRoute(bundle=True, reason=kas_route.REASON_NO_KIRO_CLI)
        with caplog.at_level(logging.INFO, logger=kas_route.__name__):
            kas_route.note_spawn_route(auto)
            kas_route.note_spawn_route(auto)
            kas_route.note_spawn_route(kas_route.RELAY)
        records = [r for r in caplog.records if r.name == kas_route.__name__]
        assert [r.levelno for r in records] == [logging.WARNING, logging.INFO]
        assert "kiro-cli is not installed" in records[0].getMessage()
        assert kas_route.last_spawn_route() == kas_route.RELAY


# ── the harness ────────────────────────────────────────────────────────────────


async def _no_bin(*, environ=None, home=None):
    return None


async def _a_bin(*, environ=None, home=None):
    return "/opt/kiro/kiro-cli"


class TestHarnessRoute:
    @pytest.mark.asyncio
    async def test_no_kiro_cli_spawns_crews_own_engine(self, tmp_path, switch_open):
        installed = _installed(tmp_path)
        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _no_bin),
            patch.object(kas_bundle, "installed_bundle", return_value=installed),
        ):
            plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
        assert plan.argv == build_kas_bundle_argv(
            str(installed.node_bin), str(installed.acp_server)
        )
        assert plan.host_auth is True
        assert plan.spawn_label == KAS_BUNDLE_SPAWN_LABEL
        assert kas_route.last_spawn_route() == kas_route.KasRoute(
            bundle=True, reason=kas_route.REASON_NO_KIRO_CLI
        )

    @pytest.mark.asyncio
    async def test_a_signed_out_launcher_with_a_vault_sign_in_spawns_crews_own_engine(
        self, tmp_path, switch_open, monkeypatch
    ):
        """The toolbox layout: kiro-cli with no ``kiro-cli-chat`` beside it, signed out,
        and the user signed in through Kiro Crew. The launcher would refuse."""
        installed = _installed(tmp_path)
        monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **k: True)
        monkeypatch.setattr(kas_route, "crew_entry_skips_launcher", lambda *a: False)
        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _a_bin),
            patch("kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", return_value=True),
            patch.object(kas_bundle, "installed_bundle", return_value=installed),
        ):
            plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
        assert plan.argv[0] == str(installed.node_bin)
        assert kas_route.last_spawn_route().reason == kas_route.REASON_KIRO_CLI_SIGNED_OUT

    @pytest.mark.asyncio
    async def test_a_working_kiro_cli_keeps_the_relay(self, tmp_path, switch_open, monkeypatch):
        monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **k: False)
        monkeypatch.setattr(kas_route, "crew_entry_skips_launcher", lambda *a: False)
        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _a_bin),
            patch("kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", return_value=False),
            patch("kiro_crew.acp.harness.kas.installed_kiro_cli_version", return_value=None),
            patch.object(
                kas_bundle,
                "installed_bundle",
                side_effect=AssertionError("the bundle is not consulted on the relay"),
            ),
        ):
            plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
        assert plan.argv == build_kas_argv("/opt/kiro/kiro-cli")
        assert kas_route.last_spawn_route() == kas_route.RELAY

    @pytest.mark.asyncio
    async def test_a_stored_kiro_cli_pick_never_leaves_kiro_cli(self, tmp_path, monkeypatch):
        """Rule 2: a Kiro CLI pick runs on KAS only because kiro-cli offers v3, so a
        missing kiro-cli stays kiro-cli's own error, not a download."""
        monkeypatch.setattr(kas_route, "_stored_as_kiro_pick", lambda: True)
        monkeypatch.setattr(
            kas_route, "automatic_switch_allowed", lambda: not kas_route._stored_as_kiro_pick()
        )
        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _no_bin),
            patch.object(
                kas_bundle,
                "installed_bundle",
                side_effect=AssertionError("a Kiro CLI pick never takes the bundle"),
            ),
        ):
            with pytest.raises(AcpKiroCliMissing):
                await KasHarness().resolve_spawn(_ctx(tmp_path))

    @pytest.mark.asyncio
    async def test_an_operator_launcher_names_kiro_cli_login(
        self, tmp_path, switch_open, monkeypatch
    ):
        """``KIROCREW_KIRO_BIN`` keeps the relay through the launcher, which only
        kiro-cli's own sign-in starts, so the messages name ``kiro-cli login``."""
        from kiro_crew.agent_sdk import host_auth

        environ = {"KIROCREW_KIRO_BIN": "/opt/kiro/kiro-cli", "PATH": "/usr/bin"}
        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _a_bin),
            patch("kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", return_value=False),
            patch("kiro_crew.acp.harness.kas.installed_kiro_cli_version", return_value=None),
        ):
            plan = await KasHarness().resolve_spawn(_ctx(tmp_path, environ))
        assert plan.argv[0] == "/opt/kiro/kiro-cli"
        assert kas_route.last_spawn_route().reason == kas_route.REASON_KIRO_CLI_LAUNCHER
        text = host_auth.signed_out_message("kas", {"PATH": "/usr/bin"}, "linux")
        assert "`kiro-cli login`" in text
        assert "Kiro sign-in" not in text

    @pytest.mark.asyncio
    async def test_an_automatic_route_installs_the_missing_engine(self, tmp_path, switch_open):
        installed = _installed(tmp_path)
        calls = []

        def fake_install(base_url, *, on_progress=None, root=None):
            calls.append(base_url)
            on_progress("KAS engine", 5, 10)
            return installed

        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _no_bin),
            patch.object(kas_bundle, "installed_bundle", return_value=None),
            patch.object(kas_bundle, "install_pinned", side_effect=fake_install),
        ):
            plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
        assert len(calls) == 1
        assert plan.argv[0] == str(installed.node_bin)
        snapshot = kas_engine_install.install_snapshot()
        assert snapshot["running"] is False
        assert (snapshot["done"], snapshot["total"]) == (5, 10)
        assert snapshot["error"] == ""

    @pytest.mark.asyncio
    async def test_an_offline_install_names_the_route_and_the_retry(self, tmp_path, switch_open):
        def offline(*a, **k):
            raise kas_bundle.KasBundleError("could not fetch https://h.test: network unreachable")

        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", _no_bin),
            patch.object(kas_bundle, "installed_bundle", return_value=None),
            patch.object(kas_bundle, "install_pinned", side_effect=offline),
        ):
            with pytest.raises(AcpRuntimeError) as caught:
                await KasHarness().resolve_spawn(_ctx(tmp_path))
        text = str(caught.value)
        assert text.startswith(
            "KAS runs on Kiro Crew's own engine because kiro-cli is not installed."
        )
        assert "network unreachable" in text
        assert "`kirocrew kas install`" in text
        assert "kiro-cli login" not in text
        assert kas_engine_install.install_snapshot()["error"]


# ── the installer ──────────────────────────────────────────────────────────────


class TestEngineInstaller:
    def test_two_callers_share_one_install(self, tmp_path, monkeypatch):
        installed = _installed(tmp_path)
        state = {"present": None}
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fake_install(base_url, *, on_progress=None, root=None):
            calls.append(base_url)
            started.set()
            assert release.wait(5)
            state["present"] = installed
            return installed

        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: state["present"])
        monkeypatch.setattr(kas_bundle, "install_pinned", fake_install)
        results = []

        def run(caller):
            results.append(
                kas_engine_install.install_pinned_engine("https://h.test", caller=caller)
            )

        first = threading.Thread(target=run, args=("spawn",))
        first.start()
        assert started.wait(5)
        assert kas_engine_install.install_snapshot()["running"] is True
        second = threading.Thread(target=run, args=("dashboard",))
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert calls == ["https://h.test"]
        assert results == [installed, installed]

    def test_inside_an_agent_sandbox_it_refuses_with_the_command(self, monkeypatch):
        from kiro_crew.sandbox_launch_mark import SANDBOX_MARKER

        monkeypatch.setenv(SANDBOX_MARKER, "1")
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
        monkeypatch.setattr(
            kas_bundle, "install_pinned", lambda *a, **k: pytest.fail("installed in a sandbox")
        )
        with pytest.raises(kas_bundle.KasBundleError, match="kirocrew kas install"):
            kas_engine_install.install_pinned_engine("https://h.test", caller="spawn")

    def test_an_overridden_version_is_never_swapped_for_the_pin(self, monkeypatch):
        """Only the release's pin installs unattended; a version picked with the
        override that is not on disk is refused with its own install command."""
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
        monkeypatch.setattr(kas_bundle, "pinned_version", lambda: "0.99.0")
        monkeypatch.setattr(
            kas_bundle, "install_pinned", lambda *a, **k: pytest.fail("installed the pin instead")
        )
        with pytest.raises(kas_bundle.KasBundleError, match="--version 0.99.0 --unpinned"):
            kas_engine_install.install_pinned_engine("https://h.test", caller="spawn")

    def test_an_installed_engine_installs_nothing(self, tmp_path, monkeypatch):
        installed = _installed(tmp_path)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: installed)
        monkeypatch.setattr(
            kas_bundle, "install_pinned", lambda *a, **k: pytest.fail("reinstalled")
        )
        assert (
            kas_engine_install.install_pinned_engine("https://h.test", caller="spawn") == installed
        )


# ── the shared predicate ───────────────────────────────────────────────────────


class TestSharedPredicate:
    def test_the_automatic_route_is_the_bundle_route_for_every_reader(
        self, monkeypatch, switch_open
    ):
        """doctor, diagnostics and the install probe read one predicate, so they all
        see the switch."""
        from types import SimpleNamespace

        from kiro_crew.agent_sdk import kas_bundle as facade

        agent = SimpleNamespace(acp_backend="kas", kas_bundle=False)
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            classmethod(lambda cls: SimpleNamespace(agent=agent)),
        )
        monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin", lambda **k: None)
        assert facade.kas_route() == kas_route.KasRoute(
            bundle=True, reason=kas_route.REASON_NO_KIRO_CLI
        )
        assert facade.kas_bundle_route() is True
        agent.acp_backend = "claude"
        assert facade.kas_bundle_route() is False
