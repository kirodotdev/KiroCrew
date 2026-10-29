"""HTTP handlers for /side: ephemeral Q&A attached to a parent slot.

Sidecar buffer on ``slot._side``; isolated ``side:{slot.key}`` LLM session;
read-only tool calls auto-approved and everything else hard-rejected via
``READ_ONLY`` (the Reads approval mode's classifier with reject as the
fallback instead of the approval card). Side messages never enter
``slot.messages`` or any persistent store.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew.acp.client import AcpAuthRequired
from kiro_crew.acp_backends import ACP_BACKEND_GOOSE, ACP_BACKENDS_SIDE_READONLY
from kiro_crew.agent_discovery import warm_project_agent_names
from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
from kiro_crew.agent_sdk.drivers.acp import (
    claude_adapter_honours_session_options,
    claude_adapter_install_command,
    claude_adapter_min_version,
    goose_side_home_supported,
)
from kiro_crew.config.loader import (
    KiroCrewConfig,
    refresh_materialized_agents,
    resolve_agent_bindings,
)
from kiro_crew.dashboard.handlers.side_support import (  # noqa: F401 -- re-exported
    _MAX_QUESTION_BYTES,
    _check_slot_ownership,
    _read_side_queue_body,
    _requeue_unconsumed_side_steers,
    _resolve_side_queue_slot,
    _side_session_key,
    _try_side_steer,
)
from kiro_crew.dashboard.request_priority import owner_start_priority
from kiro_crew.dashboard.side_context import build_side_message
from kiro_crew.dashboard.side_readonly_spec import ReadOnlySpecError, publish_readonly_spec
from kiro_crew.dashboard.side_state import (
    MAX_SIDE_QUEUE,
    STEER_CONSUMED,
    STEER_PENDING,
    STEER_REQUEUED,
    SideState,
)
from kiro_crew.dashboard.slot_ownership import slot_not_found
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.ws import (
    broadcast_side_closed,
    broadcast_side_queue,
    broadcast_side_result,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import HookManager
from kiro_crew.llm_helpers import (
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    stream_and_collect,
)
from kiro_crew.security import StreamRedactor
from kiro_crew.security import redact
from kiro_crew.security import redact as _batch_redact
from kiro_crew.security import redact_and_truncate
from kiro_crew.sel import sel
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger(__name__)

#: Cap on a tool row's ``input_preview`` and ``purpose`` in the side stream. The
#: row is an ephemeral preview the pill folds at 800 characters, not the main
#: transcript's audit record (``chat_utils._MAX_TOOL_FIELD``, 1 MB): the audit
#: trail of a side turn's tool calls is SEL's tool-invocation row (title and
#: kind, written by ``stream_and_collect``), never the panel.
_SIDE_TOOL_PREVIEW_CHARS = 2_000


# The stream redactor holds back one trailing credential-class run, plus the
# shapes its own anchors know (a Bearer header, a ``token=`` parameter, a JWT).
# Every pattern the batch pass knows is matched inside one such run except
# three, whose match waits for text the model may write much later and spans
# bytes outside that class: a key block needs its footer (or body lines the
# truncated-block form knows) and has newlines inside; a URL is judged whole (a
# password needs the ``@`` that ends it, an exfiltration URL its payload) and
# its path or query may hold a ``!`` or a ``$``; a labelled value (an AWS field,
# ``aws_secret_access_key: ``; an ``Authorization: Bearer `` header) is folded
# with its label into one tag, and the space after the label commits the label
# alone (the stream redactor's own Bearer anchor lets go of the header once a
# byte outside its value class, a quote, follows). Text inside an open one is
# held back here too, from the header, the scheme or the label, until the
# pattern can be judged: a key block until its footer, a URL until the
# whitespace that ends it, a label until its value's first byte.
_KEY_BLOCK_HEADER_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_BLOCK_FOOTER_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_OPEN_URL_RE = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://\S*\Z")
_OPEN_LABEL_RE = re.compile(
    r"(?:(?:SecretAccessKey|aws_secret_access_key|SessionToken|aws_session_token"
    r"|AccessKeyId|aws_access_key_id)[\"']?\s*(?:[:=]\s*[\"']?)?"
    r"|(?i:Authorization)[\"']?\s*(?:[:=]\s*[\"']?(?:(?i:Bearer)\s*)?)?)\Z"
)

# Up to this many committed characters every commit gets its own whole-text
# pass. Past it a pass waits for ``committed // 32`` new characters (at most
# ``_PASS_STEP_MAX``), so a long reply costs a bounded number of passes, each
# over the reply so far, instead of one per chunk.
_PASS_EVERY_COMMIT_BELOW = 8_192
_PASS_STEP_MAX = 1_024


def _open_credential_start(text: str) -> int | None:
    """Offset of a key block, URL or value label ``text`` opens and does not
    close, else ``None``. Everything from it must wait for the text that
    decides it."""
    starts = []
    for header in _KEY_BLOCK_HEADER_RE.finditer(text):
        if not _KEY_BLOCK_FOOTER_RE.search(text, header.end()):
            starts.append(header.start())  # the first header with no footer after it
            break
    for pattern in (_OPEN_URL_RE, _OPEN_LABEL_RE):
        match = pattern.search(text)
        if match is not None:
            starts.append(match.start())
    return min(starts) if starts else None


class _SegmentRedactor:
    """Redact a side reply that tool rows split into text rows.

    The main chat settles its one row with ``redact()`` over the whole reply.
    A tool row splits the side reply into rows, and a row cannot be redacted
    from its own text alone: a pattern that spans the split (a key header above
    the row, its base64 body below; ``postgres://user:pass`` above, ``@host``
    below) matches only with both halves in one text. So one pass over the
    whole reply so far decides every frame, and a row's text is what that pass
    produces past the rows above it.

    The :class:`StreamRedactor` inside says WHEN the pass may run: it withholds
    a trailing credential-class run the way the main chat's live stream does
    (``chat_runner._wsred``), and ``_open_credential_start`` extends that hold
    over the two shapes whose judgment waits for later text, so the text a pass
    runs over never ends inside a credential the batch patterns know. The
    stream redactor's own output is not drawn; the live view is the whole-text
    pass over the committed text, so a commit the pass rewrites (a key body
    committed under its header) never reaches the wire. The run still held when
    a tool row lands is neither flushed nor drawn: the text after the call
    decides it.

    The pass runs over the reply as written, with one substitution. A value the
    stream redactor DROPS (a credential still open at its hold-back ceiling,
    ``_STREAM_HOLDBACK_JWT_MAX``, or a complete ``token=`` value its cut
    bisects: it emits its tag and swallows the bytes, holding nothing) is a
    value the batch patterns need not see whole, so a pass over the bytes as
    written would hand them on (an unsigned JWT's claims, a bearer token's
    head). In the text the passes see (``_view``), the dropped bytes are the
    tag they were traded for; see ``_take``. The stored reply is the pass over
    that text too, so a dropped value stays dropped.

    What a frame can still carry is what ``redact()`` leaves in the reply as
    written so far. A row above a tool row is settled once and cannot be taken
    back, so the holds above keep everything a later byte could turn into a
    credential out of it; a key block the reply never closes is held to the
    end and judged there by the batch pass on what the reply has (the
    truncated-block form of the pattern), which is stricter than the main
    chat's live stream, which draws such a body as it comes.
    """

    __slots__ = (
        "_wsred",
        "_committed_through",
        "_view",
        "_len",
        "_wrote",
        "_passed",
        "_rows",
        "_settled",
        "_streamed",
    )

    def __init__(self) -> None:
        # The stream redactor commits through the batch function it is given;
        # ``_take`` reads what it committed off this hook.
        self._committed_through: list[tuple[str, str]] = []
        self._wsred = StreamRedactor(self._batch)
        self._view: list[str] = []  # the reply as the passes see it (``_take``)
        self._len = 0  # len("".join(_view))
        self._wrote = False  # whether the model wrote any text
        self._passed = 0  # committed length at the last live pass
        self._rows = 0  # tool rows drawn so far
        self._settled = ""  # the pass over the reply up to the row above
        self._streamed = ""  # live deltas drawn for the current row

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def wrote(self) -> bool:
        return self._wrote

    def _batch(self, text: str) -> str:
        """What the stream redactor commits through: the batch function it
        would use on its own (its commits are not the whole-text pass, which
        is ``redact`` above and may be stubbed in tests), recorded."""
        out = _batch_redact(text)
        self._committed_through.append((text, out))
        return out

    def _take(self, text: str) -> str:
        """Feed a delta to the stream redactor and keep ``_view`` in step with
        it; return what the stream redactor emitted (``""`` while it holds).

        ``_view`` holds the bytes the stream redactor has consumed from its
        buffer (the run it still holds is not in it yet), as written, except
        the bytes it DROPPED, which stand in the view as what it emitted for
        them. What it emits in a feed is the batch redaction of each run it
        commits (through ``_batch``, which records the run and its redaction)
        with its own tag where it drops a value: the feed that arms a drop
        emits the committed prefix's redaction and the tag for the value to the
        buffer's end (or for a complete token value its cut bisects), a feed
        that continues a drop emits nothing (or the tag again at its bound),
        and the feed that ends a drop swallows the run and commits the rest of
        the delta as usual. So the view takes each committed run as written in
        place of its redaction, and whatever else was emitted as it is. The
        rule is on what the redactor did, not on which path did it, so a path
        this docstring does not list is covered too.
        """
        self._committed_through.clear()
        out = self._wsred.feed(text)
        take = ""
        pos = 0
        for committed, redacted in self._committed_through:
            # The stream redactor emits each commit's redaction in order, with
            # only its own tags between them. A commit that cannot be found
            # where the previous one ended means that contract changed under
            # this code; failing the turn is safer than splicing text that is
            # not the reply (the handler reports the turn as interrupted).
            at = out.find(redacted, pos)
            if at < 0:
                raise RuntimeError("StreamRedactor emitted a commit out of order")
            take += out[pos:at] + committed
            pos = at + len(redacted)
        take += out[pos:]
        self._view.append(take)
        self._len += len(take)
        return out

    def _committed(self) -> str:
        """The text a pass may run over: the view (which never ends inside the
        run the stream redactor holds) minus an open key block or URL."""
        committed = "".join(self._view)
        cut = _open_credential_start(committed)
        return committed if cut is None else committed[:cut]

    def _share(self, whole: str) -> str:
        """The current row's share of a pass over the reply: what it produces
        past the rows above. Those rows were settled by a pass over a prefix of
        the same text. A later pass agrees with it except where the batch
        redactor's judgment of that prefix changed with the text after it (a
        header name folded into the tag of the value that followed the call),
        so the share starts where the two passes stop agreeing: the rewrite is
        drawn in this row, and the row above keeps what it was settled with."""
        if whole.startswith(self._settled):
            return whole[len(self._settled) :]
        return whole[len(os.path.commonprefix((whole, self._settled))) :]

    def feed(self, text: str) -> str:
        """Take a delta; return the part safe to draw now (possibly empty).

        A live pass is one ``redact()`` over the committed reply (about 1.4 ms
        per 10k characters, measured), paid on the event loop like the main
        chat's per-flush redaction. Past ``_PASS_EVERY_COMMIT_BELOW`` committed
        characters the pass waits for a step of new text (see ``_pass_step``):
        a 64k-character reply costs about 60 passes past that point, not one
        per chunk.
        """
        self._wrote = self._wrote or bool(text)
        if not self._take(text):
            return ""
        if self._len - self._passed < self._pass_step():
            return ""
        self._passed = self._len
        expected = self._share(redact(self._committed()))
        if not expected.startswith(self._streamed):
            # The pass rewrote text this row already shows. A delta cannot fix
            # that; the frame that settles the row replaces it.
            return ""
        out = expected[len(self._streamed) :]
        self._streamed = expected
        return out

    def _pass_step(self) -> int:
        """New committed characters a live pass waits for."""
        if self._passed < _PASS_EVERY_COMMIT_BELOW:
            return 1
        return min(_PASS_STEP_MAX, self._passed // 32)

    def close_row(self) -> str | None:
        """A tool row lands: settle the current row, start the next.

        Returns the row's text when it differs from the live deltas drawn for
        it (``None`` otherwise, nothing to send). The client draws every plain
        delta as sent and replaces the row only on a settle or terminal frame
        (``store/chat/side.ts``), so the deltas drawn are exactly ``_streamed``.
        """
        self._passed = self._len
        whole = redact(self._committed())
        row_text = self._share(whole)
        result = row_text if row_text != self._streamed else None
        self._settled = whole
        self._streamed = ""
        self._rows += 1
        return result

    def reply(self) -> str:
        """The whole reply redacted, as stored: the pass over the view plus the
        run the stream redactor still holds, nothing held back (a key block or
        URI the reply left open is judged by the batch pass on what the reply
        has, as the main chat's terminal frame judges it)."""
        return redact("".join(self._view) + self._wsred.held)

    def finish(self) -> str:
        """The terminal frame's text: ``reply()`` past the rows above."""
        return self._share(self.reply())


# How long the stop handler waits for a cancelled side turn's cleanup (session
# release, queue hold, is_complete flip) to run before settling the client
# anyway. Bounds a wedged ``finally`` so a stop never hangs the request.
_STOP_CLEANUP_TIMEOUT = 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dispatch_side_turn(
    state: DashboardState,
    slot,
    question: str,
    *,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> str:
    """Start a side turn for *question* and return its run_id.

    Callers must have established that no side turn is in flight; there is no
    await between the caller's check and the state mutation here, so the
    single-threaded event loop makes the pair atomic.
    """
    side = slot._side
    run_id = uuid.uuid4().hex
    is_first_turn = not any(m.get("role") == "assistant" for m in side.messages)
    side.last_run_id = run_id
    side.is_complete = False
    message_id = side.append_user(question, run_id=run_id)

    broadcast_side_result(
        state,
        slot_key=slot.key,
        run_id=run_id,
        role="user",
        content=question,
        message_id=message_id,
    )

    task = asyncio.create_task(
        _run_side_turn(
            state,
            slot,
            run_id,
            question,
            is_first_turn=is_first_turn,
            start_priority=start_priority,
        )
    )
    # Store the handle on the sidecar so ``api_side_stop`` can cancel this turn
    # by lookup; ``state._background_tasks`` has no per-slot key. The turn's
    # finally clears it once this run is the sidecar's current one.
    side.task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return run_id


def _drain_side_queue(state: DashboardState, slot, run_id: str) -> None:
    """Promote the oldest queued message to the next side turn.

    ``run_id`` is identity-checked against the sidecar's current run so a stale
    task from a closed-and-reopened side can never dispatch onto the new state.
    No-op when the side is closed, still busy, STOPPING, or the queue is empty.
    """
    side = slot._side
    if side is None or not side.open:
        return
    if side.last_run_id != run_id or not side.is_complete or side.is_stopping:
        # ``is_stopping`` holds a deliberately-cancelled turn busy through the
        # stop handler's cleanup even after its own ``finally`` flipped
        # ``is_complete`` True; draining a successor in that window would race
        # a replacement over the still-settling cancelled turn.
        return
    entry = side.queue_pop()
    if entry is None:
        return
    broadcast_side_queue(
        state,
        slot_key=slot.key,
        action="drain",
        queue_id=entry["id"],
        depth=len(side.queue),
    )
    try:
        _dispatch_side_turn(state, slot, entry["content"])
    except Exception:
        # The card is already retired on the client, so put the text back at the
        # head rather than only reporting the failure: "a submit is never
        # dropped" has to hold on this path too.
        logger.exception("Side queue drain failed: slot=%s", slot.key)
        # Room is guaranteed here: this path just popped the entry it is putting
        # back. Still handled, so a future change cannot silently broadcast null.
        steer_id = entry.get("steer_id", "")
        qid = side.queue_insert_front(entry["content"], steer_id=steer_id)
        if qid is None:
            logger.error("Side re-insert found no room: slot=%s", slot.key)
            return
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=entry["content"],
            depth=len(side.queue),
            front=True,
            steer_id=steer_id,
        )
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=entry["id"],
            role="assistant",
            content="(queued side question could not be started — it is back in the queue)",
            is_error=True,
            final=True,
        )


