"""Tests for `pr_findings.py --rounds`, the kirocrew-prepare-pr loop's cross-round memory.

The view is rebuilt from the PR's own writer-authored disposition comments, so
nothing lives on disk and the record outlives the loop. Three properties are
load-bearing:

- one round per judged `head=`, in first-disposed order, regardless of which
  lane each comment targets;
- the two optional plain lines (`self-added:`, `mechanism:`) are read from the
  comment body OUTSIDE the `> ` block, and a span disposed twice in one round
  counts that round once;
- the retrospective trigger is an EXIT CODE (30): a span FIXED in two earlier
  rounds (SEAM REPEAT; SEAM LIMIT after four), or the next round being the
  3rd/6th/9th. A rebuttal or a deferral answered again is not a fix and never
  triggers it, and no step of the view asks a person.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import ModuleType

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "pr_findings.py"
)


def _load() -> ModuleType:
    return load_skill_script("prepare_pr_findings_rounds", SCRIPT)


def _disp(
    cid: int, target: str, head: str, span: str | None, extra: str = "", word: str = "fixed"
) -> dict:
    marker = "<!-- ai-review-disposition target={} head={} -->\n".format(target, head)
    claim = (
        "**{}** — span={}\n".format(word, span) if span else "**{}** — a watch item\n".format(word)
    )
    body = marker + claim + extra + "\n> rationale line naming span=deadbeef0000 as evidence only\n"
    return {"id": cid, "body": body, "user": {"login": "writer"}}


def _fix_counts(module: ModuleType) -> dict:
    """{span: distinct judged heads that fixed it}, from the same counter `--rounds` prints."""
    by_head, order, _, _ = module._collect_rounds("o/r", 7)
    fixes = module._review_contract.fix_rounds(order, by_head)
    return {sp: len(rs) for sp, rs in fixes.items()}


def _wire(
    module: ModuleType,
    monkeypatch,
    comments: list[dict],
    on_head: set[str] | None = None,
    pushes: int | None = 0,
) -> None:
    """Serve the dispositions, the spans on the current head (None: unreadable, so present)
    and the PR's pushes after a blocked head (None: unreadable)."""
    monkeypatch.setattr(module, "current_head_spans", lambda repo, number, head: on_head)
    monkeypatch.setattr(module, "pr_blocked_pushes", lambda repo, number, since="": pushes)
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


def test_size_line_splits_code_and_test_and_never_moves_the_exit(monkeypatch, capsys):
    mod = _load()
    first, now = "a" * 40, "c" * 40
    diffs = {
        first: "src/x.py\t10\t2\ntest/test_x.py\t5\t0\n",
        now: "src/x.py\t40\t5\ntest/test_x.py\t20\t1\nwebsite/electron/mochi/test/machineStore.test.js\t3\t0\n",
    }

    def fake_run(args):
        sha = args[2].rsplit("...", 1)[1]
        return (0, diffs[sha], "") if sha in diffs else (1, "", "No commit found")

    monkeypatch.setattr(mod, "run", fake_run)
    _wire(mod, monkeypatch, [_disp(1, "gpt", first, "aaaaaaaaaaa1")])
    assert mod.rounds_view("o/r", 7, now, {"baseRefName": "main"}) == 0
    out = capsys.readouterr().out
    assert "size at first judged head: code 12, test 5 lines   now: code 45, test 24 lines" in out
    # a first head GitHub cannot read (force-pushed away) reads as unknown
    _wire(mod, monkeypatch, [_disp(1, "gpt", "d" * 40, "aaaaaaaaaaa1")])
    assert mod.rounds_view("o/r", 7, now, {"baseRefName": "main"}) == 0
    assert "size at first judged head: unknown   now: code 45" in capsys.readouterr().out
    # a compare GitHub truncated at its file cap is not a size
    diffs[now] = "src/x.py\t1\t0\n" * mod.COMPARE_FILE_CAP
    assert mod.rounds_view("o/r", 7, now, {"baseRefName": "main"}) == 0
    assert "now: unknown" in capsys.readouterr().out


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
    # The intent lives in the PR body's frozen sections, not in a separate comment.
    assert "prepare-pr-intent" not in skill
    flat = " ".join(skill.split())
    assert "Copy the frozen goal verbatim every round." in flat
    assert "revert to `<head sha>` and redo via a smaller path" in flat
    assert "`self-added: yes|no`" in skill and "`mechanism: <one line>`" in skill
    assert "round_notes" not in skill
    assert "## Three questions per finding" in skill


def test_the_frozen_goal_line_is_printed_from_the_pr_body(monkeypatch, capsys):
    mod = _load()
    _wire(mod, monkeypatch, [])
    body = "## Problem / Motivation\r\n\r\n**Goal:**  Users can resume a chat.  \r\n"
    assert mod.rounds_view("o/r", 7, "f" * 40, {"body": body}) == 0
    assert "goal (frozen): Users can resume a chat.\n" in capsys.readouterr().out


def test_an_unfilled_or_absent_goal_line_reads_as_missing(monkeypatch, capsys):
    mod = _load()
    _wire(mod, monkeypatch, [])
    # The template ships an empty `**Goal:**` scaffold; that is not a goal.
    for pr_json in ({"body": "## Problem / Motivation\n\n**Goal:**\n\nText.\n"}, {}):
        mod.rounds_view("o/r", 7, "f" * 40, pr_json)
        assert "goal (frozen): MISSING" in capsys.readouterr().out


def test_the_goal_line_is_redacted_before_printing(monkeypatch, capsys):
    """The body is PR-controlled, so a credential in it must not reach the transcript."""
    mod = _load()
    _wire(mod, monkeypatch, [])
    secret = "ghp_" + "a" * 36
    mod.rounds_view("o/r", 7, "f" * 40, {"body": "**Goal:** leak token={}\n".format(secret)})
    out = capsys.readouterr().out
    assert "goal (frozen): leak" in out
    assert secret not in out


HEADS = ["1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40, "6" * 40, "7" * 40]
CLASS_A = "class: a hook can come from a source other than core.hooksPath\n"
CLASS_A2 = "class: a git hook can come from a source other than core hooksPath\n"
CLASS_B = "class: the export path drops the trailing newline\n"


def test_a_span_fixed_in_two_heads_prints_seam_repeat_and_exits_30(monkeypatch, capsys):
    mod = _load()
    # four rounds disposed, so the cadence alone (the 5th round) would not fire
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1", CLASS_A),
        _disp(2, "gpt", HEADS[1], "aaaaaaaaaaa1", CLASS_A2),
        _disp(3, "gpt", HEADS[2], "cccccccccc21"),
        _disp(4, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM REPEAT span=aaaaaaaaaaa1 fixed in rounds 0, 1." in out
    assert "SEAM LIMIT" not in out
    assert "remove the second writer" in out and "defer with an issue" in out
    assert "No one needs to approve" in out


def test_a_span_fixed_in_one_head_is_not_a_seam(monkeypatch, capsys):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "{:011d}{}".format(i, "c")) for i in range(4)]
    comments[1] = _disp(2, "gpt", HEADS[1], "aaaaaaaaaaa1", CLASS_A)
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert "SEAM" not in capsys.readouterr().out


def test_two_fixed_records_of_one_head_count_that_head_once_and_keep_every_span(
    monkeypatch, capsys
):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1"),
        _disp(2, "opus", HEADS[0], "aaaaaaaaaaa1"),
        _disp(3, "gpt", HEADS[0], "bbbbbbbbbbb1"),
        _disp(4, "gpt", HEADS[1], "aaaaaaaaaaa1"),
        _disp(5, "gpt", HEADS[1], "bbbbbbbbbbb1", word="rebutted"),
    ]
    _wire(mod, monkeypatch, comments)
    assert _fix_counts(mod) == {"aaaaaaaaaaa1": 2, "bbbbbbbbbbb1": 1}


