"""In-memory record of the prompt text each turn handed to the agent.

The dashboard's Context Breakdown tab shows what each turn injected by SIZE
(``ctx_blocks``: label -> characters). That answers "how much" but not "what":
a developer chasing a rule the model ignored, a block that doubled, or a marker
that landed in the wrong place needs to read the exact text the turn put on the
wire. The other holders of that text do not serve this reader: the per-turn
diagnostics in ``acp/prompt_blocks.py`` are content-free by requirement, the
usage shards carry sizes only, and the opt-in wire recorder
(``acp/_frame_record.py``) needs an environment variable at gateway start and
writes files a human then has to find.

This module is the small third holder: a bounded ring of the newest prompts per
session, in process memory only, read back through :func:`snapshot` by ``GET
/api/telemetry/prompt-trace``. Three properties fix its shape:

* **Memory, never disk.** A prompt carries the user's memory, lessons and skill
  text. Nothing here is persisted, so a gateway restart forgets it and no file
  needs owner-only permissions, redaction or a review step. The wire recorder
  remains the tool for a durable capture.
* **Bounded in every dimension it retains, and every bound is said.** At most
  :data:`MAX_CHARS_PER_TURN` characters of one prompt (the record says when it
  was cut, and the cut lands on the last whitespace at or below the cap so a
  credential is never split into a prefix the read-side scrub cannot match), at most :data:`MAX_TURNS_PER_SESSION` records per session, at most
  :data:`MAX_TOTAL_CHARS` characters across all sessions, at most
  :data:`MAX_SESSION_KEYS` sessions held at once, and a key longer than
  :data:`MAX_RETAINED_KEY_CHARS` is held under its digest; when the character
  budget or the session count is exceeded the least recently written session is
  dropped whole, and a lone session still over the budget sheds its oldest
  records down to its newest prompt — only a single prompt larger than the
  whole budget may exceed it. A session-start prompt can run to a few hundred kilobytes, a
  busy gateway serves many sessions, and :func:`forget` is called only by the
  dashboard's close and sweep paths (a channel session with no tab open on it
  never calls it), so a ring bounded on text alone would still grow in key
  count and key length for the gateway's lifetime. Each ring counts the prompts its own
  cap pushed out, and a session that was evicted is remembered by key (the same
  :data:`MAX_SESSION_KEYS` bounds that table: two structures bounding one
  population must not drift), so a reader can tell "truncated" and "evicted to
  make room" apart from "never recorded" and "gone with a restart" — a silent
  tail would read exactly like the last two. The one thing cached beside the
  text, a record's block spans, is held only while it costs no more than that
  text (:data:`SPAN_COST_CHARS`), so the whole ring is bounded by twice the
  character budget and a span-dense record is re-scanned per read instead. The
  user-span announcement a record consumes (:func:`announce_user_span`) is held
  outside that budget, on the task, so it retains the user's text only as a
  length and its first and last :data:`USER_PROBE_CHARS` characters — at most
  two probes however large the message.
* **Restricted sessions record nothing.** The callers gate on the session's
  memory mode before calling :func:`record`, the same gate the wire recorder
  and the transcript store apply, so an incognito or temporary session leaves
  no prompt text behind even in memory. Closing or sweeping a tab calls
  :func:`forget` for a ``dashboard:`` session, so a closed session's text is
  not servable either; a channel key is never forgotten there, because the
  channel conversation outlives the tab that showed it.
* **Only sessions a Context tab can show are recorded.** The one reader is the
  slot endpoint, which resolves a tab to ``dashboard:<slot>`` or to the channel
  key a linked tab runs on; a cron, hook or subagent session has neither, so a
  record of its prompt would have no reader and would only spend the shared
  budget — and, under LRU-by-write eviction, push out the idle dashboard
  session a developer is actually reading. :func:`record` therefore skips any
  key outside those namespaces (:func:`readable_session_key`).

The text recorded is the string the provider hands its transport's ``send`` —
after ``EssentialDelivery`` has substituted its receipt envelope, so it is what
``build_prompt_blocks`` wraps into the ``session/prompt`` text block(s). An
image reference in that string becomes an image block on the wire and stays a
path here; that is the one place record and wire differ. The record also keeps
the length the prompt had BEFORE that substitution (``assembled_chars``): that
is the size the assembler measured and the usage row reports, and on a member
session in its acknowledged steady state the envelope is dropped entirely, so
the two lengths differ on every such turn. A reader matching a record to a
usage row compares against ``assembled_chars``; ``chars`` says what went on
the wire.

The user's own text is the one span of a prompt the scan must not trust: a
message with a line starting ``[Memory `` would otherwise read as a memory block
and swallow every genuine block after it. The assembler knows the exact span
and says so with :func:`announce_user_span` right before it hands the prompt to
the provider; :func:`record`, called on the same task once the transport has
accepted the prompt, re-finds that span in the text it is given (the receipt
substitution in between may have shifted it) and keeps it on the record, so the audit view is carved the way the
size breakdown is. A prompt sent with no announcement — a provider driven
outside the dashboard runner — is scanned without a carve, as before.
"""

