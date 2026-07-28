"""Tests for the authenticated onboarding import API."""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import handlers


class _AuditLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **event: Any) -> None:
        self.events.append(event)


def _handler_module():
    return importlib.import_module("kiro_crew.dashboard.handlers.onboarding_import")


def _make_app(module, state: object | None = None) -> web.Application:
    @web.middleware
    async def test_auth(request: web.Request, handler):
        caller = request.headers.get("X-Test-User")
        if caller:
            request["user"] = caller
        return await handler(request)

    app = web.Application(middlewares=[test_auth])
    app["state"] = state or SimpleNamespace()
    app.router.add_get("/api/onboarding/import/scan", module.api_onboarding_import_scan)
    app.router.add_post("/api/onboarding/import/apply", module.api_onboarding_import_apply)
    app.router.add_put("/api/onboarding/import/state", module.api_onboarding_import_state)
    return app


def test_handlers_package_exports_onboarding_import_endpoints() -> None:
    assert handlers.api_onboarding_import_scan is not None
    assert handlers.api_onboarding_import_apply is not None
    assert handlers.api_onboarding_import_state is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/onboarding/import/scan"),
        ("post", "/api/onboarding/import/apply"),
        ("put", "/api/onboarding/import/state"),
    ],
)
async def test_all_onboarding_import_endpoints_require_authentication(
    monkeypatch, method: str, path: str
) -> None:
    module = _handler_module()
    audit = _AuditLog()
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await getattr(client, method)(path, json={})
        response_body = await response.json()

    assert response.status == 401
    assert response_body == {"error": "authentication required"}
    assert audit.events[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_scan_runs_preview_off_event_loop_and_returns_result(monkeypatch) -> None:
    module = _handler_module()
    audit = _AuditLog()
    event_loop_thread = threading.get_ident()
    preview_threads: list[int] = []

    def preview_import(source_ids=None):
        preview_threads.append(threading.get_ident())
        return {
            "sources": [
                {
                    "id": "claude_code",
                    "name": "Claude Code",
                    "root": "/Users/alice/.claude",
                    "categories": [
                        {
                            "id": "skills",
                            "label": "Skills",
                            "count": 2,
                            "selected": True,
                        }
                    ],
                }
            ],
            "source_ids": source_ids,
            "off_thread": threading.get_ident() != event_loop_thread,
            "selection": [{"source_id": "claude_code", "category_id": "skills"}],
            "skipped": [
                {
                    "source_id": "claude_code",
                    "category_id": "settings",
                    "reason": "credential_bearing_setting",
                }
            ],
        }

    monkeypatch.setattr(module, "_backend", lambda: SimpleNamespace(preview_import=preview_import))
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.get(
            "/api/onboarding/import/scan",
            headers={"X-Test-User": "owner"},
        )
        response_body = await response.json()

    assert response.status == 200
    assert response_body == {
        "sources": [
            {
                "id": "claude_code",
                "name": "Claude Code",
                "detected": True,
                "categories": [
                    {
                        "id": "skills",
                        "label": "Skills",
                        "count": 2,
                        "description": "User-authored skills and supporting files",
                    }
                ],
            }
        ],
        "skipped": [
            {
                "source": "Claude Code",
                "category": "Settings",
                "reason": "credential_bearing_setting",
            }
        ],
        "merge_only": True,
    }
    assert "/Users/alice" not in str(response_body)
    assert len(preview_threads) == 1
    assert preview_threads[0] != event_loop_thread
    assert audit.events[-1] == {
        "caller": "owner",
        "operation": "onboarding.import.scan",
        "outcome": "completed",
        "source": "dashboard",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"sources": "claude_code"},
        {"sources": []},
        {"sources": [{"id": "", "categories": ["skills"]}]},
        {"sources": [{"id": "claude_code", "categories": "skills"}]},
        {"sources": [{"id": "claude_code", "categories": []}]},
        {"sources": [{"id": "unknown", "categories": ["skills"]}]},
        {"sources": [{"id": "claude_code", "categories": ["unknown"]}]},
    ],
)
async def test_apply_rejects_invalid_plan_with_generic_400(monkeypatch, body: object) -> None:
    module = _handler_module()
    audit = _AuditLog()
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/onboarding/import/apply",
            json=body,
            headers={"X-Test-User": "owner"},
        )
        response_body = await response.json()

    assert response.status == 400
    assert response_body == {"error": "invalid request"}
    assert audit.events[-1]["outcome"] == "failed"
    assert audit.events[-1]["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_apply_rejects_malformed_json(monkeypatch) -> None:
    module = _handler_module()
    audit = _AuditLog()
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/onboarding/import/apply",
            data="{",
            headers={
                "Content-Type": "application/json",
                "X-Test-User": "owner",
            },
        )
        response_body = await response.json()

    assert response.status == 400
    assert response_body == {"error": "invalid request"}
    assert audit.events[-1]["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_apply_runs_off_thread_with_state_dependencies(monkeypatch) -> None:
    module = _handler_module()
    audit = _AuditLog()
    event_loop_thread = threading.get_ident()
    cron_service = object()
    vector_memory = object()
    lesson_store = object()
    state = SimpleNamespace(
        crons=cron_service,
        lessons=lesson_store,
        context_builder=SimpleNamespace(memory=SimpleNamespace(vector_store=vector_memory)),
    )
    request_body = {
        "sources": [
            {"id": "claude_code", "categories": ["skills", "memories"]},
        ]
    }
    fresh_plan = {
        "sources": [
            {
                "id": "claude_code",
                "categories": [
                    {"id": "skills", "selected": True},
                    {"id": "memories", "selected": True},
                    {"id": "workspaces", "selected": True},
                ],
            }
        ],
        "selection": [
            {"source_id": "claude_code", "category_id": "skills"},
            {"source_id": "claude_code", "category_id": "memories"},
            {"source_id": "claude_code", "category_id": "workspaces"},
        ],
    }
    preview_calls: list[list[str] | None] = []
    received: dict[str, object] = {}

    def preview_import(source_ids=None):
        preview_calls.append(source_ids)
        return fresh_plan

    def apply_import(
        received_plan,
        cron_service=None,
        vector_store=None,
        lesson_store=None,
    ):
        received.update(
            {
                "selection": received_plan["selection"],
                "category_selections": [
                    category["selected"] for category in received_plan["sources"][0]["categories"]
                ],
                "has_cron_service": cron_service is state.crons,
                "has_vector_store": vector_store is state.context_builder.memory.vector_store,
                "has_lesson_store": lesson_store is state.lessons,
                "off_thread": threading.get_ident() != event_loop_thread,
            }
        )
        return {
            "imported": {"skills": 2, "memories": 1},
            "imported_count": 3,
            "already_imported": 1,
            "conflicts": [{"reason": "destination_conflict"}],
            "skipped": [{"reason": "write_failed"}],
            "secret_count": 2,
            "item_outcomes": [
                {
                    "source_id": "claude_code",
                    "category_id": "skills",
                    "item_hash": "a" * 64,
                    "outcome": "accepted",
                },
                {
                    "source_id": "claude_code",
                    "category_id": "skills",
                    "item_hash": "b" * 64,
                    "outcome": "deduplicated",
                },
            ],
        }

    monkeypatch.setattr(
        module,
        "_backend",
        lambda: SimpleNamespace(preview_import=preview_import, apply_import=apply_import),
    )
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module, state))) as client:
        response = await client.post(
            "/api/onboarding/import/apply",
            json=request_body,
            headers={"X-Test-User": "owner"},
        )
        response_body = await response.json()

    assert response.status == 200
    assert response_body == {
        "ok": True,
        "summary": {
            "imported": 3,
            "deduplicated": 1,
            "skipped": 4,
        },
    }
    assert received == {
        "selection": [
            {"source_id": "claude_code", "category_id": "skills"},
            {"source_id": "claude_code", "category_id": "memories"},
        ],
        "category_selections": [True, True, False],
        "has_cron_service": True,
        "has_vector_store": True,
        "has_lesson_store": True,
        "off_thread": True,
    }
    assert preview_calls == [["claude_code"]]
    item_events = [
        event for event in audit.events if event["operation"] == "onboarding.import.item"
    ]
    assert [event["outcome"] for event in item_events] == ["accepted", "deduplicated"]
    assert item_events[0]["resources"] == f"claude_code:skills:{'a' * 64}"
    assert audit.events[-1]["outcome"] == "completed"


