"""Tests for :mod:`kiro_crew.dashboard.handlers.artifacts`.

Uses MagicMock requests (matching the test_dashboard_cron_channel.py pattern)
plus a real :class:`ArtifactStore` rooted at a tmp dir for end-to-end coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers.artifacts import (
    _MAX_BODY_BYTES,
    api_artifact_delete,
    api_artifact_detail,
    api_artifact_materialize,
    api_artifact_relocate,
    api_artifact_session_docs,
    api_artifact_set_pinned,
    api_artifact_settle_blank,
    api_artifact_update,
    api_artifact_version_detail,
    api_artifact_versions,
    api_artifacts_create,
    api_artifacts_list,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """Replace the module-level default store with one rooted at tmp_path."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    return store


def _request(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    query: dict | None = None,
    session_key: str = "dashboard:test",
    restricted: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock aiohttp Request with the right shape for our handlers."""
    req = MagicMock()
    headers = {"X-Session-Key": session_key}
    if extra_headers:
        headers.update(extra_headers)
    req.headers = headers
    req.match_info = match or {}
    req.query = query or {}
    if isinstance(body, dict):
        encoded = json.dumps(body).encode()
        req.read = AsyncMock(return_value=encoded)
    elif isinstance(body, bytes):
        req.read = AsyncMock(return_value=body)
    else:
        req.read = AsyncMock(return_value=b"")
    # Attach the restricted-session flag via a stub on the request app.
    # Provide a non-None state so handlers don't short-circuit on the
    # state-is-None deny-by-default guard.
    state = MagicMock()
    # ``get_slot`` must return None (session gone) rather than a bare MagicMock:
    # for any artifact carrying a ``session_key``, the list/detail handlers
    # resolve a title through it and then redact the result, and a MagicMock
    # title blows up inside the redaction regex. None yields the real
    # "(deleted session)" string, which is a valid state for a test store.
    state.get_slot.return_value = None
    req.app = {"state": state, "_restricted_session": restricted}
    return req


@pytest.fixture
def patch_restricted(monkeypatch):
    """Make _is_restricted_session read req.app['_restricted_session']."""
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


@pytest.fixture
def linkable_project(tmp_path: Path, monkeypatch):
    """A project directory whose files earn a LINK verdict on promote.

    ``POST /api/artifacts`` now classifies ``source_path`` (see
    ``kiro_crew.artifact_source``): a disposable file is snapshotted and NO
    pointer is stored, so dedup-by-``source_path`` only applies to linked files.
    Pytest's ``tmp_path`` lives under the system temp dir — itself a disposable
    root — so the temp-root seam is narrowed to ``tmp_path/tmp`` and the project
    gets a ``.git`` marker to make it a real repo.

    Returns the project dir. Project-root discovery is stubbed to empty so the
    test never reads the developer's real ``recent_projects.json``.
    """
    from kiro_crew import artifact_source

    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(artifact_source, "_tempdir", lambda: str(tmp_path / "tmp"))
    proj = tmp_path / "project"
    (proj / ".git").mkdir(parents=True)
    return proj


@pytest.fixture
def disposable_file(tmp_path: Path, monkeypatch):
    """A file in a disposable root (temp dir), which must be COPIED not linked."""
    from kiro_crew import artifact_source

    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(artifact_source, "_tempdir", lambda: str(tmp))
    target = tmp / "scratch.md"
    target.write_text("# scratch", encoding="utf-8")
    return target


def _json_body(resp) -> dict:
    return json.loads(resp.body)


# ── List ────────────────────────────────────────────────────────────────────


class TestList:
    @pytest.mark.asyncio
    async def test_empty(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifacts_list(_request())
        assert resp.status == 200
        assert _json_body(resp) == {"artifacts": []}

    @pytest.mark.asyncio
    async def test_returns_items(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="aa")
        isolated_store.create(name="b", content="bb", tags=["x"])
        resp = await api_artifacts_list(_request())
        body = _json_body(resp)
        assert len(body["artifacts"]) == 2
        # Newest first.
        assert body["artifacts"][0]["slug"] == "b"
        # Content is not included on list responses.
        assert "content" not in body["artifacts"][0]

    @pytest.mark.asyncio
    async def test_no_snippet_by_default(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="<p>hello world</p>")
        resp = await api_artifacts_list(_request())
        assert "snippet" not in _json_body(resp)["artifacts"][0]

    @pytest.mark.asyncio
    async def test_snippet_when_requested(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="<p>hello   <b>world</b></p>")
        resp = await api_artifacts_list(_request(query={"snippet": "1"}))
        art = _json_body(resp)["artifacts"][0]
        # Tags stripped, whitespace collapsed.
        assert art["snippet"] == "hello world"

    @pytest.mark.asyncio
    async def test_snippet_truncated_and_bounded(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="word " * 200)
        resp = await api_artifacts_list(_request(query={"snippet": "1"}))
        snippet = _json_body(resp)["artifacts"][0]["snippet"]
        assert 0 < len(snippet) <= 160

    @pytest.mark.asyncio
    async def test_snippet_strips_markdown(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(
            name="Doc",
            kind="markdown",
            content="# Title\n\n**Bold** and _italic_ and `code` and [link](http://x)\n- item",
        )
        resp = await api_artifacts_list(_request(query={"snippet": "1"}))
        snip = _json_body(resp)["artifacts"][0]["snippet"]
        assert not any(ch in snip for ch in "#*_`")
        for word in ("Title", "Bold", "italic", "code", "link", "item"):
            assert word in snip

    @pytest.mark.asyncio
    async def test_content_match_finds_by_content(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="Alpha", content="the quick brown fox")
        isolated_store.create(name="Beta", content="nothing here")
        resp = await api_artifacts_list(_request(query={"q": "brown", "content": "1"}))
        assert [a["slug"] for a in _json_body(resp)["artifacts"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_content_snippet_is_match_centered(
        self, isolated_store, patch_restricted
    ) -> None:
        content = "line one\nline two\nHERE is the MATCH keyword\nline four\nline five\nline six"
        isolated_store.create(name="Doc", content=content, kind="markdown")
        resp = await api_artifacts_list(
            _request(query={"q": "match", "content": "1", "snippet": "1"})
        )
        snip = _json_body(resp)["artifacts"][0]["snippet"]
        lines = snip.split("\n")
        assert len(lines) <= 5
        assert "match" in snip.lower()  # matched term present (frontend highlights it)
        assert "line two" in snip and "line four" in snip  # a line before and after

    @pytest.mark.asyncio
    async def test_content_match_finds_by_tag(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="Alpha", content="x", tags=["ops"])
        isolated_store.create(name="Beta", content="x")
        resp = await api_artifacts_list(_request(query={"q": "ops", "content": "1"}))
        assert [a["slug"] for a in _json_body(resp)["artifacts"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_content_match_finds_by_description(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="Alpha", content="x", description="review dashboard")
        isolated_store.create(name="Beta", content="x")
        resp = await api_artifacts_list(_request(query={"q": "dashboard", "content": "1"}))
        assert [a["slug"] for a in _json_body(resp)["artifacts"]] == ["alpha"]

    @pytest.mark.asyncio
    async def test_q_without_content_flag_is_name_only(
        self, isolated_store, patch_restricted
    ) -> None:
        # A content hit must NOT match unless ?content=1 is set.
        isolated_store.create(name="Alpha", content="the quick brown fox")
        resp = await api_artifacts_list(_request(query={"q": "brown"}))
        assert _json_body(resp)["artifacts"] == []

    @pytest.mark.asyncio
    async def test_filter_by_tag(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="x")
        isolated_store.create(name="b", content="x", tags=["op"])
        resp = await api_artifacts_list(_request(query={"tag": "op"}))
        body = _json_body(resp)
        assert {a["slug"] for a in body["artifacts"]} == {"b"}

    @pytest.mark.asyncio
    async def test_filter_by_kind(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="a", content="x", kind="widget")
        isolated_store.create(name="b", content="x", kind="markdown")
        resp = await api_artifacts_list(_request(query={"kind": "markdown"}))
        body = _json_body(resp)
        assert {a["slug"] for a in body["artifacts"]} == {"b"}

    @pytest.mark.asyncio
    async def test_filter_by_q(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="CR queue", content="x")
        isolated_store.create(name="Tickets", content="x")
        resp = await api_artifacts_list(_request(query={"q": "queue"}))
        body = _json_body(resp)
        assert {a["slug"] for a in body["artifacts"]} == {"cr-queue"}

    # ── ?session= (in-session Artifacts tab) ────────────────────────────────

    @pytest.mark.asyncio
    async def test_filter_by_session(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="mine", content="x", session_key="dashboard:chat-1")
        isolated_store.create(name="theirs", content="x", session_key="dashboard:chat-2")
        resp = await api_artifacts_list(_request(query={"session": "dashboard:chat-1"}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"mine"}

    @pytest.mark.asyncio
    async def test_absent_session_does_not_scope(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="mine", content="x", session_key="dashboard:chat-1")
        isolated_store.create(name="orphan", content="x")
        resp = await api_artifacts_list(_request())
        assert len(_json_body(resp)["artifacts"]) == 2

    @pytest.mark.asyncio
    async def test_empty_session_matches_only_unattributed(
        self, isolated_store, patch_restricted
    ) -> None:
        """``?session=`` is the no-origin bucket, distinct from "don't scope"."""
        isolated_store.create(name="mine", content="x", session_key="dashboard:chat-1")
        isolated_store.create(name="orphan", content="x")
        resp = await api_artifacts_list(_request(query={"session": ""}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"orphan"}

    @pytest.mark.asyncio
    async def test_malformed_session_returns_nothing_not_the_no_origin_bucket(
        self, isolated_store, patch_restricted
    ) -> None:
        """An invalid key must match NOTHING — not the ``""`` bucket, not everything.

        The unattributed artifact here is load-bearing: every ``artifact_save``
        from the MCP path stores ``session_key=""``, so a validation miss that
        collapsed to ``""`` would hand the caller a FOREIGN artifact list. With
        only attributed fixtures this test passes either way.
        """
        isolated_store.create(name="mine", content="x", session_key="dashboard:chat-1")
        isolated_store.create(name="other", content="x", session_key="dashboard:chat-2")
        isolated_store.create(name="unattributed", content="x")  # session_key=""
        resp = await api_artifacts_list(_request(query={"session": "../../etc/passwd"}))
        assert _json_body(resp)["artifacts"] == []

    @pytest.mark.asyncio
    async def test_over_long_session_key_does_not_leak_another_session(
        self, isolated_store, patch_restricted
    ) -> None:
        """A >128-char slot key is REAL, not hostile — it must not return foreign rows.

        The artifact companion-chat flow names a slot ``Artifact: <name>`` and
        artifact names run to ``MAX_NAME_LEN`` (200), so a slot key can exceed the
        session-key grammar's 128-char cap. The write side stores that key
        verbatim, so its artifacts exist; the read side must either find them or
        find nothing — never someone else's.
        """
        long_slot = "chat-1-" + "x" * 140
        assert len(long_slot) > 128
        isolated_store.create(name="mine", content="x", session_key=long_slot)
        isolated_store.create(name="unattributed", content="x")  # session_key=""
        resp = await api_artifacts_list(_request(query={"session": long_slot}))
        slugs = {a["slug"] for a in _json_body(resp)["artifacts"]}
        assert "unattributed" not in slugs, "must not fall back to the no-origin bucket"

    # ── ?touched_by= (involvement scope: created OR read OR iterated) ───────

    @pytest.mark.asyncio
    async def test_touched_by_matches_originating_session(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="mine", content="x", session_key="chat-1")
        isolated_store.create(name="theirs", content="x", session_key="chat-2")
        resp = await api_artifacts_list(_request(query={"touched_by": "chat-1"}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"mine"}

    @pytest.mark.asyncio
    async def test_touched_by_matches_a_session_that_only_read_it(
        self, isolated_store, patch_restricted
    ) -> None:
        """The point of the filter: consumption counts, not just authorship.

        ``?session=`` cannot answer this — the artifact was authored by nobody
        and chat-1 only left a ``referenced`` breadcrumb on it.
        """
        isolated_store.create(name="read-only", content="x", slug="read-only")
        isolated_store.record_impression("read-only", by="agent", session_id="chat-1")
        resp = await api_artifacts_list(_request(query={"touched_by": "chat-1"}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"read-only"}
        # The origin-only filter still reports nothing, which is why both exist.
        resp2 = await api_artifacts_list(_request(query={"session": "chat-1"}))
        assert _json_body(resp2)["artifacts"] == []

    @pytest.mark.asyncio
    async def test_touched_by_matches_a_session_that_iterated_on_it(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="edited", content="v1", slug="edited")
        isolated_store.update(
            "edited", content="v2", session_id="chat-9", actor="agent", snapshot=True
        )
        resp = await api_artifacts_list(_request(query={"touched_by": "chat-9"}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"edited"}

    @pytest.mark.asyncio
    async def test_touched_by_excludes_untouched_artifacts(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="mine", content="x", session_key="chat-1")
        isolated_store.create(name="stranger", content="x", session_key="chat-2")
        isolated_store.create(name="orphan", content="x")
        resp = await api_artifacts_list(_request(query={"touched_by": "chat-1"}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"mine"}

    @pytest.mark.asyncio
    async def test_empty_touched_by_does_not_scope(
        self, isolated_store, patch_restricted
    ) -> None:
        """``?touched_by=`` is a no-op, NOT the no-origin bucket.

        Unlike ``?session=`` there is no useful "nobody touched it" view, so an
        empty value must not silently filter the list down to unattributed rows.
        """
        isolated_store.create(name="mine", content="x", session_key="chat-1")
        isolated_store.create(name="orphan", content="x")
        resp = await api_artifacts_list(_request(query={"touched_by": ""}))
        assert len(_json_body(resp)["artifacts"]) == 2

    @pytest.mark.asyncio
    async def test_malformed_touched_by_returns_nothing(
        self, isolated_store, patch_restricted
    ) -> None:
        """A grammar miss must match zero rows, never fall back to everything.

        The unattributed fixture is present so a collapse-to-"" bug can't pass.
        """
        isolated_store.create(name="mine", content="x", session_key="chat-1")
        isolated_store.create(name="orphan", content="x")
        resp = await api_artifacts_list(_request(query={"touched_by": "../../etc/passwd"}))
        assert _json_body(resp)["artifacts"] == []

    # ── ?pinned= ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_filter_pinned_true(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="starred", content="x")
        isolated_store.create(name="plain", content="x")
        isolated_store.set_pinned("starred", True)
        resp = await api_artifacts_list(_request(query={"pinned": "1"}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"starred"}

    @pytest.mark.asyncio
    async def test_filter_pinned_false(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="starred", content="x")
        isolated_store.create(name="plain", content="x")
        isolated_store.set_pinned("starred", True)
        resp = await api_artifacts_list(_request(query={"pinned": "0"}))
        assert {a["slug"] for a in _json_body(resp)["artifacts"]} == {"plain"}

    @pytest.mark.asyncio
    async def test_unrecognized_pinned_value_does_not_scope(
        self, isolated_store, patch_restricted
    ) -> None:
        """``?pinned=yep`` must not be read as False and silently hide starred items."""
        isolated_store.create(name="starred", content="x")
        isolated_store.create(name="plain", content="x")
        isolated_store.set_pinned("starred", True)
        resp = await api_artifacts_list(_request(query={"pinned": "yep"}))
        assert len(_json_body(resp)["artifacts"]) == 2


# ── Create ──────────────────────────────────────────────────────────────────


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_artifact(self, isolated_store, patch_restricted) -> None:
        body = {"name": "Hello", "content": "<p>hello</p>", "tags": ["greeting"]}
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 201
        result = _json_body(resp)
        assert result["slug"] == "hello"
        assert result["version"] == 1
        assert result["content"] == "<p>hello</p>"
        # Persisted on disk.
        assert (isolated_store.root / "hello" / "current.html").exists()

    @pytest.mark.asyncio
    async def test_validation_error_returns_400(self, isolated_store, patch_restricted) -> None:
        body = {"name": "", "content": "x"}
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 400
        assert "name" in _json_body(resp)["error"]

    @pytest.mark.asyncio
    async def test_duplicate_slug_returns_409(self, isolated_store, patch_restricted) -> None:
        body = {"name": "x", "content": "a", "slug": "taken"}
        await api_artifacts_create(_request(body=body))
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_save_carries_theme_contrast_warning(
        self, isolated_store, patch_restricted
    ) -> None:
        # Computed HERE at the convergence point (same relay pattern as
        # slug_collided_with) so the CLI and MCP clients both hear the same
        # verdict — the MCP-layer hint alone missed the CLI path.
        body = {"name": "Perf", "content": '<div style="color:#111">x</div>', "kind": "widget"}
        created = _json_body(await api_artifacts_create(_request(body=body)))
        assert created["theme_contrast_warning"] is True

    @pytest.mark.asyncio
    async def test_save_warning_silent_for_theme_aware_content(
        self, isolated_store, patch_restricted
    ) -> None:
        # The recommended fallback form carries a hex literal AND var(--…);
        # theme-aware content must not flag.
        body = {
            "name": "Perf2",
            "content": '<div style="color:var(--text,#111);background:var(--bg,#fff)">x</div>',
            "kind": "widget",
        }
        created = _json_body(await api_artifacts_create(_request(body=body)))
        assert created["theme_contrast_warning"] is False

    @pytest.mark.asyncio
    async def test_promoted_save_scans_persisted_content_not_body(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        # A linked-file save persists the SERVER-read bytes (promotion), so
        # the verdict must come from art.content -- a client body that lost
        # the colors (stale preview / redaction) must not suppress it.
        src = linkable_project / "page.html"
        src.write_text('<body style="color:#111">x</body>', encoding="utf-8")
        body = {
            "name": "page",
            "content": "<body>stale preview without colors</body>",
            "kind": "html",
            "source": "manual",
            "source_path": str(src),
        }
        created = _json_body(await api_artifacts_create(_request(body=body)))
        assert created["content"] == '<body style="color:#111">x</body>'
        assert created["theme_contrast_warning"] is True

    @pytest.mark.asyncio
    async def test_suffixed_slug_is_reported_in_the_response(
        self, isolated_store, patch_restricted
    ) -> None:
        # The CLI and the MCP tool are both HTTP clients, so the collision only
        # reaches them if the create response carries it.
        body = {"name": "Run Summary", "content": "first"}
        first = _json_body(await api_artifacts_create(_request(body=body)))
        assert first["slug"] == "run-summary"
        assert first["slug_collided_with"] == ""

        second = _json_body(await api_artifacts_create(_request(body=body)))
        assert second["slug"] == "run-summary-2"
        assert second["slug_collided_with"] == "run-summary"

    @pytest.mark.asyncio
    async def test_an_explicit_slug_reports_no_collision(
        self, isolated_store, patch_restricted
    ) -> None:
        # An explicit slug differs from the name-derived one by intent; the store
        # refuses a taken one outright, so comparing the two here would invent a
        # collision that never happened.
        body = {"name": "Run Summary", "content": "a", "slug": "chosen-by-hand"}
        created = _json_body(await api_artifacts_create(_request(body=body)))
        assert created["slug"] == "chosen-by-hand"
        assert created["slug_collided_with"] == ""

    @pytest.mark.asyncio
    async def test_the_collision_report_is_create_only(
        self, isolated_store, patch_restricted
    ) -> None:
        # It describes one create call, so a later read must not carry it —
        # otherwise every GET and LIST ships a permanently empty field.
        body = {"name": "Run Summary", "content": "a"}
        await api_artifacts_create(_request(body=body))
        fetched = _json_body(await api_artifact_detail(_request(match={"slug": "run-summary"})))
        # Positive control: this is a real artifact payload, so the absence below
        # is a fact about the field and not about a failed read.
        assert fetched["slug"] == "run-summary"
        assert "slug_collided_with" not in fetched

    @pytest.mark.asyncio
    async def test_restricted_session_denied(self, isolated_store, patch_restricted) -> None:
        body = {"name": "x", "content": "a"}
        resp = await api_artifacts_create(_request(body=body, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_invalid_json(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifacts_create(_request(body=b"{not json"))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_body(self, isolated_store, patch_restricted) -> None:
        big = b"x" * (_MAX_BODY_BYTES + 1)
        resp = await api_artifacts_create(_request(body=big))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # Regression: store.create() raising the base ArtifactError (e.g. a
        # sensitive-path refusal from _write_text() that fires after the
        # duplicate-slug check passes) used to be caught by the same except
        # branch as ArtifactAlreadyExistsError, returning a misleading 409. Now
        # the two are distinguished — duplicates are 409, all other store
        # errors are 500.
        from kiro_crew.artifacts import ArtifactError

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to write sensitive path: ~/.aws/credentials")

        monkeypatch.setattr(isolated_store, "create", _boom)
        body = {"name": "x", "content": "a"}
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]

    # ── source_path auto-dedup (Phase 6) ──────────────────────
    # NB: dedup keys on the STORED source_path, and only a LINK verdict stores
    # one — a disposable file is copied with no pointer at all. These tests
    # therefore promote from a real project (``linkable_project``); the
    # copy-verdict behavior is pinned in TestPromoteVerdict below.
    @pytest.mark.asyncio
    async def test_first_save_with_source_path_creates_201(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        src = linkable_project / "brd.md"
        src.write_text("# v1", encoding="utf-8")
        body = {
            "name": "brd",
            "content": "# v1",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(src),
        }
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 201
        result = _json_body(resp)
        assert result["version"] == 1
        assert result["source_path"] == str(src)
        assert result["source_root"] == str(linkable_project)

    @pytest.mark.asyncio
    async def test_resave_with_same_source_path_silently_bumps_to_200(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        src = linkable_project / "brd.md"
        src.write_text("# v1", encoding="utf-8")
        body = {
            "name": "brd",
            "content": "# v1",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(src),
        }
        first = await api_artifacts_create(_request(body=body))
        assert first.status == 201
        slug = _json_body(first)["slug"]
        # Same path, so the backend bumps the existing artifact's version rather
        # than creating a parallel duplicate. The bytes come from the FILE, not
        # from the request: a promotion names a file, and the dashboard's own
        # preview of that file is redacted and possibly truncated, so anything
        # the client echoes back cannot be trusted as the artifact's content.
        # (Editing an artifact's body is api_artifact_update's job, not this
        # endpoint's.)
        src.write_text("# v2 from disk", encoding="utf-8")
        body2 = {**body, "content": "# client-supplied, must be ignored"}
        second = await api_artifacts_create(_request(body=body2))
        assert second.status == 200  # 200 = bumped, 201 = created new
        result = _json_body(second)
        assert result["slug"] == slug
        assert result["version"] == 2
        assert result["content"] == "# v2 from disk"

    @pytest.mark.asyncio
    async def test_promotion_ignores_client_content_and_reads_the_source(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        """The dashboard preview is redacted; the artifact must not inherit that.

        ``/api/file-read`` replaces credential-shaped spans with placeholders, so
        persisting the client's copy would bake those placeholders in permanently
        -- and for a COPY there is no live pointer left to recover the real bytes.
        """
        src = linkable_project / "notes.md"
        real = "secret = swordfish-actual"
        src.write_text(real, encoding="utf-8")
        body = {
            "name": "cfg",
            "content": "secret = [REDACTED]",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(src),
        }
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 201
        assert _json_body(resp)["content"] == real

    @pytest.mark.asyncio
    async def test_promotion_of_an_oversized_source_is_refused_not_a_500(
        self, isolated_store, patch_restricted, linkable_project, monkeypatch
    ) -> None:
        # FileTooLargeError is not an OSError subclass, so an oversized source
        # escaped the promotion read as an unhandled exception and surfaced as a
        # 500. It must come back as a refusal the caller can show.
        from kiro_crew.dashboard.handlers import artifacts as _handlers

        src = linkable_project / "big.md"
        src.write_text("plenty of bytes here", encoding="utf-8")
        # The promotion read passes MAX_CONTENT_BYTES as its max_bytes, so that is
        # the cap that decides FileTooLargeError here.
        monkeypatch.setattr(_handlers, "MAX_CONTENT_BYTES", 4, raising=False)
        body = {
            "name": "big",
            "content": "ignored",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(src),
        }
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_copy_outside_allowed_roots_is_refused(
        self, isolated_store, patch_restricted, linkable_project, tmp_path_factory, monkeypatch
    ) -> None:
        """A copy outside the store's roots is refused, not silently downgraded.

        The server-side promotion read is confined to the artifact's authorizing
        root. A COPY has no project root, so if the path were kept the
        confinement would degrade to the file's OWN directory -- always a
        tautology -- and ``POST /api/artifacts {"source_path": "/etc/passwd"}``
        would read any process-readable file and persist its bytes. Outside the
        allowed roots the pointer is dropped and no server-side read happens, so
        what lands is the client's own (redacted, confined) copy.
        """
        # The allowed-roots set is NARROWED for this test instead of hunting for a
        # directory that happens to sit outside it. On Windows the temp dir lives
        # under the user profile, which IS an allowed root, so "a sibling of
        # tmp_path" is outside the roots on Linux and inside them on Windows --
        # the barrier itself is what is under test, not the platform's temp
        # layout.
        outside = tmp_path_factory.mktemp("outside") / "elsewhere.md"
        outside.write_text("BYTES THE SERVER MUST NOT READ", encoding="utf-8")
        monkeypatch.setattr(
            type(isolated_store),
            "allowed_source_roots",
            lambda self, source_root="": [Path(isolated_store.root)],
        )
        body = {
            "name": "loose",
            "content": "client copy",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(outside),
        }
        # REFUSED outright. Dropping the pointer but keeping the client's body was
        # not enough: that body came from /api/file-read, which redacts, so the
        # artifact would have silently stored placeholders as its real content.
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 400
        assert "outside the locations" in _json_body(resp).get("error", "")

    @pytest.mark.asyncio
    async def test_promotion_of_a_vanished_source_keeps_no_pointer(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        # A vanished source degrades to a PLAIN save: the verdict declines to
        # confirm the path (it must not leak whether a given file exists), so no
        # server-side read happens and the client's body is kept as ordinary
        # content. What must NOT happen is the artifact keeping a source pointer
        # it could later present as a live link to a file it never read.
        body = {
            "name": "gone",
            "content": "# stale client copy",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(linkable_project / "not-there.md"),
        }
        resp = await api_artifacts_create(_request(body=body))
        assert resp.status == 201
        result = _json_body(resp)
        assert result["content"] == "# stale client copy"
        assert not result.get("source_path")

    @pytest.mark.asyncio
    async def test_resave_with_different_source_path_creates_new(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        a = linkable_project / "a.md"
        b = linkable_project / "b.md"
        a.write_text("x", encoding="utf-8")
        b.write_text("y", encoding="utf-8")
        body1 = {
            "name": "a",
            "content": "x",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(a),
        }
        await api_artifacts_create(_request(body=body1))
        body2 = {
            "name": "b",
            "content": "y",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(b),
        }
        resp = await api_artifacts_create(_request(body=body2))
        assert resp.status == 201  # different path → new artifact
        assert _json_body(resp)["version"] == 1

    @pytest.mark.asyncio
    async def test_save_without_source_path_never_dedups(
        self, isolated_store, patch_restricted
    ) -> None:
        # Chat-backed widgets are saved without source_path. Two saves of
        # the same widget content must produce two separate artifacts —
        # NOT silently merge into one — because a chat-backed artifact's
        # identity is its slug, not its source. Regression guard for the
        # bug alice hit where a markdown file's "Add to artifacts" was
        # matching a previously-saved widget because the lookup degraded
        # to "first artifact in list".
        body = {"name": "widget", "content": "<p>hi</p>", "kind": "widget", "source": "chat"}
        first = await api_artifacts_create(_request(body=body))
        second = await api_artifacts_create(_request(body=body))
        assert first.status == 201
        assert second.status == 201  # both genuine creates
        assert _json_body(first)["slug"] != _json_body(second)["slug"]

    @pytest.mark.asyncio
    async def test_mcp_dedup_resave_tags_event_as_agent(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        # review-bot round 12: the dedup path used to hardcode actor='user' so
        # MCP-driven re-saves silently appeared on the activity timeline as
        # 'edited by user' instead of 'iterated by agent'. Now the handler
        # infers actor from X-Internal-Secret like api_artifact_update.
        src = linkable_project / "brd.md"
        src.write_text("# v1", encoding="utf-8")
        body = {
            "name": "brd",
            "content": "# v1",
            "kind": "markdown",
            "source": "manual",
            "source_path": str(src),
        }
        first = await api_artifacts_create(_request(body=body))
        assert first.status == 201
        body2 = {**body, "content": "# v2 from agent"}
        second = await api_artifacts_create(
            _request(
                body=body2,
                extra_headers={"X-Internal-Secret": "fake"},
            )
        )
        assert second.status == 200
        slug = _json_body(second)["slug"]
        # Latest event should be 'iterated' (agent), not 'edited' (user).
        art = isolated_store.get(slug)
        latest_event = art.events[-1]
        assert latest_event["type"] == "iterated"
        assert latest_event["by"] == "agent"


# ── Promote verdict: copy vs link on POST /api/artifacts ──────────────────────


class TestPromoteVerdict:
    """``POST /api/artifacts`` runs ``source_path`` through the classifier.

    Before this, the raw request string was stored unvalidated — so a project
    file outside ``$HOME`` became a pointer the store then refused to read, and
    the artifact silently served a stale snapshot while looking healthy.
    """

    @pytest.mark.asyncio
    async def test_project_file_is_linked_with_its_root(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        src = linkable_project / "spec.md"
        src.write_text("# live", encoding="utf-8")
        resp = await api_artifacts_create(
            _request(body={"name": "spec", "content": "# live", "source_path": str(src)})
        )
        assert resp.status == 201
        stored = isolated_store.get(_json_body(resp)["slug"])
        assert stored.source_path == str(src)
        assert stored.source_root == str(linkable_project)

    @pytest.mark.asyncio
    async def test_linked_artifact_reads_live_file(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        # End-to-end proof the recorded root actually authorizes the read: edit
        # the file behind the artifact's back and the GET must see it.
        src = linkable_project / "spec.md"
        src.write_text("# live", encoding="utf-8")
        created = await api_artifacts_create(
            _request(body={"name": "spec", "content": "# live", "source_path": str(src)})
        )
        slug = _json_body(created)["slug"]
        src.write_text("# edited externally", encoding="utf-8")
        resp = await api_artifact_detail(_request(match={"slug": slug}))
        body = _json_body(resp)
        assert body["content"] == "# edited externally"
        assert body["source_missing"] is False

    @pytest.mark.asyncio
    async def test_disposable_file_keeps_path_as_dedup_identity(
        self, isolated_store, patch_restricted, disposable_file
    ) -> None:
        resp = await api_artifacts_create(
            _request(
                body={
                    "name": "scratch",
                    "content": "# scratch",
                    "source_path": str(disposable_file),
                }
            )
        )
        assert resp.status == 201
        stored = isolated_store.get(_json_body(resp)["slug"])
        # A copy keeps the path as PROVENANCE + dedup identity -- it is the only
        # key find_by_source_path matches on, so dropping it made a second
        # promotion of the same file mint a duplicate artifact. Liveness is
        # carried by source_copy_only instead, so no dead pointer is created.
        assert stored.source_path == str(disposable_file)
        assert stored.source_root == ""
        assert stored.source_copy_only is True

    @pytest.mark.asyncio
    async def test_promoting_a_disposable_file_twice_does_not_duplicate(
        self, isolated_store, patch_restricted, disposable_file
    ) -> None:
        """The copy verdict must keep dedup identity.

        Regression: when a copy stored no ``source_path`` there was nothing for
        ``find_by_source_path`` to match, so pressing the promote control twice
        on the same scratch file minted two artifacts.
        """
        body = {
            "name": "scratch",
            "content": "# scratch",
            "source_path": str(disposable_file),
        }
        first = await api_artifacts_create(_request(body=dict(body)))
        assert first.status == 201
        second = await api_artifacts_create(_request(body=dict(body)))
        # Same artifact, resolved by source_path -- not a second 201.
        assert second.status == 200
        assert _json_body(second)["slug"] == _json_body(first)["slug"]
        assert len(isolated_store.list()) == 1

    @pytest.mark.asyncio
    async def test_copied_artifact_survives_source_deletion(
        self, isolated_store, patch_restricted, disposable_file
    ) -> None:
        resp = await api_artifacts_create(
            _request(
                body={
                    "name": "scratch",
                    "content": "# scratch",
                    "source_path": str(disposable_file),
                }
            )
        )
        slug = _json_body(resp)["slug"]
        disposable_file.unlink()
        detail = _json_body(await api_artifact_detail(_request(match={"slug": slug})))
        assert detail["content"] == "# scratch"
        assert detail["source_missing"] is False  # nothing was pointing at it

    @pytest.mark.asyncio
    async def test_sensitive_path_is_indistinguishable_from_a_missing_one(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ) -> None:
        """A rejected path must not reveal whether it exists.

        A copy verdict RETAINS source_path, so probing existence before the
        sensitive-path gate would let the response distinguish "this sensitive
        file is here" from "it is not". Both must look identical.
        """
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        secret = tmp_path / "creds"
        secret.write_text("SECRET", encoding="utf-8")
        monkeypatch.setattr(
            art_handlers.hooks,
            "validate_file_path",
            lambda raw: None if "creds" in str(raw) else str(raw),
        )
        present = await api_artifacts_create(
            _request(body={"name": "a", "content": "x", "source_path": str(secret)})
        )
        absent = await api_artifacts_create(
            _request(
                body={"name": "b", "content": "x", "source_path": str(tmp_path / "creds-gone")}
            )
        )
        assert present.status == absent.status == 201
        # Neither records the path, so the two responses carry the same signal.
        assert isolated_store.get(_json_body(present)["slug"]).source_path == ""
        assert isolated_store.get(_json_body(absent)["slug"]).source_path == ""

    @pytest.mark.asyncio
    async def test_nonexistent_path_is_not_stored(
        self, isolated_store, patch_restricted, linkable_project
    ) -> None:
        resp = await api_artifacts_create(
            _request(body={"name": "ghost", "content": "x", "source_path": "/p/nope.md"})
        )
        assert resp.status == 201
        assert isolated_store.get(_json_body(resp)["slug"]).source_path == ""

    @pytest.mark.asyncio
    async def test_nul_byte_source_path_is_refused_not_a_500(
        self, isolated_store, patch_restricted
    ) -> None:
        """A malformed path must be refused, never crash the request.

        ``os.path.realpath`` raises ``ValueError`` on an embedded NUL byte, which
        would otherwise escape as an HTTP 500.
        """
        resp = await api_artifacts_create(
            _request(body={"name": "x", "content": "c", "source_path": "/tmp/a\x00b.md"})
        )
        assert resp.status == 201
        assert isolated_store.get(_json_body(resp)["slug"]).source_path == ""

    @pytest.mark.asyncio
    async def test_non_string_source_path_is_ignored(
        self, isolated_store, patch_restricted
    ) -> None:
        resp = await api_artifacts_create(
            _request(body={"name": "x", "content": "c", "source_path": 42})
        )
        assert resp.status == 201
        assert isolated_store.get(_json_body(resp)["slug"]).source_path == ""

    @pytest.mark.asyncio
    async def test_request_cannot_nominate_its_own_root(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ) -> None:
        # source_root is never read from the body — only the server-side
        # classifier can set it. Otherwise a caller could authorize any root.
        from kiro_crew import artifact_source

        (tmp_path / "tmp").mkdir()
        monkeypatch.setattr(artifact_source, "_tempdir", lambda: str(tmp_path / "tmp"))
        loose = tmp_path / "loose" / "doc.md"
        loose.parent.mkdir()
        loose.write_text("x", encoding="utf-8")
        resp = await api_artifacts_create(
            _request(
                body={
                    "name": "x",
                    "content": "x",
                    "source_path": str(loose),
                    "source_root": str(tmp_path),  # attacker-supplied
                }
            )
        )
        assert resp.status == 201
        stored = isolated_store.get(_json_body(resp)["slug"])
        # The path is kept as provenance (it exists), but the ROOT the body
        # tried to nominate is not: only the classifier sets that, and it grants
        # a root solely for an observable git repo. This tree is not one, so the
        # artifact stays a copy no matter what the body claimed.
        assert stored.source_copy_only is True
        assert stored.source_root == ""
        assert stored.source_path == str(loose)


class TestDetail:
    @pytest.mark.asyncio
    async def test_returns_content(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(_request(match={"slug": "x"}))
        assert resp.status == 200
        body = _json_body(resp)
        assert body["content"] == "<x/>"

    @pytest.mark.asyncio
    async def test_missing_returns_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_detail(_request(match={"slug": "nope"}))
        assert resp.status == 404

    # ── agent-read breadcrumbs (feeds ?touched_by=) ──────────────────────────

    @pytest.mark.asyncio
    async def test_agent_read_records_referenced_event(
        self, isolated_store, patch_restricted
    ) -> None:
        """An MCP read leaves a breadcrumb so the session can list what it consumed."""
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        assert resp.status == 200
        events = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
        assert len(events) == 1
        assert events[0]["session_id"] == "chat-7"
        assert events[0]["by"] == "agent"

    @pytest.mark.asyncio
    async def test_agent_read_still_returns_content(
        self, isolated_store, patch_restricted
    ) -> None:
        """Regression guard: recording must not strip the body off the response.

        ``record_impression`` returns a meta loaded WITHOUT content, so assigning
        it over the artifact would make every agent read come back empty — the
        artifact would look blank to the agent that just asked for it.
        """
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        assert _json_body(resp)["content"] == "<x/>"

    @pytest.mark.asyncio
    async def test_browser_read_records_nothing(
        self, isolated_store, patch_restricted
    ) -> None:
        """A dashboard page view is not session activity and must not mutate."""
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_agent_read_without_internal_secret_records_nothing(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(_request(match={"slug": "x"}, session_key="chat-7"))
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_restricted_session_read_records_nothing(
        self, isolated_store, patch_restricted
    ) -> None:
        """Incognito/temporary sessions exist to leave no durable trace.

        The read itself is still served (reads are not gated here) — only the
        breadcrumb is withheld.
        """
        isolated_store.create(name="x", content="<x/>", slug="x")
        resp = await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                restricted=True,
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["content"] == "<x/>"
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_repeated_agent_reads_record_one_breadcrumb(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="<x/>", slug="x")
        req = dict(
            match={"slug": "x"},
            session_key="chat-7",
            extra_headers={"X-Internal-Secret": "s"},
        )
        await api_artifact_detail(_request(**req))
        await api_artifact_detail(_request(**req))
        events = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_missing_state_records_nothing(
        self, isolated_store, patch_restricted
    ) -> None:
        """Deny by default: no dashboard state means the breadcrumb is withheld.

        Guards the fail-open shape ``not (state is not None and restricted(...))``,
        which evaluates to True when ``state`` is falsy and would let an
        unclassifiable session — including an incognito one — write.
        """
        isolated_store.create(name="x", content="<x/>", slug="x")
        req = _request(
            match={"slug": "x"},
            session_key="chat-7",
            extra_headers={"X-Internal-Secret": "s"},
        )
        req.app = {"state": None, "_restricted_session": False}
        resp = await api_artifact_detail(req)
        assert resp.status == 200
        assert _json_body(resp)["content"] == "<x/>"
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_agent_read_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """The write is a permission decision, so it belongs in the SEL trail."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_reference"
        assert kwargs["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_withheld_breadcrumb_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """A withheld breadcrumb is still a decision — record it, mirroring
        the POST /events sibling, which audits its restricted-session denial."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(
            _request(
                match={"slug": "x"},
                session_key="chat-7",
                restricted=True,
                extra_headers={"X-Internal-Secret": "s"},
            )
        )
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_reference"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_browser_read_is_not_audited_as_a_reference(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        """A plain page view makes no decision, so it must not add SEL noise."""
        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="x", content="<x/>", slug="x")
        await api_artifact_detail(_request(match={"slug": "x"}))
        sel_stub.log_tool_invocation.assert_not_called()


class TestUpdate:
    @pytest.mark.asyncio
    async def test_content_update_carries_theme_contrast_warning(
        self, isolated_store, patch_restricted
    ) -> None:
        # The CLI's PATCH is exactly how the motivating incident's script
        # pushed its hardcoded-palette page — the verdict must ride the
        # update response, not just the MCP tool's local hint.
        isolated_store.create(name="page", content="<p>ok</p>", slug="page")
        resp = await api_artifact_update(
            _request(body={"content": '<body style="color:#111">x</body>'}, match={"slug": "page"})
        )
        assert resp.status == 200
        assert _json_body(resp)["theme_contrast_warning"] is True

    @pytest.mark.asyncio
    async def test_metadata_only_update_stamps_warning_false(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="page", content='<p style="color:#111">x</p>', slug="page")
        resp = await api_artifact_update(_request(body={"name": "Renamed"}, match={"slug": "page"}))
        assert resp.status == 200
        assert _json_body(resp)["theme_contrast_warning"] is False

    @pytest.mark.asyncio
    async def test_content_change_with_snapshot_bumps_version(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"content": "v2", "snapshot": True}, match={"slug": "x"})
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 2
        assert body["content"] == "v2"

    @pytest.mark.asyncio
    async def test_dashboard_save_without_snapshot_keeps_version(
        self, isolated_store, patch_restricted
    ) -> None:
        # New behavior (round 5, explicit-snapshot model): a
        # dashboard PATCH with no snapshot flag updates the live state but
        # does NOT bump version. Versioning becomes deliberate.
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(_request(body={"content": "v2"}, match={"slug": "x"}))
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 1  # version unchanged
        assert body["content"] == "v2"  # live state updated

    @pytest.mark.asyncio
    async def test_mcp_update_auto_snapshots(self, isolated_store, patch_restricted) -> None:
        # MCP-originated requests (X-Internal-Secret header) auto-snapshot
        # because each agent iteration is a meaningful state change worth
        # versioning.
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(
                body={"content": "v2"},
                match={"slug": "x"},
                extra_headers={"X-Internal-Secret": "fake-secret"},
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["version"] == 2  # MCP call → auto-snapshot

    @pytest.mark.asyncio
    async def test_metadata_change_no_version_bump(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"description": "updated"}, match={"slug": "x"})
        )
        body = _json_body(resp)
        assert body["version"] == 1
        assert body["description"] == "updated"

    @pytest.mark.asyncio
    async def test_missing_returns_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_update(_request(body={"content": "x"}, match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_session_denied(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_update(
            _request(body={"content": "v2"}, match={"slug": "x"}, restricted=True)
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_invalid_field_returns_400(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        # over-long description
        resp = await api_artifact_update(
            _request(body={"description": "z" * 5_000}, match={"slug": "x"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # Regression: store.update() raising the base ArtifactError (e.g. a
        # sensitive-path refusal from _write_text) used to escape the handler
        # and surface as an unhandled 500 with no audit trail. Now caught
        # explicitly and audited as an error.
        from kiro_crew.artifacts import ArtifactError

        isolated_store.create(name="x", content="v1", slug="x")

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to write sensitive path: ~/.aws/credentials")

        monkeypatch.setattr(isolated_store, "update", _boom)
        resp = await api_artifact_update(_request(body={"content": "v2"}, match={"slug": "x"}))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]


class TestDelete:
    @pytest.mark.asyncio
    async def test_deletes(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="a", slug="x")
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp) == {"ok": True}
        assert not (isolated_store.root / "x").exists()

    @pytest.mark.asyncio
    async def test_missing_404(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifact_delete(_request(match={"slug": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_denied(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="a", slug="x")
        resp = await api_artifact_delete(_request(match={"slug": "x"}, restricted=True))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_artifact_error_fallback_returns_500(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        # Regression: a base ArtifactError raised by store.delete() (e.g. a
        # future store-level sensitive-path or filesystem refusal) used to
        # escape the handler and 500 silently. Now caught and audited.
        from kiro_crew.artifacts import ArtifactError

        isolated_store.create(name="x", content="a", slug="x")

        def _boom(*_a, **_kw):
            raise ArtifactError("refusing to remove sensitive path: ~/...")

        monkeypatch.setattr(isolated_store, "delete", _boom)
        resp = await api_artifact_delete(_request(match={"slug": "x"}))
        assert resp.status == 500
        assert "sensitive path" in _json_body(resp)["error"]


# ── Versions ────────────────────────────────────────────────────────────────


class TestVersions:
    @pytest.mark.asyncio
    async def test_lists_versions(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        isolated_store.update("x", content="v2", snapshot=True)
        resp = await api_artifact_versions(_request(match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp) == {"slug": "x", "versions": [1, 2]}

    @pytest.mark.asyncio
    async def test_specific_version(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        isolated_store.update("x", content="v2")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "1"}))
        body = _json_body(resp)
        assert body["content"] == "v1"

    @pytest.mark.asyncio
    async def test_invalid_version_returns_400(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "abc"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_out_of_range_404(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="v1", slug="x")
        resp = await api_artifact_version_detail(_request(match={"slug": "x", "version": "99"}))
        assert resp.status == 404


# ── Record events (referenced) ─────────────────────────────────────────────


class TestRecordEvent:
    """Tests for ``POST /api/artifacts/<slug>/events``.

    This endpoint exists so ``WidgetFrame`` can log a ``referenced`` event
    each time a chat impression of a saved artifact mounts. It
    deliberately rejects content-mutating event types —
    those have to come through create/update so version-bump bookkeeping
    stays coupled to actual content changes.
    """

    @pytest.mark.asyncio
    async def test_referenced_event_recorded_with_metadata(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={
                    "type": "referenced",
                    "metadata": {
                        "message_ts": "1779995123.456789",
                        "widget_index": 0,
                    },
                },
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["slug"] == "x"
        assert body["event"]["type"] == "referenced"
        assert body["event"]["by"] == "user"  # X-Internal-Secret absent
        assert body["event"]["metadata"]["message_ts"] == "1779995123.456789"
        assert body["event"]["metadata"]["widget_index"] == 0
        # Verify the event is persisted in the artifact's event log.
        art = isolated_store.get("x")
        ref_events = [e for e in art.events if e.get("type") == "referenced"]
        assert len(ref_events) == 1

    @pytest.mark.asyncio
    async def test_mcp_actor_inferred_from_internal_secret(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                extra_headers={"X-Internal-Secret": "anything-non-empty"},
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["event"]["by"] == "agent"

    @pytest.mark.asyncio
    async def test_dashboard_ui_session_dropped(self, isolated_store, patch_restricted) -> None:
        # Browser dashboard sends X-Session-Key="dashboard:ui" as a default
        # for non-chat-scoped requests. That literal isn't a real slot key
        # and would mislead the activity timeline if recorded — the handler
        # explicitly drops it. Same rule as the create/update endpoints.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="dashboard:ui",
            )
        )
        assert resp.status == 200
        # No session_id key in the event.
        assert "session_id" not in _json_body(resp)["event"]

    @pytest.mark.asyncio
    async def test_real_session_key_recorded(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="chat-3-1779995123",
            )
        )
        assert resp.status == 200
        assert _json_body(resp)["event"]["session_id"] == "chat-3-1779995123"

    @pytest.mark.asyncio
    async def test_rejects_content_mutating_event_types(
        self, isolated_store, patch_restricted
    ) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        for bad_type in ("created", "edited", "iterated", "reverted"):
            resp = await api_artifact_record_event(
                _request(
                    match={"slug": "x"},
                    body={"type": bad_type},
                )
            )
            assert resp.status == 400, f"expected 400 for type={bad_type!r}, got {resp.status}"

    @pytest.mark.asyncio
    async def test_unknown_slug_returns_404(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        resp = await api_artifact_record_event(
            _request(
                match={"slug": "does-not-exist"},
                body={"type": "referenced"},
            )
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restricted_session_forbidden(self, isolated_store, patch_restricted) -> None:
        # Appending events mutates meta.json, so a restricted session must
        # be rejected with 403 like the other mutation endpoints — it must
        # not be able to flood an artifact's event log.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                restricted=True,
            )
        )
        assert resp.status == 403
        # No event recorded.
        art = isolated_store.get("x")
        assert [e for e in art.events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_suppressed_response_when_session_has_cud(
        self, isolated_store, patch_restricted
    ) -> None:
        # When the session already has a CUD event, the impression is
        # suppressed: the handler must return suppressed:true with a null
        # event, NOT a stale prior event echoed as if it were recorded.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        isolated_store.update(
            "x",
            content="<div>v2</div>",
            session_id="chat-9-1779995123",
            actor="agent",
            snapshot=True,
        )
        resp = await api_artifact_record_event(
            _request(
                match={"slug": "x"},
                body={"type": "referenced"},
                session_key="chat-9-1779995123",
            )
        )
        assert resp.status == 200
        body = _json_body(resp)
        assert body["suppressed"] is True
        assert body["event"] is None
        assert [e for e in isolated_store.get("x").events if e.get("type") == "referenced"] == []

    @pytest.mark.asyncio
    async def test_same_impression_in_session_recorded_once(
        self, isolated_store, patch_restricted
    ) -> None:
        # The reported bug: reloading / revisiting a tab clears
        # the frontend sessionStorage debounce, so the same chat session
        # re-POSTs a `referenced` for the same impression. The store must
        # record it once — the second POST returns suppressed:true.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        isolated_store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        body = {"type": "referenced", "metadata": {"message_ts": "1780036091.1", "widget_index": 0}}
        first = await api_artifact_record_event(
            _request(match={"slug": "x"}, body=body, session_key="chat-2-1780036091")
        )
        second = await api_artifact_record_event(
            _request(match={"slug": "x"}, body=body, session_key="chat-2-1780036091")
        )
        assert _json_body(first)["event"]["type"] == "referenced"
        assert _json_body(second)["suppressed"] is True
        ref = [e for e in isolated_store.get("x").events if e.get("type") == "referenced"]
        assert len(ref) == 1

    @pytest.mark.asyncio
    async def test_does_not_bump_version_or_change_content(
        self, isolated_store, patch_restricted
    ) -> None:
        # A referenced event is pure observability — it must not touch
        # the artifact's content or version. Regression guard so a
        # future refactor that accidentally routes referenced events
        # through update() (which DOES bump on content change) doesn't
        # silently turn impression-logging into a version-churn engine.
        from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event

        art = isolated_store.create(name="X", content="<div>orig</div>", slug="x", kind="widget")
        original_version = art.version
        original_content = isolated_store.get("x").content

        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-a-1")
        )
        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-b-2")
        )
        await api_artifact_record_event(
            _request(match={"slug": "x"}, body={"type": "referenced"}, session_key="chat-c-3")
        )

        post = isolated_store.get("x")
        assert post.version == original_version
        assert post.content == original_content
        ref_events = [e for e in post.events if e.get("type") == "referenced"]
        assert len(ref_events) == 3


# ── Denial audit (SEL) for new pin / materialize / session-doc routes ─────────


class TestDenialAudit:
    """Every denial/error exit on the new routes must emit a SEL audit event."""

    def _capture_sel(self, monkeypatch):
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        return sel_stub

    @pytest.mark.asyncio
    async def test_materialize_non_string_path_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_materialize(_request(body={"path": 123}))
        assert resp.status == 400
        sel_stub.log_tool_invocation.assert_called_once()
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_materialize"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_materialize_restricted_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_materialize(_request(body={"path": "/x.md"}, restricted=True))
        assert resp.status == 403
        assert sel_stub.log_tool_invocation.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_set_pinned_non_bool_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_set_pinned(_request(match={"slug": "x"}, body={"pinned": "yes"}))
        assert resp.status == 400
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_set_pinned"
        assert kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_session_docs_restricted_is_audited(
        self, isolated_store, patch_restricted, monkeypatch
    ) -> None:
        sel_stub = self._capture_sel(monkeypatch)
        resp = await api_artifact_session_docs(_request(restricted=True))
        assert resp.status == 403
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_session_docs"
        assert kwargs["outcome"] == "denied"


# ── Relocate (fixed-root containment) ─────────────────────────────────────────


class TestRelocate:
    """api_artifact_relocate confines source_path to $HOME (+ configured roots),
    so an agent cannot aim an artifact at /etc/passwd or another user's files
    and exfiltrate them via a later GET (PR #14 alice + CodeQL py/path-injection)."""

    @pytest.mark.asyncio
    async def test_home_file_allowed(self, isolated_store, patch_restricted, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        target = home / "notes.md"
        target.write_text("# hi")
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)
        assert isolated_store.get("doc").source_path == str(target.resolve())

    @pytest.mark.asyncio
    async def test_outside_home_denied(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        # The barrier now shares the store's root set, which includes the DATA
        # HOME (the store root's parent). Re-root the store under the fake home
        # — mirroring production's ~/.kiro/crew/artifacts — so "outside home"
        # is genuinely outside every allowed root and the denial still means
        # something.
        store = ArtifactStore(root=home / ".kiro" / "crew" / "artifacts")
        monkeypatch.setattr(art_mod, "_default_store", store)
        # A file OUTSIDE home (sibling tmp dir) must be refused with 403.
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("secret")
        store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(outside)})
        )
        assert resp.status == 403
        assert "home" in _json_body(resp)["error"].lower()

    @pytest.mark.asyncio
    async def test_data_home_root_allowed_third_site_pin(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        """The relocate barrier is the THIRD consumer of the allowed-roots set.

        Its private copy omitted the data-home root, so it refused paths the
        store's own read/write barriers accept. Sharing
        ``ArtifactStore.allowed_source_roots()`` closes that gap — pinned here
        because the two sites disagreeing is invisible until a user hits it.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        # isolated_store is rooted at tmp_path/artifacts → data home == tmp_path.
        target = tmp_path / "in-data-home.md"
        target.write_text("# hi", encoding="utf-8")
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)
        # …and the store agrees, which is the whole point of one producer.
        assert isolated_store._try_read_source_path(str(target)) == "# hi"
        assert isolated_store._try_write_source_path(str(target), "# edited") is True

    @pytest.mark.asyncio
    async def test_configured_extra_root_allowed(
        self, isolated_store, patch_restricted, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        extra = tmp_path / "shared"
        extra.mkdir()
        target = extra / "doc.md"
        target.write_text("# shared")
        # Configure the extra root via publish.relocate_roots.
        from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

        cfg = KiroCrewConfig()
        cfg.publish = PublishConfig(relocate_roots=[str(extra)])
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": str(target)})
        )
        assert resp.status == 200, _json_body(resp)

    @pytest.mark.asyncio
    async def test_traversal_denied(self, isolated_store, patch_restricted, monkeypatch):
        isolated_store.create(name="Doc", content="x", slug="doc", kind="markdown")
        resp = await api_artifact_relocate(
            _request(match={"slug": "doc"}, body={"source_path": "../../etc/passwd"})
        )
        assert resp.status == 403
        assert "traversal" in _json_body(resp)["error"].lower()


# ── Teardown ────────────────────────────────────────────────────────────────


class TestTeardown:
    """Tests for _handle_teardown restricted-session deny + confirm gate."""

    @pytest.fixture(autouse=True)
    def patch_deploy_restricted(self, monkeypatch):
        """Patch _is_restricted_session at the import point used by deploy handlers."""
        from kiro_crew.dashboard.handlers import _shared

        def _stub(_state, req) -> bool:
            return req.app.get("_restricted_session", False)

        monkeypatch.setattr(_shared, "_is_restricted_session", _stub)

    @pytest.mark.asyncio
    async def test_restricted_session_denied(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "x"}, body={"confirm": True}, restricted=True)
        resp = await _handle_teardown(req)
        assert resp.status == 403
        assert "restricted session" in json.loads(resp.body)["error"]

    @pytest.mark.asyncio
    async def test_missing_confirm_returns_400(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "x"}, body={})
        resp = await _handle_teardown(req)
        assert resp.status == 400
        assert "confirm" in json.loads(resp.body)["error"]

    @pytest.mark.asyncio
    async def test_confirm_false_returns_400(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "x"}, body={"confirm": False})
        resp = await _handle_teardown(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_not_found_returns_404(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        req = _request(match={"slug": "nonexistent"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_success_includes_infra_note(self, isolated_store, patch_restricted) -> None:
        from kiro_crew.deploy.handlers import _handle_teardown

        # Create a webapp artifact to tear down
        isolated_store.create(
            name="My App", content="<h1>App</h1>", slug="my-app", kind="webapp",
            webapp_metadata={"lifecycle": {"status": "deployed"}, "deploy_target": {"public_url": "https://x.cloudfront.net/my-app/"}},
        )
        req = _request(match={"slug": "my-app"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        # R39 F2: this fixture records no deploy profile, so there is no
        # manifest to expire — "skipped" (a true write failure is now a
        # retryable 502, never a silent success).
        assert body["manifest"] == "skipped"
        assert "reaper" in body["note"]

    @pytest.mark.asyncio
    async def test_manifest_expiry_success(self, isolated_store, patch_restricted, monkeypatch) -> None:
        """When engine.run_aws succeeds, manifest field should be 'expired-now'."""
        from kiro_crew.deploy import engine
        from kiro_crew.deploy import profiles as profiles_mod
        from kiro_crew.deploy.handlers import _handle_teardown

        # Register the profile so _expire_manifest_best_effort passes the registry check.
        reg = profiles_mod.load_registry()
        reg["profiles"].append(profiles_mod.make_entry("test-profile", "us-west-2"))
        reg["default"] = "test-profile"
        profiles_mod.save_registry(reg)

        isolated_store.create(
            name="Expire App", content="<h1>App</h1>", slug="expire-app", kind="webapp",
            webapp_metadata={
                "lifecycle": {"status": "deployed", "created_at": "2026-01-01T00:00:00Z"},
                # R17 F1: fail-closed identity check requires BOTH the metadata
                # and the existing manifest to carry a matching distribution_id.
                "deploy_target": {"profile": "test-profile", "region": "us-west-2",
                                  "distribution_id": "EDEPLOY1"},
                "slug": "expire-app",
            },
        )

        calls: list[tuple] = []

        def mock_run_aws(args, profile, timeout=30):
            calls.append((args, profile))
            import json as _j
            if "describe-stacks" in args:
                outputs = [
                    {"OutputKey": "BucketName", "OutputValue": "test-bucket"},
                    {"OutputKey": "DistributionId", "OutputValue": "E123"},
                    {"OutputKey": "DistributionDomainName", "OutputValue": "d.cloudfront.net"},
                ]
                return (0, _j.dumps(outputs), "")
            if "s3" in args and "cp" in args:
                if "-" in args:  # manifest READ (download to stdout)
                    return (0, _j.dumps({"slug": "expire-app",
                                         "distribution_id": "EDEPLOY1",
                                         "arch": "engine"}), "")
                return (0, "", "")
            return (0, "", "")

        monkeypatch.setattr(engine, "run_aws", mock_run_aws)
        req = _request(match={"slug": "expire-app"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["manifest"] == "expired-now"
        # R12 F2: manifest expiry now READS the existing manifest first, then
        # WRITES the patched one — two s3 cp calls, both on the same key.
        s3_calls = [c for c in calls if "s3" in c[0] and "cp" in c[0]]
        assert len(s3_calls) == 2
        for call in s3_calls:
            assert any("expire-app/.kirocrew-deploy.json" in a for a in call[0])

    @pytest.mark.asyncio
    async def test_teardown_fail_closed_when_aws_unreachable(self, isolated_store, patch_restricted, monkeypatch) -> None:
        """When engine.run_aws fails, the reaper check cannot be confirmed —
        teardown must fail closed (409, NO tombstone) rather than expiring the
        manifest into the void (round-18: reaper-not-installed guard)."""
        from kiro_crew.deploy import engine
        from kiro_crew.deploy.handlers import _handle_teardown

        isolated_store.create(
            name="Fail App", content="<h1>App</h1>", slug="fail-app", kind="webapp",
            webapp_metadata={
                "lifecycle": {"status": "deployed"},
                "deploy_target": {"profile": "bad-profile", "region": "us-west-2"},
                "slug": "fail-app",
            },
        )

        def mock_run_aws(args, profile, timeout=30):
            return (1, "", "AccessDenied")

        monkeypatch.setattr(engine, "run_aws", mock_run_aws)
        # F2 (r4): register the profile so this test exercises the
        # AWS-unreachable path, not the unregistered-profile 409.
        from kiro_crew.deploy import profiles as _profiles_mod
        monkeypatch.setattr(_profiles_mod, "load_registry", lambda: {
            "version": 2,
            "profiles": [{"name": "bad-profile", "region": "us-west-2"}],
            "default": "bad-profile",
        })
        req = _request(match={"slug": "fail-app"}, body={"confirm": True})
        resp = await _handle_teardown(req)
        assert resp.status == 409
        body = json.loads(resp.body)
        assert "reaper" in body.get("error", "").lower()
        # Artifact must NOT be tombstoned — teardown did not actually happen.
        art = isolated_store.get("fail-app")
        assert art.webapp_metadata.lifecycle.status != "expired"


class TestSettleBlankEndpoint:
    """``POST /api/artifacts/{slug}/settle`` -- the atomic leave-time resolution."""

    UNTITLED = "Untitled"

    @pytest.mark.asyncio
    async def test_deletes_an_abandoned_blank(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": ""}, match={"slug": "u"})
        )
        assert resp.status == 200
        assert _json_body(resp)["outcome"] == "deleted"

    @pytest.mark.asyncio
    async def test_saves_a_draft(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "# notes"}, match={"slug": "u"}
            )
        )
        assert _json_body(resp)["outcome"] == "saved"
        assert isolated_store.get("u").content == "# notes"

    @pytest.mark.asyncio
    async def test_keeps_an_invested_document(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="Release plan", content="# real", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "# stale"}, match={"slug": "u"}
            )
        )
        assert _json_body(resp)["outcome"] == "kept"
        assert isolated_store.get("u").content == "# real"

    @pytest.mark.asyncio
    async def test_requires_the_untitled_placeholder(
        self, isolated_store, patch_restricted
    ) -> None:
        """Without it the store cannot tell an unnamed document from a named one,
        and guessing would risk deleting a named document."""
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(_request(body={"draft": ""}, match={"slug": "u"}))
        assert resp.status == 400
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("draft", [[], {}, 7])
    async def test_refuses_a_non_string_draft(
        self, isolated_store, patch_restricted, draft
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": draft}, match={"slug": "u"})
        )
        assert resp.status == 400
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body,reason",
        [
            pytest.param({"draft": ""}, "untitled_name is required", id="missing-placeholder"),
            pytest.param(
                {"untitled_name": "  ", "draft": ""},
                "untitled_name is required",
                id="blank-placeholder",
            ),
            pytest.param(
                {"untitled_name": "Untitled", "draft": []},
                "draft must be a string",
                id="non-string-draft",
            ),
        ],
    )
    async def test_every_rejection_is_audited(
        self, isolated_store, patch_restricted, monkeypatch, body, reason
    ) -> None:
        """This endpoint can DELETE a document, so a refused attempt is as
        interesting to an auditor as a successful one -- a silent 400 would leave
        no trace of it."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import artifacts as art_handlers

        sel_stub = MagicMock()
        monkeypatch.setattr(art_handlers, "sel", lambda: sel_stub)
        isolated_store.create(name="Untitled", content="", slug="u")
        resp = await api_artifact_settle_blank(_request(body=body, match={"slug": "u"}))
        assert resp.status == 400
        kwargs = sel_stub.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "artifact_settle_blank"
        assert kwargs["outcome"] == "denied"
        assert reason in kwargs["error"]

    @pytest.mark.asyncio
    async def test_allow_delete_false_rescues_the_draft_without_deleting(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "# typed", "allow_delete": False},
                match={"slug": "u"},
            )
        )
        assert _json_body(resp)["outcome"] == "saved"
        assert isolated_store.get("u").content == "# typed"

    @pytest.mark.asyncio
    async def test_allow_delete_false_keeps_an_empty_shell(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "", "allow_delete": False},
                match={"slug": "u"},
            )
        )
        assert _json_body(resp)["outcome"] == "kept"
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    async def test_refuses_a_non_boolean_allow_delete(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name=self.UNTITLED, content="", slug="u")
        resp = await api_artifact_settle_blank(
            _request(
                body={"untitled_name": self.UNTITLED, "draft": "", "allow_delete": "no"},
                match={"slug": "u"},
            )
        )
        assert resp.status == 400
        assert isolated_store.get("u") is not None

    @pytest.mark.asyncio
    async def test_an_already_gone_document_is_not_an_error(
        self, isolated_store, patch_restricted
    ) -> None:
        """Double navigation, or deleted in another window -- nothing to settle."""
        resp = await api_artifact_settle_blank(
            _request(body={"untitled_name": self.UNTITLED, "draft": ""}, match={"slug": "gone"})
        )
        assert resp.status == 200
        assert _json_body(resp)["outcome"] == "kept"


