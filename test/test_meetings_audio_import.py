"""Importing an existing recording into a live meeting.

Two halves, tested separately because they fail differently:

* :mod:`...domain.audio` — the pure split from one transcript blob into the lines
  the dispatch transaction expects. Every downstream consumer (transcript append,
  dictionary, noise gate, agent batcher) is per-line, so the boundary rules are the
  feature.
* the route — a file path arriving from a client, which means the interesting tests
  are the refusals and their ORDER, not the happy path.

No model and no audio decoder is ever reached: ``transcribe_audio`` and the
availability probe are patched in every route test.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from meetings_helpers import (  # noqa: F401 — fixtures are used by name
    app_fixture,
    client_for,
    enabled_fixture,
    fake_sessions_fixture,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.domain import audio
from kiro_crew.apps.builtins.meetings.backend.routes import _common
from kiro_crew.apps.builtins.meetings.backend.routes import audio_import as ai

BASE = k.API_BASE


async def _start(client, meeting_id: str = "standup") -> None:
    await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
    resp = await client.post(f"{BASE}/meetings/{meeting_id}/start", json={})
    assert resp.status == 200, await resp.text()


@pytest.fixture(autouse=True)
def _owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route test calls as the dashboard owner unless it says otherwise.

    Mirrors ``_patch``'s philosophy: the owner gate stays ACTIVE but passing, so
    the normal-path tests exercise the handler THROUGH the gate rather than
    bypassing it. The helper itself reads dashboard auth state this bare test
    app does not carry, so it is patched at this module's import site — the
    denial test overrides it back to False.
    """
    monkeypatch.setattr(ai, "is_owner_dashboard_request", lambda _request: True)


def _patch(monkeypatch: pytest.MonkeyPatch, **over: Any) -> dict[str, list]:
    """Patch the route's external dependencies. Returns a call log.

    ``transcribe_audio`` (and the config/duration helpers beside it) is patched on
    :mod:`kiro_crew.transcribe`, not on this module, because the route imports
    them INSIDE the handler (so a heavy optional dependency is not imported at
    gateway startup). Defaults keep the gate ACTIVE but passing — a cap exists
    and the recording is under it — so every normal-path test exercises the gate
    rather than bypassing it. The snapshot copy is stubbed to an identity (the
    vetted path IS the snapshot) so route tests need no real files; the real
    helper has its own tests below.
    """
    import kiro_crew.transcribe as transcribe_mod

    stt_config = over.get("stt_config", SimpleNamespace(timeout_secs=300))
    log: dict[str, list] = {
        "vetted": [],
        "transcribed": [],
        "probed": [],
        "probe_timeouts": [],
        "config_loads": [],
        "snapshots": [],
        "ready_config": [],
        "cap_config": [],
    }

    def _vet(raw: str) -> tuple[str, str]:
        log["vetted"].append(raw)
        return over.get("vet", (raw, ""))

    def _load_config() -> Any:
        log["config_loads"].append(stt_config)
        return stt_config

    def _ready(cfg: Any) -> bool:
        log["ready_config"].append(cfg)
        return over.get("ready", True)

    def _snapshot(canonical: str, snapshot_dir: str) -> str | None:
        log["snapshots"].append(canonical)
        if over.get("snapshot_refused"):
            return None
        return canonical

    async def _transcribe(path: str, *a: Any, **kw: Any) -> str | None:
        log["transcribed"].append((path, a[0] if a else kw.get("stt_config")))
        return over.get("transcript", "we decided to ship on Friday")

    def _cap(cfg: Any = None) -> int | None:
        log["cap_config"].append(cfg)
        return over.get("cap", 3600)

    async def _exceeds(path: str, max_secs: int, **kw: Any) -> bool | None:
        log["probed"].append((path, max_secs))
        log["probe_timeouts"].append(kw.get("timeout_secs"))
        return over.get("exceeds", False)

    monkeypatch.setattr(ai, "_vet_audio_file", _vet)
    monkeypatch.setattr(ai, "_transcription_ready", _ready)
    monkeypatch.setattr(ai, "_snapshot_recording", _snapshot)
    monkeypatch.setattr(transcribe_mod, "load_stt_config", _load_config)
    monkeypatch.setattr(transcribe_mod, "transcribe_audio", _transcribe)
    monkeypatch.setattr(transcribe_mod, "batch_duration_cap_secs", _cap)
    monkeypatch.setattr(transcribe_mod, "audio_exceeds_secs", _exceeds)
    return log


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


