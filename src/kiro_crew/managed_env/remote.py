"""Run a script on a managed environment over ssh, and probe its gateway.

The ``crew-sbx-<id>`` alias comes from the block :mod:`.ssh_config` writes: user
``root``, no key, and a ``ProxyCommand`` that mints a fresh session per ssh run. The
script travels on stdin to ``sh -s``, never on the command line, so nothing in it is
parsed by the local shell or shows up in a process listing.

Only ``/projects`` survives a restart of the environment, so every script runs with
:data:`XDG_DATA_HOME` there and Crew's launcher (:data:`WRAPPER`) first on the PATH.

**A lone ssh exit 255.** The environment's ssh server can send all of a command's output
and then exit 255 with no message. An idempotent script (``retry=True``) is run once
more on such an answer. A 255 that carries a ``mint-and-attach:`` line is a failed
connect, reported at once, so a failed mint never costs a second session.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

SSH_HOST_PREFIX = "crew-sbx-"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

ROOT = "/projects"
XDG_DATA_HOME = f"{ROOT}/.xdg-data"
CREW_BIN = f"{ROOT}/.crew/bin"
#: Crew's launcher on /projects; the Remote Crew row's ``remote_bin``.
WRAPPER = f"{CREW_BIN}/kirocrew"
#: The gateway's port on the environment (the registry row's ``remote_port``).
GATEWAY_PORT = 5476
PROBE_EVERY_SECS = 2
PROBE_UP_TO_SECS = 120
SSH_FAILED_EXIT = 255
_SSH_NOISE = ("Pseudo-terminal will not be allocated", "Warning: Permanently added")
_MINT_LINE = "mint-and-attach:"


def ssh_host_for(exec_environment_id: str) -> str:
    if not _ID_RE.fullmatch(exec_environment_id or ""):
        raise ValueError("not an environment id")
    return SSH_HOST_PREFIX + exec_environment_id


def sandbox_id_from_host(ssh_host: str) -> str:
    """The environment id of a ``crew-sbx-<id>`` alias, or ``""``."""
    if not isinstance(ssh_host, str) or not ssh_host.startswith(SSH_HOST_PREFIX):
        return ""
    sid = ssh_host[len(SSH_HOST_PREFIX) :]
    return sid if _ID_RE.fullmatch(sid) else ""


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


SshRunner = Callable[[list, str, float], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: list, stdin: str, timeout: float) -> "subprocess.CompletedProcess[str]":
    # Spawned the way the core's tunnel and token mint spawn ssh, so a gateway started
    # with a short PATH still finds the ProxyCommand's tools.
    from kiro_crew.instances.token_mint import ssh_spawn_argv_env

    argv, env = ssh_spawn_argv_env(argv)
    # Bytes: a text-mode stdin on Windows would turn every "\n" into "\r\n".
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
    if not sandbox_id_from_host(host):
        raise ValueError(f"not a managed-environment ssh alias: {host!r}")
    return [
        "ssh", "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=30",
        "-o", "ServerAliveInterval=15",
        host,
        "sh -s",
    ]  # fmt: skip


def _lone_255(res: RemoteResult) -> bool:
    return res.returncode == SSH_FAILED_EXIT and not res.timed_out and _MINT_LINE not in res.stderr


def run_script(
    host: str,
    script: str,
    *,
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[SshRunner] = None,
    retry: bool = False,
) -> RemoteResult:
    """Run *script* on *host*; a transport failure is a 255 result, never a raise."""
    lines = [
        f'export PATH="{CREW_BIN}:$HOME/.local/bin:$PATH"',
        f"export XDG_DATA_HOME={XDG_DATA_HOME}",
    ]
    for key, value in (env or {}).items():
        if key in ("PATH", "XDG_DATA_HOME") or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError(f"bad environment variable name: {key!r}")
        lines.append(f"export {key}={shlex.quote(value)}")
    body = "\n".join(lines) + "\n" + script
    argv = ssh_argv(host)
    res = _run_once(runner or _default_runner, argv, body, host, timeout)
    if retry and _lone_255(res):
        res = _run_once(runner or _default_runner, argv, body, host, timeout)
    return res


def _run_once(runner: SshRunner, argv: list, body: str, host: str, timeout: float) -> RemoteResult:
    try:
        res = runner(argv, body, timeout)
    except FileNotFoundError:
        return RemoteResult(SSH_FAILED_EXIT, "", "ssh was not found on this machine")
    except subprocess.TimeoutExpired:
        return RemoteResult(
            SSH_FAILED_EXIT, "", f"ssh to {host} timed out after {int(timeout)}s", timed_out=True
        )
    except OSError as exc:
        return RemoteResult(SSH_FAILED_EXIT, "", f"ssh to {host} failed: {type(exc).__name__}")
    err = "\n".join(ln for ln in (res.stderr or "").splitlines() if not ln.startswith(_SSH_NOISE))
    return RemoteResult(res.returncode, res.stdout or "", err)


#: ``GET /api/health`` on the environment's own loopback with the shell alone.
HEALTH_SCRIPT = f"""
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
) -> bool:
    """Ask the gateway for ``/api/health`` every few seconds, for up to two minutes."""
    deadline = clock() + PROBE_UP_TO_SECS
    while True:
        if _health_ok(run(host, f"bash -s <<'EOF'\n{HEALTH_SCRIPT}\nEOF", timeout=15)):
            return True
        if clock() >= deadline:
            return False
        sleep(PROBE_EVERY_SECS)
