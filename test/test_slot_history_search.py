"""``/api/chat/slots/{slot}/history/search`` and ``/history/around``: one chat's
history search window, read in bounded pages."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_history_search as shs
from kiro_crew.history import ConversationLog
from kiro_crew.history_projection_contract import TranscriptPage
from kiro_crew.testing.synthetic_credentials import SAMPLE_SLACK_BOT_TOKEN

SEARCH = "/api/chat/slots/{slot}/history/search"
AROUND = "/api/chat/slots/{slot}/history/around"
COUNTS = "/api/chat/slots/{slot}/history/counts"


class _PagedRows:
    """A stand-in log over fixed rows that counts the rows each page decodes."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.decoded = 0

    def read_messages_chained_page(self, key, *, limit, before=None, expected_revision=None):
        total = len(self.rows)
        end = total if before is None else max(0, min(before, total))
        start = max(0, end - limit)
        self.decoded += end - start
        return TranscriptPage(self.rows[start:end], total, start, start > 0, ())

    def read_messages(self, key):
        return list(self.rows)


def _row(i: int, content: str, *, role: str = "user", day: int = 1, **meta) -> dict:
    row = {"role": role, "content": content, "ts": f"2026-11-{day:02d}T10:00:{i % 60:02d}+00:00"}
    row["meta"] = {"mid": f"m{i}", **meta}
    return row


def _epoch(day: int) -> float:
    from datetime import datetime, timezone

    return datetime(2026, 11, day, tzinfo=timezone.utc).timestamp()


def _search(rows, **kw):
    kw.setdefault("query", None)
    kw.setdefault("kind", "")
    kw.setdefault("since", None)
    kw.setdefault("until", None)
    kw.setdefault("before", None)
    kw.setdefault("limit", 30)
    return shs.search_history(rows, "k", **kw)


def test_hits_come_newest_first_with_their_row_index():
    log = _PagedRows([_row(0, "plum one"), _row(1, "other"), _row(2, "plum two")])

    body = _search(log, query=shs.message_query("plum"))

    assert [h["index"] for h in body["hits"]] == [2, 0]
    assert body["hits"][0]["mid"] == "m2"
    assert body["next_before"] is None


def test_search_retries_the_whole_response_when_rotation_lands_between_pages(monkeypatch):
    monkeypatch.setattr(shs, "SCAN_PAGE_ROWS", 2)
    log = _PagedRows([_row(i, f"plum {i}") for i in range(6)])
    read = log.read_messages_chained_page
    probes = 0
    pages = 0
    generation = 0

    def rotating(key, *, limit, before=None, expected_revision=None):
        nonlocal probes, pages, generation
        if limit == 0:
            probes += 1
        else:
            pages += 1
            if pages == 2:
                log.rows = log.rows[2:]
                generation += 1
        revision = ((key, None, generation),)
        if expected_revision is not None and expected_revision != revision:
            raise shs.TranscriptRevisionChanged("rotated between pages")
        page = read(key, limit=limit, before=before)
        return TranscriptPage(page.messages, page.total, page.next_before, page.has_more, revision)

    monkeypatch.setattr(log, "read_messages_chained_page", rotating)

    body = shs._read_settled(_search, log, query=shs.message_query("plum"))

    assert probes == 2
    assert [(hit["index"], hit["mid"]) for hit in body["hits"]] == [
        (3, "m5"),
        (2, "m4"),
        (1, "m3"),
        (0, "m2"),
    ]
    assert body["total"] == 4
    assert body["next_before"] is None


def test_a_full_page_of_hits_says_where_to_resume():
    log = _PagedRows([_row(i, f"plum {i}") for i in range(10)])

    first = _search(log, query=shs.message_query("plum"), limit=3)
    rest = _search(log, query=shs.message_query("plum"), limit=30, before=first["next_before"])

    assert [h["index"] for h in first["hits"]] == [9, 8, 7]
    assert first["next_before"] == 7
    assert [h["index"] for h in rest["hits"]] == [6, 5, 4, 3, 2, 1, 0]


def test_one_reply_scans_at_most_the_budget(monkeypatch):
    monkeypatch.setattr(shs, "SCAN_BUDGET_ROWS", 50)
    monkeypatch.setattr(shs, "SCAN_PAGE_ROWS", 20)
    log = _PagedRows([_row(i, "nothing here") for i in range(500)])

    body = _search(log, query=shs.message_query("plum"))

    assert body["hits"] == []
    assert log.decoded == 60
    assert body["next_before"] == 440


def test_the_date_range_keeps_only_rows_inside_it_and_stops_below_since():
    rows = [_row(i, "plum", day=d) for i, d in enumerate([1, 1, 3, 5, 7])]
    log = _PagedRows(rows)

    body = _search(log, query=shs.message_query("plum"), since=_epoch(3), until=_epoch(6))

    assert [h["index"] for h in body["hits"]] == [3, 2]


def test_kind_files_and_images_filter_by_what_the_row_carries(tmp_path):
    cat, chart = tmp_path / "cat.png", tmp_path / "chart.png"
    rows = [
        _row(0, "[attached_file 1] /home/u/report.pdf"),
        _row(1, "look", files=[str(cat)]),
        _row(2, f"drawn ![chart](<{chart.as_posix()}>)", role="assistant"),
        _row(3, "plain words"),
    ]
    log = _PagedRows(rows)

    files = _search(log, kind="files")
    images = _search(log, kind="images")

    assert [h["index"] for h in files["hits"]] == [1, 0]
    assert [h["index"] for h in images["hits"]] == [2, 1]
    assert images["hits"][0]["images"] == [str(chart)]
    assert images["hits"][0]["snippet"] == "drawn"
    assert files["hits"][1]["snippet"] == ""
    assert files["hits"][1]["file_count"] == 1


