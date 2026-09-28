"""Owner gate on four dashboard routes that reach the owner's logs, config or interpreter.

An allow-listed channel user can hold a dashboard token whose claims are
``app == ""`` with a non-owner subject. These routes must refuse that caller:

* ``POST /api/diagnostics/collect`` and ``GET /api/diagnostics/download/{filename}``
  build and serve a bundle carrying the owner's gateway and chat logs.
* ``POST /api/memory/embedding-model`` rewrites the owner's model config and
  re-embeds the owner's vector store.
* ``POST /api/apps/auto-improvement/deps/install`` runs pip in the gateway
  interpreter. Only the dashboard owner may run it; an app token is refused.

Every side effect is a stub that records whether it was reached.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.diagnostics import BundleResult

pytestmark = pytest.mark.asyncio

OWNER = "U0OWNER0000"
NON_OWNER = "U0NONOWNER"


def _client(app: web.Application, user: str, app_claim: str = "") -> TestClient:
    @web.middleware
    async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = app_claim
        return await handler(request)

    app.middlewares.append(_identity)
    return TestClient(TestServer(app))


def _base_app() -> web.Application:
    app = web.Application()
    app["state"] = MagicMock(owner_id=OWNER)
    return app


# ── diagnostics ──────────────────────────────────────────────────────────────


def _diagnostics_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[web.Application, list[dict]]:
    import kiro_crew.dashboard.handlers as handlers_pkg
    from kiro_crew.dashboard.handlers import diagnostics as dh

    calls: list[dict] = []
    out = tmp_path / "diagnostics"
    out.mkdir()
    (out / "existing.zip").write_bytes(b"PK\x03\x04owner-bundle")

    def _fake_collect(**kw: Any) -> BundleResult:
        calls.append(kw)
        return BundleResult(
            zip_path=out / "existing.zip",
            filename="existing.zip",
            included=["manifest.json"],
            skipped=[],
            redaction_summary={},
            github_issue_url="about:blank",
        )

    monkeypatch.setattr(dh, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(dh.diagnostics, "collect_bundle", _fake_collect)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())

    app = _base_app()
    app.router.add_post("/api/diagnostics/collect", dh.api_diagnostics_collect)
    app.router.add_get("/api/diagnostics/download/{filename}", dh.api_diagnostics_download)
    return app, calls


async def test_diagnostics_collect_refuses_non_owner(monkeypatch, tmp_path) -> None:
    app, calls = _diagnostics_app(monkeypatch, tmp_path)
    async with _client(app, NON_OWNER) as client:
        resp = await client.post("/api/diagnostics/collect", json={})
        status, body = resp.status, await resp.json()
    assert status == 403
    assert body["code"] == "owner_only"
    assert calls == []


async def test_diagnostics_download_refuses_non_owner(monkeypatch, tmp_path) -> None:
    app, _calls = _diagnostics_app(monkeypatch, tmp_path)
    async with _client(app, NON_OWNER) as client:
        resp = await client.get("/api/diagnostics/download/existing.zip")
        status, blob = resp.status, await resp.read()
    assert status == 403
    assert b"owner-bundle" not in blob


async def test_diagnostics_download_refuses_an_app_token(monkeypatch, tmp_path) -> None:
    """Not an App Kit route: an app token is not the owner either."""
    app, _calls = _diagnostics_app(monkeypatch, tmp_path)
    async with _client(app, OWNER, app_claim="some-app") as client:
        resp = await client.get("/api/diagnostics/download/existing.zip")
        status = resp.status
    assert status == 403


async def test_diagnostics_collect_and_download_allow_owner(monkeypatch, tmp_path) -> None:
    app, calls = _diagnostics_app(monkeypatch, tmp_path)
    async with _client(app, OWNER) as client:
        resp = await client.post("/api/diagnostics/collect", json={})
        assert resp.status == 200
        url = (await resp.json())["download_url"]
        dl = await client.get(url)
        status, blob = dl.status, await dl.read()
    assert len(calls) == 1
    assert status == 200
    assert blob == b"PK\x03\x04owner-bundle"


# ── embedding model ──────────────────────────────────────────────────────────


def _embedding_app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[web.Application, list, threading.Event]:
    from kiro_crew.dashboard.handlers import memory

    applied: list = []
    done = threading.Event()

    def _fake_apply(store: Any, raw: str, loop: Any) -> None:
        applied.append((store, raw))
        done.set()

    monkeypatch.delenv("KIROCREW_EMBED_MODEL_PATH", raising=False)
    monkeypatch.setattr(memory, "memory_startup_refusal", lambda: None)
    monkeypatch.setattr(
        memory, "validate_custom_model_path", lambda raw, origin="": (Path(raw), "", "")
    )
    monkeypatch.setattr(memory, "_get_vector_store_async", AsyncMock(return_value="STORE"))
    prog = MagicMock()
    prog.is_active.return_value = False
    monkeypatch.setattr(memory, "reembed_progress", lambda: prog)
    monkeypatch.setattr(memory, "embed_executor", lambda: None)
    monkeypatch.setattr(memory, "_apply_embedding_model", _fake_apply)
    monkeypatch.setattr(memory, "_sel", lambda: MagicMock())

    app = _base_app()
    app.router.add_post("/api/memory/embedding-model", memory.api_memory_embedding_model)
    return app, applied, done


_MODEL = {"path": "/srv/model.gguf"}


async def test_embedding_model_refuses_non_owner(monkeypatch) -> None:
    app, applied, done = _embedding_app(monkeypatch)
    async with _client(app, NON_OWNER) as client:
        resp = await client.post("/api/memory/embedding-model", json=_MODEL)
        status, body = resp.status, await resp.json()
    done.wait(1)
    assert status == 403
    assert body["code"] == "owner_only"
    assert applied == []


async def test_embedding_model_allows_owner(monkeypatch) -> None:
    app, applied, done = _embedding_app(monkeypatch)
    async with _client(app, OWNER) as client:
        resp = await client.post("/api/memory/embedding-model", json=_MODEL)
        status = resp.status
    assert done.wait(5)
    assert status == 200
    assert applied == [("STORE", _MODEL["path"])]


# ── auto-improvement deps install ────────────────────────────────────────────


def _deps_app(monkeypatch: pytest.MonkeyPatch) -> tuple[web.Application, list]:
    from kiro_crew.apps.builtins.auto_improvement.backend import deps, routes

    calls: list = []

    def _fake_install() -> dict:
        calls.append(True)
        return {"ok": True, "installed": ["ruff"]}

    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(deps, "install_deps", _fake_install)

    app = _base_app()
    app.router.add_post(
        "/api/apps/auto-improvement/deps/install",
        routes._require_enabled(routes._handle_deps_install),
    )
    return app, calls


async def test_deps_install_refuses_non_owner_dashboard_user(monkeypatch) -> None:
    app, calls = _deps_app(monkeypatch)
    async with _client(app, NON_OWNER) as client:
        resp = await client.post("/api/apps/auto-improvement/deps/install")
        status, body = resp.status, await resp.json()
    assert status == 403
    assert body["code"] == "owner_only"
    assert calls == []


async def test_deps_install_allows_owner(monkeypatch) -> None:
    app, calls = _deps_app(monkeypatch)
    async with _client(app, OWNER) as client:
        resp = await client.post("/api/apps/auto-improvement/deps/install")
        status = resp.status
    assert status == 200
    assert calls == [True]


async def test_deps_install_refuses_an_app_token(monkeypatch) -> None:
    """An app token is not the owner, even the app's own token."""
    app, calls = _deps_app(monkeypatch)
    async with _client(app, "app-subject", app_claim="auto-improvement") as client:
        resp = await client.post("/api/apps/auto-improvement/deps/install")
        status = resp.status
    assert status == 403
    assert calls == []


