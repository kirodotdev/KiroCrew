"""Slack's thread adapter: RECORD the thread session Slack already opens.

Slack has keyed one session per thread since it shipped. ``handler.py`` and
``transport_dispatch.py`` both derive ``reply_ts = thread_ts or msg_ts`` and
``session_key = canonical_key(reply_ts)`` -- ``slack:<ts>`` -- so a top-level
message becomes its own thread-session and every reply in that thread folds onto
it. That is already "one session per anchored message". Nothing here re-keys
anything, and nothing here creates a session.

What was missing is the ANCHOR: the durable statement of which message the session
hangs off, in the one shape every surface uses
(:class:`~kiro_crew.messaging.link.ThreadAnchor`). Without it a Slack thread is
only ever a session, discoverable by its key's spelling and by nothing else, which
is exactly why the dashboard's threads and Slack's threads met nowhere.

Where the record lives, and why it is not the sidecar index
----------------------------------------------------------
The dashboard keeps ONE record of an anchor: an index beside the PARENT's
transcript. It has one hard precondition -- ``write_thread_anchor`` refuses unless
the parent's transcript exists and holds a row whose ``meta.mid`` is the anchor's
message id -- because on the dashboard an anchor is durable only through the row it
hangs off.

A Slack channel has no such row. The parent conversation is a Slack channel, and
Kiro Crew keeps no transcript for one: the transcripts it keeps are per THREAD
(``slack:<ts>``). So the index cannot hold a Slack anchor, and manufacturing a
per-channel transcript to give it somewhere to live would invent a conversation
nobody writes to.

So on Slack the anchor lives where it is already durable and already correct:

* ``_thread_anchor`` on the thread session's OWN metadata -- this surface's only
  record of the relation, which its writer also reads as an idempotency guard; and
* ``thread/opened`` in the session-kind crew log, which is the ledger for "what
  hangs off what" and is where the thread list and the summary card read from.

Both are the same shape the dashboard writes, read by the same reader. Slack keeps
the channel-side listing it already had -- a Slack thread is visible in Slack. So
each surface has exactly one record, in the only place that surface can make
durable, and there is never a second copy to keep in step.

Left alone, deliberately
------------------------
* ``slack.dm_single_session`` (default off), the 1:1 DM fold. Under it the session
  is keyed by the CHANNEL, so a reply in that DM is a layout habit rather than a
  new topic and there is no thread to anchor. :func:`resolve_anchor` answers
  ``None`` there, and it reads that from the key the dispatch actually used rather
  than from the flag, so a dashboard-linked route is excluded by the same test.
* the dashboard->Slack mirror binding (``SessionMap.get/set_slack_link``).
  An anchor is not routed through the mirror -- guardrail G3.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from kiro_crew.constants import SLACK_NAMESPACE
from kiro_crew.messaging.link import ThreadAnchor, canonical_key

logger = logging.getLogger(__name__)

#: The metadata key the anchor is recorded under, on THIS surface only. A
#: dashboard thread's relation lives in the parent conversation's anchor index,
#: which has the parent row to guard it; a Slack channel has no such row, so the
#: thread session's own metadata is the durable place left. The key is also read
#: back here as the idempotency guard, so a replayed event records the anchor once.
THREAD_ANCHOR_META = "_thread_anchor"

#: A Slack channel id: ``C`` public, ``D`` 1:1 DM, ``G``/``C`` group, ``mpim``.
#: Bounded and charset-fenced rather than matched against one shape, because
#: enterprise-grid ids are longer and a refusal here would silently drop the
#: anchor on a real workspace.
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _is_channel_id(channel: str) -> bool:
    return bool(_CHANNEL_ID_RE.match(channel or ""))


def anchor_for(channel: str, reply_ts: str) -> ThreadAnchor | None:
    """The anchor a Slack thread rooted at *reply_ts* in *channel* hangs off.

    ``reply_ts`` is the handler's own ``thread_ts or msg_ts``: the ts of the
    message the thread hangs under, which on Slack is also the thread's id. Built
    through :meth:`ThreadAnchor.from_dict` so the neutral type's bounds apply
    rather than a second set of rules here.
    """
    if not _is_channel_id(channel):
        return None
    return ThreadAnchor.from_dict(
        {"surface": SLACK_NAMESPACE, "conversation": channel, "mid": reply_ts}
    )


def is_thread_session(session_key: str, reply_ts: str) -> bool:
    """Whether *session_key* is the session of the thread rooted at *reply_ts*.

    The one test that tells an anchored thread from everything else, and it reads
    the key the dispatch ACTUALLY used rather than any flag:

    * an ordinary Slack thread keys ``slack:<reply_ts>`` -- ``True``;
    * a 1:1 DM under ``slack.dm_single_session`` keys ``slack:<channel_id>``, so
      the session spans the whole DM and there is no thread to anchor -- ``False``;
    * a message routed into a dashboard-linked session keys ``dashboard:<slot>``,
      which is that slot's conversation and not this thread's -- ``False``.

    Reading the key rather than the flag is what keeps the two exclusions from
    drifting apart: either one changes the key, and this sees it.
    """
    return bool(session_key) and bool(reply_ts) and session_key == canonical_key(reply_ts)


# ── Recording, at the dispatch site ──────────────────────────────────────────


def record_anchor(
    *,
    conversation_log: Any | None,
    session_key: str,
    channel: str,
    reply_ts: str,
    client: Any = None,
    title: str = "",
    agent: str | None = None,
) -> ThreadAnchor | None:
    """Record the Slack thread session *session_key* as anchored to its root message.

    Called from the dispatch, right where it has just learnt the session is new --
    so once per session in the ordinary case, and again on a dispatch that finds it
    new after a restart, which the idempotency below is for. Returns the anchor it
    recorded, or ``None`` when there was nothing to record -- the ordinary answer
    for a folded DM, a dashboard-linked route, or an unrecognised channel id.

    Idempotent: a session that already carries an anchor is left exactly as it is
    and no second ``thread/opened`` is written. That matters because "is this
    session new" is the dispatch's answer, not this function's -- a process
    restart makes a live session new again while its transcript and its metadata
    are still on disk.

    Never raises. An anchor is a record ABOUT a turn, and a turn that answered the
    user must not fail because its bookkeeping did.
    """
    try:
        if conversation_log is None:
            return None
        if not is_thread_session(session_key, reply_ts):
            return None
        anchor = anchor_for(channel, reply_ts)
        if anchor is None:
            return None
        metadata = conversation_log.get_metadata(session_key)
        if ThreadAnchor.from_dict(metadata.get(THREAD_ANCHOR_META)) is not None:
            return None
        fields: dict[str, Any] = {THREAD_ANCHOR_META: anchor.to_dict()}
        # This write may be the one that CREATES the transcript, because it lands
        # where the dispatch has just learnt the session is new -- before the
        # turn's own first row. Whichever write creates the file is the one whose
        # metadata header records the agent, so an anchor write that arrived first
        # and carried no agent would leave the session's agent unrecorded for
        # good. Only filled when the header has none: a session that already
        # names its agent keeps it.
        if agent and not metadata.get("agent"):
            fields["agent"] = agent
        conversation_log.update_metadata(session_key, fields)
    except Exception:
        logger.debug("slack thread anchor could not be recorded", exc_info=True)
        return None
    _emit_opened(client, anchor, session_key, title)
    return anchor


def _emit_opened(client: Any, anchor: ThreadAnchor, thread_slot: str, title: str) -> None:
    """Write ``thread/opened`` for a Slack thread, on the thread's own log.

    The dashboard writes this entry on the PARENT conversation's log, where a
    reader asks "what hangs off this chat". A Slack channel has no log to write it
    on, so it goes on the thread's own -- which is where a reader of this thread
    asks the mirror-image question, "what does this hang off", and is the same log
    that already carries this session's turns.

    The ACP session id is resolved HERE, from the session handle the dispatch
    already has, rather than at the two call sites: a turn that failed before
    ``session/new`` has none, and an empty id is a no-op the emitter already
    understands.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        sid = crew_log_emit.session_id_of(client)
        if not sid:
            return
        crew_log_emit.on_thread_opened(
            sid,
            anchor=anchor.to_dict(),
            thread_slot=thread_slot,
            title=title,
            opened_by="user",
        )
    except Exception:
        logger.debug("thread/opened could not be recorded for a slack thread", exc_info=True)
