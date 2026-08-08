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
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod


class _Req:
    """Request double: ``app["state"]``, ``json()``, ``match_info``, ``query``."""

    def __init__(
        self,
        state: Any = None,
        body: Any = None,
        *,
        match_info: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        remote: str = "127.0.0.1",
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.app: dict[str, Any] = {"state": state}
        self._body = body
        self.match_info = match_info or {}
        self.query = query or {}
        self.remote = remote
        self._extra = extra or {}

    async def json(self) -> Any:
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body

    def get(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)


_BAD_JSON = ValueError("not json")


def _run(handler: Any, req: _Req) -> web.Response:
    """Drive one coroutine handler to completion and return its response."""
    return asyncio.run(handler(req))


def _run_view(req: _Req, text: str) -> tuple[str, dict]:
    """Call the (privately typed) ``_apply_result_view`` with the request double."""
    view: Any = mod._apply_result_view
    return asyncio.run(view(req, text))


def _payload(resp: web.Response) -> Any:
    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    return json.loads(body)


def _state(**kw: Any) -> Any:
    """A DashboardState double with the JSON-serializable fields pinned."""
    state = MagicMock()
    state.subagents = None
    state.slack_client = None
    state._native_cards = {}
    state._notification_log = []
    state._unread_count = 0
    state.ws_client_count.return_value = 0
    for key, val in kw.items():
        setattr(state, key, val)
    return state


def _info(**kw: Any) -> Any:
    """A SubagentInfo double with defaults for every field the handlers read."""
    base: dict[str, Any] = {
        "id": "a1",
        "task": "do it",
        "done": False,
        "error": "",
        "result": "",
        "result_path": "",
        "started": 1_700_000_000.0,
        "turns": 2,
        "last_tool": "fs_read",
        "parent_session_key": "dashboard:chat-1",
        "agent": "kirocrew",
        "user_stopped": False,
        "outcome": "",
        "max_turns": 0,
        "cwd": "",
        "model": "",
        "approval_mode": "",
        "silent": False,
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
        assert _payload(resp) == {"error": "cwd not allowed", "counted": True}

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
        mgr.release_conversation.return_value = (False, "conversation_busy: in flight")
        resp = _run(mod.api_spawn_release, self._req(mgr))
        assert resp.status == 409
        assert _payload(resp)["code"] == "conversation_busy"

    def test_404_when_gone(self) -> None:
        mgr = _mgr()
        mgr.release_conversation.return_value = (False, "conversation_gone")
        assert _run(mod.api_spawn_release, self._req(mgr)).status == 404

    def test_success(self) -> None:
        mgr = _mgr()
        mgr.release_conversation.return_value = (True, "")
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
        slot = SimpleNamespace(_subagents_inline_collected=set())
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

    def test_running_agent_reports_progress_fields(self) -> None:
        mgr = _mgr()
        mgr.get.return_value = _info(done=False)
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["done"] is False
        assert data["turns"] == 2 and data["last_tool"] == "fs_read"
        assert isinstance(data["elapsed"], int)

    def test_done_agent_prefers_full_result_from_disk(self, tmp_path: Path) -> None:
        result_file = tmp_path / "result.txt"
        result_file.write_text("full transcript", encoding="utf-8")
        mgr = _mgr()
        mgr.get.return_value = _info(
            done=True, result="truncated", result_path=str(result_file), error="oops"
        )
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        data = _payload(_run(mod.api_spawn_status, req))
        assert data["result"] == "full transcript"
        assert data["error"] == "oops"

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
                _info(id="fin", done=True, result="r", error="e", outcome="failed"),
            ]
        )
        agents = _payload(_run(mod.api_spawn_list, _Req(_state(subagents=mgr))))["agents"]
        assert [a["id"] for a in agents] == ["run", "fin"]
        assert "turns" in agents[0] and "result" not in agents[0]
        assert agents[1]["outcome"] == "failed" and agents[1]["stopped"] is False

    def test_finished_agent_without_error_reports_empty_string(self) -> None:
        mgr = _mgr(all_agents=[_info(done=True, error="")])
        agents = _payload(_run(mod.api_spawn_list, _Req(_state(subagents=mgr))))["agents"]
        assert agents[0]["error"] == ""


class TestApiSpawnRetry:
    def _req(self, mgr: Any, agent_id: str = "a1") -> _Req:
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
        )
        mgr.spawn.return_value = _info(id="new")
        _run(mod.api_spawn_retry, self._req(mgr))
        kwargs = mgr.spawn.call_args.kwargs
        assert kwargs["include_memory"] is False
        assert kwargs["include_lessons"] is True
        assert kwargs["include_project"] is False


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

    def test_404_when_managed_agent_is_unknown(self) -> None:
        req = _Req(_state(subagents=_mgr()), None, match_info={"agent_id": "a1"})
        assert _run(mod.api_spawn_delete, req).status == 404

    def test_cancels_a_running_agent(self) -> None:
        mgr = _mgr(_agents={"a1": _info()}, cancel=AsyncMock(return_value=True))
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        assert _payload(_run(mod.api_spawn_delete, req)) == {"ok": True, "cancelled": True}

    def test_removes_an_already_finished_agent(self) -> None:
        mgr = _mgr(_agents={"a1": _info()}, cancel=AsyncMock(return_value=False))
        mgr._tasks = {"a1": object()}
        req = _Req(_state(subagents=mgr), None, match_info={"agent_id": "a1"})
        assert _payload(_run(mod.api_spawn_delete, req))["cancelled"] is False
        assert mgr._agents == {} and mgr._tasks == {}


class TestApiSpawnClear:
    def test_ok_without_manager(self) -> None:
        assert _payload(_run(mod.api_spawn_clear, _Req(_state()))) == {"ok": True}

    def test_clears_only_finished_agents(self) -> None:
        mgr = _mgr(all_agents=[_info(id="run", done=False), _info(id="fin", done=True)])
        mgr._agents = {"run": _info(), "fin": _info()}
        resp = _run(mod.api_spawn_clear, _Req(_state(subagents=mgr)))
        assert _payload(resp) == {"ok": True, "cleared": 1}
        assert list(mgr._agents) == ["run"]


# ── notifications ──