class TestSplitTranscript:
    def _split(self, text: str, *, max_chars: int = 4000, max_lines: int = 2000) -> list[str]:
        return audio.split_transcript(text, max_chars=max_chars, max_lines=max_lines)

    def test_nothing_in_nothing_out(self):
        assert self._split("") == []
        assert self._split("   \n\n\t ") == []

    def test_prefers_the_transcribers_own_segments(self):
        """Tier 1. A whisper segment is the closest thing to "one utterance"."""
        assert self._split("first line\nsecond line\n\nthird line") == [
            "first line",
            "second line",
            "third line",
        ]

    def test_does_not_resplit_segments_on_punctuation(self):
        """A segment containing two sentences stays ONE line.

        Tier 1 wins outright: the transcriber's own boundary is better information
        than anything punctuation can reconstruct, so sentence splitting must not
        also run over it.
        """
        assert self._split("Yes. No.\nMaybe.") == ["Yes. No.", "Maybe."]

    def test_falls_back_to_sentences_for_a_single_paragraph(self):
        """Tier 2. AWS Transcribe returns one line for the whole recording."""
        assert self._split("We shipped it. Bob owns the rollback! Does that work?") == [
            "We shipped it.",
            "Bob owns the rollback!",
            "Does that work?",
        ]

    def test_a_decimal_point_is_not_a_sentence_boundary(self):
        # The lookahead requires whitespace after the mark, which is what keeps
        # "3.5" and "v1.2" intact.
        assert self._split("We picked version 1.2 and 3.5 GB of RAM.") == [
            "We picked version 1.2 and 3.5 GB of RAM.",
        ]

    def test_splits_cjk_sentence_marks(self):
        assert self._split("出荷を決めた。ロールバックは田中さんが担当。") == [
            "出荷を決めた。",
            "ロールバックは田中さんが担当。",
        ]

    def test_an_over_long_line_is_wrapped_not_truncated(self):
        """Tier 3. Truncating would silently DROP the tail of a long sentence."""
        long_line = " ".join(["word"] * 100)  # ~499 chars
        out = self._split(long_line, max_chars=50)
        assert len(out) > 1
        assert all(len(line) <= 50 for line in out)
        # Every word survives, and in order.
        assert " ".join(out).split() == long_line.split()

    def test_text_with_no_spaces_is_hard_sliced(self):
        # CJK has no word spaces, so the whitespace-preferring wrap must not loop
        # forever or give up.
        out = self._split("あ" * 120, max_chars=50)
        assert [len(line) for line in out] == [50, 50, 20]

    def test_a_line_count_overflow_is_rejected_not_sliced(self):
        """GPT review: a capped result is indistinguishable from a complete one.

        Silently discarding the tail past ``max_lines`` while the route returns
        200 is data loss the user cannot see — the split refuses the whole
        recording instead.
        """
        with pytest.raises(audio.TranscriptTooLong):
            self._split("\n".join(f"line {i}" for i in range(50)), max_lines=10)

    def test_a_recording_exactly_at_the_line_budget_is_accepted(self):
        out = self._split("\n".join(f"line {i}" for i in range(10)), max_lines=10)
        assert len(out) == 10
        assert out[0] == "line 0"
        assert out[-1] == "line 9"

    def test_every_line_is_stripped_and_non_empty(self):
        out = self._split("  padded  \n\n\n   \n  also padded  ")
        assert out == ["padded", "also padded"]


