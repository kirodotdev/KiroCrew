"""Meeting session state — per-agent batching dispatcher and lifecycle.

A live meeting fans transcription lines out to several background agent
sessions, each maintaining its own output file (notes, diagram, task list). The
per-agent :class:`AgentQueue` batches lines so an agent gets a paragraph of
context every ~30s instead of one interruption per utterance, and a circuit
breaker pauses an agent whose dispatches keep failing.

**Dispatch is in-process.** Upstream posted every batch back to its own gateway
over authenticated loopback HTTP (``POST /api/chat`` with an internal secret).
Here the app's routes are registered ON the gateway, so a batch goes straight to
the shared :class:`~kiro_crew.session.SessionManager` via
:func:`~kiro_crew.llm_helpers.stream_and_collect` — no socket, no secret, no
second copy of the auth path. Approval runs under
:data:`~kiro_crew.llm_helpers.ToolApprovalPolicy.HOOK_BASED` so the agents'
file writes still traverse the PreToolUse gate (deny patterns, sensitive paths,
governance) exactly like any other turn.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain.dictionary import DomainDictionary
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.security import redact
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.meetings")

#: Separator between queued transcript lines in one dispatched batch. A blank
#: line reads as a paragraph break to the agent, so fragments of speech stay
#: visually distinct rather than running together into one sentence.
_BATCH_SEP = "\n\n"

#: Ceiling on batches one `flush_now()` drain will dispatch. A meeting that
#: accumulated a huge backlog still ends in bounded time, and a dispatch that
#: keeps failing cannot spin the loop. Generous: at MAX_BATCH_CHARS each, this
#: is far more transcript than a real meeting produces.
_MAX_DRAIN_BATCHES = 50

# The process-wide dictionary, reloaded whenever the user edits it.
_dictionary = DomainDictionary()


#: Serializes every access to the process-wide shared dictionary.
#:
#: Lives HERE, with the state it guards, rather than at one of its callers: the
#: object is module-global, so a lock owned by `routes/settings.py` covered the two
#: mutating routes and left `reload_dictionary`'s other callers (the GET, the
#: explicit reload, the startup counter) racing them. A read that lands mid-edit
#: resets the shared object before the edit is saved, so a successful add
#: disappears.
#:
#: An RLock, for the same reason as `store._META_LOCK`: `_add_term` holds it across
#: a reload-mutate-save, and `reload_dictionary` takes it too.
_DICTIONARY_LOCK = threading.RLock()


def dictionary_transaction() -> "threading.RLock":
    """The lock guarding the shared dictionary. Use as ``with``.

    Callers that reload, MUTATE and save need it across all three; a bare
    :func:`reload_dictionary` takes it internally.
    """
    return _DICTIONARY_LOCK


def reload_dictionary(root: Path | None = None) -> DomainDictionary:
    """(Re)load the shared dictionary from disk and return it.

    Takes :func:`dictionary_transaction` itself, so a plain read cannot land in the
    middle of another request's reload-mutate-save and reset the object before the
    save.
    """
    with _DICTIONARY_LOCK:
        _dictionary.load(store.dictionary_path(root))
        return _dictionary


def shared_dictionary() -> DomainDictionary:
    return _dictionary


def slot_key(agent_id: str, meeting_id: str) -> str:
    """Session key for one agent's stream in one meeting."""
    return f"{k.SLOT_PREFIX}-{agent_id}-{meeting_id}"


#: Fragments the recognizer emits on its own, which carry no meeting content.
#:
#: An explicit set rather than a shape rule. The old rule — "three or fewer words,
#: all one or two characters" — was measured against the noise it was written for
#: and not against real speech, so it also dropped meaningful short utterances:
#: ``"I do"``, ``"we go"``, ``"no it is"``, ``"do it"``, ``"he is up"``. Those are
#: the answers to questions, and losing them removes exactly the decision a meeting
#: was held to reach, with nothing in the notes to show a turn was dropped.
#:
#: Enumerating instead means a fragment not on the list reaches the agents. That is
#: the right direction to fail: a stray ``"uh"`` in the transcript costs a reader
#: nothing, while a missing ``"I do"`` can invert the meaning of a decision.
_NOISE_FRAGMENTS = frozenset(
    {
        "",
        "i",
        "uh",
        "um",
        "ah",
        "eh",
        "oh",
        "hm",
        "hmm",
        "mm",
        "mhm",
        "er",
        "erm",
        "ok",
        "okay",
        "so",
        "and",
        "but",
        "the",
        "a",
    }
)


