"""A background approval card on Slack says which job asked, before and after the answer.

``_interactive_approval`` posts the Block Kit card for cron, sub-agent, task-runner and
AutoNudge approvals. The card showed the tool title, the command and a ``[cron]`` tag,
and once answered it was rewritten to ``🔐 *<title>* — ✅ Approved``: a user running
several cron jobs could not tell which one was asking, or which one they had approved.
The cron turn now passes ``requester`` (job name, id and agent); the card carries a
``Requested by …`` line while it waits, in its plain-text fallback, and after it is
answered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import _make_gw_for_llm, _make_llm_job, _run_llm_callback

from kiro_crew.llm_helpers import LLMEvent
from kiro_crew.slack.handler import _build_approval_blocks, _PendingApproval

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

REQUESTER = "cron job backfill-copilot-doc (de322632), agent doc-writer"


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
        title="Running: @local-chorus-mcp/markdown",
        tool_input='{"doc": "copilot.md"}',
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
    assert f"Requested by {REQUESTER}" in _texts(blocks)
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
    assert text == f"🔐 *Running: @local-chorus-mcp/markdown* — {status}\nRequested by {REQUESTER}"


@pytest.mark.asyncio
async def test_no_requester_leaves_the_card_as_it_was() -> None:
    gw = _gateway()

    await _ask(gw, requester="")

    _channel, blocks, fallback, _thread = gw.slack.post_blocks.await_args.args
    assert blocks == _build_approval_blocks(_event(), is_dm=True, source="cron")
    assert fallback == "🔐 [cron] Approve: Running: @local-chorus-mcp/markdown?"
    text = gw.slack.update_message.await_args.kwargs["text"]
    assert text == "🔐 *Running: @local-chorus-mcp/markdown* — ✅ Approved"


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
async def test_a_single_agent_cron_passes_its_name_id_and_agent() -> None:
    gw = _make_gw_for_llm()
    job = _make_llm_job(id="de322632", name="backfill-copilot-doc", agent_id="doc-writer")

    await _run_llm_callback(gw, job)

    gw._interactive_approval.assert_any_call(
        "cron", requester="cron job backfill-copilot-doc (de322632), agent doc-writer"
    )


@pytest.mark.asyncio
async def test_each_step_of_a_sequence_names_its_own_agent() -> None:
    gw = _make_gw_for_llm()
    job = _make_llm_job(id="s1", name="relay", agent_id="", agent_sequence=["seq-a", "seq-b"])

    await _run_llm_callback(gw, job)

    requested = [c.kwargs.get("requester") for c in gw._interactive_approval.call_args_list]
    assert requested == ["cron job relay (s1), agent seq-a", "cron job relay (s1), agent seq-b"]


@pytest.mark.asyncio
async def test_an_auto_approved_cron_builds_no_approval_callback() -> None:
    gw = _make_gw_for_llm()
    job = _make_llm_job(id="a1", name="trusted", agent_id="doc-writer", approval_mode="auto")

    await _run_llm_callback(gw, job)

    gw._interactive_approval.assert_not_called()
