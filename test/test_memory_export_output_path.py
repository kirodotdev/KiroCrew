"""``memory export --output`` writes into a missing folder, and reports a path it
cannot write as one line.

Pinned: a missing parent folder is created (as ``perf`` and ``manifest`` do); a
path that still cannot be written is one ``Error:`` line on stderr, exit 1, no
success line, never a traceback. A newline or terminal control in the path
cannot split that line or reach the terminal live.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.terminal_safe import safe_terminal_line
from kiro_crew.vector_memory import VectorMemoryStore


def _seed_default_store() -> None:
    db_path = resolve_store_path(DEFAULT_MEMORY_STORE)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = VectorMemoryStore(db_path=db_path)
    store.init()
    try:
        assert store.write_episodic("The crew shipped a release.", source="test") is True
    finally:
        store.close()


def _export(out: Path) -> None:
    try:
        cc._memory_cmd(argparse.Namespace(mem_action="export", output=str(out), store=None))
    except OSError as exc:
        pytest.fail(f"a raw {type(exc).__name__} escaped instead of an error line: {exc}")


def test_a_missing_parent_folder_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_default_store()
    out = tmp_path / "missing" / "deeper" / "export.json"
    _export(out)
    assert len(json.loads(out.read_text(encoding="utf-8"))["episodic"]) == 1
    assert f"Exported to {out}" in capsys.readouterr().out


# Every case is unwritable through a plain directory or a plain file that the
# test creates, so the setup uses only names every platform accepts. The last
# case puts the control characters in the part of the path that is never
# created: its parent is a regular file, so the write fails on every platform.
@pytest.mark.parametrize(
    "target", ["a-directory", "a-file/export.json", "a-file/two\nlines\x1b[2J.json"]
)
def test_an_unwritable_output_path_is_one_error_line_and_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    _seed_default_store()
    (tmp_path / "a-directory").mkdir()
    (tmp_path / "a-file").write_text("", encoding="utf-8")
    out = tmp_path / target
    with pytest.raises(SystemExit) as exc_info:
        _export(out)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    err_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(err_lines) == 1, captured.err
    assert err_lines[0].startswith(f"Error: could not write {safe_terminal_line(str(out))}: ")
    assert "\x1b" not in captured.err
    assert "Exported to" not in captured.out
