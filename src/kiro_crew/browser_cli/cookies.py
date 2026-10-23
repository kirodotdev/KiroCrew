"""Imported browser cookies, stored as a Playwright ``storageState`` and applied
to the agent's ``playwright-cli`` sessions.

The dashboard user exports cookies from the browser where they are already
logged in and imports them here; the gateway normalises them to Playwright's
cookie shape and writes one owner-only ``storageState`` file in its own
directory under the data home. That directory is **bind-masked out of every
agent sandbox** (``sandbox._CREW_HIDDEN_LEAVES``, pre-created before every
spawn so the mask always mounts): the agent must never be able to read a
session cookie off disk, and a spawned shell's ``open()`` never routes through
the tool gate, so only the OS mask holds. The cookies still have to reach the
agent's browser. Three invariants decide how:

1. **The agent's browser daemon always runs inside the agent's sandbox**,
   started by the agent's own command exactly as before this feature. The
   gateway never starts a daemon for an agent session: a gateway-privileged
   daemon driven by the agent (``upload <masked path>``, ``state-save``) would
   be a sandbox escape.
2. **No config the agent or its daemon reads names the hidden file.** The
   launch config (:func:`kiro_crew.browser_cli.launch.desired_config`) stays
   engine-only; there is no second, gateway-side config.
3. **Cookies reach a daemon only through its control socket, as data.** The
   gateway connects to the session's control socket itself and sends one
   ``cookie-set`` request per cookie in the daemon's own wire format
   (newline-delimited JSON, :func:`_inject_into_session`); the daemon does
   ``context.addCookies``. It opens no file, and no process is spawned, so a
   cookie value is never on any argv. Two entry points: the import handler injects into every live
   ``kc-*`` session at once (:func:`inject_into_live_sessions`), and a
   gateway-side :class:`SessionWatcher` thread injects into every daemon
   socket that appears afterwards, so a session the agent starts later carries
   the cookies too. The clear handler runs ``cookie-clear`` on the live
   sessions (:func:`clear_live_sessions`) and resets the watcher's record.

**Residuals, stated plainly.** (i) The watcher polls every
:data:`WATCH_INTERVAL_S` seconds, so the very first navigation of a brand-new
session can run before its cookies land (at most one interval late; the next
navigation has them). (ii) The control socket is the agent's own: anything
that can connect to it (the agent's sandbox, same-UID processes) can already
drive that browser, including ``cookie-list``, so the values are as readable
there as the cookies of any site the agent logs into. The daemon's reply
echoes the value, which is why an injection failure is reported by a fixed
reason and never by the reply's text.

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
import socket
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser_cli.install import cli_command, cli_env
from kiro_crew.browser_cli.launch import (
    _LIFECYCLE_DIR,
    _SESSION_PREFIX,
    DAEMON_DIR_ENV,
    SESSION_ENV,
    SOCKETS_ENV,
    _session_leaf,
    daemon_dir,
    socket_dir,
)
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: The directory under the data home that holds the storageState. A DIRECTORY,
#: not a bare file, so its sandbox mask always has a name to bind over: it is
#: pre-created before every namespace spawn (``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``),
#: and a file written inside it later stays inside the stand-in a running sandbox
#: sees. A bare file absent at spawn gets no mask, so a sandbox started before the
#: first import would read the cookies. Listed in ``sandbox._CREW_HIDDEN_LEAVES``
#: and ``security._CREW_SECRET_LEAVES`` under exactly this name: renaming it here
#: without renaming it there would silently put the cookie values back in the
#: agent's reach. Nothing the agent or its daemon reads ever names this path.
STORAGE_STATE_DIR = "browser-cookie-import"

#: The storageState file inside :data:`STORAGE_STATE_DIR`.
STORAGE_STATE_FILE = "storage-state.json"

#: Reject an import whose raw text exceeds this, before parsing: the same 2 MiB
#: ceiling the HTTP handler enforces, restated here so a non-HTTP caller
#: (a test, a future CLI path) gets the same bound.
MAX_IMPORT_BYTES = 2 * 1024 * 1024

#: Reject an import carrying more than this many cookies. A logged-in browser
#: profile has a few hundred; five thousand is far past any legitimate export
#: and bounds the work of writing and re-loading the state.
MAX_COOKIES = 5000

#: How long one CLI client call against a live session (``cookie-set`` for one
#: cookie, ``cookie-clear``, ``list``) may take before it is abandoned.
_CLI_TIMEOUT_S = 15.0

#: Ceiling on one daemon reply line. A ``cookie-set`` reply is a short
#: confirmation; a peer that streams more than this without a newline is not a
#: daemon this module understands, and the read stops rather than buffering it.
_REPLY_MAX_BYTES = 1024 * 1024

#: How often the :class:`SessionWatcher` looks for new daemon sockets. The
#: first navigation of a brand-new session can run up to this long before its
#: cookies land (see the module docstring).
WATCH_INTERVAL_S = 2.0

#: Upper bound on the sessions one watcher tick injects into, so a burst of new
#: sessions cannot turn one tick into an unbounded run of CLI spawns; the rest
#: are picked up on the following ticks.
WATCH_MAX_SESSIONS_PER_TICK = 4

#: How many ticks the watcher retries a socket whose injection failed before it
#: stops trying. A socket can appear before its daemon accepts connections, and
#: a socket can outlive its daemon (the CLI unlinks it on a failed connect, not
#: on exit); a small budget covers the first and bounds the second.
WATCH_MAX_ATTEMPTS = 3

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


#: One lock for every read-and-apply of the stored cookies: an import (save then
#: inject), a clear (delete then ``cookie-clear``) and a watcher tick (load then
#: inject). Without it a tick that loaded the cookies before a clear could inject
#: them after the clear reported success, leaving the browser signed in. Taken
#: only off the event loop (the handlers run their transaction in a thread).
_TRANSACTION_LOCK = threading.Lock()


def transaction() -> threading.Lock:
    """The lock an import or clear holds across its whole read-and-apply."""
    return _TRANSACTION_LOCK


class CookieImportError(ValueError):
    """A cookie import that cannot be accepted, with a user-readable message."""


def storage_state_path() -> Path:
    """Where the imported storageState lives, under the data home."""
    return config_dir() / STORAGE_STATE_DIR / STORAGE_STATE_FILE


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


#: The string spellings of a boolean cookie attribute that exports emit.
#: Cookie-Editor sometimes writes ``"secure": "true"``; Netscape exports carry
#: ``TRUE``/``FALSE`` (handled by :func:`_parse_netscape` before this point).
_FLAG_STRINGS = {"true": True, "1": True, "false": False, "0": False}


def _normalize_flag(raw: Any, field: str, cookie_name: str) -> bool:
    """Normalise a ``secure`` / ``httpOnly`` attribute to a real boolean.

    Accepts a JSON boolean, an absent/``null`` value (``False``), and the
    strings ``"true"``/``"false"``/``"1"``/``"0"`` case-insensitively, which is
    what the extension exports actually produce. Anything else -- a number, an
    object, another word -- raises :class:`CookieImportError` rather than being
    coerced: ``bool("false")`` is ``True``, and silently marking an httpOnly
    cookie as script-readable (or the reverse) is exactly the kind of quiet
    corruption an import must refuse.
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        mapped = _FLAG_STRINGS.get(raw.strip().lower())
        if mapped is not None:
            return mapped
    raise CookieImportError(
        f"cookie {cookie_name!r} has an invalid {field} value; expected true or false"
    )


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
        "httpOnly": _normalize_flag(raw.get("httpOnly"), "httpOnly", name),
        "secure": _normalize_flag(raw.get("secure"), "secure", name),
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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
            timeout=_CLI_TIMEOUT_S,
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


