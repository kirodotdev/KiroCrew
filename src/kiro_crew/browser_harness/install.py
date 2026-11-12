"""Installing Browser Harness: a pinned venv under the data home, from a hash lock.

``<home>/browser-harness/venv`` is a virtual environment the gateway creates with its
own interpreter and fills with ``pip install --require-hashes -r requirements.lock``. The
lock ships in this package (``setup.cfg`` package data) and pins every wheel of
``browser-harness==0.1.13`` and its dependencies by sha256, so what runs under the
gateway is exactly what was audited: a mirror or a hijacked index cannot substitute a
file without failing the hash check.

A runtime that already ships the harness at the lock's pins (a distribution that
bundles it beside the gateway) needs no index at all: the venv is made without pip and
the gateway copies the distributions listed in the lock into the sealed prefix.
The runtime's files are not hash-checked. Until copied, they have the gateway's own
exposure: only its ``kiro_crew`` package directory is sealed. After installation,
runtime edits or removal cannot change the driver's copy.

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
import importlib.metadata
import importlib.util
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.browser_harness import HARNESS_VERSION, build_deps, chromium
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
#: two values in :func:`_child_env`. The proxy variables ARE inherited: pip needs
#: them to reach the index on a proxied network.
_SCRUBBED_ENV_PREFIXES: tuple[str, ...] = ("PYTHON", "PIP_")

#: pip flags: hashes are mandatory, nothing is prompted, no version nag on stderr.
_PIP_ARGS: tuple[str, ...] = (
    "install",
    "--require-hashes",
    "--no-input",
    "--disable-pip-version-check",
    "-r",
)

#: What the gateway's interpreter must carry at the lock's own pins for its copy to
#: stand in for the pip install: the harness and the two packages of its own project
#: it is released in step with. The rest of the lock is general-purpose libraries a
#: bundling runtime shares with its other packages, so their releases may differ.
_RUNTIME_DISTRIBUTIONS: tuple[str, ...] = ("browser-harness", "cdp-use", "fetch-use")

#: Run by the copied venv's interpreter: the harness imports, and each ``name==version``
#: argument is the version the venv itself resolves.
_RUNTIME_CHECK = (
    "import importlib.metadata as m, sys, browser_harness\n"
    "for pin in sys.argv[1:]:\n"
    "    name, _, want = pin.partition('==')\n"
    "    if m.version(name) != want:\n"
    "        sys.exit(f'{name} {m.version(name)} in the venv, not {want}')\n"
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
    # A runtime that ships the pinned harness is copied: no pip, no index and
    # nothing to build, so neither the pip checks nor the pillow refusal below apply.
    runtime_files = _runtime_harness_files()
    if runtime_files is not None:
        _copy_runtime_harness(prefix, notify, run, runtime_files, stop=stop)
        return
    # Interpreters differ in which half of pip they ship: the packaged app has
    # ``pip`` but no ``ensurepip``; the Toolbox runtime has ``ensurepip`` but no
    # ``pip``. With the gateway's pip, the venv is made ``--without-pip`` and that
    # pip installs into it (``--python``). Without it, the venv bootstraps its own
    # pip through ``ensurepip`` (offline, bundled wheel) and installs with that.
    gateway_pip = _has_module("pip")
    if not gateway_pip and not _has_module("ensurepip"):
        raise InstallError(
            STAGE_PREPARING,
            f"{sys.executable} has neither pip nor ensurepip; cannot install packages",
        )
    state = install_state(prefix.parent)
    python = venv_python(prefix)
    # Without the gateway's pip the venv must carry its own. A venv filled from
    # the runtime is rebuilt so its dependency versions cannot linger behind the lock's.
    venv_pip_ok = gateway_pip or _venv_has_pip(prefix)
    runtime_copy = _venv_from_runtime(prefix)
    # A finished install whose interpreter is gone (the base Python it links to was
    # removed, as when Toolbox prunes the version the venv was built on) still holds
    # every locked package. Re-pointing it at this interpreter is offline; a
    # ``--clear`` rebuild would fetch every wheel from the index again.
    repair = (
        not state.python
        and state.version == HARNESS_VERSION
        and venv_pip_ok
        and not runtime_copy
        and _venv_built_by_this_minor(prefix)
    )
    rebuild = not repair and (
        not state.python or state.version != HARNESS_VERSION or not venv_pip_ok or runtime_copy
    )
    # Before anything is cleared: a host that cannot build the locked pillow is
    # refused with the missing pieces named, and a working install stays as it is.
    if rebuild or not _venv_has_locked_pillow(prefix):
        refused = build_deps.refusal_for_this_host()
        if refused is not None:
            raise InstallError(STAGE_PREPARING, refused)
    if repair:
        notify(STAGE_CREATING_VENV, str(prefix))
        # ``venv --upgrade`` keeps an interpreter entry that is already a symlink,
        # dangling or not, so the links into the removed Python go first. It
        # rewrites ``pyvenv.cfg`` and the links and leaves site-packages as it is;
        # ``--without-pip`` keeps the pip the venv already has.
        _drop_dangling_interpreter_links(prefix)
        repair_argv = [sys.executable, *_ISOLATED, "-m", "venv", "--upgrade", "--without-pip"]
        repair_argv.append(str(venv_dir(prefix)))
        _step(run, repair_argv, _VENV_TIMEOUT_SECS, STAGE_CREATING_VENV)
    elif rebuild:
        # The marker goes first: a rebuild that fails part-way must read as
        # "not installed", never as a finished install of a venv now cleared.
        (prefix / MARKER_FILE).unlink(missing_ok=True)
        notify(STAGE_CREATING_VENV, str(prefix))
        # ``--clear`` empties the venv directory, and only it: Chromium lives in
        # ``<prefix>/chromium``, beside ``<prefix>/venv``, and is not touched.
        venv_argv = [sys.executable, *_ISOLATED, "-m", "venv", "--clear"]
        if gateway_pip:
            venv_argv.append("--without-pip")
        venv_argv.append(str(venv_dir(prefix)))
        _step(run, venv_argv, _VENV_TIMEOUT_SECS, STAGE_CREATING_VENV)
    notify(STAGE_INSTALLING_HARNESS, HARNESS_VERSION)
    if gateway_pip:
        pip_argv = [sys.executable, *_ISOLATED, "-m", "pip", "--python", str(python)]
    else:
        pip_argv = [str(python), *_ISOLATED, "-m", "pip"]
    _step(
        run,
        [*pip_argv, *_PIP_ARGS, str(LOCK_FILE)],
        _PIP_TIMEOUT_SECS,
        STAGE_INSTALLING_HARNESS,
    )
    _check_stop(stop, STAGE_INSTALLING_HARNESS)
    _write_marker(prefix)


def _copy_runtime_harness(
    prefix: Path,
    notify: ProgressCallback,
    run: Callable[..., subprocess.CompletedProcess[str]],
    files: list[tuple[Path, Path, tuple[int, int], Path]],
    *,
    stop: threading.Event | None = None,
) -> None:
    """Copy the runtime's recorded package files into a fresh, sealed venv."""
    (prefix / MARKER_FILE).unlink(missing_ok=True)
    notify(STAGE_CREATING_VENV, str(prefix))
    venv_argv = [sys.executable, *_ISOLATED, "-m", "venv", "--clear", "--without-pip"]
    _step(run, [*venv_argv, str(venv_dir(prefix))], _VENV_TIMEOUT_SECS, STAGE_CREATING_VENV)
    notify(STAGE_INSTALLING_HARNESS, HARNESS_VERSION)
    site = _venv_site_packages(prefix)
    # A replaced FIFO must not block the open before fstat can refuse it.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    roots: dict[Path, int] = {}
    with ExitStack() as stack:
        for source, relative, identity, root in files:
            _check_stop(stop, STAGE_INSTALLING_HARNESS)
            try:
                if os.open in os.supports_dir_fd:
                    if root not in roots:
                        root_fd = os.open(
                            root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                        )
                        stack.callback(os.close, root_fd)
                        roots[root] = root_fd
                    # Walk the resolved RECORD source so in-root aliases remain valid.
                    fd = _open_runtime_file(roots[root], source.relative_to(root), flags)
                else:
                    fd = os.open(source, flags)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                        raise InstallError(
                            STAGE_INSTALLING_HARNESS, f"runtime source changed: {relative}"
                        )
                    target = site / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with os.fdopen(fd, "rb", closefd=False) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise InstallError(
                    STAGE_INSTALLING_HARNESS, f"cannot copy runtime file {relative}: {exc}"
                ) from exc
    pins = _lock_pins()
    check = [f"{name}=={pins[name]}" for name in _RUNTIME_DISTRIBUTIONS]
    _step(
        run,
        [str(venv_python(prefix)), *_ISOLATED, "-c", _RUNTIME_CHECK, *check],
        _VENV_TIMEOUT_SECS,
        STAGE_INSTALLING_HARNESS,
    )
    _check_stop(stop, STAGE_INSTALLING_HARNESS)
    _write_marker(prefix, source="runtime")


