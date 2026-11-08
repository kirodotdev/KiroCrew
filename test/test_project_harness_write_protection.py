"""A project's own opencode and pi config is write-protected for the file-edit tools.

Each harness reads these from the session's work dir or a parent when it starts
(``agent_sdk.native_allow_stores.PROJECT_ALLOW_STORES``). Reads stay allowed.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.security import is_sensitive_path, is_sensitive_write_path

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

_REFUSED = (
    "opencode.json",
    "opencode.jsonc",
    "sub/dir/opencode.json",
    ".opencode",
    ".opencode/opencode.json",
    ".opencode/opencode.jsonc",
    ".opencode/plugin/hook.ts",
    ".opencode/plugins/hook.ts",
    ".opencode/agent/build.md",
    ".opencode/tool/run.ts",
    ".opencode/package.json",
    ".pi/settings.json",
    ".pi/mcp.json",
    ".pi/extensions",
    ".pi/extensions/ext.ts",
    ".pi/extensions/pkg/index.ts",
    ".OpenCode/OpenCode.json",
    ".PI/Settings.json",
)

_ALLOWED = (
    "opencode.json.bak",
    "notes/opencode.md",
    ".opencode-notes/x.json",
    "my.opencode/x.json",
    ".pi/notes.md",
    ".pi/skills/s/SKILL.md",
    ".pi/extensions.md",
    "pi/settings.json",
    "settings.json",
    "src/app.py",
)


def _under(root: Path, rel: str) -> str:
    return str(root.joinpath(*PurePosixPath(rel).parts))


@pytest.mark.parametrize("rel", _REFUSED)
def test_project_config_is_refused_in_any_directory(rel: str, tmp_path: Path) -> None:
    project = tmp_path / "some" / "project"
    assert is_sensitive_write_path(_under(project, rel)), rel
    assert is_sensitive_write_path(rel, str(project)), rel
    assert not is_sensitive_path(_under(project, rel)), rel


@pytest.mark.parametrize("rel", _ALLOWED)
def test_neighbours_are_not_over_blocked(rel: str, tmp_path: Path) -> None:
    assert not is_sensitive_write_path(_under(tmp_path / "project", rel)), rel


def test_the_edit_tool_is_denied_and_the_read_is_not(tmp_path: Path) -> None:
    target = _under(tmp_path, ".opencode/plugin/hook.ts")
    hooks = HookManager(HooksConfig.from_dict({}))

    def call(kind: str):
        return hooks.on_tool_call(
            "Editing", session_key="cli_chat", tool_kind=kind, raw_params={"path": target}
        )

    assert call("edit").action == TOOL_DENY
    assert call("read").action != TOOL_DENY
