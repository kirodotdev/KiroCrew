"""CLI server lifecycle commands — update, stop, token, logout, status, gateway, run."""

from __future__ import annotations

import argparse
import http.client
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
from kiro_crew.beacon import is_default_home
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
from kiro_crew.embeddings import (
    make_sync_embed_fn,
    model_file_present,
    store_embedding_space_is_stale,
)
from kiro_crew.env import activate_mise
from kiro_crew.frontend import build_frontend_sync, ensure_dev_dist_symlink
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import HookManager, hooks_config_from_config_dict
from kiro_crew.instances import run_marker
from kiro_crew.learn import LessonStore
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.memory import MemoryStore
from kiro_crew.preflight import run_preflight_checks
from kiro_crew.sel import sel
from kiro_crew.service import controller as service_controller
from kiro_crew.service import linux as svc_linux
from kiro_crew.service import macos as svc_macos
from kiro_crew.service.common import SERVICE_NAME, Platform, current_platform
from kiro_crew.session import SessionManager
from kiro_crew.skill_usage import register_skill_read_observer
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
        with loopback_urlopen(req, timeout=2) as resp:  # nosemgrep
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
        with loopback_urlopen(req, timeout=5) as resp:
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
        with loopback_urlopen(req, timeout=5) as resp:
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
        # would then double-spawn).
        if not platform_compat.listening_pid_tool_available():
            _tool = platform_compat.listening_pid_tool()
            # A host that keeps its binaries outside the system directories
            # (NixOS, a Homebrew or conda prefix) has the tool and still lands
            # here, because the lookup is pinned to those directories. Telling
            # that operator to install what they already have sends them in
            # circles, so name where it actually is instead.
            _unpinned = platform_compat.tool_outside_trusted_dirs(_tool)
            _reason = f"{_tool}_outside_trusted_dirs" if _unpinned else f"{_tool}_not_found"
            sel().log_api_access(
                caller="cli",
                operation="gateway_stop",
                outcome="no_target",
                source="cli",
                resources=f"port={port} reason={_reason}",
            )
            if _unpinned:
                print(
                    f"`{_tool}` is installed at {_unpinned}, outside the system directories "
                    f"Kiro Crew resolves it from, so it cannot look up the gateway process "
                    f"on port {port}. Falling back to PATH is deliberately refused: a "
                    f"gateway's PATH can lead with writable directories."
                )
            else:
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


def _wait_for_pids_exit(pids: list[int], timeout: float) -> list[int]:
    """Block until every pid in *pids* is gone. Return the ones still alive.

    An empty return means they all exited. :func:`_restart` uses this to keep a
    replacement gateway from starting while the incumbent is still shutting down.

    The incumbent holds an exclusive ``flock`` on ``<KIROCREW_HOME>/gateway.lock``
    (see :mod:`kiro_crew.gateway_lock`) for its whole lifetime, and the release
    happens only after ``asyncio.run(_gateway(...))`` returns — so the lock
    outlives dashboard teardown, cron-scheduler stop, conversation-log flushes,
    and MCP child reaping. The replacement's acquire is a single non-blocking
    attempt that prints a refusal and exits 1, so spawning it too early leaves NO
    gateway running at all. ``_stop`` waits at most 1s and reports nothing back,
    which is why restart does its own bounded wait.

    Pid reuse is possible but fails SAFE: the caller classifies the pids as
    KiroCrew gateways once, before the stop, and a pid recycled onto an unrelated
    process only makes this wait time out. That produces a loud refusal, never a
    premature "the incumbent is gone" all-clear. Re-classifying inside the loop
    would invert that -- a recycled pid would read as "not a gateway" and let the
    replacement spawn while the real incumbent still holds the lock.
    """
    if not pids:
        return []
    deadline = time.monotonic() + timeout
    while True:
        alive = [p for p in pids if not _pid_exited(p)]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.1)


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