async def _side_read_only_tools(side_backend: str | None) -> bool:
    """Whether a side turn on *side_backend* runs read-only tools.

    One answer for the turn and for ``GET /api/chat/side/tools``, so the footer
    states what the turn does. ``None`` is a config that did not load.
    """
    if side_backend is None or side_backend not in ACP_BACKENDS_SIDE_READONLY:
        return False
    if is_claude_backend_name(side_backend):
        # The confinement rides _meta.claudeCode.options, which an adapter
        # below the verified floor may ignore.
        return await asyncio.to_thread(claude_adapter_honours_session_options)
    if side_backend == ACP_BACKEND_GOOSE:
        # The confinement is a separate goose home, which only POSIX hosts get.
        return goose_side_home_supported()
    return True


async def _run_side_turn(
    state: DashboardState,
    slot,
    run_id: str,
    question: str,
    *,
    is_first_turn: bool,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> None:
    """Background task: drive one side turn and broadcast chunks over WS."""
    # Local import: chat_runner imports this package, so a module-level import
    # would close a cycle. Bound here rather than inside the try below because
    # the ``except`` clause has to name the exception.
    from kiro_crew.dashboard.chat_runner import (
        _AppAgentNotLoaded,
        _recover_app_agent_binding,
    )

    # The sidecar this turn belongs to. Every later mutation is gated on the slot
    # still pointing at THIS object, so a close+reopen part-way through can never
    # have the old turn settle or requeue onto the replacement's state — and the
    # ACP session is keyed by this sidecar's generation for the same reason.
    side_at_start = slot._side
    if side_at_start is None:
        logger.info("Side turn dropped: no sidecar on slot=%s run_id=%s", slot.key, run_id)
        return
    side_key = _side_session_key(slot.key, side_at_start.gen)
    # The prompt envelope is built below, once the harness is known: the boundary
    # it states (read-only allowance vs. no tools) follows the approval policy
    # the turn will actually run under.
    # Redaction for the live side stream. broadcast_side_result already redacts
    # each frame, but per-frame redaction alone misses a secret split across
    # streaming chunk boundaries (the StreamRedactor inside withholds a trailing
    # credential-class run until it's confirmed safe, as chat_runner._wsred
    # does) or across a tool row, which splits the reply into rows: every row is
    # redacted with the whole reply as its context.
    _redactor = _SegmentRedactor()
    drawn_tool_ids: set[str] = set()

    def _emit(text: str) -> None:
        """Broadcast a live delta."""
        if text:
            _broadcast(role="assistant", content=text)

    def _broadcast(**frame: Any) -> None:
        """Broadcast one of this run's frames while its sidecar is still the slot's.

        A close tells every tab to clear the conversation (``chat.side_closed``),
        and after a reopen the conversation on screen is a newer one, so a later
        frame of this run would land in a conversation it does not belong to: it
        would clear that conversation's busy state and become its last run.
        """
        if slot._side is side_at_start:
            broadcast_side_result(state, slot_key=slot.key, run_id=run_id, **frame)

    def _on_chunk(text: str) -> None:
        _emit(_redactor.feed(text))

    def _on_tool_call(event: Any, approved: bool) -> None:
        """Draw a tool attempt where it happened in the reply.

        ``event`` is the ``LLMEvent`` ``stream_and_collect`` reports. The row is
        a ``🔧 <title>``, shaped as the main chat's (``chat_runner._tool_meta``
        / ``_tool_call_ws_payload``) so the shared transcript draws it with the
        same pill; a refused attempt also gets the hidden ``🚫`` sibling the
        transcript reads as "blocked". Every field is LLM-authored:
        ``broadcast_side_result`` redacts each one. kiro-cli reports an attempt
        as a tool-call event and then asks permission for it, so a refusal can
        arrive for a row already drawn: that adds the sibling alone.

        The text row ends at every tool row, the ``🚫`` sibling included: the
        client starts a new text row under each, so the server closes one there
        too, or a delta that landed between a row and its sibling would be drawn
        twice by the terminal frame. The row is settled with a whole-text pass
        (see ``_SegmentRedactor``) the way the terminal frame settles the last
        row; the run the stream redactor still withholds is neither flushed nor
        settled, and is drawn under the row once the text after the call has
        proven it safe.
        """
        if slot._side is not side_at_start:
            return
        title = event.title or event.tool_name or "tool"
        call_id = event.tool_call_id or ""

        def _close_row() -> None:
            # The frame replaces the row's streamed deltas and is sent only
            # when the whole-text pass rewrites them.
            settled = _redactor.close_row()
            if settled is not None:
                _broadcast(
                    role="assistant",
                    content=settled,
                    settle=True,
                )

        if call_id and call_id in drawn_tool_ids:
            if not approved:
                _close_row()
                _broadcast(
                    role="tool",
                    content=f"🚫 {title}",
                    meta={"tool_call_id": call_id},
                )
            return
        _close_row()
        if call_id:
            drawn_tool_ids.add(call_id)
        # Redacted whole, then cut: a cut inside a credential leaves a prefix no
        # pattern matches (``postgres://user:pass`` without its ``@host``).
        preview = redact_and_truncate(event.tool_input or "", _SIDE_TOOL_PREVIEW_CHARS)
        meta = {
            "tool_call_id": call_id,
            "kind": event.tool_kind or "",
            "purpose": redact_and_truncate(event.tool_purpose or "", _SIDE_TOOL_PREVIEW_CHARS),
            "input_preview": preview,
        }
        content = f"🔧 {title}"
        if preview and preview != title:
            content += f"\n{preview}"
        _broadcast(role="tool", content=content, meta=meta)
        if not approved:
            _broadcast(
                role="tool",
                content=f"🚫 {title}",
                meta={"tool_call_id": call_id},
            )

    def _on_steer_consumed(snapshot: str) -> None:
        """Settle the steers this echo proves the backend injected.

        Bound to ``side_at_start``: an echo belongs to the turn that produced it,
        so a close+reopen mid-turn must not let it settle the REPLACEMENT
        sidecar's own pending steer — that steer would then never be requeued and
        the question would be lost silently.

        Whatever stays pending never reached a generation, and the finally below
        turns it back into a visible queue card.
        """
        if side_at_start is None or slot._side is not side_at_start:
            return
        # Proof of injection is the ONLY thing that may put a steer in the
        # transcript, so this callback is the single writer of the chip. The
        # submitting request cannot do it: when its RPC returns, consumption is
        # still unproven, and committing then duplicated any question the model
        # never consumed (the finally requeues it and the queue runs it for real).
        for entry in side_at_start.steer_settle(snapshot):
            steer_run_id = side_at_start.last_run_id or ""
            message_id = side_at_start.append_user(entry["text"], steer=True, run_id=steer_run_id)
            broadcast_side_result(
                state,
                slot_key=slot.key,
                run_id=steer_run_id,
                role="user",
                content=entry["text"],
                steer=True,
                message_id=message_id,
            )

    provider = None
    acquired_key = ""
    auth_required = False
    # A stop request cancels this task; the finally then holds the queue intact
    # rather than draining the next question into a fresh turn — stopping means
    # halt, not "run the next one". The requeue of unconsumed steers still runs,
    # so nothing the user typed is lost.
    cancelled = False
    try:
        # Resolve the KiroCrew slot agent name (e.g. "default") to the real
        # kiro-cli agent (e.g. "kirocrew") before creating the session. Passing
        # the raw slot name straight through to get_or_create -> create_session
        # -> set_mode fails with "Mode '<name>' not found" because there is no
        # matching ~/.kiro/agents/<name>.json. Mirrors chat_runner._run_chat and
        # chat_handlers, which resolve bindings for the same reason. Best-effort:
        # a config-load failure degrades to the raw slot.agent rather than
        # crashing the turn.
        kiro_agent: str | None = None
        # An app-owned slot whose agent never resolved (see the fail-loud guard
        # after this block). Captured inside the try so the raise lives OUTSIDE
        # it and is not swallowed by the resolve except.
        _app_agent_unresolved = False
        # ONE reading of the slot's project and agent for the whole turn. The
        # derivation below is checked against a project (the shadow scan of its
        # ``.kiro/agents``), the session is spawned in a project (kiro-cli
        # resolves ``--agent`` against that scope first), and the binding names
        # a project: all three must be the same one. Read live, a project change
        # landing during an await would have the check run against the old
        # project and the spawn happen in the new one, loading a file the check
        # never saw. A change is picked up by the NEXT turn, whose binding then
        # differs and cold-starts under its own derivation.
        project: str | None = slot.project or None
        slot_agent: str | None = slot.agent or None
        # The READ_ONLY policy's classifier is the gateway's ONE live hook gate,
        # the same object the main chat consults (``chat_runner`` reads
        # ``state.context_builder.hooks``). Settings > Security hot-reloads that
        # object (``handlers/security._reload_live_hooks``), so a deny added
        # mid-turn binds an in-flight side turn exactly as it binds the parent
        # slot, and no per-turn manager is built here — building one would read
        # the keystone ``denied_commands.json`` synchronously on the event loop
        # and freeze the opt-out state at turn start. A host with no context
        # builder has no gate: READ_ONLY then fails closed and rejects every
        # tool call (``read_only_policy_no_hooks``) rather than classifying
        # against a default manager that carries none of the operator's rules.
        side_hooks: HookManager | None = (
            state.context_builder.hooks if state.context_builder is not None else None
        )
        # The selected harness, read from the same config load that resolves the
        # agent. ``None`` when the load failed: the harness is then unknown, and
        # the read-only allowance below is granted by positive membership only.
        # The config itself is kept too: the first-turn envelope reads the
        # reply-style setting off it (``build_side_format_prompt``), and a
        # failed load leaves it ``None`` so that block is simply omitted.
        side_backend: str | None = None
        cfg: KiroCrewConfig | None = None
        try:
            cfg = KiroCrewConfig.load()
            side_backend = cfg.agent.acp_backend
            # Warm off-loop, resolve inline — same reasoning as the main chat path
            # (offloading the resolver itself would swallow a StopIteration into a
            # Future and hang the await).
            await warm_project_agent_names(
                project or "", operation="side_panel", source="dashboard"
            )
            bindings = resolve_agent_bindings(cfg, slot_agent, project)
            kiro_agent = bindings.kiro_agent
            # SELF-HEAL an app-owned slot whose agent did not resolve, on the same
            # two escalating rungs `_run_chat` uses, for the same reason: an app's
            # agents live only in ``~/.kiro/agents/<app>--<agent>.json``, so the
            # resolver can honor them only through the materialized-agent snapshot,
            # which is COLD on the event loop until a warm lands. A cold read falls
            # back to the default agent with ``requested_resolved=False``.
            #
            # A side turn reaches this with its OWN session, so it cannot ride on
            # the main chat having healed first — a user can open the side panel on
            # an app slot that has never taken a turn. Guarded so the common path
            # (no ``_app``, or already resolved) does zero extra work and no I/O.
            if slot._app and not bindings.requested_resolved:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(), refresh_materialized_agents
                    )
                except Exception:  # noqa: BLE001 — a warm failure only costs the fail-loud
                    logger.warning(
                        "Side turn: failed to warm materialized agents for app slot %s",
                        slot.key,
                        exc_info=True,
                    )
                bindings = resolve_agent_bindings(cfg, slot_agent, project)
                kiro_agent = bindings.kiro_agent
                if not bindings.requested_resolved:
                    bindings = await _recover_app_agent_binding(cfg, slot, project=project)
                    kiro_agent = bindings.kiro_agent
            _app_agent_unresolved = bool(slot._app) and not bindings.requested_resolved
        except Exception:
            logger.warning(
                "Side turn: failed to resolve agent bindings for slot=%s; "
                "falling back to raw slot.agent",
                slot.key,
                exc_info=True,
            )

        # FAIL-LOUD, exactly as the main chat does: an app-owned slot whose agent
        # STILL did not resolve must NOT run the default agent. Substituting it
        # here is worse than the main chat's version of the same bug, because a
        # side answer carries no turn card the user would scrutinise — it reads as
        # this app's assistant answering, while actually being the generic default
        # with none of the app's MCP tools. Raised before get_or_create, so the
        # turn aborts without ever creating a session or dispatching an agent.
        if _app_agent_unresolved:
            raise _AppAgentNotLoaded(
                f"The app agent '{slot_agent or ''}' isn't loaded yet — "
                "try again in a moment, or restart the gateway."
            )

        # Whether this turn may EXECUTE read-only tools is a positive capability
        # of the harness (``ACP_BACKENDS_SIDE_READONLY``, harness-parity H6),
        # never a negation. A harness's own pre-approval surface is one the gate
        # cannot see, so each member confines its side session so that surface
        # cannot reach a write: kiro-cli through the derived agent spec below,
        # claude-agent-acp through the session options its client sends for
        # ``side_read_only``, goose through the separate goose home its launch
        # builds for ``side_read_only``. Off the set — or with the harness unknown because
        # the config never loaded — the turn runs the pre-allowance posture: the
        # base agent under REJECT_ALL, and the prompt and fallback say tools are
        # unavailable here.
        tools_available = await _side_read_only_tools(side_backend)
        claude_side = tools_available and is_claude_backend_name(side_backend)
        goose_side = tools_available and side_backend == ACP_BACKEND_GOOSE
        side_session_kwargs: dict[str, Any] = {}
        if goose_side:
            # goose reads no kiro agent spec either. Its launch starts the side
            # session on a goose home of its own, where no permission.yaml can
            # pre-approve a tool and no MCP server is mounted, so every call
            # reaches the READ_ONLY gate below.
            side_agent: str | None = kiro_agent or slot_agent
            approval_policy = ToolApprovalPolicy.READ_ONLY
            binding = (side_agent or "", project or "", "goose_read_only")
            side_session_kwargs["side_read_only"] = True
        elif claude_side:
            # claude reads no kiro agent spec. Its client starts the session with
            # only the read built-ins and no MCP server and pins the asking mode,
            # so every web call reaches the READ_ONLY gate below. Read, Grep and
            # Glob inside the work dir run natively, which is a read, and the
            # session's deny rules keep them off the host's sensitive paths.
            side_agent = kiro_agent or slot_agent
            approval_policy = ToolApprovalPolicy.READ_ONLY
            binding = (side_agent or "", project or "", "claude_read_only")
            side_session_kwargs["side_read_only"] = True
        elif tools_available:
            # The side session runs under ``<agent>--readonly``: the resolved
            # agent's spec with every backend-side grant emptied
            # (``allowedTools: []``, no ``autoApprove``, no ``toolsSettings``
            # ``allowed*``/``trusted*``/``auto*``, no ``includeMcpJson``, an
            # empty KAS policy, no lifecycle hooks). kiro-cli approves a granted
            # tool itself and never raises a permission request, so under the
            # parent's own spec the READ_ONLY gate below would never see the
            # user's main-chat grants — cron removals included. With the derived
            # spec every call kiro-cli does not trust natively raises a request
            # and the gate judges it (kiro-cli runs ``fs_read`` itself — a read,
            # so the guarantee holds outside the gate). Derived
            # from the live base spec on every turn (regenerated when the base
            # changes, written only when the content differs) and published
            # into the kiro agent registry, the only place kiro-cli loads agents
            # from; see ``side_readonly_spec``. File I/O, so off the loop. FAIL
            # CLOSED: a turn whose spec cannot be produced is refused below
            # (``ReadOnlySpecError``) — it never runs under the base agent.
            base_agent = kiro_agent or slot_agent or "kirocrew"
            published = await asyncio.to_thread(publish_readonly_spec, base_agent, project)
            side_agent = published.name
            approval_policy = ToolApprovalPolicy.READ_ONLY
            binding = (published.name, project or "", published.digest)
        else:
            side_agent = kiro_agent or slot_agent
            approval_policy = ToolApprovalPolicy.REJECT_ALL
            binding = (side_agent or "", project or "", "reject_all")

        # The derivation suspended this task; a close (or close+reopen) may have
        # landed meanwhile. A stale task must not touch any session: its own
        # generation's session was destroyed by the close, and the replacement's
        # lives under another key it must never acquire.
        if slot._side is not side_at_start:
            logger.info(
                "Side turn dropped before acquisition: sidecar replaced for slot=%s run_id=%s",
                slot.key,
                run_id,
            )
            return

        # ``get_or_create`` reuses a live session for this key whatever agent or
        # cwd the call asks for, and kiro-cli read the spec at spawn. A session
        # retained from an earlier turn on THIS sidecar may therefore be running
        # under another agent (the slot's agent changed), in another project (its
        # files, its governance profile), or under a derived spec whose content
        # the base has since changed. None of those may serve this turn: when the
        # binding recorded on the sidecar is not exactly this one — or a live
        # session exists that no sidecar binding vouches for — the session is
        # destroyed and the acquisition below cold-starts under the derived spec.
        # The key is this sidecar's alone, so no other turn shares the session.
        if state.sessions.get_provider(side_key) is not None and side_at_start.binding != binding:
            logger.info(
                "Side turn: rebinding side session for slot=%s (agent/cwd/spec changed); "
                "cold-starting under %s",
                slot.key,
                side_agent,
            )
            await state.sessions.destroy(side_key)
            # The destroy suspended this task too: a close landing meanwhile
            # destroyed this same key, and acquiring it again would leave a
            # session no sidecar owns.
            if slot._side is not side_at_start:
                logger.info(
                    "Side turn dropped after rebind: sidecar replaced for slot=%s run_id=%s",
                    slot.key,
                    run_id,
                )
                return

        provider, is_new, resumed = await state.sessions.get_or_create(
            side_key,
            agent=side_agent,
            # The session must run in the slot's project directory for a
            # project-scope agent resolved above to be loadable at all:
            # kiro-cli resolves --agent against $PWD/.kiro/agents, so creating
            # the side session without this cwd has set_mode reject the very
            # name resolve_agent_bindings just returned. Mirrors the dashboard
            # turn's _turn_admit, which passes the same cwd for the same
            # reason. The derived spec itself lives in the user-level registry,
            # which kiro-cli searches after the project scope.
            cwd=project,
            start_priority=start_priority,
            **side_session_kwargs,
        )
        acquired_key = side_key
        # ``get_or_create`` suspended this task as well. A close landing during
        # it destroyed this key — before or after the creation completed — so
        # the session just acquired belongs to no sidecar and nothing else will
        # ever destroy it. The finally releases the lease; the session goes now.
        if slot._side is not side_at_start:
            logger.info(
                "Side turn dropped after acquisition: sidecar replaced for slot=%s run_id=%s",
                slot.key,
                run_id,
            )
            await state.sessions.destroy(side_key)
            return
        side_at_start.binding = binding
        # The full envelope (instructions, parent snapshot, side history, the
        # boundary prompt) goes to a session that has no framing yet. That is
        # the sidecar's first turn — and equally a session cold-started on a
        # later turn: the rebind above, or an eviction after the provider died.
        # A follow-up into a live session, or one kiro-cli resumed with its own
        # history, gets the bare question. Judged on what ``get_or_create``
        # actually did, not on the sidecar's transcript alone, so the model
        # behind a fresh process is never handed a bare question with nothing
        # telling it where it is or what it may do.
        message = build_side_message(
            slot,
            question,
            is_first_turn=is_first_turn or (is_new and not resumed),
            tools_available=tools_available,
            shell_available=not (claude_side or goose_side),
            file_reads_available=not goose_side,
            cfg=cfg,
        )
        try:
            returned_text = await stream_and_collect(
                provider,
                message,
                # Reads-mode semantics with reject as the fallback on a harness in
                # ACP_BACKENDS_SIDE_READONLY: provably read-only calls run,
                # everything else is refused (side chat has no approval card to
                # fall back to); REJECT_ALL on any other harness (see
                # ``tools_available`` above). The gate's
                # identity is the SIDE key: ``sel._infer_source`` classifies
                # ``side:*`` as the dashboard surface, so a dashboard-bound
                # governance profile binds this turn exactly as it binds the
                # parent slot, while SEL rows and the ACP session stay keyed to
                # the side session rather than the parent's.
                approval_policy=approval_policy,
                hooks=side_hooks,
                session_key=side_key,
                agent=slot_agent or "kirocrew",
                app=slot._app or "",
                on_chunk=_on_chunk,
                on_steer_consumed=_on_steer_consumed,
                on_tool_call=_on_tool_call,
            )
            # The reply as stored and as the terminal frame carries it (that
            # frame replaces the streamed deltas) is the redactor's pass over
            # the text it saw chunk by chunk, not redact() over the text
            # stream_and_collect returns: the two differ only where the stream
            # redactor dropped an over-long value, which the batch patterns
            # could not judge and must not get back. Never trust LLM output on
            # an external surface. redact() applies BOTH passes —
            # redact_exfiltration_urls() then redact_credentials() (security.py)
            # — so exfil URLs and credentials are both scrubbed here.
            response_text = _redactor.reply()
        except PromptBusyExhaustedError:
            logger.warning(
                "Side turn aborted (prompt busy exhausted): slot=%s run_id=%s",
                slot.key,
                run_id,
            )
            response_text = _redactor.reply()
            notice = "(side conversation interrupted — please retry)"
            # The partial answer stays in the history the model is handed; the
            # notice is stored after it, so a panel restored from ``GET .../side``
            # draws the turn as these frames do: what streamed, then the notice.
            # The answer goes out only when this sidecar stored it: a conversation
            # closed and reopened meanwhile must not receive the old run's text.
            partial_message_id = ""
            message_id = ""
            if slot._side is not None and slot._side.open and slot._side.last_run_id == run_id:
                if response_text:
                    partial_message_id = slot._side.append_assistant(response_text, run_id=run_id)
                message_id = slot._side.append_assistant(notice, run_id=run_id, is_error=True)
            if partial_message_id:
                # A tool row split the partial answer: the frame settles the row
                # below the last tool row, as the success path's does.
                split = bool(_redactor.rows)
                _broadcast(
                    role="assistant",
                    content=_redactor.finish() if split else response_text,
                    final=True,
                    message_id=partial_message_id,
                    reply=response_text if split else "",
                )
            _broadcast(
                role="assistant",
                content=notice,
                is_error=True,
                final=True,
                message_id=message_id,
            )
            return

        if not returned_text:
            # The returned text, not the streamed chunks: a prompt-busy retry starts
            # its text over, so chunks from an earlier attempt can stand beside an
            # empty answer.
            # Same vocabulary as the composer footer
            # (``pages.chat.sideChat.context_only_tools_unavailable`` on a
            # read-only harness, ``…_backend`` elsewhere): the strings describe one
            # boundary and must not drift.
            response_text = (
                (
                    "Side Chat is read-only: lookups work here, but changes don't. "
                    "Use the main chat to take action."
                )
                if tools_available
                else (
                    "Side Chat can't use tools on this agent backend. "
                    "Use the main chat to take action."
                )
            )
            logger.info(
                "Side turn produced no text (tool rejection): " "slot=%s run_id=%s",
                slot.key,
                run_id,
            )

        message_id = ""
        if slot._side is not None and slot._side.open and slot._side.last_run_id == run_id:
            message_id = slot._side.append_assistant(response_text, run_id=run_id)

        # The terminal frame replaces what the client streamed for the row it
        # lands on. With no tool row that row holds the whole reply, and the
        # frame carries the whole redacted text as before. A tool row split the
        # reply into rows, and the frame then carries only the last row's text:
        # the whole reply redacted in one pass (so a pattern that spans a tool
        # row is matched whole), past what the rows above show. A turn that ran
        # tools but wrote nothing keeps the fallback sentence above.
        final_text = response_text
        whole_reply = ""
        if _redactor.rows and returned_text:
            final_text = _redactor.finish()
            whole_reply = response_text
        _broadcast(
            role="assistant",
            content=final_text,
            ts=time.time(),
            final=True,
            message_id=message_id,
            reply=whole_reply,
        )
    except asyncio.CancelledError:
        # A stop request (``api_side_stop``) cancels this task. The terminal
        # frame that clears the client's streaming/pending is broadcast by that
        # handler, not here: a cancel can interrupt this task mid-await, and the
        # handler is the one place that knows a cancel is deliberate rather than
        # a shutdown. Mark it so the finally holds the queue instead of draining.
        cancelled = True
        raise
    except AcpAuthRequired as exc:
        # A signed-out CLI is actionable, so surface its own message rather than
        # the generic failure below — the side panel has no other channel to tell
        # the user what to do. Latch the service signed-out too, so the
        # fail-closed gates stop trusting a stale ready value.
        logger.warning("Side turn auth required: slot=%s run_id=%s", slot.key, run_id)
        # Non-retryable: every queued question would hit the same wall. Draining
        # would spend the whole queue on identical failures and leave nothing to
        # resume after signing in, so the queue is held intact below. Mirrors the main
        # chat, which already makes this call in `_on_auth_required` (chat_turn/turn_failures.py).
        auth_required = True
        # Local import: chat_runner imports from this package, so a module-level
        # import would close a cycle.
        from kiro_crew.dashboard.chat_runner import _mark_kiro_signed_out

        _mark_kiro_signed_out(state)
        _broadcast(role="assistant", content=str(exc), is_error=True, final=True)
    except _AppAgentNotLoaded as exc:
        # Terminal for this turn and deliberately not a generic failure: the
        # message names the agent and says what to do, which the catch-all below
        # would replace with "see server logs". No session was created, so there
        # is nothing to release. The queue is NOT held (unlike the auth arm
        # above): the self-heal rungs re-run per turn, so the next queued
        # question is a real retry rather than a repeat of a settled refusal.
        logger.warning(
            "Side turn: app agent not loaded for slot=%s run_id=%s: %s",
            slot.key,
            run_id,
            exc,
        )
        _broadcast(role="assistant", content=str(exc), is_error=True, final=True)
    except ReadOnlySpecError as exc:
        # Terminal for this turn, and never a fallback to the base agent: without
        # the derived spec the READ_ONLY gate would not see the base agent's
        # grants, so refusing is the only safe answer. No session was created, so
        # there is nothing to release; the queue is not held, because the spec is
        # re-derived per turn and the next question is a real retry. The stable
        # code is for the operator: it goes in the log line, not in the panel,
        # where a user's vocabulary belongs.
        logger.warning(
            "Side turn refused: read-only agent spec unavailable (%s) for slot=%s run_id=%s: %s",
            exc.code,
            slot.key,
            run_id,
            exc.detail,
        )
        try:
            sel().log_api_access(
                caller=slot._app or "dashboard",
                operation="chat.side_turn",
                outcome="denied",
                source="dashboard",
                resources=f"slot={slot.key} run_id={run_id} code={exc.code}",
                error=f"read-only agent spec unavailable: {exc.detail}",
            )
        except Exception:  # noqa: BLE001 — the refusal must reach the panel regardless
            logger.debug("SEL audit unavailable for side-turn refusal", exc_info=True)
        _broadcast(
            role="assistant",
            content="Side Chat couldn't start. Try again, or use the main chat.",
            is_error=True,
            final=True,
        )
    except Exception:
        logger.exception(
            "Side turn failed: slot=%s run_id=%s",
            slot.key,
            run_id,
        )
        _broadcast(
            role="assistant",
            content="(side conversation failed — see server logs)",
            is_error=True,
            final=True,
        )
    finally:
        # Identity-check run_id so a stale task from a closed-and-reopened
        # side never flips is_complete on the new state's in-flight turn.
        if slot._side is not None and slot._side.last_run_id == run_id:
            slot._side.is_complete = True
            # Drop the handle this run registered: it is done, and a stop
            # request landing after this must not cancel a task that already
            # finished (or, worse, the NEXT turn once the drain below starts
            # one). The identity gate above already excludes a superseded
            # sidecar, whose own task handle this task never owned.
            slot._side.task = None
        if acquired_key:
            try:
                state.sessions.release(acquired_key)
            except Exception:
                logger.debug(
                    "Failed to release side session %s",
                    acquired_key,
                    exc_info=True,
                )
        # Release first, then drain: the next turn re-acquires the same isolated
        # session, so dispatching before the release would leave it held twice.
        _requeue_unconsumed_side_steers(state, slot, run_id, side_at_start)
        # Unconsumed steers are still requeued above — they were never delivered
        # either, and the head of the queue is where a resume should find them.
        # Only the DISPATCH is withheld, so nothing is lost, merely paused.
        if not auth_required and not cancelled:
            _drain_side_queue(state, slot, run_id)