# ── allowed decisions are audited ────────────────────────────────────────────


def _record_offloads(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    """Record the name of every function a module hands to ``asyncio.to_thread``."""
    offloaded: list[str] = []
    real = module.asyncio.to_thread

    async def _to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(module.asyncio, "to_thread", _to_thread)
    return offloaded


async def test_diagnostics_collect_audits_the_allowed_build(monkeypatch, tmp_path) -> None:
    import kiro_crew.dashboard.handlers as handlers_pkg
    from kiro_crew.dashboard.handlers import diagnostics as dh

    app, calls = _diagnostics_app(monkeypatch, tmp_path)
    audit = MagicMock()
    monkeypatch.setattr(handlers_pkg, "sel", lambda: audit)
    offloaded = _record_offloads(monkeypatch, dh)
    async with _client(app, OWNER) as client:
        resp = await client.post("/api/diagnostics/collect", json={})
        status = resp.status
    assert status == 200
    assert len(calls) == 1
    allowed = [
        c.kwargs
        for c in audit.log_tool_invocation.call_args_list
        if c.kwargs.get("tool_name") == "diagnostics_collect"
    ]
    assert [a["outcome"] for a in allowed] == ["allowed"]
    assert "_audit_collect_allowed_sync" in offloaded, "the audit must run off the event loop"


async def test_deps_install_audits_the_allowed_install(monkeypatch) -> None:
    import kiro_crew.sel as sel_mod
    from kiro_crew.apps.builtins.auto_improvement.backend import routes

    app, calls = _deps_app(monkeypatch)
    audit = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: audit)
    offloaded = _record_offloads(monkeypatch, routes)
    async with _client(app, OWNER) as client:
        resp = await client.post("/api/apps/auto-improvement/deps/install")
        status = resp.status
    assert status == 200
    assert calls == [True]
    allowed = [
        c.kwargs
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("operation") == "auto_improvement.deps_install"
    ]
    assert [(a["outcome"], a["caller"]) for a in allowed] == [("allowed", OWNER)]
    assert "_audit_deps_install_allowed_sync" in offloaded, "the audit must run off the event loop"
