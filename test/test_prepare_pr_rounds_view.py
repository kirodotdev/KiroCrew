"""Tests for `pr_findings.py --rounds`, the prepare-pr loop's cross-round memory.

The view is rebuilt from the PR's own writer-authored disposition comments, so
nothing lives on disk and the record outlives the loop. Three properties are
load-bearing:

- one round per judged `head=`, in first-disposed order, regardless of which
  lane each comment targets;
- the two optional plain lines (`self-added:`, `mechanism:`) are read from the
  comment body OUTSIDE the `> ` block, and a span disposed twice in one round
  counts that round once;
- the retrospective trigger is an EXIT CODE (30): a span at three rounds, or the
  next round being the 3rd/6th/9th.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "pr_findings.py"
)


def _load() -> ModuleType:
    return load_skill_script("prepare_pr_findings_rounds", SCRIPT)


def _disp(cid: int, target: str, head: str, span: str | None, extra: str = "") -> dict:
    marker = "<!-- ai-review-disposition target={} head={} -->\n".format(target, head)
    claim = "**fixed** — span={}\n".format(span) if span else "**fixed** — a watch item\n"
    body = marker + claim + extra + "\n> rationale line naming span=deadbeef0000 as evidence only\n"
    return {"id": cid, "body": body, "user": {"login": "writer"}}


def _wire(module: ModuleType, monkeypatch, comments: list[dict]) -> None:
    monkeypatch.setattr(module, "fetch_disposition_comments", lambda repo, number: comments)
    monkeypatch.setattr(
        module,
        "writer_disposition_records",
        lambda repo, cs: [module.parse_disposition_record(c) for c in cs],
    )


def test_rounds_are_grouped_by_judged_head_in_order(monkeypatch, capsys):
    mod = _load()
    h0 = "a" * 40
    h1 = "b" * 40
    comments = [
        _disp(1, "gpt", h0, "aaaaaaaaaaa1", "self-added: no\n"),
        _disp(2, "design", h1, None, "mechanism: retry guard on add\n"),
        _disp(3, "gpt", h1, "aaaaaaaaaaa1", "self-added: yes\n"),
        _disp(4, "gpt", h1, "aaaaaaaaaaa2", "self-added: yes\nmechanism: digest suffix on path\n"),
    ]
    _wire(mod, monkeypatch, comments)
    rc = mod.rounds_view("o/r", 7, "c" * 40, {"additions": 10, "deletions": 2})
    out = capsys.readouterr().out
    assert rc == 30, "two rounds disposed -> the next is the 3rd -> retrospective due"
    assert (
        "- round 0 — head aaaaaaaaaaaa — 1 disposition(s) [gpt×1] — spans: aaaaaaaaaaa1 — self-added: 0"
        in out
    )
    assert (
        "- round 1 — head bbbbbbbbbbbb — 3 disposition(s) [design×1, gpt×2] — spans: aaaaaaaaaaa1, aaaaaaaaaaa2 — self-added: 2"
        in out
    )
    assert "    mechanism: retry guard on add" in out
    assert "    mechanism: digest suffix on path" in out
    assert "size now: +10/-2" in out
    assert "next round: 2" in out
    assert "findings in self-added code: 2   mechanisms declared: 2" in out
    assert "recurring spans (≥3 rounds): none" in out
    # a span= inside a `> ` line is evidence, not a claim
    assert "deadbeef0000" not in out.split("spans:")[1].split("\n")[0]


def test_exit_30_on_third_round_and_on_a_span_at_three(monkeypatch, capsys):
    mod = _load()
    heads = ["1" * 40, "2" * 40, "3" * 40, "4" * 40]
    # two rounds disposed -> next is the 3rd -> due
    _wire(
        mod,
        monkeypatch,
        [_disp(1, "gpt", heads[0], "aaaaaaaaaaa1"), _disp(2, "gpt", heads[1], "aaaaaaaaaaa2")],
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    assert "RETROSPECTIVE DUE" in capsys.readouterr().out
    # four rounds, no span repeated -> next is the 5th -> not due
    _wire(
        mod,
        monkeypatch,
        [
            _disp(1, "gpt", heads[0], "aaaaaaaaaaa1"),
            _disp(2, "gpt", heads[1], "aaaaaaaaaaa2"),
            _disp(3, "gpt", heads[2], "aaaaaaaaaaa3"),
            _disp(4, "gpt", heads[3], "aaaaaaaaaaa4"),
        ],
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    # the same span in three rounds -> due regardless; twice in one round counts once
    _wire(
        mod,
        monkeypatch,
        [
            _disp(1, "gpt", heads[0], "aaaaaaaaaaa1"),
            _disp(2, "gpt", heads[1], "aaaaaaaaaaa1"),
            _disp(3, "gpt", heads[1], "aaaaaaaaaaa1"),
            _disp(4, "gpt", heads[2], "aaaaaaaaaaa1"),
            _disp(5, "gpt", heads[3], "aaaaaaaaaaa9"),
        ],
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    assert "  aaaaaaaaaaa1 ×3" in capsys.readouterr().out


def test_rounds_view_fails_closed_when_comments_are_unreadable(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "fetch_disposition_comments", lambda repo, number: None)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 2
    monkeypatch.setattr(mod, "fetch_disposition_comments", lambda repo, number: [])
    monkeypatch.setattr(mod, "writer_disposition_records", lambda repo, cs: None)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 2


def test_skill_wires_the_rounds_view_and_the_two_lines():
    skill = (SCRIPT.parent.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "pr_findings.py <pr#> --rounds" in skill
    assert "prepare-pr-intent" in skill
    assert "`self-added: yes|no`" in skill and "`mechanism: <one line>`" in skill
    assert "round_notes" not in skill
    assert "## Three questions per finding" in skill
