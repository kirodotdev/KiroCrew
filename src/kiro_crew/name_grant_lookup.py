"""How the modelled shell looks a program name up, and which file answers it.

The PowerShell ``PATH`` walk and the extension refusals it can return, the helpers that
read a name in the modelled platform's spelling, the assignment names that decide what
runs, shebang following, the trusted-system-file test, and the content digest and
identity an approval pin records (:func:`_identity`).

An owner module of :mod:`kiro_crew.name_grant`, the one import path: callers, tests and
documents reach every name here as ``kiro_crew.name_grant.<name>``. It imports
:mod:`kiro_crew.name_grant_refusals` and :mod:`kiro_crew.name_grant_tables`, and nothing
here reads a name the tests patch through the facade (``test/test_name_grant_facade.py``
holds it to that). New lookup work goes here.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import re
import stat

from kiro_crew import platform_compat
from kiro_crew.name_grant_refusals import (
    AMBIGUOUS_ENV,
    FILE_ASSOCIATION,
    Refusal,
)
from kiro_crew.name_grant_tables import (
    _COMSPEC_SCRIPT_EXTENSIONS,
    _DEFAULT_PATHEXT,
    _WINDOWS_RUNNABLE_EXTENSIONS,
)


def _windows_extensions() -> tuple[str, ...]:
    """The extensions PowerShell appends to a bare name, in the order it tries them.

    `.ps1` FIRST, then `PATHEXT` in order: measured on 5.1, a directory holding
    `tool.ps1` and `tool.exe` runs the script. Read from the environment because
    a host can extend `PATHEXT` (this one carries `.PY`), and the shell uses
    what it was given.

    Deduplicated, keeping first position: a host whose `PATHEXT` lists `.PS1`
    itself would otherwise have `.ps1` appear twice, probing each directory for
    the same file twice. The shell tries a spelling once, so this does too.
    """

    raw = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    ordered = (".ps1",) + tuple(ext.lower() for ext in raw.split(";") if ext.startswith("."))
    return tuple(dict.fromkeys(ordered))


def _windows_which(name: str, path: str) -> str | None:
    """PowerShell's resolution of a bare *name* over *path*, or ``None``.

    Directory-major: every candidate spelling is tried in the first directory
    before the second is looked at, which is what lets a later directory's
    `.exe` lose to an earlier one's `.ps1`. A name that already carries one of
    the extensions is tried as written first (`whoami.EXE` runs). Existence is
    the whole test -- Windows has no execute bit, and the shell runs any file
    with the right extension -- so the POSIX ``shutil.which`` is not used here:
    it neither knows `.ps1` nor puts it first.
    """

    extensions = _windows_extensions()
    lowered = name.lower()
    candidates = [name] if any(lowered.endswith(ext) for ext in extensions) else []
    candidates.extend(name + ext for ext in extensions)
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        for candidate in candidates:
            hit = _windows_case_insensitive_file(directory, candidate)
            if hit is not None:
                return hit
    return None


def _windows_case_insensitive_file(directory: str, candidate: str) -> str | None:
    """A file named *candidate* in *directory*, matched the way Windows matches.

    Windows file names are case-insensitive, so `FIND.exe` runs `find.exe`. On a
    case-sensitive host (a POSIX CI runner with the Windows model patched on) a
    plain ``os.path.isfile`` would answer for the exact case only, so the Windows
    model would fail to resolve a mixed-case spelling the shell resolves. The
    fast path is the exact hit -- the only one that exists on a real Windows host
    -- and only a miss falls back to a case-folded directory scan, so production
    (where `os.path.isfile` already matches case-insensitively) never pays for
    the scan.
    """

    full = os.path.join(directory, candidate)
    if os.path.isfile(full):
        return full
    target = candidate.lower()
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    for entry in entries:
        if entry.lower() == target:
            hit = os.path.join(directory, entry)
            if os.path.isfile(hit):
                return hit
    return None


def _windows_extension_refusal(name: str, found: str) -> Refusal | None:
    """Refuse a hit Windows would run through something this check cannot pin."""

    ext = os.path.splitext(found)[1].lower()
    if ext not in _WINDOWS_RUNNABLE_EXTENSIONS:
        return Refusal(
            FILE_ASSOCIATION,
            f"{name} resolves to {found}, which Windows runs through the program "
            f"registered for {ext or 'files without an extension'} -- a registry "
            "choice this check cannot identify",
        )
    if ext in _COMSPEC_SCRIPT_EXTENSIONS:
        comspec = os.environ.get("COMSPEC") or ""
        try:
            real = os.path.realpath(comspec) if comspec else ""
        except (OSError, ValueError):
            real = ""
        if not real or not _is_trusted_system_file("cmd", real):
            return Refusal(
                AMBIGUOUS_ENV,
                f"{name} resolves to the batch file {found}, which runs under COMSPEC, "
                "and COMSPEC does not name the system cmd.exe",
            )
    return None


def _program_key(name: str) -> str:
    """The name a program is matched under in the dispatcher table.

    The basename as written on POSIX. On Windows names are case-insensitive and
    an extension is optional, so `POWERSHELL.EXE` and `powershell` must both meet
    the `powershell` entry: lower-cased, with a `PATHEXT`/`.ps1` extension
    removed. Alias tables are NOT consulted through this -- `sort.exe` written
    with its extension bypasses PowerShell's alias, and must bypass ours.
    """

    base = _model_basename(name)
    if not _model_is_windows():
        return base
    base = base.lower()
    root, ext = os.path.splitext(base)
    if ext in _windows_extensions():
        return root
    return base


def _model_is_windows() -> bool:
    """The path/name FLAVOUR the model reasons in, derived from the model flag.

    The Windows model is selected by :data:`platform_compat.IS_WINDOWS`, which
    the tests patch to exercise the Windows lexer, resolution order and refusal
    codes on a POSIX runner. The host's own ``os.path`` module and ``os.sep`` do
    NOT follow that flag -- on Linux they stay POSIX -- so a name-shape or
    case-folding decision made through the host module silently answers with the
    wrong flavour under the patched flag, passing on a real Windows host and
    failing only on the POSIX CI shard. Every model decision about a NAME (its
    separators, whether it is absolute, whether two names fold together) goes
    through the helpers below so the answer depends on the model, not the host.
    """

    return platform_compat.IS_WINDOWS


def _model_normcase(text: str) -> str:
    """Case-fold *text* the way the MODEL compares names, not the host.

    Windows file names are case-insensitive, so `Git` and `git` name one file;
    POSIX names are case-sensitive, so they name two. ``os.path.normcase`` is
    the host's rule (identity on POSIX, lower-case on Windows) and cannot be
    patched by the model flag, so the model uses this instead.
    """

    return text.lower() if _model_is_windows() else text


def _model_basename(path: str) -> str:
    """The final component of *path*, split the way the MODEL splits paths.

    ``ntpath.basename`` when the model is Windows -- so
    ``C:\\Windows\\System32\\cmd.exe`` yields ``cmd.exe`` -- and
    ``posixpath.basename`` otherwise. ``os.path.basename`` follows the HOST, not
    the model flag, so on a POSIX runner with the Windows model patched on it
    would return the whole backslash string and the name-shape decisions built on
    it (the dispatcher key, the pin key, the trusted-system and inert-builtin
    consults) would silently misfire. On a real Windows host ``os.path`` IS
    ``ntpath``, so production behaviour is byte-identical.
    """

    return (ntpath if _model_is_windows() else posixpath).basename(path)


def _model_sep_in(name: str) -> bool:
    """Whether *name* carries a separator the MODEL treats as making it a path.

    ``/`` is a separator in both flavours. A backslash is a separator ONLY in
    the Windows model -- on POSIX it is an escape character (``grep '\\d'``), so
    the model must not read a backslash as path-shaped there. Keyed on the model
    flag rather than ``os.sep`` (which is a backslash only on a Windows host).
    """

    if "/" in name:
        return True
    return _model_is_windows() and "\\" in name


def _is_absolute(name: str) -> bool:
    """Whether *name* pins a location without reference to a working directory.

    On Windows ``ntpath.isabs`` alone is not that: ``\\tool.exe`` is rooted on
    the CURRENT drive and ``C:tool.exe`` is relative to C:'s current directory,
    and both change meaning with a working directory the approval never saw.
    Only a path with a drive or UNC prefix is absolute here.

    The path flavour is chosen from the MODEL flag, not the host: ``os.path`` is
    POSIX on a Linux runner even when the Windows model is patched on, so an
    absolute Windows path would read as relative there. ``ntpath`` gives the
    Windows answer on any host, and ``posixpath`` the POSIX answer, so the model
    is deterministic per flag. On a real Windows host ``os.path`` IS ``ntpath``,
    so production behaviour is byte-identical.
    """

    pathmod = ntpath if _model_is_windows() else posixpath
    if not pathmod.isabs(name):
        return False
    if _model_is_windows() and not pathmod.splitdrive(name)[0]:
        return False
    return True


#: A STRICT shell assignment name. Anything else carrying `=` in a command
#: position is not a plain `NAME=value` prefix and is refused rather than skipped.
_ASSIGN_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Environment variables whose value in a COMMAND POSITION decides which file
#: runs, or what code runs inside it. `PATH=/writable/bin head file` re-points
#: the very lookup ``name_grant`` vouches for, and the loader variables inject code
#: into whatever does run, so a command carrying one cannot be answered for by
#: resolving its program name. An ordinary assignment (`FOO=bar head x`) is left
#: alone: it changes the program's inputs, not its identity.
_EXEC_ENV_VARS = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "BASH_ENV",
        "ENV",
        "SHELL",
        "IFS",
        # A TOOL that runs a helper command named by its environment. The base
        # program is genuinely the trusted system one, so resolving its name
        # answers nothing about what executes: `GIT_SSH_COMMAND=/writable/evil
        # git fetch ssh://x` vouches for `/usr/bin/git` and runs the planted
        # file. Same shape as `LD_PRELOAD`, reached through a tool's own config.
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "GIT_EXTERNAL_DIFF",
        "GIT_PAGER",
        "GIT_EDITOR",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        # An INTERPRETER told to load extra code before the script it was given.
        # `PYTHONPATH=/writable python3 x` with a planted `sitecustomize.py`, or
        # `NODE_OPTIONS=--require=/writable/evil node x`, both run attacker code
        # inside a program this check called trusted.
        "NODE_OPTIONS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "RUBYOPT",
        "RUBYLIB",
        "GEM_PATH",
        "GEM_HOME",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "CLASSPATH",
    }
)

#: Assignment-name FAMILIES that decide what code runs. Named as families rather
#: than as more exact spellings because this is the shape that kept recurring:
#: every round found one more interpreter or tool with its own way of being told
#: to load code, and an exact list can only ever be as complete as the last
#: person to think about it. A loader prefix (``LD_*``, ``DYLD_*``) and the
#: option/search-path suffixes cover the families themselves.
#:
#: Over-refusing is the safe direction and its cost is bounded: an assignment
#: like ``PYTHONUNBUFFERED=1`` or ``MY_CONFIG_PATH=/x`` costs ONE approval
#: prompt, because a refusal here never blocks and never rewrites the command.
_EXEC_ENV_PREFIXES = ("LD_", "DYLD_")
_EXEC_ENV_SUFFIXES = ("_OPTIONS", "OPT", "PATH", "LIB", "_PRELOAD")


def _decides_execution(head: str) -> bool:
    """Whether assigning *head* decides WHAT runs, not merely a program's inputs."""

    return (
        head in _EXEC_ENV_VARS
        or head.startswith(_EXEC_ENV_PREFIXES)
        or head.endswith(_EXEC_ENV_SUFFIXES)
    )


