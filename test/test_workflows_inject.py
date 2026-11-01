"""M6.4 / FIX-15 — workflow result-to-chat injection summary.

Asserts the terminal-run summary that lands back in the originating chat: the
result blob is included, and any artifact file paths the run produced are pulled
out and listed so the chat agent can open them directly.
"""

from __future__ import annotations

import json
import re

import pytest

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
        self.folder_id = ""
        # Title-pin state the auto-title gate reads (mirrors _ChatSlot): an
        # explicit per-run title sets _titled/_title_origin and bumps the epoch.
        self._titled = False
        self._title_origin = "auto"
        self._title_epoch = 0
        # Mirror the delivery seam append_and_surface reads: append's own
        # broadcast callback (the SSE door, mid-carrying) and the reader flag
        # that suppresses it. Deliveries land in ``delivered`` so tests can
        # assert the row went out exactly once, with identity.
        self._has_reader = False
        self.delivered: list[dict] = []
        # Dismissed-source-link contract the fallback rebind now exercises: a
        # reused fallback slot's stale set is cleared + marked unhydrated on a
        # link change (mirrors the real _ChatSlot so the injector can call it).
        self._dismissed_source_links: set[str] = set()
        self._dismissed_hydrated = True

    def invalidate_source_links(self) -> None:
        pass

    def _on_message(self, _key, msg) -> None:
        self.delivered.append(msg)

    def append(
        self, role, content, cls="", ts="", *, broadcast=True, broadcast_user=False, meta=None
    ):
        # Mirror the real ``_ChatSlot.append`` contract: mint ``meta.mid``, hand
        # the appended row back, and deliver ONE live copy via ``_on_message``
        # when no reader is draining (the injector reads the id off the return
        # to stamp the durable transcript copy with the same identity).
        msg = {
            "role": role,
            "content": content,
            "meta": {**(meta or {}), "mid": f"m-test-{len(self.messages)}"},
        }
        self.messages.append(msg)
        if broadcast and (role != "user" or broadcast_user) and not self._has_reader:
            self._on_message(self.key, msg)
        return msg


class _FakeState:
    """Minimal DashboardState double: pre-seeded slots + captured broadcasts."""

    def __init__(self, slots=None) -> None:
        self._slots = dict(slots or {})
        self.conversation_log = None
        self.broadcasts: list[tuple[str, dict]] = []
        self.created: list[str] = []
        self._folders: list[dict] = []

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


def _finished(run_id: str, result: object) -> dict:
    return {"name": "x", "run_id": run_id, "status": "finished", "result": result}


def _result_of_size(chars: int) -> dict:
    """A result whose ``json.dumps(..., indent=2)`` form is exactly ``chars`` long."""
    result = {"k": "x" * (chars - len(json.dumps({"k": ""}, indent=2)))}
    assert len(json.dumps(result, indent=2)) == chars
    return result


def _json_blocks(body: str) -> list[str]:
    return re.findall(r"```json\n(.*?)\n```", body, re.S)


_NESTED_RESULT = {
    "survived": [{"id": f"F{i}", "claim": "x" * 120, "evidence": "line " * 20} for i in range(30)]
}


@pytest.mark.parametrize("chars", [200, 4000])
def test_summary_keeps_a_result_within_the_cap_unchanged(chars: int) -> None:
    result = _result_of_size(chars)
    body = _summarize(_finished("wf_fits", result))
    assert _json_blocks(body) == [json.dumps(result, indent=2)]
    assert "truncat" not in body.lower()


@pytest.mark.parametrize(
    "result", [_result_of_size(4001), _NESTED_RESULT], ids=["one-over-the-cap", "nested"]
)
def test_summary_marks_an_oversized_result_as_truncated(result: dict) -> None:
    """A result over the cap never reaches the chat as a json block that does not
    parse; the message says it was cut, gives both sizes, and names where the
    complete value is."""
    full = json.dumps(result, indent=2)
    body = _summarize(_finished("wf_big", result))
    for block in _json_blocks(body):
        json.loads(block)
    assert "truncated" in body.lower()
    assert str(len(full)) in body and "4000" in body
    assert full[:200] in body
    assert "workflow_result('wf_big')" in body and "`result`" in body


