"""Crew Mode timeline: every bar comes from a recorded transcript row.

``crew_timeline.derive_lane`` is pure, so each case feeds synthetic rows shaped like
the ones the chat transcript writes (role, content, ts, meta) and checks the bars,
marks and hand-offs; the route test drives ``api_crew_timeline`` with stub slots.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode, crew_timeline

T0 = datetime(2026, 10, 21, 3, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _row(role: str, seconds: float, content: str = "", **meta: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"role": role, "content": content, "ts": _at(seconds)}
    if meta:
        row["meta"] = meta
    return row


def _send(seconds: float, target: str, tcid: str) -> dict[str, Any]:
    payload = {"tool_id": "kirocrew-dashboard::session_send", "arguments": {"target": target}}
    return _row(
        "tool",
        seconds,
        "🔧 @kirocrew-dashboard/session_send",
        tool_call_id=tcid,
        input=json.dumps(payload),
    )


def _lane(rows: list[dict[str, Any]], *, now: float = 100, running=False, waiting=False):
    return crew_timeline.derive_lane(
        rows, now=T0 + timedelta(seconds=now), running=running, waiting_now=waiting
    )


def _kinds(lane: dict[str, Any]) -> list[str]:
    return [bar["kind"] for bar in lane["segments"]]


def test_a_finished_turn_is_one_bar_from_its_recorded_elapsed_time():
    lane = _lane(
        [
            _row("user", 0, "go"),
            _row("assistant", 10, "done", turn_stats={"elapsed_ms": 8000, "credits": 0.5}),
        ]
    )
    (turn,) = lane["segments"]
    assert turn["kind"] == "turn"
    assert turn["start"] == _at(2) and turn["end"] == _at(10)
    assert turn["end_bound"] == "recorded"
    assert lane["credits"] == 0.5


def test_an_assistant_row_without_turn_stats_draws_no_turn():
    lane = _lane([_row("user", 0, "go"), _row("assistant", 5, "partial")])
    assert lane["segments"] == []


def test_a_running_turn_runs_to_now_and_says_so():
    lane = _lane([_row("user", 10, "go")], now=40, running=True)
    (turn,) = lane["segments"]
    assert (turn["start"], turn["end"], turn["end_bound"]) == (_at(10), _at(40), "now")


def test_an_idle_session_has_no_open_bar():
    assert _lane([_row("user", 10, "go")], running=False)["segments"] == []


def test_a_tool_ends_at_the_next_row_because_its_finish_is_not_stamped():
    lane = _lane(
        [
            _row("tool", 1, "🔧 @kirocrew-work/work_brief", tool_call_id="a", done=True),
            _row("assistant", 4, "next"),
        ]
    )
    (tool,) = lane["segments"]
    assert tool["kind"] == "tool"
    assert (tool["start"], tool["end"], tool["end_bound"]) == (_at(1), _at(4), "next_row")
    assert tool["label"] == "@kirocrew-work/work_brief"


def test_a_tool_with_no_later_row_in_an_idle_session_is_a_point_not_a_guess():
    lane = _lane([_row("tool", 1, "🔧 @x/y", tool_call_id="a", done=True)])
    (tool,) = lane["segments"]
    assert tool["start"] == tool["end"] == _at(1)


def test_an_approval_wait_is_the_recorded_gap_between_the_call_and_its_approval():
    lane = _lane(
        [
            _row("tool", 2, "🔧 @kirocrew-dashboard/session_send", tool_call_id="s"),
            _row("tool", 12, "✅ @kirocrew-dashboard/session_send", tool_call_id="s"),
            _row("tool", 13, "🔧 @x/other", tool_call_id="o"),
        ]
    )
    waiting, tool, other = lane["segments"]
    assert waiting["kind"] == "waiting" and waiting["end_bound"] == "recorded"
    assert (waiting["start"], waiting["end"]) == (_at(2), _at(12))
    assert tool["kind"] == "tool" and (tool["start"], tool["end"]) == (_at(12), _at(13))
    assert other["label"] == "@x/other"
    # The ✅ row is the same call, not a second tool.
    assert _kinds(lane).count("tool") == 2


def test_a_pending_question_is_waiting_until_now():
    lane = _lane(
        [_row("tool", 5, "🔧 @kirocrew-core/ask_question", tool_call_id="q")],
        now=30,
        running=True,
        waiting=True,
    )
    bars = {bar["kind"]: bar for bar in lane["segments"]}
    assert bars["waiting"]["end"] == _at(30) and bars["waiting"]["end_bound"] == "now"


def test_a_permission_row_waits_until_the_next_row():
    lane = _lane([_row("permission", 3, "Run ls"), _row("tool", 9, "✅ Run ls")])
    assert lane["segments"][0]["kind"] == "waiting"
    assert lane["segments"][0]["end"] == _at(9)


def test_error_rows_and_refusals_are_marks_not_bars():
    lane = _lane(
        [
            _row("error", 3, "MCP readiness timed out", kind="session_start_failed"),
            _row("tool", 4, "🚫 Run rm (rejected)", tool_call_id="r"),
            _row("system", 5, "❌ Compaction failed."),
        ]
    )
    assert lane["segments"] == []
    assert [m["kind"] for m in lane["marks"]] == ["failed", "refused", "failed"]


def test_an_error_ends_the_open_turn():
    lane = _lane([_row("user", 0, "go"), _row("error", 2, "boom")], now=9, running=True)
    assert lane["segments"] == []


def test_rows_without_a_timestamp_are_skipped():
    lane = _lane([{"role": "user", "content": "x"}, {"role": "user", "ts": "nope"}])
    assert lane["first_ts"] == "" and lane["segments"] == []


def test_a_handoff_joins_the_senders_call_to_the_workers_seed_row():
    root = {
        "key": "root",
        **_lane(
            [
                _row("tool", 2, "🔧 @kirocrew-dashboard/session_create", tool_call_id="c"),
                _send(5, "w1", "s"),
                _send(5.5, "w2", "u"),
                _send(9, "w1", "t"),
            ]
        ),
    }
    worker = {
        "key": "w1",
        **_lane([_row("user", 6, "[sent by session root via session_send]\n\nWrite it")]),
    }
    # w2's seed lands after root's send to w1 at 5.5 and 9; only its own send pairs.
    second = {
        "key": "w2",
        **_lane([_row("user", 5.7, "[sent by session root via session_send]\n\nhi")]),
    }
    stray = {
        "key": "w3",
        **_lane([_row("user", 7, "[sent by session nobody via session_send]\n\nhi")]),
    }
    links = crew_timeline.handoffs([root, worker, second, stray])
    assert links == [
        {"from": "root", "to": "w1", "start": _at(5), "end": _at(6)},
        {"from": "root", "to": "w2", "start": _at(5.5), "end": _at(5.7)},
    ]


def test_only_the_genuine_envelope_on_an_unmarked_row_is_a_seed():
    """G3: the quoted (defanged) form and a person typing the envelope are no hand-off."""
    from kiro_crew.history import HUMAN_TURN_META_KEY

    assert crew_timeline._HUMAN_TURN_KEY == HUMAN_TURN_META_KEY
    genuine = "[sent by session root via session_send]\n\nWrite it"
    rows = [
        _row("user", 1, "[quoted: sent by session root via session_send]\n\nhi"),
        _row("user", 2, genuine, human=True),
        _row("user", 3, genuine),
    ]
    assert [seed["ts"] for seed in _lane(rows)["seeds"]] == [_at(3)]


def test_a_send_without_a_readable_target_is_no_handoff():
    assert crew_timeline.send_target({"meta": {"input": "not json"}}) == ""
    assert crew_timeline.send_target({"meta": {"input": '{"target": "w9"}'}}) == "w9"


def test_the_header_is_real_elapsed_time_and_summed_credits():
    lanes = [
        {"first_ts": _at(0), "last_ts": _at(30), "credits": 0.25},
        {"first_ts": _at(5), "last_ts": _at(50), "credits": 0.5},
    ]
    done = crew_timeline.summarize(lanes, now=T0 + timedelta(seconds=99), live=False)
    assert done["elapsed_ms"] == 50_000 and done["credits"] == 0.75
    live = crew_timeline.summarize(lanes, now=T0 + timedelta(seconds=99), live=True)
    assert live["elapsed_ms"] == 99_000


def test_a_long_lane_keeps_its_newest_bars_and_counts_the_rest(monkeypatch):
    monkeypatch.setattr(crew_timeline, "MAX_SEGMENTS_PER_LANE", 2)
    rows = [_row("tool", i, f"🔧 @x/t{i}", tool_call_id=str(i)) for i in range(4)]
    lane = _lane(rows)
    assert lane["dropped_segments"] == 2
    assert [bar["label"] for bar in lane["segments"]] == ["@x/t2", "@x/t3"]


def test_a_long_window_is_one_pass_not_a_scan_per_tool_row(monkeypatch):
    """G2: 10,000 rows of auto-approved calls (no ✅ rows) cost work linear in the rows.

    Counted, not timed: every row read goes through ``_meta`` and ``_content``, so a
    scan to the end per tool row (the old shape) reads ~25 million rows here.
    """
    reads = {"n": 0}
    real_meta, real_content = crew_timeline._meta, crew_timeline._content

    def _meta(row):
        reads["n"] += 1
        return real_meta(row)

    def _content(row):
        reads["n"] += 1
        return real_content(row)

    monkeypatch.setattr(crew_timeline, "_meta", _meta)
    monkeypatch.setattr(crew_timeline, "_content", _content)
    rows = []
    for i in range(5000):
        rows.append(_row("tool", i * 2, f"🔧 @x/t{i}", tool_call_id=f"c{i}", done=True))
        rows.append(_row("assistant", i * 2 + 1, "ok"))
    lane = _lane(rows, now=20_000)
    assert reads["n"] < 20 * len(rows)
    assert lane["dropped_segments"] == 5000 - crew_timeline.MAX_SEGMENTS_PER_LANE


def test_an_approval_far_down_the_window_is_still_found():
    rows = [_row("tool", 0, "🔧 @x/run", tool_call_id="far")]
    rows += [_row("assistant", 1 + i, "noise") for i in range(50)]
    rows.append(_row("tool", 100, "✅ @x/run", tool_call_id="far"))
    waiting = [bar for bar in _lane(rows)["segments"] if bar["kind"] == "waiting"]
    assert waiting and waiting[0]["end"] == _at(100)


def test_labels_go_through_the_redactor():
    lane = {"segments": [{"label": "secret A"}], "marks": [{"label": "secret B"}, {"label": ""}]}
    crew_timeline.redact_labels(lane, lambda text: text.replace("secret", "[x]"))
    assert lane["segments"][0]["label"] == "[x] A"
    assert [m["label"] for m in lane["marks"]] == ["[x] B", ""]


def test_every_gap_is_named():
    assert set(crew_timeline.GAPS) >= {
        "thinking_vs_output",
        "tool_end_time",
        "tool_failure_status",
        "tokens",
        "closed_worker_rows",
        "rows_before_window",
        "served_model_unreported",
    }


# ── the route ────────────────────────────────────────────────────────────────


def _slot_row(key: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "title": key, "agent": "kirocrew", "messages": 1, **extra}


@pytest.fixture
def _route(monkeypatch):
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    async def _loaded(slot: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _allow)
    monkeypatch.setattr(crew_workers, "_closed_workers", lambda root, live: ["gone"])
    monkeypatch.setattr(dashboard_state, "load_window_off_loop", _loaded)
    return crew_workers


def _state(rows: list[dict[str, Any]], windows: dict[str, list[dict[str, Any]]]) -> Any:
    def serialize_slots(**_k: Any) -> list[dict[str, Any]]:
        out = [dict(r) for r in rows]
        crew_mode.stamp_rows(out)
        return out

    slots = {key: SimpleNamespace(key=key, messages=msgs) for key, msgs in windows.items()}
    return SimpleNamespace(serialize_slots=serialize_slots, get_slot=slots.get)


def _get(state: Any, slot: str) -> Any:
    app = web.Application()
    app["state"] = state
    return make_mocked_request(
        "GET", f"/api/chat/slots/{slot}/crew-timeline", app=app, match_info={"slot": slot}
    )


@pytest.mark.asyncio
async def test_the_route_draws_the_root_then_its_workers_then_closed_ones(_route):
    rows = [
        _slot_row("root", agent="kirocrew-conductor", model="claude-x"),
        _slot_row("w1", created_by="root", served_model="claude-y"),
        _slot_row("other"),
    ]
    windows = {
        "root": [
            _row("user", 0, "go"),
            _send(1, "w1", "s"),
            _row("assistant", 4, "ok", turn_stats={"elapsed_ms": 4000, "credits": 0.2}),
        ],
        "w1": [
            _row("user", 2, "[sent by session root via session_send]\n\nhi"),
            _row("assistant", 6, "ok", turn_stats={"elapsed_ms": 4000, "credits": 0.3}),
        ],
    }
    resp = await _route.api_crew_timeline(_get(_state(rows, windows), "root"))
    body = json.loads(resp.text)
    assert [lane["key"] for lane in body["lanes"]] == ["root", "w1", "gone"]
    assert [lane["role"] for lane in body["lanes"]] == ["conductor", "worker", "worker"]
    assert (body["lanes"][0]["model"], body["lanes"][0]["model_source"]) == ("claude-x", "chosen")
    assert (body["lanes"][1]["model"], body["lanes"][1]["model_source"]) == ("claude-y", "served")
    assert body["lanes"][2]["status"] == "closed" and body["lanes"][2]["segments"] == []
    assert body["handoffs"] == [{"from": "root", "to": "w1", "start": _at(1), "end": _at(2)}]
    assert body["credits"] == 0.5 and body["live"] is False
    assert body["elapsed_ms"] == 6000
    assert "tool_end_time" in body["gaps"]
    # The closed lane runs on the default, so the served-model gap is named.
    assert "served_model_unreported" in body["gaps"]
    assert "other" not in {lane["key"] for lane in body["lanes"]}


@pytest.mark.asyncio
async def test_the_route_redacts_what_the_rows_carry(_route):
    """G4: a permission row with a key in it reaches the browser redacted."""
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    rows = [_slot_row("root", agent="kirocrew-conductor")]
    windows = {
        "root": [
            _row("user", 0, "go"),
            _row("permission", 1, f"Run aws with {key}"),
            _row("error", 2, f"failed: {key}"),
            _row("assistant", 3, "ok"),
        ]
    }
    resp = await _route.api_crew_timeline(_get(_state(rows, windows), "root"))
    assert key not in resp.text


def test_a_lane_with_no_model_names_the_gateway_default(monkeypatch):
    from kiro_crew.config import live
    from kiro_crew.dashboard.handlers import crew_workers

    monkeypatch.setattr(
        live, "snapshot", lambda: SimpleNamespace(agent=SimpleNamespace(model="auto"))
    )
    assert crew_workers._lane_model({"model": "", "served_model": ""}) == {
        "model": "auto",
        "model_source": "default",
    }
    monkeypatch.setattr(live, "snapshot", lambda: None)
    assert crew_workers._lane_model({}) == {"model": "", "model_source": "default"}


@pytest.mark.asyncio
async def test_the_route_refuses_a_session_that_is_not_a_crew_mode_root(_route):
    resp = await _route.api_crew_timeline(_get(_state([_slot_row("other")], {}), "other"))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_the_timeline_is_owner_only(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    async def _deny(*_a: Any, **_k: Any) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _deny)
    resp = await crew_workers.api_crew_timeline(_get(_state([], {}), "root"))
    assert resp.status == 403
