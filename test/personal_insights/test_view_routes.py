from __future__ import annotations

from pathlib import Path
from test.personal_insights.test_apply import SYNTH

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import personal_insights as routes
from kiro_crew.personal_insights.insights_facts import SessionFacts
from kiro_crew.personal_insights.insights_report import (
    FOLLOW_THROUGH_MIN_SESSIONS,
    build_actions,
    follow_through,
)
from kiro_crew.personal_insights.insights_runs import RunRepository
from kiro_crew.personal_insights.insights_view import run_view, split_report

REPORT = (
    "# Personal Insights: last 30 days\n\nintro\n\n## What stands out\n\nrecognition\n\n"
    "## Recommended next changes\n\n### 1. Verify before claiming\n\n**[Do it]**  Copy\n\n"
    '```insights-action\n{\n "action_id": "a1-x"\n}\n```\n\n'
    "## What is working\n\n- w\n\n## Coverage and method\n\n- c\n"
)


def _facts(key: str, started: str, corrections: int, stalls: int = 0) -> SessionFacts:
    return SessionFacts(
        key=key,
        title=key,
        started=started,
        ended=started,
        duration_minutes=10,
        owner_turns=3,
        assistant_turns=3,
        tool_calls=5,
        tool_errors=0,
        stalls=stalls,
        subagent_dispatches=0,
        correction_markers=corrections,
    )


@pytest.fixture
def repo(tmp_path: Path) -> RunRepository:
    r = RunRepository(tmp_path / "runs.sqlite")
    run_id = r.start_run(window_days=30, cataloged=10, selected=4)
    r.record_claims(
        run_id,
        [
            {
                "claim_id": c["claim_id"],
                "dimension": s,
                "text": c["text"],
                "session_keys": c["session_keys"],
            }
            for s in ("working", "friction")
            for c in SYNTH[s]
        ],
    )
    r.record_actions(run_id, build_actions(SYNTH), baseline_sessions=4)
    report = tmp_path / "report.md"
    report.write_text(REPORT, encoding="utf-8")
    r.finish_run(run_id, analyzed=4, served_model="m", report_path=str(report), report_digest="d")
    yield r
    r.close()


def test_split_report_removes_action_section_and_fences():
    parts = split_report(REPORT)
    assert "Recommended next changes" not in parts["before"]
    assert "insights-action" not in parts["before"] + parts["after"]
    assert parts["before"].rstrip().endswith("recognition")
    assert parts["after"].startswith("## What is working")
    assert "## Coverage and method" in parts["after"]


def test_split_report_without_action_section_keeps_everything():
    parts = split_report("# t\n\nbody\n")
    assert parts == {"before": "# t\n\nbody\n", "after": ""}


def test_run_view_exposes_actions_with_state_and_evidence(repo: RunRepository):
    view = run_view(repo)
    assert view is not None
    assert view["run"]["analyzed"] == 4
    assert [a["rank"] for a in view["actions"]] == [1, 2]
    first = view["actions"][0]
    assert first["state"] == "proposed"
    assert first["cta"] == "Do it"
    assert first["executable"] is True
    assert first["evidence_sessions"] == 3
    assert first["evidence_total"] == 4
    assert first["display_artifact"].startswith("Re-read the artifact")
    assert "insights-action" not in view["report_before"]
    assert view["runs"][0]["run_id"] == view["run"]["run_id"]


def test_run_view_reflects_applied_state(repo: RunRepository):
    view = run_view(repo)
    assert view is not None
    action = view["actions"][0]
    repo.set_action_state(
        view["run"]["run_id"],
        action["action_id"],
        "applied_verified",
        target_identity="lesson.x",
        applied=True,
        verified=True,
    )
    again = run_view(repo, view["run"]["run_id"])
    assert again is not None
    assert again["actions"][0]["state"] == "applied_verified"
    assert again["actions"][0]["applied_at"] is not None
    assert run_view(repo, "20260101T000000Z-ffffff") is None


def test_build_actions_drops_already_applied_behavior_target_and_reranks():
    first = build_actions(SYNTH)
    assert [a["rank"] for a in first] == [1, 2]
    prior = [
        {"action_key": first[0]["action_key"], "state": "applied_verified"},
        {"action_key": "unrelated", "state": "applied_verified"},
        {"action_key": first[1]["action_key"], "state": "undone"},
    ]
    remaining = build_actions(SYNTH, prior)
    assert [a["title"] for a in remaining] == ["Single-session rule"]
    assert remaining[0]["rank"] == 1


