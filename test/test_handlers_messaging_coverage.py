"""Coverage tests for dashboard messaging handlers.

Focus: the request-validation, delivery-failure and status-code branches of
``kiro_crew.dashboard.handlers.messaging`` that the existing per-channel test
files (slack/webex/wecom/telegram/discord config, send-message, notifications
phase 5) never reach -- the subagent lifecycle routes, the notification
ack/unack/channel routes, the Slack pins/reactions proxies, the browser
event/frame/config routes and the Teams config API.

Handlers are driven through a lightweight request double (same shape as
``test_webex_config_handlers.py``) rather than a live TestServer: no sockets, no
subprocesses, no real Slack/HTTP, and every filesystem write lands in
``tmp_path``.

This file covers the subagent lifecycle routes and the result-paging helpers. The
notification, Slack, Teams and channel-config routes, the env writer, the token
validators, the small helpers and the module guards are tested in
``test_handlers_messaging_coverage_channels.py``; the request double and the response
helpers live in ``handlers_messaging_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_messaging_coverage_helpers import (
    _BAD_JSON,
    _payload,
    _Req,
    _run,
    _state,
)

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew.subagent import AGENT_NOT_FOUND_CODE


def _run_view(req: _Req, text: str) -> tuple[str, dict]:
    """Call the (privately typed) ``_apply_result_view`` with the request double."""
    view: Any = mod._apply_result_view
    return asyncio.run(view(req, text))


def _info(**kw: Any) -> Any:
    """A SubagentInfo double with defaults for every field the handlers read."""
    base: dict[str, Any] = {
        "id": "a1",
        "task": "do it",
        "done": False,
        "error": "",
        "error_code": "",
        "result": "",
        "result_path": "",
        "started": 1_700_000_000.0,
        "elapsed": 0.0,
        "credits": 0.0,
        "turns": 2,
        "last_tool": "fs_read",
        "parent_session_key": "dashboard:chat-1",
        "agent": "kirocrew",
        # The retry path reuses this alongside the context triple: a retry must
        # not widen a delegated run to the global store.
        "memory_store": "",
        "crew": "",
        "user_stopped": False,
        "outcome": "",
        "max_turns": 0,
        "cwd": "",
        "model": "",
        "reasoning_effort": "",
        "approval_mode": "",
        "silent": False,
        "delegation": {},
        "_raw_task": "",
        "include_memory": True,
        "include_lessons": True,
        "include_project": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _mgr(**kw: Any) -> Any:
    mgr = MagicMock()
    mgr.max_concurrent = 4
    mgr.all_agents = []
    mgr._agents = {}
    mgr._tasks = {}
    mgr.get.return_value = None
    mgr.settle_before_delete = AsyncMock(return_value="delivered")
    for key, val in kw.items():
        setattr(mgr, key, val)
    return mgr


# ── api_spawn ──


class TestApiSpawn:
    def test_503_without_subagent_manager(self) -> None:
        resp = _run(mod.api_spawn, _Req(_state(), {"task": "x"}))
        assert resp.status == 503
        assert _payload(resp)["error"] == "subagents not available"

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_spawn, _Req(_state(subagents=_mgr()), _BAD_JSON))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_on_schema_violation(self) -> None:
        """A bad agent name is rejected by SPAWN_RUN_SCHEMA, not by the manager."""
        mgr = _mgr()
        resp = _run(mod.api_spawn, _Req(_state(subagents=mgr), {"task": "x", "agent": "bad name!"}))
        assert resp.status == 400
        mgr.spawn.assert_not_called()

    def test_400_on_blank_task(self) -> None:
        resp = _run(mod.api_spawn, _Req(_state(subagents=_mgr()), {"task": "   "}))
        assert _payload(resp)["error"] == "task is required"

    def test_400_on_unknown_approval_mode(self) -> None:
        req = _Req(_state(subagents=_mgr()), {"task": "x", "approval_mode": "yolo"})
        resp = _run(mod.api_spawn, req)
        assert resp.status == 400
        assert "approval_mode" in _payload(resp)["error"]

    def test_400_on_non_alphanumeric_batch_id(self) -> None:
        req = _Req(_state(subagents=_mgr()), {"task": "x", "batch_id": "wave-1"})
        resp = _run(mod.api_spawn, req)
        assert resp.status == 400
        assert _payload(resp)["error"] == "batch_id must be alphanumeric"

    def test_capacity_refusal_reports_counted(self) -> None:
        """429 must carry ``counted`` so spawn_run does not re-reconcile."""
        mgr = _mgr()
        mgr.spawn.return_value = None
        resp = _run(mod.api_spawn, _Req(_state(subagents=mgr), {"task": "x"}))
        assert resp.status == 429
        assert _payload(resp)["counted"] is True

    def test_inline_rejection_reports_counted(self) -> None:
        mgr = _mgr()
        mgr.spawn.return_value = _info(done=True, error="cwd not allowed")
        resp = _run(mod.api_spawn, _Req(_state(subagents=mgr), {"task": "x"}))
        assert resp.status == 400
        # An un-coded rejection kind reports the generic identifier, so the body
        # is machine-readable even where the manager mints nothing.
        assert _payload(resp) == {
            "error": "cwd not allowed",
            "code": "spawn_rejected",
            "counted": True,
        }

    def test_unknown_agent_rejection_carries_its_own_code(self) -> None:
        """The one rejection a client acts on differently keeps its own identifier:
        ``spawn_run`` stops re-posting a name the gateway already refused, and it
        must not have to parse the prose to know which refusal this was."""
        mgr = _mgr()
        mgr.spawn.return_value = _info(
            done=True,
            error="agent 'ghost' not found; available: scout",
            error_code=AGENT_NOT_FOUND_CODE,
        )
        resp = _run(mod.api_spawn, _Req(_state(subagents=mgr), {"task": "x", "agent": "ghost"}))
        assert resp.status == 400
        body = _payload(resp)
        assert body["code"] == AGENT_NOT_FOUND_CODE
        # Prose still travels for the model to read and self-correct from.
        assert "available: scout" in body["error"]

    def test_success_coerces_string_flags_and_bounds_batch_total(self) -> None:
        mgr = _mgr()
        mgr.spawn.return_value = _info(id="a9")
        req = _Req(
            _state(subagents=mgr),
            {
                "task": "  build it  ",
                "silent": "yes",
                "keep": "true",
                "batch_id": "wave1",
                "batch_total": "9999",
            },
        )
        resp = _run(mod.api_spawn, req)
        assert resp.status == 200
        assert _payload(resp) == {
            "id": "a9",
            "task": "build it",
            "status": "spawned",
            "conversation": "a9",
            "parent_work_supported": False,
        }
        kwargs = mgr.spawn.call_args.kwargs
        assert kwargs["silent"] is True
        assert kwargs["keep"] is True
        assert kwargs["batch_total"] == 1000

    def test_unparsable_batch_total_falls_back_to_zero(self) -> None:
        mgr = _mgr()
        mgr.spawn.return_value = _info()
        req = _Req(_state(subagents=mgr), {"task": "x", "batch_total": "many"})
        assert _run(mod.api_spawn, req).status == 200
        assert mgr.spawn.call_args.kwargs["batch_total"] == 0

    @pytest.mark.parametrize("raw", ["1e999", "-1e999"])
    def test_infinite_batch_total_falls_back_to_zero(self, raw: str) -> None:
        """JSON reads ``1e999`` as an infinite float, which ``int`` cannot convert."""
        mgr = _mgr()
        mgr.spawn.return_value = _info()
        body = json.loads('{"task": "x", "batch_id": "b1", "batch_total": %s}' % raw)
        assert _run(mod.api_spawn, _Req(_state(subagents=mgr), body)).status == 200
        assert mgr.spawn.call_args.kwargs["batch_total"] == 0

    def test_a_deferred_row_answers_queued_with_the_gate_reason(self) -> None:
        """The memory guard parked the row: the caller is told it WAITS and why,
        under the same ``id`` (the wave reconcile and the run card key on it)."""
        mgr = _mgr()
        mgr.spawn.return_value = _info(
            id="q1",
            queued=True,
            queued_reason="low_memory",
            queued_reason_detail="low memory: 3.2 GB available, need 4 GB",
        )
        resp = _run(mod.api_spawn, _Req(_state(subagents=mgr), {"task": "x"}))
        assert resp.status == 200
        body = _payload(resp)
        assert body["id"] == "q1"
        assert body["status"] == "queued"
        assert body["reason"] == "low_memory"
        assert body["reason_detail"] == "low memory: 3.2 GB available, need 4 GB"

    def test_a_capacity_queued_row_still_answers_spawned(self) -> None:
        """Waiting behind the cap for a stagger tick is the ordinary wave shape;
        its wire answer does not change."""
        mgr = _mgr()
        mgr.spawn.return_value = _info(id="q2", queued=True, queued_reason="concurrency_limit")
        body = _payload(_run(mod.api_spawn, _Req(_state(subagents=mgr), {"task": "x"})))
        assert body["status"] == "spawned"
        assert "reason" not in body

    @pytest.mark.parametrize("source", ["crew", "subagent"])
    @pytest.mark.parametrize("unavailable", [False, True])
    def test_execution_record_lookup_runs_off_loop_before_spawn(
        self, monkeypatch, source: str, unavailable: bool
    ) -> None:
        from kiro_crew.execution_context import read_session_execution
        from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory
        from kiro_crew.subagent_persistence import create_agent_folder

        cfg = loader.KiroCrewConfig.load()
        cfg.agents["worker"] = loader.KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="work")
        store = provision_member_memory(cfg, "worker")
        cfg.save()
        mgr = _mgr()
        mgr.spawn.return_value = _info()
        state = _state(subagents=mgr, conversation_log=None, sessions=SimpleNamespace(_pool_cwd=""))
        body = {"task": "read the assigned memory"}
        loop_thread = threading.get_ident()
        lookup_threads = []

        if source == "crew":
            body["crew"] = "worker"

            def load_config():
                lookup_threads.append(threading.get_ident())
                if unavailable:
                    raise UnknownMemoryStore("member memory cannot be read")
                return cfg

            monkeypatch.setattr(loader.KiroCrewConfig, "load", load_config)
        else:
            run_id = "offloop-parent"
            create_agent_folder(run_id, memory_store=store)
            body["parent_session"] = f"subagent:{run_id}"

            def inherited(session_key):
                lookup_threads.append(threading.get_ident())
                assert session_key == f"subagent:{run_id}"
                if unavailable:
                    raise UnknownMemoryStore("member memory cannot be read")
                return read_session_execution(session_key)

            monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", inherited)

        response = _run(mod.api_spawn, _Req(state, body))

        # A parent's privacy admission reads its execution before spawn derives
        # the child's identity. An unreadable parent stops at that first gate.
        expected_lookups = 2 if source == "subagent" and not unavailable else 1
        assert len(lookup_threads) == expected_lookups
        assert all(thread != loop_thread for thread in lookup_threads)
        if unavailable:
            assert response.status == 409
            if source == "subagent":
                assert _payload(response) == {
                    "code": "memory_unavailable",
                    "error": "The originating session's memory mode is unavailable.",
                }
            else:
                assert _payload(response) == {
                    "code": "member_identity_unavailable",
                    "error": "member memory cannot be read",
                }
            mgr.spawn.assert_not_called()
        else:
            assert response.status == 200
            mgr.spawn.assert_called_once()
            assert mgr.spawn.call_args.kwargs["memory_store"] == store
            assert (
                mgr.spawn.call_args.kwargs["_execution_context"]["member_id"]
                == cfg.agents["worker"].member_id
            )
            assert mgr.spawn.call_args.kwargs["parent_session_key"] == body.get(
                "parent_session", ""
            )


# ── api_spawn_continue ──


class TestApiSpawnContinue:
    def _req(self, mgr: Any, body: Any) -> _Req:
        return _Req(_state(subagents=mgr), body, match_info={"agent_id": "conv1"})

    def test_503_without_manager(self) -> None:
        req = _Req(_state(), {"task": "x"}, match_info={"agent_id": "conv1"})
        resp = _run(mod.api_spawn_continue, req)
        assert resp.status == 503
        assert _payload(resp)["code"] == "subagents_unavailable"

    def test_400_invalid_json(self) -> None:
        resp = _run(mod.api_spawn_continue, self._req(_mgr(), _BAD_JSON))
        assert _payload(resp)["code"] == "invalid_json"

    def test_400_task_required(self) -> None:
        resp = _run(mod.api_spawn_continue, self._req(_mgr(), {"task": ""}))
        assert _payload(resp)["code"] == "task_required"

    def test_429_capacity(self) -> None:
        mgr = _mgr()
        mgr.continue_conversation.return_value = None
        resp = _run(mod.api_spawn_continue, self._req(mgr, {"task": "x"}))
        assert resp.status == 429
        assert _payload(resp)["code"] == "capacity_reached"

    @pytest.mark.parametrize(
        "error,status,code",
        [
            ("conversation_busy: run in flight", 409, "conversation_busy"),
            ("conversation_gone: expired", 404, "conversation_gone"),
            ("resume_failed", 400, "spawn_rejected"),
        ],
    )
    def test_typed_failures_map_to_status(self, error: str, status: int, code: str) -> None:
        mgr = _mgr()
        mgr.continue_conversation.return_value = _info(done=True, error=error)
        resp = _run(mod.api_spawn_continue, self._req(mgr, {"task": "x"}))
        assert resp.status == status
        assert _payload(resp)["code"] == code

    def test_success_clamps_max_turns_and_echoes_conversation(self) -> None:
        mgr = _mgr()
        mgr.continue_conversation.return_value = _info(id="run2")
        resp = _run(mod.api_spawn_continue, self._req(mgr, {"task": "x", "max_turns": 5000}))
        assert _payload(resp) == {"id": "run2", "conversation": "conv1", "status": "spawned"}
        assert mgr.continue_conversation.call_args.kwargs["max_turns"] == 1000

    def test_unparsable_max_turns_falls_back_to_zero(self) -> None:
        mgr = _mgr()
        mgr.continue_conversation.return_value = _info()
        resp = _run(mod.api_spawn_continue, self._req(mgr, {"task": "x", "max_turns": "lots"}))
        assert resp.status == 200
        assert mgr.continue_conversation.call_args.kwargs["max_turns"] == 0

    @pytest.mark.parametrize("raw", ["1e999", "-1e999"])
    def test_infinite_max_turns_falls_back_to_zero(self, raw: str) -> None:
        """JSON reads ``1e999`` as an infinite float, which ``int`` cannot convert."""
        mgr = _mgr()
        mgr.continue_conversation.return_value = _info()
        body = json.loads('{"task": "x", "max_turns": %s}' % raw)
        assert _run(mod.api_spawn_continue, self._req(mgr, body)).status == 200
        assert mgr.continue_conversation.call_args.kwargs["max_turns"] == 0

    def test_the_runs_own_cwd_is_resolved_off_loop_and_forwarded(self) -> None:
        """A continuation has to run where the run ran, or a project-local agent
        fails to resolve and the caller respawns from a digest -- losing the
        conversation. `continue_conversation` is synchronous and on the event loop,
        so the lookup happens here, in a thread, and is passed in.
        """
        mgr = _mgr()
        mgr.recorded_cwd = MagicMock(return_value="/proj/alpha")
        mgr.continue_conversation.return_value = _info(id="run2")
        resp = _run(mod.api_spawn_continue, self._req(mgr, {"task": "x"}))
        assert resp.status == 200
        assert mgr.continue_conversation.call_args.kwargs["cwd"] == "/proj/alpha"
        mgr.recorded_cwd.assert_called_once_with("conv1")


# ── api_spawn_steer / release ──


class TestApiSpawnSteer:
    def _req(self, mgr: Any, body: Any) -> _Req:
        return _Req(_state(subagents=mgr), body, match_info={"agent_id": "a1"})

    def test_503_without_manager(self) -> None:
        req = _Req(_state(), {"message": "m"}, match_info={"agent_id": "a1"})
        assert _run(mod.api_spawn_steer, req).status == 503

    def test_400_invalid_json(self) -> None:
        resp = _run(mod.api_spawn_steer, self._req(_mgr(), _BAD_JSON))
        assert _payload(resp)["code"] == "invalid_json"

    def test_400_message_required(self) -> None:
        resp = _run(mod.api_spawn_steer, self._req(_mgr(), {"message": "  "}))
        assert _payload(resp)["code"] == "message_required"

    @pytest.mark.parametrize(
        "detail,status,code",
        [
            ("not_found", 404, "not_found"),
            ("not_running: terminal", 409, "not_running"),
            ("session_starting", 503, "session_starting"),
            ("boom", 502, "steer_failed"),
        ],
    )
    def test_failure_details_map_to_status(self, detail: str, status: int, code: str) -> None:
        mgr = _mgr(steer_run=AsyncMock(return_value=(False, detail)))
        resp = _run(mod.api_spawn_steer, self._req(mgr, {"message": "m"}))
        assert resp.status == status
        assert _payload(resp)["code"] == code

    def test_session_starting_sets_retry_after(self) -> None:
        mgr = _mgr(steer_run=AsyncMock(return_value=(False, "session_starting")))
        resp = _run(mod.api_spawn_steer, self._req(mgr, {"message": "m"}))
        assert resp.headers["Retry-After"] == "5"

    def test_success(self) -> None:
        mgr = _mgr(steer_run=AsyncMock(return_value=(True, "")))
        resp = _run(mod.api_spawn_steer, self._req(mgr, {"message": "m"}))
        assert _payload(resp) == {"id": "a1", "status": "steered"}


class TestApiSpawnRelease:
    def _req(self, mgr: Any) -> _Req:
        return _Req(_state(subagents=mgr), None, match_info={"agent_id": "conv1"})

    def test_503_without_manager(self) -> None:
        req = _Req(_state(), None, match_info={"agent_id": "conv1"})
        assert _run(mod.api_spawn_release, req).status == 503

    def test_409_while_busy(self) -> None:
        mgr = _mgr()
        mgr.release_conversation_async = AsyncMock(
            return_value=(False, "conversation_busy: in flight")
        )
        resp = _run(mod.api_spawn_release, self._req(mgr))
        assert resp.status == 409
        assert _payload(resp)["code"] == "conversation_busy"

    def test_404_when_gone(self) -> None:
        mgr = _mgr()
        mgr.release_conversation_async = AsyncMock(return_value=(False, "conversation_gone"))
        assert _run(mod.api_spawn_release, self._req(mgr)).status == 404

    def test_success(self) -> None:
        mgr = _mgr()
        mgr.release_conversation_async = AsyncMock(return_value=(True, ""))
        resp = _run(mod.api_spawn_release, self._req(mgr))
        assert _payload(resp) == {"conversation": "conv1", "status": "released"}


# ── api_spawn_lost / mark-collected ──


class TestApiSpawnLost:
    def test_503_without_manager(self) -> None:
        assert _run(mod.api_spawn_lost, _Req(_state(), {"batch_id": "w1"})).status == 503

    def test_400_invalid_json(self) -> None:
        resp = _run(mod.api_spawn_lost, _Req(_state(subagents=_mgr()), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.parametrize("batch_id", ["", "wave-1"])
    def test_400_on_bad_batch_id(self, batch_id: str) -> None:
        req = _Req(_state(subagents=_mgr()), {"batch_id": batch_id})
        resp = _run(mod.api_spawn_lost, req)
        assert resp.status == 400
        assert _payload(resp)["error"] == "valid batch_id required"

    def test_reconciles_and_bounds_fields(self) -> None:
        mgr = _mgr()
        req = _Req(
            _state(subagents=mgr),
            {
                "batch_id": "w1",
                "batch_total": "abc",
                "reason": "r" * 400,
                "parent_session": "dashboard:chat-1",
            },
        )
        resp = _run(mod.api_spawn_lost, req)
        assert _payload(resp) == {"status": "reconciled", "batch_id": "w1"}
        args = mgr.record_lost_submission.call_args.args
        assert args[0] == "w1" and args[1] == 0
        assert len(args[2]) == 300

    @pytest.mark.parametrize("raw", ["1e999", "-1e999"])
    def test_infinite_batch_total_reconciles_as_zero(self, raw: str) -> None:
        """JSON reads ``1e999`` as an infinite float, which ``int`` cannot convert."""
        mgr = _mgr()
        body = json.loads('{"batch_id": "w1", "batch_total": %s}' % raw)
        resp = _run(mod.api_spawn_lost, _Req(_state(subagents=mgr), body))
        assert _payload(resp) == {"status": "reconciled", "batch_id": "w1"}
        assert mgr.record_lost_submission.call_args.args[1] == 0


class TestApiSpawnMarkCollected:
    def test_400_invalid_json(self) -> None:
        resp = _run(mod.api_spawn_mark_collected, _Req(_state(), _BAD_JSON))
        assert _payload(resp)["code"] == "invalid_json"

    @pytest.mark.parametrize("ids", [None, [], "a1"])
    def test_400_without_ids_array(self, ids: Any) -> None:
        resp = _run(mod.api_spawn_mark_collected, _Req(_state(), {"ids": ids}))
        assert resp.status == 400
        assert _payload(resp)["code"] == "ids_required"

    def test_no_slot_when_parent_is_not_a_dashboard_session(self) -> None:
        req = _Req(_state(), {"ids": ["a1"], "parent_session": "cron:job1"})
        assert _payload(_run(mod.api_spawn_mark_collected, req)) == {"status": "no_slot"}

    def test_no_slot_when_slot_is_gone(self) -> None:
        state = _state()
        state.get_slot.return_value = None
        req = _Req(state, {"ids": ["a1"], "parent_session": "dashboard:chat-1"})
        assert _payload(_run(mod.api_spawn_mark_collected, req)) == {"status": "no_slot"}

    def test_records_ids_bounded_and_skips_non_strings(self) -> None:
        slot = SimpleNamespace(_subagents_inline_collected=set(), _queue=[])
        state = _state()
        state.get_slot.return_value = slot
        ids: list[Any] = [f"a{i}" for i in range(250)] + ["", 7]
        req = _Req(state, {"ids": ids, "parent_session": "dashboard:chat-1"})
        resp = _run(mod.api_spawn_mark_collected, req)
        assert _payload(resp) == {"status": "ok", "marked": len(ids)}
        assert len(slot._subagents_inline_collected) == 200


# ── result paging helpers ──


class TestSpawnResultView:
    def test_offset_limit_and_has_more(self) -> None:
        text = "\n".join(f"line{i}" for i in range(10))
        view, meta = mod._spawn_result_view(text, 2, 3, "")
        assert view.splitlines() == ["line2", "line3", "line4"]
        assert meta == {
            "total_lines": 10,
            "offset": 2,
            "returned_lines": 3,
            "has_more": True,
        }

    def test_grep_filters_then_slices(self) -> None:
        text = "alpha\nBETA\ngamma\nbeta-two"
        view, meta = mod._spawn_result_view(text, 0, 0, "beta")
        assert view.splitlines() == ["BETA", "beta-two"]
        assert meta["matched_lines"] == 2
        assert meta["has_more"] is False

    def test_bad_regex_reports_grep_error(self) -> None:
        view, meta = mod._spawn_result_view("a\nb", 0, 0, "(unclosed")
        assert view == ""
        assert "invalid grep regex" in meta["grep_error"]

    def test_offset_past_end_returns_nothing(self) -> None:
        view, meta = mod._spawn_result_view("a\nb", 99, 0, "")
        assert view == ""
        assert meta["offset"] == 2 and meta["returned_lines"] == 0

    def test_limit_is_hard_capped(self) -> None:
        text = "\n".join(str(i) for i in range(2500))
        _, meta = mod._spawn_result_view(text, 0, 99_999, "")
        assert meta["returned_lines"] == mod._SPAWN_STATUS_MAX_LINES

    def test_apply_view_is_a_passthrough_without_params(self) -> None:
        text, meta = _run_view(_Req(), "body")
        assert (text, meta) == ("body", {})

    def test_apply_view_ignores_non_integer_params(self) -> None:
        req = _Req(query={"offset": "x", "limit": "y"})
        assert _run_view(req, "body") == ("body", {})

    def test_apply_view_honours_query_params(self) -> None:
        req = _Req(query={"offset": "1", "limit": "1"})
        text, meta = _run_view(req, "a\nb\nc")
        assert text == "b"
        assert meta["offset"] == 1


# ── api_spawn_status ──


class TestApiSpawnStatus:
    def test_503_without_manager(self) -> None:
        req = _Req(_state(), None, match_info={"agent_id": "a1"})
        assert _run(mod.api_spawn_status, req).status == 503

    def test_404_when_absent_from_memory_and_disk(self, monkeypatch) -> None:
        mgr = _mgr()
        mgr.get.return_value = None
        monkeypatch.setattr(mod, "read_state", lambda aid: None)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        resp = _run(mod.api_spawn_status, req)
        assert resp.status == 404
        assert _payload(resp)["error"] == "not found"

    def test_404_when_persistence_lookup_raises(self, monkeypatch) -> None:
        mgr = _mgr()
        mgr.get.return_value = None

        def _boom(aid: str) -> dict:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(mod, "read_state", _boom)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        assert _run(mod.api_spawn_status, req).status == 404

    def test_disk_fallback_returns_result_and_tombstone_cause(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "a1"
        agent_dir.mkdir()
        (agent_dir / "result.txt").write_text("all good\n", encoding="utf-8")
        (agent_dir / "tombstone.json").write_text(
            json.dumps({"cause": "orphaned by restart"}), encoding="utf-8"
        )
        mgr = _mgr()
        mgr.get.return_value = None
        monkeypatch.setattr(mod, "read_state", lambda aid: {"task": "t", "started": 1.0})
        monkeypatch.setattr(mod, "_agent_dir", lambda aid: agent_dir)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["done"] is True
        assert data["result"].strip() == "all good"
        assert "orphaned by restart" in data["error"]

    def test_disk_fallback_reads_tombstone_off_event_loop(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "a1"
        agent_dir.mkdir()
        caller_thread = threading.get_ident()
        reader_threads: list[int] = []

        def _read_tombstone(_agent_id: str) -> dict[str, object]:
            reader_threads.append(threading.get_ident())
            return {"cause": "delivered", "elapsed": 4.0, "credits": 0.5}

        mgr = _mgr()
        mgr.get.return_value = None
        monkeypatch.setattr(mod, "read_state", lambda aid: {"task": "t"})
        monkeypatch.setattr(mod, "read_tombstone", _read_tombstone)
        monkeypatch.setattr(mod, "_agent_dir", lambda aid: agent_dir)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})

        data = _payload(_run(mod.api_spawn_status, req))

        assert reader_threads and reader_threads[0] != caller_thread
        assert data["elapsed"] == 4.0
        assert data["credits"] == 0.5

    def test_disk_fallback_does_not_trust_agent_state_usage(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "a1"
        agent_dir.mkdir()
        mgr = _mgr()
        mgr.get.return_value = None
        monkeypatch.setattr(
            mod,
            "read_state",
            lambda aid: {"task": "t", "elapsed": 999.0, "credits": 999.0},
        )
        monkeypatch.setattr(
            mod,
            "read_tombstone",
            lambda aid: {"cause": "delivered", "elapsed": 4.0, "credits": 0.5},
        )
        monkeypatch.setattr(mod, "_agent_dir", lambda aid: agent_dir)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})

        data = _payload(_run(mod.api_spawn_status, req))

        assert data["elapsed"] == 4.0
        assert data["credits"] == 0.5

    @pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "0.5"])
    def test_disk_fallback_omits_unsafe_tombstone_usage(
        self, monkeypatch, tmp_path: Path, value: object
    ) -> None:
        agent_dir = tmp_path / "a1"
        agent_dir.mkdir()
        mgr = _mgr()
        mgr.get.return_value = None
        monkeypatch.setattr(mod, "read_state", lambda aid: {"task": "t"})
        monkeypatch.setattr(
            mod,
            "read_tombstone",
            lambda aid: {"cause": "delivered", "elapsed": value, "credits": value},
        )
        monkeypatch.setattr(mod, "_agent_dir", lambda aid: agent_dir)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})

        data = _payload(_run(mod.api_spawn_status, req))

        assert "elapsed" not in data
        assert "credits" not in data

    def test_disk_fallback_reports_unknown_cause_on_corrupt_tombstone(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        agent_dir = tmp_path / "a1"
        agent_dir.mkdir()
        (agent_dir / "tombstone.json").write_text("{not json", encoding="utf-8")
        mgr = _mgr()
        mgr.get.return_value = None
        monkeypatch.setattr(mod, "read_state", lambda aid: {"task": "t"})
        monkeypatch.setattr(mod, "_agent_dir", lambda aid: agent_dir)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["error"] == "Orphaned (unknown cause)"
        assert data["result"] == "_No result._"

    def test_disk_fallback_paging_adds_result_meta(self, monkeypatch, tmp_path: Path) -> None:
        agent_dir = tmp_path / "a1"
        agent_dir.mkdir()
        (agent_dir / "result.txt").write_text("l0\nl1\nl2", encoding="utf-8")
        mgr = _mgr()
        mgr.get.return_value = None
        monkeypatch.setattr(mod, "read_state", lambda aid: {"task": "t"})
        monkeypatch.setattr(mod, "_agent_dir", lambda aid: agent_dir)
        req = _Req(
            _state(subagents=mgr),
            None,
            match_info={"agent_id": "a1"},
            query={"offset": "1", "limit": "1"},
        )
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["result"] == "l1"
        assert data["result_meta"]["offset"] == 1
        assert data["error"] == ""

    def test_running_agent_reports_redacted_partial_transcript(self) -> None:
        streaming_text = "working\nsecret AKIAIOSFODNN7EXAMPLE\nstill working"
        mgr = _mgr()
        mgr.get.return_value = _info(done=False, streaming_text=streaming_text)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["done"] is False
        assert data["result"] == mod._redact(streaming_text)
        assert data["turns"] == 2 and data["last_tool"] == "fs_read"
        assert isinstance(data["elapsed"], int)

    def test_running_agent_pages_partial_transcript(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(done=False, streaming_text="l0\nl1\nl2")
        req = _Req(
            _state(subagents=mgr),
            None,
            match_info={"agent_id": "a1"},
            query={"offset": "1", "limit": "1"},
        )
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["done"] is False
        assert data["result"] == "l1"
        assert data["result_meta"] == {
            "total_lines": 3,
            "offset": 1,
            "returned_lines": 1,
            "has_more": True,
        }

    def test_done_agent_prefers_full_result_from_disk(self, tmp_path: Path) -> None:
        result_file = tmp_path / "result.txt"
        result_file.write_text("full transcript", encoding="utf-8")
        mgr = _mgr()
        mgr.get.return_value = _info(
            done=True,
            result="truncated",
            result_path=str(result_file),
            error="oops",
            elapsed=28.5,
            credits=0.75,
        )
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["result"] == "full transcript"
        assert data["error"] == "oops"
        assert data["elapsed"] == 28.5
        assert data["credits"] == 0.75

    def test_done_agent_falls_back_to_in_memory_result_on_read_error(self, tmp_path: Path) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(
            done=True, result="in-memory", result_path=str(tmp_path / "missing.txt")
        )
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        assert _payload(_run(mod.api_spawn_status, req))["result"] == "in-memory"


# ── api_spawn_list / retry / delete / clear ──


class TestApiSpawnList:
    def test_empty_without_manager(self) -> None:
        assert _payload(_run(mod.api_spawn_list, _Req(_state()))) == {"agents": []}

    def test_lists_running_and_finished_shapes(self) -> None:
        mgr = _mgr(
            all_agents=[
                _info(id="run", done=False),
                _info(
                    id="fin",
                    done=True,
                    result="r",
                    error="e",
                    outcome="failed",
                    elapsed=42.5,
                    credits=1.25,
                ),
            ]
        )
        agents = _payload(_run(mod.api_spawn_list, _Req(_state(subagents=mgr))))["agents"]
        assert [a["id"] for a in agents] == ["run", "fin"]
        assert "turns" in agents[0] and "result" not in agents[0]
        assert agents[1]["outcome"] == "failed" and agents[1]["stopped"] is False
        assert "elapsed" not in agents[1]
        assert "credits" not in agents[1]

    def test_finished_agent_without_error_reports_empty_string(self) -> None:
        mgr = _mgr(all_agents=[_info(done=True, error="")])
        agents = _payload(_run(mod.api_spawn_list, _Req(_state(subagents=mgr))))["agents"]
        assert agents[0]["error"] == ""
        assert "elapsed" not in agents[0]
        assert "credits" not in agents[0]


class TestApiSpawnRetry:
    def _req(self, mgr: Any, agent_id: str = "a1") -> _Req:
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        old = mgr.get.return_value
        if isinstance(old, SimpleNamespace):
            old.execution_context = ExecutionContext(
                None, MemoryStoreRef("default"), "template", "kirocrew"
            )
        return _Req(_state(subagents=mgr), None, match_info={"agent_id": agent_id})

    def test_503_without_manager(self) -> None:
        req = _Req(_state(), None, match_info={"agent_id": "a1"})
        assert _run(mod.api_spawn_retry, req).status == 503

    def test_400_for_native_agents(self) -> None:
        resp = _run(mod.api_spawn_retry, self._req(_mgr(), "native:x"))
        assert resp.status == 400
        assert "native subagents" in _payload(resp)["error"]

    def test_404_when_unknown(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = None
        assert _run(mod.api_spawn_retry, self._req(mgr)).status == 404

    def test_409_while_running(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(done=False)
        resp = _run(mod.api_spawn_retry, self._req(mgr))
        assert resp.status == 409
        assert _payload(resp)["error"] == "agent is still running"

    def test_409_when_outcome_is_not_failed(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(done=True, outcome="stopped")
        resp = _run(mod.api_spawn_retry, self._req(mgr))
        assert resp.status == 409
        assert "outcome=stopped" in _payload(resp)["error"]

    def test_429_when_capacity_reached(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(done=True, outcome="failed")
        mgr.spawn.return_value = None
        assert _run(mod.api_spawn_retry, self._req(mgr)).status == 429

    def test_400_when_respawn_is_rejected(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(done=True, outcome="failed")
        mgr.spawn.return_value = _info(done=True, error="rejected")
        resp = _run(mod.api_spawn_retry, self._req(mgr))
        assert resp.status == 400
        assert _payload(resp)["error"] == "rejected"

    def test_respawns_raw_task_without_batch_identity(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(
            done=True, outcome="failed", _raw_task="original task", task="redacted"
        )
        mgr.spawn.return_value = _info(id="new")
        resp = _run(mod.api_spawn_retry, self._req(mgr))
        assert _payload(resp) == {"id": "new", "retried_from": "a1", "status": "spawned"}
        assert mgr.spawn.call_args.args[0] == "original task"
        assert "batch_id" not in mgr.spawn.call_args.kwargs

    def test_refuses_a_run_whose_successor_claim_is_taken(self) -> None:
        """The manager's claim decides; the route starts nothing when it is taken."""
        mgr = _mgr()
        mgr.get.return_value = _info(done=True, outcome="failed", _raw_task="original task")
        mgr.claim_retry.return_value = "c1"
        resp = _run(mod.api_spawn_retry, self._req(mgr))
        assert resp.status == 409
        assert _payload(resp)["code"] == "retry_superseded"
        assert "c1" in _payload(resp)["error"]
        mgr.claim_retry.assert_called_once_with(mgr.get.return_value)
        mgr.spawn.assert_not_called()

    def test_a_landed_retry_settles_its_claim_with_the_new_id(self) -> None:
        mgr = _mgr()
        old = _info(done=True, outcome="failed", _raw_task="t")
        mgr.get.return_value = old
        mgr.claim_retry.return_value = ""
        mgr.spawn.return_value = _info(id="new")
        assert _run(mod.api_spawn_retry, self._req(mgr)).status == 200
        assert mgr.settle_retry.call_args_list[0].args == (old, "new")

    def test_a_start_that_raises_keeps_the_claim(self) -> None:
        """The start may have accepted its row before raising, so the run is
        not handed back as retryable."""
        mgr = _mgr()
        mgr.get.return_value = _info(done=True, outcome="failed", _raw_task="t")
        mgr.claim_retry.return_value = ""
        old = mgr.get.return_value
        mgr.spawn.side_effect = RuntimeError("store write raised")
        with pytest.raises(RuntimeError):
            _run(mod.api_spawn_retry, self._req(mgr))
        assert mgr.settle_retry.call_args_list[0].args == (old, mod.SUCCESSOR_UNKNOWN)

    def test_a_raise_before_the_spawn_releases_the_claim(self) -> None:
        """Nothing can have landed before the spawn is reached, so the run stays
        retryable."""
        mgr = _mgr()
        old = _info(done=True, outcome="failed", _raw_task="t", agent="proj-agent")
        mgr.get.return_value = old
        mgr.claim_retry.return_value = ""
        with (
            patch.object(
                mod, "warm_project_agents_for_spawn", AsyncMock(side_effect=RuntimeError("x"))
            ),
            pytest.raises(RuntimeError),
        ):
            _run(mod.api_spawn_retry, self._req(mgr))
        assert mgr.settle_retry.call_args_list == [((old, None),)]
        mgr.spawn.assert_not_called()

    def test_a_refused_start_releases_the_claim(self) -> None:
        mgr = _mgr()
        old = _info(done=True, outcome="failed", _raw_task="t")
        mgr.get.return_value = old
        mgr.claim_retry.return_value = ""
        mgr.spawn.return_value = None
        assert _run(mod.api_spawn_retry, self._req(mgr)).status == 429
        assert mgr.settle_retry.call_args_list == [((old, None),)]

    def test_falls_back_to_redacted_task_when_raw_is_empty(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(done=True, outcome="failed", _raw_task="", task="shown")
        mgr.spawn.return_value = _info()
        _run(mod.api_spawn_retry, self._req(mgr))
        assert mgr.spawn.call_args.args[0] == "shown"

    def test_retry_reuses_the_failed_run_context_scope(self) -> None:
        """A retry must be the same experiment — not a wider-context rerun."""
        mgr = _mgr()
        mgr.get.return_value = _info(
            done=True,
            outcome="failed",
            _raw_task="t",
            include_memory=False,
            include_project=False,
            crew="coding",
        )
        mgr.spawn.return_value = _info(id="new")
        _run(mod.api_spawn_retry, self._req(mgr))
        kwargs = mgr.spawn.call_args.kwargs
        assert kwargs["include_memory"] is False
        assert kwargs["include_lessons"] is True
        assert kwargs["include_project"] is False
        assert kwargs["crew"] == "coding"


class TestApiSpawnDelete:
    def test_404_for_unknown_native_card(self) -> None:
        req = _Req(_state(), None, match_info={"agent_id": "native:gone"})
        assert _run(mod.api_spawn_delete, req).status == 404

    def test_native_cancel_marks_tracker_and_broadcasts(self) -> None:
        record: dict[str, Any] = {"done": False}
        slot = SimpleNamespace(_native_subagent_tracker={"sess1": record})
        state = _state()
        state._native_cards = {
            "native:c1": {"slot": "chat-1", "session_id": "sess1", "started": 1.0}
        }
        state.get_slot.return_value = slot
        req = _Req(state, None, match_info={"agent_id": "native:c1"})
        resp = _run(mod.api_spawn_delete, req)
        assert _payload(resp) == {"ok": True, "cancelled": True}
        assert record["stopped"] is True and record["outcome"] == "stopped"
        assert "native:c1" not in state._native_cards
        assert state.broadcast_ws.call_args.args[0] == "subagent_done"

    def test_native_cancel_survives_a_broken_slot_lookup(self) -> None:
        state = _state()
        state._native_cards = {"native:c1": {"slot": "chat-1"}}
        state.get_slot.side_effect = RuntimeError("no slot store")
        req = _Req(state, None, match_info={"agent_id": "native:c1"})
        assert _payload(_run(mod.api_spawn_delete, req))["ok"] is True

    def test_managed_delete_settlement_uses_one_public_manager_seam(self) -> None:
        """The HTTP handler does not own manager report or registry internals."""
        source = inspect.getsource(mod.api_spawn_delete)

        assert ".settle_before_delete(" in source
        for private in (
            "._agents",
            "._tasks",
            "._run_terminal_report",
        ):
            assert private not in source

    def test_404_when_managed_agent_is_unknown(self) -> None:
        req = _Req(_state(subagents=_mgr()), None, match_info={"agent_id": "a1"})
        assert _run(mod.api_spawn_delete, req).status == 404

    def test_cancels_a_running_agent(self) -> None:
        info = _info()
        mgr = _mgr(_agents={"a1": info}, cancel=AsyncMock(return_value=True))
        mgr.get.return_value = info
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        assert _payload(_run(mod.api_spawn_delete, req)) == {"ok": True, "cancelled": True}

    def test_removes_an_already_finished_agent(self) -> None:
        info = _info()
        mgr = _mgr(_agents={"a1": info}, cancel=AsyncMock(return_value=False))
        mgr.get.return_value = info
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})

        assert _payload(_run(mod.api_spawn_delete, req))["cancelled"] is False
        mgr.settle_before_delete.assert_awaited_once_with("a1")

    def test_preserves_finished_agent_while_settlement_is_pending(self) -> None:
        info = _info(parent_session_key="dashboard:chat-1")
        mgr = _mgr(
            _agents={"a1": info},
            cancel=AsyncMock(return_value=False),
            settle_before_delete=AsyncMock(return_value="pending"),
        )
        mgr.get.return_value = info
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})

        response = _run(mod.api_spawn_delete, req)

        assert response.status == 409
        assert _payload(response)["code"] == "completion_delivery_pending"
        mgr.settle_before_delete.assert_awaited_once_with("a1")


