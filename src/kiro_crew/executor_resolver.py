"""An aiohttp resolver whose DNS lookups run on a chosen thread pool.

aiohttp's default :class:`~aiohttp.resolver.ThreadedResolver` runs
``getaddrinfo`` on the shared default executor. A request that must not wait
behind that executor gives its connector this resolver instead: the same
lookups and the same results, on its own pool.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from concurrent.futures import Executor
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp.abc import AbstractResolver

if TYPE_CHECKING:  # `ResolveResult` exists only from aiohttp 3.10; setup.cfg allows 3.9.
    from aiohttp.abc import ResolveResult

_IS_WINDOWS = sys.platform == "win32"
_ADDRCONFIG = getattr(socket, "AI_ADDRCONFIG", 0)
_NUMERIC_HOST_FLAGS = socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
_NUMERIC_NAME_FLAGS = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV


def _lookup(host: str, port: int, family: int) -> list[tuple[Any, ...]]:
    try:
        return socket.getaddrinfo(host, port, family, socket.SOCK_STREAM, 0, _ADDRCONFIG)
    except socket.gaierror:
        # aiohttp's own retry: Windows can refuse "localhost" under AI_ADDRCONFIG.
        if not (_IS_WINDOWS and host.rstrip(".").casefold() == "localhost"):
            raise
        return socket.getaddrinfo(host, port, family, socket.SOCK_STREAM, 0, 0)


def _results(host: str, infos: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """aiohttp's six-key answer, read the way :class:`ThreadedResolver` reads it."""
    hosts: list[dict[str, Any]] = []
    for family, _type, proto, _name, address in infos:
        if family == socket.AF_INET6:
            if len(address) < 3:
                continue
            if address[3]:
                resolved, port_text = socket.getnameinfo(address, _NUMERIC_NAME_FLAGS)
                resolved_port = int(port_text)
            else:
                resolved, resolved_port = address[0], address[1]
        elif family == socket.AF_INET:
            resolved, resolved_port = address[0], address[1]
        else:
            continue
        hosts.append(
            {
                "hostname": host,
                "host": resolved,
                "port": resolved_port,
                "family": family,
                "proto": proto,
                "flags": _NUMERIC_HOST_FLAGS,
            }
        )
    return hosts


def _resolve(host: str, port: int, family: int) -> list[dict[str, Any]]:
    return _results(host, _lookup(host, port, family))


class ExecutorResolver(AbstractResolver):
    """Resolves like :class:`~aiohttp.resolver.ThreadedResolver`, on *executor*."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> "list[ResolveResult]":
        loop = asyncio.get_running_loop()
        hosts = await loop.run_in_executor(self._executor, _resolve, host, port, family)
        return hosts  # type: ignore[return-value]

    async def close(self) -> None:
        return None


def executor_connector(executor: Executor) -> aiohttp.TCPConnector:
    """A ``TCPConnector`` that resolves host names on *executor*."""
    return aiohttp.TCPConnector(resolver=ExecutorResolver(executor))
