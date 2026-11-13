"""An MCP ``${NAME}`` placeholder matches its variable in any letter case on Windows.

Windows environment names are case-insensitive and ``os.environ`` yields them
upper-cased, so ``${SystemRoot}``, ``${env:Path}`` and ``${userprofile}`` name set
variables there. Each lookup against the filtered placeholder view (the declared-env
expander, the remote-probe headers and the session-start unset notice) folds the
name the same way; a credential-filtered name stays a miss in every spelling, and
POSIX names stay case-sensitive.

Linux and macOS runners cannot make ``os.environ`` case-insensitive, so these tests
set the module's platform switch and give each variable its upper-case spelling,
which is how Windows holds every name in the view.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.acp import mcp_ref_guard
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.mcp_discovery_remote import _expand_header_placeholders
from kiro_crew.mcp_gateway import rewriter_env
from kiro_crew.mcp_gateway.rewriter import _expand_env_map, _expand_env_placeholders

_LOGGER = "kiro_crew.mcp_gateway.rewriter"


@pytest.fixture
def windows_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # raising=False keeps a build without the switch an assertion failure, not a
    # setup error; a misspelled switch still fails, because nothing folds.
    monkeypatch.setattr(rewriter_env, "_ENV_NAMES_CASE_INSENSITIVE", True, raising=False)


@pytest.mark.usefixtures("windows_names")
def test_a_mixed_case_reference_resolves_and_logs_nothing(monkeypatch, caplog) -> None:
    monkeypatch.setenv("KC32704_ROOT", r"C:\Windows")
    monkeypatch.setenv("KC32704_PATH", r"C:\bin")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        out = _expand_env_map(
            {
                "A": r"${Kc32704_Root}\System32",
                "B": "${env:Kc32704_Path};x",
                "C": "${kc32704_root}",
                "D": "${KC32704_ROOT}",
            },
            server="probe",
        )
        single = _expand_env_placeholders("${env:kc32704_Root}")
    assert out == {
        "A": r"C:\Windows\System32",
        "B": r"C:\bin;x",
        "C": r"C:\Windows",
        "D": r"C:\Windows",
    }
    assert single == r"C:\Windows"
    assert "credential-filtered" not in caplog.text
    assert "is unset" not in caplog.text


@pytest.mark.usefixtures("windows_names")
@pytest.mark.parametrize(
    "var",
    [
        "AWS_SECRET_ACCESS_KEY",  # hashing.is_secret_env_key
        "AWS_ACCESS_KEY_ID",  # manager.is_credential_env_key
        "SLACK_BOT_TOKEN",  # sandbox.scrub_agent_denied_env
    ],
)
def test_a_credential_filtered_name_misses_in_every_spelling(monkeypatch, caplog, var) -> None:
    monkeypatch.setenv(var, "real-secret-value")
    spellings = [var, var.lower(), var.title()]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        out = _expand_env_map({str(i): f"${{env:{s}}}" for i, s in enumerate(spellings)})
    assert out == {str(i): f"${{{s}}}" for i, s in enumerate(spellings)}
    refusals = [r.getMessage() for r in caplog.records if "credential-filtered" in r.getMessage()]
    assert len(refusals) == len(spellings)
    assert "real-secret-value" not in caplog.text


@pytest.mark.usefixtures("windows_names")
def test_an_unset_mixed_case_name_is_logged_as_unset(monkeypatch, caplog) -> None:
    monkeypatch.delenv("KC32704_GONE", raising=False)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _expand_env_placeholders("${Kc32704_Gone}", server="srv") == "${Kc32704_Gone}"
    assert "is unset" in caplog.text
    assert "credential-filtered" not in caplog.text


def test_posix_names_stay_case_sensitive(monkeypatch, caplog) -> None:
    monkeypatch.setattr(rewriter_env, "_ENV_NAMES_CASE_INSENSITIVE", False, raising=False)
    monkeypatch.setenv("KC32704_ROOT", "/opt/root")
    monkeypatch.delenv("Kc32704_Root", raising=False)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert _expand_env_placeholders("${Kc32704_Root}") == "${Kc32704_Root}"
    assert "credential-filtered" not in caplog.text


@pytest.mark.usefixtures("windows_names")
def test_a_remote_probe_header_resolves_a_mixed_case_reference(monkeypatch) -> None:
    monkeypatch.setenv("KC32704_TENANT", "tenant-7")
    expanded, resolved = _expand_header_placeholders({"X-Tenant": "${env:Kc32704_Tenant}"})
    assert expanded == {"X-Tenant": "tenant-7"}
    assert resolved == ["tenant-7"]


@pytest.mark.usefixtures("windows_names")
def test_the_session_notice_does_not_name_a_set_mixed_case_variable(monkeypatch) -> None:
    monkeypatch.setenv("KC32704_ROOT", r"C:\Windows")
    monkeypatch.delenv("KC32704_GONE", raising=False)
    spec = {
        "mcpServers": {
            "srv": {
                "command": "npx",
                "env": {"A": "${Kc32704_Root}", "B": "${Kc32704_Gone}"},
            }
        }
    }
    found = mcp_ref_guard.warn_unset_env_placeholders(
        spec, [{"name": "srv"}], backend=ACP_BACKEND_KAS, agent="k", child_env=frozenset()
    )
    assert found == (["srv ('Kc32704_Gone')"], 0)
