"""Tests for :mod:`kiro_crew.dashboard.handlers.artifacts`.

Uses MagicMock requests (matching the test_dashboard_cron_channel.py pattern)
plus a real :class:`ArtifactStore` rooted at a tmp dir for end-to-end coverage.

The other routes have sibling modules: one artifact's read, delete, reprobe-notice and
version routes in ``test_artifacts_handlers_item.py``; the action routes (events, the
materialize / pin / session-doc denial audit, relocate, teardown, settle) in
``test_artifacts_handlers_actions.py``; writes (the JSON body gate, ``PATCH``, tags, the
document type, webapp metadata, the content token and dashboard-package bindings) in
``test_artifacts_handlers_update.py``. The fixtures and request builder they share live in
``artifacts_handlers_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _json_body,
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)

from conftest import cap_project_root_walk
from kiro_crew import artifacts as art_mod
from kiro_crew.dashboard.handlers.artifacts import (
    _MAX_BODY_BYTES,
    _clean_origin_session_key,
    api_artifact_detail,
    api_artifacts_create,
    api_artifacts_list,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


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
    test never reads the developer's real ``recent_projects.json``. The marker
    walk is capped at ``tmp_path`` (``conftest.cap_project_root_walk``) so only
    the ``.git`` planted here can earn a LINK, whatever sits above the host's
    temp root.
    """
    from kiro_crew import artifact_source

    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(artifact_source, "_tempdir", lambda: str(tmp_path / "tmp"))
    cap_project_root_walk(monkeypatch, tmp_path)
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
    cap_project_root_walk(monkeypatch, tmp_path)
    target = tmp / "scratch.md"
    target.write_text("# scratch", encoding="utf-8")
    return target


# ── Origin session key ──────────────────────────────────────────────────────


class TestCleanOriginSessionKey:
    """The validator judges the WHOLE client value, trailing newline included.

    Python's ``$`` matches immediately before a final newline, so a
    ``$``-anchored pattern applied with ``.match`` accepts ``"dashboard:abc\\n"``,
    and both HTTP write paths (artifact create, conversation-log materialize)
    would store it unchanged; the store compares session keys exactly, so such a
    value never matches the real session again. The pattern anchors at ``\\Z``,
    as the artifact tag validators do.
    """

    @pytest.mark.parametrize(
        "key", ["chat-2", "dashboard:chat-2", "cron:foo", "1700000000.123456", "a" * 128]
    )
    def test_key_inside_the_grammar_is_returned_unchanged(self, key: str) -> None:
        assert _clean_origin_session_key(key) == key

    @pytest.mark.parametrize("key", ["dashboard:abc\n", "a" * 128 + "\n"])
    def test_trailing_newline_collapses_to_no_origin(self, key: str) -> None:
        """The one placement a ``$`` anchor lets through."""
        assert _clean_origin_session_key(key) == ""

    @pytest.mark.parametrize("key", ["dashboard:a\nbc", "\ndashboard:abc", "dashboard:abc\n\n"])
    def test_any_other_newline_placement_still_collapses(self, key: str) -> None:
        assert _clean_origin_session_key(key) == ""

    @pytest.mark.parametrize("raw", [None, 7, ["chat-2"], "", "a" * 129, "chat 2", "<b>"])
    def test_values_outside_the_grammar_still_collapse(self, raw: object) -> None:
        assert _clean_origin_session_key(raw) == ""


# ── List ────────────────────────────────────────────────────────────────────


class TestList:
    @pytest.mark.asyncio
    async def test_empty(self, isolated_store, patch_restricted) -> None:
        resp = await api_artifacts_list(_request())
        assert resp.status == 200
        assert _json_body(resp) == {"artifacts": []}

    @pytest.mark.asyncio
    async def test_returns_items(self, isolated_store, patch_restricted, monkeypatch) -> None:
        # Stamp the two artifacts from a counter, not from the host clock. `_now_iso`
        # reads `datetime.now`, whose resolution is about 15.6 ms on Windows, so two
        # creates inside one tick carry the SAME `updated_at`; `list()` sorts on that
        # key alone, and a stable sort then falls back to directory scan order. "Newest
        # first" is undefined in that window, which made this assertion pass or fail by
        # how fast the runner was rather than by the behaviour it means to pin.
        clock = iter(range(60))
        monkeypatch.setattr(
            art_mod, "_now_iso", lambda: f"2026-01-01T00:00:{next(clock):02d}.000000+00:00"
        )
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
    async def test_snippet_drops_a_link_url_holding_a_balanced_pair(
        self, isolated_store, patch_restricted
    ) -> None:
        isolated_store.create(
            name="Doc",
            kind="markdown",
            content="See [Python](https://en.wikipedia.org/wiki/Python_(programming_language)) docs",
        )
        resp = await api_artifacts_list(_request(query={"snippet": "1"}))
        assert _json_body(resp)["artifacts"][0]["snippet"] == "See Python docs"

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
    async def test_empty_touched_by_does_not_scope(self, isolated_store, patch_restricted) -> None:
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
        # store.create() raising the base ArtifactError (e.g. a sensitive-path
        # refusal from _write_text() that fires after the duplicate-slug check
        # passes) must be distinguished from ArtifactAlreadyExistsError:
        # duplicates are 409, all other store errors are 500.
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
        # matching an already-saved widget because the lookup degraded
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
        # The dedup path must not hardcode actor='user': it infers actor from
        # X-Internal-Secret like api_artifact_update, so an MCP-driven re-save
        # appears on the activity timeline as 'iterated by agent', not
        # 'edited by user'.
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
        cap_project_root_walk(monkeypatch, tmp_path)
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