#: Returned by :func:`_shebang_interpreter` for an ``env`` shebang carrying more
#: than one bare name. A sentinel rather than ``None`` or a guess: ``None`` means
#: "no shebang, nothing to follow" and would ALLOW the command, and guessing the
#: interpreter validates the wrong file while vouching for the right one. Not a
#: possible filename, so it cannot collide with a real interpreter.
_COMPLEX_ENV_SHEBANG = "\x00complex-env-shebang"

#: How deep an interpreter chain is followed. A shebang normally names an ELF
#: binary, so one step ends it; two allows for a wrapper script in between.
_INTERPRETER_DEPTH = 2


def _shebang_interpreter(real: str) -> str | None:
    """The interpreter a script hands itself to, or ``None`` for a binary.

    Pinning a SCRIPT binds its bytes, and its bytes may say
    ``#!/usr/bin/env node`` -- which resolves ``node`` from ``PATH`` at exec time,
    exactly the lookup this module exists to distrust. The script can stay
    byte-identical while the program that actually runs is replaced underneath
    it, so the interpreter has to face the same questions the script did.

    Returns the interpreter as written: an absolute path (``/bin/sh``) or a bare
    name when the shebang goes through ``env``. Flags are skipped, so
    ``#!/usr/bin/env -S node --flag`` yields ``node``. ``None`` means there is no
    shebang to follow -- a binary, or a file whose first bytes are not ``#!``.
    """

    if not _is_regular_file(real):
        # Same reason the digest refuses one: opening a FIFO here would block a
        # worker thread in the kernel forever.
        return None
    try:
        with open(real, "rb") as handle:
            first = handle.readline(256)
    except (OSError, ValueError):
        return None
    if not first.startswith(b"#!"):
        return None
    try:
        tokens = first[2:].decode("utf-8", "replace").strip().split()
    except ValueError:  # pragma: no cover - decode with errors= cannot raise
        return None
    if not tokens:
        return None
    interpreter = tokens[0]
    if os.path.basename(interpreter) in ("env", "env.exe"):
        # The `env` BINARY is what the kernel runs, so it decides what executes
        # no matter which name follows it. Reading the name and forgetting the
        # path would validate `node` while `#!~/.local/bin/env node` runs a
        # planted `env` -- and because a pin binds the SCRIPT's bytes, the script
        # keeps matching while the file behind its shebang is swapped. So the
        # path is held to the same standard as any other program: it must BE the
        # system `env`, not merely be spelled like it.
        try:
            real_env = os.path.realpath(interpreter)
        except (OSError, ValueError):
            return _COMPLEX_ENV_SHEBANG
        if not _is_trusted_system_file(os.path.basename(interpreter), real_env):
            return _COMPLEX_ENV_SHEBANG
        # ONLY the bare `#!/usr/bin/env NAME` form is read. `env`'s options take
        # operands (`-u VAR`, `-S 'cmd args'`, `--chdir=DIR`), so picking the
        # first non-flag token mistakes `VAR` for the interpreter -- which is
        # worse than not answering, because it validates the wrong file and
        # vouches for the command. Anything more complex than one bare name is
        # refused by returning a token that cannot resolve.
        rest = tokens[1:]
        if len(rest) == 1 and not rest[0].startswith("-") and "=" not in rest[0]:
            return rest[0]
        return _COMPLEX_ENV_SHEBANG
    return interpreter


