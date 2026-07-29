"""CLI server lifecycle commands — update, stop, token, logout, status, gateway, run."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew import __version__, platform_compat
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    _DEFAULT_PORT,
    _session_work_dir,
    build_provider_factory,
    config_dir,
    config_path,
)
from kiro_crew.constants import DATA_WARNING
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers.core import DASHBOARD_HTML_NOT_FOUND_MARKER
from kiro_crew.dashboard.origin import (
    dashboard_origin,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_crew.dashboard.token_auth import parse_duration
from kiro_crew.embeddings import make_sync_embed_fn, model_file_present
from kiro_crew.env import activate_mise
from kiro_crew.frontend import build_frontend_sync, ensure_dev_dist_symlink
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import HookManager, hooks_config_from_config_dict
from kiro_crew.instances import run_marker
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.preflight import run_preflight_checks
from kiro_crew.sel import sel
from kiro_crew.service import controller as service_controller
from kiro_crew.service import linux as svc_linux
from kiro_crew.service import macos as svc_macos
from kiro_crew.service.common import SERVICE_NAME, Platform, current_platform
from kiro_crew.session import SessionManager
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.gateway import run_gateway
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.vector_memory import VectorMemoryStore

# Loopback address used for the CLI's OWN requests to the gateway. Deliberately
# the literal IPv4 address, never the name ``localhost``: on a dual-stack host
# ``localhost`` may resolve to ``::1`` first, so a different local user who binds
# ``[::1]:<port>`` beside the real IPv4 gateway would receive requests carrying
# ``X-Local-Secret`` — and the listener verification in _gateway_owns_port is
# address-agnostic (``lsof -ti TCP:<port>`` cannot tell the two sockets apart),
# so it would still see the genuine gateway and pass. Pinning the address binds
# the request to the endpoint we actually verified.
#
# This is ONLY for CLI->gateway requests. The URL *printed* for the browser
# stays ``resolve_dashboard_host()`` (``localhost``), which must not change: the
# SPA's per-origin localStorage is keyed on that host, so emitting a different
# origin would make every dashboard setting appear reset.
_CLI_LOOPBACK = "127.0.0.1"


def _config_url_port() -> int | None:
    """Port explicitly named by ``dashboard.url``, or ``None``.

    Distinct from :func:`parse_dashboard_url`, which substitutes
    ``_DEFAULT_PORT`` for a portless URL (``http://my.host``). That substitution
    is right for the *server* (it must bind something) but wrong for a client:
    it would report "config says 5476" and short-circuit the run-marker
    fallback below, even though the user never named a port. So detect the
    explicit case and let a portless URL fall through.
    """
    try:
        cfg = KiroCrewConfig.load()
        url = cfg.dashboard.url or ""
    except Exception:
        # Config load failures must not break client commands.
        return None
    if not isinstance(url, str):
        # ``dashboard.url`` is user-editable JSON and core installs may lack
        # jsonschema, so the value can be any type (``"url": 123``). urlparse
        # raises TypeError on a non-str, which is NOT a ValueError — without
        # this guard a bad config type would crash every client command.
        logging.getLogger(__name__).warning(
            "Ignoring non-string dashboard.url of type %s", type(url).__name__
        )
        return None
    if not url:
        return None
    try:
        _, port = parse_dashboard_url(url)
        # parse_dashboard_url already normalises the scheme, tolerates malformed
        # URLs and applies the KIROCREW_PORT override; re-split only to learn
        # whether the port was written down or defaulted in.
        explicit = urllib.parse.urlsplit(url if "://" in url else f"http://{url}").port
    except (TypeError, ValueError):
        return None
    return port if explicit is not None else None


def _gateway_owns_port(port: int) -> bool:
    """True only when *this user's* gateway process is listening on *port*.

    Reachability is not enough to trust a discovered port. Client commands hand
    the local secret to whatever answers (``_token`` and ``_logout`` send
    ``X-Local-Secret``), and ``clear_marker`` runs only on graceful shutdown —
    so a crashed gateway leaves a marker naming a port some unrelated process
    may since have bound. A bare TCP connect would walk the secret straight into
    that process, which could then mint owner tokens against the real gateway.

    A command-line check is not enough either: argv is attacker-chosen, so a
    listener launched as ``/tmp/kirocrew gateway`` would pass it. The proof used
    here is an identity the attacker cannot forge, in three parts:

    1. **Recorded pid** — ``run_marker.read_pid(port)`` reads the sidecar the
       gateway wrote at ``0600`` inside the ``0700`` ``run/`` dir. Another local
       user cannot write it, so they cannot nominate a process of theirs.
    2. **Holds the port** — that pid must be among
       ``platform_compat.find_listening_pids(port)``. This is what makes a stale
       recorded pid harmless: it has to actually hold the port we are about to
       send the secret to.
    3. **Owned by us, and ours** — the pid's uid must equal the caller's
       (``process_owner_uid``), and its argv must look like a gateway. The uid
       check is what closes pid *recycling* into a foreign user's process; argv
       remains only as defense in depth, never as the sole proof.

    A same-user attacker is out of scope by construction: they can already read
    ``.local_secret`` (mode ``0600``, their own uid), so nothing here can be an
    escalation for them. The boundary this closes is a *different* local user.

    **Fails closed** at every step: no sidecar, no recorded pid, a pid that does
    not hold the port, an unresolvable uid, a missing lookup tool
    (``find_listening_pids`` folds that into an empty list) or a throwing one —
    all deny, and discovery is skipped in favour of the documented default.
    ``--port`` and ``KIROCREW_PORT`` remain available on such hosts.

    **Non-POSIX denies outright.** ``process_owner_uid`` cannot report an owner
    on Windows, and a home that is writable by another user (a shared or
    misconfigured ``KIROCREW_HOME``) would let them replace both the marker and
    the sidecar with a forged listener — the file-permission argument that
    carries step 1 is exactly what stops holding there. Rather than trust
    steps 1-2 alone, discovery is skipped: Windows users keep ``--port`` /
    ``KIROCREW_PORT``, which is precisely where they were before this fallback
    existed, so nothing regresses. This is the one place the feature is
    deliberately unavailable rather than approximated.
    """
    if not platform_compat.IS_POSIX:
        return False
    recorded = run_marker.read_pid(port)
    if recorded is None:
        return False
    try:
        pids = platform_compat.find_listening_pids(port)
    except Exception:
        return False
    if recorded not in pids:
        return False
    owner = platform_compat.process_owner_uid(recorded)
    if owner is None or owner != os.getuid():
        return False
    return _is_kirocrew_process(recorded)


def _marker_port() -> int | None:
    """Port of the sole gateway-owned run-marker, or ``None``.

    Zero-configuration discovery for the common single-gateway box: the gateway
    already advertises itself by writing ``<data-home>/run/gateway-<port>.bin``
    (see :mod:`kiro_crew.instances.run_marker`), so a client with no ``--port``,
    no ``KIROCREW_PORT`` and no port in ``dashboard.url`` can read that instead
    of assuming 5476 and connecting to a dead port.

    Two guards keep this from being a guess:

    * **Ownership.** Only ports where a verified KiroCrew gateway process is
      listening count (:func:`_gateway_owns_port`); a stale marker, or one whose
      port has been taken over by an unrelated process, is discarded.
    * **Ambiguity.** With several gateways up there is no basis to pick one, so
      this refuses (returns ``None``, landing on the documented default) and
      tells the user on stderr which ports it saw and how to name one.
    """
    try:
        candidates = run_marker.marker_ports()
    except Exception:
        return None
    if not candidates:
        return None
    owned = [p for p in candidates if _gateway_owns_port(p)]
    if len(owned) == 1:
        return owned[0]
    if len(owned) > 1:
        print(
            f"⚠️  Multiple gateways are running (ports {', '.join(str(p) for p in owned)}); "
            f"not guessing which one you meant — using {_DEFAULT_PORT}. "
            "Pass --port or set KIROCREW_PORT to target a specific gateway.",
            file=sys.stderr,
        )
    return None


def resolve_client_port(cli_port: int | None) -> int:
    """Return the dashboard port a *client* CLI command (token/status/logout/stop)
    should talk to.

    Resolution order:

    1. Explicit ``--port`` CLI flag if the user passed one (``cli_port`` is not ``None``).
    2. ``KIROCREW_PORT`` env var if set to a valid integer.
    3. Port explicitly named by ``dashboard.url`` in the config file
       (``<data-home>/config.json``), when it parses.
    4. The sole gateway-owned run-marker (``<data-home>/run/gateway-<port>.bin``)
       — see :func:`_marker_port`. Skipped when no marker's port is held by a
       verified gateway process, and refused (with a stderr hint) when several
       are.
    5. ``_DEFAULT_PORT`` (5476) as the final fallback.

    Steps 1-3 match the server-side ``parse_dashboard_url()`` logic so that
    ``kirocrew token`` / ``status`` / ``logout`` / ``stop`` all hit the same
    port the gateway is actually bound to when the user has configured a
    non-default ``dashboard.url`` (for example a dev instance on 6777 or an
    alternative prod port like 7778). Step 4 covers the case where nothing was
    configured at all but a gateway is up on a non-default port (e.g. started
    with ``kirocrew gateway --port 6776``): the running gateway's own marker is
    better evidence than the 5476 default.
    """
    if cli_port is not None:
        return cli_port
    env_port = os.environ.get("KIROCREW_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            # Fall through to config/marker/default — main() validates this
            # early, but guard here too in case the helper is reached via
            # another path.
            pass
    cfg_port = _config_url_port()
    if cfg_port:
        return cfg_port
    discovered = _marker_port()
    if discovered:
        return discovered
    return _DEFAULT_PORT


def _probe_dashboard_health(port: int) -> None:
    """Warn on stderr if the gateway is serving a stale dashboard.

    Best-effort: a cookieless GET / checks the response body for the
    "Dashboard HTML not found" marker that a stale gateway serves when its
    static assets have been pruned (e.g. by an update). If detected, a warning
    is printed to stderr so callers know the token won't yield a working
    dashboard. Network errors are silently ignored.
    """
    try:
        req = urllib.request.Request(f"http://{_CLI_LOOPBACK}:{port}/", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:  # nosemgrep
            body = resp.read(8192).decode("utf-8", errors="replace")
            if DASHBOARD_HTML_NOT_FOUND_MARKER.lower() in body.lower():
                print(
                    "⚠️  Warning: gateway is serving a stale dashboard "
                    "(assets missing — likely an update pruned the "
                    "running install). Restart the gateway to fix.",
                    file=sys.stderr,
                )
    except Exception:
        pass


def _token(args: argparse.Namespace) -> None:
    """Print a dashboard URL with a fresh auth token.

    Diagnostics discipline: **stdout carries only the URL(s)**; every failure
    reason goes to **stderr**. stdout here is a parsed machine interface — the
    remote-mint path (:func:`kiro_crew.instances.token_mint.mint_remote_token`)
    runs this over SSH and regex-extracts the JWT from stdout, so mixing error
    prose into stdout both violates the Unix convention and hides the reason
    from any caller that only captures stderr (which is how a failed remote
    mint used to surface as a useless ``<no stderr>``).
    """
    # Seam-supplied pre-launch checks (CPP IdentityProvider seam) — e.g. a
    # companion SSO-session freshness prompt before minting a token. Public
    # default = no checks; see kiro_crew.preflight.
    run_preflight_checks()

    ttl = parse_duration(args.ttl)
    if ttl is None:
        print(f"❌ Invalid TTL: {args.ttl} (use e.g. 1h, 30m)", file=sys.stderr)
        sys.exit(1)

    port = resolve_client_port(args.port)
    secret_path = config_dir() / ".local_secret"
    try:
        secret = secret_path.read_text().strip()
    except FileNotFoundError:
        print("❌ Gateway not running — start it with: kirocrew gateway", file=sys.stderr)
        sys.exit(1)

    url = f"http://{_CLI_LOOPBACK}:{port}/api/token/local?ttl={args.ttl}"
    epp = getattr(args, "embed_parent_port", None)
    if epp:
        url += f"&embed_parent_port={int(epp)}"
    req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            token = data.get("token", "")
    except Exception as exc:
        print(f"❌ Could not reach gateway on port {port}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not token:
        print("❌ Gateway returned empty token", file=sys.stderr)
        sys.exit(1)
    _probe_dashboard_health(port)

    # Print the SAME canonical loopback host the gateway uses for its auto-open
    # and !dashboard links. resolve_dashboard_host() returns "localhost" for the
    # loopback case — it resolves in every browser and through SSH tunnels (unlike
    # *.localhost names, which Safari / the macOS resolver do not map). Emitting a
    # host the gateway does NOT serve on would land the browser on a different
    # origin, splitting the SPA's per-origin localStorage so all dashboard
    # settings appear reset. Keeping the host consistent avoids that.
    host = resolve_dashboard_host(local_only=True)
    print(f"http://{host}:{port}?token={token}")
    origin = dashboard_origin(KiroCrewConfig.load().dashboard.url)
    if origin and "localhost" not in origin:
        print()
        print(f"{origin}/?token={token}")


def _logout(port: int) -> None:
    """Revoke all dashboard sessions by calling the gateway's /api/logout endpoint."""
    secret_path = config_dir() / ".local_secret"
    try:
        secret = secret_path.read_text().strip()
    except FileNotFoundError:
        print("❌ Gateway not running — start it with: kirocrew gateway")
        sys.exit(1)

    url = f"http://{_CLI_LOOPBACK}:{port}/api/logout"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"X-Local-Secret": secret, "Content-Type": "application/json"},
        data=b"{}",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                print("✅ All dashboard sessions revoked.")
            else:
                print(f"❌ Failed to revoke sessions: {data.get('error', 'unknown error')}")
                sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"❌ Failed to revoke sessions: HTTP {e.code}")
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        print("❌ Gateway not running — start it with: kirocrew gateway")
        sys.exit(1)