def test_kind_links_keeps_rows_with_a_web_link_and_the_snippet_reads_as_text():
    url = "https://github.com/kirodotdev/KiroCrew/pull/39731/files#diff-0123456789abcdef"
    rows = [
        _row(0, "no link here"),
        _row(1, f"| PR | state |\n|---|---|\n| [#39731]({url}) | **green** |", role="assistant"),
        _row(2, f"see {url}"),
    ]
    log = _PagedRows(rows)

    body = _search(log, kind="links")

    assert [h["index"] for h in body["hits"]] == [2, 1]
    assert body["hits"][0]["snippet"] == "see github.com/…"
    assert body["hits"][1]["snippet"] == "PR state #39731 green"
    assert body["hits"][1]["link_count"] == 1


def _file_row(i: int, filename: str, path: str, content_type: str) -> dict:
    """A ``file_send`` card row, as ``file_api/outbox.py`` appends it."""
    record = {"filename": filename, "path": path, "content_type": content_type, "size": 1}
    return _row(i, json.dumps(record), role="file")


def test_videos_pdfs_and_html_come_from_attachments_sent_files_and_inline_refs():
    rows = [
        _row(0, "[attached_file 1] /home/u/clip.mp4"),
        _row(1, "notes", files=["/home/u/spec.pdf"]),
        _row(2, "watch ![demo](/home/u/demo.webm)", role="assistant"),
        _file_row(3, "report.html", "/out/report.html", "text/html"),
        _file_row(4, "paper", "/out/paper", "application/pdf"),
        _file_row(5, "talk.mov", "/out/talk.mov", "video/quicktime"),
        _row(6, "plain words"),
        _row(7, "", role="file"),
    ]
    log = _PagedRows(rows)

    videos = _search(log, kind="videos")
    pdfs = _search(log, kind="pdfs")
    html = _search(log, kind="html")

    assert [h["index"] for h in videos["hits"]] == [5, 2, 0]
    assert [h["index"] for h in pdfs["hits"]] == [4, 1]
    assert [h["index"] for h in html["hits"]] == [3]
    sent = videos["hits"][0]
    assert (sent["role"], sent["snippet"], sent["video_names"]) == (
        "file",
        "talk.mov",
        ["talk.mov"],
    )
    assert videos["hits"][1]["video_names"] == ["demo.webm"]
    # The chat plays a video ref, so it is not a picture.
    assert "image_count" not in videos["hits"][1]
    assert (pdfs["hits"][0]["pdf_count"], pdfs["hits"][0]["pdf_names"]) == (1, ["paper"])
    assert html["hits"][0]["html_names"] == ["report.html"]
    assert [h["index"] for h in _search(log, query=shs.message_query("report"))["hits"]] == [3]
    assert videos["counts"] == {
        "": 7,
        "files": 5,
        "images": 0,
        "links": 0,
        "videos": 3,
        "pdfs": 2,
        "html": 1,
        "widgets": 0,
        "pinned": 0,
    }


def test_a_widget_reply_is_found_by_its_title_and_prose_never_its_body():
    rows = [
        _row(
            0,
            'Here is the chart\n<mcwidget title="Sales chart">\n<div>plum body</div>\n</mcwidget>',
            role="assistant",
        ),
        _row(1, '<mcwidget title="Bound" slug="bound-1"><b>x</b></mcwidget>', role="assistant"),
        _row(2, 'docs:\n```\n<mcwidget title="Fenced">x</mcwidget>\n```', role="assistant"),
        _row(3, 'inline `<mcwidget title="Code">x</mcwidget>` span', role="assistant"),
    ]
    log = _PagedRows(rows)

    body = _search(log, kind="widgets")

    assert [h["index"] for h in body["hits"]] == [1, 0]
    assert (body["hits"][0]["snippet"], body["hits"][0]["widget_titles"]) == ("Bound", ["Bound"])
    assert body["hits"][1]["snippet"] == "Here is the chart"
    assert (body["hits"][1]["widget_count"], body["hits"][1]["widget_titles"]) == (
        1,
        ["Sales chart"],
    )
    assert _search(log, query=shs.message_query("plum"))["hits"] == []
    assert [h["index"] for h in _search(log, query=shs.message_query("sales"))["hits"]] == [0]


def test_a_sent_video_is_a_video_and_a_file():
    log = _PagedRows([_file_row(0, "clip.mp4", "/out/clip.mp4", "video/mp4")])

    body = _search(log, kind="videos")

    assert [h["index"] for h in body["hits"]] == [0]
    assert (body["counts"]["videos"], body["counts"]["files"]) == (1, 1)


def test_a_credential_in_a_file_name_or_widget_title_is_redacted():
    token = SAMPLE_SLACK_BOT_TOKEN
    log = _PagedRows(
        [
            _file_row(0, f"{token}.html", f"/out/{token}.html", "text/html"),
            _row(1, f'<mcwidget title="{token}">x</mcwidget>', role="assistant"),
            _row(2, f"[attached_file 1] /home/u/{token}.pdf"),
        ]
    )

    for kind in ("html", "widgets", "pdfs"):
        assert token not in json.dumps(_search(log, kind=kind)["hits"])


def test_the_query_matches_the_redacted_text_the_results_show():
    token = SAMPLE_SLACK_BOT_TOKEN
    part = token[8:24]
    log = _PagedRows(
        [
            _row(0, f"the plum bot uses {token}"),
            _row(1, f'<mcwidget title="plum {token}">x</mcwidget>', role="assistant"),
            _file_row(2, f"plum {token}.txt", f"/out/plum {token}.txt", "text/plain"),
        ]
    )

    assert _search(log, query=shs.message_query(part))["hits"] == []
    assert [h["index"] for h in _search(log, query=shs.message_query("plum"))["hits"]] == [2, 1, 0]


