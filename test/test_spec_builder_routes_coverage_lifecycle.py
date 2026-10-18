"""Spec Builder's HTTP handlers for an existing spec: detail, message, handoff, stop, delete.

Then the route table the app registers, and the failure branches the happy paths
never reach: swallowed filesystem errors, the git spawn seam, a reap race while
halting git, and teardown, delete and handoff-unwind failures.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
import spec_builder_routes_coverage_helpers
from aiohttp import web
from spec_builder_routes_coverage_helpers import (
    _OWNER_USER,
    CRED_NAME,
    _body,
    _entry,
    _FakeProc,
    _handoff_request,
    _mk,
    _no_rehydrate,
    _ready_handoff,
    _Slot,
    _State,
    _write_index,
)

from kiro_crew.apps.builtins.spec_builder.backend import handlers as handler_module
from kiro_crew.apps.builtins.spec_builder.backend import runtime as runtime_module
from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes as r

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_armed = spec_builder_routes_coverage_helpers._armed
_no_autonudge = spec_builder_routes_coverage_helpers._no_autonudge
_no_dispatch = spec_builder_routes_coverage_helpers._no_dispatch
_pin_state_dir = spec_builder_routes_coverage_helpers._pin_state_dir
_quiet_sel = spec_builder_routes_coverage_helpers._quiet_sel


# -- HTTP: detail, transcript, message ----------------------------------------


class TestHandleGet:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}, authed=False))
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}))
        assert out.status == 404 and _body(out)["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_the_documents_state_and_context_counters_are_returned(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        (spec / "requirements.md").write_text("the requirements")
        (spec / "tasks.md").write_text("- [ ] one")
        (spec / ".spec-state.json").write_text(json.dumps({"blocking": "review"}))
        _write_index({"demo": _entry(spec, worktree_branch="spec/demo")})
        slot = _Slot("spec-builder-demo-0123abcd", running=True)
        slot.messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "fs_write"},
            {"role": "tool", "content": "fs_read"},
            {"role": "assistant", "content": "done"},
        ]
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with _no_rehydrate():
            out = await r._handle_get(_mk("GET", "specs/demo", state=state, match={"name": "demo"}))
        payload = _body(out)
        assert payload["phase"] == "tasks"
        assert payload["files"]["requirements.md"] == "the requirements"
        assert payload["files"]["design.md"] is None
        assert payload["state"]["blocking"] == "review"
        assert payload["running"] is True
        assert payload["slot_key"] == "spec-builder-demo-0123abcd"
        assert payload["context"] == {
            "worktree_branch": "spec/demo",
            "turns": 1,
            "tool_calls": 2,
        }

    @pytest.mark.asyncio
    async def test_without_a_live_slot_the_key_is_resolved_from_the_index(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})
        # The SPA must NOT derive the key from the name: keys are per-creation, so a
        # reused name would mount the embed against the previous transcript.
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}))
        payload = _body(out)
        assert payload["slot_key"] == "spec-builder-demo-0123abcd"
        assert payload["running"] is False

    @pytest.mark.asyncio
    async def test_a_spec_deleted_while_the_documents_were_read_is_404(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})

        def _collect_then_delete(_spec_dir):
            r._save_index({})
            return "new", {}, None, {}

        with mock.patch.object(r, "_collect_spec_documents", _collect_then_delete):
            out = await r._handle_get(
                _mk("GET", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_spec_recreated_elsewhere_during_the_read_is_409(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})

        def _collect_then_replace(_spec_dir):
            r._save_index({"demo": _entry(tmp_path / "somewhere-else")})
            return "new", {}, None, {}

        with mock.patch.object(r, "_collect_spec_documents", _collect_then_replace):
            out = await r._handle_get(
                _mk("GET", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_read"

    @pytest.mark.asyncio
    async def test_a_foreign_slot_refuses_the_whole_detail_read(self, tmp_path):
        # Returning 200 anyway let the user read, message into and approve tool
        # calls in an unrelated session from this app.
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})
        state = _State(
            **{"spec-builder-demo-0123abcd": _Slot("spec-builder-demo-0123abcd", app="issue-radar")}
        )
        with _no_rehydrate():
            out = await r._handle_get(_mk("GET", "specs/demo", state=state, match={"name": "demo"}))
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"

    @pytest.mark.asyncio
    async def test_index_strings_are_redacted(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec, spec_type=CRED_NAME, worktree_branch=CRED_NAME)})
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}))
        assert CRED_NAME not in json.dumps(_body(out))


class TestHandleMessages:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_messages(
            _mk("GET", "specs/demo/messages", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_messages(
            _mk("GET", "specs/demo/messages", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_the_transcript_and_the_running_flag_are_returned(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        slot = _Slot("spec-builder-demo-0123abcd", running=True)
        slot.messages = [{"role": "user", "content": "hello", "ts": "1"}]
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with _no_rehydrate():
            out = await r._handle_messages(
                _mk("GET", "specs/demo/messages", state=state, match={"name": "demo"})
            )
        assert _body(out) == {
            "messages": [{"role": "user", "content": "hello", "ts": "1"}],
            "running": True,
        }

    @pytest.mark.asyncio
    async def test_a_foreign_slot_refuses_rather_than_leaking_the_conversation(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        state = _State(
            **{"spec-builder-demo-0123abcd": _Slot("spec-builder-demo-0123abcd", app="issue-radar")}
        )
        with _no_rehydrate():
            out = await r._handle_messages(
                _mk("GET", "specs/demo/messages", state=state, match={"name": "demo"})
            )
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_message(
            _mk("POST", "specs/demo/message", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_message(
            _mk("POST", "specs/demo/message", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_400(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_message(
            _mk("POST", "specs/demo/message", state=_State(), match={"name": "demo"}, body=None)
        )
        assert out.status == 400

    @pytest.mark.asyncio
    async def test_an_empty_text_is_400(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_message(
            _mk(
                "POST",
                "specs/demo/message",
                state=_State(),
                match={"name": "demo"},
                body={"text": "   "},
            )
        )
        assert out.status == 400 and _body(out)["code"] == "text_required"

    @pytest.mark.asyncio
    async def test_a_claim_naming_a_different_directory_is_409(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_message(
            _mk(
                "POST",
                "specs/demo/message",
                state=_State(),
                match={"name": "demo"},
                body={"text": "go", "spec_dir": "/somewhere-else"},
            )
        )
        assert out.status == 409 and _body(out)["code"] == "stale_client"

    @pytest.mark.asyncio
    async def test_a_foreign_slot_refuses_the_dispatch(self, tmp_path, _no_dispatch):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=None)):
            out = await r._handle_message(
                _mk(
                    "POST",
                    "specs/demo/message",
                    state=_State(),
                    match={"name": "demo"},
                    body={"text": "go"},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"
        _no_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_delete_that_lands_during_slot_acquisition_is_409(self, tmp_path, _no_dispatch):
        # _ensure_worker_slot awaits, so a delete can start AND finish between the
        # first check and the dispatch.
        _write_index({"demo": _entry(tmp_path / "demo")})

        async def _slot_then_delete(*_a, **_kw):
            r._save_index({})
            return _Slot("spec-builder-demo-0123abcd")

        with mock.patch.object(r, "_ensure_worker_slot", _slot_then_delete):
            out = await r._handle_message(
                _mk(
                    "POST",
                    "specs/demo/message",
                    state=_State(),
                    match={"name": "demo"},
                    body={"text": "go"},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        _no_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_turn_is_relayed_and_the_spec_stamped(self, tmp_path, _no_dispatch):
        _write_index({"demo": _entry(tmp_path / "demo", working_dir=str(tmp_path))})
        with _no_rehydrate():
            out = await r._handle_message(
                _mk(
                    "POST",
                    "specs/demo/message",
                    state=_State(),
                    match={"name": "demo"},
                    body={
                        "text": " draft it ",
                        "spec_dir": str(tmp_path / "demo"),
                        "slot_key": "spec-builder-demo-0123abcd",
                    },
                )
            )
        assert _body(out) == {"ok": True}
        assert _no_dispatch.call_args.args[2] == "draft it"
        assert r._load_index()["demo"]["updated_at"] > 200.0


# -- HTTP: handoff ------------------------------------------------------------


class TestHandleHandoff:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_handoff(
            _mk("POST", "specs/demo/handoff", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_handoff(
            _mk("POST", "specs/demo/handoff", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_stale_claim_is_refused_before_the_sentinel_is_disarmed(self, tmp_path):
        prepare = mock.Mock(return_value=(True, "/spec/STOP"))
        with mock.patch.object(r, "_prepare_handoff", prepare):
            out = await r._handle_handoff(
                _handoff_request(tmp_path, _State(), spec_dir="/somewhere-else")
            )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        # _prepare_handoff CLEARS the STOP sentinel a Pause wrote.
        prepare.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_missing_tasks_file_is_409(self, tmp_path):
        with mock.patch.object(r, "_prepare_handoff", return_value=(False, "")):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "tasks_missing"

    @pytest.mark.asyncio
    async def test_a_spec_replaced_during_the_filesystem_hop_is_409(self, tmp_path):
        def _prepare_then_replace(*_a, **_kw):
            r._save_index(
                {"demo": _entry(tmp_path / "demo", slot_key="spec-builder-demo-ffffffff")}
            )
            return True, "/spec/STOP"

        with mock.patch.object(r, "_prepare_handoff", _prepare_then_replace):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"

    @pytest.mark.asyncio
    async def test_no_autonudge_service_fails_closed_with_503(self, tmp_path, _quiet_sel):
        # Without the service this must fail closed, not swallow the failure and
        # run an autonomous turn WITHOUT passing the authorization chokepoint.
        with _ready_handoff():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 503 and _body(out)["code"] == "autonudge_unavailable"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_handoff_denied" in ops

    @pytest.mark.asyncio
    async def test_a_missing_authorization_helper_also_fails_closed(self, tmp_path):
        svc = mock.MagicMock()
        with (
            _ready_handoff(),
            mock.patch.object(r, "_autonudge_instance", lambda: svc),
            mock.patch.object(r, "authorize_and_add_nudge", None),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 503 and _body(out)["code"] == "autonudge_unavailable"

    @pytest.mark.asyncio
    async def test_a_second_handoff_is_409_rather_than_a_second_dispatch(
        self, tmp_path, _armed, _no_dispatch
    ):
        with _ready_handoff(), _no_rehydrate():
            first = await r._handle_handoff(_handoff_request(tmp_path, _State()))
            assert first.status == 200
            second = await r._handle_handoff(
                _mk(
                    "POST",
                    "specs/demo/handoff",
                    state=_State(),
                    match={"name": "demo"},
                    body={},
                )
            )
        assert second.status == 409 and _body(second)["code"] == "already_executing"

    @pytest.mark.asyncio
    async def test_a_claim_that_cannot_be_recorded_is_500_and_starts_nothing(
        self, tmp_path, _armed, _no_dispatch
    ):
        # Pause keys off the recorded state, so the run must not proceed without it.
        with (
            _ready_handoff(),
            mock.patch.object(r, "_claim_execution", mock.AsyncMock(side_effect=OSError("disk"))),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 500 and _body(out)["code"] == "exec_state_write_failed"
        _no_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_spec_that_disappears_at_the_claim_is_409(self, tmp_path, _armed):
        with (
            _ready_handoff(),
            mock.patch.object(
                r, "_claim_execution", mock.AsyncMock(return_value=(r._CLAIM_GONE, {}))
            ),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"

    @pytest.mark.asyncio
    async def test_a_foreign_slot_gives_the_claim_back(self, tmp_path, _armed, _no_dispatch):
        with (
            _ready_handoff(),
            mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=None)),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"
        # Otherwise the spec stays marked executing with nothing running.
        stored = r._load_index()["demo"]
        assert stored["status"] == "planning" and stored["exec_arming_at"] == 0.0

    @pytest.mark.asyncio
    async def test_an_authorization_that_raises_is_503_and_unwinds(
        self, tmp_path, _armed, _no_dispatch, _quiet_sel
    ):
        _armed.authz.side_effect = RuntimeError("authz exploded")
        with _ready_handoff(), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 503 and _body(out)["code"] == "authorization_failed"
        assert r._load_index()["demo"]["status"] == "planning"
        _no_dispatch.assert_not_called()
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_handoff_aborted" in ops

    @pytest.mark.asyncio
    async def test_a_refused_authorization_is_403_and_unwinds(self, tmp_path, _armed, _no_dispatch):
        _armed.authz.return_value = (None, "message limit reached", 0)
        with _ready_handoff(), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 403 and _body(out)["code"] == "authorization_refused"
        assert "message limit reached" in _body(out)["error"]
        assert r._load_index()["demo"]["status"] == "planning"

    @pytest.mark.asyncio
    async def test_a_pre_existing_slot_is_not_torn_down_by_the_unwind(
        self, tmp_path, _armed, _no_dispatch
    ):
        # A pre-existing slot carries the user's conversation; destroying it
        # because a later write failed loses work the handoff never owned.
        _armed.authz.return_value = (None, "refused", 0)
        slot = _Slot("spec-builder-demo-0123abcd")
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with _ready_handoff(), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, state))
        assert out.status == 403
        assert state._slots["spec-builder-demo-0123abcd"] is slot

    @pytest.mark.asyncio
    async def test_a_delete_landing_during_authorization_is_409_and_removes_the_loop(
        self, tmp_path, _armed, _no_dispatch
    ):
        async def _authz_then_delete(**_kw):
            r._save_index({})
            return SimpleNamespace(id="loop-1"), None, 0

        _armed.authz.side_effect = _authz_then_delete
        remove = mock.AsyncMock()
        with (
            _ready_handoff(),
            _no_rehydrate(),
            mock.patch.object(r, "_remove_nudge_loop_for_slot", remove),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"
        # Ours arrives after the delete's own by-name teardown, so it must be
        # removed here or it nudges a spec that is already gone.
        assert remove.await_args.kwargs["only_loop_id"] == "loop-1"

    @pytest.mark.asyncio
    async def test_a_successful_handoff_arms_a_bounded_loop_and_dispatches(
        self, tmp_path, _armed, _no_dispatch
    ):
        with _ready_handoff("/spec/STOP"), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert _body(out) == {"ok": True, "status": "executing"}
        kwargs = _armed.authz.await_args.kwargs
        # Bounded: svc.add was called directly once, with max_cycles=0 -- an
        # unbounded loop. The shared chokepoint is what enforces the cap.
        assert kwargs["max_cycles"] == r._EXEC_MAX_CYCLES
        assert kwargs["stop_sentinel_path"] == "/spec/STOP"
        assert kwargs["source"] == "app:spec-builder"
        assert kwargs["caller"] == _OWNER_USER
        stored = r._load_index()["demo"]
        assert stored["status"] == "executing"
        # The pre-arm exemption ends once the reconciler can see the loop.
        assert stored["exec_arming_at"] == 0.0
        prompt = _no_dispatch.call_args.args[2]
        assert "EXECUTION HANDOFF" in prompt and "tasks.md" in prompt


# -- HTTP: stop ---------------------------------------------------------------


class TestHandleStopExecution:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_stop_execution(
            _mk("POST", "specs/demo/stop", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_stop_execution(
            _mk("POST", "specs/demo/stop", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_stale_claim_is_refused_before_anything_is_halted(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        halt = mock.AsyncMock()
        with mock.patch.object(r, "_halt_execution", halt):
            out = await r._handle_stop_execution(
                _mk(
                    "POST",
                    "specs/demo/stop",
                    state=_State(),
                    match={"name": "demo"},
                    body={"slot_key": "spec-builder-demo-ffffffff"},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        halt.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_halt_is_503_rather_than_a_false_stopped(self, tmp_path, _quiet_sel):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        with mock.patch.object(
            r, "_halt_execution", mock.AsyncMock(side_effect=OSError("store down"))
        ):
            out = await r._handle_stop_execution(
                _mk("POST", "specs/demo/stop", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "stop_failed"
        # Saying "stopped" would be false and the user would not retry.
        assert r._load_index()["demo"]["status"] == "executing"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_stop_failed" in ops

    @pytest.mark.asyncio
    async def test_a_spec_deleted_while_halting_is_404_not_a_resurrected_entry(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})

        async def _halt_then_delete(*_a, **_kw):
            r._save_index({})

        with mock.patch.object(r, "_halt_execution", _halt_then_delete):
            out = await r._handle_stop_execution(
                _mk("POST", "specs/demo/stop", state=_State(), match={"name": "demo"})
            )
        assert out.status == 404
        assert r._load_index() == {}

    @pytest.mark.asyncio
    async def test_a_successful_stop_records_planning(self, tmp_path, _quiet_sel):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        halt = mock.AsyncMock()
        with mock.patch.object(r, "_halt_execution", halt):
            out = await r._handle_stop_execution(
                _mk(
                    "POST",
                    "specs/demo/stop",
                    state=_State(),
                    match={"name": "demo"},
                    body={
                        "spec_dir": str(tmp_path / "demo"),
                        "slot_key": "spec-builder-demo-0123abcd",
                    },
                )
            )
        assert _body(out) == {"ok": True, "status": "planning"}
        assert r._load_index()["demo"]["status"] == "planning"
        assert halt.await_args.kwargs["expect_slot_key"] == "spec-builder-demo-0123abcd"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_stop_execution" in ops


# -- HTTP: delete -------------------------------------------------------------


class TestHandleDelete:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_delete(
            _mk("DELETE", "specs/demo", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_delete(
            _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_stale_claim_is_refused_and_leaves_no_tombstone(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_delete(
            _mk(
                "DELETE",
                "specs/demo",
                state=_State(),
                match={"name": "demo"},
                query="spec_dir=/somewhere-else",
            )
        )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        assert "demo" in r._load_index()

    @pytest.mark.asyncio
    async def test_a_reservation_that_cannot_be_taken_is_404_and_clears_the_tombstone(
        self, tmp_path
    ):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(r, "_mark_deleting", mock.AsyncMock(return_value=False)):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 404
        # Leaving it would hide a spec the user still has from their own list.
        assert r._load_deleted() == []

    @pytest.mark.asyncio
    async def test_a_failed_loop_removal_aborts_the_delete_and_releases_everything(
        self, tmp_path, _quiet_sel
    ):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(
            r, "_remove_nudge_loop", mock.AsyncMock(side_effect=OSError("store unwritable"))
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "loop_removal_failed"
        stored = r._load_index()["demo"]
        assert r._DELETING not in stored
        assert r._load_deleted() == []
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_delete_aborted" in ops

    @pytest.mark.asyncio
    async def test_a_failed_archive_aborts_the_delete(self, tmp_path):
        # The conversation is the user's data; a failed history write must abort
        # the delete, not be logged at DEBUG while the delete returns 200.
        _write_index({"demo": _entry(tmp_path / "demo")})
        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=False)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "archive_failed"
        assert "nothing was deleted" in _body(out)["error"]
        assert r._DELETING not in r._load_index()["demo"]
        assert r._load_deleted() == []

    @pytest.mark.asyncio
    async def test_an_archive_failure_whose_reservation_also_sticks_says_so(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=False)),
            mock.patch.object(r, "_unmark_deleting", mock.AsyncMock(return_value=False)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503
        assert "may need a reload" in _body(out)["error"]

    @pytest.mark.asyncio
    async def test_an_archived_spec_whose_record_survives_is_503_not_an_undelete(self, tmp_path):
        # Un-deleting would be the lie the ordering exists to prevent: the
        # conversation is ALREADY archived. The reservation stays so a retry is
        # idempotent.
        _write_index({"demo": _entry(tmp_path / "demo")})
        with (
            mock.patch.object(r, "_mark_deleting", mock.AsyncMock(return_value=True)),
            mock.patch.object(r, "_commit_delete_teardown", mock.AsyncMock(return_value=True)),
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=True)),
            mock.patch.object(r, "_mutate_index", mock.AsyncMock(return_value=False)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "index_write_failed"

    @pytest.mark.asyncio
    async def test_a_successful_delete_tombstones_the_directory_and_drops_the_entry(
        self, tmp_path, _quiet_sel
    ):
        spec = tmp_path / "demo"
        spec.mkdir()
        (spec / "tasks.md").write_text("- [x] done")
        _write_index({"demo": _entry(spec)})
        slot = _Slot("spec-builder-demo-0123abcd")
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()),
        ):
            out = await r._handle_delete(
                _mk(
                    "DELETE",
                    "specs/demo",
                    state=state,
                    match={"name": "demo"},
                    query=f"spec_dir={spec}&slot_key=spec-builder-demo-0123abcd",
                )
            )
        assert _body(out) == {"ok": True}
        assert r._load_index() == {}
        assert r._load_deleted() == [str(spec)]
        # The .md files are the user's own project files -- they stay.
        assert (spec / "tasks.md").is_file()
        assert state._slots == {}
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_delete" in ops

    @pytest.mark.asyncio
    async def test_the_runtime_is_captured_only_after_the_name_is_reserved(self, tmp_path):
        """With the marker set first, a concurrent message cannot materialize a new
        slot that this capture has already passed."""
        _write_index({"demo": _entry(tmp_path / "demo")})
        order: list[str] = []
        real_mark = r._mark_deleting

        async def _mark(*a, **kw):
            order.append("reserved")
            return await real_mark(*a, **kw)

        def _loop_id(name):
            order.append("captured")
            return None

        with (
            mock.patch.object(r, "_mark_deleting", _mark),
            mock.patch.object(r, "_exec_loop_id", _loop_id),
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=True)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 200
        assert order == ["reserved", "captured"]


# -- route registration -------------------------------------------------------


class TestRegisterRoutes:
    def test_test_facade_patches_owner_and_captured_consumer_bindings(self):
        original_owner = runtime_module._dispatch_turn
        original_consumer = handler_module._dispatch_turn
        replacement = object()

        with mock.patch.object(r, "_dispatch_turn", replacement):
            assert runtime_module._dispatch_turn is replacement
            assert handler_module._dispatch_turn is replacement

        assert runtime_module._dispatch_turn is original_owner
        assert handler_module._dispatch_turn is original_consumer

    def test_every_documented_route_is_registered_and_gated(self):
        app = web.Application()
        r.register_routes(app)
        routes = [route for route in app.router.routes() if route.method != "HEAD"]
        gate_code = r._require_enabled(mock.AsyncMock()).__code__
        # functools.wraps deliberately preserves the raw handler's name, so the
        # route-name contract alone cannot prove the enablement gate is present.
        assert all(getattr(route.handler, "__code__", None) is gate_code for route in routes)
        registered = {
            (route.method, route.resource.canonical) for route in routes  # type: ignore[union-attr]
        }
        base = f"/api/apps/{r.APP_NAME}"
        assert registered == {
            ("GET", f"{base}/settings"),
            ("PUT", f"{base}/settings"),
            # POST alias: the SPA page uses POST for settings writes.
            ("POST", f"{base}/settings"),
            ("GET", f"{base}/repo-info"),
            ("GET", f"{base}/browse"),
            ("GET", f"{base}/specs"),
            ("POST", f"{base}/specs"),
            ("GET", f"{base}/specs/{{name}}"),
            ("GET", f"{base}/specs/{{name}}/messages"),
            ("POST", f"{base}/specs/{{name}}/recover-decision"),
            ("POST", f"{base}/specs/{{name}}/message"),
            ("POST", f"{base}/specs/{{name}}/handoff"),
            # Alias: the SPA page calls this "execute".
            ("POST", f"{base}/specs/{{name}}/execute"),
            ("POST", f"{base}/specs/{{name}}/stop"),
            ("DELETE", f"{base}/specs/{{name}}"),
            # The user's own authority over the artifacts, rather than asking the
            # agent for every change: record an approval of the version on screen,
            # run ONE task from tasks.md, and rename/archive/duplicate the spec.
            ("POST", f"{base}/specs/{{name}}/approve"),
            ("POST", f"{base}/specs/{{name}}/task"),
            ("POST", f"{base}/specs/{{name}}/title"),
            ("POST", f"{base}/specs/{{name}}/archive"),
            ("POST", f"{base}/specs/{{name}}/duplicate"),
        }

    def test_automatic_head_routes_cover_each_get_path(self):
        app = web.Application()
        r.register_routes(app)
        routes = list(app.router.routes())
        paths_by_method = {
            method: {
                route.resource.canonical  # type: ignore[union-attr]
                for route in routes
                if route.method == method
            }
            for method in ("GET", "HEAD")
        }
        assert paths_by_method["HEAD"] == paths_by_method["GET"]

    def test_registration_creates_nothing_on_disk(self, tmp_path):
        # This runs during start_dashboard ON THE EVENT LOOP, so a KIROCREW_HOME on
        # stalled network storage would freeze gateway startup on a directory the
        # app may never need.
        r.register_routes(web.Application())
        assert not r._state_dir().exists()

    @pytest.mark.asyncio
    async def test_the_handoff_and_execute_aliases_are_the_same_handler(self):
        app = web.Application()
        r.register_routes(app)
        handlers = {
            route.resource.canonical: route.handler  # type: ignore[union-attr]
            for route in app.router.routes()
            if route.method == "POST"
        }
        base = f"/api/apps/{r.APP_NAME}"
        assert (
            handlers[f"{base}/specs/{{name}}/handoff"].__name__
            == handlers[f"{base}/specs/{{name}}/execute"].__name__
            == "_handle_handoff"
        )


# -- failure branches the happy paths never reach -----------------------------


class TestFilesystemErrorsAreSwallowed:
    def test_an_entry_that_cannot_be_probed_is_skipped_not_fatal(self, tmp_path):
        (tmp_path / "src").mkdir()
        with mock.patch.object(r, "is_sensitive_path", side_effect=OSError("stale mount")):
            assert r._scan_subdirs(str(tmp_path)) == []

    def test_a_spec_file_that_cannot_be_probed_is_refused(self, tmp_path):
        (tmp_path / "tasks.md").write_text("x")
        with mock.patch.object(r, "is_sensitive_path", side_effect=OSError("stale mount")):
            assert r._spec_file(tmp_path, "tasks.md") is None

    def test_a_spec_dir_that_cannot_be_probed_does_not_verify(self, tmp_path):
        with mock.patch.object(r, "is_sensitive_path", side_effect=OSError("stale mount")):
            assert r._verified_spec_dir(tmp_path) is None

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_directory_that_cannot_be_pinned_fails_the_sentinel_write(self, tmp_path):
        with mock.patch.object(os, "open", side_effect=OSError("EACCES")):
            assert r._write_stop_sentinel(tmp_path) is False

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_failed_sentinel_write_cleans_up_its_temp_file(self, tmp_path):
        real_open = os.open
        calls: list[int] = []

        def _fail_the_temp(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:  # the directory pin succeeded; the temp create fails
                raise OSError("ENOSPC")
            return real_open(*args, **kwargs)

        with mock.patch.object(os, "open", _fail_the_temp):
            assert r._write_stop_sentinel(tmp_path) is False
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_directory_that_cannot_be_pinned_leaves_the_sentinel_alone(self, tmp_path):
        marker = tmp_path / r._STOP_FILE
        marker.write_text("1")
        with mock.patch.object(os, "open", side_effect=OSError("EACCES")):
            r._clear_stop_sentinel(tmp_path)
        assert marker.exists()


class TestPrepareGitSpawn:
    def test_it_passes_the_sandbox_triple_straight_through(self):
        argv = ["git", "-C", "/repo", "rev-parse"]
        with mock.patch.object(
            r, "sandboxed_spawn_argv", return_value=(["wrapped"], {"PATH": "/bin"}, "/tmp/env")
        ) as wrap:
            assert r._prepare_git_spawn(argv) == (["wrapped"], {"PATH": "/bin"}, "/tmp/env")
        wrap.assert_called_once_with(argv)


class TestHaltGitReapRace:
    @pytest.mark.asyncio
    async def test_a_process_that_dies_during_the_reap_is_not_an_error(self):
        proc = _FakeProc()
        proc.wait = mock.AsyncMock(side_effect=ProcessLookupError())  # type: ignore
        await r._halt_git(proc, "worktree")
        assert proc.killed is True


class TestTeardownFailureBranches:
    @pytest.mark.asyncio
    async def test_a_registry_lookup_that_raises_reads_as_no_slot(self):
        state = _State()
        state.get_slot = mock.Mock(side_effect=RuntimeError("registry broken"))  # type: ignore
        assert await r._teardown_worker_slot(state, "demo") is True

    @pytest.mark.asyncio
    async def test_a_registry_pop_that_raises_does_not_stop_the_teardown(self):
        class _StubbornRegistry(dict):
            def pop(self, *_a, **_kw):
                raise RuntimeError("frozen registry")

        slot = _Slot("spec-builder-demo")
        state = _State()
        state._slots = _StubbornRegistry({"spec-builder-demo": slot})  # type: ignore[assignment]
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
        ) as save:
            assert await r._teardown_worker_slot(state, "demo") is True
        save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_worker_task_that_raises_while_being_cancelled_is_tolerated(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot.task = mock.Mock()  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _explode(*_a, **_kw):
            raise RuntimeError("teardown blew up")

        with (
            mock.patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()),
            mock.patch.object(asyncio, "wait_for", _explode),
        ):
            assert await r._teardown_worker_slot(state, "demo") is True

    @pytest.mark.asyncio
    async def test_a_slot_that_cannot_be_put_back_after_a_failed_archive_still_reports_false(self):
        class _NoWrites(dict):
            def __setitem__(self, *_a, **_kw):
                raise RuntimeError("frozen registry")

            def pop(self, key, default=None):
                return dict.pop(self, key, default)

        slot = _Slot("spec-builder-demo")
        state = _State()
        state._slots = _NoWrites({"spec-builder-demo": slot})  # type: ignore[assignment]
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
            mock.AsyncMock(side_effect=OSError("disk full")),
        ):
            assert await r._teardown_worker_slot(state, "demo", require_archive=True) is False

    @pytest.mark.asyncio
    async def test_a_worker_task_that_raises_while_pausing_is_tolerated(self):
        slot = _Slot("spec-builder-demo", running=True)
        task = mock.Mock()
        task.done.return_value = False
        slot.task = task  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _explode(*_a, **_kw):
            raise RuntimeError("pause blew up")

        with mock.patch.object(asyncio, "wait_for", _explode):
            assert await r._halt_active_turn(state, "demo") is True

    def test_a_slot_that_rejects_the_synthesis_reset_is_tolerated(self):
        class _NoSynthesis:
            def __setattr__(self, name, value):
                raise RuntimeError("frozen slot")

        r._discard_queued_work(_NoSynthesis())  # must not raise


class TestDeleteRaceBranches:
    @pytest.mark.asyncio
    async def test_an_entry_that_moved_under_the_reservation_is_not_popped(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})

        async def _teardown_then_move(*_a, **_kw):
            index = r._load_index()
            index["demo"]["spec_dir"] = str(tmp_path / "moved")
            r._save_index(index)
            return True

        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", _teardown_then_move),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "index_write_failed"
        # The reservation stays, which keeps the spec hidden and the retry idempotent.
        assert r._load_index()["demo"][r._DELETING]["owner"] == r._PROCESS_ID

    @pytest.mark.asyncio
    async def test_an_entry_whose_creation_changed_is_not_popped(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})

        async def _teardown_then_rekey(*_a, **_kw):
            index = r._load_index()
            index["demo"]["slot_key"] = "spec-builder-demo-ffffffff"
            r._save_index(index)
            return True

        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", _teardown_then_rekey),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "index_write_failed"


class TestHandoffUnwindFailureBranches:
    @pytest.mark.asyncio
    async def test_a_loop_that_cannot_be_removed_while_unwinding_is_logged_not_raised(
        self, tmp_path, _armed, _no_dispatch
    ):
        # This is already an abort path -- the reason that brought us here is the
        # story worth surfacing, so the removal stays best-effort HERE only.
        async def _authz_then_delete(**_kw):
            r._save_index({})
            return SimpleNamespace(id="loop-1"), None, 0

        _armed.authz.side_effect = _authz_then_delete
        with (
            _ready_handoff(),
            _no_rehydrate(),
            mock.patch.object(
                r, "_remove_nudge_loop", mock.AsyncMock(side_effect=OSError("store down"))
            ),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"

    @pytest.mark.asyncio
    async def test_an_execution_state_that_cannot_be_cleared_while_unwinding_is_logged(
        self, tmp_path, _armed, _no_dispatch
    ):
        _armed.authz.return_value = (None, "refused", 0)
        real_touch = r._touch_spec
        calls: list[int] = []

        async def _touch(*a, **kw):
            calls.append(1)
            if kw.get("status") == "planning":
                raise OSError("index unwritable")
            return await real_touch(*a, **kw)

        with _ready_handoff(), _no_rehydrate(), mock.patch.object(r, "_touch_spec", _touch):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        # The refusal the user needs to see survives the failed unwind.
        assert out.status == 403 and _body(out)["code"] == "authorization_refused"