def test_a_span_with_no_class_sentence_still_counts(monkeypatch, capsys):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1"),
        _disp(2, "gpt", HEADS[1], "aaaaaaaaaaa1", CLASS_A),
        _disp(3, "gpt", HEADS[2], "cccccccccc21"),
        _disp(4, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    assert "SEAM REPEAT span=aaaaaaaaaaa1" in capsys.readouterr().out


def test_a_span_fixed_in_four_heads_prints_seam_limit_with_only_narrow_or_defer(
    monkeypatch, capsys
):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1", CLASS_A) for i in range(4)]
    comments.append(_disp(9, "gpt", HEADS[4], "cccccccccc21"))
    comments.append(_disp(10, "gpt", HEADS[5], "cccccccccc22"))
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM LIMIT span=aaaaaaaaaaa1 fixed in rounds 0, 1, 2, 3." in out
    assert "SEAM REPEAT" not in out
    assert "narrow the scope, or defer with an issue" in out
    assert "remove the second writer" not in out


def test_answering_a_rebuttal_again_is_not_a_fix(monkeypatch, capsys):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1", word="rebutted") for i in range(4)]
    _wire(mod, monkeypatch, comments)
    # four rounds, the same span in all four, no fix: mentions alone do not count
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert "SEAM" not in capsys.readouterr().out


def test_the_fix_count_is_of_distinct_heads_whatever_the_class_lines_say(monkeypatch):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1", CLASS_A),
        _disp(2, "gpt", HEADS[1], "aaaaaaaaaaa1", CLASS_A2),
        _disp(3, "gpt", HEADS[2], "aaaaaaaaaaa1", CLASS_A),
        _disp(4, "gpt", HEADS[0], "bbbbbbbbbbb1", CLASS_A),
        _disp(5, "gpt", HEADS[1], "bbbbbbbbbbb1", CLASS_B),
    ]
    _wire(mod, monkeypatch, comments)
    assert _fix_counts(mod) == {"aaaaaaaaaaa1": 3, "bbbbbbbbbbb1": 2}


def test_the_findings_view_prints_a_class_sweep_line_per_span(capsys):
    mod = _load()
    mod.sweep_prompt([])
    assert capsys.readouterr().out == ""
    mod.sweep_prompt(["aaaaaaaaaaa1", "bbbbbbbbbbb1"])
    out = capsys.readouterr().out
    assert "CLASS SWEEP span=aaaaaaaaaaa1:" in out and "CLASS SWEEP span=bbbbbbbbbbb1:" in out
    assert "prepare-pr-sweep.md" in out and "references/seam-protocol.md" in out


def test_the_seam_protocol_reference_carries_the_pick_order_and_the_sweep_format():
    ref = (SCRIPT.parent.parent / "references" / "seam-protocol.md").read_text(encoding="utf-8")
    for needle in (
        "SEAM REPEAT",
        "SEAM LIMIT",
        "prepare-pr-sweep.md",
        "search: none --",
        "resolution: deferred #<issue> pinned-by <test id>",
        "Nothing here asks a person",
    ):
        assert needle in ref
    skill = (SCRIPT.parent.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "references/seam-protocol.md" in skill and "`SEAM LIMIT`" in skill


def test_unlike_class_lines_never_hide_a_seam(monkeypatch, capsys):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1", CLASS_A),
        _disp(2, "gpt", HEADS[1], "aaaaaaaaaaa1", CLASS_B),
        _disp(3, "gpt", HEADS[2], "aaaaaaaaaaa1", CLASS_B),
        _disp(4, "gpt", HEADS[3], "aaaaaaaaaaa1", CLASS_B),
    ]
    _wire(mod, monkeypatch, comments)
    # A, B, B, B: four heads fixed the span, so the next fix is its 5th
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    assert "SEAM LIMIT span=aaaaaaaaaaa1 fixed in rounds 0, 1, 2, 3." in capsys.readouterr().out
    assert _fix_counts(mod) == {"aaaaaaaaaaa1": 4}


def test_the_counter_has_no_class_similarity():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "difflib" not in src and "alike" not in src.lower() and "CLASS_ALIKE" not in src
    assert "CLASS_RE" not in src and "declared_class" not in src and "labels" not in src


