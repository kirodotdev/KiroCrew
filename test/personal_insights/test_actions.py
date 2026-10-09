from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.personal_insights import insights_diff as diffmod
from kiro_crew.personal_insights import insights_models as models
from kiro_crew.personal_insights import insights_verification as verify
from kiro_crew.personal_insights.insights_canonical import canonical_lesson_digest
from kiro_crew.personal_insights.insights_models import (
    BurdenVector,
    ClaimEvidence,
    action_key,
    enumerate_candidates,
    stable_target_scope,
)
from kiro_crew.personal_insights.insights_ontology import BehaviorKey
from kiro_crew.personal_insights.insights_platform import (
    PlatformError,
    RuntimePlatform,
    resolve_runtime_platform,
)
from kiro_crew.personal_insights.insights_registry import load_registry

SUBJECT = "session_owner"
WORKSPACE = "ws-opaque-1234"


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def _bkey(predicate: str, scope: str = "global", subject: str = SUBJECT) -> BehaviorKey:
    return BehaviorKey(subject, predicate, "positive", scope)


def _evidence(
    predicate: str,
    *,
    supporting: int = 2,
    tier: str = models.TIER_REPEATED,
    repo: str | None = None,
    base_digest: str | None = None,
    scope: str = "global",
    verified: bool = False,
    burden: BurdenVector | None = None,
    subject: str = SUBJECT,
) -> ClaimEvidence:
    return ClaimEvidence(
        behavior_key=_bkey(predicate, scope, subject),
        supporting_session_count=supporting,
        claim_eligible_session_count=supporting,
        wording_tier=tier,
        verified_consequence=verified,
        burden=burden or BurdenVector(),
        repository_scope=repo,
        base_digest=base_digest,
        workspace_id=WORKSPACE,
        display_locator="docs/x.md",
    )


LINUX = RuntimePlatform("linux")


# ── eligibility floor and tier ──


def test_minimum_supporting_sessions_is_two() -> None:
    assert models.MINIMUM_SUPPORTING_SESSIONS == 2


def test_two_session_floor_accepts(registry) -> None:
    result = enumerate_candidates(registry, [_evidence("repeats_context_setup")], LINUX)
    assert len(result.candidates) == 1
    assert result.candidates[0].capability_id == "prompt.context-setup-reuse"


def test_one_session_rejected_below_floor(registry) -> None:
    result = enumerate_candidates(
        registry, [_evidence("repeats_context_setup", supporting=1)], LINUX
    )
    assert not result.candidates
    assert result.rejected[0].reason == models.REJECT_SUPPORT_FLOOR


@pytest.mark.parametrize(
    "predicate",
    ["repeats_context_setup", "verification_omission_explicit", "needs_capability_discovery"],
)
def test_single_tier_rejected_for_every_class(registry, predicate: str) -> None:
    result = enumerate_candidates(registry, [_evidence(predicate, tier=models.TIER_SINGLE)], LINUX)
    assert not result.candidates
    assert result.rejected[0].reason == models.REJECT_SINGLE_TIER


# ── stable scope ──


def test_stable_scope_prompt_is_none(registry) -> None:
    cap = registry.by_id("prompt.context-setup-reuse")
    ev = _evidence("repeats_context_setup")
    assert stable_target_scope(cap, ev) == models.SCOPE_NONE


def test_stable_scope_existing_capability_is_capability(registry) -> None:
    cap = registry.by_id("existing.app-list")
    ev = _evidence("needs_capability_discovery")
    assert stable_target_scope(cap, ev) == models.SCOPE_CAPABILITY


def test_stable_scope_lesson_global_uses_no_repository_scope(registry) -> None:
    cap = registry.by_id("lesson.verification-omission")
    ev = _evidence("verification_omission_explicit")
    scope = stable_target_scope(cap, ev)
    assert scope == f"lesson_scope\x1fglobal\x1f{models.NO_REPOSITORY_SCOPE}"


def test_stable_scope_lesson_with_repo(registry) -> None:
    cap = registry.by_id("lesson.verification-omission")
    ev = _evidence("verification_omission_explicit", repo="src/pkg")
    scope = stable_target_scope(cap, ev)
    assert scope == "lesson_scope\x1fglobal\x1fsrc/pkg"


def test_stable_scope_steering_workspace_file(registry) -> None:
    cap = registry.by_id("steering.reversible-changes")
    ev = _evidence(
        "preserves_reversible_changes",
        repo="src/pkg",
        base_digest="a" * 64,
        subject="workflow_pattern",
    )
    scope = stable_target_scope(cap, ev)
    assert scope == f"workspace_file\x1f{WORKSPACE}\x1fdocs/x.md"


# ── action key composition ──


def test_action_key_hashes_four_tuple() -> None:
    key = _bkey("repeats_context_setup")
    parts = [key.canonical(), "cap", "prompt", models.SCOPE_NONE]
    framed = "".join(f"{len(part)}:{part}\x1e" for part in parts)
    expected = hashlib.sha256(framed.encode("utf-8")).hexdigest()
    assert action_key(key, "cap", "prompt", models.SCOPE_NONE) == expected


# ── prerequisites and platform ──


def test_steering_without_repo_scope_is_prerequisite_rejected(registry) -> None:
    result = enumerate_candidates(
        registry, [_evidence("preserves_reversible_changes", subject="workflow_pattern")], LINUX
    )
    assert not result.candidates
    assert result.rejected[0].reason == models.REJECT_PREREQUISITE


def test_steering_without_base_digest_is_rejected(registry) -> None:
    result = enumerate_candidates(
        registry,
        [_evidence("preserves_reversible_changes", repo="src/pkg", subject="workflow_pattern")],
        LINUX,
    )
    assert not result.candidates
    assert result.rejected[0].reason == models.REJECT_BASE_DIGEST


def test_steering_prerequisites_satisfied_accepts(registry) -> None:
    result = enumerate_candidates(
        registry,
        [
            _evidence(
                "preserves_reversible_changes",
                repo="src/pkg",
                base_digest="a" * 64,
                subject="workflow_pattern",
            )
        ],
        LINUX,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].capability_id == "steering.reversible-changes"


def test_missing_repo_scope_is_not_rejection_for_lesson(registry) -> None:
    result = enumerate_candidates(registry, [_evidence("verification_omission_explicit")], LINUX)
    assert len(result.candidates) == 1
    assert result.candidates[0].capability_id == "lesson.verification-omission"
    assert not result.rejected


def test_unsupported_platform_is_rejected(registry) -> None:
    import dataclasses

    caps = []
    for cap in registry.capabilities:
        if cap.capability_id == "prompt.context-setup-reuse":
            caps.append(dataclasses.replace(cap, supported_platforms=("darwin",)))
        else:
            caps.append(cap)
    modified = dataclasses.replace(registry, capabilities=tuple(caps))
    result = enumerate_candidates(modified, [_evidence("repeats_context_setup")], LINUX)
    assert not result.candidates
    assert result.rejected[0].reason == models.REJECT_PLATFORM


def test_client_platform_override_rejected(registry) -> None:
    for forbidden in ("platform", "execution_platform", "account", "workspace_id", "project"):
        with pytest.raises(PlatformError):
            enumerate_candidates(
                registry,
                [_evidence("repeats_context_setup")],
                LINUX,
                request_fields={forbidden: "x"},
            )


def test_resolve_runtime_platform_rejects_null() -> None:
    with pytest.raises(PlatformError):
        resolve_runtime_platform(None)