#: Identity of one daemon control socket: ``(path, inode, ctime_ns)``. A new
#: daemon for the same session gets a new socket file, so a key that has been
#: injected into stays injected even when the path is later reused.
SocketKey = tuple[str, int, int]


def _live_sockets() -> dict[str, SocketKey]:
    """Live ``kc-*`` sessions found by SHAPE, each with its newest control socket.

    Every generated session gets its own ``<data-home>/pw/<8hex>/{s,d}`` subtree
    (:func:`kiro_crew.browser_cli.launch.browser_socket_env`), precisely so
    ``playwright-cli list`` under one root cannot see a peer's browser -- which
    also means a bare ``list`` from the gateway sees none of them. So the
    gateway enumerates by shape: a ``pw/<8hex>/s/cli/*.sock`` control socket
    marks session ``kc-<8hex>`` as a candidate. A socket can outlive its daemon
    (the CLI unlinks it on a failed connect, not on exit), so "candidate" is
    the honest word; the command run against it reports the truth. Never
    raises; an unreadable root is an empty result.
    """
    found: dict[str, SocketKey] = {}
    root = config_dir() / _LIFECYCLE_DIR
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        name = f"{_SESSION_PREFIX}{entry.name}"
        if not _session_leaf(name):
            continue
        newest: tuple[int, SocketKey] | None = None
        try:
            for sock in (entry / "s" / "cli").iterdir():
                if sock.suffix != ".sock":
                    continue
                st = sock.stat()
                key: SocketKey = (str(sock), st.st_ino, st.st_ctime_ns)
                if newest is None or st.st_ctime_ns > newest[0]:
                    newest = (st.st_ctime_ns, key)
        except OSError:
            continue
        if newest is not None:
            found[name] = newest[1]
    return found


