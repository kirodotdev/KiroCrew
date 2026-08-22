"""Channel-neutral conversation auto-titling.

After the first successful turn a channel conversation has a name only if the
user typed one; otherwise every surface that lists it shows a deterministic
fallback (a truncated first message, or the channel's own label). This module
spends one short background turn asking the model for a name instead, and it is
shared so a second channel inherits the whole shape rather than a second,
subtly-different copy of it.

What the turn is, and is not
----------------------------
* **Tool-free by construction.** Every ``EVENT_PERMISSION_REQUEST`` is rejected
  and audited (``auto_title.tool_rejected``). A naming turn has no business
  reading a file or running a command, and the prompt is built from
  conversation text the model itself produced — treat it as untrusted.
* **Bounded.** One turn, :data:`TITLE_TURN_TIMEOUT_SECS` seconds, at most
  :data:`TITLE_INPUT_CHARS` characters from each side of the exchange, and the
  result is capped at :data:`TITLE_MAX_CHARS` after redaction.
* **No model id anywhere.** The turn runs on the shared background session via
  ``llm_helpers.background_turn``, so the model is whatever that session was
  created with (``agent.role_models.background``, default ``"auto"``). Never
  pass a concrete model id here — see ``AGENTS.md`` § Model selection.
* **Serialized.** :func:`get_lock` gates the shared background session so two
  conversations titling at once do not interleave on it. The lock is taken
  OUTSIDE the session acquire, matching the ordering every other background
  caller uses.

Claiming, and why the LRU lives here
------------------------------------
:func:`try_claim` is check-and-mark in one synchronous step, so two turns racing
to title the same session produce exactly one attempt — including two turns on
two DIFFERENT channels that resolved to the same session key, which is the case a
per-channel LRU could not see. A caller claims BEFORE it fires the task; a SKIP
verdict or a transient failure calls :func:`release_claim` so the next exchange
retries, and a message arriving inside that window is intentionally skipped
rather than double-titling.

A manual rename always wins. Two guards enforce that, because they cover
different windows: the in-process one (:data:`TITLE_KIND_MANUAL` recorded on the
claim) catches a rename that lands while the naming turn is streaming, and the
persisted one — the record must still carry NO title of its own — catches a
rename made before a gateway restart, when the LRU is empty and the claim would
otherwise be taken again.

What a second channel must supply
---------------------------------
This module may not import ``kiro_crew.slack`` or ``kiro_crew.dashboard`` (the
one-way dependency invariant in ``docs/system-specs/modules/messaging.md``), so
the two channel-shaped pieces are parameters:

* ``source`` — the channel name. It labels the background turn's spend
  (``bg:{source}_auto_title``) and the SEL audits, and nothing else.
* ``set_channel_title`` — an optional awaitable given the final title, which
  renames the conversation on the platform itself (Slack's
  ``set_thread_title``). A channel with no renameable conversation omits it and
  still gets the transcript title, which is what the dashboard and history read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.llm_helpers import background_turn
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Cap on the claim tracker. Bounded so a long-running gateway serving many
#: conversations cannot grow it without limit; eviction is least-recently-marked.
TITLE_LRU_MAX = 10_000

#: Claim kinds. ``manual`` is the one that outranks a naming turn in flight.
TITLE_KIND_AUTO = "auto"
TITLE_KIND_MANUAL = "manual"

#: Per-side input budget for the prompt, and the ceiling on the stored title.
TITLE_INPUT_CHARS = 200
TITLE_MAX_CHARS = 80

#: Wall-clock budget for the naming turn. A title is worth one short turn and no
#: more: the conversation is already answered, and the user is not waiting.
TITLE_TURN_TIMEOUT_SECS = 30.0

#: The verdict the prompt asks for when the topic is not nameable yet.
TITLE_SKIP_VERDICT = "SKIP"

#: session_key -> claim kind (``None`` for an in-flight automatic claim).
_titled: "OrderedDict[str, str | None]" = OrderedDict()

#: One inner lock per event loop, resolved against the running loop on every
#: acquire (see :func:`get_lock`).
_lock = LoopBoundLock()

#: An awaitable that renames the conversation on the channel itself.
ChannelTitleSetter = Callable[[str], Awaitable[None]]


def mark_titled(session_key: str, kind: str | None = None) -> None:
    """Record *session_key* as titled (or claimed, with ``kind=None``)."""
    _titled[session_key] = kind
    _titled.move_to_end(session_key)
    if len(_titled) > TITLE_LRU_MAX:
        _titled.popitem(last=False)


def is_titled(session_key: str) -> bool:
    """Whether *session_key* already has a title or a claim on one."""
    return session_key in _titled


def titled_kind(session_key: str) -> str | None:
    """The claim kind recorded for *session_key*, or ``None``."""
    return _titled.get(session_key)


def release_claim(session_key: str) -> None:
    """Drop *session_key*'s claim so the next exchange may retry."""
    _titled.pop(session_key, None)


