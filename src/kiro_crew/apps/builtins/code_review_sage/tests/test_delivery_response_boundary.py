"""Recovery trusts driver intent, never reviewer or poster delivery metadata."""

import copy
import json
import re

import pytest
from sage_lib import results
from sage_lib import review_driver as D
from sage_lib import store

from .test_post_comments import _record, _response


@pytest.mark.parametrize("run_id", [None, "scoped"])
def test_stored_recovery_rebuilds_redaction_before_dispatch(tmp_path, monkeypatch, run_id):
    link = "https://github.com/o/r/pull/1"
    cid = D._cid(link)
    record = _record(cid, red=1, yellow=0)
    record["pending_comments"] = D.pipeline.build_pending_comments(record)
    record["pending_comments"][0]["body"] = "ghp_" + "A" * 36
    payload = {"body": "ghp_" + "A" * 36, "commit_id": "1", "comments": []}
    record["github_review_payload"] = payload
    record["delivery_intent"] = {
        "state": "prepared",
        "operation_id": "a" * 32,
        "payload_digest": D._payload_digest(D._outbound_payload(payload, "a" * 32)),
    }
    results.write_result(record, tmp_path, run_id)
    dispatch = pytest.fail
    outcome = D.post_recorded(cid, link, root=tmp_path, run_id=run_id, dispatch=dispatch)
    assert not outcome["post_ok"]
    assert "redacted saved selection" in outcome["post_error"]


@pytest.mark.parametrize("run_id", [None, "scoped"])
@pytest.mark.parametrize("followup", [False, True, "failed", "invalid"])
@pytest.mark.parametrize("state", ["prepared", "attempting", "indeterminate"])
def test_worker_intent_cannot_bypass_redaction(tmp_path, monkeypatch, run_id, followup, state):
    root = tmp_path / "app"
    link = "https://github.com/o/r/pull/1"
    cid = D._cid(link)
    secret = "ghp_" + "A" * 36
    sent = []
    remote: list[dict] = []

    def github(path, **kwargs):
        if path.endswith("/reviews"):
            return remote
        if path.endswith("/comments"):
            return remote[0]["comments"]
        if path.endswith("/pulls/1"):
            return [{"head": {"sha": "1"}, "base": {"sha": "base"}}]
        raise AssertionError(path)

    monkeypatch.setattr(D.discovery, "run_gh_json", github)
    monkeypatch.setattr(D.discovery, "current_login", lambda **kw: "sage")
    base = _record(cid, red=1, yellow=0)
    base["findings"][0]["observation"] = f"exposed token {secret}"
    base["coverage_complete"] = not followup

    def dispatch(task, timeout):
        if "pre-redacted DRAFT review comments" in task:
            sent.append(copy.deepcopy(results.read_result(cid, root)))
            return {"ok": False, "error": "response lost"}
        initial = "SINGLE thorough pass" in task
        record = copy.deepcopy(base)
        record["coverage_complete"] = not (initial and followup)
        if not followup or not initial:
            record["pending_comments"] = D.pipeline.build_pending_comments(record)
            payload = {"body": secret, "commit_id": "1", "comments": []}
            record["github_review_payload"] = payload
            # A matching digest makes the public-head bypass reachable, rather than
            # testing rejection of an accidentally malformed attacker payload.
            record["delivery_intent"] = {
                "operation_id": "a" * 32,
                "target": link,
                "revision": "1",
                "state": state,
                "payload_digest": D._payload_digest(payload),
                "selected_keys": [entry["key"] for entry in record["pending_comments"]],
                "selected_units": 1,
            }
        results.write_result(record, root, run_id)
        envelope = json.loads(_response(task, record))
        if not initial:
            match = re.search(r'"base_digest": "([^"]+)"', task)
            assert match is not None
            envelope["base_digest"] = match[1]
            if followup == "invalid":
                envelope["capability"] = "wrong-capability"
        return {
            "ok": not (followup == "failed" and not initial),
            "output": json.dumps(envelope),
        }

    out = D.run_review(
        [link],
        dispatch=dispatch,
        root=root,
        run_id=run_id,
        post=True,
        generate_report=False,
        concurrency=1,
    )
    assert len(sent) == 1
    assert secret not in json.dumps(sent[0]["github_review_payload"])
    assert "exposed token" in sent[0]["github_review_payload"]["comments"][0]["body"]
    assert sent[0]["delivery_intent"]["operation_id"] != "a" * 32
    assert not out["per_change"][0]["post_ok"]
    saved = results.read_result(cid, root, run_id)
    intent = copy.deepcopy(saved["delivery_intent"])
    assert intent["state"] == "indeterminate"
    if run_id is None:
        assert list(store.runs_root(root).iterdir()) == []

    def retry(task, timeout):
        staged = results.read_result(cid, root)
        assert staged["delivery_intent"]["operation_id"] == intent["operation_id"]
        wire = D._outbound_payload(staged["github_review_payload"], intent["operation_id"])
        remote.append(dict(wire, id="42", state="PENDING", user={"login": "sage"}))
        return {"ok": False, "error": "response lost after delivery"}

    resumed = D.post_recorded(cid, link, dispatch=retry, root=root, run_id=run_id)
    assert resumed["post_ok"]
    assert resumed["posted_review_id"] == "42"
    saved = results.read_result(cid, root, run_id)
    assert saved["delivery_intent"]["state"] == "confirmed"
    assert saved["delivery_intent"]["operation_id"] == intent["operation_id"]


