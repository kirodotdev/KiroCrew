"""A project spec that declares a managed agent name refuses the kiro spawn.

kiro-cli resolves ``--agent`` against the session's cwd before the installed agents
directory, so a checkout carrying a spec under a name Kiro Crew writes would run in
place of the installed one. These tests pin the decision function and the kiro
harness's enforcement of it; ``test_agent_admission_paths_ratchet.py`` pins every
spawn path that must call it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import managed_agent_shadow as managed_shadow
from kiro_crew.acp.harness import SpawnContext, harness_for
from kiro_crew.acp.managed_agent_shadow import (
    SHADOW_REFUSED_AGENT_NAMES,
    managed_agent_shadow_refusal,
)
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES, WORKER_AGENT_FILENAME


def _project_spec(project: Path, filename: str, body: dict | str) -> Path:
    path = project / ".kiro" / "agents" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    path.write_text(text, encoding="utf-8")
    return path


def test_every_managed_name_but_the_worker_is_covered():
    stems = {name[: -len(".json")] for name in OWNED_KIRO_AGENT_FILES}
    assert SHADOW_REFUSED_AGENT_NAMES == stems - {WORKER_AGENT_FILENAME[: -len(".json")]}
    assert "kirocrew" in SHADOW_REFUSED_AGENT_NAMES


@pytest.mark.parametrize("agent", sorted(SHADOW_REFUSED_AGENT_NAMES))
def test_a_project_copy_of_a_managed_name_is_refused(agent, tmp_path):
    shadow = _project_spec(
        tmp_path, f"{agent}.json", {"name": agent, "allowedTools": ["execute_bash"]}
    )
    refusal = managed_agent_shadow_refusal(agent, tmp_path)
    assert refusal is not None
    assert str(shadow) in refusal
    assert "Remove that project spec" in refusal


def test_a_project_file_declaring_the_name_under_another_filename_is_refused(tmp_path):
    _project_spec(tmp_path, "anything.json", {"name": "kirocrew"})
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is not None


def test_a_project_file_named_after_the_agent_is_refused_whatever_it_declares(tmp_path):
    """kiro-cli also selects a file by its stem, so the declared name cannot hide it."""
    shadow = _project_spec(tmp_path, "kirocrew.json", {"name": "other", "allowedTools": ["*"]})
    refusal = managed_agent_shadow_refusal("kirocrew", tmp_path)
    assert refusal is not None and str(shadow) in refusal


def test_a_session_in_the_home_directory_does_not_shadow_itself(tmp_path, monkeypatch):
    """With cwd at home, the checkout's agents directory IS the installed one."""
    installed = tmp_path / ".kiro" / "agents"
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: installed)
    _project_spec(tmp_path, "kirocrew.json", {"name": "kirocrew"})
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is None


def test_a_file_the_discovery_roster_hides_is_still_seen(tmp_path):
    """The roster omits skill-view aliases; kiro-cli does not, so neither may this."""
    _project_spec(tmp_path, "kirocrew-skill-view-x.json", {"name": "kirocrew", "tools": ["*"]})
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is not None


def test_a_markdown_file_declaring_the_name_is_refused(tmp_path):
    _project_spec(tmp_path, "notes.md", "---\nname: kirocrew\n---\nprompt\n")
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is not None


def test_a_protected_checkout_is_not_scanned(tmp_path, monkeypatch):
    from kiro_crew.acp import managed_agent_shadow as mod

    _project_spec(tmp_path, "kirocrew.json", {"name": "kirocrew"})
    monkeypatch.setattr(mod, "is_sensitive_path", lambda path: True)
    monkeypatch.setattr(mod, "_project_spec_files", lambda d: pytest.fail("scanned"))
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink loop")
def test_an_unstatable_entry_does_not_hide_the_shadow(tmp_path):
    """A self-looping symlink makes its stat raise; the other entries still count."""
    shadow = _project_spec(tmp_path, "kirocrew.json", {"name": "kirocrew"})
    loop = shadow.parent / "loop.json"
    loop.symlink_to(loop)
    refusal = managed_agent_shadow_refusal("kirocrew", tmp_path)
    assert refusal is not None and str(shadow) in refusal


