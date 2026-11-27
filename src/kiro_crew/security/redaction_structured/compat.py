"""The rule table as the credential plan, and its stream side.

:func:`kiro_crew.security.redaction._credential_redaction_plan` is the one plan
every credential entry point splices (``redact_credentials``,
``redact_credentials_with_records``, ``pass_compose``). It calls
:func:`credential_plan`:

1. the structured detectors (:mod:`.engine`) read the text and resolve their hits;
2. a ``token`` URL parameter value that only the ``url.token_param.token`` row reads
   stays where the owner's companion URL prefixes exempt it
   (``redaction._exempt_token_values``);
3. each other Secret hit becomes a span replaced by
   :data:`~kiro_crew.security.redaction.REDACTED_CREDENTIAL_TAG` (a base64 or hex
   run whose decoded text holds a secret by
   ``[REDACTED: encoded credential]``); only the value is replaced, the label,
   header name or URL around it stays.

Nothing else decides: a value no row reads is left as written. Identifier hits are
never replaced.

Shapes kept
-----------
* spans are ``(start, end, tag)``, sorted and disjoint;
* one warning per Secret hit, in text order;
* ``rules`` maps each span's start to ``(rule, label)``. A Secret hit carries the
  older rule name for its format (``github_token``, ``aws_secret_access_key``, ...),
  because the source cards read those names; its label is ``""``.

Fail-closed
-----------
Any error reading the detectors, or loading their table at all, yields one span
over the WHOLE text with
:data:`~kiro_crew.security.redaction.REDACTION_UNAVAILABLE_TAG`, one value-free
warning, rule :data:`~kiro_crew.security.redaction.REDACTION_UNAVAILABLE_RULE`, a
log line naming only the error's type, and one count on
:data:`FAIL_CLOSED_METRIC`. It never raises and never returns the raw text.

Streaming
---------
``StreamRedactor`` asks :func:`stream_read` where the detectors need a buffer held:
the start of any unit still undecided at the buffer's end, of any hit that would
be cut, and the last :data:`~.stream.TAIL` characters, so a partial anchor is
never committed. A unit decided while still running (a private-key block with no
END past its bound) is replaced and the stream drops input to that unit's end
(:func:`discard_run`). A header, cookie or command-line value still running past its
hold bound is replaced too, and the stream drops the rest of its line, or the rest
of the text once that line names a private-key block (:func:`line_drop_tail`). Each
committed window is read after the raw text that
preceded it (:func:`look_behind`), as the whole text reads it. A read that fails
while the detectors can still load holds the whole buffer, never a cut chosen
without them.
"""

from __future__ import annotations

import bisect
import contextlib
import logging
import threading
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

import kiro_crew.security.redaction as redaction
from kiro_crew.default_ignorable import DEFAULT_IGNORABLE, DEFAULT_IGNORABLE_RUN_RE
from kiro_crew.security.redaction_structured import charsets

if TYPE_CHECKING:
    from kiro_crew.security.redaction_structured.model import Hit

logger = logging.getLogger(__name__)

#: Counted once per fail-closed plan. No attributes: nothing about the text.
FAIL_CLOSED_METRIC = "kirocrew.security.redaction.fail_closed"

_Span = tuple[int, int, str]
_Plan = tuple[list[_Span], list[str], dict[int, tuple[str, str]]]

#: Table rule id -> the rule name pass 1 recorded for the same value. A rule the
#: old pass had no name for is ``credential_pattern``, its catch-all.
_OLD_RULE: dict[str, str] = {
    "github.classic": "github_token",
    "github.fine_grained": "github_token",
    "slack.token": "slack_token",
    "stripe.key": "stripe_key",
    "stripe.webhook_secret": "stripe_key",
    "anthropic.key": "anthropic_key",
    "google.oauth_client_secret": "google_oauth_secret",
    "gitlab.pat": "gitlab_token",
    "sendgrid.key": "sendgrid_key",
    "digitalocean.token": "digitalocean_token",
    "jwt": "jwt",
    "jwe": "jwt",
    "link_token": "jwt",
    "aws.secret_access_key": "aws_secret_access_key",
    "aws.secret_access_key.label": "aws_secret_access_key",
    "aws.session_token.label": "aws_session_token",
    "pem.private_key": "private_key",
    "pgp.private_key": "private_key",
    "encoded_credential": "encoded_credential",
    "url.userinfo_password": "url_userinfo",
    "telegram.bot_token": "telegram_bot_token",
    "discord.bot_token": "discord_bot_token",
    "openai.project_key": "openai_key",
    "npm.token": "npm_token",
    "pypi.token": "pypi_token",
    "url.token_param.token": "token_parameter",
}


