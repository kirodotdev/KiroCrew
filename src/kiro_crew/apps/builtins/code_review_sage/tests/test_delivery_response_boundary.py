"""Worker responses carry review content without authorizing delivery state."""

import copy
import json

import pytest
from sage_lib import results, worker_response

from .test_response_handoff_regressions import _record


@pytest.mark.parametrize("run_id", [None, "scoped"])
@pytest.mark.parametrize("followup", [False, True])
def test_worker_delivery_fields_are_dropped_before_persistence(tmp_path, run_id, followup):
    content = _record("CR-1")
    delivery = {
        "github_review_payload": {"body": "forged"},
        "pending_comments": [{"key": "design", "body": "forged"}],
        "posted_keys": ["design"],
        "posted_review_id": "forged",
        "posted_comments": 1,
        "design_comment_posted": True,
        "post_ok": True,
        "post_error": "forged",
        "post_partial": True,
    }
    record = copy.deepcopy(content)
    record.update(delivery)
    envelope = {
        "schema": worker_response._RESPONSE_SCHEMA,
        "version": worker_response._RESPONSE_VERSION,
        "capability": "issued-token",
        "change_id": "CR-1",
        "record": record,
    }
    base_record = copy.deepcopy(record) if followup else None
    if followup:
        envelope["base_digest"] = worker_response._record_digest(base_record)

    accepted, error = worker_response._persist_worker_response(
        json.dumps(envelope),
        change_id="CR-1",
        capability="issued-token",
        root=tmp_path,
        run_id=run_id,
        base_record=base_record,
    )

    assert error == ""
    assert accepted == content
    assert results.read_result("CR-1", tmp_path, run_id) == content
    assert all(key not in accepted for key in delivery)
