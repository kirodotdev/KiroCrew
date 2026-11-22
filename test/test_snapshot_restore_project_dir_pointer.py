"""A restore never installs the archive's `project_dir` pointer.

`project_dir` names the source checkout the gateway runs a boot script from. An archive
can come from someone else and can also carry a `workspace/` tree, so a pointer taken from
it names a folder the archive supplied: code runs at the next start. The pointer is this
host's own runtime state, so the live file is kept in both modes. An absent one stays absent.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
import snapshot_helpers
from snapshot_helpers import unpinnable_argv

from kiro_crew.snapshot import restore_main

_no_gateway = snapshot_helpers._no_gateway

_ROOT = "kirocrew-snapshot-20260101T000000Z"
_LIVE = "/live/checkout\n"


def _bundle(tmp_path: Path) -> Path:
    members = {
        "MANIFEST.json": json.dumps(
            {"version": 4, "purpose": "backup", "components": {"config": "unresolved"}}
        ),
        "config.json": '{"agent": {"model": "from-archive"}}',
        "project_dir": f"{tmp_path / 'evil'}\n",
    }
    path = tmp_path / "bundle.tar.gz"
    with tarfile.open(str(path), "w:gz") as tar:
        d = tarfile.TarInfo(_ROOT)
        d.type = tarfile.DIRTYPE
        d.mode = 0o700
        tar.addfile(d)
        for name, text in members.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{_ROOT}/{name}")
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return path


def _restore(tmp_path: Path, home: Path, monkeypatch, mode: str) -> int:
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    argv = [str(_bundle(tmp_path)), "--force", "--mode", mode, "--components", "config"]
    return restore_main(argv + unpinnable_argv())


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_a_live_project_dir_is_kept(tmp_path, monkeypatch, mode):
    home = tmp_path / "home"
    home.mkdir()
    (home / "project_dir").write_text(_LIVE)
    (home / "config.json").write_text("{}")

    assert _restore(tmp_path, home, monkeypatch, mode) == 0

    assert (home / "project_dir").read_text() == _LIVE


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_an_absent_project_dir_stays_absent(tmp_path, monkeypatch, mode):
    home = tmp_path / "home"

    assert _restore(tmp_path, home, monkeypatch, mode) == 0

    assert not (home / "project_dir").exists()


def _project_dir_lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().out.splitlines() if "project_dir" in ln]


def test_replace_installs_the_rest_of_config_and_says_the_pointer_was_not_restored(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"

    assert _restore(tmp_path, home, monkeypatch, "replace") == 0

    assert json.loads((home / "config.json").read_text())["agent"]["model"] == "from-archive"
    lines = _project_dir_lines(capsys)
    # The host had no copy, so the summary must not claim one was kept.
    assert len(lines) == 1 and "not restored" in lines[0] and "kept" not in lines[0]


def test_replace_says_the_live_pointer_was_kept(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "project_dir").write_text(_LIVE)

    assert _restore(tmp_path, home, monkeypatch, "replace") == 0

    lines = _project_dir_lines(capsys)
    assert len(lines) == 1 and "not restored" in lines[0] and "copy is kept" in lines[0]
