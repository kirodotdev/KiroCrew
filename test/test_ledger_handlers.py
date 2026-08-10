"""Tests for the ledger HTTP handlers (#2641).

Mirrors ``test_artifact_folder_handlers.py``: MagicMock requests + a real
:class:`LedgerStore` rooted at a tmp dir, SEL and slot-history persistence
stubbed out.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import ledgers as lh
from kiro_crew.ledgers import LedgerStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> LedgerStore:
    s = LedgerStore(root=tmp_path / "ledgers")
    monkeypatch.setattr(lh, "_store", lambda: s)
    monkeypatch.setattr(lh, "sel", lambda: MagicMock())
    monkeypatch.setattr(lh, "save_slot_off_loop", AsyncMock())
    return s


def _state(slots: dict | None = None) -> MagicMock:
    state = MagicMock()
    state._slots = slots if slots is not None else {}
    return state


def _request(*, body: dict | None = None, match: dict | None = None, state=None) -> MagicMock:
    req = MagicMock()
    req.match_info = match or {}
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("no body"))
    else:
        req.json = AsyncMock(return_value=body)
    req.app = {"state": state if state is not None else _state()}
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


class TestCreateListGet:
    @pytest.mark.asyncio
    async def test_create_and_list(self, store) -> None:
        resp = await lh.api_ledger_create(_request(body={"title": "Ideas"}))
        assert resp.status == 201
        created = _body(resp)
        assert created["title"] == "Ideas" and created["pinned_by"] == []

        resp = await lh.api_ledgers_list(_request())
        rows = _body(resp)
        assert len(rows) == 1 and rows[0]["id"] == created["id"]
        assert rows[0]["progress"] == {"done": 0, "total": 0}

    @pytest.mark.asyncio
    async def test_create_without_body_defaults(self, store) -> None:
        resp = await lh.api_ledger_create(_request())
        assert resp.status == 201
        assert _body(resp)["title"] == "Untitled ledger"

    @pytest.mark.asyncio
    async def test_get_includes_pinned_by(self, store) -> None:
        lid = store.create("t")["id"]
        slots = {
            "chat-1": SimpleNamespace(ledger_id=lid),
            "chat-2": SimpleNamespace(ledger_id=""),
        }
        resp = await lh.api_ledger_get(
            _request(match={"id": lid}, state=_state(slots))
        )
        assert resp.status == 200
        assert _body(resp)["pinned_by"] == ["chat-1"]

    @pytest.mark.asyncio
    async def test_get_unknown_404(self, store) -> None:
        resp = await lh.api_ledger_get(_request(match={"id": "0123456789ab"}))
        assert resp.status == 404


class TestUpdate:
    @pytest.mark.asyncio
    async def test_content_requires_base_version(self, store) -> None:
        lid = store.create("t")["id"]
        resp = await lh.api_ledger_update(
            _request(body={"content": "x"}, match={"id": lid})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_cas_write_and_stale_409(self, store) -> None:
        lid = store.create("t")["id"]
        ok = await lh.api_ledger_update(
            _request(body={"content": "v2 text", "base_version": 1}, match={"id": lid})
        )
        assert ok.status == 200 and _body(ok)["version"] == 2

        stale = await lh.api_ledger_update(
            _request(body={"content": "loser", "base_version": 1}, match={"id": lid})
        )
        assert stale.status == 409
        payload = _body(stale)
        assert payload["error"] == "version_conflict"
        assert payload["current"] == {"content": "v2 text", "version": 2}
        # The losing write never landed.
        assert store.get(lid)["content"] == "v2 text"

    @pytest.mark.asyncio
    async def test_rename(self, store) -> None:
        lid = store.create("t")["id"]
        resp = await lh.api_ledger_update(
            _request(body={"title": "renamed"}, match={"id": lid})
        )
        assert resp.status == 200 and _body(resp)["title"] == "renamed"

    @pytest.mark.asyncio
    async def test_nothing_to_update_400(self, store) -> None:
        lid = store.create("t")["id"]
        resp = await lh.api_ledger_update(_request(body={}, match={"id": lid}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversize_content_400(self, store) -> None:
        lid = store.create("t")["id"]
        resp = await lh.api_ledger_update(
            _request(body={"content": "y" * 50_001, "base_version": 1}, match={"id": lid})
        )
        assert resp.status == 400


class TestToggle:
    @pytest.mark.asyncio
    async def test_toggle_ok_and_conflict(self, store) -> None:
        lid = store.create("t")["id"]
        store.update(lid, content="- [ ] item\n", base_version=1)
        ok = await lh.api_ledger_toggle(
            _request(body={"line": 0, "expected": "- [ ] item"}, match={"id": lid})
        )
        assert ok.status == 200 and "- [x] item" in _body(ok)["content"]

        conflict = await lh.api_ledger_toggle(
            _request(body={"line": 0, "expected": "- [ ] item"}, match={"id": lid})
        )
        assert conflict.status == 409

    @pytest.mark.asyncio
    async def test_toggle_validates_input(self, store) -> None:
        lid = store.create("t")["id"]
        resp = await lh.api_ledger_toggle(
            _request(body={"line": -1, "expected": "x"}, match={"id": lid})
        )
        assert resp.status == 400


class TestDeleteAndPin:
    @pytest.mark.asyncio
    async def test_delete_unpins_slots(self, store) -> None:
        lid = store.create("t")["id"]
        slot = SimpleNamespace(ledger_id=lid)
        state = _state({"chat-1": slot})
        resp = await lh.api_ledger_delete(_request(match={"id": lid}, state=state))
        assert resp.status == 200 and _body(resp)["ok"] is True
        assert slot.ledger_id == ""
        state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_pin_and_unpin_slot(self, store) -> None:
        lid = store.create("t")["id"]
        slot = SimpleNamespace(ledger_id="")
        state = _state({"chat-1": slot})
        resp = await lh.api_chat_slot_ledger(
            _request(body={"ledger_id": lid}, match={"slot": "chat-1"}, state=state)
        )
        assert resp.status == 200 and slot.ledger_id == lid

        resp = await lh.api_chat_slot_ledger(
            _request(body={"ledger_id": ""}, match={"slot": "chat-1"}, state=state)
        )
        assert resp.status == 200 and slot.ledger_id == ""

    @pytest.mark.asyncio
    async def test_pin_unknown_ledger_400(self, store) -> None:
        slot = SimpleNamespace(ledger_id="")
        state = _state({"chat-1": slot})
        resp = await lh.api_chat_slot_ledger(
            _request(body={"ledger_id": "0123456789ab"}, match={"slot": "chat-1"}, state=state)
        )
        assert resp.status == 400 and slot.ledger_id == ""

    @pytest.mark.asyncio
    async def test_pin_unknown_slot_404(self, store) -> None:
        resp = await lh.api_chat_slot_ledger(
            _request(body={"ledger_id": ""}, match={"slot": "nope"}, state=_state())
        )
        assert resp.status == 404