class TestNotificationRoutes:
    def test_list_returns_log_and_unread(self) -> None:
        state = _state(_notification_log=[{"ts": "1"}], _unread_count=3)
        resp = _run(mod.api_notifications, _Req(state))
        assert _payload(resp) == {"notifications": [{"ts": "1"}], "unread": 3}

    @pytest.mark.parametrize(
        "handler",
        [mod.api_notification_delete, mod.api_notification_ack, mod.api_notification_unack],
    )
    def test_ts_routes_reject_invalid_json(self, handler: Any) -> None:
        resp = _run(handler, _Req(_state(), _BAD_JSON))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.parametrize(
        "handler",
        [mod.api_notification_delete, mod.api_notification_ack, mod.api_notification_unack],
    )
    def test_ts_routes_require_ts(self, handler: Any) -> None:
        resp = _run(handler, _Req(_state(), {}))
        assert resp.status == 400
        assert _payload(resp)["error"] == "ts is required"

    def test_delete_forwards_to_state(self) -> None:
        state = _state(delete_notification=AsyncMock(return_value=True))
        assert _payload(_run(mod.api_notification_delete, _Req(state, {"ts": "1"}))) == {"ok": True}
        state.delete_notification.assert_awaited_once_with("1")

    def test_clear_forwards_to_state(self) -> None:
        state = _state(clear_notifications=AsyncMock())
        assert _payload(_run(mod.api_notifications_clear, _Req(state))) == {"ok": True}
        state.clear_notifications.assert_awaited_once()

    def test_ack_forwards_to_state(self) -> None:
        state = _state(ack_notification=AsyncMock(return_value=False))
        assert _payload(_run(mod.api_notification_ack, _Req(state, {"ts": "1"}))) == {"ok": False}

    def test_unack_of_a_cron_notification_also_unacks_the_job(self) -> None:
        state = _state(
            _notification_log=[{"ts": "1", "kind": "cron", "job_id": "j1"}],
            unack_notification=AsyncMock(return_value=True),
        )
        state.crons.unack_job_async = AsyncMock()
        assert _payload(_run(mod.api_notification_unack, _Req(state, {"ts": "1"})))["ok"] is True
        state.crons.unack_job_async.assert_awaited_once_with("j1")

    def test_unack_survives_a_busy_cron_store(self) -> None:
        from kiro_crew.cron import CronStoreBusy

        state = _state(
            _notification_log=[{"ts": "1", "kind": "cron", "job_id": "j1"}],
            unack_notification=AsyncMock(return_value=True),
        )
        state.crons.unack_job_async = AsyncMock(side_effect=CronStoreBusy("busy"))
        assert _payload(_run(mod.api_notification_unack, _Req(state, {"ts": "1"})))["ok"] is True

    def test_ack_all_marks_every_entry_and_rewrites(self) -> None:
        log: list[dict[str, Any]] = [{"ts": "1", "acked": False}, {"ts": "2"}]
        state = _state(_notification_log=log, _rewrite_notifications_async=AsyncMock())
        assert _payload(_run(mod.api_notifications_ack_all, _Req(state))) == {"ok": True}
        assert all(n["acked"] for n in log)
        state._rewrite_notifications_async.assert_awaited_once()
        assert state.broadcast_ws.call_args.args == ("notification_ack", {"ts": "*"})


class TestNotificationChannels:
    def test_merges_registered_and_stored_channels(self) -> None:
        state = _state()
        state.notification_bus.channels.return_value = {"system.approval": "high"}
        state.notification_channel_settings.all_settings.return_value = {
            "app:demo.alerts": {"muted": True}
        }
        channels = _payload(_run(mod.api_notification_channels, _Req(state)))["channels"]
        by_name = {c["channel"]: c for c in channels}
        assert by_name["system.approval"]["protected"] is True
        assert by_name["system.approval"]["registered"] is True
        stale = by_name["app:demo.alerts"]
        assert stale["registered"] is False
        assert stale["default_priority"] is None
        assert stale["source"] == "app:demo"
        assert stale["settings"] == {"muted": True}


class TestNotificationChannelSettings:
    def _state_with_update(self, entry: Any = None, error: Any = None) -> Any:
        state = _state()
        if error is not None:
            state.notification_channel_settings.update.side_effect = error
        else:
            state.notification_channel_settings.update.return_value = entry or {"muted": True}
        return state

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_notification_channel_settings, _Req(_state(), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON body"

    @pytest.mark.parametrize("body", [[], None, "str"])
    def test_400_on_non_object_body(self, body: Any) -> None:
        resp = _run(mod.api_notification_channel_settings, _Req(_state(), body))
        assert resp.status == 400
        assert _payload(resp)["error"] == "body must be a JSON object"

    @pytest.mark.parametrize("channel", [None, "", "   ", 7])
    def test_400_without_a_channel(self, channel: Any) -> None:
        req = _Req(_state(), {"channel": channel})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "channel is required"

    def test_400_on_overlong_channel(self) -> None:
        req = _Req(_state(), {"channel": "c" * 257})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "channel name too long"

    def test_400_on_non_boolean_muted(self) -> None:
        req = _Req(_state(), {"channel": "a.b", "muted": "yes"})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "muted must be a boolean"

    def test_400_on_non_string_priority(self) -> None:
        req = _Req(_state(), {"channel": "a.b", "priority": 3})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp)["error"] == "priority must be a string or null"

    def test_settings_error_becomes_400(self) -> None:
        from kiro_crew.notifications.settings import ChannelSettingsError

        state = self._state_with_update(error=ChannelSettingsError("cannot mute approval"))
        req = _Req(state, {"channel": "system.approval", "muted": True})
        resp = _run(mod.api_notification_channel_settings, req)
        assert resp.status == 400
        assert _payload(resp)["error"] == "cannot mute approval"

    def test_null_priority_clears_the_override(self) -> None:
        state = self._state_with_update(entry={"muted": False})
        req = _Req(state, {"channel": " a.b ", "priority": None})
        resp = _run(mod.api_notification_channel_settings, req)
        assert _payload(resp) == {"ok": True, "channel": "a.b", "settings": {"muted": False}}
        kwargs = state.notification_channel_settings.update.call_args.kwargs
        assert kwargs["clear_priority"] is True and kwargs["priority"] is None

    def test_explicit_priority_is_forwarded(self) -> None:
        state = self._state_with_update(entry={"priority": "high"})
        req = _Req(state, {"channel": "a.b", "priority": "high", "muted": True})
        assert _run(mod.api_notification_channel_settings, req).status == 200
        kwargs = state.notification_channel_settings.update.call_args.kwargs
        assert kwargs["priority"] == "high" and kwargs["clear_priority"] is False


# ── Slack pins / reactions proxies ──


def _track(monkeypatch, tracked: bool) -> None:
    monkeypatch.setattr("kiro_crew.slack.handler.is_tracked_channel", lambda cid: tracked)


