"""The claude-agent-acp and codex-acp signals that ride ordinary frames.

Claude's compaction notices, codex's adapter notices and errors written as message
text, Claude's background-launch records, and codex's compaction ``tool_call`` pairs:
each adapter reports these in a frame shape the protocol gives another meaning, so
they are recognised here before the frame is parsed as what it looks like.

:mod:`kiro_crew.acp._dispatch` stays the import path and the patch surface; see
:mod:`kiro_crew.acp._dispatch_parts`.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any, cast

from kiro_crew.acp._dispatch_parts.surfaced_text import redact_text
from kiro_crew.acp.types import (
    EVENT_TEXT_CHUNK,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    AcpEvent,
)

# claude-agent-acp has no out-of-band compaction notification: where kiro-cli
# sends ``_kiro.dev/compaction/status`` and KAS sends its summarization kinds
# under ``_meta.kiro``, the Claude adapter reports compaction as PLAIN
# ``agent_message_chunk`` TEXT, indistinguishable from model prose to any
# client.  Verbatim from the shipped adapter (``dist/acp-agent.js``, the
# ``status`` case of its SDK-message switch):
#
#   status === "compacting"          -> "Compacting..."
#   compact_result === "success"     -> "\n\nCompacting completed."
#   compact_result === "failed"      -> "\n\nCompacting failed{reason}"
#
# where ``reason`` is ``": " + compact_error`` or ``"."``.  Recognising these
# here makes the Claude backend the third producer of EVENT_COMPACTION_STATUS,
# so every consumer that already handles the kiro-cli and KAS shapes (the
# dashboard notice + context-meter reset, the messaging drivers, and
# ``wait_for_compaction``) works on Claude with no per-surface change.
#
# The notice TEXT is still forwarded on every path: classifying one of these
# literals is a guess about bare prose, so a layer that dropped the chunk would
# turn any wrong guess into deleted model output. Structured consumers get the
# chunk flagged ``AcpEvent.control_notice`` instead, which lets them show it
# without counting it as the turn's own answer.
#
# Only ``compact_result`` carries a terminal, and per the adapter's own comment
# the SDK sends it for MANUAL ``/compact`` only: an AUTOMATIC mid-turn
# compaction takes the adapter's ``compact_boundary`` case, which emits a
# ``usage_update`` and no text at all.  So an auto-compaction produces a
# ``started`` with no terminal, and callers must settle it at turn end rather
# than waiting for one (see ``AcpClient._dispatch_events``).
_CLAUDE_COMPACTION_STARTED_MARKER = "Compacting..."
_CLAUDE_COMPACTION_COMPLETED_MARKER = "Compacting completed."
_CLAUDE_COMPACTION_FAILED_MARKER = "Compacting failed"


def parse_claude_compaction_notice(chunk: str) -> tuple[str, str] | None:
    """Classify a claude-agent-acp compaction notice chunk.

    Returns ``(status_type, detail)`` with ``status_type`` in
    ``started``/``completed``/``failed`` — the same vocabulary kiro-cli's
    ``_kiro.dev/compaction/status`` and KAS's summarization kinds already use —
    or ``None`` when *chunk* is not one of the adapter's notices.

    Matched on the STRIPPED chunk (the adapter prefixes the two terminals with
    ``\\n\\n``), and every arm is anchored to a WHOLE chunk.  ``started`` and
    ``completed`` are exact equality; ``failed`` accepts only the two shapes the
    adapter actually emits — bare ``Compacting failed.`` or
    ``Compacting failed: <reason>`` — rather than any chunk merely STARTING with
    the marker.  Anchoring is the point, and it has to hold on all three arms: a
    model that opens a real answer with "Compacting failed because…" would
    otherwise be reclassified as a control notice and have its output erased
    from the transcript.

    The returned detail is NOT redacted: it is backend-echoed text, so callers
    that surface it must pass it through their own redaction the same way they
    already do for the kiro-cli/KAS compaction summaries.
    """
    text = chunk.strip()
    if not text:
        return None
    if text == _CLAUDE_COMPACTION_STARTED_MARKER:
        return "started", ""
    if text == _CLAUDE_COMPACTION_COMPLETED_MARKER:
        return "completed", ""
    if text in (_CLAUDE_COMPACTION_FAILED_MARKER, f"{_CLAUDE_COMPACTION_FAILED_MARKER}."):
        return "failed", ""
    reason_prefix = f"{_CLAUDE_COMPACTION_FAILED_MARKER}: "
    if text.startswith(reason_prefix):
        return "failed", text[len(reason_prefix) :].strip().rstrip(".")
    return None


# codex-acp writes a few notices of its own as unattributed message text for a
# client without AIR or ``session.notices``: ``createWarningEvent``,
# ``createConfigWarningEvent`` and ``createContextCompactedEvent``. They are
# never a turn's failure, so they are delivered as they arrive rather than held.
# A prompt the adapter parses as a slash command (``parseCommand``: the first
# text block, trimmed, starts with ``/``) is excluded wholesale by the caller,
# because command output and the ``/review`` result are unattributed text too.
_CODEX_ADAPTER_NOTICE_PREFIXES = ("Warning: ", "Config warning: ", "*Context compacted")


def is_unattributed_error_candidate(event: AcpEvent) -> bool:
    """Whether *event* may be a provider failure that codex-acp wrote as text.

    True for a text chunk that named no ``messageId`` and is not one of the
    adapter's fixed notices. Read only for a harness in
    ``ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR``, and only on a prompt the
    adapter did not take as a slash command; see ``AcpSessionHandle._run_turn``.
    """
    return (
        event.kind == EVENT_TEXT_CHUNK
        and event.unattributed
        and not event.text.startswith(_CODEX_ADAPTER_NOTICE_PREFIXES)
    )


def prompt_is_adapter_command(params: dict[str, Any]) -> bool:
    """Whether codex-acp will parse this ``session/prompt`` as a slash command.

    The adapter's own test (``parseCommand``): the FIRST content block is text
    and, trimmed, starts with ``/``.
    """
    blocks = params.get("prompt") if isinstance(params, dict) else None
    if not isinstance(blocks, list) or not blocks:
        return False
    first = blocks[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return False
    return str(first.get("text") or "").strip().startswith("/")


def _service_error_detail(text: str) -> str:
    """The provider message inside codex's service-error envelope, or *text*.

    codex-acp before 2.1 forwards the provider body verbatim, for example
    ``{"type":"error","error":{"message":"...","type":"invalid_request_error"},
    "status":400}``; 2.1 unwraps the same envelope to its message itself. The
    HTTP status is kept beside the message so the shared classifier reads the
    same evidence it reads for every other harness: a 5xx is transient there and
    a 4xx is not.
    """
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if not isinstance(value, dict) or value.get("type") != "error":
        return text
    error = value.get("error")
    status = value.get("status")
    if not isinstance(error, dict) or isinstance(status, bool) or not isinstance(status, int):
        return text
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        return text
    return f"{message.strip()} (HTTP {status})"


def unattributed_terminal_error(texts: list[str]) -> dict[str, Any] | None:
    """The JSON-RPC-shaped error for a turn that ended on adapter-written text.

    *texts* are the held candidate chunks of one turn, in order. codex-acp's
    ``createErrorEvent`` writes each terminal error as ONE chunk, the message
    followed by a blank line, so a run that is not made only of such chunks is
    streamed prose from somewhere else and returns ``None``: the caller then
    delivers it as text, which is what every turn did before this check. A
    repeated error (the native notification and a duplicate of it) is reported
    once.

    The result has the shape a backend's own error response has, so it goes
    through ``_raise_acp_error`` and gets the same wording and the same retry
    verdict as any other harness's provider failure.
    """
    if not texts or any(not t.endswith("\n\n") or not t.strip() for t in texts):
        return None
    distinct = list(dict.fromkeys(_service_error_detail(t.strip()) for t in texts))
    return {"code": -32603, "message": "Internal error", "data": "; ".join(distinct)}


#: How many launch labels one session's record keeps for the recycle notice. A
#: session can launch any number of background tasks; the notice names the
#: newest few, and the record stays bounded however many it saw.
BACKGROUND_LAUNCH_LABELS_MAX = 3

#: Claude Code tools whose asynchronous launch the adapter waits out before it
#: answers the prompt (``Turn.deferredSettle`` in claude-agent-acp).
_HELD_ASYNC_LAUNCH_TOOLS = frozenset({"Agent", "Task"})


def _printable_launch_label(name: str) -> str:
    """Fold a model-authored workflow name into one line of printable text.

    The label is retained on the session record, written to a ``logger.info``
    line, and surfaced to the user as the recycle notice reason, so a newline,
    ANSI escape or bidi override in it forges log lines or steers the display.
    ``redact_text`` scrubs credentials and exfil URLs only, and the shared
    terminal sanitizers (``kiro_crew.terminal_safe``) keep tab/newline as
    content and pass bidi format characters through — neither leaves this sink
    clean. Category ``Cc`` covers ESC, newlines, tabs and NUL; ``Cf`` covers
    bidi overrides and other invisibles. Each becomes a space so the words it
    separated stay separated, then whitespace runs collapse so the label reads
    as one line. A name with no printable content folds to the empty string,
    which the caller maps to its generic label.
    """
    spaced = "".join(" " if unicodedata.category(char) in ("Cc", "Cf") else char for char in name)
    return " ".join(spaced.split())


def parse_background_launch(update: dict[str, Any]) -> str | None:
    """Name the background work a ``tool_call_update`` reports launching, or None.

    Claude Code runs a backgrounded Bash command or a Workflow in the session's
    own process tree after the prompt returns, and claude-agent-acp says so on
    the launching call's structured tool response,
    ``_meta.claudeCode.toolResponse``: a Bash command carries its
    ``backgroundTaskId``, and an asynchronous launch (a Workflow) carries
    ``status: "async_launched"`` with its ``taskId``. Both are structured fields
    of the harness's own result, never prose, so the parse cannot be steered by
    what a command printed. Nothing reports the END of that work to a client
    that has not declared the adapter's AIR extension, which is why this only
    names a launch. The marker is stamped by the harness's PostToolUse hook,
    which runs only for a live call, so a ``session/load`` replay cannot set it.

    An asynchronous sub-agent (``Agent`` / ``Task``) reports the same
    ``async_launched`` status but is not counted: the adapter holds the prompt
    open until such a sub-agent settles, so the turn is still in flight and the
    busy semaphore already covers it.

    Returns a short human label for the notice (``workflow "name"`` or
    ``background command``), or ``None`` for every other frame, including every
    frame of a harness that stamps no ``claudeCode`` key.
    """
    if update.get("sessionUpdate") != UPDATE_TOOL_CALL_UPDATE:
        return None
    meta = update.get("_meta")
    claude = meta.get("claudeCode") if isinstance(meta, dict) else None
    if not isinstance(claude, dict):
        return None
    response = claude.get("toolResponse")
    if not isinstance(response, dict):
        return None
    task_id = response.get("backgroundTaskId")
    if isinstance(task_id, str) and task_id.strip():
        return "background command"
    task_id = response.get("taskId")
    if claude.get("toolName") in _HELD_ASYNC_LAUNCH_TOOLS:
        return None
    if response.get("status") == "async_launched" and isinstance(task_id, str) and task_id.strip():
        name = response.get("workflowName")
        if isinstance(name, str):
            # Defang before redacting (controls out, then credentials), and
            # bound only AFTER redaction so a slice cannot sever a credential.
            printable = _printable_launch_label(name)
            if printable:
                return f'workflow "{redact_text(printable)[:80]}"'
        return "background task"
    return None


#: Words that mark a ``toolResponse`` key, or its ``status`` value, as reporting
#: work that outlives the call. Matched case-insensitively as substrings, so a
#: rename such as ``backgroundTaskId`` -> ``background_task_id`` or
#: ``async_launched`` -> ``asyncLaunched`` still trips the drift check.
_LAUNCH_SHAPED_WORDS = ("background", "async")

#: Most key names a drift line lists, so a huge response cannot flood the log.
_DRIFT_KEYS_MAX = 5


def _is_launch_shaped(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _LAUNCH_SHAPED_WORDS)


def unrecognised_background_launch(update: dict[str, Any]) -> str | None:
    """Describe a launch-shaped tool response ``parse_background_launch`` missed.

    The two shapes that parse reads were captured from one claude-agent-acp
    release, and Kiro Crew does not pin the adapter. If a later release renames
    or reshapes them, the parse silently returns None and the watchdog goes back
    to recycling sessions mid-workflow, with nothing failing. This is the
    tripwire for that: it fires only on a ``tool_call_update`` whose
    ``_meta.claudeCode.toolResponse`` has a key, or a string ``status``, that
    contains ``background`` or ``async`` (case-insensitive), from a tool other
    than a held ``Agent``/``Task`` launch, when the parse recognised nothing.
    An ordinary foreground result carries no such word, so the check is silent
    on every frame the parse already understands or rightly ignores.

    Returns a one-line description of the offending keys (and the status, when
    it is the match) for a log line, or ``None``. Every name is folded to
    printable text and bounded, since the response is adapter-authored.
    """
    if parse_background_launch(update) is not None:
        return None
    if update.get("sessionUpdate") != UPDATE_TOOL_CALL_UPDATE:
        return None
    meta = update.get("_meta")
    claude = meta.get("claudeCode") if isinstance(meta, dict) else None
    if not isinstance(claude, dict) or claude.get("toolName") in _HELD_ASYNC_LAUNCH_TOOLS:
        return None
    response = claude.get("toolResponse")
    if not isinstance(response, dict):
        return None
    keys = sorted(
        _printable_launch_label(key)[:40]
        for key in response
        if isinstance(key, str) and _is_launch_shaped(key)
    )
    status = response.get("status")
    status_hit = isinstance(status, str) and _is_launch_shaped(status)
    if not keys and not status_hit:
        return None
    parts = []
    if keys:
        shown = ", ".join(keys[:_DRIFT_KEYS_MAX])
        extra = len(keys) - _DRIFT_KEYS_MAX
        parts.append(f"keys {shown}" + (f" (and {extra} more)" if extra > 0 else ""))
    if status_hit:
        parts.append(f'status "{redact_text(_printable_launch_label(cast(str, status)))[:40]}"')
    tool = claude.get("toolName")
    if isinstance(tool, str) and tool:
        parts.append(f"tool {_printable_launch_label(tool)[:40]}")
    return "; ".join(parts)


@dataclass
class BackgroundLaunchRecord:
    """When a session's harness last launched background work, and what it was.

    One per session, kept by ``AcpClient`` only — the transport that serves
    claude, the one harness whose adapter stamps ``_meta.claudeCode`` on its
    frames. The ``AcpSessionHandle`` transport serves only backends that never
    stamp the marker, so a record there could never note anything. ``note`` is
    called on every session update and is cheap on the miss path. The record is
    only ever overwritten, never cleared by a later turn: a turn ending says
    nothing about whether work it did not start has finished.
    """

    launched_at: float | None = None
    labels: list[str] = field(default_factory=list)
    omitted: int = 0
    #: Set by ``note`` the first time a frame looks like a launch the parse
    #: does not recognise (``unrecognised_background_launch``) and handed out
    #: once by ``take_drift``, so the caller logs that drift once per record
    #: rather than once per frame.
    drift: str | None = None
    drift_reported: bool = False

    def take_drift(self) -> str | None:
        """The pending drift description, at most once per record."""
        drift, self.drift = self.drift, None
        return drift

    def note(self, update: object, now: float) -> bool:
        """Record *update* if it reports a background launch; True when it did."""
        if not isinstance(update, dict):
            return False
        label = parse_background_launch(update)
        if label is None:
            if not self.drift_reported:
                self.drift = unrecognised_background_launch(update)
                self.drift_reported = self.drift is not None
            return False
        self.launched_at = now
        if label in self.labels:
            self.labels.remove(label)
        self.labels.append(label)
        overflow = len(self.labels) - BACKGROUND_LAUNCH_LABELS_MAX
        if overflow > 0:
            self.omitted += overflow
            del self.labels[:overflow]
        return True

    def age(self, now: float) -> float | None:
        """Seconds since the newest launch, or ``None`` when there was none."""
        if self.launched_at is None:
            return None
        return max(0.0, now - self.launched_at)

    def describe(self) -> str:
        """The launched work, newest last, for a user-facing notice."""
        described = ", ".join(self.labels)
        if self.omitted:
            return f"{described} (and {self.omitted} more)"
        return described


#: The key codex-acp stamps on ``_meta`` for a context-compaction frame, with a
#: ``{"version": 1}`` payload. Its own name for the field, so a reader can match
#: this literal against the adapter bundle.
_CODEX_COMPACTION_META_KEY = "contextCompaction"

#: The title codex-acp gives its compaction ``tool_call``. From 2.2.2 the pair
#: carries no ``_meta`` at all, and of what is left (``kind: think``, the status,
#: this title) only the title tells a compaction from any other think call. So it
#: is matched exactly, and only on frames with no marker. Measured on codex-acp
#: 2.2.2 in the Real Adapter Contract Tests lane.
CODEX_COMPACTION_TITLE = "Compact conversation"


def parse_codex_compaction_update(
    update: dict[str, Any], pending: object = None, *, allow_unmarked: bool = False
) -> str | None:
    """Classify a codex-acp context-compaction frame, or ``None``.

    Returns ``started`` or ``completed`` -- the same vocabulary kiro-cli's
    ``_kiro.dev/compaction/status``, KAS's summarization kinds and the claude
    notices already use -- so codex becomes the fourth producer of
    ``EVENT_COMPACTION_STATUS`` and every consumer works with no per-surface
    change.

    codex-acp reports compaction as an ordinary ``tool_call`` pair rather than an
    out-of-band notification, and unlike the claude adapter it stamps the pair:
    ``_meta.contextCompaction`` is a MARKER, so this is a structural match rather
    than a guess about prose. Measured on codex-acp 1.11.0, driven over stdio::

        tool_call        kind=think  status=in_progress  title="Compact conversation"
                         _meta={"contextCompaction": {"version": 1}}
        tool_call_update             status=completed    title="Compact conversation"
                         _meta={"contextCompaction": {"version": 1}}
        session/prompt response -> {"stopReason": "end_turn"}

    Matched on the marker and the FRAME KIND, never on the title -- the title is
    display text the adapter is free to reword, and it is localized on some
    clients.

    codex-acp 2.2.2 dropped the marker. Its pair, captured in the lane
    (``test/fixtures/acp_frames/codex/compact-live-2.2.2.jsonl``)::

        tool_call        kind=think  status=in_progress  title="Compact conversation"
        tool_call_update             status=completed    (no title, no kind)

    So an UNMARKED frame is read only with *allow_unmarked* (the caller's harness is
    in ``ACP_BACKENDS_UNMARKED_COMPACTION_PAIR``), and only in that exact shape: a
    ``think`` call titled
    ``CODEX_COMPACTION_TITLE`` starts it, and the update completes it only when its
    ``toolCallId`` is *pending* -- the id the caller kept from that start. A marked
    frame never needs *pending*, so the marked path is unchanged.

    Two arms rather than three, because there is no third frame to read:

    * There is no ``failed`` status. A compaction that errors emits an
      ``agent_message_chunk`` ("Error running remote compact task: ...") and then
      the adapter's ``runCompact`` never resolves, so the ``session/prompt``
      request itself is never answered (observed: a 240s wait with no response).
      Synthesizing a ``failed`` from that text would be the prose guess this
      marker exists to avoid, and the strand is a prompt-timeout problem rather
      than a compaction-status one.
    * A ``tool_call`` whose status is already ``completed`` is deliberately NOT a
      terminal. That is the shape a past compaction takes when the adapter
      REPLAYS a loaded session's history, and reading it as a live terminal would
      reset the context counters against a window nobody just summarized.
    """
    kind = update.get("sessionUpdate")
    if kind not in (UPDATE_TOOL_CALL, UPDATE_TOOL_CALL_UPDATE):
        return None
    meta = update.get("_meta")
    status = update.get("status")
    if not isinstance(meta, dict) or _CODEX_COMPACTION_META_KEY not in meta:
        if meta is not None or not allow_unmarked:
            return None
        if kind == UPDATE_TOOL_CALL:
            unmarked = (
                update.get("kind") == "think" and update.get("title") == CODEX_COMPACTION_TITLE
            )
        else:
            call_id = update.get("toolCallId")
            unmarked = isinstance(pending, str) and bool(call_id) and call_id == pending
        if not unmarked:
            return None
    if kind == UPDATE_TOOL_CALL and status == "in_progress":
        return "started"
    if kind == UPDATE_TOOL_CALL_UPDATE and status == "completed":
        return "completed"
    return None
