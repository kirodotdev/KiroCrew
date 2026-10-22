"""A crewmate's weekly report or monthly review as Markdown or as a whole HTML page.

* an html report is an ``html`` artifact in the same folder, with the same tags,
  notification and goal-record entry as a Markdown one;
* both kinds are marked ``goal_report`` on the artifact, server-side, so the viewer
  opens the page in the crewmate Dashboard page's sandbox; nothing else sets it;
* one format per report: both or neither is refused, and an html page over the
  Dashboard page's own ceiling is refused;
* the text the crewmate writes is scrubbed in either format;
* the weekly and monthly prompts share one "How the report looks" piece, written as
  principles, and the tool description points at it.
"""

from __future__ import annotations

import base64
import html.parser
import random
import re
from typing import Any

from guide_route_helpers import FakeState
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    REPORT,
    TOKEN,
    _call,
    _owner_ok,
    _quiet_sel,
    _seed,
    _slug,
    _thread,
)

from kiro_crew import dashboard_frame, mcp_mate, member_goals
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.dashboard import goal_reminder

PAGE = (
    "<style>@page { size: A4; margin: 0 } .sheet { width: 210mm; height: 297mm }</style>"
    '<section class="sheet"><header><h1>Week 41</h1></header>'
    "<p>PRs described: 14 -> 19 of 27.</p>"
    '<svg viewBox="0 0 100 40"><polyline points="0,30 50,20 100,10"/></svg></section>'
)


def _setup() -> tuple[FakeState, Any, list[Any]]:
    _seed()
    state = FakeState()
    pushed: list[Any] = []

    class Bus:
        def push(self, payload):
            pushed.append(payload)

    state.notification_bus = Bus()  # type: ignore[attr-defined]
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    member_goals.set_goal(_slug(ASSISTANT_MEMBER_NAME), ASSISTANT_MEMBER_NAME, "Ship", by="user")
    return state, slot, pushed


def test_an_html_report_is_an_html_artifact_filed_like_a_markdown_one(_floor_monkeypatch) -> None:
    from kiro_crew import artifacts as artifacts_mod

    state, slot, pushed = _setup()
    body_in = {"kind": "weekly", "title": "Week 41", "html": PAGE}
    status, body = _call(state, REPORT, body_in, slot.key)
    assert status == 200, body
    assert body["format"] == "html"
    art = artifacts_mod.get_default_store().get(body["slug"])
    assert art.kind == "html"
    assert art.content == PAGE
    assert art.goal_report is True
    assert art.tags == ["goal-report", "weekly"]
    folders = artifacts_mod.get_default_folder_store()
    label = ASSISTANT_MEMBER_NAME.replace("/", "-")
    assert art.folder_id == folders.resolve_path(f"Crewmates/{label}/Reports")
    (note,) = pushed
    assert note.url == f"/artifacts/{body['slug']}"
    assert PAGE not in (note.body or "")
    (report,) = member_goals.read_goals(_slug(ASSISTANT_MEMBER_NAME), ASSISTANT_MEMBER_NAME)[
        "reports"
    ]
    assert (report["kind"], report["slug"]) == ("weekly", body["slug"])


def test_a_markdown_report_still_saves_and_is_marked_a_report(_floor_monkeypatch) -> None:
    from kiro_crew import artifacts as artifacts_mod

    state, slot, _pushed = _setup()
    md = "# Month\n\nOn track."
    status, body = _call(
        state, REPORT, {"kind": "monthly", "title": "October", "markdown": md}, slot.key
    )
    assert status == 200, body
    art = artifacts_mod.get_default_store().get(body["slug"])
    assert (art.kind, art.content, art.goal_report) == ("markdown", md, True)
    assert body["format"] == "markdown"


