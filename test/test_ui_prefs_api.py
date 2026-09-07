"""Tests for the GET/PUT /api/ui-prefs endpoint."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import ui_prefs as ui_prefs_store
from kiro_crew.dashboard.handlers import ui_prefs as ui_prefs_handler


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_prefs_store, "config_dir", lambda: tmp_path)
    # Each test gets its own writer lock, so a lock left held by a failing test
    # cannot deadlock the next one.
    monkeypatch.setattr(ui_prefs_handler, "_lock", None)
    return tmp_path


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run PAST the owner gate by default. The gate itself is asserted by
    ``test_a_non_owner_cannot_write`` and by the registrar-wide invariant in
    test_agent_config_owner_gate_invariant.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/ui-prefs", ui_prefs_handler.api_ui_prefs)
    app.router.add_put("/api/ui-prefs", ui_prefs_handler.api_ui_prefs)
    return app


def _client() -> TestClient:
    """A client for the endpoint under test.

    Deliberately a helper used via ``async with`` rather than a pytest fixture:
    an async fixture needs a plugin this suite does not enable, and would error
    at setup instead of running.
    """
    return TestClient(TestServer(_make_app()))


@pytest.mark.asyncio
async def test_get_on_a_fresh_host_returns_empty():
    async with _client() as client:
        res = await client.get("/api/ui-prefs")
        assert res.status == 200
        assert await res.json() == {"prefs": {}}


@pytest.mark.asyncio
async def test_put_then_get_round_trips():
    async with _client() as client:
        res = await client.put("/api/ui-prefs", json={"prefs": {"mc-chat-config": '{"a":1}'}})
        assert res.status == 200
        assert await res.json() == {"prefs": {"mc-chat-config": '{"a":1}'}}

        res = await client.get("/api/ui-prefs")
        assert await res.json() == {"prefs": {"mc-chat-config": '{"a":1}'}}


@pytest.mark.asyncio
async def test_put_merges_and_null_deletes():
    async with _client() as client:
        await client.put("/api/ui-prefs", json={"prefs": {"a": "1", "b": "2"}})
        res = await client.put("/api/ui-prefs", json={"prefs": {"b": None, "c": "3"}})
        assert await res.json() == {"prefs": {"a": "1", "c": "3"}}


@pytest.mark.parametrize(
    "body",
    [
        {},  # no 'prefs'
        {"prefs": []},  # wrong type
        {"prefs": {"mc-zoom": 1}},  # non-string value
        {"prefs": {"kiro_crew_token": "leak"}},  # credential-shaped key
    ],
)
@pytest.mark.asyncio
async def test_bad_bodies_are_400(body):
    async with _client() as client:
        res = await client.put("/api/ui-prefs", json=body)
        assert res.status == 400


@pytest.mark.asyncio
async def test_non_json_body_is_400():
    async with _client() as client:
        res = await client.put(
            "/api/ui-prefs", data=b"not json", headers={"Content-Type": "application/json"}
        )
        assert res.status == 400


@pytest.mark.asyncio
async def test_an_unwritable_home_is_503_not_500(monkeypatch):
    def _boom(_patch):
        raise OSError("read-only file system")

    monkeypatch.setattr(ui_prefs_handler, "merge_ui_prefs", _boom)
    async with _client() as client:
        res = await client.put("/api/ui-prefs", json={"prefs": {"a": "1"}})
        assert res.status == 503


@pytest.mark.asyncio
async def test_a_non_owner_cannot_read(monkeypatch):
    """These values are the owner's and some name real host paths (file-explorer
    state, cloud launch defaults), so the READ is gated too, not just the write."""
    ui_prefs_store.merge_ui_prefs({"kc:file-explorer:state:v2": '{"cwd":"/home/user"}'})
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: False,
    )
    async with _client() as client:
        res = await client.get("/api/ui-prefs")
        assert res.status in (401, 403)
        assert "/home/user" not in await res.text()


@pytest.mark.asyncio
async def test_a_non_owner_cannot_write(monkeypatch):
    """A shared dashboard's viewer must not overwrite the owner's settings."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: False,
    )
    async with _client() as client:
        res = await client.put("/api/ui-prefs", json={"prefs": {"mc-zoom": "9"}})
        assert res.status in (401, 403)
    # And nothing landed.
    assert ui_prefs_store.load_ui_prefs() == {}


@pytest.mark.asyncio
async def test_the_gate_runs_before_body_validation(monkeypatch):
    """A non-owner must not be able to probe the validator's error messages."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: False,
    )
    async with _client() as client:
        res = await client.put("/api/ui-prefs", json={"nonsense": True})
        assert res.status in (401, 403)


@pytest.mark.asyncio
async def test_an_unknown_charset_is_400_not_500():
    """A bare `await request.json()` lets LookupError escape as a 500."""
    async with _client() as client:
        res = await client.put(
            "/api/ui-prefs",
            data=b'{"prefs":{}}',
            headers={"Content-Type": "application/json; charset=nope-not-a-codec"},
        )
        assert res.status == 400


@pytest.mark.asyncio
async def test_deeply_nested_json_is_400_not_500():
    """RecursionError from CPython's parser must not reach the client as a 500."""
    deep = "[" * 5000 + "]" * 5000
    async with _client() as client:
        res = await client.put(
            "/api/ui-prefs",
            data=('{"prefs":' + deep + "}").encode(),
            headers={"Content-Type": "application/json"},
        )
        assert res.status == 400


@pytest.mark.asyncio
async def test_a_max_size_value_is_accepted_not_413():
    """The request cap must cover a value the STORE allows.

    The dashboard's shared default is exactly MAX_VALUE_BYTES, so the JSON
    envelope pushed a legal value past it: the PUT 413'd, the per-key retry 413'd
    too, and the preference was silently never backed up."""
    big = "x" * ui_prefs_store.MAX_VALUE_BYTES
    async with _client() as client:
        res = await client.put("/api/ui-prefs", json={"prefs": {"kc:file-explorer:state:v2": big}})
        assert res.status == 200
    assert ui_prefs_store.load_ui_prefs()["kc:file-explorer:state:v2"] == big


@pytest.mark.asyncio
async def test_a_body_past_the_endpoint_cap_is_refused():
    """The cap still exists: it is sized to the store, not removed."""
    async with _client() as client:
        res = await client.put(
            "/api/ui-prefs",
            data=b'{"prefs":{"k":"' + b"x" * (ui_prefs_store.MAX_REQUEST_BYTES + 1024) + b'"}}',
            headers={"Content-Type": "application/json"},
        )
        assert res.status == 413


@pytest.mark.asyncio
async def test_concurrent_puts_do_not_lose_a_patch():
    """Two tabs flushing at once must both land: merge is read-modify-write."""
    async with _client() as client:
        await asyncio.gather(
            client.put("/api/ui-prefs", json={"prefs": {"from-tab-a": "1"}}),
            client.put("/api/ui-prefs", json={"prefs": {"from-tab-b": "2"}}),
        )
        res = await client.get("/api/ui-prefs")
        assert await res.json() == {"prefs": {"from-tab-a": "1", "from-tab-b": "2"}}
