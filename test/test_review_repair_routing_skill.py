"""Source contracts for repair delegation, not live model-entitlement tests."""

import ast
import json
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev"
PREPARE = SKILLS / "prepare-pr" / "SKILL.md"


def _text(path):
    return path.read_text(encoding="utf-8")


def _routing():
    return _text(PREPARE).split("## Review repair routing\n", 1)[1].split("\n## Dispositions", 1)[0]


def test_family_ladders_preserve_generation_then_capability_fallback():
    rows = [line.split("|")[2].strip() for line in _routing().splitlines() if " -> " in line]
    assert [row.split(" -> ") for row in rows] == [
        [
            "Fable 5.1",
            "Fable 5",
            "latest available Opus",
            "older Opus generations",
            "lower-capability available general model",
        ],
        [
            "GPT 6",
            "GPT 5.6 best available variant",
            "older capable GPT",
            "available general fallback",
        ],
    ]
    assert "Opus-family" in _routing() and "GPT 5.6 review lane" in _routing()
    assert "fork lanes" in _routing()


def test_model_selection_is_discovered_and_service_evidence_is_not_invented():
    text = " ".join(_routing().split())
    for contract in (
        "exact IDs from the current backend/account model listing",
        "catalogue entry is not entitlement",
        "schema supports model pinning",
        "runtime/provider-reported actual model evidence",
        "effort-application note is not proof",
        "served model unverified",
        "Disclose fallback family and reason",
    ):
        assert contract in text
    assert "spawn_sub_agents" not in text


def test_repair_work_and_self_review_are_delegated_then_parent_checked():
    text = " ".join(_routing().split())
    for contract in (
        "pin `model` explicitly",
        "blocker, not permission for the parent to self-fix",
        "Delegate the minimal fix AND self-review",
        "regression tests",
        "commits, pushes, merges or recursive delegation",
        "Serialize overlapping writers",
        "The parent reads the returned diff",
        "Phase 2's unchanged gates",
        "delegation grants no commit/push authority",
        "SHA-pinned force-with-lease",
    ):
        assert contract in text
    phase_three = _text(PREPARE).split("### Phase 3", 1)[1].split("### Phase 4", 1)[0]
    assert "MUST execute [Review repair routing](#review-repair-routing)" in phase_three


def test_fallback_does_not_skip_siblings_or_replay_partial_edits():
    text = " ".join(_routing().split())
    for contract in (
        "finite candidate list, each candidate once",
        "Only explicit model unavailability before work starts",
        "Tool/policy errors or transport failures are not model unavailability",
        "Before any retry inspect the run result, transcript and diff",
        "do not automatically rerun partial edits",
        "never bypass policy or retry endlessly",
    ):
        assert contract in text
    assert "one dispatch per preference tier" not in text
    assert "primary originating lane" not in text


@pytest.mark.parametrize("name", ["kirocrew-worktree-dev", "babysit"])
def test_entry_skills_load_one_canonical_contract(name):
    path = SKILLS / name / "SKILL.md"
    text = _text(path)
    link = "../prepare-pr/SKILL.md#review-repair-routing"
    assert "MUST load" in text and link in text
    assert (path.parent / link.split("#")[0]).resolve() == PREPARE
    assert "Fable 5.1" not in text


def test_local_review_and_generic_monitoring_keep_their_own_contracts():
    prepare = " ".join(_text(PREPARE).split())
    assert "never the repair-family table" in prepare
    assert "do not change CI models, profile `reviewers[]`/`model_tier`" in prepare
    babysit = " ".join(_text(SKILLS / "babysit" / "SKILL.md").split())
    assert "works without a Kiro Crew checkout or prepare-pr installed" in babysit
    for contract in ("GitLab", "Bitbucket", "Stall tripwire", "not counted AND not reset"):
        assert contract in babysit
    assert "Other-host guidance is not live-verified here" in babysit
    assert "detailed_merge_status" not in babysit


@pytest.mark.parametrize(
    ("name", "byte_ceiling"),
    [("kirocrew-worktree-dev", 16000), ("babysit", 22000), ("prepare-pr", 68000)],
)
def test_slim_skills_do_not_regrow_duplicate_guidance_or_wire_model_ids(name, byte_ceiling):
    text = _text(SKILLS / name / "SKILL.md")
    assert len(text.encode("utf-8")) <= byte_ceiling
    assert not re.search(r"\b(?:gpt|claude|fable)-(?:\d|opus|fable)", text)
    assert len(_routing().encode("utf-8")) <= 3000


def test_ci_doc_links_the_contract_and_rationale_drops_stale_budget():
    ci_doc = _text(ROOT / "docs/ci/ci-and-reviews.md")
    assert "prepare-pr/SKILL.md#review-repair-routing" in ci_doc
    rationale = _text(SKILLS / "prepare-pr/references/rationale.md")
    assert "3 is the real limit" not in rationale and "10-iteration backstop" not in rationale
    assert "## Why review repairs are delegated by reviewer family" in rationale


