import pytest

from kiro_crew.apps.builtins.code_review_sage.sage_lib import results, review_driver, store


def _record(**fields):
    return {
        "schema": "code-review-sage-result",
        "version": 1,
        "change_id": "CR-1",
        "platform": "github",
        "repo_identity": "github.com/o/r",
        "revision": "a" * 40,
        "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
        **fields,
    }


@pytest.mark.parametrize("pending", [[None], {"key": "design"}, "invalid"])
def test_worker_saved_comments_without_intent_are_rebuilt(tmp_path, monkeypatch, pending):
    store.ensure_layout(tmp_path)
    record = _record(pending_comments=pending)
    results.write_result(record, tmp_path)
    monkeypatch.setattr(
        review_driver,
        "_probe_delivery",
        lambda *args: (review_driver.DeliveryProbe.ABSENT, "", ""),
    )
    dispatched = []

    def dispatch(task, timeout=0):
        dispatched.append(task)
        return {"ok": True}

    out = review_driver.post_recorded(
        "CR-1",
        "https://github.com/o/r/pull/1",
        dispatch=dispatch,
        root=tmp_path,
        confirm=lambda *args: "12",
    )

    assert out["post_ok"]
    assert len(dispatched) == 1
    saved = results.read_result("CR-1", tmp_path)
    assert saved["pending_comments"] == review_driver.pipeline.build_pending_comments(record)
    assert saved["delivery_intent"]["state"] == "confirmed"


@pytest.mark.parametrize("pending", [[None], [{"key": "design"}, None], {}, "invalid"])
@pytest.mark.parametrize("state", ["prepared", "attempting", "indeterminate", "confirmed"])
def test_retained_intent_with_malformed_comments_returns_error(
    tmp_path, monkeypatch, pending, state
):
    store.ensure_layout(tmp_path)
    intent = {"operation_id": "a" * 32, "state": state, "selected_keys": ["design"]}
    results.write_result(_record(pending_comments=pending, delivery_intent=intent), tmp_path)

    def unexpected_call(*args, **kwargs):
        pytest.fail("Malformed retained entries must not reach GitHub or the poster")

    monkeypatch.setattr(review_driver, "_probe_delivery", unexpected_call)
    for _ in range(2):
        out = review_driver.post_recorded(
            "CR-1", "https://github.com/o/r/pull/1", dispatch=unexpected_call, root=tmp_path
        )
        assert not out["post_ok"]
        assert "list of objects" in out["post_error"]
    saved = results.read_result("CR-1", tmp_path)
    assert saved["pending_comments"] == pending
    assert saved["delivery_intent"]["operation_id"] == intent["operation_id"]
    assert saved["delivery_intent"]["state"] == "indeterminate"
