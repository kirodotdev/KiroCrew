"""Tests for the voice_reply pipeline around a provider.

The ``synthesize_speech`` dispatcher and the refusal it must not swallow, the
Slack upload, ``voice_reply`` end to end, the streaming reply with its redaction
and consent gate, text-type detection, and the cleanup ``stitch_mp3s`` owes on
every failure exit.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from voice_reply_helpers import _no_argv_prefixers  # noqa: F401
from voice_reply_helpers import _polly_consented_fixture  # noqa: F401
from voice_reply_helpers import (
    _capture_mkstemp,
    _mock_subprocess,
    _patch_aws_on_path,
)

from kiro_crew.voice_reply import (
    PROVIDER_POLLY,
    PROVIDER_SYSTEM,
    SYSTEM_ENGINE_ESPEAK,
    _synthesize_polly,
    stitch_mp3s,
    synthesize_speech,
    text_to_ssml,
    upload_voice_to_slack,
    voice_reply,
)

# ── synthesize_speech() dispatcher ───────────────────────────────────────


class TestSynthesizeSpeechDispatcher:
    @pytest.mark.asyncio
    async def test_polly_dispatch(self) -> None:
        with (
            patch(
                "kiro_crew.voice_reply._synthesize_polly",
                new=AsyncMock(return_value="/tmp/out.mp3"),
            ) as mock_polly,
            patch(
                "kiro_crew.voice_reply._synthesize_system",
                new=AsyncMock(return_value="/tmp/out.wav"),
            ) as mock_system,
        ):
            out = await synthesize_speech("hello world", provider=PROVIDER_POLLY)
        assert out == "/tmp/out.mp3"
        mock_polly.assert_awaited_once()
        mock_system.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_dispatch(self) -> None:
        with (
            patch(
                "kiro_crew.voice_reply._synthesize_polly",
                new=AsyncMock(return_value="/tmp/out.mp3"),
            ) as mock_polly,
            patch(
                "kiro_crew.voice_reply._synthesize_system",
                new=AsyncMock(return_value="/tmp/system.wav"),
            ) as mock_system,
        ):
            out = await synthesize_speech(
                "**hello** world",
                provider=PROVIDER_SYSTEM,
                system_voice="Alex",
                rate="120%",
            )
        assert out == "/tmp/system.wav"
        mock_polly.assert_not_awaited()
        # Markdown is stripped before it reaches an engine that speaks it
        # literally, and the speed knob is the shared `rate` percentage.
        mock_system.assert_awaited_once_with("hello world", voice="Alex", rate="120%")

    @pytest.mark.asyncio
    async def test_system_empty_text_returns_none(self) -> None:
        with patch(
            "kiro_crew.voice_reply._synthesize_system",
            new=AsyncMock(return_value="/tmp/system.wav"),
        ) as mock_system:
            assert await synthesize_speech("**", provider=PROVIDER_SYSTEM) is None
        mock_system.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_none(self) -> None:
        assert await synthesize_speech("hi", provider="bogus") is None

    @pytest.mark.asyncio
    async def test_polly_empty_ssml_returns_none(self) -> None:
        # Pure markdown that strip_markdown reduces to empty yields empty ssml.
        with patch(
            "kiro_crew.voice_reply._synthesize_polly",
            new=AsyncMock(return_value=None),
        ) as mock_polly:
            out = await synthesize_speech("", provider=PROVIDER_POLLY)
        assert out is None
        mock_polly.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redacts_credentials_before_synthesis(self) -> None:
        """LLM output must be redacted for credentials before crossing into audio."""
        # AKIA... pattern is a typical AWS key shape caught by redact_credentials.
        raw = "secret AKIAIOSFODNN7EXAMPLE here"
        captured_text: list[str] = []

        async def capture_polly(ssml, **kwargs):
            captured_text.append(ssml)
            return "/tmp/out.mp3"

        with patch(
            "kiro_crew.voice_reply._synthesize_polly",
            side_effect=capture_polly,
        ):
            await synthesize_speech(raw, provider=PROVIDER_POLLY)

        assert captured_text, "polly should have been called"
        # The raw AKIA key should NOT appear in the SSML passed to Polly.
        assert "AKIAIOSFODNN7EXAMPLE" not in captured_text[0]


# ── upload_voice_to_slack() ──────────────────────────────────────────────


class TestUploadVoiceToSlack:
    @pytest.mark.asyncio
    async def test_mp3_filename_preserved(self, tmp_path) -> None:
        audio = tmp_path / "x.mp3"
        audio.write_bytes(b"x")
        client = MagicMock()
        client.upload_file = AsyncMock(return_value=None)
        assert await upload_voice_to_slack(client, "C1", "t1", str(audio)) is True
        kwargs = client.upload_file.call_args.kwargs
        assert kwargs["filename"] == "voice-reply.mp3"

    @pytest.mark.asyncio
    async def test_wav_filename_preserved(self, tmp_path) -> None:
        audio = tmp_path / "x.wav"
        audio.write_bytes(b"x")
        client = MagicMock()
        client.upload_file = AsyncMock(return_value=None)
        assert await upload_voice_to_slack(client, "C1", "t1", str(audio)) is True
        kwargs = client.upload_file.call_args.kwargs
        assert kwargs["filename"] == "voice-reply.wav"

    @pytest.mark.asyncio
    async def test_extensionless_defaults_to_mp3(self, tmp_path) -> None:
        audio = tmp_path / "no_ext"
        audio.write_bytes(b"x")
        client = MagicMock()
        client.upload_file = AsyncMock(return_value=None)
        assert await upload_voice_to_slack(client, "C1", "t1", str(audio)) is True
        kwargs = client.upload_file.call_args.kwargs
        assert kwargs["filename"] == "voice-reply.mp3"

    @pytest.mark.asyncio
    async def test_upload_exception_returns_false(self, tmp_path) -> None:
        audio = tmp_path / "x.mp3"
        audio.write_bytes(b"x")
        client = MagicMock()
        client.upload_file = AsyncMock(side_effect=RuntimeError("slack down"))
        assert await upload_voice_to_slack(client, "C1", "t1", str(audio)) is False


# ── voice_reply() end-to-end ────────────────────────────────────────────


class TestVoiceReplyEndToEnd:
    @pytest.mark.asyncio
    async def test_synthesis_fails_returns_false(self) -> None:
        client = MagicMock()
        with patch(
            "kiro_crew.voice_reply.synthesize_speech",
            new=AsyncMock(return_value=None),
        ):
            assert await voice_reply(client, "C1", "t1", "hi") is False

    @pytest.mark.asyncio
    async def test_success_uploads_and_unlinks(self, tmp_path) -> None:
        audio = tmp_path / "out.wav"
        audio.write_bytes(b"x" * 200)
        client = MagicMock()
        client.upload_file = AsyncMock(return_value=None)
        with patch(
            "kiro_crew.voice_reply.synthesize_speech",
            new=AsyncMock(return_value=str(audio)),
        ):
            ok = await voice_reply(
                client,
                "C1",
                "t1",
                "hello",
                provider=PROVIDER_SYSTEM,
            )
        assert ok is True
        # Temp file should have been unlinked after successful upload.
        assert not audio.exists()

    @pytest.mark.asyncio
    async def test_unlink_happens_even_on_upload_failure(self, tmp_path) -> None:
        audio = tmp_path / "out.mp3"
        audio.write_bytes(b"x" * 200)
        client = MagicMock()
        client.upload_file = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(
            "kiro_crew.voice_reply.synthesize_speech",
            new=AsyncMock(return_value=str(audio)),
        ):
            ok = await voice_reply(client, "C1", "t1", "hi")
        assert ok is False
        assert not audio.exists(), "temp audio must be cleaned up on upload failure"


# ── streaming_voice_reply() redaction ───────────────────────────────────


class TestStreamingVoiceReply:
    @pytest.mark.asyncio
    async def test_redacts_credentials_before_synthesis(self, tmp_path) -> None:
        from kiro_crew.voice_reply import streaming_voice_reply

        sentences_seen: list[str] = []

        async def fake_polly(ssml, **kwargs):
            sentences_seen.append(ssml)
            out = tmp_path / f"s{len(sentences_seen)}.mp3"
            out.write_bytes(b"x" * 200)
            return str(out)

        with patch(
            "kiro_crew.voice_reply._synthesize_polly",
            side_effect=fake_polly,
        ):
            gen = streaming_voice_reply("AKIAIOSFODNN7EXAMPLE is secret. Bye.")
            async for _idx, _sent, _bytes in gen:
                pass

        assert sentences_seen, "polly should have been called per sentence"
        for s in sentences_seen:
            assert "AKIAIOSFODNN7EXAMPLE" not in s

    @pytest.mark.asyncio
    async def test_skips_sentences_with_failed_synth(self, tmp_path) -> None:
        from kiro_crew.voice_reply import streaming_voice_reply

        calls = {"n": 0}

        async def alternating(ssml, **kwargs):
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                return None
            out = tmp_path / f"s{calls['n']}.mp3"
            out.write_bytes(b"x" * 200)
            return str(out)

        with patch(
            "kiro_crew.voice_reply._synthesize_polly",
            side_effect=alternating,
        ):
            collected = []
            async for idx, sent, data in streaming_voice_reply(
                "First. Second. Third.",
            ):
                collected.append(idx)

        # Only the odd-numbered calls succeed (1, 3).
        assert collected == [0, 2]


class TestStreamingVoiceReplyConsent:
    """The dashboard stream raises only when no consent is recorded.

    ``_polly_consented`` grants Polly for the default profile+region with a
    matching account probe; each case below alters one of those three.
    """

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, _floor_monkeypatch, _polly_consented):
        # See TestSynthesizePolly._passthrough_sandbox.
        _floor_monkeypatch.setattr(
            "kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        _patch_aws_on_path(_floor_monkeypatch)

    @staticmethod
    def _fake_exec(spoken: list[str]):
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            spoken.append(cmd[-2])
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        return fake_exec

    @staticmethod
    async def _collect(**kwargs) -> list[int]:
        from kiro_crew.voice_reply import streaming_voice_reply

        stream = streaming_voice_reply("First. Second.", **kwargs)
        return [idx async for idx, _sent, _data in stream]

    @pytest.mark.asyncio
    async def test_a_failed_account_check_skips_only_that_sentence(self, monkeypatch) -> None:
        """The grant stands; the account could not be re-checked once.

        The first sentence is skipped and the second is checked again and
        spoken, so a transient fault never ends the read aloud with a notice
        telling the person to confirm a consent they already gave.
        """
        from kiro_crew import aws_consent

        probes: list[str] = []

        async def _flaky(_profile: str, _region: str, *, use_cache: bool = True):
            probes.append(_profile)
            if len(probes) == 1:
                return aws_consent.Identity(ok=False, detail="STS unreachable")
            return aws_consent.Identity(ok=True, account="111122223333")

        monkeypatch.setattr(aws_consent, "probe_identity", _flaky)
        spoken: list[str] = []
        with (
            patch.object(aws_consent, "audit_decision") as audit,
            patch("asyncio.create_subprocess_exec", side_effect=self._fake_exec(spoken)),
        ):
            collected = await self._collect()

        assert collected == [1]
        assert len(spoken) == 1 and "Second." in spoken[0]
        assert len(probes) == 2
        assert [c.kwargs.get("outcome") for c in audit.call_args_list] == ["denied", "verified"]

    @pytest.mark.asyncio
    async def test_no_matching_grant_raises_at_the_first_sentence(self) -> None:
        """Consent recorded for another region: fixed text, refused once."""
        from kiro_crew import aws_consent
        from kiro_crew.voice_reply import VoiceSynthesisError

        with (
            patch.object(aws_consent, "audit_decision") as audit,
            patch("asyncio.create_subprocess_exec") as spawn,
            pytest.raises(VoiceSynthesisError) as refused,
        ):
            await self._collect(region="us-east-1")

        assert refused.value.code == "voice_consent_required"
        assert str(refused.value) == (
            "Amazon Polly use is not confirmed for this profile and region. "
            "Nothing was sent to AWS. Confirm it in Settings -> Voice."
        )
        assert "us-east-1" not in str(refused.value)
        assert [c.kwargs.get("outcome") for c in audit.call_args_list] == ["denied"]
        assert spawn.call_count == 0

    @pytest.mark.asyncio
    async def test_consent_withdrawn_mid_reply_keeps_the_spoken_part(self, monkeypatch) -> None:
        """The account under the profile changes after the first sentence.

        The first sentence is already spoken. The gate then withdraws the grant,
        so the second sentence raises and the person sees the consent notice
        after the audio they already heard.
        """
        from kiro_crew import aws_consent
        from kiro_crew.voice_reply import VoiceSynthesisError, streaming_voice_reply

        probes: list[str] = []

        async def _repointed(_profile: str, _region: str, *, use_cache: bool = True):
            probes.append(_profile)
            account = "111122223333" if len(probes) == 1 else "999988887777"
            return aws_consent.Identity(ok=True, account=account)

        monkeypatch.setattr(aws_consent, "probe_identity", _repointed)
        spoken: list[str] = []
        collected: list[int] = []
        with (
            patch.object(aws_consent, "audit_decision"),
            patch("asyncio.create_subprocess_exec", side_effect=self._fake_exec(spoken)),
            pytest.raises(VoiceSynthesisError) as refused,
        ):
            async for idx, _sent, _data in streaming_voice_reply("First. Second."):
                collected.append(idx)

        assert collected == [0]
        assert len(spoken) == 1 and "First." in spoken[0]
        assert refused.value.code == "voice_consent_required"
        assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is None

    @pytest.mark.asyncio
    async def test_unattended_call_returns_none_on_either_refusal(self) -> None:
        """Without ``raise_refusal`` a refusal is "no audio", whatever its cause."""
        from kiro_crew import aws_consent

        unknown = aws_consent.Identity(ok=False, detail="STS unreachable")
        with (
            patch.object(aws_consent, "probe_identity", AsyncMock(return_value=unknown)),
            patch("asyncio.create_subprocess_exec") as spawn,
        ):
            assert await _synthesize_polly("<speak>hi</speak>") is None
            assert await _synthesize_polly("<speak>hi</speak>", region="us-east-1") is None
        assert spawn.call_count == 0


class TestTextTypeAutoDetection:
    """Tests for --text-type dynamic selection (ssml vs text)."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _passthrough_sandbox(self, monkeypatch, _polly_consented):
        # See TestSynthesizePolly._passthrough_sandbox.
        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None))
        _patch_aws_on_path(monkeypatch)

    @pytest.mark.asyncio
    async def test_ssml_input_uses_ssml_text_type(self, tmp_path) -> None:
        proc = _mock_subprocess(returncode=0)
        captured_cmd: list[str] = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly("<speak><prosody>hello</prosody></speak>")
        assert result is not None
        os.unlink(result)
        idx = captured_cmd.index("--text-type")
        assert captured_cmd[idx + 1] == "ssml"

    @pytest.mark.asyncio
    async def test_plain_text_input_uses_text_type(self, tmp_path) -> None:
        proc = _mock_subprocess(returncode=0)
        captured_cmd: list[str] = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly("Hello world plain text")
        assert result is not None
        os.unlink(result)
        idx = captured_cmd.index("--text-type")
        assert captured_cmd[idx + 1] == "text"

    @pytest.mark.asyncio
    async def test_neural_engine_text_to_ssml_returns_ssml_without_prosody(self) -> None:
        """Neural engine should return SSML with break tags but no prosody wrapper."""
        result = text_to_ssml("Hello world", engine="neural")
        assert result.startswith("<speak>")
        assert "</speak>" in result
        assert "<prosody" not in result

    @pytest.mark.asyncio
    async def test_standard_engine_text_to_ssml_returns_ssml(self) -> None:
        """Standard engine should return SSML."""
        result = text_to_ssml("Hello world", engine="standard")
        assert result.startswith("<speak")
        assert "</speak>" in result


