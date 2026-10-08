"""One-time schedule cards and Mate's read-only packaged-docs search.

A ``schedule.create`` card takes exactly one of ``cron_expr`` (recurring) or
``at`` (a local date-time, runs once). The gateway resolves ``at`` in the card's
zone, refuses a time already gone, and sends epoch ``at`` to the SAME
``POST /api/crons`` route the recurring card uses, which makes a single-fire job.
``search_docs`` reads only the shipped ``kiro_crew/docs`` pages.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_guide
from kiro_crew.dashboard import change_cards as cards

KIND = catalog.KIND_SCHEDULE_CREATE
FUTURE = "2099-10-04T09:00"
LA = "America/Los_Angeles"


def _one_shot(**extra: Any) -> dict[str, Any]:
    return {"name": "Dentist", "message": "Call the dentist", "at": FUTURE, **extra}


# ── validation ──


def test_one_shot_validates_and_normalizes():
    p = catalog.validate_params(KIND, _one_shot(at="2099-10-04T09:00:00", timezone=LA))
    assert p == {"name": "Dentist", "message": "Call the dentist", "at": FUTURE, "timezone": LA}
    assert "cron_expr" not in p


def test_recurring_still_validates():
    p = catalog.validate_params(KIND, {"name": "n", "message": "m", "cron_expr": "0 9 * * 1-5"})
    assert p["cron_expr"] == "0 9 * * 1-5" and "at" not in p


@pytest.mark.parametrize(
    "params",
    [
        _one_shot(cron_expr="0 9 * * *"),  # both
        {"name": "n", "message": "m"},  # neither
    ],
)
def test_exactly_one_of_cron_or_at(params):
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(KIND, params)
    assert exc.value.code == "invalid_param"
    assert "exactly one of 'cron_expr' or 'at'" in exc.value.message


@pytest.mark.parametrize(
    "at",
    ["tomorrow 9am", "2099-10-04", "2099-10-04T09:00-04:00", "2099-10-04T09:00Z", 1700000000],
)
def test_at_must_be_a_local_iso_datetime_without_offset(at):
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(KIND, _one_shot(at=at))
    assert exc.value.code == "invalid_param"


def test_at_must_be_a_real_date():
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(KIND, _one_shot(at="2099-02-30T09:00"))
    assert exc.value.code == "invalid_at"


def test_at_is_editable_on_the_card():
    assert "at" in catalog.KINDS[KIND].editable
    assert catalog.KINDS[KIND].params_schema["required"] == ["name", "message"]
    assert "at" in catalog.KINDS[KIND].params_schema["properties"]


# ── context: the instant, in the card's zone ──


def test_context_resolves_at_in_the_cards_zone():
    ctx = cards.one_shot_context(FUTURE, LA)
    expected = datetime(2099, 10, 4, 9, 0, tzinfo=ZoneInfo(LA)).timestamp()
    assert ctx == {
        "next_run_at": expected,
        "timezone": LA,
        "once": True,
        "run_at_local": FUTURE,
        "at_ts": expected,
    }


def test_context_defaults_to_the_configured_zone(monkeypatch):
    from kiro_crew.cron_service import schedule

    monkeypatch.setattr(schedule, "get_local_tz", lambda: ("Asia/Tokyo", ZoneInfo("Asia/Tokyo")))
    ctx = cards.one_shot_context(FUTURE, "")
    assert ctx["timezone"] == "Asia/Tokyo"
    assert ctx["at_ts"] == datetime(2099, 10, 4, 9, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()


def test_context_refuses_a_time_already_gone():
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.one_shot_context("2000-01-01T09:00", LA)
    assert exc.value.code == "at_in_past"


def test_context_refuses_an_unknown_zone():
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.one_shot_context(FUTURE, "Mars/Olympus")
    assert exc.value.code == "invalid_timezone"


def test_context_refuses_a_wall_clock_the_zone_skips():
    # 02:30 on the 2027 spring-forward day never happens in Los Angeles; the
    # card must not quietly schedule a different time.
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards.one_shot_context("2027-03-14T02:30", LA)
    assert exc.value.code == "at_not_in_zone"


def test_a_recurring_card_without_a_zone_names_the_configured_one(monkeypatch):
    from kiro_crew.cron_service import schedule

    monkeypatch.setattr(schedule, "get_local_tz", lambda: (LA, ZoneInfo(LA)))
    p = catalog.validate_params(
        KIND, {"name": "Standup", "message": "hi", "cron_expr": "0 9 * * *"}
    )
    ctx = asyncio.run(cards.read_context(KIND, p, {}, state=None, app=None))
    assert ctx["timezone"] == LA


def test_read_context_routes_a_one_shot_card():
    p = catalog.validate_params(KIND, _one_shot(timezone=LA))
    ctx = asyncio.run(cards.read_context(KIND, p, {}, state=None, app=None))
    assert ctx["once"] is True and ctx["timezone"] == LA


# ── preview ──


def _preview(tz: str = LA) -> dict[str, Any]:
    p = catalog.validate_params(KIND, _one_shot(timezone=tz))
    return catalog.build_preview(KIND, p, {}, cards.one_shot_context(p["at"], tz))


def test_preview_marks_once_and_sends_epoch_at_to_the_existing_route():
    preview = _preview()
    at_ts = int(datetime(2099, 10, 4, 9, tzinfo=ZoneInfo(LA)).timestamp())
    assert preview["once"] is True
    assert preview["title"] == "Create one-time reminder “Dentist”"
    when = next(r for r in preview["changes"] if r.get("field") == "at")
    assert when == {
        "field": "at",
        "label": "When",
        "after": FUTURE,
        "once": True,
        "timezone": LA,
    }
    assert preview["apply"] == [
        {
            "method": "POST",
            "path": "/api/crons",
            "body": {
                "name": "Dentist",
                "message": "Call the dentist",
                "timezone": LA,
                "at": at_ts,
            },
        }
    ]
    assert "cron" not in preview["apply"][0]["body"]


def test_preview_refuses_without_a_resolved_instant():
    p = catalog.validate_params(KIND, _one_shot(timezone=LA))
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(KIND, p, {}, {"timezone": LA})
    assert exc.value.code == "invalid_at"


def test_recurring_preview_is_not_marked_once():
    p = catalog.validate_params(KIND, {"name": "n", "message": "m", "cron_expr": "0 9 * * *"})
    preview = catalog.build_preview(KIND, p, {}, {})
    assert "once" not in preview
    assert preview["apply"][0]["body"]["cron"] == "0 9 * * *"


def test_card_record_carries_the_once_marker():
    rec: dict[str, Any] = {"kind": KIND}
    ctx = cards.one_shot_context(FUTURE, LA)
    store = cards.CardStore.__new__(cards.CardStore)
    store._apply_preview(rec, _preview(), {}, ctx)
    assert rec["once"] is True
    assert rec["run_at_local"] == FUTURE
    assert rec["timezone"] == LA
    assert rec["next_run_at"] == ctx["at_ts"]


# ── execute: the existing POST /api/crons makes a single-fire job ──


def test_the_existing_route_creates_a_single_fire_job():
    from kiro_crew.dashboard.handlers import cron as cron_handlers

    calls: list[tuple[str, str, dict[str, Any]]] = []

    class _Crons:
        async def add_job_async(self, name, message, **kw):
            calls.append((name, message, kw))
            return SimpleNamespace(id="job1")

    state = SimpleNamespace(crons=_Crons(), push_refresh=lambda _k: None)
    body = _preview()["apply"][0]["body"]

    async def run() -> tuple[int, dict[str, Any]]:
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/crons", cron_handlers.api_crons_create)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/crons", json=body)
            return resp.status, await resp.json()
        finally:
            await client.close()

    status, data = asyncio.run(run())
    assert status == 200 and data == {"ok": True, "id": "job1"}
    name, message, kw = calls[0]
    assert (name, message) == ("Dentist", "Call the dentist")
    assert kw["at_ts"] == body["at"] and kw["delete_after_run"] is True
    assert "cron_expr" not in kw and "every_secs" not in kw
    assert kw["timezone"] == LA


def test_undo_deletes_the_one_shot_job():
    p = catalog.validate_params(KIND, _one_shot(timezone=LA))
    undo, reason = catalog.build_undo(KIND, p, {}, [{"id": "job1"}], 1)
    assert reason is None
    assert undo == [{"method": "DELETE", "path": "/api/crons/job1", "body": None}]


# ── search_docs: read-only, packaged docs only ──


def test_search_docs_finds_slack_setup():
    d = mcp_guide.search_docs("connect Slack")
    pages = [r["page"] for r in d["results"]]
    assert "slack-integration" in pages
    assert len(pages) <= 5


def test_search_docs_reads_a_page_and_pages_on():
    d = mcp_guide.search_docs(page="slack-integration")
    assert d["page"] == "slack-integration" and d["text"]
    big = max(mcp_guide._doc_pages().items(), key=lambda kv: kv[1].stat().st_size)
    first = mcp_guide.search_docs(page=big[0])
    if "next_offset" in first:
        more = mcp_guide.search_docs(page=big[0], offset=first["next_offset"])
        assert more["text"] and more["text"] != first["text"]


@pytest.mark.parametrize(
    "page",
    [
        "../agent",
        "/etc/passwd",
        "../../pyproject",
        "README/../../x",
        "nope",
        # Real .md files OUTSIDE the docs directory in a source checkout.
        "../../../README",
        "../../../docs/system-specs/modules/crew-mode",
    ],
)
def test_search_docs_never_reads_outside_the_docs_listing(page):
    assert "error" in mcp_guide.search_docs(page=page)


def test_search_docs_finds_where_closed_chats_live():
    d = mcp_guide.search_docs("closed sessions older sessions")
    blob = json.dumps(d).lower()
    assert "older sessions" in blob


def test_search_docs_needs_no_caller_identity(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("search_docs must not need a session key or a gateway call")

    monkeypatch.setattr(mcp_guide, "_strict_session_key", boom)
    monkeypatch.setattr(mcp_guide, "_get", boom)
    monkeypatch.setattr(mcp_guide, "_post", boom)
    out = json.loads(mcp_guide._call_tool_inner("search_docs", {"query": "Slack"}))
    assert out["results"]


def test_search_docs_and_find_ui_are_pre_approved_reads_on_every_agent():
    import json as _json

    from kiro_crew import agent

    assert "@kirocrew-guide/search_docs" in agent._GUIDE_AUTO_GRANTS
    assert "@kirocrew-guide/find_ui" in agent._GUIDE_AUTO_GRANTS
    shipped = _json.loads((agent._BUNDLED_CFG_DIR / "defaults.json").read_text())
    # The shipped default template carries exactly the reviewed guide grants.
    assert [g for g in shipped["allowedTools"] if g.startswith("@kirocrew-guide")] == list(
        agent._GUIDE_AUTO_GRANTS
    )
    assert "@kirocrew-guide" in shipped["tools"]