def test_a_class_line_in_a_disposition_is_never_printed_or_counted(monkeypatch, capsys):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1", CLASS_A),
        _disp(2, "gpt", HEADS[1], "aaaaaaaaaaa1", CLASS_B),
        _disp(3, "gpt", HEADS[2], "cccccccccc21"),
        _disp(4, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM REPEAT span=aaaaaaaaaaa1 fixed in rounds 0, 1." in out
    assert "class (label" not in out and "trailing newline" not in out


def test_a_rebuttal_that_quotes_a_fixed_bullet_is_not_a_fix(monkeypatch, capsys):
    mod = _load()
    quote = "> - **fixed** in head 1: the earlier patch\n> Outcome: fixed in abc123\n"
    comments = []
    for i in range(4):
        body = (
            "<!-- ai-review-disposition target=gpt head={} -->\n"
            "**rebutted** — span=aaaaaaaaaaa1\n{}"
        ).format(HEADS[i], quote)
        comments.append({"id": i + 1, "body": body, "user": {"login": "writer"}})
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0, "four rebuttals are not four fixes"
    assert "SEAM" not in capsys.readouterr().out
    assert _fix_counts(mod) == {}


def test_a_fenced_block_never_declares_an_outcome():
    mod = _load()
    body = "<!-- m -->\n**rebutted** — x\n```\n**fixed** — y\nclass: inside a fence\n```\n"
    assert mod.declared_outcome(body) == "rebutted"
    assert mod.declared_outcome("```\n**fixed**\n```\n") == ""


@pytest.mark.parametrize(
    ("body", "word"),
    [
        ("<!-- m -->\n**fixed** — span=aaaaaaaaaaa1\n", "fixed"),
        ("<!-- m -->\n- **fixed** — span=aaaaaaaaaaa1\n", "fixed"),
        ("<!-- m -->\ntitle\n\n> Outcome: fixed in 7ff6176c3d. Detail\n", "fixed"),
        ("<!-- m -->\ntitle\n\n> Outcome: checked, no change needed.\n", "checked"),
        ("<!-- m -->\n**rebutted** -- span=aaaaaaaaaaa1\n> - **fixed** quoted\n", "rebutted"),
        ("<!-- m -->\n**fixed** -- span=aaaaaaaaaaa1\n> rationale\n", "fixed"),
        ("<!-- m -->\n- **deferred** span=aaaaaaaaaaa1\n> #4711\n", "deferred"),
        ("<!-- m -->\nOutcome: rebutted\n", "rebutted"),
        (
            "<!-- m -->\n- **Title** span=aaaaaaaaaaa1\nDisposition: fixed, in the next push.\n",
            "fixed",
        ),
        ("<!-- m -->\n**Disposition: rebutted (no change needed)** -- x\n", "rebutted"),
        ("<!-- m -->\n> a quoted line that says fixed\n", ""),
        ("<!-- m -->\nfixed without any marker form\n", ""),
        ("<!-- m -->\n**Refused activation puts a message in the origin chat**\n", ""),
        ("<!-- m -->\n**Fixed point iteration is slow**\n> detail\n", ""),
        ("<!-- m -->\n- **span=aaaaaaaaaaa1** Retry guard fails\n", ""),
        # the bold token must be exactly an outcome word and start the record
        ("<!-- m -->\n- **fixed: the guard** span=aaaaaaaaaaa1\n> detail\n", ""),
        ("<!-- m -->\n- **Title** span=aaaaaaaaaaa1\n**fixed** -- later line\n", ""),
        ("<!-- m -->\n- **span=aaaaaaaaaaa1** fixed in head 1083c00c5\n", ""),
    ],
)
def test_the_declared_outcome_is_read_from_the_forms_dispositions_use(body, word):
    assert _load().declared_outcome(body) == word


def test_a_quoted_outcome_line_is_a_fix_in_the_rounds_view(monkeypatch, capsys):
    mod = _load()
    comments = []
    for i in range(2):
        body = (
            "<!-- ai-review-disposition target=gpt head={} -->\n"
            "title of the finding span=aaaaaaaaaaa1\n\n> Outcome: fixed in 7ff6176c3d. Detail.\n"
        ).format(HEADS[i])
        comments.append({"id": i + 1, "body": body, "user": {"login": "writer"}})
    comments.append(_disp(8, "gpt", HEADS[2], "cccccccccc21"))
    comments.append(_disp(9, "gpt", HEADS[3], "cccccccccc22"))
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    assert "SEAM REPEAT span=aaaaaaaaaaa1 fixed in rounds 0, 1." in capsys.readouterr().out


def test_a_bold_title_with_no_outcome_line_counts_nothing(monkeypatch, capsys):
    mod = _load()
    comments = []
    for i in range(4):
        body = (
            "<!-- ai-review-disposition target=gpt head={} -->\n"
            "**Fixed point iteration is slow** span=aaaaaaaaaaa1\n\n> some rationale\n"
        ).format(HEADS[i])
        comments.append({"id": i + 1, "body": body, "user": {"login": "writer"}})
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert _fix_counts(mod) == {}


def test_evidence_inside_a_blockquoted_fence_declares_no_outcome(monkeypatch, capsys):
    mod = _load()
    body = (
        "<!-- m -->\n- **span=aaaaaaaaaaa1** rebutted\n> ```\n> Outcome: fixed in abc\n> ```\n"
        "> Outcome: rebutted, by design.\n"
    )
    assert mod.declared_outcome(body) == "rebutted"
    assert mod.declared_outcome("<!-- m -->\ntitle\n> ```\n> Outcome: fixed in abc\n> ```\n") == ""
    comments = []
    for i in range(4):
        text = (
            "<!-- ai-review-disposition target=gpt head={} -->\n"
            "**rebutted** span=aaaaaaaaaaa1\n> ```\n> Outcome: fixed in abc\n> ```\n"
            "> Outcome: rebutted, by design.\n"
        ).format(HEADS[i])
        comments.append({"id": i + 1, "body": text, "user": {"login": "writer"}})
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert "SEAM" not in capsys.readouterr().out
    assert _fix_counts(mod) == {}


def test_a_short_and_a_full_spelling_of_one_head_are_one_round(monkeypatch, capsys):
    mod = _load()
    full = "abcdef0123456789abcdef0123456789abcdef01"
    comments = [
        _disp(1, "gpt", full, "aaaaaaaaaaa1"),
        _disp(2, "gpt", full[:7], "aaaaaaaaaaa1"),
        _disp(3, "opus", full[:10], "bbbbbbbbbbb1"),
        _disp(4, "gpt", HEADS[1], "cccccccccc21"),
        _disp(5, "gpt", HEADS[2], "cccccccccc22"),
        _disp(6, "gpt", HEADS[3], "cccccccccc23"),
    ]
    _wire(mod, monkeypatch, comments)
    # four judged heads, none repeating a span: no seam, and the 5th round is not a cadence round
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "- round 0 — head abcdef012345 — 3 disposition(s)" in out
    assert "next round: 4" in out and "SEAM" not in out
    assert _fix_counts(mod) == {
        "aaaaaaaaaaa1": 1,
        "bbbbbbbbbbb1": 1,
        "cccccccccc21": 1,
        "cccccccccc22": 1,
        "cccccccccc23": 1,
    }


def test_two_different_heads_with_a_shared_short_prefix_stay_two_rounds(monkeypatch):
    mod = _load()
    comments = [
        _disp(1, "gpt", "abcdef0" + "1" * 33, "aaaaaaaaaaa1"),
        _disp(2, "gpt", "abcdef1" + "1" * 33, "aaaaaaaaaaa1"),
    ]
    _wire(mod, monkeypatch, comments)
    assert _fix_counts(mod) == {"aaaaaaaaaaa1": 2}


# Fence tracking: the opening character and length are remembered, and only a
# matching fence closes the block.
def _outcome(*lines: str) -> str:
    return _load().declared_outcome("<!-- m -->\n" + "\n".join(lines) + "\n")


def test_a_backtick_fence_inside_a_tilde_block_does_not_close_it():
    # the evidence block is ~~~markdown; the ``` inside it is content, so the quoted
    # fixed line stays evidence and the declared outcome is the later rebuttal
    assert (
        _outcome(
            "~~~markdown", "```", "> Outcome: fixed in abc", "```", "~~~", "> Outcome: rebutted."
        )
        == "rebutted"
    )
    assert _outcome("~~~markdown", "```", "> Outcome: fixed in abc", "```", "~~~") == ""


def test_a_tilde_fence_inside_a_backtick_block_does_not_close_it():
    assert _outcome(
        "```", "~~~", "> Outcome: fixed in abc", "~~~", "```", "> Outcome: rebutted."
    ) == ("rebutted")


def test_a_shorter_fence_does_not_close_a_longer_one():
    assert (
        _outcome("````", "```", "> Outcome: fixed in abc", "```", "````", "> Outcome: rebutted.")
        == "rebutted"
    )
    assert (
        _outcome("~~~~~", "~~~", "> Outcome: fixed in abc", "~~~", "~~~~~", "> Outcome: x.") == "x"
    )


def test_a_longer_closing_fence_closes_and_text_after_it_is_read():
    assert _outcome("```", "> Outcome: fixed in abc", "`````", "> Outcome: rebutted.") == "rebutted"


def test_a_closing_fence_carries_no_info_string_and_at_most_three_spaces_of_indent():
    # "```text" does not close a ``` block; four spaces of indent do not either
    assert _outcome("```", "```text", "> Outcome: fixed in abc", "```", "> Outcome: rebutted.") == (
        "rebutted"
    )
    assert _outcome("```", "    ```", "> Outcome: fixed in abc", "   ```", "> Outcome: x.") == "x"


def test_inline_code_with_backticks_and_deep_indent_are_not_fence_openers():
    assert _outcome("```inline``` text", "> Outcome: fixed in abc") == "fixed"
    assert _outcome("    ```", "> Outcome: fixed in abc") == "fixed"
    assert _outcome("   ```", "> Outcome: fixed in abc") == ""


def test_fences_are_tracked_through_blockquote_markers():
    assert (
        _outcome(
            "> ~~~markdown",
            "> ```",
            "> Outcome: fixed in abc",
            "> ```",
            "> ~~~",
            "> Outcome: rebutted.",
        )
        == "rebutted"
    )
    assert _outcome(">> ```", ">> Outcome: fixed in abc", ">> ```", "> Outcome: rebutted.") == (
        "rebutted"
    )


def test_a_block_that_never_closes_hides_what_follows_so_it_counts_nothing():
    assert _outcome("```", "> Outcome: fixed in abc") == ""


def test_a_nested_fence_in_evidence_is_not_a_false_fix_in_the_rounds_view(monkeypatch, capsys):
    mod = _load()
    comments = []
    for i in range(4):
        text = (
            "<!-- ai-review-disposition target=gpt head={} -->\n"
            "**rebutted** span=aaaaaaaaaaa1\n~~~markdown\n```\n- **fixed** quoted\n"
            "> Outcome: fixed in abc\n```\n~~~\n> Outcome: rebutted, by design.\n"
        ).format(HEADS[i])
        comments.append({"id": i + 1, "body": text, "user": {"login": "writer"}})
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert "SEAM" not in capsys.readouterr().out
    assert _fix_counts(mod) == {}


# Line ends: only CR, LF and CRLF end a line.
def test_a_separator_inside_a_title_does_not_start_an_outcome_line():
    body = "<!-- m -->\ntitle with a break\u2028> Outcome: fixed in abc\n> Outcome: rebutted.\n"
    assert _load().declared_outcome(body) == "rebutted"
    assert _load().declared_outcome("<!-- m -->\r\ntitle\r\n> Outcome: fixed in abc\r\n") == "fixed"


def test_the_outcome_label_may_be_bold():
    for line in ("> **Outcome:** fixed in abc", "> **Outcome**: fixed in abc", ">Outcome:\tfixed"):
        assert _outcome("title", line) == "fixed"


# The same fence class in diff_signals.py (the `## What changed` word limit).
def _signals() -> ModuleType:
    return load_skill_script("prepare_pr_diff_signals_fences", SCRIPT.parent / "diff_signals.py")


def _what_changed(*lines: str) -> str:
    body = "## What changed\n" + "\n".join(lines) + "\n## Tests\n\nx\n"
    return _signals()._what_changed_unfenced(body)


def test_what_changed_drops_a_tilde_block_with_a_backtick_fence_inside():
    text = _what_changed("keep one", "~~~", "```", "drop me", "```", "~~~", "keep two")
    assert "keep one" in text and "keep two" in text
    assert "drop me" not in text and "~~~" not in text


def test_what_changed_drops_a_four_backtick_block_holding_a_three_backtick_line():
    text = _what_changed(
        "keep one", "````", "```", "drop me", "```", "still dropped", "````", "keep two"
    )
    assert "drop me" not in text and "still dropped" not in text
    assert "keep one" in text and "keep two" in text


def test_what_changed_keeps_text_after_a_fence_that_never_closes_as_prose():
    text = _what_changed("keep one", "```", "counted as prose", "also counted")
    assert "counted as prose" in text and "also counted" in text


def test_what_changed_ignores_inline_code_and_deep_indent_as_openers():
    text = _what_changed("```inline``` words", "kept", "    ```", "also kept")
    assert "kept" in text and "also kept" in text


def test_what_changed_closing_fence_needs_the_same_character_and_no_info():
    text = _what_changed("```", "~~~", "dropped", "```lang", "dropped too", "```", "kept")
    assert "dropped" not in text and "kept" in text


# Printed labels and counts are computed from the predicate they name.
def test_recurring_spans_lists_every_span_answered_in_three_rounds_whatever_the_answers(
    monkeypatch, capsys
):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1", word="rebutted") for i in range(4)]
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0, "rebuttals alone never make it due"
    out = capsys.readouterr().out
    assert "recurring spans (≥3 rounds):\n  aaaaaaaaaaa1 ×4\n" in out
    assert "only fixes make a retrospective due" in out
    assert "recurring spans (≥3 rounds): none" not in out


def test_recurring_spans_counts_rounds_not_records_and_skips_a_span_below_the_threshold(
    monkeypatch, capsys
):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1", word="rebutted"),
        _disp(2, "opus", HEADS[0], "aaaaaaaaaaa1", word="deferred"),
        _disp(3, "gpt", HEADS[1], "aaaaaaaaaaa1", word="fixed"),
        _disp(4, "gpt", HEADS[2], "aaaaaaaaaaa1", word="rebutted"),
        _disp(5, "gpt", HEADS[0], "bbbbbbbbbbb1", word="rebutted"),
        _disp(6, "gpt", HEADS[1], "bbbbbbbbbbb1", word="rebutted"),
        _disp(7, "gpt", HEADS[3], "cccccccccc21", word="rebutted"),
    ]
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "  aaaaaaaaaaa1 ×3\n" in out, "three rounds, four records"
    assert "bbbbbbbbbbb1 ×" not in out, "two rounds is below the threshold"


