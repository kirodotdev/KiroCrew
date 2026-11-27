"""A project's own MCP files: refused to the agent's file-edit tool, sealed in its sandbox.

The name match in any folder, the remedy the refusal names, and the read-only hold
the sandbox puts on the session folder's files.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.project_mcp_files import (
    CLAUDE_PROJECT_MCP_FILE,
    KIRO_PROJECT_MCP_FILE,
    project_mcp_targets,
    project_mcp_write,
)
from kiro_crew.security.paths import sensitive_write_path_refusal

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="creates symlinks")


@pytest.mark.parametrize(
    "rel",
    [".mcp.json", ".kiro/settings/mcp.json", "sub/dir/.mcp.json", "x/.KIRO/Settings/MCP.json"],
)
def test_a_project_mcp_file_in_any_folder_is_refused_and_names_request_server(tmp_path, rel):
    reason = sensitive_write_path_refusal(rel, str(tmp_path))
    assert reason is not None and reason.startswith("Blocked: write to protected path")
    assert "request_server" in reason


@pytest.mark.parametrize("rel", ["mcp.json", ".kiro/settings/other.json", "my.mcp.json.bak"])
def test_other_names_are_not_project_mcp_files(tmp_path, rel):
    assert not project_mcp_write(rel, str(tmp_path))
    assert sensitive_write_path_refusal(rel, str(tmp_path)) is None


@_POSIX_ONLY
def test_a_link_to_a_project_mcp_file_is_refused_too(tmp_path):
    (tmp_path / ".mcp.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.json").symlink_to(tmp_path / ".mcp.json")
    assert project_mcp_write("notes.json", str(tmp_path))


def test_the_folder_seal_lists_both_names_even_when_absent(tmp_path):
    targets = project_mcp_targets(str(tmp_path))
    assert str(tmp_path / KIRO_PROJECT_MCP_FILE) in targets
    assert str(tmp_path / CLAUDE_PROJECT_MCP_FILE) in targets
    assert project_mcp_targets("relative/folder") == ()
    assert project_mcp_targets(str(tmp_path / "missing")) == ()


@_POSIX_ONLY
@pytest.mark.parametrize("link", [".", "..", "dir"])
def test_a_name_that_reaches_a_folder_is_never_sealed(tmp_path, link):
    # A cloned repo can carry `.mcp.json -> .`; sealing that would make the folder
    # read-only, so the name is skipped and the folder stays writable.
    project = tmp_path / "project"
    (project / "dir").mkdir(parents=True)
    (project / ".mcp.json").symlink_to(link)
    (project / ".kiro" / "settings" / "mcp.json").mkdir(parents=True)
    targets = project_mcp_targets(str(project))
    assert targets == ()


@_POSIX_ONLY
def test_a_folder_reached_through_a_link_seals_its_resolved_names_too(tmp_path):
    # Seatbelt matches the resolved path (/tmp -> /private/tmp, a cloud drive), so an
    # absent name must be listed under the folder's real path as well.
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    targets = project_mcp_targets(str(linked))
    for name in (KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE):
        assert str(linked / name) in targets
        assert os.path.join(os.path.realpath(real), name) in targets


@_POSIX_ONLY
def test_an_absent_file_behind_a_linked_settings_folder_is_sealed_where_it_points(tmp_path):
    # A dotfiles checkout links .kiro/settings elsewhere; with mcp.json absent the
    # name it would be written under must be listed too.
    elsewhere = tmp_path / "dotfiles-settings"
    elsewhere.mkdir()
    project = tmp_path / "project"
    (project / ".kiro").mkdir(parents=True)
    (project / ".kiro" / "settings").symlink_to(elsewhere, target_is_directory=True)
    assert str(elsewhere.resolve() / "mcp.json") in project_mcp_targets(str(project))


@_POSIX_ONLY
@pytest.mark.parametrize("shape", ["self-loop", "through-a-file"])
def test_a_name_the_seal_cannot_resolve_is_skipped_never_fails_the_launch(tmp_path, shape):
    # The Linux launcher refuses to start on a name it cannot pin, so a link that
    # loops or runs through a file is left out instead of stopping the chat.
    if shape == "self-loop":
        (tmp_path / ".mcp.json").symlink_to(".mcp.json")
    else:
        (tmp_path / "plain.txt").write_text("", encoding="utf-8")
        (tmp_path / ".mcp.json").symlink_to(tmp_path / "plain.txt" / "mcp.json")
    targets = project_mcp_targets(str(tmp_path))
    assert not any(t.endswith(CLAUDE_PROJECT_MCP_FILE) for t in targets)
    assert str(tmp_path / KIRO_PROJECT_MCP_FILE) in targets


@_POSIX_ONLY
@pytest.mark.parametrize("blocker", [".kiro", ".kiro/settings"])
def test_a_kiro_folder_that_is_a_file_is_skipped_so_the_chat_still_starts(tmp_path, blocker):
    # The Linux launcher cannot pin a name under a file; listing it would stop the
    # launch. No harness reads such a name, so only .mcp.json is sealed.
    if blocker == ".kiro/settings":
        (tmp_path / ".kiro").mkdir()
    (tmp_path / blocker).write_text("", encoding="utf-8")
    targets = project_mcp_targets(str(tmp_path))
    assert not any(t.endswith(os.path.join("settings", "mcp.json")) for t in targets)
    assert str(tmp_path / CLAUDE_PROJECT_MCP_FILE) in targets


@_POSIX_ONLY
def test_the_folder_seal_lists_a_linked_files_target(tmp_path):
    real = tmp_path / "dotfiles.json"
    real.write_text("{}", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").symlink_to(real)
    assert str(real) in project_mcp_targets(str(project))


# ── the sandbox seal ──


def _project_host(monkeypatch, folder, backend):
    from kiro_crew import sandbox
    from kiro_crew.sandbox_plan import SandboxRequest, plan_confinement

    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    monkeypatch.setattr(sandbox, "_resolved_kiro_mcp_registry_targets", lambda: [])
    request = SandboxRequest(tier="standard", backend=backend, project_folder=str(folder))
    host = sandbox._live_plan_host(request)
    return host, plan_confinement(request, host)


@_POSIX_ONLY
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
def test_the_spawn_folders_project_files_are_sealed_on_both_launchers(
    tmp_path, monkeypatch, backend
):
    (tmp_path / ".kiro" / "settings").mkdir(parents=True)
    (tmp_path / KIRO_PROJECT_MCP_FILE).write_text("{}", encoding="utf-8")
    host, plan = _project_host(monkeypatch, tmp_path, backend)
    assert str(tmp_path / KIRO_PROJECT_MCP_FILE) in plan.readonly
    assert str(tmp_path / CLAUDE_PROJECT_MCP_FILE) in plan.readonly
    if backend == "seatbelt":
        # A renamed folder would carry a path rule away, so each existing one is guarded.
        assert str(tmp_path / ".kiro" / "settings") in host.project_ancestor_guards
        assert str(tmp_path / ".kiro") in host.project_ancestor_guards
        assert str(tmp_path / ".kiro") in plan.runtime_ancestor_guards


@_POSIX_ONLY
def test_an_absent_kiro_folder_is_never_guarded_so_the_agent_can_create_it(tmp_path, monkeypatch):
    host, plan = _project_host(monkeypatch, tmp_path, "seatbelt")
    assert str(tmp_path / KIRO_PROJECT_MCP_FILE) in plan.readonly
    guards = (*host.readonly_ancestor_guards, *host.project_ancestor_guards)
    assert not any(g.startswith(str(tmp_path / ".kiro")) for g in guards)


@_POSIX_ONLY
def test_no_project_folder_seals_no_project_file(tmp_path, monkeypatch):
    host, _plan = _project_host(monkeypatch, "", "namespace")
    assert host.project_mcp_targets == ()


@_POSIX_ONLY
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
def test_an_operator_write_exception_holding_the_project_stays_writable(backend):
    # A read-only drive with a writable Documents folder, and a chat in a project
    # under it: the two file seals land on top and never cost the exception.
    from kiro_crew.sandbox_plan import CarveoutProbe, PlanHost, SandboxRequest, plan_confinement

    def probe(path):
        return CarveoutProbe(raw=path, lexical=path, canonical=path, is_dir=True)

    docs = "/mnt/c/Users/me/Documents"
    project = f"{docs}/proj"
    targets = (f"{project}/{KIRO_PROJECT_MCP_FILE}", f"{project}/{CLAUDE_PROJECT_MCP_FILE}")
    host = PlanHost(
        home="/srv/u",
        cwd="/work",
        uid=1000,
        gid=1000,
        project_mcp_targets=targets,
        # Every folder above the files, the shape the live host emits on Seatbelt.
        project_ancestor_guards=(f"{project}/.kiro/settings", f"{project}/.kiro", project, docs),
        operator_readonly_probes=(probe("/mnt/c"),),
        operator_exception_probes=(probe(docs),),
    )
    plan = plan_confinement(SandboxRequest(tier="standard", backend=backend), host)
    assert plan.operator_writable == (docs,)
    assert set(targets) <= set(plan.readonly)
    assert project in plan.runtime_ancestor_guards
    assert plan.refusals == ()


def test_the_module_paths_join_under_the_folder():
    assert Path(KIRO_PROJECT_MCP_FILE).parts == (".kiro", "settings", "mcp.json")
