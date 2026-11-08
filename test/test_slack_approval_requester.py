"""A background approval card on Slack says which job asked, before and after the answer.

``_interactive_approval`` posts the Block Kit card for cron, sub-agent, task-runner and
AutoNudge approvals. The card showed the tool title, the command and a ``[cron]`` tag,
and once answered it was rewritten to ``🔐 *<title>* — ✅ Approved``: a user running
several cron jobs could not tell which one was asking, or which one they had approved.
The cron turn now passes ``requester`` (job name, id and agent); the card carries a
``From …`` line while it waits, in its plain-text fallback, and after it is
answered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import _make_gw_for_llm, _make_llm_job, _run_llm_callback

from kiro_crew.llm_helpers import LLMEvent
from kiro_crew.slack.handler import _build_approval_blocks, _PendingApproval

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

REQUESTER = "cron job de322632, agent doc-writer: backfill-weekly-doc"


def _gateway():
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.sessions.get_channel = MagicMock(return_value=None)
    gw.sessions.get_thread = MagicMock(return_value=None)
    gw.slack = MagicMock()
    gw.slack.open_dm = AsyncMock(return_value="D_OWNER")
    gw.slack.post_blocks = AsyncMock(return_value="approval_ts")
    gw.slack.update_message = AsyncMock()
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw._cfg = MagicMock()
    gw._cfg.hooks = MagicMock()
    gw._cfg.hooks.get = MagicMock(return_value=[])
    gw._approval_mode = None
    return gw


def _event() -> LLMEvent:
    return LLMEvent(
        kind="permission_request",
        request_id="req1",
        title="Running: @notes-mcp/write_doc",
        tool_input='{"doc": "weekly.md"}',
    )


def _answered(outcome: str) -> _PendingApproval:
    pending = _PendingApproval(provider=None, request_id="req1")  # type: ignore[arg-type]
    pending.future.set_result(outcome)
    return pending


async def _ask(gw, outcome: str = "approved", requester: str = REQUESTER) -> bool:
    with (
        patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
        patch("kiro_crew.slack.handler._PendingApproval", return_value=_answered(outcome)),
    ):
        approve = gw._interactive_approval("cron", requester=requester)
        return await approve(_event(), "")


def _texts(blocks: list[dict]) -> list[str]:
    return [
        element["text"]
        for block in blocks
        if block["type"] == "context"
        for element in block["elements"]
    ]


@pytest.mark.asyncio
async def test_the_waiting_card_names_the_requester() -> None:
    gw = _gateway()

    assert await _ask(gw) is True

    channel, blocks, fallback, _thread = gw.slack.post_blocks.await_args.args
    assert channel == "D_OWNER"
    assert f"From {REQUESTER}" in _texts(blocks)
    assert fallback.endswith(f"({REQUESTER})")
    # Everything the card showed before is still there, in the same order.
    assert blocks[: len(blocks) - 1] == _build_approval_blocks(_event(), is_dm=True, source="cron")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status"), [("approved", "✅ Approved"), ("rejected", "🚫 Rejected")]
)
async def test_the_answered_card_keeps_the_requester(outcome: str, status: str) -> None:
    gw = _gateway()

    await _ask(gw, outcome)

    _channel, _ts = gw.slack.update_message.await_args.args
    text = gw.slack.update_message.await_args.kwargs["text"]
    assert text == f"🔐 *Running: @notes-mcp/write_doc* — {status}\nFrom {REQUESTER}"


@pytest.mark.asyncio
async def test_no_requester_leaves_the_card_as_it_was() -> None:
    gw = _gateway()

    await _ask(gw, requester="")

    _channel, blocks, fallback, _thread = gw.slack.post_blocks.await_args.args
    assert blocks == _build_approval_blocks(_event(), is_dm=True, source="cron")
    assert fallback == "🔐 [cron] Approve: Running: @notes-mcp/write_doc?"
    text = gw.slack.update_message.await_args.kwargs["text"]
    assert text == "🔐 *Running: @notes-mcp/write_doc* — ✅ Approved"


@pytest.mark.asyncio
async def test_a_job_name_cannot_inject_slack_markup_or_leak_a_secret() -> None:
    """A job name is user-authored: Slack would parse ``<!channel>`` as a mention,
    and a token pasted into a name must not be posted."""
    gw = _gateway()
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

    await _ask(gw, requester=f"cron job <!channel> {token} (j1)")

    _channel, blocks, fallback, _thread = gw.slack.post_blocks.await_args.args
    line = _texts(blocks)[-1]
    assert "<!channel>" not in line and "&lt;!channel&gt;" in line
    # The top-level text drives notifications and block-less clients, so it is
    # an entity sink too.
    assert "<!channel>" not in fallback and "&lt;!channel&gt;" in fallback
    assert token not in line and token not in fallback
    assert token not in gw.slack.update_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_a_key_split_by_slack_emphasis_is_still_redacted() -> None:
    """Slack renders ``*...*`` away, so a key a literal scan sees as split shows intact."""
    gw = _gateway()
    suffix = "Q7R8S9T0U1V2W3X4"

    await _ask(gw, requester=f"cron job AKIA*{suffix}* (j1)")

    _channel, blocks, fallback, _thread = gw.slack.post_blocks.await_args.args
    for sink in (_texts(blocks)[-1], fallback, gw.slack.update_message.await_args.kwargs["text"]):
        assert suffix not in sink


@pytest.mark.asyncio
async def test_a_job_name_cannot_add_a_line_to_the_card() -> None:
    """A line break or control character in a job name would draw a forged line,
    such as a fake ``✅ Approved``, under the card's own text."""
    gw = _gateway()

    await _ask(gw, requester="cron job nightly\r\n✅ Approved\tby\x00owner\u2028now (j1)")

    flat = "cron job nightly ✅ Approved by owner now (j1)"
    _channel, blocks, fallback, _thread = gw.slack.post_blocks.await_args.args
    assert _texts(blocks)[-1] == f"From {flat}"
    assert fallback.endswith(f"({flat})")
    text = gw.slack.update_message.await_args.kwargs["text"]
    assert text == f"🔐 *Running: @notes-mcp/write_doc* — ✅ Approved\nFrom {flat}"


@pytest.mark.asyncio
async def test_a_single_agent_cron_passes_its_name_id_and_agent() -> None:
    gw = _make_gw_for_llm()
    job = _make_llm_job(id="de322632", name="backfill-weekly-doc", agent_id="doc-writer")

    await _run_llm_callback(gw, job)

    gw._interactive_approval.assert_any_call(
        "cron", requester="cron job de322632, agent doc-writer: backfill-weekly-doc"
    )


@pytest.mark.asyncio
async def test_each_step_of_a_sequence_names_its_own_agent() -> None:
    gw = _make_gw_for_llm()
    job = _make_llm_job(id="s1", name="relay", agent_id="", agent_sequence=["seq-a", "seq-b"])

    await _run_llm_callback(gw, job)

    requested = [c.kwargs.get("requester") for c in gw._interactive_approval.call_args_list]
    assert requested == ["cron job s1, agent seq-a: relay", "cron job s1, agent seq-b: relay"]