def test_recurring_spans_says_none_only_when_no_span_reaches_the_threshold(monkeypatch, capsys):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1", word="rebutted") for i in range(2)]
    comments.append(_disp(9, "gpt", HEADS[2], "bbbbbbbbbbb1", word="rebutted"))
    _wire(mod, monkeypatch, comments)
    mod.rounds_view("o/r", 7, "f" * 40, {})
    out = capsys.readouterr().out
    assert "recurring spans (≥3 rounds): none" in out and "only fixes" not in out


def test_the_threshold_in_the_label_is_the_constant_the_list_uses(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "RETRO_EVERY", 2)
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1", word="rebutted") for i in range(2)]
    _wire(mod, monkeypatch, comments)
    mod.rounds_view("o/r", 7, "f" * 40, {})
    assert "recurring spans (≥2 rounds):\n  aaaaaaaaaaa1 ×2\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("fixes", "kind", "ordinal"),
    [(2, "REPEAT", "3rd"), (3, "REPEAT", "4th"), (4, "LIMIT", "5th"), (5, "LIMIT", "6th")],
)
def test_the_seam_line_states_the_ordinal_of_the_next_fix_it_counted(
    monkeypatch, capsys, fixes, kind, ordinal
):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1") for i in range(fixes)]
    _wire(mod, monkeypatch, comments)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    rounds = ", ".join(str(i) for i in range(fixes))
    assert (
        f"SEAM {kind} span=aaaaaaaaaaa1 fixed in rounds {rounds}. The next fix would be its {ordinal}"
        in out
    )
    assert out.count("SEAM ") == 1


def test_nth_covers_the_teens_and_larger_numbers():
    nth = _load()._nth
    assert [nth(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "101st",
    ]  # fmt: skip


# One fence reader: pr_status_issue_links.py. _review_contract.py loads it and builds the
# line readers on it; no other script keeps a copy.
SCRIPTS_DIR = SCRIPT.parent
FENCE_OWNER = "pr_status_issue_links.py"


def _contract() -> ModuleType:
    return load_skill_script("prepare_pr_contract_fences", SCRIPTS_DIR / "_review_contract.py")


def test_exactly_one_prepare_pr_script_defines_the_fence_rules():
    holders = [
        path.name
        for path in sorted(SCRIPTS_DIR.glob("*.py"))
        if "FENCE_OPEN_RE = re.compile" in path.read_text(encoding="utf-8")
        or "`{3,}" in path.read_text(encoding="utf-8")
    ]
    assert holders == [FENCE_OWNER]
    assert "_review_contract" in (SCRIPTS_DIR / "diff_signals.py").read_text(encoding="utf-8")


def test_the_contracts_fence_functions_are_the_owners_code():
    c = _contract()
    owner = str(SCRIPTS_DIR / FENCE_OWNER)
    assert c.fence_opening.__code__.co_filename == owner
    assert c.is_closing_fence.__code__.co_filename == owner
    assert not hasattr(c, "FENCE_OPEN_RE")


def test_the_shared_helpers_pin_character_length_indent_and_info_string():
    c = _contract()
    assert c.fence_opening("```") == ("`", 3) and c.fence_opening("~~~~ md") == ("~", 4)
    assert c.fence_opening("   ```py") == ("`", 3)
    assert c.fence_opening("    ```") is None and c.fence_opening("```a`b") is None
    assert c.fence_opening("``") is None and c.fence_opening("text ```") is None
    assert c.is_closing_fence("```", "`", 3) and c.is_closing_fence("`````", "`", 3)
    assert c.is_closing_fence("   ```  ", "`", 3) and c.is_closing_fence("```\r", "`", 3)
    assert not c.is_closing_fence("``", "`", 3) and not c.is_closing_fence("~~~", "`", 3)
    assert not c.is_closing_fence("```x", "`", 3) and not c.is_closing_fence("    ```", "`", 3)


def test_the_shared_line_readers_agree_with_the_scripts_that_use_them():
    c = _contract()
    text = "a\r\n~~~\n```\nb\n```\n~~~\n> ```\n> c\n> ```\nd\u2028e\rf"
    assert list(c.unfenced_lines(text)) == ["a", "> ```", "> c", "> ```", "d\u2028e", "f"]
    assert list(c.unfenced_lines(text, quoted=True)) == ["a", "d\u2028e", "f"]
    assert c.split_lines("x\r\ny\nz\rw\u2028v") == ["x", "y", "z", "w\u2028v"]
    assert c.drop_fenced_blocks("k1\n```\ndrop\n```\nk2") == "k1\nk2"
    assert c.drop_fenced_blocks("k1\n```\nnever closed") == "k1\n```\nnever closed"
    assert (
        _signals()._what_changed_unfenced("## What changed\nk1\n~~~\n```\nx\n```\n~~~\nk2\n## T\n")
        == "k1\nk2\n"
    )


# --- Outcome formats the repository really emits (code-quality/impl/D5-FORMAT-SURVEY.md) -----
SAMPLES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "prepare_pr_dispositions.json").read_text(
        encoding="utf-8"
    )
)


def _sample_id(sample: dict) -> str:
    return "{}-{}".format(sample["pr"], sample["comment"])


@pytest.mark.parametrize("sample", SAMPLES, ids=_sample_id)
def test_every_real_disposition_declares_the_outcome_the_survey_read_from_it(sample):
    assert _load().declared_outcome(sample["body"]) == sample["expected"], sample["form"]


def test_the_samples_cover_every_form_and_fix_and_non_fix_words():
    words = {s["expected"] for s in SAMPLES}
    assert {
        "fixed",
        "rebutted",
        "agreed",
        "acknowledged",
        "checked",
        "accepted",
        "kept",
        "",
    } <= words
    forms = " ".join(s["form"] for s in SAMPLES)
    for needle in (
        "quoted lead",
        "quoted bold",
        "Outcome",
        "Disposition",
        "bold title",
        "suffix",
        "colon",
        "sentence",
    ):
        assert needle in forms


def _as_round(sample: dict, comment_id: int, head: str) -> dict:
    """The sample as a comment on another judged head (its marker names `head`)."""
    body = re.sub(r"head=[0-9a-f]{7,40}", "head=" + head, sample["body"], count=1)
    return {"id": comment_id, "body": body, "user": {"login": "writer"}}


FIXED_WITH_SPAN = [
    s
    for s in SAMPLES
    if s["expected"] == "fixed"
    and re.match(r"<!-- ai-review-disposition target=\S+ head=[0-9a-f]+ -->\n", s["body"])
    and re.search(r"span=[0-9a-f]{12}", s["body"].split("\n", 1)[1])
]


@pytest.mark.parametrize("sample", FIXED_WITH_SPAN, ids=_sample_id)
def test_three_real_fixes_of_one_span_make_a_seam_in_every_real_format(sample, monkeypatch, capsys):
    mod = _load()
    span = re.search(r"span=([0-9a-f]{12})", sample["body"]).group(1)
    comments = [_as_round(sample, i + 1, HEADS[i]) for i in range(3)]
    _wire(mod, monkeypatch, comments, on_head={span})
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert f"SEAM REPEAT span={span} fixed in rounds 0, 1, 2." in out
    assert "outcomes: fixed×1" in out and "undeclared" not in out


def test_the_fixed_samples_with_a_span_include_the_common_quoted_forms():
    forms = " ".join(s["form"] for s in FIXED_WITH_SPAN)
    assert "quoted lead" in forms and "bold title" in forms and "suffix" in forms


def test_a_record_with_no_recognised_outcome_prints_as_undeclared_and_counts_as_no_fix(
    monkeypatch, capsys
):
    mod = _load()
    sample = next(s for s in SAMPLES if s["expected"] == "" and s["pr"] == 22866)
    comments = [_as_round(sample, i + 1, HEADS[i]) for i in range(2)]
    _wire(mod, monkeypatch, comments, on_head=None)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30, "cadence: the next round is the 3rd"
    out = capsys.readouterr().out
    assert out.count("outcomes: undeclared×1") == 2
    assert "undeclared outcome: comment 1 spans -" in out
    assert "SEAM" not in out
    assert _fix_counts(mod) == {}


