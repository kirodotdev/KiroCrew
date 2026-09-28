"""Workflow snapshots use one owner record while preserving privacy modes."""

import json

import pytest

from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.workflow_memory import read_task_snapshot, write_task_snapshot


def test_member_task_payload_has_one_canonical_record(tmp_path):
    execution = ExecutionContext(
        "alice", MemoryStoreRef("member-alice", "alice"), "member", "shared-template"
    )
    path = tmp_path / "runs.json"
    row = {
        "task_id": "synthetic",
        "error": "synthetic failure",
        "execution_context": execution.to_record(),
    }
    write_task_snapshot(path, json.dumps([row]))
    assert json.loads(read_task_snapshot(path)) == [row]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("mode", ["incognito", "temporary"])
def test_restricted_payload_is_not_persisted(tmp_path, mode):
    execution = ExecutionContext(
        "alice", MemoryStoreRef("member-alice", "alice"), "member", "shared-template", mode
    )
    path = tmp_path / "runs.json"
    row = {
        "task_id": "synthetic",
        "error": "restricted synthetic failure",
        "execution_context": execution.to_record(),
    }
    write_task_snapshot(path, json.dumps([row]))
    assert json.loads(path.read_text(encoding="utf-8")) == []
