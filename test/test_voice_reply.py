"""Tests for voice_reply module — Polly + Piper TTS integration."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.voice_reply import (
    DEFAULT_LENGTH_SCALE,
    DEFAULT_PITCH,
    DEFAULT_PROVIDER,
    DEFAULT_RATE,
    PROVIDER_PIPER,
    PROVIDER_POLLY,
    VALID_ENGINES,
    VALID_PROVIDERS,
    _resolve_piper_binary,
    _synthesize_piper,
    _synthesize_polly,
    _validate_pitch,
    _validate_rate,
    is_available,
    split_sentences,
    strip_markdown,
    synthesize_speech,
    text_to_ssml,
    upload_voice_to_slack,
    validate_length_scale,
    voice_reply,
)


class TestStripMarkdown:
    def test_removes_code_blocks(self) -> None:
        assert strip_markdown("before ```code``` after") == "before (code block) after"

    def test_removes_inline_code(self) -> None:
        assert strip_markdown("use `foo` here") == "use foo here"

    def test_removes_slack_links(self) -> None:
        assert strip_markdown("<https://example.com|Example>") == "Example"
        assert strip_markdown("<https://example.com>") == "(link)"

    def test_removes_markdown_links(self) -> None:
        assert strip_markdown("[click](https://example.com)") == "click"

    def test_removes_bold_italic(self) -> None:
        assert strip_markdown("**bold** and *italic*") == "bold and italic"

    def test_removes_emoji_shortcodes(self) -> None:
        assert strip_markdown("hello :wave: world") == "hello world"

    def test_preserves_plain_text(self) -> None:
        assert strip_markdown("hello world") == "hello world"

    def test_collapses_whitespace(self) -> None:
        assert strip_markdown("a\n\n\n\nb") == "a\n\nb"

    def test_preserves_bullet_lists(self) -> None:
        result = strip_markdown("- item one\n- item two")
        assert "item one" in result
        assert "item two" in result


class TestTextToSsml:
    def test_empty_input(self) -> None:
        assert text_to_ssml("") == ""

    def test_basic_ssml(self) -> None:
        result = text_to_ssml("Hello world")
        assert result.startswith("<speak>")
        assert result.endswith("</speak>")
        assert "Hello world" in result

    def test_includes_rate(self) -> None:
        result = text_to_ssml("test", rate="110%")
        assert 'rate="110%"' in result

    def test_returns_ssml_without_prosody_for_neural(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="neural")
        assert result.startswith("<speak>")
        assert "</speak>" in result
        assert "<prosody" not in result
        assert "pitch" not in result

    def test_neural_escapes_xml_entities(self) -> None:
        result = text_to_ssml("a & b < c > d", engine="neural")
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result
        assert "<prosody" not in result

    def test_neural_adds_break_tags(self) -> None:
        result = text_to_ssml("para one\n\npara two", engine="neural")
        assert 'break time="600ms"' in result
        assert "<prosody" not in result

    def test_neural_truncates_long_text(self) -> None:
        long_text = "word. " * 1000
        result = text_to_ssml(long_text, engine="neural")
        assert result.startswith("<speak>")
        assert "</speak>" in result
        assert "<prosody" not in result
        # Should be truncated
        assert len(result) < len(long_text) + 100

    def test_excludes_pitch_for_generative(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="generative")
        assert "pitch" not in result

    def test_excludes_pitch_for_long_form(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="long-form")
        assert "pitch" not in result

    def test_escapes_xml_entities(self) -> None:
        result = text_to_ssml("a & b < c > d")
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result

    def test_truncates_long_text(self) -> None:
        long_text = "word. " * 1000
        result = text_to_ssml(long_text)
        # Should end with a period (sentence boundary truncation)
        assert result.endswith("</prosody></speak>")

    def test_adds_paragraph_breaks(self) -> None:
        result = text_to_ssml("para one\n\npara two")
        assert 'break time="600ms"' in result

    def test_adds_line_breaks(self) -> None:
        result = text_to_ssml("line one\nline two")
        assert 'break time="300ms"' in result


class TestValidation:
    def test_valid_rate(self) -> None:
        assert _validate_rate("95%") == "95%"
        assert _validate_rate("110%") == "110%"
        assert _validate_rate("50%") == "50%"

    def test_invalid_rate_returns_default(self) -> None:
        assert _validate_rate("banana") == DEFAULT_RATE
        assert _validate_rate("") == DEFAULT_RATE
        assert _validate_rate("1000%") == DEFAULT_RATE

    def test_valid_pitch(self) -> None:
        assert _validate_pitch("+10%") == "+10%"
        assert _validate_pitch("-5%") == "-5%"
        assert _validate_pitch("+0%") == "+0%"

    def test_invalid_pitch_returns_default(self) -> None:
        assert _validate_pitch("banana") == DEFAULT_PITCH
        assert _validate_pitch("10%") == DEFAULT_PITCH  # missing +/-
        assert _validate_pitch("") == DEFAULT_PITCH

    def test_valid_length_scale(self) -> None:
        assert validate_length_scale(1.5) == 1.5
        assert validate_length_scale("0.85") == 0.85
        assert validate_length_scale(2) == 2.0

    def test_invalid_length_scale_returns_default(self) -> None:
        # Non-numeric, non-finite, zero/negative, and OverflowError (huge int)
        # all fall back to the default rather than reaching synthesis or being
        # persisted as unserializable JSON.
        for bad in ["fast", None, float("inf"), float("nan"), 0, -1.0, 10 ** 400, [1]]:
            assert validate_length_scale(bad) == DEFAULT_LENGTH_SCALE

    def test_valid_engines(self) -> None:
        assert "neural" in VALID_ENGINES
        assert "generative" in VALID_ENGINES
        assert "long-form" in VALID_ENGINES
        assert "standard" in VALID_ENGINES
        assert "invalid" not in VALID_ENGINES


class TestSplitSentences:
    def test_multi_sentence(self) -> None:
        assert split_sentences("Hello world. How are you?") == [
            "Hello world.",
            "How are you?",
        ]

    def test_single_sentence(self) -> None:
        assert split_sentences("Hello world.") == ["Hello world."]

    def test_empty_input(self) -> None:
        assert split_sentences("") == []

    def test_strips_markdown_before_splitting(self) -> None:
        assert split_sentences("**Bold sentence.** Another one.") == [
            "Bold sentence.",
            "Another one.",
        ]


# ── Helpers ──────────────────────────────────────────────────────────────


def _mock_subprocess(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> AsyncMock:
    """Return an AsyncMock shaped like asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _make_executable(path: str) -> None:
    """Touch *path* and flag it executable so shutil.which / os.access pass."""
    with open(path, "wb") as f:
        f.write(b"#!/bin/sh\n")
    os.chmod(path, 0o755)