def test_cache_identity_separated_by_platform(registry) -> None:
    linux = enumerate_candidates(registry, [_evidence("repeats_context_setup")], LINUX).candidates[
        0
    ]
    darwin = enumerate_candidates(
        registry, [_evidence("repeats_context_setup")], RuntimePlatform("darwin")
    ).candidates[0]
    assert linux.cache_identity != darwin.cache_identity


# ── dedup and ranking ──


def test_canonical_dedup_before_sort(registry) -> None:
    ev = _evidence("repeats_context_setup")
    result = enumerate_candidates(registry, [ev, ev], LINUX)
    assert len(result.candidates) == 1


def test_verified_consequence_ranks_before_interpretation(registry) -> None:
    interpreted = _evidence("repeats_context_setup", supporting=9, verified=False)
    verified = _evidence("verification_omission_explicit", supporting=2, verified=True)
    result = enumerate_candidates(registry, [interpreted, verified], LINUX)
    assert result.candidates[0].verified_consequence is True


def test_higher_supporting_count_ranks_higher_among_interpreted(registry) -> None:
    small = _evidence("repeats_context_setup", supporting=2, verified=False)
    big = _evidence("needs_capability_discovery", supporting=9, verified=False)
    result = enumerate_candidates(registry, [small, big], LINUX)
    assert result.candidates[0].supporting_session_count == 9


def test_roots_take_each_behavior_head_before_alternatives(registry) -> None:
    evidences = [
        _evidence("repeats_context_setup"),
        _evidence("verification_omission_explicit"),
        _evidence("needs_capability_discovery"),
    ]
    result = enumerate_candidates(registry, evidences, LINUX)
    behaviors = {c.behavior_key.canonical() for c in result.roots}
    assert len(behaviors) == len(result.roots)
    assert len(result.roots) <= models.MAX_ROOTS


def test_same_behavior_fallback_advances(registry) -> None:
    ev = _evidence("repeats_context_setup")
    result = enumerate_candidates(registry, [ev], LINUX)
    nxt = models.same_behavior_fallback(result, ev.behavior_key, {"prompt.context-setup-reuse"})
    assert nxt is None


# ── verification oracles ──


def test_read_only_argv_oracle() -> None:
    oracle = verify.build_read_only_argv(("kirocrew", "learn", "list"), 0, "text")
    assert verify.read_only_argv_passes(oracle, 0, "text") is True
    assert verify.read_only_argv_passes(oracle, 1, "text") is False
    with pytest.raises(verify.VerificationError):
        verify.build_read_only_argv((), 0, "text")


def test_state_readback_oracle() -> None:
    oracle = verify.build_state_readback("lesson", "a" * 64)
    assert verify.state_readback_passes(oracle, "a" * 64) is True
    assert verify.state_readback_passes(oracle, "b" * 64) is False
    with pytest.raises(verify.VerificationError):
        verify.build_state_readback("lesson", "short")
    with pytest.raises(verify.VerificationError):
        verify.build_state_readback("unknown", "a" * 64)


def test_future_observation_oracle_never_passes_from_count_alone() -> None:
    oracle = verify.build_future_observation("session_owner|verifies_changes|positive|global", 3)
    assert verify.future_observation_opens_manual_review(oracle, 3) is True
    assert verify.future_observation_opens_manual_review(oracle, 2) is False
    with pytest.raises(verify.VerificationError):
        verify.build_future_observation("k", 0)


def test_discriminated_kinds_are_exact() -> None:
    assert verify.VERIFICATION_KINDS == frozenset(
        {"read_only_argv", "state_readback", "future_observation"}
    )


# ── steering diff apply/reverse digest restoration ──

BASE = "line one\nline two\nline three\n"
BASE_DIGEST = hashlib.sha256(BASE.encode("utf-8")).hexdigest()
GOOD_DIFF = (
    "--- a/.kiro/steering/x.md\n"
    "+++ b/.kiro/steering/x.md\n"
    "@@ -1,3 +1,3 @@\n"
    " line one\n"
    "-line two\n"
    "+line two changed\n"
    " line three\n"
)


def test_steering_diff_apply_and_reverse_restore_base_digest() -> None:
    modified = diffmod.apply_diff(BASE, GOOD_DIFF, BASE_DIGEST, expected_path=".kiro/steering/x.md")
    assert modified == "line one\nline two changed\nline three\n"
    restored = diffmod.reverse_apply_diff(modified, GOOD_DIFF, expected_path=".kiro/steering/x.md")
    assert hashlib.sha256(restored.encode("utf-8")).hexdigest() == BASE_DIGEST


def test_steering_diff_wrong_base_digest_rejected() -> None:
    with pytest.raises(diffmod.DiffError, match="base digest"):
        diffmod.apply_diff(BASE, GOOD_DIFF, "0" * 64, expected_path=".kiro/steering/x.md")


@pytest.mark.parametrize(
    "bad, match",
    [
        ("--- a/x\n+++ b/x\nnew file mode 100644\n@@ -1,1 +1,1 @@\n-a\n+b\n", "creation, deletion"),
        (
            "--- a/x\n+++ b/x\ndeleted file mode 100644\n@@ -1,1 +1,1 @@\n-a\n+b\n",
            "creation, deletion",
        ),
        ("--- a/x\n+++ b/x\nnew mode 120000\n@@ -1,1 +1,1 @@\n-a\n+b\n", "creation, deletion"),
        ("--- a/x\n+++ b/x\nrename from x\nrename to y\n@@ -1,1 +1,1 @@\n-a\n+b\n", "rename"),
        ("--- a/x\n+++ b/x\nBinary files a/x and b/x differ\n", "binary"),
        ("--- a//etc/p\n+++ b//etc/p\n@@ -1,1 +1,1 @@\n-a\n+b\n", "absolute path"),
        ("--- a/../e\n+++ b/../e\n@@ -1,1 +1,1 @@\n-a\n+b\n", "traversal"),
        ("--- a/x\n+++ b/y\n@@ -1,1 +1,1 @@\n-a\n+b\n", "paths must match"),
        ("--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n*bad\n", "malformed hunk line"),
    ],
)
def test_forbidden_diff_classes(bad: str, match: str) -> None:
    with pytest.raises(diffmod.DiffError, match=match):
        diffmod.parse_diff(bad)


def test_overlapping_hunks_rejected() -> None:
    overlapping = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n-b\n+bb\n@@ -2,2 +2,2 @@\n b\n-c\n+cc\n"
    with pytest.raises(diffmod.DiffError, match="overlapping"):
        diffmod.parse_diff(overlapping)


# ── isolated lesson-store apply/remove digest restoration ──


def _lessons_digest(store: LessonStore) -> str:
    rows = sorted(
        canonical_lesson_digest(
            {
                "rule": le.rule,
                "negative": le.negative,
                "category": le.category,
                "repo_scope": le.repo_scope,
                "applies": le.applies,
            }
        )
        for le in store.load_all()
    )
    return hashlib.sha256("".join(rows).encode("ascii")).hexdigest()


def test_lesson_store_apply_remove_restores_digest(tmp_path: Path) -> None:
    store = LessonStore(base_dir=tmp_path)
    store.save(Lesson(ts="1970-01-01T00:00:00Z", rule="pre-existing", category="preference"))
    base_digest = _lessons_digest(store)
    store.save(
        Lesson(ts="1970-01-01T00:00:01Z", rule="proposed lesson text", category="preference")
    )
    assert _lessons_digest(store) != base_digest
    assert store.remove("proposed lesson text") is True
    assert _lessons_digest(store) == base_digest


