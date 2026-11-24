"""Name a block of text that is not a chat transcript, such as a sub-agent's task.

``chat_title`` owns the titling call that names chat sessions; this module feeds
it text that arrives as one block rather than as a conversation. It reads that
call from ``chat_title`` each time it runs, so a test that patches the titler
there reaches this path too.
"""

from __future__ import annotations

import re

from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.state import DashboardState


async def title_for_text(state: DashboardState, text: str, *, session_key: str = "") -> str:
    """Name *text* with the same titling call a session's first title uses.

    Each paragraph of *text* goes to the model as its own line. The titler keeps
    only the first ``chat_title._TITLE_LINE_BUDGET`` characters of a line, and an
    orchestrator's task often opens with boilerplate shared by every task it
    sends, so the whole text as one line would show the model only that
    boilerplate. ``""`` means no name: the text was empty, the model answered
    SKIP or prose, or nothing usable came back. Errors from the background call
    propagate, as they do for a session title.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text)]
    messages = [{"role": "user", "content": p} for p in paragraphs if p]
    if not messages:
        return ""
    return await chat_title._generate_title_via_kiro(
        state, messages[: chat_title._TITLE_PROMPT_WINDOW], session_key=session_key
    )