# _synthesize_polly() short-circuits to None when the `aws` CLI is absent, so any
# test that exercises the argv build or the subprocess lifecycle must state that
# the CLI is present. It is NOT present on a stock Windows box (nor on a minimal
# Linux CI image), so relying on the ambient host makes those tests silently
# host-dependent rather than deterministic.
_FAKE_AWS_CLI = "aws.exe" if os.name == "nt" else "/usr/bin/aws"


# Stands in for the remedy prose sandbox.wrap_argv builds for kind="no_backend".
# The handlers under test must RELAY this string, not compose their own copy —
# only this kind names the opt-in, so a hardcoded remedy would be wrong for the
# "transient" and "foreign_sandbox" kinds.
_SANDBOX_REMEDY = (
    "No OS-level sandbox backend is available on this host. If this host "
    "genuinely lacks a sandbox backend, set "
    "agent.sandbox_allow_unsandboxed_exec=true in ~/.kiro/crew/config.json."
)


def _patch_aws_on_path(monkeypatch) -> None:
    """Make ``shutil.which`` report the ``aws`` CLI present, others absent."""
    monkeypatch.setattr(
        "kiro_crew.voice_reply.shutil.which",
        lambda name, *a, **k: _FAKE_AWS_CLI if name == "aws" else None,
    )


