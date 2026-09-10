"""Open a URL the dashboard OWNER typed into the Browser panel, in the gateway host's browser.

The Browser panel has two transports. In the desktop app a native Chromium view
owns the panel and an external site lands there. Everywhere else -- a plain
browser tab, including a laptop reaching a remote gateway over an SSH tunnel --
the only browser that can render an external site is the Playwright CLI's, on the
gateway host, shown through the ``show`` dashboard that :mod:`view` supervises.
The CLI's own dashboard cannot open a session by itself (its bundle renders "No
open sessions." and offers navigation only inside one that already exists), and
every other ``playwright-cli`` invocation is an agent's shell turn, so without
this module the address bar would point a CSP-blocked iframe at ``google.com``
and report the site as "not reachable".

This module is that launcher. It runs the CLI as a supervised subprocess,
exactly as :mod:`view` runs ``show``: ``playwright-cli -s=<session> goto <url>``
when the session's browser is up, ``open <url>`` when the CLI's own ``list``
says it is not (a bare ``open`` on a live session tears that browser down and
starts another, losing its tabs). The human then sees and drives the page in the
framed ``show`` view.

**Consent.** The capability model routes AGENT browsing through the shell approval
ladder, and this endpoint would let an agent skip it, so the route is owner-only
and refuses ``X-Internal-Secret`` callers (:func:`handlers.api_browser_open`). A
human pressing Enter in an authenticated dashboard IS the approval. That is also
why the spawn is benign for ``test_spawn_audit``: the only free input is a URL the
owner typed, validated to ``http``/``https`` before it reaches argv.

**Session naming and ownership.** One session per dashboard chat slot,
``panel-<owner6>-<slot8>``: a digest of this gateway's data home, then one of the
slot's session key. The name deliberately does NOT match the generated
``kc-<8hex>`` shape: the orphan sweep (``session_pid``) reclaims a ``kc-`` daemon
as soon as no live process carries its ``PLAYWRIGHT_CLI_SESSION``, and the only
process that ever carries this one is the short-lived CLI invocation itself -- a
generated name would have the sweep kill the human's browser out from under them
ten minutes in. A ``panel-`` session is therefore operator-class to the sweep
(structurally excluded, never signalled) and its lifetime is owned HERE, by the
gateway whose tag it carries. Several gateways on one host can share the CLI's
session registry (same ``HOME`` and working directory: a pod started from the
live checkout, a second install), so the owner is made legible from the name:
a sibling never produces this gateway's tag, a ``goto`` against a session
already open under our name can only be reaching this gateway's own previous
life, and only sessions under our prefix are ever closed -- every one this life
opened at shutdown (:func:`close_all`, hooked beside :func:`view.stop`), and
any a previous life left behind at startup (:func:`reclaim_stranded`). A panel
session never outlives the gateway that owns it; an unclean death only defers
the close to the next start.

Every function here blocks on a subprocess; a caller on the event loop offloads it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from kiro_crew import platform_compat
from kiro_crew.browser_cli.install import (
    _redact,
    cli_command,
    cli_dashboard_socket_supported,
    cli_env,
    cli_path,
)
from kiro_crew.browser_cli.launch import SESSION_ENV, SOCKETS_ENV, ui_socket_env
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Reserved for the Browser panel's own sessions. Distinct from the generated
#: ``kc-`` prefix on purpose -- see the module docstring.
SESSION_PREFIX = "panel-"

# ``open`` launches Chromium and then navigates; a cold start on a slow host is
# tens of seconds, and the CLI's own navigation timeout has to fit inside.
_OPEN_TIMEOUT_S = 90.0
_GOTO_TIMEOUT_S = 60.0
_LIST_TIMEOUT_S = 20.0
_CLOSE_TIMEOUT_S = 20.0
_REVEAL_TIMEOUT_S = 2.0
#: What :func:`_run_cli` answers when the CLI outlived its budget (the shell
#: convention for a timed-out command).
_TIMEOUT_RC = 124

# The panel renders the error verbatim; a stack trace with a 2 KB argv dump in
# it is not what an operator needs to read.
_ERROR_CAP = 2000

#: Chromium's own words when the host cannot run its sandbox. Decides only
#: whether :data:`SANDBOX_REMEDY` is appended; the CLI's text is shown either way.
_SANDBOX_MARKER = "No usable sandbox"

#: The remedy for the sandbox case, in the spec's terms: Kiro Crew never drops
#: the sandbox by default; the operator names their own config to accept that
#: trade-off. Appended AFTER the CLI's verbatim text so it cannot be pushed out
#: of the cap.
SANDBOX_REMEDY = (
    "Chromium could not start because this host cannot run its sandbox. Kiro Crew "
    "never disables the sandbox by default. To accept that trade-off on this host, "
    "point PLAYWRIGHT_MCP_CONFIG in the gateway's environment at your own "
    'playwright-cli config, for example {"browser": {"browserName": "chromium", '
    '"launchOptions": {"chromiumSandbox": false}}}, and restart the gateway.'
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: Node's uncaught-error preamble: an absolute ``<file>.js:<line>`` on its own,
#: followed by the source line and a ``^`` caret. Diagnosis-free.
_NODE_FRAME_RE = re.compile(r"^\S+\.[cm]?js:\d+$")
#: Lines of the CLI's output that carry no diagnosis: the update-available box
#: and the launch call-log's lifecycle chatter (the ``<launching>`` line alone is
#: the full Chromium argv, ~2 KB).
_NOISE_PREFIXES = ("╔", "║", "╚")
_NOISE_MARKERS = (
    "<launching>",
    "<launched>",
    "<gracefully close",
    "<kill>",
    "<will force kill>",
    "<process did exit",
    "temporary directories cleanup",
)


@dataclass(frozen=True)
class LaunchResult:
    """What one address-bar submission produced."""

    ok: bool
    #: The CLI session the slot's browser lives in (``panel-<owner6>-<slot8>``). The panel
    #: shows it in the view's header so the human can tell this chat's session
    #: from the others in the framed dashboard's sidebar.
    session: str
    #: The CLI's own text (plus the sandbox remedy when it applies); ``None`` on
    #: success.
    error: str | None
    #: Whether the framed ``show`` dashboard attached its viewport to the session
    #: after a successful launch. ``False`` when the reveal was skipped (no shared
    #: socket root, an unsupported bundle layout, Windows) or nothing answered on
    #: the socket: the page is open, and the reader has to pick the session in
    #: the dashboard's sidebar -- the panel says so only in that case.
    attached: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. The URL is the caller's own input and is not echoed."""
        return {
            "ok": self.ok,
            "session": self.session,
            "error": self.error,
            "attached": self.attached,
        }