@pytest.mark.asyncio
async def test_apply_rebuilds_agent_config_after_mcp_import(monkeypatch) -> None:
    module = _handler_module()
    rebuild_threads: list[int] = []
    event_loop_thread = threading.get_ident()

    def preview_import(source_ids=None):
        return {
            "sources": [
                {
                    "id": "codex",
                    "categories": [{"id": "mcp_servers", "selected": True}],
                }
            ],
            "selection": [{"source_id": "codex", "category_id": "mcp_servers"}],
        }

    def apply_import(plan, **kwargs):
        return {
            "imported": {"mcp_servers": 1},
            "imported_count": 1,
            "already_imported": 0,
        }

    def rebuild_agent_config() -> None:
        rebuild_threads.append(threading.get_ident())

    monkeypatch.setattr(
        module,
        "_backend",
        lambda: SimpleNamespace(preview_import=preview_import, apply_import=apply_import),
    )
    monkeypatch.setattr(module, "_rebuild_agent_config", rebuild_agent_config)
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.post(
            "/api/onboarding/import/apply",
            json={"sources": [{"id": "codex", "categories": ["mcp_servers"]}]},
            headers={"X-Test-User": "owner"},
        )

    assert response.status == 200
    assert len(rebuild_threads) == 1
    assert rebuild_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_apply_serializes_shared_config_mutations(monkeypatch) -> None:
    module = _handler_module()
    active = 0
    max_active = 0
    activity_lock = threading.Lock()

    def preview_import(source_ids=None):
        return {
            "sources": [
                {
                    "id": "codex",
                    "categories": [{"id": "settings", "selected": True}],
                }
            ],
            "selection": [{"source_id": "codex", "category_id": "settings"}],
        }

    def apply_import(plan, **kwargs):
        nonlocal active, max_active
        with activity_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with activity_lock:
            active -= 1
        return {"imported_count": 0, "already_imported": 0}

    monkeypatch.setattr(
        module,
        "_backend",
        lambda: SimpleNamespace(preview_import=preview_import, apply_import=apply_import),
    )
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    request_body = {"sources": [{"id": "codex", "categories": ["settings"]}]}

    async with TestClient(TestServer(_make_app(module))) as client:
        responses = await asyncio.gather(
            client.post(
                "/api/onboarding/import/apply",
                json=request_body,
                headers={"X-Test-User": "owner"},
            ),
            client.post(
                "/api/onboarding/import/apply",
                json=request_body,
                headers={"X-Test-User": "owner"},
            ),
        )

    assert [response.status for response in responses] == [200, 200]
    assert max_active == 1