def test_summary_does_not_label_an_unserializable_result_as_json() -> None:
    body = _summarize(_finished("wf_odd", {("a", "b"): 1}))
    assert _json_blocks(body) == []
    assert "('a', 'b')" in body


def test_summary_header_format_is_pinned_for_frontend() -> None:
    """The dashboard's WorkflowCompletionCard detects/parses completions by
    matching this exact header shape (regex in website/.../WorkflowCompletionCard.tsx):

        [Workflow completion event]\\nWorkflow `<name>` (<run_id>) -> **<status>**

    (arrow is the U+2192 rightwards arrow). This cross-layer contract has no
    shared constant, so pin the format here — if the header wording drifts, this
    test fails instead of the launch/completion card silently degrading in the
    UI."""
    snap = {"name": "pizza", "run_id": "wf_1", "status": "finished", "result": {"n": 1}}
    body = _summarize(snap)
    lines = body.splitlines()
    assert lines[0] == "[Workflow completion event]"
    assert lines[1] == "Workflow `pizza` (wf_1) \u2192 **finished**"


# --------------------------------------------------------------------------- #
# Routing: result lands in the ORIGINATING chat slot, not a separate
# workflow-<id> slot the user never sees.
# --------------------------------------------------------------------------- #


def test_slot_key_from_session_strips_prefix() -> None:
    assert _slot_key_from_session("dashboard:chat-2-123") == "chat-2-123"
    assert _slot_key_from_session("chat-7") == "chat-7"  # already bare


def test_inject_routes_to_originating_slot_and_broadcasts_live() -> None:
    origin = _FakeSlot("chat-2-123")
    state = _FakeState({"chat-2-123": origin})
    snap = {
        "name": "pizza",
        "run_id": "wf_2",
        "status": "finished",
        "session_key": "dashboard:chat-2-123",
        "result": {"report": "/tmp/r.md"},
    }
    ok = inject_workflow_result(state, "wf_2", snap)
    assert ok is True
    # Appended to the ORIGINATING chat slot, NOT a new workflow-<id> slot.
    assert state.created == []  # never created a fallback slot
    assert len(origin.messages) == 1
    assert "pizza" in origin.messages[0]["content"]
    # Live delivery goes through append's OWN mid-carrying door exactly once
    # (no reader active). A second hand-built broadcast_ws frame would be
    # mid-less and render as a duplicate bubble.
    assert len(origin.delivered) == 1
    assert origin.delivered[0]["meta"]["mid"] == origin.messages[0]["meta"]["mid"]
    # The route kind rides meta, which both delivery doors carry.
    assert origin.delivered[0]["meta"]["kind"] == "workflow_result"
    assert [p for k, p in state.broadcasts if k == "chat_message"] == []


def test_inject_falls_back_when_origin_slot_gone() -> None:
    state = _FakeState({})  # originating slot is gone
    snap = {
        "name": "pizza",
        "run_id": "wf_9",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
    }
    ok = inject_workflow_result(state, "wf_9", snap)
    assert ok is True
    assert state.created == ["workflow-wf_9"]  # dedicated fallback slot created


def test_fallback_slot_uses_per_run_title_when_given() -> None:
    # A webhook-driven run (originating session always gone) can title its own
    # fallback slot, so many runs of the same workflow are told apart in the
    # sidebar instead of all reading ``Workflow: <name>``.
    state = _FakeState({})
    snap = {
        "name": "mr-review-sage-bot",
        "run_id": "wf_t",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
        "fallback_title": "MR !1234 review",
    }
    ok = inject_workflow_result(state, "wf_t", snap)
    assert ok is True
    slot = state.get_slot("workflow-wf_t")
    assert slot.title == "MR !1234 review"
    # The explicit title is pinned so a follow-up turn's auto-titling cannot
    # silently overwrite it (mirrors slot_create's caller-explicit title).
    assert slot._titled is True
    assert slot._title_origin == "user"
    assert slot._title_epoch == 1


