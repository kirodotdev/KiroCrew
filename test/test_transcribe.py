"""Tests for speech-to-text transcription feature."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat as _pc
from kiro_crew.config.loader import SttConfig
from kiro_crew.transcribe import (
    BREW_PATH_DIRS,
    _find_mlx_whisper,
    _find_whisper,
    _is_openai_whisper,
    _ProfileCredentialResolver,
    find_brew,
    is_available,
    transcribe_audio,
)

# ---------------------------------------------------------------------------
# _find_whisper
# ---------------------------------------------------------------------------


def _no_own_venv(monkeypatch) -> None:
    """Neutralize the running interpreter's own scripts dir.

    ``_find_whisper`` probes it (that is what makes an install into the app's own
    venv work), and on a dev machine that directory really does contain a
    ``whisper`` — so a test isolating any LATER probe has to switch it off or it
    never gets there. Same reason these tests already stub ``shutil.which`` and
    ``_python3_bin_dir``.
    """
    monkeypatch.setattr("kiro_crew.transcribe._own_scripts_dir", lambda: "")


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
            _no_own_venv(monkeypatch)
            monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [str(tmp_path / "w")])
            assert _find_whisper("") is None

    def test_finds_whisper_installed_into_our_own_venv(self, tmp_path, monkeypatch):
        """``pip install openai-whisper`` inside the app's venv must be enough.

        Nothing else in the search order looks there: ``shutil.which`` only sees
        PATH (a venv is on PATH only after ``activate``, and the gateway runs as
        ``<venv>/bin/kirocrew``), and ``_python3_bin_dir`` deliberately asks the
        SYSTEM python3. So the obvious install left ``is_available()`` False, with
        no fix but setting ``stt.whisper_path`` by hand.
        """
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        binary = venv_bin / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setattr("kiro_crew.transcribe.sys.executable", str(venv_bin / "python"))
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [])
            monkeypatch.setattr("kiro_crew.transcribe._python3_bin_dir", lambda: "")
            assert _find_whisper("") == str(binary)

    def test_our_venv_is_preferred_over_the_system_python(self, tmp_path, monkeypatch):
        """Both present: the environment the caller installed into wins.

        Picking the system one would run a DIFFERENT Whisper than the operator
        just installed — a silently wrong version, or a missing model cache.
        """
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        ours = venv_bin / "whisper"
        ours.write_text("#!/bin/sh\n")
        ours.chmod(0o755)
        sys_bin = tmp_path / "system" / "bin"
        sys_bin.mkdir(parents=True)
        theirs = sys_bin / "whisper"
        theirs.write_text("#!/bin/sh\n")
        theirs.chmod(0o755)

        monkeypatch.setattr("kiro_crew.transcribe.sys.executable", str(venv_bin / "python"))
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [])
            monkeypatch.setattr("kiro_crew.transcribe._python3_bin_dir", lambda: str(sys_bin))
            assert _find_whisper("") == str(ours)

    def test_path_still_wins_over_the_venv(self, tmp_path, monkeypatch):
        """A whisper already on PATH is what the operator chose; do not override it."""
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "whisper").write_text("#!/bin/sh\n")
        (venv_bin / "whisper").chmod(0o755)
        monkeypatch.setattr("kiro_crew.transcribe.sys.executable", str(venv_bin / "python"))
        with patch("kiro_crew.transcribe.shutil.which", return_value="/usr/bin/whisper"):
            assert _find_whisper("") == "/usr/bin/whisper"

    def test_empty_path_finds_in_search_paths(self, tmp_path, monkeypatch):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            _no_own_venv(monkeypatch)
            monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [str(binary)])
            assert _find_whisper("") == str(binary)

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _find_whisper("~/whisper") == str(binary)

    def test_scripts_dir_fallback_finds_dot_exe_on_windows(self, tmp_path, monkeypatch):
        """Regression: a pip console script is ``whisper.exe`` in Scripts\\ on
        Windows; the extensionless probe never found it. The suffix sweep must.
        """
        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        exe = scripts / "whisper.exe"
        exe.write_text("")  # no execute bit on Windows
        monkeypatch.setattr("kiro_crew.transcribe.platform_compat.IS_WINDOWS", True)
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            _no_own_venv(monkeypatch)
            monkeypatch.setattr("kiro_crew.transcribe._python3_bin_dir", lambda: str(scripts))
            monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [])
            assert _find_whisper("") == str(exe)


# ---------------------------------------------------------------------------
# _is_openai_whisper — the --fp16 gate (issue #1896)
# ---------------------------------------------------------------------------


class TestIsOpenaiWhisper:
    @pytest.mark.parametrize(
        "path",
        [
            "whisper",
            "/usr/bin/whisper",
            "/opt/homebrew/bin/whisper",
            "whisper.exe",  # Windows console script — .stem drops the suffix
            "/usr/bin/WHISPER",  # case-insensitive
        ],
    )
    def test_reference_binary_is_openai(self, path):
        assert _is_openai_whisper(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "whisper-ctranslate2",
            "/usr/local/bin/whisper-ctranslate2",
            "/home/u/.local/bin/faster-whisper",
            "/usr/bin/whisperx",
            "/opt/whisper-cpp/main",
        ],
    )
    def test_dropin_engines_are_not_openai(self, path):
        assert _is_openai_whisper(path) is False


# ---------------------------------------------------------------------------
# _transcribe_native --fp16 gating end-to-end (issue #1896)
# ---------------------------------------------------------------------------


class TestNativeFp16Gating:
    """``--fp16 False`` must reach openai-whisper but never a drop-in engine.

    Passing it to whisper-ctranslate2 makes the CLI exit rc=2 and the user sees
    a silent empty transcript, so the flag is gated on the resolved binary name.
    """

    async def _run_native(self, tmp_path, whisper_bin: str) -> list:
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True, provider="whisper", timeout_secs=10)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = list(args)
            out_dir = args[args.index("--output_dir") + 1]
            Path(out_dir).joinpath("test.txt").write_text("hello world")
            return mock_proc

        with patch("kiro_crew.transcribe._find_whisper", return_value=whisper_bin):
            with patch(
                "kiro_crew.transcribe.asyncio.create_subprocess_exec", side_effect=fake_exec
            ):
                result = await transcribe_audio(str(audio), cfg)
        assert result == "hello world"
        return captured["args"]

    @pytest.mark.asyncio
    async def test_openai_whisper_gets_fp16(self, tmp_path):
        args = await self._run_native(tmp_path, "/usr/bin/whisper")
        assert "--fp16" in args
        assert args[args.index("--fp16") + 1] == "False"

    @pytest.mark.asyncio
    async def test_dropin_engine_omits_fp16(self, tmp_path):
        args = await self._run_native(tmp_path, "/usr/local/bin/whisper-ctranslate2")
        assert "--fp16" not in args
        # The rest of the invocation is unchanged — the engine still gets its model/output flags.
        assert "--model" in args and "--output_format" in args


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
# find_brew
# ---------------------------------------------------------------------------
class TestFindBrew:
    """A GUI-launched gateway inherits PATH=/usr/bin:/bin:/usr/sbin:/sbin, so
    ``shutil.which("brew")`` reports Homebrew MISSING on a machine that has it.
    ``find_brew`` falls back to the fixed install prefixes."""

    def test_found_on_path(self):
        with patch(
            "kiro_crew.transcribe.shutil.which", return_value="/opt/homebrew/bin/brew"
        ):
            assert find_brew() == "/opt/homebrew/bin/brew"

    def test_found_off_path_via_prefix(self, tmp_path, monkeypatch):
        brew = tmp_path / "brew"
        brew.write_text("#!/bin/sh\n")
        brew.chmod(0o755)
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr(
                "kiro_crew.transcribe._BREW_CANDIDATE_PATHS", [str(brew)]
            )
            assert find_brew() == str(brew)

    def test_not_installed(self, monkeypatch):
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr(
                "kiro_crew.transcribe._BREW_CANDIDATE_PATHS", ["/nonexistent/brew"]
            )
            assert find_brew() is None

    def test_path_dirs_cover_both_mac_prefixes_and_pipx_bin(self):
        """The shell-side list must cover Intel + Apple Silicon brew and the
        ``~/.local/bin`` dir pipx installs ``mlx_whisper`` into."""
        assert "/opt/homebrew/bin" in BREW_PATH_DIRS
        assert "/usr/local/bin" in BREW_PATH_DIRS
        assert os.path.expanduser("~/.local/bin") in BREW_PATH_DIRS
        # Expanded, not left as a shell variable — the script quotes each entry.
        assert not any(d.startswith("$") or d.startswith("~") for d in BREW_PATH_DIRS)


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
    async def test_whisper_discovery_runs_off_event_loop(self, tmp_path, monkeypatch):
        from threading import get_ident

        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        cfg = SttConfig(enabled=True)
        loop_thread = get_ident()
        discovery_threads = []

        def discover_python_bin_dir():
            discovery_threads.append(get_ident())
            return ""

        monkeypatch.setattr(
            "kiro_crew.transcribe._python3_bin_dir", discover_python_bin_dir
        )
        # This test observes the thread `_python3_bin_dir` runs on, so the probe
        # BEFORE it must miss — otherwise discovery short-circuits and never
        # reaches the call being watched.
        _no_own_venv(monkeypatch)
        monkeypatch.setattr("kiro_crew.transcribe._WHISPER_SEARCH_PATHS", [])
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            result = await transcribe_audio(str(audio), cfg)

        assert result is None
        assert discovery_threads
        assert discovery_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_aws_audio_read_runs_off_event_loop(self, tmp_path, monkeypatch):
        from threading import get_ident

        from kiro_crew import transcribe as tr

        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake audio")
        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        loop_thread = get_ident()
        read_threads = []

        def read_audio(path):
            assert path == str(audio)
            read_threads.append(get_ident())
            return b"fake audio"

        input_stream = SimpleNamespace(
            send_audio_event=AsyncMock(),
            end_stream=AsyncMock(),
        )
        stream = SimpleNamespace(input_stream=input_stream, output_stream=object())

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def start_stream_transcription(self, **kwargs):
                return stream

        class FakeHandler:
            def __init__(self, output_stream, transcript_parts):
                pass

            async def handle_events(self):
                pass

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_read_audio_bytes", read_audio)
        monkeypatch.setattr(
            tr,
            "_load_aws_transcribe_components",
            lambda: (FakeClient, FakeHandler),
        )

        result = await tr._transcribe_aws(str(audio), cfg)

        assert result is None
        assert read_threads
        assert read_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_whisper_output_file_io_runs_off_event_loop(
        self, tmp_path, monkeypatch
    ):
        from threading import get_ident

        binary = tmp_path / "whisper"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        audio = tmp_path / "test.webm"
        audio.write_text("fake audio")
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        cfg = SttConfig(enabled=True, whisper_path=str(binary), timeout_secs=10)
        loop_thread = get_ident()
        io_threads = []

        def make_output_dir():
            io_threads.append(get_ident())
            return str(output_dir)

        def collect_output(*args, **kwargs):
            io_threads.append(get_ident())
            return "Hello world"

        def remove_output_dir(*args, **kwargs):
            io_threads.append(get_ident())

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        monkeypatch.setattr("kiro_crew.transcribe.tempfile.mkdtemp", make_output_dir)
        monkeypatch.setattr(
            "kiro_crew.transcribe._collect_whisper_output", collect_output
        )
        monkeypatch.setattr("kiro_crew.transcribe.shutil.rmtree", remove_output_dir)
        with patch(
            "kiro_crew.transcribe.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await transcribe_audio(str(audio), cfg)

        assert result == "Hello world"
        assert len(io_threads) == 3
        assert all(thread != loop_thread for thread in io_threads)

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

        # Patch where events.py BOUND the symbol, not where it is defined: events.py
        # does `from kiro_crew.transcribe import transcribe_audio`, so it holds its own
        # module global. Patching the definition left the REAL transcriber running --
        # the assertion passed for the wrong reason and the test was the 3rd slowest in
        # the suite. Matches the sibling test above.
        with patch(
            "kiro_crew.slack.events.transcribe_audio", new_callable=AsyncMock, return_value=None
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


class TestFfmpegCandidateDirsWindows:
    """Regression guards for the Windows ffmpeg discovery fix.

    Runs on POSIX CI by monkeypatching ``platform_compat.IS_WINDOWS`` (same
    pattern as ``TestTaskkillErrorMapping`` in ``test_platform_compat.py``) —
    the branch construction is platform-independent code.
    """

    def test_windows_dirs_appended(self, monkeypatch):
        from kiro_crew import platform_compat as pc
        from kiro_crew import transcribe as tr

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        pf = r"C:\Program Files"
        la = r"C:\Users\user\AppData\Local"
        monkeypatch.setenv("ProgramFiles", pf)
        monkeypatch.setenv("LOCALAPPDATA", la)

        dirs = tr._ffmpeg_candidate_dirs()
        assert os.path.join(pf, "ffmpeg", "bin") in dirs
        assert os.path.join(la, "Programs", "ffmpeg", "bin") in dirs
        assert "/usr/local/bin" in dirs

    def test_non_windows_omits_windows_dirs(self, monkeypatch):
        from kiro_crew import platform_compat as pc
        from kiro_crew import transcribe as tr

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        dirs = tr._ffmpeg_candidate_dirs()
        for d in dirs:
            assert "Program Files" not in d
            assert "AppData" not in d

    def test_ensure_ffmpeg_probes_with_which(self, tmp_path, monkeypatch):
        """``ensure_ffmpeg_in_path`` must use ``shutil.which(name, path=d)`` so it
        catches ``ffmpeg.exe`` on Windows in addition to plain ``ffmpeg`` on POSIX.
        Regression: the prior implementation called ``os.path.isfile(<d>/ffmpeg)``
        which is blind to the ``.exe`` suffix.
        """
        from kiro_crew import transcribe as tr

        target_dir = tmp_path / "ffbin"
        target_dir.mkdir()

        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(target_dir)])
        monkeypatch.setenv("PATH", "/nowhere")

        calls: list[tuple[str, str]] = []

        def fake_which(name, path=None):
            calls.append((name, path or ""))
            return f"{path}/ffmpeg" if path == str(target_dir) else None

        monkeypatch.setattr(tr.shutil, "which", fake_which)
        tr.ensure_ffmpeg_in_path()

        assert calls and calls[0][0] == "ffmpeg"
        assert calls[0][1] == str(target_dir)
        assert os.environ["PATH"].startswith(str(target_dir))

    def test_ensure_ffmpeg_skips_dirs_already_on_path(self, tmp_path, monkeypatch):
        from kiro_crew import transcribe as tr

        target_dir = tmp_path / "ffbin"
        target_dir.mkdir()
        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(target_dir)])
        monkeypatch.setenv("PATH", f"{target_dir}{os.pathsep}/nowhere")

        called: list[str] = []

        def fake_which(name, path=None):
            called.append(name)
            return None

        monkeypatch.setattr(tr.shutil, "which", fake_which)
        tr.ensure_ffmpeg_in_path()

        assert called == []
        assert os.environ["PATH"].startswith(str(target_dir))


class TestFfmpegDiscoveryWindowsOnly:
    """Real, unmocked Windows behaviour — mkdir a fake install dir, drop an
    ``ffmpeg.exe`` inside, point ``_FFMPEG_CANDIDATE_DIRS`` at it, verify
    ``ensure_ffmpeg_in_path`` picks it up. Skipped on POSIX.
    """

    @pytest.mark.skipif(
        not _pc.IS_WINDOWS,
        reason="Windows-only: exercises PATHEXT-driven .exe suffix resolution.",
    )
    def test_ffmpeg_exe_discovered(self, tmp_path, monkeypatch):
        from kiro_crew import transcribe as tr

        ffbin = tmp_path / "ffbin"
        ffbin.mkdir()
        exe = ffbin / "ffmpeg.exe"
        exe.write_bytes(b"MZ")

        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(ffbin)])
        monkeypatch.setenv("PATH", r"C:\\Windows\\System32")

        tr.ensure_ffmpeg_in_path()
        assert os.environ["PATH"].startswith(str(ffbin))


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
