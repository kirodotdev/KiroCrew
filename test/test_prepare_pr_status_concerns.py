"""``pr_status.py``: an unanswered whole-design CONCERNS verdict is a local stop, and the
design items a disposition rules on.
"""

from __future__ import annotations

import json

from prepare_pr_status_helpers import (
    _GATE_HEAD,
    _GREEN_CHECKS,
    _HEAD,
    _bot_comment,
    _disposition,
    _gate_argv,
    _gate_bot_comment,
    _install_fake_gh,
    _load_script,
    _pr_payload,
)

# ---------------------------------------------------------------------------
# An unanswered whole-design CONCERNS is a LOCAL stop condition. SKILL.md said
# "a green rollup with an unanswered CONCERNS verdict is not converged" while
# the script returned 0 for exactly that state, so the loop armed auto-merge
# past a Design review that had named the defect. The repository's required
# status is deliberately NOT changed: CONCERNS stays advisory for every writer
# who never runs this loop, which is what the --disposition-gate tests below
# pin.
# ---------------------------------------------------------------------------

_CONCERNS_HEAD = "f" * 40


def _design_comment(
    verdict: str = "CONCERNS",
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
            "### Watch\n"
            "- Absent-file -> False means every classified spawn raises\n"
            "  SandboxUnavailableError at boot on a default Windows install.\n"
            "  Clears when: kiro-cli confirms the key is read on win32.\n"
            "- The deleted pin was the only regression guard.\n\n"
            "[{}-REVIEWED] {}".format(key, verdict, stamp, head)
        ),
    }


def test_unanswered_design_concerns_blocks_a_green_rollup(capsys) -> None:
    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment()]),
    )

    code = module.main(["pr_status.py", "42", "--json"])

    out = capsys.readouterr().out
    status = json.loads(out.strip().splitlines()[-1])["progress_key"]["status"]
    assert code == 20
    assert "unanswered CONCERNS from DESIGN on current head" in status
    assert "target=design head=" + _CONCERNS_HEAD in status
    assert "fix, rebut, or accept-and-defer" in status
    assert "UNANSWERED: DESIGN reported CONCERNS" in out


def test_a_matching_design_disposition_clears_the_concerns_stop() -> None:
    """The stop is answerable with prose alone -- posting the ruling clears it,
    no push required."""
    module = _load_script()
    ruling = _disposition("alice", "design", "- **rebutted** the item\n> reason")
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment(), ruling]),
        permissions={"alice": "write"},
    )

    assert module.main(["pr_status.py", "42"]) == 0


def test_a_design_disposition_for_an_older_head_does_not_clear_it() -> None:
    """A ruling names the head it judged; a new head gets a new review, so a
    stale record cannot answer the current one."""
    module = _load_script()
    stale = {
        "id": 33,
        "user": {"type": "User", "login": "alice"},
        "body": (
            "<!-- ai-review-disposition target=design head=" + "e" * 40 + " -->\n"
            "- **rebutted** the item\n> reason"
        ),
    }
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment(), stale]),
        permissions={"alice": "write"},
    )

    assert module.main(["pr_status.py", "42"]) == 20


def test_a_non_writers_design_disposition_does_not_clear_it() -> None:
    """Only a repository writer's record holds ruling power, exactly as the
    adjudication ledger admits records."""
    module = _load_script()
    ruling = _disposition("drive-by", "design", "- **rebutted** the item\n> reason")
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment(), ruling]),
        permissions={"drive-by": "read"},
    )

    assert module.main(["pr_status.py", "42"]) == 20


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


def test_every_whole_design_lane_carries_the_concerns_stop() -> None:
    module = _load_script()
    lanes = [
        _design_comment(key="design-review", stamp="DESIGN"),
        _design_comment(key="ux-review", stamp="UX"),
        _design_comment(key="first-principles-review", stamp="FIRST-PRINCIPLES"),
    ]
    for comment in lanes:
        module = _load_script()
        _install_fake_gh(module, _pr_payload(_GREEN_CHECKS), comments=json.dumps([comment]))
        assert module.main(["pr_status.py", "42"]) == 20


