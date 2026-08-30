from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import create_skill

_MOD = "kiro_crew.dashboard.handlers.create_skill"

_RECORDS = [
    {"role": "user", "content": "walk me through the code deploy"},
    {"role": "assistant", "content": "first build, then ship, then verify"},
]


def _request(*, app_token: bool = False, internal: bool = False):
    values = {
        "app": "an-app" if app_token else "",
        "internal_auth": internal,
        "user": "owner",
    }
    req = MagicMock()
    req.get = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    return req


def _state(*, subagents=None, conversation_log=None, slots=None):
    return SimpleNamespace(
        subagents=subagents,
        conversation_log=conversation_log,
        _slots={} if slots is None else slots,
    )


def _subagents(*, info=None, max_concurrent: int = 3):
    mgr = MagicMock()
    mgr.spawn = MagicMock(return_value=info)
    mgr.max_concurrent = max_concurrent
    return mgr


def _log(records=None):
    log = MagicMock()
    log.read_messages_chained = MagicMock(return_value=_RECORDS if records is None else records)
    return log


def _patched(body):
    return patch.multiple(
        _MOD,
        read_bounded_json=AsyncMock(return_value=(body, None)),
        is_owner_dashboard_request=MagicMock(return_value=True),
        slot_history_key=MagicMock(return_value="dashboard:chat-1"),
        effective_session_key=MagicMock(return_value="dashboard:chat-1"),
        _session_key_is_restricted=MagicMock(return_value=False),
        sel=MagicMock(),
    )


def _ok_state():
    info = SimpleNamespace(id="sub-123", done=False, error="")
    subagents = _subagents(info=info)
    slot = SimpleNamespace(memory_mode="persistent")
    state = _state(subagents=subagents, conversation_log=_log(), slots={"chat-1": slot})
    return state, subagents


