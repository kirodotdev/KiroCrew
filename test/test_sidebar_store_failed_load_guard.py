"""A sidebar store whose file fails to load at boot is never replaced by the next create.

``tags.json``, ``tag_boards.json`` and ``chat_pins.json`` are hand-editable. A
leading UTF-8 BOM (Windows PowerShell ``Set-Content -Encoding UTF8``) loads like
plain UTF-8. A file that still cannot be parsed leaves the in-memory list empty,
so the create that follows answers 5xx and leaves the file bytes as they were,
instead of writing a list that holds only the new row over every saved one.
"""

from __future__ import annotations

import json

import chat_tags_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app
from dashboard_chat_pins_helpers import _client as _pins_client

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret

_BOM = b"\xef\xbb\xbf"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return tmp_path


def _pin(i: int) -> dict:
    return {
        "id": f"pin{i}",
        "slot_key": "chat-1",
        "mid": f"m-pin-{i}",
        "role": "assistant",
        "preview": "p",
        "pinned_at": "2026-11-01T00:00:00+00:00",
    }


def _ids(path) -> list[str]:
    return [row["id"] for row in json.loads(path.read_text(encoding="utf-8"))]


def _unreadable(rows: list[dict]) -> dict[str, bytes]:
    """File bodies the loader cannot parse, keyed by test id."""
    text = json.dumps(rows)
    return {
        "utf16le": text.encode("utf-16-le"),
        "typo": text.encode()[:-1],
        "non_list_root": json.dumps({"rows": rows}).encode(),
        # Deeper than the interpreter stack: json.loads raises RecursionError.
        "deep_nesting": b"[" * 100_000 + b"]" * 100_000,
    }


_TAGS = [{"id": "t-a", "name": "A"}, {"id": "t-b", "name": "B"}]
_COLUMNS = [{"id": "c1", "name": "C1", "tag_ids": [], "order": 0}]
_PINS = [_pin(1), _pin(2)]
_PIN_BODY = {
    "slot_key": "chat-1",
    "mid": "m-pin-3",
    "message_ts": "2026-11-01T00:00:01Z",
    "role": "assistant",
    "preview": "p3",
}


@pytest.mark.asyncio
async def test_bom_tags_file_keeps_its_tags_on_create(cfg, tmp_path) -> None:
    (cfg / "tags.json").write_bytes(_BOM + json.dumps(_TAGS).encode())
    state = _make_state(tmp_path)
    state.load_tags()
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.post("/api/chat/tags", json={"name": "Gamma"})
        assert resp.status in (200, 201), await resp.text()
    assert {"t-a", "t-b"} <= set(_ids(cfg / "tags.json"))
    assert len(_ids(cfg / "tags.json")) == 3


@pytest.mark.asyncio
async def test_bom_tag_boards_file_keeps_its_columns_on_create(cfg, tmp_path) -> None:
    (cfg / "tag_boards.json").write_bytes(_BOM + json.dumps(_COLUMNS).encode())
    state = _make_state(tmp_path)
    state.load_tags()
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.post("/api/chat/tag-columns", json={"name": "C2"})
        assert resp.status in (200, 201), await resp.text()
    ids = _ids(cfg / "tag_boards.json")
    assert ids[0] == "c1" and len(ids) == 2


@pytest.mark.asyncio
async def test_bom_chat_pins_file_keeps_its_pins_on_create(cfg, tmp_path) -> None:
    (cfg / "chat_pins.json").write_bytes(_BOM + json.dumps(_PINS).encode())
    async with _pins_client(tmp_path) as client:
        state = client.server.app["state"]
        state.load_chat_pins()
        resp = await client.post("/api/chat/pins", json=_PIN_BODY)
        assert resp.status == 201, await resp.text()
    ids = _ids(cfg / "chat_pins.json")
    assert ids[:2] == ["pin1", "pin2"] and len(ids) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["utf16le", "typo", "non_list_root", "deep_nesting"])
