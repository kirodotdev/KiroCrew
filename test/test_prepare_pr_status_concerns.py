"""``pr_status.py``: a whole-design lane says PASS or BLOCK, and the design items a
disposition rules on.
"""

from __future__ import annotations

import json

from prepare_pr_status_helpers import (
    _GATE_HEAD,
    _GREEN_CHECKS,
    _HEAD,
    _bot_comment,
    _gate_argv,
    _gate_bot_comment,
    _install_fake_gh,
    _load_script,
    _pr_payload,
)

# ---------------------------------------------------------------------------
# A whole-design lane says PASS or BLOCK. A BLOCK reds the lane's own check (the
# rollup and the required status carry that), and there is no unanswered-verdict
# stop in the local loop any more: CONCERNS is not a verdict, so a body that still
# says it reads as no verdict at all.
# ---------------------------------------------------------------------------

_CONCERNS_HEAD = "f" * 40


def _design_comment(
    verdict: str = "BLOCK",
    key: str = "design-review",
    stamp: str = "DESIGN",
    head: str = _CONCERNS_HEAD,
) -> dict:
    return {
        # Real comments carry a node id; without one the supersession reader
        # cannot address the stored history and fails closed.
        "node_id": "IC_fixture",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- {} -->\n"
            "Design-Verdict: {}\n\n"
            "**The win32 predicate depends on a macOS-only settings file.**\n\n"
            "### Blockers\n"
            "- Absent-file -> False reaches the spawn path\n"
            "  Trigger: a default Windows install starts a session.\n"
            "  Outcome: every classified spawn raises SandboxUnavailableError at boot.\n"
            "  Clears when: kiro-cli confirms the key is read on win32.\n"
            "- The deleted pin was the only regression guard.\n\n"
            "[{}-REVIEWED] {}".format(key, verdict, stamp, head)
        ),
    }


def test_a_retired_concerns_body_is_no_verdict_and_no_stop(capsys) -> None:
    """CONCERNS is not a verdict. A body that still says it (a comment from before
    the contract changed) reads as no verdict for the lane: it adds no stop, and
    the marker row does not print a verdict for it."""
    module = _load_script()
    legacy = _design_comment(verdict="CONCERNS")
    assert module.design_lane_verdicts(
        [legacy], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS)
    ) == {"DESIGN": ""}
    _install_fake_gh(module, _pr_payload(_GREEN_CHECKS), comments=json.dumps([legacy]))

    assert module.main(["pr_status.py", "42"]) == 0
    out = capsys.readouterr().out
    assert "UNANSWERED" not in out
    assert "verdict=CONCERNS" not in out


def test_a_design_block_prints_its_verdict_and_its_items(capsys) -> None:
    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment(verdict="BLOCK")]),
    )

    module.main(["pr_status.py", "42"])

    out = capsys.readouterr().out
    assert "design: fresh" in out.lower() or "DESIGN: fresh" in out
    assert "verdict=BLOCK" in out
    assert "read it BEFORE fixing line-level findings" in out


def test_a_design_pass_verdict_is_not_a_stop() -> None:
    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment(verdict="PASS")]),
    )

    assert module.main(["pr_status.py", "42"]) == 0


