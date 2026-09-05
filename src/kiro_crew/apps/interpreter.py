"""Shared interpreter resolution for app spawn paths.

One policy, two consumers. The app BACKEND launcher (``backend.py``) and the
app stdio MCP SERVER registration (``bridges.py``) both spawn Python processes
on an app's behalf, and both must refuse to trust a bare ``python3``: a bare
name is resolved through PATH at spawn time, which is not guaranteed to exist
(some hosts ship only a versioned interpreter, so ``execvp("python3")`` raises
FileNotFoundError) and, even when present, may be an older system interpreter
than the one the app's dependencies were installed against — the process then
starts under the wrong interpreter and dies on import, with nothing surfaced
to the user.

The policy: once the gateway has provisioned the app's dependencies
(``pip install --target`` into :func:`app_deps_dir`, reaching the child via
``PYTHONPATH``), the gateway's own ``sys.executable`` is the only
ABI-consistent interpreter and is used unconditionally. An app that
DECLARES ``requirements.txt`` also pins ``sys.executable`` even when
provisioning has not (yet) succeeded: the deps will be provisioned for that
interpreter's ABI on a later start, and letting a shipped venv win in the
interim would flip the ABI out from under the eventual install. Only an app
with NO declared requirements prefers its OWN venv interpreter, when it
ships a version-matched one (a self-consistent environment the app author
built), else falls back to ``sys.executable`` (always an absolute path to a
real interpreter). The gateway itself does not create app venvs. Keeping the
policy in one place is the point — two divergent copies is exactly the
defect class this module removes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from kiro_crew import platform_compat, sandbox
from kiro_crew.apps.registry import minimal_env


def venv_python_path(root: Path) -> Path:
    """The path where ``root``'s venv interpreter would live (may not exist).

    POSIX venvs ship ``bin/python3``; native-Windows venvs ship
    ``Scripts\\python.exe`` and no ``python3`` at all (the same layout split
    ``cli_doctor`` and dev-fleet's ``_venv_python`` already handle).
    """
    if platform_compat.IS_WINDOWS:
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python3"


def app_deps_dir(root: Path) -> Path:
    """Directory an app's ``requirements.txt`` is provisioned into.

    Populated by ``pip install --target`` (``backend.py``'s spawn path) and
    exposed to processes spawned on the app's behalf via ``PYTHONPATH``. A
    plain directory rather than a venv, because venv creation needs
    ``ensurepip`` — which the packaged install's bundled interpreter does not
    ship — and the half-created skeleton a failed attempt leaves behind is
    runnable enough that :func:`resolve_app_python` would prefer it while it
    holds no dependencies at all. A ``--target`` install has no bootstrap
    step, so it cannot leave that trap.

    Beneath ``data/``, not the app root: ``update_app`` replaces the app tree
    but preserves ``data/``, so an update keeps the last good install — a
    restart right after an update works even when pip (or the network) is
    unavailable, and the stamp check reprovisions only when requirements
    actually changed. Staging and prior trees live beside it for the same
    reason (the two swap renames must stay on one filesystem).
    """
    return root / "data" / ".kirocrew-deps"


def app_deps_active(root: Path) -> bool:
    """True when the provisioned deps dir should influence resolution.

    Presence alone is not enough: ``data/`` (where the deps live) survives
    app updates, so an update that REMOVES requirements.txt leaves the tree
    behind — and a stale tree must neither pin the interpreter nor inject
    removed dependency code. Active means the app still declares a
    requirements.txt AND the provisioned tree exists.
    """
    return (root / "requirements.txt").is_file() and app_deps_dir(root).is_dir()


def _runnable(path: Path) -> bool:
    """Executable AND non-empty — the resolution-safety predicate.

    ``is_executable_file`` alone is not enough: on Windows it is an
    extension-allowlist check (there is no execute bit), so a zero-byte
    ``python.exe`` left by an interrupted copy/restore — the same shape as the
    Microsoft-Store reparse stub — would be accepted and then fail at spawn
    time with no diagnostic. An empty file cannot be a working interpreter or
    console script on any platform, so the size check is applied uniformly.
    """
    try:
        return platform_compat.is_executable_file(path) and path.stat().st_size > 0
    except OSError:
        return False


def _venv_is_usable(root: Path) -> bool:
    """Positive evidence that ``root``'s ``.venv`` is a completed, working
    environment for THIS gateway — not a bootstrap skeleton and not a
    cross-ABI copy.

    A minor-version match on ``pyvenv.cfg`` is not enough in either direction
    (the defect this replaces): a failed ``python -m venv`` leaves a skeleton
    whose ``bin/python3`` is the fully-runnable system interpreter and whose
    ``pyvenv.cfg`` names the current minor version, so a version check ACCEPTS
    it; and a venv built by a different minor version but carrying pure-Python
    deps the app needs would be REJECTED. Neither the file's existence nor its
    version field is positive evidence that the interpreter runs and owns its
    site.

    So probe it: run the candidate interpreter and require it to (1) start,
    (2) report a ``sys.prefix`` inside this venv (it is the venv's own
    interpreter, not a bare fallback), and (3) report the SAME
    ``(major, minor)`` as the gateway — provisioned deps reach the child via
    ``PYTHONPATH`` built by ``sys.executable``, so a differing ABI would mix
    wheels. The probe is ``-I`` (isolated: ignores env/user site) and
    ``-S``-free so it sees the venv's own site, timeboxed, and any failure
    (nonzero, timeout, OSError, unparsable output) is treated as "not usable"
    — a fallback to ``sys.executable`` is always safe, so the probe fails
    closed.
    """
    venv_py = venv_python_path(root)
    if not _runnable(venv_py):
        return False
    probe = (
        "import sys,json;"
        "print(json.dumps([sys.prefix, sys.version_info[0], sys.version_info[1],"
        " sys.implementation.name, sys.platform]))"
    )
    # The candidate interpreter is an app-writable file — an app backend can
    # plant an arbitrary executable at .venv/bin/python3 — so the probe runs
    # under the same OS sandbox + resource ceilings as every other app spawn
    # (wrap_argv + cgroup scope + run_limited), never with more privilege
    # than an app spawn gets. On a host with no sandbox backend wrap_argv
    # fail-closes — UNLESS the operator has explicitly opted into
    # unsandboxed execution, in which case this probe (like every app spawn
    # under that opt-in) runs unconfined; the opt-in accepts that for all
    # app code, and the probe adds no exposure beyond the spawn that would
    # follow it. Every probe failure reads as "no positive evidence" and the
    # caller falls back to sys.executable — always a safe answer, so nothing
    # here may propagate an exception into spawn or registration.
    cleanup_path = None
    try:
        argv, cleanup_path = sandbox.wrap_argv([str(venv_py), "-I", "-c", probe], mode="standard")
        argv = sandbox.cgroup_scope_argv(argv)
        # Bounded capture: the probe executable is app-controlled and
        # capture_output would buffer its whole stdout in the GATEWAY's
        # memory — a hostile "python" that floods output must exhaust a
        # capped buffer, not the gateway. stdout goes to a temp file and
        # only a bounded TAIL is read back (the probe's real answer is one
        # short JSON line, printed last); stderr is discarded outright.
        with tempfile.TemporaryFile() as _outbuf:
            proc = sandbox.run_limited(
                argv,
                stdout=_outbuf,
                stderr=subprocess.DEVNULL,
                timeout=10,
                # The probe executable is app-controlled: hand it the same
                # sanitized environment every app subprocess gets, never the
                # gateway's own (which can carry credentials).
                env=minimal_env(),
            )
            _outbuf.seek(0, os.SEEK_END)
            _size = _outbuf.tell()
            _outbuf.seek(max(0, _size - 4096))
            probe_out = _outbuf.read().decode("utf-8", "replace")
    except Exception:
        return False
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass
    if proc.returncode != 0 or not probe_out.strip():
        return False
    try:
        prefix, major, minor, impl, plat = json.loads(probe_out.strip().splitlines()[-1])
    except (ValueError, TypeError):
        return False
    if (major, minor) != (sys.version_info[0], sys.version_info[1]):
        return False
    # Version numbers alone are not ABI: a PyPy (or other-implementation)
    # venv reporting the same major.minor would still crash on the
    # CPython-built native wheels the gateway's pip provisions, and a
    # cross-platform prefix (a copied venv) is equally foreign. Require the
    # implementation and platform to match the gateway's own.
    if impl != sys.implementation.name or plat != sys.platform:
        return False
    try:
        # TypeError included: the probe output is app-controlled, so prefix
        # can be JSON null (or any non-string) and Path(None) must read as
        # "not usable", never escape into spawn/registration.
        return Path(prefix).resolve() == (root / ".venv").resolve()
    except (OSError, TypeError):
        return False


def resolve_app_python(root: Path | None) -> str:
    """Absolute interpreter for processes spawned on an app's behalf.

    When the gateway has provisioned the app's dependencies
    (:func:`app_deps_dir` exists), the answer is ``sys.executable``
    unconditionally: those wheels were built by the gateway's interpreter and
    reach the child via ``PYTHONPATH``, so the gateway's interpreter is the
    only ABI-consistent choice.

    An app that DECLARES ``requirements.txt`` pins ``sys.executable`` even
    while provisioning has not (yet) succeeded — see the module docstring.
    Otherwise (no declared requirements, no provisioned deps), prefers
    ``<root>/.venv``'s interpreter only when
    :func:`_venv_is_usable` gives positive evidence it is a completed,
    same-ABI environment (a probe, not a ``pyvenv.cfg`` heuristic — a failed
    ``venv`` skeleton passes a version check but is not a usable env), else
    ``sys.executable`` — never a bare PATH-resolved name. ``root=None`` means
    "no app context" and resolves straight to ``sys.executable``.
    """
    if root is not None and not app_deps_active(root):
        # A DECLARED requirements.txt owns the interpreter choice even
        # before (or when) provisioning succeeds: the deps mechanism will
        # install for the gateway interpreter, and the app's .venv is at
        # best redundant, at worst a prefix-valid EMPTY skeleton left by a
        # failed `-m venv` (no ensurepip on packaged installs) that passes
        # the usability probe but holds none of the declared dependencies
        # — an ASGI backend launched there dies importing uvicorn. Probe
        # the venv only for apps that manage their own environment (no
        # requirements.txt).
        if not (root / "requirements.txt").is_file() and _venv_is_usable(root):
            return str(venv_python_path(root))
    return sys.executable


def path_command_is_abi_matched(app_root: Path, name: str) -> bool:
    """True when a path-carrying command positively matches the deps ABI.

    Positive means: the path resolves to the gateway's own interpreter, or
    to the app venv's python while that venv passes the interpreter version
    probe (:func:`_venv_is_usable`). Any resolution failure reads as "no
    match" — the server simply does not receive the deps PYTHONPATH.
    """
    try:
        cand = Path(name)
        if not cand.is_absolute():
            # A relative path command resolves against the SESSION's cwd at
            # spawn time, not against the app root this check runs under -
            # a match proven here says nothing about the interpreter that
            # will actually execute. Never a positive match.
            return False
        resolved = cand.resolve(strict=True)
    except OSError:
        return False
    try:
        if resolved == Path(sys.executable).resolve():
            return True
    except OSError:
        return False
    venv_bin = app_root / ".venv" / ("Scripts" if platform_compat.IS_WINDOWS else "bin")
    if not venv_bin.is_dir():
        return False
    try:
        # Validate WHERE the command lives BEFORE dereferencing it: a venv
        # python is normally a SYMLINK to the base interpreter, so the
        # fully-resolved parent is the base install's bin dir, never the
        # venv's — comparing that made this arm unreachable for standard
        # venvs and silently dropped their deps. Resolve the parent chain
        # (that part must be inside .venv/bin) but keep the final component
        # unresolved; whether the interpreter behind the link is actually
        # version-matched is _venv_is_usable's probe to answer.
        if cand.parent.resolve(strict=True) != venv_bin.resolve(strict=True):
            return False
    except OSError:
        return False
    # EXACT interpreter spelling, not a prefix: `.venv/bin/python-worker`
    # is a console script, and injecting interpreter operands into it would
    # kill the server. python / pythonX / pythonX.Y (+.exe) only.
    if not re.fullmatch(r"python[\d.]*(\.exe)?", cand.name.lower()):
        return False
    return _venv_is_usable(app_root)


def venv_provided_command(root: Path, name: str) -> str | None:
    """Absolute path of ``name`` if the app's venv or deps dir provides it.

    Covers console scripts a pip install creates: ``.venv/bin/<name>`` for an
    app-owned venv (probed only when the venv passes the same minor-version
    match as :func:`resolve_app_python`), and ``<deps dir>/bin/<name>`` for
    the gateway's ``pip install --target`` provisioning (``Scripts\\`` on
    Windows in both layouts; the ``.exe`` suffix is appended only when
    ``name`` does not already carry it). Both are invisible to PATH — a venv
    is never activated and a target dir has no activation at all. Only a
    runnable provided binary is a safe rewrite target: anything else a
    manifest names bare (``node``, ``docker``) was a deliberate PATH
    dependency and must be left alone, and a non-executable file (a data
    artifact, a partial pip install) must not displace a command that would
    otherwise work.

    Callers must pass a bare NAME (no path separators, no drive qualifier) —
    the caller-side guard in ``resolve_stdio_command`` enforces that, keeping
    the joins below inside the probed directories.
    """
    if platform_compat.IS_WINDOWS:
        scripts = "Scripts"
        exe_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    else:
        scripts = "bin"
        exe_name = name
    # Mirror resolve_app_python's precedence: once the gateway has
    # provisioned the deps dir, its scripts (shebang: sys.executable) are the
    # only ABI-consistent choice, and a venv script — whose shebang is the
    # venv's own interpreter — is skipped unless the venv is positively usable
    # (probed), because a same-ABI console script from a broken/foreign venv
    # would still fail.
    deps_present = app_deps_active(root)
    bases: list[Path] = []
    if not deps_present and _venv_is_usable(root):
        bases.append(root / ".venv" / scripts)
    # The venv layout is platform-split (bin vs Scripts), but pip's --target
    # uses its "home" scheme, whose script dir is `bin` on EVERY platform —
    # while some pip versions have shipped `Scripts` on Windows instead.
    # Probe both under the deps dir: one extra stat buys correctness across
    # pip versions, and a bare-name join stays inside the probed dirs.
    # Gated on ACTIVE deps (declared AND present): data/ survives updates,
    # so an update that removed requirements.txt must not have a stale tree
    # keep resolving — and executing — removed dependency scripts.
    if deps_present:
        bases.append(app_deps_dir(root) / "bin")
        if platform_compat.IS_WINDOWS:
            bases.append(app_deps_dir(root) / "Scripts")
    for base in bases:
        candidate = base / exe_name
        if _runnable(candidate):
            return str(candidate)
    return None
