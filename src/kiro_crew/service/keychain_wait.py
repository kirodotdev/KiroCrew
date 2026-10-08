"""Bounded wait for the macOS login Keychain before the gateway starts.

launchd can start the gateway LaunchAgent before anyone has logged in, while the
login Keychain is still locked. The Kiro CLI backend keeps its sign-in token in
that Keychain, so a gateway started then cannot reach its backend. Instead of
starting into that state, the gateway waits a bounded time for the Keychain to
unlock (a normal login after boot does that), and if it never does, exits
non-zero with a message that names the locked Keychain and how to unlock it.
The non-zero exit lets launchd's ``KeepAlive`` relaunch it later.

Only the Keychain's lock STATE is read (``security show-keychain-info``); no
item in it is ever read, so nothing secret can reach a log.

``KIROCREW_KEYCHAIN_WAIT_SECS`` sets the bound (default 300). ``0`` fails at
once when the Keychain is locked.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

WAIT_ENV = "KIROCREW_KEYCHAIN_WAIT_SECS"
DEFAULT_WAIT_SECS = 300.0
POLL_SECS = 5.0
_PROBE_TIMEOUT_SECS = 10.0

# The text ``security`` prints when a Keychain is locked and no GUI session can
# prompt for its password (errSecInteractionNotAllowed).
_LOCKED_MARKER = "interaction is not allowed"


def login_keychain_path() -> Path:
    return Path.home() / "Library" / "Keychains" / "login.keychain-db"


def uses_kiro_backend(agent_config: object) -> bool:
    """True when the main or the member backend is kiro-cli.

    Only kiro-cli keeps its sign-in in the login Keychain; a gateway on any
    other backend has no reason to wait for it.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

    return any(
        getattr(agent_config, field, ACP_BACKEND_KIRO) == ACP_BACKEND_KIRO
        for field in ("acp_backend", "member_acp_backend")
    )


def wait_budget_secs(environ: dict[str, str] | None = None) -> float:
    """The configured bound in seconds; a bad or negative value gets the default."""
    raw = (environ if environ is not None else os.environ).get(WAIT_ENV, "").strip()
    if not raw:
        return DEFAULT_WAIT_SECS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WAIT_SECS
    if value != value or value < 0:  # NaN or negative
        return DEFAULT_WAIT_SECS
    return value


def keychain_is_locked(path: Path) -> bool:
    """True only when ``security`` positively reports *path* as locked.

    Anything else (no ``security`` binary, no such Keychain, a probe that hangs
    or fails for another reason) is not evidence of a lock, so it returns False
    and the gateway starts as before.
    """
    try:
        result = subprocess.run(
            ["security", "show-keychain-info", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode == 0:
        return False
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return _LOCKED_MARKER in output


def locked_message(path: Path, waited_secs: float) -> str:
    return (
        f"The macOS login Keychain ({path}) is still locked after waiting "
        f"{waited_secs:.0f}s, so the gateway cannot read the Kiro CLI sign-in. "
        "Log in to this Mac, or unlock it from a terminal with "
        f"`security unlock-keychain {path}`, then start the gateway again "
        f"(launchd retries on its own). Set {WAIT_ENV} to change how long the "
        "gateway waits."
    )


def wait_for_login_keychain(
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    is_locked: Callable[[Path], bool] = keychain_is_locked,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    stderr: TextIO | None = None,
    keychain: Path | None = None,
) -> str | None:
    """Wait (bounded) for the login Keychain to unlock.

    Returns None when the gateway may start: not macOS, no login Keychain, or
    the Keychain is (or becomes) unlocked. Returns the failure message when it
    is still locked once the bound is spent; the caller exits non-zero with it.
    """
    if (platform if platform is not None else sys.platform) != "darwin":
        return None
    path = keychain if keychain is not None else login_keychain_path()
    if not path.exists():
        return None
    if not is_locked(path):
        return None
    budget = wait_budget_secs(environ)
    out = stderr if stderr is not None else sys.stderr
    print(
        f"⏳ The macOS login Keychain ({path}) is locked; waiting up to "
        f"{budget:.0f}s for it to unlock before starting the gateway.",
        file=out,
        flush=True,
    )
    start = monotonic()
    while True:
        elapsed = monotonic() - start
        if elapsed >= budget:
            return locked_message(path, elapsed)
        sleep(min(POLL_SECS, budget - elapsed))
        if not is_locked(path):
            return None
