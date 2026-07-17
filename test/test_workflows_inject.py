"""M6.4 / FIX-15 — workflow result-to-chat injection summary.

Asserts the terminal-run summary that lands back in the originating chat: the
result blob is included, and any artifact file paths the run produced are pulled
out and listed so the chat agent can open them directly.
"""

from __future__ import annotations

from kiro_crew.dashboard.workflow_inject import (
    _collect_artifact_paths,
    _slot_key_from_session,
    _summarize,
    inject_workflow_result,
)


# --- lightweight fakes for routing tests --------------------------------------
class _FakeSlot:
    def __init__(self, key: str) -> None:
        self.key = key
        self.name = key
        self.messages: list[dict] = []
        self.linked_session_key = ""
        self.title = ""

    def append(self, role, content, cls="", ts="", *, broadcast=True, meta=None):
        self.messages.append({"role": role, "content": content})


class _FakeState:
    """Minimal DashboardState double: pre-seeded slots + captured broadcasts."""

    def __init__(self, slots=None) -> None:
        self._slots = dict(slots or {})
        self.conversation_log = None
        self.broadcasts: list[tuple[str, dict]] = []
        self.created: list[str] = []

    def get_slot(self, name):
        return self._slots.get(name)

    def get_or_create_slot(self, name, **kw):
        self.created.append(name)
        if name not in self._slots:
            self._slots[name] = _FakeSlot(name)
        return self._slots[name]

    def broadcast_ws(self, kind, payload):
        self.broadcasts.append((kind, payload))


def test_collect_artifact_paths_finds_absolute_file_paths() -> None:
    result = {
        "report": "/home/u/out/report.md",
        "sections": ["/tmp/wf/s1.txt", "/tmp/wf/s2.json", "just prose, no path"],
        "nested": {"img": "/var/data/chart.png"},
    }
    out: list[str] = []
    _collect_artifact_paths(result, out)
    assert "/home/u/out/report.md" in out
    assert "/tmp/wf/s1.txt" in out and "/tmp/wf/s2.json" in out
    assert "/var/data/chart.png" in out
    # prose without a path contributes nothing
    assert all(p.startswith("/") and "." in p.rsplit("/", 1)[-1] for p in out)


def test_collect_artifact_paths_ignores_non_paths() -> None:
    out: list[str] = []
    _collect_artifact_paths({"a": "hello world", "b": ["x/y", "relative/no.txt"]}, out)
    # relative paths are intentionally NOT matched (absolute-only, conservative)
    assert out == []


def test_summary_lists_artifacts_for_finished_run() -> None:
    snap = {
        "name": "pizza",
        "run_id": "wf_1",
        "status": "finished",
        "result": {"report": "/home/u/pizza.md"},
    }
    body = _summarize(snap)
    assert "Artifacts" in body
    assert "`/home/u/pizza.md`" in body
    assert "workflow_result('wf_1')" in body


def test_summary_no_artifacts_section_when_none() -> None:
    snap = {"name": "x", "run_id": "wf_2", "status": "finished", "result": {"n": 3}}
    body = _summarize(snap)
    assert "Artifacts" not in body


def test_summary_reports_failure() -> None:
    snap = {"name": "x", "run_id": "wf_3", "status": "failed", "error": "boom"}
    body = _summarize(snap)
    assert "failed" in body and "boom" in body


# --------------------------------------------------------------------------- #
# Routing: result lands in the ORIGINATING chat slot (regression — it used to
# go to a separate workflow-<id> slot the user never saw).
# --------------------------------------------------------------------------- #


def test_slot_key_from_session_strips_prefix() -> None:
    assert _slot_key_from_session("dashboard:chat-2-123") == "chat-2-123"
    assert _slot_key_from_session("chat-7") == "chat-7"  # already bare


def test_inject_routes_to_originating_slot_and_broadcasts_live() -> None:
    origin = _FakeSlot("chat-2-123")
    state = _FakeState({"chat-2-123": origin})
    snap = {
        "name": "pizza", "run_id": "wf_2", "status": "finished",
        "session_key": "dashboard:chat-2-123", "result": {"report": "/tmp/r.md"},
    }
    ok = inject_workflow_result(state, "wf_2", snap)
    assert ok is True
    # Appended to the ORIGINATING chat slot, NOT a new workflow-<id> slot.
    assert state.created == []  # never created a fallback slot
    assert len(origin.messages) == 1
    assert "pizza" in origin.messages[0]["content"]
    # Live chat_message broadcast to that slot so it shows without a manual fetch.
    chat_msgs = [p for k, p in state.broadcasts if k == "chat_message"]
    assert chat_msgs and chat_msgs[0]["slot"] == "chat-2-123"
    assert chat_msgs[0]["role"] == "assistant"