def old_rule(rule_id: str) -> str:
    """The pass-1 rule name a source card reads for a table rule id."""
    return _OLD_RULE.get(rule_id, "credential_pattern")


# ── Detectors ──

#: Set once a load fails, so a broken table costs one failed import per process
#: rather than one per call; every plan then fails closed. A ``RecursionError``
#: or ``MemoryError`` does not set it: that is the caller's exhausted stack or
#: heap at that one call, not the table.
_LOAD_FAILED = False

#: Serializes the first load. Redaction's first call can come from two threads
#: at once (the SEL writer thread redacts rows while the loop thread redacts
#: an alert), and the fail-closed flag must never be set by that race.
_LOAD_LOCK = threading.Lock()


def _load() -> None:
    """Import the detectors and build their table; raises if that ever failed.

    Race-safe by construction, in two parts. The modules come through
    ``importlib.import_module``, which waits on the per-module import lock
    until a module another thread is mid-importing has finished — a
    ``from package import engine`` would instead resolve the PARENT'S
    attribute, which can name a partially-initialized module while that
    import is in flight, and ``engine.scan`` then raises on a module that is
    fine. And the whole load holds ``_LOAD_LOCK``, so two first-callers
    cannot interleave and ``_LOAD_FAILED`` is only ever set by a load that
    genuinely failed (a bad table), never by concurrency — once set it
    poisons every later plan fail-closed for the life of the process, which
    is exactly right for a broken table and exactly wrong for a lost race.
    """
    global _LOAD_FAILED
    if _LOAD_FAILED:
        raise RuntimeError("structured credential detectors unavailable")
    with _LOAD_LOCK:
        if _LOAD_FAILED:
            raise RuntimeError("structured credential detectors unavailable")
        try:
            import importlib

            engine = importlib.import_module("kiro_crew.security.redaction_structured.engine")
            table = importlib.import_module("kiro_crew.security.redaction_structured.table")

            table.TABLE  # noqa: B018 - built at import; a bad row raises there
            engine.scan  # noqa: B018
        except (RecursionError, MemoryError):
            # The caller's stack or heap ran out at this call, which says
            # nothing about the table: this plan fails closed, the next one
            # loads again. A table whose own import recursed without end
            # would be retried, and fail closed, on every plan.
            raise
        except Exception:
            _LOAD_FAILED = True
            raise


#: ``(context, window, armour_from)`` a stream sets while it redacts one committed window.
_LOOK_BEHIND: ContextVar[tuple[str, str, int] | None] = ContextVar(
    "kirocrew_structured_look_behind", default=None
)


@contextlib.contextmanager
def look_behind(context: str, window: str, armour_from: int = 0) -> Iterator[None]:
    """Read ``window`` after ``context`` (raw text already emitted) while inside.

    Only a plan over exactly ``window`` uses it; a plan over any other text (a
    URL the outbound check reads, a rewritten copy) reads that text alone.
    ``armour_from`` (an offset in ``window``, negative inside ``context``) is where
    the last committed private-key block's END line ends: the scan for private-key
    blocks starts there, as whole text resumes it.
    """
    token = _LOOK_BEHIND.set((context, window, armour_from) if context else None)
    try:
        yield
    finally:
        _LOOK_BEHIND.reset(token)


@dataclass(frozen=True, slots=True)
class _Read:
    """One detector read of ``context + text``; offsets in that window."""

    window: str
    hits: list[Hit]
    resolved: list[Hit]
    open_from: int | None
    base: int
    replace_open_from: int | None = None
    armour_blocks: tuple[tuple[int, int], ...] = ()


