"""Coverage tests for the uncovered halves of the artifact HTTP handlers.

Aimed at the branches the existing suites (``test_artifacts_handlers.py``,
``test_artifact_comment_handlers.py``, ``test_handlers_artifacts_coverage.py``)
never reach:

* ``GET /api/artifacts/session-docs`` — the whole endpoint (deny, no
  conversation log, scan failure, session scoping, saved-flag mapping),
* ``POST /api/artifacts/materialize`` — the whole endpoint (deny, body
  validation, missing conversation log, unauthorized path, real success and
  its idempotent second call),
* ``PATCH /api/artifacts/{slug}/relocate`` — every sanitizer rung
  (traversal, fixed-root containment, sensitive denylist, existence, dir) plus
  the success and pointer-clearing paths,
* the provider-push halves of the comment handlers (post / reply /
  mark-review / delete / edit) including push failure and the governance
  denial that keeps the mutation local.

Harness is the established one: MagicMock aiohttp requests, a real
:class:`ArtifactStore` rooted at ``tmp_path`` wired in as the process default,
a stub provider injected by monkeypatching ``get_provider`` (no registry
mutation, no network, no subprocess).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import (
    ArtifactComment,
    ArtifactError,
    ArtifactPublication,
    ArtifactStore,
)
from kiro_crew.dashboard.handlers import artifacts as h
from kiro_crew.publish_provider import Capability, RemoteComment

# ── Harness ──────────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    """Write UTF-8 text with LF endings preserved on every platform.

    ``Path.write_text`` opens in *text* mode, so Windows rewrites each ``\n``
    to ``\r\n`` on disk while the handlers under test read those bytes back
    untranslated. A byte-exact content assertion then passes on POSIX and
    fails only on Windows. Pinning ``newline`` disables the translation so the
    file holds exactly the bytes the test declares.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """Real ArtifactStore under tmp_path, wired as the process default.

    ``KIROCREW_HOME`` is redirected too: ``allowed_source_roots`` loads the
    config, and no test may read or write the operator's real data home.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", s)
    return s


@pytest.fixture(autouse=True)
def patch_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restriction is read off the request, so a test can flip it per call."""
    monkeypatch.setattr(
        h,
        "_is_restricted_session",
        lambda state, request: bool(request.app.get("_restricted", False)),
    )


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Collect SEL events instead of writing them to the real audit log."""
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(h, "_audit", lambda **kw: events.append(kw))
    return events


def _req(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    query: dict | None = None,
    restricted: bool = False,
    no_state: bool = False,
    conversation_log: Any = "unset",
    internal_secret: bool = False,
) -> MagicMock:
    """MagicMock aiohttp Request shaped for these handlers."""
    req = MagicMock()
    headers = {"X-Session-Key": "dashboard:test"}
    if internal_secret:
        headers["X-Internal-Secret"] = "s3cr3t-stub"
    req.headers = headers
    req.match_info = match or {}
    req.query = query or {}
    req.rel_url.query = query or {}
    if isinstance(body, dict):
        req.read = AsyncMock(return_value=json.dumps(body).encode())
    elif isinstance(body, bytes):
        req.read = AsyncMock(return_value=body)
    else:
        req.read = AsyncMock(return_value=b"")
    state = MagicMock()
    if conversation_log != "unset":
        state.conversation_log = conversation_log
    req.app = {"state": None if no_state else state, "_restricted": restricted}
    return req


def _j(resp: Any) -> dict:
    return json.loads(resp.body)


class _FakeLog:
    """Minimal conversation-log stand-in: list_sessions + read_messages."""

    def __init__(
        self,
        sessions: list[Any] | None = None,
        messages: dict[str, list[Any]] | None = None,
        *,
        sessions_raise: bool = False,
        unreadable: set[str] | None = None,
    ) -> None:
        self._sessions = sessions or []
        self._messages = messages or {}
        self._sessions_raise = sessions_raise
        self._unreadable = unreadable or set()

    def list_sessions(self) -> list[Any]:
        if self._sessions_raise:
            raise RuntimeError("history dir corrupt")
        return self._sessions

    def read_messages(self, key: str) -> list[Any]:
        if key in self._unreadable:
            raise OSError("unreadable session")
        return self._messages.get(key, [])


def _doc_change(path: str, ts: str = "2026-01-01T00:00:00Z") -> dict[str, Any]:
    """One history message recording a file change for ``path``."""
    return {"ts": ts, "meta": {"file_changes": [{"path": path}]}}


# ── GET /api/artifacts/session-docs ──────────────────────────────────────────


class TestSessionDocs:
    @pytest.mark.asyncio
    async def test_missing_state_denies_403(self, store: ArtifactStore, audit: list) -> None:
        resp = await h.api_artifact_session_docs(_req(no_state=True))
        assert resp.status == 403
        assert audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(self, store: ArtifactStore, audit: list) -> None:
        resp = await h.api_artifact_session_docs(_req(restricted=True))
        assert resp.status == 403
        assert audit[-1]["error"] == "restricted session"

    @pytest.mark.asyncio
    async def test_no_conversation_log_returns_empty(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_session_docs(_req(conversation_log=None))
        assert resp.status == 200
        assert _j(resp) == {"docs": []}

    @pytest.mark.asyncio
    async def test_lists_documents_and_skips_code(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        doc = tmp_path / "notes.md"
        _write(doc, "# notes\n")
        code = tmp_path / "main.py"
        _write(code, "x = 1\n")
        log = _FakeLog(
            sessions=[{"key": "chat-1", "title": "First", "modified": 100.0}],
            messages={"chat-1": [_doc_change(str(doc)), _doc_change(str(code))]},
        )
        resp = await h.api_artifact_session_docs(_req(conversation_log=log))
        assert resp.status == 200
        docs = _j(resp)["docs"]
        assert [d["name"] for d in docs] == ["notes.md"]
        assert docs[0]["session_title"] == "First"
        assert docs[0]["saved"] is False
        assert docs[0]["slug"] == ""

    @pytest.mark.asyncio
    async def test_saved_flag_comes_from_pinned_artifacts(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        doc = tmp_path / "saved.md"
        _write(doc, "# saved\n")
        art = store.create(
            name="saved.md", content="# saved\n", kind="markdown", source_path=str(doc)
        )
        store.set_pinned(art.slug, True)
        log = _FakeLog(
            sessions=[{"key": "chat-1", "title": "T", "modified": 1.0}],
            messages={"chat-1": [_doc_change(str(doc))]},
        )
        resp = await h.api_artifact_session_docs(_req(conversation_log=log))
        docs = _j(resp)["docs"]
        assert docs[0]["saved"] is True
        assert docs[0]["slug"] == art.slug

    @pytest.mark.asyncio
    async def test_unpinned_artifact_is_not_saved(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        # source_path matches, but the artifact is not pinned — the saved_map
        # only carries pinned records, so the doc still reads as unsaved.
        doc = tmp_path / "draft.md"
        _write(doc, "# draft\n")
        store.create(name="draft.md", content="# draft\n", kind="markdown", source_path=str(doc))
        log = _FakeLog(
            sessions=[{"key": "chat-1", "title": "T", "modified": 1.0}],
            messages={"chat-1": [_doc_change(str(doc))]},
        )
        resp = await h.api_artifact_session_docs(_req(conversation_log=log))
        assert _j(resp)["docs"][0]["saved"] is False

    @pytest.mark.asyncio
    async def test_session_query_scopes_the_scan(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        mine = tmp_path / "mine.md"
        _write(mine, "a")
        other = tmp_path / "other.md"
        _write(other, "b")
        log = _FakeLog(
            sessions=[
                {"key": "dashboard_chat-1", "title": "Mine", "modified": 2.0},
                {"key": "chat-9", "title": "Other", "modified": 3.0},
            ],
            messages={
                "dashboard_chat-1": [_doc_change(str(mine))],
                "chat-9": [_doc_change(str(other))],
            },
        )
        # A dashboard slot key maps to the ``dashboard_{slot}`` history key.
        resp = await h.api_artifact_session_docs(
            _req(conversation_log=log, query={"session": "chat-1"})
        )
        assert [d["name"] for d in _j(resp)["docs"]] == ["mine.md"]

    @pytest.mark.asyncio
    async def test_corrupt_history_yields_empty_not_500(self, store: ArtifactStore) -> None:
        # list_sessions blowing up is swallowed by the collector, so the page
        # renders empty rather than erroring.
        resp = await h.api_artifact_session_docs(
            _req(conversation_log=_FakeLog(sessions_raise=True))
        )
        assert resp.status == 200
        assert _j(resp)["docs"] == []

    @pytest.mark.asyncio
    async def test_unreadable_session_is_skipped(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        doc = tmp_path / "ok.md"
        _write(doc, "a")
        log = _FakeLog(
            sessions=[
                {"key": "bad", "title": "Bad", "modified": 9.0},
                {"key": "good", "title": "Good", "modified": 1.0},
            ],
            messages={"good": [_doc_change(str(doc))]},
            unreadable={"bad"},
        )
        resp = await h.api_artifact_session_docs(_req(conversation_log=log))
        assert [d["name"] for d in _j(resp)["docs"]] == ["ok.md"]

    @pytest.mark.asyncio
    async def test_malformed_history_entries_are_skipped(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        doc = tmp_path / "real.md"
        _write(doc, "a")
        log = _FakeLog(
            sessions=[
                "not-a-dict",
                {"title": "no key"},
                {"key": "chat-1", "title": "T", "modified": "not-a-float"},
            ],
            messages={
                "chat-1": [
                    "not-a-dict",
                    {"meta": "not-a-dict"},
                    {"meta": {"file_changes": "not-a-list"}},
                    {"meta": {"file_changes": ["not-a-dict", {"path": 42}, {"path": "  "}]}},
                    _doc_change(str(doc)),
                ]
            },
        )
        resp = await h.api_artifact_session_docs(_req(conversation_log=log))
        assert resp.status == 200
        assert [d["name"] for d in _j(resp)["docs"]] == ["real.md"]
        # A non-numeric ``modified`` degrades to 0.0, which renders as no date.
        assert _j(resp)["docs"][0]["updated_at"] == ""

    @pytest.mark.asyncio
    async def test_scan_failure_is_a_redacted_500(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, audit: list
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("token=AKIAIOSFODNN7EXAMPLE blew up")

        monkeypatch.setattr(h, "_scan_session_docs", _boom)
        resp = await h.api_artifact_session_docs(_req(conversation_log=_FakeLog()))
        assert resp.status == 500
        assert audit[-1]["outcome"] == "error"
        assert "AKIAIOSFODNN7EXAMPLE" not in audit[-1]["error"]


# ── POST /api/artifacts/materialize ──────────────────────────────────────────


class TestMaterialize:
    @pytest.mark.asyncio
    async def test_missing_state_denies_403(self, store: ArtifactStore, audit: list) -> None:
        resp = await h.api_artifact_materialize(_req(body={"path": "/x.md"}, no_state=True))
        assert resp.status == 403
        assert audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_materialize(_req(body={"path": "/x.md"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_malformed_json_400(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_materialize(_req(body=b"{not json"))
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [None, 42, "   ", ""])
    async def test_bad_path_400(self, store: ArtifactStore, path: Any) -> None:
        resp = await h.api_artifact_materialize(_req(body={"path": path}))
        assert resp.status == 400
        assert _j(resp)["error"] == "path required (must be a string)"

    @pytest.mark.asyncio
    async def test_missing_conversation_log_500(self, store: ArtifactStore, audit: list) -> None:
        resp = await h.api_artifact_materialize(
            _req(body={"path": "/tmp/a.md"}, conversation_log=None)
        )
        assert resp.status == 500
        assert _j(resp)["error"] == "conversation log unavailable"
        assert audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_relative_path_400(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_materialize(
            _req(body={"path": "notes.md"}, conversation_log=_FakeLog())
        )
        assert resp.status == 400
        assert _j(resp)["error"] == "document path must be absolute"

    @pytest.mark.asyncio
    async def test_unrecorded_document_is_refused(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        # A real .md file that no chat produced is not in the allowlist.
        doc = tmp_path / "stranger.md"
        _write(doc, "# nope\n")
        resp = await h.api_artifact_materialize(
            _req(body={"path": str(doc)}, conversation_log=_FakeLog())
        )
        assert resp.status == 400
        assert "chat history" in _j(resp)["error"]

    @pytest.mark.asyncio
    async def test_non_document_extension_refused(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        code = tmp_path / "script.py"
        _write(code, "x = 1\n")
        resp = await h.api_artifact_materialize(
            _req(body={"path": str(code)}, conversation_log=_FakeLog())
        )
        assert resp.status == 400
        assert "document files" in _j(resp)["error"]

    @pytest.mark.asyncio
    async def test_recorded_document_is_saved_and_pinned(
        self, store: ArtifactStore, tmp_path: Path, audit: list
    ) -> None:
        doc = tmp_path / "kept.md"
        _write(doc, "# kept\n\nbody\n")
        log = _FakeLog(
            sessions=[{"key": "chat-1", "title": "T", "modified": 1.0}],
            messages={"chat-1": [_doc_change(str(doc))]},
        )
        resp = await h.api_artifact_materialize(
            _req(body={"path": str(doc), "origin_session_key": "chat-1"}, conversation_log=log)
        )
        assert resp.status == 200, _j(resp)
        data = _j(resp)
        assert data["pinned"] is True
        # Observed behaviour, not the intended one: the handler serializes with
        # include_content=True, but the record it serializes comes straight from
        # ``set_pinned`` — a metadata-only mutation that returns ``_load_meta``
        # with ``content=None``. So the save response carries no body and the
        # dashboard has to re-fetch the detail endpoint. ``api_artifact_relocate``
        # avoids exactly this by re-``get``ing before serializing. Asserted so a
        # future fix (re-read before serialize) trips this test deliberately
        # rather than silently.
        assert data["content"] is None
        art = store.get(data["slug"])
        # The bytes themselves did land — only the response omits them.
        assert art.content == "# kept\n\nbody\n"
        # Compare realpath on BOTH sides: the handler canonicalizes, and a
        # Windows temp dir is the short (8.3) spelling of the same directory.
        assert os.path.realpath(art.source_path) == os.path.realpath(str(doc))

    @pytest.mark.asyncio
    async def test_second_call_is_idempotent(self, store: ArtifactStore, tmp_path: Path) -> None:
        doc = tmp_path / "twice.md"
        _write(doc, "# twice\n")
        log = _FakeLog(
            sessions=[{"key": "chat-1", "title": "T", "modified": 1.0}],
            messages={"chat-1": [_doc_change(str(doc))]},
        )
        first = _j(await h.api_artifact_materialize(_req(body={"path": str(doc)}, conversation_log=log)))
        second = _j(
            await h.api_artifact_materialize(_req(body={"path": str(doc)}, conversation_log=log))
        )
        assert first["slug"] == second["slug"]
        assert len(store.list()) == 1

    @pytest.mark.asyncio
    async def test_store_error_is_a_500(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, audit: list
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise ArtifactError("disk went away")

        monkeypatch.setattr(h, "_materialize_and_pin", _boom)
        resp = await h.api_artifact_materialize(
            _req(body={"path": "/tmp/a.md"}, conversation_log=_FakeLog())
        )
        assert resp.status == 500
        assert audit[-1]["outcome"] == "error"

    @pytest.mark.asyncio
    async def test_audited_path_is_redacted(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, audit: list
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise ArtifactError("nope")

        monkeypatch.setattr(h, "_materialize_and_pin", _boom)
        await h.api_artifact_materialize(
            _req(
                body={"path": "/tmp/AKIAIOSFODNN7EXAMPLE/a.md"},
                conversation_log=_FakeLog(),
            )
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(audit[-1]["extra"])


# ── PATCH /api/artifacts/{slug}/relocate ─────────────────────────────────────


class TestRelocate:
    @pytest.mark.asyncio
    async def test_missing_state_denies_403(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": ""}, match={"slug": "doc"}, no_state=True)
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_restricted_session_denies_403(self, store: ArtifactStore, audit: list) -> None:
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": ""}, match={"slug": "doc"}, restricted=True)
        )
        assert resp.status == 403
        assert audit[-1]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_malformed_json_400(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_relocate(_req(body=b"{oops", match={"slug": "doc"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_source_path_400(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_relocate(_req(body={}, match={"slug": "doc"}))
        assert resp.status == 400
        assert _j(resp)["error"] == "source_path is required"

    @pytest.mark.asyncio
    async def test_non_string_source_path_400(self, store: ArtifactStore) -> None:
        resp = await h.api_artifact_relocate(_req(body={"source_path": 7}, match={"slug": "doc"}))
        assert resp.status == 400
        assert _j(resp)["error"] == "source_path must be a string"

    @pytest.mark.asyncio
    async def test_traversal_denied_403(self, store: ArtifactStore, tmp_path: Path) -> None:
        traversal = str(Path(tmp_path) / ".." / "escape.md")
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": traversal}, match={"slug": "doc"})
        )
        assert resp.status == 403
        assert _j(resp)["error"] == "path traversal not allowed"

    @pytest.mark.asyncio
    async def test_outside_allowed_roots_403(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin the root set so the assertion does not depend on where the host
        # puts $HOME or the temp dir.
        monkeypatch.setattr(store, "allowed_source_roots", lambda source_root="": [tmp_path / "in"])
        (tmp_path / "in").mkdir()
        outside = tmp_path / "out.md"
        _write(outside, "x")
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": str(outside)}, match={"slug": "doc"})
        )
        assert resp.status == 403
        assert "home directory" in _j(resp)["error"]

    @pytest.mark.asyncio
    async def test_sensitive_path_403(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = tmp_path / "creds.md"
        _write(secret, "x")
        monkeypatch.setattr(h, "is_sensitive_path", lambda _p: True)
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": str(secret)}, match={"slug": "doc"})
        )
        assert resp.status == 403
        assert _j(resp)["error"] == "cannot point to a sensitive path"

    @pytest.mark.asyncio
    async def test_missing_file_400(self, store: ArtifactStore, tmp_path: Path) -> None:
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": str(tmp_path / "gone.md")}, match={"slug": "doc"})
        )
        assert resp.status == 400
        assert "does not exist" in _j(resp)["error"]

    @pytest.mark.asyncio
    async def test_directory_400(self, store: ArtifactStore, tmp_path: Path) -> None:
        target = tmp_path / "adir"
        target.mkdir()
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": str(target)}, match={"slug": "doc"})
        )
        assert resp.status == 400
        assert "not a directory" in _j(resp)["error"]

    @pytest.mark.asyncio
    async def test_unknown_slug_404(self, store: ArtifactStore, tmp_path: Path) -> None:
        f = tmp_path / "live.md"
        _write(f, "# live\n")
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": str(f)}, match={"slug": "missing"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_success_repoints_and_serves_live_bytes(
        self, store: ArtifactStore, tmp_path: Path, audit: list
    ) -> None:
        art = store.create(name="Doc", content="# stale\n", kind="markdown")
        f = tmp_path / "live.md"
        _write(f, "# live\n")
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": str(f)}, match={"slug": art.slug})
        )
        assert resp.status == 200, _j(resp)
        data = _j(resp)
        assert data["content"] == "# live\n"
        assert os.path.realpath(store.get(art.slug).source_path) == os.path.realpath(str(f))
        assert audit[-1]["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_empty_source_path_clears_the_pointer(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        f = tmp_path / "was.md"
        _write(f, "# was\n")
        art = store.create(name="Doc", content="# own\n", kind="markdown", source_path=str(f))
        resp = await h.api_artifact_relocate(
            _req(body={"source_path": ""}, match={"slug": art.slug})
        )
        assert resp.status == 200
        assert store.get(art.slug).source_path == ""


# ── Comment provider push ────────────────────────────────────────────────────


class _StubProvider:
    """Comment-capable provider stub; records calls, can fail on demand."""

    def __init__(self, caps: set[Capability], *, fail: Exception | None = None) -> None:
        self.caps = caps
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def capabilities(self) -> set[Capability]:
        return self.caps

    def _hit(self, label: str, remote_id: str = "") -> None:
        self.calls.append((label, remote_id))
        if self.fail is not None:
            raise self.fail

    async def fetch_comments(self, *, external_id: str) -> list[RemoteComment]:
        self._hit("fetch_comments", external_id)
        return []

    async def post_comment(
        self, *, external_id: str, body: str, anchor: Any = None
    ) -> RemoteComment:
        self._hit("post_comment", external_id)
        return RemoteComment(remote_id="rc-1", thread_id="rc-1", author="them", body=body)

    async def reply_comment(
        self, *, external_id: str, parent_remote_id: str, body: str
    ) -> RemoteComment:
        self._hit("reply_comment", parent_remote_id)
        return RemoteComment(
            remote_id="rc-2",
            thread_id=parent_remote_id,
            author="them",
            body=body,
            parent_id=parent_remote_id,
        )

    async def mark_review(self, *, external_id: str, remote_id: str) -> None:
        self._hit("mark_review", remote_id)

    async def delete_comment(self, *, external_id: str, remote_id: str) -> None:
        self._hit("delete_comment", remote_id)

    async def edit_comment(self, *, external_id: str, remote_id: str, body: str) -> None:
        self._hit("edit_comment", remote_id)


@pytest.fixture
def published(store: ArtifactStore) -> ArtifactStore:
    """An artifact with a publication, so comments have a sync target."""
    store.create(name="Doc", content="# Hello\n\nsome body text", slug="doc", kind="markdown")
    store.set_publication(
        "doc",
        ArtifactPublication(artifact_id="EXT1", view_url="https://x/EXT1", provider="fakeprov"),
    )
    return store


@pytest.fixture
def gate_open(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Publish-governance permits, recording which provider it was asked about."""
    asked: list[str] = []

    def _permit(_request: Any, provider_name: str) -> None:
        asked.append(provider_name)
        return None

    monkeypatch.setattr(h, "_publish_governance_denied", _permit)
    return asked