from __future__ import annotations

import collections
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from kiro_crew import runtime_death
from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES
from kiro_crew.context_blocks import block_spans
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

#: Newest prompts kept per session. A developer reading the tab wants the last
#: few turns, not the session's history; the transcript store has that.
MAX_TURNS_PER_SESSION: Final = 12

#: Characters kept of ONE prompt. A count cap bounds memory only when each item
#: is bounded too: without this, one multi-megabyte paste would hold twelve
#: copies of itself resident and push every other session out of the budget on
#: the way. A session-start prompt runs to a few hundred thousand characters, so
#: 2M is headroom, not a squeeze; a longer prompt is kept from its start and the
#: record says it was cut. The cut lands on whitespace, never inside a token
#: (:func:`_cut_point`), so a credential is kept whole or dropped whole.
MAX_CHARS_PER_TURN: Final = 2_000_000

#: What one retained span tuple costs, in characters of text: a 3-tuple, two ints
#: past the small-int cache and a list slot measure ~128 bytes, and compact ASCII
#: text is one byte per character. A record's spans are CACHED on it only while
#: ``len(spans) * SPAN_COST_CHARS <= len(text)`` — the cache never costs more than
#: the text it describes, so everything the ring retains is bounded by twice
#: :data:`MAX_TOTAL_CHARS`; a span-dense record (a prompt of alternating one-line
#: markers) is re-scanned per read instead of being cached. Adjacent spans with
#: one label are coalesced first, which is what keeps the ordinary record (and the
#: pathological repeated-marker one) far under the line.
SPAN_COST_CHARS: Final = 128

#: Characters kept across ALL sessions before the least recently written
#: session is evicted whole. Recording is always on for persistent sessions —
#: there is no operator switch, so a gateway nobody ever opens Developer Mode
#: on pays this ceiling too — which is why it is sized for the reader, not the
#: host: 16M characters is 16–64 MB of Python string (one to four bytes per
#: code point, by the widest character each string holds), a few sessions'
#: worth of recent turns, far more than a developer reads and small enough that
#: a memory-constrained host does not notice it.
MAX_TOTAL_CHARS: Final = 16_000_000

#: Sessions held at once, and session keys remembered as "evicted whole" so their
#: next read can say so. ONE constant for both tables: they bound one population
#: (a key leaves the first to enter the second), and two constants would let the
#: bounds drift apart. The oldest goes first in either. Sized well above the
#: slots a dashboard holds open, so an ordinary gateway never reaches it; it
#: exists for the sessions no close path ever forgets.
MAX_SESSION_KEYS: Final = 1024

#: Longest session key retained as given; a longer one is held under its SHA-256
#: at every door (record, read and forget), so the table's key bytes are bounded
#: by count × this and a key written under one name is never looked up under
#: another. The ONE spelling of that bound, shared with ``runtime_death``'s
#: streak table rather than copied, so the two cannot drift onto two rules.
MAX_RETAINED_KEY_CHARS: Final = runtime_death.MAX_RETAINED_KEY_CHARS

#: How much of the user's text the span announcement keeps to re-find it by —
#: its first and its last this-many characters, beside its length. The hint is
#: held on the task outside the ring's character budget until the next record
#: consumes it, so it must be bounded on its own; and it is a text probe rather
#: than a digest because a digest is a hash of the user's message, which is not
#: a thing this module should be doing (a scanner reads it as hashing sensitive
#: data). Head and tail together with the length tell the two candidate
#: positions apart in every realistic case: a shift lands the tail probe on the
#: block AFTER the user's text, which is not the user's text.
USER_PROBE_CHARS: Final = 1024


