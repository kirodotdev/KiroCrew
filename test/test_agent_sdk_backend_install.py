"""The ACP backend install probe: what the dashboard is told about this machine.

The probe exists because ``selectable`` is a build/policy fact and cannot say
whether the harness would actually start. Four properties carry that, and each
is a way the surface has a specific way of lying to an operator:

1. The probe answers through the SPAWN's resolvers. Every case here monkeypatches
   ``kiro_crew.acp.client``'s own resolvers, so a probe that grew a private PATH
   search would stop responding to these tests -- which is the point: a second
   search agrees with the spawn only by coincidence.
2. ``kas`` tracks ``kiro`` exactly, because KAS is served by kiro-cli's ACP relay
   and has no binary of its own. A drifting kas verdict would report a harness
   absent (or present) on the strength of a binary nobody looks for.
3. A resolver that RAISES yields ``unknown``, never ``missing``. Collapsing the
   two tells someone to run a global npm install for something they may already
   have.
4. Claude names WHICH component is absent. The adapter and the Claude CLI have
   different remedies, so a bare "missing" leaves the operator reinstalling the
   half they already have.

Plus the cache (the dashboard polls this and the Claude probe spawns mise) and
the endpoint's owner gate.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELF_SERVED_ACP,
)
from kiro_crew.agent_sdk import backend_cards
from kiro_crew.agent_sdk import backend_install as probe
from kiro_crew.agent_sdk import host_auth

#: The stand-in resolution for a self-served harness that is meant to be present.
#: One value for all of them: what these cases assert is the VERDICT the probe
#: derives, and the particular path it derives it from is not part of that.
_PRESENT = ("/usr/local/bin/harness", "/usr/bin")

#: Its absent counterpart. Named so a case reads as the state it is setting up.
_ABSENT = (None, "/usr/bin")


@pytest.fixture(autouse=True)
def _clean_cache():
    """Verdicts are cached in a module global, so one test must not seed another.

    Cleared on both sides: a test that populates the cache and fails partway
    would otherwise hand its verdict to the next one, which then passes for the
    wrong reason.
    """
    probe.clear_probe_cache()
    yield
    probe.clear_probe_cache()


def _stub_resolvers(
    monkeypatch,
    *,
    kiro="/usr/local/bin/kiro-cli",
    adapter=(["node", "/n/acp.js"], "/usr/bin"),
    claude_cli="/usr/local/bin/claude",
    codex=(None, "/usr/bin"),
    pi_acp=(["node", "/n/pi-acp.js"], "/usr/bin"),
    pi_cli=("/usr/local/bin/pi", "/usr/bin"),
    codex_acp=(["node", "/n/codex-acp.js"], "/usr/bin"),
    self_served=None,
):
    """Patch the spawn resolvers on the module the driver imports from.

    Patched on ``kiro_crew.acp.client`` -- the DEFINING module -- because the
    driver imports them function-locally at call time, so that is the namespace
    the lookup actually reaches. A value of ``None`` (or ``(None, path)``) is the
    resolvers' own "not found" answer, not an error.
    """
    from kiro_crew.acp import client

    monkeypatch.setattr(client, "_resolve_kiro_bin", lambda **_kw: kiro)
    monkeypatch.setattr(client, "_resolve_claude_acp_bin", lambda: adapter)
    monkeypatch.setattr(client, "_resolve_claude_code_executable", lambda: claude_cli)
    # pi's two halves: both are installed on the recording host, so a payload
    # assertion that reached the real resolver would read ``installed`` here and
    # ``missing`` in CI.
    monkeypatch.setattr(client, "_resolve_pi_acp_bin", lambda: pi_acp)
    monkeypatch.setattr(client, "_resolve_pi_bin", lambda: pi_cli)
    # codex, stubbed for the same reason as the three above and missed when they were
    # added: the payload assertion below pins its row as ``missing``, so on a host that
    # HAS the codex adapter installed the real resolver answers ``installed`` and the
    # test fails for a property of the machine rather than of the code.
    monkeypatch.setattr(client, "_resolve_codex_acp_bin", lambda: codex_acp)
    # The self-served harnesses, stubbed through the ONE resolver they share. The
    # default answers every MEMBER of the launch table rather than naming harnesses,
    # so onboarding one needs no edit here -- and every member needs an answer for
    # the same reason pi does: each is installed on some recording host, so a payload
    # assertion reaching the real resolver would read ``installed`` there and
    # ``missing`` in CI. The shared cache is cleared too: the resolution consults it
    # first, and a sibling test may have filled it.
    answers = {backend: _PRESENT for backend in ACP_BACKENDS_SELF_SERVED_ACP}
    answers.update(self_served or {})
    monkeypatch.setattr(client, "_resolve_self_served_bin", lambda backend: answers[backend])
    monkeypatch.setattr(client, "_self_served_bin_caches", {})


# ── The opencode driver seams ──


class TestSelfServedDriverSeams:
    """One harness, one component: the seam answers presence and nothing else.

    Parameterized over every member of ``ACP_BACKEND_LAUNCH`` rather than written per
    harness, because the seam is one function: a harness of this shape that answered
    differently would be a defect in the record, not in a function of its own.
    """

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_a_resolved_binary_is_a_yes(self, backend, monkeypatch):
        from kiro_crew.agent_sdk.drivers import acp as driver

        _stub_resolvers(monkeypatch, self_served={backend: _PRESENT})
        assert driver.self_served_resolves(backend) is True

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_an_absent_binary_is_a_no(self, backend, monkeypatch):
        from kiro_crew.agent_sdk.drivers import acp as driver

        _stub_resolvers(monkeypatch, self_served={backend: _ABSENT})
        assert driver.self_served_resolves(backend) is False

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_the_install_command_is_the_records_own(self, backend):
        """Read from the record, so the advice cannot drift from the binary searched for.

        Compared against the record rather than a literal: a literal here would pin
        the WORDING of operator advice, when the contract is that the two agree.
        """
        from kiro_crew.agent_sdk.backends import launch_for
        from kiro_crew.agent_sdk.drivers import acp as driver

        assert driver.self_served_install_command(backend) == launch_for(backend).install_command
        assert launch_for(backend).install_command


class TestSelfServedCachedNegative:
    """The cases the adapter seams honour, on the ONE cache these harnesses share.

    An ABSENT key is the "not looked at yet" state the adapters spell with a
    sentinel, and it must not read as a negative: a probe that answered
    ``restart_required`` for a harness this process never resolved would tell an
    operator to restart for nothing.
    """

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_an_absent_key_is_not_a_negative(self, backend, monkeypatch):
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_self_served_bin_caches", {})
        assert driver.self_served_cached_negative(backend) is False

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_a_cached_absence_is_a_negative(self, backend, monkeypatch):
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_self_served_bin_caches", {backend: (None, "/usr/bin")})
        assert driver.self_served_cached_negative(backend) is True

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_a_cached_path_is_not_a_negative(self, backend, monkeypatch):
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(
            client, "_self_served_bin_caches", {backend: ("/opt/harness", "/usr/bin")}
        )
        assert driver.self_served_cached_negative(backend) is False

    def test_one_harnesss_absence_is_not_anothers(self, monkeypatch):
        """The cache is shared, so a miss must stay keyed to the harness that missed."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(
            client,
            "_self_served_bin_caches",
            {ACP_BACKEND_OPENCODE: (None, "/usr/bin")},
        )
        assert driver.self_served_cached_negative(ACP_BACKEND_OPENCODE) is True
        assert driver.self_served_cached_negative(ACP_BACKEND_GOOSE) is False
        assert driver.self_served_cached_negative(ACP_BACKEND_DEEPSEEK) is False

    def test_an_unparseable_cache_fails_safe(self, monkeypatch):
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_self_served_bin_caches", {ACP_BACKEND_OPENCODE: 42})
        assert driver.self_served_cached_negative(ACP_BACKEND_OPENCODE) is False

    def test_a_non_mapping_cache_fails_safe(self, monkeypatch):
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_self_served_bin_caches", 42)
        assert driver.self_served_cached_negative(ACP_BACKEND_OPENCODE) is False

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_installed_after_a_cached_miss_reports_restart_required(self, backend, monkeypatch):
        """The divergence the flag exists for: on disk now, absent in this process."""
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch, self_served={backend: _PRESENT})
        monkeypatch.setattr(client, "_self_served_bin_caches", {backend: (None, "/usr/bin")})
        state = probe.probe_backend(backend)
        assert state.installed == probe.INSTALLED
        assert state.restart_required is True


