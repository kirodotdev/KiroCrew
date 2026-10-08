"""Work-ledger enforcement for app-contributed acceptance verdicts."""

from __future__ import annotations

import pytest

from kiro_crew import work_ledger as wl
from kiro_crew.work_vocab import canonical_json_digest

CONDUCTOR = "chat-provider-conductor"
ACCEPTANCE = {
    "kind": "release-app:release-ready",
    "input": {"change_id": 7, "environment": "test"},
}


def _item(acceptance=None) -> str:
    wl.ensure_conductor(CONDUCTOR, goal="ship")
    result = wl.apply_conductor_action(
        CONDUCTOR,
        "create",
        title="release",
        acceptance=ACCEPTANCE if acceptance is None else acceptance,
    )
    return result["item"].item_id


def _evaluation(acceptance=ACCEPTANCE) -> dict[str, str]:
    return {
        "provider": "release-app",
        "kind": "release-app:release-ready",
        "version": "1.2.3",
        "manifest_digest": "a" * 64,
        "backend_generation": "b" * 64,
        "acceptance_digest": canonical_json_digest(acceptance),
        "authority": "trusted-app",
        "endpoint": "acceptance/release-ready",
        "evidence": "all checks green",
    }


def test_namespaced_acceptance_is_concrete_only_in_the_exact_safe_shape() -> None:
    assert wl.is_acceptance_concrete(ACCEPTANCE) is True
    assert wl.is_acceptance_concrete({"kind": ACCEPTANCE["kind"], "input": {}}) is True
    assert wl.is_acceptance_concrete({"kind": ACCEPTANCE["kind"]}) is False
    assert wl.is_acceptance_concrete({**ACCEPTANCE, "command": "release"}) is False
    assert wl.is_acceptance_concrete({"kind": "release-app:bad_kind", "input": {}}) is False


def test_a_caller_cannot_write_a_passing_contributed_verdict_or_accept_it() -> None:
    item_id = _item()
    with pytest.raises(wl.WorkLedgerError) as verdict_error:
        wl.apply_conductor_action(
            CONDUCTOR,
            "verdict",
            item_id=item_id,
            verdict="pass",
        )
    assert verdict_error.value.code == wl.CODE_PROVIDER_VERDICT_REQUIRED

    with pytest.raises(wl.WorkLedgerError) as close_error:
        wl.apply_conductor_action(
            CONDUCTOR,
            "close",
            item_id=item_id,
            state="accepted",
        )
    assert close_error.value.code == wl.CODE_PROVIDER_VERDICT_REQUIRED
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.state == "open" and item.verdict is None


def test_host_evaluation_records_provenance_and_unlocks_an_accepted_close() -> None:
    item_id = _item()
    result = wl.apply_provider_evaluation(
        CONDUCTOR,
        item_id,
        expected_acceptance=ACCEPTANCE,
        verdict="pass",
        evaluation=_evaluation(),
    )
    item = result["item"]
    assert item.verdict == "pass"
    assert item.evaluation["provider"] == "release-app"
    assert item.evaluation["kind"] == ACCEPTANCE["kind"]
    assert item.evaluation["acceptance_digest"] == canonical_json_digest(ACCEPTANCE)
    assert item.evaluation["backend_generation"] == "b" * 64
    assert item.evaluation["evaluated_at"]

    closed = wl.apply_conductor_action(
        CONDUCTOR,
        "close",
        item_id=item_id,
        state="accepted",
    )["item"]
    assert closed.state == "accepted"


def test_a_stale_evaluation_cannot_land_after_the_acceptance_changes() -> None:
    item_id = _item()
    changed = {
        "kind": ACCEPTANCE["kind"],
        "input": {"change_id": 8, "environment": "test"},
    }
    wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance=changed)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_provider_evaluation(
            CONDUCTOR,
            item_id,
            expected_acceptance=ACCEPTANCE,
            verdict="pass",
            evaluation=_evaluation(),
        )
    assert caught.value.code == wl.CODE_ACCEPTANCE_CHANGED
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.verdict is None and item.evaluation == {}


