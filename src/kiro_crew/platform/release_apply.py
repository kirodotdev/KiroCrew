"""Applying a release: its code, then the KAS engine it pins, as one step.

A Kiro Crew release is more than its Python code. Each release pins exactly one KAS
engine version (``acp/kas_pin.py``), and an install that runs KAS needs that engine
on disk the moment it starts the new code: ``installed_bundle()`` looks for the
pinned version only, so a release whose pin moved finds nothing and every KAS
spawn on the bundle route is refused. So the RELEASE, not the wheel, is the unit
an update installs, and :func:`apply_release` is the one function every update
path goes through: ``kirocrew update`` (git checkout and managed venv), the
dashboard's ``POST /api/update`` and ``POST /api/update/approve``, and the
gateway's unattended auto-apply (git and wheel). Nothing else in an update path
runs the package install or the engine install.

The order is fixed:

1. the new code is installed (``dep_sync.sync_or_reinstall`` into the checkout's
   venv, or the shadow tree the wheel engine builds beside the running one);
2. the NEW release's own ``kirocrew kas install --if-in-use`` runs, on the new
   code's interpreter, so the pin it installs is the new release's and not the
   one this process imported. The child decides whether the install runs Crew's
   own engine (the bundle route is on, or an engine version is already on disk)
   and does nothing otherwise, so a relay-route host that cannot run the engine
   keeps updating;
3. only then is the release switched to: the wheel engine promotes the stable
   link, the git callers restart. A failed or timed-out engine install fails the
   update before that switch, and the old version keeps running.

The engine install never removes another version, so the previous release's
engine stays on disk and a rollback to it needs no download.

Every child runs through the wheel engine's one spawn
(:func:`wheel_engine_build_child._spawn_build_child`): its own process group, the
gateway's trusted ``PATH`` when started inside the gateway (on a host that has one;
Windows does not, and its git route keeps the gateway's environment as its pip step
does), a cancel that kills it.

Imports: the git route's pieces (``dep_sync``, the child spawn, the cancel) load
with this module, because on that route the checkout has already moved when this
runs, and an import deferred until then would read the incoming revision's files.
The wheel engine itself is imported only by the wheel route, which moves no code
in place, so the gateway's boot path does not initialise the managed-venv engine.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, overload

from kiro_crew import dep_sync
from kiro_crew.config.paths import data_home
from kiro_crew.platform.wheel_engine_build_child import _redacted_detail, _spawn_build_child
from kiro_crew.platform.wheel_engine_contract import ApplyCancel, WheelUpdateError, _BuildContext
from kiro_crew.platform_compat import trusted_system_path

#: What a git-route :func:`apply_release` returns when the code installed but the
#: engine install did not. Distinct from ``dep_sync.REFUSED`` and pip's own ``1``
#: so a caller never takes the dependency repair meant for a failed pip.
ENGINE_FAILED = 3

#: Bound on the engine install child. The engine and its Node runtime are a few
#: hundred MB on first install; a version already on disk returns at once.
ENGINE_INSTALL_TIMEOUT_SECS = 900.0

#: The arguments after the new release's interpreter. ``-I`` like every other
#: child of an update, so an inherited ``PYTHONPATH`` cannot pick the code, and
#: ``-P`` like every self-launch of ``-m kiro_crew``.
ENGINE_INSTALL_ARGS = ("-I", "-P", "-m", "kiro_crew", "kas", "install", "--if-in-use")

#: The variable that names the data home.
DATA_HOME_ENV = "KIROCREW_HOME"

#: The progress line both routes show while the engine step runs.
ENGINE_STEP_MESSAGE = "Installing the KAS engine the new version pins (when this install runs it)…"


#: What argparse prints when the target release predates the engine step: one with
#: no ``kas`` command at all, or one whose ``kas install`` has no ``--if-in-use``.
#: Such a release (a channel move or rollback to an older build) never asked for
#: its engine to come with it, so there is nothing to install for it.
_PREDATES_THE_STEP = (b"invalid choice: 'kas'", b"unrecognized arguments: --if-in-use")


class EngineInstallError(WheelUpdateError):
    """The new release's engine install did not succeed, so nothing was switched."""