def _stop(cli_port: int | None = None) -> None:
    """Stop a running KiroCrew gateway.

    Accepts the raw CLI ``--port`` value (``None`` when not passed).
    Resolution and service-bypass are both derived from this single input:

    - ``cli_port is None``: user didn't pass ``--port``, so we resolve via
      env/config/default AND try the systemd/launchd service first.
    - ``cli_port is not None``: user explicitly targeted a port, so we
      bypass the service short-circuit and SIGTERM the gateway bound to
      that port directly.
    """
    port = resolve_client_port(cli_port)
    if cli_port is None and service_controller.stop_service():
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="allowed",
            source="cli",
            resources=f"port={port} via=service",
        )
        print("✅ Stopped kirocrew service. To remove it: kirocrew service uninstall")
        return

    # Cross-platform port -> listening PID lookup (lsof on POSIX, netstat -ano
    # on Windows — there is no lsof there, which previously made `kirocrew stop`
    # a no-op on Windows).
    pids = platform_compat.find_listening_pids(port)

    if not pids:
        # Distinguish "lookup tool absent" from "genuinely no listener":
        # find_listening_pids folds a missing lsof into an empty list, so without
        # this a running gateway would be mis-reported as stopped (and _restart
        # would then double-spawn). Restores the pre-shim dedicated diagnostic.
        if not platform_compat.listening_pid_tool_available():
            _tool = platform_compat.listening_pid_tool()
            sel().log_api_access(
                caller="cli",
                operation="gateway_stop",
                outcome="no_target",
                source="cli",
                resources=f"port={port} reason={_tool}_not_found",
            )
            print(
                f"`{_tool}` not found — cannot look up the gateway process on "
                f"port {port}. Install {_tool} and retry."
            )
            sys.exit(1)
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="no_target",
            source="cli",
            resources=f"port={port}",
        )
        print(f"No Kiro Crew gateway currently running on port {port}.")
        sys.exit(1)

    # Only kill processes that are actually KiroCrew gateways.
    # Note: TOCTOU race exists between this check and the kill — the PID could be
    # recycled. Acceptable risk for an interactive CLI tool with low blast radius.
    pids = [p for p in pids if _is_kirocrew_process(p)]
    if not pids:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="no_target",
            source="cli",
            resources=f"port={port} reason=no_kirocrew_process",
        )
        print(f"No Kiro Crew gateway currently running on port {port}.")
        sys.exit(1)

    sent: set[int] = set()
    denied: list[int] = []
    for pid in pids:
        if platform_compat.IS_WINDOWS:
            # No POSIX signals or graceful shutdown for a detached console-less
            # gateway: kill_process_tree uses `taskkill /T /F` so the gateway's
            # detached kiro-cli / MCP-server children are reaped too (a single-PID
            # kill_pid would orphan them). kill_process_tree raises
            # ProcessLookupError / PermissionError / OSError on non-zero
            # taskkill exit — same shape POSIX uses.
            try:
                platform_compat.kill_process_tree(pid, platform_compat.SIGTERM)
                sent.add(pid)
            except ProcessLookupError:
                pass  # already gone
            except PermissionError:
                denied.append(pid)
            except OSError:
                # Generic taskkill failure — re-check liveness rather than
                # guessing whether the pid is denied vs really gone.
                if platform_compat.pid_exists(pid):
                    denied.append(pid)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            sent.add(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied.append(pid)

    # Wait briefly for processes to exit so the port is freed
    if sent:
        for _ in range(10):  # up to 1s
            time.sleep(0.1)
            if all(_pid_exited(p) for p in sent):
                break

    if sent:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="allowed",
            source="cli",
            resources=f"pids={sorted(sent)} port={port}",
        )
        _verb = "Terminated" if platform_compat.IS_WINDOWS else "Sent SIGTERM to"
        print(f"✅ {_verb} gateway (pid {', '.join(str(p) for p in sorted(sent))}).")
    if denied:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="denied",
            source="cli",
            resources=f"pids={denied} port={port}",
        )
        print(
            f"❌ No permission to stop pid {', '.join(str(p) for p in denied)} — try: sudo kirocrew stop"
        )
        sys.exit(1)
    if not sent:
        sel().log_api_access(
            caller="cli",
            operation="gateway_stop",
            outcome="no_target",
            source="cli",
            resources=f"port={port} reason=process_already_exited",
        )
        print(f"No Kiro Crew gateway currently running on port {port} (process already exited).")
        sys.exit(1)