def test_supplied_title_survives_a_follow_up_auto_title() -> None:
    # End to end: a supplied fallback title is pinned by the injector, so a
    # later follow-up turn's background auto-titling (`_maybe_auto_title`)
    # leaves it alone instead of silently replacing it. Without the pin the
    # `_titled` gate would be open and the title would be overwritten.
    import asyncio

    from kiro_crew.dashboard.chat_title import _maybe_auto_title

    state = _FakeState({})
    snap = {
        "name": "mr-review-sage-bot",
        "run_id": "wf_e2e",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
        "fallback_title": "MR !1234 review",
    }
    assert inject_workflow_result(state, "wf_e2e", snap) is True
    slot = state.get_slot("workflow-wf_e2e")
    # The auto-title path reads these; a non-member slot with a user turn would
    # normally be titled, but the pin set by the injector short-circuits it.
    slot.mode = ""
    slot._title_in_flight = False
    slot.messages.append({"role": "user", "content": "please re-run the review"})
    asyncio.run(_maybe_auto_title(state, slot))
    assert slot.title == "MR !1234 review"  # the caller's title is untouched
    # No per-run title given -> the default ``Workflow: <name>`` is unchanged.
    state = _FakeState({})
    snap = {
        "name": "pizza",
        "run_id": "wf_d",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
    }
    ok = inject_workflow_result(state, "wf_d", snap)
    assert ok is True
    slot = state.get_slot("workflow-wf_d")
    assert slot.title == "Workflow: pizza"
    # The default title is NOT pinned: a run that named no title keeps today's
    # auto-title behaviour.
    assert slot._titled is False
    assert slot._title_origin == "auto"


def test_fallback_title_is_cleaned_at_the_injector() -> None:
    # The title is cleaned where every run path converges on applying it: a
    # title with newlines/control chars collapses to a single bounded line.
    state = _FakeState({})
    snap = {
        "name": "x",
        "run_id": "wf_c",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
        "fallback_title": "MR  !1234\n\treview",
    }
    ok = inject_workflow_result(state, "wf_c", snap)
    assert ok is True
    slot = state.get_slot("workflow-wf_c")
    assert slot.title == "MR !1234 review"


def test_fallback_slot_filed_under_resolved_folder_id() -> None:
    # The folder id is resolved server-side by the run handler (from an
    # EXISTING folder); the fallback slot is filed under it when the folder
    # still exists at delivery.
    state = _FakeState({})
    state._folders = [{"id": "fld_mr"}]
    snap = {
        "name": "mr-review-sage-bot",
        "run_id": "wf_f",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
        "fallback_folder_id": "fld_mr",
    }
    ok = inject_workflow_result(state, "wf_f", snap)
    assert ok is True
    slot = state.get_slot("workflow-wf_f")
    assert slot.folder_id == "fld_mr"


def test_fallback_slot_unfiled_when_folder_deleted_before_delivery() -> None:
    # The folder resolved to an existing id at run start but was deleted
    # before the result arrives -> the slot is left unfiled and nothing is
    # recreated, no error.
    state = _FakeState({})
    state._folders = []  # the resolved folder is absent at delivery
    snap = {
        "name": "mr-review-sage-bot",
        "run_id": "wf_g",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
        "fallback_folder_id": "fld_gone",
    }
    ok = inject_workflow_result(state, "wf_g", snap)
    assert ok is True
    slot = state.get_slot("workflow-wf_g")
    assert slot.folder_id == ""  # unfiled, default preserved


