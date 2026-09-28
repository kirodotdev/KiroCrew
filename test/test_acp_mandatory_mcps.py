"""Crew's own MCP servers are exempt from kiro-cli's Tool Search deferral.

Loading a deferred spec rewrites the request's ``tools`` array, and an
extended-thinking model's thinking blocks carry a signature bound to the array
they were minted under. Replaying one across a load makes the provider reject the
whole conversation ("The ``tools`` list differs from the one this block was
created with"), and every later turn on that session then fails identically.

Crew names its own servers in ``ASBX_KIRO_MANDATORY_MCPS`` so kiro-cli keeps
their specs resident and never loads one mid-turn. These tests cover the value
formatter, the server set it is built from, and the spawn-env hop -- including
which actor is allowed to change the answer.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.harness._common import apply_mandatory_mcps_env
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.harness.kiro import KiroHarness
from kiro_crew.agent import crew_owned_mcp_servers, emission_eligible_mcp_servers
from kiro_crew.agent_sdk.tool_search import MANDATORY_MCPS_ENV, mandatory_mcps_env_value


@pytest.fixture
def no_ambient(monkeypatch):
    """No operator value in the ambient environment.

    The hook reads ``os.environ`` directly, so a value the developer happens to
    export would otherwise decide these assertions.
    """
    monkeypatch.delenv(MANDATORY_MCPS_ENV, raising=False)


@pytest.fixture
def quiet_credentials(monkeypatch):
    """Silence the credential half of both harness hooks.

    ``apply_spawn_env`` also injects (kiro) or strips (KAS) the API key, which
    reads the data home. These tests are about the exemption, and a host read
    would make them depend on whatever credentials the machine carries.
    """
    monkeypatch.setattr(
        "kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None, raising=True
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None, raising=True
    )


def _crew_value() -> str:
    return mandatory_mcps_env_value(crew_owned_mcp_servers())


class TestTheEnvValue:
    def test_the_name_is_the_one_kiro_cli_reads(self) -> None:
        """Pinned: the engine reads this exact spelling from the process env, so a
        rename here silently stops exempting anything."""
        assert MANDATORY_MCPS_ENV == "ASBX_KIRO_MANDATORY_MCPS"

    def test_names_render_sorted_so_a_respawn_matches_the_spawn(self) -> None:
        """The same set must render one string. A resume that presented a different
        order would look like a different tool surface for no reason."""
        assert mandatory_mcps_env_value(["b", "a"]) == "a,b"
        assert mandatory_mcps_env_value({"b", "a"}) == mandatory_mcps_env_value(["a", "b"])

    def test_an_empty_set_renders_empty_so_the_caller_sets_nothing(self) -> None:
        """``""`` is the caller's signal to set no variable at all. The engine's own
        ``filter(|s| !s.is_empty())`` already reads an empty value as absent."""
        assert mandatory_mcps_env_value([]) == ""

    def test_it_renders_the_real_server_set(self) -> None:
        """The formatter takes ``crew_owned_mcp_servers()`` as it comes and does not
        re-validate it, so this is the shape that actually ships."""
        value = _crew_value()
        assert value
        assert value.split(",") == sorted(crew_owned_mcp_servers())


class TestTheServerSet:
    def test_it_names_the_opt_in_servers_the_eligible_set_drops(self) -> None:
        """The whole reason this set exists rather than reusing
        ``emission_eligible_mcp_servers``.

        An ``opt_in`` server the user granted is in their spec serving tools, so it
        churns the ``tools`` array exactly like an always-on one. The eligible set
        answers a different question ("would a rebuild re-add it") and drops every
        one of them, which would leave the granted ones deferring.
        """
        owned = crew_owned_mcp_servers()
        for opt_in in ("kirocrew-dashboard", "kirocrew-work", "kirocrew-crew-log"):
            assert opt_in in owned
            assert opt_in not in emission_eligible_mcp_servers()

    def test_it_names_the_always_on_servers_too(self) -> None:
        assert {"kirocrew-core", "kirocrew-cron"} <= crew_owned_mcp_servers()

    def test_it_is_a_superset_of_what_a_rebuild_would_emit(self) -> None:
        """Erring wide is free (a name for an absent server matches no tool); erring
        narrow is the defect, so the owned set may never be the smaller one."""
        assert emission_eligible_mcp_servers() <= crew_owned_mcp_servers()

    def test_it_carries_the_edition_seam_and_claims_no_prefix(self) -> None:
        """The composition IS the contract, and asserting a ``kirocrew-`` prefix
        instead would be vacuous.

        The set is the managed map plus the edition adapter's extras. The public
        build's adapter returns nothing, so a prefix assertion would pass without
        ever seeing an extra — and the seam does not constrain its keys, so a caller
        may not assume one. Pin what is actually promised.
        """
        from kiro_crew.agent import _MANAGED_MCP_SERVERS, _extra_mcp_servers

        assert crew_owned_mcp_servers() == frozenset((*_MANAGED_MCP_SERVERS, *_extra_mcp_servers()))


class TestWhichActorDecides:
    """An operator's AMBIENT value decides; a per-session OVERLAY never does.

    The hop receives ``{**os.environ, **extra_env}``, and ``extra_env`` carries
    per-session overlays — a cron job's own ``env`` block among them, which
    ``cron_job_env_without_reserved`` passes through for every key outside
    ``_CRON_RESERVED_ENV_KEYS`` and which an app manifest's ``crons[].env`` can
    author. Letting that suppress the exemption would brick that cron's sessions
    with no code-level recovery, since the variable is fixed at spawn.
    """

    def test_an_overlay_cannot_disable_the_exemption(self, no_ambient) -> None:
        """The blocking case: an empty value arriving from a per-session overlay.

        Nothing in the ambient environment said "exempt nothing", so this is not an
        operator choice — it is an overlay, and it is overwritten.
        """
        env = {MANDATORY_MCPS_ENV: ""}
        apply_mandatory_mcps_env(env)
        assert env[MANDATORY_MCPS_ENV] == _crew_value()

    def test_an_overlay_cannot_substitute_its_own_list(self, no_ambient) -> None:
        env = {MANDATORY_MCPS_ENV: "attacker-chosen"}
        apply_mandatory_mcps_env(env)
        assert env[MANDATORY_MCPS_ENV] == _crew_value()

    def test_an_overlay_cannot_invent_the_key_when_crew_names_nothing(
        self, no_ambient, monkeypatch
    ) -> None:
        """With no Crew servers to name, the key is removed rather than left behind.

        Otherwise an overlay's value would survive by default — the one path where
        doing nothing would have honoured it.
        """
        monkeypatch.setattr(
            "kiro_crew.agent.crew_owned_mcp_servers", lambda: frozenset(), raising=True
        )
        env = {MANDATORY_MCPS_ENV: "attacker-chosen"}
        apply_mandatory_mcps_env(env)
        assert MANDATORY_MCPS_ENV not in env

    def test_an_ambient_operator_value_wins(self, monkeypatch) -> None:
        """Crew names the servers it knows are hot; it does not cap what an operator
        chose to exempt — and the operator outranks any overlay."""
        monkeypatch.setenv(MANDATORY_MCPS_ENV, "my-own-server")
        env = {MANDATORY_MCPS_ENV: "overlay-value"}
        apply_mandatory_mcps_env(env)
        assert env[MANDATORY_MCPS_ENV] == "my-own-server"

    def test_an_ambient_empty_value_is_honoured(self, monkeypatch) -> None:
        """An empty ambient value is the ONLY spelling that says "exempt nothing".

        The engine drops empty fields, so an empty variable and an absent one mean the
        same thing to it — which makes ``""`` an operator's only way to turn the
        always-resident schema cost back off. Truthiness would leave that choice
        unexpressible.
        """
        monkeypatch.setenv(MANDATORY_MCPS_ENV, "")
        env = {MANDATORY_MCPS_ENV: "overlay-value"}
        apply_mandatory_mcps_env(env)
        assert env[MANDATORY_MCPS_ENV] == ""


class TestBothKiroFamilyHosts:
    """Both hosts get it, because the relay IS kiro-cli.

    KAS is launched as ``kiro-cli acp --agent-engine v3`` — the same ``acp``
    subcommand — and that subcommand reads the variable unconditionally.
    """

    def test_the_kiro_host_applies_it(self, no_ambient, quiet_credentials) -> None:
        env: dict[str, str] = {}
        KiroHarness().apply_spawn_env(env)
        assert env[MANDATORY_MCPS_ENV] == _crew_value()

    def test_the_kas_host_applies_it(self, no_ambient, quiet_credentials) -> None:
        env: dict[str, str] = {}
        KasHarness().apply_spawn_env(env)
        assert env[MANDATORY_MCPS_ENV] == _crew_value()

    def test_kas_launches_the_same_acp_subcommand(self) -> None:
        """The premise of covering KAS at all. The shared read follows only while the
        relay runs the same subcommand; a different one breaks that inheritance."""
        from kiro_crew.acp.harness.kiro import KIRO_CLI_SUBCMD
        from kiro_crew.acp.kas_transport import KAS_RELAY_SUBCMD, build_kas_argv

        assert KAS_RELAY_SUBCMD == KIRO_CLI_SUBCMD
        assert build_kas_argv("/bin/kiro-cli")[1] == KIRO_CLI_SUBCMD

    def test_neither_host_loses_its_credential_step(self, no_ambient, monkeypatch) -> None:
        """The hooks each had one job before this change and must still do it."""
        seen: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.config.loader.inject_kiro_cli_api_key",
            lambda _env: seen.append("inject"),
            raising=True,
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.strip_kiro_cli_api_key",
            lambda _env: seen.append("strip"),
            raising=True,
        )
        KiroHarness().apply_spawn_env({})
        KasHarness().apply_spawn_env({})
        assert seen == ["inject", "strip"]

    def test_the_runtime_spawn_path_applies_the_hook(self) -> None:
        """The hook is only worth anything if the path that spawns a session calls
        it. Pinned by source because that method launches a real child."""
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        assert "self._harness.apply_spawn_env(env)" in inspect.getsource(AcpRuntime._spawn_admitted)

    def test_the_value_does_not_depend_on_the_tool_search_toggle(self, no_ambient) -> None:
        """Applied whether or not deferral is on.

        The engine ignores the list while deferral is off, so a toggle-independent
        value cannot disagree with the toggle — and a resume cannot arrive carrying a
        different exemption than the spawn it resumes. The hop reads no Tool Search
        state at all, which is the point: there is nothing to disagree with.
        """
        import inspect

        code = inspect.getsource(apply_mandatory_mcps_env).split('"""')[-1]
        # It may import from the tool_search MODULE (that is where the env-var name
        # lives); what it must never do is read the SETTING or its thresholds.
        for state in ("ToolSearchSettings", "min_pct", "min_tokens", "enabled"):
            assert state not in code
        env: dict[str, str] = {}
        apply_mandatory_mcps_env(env)
        assert env[MANDATORY_MCPS_ENV]
