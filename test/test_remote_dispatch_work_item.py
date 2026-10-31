"""Remote dispatch wave 2: a peer slot born bound to a hub's work item.

Pins the wave-2 exit criteria of ``docs/request-for-change/rfc-conductor-remote-dispatch.md``
(Migration plan): with the Feature Preview on, ``POST /api/chat/slots`` carrying a
``work_item`` creates the slot already bound to a ``hub:`` mailbox item; that slot's
own ``work_brief`` returns the item and its ``work_report`` updates the copy;
``GET /api/chat/slots/{slot}/work-item`` returns the worker's fields. A slot created
without ``work_item`` answers 404 on both routes. With the preview off nothing changes:
the field is ignored, nothing is echoed, and both routes answer 404.

Nearby inputs: a duplicate ``item_id``, a closed slot, oversized fields (a title past
its cap, an acceptance whose first report could not fit one crew-log line), a PATCH
naming a worker-owned field, a non-owner caller, a named create, an unknown field.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew import remote_dispatch_mailbox as mailbox
from kiro_crew import work_ledger as wl
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers, remote_dispatch_headroom
from kiro_crew.dashboard.handlers import remote_work_item
from kiro_crew.dashboard.handlers import work_ledger as routes

HUB_CONDUCTOR = "chat-hub-conductor"

#: Upper bound on every wait in the cancellation tests, so a broken path fails
#: here instead of hanging the worker.
_BOUND_SECS = 30


def _work_item(item_id: str = "it_0000abcd", **overrides: Any) -> dict[str, Any]:
    return {
        "conductor_key": HUB_CONDUCTOR,
        "item_id": item_id,
        "title": "port the gate",
        "acceptance": {"kind": "pr_checks", "pr": "TBD", "repo": "o/r"},
        "decision": "start from main",
        **overrides,
    }


@pytest.fixture
def preview(monkeypatch):
    """The Feature Preview switch, on unless a test turns it off."""
    flag = {"on": True}
    monkeypatch.setattr(
        remote_dispatch_headroom,
        "remote_dispatch_settings",
        lambda: remote_dispatch_headroom.RemoteDispatchSettings(
            enabled=flag["on"], memory_gb=0.0, disk_gb=0.0
        ),
    )
    # The admission gate is wave 3's and has its own suite; here this crew has room.
    monkeypatch.setattr(remote_dispatch_headroom, "_short_resource", lambda *a, **k: "")
    return flag


@pytest.fixture
def state(tmp_path, monkeypatch):
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    st = _make_state(tmp_path)
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **k: None)
    return st


def _app(state: Any, *, owner: bool = True) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_get(
        "/api/chat/slots/{slot}/work-item", remote_work_item.api_chat_slot_work_item_get
    )
    app.router.add_patch(
        "/api/chat/slots/{slot}/work-item", remote_work_item.api_chat_slot_work_item_patch
    )
    if owner:
        return as_owner(app)

    @web.middleware
    async def _app_token(request, handler):
        request["user"] = "local-app"
        request["app"] = "notes"
        return await handler(request)

    app.middlewares.append(_app_token)
    return app


async def _call(state: Any, method: str, path: str, body: Any = None, *, owner: bool = True):
    async with TestClient(TestServer(_app(state, owner=owner))) as client:
        resp = await client.request(method, path, json=body)
        return resp.status, await resp.json()


# ── the worker's own tools, as the peer serves them ──────────────────────


@pytest.fixture
def worker_routes(monkeypatch):
    """Open the worker routes' session gates, as ``test_work_ledger_tools`` does."""

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    recorded: list[tuple[str, dict]] = []

    def _on_work_recorded(unit: str, data: dict) -> bool:
        recorded.append((unit, data))
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _on_work_recorded)
    return recorded


def _worker_req(method: str, path: str, sk: str, body: Any = ...) -> web.Request:
    app = web.Application()
    st = MagicMock()
    st.get_slot = MagicMock(return_value=None)
    app["state"] = st
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _brief(sk: str) -> tuple[int, dict]:
    resp = await routes.api_work_brief(_worker_req("GET", "/api/work-ledger/brief", sk))
    return resp.status, json.loads(resp.text)