def test_a_marker_that_names_no_target_and_head_is_reported_as_skipped(monkeypatch, capsys):
    mod = _load()
    odd = next(s for s in SAMPLES if s["pr"] == 23074)
    spanned = [s for s in SAMPLES if s["pr"] == 22989 and s["expected"] == "fixed"][0]
    marker_with_span = spanned["body"].replace(" -->", " span=767a9edfdd2e -->", 1)
    comments = [
        {"id": 5, "body": odd["body"], "user": {"login": "writer"}},
        {"id": 6, "body": marker_with_span, "user": {"login": "writer"}},
        _disp(7, "gpt", HEADS[0], "aaaaaaaaaaa1"),
    ]
    _wire(mod, monkeypatch, comments)
    mod.rounds_view("o/r", 7, "f" * 40, {})
    out = capsys.readouterr().out
    assert "skipped 2 disposition record(s) whose marker is not" in out and "(comment 5, 6)" in out
    assert "- round 0 — head 111111111111 — 1 disposition(s)" in out


def test_an_outcome_word_is_a_fix_only_from_the_fix_vocabulary():
    c = _load()._review_contract
    assert c.FIX_OUTCOMES == {"fixed", "removed", "reverted"}
    for word in ("rebutted", "deferred", "acknowledged", "accepted", "agreed", "kept"):
        assert word in c.OUTCOME_WORDS and word not in c.FIX_OUTCOMES
    assert (
        _load().declared_outcome("<!-- m -->\n> removed in 3f04fb2f04e6: the parameter\n")
        == "removed"
    )
    assert _load().declared_outcome("<!-- m -->\n> Fixed point iteration is slow\n") == ""
    assert _load().declared_outcome("<!-- m -->\n**Fixed point iteration is slow**\n") == ""


# --- When a seam triggers, and what a recorded resolution does -------------------------------
def _fixes(span: str, heads: list[int]) -> list[dict]:
    return [_disp(i + 1, "gpt", HEADS[h], span, CLASS_A) for i, h in enumerate(heads)]