# ---------------------------------------------------------------------------
# Refusals, and their order
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_non_owner_caller_is_refused_before_anything_runs(self, app, monkeypatch):
        """The owner gate comes before everything — body parsing included.

        The route's capability is "read an arbitrary host file by path", the
        class aws-control and the app job routes reserve for the dashboard
        owner. A non-owner gets the shared denial shape, and none of the
        machinery below the gate (vetting, transcription) ever runs — proven by
        the call log staying empty even though the request body names a path.
        """
        log = _patch(monkeypatch)
        monkeypatch.setattr(ai, "is_owner_dashboard_request", lambda _request: False)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "dashboard_owner_required"
        assert log["vetted"] == []
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_both_owner_decisions_reach_the_audit_trail(self, app, monkeypatch):
        """GPT review r13: an ALLOW is a permission decision too. Without the
        allowed record, an owner request that then fails JSON parsing would
        leave no trace the owner path was entered; auditing only denials shows
        who was refused but never who got through."""
        _patch(monkeypatch, transcript="hello")
        records: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            ai,
            "audit",
            lambda op, res, *, outcome, error="": records.append((op, res, outcome)),
        )
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 200
        assert any(o == "allowed" and "owner-check" in r for _op, r, o in records)

        monkeypatch.setattr(ai, "is_owner_dashboard_request", lambda _request: False)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 403
        assert any(o == "denied" and "non-owner" in r for _op, r, o in records)

    @pytest.mark.asyncio
    async def test_no_live_meeting_is_409(self, app, monkeypatch):
        log = _patch(monkeypatch)
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "no_active_meeting"
        # And nothing was transcribed: the session check comes FIRST so an hour of
        # audio is not decoded on the way to an error we could give immediately.
        assert log["transcribed"] == []
        assert log["vetted"] == []

    @pytest.mark.asyncio
    async def test_an_expired_session_is_410_and_ends_the_meeting(
        self, app, fake_sessions, monkeypatch, root: Path
    ):
        """Shared with /dispatch through `_common.dispatch_admission`, side effects included."""
        _patch(monkeypatch)
        async with client_for(app) as client:
            await _start(client)
            session = _common.ACTIVE.get("standup")
            assert session is not None
            session.started_at -= k.MAX_SESSION_DURATION + 1

            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 410
            assert (await resp.json())["code"] == "meeting_session_expired"

            body = await (await client.get(f"{BASE}/meetings/standup")).json()
        # The expiry branch's side effect: the session is gone AND the meeting says so.
        assert _common.ACTIVE.get("standup") is None
        assert body["meta"]["status"] == k.STATUS_ENDED

    @pytest.mark.asyncio
    async def test_a_recreated_meeting_does_not_receive_the_old_recording(
        self, app, fake_sessions, monkeypatch
    ):
        """A meeting stopped and recreated with the SAME id mid-import is not contaminated.

        The id is a name, not an identity: transcription takes minutes, and a user
        who stops the meeting and starts a new one under the same id during that
        window must not find someone else's recording in the new meeting's
        transcript. The import pins the session OBJECT admitted at its start and
        every dispatched line requires that same object, so the replacement gets a
        410 instead of the old lines.
        """
        import kiro_crew.transcribe as transcribe_mod

        _patch(monkeypatch)
        async with client_for(app) as client:
            await _start(client)
            old = _common.ACTIVE.get("standup")
            assert old is not None

            async def _transcribe_while_recreated(path: str, *_a: Any, **_kw: Any) -> str:
                # Mid-transcription: the meeting is stopped and a NEW one is
                # started under the same id — the reviewer's exact scenario.
                resp = await client.post(f"{BASE}/meetings/standup/stop", json={})
                assert resp.status == 200, await resp.text()
                await _start(client)
                return "a line from the old recording"

            monkeypatch.setattr(transcribe_mod, "transcribe_audio", _transcribe_while_recreated)

            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 410
            assert (await resp.json())["code"] == "meeting_session_replaced"

            new = _common.ACTIVE.get("standup")
            assert new is not None and new is not old
            # Nothing from the old recording reached the replacement session...
            assert all(len(q.queue) == 0 for q in new.agents.values())
            # ...or the recreated meeting's transcript.
            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
        assert all("old recording" not in seg["text"] for seg in body["segments"]), body["segments"]

    @pytest.mark.asyncio
    async def test_a_replacement_still_initializing_answers_410_not_409(
        self, app, fake_sessions, monkeypatch
    ):
        """GPT review r7: the replacement's INITIALIZATION window.

        A recreated session spends its first moments with ingress closed, where
        ``get_for_dispatch`` answers None. A 409 ``no_active_meeting`` there would
        tell the import to retry — into a session it was never admitted to. The
        identity check must see the initializing session (``ACTIVE.get``) and
        answer the permanent 410, same as the fully-started replacement above.
        """
        import kiro_crew.transcribe as transcribe_mod

        _patch(monkeypatch)
        async with client_for(app) as client:
            await _start(client)
            old = _common.ACTIVE.get("standup")
            assert old is not None

            async def _transcribe_while_replacement_initializes(
                path: str, *_a: Any, **_kw: Any
            ) -> str:
                # Stop, start the replacement, then close its ingress the same way
                # the start handler does mid-initialization: session installed,
                # dispatches held, exactly the window the 409 used to leak from.
                resp = await client.post(f"{BASE}/meetings/standup/stop", json={})
                assert resp.status == 200, await resp.text()
                await _start(client)
                _common.ACTIVE.suspend_dispatches(_common.ACTIVE.get("standup"), buffer_speech=True)
                return "a line from the old recording"

            monkeypatch.setattr(
                transcribe_mod, "transcribe_audio", _transcribe_while_replacement_initializes
            )

            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 410, await resp.text()
            assert (await resp.json())["code"] == "meeting_session_replaced"

            # Nothing was buffered into the initializing replacement's hold.
            new = _common.ACTIVE.get("standup")
            assert new is not None and new is not old
            assert all(len(q.queue) == 0 for q in new.agents.values())

    @pytest.mark.asyncio
    async def test_a_denied_path_is_403(self, app, fake_sessions, monkeypatch):
        log = _patch(monkeypatch, vet=("", "denied"))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import",
                json={"audio_path": "/home/someone/.aws/credentials"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "audio_path_denied"
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_a_missing_file_is_404(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, vet=("", "not_a_file"))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/gone.wav"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "audio_file_not_found"

    @pytest.mark.asyncio
    async def test_an_unsupported_format_is_400(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, vet=("", "unsupported_format"))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/notes.pdf"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "audio_format_unsupported"

    @pytest.mark.asyncio
    async def test_unavailable_speech_to_text_is_503(self, app, fake_sessions, monkeypatch):
        """503, not 400: the request is fine and works once Settings is fixed."""
        log = _patch(monkeypatch, ready=False)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "transcription_unavailable"
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_a_failed_transcription_is_502(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, transcript=None)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "transcription_failed"

    @pytest.mark.asyncio
    async def test_an_emptied_transcript_is_also_502(self, app, fake_sessions, monkeypatch):
        """The hallucination filter returns "" for a transcript that was all boilerplate.

        Reporting success with zero lines would put "Thanks for watching!" — or
        nothing at all — in front of the user as a completed import.
        """
        _patch(monkeypatch, transcript="")
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 502

    @pytest.mark.asyncio
    async def test_an_over_long_recording_is_413_and_nothing_is_dispatched(
        self, app, fake_sessions, monkeypatch
    ):
        """The whole recording is refused — never a silent partial import."""
        _patch(monkeypatch, transcript="\n".join(f"line {i}" for i in range(10)))
        monkeypatch.setattr(k, "MAX_IMPORT_LINES", 5)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 413
            assert (await resp.json())["code"] == "recording_too_long"
            session = _common.ACTIVE.get("standup")
            assert session is not None
            assert all(len(q.queue) == 0 for q in session.agents.values())
            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
        assert body["segments"] == []

    @pytest.mark.asyncio
    async def test_a_missing_path_is_400(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/import", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_an_oversized_file_is_413_and_never_reaches_the_decoder(
        self, app, fake_sessions, monkeypatch
    ):
        """GPT review: the size ceiling refuses BEFORE transcription, so an
        oversized upload costs nothing — no decode, no dispatch."""
        log = _patch(monkeypatch, vet=("", "file_too_large"))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/huge.wav"}
            )
            assert resp.status == 413
            assert (await resp.json())["code"] == "audio_file_too_large"
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_a_recording_over_the_decoder_cap_is_413_not_a_truncated_200(
        self, app, fake_sessions, monkeypatch
    ):
        """GPT review: the local decoder stops reading at its ceiling WITHOUT
        saying so, so a recording over the cap must be refused whole before
        transcription — never transcribed-truncated and dispatched as a 200."""
        log = _patch(monkeypatch, exceeds=True)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/marathon.webm"}
            )
            assert resp.status == 413
            assert (await resp.json())["code"] == "recording_too_long"
        assert log["probed"] == [("/tmp/marathon.webm", 3600)]
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_the_probe_gets_the_transcodes_own_time_budget(
        self, app, fake_sessions, monkeypatch
    ):
        """GPT review r8: the probe decodes a strict subset of the transcode's
        work, so giving it a SHORTER budget than ``stt_config.timeout_secs``
        opens a band where the probe times out (None → proceed) but the
        transcode "succeeds" truncated — silent data loss on exactly the
        over-cap files the guard exists to catch. The route must hand the probe
        the same budget the transcode will get."""
        log = _patch(monkeypatch, stt_config=SimpleNamespace(timeout_secs=222))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/long.webm"}
            )
            assert resp.status == 200
        assert log["probe_timeouts"] == [222]

    @pytest.mark.asyncio
    async def test_an_indeterminate_duration_probe_is_refused_retryably(
        self, app, fake_sessions, monkeypatch
    ):
        """GPT review r14: the aligned probe budget covers PERSISTENT None
        causes (they defeat the transcode too), but a TRANSIENT one — a load
        spike that clears between probe and transcode — would let the local
        decoder truncate an over-cap recording and answer 200. An indeterminate
        probe is refused with a retryable 503 whose message names the retry and
        the cap, so the refusal is actionable, never silent data loss."""
        log = _patch(monkeypatch, exceeds=None)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/glitch.webm"}
            )
            assert resp.status == 503
            body = await resp.json()
            assert body["code"] == "duration_unverified"
            # Actionable: the message tells the user what to do next.
            assert "retry" in body["error"] and "minutes" in body["error"]
        assert log["transcribed"] == [], "nothing may be transcribed on an unverified duration"

    @pytest.mark.asyncio
    async def test_a_provider_without_a_ceiling_skips_the_duration_probe(
        self, app, fake_sessions, monkeypatch
    ):
        """AWS/Apple providers fail loudly instead of truncating, so an import
        under them is not probed and not wrongly refused."""
        log = _patch(monkeypatch, cap=None, exceeds=True)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/long.ogg"}
            )
            assert resp.status == 200
        assert log["probed"] == []
        assert [p for p, _cfg in log["transcribed"]] == ["/tmp/long.ogg"]

    @pytest.mark.asyncio
    async def test_one_config_snapshot_feeds_readiness_cap_and_transcription(
        self, app, fake_sessions, monkeypatch
    ):
        """GPT review: the readiness check, the duration gate, and the
        transcription must all describe the SAME provider. The handler loads one
        config snapshot and passes that identical object to all three — three
        separate loads could straddle a provider switch, re-opening the
        silent-truncation hole the duration gate exists to close."""
        marker = SimpleNamespace(timeout_secs=222)
        log = _patch(monkeypatch, stt_config=marker)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 200
        assert log["config_loads"] == [marker], "exactly one config read per request"
        assert log["ready_config"] == [marker]
        assert log["cap_config"] == [marker]
        assert [cfg for _p, cfg in log["transcribed"]] == [marker]

    @pytest.mark.asyncio
    async def test_a_snapshot_refusal_is_403_and_nothing_is_transcribed(
        self, app, fake_sessions, monkeypatch
    ):
        """GPT review: the vetted path can be swapped before it is opened. The
        pinned snapshot copy is what closes that window, so a source it refuses
        (no longer the validated inode) is denied like any unreadable path —
        never probed, never transcribed."""
        log = _patch(monkeypatch, snapshot_refused=True)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/swapped.mp3"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "audio_path_denied"
        assert log["snapshots"] == ["/tmp/swapped.mp3"]
        assert log["probed"] == []
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_a_concurrent_import_into_the_same_meeting_is_409(
        self, app, fake_sessions, monkeypatch
    ):
        """One import per meeting at a time: a second request answers 409 while
        the first is running, and the guard is released when the first ends."""
        import kiro_crew.transcribe as transcribe_mod

        _patch(monkeypatch)
        gate = asyncio.Event()
        started = asyncio.Event()

        async def _slow_transcribe(path: str, *_a: object, **_kw: object) -> str:
            started.set()
            await gate.wait()
            return "we decided to ship on Friday"

        monkeypatch.setattr(transcribe_mod, "transcribe_audio", _slow_transcribe)
        async with client_for(app) as client:
            await _start(client)
            first = asyncio.create_task(
                client.post(f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"})
            )
            await asyncio.wait_for(started.wait(), timeout=5.0)
            second = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/b.wav"}
            )
            assert second.status == 409
            assert (await second.json())["code"] == "import_in_progress"
            gate.set()
            resp = await asyncio.wait_for(first, timeout=5.0)
            assert resp.status == 200
            # The guard is released on completion — a follow-up import is admitted.
            assert ai._imports_in_flight == set()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestImport:
    @pytest.mark.asyncio
    async def test_the_transcript_reaches_every_unmuted_agent(
        self, app, fake_sessions, monkeypatch
    ):
        """The point of routing through the dispatch transaction: the whole pipeline."""
        _patch(monkeypatch, transcript="we shipped it\nBob owns the rollback")
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 200
            body = await resp.json()

            assert body["lines"] == 2
            assert body["dispatched"] == 2
            # Read INSIDE the client block: the app's `on_cleanup` hook drains and
            # clears the active session, so the queues are gone once it exits.
            session = _common.ACTIVE.get("standup")
            assert session is not None
            for queue in session.agents.values():
                assert "we shipped it" in queue.queue
                assert "Bob owns the rollback" in queue.queue

    @pytest.mark.asyncio
    async def test_imported_lines_are_persisted_before_fan_out(
        self, app, fake_sessions, monkeypatch
    ):
        """An accepted agent line cannot be absent from the transcript — imports too.

        The app-wide data-integrity boundary (`_common.dispatch_line` persists, then
        fans out) applies to an imported recording exactly as it does to speech, so
        the transcript panel can read the import back like anything spoken.
        """
        _patch(monkeypatch, transcript="we shipped it\nBob owns the rollback")
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 200
            body = await (await client.get(f"{BASE}/meetings/standup/transcript")).json()
        assert [seg["text"] for seg in body["segments"]] == [
            "we shipped it",
            "Bob owns the rollback",
        ]
        # Imported audio is finalized speech that went through STT, so it carries the
        # same source live STT segments do — the panel needs no third rendering rule.
        assert all(seg["source"] == k.TRANSCRIPT_SOURCE_SPEECH for seg in body["segments"])

    @pytest.mark.asyncio
    async def test_the_domain_dictionary_corrects_an_imported_line(
        self, app, fake_sessions, monkeypatch
    ):
        """Imported text goes through the SAME pipeline as speech, corrections included.

        This is the whole argument for dispatching rather than storing: nothing had
        to be re-implemented for import, and nothing can drift.
        """
        from kiro_crew.apps.builtins.meetings.backend.domain import session as sess

        _patch(monkeypatch, transcript="we deployed it to cooper netties today")
        async with client_for(app) as client:
            await _start(client)
            # Loaded AFTER the server is up: the app's `on_startup` hook reloads the
            # dictionary from disk, which would replace terms loaded any earlier.
            sess.shared_dictionary().load_terms(
                [{"correct": "Kubernetes", "aliases": ["cooper netties"]}]
            )
            await client.post(f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"})
            session = _common.ACTIVE.get("standup")
            assert session is not None
            queued = next(iter(session.agents.values())).queue
            assert any("Kubernetes" in line for line in queued)

    @pytest.mark.asyncio
    async def test_a_muted_agent_is_skipped(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, transcript="one line")
        async with client_for(app) as client:
            await _start(client)
            await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "muted": True},
            )
            await client.post(f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"})
            session = _common.ACTIVE.get("standup")
            assert session is not None
            assert session.agents["note-taker"].queue == []
            assert session.agents["sketch-artist"].queue == ["one line"]

    @pytest.mark.asyncio
    async def test_lines_and_dispatched_are_reported_separately(
        self, app, fake_sessions, monkeypatch
    ):
        """The gap between them is what the noise gate dropped.

        A recording that yields lines of which NONE were dispatched is a real
        outcome — an empty room, filler — and must be visible rather than reported
        as a clean success.
        """
        _patch(monkeypatch, transcript="uh\num\nuh")
        async with client_for(app) as client:
            await _start(client)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
                )
            ).json()
        assert body["lines"] == 3
        assert body["dispatched"] == 0

    @pytest.mark.asyncio
    async def test_the_canonical_path_is_reported_not_the_clients(
        self, app, fake_sessions, monkeypatch
    ):
        _patch(monkeypatch, vet=("/canonical/a.wav", ""))
        async with client_for(app) as client:
            await _start(client)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/import",
                    json={"audio_path": "~/link-to-a.wav"},
                )
            ).json()
        assert body["path"] == "/canonical/a.wav"


