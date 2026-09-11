"""Promote an ephemeral session to persistent — POST /api/chat/slots/{slot}/promote.

"Keep this chat" (the dashboard's promotion action) forks the WHOLE transcript
into a brand-new persistent slot and leaves the ephemeral original untouched,
rather than rewriting the source transcript's ``memory_mode`` header in place
— see ``docs/system-specs/modules/history.md`` § "Promoting an ephemeral
session" and ``chat_fork.api_chat_slot_promote``'s docstring for why.

This module tests only the ADDITIVE behavior on top of fork (guard exception,
title, transcript note, memory_mode, SEL audit, no MCP/CLI surface). The
snapshot-race, mid-rotation and rollback machinery promotion reuses is already
covered by ``test_chat_fork_error_codes.py`` and ``test_fork_reconciles_after_
flush.py`` and is not re-tested here.
"""

from __future__ import annotations

import inspect

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.history import is_incognito_transcript
from kiro_crew.sel import sel


@pytest.fixture(autouse=True)
def _private_sel_root_per_test(sel_private_root):
    """Give this module its own SEL root so the audit-record assertions below
    read only records this module's own requests wrote (see ``sel_private_
    root``'s docstring in the rootdir ``conftest.py``)."""
    yield


def _seed(tmp_path, name: str, memory_mode: str | None = None):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name, memory_mode=memory_mode)
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return state


async def _promote(state, slot_name: str, payload: dict | None = None) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{slot_name}/promote", json=payload if payload is not None else {}
        )
        return resp.status, await resp.json()


async def _promote_as_app(
    state, slot_name: str, app_name: str, payload: dict | None = None
) -> tuple[int, dict]:
    """Same request, but authenticated as an app token rather than the
    dashboard user — mirrors ``test_chat_fork_error_codes.py``'s
    ``test_the_three_404s_stay_indistinguishable`` app-identity fixture."""

    @web.middleware
    async def _as_app(request: web.Request, handler):
        request["app"] = app_name
        request["user"] = app_name
        return await handler(request)

    app = _make_app(state)
    app.middlewares.insert(0, _as_app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/chat/slots/{slot_name}/promote", json=payload if payload is not None else {}
        )
        return resp.status, await resp.json()


async def _promote_with_no_app_claim(state, slot_name: str) -> tuple[int, dict]:
    """Same request from a caller the auth layer could not place.

    ``token_auth_middleware``'s internal-secret arm (loopback +
    ``X-Internal-Secret``, which ``/api/chat``'s mixed-internal prefix admits)
    publishes the ``app`` claim NARROWING-ONLY: it is set only when an app is
    positively resolved, and left ABSENT when the caller cannot be placed. So
    "no app claim" is not the dashboard user — it is every caller holding the
    internal secret, which a spawned shell can read off the VISIBLE
    ``.local_secret`` crew-home leaf. The middleware runs INNER of the helper's
    dashboard-owner default so it can drop the claim that default fills in."""

    @web.middleware
    async def _strip_app_claim(request: web.Request, handler):
        request.pop("app", None)
        return await handler(request)

    app = _make_app(state)
    app.middlewares.append(_strip_app_claim)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{slot_name}/promote", json={})
        return resp.status, await resp.json()


# ── success: incognito ──


