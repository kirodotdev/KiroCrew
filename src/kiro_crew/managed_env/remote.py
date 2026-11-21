"""Run a bash script on a managed environment over ssh, and probe its gateway.

The ``crew-sbx-<id>`` alias comes from the managed block :mod:`.ssh_config` writes: user
``root``, no key, and a ``ProxyCommand`` that mints a fresh session per ssh run. The
script travels on stdin to ``bash -s``, never on the command line, so nothing in it is
parsed by the local shell or shows up in a process listing.

**Data on /projects.** The ssh shell is root with ``HOME=/root``, which a maintenance
restart wipes; only ``/projects`` survives, and the boot hook runs with
``XDG_DATA_HOME=/projects/.xdg-data``. So :func:`run_script` exports
:data:`XDG_DATA_HOME` on every exec, and a caller cannot override it.

**Crew's launcher first.** The install step puts Crew's launcher (and the Python it links)
in ``/projects/.crew/bin`` (:data:`.install.CREW_BIN`). That launcher carries
``KIROCREW_HOME`` and survives a restart, so every exec puts that dir first on the PATH
(:data:`PATH_LINE`).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from kiro_crew.managed_env.install import CREW_BIN

#: The first line of every exec: Crew's bin dir on /projects, then the user tool dirs a
#: non-interactive ``ssh host cmd`` lacks.
PATH_LINE = f'export PATH="{CREW_BIN}:$HOME/.local/bin:$PATH"'

#: The data root on a managed environment (the boot hook's value).
XDG_DATA_HOME = "/projects/.xdg-data"
#: The gateway's port on the managed environment (the registry row's ``remote_port``).
GATEWAY_PORT = 5476
PROBE_EVERY_SECS = 2
PROBE_UP_TO_SECS = 120

_SSH_NOISE = ("Pseudo-terminal will not be allocated", "Warning: Permanently added")


@dataclass(frozen=True)
class RemoteResult:
    returncode: int
    stdout: str
    stderr: str
    #: ssh was killed at the caller's timeout, so what the remote printed is lost.
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def last_line(self, prefix: str = "") -> str:
        """The last stdout line that starts with *prefix* (any line when empty)."""
        for line in reversed(self.stdout.splitlines()):
            if line.startswith(prefix):
                return line.strip()
        return ""


SshRunner = Callable[[list, str, float], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: list, stdin: str, timeout: float) -> "subprocess.CompletedProcess[str]":
    # Spawn ssh the way the core's tunnel and token mint do, so a gateway started with a
    # short PATH still finds the ProxyCommand's tools.
    from kiro_crew.instances.token_mint import ssh_spawn_argv_env

    argv, env = ssh_spawn_argv_env(argv)
    # Bytes, not text mode: a text-mode stdin on Windows turns every "\n" into "\r\n".
    res = subprocess.run(
        argv,
        env=env,
        input=stdin.encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(
        res.args,
        res.returncode,
        res.stdout.decode("utf-8", errors="replace"),
        res.stderr.decode("utf-8", errors="replace"),
    )


def ssh_argv(host: str) -> list:
    from kiro_crew.managed_env.engine import sandbox_id_from_host

    if not sandbox_id_from_host(host):
        raise ValueError(f"not a sandbox ssh alias: {host!r}")
    return [
        "ssh", "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=30",
        "-o", "ServerAliveInterval=15",
        host,
        "bash -s",
    ]  # fmt: skip


def run_script(
    host: str,
    script: str,
    *,
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[SshRunner] = None,
) -> RemoteResult:
    """Run *script* on the sandbox *host*; a transport failure is a 255 result."""
    lines = [PATH_LINE, f"export XDG_DATA_HOME={XDG_DATA_HOME}"]
    for key, value in (env or {}).items():
        if key == "XDG_DATA_HOME" or not key.replace("_", "").isalnum():
            raise ValueError(f"bad environment variable name: {key!r}")
        lines.append(f"export {key}={shlex.quote(value)}")
    body = "\n".join(lines) + "\n" + script
    try:
        res = (runner or _default_runner)(ssh_argv(host), body, timeout)
    except FileNotFoundError:
        return RemoteResult(255, "", "ssh was not found on this machine")
    except subprocess.TimeoutExpired:
        return RemoteResult(
            255, "", f"ssh to {host} timed out after {int(timeout)}s", timed_out=True
        )
    except OSError as exc:
        return RemoteResult(255, "", f"ssh to {host} failed: {type(exc).__name__}")
    err = "\n".join(ln for ln in (res.stderr or "").splitlines() if not ln.startswith(_SSH_NOISE))
    return RemoteResult(res.returncode, res.stdout or "", err)


#: ``GET /api/health`` on the sandbox's own loopback with bash alone (no curl needed).
HEALTH_SCRIPT = f"""
set +e
exec 3<>/dev/tcp/127.0.0.1/{GATEWAY_PORT} || exit 1
printf 'GET /api/health HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\nConnection: close\\r\\n\\r\\n' >&3
timeout 5 cat <&3
""".strip()


def _health_ok(res: RemoteResult) -> bool:
    head, _, body = res.stdout.partition("\r\n\r\n")
    if not head.startswith("HTTP/") or " 200" not in head.splitlines()[0]:
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def probe_health(
    run: Callable[..., RemoteResult],
    host: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    every: float = PROBE_EVERY_SECS,
    up_to: float = PROBE_UP_TO_SECS,
) -> bool:
    """Ask the gateway for ``/api/health`` every *every* s for up to *up_to* s."""
    deadline = clock() + up_to
    while True:
        try:
            if _health_ok(run(host, HEALTH_SCRIPT, timeout=15)):
                return True
        except Exception:  # noqa: BLE001 - a failed probe is "not yet", never a raise
            pass
        if clock() >= deadline:
            return False
        sleep(every)


def map_boot_state(line: str) -> str:
    """The boot hook's last ``READY ...`` line as ``ready`` / ``needs_signin`` / ``unknown``."""
    parts = (line or "").split()
    if len(parts) >= 2 and parts[0] == "READY":
        if parts[1] == "signed-in":
            return "ready"
        if parts[1] in ("needs-login", "needs_signin"):
            return "needs_signin"
    return "unknown"
