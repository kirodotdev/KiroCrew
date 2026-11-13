"""The files kiro-cli's engine reads tool auto-approve rules from are write-protected.

A rule with ``effect: allow`` in one of these files lets a tool run with no permission
request, so the agent's file-edit tools must not write them. The fence is the file-tool
gate only: reads stay allowed, and a program that writes the file itself (the engine
saving a rule the user chose, the user's editor) never asks the gate.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from kiro_crew import deny_guidance, security
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.native_allow_stores import NATIVE_ALLOW_STORES_NONE
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.llm_helpers_parts.tool_input_denials import _edit_target_denial
from kiro_crew.security import is_sensitive_path, is_sensitive_write_path

#: One writable path per fenced leaf: the two user files, and a workspace's own file
#: of each name inside the folder the engine keeps them in.
_RULE_FILES = (
    ".kiro/settings/permissions.yaml",
    ".kiro/settings/permissions.json",
    ".kiro/workspace-roots/0123abcd/permissions.yaml",
    ".kiro/workspace-roots/0123abcd/permissions.json",
)
#: The harnesses that run kiro-cli's engine and so read these files.
_KIRO_ENGINE_BACKENDS = (ACP_BACKEND_KIRO, ACP_BACKEND_KAS)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home, so no test names a real user's file."""
    root = tmp_path / "home"
    root.mkdir()
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    security._home_targets_cache.clear()
    yield root
    security._home_targets_cache.clear()


def _under(root: Path, rel: str) -> str:
    return str(root.joinpath(*PurePosixPath(rel).parts))


def test_the_test_paths_cover_every_fenced_leaf() -> None:
    from kiro_crew.security.paths_lists import _KIRO_PERMISSION_RULE_LEAVES

    assert _KIRO_PERMISSION_RULE_LEAVES
    for leaf in _KIRO_PERMISSION_RULE_LEAVES:
        assert any(rel == leaf or rel.startswith(f"{leaf}/") for rel in _RULE_FILES), leaf


@pytest.mark.parametrize("backend", _KIRO_ENGINE_BACKENDS)
@pytest.mark.parametrize("rel", _RULE_FILES)
def test_a_file_edit_of_a_rules_file_is_refused(backend: str, rel: str, home: Path) -> None:
    # These harnesses have no row of their own: their reason says the files are fenced
    # by name, so the fence has to hold for the files this engine reads.
    assert "permissions" in NATIVE_ALLOW_STORES_NONE[backend]
    assert is_sensitive_write_path(_under(home, rel)) is True
    assert is_sensitive_write_path(f"~/{rel}") is True


@pytest.mark.parametrize("rel", _RULE_FILES)
def test_both_gates_refuse_the_edit(rel: str, home: Path) -> None:
    target = _under(home, rel)
    hit = _edit_target_denial({"path": target}, "", "edit")
    assert hit is not None and hit[0] == "path"
    assert hit[1] == f"Blocked: write to protected path: {target}"
    decision = HookManager(HooksConfig.from_dict({})).on_tool_call(
        "Editing a file",
        session_key="cli_chat",
        tool_kind="edit",
        raw_params={"command": "create", "fileText": "x"},
        diff_path=target,
    )
    assert decision.action == TOOL_DENY
    assert decision.reason == f"Blocked: modification of write-protected config path: {target}"


@pytest.mark.parametrize("rel", _RULE_FILES[:2])
def test_a_moved_kiro_home_is_covered(
    rel: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = tmp_path / "kirohome"
    monkeypatch.setenv("KIRO_HOME", str(moved))
    security._home_targets_cache.clear()
    tail = PurePosixPath(rel).parts[1:]
    assert is_sensitive_write_path(str(moved.joinpath(*tail))) is True
    assert is_sensitive_write_path(_under(home, rel)) is True


def test_other_files_in_the_settings_folder_stay_writable(home: Path) -> None:
    for rel in (
        ".kiro/settings",
        ".kiro/settings/cli.json",
        ".kiro/settings/kiroignore",
        ".kiro/settings/permissions.yaml.bak",
        ".kiro/settings/permissions.yml",
        ".kiro/settings-backup/permissions.yaml",
        ".kiro/workspace-roots-old/0123abcd/permissions.yaml",
        "project/.kiro/settings/permissions.yaml",
        "project/permissions.json",
    ):
        assert is_sensitive_write_path(_under(home, rel)) is False, rel


# ── the writers that must keep working ──


@pytest.mark.parametrize("rel", _RULE_FILES)
def test_reading_a_rules_file_stays_allowed(rel: str, home: Path) -> None:
    # ``kiro_cli_workspace_trust`` and the file-read tools read these.
    assert is_sensitive_path(_under(home, rel)) is False


@pytest.mark.parametrize("rel", _RULE_FILES)
def test_a_program_that_writes_the_file_itself_is_not_stopped(rel: str, home: Path) -> None:
    # The engine saving a rule the user chose, and the user's own editor, write with
    # their own file calls. The fence is a verdict on a tool call's path: it changes
    # nothing on disk, so a first write and a later replace both go through.
    target = Path(_under(home, rel))
    assert is_sensitive_write_path(str(target)) is True
    target.parent.mkdir(parents=True)
    target.write_text("rules: []\n", encoding="utf-8")
    staged = target.with_name(f"{target.name}.tmp")
    staged.write_text("rules:\n  - capability: mcp\n    effect: ask\n", encoding="utf-8")
    os.replace(staged, target)
    assert "effect: ask" in target.read_text(encoding="utf-8")
    assert os.access(target, os.W_OK)


# ── what the agent is told ──


@pytest.mark.parametrize("rel", (_RULE_FILES[0], ".kiro/settings/mcp.json"))
def test_each_gates_refusal_tells_the_agent_to_leave_the_edit_to_the_user(
    rel: str, home: Path
) -> None:
    # The edit gate answers first and the hook gate words the same refusal its own
    # way; both wordings must reach the guidance, for every write-protected path.
    target = _under(home, rel)
    hit = _edit_target_denial({"path": target}, "", "edit")
    assert hit is not None
    for reason in (hit[1], f"Blocked: modification of write-protected config path: {target}"):
        assert deny_guidance.classify_deny(reason, target) == deny_guidance.DENY_CLASS_TRUST_ROOT
        assert "let the user edit it themselves" in deny_guidance.remediation_for(reason, target)