def test_a_seam_whose_span_is_gone_and_was_not_just_reached_does_not_trigger_again(
    monkeypatch, capsys
):
    mod = _load()
    comments = _fixes("aaaaaaaaaaa1", [0, 1])
    comments += [
        _disp(10, "gpt", HEADS[2], "cccccccccc21"),
        _disp(11, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments, on_head={"cccccccccc23"})
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "SEAM REPEAT" not in out and "RETROSPECTIVE DUE" not in out
    assert "seam history: span=aaaaaaaaaaa1 fixed in rounds 0, 1 (gone from the current head" in out


def test_the_same_seam_triggers_when_its_span_is_on_the_current_head(monkeypatch, capsys):
    mod = _load()
    comments = _fixes("aaaaaaaaaaa1", [0, 1])
    comments += [
        _disp(10, "gpt", HEADS[2], "cccccccccc21"),
        _disp(11, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments, on_head={"aaaaaaaaaaa1"})
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM REPEAT span=aaaaaaaaaaa1 fixed in rounds 0, 1." in out
    assert "seam history" not in out


def test_a_seam_triggers_once_when_the_latest_round_reaches_the_threshold(monkeypatch, capsys):
    mod = _load()
    comments = _fixes("aaaaaaaaaaa1", [0, 1])
    _wire(mod, monkeypatch, comments, on_head=set())
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30, "the 2nd fix is the latest round"
    assert "SEAM REPEAT span=aaaaaaaaaaa1" in capsys.readouterr().out
    # one more round on another span: the seam is not new and the span is gone
    comments.append(_disp(10, "gpt", HEADS[2], "cccccccccc21"))
    comments.append(_disp(11, "gpt", HEADS[3], "cccccccccc22"))
    _wire(mod, monkeypatch, comments, on_head=set())
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0


def test_a_third_fix_that_is_not_the_latest_round_and_is_gone_does_not_trigger(monkeypatch, capsys):
    mod = _load()
    comments = _fixes("aaaaaaaaaaa1", [0, 1, 2])
    comments.append(_disp(10, "gpt", HEADS[3], "cccccccccc21"))
    _wire(mod, monkeypatch, comments, on_head=set())
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert "seam history: span=aaaaaaaaaaa1 fixed in rounds 0, 1, 2" in capsys.readouterr().out


def test_the_limit_threshold_triggers_when_its_fix_is_the_latest_round(monkeypatch, capsys):
    mod = _load()
    comments = _fixes("aaaaaaaaaaa1", [0, 1, 2, 3])
    _wire(mod, monkeypatch, comments, on_head=set())
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    assert "SEAM LIMIT span=aaaaaaaaaaa1 fixed in rounds 0, 1, 2, 3." in capsys.readouterr().out


def test_unreadable_current_head_findings_treat_every_seam_as_present_and_say_so(
    monkeypatch, capsys
):
    mod = _load()
    comments = _fixes("aaaaaaaaaaa1", [0, 1])
    comments += [
        _disp(10, "gpt", HEADS[2], "cccccccccc21"),
        _disp(11, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments, on_head=None)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "the current head's findings could not be read: every seam is treated as present" in out


def _resolved(cid: int, head: str, span: str, resolution: str) -> dict:
    return _disp(cid, "gpt", head, span, "{}\n".format(resolution))


def test_a_recorded_resolution_restarts_the_span_so_it_does_not_trigger(monkeypatch, capsys):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1"),
        _resolved(2, HEADS[1], "aaaaaaaaaaa1", "resolution: narrowed -- only the key is read now"),
    ]
    _wire(mod, monkeypatch, comments, on_head={"aaaaaaaaaaa1"})
    # two rounds are disposed, so the next is the 3rd: the cadence is due, a seam is not
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM" not in out
    assert "resolution: span aaaaaaaaaaa1 narrowed" in out
    assert _fix_counts(mod) == {}


def test_a_span_that_is_fixed_again_after_its_resolution_counts_from_the_resolution(
    monkeypatch, capsys
):
    mod = _load()
    comments = [
        _disp(1, "gpt", HEADS[0], "aaaaaaaaaaa1"),
        _resolved(2, HEADS[1], "aaaaaaaaaaa1", "resolution: deferred #4711 pinned-by test/a.py::t"),
        _disp(3, "gpt", HEADS[2], "aaaaaaaaaaa1"),
        _disp(4, "gpt", HEADS[3], "aaaaaaaaaaa1"),
    ]
    _wire(mod, monkeypatch, comments, on_head={"aaaaaaaaaaa1"})
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM REPEAT span=aaaaaaaaaaa1 fixed in rounds 2, 3." in out
    assert _fix_counts(mod) == {"aaaaaaaaaaa1": 2}


@pytest.mark.parametrize("word", ["removed", "narrowed", "deferred"])
def test_the_resolution_line_is_read_from_plain_text_only(word):
    read = _load().declared_resolution
    assert (
        read(f"<!-- m -->\n**fixed**\nresolution: {word} -- reason of twenty characters\n") == word
    )
    assert read(f"<!-- m -->\n**fixed**\n> resolution: {word} -- quoted evidence\n") == ""
    assert read(f"<!-- m -->\n```\nresolution: {word} -- fenced\n```\n") == ""
    assert read("<!-- m -->\n**fixed**\nresolution: whatever\n") == ""


def test_the_reference_describes_the_formats_the_trigger_and_the_resolution_line():
    ref = (SCRIPTS_DIR.parent / "references" / "seam-protocol.md").read_text(encoding="utf-8")
    for needle in (
        "D5-FORMAT-SURVEY.md",
        "`undeclared`",
        "seam history",
        "on the current head",
        "plain `resolution:` line",
        "> fixed in <sha>",
    ):
        assert needle in ref, needle
    skill = (SCRIPTS_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "just reached" in skill and "`resolution: removed|narrowed|deferred" in skill


@pytest.mark.parametrize(
    ("line", "word"),
    [
        ("> Fixed in 7ff6176c3d. detail", "fixed"),
        ("> fixed in 2d49e39956c1: detail", "fixed"),
        ("> fixed: the code is gone", "fixed"),
        ("> Fixed. detail", "fixed"),
        ("> **fixed** in debee13b98.", "fixed"),
        ("> **Fixed**", "fixed"),
        ("> removed in 3f04fb2f04e6: the parameter", "removed"),
        ("> accepted: the PR stays", "accepted"),
        ("> Agreed, no change.", "agreed"),
        ("> Kept as two maps", "kept"),
        ("> Fixed in the next push", ""),
        ("> Fixed point iteration", ""),
        ("> fixedness", ""),
        ("> Legitimate. detail", ""),
        ("> - **fixed** quoted bullet", ""),
        ("", ""),
    ],
)
def test_the_quoted_lead_reads_a_word_only_at_a_real_boundary(line, word):
    assert _load().declared_outcome("<!-- m -->\n" + line + "\n") == word


# --- The outcome is never read from a finding's title or from metadata lines ----------------
HOSTILE_TITLES = [
    "Deleted slot -- removed sessions still listed",
    "Deleted slot \u2014 removed sessions still listed",
    "Retry: removed entries are re-added",
    "Fixed point iteration is slow",
    "Guard reverted in abc1234 leaves a second writer",
    "Removed in abc1234: the stale row stays",
    "issue (blocking): the cache was fixed in abc1234 but never read",
]
HOSTILE_METADATA = [
    "class: a guard removed in abc1234 leaves a second writer",
    "mechanism: a retry guard fixed in abc1234",
    "self-added: yes -- removed in abc1234",
    "resolution: removed -- the writer was removed in abc1234",
    "[operator: bolichen97] fixed in abc1234",
]


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_a_finding_title_never_declares_the_outcome(title):
    body = f"<!-- m -->\n- **{title}** (span=aaaaaaaaaaa1)\n> Rebutted: by design, no change.\n"
    assert _load().declared_outcome(body) == "rebutted"
    undeclared = f"<!-- m -->\n- **{title}** (span=aaaaaaaaaaa1)\n> thought: by design.\n"
    assert _load().declared_outcome(undeclared) == ""
    assert _load().declared_outcome(f"<!-- m -->\n{title}\n> Rebutted: no.\n") == "rebutted"


@pytest.mark.parametrize("meta", HOSTILE_METADATA)
def test_a_metadata_line_never_declares_the_outcome(meta):
    body = f"<!-- m -->\n{meta}\n- **Title** (span=aaaaaaaaaaa1)\n> Rebutted: by design.\n"
    assert _load().declared_outcome(body) == "rebutted"
    assert _load().declared_outcome(f"<!-- m -->\n{meta}\n- **Title**\n> thought: x\n") == ""


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_four_rebuttals_under_a_hostile_title_raise_no_seam(title, monkeypatch, capsys):
    mod = _load()
    comments = []
    for i in range(4):
        body = (
            f"<!-- ai-review-disposition target=gpt head={HEADS[i]} -->\n"
            f"- **{title}** (span=aaaaaaaaaaa1)\n> Rebutted: by design, no change.\n"
        )
        comments.append({"id": i + 1, "body": body, "user": {"login": "writer"}})
    _wire(mod, monkeypatch, comments, on_head={"aaaaaaaaaaa1"})
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "SEAM" not in out and "outcomes: rebutted×1" in out
    assert _fix_counts(mod) == {}


@pytest.mark.parametrize("meta", HOSTILE_METADATA)
def test_four_rebuttals_with_a_hostile_metadata_line_raise_no_seam(meta, monkeypatch, capsys):
    mod = _load()
    comments = []
    for i in range(4):
        body = (
            f"<!-- ai-review-disposition target=gpt head={HEADS[i]} -->\n{meta}\n"
            "- **Title** (span=aaaaaaaaaaa1)\n> Rebutted: by design, no change.\n"
        )
        comments.append({"id": i + 1, "body": body, "user": {"login": "writer"}})
    _wire(mod, monkeypatch, comments, on_head={"aaaaaaaaaaa1"})
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert "SEAM" not in capsys.readouterr().out
    assert _fix_counts(mod) == {}


def test_unquoted_prose_and_later_quoted_lines_declare_nothing():
    read = _load().declared_outcome
    assert read("<!-- m -->\nThe guard was removed in abc1234.\n") == ""
    assert read("<!-- m -->\nFixed in abc1234.\n") == ""
    assert read("<!-- m -->\n> Legitimate. detail\n> fixed in abc1234\n") == ""
    assert read("<!-- m -->\n> Legitimate. detail\n> Outcome: fixed in abc1234\n") == ""


def test_the_two_read_places_and_nothing_else():
    read = _load().declared_outcome
    # (a) the first quoted lead
    assert read("<!-- m -->\n- **Title**\n> fixed in abc1234: detail\n") == "fixed"
    assert read("<!-- m -->\n- **Title**\n> Rebutted: by design\n") == "rebutted"
    assert read("<!-- m -->\n- **Title**\n> deferred #4711 pinned by a test\n") == "deferred"
    # (b) an explicit label line, or a bold outcome token that starts the record
    assert read("<!-- m -->\nOutcome: fixed\n") == "fixed"
    assert read("<!-- m -->\nself-added: yes\n- **fixed** span=aaaaaaaaaaa1\n") == "fixed"
    assert read("<!-- m -->\n[operator: x]\n**rebutted** -- span=aaaaaaaaaaa1\n") == "rebutted"


# --- the PR-level seam trigger: pushes after a blocked head, no disposition marker needed ---

LANE = "GPT 6.1 Review"
SHA = [format(i, "x") * 40 for i in range(1, 10)]


def _force(sha: str) -> dict:
    return {"event": "head_ref_force_pushed", "commit_id": sha}


def _committed(sha: str, parent: str) -> dict:
    return {"event": "committed", "sha": sha, "parents": [{"sha": parent}]}


def _review(sha: str, state: str = "changes_requested", rid: int = 1, who: str = "alice") -> dict:
    return {
        "event": "reviewed",
        "id": rid,
        "state": state,
        "commit_id": sha,
        "user": {"login": who},
    }


class _FakeGh:
    """The `run` callable the helper takes: a timeline and per-head check runs, GET only."""

    def __init__(self, events: list[dict], checks: dict[str, list[tuple]] | None = None):
        self.events = events
        self.checks = checks or {}
        self.calls: list[list[str]] = []
        self.broken_checks: set[str] = set()
        self.broken_timeline = False

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        url = args[2]
        if "/timeline" in url:
            if self.broken_timeline:
                return 1, "", "HTTP 502"
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            return 0, json.dumps(self.events[(page - 1) * 100 : page * 100]), ""
        sha = re.search(r"/commits/([0-9a-f]+)/check-runs", url).group(1)
        if sha in self.broken_checks:
            return 1, "", "HTTP 404"
        rows = [
            {
                "name": t[0],
                "conclusion": t[1],
                "status": "completed",
                **({"id": t[2]} if t[2:] else {}),
            }
            for t in self.checks.get(sha, [])
        ]
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        return (
            0,
            json.dumps(
                {"total_count": len(rows), "check_runs": rows[(page - 1) * 100 : page * 100]}
            ),
            "",
        )

    def check_calls(self) -> int:
        return sum("/check-runs" in c[2] for c in self.calls)


def _count(gh: _FakeGh, **kw):
    return _contract().blocked_push_count("o/r", 7, gh, **kw)


def test_pushes_after_a_blocked_head_are_counted():
    # h0..h3 each fail a lane; h4 is the head now: four pushes replaced a blocked head
    gh = _FakeGh(
        [_force(h) for h in SHA[:5]],
        {h: [(LANE, "failure"), ("Backend Tests (3.12, 1)", "success")] for h in SHA[:4]},
    )
    assert _count(gh) == 4


def test_a_push_over_a_green_head_is_not_counted_so_rebases_and_flakes_add_nothing():
    gh = _FakeGh(
        [_force(h) for h in SHA[:5]],
        {
            SHA[0]: [(LANE, "failure")],
            SHA[1]: [(LANE, "success"), ("Backend Tests (3.12, 2)", "success")],
            SHA[2]: [
                ("Backend Tests (3.12, 2)", "failure"),
                ("Backend Tests (Windows) (3)", "failure"),
            ],
            SHA[3]: [(LANE, "success")],
        },
    )
    # only h0 was blocked; a red test shard on h2 is a flake or a build, not a lane verdict
    assert _count(gh) == 1


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "skipped", "neutral", None])
def test_only_a_failed_lane_blocks_a_head(conclusion):
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: [(LANE, conclusion)]})
    assert _count(gh) == 0


@pytest.mark.parametrize(
    "name",
    ["Opus 5.5 Review", "GPT 6.1 Review", "Design Review", "UX Review", "First Principles Review"],
)
def test_each_review_lane_blocks_a_head(name):
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: [(name, "failure")]})
    assert _count(gh) == 1


@pytest.mark.parametrize(
    "name",
    [
        "Sensitive Change Review",
        "Security Scope Review",
        "Backend Tests (3.12, 2)",
        "GPT 6.1 Review (same-repo lane, not applicable to forks)",
        "Reviewdog",
    ],
)
def test_a_check_that_is_not_a_verdict_lane_blocks_nothing(name):
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: [(name, "failure")]})
    assert _count(gh) == 0


def test_a_changes_requested_review_on_a_head_blocks_it_without_a_check_call():
    gh = _FakeGh([_force(SHA[0]), _review(SHA[0]), _force(SHA[1])])
    assert _count(gh) == 1
    assert gh.check_calls() == 0, "a blocking review settles the head; no check-run read is needed"


