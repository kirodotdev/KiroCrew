"""A restore refuses a config.json whose roster keys or workspace folders could escape.

The restore installs the archive's ``config.json`` whole. A roster key (or a row's
``member_id``) is later joined into a file path, so one that is not exactly one path
component, or a workspace folder outside this install's workspace root, makes the whole
restore refuse before anything is changed.
"""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap

LIVE = '{"agents": {"live": {}}}'


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.json").write_text(LIVE, encoding="utf-8")
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws-root"))
    return h


def _bundle(tmp_path: Path, config: dict, name: str = "b", file_name: str = "config.json") -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / file_name).write_text(json.dumps(config), encoding="utf-8")
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"config": "unresolved"}}', encoding="utf-8"
    )
    bundle = tmp_path / f"{name}.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return bundle


def _restore(bundle: Path, extra=(), *, mode: str = "replace", include_config: bool = False):
    # A Replace keeps the live config.json unless the settings box is ticked
    # (--include-config); the tests that read the INSTALLED config tick it.
    flag = ["--include-config"] if include_config else []
    return snap.restore_main(
        [str(bundle), "--mode", mode, "--force", "--components", "config", *flag, *extra]
    )


@pytest.mark.parametrize("key", ["../x", "a/b", "..", ".", "a\\b", "a\x00b", "a\nb"])
def test_a_roster_key_that_is_not_one_path_component_refuses_the_whole_restore(
    home, tmp_path, capsys, key
):
    bundle = _bundle(tmp_path, {"agents": {key: {"member_id": "ok"}}})
    rc = _restore(bundle)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "agent name" in out, out
    assert (home / "config.json").read_text(encoding="utf-8") == LIVE


@pytest.mark.parametrize("member_id", ["../x", "a/b", "..", "a\nb"])
def test_a_member_id_that_is_not_one_path_component_refuses_the_whole_restore(
    home, tmp_path, capsys, member_id
):
    bundle = _bundle(tmp_path, {"agents": {"ok": {"member_id": member_id}}})
    rc = _restore(bundle)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "member_id" in out, out
    assert (home / "config.json").read_text(encoding="utf-8") == LIVE


def test_the_refusal_names_the_key_and_nothing_else_of_the_archive(home, tmp_path, capsys):
    bundle = _bundle(
        tmp_path, {"agents": {"../x": {"member_id": "ok", "instructions": "SECRET-TEXT"}}}
    )
    assert _restore(bundle) == 1
    out = capsys.readouterr().out
    assert "SECRET-TEXT" not in out
    assert "\x00" not in out


def test_a_key_with_dots_spaces_or_cjk_still_restores(home, tmp_path, capsys):
    cfg = {"agents": {"my.pkg": {"member_id": "m.1"}, "my crew": {}, "小明": {}}}
    bundle = _bundle(tmp_path, cfg)
    assert _restore(bundle, unpinnable_argv(), include_config=True) == 0, capsys.readouterr().out
    assert set(json.loads((home / "config.json").read_text())["agents"]) == set(cfg["agents"])


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
def test_a_workspace_folder_outside_the_roots_refuses_the_whole_restore(
    home, tmp_path, capsys, workspace_dir
):
    bundle = _bundle(tmp_path, {"workspaces": {"evil": {"dir": workspace_dir}}})
    rc = _restore(bundle)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "evil" in out and "workspace" in out, out
    assert workspace_dir not in out
    assert (home / "config.json").read_text(encoding="utf-8") == LIVE


def test_a_workspace_folder_under_the_workspace_root_or_relative_still_restores(
    home, tmp_path, capsys
):
    cfg = {
        "workspaces": {
            "rel": {"dir": "workspace"},
            "plain": "workspace-two",
            "inside": {"dir": str(tmp_path / "ws-root" / "proj")},
        }
    }
    bundle = _bundle(tmp_path, cfg)
    assert _restore(bundle, unpinnable_argv()) == 0, capsys.readouterr().out


@pytest.mark.parametrize("workspace_dir", ["/home/alice/code/proj", "/var/elsewhere/x", "/etc"])
def test_an_absolute_workspace_folder_does_not_refuse_the_restore(
    home, tmp_path, capsys, workspace_dir
):
    """An absolute folder is a supported placement; where it is used is judged there."""
    cfg = {"agents": {"keep": {}}, "workspaces": {"proj": {"dir": workspace_dir}}}
    bundle = _bundle(tmp_path, cfg)
    assert _restore(bundle, unpinnable_argv(), include_config=True) == 0, capsys.readouterr().out
    assert json.loads((home / "config.json").read_text())["workspaces"]["proj"]["dir"] == (
        workspace_dir
    )


def test_a_default_replace_keeps_the_live_config_and_still_vets_the_archive(
    home, tmp_path, capsys
):
    """Unticked, the archive's config.json is not installed, but an unsafe one still refuses."""
    safe = _bundle(tmp_path, {"agents": {"my.pkg": {}}}, name="safe")
    assert _restore(safe, unpinnable_argv()) == 0, capsys.readouterr().out
    assert (home / "config.json").read_text(encoding="utf-8") == LIVE
    unsafe = _bundle(tmp_path, {"agents": {"../x": {}}}, name="unsafe")
    assert _restore(unsafe) == 1, capsys.readouterr().out
    assert (home / "config.json").read_text(encoding="utf-8") == LIVE


def test_merge_does_not_vet_or_install_an_unsafe_overlay_when_live_overlay_is_absent(
    home, tmp_path, capsys
):
    bundle = _bundle(
        tmp_path,
        {"agents": {"../x": {}}},
        name="unsafe-overlay-merge",
        file_name="config.local.json",
    )

    rc = _restore(bundle, unpinnable_argv(), mode="merge")
    out = capsys.readouterr().out

    assert rc == 0, out
    assert not (home / "config.local.json").exists()


def test_replace_still_refuses_an_unsafe_overlay(home, tmp_path, capsys):
    bundle = _bundle(
        tmp_path,
        {"agents": {"../x": {}}},
        name="unsafe-overlay-replace",
        file_name="config.local.json",
    )

    rc = _restore(bundle)
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "agent name" in out, out
    assert not (home / "config.local.json").exists()


def test_merge_still_refuses_an_unsafe_config_when_live_config_is_absent(home, tmp_path, capsys):
    (home / "config.json").unlink()
    bundle = _bundle(tmp_path, {"agents": {"../x": {}}}, name="unsafe-config-merge")

    rc = _restore(bundle, mode="merge")
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "agent name" in out, out
    assert not (home / "config.json").exists()


def test_the_overlay_config_local_json_is_held_to_the_same_workspace_rule(home, tmp_path, capsys):
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "config.local.json").write_text(
        json.dumps({"workspaces": {"evil": {"dir": "../../outside"}}}), encoding="utf-8"
    )
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"config": "unresolved"}}', encoding="utf-8"
    )
    bundle = tmp_path / "overlay.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    rc = _restore(bundle)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "evil" in out and "workspace" in out, out
    assert not (home / "config.local.json").exists()
