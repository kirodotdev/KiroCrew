"""Trusted stdio<->Unix-socket MCP relay (the provider-spawned half of the bridge).

Editor-supplied stdio MCP commands are untrusted. Passing them directly to
kiro-cli would run them outside Kiro Crew's credential-file masking and process
resource controls.

The bridge instead has :class:`~kiro_crew.acp_server.mcp_supervisor.SessionMcpSupervisor`
spawn and OWN the real, long-lived child under Kiro Crew's sandbox, and expose it
through a per-session Unix-domain socket. kiro-cli is handed only THIS proxy as
the ``command`` — a trusted Kiro Crew binary carrying nothing but a socket path
(argv) and a one-time token (read from a 0600 file named in the environment).
The untrusted original command/env never reach kiro-cli.

**This file is intentionally spawned by absolute path** (``python <thisfile>``),
NOT ``python -m kiro_crew.acp_server.mcp_proxy``, so running it does not import
the ``kiro_crew`` package (which would pull in aiohttp and the whole server
stack). It is therefore restricted to the Python standard library.

Wire framing on the socket:
  1. The proxy sends exactly one line ``<token>\n`` to authenticate.
  2. Everything after that first line is raw, bidirectional JSON-RPC bytes,
     forwarded verbatim between this process's stdio and the socket. The MCP
     ``initialize`` handshake therefore happens end-to-end between kiro-cli and
     the real child — the supervisor never consumes it, so the child is
     initialized exactly once and init errors surface to the provider.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_SOCKET_ENV = "KIROCREW_MCP_PROXY_SOCKET"
_TOKEN_FILE_ENV = "KIROCREW_MCP_PROXY_TOKEN_FILE"
_CHUNK = 65536


async def _pump(read, write_fn, drain_fn, close_fn) -> None:
    """Copy chunks from *read* to the writer until EOF, then close the writer."""
    try:
        while True:
            data = await read(_CHUNK)
            if not data:
                break
            write_fn(data)
            await drain_fn()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            close_fn()
        except OSError:
            pass


async def _run(socket_path: str, token: str) -> int:
    try:
        sock_reader, sock_writer = await asyncio.open_unix_connection(socket_path)
    except (OSError, ConnectionError) as exc:
        sys.stderr.write(f"mcp_proxy: cannot connect to supervisor socket: {exc}\n")
        return 1

    # Authenticate: one token line, then raw relay.
    sock_writer.write((token + "\n").encode("utf-8"))
    try:
        await sock_writer.drain()
    except (OSError, ConnectionError) as exc:
        sys.stderr.write(f"mcp_proxy: auth write failed: {exc}\n")
        return 1

    loop = asyncio.get_running_loop()
    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin_reader), sys.stdin)
    stdout_transport, stdout_proto = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    stdout_writer = asyncio.StreamWriter(stdout_transport, stdout_proto, None, loop)

    # stdin (from kiro-cli) -> socket (to the sandboxed child)
    to_child = asyncio.ensure_future(
        _pump(stdin_reader.read, sock_writer.write, sock_writer.drain, sock_writer.close)
    )
    # socket (from the child) -> stdout (to kiro-cli)
    to_editor = asyncio.ensure_future(
        _pump(sock_reader.read, stdout_writer.write, stdout_writer.drain, stdout_writer.close)
    )
    _done, pending = await asyncio.wait({to_child, to_editor}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return 0


def _read_token() -> str:
    token_file = os.environ.get(_TOKEN_FILE_ENV, "")
    if not token_file:
        return ""
    try:
        with open(token_file, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kiro Crew ACP stdio MCP proxy")
    parser.add_argument("--socket", default=os.environ.get(_SOCKET_ENV, ""))
    args = parser.parse_args(argv)
    if not args.socket:
        sys.stderr.write("mcp_proxy: no --socket / KIROCREW_MCP_PROXY_SOCKET given\n")
        return 2
    token = _read_token()
    try:
        return asyncio.run(_run(args.socket, token))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
