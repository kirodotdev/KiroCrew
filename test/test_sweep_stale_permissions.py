"""Tests for _sweep_stale_permissions — orphaned permission cleanup at turn-start."""

import json
from unittest.mock import MagicMock, patch

from kiro_crew.dashboard.chat_handlers import _sweep_stale_permissions


class TestSweepStalePermissions:
    def _make_slot(self, messages):
        slot = MagicMock()
        slot.messages = messages
        slot._dirty = False
        return slot

    def test_marks_unresolved_permission_as_stale(self) -> None:
        msgs = [
            {"role": "user", "content": "hi", "cls": ""},
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "abc"})},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_stale_permissions(slot)
        cls = json.loads(msgs[1]["cls"])
        assert cls["resolved"] == "stale"
        assert slot._dirty is True

    def test_skips_already_resolved_permission(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "abc", "resolved": "approved"})},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_stale_permissions(slot)
        cls = json.loads(msgs[0]["cls"])
        assert cls["resolved"] == "approved"
        assert slot._dirty is False

    def test_skips_non_permission_messages(self) -> None:
        msgs = [
            {"role": "user", "content": "hi", "cls": ""},
            {"role": "assistant", "content": "hello", "cls": ""},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_stale_permissions(slot)
        assert slot._dirty is False

    def test_handles_malformed_cls_gracefully(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": "not-json"},
            {"role": "permission", "content": "read", "cls": json.dumps({"request_id": "xyz"})},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_stale_permissions(slot)
        assert msgs[0]["cls"] == "not-json"  # untouched
        assert json.loads(msgs[1]["cls"])["resolved"] == "stale"

    def test_handles_valid_json_non_dict_cls(self) -> None:
        # cls that parses to valid JSON but is NOT a dict ([], str, int, null)
        # must be skipped, not raise TypeError that aborts the whole sweep.
        msgs = [
            {"role": "permission", "content": "a", "cls": json.dumps([])},
            {"role": "permission", "content": "b", "cls": json.dumps("x")},
            {"role": "permission", "content": "c", "cls": json.dumps(123)},
            {"role": "permission", "content": "d", "cls": json.dumps(None)},
            {"role": "permission", "content": "e", "cls": json.dumps({"request_id": "xyz"})},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_stale_permissions(slot)
        # non-dict entries left untouched
        assert msgs[0]["cls"] == json.dumps([])
        assert msgs[1]["cls"] == json.dumps("x")
        assert msgs[2]["cls"] == json.dumps(123)
        assert msgs[3]["cls"] == json.dumps(None)
        # the trailing valid dict still gets swept (proves loop did not abort)
        assert json.loads(msgs[4]["cls"])["resolved"] == "stale"

    def test_emits_sel_audit_event(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "abc"})},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel") as mock_sel:
            _sweep_stale_permissions(slot)
        mock_sel().log_api_access.assert_called_once_with(
            caller="gateway",
            operation="permission.resolve_stale",
            outcome="allowed",
            source="turn_start_sweep",
            resources="abc",
        )

    def test_sweeps_multiple_orphaned_permissions(self) -> None:
        msgs = [
            {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "a"})},
            {"role": "permission", "content": "read", "cls": json.dumps({"request_id": "b"})},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_stale_permissions(slot)
        assert json.loads(msgs[0]["cls"])["resolved"] == "stale"
        assert json.loads(msgs[1]["cls"])["resolved"] == "stale"
        assert slot._dirty is True

    def test_resolved_false_is_treated_as_resolved(self) -> None:
        """resolved: False (any future state) should NOT be marked stale."""
        msgs = [
            {"role": "permission", "content": "shell",
             "cls": json.dumps({"request_id": "abc", "resolved": False})},
        ]
        slot = self._make_slot(msgs)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_stale_permissions(slot)
        cls = json.loads(msgs[0]["cls"])
        assert cls["resolved"] is False  # untouched
        assert slot._dirty is False
