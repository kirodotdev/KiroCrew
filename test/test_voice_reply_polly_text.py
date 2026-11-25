"""The Polly ``--text`` value for a Windows ``.cmd`` / ``.bat`` AWS CLI launcher.

``cmd.exe`` re-parses a batch launcher's command line, so for one the SSML goes
in a temp file the CLI loads through ``file://``. Every other launcher and
platform keeps the SSML in the argv, as on main. The temp file is removed on
every exit of ``_synthesize_polly``, and the child env is main's.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from unittest.mock import AsyncMock, patch

import pytest
from voice_reply_helpers import _no_argv_prefixers  # noqa: F401
from voice_reply_helpers import _polly_consented_fixture  # noqa: F401
from voice_reply_helpers import _mock_subprocess

from kiro_crew import platform_compat, voice_reply_polly_text
from kiro_crew.platform_compat_runtime import _UTF8_PROCESS_ENV
from kiro_crew.sandbox import without_python_env
from kiro_crew.testing.wait import await_until, default_timeout
from kiro_crew.voice_reply import _synthesize_polly, text_to_ssml
from kiro_crew.voice_reply_polly_text import polly_text_args

_MOD = "kiro_crew.voice_reply_polly_text"
_SSML = '<speak><prosody rate="100%">He said "ready &amp; waiting | 50% ^ up.</prosody></speak>'
# SynthesizeSpeech refuses a request text over 6,000 characters.
_POLLY_MAX_REQUEST_CHARS = 6000


def _read_file_arg(args: list[str]) -> str:
    assert args[0] == "--text" and args[1].startswith("file://")
    with open(args[1][len("file://") :], encoding="utf-8") as f:
        return f.read()


def _unlink(path: str | None) -> None:
    if path:
        os.unlink(path)


def _recording_mkstemp(created: list[str]):
    real_mkstemp = tempfile.mkstemp

    def recording(*a, **k):
        fd, p = real_mkstemp(*a, **k)
        created.append(p)
        return fd, p

    return recording


class TestPollyTextArgs:
    def test_not_windows_keeps_the_argv(self, monkeypatch) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", False)
        assert polly_text_args("/opt/aws.cmd", _SSML) == (["--text", _SSML], None)

    @pytest.mark.parametrize("aws_bin", ["C:\\aws\\aws.exe", "C:\\aws\\aws.EXE", "aws"])
    def test_an_exe_keeps_the_argv(self, monkeypatch, aws_bin) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        assert polly_text_args(aws_bin, _SSML) == (["--text", _SSML], None)

    def test_plain_text_keeps_the_argv(self, monkeypatch) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        assert polly_text_args("C:\\aws\\aws.cmd", "hi") == (["--text", "hi"], None)

    @pytest.mark.parametrize("aws_bin", ["C:\\aws\\aws.cmd", "C:\\aws\\AWS.BAT"])
    def test_a_batch_launcher_reads_the_ssml_from_a_file(self, monkeypatch, aws_bin) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        args, path = polly_text_args(aws_bin, _SSML)
        try:
            assert path is not None and args == ["--text", f"file://{path}"]
            assert _read_file_arg(args) == _SSML
        finally:
            _unlink(path)

    def test_non_ascii_is_written_as_utf8(self, monkeypatch) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        ssml = "<speak>caf\u00e9 \u4f60\u597d</speak>"
        _args, path = polly_text_args("C:\\aws\\aws.cmd", ssml)
        try:
            with open(path, "rb") as f:
                assert f.read() == ssml.encode("utf-8")
        finally:
            _unlink(path)

    def test_a_long_cjk_reply_stays_within_the_polly_request_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        ssml = text_to_ssml("\u4f60" * 2900, engine="generative")
        args, path = polly_text_args("C:\\aws\\aws.cmd", ssml)
        try:
            sent = _read_file_arg(args)
            assert sent == ssml
            assert len(sent) <= _POLLY_MAX_REQUEST_CHARS
        finally:
            _unlink(path)

    @pytest.mark.parametrize("marker", ["$", "%"])
    def test_a_temp_dir_the_cli_would_expand_keeps_the_argv(
        self, monkeypatch, tmp_path, marker
    ) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        odd = tmp_path / f"a{marker}b"
        odd.mkdir()
        with monkeypatch.context() as m:
            m.setattr(voice_reply_polly_text.tempfile, "tempdir", str(odd))
            got = polly_text_args("C:\\aws\\aws.cmd", _SSML)
        assert got == (["--text", _SSML], None)
        assert list(odd.iterdir()) == []

    def test_a_failed_write_removes_the_file(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)

        def refuse(*_a, **_kw):
            raise OSError("disk full")

        # The module's own ``open`` name only; ``tempfile.tempdir`` is a module
        # global, so it is undone before tmp_path's teardown.
        monkeypatch.setattr(voice_reply_polly_text, "open", refuse, raising=False)
        with monkeypatch.context() as m:
            m.setattr(voice_reply_polly_text.tempfile, "tempdir", str(tmp_path))
            with pytest.raises(OSError):
                polly_text_args("C:\\aws\\aws.cmd", _SSML)
        assert list(tmp_path.iterdir()) == []


class TestSynthesizePollyBatchLauncher:
    @pytest.fixture(autouse=True)
    def _batch_launcher(self, _floor_monkeypatch, _polly_consented):
        _floor_monkeypatch.setattr(
            "kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        _floor_monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        _floor_monkeypatch.setattr(
            "kiro_crew.voice_reply.resolve_polly_cli", lambda: "C:\\aws\\aws.cmd"
        )

    @staticmethod
    def _exec(seen: dict, proc, *, size: int = 200):
        async def fake_exec(*cmd, **kwargs):
            text = cmd[cmd.index("--text") + 1]
            seen["text_arg"] = text
            seen["text_path"] = text[len("file://") :]
            seen["env"] = kwargs.get("env")
            with open(seen["text_path"], encoding="utf-8") as f:
                seen["content"] = f.read()
            with open(cmd[-1], "wb") as f:
                f.write(b"x" * size)
            return proc

        return fake_exec

    @pytest.mark.asyncio
    async def test_success_passes_the_ssml_unchanged_and_removes_the_file(self) -> None:
        ssml = text_to_ssml('He said "ready & waiting | 50% ^ up.', engine="generative")
        seen: dict = {}
        with patch(
            "asyncio.create_subprocess_exec", side_effect=self._exec(seen, _mock_subprocess())
        ):
            result = await _synthesize_polly(ssml)
        assert result is not None
        os.unlink(result)
        assert seen["text_arg"].startswith("file://")
        assert seen["content"] == ssml
        assert "ready &amp; waiting" in seen["content"]
        assert not os.path.exists(seen["text_path"])

    @pytest.mark.asyncio
    async def test_the_child_keeps_the_gateway_s_utf8_mode_and_main_s_env(
        self, monkeypatch
    ) -> None:
        # Set the env the way every entrypoint's ensure_utf8_console does. The
        # CLI decodes the file in UTF-8 only because it inherits PYTHONUTF8=1.
        for name, value in _UTF8_PROCESS_ENV.items():
            monkeypatch.setenv(name, value)
        ssml = text_to_ssml("caf\u00e9 \u4f60\u597d", engine="generative")
        seen: dict = {}
        with patch(
            "asyncio.create_subprocess_exec", side_effect=self._exec(seen, _mock_subprocess())
        ):
            result = await _synthesize_polly(ssml)
        assert result is not None
        os.unlink(result)
        assert seen["content"] == ssml
        assert seen["env"]["PYTHONUTF8"] == "1"
        assert seen["env"] == without_python_env(), "this change adds or removes no variable"
        assert not os.path.exists(seen["text_path"])

    @pytest.mark.asyncio
    async def test_failure_removes_the_file(self) -> None:
        seen: dict = {}
        proc = _mock_subprocess(returncode=1, stderr=b"denied")
        with patch("asyncio.create_subprocess_exec", side_effect=self._exec(seen, proc)):
            assert await _synthesize_polly(_SSML) is None
        assert seen["content"] == _SSML
        assert not os.path.exists(seen["text_path"])

    @pytest.mark.asyncio
    async def test_too_small_output_removes_the_file(self) -> None:
        seen: dict = {}
        proc = _mock_subprocess()
        with patch("asyncio.create_subprocess_exec", side_effect=self._exec(seen, proc, size=50)):
            assert await _synthesize_polly(_SSML) is None
        assert not os.path.exists(seen["text_path"])

    @pytest.mark.asyncio
    async def test_timeout_removes_the_file(self) -> None:
        seen: dict = {}
        proc = _mock_subprocess()
        # The first communicate is the bounded wait timing out; the second is the reap.
        proc.communicate = AsyncMock(side_effect=[asyncio.TimeoutError, (b"", b"")])
        with patch("asyncio.create_subprocess_exec", side_effect=self._exec(seen, proc)):
            assert await _synthesize_polly(_SSML) is None
        proc.kill.assert_called_once()
        assert not os.path.exists(seen["text_path"])

    @pytest.mark.asyncio
    async def test_cancellation_removes_the_file(self) -> None:
        seen: dict = {}
        proc = _mock_subprocess()
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        with patch("asyncio.create_subprocess_exec", side_effect=self._exec(seen, proc)):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_polly(_SSML)
        assert not os.path.exists(seen["text_path"])

    @pytest.mark.asyncio
    async def test_a_sandbox_refusal_removes_the_file(self, monkeypatch) -> None:
        from kiro_crew.sandbox import SandboxUnavailableError

        created: list[str] = []

        def refuse(cmd, mode):
            raise SandboxUnavailableError("no backend", "no_backend", "test")

        monkeypatch.setattr("kiro_crew.voice_reply.wrap_argv", refuse)
        # ``tempfile.mkstemp`` is a module global, so it is undone before teardown.
        with monkeypatch.context() as m:
            m.setattr(voice_reply_polly_text.tempfile, "mkstemp", _recording_mkstemp(created))
            with pytest.raises(SandboxUnavailableError):
                await _synthesize_polly(_SSML)
        ssml_files = [p for p in created if p.endswith(".ssml")]
        assert len(ssml_files) == 1
        assert not os.path.exists(ssml_files[0])

    @pytest.mark.asyncio
    async def test_a_failed_write_runs_no_cli_and_leaves_nothing(self, monkeypatch) -> None:
        created: list[str] = []
        spawned = AsyncMock()

        def refuse(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(voice_reply_polly_text, "open", refuse, raising=False)
        # ``tempfile.mkstemp`` is a module global, so it is undone before teardown.
        with monkeypatch.context() as m:
            m.setattr(voice_reply_polly_text.tempfile, "mkstemp", _recording_mkstemp(created))
            with patch("asyncio.create_subprocess_exec", spawned):
                assert await _synthesize_polly(_SSML) is None
        spawned.assert_not_called()
        assert created, "both the mp3 and the ssml file come from mkstemp"
        assert [p for p in created if os.path.exists(p)] == []


class TestPreparedOffTheLoop:
    @pytest.fixture(autouse=True)
    def _batch_launcher(self, _floor_monkeypatch, _polly_consented):
        _floor_monkeypatch.setattr(
            "kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        _floor_monkeypatch.setattr(f"{_MOD}.IS_WINDOWS", True)
        _floor_monkeypatch.setattr(
            "kiro_crew.voice_reply.resolve_polly_cli", lambda: "C:\\aws\\aws.cmd"
        )

    @pytest.mark.asyncio
    async def test_the_file_is_written_off_the_event_loop(self, monkeypatch) -> None:
        loop_thread = threading.get_ident()
        seen: dict = {}
        real = voice_reply_polly_text.polly_text_args

        def recording(aws_bin, text):
            seen["thread"] = threading.get_ident()
            return real(aws_bin, text)

        monkeypatch.setattr(voice_reply_polly_text, "polly_text_args", recording)
        proc = _mock_subprocess(returncode=1, stderr=b"denied")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await asyncio.wait_for(_synthesize_polly(_SSML), default_timeout())
        assert result is None
        assert seen["thread"] != loop_thread

    @pytest.mark.asyncio
    async def test_cancel_while_writing_removes_the_file(self, monkeypatch) -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        made: list[str] = []
        real = voice_reply_polly_text.polly_text_args
        spawned = AsyncMock()

        def held(aws_bin, text):
            args, path = real(aws_bin, text)
            made.append(path)
            loop.call_soon_threadsafe(entered.set)
            # Bounded so a broken test cannot strand the worker thread.
            release.wait(default_timeout())
            return args, path

        monkeypatch.setattr(voice_reply_polly_text, "polly_text_args", held)
        with patch("asyncio.create_subprocess_exec", spawned):
            task = asyncio.ensure_future(_synthesize_polly(_SSML))
            await await_until(entered, what="the worker writing the ssml file")
            # cancel() lands on the shielded wait at once, while the worker is held.
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, default_timeout())
        spawned.assert_not_called()
        assert len(made) == 1 and made[0]
        assert not os.path.exists(made[0])


_DUMPER = """import json, locale, os, sys
argv = sys.argv[2:]
text = argv[argv.index("--text") + 1]
body = None
if text.startswith("file://"):
    # The decoding AWS CLI v1's compat_open uses for a file:// parameter.
    with open(text[len("file://"):], encoding=locale.getpreferredencoding()) as f:
        body = f.read()
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump({"text": text, "body": body}, f)
with open(argv[-1], "wb") as f:
    f.write(b"x" * 200)