class TestUpdateDocumentType:
    """The artifact page's type control -- ``PATCH {"kind": ...}``.

    The control exists because some types cannot be inferred from content:
    markdown and plain text accept identical bytes and differ only in whether
    ``#`` means "heading" or a literal hash. That is intent, so it has to be
    stated rather than sniffed.
    """

    @pytest.mark.asyncio
    async def test_accepts_a_selectable_kind(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": "text"}, match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp)["kind"] == "text"
        assert isolated_store.get("x").kind == "text"

    @pytest.mark.asyncio
    async def test_choosing_a_kind_does_not_bump_the_version(
        self, isolated_store, patch_restricted
    ) -> None:
        """Re-typing is a render change, not a content edit."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": "text"}, match={"slug": "x"}))
        assert _json_body(resp)["version"] == 1

    @pytest.mark.asyncio
    async def test_choosing_a_kind_pins_it_against_auto_detect(
        self, isolated_store, patch_restricted
    ) -> None:
        """The whole point of the control: a stated type must stick.

        A blank document is born auto-typed, so without pinning the next save
        would re-detect and silently overrule what the user chose.
        """
        isolated_store.create(name="x", content="", slug="x")
        assert isolated_store.get("x").kind_auto is True
        await api_artifact_update(_request(body={"kind": "text"}, match={"slug": "x"}))
        assert isolated_store.get("x").kind_auto is False
        # JSON content would normally re-type an auto-typed document.
        isolated_store.update("x", content='{"a": 1}')
        assert isolated_store.get("x").kind == "text"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["widget", "html", "webapp"])
    async def test_refuses_kinds_with_no_editor(
        self, isolated_store, patch_restricted, kind: str
    ) -> None:
        """``widget`` / ``html`` render in a sandboxed iframe with no editor, and
        ``webapp`` carries deploy metadata -- selecting one would strand the
        document the user is typing in."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": kind}, match={"slug": "x"}))
        assert resp.status == 400
        assert isolated_store.get("x").kind == "markdown"

    @pytest.mark.asyncio
    async def test_refuses_a_kind_that_is_not_a_kind(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": "../etc"}, match={"slug": "x"}))
        assert resp.status == 400
        assert isolated_store.get("x").kind == "markdown"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", [[], {}, 7, True])
    async def test_refuses_a_non_string_kind_without_crashing(
        self, isolated_store, patch_restricted, kind
    ) -> None:
        """A JSON body can carry any type here. An unhashable one raises
        TypeError on the set lookup -- a 500 where the caller deserves a 400."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"kind": kind}, match={"slug": "x"}))
        assert resp.status == 400
        assert isolated_store.get("x").kind == "markdown"

    @pytest.mark.asyncio
    async def test_omitting_kind_leaves_it_alone(self, isolated_store, patch_restricted) -> None:
        """A content save must not disturb the type, or every editor keystroke
        would fight the control."""
        isolated_store.create(name="x", content="# hi", slug="x", kind="markdown")
        resp = await api_artifact_update(_request(body={"content": "# bye"}, match={"slug": "x"}))
        assert resp.status == 200
        assert _json_body(resp)["kind"] == "markdown"


class TestUpdateWebappMetadata:
    """artifact_update accepts validated webapp_metadata (round-15)."""

    @pytest.mark.asyncio
    async def test_update_sets_webapp_metadata(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="app", content="draft app", slug="app", kind="webapp")
        resp = await api_artifact_update(
            _request(
                body={"webapp_metadata": {
                    "deploy_target": {"provider": "aws", "profile": "deploy-mvp",
                                      "public_url": "https://d.cloudfront.net/app/"},
                    "lifecycle": {"status": "live"},
                }},
                match={"slug": "app"},
            )
        )
        assert resp.status == 200
        art = isolated_store.get("app")
        assert art.webapp_metadata is not None
        assert art.webapp_metadata.deploy_target.profile == "deploy-mvp"
        assert art.webapp_metadata.lifecycle.status == "live"

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_metadata_url(self, isolated_store, patch_restricted) -> None:
        isolated_store.create(name="app2", content="draft", slug="app2", kind="webapp")
        resp = await api_artifact_update(
            _request(
                body={"webapp_metadata": {
                    "deploy_target": {"public_url": "javascript:alert(1)"},
                }},
                match={"slug": "app2"},
            )
        )
        # Bounded validator rejects non-http(s) URLs.
        assert resp.status == 400