async def _report(sk: str, body: dict) -> tuple[int, dict]:
    resp = await routes.api_work_report(_worker_req("POST", "/api/work-ledger/report", sk, body))
    return resp.status, json.loads(resp.text)


# ── preview on: the exit criteria ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bound_create_echoes_the_item_and_the_worker_reads_its_brief(
    state, preview, worker_routes
):
    status, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 200, created
    assert created["work_item_id"] == "it_0000abcd"
    slot = created["key"]

    assert wl.read_binding(slot) == (f"hub:{HUB_CONDUCTOR}", "it_0000abcd")
    status, body = await _brief(slot)
    assert status == 200, body
    brief = body["brief"]
    assert brief["item_id"] == "it_0000abcd"
    assert brief["title"] == "port the gate"
    assert brief["acceptance"] == {"kind": "pr_checks", "pr": "TBD", "repo": "o/r"}
    assert brief["decision"] == "start from main"


@pytest.mark.asyncio
async def test_a_report_updates_the_copy_and_the_get_returns_it(state, preview, worker_routes):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]

    status, body = await _report(
        slot,
        {"status": "done", "summary": "pushed", "artifacts": {"branch": "feat/x"}, "pr": 7},
    )
    assert status == 200, body
    # The item was never recorded whole, so the first report carries all of it.
    assert any(data.get("baseline") and data.get("title") for _, data in worker_routes)

    status, body = await _call(state, "GET", f"/api/chat/slots/{slot}/work-item")
    assert status == 200, body
    item = body["work_item"]
    assert item["item_id"] == "it_0000abcd"
    assert item["status"] == "done"
    assert item["summary"] == "pushed"
    assert item["artifacts"] == {"branch": "feat/x"}
    assert item["pr"] == 7
    assert item["last_report_at"]
    # Only the worker-owned fields, never the hub's own bar or decision.
    assert "acceptance" not in item and "decision" not in item
    assert [e["kind"] for e in body["events"]] == ["create", "report"]


@pytest.mark.asyncio
async def test_a_patch_writes_the_decision_and_nothing_the_worker_owns(
    state, preview, worker_routes
):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]
    await _report(slot, {"status": "question", "summary": "which base?"})
    board = f"hub:{HUB_CONDUCTOR}"
    before = (wl.read_work_item(board, "it_0000abcd") or wl.WorkItem()).to_dict()

    status, body = await _call(
        state, "PATCH", f"/api/chat/slots/{slot}/work-item", {"decision": "use release/1"}
    )
    assert status == 200, body
    after = (wl.read_work_item(board, "it_0000abcd") or wl.WorkItem()).to_dict()
    changed = {k for k in after if before.get(k) != after.get(k)}
    assert changed <= {"decision", "decided_at", "recorded_at"}
    assert after["decision"] == "use release/1"
    assert after["status"] == "question" and after["summary"] == "which base?"
    _, brief = await _brief(slot)
    assert brief["brief"]["decision"] == "use release/1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"decision": "x", "status": "done"},
        {"summary": "forged"},
        {"artifacts": {"pr": "1"}},
        {"pr": 1},
        {"last_report_at": "2026-01-01T00:00:00+00:00"},
        {},
    ],
)
async def test_a_patch_naming_a_worker_field_is_refused_and_changes_nothing(state, preview, patch):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]
    board = f"hub:{HUB_CONDUCTOR}"
    before = wl.item_path(board, "it_0000abcd").read_bytes()

    status, body = await _call(state, "PATCH", f"/api/chat/slots/{slot}/work-item", patch)
    assert status == 400, body
    assert wl.item_path(board, "it_0000abcd").read_bytes() == before


@pytest.mark.asyncio
async def test_a_slot_created_without_a_work_item_answers_404_on_both_routes(state, preview):
    status, created = await _call(state, "POST", "/api/chat/slots", {})
    assert status == 200
    assert "work_item_id" not in created
    slot = created["key"]
    assert (await _call(state, "GET", f"/api/chat/slots/{slot}/work-item"))[0] == 404
    patched = await _call(state, "PATCH", f"/api/chat/slots/{slot}/work-item", {"decision": "x"})
    assert patched[0] == 404