# Subcommands that launch a long-running KiroCrew *server* process which
# ``kirocrew stop`` may need to terminate. These mirror the entry-point
# subcommands dispatched in ``cli.py`` (``gateway`` / ``dashboard``; ``start``
# is the historical alias). The task runner (``run``) is intentionally excluded:
# it is not bound to the dashboard port, so we must never SIGTERM it from
# ``kirocrew stop``.
_KIROCREW_SERVER_SUBCOMMANDS = frozenset({"gateway", "dashboard", "start"})


def _basename_stem(tok: str) -> str:
    """Basename of *tok* without a Windows ``.exe`` suffix.

    Lets the venv launchers ``python.exe`` / ``kirocrew.exe`` match the same
    checks as their POSIX ``python`` / ``kirocrew`` counterparts. ``shlex.split``
    with ``posix=False`` leaves quotes on some tokens, so strip them too.

    Split on BOTH separators explicitly rather than via ``os.path.basename``:
    that is host-dependent (``posixpath`` on Linux does NOT split backslashes),
    so a Windows cmdline classified on the Linux CI fleet would keep its full
    ``D:\\...\\kirocrew.exe`` path and never match. This is host-independent — a
    basename is whatever follows the last ``/`` or ``\\``.

    Module scope rather than nested in :func:`_args_look_like_kirocrew` so
    :func:`_own_console_script` shares the one definition.
    """
    cleaned = tok.strip('"')
    base = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base


