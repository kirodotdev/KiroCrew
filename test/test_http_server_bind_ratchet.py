"""Ratchet: no new bare ``HTTPServer((host, port), ...)`` construction.

``http.server.HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` between
``bind()`` and ``listen()``. On a macOS runner that system-resolver lookup can
stall for longer than a parent's start deadline while the socket is bound but
refuses every connect, and the macOS shard goes red with
``runtime stayed {'state': 'starting'}``. ``plumb_cpu.py`` and the real-spawn
stand-in avoid it by overriding ``server_bind``.

testing-conventions.md ("``http.server`` looks the host up between ``bind()``
and ``listen()``") names the fix: subclass and call
``socketserver.TCPServer.server_bind`` so no name is resolved. The count is
exact, so a removed site records the new number in the same commit; a new
site either subclasses or records the number with its reason in the PR.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("tree_scan_http_server_bind_ratchet")

_ROOT = Path(__file__).resolve().parent.parent
_SCANNED = (_ROOT / "src" / "kiro_crew", _ROOT / "test")

# A direct construction of the stdlib class, the shape whose server_bind resolves
# the host: the class NAME followed by ``((``. Matched on code tokens, so the
# shape written in a comment or a string (a detector's own fixture) is not a
# site. A subclass (`class S(ThreadingHTTPServer)`) is not a construction.
_CLASSES = frozenset({"HTTPServer", "ThreadingHTTPServer"})
_SKIP = frozenset(
    {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}
)

_BASELINE_SITES = 15


def _lines_with_construction(text: str) -> list[int]:
    try:
        tokens = [
            tok
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
            if tok.type not in _SKIP
        ]
    except (tokenize.TokenError, SyntaxError):
        return []
    return [
        tok.start[0]
        for tok, nxt, third in zip(tokens, tokens[1:], tokens[2:])
        if tok.type == tokenize.NAME
        and tok.string in _CLASSES
        and nxt.string == "("
        and third.string == "("
    ]


def _sites() -> list[str]:
    found: list[str] = []
    for base in _SCANNED:
        for path in sorted(base.rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "HTTPServer" not in text:
                continue
            lines = text.splitlines()
            for number in _lines_with_construction(text):
                found.append(f"{path.relative_to(_ROOT)}:{number}: {lines[number - 1].strip()}")
    return found


def test_the_needle_matches_the_shipped_shape() -> None:
    assert _lines_with_construction(
        'ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()\n'
    )
    assert _lines_with_construction('srv = http.server.HTTPServer(("127.0.0.1", 0), _H)\n')
    assert not _lines_with_construction("class LoopbackServer(ThreadingHTTPServer):\n    pass\n")
    assert not _lines_with_construction(
        'LoopbackServer(("127.0.0.1", args.port), AttestedHandler)\n'
    )
    assert not _lines_with_construction('x = 1  # HTTPServer(("127.0.0.1", 8080), H)\n')
    assert not _lines_with_construction("s = \"HTTPServer(('127.0.0.1', 8080), H)\"\n")


def test_bare_http_server_constructions_match_the_recorded_count() -> None:
    sites = _sites()
    assert len(sites) == _BASELINE_SITES, (
        f"{len(sites)} bare HTTPServer((host, port), ...) construction(s) "
        f"(recorded: {_BASELINE_SITES}). Its server_bind resolves the host with "
        "socket.getfqdn, which can stall on macOS while the socket refuses connects. "
        "Subclass and call socketserver.TCPServer.server_bind (see plumb_cpu.py), or, "
        "if you REMOVED a site, set _BASELINE_SITES to the new number.\n" + "\n".join(sites)
    )
