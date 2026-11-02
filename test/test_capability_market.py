"""Tests for the marketplace ops on the ``CapabilityManager`` seam and their routes.

A marketplace listing (a skill, an MCP server or a theme, possibly with bundled
components and dependencies) is installed by the EDITION's own installer: the core
never downloads, verifies or places a file itself, so the files land exactly where
that installer puts them from its own command line. The core adds three ops and
the routes in front of them:

  * ``market_available()``;
  * ``market_plan(ref)`` -- a read: what installing ``ref`` would place, with every
    item's sha256 and publisher, the union of declared permissions and a
    ``planDigest`` that names this exact plan;
  * ``market_apply(ref, plan_digest)`` -- install only the plan the user confirmed.

Covered: the ``Default`` adapter (public build: unavailable), the liveness wrapper
(every op bounded, feature detection, the digest and per-item governance check at
apply), and the routes on a real aiohttp app (human-only, owner-only, input
validation, plan shape). Every test runs with ``HOME`` and ``KIROCREW_HOME`` in a
temp directory.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.platform.context as platform_context
from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.platform.capability_bound import (
    CAPABILITY_INSTALL_TIMEOUT,
    CAPABILITY_READ_TIMEOUT,
    BoundedCapabilityManager,
)
from kiro_crew.platform.defaults import DefaultCapabilityManager
from kiro_crew.platform.interfaces import CapabilityResult

_FIXTURE = Path(__file__).parent / "fixtures" / "market_install_plan.json"
_DIGEST = "5016ee2afad0fa54c71fa8cf4dbb57afb85ea238cb68911db0e4f5875b99156c"
_REF = "team-a/pdf-tools"


def _plan() -> Dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def temp_home(tmp_path, monkeypatch):
    """Never the real ``~/.kiro``: anything that reached the filesystem lands here."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kiro" / "crew"))
    return tmp_path


_OPS = {
    "market_plan": CAPABILITY_READ_TIMEOUT,
    "market_apply": CAPABILITY_INSTALL_TIMEOUT,
}


class _FakeManager:
    def __init__(self, *, available: bool = True, ok: bool = True, plan: Any = None) -> None:
        self._available = available
        self._ok = ok
        self._plan = _plan() if plan is None else plan
        self.calls: List[tuple] = []

    def available(self) -> bool:
        return self._available

    def market_available(self) -> bool:
        return self._available

    async def market_plan(self, ref: str) -> Dict[str, Any]:
        self.calls.append(("plan", ref))
        return self._plan

    async def market_apply(self, ref: str, plan_digest: str) -> CapabilityResult:
        self.calls.append(("apply", ref, plan_digest))
        return CapabilityResult(ok=self._ok, message="" if self._ok else "the plan changed")


class _PreMarketEdition:
    """An edition manager written before the marketplace ops: it has none of them."""

    def available(self) -> bool:
        return True


class TestDefaultAdapter:
    def test_unavailable(self):
        assert DefaultCapabilityManager().market_available() is False

    def test_plan_is_empty(self):
        assert asyncio.run(DefaultCapabilityManager().market_plan(_REF)) == {}

    def test_apply_fails_closed(self):
        result = asyncio.run(DefaultCapabilityManager().market_apply(_REF, _DIGEST))
        assert result.ok is False
        assert "not available" in result.message


class TestLivenessBound:
    def test_every_market_op_is_bounded(self, monkeypatch):
        recorded: List[float] = []
        real_wait_for = asyncio.wait_for

        async def spy(awaitable, timeout):
            recorded.append(timeout)
            return await real_wait_for(awaitable, timeout)

        monkeypatch.setattr(asyncio, "wait_for", spy)
        bounded = BoundedCapabilityManager(_FakeManager())
        for op, expected in _OPS.items():
            recorded.clear()
            args = (_REF, _DIGEST) if op == "market_apply" else (_REF,)
            asyncio.run(getattr(bounded, op)(*args))
            # apply first re-reads the plan (a bounded read) to ask governance per item.
            prior = [CAPABILITY_READ_TIMEOUT] if op == "market_apply" else []
            assert recorded == [*prior, expected], f"{op} not bounded at {expected}s"


class TestAnEditionWithoutTheOps:
    def test_reports_unavailable_instead_of_raising(self):
        bounded = BoundedCapabilityManager(_PreMarketEdition())  # type: ignore[arg-type]
        assert bounded.market_available() is False
        assert asyncio.run(bounded.market_plan(_REF)) == {}
        assert asyncio.run(bounded.market_apply(_REF, _DIGEST)).ok is False


