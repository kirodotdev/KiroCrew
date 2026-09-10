"""``kirocrew acp`` — serve Kiro Crew to an editor over stdio.

An ACP-aware editor (VS Code, Zed) spawns this process and speaks JSON-RPC 2.0
over its stdin/stdout. Turns run through the same machinery a dashboard turn
does — context assembly, the session registry, and the PreToolUse hook gate — so
an editor session gets Kiro Crew's memory, lessons, and skills rather than a bare
kiro-cli session.

**stdout is the protocol.** Nothing may write to it except JSON-RPC frames: a
stray print corrupts the stream and the editor drops the session. Logging is
pinned to stderr, which is where an ACP client surfaces agent logs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from kiro_crew import __version__, platform_compat
from kiro_crew.acp_server.gateway import make_prompt_handler
from kiro_crew.acp_server.http_backend import HttpGatewayBackend, default_base_url
from kiro_crew.acp_server.server import AcpAgentServer
from kiro_crew.acp_server.transport import (
    ACP_FRAME_LIMIT_BYTES,
    AgentTransport,
    FrameWriter,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.hooks import HookManager, HooksConfig
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.session import SessionManager
from kiro_crew.skills import SkillsLoader

logger = logging.getLogger(__name__)


@dataclass
class _Services:
    """Concrete ``GatewayServices``: what the prompt handler needs."""

    sessions: Any
    context_builder: Any


def _configure_logging(verbose: bool) -> None:
    """Send all logging to stderr; stdout belongs to the protocol.

    ``force=True`` tears down handlers an imported module may have installed on
    the root logger — one writing to stdout would corrupt the JSON-RPC stream.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


class _ThreadedPipeWriter:
    """Buffer writes until ``drain`` flushes them off the event-loop thread."""

    def __init__(self, stream: Any) -> None:
        self._fd = stream.fileno()
        self._pending = bytearray()

    def write(self, data: bytes) -> None:
        self._pending.extend(data)

    async def drain(self) -> None:
        data = bytes(self._pending)
        self._pending.clear()
        if data:
            await asyncio.to_thread(self._write_all, data)

    def _write_all(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(self._fd, remaining)
            if written <= 0:
                raise OSError("stdio pipe write made no progress")
            remaining = remaining[written:]


_STDIO_READ_CHUNK_BYTES = 64 * 1024
_STDIO_READER_TASKS: set[asyncio.Task[None]] = set()


class _ThreadedReadTransport(asyncio.ReadTransport):
    """Apply ``StreamReader`` flow control to descriptor reads in worker threads."""

    def __init__(self) -> None:
        super().__init__()
        self._readable = asyncio.Event()
        self._readable.set()

    async def wait_until_readable(self) -> None:
        await self._readable.wait()

    def pause_reading(self) -> None:
        self._readable.clear()

    def resume_reading(self) -> None:
        self._readable.set()

    def is_reading(self) -> bool:
        return self._readable.is_set()


async def _pump_threaded_reader(
    source_fd: int,
    reader: asyncio.StreamReader,
    transport: _ThreadedReadTransport,
) -> None:
    try:
        while True:
            await transport.wait_until_readable()
            chunk = await asyncio.to_thread(os.read, source_fd, _STDIO_READ_CHUNK_BYTES)
            if not chunk:
                reader.feed_eof()
                return
            reader.feed_data(chunk)
    except (OSError, ValueError) as exc:
        reader.set_exception(exc)


async def _stdio_streams(
    stdin: Any = None, stdout: Any = None
) -> tuple[asyncio.StreamReader, FrameWriter]:
    """Wrap this process's stdin/stdout as asyncio-compatible streams.

    Windows anonymous pipes are synchronous handles and cannot register with a
    Proactor IOCP, so bounded descriptor reads and complete writes run in worker
    threads there.
    """
    source = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    target = stdout if stdout is not None else getattr(sys.stdout, "buffer", sys.stdout)
    if source is None or target is None:
        raise RuntimeError("standard streams are unavailable")
    reader = asyncio.StreamReader(limit=ACP_FRAME_LIMIT_BYTES)
    if platform_compat.IS_WINDOWS:
        read_transport = _ThreadedReadTransport()
        reader.set_transport(read_transport)
        task = asyncio.create_task(_pump_threaded_reader(source.fileno(), reader, read_transport))
        _STDIO_READER_TASKS.add(task)
        task.add_done_callback(_STDIO_READER_TASKS.discard)
        return reader, _ThreadedPipeWriter(target)

    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), source)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, target)
    return reader, asyncio.StreamWriter(transport, protocol, None, loop)


