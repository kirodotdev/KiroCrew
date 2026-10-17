"""An empty reply's tool-call count must not defeat duplicate suppression.

``_GateTally.empty_reply_placeholder`` puts the approved tool-call count into the
text a no-prose turn delivers ("Completed with no reply text -- N tool calls
ran"). Duplicate suppression hashes the delivered text, and the count moves from
run to run even when the job did the same thing, so a key taken over the
displayed placeholder re-posts a non-silent job that keeps returning no prose
every time the count moves, where the constant ``_No response._`` hashes
identically and is suppressed.

These tests pin the contract: the count stays in what the user reads, the dedup
hash is taken over a count-free twin, the three placeholder SHAPES (a delivery
attempt, work without one, a turn that approved no tool) still hash apart, the
annotations wrapped around the placeholder still count, and model prose keeps
every number it carries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.slack.gateway import _GateTally, _result_hash

GateScript = list[tuple[str, bool, bool]]

READ = ("Read README.md", True, False)
SEND = ("Running: @kirocrew-core/send_message", True, False)
BLOCKED = ("echo hi", False, True)

# The display strings, on bytes: the dedup key is derived from them and must
# not move what the user sees.
WORK_ONE = "_Completed with no reply text -- 1 tool call ran._"
WORK_TWO = "_Completed with no reply text -- 2 tool calls ran._"
SENT_ONE = "_Silent run completed -- delivery attempted via send_message (1 tool call ran)._"
SENT_TWO = "_Silent run completed -- delivery attempted via send_message (2 tool calls ran)._"
NO_RESPONSE = "_No response._"


def _tally(*script: tuple[str, bool, bool]) -> _GateTally:
    tally = _GateTally()
    for step in script:
        tally.note(*step)
    return tally


# ── The dedup twin on the tally ─────────────────────────────────────────────


def test_the_display_placeholder_is_byte_identical_to_before() -> None:
    assert _tally(READ).empty_reply_placeholder() == WORK_ONE
    assert _tally(READ, READ).empty_reply_placeholder() == WORK_TWO
    assert _tally(SEND).empty_reply_placeholder() == SENT_ONE
    assert _tally(READ, SEND).empty_reply_placeholder() == SENT_TWO
    assert _tally().empty_reply_placeholder() == NO_RESPONSE
    assert _tally(BLOCKED).empty_reply_placeholder() == NO_RESPONSE


def test_the_dedup_twin_does_not_move_with_the_count() -> None:
    """Same shape, different count: the display differs, the dedup key does not."""
    one, two = _tally(READ), _tally(READ, READ)
    assert one.empty_reply_placeholder() != two.empty_reply_placeholder()
    assert one.empty_reply_dedup_text() == two.empty_reply_dedup_text()

    sent_one, sent_two = _tally(SEND), _tally(READ, SEND)
    assert sent_one.empty_reply_placeholder() != sent_two.empty_reply_placeholder()
    assert sent_one.empty_reply_dedup_text() == sent_two.empty_reply_dedup_text()


def test_the_dedup_twin_keeps_the_three_shapes_apart() -> None:
    """A delivery attempt, work without one and a dead turn are different results."""
    keys = {
        _tally().empty_reply_dedup_text(),
        _tally(READ).empty_reply_dedup_text(),
        _tally(SEND).empty_reply_dedup_text(),
    }
    assert len(keys) == 3
    # The dead-run shape has no count to drop: its key IS the old constant.
    assert _tally().empty_reply_dedup_text() == NO_RESPONSE
    assert _tally(BLOCKED).empty_reply_dedup_text() == NO_RESPONSE


def test_the_dedup_twin_differs_from_the_display_only_in_the_count_slot() -> None:
    """One template serves both, so the twin cannot drift from the display."""
    work = _tally(READ)
    assert work.empty_reply_placeholder().replace("1 tool call ran", "N tool calls ran") == (
        work.empty_reply_dedup_text()
    )
    sent = _tally(READ, SEND)
    assert sent.empty_reply_placeholder().replace("2 tool calls ran", "N tool calls ran") == (
        sent.empty_reply_dedup_text()
    )


# ── _dedup_text: the swap inside the delivered text ─────────────────────────


def test_dedup_text_swaps_the_placeholder_and_keeps_the_annotations() -> None:
    from kiro_crew.slack.gateway import _dedup_text

    tally = _tally(READ, READ)
    prefix = "⚠️ Model 'x' unavailable; ran with default.\n\n"
    delivered = prefix + tally.empty_reply_placeholder()

    key = _dedup_text(delivered, tally, empty_reply=True)

    assert key == prefix + tally.empty_reply_dedup_text()
    assert "2 tool calls ran" not in key
    # The annotation is part of the result: with and without it hash apart.
    assert _result_hash(key) != _result_hash(tally.empty_reply_dedup_text())


def test_dedup_text_is_the_delivered_text_when_the_model_wrote_prose() -> None:
    """Numbers in model prose are the result; only the placeholder's count is volatile.

    The tally is built so its own placeholder IS the prose in the last case:
    the flag, not a text match, decides whether the swap happens.
    """
    from kiro_crew.slack.gateway import _dedup_text

    tally = _tally(READ, READ)
    assert tally.empty_reply_placeholder() == WORK_TWO
    for prose in ("3 failures", "2 tool calls ran", WORK_TWO):
        assert _dedup_text(prose, tally, empty_reply=False) == prose
    assert _result_hash("3 failures") != _result_hash("5 failures")
    assert _result_hash("3 tool calls ran") != _result_hash("5 tool calls ran")


# ── Through the real cron path ──────────────────────────────────────────────


def _make_gateway():
    """A minimal GatewayOrchestrator for the dedup path: Slack posts, bell rings.

    Mirrors ``test_cron_dedup._make_gateway``; add an attribute here if the
    orchestrator grows one the cron path reads.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.slack.post_blocks = AsyncMock(return_value="ts1")
    gw.conv_log = None
    gw.dashboard_state = MagicMock()
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.cancel_current = AsyncMock()
    gw.sessions.set_thread = AsyncMock()
    gw.sessions.set_channel = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="cb")
    return gw


