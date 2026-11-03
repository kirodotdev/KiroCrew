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

    async def notify_turn_prepare(_state, slot) -> None:
        prepared.append(slot.key)
        slot.project = str(tree)  # what the workspace's project move does

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