def is_noise(text: str) -> bool:
    """True for transcription fragments not worth an agent turn.

    A segment is noise only when EVERY word is a recognizer filler
    (:data:`_NOISE_FRAGMENTS`) — so ``"uh"``, ``"I I"`` and ``"OK so uh"`` are
    dropped while ``"I do"``, ``"we go"`` and ``"no it is"`` are not, because
    ``do``/``go``/``no``/``it``/``is`` are real words that happen to be short.

    Length-capped as well: a long run of fillers is still filler, but a segment
    with many words is far more likely to be speech the filter should not judge.
    """
    words = text.lower().split()
    if not words or len(words) > 6:
        return False
    return all(word.strip(".,!?;:") in _NOISE_FRAGMENTS for word in words)


# ── agent dispatch ──────────────────────────────────────────────────────────


async def dispatch_to_agent(
    sessions: Any, key: str, text: str, agent: str = "", *, hooks: Any = None
) -> None:
    """Send one batch to an agent's background session.

    Raises on failure so :class:`AgentQueue`'s circuit breaker can see it.
    """
    if sessions is None:
        raise RuntimeError("session manager unavailable")
    provider, _is_new, _resumed = await sessions.get_or_create(key, agent=agent or None)
    try:
        # Identity is threaded so the PreToolUse gate resolves ceiling ∩ PROFILE,
        # not the ceiling alone. The gate can only look up a profile whose name it
        # was given, and with these empty an operator profile narrowing this app —
        # denying `filesystem.write`, say — was silently not applied to tools this
        # dispatch approved. `app` is the load-bearing one; `session_key`/`agent`
        # additionally make the SEL audit attribute the call to this meeting rather
        # than to an anonymous background turn.
        await stream_and_collect(
            provider,
            text,
            approval_policy=ToolApprovalPolicy.HOOK_BASED,
            hooks=hooks,
            session_key=key,
            agent=agent,
            app=k.APP_NAME,
        )
    finally:
        # The session is long-lived for the meeting's duration (each batch adds to
        # the same conversation), so release the turn semaphore but never destroy.
        try:
            sessions.release(key)
        except Exception:
            logger.debug("meetings: session release failed for %s", key, exc_info=True)