@pytest.fixture(autouse=True)
def _no_argv_prefixers(monkeypatch):
    """Strip the host-dependent argv prefixes for every test in this module.

    Two layers sit between the command these tests build and the
    ``create_subprocess_exec`` they mock, and BOTH prepend to the argv:

    * ``cgroup_scope_argv`` — prepends a launcher on a cgroup-v2 host.
    * ``create_subprocess_limited`` — prepends an RLIMIT shim that re-``exec``s
      in place, so the real argv[0] becomes a python interpreter path.

    Either one displaces argv[0] and makes an assertion about the built command
    host-dependent: green wherever the host offers neither (Windows, an
    unprivileged macOS box) and red on a Linux runner that offers both. Both are
    pinned module-wide rather than per-test so a new test cannot silently inherit
    the same host dependence. A test specifically about resource limits or cgroup
    scoping should patch the real function back.
    """
    monkeypatch.setattr("kiro_crew.voice_reply.cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(
        "kiro_crew.voice_reply.create_subprocess_limited",
        lambda *argv, **kw: asyncio.create_subprocess_exec(*argv, **kw),
    )


# ── Provider constants ──────────────────────────────────────────────────


class TestProviderConstants:
    def test_constants_defined(self) -> None:
        assert PROVIDER_POLLY == "polly"
        assert PROVIDER_PIPER == "piper"
        # Piper (local offline TTS) is the documented recommended default —
        # it works without AWS credentials. Polly stays valid when explicitly selected.
        assert DEFAULT_PROVIDER == PROVIDER_PIPER
        assert PROVIDER_POLLY in VALID_PROVIDERS
        assert PROVIDER_PIPER in VALID_PROVIDERS


# ── is_available() ──────────────────────────────────────────────────────


class TestIsAvailable:
    def test_polly_available_when_aws_on_path(self) -> None:
        with patch("kiro_crew.voice_reply.shutil.which", return_value="/usr/bin/aws"):
            assert is_available(PROVIDER_POLLY) is True

    def test_polly_unavailable_when_aws_missing(self) -> None:
        with patch("kiro_crew.voice_reply.shutil.which", return_value=None):
            assert is_available(PROVIDER_POLLY) is False

    def test_piper_unavailable_when_binary_missing(self, tmp_path) -> None:
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"fake")
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=None,
        ):
            assert is_available(
                PROVIDER_PIPER, piper_binary="", piper_model=str(model),
            ) is False

    def test_piper_unavailable_when_model_empty(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ):
            assert is_available(PROVIDER_PIPER, piper_model="") is False

    def test_piper_unavailable_when_model_file_missing(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ):
            # Model path provided but file doesn't exist.
            assert is_available(
                PROVIDER_PIPER, piper_model=str(tmp_path / "nope.onnx"),
            ) is False

    def test_piper_available_when_binary_and_model_exist(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"fake model")
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ):
            assert is_available(
                PROVIDER_PIPER, piper_model=str(model),
            ) is True

    def test_unknown_provider_returns_false(self, caplog) -> None:
        assert is_available("bogus") is False


# ── _resolve_piper_binary() ─────────────────────────────────────────────


