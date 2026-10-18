"""Tests for long recordings in ``kiro_crew.transcribe``.

Covers the provider-aware batch duration cap, the duration verdicts (WAV
headers and the ffmpeg null-decode probe), the segment cut rules, the
silence scan, the per-segment extract, and the orchestration that splits,
transcribes and stitches an over-cap recording.
"""

from __future__ import annotations

import asyncio
import os
import wave
from types import SimpleNamespace

import pytest

from kiro_crew import transcribe


class TestBatchDurationCap:
    """The provider-aware ceiling the meetings import route gates on."""

    def _cfg(self, provider: str) -> SimpleNamespace:
        return SimpleNamespace(provider=provider)

    def test_the_local_provider_has_the_decoder_ceiling(self):
        assert transcribe.batch_duration_cap_secs(self._cfg("local")) == transcribe._MAX_AUDIO_SECS

    def test_the_apple_provider_shares_the_ceiling(self):
        """The Apple lane's to-native remux is bounded with ``-t`` (an
        unbounded conversion could exhaust the temp volume, GPT review r23),
        so the import gate must refuse over-cap recordings for it too."""
        assert transcribe.batch_duration_cap_secs(self._cfg("apple")) == transcribe._MAX_AUDIO_SECS

    def test_providers_that_fail_loudly_have_no_ceiling(self):
        """AWS refuses an oversized payload outright, so it needs — and may
        impose — no local ceiling."""
        assert transcribe.batch_duration_cap_secs(self._cfg("transcribe")) is None

    def test_an_unrecognised_provider_gets_the_local_ceiling(self):
        """The dispatch in transcribe_audio lands unknown providers on the local
        engine, so the cap answer must travel with them."""
        assert (
            transcribe.batch_duration_cap_secs(self._cfg("something-new"))
            == transcribe._MAX_AUDIO_SECS
        )


class TestAudioExceedsSecs:
    """Duration verdicts: exact for WAV headers, honest None when unanswerable."""

    def _write_wav(self, path, seconds: int, rate: int = 100) -> None:
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(b"\x00\x00" * (seconds * rate))

    @pytest.mark.asyncio
    async def test_a_short_wav_is_under_the_cap(self, tmp_path):
        p = tmp_path / "short.wav"
        self._write_wav(p, seconds=5)
        assert await transcribe.audio_exceeds_secs(str(p), 10) is False

    @pytest.mark.asyncio
    async def test_a_wav_over_the_cap_is_detected_from_the_header_alone(self, tmp_path):
        """No decode and no sample read: the verdict is header math, which is
        how a multi-hour meeting WAV can be refused instantly."""
        p = tmp_path / "marathon.wav"
        self._write_wav(p, seconds=11)
        assert await transcribe.audio_exceeds_secs(str(p), 10) is True

    @pytest.mark.asyncio
    async def test_a_wav_exactly_at_the_cap_is_not_over_it(self, tmp_path):
        p = tmp_path / "exact.wav"
        self._write_wav(p, seconds=10)
        assert await transcribe.audio_exceeds_secs(str(p), 10) is False

    @pytest.mark.asyncio
    async def test_a_nonwav_with_no_ffmpeg_is_unanswerable_not_wrong(self, tmp_path, monkeypatch):
        """None, not False: without the decoder there is no answer — and the
        transcode that would truncate needs the same missing binary, so nothing
        is silently lost by proceeding."""
        p = tmp_path / "memo.opus"
        p.write_bytes(b"not really audio")

        async def _no_decoder():
            return None

        monkeypatch.setattr(transcribe, "_resolve_ffmpeg_for_execution", _no_decoder)
        assert await transcribe.audio_exceeds_secs(str(p), 10) is None

    def test_the_progress_parser_reads_the_last_out_time(self):
        """ffmpeg emits a progress block per interval; only the final decoded
        timestamp is the file's (bounded) duration."""
        stdout = b"out_time_us=1000000\nprogress=continue\nout_time_us=3601000000\nprogress=end\n"
        matches = transcribe._PROGRESS_OUT_TIME_RE.findall(stdout)
        assert int(matches[-1]) / 1_000_000 == 3601.0

    def test_wav_duration_math_ignores_sample_rate_mismatches(self, tmp_path):
        """_pcm_from_wav would reject a 44.1 kHz file (ffmpeg's job), but the
        duration answer is pure header math and stays exact for any rate."""
        p = tmp_path / "hifi.wav"
        self._write_wav(p, seconds=7, rate=200)
        assert transcribe._wav_duration_secs(str(p)) == 7.0

    def test_an_unreadable_file_has_no_wav_duration(self, tmp_path):
        p = tmp_path / "not.wav"
        p.write_bytes(b"RIFF but not really")
        assert transcribe._wav_duration_secs(str(p)) is None


