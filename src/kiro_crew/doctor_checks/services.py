"""Service rows of ``kirocrew doctor``: how this host runs and serves the gateway.

The installed service definition and the environment it runs with, the per-user
service manager pods need, the dashboard's bind and auth, and whether the gateway
answers on its port.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig


def _doctor_managed_service_policy(issues: list[str]) -> None:
    """Surface installed service definitions that predate launch-class policy."""
    state = cli_doctor.service_controller.installed_service_has_managed_marker()
    if state is None:
        return
    print("\nManaged Service")
    if state:
        print("  watchdog:    ✅ managed-service policy marker installed")
        return
    print("  watchdog:    ⚠️  installed definition predates managed-service defaults")
    print("               Fix: run `kirocrew service install` once, then restart the service")
    issues.append("managed service definition is outdated")


def _doctor_pod_session_bus(issues: list[str]) -> None:
    """Report whether pods can reach the per-user service manager.

    Socket existence is not reachability: an outer sandbox can leave
    ``$XDG_RUNTIME_DIR/bus`` visible while denying ``connect(2)``. The shared
    pod probe keeps that state separate from an absent user session bus and from
    an unclassified systemctl failure.

    A host without a per-user manager unit is not applicable: enabling linger
    cannot start a unit that does not exist. This advisory check precedes the
    connection probe; the execution gate remains permissive when a bus exists.

    Advisory only. Pods are an optional development feature, so an unavailable
    backend never changes doctor's exit code. Doctor reports the action but does
    not enable linger or change the caller's sandbox.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    print("\nPods")
    if not sys.platform.startswith("linux"):
        print(
            f"  session bus: ⏹ not applicable ({sys.platform} — pods are "
            "Linux `systemd --user` only)"
        )
        return
    if shutil.which("systemctl") is None:
        print("  session bus: ⏹ not applicable (no `systemctl` on PATH)")
        return

    # Local import keeps the pod package out of every other CLI command's import
    # graph. pod.runtime imports no CLI module, so this remains circular-safe.
    from kiro_crew.pod.runtime import (
        USER_BUS_NO_SESSION,
        USER_BUS_REACHABLE,
        USER_BUS_SANDBOXED_AWAY,
        probe_user_bus,
        user_bus_failure_message,
        user_manager_unit,
    )

    uid = getattr(os, "getuid", lambda: -1)()
    if user_manager_unit(uid) is None:
        # Probed before the bus on purpose: a stray session dbus-daemon can
        # create a socket without a per-user manager. Doctor only reports;
        # require_systemd() stays permissive when a bus exists so an incomplete
        # unit-path probe cannot prevent execution on a working host.
        print(
            "  session bus: ⏹ not applicable (no systemd per-user manager on this "
            "host — pods are unavailable)"
        )
        print("               Enterprise Linux 7 derivatives (RHEL 7, CentOS 7,")
        print("               Amazon Linux 2) ship systemd without `user@.service`,")
        print("               so `loginctl enable-linger` cannot help. Use")
        print("               `./dev-backend.sh` to run a worktree gateway here.")
        return

    result = probe_user_bus()
    if result.status != USER_BUS_REACHABLE:
        label = {
            USER_BUS_NO_SESSION: "no user session bus",
            USER_BUS_SANDBOXED_AWAY: "sandboxed away",
        }.get(result.status, "unreachable")
        print(f"  session bus: ❌ {label} ({result.socket})")
        for line in user_bus_failure_message(result).splitlines():
            print(f"               {line}")
        print("               Everything else works.")
        return

    print(f"  session bus: ✅ {result.socket}")
    user = (
        os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or str(getattr(os, "getuid", lambda: -1)())
    )
    if cli_doctor._linger_enabled(user) is False:
        print("  linger:      ⚠️  disabled — the per-user systemd instance exits on logout,")
        print(f"               taking running pods with it. Fix: loginctl enable-linger {user}")