@pytest.mark.parametrize("agent", ["kirocrew", "reviewer"])
def test_a_project_file_claiming_a_skill_view_alias_is_refused_for_any_agent(agent, tmp_path):
    """The spawn may hand kiro-cli an alias rather than the agent's own name."""
    _project_spec(tmp_path, "reviewer.json", {"name": "reviewer"})
    _project_spec(tmp_path, "x.json", {"name": "kirocrew-skill-view-0123", "tools": ["*"]})
    refusal = managed_agent_shadow_refusal(agent, tmp_path)
    assert refusal is not None and "'kirocrew-skill-view-0123'" in refusal


def test_a_project_file_named_as_a_skill_view_alias_is_refused(tmp_path):
    _project_spec(tmp_path, "kirocrew-skill-view-abc.json", {"name": "anything"})
    assert managed_agent_shadow_refusal("reviewer", tmp_path) is not None


def test_a_claimed_managed_name_refuses_whichever_agent_the_spawn_names(tmp_path):
    """kiro-cli loads every project agent, and a shared runtime can switch to one."""
    _project_spec(tmp_path, "kirocrew-lite.json", {"name": "kirocrew-lite", "tools": ["*"]})
    _project_spec(tmp_path, "reviewer.json", {"name": "reviewer"})
    refusal = managed_agent_shadow_refusal("reviewer", tmp_path)
    assert refusal is not None and "'kirocrew-lite'" in refusal


def test_a_nested_file_declaring_a_managed_name_is_refused(tmp_path):
    """kiro-cli loads nested agent directories too."""
    _project_spec(tmp_path, "team/x.json", {"name": "kirocrew"})
    assert managed_agent_shadow_refusal("reviewer", tmp_path) is not None


def test_a_managed_name_in_another_case_is_refused(tmp_path):
    _project_spec(tmp_path, "KIROCREW-LITE.json", {"name": "KIROCREW-LITE"})
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is not None


def test_an_agents_tree_too_large_to_check_is_refused(tmp_path, monkeypatch):
    from kiro_crew.acp import managed_agent_shadow as mod

    monkeypatch.setattr(mod, "_MAX_SPEC_FILES", 2)
    for i in range(3):
        _project_spec(tmp_path, f"a{i}.json", {"name": f"a{i}"})
    refusal = managed_agent_shadow_refusal("reviewer", tmp_path)
    assert refusal is not None and "too many to check" in refusal


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hardlink")
def test_a_spec_the_hardened_reader_refuses_is_not_read_as_its_filename(tmp_path):
    """A hardlinked spec cannot be read, so its declaration cannot be ruled out."""
    shadow = _project_spec(tmp_path, "anything.json", {"name": "kirocrew", "tools": ["*"]})
    (tmp_path / "outside.json").hardlink_to(shadow)
    refusal = managed_agent_shadow_refusal("reviewer", tmp_path)
    assert refusal is not None and str(shadow) in refusal
    assert "could not be read" in refusal


def test_a_spec_that_does_not_parse_refuses_until_it_is_fixed(tmp_path):
    spec = _project_spec(tmp_path, "broken.json", "{not json")
    refusal = managed_agent_shadow_refusal("reviewer", tmp_path)
    assert refusal is not None and "could not be read" in refusal and str(spec) in refusal


@pytest.mark.parametrize("filename", ["kirocrew.JSON", "kirocrew.Md", "x.JSON"])
def test_a_spec_suffix_in_another_case_is_still_a_spec(filename, tmp_path):
    _project_spec(tmp_path, filename, {"name": "kirocrew"} if "JSON" in filename else "x")
    assert managed_agent_shadow_refusal("reviewer", tmp_path) is not None


def test_a_markdown_project_copy_is_refused(tmp_path):
    _project_spec(tmp_path, "kirocrew-lite.md", "---\nname: kirocrew-lite\n---\nprompt\n")
    assert managed_agent_shadow_refusal("kirocrew-lite", tmp_path) is not None


def test_a_project_copy_that_only_removes_restrictions_is_refused_too(tmp_path):
    """The refusal does not weigh the copy's grants: a looser copy is still not ours."""
    _project_spec(tmp_path, "kirocrew.json", {"name": "kirocrew", "tools": []})
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is not None


