"""Tests for the prepare-pr round_notes.py per-PR round log.

The loop reads this file at the top of every round and writes it at the end, so
three properties are load-bearing:

- it lives OUTSIDE the worktree under ``KIROCREW_HOME``, so it can neither dirty
  the tree Phase 3 pushes nor land in the diff;
- ``show`` derives span recurrence and the self-added-finding count from the
  entries -- that table is the loop's only cross-round memory, and a wrong count
  either fires a retrospective for nothing or misses the one that mattered;
- ``rm`` / ``prune`` actually delete, since a note that outlives its PR is how
  the directory grows without bound.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = str(
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "round_notes.py"
)


def _run(home: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, KIROCREW_HOME=str(home))
    return subprocess.run(
        [sys.executable, SCRIPT, "--repo", "acme/widgets", "--branch", "feat/x", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd or home),
        check=False,
    )


def _note(home: Path) -> Path:
    return home / "prepare-pr" / "acme-widgets" / "feat-x.md"


def test_show_without_a_note_is_exit_20(tmp_path):
    proc = _run(tmp_path, "show")
    assert proc.returncode == 20
    assert "no note" in proc.stderr


def test_init_writes_outside_the_worktree_and_is_idempotent(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    proc = _run(tmp_path, "init", "--intent", "seed settings from advertised models", cwd=work)
    assert proc.returncode == 0, proc.stderr
    assert _note(tmp_path).exists()
    assert not list(work.iterdir()), "init must not write into the worktree"
    body = _note(tmp_path).read_text(encoding="utf-8")
    assert "seed settings from advertised models" in body

    again = _run(tmp_path, "init", "--intent", "something else", cwd=work)
    assert again.returncode == 0
    assert "already exists" in again.stdout
    assert "something else" not in _note(tmp_path).read_text(encoding="utf-8")


def test_refuses_a_base_branch(tmp_path):
    env = dict(os.environ, KIROCREW_HOME=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--repo", "acme/widgets", "--branch", "main", "init", "--intent", "x"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 2
    assert "base branch" in proc.stderr


def test_add_records_rounds_deltas_spans_and_mechanisms(tmp_path):
    assert _run(tmp_path, "init", "--intent", "intent").returncode == 0
    assert (
        _run(
            tmp_path,
            "add",
            "--head",
            "aaaa1111bbbb2222",
            "--additions",
            "1094",
            "--deletions",
            "82",
            "--finding",
            "span=c9a9b420 | sidecar forgeable | self-added:no | fixed",
            "--mechanism",
            "ownership record sidecar",
        ).returncode
        == 0
    )
    assert (
        _run(
            tmp_path,
            "add",
            "--head",
            "cccc3333",
            "--additions",
            "1536",
            "--deletions",
            "90",
            "--finding",
            "span=c9a9b420 | visible before owner | self-added:yes | fixed",
            "--finding",
            "span=0dfda262 | pruning revokes | self-added:yes | fixed",
            "--mechanism",
            "stage-and-rename",
        ).returncode
        == 0
    )
    assert (
        _run(
            tmp_path,
            "add",
            "--head",
            "dddd4444",
            "--additions",
            "1834",
            "--deletions",
            "92",
            "--finding",
            "span=c9a9b420 | failed cleanup keeps owner | self-added:yes | rebutted",
        ).returncode
        == 0
    )

    body = _note(tmp_path).read_text(encoding="utf-8")
    assert "## Round 0 — head aaaa1111bbbb — +1094/-82 —" in body
    assert "## Round 1 — head cccc3333 — +1536/-90 (Δ +442/+8)" in body
    assert body.count("- r0: ownership record sidecar") == 1
    assert body.count("- r1: stage-and-rename") == 1
    assert "(none yet)" not in body

    shown = _run(tmp_path, "show", "--json")
    assert shown.returncode == 0, shown.stderr
    summary = json.loads(shown.stdout)
    assert summary["intent"] == "intent"
    assert summary["growth"] == {"first": 1094, "last": 1834}
    assert summary["mechanisms"] == ["r0: ownership record sidecar", "r1: stage-and-rename"]
    assert summary["recurring_spans"] == [["c9a9b420", 3]]
    assert summary["spans"]["c9a9b420"]["rounds"] == [0, 1, 2]
    assert summary["self_added_findings"] == 3

    text = _run(tmp_path, "show")
    assert "recurring spans (≥3 rounds):" in text.stdout
    assert "c9a9b420 ×3" in text.stdout
    assert "findings in self-added code: 3" in text.stdout


def test_add_rejects_a_malformed_finding(tmp_path):
    assert _run(tmp_path, "init", "--intent", "intent").returncode == 0
    proc = _run(
        tmp_path, "add", "--head", "a", "--additions", "1", "--deletions", "0",
        "--finding", "span=abc | title only",
    )
    assert proc.returncode == 2
    assert "--finding must be" in proc.stderr


def test_add_before_init_is_an_error(tmp_path):
    proc = _run(tmp_path, "add", "--head", "a", "--additions", "1", "--deletions", "0")
    assert proc.returncode == 2
    assert "init" in proc.stderr


def test_rm_and_prune_delete(tmp_path):
    assert _run(tmp_path, "init", "--intent", "intent").returncode == 0
    note = _note(tmp_path)
    assert note.exists()
    assert _run(tmp_path, "rm").returncode == 0
    assert not note.exists()
    # rm on a missing note is a no-op, not an error
    assert _run(tmp_path, "rm").returncode == 0

    assert _run(tmp_path, "init", "--intent", "intent").returncode == 0
    old = time.time() - 30 * 86400
    os.utime(note, (old, old))
    fresh_dir = tmp_path / "prepare-pr" / "acme-other"
    fresh_dir.mkdir(parents=True)
    fresh = fresh_dir / "feat-y.md"
    fresh.write_text("# fresh\n", encoding="utf-8")

    proc = _run(tmp_path, "prune", "--days", "14")
    assert proc.returncode == 0
    assert "pruned 1 note" in proc.stdout
    assert not note.exists()
    assert not note.parent.exists(), "an emptied repo directory is removed too"
    assert fresh.exists()


def test_skill_wires_the_notes_into_the_loop():
    skill = (Path(SCRIPT).parent.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "round_notes.py init" in skill
    assert "round_notes.py show" in skill
    assert "round_notes.py add" in skill
    assert "round_notes.py rm" in skill
    assert "round_notes.py prune" in skill
    # The third question is the reason the notes exist.
    assert "## Three questions per finding" in skill
    assert "self-added" in skill