# ---------------------------------------------------------------------------
# The path barrier
# ---------------------------------------------------------------------------


class TestPathBarrier:
    def test_the_shared_gate_is_used_and_the_predicate_is_not(self):
        """``validate_file_path``, never ``is_sensitive_path`` directly.

        Using the gate is what makes this route's answer identical to every other
        file read in the product; calling the predicate here would be a second
        opinion that can drift from it.
        """
        import inspect

        src = inspect.getsource(ai)
        assert "validate_file_path(" in src
        # The CALL, not the word — the module docstring names the predicate when it
        # explains what the gate enforces, and that prose is the point.
        assert "is_sensitive_path(" not in src

    def test_the_extension_is_checked_on_the_canonical_path(self, tmp_path: Path):
        """A symlink named ``.mp3`` must not smuggle in its target.

        ``validate_file_path`` resolves symlinks, and the suffix test runs on the
        RESULT — so the name the client chose is never what is checked.
        """
        target = tmp_path / "secret.pdf"
        target.write_bytes(b"%PDF-1.4")
        link = tmp_path / "innocent.mp3"
        link.symlink_to(target)

        canonical, reason = ai._vet_audio_file(str(link))
        assert canonical == ""
        assert reason == "unsupported_format"

    def test_a_nul_byte_path_is_denied_not_a_crash(self):
        """GPT review: `realpath` raises ValueError on an embedded NUL byte.

        A malformed path the OS refuses to work with must come back as a
        refusal — never propagate and 500 the request. The exact reason is
        platform-dependent: POSIX `realpath` raises (caught -> "denied"),
        while Windows' non-strict `realpath` swallows the error and the
        existence check then reports "not_a_file". Both are non-crash
        refusals the route maps to a 4xx, which is the pinned invariant.
        """
        canonical, reason = ai._vet_audio_file("/tmp/a\x00b.wav")
        assert canonical == ""
        assert reason in ("denied", "not_a_file")

    def test_a_real_audio_file_passes(self, tmp_path: Path):
        wav = tmp_path / "meeting.wav"
        wav.write_bytes(b"RIFF....WAVE")
        canonical, reason = ai._vet_audio_file(str(wav))
        assert reason == ""
        assert canonical == str(wav.resolve())

    def test_an_oversized_file_is_refused_before_decoding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """GPT review: the decoder materializes PCM for the whole file, so the
        size ceiling must fire in the vet step — while the memory cost is zero."""
        wav = tmp_path / "huge.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 60 + b"WAVE")
        monkeypatch.setattr(k, "MAX_IMPORT_AUDIO_BYTES", 8)
        canonical, reason = ai._vet_audio_file(str(wav))
        assert canonical == ""
        assert reason == "file_too_large"

    def test_a_file_exactly_at_the_size_ceiling_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        wav = tmp_path / "fits.wav"
        payload = b"RIFF....WAVE"
        wav.write_bytes(payload)
        monkeypatch.setattr(k, "MAX_IMPORT_AUDIO_BYTES", len(payload))
        canonical, reason = ai._vet_audio_file(str(wav))
        assert reason == ""
        assert canonical == str(wav.resolve())

    def test_a_directory_is_not_a_file(self, tmp_path: Path):
        d = tmp_path / "recordings.wav"
        d.mkdir()
        assert ai._vet_audio_file(str(d)) == ("", "not_a_file")

    def test_every_accepted_extension_is_lowercase_and_dotted(self):
        # The check lowercases the suffix, so an uppercase entry here would be dead.
        for ext in k.IMPORT_AUDIO_EXTENSIONS:
            assert ext == ext.lower()
            assert ext.startswith(".")

    def test_an_uppercase_suffix_is_still_accepted(self, tmp_path: Path):
        wav = tmp_path / "MEETING.WAV"
        wav.write_bytes(b"RIFF....WAVE")
        assert ai._vet_audio_file(str(wav))[1] == ""


