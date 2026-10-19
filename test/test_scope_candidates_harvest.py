"""``scope_candidates.py harvest``: a confirmed scope row becomes a corpus edit.

The Security Scope Review lane confirms boundary rows with the real classifier
and prints them in its PR comment. ``harvest`` appends them to the committed
golden-paths corpus, so the denial differential checks them on every run.
These tests pin the rule it holds: only confirmed rows, in the shipped corpus's
own shape, and never in a way that softens either lane's verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_scope_candidates as base

scope = base.scope
deny_diff = base.deny_diff
_report = base._report
_row = base._row
_write = base._write

SHIPPED_CORPUS = (
    base.ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor" / "golden-paths.json"
)


class TestHarvestCarriesConfirmedRowsIntoTheCorpus:
    REGRESSION = base.TestAConfirmedRowSurvivesARerunTheModelDoesNotRepropose.REGRESSION

    def _rows(self, tmp_path: Path) -> Path:
        report = tmp_path / "leg.json"
        report.write_text(
            json.dumps(_report("any", classified=1, regressions=[self.REGRESSION])),
            encoding="utf-8",
        )
        rows = tmp_path / "rows.json"
        assert scope.main(["verdict", "--report", f"posix={report}", "--out-rows", str(rows)]) == 1
        return rows

    def _harvest(self, rows: Path, corpus: Path, surface: str = "gh-read") -> int:
        return scope.main(
            [
                "harvest",
                "--rows",
                str(rows),
                "--corpus",
                str(corpus),
                "--surface",
                surface,
            ]
        )

    def test_a_confirmed_row_is_appended_in_the_shipped_corpus_shape(self, tmp_path: Path) -> None:
        corpus = tmp_path / "golden-paths.json"
        corpus.write_text(SHIPPED_CORPUS.read_text(encoding="utf-8"), encoding="utf-8")
        before = json.loads(corpus.read_text(encoding="utf-8"))

        assert self._harvest(self._rows(tmp_path), corpus) == 0

        after = json.loads(corpus.read_text(encoding="utf-8"))
        # Every existing byte of meaning stays: the header, and each row in order.
        assert after["_about"] == before["_about"]
        assert after["golden_paths"][:-1] == before["golden_paths"]
        added = after["golden_paths"][-1]
        assert added == {
            "kind": "shell",
            "surface": "gh-read",
            "command_or_flow": "gh pr view 1",
            "platform": "any",
            "reason": "a read-only query the product depends on",
        }
        # Same keys as every shipped row, so the ledger import reads it too.
        assert set(added) == set(before["golden_paths"][0])
        assert len(deny_diff.load_corpus(corpus)) == len(before["golden_paths"]) + 1

    def test_a_second_harvest_adds_nothing_and_says_so(self, tmp_path: Path) -> None:
        rows = self._rows(tmp_path)
        corpus = _write(tmp_path / "corpus.json", [_row("git status")])
        assert self._harvest(rows, corpus) == 0
        once = corpus.read_bytes()

        assert self._harvest(rows, corpus) == 3
        assert corpus.read_bytes() == once

    @pytest.mark.parametrize(
        ("row", "surface"),
        [
            (_row("gh pr view 1", kind="flow"), "gh-read"),
            (_row("gh pr view 1", reason="  "), "gh-read"),
            (_row("aws s3 ls --profile [REDACTED-KEY]"), "gh-read"),
            (_row("curl -H 'Authorization: [REDACTED]' localhost"), "gh-read"),
            (_row("gh pr view 1"), " "),
        ],
        ids=["not-shell", "blank-reason", "redacted", "redacted-value", "blank-surface"],
    )
    def test_a_row_the_corpus_cannot_use_is_refused_and_nothing_is_written(
        self, tmp_path: Path, row: dict, surface: str
    ) -> None:
        rows = _write(tmp_path / "rows.json", [row])
        corpus = _write(tmp_path / "corpus.json", [_row("git status")])
        before = corpus.read_bytes()

        assert self._harvest(rows, corpus, surface) == 2
        assert corpus.read_bytes() == before

    def test_a_harvested_row_cannot_turn_either_lane_s_block_into_a_pass(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The denial differential, over a fake classifier: the head refuses two
        # commands the base allows. One is already a corpus row; harvesting adds
        # the other plus a row both refs allow.
        refused_at_head = {"git push origin feat", "gh pr view 1"}

        def classify(checkout, commands, *, home, side="head", tiers=None):
            denied = refused_at_head if side == "head" else set()
            return [deny_diff.Verdict(denied=c in denied, reason="r") for c in commands], []

        monkeypatch.setattr(deny_diff, "resolve_checkout", lambda root, ref, dest: dest)
        monkeypatch.setattr(deny_diff, "load_shell_deny_tiers", lambda root: ())
        monkeypatch.setattr(deny_diff, "_rev_parse", lambda root, ref: ref)
        monkeypatch.setattr(deny_diff, "classify", classify)

        def regressions(corpus: Path) -> tuple[set[str], int]:
            report = deny_diff.differential(
                tmp_path, "base", "head", corpus, platform="posix", workdir=tmp_path / "w"
            )
            return {row.command for row, _ in report.regressions}, report.exit_code

        corpus = _write(
            tmp_path / "corpus.json", [_row("git push origin feat"), _row("git status")]
        )
        old_rows, old_exit = regressions(corpus)
        rows = _write(tmp_path / "rows.json", [_row("gh pr view 1"), _row("git log -1")])
        assert self._harvest(rows, corpus) == 0
        new_rows, new_exit = regressions(corpus)

        # Rows are only appended, so every old regression is still one, and the
        # exit code never drops from block to pass.
        assert old_rows <= new_rows
        assert new_exit >= old_exit
        assert "gh pr view 1" in new_rows

        # The scope lane now drops the harvested row as covered. That is a hand-off,
        # not a loss: the row it drops is one the differential just reported.
        candidates = _write(tmp_path / "c.json", [_row("gh pr view 1")])
        out = tmp_path / "normalized.json"
        rc = scope.main(
            [
                "validate",
                "--candidates",
                str(candidates),
                "--corpus",
                str(corpus),
                "--out",
                str(out),
            ]
        )
        assert rc == 3
