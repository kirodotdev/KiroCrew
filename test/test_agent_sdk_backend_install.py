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
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk import backend_install as probe


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
):
    """Patch the three spawn resolvers on the module the driver imports from.

    Patched on ``kiro_crew.acp.client`` -- the DEFINING module -- because the
    driver imports them function-locally at call time, so that is the namespace
    the lookup actually reaches. A value of ``None`` (or ``(None, path)``) is the
    resolvers' own "not found" answer, not an error.
    """
    from kiro_crew.acp import client

    monkeypatch.setattr(client, "_resolve_kiro_bin", lambda **_kw: kiro)
    monkeypatch.setattr(client, "_resolve_claude_acp_bin", lambda: adapter)
    monkeypatch.setattr(client, "_resolve_claude_code_executable", lambda: claude_cli)


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

        monkeypatch.setattr(
            client,
            "_resolve_kiro_bin",
            lambda **_kw: (calls.append("kiro"), "/usr/local/bin/kiro-cli")[1],
        )
        monkeypatch.setattr(client, "_resolve_claude_acp_bin", lambda: (None, "/usr/bin"))
        monkeypatch.setattr(client, "_resolve_claude_code_executable", lambda: None)
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


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1"):
    """A request shaped like a real dashboard call, for the owner predicate.

    ``_is_dashboard_owner`` requires ``request["app"]`` present-and-EMPTY and
    the caller to equal ``state.owner_id``, so a bare ``MagicMock`` (whose
    ``get`` returns truthy stubs) is refused rather than admitted -- the stub has
    to answer those two keys precisely or the test lands on the gate instead of
    its subject.
    """
    req = MagicMock()
    req.path = "/api/acp-backends"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
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

        _stub_resolvers(monkeypatch, adapter=(None, "/usr/bin"), claude_cli=None)
        # ``selectable`` is pinned rather than read live: this assertion is about
        # the payload carrying the governance answer, not about what this
        # deployment's policy happens to permit today.
        import kiro_crew.dashboard.handlers.core as core

        monkeypatch.setattr(core, "_selectable_acp_backends", lambda: ["", "kas"])

        response = asyncio.run(handler.api_acp_backend_status(_request()))
        assert response.status == 200

        rows = json.loads(response.text or "{}")["backends"]
        assert [r["policy_id"] for r in rows] == ["claude", "codex", "kas", "kiro"]
        for row in rows:
            assert set(row) == {
                "id",
                "policy_id",
                "selectable",
                "installed",
                "missing_components",
                "install_command",
                "restart_required",
            }

        by_policy = {r["policy_id"]: r for r in rows}
        assert by_policy["kiro"] == {
            "id": "",
            "policy_id": "kiro",
            "selectable": True,
            "installed": "installed",
            "missing_components": [],
            "install_command": "",
            "restart_required": False,
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

    def test_an_unknown_row_names_no_components(self, monkeypatch):
        """The three-state rule, enforced at the payload boundary too.

        ``missing_components`` is non-empty only for ``missing``, so a row whose
        check failed cannot suggest an install the probe never justified.
        """
        from kiro_crew.dashboard.handlers import acp_backend_status as handler

        def _boom(**_kw):
            raise OSError("trust snapshot failed")

        from kiro_crew.acp import client

        monkeypatch.setattr(client, "_resolve_kiro_bin", _boom)
        monkeypatch.setattr(client, "_resolve_claude_acp_bin", lambda: (None, "/usr/bin"))
        monkeypatch.setattr(client, "_resolve_claude_code_executable", lambda: None)
        import kiro_crew.dashboard.handlers.core as core

        monkeypatch.setattr(core, "_selectable_acp_backends", lambda: ["", "kas"])

        response = asyncio.run(handler.api_acp_backend_status(_request()))
        by_policy = {r["policy_id"]: r for r in json.loads(response.text or "{}")["backends"]}
        assert by_policy["kiro"]["installed"] == "unknown"
        assert by_policy["kiro"]["missing_components"] == []
        assert by_policy["kas"]["installed"] == "unknown"


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