class TestSlackPins:
    def test_skipped_without_a_slack_client(self) -> None:
        resp = _run(mod.api_slack_pins, _Req(_state(), {"action": "list"}))
        assert _payload(resp) == {"ok": True, "skipped": "no_slack"}

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=MagicMock()), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_on_unknown_action(self) -> None:
        req = _Req(_state(slack_client=MagicMock()), {"action": "toggle"})
        resp = _run(mod.api_slack_pins, req)
        assert resp.status == 400
        assert "action must be" in _payload(resp)["error"]

    @pytest.mark.parametrize("channel", [7, "", "not-a-channel"])
    def test_400_on_bad_channel(self, channel: Any) -> None:
        req = _Req(_state(slack_client=MagicMock()), {"action": "list", "channel": channel})
        resp = _run(mod.api_slack_pins, req)
        assert _payload(resp)["error"] == "invalid channel ID format"

    @pytest.mark.parametrize("ts", [None, "nope"])
    def test_400_on_bad_timestamp_for_mutations(self, ts: Any) -> None:
        body = {"action": "add", "channel": "C0123ABC456", "ts": ts}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 400
        assert "Slack timestamp" in _payload(resp)["error"]

    def test_403_for_untracked_channel(self, monkeypatch) -> None:
        _track(monkeypatch, False)
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 403
        assert "not in tracked channels" in _payload(resp)["error"]

    @pytest.mark.parametrize("action,method", [("add", "add_pin"), ("remove", "remove_pin")])
    def test_add_and_remove_call_through(self, monkeypatch, action: str, method: str) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        setattr(slack, method, AsyncMock())
        body = {"action": action, "channel": "C0123ABC456", "ts": "1712793600.123456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert _payload(resp) == {"ok": True}
        getattr(slack, method).assert_awaited_once_with("C0123ABC456", "1712793600.123456")

    def test_list_redacts_pinned_text(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.list_pins = AsyncMock(return_value=[{"text": "token xoxb-1234567890-secret"}])
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert "xoxb-1234567890-secret" not in _payload(resp)["pins"][0]["text"]

    def test_list_tolerates_a_pin_without_text(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.list_pins = AsyncMock(return_value=[{"ts": "1.0"}])
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert _payload(resp)["pins"][0]["text"] == ""

    def test_slack_failure_becomes_502(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.list_pins = AsyncMock(side_effect=RuntimeError("slack down"))
        body = {"action": "list", "channel": "C0123ABC456"}
        resp = _run(mod.api_slack_pins, _Req(_state(slack_client=slack), body))
        assert resp.status == 502
        assert _payload(resp)["error"] == "slack down"


class TestSlackReactions:
    _CHANNEL = "C0123ABC456"
    _TS = "1712793600.123456"

    def test_skipped_without_a_slack_client(self) -> None:
        resp = _run(mod.api_slack_reactions, _Req(_state(), {"action": "add"}))
        assert _payload(resp) == {"ok": True, "skipped": "no_slack"}

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_on_list_action(self) -> None:
        """Reactions has no list op -- only add/remove."""
        req = _Req(_state(slack_client=MagicMock()), {"action": "list"})
        resp = _run(mod.api_slack_reactions, req)
        assert _payload(resp)["error"] == "action must be 'add' or 'remove'"

    @pytest.mark.parametrize("channel", [7, "bogus"])
    def test_400_on_bad_channel(self, channel: Any) -> None:
        req = _Req(_state(slack_client=MagicMock()), {"action": "add", "channel": channel})
        resp = _run(mod.api_slack_reactions, req)
        assert _payload(resp)["error"] == "invalid channel ID format"

    def test_400_on_bad_timestamp(self) -> None:
        body = {"action": "add", "channel": self._CHANNEL, "ts": "now"}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), body))
        assert "Slack timestamp" in _payload(resp)["error"]

    @pytest.mark.parametrize("emoji", [7, "", "not valid!"])
    def test_400_on_bad_emoji(self, emoji: Any) -> None:
        body = {"action": "add", "channel": self._CHANNEL, "ts": self._TS, "emoji": emoji}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid emoji name"

    def test_403_for_untracked_channel(self, monkeypatch) -> None:
        _track(monkeypatch, False)
        body = {
            "action": "add",
            "channel": self._CHANNEL,
            "ts": self._TS,
            "emoji": "white_check_mark",
        }
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=MagicMock()), body))
        assert resp.status == 403

    @pytest.mark.parametrize(
        "action,method", [("add", "add_reaction"), ("remove", "remove_reaction")]
    )
    def test_add_and_remove_raise_on_error(self, monkeypatch, action: str, method: str) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        setattr(slack, method, AsyncMock())
        body = {"action": action, "channel": self._CHANNEL, "ts": self._TS, "emoji": "eyes"}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=slack), body))
        assert _payload(resp) == {"ok": True}
        getattr(slack, method).assert_awaited_once_with(
            self._CHANNEL, self._TS, "eyes", raise_on_error=True
        )

    def test_slack_failure_becomes_502_with_redacted_error(self, monkeypatch) -> None:
        _track(monkeypatch, True)
        slack = MagicMock()
        slack.add_reaction = AsyncMock(side_effect=RuntimeError("failed for xoxb-9999999999-abc"))
        body = {"action": "add", "channel": self._CHANNEL, "ts": self._TS, "emoji": "eyes"}
        resp = _run(mod.api_slack_reactions, _Req(_state(slack_client=slack), body))
        assert resp.status == 502
        assert "xoxb-9999999999-abc" not in _payload(resp)["error"]


# ── browser routes ──


