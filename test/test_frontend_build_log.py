"""The update-driven frontend build reports what it did and keeps its output.

``build_frontend_sync`` / ``build_frontend_async`` are what the three
source-install update paths call before they reinstall and restart. Each must
tell a failed build (``static/dist`` still serves the previous bundle) from a
skipped one, and leave the npm output where the failure can be read afterwards.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import frontend, pinned_fs
from kiro_crew.config.paths import config_dir
from kiro_crew.testing.wait import async_wait_until

#: Without a pinned walk (Windows) no log is opened and the output is discarded,
#: so only the tests about the log's contents depend on one.
needs_log = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="no pinned walk: the build output is discarded on this platform",
)


@pytest.fixture
def website(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    site = tmp_path / "proj" / "website"
    site.mkdir(parents=True)
    (site / "package.json").write_text("{}")
    monkeypatch.delenv("KIROCREW_EDITION_DIR", raising=False)
    monkeypatch.setattr(frontend.shutil, "which", lambda _name: "/usr/bin/npm")
    monkeypatch.setattr(frontend, "_stage_dist_locked", lambda *_a, **_k: True)
    return site


def _write_output(kwargs: dict, text: str) -> None:
    """Write *text* the way a child would: straight to the descriptor it was given.

    A pipe or DEVNULL sentinel (no log was opened) discards it, as the child would.
    """
    out = kwargs.get("stdout")
    if hasattr(out, "fileno"):
        os.write(out.fileno(), text.encode())


def _popen(website: Path, *, build_rc: int):
    """A Popen double for the install (``npm ci``/``install``) and the build."""

    class _P:
        pid = 4242

        def __init__(self, argv, **kwargs) -> None:
            self.argv = list(argv)
            self.kwargs = kwargs
            self.returncode = None

        def communicate(self, timeout=None):
            _write_output(self.kwargs, "added 812 packages\n")
            self.returncode = 0
            return (None, None)

        def wait(self, timeout=None):
            _write_output(self.kwargs, "vite v8 building for production...\n")
            if build_rc == 0:
                (website / "dist").mkdir(exist_ok=True)
                (website / "dist" / "index.html").write_text("<html></html>")
            else:
                _write_output(
                    self.kwargs,
                    "error TS2304: Cannot find name 'Foo'\n"
                    "fetch https://user:hunter2pass@registry.example.com/x\n",
                )
            self.returncode = build_rc
            return build_rc

    return _P


def _expected_log() -> Path:
    return config_dir() / "logs" / "frontend-build.log"


@needs_log
def test_a_failed_sync_build_reports_failed_and_keeps_its_output(website, monkeypatch):
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen(website, build_rc=1))

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "failed"
    assert result.log_path == _expected_log()
    text = result.log_path.read_text()
    assert "$ npm install" in text and "added 812 packages" in text
    assert "$ npm run build" in text and "error TS2304" in text
    message = result.failure_message()
    assert str(result.log_path) in message
    assert "error TS2304: Cannot find name 'Foo'" in message
    # The tail is shown in progress cards and the console, so it is redacted.
    assert "hunter2pass" not in message
    if sys.platform != "win32":
        assert stat.S_IMODE(result.log_path.stat().st_mode) == 0o600


def test_a_sync_build_that_publishes_reports_built(website, monkeypatch):
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen(website, build_rc=0))

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "built"
    if pinned_fs.supports_pinned_walk():
        assert result.log_path == _expected_log()


def test_a_sync_build_that_cannot_stage_reports_failed(website, monkeypatch):
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen(website, build_rc=0))
    monkeypatch.setattr(frontend, "_stage_dist_locked", lambda *_a, **_k: False)

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "failed"


@needs_log
def test_a_failed_sync_install_reports_failed(website, monkeypatch):
    popen = _popen(website, build_rc=0)

    def _failing_install(self, timeout=None):
        _write_output(self.kwargs, "npm ERR! code E401\n")
        self.returncode = 1
        return (None, None)

    monkeypatch.setattr(popen, "communicate", _failing_install)
    monkeypatch.setattr(frontend.subprocess, "Popen", popen)

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "failed"
    assert "npm ERR! code E401" in result.tail


def test_no_website_dir_is_skipped_not_failed(tmp_path):
    result = frontend.build_frontend_sync(tmp_path, log=lambda _m: None)

    assert result.outcome == "skipped"


def test_no_npm_is_skipped_not_failed(website, monkeypatch):
    monkeypatch.setattr(frontend.shutil, "which", lambda _name: None)

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "skipped"


def _async_install(rc: int = 0):
    class _Proc:
        returncode = None

        async def wait(self):
            self.returncode = rc
            return rc

    async def _exec(*_argv, **kwargs):
        _write_output(kwargs, "added 812 packages\n")
        return _Proc()

    return _exec


@needs_log
def test_a_failed_async_build_reports_failed_and_keeps_its_output(website, monkeypatch):
    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _async_install())
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen(website, build_rc=1))
    notes: list[tuple[str, str]] = []

    result = asyncio.run(
        frontend.build_frontend_async(
            str(website.parent), push_progress=lambda s, m: notes.append((s, m))
        )
    )

    assert result.outcome == "failed"
    assert result.log_path == _expected_log()
    text = result.log_path.read_text()
    assert "added 812 packages" in text and "error TS2304" in text
    assert str(result.log_path) in result.failure_message()
    # The existing per-cause warning is still pushed.
    assert any("Frontend build failed" in m for _s, m in notes), notes


def test_an_async_build_that_publishes_reports_built(website, monkeypatch):
    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _async_install())
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen(website, build_rc=0))

    result = asyncio.run(frontend.build_frontend_async(str(website.parent)))

    assert result.outcome == "built"


@needs_log
def test_a_failed_async_install_reports_failed(website, monkeypatch):
    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _async_install(rc=1))
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen(website, build_rc=0))

    result = asyncio.run(frontend.build_frontend_async(str(website.parent)))

    assert result.outcome == "failed"
    assert "added 812 packages" in result.tail


def test_async_no_npm_is_skipped_not_failed(website, monkeypatch):
    monkeypatch.setattr(frontend.shutil, "which", lambda _name: None)

    result = asyncio.run(frontend.build_frontend_async(str(website.parent)))

    assert result.outcome == "skipped"


def test_an_unopenable_log_still_builds_with_output_discarded(website, monkeypatch):
    monkeypatch.setattr(frontend, "_open_build_log", lambda: (None, None))
    seen: list[object] = []
    popen = _popen(website, build_rc=1)

    def _wait(self, timeout=None):
        seen.append(self.kwargs.get("stdout"))
        self.returncode = 1
        return 1

    def _communicate(self, timeout=None):
        self.returncode = 0
        return (b"", b"")

    monkeypatch.setattr(popen, "wait", _wait)
    monkeypatch.setattr(popen, "communicate", _communicate)
    monkeypatch.setattr(frontend.subprocess, "Popen", popen)

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "failed"
    assert result.log_path is None
    assert seen == [frontend.subprocess.DEVNULL]
    assert "Build output" not in result.failure_message()


def _protected_file(tmp_path: Path) -> Path:
    victim = tmp_path / "security_policy.json"
    victim.write_text('{"keep": true}')
    return victim


def _assert_untouched_and_discarded(result, victim: Path) -> None:
    assert victim.read_text() == '{"keep": true}'
    assert result.outcome == "failed"
    assert result.log_path is None
    assert "error TS2304" not in result.failure_message()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_a_symlinked_log_is_refused_and_its_target_left_alone(website, monkeypatch, tmp_path):
    """``logs/`` is writable from the sandbox; the gateway must not write through a link."""
    victim = _protected_file(tmp_path)
    log = _expected_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.symlink_to(victim)
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen_discarding(website))

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    _assert_untouched_and_discarded(result, victim)


def test_a_hard_linked_log_is_refused_and_its_other_name_left_alone(website, monkeypatch, tmp_path):
    victim = _protected_file(tmp_path)
    log = _expected_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(victim, log)
    except OSError as exc:  # pragma: no cover - a filesystem without hard links
        pytest.skip(f"no hard links here: {exc}")
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen_discarding(website))

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    _assert_untouched_and_discarded(result, victim)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_a_symlinked_logs_dir_is_refused(website, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / _expected_log().name
    victim.write_text('{"keep": true}')
    logs = _expected_log().parent
    if logs.exists():
        for child in logs.iterdir():
            child.unlink()
        logs.rmdir()
    logs.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen_discarding(website))

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    _assert_untouched_and_discarded(result, victim)


def _popen_discarding(website: Path):
    """Build doubles for a refused log: output must go to DEVNULL, never a file."""
    popen = _popen(website, build_rc=1)

    def _communicate(self, timeout=None):
        assert not hasattr(self.kwargs.get("stdout"), "fileno")
        self.returncode = 0
        return (b"", b"")

    def _wait(self, timeout=None):
        assert self.kwargs.get("stdout") is frontend.subprocess.DEVNULL
        self.returncode = 1
        return 1

    popen.communicate = _communicate
    popen.wait = _wait
    return popen


def test_without_a_pinned_walk_the_output_is_discarded(website, monkeypatch):
    """Windows has no pinned walk, and a junction there escapes a by-name check.

    So no log is opened by name at all: the build runs and its output is
    discarded, as it was before the log existed.
    """
    from kiro_crew import pinned_fs

    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen_discarding(website))

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "failed"
    assert result.log_path is None
    assert not _expected_log().exists()


def test_the_async_build_touches_its_log_off_the_event_loop(website, monkeypatch):
    """A stalled data-home mount must not stall the gateway's loop."""
    import threading

    loop_thread: list[int] = []
    seen: list[tuple[str, int]] = []
    real_open, real_tail = frontend._open_build_log, frontend._build_log_tail

    def _open():
        seen.append(("open", threading.get_ident()))
        return real_open()

    def _tail(build_log):
        seen.append(("tail", threading.get_ident()))
        return real_tail(build_log)

    monkeypatch.setattr(frontend, "_open_build_log", _open)
    monkeypatch.setattr(frontend, "_build_log_tail", _tail)
    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _async_install())
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen(website, build_rc=1))

    async def _run():
        loop_thread.append(threading.get_ident())
        return await frontend.build_frontend_async(str(website.parent))

    result = asyncio.run(_run())

    assert result.outcome == "failed"
    assert {kind for kind, _ in seen} == {"open", "tail"}
    assert all(ident != loop_thread[0] for _, ident in seen), seen


