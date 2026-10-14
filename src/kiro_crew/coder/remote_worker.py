"""Install, start and stop the guest worker over ``coder ssh``.

File contents and secrets travel on stdin, never argv; guest files are created
under ``umask 077``; every interpolated value is validated or ``shlex``-quoted.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from kiro_crew import platform_compat
from kiro_crew.remote_session import wire
from kiro_crew.session_environment import WORKSPACE_SERVER_NAME

logger = logging.getLogger(__name__)

WORKER_DIR = "$HOME/.kirocrew-worker"
#: Beside a launch's git-proxy port file: the number of a proxy start that failed.
#: Mirrors ``workspace_tools.GIT_PROXY_FAILED_SUFFIX``.
GIT_PROXY_FAILED_SUFFIX = ".failed"
_WORKER_FILES = ("wire.py", "worker.py", "workspace_tools.py", "git_proxy.py")
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
    """The guest commands' environment: no gateway secret, and no inherited ``CODER_*``
    override (session token, URL, config dir), so ``coder ssh`` authenticates with
    the CLI's stored login exactly as the lifecycle calls do (``_login_runner``)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODER_")}
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
    """Run ``argv`` with ``stdin``; return bounded stdout or raise a short reason.
    Defaults bound guest commands; Coder lifecycle calls pass larger limits."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )

    async def drain(stream: asyncio.StreamReader, *, discard: bool = False) -> bytes:
        kept = bytearray()
        while chunk := await stream.read(min(65536, max_output + 1)):
            if discard:
                continue
            if len(kept) + len(chunk) > max_output:
                raise RemoteWorkerError("guest command returned too much data")
            kept.extend(chunk)
        return bytes(kept)

    async def feed() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(drain(process.stdout))
    stderr_task = asyncio.create_task(drain(process.stderr))
    tasks = (
        stdout_task,
        stderr_task,
        asyncio.create_task(feed()),
        asyncio.create_task(process.wait()),
    )
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout)
        out, err = stdout_task.result(), stderr_task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Reap through bounded readers; never buffer guest output in communicate.
        if process.returncode is None:
            try:
                await platform_compat.kill_process_tree_async(process.pid, platform_compat.SIGKILL)
            except ProcessLookupError:
                # Exited after the drain but not yet reaped (Windows taskkill).
                pass
            finally:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(
                    asyncio.gather(
                        process.wait(),
                        drain(process.stdout, discard=True),
                        drain(process.stderr, discard=True),
                    ),
                    platform_compat.REAP_TIMEOUT_SECS,
                )
    if process.returncode:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise RemoteWorkerError(
            f"guest command failed (exit {process.returncode}): {tail[0][:200]}"
        )
    return out


class CoderShell:
    """Commands over ``coder ssh``, using the gateway machine's ``coder login``."""

    def __init__(self, coder_bin: str, workspace: str, deployment_url: str) -> None:
        from kiro_crew.coder.client import CoderClient

        self.coder_bin = coder_bin
        self.workspace = CoderClient._validated_workspace_name(workspace)
        self.deployment_url = deployment_url

    def argv(self, command: str) -> list[str]:
        return [*self.interactive_argv(), "--", command]

    def interactive_argv(self) -> list[str]:
        """Shell argv on the verified deployment (not the CLI default or an
        inherited CODER_URL); never autostarts (and bills) a stopped workspace."""
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