def test_a_report_is_one_format_both_or_neither_is_refused(_floor_monkeypatch) -> None:
    state, slot, _pushed = _setup()
    both = {"kind": "weekly", "title": "W", "markdown": "# W", "html": PAGE}
    status, body = _call(state, REPORT, both, slot.key)
    assert (status, body["code"]) == (400, "report_format_ambiguous")
    for neither in (
        {"kind": "weekly", "title": "W"},
        {"kind": "weekly", "title": "W", "html": " "},
    ):
        status, body = _call(state, REPORT, neither, slot.key)
        assert (status, body["code"]) == (400, "empty_report")
    status, body = _call(state, REPORT, {"kind": "weekly", "title": "W", "html": 7}, slot.key)
    assert (status, body["code"]) == (400, "invalid_report")
    assert (
        member_goals.read_goals(_slug(ASSISTANT_MEMBER_NAME), ASSISTANT_MEMBER_NAME)["reports"]
        == []
    )


def test_an_html_report_over_the_dashboard_page_ceiling_is_refused(_floor_monkeypatch) -> None:
    assert member_goals.REPORT_HTML_MAX_BYTES == dashboard_frame.MAX_PAGE_BYTES
    # The test app takes aiohttp's default 1 MiB body, so the ceiling is lowered
    # for the round trip; the comparison above pins the real value.
    _floor_monkeypatch.setattr(member_goals, "REPORT_HTML_MAX_BYTES", 4096)
    state, slot, _pushed = _setup()
    # Measured in bytes: a two-byte character page just over half the cap in
    # characters is over the cap in bytes.
    big = "\u00e9" * (member_goals.REPORT_HTML_MAX_BYTES // 2 + 1)
    status, body = _call(state, REPORT, {"kind": "weekly", "title": "W", "html": big}, slot.key)
    assert (status, body["code"]) == (400, "report_too_large")
    at_cap = "a" * member_goals.REPORT_HTML_MAX_BYTES
    status, body = _call(state, REPORT, {"kind": "weekly", "title": "W", "html": at_cap}, slot.key)
    assert status == 200, body


def test_the_html_the_crewmate_writes_is_scrubbed(_floor_monkeypatch) -> None:
    from kiro_crew import artifacts as artifacts_mod

    state, slot, _pushed = _setup()
    page = f"<p>token {TOKEN}</p>"
    status, body = _call(state, REPORT, {"kind": "weekly", "title": "W", "html": page}, slot.key)
    assert status == 200, body
    assert TOKEN not in (artifacts_mod.get_default_store().get(body["slug"]).content or "")


def test_a_report_page_takes_the_plain_scrub_exactly(_floor_monkeypatch) -> None:
    """A page gets the same scrub as any other goal string; no payload is exempt.

    A report page embeds no raster image by contract, so a base64 run it carries
    anyway (here a seeded blob in a data URI) is redacted as the plain scrub would.
    """
    from kiro_crew import artifacts as artifacts_mod

    blob = base64.b64encode(random.Random(7).randbytes(300)).decode()
    page = PAGE + f'<img src="data:image/png;base64,{blob}"><p>{TOKEN}</p>'
    plain = member_goals.scrub_text(page)
    assert blob not in plain and TOKEN not in plain
    state, slot, _pushed = _setup()
    status, body = _call(state, REPORT, {"kind": "weekly", "title": "W", "html": page}, slot.key)
    assert status == 200, body
    assert artifacts_mod.get_default_store().get(body["slug"]).content == plain


def test_only_save_report_marks_an_artifact_a_report_and_the_mark_lasts(
    _floor_monkeypatch,
) -> None:
    from kiro_crew import artifacts as artifacts_mod
    from kiro_crew.artifact_store.records import encode_meta

    store = artifacts_mod.get_default_store()
    plain = store.create(name="Plain page", content=PAGE, kind="html")
    assert plain.goal_report is False
    state, slot, _pushed = _setup()
    status, body = _call(state, REPORT, {"kind": "weekly", "title": "W", "html": PAGE}, slot.key)
    assert status == 200, body
    store.update(body["slug"], content=PAGE + "<p>edited</p>", tags=["other"], snapshot=True)
    again = artifacts_mod.ArtifactStore(store._root).get(body["slug"])
    assert again.goal_report is True
    assert '"goal_report": true' in encode_meta(again)
    assert store.get(plain.slug).goal_report is False


def test_a_published_html_report_is_one_frame_that_runs_no_script_and_follows_no_refresh(
    _floor_monkeypatch,
) -> None:
    """The copy a destination serves is as static as the one the viewer opens."""
    from kiro_crew import artifacts as artifacts_mod
    from kiro_crew import publish_sync

    store = artifacts_mod.get_default_store()
    plain = store.create(name="Plain page", content=PAGE, kind="html")
    state, slot, _pushed = _setup()
    status, body = _call(state, REPORT, {"kind": "weekly", "title": "W", "html": PAGE}, slot.key)
    assert status == 200, body
    refresh = '<meta http-equiv="refresh" content="0;url=https://example.org/?reading=42">'
    # An agent may rename the report; the title lands in the uploaded document.
    store.update(body["slug"], content=PAGE + refresh, name=f"Week {TOKEN}")
    published, content_type = publish_sync._render_content(store.get(body["slug"]))
    assert content_type == "text/html"
    outer = html.parser.HTMLParser()
    tags: list[tuple[str, dict[str, str | None]]] = []
    outer.handle_starttag = lambda tag, attrs: tags.append((tag, dict(attrs)))  # type: ignore[method-assign]
    outer.feed(published)
    # The outer document holds no page markup: one frame, with an EMPTY sandbox,
    # which runs no script and follows no refresh.
    assert [t for t, _ in tags] == ["meta", "title", "style", "iframe"]
    assert TOKEN not in published
    frame = tags[-1][1]
    assert frame["sandbox"] == ""
    # Inside, the no-fetch policy precedes every page byte; the page is intact.
    inner = frame["srcdoc"] or ""
    assert inner.startswith(publish_sync.STATIC_REPORT_PREFIX)
    assert "default-src 'none'" in publish_sync.STATIC_REPORT_PREFIX
    assert inner.endswith(refresh)
    # An ordinary html artifact is published as it is today.
    ordinary, _ = publish_sync._render_content(store.get(plain.slug))
    assert ordinary == PAGE


def test_a_published_report_s_links_stay_clickable_and_show_their_url_on_hover(
    _floor_monkeypatch,
) -> None:
    """A link in the served copy keeps its href and shows it on hover, as in a session."""
    from kiro_crew import publish_sync

    links = (
        '<p><a href="https://example.org/pr/7?tab=files&amp;x=1">PR 7</a>'
        ' <a href="https://example.org/kept" title="Own title">Kept</a></p>'
        '<map><area href="https://example.org/m" alt="M"></map>'
    )
    published = publish_sync._static_report_document("W", PAGE + links)
    srcdoc: list[str] = []
    outer = html.parser.HTMLParser()
    outer.handle_starttag = lambda tag, attrs: srcdoc.extend(  # type: ignore[method-assign]
        v or "" for k, v in attrs if tag == "iframe" and k == "srcdoc"
    )
    outer.feed(published)
    found: list[dict[str, str | None]] = []
    inner = html.parser.HTMLParser()
    inner.handle_starttag = lambda tag, attrs: found.extend(  # type: ignore[method-assign]
        [dict(attrs)] if tag in ("a", "area") else []
    )
    inner.feed(srcdoc[0])
    pr, kept, area = found
    assert pr["href"] == pr["title"] == "https://example.org/pr/7?tab=files&x=1"
    assert kept == {"href": "https://example.org/kept", "title": "Own title"}
    assert area["href"] == area["title"] == "https://example.org/m"
    assert srcdoc[0].endswith(
        PAGE
        + links.replace(
            '">PR 7', '" title="https://example.org/pr/7?tab=files&amp;x=1">PR 7'
        ).replace('alt="M">', 'alt="M" title="https://example.org/m">')
    )


def test_both_check_in_prompts_share_the_report_look_and_name_both_formats() -> None:
    rec = member_goals.empty_record()
    rec["long_term"] = "Keep docs current"
    for cadence in ("weekly", "monthly"):
        prompt = goal_reminder.reminder_prompt(rec, cadence)
        assert goal_reminder.REPORT_LOOK in prompt
        assert "html" in prompt and "markdown" in prompt
    assert goal_reminder.REPORT_LOOK not in goal_reminder.reminder_prompt(rec, "daily")
    (tool,) = [t for t in mcp_mate._tool_definitions() if t["name"] == "save_goal_report"]
    assert "How the report looks" in tool["description"]
    assert goal_reminder.REPORT_LOOK.startswith("How the report looks")
    assert set(tool["inputSchema"]["properties"]) >= {"markdown", "html"}
    assert tool["inputSchema"]["required"] == ["kind", "title"]


#: Common typefaces a prompt might be tempted to name. The look names none: each
#: system draws its own face from the generic families.
_TYPEFACES = (
    "Inter",
    "Helvetica",
    "Arial",
    "Roboto",
    "Georgia",
    "Times New Roman",
    "Segoe UI",
    "SF Pro",
    "San Francisco",
    "Menlo",
    "Consolas",
    "Courier",
    "Fira",
    "JetBrains Mono",
    "Source Sans",
    "Noto",
    "Merriweather",
    "Playfair",
    "Lato",
    "Open Sans",
    "Montserrat",
    "IBM Plex",
    "Garamond",
    "Palatino",
    "Verdana",
    "Tahoma",
    "PingFang",
    "Hiragino",
    "Microsoft YaHei",
    "-apple-system",
    "BlinkMacSystemFont",
)


#: Language and script names the look leaves out.
_LANGUAGES_AND_SCRIPTS = (
    "Chinese",
    "Japanese",
    "Korean",
    "CJK",
    "Han",
    "Hanzi",
    "Kanji",
    "Kana",
    "Hangul",
    "English",
    "Latin",
    "Cyrillic",
    "Russian",
    "Greek",
    "Arabic",
    "Hebrew",
    "Devanagari",
    "Hindi",
    "Bengali",
    "Thai",
    "Vietnamese",
    "German",
    "French",
    "Spanish",
    "Italian",
    "Portuguese",
)


def test_the_report_look_names_no_typeface_and_states_the_pairing() -> None:
    look = goal_reminder.REPORT_LOOK
    for face in _TYPEFACES:
        assert not re.search(rf"(?<![\w-]){re.escape(face)}(?![\w-])", look, re.I), face
    for generic in ("system-ui, sans-serif", "ui-serif, serif", "ui-monospace, monospace"):
        assert generic in look, generic
    for role in ("--font-sans", "--font-serif", "--font-mono"):
        assert role in look, role
    lowered = look.lower()
    for phrase in (
        "two families at most",
        "mono only for code",
        "not for body text or headings",
        "size, weight and colour",
        "tabular-nums",
        "any one script in one style",
        "pick each script's system face",
    ):
        assert phrase in lowered, phrase
    # Language-neutral: Crew ships many locales, so the look names no language
    # and no script.
    for name in _LANGUAGES_AND_SCRIPTS:
        assert not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", look, re.I), name


def test_the_report_look_is_written_as_principles() -> None:
    look = goal_reminder.REPORT_LOOK.lower()
    for word in ("must", "never", "do not", "don't", "exactly"):
        assert word not in look, word
    # What it covers: conclusion first, content-shaped sections, one system,
    # a printable static page, plain words that agree with the readings, and
    # the user's style over the crewmate's own.
    for phrase in (
        "conclusion",
        "a comparison as a table",
        "a blocker as a card",
        "one visual system",
        "a4",
        "page number",
        "print-color-adjust",
        "inline svg",
        "embed no raster image",
        "no <img>, no data:image",
        "no scripts",
        "without internal ids",
        "agree with the readings",
        "the user has asked for wins",
        "each colour one meaning",
        "pair it with a word",
        "short explanation where it first appears",
        "by one name",
        "one time span",
    ):
        assert phrase in look, phrase


def test_the_mate_tool_forwards_the_html_page(_floor_monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    def post(path: str, body: dict[str, Any], *, session_key: str) -> dict[str, Any]:
        sent.append({"path": path, **body})
        return {"ok": True, "title": "Week 41", "url": "/artifacts/week-41"}

    _floor_monkeypatch.setattr(mcp_mate, "_post", post)
    out = mcp_mate._goal_call(
        "save_goal_report", {"kind": "weekly", "title": "Week 41", "html": PAGE}, "sk"
    )
    ((call,),) = [sent]
    assert call["path"] == "/api/member/self/goals/report"
    assert (call["html"], call["markdown"]) == (PAGE, "")
    assert "/artifacts/week-41" in out