def test_a_project_agent_under_its_own_name_is_admitted(tmp_path):
    _project_spec(tmp_path, "reviewer.json", {"name": "reviewer"})
    assert managed_agent_shadow_refusal("reviewer", tmp_path) is None


def test_a_managed_name_with_no_project_copy_is_admitted(tmp_path):
    _project_spec(tmp_path, "reviewer.json", {"name": "reviewer"})
    assert managed_agent_shadow_refusal("kirocrew", tmp_path) is None


def test_the_worker_is_left_to_its_own_gate(tmp_path):
    _project_spec(tmp_path, WORKER_AGENT_FILENAME, {"name": "kirocrew-worker"})
    assert managed_agent_shadow_refusal("kirocrew-worker", tmp_path) is None


@pytest.mark.parametrize("work_dir", [None, ""])
def test_no_work_dir_has_nothing_to_shadow(work_dir):
    assert managed_agent_shadow_refusal("kirocrew", work_dir) is None


def test_no_agent_is_admitted(tmp_path):
    assert managed_agent_shadow_refusal(None, tmp_path) is None


# ── Enforcement on the kiro harness ──


@pytest.fixture
def kiro_spawn_ready(monkeypatch):
    """Every other pre-spawn step answers "go", so only the shadow check can refuse."""

    async def _bin(*, environ, home):
        return "/pinned/kiro-cli"

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(
        sandbox_mod, "delegated_workspace_exposes_sealed_target", lambda work_dir: ""
    )


def _ctx(work_dir: Path, agent: str) -> SpawnContext:
    return SpawnContext(
        agent=agent, work_dir=str(work_dir), model=None, environ={}, home=Path(work_dir)
    )


@pytest.mark.asyncio
async def test_the_kiro_harness_refuses_a_shadowed_managed_agent(kiro_spawn_ready, tmp_path):
    from kiro_crew.acp.session_handle import AcpRuntimeError

    _project_spec(tmp_path, "kirocrew.json", {"name": "kirocrew", "allowedTools": ["*"]})
    with pytest.raises(AcpRuntimeError, match="declares its own 'kirocrew' agent spec"):
        await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path, "kirocrew"))


@pytest.mark.asyncio
async def test_the_kiro_harness_asks_about_the_spawn_cwd(kiro_spawn_ready, monkeypatch, tmp_path):
    seen: list[tuple[str, str]] = []

    def _record(agent, work_dir):
        seen.append((agent, work_dir))
        return None

    monkeypatch.setattr(managed_shadow, "managed_agent_shadow_refusal", _record)
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path, "kirocrew"))
    assert seen == [("kirocrew", str(tmp_path))]
    assert plan.argv[:4] == ["/pinned/kiro-cli", "acp", "--agent", "kirocrew"]


@pytest.mark.asyncio
async def test_the_kiro_harness_admits_a_project_agent_of_its_own(kiro_spawn_ready, tmp_path):
    _project_spec(tmp_path, "reviewer.json", {"name": "reviewer"})
    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(_ctx(tmp_path, "reviewer"))
    assert plan.argv[:4] == ["/pinned/kiro-cli", "acp", "--agent", "reviewer"]


def test_the_direct_client_refuses_a_shadowed_managed_agent(tmp_path):
    """The other kiro-cli spawn path, driven through ``AcpClient._spawn`` itself."""
    import acp_launch_capture as capture_mod

    _project_spec(tmp_path / "workspace", "kirocrew.json", {"name": "kirocrew"})
    with pytest.raises(client_mod.AcpError, match="declares its own 'kirocrew' agent spec"):
        capture_mod.capture(ACP_BACKEND_KIRO, tmp_path)


def test_the_direct_client_admits_a_checkout_without_a_shadow(tmp_path):
    import acp_launch_capture as capture_mod

    _project_spec(tmp_path / "workspace", "reviewer.json", {"name": "reviewer"})
    answers = capture_mod.capture(ACP_BACKEND_KIRO, tmp_path)
    assert "--agent" in answers["argv"]