@pytest.fixture(params=["posix", "nt"])
def destination_platform(request, monkeypatch):
    from pathlib import PurePosixPath, PureWindowsPath

    from kiro_crew.messaging import outbound_files

    class LocalPath(PureWindowsPath if request.param == "nt" else PurePosixPath):
        def expanduser(self):
            return self

    monkeypatch.setattr(outbound_files, "os", SimpleNamespace(name=request.param))
    monkeypatch.setattr(outbound_files, "Path", LocalPath)
    monkeypatch.setattr(
        outbound_files,
        "unc_probe_allowed",
        lambda raw: PureWindowsPath(raw).is_relative_to(PureWindowsPath("//fileserver/share")),
    )
    return request.param


@pytest.mark.parametrize(
    "dest",
    [
        "https://example.com/x.png",
        "http://example.com/x.png",
        "HTTPS://example.com/x.png",
        "https:example.com/x.png",
        "http:example.com/x.png",
        "//example.com/x.png",
        "file:////example.com/share/x.png",
        "data:image/png;base64,AAAA",
        "blob:https://example.com/x.png",
        r"\\example.com\share\x.png",
        "x.png",
    ],
)
def test_a_picture_that_is_not_an_absolute_local_path_is_never_a_thumbnail(
    dest, destination_platform
):
    log = _PagedRows(
        [
            _row(0, f"[attached_file 1] {dest}"),
            _row(1, f"see ![x]({dest})"),
            _row(2, "see", files=[dest]),
        ]
    )

    for hit in (
        _search(log, kind="files")["hits"] + _search(log, query=shs.message_query("see"))["hits"]
    ):
        assert "images" not in hit
        assert "image_count" not in hit
    assert _search(log, kind="images")["hits"] == []


def test_a_local_picture_is_a_thumbnail(destination_platform):
    dest = r"C:\u\cat.png" if destination_platform == "nt" else "/home/u/cat.png"
    log = _PagedRows([_row(0, f"[attached_file 1] {dest}"), _row(1, "look", files=[dest])])

    assert [h["images"] for h in _search(log, kind="images")["hits"]] == [[dest], [dest]]


@pytest.mark.parametrize("dest", ["//fileserver/share/x.png", r"\\fileserver\share\x.png"])
def test_unc_pictures_use_native_paths_and_keep_counts(destination_platform, dest):
    from pathlib import PureWindowsPath

    paths = [dest.replace("x.png", f"{i}.png") for i in range(shs.MAX_THUMBS + 1)]
    inline = " ".join(f"![x]({PureWindowsPath(p).as_posix()})" for p in paths)
    log = _PagedRows(
        [
            _row(0, "\n".join(f"[attached_file {i + 1}] {p}" for i, p in enumerate(paths))),
            _row(1, inline, files=paths),
            _row(2, inline, role="assistant"),
        ]
    )

    body = _search(log, kind="images")

    if destination_platform == "posix":
        assert body["hits"] == []
    else:
        assert [hit["index"] for hit in body["hits"]] == [2, 1, 0]
        for hit in body["hits"]:
            assert hit["image_count"] == len(paths)
            assert hit["images"] == [str(PureWindowsPath(p)) for p in paths[: shs.MAX_THUMBS]]


def test_a_password_in_a_long_url_is_redacted_before_the_url_is_shortened():
    url = "https://admin:Tr0ub4dorPw@db.internal.example.com/app/settings/path"
    log = _PagedRows([_row(0, f"connect with {url}")])

    for body in (_search(log, kind="links"), _search(log, query=shs.message_query("connect"))):
        assert "Tr0ub4dorPw" not in body["hits"][0]["snippet"]


@pytest.mark.parametrize(
    "build_pump",
    [
        pytest.param(lambda n: "a" + " " * n + "b", id="spaces"),
        pytest.param(lambda n: "a" + "\n" * n + "b", id="newlines"),
        pytest.param(lambda n: "-" * n + "x", id="dashes"),
        pytest.param(lambda n: "|" + " " * n + "x", id="pipe-then-spaces"),
        pytest.param(lambda n: "[" * n, id="open-brackets"),
        pytest.param(lambda n: "[a](https://" * n, id="unclosed-links"),
    ],
)
def test_the_excerpt_cleanup_reads_a_long_run_in_linear_time(build_pump):
    # A message is any size, and the cleanup is one regex call that holds the
    # GIL, so a run it re-reads from each of its characters stalls the gateway.
    # The work is inside a C call, so the shared ramped thread-CPU guard measures it.
    from conftest import assert_rejected_without_backtracking

    def reject(text: str) -> None:
        assert "|" not in shs.snippet_text(text)

    assert_rejected_without_backtracking(reject, build_pump)


def test_a_query_matches_text_inside_a_url_the_excerpt_shortens():
    url = "https://github.com/kirodotdev/KiroCrew/pull/8811"
    log = _PagedRows([_row(0, f"see [the fix]({url})", role="assistant")])

    body = _search(log, query=shs.message_query("8811"))

    assert [h["index"] for h in body["hits"]] == [0]


def test_rows_the_chat_does_not_draw_as_messages_are_never_hits():
    rows = [
        _row(0, "plum \u200b"),
        _row(1, "\u200b", role="assistant"),
        _row(
            2,
            "[Subagent completion event]\n\nplum",
            role="assistant",
            subagentCompletion={"kind": "single", "agentId": "abc", "outcome": "ok"},
        ),
        _row(3, "plum for real"),
    ]
    log = _PagedRows(rows)

    assert [h["index"] for h in _search(log, query=shs.message_query("plum"))["hits"]] == [3, 0]
    assert [h["index"] for h in _search(log, kind="links")["hits"]] == []