async def api_side_open(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/open — idempotent sidecar init."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    own = _check_slot_ownership(request, slot, "chat.side_open")
    if own is not None:
        return own

    if slot._side is None or not slot._side.open:
        slot._side = SideState(open=True, created_at=_now_iso())
        outcome = "opened"
    else:
        outcome = "reopened"

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_open",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},result={outcome}",
    )
    return web.json_response(
        {
            "ok": True,
            "open": True,
            "messages": len(slot._side.messages),
            "last_run_id": slot._side.last_run_id,
            "created_at": slot._side.created_at,
        }
    )


def _wire_side_row(entry: dict[str, Any]) -> dict[str, Any]:
    """One stored side row as ``GET .../side`` returns it, redacted like its frame."""
    row: dict[str, Any] = {
        "id": entry.get("id", ""),
        "role": entry.get("role", ""),
        "content": redact(entry.get("content", "") or ""),
        "ts": entry.get("ts", ""),
    }
    if entry.get("run_id"):
        row["run_id"] = entry["run_id"]
    if entry.get("steer"):
        row["steer"] = True
    if entry.get("is_error"):
        row["is_error"] = True
    return row


async def api_side_state(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/side — the side conversation this gateway holds.

    A reloaded panel restores from this: the transcript, the queue and whether a
    turn is still running. It reads the sidecar in memory and writes nothing, so
    a gateway restart still ends the conversation.

    Owner-only, for every caller: the ``chat.side_result`` and ``chat.side_queue``
    frames reach owner sockets only, and this read answers the same audience,
    including for an app on a slot it owns. Every text is redacted the way those
    frames redact it, so the read reveals nothing a frame would not have.
    """
    state: DashboardState = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if not slot:
        return slot_not_found()

    own = _check_slot_ownership(request, slot, "chat.side_state")
    if own is not None:
        return own

    # Body-scope import, for the cycle ``api_side_turn`` explains.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "chat.side_state")
    if owner_denied is not None:
        return owner_denied

    side = slot._side
    if side is None or not side.open:
        is_open, message_count, queue_count = False, 0, 0
        payload: dict[str, Any] = {"open": False}
    else:
        is_open, message_count, queue_count = True, len(side.messages), len(side.queue)
        payload = {
            "open": True,
            "created_at": side.created_at,
            "last_run_id": side.last_run_id,
            # The busy gate ``api_side_turn`` applies, so the panel offers steer and
            # queue exactly when a submit would get them.
            "running": side.turn_in_flight,
            "messages": [_wire_side_row(entry) for entry in side.messages],
            "queue": [
                {
                    "id": entry["id"],
                    "content": redact(entry["content"]),
                    "ts": entry["ts"],
                    "rev": entry.get("rev", 0),
                }
                for entry in side.queue
            ],
        }
    # Counted from the side state, not the redacted payload: the audit row carries no side text.
    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_state",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},open={is_open},messages={message_count},queue={queue_count}",
    )
    return web.json_response(payload)


async def api_side_tools(request: web.Request) -> web.Response:
    """GET /api/chat/side/tools — whether a side turn runs read-only tools.

    Not per-slot: the answer is the configured harness and the host's adapter.
    ``claude_adapter_outdated`` says the only thing missing is a newer
    claude-agent-acp, so the footer can tell the user to update it.
    ``claude_adapter_min_version`` and ``claude_adapter_update_command`` are the
    floor and the ``npm i -g`` step that footer names, read from the constants
    the turn's own check uses so the copy cannot drift from them.
    ``config_unavailable`` says the config did not load, so the turn runs with
    no tools for that reason rather than because of the harness.
    """
    config_unavailable = False
    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        side_backend: str | None = cfg.agent.acp_backend
    except Exception:
        logger.debug("Side tools: config load failed", exc_info=True)
        side_backend = None
        config_unavailable = True
    read_only_tools = await _side_read_only_tools(side_backend)
    claude_adapter_outdated = (
        not read_only_tools
        and side_backend in ACP_BACKENDS_SIDE_READONLY
        and is_claude_backend_name(side_backend)
    )
    return web.json_response(
        {
            "read_only_tools": read_only_tools,
            "claude_adapter_outdated": claude_adapter_outdated,
            "claude_adapter_min_version": claude_adapter_min_version(),
            "claude_adapter_update_command": claude_adapter_install_command(),
            "config_unavailable": config_unavailable,
        }
    )


async def api_side_turn(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/turn — body ``{"question": str, "steer"?: bool}``.

    Three outcomes, all returning immediately:

    - idle → starts a turn, ``{ok, run_id}``; the stream runs as a background
      task and chunks are broadcast on ``chat.side_result``.
    - in flight + ``steer`` → injects into the RUNNING turn, ``{ok, steered}``.
    - in flight (or a steer that could not be delivered) → queues behind the
      turn, ``{ok, queued, queue_id, depth}``.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    # Owner identity is a property of a dashboard-user request: ``app == ""`` is
    # the class ``is_owner_dashboard_request`` can rule on at all. An app token
    # carries its app's name and keeps the slot scoping below unchanged.
    if request.get("app") == "":
        # Body-scope import, like the sibling gates in this package
        # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
        # reaches back into sibling handler modules, so importing the helper at
        # module scope from here would close a cycle.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "chat.side_turn")
        if owner_denied is not None:
            return owner_denied

    own = _check_slot_ownership(request, slot, "chat.side_turn")
    if own is not None:
        return own

    if not request.body_exists:
        return web.json_response({"error": "missing JSON body"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object"},
            status=400,
        )

    question = body.get("question")
    if not isinstance(question, str):
        return web.json_response(
            {"error": "question must be a string"},
            status=400,
        )
    question = question.strip()
    if not question:
        return web.json_response(
            {"error": "question must not be empty"},
            status=400,
        )
    if len(question.encode("utf-8")) > _MAX_QUESTION_BYTES:
        return web.json_response(
            {"error": f"question too long (max {_MAX_QUESTION_BYTES} bytes)"},
            status=400,
        )

    # Check after body parse: ``api_side_close`` may have landed during
    # ``await request.json()``.
    if slot._side is None or not slot._side.open:
        return web.json_response(
            {"error": "side conversation is not open"},
            status=409,
        )

    if slot._side.turn_in_flight:
        # A turn is in flight. Mirror the main chat: steer injects into the
        # RUNNING turn, queue defers it to the next one, and an unavailable
        # steer falls through to the queue so the text is NEVER dropped.
        #
        # ``is_stopping`` is folded into "in flight" so a stop in progress keeps
        # the turn busy through its cleanup: the cancelled turn's own ``finally``
        # flips ``is_complete`` True as it unwinds, which lands during the stop
        # handler's bounded await, and without this clause a successor could
        # dispatch in that window before the stopped row is recorded.
        # Bind the commit to the sidecar OBJECT and run the steer was aimed at,
        # captured before the RPC suspends. Re-reading them afterwards would
        # attribute a steer to whatever is live now: a close+reopen swaps in a
        # fresh SideState that is also ``open``, and a completed turn's drain can
        # already have started the next run.
        side_before = slot._side
        run_before = side_before.last_run_id
        if body.get("steer"):
            steer_id = await _try_side_steer(state, slot, question)
            # Identity is checked on BOTH outcomes: a close+reopen during the RPC
            # leaves ``slot._side`` pointing at a fresh, also-``open`` sidecar, and
            # queueing this question onto it would put it in a side conversation
            # that never asked for it.
            if slot._side is not side_before or not side_before.open:
                return web.json_response(
                    {
                        "error": "side conversation is not open",
                        "code": "side_not_open",
                    },
                    status=409,
                )
            if steer_id is not None:
                ledger = side_before.steer_state(steer_id)
                # CONSUMED is the backend's proof of injection, and it outranks
                # turn completion: the question reached a generation and was
                # answered, so it belongs in the transcript even though the turn
                # has since ended. The echo callback has ALREADY appended and
                # broadcast it — this branch only reports the outcome, because two
                # writers would render the chip twice.
                if ledger == STEER_CONSUMED:
                    sel().log_api_access(
                        caller=request.get("app", "") or "dashboard",
                        operation="chat.side_turn",
                        outcome="allowed",
                        source="dashboard",
                        resources=(
                            f"slot={slot.key},run_id={run_before},"
                            f"mode=steer,ledger={ledger},"
                            f"question_len={len(question)}"
                        ),
                    )
                    return web.json_response({"ok": True, "steered": True, "run_id": run_before})
                if ledger == STEER_PENDING and (
                    side_before.last_run_id == run_before and not side_before.is_complete
                ):
                    # Genuinely in flight, consumption unproven. Report that
                    # instead of claiming delivery: the outcome arrives as a frame
                    # — the chip if the echo proves it, a queue card if the turn's
                    # finally requeues it. The ledger holds the text either way.
                    #
                    # Pending but the run has MOVED ON is a different case and must
                    # keep falling through below: nobody is going to consume it, so
                    # it becomes a queue card now rather than waiting on a frame
                    # that will never come.
                    return web.json_response(
                        {
                            "ok": True,
                            "steered": False,
                            "pending": True,
                            "run_id": run_before,
                            # So the submitter can recognise the queue card this
                            # steer becomes if it is never consumed, and restore
                            # its own RAW text rather than the redacted broadcast.
                            "steer_id": steer_id,
                        }
                    )
                if ledger in (STEER_REQUEUED, None):
                    # REQUEUED: the turn's finally already made a card.
                    # None: the entry aged out of the ledger, so its outcome was
                    # decided by someone else. Either way a second entry here
                    # would ask the same question twice, which is the one
                    # direction worse than a duplicate-looking response.
                    return web.json_response(
                        {
                            "ok": True,
                            "queued": True,
                            "demoted": True,
                            "depth": len(side_before.queue),
                            # The card already exists — the turn's finally made it
                            # while the steer RPC was awaiting — so this response is
                            # the submitter's only chance to learn a handle for it.
                            # Its queue id is unknown here; the ledger id is what the
                            # requeue broadcast carries, so it is what correlates.
                            "steer_id": steer_id,
                        }
                    )
                # Still pending but the run moved on: nobody else has claimed it,
                # so take it and queue it below.
                side_before.steer_mark(steer_id, STEER_REQUEUED)

        side = slot._side
        if side is None or not side.open:
            return web.json_response(
                {"error": "side conversation is not open", "code": "side_not_open"},
                status=409,
            )
        qid = side.queue_append(question)
        if qid is None:
            sel().log_api_access(
                caller=request.get("app", "") or "dashboard",
                operation="chat.side_turn",
                outcome="denied",
                source="dashboard",
                resources=f"slot={slot.key},depth={len(side.queue)}",
                error="side queue full",
            )
            return web.json_response(
                {
                    "error": (
                        f"side queue is full (max {MAX_SIDE_QUEUE}) — "
                        "wait for the current turn or cancel a queued message"
                    ),
                    "code": "side_queue_full",
                    "depth": len(side.queue),
                },
                status=429,
            )
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=question,
            depth=len(side.queue),
        )
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="chat.side_turn",
            outcome="allowed",
            source="dashboard",
            resources=(
                f"slot={slot.key},mode=queue,queue_id={qid},"
                f"depth={len(side.queue)},question_len={len(question)}"
            ),
        )
        # The turn may have finished during the steer attempt above. The drain
        # hook only runs inside a turn's finally, so an entry queued after that
        # point would sit forever — kick the drain here.
        if side.is_complete:
            _drain_side_queue(state, slot, side.last_run_id)
        return web.json_response(
            {
                "ok": True,
                "queued": True,
                "queue_id": qid,
                # Whether the entry is STILL queued as of this response. A turn
                # that finished during the request can have drained it already,
                # and a client that materialised a card for it would show one the
                # server no longer has — cancelling that card 404s.
                "still_queued": any(e["id"] == qid for e in side.queue),
                "depth": len(side.queue),
                # Tell a user who pressed "Steer" that the turn ended under them,
                # so the queue card is not the only hint that the mode changed.
                **({"demoted": True} if body.get("steer") else {}),
            }
        )

    idle_side = slot._side
    if idle_side is not None and idle_side.queue:
        # Idle, but questions are already waiting — the auth hold leaves exactly
        # this state. Dispatching now would run the newest question first and leave
        # the earlier ones behind it, so join the tail and let the head go next.
        # This is also what restarts a held queue: the drain below moves it.
        qid = idle_side.queue_append(question)
        if qid is None:
            return web.json_response(
                {
                    "error": (
                        f"side queue is full (max {MAX_SIDE_QUEUE}) — "
                        "wait for the current turn or cancel a queued message"
                    ),
                    "code": "side_queue_full",
                    "depth": len(idle_side.queue),
                },
                status=429,
            )
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=question,
            depth=len(idle_side.queue),
        )
        _drain_side_queue(state, slot, idle_side.last_run_id)
        return web.json_response(
            {
                "ok": True,
                "queued": True,
                "queue_id": qid,
                "still_queued": any(e["id"] == qid for e in idle_side.queue),
                "depth": len(idle_side.queue),
            }
        )

    # The owner asking is waiting on the panel; an app token is not. A queued
    # question drains BACKGROUND, onto the side session the turn before it warmed.
    run_id = _dispatch_side_turn(
        state, slot, question, start_priority=owner_start_priority(request)
    )

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_turn",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"slot={slot.key},run_id={run_id},"
            f"messages={len(slot._side.messages)},"
            f"question_len={len(question)}"
        ),
    )
    return web.json_response(
        {
            "ok": True,
            "run_id": run_id,
            "messages": len(slot._side.messages),
        }
    )


async def api_side_stop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/stop — cancel the in-flight side turn.

    The escape hatch for a hung side turn. ``/interrupt`` targets the slot's
    MAIN run (``slot.running``); a side turn runs on a separate background
    asyncio task whose handle lives on the sidecar (``slot._side.task``), so it
    needs its own cancel path. Cancelling the task alone is not enough — the
    client's ``streaming``/``pending`` only clears on a terminal frame — so a
    terminal ``chat.side_result`` is broadcast here, where a cancel is known to
    be deliberate (``_run_side_turn`` cannot tell a stop from a shutdown).

    Idempotent: with no turn in flight (or a stop already claimed) it returns
    ``{"ok": True}`` without settling. The SEL ``resources=`` line records the
    ``stopped``/``settled`` disposition for audit; the wire body stays minimal
    because the frontend fires the mutation and wires only ``onError``.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    # Owner-dashboard gate, mirroring ``api_side_turn``: stopping a turn is a
    # dashboard-user action, and an app token keeps the slot scoping below.
    if request.get("app") == "":
        # circular import: handlers._shared pulls in sibling handler modules in
        # this package at import time, so a module-level import here risks closing
        # the cycle; bind it at call time (mirrors api_side_turn above).
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "chat.side_stop")
        if owner_denied is not None:
            return owner_denied

    own = _check_slot_ownership(request, slot, "chat.side_stop")
    if own is not None:
        return own

    side = slot._side
    if side is None or not side.open:
        return web.json_response(
            {"error": "side conversation is not open", "code": "side_not_open"},
            status=409,
        )

    run_id = side.last_run_id
    task = side.task
    # Nothing to stop: no run id, the turn already settled, or no live handle.
    # Report it rather than erroring — a stop pressed just as the turn finished
    # is a race the UI must survive, not a failure.
    if not run_id or side.is_complete or task is None or task.done():
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="chat.side_stop",
            outcome="allowed",
            source="dashboard",
            resources=f"slot={slot.key},stopped=false",
        )
        return web.json_response({"ok": True})

    # Atomically CLAIM the turn before the first await by clearing the HANDLE
    # (not ``is_complete``). This runs synchronously after the early-out above
    # with no await between the two, so in asyncio's single-threaded model the
    # check-and-claim is atomic: a second concurrent stop (ordinary double-click,
    # or two dashboard tabs) that races in while we are awaiting
    # ``provider.cancel()`` / the task cleanup below re-reads ``side.task`` at the
    # early-out, sees it is ``None``, and returns ``{ok}`` without settling — so
    # only ONE request appends the stopped row and broadcasts the terminal frame.
    #
    # We MUST claim via ``side.task`` and NOT via ``is_complete``: a concurrent
    # ``POST /side/turn`` suspended at ``await request.json()`` resumes to read
    # the busy gate ``last_run_id and not is_complete`` — flipping
    # ``is_complete`` True here would read as idle, dispatching a replacement R2
    # over the still-cancelling R1, after which the stop handler sees the
    # replacement and returns ``superseded``, leaving R1's user line unanswered
    # in the history fed to the next turn (a security-class gap). Leaving
    # ``is_complete`` False keeps both ``api_side_turn`` and ``_drain_side_queue``
    # seeing the turn as in flight, so no replacement is dispatched during the
    # cancel window. ``task`` is already captured above, so clearing the field
    # does not lose the handle we still cancel+await.
    #
    # Clearing ``side.task`` is NOT sufficient on its own: the cancelled turn's
    # own ``finally`` flips ``is_complete`` True synchronously as it unwinds, and
    # that lands DURING the ``await`` of the task cleanup below — reopening the
    # busy gate before this handler resumes to record the stopped row. So we also
    # raise ``is_stopping``, which the busy gate honours as "in flight", holding
    # the turn busy until we clear it in the ``finally`` after settling. Both writes
    # are a single synchronous claim with no
    # await between them, so the check-and-claim stays atomic.
    side.task = None
    side.is_stopping = True

    try:
        return await _cancel_and_settle_side_turn(request, state, slot, side, task, run_id)
    finally:
        # Release the busy claim on EVERY exit path (settled, superseded,
        # timeout, or an unexpected error): a stop that failed to clear it would
        # wedge the sidecar busy forever, so no further turn could ever start —
        # a worse hang than the one the endpoint exists to escape. By here the
        # terminal row (if any) is already recorded, so admitting a successor is
        # safe.
        side.is_stopping = False


async def _cancel_and_settle_side_turn(
    request: web.Request,
    state,
    slot,
    side,
    task: asyncio.Task[None],
    run_id: str,
) -> web.Response:
    """Cancel the in-flight side turn and settle the client with a terminal row.

    Factored out of :func:`api_side_stop` so the caller can hold the
    ``is_stopping`` busy claim across the whole cancel+settle in a ``try`` and
    release it in a ``finally`` on every exit path. Assumes the caller has
    already claimed the turn (``side.task = None``, ``side.is_stopping = True``).
    """
    # whose ``finally`` releases the lease but never sends ``session/cancel`` —
    # so without this the stopped turn keeps generating (and billing) and the
    # next side question pays the prompt-busy retry cycle. ``provider.cancel()``
    # is the typed protocol cancel (AcpProvider.cancel routes it to the backend
    # and swallows a dead-runtime error), so it cannot raise out of this handler.
    provider = state.sessions.get_provider(_side_session_key(slot.key, side.gen))
    if provider is not None:
        await provider.cancel()

    # Cancel the task, then AWAIT its cancellation cleanup before publishing
    # idle. ``_run_side_turn`` re-raises CancelledError (marking the turn
    # ``cancelled`` so its finally holds the queue) and its finally releases the
    # session lease and flips ``is_complete``. Awaiting it here closes a race:
    # a concurrent side-turn request that replaced ``last_run_id``
    # before the cleanup ran would make the cancelled turn's identity-gated
    # requeue skip, losing a pending steer. Awaiting (bounded, shielded from a
    # cancel of THIS request) lets that cleanup finish first.
    task.cancel()
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_STOP_CLEANUP_TIMEOUT)
    except asyncio.TimeoutError:
        # The task's finally is wedged (cleanup did not finish within the
        # budget). We must still settle the client regardless — leaving it
        # unsettled is the exact deep-hang the endpoint exists to escape.
        timed_out = True
    except (asyncio.CancelledError, Exception):
        # CancelledError is the task's own expected unwind; any other exception
        # is the turn's own error, already surfaced on its frame.
        pass

    # Decide whether to settle with a stopped row. Two conditions must hold:
    #
    #   1. No genuine replacement took over. ``provider.cancel()`` can end the
    #      prompt NORMALLY (no CancelledError); the turn's own finally then flips
    #      ``is_complete`` and ``_drain_side_queue`` can dispatch the NEXT queued
    #      turn onto the same ``side:<slot>:<gen>`` key during the awaits above.
    #      If that happened, ``last_run_id`` now belongs to that replacement, so
    #      settling here would erase its live handle. The cancelled turn's own
    #      finally never changes ``last_run_id`` (and the drain that would start
    #      a replacement is skipped only when ``cancelled`` is True), so a
    #      changed ``last_run_id`` is the replacement signal. We also require the
    #      same sidecar object.
    #
    #   2. The task was actually CANCELLED (or the cleanup WEDGED). If
    #      ``provider.cancel()`` let the prompt finish normally before
    #      ``task.cancel()`` landed, the turn took its SUCCESS path — the real
    #      answer was appended and broadcast — and ``task.cancelled()`` is False
    #      with the task genuinely done. Appending a ``(side response stopped)``
    #      row on top of a delivered answer would corrupt the transcript, so a
    #      genuinely-normal completion gets no row. But a TIMEOUT
    #      is NOT a normal completion: the task is still running, its finally is
    #      wedged, and ``task.cancelled()`` is also False — folding that into the
    #      normal-completion branch would leave the panel streaming forever in
    #      the exact deep-hang the endpoint exists to escape. So
    #      we settle the stopped row whenever the task was cancelled OR the
    #      cleanup timed out, and skip it only on a genuine normal completion.
    settled_as = None
    if not (slot._side is side and side.last_run_id == run_id):
        settled_as = "superseded"
    elif timed_out:
        # Wedged cleanup: fall through to settle the client with the stopped row
        # below (do NOT classify as completed-normally). SEL records the wedge.
        settled_as = None
    elif not task.cancelled():
        # Turn ended normally during the cancel window; its own answer already
        # settled the row. Nothing to append — just report the stop as a no-op
        # settle (the turn's own finally flips ``is_complete``, so the panel is
        # consistent).
        settled_as = "completed-normally"

    if settled_as is not None:
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="chat.side_stop",
            outcome="allowed",
            source="dashboard",
            resources=f"slot={slot.key},stopped=true,settled={settled_as}",
        )
        return web.json_response({"ok": True})

    # Mark the turn settled. On a real cancel the task's own finally also flips
    # this (idempotent); on a WEDGED timeout the finally never ran, so without
    # this the panel's sidecar would stay ``is_complete=False`` after we settle
    # the client, re-reading as busy. ``side.task`` was already cleared at the
    # claim above.
    side.is_complete = True

    # Record the stop in the sidecar transcript so the next turn does not read
    # the stopped question as an open, unanswered ``User:`` line. Without this
    # ``side.messages`` ends ``[user: Q1]`` and ``_format_side_history`` hands
    # the model Q1 as still-pending.
    message_id = side.append_assistant("(side response stopped)", run_id=run_id, is_error=True)

    # The terminal frame the client needs to clear streaming/pending. A fresh
    # run_id would read as a new turn's answer; this one matches the row the
    # hung turn was streaming into, so it settles that row.
    broadcast_side_result(
        state,
        slot_key=slot.key,
        run_id=run_id,
        role="assistant",
        content="(side response stopped)",
        is_error=True,
        final=True,
        message_id=message_id,
    )

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_stop",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"slot={slot.key},run_id={run_id},stopped=true,"
            f"settled={'timeout' if timed_out else 'cancelled'}"
        ),
    )
    # The SEL ``resources=`` line above records ``run_id``/``stopped`` for
    # audit; the frontend ignores the response body (fires the mutation,
    # wires only ``onError``), so the success body is the minimal ``{ok}``.
    return web.json_response({"ok": True})


async def api_side_queue_cancel(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot}/side/queue/{queue_id} — drop one entry.

    The frontend moves the cancelled text back into its composer, so the content
    is echoed back (redacted) rather than discarded silently.
    """
    state: DashboardState = request.app["state"]
    # Read the body FIRST: this is the only await in the handler, and everything after it
    # depends on `_side` still existing. A concurrent `/side/close` clears `_side`, so an await
    # placed after the entry is removed would raise with the entry already gone and no
    # broadcast sent — losing the question outright.
    #
    # The tab id lets other tabs drop the card without also pasting the question into their own
    # composer. Absent or malformed is fine: consumers then fall back to releasing.
    origin_client = ""
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("client"), str):
            origin_client = body["client"][:128]
    except Exception:
        origin_client = ""
    slot, err = _resolve_side_queue_slot(request, "chat.side_queue_cancel")
    if err is not None:
        return err
    # Bound to a local so that adding an await below could not silently reintroduce the crash
    # through `slot._side` being replaced or cleared underneath.
    side = slot._side
    queue_id = request.match_info["queue_id"]
    content = side.queue_remove(queue_id)
    if content is None:
        return web.json_response(
            {"error": "queue entry not found", "code": "queue_entry_not_found"},
            status=404,
        )
    broadcast_side_queue(
        state,
        slot_key=slot.key,
        action="cancel",
        queue_id=queue_id,
        content=content,
        depth=len(side.queue),
        origin_client=origin_client,
    )
    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_queue_cancel",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},queue_id={queue_id},depth={len(slot._side.queue)}",
    )
    return web.json_response(
        {"ok": True, "content": redact(content), "depth": len(slot._side.queue)}
    )