def test_follow_through_is_too_early_until_enough_sessions():
    prior = [
        {
            "action_id": "a1",
            "run_id": "r1",
            "title": "t",
            "state": "applied_verified",
            "applied_at": 1_700_000_000.0,
            "behavior_predicate": "p",
        }
    ]
    before = [_facts(f"b{i}", "2023-11-10T00:00:00+00:00", 1) for i in range(4)]
    since = [_facts(f"s{i}", "2023-11-20T00:00:00+00:00", 0) for i in range(2)]
    item = follow_through(prior, before + since)[0]
    assert item["verdict"] == "too_early"
    assert item["since"]["sessions"] == 2
    assert item["sessions_needed"] == FOLLOW_THROUGH_MIN_SESSIONS - 2


def test_follow_through_reports_improvement_from_deterministic_signals():
    prior = [
        {
            "action_id": "a1",
            "run_id": "r1",
            "title": "t",
            "state": "applied_verified",
            "applied_at": 1_700_000_000.0,
        }
    ]
    before = [_facts(f"b{i}", "2023-11-10T00:00:00Z", 1) for i in range(4)]
    since = [_facts(f"s{i}", "2023-11-20T00:00:00Z", 1 if i == 0 else 0) for i in range(5)]
    item = follow_through(prior, before + since)[0]
    assert item["verdict"] == "improved"
    assert item["before"]["corrected_share"] == 100.0
    assert item["since"]["corrected_share"] == 20.0
    assert item["delta_points"] == -80.0
    worse = follow_through(
        prior,
        [_facts("b", "2023-11-10T00:00:00Z", 0)]
        + before[:0]
        + [_facts(f"w{i}", "2023-11-20T00:00:00Z", 1) for i in range(5)],
    )[0]
    assert worse["verdict"] == "worse"


@pytest.mark.asyncio
async def test_routes_refuse_app_tokens_and_bad_ids(monkeypatch):
    audits: list[tuple[str, str]] = []

    class Sel:
        def log_tool_invocation(self, **kw):
            audits.append((kw["tool_name"], kw["outcome"]))

    monkeypatch.setattr(routes._sel_mod, "sel", lambda: Sel())
    req = make_mocked_request("GET", "/api/personal-insights/latest")
    req["app"] = "some-app"
    assert (await routes.api_personal_insights_latest(req)).status == 403
    req = make_mocked_request("POST", "/api/personal-insights/actions/x/do-it")
    assert (await routes.api_personal_insights_do_it(req)).status == 403
    req = make_mocked_request(
        "POST",
        "/api/personal-insights/actions/BAD!/do-it",
        match_info={"action_id": "BAD!"},
    )
    req["app"] = ""
    assert (await routes.api_personal_insights_do_it(req)).status == 400
    req = make_mocked_request(
        "GET", "/api/personal-insights/runs/nope", match_info={"run_id": "nope"}
    )
    req["app"] = ""
    assert (await routes.api_personal_insights_run(req)).status == 400
    assert ("personal_insights.read", "denied") in audits
    assert ("personal_insights.do_it", "denied") in audits


@pytest.mark.asyncio
async def test_do_it_route_returns_409_when_held_and_200_when_applied(monkeypatch):
    class Sel:
        def log_tool_invocation(self, **kw):
            pass

    monkeypatch.setattr(routes._sel_mod, "sel", lambda: Sel())
    monkeypatch.setattr(
        routes, "_apply", lambda action_id, force: {"state": "held", "message": "m"}
    )
    body: dict = {}

    async def fake_read(_request, max_bytes=0, **_kw):
        return body, None

    monkeypatch.setattr(routes, "read_bounded_json", fake_read)
    req = make_mocked_request(
        "POST",
        "/api/personal-insights/actions/a1-x/do-it",
        match_info={"action_id": "a1-x"},
    )
    req["app"] = ""
    resp = await routes.api_personal_insights_do_it(req)
    assert resp.status == 409
    seen: dict[str, bool] = {}

    def fake_apply(action_id, force):
        seen["force"] = force
        return {"state": "applied_verified", "action_id": action_id}

    monkeypatch.setattr(routes, "_apply", fake_apply)
    body = {"force": True}
    req = make_mocked_request(
        "POST",
        "/api/personal-insights/actions/a1-x/do-it",
        match_info={"action_id": "a1-x"},
    )
    req["app"] = ""
    resp = await routes.api_personal_insights_do_it(req)
    assert resp.status == 200
    assert seen == {"force": True}
    monkeypatch.setattr(routes, "_undo", lambda action_id: {"state": "undone"})
    req = make_mocked_request(
        "POST",
        "/api/personal-insights/actions/a1-x/undo",
        match_info={"action_id": "a1-x"},
    )
    req["app"] = ""
    assert (await routes.api_personal_insights_undo(req)).status == 200