def _doctor_headless_auth(issues: list[str]) -> None:
    """Report an API-key credential the INSTALLED service cannot see.

    This is the one place the contradiction is visible in a single output: the
    ``kiro login`` line above runs ``whoami`` with the inherited environment and
    reports signed in, while the dashboard's readiness gate reads the gateway's
    own environment and reports signed out. Install-time is too early to be the
    only report — the symptom surfaces when the service is ALREADY installed (a
    key added to a shell profile afterwards, a host re-provisioned from a
    snapshot, an operator who reaches the docs only after hitting the wall), and
    none of those orderings run ``service install`` again.

    Gated on a service definition existing, which is what keeps the report
    plausible. Without one the gateway runs in the foreground and inherits this
    very shell, so the credential DOES reach it and warning here would be a
    false positive on a working host.

    Advisory only (never appended to ``issues``, like the pod-session-bus and
    memory-pressure probes): ``issues`` is doctor's exit-code channel, so an
    entry here makes the verdict ❌ and exits non-zero — a claim this shell
    cannot establish. ``service_environment()`` bakes ``HOME``, so a service on
    a host that ran ``kiro-cli login`` before the key was exported resolves that
    credential store and is healthy while the check still fires; and a unit path
    proves a definition exists on disk, not that the unit is the gateway
    currently serving, so a stopped unit beside a foreground ``kirocrew gateway``
    also reads as broken. Reporting the exposure is right; failing doctor on a
    host where sign-in works is the same contradiction-with-reality this
    diagnostic exists to surface, one layer up.

    Best-effort like the probes around it: a failure to read the environment or
    the unit path must not fail ``doctor``, whose job is to report.
    """
    del issues  # advisory-only diagnostic; keeps the call-site signature uniform
    try:
        if cli_doctor.service_controller.installed_unit_path() is None:
            return
        warning = cli_doctor.common_service.headless_auth_warning()
    except Exception:
        return
    if not warning:
        return
    print("  kiro key:    ⚠️  set here, but the installed service cannot see it")
    for line in warning.splitlines():
        print(f"{render._INDENT}{line.strip()}" if line.strip() else "")


#: Seconds ``kiro-cli-chat --version`` may take. A local read of a resolved
#: binary, so it gets the same short leash as the ``kiro-cli --version`` row.
_CHAT_DELEGATE_PROBE_TIMEOUT_SECS = 5

#: The one remedy for a chat delegate that will not launch. Every observed case
#: (a ``~/.local/bin/kiro-cli-chat`` link left pointing at a removed release, a
#: partial upgrade) was fixed by reinstalling, and nothing Crew can change helps.
CHAT_DELEGATE_BROKEN_FIX = "reinstall Kiro CLI (rerun its installer), then rerun kirocrew doctor"


def _chat_delegate_target(kiro: str) -> str | None:
    """The ``kiro-cli-chat`` a session started through *kiro* will exec, or ``None``.

    ``kiro-cli --version`` is answered by the launcher itself, so it passes while
    the chat binary every session actually runs cannot start (the field report:
    ``failed to launch kiro-cli-chat``, surfaced as ``process exited (rc=None)``).
    The launcher finds the chat binary in ``$HOME/.local/bin`` and then on
    ``PATH``, never in its own directory (see ``kiro_cli.BUNDLED_KIRO_CLI_ENTRY``),
    so this looks in the same two places in the same order. A path that merely
    EXISTS as a link counts -- a dangling link is the broken install this check
    is for, and ``os.path.lexists`` keeps it in view where a PATH lookup would
    skip past it to a different copy the launcher never reaches.

    The exception is a macOS launcher inside ``Kiro CLI.app/Contents/MacOS``: it
    finds ``.app/Contents/MacOS/`` in its own path and execs the chat binary
    beside it (``kiro_prerequisite_candidate``), so that sibling is the target.

    *kiro* already named ``kiro-cli-chat`` (the desktop bundle, or an operator's
    ``KIROCREW_KIRO_BIN``) is probed as itself. Any other name is a wrapper whose
    delegate is unknown, and Windows ships one self-contained ``kiro-cli.exe``;
    both return ``None`` so the row stays silent rather than guessing. A
    ``kiro-cli`` launcher with no chat binary anywhere returns ``""``: that
    launcher fails every ``chat``/``acp`` start, so the caller reports it broken.
    """
    if sys.platform == "win32" or not kiro:
        return None
    name = os.path.basename(kiro)
    if name == "kiro-cli-chat":
        return kiro
    if name != "kiro-cli":
        return None
    launcher_dir = os.path.dirname(kiro)
    if sys.platform == "darwin" and launcher_dir.endswith(".app/Contents/MacOS"):
        return os.path.join(launcher_dir, "kiro-cli-chat")
    local_chat = os.path.join(os.path.expanduser("~"), ".local", "bin", "kiro-cli-chat")
    if os.path.lexists(local_chat):
        return local_chat
    # Sessions launch with the augmented search path (``acp.launch`` sets it on
    # the spawn env), so the fallback searches that path too: a delegate in
    # ``/usr/local/bin`` is found by sessions even when doctor's own shell
    # leaves it off ``PATH``. Read at call time: a doctor family binds no
    # project name at module scope.
    from kiro_crew import env

    search_path = env.augmented_path(os.environ.get("PATH", ""))
    return shutil.which("kiro-cli-chat", path=search_path) or ""