#: The one session-key namespace the dashboard mints for its own tabs.
_DASHBOARD_NAMESPACE: Final = "dashboard"


def readable_session_key(session_key: str) -> bool:
    """Whether a Context tab could ever ask for this key's prompts.

    The endpoint resolves a tab to ``dashboard:<slot>`` or, for a channel-linked
    tab, to the channel's own key; nothing resolves a ``cron:``, ``hook:`` or
    ``subagent:`` key. Recording those would fill the budget with text no one
    can read. Channel keys pass: a channel-linked tab runs its turns on the
    channel's own key, and those turns are the ones a tab can show. (Whether a
    given prompt on a passing key is kept is :func:`record`'s turn-number check;
    this is only the namespace half.)
    """
    head, sep, _ = session_key.partition(":")
    return bool(sep) and (head == _DASHBOARD_NAMESPACE or head in CHANNEL_SESSION_NAMESPACES)


@dataclass(frozen=True)
class _UserSpanHint:
    """Where the assembler says the user's text sits in the prompt it is sending."""

    start: int
    end: int
    #: Length of the prompt the span was measured against, so a record of a
    #: text of another length knows how far a prefix substitution moved it.
    prompt_len: int
    #: The user's text by its length and its first and last
    #: :data:`USER_PROBE_CHARS` characters, never the whole text: a candidate
    #: position is accepted only when its slice has this length AND opens and
    #: closes with these, so a wrong shift cannot carve someone else's bytes as
    #: the user's — while the hint stays bounded however large the message,
    #: because it is held OUTSIDE the ring's character budget (on the task, until
    #: the next record consumes it) and so must not retain the message to bound.
    probe_len: int
    probe_head: str
    probe_tail: str


#: The span the current task's next :func:`record` should carve, set by the
#: assembler and consumed by the first record that follows. A context variable
#: rather than a per-session table: the assembler's session key and the
#: provider's need not be spelled the same, and the value dies with the task.
_announced_user_span: ContextVar[_UserSpanHint | None] = ContextVar(
    "prompt_trace_user_span", default=None
)


#: The arrival length the OUTER delivery measured, for a record made by an inner
#: one. On the shared-runtime backend ``AcpProvider`` wraps an
#: ``AcpSessionProvider`` and each has an ``EssentialDelivery``; the outer one
#: performs the receipt substitution, so the inner one — the recorder there —
#: receives the already-substituted text and would measure ``assembled_chars ==
#: chars`` on every turn. Same shape as the user-span announcement: set by the
#: side that knows, consumed by the next record on this task.
#: The turn number the assembler is composing for, announced beside the span and
#: consumed by the same :func:`record`: the usage row carries this number too, so
#: a reader joins the two by ID rather than by time.
_announced_turn: ContextVar[int | None] = ContextVar("prompt_trace_announced_turn", default=None)
_announced_assembled_chars: ContextVar[int | None] = ContextVar(
    "prompt_trace_assembled_chars", default=None
)


def announce_assembled_chars(assembled_chars: int) -> None:
    """Say how long the prompt was BEFORE the receipt substitution, for the next record."""
    _announced_assembled_chars.set(assembled_chars)


def announce_user_span(
    prompt: str, span: tuple[int, int] | None, *, turn: int | None = None
) -> None:
    """Say where the user's text sits in *prompt*, about to be sent on this task.

    Called by the assembler with the FINAL prompt and the span it measured
    against it; the next :func:`record` on the same task picks it up. ``None``
    (the assembler could not vouch for a span) clears any earlier announcement
    so a stale one cannot carve a later prompt.

    *turn* is the turn number the assembler stamps the usage row with
    (``context/composed``'s ``turn``); recorded on the prompt so the tab joins the
    two by id. Announced and consumed with the span — one announcement, one
    prompt — so it can no more carry over to a later turn than the span can.
    """
    _announced_turn.set(turn)
    if span is None:
        _announced_user_span.set(None)
        return
    start, end = span
    if not (0 <= start <= end <= len(prompt)):
        _announced_user_span.set(None)
        return
    head, tail = _probes(prompt, start, end)
    _announced_user_span.set(_UserSpanHint(start, end, len(prompt), end - start, head, tail))


