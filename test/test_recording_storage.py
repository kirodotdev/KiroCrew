"""Where a recording's files land, and who is allowed to decide that.

Two halves:

* ``/api/ws/recording``'s ``start`` frame now takes an optional ``meeting_id``.
  Core resolves it through whatever store an app registered -- it never builds a
  path itself -- and REFUSES to start when the id cannot be placed, rather than
  recording into the void.
* Meetings' adapter, which is what turns that id into a directory. Every path it
  returns has been through ``safe_meeting_id`` and ``contain``, so traversal and
  symlink escapes are rejected there rather than trusted here.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.recording_store import (
    MeetingsRecordingStore,
)
from kiro_crew.recording import recovery
from kiro_crew.recording.ws import _resolve_storage_dir, api_ws_recording


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/ws/recording", api_ws_recording)
    app["allowed_origins"] = {"http://localhost:5476"}
    return app


def _pcm(n_samples: int = 160, amplitude: int = 5000) -> bytes:
    return struct.pack(f"<{n_samples}h", *([amplitude] * n_samples))


@pytest.fixture(name="no_store", autouse=True)
def no_store_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no registered store.

    The registry is module-global and ``register_routes`` installs Meetings' adapter,
    so any earlier meetings test would otherwise leave one pointing at a deleted
    ``tmp_path``.
    """
    monkeypatch.setattr(recovery, "_store", None)


@pytest.fixture(name="patched_guards")
def patched_guards_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the origin guard and use a local STT provider (no duration cap)."""
    monkeypatch.setattr("kiro_crew.recording.ws.check_origin", lambda r, require: True)
    monkeypatch.setattr("kiro_crew.recording.ws.sel", lambda: MagicMock())
    fake_cfg = MagicMock()
    fake_cfg.stt.provider = "whisper"
    fake_cfg.recording.require_local_gateway = False
    monkeypatch.setattr(
        "kiro_crew.recording.ws.KiroCrewConfig.load",
        classmethod(lambda cls: fake_cfg),
    )


class _StubStore:
    """A minimal registered store: one directory, or ``None`` for anything else."""

    def __init__(self, allowed: str, target: Path) -> None:
        self._allowed = allowed
        self._target = target
        self.calls: list[str] = []

    def resolve_meeting_dir(self, meeting_id: str, root: Optional[Path] = None) -> Optional[Path]:
        self.calls.append(meeting_id)
        if meeting_id != self._allowed:
            return None
        self._target.mkdir(parents=True, exist_ok=True)
        return self._target

    # Unused by the socket; present so the object satisfies the protocol.
    def list_meetings(self, root: Optional[Path] = None) -> list[dict[str, Any]]:
        return []

    def get_meeting(self, meeting_id: str, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
        return None

    def update_meeting(
        self, meeting_id: str, patch: dict[str, Any], root: Optional[Path] = None
    ) -> Optional[dict[str, Any]]:
        return None


class TestResolveStorageDir:
    """The core-side helper. It must never raise, whatever the store does."""

    def test_returns_none_when_no_store_is_registered(self) -> None:
        # A gateway running without the owning app installed. The recording is
        # refused, not silently unpersisted.
        assert _resolve_storage_dir("meet-1") is None

    def test_delegates_to_the_registered_store(self, tmp_path: Path) -> None:
        stub = _StubStore("meet-1", tmp_path / "meet-1")
        recovery.register_meeting_store(stub)
        assert _resolve_storage_dir("meet-1") == tmp_path / "meet-1"
        assert stub.calls == ["meet-1"]

    def test_swallows_a_store_that_raises(self, tmp_path: Path) -> None:
        # Core must not let an app's failure take the socket down.
        broken = MagicMock()
        broken.resolve_meeting_dir.side_effect = RuntimeError("boom")
        recovery.register_meeting_store(broken)
        assert _resolve_storage_dir("meet-1") is None


class TestRecordingStorageWiring:
    """The ``start`` frame's ``meeting_id``, end to end over the socket."""

    @pytest.mark.asyncio
    async def test_audio_is_written_into_the_meeting_directory(
        self, patched_guards: None, tmp_path: Path
    ) -> None:
        target = tmp_path / "meetings" / "meet-1"
        recovery.register_meeting_store(_StubStore("meet-1", target))

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "meeting_id": "meet-1", "title": "Weekly"})
            assert (await ws.receive_json())["type"] == "ready"

            await ws.send_bytes(_pcm())
            assert (await ws.receive_json())["type"] == "level"

            await ws.send_json({"type": "stop"})
            # The server breaks out of its loop and closes; receiving that close is
            # what guarantees the finally block (and the WAV flush) has run.
            await ws.receive()

        audio = target / "audio.wav"
        assert audio.is_file()
        # A WAV header is 44 bytes; anything larger means samples actually landed.
        assert audio.stat().st_size > 44

    @pytest.mark.asyncio
    async def test_start_without_a_meeting_id_still_works(self, patched_guards: None) -> None:
        # Back-compat and the dictation case: a recording that persists nothing needs
        # no store and must not be refused.
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "title": "No meeting"})
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_json({"type": "stop"})
            await ws.close()

    @pytest.mark.asyncio
    async def test_refuses_a_meeting_id_the_store_rejects(
        self, patched_guards: None, tmp_path: Path
    ) -> None:
        # The whole point of refusing: a client that named a meeting expects a file at
        # the end of it, so starting anyway would produce a recording that silently
        # persisted nothing.
        recovery.register_meeting_store(_StubStore("meet-1", tmp_path / "meet-1"))

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "meeting_id": "../../etc"})
            resp = await ws.receive_json()
            assert resp["type"] == "error"
            assert resp["message"] == "recording storage unavailable"
            await ws.close()

    @pytest.mark.asyncio
    async def test_refuses_a_meeting_id_with_no_store_registered(
        self, patched_guards: None
    ) -> None:
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "meeting_id": "meet-1"})
            resp = await ws.receive_json()
            assert resp["type"] == "error"
            assert resp["message"] == "recording storage unavailable"
            await ws.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["", "   ", 17, [], {}])
    async def test_rejects_a_malformed_meeting_id(self, patched_guards: None, bad: object) -> None:
        # Absent means "persist nothing"; empty or wrongly-typed means the client is
        # broken, and the two must not be conflated.
        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "meeting_id": bad})
            resp = await ws.receive_json()
            assert resp["type"] == "error"
            assert resp["message"] == "meeting_id must be a non-empty string"
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_rejected_start_leaves_the_socket_reusable(
        self, patched_guards: None, tmp_path: Path
    ) -> None:
        # The concurrency cap is one session, so a refused start must not have
        # registered anything -- otherwise the corrected retry would be refused too.
        target = tmp_path / "meet-1"
        recovery.register_meeting_store(_StubStore("meet-1", target))

        async with TestClient(TestServer(_make_app())) as client:
            ws = await client.ws_connect("/api/ws/recording")
            await ws.send_json({"type": "start", "meeting_id": "nope"})
            assert (await ws.receive_json())["type"] == "error"

            await ws.send_json({"type": "start", "meeting_id": "meet-1"})
            assert (await ws.receive_json())["type"] == "ready"

            await ws.send_json({"type": "stop"})
            await ws.receive()