def kas_relay_entry(kiro: str | None) -> str | None:
    """The ``kiro-cli-chat`` a relay KAS spawn execs in place of *kiro*, or ``None``.

    Mirrors the swap in ``KasHarness.resolve_spawn``: when Crew's vault holds a
    usable identity, the relay enters through the ``kiro-cli-chat`` beside the
    resolved launcher (:func:`kiro_cli.chat_sibling`) rather than through the
    launcher, so the launcher's ``~/.local/bin`` delegate is not on that path.
    ``None`` whenever the spawn keeps the launcher: no usable vault identity,
    the operator's own ``KIROCREW_KIRO_BIN`` (never swapped), or no executable
    sibling. A plain read of the vault and a ``stat``; never raises.
    """
    if not kiro:
        return None
    try:
        # Local imports: the vault reader pulls in cryptography, which the rest
        # of doctor's import graph does not need. The override rule is the
        # harness's own, so the two cannot drift.
        from kiro_crew.agent_sdk.kas_bundle import kas_operator_override
        from kiro_crew.auth.bridge import vault_holds_identity
        from kiro_crew.kiro_cli import chat_sibling

        if kas_operator_override(kiro, os.environ):
            return None
        sibling = chat_sibling(kiro)
        if not sibling or not vault_holds_identity(warn=False):
            return None
    except Exception:  # noqa: BLE001 - a diagnostic probe must not fail doctor
        return None
    return sibling


