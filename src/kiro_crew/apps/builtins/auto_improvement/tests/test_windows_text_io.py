"""Text I/O in the spine must name its encoding.

Windows' default text encoding is the active code page, not UTF-8 — cp1252 on a
US install and cp936 on the zh-CN one this app was audited on. Every site below
handles text the app itself authored or read back (ledger rows carrying
agent-written finding titles, the discovery diagnostic log, a source file in the
repo under measurement), so an unqualified ``read_text()`` / ``open()`` decodes
as cp936 there: the ledger reload raises ``UnicodeDecodeError`` out of
``_load`` (which does not guard it) and the stub proposer writes mojibake back
into the tree it is measuring.

Pinned as source assertions rather than round-trips because the defect is
invisible on the POSIX CI host, where the default encoding already is UTF-8 —
a behavioural test would pass there with or without the fix.
"""

from pathlib import Path

import kiro_crew.apps.builtins.auto_improvement.spine as spine_pkg

_SPINE = Path(spine_pkg.__file__).parent


def _src(name: str) -> str:
    return (_SPINE / name).read_text(encoding="utf-8")


def test_ledger_reads_and_appends_as_utf8():
    """The pair must agree: an append in cp936 and a read in UTF-8 is worse than
    either alone, so both call sites are pinned together."""
    src = _src("ledger.py")
    assert 'read_text(encoding="utf-8", errors="replace")' in src
    assert 'self.path.open("a", encoding="utf-8")' in src
    assert "self.path.read_text()" not in src
    assert 'self.path.open("a")' not in src


def test_discovery_diagnostic_log_is_utf8():
    """`errors="replace"` is deliberately absent: this is a write of our own
    JSON, and silently replacing a character would corrupt a diagnostic."""
    src = _src("agent_discovery.py")
    assert '"agent_discovery.log", "a", encoding="utf-8"' in src
    assert '"agent_discovery.log", "a")' not in src


def test_stub_proposer_rewrites_target_sources_as_utf8():
    src = _src("profile.py")
    assert 'existing = init.read_text(encoding="utf-8")' in src
    assert "init.read_text()" not in src
