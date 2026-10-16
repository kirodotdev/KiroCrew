"""Run a remote session's engine in a Coder workspace through the guest worker.

:class:`RemoteWorkerLaunch` is what ``AcpRuntime.set_remote_launch`` takes. On
``local_argv`` it installs ``wire.py`` + ``worker.py`` in the workspace,
delivers a fresh per-session secret, starts the worker in the background, and
returns the local ``kiro_crew.remote_session.client`` argv whose dial command
reaches that worker through ``coder ssh``.

Security properties:

* File contents and the secret travel on the remote command's STDIN, never in
  argv, so they are not visible in the guest's or this machine's process list.
* Guest files are created under ``umask 077`` in ``~/.kirocrew-worker``; the
  worker reads its secret file and unlinks it.
* The gateway's copy of the secret is a 0600 file in a 0700 directory, created
  with ``O_EXCL`` and unlinked by the bridge when it starts.
* The engine runs as ``env -u KIRO_API_KEY kiro-cli acp ...`` without
  ``--auth-method cli``, and the worker also withholds credential variables, so
  the engine draws its token from the gateway over the session connection.
* Every interpolated value is validated (session id) or ``shlex``-quoted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from kiro_crew import platform_compat
from kiro_crew.remote_session import wire

logger = logging.getLogger(__name__)

WORKER_DIR = "$HOME/.kirocrew-worker"
_WORKER_FILES = ("wire.py", "worker.py", "workspace_tools.py")
#: The ``mcpServers`` name the local engine sees for the remote workspace.
WORKSPACE_SERVER_NAME = "workspace"
_COMMAND_TIMEOUT = 120.0
_OUTPUT_MAX = 64 * 1024
#: Variables never handed to the dial/install commands' environment.
_WITHHELD_ENV = ("KIRO_API_KEY", "KIRO_IDENTITY", "CREW_SESSION_SECRET")


class RemoteWorkerError(RuntimeError):
    """Preparing or reaching the guest worker failed."""


class RemoteShell(Protocol):
    """How commands reach the guest: a run-and-collect call, and a dial argv."""

    async def run(self, command: str, stdin: bytes = b"") -> bytes: ...

    def argv(self, command: str) -> list[str]: ...


def _scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _WITHHELD_ENV:
        env.pop(name, None)
    return env


async def run_collect(
    argv: Sequence[str],
    stdin: bytes,
    env: dict[str, str] | None,
    *,
    max_output: int = _OUTPUT_MAX,
    timeout: float = _COMMAND_TIMEOUT,
) -> bytes:
    """Run ``argv`` with ``stdin``; return bounded stdout or raise with a short reason.

    The defaults bound guest commands. Coder lifecycle calls (``coder list``,
    ``create``) pass the Coder client's own, larger limits: their JSON grows with
    the deployment's workspace count, and a create waits for a full build.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        # Only the last line of stderr, bounded: guest logs can be long, and the
        # secret never reaches stderr because it only travels on stdin.
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise RemoteWorkerError(
            f"guest command failed (exit {process.returncode}): {tail[0][:200]}"
        )
    if len(out) > max_output:
        raise RemoteWorkerError("guest command returned too much data")
    return out


class CoderShell:
    """Commands over ``coder ssh``, using the gateway machine's ``coder login``."""

    def __init__(self, coder_bin: str, workspace: str, deployment_url: str) -> None:
        from kiro_crew.coder.client import CoderClient

        self.coder_bin = coder_bin
        self.workspace = CoderClient._validated_workspace_name(workspace)
        self.deployment_url = deployment_url

    def argv(self, command: str) -> list[str]:
        # --disable-autostart: dialling must never start (and bill) a stopped
        # workspace behind the operator's back; starting is an explicit action.
        return [*self.interactive_argv(), "--", command]

    def interactive_argv(self) -> list[str]:
        """An interactive shell (the dashboard terminal); never autostarts either."""
        # The deployment is the one whose workspace identity was verified, not
        # the CLI's current default or an inherited CODER_URL.
        return [
            self.coder_bin,
            "--url",
            self.deployment_url,
            "ssh",
            "--disable-autostart",
            self.workspace,
        ]

    async def run(self, command: str, stdin: bytes = b"") -> bytes:
        return await run_collect(self.argv(command), stdin, _scrubbed_env())


