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
import sys
from dataclasses import dataclass
from typing import Any

from kiro_crew import __version__
from kiro_crew.acp_server.gateway import make_prompt_handler
from kiro_crew.acp_server.http_backend import HttpGatewayBackend, default_base_url
from kiro_crew.acp_server.server import AcpAgentServer
from kiro_crew.acp_server.transport import ACP_FRAME_LIMIT_BYTES, AgentTransport
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

    ``force=True`` tears down handlers a previously-imported module may have
    installed on the root logger — one of those writing to stdout would corrupt
    the JSON-RPC stream.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


async def _stdio_streams(
    stdin: Any = None, stdout: Any = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap this process's stdin/stdout as asyncio streams.

    The pipes are parameters rather than hard references to ``sys.stdin`` /
    ``sys.stdout`` so a test can pass real ``os.pipe()`` ends without globally
    reassigning the process's streams (which would also fight pytest's capture).
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=ACP_FRAME_LIMIT_BYTES)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), stdin or sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, stdout or sys.stdout
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return reader, writer


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
