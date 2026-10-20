"""The host programs the Notes backend finds and runs: gh, the file manager, the folder chooser.

``_find_gh`` resolves the GitHub CLI from fixed install roots, never ``PATH``;
``_trusted_env`` is the environment every spawn passes, with ``PATH`` pinned the same way;
``_reveal_folder_sync`` opens a folder in the OS file manager and ``_pick_folder_sync``
shows the macOS folder chooser. The ``gh auth token`` mint stays in ``server``, the facade
this module is reached through, beside the token memo it fills.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.md_notebook import git_ops
from kiro_crew.apps.builtins.md_notebook.server_http import ApiError

# Native folder picker.
PICK_FOLDER_TIMEOUT_SEC = 120
# Revealing a folder in the OS file manager. Absolute paths, never resolved from
# PATH: the front of PATH is agent-writable, so a `open`/`xdg-open` shim planted
# there would be what this launches.
REVEAL_TIMEOUT_SEC = 20
REVEAL_BINARIES = {
    "darwin": "/usr/bin/open",
    "linux": "/usr/bin/xdg-open",
}
PICK_FOLDER_SCRIPT = "\n".join(
    [
        "activate",
        "try",
        '  set chosen to choose folder with prompt "Choose your notes folder"',
        "  return POSIX path of chosen",
        "on error number -128",
        '  return ""',
        "end try",
    ]
)


def _windows_gh_candidates() -> list[str]:
    """Fixed GitHub-CLI install roots on Windows. PATH is still NOT consulted.

    Only the machine-wide install roots: ``%ProgramFiles%`` and
    ``%ProgramW6432%`` (where a 64-bit install lands when the host interpreter
    is 32-bit and ``%ProgramFiles%`` resolves to the x86 tree). Both need an
    admin-elevated installer to write to, matching the no-PATH-hijack property
    ``_find_gh``'s docstring relies on.

    Deliberately NOT ``%LOCALAPPDATA%\\Programs`` (winget's per-user install
    root): unlike Program Files, it sits inside the user's own profile and is
    writable by anything running as that user -- including this agent. Trusting
    a ``gh.exe`` planted there would let it run unsandboxed the next time a
    vault operation mints a gh-derived token, with the backend's PAT and proxy
    secret in scope. A user who installed gh per-user only (no admin rights)
    still authenticates via a stored PAT; this is a narrower gh-derived-auth
    surface on Windows, not a hard requirement.
    """
    roots: list[str] = []
    for var in ("ProgramFiles", "ProgramW6432"):
        value = os.environ.get(var)
        if value and value not in roots:
            roots.append(value)
    return [os.path.join(root, "GitHub CLI", "gh.exe") for root in roots]


def _find_gh() -> Optional[str]:
    """Absolute path to a trusted ``gh``, or None. PATH is NOT consulted — a
    workspace-writable entry could shadow ``gh`` with a planted binary that runs
    unsandboxed when a token is minted, so we only trust system/admin locations
    and fail closed (no gh -> no gh-derived token, which the caller tolerates)."""
    override = os.environ.get("MD_NOTEBOOK_GH_BIN")
    candidates = (
        [override]
        if override
        else [
            # System / package-manager locations only. `~/.local/bin` is
            # deliberately NOT here: it is a spot the agent itself can write, so
            # trusting a `gh` there would let a planted binary run with the
            # backend's PAT + proxy secret in scope. gh is optional — no trusted
            # gh simply means no gh-derived token.
            "/opt/homebrew/bin/gh",
            "/usr/local/bin/gh",
            "/usr/bin/gh",
        ]
    )
    if not override and platform_compat.IS_WINDOWS:
        # None of the entries above can ever match on Windows, so gh-derived auth
        # was unavailable there however the user had logged in. Same rule, same
        # reason: fixed install roots only, never PATH.
        candidates = _windows_gh_candidates()
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _trusted_env() -> dict[str, str]:
    """The current environment with PATH pinned to trusted system directories.

    Every subprocess this module spawns dispatches to child helpers that are
    resolved through PATH, and the gateway's inherited PATH can carry an
    agent-writable entry (`~/.local/bin` is the usual one). Pinning it means a
    planted helper cannot ride along on an action the user initiated. Same pin, and
    the same reason, as `run_git` — see `git_ops.TRUSTED_PATH`.

    Only PATH is replaced: the rest of the environment is what lets these tools
    reach the running desktop session (DISPLAY, DBUS_SESSION_BUS_ADDRESS, XDG_*).
    Windows keeps the inherited PATH, where helpers live beside their install.
    """
    env = dict(os.environ)
    if os.name == "posix":
        env["PATH"] = git_ops.TRUSTED_PATH
    return env


def _reveal_binary() -> Optional[str]:
    """The absolute file-manager binary for this platform, or None.

    Existence-checked rather than assumed: a minimal Linux container has no
    `xdg-open`, and a missing binary must read as "not supported here" rather
    than a 500.
    """
    if sys.platform.startswith("win"):
        return "explorer"  # handled via os.startfile, never spawned
    binary = REVEAL_BINARIES.get("darwin" if sys.platform == "darwin" else "linux")
    return binary if binary and os.path.isfile(binary) else None


def _reveal_folder_sync(path: str) -> None:
    """Open a directory in the OS file manager.

    The path is computed by the caller from the vault descriptor — never taken
    from the request — and the argv is a fixed binary plus that one path, with no
    shell. Blocking, so callers offload it.
    """
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 — Windows-only API
        return
    binary = _reveal_binary()
    if not binary:
        raise ApiError(
            "opening a folder is not supported on this system",
            501,
            code="folder_open_unsupported",
        )
    # `xdg-open` is a SHELL SCRIPT that dispatches to whichever helper it finds on
    # PATH — `gio`, `gvfs-open`, `exo-open`, `kde-open`, `dbus-send` — so a planted
    # `gio` in an agent-writable PATH entry would execute the moment the user clicks
    # the delete dialog's `.trash` link, a click they have every reason to trust.
    proc = subprocess.run(
        [binary, path],
        capture_output=True,
        text=True,  # subprocess-encoding: locale -- the file manager writes in the locale
        timeout=REVEAL_TIMEOUT_SEC,
        check=False,
        env=_trusted_env(),
    )
    if proc.returncode != 0:
        raise ApiError("could not open the folder", 500, code="folder_open_failed")


def pick_folder_supported() -> bool:
    """macOS only, and suppressible so tests never open a GUI dialog."""
    return sys.platform == "darwin" and not os.environ.get("MD_NOTEBOOK_NO_PICKER")


def _pick_folder_sync() -> Optional[str]:
    """Show the OS folder chooser. Returns the path, or None if cancelled."""
    proc = subprocess.run(
        ["/usr/bin/osascript", "-e", PICK_FOLDER_SCRIPT],
        capture_output=True,
        text=True,  # subprocess-encoding: locale -- osascript writes the path in the locale
        timeout=PICK_FOLDER_TIMEOUT_SEC,
        check=False,
        env=_trusted_env(),
    )
    if proc.returncode != 0:
        raise ApiError("could not open the folder chooser", 500, code="folder_chooser_failed")
    # osascript returns a trailing slash; the attach flow wants none.
    path = proc.stdout.strip()
    return path.rstrip("/") if path else None
