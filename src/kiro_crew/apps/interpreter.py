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

The policy: prefer the app's OWN venv interpreter (that is where the app's
``requirements.txt`` was installed, so it is the only interpreter guaranteed
to carry the app's dependencies), else fall back to the gateway's own
``sys.executable`` (always an absolute path to a real interpreter). Keeping
the policy in one place is the point — two divergent copies is exactly the
defect class this module removes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kiro_crew import platform_compat


def venv_python_path(root: Path) -> Path:
    """The path where ``root``'s venv interpreter would live (may not exist).

    POSIX venvs ship ``bin/python3``; native-Windows venvs ship
    ``Scripts\\python.exe`` and no ``python3`` at all (the same layout split
    ``cli_doctor`` and dev-fleet's ``_venv_python`` already handle).
    """
    if platform_compat.IS_WINDOWS:
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python3"


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


def resolve_app_python(root: Path | None) -> str:
    """Absolute interpreter for processes spawned on an app's behalf.

    Prefers ``<root>/.venv``'s interpreter when it exists as a runnable,
    non-empty executable (the app's own dependencies live there), else the
    gateway's ``sys.executable`` — never a bare PATH-resolved name. The
    runnability check matters: a venv interpreter that lost its execute bit or
    was truncated to zero bytes (a partial copy, a restore that dropped
    content) would turn a working ``sys.executable`` fallback into a
    guaranteed spawn failure. ``root=None`` means "no app context" and
    resolves straight to ``sys.executable``.
    """
    if root is not None:
        venv_py = venv_python_path(root)
        if _runnable(venv_py):
            return str(venv_py)
    return sys.executable


def venv_provided_command(root: Path, name: str) -> str | None:
    """Absolute path of ``name`` if the app's venv provides it, else ``None``.

    Covers console scripts a venv install creates (``.venv/bin/<name>`` on
    POSIX, ``.venv\\Scripts\\<name>.exe`` on Windows — the ``.exe`` suffix is
    appended only when ``name`` does not already carry it). Only a runnable
    venv-provided binary is a safe rewrite target: anything else a manifest
    names bare (``node``, ``docker``) was a deliberate PATH dependency and must
    be left alone, and a non-executable venv file (a data artifact, a partial
    pip install) must not displace a command that would otherwise work.

    Callers must pass a bare NAME (no path separators, no drive qualifier) —
    the caller-side guard in ``resolve_stdio_command`` enforces that, keeping
    the join below inside the venv directory.
    """
    if platform_compat.IS_WINDOWS:
        exe_name = name if name.lower().endswith(".exe") else f"{name}.exe"
        candidate = root / ".venv" / "Scripts" / exe_name
    else:
        candidate = root / ".venv" / "bin" / name
    return str(candidate) if _runnable(candidate) else None