async def api_side_queue_edit(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/side/queue/{queue_id} — body ``{"content"}``.

    Rewrites one queued entry in place; queue order is preserved.
    """
    state: DashboardState = request.app["state"]
    # Body first: `_side` must not be resolved across a suspension point, because a concurrent
    # `/side/close` clears it and every use below would raise.
    parsed = await _read_side_queue_body(request)
    if isinstance(parsed, web.Response):
        return parsed
    slot, err = _resolve_side_queue_slot(request, "chat.side_queue_edit")
    if err is not None:
        return err
    # Re-read after the body await: a close (or a drain) may have landed.
    side = slot._side
    if side is None or not side.open:
        return web.json_response(
            {"error": "side conversation is not open", "code": "side_not_open"},
            status=409,
        )
    queue_id = request.match_info["queue_id"]
    rev = side.queue_edit(queue_id, parsed)
    if rev is None:
        return web.json_response(
            {"error": "queue entry not found", "code": "queue_entry_not_found"},
            status=404,
        )
    broadcast_side_queue(
        state,
        slot_key=slot.key,
        action="edit",
        queue_id=queue_id,
        content=parsed,
        depth=len(side.queue),
        rev=rev,
    )
    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_queue_edit",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},queue_id={queue_id},content_len={len(parsed)}",
    )
    return web.json_response({"ok": True, "depth": len(side.queue)})


async def api_side_close(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/close — drop sidecar + destroy session.

    Every tab learns of it (``chat.side_closed``): the body's ``client`` names the
    tab that asked, which clears its own panel when it sends the close.
    """
    state: DashboardState = request.app["state"]
    # Read before the sidecar is touched, as the queue cancel does: this is an
    # await, and nothing below may run against a sidecar it has already dropped.
    origin_client = ""
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("client"), str):
            origin_client = body["client"][:128]
    except Exception:
        origin_client = ""
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    own = _check_slot_ownership(request, slot, "chat.side_close")
    if own is not None:
        return own

    was_open = slot._side is not None and slot._side.open
    # The key belongs to the sidecar being closed: read its generation before
    # the sidecar is dropped, so a reopen (a new generation, a new key) that
    # lands while the destroy below is in flight is never the one destroyed.
    closing_side = slot._side
    closing_gen = closing_side.gen if closing_side is not None else ""
    dropped: list[dict[str, str]] = []
    if closing_side is not None:
        for entry in closing_side.queue:
            dropped_entry = {"queue_id": entry["id"]}
            steer_id = entry.get("steer_id")
            if isinstance(steer_id, str) and steer_id:
                dropped_entry["steer_id"] = steer_id
            dropped.append(dropped_entry)
    slot._side = None
    # Sent whether or not one was open: a tab can still show a conversation the
    # gateway lost in a restart, and this is the moment to clear it.
    broadcast_side_closed(
        state,
        slot_key=slot.key,
        origin_client=origin_client,
        dropped=dropped,
    )

    side_key = _side_session_key(slot.key, closing_gen)
    try:
        await state.sessions.destroy(side_key)
    except Exception:
        logger.debug(
            "Failed to destroy side session %s",
            side_key,
            exc_info=True,
        )

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_close",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},was_open={was_open}",
    )
    return web.json_response({"ok": True, "was_open": was_open})