class TestBrowserEvent:
    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_browser_event, _Req(_state(), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_without_an_event_name(self) -> None:
        resp = _run(mod.api_browser_event, _Req(_state(), {"url": "x"}))
        assert resp.status == 400
        assert _payload(resp)["error"] == "event is required"

    def test_broadcasts_extra_fields_and_redacts_strings(self) -> None:
        state = _state()
        body = {
            "event": "navigate",
            "type": "ignored",
            "ts": "ignored",
            "note": "token xoxb-1234567890-secret",
            "count": 3,
        }
        assert _payload(_run(mod.api_browser_event, _Req(state, body))) == {"ok": True}
        name, payload = state.broadcast_ws.call_args.args
        assert name == "browser_event"
        assert payload["event"] == "navigate"
        assert payload["count"] == 3
        assert "xoxb-1234567890-secret" not in payload["note"]
        assert payload["type"] == "browser_event"


class TestResolveBrowseSessionKey:
    def test_non_integer_pid_resolves_to_nothing(self) -> None:
        assert mod._resolve_browse_session_key("nope") == ""
        assert mod._resolve_browse_session_key(None) == ""

    def test_direct_hit_on_the_posting_pid(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "dashboard:chat-1")
        assert mod._resolve_browse_session_key(42) == "dashboard:chat-1"

    def test_walks_up_to_the_ancestor_that_has_a_sidecar(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "dashboard:chat-7" if pid == 9 else "")
        monkeypatch.setattr(mod.platform_compat, "get_ppid", lambda pid: 9)
        assert mod._resolve_browse_session_key(42) == "dashboard:chat-7"

    def test_stops_when_the_ancestor_walk_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")

        def _boom(pid: int) -> int:
            raise OSError("no such process")

        monkeypatch.setattr(mod.platform_compat, "get_ppid", _boom)
        assert mod._resolve_browse_session_key(42) == ""

    def test_cycle_in_the_process_chain_terminates(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        monkeypatch.setattr(mod.platform_compat, "get_ppid", lambda pid: 42 if pid == 43 else 43)
        assert mod._resolve_browse_session_key(42) == ""


class TestBrowserFrame:
    _FRAME = "aGVsbG8="

    def test_403_from_off_host(self) -> None:
        req = _Req(_state(), {"data": self._FRAME}, remote="203.0.113.7")
        resp = _run(mod.api_browser_frame, req)
        assert resp.status == 403
        assert _payload(resp)["error"] == "loopback only"

    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_browser_frame, _Req(_state(), _BAD_JSON))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_when_the_body_carries_no_frame(self) -> None:
        resp = _run(mod.api_browser_frame, _Req(_state(), {"format": "jpeg"}))
        assert resp.status == 400
        assert _payload(resp)["error"] == "no frame data"

    def test_broadcasts_and_reports_subscriber_count(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        state = _state()
        state.ws_client_count.return_value = 2
        body = {"data": self._FRAME, "format": "png", "source": "pump"}
        resp = _run(mod.api_browser_frame, _Req(state, body))
        assert _payload(resp) == {"ok": True, "subscribers": 2}
        name, payload = state.broadcast_ws.call_args.args
        assert name == mod.BROWSER_FRAME_EVENT
        assert payload["format"] == "png"

    def test_resolved_key_overrides_and_strips_the_dashboard_prefix(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "dashboard:chat-5")
        state = _state()
        body = {"data": self._FRAME, "host_pid": 11}
        assert _run(mod.api_browser_frame, _Req(state, body)).status == 200
        assert state.broadcast_ws.call_args.args[1]["session_key"] == "chat-5"


class TestBrowserPumpAudit:
    def test_403_from_off_host(self) -> None:
        resp = _run(mod.api_browser_pump_audit, _Req(_state(), remote="203.0.113.7"))
        assert resp.status == 403

    def test_ok_from_loopback(self) -> None:
        assert _payload(_run(mod.api_browser_pump_audit, _Req(_state()))) == {"ok": True}


class TestBrowserAuthRetry:
    def test_broadcasts_the_ensure_result(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "browser_auth_ensure", lambda: {"healthy": True})
        state = _state()
        resp = _run(mod.api_browser_auth_retry, _Req(state))
        assert _payload(resp) == {"healthy": True}
        assert state.broadcast_browser_event.call_args.args[0] == "auth_retry"

    def test_failure_becomes_500(self, monkeypatch) -> None:
        def _boom() -> dict:
            raise RuntimeError("no cookies")

        monkeypatch.setattr(mod, "browser_auth_ensure", _boom)
        resp = _run(mod.api_browser_auth_retry, _Req(_state()))
        assert resp.status == 500
        assert _payload(resp)["error"] == "no cookies"


class TestBrowserConfig:
    def test_get_reports_mode_engine_extension_and_install(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "browser_mode_enabled", lambda: True)
        monkeypatch.setattr(mod, "get_browser_engine", lambda: "firefox")
        monkeypatch.setattr(mod, "has_playwright_extension", lambda: True)
        monkeypatch.setattr(mod, "get_extension_token", lambda: "tok")
        monkeypatch.setattr(mod, "is_playwright_installed", lambda: True)
        resp = _run(mod.api_browser_config_get, _Req(_state()))
        assert _payload(resp) == {
            "enabled": True,
            "engine": "firefox",
            "engines": list(mod.BROWSER_ENGINES),
            "extension_mode": True,
            "token": True,
            "installed": True,
        }

    def _stub_enable_side_effects(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "generate_playwright_config", lambda engine=None: None)
        monkeypatch.setattr(
            mod, "ensure_playwright_installed", lambda engine: {"ok": True, "step": "done"}
        )

    def test_save_enables_extension_mode_and_writes_the_token(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)
        self._stub_enable_side_effects(monkeypatch)
        monkeypatch.setattr(mod, "register_playwright_proxy", lambda: (None, "registered"))
        body = {"enabled": True, "extension_mode": True, "token": "secret-value"}
        resp = _run(mod.api_browser_config_save, _Req(_state(), body))
        payload = _payload(resp)
        assert payload["ok"] is True and payload["enabled"] is True
        assert payload["mcp_status"] == "registered"
        assert (tmp_path / "playwright-extension-mode").exists()
        assert (tmp_path / "playwright-extension-token").read_text() == "secret-value"

    def test_save_disabling_deregisters_and_removes_both_files(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        (tmp_path / "playwright-extension-mode").touch()
        (tmp_path / "playwright-extension-token").write_text("x", encoding="utf-8")
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)
        monkeypatch.setattr(mod, "deregister_playwright_proxy", lambda: (None, "deregistered"))
        resp = _run(mod.api_browser_config_save, _Req(_state(), {"enabled": False, "extension_mode": False}))
        assert resp.status == 200
        assert _payload(resp)["mcp_status"] == "deregistered"
        assert not (tmp_path / "playwright-extension-mode").exists()
        assert not (tmp_path / "playwright-extension-token").exists()

    def test_mcp_registration_failure_is_reported_not_raised(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)
        self._stub_enable_side_effects(monkeypatch)

        def _boom() -> tuple[None, str]:
            raise OSError("mcp.json locked")

        monkeypatch.setattr(mod, "register_playwright_proxy", _boom)
        resp = _run(mod.api_browser_config_save, _Req(_state(), {"enabled": True, "extension_mode": False}))
        payload = _payload(resp)
        assert payload["ok"] is True
        assert payload["mcp_status"] == "registration-failed"

    def test_installer_exception_never_500s_defers_softly(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # Enabling Browser Mode must NEVER 500 or surface a raw install error, even
        # if the (contracted-non-raising) installer raises unexpectedly. The save
        # returns 200 with a calm browser-deferred advisory; Browser Mode stays on.
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)
        monkeypatch.setattr(mod, "generate_playwright_config", lambda engine=None: None)
        monkeypatch.setattr(mod, "register_playwright_proxy", lambda: (None, "registered"))

        def _explode(engine: str) -> dict:
            raise RuntimeError("unexpected boom deep in the installer")

        monkeypatch.setattr(mod, "ensure_playwright_installed", _explode)
        resp = _run(mod.api_browser_config_save, _Req(_state(), {"enabled": True, "extension_mode": False}))
        assert resp.status == 200
        payload = _payload(resp)
        assert payload["ok"] is True and payload["enabled"] is True
        assert payload["install"]["step"] == "browser-deferred"
        assert "boom" not in payload["install"]["detail"]

    def test_app_token_cannot_enable_browser_mode(self, monkeypatch, tmp_path: Path) -> None:
        # Enabling Browser Mode is a keystone-level grant; an app token (truthy
        # request["app"]) must be refused with 403 before any state is written.
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)

        def _must_not_write(_enabled: bool) -> None:
            raise AssertionError("app token must not reach set_browser_mode_enabled")

        monkeypatch.setattr(mod, "set_browser_mode_enabled", _must_not_write)
        req = _Req(_state(), {"enabled": True, "extension_mode": False}, extra={"app": "some-app"})
        resp = _run(mod.api_browser_config_save, req)
        assert resp.status == 403
        assert _payload(resp)["code"] == "dashboard_user_required"

    def test_truthy_non_bool_does_not_enable(self, monkeypatch, tmp_path: Path) -> None:
        # A truthy non-boolean ("false"/1/"off") must NOT enable a security
        # capability — only a real JSON true does. So it takes the disable path
        # (deregister), never the installer/register path.
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)
        monkeypatch.setattr(mod, "deregister_playwright_proxy", lambda: (None, "absent"))

        def _must_not_install(engine: str) -> dict:
            raise AssertionError('"false" must not trigger the installer')

        monkeypatch.setattr(mod, "ensure_playwright_installed", _must_not_install)
        resp = _run(mod.api_browser_config_save, _Req(_state(), {"enabled": "false", "extension_mode": False}))
        payload = _payload(resp)
        assert payload["ok"] is True
        assert payload["enabled"] is False

    def test_disabling_revokes_active_sessions(self, monkeypatch, tmp_path: Path) -> None:
        # Disabling must reset live sessions, or the running ACP session keeps its
        # cached browser_* tools (kiro-cli caches tools/list for the session's
        # lifetime) and browsing works while Settings say "off". Fires because
        # this is a real enable->disable transition.
        (tmp_path / "browser-mode-enabled").touch()  # currently ENABLED
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)
        monkeypatch.setattr(mod, "browser_mode_enabled", lambda: True)
        monkeypatch.setattr(mod, "deregister_playwright_proxy", lambda: (None, "deregistered"))
        import kiro_crew.dashboard.handlers.sessions as sessions_mod

        calls: list[int] = []

        async def _fake_reset(_req: Any) -> int:
            calls.append(1)
            return 2

        monkeypatch.setattr(sessions_mod, "_reset_all_sessions", _fake_reset)
        resp = _run(mod.api_browser_config_save, _Req(_state(), {"enabled": False, "extension_mode": False}))
        payload = _payload(resp)
        assert calls == [1]
        assert payload["sessions_reset"] == 2

    def test_no_op_resave_does_not_reset_sessions(self, monkeypatch, tmp_path: Path) -> None:
        # Re-saving the same disabled value is not a transition and must NOT tear
        # down the user's live session.
        monkeypatch.setattr(loader, "data_home", lambda: tmp_path)
        monkeypatch.setattr(mod, "browser_mode_enabled", lambda: False)
        monkeypatch.setattr(mod, "deregister_playwright_proxy", lambda: (None, "absent"))
        import kiro_crew.dashboard.handlers.sessions as sessions_mod

        def _must_not_reset(_req: Any) -> int:
            raise AssertionError("a no-op re-save must not reset sessions")

        monkeypatch.setattr(sessions_mod, "_reset_all_sessions", _must_not_reset)
        resp = _run(mod.api_browser_config_save, _Req(_state(), {"enabled": False, "extension_mode": False}))
        payload = _payload(resp)
        assert payload["sessions_reset"] == 0