@pytest.mark.parametrize("page_rows", [1, 200])
@pytest.mark.parametrize(
    "successor, before, expected",
    [
        (_row(2, "plum"), None, [2]),
        (_row(2, "plum", mid=None), None, [2]),
        # A resumed reply below the successor still hides the steer it replaced.
        (_row(2, "plum"), 2, []),
        (_row(2, "plum", mid="other"), None, [2, 0]),
        (_row(2, "plum", role="assistant"), None, [2, 0]),
        (_row(2, "plum", role="nudge"), None, [0]),
        (_row(2, "changed plum"), None, [2, 0]),
        (_row(2, "plum", steer=True), None, [2, 0]),
    ],
)
def test_search_hides_a_superseded_steer_only_with_its_drawn_successor(
    monkeypatch, page_rows, successor, before, expected
):
    monkeypatch.setattr(shs, "SCAN_PAGE_ROWS", page_rows)
    rows = [
        _row(0, "plum", steer=True, steerState="superseded", supersededBy="m2"),
        _row(1, "unrelated"),
        successor,
    ]
    log = _PagedRows(rows)

    body = _search(log, query=shs.message_query("plum"), before=before)

    assert [h["index"] for h in body["hits"]] == expected
    # A resumed reply also reads the rows above its resume point once.
    assert log.decoded == len(rows)


def test_a_bot_chat_preview_returns_only_what_its_chat_draws():
    rows = [
        _row(0, "hello"),
        {"role": "tool", "content": "ran ls", "ts": "2026-11-01T10:00:01+00:00"},
        _row(2, "\u200b", role="assistant"),
        _row(3, "hi there", role="assistant"),
        _file_row(4, "clip.mp4", "/out/clip.mp4", "video/mp4"),
    ]
    log = _PagedRows(rows)

    bot = shs.read_around(log, "k", start=0, end=5, speech_only=True)
    session = shs.read_around(log, "k", start=0, end=5)

    assert [r["role"] for r in bot["rows"]] == ["user", "assistant", "file"]
    assert [r["content"] for r in bot["rows"][:2]] == ["hello", "hi there"]
    assert len(session["rows"]) == 5
    assert (bot["start"], bot["end"]) == (0, 5)


def test_around_says_when_nothing_earlier_can_be_read():
    rows = [_row(i, f"row {i}", day=1 + (i >= 3)) for i in range(6)]
    log = _PagedRows(rows)

    assert shs.read_around(log, "k", start=0, end=2)["has_earlier"] is False
    assert shs.read_around(log, "k", start=3, end=5)["has_earlier"] is True
    floored = shs.read_around(log, "k", start=0, end=5, floor=rows[2]["ts"])
    assert floored["start"] == 3 and floored["has_earlier"] is False


def test_tool_and_system_rows_never_match():
    log = _PagedRows([_row(0, "plum", role="tool"), _row(1, "plum", role="system")])

    assert _search(log, query=shs.message_query("plum"))["hits"] == []


def test_around_reads_one_bounded_range():
    log = _PagedRows([_row(i, f"row {i}") for i in range(30)])

    read = shs.read_around(log, "k", start=10, end=15)

    assert [r["content"] for r in read["rows"]] == [f"row {i}" for i in range(10, 15)]
    assert (read["start"], read["end"], read["total"]) == (10, 15, 30)
    assert log.decoded == 5


def _app(
    log: ConversationLog, slots: dict, request_app: str = "", pins: list | None = None
) -> web.Application:
    @web.middleware
    async def _caller(request, handler):
        if request_app:
            request["app"] = request_app
        return await handler(request)

    app = web.Application(middlewares=[_caller])
    app["state"] = SimpleNamespace(conversation_log=log, _slots=slots, _chat_pins=pins or [])
    app.router.add_get(SEARCH, shs.api_chat_slot_history_search)
    app.router.add_get(AROUND, shs.api_chat_slot_history_around)
    app.router.add_get(COUNTS, shs.api_chat_slot_history_counts)
    return app


def _slot(key: str) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        _app="",
        linked_session_key=key,
        workspace=None,
        _history_key=key,
    )


async def _get(app: web.Application, path: str) -> tuple[int, dict]:
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        return resp.status, await resp.json()