class _FakeFfmpegProc:
    """Stand-in for the ffmpeg probe child: scripted stdout/returncode, and a
    hang mode that blocks until kill() so the timeout arm and _kill_and_reap
    can both be exercised without a real process (or a real platform)."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, hang: bool = False) -> None:
        self._stdout = stdout
        self.returncode = returncode
        self._hang = hang
        self._released = asyncio.Event()
        self.killed = False

    async def communicate(self):
        if self._hang and not self.killed:
            # flake-ok: pre-existing unbounded Event wait in the fake probe child's communicate that blocks the hang-mode test until kill sets the release event, moved verbatim; tracked for a de-flake pass
            await self._released.wait()
        return self._stdout, b""

    def kill(self) -> None:
        self.killed = True
        self._released.set()


class TestAudioExceedsSecsProbe:
    """The ffmpeg null-decode branch, driven through a scripted child process."""

    def _arm(self, monkeypatch, proc):
        async def _resolve():
            return "/opt/fake/ffmpeg"

        async def _spawn(_executable, *_argv, **_kw):
            if isinstance(proc, BaseException):
                raise proc
            return proc

        monkeypatch.setattr(transcribe, "_resolve_ffmpeg_for_execution", _resolve)
        monkeypatch.setattr(transcribe, "_create_ffmpeg_subprocess", _spawn)

    @pytest.mark.asyncio
    async def test_a_decode_past_the_cap_answers_true(self, tmp_path, monkeypatch):
        p = tmp_path / "long.webm"
        p.write_bytes(b"x")
        stdout = b"out_time_us=5000000\nprogress=continue\nout_time_us=11000000\nprogress=end\n"
        self._arm(monkeypatch, _FakeFfmpegProc(stdout=stdout))
        assert await transcribe.audio_exceeds_secs(str(p), 10) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("returncode", [0, 1])
    async def test_the_probe_closes_the_authenticated_handle_off_loop(
        self, tmp_path, monkeypatch, returncode
    ):
        """With close ownership on the callers, a probe that resolves an
        authenticated handle and never closes it runs the blocking ``os.close``
        on the gateway event loop via ``__del__``. Every exit — success and
        failure alike — must release the handle through
        ``_close_ffmpeg_for_execution``, off the loop thread, like the
        module's sibling spawn sites."""
        import threading

        p = tmp_path / "talk.webm"
        p.write_bytes(b"x")
        loop_thread = threading.get_ident()
        close_threads: list[int] = []

        handle = transcribe._AuthenticatedFfmpeg("src", -1, "/opt/fake/ffmpeg")
        original_close = transcribe._AuthenticatedFfmpeg.close

        def recording_close(self):
            close_threads.append(threading.get_ident())
            original_close(self)

        monkeypatch.setattr(transcribe._AuthenticatedFfmpeg, "close", recording_close)

        async def _resolve():
            return handle

        stdout = b"out_time_us=2000000\nprogress=end\n" if returncode == 0 else b""
        proc = _FakeFfmpegProc(stdout=stdout, returncode=returncode)

        async def _spawn(_executable, *_argv, **_kw):
            return proc

        monkeypatch.setattr(transcribe, "_resolve_ffmpeg_for_execution", _resolve)
        monkeypatch.setattr(transcribe, "_create_ffmpeg_subprocess", _spawn)
        await transcribe.audio_exceeds_secs(str(p), 10)
        assert len(close_threads) == 1, "the handle must be closed exactly once"
        assert close_threads[0] != loop_thread, "the close must run off the event loop"

    @pytest.mark.asyncio
    async def test_a_decode_under_the_cap_answers_false(self, tmp_path, monkeypatch):
        p = tmp_path / "short.webm"
        p.write_bytes(b"x")
        self._arm(monkeypatch, _FakeFfmpegProc(stdout=b"out_time_us=2000000\nprogress=end\n"))
        assert await transcribe.audio_exceeds_secs(str(p), 10) is False

    @pytest.mark.asyncio
    async def test_a_failed_decode_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "corrupt.webm"
        p.write_bytes(b"x")
        self._arm(monkeypatch, _FakeFfmpegProc(returncode=1))
        assert await transcribe.audio_exceeds_secs(str(p), 10) is None

    @pytest.mark.asyncio
    async def test_a_decode_with_no_progress_output_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "odd.webm"
        p.write_bytes(b"x")
        self._arm(monkeypatch, _FakeFfmpegProc(stdout=b""))
        assert await transcribe.audio_exceeds_secs(str(p), 10) is None

    @pytest.mark.asyncio
    async def test_a_hung_probe_times_out_reaps_the_child_and_answers_none(
        self, tmp_path, monkeypatch
    ):
        p = tmp_path / "hang.webm"
        p.write_bytes(b"x")
        proc = _FakeFfmpegProc(hang=True)
        self._arm(monkeypatch, proc)
        # flake-ok: pre-existing zero-second probe timeout that the hung fake child must outlast so the timeout arm kills and reaps it, moved verbatim; tracked for a de-flake pass
        assert await transcribe.audio_exceeds_secs(str(p), 10, timeout_secs=0) is None
        assert proc.killed, "the timed-out child must be killed, not leaked"

    @pytest.mark.asyncio
    async def test_an_unspawnable_ffmpeg_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "noexec.webm"
        p.write_bytes(b"x")
        self._arm(monkeypatch, OSError("exec format error"))
        assert await transcribe.audio_exceeds_secs(str(p), 10) is None

    def test_the_capless_answer_loads_config_when_none_is_given(self, monkeypatch):
        monkeypatch.setattr(
            transcribe, "_load_stt_config", lambda: SimpleNamespace(provider="transcribe")
        )
        assert transcribe.batch_duration_cap_secs() is None

    def test_only_the_local_provider_splits(self):
        # Local truncates silently -> split. Apple fails loudly + its helper's
        # sandbox masks the segment dir -> no split. AWS has no ceiling -> no split.
        assert transcribe.provider_splits_oversized(SimpleNamespace(provider="local")) is True
        assert transcribe.provider_splits_oversized(SimpleNamespace(provider="apple")) is False
        assert transcribe.provider_splits_oversized(SimpleNamespace(provider="transcribe")) is False
        # An unrecognised provider degrades to local (the config loader does this),
        # so it splits like local rather than silently refusing.
        assert transcribe.provider_splits_oversized(SimpleNamespace(provider="whatever")) is True

    def test_it_takes_a_required_config_and_never_reads_config_itself(self, monkeypatch):
        # First Principles subtraction: the one caller always passes the shared
        # snapshot, so the function takes a REQUIRED arg and never self-loads config
        # (which would open a window for the three answers to describe different
        # providers). A bare call is a TypeError, not a silent config read.
        called = {"n": 0}
        monkeypatch.setattr(
            transcribe, "_load_stt_config", lambda: called.__setitem__("n", called["n"] + 1)
        )
        transcribe.provider_splits_oversized(SimpleNamespace(provider="local"))
        assert called["n"] == 0
        with pytest.raises(TypeError):
            transcribe.provider_splits_oversized()


