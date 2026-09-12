"""Every fixed-port app backend binds address reuse to the platform.

``http.server.HTTPServer`` hardcodes ``allow_reuse_address = 1``. That flag does
not mean the same thing on both families:

* on POSIX it only waives TIME_WAIT, so a restart can rebind a port whose
  previous listener has already closed;
* on Windows ``SO_REUSEADDR`` additionally lets a socket bind an address that
  still has a LIVE listener.

The gateway decides that an app backend hit a port collision by observing that
the spawned child died on its initial bind (``kiro_crew/apps/backend.py`` --
``_survived_initial_bind`` and the EADDRINUSE reasoning around it). That signal
only exists while the bind is allowed to fail, so with the stock flag a second
backend binds the same fixed port on Windows, the gateway reports a healthy
start, and the two processes split incoming requests.

Every backend that binds a FIXED port is pinned here rather than once per app,
because the failure mode is drift: the flag is a stdlib default, so a listener
that does not opt out inherits it silently, and one module's opt-out says
nothing about its siblings or about the scaffold that generates new ones.
"""

from __future__ import annotations

import re
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps import scaffold
from kiro_crew.apps.builtins.design_tweak.backend import server as design_tweak_server
from kiro_crew.apps.builtins.file_explorer import server as file_explorer_server
from kiro_crew.apps.builtins.workflows import server as workflows_server

APPS_ROOT = Path(scaffold.__file__).resolve().parent

#: (label, module, handler symbol as written at the bind site).
FIXED_PORT_BACKENDS = [
    ("workflows", workflows_server, "_Handler"),
    ("design_tweak", design_tweak_server, "Handler"),
    ("file_explorer", file_explorer_server, "FileExplorerHandler"),
]

_IDS = [label for label, _module, _handler in FIXED_PORT_BACKENDS]


def test_the_stdlib_default_is_what_we_are_overriding() -> None:
    """Keeps every assertion below non-tautological.

    ``http.server`` really does turn the flag on for every platform, so a
    per-server subclass is the only way to scope it.
    """
    assert bool(HTTPServer.allow_reuse_address) is True
    assert bool(ThreadingHTTPServer.allow_reuse_address) is True


@pytest.mark.parametrize(("label", "module", "handler"), FIXED_PORT_BACKENDS, ids=_IDS)
def test_listener_binds_address_reuse_to_the_platform(label, module, handler) -> None:
    assert issubclass(module._Server, ThreadingHTTPServer)
    assert module._Server.allow_reuse_address is platform_compat.IS_POSIX
    if platform_compat.IS_WINDOWS:
        assert module._Server.allow_reuse_address is False


@pytest.mark.parametrize(("label", "module", "handler"), FIXED_PORT_BACKENDS, ids=_IDS)
def test_the_entry_point_binds_through_that_subclass(label, module, handler) -> None:
    """A subclass nothing constructs would leave the stock flag on the real socket."""
    src = Path(module.__file__).read_text(encoding="utf-8")
    assert f'_Server(("127.0.0.1", PORT), {handler})' in src
    assert f'ThreadingHTTPServer(("127.0.0.1", PORT), {handler})' not in src
    assert '"0.0.0.0"' not in src


def _bind(server_cls, port: int):
    return server_cls(("127.0.0.1", port), BaseHTTPRequestHandler)


@pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason="POSIX SO_REUSEADDR only waives TIME_WAIT, so a live listener already "
    "refuses a second bind there and the two flag values are indistinguishable",
)
@pytest.mark.parametrize(("label", "module", "handler"), FIXED_PORT_BACKENDS, ids=_IDS)
def test_a_second_windows_backend_cannot_take_the_live_port(label, module, handler) -> None:
    """The consequence, measured: two backends on one port instead of one failure.

    Both listeners are the SAME class in each half, which is what a duplicate
    backend actually is. Port 0 only chooses WHICH port is contended; the
    contention is what the flag decides, so an ephemeral port keeps the test off
    a possibly busy 9100/9110/9120.
    """
    # Negative control -- a listener that keeps the stdlib default: the second
    # one takes the live port instead of failing, and traffic splits.
    first_stock = _bind(ThreadingHTTPServer, 0)
    try:
        port = first_stock.server_address[1]
        second_stock = _bind(ThreadingHTTPServer, port)
        try:
            assert second_stock.server_address[1] == port
        finally:
            second_stock.server_close()
    finally:
        first_stock.server_close()

    first = _bind(module._Server, 0)
    try:
        with pytest.raises(OSError) as excinfo:
            _bind(module._Server, first.server_address[1])
        assert excinfo.value.errno == socket.errno.EADDRINUSE
    finally:
        first.server_close()


def test_the_scaffolded_backend_template_binds_reuse_to_the_platform() -> None:
    """A new app copies this template, so the stock flag would keep propagating."""
    template = scaffold._BACKEND_TEMPLATE
    assert 'allow_reuse_address = not sys.platform.startswith("win")' in template
    assert 'Server(("127.0.0.1", PORT), Handler).serve_forever()' in template
    assert 'HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()' not in template


# ``ThreadingHTTPServer(("127.0.0.1", 0), ...)`` -- an ephemeral port cannot
# collide, so those binds are outside this rule.
_STOCK_FIXED_BIND_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:Threading)?HTTPServer\(\s*\(\s*[\"'][^\"']+[\"']\s*,(?!\s*0\s*\))"
)


def _stock_fixed_port_binds() -> list[str]:
    hits = []
    for path in sorted(APPS_ROOT.rglob("*.py")):
        if "/tests/" in path.as_posix():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _STOCK_FIXED_BIND_RE.search(line):
                hits.append(f"{path.relative_to(APPS_ROOT).as_posix()}:{lineno}: {line.strip()}")
    return hits


def test_no_app_backend_binds_a_fixed_port_through_the_stock_server() -> None:
    """A fixed-port listener must opt out of the stdlib flag, not inherit it."""
    assert _stock_fixed_port_binds() == []


def test_the_ratchet_can_actually_fail() -> None:
    """A scan that matches nothing would pass the test above vacuously."""
    assert _STOCK_FIXED_BIND_RE.search('server = ThreadingHTTPServer(("127.0.0.1", PORT), H)')
    assert _STOCK_FIXED_BIND_RE.search('HTTPServer(("127.0.0.1", 9100), H)')
    assert not _STOCK_FIXED_BIND_RE.search('server = _Server(("127.0.0.1", PORT), H)')
    assert not _STOCK_FIXED_BIND_RE.search('ThreadingHTTPServer(("127.0.0.1", 0), bound)')