@pytest.mark.asyncio
async def test_a_locally_bound_worker_is_not_reachable_through_the_routes(state, preview):
    """A worker a LOCAL conductor bound is not a hub's: the routes do not expose it."""
    _, created = await _call(state, "POST", "/api/chat/slots", {})
    slot = created["key"]
    wl.ensure_conductor("chat-local-conductor")
    item = wl.apply_conductor_action(
        "chat-local-conductor", "create", title="t", acceptance={"kind": "human_approval"}
    )["item"]
    wl.apply_conductor_action(
        "chat-local-conductor", "bind", item_id=item.item_id, worker_session_key=slot
    )
    assert (await _call(state, "GET", f"/api/chat/slots/{slot}/work-item"))[0] == 404
    patched = await _call(state, "PATCH", f"/api/chat/slots/{slot}/work-item", {"decision": "x"})
    assert patched[0] == 404
    assert (wl.read_work_item("chat-local-conductor", item.item_id) or wl.WorkItem()).decision == ""


@pytest.mark.asyncio
async def test_a_closed_slot_still_reads_its_own_item_but_takes_no_decision(state, preview):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]
    state._slots.pop(slot)
    # The worker's last reports stay readable for the hub after its slot is gone.
    status, body = await _call(state, "GET", f"/api/chat/slots/{slot}/work-item")
    assert status == 200 and body["work_item"]["item_id"] == "it_0000abcd"
    patched = await _call(state, "PATCH", f"/api/chat/slots/{slot}/work-item", {"decision": "x"})
    assert patched[0] == 404
    # A closed slot the item does not name answers nothing.
    assert (await _call(state, "GET", "/api/chat/slots/chat-404/work-item"))[0] == 404


@pytest.mark.asyncio
async def test_a_decision_on_a_closed_item_is_refused(state, preview):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    board = f"hub:{HUB_CONDUCTOR}"
    wl.apply_conductor_action(board, "close", item_id="it_0000abcd", state="accepted")
    status, body = await _call(
        state, "PATCH", f"/api/chat/slots/{created['key']}/work-item", {"decision": "x"}
    )
    assert status == 409 and body["code"] == wl.CODE_ITEM_CLOSED


@pytest.mark.asyncio
async def test_a_duplicate_item_id_is_refused_and_leaves_no_second_slot(state, preview):
    status, first = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 200
    slots_before = set(state._slots)

    status, body = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 409, body
    assert body["code"] == wl.CODE_DUPLICATE_ITEM
    assert set(state._slots) == slots_before
    # The first worker keeps its binding.
    assert wl.read_binding(first["key"]) == (f"hub:{HUB_CONDUCTOR}", "it_0000abcd")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_item", "code"),
    [
        (_work_item(title="t" * (wl.MAX_TITLE_CHARS + 1)), wl.CODE_FIELD_TOO_LONG),
        (_work_item(decision="d" * (wl.MAX_DECISION_CHARS + 1)), wl.CODE_FIELD_TOO_LONG),
        # Fits the item record, but not the one crew-log line the worker's first
        # report must carry it in (MAX_ENTRY_BYTES).
        (_work_item(acceptance={"kind": "x", "blob": "a" * 55_000}), "work_item_too_large"),
        (_work_item(item_id="not-an-id"), wl.CODE_INVALID_VALUE),
        (_work_item(conductor_key="a/b"), wl.CODE_INVALID_VALUE),
        (_work_item(state="accepted"), wl.CODE_INVALID_VALUE),
        ({k: v for k, v in _work_item().items() if k != "acceptance"}, wl.CODE_INVALID_VALUE),
        ("not an object", wl.CODE_INVALID_VALUE),
    ],
)
async def test_an_invalid_or_oversized_work_item_builds_nothing(state, preview, work_item, code):
    before = set(state._slots)
    status, body = await _call(state, "POST", "/api/chat/slots", {"work_item": work_item})
    assert status == 400, body
    assert body["code"] == code
    assert set(state._slots) == before
    assert not wl.items_dir(f"hub:{HUB_CONDUCTOR}").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [0, False, 0.0, [], {}, "0"])
