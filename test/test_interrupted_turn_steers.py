"""Mid-turn steers in the interrupted-turn restore a Resume prepends.

A steer is a ``user`` row marked ``meta.steer`` that joined a turn already
running. The restore must name the row that OPENED the turn as the interrupted
request and carry each steer, in order, with the partial output -- except a
steer the turn never took, which runs as its own turn.
"""

from __future__ import annotations

from kiro_crew.context import build_interrupted_turn_preamble
from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS


def _steer(text: str) -> dict:
    return {"role": "user", "content": text, "meta": {"steer": True, "steerState": "consumed"}}


def _resume(rows: list[dict]) -> str:
    current = {"role": "inject", "content": "resume", "meta": {"injectKind": "recovery"}}
    rows.append(current)
    return build_interrupted_turn_preamble(
        rows, current=current, opener_inject_kinds=TURN_OPENING_INJECT_KINDS
    )


def _earlier_turn() -> list[dict]:
    return [
        {"role": "user", "content": "earlier answered request"},
        {"role": "assistant", "content": "earlier answer"},
    ]


def test_a_steer_is_not_taken_as_the_interrupted_request():
    rows = _earlier_turn() + [
        {"role": "user", "content": "deploy the canary to us-west-2"},
        {"role": "assistant", "content": "Starting the canary deploy"},
        _steer("use the blue fleet instead"),
        {"role": "tool", "content": "", "meta": {"tool": "shell"}},
        {"role": "assistant", "content": "Switching to the blue fleet"},
        {"role": "error", "content": "connection lost"},
    ]

    out = _resume(rows)

    assert "Interrupted request:\ndeploy the canary to us-west-2" in out
    assert "the user's most recent request" in out
    assert "earlier answer" not in out
    partial = out.split("Partial response before the interruption:\n", 1)[1]
    assert partial.index("Starting the canary deploy") < partial.index(
        "[Steered mid-turn] use the blue fleet instead"
    )
    assert partial.index("[Steered mid-turn] use the blue fleet instead") < partial.index(
        "Switching to the blue fleet"
    )


def test_several_steers_are_carried_in_the_order_they_were_sent():
    rows = [
        {"role": "user", "content": "write the release notes"},
        {"role": "assistant", "content": "Drafting"},
        _steer("first steer"),
        _steer("second steer"),
        {"role": "assistant", "content": "Adjusted"},
        _steer("third steer"),
    ]

    out = _resume(rows)

    assert "Interrupted request:\nwrite the release notes" in out
    partial = out.split("Partial response before the interruption:\n", 1)[1]
    lines = partial.split("\n[END INTERRUPTED TURN]", 1)[0].split("\n")
    assert lines == [
        "Drafting",
        "[Steered mid-turn] first steer",
        "[Steered mid-turn] second steer",
        "Adjusted",
        "[Steered mid-turn] third steer",
    ]


def test_a_turn_with_no_steers_restores_the_same_frame():
    rows = _earlier_turn() + [
        {"role": "user", "content": "rotate the keys"},
        {"role": "assistant", "content": "Rotating"},
        {"role": "error", "content": "connection lost"},
    ]

    out = _resume(rows)

    assert out == "\n".join(
        [
            "[INTERRUPTED TURN — context restore]",
            "The turn below was cut off when the agent process serving this "
            "conversation stopped, so the restored conversation may not include it. "
            "It is the user's most recent request, the one you are being asked to "
            "carry on with. Tool calls it made may already have taken effect.",
            "",
            "Interrupted request:\nrotate the keys",
            "",
            "Partial response before the interruption:\nRotating",
            "[END INTERRUPTED TURN]",
        ]
    )
    assert "[Steered mid-turn]" not in out