class TestResolvePiperBinary:
    def test_configured_path_preferred(self, tmp_path) -> None:
        bin_path = tmp_path / "my-piper"
        _make_executable(str(bin_path))
        assert _resolve_piper_binary(str(bin_path)) == str(bin_path)

    def test_configured_path_missing_returns_none(self, tmp_path) -> None:
        assert _resolve_piper_binary(str(tmp_path / "nope")) is None

    def test_configured_path_not_executable_returns_none(self, tmp_path) -> None:
        p = tmp_path / "not-exec"
        p.write_bytes(b"")  # exists but not chmod +x
        assert _resolve_piper_binary(str(p)) is None

    def test_configured_expanduser(self, tmp_path, monkeypatch) -> None:
        bin_path = tmp_path / "piper-home"
        _make_executable(str(bin_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _resolve_piper_binary("~/piper-home") == str(bin_path)

    def test_falls_back_to_path(self, tmp_path) -> None:
        with patch(
            "kiro_crew.voice_reply.shutil.which", return_value="/usr/local/bin/piper",
        ), patch("os.path.isfile", return_value=False):
            assert _resolve_piper_binary("") == "/usr/local/bin/piper"

    def test_falls_back_to_venv(self, tmp_path, monkeypatch) -> None:
        venv_bin = tmp_path / "piper-venv" / "bin"
        venv_bin.mkdir(parents=True)
        bin_path = venv_bin / "piper"
        _make_executable(str(bin_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("kiro_crew.voice_reply.shutil.which", return_value=None):
            assert _resolve_piper_binary("") == str(bin_path)

    def test_nothing_found_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))  # no venv exists
        with patch("kiro_crew.voice_reply.shutil.which", return_value=None):
            assert _resolve_piper_binary("") is None


# ── _synthesize_piper() ──────────────────────────────────────────────────


class TestSynthesizePiper:
    @pytest.mark.asyncio
    async def test_binary_not_found_returns_none(self) -> None:
        with patch("kiro_crew.voice_reply._resolve_piper_binary", return_value=None):
            assert await _synthesize_piper("hi") is None

    @pytest.mark.asyncio
    async def test_model_missing_returns_none(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ):
            # Empty model
            assert await _synthesize_piper("hi", piper_model="") is None
            # Nonexistent file
            assert await _synthesize_piper(
                "hi", piper_model=str(tmp_path / "missing.onnx"),
            ) is None

    @pytest.mark.asyncio
    async def test_success_returns_wav_path(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=0)

        def fake_wrap(cmd, mode):
            return cmd, None  # no sandbox, no cleanup

        # The synthesized file needs size >= 100 to be considered valid.
        async def fake_exec(*cmd, **kwargs):
            # The output file is the arg after "-f"
            out_idx = cmd.index("-f") + 1
            out_path = cmd[out_idx]
            with open(out_path, "wb") as f:
                f.write(b"RIFF" + b"x" * 200)
            return proc

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch("kiro_crew.voice_reply.wrap_argv", side_effect=fake_wrap), patch(
            "asyncio.create_subprocess_exec", side_effect=fake_exec,
        ):
            result = await _synthesize_piper(
                "hello", piper_model=str(model),
            )
        assert result is not None
        assert result.endswith(".wav")
        assert os.path.isfile(result)
        os.unlink(result)

    @pytest.mark.asyncio
    async def test_success_with_config_and_length_scale(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        cfg = tmp_path / "voice.onnx.json"
        cfg.write_text("{}")

        proc = _mock_subprocess(returncode=0)
        captured_cmd: list[str] = []

        def fake_wrap(cmd, mode):
            captured_cmd.extend(cmd)
            return cmd, None

        async def fake_exec(*cmd, **kwargs):
            out_path = cmd[cmd.index("-f") + 1]
            with open(out_path, "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch("kiro_crew.voice_reply.wrap_argv", side_effect=fake_wrap), patch(
            "asyncio.create_subprocess_exec", side_effect=fake_exec,
        ):
            result = await _synthesize_piper(
                "hello",
                piper_model=str(model),
                piper_model_config=str(cfg),
                length_scale=0.9,
            )
        assert result is not None
        os.unlink(result)
        # Config + length-scale should be present in cmd.
        assert "-c" in captured_cmd
        assert str(cfg) in captured_cmd
        assert "--length-scale" in captured_cmd
        assert "0.9" in captured_cmd

    @pytest.mark.asyncio
    async def test_nonzero_returncode_unlinks_and_returns_none(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=1, stderr=b"bad voice")

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=lambda c, mode: (c, None),
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc,
        ):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None

    @pytest.mark.asyncio
    async def test_output_too_small_unlinks_and_returns_none(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            out_path = cmd[cmd.index("-f") + 1]
            with open(out_path, "wb") as f:
                f.write(b"tiny")  # < 100 bytes
            return proc

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=lambda c, mode: (c, None),
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=fake_exec,
        ):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None

    @pytest.mark.asyncio
    async def test_timeout_kills_and_returns_none(self, tmp_path) -> None:
        import asyncio as _asyncio

        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=0)

        async def hang_wait_for(coro, timeout=None):
            coro.close()
            raise _asyncio.TimeoutError()

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=lambda c, mode: (c, None),
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc,
        ), patch("asyncio.wait_for", side_effect=hang_wait_for):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()

    @pytest.mark.asyncio
    async def test_timeout_survives_process_lookup_error(self, tmp_path) -> None:
        """If proc.kill() raises ProcessLookupError, synthesize still returns None cleanly."""
        import asyncio as _asyncio

        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=0)
        proc.kill.side_effect = ProcessLookupError

        async def hang_wait_for(coro, timeout=None):
            coro.close()
            raise _asyncio.TimeoutError()

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=lambda c, mode: (c, None),
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc,
        ), patch("asyncio.wait_for", side_effect=hang_wait_for):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None

    @pytest.mark.asyncio
    async def test_exec_exception_returns_none(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=lambda c, mode: (c, None),
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("boom"),
        ):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_returns_none_and_unlinks(
        self, tmp_path, monkeypatch, caplog,
    ) -> None:
        """A fail-closed sandbox is reported with its remedy, not as a generic error.

        Mirrors the Polly counterpart: no OS sandbox backend (every Windows host,
        Linux without user namespaces) makes wrap_argv raise, and piper must
        degrade to None, unlink the temp WAV, and relay the sandbox layer's own
        remedy prose rather than logging a stack trace that reads as a
        binary/model fault.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*a, **k):
            fd, p = real_mkstemp(*a, **k)
            created.append(p)
            return fd, p

        monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", tracking_mkstemp)

        def refuse(cmd, mode):
            raise SandboxUnavailableError(_SANDBOX_REMEDY, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", refuse)
        monkeypatch.setattr(
            "kiro_crew.voice_reply._resolve_piper_binary", lambda *a, **k: str(bin_path)
        )

        with caplog.at_level("ERROR", logger="kiro_crew.voice_reply"):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None

        assert created, "piper should have allocated a temp wav"
        assert not os.path.exists(created[0]), "temp wav must be unlinked"
        assert _SANDBOX_REMEDY in caplog.text
        assert "no_backend" in caplog.text
        assert "piper synthesis error" not in caplog.text

    @pytest.mark.asyncio
    async def test_sandbox_cleanup_unlinked(self, tmp_path) -> None:
        """If wrap_argv returns a cleanup path, it must be unlinked after exit."""
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        cleanup_path = tmp_path / "sandbox-profile"
        cleanup_path.write_text("profile")

        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            out_path = cmd[cmd.index("-f") + 1]
            with open(out_path, "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary", return_value=str(bin_path),
        ), patch(
            "kiro_crew.voice_reply.wrap_argv",
            side_effect=lambda c, mode: (c, str(cleanup_path)),
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=fake_exec,
        ):
            result = await _synthesize_piper("hello", piper_model=str(model))

        assert result is not None
        os.unlink(result)
        assert not cleanup_path.exists(), "sandbox cleanup file should be removed"


# ── _synthesize_polly() ──────────────────────────────────────────────────


class TestSynthesizePolly:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        # _synthesize_polly() calls wrap_argv before create_subprocess_exec.
        # wrap_argv fail-closes on any host with no OS sandbox backend (macOS 26,
        # every Windows host), which is caught and returns None. Patch to
        # passthrough so the existing create_subprocess_exec mocks run.
        monkeypatch.setattr(
            "kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        # cgroup_scope_argv is neutralized module-wide by _no_cgroup_scope.
        _patch_aws_on_path(monkeypatch)

    @pytest.mark.asyncio
    async def test_invalid_engine_falls_back_to_default(self, tmp_path) -> None:
        proc = _mock_subprocess(returncode=0)

        captured_cmd: list[str] = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            # Write fake MP3 to the final positional path arg.
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly(
                "<speak>hi</speak>", engine="invalid-engine",
            )
        assert result is not None
        os.unlink(result)
        # Engine defaults to 'generative' (DEFAULT_ENGINE) on invalid input.
        assert "generative" in captured_cmd

    @pytest.mark.asyncio
    async def test_profile_and_region_passed_through(self, tmp_path) -> None:
        proc = _mock_subprocess(returncode=0)

        captured: list[str] = []

        async def fake_exec(*cmd, **kwargs):
            captured.extend(cmd)
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly(
                "<speak>hi</speak>",
                aws_profile="my-profile",
                region="us-east-2",
            )
        assert result is not None
        os.unlink(result)
        assert "--profile" in captured and "my-profile" in captured
        assert "--region" in captured and "us-east-2" in captured

    @pytest.mark.asyncio
    async def test_nonzero_rc_returns_none(self) -> None:
        proc = _mock_subprocess(returncode=1, stderr=b"denied")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_output_too_small_returns_none(self) -> None:
        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"tiny")
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self) -> None:
        with patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("no aws"),
        ):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_applies_wrap_argv_sandbox(self, tmp_path) -> None:
        """``aws polly`` consumes LLM-derived SSML on argv -- must be sandboxed."""
        proc = _mock_subprocess(returncode=0)
        wrap_called = {"n": 0}

        def fake_wrap(cmd, mode):
            wrap_called["n"] += 1
            assert mode == "standard", "polly should use standard sandbox mode"
            return cmd, None  # no cleanup file

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=fake_wrap,
        ), patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly("<speak>hi</speak>")
        assert result is not None
        os.unlink(result)
        assert wrap_called["n"] == 1, "wrap_argv must be invoked exactly once"

    @pytest.mark.asyncio
    async def test_timeout_kills_and_returns_none(self, tmp_path) -> None:
        """Polly timeout must kill the subprocess so it doesn't linger."""
        import asyncio as _asyncio

        proc = _mock_subprocess(returncode=0)

        async def hang_wait_for(coro, timeout=None):
            coro.close()
            raise _asyncio.TimeoutError()

        with patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=lambda c, mode: (c, None),
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc,
        ), patch("asyncio.wait_for", side_effect=hang_wait_for):
            assert await _synthesize_polly("<speak>hi</speak>") is None

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()

    @pytest.mark.asyncio
    async def test_timeout_survives_process_lookup_error(self, tmp_path) -> None:
        """Polly timeout path tolerates ProcessLookupError on kill (already-exited child)."""
        import asyncio as _asyncio

        proc = _mock_subprocess(returncode=0)
        proc.kill.side_effect = ProcessLookupError

        async def hang_wait_for(coro, timeout=None):
            coro.close()
            raise _asyncio.TimeoutError()

        with patch(
            "kiro_crew.voice_reply.wrap_argv", side_effect=lambda c, mode: (c, None),
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc,
        ), patch("asyncio.wait_for", side_effect=hang_wait_for):
            assert await _synthesize_polly("<speak>hi</speak>") is None

    @pytest.mark.asyncio
    async def test_sandbox_cleanup_unlinked(self, tmp_path) -> None:
        """If wrap_argv returns a cleanup path, polly must unlink it after exit."""
        cleanup_path = tmp_path / "polly-sandbox-profile"
        cleanup_path.write_text("profile")

        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * 200)
            return proc

        with patch(
            "kiro_crew.voice_reply.wrap_argv",
            side_effect=lambda c, mode: (c, str(cleanup_path)),
        ), patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _synthesize_polly("<speak>hi</speak>")
        assert result is not None
        os.unlink(result)
        assert not cleanup_path.exists(), "polly sandbox cleanup file should be removed"

    @pytest.mark.asyncio
    async def test_aws_cli_missing_short_circuits_before_spawn(self, monkeypatch) -> None:
        """Absent ``aws`` CLI degrades to None without attempting a spawn.

        The guard must run BEFORE create_subprocess_exec: reaching the spawn
        would raise FileNotFoundError instead of degrading gracefully.
        """
        monkeypatch.setattr(
            "kiro_crew.voice_reply.shutil.which", lambda name, *a, **k: None
        )
        spawned = {"n": 0}

        async def fake_exec(*cmd, **kwargs):
            spawned["n"] += 1
            raise AssertionError("must not spawn when the aws CLI is absent")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            assert await _synthesize_polly("<speak>hi</speak>") is None
        assert spawned["n"] == 0

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_returns_none_and_unlinks(
        self, monkeypatch, caplog,
    ) -> None:
        """A fail-closed sandbox is reported with its remedy, not as a generic error.

        Every Windows host (and Linux without user namespaces) has no OS sandbox
        backend, so wrap_argv raises SandboxUnavailableError. Polly must degrade to
        None, unlink the temp MP3, and relay the sandbox layer's remedy prose — the
        generic handler's "Polly synthesis error" stack trace misattributes this to
        Polly or AWS credentials.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*a, **k):
            fd, p = real_mkstemp(*a, **k)
            created.append(p)
            return fd, p

        monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", tracking_mkstemp)

        def refuse(cmd, mode):
            raise SandboxUnavailableError(_SANDBOX_REMEDY, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", refuse)

        with caplog.at_level("ERROR", logger="kiro_crew.voice_reply"):
            assert await _synthesize_polly("<speak>hi</speak>") is None

        assert created, "polly should have allocated a temp mp3"
        assert not os.path.exists(created[0]), "temp mp3 must be unlinked"
        assert _SANDBOX_REMEDY in caplog.text
        assert "no_backend" in caplog.text
        assert "Polly synthesis error" not in caplog.text

    @pytest.mark.asyncio
    async def test_transient_sandbox_refusal_does_not_advise_disabling(
        self, monkeypatch, caplog,
    ) -> None:
        """A ``transient`` refusal must relay retry advice, not the opt-in key.

        SandboxUnavailableError.kind is the contract: for ``"transient"`` the
        sandbox layer's own prose says retry and explicitly says callers must NOT
        advise disabling the sandbox. Hardcoding the
        ``sandbox_allow_unsandboxed_exec`` remedy in this handler would tell an
        operator to permanently drop isolation to work around momentary resource
        pressure, so the handler must relay ``str(exc)`` rather than its own copy.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        transient_prose = (
            "This probe failure looks TRANSIENT (momentary resource pressure) "
            "— it is not cached. Do NOT disable the sandbox for this; retry."
        )

        def refuse(cmd, mode):
            raise SandboxUnavailableError(transient_prose, "transient", "fork: EAGAIN")

        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", refuse)

        with caplog.at_level("ERROR", logger="kiro_crew.voice_reply"):
            assert await _synthesize_polly("<speak>hi</speak>") is None

        assert transient_prose in caplog.text
        assert "transient" in caplog.text
        assert "sandbox_allow_unsandboxed_exec" not in caplog.text