#: Read size for the identity digest. The WHOLE file is digested -- this is only
#: the chunk size. Capping the digest and hashing only a large file's head and
#: tail would leave a middle-only rewrite of a big binary undetected when it also
#: preserved the size and landed inside one ctime tick.
#: Refusing large files instead would have been worse: `node`, `gh` and `docker`
#: are all above any sane cap, and they are exactly what people grant.
_DIGEST_CHUNK = 1 << 20


def _is_regular_file(real: str) -> bool:
    """Whether *real* is a REGULAR file, so reading it cannot block forever.

    A FIFO passes every test that matters to a ``PATH`` lookup -- it exists, it
    can carry the execute bit, it is not a directory -- so ``shutil.which``
    returns it and the digest below would ``open()`` it ``O_RDONLY``, which blocks
    in the kernel until a writer appears. On a worker thread that is permanent:
    an outer timeout can cancel the await but cannot free a syscall-blocked
    thread, so repeating it drains the shared executor and stalls every other
    session's approvals. A character or block device is the same shape. Only a
    regular file is read.
    """

    try:
        return stat.S_ISREG(os.stat(real).st_mode)
    except (OSError, ValueError):
        return False


def _content_digest(real: str, size: int) -> str | None:
    """A digest of ALL of *real*'s bytes, with the size mixed in.

    Metadata alone cannot answer "is this the same program": ``mtime`` and
    ``size`` are both under the writer's control (a same-size rewrite followed by
    ``os.utime`` restores the pair exactly), and while ``st_ctime_ns`` is
    kernel-set and unrestorable, its clock has a tick -- a rewrite inside the
    same tick as the pin leaves it equal, measured on both tmpfs and xfs. So the
    digest is what actually decides, and the metadata rides along to catch the
    cheap cases first.

    The whole file, not a window: hashing only the head and tail left a
    middle-only rewrite of a large binary undetected when it preserved the size
    and landed in one ctime tick. The cost is read bandwidth on an auto-approve
    decision -- for a ~100 MB interpreter roughly a quarter second, page-cached
    after the first pass, and always on a worker thread, never the event loop.
    Only a NON-system program reaches here (a system-resolved one is identified
    by its own directory), so the ordinary read-only allowlist never pays it.
    """

    if not _is_regular_file(real):
        return None
    try:
        digest = hashlib.sha256()
        digest.update(str(size).encode())
        with open(real, "rb") as handle:
            while True:
                chunk = handle.read(_DIGEST_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _identity(real: str) -> tuple | None:
    """A value that changes whenever the file behind a name changes.

    Content first (:func:`_content_digest`), with the metadata that a writer
    cannot restore -- inode, device, and the kernel-set ``st_ctime_ns`` -- as
    corroboration. ``mtime`` and ``size`` are included for completeness but are
    NOT what the guarantee rests on: both are forgeable by the same-uid process
    this pin exists to catch.
    """

    try:
        st = os.stat(real)
    except (OSError, ValueError):
        return None
    digest = _content_digest(real, st.st_size)
    if digest is None:
        # Unreadable, or not a regular file at all (a planted FIFO resolves like
        # a program but cannot be identified as one). Either way there is nothing
        # to pin, and the caller turns this into a refusal.
        return None
    return (
        # Folded the way the model compares names: a case-insensitive model
        # reaches one file through several spellings, and the resolver hands
        # back whichever spelling the caller asked for, so the raw string
        # would give one file as many identities as it has spellings.
        _model_normcase(real),
        digest,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_size,
        st.st_ino,
        st.st_dev,
    )


def _is_trusted_system_file(name: str, real: str) -> bool:
    """Whether *real* IS the trusted system program called *name*."""

    system = platform_compat.trusted_system_bin(name)
    if system is None:
        return False
    try:
        return _model_normcase(os.path.realpath(system)) == _model_normcase(real)
    except (OSError, ValueError):
        return False