def test_a_steered_automated_opener_stays_the_boundary():
    # A steer into a turn a nudge, a sub-agent completion or a cron delivery
    # opened belongs to that turn: the walk stops at that opener and never
    # reaches the user request answered before it.
    openers = [
        ({"role": "nudge", "content": "cycle 3"}, "a monitor loop cycle"),
        ({"role": "subagent", "content": "review done"}, "a sub-agent completion"),
        (
            {"role": "inject", "content": "rotate the logs", "meta": {"injectKind": "cron"}},
            "a scheduled job",
        ),
    ]
    for opener, kind in openers:
        rows = _earlier_turn() + [
            opener,
            {"role": "assistant", "content": "Working on it"},
            _steer("skip the archive step"),
        ]

        out = _resume(rows)

        assert f"Interrupted automated delivery:\n{opener['content']}" in out, kind
        assert kind in out and "the user's most recent request" not in out, kind
        assert "[Steered mid-turn] skip the archive step" in out, kind
        assert "earlier answered request" not in out, kind


def test_a_steer_after_an_earlier_resume_press_still_finds_the_request():
    rows = _earlier_turn() + [
        {"role": "user", "content": "migrate the table"},
        {"role": "assistant", "content": "Copying rows"},
        {"role": "inject", "content": "resume (first press)", "meta": {"injectKind": "recovery"}},
        _steer("batch it in thousands"),
        {"role": "assistant", "content": "Batching"},
    ]

    out = _resume(rows)

    assert "Interrupted request:\nmigrate the table" in out
    assert "[Steered mid-turn] batch it in thousands" in out
    assert "resume (first press)" not in out


def test_a_window_holding_only_steers_restores_nothing():
    # The opener fell outside the window: a steer is never promoted to the
    # interrupted request in its place.
    rows = [
        _steer("use the blue fleet instead"),
        {"role": "assistant", "content": "Switching"},
    ]

    assert _resume(rows) == ""


def test_only_a_true_steer_flag_marks_a_steer():
    for meta in ({"steer": False}, {"steer": "true"}, {}, None):
        row = {"role": "user", "content": "plain request"}
        if meta is not None:
            row["meta"] = meta
        out = _resume(_earlier_turn() + [row])
        assert "Interrupted request:\nplain request" in out, meta


def test_a_requeued_steer_is_walked_past_but_not_carried():
    # The turn ended before it took this steer, so it waits in the queue as its
    # own turn (or the user dismissed it). The restore still finds the request
    # behind it, but must not tell the model the steer redirected this turn.
    requeued = _steer("also bump the version")
    requeued["meta"]["steerState"] = "requeued"
    rows = _earlier_turn() + [
        {"role": "user", "content": "tag the release"},
        {"role": "assistant", "content": "Tagging"},
        _steer("sign the tag"),
        requeued,
    ]

    out = _resume(rows)

    assert "Interrupted request:\ntag the release" in out
    assert "[Steered mid-turn] sign the tag" in out
    assert "also bump the version" not in out


def test_a_steer_with_an_unsettled_or_missing_state_is_carried():
    # `written` (sent, consumption not yet echoed) and a row with no state may
    # have redirected the turn; only `requeued` says it did not.
    for meta in ({"steer": True, "steerState": "written"}, {"steer": True}):
        rows = [
            {"role": "user", "content": "tag the release"},
            {"role": "user", "content": "sign the tag", "meta": meta},
        ]
        assert "[Steered mid-turn] sign the tag" in _resume(rows), meta


def test_a_taken_back_steer_is_walked_past_but_not_carried():
    # The person took this steer back before the turn read it, so it never
    # redirected the turn and must not be presented to the model as if it had.
    retracted = _steer("also bump the version")
    retracted["meta"]["steerState"] = "retracted"
    rows = _earlier_turn() + [
        {"role": "user", "content": "tag the release"},
        {"role": "assistant", "content": "Tagging"},
        _steer("sign the tag"),
        retracted,
    ]

    out = _resume(rows)

    assert "Interrupted request:\ntag the release" in out
    assert "[Steered mid-turn] sign the tag" in out
    assert "also bump the version" not in out