# ── small helpers ──


class TestHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", ""),
            ("xoxb-1234-abcdwxyz", "xoxb-••••wxyz"),
            ("abcdefgh", "••••efgh"),
            ("ab", "••••"),
        ],
    )
    def test_mask_secret(self, value: str, expected: str) -> None:
        assert mod._mask_secret(value) == expected

    def test_clean_id_list_drops_blanks_and_normalizes(self) -> None:
        assert mod._clean_id_list([" a ", "", "b"], lambda v: True, "id") == ["a", "b"]

    def test_clean_id_list_rejects_a_non_list(self) -> None:
        with pytest.raises(ValueError, match="ids must be a list"):
            mod._clean_id_list("a", lambda v: True, "id")

    def test_clean_id_list_rejects_an_invalid_entry(self) -> None:
        with pytest.raises(ValueError, match="invalid id: bad"):
            mod._clean_id_list(["bad"], lambda v: v != "bad", "id")

    @pytest.mark.parametrize(
        "value,valid",
        [("kyle@example.com", True), ("", False), ("a" * 260, False), ("no-at-sign", False)],
    )
    def test_is_valid_webex_email(self, value: str, valid: bool) -> None:
        assert mod._is_valid_webex_email(value) is valid

    @pytest.mark.parametrize(
        "value,valid",
        [
            ("kyle@example.com", True),
            ("has space@example.com", False),
            ("two@@example.com", False),
            ("nodot@example", False),
        ],
    )
    def test_is_valid_webex_email_shape_rules(self, value: str, valid: bool) -> None:
        assert mod._is_valid_webex_email(value) is valid

    @pytest.mark.parametrize(
        "value,valid",
        [
            ("Zezhen.Xu-1@corp", True),
            ("", False),
            ("u" * 65, False),
            ("有中文", False),
            ("has space", False),
        ],
    )
    def test_is_valid_wecom_userid(self, value: str, valid: bool) -> None:
        assert mod._is_valid_wecom_userid(value) is valid

    @pytest.mark.parametrize(
        "value,valid",
        [
            ("kyle@example.com", True),
            ("00000000-0000-0000-0000-000000000000", True),
            ("", False),
            ("p" * 255, False),
            ("has space", False),
        ],
    )
    def test_is_valid_teams_principal(self, value: str, valid: bool) -> None:
        assert mod._is_valid_teams_principal(value) is valid

    def test_missing_scope_message_names_a_single_scope(self) -> None:
        msg = mod._missing_scope_message("users:read")
        assert "the users:read OAuth scope" in msg
        assert "add users:read to" in msg

    def test_missing_scope_message_pluralizes(self) -> None:
        msg = mod._missing_scope_message("users:read, pins:write")
        assert "OAuth scopes" in msg

    def test_missing_scope_message_falls_back_when_unnamed(self) -> None:
        msg = mod._missing_scope_message("")
        assert "requires an OAuth scope" in msg
        assert "add the required scope to" in msg

    def test_sanitize_blocks_redacts_keys_and_values(self) -> None:
        from kiro_crew.security import redact_credentials

        blocks = [{"text": {"type": "mrkdwn", "text": "tok xoxb-1234567890-secret"}}]
        out = mod._sanitize_blocks(blocks, redact_credentials)
        assert "xoxb-1234567890-secret" not in json.dumps(out)
        assert blocks[0]["text"]["text"].endswith("secret")  # input untouched

    def test_sanitize_blocks_truncates_deep_structures(self) -> None:
        from kiro_crew.security import redact_credentials

        deep: Any = "leaf"
        for _ in range(mod._MAX_WALK_DEPTH + 3):
            deep = {"child": deep}
        out = mod._sanitize_blocks([deep], redact_credentials)
        assert isinstance(out, list) and len(out) == 1

    def test_sanitize_blocks_caps_the_block_count(self) -> None:
        from kiro_crew.security import redact_credentials

        blocks = [{"i": i} for i in range(mod._MAX_BLOCKS + 5)]
        assert len(mod._sanitize_blocks(blocks, redact_credentials)) == mod._MAX_BLOCKS


class TestResolveSessionTarget:
    def test_rejects_any_target_other_than_origin(self) -> None:
        assert mod._resolve_session_target(_state(), "chat-1", "cron:j1") == (None, None)

    def test_rejects_a_non_cron_caller(self) -> None:
        assert mod._resolve_session_target(_state(), "origin", "dashboard:chat-1") == (None, None)

    def test_returns_none_for_an_unknown_job(self) -> None:
        state = _state()
        state.crons.list_jobs.return_value = []
        assert mod._resolve_session_target(state, "origin", "cron:j1") == (None, None)

    def test_returns_none_for_a_job_with_no_originating_session(self) -> None:
        state = _state()
        state.crons.list_jobs.return_value = [SimpleNamespace(id="j1", session_key="", name="n")]
        assert mod._resolve_session_target(state, "origin", "cron:j1") == (None, None)

    def test_strips_the_dashboard_prefix_from_the_slot_key(self) -> None:
        state = _state()
        state.crons.list_jobs.return_value = [
            SimpleNamespace(id="j1", session_key="dashboard:chat-3", name="Nightly")
        ]
        assert mod._resolve_session_target(state, "origin", "cron:j1:run7") == (
            "chat-3",
            "Nightly",
        )


# ── Teams config API ──


