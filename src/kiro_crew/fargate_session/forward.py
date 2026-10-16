"""The gateway's SSM port forward to one session task's bootstrap.

Enrollment attests the connected TCP peer against the pinned AWS child and its
process descendants. A listening port alone never authorizes sending a key.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
from typing import Any, Callable, Optional

from kiro_crew import platform_compat
from kiro_crew.session_environment import SessionEnvironmentUnavailable

_READY_SECS = 45.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PortForward:
    """Keep a local port forwarded to ``remote_port`` in an SSM target."""

    def __init__(
        self,
        target: str,
        remote_port: int,
        *,
        profile: str = "",
        region: str = "",
        spawn: Optional[Callable[[list[str]], Any]] = None,
    ) -> None:
        self.target = target
        self.remote_port = remote_port
        self.profile = profile
        self.region = region
        self.local_port = 0
        self._spawn = spawn or self._spawn_ssm
        self._proc: Any = None
        self._start: Optional[str] = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _spawn_ssm(argv: list[str]) -> subprocess.Popen:
        from kiro_crew.deploy.engine import aws_spawn_env

        return subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=aws_spawn_env(argv[0]),
        )

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def ensure(self) -> int:
        """The live local port, starting (or restarting) the forward if needed."""
        async with self._lock:
            if self._alive():
                return self.local_port
            await self._stop_unlocked()
            from kiro_crew.cloud import ssm

            if not await asyncio.to_thread(ssm.session_manager_plugin_installed):
                raise SessionEnvironmentUnavailable(
                    "the AWS Session Manager plugin is not installed on the gateway machine"
                )
            port = _free_port()
            argv = await asyncio.to_thread(
                ssm.build_port_forward_argv,
                self.target,
                self.remote_port,
                port,
                self.profile,
                self.region,
            )
            self._proc = self._spawn(argv)
            self._start = await asyncio.to_thread(
                platform_compat.process_start_time, self._proc.pid
            )
            self.local_port = port
            proc = self._proc
            ready = await asyncio.to_thread(
                lambda: ssm.wait_for_local_port(port, timeout=_READY_SECS, proc=proc)
            )
            if not ready or not self._alive():
                await self._stop_unlocked()
                raise SessionEnvironmentUnavailable(
                    "could not open the SSM port forward to the session task"
                )
            return port

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        port = await self.ensure()
        return await asyncio.open_connection("127.0.0.1", port, limit=64 * 1024 * 1024)

    async def verify_peer(self, writer: asyncio.StreamWriter) -> None:
        """Prove the connected endpoint belongs to this target's pinned AWS child.

        The kernel's reverse four-tuple identifies the accepted connection, not
        a listener that could have been rebound. Unknown ownership or ancestry
        fails closed. Called after reading the greeting so the peer has accepted.
        """
        proc, start = self._proc, self._start
        local, peer = writer.get_extra_info("sockname"), writer.get_extra_info("peername")

        def owned() -> bool:
            if proc is None or start is None or proc.poll() is not None:
                return False
            pid = platform_compat.get_tcp_peer_pid(local, peer)
            if pid is None:
                return False
            peer_pid = pid
            peer_start = platform_compat.process_start_time(pid)
            if peer_start is None:
                return False
            seen: set[int] = set()
            while pid > 0 and pid not in seen and len(seen) < 32:
                if pid == proc.pid:
                    return (
                        proc.poll() is None
                        and platform_compat.process_start_time(proc.pid) == start
                        and platform_compat.process_start_time(peer_pid) == peer_start
                    )
                seen.add(pid)
                pid = platform_compat.get_ppid(pid)
            return False

        if not await asyncio.to_thread(owned) or self._proc is not proc or self._start != start:
            raise SessionEnvironmentUnavailable(
                "refusing bootstrap enrollment: the connected peer is not the task's SSM forward"
            )

    async def _stop_unlocked(self) -> None:
        proc, self._proc = self._proc, None
        self._start = None
        if proc is not None:
            from kiro_crew.cloud import ssm

            await asyncio.to_thread(ssm.kill_port_forward, proc)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_unlocked()
