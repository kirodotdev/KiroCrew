"""The dashboard SSE streams apply the WebSocket's per-app scope to app-token readers.

Every ``GET /api/stream`` queue gets the same raw note from
``DashboardState._broadcast``, so an app token granted the route must still see
only the slots, titles and messages its scope covers. Each reader is drained up
to a fence frame sent last, so the negative checks cover everything delivered
before it, in any order. ``GET /api/logs`` needs the ``log`` event scope, on
every replayed and live entry, and a reader the auth middleware marked neither a
dashboard user nor an app gets neither stream.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from app_token_records import mark_apps_live
from chat_test_helpers import _make_app, _make_state

from kiro_crew.apps import manager_records
from kiro_crew.dashboard import revocation_gen, sse_event_scope, token_auth, ws_event_scope
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.testing.wait import async_wait_until, finished

# Bound at import, before ``scope_state`` replaces the module attribute with a fake.
_real_get_app_manifest = manager_records.get_app_manifest

_SCOPED = "sse-scope-owner-app"
_BYSTANDER = "sse-scope-bystander"
_USER_SLOT = "sse-scope-user-slot"
_APP_SLOT = "sse-scope-app-slot"
_LOGGER = "sse-scope-log-reader"
_WATCHER = "sse-scope-user-watcher"
_FENCE = "sse-scope-test-finished"

_MANIFESTS = {
    # No ``slots:*`` declaration: the own-slot default is its whole slot scope.
    _SCOPED: ["artifacts"],
    _BYSTANDER: [],
    _LOGGER: ["log"],
    _WATCHER: ["slots:user"],
}
_OWNER_ONLY = {"branch": "sse-b", "commit": "sse-c", "update_policy_file": "/srv/policy/p"}


@pytest.fixture
def unreadable():
    """Apps whose manifest read raises inside the real declaration loader."""
    return set()


@pytest.fixture
def scope_state(tmp_path, monkeypatch, unreadable):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"sse-scope-test-signing-key")
    mark_apps_live(monkeypatch)
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: True)
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    for name in ("_declared_cache", "_exposeto_cache", "_sel_last_audit"):
        monkeypatch.setattr(ws_event_scope, name, {})
    for name in ("_declared_refreshing", "_exposeto_refreshing"):
        monkeypatch.setattr(ws_event_scope, name, set())
    monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _name: True)

    def manifest(name):
        events = _MANIFESTS.get(name)
        if events is None:
            return None
        return SimpleNamespace(
            permissions=SimpleNamespace(api=["/api/stream", "/api/logs"], events=events)
        )

    def scope_manifest(name):
        if name in unreadable:
            raise OSError("app.json unreadable")
        return manifest(name)

    # The token middleware keeps reading the manifest: only the scope loader fails.
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", manifest)
    monkeypatch.setattr(ws_event_scope, "get_app_manifest", scope_manifest)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    state = _make_state(tmp_path)
    state.get_or_create_slot(_USER_SLOT, origin=SlotOrigin.USER)
    state.get_or_create_slot(_APP_SLOT, app=_SCOPED, origin=SlotOrigin.APP)
    monkeypatch.setattr(state, "status_snapshot", lambda **_kwargs: dict(_OWNER_ONLY))
    import time as _time

    from kiro_crew.dashboard import status_counts as _sc

    monkeypatch.setattr(_sc, "_counts_cache", (0, 0))
    monkeypatch.setattr(_sc, "_counts_cache_ts", _time.monotonic())
    monkeypatch.setattr(_sc, "_counts_cache_failures", 0)
    monkeypatch.setattr(_sc, "_counts_refresh_inflight", False)
    yield state
    stopped.set()


def _row(key: str) -> dict:
    return {"key": key, "title": f"title-{key}", "last_message": f"said-in-{key}"}


def _notes() -> list[dict]:
    rows = [_row(_USER_SLOT), _row(_APP_SLOT)]
    notes: list[dict] = [{"_type": "slots", "_slots_list": rows, "slots": json.dumps(rows)}]
    for key in (_USER_SLOT, _APP_SLOT):
        notes.append({"_type": "slot_title", "key": key, "title": f"renamed-{key}"})
        notes.append(
            {"_type": "chat_message", "slot": key, "role": "assistant", "content": f"msg-{key}"}
        )
    notes.append({"_type": "artifact_update", "slug": "sse-scope-artifact", "version": 2})
    return notes


async def _until_fence(response) -> list[tuple[str, str]]:
    frames = []
    while True:
        block = (await response.content.readuntil(b"\n\n")).decode()
        if not block:
            raise EOFError("SSE closed before the fence")
        event, _, data = block.partition("\ndata: ")
        event = event.removeprefix("event: ")
        if event == "refresh" and _FENCE in data:
            return frames
        if event != "dashboard":
            frames.append((event, data.strip()))


def _token(app_claim: str) -> str:
    return token_auth.generate_token("local-app", **({"app": app_claim} if app_claim else {}))


_STATUS: dict[str, dict] = {}


async def _read_streams(state, readers=(_SCOPED, _BYSTANDER), after_connect=None):
    """Each reader's frames up to the fence; its first status frame lands in ``_STATUS``."""
    app = _make_app(state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app.router.add_get("/api/stream", updates.api_stream)
    readers = {"dashboard": "", **{name: name for name in readers}}
    async with TestClient(TestServer(app, host="127.0.0.1", port=0)) as client:
        streams = {}
        for name, app_claim in readers.items():
            stream = await client.get("/api/stream", params={"token": _token(app_claim)})
            assert stream.status == 200, name
            # The first status frame proves register_sse() ran for this reader.
            first = await finished(stream.content.readuntil(b"\n\n"), f"{name} first")
            assert first.startswith(b"event: dashboard"), name
            _STATUS[name] = json.loads(first.decode().partition("\ndata: ")[2])
            streams[name] = stream
        if after_connect is not None:
            after_connect()
        for note in _notes():
            state._broadcast(note)
        state._broadcast({"_type": "refresh", "kinds": _FENCE})
        drained = await finished(
            asyncio.gather(*(_until_fence(s) for s in streams.values())), "fence"
        )
        for stream in streams.values():
            stream.close()
    return dict(zip(readers, drained))


def _slot_rows(frames) -> list[str]:
    return [row["key"] for event, data in frames if event == "slots" for row in json.loads(data)]


def _by_event(frames, event) -> list[dict]:
    return [json.loads(data) for name, data in frames if name == event]


@pytest.mark.asyncio
async def test_app_token_stream_reader_sees_only_its_scope(scope_state):
    got = await _read_streams(scope_state)

    dashboard = got["dashboard"]
    assert sorted(_slot_rows(dashboard)) == sorted([_USER_SLOT, _APP_SLOT])
    assert {t["key"] for t in _by_event(dashboard, "slot_title")} == {_USER_SLOT, _APP_SLOT}
    assert {m["slot"] for m in _by_event(dashboard, "chat_message")} == {_USER_SLOT, _APP_SLOT}
    assert [n.get("slug") for n in _by_event(dashboard, "notification")] == ["sse-scope-artifact"]

    scoped = got[_SCOPED]
    assert _slot_rows(scoped) == [_APP_SLOT]
    assert [t["key"] for t in _by_event(scoped, "slot_title")] == [_APP_SLOT]
    assert [m["slot"] for m in _by_event(scoped, "chat_message")] == [_APP_SLOT]
    assert [n.get("slug") for n in _by_event(scoped, "notification")] == ["sse-scope-artifact"]
    assert _USER_SLOT not in "".join(data for _, data in scoped)

    # No slot in scope: no slots frame at all, and nothing else scoped.
    assert got[_BYSTANDER] == []

    # The status frame drops the owner-only fields for an app, as on the WebSocket.
    assert {k: _STATUS["dashboard"][k] for k in _OWNER_ONLY} == _OWNER_ONLY
    assert not set(_OWNER_ONLY) & set(_STATUS[_SCOPED])
    assert (_SCOPED, "dashboard", "granted") in ws_event_scope._sel_last_audit


@pytest.mark.asyncio
async def test_unreadable_declarations_send_nothing_scoped(scope_state, unreadable):
    """The manifest read fails inside the loader: Tier 0 only, not the own-slot default."""
    unreadable.add(_SCOPED)
    got = await _read_streams(scope_state)
    assert got[_SCOPED] == []
    assert sorted(_slot_rows(got["dashboard"])) == sorted([_USER_SLOT, _APP_SLOT])
    audit = ws_event_scope._sel_last_audit
    assert (_SCOPED, "stream", "declarations_unreadable") in audit
    assert (_SCOPED, "chat_message", "declarations_unreadable") in audit
    assert (_SCOPED, "refresh", "granted") in audit


def test_the_websocket_keeps_its_mapping_of_an_unreadable_set(scope_state, unreadable):
    """The WS loader still reads an unreadable set as empty; only SSE sees the marker."""
    unreadable.add(_SCOPED)
    assert ws_event_scope.load_declared_events_checked(_SCOPED) == (True, None)
    assert ws_event_scope.load_declared_events_for_connect(_SCOPED) == (True, frozenset())
    # The WebSocket's fields keep the mapping; the marker has its own field.
    assert ws_event_scope._declared_cache[_SCOPED][1:] == (True, frozenset(), True)
    # A successful read that declares nothing stays an empty set, not the marker.
    assert ws_event_scope.load_declared_events_checked(_BYSTANDER) == (True, frozenset())


@pytest.fixture
def corrupt_manifest(tmp_path, monkeypatch, scope_state):
    """Write *name*'s ``app.json`` as corrupt JSON and read it with the real loader."""
    monkeypatch.setattr(manager_records, "config_dir", lambda: tmp_path)
    corrupt: set[str] = set()
    fake = ws_event_scope.get_app_manifest

    def scope_manifest(name):
        return _real_get_app_manifest(name) if name in corrupt else fake(name)

    monkeypatch.setattr(ws_event_scope, "get_app_manifest", scope_manifest)

    def write(name: str) -> None:
        root = manager_records.app_dir(name)
        root.mkdir(parents=True)
        (root / manager_records.APP_MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
        # A readable record, so the token middleware (on its fake manifest) still
        # admits the app; the consent key keeps the record read off the manifest.
        record = {
            "name": name,
            "version": "1.0.0",
            "enabled": True,
            "sessionApprovalConsentPending": True,
        }
        (root / manager_records.INSTALLED_META_FILENAME).write_text(
            json.dumps(record), encoding="utf-8"
        )
        assert _real_get_app_manifest(name) is None
        corrupt.add(name)

    return write


@pytest.mark.asyncio
async def test_a_corrupt_manifest_sends_nothing_scoped(scope_state, corrupt_manifest):
    """``get_app_manifest`` swallows the parse error: still unreadable, Tier 0 only."""
    corrupt_manifest(_SCOPED)
    got = await _read_streams(scope_state)
    assert got[_SCOPED] == []
    assert (_SCOPED, "stream", "declarations_unreadable") in ws_event_scope._sel_last_audit
    assert (_SCOPED, "refresh", "granted") in ws_event_scope._sel_last_audit
    # The WebSocket keeps its mapping: an empty set, so the app's own slots still go out.
    assert ws_event_scope.load_declared_events_checked(_SCOPED) == (True, None)
    assert ws_event_scope.load_declared_events_for_connect(_SCOPED) == (True, frozenset())


@pytest.mark.asyncio
async def test_log_stream_refuses_a_corrupt_manifest(scope_state, log_ring, corrupt_manifest):
    corrupt_manifest(_LOGGER)
    got = await _log_reads(scope_state)
    assert got[_LOGGER][0] == 403 and _LOG_LINE not in got[_LOGGER][1]
    assert got["dashboard"][0] == 200 and _LOG_LINE in got["dashboard"][1]
    assert (_LOGGER, "log", "declarations_unreadable") in ws_event_scope._sel_last_audit


@pytest.fixture
def grants_unread(monkeypatch):
    """``staged_app_grants`` returns before reading the manifest, as mid tree swap."""
    monkeypatch.setattr(ws_event_scope, "staged_app_grants", lambda _name, _family, _read: [])


@pytest.mark.asyncio
async def test_grants_returned_unread_send_nothing_scoped(scope_state, grants_unread):
    """No read of the declarations is unreadable, not a read that declares nothing."""
    got = await _read_streams(scope_state)
    assert got[_SCOPED] == []
    assert sorted(_slot_rows(got["dashboard"])) == sorted([_USER_SLOT, _APP_SLOT])
    assert (_SCOPED, "stream", "declarations_unreadable") in ws_event_scope._sel_last_audit
    assert ws_event_scope.load_declared_events_checked(_SCOPED) == (True, None)
    assert ws_event_scope.load_declared_events_for_connect(_SCOPED) == (True, frozenset())
    assert ws_event_scope._declared_cache[_SCOPED][1:] == (True, frozenset(), True)


@pytest.mark.asyncio
async def test_log_stream_refuses_grants_returned_unread(scope_state, log_ring, grants_unread):
    got = await _log_reads(scope_state)
    assert got[_LOGGER][0] == 403 and _LOG_LINE not in got[_LOGGER][1]
    assert got["dashboard"][0] == 200 and _LOG_LINE in got["dashboard"][1]
    assert (_LOGGER, "log", "declarations_unreadable") in ws_event_scope._sel_last_audit


def _corrupt_and_reread(corrupt_manifest, name: str) -> None:
    """Corrupt *name*'s ``app.json``, then re-read it as another connect would."""
    corrupt_manifest(name)
    assert ws_event_scope.load_declared_events_for_connect(name) == (True, frozenset())


@pytest.mark.asyncio
async def test_declarations_unreadable_after_connect_withhold_later_notes(
    scope_state, corrupt_manifest
):
    """Readable at connect, unreadable on a later read: Tier 0 only from then on."""
    before = await _read_streams(scope_state, readers=(_SCOPED,))
    assert _slot_rows(before[_SCOPED]) == [_APP_SLOT]
    after = await _read_streams(
        scope_state,
        readers=(_SCOPED,),
        after_connect=lambda: _corrupt_and_reread(corrupt_manifest, _SCOPED),
    )
    assert after[_SCOPED] == []
    assert sorted(_slot_rows(after["dashboard"])) == sorted([_USER_SLOT, _APP_SLOT])
    audit = ws_event_scope._sel_last_audit
    assert (_SCOPED, "chat_message", "declarations_unreadable") in audit
    # The WebSocket keeps its mapping: declares nothing, own slots, not revoked.
    assert ws_event_scope.effective_allowed_events(_SCOPED, frozenset({"artifacts"})) == (
        frozenset()
    )
    assert ws_event_scope.app_events_revoked(_SCOPED) is False


@pytest.mark.asyncio
async def test_a_disabled_app_is_refused_the_stream(scope_state, monkeypatch):
    monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda name: name != _SCOPED)
    app = _make_app(scope_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app.router.add_get("/api/stream", updates.api_stream)
    async with TestClient(TestServer(app, host="127.0.0.1", port=0)) as client:
        resp = await client.get("/api/stream", params={"token": _token(_SCOPED)})
        assert resp.status == 403
        assert json.loads(await resp.text())["code"] == "forbidden"
        ok = await client.get("/api/stream", params={"token": _token("")})
        assert ok.status == 200
        ok.close()
    assert (_SCOPED, "stream", "app_disabled") in ws_event_scope._sel_last_audit


@pytest.mark.asyncio
async def test_a_scope_revoked_after_connect_is_withheld(scope_state):
    """The stream re-reads the live declarations per note, as the WebSocket does."""

    def revoke():
        ws_event_scope._declared_cache[_WATCHER] = (time.monotonic(), True, frozenset(), False)

    before = await _read_streams(scope_state, readers=(_WATCHER,))
    assert _slot_rows(before[_WATCHER]) == [_USER_SLOT]
    after = await _read_streams(scope_state, readers=(_WATCHER,), after_connect=revoke)
    assert after[_WATCHER] == []
    assert sorted(_slot_rows(after["dashboard"])) == sorted([_USER_SLOT, _APP_SLOT])


def test_scope_helper_is_the_ws_gate(monkeypatch):
    """The helper defers to ``ws_event_allowed``; it has no evaluator of its own."""
    seen = []

    def gate(event_type, data, **_kwargs):
        seen.append(event_type)
        return False

    monkeypatch.setattr(sse_event_scope, "ws_event_allowed", gate)
    monkeypatch.setattr(sse_event_scope, "effective_allowed_events", lambda _a, s: s)
    note = {"_type": "slot_title", "key": "k", "title": "t"}
    assert (
        sse_event_scope.scope_note_for_app(
            note, app="a", connect_events=frozenset(), state=SimpleNamespace(_slots={})
        )
        is None
    )
    assert seen == ["slot_title"]


_LOG_LINE = "sse-scope-log-line"


@pytest.fixture
def log_ring(monkeypatch):
    """A ring holding one known entry, so a replay is read up to it and no further."""
    ring = collections.deque([json.dumps({"level": "INFO", "msg": _LOG_LINE})], maxlen=10)
    monkeypatch.setattr(updates, "_log_ring", ring)
    return ring


async def _log_reads(state) -> dict[str, tuple[int, str]]:
    """Status and the first SSE block (or the refusal body) of each log reader."""
    app = _make_app(state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app.router.add_get("/api/logs", updates.api_logs)
    readers = {"dashboard": "", _LOGGER: _LOGGER, _SCOPED: _SCOPED}
    got = {}
    async with TestClient(TestServer(app, host="127.0.0.1", port=0)) as client:
        for name, app_claim in readers.items():
            token = token_auth.generate_token(
                "local-app", **({"app": app_claim} if app_claim else {})
            )
            resp = await client.get("/api/logs", params={"token": token, "lines": "1"})
            if resp.status == 200:
                body = await finished(resp.content.readuntil(b"\n\n"), f"{name} replay")
                got[name] = (200, body.decode())
            else:
                got[name] = (resp.status, await resp.text())
            resp.close()
    return got


@pytest.mark.asyncio
async def test_log_stream_needs_the_log_scope(scope_state, log_ring):
    got = await _log_reads(scope_state)
    assert got["dashboard"][0] == 200 and _LOG_LINE in got["dashboard"][1]
    assert got[_LOGGER][0] == 200 and _LOG_LINE in got[_LOGGER][1]
    status, body = got[_SCOPED]
    assert status == 403
    assert json.loads(body)["code"] == "forbidden"
    assert _LOG_LINE not in body


@pytest.mark.asyncio
async def test_a_dashboard_user_log_read_records_its_grant(scope_state, log_ring):
    """The replay bypasses the chokepoint, so the grant is recorded at the door."""
    got = await _log_reads(scope_state)
    assert got["dashboard"][0] == 200
    grant = (ws_event_scope.DASHBOARD_USER_AUDITEE, "log", "granted")
    assert grant in ws_event_scope._sel_last_audit


@pytest.mark.asyncio
async def test_log_stream_refuses_unreadable_declarations(scope_state, log_ring, unreadable):
    unreadable.add(_LOGGER)
    got = await _log_reads(scope_state)
    assert got[_LOGGER][0] == 403 and _LOG_LINE not in got[_LOGGER][1]
    assert got["dashboard"][0] == 200 and _LOG_LINE in got["dashboard"][1]
    assert (_LOGGER, "log", "declarations_unreadable") in ws_event_scope._sel_last_audit


def _revoke_log_scope() -> None:
    ws_event_scope._declared_cache[_LOGGER] = (time.monotonic(), True, frozenset(), False)


async def _until_eof_or(resp, leaks: list[bytes]) -> bytes:
    """Read to EOF, or only until a leaked entry shows up (so a leak fails fast)."""
    body = b""
    while chunk := await resp.content.readany():
        body += chunk
        if any(leak in body for leak in leaks):
            break
    return body


async def _logger_body(
    state, *, lines: int, leaks: list[bytes], after_open=None, reason="log_scope_not_granted"
) -> bytes:
    """What an admitted ``_LOGGER`` stream sends until it ends (or leaks)."""
    app = _make_app(state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app.router.add_get("/api/logs", updates.api_logs)
    async with TestClient(TestServer(app, host="127.0.0.1", port=0)) as client:
        resp = await client.get("/api/logs", params={"token": _token(_LOGGER), "lines": lines})
        assert resp.status == 200
        if after_open is not None:
            await after_open(resp)
        body = await finished(_until_eof_or(resp, leaks), "the log stream ending")
    assert (_LOGGER, "log", reason) in ws_event_scope._sel_last_audit
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_after", [0, 1])
async def test_replayed_log_entries_stop_when_the_log_scope_is_revoked(
    scope_state, log_ring, monkeypatch, revoked_after
):
    """Revoked after admission (in prepare) or between two replayed entries."""
    log_ring.clear()
    log_ring.extend(json.dumps({"msg": f"{_LOG_LINE}-{n}"}) for n in range(3))
    real_prepare, real_write = web.StreamResponse.prepare, web.StreamResponse.write

    async def prepare(self, request):
        result = await real_prepare(self, request)
        if revoked_after == 0:
            _revoke_log_scope()
        return result

    async def write(self, data):
        await real_write(self, data)
        if f"{_LOG_LINE}-0".encode() in data:
            _revoke_log_scope()

    monkeypatch.setattr(web.StreamResponse, "prepare", prepare)
    monkeypatch.setattr(web.StreamResponse, "write", write)
    leaks = [f"{_LOG_LINE}-{n}".encode() for n in range(revoked_after, 3)]
    body = (await _logger_body(scope_state, lines=3, leaks=leaks)).decode()
    sent = [n for n in range(3) if f"{_LOG_LINE}-{n}" in body]
    assert sent == list(range(revoked_after))


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["revoked", "unreadable"])
async def test_live_log_entries_stop_when_the_log_scope_is_revoked(
    scope_state, log_ring, corrupt_manifest, cause
):
    """The tail re-reads the live scope per entry, as ``_safe_ws_send`` does.

    ``unreadable``: declarations readable at connect that a later read finds
    unreadable end the tail too, while the WebSocket keeps its empty-set mapping.
    """
    secret = "sse-scope-live-line-after-revoke"
    root = logging.getLogger("kiro_crew")

    def revoke():
        if cause == "revoked":
            _revoke_log_scope()
        else:
            _corrupt_and_reread(corrupt_manifest, _LOGGER)
            assert ws_event_scope._declared_cache[_LOGGER][1:] == (True, frozenset(), True)

    async def revoke_then_log(resp):
        replay = await finished(resp.content.readuntil(b"\n\n"), "replay")
        assert _LOG_LINE.encode() in replay
        await async_wait_until(
            lambda: any(type(h).__name__ == "_QueueLogHandler" for h in root.handlers),
            describe=lambda: "the live log handler attached",
        )
        revoke()
        root.error(secret)

    reason = "log_scope_not_granted" if cause == "revoked" else "declarations_unreadable"
    body = await _logger_body(
        scope_state, lines=1, leaks=[secret.encode()], after_open=revoke_then_log, reason=reason
    )
    assert secret.encode() not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/stream", "/api/logs"])
async def test_a_reader_with_neither_mark_is_refused(scope_state, log_ring, path):
    """No auth middleware ran: neither ``is_dashboard_user`` nor an app claim."""
    app = web.Application()
    app["state"] = scope_state
    app.router.add_get("/api/stream", updates.api_stream)
    app.router.add_get("/api/logs", updates.api_logs)
    async with TestClient(TestServer(app, host="127.0.0.1", port=0)) as client:
        resp = await client.get(path)
        # Status first: an admitted reader's stream never ends, so its body is not read.
        assert resp.status == 403
        body = await resp.text()
    assert json.loads(body)["code"] == "forbidden"
    assert _LOG_LINE not in body


def _boom(*_args, **_kwargs):
    raise RuntimeError("scope read failed")


@pytest.fixture
def audits(monkeypatch):
    """Record the helper's SEL calls instead of writing them."""
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(sse_event_scope, "_audit_deny", lambda a, e, r: seen.append((a, e, r)))
    monkeypatch.setattr(sse_event_scope, "_audit_allow", lambda a, e: seen.append((a, e, "ok")))
    monkeypatch.setattr(sse_event_scope, "declared_events_unreadable", lambda _a: False)
    return seen


_NO_SLOTS = SimpleNamespace(_slots={})


@pytest.mark.asyncio
async def test_a_failed_connect_read_is_unreadable_and_refuses_the_log(audits, monkeypatch):
    monkeypatch.setattr(sse_event_scope, "load_declared_events_checked", _boom)
    request = make_mocked_request("GET", "/api/logs")
    refused, events = await sse_event_scope.log_reader_refusal(request, "a")
    assert refused is not None and refused.status == 403
    assert events == frozenset()
    assert ("a", "log", "scope_check_exception") in audits


@pytest.mark.asyncio
async def test_a_disabled_app_is_refused_the_log(audits, monkeypatch):
    monkeypatch.setattr(
        sse_event_scope, "load_declared_events_checked", lambda _a: (False, frozenset({"log"}))
    )
    refused, _events = await sse_event_scope.log_reader_refusal(
        make_mocked_request("GET", "/api/logs"), "a"
    )
    assert refused is not None and refused.status == 403
    assert ("a", "log", "app_disabled") in audits


def test_a_failed_live_check_counts_as_unreadable(audits, monkeypatch):
    monkeypatch.setattr(sse_event_scope, "declared_events_unreadable", _boom)
    note = {"_type": "slot_title", "key": "k", "title": "t"}
    got = sse_event_scope.scope_note_for_app(
        note, app="a", connect_events=frozenset({"slots"}), state=_NO_SLOTS
    )
    assert got is None
    assert sse_event_scope.log_scope_granted("a", frozenset({"log"})) is False
    assert ("a", "log", "declarations_unreadable") in audits


def test_a_failed_live_scope_read_refuses(audits, monkeypatch):
    monkeypatch.setattr(sse_event_scope, "effective_allowed_events", _boom)
    assert sse_event_scope.log_scope_granted("a", frozenset({"log"})) is False
    note = {"_type": "slot_title", "key": "k", "title": "t"}
    assert (
        sse_event_scope.scope_note_for_app(
            note, app="a", connect_events=frozenset({"slots"}), state=_NO_SLOTS
        )
        is None
    )
    assert ("a", "log", "scope_check_exception") in audits
    assert ("a", "slot_title", "scope_check_exception") in audits


def test_a_slots_note_without_its_parsed_list_is_read_from_json(monkeypatch):
    monkeypatch.setattr(sse_event_scope, "declared_events_unreadable", lambda _a: False)
    monkeypatch.setattr(sse_event_scope, "effective_allowed_events", lambda _a, s: s)
    monkeypatch.setattr(sse_event_scope, "ws_event_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        sse_event_scope,
        "filter_slots_for_app",
        lambda rows, _app, _allowed, _state: [r for r in rows if r["key"] == "mine"],
    )
    note = {"_type": "slots", "slots": json.dumps([{"key": "mine"}, {"key": "other"}])}
    got = sse_event_scope.scope_note_for_app(
        note, app="a", connect_events=frozenset({"slots"}), state=_NO_SLOTS
    )
    assert got is not None and json.loads(got["slots"]) == [{"key": "mine"}]


def test_a_failing_audit_write_does_not_change_the_answer(monkeypatch):
    monkeypatch.setattr(sse_event_scope, "_audit_deny", _boom)
    monkeypatch.setattr(sse_event_scope, "_audit_allow", _boom)
    monkeypatch.setattr(sse_event_scope, "declared_events_unreadable", lambda _a: False)
    monkeypatch.setattr(sse_event_scope, "effective_allowed_events", lambda _a, s: s)
    assert sse_event_scope.log_scope_granted("a", frozenset({"log"})) is True
    assert sse_event_scope.log_scope_granted("a", frozenset()) is False