_lock = threading.Lock()
#: Per-session locks so two submissions for one slot cannot race an ``open``
#: against a ``goto`` (a concurrent ``open`` pair would start two daemons).
_session_locks: dict[str, threading.Lock] = {}
#: Sessions this gateway opened, so shutdown can close exactly those.
_opened: set[str] = set()


def owner_tag() -> str:
    """Six hex digits naming THIS gateway: a digest of its data home.

    The data home (:func:`config_dir`) is what tells one gateway from another on
    a host where several share the CLI's session registry (same ``HOME`` and
    working directory -- a pod started from the live checkout, a second install);
    it is also what scopes the gateway's socket root, so the two agree. An
    identifier, not a secret.
    """
    return hashlib.sha256(str(config_dir()).encode("utf-8")).hexdigest()[:6]


def owner_prefix() -> str:
    """``panel-<owner6>-``: the prefix every session THIS gateway owns carries."""
    return f"{SESSION_PREFIX}{owner_tag()}-"


def session_name(session_key: str) -> str:
    """The Playwright session for a dashboard chat slot: ``panel-<owner6>-<slot8>``.

    The owner tag makes the session's owner legible from its name alone, which is
    the ownership contract for a registry several gateways may share: a sibling
    gateway never produces this gateway's tag, so a ``goto`` against a session
    already open under our name ("adopt") can only ever be reaching this
    gateway's own previous life, and only sessions under our prefix are ever
    closed (:func:`close_all`, :func:`reclaim_stranded`). Deterministic per slot
    so a gateway that restarted without closing its sessions finds the same
    browser again instead of leaking a second one; hex-only so the name is inert
    on argv and in a socket path. Digests are identifiers, not secrets: they only
    have to be stable and collision-free across a host's few gateways and a
    dashboard's handful of chat slots.
    """
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:8]
    return f"{owner_prefix()}{digest}"


