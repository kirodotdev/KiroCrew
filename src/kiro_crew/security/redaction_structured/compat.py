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
(:func:`discard_run`). Each committed window is read after the raw text that
preceded it (:func:`look_behind`), as the whole text reads it.
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
#: rather than one per call; every plan then fails closed.
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
        except Exception:
            _LOAD_FAILED = True
            raise


#: ``(context, window)`` a stream sets while it redacts one committed window.
_LOOK_BEHIND: ContextVar[tuple[str, str] | None] = ContextVar(
    "kirocrew_structured_look_behind", default=None
)


@contextlib.contextmanager
def look_behind(context: str, window: str) -> Iterator[None]:
    """Read ``window`` after ``context`` (raw text already emitted) while inside.

    Only a plan over exactly ``window`` uses it; a plan over any other text (a
    URL the outbound check reads, a rewritten copy) reads that text alone.
    """
    token = _LOOK_BEHIND.set((context, window) if context else None)
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


def _read(text: str, final: bool = True) -> _Read:
    from kiro_crew.security.redaction_structured.engine import resolve, scan

    pair = _LOOK_BEHIND.get()
    context = pair[0] if pair is not None and pair[1] == text else ""
    base = len(context)
    window = context + text
    result = scan(window, final=final, origin=base)
    hits = [h for h in result.hits if h.start >= base]
    return _Read(window, hits, resolve(hits), result.open_from, base, result.replace_open_from)


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
    at the anchor-prefix margin. A ``tail`` of ``line`` (:data:`~.model.TAIL_LINE`)
    drops nothing: the stream writes ``tag`` and hides its output to the next line
    break, and reads the rest of the buffer as before. ``quiet_from`` is the buffer
    offset of a held Cookie line's name (-1 for none): until a line break arrives or the
    hold passes its bound, a feed may skip this read (:func:`.stream.quiet`).
    """

    cut: int
    tail: str = ""
    undecided: bool = False
    tag: str = redaction.REDACTED_CREDENTIAL_TAG
    quiet_from: int = -1


#: How every credential placeholder starts (``[REDACTED: credential]`` and the rest).
_TAG_HEAD = "[REDACTED"


def _tail_cut(buf: str, base: int, cut: int) -> int:
    """The fixed tail hold, ending at the last word: ``cut`` moved up past the last blank.

    A partial anchor literal is one word (``ghp_``, ``eyJ``, ``https://``, a key
    spelling); an anchor that spans a blank (a private-key header, a labelled key
    and its value) is a unit the detectors report undecided, or one the passes'
    own stream holds keep. So only the last word needs holding.
    """
    blank = max(buf.rfind(c) for c in " \t\r\n")
    if blank == -1:
        return cut
    # A redaction tag holds a blank; a cut inside one would hand half a tag to a
    # pass that reads the whole. Such a blank moves the cut to the tag's start.
    tag = buf.rfind(_TAG_HEAD, 0, blank)
    if tag != -1 and buf.find("]", tag, blank) == -1:
        return max(cut, base + tag)
    return max(cut, base + blank + 1)


def stream_read(
    context: str, buf: str, *, boundary: bool = False, hiding: bool = False
) -> StreamRead | None:
    """The detectors' hold on ``buf`` read after ``context``.

    None when the detectors cannot load: the batch plan fails closed on the same
    error, so there is nothing to hold for.

    With ``boundary`` (a display boundary: a tool call, a prompt, the turn's end)
    the fixed :data:`~.stream.TAIL` hold is skipped; every unit still undecided and
    every hit the cut would split is held as before.

    Only what can change the text written is held: a unit or hit that an
    Identifier-value row alone reads (a data URI payload, a request id) replaces
    nothing and overrides no Secret hit, so the committed text is the same wherever
    the cut falls in it, and it is not held.

    ``hiding`` is set while the stream hides a line already (``TAIL_LINE``): that
    line is then not reported again, only one after a line break.
    """
    try:
        _load()
        from kiro_crew.security.redaction_structured.engine import resolve
        from kiro_crew.security.redaction_structured.model import IDENTIFIER, SECRET, TAIL_LINE
        from kiro_crew.security.redaction_structured.stream import TAIL, line_break

        with look_behind(context, buf):
            read = _read(buf, final=False)
    except Exception:
        return None
    base, open_from = read.base, read.replace_open_from
    n = base + len(buf)
    # A Cookie line past its bound is a signal, not a span: the stream hides the line.
    line = next((h for h in read.hits if h.tail == TAIL_LINE and h.end == n), None)
    pool = read.resolved
    if line is not None:
        pool = resolve([h for h in read.hits if h.tail != TAIL_LINE])
        # The line already hidden is not reported again; a later one is.
        if hiding and line_break(buf[: line.start - base]) == -1:
            line = None
    # An Identifier-value hit (a structure reading that is not structural).
    resolved = [
        h
        for h in pool
        if not (h.tier == IDENTIFIER and h.detector == "structure" and not h.structural)
    ]
    held = read.open_from
    quiet_from = (
        held - base if held is not None and read.window[held : held + 6].lower() == "cookie" else -1
    )
    tail_hit = next((h for h in resolved if h.tier == SECRET and h.tail and h.end == n), None)
    # A run decided inside a Cookie line to hide is hidden with the line, so the line's
    # name is never dropped ahead of a later pair on it.
    if line is not None and tail_hit is not None and tail_hit.start >= line.start:
        tail_hit = None
    if tail_hit is not None and (open_from is None or open_from >= tail_hit.start):
        return StreamRead(tail_hit.start - base, tail_hit.tail, tag=_tag(tail_hit.rule_id))
    cut = n if boundary else _tail_cut(buf, base, n - TAIL)
    undecided = open_from is not None
    if open_from is not None:
        cut = min(cut, open_from)
    if line is not None:
        cut = min(cut, line.start)
        undecided = True
    cut = max(cut, base)
    moved = True
    while moved:
        moved = False
        for h in resolved:
            # A hit not committed yet keeps the text it was decided from (its
            # anchor) buffered, so the window that commits it decides it alike.
            first = h.start if h.anchor < 0 else min(h.start, h.anchor)
            # The cut only ever moves back (an anchor in the look-behind holds it at
            # ``base``), so the loop ends.
            if first < cut < h.end and max(base, first) < cut:
                cut = max(base, first)
                moved = undecided = True
    if line is not None and cut < line.start:
        # Another hold stops the cut before the line: keep the Cookie name with it, so
        # the next read still decides the line from its name.
        cut = max(base, min(cut, line.anchor))
    if line is not None and cut == line.start:
        return StreamRead(cut - base, TAIL_LINE, tag=_tag(line.rule_id))
    return StreamRead(cut - base, undecided=undecided, quiet_from=quiet_from)


def discard_run(tail: str, pad: int, buf: str) -> tuple[int, int, bool]:
    """``(dropped, pad, done)`` for a dropped unit's continuation in ``buf``.

    The structured stream drops with the same reader, :func:`.stream.discard`.
    """
    from kiro_crew.security.redaction_structured.stream import discard

    return discard(tail, pad, buf)


def stream_context_max() -> int:
    """How much raw emitted text a stream keeps for :func:`look_behind`."""
    from kiro_crew.security.redaction_structured.stream import CONTEXT

    return CONTEXT


def line_break(text: str) -> int:
    """:func:`.stream.line_break`: the first line break in *text*, or -1."""
    from kiro_crew.security.redaction_structured.stream import line_break as first

    return first(text)


def quiet(held: str, chunk: str) -> bool:
    """:func:`.stream.quiet`: whether a feed may skip the read for a held Cookie line."""
    from kiro_crew.security.redaction_structured.stream import quiet as skip

    return skip(held, chunk)