def _read(text: str, final: bool = True) -> _Read:
    from kiro_crew.security.redaction_structured.engine import resolve, scan

    look = _LOOK_BEHIND.get()
    context, armour_from = (look[0], look[2]) if look is not None and look[1] == text else ("", 0)
    base = len(context)
    window = context + text
    result = scan(window, final=final, origin=base, armour_from=max(0, base + armour_from))
    hits = [h for h in result.hits if h.start >= base]
    return _Read(
        window,
        hits,
        resolve(hits),
        result.open_from,
        base,
        result.replace_open_from,
        result.armour_blocks,
    )


def _secrets_of(read: _Read) -> list[Hit]:
    from dataclasses import replace

    from kiro_crew.security.redaction_structured.model import SECRET

    base = read.base
    return [
        replace(h, start=h.start - base, end=h.end - base) if base else h
        for h in read.resolved
        if h.tier == SECRET
    ]


def secret_hits(text: str) -> list[Hit]:
    """The resolved Secret hits over ``text``, in text order, in ``text`` offsets."""
    return _secrets_of(_read(text))


# ── Plan ──

_counting = threading.local()
#: Set while :func:`_fail_closed` logs and counts one failure on this thread.
_reporting = threading.local()


def _count_fail_closed() -> None:
    """One count on :data:`FAIL_CLOSED_METRIC`, best effort and never re-entered."""
    if getattr(_counting, "active", False):
        return
    _counting.active = True
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().counter(
            FAIL_CLOSED_METRIC,
            description="Credential redaction replaced a whole text because its detectors failed.",
        )
    except Exception:  # telemetry must never break redaction
        pass
    finally:
        _counting.active = False


def _fail_closed(text: str, exc: BaseException) -> _Plan:
    # A handler that redacts the warning below (the live log stream) fails the same
    # way; that nested failure still replaces its text but reports nothing more, so
    # one failure is one warning and one count.
    if not getattr(_reporting, "active", False):
        _reporting.active = True
        try:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs the exception type only, never the text or a value
            logger.warning(
                "credential redaction failed (%s); whole text replaced", type(exc).__name__
            )
            _count_fail_closed()
        finally:
            _reporting.active = False
    return (
        [(0, len(text), redaction.REDACTION_UNAVAILABLE_TAG)],
        ["Redaction unavailable: whole text replaced"],
        {0: redaction.REDACTION_UNAVAILABLE_RECORD},
    )


#: The table rule that reports a base64 or hex run whose decoded text holds a secret.
_ENCODED_RULE = "encoded_credential"


def _tag(rule_id: str) -> str:
    """The placeholder a Secret hit of ``rule_id`` is replaced by."""
    if rule_id == _ENCODED_RULE:
        return redaction._REDACTED_ENCODED_CREDENTIAL_TAG
    return redaction.REDACTED_CREDENTIAL_TAG


#: The table row that reads a URL's ``token`` parameter.
_TOKEN_PARAM_RULE = "url.token_param.token"


def _warning(rule_id: str, length: int) -> str:
    if rule_id == _ENCODED_RULE:
        return f"Redacted base64-encoded credential ({length} chars)"
    if rule_id == _TOKEN_PARAM_RULE:
        return f"Redacted token parameter value ({length} chars)"
    return f"Redacted credential pattern ({length} chars)"


def _exempting_tokens(
    read: _Read, defer_token_param: bool, token_param_prefixes: tuple[str, ...] | None
) -> _Read:
    """*read* without each ``token`` parameter hit the owner's companion URLs keep.

    Only a value no other Secret hit touches is the exemption's to judge
    (``redaction._exempt_token_values``); a value another row reads is masked.
    """
    tokens = [h for h in read.hits if h.rule_id == _TOKEN_PARAM_RULE]
    if not tokens:
        return read
    from dataclasses import replace

    from kiro_crew.security.redaction_structured.engine import resolve
    from kiro_crew.security.redaction_structured.model import SECRET

    taken = [
        h
        for h in resolve([h for h in read.hits if h.rule_id != _TOKEN_PARAM_RULE])
        if h.tier == SECRET
    ]
    starts = [h.start for h in taken]

    def touched(h: Hit) -> bool:
        i = bisect.bisect_left(starts, h.end) - 1
        return i >= 0 and taken[i].end > h.start

    whole = sorted((h.start, h.end) for h in tokens if not touched(h))
    kept = redaction._exempt_token_values(
        read.window, whole, defer_token_param, token_param_prefixes
    )
    if not kept:
        return read
    hits = [h for h in read.hits if h.rule_id != _TOKEN_PARAM_RULE or (h.start, h.end) not in kept]
    return replace(read, hits=hits, resolved=resolve(hits))


