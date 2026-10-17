"""``pr_status.py``: accepted human-override records."""

from __future__ import annotations

import json
import re

from prepare_pr_status_helpers import (
    _ELIDED,
    _HEAD,
    _MIXED_HEAD,
    _OLD,
    ROOT,
    _bot_comment,
    _clean_checks,
    _install_fake_gh,
    _last_line_json,
    _load_script,
    _pr_payload,
)

# ---------------------------------------------------------------------------
# Accepted human-override records.
#
# `ai-review-human-override.yml` records a repository writer's SHA-scoped
# decision as a bot-authored comment whose FIRST bytes are
# `<!-- ai-review-human-override target=<lane> head=<sha> actor=<login>
# source=<id> -->`, then the lane workflow REPLACES its own keyed comment with
# a stampless "human override accepted" body, because no model verdict exists
# to stamp. Both halves are real: the `[<NAME>-REVIEWED]` stamp stays the proof
# a MODEL ran, and the override record is independent proof a HUMAN adjudicated
# this head. A consumer that reads only the first reads an accepted override
# as an unreviewed head.
# ---------------------------------------------------------------------------

_OVERRIDE_SOURCE = "5768692900"


def _override_comment(
    target: str = "gpt",
    head: str = _HEAD,
    actor: str = "maintainer",
    source: str = _OVERRIDE_SOURCE,
    login: str = "github-actions[bot]",
    user_type: str = "Bot",
    marker: str | None = None,
    lead: str = "",
) -> dict[str, object]:
    """The record ai-review-human-override.yml posts, byte-shape included."""
    line = (
        marker
        if marker is not None
        else "<!-- ai-review-human-override target={} head={} actor={} source={} -->".format(
            target, head, actor, source
        )
    )
    body = (
        "{}{}\n## Human judgment recorded\n\n"
        "@{} marked the **{}** AI finding as false positive, not applicable, or "
        "explicitly accepted for `{}`.\n\n> the finding is not reachable\n\n"
        "_This decision applies only to this commit. A new push requires a new "
        "judgment._".format(lead, line, actor, target, head)
    )
    return {"user": {"type": user_type, "login": login}, "body": body}


def _override_lane_comment(head: str = _HEAD, actor: str = "maintainer") -> dict[str, object]:
    """The stampless body the GPT lane rewrites its keyed comment to."""
    return _bot_comment(
        "## GPT 6.1 Review \u2014 human override accepted\n\n"
        "Human judgment by @{} overrides the GPT 6.1 finding for `{}`.\n\n"
        "_The model was not re-run because an authorized human decision "
        "supersedes it._".format(actor, head),
        key="codex-ai-review",
    )


