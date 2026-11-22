"""An import skips a config.json whose roster keys or workspace folders could escape.

The archive's ``config.json`` is vetted before either import mode applies it. A roster key
or ``member_id`` that is not exactly one path component, or a workspace folder outside this
install's workspace root, skips the whole document; the rest of the import continues.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew import portability


@pytest.fixture(autouse=True)
def _roots(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws-root"))


def _stage(tmp_path: Path, config: dict) -> Path:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return snap


def _vet(snap: Path):
    summary: dict = {"items": []}
    vetted = portability._vet_archive_settings(snap, summary)
    return vetted, summary


@pytest.mark.parametrize("key", ["../x", "a/b", "..", ".", "a\\b", "a\nb"])
def test_a_roster_key_that_is_not_one_path_component_skips_the_config(tmp_path, key):
    snap = _stage(tmp_path, {"agents": {key: {"member_id": "ok"}}})
    vetted, summary = _vet(snap)
    assert "config.json" not in vetted
    assert not (snap / "config.json").exists()
    assert summary.get("refused_merges") == ["config"], summary
    assert any(i.startswith("config (skipped: ") and "agent name" in i for i in summary["items"])


@pytest.mark.parametrize("member_id", ["../x", "a/b", "..", "a\nb"])
def test_a_member_id_that_is_not_one_path_component_skips_the_config(tmp_path, member_id):
    snap = _stage(tmp_path, {"agents": {"ok": {"member_id": member_id}}})
    vetted, summary = _vet(snap)
    assert "config.json" not in vetted
    assert any("member_id" in i for i in summary["items"]), summary


def test_a_key_with_dots_spaces_or_cjk_is_kept(tmp_path):
    cfg = {"agents": {"my.pkg": {"member_id": "m.1"}, "my crew": {}, "小明": {}}}
    snap = _stage(tmp_path, cfg)
    vetted, summary = _vet(snap)
    assert vetted["config.json"] == cfg
    assert (snap / "config.json").exists()
    assert not summary.get("refused_merges")


@pytest.mark.parametrize(
    "workspace_dir",
    [
        "../../outside",
        "..\\outside",
        "../a\x00b",
        "~\x00/../x",
        r"C:..\x",
        "D:x",
        pytest.param(
            "~nosuchuser_zz/x",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows expands ~name, so the runtime does not fail on it",
            ),
        ),
        pytest.param(
            "./~nosuchuser_zz/x",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows expands ~name, so the runtime does not fail on it",
            ),
        ),
        pytest.param(
            "\\\\/../../x",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows reads a leading double backslash as a UNC root, not judged",
            ),
        ),
        ".. /x",
        pytest.param(
            "C:/x/../../../etc",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows reads C:/ as an absolute path, which is not judged",
            ),
        ),
        pytest.param(
            r"\x/../../../../../../etc",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows reads a leading backslash as an absolute path, which is not judged",
            ),
        ),
        pytest.param(
            r"workspace/a\b/../../..",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows reads both spellings as the same path, which stays inside",
            ),
        ),
        pytest.param(
            r"a\b/../../x",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows reads both spellings as the same path, which stays inside",
            ),
        ),
    ],
)
def test_a_workspace_folder_outside_the_roots_skips_the_config(tmp_path, workspace_dir):
    snap = _stage(tmp_path, {"workspaces": {"evil": {"dir": workspace_dir}}})
    vetted, summary = _vet(snap)
    assert "config.json" not in vetted
    text = " ".join(summary["items"])
    assert "evil" in text and "workspace" in text, summary
    assert workspace_dir not in text


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows collapses .. before following a link, so there is no escape to refuse",
)
def test_a_workspace_folder_that_escapes_through_a_symlink_skips_the_config(tmp_path):
    workspace = tmp_path / "home" / "workspace"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)

    snap = _stage(tmp_path, {"workspaces": {"evil": {"dir": "workspace/link/../target"}}})
    vetted, summary = _vet(snap)
    assert "config.json" not in vetted
    text = " ".join(summary["items"])
    assert "evil" in text and "workspace" in text, summary


def test_a_workspace_folder_under_the_workspace_root_or_relative_is_kept(tmp_path):
    cfg = {
        "workspaces": {
            "rel": {"dir": "workspace"},
            "plain": "workspace-two",
            "dotdot": {"dir": "workspace/a/../b"},
            "dotdot-root": {"dir": "workspace/.."},
            "dotdot-trailing": {"dir": "workspace/a/../b/"},
            "backslash": {"dir": r"workspace/a\b"},
            "inside": {"dir": str(tmp_path / "ws-root" / "proj")},
        }
    }
    snap = _stage(tmp_path, cfg)
    vetted, _ = _vet(snap)
    assert vetted["config.json"] == cfg


@pytest.mark.parametrize(
    "workspace_dir",
    [
        "/home/alice/code/proj",
        "/var/elsewhere/x",
        "/etc",
        "/x/../../etc",
        r"C:\x\..\..\y",
        r"\\host\share\..\y",
    ],
)
def test_an_absolute_workspace_folder_keeps_the_whole_config(tmp_path, workspace_dir):
    """An absolute folder is a supported placement; it must not drop agents and settings."""
    cfg = {"agents": {"keep": {}}, "workspaces": {"proj": {"dir": workspace_dir}}}
    snap = _stage(tmp_path, cfg)
    vetted, summary = _vet(snap)
    assert vetted["config.json"] == cfg
    assert not summary.get("refused_merges")


@pytest.mark.parametrize("workspace_dir", ["../../outside", "..\\outside"])
def test_the_overlay_config_local_json_is_held_to_the_same_workspace_rule(tmp_path, workspace_dir):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.local.json").write_text(
        json.dumps({"workspaces": {"evil": {"dir": workspace_dir}}}), encoding="utf-8"
    )
    vetted, summary = _vet(snap)
    assert "config.local.json" not in vetted
    assert not (snap / "config.local.json").exists()
    assert summary.get("refused_merges") == ["config.local"], summary


@pytest.mark.parametrize(
    "workspace_dir",
    ["/x/../../etc", r"\x\..\..\y", r"C:\x\..\..\y", r"\\host\share\..\y"],
)
def test_a_rooted_or_drive_folder_is_kept_on_every_platform(_floor_monkeypatch, workspace_dir):
    """Kept where Windows or the OS reads it as absolute; on POSIX the backslash forms are one
    name inside the data home."""
    import os

    from kiro_crew.archive_config_vetting import _dir_outside_roots

    _floor_monkeypatch.setattr(os.path, "isabs", lambda p: False)
    assert _dir_outside_roots(workspace_dir, ["/w", "/data/Crew"]) is False


def test_workspace_containment_compares_case_the_way_the_platform_does(_floor_monkeypatch):
    """On a case-insensitive platform a differently cased spelling of a root is inside it."""
    import os

    from kiro_crew.archive_config_vetting import _dir_outside_roots

    roots = ["/w", "/data/Crew"]
    case_sensitive = os.path.normcase("A") == "A"  # False on Windows, where it lowercases
    assert _dir_outside_roots("../CREW/x", roots) is case_sensitive
    _floor_monkeypatch.setattr(os.path, "normcase", lambda p: p.lower())
    assert _dir_outside_roots("../CREW/x", roots) is False
