"""Imported browser cookies, stored as a Playwright ``storageState`` and applied
to the agent's ``playwright-cli`` sessions.

The dashboard user exports cookies from the browser where they are already
logged in and imports them here; the gateway normalises them to Playwright's
cookie shape and writes one owner-only ``storageState`` file under the data
home. That file is **bind-masked out of every agent sandbox**
(``sandbox._CREW_HIDDEN_LEAVES``): the agent must never be able to read a
session cookie off disk, and a spawned shell's ``open()`` never routes through
the tool gate, so only the OS mask holds. The cookies still have to reach the
agent's browser, and a hidden file cannot be named in the config the AGENT's
``playwright-cli`` reads -- the daemon an agent command starts runs inside the
same sandbox and would fail on ENOENT. So two configs exist, and the state
travels through the GATEWAY:

* :func:`kiro_crew.browser_cli.launch.desired_config` (the file the agent's
  ``PLAYWRIGHT_MCP_CONFIG`` names) stays engine-only and never references the
  hidden path.
* :func:`gateway_config_path` holds the same document PLUS
  ``browser.contextOptions.storageState`` while the state file exists. Only
  gateway-side spawns use it.
* :func:`prewarm_session` starts the daemon for an agent's ``kc-*`` session
  from the GATEWAY process, with that config and the same socket/registry
  directories the agent process is about to receive, so the agent's later
  commands connect to a daemon that already carries the cookies and that runs
  outside its sandbox. The agent sees the session, never the file.

**Three import shapes are accepted**, because that is what the browsers and
their export extensions actually produce:

1. A Playwright ``storageState`` object (``{"cookies": [...], "origins": [...]}``)
   -- what ``playwright-cli state-save`` writes, so a round-trip is lossless.
2. A bare JSON array of cookie objects -- the Cookie-Editor / EditThisCookie /
   "Get cookies.txt" extension export. Field names differ from Playwright's
   (``expirationDate`` for the expiry, a wider ``sameSite`` vocabulary), so they
   are normalised.
3. Netscape ``cookies.txt`` -- the tab-separated format ``curl``/``wget`` and
   the "Get cookies.txt" extension emit, including the ``#HttpOnly_`` domain
   prefix convention.

Every cookie is normalised to the Playwright shape: ``name``, ``value``,
``domain``, ``path``, ``expires`` (a float, or ``-1`` for a session cookie),
``httpOnly``, ``secure``, ``sameSite`` in ``{"Strict", "Lax", "None"}``. Already
expired cookies are dropped. Every failure raises :class:`CookieImportError`
with a message meant to be shown to the user.

**Values never leave.** :func:`storage_state_summary` reports counts, domains
and the earliest expiry, and nothing here ever returns or logs a cookie value.
The agent is told (see ``docs/browser-control.md``) that imported cookies apply
to new sessions on their own and that it must never read the storage-state file
-- and the sandbox mask is what makes that last sentence a fact rather than an
instruction.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser_cli.install import cli_command, cli_env
from kiro_crew.browser_cli.launch import (
    _LIFECYCLE_DIR,
    _SESSION_PREFIX,
    CONFIG_ENV,
    DAEMON_DIR_ENV,
    SESSION_ENV,
    SOCKETS_ENV,
    _session_leaf,
    daemon_dir,
    desired_config,
    launch_config_path,
    socket_dir,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

logger = logging.getLogger(__name__)

#: The storageState file the gateway-launched daemon loads. Fixed name under the
#: data home so an isolated ``KIROCREW_HOME`` (a pod, a test) stays isolated here
#: too. Listed in ``sandbox._CREW_HIDDEN_LEAVES`` and ``security._CREW_SECRET_LEAVES``
#: under exactly this name: renaming it here without renaming it there would
#: silently put the cookie values back in the agent's reach.
STORAGE_STATE_FILE = "browser-storage-state.json"

#: The launch config GATEWAY-side spawns read: the agent's config plus the
#: ``storageState`` key. A separate file from the agent's so the agent's copy can
#: never carry a path its sandbox cannot open (see the module docstring).
_GATEWAY_CONFIG_FILE = "playwright-cli-gateway-config.json"

#: Reject an import whose raw text exceeds this, before parsing: the same 2 MiB
#: ceiling the HTTP handler enforces, restated here so a non-HTTP caller
#: (a test, a future CLI path) gets the same bound.
MAX_IMPORT_BYTES = 2 * 1024 * 1024

#: Reject an import carrying more than this many cookies. A logged-in browser
#: profile has a few hundred; five thousand is far past any legitimate export
#: and bounds the work of writing and re-loading the state.
MAX_COOKIES = 5000

#: How long a ``state-load`` / ``cookie-clear`` on one live session is allowed to take.
_HOT_LOAD_TIMEOUT_S = 15.0

#: How long a gateway-side pre-warm (``open about:blank``, which starts the
#: daemon and launches Chromium) may run before it is abandoned. Generous
#: because a cold Chromium start on a loaded host takes tens of seconds, and
#: nothing waits on it: the agent spawn has already returned.
_PREWARM_TIMEOUT_S = 90.0

#: Playwright's three accepted ``sameSite`` values.
_SAME_SITE_VALUES = frozenset({"Strict", "Lax", "None"})

#: Maps the wider extension/browser ``sameSite`` vocabulary onto Playwright's
#: three. Anything absent or unrecognised falls back to ``Lax`` (see
#: :func:`_normalize_same_site`), which is the browser default for an unspecified
#: attribute.
_SAME_SITE_ALIASES = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


class CookieImportError(ValueError):
    """A cookie import that cannot be accepted, with a user-readable message."""


def storage_state_path() -> Path:
    """Where the imported storageState lives, under the data home."""
    return config_dir() / STORAGE_STATE_FILE


def _normalize_same_site(raw: Any) -> str:
    """Normalise any browser/extension ``sameSite`` spelling to Playwright's set.

    Playwright accepts only ``Strict``/``Lax``/``None``; the extensions emit
    ``strict``/``lax``/``none``/``no_restriction``/``unspecified`` as well as the
    capitalised forms. An absent or unrecognised value becomes ``Lax`` -- the
    browser default for a cookie with no ``SameSite`` attribute.
    """
    if isinstance(raw, str):
        if raw in _SAME_SITE_VALUES:
            return raw
        mapped = _SAME_SITE_ALIASES.get(raw.strip().lower())
        if mapped is not None:
            return mapped
    return "Lax"


def _reject_json_constant(name: str) -> Any:
    """Refuse the non-standard JSON constants ``NaN``/``Infinity``/``-Infinity``.

    ``json.loads`` accepts them by default; a cookie export never legitimately
    carries one, and letting one through would persist a value the daemon's
    strict parser cannot read.
    """
    raise CookieImportError(f"cookie import contains the invalid JSON constant {name}")


def _normalize_expires(raw: Any) -> float:
    """Normalise an expiry to a float unix timestamp, or ``-1`` for a session cookie.

    Playwright uses ``-1`` for "expires at end of session". A missing, null,
    non-numeric, or non-positive expiry is treated as a session cookie: an
    export that omits the field is common, and Netscape ``cookies.txt`` writes a
    literal ``0`` for a session cookie. A positive value is kept verbatim (an
    already-past one is dropped by the caller against the current time).
    """
    if isinstance(raw, bool) or raw is None:
        return -1.0
    parsed: float | None = None
    if isinstance(raw, (int, float)):
        parsed = float(raw)
    elif isinstance(raw, str):
        try:
            parsed = float(raw)
        except ValueError:
            parsed = None
    if parsed is None or not math.isfinite(parsed) or parsed <= 0:
        # NaN/Infinity would be written verbatim and make the persisted state
        # unreadable to the daemon; they carry no expiry, so: session cookie.
        return -1.0
    return parsed


def _normalize_cookie(raw: Any) -> dict[str, Any]:
    """Normalise one extension/browser cookie object to the Playwright shape.

    Raises :class:`CookieImportError` when a required field is missing: a cookie
    with no ``name`` or no ``domain`` cannot be applied to a session and silently
    dropping it would hide a broken export. Expiry-based dropping is the caller's
    job (:func:`parse_cookie_import`), which knows the current time.
    """
    if not isinstance(raw, dict):
        raise CookieImportError("each cookie must be a JSON object")
    name = raw.get("name")
    domain = raw.get("domain")
    if not isinstance(name, str) or not name:
        raise CookieImportError("a cookie is missing its name")
    if not isinstance(domain, str) or not domain:
        raise CookieImportError(f"cookie {name!r} is missing its domain")
    value = raw.get("value")
    # ``expirationDate`` is the extension spelling; ``expires`` is Playwright's.
    expires = _normalize_expires(
        raw["expirationDate"] if "expirationDate" in raw else raw.get("expires")
    )
    path = raw.get("path")
    return {
        "name": name,
        "value": value if isinstance(value, str) else "",
        "domain": domain,
        "path": path if isinstance(path, str) and path else "/",
        "expires": expires,
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": bool(raw.get("secure", False)),
        "sameSite": _normalize_same_site(raw.get("sameSite")),
    }


def _parse_netscape(text: str) -> list[dict[str, Any]]:
    """Parse a Netscape ``cookies.txt`` body into raw cookie dicts.

    Tab-separated, seven columns: ``domain  includeSubdomains  path  secure
    expires  name  value``. Lines beginning ``#`` are comments, except the
    ``#HttpOnly_`` prefix convention which marks the cookie httpOnly and carries
    the real domain after the prefix. A line with too few columns is skipped
    rather than failing the whole import.
    """
    cookies: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        http_only = False
        if stripped.startswith("#HttpOnly_"):
            http_only = True
            stripped = stripped[len("#HttpOnly_") :]
        elif stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) < 7:
            continue
        domain, _sub, path, secure, expires, name, value = parts[:7]
        cookies.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure.strip().upper() == "TRUE",
                "expires": expires,
                "name": name,
                "value": value,
                "httpOnly": http_only,
            }
        )
    return cookies


def parse_cookie_import(text: str) -> list[dict[str, Any]]:
    """Parse and normalise an import in any accepted shape into Playwright cookies.

    Accepts a Playwright ``storageState`` object, a bare JSON array of cookie
    objects, or a Netscape ``cookies.txt`` body. Normalises every cookie to the
    Playwright shape, drops already-expired cookies, and enforces the size and
    count limits. Raises :class:`CookieImportError` with a user-readable message
    on any rejection.
    """
    if not isinstance(text, str):
        raise CookieImportError("cookie import must be text")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_IMPORT_BYTES:
        raise CookieImportError("cookie import is too large (limit 2 MiB)")
    if not text.strip():
        raise CookieImportError("cookie import is empty")

    raw_cookies: list[Any]
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            parsed = json.loads(text, parse_constant=_reject_json_constant)
        except RecursionError as exc:
            # json.loads recurses per nesting level; a pathological document
            # must be a 400, not a 500 from the handler.
            raise CookieImportError("cookie import is nested too deeply") from exc
        except ValueError as exc:
            raise CookieImportError(f"cookie import is not valid JSON: {exc}") from exc
        if isinstance(parsed, dict):
            # A Playwright storageState object.
            candidate = parsed.get("cookies")
            if not isinstance(candidate, list):
                raise CookieImportError('storageState JSON must carry a "cookies" array')
            raw_cookies = candidate
        elif isinstance(parsed, list):
            raw_cookies = parsed
        else:
            raise CookieImportError(
                "cookie import JSON must be a storageState object or an array of cookies"
            )
    else:
        raw_cookies = _parse_netscape(text)
        if not raw_cookies:
            raise CookieImportError(
                "cookie import is neither JSON nor a recognisable cookies.txt file"
            )

    if len(raw_cookies) > MAX_COOKIES:
        raise CookieImportError(f"too many cookies (limit {MAX_COOKIES})")

    now = time.time()
    cookies: list[dict[str, Any]] = []
    for raw in raw_cookies:
        cookie = _normalize_cookie(raw)
        # Drop an already-expired cookie: a positive expiry in the past can
        # never authenticate a session, and loading it only clutters the state.
        if cookie["expires"] > 0 and cookie["expires"] < now:
            continue
        cookies.append(cookie)

    if not cookies:
        raise CookieImportError("no unexpired cookies to import")
    return cookies


def save_storage_state(cookies: list[dict[str, Any]]) -> Path:
    """Write *cookies* as a Playwright storageState file, owner-only, and return its path.

    Atomic (temp file + rename) and locked down to the owner: the file carries
    live session cookies, so it is written ``0o600`` on POSIX and with an
    owner-only DACL on Windows via :func:`atomic_write`'s ``restrict_to_owner``
    (a raw ``mode=0o600`` is a no-op on Windows).
    """
    path = storage_state_path()
    payload = json.dumps({"cookies": cookies, "origins": []}, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, payload, mode=0o600, restrict_to_owner=True)
    return path


def clear_storage_state() -> bool:
    """Delete the storageState file. Returns True if a file was removed."""
    path = storage_state_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def gateway_config_path() -> Path:
    """Where the gateway-side launch config lives, under the data home."""
    return config_dir() / _GATEWAY_CONFIG_FILE


def gateway_config() -> dict[str, object]:
    """The agent's launch config plus ``storageState`` while the state file exists.

    Exactly :func:`kiro_crew.browser_cli.launch.desired_config` when no cookies
    are imported, so a gateway-side spawn behaves like an agent's in every other
    respect; with the file present it adds ``browser.contextOptions.storageState``
    naming it. Only processes the GATEWAY launches read this document -- the
    path it names is masked from every agent sandbox.
    """
    config = desired_config()
    state_path = storage_state_path()
    if state_path.is_file():
        browser = config["browser"]
        assert isinstance(browser, dict)
        browser["contextOptions"] = {"storageState": str(state_path)}
    return config


def write_gateway_config() -> Path | None:
    """Write the gateway-side config, returning its path (``None`` when it could not be).

    Rewritten whenever it does not already match :func:`gateway_config`, so the
    ``storageState`` key appears after an import and disappears after a clear.
    Best effort: a config that cannot be written means the next pre-warm is
    skipped, never that an agent spawn fails.
    """
    path = gateway_config_path()
    payload = json.dumps(gateway_config(), indent=2) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == payload:
            return path
    except (OSError, UnicodeDecodeError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, payload)
    except OSError:
        logger.warning("could not write the gateway browser launch config at %s", path)
        return None
    return path


def storage_state_summary() -> dict[str, Any] | None:
    """Summarise the stored cookies without ever returning a value.

    ``None`` when no storageState file is present or it cannot be read. The
    summary carries the cookie count, the distinct domains (leading dot
    stripped, sorted), the earliest positive expiry (or ``None`` if every cookie
    is a session cookie), and the file's modification time as ``imported_at``.
    NEVER a cookie value.
    """
    path = storage_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return None

    domains: set[str] = set()
    earliest_expiry: float | None = None
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain")
        if isinstance(domain, str) and domain:
            domains.add(domain[1:] if domain.startswith(".") else domain)
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            if earliest_expiry is None or expires < earliest_expiry:
                earliest_expiry = float(expires)

    try:
        imported_at = path.stat().st_mtime
    except OSError:
        imported_at = time.time()

    return {
        "cookie_count": len(cookies),
        "domains": sorted(domains),
        "earliest_expiry": earliest_expiry,
        "imported_at": imported_at,
    }


# ── Live sessions: the gateway reaching an agent's daemon ────────────────────


def _session_env(session_name: str) -> dict[str, str]:
    """The lifecycle variables an agent process was handed for *session_name*.

    Mirrors :func:`kiro_crew.browser_cli.launch.browser_socket_env` for the
    DEFAULT lifecycle root: the socket and registry directories are derived from
    the generated name, so a gateway-side CLI client pointed at them connects to
    the same daemon the agent's commands reach. An operator-configured root
    (``SOCKETS_ENV`` set to a foreign path before the gateway started) is not
    re-derived here; sessions under it are simply not enumerated.
    """
    return {
        SESSION_ENV: session_name,
        SOCKETS_ENV: str(socket_dir(session_name)),
        DAEMON_DIR_ENV: str(daemon_dir(session_name)),
    }


def _live_session_names() -> list[str]:
    """Best-effort list of the live ``kc-*`` sessions visible to ``playwright-cli list``.

    Runs ``list`` under the gateway's own environment and returns the names
    beginning with the Kiro-Crew session prefix. Never raises: an empty list
    means "could not enumerate". Only sessions registered in the DEFAULT
    registry show up here -- one an agent started on a host where the lifecycle
    hooks are unsupported -- which is why :func:`_live_sessions` scans the
    per-session lifecycle roots first and falls back to this.
    """
    command = cli_command()
    if command is None:
        return []
    try:
        proc = subprocess.run(
            [*command, "list"],
            capture_output=True,
            timeout=_HOT_LOAD_TIMEOUT_S,
            env=cli_env(),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        token = line.strip().split()[:1]
        if token and token[0].startswith(_SESSION_PREFIX):
            names.append(token[0])
    return names


def _live_sessions() -> dict[str, dict[str, str]]:
    """Candidate live ``kc-*`` sessions, each with the env a CLI client needs to reach it.

    Every generated session gets its own ``<data-home>/pw/<8hex>/{s,d}`` subtree
    (:func:`kiro_crew.browser_cli.launch.browser_socket_env`), precisely so
    ``playwright-cli list`` under one root cannot see a peer's browser -- which
    also means a bare ``list`` from the gateway sees none of them. So the
    gateway enumerates by SHAPE: a ``pw/<8hex>/s/cli/*.sock`` control socket
    marks session ``kc-<8hex>`` as a candidate. A socket can outlive its daemon
    (the CLI unlinks it on a failed connect, not on exit), so "candidate" is
    the honest word; the command run against it reports the truth. Falls back
    to the ``list`` output when no per-session root holds a socket.
    """
    found: dict[str, dict[str, str]] = {}
    root = config_dir() / _LIFECYCLE_DIR
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        name = f"{_SESSION_PREFIX}{entry.name}"
        if not _session_leaf(name):
            continue
        try:
            sockets = [p for p in (entry / "s" / "cli").iterdir() if p.suffix == ".sock"]
        except OSError:
            continue
        if sockets:
            found[name] = _session_env(name)
    if found:
        return found
    return {name: {} for name in _live_session_names()}


def _run_on_sessions(
    verb: list[str], sessions: Mapping[str, Mapping[str, str]]
) -> tuple[list[str], dict[str, str]]:
    """Run ``playwright-cli -s=<name> <verb>`` on each session; ``(succeeded, {name: reason})``.

    Never raises. Each session gets the gateway's CLI environment plus its own
    lifecycle variables, so the client connects to THAT session's daemon.
    """
    command = cli_command()
    if command is None:
        return [], {name: "playwright-cli is not installed" for name in sessions}
    base_env = cli_env()
    done: list[str] = []
    failed: dict[str, str] = {}
    for name, overrides in sessions.items():
        env = {**base_env, **overrides}
        try:
            proc = subprocess.run(
                [*command, f"-s={name}", *verb],
                capture_output=True,
                timeout=_HOT_LOAD_TIMEOUT_S,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failed[name] = str(exc)
            continue
        if proc.returncode == 0:
            done.append(name)
        else:
            failed[name] = (proc.stderr or proc.stdout or f"{verb[0]} failed").strip()[:200]
    return done, failed


def hot_load_into_live_sessions(path: Path) -> dict[str, Any]:
    """Load *path* into every live ``kc-*`` session, best effort.

    Runs ``playwright-cli -s=<name> state-load <path>`` against each candidate
    session (see :func:`_live_sessions`). NEVER raises. Returns ``{"loaded":
    [names], "failed": {name: reason}}`` plus a one-line ``note`` whenever
    NOTHING loaded, so the caller can show why rather than an empty success.

    Only a GATEWAY-owned daemon (:func:`prewarm_session`) can succeed: the CLI
    forwards the filename and the DAEMON opens it, and a daemon the agent's own
    command started runs inside the agent's sandbox, where the file is masked.
    Such a session fails here with the daemon's own error, which is the
    truthful outcome -- the agent's NEXT session, pre-warmed by the gateway,
    carries the cookies, which is what the panel's new-session hint says.
    """
    if cli_command() is None:
        return {"loaded": [], "failed": {}, "note": "playwright-cli is not installed"}
    sessions = _live_sessions()
    if not sessions:
        return {
            "loaded": [],
            "failed": {},
            "note": "no live browser sessions; cookies apply to new sessions automatically",
        }
    loaded, failed = _run_on_sessions(["state-load", str(path)], sessions)
    result: dict[str, Any] = {"loaded": loaded, "failed": failed}
    if not loaded:
        result["note"] = (
            "no open browser session could load the cookies (a session the agent "
            "started itself cannot read them); they apply to new sessions automatically"
        )
    return result


def clear_live_sessions() -> dict[str, Any]:
    """Clear the cookies of every live ``kc-*`` session, best effort.

    Companion of :func:`clear_storage_state`: deleting the file stops NEW
    sessions from loading the cookies, but a browser already open stays signed
    in until it is closed. This runs ``playwright-cli -s=<name> cookie-clear``
    on each candidate session so a clear from the dashboard means what it says.
    NEVER raises. Returns ``{"cleared": [names], "failed": {name: reason}}``
    plus a ``note`` when there was nothing to clear.
    """
    if cli_command() is None:
        return {"cleared": [], "failed": {}, "note": "playwright-cli is not installed"}
    sessions = _live_sessions()
    if not sessions:
        return {"cleared": [], "failed": {}, "note": "no live browser sessions"}
    cleared, failed = _run_on_sessions(["cookie-clear"], sessions)
    return {"cleared": cleared, "failed": failed}


# ── Pre-warm: a gateway-owned daemon for the agent's session ─────────────────


def _run_prewarm(argv: list[str], env: dict[str, str], session_name: str) -> None:
    """Thread body of :func:`prewarm_session`: run the CLI once and log the outcome."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PREWARM_TIMEOUT_S,
            env=env,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("browser pre-warm for %s did not finish in time", session_name)
        return
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("browser pre-warm for %s could not run: %s", session_name, exc)
        return
    if proc.returncode != 0:
        logger.warning(
            "browser pre-warm for %s failed (rc=%s): %s",
            session_name,
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip()[:200],
        )
    else:
        logger.debug("browser pre-warm for %s started a daemon with imported cookies", session_name)


def prewarm_session(session_name: str, env: Mapping[str, str]) -> bool:
    """Start the daemon for an agent's browser session from the GATEWAY, with the cookies.

    Called at the two agent spawn sites right after the child's browser
    environment is computed. When imported cookies exist, this runs
    ``playwright-cli -s=<session_name> open about:blank`` in a daemon thread,
    OUTSIDE any sandbox, with :func:`gateway_config_path` as the CLI config and
    the same ``SOCKETS_ENV`` / ``DAEMON_DIR_ENV`` values *env* hands the agent.
    The daemon that command leaves running is the one the agent's later
    commands connect to over the socket, so the agent's session carries the
    cookies while the file that holds them stays masked from the agent's
    filesystem view. The daemon's exec-time environment also carries
    ``SESSION_ENV`` and the ``KIROCREW_SPAWNED`` marker, exactly as an
    agent-started daemon's would, so the orphan sweep in ``session_pid`` still
    recognises and reaps it once the agent process is gone.

    Returns ``True`` when a pre-warm was SCHEDULED, ``False`` when there was
    nothing to do -- no imported cookies, a name that is not a generated
    ``kc-<8hex>``, an *env* without the lifecycle variables (the daemon would
    then land under the agent's private ``TMPDIR`` where the gateway cannot
    address it), an operator-supplied ``PLAYWRIGHT_MCP_CONFIG`` (their config
    wins, as everywhere else), or no CLI. Fire-and-forget: never blocks the
    spawn, never raises; a failure is one warning in the gateway log.

    Residual race, by design: an agent whose FIRST browser command lands before
    this daemon is up starts its own in-sandbox, cookie-less daemon, and the
    pre-warm then fails to bind the same session. The panel already tells the
    user that imported cookies apply to NEW sessions, and the next spawn gets
    them.
    """
    try:
        if not _session_leaf(session_name):
            return False
        sockets = env.get(SOCKETS_ENV, "").strip()
        daemons = env.get(DAEMON_DIR_ENV, "").strip()
        if not sockets or not daemons:
            return False
        configured = os.environ.get(CONFIG_ENV, "").strip()
        if configured and configured != str(launch_config_path()):
            return False
        if not storage_state_path().is_file():
            return False
        command = cli_command()
        if command is None:
            return False
        config = write_gateway_config()
        if config is None:
            return False
        child_env = cli_env()
        child_env.update(
            {
                SESSION_ENV: session_name,
                SOCKETS_ENV: sockets,
                DAEMON_DIR_ENV: daemons,
                CONFIG_ENV: str(config),
                KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE,
            }
        )
        argv = [*command, f"-s={session_name}", "open", "about:blank"]
        threading.Thread(
            target=_run_prewarm,
            args=(argv, child_env, session_name),
            name=f"browser-prewarm-{session_name}",
            daemon=True,
        ).start()
        return True
    except Exception as exc:  # noqa: BLE001 - a spawn must never fail on a pre-warm
        logger.warning("browser pre-warm for %s was skipped: %s", session_name, exc)
        return False