@dataclass(frozen=True)
class GitRelease:
    """A release that is a checked-out revision, installed into *target_py*'s venv.

    The caller has already moved the checkout. *cancel* is set by
    :func:`apply_release_supervised`; *trusted_env* is ``False`` only for
    ``kirocrew update`` in the operator's own shell.
    """

    repo: Path
    target_py: Path
    emit: dep_sync.Emit
    timeout: float | None = None
    cancel: ApplyCancel | None = None
    trusted_env: bool = True


@dataclass(frozen=True)
class WheelRelease:
    """A release that is a signed wheel, built into a shadow tree and promoted.

    The fields are :func:`wheel_engine.apply_wheel_update`'s, which this hands on.
    """

    channel: str
    feed_base: str
    artifact_base: str
    expected_version: str
    progress: Callable[[str], None] = lambda _msg: None
    cancel: ApplyCancel | None = None
    on_locked: Callable[[], None] = lambda: None
    preflight: Callable[[], None] = lambda: None
    before_promote: Callable[[], None] = lambda: None
    held_lock_fd: int | None = None
    trusted_env: bool = True


def _data_home_env() -> dict[str, str]:
    """``KIROCREW_HOME`` as this process resolved it, for the engine child.

    The child runs in ``/``, so a relative override would resolve somewhere else
    there and the engine would land outside the data home this install runs on.
    Only an override is pinned: with none, the child resolves the same default.
    """
    if not os.environ.get(DATA_HOME_ENV):
        return {}
    return {DATA_HOME_ENV: str(Path(data_home()).resolve())}


def _install_engine(python: Path, ctx: _BuildContext) -> None:
    """Run the new release's ``kirocrew kas install --if-in-use`` on *python*.

    Raises :class:`EngineInstallError` on a nonzero exit, and the spawn's own
    :class:`WheelUpdateError` (timeout, could not start) or
    ``WheelUpdateCancelled`` otherwise. The child's stderr tail, redacted, is the
    reason a caller shows. A target release that predates the step (argparse's
    usage exit naming it, :data:`_PREDATES_THE_STEP`) is a success: a channel
    move or rollback to an older build must not fail on a flag that build never
    had.
    """
    rc, _out, err = _spawn_build_child(
        [str(python), *ENGINE_INSTALL_ARGS],
        ENGINE_INSTALL_TIMEOUT_SECS,
        "the KAS engine install",
        ctx=ctx,
        env_overrides=_data_home_env(),
    )
    if rc == 2 and any(marker in err for marker in _PREDATES_THE_STEP):
        return
    if rc != 0:
        detail = _redacted_detail(err)
        raise EngineInstallError(
            f"the new version's KAS engine install exited {rc}" + (f": {detail}" if detail else "")
        )


def _apply_git(release: GitRelease) -> int:
    # dep_sync reads a plain Event; the release's cancel sets it, so one cancel
    # reaches both the pip step and the engine child.
    stop = threading.Event()
    undo = release.cancel.on_set(stop.set) if release.cancel is not None else None
    try:
        rc = dep_sync.sync_or_reinstall(
            release.repo, release.target_py, release.emit, timeout=release.timeout, cancel=stop
        )
        if rc != 0:
            return rc
        release.emit(ENGINE_STEP_MESSAGE, False)
        # A host with no trusted system PATH (Windows) runs the child on the
        # gateway's own environment, as dep_sync's pip step there does; refusing
        # it would fail every git update on a platform that route supports.
        trusted = release.trusted_env and trusted_system_path() is not None
        try:
            _install_engine(
                release.target_py,
                _BuildContext(cancel=release.cancel, trusted_env=trusted),
            )
        except WheelUpdateError as exc:
            release.emit(
                f"{exc}. The update stopped before the restart, so the running version "
                "keeps running; the checkout already holds the new code, so run "
                "`kirocrew kas install` before restarting",
                True,
            )
            return ENGINE_FAILED
        return 0
    finally:
        if undo is not None:
            undo()


