"""Crew MCP deferral follows the verified engine floor and operator override.

Older or unknown engines keep Crew's servers resident. A per-session overlay
cannot set or clear the list, and an ambient operator value wins verbatim.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import kiro_cli
from kiro_crew.acp.harness._common import MANDATORY_MCPS_ENV as _ENV
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.harness.kiro import KiroHarness


@pytest.fixture(autouse=True)
def quiet_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep credentials independent of the host."""
    monkeypatch.setattr(
        "kiro_crew.config.loader.inject_kiro_cli_api_key", lambda _env: None, raising=True
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.strip_kiro_cli_api_key", lambda _env: None, raising=True
    )


_HOSTS = pytest.mark.parametrize("harness", [KiroHarness, KasHarness], ids=["kiro", "kas"])


def test_the_name_is_the_one_kiro_cli_reads() -> None:
    assert _ENV == "ASBX_KIRO_MANDATORY_MCPS"


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        (None, False),
        ((2, 24, 0), False),
        ((2, 26, 0), False),
        ((2, 26, 1), False),
        ((2, 27, 0), True),
    ],
)
def test_deferral_version_floor(version, supported) -> None:
    assert kiro_cli.MANDATORY_MCPS_DROP_MIN_VERSION == (2, 27, 0)
    assert kiro_cli.mandatory_mcps_drop_supported(version) is supported


@_HOSTS
def test_spawn_env_names_no_exempt_servers(harness, monkeypatch) -> None:
    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: True)
    monkeypatch.delenv(_ENV, raising=False)
    env: dict[str, str] = {}
    harness().apply_spawn_env(env)
    assert _ENV not in env


@_HOSTS
def test_an_overlay_cannot_set_the_list(harness, monkeypatch) -> None:
    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: True)
    monkeypatch.delenv(_ENV, raising=False)
    env = {_ENV: "overlay-server"}
    harness().apply_spawn_env(env)
    assert _ENV not in env


@_HOSTS
@pytest.mark.parametrize("overlay", [None, "", "overlay-server"])
def test_older_or_unknown_engine_keeps_crew_servers(harness, overlay, monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: False)
    monkeypatch.setattr(
        agent_mod, "_MANAGED_MCP_SERVERS", {"kirocrew-z": {}, "kirocrew-work": {"opt_in": True}}
    )
    monkeypatch.setattr(
        agent_mod, "_extra_mcp_servers", lambda: {"edition-a": {}, "kirocrew-z": {}}
    )
    env = {} if overlay is None else {_ENV: overlay}
    harness().apply_spawn_env(env)
    assert env[_ENV] == "edition-a,kirocrew-work,kirocrew-z"


@_HOSTS
def test_empty_crew_server_set_removes_overlay(harness, monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: False)
    monkeypatch.setattr(agent_mod, "_MANAGED_MCP_SERVERS", {})
    monkeypatch.setattr(agent_mod, "_extra_mcp_servers", lambda: {})
    env = {_ENV: "overlay-server"}
    harness().apply_spawn_env(env)
    assert _ENV not in env


@_HOSTS
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("ambient", ["my-own-server", ""], ids=["named", "empty"])
def test_the_operator_value_wins_over_an_overlay(harness, allowed, ambient, monkeypatch) -> None:
    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: allowed)
    monkeypatch.setenv(_ENV, ambient)
    env = {_ENV: "overlay-server"}
    harness().apply_spawn_env(env)
    assert env[_ENV] == ambient


@_HOSTS
@pytest.mark.parametrize("spawned_binary", [None, "/spawned/kiro-cli"])
def test_operator_override_skips_version_probe(harness, spawned_binary, monkeypatch) -> None:
    def unexpected_probe(*_args):
        pytest.fail("An ambient override must not probe the engine")

    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", unexpected_probe)
    monkeypatch.setenv(_ENV, "")
    env = {_ENV: "overlay-server"}
    harness().apply_spawn_env(env, spawned_binary=spawned_binary)
    assert env[_ENV] == ""


@_HOSTS
@pytest.mark.parametrize("allowed", [True, False])
def test_spawn_env_passes_the_spawned_binary(harness, allowed, monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)

    def drop_allowed(binary):
        assert binary == "/spawned/kiro-cli"
        return allowed

    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", drop_allowed)
    env = {_ENV: "overlay-server"}
    harness().apply_spawn_env(env, spawned_binary="/spawned/kiro-cli")
    if allowed:
        assert _ENV not in env
    else:
        assert env[_ENV].split(",") == sorted(
            {*agent_mod._MANAGED_MCP_SERVERS, *agent_mod._extra_mcp_servers()}
        )