def credential_plan(
    text: str,
    defer_token_param: bool = False,
    token_param_prefixes: tuple[str, ...] | None = None,
) -> _Plan:
    """The credential plan: the rule table's Secret hits (module docstring).

    Arguments and result are ``redaction._credential_redaction_plan``'s; the owner's
    bypass is decided by that caller before this runs. Never raises.
    """
    try:
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        _load()
        read = _exempting_tokens(_read(text), defer_token_param, token_param_prefixes)
        hits = _secrets_of(read)
        spans = [(h.start, h.end, _tag(h.rule_id)) for h in hits]
        warnings = [_warning(h.rule_id, h.end - h.start) for h in hits]
        rules = {h.start: (old_rule(h.rule_id), "") for h in hits}
        return spans, warnings, rules
    except Exception as exc:
        return _fail_closed(text if isinstance(text, str) else "", exc)


# ── Stream ──


@dataclass(frozen=True, slots=True)
class StreamRead:
    """Where a stream buffer must be held for the detectors, in buffer offsets.

    ``cut`` is the furthest the buffer may be committed. When ``tail`` is set, a
    Secret unit starting at ``cut`` is decided while still running to the
    buffer's end: the stream replaces it with ``tag`` and drops input to its end.
    ``undecided`` is set when ``cut`` stops at a unit the detectors have not
    decided yet (an open unit, or a hit the cut would split), rather than only
    at the anchor-prefix margin.
    """

    cut: int
    tail: str = ""
    undecided: bool = False
    tag: str = redaction.REDACTED_CREDENTIAL_TAG
    #: ``(BEGIN, END line end)`` of each private-key block that ended, in buffer
    #: offsets: once a BEGIN is committed, its end is the stream's next ``armour_from``.
    armour_blocks: tuple[tuple[int, int], ...] = ()
    #: ``(start, end)`` of each Secret hit the read decided, in buffer offsets.
    secrets: tuple[tuple[int, int], ...] = ()
    #: The drop is the line drop of a header, cookie or command-line value replaced
    #: at its hold bound (``headers._held``), and nothing that runs further: never
    #: set with a drop to the end of the text. ``seen`` is what that line drop has
    #: read so far for :func:`line_drop_tail`.
    held: bool = False
    seen: str = ""


#: How every credential placeholder starts (``[REDACTED: credential]`` and the rest).
_TAG_HEAD = "[REDACTED"


def ends_word(c: str) -> bool:
    """Whether *c* ends a word for the stream's holds: a blank, or a non-ASCII
    character that no reader folds onto ASCII. Every anchor literal and credential
    is ASCII, so a CJK character, which writing in those scripts puts where English
    puts a blank, cannot sit inside one. A character a label match folds onto an
    ASCII letter (``ſ`` reads as ``s`` under :func:`.charsets.ascii_lower` and a
    case-blind regex) can, so it stays inside the word. So does a default-ignorable
    character (``U+200B``): it renders as nothing, and the credential plan reads a
    token through it."""
    if c.isspace():
        return True
    if c.isascii() or ord(c) in DEFAULT_IGNORABLE:
        return False
    folds = (charsets.ascii_lower(c), c.casefold(), c.lower(), c.upper())
    return not any(f.isascii() for f in folds)


def word_start(buf: str, pos: int) -> int:
    """*pos* moved back to the start of the word it falls in (see :func:`ends_word`).

    The stream snaps the detectors' fixed tail hold back to here. Snapping to a
    blank alone held a whole Chinese or Japanese paragraph back from the live chat,
    since that prose has none."""
    return _last_word_end(buf, pos) + 1