class TestCreateSkillFromSession:
    @pytest.mark.asyncio
    async def test_spawns_background_subagent(self):
        state, subagents = _ok_state()
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "skill for the deploy runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 202
        payload = json.loads(resp.body)
        assert payload == {"id": "sub-123", "status": "spawned"}
        subagents.spawn.assert_called_once()
        kwargs = subagents.spawn.call_args.kwargs
        assert kwargs["parent_session_key"] == "dashboard:chat-1"
        assert kwargs["approval_mode"] == "spawn"
        assert kwargs["silent"] is True
        assert kwargs["model"] is None
        task = subagents.spawn.call_args.args[0]
        assert "skill for the deploy runbook" in task
        assert "TRANSCRIPT" in task

    @pytest.mark.asyncio
    async def test_passes_target_agent_and_app_to_spawn(self):
        state, subagents = _ok_state()
        state._slots["chat-1"] = SimpleNamespace(
            memory_mode="persistent", agent="proj-agent", _app="myapp"
        )
        req = _request()
        req.app = {"state": state}
        with (
            _patched({"session_key": "chat-1", "purpose": "capture the deploy runbook"}),
            patch(f"{_MOD}._validate_agent", MagicMock(return_value=("proj-agent", ""))),
        ):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 202
        kwargs = subagents.spawn.call_args.kwargs
        assert kwargs["agent"] == "proj-agent"
        assert kwargs["app"] == "myapp"
        assert kwargs["_agent_prevalidated"] is True

    @pytest.mark.asyncio
    async def test_default_agent_sentinel_uses_host_default(self):
        # slot.agent == "default" is the host-default sentinel, not a resolvable agent
        # name; it must NOT be rejected -- the subagent runs under the host default.
        state, subagents = _ok_state()
        state._slots["chat-1"] = SimpleNamespace(memory_mode="persistent", agent="default", _app="")
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "capture the runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 202
        kwargs = subagents.spawn.call_args.kwargs
        assert kwargs["agent"] == ""
        assert kwargs["_agent_prevalidated"] is False

    @pytest.mark.asyncio
    async def test_unresolvable_agent_is_rejected(self):
        # A named target agent that no longer resolves is refused (400), not silently
        # downgraded to the default agent.
        state, subagents = _ok_state()
        state._slots["chat-1"] = SimpleNamespace(memory_mode="persistent", agent="ghost", _app="")
        req = _request()
        req.app = {"state": state}
        with (
            _patched({"session_key": "chat-1", "purpose": "capture the runbook"}),
            patch(
                f"{_MOD}._validate_agent", MagicMock(return_value=("", "agent 'ghost' not found"))
            ),
        ):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "agent_unavailable"
        subagents.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_restriction_rechecked_before_spawn_refuses_if_flipped(self):
        # TOCTOU: pass the pre-read check, then the target flips to restricted during the
        # awaited transcript read -> the re-check before spawn must refuse.
        state, subagents = _ok_state()
        req = _request()
        req.app = {"state": state}
        with patch.multiple(
            _MOD,
            read_bounded_json=AsyncMock(
                return_value=({"session_key": "chat-1", "purpose": "capture the runbook"}, None)
            ),
            is_owner_dashboard_request=MagicMock(return_value=True),
            slot_history_key=MagicMock(return_value="dashboard:chat-1"),
            effective_session_key=MagicMock(return_value="dashboard:chat-1"),
            _session_key_is_restricted=MagicMock(side_effect=[False, True]),
            sel=MagicMock(),
        ):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "incognito_session"
        subagents.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_purpose_is_rejected(self):
        state, subagents = _ok_state()
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "   "}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "purpose_required"
        subagents.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_purpose_too_long_returns_400_with_code(self):
        state, subagents = _ok_state()
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "x" * 5000}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "purpose_too_long"
        subagents.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_session_key_returns_400_with_code(self):
        state, _ = _ok_state()
        req = _request()
        req.app = {"state": state}
        with _patched({"purpose": "anything"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "session_key_required"

    @pytest.mark.asyncio
    async def test_unknown_session_returns_404_with_code(self):
        state = _state(subagents=_subagents(), conversation_log=_log(), slots={})
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "capture the deploy runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 404
        assert json.loads(resp.body)["code"] == "unknown_session"

    @pytest.mark.asyncio
    async def test_incognito_session_is_refused(self):
        subagents = _subagents(info=SimpleNamespace(id="x", done=False, error=""))
        slot = SimpleNamespace(memory_mode="incognito")
        state = _state(subagents=subagents, conversation_log=_log(), slots={"chat-1": slot})
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "capture the deploy runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "incognito_session"
        subagents.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_linked_incognito_session_is_refused(self):
        # A channel-born slot can be persistent itself while its linked session
        # (a Slack thread set to !incognito) is restricted on the SessionMap. The
        # canonical check must refuse even though slot.memory_mode is fine.
        subagents = _subagents(info=SimpleNamespace(id="x", done=False, error=""))
        slot = SimpleNamespace(memory_mode="persistent")
        state = _state(subagents=subagents, conversation_log=_log(), slots={"chat-1": slot})
        req = _request()
        req.app = {"state": state}
        with patch.multiple(
            _MOD,
            read_bounded_json=AsyncMock(
                return_value=({"session_key": "chat-1", "purpose": "capture the runbook"}, None)
            ),
            is_owner_dashboard_request=MagicMock(return_value=True),
            slot_history_key=MagicMock(return_value="slack:1720.55"),
            effective_session_key=MagicMock(return_value="slack:1720.55"),
            _session_key_is_restricted=MagicMock(return_value=True),
            sel=MagicMock(),
        ):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "incognito_session"
        subagents.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_session_is_refused(self):
        subagents = _subagents(info=SimpleNamespace(id="x", done=False, error=""))
        slot = SimpleNamespace(memory_mode="persistent")
        assistant_only = [{"role": "assistant", "content": "hello"}]
        state = _state(
            subagents=subagents, conversation_log=_log(assistant_only), slots={"chat-1": slot}
        )
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "capture the deploy runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "empty_session"
        subagents.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_capacity_returns_429_with_code(self):
        subagents = _subagents(info=None)
        slot = SimpleNamespace(memory_mode="persistent")
        state = _state(subagents=subagents, conversation_log=_log(), slots={"chat-1": slot})
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "capture the deploy runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 429
        assert json.loads(resp.body)["code"] == "at_capacity"

    @pytest.mark.asyncio
    async def test_queued_capture_is_cancelled_and_retryable(self):
        # A capacity-queued capture authors at DRAIN time, past the pre-spawn
        # privacy re-check, and the drain never re-checks restriction -- so it must
        # be refused (cancelled + retryable), not accepted, or an incognito flip
        # while queued would leak a now-private transcript into a skill.
        queued = SimpleNamespace(id="sub-q", done=False, error="", queued=True)
        subagents = _subagents(info=queued)
        subagents.cancel = AsyncMock(return_value=True)
        slot = SimpleNamespace(memory_mode="persistent")
        state = _state(subagents=subagents, conversation_log=_log(), slots={"chat-1": slot})
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "capture the deploy runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "at_capacity"
        subagents.cancel.assert_awaited_once_with("sub-q")

    @pytest.mark.asyncio
    async def test_spawn_rejection_returns_400_with_code(self):
        rejected = SimpleNamespace(id="x", done=True, error="rejected by governance")
        subagents = _subagents(info=rejected)
        slot = SimpleNamespace(memory_mode="persistent")
        state = _state(subagents=subagents, conversation_log=_log(), slots={"chat-1": slot})
        req = _request()
        req.app = {"state": state}
        with _patched({"session_key": "chat-1", "purpose": "capture the deploy runbook"}):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "spawn_rejected"

    @pytest.mark.asyncio
    async def test_subagents_unavailable_returns_503_with_code(self):
        state = _state(subagents=None, conversation_log=_log(), slots={})
        req = _request()
        req.app = {"state": state}
        with patch(f"{_MOD}.is_owner_dashboard_request", MagicMock(return_value=True)):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 503
        assert json.loads(resp.body)["code"] == "subagents_unavailable"

    @pytest.mark.asyncio
    async def test_app_token_is_refused(self):
        req = _request(app_token=True)
        resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "app_forbidden"

    @pytest.mark.asyncio
    async def test_internal_caller_is_refused_human_only(self):
        req = _request(internal=True)
        resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "human_only"

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self):
        req = _request()
        req.app = {"state": _ok_state()[0]}
        with patch(f"{_MOD}.is_owner_dashboard_request", MagicMock(return_value=False)):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "forbidden"

    @pytest.mark.asyncio
    async def test_invalid_json_body_returns_coded_error(self):
        state, _ = _ok_state()
        req = _request()
        req.app = {"state": state}
        coded = web.json_response(
            {"error": "invalid JSON body", "code": "invalid_json"}, status=400
        )
        with patch.multiple(
            _MOD,
            read_bounded_json=AsyncMock(return_value=(None, coded)),
            is_owner_dashboard_request=MagicMock(return_value=True),
        ):
            resp = await create_skill.api_create_skill_from_session(req)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_json"