class TestChooseSegmentCuts:
    """The boundary rules for splitting an over-cap recording — a pure function,
    tested directly the way ``audio.split_transcript`` is, because the interesting
    part is where the cuts land, not the ffmpeg plumbing around them."""

    def _cuts(self, duration, cap, silences):
        return transcribe._choose_segment_cuts(duration, cap, silences)

    def test_a_recording_within_the_cap_is_not_split(self):
        assert self._cuts(50.0, 60, [10.0, 20.0]) == []

    def test_a_recording_exactly_at_the_cap_is_not_split(self):
        # duration - start == cap is NOT "> cap", so no cut.
        assert self._cuts(60.0, 60, []) == []

    def test_it_cuts_at_the_last_silence_before_the_cap(self):
        # Cap 60, window 30: silences at 35 and 58 are both in (30, 60]; the last
        # one (58) is chosen so the segment is as full as the pause allows.
        assert self._cuts(90.0, 60, [35.0, 58.0]) == [58.0]

    def test_a_silence_past_the_cap_is_not_used(self):
        # 61 is past the cap; only 40 qualifies.
        assert self._cuts(90.0, 60, [40.0, 61.0]) == [40.0]

    def test_no_silence_in_the_window_cuts_hard_at_the_cap(self):
        # The only silence (5) is outside the (30, 60] window, so the cut is the
        # hard cap boundary — the documented rare seam.
        assert self._cuts(90.0, 60, [5.0]) == [60.0]

    def test_no_silence_at_all_cuts_hard_at_every_cap_multiple(self):
        assert self._cuts(200.0, 60, []) == [60.0, 120.0, 180.0]

    def test_each_cut_advances_from_the_previous_one(self):
        # Second window is (start+30, start+60] measured from the FIRST cut, not
        # from zero — a silence reused would stall the loop.
        cuts = self._cuts(150.0, 60, [55.0, 110.0])
        assert cuts == [55.0, 110.0]
        # Every resulting segment is within the cap.
        bounds = [0.0, *cuts, 150.0]
        assert all(bounds[i + 1] - bounds[i] <= 60 for i in range(len(bounds) - 1))

    def test_a_silence_at_or_before_the_running_start_is_ignored(self):
        # After the first cut at 60, a silence at 60 (== start) must not be reused
        # as the next cut; the loop falls to the hard target 120.
        assert self._cuts(150.0, 60, [60.0]) == [60.0, 120.0]

    def test_the_last_segment_may_be_shorter_than_the_cap(self):
        # 130s at cap 60 with a silence at 58: cuts at 58 and 118, leaving a 12s
        # tail — a short final segment is fine, it is not padded.
        assert self._cuts(130.0, 60, [58.0, 118.0]) == [58.0, 118.0]

    def test_unsorted_silence_input_is_handled(self):
        assert self._cuts(90.0, 60, [58.0, 35.0]) == [58.0]


