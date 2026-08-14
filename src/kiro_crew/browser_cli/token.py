"""The optional Playwright extension token.

Attaching to the operator's own browser works without this: `playwright-cli`
appends the token to its relay URL only when the environment carries one
(`if (token) url.searchParams.set("token", token)`), and the extension answers a
tokenless handshake by asking the human to approve the connection in the browser.

The token therefore buys exactly one thing -- it removes that approval click --
and costs the two things every stored credential costs: somewhere to keep it, and
a way for a process that should not read it to read it anyway. It is opt-in for
that reason, and absent by default.

**Exposure, stated plainly.** The CLI reads the token from its environment, and the
agent runs the CLI as a shell command, so the value has to be on the environment
of a process the agent's shells descend from. Every command the agent runs can
therefore read it. That is deliberate and is the better of the two available
shapes: the alternative is for the agent to compose the value into a command line
itself, which puts the plaintext into tool-call transcripts and within reach of a
page that talks the agent into echoing it. Here the agent never handles the value.

The blast radius is bounded by what the token authorizes: connecting to the
extension's local relay in a browser already running on this machine. It is not a
cloud credential and grants nothing off-host.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: The variable `playwright-cli` reads. Its name is fixed by the CLI, not by us.
TOKEN_ENV = "PLAYWRIGHT_MCP_EXTENSION_TOKEN"

_TOKEN_FILE = "playwright-extension-token"


def token_path() -> Path:
    """Where the token is stored.

    Registered in :data:`kiro_crew.security._CREW_SECRET_LEAVES`, so the agent's
    own file tools cannot read it even though the environment it inherits can.
    That asymmetry is the point: a credential the agent never needs to open.
    """
    return config_dir() / _TOKEN_FILE


def read_token() -> str | None:
    """The stored token, or ``None`` when none is set.

    Whitespace is stripped because the value is pasted from a browser UI, where a
    trailing newline is the norm and would otherwise be sent as part of the token.
    """
    try:
        raw = token_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return raw or None


def has_token() -> bool:
    """Whether a token is set, without reading its value.

    Every status surface uses this. Nothing returns the token itself: a value that
    only ever has to reach a child process's environment has no reason to travel
    back out through an API response.
    """
    return read_token() is not None


def set_token(value: str) -> None:
    """Store *value*, or clear the token when it is blank.

    Written owner-only via ``restrict_to_owner`` and atomically, so a concurrent
    read never sees a half-written token, a later reader cannot pick up a truncated
    one, and the file is never world-readable — even on Windows where a numeric
    mode is silently ignored.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        clear_token()
        return
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, cleaned + "\n", restrict_to_owner=True, restrict_on_error="raise")


def clear_token() -> None:
    """Remove the token. Absent is the default state, so this cannot fail loudly."""
    try:
        token_path().unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.debug("could not remove the extension token", exc_info=True)


def cli_env_overrides() -> dict[str, str]:
    """Environment additions carrying the token, empty when none is set.

    Merged into the gateway's own environment at startup and again whenever the
    token changes, because a child process reads the environment it was given: a
    token written after a shell started does not reach that shell.
    """
    token = read_token()
    return {TOKEN_ENV: token} if token else {}