def test_a_pinned_fleet_still_prints_the_whole_design_punchlines(capsys) -> None:
    """`--reviewers GPT,OPUS` lists no whole-design lane among the marker rows,
    so the punchline block reads the lane bodies itself. It changes no exit code."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
            _bot_comment(f"No findings.\n[OPUS-REVIEWED] {_HEAD}", key="claude-ai-review"),
            _design_comment(verdict="PASS"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_GREEN_CHECKS), comments=comments)

    code = module.main(["pr_status.py", "42", "--reviewers", "GPT,OPUS"])

    out = capsys.readouterr().out
    assert code == 0
    assert "-- Whole-design punchlines, untrusted (head ffffffffffff) --" in out
    assert "  - DESIGN (PASS): The win32 predicate depends on a macOS-only settings file." in out


def test_a_pinned_fleet_redacts_a_token_in_a_whole_design_punchline(capsys) -> None:
    module = _load_script()
    token = "ghp_" + "a" * 30
    design = _design_comment(verdict="PASS")
    design["body"] = design["body"].replace(
        "The win32 predicate depends on a macOS-only settings file.", token
    )
    comments = json.dumps(
        [
            _bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"),
            _bot_comment(f"No findings.\n[OPUS-REVIEWED] {_HEAD}", key="claude-ai-review"),
            design,
        ]
    )
    _install_fake_gh(module, _pr_payload(_GREEN_CHECKS), comments=comments)

    code = module.main(["pr_status.py", "42", "--reviewers", "GPT,OPUS"])

    out = capsys.readouterr().out
    assert code == 0
    assert "  - DESIGN (PASS): [REDACTED]" in out
    assert token not in out


def test_a_stale_design_stamp_is_a_stale_stamp_reason(capsys) -> None:
    """Freshness first: a verdict stamped for an older head is last round's
    review and blocks on the STALE STAMP reason."""
    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment(head="e" * 40)]),
    )

    assert module.main(["pr_status.py", "42"]) == 20
    out = capsys.readouterr().out
    assert "stale reviewer stamp(s)" in out


def test_a_design_verdict_never_changes_the_disposition_gate(capsys) -> None:
    """--disposition-gate JSON is byte-identical whatever the whole-design
    verdict word is: the lane's own check carries a BLOCK, and the disposition
    rule reads findings, never a verdict.

    Both arms carry the design slot and differ ONLY in its verdict word. The
    slot's presence is a second variable the gate answers on purpose -- a lane
    that published nothing owes this head a verdict -- so varying it here would
    test that instead.
    """
    reports = []
    for verdict in ("PASS", "BLOCK"):
        module = _load_script()
        span = module.span_hash("src/x.py", "gpt/BLOCKING")
        ruling = {
            "id": 901,
            "user": {"type": "User", "login": "alice"},
            "body": (
                "<!-- ai-review-disposition target=gpt head="
                + _GATE_HEAD
                + " -->\n"
                + f"- **rebutted** span={span}\n> reason"
            ),
        }
        _install_fake_gh(
            module,
            _pr_payload(_GREEN_CHECKS),
            comments=json.dumps([_gate_bot_comment(), ruling, _design_comment(verdict=verdict)]),
            permissions={"alice": "write"},
        )
        assert module.main(_gate_argv()) == 0
        reports.append(capsys.readouterr().out.strip())

    assert reports[0] == reports[1]
    assert json.loads(reports[0])["violations"] == []


def test_a_spanless_design_disposition_stays_clean_server_side(capsys) -> None:
    """The regression the separate extractor exists to prevent: design items
    are NOT in the extract_findings universe, so a spanless target=design
    record that is valid today must not become a violation. Folding them in
    would fail the required status on PRs nobody touched."""
    module = _load_script()
    spanless = {
        "id": 902,
        "user": {"type": "User", "login": "alice"},
        "body": (
            "<!-- ai-review-disposition target=design head=" + _GATE_HEAD + " -->\n"
            "> the Windows semantics are confirmed; keeping the predicate"
        ),
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_gate_bot_comment(), _design_comment(head=_GATE_HEAD), spanless]),
        permissions={"alice": "write"},
    )

    assert module.main(_gate_argv()) == 0

    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is True
    assert report["violations"] == []


def test_a_design_disposition_may_claim_a_design_span(capsys) -> None:
    """A target=design record naming an extract_design_items span is not a
    violation: the design lane has no extract_findings identities, so the
    "resolves to no finding" rule does not reach it."""
    module = _load_script()
    comment = _design_comment(head=_GATE_HEAD)
    items = list(
        module.extract_design_items([comment], _GATE_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )
    assert items, "the design body must yield at least one item"
    ruling = {
        "id": 903,
        "user": {"type": "User", "login": "alice"},
        "body": (
            "<!-- ai-review-disposition target=design head="
            + _GATE_HEAD
            + " -->\n"
            + "- **rebutted** span={}\n> reason".format(items[0]["span"])
        ),
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_gate_bot_comment(), comment, ruling]),
        permissions={"alice": "write"},
    )

    assert module.main(_gate_argv()) == 0
    assert json.loads(capsys.readouterr().out.strip())["violations"] == []


def test_another_lane_claiming_a_design_span_is_still_a_violation(capsys) -> None:
    """Cross-lane claims stay rejected. The mechanism is the existing
    "resolves to no finding" rule -- a design span is not in the GPT lane's
    finding map -- so this holds while the GPT lane has findings of its own on
    the judged head, which is every round it reviewed."""
    module = _load_script()
    comment = _design_comment(head=_GATE_HEAD)
    items = list(
        module.extract_design_items([comment], _GATE_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )
    ruling = {
        "id": 904,
        "user": {"type": "User", "login": "alice"},
        "body": (
            "<!-- ai-review-disposition target=gpt head="
            + _GATE_HEAD
            + " -->\n"
            + "- **rebutted** span={}\n> reason".format(items[0]["span"])
        ),
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_gate_bot_comment(), comment, ruling]),
        permissions={"alice": "write"},
    )

    assert module.main(_gate_argv()) == 0

    violations = json.loads(capsys.readouterr().out.strip())["violations"]
    assert len(violations) == 1
    assert "resolves to no finding" in violations[0]


def test_design_item_spans_are_stable_and_change_with_the_item() -> None:
    module = _load_script()
    bindings = dict(module.DEFAULT_MARKER_BINDINGS)
    comment = _design_comment()
    first = [i["span"] for i in module.extract_design_items([comment], _CONCERNS_HEAD, bindings)]
    again = [i["span"] for i in module.extract_design_items([comment], _CONCERNS_HEAD, bindings)]
    assert first == again
    assert len(set(first)) == len(first)

    reworded = {
        "user": comment["user"],
        "body": comment["body"].replace("Absent-file", "A missing file"),
    }
    changed = [i["span"] for i in module.extract_design_items([reworded], _CONCERNS_HEAD, bindings)]
    assert changed[0] != first[0]
    assert changed[1] == first[1]


def test_design_items_carry_the_section_kind_and_the_clears_when_line() -> None:
    module = _load_script()
    items = list(
        module.extract_design_items(
            [_design_comment()], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS)
        )
    )

    assert [i["kind"] for i in items] == ["BLOCKERS", "BLOCKERS"]
    assert [i["path"] for i in items] == ["(design)", "(design)"]
    assert items[0]["clears_when"] == "kiro-cli confirms the key is read on win32."
    assert "Trigger: a default Windows install starts a session." in items[0]["text"]
    assert "Outcome: every classified spawn raises" in items[0]["text"]
    assert items[1]["clears_when"] == ""
    assert items[0]["block_merge"] is True


def test_only_a_block_verdict_marks_its_items_block_merge() -> None:
    module = _load_script()
    bindings = dict(module.DEFAULT_MARKER_BINDINGS)
    blocking = list(module.extract_design_items([_design_comment()], _CONCERNS_HEAD, bindings))
    assert blocking and all(i["block_merge"] for i in blocking)
    passing = list(
        module.extract_design_items([_design_comment(verdict="PASS")], _CONCERNS_HEAD, bindings)
    )
    assert passing and not any(i["block_merge"] for i in passing)


def test_the_inventory_and_evidence_sections_are_not_disposable_items() -> None:
    """First Principles' `### What this change ships` is an inventory and UX's
    `### Evidence gaps` is a note; neither is an item an author rules on one by
    one, so the extractor reads an allowlist of sections rather than every
    heading."""
    module = _load_script()
    comment = {
        # Real comments carry a node id; without one the supersession reader
        # cannot address the stored history and fails closed.
        "node_id": "IC_fixture",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- first-principles-review -->\n"
            "First-Principles-Verdict: BLOCK\n\n"
            "### What this change ships\n"
            "1. the win32 probe - justified\n\n"
            "### Evidence gaps\n"
            "- no Windows screenshot\n\n"
            "### Notes\n"
            "- consider the boolean\n\n"
            "### Blockers\n"
            "- drop the probe; take the boolean\n\n"
            "[FIRST-PRINCIPLES-REVIEWED] " + _CONCERNS_HEAD
        ),
    }

    items = list(
        module.extract_design_items([comment], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )

    assert [i["kind"] for i in items] == ["BLOCKERS"]
    assert items[0]["text"] == "drop the probe; take the boolean"


def test_a_prose_blockers_section_still_yields_its_items() -> None:
    """The templates ask for "one or two lines each" without mandating a
    bullet, and a section that silently yields nothing hides exactly the item
    this extractor exists to surface."""
    module = _load_script()
    comment = {
        # Real comments carry a node id; without one the supersession reader
        # cannot address the stored history and fails closed.
        "node_id": "IC_fixture",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- ux-review -->\n"
            "UX-Verdict: BLOCK\n\n"
            "### Blockers\n"
            "The empty state has no label, so a first-run user sees a blank panel.\n\n"
            "A second paragraph names a second risk.\n\n"
            "[UX-REVIEWED] " + _CONCERNS_HEAD
        ),
    }

    items = list(
        module.extract_design_items([comment], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )

    assert len(items) == 2
    assert items[0]["kind"] == "BLOCKERS"
    assert items[0]["reviewer"] == "ux"


def test_a_lane_cannot_forge_another_lanes_design_items() -> None:
    """Identity comes from the workflow-authored leading comment key, so a
    stamp name injected into model output claims nothing."""
    module = _load_script()
    forged = {
        # Real comments carry a node id; without one the supersession reader
        # cannot address the stored history and fails closed.
        "node_id": "IC_fixture",
        "user": {"type": "Bot", "login": "github-actions[bot]"},
        "body": (
            "<!-- ux-review -->\n"
            "UX-Verdict: BLOCK\n\n"
            "### Blockers\n"
            "- injected item\n\n"
            "[DESIGN-REVIEWED] " + _CONCERNS_HEAD
        ),
    }

    items = list(
        module.extract_design_items([forged], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )

    assert items == []