def test_fallback_slot_unfiled_when_no_folder_given() -> None:
    # No folder given -> the fallback slot stays unfiled (today's default).
    state = _FakeState({})
    snap = {
        "name": "pizza",
        "run_id": "wf_h",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
    }
    ok = inject_workflow_result(state, "wf_h", snap)
    assert ok is True
    slot = state.get_slot("workflow-wf_h")
    assert slot.folder_id == ""


def test_fallback_slot_unfiled_when_folder_is_mate_home_at_delivery() -> None:
    # The folder exists at delivery but sits in a member's home, and the slot's
    # own store is not that member's. Delivery re-checks ownership against the
    # run's real store and leaves the slot unfiled — fail-safe, no error — even
    # though the admission-time refusal already guards the common path.
    state = _FakeState({})
    state._folders = [
        {"id": "home-1", "parent_id": "", "mate_home": True, "owner_app": "member:store-x"},
        {"id": "child-1", "parent_id": "home-1"},
    ]
    snap = {
        "name": "mr-review-sage-bot",
        "run_id": "wf_m",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
        "fallback_folder_id": "child-1",
    }
    ok = inject_workflow_result(state, "wf_m", snap)
    assert ok is True
    slot = state.get_slot("workflow-wf_m")
    assert slot.folder_id == ""  # unfiled: a member's home is not ours to file into


def test_inject_no_session_key_returns_false() -> None:
    state = _FakeState({})
    snap = {"name": "x", "run_id": "wf_0", "status": "finished", "result": {}, "session_key": ""}
    assert inject_workflow_result(state, "wf_0", snap) is False


def test_durable_copy_carries_the_window_rows_id() -> None:
    # The durable transcript copy must ride with the SAME ``meta.mid`` the
    # window copy was minted; a re-minted or absent id leaves a bounded
    # slot-detail read unable to reconcile the two copies as one message.
    from unittest.mock import MagicMock, patch

    origin = _FakeSlot("chat-2-123")
    state = _FakeState({"chat-2-123": origin})
    state.conversation_log = MagicMock()
    snap = {
        "name": "pizza",
        "run_id": "wf_2",
        "status": "finished",
        "session_key": "dashboard:chat-2-123",
        "result": {"ok": True},
    }
    with patch("kiro_crew.dashboard.workflow_inject.append_if_absent_off_loop") as durable:
        assert inject_workflow_result(state, "wf_2", snap) is True
    assert len(origin.messages) == 1
    window_mid = origin.messages[0]["meta"]["mid"]
    assert (
        durable.call_args.kwargs["mid"] == window_mid
    ), "the durable copy did not carry the window row's id"


def test_inject_dedups_on_refire() -> None:
    origin = _FakeSlot("chat-5")
    state = _FakeState({"chat-5": origin})
    snap = {
        "name": "x",
        "run_id": "wf_5",
        "status": "finished",
        "session_key": "dashboard:chat-5",
        "result": {"v": 1},
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
        "name": "pizza",
        "run_id": "wf_2",
        "status": "finished",
        "session_key": "dashboard:chat-2-123",
        "result": {"n": 1},
    }
    fired: list = []
    ok = inject_workflow_result(
        state, "wf_2", snap, on_injected=lambda s, sn: fired.append((s, sn))
    )
    assert ok is True
    assert len(fired) == 1
    assert fired[0][0] is origin  # the live originating slot
    assert fired[0][1]["run_id"] == "wf_2"


def test_on_injected_not_fired_for_fallback_slot() -> None:
    # Originating slot gone -> fallback workflow-<id> slot -> no agent watching it,
    # so the auto-turn callback must NOT fire.
    state = _FakeState({})
    snap = {
        "name": "x",
        "run_id": "wf_9",
        "status": "finished",
        "session_key": "dashboard:chat-gone",
        "result": {"ok": True},
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
        "name": "x",
        "run_id": "wf_5",
        "status": "finished",
        "session_key": "dashboard:chat-5",
        "result": {"v": 1},
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
    assert (
        inject_workflow_result(state, "wf_0", snap, on_injected=lambda s, sn: fired.append(s))
        is False
    )
    assert fired == []