@pytest.fixture
def real_log(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    log.append("dashboard:chat-1", "user", "where is the plum report")
    log.append("dashboard:chat-1", "assistant", "the plum report is in docs")
    log.append("dashboard:chat-1", "user", "thanks")
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    return log


@pytest.mark.asyncio
async def test_the_route_answers_hits_and_the_preview_rows(real_log):
    app = _app(real_log, {"dashboard:chat-1": _slot("dashboard:chat-1")})

    status, body = await _get(app, "/api/chat/slots/dashboard:chat-1/history/search?q=plum")
    status2, around = await _get(
        app, "/api/chat/slots/dashboard:chat-1/history/around?start=0&end=3"
    )

    assert status == 200
    assert [(h["index"], h["role"]) for h in body["hits"]] == [(1, "assistant"), (0, "user")]
    assert status2 == 200
    assert [m["role"] for m in around["messages"]] == ["user", "assistant", "user"]
    assert around["total"] == 3


@pytest.mark.asyncio
async def test_no_query_and_no_filter_lists_nothing(real_log):
    app = _app(real_log, {"dashboard:chat-1": _slot("dashboard:chat-1")})

    status, body = await _get(app, "/api/chat/slots/dashboard:chat-1/history/search")

    assert status == 200
    assert body["hits"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["-plum", "title:plum"])
async def test_a_query_without_searchable_terms_does_not_become_filter_only(monkeypatch, q):
    rows = [_row(0, "plum", files=["/report.pdf"]), _row(1, "pear", files=["/report.pdf"])]
    log = _PagedRows(rows)
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    app = _app(log, {"chat-1": _slot("chat-1")})
    query = shs.message_query(q)
    assert shs.message_hits(rows, query, frozenset({"user", "assistant"})) == ([], 0)

    status, body = await _get(app, f"/api/chat/slots/chat-1/history/search?kind=files&q={q}")
    filter_status, filtered = await _get(app, "/api/chat/slots/chat-1/history/search?kind=files")

    assert status == filter_status == 200
    assert body["hits"] == []
    assert [hit["index"] for hit in filtered["hits"]] == [1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("member", [False, True])
async def test_the_route_finds_a_sent_video_in_either_chat(monkeypatch, member):
    # A bot chat draws file_send cards too (crewmateBubbles.isCrewmateChatRow),
    # so its search finds them, and its preview shows the card a hit opens.
    log = _PagedRows([_file_row(0, "clip.mp4", "/out/clip.mp4", "video/mp4")])
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    monkeypatch.setattr(shs, "is_member_thread", lambda slot: member)
    app = _app(log, {"chat-1": _slot("chat-1")})

    status, body = await _get(app, "/api/chat/slots/chat-1/history/search?kind=videos")
    _, around = await _get(app, "/api/chat/slots/chat-1/history/around?start=0&end=1")

    assert status == 200
    assert [hit["index"] for hit in body["hits"]] == [0]
    assert body["counts"]["videos"] == 1
    assert [m["role"] for m in around["messages"]] == ["file"]


@pytest.mark.parametrize(
    "query, code",
    [
        ("kind=audio", "invalid_kind"),
        ("kind=Videos", "invalid_kind"),
        ("since=yesterday", "invalid_time"),
        ("q=plum&before=-1", "invalid_query_params"),
    ],
)
@pytest.mark.asyncio
async def test_an_unreadable_parameter_is_refused(real_log, query, code):
    app = _app(real_log, {"dashboard:chat-1": _slot("dashboard:chat-1")})

    status, body = await _get(app, f"/api/chat/slots/dashboard:chat-1/history/search?{query}")

    assert status == 400
    assert body["code"] == code


@pytest.mark.asyncio
async def test_an_unknown_slot_is_not_found(real_log):
    app = _app(real_log, {})

    status, _ = await _get(app, "/api/chat/slots/nope/history/search?q=plum")
    status2, _ = await _get(app, "/api/chat/slots/nope/history/around?start=0&end=1")

    assert (status, status2) == (404, 404)


@pytest.mark.asyncio
async def test_an_app_that_does_not_own_the_slot_is_refused(real_log):
    app = _app(real_log, {"dashboard:chat-1": _slot("dashboard:chat-1")}, request_app="other")

    status, _ = await _get(app, "/api/chat/slots/dashboard:chat-1/history/search?q=plum")

    assert status == 404


@pytest.mark.asyncio
async def test_preview_relocates_a_saved_hit_after_rotation(monkeypatch):
    log = _PagedRows([_row(i, f"plum {i}") for i in range(400)])
    hit = _search(log, query=shs.message_query("plum 395"))["hits"][0]
    assert hit["index"] == 395
    log.rows = log.rows[200:]
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    app = _app(log, {"chat-1": _slot("chat-1")})

    status, body = await _get(
        app, "/api/chat/slots/chat-1/history/around?start=387&end=404&mid=m395"
    )

    assert status == 200
    assert (body["start"], body["end"], body["total"]) == (187, 200, 200)
    assert any(m.get("meta", {}).get("mid") == "m395" for m in body["messages"])


@pytest.mark.asyncio
async def test_preview_refuses_a_removed_hit_instead_of_showing_another_row(monkeypatch):
    log = _PagedRows([_row(i, f"row {i}") for i in range(200, 400)])
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    app = _app(log, {"chat-1": _slot("chat-1")})

    status, body = await _get(app, "/api/chat/slots/chat-1/history/around?start=87&end=104&mid=m95")

    assert status == 404
    assert body["code"] == "message_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("mid", ["", "x" * 257, "bad%00id"])
async def test_preview_validates_the_message_id(real_log, mid):
    app = _app(real_log, {"dashboard:chat-1": _slot("dashboard:chat-1")})
    status, body = await _get(
        app, f"/api/chat/slots/dashboard:chat-1/history/around?start=0&end=3&mid={mid}"
    )
    assert status == 400
    assert body["code"] == "invalid_mid"


def test_preview_id_lookup_is_bounded_and_checks_the_old_range_first(monkeypatch):
    monkeypatch.setattr(shs, "SCAN_BUDGET_ROWS", 50)
    monkeypatch.setattr(shs, "SCAN_PAGE_ROWS", 20)
    log = _PagedRows([_row(i, f"row {i}") for i in range(500)])
    read = shs.read_around(log, "k", start=7, end=24, mid="m15")
    assert read is not None and read["start"] == 7
    assert log.decoded == 34

    log.decoded = 0
    assert shs.read_around(log, "k", start=200, end=217, mid="gone") is None
    assert log.decoded == 17 + 50


def test_preview_does_not_return_a_hit_behind_the_display_floor():
    rows = [_row(i, f"row {i}") for i in range(6)]
    assert (
        shs.read_around(_PagedRows(rows), "k", start=0, end=6, mid="m2", floor=rows[2]["ts"])
        is None
    )


@pytest.mark.asyncio
async def test_preview_does_not_mix_revisions_during_id_lookup(monkeypatch):
    from kiro_crew.history_projection_contract import TranscriptRevisionChanged

    log = _PagedRows([_row(i, f"row {i}") for i in range(10)])
    read = log.read_messages_chained_page

    def changed(key, *, limit, before=None, expected_revision=None):
        if expected_revision is not None:
            raise TranscriptRevisionChanged("rotated")
        return read(key, limit=limit, before=before)

    monkeypatch.setattr(log, "read_messages_chained_page", changed)
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    status, body = await _get(
        _app(log, {"chat-1": _slot("chat-1")}),
        "/api/chat/slots/chat-1/history/around?start=0&end=10&mid=m5",
    )
    assert status == 503
    assert body["code"] == "history_unreadable"


def _moving_log(monkeypatch, moves: int) -> tuple[_PagedRows, list[int]]:
    """A log whose first *moves* attempts each see a write land mid-read; the
    returned list counts the attempts (one ``limit=0`` probe each)."""
    from kiro_crew.history_projection_contract import TranscriptRevisionChanged

    log = _PagedRows([_row(i, f"plum {i}") for i in range(10)])
    read = log.read_messages_chained_page
    probes: list[int] = []

    def moving(key, *, limit, before=None, expected_revision=None):
        if limit == 0:
            probes.append(1)
            return read(key, limit=limit, before=before)
        if len(probes) <= moves:
            raise TranscriptRevisionChanged("appended")
        return read(key, limit=limit, before=before)

    monkeypatch.setattr(log, "read_messages_chained_page", moving)
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    return log, probes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["history/around?start=0&end=10&mid=m5", "history/search?q=plum"],
)
async def test_a_write_during_one_read_is_retried_from_a_fresh_probe(monkeypatch, path):
    log, probes = _moving_log(monkeypatch, moves=1)

    status, _ = await _get(_app(log, {"chat-1": _slot("chat-1")}), f"/api/chat/slots/chat-1/{path}")

    assert status == 200
    assert len(probes) == 2


@pytest.mark.asyncio
async def test_a_preview_that_moves_on_every_attempt_gives_up_after_the_bound(monkeypatch):
    log, probes = _moving_log(monkeypatch, moves=10**6)

    status, body = await _get(
        _app(log, {"chat-1": _slot("chat-1")}),
        "/api/chat/slots/chat-1/history/around?start=0&end=10",
    )

    assert status == 503
    assert body["code"] == "history_unreadable"
    assert len(probes) == shs.SNAPSHOT_ATTEMPTS


def test_a_picture_whose_path_holds_a_credential_is_counted_but_never_sent(tmp_path):
    secret, chart = str(tmp_path / f"{SAMPLE_SLACK_BOT_TOKEN}.png"), str(tmp_path / "chart.png")
    rows = [_row(0, "two pictures", files=[secret, chart])]

    hit = _search(_PagedRows(rows), kind="images")["hits"][0]

    assert hit["image_count"] == 2
    assert hit["images"] == [chart]
    assert SAMPLE_SLACK_BOT_TOKEN not in json.dumps(hit)


class _Kinds:
    """A store stand-in for ``artifact_kinds``: fixed kinds, each lookup recorded."""

    def __init__(self, kinds: dict[str, str]) -> None:
        self.kinds = kinds
        self.asked: list[str] = []

    def kind_of(self, slug: str) -> str | None:
        self.asked.append(slug)
        return self.kinds.get(slug)


def test_a_saved_html_artifact_link_counts_as_html_and_other_kinds_do_not():
    store = _Kinds({"report": "html", "notes": "markdown", "chart": "widget"})
    rows = [
        _row(0, "saved [Q3 report](/artifacts/report)", role="assistant"),
        _row(1, "saved [notes](/artifacts/notes)", role="assistant"),
        _row(2, "saved [chart](/artifacts/chart)", role="assistant"),
        _row(3, "saved [gone](/artifacts/gone)", role="assistant"),
    ]

    body = _search(
        _PagedRows(rows), kind="html", artifact_kind=shs.artifact_kinds(lambda: store, "")
    )

    assert [h["index"] for h in body["hits"]] == [0]
    assert (body["hits"][0]["html_count"], body["hits"][0]["html_names"]) == (1, ["Q3 report"])
    assert body["counts"]["html"] == 1


def test_an_artifact_label_with_a_slash_is_named_whole_beside_an_attached_page(tmp_path):
    store = _Kinds({"q-report": "html"})
    page = str(tmp_path / "pages" / "map.html")
    rows = [_row(0, "saved [Q1/Q2 report](/artifacts/q-report)", role="assistant", files=[page])]

    hit = _search(
        _PagedRows(rows), kind="html", artifact_kind=shs.artifact_kinds(lambda: store, "")
    )["hits"][0]

    assert (hit["html_count"], hit["html_names"]) == (2, ["map.html", "Q1/Q2 report"])


def test_a_repeated_artifact_link_is_looked_up_once_per_request():
    store = _Kinds({"report": "html"})
    rows = [_row(i, f"see [r](/artifacts/report) {i}", role="assistant") for i in range(5)]

    body = _search(
        _PagedRows(rows), kind="html", artifact_kind=shs.artifact_kinds(lambda: store, "")
    )

    assert len(body["hits"]) == 5
    assert store.asked == ["report"]


def test_one_row_looks_up_at_most_max_artifact_links_artifact_links():
    store = _Kinds({})
    text = " ".join(f"[a{i}](/artifacts/a{i})" for i in range(shs.MAX_ARTIFACT_LINKS + 3))

    _search(
        _PagedRows([_row(0, text)]),
        kind="html",
        artifact_kind=shs.artifact_kinds(lambda: store, ""),
    )

    assert store.asked == [f"a{i}" for i in range(shs.MAX_ARTIFACT_LINKS)]


@pytest.mark.parametrize(
    "build_pump",
    [
        pytest.param(lambda n: "[" * n + "](/artifacts/x", id="open-brackets"),
        pytest.param(lambda n: "](/artifacts/" * n, id="unclosed-links"),
    ],
)
def test_the_artifact_link_scan_reads_a_long_run_in_linear_time(build_pump):
    from conftest import assert_rejected_without_backtracking

    def reject(text: str) -> None:
        assert shs.artifact_links(text) == []

    assert_rejected_without_backtracking(reject, build_pump)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller, allowed, expected", [("", False, 1), ("acme", False, 0), ("acme", True, 1)]
)
async def test_an_app_caller_counts_only_the_artifacts_its_token_may_read(
    monkeypatch, caller, allowed, expected
):
    from kiro_crew.dashboard import token_auth_app_scope

    store = _Kinds({"report": "html"})
    paths: list[tuple[str, str]] = []

    def may_read(app: str, path: str) -> bool:
        paths.append((app, path))
        return allowed

    monkeypatch.setattr(token_auth_app_scope, "app_token_path_allowed", may_read)
    monkeypatch.setattr(shs, "_default_artifact_store", lambda: store)
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    slot = _slot("chat-1")
    slot._app = "acme"
    log = _PagedRows([_row(0, "saved [Report](/artifacts/report)", role="assistant")])
    app = _app(log, {"chat-1": slot}, request_app=caller)

    status, body = await _get(app, "/api/chat/slots/chat-1/history/search?kind=html")

    assert status == 200
    assert len(body["hits"]) == body["counts"]["html"] == expected
    assert "Report" not in json.dumps(body) or expected
    assert paths == ([("acme", "/api/artifacts/report")] if caller else [])


