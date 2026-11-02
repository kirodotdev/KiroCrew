"""Installing Browser Harness: a pinned venv under the data home, from a hash lock.

``<home>/browser-harness/venv`` is a virtual environment the gateway creates with its
own interpreter and fills with ``pip install --require-hashes -r requirements.lock``. The
lock ships in this package (``setup.cfg`` package data) and pins every wheel of
``browser-harness==0.1.13`` and its dependencies by sha256, so what runs under the
gateway is exactly what was audited: a mirror or a hijacked index cannot substitute a
file without failing the hash check.

The prefix is a ``READONLY`` crew-home leaf (``sandbox_leaves``): a sandboxed process
can neither replace the interpreter the gateway later executes nor read the Chromium
profile beside it. The installer runs in the gateway, outside the sandbox, so it can
still lay down a new version.

Kiro's Chromium (:mod:`.chromium`) is downloaded into the same prefix by the same
install job so one Settings button covers the whole engine.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.browser_harness import HARNESS_VERSION, chromium
from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: The crew-home leaf. Spelled identically in ``sandbox_leaves`` and
#: ``security.paths_lists``; ``test_browser_harness_install`` pins the three equal.
PREFIX_LEAF = "browser-harness"

#: The shipped lock file, beside this module.
LOCK_FILE = Path(__file__).with_name("requirements.lock")

#: Written last, so a prefix without it is an install that did not finish.
MARKER_FILE = "kirocrew-harness.json"

#: Harness 0.1.13 declares ``Requires-Python >= 3.11``.
MIN_PYTHON: tuple[int, int] = (3, 11)

#: Progress stages, in order. The Settings page shows the current one.
STAGE_PREPARING = "preparing"
STAGE_CREATING_VENV = "creating_venv"
STAGE_INSTALLING_HARNESS = "installing_harness"
STAGE_FINISHING = "finishing"
STAGES: tuple[str, ...] = (
    STAGE_PREPARING,
    STAGE_CREATING_VENV,
    STAGE_INSTALLING_HARNESS,
    chromium.STAGE_DOWNLOADING,
    chromium.STAGE_EXTRACTING,
    STAGE_FINISHING,
)

#: Which parts an install request may ask for.
COMPONENT_HARNESS = "harness"
COMPONENT_CHROMIUM = "chromium"
COMPONENTS: tuple[str, ...] = (COMPONENT_HARNESS, COMPONENT_CHROMIUM)

#: Budgets for the two subprocess steps. ``venv`` is seconds; a cold pip install of
#: a dozen wheels over a slow link is minutes.
_VENV_TIMEOUT_SECS = 120.0
_PIP_TIMEOUT_SECS = 15 * 60.0

#: How much of a failed step's output the state carries. The Settings page shows
#: the tail, which for pip is where the reason is.
ERROR_DETAIL_CAP = 2000

#: Isolated mode for both interpreters the installer runs. ``-m`` would otherwise
#: put the working directory first on ``sys.path`` and honour ``PYTHONPATH``, so a
#: ``venv/`` or ``pip/`` package an agent wrote into the gateway's cwd (a pod
#: gateway's cwd is the agent's workspace) would run as the owner the moment they
#: press Install. The daemon and the worker are spawned the same way
#: (``supervisor._start_daemon``, ``_start_worker``); this is the step that lays
#: down the interpreter they run.
_ISOLATED: tuple[str, ...] = ("-I",)

#: Environment prefixes the children never inherit. ``-I`` already ignores the
#: ``PYTHON*`` family inside the interpreter; pip reads ``PIP_*`` itself
#: (``PIP_INDEX_URL``, ``PIP_FIND_LINKS``, a config file) and is given only the
#: one value below. The proxy variables ARE inherited: pip needs them to reach the
#: index on a proxied network.
_SCRUBBED_ENV_PREFIXES: tuple[str, ...] = ("PYTHON", "PIP_")

#: pip flags: hashes are mandatory, nothing is prompted, no version nag on stderr.
_PIP_ARGS: tuple[str, ...] = (
    "-m",
    "pip",
    "install",
    "--require-hashes",
    "--no-input",
    "--disable-pip-version-check",
    "-r",
)

ProgressCallback = Callable[[str, str], None]


class InstallError(RuntimeError):
    """A step failed; ``detail`` is the bounded, operator-readable reason."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail[-ERROR_DETAIL_CAP:]