# ── cross-process action key stability ──


def test_action_key_stable_across_processes(tmp_path: Path) -> None:
    key = _bkey("verification_omission_explicit")
    in_process = action_key(
        key,
        "lesson.verification-omission",
        "lesson_proposal",
        "lesson_scope\x1fglobal\x1fno_repository_scope",
    )
    script = tmp_path / "keygen.py"
    script.write_text(
        "from kiro_crew.personal_insights.insights_ontology import BehaviorKey\n"
        "from kiro_crew.personal_insights.insights_models import action_key\n"
        "k = BehaviorKey('session_owner', 'verification_omission_explicit', 'positive', 'global')\n"
        "print(action_key(k, 'lesson.verification-omission', 'lesson_proposal',"
        " 'lesson_scope\\x1fglobal\\x1fno_repository_scope'))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == in_process


# ── Spec v6 defect-closure behavioral tests ──


def test_eligibility_requires_two_claim_eligible_not_raw_support(registry) -> None:
    ev = _evidence("repeats_context_setup", supporting=1)
    ev = dataclasses_replace(ev, claim_eligible_session_count=1)
    result = enumerate_candidates(registry, [ev], LINUX)
    assert not result.candidates
    assert result.rejected[0].reason == models.REJECT_SUPPORT_FLOOR


def test_two_claim_eligible_accepts_even_with_equal_support(registry) -> None:
    ev = _evidence("repeats_context_setup", supporting=2)
    ev = dataclasses_replace(ev, claim_eligible_session_count=2)
    result = enumerate_candidates(registry, [ev], LINUX)
    assert len(result.candidates) == 1


def test_reversibility_ranking_puts_fully_reversible_first() -> None:
    assert models.reversibility_rank("fully_reversible") < models.reversibility_rank(
        "reversible_with_undo"
    )
    assert models.reversibility_rank("reversible_with_undo") < models.reversibility_rank(
        "irreversible"
    )


def test_all_nine_ranking_levels_present_and_ordered(registry) -> None:
    levels = models.RANKING_LEVELS
    assert levels == (
        "verified_consequence",
        "supporting_session_count",
        "burden_vector",
        "implementation_effort",
        "reversibility",
        "verification_latency",
        "selection_priority",
        "capability_id",
        "canonical_tuple",
    )


def test_reversibility_level_prefers_greater_reversibility(registry) -> None:
    import dataclasses

    caps = []
    for cap in registry.capabilities:
        if cap.capability_id == "prompt.context-setup-reuse":
            caps.append(dataclasses.replace(cap, reversibility="irreversible"))
        else:
            caps.append(cap)
    modified = dataclasses.replace(registry, capabilities=tuple(caps))
    a = _evidence("repeats_context_setup", supporting=4, verified=False)
    b = _evidence("needs_capability_discovery", supporting=4, verified=False)
    result = enumerate_candidates(modified, [a, b], LINUX)
    first = result.candidates[0]
    assert first.capability_id == "existing.app-list"
    assert first.reversibility == "fully_reversible"


@pytest.mark.parametrize(
    "predicate, subject",
    [
        ("repeats_context_setup", SUBJECT),
        ("verification_omission_explicit", SUBJECT),
        ("needs_capability_discovery", SUBJECT),
        ("preserves_reversible_changes", "workflow_pattern"),
    ],
)
def test_one_session_rejected_all_classes_with_valid_prerequisites(
    registry, predicate: str, subject: str
) -> None:
    ev = _evidence(
        predicate,
        supporting=1,
        repo="src/pkg",
        base_digest="a" * 64,
        subject=subject,
    )
    ev = dataclasses_replace(ev, claim_eligible_session_count=1)
    result = enumerate_candidates(registry, [ev], LINUX)
    assert not result.candidates
    assert result.rejected[0].reason == models.REJECT_SUPPORT_FLOOR


def test_behavior_key_rejects_negative_polarity_on_positive_predicate() -> None:
    with pytest.raises(Exception):
        BehaviorKey("session_owner", "verifies_changes", "negative", "global")


def test_behavior_key_accepts_absence_predicate_positive() -> None:
    key = BehaviorKey("session_owner", "verification_omission_explicit", "positive", "global")
    assert key.polarity == "positive"


def test_behavior_key_rejects_non_global_non_workspace_scope() -> None:
    with pytest.raises(Exception):
        BehaviorKey("session_owner", "verifies_changes", "positive", "not a workspace id")


def test_behavior_key_accepts_stable_workspace_id() -> None:
    key = BehaviorKey("session_owner", "verifies_changes", "positive", "ws-abcDEF012345")
    assert key.scope == "ws-abcDEF012345"


def test_stable_scope_rejects_control_delimiter_in_locator(registry) -> None:
    cap = registry.by_id("steering.reversible-changes")
    ev = _evidence(
        "preserves_reversible_changes",
        repo="src/pkg",
        base_digest="a" * 64,
        subject="workflow_pattern",
    )
    bad = dataclasses_replace(ev, display_locator="docs/\x1fx.md")
    with pytest.raises(models.ModelError):
        stable_target_scope(cap, bad)


def test_stable_scope_rejects_traversal_in_repo(registry) -> None:
    cap = registry.by_id("lesson.verification-omission")
    ev = _evidence("verification_omission_explicit", repo="../escape")
    with pytest.raises(models.ModelError):
        stable_target_scope(cap, ev)


def test_action_key_framing_is_collision_safe() -> None:
    key = _bkey("repeats_context_setup")
    a = action_key(key, "cap", "prompt", "x\x1fy")
    b = action_key(key, "cap", "prompt\x1fx", "y")
    assert a != b


def test_duplicate_evidence_dedup_is_deterministic(registry) -> None:
    ev1 = _evidence("repeats_context_setup", supporting=2)
    ev2 = _evidence("repeats_context_setup", supporting=9)
    first = enumerate_candidates(registry, [ev1, ev2], LINUX)
    second = enumerate_candidates(registry, [ev2, ev1], LINUX)
    assert [c.action_key_value for c in first.candidates] == [
        c.action_key_value for c in second.candidates
    ]
    assert (
        first.candidates[0].supporting_session_count
        == second.candidates[0].supporting_session_count
    )


def test_wording_attempt_ceiling_bounds_fallback(registry) -> None:
    queue = models.BehaviorQueue(
        candidates=tuple(
            enumerate_candidates(registry, [_evidence("repeats_context_setup")], LINUX).candidates
        ),
        wording_attempt_ceiling=1,
    )
    attempts = list(queue.attempts())
    assert len(attempts) <= 1


def test_next_behavior_promoted_only_after_exhaustion(registry) -> None:
    plan = models.select_roots_with_queues(
        enumerate_candidates(
            registry,
            [
                _evidence("repeats_context_setup"),
                _evidence("verification_omission_explicit"),
            ],
            LINUX,
        ),
        wording_attempt_ceiling=3,
    )
    assert [q.behavior_canonical for q in plan] == sorted(
        {q.behavior_canonical for q in plan},
        key=lambda k: [p.behavior_canonical for p in plan].index(k),
    )
    assert len(plan) <= models.MAX_ROOTS


def test_cross_process_action_key_shares_persisted_workspace_and_lesson(tmp_path: Path) -> None:
    from kiro_crew.learn import Lesson, LessonStore

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "workspace_id.txt").write_text("ws-shared-9999", encoding="utf-8")
    store = LessonStore(base_dir=fixture)
    store.save(
        Lesson(
            ts="1970-01-01T00:00:00Z",
            rule="shared lesson rule",
            category="preference",
        )
    )
    script = tmp_path / "keygen.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from kiro_crew.learn import LessonStore\n"
        "from kiro_crew.personal_insights.insights_ontology import BehaviorKey\n"
        "from kiro_crew.personal_insights.insights_models import action_key, stable_target_scope\n"
        "from kiro_crew.personal_insights.insights_registry import load_registry\n"
        "fixture = Path(sys.argv[1])\n"
        "ws = (fixture / 'workspace_id.txt').read_text().strip()\n"
        "rows = LessonStore(base_dir=fixture).load_all()\n"
        "assert rows and rows[0].rule == 'shared lesson rule'\n"
        "cap = load_registry().by_id('lesson.verification-omission')\n"
        "k = BehaviorKey('session_owner', 'verification_omission_explicit', 'positive', ws)\n"
        "from kiro_crew.personal_insights.insights_models import ClaimEvidence, BurdenVector\n"
        "ev = ClaimEvidence(k, 2, 2, 'repeated', False, BurdenVector(), None, None, ws, None)\n"
        "scope = stable_target_scope(cap, ev)\n"
        "print(action_key(k, cap.capability_id, cap.cls, scope))\n",
        encoding="utf-8",
    )

    def _compute() -> str:
        ws = (fixture / "workspace_id.txt").read_text().strip()
        rows = LessonStore(base_dir=fixture).load_all()
        assert rows and rows[0].rule == "shared lesson rule"
        cap = registry_singleton().by_id("lesson.verification-omission")
        key = BehaviorKey("session_owner", "verification_omission_explicit", "positive", ws)
        ev = models.ClaimEvidence(
            key, 2, 2, "repeated", False, models.BurdenVector(), None, None, ws, None
        )
        scope = stable_target_scope(cap, ev)
        return action_key(key, cap.capability_id, cap.cls, scope)

    in_process = _compute()
    env = dict(os.environ)
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(script), str(fixture)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == in_process


