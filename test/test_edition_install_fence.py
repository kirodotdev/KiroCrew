"""An enterprise build's packaged install tree is on the agent's file-tool write gate.

RFC enterprise edition, Design section 4: the agent cannot change the install
directory. The OS sandbox seals the tree for shells (``test_sandbox_install_root_seal``);
these pin the file-tool half: an edit into the tree is refused in an enterprise build,
however the path is spelled, while reads stay open and an ``oss`` build is unchanged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import edition_install_fence, sandbox, security
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.llm_helpers import _edit_target_denial

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")


@pytest.fixture
def install(tmp_path: Path, monkeypatch) -> Path:
    """A packaged install tree the gateway imports from, as the fence sees it."""
    root = tmp_path / "venv" / "lib" / "site-packages" / "kiro_crew"
    (root / "static").mkdir(parents=True)
    (root / "server.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        sandbox, "_resolved_install_root_targets", lambda: [str(root), os.path.realpath(root)]
    )
    return root


@pytest.fixture
def enterprise(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def test_enterprise_refuses_a_write_into_the_install_tree(install: Path, enterprise) -> None:
    for target in (install, install / "server.py", install / "static" / "new.js"):
        reason = security.sensitive_write_path_refusal(str(target))
        assert reason == f"Blocked: write to protected path: {target}"
        assert security.is_sensitive_write_path(str(target)) is True


def test_enterprise_refuses_dotdot_into_the_install_tree(install: Path, enterprise) -> None:
    spelled = str(install / "static" / ".." / "server.py")
    assert security.is_sensitive_write_path(spelled) is True


@_POSIX_ONLY
def test_enterprise_refuses_a_symlink_into_the_install_tree(
    install: Path, enterprise, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "link").symlink_to(install)
    assert security.is_sensitive_write_path(str(work / "link" / "server.py")) is True
    assert security.is_sensitive_write_path("link/server.py", base_dir=str(work)) is True


def test_enterprise_leaves_reads_and_siblings_open(
    install: Path, enterprise, tmp_path: Path
) -> None:
    assert security.is_sensitive_path(str(install / "server.py")) is False
    sibling = install.parent / "other_pkg" / "mod.py"
    assert security.is_sensitive_write_path(str(sibling)) is False
    prefix_twin = install.parent / "kiro_crew_extra" / "mod.py"
    assert security.is_sensitive_write_path(str(prefix_twin)) is False


def test_oss_is_unchanged(install: Path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "oss")
    assert security.sensitive_write_path_refusal(str(install / "server.py")) is None


def test_source_checkout_has_nothing_to_fence(enterprise, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [])
    assert edition_install_fence.install_root_write(str(tmp_path / "x.py")) is False


def test_a_stall_still_refuses(install: Path, enterprise, monkeypatch) -> None:
    def stalled(*args, **kwargs):
        raise security.PathResolutionStalled(str(install), str(install))

    monkeypatch.setattr(security.paths, "_candidate_forms", stalled)
    monkeypatch.setattr(security.paths, "_path_in_home_dirs", lambda *a, **k: False)
    monkeypatch.setattr(security.paths, "_is_keystone_publish_artifact", lambda *a, **k: False)
    monkeypatch.setattr(security.paths, "_native_store_write", lambda *a, **k: False)
    reason = security.sensitive_write_path_refusal(str(install / "server.py"))
    assert reason is not None and security.is_unverifiable_path_refusal(reason)


def test_hook_gate_denies_an_edit_into_the_install_tree(install: Path, monkeypatch) -> None:
    # Only the fence reads enterprise here: a whole enterprise build needs a
    # companion profile to boot (RFC D1), which the gate test does not compose.
    monkeypatch.setattr(edition_install_fence, "is_enterprise", lambda: True)
    decision = HookManager(HooksConfig.from_dict({})).on_tool_call(
        "Editing a file",
        session_key="cli_chat",
        tool_kind="edit",
        raw_params={"path": str(install / "server.py"), "content": "x"},
    )
    assert decision.action == TOOL_DENY
    assert "write-protected" in decision.reason


def test_permission_card_edit_into_the_install_tree_is_refused(install: Path, enterprise) -> None:
    hit = _edit_target_denial({"path": str(install / "server.py")}, "", "edit")
    assert hit is not None
    assert hit[1].startswith("Blocked: write to protected path: ")