def try_claim(session_key: str) -> bool:
    """Claim the right to title *session_key*, returning whether we got it.

    Check and mark in ONE synchronous step, with no await between them, so two
    concurrent turns — on this channel or on another one that resolved to the
    same session — cannot both decide they are the first. The loser does nothing
    and the winner owns the claim until it succeeds or calls
    :func:`release_claim`.
    """
    if session_key in _titled:
        return False
    mark_titled(session_key)
    return True


def reset() -> None:
    """Drop every claim AND rebind the lock. For tests and gateway teardown only.

    Both halves, because a caller resetting this state is recovering from something
    that did not finish: a test that crashed mid-title leaves the claim marked and
    the lock HELD, and clearing only the claim leaves the next caller blocking on a
    permit nobody will release. ``LoopBoundLock`` rebinds per loop on its own, which
    covers a new event loop but not a leaked permit on the same one.
    """
    global _lock
    _titled.clear()
    _lock = LoopBoundLock()


def get_lock() -> LoopBoundLock:
    """Return the auto-title lock, which resolves against the running loop itself.

    A cached ``asyncio.Lock`` raises ``RuntimeError`` when acquired from a
    different loop than the one it was first used on (Python 3.10+), which an
    outer ``except Exception`` then swallows as a silently skipped title.
    ``LoopBoundLock`` is the shared fix for that class, so this holds one rather
    than hand-rolling the rebind here.
    """
    return _lock


def build_title_prompt(user_msg: str, assistant_msg: str) -> str:
    """Build the naming prompt.

    An f-string, not ``str.format``: the conversation text is interpolated in,
    and a curly brace in it (JSON, a code snippet, a template) makes ``format``
    raise ``KeyError`` and lose the title entirely.
    """
    return (
        "You are a session naming agent. Given the conversation below, decide if the topic "
        "is clear enough to name.\n\n"
        "If YES: reply with ONLY a short title (3-6 words). No quotes, no punctuation.\n"
        f"If NO (too vague, just greetings, or unclear topic): reply with exactly "
        f"{TITLE_SKIP_VERDICT}\n\n"
        f"user: {user_msg}\nassistant: {assistant_msg}"
    )


def clean_title(raw: str) -> str:
    """Reduce a model reply to a storable title, or ``""`` for no title.

    Keeps the first line only, trims quoting and trailing punctuation, drops
    angle brackets (they open a link in Slack's mrkdwn and a tag in Telegram's
    HTML, and a title is rendered as-is on both), then redacts and caps. Returns
    ``""`` for an empty reply or the SKIP verdict, which the caller treats as
    "not nameable yet" rather than as a failure.
    """
    title = raw.split("\n")[0].strip("\"'. \t")
    title = title.replace("<", "").replace(">", "")
    if not title or title.upper() == TITLE_SKIP_VERDICT:
        return ""
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    return title[:TITLE_MAX_CHARS]


def _record_is_untitled(meta: dict) -> bool:
    """Guard for the transcript write: the record must carry no title yet.

    Evaluated inside ``ConversationLog``'s cross-process lock, so it decides
    against the record as it stands at WRITE time rather than as it stood when
    the naming turn started. A record that already has a title is showing a name
    somebody chose — a manual rename, possibly from before this process started —
    and a generated one must not replace it.
    """
    return not str(meta.get("title") or "").strip()