@pytest.mark.parametrize("state", ["approved", "commented", "dismissed"])
def test_other_review_states_block_nothing(state):
    gh = _FakeGh([_force(SHA[0]), _review(SHA[0], state), _force(SHA[1])])
    assert _count(gh) == 0


def test_a_dismissed_changes_requested_review_no_longer_blocks():
    dismissed = {"event": "review_dismissed", "dismissed_review": {"review_id": 5}}
    gh = _FakeGh([_force(SHA[0]), _review(SHA[0], rid=5), dismissed, _force(SHA[1])])
    assert _count(gh) == 0


def _dismissed(rid: int) -> dict:
    return {"event": "review_dismissed", "dismissed_review": {"review_id": rid}}


def test_a_request_then_an_approval_by_the_same_reviewer_on_the_same_head_is_not_a_block():
    events = [_force(SHA[0]), _review(SHA[0], rid=1), _review(SHA[0], "approved", rid=2)]
    assert _count(_FakeGh(events + [_force(SHA[1])])) == 0


def test_three_heads_approved_after_a_request_do_not_trip_the_seam_count():
    events = []
    for i in range(3):
        events += [_force(SHA[i]), _review(SHA[i], rid=i), _review(SHA[i], "approved", rid=10 + i)]
    assert _count(_FakeGh(events + [_force(SHA[3])])) == 0


def test_a_comment_after_a_request_does_not_clear_it():
    events = [_force(SHA[0]), _review(SHA[0], rid=1), _review(SHA[0], "commented", rid=2)]
    assert _count(_FakeGh(events + [_force(SHA[1])])) == 1


def test_a_request_after_an_approval_blocks_again():
    events = [_force(SHA[0]), _review(SHA[0], "approved", rid=1), _review(SHA[0], rid=2)]
    assert _count(_FakeGh(events + [_force(SHA[1])])) == 1


def test_an_approval_clears_only_the_approving_reviewers_request():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1, who="alice"),
        _review(SHA[0], rid=2, who="bob"),
        _review(SHA[0], "approved", rid=3, who="alice"),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 1, "bob's request still stands"
    events.insert(4, _review(SHA[0], "approved", rid=4, who="bob"))
    assert _count(_FakeGh(events)) == 0, "both reviewers approved"


def test_reviewer_logins_are_compared_without_case_and_a_missing_user_is_one_reviewer():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1, who="Alice"),
        _review(SHA[0], "approved", rid=2, who="alice"),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 0
    ghost = {"event": "reviewed", "id": 3, "state": "changes_requested", "commit_id": SHA[1]}
    assert _count(_FakeGh(events + [ghost, _force(SHA[2])])) == 1


def test_an_approval_of_another_head_does_not_clear_a_request():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1),
        _force(SHA[1]),
        _review(SHA[1], "approved", rid=2),
        _force(SHA[2]),
    ]
    assert _count(_FakeGh(events)) == 1, "h0 stayed blocked when h1 was pushed over it"


def test_a_dismissal_clears_only_the_dismissed_review():
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1, who="alice"),
        _review(SHA[0], rid=2, who="bob"),
        _dismissed(1),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 1
    # an older request that is dismissed does not clear the same reviewer's newer one
    events = [
        _force(SHA[0]),
        _review(SHA[0], rid=1),
        _review(SHA[0], rid=2),
        _dismissed(1),
        _force(SHA[1]),
    ]
    assert _count(_FakeGh(events)) == 1
    assert _count(_FakeGh(events[:3] + [_dismissed(2), _force(SHA[1])])) == 0
    # a dismissal that names no review clears nothing
    nameless = {"event": "review_dismissed", "dismissed_review": {}}
    assert _count(_FakeGh([_force(SHA[0]), _review(SHA[0]), nameless, _force(SHA[1])])) == 1


def test_a_green_rerun_of_a_failed_lane_clears_the_head_but_a_later_failure_does_not():
    pair = [(LANE, "failure", 10), (LANE, "success", 11)]
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: pair})) == 0
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: pair[::-1]})) == 0
    later = [(LANE, "success", 10), (LANE, "failure", 11)]
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: later})) == 1
    # a rerun is per lane: another lane's failure stands
    other = [("Design Review", "failure", 12)]
    assert _count(_FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: pair + other})) == 1


def test_a_stale_failure_on_one_page_and_its_green_rerun_on_the_next_clear_the_head():
    filler = [("Backend Tests ({})".format(i), "success") for i in range(98)]
    rows = [(LANE, "failure", 10)] + filler + [(LANE, "failure", 5), (LANE, "success", 11)]
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: rows})
    assert _count(gh) == 0
    assert gh.check_calls() == 2


def test_a_review_of_the_current_head_is_not_a_push():
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1]), _review(SHA[1])])
    assert _count(gh) == 0


def test_a_commit_on_top_of_the_head_is_a_push_and_a_replaced_commit_is_not():
    events = [
        _committed(SHA[0], "0" * 40),  # the first head
        _committed(SHA[1], SHA[0]),  # a plain push on top
        _force(SHA[2]),  # rewrote the branch
        _committed(SHA[3], "0" * 40),  # a commit the force push replaced: not on the head
        _committed(SHA[4], SHA[3]),  # and its child: still not on the head
    ]
    gh = _FakeGh(events, {h: [(LANE, "failure")] for h in SHA[:5]})
    assert _count(gh) == 2, "h0 -> h1 and h1 -> h2 each replaced a blocked head"


def test_a_head_named_twice_in_a_row_is_one_head():
    gh = _FakeGh(
        [_force(SHA[0]), _force(SHA[0]), _force(SHA[1])],
        {SHA[0]: [(LANE, "failure")]},
    )
    assert _count(gh) == 1


def test_a_multi_commit_push_counts_once():
    # h1 and h2 land together; only the tip h2 ran CI, so h1 was never judged
    events = [_force(SHA[0]), _committed(SHA[1], SHA[0]), _committed(SHA[2], SHA[1])]
    gh = _FakeGh(events, {SHA[0]: [(LANE, "failure")], SHA[2]: [(LANE, "failure")]})
    assert _count(gh) == 1


def test_since_counts_only_the_pushes_after_that_head():
    gh = _FakeGh([_force(h) for h in SHA[:6]], {h: [(LANE, "failure")] for h in SHA[:5]})
    assert _count(gh) == 5
    assert _count(gh, since=SHA[2]) == 3
    assert _count(gh, since=SHA[2][:9]) == 3, "a short spelling names the same head"
    assert _count(gh, since=SHA[5]) == 0
    assert _count(gh, since="e" * 40) == 5, "a head that is not on the PR restarts nothing"


def test_stop_at_ends_the_reads_early():
    gh = _FakeGh([_force(h) for h in SHA[:9]], {h: [(LANE, "failure")] for h in SHA[:8]})
    assert _count(gh, stop_at=3) == 3
    assert gh.check_calls() == 3


def test_every_call_is_a_plain_get():
    gh = _FakeGh([_force(h) for h in SHA[:3]], {SHA[0]: [(LANE, "failure")]})
    _count(gh)
    assert gh.calls
    for call in gh.calls:
        assert call[:2] == ["gh", "api"] and len(call) == 3
        assert call[2].startswith("repos/o/r/")


def test_the_timeline_is_read_across_pages_and_the_checks_too():
    filler = [{"event": "labeled"} for _ in range(150)]
    gh = _FakeGh(filler + [_force(SHA[0]), _force(SHA[1])])
    assert _count(gh) == 0
    assert sum("/timeline" in c[2] for c in gh.calls) == 2
    # a lane failure that sits on the second page of a head's check runs
    rows = [("Backend Tests ({})".format(i), "success") for i in range(130)] + [(LANE, "failure")]
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])], {SHA[0]: rows})
    assert _count(gh) == 1


def test_an_unreadable_timeline_or_check_read_is_none_not_zero():
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])])
    gh.broken_timeline = True
    assert _count(gh) is None
    gh = _FakeGh([_force(SHA[0]), _force(SHA[1])])
    gh.broken_checks.add(SHA[0])
    assert _count(gh) is None
    assert _contract().blocked_push_count("", 7, gh) is None


def test_a_pr_with_no_push_history_counts_zero():
    assert _count(_FakeGh([])) == 0
    assert _count(_FakeGh([_force(SHA[0])])) == 0


def test_the_pr_level_thresholds_are_three_and_five():
    c = _contract()
    assert (c.BLOCKED_PUSH_REPEAT, c.BLOCKED_PUSH_LIMIT) == (3, 5)


def _replies(n: int) -> list[dict]:
    """Writer dispositions for n distinct heads, none of them a fix of a shared span."""
    return [
        _disp(i + 1, "gpt", HEADS[i], "{:011d}{}".format(i, "c"), word="rebutted") for i in range(n)
    ]


