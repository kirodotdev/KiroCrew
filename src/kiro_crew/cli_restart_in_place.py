"""``kirocrew restart`` of a supervised POSIX gateway: the gateway restarts itself in place.

A foreground gateway may be the child of a supervisor (a shell-script loop, a
runit-style service, any parent that starts it again when it exits). Stopping it and
starting a detached replacement hands the gateway to nobody: the supervisor sees its
child exit 0 and stops watching. So :func:`restart_in_place` first asks the gateway
to restart itself, through the restart mode of its host-only
``POST /api/restart/when-idle``. The gateway decides: a supervised one re-execs its
own command line with ``os.execv``, keeps its pid, and stays the supervisor's child.
Every other gateway (one in a terminal, a detached one, one a service unit launched)
refuses with ``403 restart_in_place_unsupervised``, and the refusal falls back
silently to the stop and spawn, whose output and exit code are unchanged.

The pid and the run marker's start token survive the exec, so they cannot tell the
new gateway from the old one. Every boot mints a fresh local secret and writes it
after it binds, so the new generation is the one whose secret differs from the
secret the request carried and whose ``/api/ready`` answers 200, from the gateway
the run marker names wherever the port's listener can be looked up.

Windows keeps the stop-and-spawn path (its ``execv`` starts a new process), and so
does every gateway that refuses or cannot answer the request: an older build, a
gateway the ownership proof refuses, a wedged one. Once the gateway has accepted,
nothing here starts another: a supervisor may be the one starting it.

Every name shared with :mod:`kiro_crew.cli_server_restart` is read from it at call
time, so a patch through the ``kiro_crew.cli_server`` facade reaches this module too.
"""

from __future__ import annotations

import http.client
import json
import sys
import urllib.error
import urllib.request

from kiro_crew import cli_server_restart as _restart_cli
from kiro_crew import platform_compat
from kiro_crew.terminal_safe import safe_terminal_line

#: Seconds the restart request may take to be answered; the restart itself runs after.
_ASK_TIMEOUT_SECS = 15

#: The request body: the gate's restart mode, alone.
_RESTART_BODY = b'{"restart":true}'


def restart_in_place(port: int, *, headless: bool | None) -> bool:
    """Restart the gateway on *port* in place; ``False`` when the caller must stop and spawn.

    ``True`` once the new generation serves, after the success line and the access
    lines (*headless* is the mode read before the restart, used when the new
    generation's own cannot be read). Exits 1 when the gateway accepted the restart
    but no new generation answered within the budget.
    """
    if not platform_compat.IS_POSIX:
        return False
    secret = _restart_cli.read_local_secret(port, dial_host=_restart_cli._CLI_LOOPBACK)
    if not secret:
        return False
    prior_pid = _restart_cli.run_marker.read_pid(port)
    if prior_pid is not None and _restart_cli._parent_pid() == prior_pid:
        # The gateway itself started this command (Dev Fleet's foreground restart
        # does), and such a caller tells the restart finished by a new gateway
        # pid, which an in-place exec keeps. Stop and spawn gives it the new pid
        # it waits for.
        return False
    if not _ask_restart(port, secret):
        return False
    served, waited = _wait_new_generation(port, secret, prior_pid)
    pid = _restart_cli.run_marker.read_pid(port) or prior_pid
    # A run marker that names no pid, before or after, leaves the pid out.
    pid_field = f" pid={pid}" if pid is not None else ""
    if not served:
        _restart_cli.sel().log_api_access(
            caller="cli",
            operation="gateway_restart",
            outcome="denied",
            source="cli",
            resources=f"port={port} via=in_place{pid_field} reason=in_place_not_back_within={waited}s",
        )
        print(
            f"❌ The gateway on port {port} accepted the restart but did not come back "
            f"within {waited}s.\n"
            "   It may still be starting, or its restart stopped before the exec.\n"
            "   To follow it, run: kirocrew logs -f  (or check: kirocrew status)"
        )
        sys.exit(1)
    _restart_cli.sel().log_api_access(
        caller="cli",
        operation="gateway_restart",
        outcome="allowed",
        source="cli",
        resources=f"port={port} via=in_place{pid_field}",
    )
    pid_text = f" (pid {pid})" if pid is not None else ""
    print(f"✅ Restarted the gateway in place{pid_text}. Logs: kirocrew logs -f")
    from kiro_crew.cli_restart_headless import live_gateway_headless
    from kiro_crew.dashboard.headless import print_client_access

    now_headless = live_gateway_headless(port)
    if now_headless if now_headless is not None else headless:
        print_client_access(port)
    else:
        _restart_cli._print_token_url(port)
    return True