def _pin(slot: str, mid: str = "", ts: str = "", app: str = "") -> dict:
    return {"slot_key": slot, "mid": mid, "message_ts": ts, "origin_app": app}


def test_the_pinned_kind_lists_only_pinned_rows_and_combines_with_the_query():
    rows = [_row(0, "plum one"), _row(1, "plum two"), _row(2, "pear"), _row(3, "plum three")]
    # One pin by id, one saved before rows had ids, by its time alone.
    pinned = shs.Pinned([_pin("k", mid="m1"), _pin("k", ts=rows[3]["ts"]), _pin("k", mid="m2")])

    body = _search(_PagedRows(rows), kind="pinned", pinned=pinned)
    plum = _search(_PagedRows(rows), kind="pinned", query=shs.message_query("plum"), pinned=pinned)

    assert [h["index"] for h in body["hits"]] == [3, 2, 1]
    assert [h["index"] for h in plum["hits"]] == [3, 1]
    assert (plum["counts"]["pinned"], plum["counts"][""]) == (2, 3)


def test_with_no_pins_the_pinned_kind_lists_nothing():
    rows = [_row(0, "plum one")]

    assert _search(_PagedRows(rows), kind="pinned")["hits"] == []
    assert _search(_PagedRows(rows), kind="pinned", pinned=shs.Pinned([]))["hits"] == []


