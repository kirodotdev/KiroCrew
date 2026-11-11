"""A dashboard send whose words are already in the linked channel conversation.

A channel challenge-and-redirect link carries the person's channel message as its
prompt. Opening it, the dashboard creates or reconnects the session, links it to
the thread and sends that prompt (``useSignedLinkAnchor``). The words are already
in the thread, right above where the mirror would quote them, so the mirror's echo
(``recipient._prompt_typed_in_dashboard``) must skip that turn.

The send says so in its row meta: ``meta.origin == CHANNEL_CHALLENGE_ORIGIN``, the
key the widget prefill already writes ``widget`` to. The claim can only REMOVE an
echo. It grants nothing and changes no other stamp, so a sender who puts it on
another message only loses that message's quote in the thread.

The mark follows the send down every path ``POST /api/chat`` can take it:

* an immediate turn gets ``_prompt_on_channel`` (:func:`turn_kwargs`);
* a queued or held entry carries :data:`PROMPT_ON_CHANNEL_META_KEY` in its meta
  (:func:`entry_meta`, :func:`stamp_queued`), which survives a restart with the
  queue, and the drain hands it back to the turn (:func:`batch_prompt_on_channel`).
  An edit that changes the entry's words drops the mark, since the new words are
  the editor's and are not in the thread;
* a recovery replay needs no mark: the echo is never owed for this turn, so the
  replay is not stamped as owing one (``recipient._mirror_echo_owed``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: ``meta.origin`` of the prompt a channel challenge link sends.
CHANNEL_CHALLENGE_ORIGIN = "channel_challenge"

#: Queue-entry meta: the entry's words are already in the linked conversation.
PROMPT_ON_CHANNEL_META_KEY = "promptOnChannel"


def claims_prompt_on_channel(body: Any) -> bool:
    """Whether a ``POST /api/chat`` body says its prompt is already on the channel."""
    meta = body.get("meta") if isinstance(body, dict) else None
    return isinstance(meta, dict) and meta.get("origin") == CHANNEL_CHALLENGE_ORIGIN


def turn_kwargs(body: Any) -> dict[str, bool]:
    """The ``_run_chat`` keyword an immediate send of *body* adds; empty for any other send."""
    return {"_prompt_on_channel": True} if claims_prompt_on_channel(body) else {}


def entry_meta(body: Any) -> dict[str, bool]:
    """The queue-entry meta a held send of *body* adds; empty for any other send."""
    return {PROMPT_ON_CHANNEL_META_KEY: True} if claims_prompt_on_channel(body) else {}


def stamp_queued(owner: Any, queue_id: str, body: Any) -> None:
    """Mark the queued entry *queue_id* when *body* claims its prompt is on the channel.

    Called right after the append that returned the id, with nothing suspending
    between the two, so the entry is still in *owner*'s live queue.
    """
    if not claims_prompt_on_channel(body):
        return
    for item in getattr(owner, "_queue", None) or ():
        if isinstance(item, dict) and item.get("id") == queue_id:
            meta = item.get("meta")
            if not isinstance(meta, dict):
                meta = item["meta"] = {}
            meta[PROMPT_ON_CHANNEL_META_KEY] = True
            return


def entry_prompt_on_channel(item: Any) -> bool:
    """Whether the queue entry *item* carries the mark."""
    meta = item.get("meta") if isinstance(item, dict) else None
    return isinstance(meta, dict) and meta.get(PROMPT_ON_CHANNEL_META_KEY) is True


def batch_prompt_on_channel(consumed: Iterable[Any]) -> bool:
    """Whether the consumed entries are marked, so the turn's echo skips.

    A merge run never mixes marked and unmarked entries (``_dequeue_next_message``
    stops at the change), so a typed message is never folded into a skipped echo.
    """
    return any(entry_prompt_on_channel(item) for item in consumed)
