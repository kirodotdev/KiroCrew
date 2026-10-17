"""The allowed-host routes behind a blocked-link card's *Allow for this host*.

The aiohttp handlers are exercised through an in-test ``TestClient`` opened with
``async with``, as the rest of the suite does (async-gen fixtures are avoided).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import redaction as handlers
from kiro_crew.security import redaction_allow


class _State:
    """The owner shape ``as_owner`` expects, plus the slots the allow route reads."""

    owner_id = ""

    def __init__(self, slots: dict[str, str]) -> None:
        self._slots = {k: SimpleNamespace(workspace=ws) for k, ws in slots.items()}

    def get_slot(self, key: str):
        return self._slots.get(key)


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "redaction-allow" / "hosts.json"
    monkeypatch.setattr(redaction_allow, "_path_override", path)
    monkeypatch.setattr(redaction_allow, "_snapshot", None)
    monkeypatch.setattr(redaction_allow, "_loading", False)
    monkeypatch.setattr(handlers, "_audit", lambda *a, **k: None)
    return path


def _client(slots: dict[str, str] | None = None) -> TestClient:
    app = web.Application()
    app["state"] = _State(slots or {"s1": "ws1", "odd": "team/dev"})
    app.router.add_get("/api/redaction/allowed-hosts", handlers.api_redaction_allowed_hosts)
    app.router.add_post("/api/redaction/allowed-hosts", handlers.api_redaction_allow_host)
    app.router.add_delete("/api/redaction/allowed-hosts", handlers.api_redaction_revoke_host)
    return TestClient(TestServer(as_owner(app)))


@pytest.mark.asyncio
async def test_allow_list_and_revoke_through_the_routes() -> None:
    async with _client() as client:
        res = await client.post(
            "/api/redaction/allowed-hosts", json={"slot": "s1", "host": "Reviews.Corp.Example"}
        )
        assert res.status == 200
        assert await res.json() == {"ok": True, "workspace": "ws1"}
        res = await client.get("/api/redaction/allowed-hosts")
        assert (await res.json())["workspaces"] == {"ws1": ["reviews.corp.example"]}
        res = await client.delete(
            "/api/redaction/allowed-hosts",
            params={"workspace": "ws1", "host": "reviews.corp.example"},
        )
        assert await res.json() == {"ok": True, "removed": True}
        res = await client.get("/api/redaction/allowed-hosts")
        assert (await res.json())["workspaces"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"slot": "s1", "host": "not a host"}, "invalid_host"),
        ({"slot": 3, "host": "reviews.corp.example"}, "invalid_slot"),
        (["not", "an", "object"], "body_not_object"),
    ],
)
async def test_allow_refuses_off_shape_input_with_a_code(body, code) -> None:
    async with _client() as client:
        res = await client.post("/api/redaction/allowed-hosts", json=body)
        assert res.status == 400
        assert (await res.json())["code"] == code


@pytest.mark.asyncio
async def test_allow_caps_a_chunked_body_without_a_declared_length() -> None:
    async def _chunks():
        yield b'{"slot": "s1", "host": "'
        yield b"a" * 8192
        yield b'"}'

    async with _client() as client:
        res = await client.post(
            "/api/redaction/allowed-hosts",
            data=_chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert res.status == 413
        assert (await res.json())["code"] == "payload_too_large"
    assert redaction_allow.list_allowed() == {}


@pytest.mark.asyncio
async def test_allow_refuses_an_unknown_slot_and_an_unsupported_workspace() -> None:
    async with _client() as client:
        res = await client.post(
            "/api/redaction/allowed-hosts", json={"slot": "nope", "host": "a.example"}
        )
        assert res.status == 404
        assert (await res.json())["code"] == "slot_not_found"
        res = await client.post(
            "/api/redaction/allowed-hosts", json={"slot": "odd", "host": "a.example"}
        )
        assert res.status == 400
        assert (await res.json())["code"] == "unsupported_workspace"
    assert redaction_allow.list_allowed() == {}


@pytest.mark.asyncio
async def test_allow_reports_a_full_list(monkeypatch) -> None:
    monkeypatch.setattr(redaction_allow, "MAX_HOSTS_PER_WORKSPACE", 1)
    async with _client() as client:
        ok = await client.post(
            "/api/redaction/allowed-hosts", json={"slot": "s1", "host": "a.example"}
        )
        assert ok.status == 200
        full = await client.post(
            "/api/redaction/allowed-hosts", json={"slot": "s1", "host": "b.example"}
        )
        assert full.status == 400
        assert (await full.json())["code"] == "allow_list_full"


@pytest.mark.asyncio
async def test_revoke_refuses_an_off_shape_host() -> None:
    async with _client() as client:
        res = await client.delete(
            "/api/redaction/allowed-hosts", params={"workspace": "ws1", "host": "x y"}
        )
        assert res.status == 400
        assert (await res.json())["code"] == "invalid_host"


def test_the_allow_list_adds_no_gateway_startup_work() -> None:
    """The list loads on its first read, off the boot path."""
    from kiro_crew.dashboard.routes import system

    source = Path(system.__file__).read_text(encoding="utf-8")
    assert "redaction_handlers.preload" not in source
    assert not hasattr(handlers, "preload_allowed_hosts")