# ── stitch_mp3s() failure-path cleanup ──────────────────────────────────


class TestStitchMp3s:
    """Failure exits must not leak the internally allocated (mkstemp) output.

    When the caller supplies no ``output``, ``stitch_mp3s`` allocates one via
    ``mkstemp``. On any unsuccessful exit it returns ``None``, so no caller
    ever receives that path — the file must be removed before returning. A
    caller-supplied ``output`` is never owned by the function and must stay
    untouched on failure.
    """

    @staticmethod
    def _two_inputs(tmp_path) -> list[str]:
        """Two fake MP3 inputs — one path short-circuits before ffmpeg runs."""
        paths = []
        for name in ("a.mp3", "b.mp3"):
            p = tmp_path / name
            p.write_bytes(b"fake-mp3")
            paths.append(str(p))
        return paths

    @pytest.mark.asyncio
    async def test_spawn_failure_removes_owned_temp(self, tmp_path, monkeypatch) -> None:
        allocated = _capture_mkstemp(monkeypatch)

        async def fake_exec(*cmd, **kwargs):
            raise FileNotFoundError("ffmpeg not on PATH")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(self._two_inputs(tmp_path))

        assert result is None
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_nonzero_exit_removes_owned_temp(self, tmp_path, monkeypatch) -> None:
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=1, stderr=b"concat error")

        async def fake_exec(*cmd, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(self._two_inputs(tmp_path))

        assert result is None
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_timeout_kills_child_and_removes_owned_temp(self, tmp_path, monkeypatch) -> None:
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        async def fake_exec(*cmd, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(self._two_inputs(tmp_path))

        assert result is None
        # wait_for cancels communicate() but does not terminate the child;
        # the child must be killed and reaped via communicate() (which drains
        # the pipes) BEFORE the unlink, or Windows refuses to remove the
        # still-open output file. Using wait() instead of communicate() can
        # hang when the child is blocked writing to a full stderr PIPE.
        proc.kill.assert_called_once()
        proc.communicate.assert_awaited()
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_cancellation_kills_child_and_removes_owned_temp(
        self, tmp_path, monkeypatch
    ) -> None:
        # CancelledError is a BaseException: it bypasses ``except Exception``,
        # so only a finally-based invariant discards the owned output here.
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)

        async def fake_exec(*cmd, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(asyncio.CancelledError):
                await stitch_mp3s(self._two_inputs(tmp_path))

        proc.kill.assert_called_once()
        proc.communicate.assert_awaited()
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_empty_output_removes_owned_temp(self, tmp_path, monkeypatch) -> None:
        # The mkstemp allocation always exists on disk, so "ffmpeg produced no
        # output" manifests as a zero-byte file, never an absent one.
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            return proc  # exits 0 but writes nothing to the output path

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(self._two_inputs(tmp_path))

        assert result is None
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])

    @pytest.mark.asyncio
    async def test_failure_leaves_caller_supplied_output_untouched(
        self, tmp_path, monkeypatch
    ) -> None:
        allocated = _capture_mkstemp(monkeypatch)
        caller_output = tmp_path / "caller.mp3"
        caller_output.write_bytes(b"pre-existing caller data")
        proc = _mock_subprocess(returncode=1)

        async def fake_exec(*cmd, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(self._two_inputs(tmp_path), output=str(caller_output))

        assert result is None
        assert allocated == []  # caller supplied the path; nothing was allocated
        assert caller_output.exists()
        assert caller_output.read_bytes() == b"pre-existing caller data"

    @pytest.mark.asyncio
    async def test_success_returns_owned_output_intact(self, tmp_path, monkeypatch) -> None:
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[-1], "wb") as f:  # output path is the last argv entry
                f.write(b"stitched-mp3-data")
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(self._two_inputs(tmp_path))

        try:
            assert len(allocated) == 1
            assert result == allocated[0]
            assert os.path.exists(result)
            assert os.path.getsize(result) > 0
        finally:
            # Not on the happy path: a failing assert above must not leave
            # the mkstemp file behind as test residue.
            if allocated and os.path.exists(allocated[0]):
                os.unlink(allocated[0])

    @pytest.mark.asyncio
    async def test_timeout_reaps_child_via_communicate_not_wait(
        self, tmp_path, monkeypatch
    ) -> None:
        """After a timeout kills the ffmpeg child, the cleanup must call
        ``communicate()`` -- not ``wait()`` -- so that PIPE buffers are
        drained. A child blocked writing to a full stderr PIPE would hang
        the event loop if only ``wait()`` were used."""
        allocated = _capture_mkstemp(monkeypatch)
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        async def fake_exec(*cmd, **kwargs):
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(self._two_inputs(tmp_path))

        assert result is None
        proc.kill.assert_called_once()
        # The critical pin: reap via communicate(), not wait(). The stitch
        # call itself awaits communicate once; the reap must award a SECOND
        # await, and wait() must never be touched.
        assert proc.communicate.await_count == 2
        proc.wait.assert_not_awaited()
        assert len(allocated) == 1
        assert not os.path.exists(allocated[0])