async def test_unreadable_tags_file_refuses_create_and_keeps_bytes(cfg, tmp_path, kind) -> None:
    original = _unreadable(_TAGS)[kind]
    (cfg / "tags.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_tags()
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.post("/api/chat/tags", json={"name": "Gamma"})
        assert resp.status >= 500
    assert (cfg / "tags.json").read_bytes() == original
    assert not any(t.get("name") == "Gamma" for t in state._tags)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["utf16le", "typo", "non_list_root", "deep_nesting"])
async def test_unreadable_tag_boards_file_refuses_create_and_keeps_bytes(
    cfg, tmp_path, kind
) -> None:
    original = _unreadable(_COLUMNS)[kind]
    (cfg / "tag_boards.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_tags()
    async with TestClient(TestServer(_make_tags_app(state))) as client:
        resp = await client.post("/api/chat/tag-columns", json={"name": "C2"})
        assert resp.status >= 500
    assert (cfg / "tag_boards.json").read_bytes() == original
    assert state._tag_boards == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["utf16le", "typo", "non_list_root", "deep_nesting"])
async def test_unreadable_chat_pins_file_refuses_create_and_keeps_bytes(
    cfg, tmp_path, kind
) -> None:
    original = _unreadable(_PINS)[kind]
    (cfg / "chat_pins.json").write_bytes(original)
    async with _pins_client(tmp_path) as client:
        state = client.server.app["state"]
        state.load_chat_pins()
        resp = await client.post("/api/chat/pins", json=_PIN_BODY)
        assert resp.status == 500
    assert (cfg / "chat_pins.json").read_bytes() == original
    assert state._chat_pins == []


def test_boot_backfill_save_skips_an_unreadable_tags_file(cfg, tmp_path) -> None:
    """``save_tags`` logs and returns without writing, so boot keeps going."""
    original = _unreadable(_TAGS)["typo"]
    (cfg / "tags.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_tags()
    state._tags = [{"id": "t-x", "name": "X"}]
    state.save_tags()
    assert (cfg / "tags.json").read_bytes() == original


def test_boot_save_skips_an_unreadable_tag_boards_file(cfg, tmp_path) -> None:
    original = _unreadable(_COLUMNS)["typo"]
    (cfg / "tag_boards.json").write_bytes(original)
    state = _make_state(tmp_path)
    state.load_tags()
    state._tag_boards = [{"id": "c-x", "name": "X", "tag_ids": [], "order": 0}]
    state.save_tag_boards()
    assert (cfg / "tag_boards.json").read_bytes() == original


def test_a_later_good_load_lifts_the_refusal(cfg, tmp_path) -> None:
    (cfg / "chat_pins.json").write_bytes(_unreadable(_PINS)["typo"])
    state = _make_state(tmp_path)
    state.load_chat_pins()
    with pytest.raises(OSError):
        state.save_chat_pins()
    (cfg / "chat_pins.json").write_text(json.dumps(_PINS), encoding="utf-8")
    state.load_chat_pins()
    state._chat_pins.append(_pin(3))
    state.save_chat_pins()
    assert _ids(cfg / "chat_pins.json") == ["pin1", "pin2", "pin3"]


def test_missing_and_empty_files_still_write(cfg, tmp_path) -> None:
    """A missing file seeds or writes as before, and an explicit ``[]`` stays writable."""
    state = _make_state(tmp_path)
    state.load_tags()
    state.load_chat_pins()
    assert (cfg / "tags.json").exists()
    state._chat_pins.append(_pin(1))
    state.save_chat_pins()
    state.save_tag_boards_snapshot(list(_COLUMNS))
    assert _ids(cfg / "chat_pins.json") == ["pin1"]
    assert _ids(cfg / "tag_boards.json") == ["c1"]
    (cfg / "tags.json").write_text("[]", encoding="utf-8")
    state.load_tags()
    assert state._tags == []
    state.save_tags_snapshot([{"id": "t-z", "name": "Z"}])
    assert _ids(cfg / "tags.json") == ["t-z"]
