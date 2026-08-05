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

import json
import os
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import loader

#: Set in the environment of the exec'd image. Its presence means "this process
#: is already the result of a live-target exec", which terminates the chain even
#: if the pointer is somehow still satisfiable — the loop guard that does not
#: depend on path comparison being correct.
EXEC_MARKER = "KIROCREW_LIVE_EXECED"

#: Owner-only: the pointer is a code-execution input, and mode does not isolate
#: another process running as the same UID, but a world-writable one would be
#: strictly worse.
_MODE = 0o600

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


def read_target_reason() -> tuple[Path | None, str | None]:
    """``(target, reason)`` — at most one of the two is ever set.

    An absent pointer is the ordinary case and yields ``(None, None)``: nothing
    to explain. A pointer that exists but cannot be honoured yields
    ``(None, <why>)`` so the boot log and the dashboard can say what was ignored
    instead of silently running the wrong code.
    """
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
    raw = data.get("checkout")
    if not isinstance(raw, str):
        return None, f"the live-target pointer has no 'checkout' string: {path}"
    try:
        return validate(raw), None
    except InvalidTarget as exc:
        return None, str(exc)


def write_target(checkout: Path | str) -> Path:
    """Validate and store *checkout* as the live target. Returns the resolved path.

    Validation happens BEFORE the write, so an unusable target is refused up
    front rather than persisted and then ignored on every subsequent boot.
    """
    resolved = validate(str(checkout))
    payload = json.dumps({"checkout": str(resolved)}, indent=2) + "\n"
    path = pointer_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, payload, mode=_MODE)
    # atomic_write's mode is a POSIX permission bit and a no-op on Windows, which
    # would leave a code-execution input inheriting the directory's ACL there.
    # restrict_to_owner applies the owner-only DACL instead, so the pointer is
    # owner-only on every platform.
    platform_compat.restrict_to_owner(path)
    return resolved


def snapshot() -> str | None:
    """The pointer's raw content, or ``None`` when the file is absent.

    Only absence maps to ``None``; an unreadable or undecodable file propagates.
    The caller uses this to make a cutover reversible, and ``restore(None)``
    DELETES the pointer — so reporting a file we merely could not read as "there
    was nothing here" would let a failed cutover destroy a live target.
    """
    try:
        return pointer_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def restore(prior: str | None) -> bool:
    """Put the pointer back to *prior* (deleting it when that was ``None``).

    Best-effort by contract: returns ``False`` rather than raising, so a caller
    unwinding a failed cutover can report that the rollback itself did not land
    instead of losing the original failure.
    """
    path = pointer_path()
    try:
        if prior is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, prior, mode=_MODE)
            # Harden the restored file for the same reason write_target does:
            # atomic_write's mode is a POSIX permission bit and a no-op on
            # Windows, so without this the pointer — a code-execution input read
            # at every startup — would come back inheriting the directory's ACL.
            # A rollback must not be the step that widens access to it.
            platform_compat.restrict_to_owner(path)
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