def _probes(text: str, start: int, end: int) -> tuple[str, str]:
    """The bounded head and tail of ``text[start:end]`` (see :class:`_UserSpanHint`).

    A slice that fits in one probe is held whole as the head, with an empty
    tail; a longer one keeps its first and last :data:`USER_PROBE_CHARS`.
    """
    if end - start <= USER_PROBE_CHARS:
        return text[start:end], ""
    return text[start : start + USER_PROBE_CHARS], text[end - USER_PROBE_CHARS : end]


def _locate_user_span(hint: _UserSpanHint | None, text: str) -> tuple[int, int] | None:
    """Re-find the announced span in *text*, or ``None`` when it cannot be vouched for.

    Between the announcement and the transport write the only mutation is the
    receipt substitution, which changes the prompt's length by one delta
    somewhere before or after the user's text. So the span is either where it
    was or moved by exactly that delta — and a candidate is accepted only when
    the slice still has the announced text's length and its head and tail,
    never on position alone.
    """
    if hint is None or hint.probe_len <= 0:
        return None
    for shift in (0, len(text) - hint.prompt_len):
        start, end = hint.start + shift, hint.end + shift
        if (
            0 <= start <= end <= len(text)
            and end - start == hint.probe_len
            and _probes(text, start, end) == (hint.probe_head, hint.probe_tail)
        ):
            return (start, end)
    return None


