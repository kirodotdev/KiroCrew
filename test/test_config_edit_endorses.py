"""``kirocrew config edit`` publishes the edit as Kiro Crew's own write.

Where the sandbox seals the keystone files, a ``config.json`` that differs from its
endorsed copy is pending until the owner applies it (``config.loosening_provenance``).
``config edit`` runs the editor on a copy staged in the sealed ``config-provenance``
directory and publishes the result through ``update_config_locked``, so the edit takes
effect at once; it refuses while the file is already pending, and when the file
changed while the editor was open.

Both editor launch seams are replaced in every test: ``_run_editor``, and ``os.execvp``
emulated as an exec (edit, then exit), so no real editor ever runs. The Ctrl-C test keeps
the real ``_run_editor`` and replaces the ``subprocess.Popen`` beneath it instead.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp dir.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path
from typing import Callable

import pytest
from config_pending_helpers import make_config_json_pending

from kiro_crew.cli_config import _config_cmd
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.loader import config_path, update_config_locked
from kiro_crew.config.loader_document import PendingEditBlocksSave


def _set_bot_name(name: str) -> Callable[[dict], dict]:
    def mutate(doc: dict) -> dict:
        doc.setdefault("agent", {})["bot_name"] = name
        return doc

    return mutate


def _edit_bot_name(path: Path, name: str) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    _set_bot_name(name)(doc)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _install_editor(
    monkeypatch: pytest.MonkeyPatch, edit: Callable[[Path], None], status: int = 0
) -> list[Path]:
    """Route both launch seams to *edit*; return the paths the editor was handed."""
    calls: list[Path] = []

    def run_editor(_editor: str, path: Path) -> int:
        calls.append(Path(path))
        edit(Path(path))
        return status

    def execvp(_file: str, argv: list[str]) -> None:
        calls.append(Path(argv[-1]))
        edit(Path(argv[-1]))
        raise SystemExit(0)

    monkeypatch.setattr("kiro_crew.cli_config._run_editor", run_editor, raising=False)
    monkeypatch.setattr(os, "execvp", execvp)
    monkeypatch.setenv("EDITOR", "fake-editor")
    return calls


def _run_edit() -> None:
    _config_cmd(argparse.Namespace(config_action="edit"))


pytestmark = pytest.mark.usefixtures("enforcing")


@pytest.fixture
def enforcing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    update_config_locked(config_path(), mutate=_set_bot_name("Alpha"))
    assert not lp.leaf_pending(lp.LEAF_CONFIG)


def test_an_edit_takes_effect_without_an_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_editor(monkeypatch, lambda p: _edit_bot_name(p, "Beta"))

    try:
        _run_edit()
    except SystemExit as exc:
        assert exc.code in (0, None)

    assert len(calls) == 1
    assert not lp.leaf_pending(lp.LEAF_CONFIG)
    assert KiroCrewConfig.load().agent.bot_name == "Beta"


def test_a_pending_config_json_is_refused_before_the_editor_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = make_config_json_pending(monkeypatch)
    pending = path.read_bytes()
    calls = _install_editor(monkeypatch, lambda p: _edit_bot_name(p, "Beta"))

    with pytest.raises(PendingEditBlocksSave):
        _run_edit()

    assert calls == []
    assert path.read_bytes() == pending
    assert lp.leaf_pending(lp.LEAF_CONFIG)


def test_a_change_while_the_editor_was_open_is_refused_and_keeps_the_edit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def edit_and_race(path: Path) -> None:
        _edit_bot_name(path, "Beta")
        update_config_locked(config_path(), mutate=_set_bot_name("Concurrent"))

    _install_editor(monkeypatch, edit_and_race)

    with pytest.raises(SystemExit) as exc:
        _run_edit()

    assert exc.value.code == 1
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1
    assert "changed while the editor was open" in err
    assert not lp.leaf_pending(lp.LEAF_CONFIG)
    assert KiroCrewConfig.load().agent.bot_name == "Concurrent"
    assert "kept at " in err, err
    kept = Path(err.rsplit("kept at ", 1)[1])
    assert json.loads(kept.read_text(encoding="utf-8"))["agent"]["bot_name"] == "Beta"


def test_an_editor_that_exits_non_zero_keeps_the_saved_edit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_editor(monkeypatch, lambda p: _edit_bot_name(p, "Beta"), status=1)

    with pytest.raises(SystemExit) as exc:
        _run_edit()

    assert exc.value.code == 1
    err = capsys.readouterr().err.strip()
    assert "exited with status 1" in err
    assert KiroCrewConfig.load().agent.bot_name == "Alpha"
    kept = Path(err.rsplit("kept at ", 1)[1])
    assert json.loads(kept.read_text(encoding="utf-8"))["agent"]["bot_name"] == "Beta"


def test_a_corrupt_config_json_can_be_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    config_path().write_text("{bad", encoding="utf-8")
    good = json.dumps({"agent": {"bot_name": "Fixed"}}) + "\n"
    _install_editor(monkeypatch, lambda p: p.write_text(good, encoding="utf-8"))

    try:
        _run_edit()
    except SystemExit as exc:
        assert exc.code in (0, None)

    assert KiroCrewConfig.load().agent.bot_name == "Fixed"


class _EditorInterruptedAtTheTerminal:
    """A ``Popen`` stand-in: the editor saves an edit, then Ctrl-C reaches this process.

    The terminal sends Ctrl-C to its whole foreground group, so a parent waiting on an
    editor that runs a shell command (vim's ``:!sleep 60``) receives it too.
    """

    def __init__(self, args: list[str], **_kwargs: object) -> None:
        self.args = args
        self.returncode: int | None = None

    def __enter__(self) -> _EditorInterruptedAtTheTerminal:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            _edit_bot_name(Path(self.args[-1]), "Beta")
            signal.raise_signal(signal.SIGINT)
            self.returncode = 0
        return self.returncode

    def communicate(self, _input: object = None, timeout: float | None = None) -> tuple[None, None]:
        self.wait(timeout)
        return None, None

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_ctrl_c_while_the_editor_runs_does_not_stop_config_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def execvp(_file: str, _argv: list[str]) -> None:
        raise AssertionError("config edit must not exec the editor in place")

    monkeypatch.setattr("kiro_crew.cli_config.subprocess.Popen", _EditorInterruptedAtTheTerminal)
    monkeypatch.setattr(os, "execvp", execvp)
    monkeypatch.setenv("EDITOR", "fake-editor")
    # A launcher can start the worker with SIGINT ignored, which would hide the bug,
    # so the test runs under Python's own Ctrl-C handler.
    inherited = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        try:
            _run_edit()
        except KeyboardInterrupt:
            pytest.fail("Ctrl-C at the terminal stopped config edit and lost the edit")
        except SystemExit as exc:
            assert exc.code in (0, None)
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, inherited)

    assert not lp.leaf_pending(lp.LEAF_CONFIG)
    assert KiroCrewConfig.load().agent.bot_name == "Beta"