#: Every ASCII character :func:`ends_word` accepts (each one ``str.isspace`` accepts).
#: Any other ASCII character stays inside a word.
_ASCII_BLANKS = " \n\t\r\x0b\x0c\x1c\x1d\x1e\x1f"


def _last_word_end(buf: str, end: int | None = None) -> int:
    """The index of the last character of ``buf[:end]`` that :func:`ends_word`, or -1.

    The stream asks this of its whole buffer on every feed, and a long run with no
    blank (a key body, a base64 line) is common, so no character is checked one at
    a time unless it might end a word. ``str.rfind`` finds the last ASCII blank.
    After it only a non-ASCII character can end a word, so windows that double
    backwards from ``end`` skip any slice ``str.isascii`` accepts, and check the
    others a character at a time: the work stays linear in the distance walked back."""
    hi = len(buf) if end is None else end
    blank = -1
    for c in _ASCII_BLANKS:
        blank = max(blank, buf.rfind(c, blank + 1, hi))
    size = 64
    while hi > blank + 1:
        lo = max(blank + 1, hi - size)
        if not buf[lo:hi].isascii():
            for i in range(hi - 1, lo - 1, -1):
                if ends_word(buf[i]):
                    return i
        hi, size = lo, size * 2
    return blank


def _tail_cut(buf: str, base: int, cut: int) -> int:
    """The fixed tail hold, ending at the last word: ``cut`` moved up past the last
    character that ends a word (:func:`ends_word`).

    A partial anchor literal is one word (``ghp_``, ``eyJ``, ``https://``, a key
    spelling); an anchor that spans a blank (a private-key header, a labelled key
    and its value) is a unit the detectors report undecided, or one the passes'
    own stream holds keep. So only the last word needs holding.
    """
    blank = _last_word_end(buf)
    if blank == -1:
        return cut
    # A redaction tag holds a blank; a cut inside one would hand half a tag to a
    # pass that reads the whole. Such a blank moves the cut to the tag's start.
    tag = buf.rfind(_TAG_HEAD, 0, blank)
    if tag != -1 and buf.find("]", tag, blank) == -1:
        return max(cut, base + tag)
    return max(cut, base + blank + 1)


