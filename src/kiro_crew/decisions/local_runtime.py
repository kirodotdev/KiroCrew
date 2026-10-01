"""Run a local decision model for the gateway: fetch it, install it, serve it.

A local preset (:mod:`.local_models`) names everything the gateway needs to run
the model itself, so the owner never installs or starts anything by hand:

1. **Weights.** Each pinned file is fetched from Kiro Crew's model CDN through
   :func:`kiro_crew.asset_downloader.download_to`, which installs nothing until
   the streamed sha256 matches the pin. A receipt beside the files records which
   pins are already verified, so a restart does not rehash 8 GB.
2. **Environment.** A uv-managed Python 3.12 virtualenv per preset, built from
   the exact-pin lock shipped in ``local_servers/``. On Linux torch comes from the
   PyTorch CPU index, which keeps the environment near 1 GB instead of pulling the
   CUDA build. uv runs under the shared sandbox with a scrubbed environment: it
   executes third-party build code and is not entitled to the gateway's
   credentials.
3. **Server.** The preset's launcher runs in that environment, bound to
   127.0.0.1 on the port the provider route chose, sandboxed and in a cgroup
   scope. It is ready when ``GET /kirocrew-attest`` answers with the secret this
   launch handed it: any program can bind the port while the weights load, and
   only the server this runtime started knows that secret.

One preset runs at a time: :meth:`LocalModelRuntime.activate` replaces whatever
was wanted before, :meth:`deactivate` stops it. A server that exits on its own is
restarted with a growing delay; after :data:`MAX_FAILURES` consecutive failures
the runtime stops trying and reports the error with the tail of the server log,
because a model that cannot start (no memory, a broken download) will not start
on the next attempt either.

Everything blocking runs on one worker thread per activation, and a worker holds
its preset's writer lock for as long as it touches that preset's files, so a
replacement worker for the same preset waits for the old one, and :meth:`remove`
refuses a preset a worker still holds. :meth:`deactivate` stops a child process,
so it, like the rest of the control surface, is called off the event loop.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from kiro_crew import asset_downloader
from kiro_crew import env as _env
from kiro_crew import platform_compat
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import data_home
from kiro_crew.decisions import capability, gate, local_models
from kiro_crew.decisions.local_models import LocalModel
from kiro_crew.sandbox import popen_limited, sandboxed_spawn_argv
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger("kirocrew.decisions.local_runtime")

#: Directory under the data home every preset lives in.
#: Weights and their receipts, inside the ``decisions`` leaf every sandbox mounts
#: read-only, so an agent cannot plant a link the gateway then downloads through.
WEIGHTS_SUBDIR = Path("decisions") / "models"
#: Each preset's environment and server log. The sandboxed uv and the server write
#: here, so it cannot be read-only; the gateway writes into it only through
#: :func:`_refuse_links` and exclusive, no-follow opens.
WORK_SUBDIR = Path("models") / "decisions"

#: Launchers and dependency locks ship beside this module.
SERVERS_DIR = Path(__file__).resolve().parent / "local_servers"

#: Seconds a fresh server gets to load its weights and attest itself. Plumb
#: loads 8 GB from disk; a cold page cache on a slow disk is the worst case.
STARTUP_TIMEOUT_SECS = 600
#: Consecutive start failures after which the runtime stops retrying.
MAX_FAILURES = 4
#: Seconds a ready server must stay up before its exit stops counting as consecutive.
STABLE_RUN_SECS = 600
#: Delay before restart attempt *n* (1-based); the last entry repeats.
RESTART_BACKOFF_SECS = (5, 30, 120)
#: How long a stop waits for a graceful exit before forcing one.
STOP_GRACE_SECS = 10
#: uv's ceiling for building one environment; torch alone is a 200 MB wheel.
UV_TIMEOUT_SECS = 1800
#: How often a running install checks whether it has been told to stop.
RUN_POLL_SECS = 0.5
#: The PyTorch CPU-only wheel index (Linux); other platforms' PyPI torch is CPU.
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
#: Python every lock was resolved and served with.
ENV_PYTHON = "3.12"

STATE_IDLE = "idle"
STATE_DOWNLOADING = "downloading"
STATE_INSTALLING = "installing"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_ERROR = "error"

#: Lines of the server log carried into an error, so the card can show why.
_LOG_TAIL_LINES = 20
#: Characters of that tail kept: one line of a traceback can be megabytes.
_LOG_TAIL_CHARS = 2000
#: Environment variable the launcher reads its attestation secret from.
ATTEST_ENV = "KIROCREW_LOCAL_ATTEST"
#: Route the launcher answers with that secret.
ATTEST_PATH = "/kirocrew-attest"
#: Ports above a preset's default the route tries before asking the OS: a port
#: picked hours before the server binds it must not be an ephemeral one, which
#: any outbound connection on the machine can claim in the meantime.
PORT_BAND = 32


class _Cancelled(Exception):
    """Raised from the download's progress callback to abandon a stopped run."""


class _LinkedPath(Exception):
    """A path the runtime is about to write through has a symlink or junction in it."""


def _refuse_links(root: Path, path: Path) -> None:
    """Raise :class:`_LinkedPath` if *root* or any existing part of *path* under it is a link.

    The model server runs third-party code with its preset directory writable, so
    a link it left there must not carry the gateway's next write somewhere else.
    """
    parts = [root, *(root / rel for rel in _prefixes(path.relative_to(root)))]
    for part in parts:
        if not os.path.lexists(part):
            return
        if platform_compat.is_link_or_junction(part):
            raise _LinkedPath(f"{part.name or part} is a link; refusing to write through it")


def _prefixes(rel: Path) -> list[Path]:
    out: list[Path] = []
    acc = Path()
    for name in rel.parts:
        acc = acc / name
        out.append(acc)
    return out


def models_root() -> Path:
    """Where preset weights are stored under the data home."""
    return data_home() / WEIGHTS_SUBDIR


def work_root() -> Path:
    """Where preset environments and server logs live under the data home."""
    return data_home() / WORK_SUBDIR


#: ``O_NOFOLLOW`` where the platform has it; ``O_EXCL`` below refuses an existing
#: link everywhere, this also refuses one on an append open.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


def _write_new(root: Path, path: Path, data: bytes) -> None:
    """Replace *path* with *data* without following a link at or above it under *root*.

    A link in a directory above is refused; a link AT *path* is removed and replaced.
    """
    _refuse_links(root, path.parent)
    try:
        os.unlink(path)  # removes a planted link itself, never its target
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, 0o644)
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def _open_log(root: Path, path: Path) -> int:
    """An append descriptor for the server log that does not follow a planted link."""
    _refuse_links(root, path)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_NOFOLLOW | _O_BINARY
    fd = os.open(path, flags, 0o644)
    # A hard link is not a link to lstat: the server could have tied this name to
    # another file it can reach, which the next launch would then append to.
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise _LinkedPath(f"{path.name} is not a plain file; refusing to write to it")
    return fd


@dataclass
class _Status:
    preset: str = ""
    state: str = STATE_IDLE
    port: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    error: str = ""
    failures: int = 0

    def as_dict(self) -> dict:
        return {
            "preset": self.preset,
            "state": self.state,
            "port": self.port,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "error": self.error,
        }


@dataclass
class _Run:
    """One activation. Its worker exits once ``stop`` is set."""

    model: LocalModel
    port: int
    stop: threading.Event = field(default_factory=threading.Event)
    proc: "subprocess.Popen[bytes] | None" = None
    thread: threading.Thread | None = None
    #: Secret the current launch must echo before it counts as ready; set per spawn.
    nonce: str = ""


def _requirements_digest(m: LocalModel) -> str:
    return hashlib.sha256((SERVERS_DIR / m.requirements).read_bytes()).hexdigest()


class LocalModelRuntime:
    """Download, install and supervise the one local decision model that is wanted."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        download: Callable[..., tuple[bool, str]] | None = None,
        run_command: Callable[..., tuple[int, str]] | None = None,
        spawn: (
            Callable[
                [list[str], Path, Path, dict[str, str]],
                "tuple[subprocess.Popen[bytes], str | None]",
            ]
            | None
        ) = None,
        health: Callable[[int, str], bool] | None = None,
        sleep: Callable[[float, threading.Event], bool] | None = None,
    ) -> None:
        self._root_override = root
        self._download = download or _default_download
        self._run_command = run_command or _sandboxed_run
        self._spawn = spawn or _sandboxed_spawn
        self._health = health or _health_ok
        self._sleep = sleep or _interruptible_sleep
        self._lock = threading.Lock()
        self._run: _Run | None = None
        self._status = _Status()
        #: Held by the worker using a preset's files; see the module docstring.
        self._writers: dict[str, threading.Lock] = {}

    def _writer(self, m: LocalModel) -> threading.Lock:
        with self._lock:
            return self._writers.setdefault(m.id, threading.Lock())

    # -- paths ---------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root_override if self._root_override is not None else models_root()

    @property
    def work(self) -> Path:
        return self._root_override if self._root_override is not None else work_root()

    def weights_dir(self, m: LocalModel) -> Path:
        return self.root / m.id / m.revision

    def env_dir(self, m: LocalModel) -> Path:
        return self.work / m.id / "env"

    def log_path(self, m: LocalModel) -> Path:
        return self.work / m.id / "server.log"

    def _receipt_path(self, m: LocalModel) -> Path:
        return self.weights_dir(m) / ".verified.json"

    def _env_marker(self, m: LocalModel) -> Path:
        return self.env_dir(m) / ".kirocrew-ready"

    def _env_python(self, m: LocalModel) -> Path:
        env = self.env_dir(m)
        return env / "Scripts" / "python.exe" if sys.platform == "win32" else env / "bin" / "python"

    # -- what the card reads -------------------------------------------------

    def is_installed(self, m: LocalModel) -> bool:
        """Every pinned file verified and the environment built from today's lock."""
        return self._missing_files(m) == [] and self._env_ready(m)

    def status(self) -> dict:
        """The wanted preset's state, as JSON. Never blocks on IO beyond a stat."""
        with self._lock:
            return self._status.as_dict()

    def installed_ids(self) -> list[str]:
        return [m.id for m in local_models.LOCAL_MODELS if self.is_installed(m)]

    # -- control -------------------------------------------------------------

    def activate(self, m: LocalModel, port: int) -> None:
        """Make *m* the running model on *port*. Returns at once.

        Re-activating the preset that is already running on the same port is a
        no-op; anything else stops the current run first, so a retry after an
        error starts clean.
        """
        with self._lock:
            current = self._run
            if (
                current is not None
                and current.model.id == m.id
                and current.port == port
                and not current.stop.is_set()
                and self._status.state != STATE_ERROR
            ):
                return
        self.deactivate()
        run = _Run(model=m, port=port)
        with self._lock:
            self._run = run
            self._status = _Status(preset=m.id, state=STATE_DOWNLOADING, port=port)
        run.thread = threading.Thread(
            target=self._work, args=(run,), name=f"local-model-{m.id}", daemon=True
        )
        run.thread.start()

    def deactivate(self, *, wait: bool = False) -> None:
        """Stop whatever is running; the status returns to idle."""
        with self._lock:
            run, self._run = self._run, None
            self._status = _Status()
        if run is None:
            return
        run.stop.set()
        self._stop_proc(run)
        if wait and run.thread is not None:
            run.thread.join(timeout=STOP_GRACE_SECS + 5)

    def remove(self, m: LocalModel) -> bool:
        """Delete *m*'s weights and environment; False while a worker still uses them.

        Raises ``OSError`` when a file could not be removed, so a partial delete is
        never reported as done.

        The writer lock is what decides, not the status: a stopped worker can still
        be finishing a step after :meth:`deactivate` has reset the status to idle.
        """
        writer = self._writer(m)
        if not writer.acquire(blocking=False):
            return False
        try:
            for tree in (self.root / m.id, self.work / m.id):
                if os.path.lexists(tree):
                    shutil.rmtree(tree)
        finally:
            writer.release()
        return True

    # -- worker --------------------------------------------------------------

    def _set(self, run: _Run, **changes: object) -> bool:
        """Update the status if *run* is still the wanted one; whether it is."""
        with self._lock:
            if self._run is not run:
                return False
            for key, value in changes.items():
                setattr(self._status, key, value)
            return True

    def _fail(self, run: _Run, message: str) -> None:
        logger.warning(
            "local decision model %s: %s", run.model.id, message.splitlines()[0] if message else ""
        )
        self._set(run, state=STATE_ERROR, error=message)

    def _work(self, run: _Run) -> None:
        m = run.model
        writer = self._writer(m)
        # A previous worker for this preset may still be finishing a step it cannot
        # be interrupted in (a uv install); wait for it rather than share its files.
        while not writer.acquire(timeout=0.5):
            if run.stop.is_set():
                return
        try:
            if run.stop.is_set():
                return
            if not self._ensure_weights(run):
                return
            if not self._ensure_env(run):
                return
            self._serve(run)
        except _LinkedPath as exc:
            self._fail(run, f"the model directory is not safe to write: {exc}")
        except Exception as exc:  # the worker has no caller to raise to
            logger.exception("local decision model %s: worker crashed", m.id)
            self._fail(run, f"unexpected error: {type(exc).__name__}")
        finally:
            writer.release()

    def _missing_files(self, m: LocalModel) -> list[local_models.ModelFile]:
        try:
            receipt = json.loads(self._receipt_path(m).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = {}
        if not isinstance(receipt, dict):
            receipt = {}
        missing = []
        for f in m.files:
            path = self.weights_dir(m) / f.path
            try:
                size_ok = path.stat().st_size == f.size
            except OSError:
                size_ok = False
            if not size_ok or receipt.get(f.path) != f.sha256:
                missing.append(f)
        return missing

    def _write_receipt(self, m: LocalModel, f: local_models.ModelFile) -> None:
        path = self._receipt_path(m)
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                receipt = {}
        except (OSError, ValueError):
            receipt = {}
        receipt[f.path] = f.sha256
        tmp = path.with_suffix(".tmp")
        _refuse_links(self.root, tmp)
        tmp.write_text(json.dumps(receipt, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def _ensure_weights(self, run: _Run) -> bool:
        m = run.model
        missing = self._missing_files(m)
        total = m.download_bytes
        done = total - sum(f.size for f in missing)
        self._set(run, state=STATE_DOWNLOADING, bytes_done=done, bytes_total=total)
        for f in missing:
            if run.stop.is_set():
                return False
            target = self.weights_dir(m) / f.path
            _refuse_links(self.root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            _refuse_links(self.root, target)
            base = done

            def _progress(got: int, _size: int, base: int = base) -> None:
                if run.stop.is_set():
                    # The downloader catches this, keeps the partial for a resume,
                    # closes the file and returns; nothing writes after a stop.
                    raise _Cancelled
                self._set(run, bytes_done=base + got)

            ok, reason = self._download(
                target,
                m.file_url(f),
                sha256=f.sha256,
                size=f.size,
                resume=True,
                on_progress=_progress,
                label=f"{m.name} {f.path}",
            )
            if run.stop.is_set():
                return False
            if not ok:
                self._fail(run, reason or f"download of {f.path} failed")
                return False
            self._write_receipt(m, f)
            done += f.size
            self._set(run, bytes_done=done)
        return not run.stop.is_set()

    def _env_ready(self, m: LocalModel) -> bool:
        try:
            marker = self._env_marker(m).read_text(encoding="utf-8").strip()
        except OSError:
            return False
        return marker == _requirements_digest(m) and self._env_python(m).exists()

    def _ensure_env(self, run: _Run) -> bool:
        m = run.model
        if self._env_ready(m):
            return True
        if not self._set(run, state=STATE_INSTALLING):
            return False
        uv = _env.resolve_uv()
        if uv is None:
            self._fail(run, "uv is not available to build the model's environment")
            return False
        env = self.env_dir(m)
        _refuse_links(self.work, env)
        shutil.rmtree(env, ignore_errors=True)
        env.parent.mkdir(parents=True, exist_ok=True)
        own = self.work / m.id
        code, out = self._run_command(
            [uv, "venv", "--quiet", "--python", ENV_PYTHON, str(env)],
            UV_TIMEOUT_SECS,
            own,
            stop=run.stop,
        )
        if code != 0:
            self._fail(run, f"creating the environment failed:\n{out[-2000:]}")
            return False
        # The lock is copied beside the environment: the sandboxed uv reads it
        # from a directory it is allowed to see, whatever the gateway's install path.
        lock = env / "requirements.txt"
        _write_new(self.work, lock, (SERVERS_DIR / m.requirements).read_bytes())
        argv = [
            uv,
            "pip",
            "install",
            "--quiet",
            "--python",
            str(self._env_python(m)),
            "-r",
            str(lock),
        ]
        if sys.platform.startswith("linux"):
            argv += [
                "--index-url",
                TORCH_CPU_INDEX,
                "--extra-index-url",
                "https://pypi.org/simple",
                "--index-strategy",
                "unsafe-best-match",
            ]
        if run.stop.is_set():
            return False
        code, out = self._run_command(argv, UV_TIMEOUT_SECS, own, stop=run.stop)
        if code != 0:
            self._fail(run, f"installing the model's dependencies failed:\n{out[-2000:]}")
            return False
        _write_new(self.work, self._env_marker(m), _requirements_digest(m).encode("utf-8"))
        return not run.stop.is_set()

    def _serve(self, run: _Run) -> None:
        m = run.model
        while not run.stop.is_set():
            if not _port_free(run.port):
                self._fail(run, f"port {run.port} is already in use on this machine")
                return
            self._set(run, state=STATE_STARTING, error="")
            # A fresh secret per launch: a listener that read the previous one from
            # the last server's attest route cannot pass for this one.
            run.nonce = secrets.token_hex(16)
            # Copied on every start rather than at install: the launcher ships with
            # the gateway, and an upgraded gateway must not run last release's copy.
            _write_new(
                self.work, self.env_dir(m) / m.launcher, (SERVERS_DIR / m.launcher).read_bytes()
            )
            argv = [
                str(self._env_python(m)),
                "-I",
                str(self.env_dir(m) / m.launcher),
                "--weights",
                str(self.weights_dir(m)),
                "--port",
                str(run.port),
            ]
            try:
                proc, cleanup = self._spawn(
                    argv, self.log_path(m), self.work / m.id, {ATTEST_ENV: run.nonce}
                )
            except OSError as exc:
                self._fail(run, f"the model server could not be started: {exc}")
                return
            try:
                with self._lock:
                    superseded = self._run is not run
                    if not superseded:
                        run.proc = proc
                if superseded:
                    # Outside the lock: a stop can wait 30 s, and status() takes it.
                    _terminate(proc)
                    return
                if self._wait_ready(run, proc):
                    self._set(run, state=STATE_RUNNING)
                    started = time.monotonic()
                    proc.wait()
                    # Only a server that stayed up a while clears the budget, so one
                    # that attests and then dies keeps counting towards MAX_FAILURES.
                    if time.monotonic() - started >= STABLE_RUN_SECS:
                        self._set(run, failures=0)
            finally:
                _unlink_quietly(cleanup)
            if run.stop.is_set():
                return
            # The server is gone without being asked to stop.
            with self._lock:
                if self._run is not run:
                    return
                self._status.failures += 1
                failures = self._status.failures
            if failures >= MAX_FAILURES:
                self._fail(
                    run, f"the model server stopped {failures} times in a row:\n{self._log_tail(m)}"
                )
                return
            delay = RESTART_BACKOFF_SECS[min(failures, len(RESTART_BACKOFF_SECS)) - 1]
            self._set(
                run, state=STATE_STARTING, error=f"restarting after an exit:\n{self._log_tail(m)}"
            )
            if self._sleep(delay, run.stop):
                return

    def _wait_ready(self, run: _Run, proc: "subprocess.Popen[bytes]") -> bool:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECS
        while time.monotonic() < deadline:
            if run.stop.is_set() or proc.poll() is not None:
                return False
            if self._health(run.port, run.nonce):
                return True
            if self._sleep(1.0, run.stop):
                return False
        _terminate(proc)
        return False

    def _stop_proc(self, run: _Run) -> None:
        with self._lock:
            proc, run.proc = run.proc, None
        if proc is not None:
            _terminate(proc)

    def _log_tail(self, m: LocalModel) -> str:
        try:
            lines = self.log_path(m).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-_LOG_TAIL_LINES:])[-_LOG_TAIL_CHARS:]


# -- default seams ------------------------------------------------------------


def _default_download(path: Path, url: str, **kwargs: object) -> tuple[bool, str]:
    return asset_downloader.download_to(path, url, **kwargs)  # type: ignore[arg-type]


def _sandboxed_run(
    argv: list[str], timeout: int, own_dir: Path, stop: threading.Event | None = None
) -> tuple[int, str]:
    """Run *argv* under the shared sandbox with a scrubbed environment.

    *own_dir* is the preset's directory, re-exposed read-write: it may sit inside
    a tree the sandbox hides, and the environment has to be written there. A set
    *stop* ends the command early, so a deactivation or a gateway shutdown never
    leaves an installer writing into an environment a later run rebuilds.
    """
    wrapped, env, cleanup = sandboxed_spawn_argv(
        argv, mode="strict", strip_python_env=True, extra_private_dirs=(str(own_dir),)
    )
    name = Path(argv[0]).name
    try:
        try:
            proc = popen_limited(  # noqa: S603 - fixed argv, no request-derived values
                wrapped,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **UTF8_TEXT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, f"{name} could not be run: {exc}"
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, _ = proc.communicate(timeout=RUN_POLL_SECS)
                return proc.returncode, (out or "").strip()
            except subprocess.TimeoutExpired:
                if stop is not None and stop.is_set():
                    _kill_and_reap(proc)
                    return 1, f"{name} was stopped"
                if time.monotonic() >= deadline:
                    _kill_and_reap(proc)
                    return 1, f"{name} timed out after {timeout}s"
    finally:
        _unlink_quietly(cleanup)


def _kill_and_reap(proc: "subprocess.Popen[str]") -> None:
    proc.kill()
    try:
        proc.communicate(timeout=STOP_GRACE_SECS)
    except subprocess.TimeoutExpired:
        logger.warning("local decision model installer did not exit after kill")


def _unlink_quietly(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _sandboxed_spawn(
    argv: list[str], log_path: Path, own_dir: Path, extra_env: dict[str, str]
) -> "tuple[subprocess.Popen[bytes], str | None]":
    """Start the model server sandboxed, in a cgroup scope, logging to *log_path*.

    Returns the handle and the sandbox's temp launcher/profile, which the caller
    unlinks once the handle is reaped. *extra_env* is added to the scrubbed
    environment, after the scrub.
    """
    wrapped, env, cleanup = sandboxed_spawn_argv(
        argv, mode="strict", strip_python_env=True, extra_private_dirs=(str(own_dir),)
    )
    env = {**env, **extra_env}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with os.fdopen(_open_log(own_dir, log_path), "ab") as log:
            proc = popen_limited(  # noqa: S603 - fixed argv, no request-derived values
                wrapped,
                stdout=log,
                stderr=subprocess.STDOUT,
                # The launcher exits when this pipe closes; see :func:`_terminate`.
                stdin=subprocess.PIPE,
                env=env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
    except OSError:
        _unlink_quietly(cleanup)
        raise
    return proc, cleanup


def _terminate(proc: "subprocess.Popen[bytes]") -> None:
    """Stop a server this runtime started.

    The server runs behind the sandbox launcher, so a signal to the handle reaches
    the launcher and not the server under it. The stop is therefore the server's
    stdin: each launcher exits at end of input, which also ends it if the gateway
    itself dies. ``terminate`` and ``kill`` on the handle are the fallback for the
    launcher, and act only on the process this Popen still owns, never a pid.
    """
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=STOP_GRACE_SECS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=STOP_GRACE_SECS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=STOP_GRACE_SECS)
        except subprocess.TimeoutExpired:
            logger.warning("local decision model server did not exit after kill")
    except OSError:
        pass


def _health_ok(port: int, nonce: str) -> bool:
    """Whether the server on 127.0.0.1:*port* is the one this launch started.

    It must answer ``GET /kirocrew-attest`` with *nonce*, which only that server was
    given: an unrelated program that took the port while the weights loaded cannot,
    so it is never attested ready and no decision content is routed to it.

    A connection to a fixed host, not a URL: there is no scheme for a value to
    choose, so the probe can only ever reach this machine.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", ATTEST_PATH)
        resp = conn.getresponse()
        body = resp.read(256)
        return resp.status == 200 and secrets.compare_digest(body.strip(), nonce.encode())
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _port_free(port: int) -> bool:
    """Whether a server could listen on *port*, as the model servers bind it.

    Both servers set ``SO_REUSEADDR``, so the probe does too: without it a port
    left in TIME_WAIT by the previous run reads as taken. Not on Windows, where
    the option lets a second listener take a port another process holds.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port(preferred: int) -> int:
    """The first free port from *preferred* up :data:`PORT_BAND`, else one the OS hands out."""
    for port in range(preferred, min(preferred + PORT_BAND, 65536)):
        if _port_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _interruptible_sleep(seconds: float, stop: threading.Event) -> bool:
    """Sleep *seconds* unless *stop* is set first; whether it was."""
    return stop.wait(seconds)


_RUNTIME: LocalModelRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime() -> LocalModelRuntime:
    """The gateway's one runtime."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = LocalModelRuntime()
        return _RUNTIME


def resume_configured() -> str:
    """Start the preset ``decisions.provider`` names, if it names one; its id or "".

    Called at gateway start, off the event loop. A preset whose download or
    install was interrupted resumes from where the receipt left it. Nothing runs
    when the fleet withdrew local models. The owner's switch is deliberately not
    consulted: the consent surfaces read the local row only while the runtime is
    preparing or serving the preset, so stopping it with the switch off would put
    the switch back under the hosted row -- which a fleet that withdrew hosted Jev
    denies -- and the owner could never turn it on again.
    """
    cfg = live.snapshot() or KiroCrewConfig.load()
    endpoint = gate.configured_endpoint(cfg)
    provider = getattr(getattr(cfg, "decisions", None), "provider", None)
    m = local_models.get(local_models.active_id(endpoint, getattr(provider, "model", "")))
    if m is None or capability.is_decisions_denied(local=True):
        return ""
    port = urlsplit(endpoint).port
    if port is None:
        return ""
    get_runtime().activate(m, port)
    return m.id