class TestSnapshotRecording:
    """The pinned snapshot copy — the real helper against a real filesystem."""

    def test_a_regular_file_is_copied_with_its_suffix(self, tmp_path: Path):
        src = tmp_path / "talk.MP3"
        src.write_bytes(b"audio bytes")
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        dst = ai._snapshot_recording(str(src), str(snap_dir))
        assert dst is not None and dst.endswith(".mp3")
        assert Path(dst).read_bytes() == b"audio bytes"

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlinks")
    def test_a_path_swapped_for_a_symlink_is_refused(self, tmp_path: Path):
        """GPT review: the swap attack — the validated NAME now points at a link
        to something else. The pinned open (O_NOFOLLOW + fstat on the
        descriptor) refuses it instead of copying the link's target."""
        secret = tmp_path / "credentials"
        secret.write_bytes(b"AKIA...")
        swapped = tmp_path / "talk.wav"
        try:
            os.symlink(secret, swapped)
        except OSError:
            pytest.skip("symlinks not permitted here")
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        assert ai._snapshot_recording(str(swapped), str(snap_dir)) is None
        assert list(snap_dir.iterdir()) == [], "nothing may be copied from a swapped path"

    def test_a_source_grown_past_the_ceiling_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The vet step's size ceiling judged the original name; the snapshot
        re-judges the bytes actually copied, so a swap to a huge file cannot
        ride the earlier answer into the decoder. GPT review r12: enforced
        INSIDE the copy, so the refusal materializes (at most) an emptied
        destination entry, never the oversize bytes."""
        monkeypatch.setattr(ai.k, "MAX_IMPORT_AUDIO_BYTES", 4)
        src = tmp_path / "talk.wav"
        src.write_bytes(b"more than four bytes")
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        assert ai._snapshot_recording(str(src), str(snap_dir)) is None
        assert sum(p.stat().st_size for p in snap_dir.iterdir()) == 0

    @pytest.mark.asyncio
    async def test_cleanup_joins_the_copy_before_removing_the_directory(self, tmp_path: Path):
        """GPT review r12: a ``to_thread`` copy cannot be cancelled, so removing
        the snapshot directory while the worker still holds a handle inside it
        loses the race on Windows (``rmtree`` cannot delete an open file,
        ``ignore_errors`` hides it, the stale recording leaks). The cleanup
        helper must JOIN the copy first -- proven here by a copy that holds the
        directory open until released: the removal must not happen while the
        worker is still inside."""
        import threading

        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        (snap_dir / "recording.wav").write_bytes(b"bytes")
        release = threading.Event()
        entered = threading.Event()

        def _slow_copy() -> None:
            with open(snap_dir / "recording.wav", "rb"):
                entered.set()
                release.wait(timeout=10)

        copy_task = asyncio.ensure_future(asyncio.to_thread(_slow_copy))
        await asyncio.to_thread(entered.wait, 10)

        cleanup = asyncio.ensure_future(ai._remove_snapshot_dir(copy_task, str(snap_dir)))
        await asyncio.sleep(0.1)
        assert snap_dir.exists(), "removal must wait for the copy worker to exit"

        release.set()
        await cleanup
        assert not snap_dir.exists()

    def test_a_vanished_source_is_refused_not_raised(self, tmp_path: Path):
        """GPT review r7: ``copy_file_pinned`` propagates ``FileNotFoundError`` BY
        CONTRACT so the caller can tolerate a vanished source — the helper maps it
        to the same None as every other refusal, so the route answers 403, not 500."""
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        assert ai._snapshot_recording(str(tmp_path / "gone.wav"), str(snap_dir)) is None
        assert list(snap_dir.iterdir()) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_an_unreadable_source_is_refused_not_raised(self, tmp_path: Path):
        """GPT review r8: a vanished source was tolerated but a PERMISSION-DENIED
        one escaped as an unhandled ``PermissionError`` → 500. Every ``OSError``
        out of the pinned copy is the same story — a source this request cannot
        read — so the helper maps them all to None and the route answers the
        same 403 as any other unreadable path."""
        if os.geteuid() == 0:  # pragma: no cover — root ignores permission bits
            pytest.skip("running as root; chmod 000 does not deny reads")
        src = tmp_path / "private.wav"
        src.write_bytes(b"not yours")
        src.chmod(0)
        try:
            snap_dir = tmp_path / "snap"
            snap_dir.mkdir()
            assert ai._snapshot_recording(str(src), str(snap_dir)) is None
            assert list(snap_dir.iterdir()) == []
        finally:
            src.chmod(0o600)

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlinks")
    def test_an_ancestor_swapped_for_a_symlink_is_refused(self, tmp_path: Path):
        """GPT review r7: ``O_NOFOLLOW`` on the final component never fires when an
        ANCESTOR directory is the link — the traversal is redirected before the
        final open. ``pin_parent`` walks the chain with one O_NOFOLLOW ``openat``
        per component and refuses the swapped ancestor."""
        from kiro_crew.pinned_fs import supports_pinned_walk

        if not supports_pinned_walk():
            pytest.skip("platform cannot pin a directory walk")
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "talk.wav").write_bytes(b"innocent")
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        (evil_dir / "talk.wav").write_bytes(b"AKIA...")
        # The path the vet step validated…
        canonical = str(real_dir / "talk.wav")
        # …whose ancestor is now a link to somewhere else entirely.
        try:
            os.rename(real_dir, tmp_path / "moved")
            os.symlink(evil_dir, real_dir)
        except OSError:
            pytest.skip("symlinks not permitted here")
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        assert ai._snapshot_recording(canonical, str(snap_dir)) is None
        assert list(snap_dir.iterdir()) == [], "nothing may be copied through a swapped ancestor"

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlinks")
    def test_windows_fallback_refuses_an_ancestor_junction(self, tmp_path: Path, monkeypatch):
        """GPT review r9: on Windows (no ``dir_fd`` support) the by-name fallback
        only probed the FINAL component, so a junction planted on an ANCESTOR
        redirected the whole traversal — the exact swap an unprivileged Windows
        process can create. The fallback now probes the target and every
        ancestor, the same shape as ``apps/routes.py``'s Windows branch.
        Simulated here by forcing ``supports_pinned_walk`` off; the reparse
        probe answers True for POSIX symlinks too, so the refusal is
        platform-independently testable."""
        import kiro_crew.pinned_fs as pinned_fs

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "talk.wav").write_bytes(b"innocent")
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        (evil_dir / "talk.wav").write_bytes(b"AKIA...")
        canonical = str(real_dir / "talk.wav")
        try:
            os.rename(real_dir, tmp_path / "moved")
            os.symlink(evil_dir, real_dir)
        except OSError:
            pytest.skip("symlinks not permitted here")
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        assert ai._snapshot_recording(canonical, str(snap_dir)) is None
        assert list(snap_dir.iterdir()) == [], "the by-name fallback must not follow the junction"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    def test_the_route_is_registered(self):
        from aiohttp import web

        from kiro_crew.apps.builtins.meetings.backend.routes import register_routes

        app = web.Application()
        register_routes(app)
        assert any(
            route.method == "POST"
            and route.resource is not None
            and route.resource.canonical == f"{BASE}/meetings/{{meeting_id}}/import"
            for route in app.router.routes()
        )

    def test_both_producers_share_the_dispatch_transaction(self):
        """One copy of the admission transaction and the expiry side effects, not two.

        A producer that skipped `dispatch_line` would either skip the transcript
        append (breaking persist-before-fan-out) or re-implement the admission
        lock (reopening the stop-versus-append race), so both handlers are pinned
        to it.
        """
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import agents as ag

        assert "dispatch_line(" in inspect.getsource(ag.handle_dispatch_text)
        assert "dispatch_line(" in inspect.getsource(ai.handle_import_audio)
        # And the transaction owns the append, the fan-out, and the expiry side
        # effects in exactly one place.
        assert "append_transcript" in inspect.getsource(_common.dispatch_line)
        admission = inspect.getsource(_common.dispatch_admission.__wrapped__)
        assert "drain_and_clear" in admission
        assert "end_meeting_meta" in admission

    def test_the_handler_does_no_blocking_io_inline(self):
        import inspect

        src = inspect.getsource(ai.handle_import_audio)
        assert "asyncio.to_thread" in src
        # The blocking work lives in the helpers the thread runs.
        assert "validate_file_path(" not in src
        assert "is_available(" not in src

    def test_transcribe_is_imported_lazily(self):
        """Not at module import: the STT stack pulls optional heavy dependencies.

        A gateway that registers this app must not pay for a decoder nobody asked
        for, and `faster-whisper` is deliberately not a declared extra.
        """
        import inspect

        header = inspect.getsource(ai).split("logger = ")[0]
        assert "from kiro_crew.transcribe import" not in header