@dataclass
class AgentQueue:
    """Per-agent message queue with time-based batching + a circuit breaker."""

    name: str
    key: str
    agent: str = ""
    sessions: Any = None
    hooks: Any = None
    queue: list[str] = field(default_factory=list)
    busy: bool = False
    batch_interval: float = k.BATCH_INTERVAL_SECS
    _flush_task: asyncio.Task | None = field(default=None, repr=False)
    _fail_count: int = 0
    _backoff: float = 0.0

    @property
    def fail_count(self) -> int:
        return self._fail_count

    @property
    def paused(self) -> bool:
        """True when repeated dispatch failures tripped the breaker."""
        return self._fail_count >= k.MAX_DISPATCH_FAILURES

    def enqueue(self, text: str) -> None:
        self.queue.append(text)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return  # a timer is already running
        try:
            self._flush_task = asyncio.get_running_loop().create_task(self._delayed_flush())
        except RuntimeError:
            # No running loop (a sync test constructing a queue). The next
            # enqueue on-loop, or an explicit flush_now(), does the work.
            self._flush_task = None

    async def _delayed_flush(self) -> None:
        """The batching timer: sleep, flush, and keep going while work remains.

        The loop lives HERE rather than as a ``_schedule_flush()`` call inside
        ``flush()``, because this coroutine IS the body of ``_flush_task`` — so from
        inside it ``self._flush_task.done()`` is False and ``_schedule_flush`` takes
        its "a timer is already running" early return. A reschedule attempted from
        within ``flush()`` therefore does nothing at all, which is what let a queue
        needing a second batch stall until teardown discarded its tail.

        Bounded by :data:`_MAX_DRAIN_BATCHES` so neither a large backlog nor a
        persistently failing dispatch can keep one task alive indefinitely; the
        circuit breaker (``paused``) is the other exit.
        """
        for _ in range(_MAX_DRAIN_BATCHES):
            await asyncio.sleep(self.batch_interval + self._backoff)
            if not await self.flush():
                return

    async def flush_now(self) -> None:
        """Force an immediate flush (meeting end / pause).

        A pending ``_flush_task`` is in one of two states, and they must be treated
        differently. Still SLEEPING on its batch interval: cancelling it is the
        point — we are flushing now instead of later. Already inside ``flush()`` and
        awaiting the agent: cancelling **kills the live turn**, and because
        ``self.busy`` is still set the follow-up ``flush()`` below then returns
        immediately — so ending a meeting mid-dispatch lost that batch AND the
        finalization notice, which is the one moment a meeting's notes matter most.

        ``self.busy`` is the discriminator, and it is only true between entering
        ``flush()`` and its ``finally``, so an in-flight dispatch is awaited to
        completion rather than interrupted. Awaiting the task (not just the flag)
        means a dispatch that fails still runs its except-branch bookkeeping.
        """
        task = self._flush_task
        if task is not None and not task.done():
            if self.busy:
                # Mid-dispatch: let it finish. Its own `finally` clears `busy`, and a
                # failure inside it is already handled by `flush()`'s except-branch,
                # so nothing needs to propagate out of the drain.
                try:
                    await task
                except Exception:
                    logger.debug(
                        "meetings: in-flight flush for %s ended in error", self.name,
                        exc_info=True,
                    )
            else:
                task.cancel()
        # Drain, not flush-once. A queue over MAX_BATCH_CHARS takes several batches,
        # and at pause/stop there is no later timer to finish the job — whatever is
        # still queued when this returns is discarded by teardown. Bounded by
        # _MAX_DRAIN_BATCHES so a dispatch that keeps failing (or a producer still
        # enqueuing) cannot spin here forever, and by the progress check so a flush
        # that consumed nothing ends the loop rather than repeating.
        # The no-progress check keys on the FAILURE COUNT, not on the queue length.
        # A failed dispatch leaves the queue untouched and still returns True
        # (`more_queued = not self.paused`), so a length comparison read a transient
        # failure as "nothing left to consume" and ended the drain with transcript
        # still queued — which teardown then discards. That is the same silent loss
        # the drain exists to prevent, reached through the guard meant to bound it.
        #
        # Retrying while the failure count RISES is what lets a transient error
        # (a gateway hiccup, a momentarily busy agent) resolve; the count is reset
        # to 0 by a successful dispatch. Two independent bounds still apply, so a
        # genuinely stuck dispatch cannot spin: `_MAX_DRAIN_BATCHES` caps the
        # iterations, and the circuit breaker pauses the queue after
        # `MAX_DISPATCH_FAILURES`, at which point `flush()` returns False.
        for _ in range(_MAX_DRAIN_BATCHES):
            before = len(self.queue)
            failures_before = self._fail_count
            if not await self.flush():
                break
            if len(self.queue) == before and self._fail_count == failures_before:
                break  # consumed nothing AND did not fail — genuinely idle
        if self.queue:
            logger.warning(
                "meetings: %s still has %d queued line(s) after a full drain",
                self.name, len(self.queue),
            )

    def resume(self) -> None:
        """Reset the breaker and retry whatever is queued."""
        self._fail_count = 0
        self._backoff = 0.0
        if self.queue:
            self._schedule_flush()

    def _take_batch(self) -> tuple[str, int]:
        """The next batch and HOW MANY queued lines it consumed.

        Whole lines only, up to ``k.MAX_BATCH_CHARS``. The count is the contract:
        the caller deletes exactly the lines that were dispatched, so a queue over
        the cap carries its tail into the next flush instead of losing it.

        Truncating the joined string and then clearing the whole queue silently
        DESTROYED transcript — the visible symptom is a long pause (which lets the
        queue exceed 60k) followed by notes that skip the end of what was said.
        A single line longer than the cap is still truncated and consumed, because
        keeping it would wedge the queue forever.
        """
        lines: list[str] = []
        used = 0
        for line in self.queue:
            # Plus the separator this line will need — every line but the first.
            cost = len(line) + (len(_BATCH_SEP) if lines else 0)
            if lines and used + cost > k.MAX_BATCH_CHARS:
                break
            lines.append(line)
            used += cost
        return _BATCH_SEP.join(lines)[: k.MAX_BATCH_CHARS], len(lines)

    async def flush(self) -> bool:
        """Dispatch one batch. Returns True when more work is still queued.

        The return value is the signal ``_delayed_flush`` and ``flush_now`` loop on:
        a queue over ``MAX_BATCH_CHARS`` needs several batches, and this call
        deliberately sends exactly one so a single turn stays bounded.
        """
        if not self.queue or self.busy or self.paused:
            return False
        batch, size = self._take_batch()
        self.busy = True
        more_queued = False
        try:
            await dispatch_to_agent(
                self.sessions, self.key, batch, self.agent, hooks=self.hooks
            )
            del self.queue[:size]
            self._fail_count = 0
            self._backoff = 0.0
            # Lines that arrived during the dispatch, OR the tail of a queue that
            # exceeded MAX_BATCH_CHARS and so needs more than one batch.
            more_queued = bool(self.queue)
        except Exception as exc:
            self._fail_count += 1
            self._backoff = min(k.BACKOFF_STEP_SECS * self._fail_count, k.BACKOFF_CAP_SECS)
            logger.error(
                "meetings: dispatch to %s failed (%d/%d), backoff %.0fs: %s",
                self.name, self._fail_count, k.MAX_DISPATCH_FAILURES, self._backoff, exc,
            )
            more_queued = not self.paused
        finally:
            self.busy = False
        return more_queued

    def cancel(self) -> None:
        """Drop any pending timer (meeting teardown)."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None


# ── config helpers ──────────────────────────────────────────────────────────


def get_enabled_agents(
    config: dict[str, Any], agents_enabled: list[str] | None = None
) -> list[dict[str, Any]]:
    """Agent definitions filtered by an explicit allow-list, else by defaults."""
    all_agents = config.get("meeting_agents") or []
    if agents_enabled is not None:
        allowed = set(agents_enabled)
        return [a for a in all_agents if a.get("id") in allowed]
    return [a for a in all_agents if a.get("enabled_by_default", True)]


# ── the live session ────────────────────────────────────────────────────────


@dataclass
class MeetingSession:
    """Tracks one active meeting's agent queues and mute state."""

    meeting_id: str
    sessions: Any = None
    hooks: Any = None
    agents_enabled: list[str] | None = None
    config: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    agents: dict[str, AgentQueue] = field(default_factory=dict)
    muted_agents: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        config = self.config if self.config is not None else store.read_config()
        enabled = get_enabled_agents(config, self.agents_enabled)
        for agent_def in enabled:
            agent_id = str(agent_def.get("id") or "")
            if not agent_id:
                continue
            self.agents[agent_id] = self._make_queue(agent_id, agent_def.get("agent") or "")
        # The task extractor always runs — it is the app's core output, not a
        # configurable agent.
        self.agents[k.TASK_EXTRACTOR_ID] = self._make_queue(
            k.TASK_EXTRACTOR_ID, k.TASK_EXTRACTOR_AGENT
        )

    def _make_queue(self, agent_id: str, agent: str) -> AgentQueue:
        return AgentQueue(
            name=agent_id,
            key=slot_key(agent_id, self.meeting_id),
            agent=agent,
            sessions=self.sessions,
            hooks=self.hooks,
        )

    def add_agent(self, agent_id: str, agent: str) -> AgentQueue:
        """Enable an agent mid-meeting (idempotent)."""
        queue = self.agents.get(agent_id)
        if queue is None:
            queue = self._make_queue(agent_id, agent)
            self.agents[agent_id] = queue
        self.muted_agents.discard(agent_id)
        return queue

    def broadcast(self, text: str) -> int:
        """Correct, filter, and enqueue *text* for every unmuted agent.

        Returns the number of queues that accepted the line (0 when filtered).
        """
        text = _dictionary.correct(text.strip())[: k.MAX_TRANSCRIPT_CHARS]
        if not text or is_noise(text):
            return 0
        accepted = 0
        for queue in self.agents.values():
            if queue.name not in self.muted_agents:
                queue.enqueue(text)
                accepted += 1
        return accepted

    @property
    def expired(self) -> bool:
        return (time.time() - self.started_at) > k.MAX_SESSION_DURATION

    @property
    def agents_paused(self) -> bool:
        return any(queue.paused for queue in self.agents.values())

    def status(self) -> dict[str, Any]:
        return {
            "active_meeting": self.meeting_id,
            "muted_agents": sorted(self.muted_agents),
            "agents": {
                name: {
                    "busy": queue.busy,
                    "queued": len(queue.queue),
                    "fail_count": queue.fail_count,
                    "paused": queue.paused,
                }
                for name, queue in self.agents.items()
            },
            "agents_paused": self.agents_paused,
            "expired": self.expired,
        }

    async def flush_all(self) -> None:
        for queue in self.agents.values():
            await queue.flush_now()

    def cancel_all(self) -> None:
        for queue in self.agents.values():
            queue.cancel()

    def resume_all(self) -> list[str]:
        resumed: list[str] = []
        for name, queue in self.agents.items():
            if queue.fail_count > 0:
                queue.resume()
                resumed.append(name)
        return resumed


