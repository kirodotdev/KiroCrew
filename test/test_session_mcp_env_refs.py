"""``${VAR}`` / ``${env:VAR}`` in a session-array server's env, per harness.

Each harness below takes Crew's projected ``mcpServers`` array and starts the server
with the element's env as written; measured against the real binaries, none of them
but Claude Code (``${VAR}`` only) expands a reference itself. So Crew resolves both
forms the way kiro-cli does -- except on goose, which writes each server's env into
its own session database, so the reference stays unresolved there rather than the
value landing on disk.
"""

from __future__ import annotations

import pytest
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import _by_name, _write_spec

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_STRANDS,
)
from kiro_crew.providers.mirrors import mirror_for

_VALUE = "probe-value-7f3a"

#: The probe matrix: how the spec spells the env value.
_FORMS = {
    "literal": _VALUE,
    "dollar": "${PROBE_REF_VAR}",
    "env_prefix": "${env:PROBE_REF_VAR}",
}

_EXPANDING = (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_STRANDS,
)


def _project(backend: str, agents_dir, monkeypatch, value: str) -> dict:
    monkeypatch.setenv("PROBE_REF_VAR", _VALUE)
    _write_spec(
        agents_dir,
        servers={"probe": {"command": "/bin/probe", "env": {"PROBE_SETTING": value}}},
        tools=["@probe"],
    )
    projection = mirror_for(backend).session_projection(
        "kirocrew", permission_surface_owned=True, session_key="dashboard:t"
    )
    return _by_name(projection.params.get("mcpServers") or [])


@pytest.mark.parametrize("form", sorted(_FORMS))
@pytest.mark.parametrize("backend", _EXPANDING)
def test_the_server_receives_the_resolved_value(backend, form, agents_dir, monkeypatch):
    probe = _project(backend, agents_dir, monkeypatch, _FORMS[form])["probe"]
    assert probe["env"] == [{"name": "PROBE_SETTING", "value": _VALUE}]


@pytest.mark.parametrize("form", ["dollar", "env_prefix"])
def test_goose_keeps_the_reference_off_its_session_database(form, agents_dir, monkeypatch):
    probe = _project(ACP_BACKEND_GOOSE, agents_dir, monkeypatch, _FORMS[form])["probe"]
    assert probe["env"] == [{"name": "PROBE_SETTING", "value": _FORMS[form]}]


def test_goose_keeps_a_literal(agents_dir, monkeypatch):
    probe = _project(ACP_BACKEND_GOOSE, agents_dir, monkeypatch, _VALUE)["probe"]
    assert probe["env"] == [{"name": "PROBE_SETTING", "value": _VALUE}]


@pytest.mark.parametrize("backend", _EXPANDING)
def test_a_credential_named_variable_is_never_dereferenced(backend, agents_dir, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    _write_spec(
        agents_dir,
        servers={"probe": {"command": "/bin/probe", "env": {"X": "${env:AWS_SECRET_ACCESS_KEY}"}}},
        tools=["@probe"],
    )
    projection = mirror_for(backend).session_projection(
        "kirocrew", permission_surface_owned=True, session_key="dashboard:t"
    )
    probe = _by_name(projection.params.get("mcpServers") or [])["probe"]
    assert probe["env"] == [{"name": "X", "value": "${AWS_SECRET_ACCESS_KEY}"}]


def test_an_unset_variable_stays_literal(agents_dir, monkeypatch):
    monkeypatch.delenv("PROBE_UNSET_VAR", raising=False)
    _write_spec(
        agents_dir,
        servers={"probe": {"command": "/bin/probe", "env": {"X": "${PROBE_UNSET_VAR}"}}},
        tools=["@probe"],
    )
    projection = mirror_for(ACP_BACKEND_OPENCODE).session_projection(
        "kirocrew", permission_surface_owned=True
    )
    probe = _by_name(projection.params.get("mcpServers") or [])["probe"]
    assert probe["env"] == [{"name": "X", "value": "${PROBE_UNSET_VAR}"}]


def test_goose_never_restores_a_spec_command_on_crews_own_server(agents_dir, monkeypatch):
    """A spec entry named like Crew's control plane, with an env reference, must not
    bring its own command back onto the element that carries the session identity."""
    monkeypatch.setenv("PROBE_REF_VAR", _VALUE)
    _write_spec(
        agents_dir,
        servers={"kirocrew-core": {"command": "/tmp/not-crew", "env": {"X": "${PROBE_REF_VAR}"}}},
        tools=["@kirocrew-core"],
    )
    projection = mirror_for(ACP_BACKEND_GOOSE).session_projection(
        "kirocrew", permission_surface_owned=True, session_key="dashboard:t"
    )
    core = _by_name(projection.params.get("mcpServers") or [])["kirocrew-core"]
    assert core["command"] != "/tmp/not-crew"