class TestMeetingsAdapter:
    """Meetings' side of the seam -- the only thing that turns an id into a path."""

    @pytest.fixture(name="root")
    def root_fixture(self, tmp_path: Path) -> Path:
        data = tmp_path / "meetings-data"
        store.ensure_data_dirs(data)
        return data

    def test_resolves_and_creates_a_meeting_directory(self, root: Path) -> None:
        adapter = MeetingsRecordingStore()
        resolved = adapter.resolve_meeting_dir("event-42", root)
        assert resolved is not None
        assert resolved.is_dir()
        assert resolved == store.meetings_root(root).resolve() / "event-42"

    @pytest.mark.parametrize(
        "bad",
        [
            "../escape",
            "../../etc/passwd",
            "a/b",
            ".hidden",
            "..",
            "",
            "sp ace",
        ],
    )
    def test_rejects_an_unsafe_id_as_none(self, root: Path, bad: str) -> None:
        # ``MeetingsPathError`` is this app's type; core must not have to know it, so
        # every rejection collapses to None at the boundary.
        assert MeetingsRecordingStore().resolve_meeting_dir(bad, root) is None

    def test_rewrites_a_colon_the_way_the_store_does(self, root: Path) -> None:
        # Calendar event ids routinely contain colons, which is the one documented
        # substitution. The adapter must not diverge from ``safe_meeting_id`` here.
        resolved = MeetingsRecordingStore().resolve_meeting_dir("cal:evt:1", root)
        assert resolved is not None
        assert resolved.name == "cal_evt_1"

    def test_nothing_escapes_the_data_root(self, root: Path) -> None:
        resolved = MeetingsRecordingStore().resolve_meeting_dir("event-42", root)
        assert resolved is not None
        assert resolved.is_relative_to(store.data_dir(root).resolve())

    def test_honours_the_app_data_root_override(self, root: Path) -> None:
        # How the test harness points the adapter at a tmp dir: the override is read
        # lazily off the Application, so registration order does not matter.
        app = web.Application()
        app["_meetings_data_root"] = root
        resolved = MeetingsRecordingStore(app).resolve_meeting_dir("event-42")
        assert resolved is not None
        assert resolved.is_relative_to(store.data_dir(root).resolve())

    def test_surfaces_event_id_as_id_for_core(self, root: Path) -> None:
        # The rename is the reason the adapter exists rather than passing ``store``
        # itself: this app calls the key ``event_id``, core reads ``id``.
        store.write_meeting_meta("event-42", store.new_meeting_meta("event-42", "Weekly"), root)
        adapter = MeetingsRecordingStore()

        listed = adapter.list_meetings(root)
        assert [m["id"] for m in listed] == ["event-42"]
        assert [m["event_id"] for m in listed] == ["event-42"]

        one = adapter.get_meeting("event-42", root)
        assert one is not None
        assert one["id"] == "event-42"
        assert one["title"] == "Weekly"

    def test_get_meeting_is_none_for_an_unknown_or_unsafe_id(self, root: Path) -> None:
        adapter = MeetingsRecordingStore()
        assert adapter.get_meeting("never-existed", root) is None
        assert adapter.get_meeting("../escape", root) is None

    def test_update_meeting_merges_and_persists(self, root: Path) -> None:
        store.write_meeting_meta("event-42", store.new_meeting_meta("event-42", "Weekly"), root)
        adapter = MeetingsRecordingStore()

        updated = adapter.update_meeting("event-42", {"status": "transcribing"}, root)
        assert updated is not None
        assert updated["status"] == "transcribing"
        # Merged, not replaced.
        assert updated["title"] == "Weekly"
        assert store.read_meeting_meta("event-42", root)["status"] == "transcribing"

    def test_update_meeting_is_none_for_an_unknown_id(self, root: Path) -> None:
        assert MeetingsRecordingStore().update_meeting("nope", {"status": "x"}, root) is None
