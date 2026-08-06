"""Which interpreter an App Kit app's own Python runs under.

One policy, because an app has one set of dependencies but several spawn sites:
its backend process and any stdio MCP server it declares. Resolving this at each
site is how the two drifted apart — the backend refusing a bare ``python3`` while
the MCP registration wrote one through.

Kept in its own module rather than beside either caller: ``apps.backend`` and
``apps.bridges`` already defer-import each other to break a cycle, so a helper
either of them owned would have to be imported lazily by the other.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kiro_crew import platform_compat

#: Interpreter names that are resolved through ``PATH`` at spawn time rather than
#: naming a file. ``py`` is the Windows launcher and belongs here for the same
#: reason as the other two. Deliberately a closed set: a versioned name
#: (``python3.13``) and an absolute path are the author's explicit choice.
BARE_PYTHON_COMMANDS = frozenset({"python", "python3", "py"})


def is_bare_python(command: object) -> bool:
    """Whether *command* names an interpreter to be looked up rather than a file.

    Case- and whitespace-insensitive: a manifest is hand-written JSON, and
    ``"Python3"`` spawns the same launcher as ``"python3"`` on a case-insensitive
    filesystem while failing an exact match here.
    """
    return isinstance(command, str) and command.strip().lower() in BARE_PYTHON_COMMANDS


def app_venv_python(app_root: Path) -> Path | None:
    """The app venv's interpreter, or ``None`` when the app has no usable venv.

    A venv exposes its interpreter at ``bin/python3`` on POSIX and
    ``Scripts/python.exe`` on Windows, so a single hardcoded layout resolves
    nothing on the other platform and the caller falls through to the gateway's
    interpreter — losing the app's dependencies without saying so.
    """
    subpath = ("Scripts", "python.exe") if platform_compat.IS_WINDOWS else ("bin", "python3")
    candidate = app_root.joinpath(".venv", *subpath)
    return candidate if candidate.is_file() else None


def resolve_app_python(app_root: Path) -> str:
    """Interpreter to run *app_root*'s own Python with.

    The app's venv first, so its code runs against the dependencies installed for
    it; the gateway's own interpreter otherwise. Never a bare ``python3``, which
    is resolved through ``PATH`` at spawn time and so is not guaranteed to name an
    interpreter at all (a host shipping only a versioned one raises
    ``FileNotFoundError`` from ``execvp``) nor to name the right one (a system
    interpreter older than the venv's dies on the app's imports). Both failures
    are silent where they matter: the process never starts, and whatever it
    provided is simply absent.
    """
    venv_python = app_venv_python(app_root)
    return str(venv_python) if venv_python is not None else sys.executable