def validate_url(raw: str) -> str | None:
    """The URL to hand to the CLI, or ``None`` when *raw* must be refused.

    Mirrors the panel's ``normalizeUrl``: an explicit ``http``/``https`` scheme
    with a host, nothing else. Independently re-checked here because the panel is
    one caller of a network endpoint, not the only possible one.

    A secret-bearing URL is refused, not just credentials: the URL becomes a CLI
    subprocess argv, and argv is world-readable through ``/proc/<pid>/cmdline``
    for the life of that process, so a ``user:secret@host`` userinfo, a
    ``?token=`` query, or a ``#access_token=`` fragment would publish the secret
    to every account on the machine. userinfo, query, and fragment are therefore
    all refused. This is an argv limitation: lift it if the URL can ever travel
    to the CLI outside argv (a stdin, file, or documented env channel).
    """
    text = raw.strip()
    if not text or len(text) > 8192:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in text):
        return None
    return text


def _list_argv(command: list[str]) -> list[str]:
    return [*command, "--json", "list"]


def _goto_argv(command: list[str], session: str, url: str) -> list[str]:
    return [*command, f"-s={session}", "goto", url]


def _open_argv(command: list[str], session: str, url: str) -> list[str]:
    return [*command, f"-s={session}", "open", url]


def _close_argv(command: list[str], session: str) -> list[str]:
    return [*command, f"-s={session}", "close"]


def _launch_env(session: str) -> dict[str, str]:
    """The CLI child's environment.

    :func:`cli_env` is what makes every existing rule apply -- ``PATH`` to the
    managed Node toolchain, and the gateway's own ``PLAYWRIGHT_MCP_CONFIG`` /
    snapshot / token variables, which the gateway exported into its process
    environment at startup for exactly this kind of inherited invocation. The
    socket root is the gateway-owned one the ``show`` child also uses
    (:func:`ui_socket_env`), which is what lets :func:`_reveal` find the
    dashboard's singleton socket without re-deriving the CLI's default path. The
    session variable is set to the SAME name ``-s=`` selects so the daemon's
    exec-time environ and its argv agree about which session it is; nothing about
    the sandbox is added, which is the point (see :data:`SANDBOX_REMEDY`).
    """
    env = cli_env()
    env.update(ui_socket_env(env))
    env[SESSION_ENV] = session
    return env