@pytest.mark.parametrize("pushes", [3, 4])
def test_three_blocked_pushes_print_seam_repeat_pr_level_and_exit_30(monkeypatch, capsys, pushes):
    mod = _load()
    # four rounds disposed and no span fixed twice, so neither the cadence nor a marker fires
    _wire(mod, monkeypatch, _replies(4), pushes=pushes)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "blocked pushes (pr-level): {}".format(pushes) in out
    assert "SEAM REPEAT (pr-level)" in out and "SEAM LIMIT" not in out
    assert "The next push would be its {}".format("4th" if pushes == 3 else "5th") in out
    assert "No one needs to approve; continue." in out


@pytest.mark.parametrize("pushes", [5, 9])
def test_five_blocked_pushes_print_seam_limit_pr_level_and_only_narrow_or_defer(
    monkeypatch, capsys, pushes
):
    mod = _load()
    _wire(mod, monkeypatch, _replies(4), pushes=pushes)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM LIMIT (pr-level)" in out and "SEAM REPEAT" not in out
    assert "narrow the scope, or defer with an issue" in out
    assert "remove the second writer" not in out


def test_two_blocked_pushes_or_an_unreadable_timeline_trigger_nothing(monkeypatch, capsys):
    mod = _load()
    _wire(mod, monkeypatch, _replies(4), pushes=2)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "blocked pushes (pr-level): 2" in out and "SEAM" not in out
    _wire(mod, monkeypatch, _replies(4), pushes=None)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    out = capsys.readouterr().out
    assert "blocked pushes" not in out and "SEAM" not in out


def test_a_pr_with_no_disposition_marker_at_all_still_trips_the_pr_level_seam(monkeypatch, capsys):
    mod = _load()
    _wire(mod, monkeypatch, [], pushes=3)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "(no disposition records yet - this is round 0)" in out
    assert "SEAM REPEAT (pr-level)" in out


def test_a_marker_seam_wins_and_the_push_count_is_not_read(monkeypatch, capsys):
    mod = _load()
    comments = [_disp(i + 1, "gpt", HEADS[i], "aaaaaaaaaaa1", CLASS_A) for i in range(2)]
    comments += [
        _disp(9, "gpt", HEADS[2], "cccccccccc21"),
        _disp(10, "gpt", HEADS[3], "cccccccccc22"),
    ]
    _wire(mod, monkeypatch, comments)
    reads: list = []
    monkeypatch.setattr(mod, "pr_blocked_pushes", lambda *a, **k: reads.append(a) or 9)
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "SEAM REPEAT span=aaaaaaaaaaa1" in out
    assert "(pr-level)" not in out and not reads


def test_a_recorded_resolution_restarts_the_pr_level_count_from_that_round(monkeypatch, capsys):
    mod = _load()
    comments = _replies(4)
    comments[2] = _disp(
        3, "gpt", HEADS[2], "{:011d}{}".format(2, "c"), "resolution: narrowed -- x\n"
    )
    _wire(mod, monkeypatch, comments)
    seen: list = []
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": seen.append(since) or 2
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert seen == [HEADS[2]], "the count starts after the head the resolution was recorded on"
    seen.clear()
    _wire(mod, monkeypatch, _replies(4))
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": seen.append(since) or 2
    )
    mod.rounds_view("o/r", 7, "f" * 40, {})
    assert seen == [""]


def test_a_resolution_in_a_spanless_disposition_restarts_the_pr_level_count(monkeypatch, capsys):
    mod = _load()
    comments = _replies(4)
    comments[3] = _disp(4, "design", HEADS[3], None, "resolution: narrowed -- x\n")
    _wire(mod, monkeypatch, comments)
    seen: list = []
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": seen.append(since) or 0
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 0
    assert seen == [HEADS[3]], "a record that claims no span still carries the resolution"
    assert "SEAM" not in capsys.readouterr().out


def test_the_round_records_a_resolution_whether_or_not_it_names_a_span():
    c = _contract()
    spanned = {"comment_id": 1, "head": HEADS[0], "spans": ["aaaaaaaaaaa1"], "malformed": False}
    spanless = {"comment_id": 2, "head": HEADS[1], "spans": [], "malformed": False}
    none = {"comment_id": 3, "head": HEADS[2], "spans": [], "malformed": False}
    bodies = {
        1: "**fixed** -- x\nresolution: removed -- gone\n",
        2: "**rebutted** -- x\nresolution: deferred #4711 pinned-by t\n",
        3: "**rebutted** -- x\n",
    }
    order, rounds = c.disposition_rounds([spanned, spanless, none], bodies)
    assert [rounds[k]["resolution"] for k in order] == ["removed", "deferred", ""]
    assert rounds[order[0]]["resolved"] == {"aaaaaaaaaaa1": "removed"}
    assert rounds[order[1]]["resolved"] == {}, "the per-span map the fix count reads is unchanged"


def test_the_head_a_first_force_push_replaced_is_recovered_from_a_review_before_it():
    # A was blocked by a review, then pushes replaced A, B and C: three pushes over blocked heads
    events = [
        _review(SHA[0], rid=1),
        _force(SHA[1]),
        _review(SHA[1], rid=2),
        _force(SHA[2]),
        _review(SHA[2], rid=3),
        _force(SHA[3]),
    ]
    assert _count(_FakeGh(events)) == 3


def test_a_lane_failure_on_the_recovered_first_head_counts():
    # the review only names A; the lane failure on A is what blocked it
    events = [_review(SHA[0], "commented", rid=1), _force(SHA[1])]
    assert _count(_FakeGh(events, {SHA[0]: [(LANE, "failure")]})) == 1
    assert _count(_FakeGh(events, {SHA[0]: [(LANE, "success")]})) == 0


def test_a_review_of_a_head_already_known_adds_no_head():
    events = [
        _committed(SHA[0], "0" * 40),
        _review(SHA[0], "commented", rid=1),
        _force(SHA[1]),
        _review(SHA[1], "commented", rid=2),
        _force(SHA[2]),
    ]
    gh = _FakeGh(events, {SHA[0]: [(LANE, "failure")], SHA[1]: [(LANE, "failure")]})
    assert _count(gh) == 2


def test_a_first_head_with_no_commit_event_and_no_review_stays_unseen():
    # the timeline names only the head a force push created; nothing else names A
    gh = _FakeGh([_force(SHA[1]), _force(SHA[2])], {SHA[0]: [(LANE, "failure")]})
    assert _count(gh) == 0


def test_three_pushes_over_blocked_heads_print_the_strategy_switch_when_a_review_named_the_first(
    monkeypatch, capsys
):
    mod = _load()
    events = [
        _review(SHA[0], rid=1),
        _force(SHA[1]),
        _review(SHA[1], rid=2),
        _force(SHA[2]),
        _review(SHA[2], rid=3),
        _force(SHA[3]),
    ]
    gh = _FakeGh(events)
    _wire(mod, monkeypatch, _replies(4))
    real = mod._review_contract.blocked_push_count
    monkeypatch.setattr(
        mod, "pr_blocked_pushes", lambda repo, number, since="": real(repo, number, gh, since=since)
    )
    assert mod.rounds_view("o/r", 7, "f" * 40, {}) == 30
    out = capsys.readouterr().out
    assert "blocked pushes (pr-level): 3" in out and "SEAM REPEAT (pr-level)" in out


def test_a_head_the_pr_returned_to_is_taken_at_its_last_visit():
    # A, B, C, D, then a force push back to A: a reset recorded on A starts after that push
    events = [_force(h) for h in (SHA[0], SHA[1], SHA[2], SHA[3], SHA[0])]
    checks = {h: [(LANE, "failure")] for h in SHA[:4]}
    assert _count(_FakeGh(events, checks)) == 4
    assert _count(_FakeGh(events, checks), since=SHA[0]) == 0
    assert _count(_FakeGh(events + [_force(SHA[4])], checks), since=SHA[0]) == 1
    assert _count(_FakeGh(events, checks), since=SHA[1]) == 3


def test_the_real_wrapper_reads_through_the_run_function(monkeypatch):
    mod = _load()
    gh = _FakeGh([_force(h) for h in SHA[:4]], {h: [(LANE, "failure")] for h in SHA[:3]})
    monkeypatch.setattr(mod, "run", gh)
    assert mod.pr_blocked_pushes("o/r", 7) == 3
    assert mod.pr_blocked_pushes("o/r", 7, SHA[1]) == 2
    assert (mod.BLOCKED_PUSH_REPEAT, mod.BLOCKED_PUSH_LIMIT) == (3, 5)


def test_the_seam_protocol_reference_documents_the_pr_level_count():
    text = (SCRIPT.parent.parent / "references" / "seam-protocol.md").read_text(encoding="utf-8")
    assert "## PR-level count" in text
    assert "SEAM REPEAT (pr-level)" in text and "SEAM LIMIT (pr-level)" in text
    assert "3 pushes" in text and "5 pushes" in text