def test_accepted_override_satisfies_the_clause_for_that_head(capsys) -> None:
    """The shape this produces in practice: the lane's live comment is
    rewritten stampless, a duplicate from an earlier head still carries
    that older `[GPT-REVIEWED]`, and the override record names this head."""
    module = _load_script()
    comments = json.dumps(
        [
            _override_lane_comment(),
            _bot_comment(f"BLOCKING -- src/a.py:1 -- old finding\n[GPT-REVIEWED] {_OLD}"),
            _bot_comment(f"No findings.\n[OPUS-REVIEWED] {_HEAD}", key="claude-ai-review"),
            _override_comment(),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--json"]) == 0
    report = _last_line_json(capsys)
    assert report["advisory"]["stale_reviewers"] == [], report
    assert report["advisory"]["overridden_reviewers"] == {"GPT": "maintainer"}, report


def test_override_reports_a_human_decision_not_a_model_review(capsys) -> None:
    """Honesty requirement: the row must not read like the model ran."""
    module = _load_script()
    comments = json.dumps([_override_lane_comment(), _override_comment()])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 0
    out = capsys.readouterr().out
    assert "GPT: OVERRIDDEN" in out, out
    assert "@maintainer" in out, out
    assert "GPT: fresh" not in out, out
    assert "GPT: STALE" not in out, out


def test_override_keeps_the_lane_visible_with_no_stamp_anywhere(capsys) -> None:
    """Deleting the duplicate comment must not be a way to pass.

    A stamp is otherwise the ONLY thing that puts GPT in the discovered reviewer
    set, so removing that comment takes the lane out of the evaluation entirely
    -- a clean report that proves nothing. An override record for this head keeps
    the lane in the universe and answers for it."""
    module = _load_script()
    comments = json.dumps([_override_comment()])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--json"]) == 0
    report = _last_line_json(capsys)
    assert report["advisory"]["overridden_reviewers"] == {"GPT": "maintainer"}, report
    assert report["advisory"]["stale_reviewers"] == [], report


def test_a_fresh_model_stamp_outranks_an_override_record(capsys) -> None:
    """A stamp for this head means the model DID run; say so, not OVERRIDDEN."""
    module = _load_script()
    comments = json.dumps(
        [_bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}"), _override_comment()]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--json"]) == 0
    report = _last_line_json(capsys)
    assert report["advisory"]["overridden_reviewers"] == {}, report


def test_normal_review_paths_are_unchanged_by_the_override_clause(capsys) -> None:
    """No override record: a fresh stamp still clears and a stale one blocks."""
    module = _load_script()
    fresh = json.dumps([_bot_comment(f"No findings.\n[GPT-REVIEWED] {_HEAD}")])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=fresh)
    assert module.main(["pr_status.py", "42", "--json"]) == 0
    assert _last_line_json(capsys)["advisory"]["overridden_reviewers"] == {}

    module = _load_script()
    stale = json.dumps([_bot_comment(f"No findings.\n[GPT-REVIEWED] {_OLD}")])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=stale)
    assert module.main(["pr_status.py", "42", "--json"]) == 20
    report = _last_line_json(capsys)
    assert report["advisory"]["stale_reviewers"] == ["GPT"], report
    assert report["advisory"]["overridden_reviewers"] == {}, report