@pytest.fixture
def gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_publish_governance_denied", lambda _r, _p: "capability revoked")


def _use_provider(monkeypatch: pytest.MonkeyPatch, prov: _StubProvider) -> None:
    monkeypatch.setattr(h, "get_provider", lambda _name: prov)


def _remote_comment(
    store: ArtifactStore,
    *,
    comment_id: str = "c1",
    origin: str = "fakeprov:rc-9",
    body: str = "remote says hi",
) -> ArtifactComment:
    """Persist a provider-origin comment so local mutations route back."""
    cmt = ArtifactComment(
        id=comment_id,
        origin=origin,
        provider="fakeprov",
        scope="shared",
        author="them",
        body=body,
        thread_id=comment_id,
        status="open",
        target_provider="fakeprov",
        target_external_id="EXT1",
        sync_state="synced",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.add_comment("doc", cmt)
    return cmt


class TestPostCommentProviderPush:
    @pytest.mark.asyncio
    async def test_shared_comment_is_pushed_and_marked_synced(
        self,
        published: ArtifactStore,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: list[str],
    ) -> None:
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_post_comment(
            _req(
                body={
                    "text": "please fix",
                    "scope": "shared",
                    "anchor": {"quote": "some body text", "start_offset": 9, "end_offset": 23},
                },
                match={"slug": "doc"},
            )
        )
        assert resp.status == 201, _j(resp)
        assert _j(resp)["comment"]["sync_state"] == "synced"
        assert prov.calls == [("post_comment", "EXT1")]
        assert gate_open == ["fakeprov"]
        stored = published.list_comments("doc")[0]
        assert stored.origin == "fakeprov:rc-1"

    @pytest.mark.asyncio
    async def test_provider_failure_degrades_to_push_failed(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _use_provider(monkeypatch, _StubProvider({Capability.COMMENTS_WRITE}, fail=OSError("down")))
        resp = await h.api_artifact_post_comment(
            _req(body={"text": "hi", "scope": "shared"}, match={"slug": "doc"})
        )
        assert resp.status == 201
        assert _j(resp)["comment"]["sync_state"] == "push_failed"
        # The local comment is still stored — a push failure is not a 500.
        assert len(published.list_comments("doc")) == 1

    @pytest.mark.asyncio
    async def test_provider_without_write_capability_stays_local(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        prov = _StubProvider({Capability.COMMENTS_READ})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_post_comment(
            _req(body={"text": "hi", "scope": "shared"}, match={"slug": "doc"})
        )
        assert _j(resp)["comment"]["sync_state"] == "local_only"
        assert prov.calls == []

    @pytest.mark.asyncio
    async def test_governance_denial_keeps_comment_local(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_closed: None
    ) -> None:
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_post_comment(
            _req(body={"text": "hi", "scope": "shared"}, match={"slug": "doc"})
        )
        assert resp.status == 201
        assert _j(resp)["comment"]["sync_state"] == "local_only"
        assert prov.calls == []

    @pytest.mark.asyncio
    async def test_bad_scope_400(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_post_comment(
            _req(body={"text": "hi", "scope": "world"}, match={"slug": "doc"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_overlong_text_400(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_post_comment(
            _req(body={"text": "x" * 10001}, match={"slug": "doc"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_agent_author_and_anchor_are_redacted(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_post_comment(
            _req(
                body={
                    "text": "note",
                    "is_agent": True,
                    "author": "AKIAIOSFODNN7EXAMPLE",
                    "anchor": {
                        "quote": "AKIAIOSFODNN7EXAMPLE",
                        "prefix": "p",
                        "suffix": "s",
                        "version_number": 1,
                    },
                },
                match={"slug": "doc"},
            )
        )
        assert resp.status == 201
        stored = published.list_comments("doc")[0]
        assert stored.is_agent is True
        assert "AKIAIOSFODNN7EXAMPLE" not in stored.author
        assert "AKIAIOSFODNN7EXAMPLE" not in (stored.anchor_quote or "")


class TestReplyCommentProviderPush:
    @pytest.mark.asyncio
    async def test_reply_to_provider_thread_is_pushed(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": "on it"}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 201, _j(resp)
        assert _j(resp)["comment"]["sync_state"] == "synced"
        assert prov.calls == [("reply_comment", "rc-9")]

    @pytest.mark.asyncio
    async def test_reply_push_failure_degrades(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        _use_provider(monkeypatch, _StubProvider({Capability.COMMENTS_WRITE}, fail=OSError("x")))
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": "on it"}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert _j(resp)["comment"]["sync_state"] == "push_failed"

    @pytest.mark.asyncio
    async def test_reply_governance_denial_stays_local(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_closed: None
    ) -> None:
        _remote_comment(published)
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": "on it"}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert _j(resp)["comment"]["sync_state"] == "local_only"
        assert prov.calls == []

    @pytest.mark.asyncio
    async def test_reply_uses_parent_id_when_origin_has_no_remote_id(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        # A provider origin with no ``:`` falls back to the local parent id.
        _remote_comment(published, origin="fakeprov")
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        await h.api_artifact_reply_comment(
            _req(body={"text": "on it"}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert prov.calls == [("reply_comment", "c1")]


class TestMarkReviewProviderPush:
    @pytest.mark.asyncio
    async def test_marks_on_provider_and_locally(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_mark_review(
            _req(match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 200
        assert _j(resp)["status"] == "review"
        assert prov.calls == [("mark_review", "rc-9")]
        assert published.list_comments("doc")[0].status == "review"

    @pytest.mark.asyncio
    async def test_provider_failure_still_marks_locally(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        _use_provider(monkeypatch, _StubProvider({Capability.COMMENTS_WRITE}, fail=OSError("x")))
        resp = await h.api_artifact_mark_review(_req(match={"slug": "doc", "comment_id": "c1"}))
        assert resp.status == 200
        assert published.list_comments("doc")[0].status == "review"

    @pytest.mark.asyncio
    async def test_unknown_comment_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_mark_review(_req(match={"slug": "doc", "comment_id": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unknown_slug_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_mark_review(_req(match={"slug": "gone", "comment_id": "c1"}))
        assert resp.status == 404


class TestDeleteCommentProviderPush:
    @pytest.mark.asyncio
    async def test_human_delete_cascades_to_provider(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_delete_comment(
            _req(match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 200
        assert _j(resp)["deleted"] is True
        assert prov.calls == [("delete_comment", "rc-9")]
        assert published.list_comments("doc") == []

    @pytest.mark.asyncio
    async def test_provider_failure_still_deletes_locally(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        _use_provider(monkeypatch, _StubProvider({Capability.COMMENTS_WRITE}, fail=OSError("x")))
        resp = await h.api_artifact_delete_comment(_req(match={"slug": "doc", "comment_id": "c1"}))
        assert resp.status == 200
        assert published.list_comments("doc") == []

    @pytest.mark.asyncio
    async def test_agent_delete_of_provider_comment_403(
        self, published: ArtifactStore, audit: list
    ) -> None:
        _remote_comment(published)
        resp = await h.api_artifact_delete_comment(
            _req(
                body={"reason": "applied"},
                match={"slug": "doc", "comment_id": "c1"},
                internal_secret=True,
            )
        )
        assert resp.status == 403
        assert audit[-1]["error"] == "provider-synced comment"
        assert len(published.list_comments("doc")) == 1

    @pytest.mark.asyncio
    async def test_agent_delete_without_reason_400(
        self, published: ArtifactStore, audit: list
    ) -> None:
        resp = await h.api_artifact_delete_comment(
            _req(match={"slug": "doc", "comment_id": "c1"}, internal_secret=True)
        )
        assert resp.status == 400
        assert audit[-1]["error"] == "missing reason"

    @pytest.mark.asyncio
    async def test_agent_delete_with_reason_records_it(self, published: ArtifactStore) -> None:
        published.add_comment(
            "doc",
            ArtifactComment(
                id="local-1",
                body="fix the typo",
                thread_id="local-1",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        )
        resp = await h.api_artifact_delete_comment(
            _req(
                body={"reason": "typo fixed in v3"},
                match={"slug": "doc", "comment_id": "local-1"},
                internal_secret=True,
            )
        )
        assert resp.status == 200
        assert published.list_comments("doc") == []

    @pytest.mark.asyncio
    async def test_unknown_comment_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_delete_comment(
            _req(match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unknown_slug_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_delete_comment(
            _req(match={"slug": "gone", "comment_id": "c1"})
        )
        assert resp.status == 404


class TestEditComment:
    @pytest.mark.asyncio
    async def test_restricted_session_403(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "x"}, match={"slug": "doc", "comment_id": "c1"}, restricted=True)
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_malformed_json_400(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_edit_comment(
            _req(body=b"{oops", match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["", "   ", "y" * 10001])
    async def test_bad_text_400(self, published: ArtifactStore, text: str) -> None:
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": text}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_slug_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "x"}, match={"slug": "gone", "comment_id": "c1"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_unknown_comment_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "x"}, match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_local_edit_is_not_remote_synced(self, published: ArtifactStore) -> None:
        published.add_comment(
            "doc",
            ArtifactComment(
                id="local-1",
                body="old",
                thread_id="local-1",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        )
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "new body"}, match={"slug": "doc", "comment_id": "local-1"})
        )
        assert resp.status == 200
        assert _j(resp)["comment"]["remote_synced"] is False
        assert published.list_comments("doc")[0].body == "new body"

    @pytest.mark.asyncio
    async def test_provider_edit_capability_pushes(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        prov = _StubProvider({Capability.COMMENTS_EDIT})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "revised"}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 200
        assert _j(resp)["comment"]["remote_synced"] is True
        assert prov.calls == [("edit_comment", "rc-9")]

    @pytest.mark.asyncio
    async def test_mirror_provider_edits_locally_only(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        prov = _StubProvider({Capability.COMMENTS_WRITE})
        _use_provider(monkeypatch, prov)
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "revised"}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert _j(resp)["comment"]["remote_synced"] is False
        assert prov.calls == []
        assert published.list_comments("doc")[0].body == "revised"

    @pytest.mark.asyncio
    async def test_provider_edit_failure_keeps_local_edit(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch, gate_open: list[str]
    ) -> None:
        _remote_comment(published)
        _use_provider(monkeypatch, _StubProvider({Capability.COMMENTS_EDIT}, fail=OSError("x")))
        resp = await h.api_artifact_edit_comment(
            _req(body={"text": "revised"}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert _j(resp)["comment"]["remote_synced"] is False
        assert published.list_comments("doc")[0].body == "revised"

    @pytest.mark.asyncio
    async def test_edited_text_is_redacted(self, published: ArtifactStore) -> None:
        published.add_comment(
            "doc",
            ArtifactComment(
                id="local-1",
                body="old",
                thread_id="local-1",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            ),
        )
        await h.api_artifact_edit_comment(
            _req(
                body={"text": "creds AKIAIOSFODNN7EXAMPLE"},
                match={"slug": "doc", "comment_id": "local-1"},
            )
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in published.list_comments("doc")[0].body


class TestResolveAndReopen:
    @pytest.mark.asyncio
    async def test_agent_header_cannot_resolve(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_resolve_comment(
            _req(match={"slug": "doc", "comment_id": "c1"}, internal_secret=True)
        )
        assert resp.status == 403
        assert "human-only" in _j(resp)["error"]

    @pytest.mark.asyncio
    async def test_is_agent_body_flag_cannot_resolve(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_resolve_comment(
            _req(body={"is_agent": True}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_resolve_malformed_json_400(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_resolve_comment(
            _req(body=b"{oops", match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_resolve_unknown_slug_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_resolve_comment(
            _req(match={"slug": "gone", "comment_id": "c1"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_resolve_unknown_comment_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_resolve_comment(
            _req(match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_resolve_then_reopen(self, published: ArtifactStore) -> None:
        _remote_comment(published)
        resolved = await h.api_artifact_resolve_comment(
            _req(match={"slug": "doc", "comment_id": "c1"})
        )
        assert resolved.status == 200
        assert published.list_comments("doc")[0].status == "resolved"
        reopened = await h.api_artifact_reopen_comment(
            _req(match={"slug": "doc", "comment_id": "c1"})
        )
        assert reopened.status == 200
        assert _j(reopened)["status"] == "open"
        assert published.list_comments("doc")[0].status == "open"

    @pytest.mark.asyncio
    async def test_reopen_restricted_403(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_reopen_comment(
            _req(match={"slug": "doc", "comment_id": "c1"}, restricted=True)
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_reopen_unknown_slug_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_reopen_comment(_req(match={"slug": "gone", "comment_id": "c1"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reopen_unknown_comment_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_reopen_comment(_req(match={"slug": "doc", "comment_id": "no"}))
        assert resp.status == 404


# ── Comment endpoint guard rungs ─────────────────────────────────────────────


_COMMENT_MUTATORS = [
    ("post", "api_artifact_post_comment", "artifact_post_comment"),
    ("reply", "api_artifact_reply_comment", "artifact_reply_comment"),
    ("review", "api_artifact_mark_review", "artifact_mark_review"),
    ("resolve", "api_artifact_resolve_comment", "artifact_resolve_comment"),
    ("delete", "api_artifact_delete_comment", "artifact_delete_comment"),
]
_MUTATOR_IDS = [name for name, _handler, _tool in _COMMENT_MUTATORS]
_MUTATOR_HANDLERS = [handler for _name, handler, _tool in _COMMENT_MUTATORS]
_MUTATOR_PAIRS = [(handler, tool) for _name, handler, tool in _COMMENT_MUTATORS]


class TestCommentMutatorGuards:
    """Every comment mutator denies a restricted session and a state-less app,
    and audits which of the two it was."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler,tool", _MUTATOR_PAIRS, ids=_MUTATOR_IDS)
    async def test_restricted_session_403(
        self, published: ArtifactStore, audit: list, handler: str, tool: str
    ) -> None:
        resp = await getattr(h, handler)(
            _req(body={"text": "x"}, match={"slug": "doc", "comment_id": "c1"}, restricted=True)
        )
        assert resp.status == 403
        assert audit[-1] == {
            "tool": tool,
            "request": audit[-1]["request"],
            "outcome": "denied",
            "error": "restricted session",
            "extra": {"slug": "doc"},
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", _MUTATOR_HANDLERS, ids=_MUTATOR_IDS)
    async def test_missing_state_403(
        self, published: ArtifactStore, audit: list, handler: str
    ) -> None:
        resp = await getattr(h, handler)(
            _req(body={"text": "x"}, match={"slug": "doc", "comment_id": "c1"}, no_state=True)
        )
        assert resp.status == 403
        assert audit[-1]["error"] == "missing dashboard state"

    @pytest.mark.asyncio
    async def test_reply_malformed_json_is_audited_400(
        self, published: ArtifactStore, audit: list
    ) -> None:
        resp = await h.api_artifact_reply_comment(
            _req(body=b"{oops", match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 400
        assert audit[-1]["outcome"] == "denied"
        assert audit[-1]["extra"] == {"slug": "doc", "parent_id": "c1"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["", "   ", "z" * 10001])
    async def test_reply_bad_text_400(self, published: ArtifactStore, text: str) -> None:
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": text}, match={"slug": "doc", "comment_id": "c1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_reply_unknown_slug_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": "x"}, match={"slug": "gone", "comment_id": "c1"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reply_unknown_parent_404(self, published: ArtifactStore) -> None:
        resp = await h.api_artifact_reply_comment(
            _req(body={"text": "x"}, match={"slug": "doc", "comment_id": "nope"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_post_comment_missing_state_is_403(
        self, published: ArtifactStore, audit: list
    ) -> None:
        resp = await h.api_artifact_post_comment(
            _req(body={"text": "x"}, match={"slug": "doc"}, no_state=True)
        )
        assert resp.status == 403
        assert audit[-1]["tool"] == "artifact_post_comment"


class TestCommentsFetchOnViewErrors:
    """GET comments degrades to a 200 render when the provider misbehaves, and
    reports WHY in ``remote_sync_error``."""

    @pytest.mark.asyncio
    async def test_provider_timeout_reports_a_reason(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_provider(
            monkeypatch, _StubProvider({Capability.COMMENTS_READ}, fail=asyncio.TimeoutError())
        )
        resp = await h.api_artifact_comments(_req(match={"slug": "doc"}))
        assert resp.status == 200
        # str(TimeoutError()) is empty — the handler must not surface a blank reason.
        assert "timed out" in _j(resp)["remote_sync_error"]

    @pytest.mark.asyncio
    async def test_provider_error_is_redacted(
        self, published: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_provider(
            monkeypatch,
            _StubProvider(
                {Capability.COMMENTS_READ}, fail=RuntimeError("bad AKIAIOSFODNN7EXAMPLE creds")
            ),
        )
        resp = await h.api_artifact_comments(_req(match={"slug": "doc"}))
        assert resp.status == 200
        assert "AKIAIOSFODNN7EXAMPLE" not in _j(resp)["remote_sync_error"]


# ── _annotate_local_slugs ────────────────────────────────────────────────────


class TestAnnotateLocalSlugs:
    def test_non_list_payload_is_left_alone(self, store: ArtifactStore) -> None:
        out: dict[str, Any] = {"artifacts": "not-a-list"}
        h._annotate_local_slugs(out, {}, "fakeprov")
        assert out == {"artifacts": "not-a-list"}

    def test_malformed_rows_are_skipped(self, store: ArtifactStore) -> None:
        out: dict[str, Any] = {"artifacts": ["not-a-dict", {"id": ""}]}
        h._annotate_local_slugs(out, {}, "fakeprov")
        # The dict row gets an explicit None (no id to match); the string row
        # is skipped without crashing.
        assert out["artifacts"][0] == "not-a-dict"
        assert out["artifacts"][1]["local_slug"] is None

    def test_provider_scoped_key_wins(self, store: ArtifactStore) -> None:
        key = store.artifact_index_key("fakeprov", "EXT1")
        out: dict[str, Any] = {"artifacts": [{"external_id": "EXT1"}]}
        h._annotate_local_slugs(out, {key: "scoped-slug", "EXT1": "bare-slug"}, "fakeprov")
        assert out["artifacts"][0]["local_slug"] == "scoped-slug"

    def test_bare_id_fallback_only_for_default_provider(self, store: ArtifactStore) -> None:
        out: dict[str, Any] = {"artifacts": [{"artifactId": "EXT9"}]}
        h._annotate_local_slugs(out, {"EXT9": "legacy-slug"}, h.DEFAULT_PROVIDER)
        assert out["artifacts"][0]["local_slug"] == "legacy-slug"

        # A different provider must NOT inherit a legacy bare-id record.
        other: dict[str, Any] = {"artifacts": [{"artifactId": "EXT9"}]}
        h._annotate_local_slugs(other, {"EXT9": "legacy-slug"}, "someoneelse")
        assert other["artifacts"][0]["local_slug"] is None