class TestSelfServedVerdicts:
    """The probe says installed, or names the one thing to install."""

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_a_resolved_binary_is_installed_and_names_nothing(self, backend, monkeypatch):
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch, self_served={backend: _PRESENT})
        monkeypatch.setattr(client, "_self_served_bin_caches", {})
        state = probe.probe_backend(backend)
        assert state.installed == probe.INSTALLED
        assert state.missing_components == ()
        assert state.install_command == ""
        assert state.restart_required is False
        assert state.policy_id == backend

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_an_absent_binary_names_the_component_and_the_command(self, backend, monkeypatch):
        """The component is the harness's own binary, which is what must be installed.

        The live case for reading it from the record rather than restating it: one of
        these harnesses ships its ACP support as a PLUGIN, so naming the plugin here
        would send an operator to install something that still would not run.
        """
        from kiro_crew.agent_sdk.backends import launch_for
        from kiro_crew.agent_sdk.drivers import acp as driver

        _stub_resolvers(monkeypatch, self_served={backend: _ABSENT})
        state = probe.probe_backend(backend)
        assert state.installed == probe.MISSING
        assert state.missing_components == (launch_for(backend).binary,)
        assert state.install_command == driver.self_served_install_command(backend)


class TestPiVerdicts:
    """Two components, one installer: the probe names whichever half is absent."""

    def test_both_resolved_is_installed_and_names_nothing(self, monkeypatch):
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch)
        monkeypatch.setattr(client, "_pi_acp_argv_cache", client._UNRESOLVED)
        monkeypatch.setattr(client, "_pi_bin_cache", client._UNRESOLVED)
        state = probe.probe_backend(ACP_BACKEND_PI)
        assert state.installed == probe.INSTALLED
        assert state.missing_components == ()
        assert state.install_command == ""
        assert state.restart_required is False
        assert state.policy_id == "pi"

    def test_an_absent_adapter_names_the_adapter(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import acp as driver

        _stub_resolvers(monkeypatch, pi_acp=(None, "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_PI)
        assert state.installed == probe.MISSING
        assert state.missing_components == (probe.COMPONENT_PI_ACP_ADAPTER,)
        assert state.install_command == driver.pi_install_command()

    def test_an_absent_agent_names_the_agent(self, monkeypatch):
        """The half the adapter would spawn, missing on its own: a distinct verdict."""
        _stub_resolvers(monkeypatch, pi_cli=(None, "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_PI)
        assert state.installed == probe.MISSING
        assert state.missing_components == (probe.COMPONENT_PI_CLI,)
        assert state.install_command.startswith("npm i -g ")

    def test_both_absent_names_both(self, monkeypatch):
        _stub_resolvers(monkeypatch, pi_acp=(None, "/usr/bin"), pi_cli=(None, "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_PI)
        assert state.missing_components == (probe.COMPONENT_PI_ACP_ADAPTER, probe.COMPONENT_PI_CLI)

    def test_a_cached_miss_on_either_component_reports_restart_required(self, monkeypatch):
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch)
        monkeypatch.setattr(client, "_pi_acp_argv_cache", (["node", "/n/pi-acp.js"], "/usr/bin"))
        monkeypatch.setattr(client, "_pi_bin_cache", (None, "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_PI)
        assert state.installed == probe.INSTALLED
        assert state.restart_required is True


# ── The codex driver seams ──


class TestCodexDriverSeams:
    """The three functions ``_probe_codex`` reads its verdict from.

    Tested at the driver rather than only through the probe because the
    cached-negative logic is the part with a real hazard: an operator installs the
    adapter a MISSING row told them to install, and every spawn in the running
    gateway still reuses the cached ``None`` until a restart. Reporting
    ``restart_required`` instead of a bare ``installed`` is what keeps the panel
    from promising something the next session breaks.
    """

    def test_resolves_reports_a_runnable_argv(self, monkeypatch):
        """One component, unlike claude's two: the adapter ships its own Codex binary."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_resolve_codex_acp_bin", lambda: (["node", "/n/c.js"], "/p"))
        assert driver.codex_adapter_resolves() is True

    def test_resolves_reports_absence(self, monkeypatch):
        """``(None, searched_path)`` is the resolver's own "not found", not an error."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_resolve_codex_acp_bin", lambda: (None, "/searched"))
        assert driver.codex_adapter_resolves() is False

    def test_unresolved_cache_is_not_a_negative(self, monkeypatch):
        """No session has needed the adapter yet, so the fresh answer is the true one.

        Reading the sentinel as a negative would report ``restart_required`` on a
        gateway that has simply never spawned codex.
        """
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_codex_acp_argv_cache", client._UNRESOLVED)
        assert driver.codex_adapter_cached_negative() is False

    def test_absent_cache_attribute_is_not_a_negative(self, monkeypatch):
        """A build without the global must not read as a cached failure."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.delattr(client, "_codex_acp_argv_cache", raising=False)
        assert driver.codex_adapter_cached_negative() is False

    def test_cached_negative_is_reported(self, monkeypatch):
        """A cached ``None`` is the case the whole function exists for.

        The adapter may be on disk NOW while this process still refuses to spawn
        it, so the row has to say "restart" rather than "installed".
        """
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_codex_acp_argv_cache", (None, "/searched"))
        assert driver.codex_adapter_cached_negative() is True

    def test_cached_positive_is_not_a_negative(self, monkeypatch):
        """A cached runnable argv means spawns work; nothing to disclose."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_codex_acp_argv_cache", (["node", "/n/c.js"], "/p"))
        assert driver.codex_adapter_cached_negative() is False

    def test_unreadable_cache_shape_fails_safe(self, monkeypatch):
        """A cache that will not unpack must not crash a dashboard GET.

        Fails toward "no disclosure" rather than toward an exception: the row is
        built on a read-only path that must degrade, not 500.
        """
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_codex_acp_argv_cache", object())
        assert driver.codex_adapter_cached_negative() is False

    def test_install_command_names_the_package_the_resolver_searches_for(self):
        """The advice and the resolution ladder must agree by construction.

        A global install of the SCOPED package puts the UNSCOPED binary on PATH,
        which is what the ladder looks for -- so the command is built from the same
        constant rather than restated, and cannot drift from what satisfies it.
        """
        from kiro_crew.acp.client import CODEX_ACP_NPM_PKG
        from kiro_crew.agent_sdk.drivers import acp as driver

        command = driver.codex_adapter_install_command()
        assert command == f"npm i -g {CODEX_ACP_NPM_PKG}"
        assert CODEX_ACP_NPM_PKG in command


# ── Per-backend verdicts ──


class TestInstalledVerdicts:
    """A present harness reports ``installed`` and names nothing to install."""

    def test_kiro_installed_when_the_spawn_resolver_finds_the_binary(self, monkeypatch):
        _stub_resolvers(monkeypatch)
        state = probe.probe_backend(ACP_BACKEND_KIRO)
        assert state.installed == probe.INSTALLED
        assert state.missing_components == ()
        assert state.policy_id == "kiro"

    def test_claude_installed_only_when_both_components_resolve(self, monkeypatch):
        _stub_resolvers(monkeypatch)
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.INSTALLED
        assert state.missing_components == ()
        # No remedy to offer for a working backend -- an install command beside
        # an "installed" row would read as an action still outstanding.
        assert state.install_command == ""


class TestMissingVerdicts:
    """An absent harness names the component, so the operator has a next step."""

    def test_kiro_missing_names_the_cli(self, monkeypatch):
        _stub_resolvers(monkeypatch, kiro=None)
        state = probe.probe_backend(ACP_BACKEND_KIRO)
        assert state.installed == probe.MISSING
        assert state.missing_components == (probe.COMPONENT_KIRO_CLI,)

    def test_claude_missing_adapter_names_only_the_adapter(self, monkeypatch):
        """The half-install case: the CLI is there, the adapter is not.

        Reporting both would send the operator after a ``claude`` they already
        have, and the adapter's npm remedy would be buried beside it.
        """
        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.MISSING
        assert state.missing_components == (probe.COMPONENT_CLAUDE_ACP_ADAPTER,)

    def test_claude_missing_cli_names_only_the_cli_and_suggests_no_command(self, monkeypatch):
        """The mirror half-install, and the reason ``install_command`` can be "".

        The adapter's SDK does not search PATH for ``claude``, so an adapter with
        no CLI is genuinely dead -- but nothing in the repository establishes an
        install command for that half, and an invented one is worse than none.
        """
        _stub_resolvers(monkeypatch, claude_cli=None)
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.MISSING
        assert state.missing_components == (probe.COMPONENT_CLAUDE_CODE_CLI,)
        assert state.install_command == ""

    def test_claude_missing_both_names_both_and_suggests_the_adapter_install(self, monkeypatch):
        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"), claude_cli=None)
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.MISSING
        assert state.missing_components == (
            probe.COMPONENT_CLAUDE_ACP_ADAPTER,
            probe.COMPONENT_CLAUDE_CODE_CLI,
        )
        assert state.install_command.startswith("npm i -g ")

    def test_the_suggested_command_names_the_package_the_resolver_documents(self, monkeypatch):
        """Pinned against the resolver's own constant, not a literal here.

        A hardcoded package name in the test would let the two drift apart and
        still stay green, which is how an operator ends up running an install
        that produces nothing the resolver looks for.
        """
        from kiro_crew.acp.client import CLAUDE_ACP_NPM_PKG

        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.install_command == f"npm i -g {CLAUDE_ACP_NPM_PKG}"


class TestKasTracksKiro:
    """KAS has no resolver of its own, so its verdict is kiro's verdict."""

    @pytest.mark.parametrize("kiro_bin", ["/usr/local/bin/kiro-cli", None])
    def test_kas_matches_kiro_in_both_directions(self, monkeypatch, kiro_bin):
        _stub_resolvers(monkeypatch, kiro=kiro_bin)
        kiro = probe.probe_backend(ACP_BACKEND_KIRO)
        kas = probe.probe_backend(ACP_BACKEND_KAS)
        assert kas.installed == kiro.installed
        assert kas.missing_components == kiro.missing_components
        # Same verdict, own identity: the row still has to render as "kas".
        assert kas.policy_id == "kas"
        assert kas.backend == ACP_BACKEND_KAS

    def test_kas_answers_from_the_kiro_resolver_not_a_kas_one(self, monkeypatch):
        """``build_kas_argv`` takes ``kiro_bin``, so kiro's resolver IS the check.

        Probing kas alone must still consult ``_resolve_kiro_bin`` -- if it ever
        stops, something else is answering for a binary that is never spawned.
        """
        calls: list[int] = []
        from kiro_crew.acp import client

        monkeypatch.setattr(
            client,
            "_resolve_kiro_bin",
            lambda **_kw: (calls.append(1), "/usr/local/bin/kiro-cli")[1],
        )
        assert probe.probe_backend(ACP_BACKEND_KAS).installed == probe.INSTALLED
        assert calls == [1]


class TestUnknownIsNeverMissing:
    """A failed CHECK is its own state; collapsing it produces bad advice."""

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_raising_kiro_resolver_yields_unknown(self, monkeypatch, backend):
        """Covers the real case: the executable-trust snapshot can raise.

        ``_resolve_kiro_bin`` raises when a kiro-cli that IS present fails its
        trust check -- reporting that as "missing" would tell the operator to
        reinstall a binary that is sitting right there.
        """

        def _boom(**_kw):
            raise OSError("trust snapshot failed")

        from kiro_crew.acp import client

        monkeypatch.setattr(client, "_resolve_kiro_bin", _boom)
        state = probe.probe_backend(backend)
        assert state.installed == probe.UNKNOWN
        assert state.missing_components == ()

    def test_a_raising_claude_resolver_yields_unknown(self, monkeypatch):
        """The Claude probe spawns mise, so a raise here is a routine failure."""
        _stub_resolvers(monkeypatch)
        from kiro_crew.acp import client

        def _boom():
            raise RuntimeError("mise exploded")

        monkeypatch.setattr(client, "_resolve_claude_acp_bin", _boom)
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.UNKNOWN
        assert state.missing_components == ()
        assert state.install_command == ""

    def test_an_id_with_no_probe_is_unknown_rather_than_missing(self):
        """A plugin-registered backend this module has never heard of.

        It reaches ``unknown`` by lookup miss, not by falling through to
        whichever branch happened to be last.
        """
        state = probe.probe_backend("some-future-harness")
        assert state.installed == probe.UNKNOWN
        assert state.policy_id == "some-future-harness"


# ── The TTL cache ──


class TestProbeCache:
    """The dashboard polls this endpoint and the Claude probe spawns a process."""

    def test_two_probes_resolve_once(self, monkeypatch):
        calls: list[str] = []
        from kiro_crew.acp import client

        monkeypatch.setattr(
            client,
            "_resolve_kiro_bin",
            lambda **_kw: (calls.append("kiro"), "/usr/local/bin/kiro-cli")[1],
        )
        probe.probe_backend(ACP_BACKEND_KIRO)
        probe.probe_backend(ACP_BACKEND_KIRO)
        assert calls == ["kiro"]

    def test_clearing_the_cache_re_probes(self, monkeypatch):
        calls: list[str] = []
        from kiro_crew.acp import client

        monkeypatch.setattr(
            client,
            "_resolve_kiro_bin",
            lambda **_kw: (calls.append("kiro"), "/usr/local/bin/kiro-cli")[1],
        )
        probe.probe_backend(ACP_BACKEND_KIRO)
        probe.clear_probe_cache()
        probe.probe_backend(ACP_BACKEND_KIRO)
        assert calls == ["kiro", "kiro"]

    def test_an_expired_entry_re_probes(self, monkeypatch):
        """TTL honoured without a sleep: a zero TTL expires on the same tick.

        Asserting the expiry by waiting would make the test a stopwatch reading
        on a loaded xdist worker; pinning the module-level TTL keeps it a
        statement about the code.
        """
        monkeypatch.setattr(probe, "CACHE_TTL_SECONDS", 0.0)
        calls: list[str] = []
        from kiro_crew.acp import client

        monkeypatch.setattr(
            client,
            "_resolve_kiro_bin",
            lambda **_kw: (calls.append("kiro"), "/usr/local/bin/kiro-cli")[1],
        )
        probe.probe_backend(ACP_BACKEND_KIRO)
        probe.probe_backend(ACP_BACKEND_KIRO)
        assert calls == ["kiro", "kiro"]

    def test_listing_every_backend_resolves_the_shared_binary_once(self, monkeypatch):
        """kiro and kas share one cache entry, so the switch costs one resolve.

        This is the payoff of routing ``_probe_kas`` through the cached probe
        rather than calling the kiro probe directly.
        """
        calls: list[str] = []
        from kiro_crew.acp import client

        # ``probe_backends`` probes EVERY backend, and the pi/codex/self-served resolvers
        # reach ``_mise_which`` -- the host's ``mise``. Stub the whole table through the
        # file's one helper, then layer the counting kiro resolver on top.
        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"), claude_cli=None)
        monkeypatch.setattr(
            client,
            "_resolve_kiro_bin",
            lambda **_kw: (calls.append("kiro"), "/usr/local/bin/kiro-cli")[1],
        )
        probe.probe_backends()
        assert calls == ["kiro"]


class TestSpawnCacheDivergence:
    """The probe must not promise a harness the RUNNING gateway cannot spawn.

    ``AcpClient`` resolves the claude adapter once per process and keeps that
    answer for the process's whole life. So a fresh resolve here can disagree with
    what a spawn will actually do, and one direction is a trap an operator walks
    into by following this panel's own advice: a failed session caches ``None``,
    they install the adapter the UI told them to install, and a probe that bypassed
    the cache would light the option up while every spawn still dies on the cached
    ``None``.
    """

    def _cache(self, monkeypatch, value):
        from kiro_crew.acp import client

        monkeypatch.setattr(client, "_claude_acp_argv_cache", value, raising=False)

    def test_a_cached_negative_marks_a_fresh_install_restart_required(self, monkeypatch):
        _stub_resolvers(monkeypatch)  # both components resolve NOW
        self._cache(monkeypatch, (None, "/usr/bin"))  # but the process cached absent
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        # Installed is the truth about the disk; restart_required is the truth
        # about this process. Both are needed, and neither alone is honest.
        assert state.installed == probe.INSTALLED
        assert state.restart_required is True

    def test_no_restart_flag_when_the_cache_agrees(self, monkeypatch):
        _stub_resolvers(monkeypatch)
        self._cache(monkeypatch, (["node", "/x/adapter.js"], "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.INSTALLED
        assert state.restart_required is False

    def test_an_unresolved_cache_is_not_a_negative(self, monkeypatch):
        """No session has needed the adapter yet, so the next spawn resolves fresh.

        Treating the sentinel as a negative would tell every operator on a freshly
        started gateway to restart it, which is the false positive that would make
        the flag ignorable.
        """
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch)
        self._cache(monkeypatch, client._UNRESOLVED)
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.restart_required is False

    def test_a_removed_adapter_reports_missing_despite_a_cached_positive(self, monkeypatch):
        """The opposite skew needs no special case, and must not be papered over.

        The cached argv points at a path that is gone, so the spawn fails too —
        ``MISSING`` from the fresh resolve is the accurate answer, not a stale
        ``INSTALLED`` inherited from the cache.
        """
        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"))
        self._cache(monkeypatch, (["node", "/x/adapter.js"], "/usr/bin"))
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.MISSING
        assert state.restart_required is False

    def test_a_malformed_cache_value_is_not_read_as_a_negative(self, monkeypatch):
        # Defensive only because this reads another module's private global: an
        # unexpected shape must not manufacture a restart prompt.
        _stub_resolvers(monkeypatch)
        self._cache(monkeypatch, "not-a-tuple")
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.restart_required is False

    def test_the_probe_does_not_mutate_the_spawn_cache(self, monkeypatch):
        """Reading, never invalidating.

        Invalidation would make a dashboard GET mutate a global on the spawn path.
        The disclosure costs one restart; the mutation costs a side effect on a
        concurrent path.
        """
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch)
        sentinel = (None, "/usr/bin")
        self._cache(monkeypatch, sentinel)
        probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert client._claude_acp_argv_cache is sentinel


class TestProbeBackendsCoverage:
    """Every known id gets a row, sorted, including one this build cannot serve."""

    def test_rows_cover_all_known_ids_sorted_by_policy_id(self, monkeypatch):
        _stub_resolvers(monkeypatch)
        states = probe.probe_backends()
        assert {s.backend for s in states} == set(ACP_BACKENDS_KNOWN)
        policy_ids = [s.policy_id for s in states]
        assert policy_ids == sorted(policy_ids)
        # claude is absent from the public build's selectable set, and still
        # present here -- the switch has to be able to explain it, not hide it.
        assert "claude" in policy_ids


# ── The endpoint ──


#: ``_request``'s default body: asking for JSON RAISES, which is what aiohttp does
#: for a request that carries none or carries something else. Distinct from a body
#: of ``None``, which is valid JSON and a different branch of the handler.
_NO_JSON = object()


def _request(
    *,
    app: str = "",
    user: str = "owner-1",
    owner: str = "owner-1",
    json_body: object = _NO_JSON,
):
    """A request shaped like a real dashboard call, for the owner predicate.

    ``_is_dashboard_owner`` requires the ``app`` claim present-and-EMPTY and the
    caller to equal ``state.owner_id``, so a bare ``MagicMock`` (whose ``get``
    returns truthy stubs) is refused rather than admitted -- the stub has to
    answer those two keys precisely or the test lands on the gate instead of its
    subject.

    A real ``web.Request`` is a mapping, so it answers a claim through ``get``,
    ``in`` and ``[]`` alike; this stub answers all three from one dict for the
    same reason. Stubbing only one spelling would make the gate's behaviour a
    function of how it happens to be WRITTEN, so the next reader who swaps an
    equivalent form fails a test that has nothing to say about their change.

    ``json_body`` is what ``await request.json()`` answers. Left unset, it raises
    the way aiohttp does for a body that is absent or not JSON -- so a case has to
    OPT IN to being well-formed, and the GET's own cases keep the shape they had.
    """
    req = MagicMock()
    req.path = "/api/acp-backends"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}

    async def _json():
        if json_body is _NO_JSON:
            raise ValueError("not json")
        return json_body

    req.json = _json
    return req


class TestEndpointOwnerGate:
    """Host install state is host configuration, so it is the owner's to read."""

    def test_a_non_owner_is_refused_with_a_machine_readable_code(self, monkeypatch):
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        # SEL is stubbed because a denial must land even when the audit log is
        # unwritable, and because a test must not write the real event log.
        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        called: list[str] = []

        def _snapshot():
            called.append("probed")
            return []

        monkeypatch.setattr(handler, "_snapshot", _snapshot)

        response = asyncio.run(handler.api_acp_backend_status(_request(user="someone-else")))

        assert response.status == 403
        assert json.loads(response.text or "{}")["code"] == "dashboard_owner_required"
        # The refusal must precede the work: probing spawns a subprocess, so a
        # non-owner request may not reach it even to have its result discarded.
        assert called == []

    def test_an_app_token_is_not_the_dashboard_owner(self, monkeypatch):
        """``app`` non-empty means an App Kit caller, not the human owner."""
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        response = asyncio.run(handler.api_acp_backend_status(_request(app="some-app")))
        assert response.status == 403


class TestEndpointPayloadShape:
    """The pinned contract: the dashboard branches on these exact keys."""

    def test_owner_gets_one_row_per_backend_in_the_pinned_shape(self, monkeypatch):
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        # codex's resolver is stubbed NOT-FOUND alongside claude's, because the row
        # assertion below pins it as ``missing``: reaching the real resolver would read
        # ``installed`` on any host that has the adapter and ``missing`` in CI, so the
        # test would answer a question about the machine rather than about the payload.
        _stub_resolvers(
            monkeypatch,
            adapter=(None, "/usr/bin"),
            claude_cli=None,
            codex_acp=(None, "/usr/bin"),
        )
        # ``selectable`` is pinned rather than read live: this assertion is about
        # the payload carrying the governance answer, not about what this
        # deployment's policy happens to permit today.
        import kiro_crew.dashboard.handlers.core as core

        monkeypatch.setattr(core, "_selectable_acp_backends", lambda: ["", "kas"])

        response = asyncio.run(handler.api_acp_backend_status(_request()))
        assert response.status == 200

        rows = json.loads(response.text or "{}")["backends"]
        assert [r["policy_id"] for r in rows] == [
            "claude",
            "codex",
            "deepseek",
            "goose",
            "kas",
            "kiro",
            "opencode",
            "pi",
        ]
        for row in rows:
            assert set(row) == {
                "id",
                "policy_id",
                "selectable",
                "installed",
                "missing_components",
                "install_command",
                "restart_required",
                "auth",
                # The capability card, spread into the row rather than nested:
                # each of these four is read on its own by the panel. What each
                # line MEANS is pinned in ``test_backend_cards``; this file pins
                # that the row carries them.
                "capabilities",
                "security_notes",
                "operator_notes",
                "tool_approval",
                "offered_by_build",
                # The card's MCP half, and the one part of it that is NESTED rather
                # than spread: its fields answer one question together, and a panel
                # served by a gateway that predates them tests one absent object
                # instead of several absent fields. Its own shape is pinned in
                # ``test_backend_mcp_ability``.
                "mcp",
            }
            assert set(row["mcp"]) == {
                "per_tool_deny",
                "costs_whole_server",
                "ineffective",
            }
            # Sign-in is the harness's own third fact, so every row carries it --
            # including a row whose harness this build cannot serve, which is the
            # one an operator is most likely to be asking about.
            assert set(row["auth"]) == {"sign_in_remedy", "signs_in_separately"}
            # Every row carries the WHOLE card, including a harness this build
            # cannot serve: an operator comparing two harnesses is reading the
            # same questionnaire for each, and a row short of a line would make
            # the two incomparable with nothing to say so.
            assert [entry["id"] for entry in row["capabilities"]] == [
                spec.id for spec in backend_cards.USER_FACING_LINES
            ]
            assert isinstance(row["tool_approval"], str) and row["tool_approval"]

        by_policy = {r["policy_id"]: r for r in rows}
        assert by_policy["kiro"] == {
            "id": "",
            "policy_id": "kiro",
            "selectable": True,
            "installed": "installed",
            "missing_components": [],
            "install_command": "",
            "restart_required": False,
            # Compared against the declaration rather than a literal copy of the
            # remedy: the string is rendered verbatim by the panel, so a literal
            # here would pin the WORDING, and every reword of the operator advice
            # would read as a wire-contract break.
            "auth": {
                "sign_in_remedy": host_auth.declaration_for("").sign_in_remedy,
                "signs_in_separately": False,
            },
            # Compared against the projection for the same reason: the card is
            # DERIVED from capability membership, so a literal copy here would
            # pin today's memberships and read a deliberate capability change as
            # a wire break. What this asserts is that the row carries the
            # projection unaltered -- the handler adds nothing and drops nothing.
            **backend_cards.card_payload(ACP_BACKEND_KIRO),
        }
        # Not selectable in this build AND not installed here -- both facts on
        # one row, which is the whole reason the endpoint exists.
        assert by_policy["claude"]["selectable"] is False
        assert by_policy["claude"]["installed"] == "missing"
        assert by_policy["claude"]["missing_components"] == [
            probe.COMPONENT_CLAUDE_ACP_ADAPTER,
            probe.COMPONENT_CLAUDE_CODE_CLI,
        ]
        # codex is in ACP_BACKENDS_KNOWN with no entry in ``_PROBES``, so it gets a
        # row -- the endpoint lists every id the switch can show -- but the row can
        # only say ``unknown`` and must name nothing to install. That gap is why
        # codex now has a probe, so its row carries a real verdict rather than
        # ``unknown``. That is the whole reason it could be offered: the operator
        # gets the component name and the command that installs it.
        assert by_policy["codex"]["installed"] == "missing"
        assert by_policy["codex"]["missing_components"] == ["codex-acp"]
        assert by_policy["codex"]["install_command"].startswith("npm i -g ")
        # ``selectable`` stays False here because this test PINS the live enum to
        # ``["", "kas"]`` above; it asserts the payload shape, not the registry.
        assert by_policy["codex"]["selectable"] is False
        # opencode's row is the one-component shape. The resolver is stubbed PRESENT
        # above, so this pins the installed form -- and with it that the row invents
        # neither a component nor a command when there is nothing to install.
        assert by_policy["opencode"]["installed"] == "installed"
        assert by_policy["opencode"]["missing_components"] == []
        assert by_policy["opencode"]["install_command"] == ""
        # pi's row is the two-component shape with both resolvers stubbed present.
        assert by_policy["pi"]["installed"] == "installed"
        assert by_policy["pi"]["missing_components"] == []
        assert by_policy["pi"]["install_command"] == ""

    def test_an_unknown_row_names_no_components(self, monkeypatch):
        """The three-state rule, enforced at the payload boundary too.

        ``missing_components`` is non-empty only for ``missing``, so a row whose
        check failed cannot suggest an install the probe never justified.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        def _boom(**_kw):
            raise OSError("trust snapshot failed")

        from kiro_crew.acp import client

        # The handler probes EVERY backend; the pi/codex/self-served resolvers would reach
        # the host's ``mise``. Stub the whole table, then make only kiro's resolver raise.
        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"), claude_cli=None)
        monkeypatch.setattr(client, "_resolve_kiro_bin", _boom)
        import kiro_crew.dashboard.handlers.core as core

        monkeypatch.setattr(core, "_selectable_acp_backends", lambda: ["", "kas"])

        response = asyncio.run(handler.api_acp_backend_status(_request()))
        by_policy = {r["policy_id"]: r for r in json.loads(response.text or "{}")["backends"]}
        assert by_policy["kiro"]["installed"] == "unknown"
        assert by_policy["kiro"]["missing_components"] == []
        assert by_policy["kas"]["installed"] == "unknown"


class TestForgetProbe:
    """Per-backend eviction, and the one delegation it has to know about."""

    def test_it_drops_only_the_named_backend(self, monkeypatch):
        """One button must not cost a re-resolve on seven other harnesses.

        Two of the probes shell out or walk the filesystem, so a wholesale clear
        would turn one re-check into that much work on the next poll.
        """
        _stub_resolvers(monkeypatch)
        probe.probe_backend(ACP_BACKEND_CLAUDE)
        probe.probe_backend(ACP_BACKEND_CODEX)
        assert probe._cached(ACP_BACKEND_CLAUDE) is not None

        probe.forget_probe(ACP_BACKEND_CLAUDE)

        assert probe._cached(ACP_BACKEND_CLAUDE) is None
        assert probe._cached(ACP_BACKEND_CODEX) is not None

    def test_forgetting_kas_also_forgets_the_verdict_its_probe_reads(self, monkeypatch):
        """``_probe_kas`` answers by calling ``probe_backend`` for kiro.

        So dropping only the kas entry would rebuild it from kiro's still-cached
        one and report the very verdict it was asked to re-take. The delegation is
        declared beside the probes for exactly this reason: a caller that had to
        know it would be a second place that can forget.
        """
        _stub_resolvers(monkeypatch)
        probe.probe_backend(ACP_BACKEND_KAS)
        assert probe._cached(ACP_BACKEND_KAS) is not None
        assert probe._cached(ACP_BACKEND_KIRO) is not None

        probe.forget_probe(ACP_BACKEND_KAS)

        assert probe._cached(ACP_BACKEND_KAS) is None
        assert probe._cached(ACP_BACKEND_KIRO) is None

    def test_forgetting_kiro_also_forgets_the_copy_its_verdict_was_copied_into(self, monkeypatch):
        """The coupling runs BOTH ways, because kas's entry is a copy of kiro's.

        ``_probe_kas`` answers by calling ``probe_backend`` for kiro, so a re-check on
        the kiro row that dropped only kiro leaves kas holding the pre-install copy --
        the kiro row reads ready and the kas row still says missing, with a dead
        switch, for the rest of the TTL. That is a re-check reporting one answer for a
        machine it just measured and a different one for the same binary.
        """
        _stub_resolvers(monkeypatch)
        probe.probe_backend(ACP_BACKEND_KAS)
        assert probe._cached(ACP_BACKEND_KIRO) is not None
        assert probe._cached(ACP_BACKEND_KAS) is not None

        probe.forget_probe(ACP_BACKEND_KIRO)

        assert probe._cached(ACP_BACKEND_KIRO) is None
        assert probe._cached(ACP_BACKEND_KAS) is None

    def test_forgetting_an_unprobed_backend_is_silent(self):
        probe.forget_probe("never-probed")


class TestEvictionOutranksAProbeAlreadyRunning:
    """A probe that started before an eviction must not land its answer after it.

    The lock is released while a probe resolves, and the dashboard polls this module
    while the re-check button evicts -- so the poll's probe and the operator's
    eviction genuinely overlap. Without a generation the poll's pre-install verdict
    is written on top of the fresh one and every later reader is told the harness is
    still missing until the TTL runs out, which is the state the button exists to
    leave behind.
    """

    @staticmethod
    def _blocking_probe(monkeypatch, backend, state):
        """Install a probe for *backend* that parks until it is released.

        Returns ``(entered, release)`` events: the caller waits on ``entered`` to
        know the probe is inside the resolve with the lock released, which is the
        only window the race exists in -- so nothing here is a timing guess.
        """
        entered = threading.Event()
        release = threading.Event()

        def _probe():
            entered.set()
            assert release.wait(timeout=10)
            return state

        monkeypatch.setitem(probe._PROBES, backend, _probe)
        return entered, release

    def test_a_probe_that_raced_an_eviction_does_not_store_its_verdict(self, monkeypatch):
        stale = probe.BackendInstallState(
            ACP_BACKEND_CLAUDE, ACP_BACKEND_CLAUDE, probe.MISSING, ("claude-code",)
        )
        entered, release = self._blocking_probe(monkeypatch, ACP_BACKEND_CLAUDE, stale)

        answers: list[probe.BackendInstallState] = []
        worker = threading.Thread(
            target=lambda: answers.append(probe.probe_backend(ACP_BACKEND_CLAUDE))
        )
        worker.start()
        assert entered.wait(timeout=10)

        # The operator's re-check, mid-probe.
        probe.forget_probe(ACP_BACKEND_CLAUDE)
        release.set()
        worker.join(timeout=10)
        assert not worker.is_alive()

        # Its own caller still gets the answer it resolved: that verdict is honest
        # about the machine it looked at, and discarding it would leave that caller
        # with nothing.
        assert answers == [stale]
        # Nobody else does. The next reader re-resolves.
        assert probe._cached(ACP_BACKEND_CLAUDE) is None

    def test_a_probe_that_raced_nothing_still_stores_its_verdict(self, monkeypatch):
        """The fence must not cost the caching the polling path depends on."""
        fresh = probe.BackendInstallState(ACP_BACKEND_CLAUDE, ACP_BACKEND_CLAUDE, probe.INSTALLED)
        entered, release = self._blocking_probe(monkeypatch, ACP_BACKEND_CLAUDE, fresh)

        worker = threading.Thread(target=lambda: probe.probe_backend(ACP_BACKEND_CLAUDE))
        worker.start()
        assert entered.wait(timeout=10)
        release.set()
        worker.join(timeout=10)

        assert probe._cached(ACP_BACKEND_CLAUDE) == fresh

    def test_a_wholesale_clear_also_outranks_a_first_ever_probe(self, monkeypatch):
        """The id with no entry to drop is the one a clear could miss.

        A probe already in flight holds no cache entry, so a clear that bumped only
        the ids it can see would leave that call free to write a pre-clear verdict.
        """
        stale = probe.BackendInstallState(
            ACP_BACKEND_CODEX, ACP_BACKEND_CODEX, probe.MISSING, ("codex-acp",)
        )
        entered, release = self._blocking_probe(monkeypatch, ACP_BACKEND_CODEX, stale)

        worker = threading.Thread(target=lambda: probe.probe_backend(ACP_BACKEND_CODEX))
        worker.start()
        assert entered.wait(timeout=10)

        probe.clear_probe_cache()
        release.set()
        worker.join(timeout=10)

        assert probe._cached(ACP_BACKEND_CODEX) is None

    def test_forgetting_kiro_fences_an_in_flight_kas_probe(self, monkeypatch):
        """The bump travels to the copy too, not only the pop.

        An in-flight kas probe holds kiro's pre-install verdict in hand, so without
        the bump on kas it writes that copy back after the kiro row was cleared.
        """
        stale = probe.BackendInstallState(
            ACP_BACKEND_KAS, ACP_BACKEND_KAS, probe.MISSING, ("kiro-cli",)
        )
        entered, release = self._blocking_probe(monkeypatch, ACP_BACKEND_KAS, stale)

        worker = threading.Thread(target=lambda: probe.probe_backend(ACP_BACKEND_KAS))
        worker.start()
        assert entered.wait(timeout=10)

        probe.forget_probe(ACP_BACKEND_KIRO)
        release.set()
        worker.join(timeout=10)

        assert probe._cached(ACP_BACKEND_KAS) is None

    def test_forgetting_kas_fences_the_kiro_probe_its_own_probe_reads(self, monkeypatch):
        """The delegation is fenced where it is dropped, not only dropped.

        ``forget_probe(kas)`` drops kiro's entry because ``_probe_kas`` answers from
        it. An in-flight kiro probe would rebuild exactly that entry, so the bump
        has to travel to the same second id the pop does.
        """
        stale = probe.BackendInstallState(
            ACP_BACKEND_KIRO, ACP_BACKEND_KIRO, probe.MISSING, ("kiro-cli",)
        )
        entered, release = self._blocking_probe(monkeypatch, ACP_BACKEND_KIRO, stale)

        worker = threading.Thread(target=lambda: probe.probe_backend(ACP_BACKEND_KIRO))
        worker.start()
        assert entered.wait(timeout=10)

        probe.forget_probe(ACP_BACKEND_KAS)
        release.set()
        worker.join(timeout=10)

        assert probe._cached(ACP_BACKEND_KIRO) is None


class TestForgetCachedResolution:
    """The driver seam that makes the re-check REAL rather than a second read.

    Its counterparts ``*_cached_negative`` only ever CONSULT the spawn path's cache,
    and the rule behind that is about the verb: a GET is replayable and unattributed,
    so a side effect inside one is a mutation nobody asked for. An owner-gated audited
    POST is the request that did.
    """

    def test_it_returns_the_claude_adapter_cache_to_unresolved(self, monkeypatch):
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_claude_acp_argv_cache", (None, "/usr/bin"), raising=False)
        driver.forget_cached_resolution(ACP_BACKEND_CLAUDE)
        assert client._claude_acp_argv_cache is client._UNRESOLVED

    def test_it_returns_the_codex_adapter_cache_to_unresolved(self, monkeypatch):
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_codex_acp_argv_cache", (None, "/usr/bin"), raising=False)
        driver.forget_cached_resolution(ACP_BACKEND_CODEX)
        assert client._codex_acp_argv_cache is client._UNRESOLVED

    def test_it_clears_BOTH_pi_caches(self, monkeypatch):
        """Either half cached absent kills the next spawn, so both have to go."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_pi_acp_argv_cache", (None, "/usr/bin"), raising=False)
        monkeypatch.setattr(client, "_pi_bin_cache", (None, "/usr/bin"), raising=False)
        driver.forget_cached_resolution(ACP_BACKEND_PI)
        assert client._pi_acp_argv_cache is client._UNRESOLVED
        assert client._pi_bin_cache is client._UNRESOLVED

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_it_drops_one_self_served_key_and_keeps_the_siblings(self, backend, monkeypatch):
        """These harnesses share ONE dict, so forgetting one is a key deletion."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        caches = {other: _ABSENT for other in ACP_BACKENDS_SELF_SERVED_ACP}
        monkeypatch.setattr(client, "_self_served_bin_caches", caches)
        driver.forget_cached_resolution(backend)
        assert backend not in caches
        for other in ACP_BACKENDS_SELF_SERVED_ACP - {backend}:
            assert other in caches

    def test_clearing_one_adapter_leaves_the_other_alone(self, monkeypatch):
        """A re-check of codex must not make the next claude spawn re-resolve."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        kept = (None, "/usr/bin")
        monkeypatch.setattr(client, "_claude_acp_argv_cache", kept, raising=False)
        monkeypatch.setattr(client, "_codex_acp_argv_cache", (None, "/usr/bin"), raising=False)
        driver.forget_cached_resolution(ACP_BACKEND_CODEX)
        assert client._claude_acp_argv_cache is kept

    def test_a_harness_with_no_cache_of_its_own_is_silent(self, monkeypatch):
        """kiro resolves per spawn and KAS shares its answer: nothing to forget."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_self_served_bin_caches", {})
        driver.forget_cached_resolution(ACP_BACKEND_KIRO)
        driver.forget_cached_resolution(ACP_BACKEND_KAS)

    def test_the_GET_still_never_clears_anything(self, monkeypatch):
        """The seams' own rule, unchanged by the POST existing.

        A read that mutated would be the thing those docstrings forbid, and the
        endpoint pair only makes sense while this stays true.
        """
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch)
        sentinel = (None, "/usr/bin")
        monkeypatch.setattr(client, "_claude_acp_argv_cache", sentinel, raising=False)
        probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert client._claude_acp_argv_cache is sentinel


class TestRecheckClearsWhatTheGetCouldOnlyReport:
    """The operator's own sequence, in order, because each step earns the next."""

    def test_probe_then_install_then_recheck_is_INSTALLED_without_restart(self, monkeypatch):
        """probe -> negative cached -> component appears -> re-check -> clean.

        The middle step is the one a test usually skips: the process cache is
        populated by a FAILED spawn, not by the probe, so it is set explicitly.
        """
        from kiro_crew.acp import client

        # 1. The component is absent, and the probe says so.
        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"))
        first = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert first.installed == probe.MISSING
        assert probe.COMPONENT_CLAUDE_ACP_ADAPTER in first.missing_components

        # 2. A spawn tried and cached the absence, for the life of the process.
        monkeypatch.setattr(client, "_claude_acp_argv_cache", (None, "/usr/bin"), raising=False)

        # 3. The operator runs the install command the panel printed. The GET can
        #    only REPORT the divergence -- that is what restart_required is.
        _stub_resolvers(monkeypatch)
        probe.forget_probe(ACP_BACKEND_CLAUDE)
        reported = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert reported.installed == probe.INSTALLED
        assert reported.restart_required is True

        # 4. The re-check, which is allowed to clear.
        probe.forget_for_recheck(ACP_BACKEND_CLAUDE)
        rechecked = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert rechecked.installed == probe.INSTALLED
        assert rechecked.restart_required is False
        # And the spawn path really was cleared -- the next spawn resolves fresh
        # rather than reusing the absence. This is what separates a real re-check
        # from a re-read that merely looks green.
        assert client._claude_acp_argv_cache is client._UNRESOLVED

    @pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_SELF_SERVED_ACP))
    def test_the_same_sequence_holds_for_a_self_served_harness(self, backend, monkeypatch):
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch, self_served={backend: _PRESENT})
        caches = {backend: _ABSENT}
        monkeypatch.setattr(client, "_self_served_bin_caches", caches)
        reported = probe.probe_backend(backend)
        assert reported.installed == probe.INSTALLED
        assert reported.restart_required is True

        probe.forget_for_recheck(backend)
        rechecked = probe.probe_backend(backend)
        assert rechecked.installed == probe.INSTALLED
        assert rechecked.restart_required is False
        assert backend not in caches

    def test_a_recheck_cannot_fake_a_green(self, monkeypatch):
        """No parameter writes a verdict, so a still-absent component still says so."""
        from kiro_crew.acp import client

        monkeypatch.setattr(client, "_claude_acp_argv_cache", client._UNRESOLVED, raising=False)
        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"))
        probe.forget_for_recheck(ACP_BACKEND_CLAUDE)
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.MISSING
        assert probe.COMPONENT_CLAUDE_ACP_ADAPTER in state.missing_components
        assert state.restart_required is False

    def test_restart_required_survives_when_a_spawn_recaches_after_the_clear(self, monkeypatch):
        """Unblocked, never suppressed.

        The flag is read from the spawn path AFTER the clear, so a cache that is
        negative again by then keeps saying so. This is also the residual race the
        seam's docstring discloses: a resolver that began before the install
        publishes its stale answer when it resumes. The honest consequence is that
        the panel keeps telling the operator to restart, not that it goes green.
        """
        from kiro_crew.acp import client

        _stub_resolvers(monkeypatch)
        monkeypatch.setattr(client, "_claude_acp_argv_cache", client._UNRESOLVED, raising=False)
        probe.forget_for_recheck(ACP_BACKEND_CLAUDE)
        # A spawn that was already resolving resumes and publishes its stale answer.
        client._claude_acp_argv_cache = (None, "/usr/bin")
        state = probe.probe_backend(ACP_BACKEND_CLAUDE)
        assert state.installed == probe.INSTALLED
        assert state.restart_required is True


class TestRecheckEndpoint:
    """Owner-gated, id-validated, and the row shape the GET already sends."""

    def test_a_non_owner_is_refused_before_anything_is_cleared(self, monkeypatch):
        """The refusal has to precede the mutation, not just the response.

        This endpoint clears state on the spawn path. A non-owner reaching that
        even to have the result discarded would be the mutation happening.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        called: list[str] = []
        monkeypatch.setattr(handler, "_recheck", lambda backend: called.append(backend) or {})

        response = asyncio.run(
            handler.api_acp_backend_recheck(
                _request(user="someone-else", json_body={"backend": ACP_BACKEND_CLAUDE})
            )
        )

        assert response.status == 403
        assert json.loads(response.text or "{}")["code"] == "dashboard_owner_required"
        assert called == []

    def test_a_refused_recheck_is_audited_under_its_own_operation(self, monkeypatch):
        """Not under the read's name.

        An auditor asking who tried to re-probe a harness would otherwise find those
        attempts filed as ``acp_backend_status_access``, which is the one question
        the record exists to answer.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        recorder = MagicMock()
        monkeypatch.setattr(handler, "sel", lambda: recorder)
        monkeypatch.setattr(handler, "_recheck", lambda backend: {})

        asyncio.run(
            handler.api_acp_backend_recheck(
                _request(user="someone-else", json_body={"backend": ACP_BACKEND_CLAUDE})
            )
        )

        kwargs = recorder.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "acp_backend_recheck"
        assert kwargs["outcome"] == "denied"

    def test_an_app_token_is_not_the_dashboard_owner(self, monkeypatch):
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        monkeypatch.setattr(handler, "_recheck", lambda backend: {})
        response = asyncio.run(
            handler.api_acp_backend_recheck(
                _request(app="some-app", json_body={"backend": ACP_BACKEND_CLAUDE})
            )
        )
        assert response.status == 403

    @pytest.mark.parametrize(
        "body",
        [
            None,
            {},
            {"backend": "not-a-backend"},
            {"backend": None},
            {"backend": 7},
            {"backend": ["claude"]},
        ],
    )
    def test_an_unknown_id_is_refused_and_probes_nothing(self, body, monkeypatch):
        """The validation bounds a dict this process keeps.

        Without it the backend id is a caller-chosen key written into the probe
        cache on every request, so this is what keeps that dict to the ids the
        build knows rather than defensive tidying.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        called: list[str] = []
        monkeypatch.setattr(handler, "_recheck", lambda backend: called.append(backend) or {})

        response = asyncio.run(handler.api_acp_backend_recheck(_request(json_body=body)))

        assert response.status == 400
        assert json.loads(response.text or "{}")["code"] == "unknown_agent_backend"
        assert called == []

    def test_a_body_that_is_not_json_is_refused(self, monkeypatch):
        """aiohttp raises rather than answering, and that is a 400 not a 500."""
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        called: list[str] = []
        monkeypatch.setattr(handler, "_recheck", lambda backend: called.append(backend) or {})

        response = asyncio.run(handler.api_acp_backend_recheck(_request()))

        assert response.status == 400
        assert json.loads(response.text or "{}")["code"] == "unknown_agent_backend"
        assert called == []

    def test_the_empty_string_is_a_real_backend_and_is_accepted(self, monkeypatch):
        """kiro is spelled ``''``, so presence of the key -- not truthiness -- decides.

        A truthiness check would refuse the shipped default, which is the one
        harness every install has.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        seen: list[str] = []

        def _recheck(backend: str):
            seen.append(backend)
            return {"id": backend, "installed": "installed"}

        monkeypatch.setattr(handler, "_recheck", _recheck)
        response = asyncio.run(
            handler.api_acp_backend_recheck(_request(json_body={"backend": ACP_BACKEND_KIRO}))
        )
        assert response.status == 200
        assert seen == [ACP_BACKEND_KIRO]
        assert json.loads(response.text or "{}")["backend"]["id"] == ACP_BACKEND_KIRO

    def test_it_answers_one_row_in_the_shape_the_list_sends(self, monkeypatch):
        """Same builder as the GET, so a field cannot go missing from one verb.

        Asserted against the GET's own row for the same backend rather than
        against a literal: a hand-written expected shape is a third place the
        contract lives.
        """
        from kiro_crew.acp import client
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        _stub_resolvers(monkeypatch)
        # Same reason as above: this case runs the real re-probe, which clears the
        # spawn path's cache by direct assignment.
        monkeypatch.setattr(client, "_claude_acp_argv_cache", client._UNRESOLVED, raising=False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core._selectable_acp_backends",
            lambda: sorted(ACP_BACKENDS_KNOWN),
        )

        listed = {row["id"]: row for row in handler._snapshot()}
        probe.clear_probe_cache()
        response = asyncio.run(
            handler.api_acp_backend_recheck(_request(json_body={"backend": ACP_BACKEND_CLAUDE}))
        )
        rechecked = json.loads(response.text or "{}")["backend"]

        assert response.status == 200
        assert set(rechecked) == set(listed[ACP_BACKEND_CLAUDE])

    def test_an_unwritable_audit_log_does_not_break_the_remedy(self, monkeypatch):
        """A mutation whose audit failed is still one the operator asked for.

        Refusing here would make an unwritable log break the panel's only way out
        of the restart prompt, which is a worse failure than a missing record.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        broken = MagicMock()
        broken.log_api_access.side_effect = OSError("read-only")
        monkeypatch.setattr(handler, "sel", lambda: broken)
        monkeypatch.setattr(handler, "_recheck", lambda backend: {"id": backend})

        response = asyncio.run(
            handler.api_acp_backend_recheck(_request(json_body={"backend": ACP_BACKEND_CLAUDE}))
        )
        assert response.status == 200

    def test_the_successful_mutation_is_audited(self, monkeypatch):
        """Unlike the GET beside it, the record of who cleared what is the point."""
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        recorder = MagicMock()
        monkeypatch.setattr(handler, "sel", lambda: recorder)
        monkeypatch.setattr(handler, "_recheck", lambda backend: {"id": backend})

        asyncio.run(
            handler.api_acp_backend_recheck(_request(json_body={"backend": ACP_BACKEND_CLAUDE}))
        )

        recorder.log_api_access.assert_called_once()
        kwargs = recorder.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "acp_backend_recheck"
        assert kwargs["outcome"] == "success"
        assert kwargs["resources"] == ACP_BACKEND_CLAUDE

    def test_a_raising_reprobe_records_no_success(self, monkeypatch):
        """The audit follows the work, so it cannot claim an outcome that did not happen.

        Auditing first would hand the caller a 500 while the log said the re-probe
        worked -- the one direction an audit record must not be wrong in.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        recorder = MagicMock()
        monkeypatch.setattr(handler, "sel", lambda: recorder)

        def _boom(backend: str):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(handler, "_recheck", _boom)

        with pytest.raises(RuntimeError):
            asyncio.run(
                handler.api_acp_backend_recheck(_request(json_body={"backend": ACP_BACKEND_CLAUDE}))
            )

        assert recorder.log_api_access.call_count == 0

    def test_the_probe_is_offloaded_and_the_CLEAR_IS_NOT(self, monkeypatch):
        """The split that keeps the clear safe, asserted rather than commented.

        The probe resolves binaries and may shell out, so it must leave the loop. The
        clear must NOT: every reader on the spawn path has no ``await`` between its
        check and its read, so those pairs are atomic against other loop tasks and
        not against a worker thread. A clear from a thread can land inside one and
        either raise ``KeyError`` in a session spawn or make an installed adapter
        report "not found".

        Asserted structurally, because the hazard is which THREAD the call runs on
        and a timing assertion cannot say that.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        monkeypatch.setattr(handler, "_recheck", lambda backend: {"id": backend})
        offloaded: list[object] = []
        real_to_thread = asyncio.to_thread

        async def _spy(fn, /, *args, **kwargs):
            offloaded.append(fn)
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(handler.asyncio, "to_thread", _spy)
        asyncio.run(
            handler.api_acp_backend_recheck(_request(json_body={"backend": ACP_BACKEND_CLAUDE}))
        )
        assert handler._recheck in offloaded
        from kiro_crew.agent_sdk import forget_for_recheck

        assert forget_for_recheck not in offloaded

    def test_the_clear_runs_before_the_probe(self, monkeypatch):
        """Order matters: probing first would measure through the cache being dropped."""
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        order: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.forget_for_recheck",
            lambda backend: order.append("clear"),
        )
        monkeypatch.setattr(
            handler, "_recheck", lambda backend: order.append("probe") or {"id": backend}
        )
        asyncio.run(
            handler.api_acp_backend_recheck(_request(json_body={"backend": ACP_BACKEND_CLAUDE}))
        )
        assert order == ["clear", "probe"]

    def test_a_refused_recheck_clears_nothing(self, monkeypatch):
        """The mutation must sit behind the gate, not merely have its result discarded."""
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        monkeypatch.setattr(handler, "sel", lambda: MagicMock())
        cleared: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.forget_for_recheck",
            lambda backend: cleared.append(backend),
        )
        monkeypatch.setattr(handler, "_recheck", lambda backend: {"id": backend})

        asyncio.run(
            handler.api_acp_backend_recheck(
                _request(user="someone-else", json_body={"backend": ACP_BACKEND_CLAUDE})
            )
        )
        asyncio.run(
            handler.api_acp_backend_recheck(_request(json_body={"backend": "not-a-backend"}))
        )
        assert cleared == []


class TestRouteIsRegistered:
    """A handler nothing routes to is invisible to the dashboard."""

    def test_the_get_route_is_registered_on_the_agent_config_slice(self):
        from aiohttp import web

        from kiro_crew.dashboard.routes import agent_config

        app = web.Application()
        agent_config.register(app)
        routes = {
            (resource.canonical, route.method)
            for resource in app.router.resources()
            for route in resource
        }
        assert ("/api/acp-backends", "GET") in routes

    def test_the_recheck_route_is_registered_as_a_POST(self):
        """The verb is the contract, not decoration.

        A GET may not clear the spawn path's cache -- every ``*_cached_negative``
        seam says so -- and the re-check does clear it, so registering this as
        anything but a POST would put a side effect behind a replayable read.
        """
        from aiohttp import web

        from kiro_crew.dashboard.routes import agent_config

        app = web.Application()
        agent_config.register(app)
        routes = {
            (resource.canonical, route.method)
            for resource in app.router.resources()
            for route in resource
        }
        assert ("/api/acp-backends/recheck", "POST") in routes
        assert ("/api/acp-backends/recheck", "GET") not in routes


def test_the_probe_is_offloaded_off_the_event_loop(monkeypatch):
    """The Claude probe spawns mise, so running it inline would stall every tab.

    Asserted structurally -- the handler must reach the snapshot through
    ``asyncio.to_thread`` -- because a wall-clock assertion on a blocking call
    is a stopwatch reading, not a statement about the code.
    """
    from kiro_crew.dashboard.handlers import acp_backend_status as handler

    seen: list[object] = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn, /, *args, **kwargs):
        seen.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(handler.asyncio, "to_thread", _spy)
    monkeypatch.setattr(handler, "_snapshot", lambda: [])

    asyncio.run(handler.api_acp_backend_status(_request()))
    assert handler._snapshot in seen


def test_the_boot_path_does_not_import_acp_at_module_scope():
    """``kiro_crew/acp/__init__`` pulls in the client AND the runtime.

    The handler is imported by the route table on the boot path and reaches the
    probe, which reaches the driver, so a module-scope ``kiro_crew.acp`` import
    anywhere along that chain would drag both into gateway start. The driver is
    ALLOWED to import ACP -- that is what it is for -- but not at module scope.
    Read from source rather than by import-graph inspection, because the
    forbidden thing is the ``import`` STATEMENT's position.
    """
    import ast
    from pathlib import Path

    _REPO_ROOT = Path(__file__).resolve().parents[1]
    for relative in (
        "src/kiro_crew/agent_sdk/__init__.py",
        "src/kiro_crew/agent_sdk/backend_install.py",
        "src/kiro_crew/agent_sdk/drivers/acp.py",
        "src/kiro_crew/dashboard/handlers/acp_backend_status.py",
    ):
        tree = ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("kiro_crew.acp."), relative
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("kiro_crew.acp."), relative