def _spawn_detached_gateway(port: int | None = None) -> subprocess.Popen[bytes]:
    """Spawn a detached ``kirocrew gateway`` so the calling shell returns.

    Used by :func:`_restart` when no platform service is active. The
    new process:

    - Detaches via ``start_new_session=True`` (own session + process
      group), so closing the calling terminal does not SIGHUP it.
    - Drops stdin to ``/dev/null`` and redirects stdout/stderr to
      ``~/.kiro/crew/gateway.log`` (same file the existing ``logs``
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

    Returns the ``Popen`` handle, not just the pid: the caller must be able to
    ask whether the replacement is still alive before it reports success, and
    only the handle yields the child's **exit status** when it is not. A
    replacement refused by the ``KIROCREW_HOME`` ownership guard exits 1 within
    milliseconds, and that status is the whole diagnosis.
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
    return proc


_RESTART_TOKEN_TTL = "20h"
_RESTART_READY_TIMEOUT = 15  # seconds to wait for gateway to become ready
# Gap between readiness probes while waiting for the replacement gateway. Short
# enough that a fast boot is reported promptly, long enough not to hammer the
# starting gateway's event loop while it restores sessions.
_RESTART_READY_POLL_INTERVAL = 0.5
# Seconds to wait for the incumbent gateway to exit before spawning its
# replacement. Generous because a graceful shutdown reaps MCP servers and
# kiro-cli children; the wait ends as soon as the pids are gone, so the common
# case costs a fraction of a second.
_RESTART_STOP_TIMEOUT = 30.0

# Verdicts returned by :func:`_wait_gateway_ready`. The two failure modes are
# kept apart because they need different operator action: a replacement that
# DIED is a refused/broken startup (read the log), while one that never became
# READY is still running and may simply be slow.
_READY_OK = "ready"
_READY_DIED = "died"
_READY_TIMEOUT = "timeout"


def _probe_gateway_ready(port: int, timeout: int = 3) -> int:
    """HTTP status of ``GET /api/ready`` on the loopback gateway, ``0`` if unreachable.

    Same contract as :func:`kiro_crew.pod.runtime.health` (the status code, or
    ``0`` when the connection itself fails) but pointed at ``/api/ready`` rather
    than ``/api/health``. That choice is the point of the probe: liveness only
    proves a socket is bound, whereas readiness returns 503 until
    ``DashboardState.ready`` is published *and* 503 again the moment shutdown is
    requested — exactly the difference between "something answers this port" and
    "the new gateway is serving". Both paths are in ``origin.PROBE_PATHS`` and
    need no auth, so this hands no secret to whatever answers.

    Every failure mode collapses to ``0`` (unreachable), including a listener
    that answers the TCP handshake but does not speak HTTP. That case raises
    ``http.client.HTTPException`` (``BadStatusLine`` and friends), which is NOT
    an ``OSError`` or a ``URLError``, so it has to be caught explicitly -- and it
    is exactly the case this probe exists to survive: a wedged fork holding the
    port is the reason restart is being run at all, and it must produce a
    "not ready" verdict, never an uncaught traceback out of the CLI.
    """
    url = f"http://{_CLI_LOOPBACK}:{port}/api/ready"
    try:
        # Loopback-only probe to our own gateway on 127.0.0.1; the URL is
        # internally derived (never attacker-supplied), so the dynamic-URL SSRF
        # audit rule is a false positive here.
        with loopback_urlopen(url, timeout=timeout) as resp:  # nosemgrep
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0


def _replacement_is_serving(port: int, prior_pid: int | None) -> bool:
    """True when the gateway now answering *port* is NOT the pre-restart one.

    A bare port probe cannot tell a replacement from the incumbent — the old
    gateway keeps answering until its socket closes, so a 200 taken during the
    handover would report success for the process we just asked to die. The
    discriminator is the run-marker pid: every dashboard-serving gateway records
    its own pid in ``run/gateway-<port>.pid`` while wiring the dashboard, i.e.
    *before* it publishes readiness, so a ready gateway's marker always names the
    process that is serving. Waiting for that identity to CHANGE (rather than for
    any 200) mirrors the ``_gateway_start_id`` handshake in the dev-fleet app.

    On POSIX with a working listener lookup the marker claim is additionally
    checked against reality via :func:`_gateway_owns_port` (recorded pid holds
    the port, owned by us, looks like a gateway). That check denies outright on
    Windows — and on any POSIX host without ``lsof``/``netstat`` it cannot
    succeed either — so it is only applied where it can pass; elsewhere the
    marker comparison alone stands, which keeps restart from reporting a false
    failure on those hosts.

    **No marker means not proven, never proven.** An absent marker is the state
    the handover itself produces: ``clear_marker`` runs on graceful shutdown
    BEFORE the outgoing gateway's ``_shutdown()``, so there is a window in which
    the old gateway has erased its marker and its socket still answers. Treating
    that as "the replacement is serving" would report the outgoing gateway's 200
    as the new one's — precisely the confusion this function exists to prevent —
    and on a host without a listener lookup nothing downstream would catch it.
    So an unreadable marker returns ``False``: the caller keeps polling until a
    marker appears or the deadline passes. The cost is a gateway whose marker
    write failed (the write is best-effort, wrapped in ``except Exception``)
    being reported as "not ready" while it is in fact serving. That is the right
    way to be wrong here — a misleading timeout leaves a working gateway and an
    accurate ``kirocrew token``, whereas a false success leaves the operator
    believing a dead replacement is up.
    """
    recorded = run_marker.read_pid(port)
    if recorded is None:
        return False
    if prior_pid is not None and recorded == prior_pid:
        return False
    if platform_compat.IS_POSIX and platform_compat.listening_pid_tool_available():
        return _gateway_owns_port(port)
    return True


def _wait_gateway_ready(
    proc: subprocess.Popen[bytes],
    port: int,
    prior_pid: int | None,
    timeout: float,
) -> tuple[str, int | None]:
    """Poll until the spawned gateway actually serves *port*, or fail.

    Returns ``(_READY_OK, None)`` once the replacement answers ``/api/ready``,
    ``(_READY_DIED, exit_status)`` as soon as the child has exited, or
    ``(_READY_TIMEOUT, None)`` when the deadline passes with it still up.

    Two details earn their keep:

    * **Early death short-circuits the wait.** A replacement refused by the
      ``KIROCREW_HOME`` ownership guard exits within milliseconds; polling the
      port for the full timeout would turn a instantly-knowable failure into a
      15s stall with a worse message. ``proc.poll()`` is used rather than a pid
      liveness probe because we are the child's parent, so it both detects the
      exit and yields the status the operator needs. (Same shape as ``pod``'s
      ``_wait_healthy`` bailing out on a dead unit instead of burning the wait.)
    * **A zero timeout still probes once.** The deadline is checked *after* the
      probe, so a collapsed timeout reports what is actually there instead of a
      reflexive failure.
    * **The child is re-polled before the timeout verdict.** A replacement that
      exits DURING the last probe would otherwise be reported as "still running
      but not ready", sending the operator to look for a live process that no
      longer exists. The extra poll costs nothing and makes the two verdicts
      mutually exclusive in fact, not just by intention.
    """
    deadline = time.monotonic() + timeout
    while True:
        status = proc.poll()
        if status is not None:
            return _READY_DIED, status
        if _probe_gateway_ready(port) == 200 and _replacement_is_serving(port, prior_pid):
            return _READY_OK, None
        if time.monotonic() >= deadline:
            status = proc.poll()
            if status is not None:
                return _READY_DIED, status
            return _READY_TIMEOUT, None
        time.sleep(_RESTART_READY_POLL_INTERVAL)


def _print_token_url(port: int) -> None:
    """Wait for the gateway to come up, then print a fresh token URL."""
    secret_path = config_dir() / ".local_secret"
    deadline = time.monotonic() + _RESTART_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            secret = secret_path.read_text().strip()
            url = f"http://{_CLI_LOOPBACK}:{port}/api/token/local?ttl={_RESTART_TOKEN_TTL}"
            req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
            with loopback_urlopen(req, timeout=3) as resp:
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
       detached replacement and **verify it is serving** before reporting
       success: a spawn only proves a pid was created, so the replacement is
       polled on ``/api/ready`` until it answers. If it dies or never becomes
       ready the command prints why and exits non-zero rather than claiming a
       gateway that is not there.

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
    #
    # Capture the incumbent pids HERE, before the stop, so we can wait for them
    # afterwards: the replacement must not start while the old gateway still owns
    # the KIROCREW_HOME lock (see _wait_for_pids_exit). Filter with _stop()'s own
    # kirocrew-process predicate so an unrelated listener on this port never
    # becomes something we block on. The entry condition below stays on the
    # UNFILTERED lookup so _stop() keeps emitting its existing diagnostics.
    # Identity of the gateway we are about to replace, read BEFORE the stop: a
    # graceful shutdown clears the run-marker, so afterwards there is nothing
    # left to compare the replacement against. _replacement_is_serving() uses it
    # so a 200 from the OUTGOING gateway can never be mistaken for the new one
    # coming up. None (no marker, e.g. after a crash) simply means there is no
    # old identity to exclude.
    prior_marker_pid = run_marker.read_pid(port)
    listeners = platform_compat.find_listening_pids(port)
    incumbents = [p for p in listeners if _is_kirocrew_process(p)]
    if listeners or not platform_compat.listening_pid_tool_available():
        # TOCTOU: the gateway can exit between the check above and _stop()'s own
        # lookup. _stop() raises SystemExit(1) when it finds nothing — for restart
        # that's the wrong behavior. Swallow SystemExit so we always proceed to
        # spawn a fresh gateway. The user asked for a restart; an exit-before-spawn
        # here would leave them with no running gateway at all.
        try:
            _stop(cli_port)
        except SystemExit:
            pass

        alive = _wait_for_pids_exit(incumbents, _RESTART_STOP_TIMEOUT)
        if alive:
            # Refuse rather than spawn a replacement that the lock would reject.
            # Aborting leaves the user with a (slow) gateway; spawning anyway
            # would leave them with none, reported as a success.
            pids = ", ".join(str(p) for p in alive)
            sel().log_api_access(
                caller="cli",
                operation="gateway_restart",
                outcome="denied",
                source="cli",
                resources=f"port={port} reason=incumbent_still_running pids={alive}",
            )
            print(
                f"❌ Gateway (pid {pids}) did not exit within "
                f"{int(_RESTART_STOP_TIMEOUT)}s. Not starting a replacement.\n"
                f"   The old gateway still owns {config_dir()}, so a new one "
                f"would be refused and exit immediately.\n"
                f"   To inspect the shutdown, run: kirocrew logs -f\n"
                f"   If the process is wedged, force it: kill -9 {pids}"
            )
            sys.exit(1)

    proc = _spawn_detached_gateway(port)
    pid = proc.pid
    # A pid is not a running gateway. The replacement can be refused by the
    # ownership guard, crash on a bad config, or hang before it binds — all of
    # which used to print the success line below and exit 0 with nothing serving.
    # Report success only once the NEW gateway answers, and audit what happened.
    verdict, exit_status = _wait_gateway_ready(proc, port, prior_marker_pid, _RESTART_READY_TIMEOUT)
    if verdict != _READY_OK:
        reason = (
            f"replacement_died exit={exit_status}"
            if verdict == _READY_DIED
            else f"replacement_not_ready_within={int(_RESTART_READY_TIMEOUT)}s"
        )
        sel().log_api_access(
            caller="cli",
            operation="gateway_restart",
            outcome="denied",
            source="cli",
            resources=f"port={port} via=fork pid={pid} reason={reason}",
        )
        if verdict == _READY_DIED:
            print(
                f"❌ Replacement gateway (pid {pid}) died immediately "
                f"(exit status {exit_status}). Nothing is serving port {port}.\n"
                f"   A replacement that exits at once is usually refused startup — "
                f"another process still owning {config_dir()}, or a broken config.\n"
                f"   To see why it exited, run: kirocrew logs -f"
            )
        else:
            print(
                f"❌ Replacement gateway (pid {pid}) did not become ready within "
                f"{int(_RESTART_READY_TIMEOUT)}s. It is still running but not "
                f"serving port {port}.\n"
                f"   It may be slow to start or wedged during startup; nothing is "
                f"serving the dashboard yet.\n"
                f"   To follow its startup, run: kirocrew logs -f"
            )
        sys.exit(1)

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
    """Update Kiro Crew — dispatches based on install layout.

    Three install layouts, three update paths:

    * **git checkout** — fetch + reset --hard + rebuild (existing path).
    * **wheel / cli.sh** — fetch the release feed, compare versions, and
      re-run the installer if newer. This is the path that was missing and
      caused the ``KIROCREW_PROJECT_DIR not set`` error for cli.sh installs.
    * **externally managed** (desktop app, Docker) — print guidance on how
      to update via the correct surface instead of failing with an opaque error.
    """
    from kiro_crew.platform.update_layout import (
        EXTERNALLY_MANAGED,
        InstallLayout,
    )

    print("👻 Updating Kiro Crew…\n")

    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    proj_path = Path(proj) if proj else None
    is_git = proj_path is not None and (proj_path / ".git").exists()

    if not is_git:
        # Not a git checkout — check if externally managed or wheel install.
        from kiro_crew.beacon import distribution

        dist = distribution()
        if dist in EXTERNALLY_MANAGED:
            print(f"  ℹ️  This install ({dist}) is managed externally.")
            print(f"  {EXTERNALLY_MANAGED[dist]}")
            return

        # Wheel / cli.sh install path.
        layout = InstallLayout(
            kind=dist or "wheel",
            proj=proj,
            is_git=False,
            is_externally_managed=False,
            guidance="",
        )
        _update_wheel(layout)
        return

    # A git worktree or submodule stores ``.git`` as a FILE (a ``gitdir:``
    # pointer), not a directory, so accept both — otherwise `kirocrew update`
    # run from a worktree wrongly refuses with "No git repo".
    assert proj_path is not None  # narrowing: is_git=True implies proj_path was set
    if not (proj_path / ".git").exists():
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


def _update_wheel(layout) -> None:
    """Update a wheel/cli.sh install by checking the release feed and re-running the installer.

    This is the path taken when KIROCREW_PROJECT_DIR is unset or has no .git —
    the standard state for ``curl | sh`` installs where the venv at
    ``~/.kiro/crew-venv`` has no source tree.
    """
    import json
    import re
    import urllib.request

    from kiro_crew import __version__ as local_version
    from kiro_crew.platform.update_governance import update_blocked_reason
    from kiro_crew.platform.update_layout import cdn_bases, release_channel, wheel_update_command

    channel = release_channel()
    feed_base, artifact_base = cdn_bases()
    feed_url = f"{feed_base}/feed/{channel}/latest-cli.json"

    # Source-pin governance check: same seam the git path uses, applied to the
    # feed URL so a pinned fleet's wheel installs cannot bypass the ceiling.
    blocked = update_blocked_reason(feed_base)
    if not blocked:
        blocked = update_blocked_reason(artifact_base)
    if blocked:
        print(f"  🛡️  Update blocked by security policy: {blocked}")
        sys.exit(1)

    # Shell safety: cdn_bases() reads KIROCREW_CDN_BASE which is operator-set.
    # Reject metacharacters that could enable command injection when the URL
    # flows through wheel_update_command() into ``sh -c``.
    _SAFE_URL_RE = re.compile(r"^https://[A-Za-z0-9._/:%@~+\-]+$")
    if not _SAFE_URL_RE.match(feed_base) or not _SAFE_URL_RE.match(artifact_base):
        print("  ❌ CDN base URL contains disallowed characters")
        sys.exit(1)

    print(f"  📦 Install type: {layout.kind} (channel: {channel})")
    print(f"  📡 Checking {feed_url}…")

    # Fetch the release feed (scheme-validated to satisfy SAST — cdn_bases()
    # already enforces https but Semgrep cannot see through the indirection).
    if not feed_url.startswith("https://"):
        print(f"  ❌ Refusing non-HTTPS feed URL: {feed_url}")
        sys.exit(1)
    try:
        req = urllib.request.Request(feed_url, headers={"User-Agent": "kirocrew-update/1"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosemgrep: dynamic-urllib-use-detected
            raw = resp.read(65536 + 1)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  ❌ Could not reach release feed: {e}")
        print("\n  To update manually, run:")
        print(f"    {wheel_update_command(channel)}")
        sys.exit(1)

    if len(raw) > 65536:
        print("  ❌ Release feed response too large — may be corrupted")
        sys.exit(1)

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        print("  ❌ Release feed is not valid JSON")
        sys.exit(1)

    if not isinstance(manifest, dict):
        print("  ❌ Release feed has unexpected format")
        sys.exit(1)

    if manifest.get("schema") != "kirocrew-cli-artifact-manifest-v1":
        print("  ❌ Release feed schema mismatch — update the installer first")
        print(f"    {wheel_update_command(channel)}")
        sys.exit(1)

    if manifest.get("channel") != channel:
        print(f"  ❌ Feed channel mismatch (expected {channel}, got {manifest.get('channel')})")
        sys.exit(1)

    remote_version = manifest.get("version", "")
    if not remote_version:
        print("  ❌ No version in release feed")
        sys.exit(1)

    print(f"  📋 Current: {local_version}")
    print(f"  📋 Latest:  {remote_version}")

    # Compare versions using the same logic as the dashboard
    from kiro_crew.dashboard.handlers.updates import _is_newer

    newer = _is_newer(remote_version, local_version)
    if newer is None:
        print("  ⚠️  Could not compare versions — updating anyway to be safe")
    elif not newer:
        print("\n✅ Already on the latest version!")
        return

    # Run the installer
    cmd = wheel_update_command(channel)
    print("\n  🔄 Running installer…")
    print(f"     {cmd}\n")

    # Platform guard: cli.sh is a POSIX shell script; on Windows there is no sh.
    if sys.platform == "win32":
        print("  ❌ Wheel self-update is not supported on Windows.")
        print("  To update manually, run in PowerShell:")
        print(f"    {cmd}")
        sys.exit(1)

    try:
        result = subprocess.run(
            ["sh", "-c", cmd],
            timeout=300,
        )
    except FileNotFoundError:
        print("  ❌ 'sh' not found — cannot run the installer.")
        print("  To update manually, run:")
        print(f"    {cmd}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("\n  ❌ Installer timed out (5 min)")
        print("  Try running manually:")
        print(f"    {cmd}")
        sys.exit(1)
    if result.returncode != 0:
        print(f"\n  ❌ Installer exited with code {result.returncode}")
        print("  Try running manually:")
        print(f"    {cmd}")
        sys.exit(1)

    print(f"\n✅ Kiro Crew updated to {remote_version}!")
    print("\n  Restart the gateway to use the new version:")
    print("    kirocrew restart")


def _status(args: argparse.Namespace) -> None:
    """Query the running gateway for stats, or print offline message."""
    port = resolve_client_port(getattr(args, "port", None))
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with loopback_urlopen(url, timeout=3) as resp:
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


def _should_reconcile_launchd_launcher() -> bool:
    """Whether this gateway may repair the shared launchd launcher.

    Only a non-frozen production instance may.

    ``LIVE_PROGRAM`` is a per-user path under Application Support that
    ``KIROCREW_HOME`` does not scope, so a dev, pod, or worktree gateway
    repairing it would repoint the user's REAL agent at its own venv —
    recreating the serving-vs-managed mismatch the reconcile exists to prevent,
    and doing it without the operator ever acting on the production instance.
    ``is_default_home`` is reused rather than re-derived so the two cannot drift
    on what counts as the real home.

    A frozen build is excluded for a different reason: the launchd agent is a
    ``service install`` artifact belonging to a source or pip install, while a
    packaged app manages its own backend lifecycle and supplies environment its
    interpreter needs — notably ``PYTHONPYCACHEPREFIX``, which keeps bytecode out
    of the signed bundle. A launcher naming the bundled executable would be run by
    launchd WITHOUT that environment, so the interpreter would write
    ``__pycache__`` inside the app and invalidate its signature. The packaged app
    has no business owning this artifact at all.
    """
    return sys.platform == "darwin" and not getattr(sys, "frozen", False) and is_default_home()


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
    # has no dashboard shell to serve and returns the "not found" guidance page;
    # build the frontend to restore the full dashboard.
    if not no_dashboard and ensure_dev_dist_symlink() is None:
        logging.getLogger(__name__).warning(
            "Dashboard dist/ not found — the dashboard will show the "
            "'not built' guidance page until the SPA is bundled. "
            "Run `npm ci && npm run build` in the website/ directory to build "
            "the full dashboard."
        )

    # Reconcile the other derived artifact that lives outside the install: the
    # launchd agent's launcher script. It sits under Application Support, so a
    # "reset the app" gesture that clears that directory leaves the agent loaded
    # with nothing to execute and no in-product way back. Self-healing here
    # rather than in the service installer keeps a hand-customized plist intact.
    if _should_reconcile_launchd_launcher():
        try:
            svc_macos.ensure_live_program()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Could not restore the launchd live-gateway launcher: %s", exc
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
    # A stale vector space means the loaded FAISS index was built by a different
    # model, so a new-model query vector would be scored against incomparable
    # vectors. Degrade THIS run to keyword search rather than reconciling:
    # clearing is destructive, a one-shot CLI cannot re-embed a corpus, and with a
    # rejected custom path nothing could ever regenerate what it cleared. The
    # gateway reconciles and re-embeds on its next boot.
    if vector_memory.embed_fn is not None and store_embedding_space_is_stale(vector_memory):
        vector_memory.embed_fn = None
        vector_memory.embed_fn_factory = None
        print(
            "Embedding model changed — keyword search for this run "
            "(the gateway re-embeds in the background)",
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
    register_skill_read_observer(ctx)
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


def _sandbox_cmd(args: argparse.Namespace) -> int:
    """Dispatch ``kirocrew sandbox {install-profile,remove-profile,status}``.

    Mirrors :func:`_service_cmd`: platform detection and the privileged calls
    live in :mod:`kiro_crew.service.controller`, and this layer only parses
    arguments, writes the audit record, and returns an exit code.

    Installing an AppArmor profile is a privileged, security-relevant change to
    the host, so it is audited exactly like a service install.
    """
    action = getattr(args, "sandbox_action", None)
    path = getattr(args, "path", None)
    if action == "install-profile":
        rc = service_controller.install_launcher_profile(path)
        sel().log_api_access(
            caller="cli",
            operation="sandbox_profile_install",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc} path={path or '$APPIMAGE'}",
        )
        return rc
    if action == "remove-profile":
        rc = service_controller.remove_launcher_profile()
        sel().log_api_access(
            caller="cli",
            operation="sandbox_profile_remove",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    if action == "status":
        # Read-only, so no audit record — it changes nothing and is expected to
        # be polled by the desktop app on every launch.
        return service_controller.sandbox_profile_status(path)
    print(
        "Usage: kirocrew sandbox {install-profile|remove-profile|status}",
        file=sys.stderr,
    )
    return 2


def _logs_cmd(args: argparse.Namespace) -> None:
    """Tail gateway logs from the most appropriate source.

    Order of preference:
      1. systemd journal (if the system service is installed on Linux)
      2. launchd stdout file (macOS)
      3. ``~/.kiro/crew/gateway.log`` (foreground gateway)
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