# ── synthesize_speech() dispatcher ───────────────────────────────────────


class TestSynthesizeSpeechDispatcher:
    @pytest.mark.asyncio
    async def test_polly_dispatch(self) -> None:
        with patch(
            "kiro_crew.voice_reply._synthesize_polly",
            new=AsyncMock(return_value="/tmp/out.mp3"),
        ) as mock_polly, patch(
            "kiro_crew.voice_reply._synthesize_piper",
            new=AsyncMock(return_value="/tmp/out.wav"),
        ) as mock_piper:
            out = await synthesize_speech("hello world", provider=PROVIDER_POLLY)
        assert out == "/tmp/out.mp3"
        mock_polly.assert_awaited_once()
        mock_piper.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_piper_dispatch(self) -> None:
        with patch(
            "kiro_crew.voice_reply._synthesize_polly",
            new=AsyncMock(return_value="/tmp/out.mp3"),
        ) as mock_polly, patch(
            "kiro_crew.voice_reply._synthesize_piper",
            new=AsyncMock(return_value="/tmp/out.wav"),
        ) as mock_piper:
            out = await synthesize_speech("hello world", provider=PROVIDER_PIPER)
        assert out == "/tmp/out.wav"
        mock_piper.assert_awaited_once()
        mock_polly.assert_not_awaited()

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
    async def test_piper_empty_plain_returns_none(self) -> None:
        with patch(
            "kiro_crew.voice_reply._synthesize_piper",
            new=AsyncMock(return_value=None),
        ) as mock_piper:
            out = await synthesize_speech("   ", provider=PROVIDER_PIPER)
        assert out is None
        mock_piper.assert_not_awaited()

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
            "kiro_crew.voice_reply._synthesize_polly", side_effect=capture_polly,
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
                client, "C1", "t1", "hello",
                provider=PROVIDER_PIPER, piper_model="/fake/model.onnx",
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
            "kiro_crew.voice_reply._synthesize_polly", side_effect=fake_polly,
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
            "kiro_crew.voice_reply._synthesize_polly", side_effect=alternating,
        ):
            collected = []
            async for idx, sent, data in streaming_voice_reply(
                "First. Second. Third.",
            ):
                collected.append(idx)

        # Only the odd-numbered calls succeed (1, 3).
        assert collected == [0, 2]


class TestTextTypeAutoDetection:
    """Tests for --text-type dynamic selection (ssml vs text)."""

    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        # See TestSynthesizePolly._passthrough_sandbox.
        monkeypatch.setattr(
            "kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None)
        )
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
