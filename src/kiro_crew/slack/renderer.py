"""Slack ``Renderer`` for the channel-neutral ``TurnDriver``.

Maps the abstract ``OutputEvent`` stream emitted by
``kiro_crew.messaging.TurnDriver`` onto Slack's streaming + Block Kit surface
via ``SlackClientOps``. This is the Slack-specific half of the neutral
messaging layer.

Import direction: ``slack`` -> ``messaging`` (never the reverse), so this lives
in the ``slack`` package and consumes the neutral ``messaging`` contracts.

``prompt_choice`` (the first-class approval event) renders as Block Kit
approve/deny buttons. The interactive decision is awaited via
:class:`SlackApprovalDecider`, whose future is resolved by the Slack
interaction handler when the user clicks a button.

Two channel-neutral halves do the work this module used to do badly or not at
all:

* **Length splitting** belongs to
  :func:`kiro_crew.messaging.split.split_markdown_safe`, the shared fence-safe
  splitter, so this renderer owns no fence grammar. ``slack/format.py``'s
  ``split_message`` counts backticks and cuts anywhere a newline sits, which
  inverts its own open/closed state on a fence whose content contains one; it
  stays for the native handler's call sites. The splitter's streaming contract is
  consumed as written: every chunk but the last is sealed, the final one is left
  open, and the one documented over-``limit`` case (a whole line placed with its
  fence scaffolding) is bounded again against the limit Slack's own update path
  truncates at.
* **Outbound local-image extraction** belongs to
  :mod:`kiro_crew.messaging.outbound_files`, with Slack's per-file ceiling, count
  cap and ``files_upload_v2`` call in :mod:`kiro_crew.slack.files`. Extraction
  runs once, at the SEMANTIC seal (``on_done``), never on a length cut, so a
  reference is always seen whole and in its original fence context. Slack's
  stream is append-only, so markup is withheld from live frames rather than
  hidden and later edited away, and the withheld tail lands at the seal.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable

from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import (
    OutboundFile,
    Rejection,
    extract_local_refs_off_loop,
    hide_local_refs,
    protected_ref_spans,
)
from kiro_crew.messaging.renderer import Renderer, chunk_text
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.slack.files import UPLOAD_LIMITS, upload_outbound_files
from kiro_crew.slack.format import SLACK_MSG_LIMIT, extract_options, strip_thinking_tags
from kiro_crew.slack.handler import (
    _APPROVAL_TIMEOUT,
    _CURSOR,
    _EDIT_INTERVAL,
    _THINKING,
    StatusReactionController,
    _append_footer_actions,
    _filter_options_brackets,
    _safe_update,
    _tool_to_phase,
    build_timing_footer,
)
from kiro_crew.slack.outbound import PostedOptions
from kiro_crew.slack.transport import SLACK_CAPABILITIES

logger = logging.getLogger(__name__)

#: Block Kit action_id prefixes for tool approve/deny buttons.
TOOL_APPROVE_ACTION_PREFIX = "mc_tool_approve_"
TOOL_DENY_ACTION_PREFIX = "mc_tool_deny_"
#: "Trust" auto-approves all subsequent tools for THIS session only (not
#: global). Mirrors the native path's per-session trust_tool button.
TOOL_TRUST_ACTION_PREFIX = "mc_tool_trust_"

#: Thread-status text shown while the turn is in flight (mirrors handler).
_STATUS_WORKING = "is working on your request"

#: Characters held back below ``max_message_chars`` when splitting. The shared
#: splitter may exceed its limit by the fence scaffolding of a whole-line
#: placement; this absorbs the ordinary case so no chunk reaches
#: :data:`SLACK_MSG_LIMIT`, where ``_safe_update`` would truncate it.
_SPLIT_HEADROOM = 100

#: Refusal lines appended to a reply before they are summarized as a count. Three
#: lines explain a reply; twelve bury it.
_MAX_REJECTION_LINES = 3

#: Appended to a partially-streamed assistant row that the dispatcher rescues when
#: a turn dies mid-flight. Without it the retry reads a reply that simply stops
#: mid-sentence and cannot tell a truncated turn from a finished one — so it may
#: treat the work as already reported and answer nothing. The wording addresses
#: the next turn directly, because that turn is the only consumer.
#:
#: It lives here rather than in the shared ``messaging`` layer because the rescue
#: it belongs to is Slack-only, and for the reason in ``delivered_text``: Slack is
#: the one renderer that records WHICH appended bytes the API acknowledged, so it
#: is the one channel where a turn that dies mid-flight can persist text the user
#: provably saw. Other renderers on the shared path do emit mid-turn; none of them
#: can say afterwards what the user retained.
PARTIAL_TURN_MARKER = (
    "\n\n_[This turn was cut off here by a transport/backend failure, not finished. "
    "Everything above was already established — continue from this point instead of "
    "starting the request over.]_"
)


def _redact_all(text: str) -> str:
    """Both outbound redactors as one callable, in the canonical order."""
    text, _ = redact_exfiltration_urls(text)
    return redact_credentials(text)[0]


def _display_safe(text: str) -> str:
    """Redact against what Slack RENDERS, not only the bytes sent.

    The twin of the Discord renderer's ``_redact_transformed``, and applied at
    EVERY model-authored egress in this file rather than at whichever one a
    reviewer last looked at. Neither ``AKIA**<rest>**`` nor
    ``[AKIA](https://x)<rest>`` matches a credential pattern as written, and Slack
    renders the markup away and shows the reader an intact key -- so a literal-only
    scan is not a floor, it is a scan of one of the two forms that leave here.

    Idempotent, so applying it twice on a path (a released tail, then its append)
    costs nothing and keeps the guarantee at the sink instead of at the caller.
    """
    return redact_for_display(text, _redact_all)[0]


#: Slack channel capabilities live in ``slack/transport.py`` (imported above).
#: This module used to carry a second literal copy of the declaration; two
#: literals for one fact is a drift hazard, and they had already diverged once.


def _approval_registry_key(session_key: str, request_id: str | int) -> str:
    """Namespace a kiro-cli request id by session for the approval registry.

    kiro-cli's JSON-RPC request id counter restarts at ``1`` for every session
    (``acp/client.py``), so two concurrent Slack threads both produce
    ``request_id == 1``. Keying the process-global registry (and the button
    ``action_id``/``value``) by ``session_key:request_id`` keeps each thread's
    approval isolated — a click resolves ONLY its own turn's pending tool.
    Falls back to the bare id when no session is supplied (e.g. unit tests).
    """
    rid = str(request_id)
    return f"{session_key}:{rid}" if session_key else rid


def build_approval_blocks(
    title: str, request_id: str | int, session_key: str = ""
) -> list[dict[str, Any]]:
    """Build Block Kit approve/deny buttons for a tool-permission request.

    The ``action_id``s and ``value`` encode a session-namespaced approval token
    (``session_key:request_id``) so the interaction handler correlates a click
    back to the awaiting decider WITHOUT colliding across concurrent sessions
    (kiro-cli request ids restart at 1 per session).
    """
    token = _approval_registry_key(session_key, request_id)
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔧 Approve tool *{title}*?"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"{TOOL_APPROVE_ACTION_PREFIX}{token}",
                    "value": token,
                },
                {
                    # Per-session trust: auto-approve all subsequent tools for
                    # THIS session only (not global YOLO). Mirrors native.
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Trust session"},
                    "action_id": f"{TOOL_TRUST_ACTION_PREFIX}{token}",
                    "value": token,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": f"{TOOL_DENY_ACTION_PREFIX}{token}",
                    "value": token,
                },
            ],
        },
    ]


class SlackApprovalDecider:
    """Awaits a human approve/deny/trust decision for an interactive tool prompt.

    The Slack interaction handler calls :meth:`resolve` when a button is
    clicked; :meth:`__call__` (the ``TurnDriver`` decider) awaits that result.
    Registry is keyed by request id (stringified). ``session_key`` lets the
    interaction handler map a click back to its session (for per-session Trust).
    """

    #: Process-global registry mapping request_id -> the decider currently
    #: awaiting it. The Slack interaction handler (module-level, no direct
    #: reference to the per-turn decider) resolves clicks through this.
    _REGISTRY: dict[str, "SlackApprovalDecider"] = {}

    def __init__(self, session_key: str = "") -> None:
        self._futures: dict[str, asyncio.Future[bool]] = {}
        self.session_key = session_key

    async def __call__(self, event: Any) -> bool:
        rid = str(getattr(event, "request_id", ""))
        key = _approval_registry_key(self.session_key, rid)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        # _futures is per-decider, so keying by the bare rid is unambiguous
        # here; the process-global _REGISTRY must use the session-namespaced
        # key to avoid cross-session collisions (kiro-cli rids restart at 1).
        self._futures[rid] = fut
        SlackApprovalDecider._REGISTRY[key] = self
        try:
            # Deny-by-default if the user never clicks within the window.
            return await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        finally:
            self._futures.pop(rid, None)
            if SlackApprovalDecider._REGISTRY.get(key) is self:
                SlackApprovalDecider._REGISTRY.pop(key, None)

    def resolve(self, request_id: str | int, approved: bool) -> bool:
        """Resolve a pending approval. Returns True iff a future was waiting."""
        fut = self._futures.get(str(request_id))
        if fut is not None and not fut.done():
            fut.set_result(approved)
            return True
        return False

    @classmethod
    def resolve_global(cls, registry_key: str | int, approved: bool) -> bool:
        """Resolve a pending approval via the process-global registry.

        *registry_key* is the session-namespaced token from the button
        (``session_key:request_id``, or a bare id when no session). Used by the
        Slack interaction handler, which has no direct reference to the per-turn
        decider. Returns True iff a matching pending prompt was resolved (False
        if it already expired / was answered).
        """
        dec = cls._REGISTRY.get(str(registry_key))
        if dec is None:
            return False
        # Split the namespaced token back to the bare rid for the decider's
        # per-turn _futures lookup (rsplit is safe: the rid never contains ':').
        rid = str(registry_key).rsplit(":", 1)[-1]
        return dec.resolve(rid, approved)

    @classmethod
    def session_for(cls, registry_key: str | int) -> str:
        """Return the session_key of the decider awaiting *registry_key* (or "").

        Lets the interaction handler grant per-session Trust for a click without
        a direct decider reference. *registry_key* is the session-namespaced
        token from the button.
        """
        dec = cls._REGISTRY.get(str(registry_key))
        return dec.session_key if dec is not None else ""


class SlackRenderer(Renderer):
    """Renders abstract output events onto a Slack thread.

    Holds (and exposes) the underlying ``SlackClientOps`` so the inline
    dashboard->Slack mirror keeps working unchanged (guardrail G2). The
    streaming message is lazily opened on the first text/tool event.
    """

    channel_type = "slack"

    def __init__(
        self,
        slack: Any,
        channel: str,
        thread_ts: str | None,
        *,
        react_ts: str | None = None,
        reactions_enabled: bool = True,
        show_thinking: bool = True,
        capabilities: TransportCapabilities | None = None,
        decider: SlackApprovalDecider | None = None,
        now: Callable[[], float] | None = None,
        user_id: str = "",
        uploads_allowed: bool = True,
        upload_root: str = "",
    ) -> None:
        super().__init__(capabilities or SLACK_CAPABILITIES)
        self.slack = slack
        self.channel = channel
        self.thread_ts = thread_ts
        # The sending user. Two consumers:
        #   * DashboardContributor.decorate_reply on the final outbound text, so a
        #     composed edition can refresh its auth window / append an expiry
        #     footer on the transport reply path too (native handle_message
        #     already wires decorate_reply; this closes the gap so the DEFAULT
        #     non-review Slack traffic gets the same treatment).
        #   * chat.startStream recipient routing. Slack rejects the call with
        #     ``missing_recipient_user_id`` when it is absent, and the renderer
        #     then silently demotes to the non-streaming chat.update surface, so
        #     dropping this value kills streaming for the whole transport path.
        # Empty when the caller has no sender id; both consumers no-op on "".
        self._user_id = user_id
        # Message ts to react to (the user's triggering message). Falls back
        # to thread_ts so reactions still attach when not supplied separately.
        self._react_ts = react_ts or thread_ts or ""
        self._reactions_enabled = reactions_enabled
        # When slack.show_thinking is on, surface the model's reasoning as a 💭
        # thread reply above the answer (mirrors native handle_message). Off =>
        # reasoning stays private, only the typing indicator moves.
        self._show_thinking = show_thinking
        self._thinking_accumulated = ""
        self._thinking_posted = False
        self.decider = decider
        # Monotonic clock seam: injectable so the edit-throttle is deterministic
        # in tests and harness-controllable once handle_message drives this.
        self._now = now or time.monotonic
        self._stream_ts: str | None = None
        self._use_slack_stream = True  # False => chat.update cursor fallback
        # Set at turn end when this turn's footer carried an OPTIONS control, so
        # the dispatcher can record it against the session and expire it later.
        self.posted_options: PostedOptions | None = None
        # Supplied by the dispatcher when it can make this turn durable and stamp
        # the control it is about to post. Takes the final reply text, returns the
        # staleness token (or None to post untokened, which clicks then honour).
        #
        # It is a callback rather than session state on the renderer because the
        # two halves live on opposite sides of this seam: only the renderer knows
        # the final text and whether the turn produced options, and only the
        # dispatcher knows which conversation ran the turn and where its
        # transcript is. Keeping identity out of the renderer is the point of the
        # transport split, so the dispatcher passes in the one operation it owns.
        self.stamp_options: Callable[[str], Awaitable[str | None]] | None = None
        self._accumulated = ""
        # Text Slack has actually SHOWN for this turn — the delivery ledger read by
        # the dispatcher's partial-progress rescue (see ``delivered_text``). It is
        # deliberately NOT ``_accumulated``: that one grows the instant a chunk
        # arrives, while a chunk only reaches Slack when the edit throttle opens,
        # so between flushes ``_accumulated`` holds text nobody has seen.
        self._delivered = ""
        self._bracket_hold = ""  # held text from '[' until ']' to filter [OPTIONS:]
        self._stream_buffer = ""  # unsent text buffered between throttled flushes
        self._last_edit = 0.0  # monotonic ts of the last stream edit (throttle)
        self._task_counter = 0
        self._active_task_id = ""
        self._active_task_title = ""
        self._tool_start_time = 0.0  # monotonic ts of current tool start
        self._tool_timer_task: asyncio.Task | None = None  # 30s elapsed updater
        self._controller: Any = None
        self._tool_to_phase: Any = None
        self._finalized = False  # guards close() from double-finalizing
        self._t0 = 0.0
        self._started = False  # guards on_turn_start against double-fire
        # Outbound-upload gates. The root is the provider's resolved cwd, so it
        # is UNSET until the dispatcher authorizes one (``authorize_upload_root``)
        # and uploads stay off until then: extraction reads files the model named,
        # and "anywhere" is not an approved root.
        self._upload_root = upload_root if os.path.isabs(upload_root) else ""
        self._uploads_allowed = uploads_allowed
        # Visible text withheld from the append-only stream because a local image
        # reference is in play; released (markup removed) at the seal.
        self._ref_hold = ""

    async def on_turn_start(self) -> None:
        # Native sets the working thread-status before streaming and arms the
        # reaction controller at "queued". Idempotent: the dispatcher fires this
        # early (before session acquisition) so the ack reaction reaches the user
        # immediately; the TurnDriver's later call then no-ops.
        if self._started:
            return
        self._started = True
        self._t0 = self._now()
        self._ensure_controller()  # set_phase("queued")
        await self.slack.set_thread_status(self.channel, self.thread_ts or "", _STATUS_WORKING)

    def _ensure_controller(self) -> Any:
        """Lazily create the (reused) StatusReactionController.

        Imported lazily to avoid a ``handler <-> renderer`` import cycle once
        ``handler`` drives this renderer. Reuses the real controller so the
        debounce/stall/emoji behavior matches native exactly.
        """
        if self._controller is None and self._reactions_enabled:
            self._tool_to_phase = _tool_to_phase
            self._controller = StatusReactionController(
                self.slack, self.channel, self._react_ts, enabled=True
            )
            self._controller.set_phase("queued")
        return self._controller

    def _set_phase(self, phase: str) -> None:
        ctrl = self._ensure_controller()
        if ctrl is not None:
            ctrl.set_phase(phase)
            ctrl.on_progress()

    async def _ensure_stream(self) -> str:
        if self._stream_ts is None:
            ts = await self.slack.start_stream(
                self.channel, self.thread_ts or "", user_id=self._user_id or None
            )
            if ts:
                self._stream_ts = ts
                self._use_slack_stream = True
            else:
                # No streaming surface — fall back to chat.update on a posted
                # placeholder message (native ``_ensure_stream_started``).
                self._use_slack_stream = False
                self._stream_ts = await self.slack.post_message(
                    self.channel, _THINKING, self.thread_ts
                )
        return self._stream_ts

    async def _rotate_stream(self) -> str | None:
        """Stop the dead stream and start a fresh one (native ``_rotate_stream``)."""
        if self._stream_ts:
            await self.slack.stop_stream(self.channel, self._stream_ts)
        new_ts = await self.slack.start_stream(
            self.channel, self.thread_ts or "", user_id=self._user_id or None
        )
        if new_ts:
            self._stream_ts = new_ts
        else:
            self._use_slack_stream = False
        return new_ts

    async def _append_stream(self, text: str) -> bool:
        """Append to the stream, rotating once on failure (native ``_append_stream``)."""
        if not text or not self._stream_ts:
            return True
        # The last sink an appended string passes, so the floor lands here too:
        # appended text on this path is FINAL (chat.stopStream does not replace
        # it), which makes an unscanned append unrecoverable.
        text = _display_safe(text)
        ok = await self.slack.append_stream(self.channel, self._stream_ts, text)
        if not ok and self._use_slack_stream:
            if await self._rotate_stream():
                assert self._stream_ts is not None
                ok = await self.slack.append_stream(self.channel, self._stream_ts, text)
        if ok:
            # Delivery ledger. This is the ONE sink every streamed assistant string
            # passes through, and it reports whether Slack accepted the append — so
            # recording here, and only on success, is what makes ``delivered_text``
            # mean "shown" rather than "produced". Appends on this path are
            # cumulative and final, so the ledger needs no reconciliation when
            # ``_accumulated`` is reset at a ``wait`` boundary.
            self._delivered += text
        return ok

    async def _flush_stream_buffer(self) -> None:
        """Strip thinking tags and flush the buffered stream text (if any)."""
        if not self._stream_buffer:
            return
        flush, _ = strip_thinking_tags(self._stream_buffer, strip_whitespace=False)
        self._stream_buffer = ""
        if self._uploads_enabled():
            flush = await self._withhold_refs(flush)
            if not flush:
                return
        await self._append_stream(flush)

    # -- outbound local-image uploads ---------------------------------------
    def authorize_upload_root(self, root: str) -> None:
        """Authorize the provider's resolved cwd; an invalid root disables uploads."""
        self._upload_root = root if os.path.isabs(root) else ""

    def _uploads_enabled(self) -> bool:
        """Require the transport capability, an unrestricted session, and a root."""
        return (
            bool(self.capabilities.files_outbound)
            and self._uploads_allowed
            and bool(self._upload_root)
        )

    #: How much text immediately BEFORE an image span is held back with it.
    #:
    #: Slack streams by appending, and appended text is final, so two appends are
    #: rendered as one run of characters. Cutting exactly at the span start
    #: therefore sends the text before it in one append and the markup-stripped
    #: tail in another, and a credential straddling the span is spelled by the
    #: RENDERED concatenation while neither append contains it -- invisible to a
    #: scan of either string, and to the driver's rolling redactor, whose window
    #: never sees the two halves adjacent because the hold reordered them.
    #:
    #: Holding a lookbehind margin puts both halves in the same released string,
    #: which is what makes ``_release_refs``'s scan able to see the join at all.
    #: 256 characters comfortably exceeds the longest credential shape the
    #: redactors match, and over-holding costs only that the stream shows
    #: slightly less until the seal -- a delay, where under-holding is a leak.
    _REF_HOLD_LOOKBEHIND_CHARS = 256

    async def _withhold_refs(self, text: str) -> str:
        """The part of *text* that may go to the stream now, holding back the rest.

        Slack streams by APPENDING: text that lands cannot be edited away, so an
        ``![alt](/tmp/chart.png)`` reaching the stream stays in the transcript
        beside the picture the seal uploads. Everything from the earliest
        reference onward is therefore held until the seal, which releases it with
        the markup removed. Holding rather than cutting each frame is what keeps
        the seal's view whole: extraction reads the accumulated source, and the
        stream only ever shows text no later pass will contradict.

        The scan runs off-loop. It is pure CPU over model-authored text on the
        gateway's single loop, and an adversarial run of ``![`` is exactly the
        input that makes it worth the thread.
        """
        self._ref_hold += text
        spans = await asyncio.to_thread(protected_ref_spans, self._ref_hold)
        # The lookbehind margin is what lets the release scan see a credential the
        # rendered concatenation would spell; see _REF_HOLD_LOOKBEHIND_CHARS.
        cut = (
            max(0, spans[0][0] - self._REF_HOLD_LOOKBEHIND_CHARS) if spans else len(self._ref_hold)
        )
        ready, self._ref_hold = self._ref_hold[:cut], self._ref_hold[cut:]
        return ready

    async def _release_refs(self) -> str:
        """The held tail with every image reference cut out, ready to append.

        REDACTS after the cut, and the order is the whole point: removing
        ``![alt](path)`` rejoins the text around it, and that join can spell a
        credential neither half did, so a scan upstream of the cut cannot have
        seen it. The seal applies the same reasoning to ``clean_text`` after
        extraction -- but this tail is a SEPARATE egress via ``_append_stream``,
        and on the streaming path it is the text the user ends up reading
        (``stop_stream`` does not replace appended text). Redacting here rather
        than at the call site keeps the guarantee with the join that creates the
        hazard, so a later caller cannot append a tail nothing has scanned.
        """
        if not self._ref_hold:
            return ""
        held, self._ref_hold = self._ref_hold, ""
        tail = await asyncio.to_thread(hide_local_refs, held)
        if not tail:
            return ""
        return _display_safe(tail)

    async def _extract_uploads(self, text: str) -> tuple[str, list[OutboundFile], str]:
        """Pull local images out of the sealed reply; returns (body, files, notes).

        ``notes`` is the refusal text, already folded into ``body``, and returned
        separately because the streaming path cannot re-render ``body``: Slack's
        ``chat.stopStream`` does not replace what was appended, so the notes have
        to be appended there instead. Fail-soft: a reply must go out even when
        extraction cannot decide anything about the files it mentions.
        """
        try:
            result = await extract_local_refs_off_loop(
                text, within_root=self._upload_root, limits=UPLOAD_LIMITS
            )
        except Exception:
            logger.warning("slack: outbound file extraction failed", exc_info=True)
            return text, [], ""
        body = result.rewritten_text.strip()
        if not body and not result.files:
            body = text
        notes = ""
        if result.rejections:
            sel().log_api_access(
                caller=self._audit_caller(),
                operation="slack_renderer.upload_files",
                outcome="denied",
                source="slack",
                resources=f"{len(result.rejections)} rejection(s)",
                # Reason codes only: the destination is LLM-authored text.
                error=",".join(sorted({item.reason for item in result.rejections})),
            )
            notes = self._rejection_notes(result.rejections)
            body = f"{body}\n\n{notes}" if body else notes
        if result.files:
            sel().log_api_access(
                caller=self._audit_caller(),
                operation="slack_renderer.upload_files",
                outcome="allowed",
                source="slack",
                resources=f"{len(result.files)} file(s)",
            )
        return body, result.files, notes

    def _rejection_notes(self, rejections: list[Rejection]) -> str:
        """Refusal lines for the thread. Never conditional on the answer's length.

        The reason names the destination, so the user reads which picture is
        missing and why rather than a reply that talks about one that never
        arrived. A budget check belongs to no caller here: the text is split after
        this, so an answer near the cap costs the reader a chunk boundary, where
        dropping the note would cost them the explanation.
        """
        for rejection in rejections:
            logger.info("slack: local image not uploaded (%s)", rejection.reason)
        lines = [f"⚠️ _{rejection}_" for rejection in rejections[:_MAX_REJECTION_LINES]]
        if len(rejections) > _MAX_REJECTION_LINES:
            lines.append(f"⚠️ _…and {len(rejections) - _MAX_REJECTION_LINES} more_")
        note = "\n".join(lines)
        # The destination came from the model, so the line it appears in is
        # scanned like any other outbound text before it can be posted -- in the
        # DISPLAY form too, since a rejected path is echoed inside `_..._` italics
        # that Slack renders away.
        return _display_safe(note)

    async def _upload_files(self, files: list[OutboundFile]) -> None:
        """Upload the extracted files, reporting any Slack would not take."""
        try:
            failures = await upload_outbound_files(
                self.slack, self.channel, self.thread_ts or "", files
            )
        except Exception:
            logger.warning("slack: uploading extracted images failed", exc_info=True)
            return
        if not failures:
            return
        try:
            await self.slack.post_message(
                self.channel, self._rejection_notes(failures), self.thread_ts
            )
        except Exception:
            logger.warning("slack: reporting a failed image upload failed", exc_info=True)

    def _audit_caller(self) -> str:
        """Identity for the SEL audit line: the session, else the conversation."""
        session_key = self.decider.session_key if self.decider else ""
        return session_key or self.channel or "slack"

    # -- length splitting ---------------------------------------------------
    def _limit(self) -> int:
        """Split budget: the declared cap less headroom for fence scaffolding.

        Capped at what the send path actually accepts. A declaration above
        :data:`SLACK_MSG_LIMIT` cannot buy longer messages, because ``_safe_update``
        truncates there regardless. It would only move every cut past the point
        where the fence-safe boundary is still honoured.
        """
        cap = min(self.capabilities.max_message_chars or SLACK_MSG_LIMIT, SLACK_MSG_LIMIT)
        return max(500, cap - _SPLIT_HEADROOM)

    async def _split_for_slack(self, text: str, *, reserve: int = 0) -> list[str]:
        """Fence-safe chunks Slack will accept whole.

        The shared splitter budgets each chunk against :meth:`_limit`, except for
        a logical line placed whole, which carries its fence scaffolding on top.
        :func:`chunk_text` bounds that residue at Slack's own message limit,
        because the alternative there is ``_safe_update``'s tail truncation, which
        drops the synthetic closer with the content and leaves an unterminated
        code block. Blind slicing costs a boundary Markdown may render badly and
        keeps every authored character.
        """
        limit = self._limit()
        if len(text) + reserve <= limit:
            return [text]
        chunks = await asyncio.to_thread(split_markdown_safe, text, limit, reserve=reserve)
        bounded: list[str] = []
        for chunk in chunks:
            bounded.extend(chunk_text(chunk, SLACK_MSG_LIMIT - reserve) or [chunk])
        return bounded or [text]

    async def _render_fallback(self, text: str) -> None:
        """Final no-stream render: the whole answer, not a truncated prefix.

        ``_safe_update`` truncates at Slack's message limit, so an over-limit
        answer used to lose its tail with only a notice where the native handler
        splits. Consumes the splitter's contract by sealing chunk 0 into the live
        message and posting the rest as thread replies, in order.
        """
        chunks = await self._split_for_slack(text)
        if self._stream_ts is not None:
            await _safe_update(self.slack, self.channel, self._stream_ts, chunks[0])
        for part in chunks[1:]:
            try:
                await self.slack.post_message(self.channel, part, self.thread_ts)
            except Exception:
                logger.debug("slack: posting a continuation chunk failed", exc_info=True)

    async def _append_task(self, task_id: str, title: str, status: str, details: str = "") -> bool:
        """Append a task card, rotating once on failure (native ``_append_task``)."""
        if not self._stream_ts:
            return False
        ok = await self.slack.append_task(
            self.channel, self._stream_ts, task_id, title, status, details=details
        )
        if not ok and self._use_slack_stream:
            if await self._rotate_stream():
                assert self._stream_ts is not None
                return await self.slack.append_task(
                    self.channel, self._stream_ts, task_id, title, status, details=details
                )
        return ok

    def _tool_elapsed_str(self) -> str:
        """Formatted elapsed time for the active tool, or '' (native helper)."""
        if not self._tool_start_time:
            return ""
        elapsed = self._now() - self._tool_start_time
        if elapsed < 1:
            return ""
        mins, secs = divmod(elapsed, 60)
        if mins:
            return f"⏱ {int(mins)}m {secs:.1f}s"
        return f"⏱ {secs:.1f}s"

    async def _tool_elapsed_updater(self) -> None:
        """Every 30s, refresh the active task card's title with elapsed time."""
        while True:
            await asyncio.sleep(30)
            if self._active_task_id and self._tool_start_time and self._use_slack_stream:
                elapsed = self._now() - self._tool_start_time
                mins, secs = divmod(int(elapsed), 60)
                time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                # Elapsed goes in the TITLE (Slack replaces title on same
                # task_id), never details (which Slack appends).
                await self._append_task(
                    self._active_task_id, f"{self._active_task_title}  ⏱ {time_str}", "in_progress"
                )

    def _start_tool_timer(self) -> None:
        self._cancel_tool_timer()
        self._tool_start_time = self._now()
        self._tool_timer_task = asyncio.ensure_future(self._tool_elapsed_updater())

    def _cancel_tool_timer(self) -> None:
        if self._tool_timer_task and not self._tool_timer_task.done():
            self._tool_timer_task.cancel()
        self._tool_timer_task = None

    async def close(self) -> None:
        """Idempotent teardown for the transport dispatcher's ``finally``.

        Cancels the 30s ``_tool_elapsed_updater`` timer and finalizes the
        reaction controller. Without this, a ``TurnDriver.run()`` exception
        (which skips ``on_done``) would leave the timer task alive, issuing
        ``append_task`` calls every 30s against a dead stream until the event
        loop shuts down. Safe to call after ``on_done`` (no-op) and multiple
        times: the ``_finalized`` guard prevents flipping a already-successful
        turn's reaction to the error state.
        """
        self._cancel_tool_timer()
        if not self._finalized and self._controller is not None:
            try:
                self._controller.finalize(error=True)
            except Exception:
                pass  # non-critical teardown; never raise from close()
        self._finalized = True

    @property
    def delivered_text(self) -> str:
        """Assistant text Slack has actually SHOWN for this turn.

        The dispatcher's partial-progress rescue persists this when a turn dies
        mid-flight, so that a retry resumes from what was already established
        instead of re-deriving it. It is a delivery ledger, not a copy of the
        model's output, and the distinction is the whole point: ``_accumulated``
        grows the moment a chunk arrives, but a chunk only reaches Slack when the
        edit throttle opens, so persisting that would record text nobody saw as
        established fact.

        Only ``_append_stream`` advances it, and only when Slack accepted the
        append. Two consequences worth knowing before relying on it:

        * On the no-stream fallback (``_use_slack_stream`` False) this stays
          empty, because that sink cannot confirm delivery — see the note at the
          throttled ``_safe_update``. The rescue then does nothing, which is
          correct.
        * It holds the text as SHOWN: thinking tags stripped, ``[OPTIONS:…]``
          markup suppressed by the bracket-hold, and image-adjacent text still
          withheld by ``_ref_hold`` until the seal releases it. That is a subset
          of ``_accumulated``, never a superset.

        Slack is the only renderer this exists on because Slack is the only
        renderer that can say afterwards WHICH bytes the user retained. Others on
        the shared ``messaging/dispatch.drive_turn`` path do emit mid-turn —
        ``telegram`` live-edits per chunk, ``wecom`` pushes stream frames,
        ``webex`` rides the buffer tail on a status frame — but each of those
        frames is throttled, replaced wholesale, or truncated, so none yields a
        per-append record of acknowledged output to rescue from.
        """
        return self._delivered

    @property
    def turn_finalized(self) -> bool:
        """Whether this turn already reached a normal end.

        ``on_done`` sets it, so it answers exactly one question for the
        dispatcher's partial-progress rescue: did the reply COMPLETE before the
        exception? A failure raised after a finished stream — a footer post that
        4xxs, say — still unwinds through the same ``except``, and marking that
        already-complete reply as cut off would tell the next turn to resume
        work that had in fact finished.

        Read it in the ``except`` branch only. ``close()`` also sets it during
        the dispatcher's ``finally``, which runs AFTER that branch, so by the
        time teardown flips it the rescue has already made its decision.
        """
        return self._finalized

    async def on_text_chunk(self, text: str) -> None:
        # Reuses the native streaming machinery verbatim: bracket-hold filter,
        # edit-throttle batching (``_EDIT_INTERVAL``), and the chat.update
        # cursor fallback when streaming is unavailable.
        # Flush any accumulated reasoning as a 💭 reply first so it lands above
        # the answer (no-op when show_thinking is off or nothing accumulated).
        await self._maybe_post_thinking()
        self._set_phase("thinking")
        ts = await self._ensure_stream()
        # Accumulate the FULL raw text (incl. any [OPTIONS:...]) so the final
        # extract_options in on_done sees it; only filter what is *shown*.
        self._accumulated += text
        # Bracket-hold: suppress [OPTIONS:...] markup on BOTH paths. Streaming
        # flushes (and clears) the buffer each edit; the no-stream fallback
        # never flushes, so the buffer accumulates the full *filtered* text —
        # used for the intermediate chat.update below while _accumulated keeps
        # the raw text for the final extract_options in on_done.
        self._bracket_hold, self._stream_buffer = _filter_options_brackets(
            text, self._bracket_hold, self._stream_buffer
        )
        now = self._now()
        if now - self._last_edit >= _EDIT_INTERVAL:
            if self._use_slack_stream:
                await self._flush_stream_buffer()
            else:
                # No-stream fallback: strip thinking tags for the intermediate
                # chat.update, mirroring _flush_stream_buffer on the stream path
                # so <thinking>…</thinking> never leaks into interim renders.
                filtered, _ = strip_thinking_tags(self._stream_buffer, strip_whitespace=False)
                if self._uploads_enabled():
                    # This path REPLACES the message on every frame, so markup
                    # can simply be hidden: the seal's rewritten text is what the
                    # message ends up holding.
                    filtered = await asyncio.to_thread(hide_local_refs, filtered)
                # A frame shows a fence-safe prefix rather than a truncated one;
                # the final render lands the whole answer.
                frame = await self._split_for_slack(filtered, reserve=len(_CURSOR))
                # The delivery ledger deliberately does NOT advance here. This sink
                # cannot confirm anything: ``_safe_update`` returns None, swallows
                # its own exceptions, and truncates at ``SLACK_MSG_LIMIT``, and the
                # frame is a prefix rather than the whole text. So on the no-stream
                # fallback ``delivered_text`` stays empty and the dispatcher's
                # rescue is a no-op — the correct outcome, because nothing here can
                # be shown to have reached the user.
                await _safe_update(self.slack, self.channel, ts, frame[0] + _CURSOR)
            self._last_edit = now

    async def on_thinking(self, text: str) -> None:
        self._set_phase("thinking")
        # Thinking is surfaced via the thread status indicator, not the stream.
        await self.slack.set_thread_status(self.channel, self.thread_ts or "", "is_typing")
        # When enabled, accumulate the reasoning so it can be posted as a 💭
        # thread reply above the answer (honors slack.show_thinking, matching
        # native). When disabled, reasoning is never accumulated or surfaced.
        if self._show_thinking:
            self._thinking_accumulated += text or ""

    async def _maybe_post_thinking(self) -> None:
        """Post the accumulated reasoning as a 💭 thread reply, once per turn.

        Called lazily when the first answer text arrives so the reasoning lands
        above the answer (mirrors native). Redacted before posting — reasoning
        can contain credentials/URLs just like the answer.
        """
        if self._thinking_posted or not self._show_thinking:
            return
        self._thinking_posted = True
        reasoning = self._thinking_accumulated.strip()
        if not reasoning:
            return
        reasoning = _display_safe(reasoning)
        # Reasoning is unbounded, and Slack rejects an over-limit message outright
        # the whole 💭 reply, not its tail. Split it fence-safely so a long
        # chain of thought arrives as ordered replies instead of vanishing.
        for chunk in await self._split_for_slack(f"💭 {reasoning}"):
            await self.slack.post_message(self.channel, chunk, self.thread_ts)

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        # Mirror native EVENT_TOOL_CALL: flush pending text, status update,
        # complete the previous task (with elapsed), start a new in-progress
        # task + its 30s elapsed timer.
        await self._ensure_stream()
        tool_name = title.removeprefix("Running: ")
        self._ensure_controller()
        if self._controller is not None and self._tool_to_phase is not None:
            self._controller.set_phase(self._tool_to_phase(tool_name, tool_kind))
            self._controller.on_progress()
        await self.slack.set_thread_status(
            self.channel, self.thread_ts or "", f"is using {tool_name}"
        )
        # Flush any buffered streamed text before the tool status, like native.
        if self._use_slack_stream:
            await self._flush_stream_buffer()
        if self._active_task_id:
            elapsed = self._tool_elapsed_str()
            self._cancel_tool_timer()
            ct = f"{self._active_task_title}  {elapsed}" if elapsed else self._active_task_title
            await self._append_task(self._active_task_id, ct, "complete")
        self._task_counter += 1
        self._active_task_id = f"tool_{self._task_counter}"
        self._active_task_title = tool_purpose or tool_name
        await self._append_task(
            self._active_task_id, self._active_task_title, "in_progress", details=tool_name
        )
        self._start_tool_timer()
        # The `wait` tool blocks MCP for up to 30min — finalize the streaming
        # message now so Slack doesn't show an error; the next text chunk opens
        # a fresh stream when wait returns.
        if tool_name == "wait" and self._use_slack_stream and self._stream_ts:
            if self._active_task_id:
                elapsed = self._tool_elapsed_str()
                self._cancel_tool_timer()
                ct = f"{self._active_task_title}  {elapsed}" if elapsed else self._active_task_title
                await self._append_task(self._active_task_id, ct, "complete")
                self._active_task_id = ""
            # This message ends here and its accumulated source is discarded, so
            # no seal will ever extract the withheld markup. Append it as written:
            # a visible path is the honest degradation, a dropped picture is not.
            if self._ref_hold:
                await self._append_stream(self._ref_hold)
                self._ref_hold = ""
            await self.slack.stop_stream(self.channel, self._stream_ts)
            self._stream_ts = None
            self._accumulated = ""
            self._stream_buffer = ""

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        # The tool THIS request asks about. The options are the ANSWERS ("Allow",
        # "Reject"), so falling back to the first one's label puts a verb where the
        # card promises a tool name; it stays only as the last resort for a
        # permission event that carried no title at all.
        title = tool_title or (
            (options[0].get("label") or options[0].get("id", "tool")) if options else "tool"
        )
        # Namespace the approval buttons by this turn's session so a click can
        # only resolve THIS session's pending tool (kiro-cli rids restart at 1
        # per session — a bare id would collide across concurrent threads).
        session_key = self.decider.session_key if self.decider else ""
        await self.slack.post_blocks(
            self.channel,
            build_approval_blocks(title, request_id, session_key),
            "Tool approval requested",
            self.thread_ts,
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        await self.slack.set_thread_status(
            self.channel, self.thread_ts or "", "compacting context…"
        )

    async def on_done(self, stop_reason: str = "") -> None:
        # Surface any reasoning not already flushed by the first text chunk
        # (e.g. a tool-only turn that produced no answer text).
        await self._maybe_post_thinking()
        if self._controller is not None:
            self._controller.finalize(error=False)
        self._finalized = True  # close() must not re-finalize as error
        if self._active_task_id and self._stream_ts is not None:
            elapsed = self._tool_elapsed_str()
            self._cancel_tool_timer()
            ct = f"{self._active_task_title}  {elapsed}" if elapsed else self._active_task_title
            await self._append_task(self._active_task_id, ct, "complete")
            self._active_task_id = ""
        self._cancel_tool_timer()
        # Flush any buffered (throttled) stream text before finalizing.
        if self._use_slack_stream:
            await self._flush_stream_buffer()
        clean_text, options = extract_options(self._accumulated)
        # THE semantic seal, and the only place local images are extracted: the
        # whole reply is in hand, in its original fence context, so each reference
        # is seen once and whole. A length cut never extracts, because that is how a cut
        # ends up bisecting `![alt](path)` and losing the attachment.
        files: list[OutboundFile] = []
        upload_notes = ""
        if clean_text and self._uploads_enabled():
            clean_text, files, upload_notes = await self._extract_uploads(clean_text)
        # Defensive final full-text redaction before posting — belt-and-braces
        # with the driver's StreamRedactor (mirrors native's final redact pass),
        # so nothing unredacted reaches the channel even if a chunk slipped
        # through the rolling buffer. It runs AFTER extraction because removing
        # markup rejoins the text around it, and the join can spell a credential
        # neither half did.
        if clean_text:
            clean_text = _display_safe(clean_text)
            # Outbound-reply decorator seam (Default: identity, OSS-identical) —
            # the transport-path twin of the native handle_message wiring, so the
            # DEFAULT non-review Slack path also refreshes a composed edition's auth
            # window / appends its expiry footer. Runs AFTER redaction; any text the
            # decorator INTRODUCES is re-scanned so a decorator cannot smuggle a URL
            # or credential past the redaction above. Fail-safe: a raising decorator
            # falls back to the undecorated text.
            from kiro_crew.platform import current_context, safe_context_call

            _pre_decorate = clean_text
            clean_text = safe_context_call(
                lambda: current_context().dashboard.decorate_reply(
                    _pre_decorate, channel=self.channel, user_id=self._user_id
                ),
                fallback=_pre_decorate,
                log_message="dashboard.decorate_reply failed; sending undecorated reply",
            )
            if clean_text != _pre_decorate:
                # Re-scan decorator-introduced text (the redaction above ran before
                # decoration). No module logger here — the redaction itself is the
                # security property; the native path logs counts, this path stays
                # silent to avoid adding a logger to the renderer.
                clean_text = _display_safe(clean_text)
        if self._stream_ts is not None:
            if self._use_slack_stream:
                # Appended text is final on this path (chat.stopStream does not
                # replace it), so the withheld tail and the refusal notes are
                # APPENDED rather than folded into the final text.
                tail = await self._release_refs()
                if tail:
                    await self._append_stream(tail)
                if upload_notes:
                    await self._append_stream(f"\n\n{upload_notes}")
                await self.slack.stop_stream(self.channel, self._stream_ts, clean_text or None)
            else:
                # No-stream fallback: _stream_ts is a regular message ts (the
                # _THINKING placeholder), not a stream handle — finalize it via
                # chat.update, mirroring the on_tool_call gating.
                await self._render_fallback(clean_text or "")
        if files:
            # After the text, so the answer reads first and each picture lands
            # under the sentence that introduced it.
            await self._upload_files(files)
        # Clear thread status now that the turn is complete.
        await self.slack.set_thread_status(self.channel, self.thread_ts or "", "")
        # Timing footer (always posted at turn end), mirroring native.
        turn_elapsed = (self._now() - self._t0) if self._t0 else 0.0
        footer_blocks, footer_text = build_timing_footer(turn_elapsed, None)
        # Make the turn durable and stamp the control BEFORE it goes out. This is
        # the canonical Slack path, so a control posted from here with no stamp is
        # a control whose clicks can never be judged: the check abstains and
        # honours it however far the conversation has since moved. The stamp has to
        # happen here rather than in the dispatcher's post-turn bookkeeping because
        # the token rides IN this message, and it has to follow the persist because
        # it records how far this turn got.
        #
        # Best-effort by construction: a failing stamp leaves the control untokened
        # (honoured on click, i.e. today's behaviour) and leaves the turn for the
        # dispatcher's own persist to cover, rather than costing the user a footer.
        _options_token: str | None = None
        if options and self.stamp_options is not None:
            try:
                # The RAW accumulated text, not the stripped/re-redacted copy that
                # goes to Slack. This value replaces the dispatcher's own persist,
                # so it has to be what the dispatcher would have written: the
                # driver accumulates exactly the chunks it forwards here, so this
                # is byte-identical to its `accumulated` and already
                # credential-redacted by its StreamRedactor.
                #
                # Keeping the `[OPTIONS: ...]` trailer is the load-bearing part. It
                # is how a replayed turn knows to re-render the question as a
                # control instead of as literal text, so persisting the stripped
                # copy would silently turn every replayed control into prose.
                _options_token = await self.stamp_options(self._accumulated)
            except Exception:
                _options_token = None
        footer_blocks = _append_footer_actions(
            footer_blocks, options, self.thread_ts, None, None, _options_token
        )
        footer_ts = await self.slack.post_blocks(
            self.channel, footer_blocks, footer_text, self.thread_ts
        )
        if options and footer_ts:
            # The footer carries this turn's OPTIONS control. Record where it
            # landed so the next turn can strike it through once the
            # conversation has moved past the question it asked.
            self.posted_options = PostedOptions(
                channel=self.channel,
                ts=footer_ts,
                choices=tuple(options),
                blocks=tuple(footer_blocks),
                text=footer_text,
            )