def _live_sessions() -> dict[str, dict[str, str]]:
    """Candidate live ``kc-*`` sessions, each with the env a CLI client needs to reach it.

    The socket-shaped enumeration of :func:`_live_sockets`, falling back to the
    ``list`` output when no per-session root holds a socket (a host where the
    lifecycle hooks are unsupported registers sessions in the default registry
    instead, reachable with no extra environment).
    """
    found = {name: _session_env(name) for name in _live_sockets()}
    if found:
        return found
    return {name: {} for name in _live_session_names()}


def _run_on_sessions(
    verb: list[str], sessions: Mapping[str, Mapping[str, str]]
) -> tuple[list[str], dict[str, str]]:
    """Run ``playwright-cli -s=<name> <verb>`` on each session; ``(succeeded, {name: reason})``.

    Never raises. Each session gets the gateway's CLI environment plus its own
    lifecycle variables, so the client connects to THAT session's daemon. The
    verb is value-free (``cookie-clear``), so the daemon's own error text is
    the reason reported -- it names the session, never a cookie.
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
                timeout=_CLI_TIMEOUT_S,
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


def _cookie_set_args(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """The ``cookie-set`` request for one normalised cookie, as the daemon parses it.

    This is the object the CLI client builds from its own argv with minimist and
    sends as ``params.args`` (verified against the bundled CLI's
    ``parseCliCommand``): ``_`` holds the command and its two positionals, and
    every option is a key the command's strict schema declares. ``expires`` is a
    number and is omitted for a session cookie (``-1``); the two flags are sent
    only when true. A name or value is a JSON string field, so nothing in it can
    be read as an option.
    """
    args: dict[str, Any] = {
        "_": ["cookie-set", str(cookie["name"]), str(cookie.get("value", ""))],
        "domain": str(cookie["domain"]),
        "path": str(cookie.get("path") or "/"),
        "sameSite": str(cookie.get("sameSite") or "Lax"),
    }
    expires = cookie.get("expires")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
        args["expires"] = int(expires) if float(expires).is_integer() else float(expires)
    if cookie.get("httpOnly"):
        args["httpOnly"] = True
    if cookie.get("secure"):
        args["secure"] = True
    return args


def _connect(socket_path: str, timeout: float) -> socket.socket:
    """Connect to one daemon control socket (a seam the tests replace)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
    except BaseException:
        sock.close()
        raise
    return sock


def _read_reply(sock: socket.socket, buf: bytearray, deadline: float) -> bytes:
    """One newline-terminated reply line; leftover bytes stay in *buf*.

    *deadline* is the exchange's absolute ``time.monotonic()`` bound, re-applied
    before every receive: a peer that trickles bytes with no newline would
    otherwise get a fresh per-``recv`` timeout each time and hold the exchange
    open far past it.
    """
    while True:
        end = buf.find(b"\n")
        if end != -1:
            line = bytes(buf[:end])
            del buf[: end + 1]
            return line
        if len(buf) > _REPLY_MAX_BYTES:
            raise ValueError("reply too long")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("reply deadline passed")
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk


def _load_cookies() -> list[dict[str, Any]] | None:
    """The stored cookies, or ``None`` when there is no readable state file."""
    try:
        data = json.loads(storage_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return None
    return [c for c in cookies if isinstance(c, dict) and c.get("name") and c.get("domain")]


def _inject_into_session(socket_path: str, cookies: Iterable[Mapping[str, Any]]) -> str:
    """Send every cookie to one daemon over its control socket; ``""`` or a reason.

    Speaks the daemon's own protocol: one JSON object per line,
    ``{"id", "method": "run", "params": {"args", "cwd"}}``, answered by
    ``{"id", "result": {"isError", "text"}}`` or ``{"id", "error"}``. One
    connection carries every cookie, the whole exchange is bounded by
    :data:`_CLI_TIMEOUT_S`, and it stops at the first failure. No process is
    spawned, so a cookie value never appears on an argv. The reason is a fixed,
    value-free string: the reply text echoes the cookie, so it is never quoted.
    """
    if not hasattr(socket, "AF_UNIX"):
        return "unsupported platform"
    deadline = time.monotonic() + _CLI_TIMEOUT_S
    try:
        sock = _connect(socket_path, _CLI_TIMEOUT_S)
    except TimeoutError:
        return "timed out"
    except OSError as exc:
        return type(exc).__name__
    buf = bytearray()
    with sock:
        for message_id, cookie in enumerate(cookies, start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timed out"
            message = {
                "id": message_id,
                "method": "run",
                "params": {"args": _cookie_set_args(cookie), "cwd": "/"},
            }
            try:
                sock.settimeout(remaining)
                sock.sendall(json.dumps(message).encode("utf-8") + b"\n")
                line = _read_reply(sock, buf, deadline)
            except TimeoutError:
                return "timed out"
            except (OSError, ValueError) as exc:
                return type(exc).__name__
            try:
                reply = json.loads(line)
            except ValueError:
                return "malformed reply"
            if not isinstance(reply, dict) or reply.get("id") != message_id:
                return "malformed reply"
            result = reply.get("result")
            if "error" in reply or not isinstance(result, dict) or result.get("isError"):
                return "the browser refused the cookie"
    return ""


def inject_into_live_sessions() -> dict[str, Any]:
    """Inject the stored cookies into every live ``kc-*`` session, best effort.

    The import handler's half of the two injection points (the
    :class:`SessionWatcher` is the other). NEVER raises. Returns ``{"loaded":
    [names], "failed": {name: reason}}`` plus a one-line ``note`` whenever
    NOTHING loaded, so the caller can show why rather than an empty success.
    Also records the sockets it reached with the watcher, so the next tick does
    not repeat the work.
    """
    cookies = _load_cookies()
    if not cookies:
        return {"loaded": [], "failed": {}, "note": "no imported cookies to apply"}
    sockets = _live_sockets()
    if not sockets:
        return {
            "loaded": [],
            "failed": {},
            "note": "no live browser sessions; cookies apply to new sessions automatically",
        }
    loaded: list[str] = []
    failed: dict[str, str] = {}
    # Align the watcher with the file just written, so the sockets recorded
    # below belong to THIS state and its next tick does not redo them.
    _WATCHER.sync_state()
    for name, key in sockets.items():
        reason = _inject_into_session(key[0], cookies)
        if reason:
            failed[name] = reason
        else:
            loaded.append(name)
            _WATCHER.mark_injected(key)
    result: dict[str, Any] = {"loaded": loaded, "failed": failed}
    if not loaded:
        result["note"] = (
            "no open browser session accepted the cookies; they apply to new "
            "sessions automatically"
        )
    return result


def clear_live_sessions() -> dict[str, Any]:
    """Clear the cookies of every live ``kc-*`` session, best effort.

    Companion of :func:`clear_storage_state`: deleting the file stops NEW
    sessions from receiving the cookies, but a browser already open stays signed
    in until it is closed. This runs ``playwright-cli -s=<name> cookie-clear``
    on each candidate session so a clear from the dashboard means what it says,
    and forgets the watcher's record so a later import injects again. NEVER
    raises. Returns ``{"cleared": [names], "failed": {name: reason}}`` plus a
    ``note`` when there was nothing to clear.
    """
    _WATCHER.reset()
    if cli_command() is None:
        return {"cleared": [], "failed": {}, "note": "playwright-cli is not installed"}
    sessions = _live_sessions()
    if not sessions:
        return {"cleared": [], "failed": {}, "note": "no live browser sessions"}
    cleared, failed = _run_on_sessions(["cookie-clear"], sessions)
    return {"cleared": cleared, "failed": failed}


# ── SessionWatcher: cookies for daemons that appear after the import ─────────


class SessionWatcher:
    """Gateway-side thread that injects the stored cookies into new daemon sockets.

    Invariant 1 (the module docstring) rules out starting a daemon for the agent,
    so a session the agent opens AFTER an import has to be reached once it
    exists. Every :data:`WATCH_INTERVAL_S` seconds the watcher lists
    ``pw/<8hex>/s/cli/*.sock`` and, for any socket it has not injected into yet
    (keyed by path + inode + ctime), sends ``cookie-set`` per cookie over that
    socket and records the key. It only does work while the storage-state file
    exists; when the file's mtime changes (a re-import) the record is dropped so
    every live session is injected again, and :meth:`reset` (the clear handler)
    drops it too.

    Entirely off the event loop: a daemon thread the aiohttp app only starts and
    stops. Bounded: at most :data:`WATCH_MAX_SESSIONS_PER_TICK` sessions per
    tick, :data:`_CLI_TIMEOUT_S` per session, :data:`WATCH_MAX_ATTEMPTS` tries per
    socket. Never raises out of a tick; everything is logged at debug.
    """

    def __init__(self, interval: float = WATCH_INTERVAL_S) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._done: set[SocketKey] = set()
        self._attempts: dict[SocketKey, int] = {}
        self._state_sig: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- record ---------------------------------------------------------------

    def mark_injected(self, key: SocketKey) -> None:
        """Record that *key* already carries the current cookies."""
        with self._lock:
            self._done.add(key)
            self._attempts.pop(key, None)

    def reset(self) -> None:
        """Forget every injected socket, so the next tick with a file injects again."""
        with self._lock:
            self._done.clear()
            self._attempts.clear()
            self._state_sig = None

    def injected(self) -> frozenset[SocketKey]:
        """The sockets recorded as injected (for tests and diagnostics)."""
        with self._lock:
            return frozenset(self._done)

    def sync_state(self) -> bool:
        """Align the record with the state file; ``True`` when a file exists.

        A changed mtime/size (a re-import) drops the record so every live
        session is due again; a missing file drops it too, since a later import
        must reach every session. Called by the import path before it records
        what it reached, and by every tick.
        """
        try:
            st = storage_state_path().stat()
        except OSError:
            self.reset()
            return False
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if sig != self._state_sig:
                self._done.clear()
                self._attempts.clear()
                self._state_sig = sig
        return True

    # -- one pass -------------------------------------------------------------

    def tick(self) -> int:
        """One pass; returns how many sessions were injected into. Never raises."""
        try:
            return self._tick()
        except Exception:  # noqa: BLE001 - a watcher pass must never take the thread down
            logger.debug("browser cookie watcher tick failed", exc_info=True)
            return 0

    def _tick(self) -> int:
        with _TRANSACTION_LOCK:
            return self._tick_locked()

    def _tick_locked(self) -> int:
        if not self.sync_state():
            return 0
        sockets = _live_sockets()
        if not sockets:
            return 0
        pending: list[tuple[str, SocketKey]] = []
        with self._lock:
            for name, key in sockets.items():
                if key in self._done or self._attempts.get(key, 0) >= WATCH_MAX_ATTEMPTS:
                    continue
                pending.append((name, key))
            # Drop records for sockets that are gone, so the sets stay
            # bounded by the live population.
            live = set(sockets.values())
            self._done &= live
            self._attempts = {k: n for k, n in self._attempts.items() if k in live}
        if not pending:
            return 0
        cookies = _load_cookies()
        if not cookies:
            return 0
        injected = 0
        for name, key in pending[:WATCH_MAX_SESSIONS_PER_TICK]:
            reason = _inject_into_session(key[0], cookies)
            with self._lock:
                if reason:
                    self._attempts[key] = self._attempts.get(key, 0) + 1
                    logger.debug(
                        "browser cookie watcher: %s not injected (%s), attempt %d",
                        name,
                        reason,
                        self._attempts[key],
                    )
                else:
                    self._done.add(key)
                    self._attempts.pop(key, None)
                    injected += 1
                    logger.debug(
                        "browser cookie watcher: injected %d cookies into %s", len(cookies), name
                    )
        return injected

    # -- thread ---------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._interval)

    def start(self) -> None:
        """Start the thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="browser-cookie-watcher", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread and wait for it (idempotent)."""
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()


#: The one watcher per gateway. The dashboard starts it where the browser-view
#: cleanup is registered and stops it on cleanup; the handlers only touch its
#: record.
_WATCHER = SessionWatcher()


def start_session_watcher() -> None:
    """Start the gateway's cookie watcher when it has work (idempotent).

    A no-op until a storage-state file exists: gateway start calls this, and so
    does the import handler after it saves, so a gateway that never
    imports cookies runs no thread at all. Also a no-op where the platform has no
    ``AF_UNIX`` sockets, since :func:`_inject_into_session` cannot reach a daemon
    there and every tick would only fail.
    """
    if not hasattr(socket, "AF_UNIX"):
        return
    try:
        present = storage_state_path().is_file()
    except OSError:
        return
    if present:
        _WATCHER.start()


def stop_session_watcher() -> None:
    """Stop the gateway's cookie watcher (idempotent)."""
    _WATCHER.stop()