def test_an_interrupted_async_build_closes_its_log_off_the_event_loop(website, monkeypatch):
    """The interrupted path's close may wait on a stalled mount; it must not be the loop's."""
    import threading

    closed_on: list[int] = []
    closed = threading.Event()

    class _Log:
        def write(self, _data):
            return 0

        def flush(self):
            pass

        def close(self):
            closed_on.append(threading.get_ident())
            closed.set()

    monkeypatch.setattr(frontend, "_open_build_log", lambda: (_Log(), _expected_log()))
    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _async_install())

    started, release = threading.Event(), threading.Event()

    def _slow_build(*_a, **_k):
        started.set()
        release.wait(5)
        return True

    monkeypatch.setattr(frontend, "_npm_build_and_stage_locked", _slow_build)
    loop_thread: list[int] = []

    async def _run():
        loop_thread.append(threading.get_ident())
        task = asyncio.ensure_future(frontend.build_frontend_async(str(website.parent)))
        await async_wait_until(started.is_set, describe=lambda: "the build reached its worker")
        task.cancel()  # a gateway shutdown, while the build is in a worker
        release.set()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert closed.wait(5), "the interrupted run never closed its log"
    assert closed_on and closed_on[0] != loop_thread[0]


@needs_log
def test_a_long_private_key_in_the_output_is_redacted_before_the_tail_is_cut(website, monkeypatch):
    """A PEM block longer than the tail would lose its BEGIN marker to a cut-then-redact."""
    # Built at run time from parts, so no key-shaped literal sits in the source.
    kind = " ".join(("PRIVATE", "KEY"))
    body = [f"{'Q' * 40}{i:04d}xyzSECRET" for i in range(40)]
    pem = f"-----BEGIN {kind}-----\n" + "\n".join(body) + f"\n-----END {kind}-----\n"
    popen = _popen(website, build_rc=1)

    def _wait(self, timeout=None):
        _write_output(self.kwargs, "npm ERR! leaked:\n" + pem + "npm ERR! build failed\n")
        self.returncode = 1
        return 1

    monkeypatch.setattr(popen, "wait", _wait)
    monkeypatch.setattr(frontend.subprocess, "Popen", popen)

    result = frontend.build_frontend_sync(website.parent, log=lambda _m: None)

    assert result.outcome == "failed"
    assert "xyzSECRET" not in result.failure_message()
    assert "npm ERR! build failed" in result.tail


def test_a_log_too_large_to_redact_gives_no_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(frontend, "_BUILD_LOG_REDACT_MAX_BYTES", 16)
    with open(tmp_path / "big.log", "w+b") as fh:
        fh.write(b"x" * 64)
        assert frontend._build_log_tail(fh) == ""