def test_inject_falls_back_when_origin_slot_gone() -> None:
    state = _FakeState({})  # originating slot no longer exists
    snap = {
        "name": "pizza", "run_id": "wf_9", "status": "finished",
        "session_key": "dashboard:chat-gone", "result": {"ok": True},
    }
    ok = inject_workflow_result(state, "wf_9", snap)
    assert ok is True
    assert state.created == ["workflow-wf_9"]  # dedicated fallback slot created


def test_inject_no_session_key_returns_false() -> None:
    state = _FakeState({})
    snap = {"name": "x", "run_id": "wf_0", "status": "finished", "result": {}, "session_key": ""}
    assert inject_workflow_result(state, "wf_0", snap) is False


def test_inject_dedups_on_refire() -> None:
    origin = _FakeSlot("chat-5")
    state = _FakeState({"chat-5": origin})
    snap = {
        "name": "x", "run_id": "wf_5", "status": "finished",
        "session_key": "dashboard:chat-5", "result": {"v": 1},
    }
    inject_workflow_result(state, "wf_5", snap)
    inject_workflow_result(state, "wf_5", snap)  # re-fire
    assert len(origin.messages) == 1  # not double-injected


# --------------------------------------------------------------------------- #
# Auto-turn callback: on a finished chat-linked run, on_injected fires exactly
# once for the LIVE ORIGINATING slot so the gateway can run an agent turn that
# interprets the result. It must NOT fire for the workflow-<id> fallback slot,
# a dedup re-fire, or a UI-only run (no session_key).
# --------------------------------------------------------------------------- #


def test_on_injected_fires_for_originating_slot() -> None:
    origin = _FakeSlot("chat-2-123")
    state = _FakeState({"chat-2-123": origin})
    snap = {
        "name": "pizza", "run_id": "wf_2", "status": "finished",
        "session_key": "dashboard:chat-2-123", "result": {"n": 1},
    }
    fired: list = []
    ok = inject_workflow_result(state, "wf_2", snap, on_injected=lambda s, sn: fired.append((s, sn)))
    assert ok is True
    assert len(fired) == 1
    assert fired[0][0] is origin  # the live originating slot
    assert fired[0][1]["run_id"] == "wf_2"


def test_on_injected_not_fired_for_fallback_slot() -> None:
    # Originating slot gone -> fallback workflow-<id> slot -> no agent watching it,
    # so the auto-turn callback must NOT fire.
    state = _FakeState({})
    snap = {
        "name": "x", "run_id": "wf_9", "status": "finished",
        "session_key": "dashboard:chat-gone", "result": {"ok": True},
    }
    fired: list = []
    ok = inject_workflow_result(state, "wf_9", snap, on_injected=lambda s, sn: fired.append(s))
    assert ok is True
    assert state.created == ["workflow-wf_9"]
    assert fired == []  # never auto-run in the fallback slot


def test_on_injected_not_fired_on_dedup_refire() -> None:
    origin = _FakeSlot("chat-5")
    state = _FakeState({"chat-5": origin})
    snap = {
        "name": "x", "run_id": "wf_5", "status": "finished",
        "session_key": "dashboard:chat-5", "result": {"v": 1},
    }
    fired: list = []
    cb = lambda s, sn: fired.append(s)  # noqa: E731
    inject_workflow_result(state, "wf_5", snap, on_injected=cb)
    inject_workflow_result(state, "wf_5", snap, on_injected=cb)  # re-fire
    assert len(origin.messages) == 1  # not double-injected
    assert len(fired) == 1  # auto-turn only on the first, fresh inject


def test_on_injected_not_fired_for_ui_only_run() -> None:
    state = _FakeState({})
    snap = {"name": "x", "run_id": "wf_0", "status": "finished", "result": {}, "session_key": ""}
    fired: list = []
    assert inject_workflow_result(state, "wf_0", snap, on_injected=lambda s, sn: fired.append(s)) is False
    assert fired == []