def test_a_taken_back_steer_is_not_replayed_on_a_cold_start(tmp_path):
    # A cold start replays the transcript tail as conversation. A steer the person
    # took back never reached the turn; if its row kept its text (a transcript
    # written before the take-back cleared it, or a pending row), the replay would
    # still present it as something the person said.
    from kiro_crew import context as ctx
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "hist")
    key = "dashboard:replay-retracted"
    log.append(key, "user", "tag the release")
    log.append(key, "assistant", "Tagging")
    log.append(
        key,
        "user",
        "also delete the old tags",
        extra_meta={"steer": True, "steerState": "retracted"},
        mid="m-disk",
    )
    pending = [
        {
            "role": "user",
            "content": "and push to prod",
            "ts": "2099-01-01T00:00:00+00:00",
            "meta": {"steer": True, "steerState": "retracted", "mid": "m-live"},
        }
    ]

    replay = ctx.build_session_replay(log, key, pending_messages=pending)

    assert "tag the release" in replay
    assert "also delete the old tags" not in replay
    assert "and push to prod" not in replay


def test_the_requeued_state_matches_the_dashboard_lifecycle():
    from kiro_crew.context_assembly import replay
    from kiro_crew.dashboard.chat_delivery import STEER_STATE_REQUEUED, STEER_STATE_RETRACTED

    assert replay._STEER_STATE_REQUEUED == STEER_STATE_REQUEUED
    assert replay._STEER_STATE_RETRACTED == STEER_STATE_RETRACTED


def _partial(out: str) -> str:
    return out.split("Partial response before the interruption:\n", 1)[1].split(
        "\n[END INTERRUPTED TURN]", 1
    )[0]


def test_a_steer_after_a_long_stream_survives_the_cap():
    # The cap bounds the streamed assistant text. A redirect sent after the turn
    # streamed past it is the line the model most needs and must not be cut.
    rows = [
        {"role": "user", "content": "summarise the incident"},
        {"role": "assistant", "content": "x" * 5000},
        _steer("only the customer impact"),
        {"role": "assistant", "content": "Narrowing to impact"},
    ]

    partial = _partial(_resume(rows))

    assert partial.endswith("… [truncated]\n[Steered mid-turn] only the customer impact")
    assert partial.count("x") == 4000
    assert "Narrowing to impact" not in partial


def test_steer_lines_do_not_spend_the_assistant_budget():
    rows = [
        {"role": "user", "content": "draft the plan"},
        _steer("s" * 3000),
        {"role": "assistant", "content": "a" * 3500},
    ]

    partial = _partial(_resume(rows))

    assert partial == f"[Steered mid-turn] {'s' * 3000}\n{'a' * 3500}"


def test_without_steers_the_cap_cuts_where_it_always_did():
    # Reference: the whole assistant text joined, then cut at the cap.
    cap = 50
    shapes = [
        [60],
        [50],
        [49, 1],
        [49, 2],
        [24, 25],
        [24, 25, 3],
        [25, 24, 10],
        [10, 10, 10, 10, 10, 10],
        [3] * 20,
    ]
    for sizes in shapes:
        chunks = [chr(ord("a") + i) * n for i, n in enumerate(sizes)]
        joined = "\n".join(chunks)
        expected = joined[:cap] + "… [truncated]" if len(joined) > cap else joined
        rows = [{"role": "user", "content": "go"}] + [
            {"role": "assistant", "content": c} for c in chunks
        ]
        current = {"role": "inject", "content": "resume", "meta": {"injectKind": "recovery"}}
        rows.append(current)
        out = build_interrupted_turn_preamble(
            rows,
            current=current,
            opener_inject_kinds=TURN_OPENING_INJECT_KINDS,
            assist_cap=cap,
        )
        assert _partial(out) == expected, sizes


def test_a_steer_is_capped_like_the_request():
    rows = [
        {"role": "user", "content": "go"},
        _steer("s" * 9000),
    ]

    partial = _partial(_resume(rows))

    assert partial == f"[Steered mid-turn] {'s' * 8000}… [truncated]"