def _open_runtime_file(root_fd: int, relative: Path, flags: int) -> int:
    """Open a source under the pinned root; the caller owns the returned leaf fd."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    with ExitStack() as stack:
        parent_fd = root_fd
        for part in relative.parts[:-1]:
            parent_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            stack.callback(os.close, parent_fd)
        return os.open(relative.name, flags, dir_fd=parent_fd)


def _write_marker(prefix: Path, *, source: str = "pip") -> None:
    """Written last: the install under *prefix* finished, from the lock this build ships."""
    marker = {
        "version": HARNESS_VERSION,
        "lock_sha256": lock_digest(),
        "python": str(venv_python(prefix)),
        "source": source,
    }
    (prefix / MARKER_FILE).write_text(json.dumps(marker, indent=2), encoding="utf-8")


def _lock_pins(lock: Path = LOCK_FILE) -> dict[str, str]:
    """``{name: version}`` for each requirement line of the lock (``name==version \\``)."""
    pins: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ", "-")):
            continue
        name, sep, rest = line.partition("==")
        if sep:
            pins[name.strip().lower().replace("_", "-")] = rest.split(" ", 1)[0]
    return pins


def _runtime_search_path() -> list[str]:
    """Keep runtime paths, including bundle .pth entries, but never the workspace."""
    paths = [path for path in sys.path[0 if sys.flags.safe_path else 1 :] if path]
    try:
        cwd = os.path.realpath(os.getcwd())
        return [path for path in paths if os.path.realpath(path) != cwd]
    except OSError:
        return paths


def _runtime_harness_files() -> list[tuple[Path, Path, tuple[int, int], Path]] | None:
    """Recorded runtime files to copy, or ``None`` to use the hash-pinned pip path.

    The harness family must match the lock. Other lock distributions may carry the
    runtime's own versions or be absent. Every distribution copied needs a RECORD
    without startup ``.pth`` hooks. Kept sources must resolve within their distribution
    root and are returned resolved with their regular-file identity and root. Never spawns.
    """
    pins = _lock_pins()
    if not all(name in pins for name in _RUNTIME_DISTRIBUTIONS):
        return None
    files: list[tuple[Path, Path, tuple[int, int], Path]] = []
    search_path = _runtime_search_path()
    for name, version in pins.items():
        dist = next(iter(importlib.metadata.distributions(name=name, path=search_path)), None)
        if dist is None:
            if name in _RUNTIME_DISTRIBUTIONS:
                return None
            continue
        if name in _RUNTIME_DISTRIBUTIONS and dist.version != version:
            return None
        recorded = dist.files
        if recorded is None or any(file.suffix == ".pth" for file in recorded):
            return None
        root = os.path.realpath(str(dist.locate_file("")))
        for file in recorded:
            relative = Path(file)
            if relative.anchor or ".." in relative.parts or "__pycache__" in relative.parts:
                continue
            source = os.path.realpath(str(dist.locate_file(file)))
            try:
                if os.path.commonpath((root, source)) != root:
                    return None
                info = os.stat(source)
                if not stat.S_ISREG(info.st_mode):
                    return None
            except (OSError, ValueError):
                return None
            files.append((Path(source), relative, (info.st_dev, info.st_ino), Path(root)))
    return files


def _venv_site_packages(prefix: Path) -> Path:
    """The venv's site-packages, by the scheme ``python -m venv`` lays it out with."""
    base = str(venv_dir(prefix))
    layout = {"base": base, "platbase": base, "installed_base": base, "installed_platbase": base}
    return Path(sysconfig.get_path("purelib", "venv", layout))


