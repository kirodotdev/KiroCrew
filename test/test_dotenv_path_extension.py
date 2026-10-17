"""A ``PATH`` line in the data home's ``.env`` extends the process ``PATH``.

``load_credentials`` seeds the environment from ``.env`` with ``setdefault``, and a
process always has a ``PATH``, so a ``PATH=`` line there could never change it. The
agent's shell inherits the gateway's environment, so ``.env`` had no way to put an
extra directory on it. The line is read as directories to add behind the existing
``PATH``; every other key keeps its ``setdefault`` meaning.

Each test drives the real ``load_credentials`` over a real ``.env`` file. The
directories only have to be absolute, so none of them is created.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kiro_crew import env
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig

SEP = os.pathsep


def _dirs(root: Path, *names: str) -> list[str]:
    return [str(root / name) for name in names]


def _setup(monkeypatch: pytest.MonkeyPatch, root: Path, lines: list[str], base: str) -> None:
    """Write a ``.env`` holding *lines* and start the process with ``PATH=base``."""
    dotenv = root / ".env"
    dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: dotenv)
    monkeypatch.setattr(env, "_warned_bad_bin_dirs", set())
    # load_credentials overlays every recognised credential from the environment,
    # so a shell exporting one must not change what these tests read back.
    for key in loader.CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Every other name a line seeds starts unset, so ``setdefault`` takes the file's value.
    # ``load_credentials`` writes into ``os.environ`` itself and ``delenv(raising=False)``
    # records an undo only for a variable that exists, so each name is set first: a name
    # the process never held would otherwise stay set for every later test on the worker.
    # This runs before ``PATH`` is set because on Windows ``Path`` and ``PATH`` are one variable.
    for line in lines:
        name = line.partition("=")[0]
        if name != "PATH":
            monkeypatch.setenv(name, "x")
            monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", base)


def _load(*, propagate: bool = True) -> None:
    KiroCrewConfig.__new__(KiroCrewConfig).load_credentials(propagate=propagate)


def _path() -> list[str]:
    return os.environ["PATH"].split(SEP)


class TestAppendsBehindThePath:
    def test_the_directories_land_after_the_existing_path_which_is_kept_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sys_a, sys_b = _dirs(tmp_path, "sys-a", "sys-b")
        extra_c, extra_d = _dirs(tmp_path, "extra-c", "extra-d")
        # An empty entry and a repeat are legal in a PATH; the string the process
        # started with has to survive untouched in front of the additions.
        base = SEP.join([sys_a, "", sys_b, sys_a])
        _setup(monkeypatch, tmp_path, [f"PATH={extra_c}{SEP}{extra_d}"], base)
        _load()
        assert os.environ["PATH"] == SEP.join([base, extra_c, extra_d])


class TestEachDirectoryIsAddedOnce:
    def test_a_directory_the_path_already_holds_is_not_added_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        held, other = _dirs(tmp_path, "held", "other")
        extra = _dirs(tmp_path, "extra")[0]
        _setup(monkeypatch, tmp_path, [f"PATH={held}{SEP}{extra}"], SEP.join([other, held]))
        _load()
        assert _path().count(held) == 1
        assert _path().count(extra) == 1

    def test_loading_again_does_not_grow_the_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = _dirs(tmp_path, "extra")[0]
        _setup(monkeypatch, tmp_path, [f"PATH={extra}"], _dirs(tmp_path, "sys")[0])
        _load()
        once = os.environ["PATH"]
        _load()
        _load()
        assert once.split(SEP).count(extra) == 1
        assert os.environ["PATH"] == once


class TestOnlyAbsoluteDirectoriesPass:
    def test_entries_that_are_not_an_absolute_directory_are_dropped_with_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        base = _dirs(tmp_path, "sys")[0]
        good = _dirs(tmp_path, "good")[0]
        # A relative entry would resolve against the child's cwd; "$HOME/bin" is
        # shell syntax that .env does not expand. Empty segments are just noise.
        line = SEP.join(["relative/bin", "$HOME/bin", "", good, ""])
        _setup(monkeypatch, tmp_path, [f"PATH={line}"], base)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.env"):
            _load()
        assert [entry for entry in _path() if entry != base] == [good]
        dropped = [r.getMessage() for r in caplog.records if ".env PATH" in r.getMessage()]
        assert len(dropped) == 2
        assert any("relative/bin" in m for m in dropped)
        assert any("$HOME/bin" in m for m in dropped)

    def test_a_tilde_entry_expands_to_the_home_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        base = _dirs(tmp_path, "sys")[0]
        _setup(monkeypatch, tmp_path, ["PATH=~/bin"], base)
        _load()
        added = [entry for entry in _path() if entry != base]
        assert added == [os.path.expanduser("~/bin")]
        assert os.path.isabs(added[0])


class TestPathIsASupportedKey:
    def test_a_path_line_draws_no_unknown_key_warning_but_a_plain_unknown_key_still_does(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        extra = _dirs(tmp_path, "extra")[0]
        _setup(
            monkeypatch,
            tmp_path,
            [f"PATH={extra}", "KIROCREW_TEST_PLAIN=1"],
            _dirs(tmp_path, "sys")[0],
        )
        monkeypatch.setattr(loader, "_warned_env_keys", set())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            _load()
        warned = [r.getMessage() for r in caplog.records if "Unknown key" in r.getMessage()]
        assert any("Unknown key KIROCREW_TEST_PLAIN " in m for m in warned)
        assert not any("Unknown key PATH " in m for m in warned)


class TestNothingElseChanges:
    def test_other_keys_keep_their_setdefault_meaning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = _dirs(tmp_path, "extra")[0]
        lines = ["KIROCREW_TEST_HELD=from-env", "KIROCREW_TEST_NEW=from-env", f"PATH={extra}"]
        _setup(monkeypatch, tmp_path, lines, _dirs(tmp_path, "sys")[0])
        monkeypatch.setenv("KIROCREW_TEST_HELD", "from-process")
        _load()
        assert os.environ["KIROCREW_TEST_HELD"] == "from-process"
        assert os.environ["KIROCREW_TEST_NEW"] == "from-env"

    def test_a_lookalike_name_is_the_path_only_where_the_platform_folds_case(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = _dirs(tmp_path, "sys")[0]
        lookalike = _dirs(tmp_path, "lookalike")[0]
        _setup(monkeypatch, tmp_path, [f"Path={lookalike}"], base)
        _load()
        if os.name == "nt":
            assert lookalike in _path()
        else:
            assert os.environ["PATH"] == base
            assert os.environ["Path"] == lookalike

    def test_a_load_that_does_not_propagate_leaves_the_environment_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = _dirs(tmp_path, "sys")[0]
        _setup(monkeypatch, tmp_path, [f"PATH={_dirs(tmp_path, 'extra')[0]}"], base)
        _load(propagate=False)
        assert os.environ["PATH"] == base


class TestSeededNamesDoNotOutliveTheTest:
    def test_a_name_a_line_seeds_is_unset_again_once_the_undo_has_run(self, tmp_path: Path) -> None:
        """``load_credentials`` writes into ``os.environ`` itself, and an undo takes the
        value back out only if one was recorded. Neither name is one the process holds, which
        is the case ``delenv(raising=False)`` records nothing for."""
        names = ["KIROCREW_TEST_SEED_A", "KIROCREW_TEST_SEED_B"]
        assert not [name for name in names if name in os.environ]
        private = pytest.MonkeyPatch()
        try:
            lines = [f"{name}=seeded" for name in names] + [f"PATH={_dirs(tmp_path, 'extra')[0]}"]
            _setup(private, tmp_path, lines, _dirs(tmp_path, "sys")[0])
            _load()
            # Control: the load did write both names, so their absence below is the undo's doing.
            assert [os.environ.get(name) for name in names] == ["seeded", "seeded"]
        finally:
            private.undo()
        assert not [name for name in names if name in os.environ]
