"""The portable Project registry lives on BOTH crew-home fences.

The trust-bearing Project state -- the registry of pinned Git remotes/branches
and the set of registrations a session resolves through, plus its lock -- sits
in one top-level crew-home leaf (``projects-registry/``) that is on the agent
tool gate (``security.paths._SENSITIVE_HOME_DIRS`` -> ``is_sensitive_path``) AND
the OS sandbox mask (``sandbox._CREW_HIDDEN_LEAVES``), the same treatment
``memory_stores`` gets. Materialized checkouts stay under the VISIBLE
``projects/`` leaf -- the agent works in them and they are never a source of
authority -- so this test also pins that asymmetry.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import sandbox, security
from kiro_crew.project_registry import PROJECT_REGISTRY_DIR_NAME, ProjectRegistry

_HOME = os.path.expanduser("~")
_CREW = os.path.join(_HOME, ".kiro", "crew")


def _rebase(path: str | Path, probe: Path) -> str:
    return os.path.join(_CREW, os.path.relpath(str(path), str(probe)))


class TestToolGateFence:
    def test_registry_path_is_sensitive(self, tmp_path: Path) -> None:
        registry = ProjectRegistry(
            projects_dir=tmp_path / "projects",
            registry_dir=tmp_path / PROJECT_REGISTRY_DIR_NAME,
        )
        assert security.is_sensitive_path(_rebase(registry.registry_path, tmp_path))
        assert security.is_sensitive_path(_rebase(registry.lock_path, tmp_path))

    def test_leaf_is_declared_on_the_gate(self) -> None:
        # A crew-home leaf: declared in _CREW_SECRET_LEAVES, which is expanded
        # crew-prefixed into _SENSITIVE_HOME_DIRS (the same mechanism memory_stores
        # uses), so the prefixed forms are the ones that appear in the gate list.
        assert PROJECT_REGISTRY_DIR_NAME in set(security.paths._CREW_SECRET_LEAVES)
        for prefix in (".kiro/crew", ".kirocrew"):
            assert f"{prefix}/{PROJECT_REGISTRY_DIR_NAME}" in set(
                security.paths._SENSITIVE_HOME_DIRS
            )

    def test_materialized_checkouts_stay_visible(self, tmp_path: Path) -> None:
        # A source checkout under the visible ``projects/`` leaf is where the
        # agent works; fencing it would break the product. It must NOT be
        # sensitive, unlike the registry above.
        checkout = os.path.join(_CREW, "projects", "state", "some-id", "sources", "api")
        assert not security.is_sensitive_path(checkout)


class TestSandboxDisposition:
    def test_registry_leaf_is_hidden(self) -> None:
        assert PROJECT_REGISTRY_DIR_NAME in set(sandbox._CREW_HIDDEN_LEAVES)
        for prefix in (".kiro/crew", ".kirocrew"):
            assert f"{prefix}/{PROJECT_REGISTRY_DIR_NAME}" in sandbox._CREW_HIDDEN_DIRS

    def test_hidden_project_leaves_are_precreated(self) -> None:
        hidden = {leaf for leaf in sandbox._CREW_HIDDEN_LEAVES if leaf.startswith("projects")}
        assert PROJECT_REGISTRY_DIR_NAME in hidden
        assert hidden <= set(sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES)

    def test_precreate_parity_detects_missing_registry(self, monkeypatch) -> None:
        monkeypatch.setattr(
            sandbox,
            "_CREW_PRECREATE_HIDDEN_DIR_LEAVES",
            tuple(
                leaf
                for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
                if leaf != PROJECT_REGISTRY_DIR_NAME
            ),
        )
        with pytest.raises(AssertionError):
            self.test_hidden_project_leaves_are_precreated()

    def test_projects_leaf_is_not_hidden(self) -> None:
        # The materialized-state leaf is deliberately visible.
        assert "projects" not in set(sandbox._CREW_HIDDEN_LEAVES)