def _coalesce(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Merge runs of adjacent spans that share a label.

    :func:`block_spans` starts a span at every marker hit, so a block's body and
    the blank line after its closer, or a hundred consecutive ``[RUNTIME]``
    lines, arrive as that many spans of one label. A reader wants one row per
    block, and every tuple here is retained memory, so they are merged once,
    here, and the view draws one row per served span (no second merge).
    """
    out: list[tuple[int, int, str]] = []
    for start, end, label in spans:
        if out and out[-1][2] == label and out[-1][1] == start:
            out[-1] = (out[-1][0], end, label)
        else:
            out.append((start, end, label))
    return out


def _chain(text: str) -> str:
    """The egress redaction chain, in the order every other egress runs it."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


@dataclass(frozen=True)
class PromptRecord:
    """One turn's outbound prompt text, as handed to the transport."""

    ts: str
    #: Length of the prompt as sent, before any cut.
    chars: int
    #: Length of the prompt as the assembler handed it to the provider, before
    #: the receipt substitution — the size the usage row was measured from.
    assembled_chars: int
    #: The prompt text, at most :data:`MAX_CHARS_PER_TURN` characters of it, cut
    #: on whitespace so no token is split (:func:`_cut_point`).
    text: str
    #: True when ``text`` is a prefix of the prompt, not the whole of it.
    truncated: bool
    #: Where the user's own text sits in ``text``, when the assembler announced
    #: it and it was re-found; ``None`` scans without a carve.
    user_span: tuple[int, int] | None = None
    #: The turn number the assembler composed this prompt for — the same number
    #: the usage row carries — when it announced one; ``None`` for a prompt sent
    #: outside the dashboard runner, which the tab then joins by time.
    turn: int | None = None

    @property
    def spans(self) -> list[tuple[int, int, str]]:
        """The block spans of ``text``, adjacent same-label spans coalesced.

        The text is immutable, so its spans are too; computed on the first read
        (the endpoint's, off the event loop) rather than at record time, which
        sits on the turn path. Carved at ``user_span`` the way the size breakdown
        is, so a marker the user typed is credited to the user and not to the
        block it imitates. Cached on the record for later polls only while the
        cache costs no more than the text it describes
        (:data:`SPAN_COST_CHARS`): the ring's budget counts text, and a nested
        container that could outgrow it would sit outside every stated bound.
        A record past that line is re-scanned on each read instead.
        """
        cached = self.__dict__.get("spans")
        if cached is not None:
            return cached
        spans = _coalesce(block_spans(self.text, user_span=self.user_span))
        if len(spans) * SPAN_COST_CHARS <= len(self.text):
            # Frozen dataclass: the cache lives in the instance dict beside
            # ``_needs_redaction``, never as a field, so it is not part of equality.
            self.__dict__["spans"] = spans
        return spans

    def _scrub(self) -> tuple[str, list[tuple[int, int, str]], bool]:
        """Run the WHOLE prompt through the redaction chain; rebuild the spans over the result.

        The text is scrubbed as one string, never block by block: the chain's
        private-key rule masks from a ``-----BEGIN ... PRIVATE KEY-----`` marker
        through its ``END`` marker across anything between, so a key whose body
        ran across a block boundary would, scrubbed per block, keep every byte in
        the block that holds the END — that block has no BEGIN of its own.
        Whole-text scrubbing is what the chain guarantees on every other egress,
        and this one is held to it.

        The spans are then REBUILT by scanning the scrubbed text, not carried
        over: redaction changes lengths and the chain reports no offsets, and a
        redaction that swallowed a block marker (the header between the two
        halves of that key) has changed the block structure of what is served.
        The scan reads the served text as it is — a redaction tag is not a block
        marker — so a block the whole scrub left alone keeps the span it had,
        and one it consumed is simply not there. The user's carve is kept only
        when no redaction crossed its bounds (the three pieces scrubbed apart
        re-join to the whole), since an offset into the verbatim text means
        nothing in the scrubbed one otherwise; a prompt with a secret straddling
        the user's own message is then scanned without the carve.
        """
        whole = _chain(self.text)
        if whole == self.text:
            return self.text, self.spans, False
        user_span: tuple[int, int] | None = None
        if self.user_span is not None:
            us, ue = self.user_span
            head = _chain(self.text[:us])
            body = _chain(self.text[us:ue])
            if head + body + _chain(self.text[ue:]) == whole:
                user_span = (len(head), len(head) + len(body))
        return whole, _coalesce(block_spans(whole, user_span=user_span)), True

    @property
    def presented(self) -> tuple[str, list[tuple[int, int, str]], bool]:
        """The text as it may leave the process: ``(text, spans, redacted)``.

        The ring holds the prompt VERBATIM because that is what the recorder is
        for, and a prompt carries the user's memory and recall bodies, which no
        write path scrubs (the memory scrubber runs on the way OUT of the memory
        editor, not into the store). So the read side is the egress boundary: a
        token a memory record happens to hold would otherwise reach the browser
        and its Copy-all button. The WHOLE text runs through the shared
        exfiltration-URL then credential chain — never block by block, which
        would let a private key whose body crossed a block boundary keep every
        byte past it (see :meth:`_scrub`) — and the spans are realigned over the
        scrubbed text so they still line up with the text they describe:
        redaction changes lengths, and offsets into the verbatim text would point
        into the wrong block.

        Deliberately NOT a cached copy: a retained scrubbed string would double
        the memory the ring holds outside the budget ``total_chars`` enforces.
        Only the verdict is cached (``_needs_redaction``, set by the first read
        from the same pass that produced its presentation, so no read scrubs
        twice); a record with nothing to scrub (the ordinary case) is served as
        the very string the ring holds, and a record that does need scrubbing is
        scrubbed once per read — rare, off the event loop, and the price of the
        ceiling meaning what it says.
        """
        verdict = self.__dict__.get("_needs_redaction")
        if verdict is False:
            return self.text, self.spans, False
        text, spans, changed = self._scrub()
        if verdict is None:
            # Frozen dataclass: the verdict lives beside the cached spans in the
            # instance dict, never as a field, so it is not part of equality.
            self.__dict__["_needs_redaction"] = changed
        if not changed:
            return self.text, self.spans, False
        return text, spans, True

    def to_dict(self) -> dict[str, object]:
        """The record as the endpoint serves it — the PRESENTED text, never the raw."""
        text, spans, redacted = self.presented
        return {
            "ts": self.ts,
            "chars": self.chars,
            "assembled_chars": self.assembled_chars,
            "text": text,
            "truncated": self.truncated,
            "turn": self.turn,
            "redacted": redacted,
            "spans": [{"start": s, "end": e, "label": label} for s, e, label in spans],
        }


class _Ring:
    """One session's newest prompts plus the count its own cap pushed out."""

    def __init__(self) -> None:
        self.items: collections.deque[PromptRecord] = collections.deque(
            maxlen=MAX_TURNS_PER_SESSION
        )
        self.dropped = 0

    @property
    def chars(self) -> int:
        return sum(len(r.text) for r in self.items)


@dataclass(frozen=True)
class PromptSnapshot:
    """What one session's ring holds, and what its bounds pushed out."""

    records: list[PromptRecord]
    #: Prompts this session's own cap pushed out since it was first recorded.
    dropped: int
    #: True when the global budget evicted this session whole; its records are
    #: gone until its next turn, and that absence is not "never recorded".
    evicted: bool


class _Store:
    """The process-wide ring: session_key -> _Ring, LRU-ordered."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rings: collections.OrderedDict[str, _Ring] = collections.OrderedDict()
        self.evicted: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.total_chars = 0


_store = _Store()


# The last whitespace in a string: one C-speed pass, for a head of up to
# MAX_CHARS_PER_TURN + 1 characters.
_LAST_WHITESPACE: Final = re.compile(r"\s\S*\Z")
# The PEM private-key markers the read-side scrub matches (``security.redaction``):
# a BEGIN kept without its END is the one credential shape whitespace does not
# bound, so the cut must retreat before it.
_PEM_MARKER: Final = re.compile(r"-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----")


def _cut_point(text: str) -> int:
    """Where *text* is cut to fit :data:`MAX_CHARS_PER_TURN`: its length when it fits,
    else the last whitespace at or below the cap, else ``0``.

    Whitespace is the one delimiter every credential shape the read-side scrub
    knows stops at, so the kept text never ends inside one. The search is NOT
    bounded by a window: a whitespace-free run below the cap is not one token — a
    minified JSON body runs for megabytes without a space and carries quoted
    credentials inside it — so cutting at a fixed offset inside that run would
    keep a credential prefix shorter than any floor the scrub matches, served
    with ``redacted=false``. The run is dropped whole instead, back to the
    whitespace before it, however far that is; with none at all, nothing of the
    text is kept and the record says so (``truncated``, ``chars``).

    One credential shape spans whitespace: a PEM private key, whose body is many
    lines. A key whose BEGIN is kept and whose END fell past the cut is scrubbed
    on read only by the truncated-key fallback, which stops at the first line that
    is not key material — so a block header inside the body ended it early and
    the lines after it were served under ``redacted=true``. The cut therefore
    retreats to the whitespace before the first BEGIN the kept text does not
    close: the key is dropped whole, like every other token, or kept whole with
    its END.

    One linear pass, never a rescan of shrinking prefixes: the markers are walked
    in order the way the scrub pairs them (a BEGIN opens, the next END closes,
    a BEGIN inside an open block is body), so the first BEGIN still open at the
    cut is found in one sweep, and everything before it is closed pairs — a
    prompt packed with thousands of unclosed BEGIN lines costs one scan, not one
    per line (a rescan per line stalled the gateway past its watchdog).
    """
    if len(text) <= MAX_CHARS_PER_TURN:
        return len(text)
    # text[MAX_CHARS_PER_TURN] is the first character that would be dropped; a
    # space there means the cap itself sits on a boundary, and the cut is the cap.
    found = _LAST_WHITESPACE.search(text[: MAX_CHARS_PER_TURN + 1])
    cut = found.start() if found else 0
    head = text[:cut]
    open_at: int | None = None
    for match in _PEM_MARKER.finditer(head):
        if match.group(1) == "BEGIN":
            if open_at is None:
                open_at = match.start()
        elif open_at is not None:
            open_at = None
    if open_at is not None:
        before = _LAST_WHITESPACE.search(head[:open_at])
        cut = before.start() if before else 0
    return cut


def record(session_key: str, text: str, *, assembled_chars: int | None = None) -> None:
    """Remember *text* as the newest prompt sent on *session_key*.

    *assembled_chars* is the prompt's length before the receipt substitution;
    omitted, it is taken to equal the text's own length (nothing was
    substituted). An outer delivery's :func:`announce_assembled_chars` on this
    task wins over both: it measured the prompt before a substitution the
    caller here never saw. Never raises and never blocks on anything but its own short
    lock: this is called on the turn path once the transport accepted the prompt, and
    a bookkeeping fault must not cost a turn. An empty *session_key* (a pooled
    worker not yet claimed by any session) is dropped, and so is any key no
    Context tab can resolve (:func:`readable_session_key`): there is no tab that
    could read it. So is a prompt no :func:`announce_user_span` on this task
    stamped with a turn number: the tab joins a prompt to its usage row by that
    number and by nothing else, so a numberless record (a dispatcher prompt, a
    channel message answered with no tab assembling it) could never be drawn
    and would only spend the budget that evicts prompts someone can read.
    """
    if not session_key or not text or not readable_session_key(session_key):
        return
    # One announcement serves one prompt: consumed here whether or not it can
    # be re-found, so it cannot carry over to the next turn on this task.
    hint = _announced_user_span.get()
    if hint is not None:
        _announced_user_span.set(None)
    turn = _announced_turn.get()
    if turn is not None:
        _announced_turn.set(None)
    announced_len = _announced_assembled_chars.get()
    if announced_len is not None:
        _announced_assembled_chars.set(None)
        assembled_chars = announced_len
    if turn is None:
        return  # every announcement consumed above; nothing can draw this one
    cut = _cut_point(text)
    user_span = _locate_user_span(hint, text)
    if user_span is not None and user_span[0] >= cut:
        user_span = None  # the user's text fell entirely past the cut
    elif user_span is not None:
        user_span = (user_span[0], min(user_span[1], cut))
    rec = PromptRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        chars=len(text),
        assembled_chars=len(text) if assembled_chars is None else assembled_chars,
        text=text[:cut],
        truncated=cut < len(text),
        user_span=user_span,
        turn=turn,
    )
    key = runtime_death.bounded_session_key(session_key)
    try:
        with _store.lock:
            ring = _store.rings.get(key)
            if ring is None:
                ring = _Ring()
                _store.rings[key] = ring
                # A new turn on an evicted session starts its ring afresh, so the
                # next snapshot reports the fresh ring, not the past eviction.
                _store.evicted.pop(key, None)
            else:
                _store.rings.move_to_end(key)
            if len(ring.items) == ring.items.maxlen:
                _store.total_chars -= len(ring.items[0].text)
                ring.dropped += 1
            ring.items.append(rec)
            _store.total_chars += len(rec.text)
            # Evict least recently written sessions, never the one just
            # written (it is at the end): a single prompt larger than the whole
            # budget must still be readable for the session that sent it.
            while (
                _store.total_chars > MAX_TOTAL_CHARS or len(_store.rings) > MAX_SESSION_KEYS
            ) and len(_store.rings) > 1:
                oldest_key, oldest_ring = _store.rings.popitem(last=False)
                _store.total_chars -= oldest_ring.chars
                _store.evicted[oldest_key] = None
                _store.evicted.move_to_end(oldest_key)
                while len(_store.evicted) > MAX_SESSION_KEYS:
                    _store.evicted.popitem(last=False)
            # The budget holds within one session too: when this ring is the
            # only one left and still over, its OLDEST records go (counted in
            # ``dropped`` like a cap push-out), down to the newest prompt alone.
            # Nine 2M prompts on one session would otherwise hold 18M against a
            # 16M ceiling; only a single prompt larger than the whole budget is
            # allowed to exceed it.
            while _store.total_chars > MAX_TOTAL_CHARS and len(ring.items) > 1:
                _store.total_chars -= len(ring.items.popleft().text)
                ring.dropped += 1
    except Exception:  # noqa: BLE001 - bookkeeping must never reach the turn
        return


def snapshot(session_key: str) -> PromptSnapshot:
    """The prompts held for *session_key* (oldest first) and what its bounds pushed out."""
    key = runtime_death.bounded_session_key(session_key)
    with _store.lock:
        ring = _store.rings.get(key)
        if ring is None:
            return PromptSnapshot([], 0, key in _store.evicted)
        return PromptSnapshot(list(ring.items), ring.dropped, False)


def forget(session_key: str) -> None:
    """Drop everything recorded for *session_key*.

    Called when a dashboard tab is closed or swept to history: the transcript
    stays on disk for a resume, but the verbatim prompt text — memory, lessons,
    skills — has no reader once the tab is gone and must not stay servable by
    key until eviction or restart.
    """
    key = runtime_death.bounded_session_key(session_key)
    with _store.lock:
        ring = _store.rings.pop(key, None)
        _store.evicted.pop(key, None)
        if ring is not None:
            _store.total_chars -= ring.chars


def _reset_for_tests() -> None:
    _announced_user_span.set(None)
    _announced_assembled_chars.set(None)
    with _store.lock:
        _store.rings.clear()
        _store.evicted.clear()
        _store.total_chars = 0
