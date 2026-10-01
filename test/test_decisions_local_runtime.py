"""The gateway-run local decision model: fetch, install, serve, stop.

Every seam the runtime reaches the outside through -- the downloader, uv, the
spawn and the health probe -- is replaced here, so nothing is downloaded and no
server starts. What is pinned is the runtime's own logic: what it fetches and
skips, when it rebuilds the environment, what it launches, how it gives up, and
that a stop always reaches the server.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.decisions import local_models
from kiro_crew.decisions import local_runtime as lr

_LAYA = local_models.get("laya")


def _model(**changes) -> local_models.LocalModel:
    """Laya's launcher and lock, with two tiny files in place of its weights."""
    files = (
        local_models.ModelFile("LICENSE", hashlib.sha256(b"license").hexdigest(), len(b"license")),
        local_models.ModelFile(
            "sub/model.bin", hashlib.sha256(b"weights!").hexdigest(), len(b"weights!")
        ),
    )
    return replace(_LAYA, files=files, **changes)


_CONTENT = {"LICENSE": b"license", "sub/model.bin": b"weights!"}


class _Proc:
    """A server process: alive until its stdin closes or it is told to die."""

    def __init__(self, *, dies_after: float | None = None):
        self._done = threading.Event()
        self.returncode: int | None = None
        self.stdin = self
        self.terminated = False
        if dies_after is not None:
            timer = threading.Timer(dies_after, self._exit)
            timer.daemon = True
            timer.start()

    def _exit(self, code: int = 1):
        if self.returncode is None:
            self.returncode = code
        self._done.set()

    def close(self):  # stdin.close(): the launcher exits at end of input
        self._exit(0)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            import subprocess

            raise subprocess.TimeoutExpired("server", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self._exit(-15)

    def kill(self):
        self._exit(-9)


#: Every runtime a test builds, so teardown can stop its worker before the test's
#: temporary directory goes away -- including when the test failed before its own stop.
_RUNTIMES: list[lr.LocalModelRuntime] = []


@pytest.fixture(autouse=True)
def stop_every_runtime():
    yield
    while _RUNTIMES:
        rt = _RUNTIMES.pop()
        rt.deactivate(wait=True)
        run = rt._run
        assert run is None, "a runtime outlived its deactivate"


class _Harness:
    def __init__(
        self, root: Path, *, healthy: bool = True, proc_factory=None, download_ok: bool = True
    ):
        self.downloads: list[str] = []
        self.commands: list[list[str]] = []
        self.spawns: list[list[str]] = []
        self.spawn_env: list[dict[str, str]] = []
        self.cleanups: list[Path] = []
        self.sleeps: list[float] = []
        self.procs: list[_Proc] = []
        self._healthy = healthy
        self._proc_factory = proc_factory or (lambda: _Proc())
        self._download_ok = download_ok
        self.runtime = lr.LocalModelRuntime(
            root,
            download=self.download,
            run_command=self.run_command,
            spawn=self.spawn,
            health=lambda port, nonce: self._healthy
            and bool(self.procs)
            and self.procs[-1].poll() is None
            and nonce == self.spawn_env[-1][lr.ATTEST_ENV],
            sleep=self.sleep,
        )
        _RUNTIMES.append(self.runtime)

    def download(self, path, url, *, sha256, size, resume, on_progress, label):
        self.downloads.append(url)
        self.download_hook(on_progress)
        if not self._download_ok:
            return False, "kirocrew download: sha256 mismatch"
        name = url.rsplit(f"/{_LAYA.revision}/", 1)[1]
        path.write_bytes(_CONTENT[name])
        on_progress(size, size)
        return True, ""

    def run_command(self, argv, timeout, own_dir, stop=None):
        self.commands.append(argv)
        if argv[1] == "venv":
            env = Path(argv[-1])
            (env / "bin").mkdir(parents=True, exist_ok=True)
            (env / "bin" / "python").write_text("", encoding="utf-8")
        return 0, ""

    def download_hook(self, on_progress):
        """Overridden by a test that needs to act mid-download."""

    def spawn(self, argv, log_path, own_dir, extra_env):
        self.spawns.append(argv)
        self.spawn_env.append(dict(extra_env))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("Traceback: out of memory\n", encoding="utf-8")
        proc = self._proc_factory()
        self.procs.append(proc)
        cleanup = own_dir / f"sandbox-profile-{len(self.spawns)}"
        cleanup.write_text("", encoding="utf-8")
        self.cleanups.append(cleanup)
        return proc, str(cleanup)

    def sleep(self, seconds, stop):
        self.sleeps.append(seconds)
        return stop.wait(0.01)


def _wait_for(runtime, *states, timeout=5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = runtime.status()
        if s["state"] in states:
            return s
        time.sleep(0.01)
    raise AssertionError(f"runtime stayed {runtime.status()}")


@pytest.fixture(autouse=True)
def short_stop_grace(monkeypatch):
    """A stop that does not reach the server falls back to terminate after this; kept
    short so that regression fails a test in a second rather than stalling it."""
    monkeypatch.setattr(lr, "STOP_GRACE_SECS", 0.2)


@pytest.fixture
def free_port(monkeypatch):
    monkeypatch.setattr(lr, "_port_free", lambda port: True)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(lr.sys, "platform", "linux")
    monkeypatch.setattr("kiro_crew.env.resolve_uv", lambda: "/opt/uv")


class TestHappyPath:
    def test_downloads_installs_and_serves_on_the_given_port(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        m = _model()
        h.runtime.activate(m, 41001)
        s = _wait_for(h.runtime, "running")
        assert s["port"] == 41001 and s["bytes_done"] == s["bytes_total"] == m.download_bytes
        assert h.downloads == [m.file_url(f) for f in m.files]
        assert (
            tmp_path / "laya" / _LAYA.revision / "sub" / "model.bin"
        ).read_bytes() == b"weights!"
        venv, install = h.commands
        assert venv[1:3] == ["venv", "--quiet"] and "3.12" in venv
        assert install[1:3] == ["pip", "install"]
        assert lr.TORCH_CPU_INDEX in install, "Linux takes torch from the CPU-only index"
        (argv,) = h.spawns
        assert argv[1] == "-I"
        assert argv[2].endswith(m.launcher)
        assert argv[argv.index("--port") + 1] == "41001"
        assert argv[argv.index("--weights") + 1] == str(tmp_path / "laya" / _LAYA.revision)
        assert h.runtime.is_installed(m)
        h.runtime.deactivate(wait=True)

    def test_off_linux_torch_comes_from_pypi(self, tmp_path, free_port, monkeypatch):
        monkeypatch.setattr(lr.sys, "platform", "darwin")
        monkeypatch.setattr("kiro_crew.env.resolve_uv", lambda: "/opt/uv")
        h = _Harness(tmp_path)
        h.runtime.activate(_model(), 41002)
        _wait_for(h.runtime, "running")
        assert lr.TORCH_CPU_INDEX not in h.commands[1]
        h.runtime.deactivate(wait=True)

    def test_a_second_start_downloads_and_installs_nothing(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        m = _model()
        h.runtime.activate(m, 41003)
        _wait_for(h.runtime, "running")
        h.runtime.deactivate(wait=True)
        h.downloads.clear()
        h.commands.clear()
        h.runtime.activate(m, 41003)
        _wait_for(h.runtime, "running")
        assert h.downloads == [] and h.commands == []
        h.runtime.deactivate(wait=True)

    def test_reactivating_what_runs_is_a_no_op(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        m = _model()
        h.runtime.activate(m, 41004)
        _wait_for(h.runtime, "running")
        h.runtime.activate(m, 41004)
        assert len(h.spawns) == 1 and h.procs[0].poll() is None
        h.runtime.deactivate(wait=True)


class TestVerification:
    def test_a_file_whose_receipt_does_not_match_its_pin_is_fetched_again(
        self, tmp_path, free_port, linux
    ):
        h = _Harness(tmp_path)
        m = _model()
        h.runtime.activate(m, 41010)
        _wait_for(h.runtime, "running")
        h.runtime.deactivate(wait=True)
        receipt = tmp_path / "laya" / _LAYA.revision / ".verified.json"
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["sub/model.bin"] = "0" * 64
        receipt.write_text(json.dumps(data), encoding="utf-8")
        assert not h.runtime.is_installed(m)
        h.downloads.clear()
        h.runtime.activate(m, 41010)
        _wait_for(h.runtime, "running")
        assert h.downloads == [m.file_url(m.files[1])]
        h.runtime.deactivate(wait=True)

    def test_a_file_of_the_wrong_size_is_fetched_again(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        m = _model()
        h.runtime.activate(m, 41011)
        _wait_for(h.runtime, "running")
        h.runtime.deactivate(wait=True)
        (tmp_path / "laya" / _LAYA.revision / "LICENSE").write_bytes(b"lic")
        assert not h.runtime.is_installed(m)

    def test_a_failed_download_is_an_error_and_nothing_is_installed_or_run(
        self, tmp_path, free_port, linux
    ):
        h = _Harness(tmp_path, download_ok=False)
        h.runtime.activate(_model(), 41012)
        s = _wait_for(h.runtime, "error")
        assert "sha256 mismatch" in s["error"]
        assert h.commands == [] and h.spawns == []

    def test_a_changed_lock_rebuilds_the_environment(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        m = _model()
        h.runtime.activate(m, 41013)
        _wait_for(h.runtime, "running")
        h.runtime.deactivate(wait=True)
        (tmp_path / "laya" / "env" / ".kirocrew-ready").write_text(
            "an older lock", encoding="utf-8"
        )
        h.commands.clear()
        h.runtime.activate(m, 41013)
        _wait_for(h.runtime, "running")
        assert [c[1] for c in h.commands] == ["venv", "pip"]
        h.runtime.deactivate(wait=True)

    def test_without_uv_it_says_so(self, tmp_path, free_port, monkeypatch):
        monkeypatch.setattr("kiro_crew.env.resolve_uv", lambda: None)
        h = _Harness(tmp_path)
        h.runtime.activate(_model(), 41014)
        assert "uv" in _wait_for(h.runtime, "error")["error"]


class TestPlantedLinks:
    """A link left in a preset directory never carries a gateway write elsewhere."""

    def test_a_linked_weights_directory_is_refused_and_its_target_untouched(
        self, tmp_path, free_port, linux
    ):
        from kiro_crew import platform_compat

        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "root"
        (root / "laya").mkdir(parents=True)
        platform_compat.symlink_or_junction(outside, root / "laya" / _LAYA.revision)
        h = _Harness(root)
        h.runtime.activate(_model(), 41040)
        s = _wait_for(h.runtime, "error")
        assert "is a link" in s["error"]
        assert list(outside.iterdir()) == [], "nothing was written through the link"
        assert h.downloads == []

    @requires_symlinks
    def test_a_linked_file_in_the_environment_is_replaced_not_written_through(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        env = tmp_path / "work" / "laya" / "env"
        env.mkdir(parents=True)
        (env / "requirements.txt").symlink_to(target)
        lr._write_new(tmp_path / "work", env / "requirements.txt", b"torch==2\n")
        assert target.read_text(encoding="utf-8") == "{}"
        assert not (env / "requirements.txt").is_symlink()
        assert (env / "requirements.txt").read_bytes() == b"torch==2\n"

    def test_the_server_log_is_not_appended_to_through_a_hard_link(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        own = tmp_path / "work" / "laya"
        own.mkdir(parents=True)
        os.link(target, own / "server.log")
        with pytest.raises(lr._LinkedPath):
            lr._open_log(own, own / "server.log")
        assert target.read_text(encoding="utf-8") == "{}"

    def test_remove_reports_a_file_it_could_not_delete(self, tmp_path, monkeypatch):
        rt = lr.LocalModelRuntime(tmp_path)
        _RUNTIMES.append(rt)
        (tmp_path / "laya" / "env").mkdir(parents=True)

        def _refuse(path, ignore_errors=False, **k):
            # What rmtree does with a file it cannot unlink: raise, or swallow it.
            if not ignore_errors:
                raise PermissionError(13, "in use", str(path))

        monkeypatch.setattr(lr.shutil, "rmtree", _refuse)
        with pytest.raises(PermissionError):
            rt.remove(_model())

    @requires_symlinks
    def test_the_server_log_is_not_opened_through_a_link(self, tmp_path):
        target = tmp_path / "config.json"
        target.write_text("{}", encoding="utf-8")
        own = tmp_path / "work" / "laya"
        own.mkdir(parents=True)
        (own / "server.log").symlink_to(target)
        with pytest.raises(lr._LinkedPath):
            lr._open_log(own, own / "server.log")
        assert target.read_text(encoding="utf-8") == "{}"


class TestSupervision:
    def test_a_server_that_keeps_dying_is_given_up_on_with_its_log(
        self, tmp_path, free_port, linux
    ):
        h = _Harness(tmp_path, healthy=False, proc_factory=lambda: _Proc(dies_after=0.01))
        h.runtime.activate(_model(), 41020)
        s = _wait_for(h.runtime, "error")
        assert len(h.spawns) == lr.MAX_FAILURES
        assert "out of memory" in s["error"], "the card shows why"
        restarts = [d for d in h.sleeps if d in lr.RESTART_BACKOFF_SECS]
        assert restarts == list(lr.RESTART_BACKOFF_SECS[: lr.MAX_FAILURES - 1])

    def test_a_server_that_attests_then_dies_is_still_given_up_on(self, tmp_path, free_port, linux):
        # Becoming ready does not reset the budget, or this would restart forever.
        h = _Harness(tmp_path, proc_factory=lambda: _Proc(dies_after=0.05))
        h.runtime.activate(_model(), 41024)
        _wait_for(h.runtime, "error")
        assert len(h.spawns) == lr.MAX_FAILURES

    def test_every_launch_gets_its_own_secret(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path, proc_factory=lambda: _Proc(dies_after=0.05))
        h.runtime.activate(_model(), 41025)
        _wait_for(h.runtime, "error")
        secrets_seen = [env[lr.ATTEST_ENV] for env in h.spawn_env]
        assert len(set(secrets_seen)) == len(secrets_seen) == lr.MAX_FAILURES

    def test_a_crash_after_running_restarts_it(self, tmp_path, free_port, linux):
        procs = iter([_Proc(dies_after=0.2), _Proc()])
        h = _Harness(tmp_path, proc_factory=lambda: next(procs))
        h.runtime.activate(_model(), 41021)
        _wait_for(h.runtime, "running")
        deadline = time.monotonic() + 5
        while len(h.spawns) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(h.spawns) == 2
        _wait_for(h.runtime, "running")
        h.runtime.deactivate(wait=True)

    def test_a_held_port_is_reported_not_fought_over(self, tmp_path, linux, monkeypatch):
        monkeypatch.setattr(lr, "_port_free", lambda port: False)
        h = _Harness(tmp_path)
        h.runtime.activate(_model(), 41022)
        assert "41022" in _wait_for(h.runtime, "error")["error"]
        assert h.spawns == []

    def test_retrying_after_an_error_starts_over(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path, download_ok=False)
        m = _model()
        h.runtime.activate(m, 41023)
        _wait_for(h.runtime, "error")
        h._download_ok = True
        h.runtime.activate(m, 41023)
        _wait_for(h.runtime, "running")
        h.runtime.deactivate(wait=True)


class TestStop:
    def test_deactivate_closes_the_servers_stdin_and_goes_idle(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        h.runtime.activate(_model(), 41030)
        _wait_for(h.runtime, "running")
        h.runtime.deactivate(wait=True)
        (proc,) = h.procs
        assert proc.returncode == 0, "the launcher exited at end of input"
        assert proc.terminated is False, "no signal was needed"
        assert h.runtime.status()["state"] == "idle"

    def test_switching_presets_stops_the_first(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        h.runtime.activate(_model(), 41031)
        _wait_for(h.runtime, "running")
        h.runtime.activate(_model(id="laya-2"), 41032)
        assert h.procs[0].poll() is not None
        assert h.runtime.status()["preset"] == "laya-2"
        h.runtime.deactivate(wait=True)

    def test_a_server_that_ignores_end_of_input_is_terminated(self):
        proc = _Proc()
        proc.close = lambda: None  # a launcher that does not watch stdin
        lr._terminate(proc)
        assert proc.terminated and proc.poll() is not None

    def test_remove_deletes_the_presets_directory_only(self, tmp_path):
        rt = lr.LocalModelRuntime(tmp_path)
        _RUNTIMES.append(rt)
        (tmp_path / "laya" / "env").mkdir(parents=True)
        (tmp_path / "plumb-4b").mkdir()
        assert rt.remove(_model()) is True
        assert not (tmp_path / "laya").exists() and (tmp_path / "plumb-4b").exists()

    def test_a_server_is_ready_only_once_it_echoes_this_launchs_secret(
        self, tmp_path, free_port, linux
    ):
        h = _Harness(tmp_path)
        nonces: list[str] = []
        h.runtime._health = lambda port, nonce: (nonces.append(nonce), False)[1]
        h.runtime.activate(_model(), 8102)
        _wait_for(h.runtime, lr.STATE_STARTING)
        deadline = time.monotonic() + 2
        while not nonces and time.monotonic() < deadline:
            time.sleep(0.01)
        # The probe carries the secret the server was launched with, and a listener
        # that does not echo it never reaches RUNNING.
        assert nonces and nonces[0] == h.spawn_env[0][lr.ATTEST_ENV]
        assert len(nonces[0]) >= 32
        assert h.runtime.status()["state"] == lr.STATE_STARTING
        h.runtime.deactivate(wait=True)

    def test_the_sandbox_profile_is_unlinked_once_the_server_is_reaped(
        self, tmp_path, free_port, linux
    ):
        h = _Harness(tmp_path)
        h.runtime.activate(_model(), 8102)
        _wait_for(h.runtime, lr.STATE_RUNNING)
        assert h.cleanups[0].exists(), "the launcher still needs its profile"
        h.runtime.deactivate(wait=True)
        assert not h.cleanups[0].exists()

    def test_a_stop_mid_download_abandons_it_and_frees_the_files(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        rt = h.runtime
        outcome: list[object] = []

        def _stop_then_report(on_progress):
            rt.deactivate()
            # A worker still inside the transfer holds the files: remove refuses.
            outcome.append(rt.remove(_model()))
            try:
                on_progress(1, 2)
            except Exception as exc:  # what the downloader would catch
                outcome.append(type(exc).__name__)
                raise

        h.download_hook = _stop_then_report
        rt.activate(_model(), 8102)
        deadline = time.monotonic() + 5
        while len(outcome) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert outcome == [False, "_Cancelled"]
        deadline = time.monotonic() + 5
        while not rt.remove(_model()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not (tmp_path / "laya").exists(), "the worker let go and remove went through"
        assert h.commands == [] and h.spawns == [], "nothing ran after the stop"

    def test_a_replacement_worker_waits_for_the_previous_one(self, tmp_path, free_port, linux):
        h = _Harness(tmp_path)
        rt = h.runtime
        release = threading.Event()
        entered: list[int] = []

        def _slow(on_progress):
            entered.append(1)
            release.wait(5)

        h.download_hook = _slow
        rt.activate(_model(), 8102)
        deadline = time.monotonic() + 5
        while not entered and time.monotonic() < deadline:
            time.sleep(0.01)
        rt.deactivate()
        rt.activate(_model(), 8102)
        time.sleep(0.3)
        # The first worker is still inside its (uninterruptible) step, so the second
        # has not touched the files: one download in flight, not two.
        assert len(entered) == 1
        release.set()
        _wait_for(rt, lr.STATE_RUNNING)
        rt.deactivate(wait=True)

    def test_the_log_tail_is_bounded_in_characters(self, tmp_path):
        rt = lr.LocalModelRuntime(tmp_path)
        _RUNTIMES.append(rt)
        log = rt.log_path(_model())
        log.parent.mkdir(parents=True)
        log.write_text("x" * 50_000 + "\n", encoding="utf-8")
        assert len(rt._log_tail(_model())) == lr._LOG_TAIL_CHARS

    def test_free_port_stays_in_a_band_above_the_default(self, monkeypatch):
        taken = {8102, 8103}
        monkeypatch.setattr(lr, "_port_free", lambda port: port not in taken)
        assert lr.free_port(8102) == 8104


class TestLaunchers:
    @pytest.mark.parametrize("m", local_models.LOCAL_MODELS, ids=lambda m: m.id)
    def test_every_launcher_binds_loopback_and_exits_at_end_of_input(self, m):
        source = (lr.SERVERS_DIR / m.launcher).read_text(encoding="utf-8")
        assert f'os.environ.pop("{lr.ATTEST_ENV}"' in source and f'"{lr.ATTEST_PATH}"' in source
        assert '"127.0.0.1"' in source
        assert "0.0.0.0" not in source
        assert "sys.stdin.buffer.read()" in source and "os._exit(0)" in source
        assert 'HF_HUB_OFFLINE", "1"' in source, "the weights are local; nothing is fetched"


class TestResume:
    def _config(self, monkeypatch, endpoint, model):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            decisions=SimpleNamespace(provider=SimpleNamespace(endpoint=endpoint, model=model))
        )
        monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: cfg)
        monkeypatch.setattr("kiro_crew.decisions.gate.configured_endpoint", lambda c=None: endpoint)

    def _fake(self, monkeypatch):
        calls: list[tuple] = []

        class _RT:
            def activate(self, m, port):
                calls.append((m.id, port))

        monkeypatch.setattr(lr, "get_runtime", lambda: _RT())
        return calls

    def test_the_configured_preset_is_started_on_its_port(self, monkeypatch):
        self._config(monkeypatch, local_models.endpoint_for(40777), "english")
        monkeypatch.setattr(
            "kiro_crew.decisions.capability.is_decisions_denied", lambda *a, **k: False
        )
        calls = self._fake(monkeypatch)
        assert lr.resume_configured() == "laya"
        assert calls == [("laya", 40777)]

    def test_hosted_jev_starts_nothing(self, monkeypatch):
        from kiro_crew.config.sections import (
            DECISION_PROVIDER_ENDPOINT_DEFAULT,
            DECISION_PROVIDER_MODEL_DEFAULT,
        )

        self._config(
            monkeypatch, DECISION_PROVIDER_ENDPOINT_DEFAULT, DECISION_PROVIDER_MODEL_DEFAULT
        )
        calls = self._fake(monkeypatch)
        assert lr.resume_configured() == "" and calls == []

    def test_a_fleet_that_withdrew_local_models_starts_nothing(self, monkeypatch):
        self._config(monkeypatch, local_models.endpoint_for(40777), "english")
        monkeypatch.setattr(
            "kiro_crew.decisions.capability.is_decisions_denied", lambda *a, **k: True
        )
        calls = self._fake(monkeypatch)
        assert lr.resume_configured() == "" and calls == []


class TestHealthProbe:
    """The real probe against a real loopback listener: the secret decides."""

    def _serve(self, body: bytes):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    @pytest.mark.parametrize(("body", "ok"), [(b"s3cret", True), (b"other", False), (b"", False)])
    def test_only_the_launchs_own_secret_attests(self, body, ok):
        srv = self._serve(body)
        try:
            assert lr._health_ok(srv.server_address[1], "s3cret") is ok
        finally:
            srv.shutdown()
            srv.server_close()


class TestSandboxedSeams:
    """The default spawn seams, with the sandbox and the process launch faked."""

    def _fake_sandbox(self, monkeypatch, tmp_path):
        profile = tmp_path / "profile.sb"
        profile.write_text("", encoding="utf-8")
        seen: dict = {}

        def _wrap(argv, mode="standard", **kw):
            seen["mode"] = mode
            seen["kw"] = kw
            return ["sandbox", *argv], {"PATH": "/usr/bin"}, str(profile)

        monkeypatch.setattr(lr, "sandboxed_spawn_argv", _wrap)
        return profile, seen

    def test_run_is_strict_scrubbed_and_cleans_up(self, monkeypatch, tmp_path):
        profile, seen = self._fake_sandbox(monkeypatch, tmp_path)
        calls: list = []

        class _P:
            returncode = 0

            def communicate(self, timeout=None):
                return "ok", None

        def _run(argv, **kw):
            calls.append((argv, kw["env"]))
            return _P()

        monkeypatch.setattr(lr, "popen_limited", _run)
        assert lr._sandboxed_run(["uv", "venv"], 5, tmp_path) == (0, "ok")
        assert calls[0][0] == ["sandbox", "uv", "venv"]
        assert seen["mode"] == "strict" and seen["kw"]["strip_python_env"] is True
        assert not profile.exists()

    def test_run_reports_a_timeout(self, monkeypatch, tmp_path):
        import subprocess

        self._fake_sandbox(monkeypatch, tmp_path)

        class _P:
            returncode = None
            killed = False

            def communicate(self, timeout=None):
                if self.killed:
                    return "", None
                raise subprocess.TimeoutExpired("uv", timeout)

            def kill(self):
                self.killed = True

        monkeypatch.setattr(lr, "RUN_POLL_SECS", 0.01)

        def _run(argv, **kw):
            return _P()

        monkeypatch.setattr(lr, "popen_limited", _run)
        code, out = lr._sandboxed_run(["/x/uv", "pip"], 5, tmp_path)
        assert code == 1 and "timed out after 5s" in out

    def test_spawn_adds_the_secret_after_the_scrub_and_returns_the_profile(
        self, monkeypatch, tmp_path
    ):
        profile, _ = self._fake_sandbox(monkeypatch, tmp_path)
        got: dict = {}

        def _popen(argv, **kw):
            got.update(kw, argv=argv)
            return "proc"

        monkeypatch.setattr(lr, "popen_limited", _popen)
        proc, cleanup = lr._sandboxed_spawn(
            ["python", "l.py"], tmp_path / "m" / "server.log", tmp_path, {lr.ATTEST_ENV: "n"}
        )
        assert proc == "proc" and cleanup == str(profile) and profile.exists()
        assert got["env"] == {"PATH": "/usr/bin", lr.ATTEST_ENV: "n"}
        assert got["argv"] == ["sandbox", "python", "l.py"]

    def test_a_spawn_that_fails_unlinks_the_profile(self, monkeypatch, tmp_path):
        profile, _ = self._fake_sandbox(monkeypatch, tmp_path)

        def _popen(argv, **kw):
            raise OSError("no such file")

        monkeypatch.setattr(lr, "popen_limited", _popen)
        with pytest.raises(OSError):
            lr._sandboxed_spawn(["python"], tmp_path / "server.log", tmp_path, {})
        assert not profile.exists()


class TestInstallStop:
    def test_a_stop_ends_a_running_install(self, tmp_path, monkeypatch):
        """A deactivation mid-install kills the installer instead of waiting it out."""
        import sys

        monkeypatch.setattr(
            lr, "sandboxed_spawn_argv", lambda argv, **kw: (list(argv), dict(os.environ), None)
        )
        monkeypatch.setattr(lr, "STOP_GRACE_SECS", 5)
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        code, out = lr._sandboxed_run(
            [sys.executable, "-c", "import time; time.sleep(60)"], 120, tmp_path, stop=stop
        )
        assert code != 0 and "stopped" in out
        assert time.monotonic() - started < 15

    def test_an_install_without_a_stop_still_returns_its_output(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setattr(
            lr, "sandboxed_spawn_argv", lambda argv, **kw: (list(argv), dict(os.environ), None)
        )
        code, out = lr._sandboxed_run([sys.executable, "-c", "print('ok')"], 60, tmp_path)
        assert (code, out) == (0, "ok")