def _args_look_like_kirocrew(args: str) -> bool:
    """Return ``True`` if a process command-line *args* string is a KiroCrew server.

    This gates ``os.kill(pid, SIGTERM)`` in :func:`_stop`, so it must be
    **precise** (never match an unrelated process that merely mentions
    "kirocrew") while still recognising *every* way the gateway can be spawned.

    Instead of enumerating brittle substring variants (``kiro_crew.gateway`` vs
    ``kiro_crew gateway`` vs ``kirocrew gateway`` …), we parse the command line
    *structurally* and key on the real module/binary name plus a known server
    subcommand (:data:`_KIROCREW_SERVER_SUBCOMMANDS`). This is deterministic and
    robust to interpreter path, Python version suffix, and whitespace. Two spawn
    shapes are recognised:

    * **Module invocation** — ``<python> -m kiro_crew <subcmd>`` (the form used by
      a service install and the launchd/systemd service), plus the legacy dotted
      form ``<python> -m kiro_crew.<subcmd>``. A Python interpreter must precede
      ``-m`` so we don't misread some other tool's ``-m`` flag (e.g. ``grep -m``).
    * **Console script** — ``/path/to/kirocrew <subcmd>`` (used when the
      ``kirocrew`` wrapper resolves on ``PATH``).

    Examples::

        >>> _args_look_like_kirocrew("/x/python3.10 -m kiro_crew gateway")
        True
        >>> _args_look_like_kirocrew("python3 -m kiro_crew.dashboard")
        True
        >>> _args_look_like_kirocrew("/usr/local/bin/kirocrew start")
        True
        >>> _args_look_like_kirocrew("python -m kiro_crew run /tmp/spec.md")  # task runner
        False
        >>> _args_look_like_kirocrew("vim /tmp/kirocrew-notes.txt")
        False
    """
    # ``ps -o args=`` (POSIX) / Win32_Process.CommandLine (Windows WMI) return a
    # shell-style string; tokenize it the way the host shell would. On Windows
    # use posix=False so backslash path separators survive (default posix=True
    # eats them: ``C:\Py\python.exe`` -> ``C:Pypython.exe``, breaking the
    # interpreter/basename checks below). Fall back to a naive split on a
    # malformed string (e.g. an odd quote) so this best-effort check never raises.
    try:
        tokens = shlex.split(args, posix=not platform_compat.IS_WINDOWS)
    except ValueError:
        tokens = args.split()

    for index, token in enumerate(tokens):
        # --- Module form: "<python> -m kiro_crew <subcmd>" / "-m kiro_crew.<subcmd>"
        if token == "-m" and index + 1 < len(tokens):
            # Only treat "-m" as Python's module flag when a Python interpreter
            # precedes it; otherwise an unrelated tool's "-m" option could be
            # misread (e.g. "grep -m kiro_crew gateway file").
            interpreter_seen = any(_basename_stem(t).startswith("python") for t in tokens[:index])
            if interpreter_seen:
                # "kiro_crew.gateway" -> ("kiro_crew", "gateway"); a bare
                # "kiro_crew" -> ("kiro_crew", "").
                package, _, dotted_subcmd = tokens[index + 1].partition(".")
                if package == "kiro_crew":
                    # Dotted submodule form: ``-m kiro_crew.gateway``.
                    if dotted_subcmd in _KIROCREW_SERVER_SUBCOMMANDS:
                        return True
                    # Subcommand-as-argument form: ``-m kiro_crew gateway``. The
                    # subcommand is argparse's first positional after the module,
                    # i.e. always at index+2. Check only that slot so a later
                    # positional/flag value cannot match — e.g.
                    # ``-m kiro_crew run gateway`` ("gateway" is a file argument
                    # to the task runner) must NOT be treated as a server.
                    if (
                        index + 2 < len(tokens)
                        and tokens[index + 2] in _KIROCREW_SERVER_SUBCOMMANDS
                    ):
                        return True

        # --- Console-script form: ".../kirocrew <subcmd>" (or kirocrew.exe on Win)
        if (
            _basename_stem(token) == "kirocrew"
            and index + 1 < len(tokens)
            and tokens[index + 1] in _KIROCREW_SERVER_SUBCOMMANDS
        ):
            return True

    return False


def _is_kirocrew_process(pid: int) -> bool:
    """Return ``True`` if *pid* looks like a KiroCrew gateway process.

    Resolves the process command line cross-platform via
    :func:`platform_compat.process_command_line` (Linux ``/proc``, macOS ``ps``,
    Windows ``Win32_Process`` WMI — the venv ``kirocrew.exe`` re-execs
    ``python.exe`` so the image name alone is ambiguous there) and defers
    classification to :func:`_args_look_like_kirocrew`.

    ``process_command_line`` returns ``""`` on any failure (dead PID, missing
    ``ps``, WMI error), which classifies as "not a match" — _stop()'s separate
    ``listening_pid_tool_available()`` check already surfaces the tool-absent
    case, so this never needs to raise.
    """
    out = platform_compat.process_command_line(pid)
    if not out:
        return False
    return _args_look_like_kirocrew(out)


