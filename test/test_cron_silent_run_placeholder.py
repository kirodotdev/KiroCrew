"""A silent cron that sent via ``send_message`` must not read as a dead run.

A silent job is told to reply with nothing and deliver through ``send_message``,
so its turn ends with empty prose by design -- the same shape as a turn that
died before its first tool call. The gate tally counts approved tool calls and
sees each tool's title; these tests pin that the empty-reply text reads from it,
so a run that attempted a ``send_message`` delivery, a run that ran tools without
one, and a run that approved no tool each render distinctly, and only the last
reads ``_No response._``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.slack.gateway import _GateTally

GateScript = list[tuple[str, bool, bool]]

# The two wire spellings a send_message call arrives under.
SEND_KIRO_CLI = ("Running: @kirocrew-core/send_message", True, False)
SEND_ACP = ("mcp__kirocrew-core__send_message", True, False)
READ = ("Read README.md", True, False)
BLOCKED = ("rm -rf /", False, True)


def _run_silent_cron(gate: GateScript, reply: str = "", runs: int = 1) -> CronJob:
    """Drive the real ``_cron_callback`` *runs* times on a silent job.

    Each turn replays *gate* and returns *reply* as the model's prose -- empty
    by default, which is the shape a silent job's turn actually has.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.ctx_builder = MagicMock()
    gw.slack = MagicMock()
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.cancel_current = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw._interactive_approval = MagicMock(return_value="interactive_cb")

    async def fake_stream(client, msg, **kwargs):
        report: Callable[[str, bool, bool], None] | None = kwargs.get("on_tool_gate")
        assert report is not None, "the cron path must observe its tool-gate decisions"
        for title, approved, blocked in gate:
            report(title, approved, blocked)
        return reply

    job = CronJob(
        id="s1",
        name="silent-digest",
        message="go",
        schedule=CronSchedule(kind="every", every_secs=900),
        approval_mode="auto",
        silent=True,
    )

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
            for _ in range(runs):
                await captured_cb(job)

        asyncio.run(_init_and_run())

    return job


# ── The placeholder itself ──────────────────────────────────────────────────


def test_tally_names_a_send_message_attempt_for_kiro_cli_titles() -> None:
    tally = _GateTally()
    tally.note(*READ)
    tally.note(*SEND_KIRO_CLI)

    text = tally.empty_reply_placeholder()

    assert "delivery attempted via send_message" in text
    assert "2 tool calls ran" in text
    assert text != "_No response._"


def test_tally_names_a_send_message_attempt_for_acp_titles() -> None:
    tally = _GateTally()
    tally.note(*SEND_ACP)

    text = tally.empty_reply_placeholder()

    assert "delivery attempted via send_message" in text
    assert "1 tool call ran" in text


def test_tally_names_work_without_delivery() -> None:
    """Tools ran but nothing was sent: say so, and do not claim a delivery."""
    tally = _GateTally()
    tally.note(*READ)

    text = tally.empty_reply_placeholder()

    assert "1 tool call ran" in text
    assert "send_message" not in text
    assert text != "_No response._"


def test_tally_keeps_the_bare_placeholder_for_a_turn_that_did_nothing() -> None:
    """No approved tool call is the dead-run shape -- the old text is correct there."""
    assert _GateTally().empty_reply_placeholder() == "_No response._"

    blocked_only = _GateTally()
    blocked_only.note(*BLOCKED)
    assert blocked_only.empty_reply_placeholder() == "_No response._"


def test_a_refused_send_message_is_not_a_delivery() -> None:
    tally = _GateTally()
    tally.note("Running: @kirocrew-core/send_message", False, True)

    assert tally.delivered is False
    assert tally.empty_reply_placeholder() == "_No response._"


def test_a_tool_whose_name_merely_ends_in_send_message_is_not_a_delivery() -> None:
    """Match the bare tool name, not a suffix: a foreign tool must not be mistaken
    for the delivery channel."""
    tally = _GateTally()
    tally.note("Running: @other-mcp/bulk_send_message", True, False)
    tally.note("mcp__other-mcp__resend_message", True, False)

    assert tally.delivered is False
    assert "send_message" not in tally.empty_reply_placeholder()
    assert "2 tool calls ran" in tally.empty_reply_placeholder()


# ── Through the real cron path ──────────────────────────────────────────────


def test_a_silent_run_that_sent_records_the_attempt() -> None:
    job = _run_silent_cron([SEND_KIRO_CLI])

    assert job.last_result is not None
    assert "delivery attempted via send_message" in job.last_result
    assert "_No response._" not in job.last_result
    assert job.last_status != "error"


def test_a_silent_run_that_died_before_any_tool_still_reads_no_response() -> None:
    """The dead-run signature must survive: it is the only signal that shape has."""
    job = _run_silent_cron([])

    assert job.last_result == "_No response._"


def test_model_prose_on_a_silent_job_is_left_alone() -> None:
    """A silent job that DOES reply keeps its reply; the placeholder is for empty prose only."""
    job = _run_silent_cron([SEND_KIRO_CLI], reply="Digest sent.")

    assert job.last_result is not None
    assert job.last_result.startswith("Digest sent.")
