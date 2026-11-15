"""The bundled audit hook on Windows, where Crew runs spec hooks through ``cmd /c``.

``cmd.exe`` cannot parse the POSIX hook in ``config/defaults.json``, so a spec
built on Windows carries :func:`kiro_crew.shell_audit_log.windows_audit_hook_command`
instead. These tests pin that the swap happens only on Windows, that the
Windows form's Python source survives ``cmd.exe`` quoting, and that it appends
the same one-line record the POSIX hook does.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import shell_audit_log
from kiro_crew.shell_audit_log import (
    _WINDOWS_AUDIT_HOOK_SOURCE,
    platform_spec_hooks,
    windows_audit_hook_command,
)
from kiro_crew.testing.wait import default_timeout

DEFAULTS_JSON = (
    Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config" / "defaults.json"
)
RECORD = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ BASH: (?P<payload>\{.*\})$")


def _bundled_hooks() -> dict:
    return json.loads(DEFAULTS_JSON.read_text(encoding="utf-8"))["hooks"]


def _audit_commands(hooks: dict) -> list[str]:
    return [h["command"] for h in hooks.get("postToolUse", []) if "audit.log" in h["command"]]


def test_posix_keeps_the_shipped_hook_byte_for_byte() -> None:
    hooks = _bundled_hooks()
    with patch.object(shell_audit_log.platform_compat, "IS_WINDOWS", False):
        assert platform_spec_hooks(hooks) == hooks


def test_windows_spec_build_carries_the_windows_form() -> None:
    from kiro_crew import agent

    with patch.object(shell_audit_log.platform_compat, "IS_WINDOWS", True):
        config = agent.build_agent_config(gated_off=frozenset())

    assert _audit_commands(config["hooks"]) == [windows_audit_hook_command(sys.executable)]


def test_windows_swap_leaves_other_hooks_alone() -> None:
    user_hook = {"matcher": "execute_bash", "command": "echo mine"}
    hooks = _bundled_hooks()
    hooks["postToolUse"] = [*hooks["postToolUse"], user_hook]
    with patch.object(shell_audit_log.platform_compat, "IS_WINDOWS", True):
        swapped = platform_spec_hooks(hooks)
    assert swapped["postToolUse"][-1] == user_hook
    assert swapped["postToolUse"][0]["matcher"] == "execute_bash"
    assert "${" not in swapped["postToolUse"][0]["command"]


def test_windows_source_survives_cmd_quoting() -> None:
    # A double quote would end the ``-c`` argument; a ``%`` is cmd's variable
    # expansion. Either one breaks the command under ``cmd /c``.
    assert '"' not in _WINDOWS_AUDIT_HOOK_SOURCE
    assert "%" not in _WINDOWS_AUDIT_HOOK_SOURCE
    assert "\n" not in _WINDOWS_AUDIT_HOOK_SOURCE


def _run_source(env: dict[str, str], cwd: Path, payload: str) -> None:
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", _WINDOWS_AUDIT_HOOK_SOURCE],
        input=payload.encode("utf-8"),
        env=env,
        cwd=cwd,
        check=True,
        timeout=default_timeout(),
    )


@pytest.mark.parametrize(
    "payload",
    [
        '{"tool_input":{"command":"ls"}}\n',
        '{\n  "tool_input": {"command": "echo a\\nb"}\n}\r\n',
    ],
    ids=["trailing-newline", "pretty-printed"],
)
def test_windows_form_appends_one_record_per_call(tmp_path: Path, payload: str) -> None:
    env = {**os.environ, "KIROCREW_HOME": str(tmp_path)}
    _run_source(env, tmp_path, payload)
    _run_source(env, tmp_path, payload)

    lines = (tmp_path / "audit.log").read_bytes().decode("utf-8").split("\n")
    assert lines[-1] == ""
    assert len(lines[:-1]) == 2
    for line in lines[:-1]:
        match = RECORD.match(line)
        assert match, line
        assert json.loads(match.group("payload")) == json.loads(payload)


def test_windows_form_defaults_to_the_home_data_dir(tmp_path: Path) -> None:
    crew = tmp_path / ".kiro" / "crew"
    crew.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_HOME"}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    _run_source(env, tmp_path, '{"a":1}')
    assert RECORD.match((crew / "audit.log").read_text(encoding="utf-8").rstrip("\n"))


@pytest.mark.skipif(os.name != "nt", reason="runs the command the way Windows does, via cmd /c")
def test_windows_form_runs_under_cmd(tmp_path: Path) -> None:
    env = {**os.environ, "KIROCREW_HOME": str(tmp_path)}
    # A string argv reaches CreateProcess verbatim, so this is the same
    # ``cmd.exe /c "<command>"`` line the hook runner's
    # ``create_subprocess_shell`` spawns.
    subprocess.run(
        f'cmd.exe /c "{windows_audit_hook_command(sys.executable)}"',
        input=b'{"tool_input":{"command":"echo hi"}}',
        env=env,
        cwd=tmp_path,
        check=True,
        timeout=default_timeout(),
    )
    line = (tmp_path / "audit.log").read_text(encoding="utf-8").rstrip("\n")
    assert RECORD.match(line), line


@pytest.mark.asyncio
@pytest.mark.parametrize("synced", [True, False], ids=["synced_spec", "unsynced_posix_spec"])
async def test_windows_hooks_page_tags_the_audit_hook_bundled(tmp_path: Path, synced: bool) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.hooks import api_kiro_hooks

    agents = tmp_path / "agents"
    agents.mkdir()
    with patch.object(shell_audit_log.platform_compat, "IS_WINDOWS", True):
        # An unsynced spec predates the swap and still holds the shipped POSIX command.
        spec_hooks = platform_spec_hooks(_bundled_hooks()) if synced else _bundled_hooks()
        (agents / "kirocrew.json").write_text(json.dumps({"hooks": spec_hooks}))
        app = web.Application()
        app.router.add_get("/api/kiro-hooks", api_kiro_hooks)
        with (
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents),
            patch(
                "kiro_crew.dashboard.handlers.hooks._shipped_defaults", return_value=DEFAULTS_JSON
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/kiro-hooks")
                assert resp.status == 200
                rows = (await resp.json())["hooks"]["postToolUse"]

    audit = [r for r in rows if "audit.log" in r["command"]]
    assert audit and all(r["source"] == "bundled" for r in audit)
