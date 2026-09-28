"""The live target: which checkout the gateway actually runs.

Dev Fleet's "Make live" repoints the running gateway at a feature worktree. The
target is recorded in a small pointer file here rather than in the service
definition, because every service manager makes its own definition expensive to
mutate in a different way:

* a systemd **system** unit (what ``kirocrew service install`` writes on Linux)
  lives under ``/etc/systemd/system`` and needs root to change;
* a launchd plist only re-reads on ``bootout`` + ``bootstrap``, and the
  ``bootout`` half kills the very process that would have to run the second
  half;
* a packaged desktop app spawns the backend with no definition we own at all.

The gateway consults the pointer during startup: when it names a different
checkout the process ``execve``s into that checkout's own ``kirocrew``, moving
the working directory and ``PATH`` with it. A cutover is therefore "write the
pointer, then restart", and staging needs no service manager at all.

This is the version-selector/launcher-proxy shape used by ``rustup`` (proxies
read ``rust-toolchain.toml``), the Go toolchain (``go`` reads the ``toolchain``
line in ``go.mod`` and execs that toolchain), and ``rbenv``/``pyenv`` shims. It
also inherits their headline failure mode: a proxy that resolves back to itself
execs forever. :func:`maybe_reexec` is hardened against that twice over — an
env marker that survives the exec, and a realpath comparison that refuses to
exec into the image already running.

Security posture — the pointer decides which code the gateway executes, so it is
a code-execution input:

* It is **keystone-fenced** (``security._SENSITIVE_HOME_DIRS``), so agent tools
  can neither read nor write it. Only a human-driven dashboard action writes it.
* It is **validated before use** (:func:`read_target`), not merely parsed.
* Resolution is **fail-safe, never fail-open**: any malformed, stale or
  unusable pointer is ignored and the currently-installed code boots. A bad
  pointer must not be able to leave the host with no gateway — the hazard a
  persisted bad systemd drop-in carries, since it re-applies on every
  subsequent restart.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import uuid
from pathlib import Path

from kiro_crew.atomic_write import atomic_write, replace_with_retry
from kiro_crew.config import loader

#: Set in the environment of the exec'd image. Its presence means "this process
#: is already the result of a live-target exec", which terminates the chain even
#: if the pointer is somehow still satisfiable — the loop guard that does not
#: depend on path comparison being correct.
EXEC_MARKER = "KIROCREW_LIVE_EXECED"

_FILENAME = "live_target.json"


class InvalidTarget(ValueError):
    """A pointer value cannot be used as a live target.

    The message is operator-facing: it is surfaced by the dashboard when a
    cutover is refused, and logged when a stored pointer is ignored at boot.
    """


def pointer_path() -> Path:
    """Where the live-target pointer lives, inside the active data home."""
    # Resolved THROUGH the module rather than a bound name, so the repo-wide
    # ``kiro_crew.config.loader.config_dir`` patch seam still governs where the
    # pointer lands; importing the function itself would freeze it at import.
    return loader.config_dir() / _FILENAME


def target_bin(checkout: Path) -> Path:
    """The ``kirocrew`` a live target is executed through.

    Always the checkout's OWN venv entry point, never a PATH lookup: the whole
    point of a cutover is to run that checkout's code, and a bare name would
    resolve back to the machine-wide install.
    """
    if sys.platform == "win32":
        return checkout / ".venv" / "Scripts" / "kirocrew.exe"
    return checkout / ".venv" / "bin" / "kirocrew"


def validate(raw: str) -> Path:
    """Return *raw* as a usable live-target checkout, or raise.

    Every rejection is a distinct, actionable message rather than a bare False,
    because these are the reasons the dashboard shows an operator who asked for
    a cutover — and the reasons the gateway logs when it ignores a pointer.
    """
    if not raw or not raw.strip():
        return _reject("the live target is empty")
    # A control character cannot be represented in an argv/env value the way the
    # rest of the plumbing assumes, and a newline would corrupt any consumer
    # that treats the value line-wise. Reject before touching the filesystem.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return _reject("the live target contains control characters")
    try:
        checkout = Path(raw).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        return _reject(f"the live target is not a resolvable path: {exc}")
    if not checkout.is_dir():
        return _reject(f"the live target is not a directory: {checkout}")
    kcbin = target_bin(checkout)
    if not kcbin.is_file():
        return _reject(
            f"the live target has no {kcbin.name} in its .venv — provision it first "
            f"(expected {kcbin})"
        )
    # A present-but-non-executable entry point is worse than a missing one: it
    # would pass a naive existence check and then fail the exec, so the caller
    # must be able to distinguish it.
    if not os.access(kcbin, os.X_OK):
        return _reject(f"the live target's {kcbin} is not executable")
    if not (checkout / "src" / "kiro_crew").is_dir():
        return _reject(
            f"the live target does not look like a Kiro Crew checkout "
            f"(no src/kiro_crew): {checkout}"
        )
    return checkout


def _reject(message: str) -> Path:
    raise InvalidTarget(message)


#: The document that means "no live target" WITHOUT the file being absent.
#:
#: The sandbox masks this pointer from agent subprocesses, and on Linux a mask is a
#: ``mount(2)`` that cannot target a path which does not exist — so an ABSENT pointer is
#: an UNMASKED pointer, and a namespace spawned while it was absent can write one after
#: Dev Fleet creates it, choosing the code the gateway execs into at its next start.
#: ``sandbox._materialize_live_target_mask_target`` closes that by publishing this
#: document before the spawn, which requires a spelling that every reader treats exactly
#: as it treats absence: :func:`read_target_reason` returns ``(None, None)`` for it, so
#: the boot path stays on the installed build and logs nothing, and the dashboard reports
#: no pinned target rather than an unusable one.
#:
#: An explicit ``null`` rather than an empty object, so a MISSING ``checkout`` key keeps
#: its existing complaint: ``{}`` is what a hand-edit produces and ``{"chekout": ...}`` is
#: what a typo produces, and neither should pass silently as "nothing pinned".
NO_TARGET_DOCUMENT: str = json.dumps({"checkout": None}, indent=2) + "\n"


def read_target() -> Path | None:
    """The stored live target, or ``None`` when there is none to honour.

    ``None`` covers every non-usable state — absent file, unreadable file,
    malformed JSON, a value that fails :func:`validate` — deliberately, because
    the only safe reading of "I cannot establish where to go" is "stay here".
    :func:`read_target_reason` is the variant that reports WHY, for the surfaces
    that need to explain themselves.
    """
    target, _reason = read_target_reason()
    return target


def _read_pointer_object() -> tuple[dict | None, str | None]:
    """Parse the pointer once, preserving absent versus malformed semantics."""
    path = pointer_path()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError: undecodable bytes are NOT an
        # OSError, so letting that escape would raise out of the startup
        # bootstrap and crash the gateway on every boot for as long as the
        # pointer sits there — the opposite of this module's fail-safe contract.
        return None, f"the live-target pointer could not be read: {exc}"
    try:
        data = json.loads(raw_text)
    except ValueError:
        return None, f"the live-target pointer is not valid JSON: {path}"
    if not isinstance(data, dict):
        return None, f"the live-target pointer is not a JSON object: {path}"
    return data, None


def read_target_reason() -> tuple[Path | None, str | None]:
    """``(target, reason)`` — at most one of the two is ever set.

    An absent pointer is the ordinary case and yields ``(None, None)``: nothing
    to explain. A pointer that exists but cannot be honoured yields
    ``(None, <why>)`` so the boot log and the dashboard can say what was ignored
    instead of silently running the wrong code.
    """
    data, reason = _read_pointer_object()
    if data is None:
        return None, reason
    raw = data.get("checkout")
    if raw is None and "checkout" in data:
        # The mask's absent-equivalent document (:data:`NO_TARGET_DOCUMENT`), or an
        # operator clearing the pin without deleting the file. Indistinguishable from
        # absence BY DESIGN — same ``(None, None)``, so no caller can tell the
        # materialised stub from a host that never pinned anything.
        return None, None
    if not isinstance(raw, str):
        return None, f"the live-target pointer has no 'checkout' string: {pointer_path()}"
    try:
        return validate(raw), None
    except InvalidTarget as exc:
        return None, str(exc)


def read_previous_target() -> Path | None:
    """Return the validated one-level undo target, or ``None`` when unavailable.

    Undo history is actionable code-execution state, so the previous checkout
    must validate before it is offered. The current field only needs the pointer
    schema's string shape: a running checkout can lose its venv after cutover
    without making an otherwise safe previous checkout unusable.
    """
    data, _reason = _read_pointer_object()
    if data is None:
        return None
    current = data.get("checkout")
    previous = data.get("previous_checkout")
    if not isinstance(current, str) or not isinstance(previous, str):
        return None
    try:
        return validate(previous)
    except InvalidTarget:
        return None


def write_target(
    checkout: Path | str,
    *,
    previous_checkout: Path | str | None = None,
) -> Path:
    """Validate and store the live target plus optional one-level undo history.

    Both paths validate BEFORE the write, so neither an unusable target nor an
    unusable rollback destination can become persisted code-execution state.
    The previous field is omitted when absent or equal to the new target; old
    readers continue to consume the unchanged ``checkout`` field.
    """
    resolved = validate(str(checkout))
    previous = validate(str(previous_checkout)) if previous_checkout is not None else None
    data = {"checkout": str(resolved)}
    if previous is not None and previous != resolved:
        data["previous_checkout"] = str(previous)
    payload = json.dumps(data, indent=2) + "\n"
    _publish_pointer(payload)
    return resolved


#: Directory (a direct child of the crew data home) the pointer's temp file is staged
#: in. Spelled here rather than imported from ``kiro_crew.sandbox`` to keep this module
#: off that import chain; ``test_sandbox_dev_fleet_live_target.py`` pins the two equal.
#: The sandbox launcher masks this directory in every agent namespace.
_STAGING_LEAF = "live-target-staging"


def _publish_pointer(payload: str) -> None:
    """Atomically replace the pointer with *payload*, staged where no sandbox can see it.

    A plain ``atomic_write`` stages its temp file BESIDE the target — in the data-home
    root, which every sandbox can see and, being same-uid, ``link(2)``. A hard link
    taken on the temp before the rename is a second name for the inode the gateway
    ``execve``s from after the rename, outside the mask on the pointer's own name. So
    the temp lives in ``_STAGING_LEAF``, a masked directory on the same filesystem,
    written by ``atomic_write`` with the same owner-only hardening (POSIX mode and the
    Windows ACL lockdown before the payload lands), and the published inode is then
    checked: a regular file with exactly one link, or the pointer is removed again
    and the write refused — an absent pointer boots the gateway's own image, which is
    the safe default.
    """
    path = pointer_path()
    staging = path.parent / _STAGING_LEAF
    # Refuse to stage through a name an agent could have planted: the directory must be
    # a real directory (not a symlink), owner-only where modes exist.
    try:
        st = os.lstat(staging)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        # exist_ok: the sandbox materialiser creates the same directory concurrently.
        staging.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(staging)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(
            errno.ENOTDIR,
            f"{staging} is not a directory; refusing to stage the live-target pointer through it",
        )
    if os.name != "nt" and st.st_mode & 0o077:
        # A wider mode lets another account list the temps' names (the payload itself
        # is owner-only); refused rather than repaired, like the other planted-state
        # checks on this path, so a directory nobody here created is never silently
        # adopted.
        raise OSError(
            errno.EPERM,
            f"{staging} is not owner-only (mode {stat.S_IMODE(st.st_mode):o}); "
            "chmod 700 it or remove it, then retry the cutover",
        )
    staged = staging / f"pointer-{os.getpid()}-{uuid.uuid4().hex}.json"
    atomic_write(staged, payload, restrict_to_owner=True)
    try:
        replace_with_retry(staged, path)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    try:
        final = os.lstat(path)
    except FileNotFoundError:
        # Replaced from under us before we could look: someone else owns the pointer
        # now, and nothing of ours is published. Nothing to verify.
        return
    if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1:
        path.unlink(missing_ok=True)
        raise OSError(
            errno.EMLINK,
            f"refusing to publish {path}: the written pointer has {final.st_nlink} hard "
            "link(s), so a second name would reach the checkout the gateway starts next "
            "outside the sandbox mask; the pointer was removed",
        )


def snapshot() -> str | None:
    """The pointer's raw content, or ``None`` when the file is absent.

    Only absence maps to ``None``; an unreadable or undecodable file propagates.
    The caller uses this to make a cutover reversible, and ``restore(None)``
    UNPINS the target (the pointer becomes the absent-equivalent stub) — so
    reporting a file we merely could not read as "there was nothing here" would
    let a failed cutover destroy a live target.
    """
    try:
        return pointer_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def restore(prior: str | None) -> bool:
    """Put the pointer back to *prior*, or unpin it when that was ``None``.

    "Unpin" publishes :data:`NO_TARGET_DOCUMENT` rather than unlinking. Every
    reader treats the stub exactly as absence, so the observable rollback is the
    same — but an absent NAME is what the sandbox mask cannot cover: a launcher
    building a namespace between this rollback and its own materialising stub
    would find nothing to mount over, and an agent in that window could create
    the pointer and choose the checkout the gateway ``execve``s into next. The
    stub keeps a maskable regular file under the name at every instant.

    Best-effort by contract: returns ``False`` rather than raising, so a caller
    unwinding a failed cutover can report that the rollback itself did not land
    instead of losing the original failure.
    """
    try:
        # The same publisher as write_target for BOTH branches: a rollback must
        # not be the step that widens access to the pointer, nor the step that
        # stages it where a sandbox can link it, nor the step that leaves the
        # name absent. A failure surfaces as the OSError this except maps to
        # ``False`` — with nothing unprotected left published.
        _publish_pointer(NO_TARGET_DOCUMENT if prior is None else prior)
        return True
    except OSError:
        return False


def _current_image() -> str:
    """Realpath of the executable backing THIS process, for the loop guard."""
    argv0 = sys.argv[0] if sys.argv else ""
    try:
        return os.path.realpath(argv0) if argv0 else ""
    except OSError:
        return ""


def maybe_reexec(argv: list[str], *, log: object = None) -> None:
    """Exec into the stored live target, or return so the caller boots normally.

    Called from the gateway's startup path before the gateway lock is acquired
    and before any socket is bound, so an exec here leaves nothing half-done
    behind.

    Returns — rather than raising — in every "stay here" case: no pointer, an
    unusable pointer, the pointer naming the image already running, or a
    previous exec having already happened in this chain. Only a successful
    ``execve`` does not return.
    """
    if os.environ.get(EXEC_MARKER):
        return
    target, reason = read_target_reason()
    if target is None:
        if reason:
            _warn(log, f"ignoring the live target and starting the installed build: {reason}")
        return
    kcbin = target_bin(target)
    # The loop guard proper: if the pointer names the image already executing,
    # exec'ing would replace this process with itself, forever. Compare resolved
    # paths so a symlinked entry point cannot slip past.
    try:
        same_image = os.path.realpath(kcbin) == _current_image()
    except OSError:
        same_image = False
    if same_image:
        return
    env = {
        **os.environ,
        EXEC_MARKER: "1",
        # The target's own source tree, so skills/agent-spec resolution follows
        # the code being executed rather than the install that launched it.
        "KIROCREW_PROJECT_DIR": str(target),
        # The target's venv leads PATH. Without this, a bare ``kirocrew`` in a
        # subprocess (or an agent shell turn) resolves to the machine-wide
        # install, so the gateway would run the target while everything it
        # spawns re-invoked the old build.
        "PATH": os.pathsep.join([str(kcbin.parent), os.environ.get("PATH", "")]),
    }
    _warn(log, f"live target set: executing {kcbin}")
    try:
        os.chdir(target)
    except OSError as exc:
        # A cwd we cannot enter is not fatal on its own, and refusing the whole
        # cutover over it would strand the operator on the old build with no way
        # to move; the exec below still runs the right code.
        _warn(log, f"could not chdir to the live target {target}: {exc}")
    try:
        # The executable is not caller-supplied: it is derived from the
        # keystone-fenced pointer, which only a human-driven dashboard action
        # writes, and `validate` has already confirmed it is an executable file
        # inside a Kiro Crew checkout. argv is this process's own argv, and env is
        # the inherited environment plus three keys computed here. The rule fires
        # on passing an environment through at all, which is inherent to handing a
        # gateway its own env across the exec.
        os.execve(  # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
            str(kcbin),
            [str(kcbin), *argv],
            env,
        )
    except OSError as exc:
        # Fail SAFE: an exec that could not even start leaves this process
        # intact, so continue booting the installed build rather than dying with
        # no gateway at all.
        _warn(log, f"could not execute the live target {kcbin}: {exc}")


def _warn(log: object, message: str) -> None:
    """Emit *message* through *log* when given, else to stderr.

    The bootstrap runs before logging is configured, so stderr is the only sink
    guaranteed to exist — and these messages explain why the process is running
    code other than the installed build, which must never be silent.
    """
    warn = getattr(log, "warning", None)
    if callable(warn):
        warn("live-target: %s", message)
        return
    print(f"kirocrew: live-target: {message}", file=sys.stderr)