@pytest.mark.asyncio
async def test_apply_uses_none_for_unavailable_state_dependencies(monkeypatch) -> None:
    module = _handler_module()
    received: dict[str, object] = {}

    def apply_import(
        plan,
        cron_service=None,
        vector_store=None,
        lesson_store=None,
    ):
        received.update(
            {
                "plan": plan,
                "cron_service": cron_service,
                "vector_store": vector_store,
                "lesson_store": lesson_store,
            }
        )
        return {"ok": True}

    def preview_import(source_ids=None):
        return {
            "sources": [
                {
                    "id": "codex",
                    "categories": [{"id": "settings", "selected": True}],
                }
            ],
            "selection": [{"source_id": "codex", "category_id": "settings"}],
        }

    monkeypatch.setattr(
        module,
        "_backend",
        lambda: SimpleNamespace(preview_import=preview_import, apply_import=apply_import),
    )
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())
    request_body = {"sources": [{"id": "codex", "categories": ["settings"]}]}

    async with TestClient(TestServer(_make_app(module, SimpleNamespace()))) as client:
        response = await client.post(
            "/api/onboarding/import/apply",
            json=request_body,
            headers={"X-Test-User": "owner"},
        )

    assert response.status == 200
    assert received == {
        "plan": {
            "sources": [
                {
                    "id": "codex",
                    "categories": [{"id": "settings", "selected": True}],
                }
            ],
            "selection": [{"source_id": "codex", "category_id": "settings"}],
        },
        "cron_service": None,
        "vector_store": None,
        "lesson_store": None,
    }