# Writes every worker file from ONE stdin stream: a JSON header line of
# [name, length] pairs, then the bytes back to back. One ``coder ssh`` instead of
# one per file, because each round trip costs seconds on the session-start path.
# Python reads exact lengths from a pipe, which ``head -c`` does not promise. The
# guest needs python3 for the worker anyway. Kept 3.9-compatible (guest images).
_INSTALL_PROGRAM = (
    "import json,os,sys\n"
    "d=os.path.join(os.environ['HOME'],'.kirocrew-worker')\n"
    "os.makedirs(os.path.join(d,'sessions'),exist_ok=True)\n"
    "s=sys.stdin.buffer\n"
    "for n,k in json.loads(s.readline()):\n"
    f"  if n not in {list(_WORKER_FILES)!r}: sys.exit('unknown worker file')\n"
    "  b=s.read(k)\n"
    "  if len(b)!=k: sys.exit('truncated worker file')\n"
    # Per-process temp name: two starts may install into one workspace at
    # once, and a shared temp file would interleave.
    "  t=os.path.join(d,'%s.%d.tmp'%(n,os.getpid()))\n"
    "  with open(t,'wb') as f: f.write(b)\n"
    "  os.replace(t,os.path.join(d,n))\n"
)


def _install_command() -> str:
    return (
        "set -eu; umask 077; "
        'command -v python3 >/dev/null || { echo "python3 is not installed" >&2; exit 4; }; '
        f"exec python3 -c {shlex.quote(_INSTALL_PROGRAM)}"
    )


def _install_payload(files: dict[str, bytes]) -> bytes:
    for name in files:
        if name not in _WORKER_FILES:
            raise ValueError("unknown worker file")
    header = json.dumps([[name, len(data)] for name, data in files.items()])
    return header.encode("ascii") + b"\n" + b"".join(files.values())


def worker_payload() -> bytes:
    """The install stream for this build's worker files."""
    here = Path(wire.__file__).resolve().parent
    return _install_payload({name: (here / name).read_bytes() for name in _WORKER_FILES})


async def install_worker(shell: RemoteShell) -> str:
    """Install this build's worker files over ``shell``; return their digest."""
    payload = worker_payload()
    await shell.run(_install_command(), payload)
    return hashlib.sha256(payload).hexdigest()


def _start_command(session_id: str, remote_argv: Sequence[str], orphan_timeout: int) -> str:
    sid = wire.validate_session_id(session_id).decode("ascii")
    engine = shlex.join(["env", "-u", "KIRO_API_KEY", *remote_argv])
    # sid is validated to [A-Za-z0-9_-], so it needs no quoting; $HOME expands
    # when the launcher runs.
    launcher = (
        # cd first, so a child argv may name the worker's sibling files relatively.
        f'cd "$HOME/.kirocrew-worker" && S="$HOME/.kirocrew-worker/sessions"; '
        f'exec python3 "$HOME/.kirocrew-worker/worker.py" '
        f'--session-id {sid} --secret-file "$S/{sid}.key" --listen 127.0.0.1:0 '
        f'--port-file "$S/{sid}.port" --orphan-timeout {int(orphan_timeout)} -- {engine}'
    )
    return (
        f'set -eu; umask 077; D="{WORKER_DIR}"; S="$D/sessions"; mkdir -p "$S"; '
        f'rm -f "$S/{sid}.port"; cat > "$S/{sid}.key"; '
        'command -v python3 >/dev/null || { echo "python3 is not installed" >&2; exit 4; }; '
        # The worker command lives in a private launcher script, written from a
        # single-quoted literal, so the engine's shell-quoted argv survives
        # intact however the launcher is started below.
        f'R="$S/{sid}.run"; printf "%s\\n" {shlex.quote(launcher)} > "$R"; '
        f'W="/bin/sh $R"; : >"$S/{sid}.log"; '
        # Coder's agent reaps every process an SSH session started when that
        # session closes, and setsid does not escape it. A transient systemd
        # user service does, so prefer it; fall back to setsid+nohup on hosts
        # with no user manager. The unit inherits no environment except PATH.
        'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; '
        'export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"; '
        "if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then "
        f'echo "launch: systemd-run" >>"$S/{sid}.log"; '
        f'systemd-run --user --collect --quiet --unit="kirocrew-worker-{sid}" -E PATH="$PATH" '
        f'-p StandardOutput="append:$S/{sid}.log" -p StandardError="append:$S/{sid}.log" $W; '
        "else if command -v setsid >/dev/null 2>&1; then L=setsid; else L=; fi; "
        f'echo "launch: nohup ${{L:-plain}}" >>"$S/{sid}.log"; '
        f'nohup $L $W >>"$S/{sid}.log" 2>&1 </dev/null & fi; '
        f'i=0; while [ ! -s "$S/{sid}.port" ]; do i=$((i+1)); '
        f'if [ $i -gt 150 ]; then echo "worker did not start" >&2; '
        f'tail -n 3 "$S/{sid}.log" >&2 || true; exit 3; fi; sleep 0.1; done; '
        f'cat "$S/{sid}.port"'
    )