@pytest.mark.asyncio
async def test_the_route_reads_this_chats_pins_with_the_pins_lists_app_rule(tmp_path, monkeypatch):
    real_log = ConversationLog(base_dir=tmp_path)
    for i, (role, text) in enumerate([("user", "a"), ("assistant", "b"), ("user", "c")]):
        real_log.append("dashboard:chat-1", role, text, mid=f"m{i}")
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: slot.key)
    slot = _slot("dashboard:chat-1")
    # An app's own chat, which both the app and the dashboard owner may read.
    slot._app = "notes"
    slots = {"dashboard:chat-1": slot}
    pins = [
        _pin("dashboard:chat-1", mid="m0"),
        _pin("dashboard:chat-1", mid="m1", app="notes"),
        # Another chat's pin on the same id names nothing here.
        _pin("dashboard:chat-9", mid="m2"),
    ]
    path = "/api/chat/slots/dashboard:chat-1/history/search?kind=pinned"

    _, owner = await _get(_app(real_log, slots, pins=pins), path)
    _, notes = await _get(_app(real_log, slots, "notes", pins=pins), path)

    assert [h["index"] for h in owner["hits"]] == [1, 0]
    assert [h["index"] for h in notes["hits"]] == [1]


def test_every_listed_name_and_title_is_bounded_and_a_long_thumbnail_is_left_out(tmp_path):
    long = "x" * 5000
    page = str(tmp_path / f"{long}.html")
    deep = str(tmp_path / ("d" * 1100) / "cat.png")
    widget = f'<mcwidget title="{long}"><p>body</p></mcwidget>'
    rows = [
        _row(0, f"saved [{long}](/artifacts/report)", role="assistant", files=[page]),
        _row(1, widget, role="assistant"),
        _row(2, "look", files=[deep, str(tmp_path / "ok.png")]),
    ]
    kinds = shs.artifact_kinds(lambda: _Kinds({"report": "html"}), "")

    html = _search(_PagedRows(rows), kind="html", artifact_kind=kinds)["hits"][0]
    widgets = _search(_PagedRows(rows), kind="widgets")["hits"][0]
    images = _search(_PagedRows(rows), kind="images")["hits"][0]

    for name in html["html_names"] + widgets["widget_titles"]:
        assert len(name) == shs.MAX_LABEL_CHARS
    assert images["image_count"] == 2
    assert images["images"] == [str(tmp_path / "ok.png")]


def test_a_resumed_search_still_hides_a_steer_whose_successor_an_earlier_reply_read():
    rows = [
        _row(0, "plum", steer=True, steerState="superseded", supersededBy="m1"),
        _row(1, "plum"),
        _row(2, "plum later"),
    ]
    query = shs.message_query("plum")

    first = _search(_PagedRows(rows), query=query, limit=2)
    rest = _search(_PagedRows(rows), query=query, limit=30, before=first["next_before"])

    assert [h["index"] for h in first["hits"]] == [2, 1]
    assert first["next_before"] == 1
    assert rest["hits"] == []


def test_an_html_artifact_after_four_other_links_still_counts_as_html():
    store = _Kinds(
        {"a": "markdown", "b": "markdown", "c": "markdown", "d": "markdown", "map": "html"}
    )
    links = " ".join(f"[{s}](/artifacts/{s})" for s in ["a", "b", "c", "d", "map"])
    rows = [_row(0, f"saved {links}", role="assistant")]

    body = _search(
        _PagedRows(rows), kind="html", artifact_kind=shs.artifact_kinds(lambda: store, "")
    )

    assert [h["index"] for h in body["hits"]] == [0]
    assert body["hits"][0]["html_names"] == ["map"]
    assert store.asked == ["a", "b", "c", "d", "map"]


