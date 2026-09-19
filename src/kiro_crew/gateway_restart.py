"""Gateway restart target selection through the composed lifecycle provider."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.platform.context import current_context

#: The pathname this process was launched through, read once at import.
#:
#: Snapshotted here rather than read at restart time because every restart
#: consumer imports this module at module scope, which happens during gateway
#: boot: the value is therefore the one the OS supplied, taken before any agent
#: turn or update apply could rewrite ``sys.argv``. Re-reading the live list at
#: restart time would instead trust whatever wrote to it last.
#:
#: Trust placement: the OS supplies this pathname when it starts the process, the
#: same origin as ``sys.argv[1:]``, which every restart path already re-execs
#: verbatim. It authorizes nothing new — whoever chose it had already chosen the
#: process image.
_LAUNCH_PATHNAME: str = sys.argv[0] if sys.argv else ""


def _console_script_name() -> str:
    """The console-script basename a restart may re-enter on this platform.

    ``kirocrew`` is the single ``[project.scripts]`` entry point, and pip
    generates it as a native ``.exe`` launcher on Windows. Derived rather than
    listed as a set so neither spelling can be accepted on the platform that
    does not produce it.
    """
    return "kirocrew.exe" if platform_compat.IS_WINDOWS else "kirocrew"


#: Ceiling on symlink hops while tracing a route, matching the kernel's own ELOOP
#: limit. A cyclic or absurdly deep chain answers "cannot be walked", which the
#: caller treats as untrusted rather than looping.
_MAX_SYMLINK_HOPS = 40


def _objects_the_kernel_consults(path: Path) -> list[Path] | None:
    """Every filesystem object on the real route to *path*, or ``None``.

    Walks from the root one component at a time and follows each symlink the way
    the kernel does, so an intermediate hop is recorded rather than collapsed away
    by a single ``realpath``. ``None`` when the route cannot be traced -- a cyclic
    chain, one deeper than the kernel would follow, or an unreadable component --
    which the caller treats as untrusted.
    """
    if not path.is_absolute():
        return None
    objects: list[Path] = []
    hops = 0
    current = Path(path.anchor)
    remaining = list(path.parts[1:])
    objects.append(current)
    while remaining:
        current = current / remaining.pop(0)
        objects.append(current)
        while True:
            try:
                if not current.is_symlink():
                    break
                target = os.readlink(current)
            except OSError:
                return None
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                return None
            current = (
                Path(target)
                if os.path.isabs(target)
                else Path(os.path.normpath(current.parent / target))
            )
            objects.append(current)
    return objects


def _agent_could_have_written(path: Path) -> bool:
    """Could a process running as THIS uid have chosen what *path* executes?

    OWNERSHIP first, because a read-only mode proves nothing against its owner:
    the owner may ``chmod u+w`` and then write, so ``os.access(W_OK)`` returning
    False on a file we own is not a restriction on us at all. Measured rather than
    reasoned — mode ``0500`` on both a file and its directory still let their owner
    rewrite the file. Only a path owned by some OTHER uid, which an
    organization-packaged install under ``/usr/local`` or ``/opt`` is, is beyond a
    process running as the gateway.

    The whole ancestor CHAIN is examined, not the file and its immediate directory:
    a rename, a symlink swap, or delete-then-create chooses the target as
    effectively as an in-place rewrite, and it can be done at any level. A shim at
    ``/opt/vendor/bin/kirocrew`` whose every component belongs to root is beyond
    reach, while the same shim under a directory the gateway's uid owns three
    levels up is not: that directory can be swapped for one containing a different
    ``bin/kirocrew``.

    The walk is COMPONENT BY COMPONENT, following each symlink as the kernel does,
    rather than comparing two endpoint spellings. Endpoints are not enough: with
    ``/opt/bin`` a link to ``/srv/bin`` and ``/srv/bin/kirocrew`` a link to
    ``/usr/lib/kirocrew``, the lexical chain holds ``/opt`` and the resolved chain
    holds ``/usr``, while the middle hop ``/srv`` -- where retargeting actually
    happens -- appears in neither. :func:`_objects_the_kernel_consults` enumerates
    every object on the real route, and each one contributes itself and its own
    ancestors.

    The enumeration over-approximates on purpose: it can only add candidates, so it
    only ever makes this stricter. An install refused because some object on its
    route belongs to the gateway's uid is a refusal we can explain; one accepted
    because a hop was never looked at is not.

    Every uncertain answer is ``True``. An unreadable ``stat``, and Windows, where
    this process has no POSIX uid to compare and the ACL question is not one this
    function attempts — there the digest is the only answer that can trust a shim.
    Not knowing is never permission to trust it.
    """
    if not platform_compat.IS_POSIX:
        return True
    try:
        uid = os.geteuid()
        route = _objects_the_kernel_consults(path)
        if route is None:
            return True
        for obj in route:
            for candidate in (obj, *obj.parents):
                if os.stat(candidate).st_uid == uid or os.access(candidate, os.W_OK):
                    return True
    except (OSError, ValueError, AttributeError):
        return True
    return False


def _interpreter_is_beyond_reach(launcher: str) -> bool:
    """Is the program the KERNEL runs for *launcher* also beyond an agent's reach?

    Ownership of the launcher file answers for its own bytes, and for a compiled
    launcher those are the bytes that run. A SCRIPT is different: the kernel reads
    its ``#!`` line and executes the interpreter named there, so a root-owned shim
    over an interpreter the gateway's uid can write still hands that uid the
    executed code. The shim is then beyond reach and the program it runs is not.

    Read the same way :func:`kiro_crew.agent._bin_is_usable` reads a launcher --
    first 4096 bytes, first line, first word -- and judged with the same ownership
    rule as the shim, which already walks both the written and the resolved chain,
    because the kernel resolves the link while an agent that can rewrite the link
    chooses what it points at.

    Refused, not guessed, when the interpreter cannot be identified: a relative or
    bare word, and ``#!/usr/bin/env python3``, whose first word is a root-owned
    finder while the program itself comes from ``PATH`` -- and a gateway's ``PATH``
    can lead with an agent-writable directory, which is the lookup this project
    already treats as untrusted elsewhere.

    What this does NOT police is a trusted script's own content: a root-owned shim
    may name any program it likes in its body. That choice belongs to whoever owns
    the shim, which by this point is not the gateway's uid.
    """
    try:
        with open(launcher, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        return True
    first_line = head.split(b"\n", 1)[0][2:].strip().decode("utf-8", errors="replace")
    words = first_line.split()
    interpreter = words[0] if words else ""
    if not interpreter or not os.path.isabs(interpreter):
        return False
    if os.path.basename(interpreter) == "env":
        return False
    return not _agent_could_have_written(Path(interpreter))


def resolve_restart_launcher() -> str | None:
    """Validate the edition launcher before draining any live sessions.

    Imported by restart consumers before an update can retire their import tree.
    None alone opts into the core's existing Python/managed-venv resolver. A bad
    explicit target or a provider error refuses restart, never falls back to A.
    This is an availability check, not a new authorization boundary: the trusted
    composition root supplies the provider, not request/config/environment data.
    """
    launcher = current_context().gateway_lifecycle.restart_launcher()
    if launcher is None:
        return None
    if not isinstance(launcher, str) or not launcher or "\0" in launcher:
        raise ValueError("Cannot restart: invalid gateway launcher path")
    path = Path(launcher)
    if not path.is_absolute() or not path.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError("Cannot restart: gateway launcher must be an absolute executable file")
    if platform_compat.IS_WINDOWS and path.suffix.lower() != ".exe":
        raise ValueError("Cannot restart: gateway launcher must be a native Windows executable")
    # Do not resolve symlinks: dispatchers can select the app from this basename.
    return launcher


def resolve_launch_shim() -> str | None:
    """The launch pathname, when it can still carry a restart's exec.

    The last resort for a restart whose INTERPRETER is gone. An install shape
    that puts each release in its own versioned directory and removes the
    previous one when it installs the next leaves ``sys.executable`` naming a
    deleted file, and :func:`kiro_crew.platform.wheel_engine.respawn_executable`
    answers that cached path for every layout except the one managed venv it
    knows about. The restart then cannot happen at all — which is the entire
    update, because replacing the running process is what an update is for.

    A stable launcher shim lives OUTSIDE the versioned tree by construction, so
    a launch pathname that still exists after the tree was pruned is by
    definition not inside it. That is what makes it a safe answer here and not a
    way to resurrect the old version: the tree the old version lived in is gone.

    Returns ``None`` whenever the pathname cannot carry an exec, leaving the
    caller's existing refusal in place. This only ever ADDS a target after the
    interpreter guard has already rejected one; it never relaxes that guard, and
    an unusable shim is a refusal rather than a bare ``os.execv`` attempt.

    Validated as :func:`resolve_restart_launcher` validates an edition launcher —
    non-empty, NUL-free, absolute, a present executable file — plus two checks an
    edition launcher does not need, because that one is named by the trusted
    composition root while this one is an ordinary file on disk.

    The basename must be this platform's ``[project.scripts]`` entry point; being
    platform-derived it already carries the sibling's native-``.exe`` rule on
    Windows rather than restating it. And :func:`_agent_could_have_written` must
    answer no for the shim, so an agent running as the gateway cannot decide what a
    restart executes. Its symlinks are deliberately not resolved, for the same
    reason the sibling does not resolve them; requiring the whole ancestor chain to
    belong to another uid is what closes the substitution that leaves open.

    The shim's own ownership is not sufficient on its own, because for a SCRIPT the
    kernel runs the interpreter its ``#!`` line names rather than the script's
    bytes: :func:`_interpreter_is_beyond_reach` applies the same ownership rule to
    that interpreter, so a root-owned shim over an agent-writable interpreter is
    refused instead of executed.

    Scope, and it is narrow. A shim the gateway's own uid could write is REFUSED,
    with no second answer available: a content digest taken at boot would authorize
    it, but only by accepting that the bytes may be swapped again between the check
    and the exec, and an exec of attacker-chosen code is not a residual worth a
    per-user convenience. So this recovers an install whose shim belongs to another
    uid, which an organization-packaged build under ``/usr/local`` or ``/opt`` is,
    and refuses a per-user one. Beyond that, a wrapper that execs an absolute
    versioned interpreter hands its child no surviving reference to itself, and a
    shim published under some other name is not this project's entry point, so
    neither is recoverable from inside the process at all.
    """
    launcher = _LAUNCH_PATHNAME
    if not launcher or "\0" in launcher:
        # os.access and Path.is_file RAISE on an embedded NUL, so this is what
        # keeps an unusable pathname a refusal instead of an exception escaping
        # into a caller that is about to drain every session.
        return None
    path = Path(launcher)
    if path.name != _console_script_name():
        return None
    if not path.is_absolute() or not path.is_file() or not os.access(launcher, os.X_OK):
        return None
    if _agent_could_have_written(path):
        return None
    if not _interpreter_is_beyond_reach(launcher):
        return None
    return launcher