def test_a_python_equal_numeric_change_still_invalidates_evaluation() -> None:
    stored = {"kind": ACCEPTANCE["kind"], "input": {"change_id": 1}}
    evaluated = {"kind": ACCEPTANCE["kind"], "input": {"change_id": 1.0}}
    assert stored == evaluated
    assert canonical_json_digest(stored) != canonical_json_digest(evaluated)
    item_id = _item(stored)

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_provider_evaluation(
            CONDUCTOR,
            item_id,
            expected_acceptance=evaluated,
            verdict="pass",
            evaluation=_evaluation(evaluated),
        )
    assert caught.value.code == wl.CODE_ACCEPTANCE_CHANGED


def test_acceptance_promotion_invalidates_an_existing_provider_pass() -> None:
    item_id = _item()
    wl.apply_provider_evaluation(
        CONDUCTOR,
        item_id,
        expected_acceptance=ACCEPTANCE,
        verdict="pass",
        evaluation=_evaluation(),
    )
    changed = {
        "kind": ACCEPTANCE["kind"],
        "input": {"change_id": 9, "environment": "production"},
    }
    updated = wl.apply_acceptance_update(CONDUCTOR, item_id, acceptance=changed)["item"]
    assert updated.verdict is None
    assert updated.evaluation == {}
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    assert caught.value.code == wl.CODE_PROVIDER_VERDICT_REQUIRED


def test_non_passing_manual_rulings_remain_available_but_clear_provider_proof() -> None:
    item_id = _item()
    result = wl.apply_conductor_action(
        CONDUCTOR,
        "verdict",
        item_id=item_id,
        verdict="fail",
        fails=1,
    )["item"]
    assert result.verdict == "fail"
    assert result.fails == 1
    assert result.evaluation == {}


def test_built_in_acceptance_behavior_is_unchanged() -> None:
    item_id = _item({"kind": "human_approval"})
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pass")
    closed = wl.apply_conductor_action(
        CONDUCTOR,
        "close",
        item_id=item_id,
        state="accepted",
    )["item"]
    assert closed.state == "accepted"


def test_a_passing_provider_verdict_requires_complete_string_provenance() -> None:
    item_id = _item()
    incomplete = _evaluation()
    incomplete["authority"] = ""
    with pytest.raises(wl.WorkLedgerError) as missing:
        wl.apply_provider_evaluation(
            CONDUCTOR,
            item_id,
            expected_acceptance=ACCEPTANCE,
            verdict="pass",
            evaluation=incomplete,
        )
    assert missing.value.code == wl.CODE_INVALID_VALUE

    legacy = _evaluation()
    legacy.pop("backend_generation")
    with pytest.raises(wl.WorkLedgerError) as missing_generation:
        wl.apply_provider_evaluation(
            CONDUCTOR,
            item_id,
            expected_acceptance=ACCEPTANCE,
            verdict="pass",
            evaluation=legacy,
        )
    assert missing_generation.value.code == wl.CODE_INVALID_VALUE

    malformed: dict[str, object] = dict(_evaluation())
    malformed["version"] = 123
    with pytest.raises(wl.WorkLedgerError) as wrong_type:
        wl.apply_provider_evaluation(
            CONDUCTOR,
            item_id,
            expected_acceptance=ACCEPTANCE,
            verdict="pass",
            evaluation=malformed,
        )
    assert wrong_type.value.code == wl.CODE_INVALID_VALUE


def test_malformed_or_extended_stored_proof_cannot_unlock_acceptance() -> None:
    item_id = _item()
    item = wl.apply_provider_evaluation(
        CONDUCTOR,
        item_id,
        expected_acceptance=ACCEPTANCE,
        verdict="pass",
        evaluation=_evaluation(),
    )["item"]
    valid = dict(item.evaluation)

    item.evaluation["manifest_digest"] = "not-a-digest"
    assert wl._provider_pass_matches(item) is False
    item.evaluation = dict(valid)
    item.evaluation["evaluated_at"] = "not-a-time"
    assert wl._provider_pass_matches(item) is False
    item.evaluation = dict(valid)
    item.evaluation["extra"] = "forged"
    assert wl._provider_pass_matches(item) is False
    item.evaluation = dict(valid)
    item.acceptance = {
        "kind": ACCEPTANCE["kind"],
        "input": {"change_id": float("inf")},
    }
    item.evaluation["acceptance_digest"] = ""
    assert canonical_json_digest(item.acceptance) == ""
    assert wl._provider_pass_matches(item) is False
