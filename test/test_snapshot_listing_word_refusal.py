"""`kirocrew snapshot list` must not write a backup into `./list/`.

The output directory is an optional positional, so a bare `list` (the verb a user
reaches for instead of `--list`) would be taken as a directory: the command would
exit 0, write `./list/kirocrew-snapshot-<ts>.tar.gz` into the current working
directory -- inside a repository, where it can be committed -- and prune it with the
default `--keep 7`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import snapshot_helpers

from kiro_crew import cli
from kiro_crew.snapshot import snapshot_main

_no_gateway = snapshot_helpers._no_gateway


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setattr("sys.argv", ["kirocrew"] + argv)
    try:
        cli.main()
        return 0
    except SystemExit as exc:
        return exc.code


@pytest.mark.parametrize("word", ["list", "ls"])
def test_a_bare_listing_word_is_refused_and_writes_nothing(
    word: str, cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run_cli(["snapshot", word], monkeypatch) == 1

    assert list(cwd.iterdir()) == [], "the refused command must not create a directory"
    out = capsys.readouterr().out
    assert "--list" in out
    assert "no snapshot was taken" in out


def test_the_refusal_also_holds_without_the_cli_parser(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert snapshot_main(["list"]) == 1
    assert not (cwd / "list").exists()
    assert "--list" in capsys.readouterr().out


def test_an_existing_directory_named_list_is_still_a_valid_output_dir(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user made the directory, so the word names it: the guard steps aside and
    # the ordinary path runs (here the listing, so no bundle is built in the test).
    (cwd / "list").mkdir()
    assert _run_cli(["snapshot", "list", "--list"], monkeypatch) == 0
    assert "No snapshots found in list" in capsys.readouterr().out


def test_an_explicit_path_spelling_is_not_refused(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `./list` is a path, not the bare verb, so it is taken as written.
    assert _run_cli(["snapshot", "./list", "--list"], monkeypatch) == 0
    assert "did you mean" not in capsys.readouterr().out.lower()