def stream_read(
    context: str,
    buf: str,
    *,
    boundary: bool = False,
    armour_from: int = 0,
    as_written: bool = False,
) -> StreamRead | None:
    """The detectors' hold on ``buf`` read after ``context``.

    None only once a load has failed for good (``_LOAD_FAILED``): every later batch
    plan fails closed over its whole window, so there is nothing to hold for. Any
    other failure to read (a ``RecursionError`` or ``MemoryError`` that left the
    detectors loadable, or an error inside the read itself) holds the whole buffer
    as undecided: the next batch plan may succeed, and a cut chosen without the
    detectors could split a credential and stream its unanchored tail as written.
    shortcut: while reads keep failing, nothing with no credential anchor is
    committed until a read succeeds or the stream flushes (whose batch plan fails
    closed on a failure); bound the hold if a read can fail for a whole long turn.

    When ``context`` or ``buf`` holds a default-ignorable character, the text with
    them removed is read too (:func:`_joined_stream_read`) and the smaller cut wins;
    ``as_written`` reads only the text as written.

    With ``boundary`` (a display boundary: a tool call, a prompt, the turn's end)
    the fixed :data:`~.stream.TAIL` hold is skipped; every unit still undecided and
    every hit the cut would split is held as before.

    Only what can change the text written is held: a unit or hit that an
    Identifier-value row alone reads (a data URI payload, a request id) replaces
    nothing and overrides no Secret hit, so the committed text is the same wherever
    the cut falls in it, and it is not held.
    """
    if not as_written and not (context.isascii() and buf.isascii()):
        if DEFAULT_IGNORABLE_RUN_RE.search(context) or DEFAULT_IGNORABLE_RUN_RE.search(buf):
            return _joined_stream_read(context, buf, boundary, armour_from)
    try:
        _load()
        from kiro_crew.security.redaction_structured.model import IDENTIFIER, SECRET, TAIL_LINE
        from kiro_crew.security.redaction_structured.stream import TAIL, span_tail

        with look_behind(context, buf, armour_from):
            read = _read(buf, final=False)
    except Exception:
        if _LOAD_FAILED:
            return None
        return StreamRead(0, undecided=True)
    base, open_from = read.base, read.replace_open_from
    blocks = tuple((begin - base, end - base) for begin, end in read.armour_blocks)
    # An Identifier-value hit (a structure reading that is not structural).
    resolved = [
        h
        for h in read.resolved
        if not (h.tier == IDENTIFIER and h.detector == "structure" and not h.structural)
    ]
    secrets = tuple((h.start - base, h.end - base) for h in resolved if h.tier == SECRET)
    n = base + len(buf)
    tail_hit = next((h for h in resolved if h.tier == SECRET and h.tail and h.end == n), None)
    if tail_hit is not None and (open_from is None or open_from >= tail_hit.start):
        tail, seen = span_tail(read.hits, tail_hit, read.window)
        return StreamRead(
            tail_hit.start - base,
            tail,
            tag=_tag(tail_hit.rule_id),
            armour_blocks=blocks,
            secrets=secrets,
            held=tail == TAIL_LINE,
            seen=seen,
        )
    cut = n if boundary else _tail_cut(buf, base, n - TAIL)
    undecided = open_from is not None
    if open_from is not None:
        cut = min(cut, open_from)
    cut = max(cut, base)
    moved = True
    while moved:
        moved = False
        for h in resolved:
            # A hit not committed yet keeps the text it was decided from (its
            # anchor) buffered, so the window that commits it decides it alike.
            # A cut right at its end, with more text after it, would commit the
            # hit without the character it was decided by (a closing quote).
            first = h.start if h.anchor < 0 else min(h.start, h.anchor)
            if first < cut < h.end or first < cut == h.end < n:
                cut = max(base, first)
                moved = undecided = True
    return StreamRead(cut - base, undecided=undecided, armour_blocks=blocks, secrets=secrets)