"""

# Lost-run ceiling for the whole loop below (three real aws.cmd -> python round
# trips), capped at default_timeout(), half the test's --timeout, so a stall fails
# at this test's own line rather than at the runner's kill, which on Windows takes
# the xdist worker down (Class 6).
# shortcut: not yet measured on a Windows shard. A healthy round trip is one
# cmd.exe plus one interpreter start; once a shard --durations figure exists,
# set this to 10x it and cite the figure here.
_REAL_LAUNCHER_LOST_RUN_SECS = 45.0
# Bound on the reap after a kill: the python child of ``cmd.exe`` holds the pipes.
_REAP_SECS = 10.0


class _TreeKilledProc:
    """A real child whose kill takes its whole tree and whose reap is bounded."""

    def __init__(self, proc) -> None:
        self._proc = proc
        self._killed = False

    def __getattr__(self, name):
        return getattr(self._proc, name)

    async def communicate(self, *a, **k):
        if not self._killed:
            return await self._proc.communicate(*a, **k)
        return await asyncio.wait_for(
            self._proc.communicate(*a, **k),
            _REAP_SECS,  # flake-ok: bounded reap after a tree kill, reached only on a broken run
        )

    def kill(self) -> None:
        self._killed = True
        try:
            platform_compat.kill_process_tree(self._proc.pid)
        except OSError:
            self._proc.kill()


@pytest.fixture(scope="module")
def _scratch_launcher(tmp_path_factory):
    """One scratch ``aws.cmd`` and dumper for the module, like the v1 launcher."""
    root = tmp_path_factory.mktemp("aws-cmd")
    dumper = root / "dumper.py"
    dumper.write_text(_DUMPER, encoding="utf-8")
    log = root / "seen.json"
    launcher = root / "aws.cmd"
    lines = f'@echo off\r\n"{sys.executable}" "{dumper}" "{log}" %*\r\n'
    if not lines.isascii():
        # cmd.exe reads a batch file in the console code page, not UTF-8.
        pytest.skip("a non-ASCII temp or interpreter path")
    launcher.write_text(lines, encoding="ascii")
    return launcher, log


@pytest.mark.xdist_group(name="voice_reply_polly_real_cmd")
@pytest.mark.skipif(sys.platform != "win32", reason="needs a Windows command processor")
class TestRealBatchLauncher:
    @pytest.mark.asyncio
    async def test_the_launcher_receives_the_ssml_unchanged(
        self, _floor_monkeypatch, _polly_consented, _scratch_launcher, monkeypatch
    ) -> None:
        launcher, log = _scratch_launcher
        # The env every entrypoint publishes (ensure_utf8_console).
        for name, value in _UTF8_PROCESS_ENV.items():
            monkeypatch.setenv(name, value)
        _floor_monkeypatch.setattr(
            "kiro_crew.voice_reply.wrap_argv", lambda argv, **k: (list(argv), None)
        )
        _floor_monkeypatch.setattr("kiro_crew.voice_reply.resolve_polly_cli", lambda: str(launcher))

        async def spawn(*argv, **kw):
            return _TreeKilledProc(await asyncio.create_subprocess_exec(*argv, **kw))

        _floor_monkeypatch.setattr("kiro_crew.voice_reply.create_subprocess_limited", spawn)
        replies = [
            'He said "ready & waiting.',
            "pipe | caret ^ percent 50%PATH%",
            "caf\u00e9 \u4f60\u597d",
        ]
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + min(_REAL_LAUNCHER_LOST_RUN_SECS, default_timeout())
        for reply in replies:
            ssml = text_to_ssml(reply, engine="generative")
            if log.exists():
                log.unlink()
            try:
                result = await asyncio.wait_for(
                    _synthesize_polly(ssml), max(deadline - loop.time(), 0.0)
                )
            except asyncio.TimeoutError:
                pytest.fail(
                    f"aws.cmd round trip lost after {loop.time() - start:.1f}s on {reply!r}; "
                    f"stub log written: {log.exists()}"
                )
            assert result is not None, (
                f"no audio for {reply!r} after {loop.time() - start:.1f}s; "
                f"stub log written: {log.exists()}"
            )
            os.unlink(result)
            seen = json.loads(log.read_text(encoding="utf-8"))
            assert seen["body"] == ssml, reply
            assert not os.path.exists(seen["text"][len("file://") :]), reply
