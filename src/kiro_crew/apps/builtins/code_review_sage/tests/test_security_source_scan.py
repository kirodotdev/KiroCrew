"""Source-scan security invariants of the app's own modules.

Each case reads source text and pins a control a security scan asked for: no raw
exception text in the routes' 502 bodies, and no SHA-1 in the learning store.
Kiro Crew core's own invariants of this kind are
``test/test_security_source_scan_invariants.py``.
"""

from __future__ import annotations

from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent


class TestSageRoutesErrorDisclosure:
    """The 502 catch-all handlers must not return str(e)."""

    def test_no_raw_exception_in_502_response(self):
        src = _APP_ROOT / "backend" / "routes.py"
        text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in [src, *sorted(src.with_name("routes_parts").glob("*.py"))]
        )
        lines_502 = [line for line in text.splitlines() if "status=502" in line]
        # The scan must find the handlers, or a moved file makes it vacuous.
        assert lines_502, "no status=502 response in the routes"
        for line in lines_502:
            assert "str(e)" not in line, f"str(e) leaked in 502 response: {line.strip()}"


class TestWeakCryptography:
    """SHA-1 must be replaced."""

    def test_sage_learning_no_sha1(self):
        text = (_APP_ROOT / "sage_lib" / "learning.py").read_text(encoding="utf-8")
        assert "hashlib.sha1(" not in text, "SHA-1 must be replaced with SHA-256"
