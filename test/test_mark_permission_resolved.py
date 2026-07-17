"""Tests for _mark_permission_resolved — persisting approval decisions into permission messages."""

import json

from kiro_crew.dashboard.state import _mark_permission_resolved


class TestMarkPermissionResolved:
    def test_marks_matching_permission(self) -> None:
        msgs = [
            {"role": "user", "content": "hi", "cls": "", "ts": "1"},
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "abc123"}), "ts": "2"},
            {"role": "tool", "content": "ok", "cls": "", "ts": "3"},
        ]
        _mark_permission_resolved(msgs, "abc123", "approved")
        cls = json.loads(msgs[1]["cls"])
        assert cls["resolved"] == "approved"
        assert cls["request_id"] == "abc123"

    def test_does_not_touch_other_permissions(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "aaa"}), "ts": "1"},
            {"role": "permission", "content": "read", "cls": json.dumps({"request_id": "bbb"}), "ts": "2"},
        ]
        _mark_permission_resolved(msgs, "bbb", "rejected")
        assert "resolved" not in json.loads(msgs[0]["cls"])
        assert json.loads(msgs[1]["cls"])["resolved"] == "rejected"

    def test_no_match_is_noop(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "aaa"}), "ts": "1"},
        ]
        _mark_permission_resolved(msgs, "nonexistent", "approved")
        assert "resolved" not in json.loads(msgs[0]["cls"])

    def test_malformed_cls_skipped(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": "not-json", "ts": "1"},
            {"role": "permission", "content": "read", "cls": json.dumps({"request_id": "abc"}), "ts": "2"},
        ]
        _mark_permission_resolved(msgs, "abc", "trust")
        assert json.loads(msgs[1]["cls"])["resolved"] == "trust"

    def test_empty_cls_skipped(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": "", "ts": "1"},
        ]
        _mark_permission_resolved(msgs, "abc", "approved")
        assert msgs[0]["cls"] == ""

    def test_empty_messages_is_noop(self) -> None:
        msgs: list[dict] = []
        _mark_permission_resolved(msgs, "abc", "approved")
        assert msgs == []

    def test_preserves_existing_cls_fields(self) -> None:
        original = {"request_id": "abc", "tool_input": "ls -la", "is_read_only": "1"}
        msgs = [
            {"role": "permission", "content": "shell", "cls": json.dumps(original), "ts": "1"},
        ]
        _mark_permission_resolved(msgs, "abc", "approved")
        cls = json.loads(msgs[0]["cls"])
        assert cls["resolved"] == "approved"
        assert cls["tool_input"] == "ls -la"
        assert cls["is_read_only"] == "1"
        assert cls["request_id"] == "abc"

    def test_finds_most_recent_match(self) -> None:
        """With duplicate request_ids (shouldn't happen but defensive), marks the last one."""
        msgs = [
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "abc"}), "ts": "1"},
            {"role": "tool", "content": "ok", "cls": "", "ts": "2"},
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "abc"}), "ts": "3"},
        ]
        _mark_permission_resolved(msgs, "abc", "approved")
        assert "resolved" not in json.loads(msgs[0]["cls"])
        assert json.loads(msgs[2]["cls"])["resolved"] == "approved"