def registry_singleton():
    return load_registry()


def dataclasses_replace(obj, **changes):
    import dataclasses

    return dataclasses.replace(obj, **changes)


# ── defect 12: diff hunk count/range, multi-file, control chars, output bound ──


def test_diff_hunk_range_count_mismatch_rejected() -> None:
    bad = "--- a/x\n+++ b/x\n@@ -1,5 +1,1 @@\n a\n-b\n+bb\n"
    with pytest.raises(diffmod.DiffError, match="hunk range"):
        diffmod.parse_diff(bad)


def test_diff_multi_file_rejected() -> None:
    bad = (
        "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\n" "--- a/y\n+++ b/y\n@@ -1,1 +1,1 @@\n-c\n+d\n"
    )
    with pytest.raises(diffmod.DiffError, match="multi-file"):
        diffmod.parse_diff(bad)


def test_diff_control_character_in_content_rejected() -> None:
    bad = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\x00c\n"
    with pytest.raises(diffmod.DiffError, match="control character"):
        diffmod.parse_diff(bad)


def test_diff_output_bound_enforced() -> None:
    big = "x" * 50
    base = "a\n"
    base_digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    diff = f"--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+{big}\n"
    with pytest.raises(diffmod.DiffError, match="output bound"):
        diffmod.apply_diff(base, diff, base_digest, expected_path="x", max_output_bytes=10)


# ── defect 9: action/3.0 compilation and post-wording validation ──