def _run_cli(argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    """Run one CLI command; a timeout or a missing binary reads as a failure."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _TIMEOUT_RC, "", f"playwright-cli did not finish within {timeout:.0f}s"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _session_is_open(command: list[str], session: str, env: dict[str, str]) -> bool | None:
    """Whether *session*'s browser is up, from the CLI's own ``--json list``.

    Structured output rather than the wording of a failed ``goto``: the ``--json``
    flag is part of the CLI's command surface, while an error sentence is not,
    and this answer decides whether an ``open`` -- which replaces a live browser
    -- is allowed to run. ``None`` when the list could not be read, which the
    caller treats as "do not open": a ``goto`` against a dead session fails
    visibly with the CLI's own words, whereas an ``open`` against a live one
    silently destroys its tabs.
    """
    rc, out, _err = _run_cli(_list_argv(command), env, _LIST_TIMEOUT_S)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    browsers = data.get("browsers") if isinstance(data, dict) else None
    if not isinstance(browsers, list):
        return None
    for entry in browsers:
        if isinstance(entry, dict) and entry.get("name") == session:
            return entry.get("status") == "open"
    return False


def _distill(out: str, err: str) -> str:
    """The CLI's own diagnosis, with the noise it wraps it in removed.

    Verbatim lines, not a paraphrase: ANSI codes stripped, the update-available
    box and the launch call-log's lifecycle lines dropped, Node's uncaught-error
    preamble (``<file>.js:<line>``, the source line, the ``^`` caret) dropped,
    and the trailing JS object dump (which repeats every line already shown) cut
    off. Redacted before it is capped: a credential straddling the cap does not
    match its pattern, so redacting the truncated text would leak its head.
    """
    text = _ANSI_RE.sub("", f"{err}\n{out}")
    kept: list[str] = []
    skip_source_line = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if skip_source_line:
            # The line after ``<file>.js:<n>`` is the offending source line.
            skip_source_line = False
            continue
        if _NODE_FRAME_RE.match(stripped):
            skip_source_line = True
            continue
        if set(stripped) == {"^"}:
            continue
        if stripped.startswith(_NOISE_PREFIXES):
            continue
        if any(marker in stripped for marker in _NOISE_MARKERS):
            continue
        if stripped == "] {" or stripped.startswith("log: ["):
            break
        if stripped.endswith("] {"):
            # The last message line, with the object dump opening on its tail.
            kept.append(stripped[: -len(" {")].rstrip())
            break
        kept.append(stripped)
    return _redact("\n".join(kept))[:_ERROR_CAP]


def _error_text(rc: int, out: str, err: str) -> str:
    detail = _distill(out, err) or f"playwright-cli exited with status {rc}"
    if _SANDBOX_MARKER in f"{err}\n{out}":
        detail = f"{detail}\n\n{SANDBOX_REMEDY}"
    return detail


def _session_lock(session: str) -> threading.Lock:
    with _lock:
        lock = _session_locks.get(session)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session] = lock
        return lock


#: Whether the "installed CLI lacks the dashboard socket layout" warning has been
#: emitted: said once per process, at WARNING, so an upstream rename is visible
#: in the gateway log instead of a silent loss of the auto-attach.
_layout_warned = False


def _dashboard_socket_path(env: dict[str, str]) -> str | None:
    """Where the running ``show`` dashboard listens for reveal requests.

    ``<socket root>/dashboard/app.sock``, with the root being the one the
    gateway set for both of its CLI children (:func:`ui_socket_env`). ``None``
    when that root is not set -- the children are then on the CLI's own default
    path, which this module deliberately does not re-derive -- when the installed
    CLI's bundle does not carry the layout (:func:`cli_dashboard_socket_supported`,
    which is reported once at WARNING so the loss is visible), or on Windows,
    where the dashboard uses a named pipe and the human picks the session from
    the dashboard's own sidebar instead.
    """
    global _layout_warned
    if platform_compat.IS_WINDOWS:
        return None
    root = env.get(SOCKETS_ENV, "").strip()
    if not root:
        return None
    if not cli_dashboard_socket_supported():
        if not _layout_warned:
            _layout_warned = True
            logger.warning(
                "installed playwright-cli does not expose the dashboard socket layout the "
                "Browser panel's auto-attach relies on; a launched page will need one click "
                "on its session in the framed dashboard"
            )
        return None
    return os.path.join(root, "dashboard", "app.sock")


def _reveal(session: str, env: dict[str, str]) -> bool:
    """Ask the running ``show`` dashboard to attach its viewport to *session*.

    Best-effort by contract. The dashboard's session grid does not attach to a
    session on its own, so without this the human would land on a sidebar entry
    and one more click; the CLI's own way to do it (``show -s=<name>`` with no
    port) is deliberately NOT used, because when the singleton socket is stale it
    becomes the winner and launches a Chromium app window on the gateway host.
    Connecting to the socket ourselves fails closed instead: no listener, no
    reveal, and nothing else happens.

    Returns whether the dashboard took the request (the line was sent and the
    server answered). ``False`` is the "pick the session in the sidebar" case,
    which the panel tells the reader about.
    """
    path = _dashboard_socket_path(env)
    if path is None:
        return False
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_REVEAL_TIMEOUT_S)
        sock.connect(path)
        sock.sendall(json.dumps({"sessionName": session}).encode("utf-8") + b"\n")
        # The server answers with its pid and closes; read to EOF so the request
        # is not torn down before it is parsed.
        while sock.recv(1024):
            pass
    except (OSError, ValueError):
        logger.debug("browser view reveal skipped for %s", session, exc_info=True)
        return False
    finally:
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
    return True


def open_url(url: str, session_key: str) -> LaunchResult:
    """Show *url* in the chat slot's browser session, starting the browser if needed.

    ``open`` runs only when the CLI's own ``list`` says the session's browser is
    not up, because an ``open`` on a live session replaces it and discards its
    tabs. Everything else -- including a ``list`` that cannot be read -- goes
    through ``goto``, whose failure is reported in the CLI's own words rather
    than retried with an ``open``.
    """
    session = session_name(session_key)
    cli = cli_path()
    command = cli_command(cli) if cli is not None else None
    if command is None:
        return LaunchResult(False, session, "playwright-cli is not installed")
    env = _launch_env(session)
    with _session_lock(session):
        if _session_is_open(command, session, env) is False:
            rc, out, err = _run_cli(_open_argv(command, session, url), env, _OPEN_TIMEOUT_S)
            # ``open`` is "start the daemon, then goto", and the daemon can be
            # up whatever the exit code says: the CLI reports a failed first
            # navigation with rc 1 and leaves the browser running (only a
            # thrown error stops it), and a timeout kills the CLI, not the
            # detached daemon it started. Record the session on every attempt
            # so shutdown reaps it instead of leaving it for a retry that may
            # never come; closing a session that never came up is a no-op.
            with _lock:
                _opened.add(session)
        else:
            rc, out, err = _run_cli(_goto_argv(command, session, url), env, _GOTO_TIMEOUT_S)
        if rc != 0:
            detail = _error_text(rc, out, err)
            logger.warning("browser launcher failed for session %s (rc=%d)", session, rc)
            return LaunchResult(False, session, detail)
        with _lock:
            _opened.add(session)
    attached = _reveal(session, env)
    return LaunchResult(True, session, None, attached=attached)


def _own_open_sessions(command: list[str], env: dict[str, str]) -> list[str] | None:
    """Names under this gateway's prefix whose browser the CLI lists as up.

    ``None`` when the list could not be read (same contract as
    :func:`_session_is_open`).
    """
    rc, out, _err = _run_cli(_list_argv(command), env, _LIST_TIMEOUT_S)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    browsers = data.get("browsers") if isinstance(data, dict) else None
    if not isinstance(browsers, list):
        return None
    prefix = owner_prefix()
    return sorted(
        str(entry["name"])
        for entry in browsers
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"].startswith(prefix)
        and entry.get("status") == "open"
    )


def reclaim_stranded() -> int:
    """Close this gateway's ``panel-`` sessions left over from a previous life.

    For gateway startup. A clean shutdown closes them (:func:`close_all`); a
    gateway that died uncleanly could not, and its daemons kept a logged-in
    browser alive with nobody owning it. The owner tag in the name is what makes
    this safe to do from a registry other gateways share: only sessions carrying
    THIS gateway's tag are touched, so a sibling's browsers on the same host are
    never closed -- and only ones this life has not recorded, so a launch that
    beat the sweep to its slot keeps its page. Best-effort; answers how many
    were closed. Blocks on subprocesses; the startup hook offloads it.
    """
    cli = cli_path()
    command = cli_command(cli) if cli is not None else None
    if command is None:
        return 0
    stranded = _own_open_sessions(command, _launch_env(""))
    if not stranded:
        return 0
    closed = 0
    for session in stranded:
        # Under the slot's own lock: a launch for the same slot that is mid-`goto`
        # holds it and has not recorded the session yet, so without the lock the
        # close could land on the page it is adopting. Same order as open_url
        # (slot lock, then the module lock), so the two cannot deadlock.
        with _session_lock(session):
            with _lock:
                if session in _opened:
                    continue
            rc, _out, err = _run_cli(
                _close_argv(command, session), _launch_env(session), _CLOSE_TIMEOUT_S
            )
        if rc == 0:
            closed += 1
        else:
            logger.debug(
                "reclaiming browser session %s failed (rc=%d): %s", session, rc, err.strip()
            )
    if closed:
        logger.info("closed %d browser session(s) stranded by a previous gateway life", closed)
    return closed


def close_all() -> None:
    """Close every session this gateway opened. Best-effort; for shutdown.

    Scoped to OUR sessions, never a ``close-all``/``kill-all``: those would take
    an operator's own independently opened browser down with ours.
    """
    with _lock:
        sessions = sorted(_opened)
        _opened.clear()
    if not sessions:
        return
    cli = cli_path()
    command = cli_command(cli) if cli is not None else None
    if command is None:
        return
    for session in sessions:
        rc, _out, err = _run_cli(
            _close_argv(command, session), _launch_env(session), _CLOSE_TIMEOUT_S
        )
        if rc != 0:
            logger.debug("closing browser session %s failed (rc=%d): %s", session, rc, err.strip())
