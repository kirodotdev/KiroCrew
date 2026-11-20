"""``create_session`` on a per-session-state host re-enters with every keyword it was given.

On such a host (``HarnessAdapter.per_session_state``, Claude Code here)
``create_session`` hands its keywords to ``_create_session_with_harness_state``,
which prepares the session and calls ``create_session`` again. The hand-off spells
its keywords out by hand, so a keyword added to ``create_session`` is dropped there
unless it is added twice. A dropped ``member_context`` means an explicit
``member_context=`` from a caller falls back to the runtime's own value.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

from kiro_crew.acp import runtime_session_create as _create
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE


def test_the_harness_state_hand_off_forwards_every_keyword(monkeypatch):
    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CLAUDE, expect_mcp_reports=False)
    assert rt._harness.per_session_state
    # Every keyword after ``self``. ``harness_state`` stays None so the call takes
    # the hand-off; ``memory_mode`` is validated before it, so it gets a real value.
    keywords = list(inspect.signature(_create.create_session).parameters)[1:]
    keywords.remove("harness_state")
    assert "member_context" in keywords
    given = {name: object() for name in keywords}
    given["memory_mode"] = "incognito"
    hand_off = AsyncMock(return_value="handle")
    monkeypatch.setattr(rt, "_create_session_with_harness_state", hand_off)

    assert asyncio.run(rt.create_session(**given)) == "handle"

    hand_off.assert_awaited_once()
    assert hand_off.await_args.args == ()
    assert hand_off.await_args.kwargs == given
