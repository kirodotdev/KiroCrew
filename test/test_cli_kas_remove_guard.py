"""``kirocrew kas remove`` refuses a blank ``--version`` and fails cleanly.

An empty ``--version`` (an unset ``"$VAR"``) read as "no version" would widen
``kas remove --version "" --yes`` to EVERY installed engine. Removing every
version is the flag left out; a blank one is refused with exit 2 before anything
is listed or deleted. Also pinned here: a closed stdin answers the confirmation
"no" instead of raising ``EOFError``, and a delete that fails is one error line
and exit 1, not a traceback.

Every test runs on a ``tmp_path`` engines root; the operator's own engines are
never resolved (``kas_engines_root`` is patched on the facade the CLI reads).
"""

from __future__ import annotations

import argparse
import errno
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


def test_a_failed_delete_is_one_line_and_exit_1(root, monkeypatch, capsys):
    def _refuse(path, *a, **k):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(shutil, "rmtree", _refuse)

    assert cli_kas.kas_cmd(_parse("--version", "0.60.0", "--yes")) == 1

    err = capsys.readouterr().err
    assert f"could not remove {root / '0.60.0'}" in err
    assert "Traceback" not in err
    assert (root / "0.60.0").exists()