@pytest.mark.parametrize("run_id", [None, "scoped"])
@pytest.mark.parametrize("failure", ["capability", "shape", "write"])
def test_rejected_response_never_reads_or_posts_worker_file(tmp_path, monkeypatch, run_id, failure):
    root = tmp_path / "app"
    link = "https://github.com/o/r/pull/1"
    cid = D._cid(link)

    def dispatch(task, timeout):
        assert "SINGLE thorough pass" in task
        record = _record(cid)
        results.write_result(record, root, run_id)
        envelope = json.loads(_response(task, record))
        if failure == "capability":
            envelope["capability"] = "wrong-capability"
        elif failure == "shape":
            envelope["record"]["phase1"] = []
        else:

            def refuse(*args, **kwargs):
                raise OSError("persistence refused")

            monkeypatch.setattr(results, "write_result", refuse)
        monkeypatch.setattr(results, "read_result", lambda *a, **kw: pytest.fail("unadopted read"))
        return {"ok": True, "output": json.dumps(envelope)}

    out = D.run_review(
        [link],
        dispatch=dispatch,
        root=root,
        run_id=run_id,
        post=True,
        generate_report=False,
        concurrency=1,
    )
    assert not out["per_change"][0]["result_recorded"]
    assert not out["per_change"][0]["deep_reviewed"]


@pytest.mark.parametrize("run_id", [None, "scoped"])
def test_envelope_drops_driver_delivery_metadata_before_persist(tmp_path, run_id):
    record = _record("CR-1")
    record.update(
        delivery_intent={"state": "prepared"},
        posted_keys=["design"],
        posted_review_id="forged",
        github_review_payload={"body": "forged"},
    )
    response = _response('"capability": "issued-token"', record)
    accepted, error = D._persist_worker_response(
        response,
        change_id="CR-1",
        capability="issued-token",
        root=tmp_path,
        run_id=run_id,
    )
    assert error == ""
    for key in ("delivery_intent", "posted_keys", "posted_review_id", "github_review_payload"):
        assert key not in accepted
        assert key not in results.read_result("CR-1", tmp_path, run_id)


def test_lost_response_does_not_negate_confirmed_delivery(tmp_path):
    record = _record("CR-1", red=1, yellow=0)
    results.write_result(record, tmp_path, "run-a")
    outcome = D.post_recorded(
        "CR-1",
        "https://github.com/o/r/pull/1",
        root=tmp_path,
        run_id="run-a",
        dispatch=lambda *_: {"ok": False, "error": "response lost after delivery"},
        confirm=lambda *_: "42",
        accepted_record=record,
    )
    assert outcome["post_ok"]
    assert outcome["posted_review_id"] == "42"
    assert results.read_result("CR-1", tmp_path, "run-a")["posted_keys"]