#: This gateway process's tag, carried in every worker id it starts. A worker
#: whose id lacks it was started by an earlier process -- one that crashed or was
#: killed without stopping its workers -- and is reaped on this process's first
#: workspace start for that binding (``_reap_stale_command``). Hex, so it is safe
#: inside a shell glob.
_GATEWAY_EPOCH = os.urandom(3).hex()
#: Bindings this process already reaped: once per binding per process, because a
#: later start in the same process must never stop its own live workers.
_REAPED: set[str] = set()
_WORKER_ID_PREFIX_CHARS = 40
_RUNTIME_END_POLL_SECONDS = 2.0
#: A relay close still running after this goes on in the background.
_RELAY_CLOSE_WAIT_SECONDS = 30.0
#: Strong references: asyncio keeps only weak ones to tasks.
_RELAY_TASKS: set[asyncio.Future[Any]] = set()


def _keep(task: asyncio.Future[Any]) -> asyncio.Future[Any]:
    _RELAY_TASKS.add(task)
    task.add_done_callback(_RELAY_TASKS.discard)
    return task


def _reap_stale_command(binding_prefix: str, epoch: str) -> str:
    """Stop every worker of this binding that another gateway process started."""
    prefix = wire.validate_session_id(binding_prefix).decode("ascii")
    if not re.fullmatch(r"[0-9a-f]{1,16}", epoch):
        raise RemoteWorkerError("invalid gateway epoch")
    return (
        f'S="{WORKER_DIR}/sessions"; for p in "$S"/{prefix}-*.port; do '
        '[ -e "$p" ] || continue; '
        f'case "$p" in *-g{epoch}-*) continue;; esac; '
        'kill -TERM "$(cut -d" " -f2 "$p")" 2>/dev/null || true; '
        'n="${p%.port}"; rm -f "$p" "$n.key" "$n.run"; done; true'
    )


def _stop_command(session_id: str) -> str:
    sid = wire.validate_session_id(session_id).decode("ascii")
    return (
        f'S="{WORKER_DIR}/sessions"; '
        f'if [ -s "$S/{sid}.port" ]; then kill -TERM "$(cut -d" " -f2 "$S/{sid}.port")" '
        f'2>/dev/null || true; fi; rm -f "$S/{sid}.port" "$S/{sid}.key" "$S/{sid}.run"'
    )


def _diagnose_command(session_id: str) -> str:
    """Read-only: is the worker alive, how it was launched, and its log tail."""
    sid = wire.validate_session_id(session_id).decode("ascii")
    return (
        f'S="{WORKER_DIR}/sessions"; '
        f'if [ -s "$S/{sid}.port" ] && kill -0 "$(cut -d" " -f2 "$S/{sid}.port")" 2>/dev/null; '
        'then echo "worker: alive"; else echo "worker: not running"; fi; '
        'echo "setsid: $(command -v setsid || echo none)"; '
        'echo "systemd-run: $(command -v systemd-run || echo none)"; '
        'if systemctl --user show-environment >/dev/null 2>&1; then echo "user manager: yes"; '
        'else echo "user manager: no"; fi; '
        'echo "pid1: $(cat /proc/1/comm 2>/dev/null)"; '
        'echo "ssh env: $(env | grep -c ^CODER_ 2>/dev/null) CODER_ vars"; '
        f'tail -n 15 "$S/{sid}.log" 2>/dev/null || echo "no log"'
    )


def _parse_port(output: bytes) -> tuple[int, int]:
    fields = output.decode("ascii", "replace").split()
    if len(fields) != 2 or not all(f.isdigit() for f in fields):
        raise RemoteWorkerError("guest worker did not report its port")
    port, pid = int(fields[0]), int(fields[1])
    if not 0 < port < 65536 or pid <= 1:
        raise RemoteWorkerError("guest worker reported an invalid port")
    return port, pid