def _probe_chat_binary(probe: str) -> tuple[str | None, str]:
    """Run ``<probe> --version``: ``(None, version)`` when it launches, else ``(reason, "")``."""
    if not probe:
        return "no kiro-cli-chat in ~/.local/bin or on PATH", ""
    try:
        result = subprocess.run(
            [probe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CHAT_DELEGATE_PROBE_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"{probe} --version did not answer in {_CHAT_DELEGATE_PROBE_TIMEOUT_SECS}s", ""
    except OSError as exc:
        # A dangling link lands here as FileNotFoundError: the link exists, the
        # release it points at was removed.
        return f"{probe} cannot be launched ({exc.strerror or type(exc).__name__})", ""
    if result.returncode != 0:
        return f"{probe} --version exited {result.returncode}", ""
    return None, (result.stdout or result.stderr).strip()


def _doctor_kiro_chat_delegate(
    kiro: str | None,
    issues: list[str],
    *,
    required: bool,
    relay_entry: str | None = None,
    kiro_role: bool = False,
) -> None:
    """Run ``kiro-cli-chat --version`` and report it when it cannot launch.

    *required* is whether the harness selected for either role (the main
    backend or the crewmate backend) spawns kiro-cli at all. When one does, a
    broken delegate fails every session on that role, so it is appended to ``issues``
    and doctor exits non-zero. When it does not (Claude, a KAS bundle, any other
    harness), the same failure is printed as advisory and left out of
    ``issues``, like the ``kiro-cli --version`` row: failing doctor over an
    unused backend would make a working host red. Silent when there is no
    kiro-cli, on Windows, and for a wrapper whose delegate cannot be named
    (:func:`_chat_delegate_target`).

    *relay_entry* is the binary relay KAS sessions exec when Crew's vault signs
    them in (:func:`kas_relay_entry`), passed whenever relay KAS is selected for
    either role. *kiro_role* is whether the Kiro CLI backend is selected too.
    Each binary a selected role runs is probed on its own: the launcher's
    delegate when *kiro_role* is set or there is no *relay_entry*, and
    *relay_entry* whenever it is given. Any of those failing is fatal, so a
    healthy delegate never hides a broken relay entry. A broken delegate that
    no selected role runs (relay KAS alone, entering through a working
    *relay_entry*) is reported as advisory, since only the launcher's own
    non-session paths still reach it.
    """
    if not kiro:
        return
    target = _chat_delegate_target(kiro)
    if target is None:
        return
    relay = relay_entry if required and relay_entry else None
    delegate_reason, version = _probe_chat_binary(target)
    relay_reason: str | None = None
    if relay and relay != target:
        relay_reason, relay_version = _probe_chat_binary(relay)
        version = version or relay_version
    elif relay:
        relay_reason = delegate_reason
    delegate_needed = required and (relay is None or kiro_role)
    fatal = [r for r in (delegate_reason if delegate_needed else None, relay_reason) if r]
    if fatal:
        print(f"  kiro-cli-chat: ❌ Kiro CLI is broken: {'; '.join(fatal)}")
        print(f"{render._INDENT}Fix: {CHAT_DELEGATE_BROKEN_FIX}")
        issues.append("Kiro CLI is broken (kiro-cli-chat will not launch): reinstall Kiro CLI")
        return
    reason = delegate_reason
    if reason is None:
        print(f"  kiro-cli-chat: ✅ {version or target}")
        return
    if relay:
        print(
            f"  kiro-cli-chat: ⚠️  Kiro CLI is broken: {reason} (sessions signed in "
            f"through Kiro Crew launch {relay}, which works)"
        )
        print(f"{render._INDENT}Fix: {CHAT_DELEGATE_BROKEN_FIX}")
        return
    if not required:
        print(
            f"  kiro-cli-chat: ⚠️  Kiro CLI is broken: {reason} (not used by the selected backends)"
        )
        print(f"{render._INDENT}Fix: {CHAT_DELEGATE_BROKEN_FIX}")
        return
    print(f"  kiro-cli-chat: ❌ Kiro CLI is broken: {reason}")
    print(f"{render._INDENT}Fix: {CHAT_DELEGATE_BROKEN_FIX}")
    issues.append("Kiro CLI is broken (kiro-cli-chat will not launch): reinstall Kiro CLI")


class DashboardBinding(NamedTuple):
    """What the ``Configuration`` section resolved about the dashboard.

    The later sections read these rather than resolving them again: the Slack and
    Discord sections the credentials, and the reachability probe the host, the
    port and the bind.
    """

    host: str
    port: int | None
    creds: dict[str, str]
    has_slack: bool
    local: bool


def _doctor_configuration(cfg: KiroCrewConfig, issues: list[str]) -> DashboardBinding:
    """Render the ``Configuration`` section: the config dir, the agent defaults, and
    the dashboard's URL, bind and auth mode."""
    print("\nConfiguration")
    cfg_dir = cli_doctor.config_dir()
    if cfg_dir.exists():
        print(f"  config dir:  ✅ {cfg_dir}")
    else:
        print(f"  config dir:  📁 {cfg_dir} (will be created)")
    print(f"  provider:    {cfg.agent.provider}")
    print(f"  model:       {cfg.agent.model}")
    print(f"  approval:    {cfg.agent.approval_mode}")
    _host: str = ""
    _port: int | None = None
    try:
        _host, _port = cli_doctor.parse_dashboard_url(cfg.dashboard.url)
    except Exception:
        print("  dashboard:   ⚠️  cannot parse dashboard URL from config")
        issues.append("dashboard URL misconfigured")
    _display_host = _host or "localhost"
    if _port:
        print(f"  dashboard:   http://{_display_host}:{_port}")

    # Dashboard auth mode. Both this section and the Slack section key off the
    # SAME credential read and the same token pair, so the two can never
    # disagree about whether Slack is configured.
    creds = cfg.load_credentials()
    _has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))
    _local = cli_doctor.is_local_only(_host, _has_slack)
    if _local:
        print("  bind:        127.0.0.1 (local-only, SSH tunnel for remote)")
        print(
            "  auth:        token required — loopback is not exempt"
            " (CLI/MCP use the local secret)"
        )
    else:
        print("  bind:        0.0.0.0 (all interfaces)")
        print("  auth:        ✅ token auth required (via !dashboard)")
        if not _has_slack:
            print("  auth:        ⚠️  Slack not configured — token generation unavailable")
            issues.append("dashboard auth: remote bind without Slack")
    return DashboardBinding(_host, _port, creds, _has_slack, _local)