async def test_a_falsy_name_cannot_bind_an_open_conversation(state, preview, name):
    """A name of 0 or false would coerce to a key naming an open slot."""
    open_slot = state.get_or_create_slot("0")
    before = set(state._slots)
    status, body = await _call(
        state, "POST", "/api/chat/slots", {"name": name, "work_item": _work_item()}
    )
    assert status == 400 and body["code"] == "work_item_needs_new_slot"
    assert set(state._slots) == before
    assert wl.read_binding(open_slot.key) is None
    assert not wl.conductor_dir(f"hub:{HUB_CONDUCTOR}").exists()


@pytest.mark.asyncio
async def test_a_named_create_with_a_work_item_is_refused(state, preview):
    status, body = await _call(
        state, "POST", "/api/chat/slots", {"name": "chat-9", "work_item": _work_item()}
    )
    assert status == 400 and body["code"] == "work_item_needs_new_slot"
    assert "chat-9" not in state._slots


@pytest.mark.asyncio
async def test_a_non_owner_caller_cannot_bind_or_read(state, preview):
    before = set(state._slots)
    status, _ = await _call(
        state, "POST", "/api/chat/slots", {"work_item": _work_item()}, owner=False
    )
    assert status == 403
    assert set(state._slots) == before

    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]
    assert (await _call(state, "GET", f"/api/chat/slots/{slot}/work-item", owner=False))[0] == 403
    patched = await _call(
        state, "PATCH", f"/api/chat/slots/{slot}/work-item", {"decision": "x"}, owner=False
    )
    assert patched[0] == 403


@pytest.mark.asyncio
async def test_a_failed_mailbox_write_retracts_the_slot(state, preview, monkeypatch):
    def _fail(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mailbox, "open_mailbox", _fail)
    before = set(state._slots)
    status, body = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 503, body
    assert set(state._slots) == before


# ── preview off: nothing changes ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_off_ignores_the_work_item_and_echoes_nothing(state, preview):
    preview["on"] = False
    status, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 200, created
    assert "work_item_id" not in created
    assert wl.read_binding(created["key"]) is None
    assert not wl.conductor_dir(f"hub:{HUB_CONDUCTOR}").exists()


@pytest.mark.asyncio
async def test_preview_off_ignores_even_an_invalid_work_item(state, preview):
    preview["on"] = False
    status, created = await _call(state, "POST", "/api/chat/slots", {"work_item": "junk"})
    assert status == 200 and "work_item_id" not in created


@pytest.mark.asyncio
async def test_preview_off_answers_404_on_both_routes_even_for_a_bound_slot(state, preview):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]
    preview["on"] = False
    assert (await _call(state, "GET", f"/api/chat/slots/{slot}/work-item"))[0] == 404
    patched = await _call(state, "PATCH", f"/api/chat/slots/{slot}/work-item", {"decision": "x"})
    assert patched[0] == 404
    assert (await _call(state, "GET", "/api/chat/slots/chat-none/work-item"))[0] == 404


@pytest.mark.asyncio
async def test_preview_off_create_answer_matches_a_plain_create(state, preview):
    """Same row keys as a create that never named a work_item."""
    preview["on"] = False
    _, plain = await _call(state, "POST", "/api/chat/slots", {})
    _, ignored = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert set(ignored) == set(plain)


# ── refusals that keep a worker usable, and the boot path ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_a_restricted_memory_mode_cannot_be_a_bound_worker(state, preview, memory_mode):
    """A restricted session is refused the worker tools, so it is refused here."""
    before = set(state._slots)
    status, body = await _call(
        state,
        "POST",
        "/api/chat/slots",
        {"work_item": _work_item(), "memory_mode": memory_mode},
    )
    assert status == 400 and body["field"] == "memory_mode"
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_decision_that_would_overflow_every_later_report_is_refused(state, preview):
    """A large acceptance plus a long astral decision would refuse every report."""
    big = _work_item(acceptance={"kind": "x", "blob": "a" * 42_000}, decision="")
    status, created = await _call(state, "POST", "/api/chat/slots", {"work_item": big})
    assert status == 200, created
    board = f"hub:{HUB_CONDUCTOR}"
    before = wl.item_path(board, "it_0000abcd").read_bytes()

    status, body = await _call(
        state,
        "PATCH",
        f"/api/chat/slots/{created['key']}/work-item",
        {"decision": "\U0001f600" * wl.MAX_DECISION_CHARS},
    )
    assert status == 400 and body["code"] == mailbox.CODE_WORK_ITEM_TOO_LARGE, body
    assert wl.item_path(board, "it_0000abcd").read_bytes() == before