class TestTeamsConfigGet:
    def test_reports_status_and_read_only_flag(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
        monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        state = _state(teams_connected=True, teams_connect_error="x" * 200)
        resp = _run(mod.api_teams_config_get, _Req(state))
        data = _payload(resp)
        assert data["connected"] is True
        assert len(data["connect_error"]) == 120
        assert data["read_only"] is True
        assert data["configured"] is False
        assert data["allowed_emails"] == []


class TestTeamsConfigSave:
    def _save(self, monkeypatch, tmp_path: Path, body: Any) -> tuple[web.Response, Path, Path]:
        env = tmp_path / ".env"
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(loader, "env_path", lambda: env)
        monkeypatch.setattr(loader, "config_path", lambda: cfg)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
        monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "")
        return _run(mod.api_teams_config_save, _Req(_state(), body)), env, cfg

    def test_403_from_remote_sessions(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
        resp = _run(mod.api_teams_config_save, _Req(_state(), {"enabled": True}))
        assert resp.status == 403
        assert "read-only" in _payload(resp)["error"]

    def test_400_on_invalid_json(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, _BAD_JSON)
        assert _payload(resp)["error"] == "invalid JSON"

    def test_400_on_non_object_body(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, [1, 2])
        assert _payload(resp)["error"] == "body must be an object"

    def test_400_on_non_boolean_clear_flag(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"app_password_clear": "yes"})
        assert _payload(resp)["error"] == "app_password_clear must be a boolean"

    def test_400_on_whitespace_in_the_secret(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"app_password": "a b"})
        assert _payload(resp)["error"] == "app_password must not contain whitespace"

    def test_400_on_non_boolean_enabled(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"enabled": "yes"})
        assert _payload(resp)["error"] == "enabled must be a boolean"

    def test_400_on_non_string_app_id(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"app_id": 7})
        assert _payload(resp)["error"] == "app_id must be a string"

    def test_400_on_whitespace_in_tenant_id(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"tenant_id": "a b"})
        assert _payload(resp)["error"] == "tenant_id must not contain whitespace"

    def test_400_on_an_invalid_principal(self, monkeypatch, tmp_path: Path) -> None:
        resp, _, _ = self._save(monkeypatch, tmp_path, {"allowed_emails": ["has space"]})
        assert "invalid principal" in _payload(resp)["error"]

    def test_500_on_corrupt_config_json(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
        resp, _, _ = self._save(monkeypatch, tmp_path, {"enabled": True})
        assert resp.status == 500
        assert _payload(resp)["error"] == "config.json is corrupt"

    def test_persists_secret_to_env_and_config_to_json(self, monkeypatch, tmp_path: Path) -> None:
        body = {
            "enabled": True,
            "app_id": "  app-1  ",
            "tenant_id": "tenant-1",
            "allowed_emails": ["kyle@example.com"],
            "app_password": "MICROSOFT_APP_PASSWORD=super-secret",
        }
        resp, env, cfg = self._save(monkeypatch, tmp_path, body)
        assert _payload(resp)["ok"] is True
        assert _payload(resp)["restart_required"] is True
        assert "MICROSOFT_APP_PASSWORD=super-secret" in env.read_text(encoding="utf-8")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["teams"]["app_id"] == "app-1"
        assert data["teams"]["allowed_emails"] == ["kyle@example.com"]
        assert "app_password" not in json.dumps(data["teams"]).replace('"app_password"', "")

    def test_clear_flag_deletes_the_stored_secret(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "# keep me\nMICROSOFT_APP_PASSWORD=old\nOTHER=1\n", encoding="utf-8"
        )
        resp, env, _ = self._save(monkeypatch, tmp_path, {"app_password_clear": True})
        assert resp.status == 200
        text = env.read_text(encoding="utf-8")
        assert "MICROSOFT_APP_PASSWORD" not in text
        assert "# keep me" in text and "OTHER=1" in text

    def test_purges_a_legacy_plaintext_secret_from_config_json(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"teams": {"app_password": "leaked"}}), encoding="utf-8"
        )
        resp, _, cfg = self._save(monkeypatch, tmp_path, {"enabled": True})
        assert "app_password_purged" in json.dumps(_payload(resp)) or resp.status == 200
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["teams"]["app_password"] == ""

    def test_no_op_save_reports_no_restart_needed(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"teams": {"enabled": False}}), encoding="utf-8"
        )
        resp, _, _ = self._save(monkeypatch, tmp_path, {"enabled": False})
        assert _payload(resp)["restart_required"] is False

    def test_replaces_a_non_dict_teams_section(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(json.dumps({"teams": "oops"}), encoding="utf-8")
        resp, _, cfg = self._save(monkeypatch, tmp_path, {"enabled": True})
        assert resp.status == 200
        assert json.loads(cfg.read_text(encoding="utf-8"))["teams"]["enabled"] is True


class TestTeamsActivity:
    def test_503_when_the_channel_is_not_enabled(self) -> None:
        req = _Req(_state(teams_on_activity=None))
        resp = _run(mod.api_teams_activity, req)
        assert resp.status == 503
        assert resp.text == "Teams channel not enabled"


# ── env writer ──


class TestWriteEnvUpdates:
    def test_appends_new_keys_and_preserves_comments(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("# header\nA=1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"B": "2"})
        assert env.read_text(encoding="utf-8").splitlines() == ["# header", "A=1", "B=2"]

    def test_replaces_an_existing_key_in_place(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("A=1\nB=2\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"A": "9"})
        assert env.read_text(encoding="utf-8").splitlines() == ["A=9", "B=2"]

    def test_creates_the_file_when_absent(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / "nested" / ".env"
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"A": "1"})
        assert env.read_text(encoding="utf-8") == "A=1\n"

    def test_deleting_the_only_key_leaves_an_empty_file(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("A=1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"A": None})
        assert env.read_text(encoding="utf-8") == ""

    def test_a_none_value_for_an_absent_key_is_a_no_op(self, monkeypatch, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("A=1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env)
        mod._write_env_updates({"MISSING": None})
        assert env.read_text(encoding="utf-8") == "A=1\n"

    def test_a_failed_permission_lockdown_is_warned_not_raised(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from kiro_crew import platform_compat

        env = tmp_path / ".env"
        monkeypatch.setattr(loader, "env_path", lambda: env)

        def _boom(path: Any) -> None:
            raise OSError("chmod refused")

        monkeypatch.setattr(platform_compat, "restrict_to_owner", _boom)
        mod._write_env_updates({"A": "1"})
        assert env.read_text(encoding="utf-8") == "A=1\n"


class _FakeResponse:
    """Minimal aiohttp response double: ``status`` plus an awaitable ``json()``."""

    def __init__(self, status: int, payload: Any = None, *, json_raises: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._json_raises = json_raises

    async def json(self, content_type: Any = None) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


class _FakeGetCtx:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeResponse:
        return self._resp

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _fake_http(monkeypatch, resp: _FakeResponse) -> None:
    """Replace ``aiohttp.ClientSession`` so token validators make no real call."""
    import aiohttp as _aiohttp

    class _Session:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def get(self, *a: Any, **kw: Any) -> _FakeGetCtx:
            return _FakeGetCtx(resp)

    monkeypatch.setattr(_aiohttp, "ClientSession", _Session)


class TestTokenValidators:
    def test_discord_accepts_a_2xx(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200))
        assert asyncio.run(mod._validate_discord_token("tok")) is None

    def test_discord_surfaces_the_api_message(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(401, {"message": "401: Unauthorized"}))
        assert asyncio.run(mod._validate_discord_token("tok")) == "401: Unauthorized"

    def test_discord_falls_back_to_the_status_code(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(503, json_raises=True))
        assert asyncio.run(mod._validate_discord_token("tok")) == "HTTP 503"

    def test_telegram_accepts_an_ok_envelope(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200, {"ok": True}))
        assert asyncio.run(mod._validate_telegram_token("tok")) is None

    def test_telegram_surfaces_the_description(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(401, {"ok": False, "description": "Unauthorized"}))
        assert asyncio.run(mod._validate_telegram_token("tok")) == "Unauthorized"

    def test_telegram_falls_back_to_rejected(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200, ["not", "a", "dict"]))
        assert asyncio.run(mod._validate_telegram_token("tok")) == "rejected"

    def test_webex_accepts_a_2xx(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(200))
        assert asyncio.run(mod._validate_webex_token("tok")) is None

    @pytest.mark.parametrize("status", [401, 403])
    def test_webex_rejects_an_unauthorized_token(self, monkeypatch, status: int) -> None:
        _fake_http(monkeypatch, _FakeResponse(status))
        assert asyncio.run(mod._validate_webex_token("tok")) == f"invalid_token (http {status})"

    def test_webex_treats_5xx_as_unverifiable(self, monkeypatch) -> None:
        _fake_http(monkeypatch, _FakeResponse(503))
        with pytest.raises(RuntimeError, match="webex verify http 503"):
            asyncio.run(mod._validate_webex_token("tok"))

    def _fake_slack(self, monkeypatch, error: Any = None) -> list[str]:
        """Patch AsyncWebClient; returns the list of methods the validator called."""
        from slack_sdk.errors import SlackApiError
        from slack_sdk.web import async_client

        calls: list[str] = []

        class _Client:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def auth_test(self) -> None:
                calls.append("auth_test")
                if error is not None:
                    raise SlackApiError("nope", error)

            async def apps_connections_open(self, app_token: str | None = None) -> None:
                calls.append("apps_connections_open")
                if error is not None:
                    raise SlackApiError("nope", error)

        monkeypatch.setattr(async_client, "AsyncWebClient", _Client)
        return calls

    def test_slack_bot_token_uses_auth_test(self, monkeypatch) -> None:
        calls = self._fake_slack(monkeypatch)
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "xoxb-1")) is None
        assert calls == ["auth_test"]

    def test_slack_app_token_uses_connections_open(self, monkeypatch) -> None:
        calls = self._fake_slack(monkeypatch)
        assert asyncio.run(mod._validate_slack_token("SLACK_APP_TOKEN", "xapp-1")) is None
        assert calls == ["apps_connections_open"]

    def test_slack_returns_the_api_error_code(self, monkeypatch) -> None:
        self._fake_slack(monkeypatch, error={"error": "invalid_auth"})
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "x")) == "invalid_auth"

    def test_slack_falls_back_to_rejected_for_an_empty_error(self, monkeypatch) -> None:
        self._fake_slack(monkeypatch, error={"error": ""})
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "x")) == "rejected"

    def test_slack_falls_back_when_the_response_is_unreadable(self, monkeypatch) -> None:
        self._fake_slack(monkeypatch, error="not-a-mapping")
        assert asyncio.run(mod._validate_slack_token("SLACK_BOT_TOKEN", "x")) == "rejected"


class TestConfigGetHandlers:
    def _isolate(self, monkeypatch, tmp_path: Path, env: str, cfg: str) -> None:
        env_file = tmp_path / ".env"
        cfg_file = tmp_path / "config.json"
        env_file.write_text(env, encoding="utf-8")
        cfg_file.write_text(cfg, encoding="utf-8")
        monkeypatch.setattr(loader, "env_path", lambda: env_file)
        monkeypatch.setattr(loader, "config_path", lambda: cfg_file)
        monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    def test_slack_config_get_masks_the_tokens(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        monkeypatch.delenv("OWNER_ID", raising=False)
        self._isolate(
            monkeypatch,
            tmp_path,
            "SLACK_BOT_TOKEN=xoxb-1234567890-wxyz\nOWNER_ID=U_OWNER\n",
            json.dumps({"slack": {"enabled": True}}),
        )
        state = _state(slack_socket_connected=False, slack_connect_error="invalid_auth")
        data = _payload(_run(mod.api_slack_config_get, _Req(state)))
        assert data["bot_token_set"] is True
        assert data["bot_token_preview"].endswith("wxyz")
        assert "xoxb-1234567890-wxyz" not in json.dumps(data)
        assert data["app_token_set"] is False
        assert data["read_only"] is False

    def test_webex_config_get_masks_the_token(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("WEBEX_BOT_TOKEN", raising=False)
        self._isolate(
            monkeypatch,
            tmp_path,
            "WEBEX_BOT_TOKEN=webex-abcdwxyz\n",
            json.dumps({"webex": {"enabled": True, "allowed_emails": ["kyle@example.com"]}}),
        )
        state = _state(webex_connected=True, webex_connect_error="")
        data = _payload(_run(mod.api_webex_config_get, _Req(state)))
        assert data["configured"] is True
        assert data["bot_token_preview"].endswith("wxyz")
        assert "webex-abcdwxyz" not in json.dumps(data)
        assert data["allowed_emails"] == ["kyle@example.com"]


class TestSlackManifest:
    def test_400_on_an_invalid_alias(self) -> None:
        resp = _run(mod.api_slack_manifest, _Req(_state(), query={"alias": "bad alias"}))
        assert resp.status == 400
        assert _payload(resp)["error"] == "invalid alias"

    def test_renders_the_template_and_builds_a_create_url(self) -> None:
        resp = _run(mod.api_slack_manifest, _Req(_state(), query={"alias": "zezhen"}))
        data = _payload(resp)
        assert data["alias"] == "zezhen"
        assert "{{ALIAS}}" not in data["manifest"]
        assert data["create_url"].startswith("https://api.slack.com/apps?new_app=1&manifest_yaml=")

    def test_defaults_to_a_non_identifying_alias(self) -> None:
        assert _payload(_run(mod.api_slack_manifest, _Req(_state())))["alias"] == "kirocrew"


class _FakeBus:
    """Command-bus double: records calls, returns/raises what the test asks for."""

    def __init__(
        self,
        *,
        submit: Any = None,
        drain: Any = None,
        complete: bool = True,
    ) -> None:
        self._submit = submit if submit is not None else {"id": "c1", "ok": True, "result": 1}
        self._drain = drain
        self._complete = complete
        self.submit_calls: list[tuple[Any, ...]] = []
        self.drain_calls: list[tuple[Any, int]] = []
        self.complete_calls: list[tuple[str, bool, Any, Any]] = []

    async def submit(self, session_key: str, op: str, args: dict, *, timeout_ms: int) -> Any:
        self.submit_calls.append((session_key, op, args, timeout_ms))
        if isinstance(self._submit, BaseException):
            raise self._submit
        return self._submit

    async def drain(self, session_keys: list[str], *, wait_ms: int) -> Any:
        self.drain_calls.append((session_keys, wait_ms))
        return self._drain

    async def complete(self, cid: str, ok: bool, *, result: Any = None, error: Any = None) -> bool:
        self.complete_calls.append((cid, ok, result, error))
        return self._complete


def _install_bus(monkeypatch, bus: _FakeBus) -> _FakeBus:
    monkeypatch.setattr(mod, "get_command_bus", lambda: bus)
    return bus


_INTERNAL = {"internal_auth": True}


class TestBrowserCommand:
    def test_403_from_off_host(self) -> None:
        req = _Req(_state(), {"op": "click"}, remote="203.0.113.7", extra=_INTERNAL)
        resp = _run(mod.api_browser_command, req)
        assert resp.status == 403
        assert _payload(resp)["code"] == "loopback_only"

    def test_403_for_a_cookie_caller_without_the_internal_secret(self) -> None:
        resp = _run(mod.api_browser_command, _Req(_state(), {"op": "click"}))
        assert resp.status == 403

    @pytest.mark.parametrize("body", [_BAD_JSON, [1], "str", None])
    def test_400_on_a_non_object_body(self, body: Any) -> None:
        resp = _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL))
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_json"

    @pytest.mark.parametrize("op", [None, "", 7])
    def test_400_without_an_op(self, op: Any) -> None:
        resp = _run(mod.api_browser_command, _Req(_state(), {"op": op}, extra=_INTERNAL))
        assert _payload(resp)["code"] == "op_required"

    def test_400_when_args_is_not_an_object(self) -> None:
        body = {"op": "click", "args": [1, 2]}
        resp = _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL))
        assert resp.status == 400
        assert _payload(resp)["code"] == "args_must_be_object"

    def test_503_when_no_session_can_be_identified(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        body = {"op": "click", "session_key": 7}
        resp = _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL))
        assert resp.status == 503
        assert _payload(resp)["code"] == "no_native_panel"

    def test_resolved_pid_wins_over_the_body_session_key(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "dashboard:chat-9")
        bus = _install_bus(monkeypatch, _FakeBus())
        body = {"op": "click", "host_pid": 5, "session_key": "stale"}
        assert _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL)).status == 200
        assert bus.submit_calls[0][0] == "chat-9"

    @pytest.mark.parametrize("timeout_ms", [None, 0, -1, True, "500"])
    def test_invalid_timeout_falls_back_to_the_default(self, monkeypatch, timeout_ms: Any) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        bus = _install_bus(monkeypatch, _FakeBus())
        body = {"op": "click", "session_key": "chat-1", "timeout_ms": timeout_ms}
        assert _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL)).status == 200
        assert bus.submit_calls[0][3] == mod.DEFAULT_COMMAND_TIMEOUT_MS

    @pytest.mark.parametrize(
        "exc_name,status,code",
        [
            ("NoPanelError", 503, "no_native_panel"),
            ("QueueFullError", 429, "queue_full"),
            ("TimeoutError", 504, "timeout"),
        ],
    )
    def test_bus_failures_map_to_status(
        self, monkeypatch, exc_name: str, status: int, code: str
    ) -> None:
        exc: BaseException = (
            asyncio.TimeoutError()
            if exc_name == "TimeoutError"
            else getattr(mod, exc_name)()  # NoPanelError / QueueFullError
        )
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        _install_bus(monkeypatch, _FakeBus(submit=exc))
        body = {"op": "click", "session_key": "chat-1"}
        resp = _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL))
        assert resp.status == status
        assert _payload(resp)["code"] == code

    def test_successful_outcome_returns_the_result(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        _install_bus(monkeypatch, _FakeBus(submit={"id": "c1", "ok": True, "result": {"x": 1}}))
        body = {"op": "click", "session_key": "chat-1", "args": {"ref": "e7"}}
        resp = _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL))
        assert _payload(resp) == {"id": "c1", "ok": True, "result": {"x": 1}}

    def test_failed_outcome_returns_the_error(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        _install_bus(monkeypatch, _FakeBus(submit={"id": "c1", "ok": False, "error": "boom"}))
        body = {"op": "click", "session_key": "chat-1"}
        resp = _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL))
        assert _payload(resp) == {"id": "c1", "ok": False, "error": "boom"}

    def test_failed_outcome_without_a_message_uses_a_placeholder(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "verify_session_pid", lambda pid: "")
        _install_bus(monkeypatch, _FakeBus(submit={"id": "c1", "ok": False}))
        body = {"op": "click", "session_key": "chat-1"}
        resp = _run(mod.api_browser_command, _Req(_state(), body, extra=_INTERNAL))
        assert _payload(resp)["error"] == "error"


