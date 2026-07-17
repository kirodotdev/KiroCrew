"""Tests for speech-to-text transcription feature."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import SttConfig
from kiro_crew.transcribe import (
    _find_mlx_whisper,
    _find_whisper,
    _ProfileCredentialResolver,
    is_available,
    transcribe_audio,
)

# ---------------------------------------------------------------------------
# _find_whisper
# ---------------------------------------------------------------------------


class TestFindWhisper:
    def test_configured_path_exists(self, tmp_path):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        assert _find_whisper(str(binary)) == str(binary)

    def test_configured_path_missing(self):
        assert _find_whisper("/nonexistent/whisper") is None

    def test_configured_path_not_executable(self, tmp_path):
        binary = tmp_path / "whisper"
        binary.write_text("data")
        binary.chmod(0o644)
        assert _find_whisper(str(binary)) is None

    def test_empty_path_uses_which(self):
        with patch("kiro_crew.transcribe.shutil.which", return_value="/usr/bin/whisper"):
            assert _find_whisper("") == "/usr/bin/whisper"

    def test_empty_path_which_none_checks_search_paths(self, tmp_path, monkeypatch):
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [str(tmp_path / "w")])
            assert _find_whisper("") is None

    def test_empty_path_finds_in_search_paths(self, tmp_path, monkeypatch):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [str(binary)])
            assert _find_whisper("") == str(binary)

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _find_whisper("~/whisper") == str(binary)


# ---------------------------------------------------------------------------
# _find_mlx_whisper
# ---------------------------------------------------------------------------


class TestFindMlxWhisper:
    def test_found_on_path(self):
        with patch("kiro_crew.transcribe.shutil.which", return_value="/usr/local/bin/mlx_whisper"):
            assert _find_mlx_whisper() == "/usr/local/bin/mlx_whisper"

    def test_not_found(self, monkeypatch):
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._python3_bin_dir", lambda: "")
            monkeypatch.setattr("kiro_crew.transcribe._MLX_WHISPER_SEARCH_PATHS", ["/nonexistent"])
            assert _find_mlx_whisper() is None

    def test_found_in_search_paths(self, tmp_path, monkeypatch):
        binary = tmp_path / "mlx_whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._python3_bin_dir", lambda: "")
            monkeypatch.setattr(
                "kiro_crew.transcribe._MLX_WHISPER_SEARCH_PATHS", [str(binary)]
            )
            assert _find_mlx_whisper() == str(binary)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------
class TestIsAvailable:
    def test_disabled(self):
        cfg = SttConfig(enabled=False)
        assert is_available(cfg) is False

    def test_enabled_no_binary(self):
        cfg = SttConfig(enabled=True, whisper_path="/nonexistent")
        assert is_available(cfg) is False

    def test_enabled_with_binary(self, tmp_path):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        cfg = SttConfig(enabled=True, whisper_path=str(binary))
        assert is_available(cfg) is True

    def test_loads_config_when_none(self):
        mock_cfg = MagicMock()
        mock_cfg.stt = SttConfig(enabled=False)
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_cfg):
            assert is_available(None) is False

    def test_mlx_available_when_binary_found(self):
        cfg = SttConfig(enabled=True, provider="mlx")
        with patch("kiro_crew.transcribe._find_mlx_whisper", return_value="/usr/bin/mlx_whisper"):
            assert is_available(cfg) is True

    def test_mlx_unavailable_when_binary_missing(self):
        cfg = SttConfig(enabled=True, provider="mlx")
        with patch("kiro_crew.transcribe._find_mlx_whisper", return_value=None):
            assert is_available(cfg) is False


# ---------------------------------------------------------------------------
# transcribe_audio
# ---------------------------------------------------------------------------


class TestTranscribeAudio:
    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        cfg = SttConfig(enabled=False)
        result = await transcribe_audio("/tmp/test.webm", cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_binary_returns_none(self):
        cfg = SttConfig(enabled=True, whisper_path="/nonexistent")
        result = await transcribe_audio("/tmp/test.webm", cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_transcription(self, tmp_path):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, whisper_path=str(binary), timeout_secs=10)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        async def fake_exec(*args, **kwargs):
            out_dir = args[args.index("--output_dir") + 1]
            Path(out_dir).joinpath("test.txt").write_text("Hello world")
            return mock_proc

        with patch(
            "kiro_crew.transcribe.asyncio.create_subprocess_exec", side_effect=fake_exec
        ):
            result = await transcribe_audio(str(audio), cfg)
        assert result == "Hello world"

    @pytest.mark.asyncio
    async def test_whisper_failure_returns_none(self, tmp_path):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, whisper_path=str(binary), timeout_secs=10)

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with patch(
            "kiro_crew.transcribe.asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, tmp_path):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, whisper_path=str(binary), timeout_secs=1)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch(
            "kiro_crew.transcribe.asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            with patch(
                "kiro_crew.transcribe.asyncio.wait_for", side_effect=asyncio.TimeoutError
            ):
                result = await transcribe_audio(str(audio), cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_output_file_returns_none(self, tmp_path):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, whisper_path=str(binary), timeout_secs=10)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch(
            "kiro_crew.transcribe.asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_loads_config_when_none(self):
        mock_cfg = MagicMock()
        mock_cfg.stt = SttConfig(enabled=False)
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_cfg):
            result = await transcribe_audio("/tmp/test.webm", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_mlx_no_binary_returns_none(self, tmp_path):
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, provider="mlx")
        with patch("kiro_crew.transcribe._find_mlx_whisper", return_value=None):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_mlx_invalid_model_rejected_before_subprocess(self, tmp_path):
        """A malformed mlx_model (e.g. from a hand-edited config) must be
        rejected before it is ever passed to the subprocess."""
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(
            enabled=True, provider="mlx", mlx_model="; rm -rf ~", timeout_secs=10
        )
        with patch("kiro_crew.transcribe._find_mlx_whisper", return_value="/usr/bin/mlx_whisper"):
            with patch("kiro_crew.transcribe.asyncio.create_subprocess_exec") as spawn:
                result = await transcribe_audio(str(audio), cfg)
        assert result is None
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_mlx_successful_transcription(self, tmp_path):
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(
            enabled=True, provider="mlx", mlx_model="mlx-community/whisper-large-v3-turbo",
            timeout_secs=10,
        )

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = args
            out_dir = args[args.index("--output-dir") + 1]
            Path(out_dir).joinpath("test.txt").write_text("Hola mundo")
            return mock_proc

        with patch("kiro_crew.transcribe._find_mlx_whisper", return_value="/usr/bin/mlx_whisper"):
            with patch(
                "kiro_crew.transcribe.asyncio.create_subprocess_exec", side_effect=fake_exec
            ):
                result = await transcribe_audio(str(audio), cfg)
        assert result == "Hola mundo"
        # The configured HF repo must be passed via --model.
        assert "mlx-community/whisper-large-v3-turbo" in captured["args"]

    @pytest.mark.asyncio
    async def test_mlx_failure_returns_none(self, tmp_path):
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, provider="mlx", timeout_secs=10)

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"boom"))

        with patch("kiro_crew.transcribe._find_mlx_whisper", return_value="/usr/bin/mlx_whisper"):
            with patch(
                "kiro_crew.transcribe.asyncio.create_subprocess_exec", return_value=mock_proc
            ):
                result = await transcribe_audio(str(audio), cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_mlx_timeout_returns_none(self, tmp_path):
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, provider="mlx", timeout_secs=1)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("kiro_crew.transcribe._find_mlx_whisper", return_value="/usr/bin/mlx_whisper"):
            with patch(
                "kiro_crew.transcribe.asyncio.create_subprocess_exec", return_value=mock_proc
            ):
                with patch(
                    "kiro_crew.transcribe.asyncio.wait_for", side_effect=asyncio.TimeoutError
                ):
                    result = await transcribe_audio(str(audio), cfg)
        assert result is None


# ---------------------------------------------------------------------------
# events.py: _transcribe_files
# ---------------------------------------------------------------------------


class TestTranscribeFiles:
    @pytest.mark.xdist_group(name="serial")
    @pytest.mark.asyncio
    async def test_transcribe_audio_files(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()
        mock_orch.slack.download_file = AsyncMock()

        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://files.slack.com/a.webm",
                "filetype": "webm",
                "name": "voice.webm",
            },
        ]

        with patch(
            "kiro_crew.slack.events.transcribe_audio", new_callable=AsyncMock, return_value="Hello"
        ):
            result = await _transcribe_files(mock_orch, files)
        assert result == ["Hello"]

    @pytest.mark.asyncio
    async def test_skips_non_audio(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()

        files = [
            {"mimetype": "image/png", "url_private": "https://x.com/img.png", "name": "pic.png"}
        ]

        result = await _transcribe_files(mock_orch, files)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_no_url(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()

        files = [{"mimetype": "audio/webm", "name": "voice.webm"}]

        result = await _transcribe_files(mock_orch, files)
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_transcription_failure(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()
        mock_orch.slack.download_file = AsyncMock()

        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://x.com/a.webm",
                "filetype": "webm",
                "name": "v.webm",
            },
        ]

        with patch(
            "kiro_crew.transcribe.transcribe_audio", new_callable=AsyncMock, return_value=None
        ):
            result = await _transcribe_files(mock_orch, files)
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()
        mock_orch.slack.download_file = AsyncMock(side_effect=Exception("download failed"))

        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://x.com/a.webm",
                "filetype": "webm",
                "name": "v.webm",
            },
        ]

        result = await _transcribe_files(mock_orch, files)
        assert result == []


# ---------------------------------------------------------------------------
# client.py: download_file
# ---------------------------------------------------------------------------


class TestSlackClientDownloadFile:
    @pytest.mark.asyncio
    async def test_base_class_raises(self):
        from kiro_crew.slack.client import SlackClientOps

        class MinimalClient(SlackClientOps):
            async def post_message(self, *a, **kw):
                pass

            async def post_blocks(self, *a, **kw):
                pass

            async def update_message(self, *a, **kw):
                pass

            async def delete_message(self, *a, **kw):
                pass

            async def add_reaction(self, *a, **kw):
                pass

            async def remove_reaction(self, *a, **kw):
                pass

            async def open_dm(self, *a, **kw):
                pass

            async def post_ephemeral(self, *a, **kw):
                pass

            async def views_publish(self, *a, **kw):
                pass

            async def views_open(self, *a, **kw):
                pass

            async def views_update(self, *a, **kw):
                pass

            async def upload_file(self, *a, **kw):
                pass

        client = MinimalClient()
        with pytest.raises(NotImplementedError):
            await client.download_file("https://example.com/f", "/tmp/out")


# ---------------------------------------------------------------------------
# SttConfig
# ---------------------------------------------------------------------------


class TestSttConfig:
    def test_defaults(self):
        cfg = SttConfig()
        assert cfg.enabled is True
        assert cfg.whisper_path == ""
        assert cfg.model == "turbo"
        assert cfg.mlx_model == "mlx-community/whisper-large-v3-turbo"
        assert cfg.device == "cpu"
        assert cfg.timeout_secs == 300

    def test_custom_values(self):
        cfg = SttConfig(
            enabled=True, whisper_path="/opt/whisper", model="small", device="cuda", timeout_secs=60
        )
        assert cfg.enabled is True
        assert cfg.model == "small"


# ---------------------------------------------------------------------------
# Sensitive path guard (Fix #2)
# ---------------------------------------------------------------------------


class TestSensitivePathGuard:
    @pytest.mark.asyncio
    async def test_sensitive_path_blocked_for_whisper(self, tmp_path):
        """is_sensitive_path check covers whisper path, not just AWS."""
        audio = tmp_path / "test.webm"
        audio.write_text("fake")
        cfg = SttConfig(enabled=True, provider="whisper")
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_sensitive_path_blocked_for_transcribe(self, tmp_path):
        audio = tmp_path / "test.webm"
        audio.write_text("fake")
        cfg = SttConfig(enabled=True, provider="transcribe")
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None


# ---------------------------------------------------------------------------
# ensure_ffmpeg_in_path for whisper (Fix #3)
# ---------------------------------------------------------------------------


class TestFfmpegEnsuredForWhisper:
    @pytest.mark.asyncio
    async def test_ensure_ffmpeg_called_for_whisper(self, tmp_path):
        audio = tmp_path / "test.webm"
        audio.write_text("fake")
        cfg = SttConfig(enabled=True, provider="whisper", whisper_path="/nonexistent")
        with patch("kiro_crew.security.is_sensitive_path", return_value=False), \
             patch("kiro_crew.transcribe.ensure_ffmpeg_in_path") as mock_ensure:
            await transcribe_audio(str(audio), cfg)
        mock_ensure.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_ffmpeg_not_called_for_transcribe(self, tmp_path):
        audio = tmp_path / "test.ogg"
        audio.write_text("fake")
        cfg = SttConfig(enabled=True, provider="transcribe")
        with patch("kiro_crew.security.is_sensitive_path", return_value=False), \
             patch("kiro_crew.transcribe.ensure_ffmpeg_in_path") as mock_ensure, \
             patch("kiro_crew.transcribe._transcribe_aws", new_callable=AsyncMock, return_value="hi"):
            await transcribe_audio(str(audio), cfg)
        mock_ensure.assert_not_called()


# ---------------------------------------------------------------------------
# Unsupported format rejection for Transcribe
# ---------------------------------------------------------------------------


class TestTranscribeFormatValidation:
    @pytest.mark.asyncio
    async def test_rejects_unsupported_format(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_text("fake")
        cfg = SttConfig(enabled=True, provider="transcribe")
        with patch("kiro_crew.security.is_sensitive_path", return_value=False):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None


# ---------------------------------------------------------------------------
# _ProfileCredentialResolver null check (Fix #4)
# ---------------------------------------------------------------------------


class TestProfileCredentialResolver:
    @pytest.mark.asyncio
    async def test_none_credentials_raises(self):
        resolver = _ProfileCredentialResolver.__new__(_ProfileCredentialResolver)
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None
        mock_session.profile_name = "test-profile"
        resolver._session = mock_session
        mock_creds_module = MagicMock()
        with patch.dict("sys.modules", {"amazon_transcribe": MagicMock(), "amazon_transcribe.auth": mock_creds_module}):
            with pytest.raises(RuntimeError, match="No AWS credentials found"):
                await resolver.get_credentials()