def test_a_preview_page_that_starts_at_the_display_floor_offers_nothing_earlier():
    rows = [_row(i, f"row {i}") for i in range(6)]

    read = shs.read_around(_PagedRows(rows), "k", start=3, end=6, floor=rows[2]["ts"])

    assert [r["content"] for r in read["rows"]] == ["row 3", "row 4", "row 5"]
    assert read["start"] == 3
    assert read["has_earlier"] is False


def test_a_user_hit_keeps_its_sender_stamps_and_a_reply_keeps_none():
    author = {"id": "app:notes", "kind": "system", "display": {"name": "app:notes"}}
    rows = [
        _row(0, "plum from an app", author=author, turnActor="app", appLabel="notes", other="x"),
        _row(1, "plum answer", role="assistant", author=author),
    ]

    hits = _search(_PagedRows(rows), query=shs.message_query("plum"))["hits"]

    by_index = {h["index"]: h for h in hits}
    assert by_index[0]["meta"] == {"author": author, "turnActor": "app", "appLabel": "notes"}
    assert "meta" not in by_index[1]


def _mixed_rows(n: int, pic: Path) -> list[dict]:
    """*n* rows: every third has a link, every fifth attaches *pic*, an absolute
    path on this platform (a POSIX literal is not one on Windows)."""
    rows = []
    for i in range(n):
        text = f"row {i} see https://example.com/{i}" if i % 3 == 0 else f"row {i}"
        files = [str(pic)] if i % 5 == 0 else []
        rows.append(_row(i, text, **({"files": files} if files else {})))
    return rows


@pytest.mark.asyncio
async def test_counts_cover_the_whole_chat_whatever_tab_a_search_pages(monkeypatch, tmp_path):
    # Each search reply stops at its own tab's hit limit, so its counts differ
    # by tab; the counts route walks every row and answers one set.
    monkeypatch.setattr(shs, "SCAN_PAGE_ROWS", 7)
    log = _PagedRows(_mixed_rows(300, tmp_path / "pic.png"))
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: "k")
    app = _app(log, {"chat-1": _slot("k")})

    _, links = await _get(app, "/api/chat/slots/chat-1/history/search?kind=links")
    _, images = await _get(app, "/api/chat/slots/chat-1/history/search?kind=images")
    status, body = await _get(app, "/api/chat/slots/chat-1/history/counts")

    assert links["counts"][""] != images["counts"][""]
    assert status == 200
    assert body["complete"] is True
    assert body["total"] == 300
    assert (body["counts"][""], body["counts"]["links"], body["counts"]["images"]) == (300, 100, 60)
    assert body["counts"]["videos"] == 0


@pytest.mark.asyncio
async def test_counts_follow_the_query_and_say_when_the_budget_cut_them(monkeypatch, tmp_path):
    log = _PagedRows(_mixed_rows(30, tmp_path / "pic.png"))
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: "k")
    app = _app(log, {"chat-1": _slot("k")})

    _, body = await _get(app, "/api/chat/slots/chat-1/history/counts?q=row%202")
    monkeypatch.setattr(shs, "COUNT_BUDGET_ROWS", 10)
    monkeypatch.setattr(shs, "SCAN_PAGE_ROWS", 5)
    _, cut = await _get(app, "/api/chat/slots/chat-1/history/counts")

    assert body["counts"][""] == len(_search(log, query=shs.message_query("row 2"))["hits"])
    assert body["counts"][""] < 30
    assert cut["complete"] is False
    assert cut["counts"][""] == 10


def test_a_count_walk_lists_no_hit(tmp_path):
    log = _PagedRows(_mixed_rows(50, tmp_path / "pic.png"))

    body = _search(log, limit=0, budget=shs.COUNT_BUDGET_ROWS, pin_revision=False)

    assert body["hits"] == []
    assert body["counts"][""] == 50
    assert body["next_before"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["search", "counts"])
@pytest.mark.parametrize(
    ("owner", "caller", "expected"),
    [("", "other", 404), ("notes", "other", 404), ("notes", "notes", 200), ("notes", "", 200)],
)
async def test_counts_admit_exactly_the_callers_search_admits(
    monkeypatch, tmp_path, route, owner, caller, expected
):
    # The counts route reads the same history as search, so it takes the same
    # slot-ownership check: an app that does not own the chat gets the uniform
    # 404 and no row is read.
    log = _PagedRows(_mixed_rows(20, tmp_path / "pic.png"))
    monkeypatch.setattr(shs, "slot_history_key", lambda slot: "k")
    slot = _slot("k")
    slot._app = owner
    app = _app(log, {"chat-1": slot}, request_app=caller)

    status, body = await _get(app, f"/api/chat/slots/chat-1/history/{route}?q=row")

    assert status == expected
    if expected == 404:
        assert log.decoded == 0
        assert "counts" not in body
    else:
        assert body["counts"][""] > 0


@pytest.mark.asyncio
async def test_counts_refuse_an_unknown_chat(real_log):
    app = _app(real_log, {"dashboard:chat-1": _slot("dashboard:chat-1")})

    status, _ = await _get(app, "/api/chat/slots/dashboard:chat-9/history/counts")

    assert status == 404


@pytest.mark.asyncio
async def test_counts_refuse_a_bad_time(real_log):
    app = _app(real_log, {"dashboard:chat-1": _slot("dashboard:chat-1")})

    status, body = await _get(app, "/api/chat/slots/dashboard:chat-1/history/counts?since=x")

    assert status == 400
    assert body["code"] == "invalid_time"