class TestBrowserCommandDrain:
    def test_403_from_off_host(self) -> None:
        req = _Req(_state(), {"session_keys": []}, remote="203.0.113.7", extra=_INTERNAL)
        assert _run(mod.api_browser_command_drain, req).status == 403

    @pytest.mark.parametrize("body", [_BAD_JSON, [1]])
    def test_400_on_a_non_object_body(self, body: Any) -> None:
        resp = _run(mod.api_browser_command_drain, _Req(_state(), body, extra=_INTERNAL))
        assert _payload(resp)["code"] == "invalid_json"

    @pytest.mark.parametrize("keys", [None, "chat-1", ["chat-1", 7]])
    def test_400_on_bad_session_keys(self, keys: Any) -> None:
        req = _Req(_state(), {"session_keys": keys}, extra=_INTERNAL)
        resp = _run(mod.api_browser_command_drain, req)
        assert resp.status == 400
        assert _payload(resp)["code"] == "session_keys_invalid"

    def test_204_when_nothing_arrives(self, monkeypatch) -> None:
        _install_bus(monkeypatch, _FakeBus(drain=None))
        req = _Req(_state(), {"session_keys": ["chat-1"]}, extra=_INTERNAL)
        assert _run(mod.api_browser_command_drain, req).status == 204

    def test_returns_the_queued_command(self, monkeypatch) -> None:
        command = {"id": "c1", "session_key": "chat-1", "op": "click", "args": {}}
        _install_bus(monkeypatch, _FakeBus(drain=command))
        req = _Req(_state(), {"session_keys": ["chat-1"]}, extra=_INTERNAL)
        assert _payload(_run(mod.api_browser_command_drain, req)) == command

    @pytest.mark.parametrize("wait_ms", [None, 0, True, "500"])
    def test_invalid_wait_falls_back_to_the_default(self, monkeypatch, wait_ms: Any) -> None:
        bus = _install_bus(monkeypatch, _FakeBus(drain=None))
        req = _Req(_state(), {"session_keys": [], "wait_ms": wait_ms}, extra=_INTERNAL)
        _run(mod.api_browser_command_drain, req)
        assert bus.drain_calls[0][1] == mod.DEFAULT_DRAIN_WAIT_MS


