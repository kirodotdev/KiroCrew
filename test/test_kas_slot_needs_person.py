"""Gap 8: a Kiro Agent workflow parked on a person folds into the slot's
``needs_input`` flag, through ``slot_projection.kas_slot_needs_person``.

The helper reads the ALREADY-running router singleton (never constructs one) and
answers ``False`` on any doubt, so a gateway with KAS off pays nothing and the
sidebar flag never raises on the slots poll.
"""

from kiro_crew.dashboard import slot_projection
from kiro_crew.kas_workflows import router as kas_router


class _FakeRouter:
    def __init__(self, rows):
        self._rows = rows

    def rows(self):
        return self._rows


def test_empty_slot_key_is_false():
    assert slot_projection.kas_slot_needs_person("") is False


def test_no_router_singleton_is_false(monkeypatch):
    # KAS never ran in this process: the module global is None and the helper
    # must NOT call get_router() (which would build one).
    monkeypatch.setattr(kas_router, "_ROUTER", None, raising=False)
    assert slot_projection.kas_slot_needs_person("s1") is False


def test_needs_attention_row_for_this_slot(monkeypatch):
    rows = [{"workflowId": "wf1", "slotKey": "s1", "controls": {"needsAttention": True}}]
    monkeypatch.setattr(kas_router, "_ROUTER", _FakeRouter(rows), raising=False)
    assert slot_projection.kas_slot_needs_person("s1") is True
    # A run reporting into a different chat does not light THIS one.
    assert slot_projection.kas_slot_needs_person("s2") is False


def test_blocking_step_counts(monkeypatch):
    rows = [
        {
            "workflowId": "wf1",
            "slotKey": "s1",
            "controls": {"needsAttention": False},
            "blocking": {"nodeId": "n"},
        }
    ]
    monkeypatch.setattr(kas_router, "_ROUTER", _FakeRouter(rows), raising=False)
    assert slot_projection.kas_slot_needs_person("s1") is True


def test_a_running_run_with_no_hold_is_false(monkeypatch):
    rows = [
        {
            "workflowId": "wf1",
            "slotKey": "s1",
            "controls": {"needsAttention": False},
            "blocking": None,
        }
    ]
    monkeypatch.setattr(kas_router, "_ROUTER", _FakeRouter(rows), raising=False)
    assert slot_projection.kas_slot_needs_person("s1") is False


def test_rows_raising_is_swallowed(monkeypatch):
    class _Bad:
        def rows(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(kas_router, "_ROUTER", _Bad(), raising=False)
    assert slot_projection.kas_slot_needs_person("s1") is False
