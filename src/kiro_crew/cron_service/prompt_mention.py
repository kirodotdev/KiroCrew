"""Expand a leading ``@prompt`` mention in a cron job's message.

A dashboard chat turn replaces ``@name rest`` with the named prompt or SOP and
appends ``rest`` (``chat_turn.prompt_assembly``). A cron fire sends the job's
message to the provider without a chat turn, so a job whose message begins
``@agent-sop:<name>`` ran the literal tag. This applies the same expansion at the
fire.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def expand_cron_prompt_mention(
    message: str, project_dir: str | None, *, job_id: str, job_name: str
) -> str:
    """Return *message* with a leading ``@name`` mention expanded, else unchanged.

    *project_dir* is the directory the run's agent session starts in, which is where
    a ``.kiro/prompts`` project prompt is looked up, as a chat turn looks in its
    slot's project. An unknown name keeps the message as written, because a message
    that begins with ``@`` for another reason ("@here ...") ran that way before. A
    prompt the sensitive-path check refuses, or one over the size limit, is logged
    and also leaves the message as written.

    The file reads run on a worker thread, as they do for a chat turn.
    """
    if not message.startswith("@"):
        return message
    from kiro_crew.dashboard.chat_turn.prompt_assembly import _resolve_prompt_mention

    expanded, status, _chip = await asyncio.to_thread(
        _resolve_prompt_mention, message, Path(project_dir) if project_dir else None
    )
    if status == "not_found":
        return message
    mention = message.split(None, 1)[0]
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=f"cron:{job_id}",
            tool_name="prompt_expansion",
            tool_kind="prompt",
            outcome=status,
            metadata={"mention": mention},
        )
    except Exception:
        logger.debug("SEL logging failed in cron prompt expansion", exc_info=True)
    if status != "ok":
        logger.warning(
            "Cron '%s': prompt %s was not expanded (%s); the message runs as written",
            job_name,
            mention,
            status,
        )
    return expanded