async def _stream_title(client: Any, prompt: str, *, source: str) -> str:
    """Run *prompt* on *client*, rejecting every tool it asks for."""
    text = ""
    async for event in client.stream(prompt):
        if event.kind == EVENT_TEXT_CHUNK:
            text += event.text
        elif event.kind == EVENT_PERMISSION_REQUEST:
            sel().log_api_access(
                caller="system",
                operation="auto_title.tool_rejected",
                outcome="denied",
                source=source,
                resources=str(event.request_id),
            )
            await client.reject_tool(event.request_id)
        elif event.kind == EVENT_COMPLETE:
            break
    return text


async def maybe_auto_title(
    sessions: Any,
    conv_log: Any,
    session_key: str,
    user_text: str,
    assistant_text: str,
    *,
    source: str,
    resources: str = "",
    set_channel_title: ChannelTitleSetter | None = None,
) -> str:
    """Generate and apply a title for *session_key*, returning what was applied.

    Returns ``""`` when nothing was applied — a SKIP verdict, a manual title that
    won, or a failure — and releases the claim in the two cases where a later
    exchange should retry (SKIP, and any exception). A title that landed keeps
    the claim, so the conversation is named once.

    The caller MUST already hold the claim (:func:`try_claim`); this function
    does not take it, because the claim has to be made in the same synchronous
    step as the decision to fire the task.

    ``conv_log`` may be ``None`` (a channel with no transcript, or a restricted
    session that persists nothing), in which case only ``set_channel_title``
    runs. Every failure is swallowed: a conversation without a generated name is
    a cosmetic loss, and this runs fire-and-forget behind an already-delivered
    answer.
    """
    try:
        prompt = build_title_prompt(
            user_text[:TITLE_INPUT_CHARS], assistant_text[:TITLE_INPUT_CHARS]
        )
        # The lock stays OUTSIDE the session acquire: reversing them would take
        # the shared background session before the title lock and invert the
        # ordering every other caller uses.
        async with get_lock(), contextlib.AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                background_turn(sessions, task=f"{source}_auto_title")
            )
            raw = await asyncio.wait_for(
                _stream_title(client, prompt, source=source),
                timeout=TITLE_TURN_TIMEOUT_SECS,
            )

        title = clean_title(raw)
        if not title:
            release_claim(session_key)  # allow retry on the next exchange
            return ""

        if titled_kind(session_key) == TITLE_KIND_MANUAL:
            return ""  # a manual title was set while we were streaming

        if conv_log is not None:
            try:
                applied = await asyncio.to_thread(
                    conv_log.update_metadata_if,
                    session_key,
                    {"title": title},
                    _record_is_untitled,
                )
            except Exception:
                # Best-effort: a transcript that could not be written must not
                # cost the channel its title, and must not look like a retryable
                # failure either — the name was generated, the turn was spent.
                logger.debug(
                    "auto-title: could not persist the title for %s", session_key, exc_info=True
                )
            else:
                if not applied:
                    # The record already carries a name. Leave BOTH it and the
                    # channel alone: overwriting the channel title while the
                    # transcript keeps the user's own name would leave the two
                    # surfaces disagreeing about what this conversation is.
                    logger.debug("auto-title: %s already carries a title; leaving it", session_key)
                    return ""

        if set_channel_title is not None:
            await set_channel_title(title)

        sel().log_api_access(
            caller="system",
            operation=f"{source}.thread_auto_title",
            outcome="allowed",
            source=source,
            resources=resources or session_key,
        )
        logger.info("%s conversation auto-titled: %s → %r", source, session_key, title)
        return title
    except asyncio.TimeoutError:
        # Routine transient: the cap on the title stream. Not the masking concern
        # below -- a slow model is expected operational noise, and the released
        # claim already schedules a retry on the next exchange.
        release_claim(session_key)
        logger.debug("auto-title timed out for %s", session_key)
        return ""
    except Exception as exc:
        release_claim(session_key)  # allow retry on transient failure
        # WARNING, not debug: this runs on a fire-and-forget task, so it is the
        # only place a real defect on this path surfaces. Logged at debug it
        # masked a deterministic cross-loop RuntimeError into three separate
        # order-dependent CI flake classes.
        logger.warning(
            "auto-title failed for %s (%s)", session_key, type(exc).__name__, exc_info=True
        )
        return ""