@dataclass(frozen=True)
class InstallState:
    """What is installed under the prefix, from the filesystem alone."""

    prefix: Path
    installed: bool
    version: str | None
    python: Path | None
    chromium: chromium.ChromiumState
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "version": self.version,
            "wanted_version": HARNESS_VERSION,
            "prefix": str(self.prefix),
            "chromium": self.chromium.as_dict(),
            "error": self.error,
        }


#: The venv's own directory inside the prefix. Kept BESIDE the Chromium directory,
#: not around it: ``python -m venv --clear`` empties the whole directory it is given,
#: so a venv at the prefix root would delete the downloaded browser on every retry.
VENV_DIR_NAME = "venv"


def harness_prefix(home: Path) -> Path:
    """The engine's root: ``<home>/browser-harness`` (venv and Chromium beside each other)."""
    return Path(home) / PREFIX_LEAF


def venv_dir(prefix: Path) -> Path:
    """The virtual environment: ``<prefix>/venv``."""
    return prefix / VENV_DIR_NAME


def venv_python(prefix: Path) -> Path:
    """The interpreter inside the prefix's venv, by the venv layout of this OS."""
    if IS_WINDOWS:
        return venv_dir(prefix) / "Scripts" / "python.exe"
    return venv_dir(prefix) / "bin" / "python"


def lock_digest(lock: Path = LOCK_FILE) -> str:
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def install_state(home: Path) -> InstallState:
    """Read the install from disk. Never launches anything, never raises."""
    prefix = harness_prefix(home)
    python = venv_python(prefix)
    marker = prefix / MARKER_FILE
    version: str | None = None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            version = data["version"]
    except (OSError, ValueError):
        version = None
    try:
        have_python = python.is_file()
    except OSError:
        have_python = False
    installed = have_python and version == HARNESS_VERSION
    return InstallState(
        prefix=prefix,
        installed=installed,
        version=version,
        python=python if have_python else None,
        chromium=chromium.chromium_state(prefix),
    )