def test_adjudication_clear_still_clears_through_its_intact_stamp(capsys) -> None:
    """The `clear` path defuses [BLOCK-MERGE] and deliberately LEAVES the
    freshness stamp, so it must keep passing on the stamp alone -- no override
    record is involved and none is invented."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(
                "## GPT 6.1 Review \u2014 adjudicated clear\n\n"
                "The `[BLOCK-MERGE]` marker is defused. The "
                f"`[GPT-REVIEWED] {_HEAD}` freshness stamp is deliberately left "
                f"intact.\n[GPT-REVIEWED] {_HEAD}"
            )
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--json"]) == 0
    report = _last_line_json(capsys)
    assert report["advisory"]["overridden_reviewers"] == {}, report


def test_override_naming_another_head_does_not_clear() -> None:
    """The record is machine-written from `.head.sha`, so the freshness
    tolerance that exists for model-transcribed stamps has no place here: an
    older head, a prefix, and an elided splice are all refused."""
    for head, oid in (
        (_OLD, _HEAD),
        (_HEAD[:12], _HEAD),
        (_ELIDED, _MIXED_HEAD),
        (_MIXED_HEAD[:20], _MIXED_HEAD),
    ):
        module = _load_script()
        comments = json.dumps([_override_lane_comment(), _override_comment(head=head)])
        _install_fake_gh(module, _pr_payload(_clean_checks(), headRefOid=oid), comments=comments)
        assert module.main(["pr_status.py", "42", "--reviewers", "GPT"]) == 20, head


def test_override_from_an_untrusted_author_does_not_clear() -> None:
    """Authority is the bot authorship of the record: the workflow verified the
    human's write permission before posting, and only it can post as that
    login. The identical bytes from anyone else are ignored."""
    for kwargs in (
        {"login": "coverage-app[bot]"},
        {"login": "github-actions[bot]", "user_type": "User"},
        {"login": "pr-author", "user_type": "User"},
        {"login": "GitHub-Actions[bot]2", "user_type": "Bot"},
    ):
        module = _load_script()
        comments = json.dumps([_override_lane_comment(), _override_comment(**kwargs)])
        _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)
        assert module.main(["pr_status.py", "42", "--reviewers", "GPT"]) == 20, kwargs


def test_malformed_override_marker_does_not_clear() -> None:
    """Fail closed on a record that does not carry full attribution, and on a
    marker that is not the body's leading bytes -- the same two conditions the
    lane workflows require before they treat an override as active."""
    marker = "<!-- ai-review-human-override target=gpt head={} actor={} source={} -->"
    for case in (
        {"marker": f"<!-- ai-review-human-override target=gpt head={_HEAD} -->"},
        {"marker": f"<!-- ai-review-human-override target=gpt head={_HEAD} actor=m -->"},
        {"marker": f"<!-- ai-review-human-override target=gpt head={_HEAD} source=1 -->"},
        {"marker": "<!-- ai-review-human-override target=gpt actor=m source=1 -->"},
        {"marker": marker.format(_HEAD, "m", "not-a-number")},
        {"marker": marker.format("zz" * 20, "m", "1")},
        {"lead": "Heads up:\n"},
    ):
        module = _load_script()
        comments = json.dumps([_override_lane_comment(), _override_comment(**case)])
        _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)
        assert module.main(["pr_status.py", "42", "--reviewers", "GPT"]) == 20, case


def test_override_for_another_lane_does_not_clear_gpt() -> None:
    """One record answers for the lane it names."""
    module = _load_script()
    comments = json.dumps(
        [
            _override_lane_comment(),
            _bot_comment(f"BLOCKING -- src/a.py:1 -- old\n[GPT-REVIEWED] {_OLD}"),
            _override_comment(target="ux"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 20


def test_target_all_answers_for_every_lane_under_evaluation(capsys) -> None:
    """`target=all` is the spelling that clears the whole fleet at once, so it
    satisfies a stale discovered lane and a pinned lane that never posted."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"BLOCKING -- src/a.py:1 -- old\n[GPT-REVIEWED] {_OLD}"),
            _override_comment(target="all"),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)
    assert module.main(["pr_status.py", "42", "--json"]) == 0
    assert _last_line_json(capsys)["advisory"]["overridden_reviewers"] == {"GPT": "maintainer"}

    module = _load_script()
    comments = json.dumps([_override_comment(target="all")])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)
    assert module.main(["pr_status.py", "42", "--reviewers", "GPT,OPUS", "--json"]) == 0
    assert _last_line_json(capsys)["advisory"]["overridden_reviewers"] == {
        "GPT": "maintainer",
        "OPUS": "maintainer",
    }


def test_target_all_does_not_invent_a_lane_that_never_spoke(capsys) -> None:
    """Discovery mode requires only lanes that POSTED. A blanket record answers
    for those; it must not enrol UX and DESIGN so the report claims a human
    adjudicated lanes that never ran."""
    module = _load_script()
    comments = json.dumps([_override_comment(target="all")])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--json"]) == 0
    report = _last_line_json(capsys)
    assert report["advisory"]["overridden_reviewers"] == {}, report


def test_pinned_lane_with_an_override_is_not_stale(capsys) -> None:
    """Pinning is what makes a silent lane required; an override answers it."""
    module = _load_script()
    comments = json.dumps([_override_lane_comment(), _override_comment()])
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42", "--reviewers", "GPT", "--json"]) == 0
    report = _last_line_json(capsys)
    assert report["advisory"]["stale_reviewers"] == [], report
    assert report["advisory"]["overridden_reviewers"] == {"GPT": "maintainer"}, report


def test_override_does_not_defuse_a_blocking_marker_on_this_head() -> None:
    """Scope boundary. The record answers the FRESHNESS clause -- whether this
    head was judged. A `[BLOCK-MERGE]` for the current head is a separate,
    deny-only signal the adjudication `clear` path owns, and an override
    silently defusing it would let this change widen a merge gate."""
    module = _load_script()
    comments = json.dumps(
        [
            _bot_comment(f"BLOCKING -- src/a.py:1 -- live\n[BLOCK-MERGE] {_HEAD}"),
            _override_comment(),
        ]
    )
    _install_fake_gh(module, _pr_payload(_clean_checks()), comments=comments)

    assert module.main(["pr_status.py", "42"]) == 20