def _joined_stream_read(
    context: str, buf: str, boundary: bool, armour_from: int
) -> StreamRead | None:
    """:func:`stream_read` of ``buf`` as written and without its ignorable runs, joined.

    The view's offsets are mapped back onto ``buf``. A tail of either read that starts
    at or before the other read's cut is dropped from there (the view's on a tie), so a
    unit the view reads from an earlier start never streams its head, and one only the
    view decides is dropped rather than held growing. Otherwise the smaller cut wins,
    undecided while either read reports a tail, so the floor never commits into it. When
    the cuts differ, a Secret hit either read decided that starts before the cut but
    that the window committed alone does not redact (its key ID comes later) moves the
    cut back to its start, undecided. Every private-key block either read ended is kept,
    so the floor repair and the stream's resume point see a block whose header only the
    view reads. A cut inside a block moves back to its BEGIN, undecided, so the resume
    point never skips a body not yet committed; when that BEGIN falls inside another
    block, to that one's too. A held cut that leaves more of ``buf`` than the longest
    private-key block drops from there, as a key with no END does, when it was walked
    back (blocks the readings split differently can chain) or when more ignorable
    characters than that are held (the view counts its own bounds without them). Any
    other hold is bounded by its own unit.
    """
    raw = stream_read(context, buf, boundary=boundary, armour_from=armour_from, as_written=True)
    if raw is None:
        return None
    view_context, view_buf, view_armour_from = redaction._ignorable_view(context, buf, armour_from)
    view = stream_read(
        view_context, view_buf, boundary=boundary, armour_from=view_armour_from, as_written=True
    )
    if view is None:
        return None
    to_text = redaction._view_to_text(context + buf)

    def on_buf(offset: int) -> int:
        # A view offset, negative inside the context, as an offset in ``buf``.
        return to_text(len(view_context) + offset) - len(context)

    view_blocks = {(on_buf(begin), on_buf(end - 1) + 1) for begin, end in view.armour_blocks}
    blocks = tuple(sorted(view_blocks.union(raw.armour_blocks)))
    view_cut = on_buf(view.cut)
    for cut, read, other in ((view_cut, view, raw.cut), (raw.cut, raw, view_cut)):
        if read.tail and cut <= other:
            return StreamRead(
                cut,
                read.tail,
                tag=read.tag,
                armour_blocks=blocks,
                held=read.held,
                seen=read.seen,
            )
    cut = min(raw.cut, view_cut)
    undecided = raw.undecided or view.undecided or bool(raw.tail or view.tail)
    if raw.cut != view_cut:
        # A Secret hit the higher read decided can rest on text past the lower cut (a
        # key ID after its secret), which the window committed alone never reads.
        hits = raw.secrets + tuple((on_buf(s), on_buf(e - 1) + 1) for s, e in view.secrets)
        if any(start < cut for start, _ in hits):
            window = buf[:cut]
            with look_behind(context, window, armour_from):
                spans = redaction._credential_redaction_plan(window, token_param_prefixes=())[0]
            # The plan's spans are sorted and disjoint: only the last one starting at or
            # before a hit can cover it.
            starts = [a for a, _, _ in spans]

            def covered(start: int, end: int) -> bool:
                i = bisect.bisect_right(starts, start) - 1
                return i >= 0 and spans[i][1] >= end

            missed = [start for start, end in hits if start < cut and not covered(start, end)]
            if missed:
                cut, undecided = min(missed), True
    # One walk from the last BEGIN back: a move only lowers the cut, to a BEGIN.
    moved = False
    for begin, end in reversed(blocks):
        if begin < cut < end:
            cut, moved = max(begin, 0), True
    from kiro_crew.security.redaction_structured.table import TABLE

    # BEGIN line, body bound and END line of the longest block the reader reports.
    longest = max(
        len(row.begin) + len(row.end) + 2 * (row.type_max + len(row.suffix)) + row.max_block
        for row in TABLE.armour
    )
    if len(buf) - cut > longest:
        from kiro_crew.security.redaction_structured.model import TAIL_ALL

        held = buf[cut:]
        if moved or len(held) - len(DEFAULT_IGNORABLE_RUN_RE.sub("", held)) > longest:
            return StreamRead(cut, TAIL_ALL, armour_blocks=blocks)
    return StreamRead(cut, undecided=undecided or moved, armour_blocks=blocks)


def discard_run(tail: str, pad: int, buf: str) -> tuple[int, int, bool]:
    """``(dropped, pad, done)`` for a dropped unit's continuation in ``buf``.

    The structured stream drops with the same reader, :func:`.stream.discard`. A run
    reads on through default-ignorable characters, as the credential plan reads it,
    so one inside a later chunk does not end the drop and stream the rest of the run.
    """
    from kiro_crew.security.redaction_structured.stream import discard

    dropped, pad, done = discard(tail, pad, buf)
    n = len(buf)
    while done and pad == 0 and dropped < n and ord(buf[dropped]) in DEFAULT_IGNORABLE:
        skip = dropped
        while skip < n and ord(buf[skip]) in DEFAULT_IGNORABLE:
            skip += 1
        if skip == n:
            return n, 0, False
        more, pad, done = discard(tail, 0, buf[skip:])
        if not more:
            # The run ended before these characters, which stay as written.
            return dropped, pad, done
        dropped = skip + more
    return dropped, pad, done


def line_drop_tail(tail: str, seen: str, buf: str) -> tuple[str, str]:
    """``(tail, seen)`` for the next :func:`discard_run` over ``buf``: a line drop
    takes the rest of the text once the line it drops names a private-key block
    (:func:`.stream.line_tail`, which *seen* belongs to); any other tail stays."""
    from kiro_crew.security.redaction_structured.model import TAIL_LINE
    from kiro_crew.security.redaction_structured.stream import discard, line_tail

    if tail != TAIL_LINE:
        return tail, seen
    return line_tail(seen, buf[: discard(tail, 0, buf)[0]])


def stream_context_max() -> int:
    """How much raw emitted text a stream keeps for :func:`look_behind`."""
    from kiro_crew.security.redaction_structured.stream import CONTEXT

    return CONTEXT