@pytest.mark.asyncio
async def test_state_persists_import_onboarded(monkeypatch, tmp_path) -> None:
    module = _handler_module()
    audit = _AuditLog()
    saved = tmp_path / "saved.txt"
    dashboard = SimpleNamespace(import_onboarded=False)

    class Config:
        def __init__(self) -> None:
            self.dashboard = dashboard

        def save(self) -> None:
            saved.write_text(str(self.dashboard.import_onboarded), encoding="utf-8")

    config = Config()
    monkeypatch.setattr(module.KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.put(
            "/api/onboarding/import/state",
            json={"completed": True},
            headers={"X-Test-User": "owner"},
        )
        response_body = await response.json()

    assert response.status == 200
    assert response_body == {"ok": True}
    assert saved.read_text(encoding="utf-8") == "True"
    assert audit.events[-1]["outcome"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, [], {"completed": 1}, {"completed": "true"}])
async def test_state_rejects_invalid_completed_boolean(monkeypatch, body: object) -> None:
    module = _handler_module()
    monkeypatch.setattr(module, "_sel", lambda: _AuditLog())

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.put(
            "/api/onboarding/import/state",
            json=body,
            headers={"X-Test-User": "owner"},
        )
        response_body = await response.json()

    assert response.status == 400
    assert response_body == {"error": "invalid request"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/onboarding/import/scan"),
        ("post", "/api/onboarding/import/apply"),
    ],
)
async def test_import_failures_do_not_expose_private_details(
    monkeypatch, method: str, path: str
) -> None:
    module = _handler_module()
    audit = _AuditLog()
    private_detail = "/Users/alice/.claude/private-token"

    def fail(*args, **kwargs):
        raise RuntimeError(private_detail)

    backend = SimpleNamespace(preview_import=fail, apply_import=fail)
    monkeypatch.setattr(module, "_backend", lambda: backend)
    monkeypatch.setattr(module, "_sel", lambda: audit)
    kwargs: dict[str, object] = {"headers": {"X-Test-User": "owner"}}
    if method == "post":
        kwargs["json"] = {"sources": [{"id": "claude_code", "categories": ["skills"]}]}

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await getattr(client, method)(path, **kwargs)
        response_body = await response.json()

    assert response.status == 500
    assert private_detail not in str(response_body)
    assert private_detail not in str(audit.events)
    assert audit.events[-1]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_state_failure_is_generic_and_credential_free(monkeypatch) -> None:
    module = _handler_module()
    audit = _AuditLog()
    private_detail = "/Users/alice/.kiro/crew/config.json"

    def fail_load():
        raise OSError(private_detail)

    monkeypatch.setattr(module.KiroCrewConfig, "load", fail_load)
    monkeypatch.setattr(module, "_sel", lambda: audit)

    async with TestClient(TestServer(_make_app(module))) as client:
        response = await client.put(
            "/api/onboarding/import/state",
            json={"completed": True},
            headers={"X-Test-User": "owner"},
        )
        response_body = await response.json()

    assert response.status == 500
    assert response_body == {"error": "request failed"}
    assert private_detail not in str(audit.events)
    assert audit.events[-1]["outcome"] == "failed"