class TestTranscribeOversizedInSegments:
    """The orchestration that splits, transcribes and stitches an over-cap
    recording. The ffmpeg seam (silence scan + segment extract) is stubbed — the
    point here is the control flow: how many segments are cut, that each is
    transcribed through the ordinary ``transcribe_audio``, that the results are
    joined with spaces, and that ANY failure refuses the whole import."""

    def _cfg(self, timeout_secs=300):
        return SimpleNamespace(timeout_secs=timeout_secs)

    @pytest.mark.asyncio
    async def test_it_cuts_transcribes_and_stitches_into_one_paragraph(self, tmp_path, monkeypatch):
        # 130s at cap 60, one silence at 58 -> cuts [58, 118] -> 3 segments.
        async def _scan(path, *, timeout_secs):
            return [58.0, 118.0], 130.0

        extracted: list[tuple[float, float | None, str]] = []

        async def _extract(path, start, end, out, *, timeout_secs):
            extracted.append((start, end, out))
            return True

        transcribed: list[str] = []

        async def _transcribe(path, cfg):
            transcribed.append(path)
            # Segment index encoded in the filename; return distinct text.
            return f"text of {os.path.basename(path)}"

        monkeypatch.setattr(transcribe, "_detect_silence_ends", _scan)
        monkeypatch.setattr(transcribe, "_extract_segment", _extract)
        monkeypatch.setattr(transcribe, "transcribe_audio", _transcribe)

        result = await transcribe.transcribe_oversized_in_segments(
            "/dev/fd/9", 60, str(tmp_path), self._cfg()
        )
        # Three segments, joined by a SPACE in order (one paragraph, so the route's
        # split_transcript sentence-splits it into utterances instead of treating
        # each whole segment as one hard-wrapped line).
        assert result == ("text of segment-000.wav text of segment-001.wav text of segment-002.wav")
        assert "\n" not in result
        # Boundaries: [0,58), [58,118), [118,None) — last runs to EOF.
        assert [(s, e) for s, e, _ in extracted] == [(0.0, 58.0), (58.0, 118.0), (118.0, None)]
        assert len(transcribed) == 3

    @pytest.mark.asyncio
    async def test_an_unscannable_recording_refuses_the_whole_import(self, tmp_path, monkeypatch):
        async def _scan(path, *, timeout_secs):
            return None

        monkeypatch.setattr(transcribe, "_detect_silence_ends", _scan)
        result = await transcribe.transcribe_oversized_in_segments(
            "/dev/fd/9", 60, str(tmp_path), self._cfg()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_a_segment_that_fails_to_extract_refuses_the_whole_import(
        self, tmp_path, monkeypatch
    ):
        async def _scan(path, *, timeout_secs):
            return [], 200.0  # no silence -> hard cuts at 60, 120, 180

        async def _extract(path, start, end, out, *, timeout_secs):
            return start == 0.0  # only the first segment extracts

        async def _transcribe(path, cfg):
            return "ok"

        monkeypatch.setattr(transcribe, "_detect_silence_ends", _scan)
        monkeypatch.setattr(transcribe, "_extract_segment", _extract)
        monkeypatch.setattr(transcribe, "transcribe_audio", _transcribe)

        result = await transcribe.transcribe_oversized_in_segments(
            "/dev/fd/9", 60, str(tmp_path), self._cfg()
        )
        assert result is None, "a segment that cannot be extracted refuses the whole import"

    @pytest.mark.asyncio
    async def test_a_falsy_segment_transcript_refuses_the_whole_import(self, tmp_path, monkeypatch):
        # GPT + Opus review (both BLOCKING): transcribe_audio returns None for a
        # genuine recogniser failure (decode error, timeout, singleton swapped
        # mid-import) as well as for a silent segment, indistinguishable here. A
        # clean _extract_segment only proves ffmpeg carved the WAV, not that the
        # recogniser succeeded. So a falsy segment must refuse the WHOLE import,
        # never be skipped -- skipping would silently drop a real spoken span and
        # answer 200, the exact silent-partial loss the feature prevents.
        async def _scan(path, *, timeout_secs):
            return [], 130.0  # hard cuts -> 3 segments

        async def _extract(path, start, end, out, *, timeout_secs):
            return True

        calls = {"n": 0}

        async def _transcribe(path, cfg):
            calls["n"] += 1
            return "" if calls["n"] == 2 else "ok"  # the middle segment is falsy

        monkeypatch.setattr(transcribe, "_detect_silence_ends", _scan)
        monkeypatch.setattr(transcribe, "_extract_segment", _extract)
        monkeypatch.setattr(transcribe, "transcribe_audio", _transcribe)

        result = await transcribe.transcribe_oversized_in_segments(
            "/dev/fd/9", 60, str(tmp_path), self._cfg()
        )
        assert result is None, "a falsy segment transcript refuses the whole import"

    @pytest.mark.asyncio
    async def test_an_implausible_duration_is_refused_before_building_cuts(
        self, tmp_path, monkeypatch
    ):
        # GPT security-class review: a crafted container can report a far-future
        # terminal PTS. The cut loop appends one float per cap-sized window, so an
        # unbounded duration would allocate billions of cuts and freeze/OOM. The
        # duration is refused against _MAX_SEGMENTS * cap BEFORE any cut is built,
        # and no segment is ever extracted.
        extracted = {"n": 0}

        async def _scan(path, *, timeout_secs):
            # cap=60, _MAX_SEGMENTS=512 -> ceiling 30720s; report far past it.
            return [], 60.0 * 512 * 1000

        async def _extract(path, start, end, out, *, timeout_secs):
            extracted["n"] += 1
            return True

        monkeypatch.setattr(transcribe, "_detect_silence_ends", _scan)
        monkeypatch.setattr(transcribe, "_extract_segment", _extract)

        result = await transcribe.transcribe_oversized_in_segments(
            "/dev/fd/9", 60, str(tmp_path), self._cfg()
        )
        assert result is None
        assert extracted["n"] == 0, "no cut is built and no segment is extracted"

    @pytest.mark.asyncio
    async def test_a_nonpositive_duration_is_refused(self, tmp_path, monkeypatch):
        async def _scan(path, *, timeout_secs):
            return [], 0.0

        monkeypatch.setattr(transcribe, "_detect_silence_ends", _scan)
        result = await transcribe.transcribe_oversized_in_segments(
            "/dev/fd/9", 60, str(tmp_path), self._cfg()
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_each_segment_file_is_removed_after_transcription(self, tmp_path, monkeypatch):
        async def _scan(path, *, timeout_secs):
            return [], 130.0  # 3 segments

        async def _extract(path, start, end, out, *, timeout_secs):
            # Actually create the file, so the unlink is observable.
            with open(out, "wb") as fh:
                fh.write(b"RIFFfake")
            return True

        async def _transcribe(path, cfg):
            assert os.path.exists(path), "the segment must exist while it is transcribed"
            return "ok"

        monkeypatch.setattr(transcribe, "_detect_silence_ends", _scan)
        monkeypatch.setattr(transcribe, "_extract_segment", _extract)
        monkeypatch.setattr(transcribe, "transcribe_audio", _transcribe)

        result = await transcribe.transcribe_oversized_in_segments(
            "/dev/fd/9", 60, str(tmp_path), self._cfg()
        )
        assert result == "ok ok ok"
        # No segment WAV is left behind in the caller's dir.
        assert list(tmp_path.glob("segment-*.wav")) == []


class _FakeLineReader:
    """A StreamReader stand-in: hands back scripted bytes one line at a time,
    then b"" at EOF. In hang mode it blocks until released (kill)."""

    def __init__(self, data: bytes, released: "asyncio.Event | None" = None, hang: bool = False):
        self._lines = data.splitlines(keepends=True) if data else []
        self._i = 0
        self._released = released
        self._hang = hang

    async def readline(self) -> bytes:
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            return line
        if self._hang and self._released is not None and not self._released.is_set():
            # flake-ok: pre-existing unbounded Event wait in the fake line reader's readline that parks the hang-mode scan at EOF until kill sets the release event, moved verbatim; tracked for a de-flake pass
            await self._released.wait()
        return b""

    async def read(self, _n: int = -1) -> bytes:
        # Hand back all remaining bytes at once (bounded reader in the code keeps
        # only a tail), then EOF.
        if self._i < len(self._lines):
            rest = b"".join(self._lines[self._i :])
            self._i = len(self._lines)
            return rest
        if self._hang and self._released is not None and not self._released.is_set():
            # flake-ok: pre-existing unbounded Event wait in the fake line reader's read that parks the hang-mode drain at EOF until kill sets the release event, moved verbatim; tracked for a de-flake pass
            await self._released.wait()
        return b""


class _FakeFfmpegProcWithStderr:
    """Like ``_FakeFfmpegProc`` but carries stderr too. Serves BOTH read shapes:
    ``communicate()`` (the segment extract) AND ``stdout``/``stderr`` line
    readers + ``wait()`` (the silence scan's incremental drain)."""

    def __init__(
        self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, hang: bool = False
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self._released = asyncio.Event()
        self.killed = False
        self.stdout = _FakeLineReader(stdout, self._released, hang)
        self.stderr = _FakeLineReader(stderr, self._released, hang)

    async def communicate(self):
        if self._hang and not self.killed:
            # flake-ok: pre-existing unbounded Event wait in the stderr-carrying fake child's communicate that blocks the hang-mode extract until kill sets the release event, moved verbatim; tracked for a de-flake pass
            await self._released.wait()
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._released.set()


class _CancellingLineReader:
    async def readline(self):
        raise asyncio.CancelledError()

    async def read(self, _n: int = -1):
        raise asyncio.CancelledError()


class _CancellingFfmpegProc:
    """A child whose reads/communicate raise CancelledError, to exercise the
    reap-and-reraise cancellation arm of the spawn sites."""

    def __init__(self) -> None:
        self.returncode = None
        self.killed = False
        self.stdout = _CancellingLineReader()
        self.stderr = _CancellingLineReader()

    async def communicate(self):
        if not self.killed:
            raise asyncio.CancelledError()
        return b"", b""

    async def wait(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _arm_ffmpeg(monkeypatch, proc):
    """Point the resolver + spawn seam at a scripted child (or an exception)."""

    async def _resolve():
        return "/opt/fake/ffmpeg"

    async def _spawn(_executable, *_argv, **_kw):
        if isinstance(proc, BaseException):
            raise proc
        return proc

    async def _close(_executable, **_kw):
        return None

    monkeypatch.setattr(transcribe, "_resolve_ffmpeg_for_execution", _resolve)
    monkeypatch.setattr(transcribe, "_create_ffmpeg_subprocess", _spawn)
    monkeypatch.setattr(transcribe, "_close_ffmpeg_for_execution", _close)


class TestDetectSilenceEnds:
    """The one silence-scan ffmpeg pass, driven through a scripted child."""

    @pytest.mark.asyncio
    async def test_it_reads_silence_ends_and_the_duration(self, tmp_path, monkeypatch):
        p = tmp_path / "meeting.webm"
        p.write_bytes(b"x")
        # stdout carries the -progress out_time; stderr carries silencedetect events.
        stdout = b"out_time_us=1000000\nprogress=continue\nout_time_us=130000000\nprogress=end\n"
        stderr = (
            b"[silencedetect @ 0x1] silence_start: 57.5\n"
            b"[silencedetect @ 0x1] silence_end: 58.2 | silence_duration: 0.7\n"
            b"[silencedetect @ 0x1] silence_end: 118.9 | silence_duration: 0.4\n"
        )
        _arm_ffmpeg(monkeypatch, _FakeFfmpegProcWithStderr(stdout=stdout, stderr=stderr))
        result = await transcribe._detect_silence_ends(str(p), timeout_secs=300)
        assert result is not None
        silence_ends, duration = result
        assert silence_ends == [58.2, 118.9]
        assert duration == 130.0

    @pytest.mark.asyncio
    async def test_no_silence_events_is_an_empty_list_not_none(self, tmp_path, monkeypatch):
        p = tmp_path / "talky.webm"
        p.write_bytes(b"x")
        stdout = b"out_time_us=90000000\nprogress=end\n"
        _arm_ffmpeg(monkeypatch, _FakeFfmpegProcWithStderr(stdout=stdout, stderr=b""))
        result = await transcribe._detect_silence_ends(str(p), timeout_secs=300)
        assert result == ([], 90.0)

    @pytest.mark.asyncio
    async def test_no_progress_output_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "bad.webm"
        p.write_bytes(b"x")
        _arm_ffmpeg(monkeypatch, _FakeFfmpegProcWithStderr(stdout=b"", stderr=b""))
        assert await transcribe._detect_silence_ends(str(p), timeout_secs=300) is None

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "corrupt.webm"
        p.write_bytes(b"x")
        stdout = b"out_time_us=5000000\nprogress=end\n"
        _arm_ffmpeg(
            monkeypatch, _FakeFfmpegProcWithStderr(stdout=stdout, stderr=b"boom", returncode=1)
        )
        assert await transcribe._detect_silence_ends(str(p), timeout_secs=300) is None

    @pytest.mark.asyncio
    async def test_a_missing_decoder_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "x.webm"
        p.write_bytes(b"x")

        async def _no_decoder():
            return None

        monkeypatch.setattr(transcribe, "_resolve_ffmpeg_for_execution", _no_decoder)
        assert await transcribe._detect_silence_ends(str(p), timeout_secs=300) is None

    @pytest.mark.asyncio
    async def test_a_timeout_kills_the_child_and_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "slow.webm"
        p.write_bytes(b"x")
        proc = _FakeFfmpegProcWithStderr(hang=True)
        _arm_ffmpeg(monkeypatch, proc)
        # flake-ok: pre-existing zero-second scan timeout that the hung fake child must outlast so the silence scan kills and reaps it, moved verbatim; tracked for a de-flake pass
        assert await transcribe._detect_silence_ends(str(p), timeout_secs=0) is None
        assert proc.killed, "the timed-out child must be killed, not leaked"

    @pytest.mark.asyncio
    async def test_an_unresolvable_demuxer_is_unanswerable(self, monkeypatch):
        # _forced_demuxer_args raises OSError for a descriptor path whose suffix
        # cannot be read; the scan must answer None rather than sniff content.
        assert await transcribe._detect_silence_ends("/dev/fd/999", timeout_secs=300) is None

    @pytest.mark.asyncio
    async def test_an_unspawnable_ffmpeg_is_unanswerable(self, tmp_path, monkeypatch):
        p = tmp_path / "x.webm"
        p.write_bytes(b"x")
        _arm_ffmpeg(monkeypatch, OSError("exec format error"))
        assert await transcribe._detect_silence_ends(str(p), timeout_secs=300) is None

    @pytest.mark.asyncio
    async def test_a_cancellation_kills_the_child_and_propagates(self, tmp_path, monkeypatch):
        # A CancelledError landing on communicate() must reap the child (so no
        # decoder leaks) and re-raise, not be swallowed as a None answer.
        p = tmp_path / "x.webm"
        p.write_bytes(b"x")
        proc = _CancellingFfmpegProc()
        _arm_ffmpeg(monkeypatch, proc)
        with pytest.raises(asyncio.CancelledError):
            await transcribe._detect_silence_ends(str(p), timeout_secs=300)
        assert proc.killed

    @pytest.mark.asyncio
    async def test_a_pathological_flood_of_silence_events_is_refused(self, tmp_path, monkeypatch):
        # GPT security-class review: a pause-dense recording could emit millions of
        # silence_end lines. The scan keeps only parsed floats, capped at
        # _MAX_SILENCE_POINTS, and refuses (None) past the cap rather than buffering
        # unbounded memory and OOM-crashing the gateway.
        p = tmp_path / "flood.webm"
        p.write_bytes(b"x")
        monkeypatch.setattr(transcribe, "_MAX_SILENCE_POINTS", 3)
        stdout = b"out_time_us=90000000\nprogress=end\n"
        stderr = b"".join(
            b"[silencedetect] silence_end: %d.0 | silence_duration: 0.3\n" % i for i in range(10)
        )
        _arm_ffmpeg(monkeypatch, _FakeFfmpegProcWithStderr(stdout=stdout, stderr=stderr))
        assert await transcribe._detect_silence_ends(str(p), timeout_secs=300) is None

    @pytest.mark.asyncio
    async def test_an_over_64kib_stderr_line_with_no_newline_does_not_raise(
        self, tmp_path, monkeypatch
    ):
        # GPT security-class review: StreamReader.readline() raises ValueError on a
        # line past its 64 KiB limit, which a crafted container's metadata tag can
        # produce -> unhandled 500. The reader now uses read(n) (no line limit) and
        # a bounded rolling buffer, so a >64 KiB stderr line with NO newline is
        # handled cleanly and the scan still returns its duration from stdout.
        p = tmp_path / "bigline.webm"
        p.write_bytes(b"x")
        stdout = b"out_time_us=90000000\nprogress=end\n"
        stderr = b"[metadata] title: " + (b"A" * (200 * 1024))  # >64 KiB, no newline
        _arm_ffmpeg(monkeypatch, _FakeFfmpegProcWithStderr(stdout=stdout, stderr=stderr))
        result = await transcribe._detect_silence_ends(str(p), timeout_secs=300)
        assert result == ([], 90.0), "no ValueError; duration still parsed, no silence found"


class TestExtractSegment:
    """The per-segment ffmpeg extract, driven through a scripted child."""

    @pytest.mark.asyncio
    async def test_a_clean_extract_returns_true(self, tmp_path, monkeypatch):
        inp = tmp_path / "in.webm"
        inp.write_bytes(b"x")
        out = tmp_path / "segment-000.wav"
        _arm_ffmpeg(monkeypatch, _FakeFfmpegProcWithStderr(returncode=0))
        ok = await transcribe._extract_segment(str(inp), 0.0, 58.0, str(out), timeout_secs=300)
        assert ok is True

    @pytest.mark.asyncio
    async def test_the_last_segment_has_no_end(self, tmp_path, monkeypatch):
        # end=None must not raise (it means "run to EOF", no -t arg).
        inp = tmp_path / "in.webm"
        inp.write_bytes(b"x")
        out = tmp_path / "segment-001.wav"
        _arm_ffmpeg(monkeypatch, _FakeFfmpegProcWithStderr(returncode=0))
        ok = await transcribe._extract_segment(str(inp), 58.0, None, str(out), timeout_secs=300)
        assert ok is True

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_returns_false(self, tmp_path, monkeypatch):
        inp = tmp_path / "in.webm"
        inp.write_bytes(b"x")
        out = tmp_path / "segment-000.wav"
        _arm_ffmpeg(
            monkeypatch,
            _FakeFfmpegProcWithStderr(stderr=b"decode error", returncode=1),
        )
        ok = await transcribe._extract_segment(str(inp), 0.0, 58.0, str(out), timeout_secs=300)
        assert ok is False

    @pytest.mark.asyncio
    async def test_a_missing_decoder_returns_false(self, tmp_path, monkeypatch):
        inp = tmp_path / "in.webm"
        inp.write_bytes(b"x")
        out = tmp_path / "segment-000.wav"

        async def _no_decoder():
            return None

        monkeypatch.setattr(transcribe, "_resolve_ffmpeg_for_execution", _no_decoder)
        ok = await transcribe._extract_segment(str(inp), 0.0, 58.0, str(out), timeout_secs=300)
        assert ok is False

    @pytest.mark.asyncio
    async def test_a_timeout_kills_the_child_and_returns_false(self, tmp_path, monkeypatch):
        inp = tmp_path / "in.webm"
        inp.write_bytes(b"x")
        out = tmp_path / "segment-000.wav"
        proc = _FakeFfmpegProcWithStderr(hang=True)
        _arm_ffmpeg(monkeypatch, proc)
        # flake-ok: pre-existing zero-second extract timeout that the hung fake child must outlast so the segment extract kills and reaps it, moved verbatim; tracked for a de-flake pass
        ok = await transcribe._extract_segment(str(inp), 0.0, 58.0, str(out), timeout_secs=0)
        assert ok is False
        assert proc.killed, "the timed-out child must be killed, not leaked"

    @pytest.mark.asyncio
    async def test_an_unspawnable_ffmpeg_returns_false(self, tmp_path, monkeypatch):
        inp = tmp_path / "in.webm"
        inp.write_bytes(b"x")
        out = tmp_path / "segment-000.wav"
        _arm_ffmpeg(monkeypatch, OSError("exec format error"))
        ok = await transcribe._extract_segment(str(inp), 0.0, 58.0, str(out), timeout_secs=300)
        assert ok is False

    @pytest.mark.asyncio
    async def test_an_unresolvable_demuxer_returns_false(self, tmp_path, monkeypatch):
        # _forced_demuxer_args raises OSError for a descriptor path whose suffix
        # cannot be read; the extract must refuse rather than sniff content.
        out = tmp_path / "segment-000.wav"
        ok = await transcribe._extract_segment("/dev/fd/999", 0.0, 58.0, str(out), timeout_secs=300)
        assert ok is False

    @pytest.mark.asyncio
    async def test_a_cancellation_kills_the_child_and_propagates(self, tmp_path, monkeypatch):
        # A CancelledError on communicate() must reap the child and re-raise.
        inp = tmp_path / "in.webm"
        inp.write_bytes(b"x")
        out = tmp_path / "segment-000.wav"
        proc = _CancellingFfmpegProc()
        _arm_ffmpeg(monkeypatch, proc)
        with pytest.raises(asyncio.CancelledError):
            await transcribe._extract_segment(str(inp), 0.0, 58.0, str(out), timeout_secs=300)
        assert proc.killed
