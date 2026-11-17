"""The voice reply a finished transport turn speaks, as native delivery speaks one.

``handle_message_transport`` (``slack/transport_dispatch.py``) calls
``speak_finished_turn`` after the answer is out. ``_reply_by_voice`` decides whether
a reply is wanted: global voice, the thread's ``!voice on``, or a voice memo under
``voice_reply.auto_reply_to_voice``. What it speaks is ``delivered_text``, the answer
as Slack showed it (OPTIONS, control tags and thinking removed, redacted), into the
conversation the answer went to, which is channel root for a top-level message in a
flat DM.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.agent_sdk.drivers.acp_vocab import STOP_REASON_CANCELLED
from kiro_crew.slack.handler import _reply_by_voice

if TYPE_CHECKING:
    from kiro_crew.messaging.driver import TurnDriver
    from kiro_crew.slack.client import SlackClientOps
    from kiro_crew.slack.renderer import SlackRenderer

logger = logging.getLogger(__name__)


async def speak_finished_turn(
    slack: SlackClientOps,
    renderer: SlackRenderer,
    driver: TurnDriver,
    *,
    channel: str,
    thread_ts: str,
    user_id: str,
    session_key: str,
    accumulated: str,
    had_voice_input: bool,
    unfinished: bool,
) -> None:
    """Hand a finished turn's answer to ``_reply_by_voice``; never raise.

    An unfinished or cancelled turn has no finished answer to speak. A failure is
    logged and swallowed, like the other steps after the run, so a TTS fault never
    re-records the turn as a failure.
    """
    try:
        spoken = renderer.delivered_text
        if (
            spoken
            and not unfinished
            and getattr(driver, "last_stop_reason", "") != STOP_REASON_CANCELLED
        ):
            await _reply_by_voice(
                slack,
                channel,
                thread_ts,
                user_id,
                session_key,
                accumulated,
                spoken,
                had_voice_input,
            )
    except Exception:
        logger.warning(
            "transport_dispatch: voice reply dispatch failed session=%s",
            session_key,
            exc_info=True,
        )