class TestSynthesizeSpeechDoesNotSwallowARefusal:
    """The seam between the provider and the endpoint.

    The provider raises and the endpoint relays, but ``synthesize_speech`` sits
    between them, so it is where a swallowed refusal would go unnoticed. The
    endpoint tests patch ``synthesize_speech`` itself, so without this the middle
    link is covered by nothing and a future catch-all added here would ship
    silently.
    """

    @pytest.mark.asyncio
    async def test_system_refusal_travels_through_synthesize_speech(self, monkeypatch):
        from kiro_crew.sandbox import SandboxUnavailableError
        from kiro_crew.voice_reply import synthesize_speech

        prose = "SEAM-SENTINEL: the sandbox refused"

        # Patch the seam the spawn actually goes through. Reaching the real
        # sandbox layer here would make the test pass for the wrong reason on a
        # host that genuinely refuses, and vacuously on one that does not.
        async def refuse(cmd, **kw):
            raise SandboxUnavailableError(prose, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.voice_reply.sandboxed_spawn_argv_async", refuse)
        monkeypatch.setattr(
            "kiro_crew.voice_reply.resolve_system_tts",
            lambda: (SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak-ng"),
        )

        with pytest.raises(SandboxUnavailableError) as caught:
            await synthesize_speech("hello", provider=PROVIDER_SYSTEM)
        assert prose in str(caught.value)

    @pytest.mark.asyncio
    async def test_polly_refusal_travels_through_synthesize_speech(
        self, monkeypatch, _polly_consented
    ):
        from kiro_crew.sandbox import SandboxUnavailableError
        from kiro_crew.voice_reply import synthesize_speech

        prose = "SEAM-SENTINEL: the sandbox refused"

        # Polly refuses without recorded consent and without a resolvable CLI, and
        # both bail BEFORE the spawn -- so without these the test would pass for
        # the wrong reason on a host that simply has no aws binary.
        _patch_aws_on_path(monkeypatch)

        # Patch the seam the spawn actually goes through. Reaching the real
        # sandbox layer here would make the test pass for the wrong reason on a
        # host that genuinely refuses, and vacuously on one that does not.
        def refuse(cmd, mode):
            raise SandboxUnavailableError(prose, "no_backend", "not Linux")

        # Polly deliberately keeps an UNSCRUBBED env (the AWS CLI authenticates
        # from it), so it wraps through `wrap_argv_async` rather than the
        # credential-scrubbing spawn the local engines use. Patch the seam this
        # path really takes, or the spawn proceeds and the test proves nothing.
        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", refuse)

        with pytest.raises(SandboxUnavailableError) as caught:
            await synthesize_speech("hello", provider="polly")
        assert prose in str(caught.value)
