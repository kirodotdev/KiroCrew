"""``CardStore.warm`` answers 503 on a read failure, never an empty store, and
never falls back to a synchronous disk read on the event loop.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import in_dashboard_turn

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard.change_cards import (
    CODE_STORE_UNAVAILABLE,
    CardError,
    CardStore,
    _store_unavailable,
)
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.state import _ChatSlot

SLOT = "chat-1"
SK = f"dashboard:{SLOT}"
#: A reason with non-ASCII text, so the store round-trips through a UTF-8 read.
UTF8_REASON = "répondez plus brièvement — 返信は短く 🙂"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _propose(store: CardStore, reason: str = "you asked for shorter answers") -> dict[str, Any]:
    params = {"path": "chat.verbosity", "value": "ultra-brief"}
    before = {"value": "standard"}
    preview = catalog.build_preview("setting.change", params, before, {})
    return store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="setting.change",
        params=params,
        reason=reason,
        preview=preview,
        before=before,
        context={},
    )


def _seed(path: Path, *, reason: str = "you asked for shorter answers") -> tuple[dict, bytes]:
    """Flush one proposed card to *path*; return the record and its real MAC key."""
    store = CardStore(path, clock=Clock())
    rec = _propose(store, reason=reason)
    asyncio.run(store.flush())
    return rec, store._store_key()


# ── store level: both error branches raise, UTF-8 round-trips, retry recovers ──


def test_warm_raises_when_the_worker_read_fails(tmp_path, monkeypatch):
    """Branch (a): the off-thread read raises -> warm raises 503, stays unloaded."""
    path = tmp_path / "change_cards.json"
    rec, _key = _seed(path)
    real_read = Path.read_text
    denied = True

    def read_text(self: Path, *a: Any, **k: Any) -> str:
        if denied and self == path:
            raise PermissionError("denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    again = CardStore(path, clock=Clock())
    with pytest.raises(CardError) as exc:
        asyncio.run(again.warm())
    assert (exc.value.status, exc.value.code) == (503, CODE_STORE_UNAVAILABLE)
    assert again._loaded is False  # not emptied: a later warm tries again
    # Retry recovery: once the file reads again, warm loads the real card.
    denied = False
    asyncio.run(again.warm())
    assert [c["id"] for c in again.pending(None)] == [rec["id"]]


def test_warm_raises_when_the_key_is_unavailable(tmp_path):
    """Branch (a) via the vault: a store holding cards whose key cannot be read
    refuses rather than loading them unverified or dropping them."""
    path = tmp_path / "change_cards.json"
    rec, real_key = _seed(path)
    available = False

    def flaky_key() -> bytes:
        if not available:
            raise OSError("vault unreadable")
        return real_key

    again = CardStore(path, clock=Clock(), key=flaky_key)
    with pytest.raises(CardError) as exc:
        asyncio.run(again.warm())
    assert (exc.value.status, exc.value.code) == (503, CODE_STORE_UNAVAILABLE)
    assert again._loaded is False


def test_warm_propagates_a_load_text_card_error_rather_than_swallowing_it(tmp_path, monkeypatch):
    """Branch (b): ``_load_text`` raising ``store_unavailable`` is re-raised, not
    swallowed into an empty, loaded store."""
    path = tmp_path / "change_cards.json"
    _seed(path)
    again = CardStore(path, clock=Clock())

    def boom(_text: str) -> None:
        raise _store_unavailable()

    # The real read (and key) succeed; _load_text is what refuses here.
    monkeypatch.setattr(again, "_load_text", boom)
    with pytest.raises(CardError) as exc:
        asyncio.run(again.warm())
    assert (exc.value.status, exc.value.code) == (503, CODE_STORE_UNAVAILABLE)
    assert again._loaded is False


def test_load_text_raises_store_unavailable_when_the_key_cannot_verify(tmp_path):
    """The real ``_load_text`` branch warm relies on: records present, key not."""
    path = tmp_path / "change_cards.json"
    _seed(path)
    text = path.read_text(encoding="utf-8")

    def bad_key() -> bytes:
        raise OSError("vault unreadable")

    store = CardStore(path, clock=Clock(), key=bad_key)
    with pytest.raises(CardError) as exc:
        store._load_text(text)
    assert (exc.value.status, exc.value.code) == (503, CODE_STORE_UNAVAILABLE)


def test_warm_reads_a_utf8_store_without_mangling_it(tmp_path):
    """The store is read as UTF-8: a non-ASCII reason round-trips byte-for-byte."""
    path = tmp_path / "change_cards.json"
    rec, _key = _seed(path, reason=UTF8_REASON)
    raw = path.read_bytes()
    assert UTF8_REASON.encode("utf-8") in raw  # written as UTF-8, not escaped
    again = CardStore(path, clock=Clock())
    asyncio.run(again.warm())
    loaded = again.get(rec["id"])
    assert loaded["reason"] == UTF8_REASON


def test_warm_recovers_on_a_later_call_after_the_key_returns(tmp_path):
    """A transient vault failure refuses once, then warm loads the cards."""
    path = tmp_path / "change_cards.json"
    rec, real_key = _seed(path)
    available = False

    def flaky_key() -> bytes:
        if not available:
            raise OSError("vault unreadable")
        return real_key

    again = CardStore(path, clock=Clock(), key=flaky_key)
    with pytest.raises(CardError):
        asyncio.run(again.warm())
    available = True
    asyncio.run(again.warm())
    assert [c["id"] for c in again.pending(None)] == [rec["id"]]


def test_warm_reads_off_the_event_loop_thread_and_never_syncly(tmp_path, monkeypatch):
    """The disk read happens in a worker thread, so the loop never blocks on it;
    and when it fails, warm refuses rather than retrying ``read_text`` on the
    loop thread via ``_ensure_loaded``."""
    path = tmp_path / "change_cards.json"
    _seed(path)
    loop_thread = threading.get_ident()
    read_threads: list[int] = []
    real_read = Path.read_text

    def read_text(self: Path, *a: Any, **k: Any) -> str:
        if self == path:
            read_threads.append(threading.get_ident())
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)

    async def main() -> None:
        store = CardStore(path, clock=Clock())
        await store.warm()

    asyncio.run(main())
    assert read_threads, "the store file was never read"
    # Every read of the store file ran OFF the loop thread (in to_thread's worker).
    assert loop_thread not in read_threads


def test_a_failing_warm_does_no_synchronous_disk_read(tmp_path, monkeypatch):
    """When the worker read fails, warm raises; nothing re-reads the file on the
    loop thread afterwards (no ``_ensure_loaded`` fallback)."""
    path = tmp_path / "change_cards.json"
    _seed(path)
    loop_thread = threading.get_ident()
    loop_reads: list[int] = []
    real_read = Path.read_text

    def read_text(self: Path, *a: Any, **k: Any) -> str:
        if self == path:
            if threading.get_ident() == loop_thread:
                loop_reads.append(threading.get_ident())
            raise PermissionError("denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)

    async def main() -> None:
        store = CardStore(path, clock=Clock())
        with pytest.raises(CardError):
            await store.warm()

    asyncio.run(main())
    assert loop_reads == []  # the file was never read on the loop thread


# ── handler level: every async caller catches warm's 503 ──


class FakeState:
    owner_id = ""

    def __init__(self, store: CardStore) -> None:
        self._slots: dict[str, _ChatSlot] = {SLOT: in_dashboard_turn(_ChatSlot(SLOT))}
        self.frames: list[tuple[str, dict[str, Any]]] = []
        self._change_card_store = store

    def get_slot(self, name: str) -> _ChatSlot | None:
        return self._slots.get(name)

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        return 1


@web.middleware
async def _fake_auth(request: web.Request, handler):
    who = request.headers.get("X-Test-Auth", "")
    if who == "owner":
        request["user"] = "local-app"
        request["app"] = ""
    return await handler(request)


def _app(store: CardStore) -> web.Application:
    async def patch_config(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
    app["state"] = FakeState(store)
    routes.register_change_card_routes(app)
    app.router.add_patch("/api/config/kirocrew", patch_config)
    return app


def _run(store: CardStore, fn):
    async def main():
        client = TestClient(TestServer(_app(store)))
        await client.start_server()
        try:
            out = await fn(client)
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))
            return out
        finally:
            await client.close()

    return asyncio.run(main())


OWNER = {"X-Test-Auth": "owner"}


def _flaky_store(path: Path, real_key: bytes) -> tuple[CardStore, dict[str, bool]]:
    """A store at *path* whose vault key is unavailable until ``flag['up']``."""
    flag = {"up": False}

    def flaky_key() -> bytes:
        if not flag["up"]:
            raise OSError("vault unreadable")
        return real_key

    return CardStore(path, clock=Clock(), key=flaky_key), flag


def _card_headers(card: dict[str, Any], op: str = "apply", step: int = 0) -> dict[str, str]:
    return {
        "X-Test-Auth": "owner",
        "X-Card-Id": card["id"],
        "X-Card-Revision": str(card["revision"]),
        "X-Card-Op": op,
        "X-Card-Step": str(step),
    }


def test_pending_answers_503_when_warm_refuses(tmp_path):
    path = tmp_path / "change_cards.json"
    _rec, real_key = _seed(path)
    store, flag = _flaky_store(path, real_key)

    async def go(c: TestClient):
        r = await c.get("/api/cards/pending", headers=OWNER)
        return r.status, await r.json()

    status, body = _run(store, go)
    assert status == 503 and body["code"] == CODE_STORE_UNAVAILABLE
    # Recovers once the vault is back.
    flag["up"] = True

    async def go2(c: TestClient):
        r = await c.get("/api/cards/pending", headers=OWNER)
        return r.status, await r.json()

    status2, body2 = _run(store, go2)
    assert status2 == 200 and [c["id"] for c in body2["cards"]] == [_rec["id"]]


def test_pending_answers_503_on_real_invalid_utf8_without_loop_read_or_overwrite(
    tmp_path, monkeypatch
):
    """Invalid UTF-8 answers 503, leaves the file intact and never reads on the loop."""
    path = tmp_path / "change_cards.json"
    # A lone 0xFF/0xFE pair and a bare continuation byte: no valid UTF-8 decoding.
    corrupt = b'{"cards": [\xff\xfe\x80 not utf-8]}'
    path.write_bytes(corrupt)
    assert path.read_bytes() == corrupt  # precondition: on-disk bytes are the corrupt ones

    loop_thread = threading.get_ident()
    loop_reads: list[int] = []
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def tracking_read_text(self: Path, *a: Any, **k: Any) -> str:
        if self == path and threading.get_ident() == loop_thread:
            loop_reads.append(threading.get_ident())
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    store = CardStore(path, clock=Clock())

    async def go(c: TestClient):
        r = await c.get("/api/cards/pending", headers=OWNER)
        return r.status, await r.json()

    status, body = _run(store, go)
    assert status == 503 and body["code"] == CODE_STORE_UNAVAILABLE
    # The read ran off the loop thread (in to_thread's worker), never on the loop.
    assert loop_reads == []
    # The corrupt file was not overwritten by a flush over an "empty" store.
    assert real_read_bytes(path) == corrupt
    assert store._loaded is False


def test_preview_answers_503_when_warm_refuses(tmp_path):
    path = tmp_path / "change_cards.json"
    rec, real_key = _seed(path)
    store, _flag = _flaky_store(path, real_key)

    async def go(c: TestClient):
        r = await c.post(
            f"/api/cards/{rec['id']}/preview",
            json={"params": {"path": "chat.verbosity", "value": "brief"}, "revision": 1},
            headers=OWNER,
        )
        return r.status, await r.json()

    status, body = _run(store, go)
    assert status == 503 and body["code"] == CODE_STORE_UNAVAILABLE


def test_cancel_answers_503_when_warm_refuses(tmp_path):
    path = tmp_path / "change_cards.json"
    rec, real_key = _seed(path)
    store, _flag = _flaky_store(path, real_key)

    async def go(c: TestClient):
        r = await c.post(f"/api/cards/{rec['id']}/cancel", json={"revision": 1}, headers=OWNER)
        return r.status, await r.json()

    status, body = _run(store, go)
    assert status == 503 and body["code"] == CODE_STORE_UNAVAILABLE


def test_dismiss_answers_503_when_warm_refuses(tmp_path):
    path = tmp_path / "change_cards.json"
    rec, real_key = _seed(path)
    store, _flag = _flaky_store(path, real_key)

    async def go(c: TestClient):
        r = await c.post(f"/api/cards/{rec['id']}/dismiss", headers=OWNER)
        return r.status, await r.json()

    status, body = _run(store, go)
    assert status == 503 and body["code"] == CODE_STORE_UNAVAILABLE


def test_a_card_step_answers_503_when_warm_refuses(tmp_path):
    """The hooked route (apply) refuses with 503 when the store cannot be warmed,
    so no step is admitted against an empty store."""
    path = tmp_path / "change_cards.json"
    rec, real_key = _seed(path)
    store, flag = _flaky_store(path, real_key)

    async def go(c: TestClient):
        r = await c.patch(
            "/api/config/kirocrew",
            json={"path": "chat.verbosity", "value": "ultra-brief"},
            headers=_card_headers(rec),
        )
        return r.status, await r.json()

    status, body = _run(store, go)
    assert status == 503 and body["code"] == CODE_STORE_UNAVAILABLE