@pytest.mark.asyncio
async def test_a_refused_duplicate_claims_no_folder_and_saves_no_line(state, preview):
    """The refusal comes before the folder claim and the metadata save."""
    _, first = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    saved = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(chat_handlers, "save_slot_off_loop", saved)
        status, _ = await _call(
            state,
            "POST",
            "/api/chat/slots",
            {"work_item": _work_item(), "title": "retry"},
        )
    assert status == 409
    saved.assert_not_called()


@pytest.mark.asyncio
async def test_the_lock_time_duplicate_check_still_refuses_and_retracts(
    state, preview, monkeypatch
):
    """A race past the early check meets the bind's own check under the lock."""
    await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    monkeypatch.setattr(remote_work_item.mailbox, "item_exists", lambda request: False)
    monkeypatch.setattr(
        mailbox,
        "open_mailbox",
        lambda request, key: (_ for _ in ()).throw(
            wl.WorkLedgerError("dup", code=wl.CODE_DUPLICATE_ITEM, field="item_id")
        ),
    )
    before = set(state._slots)
    status, body = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 409 and body["code"] == wl.CODE_DUPLICATE_ITEM
    assert set(state._slots) == before


def test_the_route_table_and_the_create_do_not_load_remote_dispatch_at_import():
    """The boot path imports neither the handler nor the mailbox."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import kiro_crew.dashboard.routes.chat\n"
        "import kiro_crew.dashboard.chat_handlers\n"
        "loaded = [m for m in ('kiro_crew.dashboard.handlers.remote_work_item',"
        " 'kiro_crew.remote_dispatch_mailbox') if m in sys.modules]\n"
        "print(loaded)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=120,
    )
    assert out.stdout.strip().splitlines()[-1] == "[]"


@pytest.mark.asyncio
async def test_the_get_reply_redacts_a_planted_credential_and_stays_json(
    state, preview, worker_routes
):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]
    planted = "AKIA" + "ABCDEFGHIJKLMNOP"
    await _report(slot, {"status": "progress", "summary": f"key {planted}"})
    status, body = await _call(state, "GET", f"/api/chat/slots/{slot}/work-item")
    assert status == 200
    assert planted not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_create_that_raises_after_the_bind_leaves_no_binding(state, preview, monkeypatch):
    """A newborn retracted on the way out takes its mailbox item and binding too."""

    async def _boom(*a: Any, **k: Any) -> bool:
        raise RuntimeError("save failed")

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _boom)
    before = set(state._slots)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots", json={"work_item": _work_item(), "title": "worker"}
        )
        assert resp.status == 500
    assert set(state._slots) == before
    board = f"hub:{HUB_CONDUCTOR}"
    assert not wl.item_path(board, "it_0000abcd").exists()
    assert list(wl.bindings_dir().glob("*.json")) == []


@pytest.mark.asyncio
async def test_a_dirty_mailbox_is_not_read(state, preview):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    wl.mark_cache_dirty(f"hub:{HUB_CONDUCTOR}", "test")
    status, body = await _call(state, "GET", f"/api/chat/slots/{created['key']}/work-item")
    assert status == 409 and body["code"] == wl.CODE_CACHE_DIRTY


@pytest.mark.asyncio
async def test_the_get_reply_redacts_artifact_keys_too(state, preview, worker_routes):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = created["key"]
    planted = "AKIA" + "ABCDEFGHIJKLMNOP"
    await _report(slot, {"status": "progress", "summary": "s", "artifacts": {planted: "v"}})
    status, body = await _call(state, "GET", f"/api/chat/slots/{slot}/work-item")
    assert status == 200
    assert planted not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_cancelled_bind_finishes_its_write_before_the_cancellation_lands(monkeypatch):
    """The unwind then sees the binding it must undo, and the discard removes it."""
    import asyncio

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def _gated(fn: Any, *args: Any, **kwargs: Any) -> Any:
        # The write is held on the loop, not on a pool thread, until the
        # cancellation has been delivered. Every other off-loop read runs at once.
        if fn is not mailbox.open_mailbox:
            return fn(*args, **kwargs)
        entered.set()
        await asyncio.wait_for(gate.wait(), _BOUND_SECS)
        return fn(*args, **kwargs)

    parsed = mailbox.parse_work_item(_work_item())
    request = make_mocked_request("POST", "/api/chat/slots")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote_work_item.asyncio, "to_thread", _gated)
        task = asyncio.ensure_future(remote_work_item.bind_work_item(request, "chat-w1", parsed))
        try:
            await asyncio.wait_for(entered.wait(), _BOUND_SECS)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _BOUND_SECS)
    assert wl.read_binding("chat-w1") == (parsed.board, parsed.item_id)

    await remote_work_item.discard_work_item("chat-w1", parsed)
    assert wl.read_binding("chat-w1") is None
    assert not wl.item_path(parsed.board, parsed.item_id).exists()


@pytest.mark.asyncio
async def test_a_discard_for_a_slot_that_holds_no_binding_leaves_the_item():
    """A refused duplicate's unwind must not remove the first worker's item."""
    parsed = mailbox.parse_work_item(_work_item())
    mailbox.open_mailbox(parsed, "chat-first")
    await remote_work_item.discard_work_item("chat-second", parsed)
    assert wl.read_binding("chat-first") == (parsed.board, parsed.item_id)
    assert wl.item_path(parsed.board, parsed.item_id).exists()


@pytest.mark.asyncio
async def test_a_cancelled_discard_still_removes_the_binding_and_runs_the_undo():
    """A second shutdown cancellation cannot land between the discard and the undo."""
    import asyncio

    parsed = mailbox.parse_work_item(_work_item())
    mailbox.open_mailbox(parsed, "chat-w2")
    entered = asyncio.Event()
    gate = asyncio.Event()
    undone: list[bool] = []

    async def _gated(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn is not mailbox.discard_mailbox:
            return fn(*args, **kwargs)
        entered.set()
        await asyncio.wait_for(gate.wait(), _BOUND_SECS)
        return fn(*args, **kwargs)

    async def _undo() -> None:
        undone.append(True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote_work_item.asyncio, "to_thread", _gated)
        task = asyncio.ensure_future(
            remote_work_item.discard_work_item("chat-w2", parsed, then=_undo)
        )
        try:
            await asyncio.wait_for(entered.wait(), _BOUND_SECS)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _BOUND_SECS)
    assert wl.read_binding("chat-w2") is None
    assert not wl.item_path(parsed.board, parsed.item_id).exists()
    assert undone == [True]


# ── the store's own create path, dirty boards, restarts and closes ────────


@pytest.mark.asyncio
async def test_a_mailbox_board_keeps_the_stores_open_item_cap(state, preview):
    """Hub items go through the store's create path: the cap and the count apply."""
    board = f"hub:{HUB_CONDUCTOR}"
    wl.ensure_conductor(board)
    for n in range(wl.MAX_ITEMS_PER_CONDUCTOR):
        mailbox.open_mailbox(
            mailbox.parse_work_item(_work_item(item_id=f"it_{n:08x}")), f"chat-cap-{n}"
        )
    header = wl.read_conductor(board)
    assert header is not None and header.created_total == wl.MAX_ITEMS_PER_CONDUCTOR
    before = set(state._slots)
    status, body = await _call(
        state, "POST", "/api/chat/slots", {"work_item": _work_item(item_id="it_ffff0000")}
    )
    assert status == 409 and body["code"] == wl.CODE_ITEM_CAP_EXCEEDED
    assert set(state._slots) == before
    assert wl.read_binding("chat-cap-0") == (board, "it_00000000")