def _write_private(directory: Path, name: str, data: bytes) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    uid = platform_compat.effective_uid()
    if uid is not None and (info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077):
        raise RemoteWorkerError("remote session secret directory is not private")
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


class RemoteWorkerLaunch:
    """One session's remote engine, prepared on demand by ``AcpRuntime``."""

    def __init__(
        self,
        session_id: str,
        shell: RemoteShell,
        secrets_dir: Path,
        *,
        orphan_timeout: int = wire.ORPHAN_TIMEOUT_SECONDS,
        python: str = sys.executable,
        link_options: dict[str, float] | None = None,
    ) -> None:
        wire.validate_session_id(session_id)
        #: The binding this launch serves; each runtime gets its own worker id
        #: derived from it (see ``workspace_server``).
        self.binding_id = session_id
        self.session_id = session_id
        self.shell = shell
        self.secrets_dir = Path(secrets_dir)
        self.orphan_timeout = orphan_timeout
        self.python = python
        self.link_options = dict(link_options or {})
        self.port = 0
        self.relay: Any = None

    async def install(self) -> None:
        await install_worker(self.shell)

    async def start(self, remote_argv: Sequence[str], secret: bytes) -> int:
        output = await self.shell.run(
            _start_command(self.session_id, remote_argv, self.orphan_timeout),
            secret.hex().encode("ascii") + b"\n",
        )
        self.port, _pid = _parse_port(output)
        return self.port

    async def stop(self) -> None:
        await self.shell.run(_stop_command(self.session_id))

    async def diagnose(self) -> str:
        """Bounded, read-only worker status and log tail, for failure reports."""
        out = await self.shell.run(_diagnose_command(self.session_id))
        return out.decode("utf-8", "replace")[-2000:]

    def dial_argv(self, port: int) -> list[str]:
        return self.shell.argv(f"python3 {WORKER_DIR}/worker.py attach --port {int(port)}")

    async def workspace_server(self, workdir: str) -> dict[str, object]:
        """Start the guest tool server and return its local ``mcpServers`` element.

        The engine stays on the gateway and launches the returned command as an
        ordinary stdio MCP server; that command is the bridge, which carries the
        MCP stream to ``workspace_tools.py`` in the guest. No credential is placed
        in the guest: the tool server needs none.
        """
        if not isinstance(workdir, str) or not workdir.startswith("/"):
            raise RemoteWorkerError("the profile's working directory must be an absolute path")
        from kiro_crew.remote_session.relay import WorkspaceRelay, write_token_file

        # The engine may launch this server more than once per runtime (a tools
        # probe, a reload), and each launch needs a working stream of its own.
        # So nothing remote is started here beyond installing the guest files:
        # the relay starts a fresh guest worker, with a fresh one-use secret, for
        # every authenticated connection, and runs the link in this process.
        await self.install()
        await self.reap_stale_workers()
        # A restarted runtime's relay replaces the previous runtime's: close that.
        await self.close_relay()
        self.session_id = self.new_worker_id()
        token_path, token = await asyncio.to_thread(
            write_token_file, self.secrets_dir, f"{self.session_id}.relay"
        )
        relay = WorkspaceRelay(
            self,
            ["python3", "-u", "workspace_tools.py", "--root", workdir],
            token_path,
            token,
            name=self.session_id,
        )
        port = await relay.start()
        self.relay = relay
        argv = platform_compat.isolated_python_argv(
            "-P",
            "-m",
            "kiro_crew.remote_session.client",
            "--pipe",
            f"127.0.0.1:{port}",
            "--token-file",
            str(token_path),
            executable=self.python,
        )
        return {"name": WORKSPACE_SERVER_NAME, "command": argv[0], "args": argv[1:], "env": []}

    async def close_relay(self, relay: Any = None) -> None:
        """Close ``relay`` (default: the current one): listener, spare guest
        worker and token file. Idempotent; waits at most _RELAY_CLOSE_WAIT_SECONDS."""
        relay = self.relay if relay is None else relay
        if relay is None:
            return
        if relay is self.relay:
            self.relay = None
        closing = _keep(asyncio.ensure_future(relay.close()))
        try:
            await asyncio.wait_for(asyncio.shield(closing), _RELAY_CLOSE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("remote workspace relay %s is still closing", relay.name)
        except Exception:  # noqa: BLE001 -- a failed close only leaves the idle reaper
            logger.info("Could not close remote workspace relay %s", relay.name, exc_info=True)

    def close_relay_when_ended(self, is_alive: Callable[[], bool]) -> None:
        """Close the current relay once ``is_alive()`` is False for its runtime.

        Left open, each ended runtime's relay keeps a spare guest worker and a
        token file until its idle timeout, and enough fill the live-relay cap.
        """
        relay = self.relay
        if relay is None:
            return

        async def watch() -> None:
            while is_alive():
                await asyncio.sleep(_RUNTIME_END_POLL_SECONDS)
            await self.close_relay(relay)

        _keep(asyncio.ensure_future(watch()))

    # -- per-connection workers (the relay's side) -------------------------------------
    def new_worker_id(self) -> str:
        """A worker id of its own: workers sharing one id would overwrite each
        other's port and key files, and one's stop would kill the other."""
        prefix = self.binding_id[:_WORKER_ID_PREFIX_CHARS]
        return f"{prefix}-g{_GATEWAY_EPOCH}-{os.urandom(4).hex()}"

    async def reap_stale_workers(self) -> None:
        """Stop workers an earlier gateway process left running on this binding.

        After a crash the old process's worker keeps running whatever command it
        was in (a long build writing files) until its orphan timeout, beside the
        resumed session's new one. Once per binding per process; best effort, a
        failure only leaves the orphan timeout in charge.
        """
        if self.binding_id in _REAPED:
            return
        _REAPED.add(self.binding_id)
        try:
            await self.shell.run(
                _reap_stale_command(self.binding_id[:_WORKER_ID_PREFIX_CHARS], _GATEWAY_EPOCH)
            )
        except Exception:  # noqa: BLE001 -- the orphan timeout still stops them
            logger.info("Could not reap stale workers on %s", self.binding_id, exc_info=True)

    async def start_worker(self, worker_id: str, remote_argv: Sequence[str], secret: bytes) -> int:
        command = _start_command(worker_id, remote_argv, self.orphan_timeout)
        stdin = secret.hex().encode("ascii") + b"\n"
        output = await self.shell.run(command, stdin)
        port, _pid = _parse_port(output)
        return port

    async def stop_worker(self, worker_id: str) -> None:
        await self.shell.run(_stop_command(worker_id))

    def dial_env(self) -> dict[str, str]:
        return _scrubbed_env()

    def dial(self, port: int) -> Any:
        """The link's dial to the guest worker on ``port``: a ``coder ssh`` child here."""
        from kiro_crew.remote_session.client import command_dial

        return command_dial(self.dial_argv(port), self.dial_env())

    def link_kwargs(self) -> dict[str, float]:
        mapping = {
            "keepalive": "keepalive_seconds",
            "stall": "stall_seconds",
            "lease": "lease_seconds",
        }
        return {mapping[k]: float(v) for k, v in self.link_options.items() if k in mapping}

    async def local_argv(self, remote_argv: Sequence[str], *, relay: bool = False) -> list[str]:
        """Prepare the guest and return the bridge argv for ``AcpRuntime`` to spawn.

        ``relay=True`` runs the dial in THIS (gateway) process behind a loopback
        relay and points the bridge at it. Required when the bridge is an MCP child
        of a sandboxed engine, which cannot use the gateway's ``coder login``.
        """
        secret = wire.new_secret()
        await self.install()
        port = await self.start(remote_argv, secret)
        key = _write_private(
            self.secrets_dir, f"{self.session_id}-{os.urandom(4).hex()}.key", secret.hex().encode()
        )
        options: list[str] = []
        for flag in ("keepalive", "stall", "lease"):
            if flag in self.link_options:
                options += [f"--{flag}", str(float(self.link_options[flag]))]
        return platform_compat.isolated_python_argv(
            "-P",
            "-m",
            "kiro_crew.remote_session.client",
            "--session-id",
            self.session_id,
            "--secret-file",
            str(key),
            *options,
            *(await self._dial_args(port, relay)),
            executable=self.python,
        )

    async def _dial_args(self, port: int, relay: bool) -> list[str]:
        if not relay:
            return ["--", *self.dial_argv(port)]
        from kiro_crew.remote_session.relay import DialRelay

        dial = DialRelay(self.dial_argv(port), _scrubbed_env(), name=self.session_id)
        relay_port = await dial.start()
        self.relay = dial
        return ["--connect", f"127.0.0.1:{relay_port}"]