def test_ladder_generation_names_live_only_in_the_canonical_table():
    heads = [
        row.split("|")[2].split(" -> ")[0].strip()
        for row in _routing().splitlines()
        if " -> " in row
    ]
    assert heads
    for path in (
        ROOT / "docs/ci/ci-and-reviews.md",
        SKILLS / "prepare-pr/references/rationale.md",
        SKILLS / "kirocrew-worktree-dev/SKILL.md",
        SKILLS / "babysit/SKILL.md",
    ):
        for head in heads:
            assert head not in _text(path), f"{path.name} restates {head!r}"


def test_local_reviewers_are_pinned_per_call_and_launched_concurrently():
    phase_two = " ".join(
        _text(PREPARE).split("### Phase 2", 1)[1].split("### Phase 3", 1)[0].split()
    )
    assert "one model-pinned `spawn_run` call per entry in `reviewers[]`" in phase_two
    assert "never the repair-family table" in phase_two
    assert "separate calls carry independent pins" in phase_two
    assert "run concurrently" in phase_two
    assert "END THE TURN once after the whole launch batch" in phase_two
    assert "wait for every completion before reading results or editing" in phase_two
    assert "disclose the sequential fallback" in phase_two
    assert "collect completion before dispatching the next" not in phase_two
    assert "END THE TURN after each call" not in phase_two


@pytest.mark.parametrize("name", ["prepare-pr", "babysit"])
def test_arming_refusals_preserve_existing_loops_and_user_stops(name):
    text = " ".join(_text(SKILLS / name / "SKILL.md").split())
    assert "create-only refusal means a loop may already be active" in text
    assert "Missing hosting context permits" in text
    assert "retained-stop refusal needs the owner" in text
    assert "another driver" in text
    assert "explicit refusal means nothing armed" not in text
    assert "explicit arming refusal comes back, no loop runs" not in text


def test_local_review_tier_is_resolved_and_noncode_repairs_explain_verification():
    text = " ".join(_text(PREPARE).split())
    assert "a tier label is not a model ID" in text
    assert "current backend/account model listing" in text
    assert "regression tests for testable changes (otherwise explain verification)" in text


def test_babysit_separates_provider_facts_from_comment_and_reporting_work():
    text = " ".join(_text(SKILLS / "babysit" / "SKILL.md").split())
    for contract in (
        "typed provider observes every fact",
        "On Webex, use the finite legacy path",
        "generic issue/pull-request comments or advisory review findings",
        "call the finite legacy path directly with `gate=false`",
        "terminal success uses zero model turns",
        "final report or notification",
    ):
        assert contract in text
    assert "token cap applies only when usage is reported" in text
    assert "`token_usage_known`" in text
    assert "Terminal records are read-only" in text
    assert "Changing `target` or `objective` starts a new baseline" in text
    assert "retained user stop cannot be replaced by rearming" in text
    assert "do not copy or register it for new babysit work" in text
    assert "Use `0` only" not in text


@pytest.mark.parametrize("name", ["prepare-pr", "babysit"])
def test_arm_acknowledgement_requires_a_later_turn_not_an_immediate_retry(name):
    text = " ".join(_text(SKILLS / name / "SKILL.md").split())
    assert "pending request" in text or "pending application request" in text
    assert "END THE TURN" in text or "END YOUR TURN" in text
    assert "On a later" in text
    assert "before the turn ends" in text or "before the pending request applies" in text
    assert "user stop" in text and "budget" in text
    assert "retry arming at most ONCE" not in text
    assert "check BEFORE ending the turn" not in text


def test_comment_aware_recipes_are_finite_ungated_and_keep_repair_routing():

    babysit = _text(SKILLS / "babysit" / "SKILL.md")
    legacy, _ = json.JSONDecoder().raw_decode(babysit.split("monitor_start(", 1)[1])
    prepare_recipe = next(
        textwrap.dedent(block).strip()
        for block in _text(PREPARE).split("```")
        if textwrap.dedent(block).strip().startswith("monitor_start(")
    )
    call = ast.parse(prepare_recipe, mode="eval").body
    assert isinstance(call, ast.Call)
    prepare = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    for recipe in (legacy, prepare):
        assert recipe["gate"] is False
        assert recipe["interval_secs"] > 0
        assert recipe["max_cycles"] > 0
        assert recipe["max_runtime_secs"] > recipe["interval_secs"] * recipe["max_cycles"]
        for contract in (
            "Review repair routing",
            "model-pinned",
            "parent verification",
            "authorized",
            "terminal",
            "budget",
            "autonudge_stop",
        ):
            assert contract in recipe["message"]
    assert (prepare["max_cycles"], prepare["max_runtime_secs"]) == (80, 86400)
    assert (legacy["max_cycles"], legacy["max_runtime_secs"]) == (24, 14400)
    assert "`max_cycles=80` and `max_runtime_secs=86400`" in _text(PREPARE)