@pytest.mark.asyncio
async def test_promote_succeeds_for_incognito(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    status, body = await _promote(state, "secret")
    assert status == 200, body
    assert body["ok"] is True
    assert body["key"] != "secret"

    new_slot = state._slots[body["key"]]
    assert new_slot.memory_mode == "persistent"
    # The whole transcript is carried over: 2 copied turns + the promotion note.
    assert [m["role"] for m in new_slot.messages] == ["user", "assistant", "system"]
    assert "Kept from a private chat" in new_slot.messages[-1]["content"]

    # The ephemeral original is untouched.
    original = state._slots["secret"]
    assert original.memory_mode == "incognito"
    assert len(original.messages) == 2


# ── success: temporary (also notes suppressed memory reads) ──


@pytest.mark.asyncio
async def test_promote_succeeds_for_temporary_and_notes_missing_memory_reads(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "scratch", "temporary")
    status, body = await _promote(state, "scratch")
    assert status == 200, body

    new_slot = state._slots[body["key"]]
    assert new_slot.memory_mode == "persistent"
    note = new_slot.messages[-1]
    assert note["role"] == "system"
    assert note.get("meta", {}).get("kind") == "session_promoted"
    assert "temporary mode" in note["content"]
    assert "without reading stored memory" in note["content"]

    original = state._slots["scratch"]
    assert original.memory_mode == "temporary"


@pytest.mark.asyncio
async def test_incognito_promotion_omits_the_temporary_caveat(tmp_path, monkeypatch) -> None:
    """The suffix is specific to temporary mode's suppressed memory READS —
    incognito only blocks memory WRITES, so it gets the base note only."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    _, body = await _promote(state, "secret")
    note = state._slots[body["key"]].messages[-1]
    assert "temporary mode" not in note["content"]


# ── refusal: nothing to promote ──


@pytest.mark.asyncio
async def test_promote_refused_for_an_already_persistent_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "regular", None)  # defaults to persistent
    assert state._slots["regular"].memory_mode == "persistent"
    status, body = await _promote(state, "regular")
    assert status == 400
    assert body["code"] == "slot_already_persistent"


@pytest.mark.asyncio
async def test_promote_of_an_unknown_slot_is_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    status, body = await _promote(state, "nosuchslot")
    assert status == 404
    assert body["code"] == "slot_not_found"


# ── refusal: app tokens are never allowed to promote ──


@pytest.mark.asyncio
async def test_an_app_token_cannot_promote_a_slot_it_owns(tmp_path, monkeypatch) -> None:
    """Slot ownership is the wrong question for this route. An app that owns
    its own ephemeral slot (App Kit §5.2) is still refused: only a signed-in
    dashboard user may confirm "Keep this chat"."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    state._slots["secret"]._app = "app-a"
    status, body = await _promote_as_app(state, "secret", "app-a")
    assert status == 403
    assert body["code"] == "promote_requires_dashboard_user"
    # Refused before delegating: the slot is untouched and nothing was forked.
    assert state._slots["secret"].memory_mode == "incognito"
    assert len(state._slots) == 1


@pytest.mark.asyncio
async def test_an_app_token_cannot_promote_a_slot_it_does_not_own(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    status, body = await _promote_as_app(state, "secret", "app-b")
    assert status == 403
    assert body["code"] == "promote_requires_dashboard_user"


@pytest.mark.asyncio
async def test_a_caller_with_no_app_claim_cannot_promote(tmp_path, monkeypatch) -> None:
    """Admission needs POSITIVE proof of the dashboard user, never the mere
    absence of an app. The internal-secret transport leaves the claim ABSENT
    when it cannot place the caller, so admitting "no app" would hand
    promotion to any holder of the internal secret — readable by a spawned
    shell from the OS-unfenced ``.local_secret`` leaf — and a private
    transcript would become persistent with no human confirming the dialog."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    status, body = await _promote_with_no_app_claim(state, "secret")
    assert status == 403
    assert body["code"] == "promote_requires_dashboard_user"
    # Refused before delegating: nothing forked, original still ephemeral.
    assert state._slots["secret"].memory_mode == "incognito"
    assert len(state._slots) == 1


@pytest.mark.asyncio
async def test_app_token_promote_refusal_writes_a_sel_audit_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    status, _ = await _promote_as_app(state, "secret", "app-a")
    assert status == 403

    events = sel().recent(limit=50)
    matches = [
        e
        for e in events
        if e.get("operation") == "chat.slot_promote"
        and e.get("outcome") == "denied"
        and e.get("caller_identity") == "app-a"
    ]
    assert matches, [e.get("operation") for e in events]
    assert matches[0].get("source") == "app_isolation"


@pytest.mark.asyncio
async def test_promote_ignores_body_fields_and_always_copies_everything(
    tmp_path, monkeypatch
) -> None:
    """The confirmation dialog promises "the whole conversation is kept" —
    a body trying to narrow the scope (as a fork request could) must not be
    able to make that promise false."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    status, body = await _promote(state, "secret", {"at_message_index": 0, "direction": "tail"})
    assert status == 200, body
    new_slot = state._slots[body["key"]]
    assert len([m for m in new_slot.messages if m["role"] in ("user", "assistant")]) == 2


# ── SEL audit ──


@pytest.mark.asyncio
async def test_a_successful_promotion_writes_a_sel_audit_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "incognito")
    status, body = await _promote(state, "secret")
    assert status == 200, body

    events = sel().recent(limit=50)
    matches = [
        e
        for e in events
        if e.get("operation") == "chat.slot_promote" and e.get("outcome") == "allowed"
    ]
    assert matches, [e.get("operation") for e in events]
    assert "promoted_from=incognito" in matches[0].get("resources", "")


@pytest.mark.asyncio
async def test_a_refused_promotion_also_writes_a_sel_audit_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "regular", None)
    status, _ = await _promote(state, "regular")
    assert status == 400

    events = sel().recent(limit=50)
    matches = [
        e
        for e in events
        if e.get("operation") == "chat.slot_promote" and e.get("outcome") == "denied"
    ]
    assert matches, [e.get("operation") for e in events]


# ── history/summary consumers see the promoted transcript ──


@pytest.mark.asyncio
async def test_the_promoted_transcript_is_no_longer_read_as_private(tmp_path, monkeypatch) -> None:
    """History scans, MCP history tools, and summary/folder derivations all
    gate on the shared ``is_incognito_transcript`` predicate over the
    persisted ``memory_mode`` header (see ``history.md``). Once promoted, the
    NEW slot's own persisted transcript must read back as not-private."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seed(tmp_path, "secret", "temporary")
    status, body = await _promote(state, "secret")
    assert status == 200, body

    new_slot = state._slots[body["key"]]
    meta = state.conversation_log.get_metadata(slot_history_key(new_slot))
    assert meta.get("memory_mode") == "persistent"
    assert not is_incognito_transcript(meta.get("memory_mode"))

    # The ephemeral original is untouched in memory (its own persisted
    # transcript, if any existed on disk, is never written by this handler).
    original = state._slots["secret"]
    assert original.memory_mode == "temporary"
    assert is_incognito_transcript(original.memory_mode)


# ── never agent-reachable ──


def test_promotion_has_no_mcp_tool_surface() -> None:
    """Promotion is strictly human-initiated, with no MCP tool or CLI
    surface an agent could call on its own."""
    from kiro_crew.mcp_tools import build_tool_list

    tools = build_tool_list()
    names = {t.get("name", "") for t in tools}
    assert not any("promote" in n.lower() for n in names), names
    assert not any("slot_promote" in n.lower() for n in names), names


def test_promotion_has_no_reference_in_any_mcp_server_module() -> None:
    from kiro_crew import mcp_core, mcp_cron, mcp_dashboard

    for module in (mcp_core, mcp_cron, mcp_dashboard):
        src = inspect.getsource(module)
        assert "api_chat_slot_promote" not in src, module.__name__
        assert "/promote" not in src, module.__name__


def test_promotion_has_no_cli_command() -> None:
    import kiro_crew.cli_chat as cli_chat_mod

    src = inspect.getsource(cli_chat_mod)
    assert "promote" not in src.lower()
