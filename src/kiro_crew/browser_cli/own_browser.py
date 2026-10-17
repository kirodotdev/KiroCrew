"""Which browser the agent should drive: the user's own Chrome first, headless last.

The agent picks between two very different browsers and, without this module, has
nothing to pick with. ``playwright-cli open`` launches a fresh headless Chromium
that carries none of the user's logins. ``playwright-cli attach --extension=chrome``
borrows the Chrome the user is already signed in to. A request like "use my
Chrome" or "check my mail" only makes sense in the second, and the first answers
it with a sign-in page.

:func:`next_step` turns three host facts into one instruction, in this order:

1. Chrome is installed and a profile carries the Playwright extension: attach.
   Without a stored token the extension asks the human to approve, so the
   instruction says so and names where the token goes.
2. Chrome is installed but no profile carries the extension: only the user can
   add it, so ask for that and say headless is the stopgap.
3. No Chrome on this machine: headless ``open``.

A user who asks for a headless browser outright, and a host the gateway cannot
attach from (a remote gateway viewed from a laptop), override all three; the
agent-facing guidance says so in prose because no host fact can see either.

**Why the attach command carries ``PLAYWRIGHT_MCP_ISOLATED=false``.** Kiro Crew
honours an operator's ``PLAYWRIGHT_MCP_CONFIG`` untouched
(:func:`kiro_crew.browser_cli.launch.cli_env_overrides`), and a config that sets
``browser.isolated: true`` pre-empts attach: playwright-core checks ``isolated``
BEFORE ``extension`` when it creates the browser. The attach then prints
``attached to `chrome``` while it actually launched a throwaway in-memory browser
on ``about:blank``, and only a later ``detach`` admits ``was not attached``. The
CLI reads environment overrides after the config file, so the variable on the
attach command restores the real attach. It is a no-op without such a config:
extension mode already defaults ``isolated`` to false. It is scoped to the one
command because the same file may need ``isolated`` for every ``open``.

Every probe is a ``stat`` or a directory listing, never a read of profile
content, and a probe that cannot run answers "not found" rather than raising: the
caller is a tool reply, and a wrong guess there costs one extra step while an
exception costs the whole reply.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from kiro_crew.browser_cli import token

#: The Chrome Web Store id of the Playwright extension. Fixed by its publisher.
EXTENSION_ID = "mmlmfjhmonkocbjadbfplnigmagldckm"

EXTENSION_URL = f"https://chromewebstore.google.com/detail/playwright-extension/{EXTENSION_ID}"

#: The one attach spelling every agent-facing surface uses. See the module
#: docstring for the variable; ``test_own_browser`` pins the shipped guidance to it.
ATTACH_COMMAND = "PLAYWRIGHT_MCP_ISOLATED=false playwright-cli attach --extension=chrome"

#: The same attach for a Windows shell, where a leading ``NAME=value`` is not an
#: assignment. Spelled for PowerShell and scoped to the one command by removing
#: the variable afterwards, since a later ``open`` may need the file's value.
WINDOWS_ATTACH_COMMAND = (
    "$env:PLAYWRIGHT_MCP_ISOLATED='false'; playwright-cli attach --extension=chrome; "
    "Remove-Item Env:PLAYWRIGHT_MCP_ISOLATED"
)

#: Chrome keeps one directory per profile under its user data dir. A user with
#: hundreds of them is not a case worth listing past this.
_MAX_PROFILES = 64

#: Where system-wide installs are looked for. A module seam so a test can point it
#: at a temporary tree instead of reading whatever the test host has installed.
_ROOT = Path("/")


def _user_data_dir(home: Path, env: Mapping[str, str], system: str) -> Path | None:
    """Chrome's user data dir on this OS, or ``None`` when it has no known home."""
    if system == "darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    if system == "win32":
        local = env.get("LOCALAPPDATA", "").strip()
        return Path(local) / "Google" / "Chrome" / "User Data" if local else None
    if system.startswith("linux"):
        return home / ".config" / "google-chrome"
    return None


def chrome_installed(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
) -> bool:
    """Whether a branded Google Chrome is installed for this user."""
    home = home if home is not None else Path.home()
    env = env if env is not None else os.environ
    system = system or sys.platform
    candidates: list[Path] = []
    if system == "darwin":
        app = Path("Applications") / "Google Chrome.app"
        candidates = [_ROOT / app, home / app]
    elif system == "win32":
        tail = Path("Google") / "Chrome" / "Application" / "chrome.exe"
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = env.get(key, "").strip()
            if root:
                candidates.append(Path(root) / tail)
    elif system.startswith("linux"):
        if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
            return True
        candidates = [_ROOT / "opt" / "google" / "chrome" / "chrome"]
    try:
        return any(path.exists() for path in candidates)
    except OSError:
        return False


def extension_installed(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
) -> bool:
    """Whether any Chrome profile of this user carries the Playwright extension.

    Chrome unpacks an installed extension to ``<profile>/Extensions/<id>``, so the
    directory's presence is the signal. playwright-core picks that same profile on
    its own when it attaches, which is why no profile name is returned.
    """
    root = _user_data_dir(
        home if home is not None else Path.home(),
        env if env is not None else os.environ,
        system or sys.platform,
    )
    if root is None:
        return False
    try:
        profiles = [
            p for p in root.iterdir() if p.name == "Default" or p.name.startswith("Profile ")
        ]
        return any((p / "Extensions" / EXTENSION_ID).is_dir() for p in profiles[:_MAX_PROFILES])
    except OSError:
        return False


def attach_command(system: str | None = None) -> str:
    """The attach command in the form this host's shell understands."""
    return WINDOWS_ATTACH_COMMAND if (system or sys.platform) == "win32" else ATTACH_COMMAND


def next_step(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
) -> str:
    """One instruction telling the agent which browser to drive for the user's own logins.

    The token is judged from *env*, not from the stored file: the CLI reads it from
    the environment of the shell that runs the attach, and that shell inherited
    this process's environment. The file can also be masked from a sandboxed
    caller, where reading it would wrongly report "no token".
    """
    env = env if env is not None else os.environ
    attach = attach_command(system)
    if not chrome_installed(home, env, system):
        return (
            "No Google Chrome was found on this machine, so a page that needs the "
            "user's own login cannot use their browser: use the headless "
            "`playwright-cli open <url>`, which carries none of their logins."
        )
    if not extension_installed(home, env, system):
        return (
            "Google Chrome is installed but no profile has the Playwright extension. "
            "For anything that needs the user's own logins, ask them to add it "
            f"({EXTENSION_URL}), then attach with `{attach}`. Until then "
            "only the headless `playwright-cli open <url>` is available, without "
            "their logins."
        )
    token_hint = (
        ""
        if env.get(token.TOKEN_ENV, "").strip()
        else (
            " No extension token is stored, so tell the user to approve the "
            "connection in Chrome, or to paste the extension's token into "
            "Settings -> Browser so later attaches need no click."
        )
    )
    return (
        "Google Chrome with the Playwright extension is on this machine. If the user "
        "is at this machine and a page needs their own login, or they ask for their "
        f"Chrome, attach it with `{attach}`, then check `tab-list` shows more than "
        f"`about:blank`.{token_hint} Use the headless `playwright-cli open <url>` "
        "only when they ask for it, or when they reach this gateway from another "
        "machine, whose browser an attach here cannot reach."
    )