def _build_services(cfg: KiroCrewConfig) -> _Services:
    """Construct the gateway machinery in-process.

    Mirrors the CLI-side construction in ``cli_server`` rather than the
    dashboard's, since there is no web server here. Memory, lessons, and skills
    read from ``KIROCREW_HOME`` on disk, so an editor session sees the same
    accumulated state as the dashboard and Slack.
    """
    memory = MemoryStore()
    memory.init()
    context_builder = ContextBuilder(
        memory=memory,
        skills=SkillsLoader(),
        hooks=HookManager(HooksConfig.from_dict(cfg.hooks)),
        lessons=LessonStore(),
        bot_name=cfg.agent.bot_name,
    )
    sessions = SessionManager(cfg, provider_factory=cfg.create_provider_factory())
    return _Services(sessions=sessions, context_builder=context_builder)


async def _serve(args: argparse.Namespace) -> None:
    """Proxy ACP to dashboard slots by default; use ``--standalone`` offline."""
    if getattr(args, "standalone", False):
        await _serve_standalone(args)
    else:
        await _serve_gateway(args)


async def _serve_gateway(args: argparse.Namespace) -> None:
    """Back ACP sessions with dashboard chat slots via the gateway HTTP API.

    An editor turn is a first-class dashboard turn — persisted history,
    auto-title, tools, slot events, Slack mirroring — because it runs through the
    dashboard's own ``/api/chat`` path. Tool approvals surface in the editor over
    the duplex ACP pipe.
    """
    cfg = KiroCrewConfig.load()
    agent = getattr(args, "agent", None) or cfg.agent.default_agent or None
    base_url = getattr(args, "gateway_url", None) or default_base_url()
    backend = HttpGatewayBackend(base_url, agent=agent)
    await backend.open()  # fails fast if the gateway is unreachable
    reader, writer = await _stdio_streams()
    transport = AgentTransport(reader, writer)
    server = AcpAgentServer(
        transport,
        backend.prompt_handler(),
        agent_version=__version__,
        session_backend=backend,
    )
    logger.info(
        "kirocrew acp %s serving on stdio via gateway %s (agent=%s)",
        __version__,
        base_url,
        agent or "default",
    )
    try:
        await server.serve()
    finally:
        await backend.close()
        await transport.close()


async def _serve_standalone(args: argparse.Namespace) -> None:
    """Isolated in-process session registry — no gateway, no dashboard sharing.

    Offline fallback / diagnostic. Turns run through this process's own
    SessionManager, so they are NOT visible in the dashboard.
    """
    cfg = KiroCrewConfig.load()
    agent = getattr(args, "agent", None) or cfg.agent.default_agent or None
    services = _build_services(cfg)
    reader, writer = await _stdio_streams()
    transport = AgentTransport(reader, writer)
    server = AcpAgentServer(
        transport,
        make_prompt_handler(services, agent=agent),
        agent_version=__version__,
    )
    logger.info(
        "kirocrew acp %s serving on stdio, standalone (agent=%s)",
        __version__,
        agent or "default",
    )
    try:
        await server.serve()
    finally:
        # The editor has closed the pipe; reap kiro-cli children so they do not
        # outlive us as orphans.
        try:
            await asyncio.wait_for(services.sessions.close_all(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):
            logger.warning("session shutdown did not complete cleanly", exc_info=True)
        await transport.close()


def run_acp(args: argparse.Namespace) -> None:
    """Entry point for ``kirocrew acp``."""
    _configure_logging(bool(getattr(args, "verbose", False)))
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass
