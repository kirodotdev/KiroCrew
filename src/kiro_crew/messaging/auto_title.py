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
  pass a concrete model id here — see
  ``docs/system-specs/common/model-selection.md``.
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
from typing import Any, NamedTuple

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


def _record_identity(meta: dict) -> str:
    """The stamp that makes a record THIS record rather than a later one.

    ``created_at`` is minted in the one branch of ``_update_metadata_locked``
    that has no metadata line to merge into, and every later write merges fields
    over the line it reads, so the value survives an ordinary metadata write and
    changes only when the record is minted again. That is the distinction this
    module needs.

    File identity would not serve: the metadata write lands through a temporary
    file and a rename, so the inode changes on every ordinary write and cannot
    tell a replacement apart from a neighbour writing the record we are titling.
    """
    return str(meta.get("created_at") or "")


#: The record's state as a naming turn needs it. Three values, kept distinct on
#: purpose: PRESENT carries the creation stamp, ABSENT means there is no record
#: to write into, and UNKNOWN means the read did not answer -- which is evidence
#: of neither of the other two.
RECORD_PRESENT = "present"
RECORD_ABSENT = "absent"
RECORD_UNKNOWN = "unknown"


async def _read_record_state(conv_log: Any, session_key: str) -> tuple[str, str]:
    """Return the record's state, and its creation stamp when it has one.

    One read, answering completely, because every way of collapsing these three
    states into a single value has produced a defect: an absent record reading as
    an untitled one, a record with no stamp reading as an unpinnable one, and an
    unreadable record reading as a deleted one.

    ``get_metadata`` cannot express the third at all -- it returns only the dict
    from ``_read_metadata_status`` and drops the readable flag beside it, so a
    damaged first line and a deleted session are the same empty dict and neither
    raises. ``get_metadata_status`` keeps both halves, which is why it is the one
    read here.
    """
    try:
        meta, readable = await asyncio.to_thread(conv_log.get_metadata_status, session_key)
    except Exception:
        logger.debug("auto-title: could not read the record for %s", session_key, exc_info=True)
        return RECORD_UNKNOWN, ""
    if not readable:
        return RECORD_UNKNOWN, ""
    if not meta:
        return RECORD_ABSENT, ""
    return RECORD_PRESENT, _record_identity(meta)


class RecordPin(NamedTuple):
    """What the record WAS when a naming turn was authorised.

    The two halves travel together because either alone is misleading: a stamp
    without its state cannot say whether there was a record, and a state without
    its stamp cannot tell the record apart from a replacement.
    """

    state: str
    identity: str


async def pin_record(conv_log: Any, session_key: str) -> RecordPin:
    """Pin the record a naming turn is about to be started for.

    Callers MUST call this BEFORE scheduling the turn, and a caller that holds a
    per-session permit MUST call it before releasing that permit -- not merely
    adjacent to :func:`try_claim`. Reading it inside the scheduled task leaves one
    event-loop scheduling tick between the claim and the pin; reading it after the
    permit is released leaves a far wider one, because a queued turn takes the
    permit and can delete and re-mint the record while the released turn is still
    finishing its I/O. A session key is derived from the thread rather than from
    the record, so a deletion and a re-message landing in either window pin the
    REPLACEMENT -- after which the guard matches and the replacement receives the
    deleted conversation's title. Holding the permit is what makes the read
    exclusive; being next to the claim is not.

    ``maybe_auto_title`` therefore takes the pin as a required argument and does
    not read it itself. There is deliberately no default: a default would let a
    call site added later inherit the window silently, which is the shape of the
    defect this closes.

    A ``conv_log`` of ``None`` has no record to pin -- a channel with no
    transcript, or a restricted session that persists nothing -- and pins UNKNOWN.
    """
    if conv_log is None:
        return RecordPin(RECORD_UNKNOWN, "")
    state, identity = await _read_record_state(conv_log, session_key)
    return RecordPin(state, identity)


def _untitled_and_still_ours(state: str, identity: str) -> Callable[[dict], bool]:
    """Guard: untitled, AND still the record this naming turn was started for.

    Existence alone is too weak. A channel session key is derived from the
    thread, so deleting a conversation and messaging that thread again mints a
    NEW record under the SAME key, and a turn that began before the deletion
    would write its title -- derived from the conversation that was deleted --
    onto the replacement. Pinning the stamp refuses that, and because the guard
    is evaluated inside the write's own lock, neither the deletion nor the
    replacement can land between the decision and the write.

    A record with no stamp is still PRESENT, and pinning it on having no stamp is
    as firm as a stamp: every path that mints a metadata line stamps it from a
    clock, so a replacement always acquires one.

    ABSENT and UNKNOWN both REFUSE. Neither can be pinned, and the key outlives
    the record, so a record that appears during the turn is not necessarily the
    conversation the title was generated from -- a deletion landing between the
    claim and this read leaves ABSENT, and the replacement that follows reads as
    an ordinary untitled record. The cost is the first naming turn on a
    conversation whose record has not landed yet: the claim is released instead,
    so the next exchange names it once there is a record to pin.
    """

    def guard(meta: dict) -> bool:
        if not _record_is_untitled(meta):
            return False
        if state != RECORD_PRESENT:
            return False
        # ``bool(meta)`` rather than leaning on the store's ``require_existing``
        # firing first: a guard that is only correct because something upstream
        # refuses is a guard one refactor away from being wrong.
        return bool(meta) and _record_identity(meta) == identity

    return guard


async def _key_no_longer_names_our_record(
    conv_log: Any, session_key: str, state: str, identity: str
) -> bool:
    """Whether *session_key* has stopped naming the record the turn was for.

    Read after a refusal, because the refusals are not interchangeable. A record
    that already carries a name must KEEP the claim -- releasing it would spend
    another naming turn on a conversation somebody has already named. A record
    that is gone, or replaced, must release it: the claim lives in a process-wide
    LRU, so holding it silences auto-titling for whatever takes the key next, for
    as long as this process runs.

    A turn that could not pin a record at all releases the claim, and does so
    WITHOUT reading the store: there is no record of ours to preserve the claim
    for, so no reading can change the answer. That is also why this case is
    decided first. The UNKNOWN rule below is about OUR record, and we only have
    one when the pre-read pinned it -- deciding UNKNOWN first let a damaged
    recheck retain a claim nobody owned, which silenced auto-titling for whatever
    took the key next and undid the release that makes refusing ABSENT
    affordable.

    UNKNOWN keeps the claim, and that is the whole reason the state is named
    rather than inferred from an empty dict: an unreadable record would otherwise
    read as a deleted one, releasing the claim and billing a fresh naming turn on
    every following exchange for as long as the record stays damaged.
    """
    if state != RECORD_PRESENT:
        return True
    now_state, now_identity = await _read_record_state(conv_log, session_key)
    if now_state == RECORD_UNKNOWN:
        return False
    return now_state == RECORD_ABSENT or now_identity != identity


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
    pin: RecordPin,
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

    ``pin`` is REQUIRED and comes from :func:`pin_record`, which the caller must
    call before scheduling this turn; see that function for why reading it here
    would reopen a window.

    ``conv_log`` may be ``None`` (a channel with no transcript, or a restricted
    session that persists nothing), in which case only ``set_channel_title``
    runs. Every failure is swallowed: a conversation without a generated name is
    a cosmetic loss, and this runs fire-and-forget behind an already-delivered
    answer.
    """
    try:
        # The pin came from the CALLER, captured adjacent to ``try_claim`` before
        # this turn was scheduled. Reading it here instead would leave one
        # event-loop scheduling tick between the claim and the pin, and that tick
        # is enough for a deletion plus a re-message on the same thread to pin the
        # replacement -- see ``pin_record``.
        state, identity = pin
        if conv_log is not None and state != RECORD_PRESENT:
            # Decided BEFORE the model turn, because the verdict is already known:
            # a record that was not there to pin cannot be written no matter what
            # the turn produces, so streaming a title first would spend a whole
            # background turn on an answer that is discarded either way.
            if state == RECORD_ABSENT:
                # Nothing to pin, so the next exchange may name it -- which is what
                # makes refusing here affordable. The claim is process-wide, so
                # keeping it would silence naming for whatever takes this key next.
                release_claim(session_key)
            # An UNKNOWN state keeps the claim: releasing on a record that merely
            # could not be read would bill a fresh naming turn on every following
            # exchange for as long as it stays damaged.
            logger.debug("auto-title: %s has no record to pin (%s)", session_key, state)
            return ""

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
                    _untitled_and_still_ours(state, identity),
                    # A whole LLM turn separates the decision to name this
                    # conversation from this write, so the session can be deleted
                    # inside that window -- and, because a channel session key is
                    # derived from the thread, messaging that thread again mints a
                    # replacement under the same key. The guard cannot see either
                    # for itself: an ABSENT record reaches it as the same empty
                    # dict an untitled one does, and a replacement reaches it as
                    # an untitled record. The store refuses absence inside the
                    # write's own lock; the guard, evaluated in that same lock,
                    # refuses a record that is not the one the turn was for.
                    require_existing=True,
                )
            except Exception:
                # Best-effort, and deliberately not covered by the guard above: a
                # transcript that could not be written does not cost the channel
                # its title, and must not look retryable either -- the name was
                # generated and the turn was spent. The guard's scope is the
                # durable record, so this path falls through to the channel.
                logger.debug(
                    "auto-title: could not persist the title for %s", session_key, exc_info=True
                )
            else:
                if not applied:
                    # Three ways in: the record already carries a name, the
                    # session was deleted during the naming turn, or the key now
                    # names a replacement. The store refused the durable write in
                    # each of them, so there is nothing to announce and the
                    # function returns before the channel: overwriting a channel
                    # title while the transcript keeps another name would leave
                    # the two surfaces disagreeing about what this conversation
                    # is.
                    if await _key_no_longer_names_our_record(
                        conv_log, session_key, state, identity
                    ):
                        # The claim names something that is not there any more, or
                        # was never pinned at all, and the claim is process-wide,
                        # so keeping it would silence auto-titling for whatever
                        # takes this key next until the gateway restarts.
                        # Releasing it costs one more naming turn at most.
                        release_claim(session_key)
                    logger.debug(
                        "auto-title: %s declined the title (already named, gone, or replaced)",
                        session_key,
                    )
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