def _pid_exited(pid: int) -> bool:
    """Return True if *pid* no longer exists.

    Routes through ``platform_compat.pid_exists`` — a raw ``os.kill(pid, 0)``
    would TERMINATE the process on Windows instead of probing it.
    """
    return not platform_compat.pid_exists(pid)


def _own_console_script() -> str | None:
    """Absolute path of the console script *this* CLI process was invoked as.

    Returns ``None`` unless ``sys.argv[0]`` is an existing executable file
    basenamed ``kirocrew``.

    :func:`_spawn_detached_gateway` prefers this over ``shutil.which("kirocrew")``
    so a restart replaces the gateway with the *same* entry point that asked for
    the restart. ``which`` returns whatever ``kirocrew`` sits earliest on
    ``PATH``, which is not necessarily this one: a downstream edition composes
    this core behind its own ``[project.scripts]`` entry point of the same name,
    so an editable install of the stock core in another interpreter (mise, a
    stray venv) shadows it. Respawning that one starts a gateway with different
    composed providers than the one just stopped — a silent edition downgrade,
    from a command whose only job was to restart what was already running.

    ``which`` remains the fallback for invocations whose argv[0] is not a script
    path (``python -m kiro_crew restart``, a frozen bundle, a launcher that
    rewrites argv).
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0 or _basename_stem(argv0) != "kirocrew":
        return None
    path = Path(argv0)
    if not path.is_absolute():
        # argv[0] may be a bare name found on PATH ("kirocrew") or a relative
        # path; resolve it the way the shell did.
        resolved = shutil.which(argv0)
        if not resolved:
            return None
        # MUST be absolutized. ``shutil.which`` returns an argument that already
        # has a directory component *unchanged*, so ``.venv/bin/kirocrew``
        # (a `cd ~/checkout && .venv/bin/kirocrew restart` invocation) comes back
        # still relative. :func:`_spawn_detached_gateway` passes ``cwd=$HOME`` to
        # ``Popen``, which chdirs the child BEFORE exec, so a relative program
        # path would resolve under ``$HOME`` and raise ``FileNotFoundError`` —
        # after ``_stop()`` has already SIGTERMed the gateway, leaving nothing
        # running. ``absolute()`` and not ``resolve()``: prepending the cwd is the
        # whole fix, while following symlinks could exec under a different
        # basename than the one the user invoked.
        path = Path(resolved).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path)


def _spawn_detached_gateway(port: int | None = None) -> int:
    """Spawn a detached ``kirocrew gateway`` so the calling shell returns.

    Used by :func:`_restart` when no platform service is active. The
    new process:

    - Detaches via ``start_new_session=True`` (own session + process
      group), so closing the calling terminal does not SIGHUP it.
    - Drops stdin to ``/dev/null`` and redirects stdout/stderr to
      ``~/.kirocrew/gateway.log`` (same file the existing ``logs``
      command tails for foreground gateways), so the user has one
      place to look regardless of how the gateway was started.
    - Resolves the console script this CLI was invoked as
      (:func:`_own_console_script`) first, so a restart respawns the
      *same* ``kirocrew`` rather than whichever one happens to sit
      earliest on ``PATH``; then ``shutil.which("kirocrew")``, falling
      back to ``sys.executable -m kiro_crew`` so editable/source-tree
      dev installs also work without a global ``kirocrew`` symlink.
    - Closes all inherited file descriptors so it does not pin sockets
      or pipes from the parent CLI process.
    - Binds *port* when given (``--port N``).

    Passing *port* is what keeps a restart coherent. The caller has already
    resolved a port, stopped the gateway on it, and will poll *that* port for
    readiness — but the child re-resolves independently, and its resolution
    order has no access to the parent's. Once ``resolve_client_port`` can
    discover a port from a run-marker (or once the marker is cleared by the
    stop we just performed), parent and child can disagree: the replacement
    would bind 5476 while the parent polls 6776 and prints a 6776 URL. Naming
    the port explicitly removes the disagreement by construction.

    Returns the new PID.
    """
    log_path = config_dir() / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Open in append mode so successive restarts accumulate history in
    # one log file. The fd is owned by the child after Popen returns.
    log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115

    bin_path = _own_console_script() or shutil.which("kirocrew")
    if bin_path:
        argv: list[str] = [bin_path, "gateway"]
    else:
        # Source-tree/editable-install fallback: run the module directly.
        # This also covers the case where the wrapper script is not on PATH
        # (e.g. running from an unactivated checkout).
        argv = [sys.executable, "-m", "kiro_crew", "gateway"]
    if port is not None:
        argv += ["--port", str(int(port))]

    # Detach so closing the calling terminal doesn't take the gateway with it.
    # Pass both flags explicitly (NOT **dict unpack — that breaks mypy's Popen
    # overload resolution on the build fleet). POSIX: start_new_session=True (own
    # session/group, immune to SIGHUP); creationflags resolves to 0 (no-op).
    # Windows: there is no setsid (start_new_session is silently ignored), so
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP gives the child its own
    # console-less process group that survives the parent. The flags come from
    # platform_compat (getattr) so referencing them doesn't fail mypy's
    # [attr-defined] check on Linux where subprocess.* lacks them.
    proc = subprocess.Popen(  # noqa: S603 — argv from trusted sources
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        close_fds=True,
        cwd=str(Path.home()),
        start_new_session=platform_compat.IS_POSIX,
        creationflags=(platform_compat.DETACHED_PROCESS | platform_compat.CREATE_NEW_PROCESS_GROUP),
    )
    return proc.pid


_RESTART_TOKEN_TTL = "20h"
_RESTART_READY_TIMEOUT = 15  # seconds to wait for gateway to become ready


def _print_token_url(port: int) -> None:
    """Wait for the gateway to come up, then print a fresh token URL."""
    secret_path = config_dir() / ".local_secret"
    deadline = time.monotonic() + _RESTART_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            secret = secret_path.read_text().strip()
            url = f"http://{_CLI_LOOPBACK}:{port}/api/token/local?ttl={_RESTART_TOKEN_TTL}"
            req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                token = data.get("token", "")
            if token:
                # Print the canonical loopback host (kirocrew.localhost when it
                # resolves, else localhost) — same host the gateway auto-opens —
                # so the post-restart URL doesn't land the browser on a different
                # origin and split the SPA's per-origin localStorage settings.
                # (The /api/token/local call above stays localhost: it's a loopback
                # API request, not a browser URL.)
                host = resolve_dashboard_host(local_only=True)
                print(f"\n🔑 http://{host}:{port}?token={token}")
                origin = dashboard_origin(KiroCrewConfig.load().dashboard.url)
                if origin and "localhost" not in origin:
                    print(f"   {origin}/?token={token}")
                return
        except (OSError, urllib.error.URLError, FileNotFoundError, ValueError):
            pass
        time.sleep(1)
    # Non-fatal — gateway might just be slow to start
    print("\n⚠️  Could not generate token (gateway still starting?). Run: kirocrew token")


def _restart(cli_port: int | None = None) -> None:
    """Restart a running KiroCrew gateway.

    Service-aware, mirroring :func:`_stop`:

    1. If a systemd/launchd service is active AND the caller did not
       explicitly request a specific port, ask the platform to restart
       it (``systemctl restart`` / ``launchctl unload + load``).
    2. Otherwise, SIGTERM the foreground gateway via the existing
       lsof+SIGTERM path used by ``kirocrew stop``, then spawn a
       detached replacement.

    When ``cli_port is not None`` (user passed ``--port N``), branch (1) is
    bypassed: the systemd unit name is not bound to a specific port, so
    short-circuiting through it would target the wrong gateway.
    """
    port = resolve_client_port(cli_port)
    if cli_port is None and service_controller.restart_service():
        sel().log_api_access(
            caller="cli",
            operation="gateway_restart",
            outcome="allowed",
            source="cli",
            resources=f"port={port} via=service",
        )
        print("✅ Restarted kirocrew service.")
        _print_token_url(port)
        return

    # No service active — bounce the foreground gateway and detach a fresh one.
    # Reuse _stop() for the SIGTERM path so behavior stays in sync if _stop
    # ever gains new safety checks. _stop() exits the process with sys.exit(1)
    # when no gateway is running, which is wrong for restart: a user running
    # `kirocrew restart` after the gateway crashed should still get a fresh
    # gateway. Detect that case up-front instead of letting _stop() exit.
    # Also enter _stop() when the lookup tool is absent: find_listening_pids()
    # returns [] both when nothing listens AND when lsof is missing, so guarding
    # only on a truthy result would skip the stop and double-spawn a second
    # gateway on a lsof-less POSIX host. _stop() surfaces the distinct
    # "lsof not found" diagnostic (and exits) in that case.
    if (
        platform_compat.find_listening_pids(port)
        or not platform_compat.listening_pid_tool_available()
    ):
        # TOCTOU: the gateway can exit between the check above and _stop()'s own
        # lookup. _stop() raises SystemExit(1) when it finds nothing — for restart
        # that's the wrong behavior. Swallow SystemExit so we always proceed to
        # spawn a fresh gateway. The user asked for a restart; an exit-before-spawn
        # here would leave them with no running gateway at all.
        try:
            _stop(cli_port)
        except SystemExit:
            pass

    pid = _spawn_detached_gateway(port)
    sel().log_api_access(
        caller="cli",
        operation="gateway_restart",
        outcome="allowed",
        source="cli",
        resources=f"port={port} via=fork pid={pid}",
    )
    print(f"✅ Started detached gateway (pid {pid}). Logs: kirocrew logs -f")
    _print_token_url(port)


def _update() -> None:
    """Update KiroCrew via git fetch + reset --hard + rebuild."""
    print("👻 Updating Kiro Crew…\n")

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj:
        print("❌ KIROCREW_PROJECT_DIR not set — cannot locate source tree")
        print("   Run from the project directory or run `kirocrew setup` first.")
        sys.exit(1)

    proj_path = Path(proj)
    if not (proj_path / ".git").is_dir():
        print(f"❌ No git repo at {proj}")
        sys.exit(1)

    print(f"  📂 {proj}")

    # Detect current branch
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if branch_result.returncode != 0:
        print("❌ Could not determine current branch")
        sys.exit(1)
    branch = branch_result.stdout.strip() or "mainline"
    if branch == "HEAD":
        branch = "mainline"

    # Source pin, checked before the fetch so a blocked update never touches the
    # tree. A human at a terminal is not the authorization: the fleet decides
    # which remote this host may take code from.
    from kiro_crew.platform.update_governance import resolve_remote_url, update_blocked_reason

    _blocked = update_blocked_reason(resolve_remote_url(proj, remote="origin"))
    if _blocked:
        print(f"  🛡️  Update blocked by security policy: {_blocked}")
        sys.exit(1)

    # Fetch + reset --hard: no merge conflicts, untracked files preserved
    print("  ⬇️  git fetch…")
    result = subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"  ❌ git fetch failed:\n{result.stderr.strip()}")
        sys.exit(1)

    # Check if there are new commits
    diff_result = subprocess.run(
        ["git", "diff", "HEAD", f"origin/{branch}", "--quiet"],
        cwd=proj,
        capture_output=True,
        timeout=10,
    )
    if diff_result.returncode == 0:
        print("\n✅ Already up to date!")
        return

    # Warn about local tracked-file changes before discarding
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=10,
    )
    tracked_changes = [
        line for line in status.stdout.strip().splitlines() if not line.startswith("??")
    ]
    if tracked_changes:
        print("  ⚠️  Local tracked-file changes will be discarded:")
        for line in tracked_changes[:10]:
            print(f"      {line}")
        resp = input("  Continue? [y/N] ").strip().lower()
        if resp != "y":
            print("  Aborted.")
            sys.exit(0)

    print(f"  🔄 git reset --hard origin/{branch}…")
    result = subprocess.run(
        ["git", "reset", "--hard", f"origin/{branch}"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(f"  ❌ git reset failed:\n{result.stderr.strip()}")
        sys.exit(1)

    # Update the optional kiro-cli backend if present.
    if shutil.which("kiro-cli"):
        print("  🔄 kiro-cli update")
        subprocess.run(["kiro-cli", "update"], capture_output=True, timeout=120)

    # Ensure Node.js >= 16 for frontend builds
    from kiro_crew.cli import _ensure_node  # circular import: cli -> cli_server -> cli

    print("  🔄 Checking Node.js…")
    _ensure_node(proj)

    # Build the dashboard frontend assets (npm), then reinstall the package.
    build_frontend_sync(proj_path)

    print("  🔨 pip install -e .")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=proj,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ❌ Install failed:\n{result.stderr.strip()}")
        sys.exit(1)

    print("\n✅ Kiro Crew updated!")
    print(f"\n{DATA_WARNING}\n")

    # Re-install agent config so new denied commands take effect.
    # Run as subprocess since the current process has old code loaded.
    print("  🔒 Refreshing agent config…")
    r = subprocess.run(
        [sys.executable, "-m", "kiro_crew", "setup", "--agent-only"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode == 0:
        print("  ✅ Agent config refreshed (deniedCommands + hooks updated)")
    else:
        print("  ⚠️  Agent config refresh failed — run: kirocrew setup --agent-only")


def _status(args: argparse.Namespace) -> None:
    """Query the running gateway for stats, or print offline message."""
    port = resolve_client_port(getattr(args, "port", None))
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("Kiro Crew gateway is running (token auth enabled).")
            print("  For detailed stats, see the Overview page in the dashboard.")
        else:
            print(f"Kiro Crew gateway is running but returned HTTP {e.code}.")
        return
    except (urllib.error.URLError, OSError):
        print("Kiro Crew gateway is not running.")
        print("  Start it with: kirocrew gateway")
        return
    except Exception:
        print("Kiro Crew gateway is running but returned an unexpected response.")
        return

    print(f"Kiro Crew v{__version__} 👻\n")
    print(f"  Uptime:      {data.get('uptime', '—')}")
    print(f"  Sessions:    {data.get('sessions', 0)}")
    print(f"  Messages:    {data.get('messages', 0)}")
    print(f"  Tool calls:  {data.get('tool_calls', 0)}")
    print(f"  Subagents:   {data.get('subagents', 0)}")
    print(f"  Cron jobs:   {data.get('crons', 0)}")
    print(f"  Lessons:     {data.get('lessons', 0)}")


async def _gateway(
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_open: bool = False,
    port_override: str | None = None,
    json_ready: bool = False,
    approval_mode: str | None = None,
    test_mode: bool = False,
) -> None:
    """Load config and start the Slack Socket Mode gateway."""
    # Activate mise once at gateway start so every subprocess we
    # later spawn — MCP servers, script crons, kiro-cli — inherits the user's
    # mise-managed toolchain. Without this, Node-based MCP servers spawn against
    # the system /usr/bin/node (v18 on AL2) and die during `initialize` with a
    # stderr-only "Node version 18 detected" error. No-op when mise is absent.
    _mise_changed = activate_mise()
    if _mise_changed:
        logging.getLogger(__name__).info(
            "Activated mise at gateway start (updated %s)", ", ".join(_mise_changed)
        )

    # Ensure Node >= 16 so frontend builds work (avoids legacy fallback).
    from kiro_crew.cli import _ensure_node, _node_ok  # circular import: cli -> cli_server -> cli

    if not _node_ok():
        _ensure_node()

    # Resolve the dashboard's React build. Skipped in slack-only mode since no
    # dashboard will be served. When the prebuilt dist/ is missing the gateway
    # has no dashboard shell to serve and returns the "not found" guidance page
    # (the legacy dashboard.html fallback was removed — Talos V2285871874);
    # build the frontend to restore the full dashboard.
    if not no_dashboard and ensure_dev_dist_symlink() is None:
        logging.getLogger(__name__).warning(
            "Dashboard dist/ not found — the dashboard will show the "
            "'not built' guidance page until the SPA is bundled. "
            "Run `npm ci && npm run build` in the website/ directory to build "
            "the full dashboard."
        )

    if not config_path().exists():
        cfg = KiroCrewConfig()
        cfg.save()
        print(f"👻 Created default config: {config_path()}")

    cfg = KiroCrewConfig.load()
    await run_gateway(
        cfg,
        no_dashboard=no_dashboard,
        no_crons=no_crons,
        no_open=no_open,
        port_override=port_override,
        json_ready=json_ready,
        approval_mode=approval_mode,
        test_mode=test_mode,
    )


async def _run_task(args: argparse.Namespace) -> None:
    """Execute a spec file autonomously via TaskRunner."""

    spec_path = Path(args.spec).resolve()
    if not spec_path.exists():
        print(f"❌ Spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)

    cfg = KiroCrewConfig.load()
    factory = build_provider_factory(cfg)
    sessions = SessionManager(cfg, provider_factory=factory)  # type: ignore[arg-type]

    auto_test = not getattr(args, "no_test", False)
    fresh = getattr(args, "fresh", False)
    timeout = float(getattr(args, "timeout", 0))

    # Initialize history + lessons for learning and memory formation
    memory = MemoryStore()
    memory.init()

    # Vector memory (structured semantic store)

    vector_memory = VectorMemoryStore(
        confidence_threshold=cfg.memory.semantic_confidence_threshold,
        extra_prefixes=cfg.memory.semantic_keys or None,
        episodic_limit=cfg.memory.episodic_max_results,
        embedding_dim=cfg.memory.embedding_dim,
    )
    vector_memory.init()
    # Embeddings are always-on: wire the factory; bind embed_fn when the model
    # is already present. Deliberately NO download kick here — `kirocrew run`
    # is a one-shot CLI and must not start a 610MB download it will abandon at
    # exit; the long-lived gateway owns the background download.
    vector_memory.embed_fn_factory = make_sync_embed_fn
    if model_file_present():
        vector_memory.embed_fn = make_sync_embed_fn()
    else:
        print(
            "Embedding model not downloaded yet — keyword search for this run "
            "(the gateway downloads it in the background)",
            file=sys.stderr,
        )
    memory.vector_store = vector_memory

    conv_log = ConversationLog()
    conv_log.init()
    lessons = LessonStore()
    skills = SkillsLoader()
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=memory,
        sessions=sessions,
        lesson_store=lessons,
        history_idle_secs=cfg.memory.history_idle_hours * 3600,
        skills_loader=skills,
        auto_skills_enabled=cfg.skills.auto_create_from_sessions,
        auto_refine_enabled=cfg.skills.auto_refine_on_deviation,
        auto_min_tool_calls=cfg.skills.auto_min_tool_calls,
        auto_similarity_threshold=cfg.skills.auto_similarity_threshold,
        approval_required=cfg.skills.approval_required,
        max_auto_skills=cfg.skills.max_auto_skills,
        stale_after_days=cfg.skills.stale_after_days,
        archive_after_days=cfg.skills.archive_after_days,
        generate_scripts=cfg.skills.generate_scripts,
        judge_model=cfg.skills.judge_model,
    )

    async def _cli_notify(title: str, body: str, task_id: str = "") -> None:
        print(f"\n{title}")
        if body:
            print(f"  {body}")

    # Opt-out state is sourced from the keystone denied_commands.json, not
    # config.json's hooks section (the agent cannot write the keystone file).
    hooks = HookManager(hooks_config_from_config_dict(cfg.hooks))
    ctx = ContextBuilder(
        memory=memory, skills=skills, hooks=hooks, lessons=lessons, bot_name=cfg.agent.bot_name
    )

    runner = TaskRunner(
        sessions=sessions,
        context_builder=ctx,
        auto_test=auto_test,
        on_notify=_cli_notify,
        work_dir=_session_work_dir("taskrunner:main"),
        conversation_log=conv_log,
        consolidator=consolidator,
        lesson_store=lessons,
        fresh=fresh,
        global_timeout=timeout,
        workspace_dir=cfg.taskrunner.workspace_dir,
        max_parallel_steps=cfg.taskrunner.max_parallel_steps,
    )

    # Pre-warm session pool (background session for lesson extraction)
    await sessions.start_pool()

    if fresh:
        print(f"👻 Running spec (fresh): {spec_path}")
    else:
        print(f"👻 Running spec: {spec_path}")
    task_name = getattr(args, "name", "")
    result = await runner.run(spec_path, name=task_name)

    label = result.name or result.task_id
    if result.status == "completed":
        print(f"\n✅ Task completed — {label} ({len(result.tasks)} steps)")
    elif result.status == "failed":
        print(f"\n❌ Task failed ({label}): {result.error}", file=sys.stderr)
        sys.exit(1)
    elif result.status == "cancelled":
        print("\n⚠️  Task cancelled")
        sys.exit(1)

    await sessions.close_all()


def _service_cmd(args: argparse.Namespace) -> int:
    """Dispatch ``kirocrew service {install,uninstall,status}``.

    Wraps :mod:`kiro_crew.service.controller` so that platform detection
    and the underlying systemctl/launchctl calls live there. The CLI
    layer only handles argument parsing, audit logging, and exit codes.
    """
    action = getattr(args, "service_action", None)
    if action == "install":
        rc = service_controller.install_service()
        sel().log_api_access(
            caller="cli",
            operation="service_install",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    if action == "uninstall":
        rc = service_controller.uninstall_service()
        sel().log_api_access(
            caller="cli",
            operation="service_uninstall",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    if action == "status":
        rc = service_controller.service_status()
        sel().log_api_access(
            caller="cli",
            operation="service_status",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    print("Usage: kirocrew service {install|uninstall|status}", file=sys.stderr)
    return 2


def _logs_cmd(args: argparse.Namespace) -> None:
    """Tail gateway logs from the most appropriate source.

    Order of preference:
      1. systemd journal (if the system service is installed on Linux)
      2. launchd stdout file (macOS)
      3. ``~/.kirocrew/gateway.log`` (foreground gateway)
    """
    follow = bool(getattr(args, "follow", False))
    lines = int(getattr(args, "lines", 100) or 100)
    plat = current_platform()
    unit = f"{SERVICE_NAME}.service"

    # Audit before any os.execvp branch — the exec replaces this process
    # so a post-exec audit call would never run.
    sel().log_api_access(
        caller="cli",
        operation="logs",
        outcome="allowed",
        source="cli",
        resources=f"follow={follow} lines={lines} platform={plat.value}",
    )

    if plat == Platform.SYSTEMD and svc_linux.UNIT_PATH.exists():
        # Try journalctl unprivileged first — it works if the user is in
        # the `systemd-journal` or `adm` group. Only fall back to sudo
        # journalctl if the unprivileged probe returns no rows. Without
        # this fall-through, `kirocrew logs` would hang on hosts without
        # passwordless sudo, which is a surprising failure mode for a
        # read-only log-viewer.
        base = ["journalctl", "--no-pager", "-u", unit, "-n", str(lines)]
        probe = subprocess.run(
            ["journalctl", "-u", unit, "-n", "1", "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            if follow:
                base.append("-f")
            os.execvp("journalctl", base)
        # Refuse to invoke sudo without a TTY: in non-interactive
        # contexts (cron, piped scripts, systemd ExecStartPre) the sudo
        # password prompt would block forever with no way to cancel.
        if not sys.stdin.isatty():
            print(
                "👻 Insufficient permissions to read the journal without sudo, "
                "and stdin is not a TTY so sudo can't prompt.\n"
                "   Add your user to the `systemd-journal` or `adm` group, or run:\n"
                f"   sudo journalctl -u {unit} -f",
                file=sys.stderr,
            )
            sys.exit(1)
        # Fall back to sudo journalctl. `--no-pager` prevents the pager
        # (`less`) from taking over after exec, which behaves badly in
        # piped/non-interactive contexts.
        sudo_cmd = ["sudo", *base]
        if follow:
            sudo_cmd.append("-f")
        os.execvp("sudo", sudo_cmd)

    if plat == Platform.LAUNCHD and svc_macos.STDOUT_LOG.exists():
        cmd = ["tail", "-n", str(lines)]
        if follow:
            cmd.append("-f")
        cmd.append(str(svc_macos.STDOUT_LOG))
        os.execvp("tail", cmd)

    fallback = config_dir() / "gateway.log"
    if not fallback.exists():
        print(
            "👻 No gateway logs found. Either install the service "
            "(`kirocrew service install`) or start the gateway "
            "(`kirocrew gateway`).",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(fallback))
    os.execvp("tail", cmd)