def _running_gateway_version(port: int) -> str | None:
    """The version the gateway on ``port`` is running, or None when it does not say.

    Read from ``GET /api/health``, not ``/api/status``: ``/api/status`` needs the
    dashboard token, which this bare request does not carry, while ``/api/health``
    is token-exempt and adds ``version`` for a direct-local caller that sends a
    served ``Host`` (``handlers.core._liveness_payload``), which a
    ``127.0.0.1:<port>`` request is. Any failure, or a payload without a string
    ``version``, answers None so the row reads unknown rather than guessing.
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- loopback host literal plus a fixed internal path; the only interpolated value is the gateway port from config/env, so no scheme or host is reachable from input  # noqa: E501
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _doctor_running_version(port: int, issues: list[str]) -> None:
    """The ``running:`` row: the gateway's own version next to the on-disk one.

    The on-disk version is the code this doctor imported (``cli_doctor._mc_version``).
    A gateway started before an update keeps serving the old code until it
    restarts, so a mismatch is an issue whose remedy is a restart.
    """
    on_disk = cli_doctor._mc_version
    running = _running_gateway_version(port)
    if running is None:
        print(f"  running:     ⚠️  gateway version unknown (on disk {on_disk})")
    elif running == on_disk:
        print(f"  running:     ✅ {running} (same as on disk)")
    else:
        print(f"  running:     ⚠️  {running}, on disk {on_disk} (restart needed)")
        print(f"{render._INDENT}Fix: `kirocrew restart` (the gateway still runs the old code)")
        issues.append("gateway restart needed (running version differs from on-disk)")


def _doctor_gateway_reachability(dashboard: DashboardBinding, issues: list[str]) -> None:
    """The ``Connectivity`` rows after the kiro-cli one: is the gateway up, and does
    it enforce token auth off loopback."""
    _host, _port, _local = dashboard.host, dashboard.port, dashboard.local
    # Check if gateway is running — connect to 127.0.0.1 (loopback)
    # to avoid DNS resolution issues with the configured hostname.
    # Any HTTP response (even 401/403 from token auth) means the gateway is up.
    is_remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))

    if _port:
        gateway_up = False
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{_port}/api/status")
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- loopback host literal plus a fixed internal path; the only interpolated value is the gateway port from config/env, so no scheme or host is reachable from input  # noqa: E501
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            print(f"  gateway:     ✅ running (uptime {data.get('uptime', '?')})")
            gateway_up = True
        except urllib.error.HTTPError as he:
            # 401/403 means gateway is running but requires token auth
            gateway_up = True
            if he.code in (401, 403):
                print("  gateway:     ✅ running (token auth enabled)")
            else:
                print(f"  gateway:     ⚠️  HTTP {he.code}")
        except (urllib.error.URLError, OSError) as e:
            # Only a bare read timeout (the connection was accepted and the
            # gateway then stalled -- a loop thread starved of the GIL) means it
            # is up but busy. A connect-phase failure is reported as not running:
            # this read uses a 2 s deadline, which on native Windows coincides
            # with the SYN-retry before a closed loopback port reports a refusal,
            # so a connect timeout here must not read busy. The non-doctor sites,
            # whose deadlines sit past that window, use loopback_http.deadline_expired.
            if isinstance(e, TimeoutError):
                print("  gateway:     ⏳ busy (up but did not answer)")
            else:
                print("  gateway:     ⏹  not running")
        except Exception:
            print("  gateway:     ⚠️  running but returned unexpected response")
            gateway_up = True

        if gateway_up:
            _doctor_running_version(_port, issues)

        # SSH tunnel hint for remote hosts
        if is_remote:
            mh = cli_doctor.machine_hostname() or "this-host"
            print("\n  💡 Remote access: Run on your LOCAL machine:")
            print(f"     ssh -NL {_port}:localhost:{_port} {mh}")
            print("     Then run: kirocrew token")

    # Verify token auth is enforced on non-loopback (security check)
    if _port and not _local:
        if not _host:
            issues.append("cannot verify dashboard auth (host unknown)")
        else:
            try:
                ext_req = urllib.request.Request(f"http://{_host}:{_port}/api/status")
                try:
                    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- reaching the operator's OWN configured dashboard host is the test: this asserts token auth is enforced off loopback. The scheme is a literal and the host comes from dashboard.url, not from input  # noqa: E501
                    with urllib.request.urlopen(ext_req, timeout=2) as resp:
                        # 200 without token = auth is NOT enforced
                        print("  auth check:  ❌ external access allowed without token!")
                        issues.append("dashboard auth: no token required on external interface")
                except urllib.error.HTTPError as he:
                    if he.code in (401, 403):
                        print("  auth check:  ✅ token required on external interface")
                    else:
                        print(f"  auth check:  ⚠️  HTTP {he.code}")
            except Exception:
                print("  auth check:  ⏭  could not reach external interface")