def _apply_wheel(release: WheelRelease) -> Path:
    # Deferred: see the module docstring. The wheel route moves no code in place,
    # so this import reads the running release's engine.
    from kiro_crew.platform import wheel_engine

    def _engine(tree: Path, ctx: _BuildContext) -> None:
        release.progress(ENGINE_STEP_MESSAGE)
        # The managed venv is POSIX-only; the wheel engine spells its
        # interpreter the same way when it builds the tree.
        _install_engine(tree / "bin" / "python3", ctx)

    return wheel_engine.apply_wheel_update(
        channel=release.channel,
        feed_base=release.feed_base,
        artifact_base=release.artifact_base,
        expected_version=release.expected_version,
        progress=release.progress,
        cancel=release.cancel,
        on_locked=release.on_locked,
        preflight=release.preflight,
        before_promote=release.before_promote,
        held_lock_fd=release.held_lock_fd,
        trusted_env=release.trusted_env,
        install_engine=_engine,
    )


@overload
def apply_release(release: GitRelease) -> int: ...


@overload
def apply_release(release: WheelRelease) -> Path: ...


def apply_release(release: GitRelease | WheelRelease) -> int | Path:
    """Install *release*'s code, then its KAS engine; switch only when both succeeded.

    A :class:`GitRelease` returns ``0`` on success, ``dep_sync``'s code when the
    package install failed or was refused, and :data:`ENGINE_FAILED` when the
    engine install failed; the caller restarts only on ``0``. A
    :class:`WheelRelease` returns the promoted tree and raises the wheel engine's
    errors (an engine failure is an :class:`EngineInstallError`, raised before the
    stable link flips). Blocking; the gateway's callers run it on a worker thread.
    """
    if isinstance(release, GitRelease):
        return _apply_git(release)
    return _apply_wheel(release)


async def apply_release_supervised(executor: Any, release: GitRelease) -> int:
    """:func:`apply_release` for a git release on *executor*, with a cancel that reaches it.

    For the gateway's routes, which move the checkout and then install into the
    venv of the running gateway. A cancelled await cannot stop an executor
    thread, so cancelling the awaiting task (a gateway shutdown does) sets the
    release's cancel: an install still queued, or cancelled before
    ``dep_sync.sync_or_reinstall``'s last check ahead of pip, never starts pip,
    and an engine install in flight is killed (off the event loop: a Windows kill
    blocks on ``taskkill``). A pip already running is left to finish (see
    ``sync_or_reinstall``), and the cancel propagates without waiting for it.
    """
    stop = ApplyCancel()
    install = functools.partial(_apply_git, dataclasses.replace(release, cancel=stop))
    # A job still queued when the cancel lands is cancelled by ``run_in_executor``
    # itself; the cancel reaches one already running.
    fut = asyncio.get_running_loop().run_in_executor(executor, install)
    try:
        return int(await fut)
    except asyncio.CancelledError:
        # Setting the cancel runs its kill hooks synchronously, and on Windows a
        # kill is a blocking ``taskkill``: run it on a worker thread, and wait for
        # it so the engine child is told before the cancel propagates.
        await asyncio.shield(asyncio.to_thread(stop.set, "shutdown"))
        raise


__all__ = [
    "ENGINE_FAILED",
    "ENGINE_INSTALL_ARGS",
    "ENGINE_INSTALL_TIMEOUT_SECS",
    "EngineInstallError",
    "GitRelease",
    "WheelRelease",
    "apply_release",
    "apply_release_supervised",
]
