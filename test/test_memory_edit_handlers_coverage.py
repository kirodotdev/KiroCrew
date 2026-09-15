"""Behavioral coverage for the uncovered branches of ``dashboard/handlers/memory_edit.py``.

Targets: ``api_memory_records``'s malformed pagination and its ``OSError``/
``sqlite3.Error`` fallback to ``store_unavailable``, ``api_memory_records_refresh``'s
owner/write-gate/store refusals and its own DB-error fallback,
``api_memory_record_history``'s malformed pagination, store-unavailable and
success paths, and ``_bulk``'s write-gate refusal, store-unavailable refusal,
and ``OSError``/``sqlite3.Error`` fallback for both preview and apply.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import memory_stores
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config import loader as loader_mod
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import memory_edit
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, owned_store_path
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group("memory_edit_handlers_coverage")

_FINANCE = "finance"
_SESSION_KEY = "dashboard:chat-1"
_OWNER_SUBJECT = "local-app"


class _Slot:
    is_restricted = False
    blocks_reads = False


class _ConversationLog:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings = dict(bindings or {})

    def get_metadata(self, session_key: str) -> dict[str, str]:
        return {"memory_store": self._bindings.get(session_key, "")}


class _State:
    owner_id = ""
    consolidator = None
    sessions = None

    def __init__(self) -> None:
        self.context_builder = None
        self.conversation_log = _ConversationLog()
        self._slots = {"chat-1": _Slot()}
        self._restricted_keys: set[str] = set()


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "data-home"
        self.home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(self.home))
        loader_mod._invalidate_config_cache()
        monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
        self._monkeypatch = monkeypatch
        self._closables: list[VectorMemoryStore] = []
        self.unavailable: set[str] = set()
        monkeypatch.setattr(ContextBuilder, "ensure_store", staticmethod(self._ensure_store))

    def declare(self, *names: str) -> None:
        payload = {
            "memory_stores": {DEFAULT_MEMORY_STORE: {}, **{name: {} for name in names}},
            "default_memory_store": DEFAULT_MEMORY_STORE,
        }
        (self.home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        loader_mod._invalidate_config_cache()
        self._monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)

    async def _ensure_store(self, store: str) -> VectorMemoryStore | None:
        if store in self.unavailable:
            return None
        return self.tier(store)

    def tier(self, store: str) -> VectorMemoryStore:
        path = owned_store_path(store)
        assert path is not None, store
        path.parent.mkdir(parents=True, exist_ok=True)
        tier = VectorMemoryStore(db_path=path)
        tier.init()
        self._closables.append(tier)
        return tier

    def state(self) -> _State:
        return _State()

    def close(self) -> None:
        for tier in self._closables:
            tier.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    e = _Env(tmp_path, monkeypatch)
    try:
        yield e
    finally:
        e.close()


def _body_payload(raw: bytes, loop) -> streams.StreamReader:
    reader = streams.StreamReader(mock.Mock(_reading_paused=False), limit=len(raw) + 1, loop=loop)
    reader.feed_data(raw)
    reader.feed_eof()
    return reader


def _request(
    method: str,
    path: str,
    state: _State,
    *,
    query: dict[str, str] | None = None,
    owner: bool = True,
    body: Any = None,
) -> web.Request:
    import asyncio
    from urllib.parse import urlencode

    app = web.Application()
    app["state"] = state
    target = f"{path}?{urlencode(query)}" if query else path
    headers = {"X-Session-Key": _SESSION_KEY}
    kwargs: dict[str, Any] = {"headers": headers, "app": app}
    if body is not None:
        raw = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
        kwargs["payload"] = _body_payload(raw, asyncio.get_event_loop())
    request = make_mocked_request(method, target, **kwargs)
    if owner:
        request["user"] = _OWNER_SUBJECT
        request["app"] = ""
    return request


def _body(resp: web.Response) -> Any:
    return json.loads(resp.text or "")


# ── api_memory_records: malformed pagination + DB-error fallback ─────────────


class TestApiMemoryRecords:
    @pytest.mark.asyncio
    async def test_malformed_limit_is_a_memory_edit_error_400(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_edit.api_memory_records(
            _request(
                "GET",
                "/api/memory/records",
                env.state(),
                query={"store": _FINANCE, "limit": "nope"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_memory_edit"

    @pytest.mark.asyncio
    async def test_malformed_offset_is_also_a_400(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_edit.api_memory_records(
            _request(
                "GET",
                "/api/memory/records",
                env.state(),
                query={"store": _FINANCE, "offset": "nope"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_memory_edit"

    @pytest.mark.asyncio
    async def test_sqlite_error_during_listing_answers_store_unavailable(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        def _boom(*_a, **_k):
            raise sqlite3.Error("db went away")

        monkeypatch.setattr(memory_edit.memory_edit, "list_records", _boom)
        resp = await memory_edit.api_memory_records(
            _request("GET", "/api/memory/records", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_os_error_during_listing_also_answers_store_unavailable(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        def _boom(*_a, **_k):
            raise OSError("disk gone")

        monkeypatch.setattr(memory_edit.memory_edit, "list_records", _boom)
        resp = await memory_edit.api_memory_records(
            _request("GET", "/api/memory/records", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_unavailable_tier_answers_503_without_calling_list_records(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        called = []
        monkeypatch.setattr(
            memory_edit.memory_edit, "list_records", lambda *a, **k: called.append(1)
        )
        resp = await memory_edit.api_memory_records(
            _request("GET", "/api/memory/records", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 503
        assert called == []


# ── api_memory_records_refresh: owner/write-gate/store refusals + DB error ────


class TestApiMemoryRecordsRefresh:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused_before_reading_the_body(self, env) -> None:
        resp = await memory_edit.api_memory_records_refresh(
            _request(
                "POST",
                "/api/memory/records/refresh",
                env.state(),
                owner=False,
                body={"store": _FINANCE},
            )
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_undeclared_store_is_a_404(self, env) -> None:
        resp = await memory_edit.api_memory_records_refresh(
            _request(
                "POST",
                "/api/memory/records/refresh",
                env.state(),
                body={"store": "nosuchstore"},
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "unknown_memory_store"

    @pytest.mark.asyncio
    async def test_unavailable_tier_answers_503(self, env) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        resp = await memory_edit.api_memory_records_refresh(
            _request(
                "POST",
                "/api/memory/records/refresh",
                env.state(),
                body={"store": _FINANCE},
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_sqlite_error_during_refresh_answers_store_unavailable(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        def _boom(*_a, **_k):
            raise sqlite3.Error("db went away")

        monkeypatch.setattr(memory_edit.memory_edit, "refresh_records", _boom)
        resp = await memory_edit.api_memory_records_refresh(
            _request(
                "POST",
                "/api/memory/records/refresh",
                env.state(),
                body={"store": _FINANCE},
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_memory_edit_error_during_refresh_is_a_400(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        def _boom(*_a, **_k):
            raise memory_edit.memory_edit.MemoryEditError("bad selection")

        monkeypatch.setattr(memory_edit.memory_edit, "refresh_records", _boom)
        resp = await memory_edit.api_memory_records_refresh(
            _request(
                "POST",
                "/api/memory/records/refresh",
                env.state(),
                body={"store": _FINANCE},
            )
        )
        assert resp.status == 400


# ── api_memory_record_history: malformed pagination, unavailable, success ────


class TestApiMemoryRecordHistory:
    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, env) -> None:
        resp = await memory_edit.api_memory_record_history(
            _request(
                "GET",
                "/api/memory/records/history",
                env.state(),
                owner=False,
                query={"store": _FINANCE},
            )
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_undeclared_store_is_a_404(self, env) -> None:
        resp = await memory_edit.api_memory_record_history(
            _request(
                "GET",
                "/api/memory/records/history",
                env.state(),
                query={"store": "nosuchstore"},
            )
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unavailable_tier_answers_503(self, env) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        resp = await memory_edit.api_memory_record_history(
            _request("GET", "/api/memory/records/history", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_malformed_limit_is_a_400(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_edit.api_memory_record_history(
            _request(
                "GET",
                "/api/memory/records/history",
                env.state(),
                query={"store": _FINANCE, "limit": "abc"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_memory_edit"

    @pytest.mark.asyncio
    async def test_malformed_offset_is_a_400(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_edit.api_memory_record_history(
            _request(
                "GET",
                "/api/memory/records/history",
                env.state(),
                query={"store": _FINANCE, "offset": "abc"},
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_memory_edit"

    @pytest.mark.asyncio
    async def test_sqlite_error_during_history_answers_store_unavailable(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        def _boom(*_a, **_k):
            raise sqlite3.Error("db went away")

        monkeypatch.setattr(memory_edit.memory_edit, "record_history", _boom)
        resp = await memory_edit.api_memory_record_history(
            _request("GET", "/api/memory/records/history", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_no_kind_or_id_is_a_400_invalid_identity(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_edit.api_memory_record_history(
            _request("GET", "/api/memory/records/history", env.state(), query={"store": _FINANCE})
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_memory_edit"

    @pytest.mark.asyncio
    async def test_a_written_record_has_a_history_entry(self, env) -> None:
        env.declare(_FINANCE)
        tier = env.tier(_FINANCE)
        tier.set_semantic("user.email", "owner@example.com", 1.0, "user_explicit")
        resp = await memory_edit.api_memory_record_history(
            _request(
                "GET",
                "/api/memory/records/history",
                env.state(),
                query={"store": _FINANCE, "kind": "fact", "id": "user.email"},
            )
        )
        assert resp.status == 200
        body = _body(resp)
        assert body["entries"]
        assert "has_more" in body
        assert "current_revision" in body

    @pytest.mark.asyncio
    async def test_an_unwritten_record_is_a_404(self, env) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        resp = await memory_edit.api_memory_record_history(
            _request(
                "GET",
                "/api/memory/records/history",
                env.state(),
                query={"store": _FINANCE, "kind": "fact", "id": "user.nosuchrecord"},
            )
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "memory_record_missing"


# ── _bulk (preview + apply): write-gate refusal, store unavailable, DB error ──


class TestBulkPreviewAndApply:
    @pytest.mark.asyncio
    async def test_write_gate_refusal_short_circuits_preview(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        async def _refuse(_state, _request, _operation):
            return web.json_response(
                {"error": "write blocked", "code": "write_blocked"}, status=403
            )

        monkeypatch.setattr(memory_edit, "_memory_write_gate", _refuse)
        resp = await memory_edit.api_memory_bulk_preview(
            _request(
                "POST",
                "/api/memory/bulk/preview",
                env.state(),
                body={
                    "store": _FINANCE,
                    "selection": {"query": {}},
                    "operation": {"type": "forget"},
                },
            )
        )
        assert resp.status == 403
        assert _body(resp)["code"] == "write_blocked"

    @pytest.mark.asyncio
    async def test_write_gate_refusal_short_circuits_apply(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        async def _refuse(_state, _request, _operation):
            return web.json_response(
                {"error": "write blocked", "code": "write_blocked"}, status=403
            )

        monkeypatch.setattr(memory_edit, "_memory_write_gate", _refuse)
        resp = await memory_edit.api_memory_bulk_apply(
            _request(
                "POST",
                "/api/memory/bulk/apply",
                env.state(),
                body={"store": _FINANCE, "preview_id": "does-not-matter"},
            )
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_unavailable_tier_answers_503_for_preview(self, env) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        resp = await memory_edit.api_memory_bulk_preview(
            _request(
                "POST",
                "/api/memory/bulk/preview",
                env.state(),
                body={
                    "store": _FINANCE,
                    "selection": {"query": {}},
                    "operation": {"type": "forget"},
                },
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_unavailable_tier_answers_503_for_apply(self, env) -> None:
        env.declare(_FINANCE)
        env.unavailable.add(_FINANCE)
        resp = await memory_edit.api_memory_bulk_apply(
            _request(
                "POST",
                "/api/memory/bulk/apply",
                env.state(),
                body={"store": _FINANCE, "preview_id": "x"},
            )
        )
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_sqlite_error_during_preview_answers_store_unavailable(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        def _boom(*_a, **_k):
            raise sqlite3.Error("db went away")

        monkeypatch.setattr(memory_edit.memory_edit, "preview_edit", _boom)
        resp = await memory_edit.api_memory_bulk_preview(
            _request(
                "POST",
                "/api/memory/bulk/preview",
                env.state(),
                body={
                    "store": _FINANCE,
                    "selection": {"query": {}},
                    "operation": {"type": "forget"},
                },
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_os_error_during_apply_answers_store_unavailable(self, env, monkeypatch) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)

        def _boom(*_a, **_k):
            raise OSError("disk gone")

        monkeypatch.setattr(memory_edit.memory_edit, "apply_edit", _boom)
        resp = await memory_edit.api_memory_bulk_apply(
            _request(
                "POST",
                "/api/memory/bulk/apply",
                env.state(),
                body={"store": _FINANCE, "preview_id": "x"},
            )
        )
        assert resp.status == 503
        assert _body(resp)["code"] == "store_unavailable"

    @pytest.mark.asyncio
    async def test_memory_edit_error_during_preview_is_a_400_and_never_audited(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        audited: list[Any] = []

        async def _audit(*args, **kwargs):
            audited.append((args, kwargs))

        def _boom(*_a, **_k):
            raise memory_edit.memory_edit.MemoryEditError("bad op")

        monkeypatch.setattr(memory_edit, "_audit", _audit)
        monkeypatch.setattr(memory_edit.memory_edit, "preview_edit", _boom)
        resp = await memory_edit.api_memory_bulk_preview(
            _request(
                "POST",
                "/api/memory/bulk/preview",
                env.state(),
                body={
                    "store": _FINANCE,
                    "selection": {"query": {}},
                    "operation": {"type": "forget"},
                },
            )
        )
        assert resp.status == 400
        assert audited == []

    @pytest.mark.asyncio
    async def test_successful_apply_audits_success_with_changed_count(
        self, env, monkeypatch
    ) -> None:
        env.declare(_FINANCE)
        env.tier(_FINANCE)
        audited: list[Any] = []

        async def _audit(_request, operation, outcome, resources):
            audited.append((operation, outcome, resources))

        monkeypatch.setattr(memory_edit, "_audit", _audit)
        monkeypatch.setattr(
            memory_edit.memory_edit,
            "apply_edit",
            lambda *_a, **_k: {"changed_count": 3},
        )
        resp = await memory_edit.api_memory_bulk_apply(
            _request(
                "POST",
                "/api/memory/bulk/apply",
                env.state(),
                body={"store": _FINANCE, "preview_id": "x"},
            )
        )
        assert resp.status == 200
        assert _body(resp)["changed_count"] == 3
        assert audited == [("memory.bulk.apply", "success", f"{_FINANCE}:3")]
