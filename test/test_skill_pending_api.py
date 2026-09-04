"""Phase-1 tests: pending-approval + pin dashboard API handlers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import prompts as H
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

_OMITTED = object()


class _Req:
    """Minimal aiohttp-request stand-in for handler unit tests."""

    def __init__(self, loader, *, match=None, body=_OMITTED, query=None):
        state = SimpleNamespace(context_builder=SimpleNamespace(skills=loader))
        self.app = {"state": state}
        self.match_info = match or {}
        # `body or {}` would have turned a falsy-but-valid JSON body (`[]`, `0`,
        # `null`) into a dict inside the double — hiding exactly the non-object
        # bodies a handler has to survive. Only an OMITTED body defaults.
        self._body = {} if body is _OMITTED else body
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


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "name", 7, None, True])
async def test_inject_on_trigger_rejects_a_non_object_body(loader, body):
    """`[]` and `"x"` are valid JSON, so `request.json()` can hand back a
    non-dict. Calling `.get` on it would raise AttributeError and surface as a
    500 — a validation answer is the correct outcome."""
    resp = await H.api_skill_inject_on_trigger(_Req(loader, body=body))
    assert resp.status == 400
    assert _payload(resp)["code"] == "inject_not_bool"


# ── Part C: pending-update fields + approve/detail routing ──
#
# These monkeypatch the loader so they do NOT depend on part B landing the
# kind/target/base_version support in skills.py.


@pytest.mark.asyncio
async def test_list_pending_passes_update_fields(loader, monkeypatch):
    """kind/target/base_version flow through the list handler untouched."""
    monkeypatch.setattr(
        loader,
        "list_pending_skills",
        lambda: [
            {
                "slug": "deploy-helper-update",
                "name": "auto/deploy-helper-update",
                "description": "d",
                "triggers": "t",
                "has_scripts": False,
                "created_at": "",
                "source": "consolidation",
                "kind": "update",
                "target": "auto/deploy-helper",
                "base_version": 3,
            }
        ],
    )
    resp = await H.api_skills_pending(_Req(loader))
    p = _payload(resp)["pending"][0]
    assert p["kind"] == "update"
    assert p["target"] == "auto/deploy-helper"
    assert p["base_version"] == 3


@pytest.mark.asyncio
async def test_update_detail_includes_live_body(loader, monkeypatch):
    """An update candidate's detail carries the target's current live body."""
    monkeypatch.setattr(
        loader,
        "get_pending_skill",
        lambda slug: {
            "slug": slug,
            "name": "auto/deploy-helper-update",
            "meta": {"kind": "update", "target": "auto/deploy-helper"},
            "content": "## Steps\nnew\n",
            "scripts": [],
        },
    )
    monkeypatch.setattr(
        loader, "preview_pending_update",
        lambda slug: {
            "live_body": "## Steps\nOLD BODY\n",
            "proposed_body": "## Steps\nNEW BODY\n",
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-OLD BODY\n+NEW BODY\n",
            "from_version": 2,
            "to_version": 3,
            "base_version": 2,
            "stale_base": False,
        },
        raising=False,
    )
    resp = await H.api_skill_pending_detail(
        _Req(loader, match={"slug": "deploy-helper-update"})
    )
    data = _payload(resp)
    assert data["live_body"] == "## Steps\nOLD BODY\n"
    assert data["proposed_body"] == "## Steps\nNEW BODY\n"
    assert "+NEW BODY" in data["diff"]
    assert (data["from_version"], data["to_version"]) == (2, 3)
    assert data["stale_base"] is False


@pytest.mark.asyncio
async def test_update_detail_live_body_null_when_target_gone(loader, monkeypatch):
    """If the target skill was removed, live_body is null (not an error)."""
    monkeypatch.setattr(
        loader,
        "get_pending_skill",
        lambda slug: {
            "slug": slug,
            "name": "auto/deploy-helper-update",
            "meta": {"kind": "update", "target": "auto/deploy-helper"},
            "content": "## Steps\nnew\n",
            "scripts": [],
        },
    )
    monkeypatch.setattr(
        loader, "preview_pending_update", lambda slug: None, raising=False,
    )
    resp = await H.api_skill_pending_detail(
        _Req(loader, match={"slug": "deploy-helper-update"})
    )
    data = _payload(resp)
    assert data["live_body"] is None
    assert data["diff"] is None
    assert data["stale_base"] is False


@pytest.mark.asyncio
async def test_new_detail_has_no_live_body(loader):
    """A plain (new) candidate detail does not gain a live_body field."""
    resp = await H.api_skill_pending_detail(_Req(loader, match={"slug": "deploy-helper"}))
    data = _payload(resp)
    assert "live_body" not in data


@pytest.mark.asyncio
async def test_approve_routes_update_to_approve_pending_update(loader, monkeypatch):
    """kind=='update' → approve_pending_update; approve_pending_skill untouched."""
    monkeypatch.setattr(
        loader,
        "get_pending_skill",
        lambda slug: {"slug": slug, "meta": {"kind": "update", "target": "auto/deploy-helper"}},
    )
    called: dict = {}

    def _upd(slug):
        called["update"] = slug
        return "auto/deploy-helper"

    def _new(slug):
        called["new"] = slug
        return "auto/should-not-run"

    monkeypatch.setattr(loader, "approve_pending_update", _upd, raising=False)
    monkeypatch.setattr(loader, "approve_pending_skill", _new)
    monkeypatch.setattr(loader, "run_skill_lifecycle", lambda **k: None)
    resp = await H.api_skill_pending_approve(
        _Req(loader, match={"slug": "deploy-helper-update"})
    )
    assert resp.status == 200
    assert _payload(resp)["approved"] == "auto/deploy-helper"
    assert called.get("update") == "deploy-helper-update"
    assert "new" not in called


@pytest.mark.asyncio
async def test_approve_routes_new_to_approve_pending_skill(loader, monkeypatch):
    """A candidate without kind=='update' promotes via approve_pending_skill."""
    called: dict = {}

    def _upd(slug):
        called["update"] = slug
        return "auto/should-not-run"

    monkeypatch.setattr(loader, "approve_pending_update", _upd, raising=False)
    # get_pending_skill + approve_pending_skill remain the real (part-A) impls.
    resp = await H.api_skill_pending_approve(_Req(loader, match={"slug": "deploy-helper"}))
    assert resp.status == 200
    assert _payload(resp)["approved"] == "auto/deploy-helper"
    assert "update" not in called
    assert loader.list_pending_skills() == []


@pytest.mark.asyncio
async def test_dismiss_routes_update_candidate_by_slug(loader, monkeypatch):
    """Dismiss is kind-agnostic — it deletes the pending dir by slug."""
    seen: dict = {}

    def _dismiss(slug):
        seen["slug"] = slug
        return True

    monkeypatch.setattr(loader, "dismiss_pending_skill", _dismiss)
    resp = await H.api_skill_pending_dismiss(
        _Req(loader, match={"slug": "deploy-helper-update"})
    )
    assert resp.status == 200
    assert seen["slug"] == "deploy-helper-update"