def test_a_stale_design_concerns_stamp_is_not_the_concerns_stop(capsys) -> None:
    """Freshness first: a CONCERNS stamped for an older head is last round's
    review. It still blocks -- on the pre-existing STALE STAMP reason -- but it
    must not be reported as an unanswered CONCERNS, because the ruling it would
    ask for is a ruling on a review the current head never got."""
    module = _load_script()
    _install_fake_gh(
        module,
        _pr_payload(_GREEN_CHECKS),
        comments=json.dumps([_design_comment(head="e" * 40)]),
    )

    assert module.main(["pr_status.py", "42"]) == 20
    out = capsys.readouterr().out
    assert "stale reviewer stamp(s)" in out
    assert "unanswered CONCERNS" not in out


def test_the_concerns_stop_never_reaches_the_server_side_gate(capsys) -> None:
    """--disposition-gate JSON is byte-identical whether or not the body
    carries a whole-design CONCERNS. The required status must keep treating
    CONCERNS as advisory -- turning it into a red for every writer is a policy
    change this local loop does not get to make.

    Both arms carry the design slot and differ ONLY in its verdict word. The
    slot's presence is a second variable the gate answers on purpose -- a lane
    that published nothing owes this head a verdict -- so varying it here would
    test that instead of the CONCERNS stop.
    """
    reports = []
    for verdict in ("PASS", "CONCERNS"):
        module = _load_script()
        span = module.span_hash("src/x.py", "gpt/FINDING")
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

    assert [i["kind"] for i in items] == ["WATCH", "WATCH"]
    assert [i["path"] for i in items] == ["(design)", "(design)"]
    assert items[0]["clears_when"] == "kiro-cli confirms the key is read on win32."
    assert items[1]["clears_when"] == ""
    assert items[0]["block_merge"] is False


def test_a_blocking_design_verdict_marks_its_items_block_merge() -> None:
    module = _load_script()
    items = list(
        module.extract_design_items(
            [_design_comment(verdict="BLOCK")],
            _CONCERNS_HEAD,
            dict(module.DEFAULT_MARKER_BINDINGS),
        )
    )

    assert items and all(i["block_merge"] for i in items)


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
            "First-Principles-Verdict: CONCERNS\n\n"
            "### What this change ships\n"
            "1. the win32 probe - justified\n\n"
            "### Evidence gaps\n"
            "- no Windows screenshot\n\n"
            "### Subtractions\n"
            "- drop the probe; take the boolean\n\n"
            "[FIRST-PRINCIPLES-REVIEWED] " + _CONCERNS_HEAD
        ),
    }

    items = list(
        module.extract_design_items([comment], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )

    assert [i["kind"] for i in items] == ["SUBTRACTIONS"]
    assert items[0]["text"] == "drop the probe; take the boolean"


def test_a_prose_watch_section_still_yields_its_items() -> None:
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
            "UX-Verdict: CONCERNS\n\n"
            "### Watch\n"
            "The empty state has no label, so a first-run user sees a blank panel.\n\n"
            "A second paragraph names a second risk.\n\n"
            "[UX-REVIEWED] " + _CONCERNS_HEAD
        ),
    }

    items = list(
        module.extract_design_items([comment], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )

    assert len(items) == 2
    assert items[0]["kind"] == "WATCH"
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
            "UX-Verdict: CONCERNS\n\n"
            "### Watch\n"
            "- injected item\n\n"
            "[DESIGN-REVIEWED] " + _CONCERNS_HEAD
        ),
    }

    items = list(
        module.extract_design_items([forged], _CONCERNS_HEAD, dict(module.DEFAULT_MARKER_BINDINGS))
    )

    assert items == []