class TestApplyAtTheSeam:
    def test_a_changed_plan_is_refused_before_the_edition_installs(self):
        """The digest the user confirmed must still name the plan, or nothing runs."""
        inner = _FakeManager()
        result = asyncio.run(BoundedCapabilityManager(inner).market_apply(_REF, "0" * 64))
        assert result.ok is False
        assert inner.calls == [("plan", _REF)]

    def test_every_skill_and_mcp_item_is_asked_of_governance(self, monkeypatch):
        """An allowlist naming skills only refuses a plan that also brings an MCP server."""
        from kiro_crew import extension_governance as gov

        asked: List[tuple] = []

        def denied(kind: str, ident: str):
            asked.append((kind, ident))
            return "mcp servers are not allowed" if kind == "mcp" else None

        monkeypatch.setattr(gov, "extensions_off", lambda action: None)
        monkeypatch.setattr(gov, "extension_install_denied", denied)
        inner = _FakeManager()
        result = asyncio.run(BoundedCapabilityManager(inner).market_apply(_REF, _DIGEST))
        assert result.ok is False
        assert "governance" in result.message
        assert asked == [("skill", _REF), ("mcp", "team-a/docs-mcp")]
        assert ("apply", _REF, _DIGEST) not in inner.calls


# ── the routes, on a real app ──────────────────────────────────────────────────


class _FakeState:
    owner_id = ""

    def __init__(self) -> None:
        self.refreshed: List[str] = []

    def push_refresh(self, what: str) -> None:
        self.refreshed.append(what)


def _app(manager: Any, monkeypatch) -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        # Stand-in for the token-auth middleware: the claims the owner predicate
        # and the internal-secret check read. Tests select the caller by header.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = ""
        if request.headers.get("X-Test-Internal") == "1":
            request["internal_auth"] = True
        return await handler(request)

    class _Ctx:
        capability_manager = manager

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.market.rebuild_agent_spec", lambda context: True
    )
    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agent_config_routes.register(app)
    return app


def _request(app: web.Application, method: str, path: str, **kw) -> tuple[int, Any]:
    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.request(method, path, **kw)
            return resp.status, await resp.json()

    return asyncio.run(go())


_POSTS = [
    ("/api/capability/market/plan", {"ref": _REF}),
    ("/api/capability/market/apply", {"ref": _REF, "plan_digest": _DIGEST}),
]


class TestStatusRoute:
    def test_available(self, temp_home, monkeypatch):
        status, body = _request(_app(_FakeManager(), monkeypatch), "GET", "/api/capability/market")
        assert (status, body) == (200, {"available": True})

    def test_an_edition_without_the_ops_says_unavailable(self, temp_home, monkeypatch):
        mgr = BoundedCapabilityManager(_PreMarketEdition())  # type: ignore[arg-type]
        status, body = _request(_app(mgr, monkeypatch), "GET", "/api/capability/market")
        assert (status, body) == (200, {"available": False})


class TestPlanRoute:
    def test_returns_the_edition_plan_verbatim(self, temp_home, monkeypatch):
        mgr = _FakeManager()
        status, body = _request(
            _app(mgr, monkeypatch), "POST", "/api/capability/market/plan", json={"ref": _REF}
        )
        assert (status, body) == (200, _plan())
        assert mgr.calls == [("plan", _REF)]

    def test_a_bounded_range_is_a_valid_ref(self, temp_home, monkeypatch):
        mgr = _FakeManager()
        ref = "team-a/pdf-tools@^1.0.0"
        status, _ = _request(
            _app(mgr, monkeypatch), "POST", "/api/capability/market/plan", json={"ref": ref}
        )
        assert status == 200
        assert mgr.calls == [("plan", ref)]

    @pytest.mark.parametrize(
        "plan",
        [
            {},
            {"items": [], "permissions": {}},
            {"items": "x", "permissions": {}, "planDigest": _DIGEST},
            {"items": [], "permissions": [], "planDigest": _DIGEST},
            {"items": [], "permissions": {}, "planDigest": "not-a-digest"},
            # Each field the dialog reads, dropped or mistyped one at a time.
            {k: v for k, v in _plan().items() if k != "conflicts"},
            {
                **_plan(),
                "permissions": {k: v for k, v in _plan()["permissions"].items() if k != "commands"},
            },
            {**_plan(), "permissions": {**_plan()["permissions"], "commands": ["uvx x"]}},
            {**_plan(), "permissions": {**_plan()["permissions"], "network": "somewhere"}},
            {**_plan(), "installable": "yes"},
            {**_plan(), "refusal": {"code": "X"}},
            {**_plan(), "items": [{**_plan()["items"][0], "publisher": "alice"}]},
            {**_plan(), "items": [{k: v for k, v in _plan()["items"][0].items() if k != "sha256"}]},
            {**_plan(), "items": [{**_plan()["items"][0], "requiredBy": None}]},
        ],
    )
    def test_a_malformed_plan_is_502_not_shown(self, temp_home, monkeypatch, plan):
        """The dialog must never ask for consent to a plan it cannot show in full."""
        status, body = _request(
            _app(_FakeManager(plan=plan), monkeypatch),
            "POST",
            "/api/capability/market/plan",
            json={"ref": _REF},
        )
        assert status == 502
        assert body["code"] == "market_plan_malformed"


