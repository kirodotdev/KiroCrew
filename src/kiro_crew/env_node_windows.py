"""Windows default roots of the Node version managers :mod:`kiro_crew.env` scans.

On Windows fnm, nvm-windows and Volta keep Node under ``%APPDATA%``,
``%LOCALAPPDATA%`` or ``%ProgramFiles%``, and fnm puts ``node.exe`` directly in
``installation\\`` (no ``bin``). :mod:`kiro_crew.env` scans these only on
Windows, AFTER its POSIX and macOS lists. Each candidate is still probed (an
executable ``node``, or ``is_dir`` for MCP-binary discovery), so an absent root
drops out. Volta's dirs also go on ``env.augmented_path``: a shim dir appears in
BOTH lists.
"""

from __future__ import annotations

import os

from kiro_crew import platform_compat

# Per-version roots, globbed like ``env._NODE_MANAGER_GLOBS``.
MANAGER_GLOBS = (
    "{appdata}/fnm/node-versions/*/installation",
    "{appdata}/nvm/*",
)
# Shim / single-dir roots, like ``env._NODE_MANAGER_DIRS``.
MANAGER_DIRS = (
    "{localappdata}/Volta/bin",
    "{programfiles}/Volta",
)


def roots(home: str, pinned_home: str | None) -> dict[str, str]:
    """The roots :data:`MANAGER_GLOBS` and :data:`MANAGER_DIRS` format against.

    ``APPDATA`` / ``LOCALAPPDATA`` name the account running this process, so a
    *pinned_home* for another account uses that home's ``AppData`` instead
    (as ``env._windows_toolbox_bin_dir`` does); a relative value is ignored.
    ``ProgramFiles`` is machine-wide; unset or relative stays empty, so its
    entry is skipped rather than formatted into a rootless path.
    """

    def _base(var: str, *fallback: str) -> str:
        value = os.environ.get(var, "") if pinned_home is None else ""
        return value if os.path.isabs(value) else os.path.join(home, *fallback)

    program_files = os.environ.get("ProgramFiles", "")
    return {
        "appdata": _base("APPDATA", "AppData", "Roaming"),
        "localappdata": _base("LOCALAPPDATA", "AppData", "Local"),
        "programfiles": program_files if os.path.isabs(program_files) else "",
    }


def manager_globs(home: str, pinned_home: str | None = None) -> tuple[str, ...]:
    """:data:`MANAGER_GLOBS` formatted for this host; empty off Windows.

    Braces in the formatted roots are escaped, because
    ``env._manager_version_bin_dirs`` calls ``str.format`` on each pattern again.
    """
    if not platform_compat.IS_WINDOWS:
        return ()
    values = roots(home, pinned_home)
    return tuple(g.format(**values).replace("{", "{{").replace("}", "}}") for g in MANAGER_GLOBS)


def manager_dirs(home: str, pinned_home: str | None = None) -> list[str]:
    """:data:`MANAGER_DIRS` formatted and normalized for this host; empty off Windows.

    Each entry is one absolute dir. A root carrying ``os.pathsep`` or NUL would
    smuggle extra entries onto every PATH built from it, so it is dropped (as
    ``env._validated_bin_dir`` does).
    """
    if not platform_compat.IS_WINDOWS:
        return []
    values = roots(home, pinned_home)
    out: list[str] = []
    for template in MANAGER_DIRS:
        if any(not v and f"{{{k}}}" in template for k, v in values.items()):
            continue
        entry = os.path.normpath(template.format(**values))
        if os.path.isabs(entry) and os.pathsep not in entry and "\0" not in entry:
            out.append(entry)
    return out
