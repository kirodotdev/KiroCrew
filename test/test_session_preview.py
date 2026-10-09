"""``GET /api/sessions/{key}`` reads a closed session without reopening it.

``POST /api/chat/slots/{slot}/resume`` clears the transcript's ``closed`` flag
and publishes a live tab, so a pane that read through it would reopen every
conversation it showed. This endpoint is the read the Older Sessions preview
makes, and these tests pin that it changes nothing: the ``closed`` flag survives, no slot appears, and the
rows it returns go through the same display render a resumed tab gets.
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import history
from kiro_crew.dashboard.handlers import sessions as sessions_handlers
from kiro_crew.history_projection import TranscriptRevisionChanged

KEY = "dashboard:old-review"


def _preview_app(state) -> web.Application:
    @web.middleware
    async def _owner(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_owner])
    app["state"] = state
    app.router.add_get("/api/sessions/{key}", sessions_handlers.api_session_detail)
    return app


def _closed_session(state, *, rows: int = 2) -> None:
    log = state.conversation_log
    for i in range(rows):
        log.append(KEY, "user" if i % 2 == 0 else "assistant", f"row-{i}")
    log.update_metadata(KEY, {"closed": True, "title": "Old review"})


def _rotate_out(state, contents: list[str]) -> None:
    """Archive *contents* as a size-rotation segment of ``KEY``'s transcript."""
    lines = [
        json.dumps({"role": "user", "content": c, "ts": f"2026-01-01T00:00:0{i}"}) + "\n"
        for i, c in enumerate(contents)
    ]
    history._archive_lines(KEY, lines, "rotate", state.conversation_log._dir)


@pytest.mark.asyncio
async def test_preview_marks_a_rotated_transcript_as_partial(tmp_path, monkeypatch):
    """Size rotation moves the transcript's head out of the live chain.

    The page reader indexes the live chain only, so without the archive probe a
    rotated session reports its newest page as the entire conversation and the
    "most recent part" notice never shows.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    _rotate_out(state, ["archived-0", "archived-1", "archived-2"])

    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 200
        body = await resp.json()

    # The page itself is unchanged: the preview still shows the newest rows.
    assert [m["content"] for m in body["messages"]] == ["row-0", "row-1"]
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_preview_says_retry_when_the_rotated_archive_is_unreadable(tmp_path, monkeypatch):
    """An unreadable archive hides whether older rows exist, so serve no page."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)

    def unreadable(*_a, **_kw):
        raise OSError("archive directory could not be enumerated")

    monkeypatch.setattr(state.conversation_log, "has_rotated_messages_chained", unreadable)
    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 503
        assert (await resp.json())["code"] == "history_corpus_unreadable"


@pytest.mark.asyncio
async def test_preview_compact_archive_is_not_partial(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    history._archive_lines(
        KEY,
        [json.dumps({"role": "user", "content": "discarded"}) + "\n"],
        "compact",
        state.conversation_log._dir,
    )

    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 200
        assert (await resp.json())["has_more"] is False


@pytest.mark.asyncio
async def test_preview_marks_a_damaged_archive_header_as_partial(tmp_path, monkeypatch):
    """A damaged header is permanent, so it must not answer an endless retry."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    archive_dir = history._archive_dir(state.conversation_log._dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stem = history._safe_key(KEY) + history.ARCHIVE_SEGMENT_DELIMITER
    (archive_dir / f"{stem}20990101-000000.jsonl").write_text("not-json\n", encoding="utf-8")

    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 200
        body = await resp.json()
    assert [m["content"] for m in body["messages"]] == ["row-0", "row-1"]
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_rotated_preview_does_not_parse_archived_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    _rotate_out(state, ["archived-0"])

    def parsed(*_a, **_kw):
        raise AssertionError("preview must not parse archived rows")

    monkeypatch.setattr(state.conversation_log, "read_rotated_messages_chained", parsed)
    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 200
        assert (await resp.json())["has_more"] is True


@pytest.mark.asyncio
async def test_preview_returns_the_transcript_and_leaves_the_session_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)

    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 200
        body = await resp.json()

    assert body["key"] == KEY
    assert body["title"] == "Old review"
    assert [m["content"] for m in body["messages"]] == ["row-0", "row-1"]
    assert body["has_more"] is False
    # The point of the endpoint: reading did not reopen anything.
    assert state.conversation_log.get_metadata(KEY).get("closed") is True
    assert state._slots == {}


@pytest.mark.asyncio
async def test_preview_pages_to_the_newest_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_handlers, "SESSION_PREVIEW_LIMIT", 3)
    state = _make_state(tmp_path)
    _closed_session(state, rows=5)

    async with TestClient(TestServer(_preview_app(state))) as client:
        body = await (await client.get(f"/api/sessions/{KEY}")).json()

    assert [m["content"] for m in body["messages"]] == ["row-2", "row-3", "row-4"]
    assert body["has_more"] is True


def _append_raw_line_separator_row(state) -> None:
    """Append a row whose JSON string holds a raw U+2028.

    ``json.dumps(ensure_ascii=False)`` leaves U+2028 unescaped, so the file line
    is one valid JSON record to a newline-framed reader and two fragments to the
    full reader's ``str.splitlines()``. The indexed page reader refuses such a
    row with ``SplitlinesBoundaryRecord`` instead of disagreeing with the full
    reader about the row count.
    """
    row = {"role": "assistant", "content": "imported\u2028text", "ts": "2026-01-01T00:00:09"}
    path = state.conversation_log._path(KEY)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.mark.asyncio
async def test_preview_falls_back_to_the_full_reader_on_a_splitlines_boundary_row(
    tmp_path, monkeypatch
):
    """The preview serves the rows the full reader serves, not a 500."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state, rows=2)
    _append_raw_line_separator_row(state)
    state.conversation_log.append(KEY, "user", "after")

    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 200
        body = await resp.json()

    expected = [m["content"] for m in state.conversation_log.read_messages_chained_full(KEY)]
    assert [m["content"] for m in body["messages"]] == expected
    assert "after" in expected and "row-0" in expected
    assert body["has_more"] is False
    assert state.conversation_log.get_metadata(KEY)["closed"] is True


@pytest.mark.asyncio
async def test_full_reader_fallback_keeps_the_newest_page_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_handlers, "SESSION_PREVIEW_LIMIT", 2)
    state = _make_state(tmp_path)
    _closed_session(state, rows=4)
    _append_raw_line_separator_row(state)
    state.conversation_log.append(KEY, "user", "last")

    async with TestClient(TestServer(_preview_app(state))) as client:
        body = await (await client.get(f"/api/sessions/{KEY}")).json()

    full = [m["content"] for m in state.conversation_log.read_messages_chained_full(KEY)]
    assert [m["content"] for m in body["messages"]] == full[-2:]
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_preview_applies_the_display_render(tmp_path, monkeypatch):
    """Rows reach the client through ``_prepare_messages``, as a resumed tab's do."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    seen: dict = {}

    def fake_prepare(messages, running, *, live_child, workspace=None):
        seen.update(running=running, live_child=live_child, rows=len(messages))
        return [{"role": m["role"], "content": "rendered"} for m in messages]

    monkeypatch.setattr(sessions_handlers, "_prepare_messages", fake_prepare)
    async with TestClient(TestServer(_preview_app(state))) as client:
        body = await (await client.get(f"/api/sessions/{KEY}")).json()

    assert [m["content"] for m in body["messages"]] == ["rendered", "rendered"]
    assert seen == {"running": False, "live_child": "", "rows": 2}


@pytest.mark.asyncio
async def test_preview_redacts_a_credential_in_the_stored_title(tmp_path, monkeypatch):
    """A title is LLM-generated text, so it carries the display redaction too."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    secret = "AKIAIOSFODNN7EXAMPLE"
    state.conversation_log.update_metadata(KEY, {"title": f"debug key {secret}"})

    async with TestClient(TestServer(_preview_app(state))) as client:
        body = await (await client.get(f"/api/sessions/{KEY}")).json()

    assert secret not in body["title"]
    assert body["title"].startswith("debug key ")