@_HOSTS
def test_spawn_env_keeps_real_opt_in_servers_resident(harness, monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(kiro_cli, "mandatory_mcps_drop_allowed", lambda _binary: False)
    env: dict[str, str] = {}
    harness().apply_spawn_env(env)
    for server in ("kirocrew-dashboard", "kirocrew-work", "kirocrew-crew-log"):
        assert server in env[_ENV].split(",")
        assert server not in agent_mod.emission_eligible_mcp_servers()


@pytest.mark.parametrize(
    "case",
    [
        "pinned",
        "elsewhere",
        "no-pin",
        "old-sibling",
        "spawned-sibling",
        "sibling-old",
        "symlink",
        "none",
        "empty",
        "pin-error",
    ],
)
def test_deferral_requires_the_pinned_install(case, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    install = tmp_path / "install"
    install.mkdir()
    pinned = install / "kiro-cli"
    pinned.write_text("executable")
    pinned.chmod(0o755)
    versions = {str(pinned): (2, 27, 0)}
    spawned = str(pinned)
    if case == "pinned" and sys.platform != "win32":
        sibling = install / "kiro-cli-chat"
        sibling.write_text("executable")
        sibling.chmod(0o755)
        versions[str(sibling)] = (2, 27, 0)
    if case in {"old-sibling", "spawned-sibling", "sibling-old"}:
        if sys.platform == "win32":
            pytest.skip("Windows has no chat sibling")
        sibling = install / "kiro-cli-chat"
        sibling.write_text("executable")
        sibling.chmod(0o755)
        versions[str(sibling)] = (2, 27, 0) if case == "spawned-sibling" else (2, 26, 0)
        if case in {"spawned-sibling", "sibling-old"}:
            spawned = str(sibling)
    if case in {"elsewhere", "symlink"}:
        other = tmp_path / "other" / "kiro-cli"
        other.parent.mkdir()
        if case == "symlink":
            if sys.platform == "win32":
                pytest.skip("Creating symlinks requires Windows privileges")
            other.symlink_to(pinned)
        spawned = str(other)
    if case == "none":
        spawned = None
    if case == "empty":
        spawned = ""
    monkeypatch.setattr(
        kiro_cli, "pin_kiro_cli", lambda: (None if case == "no-pin" else str(pinned), False)
    )
    if case == "pin-error":

        def broken_pin():
            raise OSError("pin unavailable")

        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", broken_pin)
    probed = []

    def probe(binary):
        if binary not in versions:
            pytest.fail(f"Untrusted version probe: {binary}")
        probed.append(binary)
        return versions[binary]

    monkeypatch.setattr(kiro_cli, "kiro_cli_version_at", probe)
    assert kiro_cli.mandatory_mcps_drop_allowed(spawned) is (case in {"pinned", "spawned-sibling"})
    probes_install = {"pinned", "old-sibling", "spawned-sibling", "sibling-old"}
    assert probed == (list(versions) if case in probes_install else [])


class TestTheServerSet:
    def test_it_names_the_opt_in_servers_the_eligible_set_drops(self) -> None:
        """The whole reason this set exists rather than reusing
        ``emission_eligible_mcp_servers``.

        An ``opt_in`` server the user granted is in their spec serving tools, so it
        churns the ``tools`` array exactly like an always-on one. The eligible set
        answers a different question ("would a rebuild re-add it") and drops every
        one of them, which would leave the granted ones deferring.
        """
        owned = agent_mod.crew_owned_mcp_servers()
        for opt_in in ("kirocrew-dashboard", "kirocrew-work", "kirocrew-crew-log"):
            assert opt_in in owned
            assert opt_in not in agent_mod.emission_eligible_mcp_servers()

    def test_it_names_the_always_on_servers_too(self) -> None:
        assert {"kirocrew-core", "kirocrew-cron"} <= agent_mod.crew_owned_mcp_servers()

    def test_it_is_a_superset_of_what_a_rebuild_would_emit(self) -> None:
        """Erring wide is free (a name for an absent server matches no tool); erring
        narrow is the defect, so the owned set may never be the smaller one."""
        assert agent_mod.emission_eligible_mcp_servers() <= agent_mod.crew_owned_mcp_servers()

    def test_it_carries_the_edition_seam_and_claims_no_prefix(self) -> None:
        """The composition IS the contract, and asserting a ``kirocrew-`` prefix
        instead would be vacuous.

        The set is the managed map plus the edition adapter's extras. The public
        build's adapter returns nothing, so a prefix assertion would pass without
        ever seeing an extra — and the seam does not constrain its keys, so a caller
        may not assume one. Pin what is actually promised.
        """
        from kiro_crew.agent import _MANAGED_MCP_SERVERS, _extra_mcp_servers

        assert agent_mod.crew_owned_mcp_servers() == frozenset(
            (*_MANAGED_MCP_SERVERS, *_extra_mcp_servers())
        )


def test_operator_help_states_the_floor_and_the_override() -> None:
    """The agent.tool_search help names the measured floor and the recovery knob."""
    import dataclasses

    from kiro_crew.config.sections import AgentConfig

    help_text = next(
        f.metadata["help"] for f in dataclasses.fields(AgentConfig) if f.name == "tool_search"
    )
    floor = ".".join(str(part) for part in kiro_cli.MANDATORY_MCPS_DROP_MIN_VERSION)
    assert f">= {floor}" in help_text
    assert "pinned kiro-cli install or its kiro-cli-chat, and both are" in help_text
    assert _ENV in help_text


def test_operator_kiro_bin_override_refuses_deferral_without_probing(tmp_path, monkeypatch) -> None:
    pinned = tmp_path / "kiro-cli"
    pinned.write_text("executable")
    pinned.chmod(0o755)
    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(pinned))
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(pinned), False))

    def probe(_binary):
        pytest.fail("An operator override must not be probed")

    monkeypatch.setattr(kiro_cli, "kiro_cli_version_at", probe)
    assert kiro_cli.mandatory_mcps_drop_allowed(str(pinned)) is False


def test_posix_launcher_without_chat_sibling_refuses_deferral(tmp_path, monkeypatch) -> None:
    if sys.platform == "win32":
        pytest.skip("Windows ships one self-contained kiro-cli.exe")
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    pinned = tmp_path / "kiro-cli"
    pinned.write_text("executable")
    pinned.chmod(0o755)
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(pinned), False))

    def probe(_binary):
        pytest.fail("A launcher without its chat sibling must not be probed")

    monkeypatch.setattr(kiro_cli, "kiro_cli_version_at", probe)
    assert kiro_cli.mandatory_mcps_drop_allowed(str(pinned)) is False
