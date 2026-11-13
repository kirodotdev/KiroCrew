"""Loopback HTTP servers for tests that skip the host-name lookup on bind.

``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` between
``bind()`` and ``listen()``. On a macOS runner that reverse lookup of
``127.0.0.1`` can take about thirty seconds, so every test that starts a stock
server pays it in full. A test server is reached by address, never by name, so
these classes bind through ``socketserver.TCPServer`` and record the address
as the server name instead (the same override ``plumb_cpu.LoopbackServer``
uses).
"""

from __future__ import annotations

import http.server
import socketserver


class LoopbackHTTPServer(http.server.HTTPServer):
    """``HTTPServer`` whose bind resolves no host name."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class LoopbackThreadingHTTPServer(socketserver.ThreadingMixIn, LoopbackHTTPServer):
    """``ThreadingHTTPServer`` whose bind resolves no host name."""

    daemon_threads = True
