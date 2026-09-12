"""The Slack renderer for a dashboard-born turn on a Slack-linked session.

One Slack renderer serves a session with a Slack attachment whichever surface a
turn arrived from (RFC session-address-model §5.3, §5.4): the Slack transport
dispatcher drives :class:`~kiro_crew.slack.renderer.SlackRenderer` through
``TurnDriver`` for a Slack-born turn, and the dashboard turn loop drives the SAME
class from its own event sites for a dashboard-born turn on a linked session. The
link-time backfill seeds history through it too. This module is the one place the
dashboard constructs it, so the two dashboard callers cannot drift on how.

What differs from the Slack-born construction, and why:

* ``decider=None`` -- the dashboard owns approval. Its prompt is posted once, via
  ``post_linked_approval``, wired to the dashboard's own future; the renderer posts
  no approval card of its own without a decider.
* ``reactions_enabled=False`` -- there is no triggering Slack message to react to,
  and the fallback would react to the thread root.
* ``user_id=state.owner_id`` -- ``chat.startStream`` needs a recipient; with none
  it fails with ``missing_recipient_user_id`` and the renderer silently demotes to
  the non-streaming ``chat.update`` surface.
* ``uploads_allowed`` -- an incognito or temporary session ships no bytes into a
  channel every member can read, the same ceiling the transport dispatcher applies
  through ``_is_slack_restricted``.

The upload roots are the session's cwd plus the dashboard's uploads directory
(the renderer adds the latter itself); a caller with no absolute cwd leaves
uploads off, exactly as an unauthorized Slack-born turn does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack.renderer import SlackRenderer

logger = logging.getLogger(__name__)


async def open_slack_mirror(
    state: Any,
    slot: Any,
    session_key: str,
    channel: str,
    thread_ts: str,
    *,
    cwd: str | None,
) -> SlackRenderer:
    """Build the renderer for *slot*'s linked thread; see the module docstring.

    *cwd* is the session's resolved working directory when the caller has one
    (the live provider's ``cwd`` during a turn, the slot's project at link time).
    Anything that is not an absolute string leaves uploads off.

    Async because ``slack.show_thinking`` comes from ``KiroCrewConfig.load()``,
    which stats (and on a cache miss reads and validates) the config files: that
    is disk work, and this runs on the gateway's one loop for every linked
    turn, so it goes to a thread like every other config load on a turn path.
    """
    show_thinking = True
    try:
        show_thinking = bool((await asyncio.to_thread(KiroCrewConfig.load)).slack.show_thinking)
    except Exception:
        logger.debug("slack.show_thinking unreadable; defaulting to on", exc_info=True)
    renderer = SlackRenderer(
        state.slack_client,
        channel,
        thread_ts,
        reactions_enabled=False,
        show_thinking=show_thinking,
        decider=None,
        user_id=str(getattr(state, "owner_id", "") or ""),
        uploads_allowed=not bool(getattr(slot, "is_restricted", False)),
        session_key=session_key,
    )
    if isinstance(cwd, str) and cwd:
        renderer.authorize_upload_root(cwd)
    return renderer
