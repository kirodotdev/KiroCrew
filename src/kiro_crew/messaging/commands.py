"""Channel-neutral halves of the chat commands every DM channel ships.

A channel dispatcher's command handler is two things welded together: a decision
(what the grant becomes, whether a turn was actually cancelled, how long a login
link lives, what a cron listing says) and a send (which needs a ``chat_id``, a
``channel_id``, or a ``(conversation_id, serviceUrl)`` pair). The decision half is
identical across channels and this module owns it; only the send stays behind.

Two families landed here from opposite directions and both belong: the
**stateful** commands whose decision is a side effect on shared state
(``/stop``'s cooperative cancel, ``/yolo``'s grant ladder, the dashboard-link TTL
vocabulary), and the **path-independent** keyword commands whose answer is one
string computed from a service (``spawn``, ``cron``, ``task run``). They share the
same contract, so they share the same module rather than splitting on a
distinction no caller observes.

What lives here and what does NOT:

* HERE -- the decision plus the reply TEXT, the same shape
  :func:`~kiro_crew.messaging.link.release_conversation_location` already uses for
  ``/unlink``, so the user-facing strings have exactly one owner. Each
  keyword-command function is ``(text, service) -> reply | None``, where ``None``
  means the text is not that command and normal routing continues.
* NOT here -- anything address-shaped. Nothing below accepts a chat id, a thread
  or a service URL; ``/stop`` reaches its receipt bubble through the
  already-bound :class:`~kiro_crew.messaging.queue_receipt.ReceiptSurface`, which
  is what keeps Telegram's forum routing and Teams' service URLs channel-local.
* NOT here -- the command GRAMMAR. Which token a channel spells a command with,
  and how many words precede an argument, stays in that channel's own
  ``commands.py``; the functions below take an already-extracted argument.

**Services are duck-typed on purpose.** ``kiro_crew.subagent`` and
``kiro_crew.taskrunner`` both reach ``kiro_crew.slack`` transitively, so importing
them at runtime here would reintroduce the ``messaging -> slack`` edge the
abstraction exists to remove. They are typed under ``TYPE_CHECKING`` and consumed
through the handful of attributes named at each call.

Dependency direction is ``<channel> -> messaging`` (never the reverse), and this
module additionally imports nothing from ``kiro_crew.dashboard`` -- see
:func:`parse_dashboard_ttl` for the one place that constrains a signature.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.cron import (
    CronStoreBusy,
    CronStoreUnreadable,
    compute_next_run_ts,
    format_schedule,
    get_local_tz,
)
from kiro_crew.messaging.queue_receipt import ReceiptQueue, ReceiptSurface
from kiro_crew.safety_override import describe_grant_lifetime, safety_override
from kiro_crew.security import redact
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import edge
    from kiro_crew.cron import CronService
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.taskrunner import TaskRunner

logger = logging.getLogger(__name__)


# ── /stop (hard cancel) ──────────────────────────────────────────────────────

#: Sent when the cooperative cancel reached a live turn.
STOP_REPLY_CANCELLED = "🛑 Stopped."

#: Sent when there was no live turn -- the queue is still cleared, and saying so
#: is what distinguishes "nothing to stop" from "the stop did not work".
STOP_REPLY_IDLE = "🛑 Nothing was running — queue cleared."


async def stop_running_turn(
    sessions: Any,
    session_key: str,
    *,
    queue: ReceiptQueue,
    surface: ReceiptSurface,
) -> str:
    """Abort the in-flight turn, drop the queue, finalize the receipt.

    Returns the reply text the channel should send; the send itself is the only
    address-shaped part and stays with the caller.

    **The cancel is cooperative before it is fatal.** ``cancel(wait_ack_timeout=0)``
    writes an ACP ``session/cancel`` notification and returns without waiting, so
    the acknowledgement to the user is immediate and the turn stops at its next
    safe point. On a shared runtime that is the only path that cannot take a
    co-tenant process down with it. ``cancel`` is probed with ``getattr`` because
    a provider that predates the ext-method simply does not offer one, and a
    missing cancel must degrade to "queue cleared" rather than raise at the user.

    ``clear_queue`` and the receipt finalize run under a SINGLE hold of
    :attr:`ReceiptQueue.lock`, which is the same hold the end-of-turn drain takes
    across its dequeue plus flip. Splitting them would let a drain observe a
    queue already emptied while its receipt bubble still said "⏳ Queued",
    orphaning the bubble as the durable record of a burst that was cancelled.

    A cancel that raises is logged and reported as "nothing was running": the
    queue is still cleared, so claiming a stop that did not happen would be the
    worse lie.
    """
    cancelled_turn = False
    if sessions.is_busy(session_key):
        provider = sessions.get_provider(session_key)
        cancel = getattr(provider, "cancel", None)
        if cancel is not None:
            try:
                await cancel(wait_ack_timeout=0)
                cancelled_turn = True
            except Exception:
                logger.warning(
                    "%s: stop could not cancel the running turn for %s",
                    surface.label,
                    session_key,
                    exc_info=True,
                )
    async with queue.lock:
        sessions.clear_queue(session_key)
        await queue.finish_cancelled_locked(session_key, surface)
    return STOP_REPLY_CANCELLED if cancelled_turn else STOP_REPLY_IDLE


# ── /yolo (the process-wide auto-approve grant) ──────────────────────────────

YOLO_ON = "on"
YOLO_OFF = "off"
YOLO_RENEW = "renew"

#: The verbs that MUTATE the grant. Anything else -- including a bare command and
#: a typo -- reports status instead, so an unrecognised word can never arm it.
YOLO_ACTIONS: frozenset[str] = frozenset((YOLO_ON, YOLO_OFF, YOLO_RENEW))


@dataclass(frozen=True)
class YoloPhrasing:
    """How a channel spells the ``/yolo`` command INSIDE its own reply text.

    The two spellings below are the whole per-channel difference in these
    replies, so they are data rather than a per-channel copy of the sentence: a
    channel picks a constant instead of restating the prose, which is how the
    three copies drifted in the first place.
    """

    #: How "turn it on" is written when the reply points the user at it.
    on_command: str
    #: The verb list on the usage line.
    usage: str


#: For a channel that renders no inline code (Telegram sends plain text here).
YOLO_PHRASING_PLAIN = YoloPhrasing(on_command="/yolo on", usage="/yolo on | off | renew")

#: For a channel whose messages render markdown inline code (Teams, Discord).
YOLO_PHRASING_MARKDOWN = YoloPhrasing(on_command="`/yolo on`", usage="`/yolo on` | `off` | `renew`")

#: What arming the grant actually covers, in one sentence. The grant is process-wide, so
#: a control that offers it has to say so BEFORE the press -- "auto-approve" on a button
#: inside one chat reads as scoped to that chat. Shared, so a channel's pre-press
#: affordance and the reply that confirms the grant cannot describe different scopes.
YOLO_SCOPE_NOTE = "Applies to every tool on every surface, this chat and the dashboard alike."


def parse_yolo_action(arg: str) -> str:
    """The first word of a ``/yolo`` argument, lowercased (``""`` when absent)."""
    words = arg.strip().lower().split()
    return words[0] if words else ""


async def run_yolo_command(
    arg: str,
    *,
    source: str,
    caller: str,
    phrasing: YoloPhrasing,
) -> str:
    """Report or change the process-wide auto-approve grant; return the reply.

    Reads and writes the SAME :func:`safety_override` grant the dashboard toggle
    and Slack's ``/kirocrew yolo`` drive, so a grant taken in one channel shows up
    -- and expires -- everywhere. *source* names the surface for both the grant's
    own audit and the SEL row; *caller* is the authorized identity that asked.

    Turning it on does NOT weaken the PreToolUse security gate: the
    sensitive-path keystone, the governance ceiling and the deny-list all run
    ahead of the auto-approve ladder in ``TurnDriver``, so a hard DENY still wins.

    The three mutators run off-loop: ``activate`` resolves the ad-hoc duration
    through a live config read and every one of them writes a SEL record
    (activation's is critical), so calling them inline would put filesystem
    latency on the gateway's single event loop and stall every other
    conversation and heartbeat task on a slow disk.

    The SEL row is written HERE so its shape cannot drift per channel. A caller
    that wants the outcome should read the reply rather than re-audit.
    """
    so = safety_override()
    action = parse_yolo_action(arg)

    if action not in YOLO_ACTIONS:
        status = f"ON 🟢 ({describe_grant_lifetime()})" if so.is_active() else "OFF 🔴"
        return f"YOLO is {status}.\nUsage: {phrasing.usage}"

    outcome = "allowed"
    if action == YOLO_ON:
        if so.is_active():
            reply = f"🟢 YOLO is already ON ({describe_grant_lifetime()})."
        elif (await asyncio.to_thread(so.activate, source)).active:
            reply = (
                f"🟢 YOLO ON ({describe_grant_lifetime()}) — every tool auto-approves. "
                f"{YOLO_SCOPE_NOTE} Denied-by-policy tools are still blocked."
            )
        else:
            reply = "❌ Couldn't turn YOLO on (audit system unavailable)."
            outcome = "denied"
    elif action == YOLO_OFF:
        # Unconditional: deactivate() also zeroes the deadline of a grant that
        # already lapsed, which closes the renew grace window so a later
        # "/yolo renew" cannot resurrect it, and records the operator's decision
        # either way.
        await asyncio.to_thread(so.deactivate, source)
        reply = "🔴 YOLO OFF — tools ask for approval again."
    else:
        renewed = (await asyncio.to_thread(so.renew, source)).renewed
        reply = (
            f"🟢 YOLO renewed ({describe_grant_lifetime()})."
            if renewed
            else f"🔴 YOLO is not active — use {phrasing.on_command} first."
        )
    sel().log_api_access(
        caller=caller,
        operation=f"{source}.yolo_mode",
        outcome=outcome,
        source=source,
        resources=f"yolo_{action}",
    )
    return reply


# ── dashboard login links ────────────────────────────────────────────────────

#: How long a presigned dashboard link lives when the user names no duration.
DEFAULT_DASHBOARD_TTL_SECS = 3600

#: Floor on a requested lifetime, in seconds. ``parse_duration`` accepts ``0h`` /
#: ``0m`` and answers 0 -- a real int, not ``None`` -- so without a floor it passes
#: every "did it parse" check and mints a bearer credential that is ALREADY
#: EXPIRED: a link the user cannot use, with no explanation of why. One minute is
#: the floor because it is the shortest lifetime the ``<N>h`` / ``<N>m`` grammar
#: can express, so clamping rejects exactly one input -- an explicit zero -- and
#: leaves every duration a user can type untouched.
#:
#: Clamping rather than falling back to the default is what keeps the reply
#: honest: the caller renders the GRANTED value with :func:`format_ttl`, which can
#: then never print ``0m``. Enforced HERE, in the shared parser, so it holds for
#: every channel that mints a link rather than for whichever one last had it
#: audited.
MIN_DASHBOARD_TTL_SECS = 60

#: ``"<N>h"`` / ``"<N>m"`` -> seconds, or None. Injected, see below.
DurationParser = Callable[[str], "int | None"]


def parse_dashboard_ttl(arg: str, *, parse_duration: DurationParser) -> int:
    """Resolve a dashboard-link TTL from a command's ARGUMENT string.

    *arg* is everything after the channel's own command tokens -- ``"2h"``,
    ``"30m"``, ``""`` when the user named no duration. Taking the argument rather
    than the whole message is what makes this channel-neutral: Telegram's grammar
    is ``/kirocrew dashboard <ttl>`` (the TTL is the third word) and Teams' is
    ``/dashboard <ttl>`` (the second), so a shared parser that indexed into the
    split message would be reading one channel's word count on the other's
    behalf. Only the FIRST word of *arg* is considered; anything after it is
    ignored rather than making the whole argument unparseable.

    *parse_duration* supplies the duration vocabulary and is injected rather than
    imported: it lives in ``dashboard/token_auth.py``, and ``kiro_crew.messaging``
    imports nothing from ``kiro_crew.dashboard`` -- that one-way direction is what
    keeps the transport layer independent of the surfaces built on top of it.
    Callers already import that module for ``generate_token``.

    Returns :data:`DEFAULT_DASHBOARD_TTL_SECS` when *arg* names no duration or the
    duration does not parse: a mistyped TTL should still hand the user a working
    link rather than refuse the command. A duration that parses is clamped up to
    :data:`MIN_DASHBOARD_TTL_SECS` -- see there for why an explicit zero must not
    reach the token minter.
    """
    words = arg.strip().split()
    if words:
        parsed = parse_duration(words[0].lower())
        if parsed is not None:
            return max(parsed, MIN_DASHBOARD_TTL_SECS)
    return DEFAULT_DASHBOARD_TTL_SECS


def format_ttl(ttl_secs: int) -> str:
    """Render a TTL in seconds as a human duration ("2h", 90m -> "1h 30m").

    Never truncates: a non-hour-multiple >= 1h renders both components so the
    reply reports exactly how long the login link stays live.
    """
    hours, rem = divmod(ttl_secs, 3600)
    mins = rem // 60
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def compact_unsupported_backend(provider: Any) -> str | None:
    """Backend id when *provider* cannot serve a manual ``/compact``, else ``None``.

    The channel half of the dashboard's manual-``/compact`` capability gate
    (#7800): a backend outside ``ACP_BACKENDS_COMPACT`` treats the ``/compact``
    prompt as ordinary text and never emits a compaction status, so dispatching
    it strands ``wait_for_compaction()`` for its whole deadline. The capability
    is read off the LIVE provider — ``manual_compact_unsupported_backend`` is
    declared on the ``LLMProvider`` ABC with a ``None`` (supported) default per
    harness-parity H14 — and only a non-empty ``str`` (the ABC's stated
    contract) reads as a refusal, so a mocked or duck-typed provider's truthy
    attribute never blocks a compaction.
    """
    value = getattr(provider, "manual_compact_unsupported_backend", None)
    if isinstance(value, str) and value:
        return value
    return None


def compact_unsupported_reply(backend: str) -> str:
    """Informational reply for a manual ``/compact`` on an unsupported *backend*.

    Mirrors the dashboard's wording: the backend manages compaction
    automatically (the same relationship the ``cc_managed`` decline encodes),
    so the refusal is information, never an error.
    """
    return (
        f"ℹ️ The `{backend}` backend manages compaction automatically — it "
        "summarizes the conversation on its own as context fills, so manual "
        "`/compact` isn't needed (and isn't supported) here."
    )


#: How much of a cron job's message body a list row shows.
_CRON_MESSAGE_PREVIEW_CHARS = 50
#: How much of a subagent's task a list row shows.
_SPAWN_TASK_PREVIEW_CHARS = 60
#: How much of the spawned task the confirmation echoes back.
_SPAWN_ECHO_CHARS = 100

#: The retryable answer for a store another writer currently holds. One string,
#: because a caller that sees a different wording per verb cannot tell "retry
#: this" from "this failed".
_CRON_BUSY = "⏳ Cron store busy — try again in a moment."


def _cron_unreadable(exc: CronStoreUnreadable) -> str:
    """The NON-retryable answer for a store whose last read failed.

    A helper rather than a sibling constant of :data:`_CRON_BUSY` for one
    reason: busy is one fixed sentence, but this message names the unreadable
    path and the remediation, both of which the exception already carries — so
    a constant could not hold them and restating them here would let the two
    copies drift. The "one wording per store fault, not per verb" rule
    _CRON_BUSY documents still applies, which is why the formatting lives in a
    single place instead of at each call site.

    Distinct from busy on purpose: a client that retries a contended store must
    NOT retry this one, because an unreadable file does not heal on its own.
    """
    return f"⚠️ {exc}"


def _redact(text: str) -> str:
    """Both redaction passes, over text that may be ``None``.

    Cron job names/messages and subagent tasks are free-form text a user OR the
    LLM wrote, and every caller posts the result to an external surface AND
    persists it, so the scan runs before the string leaves this module.
    """
    return redact(text or "")


# ── spawn / bg ─────────────────────────────────────────────────────────────


def spawn_command_reply(
    text: str, manager: "SubagentManager | Any", session_key: str = ""
) -> str | None:
    """Handle ``spawn <task>`` / ``bg <task>`` / ``spawn list`` / ``spawn status``.

    Reads ``manager.running`` (an iterable of records with ``id``/``started``/
    ``task``), ``manager.max_concurrent`` and ``manager.spawn(task,
    parent_session_key=)``.
    """
    stripped = text.strip()
    low = stripped.lower()
    for prefix in ("spawn ", "bg "):
        if low.startswith(prefix):
            return spawn_task_reply(stripped[len(prefix) :].strip(), manager, session_key)
    return None


#: Arguments whose reply enumerates HOST state rather than this conversation's.
#: ``spawn list``/``status`` render ``manager.running`` -- every subagent on the box
#: with its task text -- and ``task status`` reports the one global runner. Neither
#: filters on ``session_key``; that parameter only addresses a spawn's notices.
_HOST_WIDE_ARGS = frozenset({"list", "status"})


def normalize_task_arg(arg: str) -> str:
    """Absorb ``/task``'s optional leading ``run``, once, in one place.

    The keyword grammar spells the verb ``task run``, but a channel whose command IS
    ``/task`` receives ``run <spec>`` as its argument, so :func:`task_arg_reply` has
    to strip a leading ``run`` before dispatching. Anything that has to reason about
    which subcommand an argument names must strip it the SAME way or it is reasoning
    about a different string: ``run status`` reaches the ``status`` branch, and a
    scope check that did not normalize read it as a task named "status".
    """
    arg = arg.strip()
    low = arg.lower()
    if low == "run" or low.startswith("run "):
        return arg[len("run") :].strip()
    return arg


def lists_host_state(command: str, arg: str) -> bool:
    """Whether this command+argument makes the reply a HOST-wide listing.

    A channel whose reply can reach an audience wider than the caller asks this
    before answering: the same command is conversation-scoped with one argument and
    host-scoped with another, and that distinction is not visible from the command
    name. It lives here because the functions that BUILD those replies are here, so
    a channel cannot get it wrong by reading one subcommand and generalizing --
    which is exactly how ``/spawn list`` came to be treated as conversation-scoped
    because ``/spawn <task>`` is.

    *command* is taken rather than left to the caller precisely because the
    normalization differs per command: ``task`` absorbs a leading ``run``, so
    ``/task run status`` IS ``status``, and a predicate that only saw the argument
    would answer no for it. Passing the command keeps that where the normalization
    is instead of asking every channel to remember which of its commands needs it.

    Deliberately NOT every host-touching argument: ``task cancel`` acts on the
    global runner but its reply names nothing, and refusing it would stop a forum
    operator from halting a run they can see.
    """
    if command == "task":
        arg = normalize_task_arg(arg)
    return arg.strip().lower() in _HOST_WIDE_ARGS


def spawn_task_reply(
    task: str, manager: "SubagentManager | Any", session_key: str = ""
) -> str | None:
    """Handle an ALREADY-PARSED spawn argument (``list``/``status``/a task).

    Public because a channel whose command grammar carries its own prefix (a
    Telegram ``/spawn <task>``, a Discord ``!spawn <task>``) has the argument in
    hand and must not have to re-synthesize ``"spawn " + arg`` just to have it
    stripped off again.
    """
    if not task:
        return None
    if task.lower() in ("list", "status"):
        running = list(manager.running)
        if not running:
            return "No subagents running."
        now = time.time()
        lines = ["*Running subagents:*"]
        for agent in running:
            elapsed = int(now - agent.started)
            lines.append(
                f"🔹 `{agent.id}` | {elapsed}s | {_redact(agent.task)[:_SPAWN_TASK_PREVIEW_CHARS]}"
            )
        return "\n".join(lines)
    info = manager.spawn(task, parent_session_key=session_key)
    if not info:
        return f"⚠️ Subagent capacity reached ({manager.max_concurrent}). Try again later."
    return f"🚀 Spawned subagent `{info.id}`\n_{_redact(task)[:_SPAWN_ECHO_CHARS]}_"


# ── cron ───────────────────────────────────────────────────────────────────


def _relative(delta: float) -> str:
    """A cron job's next run as a coarse relative duration."""
    if delta >= 86400:
        return f"in {int(delta // 86400)}d {int((delta % 86400) // 3600)}h"
    if delta >= 3600:
        return f"in {int(delta // 3600)}h {int((delta % 3600) // 60)}m"
    if delta > 0:
        minutes = int(delta // 60)
        return f"in {minutes}m" if minutes >= 1 else "in <1m"
    return "now"


def _cron_list(cron_service: "CronService | Any") -> str:
    jobs = cron_service.list_jobs(include_disabled=True)
    if not jobs:
        return "No cron jobs scheduled."
    now = time.time()
    tz_name, _ = get_local_tz()
    lines = ["*Your cron jobs:*"]
    for job in jobs:
        status = "✅" if job.enabled else "⏸️"
        schedule = _redact(format_schedule(job.schedule, tz_name=job.timezone or tz_name))
        last = ""
        if job.last_status == "ok":
            last = " ✓"
        elif job.last_status == "error":
            last = " ❌"
        nxt = compute_next_run_ts(job, now=now)
        next_part = f" | ⏭ {_relative(nxt - now)}" if nxt is not None else ""
        message = _redact(job.message)[:_CRON_MESSAGE_PREVIEW_CHARS]
        lines.append(f"{status} `{job.id}` | `{schedule}` | {message}{last}{next_part}")
    return "\n".join(lines)


async def cron_remove_all_reply(
    cron_service: "CronService | Any", *, source: str = "", caller: str = ""
) -> str:
    """Remove every cron job (enabled or not) and summarize what went.

    Public for the same reason as :func:`spawn_task_reply`: a channel that
    parsed ``remove all`` itself should not have to rebuild the sentence.

    *source* is the channel name and *caller* the person who typed it. Both are
    forwarded to the service so the persisted mutation and its audit cannot drift.
    """
    jobs = cron_service.list_jobs(include_disabled=True)
    # Refuse an unreadable store BEFORE the "nothing to do" answer below.
    # `list_jobs` degrades a corrupt store to an EMPTY list without raising, so
    # without this the reply for a corrupt store is byte-identical to the reply
    # for an honestly empty one -- and the `except CronStoreUnreadable` on the
    # removal never runs, because with no jobs no removal is attempted. That is
    # the quiet-versus-broken conflation, reached by omission rather than by a
    # wrong branch. `getattr` because `cron_service` is duck-typed and a fake
    # need not carry the probe. The freshness this inherits is the reply's own:
    # `list_jobs` is cache-only, so the latch is as current as the last sync.
    probe = getattr(cron_service, "raise_if_store_unreadable", None)
    if callable(probe):
        try:
            probe()
        except CronStoreUnreadable as exc:
            return _cron_unreadable(exc)
    if not jobs:
        return "No cron jobs to remove."
    # ``job.name`` is free-form user/LLM text reaching a chat reply and the
    # persisted conversation log; ``job.id`` is a generated UUID and is left as-is.
    lines = [f"- `{job.id}` — {_redact(job.name)}" for job in jobs]
    # One batch lock/reload/save, offloaded by the service itself, so a chat
    # gateway's loop is never parked on the store lock.
    try:
        await cron_service.remove_jobs(
            [job.id for job in jobs],
            actor=caller or source or "system",
            source=source or "messaging",
        )
    except CronStoreBusy:
        return _CRON_BUSY
    except CronStoreUnreadable as exc:
        return _cron_unreadable(exc)
    return f"✅ Removed {len(lines)} cron job(s):\n" + "\n".join(lines)


async def cron_command_reply(
    text: str, cron_service: "CronService | Any", *, source: str = "", caller: str = ""
) -> str | None:
    """Handle ``cron list`` / ``cron remove <id>|all`` / ``cron pause|resume <id>``.

    Async because the mutators run through the store's event-loop-safe
    ``*_async`` variants; a contended store answers "busy, retry" rather than
    parking the caller's loop on the lock.

    *source* and *caller* name the channel and person for removal attribution;
    both are keyword-only with empty defaults so existing positional calls work.
    """
    parts = text.strip().lower().split()
    if len(parts) < 2 or parts[0] != "cron":
        return None
    action = parts[1]
    if action == "list":
        return _cron_list(cron_service)
    if len(parts) < 3:
        return None
    job_id = parts[2]
    if action == "remove":
        if job_id == "all":
            return await cron_remove_all_reply(cron_service, source=source, caller=caller)
        try:
            removed = await cron_service.remove_job_async(
                job_id,
                actor=caller or source or "system",
                source=source or "messaging",
            )
        except CronStoreBusy:
            # A contended store means the delete never happened, so there is no
            # mutation to audit -- matching the dashboard's single-delete busy path.
            return _CRON_BUSY
        except CronStoreUnreadable as exc:
            # Same "nothing happened, so nothing to audit" reasoning, minus the
            # retry: the store will not become readable by itself.
            return _cron_unreadable(exc)
        return f"✅ Removed cron job `{job_id}`" if removed else f"❌ Job `{job_id}` not found"
    if action in ("pause", "resume"):
        enabled = action == "resume"
        try:
            changed = await cron_service.enable_job_async(job_id, enabled=enabled)
        except CronStoreBusy:
            return _CRON_BUSY
        except CronStoreUnreadable as exc:
            return _cron_unreadable(exc)
        if not changed:
            return f"❌ Job `{job_id}` not found"
        return f"▶️ Resumed cron job `{job_id}`" if enabled else f"⏸️ Paused cron job `{job_id}`"
    return None


# ── task run ───────────────────────────────────────────────────────────────


async def task_command_reply(
    text: str, runner: "TaskRunner | Any", *, session_key: str = ""
) -> str | None:
    """Handle the KEYWORD grammar: ``task run <spec>`` / ``status`` / ``cancel``.

    ``project run <spec>`` is accepted as an alias for ``task run <spec>``.

    ``session_key`` is keyword-only, defaults to empty, and is forwarded
    verbatim to :func:`task_arg_reply` — see there for what the runner does with
    it. It exists on this entry point too because the two grammars reach the same
    runner: without it a ``task run <spec>`` typed as a keyword escalates its
    approval notices only to the owner DM, while the same run started from a
    ``/task`` command reaches the conversation the operator is watching.
    """
    stripped = text.strip()
    low = stripped.lower()
    if low.startswith("project run "):
        stripped = "task run " + stripped[len("project run ") :]
        low = stripped.lower()
    if not low.startswith("task run "):
        return None
    return await task_arg_reply(stripped[len("task run ") :], runner, session_key=session_key)


async def task_arg_reply(
    arg: str, runner: "TaskRunner | Any", *, session_key: str = ""
) -> str | None:
    """Handle an ALREADY-PARSED task argument: a spec path, ``status``, ``cancel``.

    Public for the same reason as :func:`spawn_task_reply`, and load-bearing here
    in a way it is not there: the keyword grammar spells the verb ``task run``,
    but a channel whose command IS ``/task`` receives ``run <spec>`` as its
    argument. Re-composing ``"task run " + arg`` then yields ``task run run
    <spec>`` and the runner is handed a spec file literally named ``run <spec>``,
    which cannot exist — so a leading ``run`` is absorbed HERE, once, rather than
    at each channel's call site.

    ``session_key`` is keyword-only and defaults to empty so the positional
    signature every existing caller uses is unchanged. It names the conversation
    the command arrived in and is forwarded to the runner, which hands it to its
    notify sink; that is what lets an approval notice go back to the channel the
    operator is watching rather than only to the Slack owner DM. A caller that
    omits it keeps exactly the previous behaviour.

    Reads ``runner.running``, ``runner.status()``, ``runner.cancel()`` and
    ``runner.start_background(path, source=, session_key=)``.
    """
    # Through the shared normalizer, so the string this dispatches on is exactly the
    # string `lists_host_state` classified. Two copies of the strip is how
    # `/task run status` came to be dispatched as `status` and scope-checked as a
    # task named "run status".
    arg = normalize_task_arg(arg)
    if not arg:
        return None

    if arg.lower() == "status":
        status = runner.status()
        if not status.get("running"):
            return "No task running."
        # Progress is per-run: ``status()`` puts only ``running``/``agent``/
        # ``runs`` at the top level, so prefer the live run when several are tracked.
        runs = status.get("runs") or []
        run = next((r for r in runs if r.get("running")), runs[0] if runs else {})
        return (
            "*Task Runner*\n"
            f"Status: {run.get('status', 'idle')}\n"
            f"Steps: {run.get('completed', 0)}/{run.get('tasks', 0)}\n"
            f"Current: step {run.get('current_task', 0)}"
        )

    if arg.lower() == "cancel":
        if not runner.running:
            return "No task running."
        runner.cancel()
        return "🛑 Task cancelled."

    if runner.running:
        return "⚠️ Task runner is already running. Use `task run cancel` first."
    # The spec is READ and its contents reach the model, so an arbitrary path is
    # an exfiltration primitive: `task run ~/.ssh/id_rsa` would hand a private key
    # to a third-party LLM. Gate it on the shared ``validate_file_path``, which
    # applies the Windows UNC trusted-root check BEFORE resolving (realpath on a
    # UNC path is itself the outbound SMB probe), canonicalizes through every
    # symlink, and refuses a resolved target under a sensitive root -- so a
    # workspace symlink into ~/.ssh is refused through the link. Hand-rolling a
    # prefix test here would miss both the symlink and the UNC case.
    #
    # The CANONICAL path is what gets used from here on, not the raw argument:
    # validating one string and then acting on another is how a guard ends up
    # ornamental.
    #
    # Off-loop: realpath and stat on a user-supplied path can block on a stalled
    # network mount, and this runs on the gateway's single event loop.
    from kiro_crew.hooks import validate_file_path

    canonical = await asyncio.to_thread(validate_file_path, arg)
    if canonical is None:
        # Deliberately does not echo the path or say WHY. A refusal that
        # distinguishes "sensitive" from "malformed" is an oracle for probing
        # which roots exist on the host.
        return "❌ That path cannot be used as a task spec."
    spec_path = Path(canonical)
    if not await asyncio.to_thread(spec_path.exists):
        return f"❌ Spec file not found: `{_redact(str(spec_path))}`"
    try:
        # Widen the call only when there IS a conversation to carry. ``runner``
        # is duck-typed (this module must not import ``TaskRunner`` at runtime),
        # so passing ``session_key=`` unconditionally would break every narrower
        # stand-in that accepts only ``(path, source=)`` — and it would do so on
        # the start path, turning a working command into "Failed to start".
        if session_key:
            await runner.start_background(spec_path, source="chat", session_key=session_key)
        else:
            await runner.start_background(spec_path, source="chat")
    except Exception as exc:
        logger.warning("task run: start_background failed", exc_info=True)
        return f"❌ Failed to start: {_redact(str(exc))}"
    return (
        f"🚀 Task started: `{_redact(spec_path.name)}`\n" "Use `task run status` to check progress."
    )