@pytest.mark.asyncio
async def test_preview_of_a_missing_session_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get("/api/sessions/dashboard:nope")
        assert resp.status == 404
        assert (await resp.json())["code"] == "session_not_found"
    assert not state.conversation_log.has_log("dashboard:nope")


@pytest.mark.asyncio
async def test_preview_deleted_during_read_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)

    def deleted(*_a, **_kw):
        raise FileNotFoundError(KEY)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained_page", deleted)
    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 404
        assert (await resp.json())["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_preview_deleted_after_empty_page_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    real_read = state.conversation_log.read_messages_chained_page

    def deleted_without_error(*args, **kwargs):
        page = real_read(*args, **kwargs)
        state.conversation_log._path(KEY).unlink()
        return type(page)([], 0, 0, False, page.revision)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained_page", deleted_without_error)
    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 404
        assert (await resp.json())["code"] == "session_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        RecursionError("maximum recursion depth exceeded"),
        ValueError("integer string conversion limit exceeded"),
    ],
    ids=["invalid-utf8", "json-depth", "value-error"],
)
async def test_preview_says_retry_when_a_transcript_row_is_undecodable(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)

    def undecodable(*_a, **_kw):
        raise failure

    monkeypatch.setattr(state.conversation_log, "read_messages_chained_page", undecodable)
    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 503
        assert await resp.json() == {
            "error": "this session's history could not be read; please retry",
            "code": "history_corpus_unreadable",
        }


@pytest.mark.asyncio
async def test_preview_says_retry_when_the_transcript_keeps_changing(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)

    def torn(*_a, **_kw):
        raise TranscriptRevisionChanged("changed")

    monkeypatch.setattr(state.conversation_log, "read_messages_chained_page", torn)
    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 503
        assert (await resp.json())["code"] == "transcript_changed"


@pytest.mark.asyncio
async def test_preview_retries_when_workspace_changes_during_read(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _closed_session(state)
    state.conversation_log.update_metadata(KEY, {"workspace": "/old"})
    real_read_page = state.conversation_log.read_messages_chained_page
    workspaces: list[str | None] = []
    page_reads = 0

    def change_after_first_page_read(*args, **kwargs):
        nonlocal page_reads
        page = real_read_page(*args, **kwargs)
        if kwargs.get("limit") == sessions_handlers.SESSION_PREVIEW_LIMIT:
            page_reads += 1
            if page_reads == 1:
                state.conversation_log.append(KEY, "assistant", "row-after-race")
                state.conversation_log.update_metadata(KEY, {"workspace": "/new"})
        return page

    def fake_prepare(messages, running, *, live_child, workspace=None):
        workspaces.append(workspace)
        return messages

    monkeypatch.setattr(
        state.conversation_log,
        "read_messages_chained_page",
        change_after_first_page_read,
    )
    monkeypatch.setattr(sessions_handlers, "_prepare_messages", fake_prepare)

    async with TestClient(TestServer(_preview_app(state))) as client:
        resp = await client.get(f"/api/sessions/{KEY}")
        assert resp.status == 200
        body = await resp.json()

    assert [m["content"] for m in body["messages"]] == ["row-0", "row-1", "row-after-race"]
    assert page_reads == 2
    assert workspaces == ["/new"]