# ── lifecycle (metadata side) ───────────────────────────────────────────────


def start_meeting_meta(
    meeting_id: str,
    agents_enabled: list[str] | None = None,
    title: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """Mark a meeting active, refresh its outputs map, seed missing files.

    Self-guarding: holds ``store.meta_transaction()`` across its own
    read-modify-write rather than relying on the caller to. ``_begin_meeting``
    already holds it, which is why the lock is an RLock — see its comment.
    """
    config = store.read_config(root)
    enabled = get_enabled_agents(config, agents_enabled)
    with store.meta_transaction():
        return _start_meeting_meta_locked(meeting_id, agents_enabled, title, enabled, root)


def _start_meeting_meta_locked(
    meeting_id: str,
    agents_enabled: list[str] | None,
    title: str,
    enabled: list[dict[str, Any]],
    root: Path | None,
) -> dict[str, Any]:
    """The read-modify-write itself. Caller holds ``store.meta_transaction()``."""
    meta = store.read_meeting_meta(meeting_id, root) or store.new_meeting_meta(
        meeting_id, title or "Meeting"
    )
    if title:
        meta["title"] = title
    meta["status"] = k.STATUS_ACTIVE
    meta["started_at"] = store.utc_now_iso()
    if agents_enabled is not None:
        meta["agents_enabled"] = agents_enabled

    outputs: dict[str, str] = {}
    for agent_def in enabled:
        try:
            fname = store.agent_output_filename(agent_def)
        except store.MeetingsPathError:
            continue
        if fname:
            outputs[str(agent_def["id"])] = fname
    meta["outputs"] = outputs
    store.write_meeting_meta(meeting_id, meta, root)
    store.ensure_agent_files(meeting_id, enabled, meta.get("title", "Meeting Notes"), root)
    return meta


def end_meeting_meta(meeting_id: str, root: Path | None = None) -> dict[str, Any] | None:
    """Mark a meeting ended. Holds the metadata transaction over its own RMW.

    Stopping a meeting races an in-flight attachment or mute request otherwise: this
    read-modify-write ran unlocked against their locked ones, so whichever wrote
    last silently discarded the other's update.
    """
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            return None
        meta["status"] = k.STATUS_ENDED
        meta["ended_at"] = store.utc_now_iso()
        store.write_meeting_meta(meeting_id, meta, root)
    return meta


# ── agent kickoff prompts ───────────────────────────────────────────────────


def build_meeting_context(meta: dict[str, Any]) -> str:
    """Human-readable meeting context injected into each agent's first message.

    Everything here comes from user/calendar data, so it is redacted before it
    reaches a model prompt that the model may later echo back into chat.
    """
    parts = [f"Meeting: {redact(str(meta.get('title') or 'Meeting'))}"]
    if meta.get("description"):
        parts.append(f"Description: {redact(str(meta['description']))}")
    attendees = meta.get("attendees") or []
    if attendees:
        parts.append("Attendees: " + redact(", ".join(str(a) for a in attendees)))
    attachments = meta.get("attachments") or []
    if attachments:
        parts.append("Attached documents:")
        for att in attachments:
            if not isinstance(att, dict):
                continue
            label = redact(str(att.get("label") or ""))
            kind = att.get("type")
            if kind == "file" and att.get("path"):
                parts.append(f"  - {label}: read the file at {redact(str(att['path']))}")
            elif kind == "url" and att.get("url"):
                parts.append(f"  - {label}: {redact(str(att['url']))}")
    return "\n".join(parts)


def build_init_message(
    agent_def: dict[str, Any],
    meta: dict[str, Any],
    output_path: str,
    cross_ref: str,
) -> str:
    """The first message an agent receives when a meeting starts."""
    prompt = agent_def.get("prompt") or (
        f"You are the {agent_def.get('name') or agent_def.get('id')} agent for this meeting."
    )
    return (
        f"OUTPUT_FILE: {output_path}\n\n"
        f"{prompt}\n\n"
        "Write your output to the exact path in OUTPUT_FILE above. Copy it "
        "character-for-character — do not shorten or modify it.\n"
        "The file already exists — overwrite it directly.\n"
        "IMPORTANT: write the FULL updated file after every transcription batch. "
        "Do not accumulate in memory — write immediately so your output survives "
        "a context limit.\n\n"
        f"Meeting context:\n{build_meeting_context(meta)}\n\n"
        f"{cross_ref}\n\n"
        "Read any attached documents now for context, then wait for transcription."
    )


TASK_EXTRACTOR_PROMPT = (
    "You are a meeting task extractor. Listen for action items, assignments, and "
    "follow-ups. Maintain a JSON file with the structure "
    '{"meeting_id": "...", "tasks": [{"id": "t1", "description": "...", '
    '"assignee": "...", "priority": "medium", "status": "open"}], '
    '"updated_at": "..."}. Only extract genuine action items — not discussion '
    "topics or open questions."
)


def build_cross_reference(
    meeting_dir: str, enabled: list[dict[str, Any]]
) -> str:
    """The "here is where the other agents write" block each agent receives."""
    lines: list[str] = []
    for agent_def in enabled:
        try:
            fname = store.agent_output_filename(agent_def)
        except store.MeetingsPathError:
            continue
        if fname:
            name = agent_def.get("name") or agent_def.get("id")
            lines.append(f"  - {name} ({agent_def.get('id')}): {meeting_dir}/{fname}")
    lines.append(f"  - Tasks ({k.TASK_EXTRACTOR_ID}): {meeting_dir}/{k.TASKS_FILE}")
    return "All agent output files (read for cross-reference):\n" + "\n".join(lines)


def _init_agents_plan(
    session: MeetingSession, meta: dict[str, Any], root: Path | None = None
) -> tuple[list[dict[str, Any]], str, str]:
    """Resolve the agent list, meeting dir, and cross-reference block. BLOCKING.

    Runs on a worker thread, never the event loop: ``read_config`` parses
    ``config.json`` and ``meeting_dir`` resolves a path on disk (``resolve()``
    follows symlinks, so it stats every component) before the containment check.

    Grouped into one hop because the whole prologue is derived from one config
    snapshot, and because :func:`init_agents` must then ``await`` a dispatch per
    agent — sequential hops here would add a loop yield before every one of them.
    """
    config = session.config if session.config is not None else store.read_config(root)
    enabled = get_enabled_agents(config, meta.get("agents_enabled"))
    mdir = str(store.meeting_dir(session.meeting_id, root))
    return enabled, mdir, build_cross_reference(mdir, enabled)


async def init_agents(
    session: MeetingSession, meta: dict[str, Any], root: Path | None = None
) -> None:
    """Kick off each enabled agent's session with its instructions.

    Failures are logged, not raised: one agent that cannot start must not abort
    the meeting for the others.
    """
    enabled, mdir, cross_ref = await asyncio.to_thread(_init_agents_plan, session, meta, root)

    for agent_def in enabled:
        try:
            fname = store.agent_output_filename(agent_def)
        except store.MeetingsPathError:
            continue
        if not fname:
            continue
        agent_id = str(agent_def["id"])
        message = build_init_message(agent_def, meta, f"{mdir}/{fname}", cross_ref)
        await _safe_dispatch(session, agent_id, message, agent_def.get("agent") or "")

    task_message = build_init_message(
        {"id": k.TASK_EXTRACTOR_ID, "name": "Task Extractor", "prompt": TASK_EXTRACTOR_PROMPT},
        meta,
        f"{mdir}/{k.TASKS_FILE}",
        cross_ref,
    )
    await _safe_dispatch(
        session, k.TASK_EXTRACTOR_ID, task_message, k.TASK_EXTRACTOR_AGENT
    )


async def _safe_dispatch(
    session: MeetingSession, agent_id: str, message: str, agent: str
) -> None:
    try:
        await dispatch_to_agent(
            session.sessions,
            slot_key(agent_id, session.meeting_id),
            message,
            agent,
            hooks=session.hooks,
        )
    except Exception:
        logger.warning("meetings: could not initialize agent %s", agent_id, exc_info=True)
        _audit_dispatch(agent_id, outcome="error")
        return
    _audit_dispatch(agent_id, outcome="ok")


def _audit_dispatch(agent_id: str, *, outcome: str) -> None:
    try:
        sel().log_tool_invocation(
            session_key="",
            source=f"app:{k.APP_NAME}",
            tool_name="meetings.agent_init",
            tool_kind="agent_dispatch",
            outcome=outcome,
            resources=agent_id,
        )
    except Exception:  # pragma: no cover
        logger.debug("meetings: SEL audit failed for agent init", exc_info=True)


async def broadcast_system(session: MeetingSession, message: str) -> None:
    """Send a lifecycle notice to every agent, flushing immediately."""
    for queue in session.agents.values():
        queue.enqueue(message)
    await session.flush_all()