def _ask_restart(port: int, secret: str) -> bool:
    """Ask the gateway to restart in place; ``True`` only when it accepted.

    The gateway decides whether it restarts in place: only a supervised one
    accepts. A refusal the gateway explains (409) is printed in one line. Every
    other answer, and no answer, is ``False`` without a word, the 403 an
    unsupervised gateway answers included: the stop-and-spawn path that follows
    is the restart this command always made.
    """
    url = f"http://{_restart_cli._CLI_LOOPBACK}:{port}/api/restart/when-idle"
    headers = {"Content-Type": "application/json", "X-Internal-Secret": secret}
    req = urllib.request.Request(url, data=_RESTART_BODY, headers=headers, method="POST")
    try:
        # The local secret goes only to a listener proven to be this install's
        # own gateway, the same send helper the idle check uses.
        with _restart_cli._owner_verified_secret_urlopen(
            req, port, timeout=_ASK_TIMEOUT_SECS, operation="gateway_restart_in_place"
        ) as resp:
            body = json.loads(resp.read())
    except _restart_cli._GatewayPeerRefused:
        return False
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print(
                f"ℹ️ The gateway did not restart in place ({_refusal_reason(e)}); "
                "stopping it and starting a new one."
            )
        return False
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        return False
    return isinstance(body, dict) and body.get("ok") is True and body.get("status") == "restarting"


def _refusal_reason(error: urllib.error.HTTPError) -> str:
    """The gateway's own sentence for a 409, as one terminal-safe line without its final period.

    The line wraps it in parentheses, where the period would read ``(....);``.
    """
    try:
        body = json.loads(error.read())
    except Exception:
        body = None
    sentence = body.get("error") if isinstance(body, dict) else None
    if not isinstance(sentence, str) or not sentence.strip():
        return f"HTTP {error.code}"
    line = safe_terminal_line(sentence).rstrip()
    return line[:-1] if line.endswith(".") and len(line) > 1 else line


def _new_generation_serves(port: int, old_secret: str) -> bool:
    """Whether a gateway booted since the request serves *port*.

    The fresh secret proves a gateway booted and bound the port; the 200 alone does
    not prove that gateway sent it. One that died after writing its secret leaves the
    port to whatever binds it next. So where the listener can be looked up, the
    process the run marker names must hold the port, the check the stop-and-spawn
    path makes (:func:`kiro_crew.cli_server_restart._replacement_is_serving`).
    """
    secret = _restart_cli.read_local_secret(port, dial_host=_restart_cli._CLI_LOOPBACK)
    if not secret or secret == old_secret:
        return False
    if _restart_cli._probe_gateway_ready(port) != 200:
        return False
    if platform_compat.IS_POSIX and platform_compat.listening_pid_tool_available():
        return _restart_cli._gateway_owns_port(port)
    return True


def _wait_new_generation(port: int, old_secret: str, prior_pid: int | None) -> tuple[bool, int]:
    """Poll for the new generation; ``(served, seconds waited)``.

    The budget is the stop-and-spawn path's own: the teardown, then the boot. The
    loop is the one that path waits on (:func:`kiro_crew.cli_server_restart._poll_until_serving`),
    so the "still starting" line and the boot-time update-apply extension are its
    own; the apply is asked about the pid the run marker records, which the exec
    keeps.
    """
    ready_timeout = _restart_cli._resolve_restart_ready_timeout()
    outcome, _status, waited = _restart_cli._poll_until_serving(
        lambda: _new_generation_serves(port, old_secret),
        budget=_restart_cli._RESTART_STOP_TIMEOUT + ready_timeout,
        extension=ready_timeout,
        apply_pid=lambda: _restart_cli.run_marker.read_pid(port) or prior_pid,
    )
    return outcome == _restart_cli._POLL_SERVED, waited
