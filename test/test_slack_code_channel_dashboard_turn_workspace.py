"""A dashboard turn in a Slack code channel's session starts in the channel's workspace.

Admission reads the session's project for the runtime it starts before the
code-channel turn-start hooks run, so it fires the preparation hooks first
(``code_channels.binding.notify_turn_prepare``), where the workspace moves the
session into the channel's worktree. Checked on a real turn
through ``turn_harness.run_turn``: the session is created in the directory the
preparation moved the slot to.
"""

from __future__ import annotations

import pytest
from turn_harness import SlotSpec, TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

pytestmark = pytest.mark.asyncio


async def test_the_turns_session_starts_in_the_directory_preparation_moved_it_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from kiro_crew.slack.code_channels import binding as binding_mod

    tree = tmp_path / "channel-worktree"
    tree.mkdir()
    prepared: list[str] = []

    async def notify_turn_prepare(_state, slot) -> bool:
        prepared.append(slot.key)
        slot.project = str(tree)  # what the workspace's project move does
        return True

    monkeypatch.setattr(binding_mod, "notify_turn_prepare", notify_turn_prepare)

    def _arrange(ctx: TurnContext) -> None:
        ctx.slot.project = str(tmp_path)  # the shared checkout, before preparation

    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            message="add a regression test",
            setup=_arrange,
        ),
        slot=SlotSpec(key="chat-code-1"),
    )
    assert prepared == ["chat-code-1"]
    [created] = record.allocations
    assert created.kwargs.get("cwd") == str(tree)


async def test_a_turn_whose_workspace_is_not_ready_is_not_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The worktree should exist and does not (a git error): no session is
    acquired, so nothing runs in the shared checkout, and the row says why."""
    from kiro_crew.slack.code_channels import binding as binding_mod

    async def notify_turn_prepare(_state, _slot) -> bool:
        return False

    monkeypatch.setattr(binding_mod, "notify_turn_prepare", notify_turn_prepare)
    from kiro_crew.dashboard import chat_runner

    told: list[str] = []

    async def deliver(state, slot, sessions, session_key, message) -> None:
        told.append(message)

    # Through the runner: an owner's function reads the runner's namespace.
    monkeypatch.setattr(chat_runner, "_deliver_linked_slack_message", deliver)
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            message="add a regression test",
        ),
        slot=SlotSpec(key="chat-code-2"),
    )
    assert record.allocations == []
    [row] = [r for r in record.window if "worktree is not ready" in str(r.get("content"))]
    # Tokened, so every dashboard locale shows its own copy of the row.
    from kiro_crew.dashboard import gateway_notice as gn

    assert (row.get("meta") or {}).get(gn.GATEWAY_NOTICE_META_KEY) == (
        gn.GATEWAY_NOTICE_CODE_CHANNEL_WORKTREE_NOT_READY
    )
    # The channel is told too: a message typed there would otherwise vanish.
    assert told == [row["content"]]
