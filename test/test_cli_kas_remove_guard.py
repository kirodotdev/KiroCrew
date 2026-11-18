"""``kirocrew kas remove`` refuses a blank ``--version`` and fails cleanly.

An empty ``--version`` (an unset ``"$VAR"``) read as "no version" would widen
``kas remove --version "" --yes`` to EVERY installed engine. Removing every
version is the flag left out; a blank one is refused with exit 2 before anything
is listed or deleted. Also pinned here: a closed stdin answers the confirmation
"no" instead of raising ``EOFError``, and a delete that fails is one error line
and exit 1, not a traceback.

A version removed while a KAS session runs it is whole-or-nothing. Windows refuses
to rename a folder holding a file another process has open without delete sharing,
so a held engine fails the rename before anything is deleted; the tests stand in
for that holder by patching ``os.rename`` and ``shutil.rmtree`` (a Linux host has
no such refusal), and a maintainer checks the real holder on Windows.

Every test runs on a ``tmp_path`` engines root; the operator's own engines are
never resolved (``kas_engines_root`` is patched on the facade the CLI reads).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
from pathlib import Path

import pytest

from kiro_crew import cli_kas
from kiro_crew.agent_sdk import kas_bundle

_VERSIONS = ("0.60.0", "0.61.0")


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli_kas.add_kas_parser(parser)
    return parser.parse_args(["remove", *argv])


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "engines" / "kas"
    for v in _VERSIONS:
        (root / v).mkdir(parents=True)
        (root / v / "install.json").write_text("{}")
    monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: root)
    return root


@pytest.mark.parametrize("blank", ["", " ", "\t\n"])
def test_a_blank_version_is_refused_and_deletes_nothing(root, monkeypatch, capsys, blank):
    listed: list[Path] = []
    real_listing = kas_bundle.installed_versions

    def _listing(*, root):
        listed.append(root)
        return real_listing(root=root)

    monkeypatch.setattr(kas_bundle, "installed_versions", _listing)

    assert cli_kas.kas_cmd(_parse("--version", blank, "--yes")) == 2

    err = capsys.readouterr().err
    assert "--version is blank" in err
    assert listed == []
    assert sorted(p.name for p in root.iterdir()) == list(_VERSIONS)


def test_leaving_version_out_still_removes_every_version(root, capsys):
    assert cli_kas.kas_cmd(_parse("--yes")) == 0
    assert not root.exists()
    assert f"removed {root}" in capsys.readouterr().out


def test_a_version_that_is_not_installed_exits_non_zero(root, capsys):
    assert cli_kas.kas_cmd(_parse("--version", "9.9.9", "--yes")) == 1
    assert "KAS 9.9.9 is not installed" in capsys.readouterr().err
    assert sorted(p.name for p in root.iterdir()) == list(_VERSIONS)


def test_a_closed_stdin_answers_no(root, monkeypatch, capsys):
    def _closed(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _closed)

    assert cli_kas.kas_cmd(_parse("--version", "0.60.0")) == 1

    assert "pass --yes" in capsys.readouterr().out
    assert (root / "0.60.0").exists()


def test_a_failed_delete_of_every_version_is_one_line_and_exit_1(root, monkeypatch, capsys):
    def _refuse(path, *a, **k):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(shutil, "rmtree", _refuse)

    assert cli_kas.kas_cmd(_parse("--yes")) == 1

    err = capsys.readouterr().err
    assert f"could not remove {root}" in err
    assert "Traceback" not in err
    assert (root / "0.60.0").exists()


_ENGINE_FILES = ("acp/server.js", "install.json", "lib/a.js", "lib/b.js", "node/node.exe")


def _engine(home: Path) -> Path:
    """An installed engine under *home*; returns the Node binary a session would hold."""
    for rel in _ENGINE_FILES:
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("x")
    (home / "node" / "node.exe").chmod(0o755)
    (home / "install.json").write_text(
        json.dumps({"acp_server": "acp/server.js", "node_bin": "node/node.exe"})
    )
    return home / "node" / "node.exe"


def _files(home: Path) -> list[str]:
    return sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())


def _in_use(path: Path) -> PermissionError:
    return PermissionError(
        errno.EACCES,
        "[WinError 32] The process cannot access the file because it is being used"
        " by another process",
        str(path),
    )


def _hold(monkeypatch, held: Path) -> None:
    """Behave as Windows does while a process holds *held* open without delete sharing."""
    real_rename, real_rmtree = os.rename, shutil.rmtree

    def _rename(src, dst, *a, **k):
        if Path(src) in held.parents:
            raise _in_use(held)
        return real_rename(src, dst, *a, **k)

    def _rmtree(path, *a, **k):
        if Path(path) in held.parents:
            for f in sorted(p for p in Path(path).rglob("*") if p.is_file()):
                if f == held:
                    raise _in_use(f)
                f.unlink()
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(os, "rename", _rename)
    monkeypatch.setattr(shutil, "rmtree", _rmtree)


def test_a_held_engine_file_leaves_the_version_whole_and_listed(root, monkeypatch, capsys):
    home = root / "0.60.0"
    _hold(monkeypatch, _engine(home))
    before = _files(home)

    assert cli_kas.kas_cmd(_parse("--version", "0.60.0", "--yes")) == 1

    err = capsys.readouterr().err
    assert "0.60.0" in err
    assert "KAS session" in err
    assert "Traceback" not in err
    assert _files(home) == before
    assert kas_bundle.installed_versions(root=root) == list(_VERSIONS)
    assert kas_bundle.installed_bundle(root=root, version="0.60.0") is not None


def test_a_version_nothing_holds_is_removed_whole(root, capsys):
    _engine(root / "0.60.0")

    assert cli_kas.kas_cmd(_parse("--version", "0.60.0", "--yes")) == 0

    assert f"removed {root / '0.60.0'}" in capsys.readouterr().out
    assert sorted(p.name for p in root.iterdir()) == ["0.61.0"]


def test_a_delete_that_fails_after_the_rename_names_what_is_left(root, monkeypatch, capsys):
    _engine(root / "0.60.0")

    def _refuse(path, *a, **k):
        raise _in_use(Path(path))

    monkeypatch.setattr(shutil, "rmtree", _refuse)

    assert cli_kas.kas_cmd(_parse("--version", "0.60.0", "--yes")) == 1

    err = capsys.readouterr().err
    (leftover,) = [p for p in root.iterdir() if p.name.startswith(".")]
    assert str(leftover) in err
    assert "Traceback" not in err
    assert kas_bundle.installed_versions(root=root) == ["0.61.0"]
    assert _files(leftover) == sorted(_ENGINE_FILES)
