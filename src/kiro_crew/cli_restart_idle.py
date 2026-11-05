"""``kirocrew restart --when-idle``: restart the gateway only once nothing is running.

Asks the running gateway's ``POST /api/restart/when-idle`` whether a restart now
would interrupt work: chat turns, subagents, workflows, the task runner, channel
handlers. That is the census the gateway's own scheduled update waits on
(``GatewayOrchestrator._in_flight_work_counts``). While the gateway is busy this
waits and asks again, up to ``--max-wait``; once it answers idle, the gateway
keeps new turns paused and this runs the ordinary :func:`_restart`.

The Remote Crew "Update" and "Restart" actions run ``restart --when-idle
--max-wait 0`` on the remote over SSH or SSM, one check per call, and read the
outcome from the exit status and one marker line (on stderr and last on
stdout): :data:`EXIT_BUSY`
with :data:`BUSY_MARKER` and the counts, or :data:`EXIT_GATE_UNSUPPORTED` with
:data:`UNSUPPORTED_MARKER` when the running gateway predates the idle check, or
:data:`EXIT_GATE_REFUSED` with :data:`REFUSED_MARKER` when idleness could not
be established. None of them restarts anything. A plain ``kirocrew restart`` is unchanged.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import NoReturn

from kiro_crew.cli_server_peer import (
    _CLI_LOOPBACK,
    _GatewayPeerRefused,
    _owner_verified_secret_urlopen,
)
from kiro_crew.config.loader import read_local_secret
from kiro_crew.loopback_http import deadline_expired
from kiro_crew.port_resolution import resolve_client_port

#: Default ``--max-wait``. The same ceiling the gateway gives a mandatory update
#: it holds back for in-flight work (``_MANDATORY_UPDATE_MAX_DEFER_SECS``); a test
#: pins the two equal.
DEFAULT_MAX_WAIT_SECS = 600.0

#: How often a busy gateway is asked again.
POLL_SECS = 10.0

#: Exit status when the gateway is still busy at the end of the wait
#: (``EX_TEMPFAIL``): nothing was restarted, and a later try may succeed.
EXIT_BUSY = 75

#: Exit status when the running gateway has no idle check to ask
#: (``EX_UNAVAILABLE``): nothing was restarted.
EXIT_GATE_UNSUPPORTED = 69

#: The prefix of the line a busy exit prints, on stderr and last on stdout, for
#: a caller on another machine (see :func:`busy_marker`).
BUSY_MARKER = "kirocrew-restart-busy"

#: Exit status when the command could not establish that the gateway is idle
#: (``EX_CONFIG``): the check failed, or what a restart would reach is wider
#: than what was checked. Nothing was restarted, and only a forced restart
#: (plain ``kirocrew restart``) gets past it.
EXIT_GATE_REFUSED = 78

#: The line a refused exit prints, the same way as the busy one.
REFUSED_MARKER = "kirocrew-restart-gate-refused"

#: The line an unsupported exit prints, the same way.
UNSUPPORTED_MARKER = "kirocrew-restart-gate-unsupported"

#: Error codes the gateway's own handler answers with. Any other refusal comes
#: from a gateway that has no such route.
_GATE_REFUSAL_CODES = frozenset({"restart_gate_not_local"})

# The clock and the sleep, named so a test can drive the wait without sleeping.
_now = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True)
class _Verdict:
    """What the gateway said: ``idle``, ``busy``, ``unsupported`` or ``down``."""

    state: str
    turns: int = 0
    background: int = 0
    #: Whether the gateway that answered was launched by the service definition.
    service_managed: bool = False


class _GateFailed(Exception):
    """The idle check could not be answered, so nothing may restart."""


def _ask_gate(port: int, *, service_only: bool = False) -> _Verdict:
    """One ``POST /api/restart/when-idle`` against the gateway on *port*.

    *service_only*: the restart will go to the kirocrew service, so a gateway
    the service did not launch answers without pausing its admission.
    """
    url = f"http://{_CLI_LOOPBACK}:{port}/api/restart/when-idle"
    headers = {"Content-Type": "application/json"}
    secret = read_local_secret(port, dial_host=_CLI_LOOPBACK)
    if secret:
        headers["X-Internal-Secret"] = secret
    data = b'{"service_managed_only":true}' if service_only else b"{}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        # The local secret goes only to a listener proven to be this install's
        # own gateway, the same send helper `kirocrew update approve` uses.
        with _owner_verified_secret_urlopen(
            req, port, timeout=15, operation="gateway_restart_gate"
        ) as resp:
            body = json.loads(resp.read())
    except _GatewayPeerRefused as e:
        raise _GateFailed(f"gateway ownership could not be verified on port {port}: {e}") from e
    except urllib.error.HTTPError as e:
        try:
            code = str(json.loads(e.read()).get("code") or "")
        except Exception:
            code = ""
        if code in _GATE_REFUSAL_CODES:
            raise _GateFailed(f"the gateway refused the idle check (HTTP {e.code}, {code})") from e
        if e.code in (401, 403, 404, 405, 503):
            # No idle route on this gateway: an older build, or one that runs
            # no orchestrator. Its answer to an unknown path is an auth refusal
            # or a 404, depending on its middleware.
            return _Verdict("unsupported")
        raise _GateFailed(f"the gateway answered the idle check with HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        reason = e.reason if isinstance(e, urllib.error.URLError) else e
        if isinstance(reason, ConnectionRefusedError):
            # Nothing listens: no work can be in flight, and the ordinary
            # restart decides what starting it means.
            return _Verdict("down")
        if deadline_expired(e):
            raise _GateFailed(f"the gateway on port {port} did not answer the idle check") from e
        # A reset or any other I/O failure may come from a live gateway, so it
        # never stands in for "nothing is running".
        raise _GateFailed(f"the idle check on port {port} failed: {type(reason).__name__}") from e
    if not isinstance(body, dict):
        raise _GateFailed("the gateway's idle check answered with an unreadable body")
    turns = body.get("turns")
    background = body.get("background")
    if not isinstance(turns, int) or not isinstance(background, int):
        raise _GateFailed("the gateway's idle check answered without its counts")
    return _Verdict(
        "idle" if body.get("idle") is True else "busy",
        turns,
        background,
        service_managed=body.get("service_managed") is True,
    )


def _service_active() -> bool:
    """Whether ``restart_service`` would restart a kirocrew service here.

    The same predicate it gates on, per platform: ``linux.is_active()``, and on
    macOS ``macos.is_loaded()``, which searches both launchd domains. The
    ``launchctl list`` behind ``is_service_active()`` reads only the caller's
    domain and misses a gui/<uid> agent from SSH, which is how the Remote Crew
    actions run this command.
    """
    from kiro_crew.service import linux, macos
    from kiro_crew.service.common import Platform, current_platform

    plat = current_platform()
    if plat == Platform.SYSTEMD:
        return bool(linux.is_active())
    if plat == Platform.LAUNCHD:
        return bool(macos.is_loaded())
    return False


def _service_scopes_running() -> int:
    """How many systemd scopes run a kirocrew unit; 0 off Linux or with none.

    A service restart restarts every one of them, and the idle check asked one
    gateway only.
    """
    from kiro_crew.service import linux
    from kiro_crew.service.common import Platform, current_platform

    if current_platform() != Platform.SYSTEMD:
        return 0
    return linux.running_scope_count()


def busy_marker(turns: int, background: int) -> str:
    """The busy line: :data:`BUSY_MARKER`, a space, and the counts as one JSON object.

    ``kirocrew-restart-busy {"turns":2,"background":1}``. The reader checks the
    exact prefix and ``json.loads`` the rest, so the line needs no pattern.
    """
    counts = json.dumps({"turns": turns, "background": background}, separators=(",", ":"))
    return f"{BUSY_MARKER} {counts}"


def _print_marker(line: str) -> None:
    """Print *line* on stderr and as the LAST stdout line.

    A remote caller may see only the head of stderr (an SSH login banner can
    fill it) and only the tail of stdout, so the marker goes where each keeps it.
    """
    print(line, file=sys.stderr)
    print(line, flush=True)


def _refuse(reason: str) -> NoReturn:
    """Exit :data:`EXIT_GATE_REFUSED` without restarting: idleness could not be established."""
    print(f"❌ Not restarted: {reason}")
    _print_marker(REFUSED_MARKER)
    sys.exit(EXIT_GATE_REFUSED)


def _describe(verdict: _Verdict) -> str:
    return f"{verdict.turns} chat turn(s) and {verdict.background} background task(s)"


def _restart_when_idle(cli_port: int | None, *, max_wait_secs: float | None = None) -> None:
    """Restart once the gateway reports nothing in flight, waiting up to *max_wait_secs*.

    Exits :data:`EXIT_BUSY` if it is still busy then, :data:`EXIT_GATE_UNSUPPORTED`
    if it has no idle check, and :data:`EXIT_GATE_REFUSED` if it could not tell
    whether the gateway is idle; none of those restarts anything.
    """
    port = resolve_client_port(cli_port)
    if cli_port is None and _service_scopes_running() > 1:
        # ``restart_service`` restarts the unit in every scope that runs it,
        # and the idle check can ask only the gateway on one port. Refused
        # before asking, so no gateway's admission is paused for nothing.
        _refuse(
            "kirocrew service units run in both the system and the user scope, and "
            f"--when-idle can check only one gateway (port {port}). Restart one with "
            "--port, or run `kirocrew restart` to restart both now."
        )
    wait = DEFAULT_MAX_WAIT_SECS if max_wait_secs is None else max(0.0, max_wait_secs)
    deadline = _now() + wait
    while True:
        # Without --port the restart below goes to the running service, so only
        # the service's own gateway may answer for it. Asked as part of the
        # request, so a gateway that is not the service's answers without
        # pausing its admission for a restart that will not reach it.
        service_only = cli_port is None and _service_active()
        try:
            verdict = _ask_gate(port, service_only=service_only)
        except _GateFailed as e:
            _refuse(f"{e}.")
        if verdict.state == "down" and service_only:
            # The restart below would go to the running service, and the port
            # that refused may not be the one that service serves (a changed
            # dashboard.url, say), so nothing proves that gateway is idle.
            _refuse(
                f"nothing answered the idle check on port {port}, but a kirocrew service "
                "is running. Pass --port for the gateway you mean, or run "
                "`kirocrew restart` to restart the service now."
            )
        if service_only and verdict.state in ("idle", "busy") and not verdict.service_managed:
            # The gateway that answered says no service launched it (another
            # data home, say), so its answer is not about the service the
            # restart below would reach.
            _refuse(
                f"the gateway on port {port} is not the running kirocrew "
                "service's gateway, and without --port the restart would go to the "
                "service. Pass --port for the gateway you mean, or run `kirocrew restart` "
                "to restart the service now."
            )
        if verdict.state in ("idle", "down"):
            break
        if verdict.state == "unsupported":
            print(
                "❌ Not restarted: the running gateway cannot report its in-flight work "
                "(it runs an older version). Restart it from its own dashboard, or run "
                "`kirocrew restart` to restart it now and stop whatever is running."
            )
            _print_marker(UNSUPPORTED_MARKER)
            sys.exit(EXIT_GATE_UNSUPPORTED)
        remaining = deadline - _now()
        if remaining <= 0:
            print(
                f"❌ Not restarted: the gateway is still running {_describe(verdict)}. "
                "Try again later, or run `kirocrew restart` to restart it now and stop that work."
            )
            _print_marker(busy_marker(verdict.turns, verdict.background))
            sys.exit(EXIT_BUSY)
        pause = min(POLL_SECS, remaining)
        print(f"⏳ The gateway is running {_describe(verdict)}; checking again in {pause:.0f}s.")
        _sleep(pause)
    # Through the facade, so a patch of ``kiro_crew.cli_server._restart`` reaches it.
    from kiro_crew.cli_server import _restart

    _restart(cli_port)