def test_compile_action_produces_strict_action_3_0(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("needs_capability_discovery")
    result = enumerate_candidates(registry, [ev], LINUX)
    candidate = result.candidates[0]
    compiled = action.compile_action(
        candidate,
        registry.by_id(candidate.capability_id),
        rank=1,
        claim_ids=("claim-1",),
        claim_evidence_version_ids=("ev-1",),
        title="List available capabilities",
        why="supported by accepted claims",
        why_claim_ids=("claim-1",),
        why_work_area_claim_id=None,
        evidence_coverage_state="complete",
        supporting_sessions=3,
        claim_eligible_sessions=3,
        counterexample_sessions=0,
        wording_tier="repeated",
        active_guidance_match_id="match-1",
        expected_observation="the capability list is shown",
        verification_display="run the command and read the output",
        generated_at="1970-01-01T00:00:00Z",
        expires_at="1970-01-08T00:00:00Z",
    )
    assert compiled["schema_version"] == "kiro.personal-insights.action/3.0"
    assert compiled["action_class"] == "existing_capability"
    assert compiled["artifact"]["argv"] == ["kirocrew", "app", "list"]
    assert compiled["execution_platform"] == "linux"
    assert compiled["risk"] == "copy_only"


def test_post_wording_validation_rejects_multiple_artifacts(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    with pytest.raises(action.ActionValidationError, match="one artifact"):
        action.validate_single_artifact({"content": "x", "argv": ["y"]})


def test_post_wording_validation_rejects_placeholder(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    with pytest.raises(action.ActionValidationError, match="placeholder"):
        action.validate_no_placeholder("replace <SLOT> before using")


def test_post_wording_validation_rejects_non_registry_argv(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    with pytest.raises(action.ActionValidationError, match="argv"):
        action.validate_exact_argv(["kirocrew", "learn"], ("kirocrew", "learn", "list"))
    action.validate_exact_argv(["kirocrew", "learn", "list"], ("kirocrew", "learn", "list"))


def test_local_state_action_requires_undo(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    with pytest.raises(action.ActionValidationError, match="undo"):
        action.validate_undo("local_state_proposal", undo_artifact=None)
    action.validate_undo("copy_only", undo_artifact=None)


def test_p1_applies_nothing(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    assert action.P1_APPLIES_NOTHING is True
    assert not hasattr(action, "apply_action")


# ── defect 1: full action/3.0 contract and strict validation ──


def test_compile_action_emits_every_section_14_1_field(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("needs_capability_discovery")
    result = enumerate_candidates(registry, [ev], LINUX)
    candidate = result.candidates[0]
    compiled = action.compile_action(
        candidate,
        registry.by_id(candidate.capability_id),
        rank=1,
        claim_ids=("claim-1",),
        claim_evidence_version_ids=("ev-1",),
        title="List available capabilities",
        why="supported by accepted claims",
        why_claim_ids=("claim-1",),
        why_work_area_claim_id=None,
        evidence_coverage_state="complete",
        supporting_sessions=3,
        claim_eligible_sessions=3,
        counterexample_sessions=0,
        wording_tier="repeated",
        active_guidance_match_id="match-1",
        expected_observation="the capability list is shown",
        verification_display="run the command and read the output",
        generated_at="1970-01-01T00:00:00Z",
        expires_at="1970-01-08T00:00:00Z",
    )
    required = {
        "schema_version",
        "action_id",
        "action_key",
        "claim_ids",
        "claim_evidence_version_ids",
        "behavior_key",
        "rank",
        "title",
        "action_class",
        "why_claim_ids",
        "why_work_area_claim_id",
        "why",
        "evidence",
        "active_guidance_match_id",
        "capability_id",
        "execution_platform",
        "target",
        "artifact",
        "expected_observation",
        "verification",
        "undo",
        "risk",
        "generated_at",
        "expires_at",
    }
    assert set(compiled) == required
    assert compiled["schema_version"] == "kiro.personal-insights.action/3.0"
    assert compiled["action_id"] != compiled["action_key"]
    assert set(compiled["evidence"]) == {
        "supporting_sessions",
        "claim_eligible_sessions",
        "counterexample_sessions",
        "coverage_state",
        "wording_tier",
    }
    assert set(compiled["target"]) == {
        "kind",
        "locator",
        "display_locator",
        "stable_scope",
        "exists",
        "base_digest",
    }
    assert set(compiled["artifact"]) == {"kind", "content", "structured_parameters", "argv"}
    assert set(compiled["verification"]) == {
        "kind",
        "oracle",
        "display_artifact",
        "minimum_new_sessions",
    }
    assert set(compiled["undo"]) == {"required", "scope", "artifact"}
    assert compiled["verification"]["kind"] in {
        "read_only_argv",
        "state_readback",
        "future_observation",
    }
    assert compiled["artifact"]["argv"] == ["kirocrew", "app", "list"]
    assert compiled["execution_platform"] == "linux"


def test_compile_action_rejects_missing_required_input(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("needs_capability_discovery")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError):
        action.compile_action(
            candidate, registry.by_id(candidate.capability_id), rank=1, claim_ids=()
        )


def test_compile_action_rejects_invalid_rank(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("needs_capability_discovery")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError, match="rank"):
        action.compile_action(
            candidate,
            registry.by_id(candidate.capability_id),
            rank=0,
            claim_ids=("c",),
            claim_evidence_version_ids=("e",),
            title="t",
            why="w",
            why_claim_ids=("c",),
            why_work_area_claim_id=None,
            evidence_coverage_state="complete",
            supporting_sessions=2,
            claim_eligible_sessions=2,
            counterexample_sessions=0,
            wording_tier="repeated",
            active_guidance_match_id="m",
            expected_observation="o",
            verification_display="d",
            generated_at="1970-01-01T00:00:00Z",
            expires_at="1970-01-08T00:00:00Z",
        )


def test_compile_action_rejects_expiry_before_generation(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("needs_capability_discovery")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError, match="expires"):
        action.compile_action(
            candidate,
            registry.by_id(candidate.capability_id),
            rank=1,
            claim_ids=("c",),
            claim_evidence_version_ids=("e",),
            title="t",
            why="w",
            why_claim_ids=("c",),
            why_work_area_claim_id=None,
            evidence_coverage_state="complete",
            supporting_sessions=2,
            claim_eligible_sessions=2,
            counterexample_sessions=0,
            wording_tier="repeated",
            active_guidance_match_id="m",
            expected_observation="o",
            verification_display="d",
            generated_at="1970-01-08T00:00:00Z",
            expires_at="1970-01-01T00:00:00Z",
        )


def test_p1_has_no_apply_path(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    assert action.P1_APPLIES_NOTHING is True
    assert not hasattr(action, "apply_action")


# ── defect 3: global total wording-attempt ceiling + promotion ──


def test_global_wording_ceiling_bounds_total_attempts(registry) -> None:
    result = enumerate_candidates(
        registry,
        [
            _evidence("repeats_context_setup"),
            _evidence("verification_omission_explicit"),
            _evidence("needs_capability_discovery"),
        ],
        LINUX,
    )
    plan = models.plan_wording(result, total_wording_attempt_ceiling=2)
    assert plan.total_attempts_used <= 2
    assert len(plan.selected) <= models.MAX_ROOTS


def test_all_behavior_queues_available_not_only_roots(registry) -> None:
    result = enumerate_candidates(
        registry,
        [
            _evidence("repeats_context_setup"),
            _evidence("verification_omission_explicit"),
            _evidence("needs_capability_discovery"),
        ],
        LINUX,
    )
    plan = models.plan_wording(result, total_wording_attempt_ceiling=9)
    assert len(plan.queues) == len({c.behavior_key.canonical() for c in result.candidates})


def test_next_behavior_promoted_only_after_exhaustion_global(registry) -> None:
    result = enumerate_candidates(
        registry,
        [_evidence("repeats_context_setup"), _evidence("needs_capability_discovery")],
        LINUX,
    )
    plan = models.plan_wording(result, total_wording_attempt_ceiling=9)
    order = [q.behavior_canonical for q in plan.queues]
    root_order = [r.behavior_key.canonical() for r in result.roots]
    assert order[: len(root_order)] == root_order
    assert len(plan.selected) <= models.MAX_ROOTS


# ── defect 4: ClaimEvidence validation ──


def test_claim_evidence_rejects_negative_counts() -> None:
    with pytest.raises(models.ModelError):
        models.validate_claim_evidence(_evidence("repeats_context_setup", supporting=-1))


def test_claim_evidence_rejects_supporting_above_claim_eligible() -> None:
    ev = dataclasses_replace(
        _evidence("repeats_context_setup", supporting=5),
        claim_eligible_session_count=2,
    )
    with pytest.raises(models.ModelError, match="supporting"):
        models.validate_claim_evidence(ev)


def test_claim_evidence_rejects_invalid_tier() -> None:
    ev = dataclasses_replace(_evidence("repeats_context_setup"), wording_tier="bogus")
    with pytest.raises(models.ModelError, match="tier"):
        models.validate_claim_evidence(ev)


def test_claim_evidence_requires_accepted_and_guidance_clear() -> None:
    ev = dataclasses_replace(_evidence("repeats_context_setup"), accepted=False)
    with pytest.raises(models.ModelError, match="accepted"):
        models.validate_claim_evidence(ev)
    ev2 = dataclasses_replace(_evidence("repeats_context_setup"), guidance_cleared=False)
    with pytest.raises(models.ModelError, match="guidance"):
        models.validate_claim_evidence(ev2)
    ev3 = dataclasses_replace(_evidence("repeats_context_setup"), conflicts_with_disposition=True)
    with pytest.raises(models.ModelError, match="conflict"):
        models.validate_claim_evidence(ev3)


def test_claim_evidence_steering_requires_workspace_and_locator() -> None:
    ev = _evidence(
        "preserves_reversible_changes",
        repo="src/pkg",
        base_digest="a" * 64,
        subject="workflow_pattern",
    )
    bad = dataclasses_replace(ev, workspace_id="")
    with pytest.raises(models.ModelError, match="workspace"):
        models.validate_steering_target(bad)


# ── defect 5: server-derived platform ──


def test_current_runtime_platform_is_server_derived() -> None:
    from kiro_crew.personal_insights import insights_platform as platform

    runtime = platform.current_runtime_platform()
    assert runtime.platform in platform.SERVER_PLATFORMS


def test_current_runtime_platform_injection_for_tests_only() -> None:
    from kiro_crew.personal_insights import insights_platform as platform

    runtime = platform.current_runtime_platform(_sys_platform="darwin")
    assert runtime.platform == "darwin"
    with pytest.raises(platform.PlatformError):
        platform.current_runtime_platform(_sys_platform="plan9-unknown")


# ── defect 6: ontology closed rule enums + incompatible-evidence abstention ──


def test_ontology_rules_are_closed_enums_not_placeholders() -> None:
    from kiro_crew.personal_insights import insights_ontology as onto

    for code, decl in onto.PREDICATE_DECLARATIONS.items():
        assert decl.opportunity_rule in onto.OPPORTUNITY_RULES
        assert decl.observability_rule in onto.OBSERVABILITY_RULES
        assert decl.support_rule in onto.SUPPORT_RULES
        assert decl.counterexample_rule in onto.COUNTEREXAMPLE_RULES
        assert decl.completeness_requirement in onto.COMPLETENESS_RULES
        assert "<code>" not in decl.opportunity_rule
        assert not decl.opportunity_rule.endswith(".opportunity")


def test_incompatible_evidence_abstains() -> None:
    from kiro_crew.personal_insights import insights_ontology as onto

    decl = onto.PREDICATE_DECLARATIONS["verifies_changes"]
    assert onto.evidence_abstains(decl, available_features=("messages",)) is True
    assert onto.evidence_abstains(decl, available_features=decl.compatible_features) is False


# ── panel round: defect-closure behavioral tests ──


@pytest.mark.parametrize(
    "predicate, subject",
    [
        ("repeats_context_setup", SUBJECT),
        ("verification_omission_explicit", SUBJECT),
        ("needs_capability_discovery", SUBJECT),
        ("preserves_reversible_changes", "workflow_pattern"),
    ],
)
def test_validate_claim_evidence_rejects_single_tier_all_classes(predicate, subject) -> None:
    ev = _evidence(predicate, tier=models.TIER_SINGLE, subject=subject)
    with pytest.raises(models.ModelError, match="tier"):
        models.validate_claim_evidence(ev)


def test_validate_claim_evidence_only_valid_tiers_pass() -> None:
    for tier in (models.TIER_REPEATED, models.TIER_RECURRING, models.TIER_USUALLY):
        models.validate_claim_evidence(_evidence("repeats_context_setup", tier=tier))
    for bad in (models.TIER_SINGLE, "bogus", ""):
        with pytest.raises(models.ModelError):
            models.validate_claim_evidence(
                dataclasses_replace(_evidence("repeats_context_setup"), wording_tier=bad)
            )


def test_enumerate_enforces_validate_claim_evidence_single_tier(registry) -> None:
    ev = _evidence("repeats_context_setup", tier=models.TIER_SINGLE)
    result = enumerate_candidates(registry, [ev], LINUX)
    assert not result.candidates
    assert result.rejected and result.rejected[0].reason == models.REJECT_SINGLE_TIER


def test_enumerate_enforces_validate_claim_evidence_not_accepted(registry) -> None:
    ev = dataclasses_replace(_evidence("repeats_context_setup"), accepted=False)
    result = enumerate_candidates(registry, [ev], LINUX)
    assert not result.candidates
    assert result.rejected and "accepted" in result.rejected[0].reason


def test_enumerate_enforces_guidance_and_conflict(registry) -> None:
    not_cleared = dataclasses_replace(_evidence("repeats_context_setup"), guidance_cleared=False)
    r1 = enumerate_candidates(registry, [not_cleared], LINUX)
    assert not r1.candidates and "guidance" in r1.rejected[0].reason
    conflict = dataclasses_replace(
        _evidence("repeats_context_setup"), conflicts_with_disposition=True
    )
    r2 = enumerate_candidates(registry, [conflict], LINUX)
    assert not r2.candidates and "conflict" in r2.rejected[0].reason


def test_enumerate_enforces_count_invariant(registry) -> None:
    ev = dataclasses_replace(
        _evidence("repeats_context_setup", supporting=5), claim_eligible_session_count=2
    )
    result = enumerate_candidates(registry, [ev], LINUX)
    assert not result.candidates


def test_behavior_queue_is_exhausted_at_zero_and_one() -> None:
    cands = enumerate_candidates(
        registry_singleton(), [_evidence("repeats_context_setup")], LINUX
    ).candidates
    q = models.BehaviorQueue(candidates=cands, wording_attempt_ceiling=1)
    assert q.is_exhausted(0) is False
    assert q.is_exhausted(1) is True


def _registry_with_two_prompt_entries():
    import dataclasses

    reg = registry_singleton()
    base = reg.by_id("prompt.context-setup-reuse")
    alt = dataclasses.replace(
        base,
        capability_id="prompt.context-setup-reuse-alt",
        selection_priority=11,
    )
    return dataclasses.replace(reg, capabilities=reg.capabilities + (alt,))


def test_executable_fallback_advances_same_behavior_before_next() -> None:
    reg = _registry_with_two_prompt_entries()
    result = enumerate_candidates(
        reg,
        [_evidence("repeats_context_setup"), _evidence("needs_capability_discovery")],
        LINUX,
    )
    attempted: list[str] = []

    def validator(candidate) -> bool:
        attempted.append(candidate.capability_id)
        return candidate.capability_id == "prompt.context-setup-reuse-alt"

    plan = models.execute_post_wording(result, total_wording_attempt_ceiling=9, validator=validator)
    first_behavior = result.roots[0].behavior_key.canonical()
    assert attempted[0] == "prompt.context-setup-reuse"
    assert attempted[1] == "prompt.context-setup-reuse-alt"
    assert plan.selected[0].capability_id == "prompt.context-setup-reuse-alt"
    assert plan.selected[0].behavior_key.canonical() == first_behavior


def test_executable_fallback_promotes_next_behavior_after_root_exhaustion() -> None:
    reg = registry_singleton()
    evs = [
        _evidence("repeats_context_setup"),
        _evidence("verification_omission_explicit"),
        _evidence("needs_capability_discovery"),
    ]
    result = enumerate_candidates(reg, evs, LINUX)

    def validator(candidate) -> bool:
        return candidate.behavior_key.predicate_code != "repeats_context_setup"

    plan = models.execute_post_wording(result, total_wording_attempt_ceiling=9, validator=validator)
    chosen = {c.behavior_key.predicate_code for c in plan.selected}
    assert "repeats_context_setup" not in chosen
    assert len(plan.selected) <= models.MAX_ROOTS
    assert len(plan.selected) >= 1


def test_executable_fallback_global_ceiling_caps_total_attempts() -> None:
    reg = _registry_with_two_prompt_entries()
    result = enumerate_candidates(
        reg,
        [_evidence("repeats_context_setup"), _evidence("needs_capability_discovery")],
        LINUX,
    )
    attempts: list[str] = []

    def validator(candidate) -> bool:
        attempts.append(candidate.capability_id)
        return False

    plan = models.execute_post_wording(result, total_wording_attempt_ceiling=2, validator=validator)
    assert len(attempts) == 2
    assert plan.total_attempts_used == 2
    assert plan.selected == ()


def test_executable_fallback_emits_at_most_three_valid_actions() -> None:
    reg = registry_singleton()
    evs = [
        _evidence("repeats_context_setup"),
        _evidence("verification_omission_explicit"),
        _evidence("needs_capability_discovery"),
        _evidence(
            "preserves_reversible_changes",
            repo="src/pkg",
            base_digest="a" * 64,
            subject="workflow_pattern",
        ),
    ]
    result = enumerate_candidates(reg, evs, LINUX)
    plan = models.execute_post_wording(
        result, total_wording_attempt_ceiling=20, validator=lambda c: True
    )
    assert len(plan.selected) == models.MAX_ROOTS


def test_select_roots_with_queues_keeps_all_ranked_queues() -> None:
    reg = registry_singleton()
    evs = [
        _evidence("repeats_context_setup"),
        _evidence("verification_omission_explicit"),
        _evidence("needs_capability_discovery"),
        _evidence(
            "preserves_reversible_changes",
            repo="src/pkg",
            base_digest="a" * 64,
            subject="workflow_pattern",
        ),
    ]
    result = enumerate_candidates(reg, evs, LINUX)
    queues = models.select_roots_with_queues(result, wording_attempt_ceiling=9)
    distinct = {c.behavior_key.canonical() for c in result.candidates}
    assert len(queues) == len(distinct)


def test_burden_vector_precedence() -> None:
    reg = registry_singleton()
    high = _evidence("repeats_context_setup", burden=models.BurdenVector(verification_failures=5))
    low = _evidence(
        "needs_capability_discovery", burden=models.BurdenVector(verification_failures=0)
    )
    result = enumerate_candidates(reg, [low, high], LINUX)
    assert result.candidates[0].burden.verification_failures == 5


def test_burden_precedence_is_below_support_count() -> None:
    reg = registry_singleton()
    big_support_low_burden = _evidence(
        "repeats_context_setup", supporting=9, burden=models.BurdenVector()
    )
    small_support_high_burden = _evidence(
        "needs_capability_discovery", supporting=2, burden=models.BurdenVector(tool_errors=9)
    )
    result = enumerate_candidates(reg, [small_support_high_burden, big_support_low_burden], LINUX)
    assert result.candidates[0].supporting_session_count == 9


# ── action compile: content/params and discriminated oracle ──


def test_compile_action_rejects_missing_text_content(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("repeats_context_setup")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError, match="content"):
        action.compile_action(
            candidate,
            registry.by_id(candidate.capability_id),
            rank=1,
            claim_ids=("c",),
            claim_evidence_version_ids=("e",),
            title="t",
            why="w",
            why_claim_ids=("c",),
            why_work_area_claim_id=None,
            evidence_coverage_state="complete",
            supporting_sessions=2,
            claim_eligible_sessions=2,
            counterexample_sessions=0,
            wording_tier="repeated",
            active_guidance_match_id="m",
            expected_observation="o",
            verification_display="d",
            generated_at="1970-01-01T00:00:00Z",
            expires_at="1970-01-08T00:00:00Z",
            content=None,
        )


def test_compile_action_rejects_missing_structured_params(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("verification_omission_explicit")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError, match="structured"):
        action.compile_action(
            candidate,
            registry.by_id(candidate.capability_id),
            rank=1,
            claim_ids=("c",),
            claim_evidence_version_ids=("e",),
            title="t",
            why="w",
            why_claim_ids=("c",),
            why_work_area_claim_id=None,
            evidence_coverage_state="complete",
            supporting_sessions=2,
            claim_eligible_sessions=2,
            counterexample_sessions=0,
            wording_tier="repeated",
            active_guidance_match_id="m",
            expected_observation="o",
            verification_display="d",
            undo_artifact="undo text",
            generated_at="1970-01-01T00:00:00Z",
            expires_at="1970-01-08T00:00:00Z",
            structured_parameters=None,
        )


def test_compile_action_read_only_argv_oracle_drives_evaluator(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action
    from kiro_crew.personal_insights import insights_verification as verify

    ev = _evidence("needs_capability_discovery")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    compiled = action.compile_action(
        candidate,
        registry.by_id(candidate.capability_id),
        rank=1,
        claim_ids=("c",),
        claim_evidence_version_ids=("e",),
        title="t",
        why="w",
        why_claim_ids=("c",),
        why_work_area_claim_id=None,
        evidence_coverage_state="complete",
        supporting_sessions=2,
        claim_eligible_sessions=2,
        counterexample_sessions=0,
        wording_tier="repeated",
        active_guidance_match_id="m",
        expected_observation="o",
        verification_display="d",
        generated_at="1970-01-01T00:00:00Z",
        expires_at="1970-01-08T00:00:00Z",
    )
    oracle = compiled["verification"]["oracle"]
    assert oracle
    assert oracle["kind"] == "read_only_argv"
    assert oracle["argv"] == ["kirocrew", "app", "list"]
    assert "expected_exit" in oracle and "expected_output_class" in oracle
    built = verify.build_read_only_argv(
        tuple(oracle["argv"]), oracle["expected_exit"], oracle["expected_output_class"]
    )
    assert (
        verify.read_only_argv_passes(
            built, oracle["expected_exit"], oracle["expected_output_class"]
        )
        is True
    )
    assert (
        verify.read_only_argv_passes(
            built, oracle["expected_exit"] + 1, oracle["expected_output_class"]
        )
        is False
    )


# ── diff: duplicate zero-count insertion hunks ──


def test_duplicate_zero_count_insertion_hunks_rejected() -> None:
    bad = (
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1,0 +1,1 @@\n"
        "+inserted a\n"
        "@@ -1,0 +2,1 @@\n"
        "+inserted b\n"
    )
    with pytest.raises(diffmod.DiffError, match="overlapping|duplicate"):
        diffmod.parse_diff(bad)


def test_single_zero_count_insertion_applies_and_reverses() -> None:
    base = "line one\nline two\n"
    base_digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n line one\n+inserted\n line two\n"
    modified = diffmod.apply_diff(base, diff, base_digest, expected_path="x")
    assert modified == "line one\ninserted\nline two\n"
    restored = diffmod.reverse_apply_diff(modified, diff, expected_path="x")
    assert hashlib.sha256(restored.encode("utf-8")).hexdigest() == base_digest


# ── diff output bound N-1 / N boundary ──


def test_diff_output_bound_boundary() -> None:
    base = "a\n"
    base_digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    diff = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+abcd\n"
    result_bytes = len("abcd\n".encode("utf-8"))
    with pytest.raises(diffmod.DiffError, match="output bound"):
        diffmod.apply_diff(
            base, diff, base_digest, expected_path="x", max_output_bytes=result_bytes - 1
        )
    ok = diffmod.apply_diff(
        base, diff, base_digest, expected_path="x", max_output_bytes=result_bytes
    )
    assert ok == "abcd\n"


# ── platform cache_identity parse ──


def test_parse_cache_identity_exact() -> None:
    from kiro_crew.personal_insights import insights_platform as platform

    plat, identity = platform.parse_cache_identity("linux\x1fcap-id")
    assert plat == "linux"
    assert identity == "cap-id"


def test_parse_cache_identity_fails_closed() -> None:
    from kiro_crew.personal_insights import insights_platform as platform

    for bad in ("no-separator", "plan9\x1fx", "linux\x1fa\x1fb", "\x1fcap", "linux\x1f"):
        with pytest.raises(platform.PlatformError):
            platform.parse_cache_identity(bad)


# ── cross-process key: PYTHONPATH replaced, not appended ──


def test_cross_process_action_key_replaces_pythonpath(tmp_path: Path) -> None:
    key = _bkey("verification_omission_explicit")
    in_process = action_key(
        key,
        "lesson.verification-omission",
        "lesson_proposal",
        "lesson_scope\x1fglobal\x1fno_repository_scope",
    )
    script = tmp_path / "keygen.py"
    script.write_text(
        "from kiro_crew.personal_insights.insights_ontology import BehaviorKey\n"
        "from kiro_crew.personal_insights.insights_models import action_key\n"
        "k = BehaviorKey('session_owner', 'verification_omission_explicit', 'positive', 'global')\n"
        "print(action_key(k, 'lesson.verification-omission', 'lesson_proposal',"
        " 'lesson_scope\\x1fglobal\\x1fno_repository_scope'))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = repo_src
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == in_process


# ── steering: selection assertion from the owned surface (no shared edit) ──


def test_related_targets_selects_personal_insights_not_unrelated_session() -> None:
    import importlib

    script_dir = str(Path(__file__).resolve().parents[2] / "scripts")
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    selector = importlib.import_module("run_scoped_tests")
    targets, _verdict = selector.related_targets(
        "backend", ["src/kiro_crew/personal_insights/__init__.py"]
    )
    assert "test/personal_insights/test_actions.py" in targets
    assert "test/test_session.py" not in targets


# ── final parent round: diff target binding, oracle inputs, lessons applies ──


_TGT_BASE = "line one\nline two\nline three\n"
_TGT_BASE_DIGEST = hashlib.sha256(_TGT_BASE.encode("utf-8")).hexdigest()
_TGT_DIFF = (
    "--- a/.kiro/steering/x.md\n"
    "+++ b/.kiro/steering/x.md\n"
    "@@ -1,3 +1,3 @@\n"
    " line one\n"
    "-line two\n"
    "+line two changed\n"
    " line three\n"
)


def test_apply_diff_requires_matching_expected_path() -> None:
    modified = diffmod.apply_diff(
        _TGT_BASE, _TGT_DIFF, _TGT_BASE_DIGEST, expected_path=".kiro/steering/x.md"
    )
    assert modified == "line one\nline two changed\nline three\n"


def test_apply_diff_rejects_wrong_target_with_correct_base_digest() -> None:
    with pytest.raises(diffmod.DiffError, match="target"):
        diffmod.apply_diff(
            _TGT_BASE, _TGT_DIFF, _TGT_BASE_DIGEST, expected_path=".kiro/steering/other.md"
        )


def test_apply_diff_normalizes_expected_path() -> None:
    modified = diffmod.apply_diff(
        _TGT_BASE, _TGT_DIFF, _TGT_BASE_DIGEST, expected_path="./.kiro/steering/x.md"
    )
    assert modified == "line one\nline two changed\nline three\n"


def test_reverse_apply_diff_requires_matching_expected_path() -> None:
    modified = diffmod.apply_diff(
        _TGT_BASE, _TGT_DIFF, _TGT_BASE_DIGEST, expected_path=".kiro/steering/x.md"
    )
    restored = diffmod.reverse_apply_diff(modified, _TGT_DIFF, expected_path=".kiro/steering/x.md")
    assert hashlib.sha256(restored.encode("utf-8")).hexdigest() == _TGT_BASE_DIGEST


def test_reverse_apply_diff_rejects_wrong_target() -> None:
    modified = diffmod.apply_diff(
        _TGT_BASE, _TGT_DIFF, _TGT_BASE_DIGEST, expected_path=".kiro/steering/x.md"
    )
    with pytest.raises(diffmod.DiffError, match="target"):
        diffmod.reverse_apply_diff(modified, _TGT_DIFF, expected_path=".kiro/steering/other.md")


# ── defect 3: no zero/default oracle inputs ──


def _compile_kwargs(**overrides):
    base = dict(
        rank=1,
        claim_ids=("c",),
        claim_evidence_version_ids=("e",),
        title="t",
        why="w",
        why_claim_ids=("c",),
        why_work_area_claim_id=None,
        evidence_coverage_state="complete",
        supporting_sessions=2,
        claim_eligible_sessions=2,
        counterexample_sessions=0,
        wording_tier="repeated",
        active_guidance_match_id="m",
        expected_observation="o",
        verification_display="d",
        generated_at="1970-01-01T00:00:00Z",
        expires_at="1970-01-08T00:00:00Z",
    )
    base.update(overrides)
    return base


def test_future_observation_requires_explicit_minimum(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("repeats_context_setup")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError, match="minimum_new_sessions"):
        action.compile_action(
            candidate,
            registry.by_id(candidate.capability_id),
            content="use this prompt",
            **_compile_kwargs(),
        )


def test_future_observation_positive_compilation(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("repeats_context_setup")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    compiled = action.compile_action(
        candidate,
        registry.by_id(candidate.capability_id),
        content="use this prompt",
        future_observation_minimum_new_sessions=3,
        **_compile_kwargs(),
    )
    assert compiled["verification"]["kind"] == "future_observation"
    assert compiled["verification"]["minimum_new_sessions"] == 3
    assert compiled["verification"]["oracle"]["minimum_new_sessions"] == 3
    assert 0 not in (compiled["verification"]["minimum_new_sessions"],)


def test_future_observation_rejects_nonpositive_minimum(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("repeats_context_setup")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError):
        action.compile_action(
            candidate,
            registry.by_id(candidate.capability_id),
            content="use this prompt",
            future_observation_minimum_new_sessions=0,
            **_compile_kwargs(),
        )


def test_state_readback_lesson_derives_digest_from_artifact(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action
    from kiro_crew.personal_insights.insights_canonical import canonical_lesson_digest

    ev = _evidence("verification_omission_explicit")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    params = {
        "rule": "verify before claiming",
        "category": "preference",
        "negative": None,
        "repo_scope": None,
        "applies": "always",
    }
    compiled = action.compile_action(
        candidate,
        registry.by_id(candidate.capability_id),
        structured_parameters=params,
        undo_artifact="remove the lesson",
        **_compile_kwargs(),
    )
    oracle = compiled["verification"]["oracle"]
    assert oracle["kind"] == "state_readback"
    assert oracle["target_kind"] == "lesson"
    assert oracle["expected_digest"] == canonical_lesson_digest(params)
    assert oracle["expected_digest"] != "0" * 64


def test_state_readback_steering_requires_explicit_digest(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence(
        "preserves_reversible_changes",
        repo="src/pkg",
        base_digest="a" * 64,
        subject="workflow_pattern",
    )
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    with pytest.raises(action.ActionValidationError, match="expected_digest"):
        action.compile_action(
            candidate,
            registry.by_id(candidate.capability_id),
            content=_TGT_DIFF,
            undo_artifact="reverse the diff",
            **_compile_kwargs(),
        )
    compiled = action.compile_action(
        candidate,
        registry.by_id(candidate.capability_id),
        content=_TGT_DIFF,
        undo_artifact="reverse the diff",
        state_readback_expected_digest="b" * 64,
        **_compile_kwargs(),
    )
    assert compiled["verification"]["oracle"]["expected_digest"] == "b" * 64


def test_no_zero_placeholder_in_future_minimum_sessions(registry) -> None:
    from kiro_crew.personal_insights import insights_action as action

    ev = _evidence("repeats_context_setup")
    candidate = enumerate_candidates(registry, [ev], LINUX).candidates[0]
    compiled = action.compile_action(
        candidate,
        registry.by_id(candidate.capability_id),
        content="use this prompt",
        future_observation_minimum_new_sessions=2,
        **_compile_kwargs(),
    )
    assert compiled["verification"]["minimum_new_sessions"] >= 1


def test_lessons_digest_reflects_applies(tmp_path: Path) -> None:
    from kiro_crew.learn import Lesson, LessonStore

    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain = LessonStore(base_dir=plain_dir)
    plain.save(Lesson(ts="1970-01-01T00:00:00Z", rule="r", category="preference"))

    applied_dir = tmp_path / "applied"
    applied_dir.mkdir()
    applied = LessonStore(base_dir=applied_dir)
    applied.save(
        Lesson(ts="1970-01-01T00:00:00Z", rule="r", category="preference", applies="always")
    )

    assert _lessons_digest(plain) != _lessons_digest(applied)