class TestApiSpawnStopAll:
    def test_requires_manager(self) -> None:
        resp = _run(mod.api_spawn_stop_all, _Req(_state(), {"slot": "chat-1"}))
        assert resp.status == 503
        assert _payload(resp)["code"] == "subagents_unavailable"

    @pytest.mark.parametrize("body", [_BAD_JSON, None, {}, {"slot": ""}, {"slot": "../other"}])
    def test_rejects_invalid_slot_input(self, body: Any) -> None:
        resp = _run(mod.api_spawn_stop_all, _Req(_state(subagents=_mgr()), body))
        assert resp.status == 400

    def test_requires_an_existing_slot(self) -> None:
        state = _state(subagents=_mgr())
        state.get_slot.return_value = None
        resp = _run(mod.api_spawn_stop_all, _Req(state, {"slot": "missing"}))
        assert resp.status == 404
        assert _payload(resp)["code"] == "slot_not_found"

    def test_resolves_server_owned_session_and_stops_running_and_queue(self) -> None:
        mgr = _mgr(cancel_for_parent=AsyncMock(return_value=(2, 3)))
        state = _state(subagents=mgr)
        state.get_slot.return_value = SimpleNamespace(
            key="chat-1", linked_session_key="slack:123.456"
        )
        resp = _run(mod.api_spawn_stop_all, _Req(state, {"slot": "chat-1"}))
        assert _payload(resp) == {
            "ok": True,
            "stopped": 5,
            "running": 2,
            "queued": 3,
        }
        mgr.cancel_for_parent.assert_awaited_once_with("slack:123.456")

    @pytest.mark.parametrize("slot_app", ["", "caller-app", "other-app"])
    def test_app_token_cannot_stop_any_slot(self, slot_app: str) -> None:
        mgr = _mgr(cancel_for_parent=AsyncMock(return_value=(1, 1)))
        state = _state(subagents=mgr)
        state.get_slot.return_value = SimpleNamespace(
            key="chat-1", linked_session_key="foreign:session", _app=slot_app
        )
        req = _Req(state, {"slot": "chat-1"}, extra={"app": "caller-app"})

        resp = _run(mod.api_spawn_stop_all, req)

        assert resp.status == 403
        assert _payload(resp)["code"] == "app_token_forbidden"
        state.get_slot.assert_not_called()
        mgr.cancel_for_parent.assert_not_awaited()

    def test_missing_app_claim_is_denied_before_slot_resolution(self) -> None:
        mgr = _mgr(cancel_for_parent=AsyncMock(return_value=(1, 1)))
        state = _state(subagents=mgr)
        req = _Req(state, {"slot": "chat-1"})
        req._extra.pop("app")

        resp = _run(mod.api_spawn_stop_all, req)

        assert resp.status == 403
        assert _payload(resp)["code"] == "app_token_forbidden"
        state.get_slot.assert_not_called()
        mgr.cancel_for_parent.assert_not_awaited()