def harness_current(home: Path) -> bool:
    """The Harness is installed AND was installed from the lock this build ships.

    :func:`install_state` answers "installed" from the version alone; a build that
    re-pins a dependency without moving ``HARNESS_VERSION`` changes only the lock,
    so the marker's ``lock_sha256`` is what tells a stale venv apart. Never raises:
    an unreadable marker or lock reads as "not current", which the startup
    auto-install answers by running the install again.
    """
    if not install_state(home).installed:
        return False
    try:
        data = json.loads((harness_prefix(home) / MARKER_FILE).read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("lock_sha256") == lock_digest()
    except (OSError, ValueError):
        return False


async def install(
    home: Path,
    progress: ProgressCallback | None = None,
    *,
    components: tuple[str, ...] = COMPONENTS,
    refresh: bool = False,
) -> InstallState:
    """Create or refresh the venv and download Chromium. Raises :class:`InstallError`.

    *refresh* replaces a Chromium that is already present (Settings' Redownload);
    without it a whole copy is kept. Runs its blocking steps in a worker thread;
    *progress* is called from that thread with ``(stage, detail)`` and must
    marshal onto the loop itself.
    """
    stop = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(
            install_sync, home, progress, components=components, refresh=refresh, stop=stop
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop.set()
        # Cancellation must settle the owned child before shutdown can finish,
        # so a repeated cancel waits on as well rather than abandoning the thread.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - the worker's own error is not this cancel's
                break
        raise


def install_sync(
    home: Path,
    progress: ProgressCallback | None = None,
    *,
    components: tuple[str, ...] = COMPONENTS,
    refresh: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    stop: threading.Event | None = None,
) -> InstallState:
    """The blocking body; *runner* replaces the owned subprocess in tests."""

    def notify(stage: str, detail: str) -> None:
        _check_stop(stop, stage)
        if progress is not None:
            progress(stage, detail)
        _check_stop(stop, stage)

    prefix = harness_prefix(home)
    notify(STAGE_PREPARING, str(prefix))
    if COMPONENT_HARNESS in components:
        _install_harness(
            prefix, notify, runner or functools.partial(_run, cwd=prefix, stop=stop), stop=stop
        )
    if COMPONENT_CHROMIUM in components:
        try:
            chromium.download_chromium(
                prefix,
                notify,
                refresh=refresh,
                should_stop=stop.is_set if stop is not None else None,
            )
        except chromium.ChromiumDownloadError as exc:
            raise InstallError(chromium.STAGE_DOWNLOADING, str(exc)) from exc
    notify(STAGE_FINISHING, str(prefix))
    return install_state(home)


def _install_harness(
    prefix: Path,
    notify: ProgressCallback,
    run: Callable[..., subprocess.CompletedProcess[str]],
    *,
    stop: threading.Event | None = None,
) -> None:
    if sys.version_info < MIN_PYTHON:
        raise InstallError(
            STAGE_PREPARING,
            f"Browser Harness needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+; "
            f"the gateway runs {sys.version_info[0]}.{sys.version_info[1]}",
        )
    if not LOCK_FILE.is_file():
        raise InstallError(STAGE_PREPARING, f"lock file missing: {LOCK_FILE}")
    # The prefix exists before either spawn: it is the children's working directory.
    prefix.mkdir(parents=True, exist_ok=True)
    state = install_state(prefix.parent)
    python = venv_python(prefix)
    if not state.python or state.version != HARNESS_VERSION:
        notify(STAGE_CREATING_VENV, str(prefix))
        # ``--clear`` empties the venv directory, and only it: Chromium lives in
        # ``<prefix>/chromium``, beside ``<prefix>/venv``, and is not touched.
        _step(
            run,
            [sys.executable, *_ISOLATED, "-m", "venv", "--clear", str(venv_dir(prefix))],
            _VENV_TIMEOUT_SECS,
            STAGE_CREATING_VENV,
        )
    notify(STAGE_INSTALLING_HARNESS, HARNESS_VERSION)
    _step(
        run,
        [str(python), *_ISOLATED, *_PIP_ARGS, str(LOCK_FILE)],
        _PIP_TIMEOUT_SECS,
        STAGE_INSTALLING_HARNESS,
    )
    _check_stop(stop, STAGE_INSTALLING_HARNESS)
    marker = {"version": HARNESS_VERSION, "lock_sha256": lock_digest(), "python": str(python)}
    (prefix / MARKER_FILE).write_text(json.dumps(marker, indent=2), encoding="utf-8")


def _step(
    run: Callable[..., subprocess.CompletedProcess[str]],
    argv: list[str],
    timeout: float,
    stage: str,
) -> None:
    try:
        result = run(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(stage, f"timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise InstallError(stage, str(exc)) from exc
    if result.returncode != 0:
        output = (result.stderr or "") + (result.stdout or "")
        raise InstallError(stage, output.strip() or f"exit {result.returncode}")


def _check_stop(stop: threading.Event | None, stage: str) -> None:
    if stop is not None and stop.is_set():
        raise InstallError(stage, "install stopped")


def _run(
    argv: list[str], timeout: float, *, cwd: Path, stop: threading.Event | None = None
) -> subprocess.CompletedProcess[str]:
    """The installer's one spawn: a fixed argv of our own interpreter and the venv's.

    Every token is a constant of this module or a path derived from the data home;
    the two triggers are the owner pressing Install in Settings and the gateway's
    own startup auto-install, and neither passes anything a caller chose. The working
    directory is the prefix (owner-only, never the gateway's own cwd), the
    interpreter runs isolated (``-I`` in the argv) and the environment is
    :func:`_child_env`: three fences against the same agent-writable cwd.
    """
    _check_stop(stop, STAGE_PREPARING)
    # Files rather than PIPEs let the shared synchronous tree-stop helper wait
    # without a child filling a pipe while its TERM handler runs.
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr,
    ):
        child = subprocess.Popen(
            argv,
            stdout=stdout,
            stderr=stderr,
            cwd=str(cwd),
            env=_child_env(),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            **UTF8_TEXT,
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                _check_stop(stop, STAGE_PREPARING)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    child.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            _check_stop(stop, STAGE_PREPARING)
        except BaseException:
            platform_compat.terminate_and_reap_sync(child, grace=0.5)
            raise
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(argv, child.returncode, stdout.read(), stderr.read())


def _child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The gateway's environment minus the ``PYTHON*`` and ``PIP_*`` families, plus our two."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.upper().startswith(_SCRUBBED_ENV_PREFIXES)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_NO_INPUT"] = "1"
    return env
