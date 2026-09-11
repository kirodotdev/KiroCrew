"""``delegated_workspace_exposes_agents_dir``: the agents-tree seal is a rule
of Kiro Crew's own launcher, so a kiro-cli spawn delegated to kiro-cli's
internal sandbox (macOS with that sandbox on, first-party Windows) must be
refused when its workspace is, contains, or sits inside the agents directory.
Where the launcher wraps the child the seal holds and the workspace is free."""

from __future__ import annotations

import pytest

from kiro_crew import sandbox as sandbox_mod


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    agents = tmp_path / "home" / ".kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(sandbox_mod, "_resolved_kiro_agents_targets", lambda: [str(agents)])
    return agents


def _overlapping(agents_dir):
    return (
        agents_dir,  # the agents dir itself
        agents_dir / "nested",  # inside it
        agents_dir.parent,  # ~/.kiro
        agents_dir.parent.parent,  # the home that contains it
    )


class TestDelegatedWorkspaceExposesAgentsDir:
    def test_darwin_with_internal_sandbox_refuses_every_overlap(self, monkeypatch, agents_dir):
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: True)
        for workspace in _overlapping(agents_dir):
            reason = sandbox_mod.delegated_workspace_exposes_agents_dir(workspace)
            assert reason is not None, workspace
            assert "agents directory" in reason
            assert str(workspace) in reason

    def test_darwin_with_internal_sandbox_allows_sibling(self, monkeypatch, agents_dir):
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: True)
        sibling = agents_dir.parent / "crew" / "workspace"
        assert sandbox_mod.delegated_workspace_exposes_agents_dir(sibling) is None
        # A name that merely shares a prefix is not inside the directory.
        assert (
            sandbox_mod.delegated_workspace_exposes_agents_dir(agents_dir.parent / "agents-archive")
            is None
        )

    def test_darwin_without_internal_sandbox_keeps_the_seal_and_allows(
        self, monkeypatch, agents_dir
    ):
        # Kiro Crew's seatbelt wraps the child, so the seal applies whatever
        # the workspace is; refusing here would break $HOME-rooted workspaces
        # for no gain.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: False)
        for workspace in _overlapping(agents_dir):
            assert sandbox_mod.delegated_workspace_exposes_agents_dir(workspace) is None

    def test_linux_never_refuses(self, monkeypatch, agents_dir):
        monkeypatch.setattr(sandbox_mod.sys, "platform", "linux")
        # Even with the kiro setting on: Linux never delegates.
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: True)
        for workspace in _overlapping(agents_dir):
            assert sandbox_mod.delegated_workspace_exposes_agents_dir(workspace) is None

    def test_windows_refuses_regardless_of_kiro_setting(self, monkeypatch, agents_dir):
        # Windows has no Kiro Crew backend: every first-party spawn delegates.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "win32")
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: False)
        assert sandbox_mod.delegated_workspace_exposes_agents_dir(agents_dir) is not None
        assert (
            sandbox_mod.delegated_workspace_exposes_agents_dir(agents_dir.parent / "crew") is None
        )

    def test_none_workspace_is_not_judged(self, monkeypatch, agents_dir):
        monkeypatch.setattr(sandbox_mod.sys, "platform", "win32")
        assert sandbox_mod.delegated_workspace_exposes_agents_dir(None) is None

    def test_symlinked_workspace_resolving_into_agents_dir_is_refused(
        self, monkeypatch, agents_dir, tmp_path
    ):
        # The lexical spelling is an innocent sibling; only the canonical
        # spelling (and the inode) reveal it as the agents tree.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: True)
        inner = agents_dir / "inner"
        inner.mkdir()
        for target in (agents_dir, inner, agents_dir.parent):
            link = tmp_path / f"ws-{target.name}"
            link.symlink_to(target, target_is_directory=True)
            reason = sandbox_mod.delegated_workspace_exposes_agents_dir(link)
            assert reason is not None, link
            assert str(link) in reason

    def test_symlinked_agents_dir_spelling_is_resolved_too(self, monkeypatch, agents_dir, tmp_path):
        # The configured agents path may itself be a link; the workspace sits
        # inside the REAL tree and must still be caught.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox_mod, "kiro_internal_sandbox_enabled", lambda: True)
        alias = tmp_path / "agents-alias"
        alias.symlink_to(agents_dir, target_is_directory=True)
        monkeypatch.setattr(sandbox_mod, "_resolved_kiro_agents_targets", lambda: [str(alias)])
        inside = agents_dir / "spec-dir"
        inside.mkdir()
        assert sandbox_mod.delegated_workspace_exposes_agents_dir(inside) is not None
        assert sandbox_mod.delegated_workspace_exposes_agents_dir(tmp_path / "elsewhere") is None

    def test_not_yet_created_sibling_workspace_is_allowed(self, monkeypatch, agents_dir):
        # A workspace the spawn is about to mkdir has no inode; its lexical and
        # canonical spellings alone decide, and a sibling passes.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "win32")
        fresh = agents_dir.parent / "crew" / "not-yet"
        assert not fresh.exists()
        assert sandbox_mod.delegated_workspace_exposes_agents_dir(fresh) is None
