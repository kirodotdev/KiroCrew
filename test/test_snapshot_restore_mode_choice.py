"""`kirocrew restore` never picks a mode by itself.

Without `--mode`, the command asks on a terminal and refuses off a terminal. The presence
of a live `memory.db` does not select a mode, and Replace is never a default.
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
from pathlib import Path

import pytest
from snapshot_helpers import unpinnable_argv

from kiro_crew import snapshot as snap

LIVE = b'{"marker": "live"}'
ARCHIVED = b'{"marker": "archive"}'


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Interrupt(io.StringIO):
    def isatty(self) -> bool:
        return True

    def readline(self, *a, **k):
        raise KeyboardInterrupt


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.json").write_bytes(LIVE)
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


@pytest.fixture
def bundle(tmp_path) -> Path:
    payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
    payload.mkdir()
    (payload / "config.json").write_bytes(ARCHIVED)
    (payload / "MANIFEST.json").write_text(
        '{"version": 3, "components": {"config": "unresolved"}}', encoding="utf-8"
    )
    out = tmp_path / "b.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(payload), arcname=payload.name)
    return out


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _no_terminal(monkeypatch) -> io.StringIO:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", out)
    return out


def _terminal(monkeypatch, answer: str, stdin: io.StringIO | None = None) -> io.StringIO:
    out = _Tty()
    monkeypatch.setattr(sys, "stdin", stdin if stdin is not None else _Tty(answer))
    monkeypatch.setattr(sys, "stdout", out)
    return out


def _make_memory_db(home: Path) -> None:
    conn = snap.sqlite3.connect(str(home / "memory.db"))
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.commit()
    conn.close()


class TestNoModeOffTerminal:
    def test_refused_without_memory_db(self, home, bundle, monkeypatch):
        out = _no_terminal(monkeypatch)
        before = _tree(home)
        rc = snap.restore_main([str(bundle)])
        text = out.getvalue()
        assert rc != 0, text
        assert "--mode merge" in text and "--mode replace" in text, text
        assert "terminal" in text, text
        assert _tree(home) == before
        assert sorted(p.name for p in home.iterdir()) == sorted(before)

    def test_refused_with_memory_db(self, home, bundle, monkeypatch):
        _make_memory_db(home)
        out = _no_terminal(monkeypatch)
        before = _tree(home)
        rc = snap.restore_main([str(bundle)])
        text = out.getvalue()
        assert rc != 0, text
        assert "--mode merge" in text and "--mode replace" in text, text
        assert _tree(home) == before

    def test_dry_run_is_refused_too(self, home, bundle, monkeypatch):
        out = _no_terminal(monkeypatch)
        rc = snap.restore_main([str(bundle), "--dry-run"])
        text = out.getvalue()
        assert rc != 0, text
        assert "--mode merge" in text and "--mode replace" in text, text


class TestNoModeOnTerminal:
    def test_prompt_lists_both_modes_and_preselects_none(self, home, bundle, monkeypatch):
        out = _terminal(monkeypatch, "merge\n")
        snap.restore_main([str(bundle), *unpinnable_argv()])
        text = out.getvalue().lower()
        assert "merge" in text and "replace" in text
        assert "default" not in text

    def test_merge_answer_runs_merge(self, home, bundle, monkeypatch):
        stdin = _Tty("merge\n")
        out = _terminal(monkeypatch, "", stdin=stdin)
        rc = snap.restore_main([str(bundle), *unpinnable_argv()])
        assert rc == 0, out.getvalue()
        assert stdin.read() == "", "the answer was never asked for"
        assert (home / "config.json").read_bytes() == LIVE

    def test_replace_answer_runs_replace(self, home, bundle, monkeypatch):
        stdin = _Tty("replace\n")
        out = _terminal(monkeypatch, "", stdin=stdin)
        # --include-config: a Replace keeps the live settings documents unless they are picked.
        rc = snap.restore_main([str(bundle), "--include-config", *unpinnable_argv()])
        assert rc == 0, out.getvalue()
        assert stdin.read() == "", "the answer was never asked for"
        assert (home / "config.json").read_bytes() == ARCHIVED

    @pytest.mark.parametrize(
        "answer",
        ["\n", "maybe\n", "Replace now\n", ""],
        ids=["empty", "garbage", "extra_words", "eof"],
    )
    def test_other_answers_abort_and_change_nothing(self, home, bundle, monkeypatch, answer):
        out = _terminal(monkeypatch, answer)
        before = _tree(home)
        rc = snap.restore_main([str(bundle)])
        assert rc != 0, out.getvalue()
        assert _tree(home) == before

    def test_ctrl_c_aborts_and_changes_nothing(self, home, bundle, monkeypatch):
        out = _terminal(monkeypatch, "", stdin=_Interrupt())
        before = _tree(home)
        try:
            rc = snap.restore_main([str(bundle)])
        except KeyboardInterrupt:
            rc = 130
        assert rc != 0, out.getvalue()
        assert _tree(home) == before

    def test_dry_run_asks_and_writes_nothing(self, home, bundle, monkeypatch):
        stdin = _Tty("replace\n")
        out = _terminal(monkeypatch, "", stdin=stdin)
        before = _tree(home)
        rc = snap.restore_main([str(bundle), "--dry-run", *unpinnable_argv()])
        text = out.getvalue()
        assert rc == 0, text
        assert stdin.read() == "", "the answer was never asked for"
        assert "replace mode" in text, text
        assert _tree(home) == before


class TestHelpNoLongerStatesTheOldDefault:
    def test_registered_parser_help(self):
        from kiro_crew.cli_parser_admin import register_restore_parser

        root = argparse.ArgumentParser()
        sub = root.add_subparsers()
        register_restore_parser(sub)
        text = sub.choices["restore"].format_help().lower()
        assert "auto-detect" not in text
        assert "default" not in text.split("--mode", 1)[1].split("--dry-run", 1)[0]

    @pytest.mark.parametrize(
        "relpath",
        [
            "docs/system-specs/modules/cli.md",
            "src/kiro_crew/docs/snapshot-and-restore.md",
        ],
    )
    def test_docs_do_not_state_an_auto_detected_mode(self, relpath):
        text = (Path(__file__).resolve().parents[1] / relpath).read_text(encoding="utf-8")
        assert "auto-detect" not in text.lower().replace("auto-detects the project", "")

    def test_bundled_skill_does_not_state_a_memory_db_based_mode(self):
        skill = "src/kiro_crew/builtin_skills/kirocrew-commands/SKILL.md"
        text = (Path(__file__).resolve().parents[1] / skill).read_text(encoding="utf-8")
        assert "merge when `memory.db` exists" not in text
