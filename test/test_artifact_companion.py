"""Tests for the artifact companion chat backend.

Covers WS-A of the companion-chat feature:

- ``artifact`` binding field on chat slots — create-time parse (valid slug
  accepted; bool/injection-shaped/non-string rejected), ``to_dict`` exposure.
- Persistence round-trip — the binding survives ``_save_slot_to_history`` →
  ``_rehydrate_slot_from_history`` so a bound session restored after a gateway
  restart is still the artifact's active bound session.
- ``artifact_update`` WS broadcast — emitted from the artifact mutation funnel
  (create / content PATCH / revert / delete) and NOT on metadata-only PATCH;
  typed WS envelope shape.
- Context injection endpoint accepts dashboard-origin calls on an
  artifact-bound (non-app) slot.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_delete,
    api_artifact_update,
    api_artifacts_create,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog


def _make_state(tmp_path, **kwargs):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state) -> web.Application:
    from kiro_crew.dashboard.chat import (
        api_chat_slot_context,
        api_chat_slot_create,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_post("/api/chat/slots/{slot}/context", api_chat_slot_context)
    return app


# ── Slot binding: create-time parse + to_dict ────────────────────────────────


class TestArtifactBindingField:
    def test_default_empty(self):
        slot = _ChatSlot("s1")
        assert slot._artifact == ""
        assert slot.to_dict()["artifact"] == ""

    def test_to_dict_exposes_binding(self):
        slot = _ChatSlot("s1")
        slot._artifact = "my-dashboard"
        assert slot.to_dict()["artifact"] == "my-dashboard"

    @pytest.mark.asyncio
    async def test_create_with_valid_artifact(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "companion", "artifact": "cr-queue"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["artifact"] == "cr-queue"
            assert state._slots["companion"]._artifact == "cr-queue"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            True,  # bool — must not coerce
            123,  # non-string
            "../../etc/passwd",  # injection-shaped
            "valid-slug\n",  # trailing-newline $-anchor bypass (review-bot, needs \Z)
            "UPPER-CASE",  # violates slug grammar
            "-leading-hyphen",
            "trailing-hyphen-",
            "a" * 81,  # too long
            "",  # empty
        ],
    )
    async def test_create_with_invalid_artifact_dropped(self, tmp_path, monkeypatch, bad):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "companion", "artifact": bad}
            )
            assert resp.status == 200  # invalid binding is dropped, slot still creates
            data = await resp.json()
            assert data["artifact"] == ""
            assert state._slots["companion"]._artifact == ""

    @pytest.mark.asyncio
    async def test_create_without_artifact_unbound(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": "plain"})
            assert resp.status == 200
            assert (await resp.json())["artifact"] == ""


# ── Persistence round-trip ───────────────────────────────────────────────────


class TestArtifactBindingPersistence:
    def test_binding_persisted_in_meta(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._artifact = "cr-queue"
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("artifact") == "cr-queue"

    def test_empty_binding_not_persisted(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert "artifact" not in meta

    def test_rehydrate_restores_binding(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._artifact = "cr-queue"
        slot.append("user", "hello")
        slot.drain()
        # closed=False: resumable session (a closed session is skipped by rehydrate)
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._artifact == "cr-queue"

    def test_bulk_restore_restores_binding(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import (
            _save_slot_to_history,
            restore_recent_sessions,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._artifact = "cr-queue"
        slot.append("user", "hello")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = restore_recent_sessions(state, window_minutes=0)
        assert restored >= 1
        assert state._slots["s1"]._artifact == "cr-queue"

    @pytest.mark.parametrize(
        "tampered",
        [
            "../../etc/passwd",  # injection-shaped
            "valid-slug\n",  # trailing-newline $-anchor bypass (review-bot, needs \Z)
            "<script>alert(1)</script>",  # markup injection
            "UPPER-CASE",  # violates slug grammar
            "a" * 81,  # too long
            12345,  # non-string
        ],
    )
    def test_tampered_meta_artifact_dropped_on_restore(self, tmp_path, monkeypatch, tampered):
        """History JSONL is attacker-tamperable with disk access — an invalid
        `artifact` value must be dropped on BOTH restore paths, never reaching
        to_dict()/WS broadcasts (review-bot security-controls, rev 3)."""
        from kiro_crew.dashboard.chat_persistence import (
            _rehydrate_slot_from_history,
            _save_slot_to_history,
            restore_recent_sessions,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        # Tamper the persisted meta line directly, as an attacker would.
        path = state.conversation_log._path("dashboard:s1")
        lines = path.read_text(encoding="utf-8").splitlines()
        meta = json.loads(lines[0])
        meta["artifact"] = tampered
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._artifact == ""
        assert restored.to_dict()["artifact"] == ""

        del state._slots["s1"]
        restore_recent_sessions(state, window_minutes=0)
        assert state._slots["s1"]._artifact == ""


# ── artifact_update broadcast: state helper + WS envelope ────────────────────


class TestPushArtifactUpdate:
    def test_helper_broadcasts_note(self, tmp_path):
        state = _make_state(tmp_path)
        captured: list[dict] = []
        state._broadcast = lambda note: captured.append(note)  # type: ignore[method-assign]

        state.push_artifact_update("cr-queue", 7)
        state.push_artifact_update("cr-queue", 7, deleted=True)

        assert captured[0] == {
            "_type": "artifact_update",
            "slug": "cr-queue",
            "version": 7,
            "deleted": False,
        }
        assert captured[1]["deleted"] is True

    @pytest.mark.asyncio
    async def test_ws_envelope_is_typed(self, tmp_path):
        """Async (per async-test-for-event-loop): _broadcast is unpatched
        production code whose _send_ws_all path routes through
        asyncio.ensure_future — a running event loop must exist even though
        the send itself is stubbed here."""
        state = _make_state(tmp_path)
        state._ws_clients = [MagicMock()]
        sent: list[tuple[str, object, str]] = []
        # New chokepoint signature (msg_type, data, msg): the payload is passed
        # alongside the serialized envelope so per-app WS scope filtering can
        # inspect it.
        state._send_ws_all = (  # type: ignore[method-assign]
            lambda msg_type, data, msg: sent.append((msg_type, data, msg))
        )

        state.push_artifact_update("cr-queue", 3)

        assert len(sent) == 1
        assert sent[0][0] == "artifact_update"
        assert sent[0][1] == {"slug": "cr-queue", "version": 3, "deleted": False}
        env = json.loads(sent[0][2])
        assert env == {
            "type": "artifact_update",
            "data": {"slug": "cr-queue", "version": 3, "deleted": False},
        }


# ── artifact_update broadcast: mutation-funnel emit points ───────────────────


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    return store


@pytest.fixture(autouse=True)
def _stub_restricted(monkeypatch):
    """Make _is_restricted_session read req.app['_restricted_session'].

    Same pattern as test_artifacts_handlers.py — the real check walks
    state._slots, which is a MagicMock in these handler-level tests.
    """
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    def _stub(state, req):
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


def _request(
    *,
    body: dict | None = None,
    match: dict | None = None,
    state: MagicMock | None = None,
) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = match or {}
    req.query = {}
    encoded = json.dumps(body).encode() if body is not None else b""
    req.read = AsyncMock(return_value=encoded)
    req.app = {"state": state if state is not None else MagicMock(), "_restricted_session": False}
    # request.get("app", "") — dashboard-origin (no app token)
    req.get = MagicMock(return_value="")
    return req


class TestMutationFunnelEmits:
    @pytest.mark.asyncio
    async def test_create_emits(self, isolated_store):
        state = MagicMock()
        req = _request(body={"name": "Widget", "content": "<div/>", "kind": "widget"}, state=state)
        resp = await api_artifacts_create(req)
        assert resp.status == 201
        state.push_artifact_update.assert_called_once()
        args, kwargs = state.push_artifact_update.call_args
        assert args[0] == "widget"
        assert kwargs.get("deleted", False) is False

    @pytest.mark.asyncio
    async def test_content_patch_emits(self, isolated_store):
        isolated_store.create(name="W", content="v1", slug="w", kind="text")
        state = MagicMock()
        req = _request(body={"content": "v2", "snapshot": True}, match={"slug": "w"}, state=state)
        resp = await api_artifact_update(req)
        assert resp.status == 200
        state.push_artifact_update.assert_called_once_with("w", 2, deleted=False)

    @pytest.mark.asyncio
    async def test_metadata_only_patch_does_not_emit(self, isolated_store):
        isolated_store.create(name="W", content="v1", slug="w", kind="text")
        state = MagicMock()
        req = _request(body={"name": "Renamed"}, match={"slug": "w"}, state=state)
        resp = await api_artifact_update(req)
        assert resp.status == 200
        state.push_artifact_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_revert_emits(self, isolated_store):
        isolated_store.create(name="W", content="v1", slug="w", kind="text")
        isolated_store.update("w", content="v2", snapshot=True)
        state = MagicMock()
        req = _request(
            body={"content": "v1", "event_type": "reverted", "from_version": 1},
            match={"slug": "w"},
            state=state,
        )
        resp = await api_artifact_update(req)
        assert resp.status == 200
        state.push_artifact_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_emits_deleted_variant(self, isolated_store):
        isolated_store.create(name="W", content="v1", slug="w", kind="text")
        state = MagicMock()
        req = _request(match={"slug": "w"}, state=state)
        resp = await api_artifact_delete(req)
        assert resp.status == 200
        state.push_artifact_update.assert_called_once_with("w", 1, deleted=True)

    @pytest.mark.asyncio
    async def test_emit_failure_does_not_fail_request(self, isolated_store):
        """Broadcast is fire-and-forget: a raising state must not 500 the mutation."""
        state = MagicMock()
        state.push_artifact_update.side_effect = RuntimeError("ws down")
        req = _request(body={"name": "W2", "content": "x", "kind": "text"}, state=state)
        resp = await api_artifacts_create(req)
        assert resp.status == 201


# ── Context injection on an artifact-bound (non-app) slot ────────────────────


class TestCompanionContextInjection:
    @pytest.mark.asyncio
    async def test_dashboard_context_on_bound_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "companion", "artifact": "cr-queue"}
            )
            assert resp.status == 200
            resp = await client.post(
                "/api/chat/slots/companion/context",
                json={
                    "content": "Companion chat for artifact `cr-queue`.",
                    "source": "artifact-companion",
                    "ephemeral": True,
                },
            )
            assert resp.status == 200
            slot = state._slots["companion"]
            assert len(slot._pending_context) == 1
