"""Post-meeting batch transcription: the recording upload endpoint, the
``store.recording_path`` containment barrier, and the stop-hook transcription task.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
by convention; ``setup.cfg`` sets ``testpaths = test src/kiro_crew/apps/builtins``,
so both trees are collected, and the recording suite is kept alongside the other
``test_meetings_*`` modules here.

``transcribe_audio`` is always mocked; no test invokes real whisper or ffmpeg.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401
    app_fixture,
    client_for,
    enabled_fixture,
    fake_sessions_fixture,
    make_app,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as lifecycle

BASE = k.API_BASE


# ── store.recording_path containment ─────────────────────────────────────────


class TestRecordingPath:
    def test_inside_root(self, root: Path):
        path = store.recording_path("standup", root)
        assert path.is_relative_to(root.resolve())
        assert path.name == k.RECORDING_FILE
        assert path.parent.name == "standup"

    def test_colon_id_normalized(self, root: Path):
        path = store.recording_path("evt:123", root)
        assert path.parent.name == "evt_123"

    @pytest.mark.parametrize("raw", ["../../etc/passwd", "..", ".hidden", "a/b", "null\x00"])
    def test_rejects_traversal_and_bad_chars(self, raw, root: Path):
        with pytest.raises(store.MeetingsPathError):
            store.recording_path(raw, root)

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
    def test_symlink_out_of_root_refused(self, root: Path, tmp_path: Path):
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "meetings" / "escape"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(store.MeetingsPathError):
            store.recording_path("escape", root)


# ── POST …/{id}/audio ─────────────────────────────────────────────────────────


async def _init(client, meeting_id: str = "standup") -> None:
    resp = await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
    assert resp.status == 200, await resp.text()


def _multipart(data: bytes) -> dict:
    from aiohttp import FormData

    form = FormData()
    form.add_field("audio", data, filename="recording.webm", content_type="audio/webm")
    return {"data": form}


class TestUploadRecording:
    @pytest.mark.asyncio
    async def test_happy_multipart(self, app, root: Path):
        async with client_for(app) as client:
            await _init(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/audio", **_multipart(b"\x00\x01opusblob")
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body == {"ok": True, "bytes": len(b"\x00\x01opusblob")}
        assert store.recording_path("standup", root).read_bytes() == b"\x00\x01opusblob"

    @pytest.mark.asyncio
    async def test_happy_raw_body(self, app, root: Path):
        async with client_for(app) as client:
            await _init(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/audio",
                data=b"rawaudio",
                headers={"Content-Type": "audio/webm"},
            )
            assert resp.status == 200
            assert (await resp.json())["bytes"] == len(b"rawaudio")
        assert store.recording_path("standup", root).read_bytes() == b"rawaudio"

    @pytest.mark.asyncio
    async def test_over_cap_is_413(self, app, root: Path, monkeypatch):
        # Shrink the cap rather than uploading 200 MB.
        monkeypatch.setattr(k, "MAX_RECORDING_BYTES", 8)
        async with client_for(app) as client:
            await _init(client)
            resp = await client.post(f"{BASE}/meetings/standup/audio", **_multipart(b"waytoobig!!"))
            assert resp.status == 413
            assert (await resp.json())["code"] == "recording_too_large"
        # Nothing was persisted.
        assert not store.recording_path("standup", root).is_file()

    @pytest.mark.asyncio
    async def test_meeting_not_found_is_404(self, app):
        async with client_for(app) as client:
            resp = await client.post(f"{BASE}/meetings/ghost/audio", **_multipart(b"x"))
            assert resp.status == 404
            assert (await resp.json())["code"] == "meeting_not_found"

    @pytest.mark.asyncio
    async def test_ended_meeting_is_409(self, app, root: Path):
        async with client_for(app) as client:
            await _init(client)
            meta = store.read_meeting_meta("standup", root)
            assert meta is not None
            meta["status"] = k.STATUS_ENDED
            store.write_meeting_meta("standup", meta, root)
            resp = await client.post(f"{BASE}/meetings/standup/audio", **_multipart(b"x"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "meeting_ended"

    @pytest.mark.asyncio
    async def test_multipart_without_audio_field_is_400(self, app):
        from aiohttp import FormData

        form = FormData()
        form.add_field("notaudio", b"x", filename="x.bin")
        async with client_for(app) as client:
            await _init(client)
            resp = await client.post(f"{BASE}/meetings/standup/audio", data=form)
            assert resp.status == 400
            assert (await resp.json())["code"] == "no_audio"


# ── stop hook → transcription task ────────────────────────────────────────────


async def _drain_transcription_tasks() -> None:
    """Await every in-flight post-stop transcription task."""
    tasks = list(lifecycle._TRANSCRIBE_TASKS)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _start(client, meeting_id: str = "standup") -> None:
    await _init(client, meeting_id)
    resp = await client.post(f"{BASE}/meetings/{meeting_id}/start", json={})
    assert resp.status == 200, await resp.text()


class TestStopTranscription:
    @pytest.mark.asyncio
    async def test_recording_transcribed_appended_and_deleted(
        self, app, root: Path, fake_sessions, monkeypatch
    ):
        async def fake_transcribe(path, cfg):
            assert path == str(store.recording_path("standup", root))
            return "the transcribed meeting text"

        monkeypatch.setattr(lifecycle, "transcribe_audio", fake_transcribe)

        async with client_for(app) as client:
            await _start(client)
            store.recording_path("standup", root).write_bytes(b"opusblob")
            resp = await client.post(f"{BASE}/meetings/standup/stop")
            assert resp.status == 200
            assert (await resp.json())["status"] == k.STATUS_ENDED  # returns immediately
            await _drain_transcription_tasks()

        meta = store.read_meeting_meta("standup", root)
        assert meta is not None
        assert meta[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_DONE
        segments = store.read_transcript("standup", root)
        assert any(s["text"] == "the transcribed meeting text" for s in segments)
        # Transcribe-then-delete: audio is gone.
        assert not store.recording_path("standup", root).is_file()

    @pytest.mark.asyncio
    async def test_transcribe_failure_marks_failed_and_still_deletes(
        self, app, root: Path, fake_sessions, monkeypatch
    ):
        async def boom(path, cfg):
            raise RuntimeError("whisper exploded")

        monkeypatch.setattr(lifecycle, "transcribe_audio", boom)

        async with client_for(app) as client:
            await _start(client)
            store.recording_path("standup", root).write_bytes(b"opusblob")
            resp = await client.post(f"{BASE}/meetings/standup/stop")
            assert resp.status == 200  # meeting still ends cleanly
            await _drain_transcription_tasks()

        meta = store.read_meeting_meta("standup", root)
        assert meta is not None
        assert meta["status"] == k.STATUS_ENDED  # meeting still usable / ended
        assert meta[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_FAILED
        # Audio deleted even on failure.
        assert not store.recording_path("standup", root).is_file()

    @pytest.mark.asyncio
    async def test_no_recording_is_clean_noop_and_preserves_typed_transcript(
        self, app, root: Path, fake_sessions, monkeypatch
    ):
        called = False

        async def should_not_run(path, cfg):
            nonlocal called
            called = True
            return "nope"

        monkeypatch.setattr(lifecycle, "transcribe_audio", should_not_run)

        async with client_for(app) as client:
            await _start(client)
            # A typed line captured during the meeting.
            store.append_transcript("standup", "a typed line", k.TRANSCRIPT_SOURCE_TYPED, root)
            resp = await client.post(f"{BASE}/meetings/standup/stop")
            assert resp.status == 200
            await _drain_transcription_tasks()

        assert called is False  # no recording → transcribe never invoked
        meta = store.read_meeting_meta("standup", root)
        assert meta is not None
        assert lifecycle._META_TRANSCRIPTION not in meta  # field never set
        segments = store.read_transcript("standup", root)
        assert [s["text"] for s in segments] == ["a typed line"]  # typed transcript intact


# ── HIGH-2: double-stop must transcribe a recording exactly once ──────────────


class TestDoubleStopSingleTranscript:
    @pytest.mark.asyncio
    async def test_two_kickoffs_produce_one_segment(
        self, app, root: Path, fake_sessions, monkeypatch
    ):
        # Block the first transcribe mid-run so the SECOND kickoff races it while
        # the recording still exists — the exact double-stop window.
        release = asyncio.Event()
        calls = 0

        async def slow_transcribe(path, cfg):
            nonlocal calls
            calls += 1
            await release.wait()
            return "one and only transcript"

        monkeypatch.setattr(lifecycle, "transcribe_audio", slow_transcribe)

        async with client_for(app) as client:
            await _init(client)
        root_ = root
        store.recording_path("standup", root_).write_bytes(b"opusblob")

        # First kickoff claims the meeting (transcription=pending) and starts a task
        # that is now parked inside slow_transcribe. Second kickoff must find the
        # claim already held and start NOTHING.
        await lifecycle._kickoff_transcription("standup", root_)
        await asyncio.sleep(0)  # let the first task reach `await release.wait()`
        await lifecycle._kickoff_transcription("standup", root_)

        release.set()
        await _drain_transcription_tasks()

        assert calls == 1  # transcribe_audio invoked once, not twice
        segments = store.read_transcript("standup", root_)
        assert [s["text"] for s in segments] == ["one and only transcript"]
        meta = store.read_meeting_meta("standup", root_)
        assert meta is not None
        assert meta[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_DONE
        assert not store.recording_path("standup", root_).is_file()


# ── HIGH-3: delete during transcription must not resurrect the meeting ────────


class TestDeleteDuringTranscription:
    @pytest.mark.asyncio
    async def test_delete_before_append_leaves_no_orphan(
        self, app, root: Path, fake_sessions, monkeypatch
    ):
        release = asyncio.Event()

        async def slow_transcribe(path, cfg):
            await release.wait()
            return "text that must not resurrect the meeting"

        monkeypatch.setattr(lifecycle, "transcribe_audio", slow_transcribe)

        async with client_for(app) as client:
            await _init(client)
        store.recording_path("standup", root).write_bytes(b"opusblob")

        # Start the transcription task; park it inside transcribe.
        await lifecycle._kickoff_transcription("standup", root)
        await asyncio.sleep(0)

        # Delete the meeting WHILE the transcribe is in flight (the DELETE route
        # takes START_LOCK then rmtree's the dir under the meta lock). The meeting
        # is not active (init only, never started), so delete is admitted. Let the
        # transcribe finish and drain INSIDE this client context, so the client's
        # _on_cleanup (which bounded-awaits transcription tasks) sees none pending.
        async with client_for(app) as client:
            resp = await client.delete(f"{BASE}/meetings/standup")
            assert resp.status == 204, await resp.text()
            assert not store.meeting_dir("standup", root).exists()

            # Now let the transcribe finish. The append must NOT recreate the dir.
            release.set()
            await _drain_transcription_tasks()

        assert not store.meeting_dir("standup", root).exists()  # no resurrection
        assert store.read_meeting_meta("standup", root) is None
        assert not store.recording_path("standup", root).is_file()  # audio still deleted


# ── MEDIUM-1: append_transcript None (size ceiling) → failed, not done ────────


class TestSizeCeilingMarksFailed:
    @pytest.mark.asyncio
    async def test_append_none_marks_failed(self, app, root: Path, fake_sessions, monkeypatch):
        async def fake_transcribe(path, cfg):
            return "a valid transcript the ceiling rejects"

        monkeypatch.setattr(lifecycle, "transcribe_audio", fake_transcribe)
        # append_transcript returns None when the size ceiling would be exceeded.
        monkeypatch.setattr(store, "append_transcript", lambda *a, **k: None)

        async with client_for(app) as client:
            await _start(client)
            store.recording_path("standup", root).write_bytes(b"opusblob")
            resp = await client.post(f"{BASE}/meetings/standup/stop")
            assert resp.status == 200
            await _drain_transcription_tasks()

        meta = store.read_meeting_meta("standup", root)
        assert meta is not None
        assert meta[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_FAILED
        assert not store.recording_path("standup", root).is_file()

    @pytest.mark.asyncio
    async def test_none_transcription_marks_failed_not_done(
        self, app, root: Path, fake_sessions, monkeypatch
    ):
        # B3: whisper disabled/unavailable/silent -> transcribe_audio returns None.
        # That is NOT a success: meta must be 'failed' (so the UI does not show a
        # done meeting with an empty transcript), and the audio still deleted.
        async def none_transcribe(path, cfg):
            return None

        monkeypatch.setattr(lifecycle, "transcribe_audio", none_transcribe)
        async with client_for(app) as client:
            await _start(client)
            store.recording_path("standup", root).write_bytes(b"opusblob")
            resp = await client.post(f"{BASE}/meetings/standup/stop")
            assert resp.status == 200
            await _drain_transcription_tasks()

        meta = store.read_meeting_meta("standup", root)
        assert meta is not None
        assert meta[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_FAILED
        assert not store.recording_path("standup", root).is_file()


class TestStartupReconcile:
    def test_pending_with_recording_becomes_failed_and_deletes_audio(self, root: Path):
        # Simulate a crash mid-transcription: meta stuck 'pending' + recording on disk.
        meta = store.new_meeting_meta("standup", "Standup")
        meta[lifecycle._META_TRANSCRIPTION] = lifecycle._TRANSCRIPTION_PENDING
        store.write_meeting_meta("standup", meta, root)
        store.recording_path("standup", root).write_bytes(b"leaked audio")

        repaired = lifecycle.reconcile_pending_transcriptions(root)

        assert repaired == ["standup"]
        meta_after = store.read_meeting_meta("standup", root)
        assert meta_after is not None
        assert meta_after[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_FAILED
        assert not store.recording_path("standup", root).is_file()

    def test_pending_without_recording_becomes_failed(self, root: Path):
        # B4: a cancelled/crashed transcription can delete the audio then die
        # before writing terminal state, leaving meta 'pending' with no recording.
        # Reconcile must mark it 'failed' so the frontend poll terminates — the
        # earlier "left alone" behaviour polled forever.
        meta = store.new_meeting_meta("standup", "Standup")
        meta[lifecycle._META_TRANSCRIPTION] = lifecycle._TRANSCRIPTION_PENDING
        store.write_meeting_meta("standup", meta, root)
        assert not store.recording_path("standup", root).is_file()

        repaired = lifecycle.reconcile_pending_transcriptions(root)

        assert repaired == ["standup"]
        meta_after = store.read_meeting_meta("standup", root)
        assert meta_after is not None
        assert meta_after[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_FAILED

    def test_done_meeting_is_left_alone(self, root: Path):
        meta = store.new_meeting_meta("standup", "Standup")
        meta[lifecycle._META_TRANSCRIPTION] = lifecycle._TRANSCRIPTION_DONE
        store.write_meeting_meta("standup", meta, root)
        store.recording_path("standup", root).write_bytes(b"stray")

        repaired = lifecycle.reconcile_pending_transcriptions(root)

        assert repaired == []
        meta_after = store.read_meeting_meta("standup", root)
        assert meta_after is not None
        assert meta_after[lifecycle._META_TRANSCRIPTION] == lifecycle._TRANSCRIPTION_DONE


# ── SECURITY: streamed upload leaves no partial; success leaves only recording.webm ──


class TestUploadTempFileHygiene:
    @pytest.mark.asyncio
    async def test_over_cap_leaves_no_part_file(self, app, root: Path, monkeypatch):
        monkeypatch.setattr(k, "MAX_RECORDING_BYTES", 8)
        async with client_for(app) as client:
            await _init(client)
            resp = await client.post(f"{BASE}/meetings/standup/audio", **_multipart(b"waytoobig!!"))
            assert resp.status == 413
        mdir = store.meeting_dir("standup", root)
        # No recording.webm and no leftover .part temp file.
        assert not store.recording_path("standup", root).is_file()
        assert list(mdir.glob("*.part")) == []
        assert list(mdir.glob("recording.*.part")) == []

    @pytest.mark.asyncio
    async def test_success_leaves_only_recording_webm(self, app, root: Path):
        async with client_for(app) as client:
            await _init(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/audio", **_multipart(b"\x00\x01opusblob")
            )
            assert resp.status == 200
        mdir = store.meeting_dir("standup", root)
        assert store.recording_path("standup", root).read_bytes() == b"\x00\x01opusblob"
        # Exactly one recording artifact, no temp remnants.
        assert list(mdir.glob("recording*.part")) == []
        assert sorted(p.name for p in mdir.glob("recording*")) == [k.RECORDING_FILE]

    @pytest.mark.asyncio
    async def test_client_disconnect_mid_stream_leaves_no_part_file(
        self, app, root: Path, monkeypatch
    ):
        # Simulate aiohttp raising mid-stream (a dropped connection) AFTER the temp
        # file has been opened, and assert handle_upload_recording's
        # `except BaseException: _discard_temp` unlinks the partial so no orphan
        # `.part` accumulates in the meeting dir.
        import kiro_crew.apps.builtins.meetings.backend.routes.recording as rec

        async def _boom(request, handle):
            handle.write(b"partial-bytes-before-the-drop")
            raise ConnectionResetError("client went away")

        monkeypatch.setattr(rec, "_stream_audio_to_temp", _boom)
        async with client_for(app) as client:
            await _init(client)
            # The handler re-raises; aiohttp surfaces it as a 500. What matters is
            # the temp-file cleanup, asserted below regardless of the status.
            with contextlib.suppress(Exception):
                await client.post(f"{BASE}/meetings/standup/audio", **_multipart(b"ignored"))
        mdir = store.meeting_dir("standup", root)
        assert not store.recording_path("standup", root).is_file()
        assert list(mdir.glob("*.part")) == []
        assert list(mdir.glob("recording*.part")) == []

    @pytest.mark.asyncio
    async def test_stop_mid_upload_refuses_finalize_and_leaves_no_recording(
        self, app, root: Path, monkeypatch
    ):
        # B2: a stop/delete lands in another tab AFTER this handler's initial
        # not-ended check but WHILE the body streams. The finalize recheck (under
        # the meta lock) must then refuse to os.replace onto recording.webm — a
        # late finalize would leave untranscribed audio the already-run stop hook
        # never picks up. Simulate the race by ending the meeting from inside the
        # stream step, then assert 409 + no recording left.
        import kiro_crew.apps.builtins.meetings.backend.routes.recording as rec

        real_stream = rec._stream_audio_to_temp

        async def _stream_then_end(request, handle):
            n = await real_stream(request, handle)
            # A concurrent stop marks the meeting ended between the initial check
            # and finalize.
            meta = store.read_meeting_meta("standup", root)
            assert meta is not None
            meta["status"] = k.STATUS_ENDED
            store.write_meeting_meta("standup", meta, root)
            return n

        monkeypatch.setattr(rec, "_stream_audio_to_temp", _stream_then_end)
        async with client_for(app) as client:
            await _init(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/audio", **_multipart(b"\x00\x01opusblob")
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "meeting_ended"
        mdir = store.meeting_dir("standup", root)
        # No finalized recording and no orphan temp.
        assert not store.recording_path("standup", root).is_file()
        assert list(mdir.glob("*.part")) == []