class TestApplyRoute:
    def test_passes_the_confirmed_digest(self, temp_home, monkeypatch):
        mgr = _FakeManager()
        app = _app(mgr, monkeypatch)
        status, body = _request(
            app, "POST", "/api/capability/market/apply", json={"ref": _REF, "plan_digest": _DIGEST}
        )
        assert status == 200
        assert body["ok"] is True
        assert mgr.calls == [("apply", _REF, _DIGEST)]
        assert app["state"].refreshed == ["agents"]

    @pytest.mark.parametrize(
        "digest", [None, "", "ABC", _DIGEST.upper(), _DIGEST[:-1], _DIGEST + "0", 7]
    )
    def test_400_without_a_well_formed_digest(self, temp_home, monkeypatch, digest):
        """No digest, no install: an apply must name the plan the user saw."""
        mgr = _FakeManager()
        body: Dict[str, Any] = {"ref": _REF}
        if digest is not None:
            body["plan_digest"] = digest
        status, _ = _request(
            _app(mgr, monkeypatch), "POST", "/api/capability/market/apply", json=body
        )
        assert status == 400
        assert mgr.calls == []

    def test_an_edition_refusal_is_reported(self, temp_home, monkeypatch):
        status, body = _request(
            _app(_FakeManager(ok=False), monkeypatch),
            "POST",
            "/api/capability/market/apply",
            json={"ref": _REF, "plan_digest": _DIGEST},
        )
        assert (status, body) == (409, {"error": "the plan changed", "code": "market_refused"})


class TestEveryMarketPost:
    @pytest.mark.parametrize("path,body", _POSTS)
    def test_human_only(self, temp_home, monkeypatch, path, body):
        """An agent holding the internal secret cannot install third-party code."""
        mgr = _FakeManager()
        status, resp = _request(
            _app(mgr, monkeypatch), "POST", path, json=body, headers={"X-Test-Internal": "1"}
        )
        assert status == 403
        assert resp["code"] == "human_only"
        assert mgr.calls == []

    @pytest.mark.parametrize("path,body", _POSTS)
    def test_owner_only(self, temp_home, monkeypatch, path, body):
        mgr = _FakeManager()
        status, _ = _request(
            _app(mgr, monkeypatch), "POST", path, json=body, headers={"X-Test-User": "someone"}
        )
        assert status == 403
        assert mgr.calls == []

    @pytest.mark.parametrize("path,body", _POSTS)
    def test_503_when_unavailable(self, temp_home, monkeypatch, path, body):
        mgr = _FakeManager(available=False)
        status, _ = _request(_app(mgr, monkeypatch), "POST", path, json=body)
        assert status == 503
        assert mgr.calls == []

    @pytest.mark.parametrize("path,body", _POSTS)
    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "   ",
            "-x",
            "--yes",
            "pdf-tools",
            "a/../b",
            "../a/b",
            "a/b/c",
            "a b/c",
            "a/b;c",
            "a/$(x)",
            "a/b@",
            "/a/b",
            3,
        ],
    )
    def test_400_on_a_bad_ref(self, temp_home, monkeypatch, path, body, ref):
        mgr = _FakeManager()
        status, _ = _request(_app(mgr, monkeypatch), "POST", path, json={**body, "ref": ref})
        assert status == 400
        assert mgr.calls == []