def test_create_bound_item_refuses_a_duplicate_and_leaves_the_header():
    board = "hub:chat-store"
    wl.ensure_conductor(board)
    wl.create_bound_item(
        board, "it_0000beef", title="t", acceptance={"k": "v"}, decision="", worker_session_key="w1"
    )
    header = wl.conductor_dir(board) / "conductor.json"
    before = header.read_bytes()
    with pytest.raises(wl.WorkLedgerError) as exc:
        wl.create_bound_item(
            board,
            "it_0000beef",
            title="t",
            acceptance={"k": "v"},
            decision="",
            worker_session_key="w2",
        )
    assert exc.value.code == wl.CODE_DUPLICATE_ITEM
    assert header.read_bytes() == before
    assert wl.read_binding("w2") is None


def test_create_bound_item_puts_the_header_and_binding_back_when_the_item_write_fails(
    monkeypatch,
):
    board = "hub:chat-store"
    wl.ensure_conductor(board)
    header = wl.conductor_dir(board) / "conductor.json"
    before = header.read_bytes()

    def _fail(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(wl, "_commit_item_locked", _fail)
    with pytest.raises(OSError):
        wl.create_bound_item(
            board, "it_0000cafe", title="t", acceptance={}, decision="", worker_session_key="w3"
        )
    assert header.read_bytes() == before
    assert wl.read_binding("w3") is None


@pytest.mark.asyncio
async def test_a_create_on_a_dirty_board_is_refused_and_builds_nothing(state, preview):
    board = f"hub:{HUB_CONDUCTOR}"
    wl.ensure_conductor(board)
    wl.mark_cache_dirty(board, "test")
    before = set(state._slots)
    status, body = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 409 and body["code"] == wl.CODE_CACHE_DIRTY
    assert set(state._slots) == before
    assert not wl.item_path(board, "it_0000abcd").exists()


@pytest.mark.asyncio
async def test_a_bound_worker_has_a_birth_line_before_its_first_message(state, preview):
    """A restart before the first message brings the worker back."""
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    slot = state._slots[created["key"]]
    assert state.conversation_log.get_metadata(chat_handlers.slot_history_key(slot))


@pytest.mark.asyncio
async def test_a_failed_birth_line_retracts_the_worker_and_its_item(state, preview, monkeypatch):
    async def _unsaved(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(chat_handlers, "_persist_birth_line", _unsaved)
    before = set(state._slots)
    status, body = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 503 and body["code"] == "worker_unpersisted"
    assert set(state._slots) == before
    assert not wl.item_path(f"hub:{HUB_CONDUCTOR}", "it_0000abcd").exists()
    assert list(wl.bindings_dir().glob("*.json")) == []


def _reported() -> None:
    """The worker's first report on the mailbox item."""
    wl.apply_worker_report(f"hub:{HUB_CONDUCTOR}", "it_0000abcd", status="progress", summary="x")


@pytest.mark.asyncio
async def test_closing_the_slot_closes_its_hub_item(state, preview):
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    _reported()  # a copy the worker reported on is closed, not discarded
    slot = created["key"]
    await chat_handlers.close_slot(state, state._slots[slot], slot)
    item = wl.read_work_item(f"hub:{HUB_CONDUCTOR}", "it_0000abcd")
    assert item is not None and item.state == "abandoned" and item.closed_at


@pytest.mark.asyncio
async def test_closing_a_plain_slot_touches_no_ledger(state, preview):
    _, created = await _call(state, "POST", "/api/chat/slots", {})
    slot = created["key"]
    await chat_handlers.close_slot(state, state._slots[slot], slot)
    assert not wl.conductor_dir(f"hub:{HUB_CONDUCTOR}").exists()


@pytest.mark.asyncio
async def test_closing_a_slot_leaves_a_dirty_board_alone(state, preview):
    board = f"hub:{HUB_CONDUCTOR}"
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    wl.mark_cache_dirty(board, "test")
    before = wl.item_path(board, "it_0000abcd").read_bytes()
    slot = created["key"]
    await chat_handlers.close_slot(state, state._slots[slot], slot)
    assert wl.item_path(board, "it_0000abcd").read_bytes() == before


@pytest.mark.asyncio
async def test_a_cancelled_close_still_closes_the_item():
    import asyncio

    parsed = mailbox.parse_work_item(_work_item())
    wl.ensure_conductor(parsed.board)
    mailbox.open_mailbox(parsed, "chat-w3")
    wl.apply_worker_report(parsed.board, parsed.item_id, status="progress", summary="x")
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def _gated(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn is not mailbox.close_mailbox:
            return fn(*args, **kwargs)
        entered.set()
        await asyncio.wait_for(gate.wait(), _BOUND_SECS)
        return fn(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote_work_item.asyncio, "to_thread", _gated)
        task = asyncio.ensure_future(remote_work_item.close_work_item_for_slot("chat-w3"))
        try:
            await asyncio.wait_for(entered.wait(), _BOUND_SECS)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _BOUND_SECS)
    item = wl.read_work_item(parsed.board, parsed.item_id)
    assert item is not None and item.state == "abandoned"


@pytest.mark.asyncio
async def test_deleting_a_bound_workers_transcript_closes_its_hub_item():
    """The transcript-delete teardown is the slot's other close path."""
    from remove_slot_for_history_key_helpers import (
        _make_slot,
        _make_state,
        _remove_slot_for_history_key,
    )

    parsed = mailbox.parse_work_item(_work_item())
    wl.ensure_conductor(parsed.board)
    mailbox.open_mailbox(parsed, "chat-1-100")
    wl.apply_worker_report(parsed.board, parsed.item_id, status="progress", summary="x")
    state = _make_state({"chat-1-100": _make_slot("chat-1-100")})
    await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
    assert "chat-1-100" not in state._slots
    item = wl.read_work_item(parsed.board, parsed.item_id)
    assert item is not None and item.state == "abandoned"


@pytest.mark.asyncio
async def test_deleting_an_archived_workers_transcript_closes_its_hub_item():
    """No live slot: the transcript's own key still names the bound item."""
    from remove_slot_for_history_key_helpers import _make_state, _remove_slot_for_history_key

    parsed = mailbox.parse_work_item(_work_item())
    wl.ensure_conductor(parsed.board)
    mailbox.open_mailbox(parsed, "chat-1-200")
    wl.apply_worker_report(parsed.board, parsed.item_id, status="progress", summary="x")
    await _remove_slot_for_history_key(_make_state({}), "dashboard_chat-1-200")
    item = wl.read_work_item(parsed.board, parsed.item_id)
    assert item is not None and item.state == "abandoned"


@pytest.mark.asyncio
async def test_bulk_archival_closes_an_idle_workers_hub_item(state, preview, monkeypatch):
    """The owner's "Clean up sessions" closes like a single close."""
    _, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    _reported()  # a copy the worker reported on is closed, not discarded
    slot = created["key"]
    monkeypatch.setattr(
        chat_handlers,
        "select_idle_slot_keys",
        lambda state, **kwargs: ([slot], False),
    )
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/cleanup", chat_handlers.api_chat_slots_cleanup)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await client.post("/api/chat/slots/cleanup", json={})
        body = await resp.json()
    assert body["keys"] == [slot], body
    item = wl.read_work_item(f"hub:{HUB_CONDUCTOR}", "it_0000abcd")
    assert item is not None and item.state == "abandoned"


# ── composed with the headroom gate ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_busy_crew_binds_nothing(state, preview, monkeypatch):
    """The admission gate refuses first, so no slot, item or binding exists."""
    monkeypatch.setattr(remote_dispatch_headroom, "_short_resource", lambda *a, **k: "disk")
    before = set(state._slots)
    status, body = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 503 and body["code"] == "crew_busy"
    assert set(state._slots) == before
    assert not wl.conductor_dir(f"hub:{HUB_CONDUCTOR}").exists()


@pytest.mark.asyncio
async def test_an_admitted_create_carries_both_headroom_and_work_item_id(state, preview):
    status, created = await _call(state, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 200, created
    assert created["work_item_id"] == "it_0000abcd"
    assert "headroom" in created


@pytest.mark.asyncio
async def test_the_bind_refuses_a_slot_this_create_did_not_mint(state, preview, monkeypatch):
    """Past the body check, the bind itself still binds only a newborn."""
    real = remote_work_item.work_item_for_create

    async def _named_ok(request: Any, body: dict) -> Any:
        return await real(request, {k: v for k, v in body.items() if k != "name"})

    monkeypatch.setattr(remote_work_item, "work_item_for_create", _named_ok)
    open_slot = state.get_or_create_slot("chat-open")
    status, body = await _call(
        state, "POST", "/api/chat/slots", {"name": "chat-open", "work_item": _work_item()}
    )
    assert status == 400 and body["code"] == "work_item_needs_new_slot"
    assert wl.read_binding(open_slot.key) is None