def _venv_from_runtime(prefix: Path) -> bool:
    """Whether the marker names a runtime-filled venv, independent of its Python layout."""
    try:
        data = json.loads((prefix / MARKER_FILE).read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("source") == "runtime"
    except (OSError, ValueError):
        return False


def _has_module(name: str) -> bool:
    """Whether the gateway interpreter can import ``name`` (no subprocess)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _venv_has_pip(prefix: Path) -> bool:
    """Whether the venv under *prefix* holds its own ``pip`` package (no spawn)."""
    venv = venv_dir(prefix)
    pattern = "Lib/site-packages/pip" if IS_WINDOWS else "lib/python*/site-packages/pip"
    try:
        return any(p.is_dir() for p in venv.glob(pattern))
    except OSError:
        return False


def _venv_built_by_this_minor(prefix: Path) -> bool:
    """Whether the venv's ``pyvenv.cfg`` names this interpreter's X.Y (no spawn).

    Its compiled wheels and its ``lib/pythonX.Y`` layout only fit that minor version.
    """
    try:
        text = (venv_dir(prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "version":
            return value.strip().split(".")[:2] == [str(n) for n in sys.version_info[:2]]
    return False


def _drop_dangling_interpreter_links(prefix: Path) -> None:
    """Unlink each ``python*`` entry of the venv's script directory whose target is gone."""
    try:
        entries = list(venv_python(prefix).parent.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("python") and entry.is_symlink() and not entry.exists():
            entry.unlink(missing_ok=True)


def _venv_has_locked_pillow(prefix: Path) -> bool:
    """Whether the venv already holds the locked pillow, so pip has nothing to build (no spawn)."""
    venv = venv_dir(prefix)
    site = "Lib/site-packages" if IS_WINDOWS else "lib/python*/site-packages"
    pattern = f"{site}/[Pp]illow-{build_deps.LOCKED_PILLOW}.dist-info"
    try:
        return any(p.is_dir() for p in venv.glob(pattern))
    except OSError:
        return False


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

    Every token is a constant of this module, a pin of the shipped lock or a path
    derived from the data home; the two triggers are the owner pressing Install in
    Settings and the gateway's own startup auto-install, and neither passes anything
    a caller chose. The working directory is the prefix (owner-only, never the
    gateway's own cwd), the interpreter runs isolated (``-I`` in the argv) and the
    environment is :func:`_child_env`: three fences against the same agent-writable
    cwd.
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
    """The gateway's environment minus the ``PYTHON*`` and ``PIP_*`` families, plus our three.

    ``PIP_CONFIG_FILE`` is the null device because pip also reads config files it
    finds from ``HOME`` and the system (``~/.config/pip/pip.conf``,
    ``~/.pip/pip.conf``, ``/etc/pip.conf``, the venv's own): an ``index-url`` there
    replaces PyPI, and a CodeArtifact repository whose login expired answers 401 for
    every locked wheel. Set to ``os.devnull``, pip loads none of them (``--isolated``
    still loads the system and venv files), so the index is pip's default and the
    lock's hashes pin every file it serves. The variable also reaches the pip that
    builds an sdist's build environment, which a command-line flag would not.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.upper().startswith(_SCRUBBED_ENV_PREFIXES)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull
    return env