def test_the_override_marker_contract_matches_producer_and_consumers() -> None:
    """Mechanical enumeration of the record's two ends, read from the workflows.

    The consumer regex pins the producer's exact byte shape, so a field inserted
    ahead of ``actor=`` would stop clearing overrides -- fail-closed, but
    silently. Deriving the shape from the producer file turns that into a test
    failure.

    The target table is derived the same way. Each lane file carries BOTH the
    target spelling it consumes and its own comment key, so the table must map
    exactly the targets whose lane has a reviewer binding. A row for a lane with
    no binding resolves to nothing and would clear nothing; a bound lane missing
    from the table would ignore a recorded judgment. Both fail here.
    """
    module = _load_script()
    contract = module._review_contract
    workflows = ROOT / ".github" / "workflows"
    producer = (workflows / "ai-review-human-override.yml").read_text(encoding="utf-8")
    marker_line = next(ln for ln in producer.splitlines() if 'marker="<!--' in ln)
    rendered = (
        marker_line.split('marker="', 1)[1]
        .rsplit('"', 1)[0]
        .replace("$target", "gpt")
        .replace("$head", _HEAD)
        .replace("$ACTOR", "maintainer")
        .replace("$COMMENT_ID", _OVERRIDE_SOURCE)
    )
    match = contract.OVERRIDE_MARKER_RE.match(rendered)
    assert match is not None, rendered
    assert match.groups() == ("gpt", _HEAD, "maintainer", _OVERRIDE_SOURCE), rendered

    bindings = dict(module.DEFAULT_MARKER_BINDINGS)
    derived: dict[str, str] = {}
    blanket = False
    for path in sorted(workflows.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        targets = set(re.findall(r"<!-- ai-review-human-override target=([a-z-]+) head=", text))
        if not targets:
            continue
        blanket = blanket or contract.OVERRIDE_TARGET_ALL in targets
        keys = [key for key in bindings if "<!-- {} -->".format(key) in text]
        assert len(keys) <= 1, (path.name, keys)
        for target in targets - {contract.OVERRIDE_TARGET_ALL}:
            for key in keys:
                derived[target] = key
    assert blanket, "no lane consumes target=all"
    assert derived == dict(contract.DEFAULT_OVERRIDE_TARGET_KEYS), derived

    reachable = {
        name for target in derived for name in contract.override_reviewer_names(target, bindings)
    }
    assert reachable == set(bindings.values()), reachable


def test_the_evaluator_itself_refuses_an_untrusted_override_record() -> None:
    """Defence in depth, and the reason it needs its own test.

    A run through ``main()`` cannot observe this: ``fetch_bot_comments`` already
    drops a comment whose author is not on the allowlist, so the forged record
    never reaches the evaluator. But ``evaluate_reviewer_markers`` is exported
    and called directly, and a record's whole authority is WHO wrote it, so the
    check belongs at the point of use as well -- proven here by handing the
    function a list its caller would have filtered.
    """
    module = _load_script()
    bindings = dict(module.DEFAULT_MARKER_BINDINGS)
    forged = _override_comment(login="coverage-app[bot]")
    recorded = _override_comment()

    ignored = module.evaluate_reviewer_markers([forged], _HEAD, bindings, only=["GPT"])
    assert ignored["stale"] == ["GPT"], ignored
    assert ignored["overridden"] == {}, ignored

    honoured = module.evaluate_reviewer_markers([recorded], _HEAD, bindings, only=["GPT"])
    assert honoured["stale"] == [], honoured
    assert honoured["overridden"] == {"GPT": "maintainer"}, honoured

    # The allowlist is a parameter, not a constant: a caller that widens it --
    # the `--marker-authors` seam -- gets exactly what it asked for.
    widened = module.evaluate_reviewer_markers(
        [forged], _HEAD, bindings, only=["GPT"], authors=("coverage-app[bot]",)
    )
    assert widened["overridden"] == {"GPT": "maintainer"}, widened
