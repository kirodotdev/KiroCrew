"""Tests for ``/materialize`` authorization and session-doc scanning.

Covers the inode-identity authorization in ``_materialize_and_pin`` (the file
that is authorized is exactly the descriptor that is read — no TOCTOU re-resolve
window) and the malformed-history robustness of ``_scan_session_docs``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore, ArtifactValidationError
from kiro_crew.dashboard.handlers import artifacts as h


class _FakeLog:
    """Minimal conversation-log stub exposing recorded document paths.

    ``recorded`` is the list of absolute file paths the agent "produced" in a
    chat — the materialize authorization allowlist.
    """

    def __init__(self, recorded: list[str]):
        self._recorded = recorded

    def list_sessions(self):
        return [{"key": "dashboard_test", "modified": 1000.0, "title": "T"}]

    def read_messages(self, key):
        return [
            {
                "ts": "2026-01-01T00:00:00",
                "meta": {"file_changes": [{"path": p} for p in self._recorded]},
            }
        ]


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    return store


def _write_doc(tmp_path: Path, name: str, body: str = "# hello\n") -> str:
    p = tmp_path / name
    p.write_text(body)
    return str(p)


# ── authorization ────────────────────────────────────────────────────────────


def test_materialize_recorded_document_succeeds(isolated_store, tmp_path):
    doc = _write_doc(tmp_path, "note.md")
    art = h._materialize_and_pin(doc, _FakeLog([doc]))
    assert art.pinned is True
    assert art.source_path == os.path.realpath(doc)
    assert isolated_store.get(art.slug).content == "# hello\n"


def test_materialize_unrecorded_path_refused(isolated_store, tmp_path):
    """A real document that is NOT in the chat history is rejected."""
    recorded = _write_doc(tmp_path, "recorded.md")
    intruder = _write_doc(tmp_path, "intruder.md", "secret\n")
    with pytest.raises(ArtifactValidationError, match="chat history"):
        h._materialize_and_pin(intruder, _FakeLog([recorded]))


def test_materialize_relative_path_refused(isolated_store):
    with pytest.raises(ArtifactValidationError, match="absolute"):
        h._materialize_and_pin("relative/note.md", _FakeLog([]))


def test_materialize_non_document_refused(isolated_store, tmp_path):
    src = _write_doc(tmp_path, "script.py", "print(1)\n")
    with pytest.raises(ArtifactValidationError, match="document"):
        h._materialize_and_pin(src, _FakeLog([src]))


def test_materialize_symlinked_request_resolves_to_recorded_inode(isolated_store, tmp_path):
    """A symlink pointing at a recorded document is authorized by inode identity.

    The request path is a symlink; realpath() resolves it to the recorded
    document, whose inode matches the allowlist, so the read is authorized and
    reads exactly that inode.
    """
    doc = _write_doc(tmp_path, "real.md", "# real\n")
    link = tmp_path / "link.md"
    link.symlink_to(doc)
    art = h._materialize_and_pin(str(link), _FakeLog([doc]))
    assert isolated_store.get(art.slug).content == "# real\n"


def test_materialize_symlink_to_unrecorded_file_refused(isolated_store, tmp_path):
    """A symlink whose target is not a recorded document is refused by inode."""
    recorded = _write_doc(tmp_path, "recorded.md")
    outside = _write_doc(tmp_path, "outside.md", "nope\n")
    link = tmp_path / "sneaky.md"
    link.symlink_to(outside)
    with pytest.raises(ArtifactValidationError, match="chat history"):
        h._materialize_and_pin(str(link), _FakeLog([recorded]))


def test_materialize_is_idempotent(isolated_store, tmp_path):
    doc = _write_doc(tmp_path, "note.md")
    a1 = h._materialize_and_pin(doc, _FakeLog([doc]))
    a2 = h._materialize_and_pin(doc, _FakeLog([doc]))
    assert a1.slug == a2.slug
    assert a2.pinned is True
    assert len(isolated_store.list()) == 1


# ── malformed-history robustness ─────────────────────────────────────────────


class _MalformedLog:
    """Session log with malformed session/message/file-change shapes."""

    def list_sessions(self):
        return [
            "not-a-dict",
            {"key": None},
            {"key": "dashboard_ok", "modified": 5.0, "title": "ok"},
        ]

    def read_messages(self, key):
        return [
            "not-a-dict",
            {"meta": "wrong-type"},
            {"meta": {"file_changes": "wrong-type"}},
            {"meta": {"file_changes": ["not-a-dict", {"path": 123}, {"path": "/tmp/doc.md"}]}},
        ]


def test_scan_session_docs_survives_malformed_history():
    """One malformed entry must not crash listing or materialization."""
    out = h._scan_session_docs(_MalformedLog(), {})
    # Only the single well-formed document path survives.
    assert [e["path"] for e in out] == ["/tmp/doc.md"]


def test_recorded_doc_identities_skips_missing_and_relative(tmp_path):
    doc = _write_doc(tmp_path, "present.md")
    ids = h._recorded_doc_identities(_FakeLog([doc, "relative/x.md", str(tmp_path / "absent.md")]))
    st = os.stat(doc)
    assert (st.st_dev, st.st_ino) in ids
    # relative + missing entries contribute nothing
    assert len(ids) == 1


class _BadTimestampLog:
    """Session log whose ``modified`` values are non-numeric / non-finite."""

    def __init__(self, doc: str):
        self._doc = doc

    def list_sessions(self):
        return [
            {"key": "s1", "modified": "not-a-number", "title": "a"},
            {"key": "s2", "modified": float("nan"), "title": "b"},
            {"key": "s3", "modified": float("inf"), "title": "c"},
            {"key": "s4", "modified": 10.0, "title": "d"},
        ]

    def read_messages(self, key):
        return [{"meta": {"file_changes": [{"path": self._doc}]}}]


def test_scan_session_docs_survives_bad_timestamps(tmp_path):
    doc = _write_doc(tmp_path, "ts.md")
    out = h._scan_session_docs(_BadTimestampLog(doc), {})
    # The document is still discovered despite malformed timestamps.
    assert [e["path"] for e in out] == [doc]


# ── hooks chokepoint helper ───────────────────────────────────────────────────


def test_safe_read_with_identity_authorized(tmp_path):
    from kiro_crew.hooks import safe_read_file_bytes_with_identity

    p = tmp_path / "doc.md"
    p.write_text("# ok\n")
    st = os.stat(p)
    data = safe_read_file_bytes_with_identity(str(p), {(st.st_dev, st.st_ino)})
    assert data == b"# ok\n"


def test_safe_read_with_identity_rejects_unlisted_inode(tmp_path):
    from kiro_crew.hooks import safe_read_file_bytes_with_identity

    p = tmp_path / "doc.md"
    p.write_text("secret\n")
    with pytest.raises(PermissionError):
        safe_read_file_bytes_with_identity(str(p), set())  # empty allowlist


def test_safe_read_with_identity_missing_file_returns_none(tmp_path):
    from kiro_crew.hooks import safe_read_file_bytes_with_identity

    data = safe_read_file_bytes_with_identity(str(tmp_path / "nope.md"), {(1, 2)})
    assert data is None


def test_stat_identity_returns_dev_ino(tmp_path):
    from kiro_crew.hooks import stat_identity

    p = tmp_path / "doc.md"
    p.write_text("x\n")
    st = os.stat(p)
    assert stat_identity(str(p)) == (st.st_dev, st.st_ino)


def test_stat_identity_missing_returns_none(tmp_path):
    from kiro_crew.hooks import stat_identity

    assert stat_identity(str(tmp_path / "absent.md")) is None


def test_stat_identity_rejects_sensitive_path(tmp_path, monkeypatch):
    import kiro_crew.hooks as hooks_mod
    from kiro_crew.hooks import stat_identity

    p = tmp_path / "creds"
    p.write_text("secret\n")
    # Gate: a resolved target flagged sensitive is refused (never stat'd through).
    monkeypatch.setattr(hooks_mod, "is_sensitive_path", lambda _p: True)
    assert stat_identity(str(p)) is None
