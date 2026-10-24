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
    return shutil.which("kiro-cli-chat") or ""


def _doctor_kiro_chat_delegate(kiro: str | None, issues: list[str], *, required: bool) -> None:
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
    """
    if not kiro:
        return
    target = _chat_delegate_target(kiro)
    if target is None:
        return
    reason: str | None = None
    version = ""
    if not target:
        reason = "no kiro-cli-chat in ~/.local/bin or on PATH"
    else:
        try:
            result = subprocess.run(
                [target, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_CHAT_DELEGATE_PROBE_TIMEOUT_SECS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            reason = f"{target} --version did not answer in {_CHAT_DELEGATE_PROBE_TIMEOUT_SECS}s"
        except OSError as exc:
            # A dangling link lands here as FileNotFoundError: the link exists,
            # the release it points at was removed.
            reason = f"{target} cannot be launched ({exc.strerror or type(exc).__name__})"
        else:
            if result.returncode == 0:
                version = (result.stdout or result.stderr).strip()
            else:
                reason = f"{target} --version exited {result.returncode}"
    if reason is None:
        print(f"  kiro-cli-chat: ✅ {version or target}")
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


def _doctor_gateway_reachability(dashboard: DashboardBinding, issues: list[str]) -> None:
    """The ``Connectivity`` rows after the kiro-cli one: is the gateway up, and does
    it enforce token auth off loopback."""
    _host, _port, _local = dashboard.host, dashboard.port, dashboard.local
    # Check if gateway is running — connect to 127.0.0.1 (loopback)
    # to avoid DNS resolution issues with the configured hostname.
    # Any HTTP response (even 401/403 from token auth) means the gateway is up.
    is_remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))

    if _port:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{_port}/api/status")
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- loopback host literal plus a fixed internal path; the only interpolated value is the gateway port from config/env, so no scheme or host is reachable from input  # noqa: E501
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            print(f"  gateway:     ✅ running (uptime {data.get('uptime', '?')})")
        except urllib.error.HTTPError as he:
            # 401/403 means gateway is running but requires token auth
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
