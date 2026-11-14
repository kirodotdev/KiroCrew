"""The claude harness asks claude-agent-acp for summarized thinking text.

Recent models default ``thinking.display`` to ``omitted``, so without
``CLAUDE_CODE_THINKING_DISPLAY`` the adapter streams no ``agent_thought_chunk``
and the chat shows no thinking. The default must never override a value
the operator already set, inherited or through ``agent.env``.

An adapter below the release that reads the variable still shows no thinking, and
the installers leave an adapter already on PATH alone, so a new chat on such an
adapter gets one notice row naming the installed version, the minimum and the
upgrade command.
"""

from types import SimpleNamespace

import pytest

from kiro_crew.acp.client_claude_session import (
    CLAUDE_ACP_THINKING_DISPLAY_MIN_VERSION,
    _adapter_version_at_least,
)
from kiro_crew.acp.client_turn_control import thinking_display_version_issue
from kiro_crew.acp.harness import claude as harness_claude
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

CLAUDE = sdk_backends.ACP_BACKEND_CLAUDE


@pytest.fixture
def launch(monkeypatch):
    monkeypatch.setattr(harness_claude, "_resolve_claude_code_executable", lambda: "/bin/claude")
    return harness_claude.ClaudeLaunch()


def test_summarized_thinking_is_requested_by_default(launch):
    env: dict[str, str] = {}
    launch.apply_spawn_env(env)
    assert env["CLAUDE_CODE_THINKING_DISPLAY"] == "summarized"


@pytest.mark.parametrize("value", ["omitted", "summarized", ""])
def test_an_operator_value_wins(launch, value):
    env = {"CLAUDE_CODE_THINKING_DISPLAY": value}
    launch.apply_spawn_env(env)
    assert env["CLAUDE_CODE_THINKING_DISPLAY"] == value


def test_it_is_set_when_the_claude_executable_is_already_named(launch):
    # The executable override returns early; the thinking default must not ride on it.
    env = {"CLAUDE_CODE_EXECUTABLE": "/opt/claude"}
    launch.apply_spawn_env(env)
    assert env["CLAUDE_CODE_THINKING_DISPLAY"] == "summarized"
    assert env["CLAUDE_CODE_EXECUTABLE"] == "/opt/claude"


def test_it_is_set_when_no_claude_executable_resolves(monkeypatch):
    monkeypatch.setattr(harness_claude, "_resolve_claude_code_executable", lambda: None)
    env: dict[str, str] = {}
    harness_claude.ClaudeLaunch().apply_spawn_env(env)
    assert env["CLAUDE_CODE_THINKING_DISPLAY"] == "summarized"
    assert "CLAUDE_CODE_EXECUTABLE" not in env


def test_the_floor_is_the_release_that_reads_the_variable():
    assert CLAUDE_ACP_THINKING_DISPLAY_MIN_VERSION == (0, 99, 0)


@pytest.mark.parametrize("version", ["0.93.0", "0.98.0", "0.97.1-preview.7"])
def test_below_the_floor_names_installed_minimum_and_fix(version):
    issue = thinking_display_version_issue(CLAUDE, version)
    assert version in issue
    assert "older than 0.99.0" in issue
    assert "npm i -g @agentclientprotocol/claude-agent-acp" in issue


@pytest.mark.parametrize("version", ["0.99.0", "0.99.1", "0.100.0", "0.99.0+build.7"])
def test_at_or_above_the_floor_gets_no_notice(version):
    assert thinking_display_version_issue(CLAUDE, version) == ""


@pytest.mark.parametrize("version", ["", "unknown", "dev"])
def test_an_unknown_version_gets_no_notice(version):
    assert thinking_display_version_issue(CLAUDE, version) == ""


@pytest.mark.parametrize("backend", sorted(set(sdk_backends.ACP_BACKENDS_KNOWN) - {CLAUDE}))
def test_other_harnesses_get_no_notice(backend):
    assert thinking_display_version_issue(backend, "0.0.1") == ""


@pytest.mark.parametrize("version", ["0.98.9", "0.99.0", "0.99.0-rc.1", "0.100.0"])
def test_the_notice_agrees_with_the_floor(version):
    old = not _adapter_version_at_least(version, CLAUDE_ACP_THINKING_DISPLAY_MIN_VERSION)
    assert bool(thinking_display_version_issue(CLAUDE, version)) is old


def _provider_notice(backend, version):
    inner = SimpleNamespace(backend=backend, agent_version=version)
    return AcpProvider.thinking_upgrade_notice.fget(
        SimpleNamespace(_client=inner, agent_version=version)
    )


def test_the_provider_reads_the_session_handshake():
    assert "0.99.0" in _provider_notice(CLAUDE, "0.93.0")
    assert _provider_notice(CLAUDE, "0.99.0") == ""
    assert _provider_notice(CLAUDE, "") == ""


def test_a_new_session_below_the_floor_gets_one_notice_row(monkeypatch):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    notice = thinking_display_version_issue(CLAUDE, "0.93.0")
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(thinking_upgrade_notice=notice)
    )
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "0.99.0" in rows[0][3] and "claude-agent-acp" in rows[0][3]