def _make_job() -> CronJob:
    """A NON-silent, Slack-delivered job: the shape the issue is about."""
    return CronJob(
        id="j1",
        name="quiet-digest",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=300),
        approval_mode="auto",
        channel="C123",
    )


def _run(gw, job: CronJob, gate: GateScript, reply: str = "") -> None:
    """Drive the real ``_cron_callback`` once: replay *gate*, return *reply*."""

    async def fake_stream(client, msg, **kwargs):
        report: Callable[[str, bool, bool], None] | None = kwargs.get("on_tool_gate")
        assert report is not None, "the cron path must observe its tool-gate decisions"
        for title, approved, blocked in gate:
            report(title, approved, blocked)
        return reply

    captured_cb = None
    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream),
        patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
    ):

        def capture_cron(on_job=None, **kw):
            nonlocal captured_cb
            captured_cb = on_job
            svc = MagicMock()
            svc.start = AsyncMock()
            return svc

        mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

        async def _init_and_run():
            await gw._init_cron()
            assert captured_cb is not None
            await captured_cb(job)

        asyncio.run(_init_and_run())


def test_an_empty_reply_whose_count_moved_is_suppressed() -> None:
    """The issue's scenario: no prose on every run, one more tool call this time."""
    gw, job = _make_gateway(), _make_job()

    _run(gw, job, [READ])
    assert gw.slack.post_blocks.await_count == 1
    assert job.last_result == WORK_ONE

    _run(gw, job, [READ, READ])
    assert gw.slack.post_blocks.await_count == 1, "the second run re-posted"
    assert job.consecutive_dupes == 1
    # The count is still what the user reads; only the dedup key dropped it.
    assert job.last_result == WORK_TWO


def test_a_delivery_attempt_whose_count_moved_is_suppressed() -> None:
    gw, job = _make_gateway(), _make_job()

    _run(gw, job, [SEND])
    assert job.last_result == SENT_ONE
    _run(gw, job, [READ, SEND])
    assert job.last_result == SENT_TWO

    assert gw.slack.post_blocks.await_count == 1
    assert job.consecutive_dupes == 1


def test_a_dead_turn_is_still_suppressed_as_before() -> None:
    """The shape this issue did not break keeps the behaviour it had."""
    gw, job = _make_gateway(), _make_job()

    _run(gw, job, [])
    _run(gw, job, [])

    assert job.last_result == NO_RESPONSE
    assert gw.slack.post_blocks.await_count == 1
    assert job.consecutive_dupes == 1


def test_a_change_of_shape_is_a_new_result() -> None:
    """Dead turn -> work -> delivery attempt: each is posted, none is a dup."""
    gw, job = _make_gateway(), _make_job()

    _run(gw, job, [])
    _run(gw, job, [READ])
    _run(gw, job, [READ, SEND])

    assert gw.slack.post_blocks.await_count == 3
    assert job.consecutive_dupes == 0


def test_a_refusal_beside_an_empty_reply_is_a_new_result() -> None:
    """The partial-block banner stays in the key: a run that lost a call never
    hashes equal to one that did not, placeholder or no placeholder."""
    gw, job = _make_gateway(), _make_job()

    _run(gw, job, [READ])
    _run(gw, job, [READ, BLOCKED])

    assert gw.slack.post_blocks.await_count == 2
    assert job.consecutive_dupes == 0
    assert job.last_result is not None and job.last_result.startswith("⛔")
    assert job.last_result.endswith(WORK_ONE)


def test_model_prose_that_differs_only_in_a_number_is_not_collapsed() -> None:
    """Pins that the repair did not teach ``_result_hash`` to drop small integers."""
    gw, job = _make_gateway(), _make_job()

    _run(gw, job, [READ], reply="3 failures")
    _run(gw, job, [READ], reply="5 failures")

    assert gw.slack.post_blocks.await_count == 2
    assert job.consecutive_dupes == 0


def test_model_prose_that_echoes_the_placeholder_is_hashed_as_written() -> None:
    """Prose that happens to read like the placeholder is still prose: its count
    is the model's own, and a different count is a different result. Each run's
    tally would produce exactly this text as its placeholder, so a swap keyed on
    the text rather than on the substitution would collapse the two."""
    gw, job = _make_gateway(), _make_job()

    _run(gw, job, [READ], reply=WORK_ONE)
    _run(gw, job, [READ, READ], reply=WORK_TWO)

    assert gw.slack.post_blocks.await_count == 2
    assert job.consecutive_dupes == 0
