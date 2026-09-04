"""Ratchet: app backends must not rebuild the signed proxy target DECODED.

The gateway signs the WIRE form of the forwarded request-target
(``apps/routes.py`` signs ``yarl.URL(..., encoded=True).raw_path_qs``), so
percent-escapes reach the backend still escaped. aiohttp's ``request.path``
and ``request.query_string`` are DECODED, so a backend that reconstructs the
target from them hashes a different string for any target carrying a space,
comma, ``+``, ``#`` or non-ASCII byte -- and its HMAC check fails closed with
401 "invalid or missing proxy signature" (issue #4192: in Notes, every note
whose filename holds a space was unopenable while the app shell loaded fine).

The trap is invisible in review and in manual testing, because the decoded
and encoded forms coincide for every plain-ASCII path -- which is exactly how
two backends shipped with it. The correct reconstruction is
``proxy_auth.raw_request_target(request)`` (aiohttp) or ``self.path``
(``BaseHTTPRequestHandler``, already raw). This test scans every builtin
backend that verifies the proxy HMAC and fails on a decoded reconstruction,
so a future backend cannot reintroduce the bug.
"""

from __future__ import annotations

from pathlib import Path

_BUILTINS_DIR = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "apps" / "builtins"

#: Only files that participate in proxy-HMAC verification are in scope; a
#: backend is recognised by the shared verifier import or the header name.
_HMAC_MARKERS = ("verify_proxy_request", "X-KiroCrew-Proxy")

#: The two spellings of the decoded reconstruction that shipped the bug.
#: ``request.query_string`` has no legitimate use next to the proxy HMAC --
#: the wire form is ``raw_request_target(request)`` / ``self.path``.
_DECODED_RECONSTRUCTION = ("request.query_string", "request.path +")


def test_no_builtin_rebuilds_the_signed_target_from_decoded_request() -> None:
    offenders: list[str] = []
    scanned = 0
    for server in sorted(_BUILTINS_DIR.glob("**/server.py")):
        text = server.read_text(encoding="utf-8")
        if not any(marker in text for marker in _HMAC_MARKERS):
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(pattern in line for pattern in _DECODED_RECONSTRUCTION):
                offenders.append(f"{server.relative_to(_BUILTINS_DIR)}:{lineno}: {line.strip()}")
    # If the glob or the markers ever stop matching anything, the ratchet is
    # dead and would pass vacuously -- fail loudly instead.
    assert scanned >= 2, (
        f"expected to scan at least the md_notebook and dev_fleet backends, scanned {scanned}; "
        "the discovery glob or the HMAC markers no longer match the tree"
    )
    assert not offenders, (
        "app backend rebuilds the signed proxy target from aiohttp's DECODED "
        "request.path / request.query_string; use proxy_auth.raw_request_target(request) "
        "so the verified string matches the wire form the gateway signed:\n" + "\n".join(offenders)
    )