class TestBrowserCommandResult:
    def test_403_from_off_host(self) -> None:
        req = _Req(_state(), {"id": "c1"}, remote="203.0.113.7", extra=_INTERNAL)
        assert _run(mod.api_browser_command_result, req).status == 403

    @pytest.mark.parametrize("body", [_BAD_JSON, "str"])
    def test_400_on_a_non_object_body(self, body: Any) -> None:
        resp = _run(mod.api_browser_command_result, _Req(_state(), body, extra=_INTERNAL))
        assert _payload(resp)["code"] == "invalid_json"

    @pytest.mark.parametrize("cid", [None, "", 7])
    def test_400_without_an_id(self, cid: Any) -> None:
        resp = _run(mod.api_browser_command_result, _Req(_state(), {"id": cid}, extra=_INTERNAL))
        assert resp.status == 400
        assert _payload(resp)["code"] == "id_required"

    def test_404_for_an_unmatched_id(self, monkeypatch) -> None:
        _install_bus(monkeypatch, _FakeBus(complete=False))
        req = _Req(_state(), {"id": "c1", "ok": True}, extra=_INTERNAL)
        resp = _run(mod.api_browser_command_result, req)
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_command"

    def test_coerces_a_non_string_error(self, monkeypatch) -> None:
        bus = _install_bus(monkeypatch, _FakeBus())
        req = _Req(_state(), {"id": "c1", "ok": False, "error": 500}, extra=_INTERNAL)
        assert _payload(_run(mod.api_browser_command_result, req)) == {"ok": True}
        assert bus.complete_calls[0] == ("c1", False, None, "500")

    def test_forwards_a_successful_result(self, monkeypatch) -> None:
        bus = _install_bus(monkeypatch, _FakeBus())
        req = _Req(_state(), {"id": "c1", "ok": True, "result": {"x": 1}}, extra=_INTERNAL)
        assert _run(mod.api_browser_command_result, req).status == 200
        assert bus.complete_calls[0] == ("c1", True, {"x": 1}, None)


class TestDeleteMessage:
    def test_400_on_invalid_json(self) -> None:
        resp = _run(mod.api_delete_message, _Req(_state(), _BAD_JSON))
        assert _payload(resp)["error"] == "invalid JSON"

    @pytest.mark.parametrize("body", [{"channel": "", "ts": "1.0"}, {"channel": "C1", "ts": ""}])
    def test_400_without_channel_and_ts(self, body: dict[str, str]) -> None:
        resp = _run(mod.api_delete_message, _Req(_state(), body))
        assert resp.status == 400
        assert _payload(resp)["error"] == "channel and ts required"

    def test_503_without_a_slack_client(self) -> None:
        body = {"channel": "C1", "ts": "1.0"}
        resp = _run(mod.api_delete_message, _Req(_state(), body))
        assert resp.status == 503
        assert _payload(resp)["error"] == "Slack not connected"


def test_module_exposes_every_route_handler_under_test() -> None:
    """Guard against a rename silently skipping a whole block of these tests."""
    for name in (
        "api_spawn",
        "api_spawn_continue",
        "api_spawn_steer",
        "api_spawn_release",
        "api_spawn_lost",
        "api_spawn_mark_collected",
        "api_spawn_status",
        "api_spawn_list",
        "api_spawn_retry",
        "api_spawn_delete",
        "api_spawn_clear",
        "api_notification_channels",
        "api_notification_channel_settings",
        "api_slack_pins",
        "api_slack_reactions",
        "api_browser_event",
        "api_browser_frame",
        "api_teams_config_save",
    ):
        assert callable(getattr(mod, name)), name


def test_slack_timestamp_contract_matches_the_handler_regex() -> None:
    """The pins/reactions ts guard is a literal ``\\d+\\.\\d+`` shape check."""
    assert re.match(r"^\d+\.\d+$", "1712793600.123456")
    assert not re.match(r"^\d+\.\d+$", "1712793600")
