"""A claude launch asks claude-agent-acp for summarized thinking text.

Claude Code's recent models default the thinking display to ``omitted``: their
thinking blocks carry no text, and claude-agent-acp sends no ``agent_thought_chunk``
for an empty one (0.107.0 ``sdk-to-acp.js`` 218-232), so the chat shows no thinking.
The adapter reads ``CLAUDE_CODE_THINKING_DISPLAY`` from its own environment at
``session/new`` (``session-env.js`` ``resolveThinkingConfig``), so
``ClaudeLaunch.apply_spawn_env`` sets it to ``summarized`` (section 1) unless the
operator named it (section 2) or a Claude Code settings file decides thinking: turns it
off or sets its budget, which the adapter's explicit ``--thinking=adaptive`` would
override, or chooses its display, which the adapter's explicit display would override
(section 3).

Every settings path is under ``tmp_path``: the user directory through
``CLAUDE_CONFIG_DIR``, the managed directory patched, the project through the launch's
work directory, so no test reads the real home or the real managed path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _stop_stderr_drain

from kiro_crew.acp import claude_settings_sources
from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import claude as claude_harness
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKENDS_SUMMARIZED_THINKING,
)
from kiro_crew.testing.ids import UNALLOCATABLE_PID

DISPLAY = claude_harness.CLAUDE_THINKING_DISPLAY_ENV


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Empty user, project and managed directories, and no real ``claude`` lookup."""
    found = {name: tmp_path / name for name in ("user", "work", "managed")}
    for path in found.values():
        path.mkdir()
    monkeypatch.setattr(
        claude_settings_sources.platform_compat,
        "claude_code_managed_settings_dir",
        lambda: found["managed"],
    )
    monkeypatch.setattr(claude_harness, "_resolve_claude_code_executable", lambda: "/opt/claude")
    return found


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _launch_env(dirs: dict[str, Path], env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a launch for a session in ``dirs["work"]`` leaves."""
    launch = claude_harness.ClaudeLaunch()
    launch._work_dir = dirs["work"]
    spawn_env = {"CLAUDE_CONFIG_DIR": str(dirs["user"]), **(env or {})}
    launch.apply_spawn_env(spawn_env)
    return spawn_env


# ── 1. The default ─────────────────────────────────────────────────────────────


def test_the_names_are_the_ones_the_adapter_reads() -> None:
    assert DISPLAY == "CLAUDE_CODE_THINKING_DISPLAY"
    assert claude_harness.CLAUDE_THINKING_DISPLAY_DEFAULT == "summarized"
    assert ACP_BACKENDS_SUMMARIZED_THINKING == frozenset({ACP_BACKEND_CLAUDE})


def test_a_claude_spawn_asks_for_summarized_thinking(dirs: dict[str, Path]) -> None:
    env = _launch_env(dirs)
    assert env[DISPLAY] == "summarized"
    # Beside the checklist default and the executable, which it leaves as they were.
    assert env[claude_harness.CLAUDE_TODO_TOOLS_ENV] == "1"
    assert env["CLAUDE_CODE_EXECUTABLE"] == "/opt/claude"


def test_a_thinking_budget_in_the_spawn_environment_keeps_the_default(
    dirs: dict[str, Path],
) -> None:
    """The adapter reads both names from its own environment and sends the budget with
    the display, so a budget there decides nothing the display could undo."""
    env = _launch_env(dirs, {"MAX_THINKING_TOKENS": "4096"})
    assert env[DISPLAY] == "summarized"
    assert env["MAX_THINKING_TOKENS"] == "4096"


def test_settings_that_turn_thinking_on_keep_the_default(dirs: dict[str, Path]) -> None:
    _write(dirs["user"] / "settings.json", {"alwaysThinkingEnabled": True, "env": {"A": "1"}})
    assert _launch_env(dirs)[DISPLAY] == "summarized"


def test_a_backend_outside_the_set_gets_no_display(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_harness, "ACP_BACKENDS_SUMMARIZED_THINKING", frozenset())
    assert DISPLAY not in _launch_env(dirs)


# ── 2. The operator's value wins ───────────────────────────────────────────────


@pytest.mark.parametrize("operator_value", ["omitted", "", "summarized", "verbose"])
def test_the_operators_display_value_is_kept(dirs: dict[str, Path], operator_value: str) -> None:
    """An empty or unknown value is kept too: the operator named the variable."""
    assert _launch_env(dirs, {DISPLAY: operator_value})[DISPLAY] == operator_value


def test_the_operators_value_is_kept_without_reading_a_settings_file(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unread(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("no settings file is read when the operator named the display")

    monkeypatch.setattr(claude_harness, "a_source_decides_thinking", _unread)
    assert _launch_env(dirs, {DISPLAY: "omitted"})[DISPLAY] == "omitted"


# ── 3. A settings file that decides thinking ───────────────────────────────────


@pytest.mark.parametrize(
    "where",
    [
        ("user", "settings.json"),
        ("work", ".claude/settings.json"),
        ("work", ".claude/settings.local.json"),
        ("managed", "managed-settings.json"),
        ("managed", "managed-settings.d/10-thinking.json"),
    ],
)
@pytest.mark.parametrize(
    "payload",
    [
        {"alwaysThinkingEnabled": False},
        {"env": {"MAX_THINKING_TOKENS": "0"}},
        {"showThinkingSummaries": False},
        {"showThinkingSummaries": True},
        {"env": {DISPLAY: "omitted"}},
    ],
    ids=["thinking-off", "budget-in-env", "summaries-off", "summaries-on", "display-in-env"],
)
def test_a_settings_file_that_decides_thinking_gets_no_display(
    dirs: dict[str, Path], where: tuple[str, str], payload: dict
) -> None:
    """The adapter turns a display with no budget in its own environment into
    ``--thinking=adaptive``, which Claude Code obeys over thinking turned off or a
    budget, and hands Claude Code the display as an explicit choice, which Claude Code
    2.1.325 takes over ``showThinkingSummaries`` either way. A file whose ``env`` names
    the display is the operator choosing it too, though that value never reaches the
    adapter."""
    root, name = where
    _write(dirs[root] / name, payload)
    assert DISPLAY not in _launch_env(dirs)


def test_the_user_directory_follows_the_spawn_environment(
    dirs: dict[str, Path], tmp_path: Path
) -> None:
    other = tmp_path / "other-config"
    _write(other / "settings.json", {"alwaysThinkingEnabled": False})
    assert DISPLAY not in _launch_env(dirs, {"CLAUDE_CONFIG_DIR": str(other)})
    assert _launch_env(dirs)[DISPLAY] == "summarized"


def test_a_linked_project_file_is_refused_not_followed(
    dirs: dict[str, Path], tmp_path: Path
) -> None:
    """A cloned repository controls the project tier, so a link there is read as no file."""
    target = tmp_path / "elsewhere.json"
    _write(target, {"alwaysThinkingEnabled": False})
    link = dirs["work"] / ".claude" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    assert _launch_env(dirs)[DISPLAY] == "summarized"


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1, 2]",
        '{"env": "MAX_THINKING_TOKENS"}',
        '{"showThinkingSummaries": "yes"}',
        '{"showThinkingSummaries": null}',
    ],
)
def test_an_unreadable_or_odd_file_decides_nothing(dirs: dict[str, Path], raw: str) -> None:
    (dirs["user"] / "settings.json").write_text(raw, encoding="utf-8")
    assert _launch_env(dirs)[DISPLAY] == "summarized"


def test_a_launch_with_no_session_reads_no_project(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that never resolved a spawn has no work directory to read."""
    _write(dirs["work"] / ".claude" / "settings.json", {"alwaysThinkingEnabled": False})
    env = {"CLAUDE_CONFIG_DIR": str(dirs["user"])}
    claude_harness.ClaudeLaunch().apply_spawn_env(env)
    assert env[DISPLAY] == "summarized"


async def _call_factory(factory: Any) -> Any:
    return await factory()


async def _spawned_env(dirs: dict[str, Path]) -> dict[str, str]:
    """The environment ``AcpClient._spawn`` hands a claude session in ``dirs["work"]``."""
    client = AcpClient(
        work_dir=dirs["work"],
        acp_backend=ACP_BACKEND_CLAUDE,
        extra_env={"CLAUDE_CONFIG_DIR": str(dirs["user"])},
    )
    argv = ["/usr/bin/node", "/x/acp.js"]
    with (
        patch("kiro_crew.acp.client._resolve_claude_acp_bin", return_value=(argv, "")),
        patch("kiro_crew.acp.client.wrap_argv", return_value=(argv, None)),
        # The launch's own process seams, as ``acp_launch_capture`` stubs them: the
        # factory every host spawns through, and the Windows cleanup admission,
        # which would pin a real child's handle.
        patch.object(acp_client, "create_subprocess_limited", new_callable=AsyncMock) as mock_exec,
        patch.object(acp_client, "finish_suspended_spawn", return_value=True),
        patch.object(
            acp_client.platform_compat,
            "create_windows_cleanup_owned_process",
            side_effect=_call_factory,
        ),
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
    ):
        mock_proc = MagicMock()
        mock_proc.pid = UNALLOCATABLE_PID
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc
        await client._spawn()
        env = dict(mock_exec.call_args.kwargs["env"])
    await _stop_stderr_drain(client)
    return env


@pytest.mark.asyncio
async def test_a_session_spawn_reads_its_own_projects_settings(dirs: dict[str, Path]) -> None:
    """``resolve_spawn`` hands the launch the session's work directory, so the
    project tier the child will read is the one the default is judged by."""
    assert (await _spawned_env(dirs))[DISPLAY] == "summarized"
    _write(dirs["work"] / ".claude" / "settings.json", {"alwaysThinkingEnabled": False})
    assert DISPLAY not in await _spawned_env(dirs)
