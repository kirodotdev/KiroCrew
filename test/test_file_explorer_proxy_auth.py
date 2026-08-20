"""Proxy-signature verification for the file-explorer backend.

Regression coverage for request targets that are not requote-stable: the
gateway signs the DECODED target (aiohttp's ``match_info`` /
``request.query_string``), then sends a plain-str URL that yarl re-encodes,
so the wire bytes differ from the signed form whenever the decoded text does
not survive a requote round-trip.  A space is the everyday case — signed as
``' '`` but sent as ``'+'`` — which made every request for a path like
``~/My Documents`` fail HMAC verification and the Files tree render silently
empty.
"""

import hashlib
import hmac
import time

from kiro_crew.apps.builtins.file_explorer import server

SECRET = "test-proxy-secret"


def _sign(secret: str, method: str, target: str, body: bytes = b"") -> str:
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{ts}:{method}:{target}:{body_hash}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{ts}:{sig}"


def _auth(wire_path: str, header: str, method: str = "GET", body: bytes = b"") -> bool:
    handler = server.FileExplorerHandler.__new__(server.FileExplorerHandler)
    handler.path = wire_path
    handler.headers = {"X-KiroCrew-Proxy": header}
    handler._json = lambda code, payload: None
    return handler._authorized_or_health(method, body=body)


class TestProxyAuthRequoteStability:
    def test_raw_wire_target_accepted(self, monkeypatch):
        """Historical behavior: a signature over the exact wire bytes passes."""
        monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
        wire = "/api/read?path=%2Fhome%2Fuser%2Ffile.txt"
        assert _auth(wire, _sign(SECRET, "GET", wire)) is True

    def test_decoded_target_with_space_accepted(self, monkeypatch):
        """The fix: a signature over the DECODED target must also pass, or
        every space-path request 401s and the tree renders empty."""
        monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
        wire = "/api/read?path=%2Fhome%2Fuser%2FMy+Documents%2Fnotes.md"
        decoded = "/api/read?path=/home/user/My Documents/notes.md"
        assert _auth(wire, _sign(SECRET, "GET", decoded)) is True

    def test_wrong_signature_rejected(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
        wire = "/api/read?path=%2Fhome%2Fuser%2Ffile.txt"
        assert _auth(wire, _sign("wrong-secret", "GET", wire)) is False

    def test_body_participates_in_the_signature(self, monkeypatch):
        """The optional body parameter binds the payload: the same header must
        not verify over different bytes."""
        monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
        wire = "/api/read?path=%2Fhome%2Fuser%2Ffile.txt"
        body = b"payload"
        header = _sign(SECRET, "POST", wire, body)
        assert _auth(wire, header, method="POST", body=body) is True
        assert _auth(wire, header, method="POST", body=b"tampered") is False

    def test_health_stays_unauthenticated(self):
        """The gateway's own liveness probe hits /health unsigned."""
        assert _auth("/health", "") is True
