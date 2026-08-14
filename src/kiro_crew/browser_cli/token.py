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

#: Shell keywords a pasted assignment may carry. The extension's setup text is a
#: shell line, and a user copying it takes the whole line.
_ASSIGNMENT_PREFIXES = ("export ", "set ", "setx ")


def normalize_paste(raw: str) -> str:
    """The token value out of whatever the user pasted.

    The extension presents the token as a shell assignment, so both of these are
    what a user reasonably pastes into one field, and both mean the same token::

        PLAYWRIGHT_MCP_EXTENSION_TOKEN=<value>
        <value>

    Accepting only the bare form stores the variable name as part of the
    credential, and the failure is silent and far away: the token is written, the
    panel says "stored", and the extension simply keeps asking for approval as
    though none were set.

    The prefix is stripped only when the text left of the FIRST ``=`` is exactly
    :data:`TOKEN_ENV`. That condition is the safety property, not a nicety --
    these tokens are base64url and can legitimately contain ``=`` padding, so a
    rule like "take everything after the last ``=``" would corrupt a bare token
    that this function must pass through untouched.
    """
    text = (raw or "").strip()
    lowered = text.lower()
    for prefix in _ASSIGNMENT_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    name, sep, value = text.partition("=")
    # Case-insensitive on the NAME only: it is a label the user transcribed, while
    # the value is a credential and stays byte-exact.
    if sep and name.strip().upper() == TOKEN_ENV:
        text = value.strip()
    # Quotes come from the shell form (`NAME="value"`), where they are syntax
    # rather than token bytes. Stripped as a matched pair only, so a token that
    # legitimately begins or ends with a quote is not silently shortened.
    for quote in ('"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1]
            break
    return text.strip()


def token_path() -> Path:
    """Where the token is stored.

    Registered in :data:`kiro_crew.security._CREW_SECRET_LEAVES`, so the agent's
    own file tools cannot read it even though the environment it inherits can.
    That asymmetry is the point: a credential the agent never needs to open.
    """
    return config_dir() / _TOKEN_FILE


def read_token() -> str | None:
    """The stored token, or ``None`` when none is set.

    Normalized on the way out as well as in, via :func:`normalize_paste`: a stored
    value can hold the whole pasted assignment rather than the token alone, and the
    resulting failure is invisible (the panel reports "stored" while the extension
    keeps prompting). Repairing it on read costs the user nothing, where noticing
    and re-pasting costs them the diagnosis first. The call is idempotent, so a
    value that is already clean passes through byte-exact.
    """
    try:
        raw = normalize_paste(token_path().read_text(encoding="utf-8"))
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

    The input is whatever the user pasted, so it goes through
    :func:`normalize_paste` first: a copied shell assignment stores the same token
    as the bare value rather than the variable name plus the token.

    Written owner-only via ``restrict_to_owner`` and atomically, so a concurrent
    read never sees a half-written token, a later reader cannot pick up a truncated
    one, and the file is never world-readable — even on Windows where a numeric
    mode is silently ignored.
    """
    cleaned = normalize_paste(value)
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
