"""Coverage tests for ``kiro_crew.dashboard.handlers.memory``.

Focused on the request/response contract of the memory HTTP handlers: method
dispatch, body and query validation, the restricted-session 403 gate, the
not-found 404s, the single-flight 409s, and the fail-closed 500s. Handlers are
invoked directly with a faked ``web.Request`` (the style ``test_memory_graph.py``
uses) so no socket is bound; the vector store, memory store, embedder and
download manager are all mocks, so nothing here touches the network, a real
GGUF, or a sandbox.

``_apply_embedding_model`` is deliberately NOT exercised here — it is owned by
``test_embedding_model_apply.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.memory as mem_mod

_MOD = "kiro_crew.dashboard.handlers.memory"

_CRED = "AKIAIOSFODNN7EXAMPLE"


class _BadJSON:
    """Sentinel: make ``request.json()`` raise, as aiohttp does on bad bytes."""


def _body(resp: web.Response) -> Any:
    """Decode a ``web.json_response`` body."""
    return json.loads(resp.text or "")


def _make_state(
    *,
    vector_store: Any = None,
    memory: Any = None,
    consolidator: Any = None,
    restricted_keys: set[str] | None = None,
) -> Any:
    """A DashboardState stand-in wired for the memory handlers.

    ``_slots`` / ``_restricted_keys`` are real containers (not auto-MagicMocks)
    so ``_is_restricted_session`` runs its real logic instead of tripping over a
    truthy mock slot.
    """
    mem = memory if memory is not None else MagicMock()
    if vector_store is not None:
        mem.vector_store = vector_store
    state = MagicMock()
    state.context_builder = MagicMock(memory=mem)
    state.consolidator = consolidator
    state._slots = {}
    state._restricted_keys = set(restricted_keys or ())
    return state


def _make_request(
    state: Any,
    *,
    method: str = "GET",
    json_body: Any = None,
    query: dict[str, str] | None = None,
    match_info: dict[str, str] | None = None,
    session_key: str = "",
) -> Any:
    req = MagicMock()
    req.app = {"state": state}
    req.method = method
    req.query = query or {}
    req.match_info = match_info or {}
    req.headers = {"X-Session-Key": session_key} if session_key else {}
    if isinstance(json_body, _BadJSON):
        req.json = AsyncMock(side_effect=ValueError("not json"))
    else:
        req.json = AsyncMock(return_value=json_body)
    return req


def _store(**attrs: Any) -> Any:
    """A vector-store mock with JSON-serializable defaults."""
    store = MagicMock()
    store.embed_fn = None
    store.get_all_semantic.return_value = []
    store.get_events.return_value = []
    store.get_episodic_list.return_value = []
    store.search_episodic.return_value = []
    store.set_semantic.return_value = None
    store.delete_semantic.return_value = True
    store.delete_episodic.return_value = True
    store.memory_stats.return_value = {"semantic_count": 0}
    store.get_rejection_stats.return_value = {}
    store.get_context_preview.return_value = {"semantic": ""}
    store.get_semantic_context.return_value = ""
    store.get_episodic_context.return_value = ""
    store.import_memory.return_value = {"semantic": 0, "episodic": 0}
    store.migrate_from_markdown.return_value = {"semantic": 0, "episodic": 0}
    store.promote_episodic_patterns.return_value = 0
    for k, v in attrs.items():
        setattr(store, k, v)
    return store


@pytest.fixture(autouse=True)
def _fake_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write to the real security event log from a unit test."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())


@pytest.fixture(autouse=True)
def _reset_setup_status() -> Any:
    saved = dict(mem_mod._embedding_setup_status)
    mem_mod._embedding_setup_status = {"step": "idle", "error": ""}
    yield
    mem_mod._embedding_setup_status = saved


# ---------------------------------------------------------------------------
# preferences / projects / history — GET + PUT + malformed body
# ---------------------------------------------------------------------------


class TestPreferencesProjectsHistory:
    @pytest.mark.asyncio
    async def test_preferences_get_returns_stored_content(self) -> None:
        mem = MagicMock()
        mem.read_preferences.return_value = "- dark mode"
        state = _make_state(memory=mem)
        resp = await mem_mod.api_memory_preferences(_make_request(state))
        assert resp.status == 200
        assert _body(resp) == {"content": "- dark mode"}

    @pytest.mark.asyncio
    async def test_preferences_put_writes_content(self) -> None:
        mem = MagicMock()
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body={"content": "- new"})
        resp = await mem_mod.api_memory_preferences(req)
        assert _body(resp) == {"ok": True}
        mem.write_preferences.assert_called_once_with("- new")

    @pytest.mark.asyncio
    async def test_preferences_put_defaults_missing_content_to_empty(self) -> None:
        mem = MagicMock()
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body={})
        assert (await mem_mod.api_memory_preferences(req)).status == 200
        mem.write_preferences.assert_called_once_with("")

    @pytest.mark.asyncio
    async def test_preferences_put_rejects_invalid_json(self) -> None:
        mem = MagicMock()
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body=_BadJSON())
        resp = await mem_mod.api_memory_preferences(req)
        assert resp.status == 400
        assert _body(resp) == {"error": "invalid JSON"}
        mem.write_preferences.assert_not_called()

    @pytest.mark.asyncio
    async def test_projects_get_returns_stored_content(self) -> None:
        mem = MagicMock()
        mem.read_projects.return_value = "## Proj"
        state = _make_state(memory=mem)
        resp = await mem_mod.api_memory_projects(_make_request(state))
        assert _body(resp) == {"content": "## Proj"}

    @pytest.mark.asyncio
    async def test_projects_put_writes_content(self) -> None:
        mem = MagicMock()
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body={"content": "## New"})
        assert (await mem_mod.api_memory_projects(req)).status == 200
        mem.write_projects.assert_called_once_with("## New")

    @pytest.mark.asyncio
    async def test_projects_put_rejects_invalid_json(self) -> None:
        mem = MagicMock()
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body=_BadJSON())
        resp = await mem_mod.api_memory_projects(req)
        assert resp.status == 400
        mem.write_projects.assert_not_called()

    @pytest.mark.asyncio
    async def test_history_get_returns_recent(self) -> None:
        mem = MagicMock()
        mem.read_recent_history.return_value = "# 2026-01-01"
        state = _make_state(memory=mem)
        resp = await mem_mod.api_memory_history(_make_request(state))
        assert _body(resp) == {"content": "# 2026-01-01"}

    @pytest.mark.asyncio
    async def test_history_put_writes_today_file_creating_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "history" / "2026-01-01.md"
        mem = MagicMock()
        mem._today_history_file.return_value = target
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body={"content": "entry"})
        assert (await mem_mod.api_memory_history(req)).status == 200
        assert target.read_text(encoding="utf-8") == "entry"

    @pytest.mark.asyncio
    async def test_history_put_rejects_invalid_json(self, tmp_path: Path) -> None:
        mem = MagicMock()
        mem._today_history_file.return_value = tmp_path / "h.md"
        state = _make_state(memory=mem)
        req = _make_request(state, method="PUT", json_body=_BadJSON())
        resp = await mem_mod.api_memory_history(req)
        assert resp.status == 400
        assert not (tmp_path / "h.md").exists()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def _cfg(idle: float = 6.0, days: int = 30, migrated: bool = False) -> Any:
    cfg = MagicMock()
    cfg.memory.history_idle_hours = idle
    cfg.memory.history_max_days = days
    cfg.memory.migrated = migrated
    return cfg


class TestMemorySettings:
    @pytest.mark.asyncio
    async def test_get_reports_config_values(self) -> None:
        state = _make_state()
        with patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg(2.5, 14, True)):
            resp = await mem_mod.api_memory_settings(_make_request(state))
        assert _body(resp) == {
            "history_idle_hours": 2.5,
            "history_max_days": 14,
            "migrated": True,
        }

    @pytest.mark.asyncio
    async def test_put_rejects_invalid_json(self, tmp_path: Path) -> None:
        state = _make_state()
        req = _make_request(state, method="PUT", json_body=_BadJSON())
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=tmp_path / "config.json"),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_clamps_and_persists(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"agent": {"provider": "acp"}}), encoding="utf-8")
        state = _make_state()
        state.consolidator = None
        req = _make_request(
            state,
            method="PUT",
            json_body={"history_idle_hours": 0.1, "history_max_days": 1, "migrated": 1},
        )
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert _body(resp) == {"ok": True}
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        # Floors applied: 0.5 hours minimum, 7 days minimum.
        assert data["memory"]["history_idle_hours"] == 0.5
        assert data["memory"]["history_max_days"] == 7
        assert data["memory"]["migrated"] is True
        # Unrelated sections survive the read-modify-write.
        assert data["agent"]["provider"] == "acp"

    @pytest.mark.asyncio
    async def test_put_rejects_non_numeric_idle_hours(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        state = _make_state()
        req = _make_request(state, method="PUT", json_body={"history_idle_hours": "soon"})
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 400
        assert "history_idle_hours" in _body(resp)["error"]
        assert not cfg_path.exists()

    @pytest.mark.asyncio
    async def test_put_rejects_non_integer_max_days(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        state = _make_state()
        req = _make_request(state, method="PUT", json_body={"history_max_days": "many"})
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 400
        assert "history_max_days" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_put_fails_closed_on_unreadable_config(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{ not json", encoding="utf-8")
        state = _make_state()
        req = _make_request(state, method="PUT", json_body={"migrated": True})
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            resp = await mem_mod.api_memory_settings(req)
        assert resp.status == 500
        assert _body(resp)["code"] == "config_unreadable"
        # The user's file is left byte-for-byte intact.
        assert cfg_path.read_text(encoding="utf-8") == "{ not json"

    @pytest.mark.asyncio
    async def test_put_applies_to_running_consolidator(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        consolidator = MagicMock()
        state = _make_state(consolidator=consolidator)
        req = _make_request(state, method="PUT", json_body={"history_idle_hours": 3})
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg(idle=3.0, migrated=True)),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            assert (await mem_mod.api_memory_settings(req)).status == 200
        assert consolidator._history_idle_secs == 3.0 * 3600
        assert consolidator._migrated is True


# ---------------------------------------------------------------------------
# _redact_memory_field / _get_vector_store
# ---------------------------------------------------------------------------


class TestRedactAndStoreResolution:
    def test_bytes_are_dropped_not_serialized(self) -> None:
        assert mem_mod._redact_memory_field(b"\x00\x01") is None
        assert mem_mod._redact_memory_field(memoryview(b"ab")) is None

    def test_nested_containers_are_redacted(self) -> None:
        out = mem_mod._redact_memory_field({"a": [{"b": _CRED}], "n": 3})
        assert isinstance(out, dict)
        assert _CRED not in json.dumps(out)
        assert out["n"] == 3

    def test_non_string_scalars_pass_through(self) -> None:
        assert mem_mod._redact_memory_field(None) is None
        assert mem_mod._redact_memory_field(True) is True
        assert mem_mod._redact_memory_field(1.5) == 1.5

    def test_existing_vector_store_is_reused(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        assert mem_mod._get_vector_store(state) is store

    def test_standalone_store_created_and_cached_when_absent(self) -> None:
        mem = SimpleNamespace(vector_store=None)
        state: Any = SimpleNamespace(context_builder=SimpleNamespace(memory=mem))
        created = MagicMock()
        with (
            patch("kiro_crew.vector_memory.VectorMemoryStore", return_value=created) as ctor,
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
        ):
            first = mem_mod._get_vector_store(state)
            second = mem_mod._get_vector_store(state)
        assert first is created
        # Cached on state AND wired back onto the memory store, so only one build.
        assert second is created
        ctor.assert_called_once()
        created.init.assert_called_once()
        assert mem.vector_store is created


# ---------------------------------------------------------------------------
# semantic list / write / delete, events
# ---------------------------------------------------------------------------


class TestSemanticEndpoints:
    @pytest.mark.asyncio
    async def test_list_caps_limit_at_1000(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "99999", "offset": "7"})
        assert (await mem_mod.api_memory_semantic(req)).status == 200
        store.get_all_semantic.assert_called_once_with(limit=1000, offset=7)

    @pytest.mark.asyncio
    async def test_list_rejects_non_integer_paging(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "abc"})
        resp = await mem_mod.api_memory_semantic(req)
        assert resp.status == 400
        assert "integers" in _body(resp)["error"]
        store.get_all_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_drops_binary_columns(self) -> None:
        store = _store()
        store.get_all_semantic.return_value = [
            {"key": "k", "value_json": "v", "embedding": b"\x00\x01"}
        ]
        state = _make_state(vector_store=store)
        entry = _body(await mem_mod.api_memory_semantic(_make_request(state)))["entries"][0]
        assert "embedding" not in entry

    @pytest.mark.asyncio
    async def test_write_denied_for_restricted_session(self) -> None:
        store = _store()
        state = _make_state(vector_store=store, restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state,
            method="PUT",
            json_body={"key": "k", "value": "v"},
            session_key="dashboard:ghost",
        )
        resp = await mem_mod.api_memory_semantic_write(req)
        assert resp.status == 403
        store.set_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_rejects_invalid_json(self) -> None:
        state = _make_state(vector_store=_store())
        req = _make_request(state, method="PUT", json_body=_BadJSON())
        assert (await mem_mod.api_memory_semantic_write(req)).status == 400

    @pytest.mark.asyncio
    async def test_write_requires_key_and_value(self) -> None:
        state = _make_state(vector_store=_store())
        for body in ({"value": "v"}, {"key": "k"}, {}):
            req = _make_request(state, method="PUT", json_body=body)
            resp = await mem_mod.api_memory_semantic_write(req)
            assert resp.status == 400
            assert _body(resp)["error"] == "key and value required"

    @pytest.mark.asyncio
    async def test_write_success_forwards_confidence_and_source(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(
            state,
            method="PUT",
            json_body={"key": "k", "value": "v", "confidence": 0.5, "source": "agent"},
        )
        assert _body(await mem_mod.api_memory_semantic_write(req)) == {"ok": True}
        store.set_semantic.assert_called_once_with("k", "v", 0.5, "agent")

    @pytest.mark.asyncio
    async def test_write_non_numeric_confidence_falls_back_to_one(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(
            state, method="PUT", json_body={"key": "k", "value": "v", "confidence": "high"}
        )
        assert (await mem_mod.api_memory_semantic_write(req)).status == 200
        assert store.set_semantic.call_args[0][2] == 1.0

    @pytest.mark.asyncio
    async def test_write_conflict_reject_is_409(self) -> None:
        from kiro_crew.vector_memory import SemanticRejectCode

        store = _store()
        store.set_semantic.return_value = (SemanticRejectCode.CONFLICT, "already set")
        state = _make_state(vector_store=store)
        req = _make_request(state, method="PUT", json_body={"key": "k", "value": "v"})
        resp = await mem_mod.api_memory_semantic_write(req)
        assert resp.status == 409
        assert _body(resp)["error"] == "already set"

    @pytest.mark.asyncio
    async def test_write_other_reject_is_422_and_redacted(self) -> None:
        from kiro_crew.vector_memory import SemanticRejectCode

        store = _store()
        store.set_semantic.return_value = (
            SemanticRejectCode.VALUE_SIZE,
            f"value too large for {_CRED}",
        )
        state = _make_state(vector_store=store)
        req = _make_request(state, method="PUT", json_body={"key": "k", "value": "v"})
        resp = await mem_mod.api_memory_semantic_write(req)
        assert resp.status == 422
        assert _CRED not in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_delete_denied_for_restricted_session(self) -> None:
        store = _store()
        state = _make_state(vector_store=store, restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="DELETE", match_info={"key": "k"}, session_key="dashboard:ghost"
        )
        assert (await mem_mod.api_memory_semantic_delete(req)).status == 403
        store.delete_semantic.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_missing_key_is_404(self) -> None:
        store = _store(delete_semantic=MagicMock(return_value=False))
        state = _make_state(vector_store=store)
        req = _make_request(state, method="DELETE", match_info={"key": "nope"})
        resp = await mem_mod.api_memory_semantic_delete(req)
        assert resp.status == 404
        assert _body(resp) == {"error": "not found"}

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, method="DELETE", match_info={"key": "k"})
        assert _body(await mem_mod.api_memory_semantic_delete(req)) == {"ok": True}
        store.delete_semantic.assert_called_once_with("k", source="user_explicit")

    @pytest.mark.asyncio
    async def test_events_caps_limit_at_200(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "10000", "offset": "3"})
        assert (await mem_mod.api_memory_events(req)).status == 200
        store.get_events.assert_called_once_with(limit=200, offset=3)

    @pytest.mark.asyncio
    async def test_events_rejects_non_integer_paging(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"offset": "x"})
        assert (await mem_mod.api_memory_events(req)).status == 400
        store.get_events.assert_not_called()


# ---------------------------------------------------------------------------
# episodic endpoints
# ---------------------------------------------------------------------------


class TestEpisodicEndpoints:
    @pytest.mark.asyncio
    async def test_search_without_embed_fn_skips_embedding(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "hello", "tags": "a, b ,"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        store._try_embed.assert_not_called()
        kwargs = store.search_episodic.call_args.kwargs
        assert kwargs["query_embedding"] is None
        assert kwargs["tag_filter"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_search_embeds_query_when_embed_fn_present(self) -> None:
        store = _store(embed_fn=lambda t: [0.1])
        store._try_embed.return_value = [0.1]
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "hello"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        store._try_embed.assert_called_once_with("hello")
        assert store.search_episodic.call_args.kwargs["query_embedding"] == [0.1]

    @pytest.mark.asyncio
    async def test_search_bad_limit_falls_back_to_default(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "x", "limit": "lots"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        assert store.search_episodic.call_args.kwargs["limit"] == 20

    @pytest.mark.asyncio
    async def test_search_caps_limit_at_50_and_truncates_query(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "z" * 900, "limit": "500"})
        assert (await mem_mod.api_memory_episodic_search(req)).status == 200
        kwargs = store.search_episodic.call_args.kwargs
        assert kwargs["limit"] == 50
        assert len(kwargs["query_text"]) == 500

    @pytest.mark.asyncio
    async def test_search_results_drop_binary_and_redact_strings(self) -> None:
        store = _store()
        store.search_episodic.return_value = [
            {"id": "1", "text": f"saw {_CRED}", "embedding": b"\x00", "score": 0.5}
        ]
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "saw"})
        result = _body(await mem_mod.api_memory_episodic_search(req))["results"][0]
        assert "embedding" not in result
        assert _CRED not in result["text"]
        assert result["score"] == 0.5

    @pytest.mark.asyncio
    async def test_list_entries_are_redacted(self) -> None:
        store = _store()
        store.get_episodic_list.return_value = [{"id": "1", "text": f"key {_CRED}"}]
        state = _make_state(vector_store=store)
        entry = _body(await mem_mod.api_memory_episodic_list(_make_request(state)))["entries"][0]
        assert _CRED not in entry["text"]

    @pytest.mark.asyncio
    async def test_list_caps_limit_at_100(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "900", "offset": "2", "tags": "t1"})
        assert (await mem_mod.api_memory_episodic_list(req)).status == 200
        store.get_episodic_list.assert_called_once_with(limit=100, offset=2, tag_filter=["t1"])

    @pytest.mark.asyncio
    async def test_list_rejects_non_integer_paging(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"limit": "nope"})
        assert (await mem_mod.api_memory_episodic_list(req)).status == 400
        store.get_episodic_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_missing_id_is_404(self) -> None:
        store = _store(delete_episodic=MagicMock(return_value=False))
        state = _make_state(vector_store=store)
        req = _make_request(state, method="DELETE", match_info={"id": "42"})
        resp = await mem_mod.api_memory_episodic_delete(req)
        assert resp.status == 404
        assert _body(resp) == {"error": "not found"}

    @pytest.mark.asyncio
    async def test_delete_success(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, method="DELETE", match_info={"id": "42"})
        assert _body(await mem_mod.api_memory_episodic_delete(req)) == {"ok": True}
        store.delete_episodic.assert_called_once_with("42")


# ---------------------------------------------------------------------------
# stats / migrate / import / context-preview / observability / promote
# ---------------------------------------------------------------------------


class TestStatsMigrateImport:
    @pytest.mark.asyncio
    async def test_stats_merges_config_and_legacy_flags(self) -> None:
        store = _store()
        store.memory_stats.return_value = {"semantic_count": 4}
        state = _make_state(vector_store=store)
        cfg = _cfg(migrated=True)
        cfg.memory.embedding_provider = "llama_cpp"
        with (
            patch(f"{_MOD}.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.memory.legacy_memory_present", return_value=True),
        ):
            body = _body(await mem_mod.api_memory_stats(_make_request(state)))
        assert body == {
            "semantic_count": 4,
            "embedding_provider": "llama_cpp",
            "migrated": True,
            "has_legacy_memory": True,
        }

    @pytest.mark.asyncio
    async def test_migrate_restores_previous_embed_fn(self, tmp_path: Path) -> None:
        def _prev(text: str) -> list[float]:
            return [0.0]

        store = _store(embed_fn=_prev)
        state = _make_state(vector_store=store, consolidator=None)
        with (
            patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [1.0]),
            patch(f"{_MOD}.config_path", return_value=tmp_path / "config.json"),
        ):
            body = _body(await mem_mod.api_memory_migrate(_make_request(state)))
        assert body == {"semantic": 0, "episodic": 0}
        # Nothing migrated -> migrated flag untouched, embed_fn restored.
        assert store.embed_fn is _prev
        assert not (tmp_path / "config.json").exists()

    @pytest.mark.asyncio
    async def test_migrate_sets_migrated_when_entries_produced(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        store = _store()
        store.migrate_from_markdown.return_value = {"semantic": 3, "episodic": 0}
        consolidator = MagicMock()
        state = _make_state(vector_store=store, consolidator=consolidator)
        with (
            patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [1.0]),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
        ):
            body = _body(await mem_mod.api_memory_migrate(_make_request(state)))
        assert body["semantic"] == 3
        assert json.loads(cfg_path.read_text(encoding="utf-8"))["memory"]["migrated"] is True
        assert consolidator._migrated is True

    @pytest.mark.asyncio
    async def test_import_denied_for_restricted_session(self) -> None:
        store = _store()
        state = _make_state(vector_store=store, restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="POST", json_body={"semantic": []}, session_key="dashboard:ghost"
        )
        assert (await mem_mod.api_memory_import(req)).status == 403
        store.import_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_rejects_invalid_json(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", json_body=_BadJSON())
        assert (await mem_mod.api_memory_import(req)).status == 400
        store.import_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_returns_counts(self) -> None:
        store = _store()
        store.import_memory.return_value = {"semantic": 2, "episodic": 1}
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", json_body={"semantic": [{"key": "k"}]})
        assert _body(await mem_mod.api_memory_import(req)) == {"semantic": 2, "episodic": 1}


class TestContextPreviewAndObservability:
    @pytest.mark.asyncio
    async def test_preview_without_query_skips_episodic(self) -> None:
        store = _store()
        store.get_semantic_context.return_value = "line one"
        state = _make_state(vector_store=store)
        body = _body(await mem_mod.api_memory_context_preview(_make_request(state)))
        assert body == {"semantic_context": "line one", "episodic_context": ""}
        store.get_episodic_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_preview_filters_semantic_lines_by_query(self) -> None:
        store = _store()
        store.get_semantic_context.return_value = "[Semantic]\nprefers dark mode\nlikes tea"
        store.get_episodic_context.return_value = "episode"
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "dark"})
        body = _body(await mem_mod.api_memory_context_preview(req))
        assert "prefers dark mode" in body["semantic_context"]
        assert "likes tea" not in body["semantic_context"]
        assert body["episodic_context"] == "episode"

    @pytest.mark.asyncio
    async def test_preview_blanks_semantic_when_only_headers_match(self) -> None:
        store = _store()
        store.get_semantic_context.return_value = "[Semantic]\nlikes tea"
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "zzz-no-match"})
        body = _body(await mem_mod.api_memory_context_preview(req))
        assert body["semantic_context"] == ""

    @pytest.mark.asyncio
    async def test_observability_returns_stats_rejections_and_preview(self) -> None:
        store = _store()
        store.memory_stats.return_value = {"semantic_count": 1}
        store.get_rejection_stats.return_value = {"allowlist_reject": 2}
        store.get_context_preview.return_value = {"semantic": "s"}
        state = _make_state(vector_store=store)
        req = _make_request(state, query={"q": "q" * 900})
        body = _body(await mem_mod.api_memory_observability(req))
        assert body["stats"] == {"semantic_count": 1}
        assert body["rejections"] == {"allowlist_reject": 2}
        assert body["context_preview"] == {"semantic": "s"}
        assert len(store.get_context_preview.call_args.kwargs["query_text"]) == 500


class TestPromote:
    @pytest.mark.asyncio
    async def test_invalid_json_body_uses_defaults(self) -> None:
        store = _store(promote_episodic_patterns=MagicMock(return_value=2))
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", json_body=_BadJSON())
        assert _body(await mem_mod.api_memory_promote(req)) == {"ok": True, "promoted": 2}
        store.promote_episodic_patterns.assert_called_once_with(5, 0.75)

    @pytest.mark.asyncio
    async def test_explicit_thresholds_are_forwarded(self) -> None:
        store = _store(promote_episodic_patterns=MagicMock(return_value=0))
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", json_body={"min_count": 3, "min_sim": 0.9})
        assert (await mem_mod.api_memory_promote(req)).status == 200
        store.promote_episodic_patterns.assert_called_once_with(3, 0.9)

    @pytest.mark.asyncio
    async def test_non_numeric_thresholds_are_400(self) -> None:
        store = _store()
        state = _make_state(vector_store=store)
        req = _make_request(state, method="POST", json_body={"min_count": "five"})
        resp = await mem_mod.api_memory_promote(req)
        assert resp.status == 400
        assert "min_count/min_sim" in _body(resp)["error"]
        store.promote_episodic_patterns.assert_not_called()


# ---------------------------------------------------------------------------
# consolidate
# ---------------------------------------------------------------------------


def _consolidator() -> Any:
    c = MagicMock()
    c._running = set()
    c._tasks = set()
    c._consolidate = AsyncMock(return_value=None)
    return c


class TestConsolidate:
    @pytest.mark.asyncio
    async def test_denied_for_restricted_session(self) -> None:
        state = _make_state(consolidator=_consolidator(), restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="POST", json_body={"key": "s1"}, session_key="dashboard:ghost"
        )
        assert (await mem_mod.api_memory_consolidate(req)).status == 403

    @pytest.mark.asyncio
    async def test_503_without_consolidator(self) -> None:
        state = _make_state(consolidator=None)
        req = _make_request(state, method="POST", json_body={"key": "s1"})
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 503
        assert _body(resp) == {"error": "consolidator not available"}

    @pytest.mark.asyncio
    async def test_rejects_invalid_json(self) -> None:
        state = _make_state(consolidator=_consolidator())
        req = _make_request(state, method="POST", json_body=_BadJSON())
        assert (await mem_mod.api_memory_consolidate(req)).status == 400

    @pytest.mark.asyncio
    async def test_requires_non_blank_session_key(self) -> None:
        state = _make_state(consolidator=_consolidator())
        req = _make_request(state, method="POST", json_body={"key": "   "})
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 400
        assert _body(resp) == {"error": "session key required"}

    @pytest.mark.asyncio
    async def test_already_running_is_409(self) -> None:
        cons = _consolidator()
        cons._running.add("s1")
        state = _make_state(consolidator=cons)
        req = _make_request(state, method="POST", json_body={"key": "s1"})
        resp = await mem_mod.api_memory_consolidate(req)
        assert resp.status == 409
        cons._consolidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedules_background_consolidation(self) -> None:
        cons = _consolidator()
        state = _make_state(consolidator=cons)
        req = _make_request(state, method="POST", json_body={"key": "s1", "include_history": False})
        assert _body(await mem_mod.api_memory_consolidate(req)) == {"ok": True, "key": "s1"}
        assert "s1" in cons._running
        # Drain the spawned task so nothing is left pending at loop teardown.
        for task in list(cons._tasks):
            await task
        cons._consolidate.assert_awaited_once_with("s1", False)


# ---------------------------------------------------------------------------
# embedding-model apply endpoint (request boundary only)
# ---------------------------------------------------------------------------


def _prog(active: bool = False) -> Any:
    prog = MagicMock()
    prog.is_active.return_value = active
    prog.snapshot.return_value = {"state": "idle"}
    return prog


class TestEmbeddingModelEndpoint:
    @pytest.fixture(autouse=True)
    def _no_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_EMBED_MODEL_PATH", raising=False)

    @pytest.mark.asyncio
    async def test_denied_for_restricted_session(self) -> None:
        state = _make_state(restricted_keys={"dashboard:ghost"})
        req = _make_request(
            state, method="POST", json_body={"path": ""}, session_key="dashboard:ghost"
        )
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "restricted_session"

    @pytest.mark.asyncio
    async def test_rejects_unparseable_body(self) -> None:
        state = _make_state()
        req = _make_request(state, method="POST", json_body=_BadJSON())
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_rejects_valid_json_that_is_not_an_object(self) -> None:
        state = _make_state()
        payloads: list[Any] = [[], "str", 5]
        for payload in payloads:
            req = _make_request(state, method="POST", json_body=payload)
            resp = await mem_mod.api_memory_embedding_model(req)
            assert resp.status == 400
            assert _body(resp)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_validation_error_is_400_with_code(self) -> None:
        state = _make_state()
        req = _make_request(state, method="POST", json_body={"path": "/nope.gguf"})
        with patch(
            f"{_MOD}.validate_custom_model_path",
            return_value=(Path("/nope.gguf"), "not a file", "not_found"),
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 400
        assert _body(resp) == {"ok": False, "error": "not a file", "code": "not_found"}

    @pytest.mark.asyncio
    async def test_validate_only_reports_size_without_applying(self, tmp_path: Path) -> None:
        model = tmp_path / "m.gguf"
        model.write_bytes(b"abcd")
        state = _make_state()
        req = _make_request(
            state, method="POST", json_body={"path": str(model), "validate_only": True}
        )
        with (
            patch(f"{_MOD}.validate_custom_model_path", return_value=(model, "", "")),
            patch(f"{_MOD}._apply_embedding_model") as apply_mock,
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert _body(resp) == {"ok": True, "size_bytes": 4}
        apply_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_only_tolerates_unstatable_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.gguf"
        state = _make_state()
        req = _make_request(
            state, method="POST", json_body={"path": str(missing), "validate_only": True}
        )
        with patch(f"{_MOD}.validate_custom_model_path", return_value=(missing, "", "")):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert _body(resp) == {"ok": True, "size_bytes": 0}

    @pytest.mark.asyncio
    async def test_env_override_blocks_config_write_with_409(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_EMBED_MODEL_PATH", "/env/model.gguf")
        state = _make_state()
        req = _make_request(state, method="POST", json_body={"path": ""})
        resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 409
        assert _body(resp)["code"] == "env_override_active"

    @pytest.mark.asyncio
    async def test_apply_in_progress_is_409(self) -> None:
        state = _make_state()
        req = _make_request(state, method="POST", json_body={"path": ""})
        with patch(f"{_MOD}.reembed_progress", return_value=_prog(active=True)):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 409
        assert _body(resp)["code"] == "model_change_in_progress"

    @pytest.mark.asyncio
    async def test_unavailable_vector_store_is_503_and_arms_nothing(self) -> None:
        state = _make_state()
        prog = _prog()
        req = _make_request(state, method="POST", json_body={"path": ""})
        with (
            patch(f"{_MOD}.reembed_progress", return_value=prog),
            patch(f"{_MOD}._get_vector_store", side_effect=RuntimeError("db gone")),
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
        assert resp.status == 503
        assert _body(resp)["code"] == "vector_store_unavailable"
        prog.begin_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_revert_to_bundled_dispatches_worker(self) -> None:
        state = _make_state(vector_store=_store())
        prog = _prog()
        req = _make_request(state, method="POST", json_body={"path": ""})
        with (
            patch(f"{_MOD}.reembed_progress", return_value=prog),
            patch(f"{_MOD}._apply_embedding_model") as apply_mock,
        ):
            resp = await mem_mod.api_memory_embedding_model(req)
            assert _body(resp) == {"ok": True, "size_bytes": 0, "status": "applying"}
            prog.begin_apply.assert_called_once()
            await asyncio.wrap_future(state._embed_model_apply_task)
        apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# embedding-status custom-model branch
# ---------------------------------------------------------------------------


class TestEmbeddingStatusCustomModel:
    def _patches(self, custom: Any, model_present: bool) -> Any:
        embedder = MagicMock(model_id="custom.gguf", dim=768)
        embedder.is_ready.return_value = True
        mgr = MagicMock()
        mgr.status = {"step": "idle", "error": "", "attempt": 0}
        return (
            patch(f"{_MOD}.get_shared_embedder", return_value=embedder),
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=model_present),
            patch(f"{_MOD}.resolve_custom_model", return_value=custom),
        )

    @pytest.mark.asyncio
    async def test_healthy_custom_model_reports_done_and_no_retry(self) -> None:
        model_path = Path("/models/custom.gguf")
        custom = SimpleNamespace(error="", path=model_path)
        a, b, c, d = self._patches(custom, model_present=True)
        with a, b, c, d:
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        assert body["model_source"] == "custom"
        # Compare against str(Path) rather than the POSIX literal: the property
        # is "the handler surfaces the resolved path", not which separator the
        # host uses. Windows renders this as \models\custom.gguf.
        assert body["model_path"] == str(model_path)
        assert body["setup_step"] == "done"
        assert body["setup_error"] == ""
        assert body["can_retry"] is False
        assert body["model_dim"] == 768

    @pytest.mark.asyncio
    async def test_broken_custom_model_reports_error_without_retry(self) -> None:
        custom = SimpleNamespace(error="unreadable", path=Path("/models/custom.gguf"))
        a, b, c, d = self._patches(custom, model_present=False)
        with a, b, c, d:
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        assert body["setup_step"] == "error"
        assert body["setup_error"] == "unreadable"
        assert body["can_retry"] is False

    @pytest.mark.asyncio
    async def test_missing_custom_file_reports_error_naming_the_path(self) -> None:
        model_path = Path("/models/custom.gguf")
        custom = SimpleNamespace(error="", path=model_path)
        a, b, c, d = self._patches(custom, model_present=False)
        with a, b, c, d:
            body = _body(await mem_mod.api_memory_embedding_status(_make_request(_make_state())))
        assert body["setup_step"] == "error"
        # str(Path), not the POSIX literal -- see the note above.
        assert str(model_path) in body["setup_error"]


# ---------------------------------------------------------------------------
# _write_embed_model_config
# ---------------------------------------------------------------------------


class TestWriteEmbedModelConfig:
    @pytest.mark.asyncio
    async def test_writes_path_and_dim_preserving_other_sections(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"agent": {"provider": "acp"}, "memory": {"embed_model_id": "old"}}),
            encoding="utf-8",
        )
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._write_embed_model_config("/m/new.gguf", 1024)
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["memory"]["embed_model_path"] == "/m/new.gguf"
        assert data["memory"]["embedding_dim"] == 1024
        # The pinned id must be dropped so it is re-derived from the new file.
        assert "embed_model_id" not in data["memory"]
        assert data["agent"]["provider"] == "acp"

    @pytest.mark.asyncio
    async def test_empty_path_reverts_by_removing_the_key(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"memory": {"embed_model_path": "/m/old.gguf"}}), encoding="utf-8"
        )
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._write_embed_model_config("", 0)
        memory = json.loads(cfg_path.read_text(encoding="utf-8"))["memory"]
        assert "embed_model_path" not in memory
        # dim <= 0 means "unknown width" and must not be persisted.
        assert "embedding_dim" not in memory

    @pytest.mark.asyncio
    async def test_missing_config_is_created(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            await mem_mod._write_embed_model_config("/m/new.gguf", 512)
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["memory"]["embedding_dim"] == 512

    @pytest.mark.asyncio
    async def test_unparseable_config_raises_and_is_left_intact(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{ broken", encoding="utf-8")
        with patch(f"{_MOD}.config_path", return_value=cfg_path):
            with pytest.raises(ValueError, match="could not be parsed"):
                await mem_mod._write_embed_model_config("/m/new.gguf", 512)
        assert cfg_path.read_text(encoding="utf-8") == "{ broken"


# ---------------------------------------------------------------------------
# _ensure_pip_available — sandbox refusal + timeout
# ---------------------------------------------------------------------------


class TestEnsurePipEdgeCases:
    @pytest.mark.asyncio
    async def test_sandbox_unavailable_is_a_normal_not_ok_result(self) -> None:
        from kiro_crew.sandbox import SandboxUnavailableError

        def _refuse(argv: Any, **kw: Any) -> Any:
            raise SandboxUnavailableError("no backend", kind="no_backend", detail="not Linux")

        with (
            patch.dict("sys.modules", {"pip": None}),
            patch(f"{_MOD}.wrap_argv", side_effect=_refuse),
            patch(f"{_MOD}.create_subprocess_limited") as spawn,
        ):
            ok, err = await mem_mod._ensure_pip_available()
        assert ok is False
        assert "sandbox" in err
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_reaps_the_child_and_reports_not_ok(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        async def _timeout(coro: Any, *, timeout: Any = None) -> Any:
            coro.close()
            raise asyncio.TimeoutError

        with (
            patch.dict("sys.modules", {"pip": None}),
            patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, None)),
            patch(f"{_MOD}.create_subprocess_limited", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", side_effect=_timeout),
        ):
            ok, err = await mem_mod._ensure_pip_available()
        assert ok is False
        assert "timed out" in err
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_temp_wrapper_is_unlinked_even_when_already_gone(self, tmp_path: Path) -> None:
        """The cleanup path tolerates an OSError from a vanished wrapper file."""
        ghost = tmp_path / "wrapper.sh"  # never created
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            patch.dict("sys.modules", {"pip": None}),
            patch(f"{_MOD}.wrap_argv", side_effect=lambda argv, **kw: (argv, str(ghost))),
            patch(f"{_MOD}.create_subprocess_limited", AsyncMock(return_value=proc)),
        ):
            ok, err = await mem_mod._ensure_pip_available()
        assert (ok, err) == (True, "")


# ---------------------------------------------------------------------------
# enable / disable embeddings — guards and the download done-callback
# ---------------------------------------------------------------------------


def _dl_mgr(step: str = "idle", ensure: Any = None) -> Any:
    mgr = MagicMock()
    mgr.status = {"step": step, "error": "download failed", "attempt": 0}
    mgr.ensure_model = ensure if ensure is not None else AsyncMock(return_value=True)
    return mgr


async def _drain(state: Any) -> None:
    """Let the retained background download task finish its callbacks."""
    tasks = list(state.__dict__.get("_bg_embed_tasks", set()))
    for task in tasks:
        try:
            await task
        except BaseException:
            pass
    await asyncio.sleep(0)


class TestEnableDisableEmbeddings:
    @pytest.mark.asyncio
    async def test_disable_is_a_deliberate_410(self) -> None:
        resp = await mem_mod.api_memory_disable_embeddings(_make_request(_make_state()))
        assert resp.status == 410
        assert "always-on" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_concurrent_setup_is_409(self) -> None:
        mem_mod._embedding_setup_status = {"step": "installing_faiss", "error": ""}
        with patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr()):
            resp = await mem_mod.api_memory_enable_embeddings(
                _make_request(_make_state(), method="POST")
            )
        assert resp.status == 409
        assert "installing_faiss" in _body(resp)["error"]

    @pytest.mark.asyncio
    async def test_previous_error_is_cleared_before_retry(self) -> None:
        mem_mod._embedding_setup_status = {"step": "error", "error": "boom"}
        state = _make_state()
        with (
            patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr()),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            assert resp.status == 200
            await _drain(state)
        assert mem_mod._embedding_setup_status == {"step": "idle", "error": ""}

    @pytest.mark.asyncio
    async def test_download_failure_surfaces_as_failed_status(self) -> None:
        state = _make_state()
        mgr = _dl_mgr(ensure=AsyncMock(return_value=False))
        with (
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            assert _body(resp) == {"ok": True, "status": "downloading"}
            await _drain(state)
        assert mem_mod._embedding_setup_status["step"] == "failed"
        assert mem_mod._embedding_setup_status["error"] == "download failed"

    @pytest.mark.asyncio
    async def test_download_exception_surfaces_as_failed_status(self) -> None:
        state = _make_state()
        mgr = _dl_mgr(ensure=AsyncMock(side_effect=RuntimeError("no disk space")))
        with (
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            assert (
                await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            ).status == 200
            await _drain(state)
        assert mem_mod._embedding_setup_status["step"] == "failed"
        assert "no disk space" in str(mem_mod._embedding_setup_status["error"])

    @pytest.mark.asyncio
    async def test_download_cancellation_surfaces_as_failed_status(self) -> None:
        state = _make_state()
        started = asyncio.Event()

        async def _never_finishes(**_kw: Any) -> bool:
            started.set()
            await asyncio.Event().wait()
            return True

        mgr = _dl_mgr(ensure=_never_finishes)
        with (
            patch(f"{_MOD}.model_download_manager", return_value=mgr),
            patch(f"{_MOD}.model_file_present", return_value=False),
        ):
            assert (
                await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
            ).status == 200
            await started.wait()
            for task in list(state.__dict__.get("_bg_embed_tasks", set())):
                task.cancel()
            await _drain(state)
        assert mem_mod._embedding_setup_status == {"step": "failed", "error": "cancelled"}

    @pytest.mark.asyncio
    async def test_unexpected_error_in_download_kick_is_500(self) -> None:
        state = _make_state()
        with (
            patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr()),
            patch(f"{_MOD}.model_file_present", side_effect=RuntimeError("stat exploded")),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
        assert resp.status == 500
        assert "Click Enable to retry" in _body(resp)["error"]
        assert mem_mod._embedding_setup_status["step"] == "idle"

    @pytest.mark.asyncio
    async def test_config_unreadable_after_setup_is_500(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{ broken", encoding="utf-8")
        store = _store()
        state = _make_state(vector_store=store)
        with (
            patch(f"{_MOD}.model_download_manager", return_value=_dl_mgr("ready")),
            patch(f"{_MOD}.model_file_present", return_value=True),
            patch(f"{_MOD}.config_path", return_value=cfg_path),
            patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [0.0]),
            patch.dict("sys.modules", {"faiss": MagicMock(), "pip": MagicMock()}),
            patch(f"{_MOD}._get_vector_store", return_value=store),
        ):
            resp = await mem_mod.api_memory_enable_embeddings(_make_request(state, method="POST"))
        assert resp.status == 500
        assert _body(resp)["code"] == "config_unreadable"
        assert mem_mod._embedding_setup_status["step"] == "error"
        assert cfg_path.read_text(encoding="utf-8") == "{ broken"


# ---------------------------------------------------------------------------
# _apply_embedding_model — the worker's rollback protocol, executed
# ---------------------------------------------------------------------------


def _apply_store(previous_dim: int = 512, retargeted: bool = True) -> Any:
    store = MagicMock()
    store._embedding_dim = previous_dim
    store.set_embedding_dim.return_value = retargeted
    store.recorded_embedding_space.return_value = "sig"
    store.backfill_missing_embeddings.return_value = 7
    return store


class _ApplyHarness:
    """Patched module surface for one ``_apply_embedding_model`` run."""

    def __init__(
        self,
        *,
        ready: bool = True,
        has_wait_ready: bool = True,
        recorded: str = "sig",
        validate: tuple[Path, str, str] = (Path("/m/new.gguf"), "", ""),
        write_error: Exception | None = None,
        reconcile_error: Exception | None = None,
        serving: bool = False,
    ) -> None:
        self.prog = MagicMock()
        self.embedder = MagicMock(model_id="m", dim=1024)
        self.embedder.is_ready.return_value = ready
        if has_wait_ready:
            self.embedder.wait_ready = MagicMock(return_value=ready)
        else:
            del self.embedder.wait_ready
        self.reset = MagicMock()
        self.activate = MagicMock()
        self.install = MagicMock()
        self.candidate = MagicMock()
        self.bundled = MagicMock()
        self.write = AsyncMock(side_effect=write_error)
        self._recorded = recorded
        self._validate = validate
        self._reconcile_error = reconcile_error
        self._serving = serving
        self._stack: Any = None

    def __enter__(self) -> "_ApplyHarness":
        from contextlib import ExitStack

        self._stack = ExitStack()
        enter = self._stack.enter_context
        enter(patch(f"{_MOD}.reembed_progress", return_value=self.prog))
        enter(patch(f"{_MOD}.validate_custom_model_path", return_value=self._validate))
        enter(patch(f"{_MOD}.install_shared_embedder", self.install))
        enter(patch(f"{_MOD}.build_gated_candidate", return_value=self.candidate))
        enter(patch(f"{_MOD}.build_gated_bundled", return_value=self.bundled))
        enter(patch(f"{_MOD}.get_shared_embedder", return_value=self.embedder))
        enter(patch(f"{_MOD}.reset_shared_embedder", self.reset))
        enter(patch(f"{_MOD}.activate_shared_embedder", self.activate))
        enter(patch(f"{_MOD}.make_sync_embed_fn", return_value=lambda t: [0.0]))
        enter(
            patch(
                f"{_MOD}.reconcile_store_embedding_space",
                side_effect=self._reconcile_error,
            )
        )
        enter(patch(f"{_MOD}.active_embedding_space_signature", return_value="sig"))
        enter(patch(f"{_MOD}.embedding_backend_serving", return_value=self._serving))
        enter(patch(f"{_MOD}._write_embed_model_config", self.write))
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stack.close()


async def _run_apply(store: Any, raw: str) -> None:
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(mem_mod._apply_embedding_model, store, raw, loop)


class TestApplyEmbeddingModelWorker:
    @pytest.mark.asyncio
    async def test_revalidation_failure_installs_nothing(self) -> None:
        store = _apply_store()
        with _ApplyHarness(validate=(Path("/x"), "sensitive path", "denied")) as h:
            await _run_apply(store, "/x")
        h.prog.fail.assert_called_once_with("sensitive path")
        h.install.assert_not_called()
        store.begin_space_change.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_path_installs_a_gated_candidate(self) -> None:
        store = _apply_store()
        with _ApplyHarness() as h:
            await _run_apply(store, "/m/new.gguf")
        h.install.assert_called_once_with(h.candidate)
        store.begin_space_change.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_path_reverts_through_the_bundled_candidate(self) -> None:
        store = _apply_store()
        with _ApplyHarness() as h:
            await _run_apply(store, "")
        h.install.assert_called_once_with(h.bundled)
        h.activate.assert_called_once()
        h.prog.finish.assert_called_once_with(7)
        store.backfill_missing_embeddings.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_failure_drops_the_candidate_and_fails(self) -> None:
        store = _apply_store()
        with _ApplyHarness(ready=False) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        assert "did not load" in h.prog.fail.call_args[0][0]
        h.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedder_without_wait_ready_falls_back_to_is_ready(self) -> None:
        store = _apply_store()
        with _ApplyHarness(has_wait_ready=False) as h:
            await _run_apply(store, "")
        h.embedder.is_ready.assert_called_once()
        h.activate.assert_called_once()

    @pytest.mark.asyncio
    async def test_unreconciled_space_rolls_back_and_restores_the_width(self) -> None:
        store = _apply_store(previous_dim=512)
        store.recorded_embedding_space.return_value = "stale-sig"
        with _ApplyHarness() as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        # Width restored to the model being restored, not left on the new one.
        assert store.set_embedding_dim.call_args_list[-1][0][0] == 512
        assert "could not be removed" in h.prog.fail.call_args[0][0]
        h.write.assert_not_called()
        h.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unwritable_config_rolls_back_before_activation(self) -> None:
        store = _apply_store(previous_dim=512)
        with _ApplyHarness(write_error=ValueError("config.json could not be parsed")) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        assert store.set_embedding_dim.call_args_list[-1][0][0] == 512
        h.prog.fail.assert_called_once_with("config.json could not be parsed")
        h.activate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_failure_before_activation_rolls_back(self) -> None:
        store = _apply_store(previous_dim=512)
        with _ApplyHarness(reconcile_error=RuntimeError("disk full")) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        assert store.set_embedding_dim.call_args_list[-1][0][0] == 512
        h.prog.fail.assert_called_once_with("disk full")

    @pytest.mark.asyncio
    async def test_blank_exception_message_falls_back_to_the_class_name(self) -> None:
        store = _apply_store()
        with _ApplyHarness(reconcile_error=RuntimeError()) as h:
            await _run_apply(store, "")
        h.prog.fail.assert_called_once_with("RuntimeError")

    @pytest.mark.asyncio
    async def test_failure_after_activation_keeps_the_serving_model(self) -> None:
        """Once the backend is serving, a later failure must not drop it."""
        store = _apply_store()
        store.backfill_missing_embeddings.side_effect = RuntimeError("backfill died")
        with _ApplyHarness(serving=True) as h:
            await _run_apply(store, "")
        h.activate.assert_called_once()
        h.reset.assert_not_called()
        h.prog.fail.assert_called_once_with("backfill died")

    @pytest.mark.asyncio
    async def test_no_retarget_means_no_width_restore(self) -> None:
        store = _apply_store(previous_dim=1024, retargeted=False)
        with _ApplyHarness(reconcile_error=RuntimeError("boom")) as h:
            await _run_apply(store, "")
        h.reset.assert_called_once()
        # set_embedding_dim was called once (the retarget attempt), never again.
        store.set_embedding_dim.assert_called_once_with(1024)
        assert h.prog.fail.call_args[0][0] == "boom"


# ---------------------------------------------------------------------------
# graph error path
# ---------------------------------------------------------------------------


class TestMemoryGraphFailure:
    @pytest.mark.asyncio
    async def test_builder_failure_is_reported_as_500(self) -> None:
        state = _make_state()
        state.lessons.load_all.side_effect = RuntimeError("lesson store gone")
        resp = await mem_mod.api_memory_graph(_make_request(state))
        assert resp.status == 500
        assert _body(resp) == {"error": "failed to build memory graph"}
