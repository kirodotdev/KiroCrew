"""Owner gate on the skills CRUD write endpoints (dashboard handlers).

POST /api/skills and the PUT/DELETE branches of /api/skills/{name} mutate
skill content, which is injected into agent context — an instruction-injection
surface. These tests pin the deny-by-default gate: app tokens and non-owner
dashboard subjects are refused with a SEL-audited 403 before any write, the
owner path still works, every outcome leaves a SEL record, and GET stays open
(its own foreign-app-slot scoping is covered elsewhere).

Identity setup mirrors the skill-trust guard tests in
``test_skill_trust_store.py`` (same helper, same denial contract).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.dashboard.handlers import prompts


class _Request:
    """Minimal aiohttp-request stand-in: identity mapping + app/state/match_info.

    ``is_owner_dashboard_request`` probes the request mapping with ``in`` /
    ``[]`` and reads ``request.app["state"]``, so the stand-in supports both.
    """

    def __init__(self, identity, state, method="POST", name="demo", body=None):
        self._identity = dict(identity)
        self.app = {"state": state}
        self.method = method
        self.match_info = {"name": name}
        if body is not None:
            self.json = AsyncMock(return_value=body)

    def get(self, key, default=None):
        return self._identity.get(key, default)

    def __contains__(self, key):
        return key in self._identity

    def __getitem__(self, key):
        return self._identity[key]


def _state_with_loader(**loader_methods):
    """DashboardState stand-in whose ``_get_skills`` resolves to a mock loader."""
    loader = SimpleNamespace(**loader_methods)
    state = SimpleNamespace(owner_id="owner", context_builder=SimpleNamespace(skills=loader))
    return state, loader


@pytest.fixture
def audit(monkeypatch):
    sel = SimpleNamespace(log_api_access=Mock(), log_tool_invocation=Mock())
    monkeypatch.setattr(prompts, "_sel", lambda: sel)
    return sel


class TestWriteEndpointsRefuseAppTokens:
    """The real owner check refuses any non-empty app identity — no monkeypatch."""

    @pytest.mark.asyncio
    async def test_app_token_is_refused_by_every_write_endpoint(self, audit):
        loader = Mock()
        state = SimpleNamespace(owner_id="owner", context_builder=SimpleNamespace(skills=loader))
        identity = {"user": "owner", "app": "some-app"}

        for method, call in (
            ("POST", lambda r: prompts.api_skills_create(r)),
            ("PUT", lambda r: prompts.api_skill_detail(r)),
            ("DELETE", lambda r: prompts.api_skill_detail(r)),
        ):
            request = _Request(identity, state, method=method, body={"content": "x"})
            response = await call(request)
            assert response.status == 403, method
            assert json.loads(response.body)["code"] == "dashboard_owner_required", method
            assert audit.log_api_access.call_args.kwargs["outcome"] == "denied", method
            audit.log_api_access.reset_mock()

        loader.create_skill.assert_not_called()
        loader.update_skill.assert_not_called()
        loader.delete_skill.assert_not_called()


class TestWriteEndpointsRefuseNonOwners:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused_by_every_write_endpoint(self, audit, monkeypatch):
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: False, raising=False
        )
        loader = Mock()
        state = SimpleNamespace(context_builder=SimpleNamespace(skills=loader))
        identity = {"user": "guest"}

        for method, operation, call in (
            ("POST", "skill_create", lambda r: prompts.api_skills_create(r)),
            ("PUT", "skill_update", lambda r: prompts.api_skill_detail(r)),
            ("DELETE", "skill_delete", lambda r: prompts.api_skill_detail(r)),
        ):
            request = _Request(identity, state, method=method, body={"content": "x"})
            response = await call(request)
            assert response.status == 403, method
            assert json.loads(response.body)["code"] == "dashboard_owner_required", method
            assert audit.log_api_access.call_args.kwargs == {
                "caller": "guest",
                "operation": operation,
                "outcome": "denied",
                "source": "dashboard",
                "error": "dashboard owner required",
            }, method
            audit.log_api_access.reset_mock()

        loader.create_skill.assert_not_called()
        loader.update_skill.assert_not_called()
        loader.delete_skill.assert_not_called()


class TestOwnerWritePathsStillWork:
    @pytest.fixture(autouse=True)
    def _owner(self, monkeypatch):
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: True, raising=False
        )

    @pytest.mark.asyncio
    async def test_owner_create_is_allowed_and_audited(self, audit):
        state, loader = _state_with_loader(create_skill=Mock(return_value=True))
        request = _Request(
            {"user": "owner"}, state, method="POST", body={"name": "My Skill", "content": "b"}
        )
        response = await prompts.api_skills_create(request)
        assert response.status == 200
        assert json.loads(response.body) == {"ok": True, "name": "my-skill"}
        loader.create_skill.assert_called_once_with("my-skill", "b")
        assert audit.log_api_access.call_args.kwargs["outcome"] == "allowed"
        assert audit.log_tool_invocation.call_args.kwargs == {
            "session_key": "",
            "agent": "api",
            "source": "dashboard",
            "tool_name": "api_skills_create",
            "tool_kind": "skill",
            "outcome": "ok",
            "metadata": {"name": "my-skill"},
        }

    @pytest.mark.asyncio
    async def test_owner_update_is_allowed_and_audited(self, audit):
        state, loader = _state_with_loader(update_skill=Mock(return_value=True))
        request = _Request({"user": "owner"}, state, method="PUT", body={"content": "new"})
        response = await prompts.api_skill_detail(request)
        assert response.status == 200
        assert json.loads(response.body) == {"ok": True}
        loader.update_skill.assert_called_once_with("demo", "new")
        assert audit.log_api_access.call_args.kwargs["outcome"] == "allowed"
        kwargs = audit.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "api_skill_update"
        assert kwargs["outcome"] == "ok"
        assert kwargs["metadata"] == {"name": "demo"}

    @pytest.mark.asyncio
    async def test_owner_delete_is_allowed_and_audited(self, audit):
        state, loader = _state_with_loader(delete_skill=Mock(return_value=True))
        request = _Request({"user": "owner"}, state, method="DELETE")
        response = await prompts.api_skill_detail(request)
        assert response.status == 200
        assert json.loads(response.body) == {"ok": True}
        loader.delete_skill.assert_called_once_with("demo")
        kwargs = audit.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "api_skill_delete"
        assert kwargs["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_rejected_write_outcomes_are_audited(self, audit):
        state, _ = _state_with_loader(create_skill=Mock(return_value=False))
        request = _Request(
            {"user": "owner"}, state, method="POST", body={"name": "demo", "content": "b"}
        )
        response = await prompts.api_skills_create(request)
        assert response.status == 409
        assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "rejected"

        for method, loader_kwargs, tool_name in (
            ("PUT", {"update_skill": Mock(return_value=False)}, "api_skill_update"),
            ("DELETE", {"delete_skill": Mock(return_value=False)}, "api_skill_delete"),
        ):
            audit.log_tool_invocation.reset_mock()
            state, _ = _state_with_loader(**loader_kwargs)
            request = _Request({"user": "owner"}, state, method=method, body={"content": "x"})
            response = await prompts.api_skill_detail(request)
            assert response.status == 404, method
            kwargs = audit.log_tool_invocation.call_args.kwargs
            assert kwargs["tool_name"] == tool_name
            assert kwargs["outcome"] == "rejected"


class TestGetStaysOpen:
    """The read path keeps its existing behavior — no owner gate on GET."""

    @pytest.mark.asyncio
    async def test_non_owner_get_still_reads_a_skill(self, audit, monkeypatch):
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: False, raising=False
        )
        state, loader = _state_with_loader(load_skill=Mock(return_value="content"))
        request = _Request({"user": "guest"}, state, method="GET")
        response = await prompts.api_skill_detail(request)
        assert response.status == 200
        assert json.loads(response.body) == {"name": "demo", "content": "content"}
        loader.load_skill.assert_called_once_with("demo")


class TestSkillLifecycleEndpointsAreGated:
    """The whole mutating skill-handler family carries the owner gate.

    ``api_skill_pending_approve`` promotes candidate content into the live
    catalog (the same instruction-injection surface as a direct write), and
    dismiss / dismiss-all / pin / inject-on-trigger all mutate skill state, so
    each one refuses app tokens and non-owner subjects before touching the
    loader. First-principles review on PR #7393 flagged these as the unfixed
    siblings of the CRUD gate; this class pins the widened coverage.
    """

    _ENDPOINTS = (
        ("skill_pending_approve", lambda r: prompts.api_skill_pending_approve(r)),
        ("skill_pending_dismiss", lambda r: prompts.api_skill_pending_dismiss(r)),
        ("skill_pending_dismiss_all", lambda r: prompts.api_skills_pending_dismiss_all(r)),
        ("skill_pin", lambda r: prompts.api_skill_pin(r)),
        ("skill_inject_on_trigger", lambda r: prompts.api_skill_inject_on_trigger(r)),
    )

    @pytest.mark.asyncio
    async def test_app_token_is_refused_before_any_loader_call(self, audit):
        loader = Mock()
        state = SimpleNamespace(owner_id="owner", context_builder=SimpleNamespace(skills=loader))
        identity = {"user": "owner", "app": "some-app"}

        for operation, call in self._ENDPOINTS:
            request = _Request(identity, state, body={"name": "demo", "pinned": True})
            request.match_info = {"slug": "cand", "name": "demo"}
            response = await call(request)
            assert response.status == 403, operation
            assert json.loads(response.body)["code"] == "dashboard_owner_required", operation
            audit.log_api_access.reset_mock()

        assert loader.method_calls == []

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_and_audited(self, audit, monkeypatch):
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: False, raising=False
        )
        loader = Mock()
        state = SimpleNamespace(context_builder=SimpleNamespace(skills=loader))
        identity = {"user": "guest"}

        for operation, call in self._ENDPOINTS:
            request = _Request(identity, state, body={"name": "demo", "inject": False})
            request.match_info = {"slug": "cand", "name": "demo"}
            response = await call(request)
            assert response.status == 403, operation
            assert audit.log_api_access.call_args.kwargs == {
                "caller": "guest",
                "operation": operation,
                "outcome": "denied",
                "source": "dashboard",
                "error": "dashboard owner required",
            }, operation
            audit.log_api_access.reset_mock()

        assert loader.method_calls == []


class TestAuditFailureNeverBreaksACommittedMutation:
    """GPT round-2 blocker on PR #7393: the post-mutation outcome audit must
    not turn a committed write into a 500. The guard helper already swallows
    SEL failures (file convention); these pin the same contract on the
    outcome audits the CRUD handlers emit after the mutation lands."""

    @pytest.fixture(autouse=True)
    def _owner(self, monkeypatch):
        monkeypatch.setattr(
            prompts, "is_owner_dashboard_request", lambda _request: True, raising=False
        )

    @pytest.fixture
    def broken_audit(self, monkeypatch):
        sel = SimpleNamespace(
            log_api_access=Mock(),
            log_tool_invocation=Mock(side_effect=RuntimeError("sel unwritable")),
        )
        monkeypatch.setattr(prompts, "_sel", lambda: sel)
        return sel

    @pytest.mark.asyncio
    async def test_delete_still_returns_ok(self, broken_audit):
        state, loader = _state_with_loader(delete_skill=Mock(return_value=True))
        request = _Request({"user": "owner"}, state, method="DELETE")
        response = await prompts.api_skill_detail(request)
        assert response.status == 200
        assert json.loads(response.body) == {"ok": True}

    @pytest.mark.asyncio
    async def test_update_still_returns_ok(self, broken_audit):
        state, loader = _state_with_loader(update_skill=Mock(return_value=True))
        request = _Request({"user": "owner"}, state, method="PUT", body={"content": "x"})
        response = await prompts.api_skill_detail(request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_create_still_returns_ok(self, broken_audit):
        state, loader = _state_with_loader(create_skill=Mock(return_value=True))
        request = _Request(
            {"user": "owner"}, state, method="POST", body={"name": "demo", "content": "x"}
        )
        response = await prompts.api_skills_create(request)
        assert response.status == 200
