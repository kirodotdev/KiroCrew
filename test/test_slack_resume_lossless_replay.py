"""A fresh Slack session rebuilds its thread history losslessly.

A process death is not a context overflow. When a new process picks up an
existing Slack thread, the history it replays must carry code and tool output
verbatim -- the same tail-first replay the dashboard uses -- not an LLM summary
of the middle of the thread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from test_thread_parent_context import FakeSessionManager, _make_builder

from conftest import MockSlackClient
from kiro_crew.history import ConversationLog
from kiro_crew.slack.handler import handle_message, set_allowed_users, set_owner_id

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

_THREAD = "9999.0001"


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """The test builds a ``ContextBuilder``: close its ``SkillsLoader`` (``test/conftest.py``)."""


# A JSON tool result and a code block, each long enough that any summariser or
# per-message truncation would rewrite it.
_JSON = '{"results": [' + ", ".join(f'{{"id": {i}, "ok": true}}' for i in range(40)) + "]}"
_CODE = "```python\n" + "\n".join(f"def step_{i}():\n    return {i}" for i in range(40)) + "\n```"


@pytest.mark.asyncio
async def test_slack_resume_replays_code_and_json_verbatim(tmp_path):
    set_owner_id("U001")
    set_allowed_users([{"slack_id": "U001"}])
    slack = MockSlackClient()
    sessions = cast("SessionManager", FakeSessionManager())
    builder = _make_builder(tmp_path)
    log = ConversationLog(base_dir=tmp_path / "conv")
    # Old filler pushes the thread past the compressed-history cap, so the
    # previous path hands the middle of the thread to a summariser.
    for i in range(6):
        log.append(_THREAD, "user", f"old question {i} " + "x" * 9_000)
    log.append(_THREAD, "assistant", "Here is the tool output:\n" + _JSON + "\n" + _CODE)
    log.append(_THREAD, "user", "thanks")
    log.append(_THREAD, "assistant", "you are welcome")
    builder.conversation_log = log

    await handle_message(
        slack,
        sessions,
        "C123",
        "what did the code do?",
        thread_ts=_THREAD,
        msg_ts="9999.0099",
        user_id="U001",
        context_builder=builder,
    )

    sent = sessions._provider.last_message or ""  # type: ignore[attr-defined]
    assert _JSON in sent
    assert _CODE in sent
