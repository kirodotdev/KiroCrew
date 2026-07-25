"""Phase-1 tests: pending-approval + pin dashboard API handlers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import prompts as H
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


class _Req:
    """Minimal aiohttp-request stand-in for handler unit tests."""

    def __init__(self, loader, *, match=None, body=None, query=None):
        state = SimpleNamespace(context_builder=SimpleNamespace(skills=loader))
        self.app = {"state": state}
        self.match_info = match or {}
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


def _payload(resp):
    return json.loads(resp.body.decode())


@pytest.fixture()
def loader(tmp_path):
    ld = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    ld.stage_skill_candidate(
        "deploy-helper",
        description="deploy helper",
        triggers="deploy",
        procedure_md="## Steps\n1. go\n",
        provenance=AutoSkillProvenance(session_key="s", created_at=AutoSkillProvenance.now_iso()),
    )
    return ld


@pytest.mark.asyncio
async def test_list_pending(loader):
    resp = await H.api_skills_pending(_Req(loader))
    data = _payload(resp)
    assert [p["slug"] for p in data["pending"]] == ["deploy-helper"]


@pytest.mark.asyncio
async def test_detail(loader):
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
    data = _payload(resp)
    assert data["name"] == "auto/deploy-helper"
    assert "go" in data["content"]


@pytest.mark.asyncio
async def test_detail_invalid_slug(loader):
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "../etc"}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_pin_executor_failure_audits_and_500s(loader, monkeypatch):
    """A set_pinned executor failure must emit a SEL error event and return a
    controlled 500, not bypass auditing."""

    def _boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(loader, "set_pinned", _boom)
    events: list[dict] = []
    monkeypatch.setattr(
        H, "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    resp = await H.api_skill_pin(_Req(loader, body={"name": "auto/deploy-helper", "pinned": True}))
    assert resp.status == 500
    assert any(e.get("outcome") == "error" for e in events)


@pytest.mark.asyncio
async def test_detail_executor_failure_audits_and_500s(loader, monkeypatch):
    """A filesystem/executor failure must emit a SEL error event and return a
    controlled 500 — not bypass mandatory auditing with an unhandled crash."""

    def _boom(_slug):
        raise OSError("disk gone")

    monkeypatch.setattr(loader, "get_pending_skill", _boom)
    events: list[dict] = []
    monkeypatch.setattr(
        H, "_sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 500
    assert any(e.get("outcome") == "error" for e in events)


@pytest.mark.asyncio
async def test_approve_promotes(loader):
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 200
    assert _payload(resp)["approved"] == "auto/deploy-helper"
    assert [s["key"] for s in loader.list_auto_skills()] == ["auto/deploy-helper"]
    assert loader.list_pending_skills() == []


@pytest.mark.asyncio
async def test_approve_missing_returns_409(loader):
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "nope"}))
    assert resp.status == 409


@pytest.mark.asyncio
async def test_dismiss(loader):
    resp = await H.api_skill_pending_dismiss(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 200
    assert loader.list_pending_skills() == []
    resp2 = await H.api_skill_pending_dismiss(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp2.status == 404


@pytest.mark.asyncio
async def test_pin_roundtrip(loader):
    name = loader.approve_pending_skill("deploy-helper")
    assert name == "auto/deploy-helper"
    resp = await H.api_skill_pin(_Req(loader, body={"name": name, "pinned": True}))
    assert resp.status == 200 and _payload(resp)["pinned"] is True
    resp2 = await H.api_skill_pin(_Req(loader, body={"name": "does/not-exist", "pinned": True}))
    assert resp2.status == 400


@pytest.mark.asyncio
async def test_pin_rejects_non_bool_pinned(loader):
    name = loader.approve_pending_skill("deploy-helper")
    assert name == "auto/deploy-helper"
    # JSON string "false" must be rejected, not coerced to truthy (which would
    # pin instead of unpin) — GPT MEDIUM.
    resp = await H.api_skill_pin(_Req(loader, body={"name": name, "pinned": "false"}))
    assert resp.status == 400
    resp2 = await H.api_skill_pin(_Req(loader, body={"name": name, "pinned": 1}))
    assert resp2.status == 400