# All worker files in one ``coder ssh``: a JSON [name, length] header, then bytes.
_INSTALL_PROGRAM = (
    "import json,os,sys\n"
    "d=os.path.join(os.environ['HOME'],'.kirocrew-worker')\n"
    "os.makedirs(os.path.join(d,'sessions'),exist_ok=True)\n"
    "s=sys.stdin.buffer\n"
    "for n,k in json.loads(s.readline()):\n"
    f"  if n not in {list(_WORKER_FILES)!r}: sys.exit('unknown worker file')\n"
    "  b=s.read(k)\n"
    "  if len(b)!=k: sys.exit('truncated worker file')\n"
    # Per-process temp name so concurrent installs cannot interleave.
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
    # sid is validated, so unquoted; cd first so a child argv may name siblings relatively.
    launcher = (
        f'cd "$HOME/.kirocrew-worker" && S="$HOME/.kirocrew-worker/sessions"; '
        f'exec python3 "$HOME/.kirocrew-worker/worker.py" '
        f'--session-id {sid} --secret-file "$S/{sid}.key" --listen 127.0.0.1:0 '
        f'--port-file "$S/{sid}.port" --orphan-timeout {int(orphan_timeout)} -- {engine}'
    )
    return (
        f'set -eu; umask 077; D="{WORKER_DIR}"; S="$D/sessions"; mkdir -p "$S"; '
        f'rm -f "$S/{sid}.port"; cat > "$S/{sid}.key"; '
        'command -v python3 >/dev/null || { echo "python3 is not installed" >&2; exit 4; }; '
        # A private launcher script keeps the quoted argv intact.
        f'R="$S/{sid}.run"; printf "%s\\n" {shlex.quote(launcher)} > "$R"; '
        f'W="/bin/sh $R"; : >"$S/{sid}.log"; '
        # Coder reaps SSH-session processes (setsid included); a systemd user unit escapes.
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


#: Tag in every worker id this process starts; untagged workers are reaped.
_GATEWAY_EPOCH = os.urandom(3).hex()
#: Reaped once per binding per process, so a later start never stops its own.
_REAPED: set[str] = set()
_WORKER_ID_PREFIX_CHARS = 40
_RUNTIME_END_POLL_SECONDS = 2.0
#: A relay close still running after this goes on in the background; it covers
#: a worker stop's own wait (``_STOP_WAIT_SECONDS``) with a margin.
_RELAY_CLOSE_WAIT_SECONDS = 60.0
#: Strong references: asyncio keeps only weak ones to tasks.
_RELAY_TASKS: set[asyncio.Future[Any]] = set()
#: binding -> guest workers whose relay closed or whose stop failed, not yet confirmed
#: stopped. Such a worker may still be running a command, so a replacement runtime
#: refuses the binding until each one's exit is confirmed.
_UNCONFIRMED_STOPS: dict[str, set[str]] = {}
EARLIER_WORKER_RUNNING = (
    "a command from this chat's previous engine may still be running in the remote "
    "workspace and could overwrite new work; try again in a minute"
)


def _keep(task: asyncio.Future[Any]) -> asyncio.Future[Any]:
    _RELAY_TASKS.add(task)
    task.add_done_callback(_RELAY_TASKS.discard)
    return task


#: Shell function: signal the worker a ``.port`` record names only while that pid's
#: argv still carries ``--port-file <record>``; a reused pid is someone else's.
#: SIGUSR1 is the worker's stop-now signal (``remote_session/worker.py:Worker.stop``).
_STOP_IF_OURS = (
    'stop_ours() { q=$(cut -d" " -f2 "$1" 2>/dev/null); [ -n "$q" ] || return 0; '
    'tr "\\000" "\\n" < "/proc/$q/cmdline" 2>/dev/null | grep -qxF -- "$1" '
    '&& kill -USR1 "$q" 2>/dev/null; return 0; }; '
)


def _reap_stale_command(binding_prefix: str, epoch: str) -> str:
    """Stop every worker of this binding that another gateway process started, and
    return only once each has exited; non-zero, keeping its record, if one did not."""
    prefix = wire.validate_session_id(binding_prefix).decode("ascii")
    if not re.fullmatch(r"[0-9a-f]{1,16}", epoch):
        raise RemoteWorkerError("invalid gateway epoch")
    stale = (
        f'for p in "$S"/{prefix}-*.port; do [ -e "$p" ] || continue; '
        f'case "$p" in *-g{epoch}-*) continue;; esac; '
    )
    return (
        _STOP_IF_OURS + _WAIT_GONE + f'S="{WORKER_DIR}/sessions"; r=0; '
        # Signal all first so they wind down together, then wait for each.
        + stale
        + 'stop_ours "$p"; done; '
        + stale
        + 'if wait_gone "$p"; then n="${p%.port}"; rm -f "$p" "$n.key" "$n.run"; '
        'else r=1; fi; done; exit "$r"'
    )


#: How long a stop waits for the worker to exit: its child's stop grace plus a margin.
_STOP_WAIT_SECONDS = 45
# Wait while the record's pid is still that worker (a reaped or reused pid reads as gone).
_WAIT_GONE = (
    'wait_gone() { q=$(cut -d" " -f2 "$1" 2>/dev/null); [ -n "$q" ] || return 0; i=0; '
    'while tr "\\000" "\\n" < "/proc/$q/cmdline" 2>/dev/null | grep -qxF -- "$1"; do '
    f'[ "$i" -ge {_STOP_WAIT_SECONDS} ] && return 1; i=$((i+1)); sleep 1; done; return 0; }}; '
)


def _stop_command(session_id: str) -> str:
    """Stop the worker and return only once it has exited, so a command it was still
    running cannot touch the workspace after its runtime is gone; non-zero if it did
    not exit in time."""
    sid = wire.validate_session_id(session_id).decode("ascii")
    return (
        _STOP_IF_OURS + _WAIT_GONE + f'S="{WORKER_DIR}/sessions"; '
        f'if [ -s "$S/{sid}.port" ]; then stop_ours "$S/{sid}.port"; '
        # A worker that did not exit keeps its record, so a retry still finds it.
        f'wait_gone "$S/{sid}.port" || exit 1; fi; '
        f'rm -f "$S/{sid}.port" "$S/{sid}.key" "$S/{sid}.run"'
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


class RemoteWorkerLaunch:
    """One session's guest workers, prepared on demand by ``AcpRuntime``."""

    def __init__(
        self,
        session_id: str,
        shell: RemoteShell,
        *,
        orphan_timeout: int = wire.ORPHAN_TIMEOUT_SECONDS,
        python: str = sys.executable,
        link_options: dict[str, float] | None = None,
    ) -> None:
        wire.validate_session_id(session_id)
        #: Each runtime derives its own worker id from this binding.
        self.binding_id = session_id
        self.session_id = session_id
        self.shell = shell
        self.orphan_timeout = orphan_timeout
        self.python = python
        self.link_options = dict(link_options or {})
        self.relay: Any = None
        #: (worker id, link, serve task) while this session's git proxy runs.
        self.git_proxy: Any = None
        self._git_proxy_start: "asyncio.Future[None] | None" = None
        #: The newest workspace relay: the one whose close stops the git proxy.
        self._git_proxy_relay: Any = None
        #: Where this launch's guest git proxy reports its port, relative to the
        #: worker directory every guest worker starts in. One per launch: each
        #: runtime (a subagent's included) runs its own proxy under its own
        #: session key. The port alone grants no forwarding authority.
        self.git_proxy_port_file = f"git-proxy-{os.urandom(6).hex()}.port"
        self.git_proxy_secret = os.urandom(32).hex()
        #: Counts begin_git_proxy calls; a failed start marks its own number.
        self._git_proxy_generation = 0
        #: The marker write for a proxy that exited before it spoke (kept so it
        #: is not garbage-collected mid-write).
        self._git_proxy_failure: "asyncio.Future[None] | None" = None

    async def install(self) -> None:
        await install_worker(self.shell)

    async def diagnose(self) -> str:
        """Bounded, read-only worker status and log tail, for failure reports."""
        out = await self.shell.run(_diagnose_command(self.session_id))
        return out.decode("utf-8", "replace")[-2000:]

    def dial_argv(self, port: int) -> list[str]:
        return self.shell.argv(f"python3 {WORKER_DIR}/worker.py attach --port {int(port)}")

    async def workspace_server(self, workdir: str) -> dict[str, object]:
        """Install the guest files and return the bridge's ``mcpServers`` element."""
        if not isinstance(workdir, str) or not workdir.startswith("/"):
            raise RemoteWorkerError("the profile's working directory must be an absolute path")
        from kiro_crew.remote_session.relay import WorkspaceRelay, new_token

        # The relay starts a guest worker per connection; a restart replaces the old relay.
        await self.install()
        await self.reap_stale_workers()
        # A restarted runtime's relay replaces the previous runtime's: close that.
        # Not as the proxy's owner: the restart's own begin_git_proxy replaces it.
        self._git_proxy_relay = None
        await self.close_relay()
        await self.confirm_earlier_workers_stopped()
        self.session_id = self.new_worker_id()
        # The token file is written later, into the runtime's private scratch.
        git_secret = self.git_proxy_secret

        class GitWorkspaceRelay(WorkspaceRelay):
            async def _open_link(self) -> tuple[Any, str]:
                link, worker_id = await super()._open_link()
                try:
                    # The capability, and whether this launch is starting a git
                    # proxy: only then does an early git command wait for it.
                    state = self.launch.git_proxy_state()
                    await link.write(f"{git_secret} {state}\n".encode("ascii"))
                except BaseException:
                    with contextlib.suppress(Exception):
                        await link.aclose()
                    with contextlib.suppress(Exception):
                        await self.launch.stop_worker(worker_id)
                    raise
                return link, worker_id

            async def close(self) -> None:
                await super().close()
                # The git proxy lives as long as this session's CURRENT relay. A
                # start can make more than one (an early tools probe, a respawn),
                # and an older one closing on idle must not take the proxy away
                # from the live one: _relay_closed checks which relay owns it.
                with contextlib.suppress(Exception):
                    await self.launch._relay_closed(self)

        relay = GitWorkspaceRelay(
            self,
            [
                "python3",
                "-u",
                "workspace_tools.py",
                "--root",
                workdir,
                "--git-proxy-port-file",
                self.git_proxy_port_file,
                "--git-proxy-secret-stdin",
            ],
            new_token(),
            name=self.session_id,
        )
        port = await relay.start()
        self.relay = self._git_proxy_relay = relay
        argv = platform_compat.isolated_python_argv(
            "-P",
            "-m",
            "kiro_crew.remote_session.client",
            "--pipe",
            f"127.0.0.1:{port}",
            "--token-file",
            "",
            executable=self.python,
        )
        return {"name": WORKSPACE_SERVER_NAME, "command": argv[0], "args": argv[1:], "env": []}

    async def close_relay(self, relay: Any = None) -> None:
        """Close ``relay`` (default: current); idempotent, bounded wait."""
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

    def close_relay_when_ended(
        self, is_alive: Callable[[], bool], on_end: Optional[Callable[[], None]] = None
    ) -> None:
        """Close the current relay once ``is_alive()`` is False for its runtime;
        ``on_end`` runs first, when the runtime has ended."""
        relay = self.relay
        if relay is None:
            return

        async def watch() -> None:
            while is_alive():
                await asyncio.sleep(_RUNTIME_END_POLL_SECONDS)
            if on_end is not None:
                try:
                    on_end()
                except Exception:  # noqa: BLE001 -- the relay still closes
                    logger.debug("remote workspace end hook failed", exc_info=True)
            await self.close_relay(relay)

        _keep(asyncio.ensure_future(watch()))

    def new_worker_id(self) -> str:
        """A unique id: workers sharing one would clobber each other's files."""
        prefix = self.binding_id[:_WORKER_ID_PREFIX_CHARS]
        return f"{prefix}-g{_GATEWAY_EPOCH}-{os.urandom(4).hex()}"

    async def reap_stale_workers(self) -> None:
        """Stop workers an earlier gateway process left on this binding and wait for
        them to exit; refuse the start while one may still be running a command."""
        if self.binding_id in _REAPED:
            return
        try:
            await self.shell.run(
                _reap_stale_command(self.binding_id[:_WORKER_ID_PREFIX_CHARS], _GATEWAY_EPOCH)
            )
        except Exception as exc:
            # Not marked reaped, so the next start tries again.
            raise RemoteWorkerError(EARLIER_WORKER_RUNNING) from exc
        _REAPED.add(self.binding_id)

    async def start_worker(self, worker_id: str, remote_argv: Sequence[str], secret: bytes) -> int:
        command = _start_command(worker_id, remote_argv, self.orphan_timeout)
        stdin = secret.hex().encode("ascii") + b"\n"
        try:
            output = await self.shell.run(command, stdin)
            port, _pid = _parse_port(output)
        except Exception:
            # A start that fails partway may still have launched the worker; a stop
            # that cannot confirm records it.
            with contextlib.suppress(Exception):
                await self.stop_worker(worker_id)
            raise
        return port

    async def _stop_on_guest(self, worker_id: str) -> None:
        await self.shell.run(_stop_command(worker_id))

    def mark_stopping(self, worker_ids: Sequence[str]) -> None:
        """Record ``worker_ids`` as not yet confirmed stopped: their relay is closing.

        Only a closing relay or a failed stop records a worker. A worker a live relay
        still owns (another runtime on this binding, such as a subagent's) is never
        recorded, so a replacement never stops it.
        """
        if worker_ids:
            _UNCONFIRMED_STOPS.setdefault(self.binding_id, set()).update(worker_ids)

    async def stop_worker(self, worker_id: str) -> None:
        try:
            await self._stop_on_guest(worker_id)
        except BaseException:
            self.mark_stopping([worker_id])
            raise
        pending = _UNCONFIRMED_STOPS.get(self.binding_id)
        if pending is not None:
            pending.discard(worker_id)
            if not pending:
                _UNCONFIRMED_STOPS.pop(self.binding_id, None)

    async def confirm_earlier_workers_stopped(self) -> None:
        """Stop every worker on this binding not yet confirmed stopped, or refuse.

        A failed stop or a relay close that outlived its wait leaves a worker that
        may still run a command; the replacement must not share the workspace with it.
        """
        for worker_id in sorted(_UNCONFIRMED_STOPS.get(self.binding_id, ())):
            try:
                await self.stop_worker(worker_id)
            except Exception as exc:
                raise RemoteWorkerError(EARLIER_WORKER_RUNNING) from exc

    # -- git proxy ------------------------------------------------------------------
    def git_proxy_state(self) -> str:
        """What a new tool server is told about this launch's git proxy.

        ``starting:<n>`` while start ``n`` is in flight or its proxy runs, so an
        early git command waits for the port file (or for start ``n``'s failure
        marker); ``none`` when no start was made or the last one failed, so no
        command waits at all.
        """
        start = self._git_proxy_start
        if start is None or (start.done() and self.git_proxy is None):
            return "none"
        return f"starting:{self._git_proxy_generation}"

    async def _mark_git_proxy_failed(self, generation: int) -> None:
        """Tell the guest's tool servers that start ``generation`` will not come up."""
        marker = shlex.quote(self.git_proxy_port_file + GIT_PROXY_FAILED_SUFFIX)
        with contextlib.suppress(Exception):
            await self.shell.run(
                f'cd "$HOME/.kirocrew-worker" && printf %s {int(generation)} > {marker}'
            )

    async def start_git_proxy(
        self, session_key: str, *, agent: str, **channel_options: Any
    ) -> None:
        """Serve this session's guest git traffic from the gateway, credential here.

        Starts ``git_proxy.py`` in the guest under its own worker and link, and a
        :class:`~kiro_crew.remote_session.git_gateway.GitProxyChannel` that answers
        it. Best effort: a failure leaves git without GitHub access in this
        session, never the session without a workspace. A start that fails marks
        itself failed in the guest, which ends any git command's wait for it.
        Stopped by :meth:`stop_git_proxy`, which the workspace relay calls when it
        closes.
        """
        generation = self._git_proxy_generation
        started = False
        try:
            started = await self._start_git_proxy(session_key, agent=agent, **channel_options)
        finally:
            if not started:
                await asyncio.shield(self._mark_git_proxy_failed(generation))

    async def _start_git_proxy(
        self, session_key: str, *, agent: str, **channel_options: Any
    ) -> bool:
        from kiro_crew.remote_session import git_proxy
        from kiro_crew.remote_session.git_gateway import GitProxyChannel, GitProxyLink

        await self._stop_running_git_proxy()
        if not isinstance(agent, str) or not agent.strip():
            logger.warning("git proxy: calling agent identity is required")
            return False
        worker_id = self.new_worker_id()
        secret = wire.new_secret()
        try:
            port = await self.start_worker(
                worker_id,
                [
                    "python3",
                    "-u",
                    "git_proxy.py",
                    "--port-file",
                    self.git_proxy_port_file,
                    # The file install_worker wrote from this build: a guest of
                    # any other build refuses to start (git_proxy.main).
                    "--build",
                    git_proxy.build_digest(),
                ],
                secret,
            )
        except RemoteWorkerError as exc:
            logger.warning("git proxy: could not start in the workspace: %s", exc)
            return False
        dial = self.dial(port)
        # Same construction as the workspace relay's links (relay._open_link).
        factory: Any = channel_options.pop("link_factory", None) or GitProxyLink
        link = factory(worker_id, secret, dial, **self.link_kwargs())
        link.start()
        try:
            await link.write(self.git_proxy_secret.encode("ascii") + b"\n")
        except BaseException:
            with contextlib.suppress(Exception):
                await link.aclose()
            with contextlib.suppress(Exception):
                await self.stop_worker(worker_id)
            raise
        channel = GitProxyChannel(link, session_key, agent=agent, **channel_options)
        task = asyncio.ensure_future(channel.serve())
        self.git_proxy = (worker_id, link, task)
        generation = self._git_proxy_generation
        task.add_done_callback(functools.partial(self._git_proxy_ended, generation, channel))
        return True

    def _git_proxy_ended(self, generation: int, channel: Any, task: "asyncio.Future[None]") -> None:
        """A guest proxy that ended before its first frame never listened (it
        refused another build's gateway, say): end git's wait for it now."""
        if channel.heard or generation != self._git_proxy_generation:
            return
        logger.warning(
            "git proxy: the workspace's git_proxy.py exited before it started; "
            "its reason is in the worker log"
        )
        self._git_proxy_failure = asyncio.ensure_future(self._mark_git_proxy_failed(generation))

    async def _relay_closed(self, relay: Any) -> None:
        """A workspace relay closed: stop the git proxy only if that relay owns it.

        Compared against the owner, not ``self.relay``: :meth:`close_relay`
        clears ``self.relay`` before the close runs this, on every runtime end.
        """
        if relay is self._git_proxy_relay:
            self._git_proxy_relay = None
            await self.stop_git_proxy()

    def begin_git_proxy(self, session_key: str, *, agent: str, **channel_options: Any) -> None:
        """Start the git proxy in the background: it must not delay the session start."""
        if self._git_proxy_start is not None and not self._git_proxy_start.done():
            return
        self._git_proxy_generation += 1
        self._git_proxy_start = asyncio.ensure_future(
            self.start_git_proxy(session_key, agent=agent, **channel_options)
        )

    async def stop_git_proxy(self) -> None:
        starting, self._git_proxy_start = self._git_proxy_start, None
        if starting is not None and not starting.done():
            # Let a start finish rather than cancel it mid-`coder ssh`: a cancelled
            # start can leave its guest worker running with no id to stop it by.
            await asyncio.gather(starting, return_exceptions=True)
        await self._stop_running_git_proxy()

    async def _stop_running_git_proxy(self) -> None:
        current, self.git_proxy = self.git_proxy, None
        if current is None:
            return
        worker_id, link, task = current
        # End the proxy's input first: it then exits on its own and removes its
        # port file, rather than being killed later with the file left behind.
        with contextlib.suppress(Exception):
            link.close_input()
            await asyncio.wait_for(asyncio.shield(task), 5)
        with contextlib.suppress(Exception):
            await link.aclose()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.stop_worker(worker_id)

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
