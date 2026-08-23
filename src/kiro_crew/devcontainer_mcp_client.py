"""Stdio-to-socket pipe used inside a Dev Container.

Copied into a gateway-owned directory and bind-mounted into the container.
kiro-cli then launches this instead of ``kirocrew mcp-core`` (and the other
managed servers). Those servers run on the HOST and talk to the gateway on
loopback; this file only moves bytes.

Two transports, same copy loop. The last argument is a per-runtime secret
the host accept loop checks before spawning the MCP child:

* unix: ``client.py /tmp/kirocrew-mcp-bridge/<token>.<sub>.sock <secret>``
  (native Linux; bind-mounted AF_UNIX shares a kernel with the gateway)
* tcp: ``client.py tcp host.docker.internal <port> <secret>``
  (Docker Desktop; AF_UNIX does not survive the VM file share)

No ``kiro_crew`` imports: the image does not have the package, and a client
that imported it would fail the moment it started.
"""

from __future__ import annotations

import os
import select
import socket
import sys


def _copy(sock: socket.socket) -> int:
    """Bidirectional copy between stdin/stdout and *sock* until either side ends."""
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    sock_fd = sock.fileno()
    stdin_open = True
    try:
        while True:
            readers = [sock_fd]
            if stdin_open:
                readers.append(stdin_fd)
            readable, _, _ = select.select(readers, [], [])
            if stdin_fd in readable:
                chunk = os.read(stdin_fd, 65536)
                if not chunk:
                    stdin_open = False
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    sock.sendall(chunk)
            if sock_fd in readable:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                os.write(stdout_fd, chunk)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return 1
    return 0


def _offer_secret(sock: socket.socket, secret: str) -> None:
    sock.sendall(secret.encode("ascii") + b"\n")


def _connect(args: list[str]) -> socket.socket:
    if len(args) == 2 and args[0] and args[0] != "tcp" and args[1]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(args[0])
        _offer_secret(sock, args[1])
        return sock
    if len(args) == 4 and args[0] == "tcp" and args[1] and args[2].isdigit() and args[3]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((args[1], int(args[2])))
        _offer_secret(sock, args[3])
        return sock
    raise ValueError("usage")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        sock = _connect(args)
    except ValueError:
        sys.stderr.write(
            "usage: client.py <unix-socket-path> <secret> | client.py tcp <host> <port> <secret>\n"
        )
        return 2
    except OSError as exc:
        sys.stderr.write(f"mcp-bridge: {exc}\n")
        return 1
    try:
        return _copy(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
