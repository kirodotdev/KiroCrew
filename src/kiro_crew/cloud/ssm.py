"""SSM Session Manager primitives for the cloud launcher.

Two kinds of SSM interaction:

1. **Run a command on the instance and read its output** — ``send-command`` +
   poll ``get-command-invocation``. Goes through the :mod:`cloud.aws` chokepoint
   (captured output), so it is unit-testable by mocking ``run_aws``.
2. **Open a long-lived port-forward tunnel** — ``start-session`` with the
   ``AWS-StartPortForwardingSession`` document. This is a streaming child
   process, so it is spawned directly with ``subprocess.Popen`` (not through the
   capture-only chokepoint). The argv builders are pure and testable.

Requires the ``session-manager-plugin`` on the client for #2 (bundled by the
launcher prerequisites); #1 needs only the ``aws`` CLI.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kiro_crew import platform_compat
from kiro_crew.cloud import aws

logger = logging.getLogger(__name__)

# How long to wait for a send-command invocation to finish, and the poll gap.
_CMD_TOTAL_WAIT = 180
_CMD_POLL_INTERVAL = 3

# Indirection so tests can patch out the poll sleep.
_sleep = time.sleep

_PORT_FORWARD_DOC = "AWS-StartPortForwardingSession"
_SESSION_MANAGER_PLUGIN = "session-manager-plugin"
_SESSION_MANAGER_PLUGIN_DOC_URL = (
    "https://docs.aws.amazon.com/systems-manager/latest/userguide/"
    "session-manager-working-with-install-plugin.html"
)

# Valid Unix username shape for the `sudo -u <run_as>` target.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass
class CommandResult:
    """Outcome of an SSM send-command run."""

    status: str  # Success | Failed | TimedOut | ...
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.status == "Success" and self.exit_code == 0


@dataclass
class PluginInstallResult:
    """Outcome of installing the local AWS Session Manager plugin."""

    ok: bool
    message: str = ""


def run_command(
    instance_id: str,
    command: str,
    profile: str = "",
    region: str = "",
    *,
    run_as: str = "ec2-user",
    total_wait: int = _CMD_TOTAL_WAIT,
) -> CommandResult:
    """Run a shell ``command`` on the instance via SSM and return its output.

    Uses ``AWS-RunShellScript``; wraps the command in ``sudo -u <run_as> -i`` so
    it runs as the KiroCrew user with a login shell (PATH, home). Blocks until
    the invocation completes or ``total_wait`` elapses.

    ``run_as`` is charset-validated (Unix username shape) even though every
    caller uses the default today — this is the SSM command chokepoint, so the
    injection surface stays closed if a caller ever threads user input through.
    """
    if not _USERNAME_RE.match(run_as):
        raise aws.AWSError(f"invalid run_as user: {run_as!r}", action="ssm:SendCommand")
    wrapped = _wrap_remote_command(command, run_as)
    send = aws.checked_json(
        [
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            f"commands={_json_str_list([wrapped])}",
        ],
        profile,
        region,
        action="ssm:SendCommand",
    )
    command_id = ""
    if isinstance(send, dict):
        command_id = send.get("Command", {}).get("CommandId", "")
    if not command_id:
        raise aws.AWSError("ssm send-command returned no CommandId", action="ssm:SendCommand")

    deadline = total_wait
    waited = 0
    while True:
        rc, out, err = aws.run_aws(
            [
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
                "--output",
                "json",
            ],
            profile,
            region,
        )
        if rc == 0:
            import json

            try:
                inv = json.loads(out or "{}")
            except json.JSONDecodeError:
                inv = {}
            status = inv.get("Status", "")
            if status in ("Success", "Failed", "Cancelled", "TimedOut"):
                raw_code = inv.get("ResponseCode", -1)
                try:
                    exit_code = int(raw_code)
                except (TypeError, ValueError):
                    exit_code = -1
                return CommandResult(
                    status=status,
                    stdout=inv.get("StandardOutputContent", ""),
                    stderr=inv.get("StandardErrorContent", ""),
                    exit_code=exit_code,
                )
        # "InvocationDoesNotExist" briefly after send — keep polling.
        if waited >= deadline:
            return CommandResult(status="TimedOut", stdout="", stderr=err, exit_code=-1)
        _sleep(_CMD_POLL_INTERVAL)
        waited += _CMD_POLL_INTERVAL


def build_port_forward_argv(
    instance_id: str,
    remote_port: int,
    local_port: int,
    profile: str = "",
    region: str = "",
) -> list[str]:
    """Build the ``aws ssm start-session`` port-forward argv (pure/testable)."""
    argv = [
        "aws",
        "ssm",
        "start-session",
        "--target",
        instance_id,
        "--document-name",
        _PORT_FORWARD_DOC,
        "--parameters",
        f"portNumber={remote_port},localPortNumber={local_port}",
    ]
    if region:
        argv += ["--region", region]
    if profile:
        argv += ["--profile", profile]
    return argv


def build_interactive_session_argv(
    instance_id: str, profile: str = "", region: str = ""
) -> list[str]:
    """Build the plain ``aws ssm start-session`` argv (interactive shell)."""
    argv = ["aws", "ssm", "start-session", "--target", instance_id]
    if region:
        argv += ["--region", region]
    if profile:
        argv += ["--profile", profile]
    return argv


def session_manager_plugin_installed() -> bool:
    """True when the local AWS Session Manager plugin is available."""
    return shutil.which(_SESSION_MANAGER_PLUGIN) is not None


def session_manager_plugin_install_hint() -> str:
    """Human-readable install hint for the local SSM plugin prerequisite."""
    return (
        "session-manager-plugin is not installed locally. Install the AWS "
        f"Session Manager plugin, then rerun `kirocrew cloud connect`: "
        f"{_SESSION_MANAGER_PLUGIN_DOC_URL}"
    )


def install_session_manager_plugin() -> PluginInstallResult:
    """Install the local AWS Session Manager plugin for SSM port-forwarding."""
    if session_manager_plugin_installed():
        return PluginInstallResult(ok=True, message="session-manager-plugin already installed")

    import tempfile

    with tempfile.TemporaryDirectory(prefix="kirocrew-ssm-plugin-") as tmp:
        tmpdir = Path(tmp)
        plan = _session_manager_plugin_install_plan(tmpdir)
        if plan is None:
            return PluginInstallResult(ok=False, message=session_manager_plugin_install_hint())
        url, package_path, commands = plan
        try:
            _download_file(url, package_path)
        except Exception as exc:
            return PluginInstallResult(
                ok=False,
                message=f"Could not download session-manager-plugin package: {exc}",
            )

        for argv in commands:
            rc, _out, err = _run_install_command(argv)
            if rc != 0:
                return PluginInstallResult(ok=False, message=_install_error(argv, err))

    if session_manager_plugin_installed():
        return PluginInstallResult(ok=True, message="session-manager-plugin installed")
    return PluginInstallResult(
        ok=False,
        message=(
            "session-manager-plugin installer completed, but the binary was not found on PATH. "
            f"See {_SESSION_MANAGER_PLUGIN_DOC_URL}"
        ),
    )


def require_session_manager_plugin() -> None:
    """Fail fast before starting an SSM port-forward without the local plugin."""
    if not session_manager_plugin_installed():
        raise aws.AWSError(session_manager_plugin_install_hint(), action="ssm:StartSession")


def open_port_forward(
    instance_id: str,
    remote_port: int,
    local_port: int,
    profile: str = "",
    region: str = "",
) -> subprocess.Popen:
    """Spawn a background SSM port-forward. Returns the live child process.

    The caller owns the process (terminate it to close the tunnel). Not routed
    through :func:`cloud.aws.run_aws` because the session streams for its whole
    lifetime rather than returning captured output.

    Output goes to DEVNULL: the plugin logs a line per connection, and no
    caller drains the pipes (they block on ``wait()``), so PIPE would deadlock
    the tunnel once the OS pipe buffer fills. Tunnel liveness is verified via
    :func:`wait_for_local_port`, not by parsing plugin output.
    """
    # Streaming child — bypasses the run_aws chokepoint, so carry the same
    # human-action guard here (opening a tunnel to the box is a sensitive,
    # human-only action, not something an agent session may do).
    aws.assert_human_action("ssm:StartSession")
    require_session_manager_plugin()
    argv = build_port_forward_argv(instance_id, remote_port, local_port, profile, region)
    logger.info(
        "opening SSM port-forward %s: local %d -> remote %d", instance_id, local_port, remote_port
    )
    return subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )


def kill_port_forward(proc: Optional[subprocess.Popen]) -> None:
    """Tear down a port-forward child AND its ``session-manager-plugin`` child.

    :func:`open_port_forward` uses ``start_new_session=True``, so the ``aws``
    wrapper is a process-group leader and the plugin (which actually holds the
    forwarded local port) is in the same group. ``proc.terminate()`` signals ONLY
    the wrapper — the plugin would survive and keep the port bound. On POSIX we
    therefore signal the whole group (``killpg``); if that's unavailable (or the
    group is already gone) fall back to the single-process kill. Both the
    dashboard tunnel (``connect``) and the login callback tunnel (``login``) go
    through this so neither leaves an orphaned plugin/port behind.

    WINDOWS has no ``os.killpg``/``os.getpgid`` (and silently ignores
    ``start_new_session``), so the group signal is not merely unavailable there --
    it can never succeed, and the fallback ``proc.terminate()`` kills ONLY the
    wrapper, leaving the plugin holding the forwarded port exactly as this
    function exists to prevent. Windows therefore gets its own tree kill via
    ``taskkill /T``, which walks the child chain the way a process group does on
    POSIX.
    """
    if proc is None or proc.poll() is not None:
        return

    def _signal_group(sig: int) -> bool:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            return False

    def _kill_tree_windows() -> bool:
        """``taskkill /T /F`` the wrapper and every process below it.

        /T includes the child tree, /F is forceful (the plugin does not handle a
        graceful console event). Returns False so the caller can still fall back
        if taskkill is missing or refuses.
        """
        # getattr, not proc.pid: this helper accepts any Popen-LIKE object, and the
        # POSIX branch already tolerates one without a pid (its `_signal_group`
        # catches AttributeError and falls back to terminate()). Without the same
        # tolerance here, a caller passing a lightweight stand-in works on Linux
        # and raises on Windows.
        pid = getattr(proc, "pid", None)
        if pid is None:
            return False
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        # taskkill returns non-zero when the pid already exited, which is the
        # desired end state, so success is judged by the process being gone.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return False
        return True

    if os.name == "nt":
        if _kill_tree_windows():
            return
        # Fall through: better a parent-only kill than nothing.

    try:
        if not _signal_group(platform_compat.SIGTERM):
            proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        # Signal numbers come from platform_compat, not `signal`: Windows has no
        # `signal.SIGKILL`, so naming it here raises AttributeError while
        # evaluating the call argument — inside this handler's own try, which
        # swallows it — and `proc.kill()` would never run. That silently turns
        # the escalation into a no-op on the one platform that reaches it via
        # the taskkill fall-through, leaving the plugin holding the port.
        try:
            if not _signal_group(platform_compat.SIGKILL):
                proc.kill()
        except Exception:
            pass


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if nothing is already listening on ``host:port``.

    Pre-tunnel guard: if a port is already occupied by an unrelated process,
    the SSM child will fail to bind, but a probe against the port would still
    succeed against that foreign listener — so we must refuse before minting a
    token or spawning, rather than send the dashboard JWT to a stranger.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        # connect_ex == 0 means something already accepted -> port is occupied.
        return sock.connect_ex((host, port)) != 0


def wait_for_local_port(
    port: int,
    host: str = "127.0.0.1",
    timeout: float = 30.0,
    proc: Optional[subprocess.Popen] = None,
) -> bool:
    """Poll until ``host:port`` accepts a TCP connection (tunnel is up).

    When ``proc`` is given, stop early if the child has exited: a listener that
    only appears after the SSM port-forward died is not our tunnel, and
    returning true for it would expose the dashboard token to that process.
    """
    import socket

    deadline_iters = int(timeout / 0.5)
    for _ in range(max(1, deadline_iters)):
        if proc is not None and proc.poll() is not None:
            return False  # the SSM child exited — any listener here is not ours
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                return True
        _sleep(0.5)
    return False


def instance_is_managed(instance_id: str, profile: str = "", region: str = "") -> bool:
    """True if SSM reports the instance as a managed, online node (Agent up)."""
    rc, out, _err = aws.run_aws(
        [
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
            "--query",
            "InstanceInformationList[0].PingStatus",
            "--output",
            "text",
        ],
        profile,
        region,
    )
    return rc == 0 and out.strip() == "Online"


# --- small helpers ---------------------------------------------------------


def _shq(s: str) -> str:
    """Single-quote a string for safe embedding in a bash -lc argument."""
    return "'" + s.replace("'", "'\\''") + "'"


def _wrap_remote_command(command: str, run_as: str) -> str:
    """Wrap a (possibly multi-line) script for reliable execution over SSM.

    SSM's ``AWS-RunShellScript`` mangles multi-line command strings (it strips
    the newlines, gluing statements together — ``set +eKIRO=...`` — which breaks
    any real script). To be newline- and quoting-proof we base64-encode the
    whole script and decode+run it on the box under ``sudo -u <run_as> -i bash``.
    """
    import base64

    b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    # Single-line, no embedded newlines: SSM can't mangle it.
    return f"echo {b64} | base64 -d | sudo -u {run_as} -i bash"


def _session_manager_plugin_install_plan(
    tmpdir: Path,
) -> tuple[str, Path, list[list[str]]] | None:
    """Return (download_url, package_path, install_commands) for this platform."""
    system = platform.system()
    arch = _normalized_arch()
    if system == "Darwin":
        url_arch = "mac_arm64" if arch == "arm64" else "mac"
        package_path = tmpdir / "session-manager-plugin.pkg"
        return (
            f"https://s3.amazonaws.com/session-manager-downloads/plugin/latest/{url_arch}/"
            "session-manager-plugin.pkg",
            package_path,
            [
                ["sudo", "installer", "-pkg", str(package_path), "-target", "/"],
                ["sudo", "mkdir", "-p", "/usr/local/bin"],
                [
                    "sudo",
                    "ln",
                    "-sf",
                    "/usr/local/sessionmanagerplugin/bin/session-manager-plugin",
                    "/usr/local/bin/session-manager-plugin",
                ],
            ],
        )

    if system != "Linux":
        return None

    if shutil.which("dpkg"):
        url_arch = "ubuntu_arm64" if arch == "arm64" else "ubuntu_64bit"
        package_path = tmpdir / "session-manager-plugin.deb"
        return (
            f"https://s3.amazonaws.com/session-manager-downloads/plugin/latest/{url_arch}/"
            "session-manager-plugin.deb",
            package_path,
            [["sudo", "dpkg", "-i", str(package_path)]],
        )

    if shutil.which("rpm"):
        url_arch = "linux_arm64" if arch == "arm64" else "linux_64bit"
        package_path = tmpdir / "session-manager-plugin.rpm"
        installer = shutil.which("dnf") or shutil.which("yum")
        if installer:
            commands = [["sudo", installer, "install", "-y", str(package_path)]]
        else:
            commands = [["sudo", "rpm", "-Uvh", str(package_path)]]
        return (
            f"https://s3.amazonaws.com/session-manager-downloads/plugin/latest/{url_arch}/"
            "session-manager-plugin.rpm",
            package_path,
            commands,
        )

    return None


def _normalized_arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x86_64"


def _download_file(url: str, dest: Path) -> None:
    """Download a package file using stdlib HTTPS."""
    import urllib.request

    # URLs come only from the fixed per-platform install plan (hardcoded
    # https://s3.amazonaws.com/session-manager-downloads/...), never from user
    # input; the scheme check keeps file:// et al. out even if that changes.
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS download URL: {url}")
    with urllib.request.urlopen(url, timeout=120) as response, dest.open("wb") as fh:  # nosemgrep
        shutil.copyfileobj(response, fh)


def _run_install_command(argv: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, check=False)  # noqa: S603 — fixed platform plan
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    return proc.returncode, "", ""


def _install_error(argv: list[str], err: str) -> str:
    command = " ".join(argv)
    detail = err.strip().splitlines()[-1] if err.strip() else "installer returned non-zero"
    return f"`{command}` failed: {detail}"


def _json_str_list(items: list[str]) -> str:
    """Render a JSON array of strings for the --parameters commands=[...] form."""
    import json

    return json.dumps(items)
