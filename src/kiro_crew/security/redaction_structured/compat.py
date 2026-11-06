"""The structured detectors in front of the credential plan, behind an internal switch.

:func:`kiro_crew.security.redaction._credential_redaction_plan` is the one plan
every credential entry point splices (``redact_credentials``,
``redact_credentials_with_records``, ``pass_compose``). With the internal switch
(:func:`use_rule_table`) ON it calls :func:`credential_plan_via_table`:

1. the structured detectors (:mod:`.engine`) read the text and resolve their hits;
2. each Secret hit is replaced by
   :data:`~kiro_crew.security.redaction.REDACTED_CREDENTIAL_TAG`, and each
   Identifier value (:func:`.protection.masks_for`) by as many :data:`PLACEHOLDER`
   characters, which no pass reads as part of a credential;
3. the plan's own passes run on that text: pass 1 without the branches the
   detectors cover (:func:`residual_matches`), then passes 2-4 unchanged;
4. their spans are moved back onto the original text (a span edge inside a tag
   widens to the whole hit the tag replaced) and merged with the Secret hits. An
   Identifier value is restored as it was unless a pass's span covers it.

With the switch OFF nothing here runs and the plan is unchanged.

Pass 1 without the covered branches
-----------------------------------
With the switch on, pass 1 does not report a match whose rule is in
:data:`COVERED_RULES` (the detectors read those formats), nor a bare AWS key ID
(an Identifier), nor a labelled ``AccessKeyId`` whose value holds a
:data:`PLACEHOLDER` (a protected key ID). Every other branch runs as before,
including the URL userinfo branch; passes 2-4 are not narrowed. The stream's
pass-1 holds read the same matches (:func:`kiro_crew.security.redaction._pass1_matches`).

Shapes kept
-----------
* spans are ``(start, end, tag)``, sorted and disjoint;
* one ``Redacted credential pattern (N chars)`` warning per Secret hit, in text
  order, before the passes' own warnings; a pass-1 warning gives the length of
  its span on the original text;
* ``rules`` maps each span's start to ``(rule, label)``. A Secret hit carries the
  old pass-1 rule name for its format (``github_token``, ``aws_secret_access_key``,
  ...), because the source cards read those names; its label is ``""`` and the
  field name before the value stays visible.

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

The switch reads a module flag that only code sets (:func:`publish_use_rule_table`).
It is ON and there is no user setting for it.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Collection, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import kiro_crew.security.redaction as redaction

if TYPE_CHECKING:
    from kiro_crew.security.redaction_structured.model import Hit
    from kiro_crew.security.redaction_structured.protection import Mask

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
}


def old_rule(rule_id: str) -> str:
    """The pass-1 rule name a source card reads for a table rule id."""
    return _OLD_RULE.get(rule_id, "credential_pattern")


#: Pass-1 rules whose formats the detectors read; pass 1 does not report them
#: with the switch on. The URL userinfo branch and the vendor rows the detectors
#: have no row for are not here and keep running.
COVERED_RULES = frozenset({"private_key", "jwt", "aws_secret_access_key", "aws_session_token"})

#: What a protected Identifier value is written as while the passes read the
#: text: one private-use character per original character. No pass reads it as
#: a word, a base64 or hex character, a separator or white space.
PLACEHOLDER = "\U000f0000"

_KEY_ID_RULE = "aws_access_key_id"
_JWT_RULE = "jwt"


def _jose_refusal_ruled(text: str, m: Any) -> bool:
    """Whether a pass-1 ``jwt`` match is one the detectors refuse by design.

    The plan's passes read text whose detector Secret hits are already tags, so a
    ``jwt`` match left there is a token the detectors refused. Two refusals are
    by design and stay visible: a token glued to a base64url character on its
    left, and a first segment that decodes to no JSON header naming an ``alg``
    other than ``none``. Any other refusal (a trailing segment such as ``.json``,
    a segment past a row's length bound, a payload that is not an object) leaves
    the match to pass 1, as with the switch off.
    """
    from kiro_crew.security.redaction_structured.charsets import B64URL
    from kiro_crew.security.redaction_structured.checks import _DECODE_CAP, b64url_json_object

    start = m.start()
    if start and text[start - 1] in B64URL:
        return True
    head = m.group().split(".", 1)[0]
    if len(head) > _DECODE_CAP:
        return False
    header = b64url_json_object(head)
    alg = header.get("alg") if header is not None else None
    return not (isinstance(alg, str) and alg and alg.lower() != "none")


def residual_matches(text: str) -> Iterator[Any]:
    """Pass 1's matches (``re.Match`` objects) over ``text`` without the covered branches.

    Dropped: a match whose rule is in :data:`COVERED_RULES`, a bare AWS key ID
    (an Identifier the detectors keep visible), and a labelled ``AccessKeyId``
    whose value holds a :data:`PLACEHOLDER` (its key ID is protected).
    A ``jwt`` match is dropped only for a refusal the detectors make by design
    (:func:`_jose_refusal_ruled`); pass 1 still removes every other one.
    """
    for m in redaction._credential_matches(text):
        rule, label = redaction._pass1_rule(m.group())
        if rule in COVERED_RULES and (rule != _JWT_RULE or _jose_refusal_ruled(text, m)):
            continue
        if rule == _KEY_ID_RULE and (not label or PLACEHOLDER in m.group()[len(label) :]):
            continue
        yield m


# ── Switch ──

_USE_RULE_TABLE = True
_PUBLISH_LOCK = threading.Lock()


def publish_use_rule_table(enabled: bool) -> None:
    """Set what :func:`use_rule_table` answers. Code only; no user setting reads it."""
    global _USE_RULE_TABLE
    with _PUBLISH_LOCK:
        _USE_RULE_TABLE = bool(enabled)


def use_rule_table() -> bool:
    """Whether the credential plan reads the structured detectors first (True) or not."""
    return _USE_RULE_TABLE


# ── Detectors ──

#: Set once a load fails, so a broken table costs one failed import per process
#: rather than one per call; every plan then fails closed.
_LOAD_FAILED = False


def _load() -> None:
    """Import the detectors and build their table; raises if that ever failed."""
    global _LOAD_FAILED
    if _LOAD_FAILED:
        raise RuntimeError("structured credential detectors unavailable")
    try:
        from kiro_crew.security.redaction_structured import engine, table

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


def _read(text: str, final: bool = True) -> _Read:
    from kiro_crew.security.redaction_structured.engine import resolve, scan

    pair = _LOOK_BEHIND.get()
    context = pair[0] if pair is not None and pair[1] == text else ""
    base = len(context)
    window = context + text
    result = scan(window, final=final, origin=base)
    hits = [h for h in result.hits if h.start >= base]
    return _Read(window, hits, resolve(hits), result.open_from, base)


def _resolved(text: str, final: bool = True) -> tuple[list[Hit], int | None, int]:
    """``(resolved hits, open_from, base)`` over ``context + text``; offsets in that window."""
    read = _read(text, final)
    return read.resolved, read.open_from, read.base


def _secrets_of(read: _Read) -> list[Hit]:
    from dataclasses import replace

    from kiro_crew.security.redaction_structured.model import SECRET

    base = read.base
    return [
        replace(h, start=h.start - base, end=h.end - base) if base else h
        for h in read.resolved
        if h.tier == SECRET
    ]


def _masks_of(read: _Read) -> list[Mask]:
    """The protected Identifier values in ``text`` offsets (none in the look-behind)."""
    from kiro_crew.security.redaction_structured.protection import masks_for

    base = read.base
    masks = masks_for(read.window, read.hits, read.resolved)
    return [(s - base, e - base, r) for s, e, r in masks if s >= base]


def secret_hits(text: str) -> list[Hit]:
    """The resolved Secret hits over ``text``, in text order, in ``text`` offsets."""
    return _secrets_of(_read(text))


def protected_values(text: str) -> list[Mask]:
    """The Identifier values the plan protects in ``text``, as ``(start, end, rule_id)``."""
    return _masks_of(_read(text))


# ── Plan ──

_counting = threading.local()


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
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs the exception type only, never the text or a value
    logger.warning("credential redaction failed (%s); whole text replaced", type(exc).__name__)
    _count_fail_closed()
    return (
        [(0, len(text), redaction.REDACTION_UNAVAILABLE_TAG)],
        ["Redaction unavailable: whole text replaced"],
        {0: redaction.REDACTION_UNAVAILABLE_RECORD},
    )


def _merge(engine: list[_Span], passes: list[_Span]) -> list[_Span]:
    """Sorted, disjoint union of two span lists; an overlap keeps the earlier span's tag."""
    out: list[_Span] = []
    for start, end, tag in sorted(engine + passes, key=lambda s: (s[0], -s[1])):
        if out and start < out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end, out[-1][2])
            continue
        out.append((start, end, tag))
    return out


def _with_placeholders(text: str, masks: list[Mask]) -> str:
    """``text`` with each mask's characters written as :data:`PLACEHOLDER`; same length."""
    parts: list[str] = []
    cursor = 0
    for start, end, _rule in masks:
        parts.append(text[cursor:start])
        parts.append(PLACEHOLDER * (end - start))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _token_param_gaps(
    plain: str, hits: list[Hit], spans: list[_Span]
) -> tuple[list[_Span], list[str]]:
    """The parts of a ``token=`` value a detector hit only partly covers, still in clear.

    The passes read the text with each hit replaced by the tag, so a value that
    starts with a hit reads to the token parameter pass as already redacted and
    is skipped. Main's pass redacts such a value now, gaps and all; this does the
    same for every value a hit touches. ``plain`` is the text with only the masks
    written (same length as the input).
    """
    tag = redaction.REDACTED_CREDENTIAL_TAG
    out: list[_Span] = []
    warnings: list[str] = []
    for m in redaction._TOKEN_PARAM_RE.finditer(plain):
        vs, ve = m.start(1), m.end(1)
        if not any(h.start < ve and h.end > vs for h in hits):
            continue
        gaps = redaction._uncovered(vs, ve, spans)
        if gaps:
            out += [(s, e, tag) for s, e in gaps]
            warnings.append(f"Redacted token parameter value ({ve - vs} chars)")
    return out, warnings


def _combine(
    text: str,
    hits: list[Hit],
    masks: list[Mask],
    defer_token_param: bool,
    judged_sri_values: Collection[str],
) -> _Plan:
    """Run the plan's passes with each hit tagged and each mask hidden; merge onto ``text``.

    A mask never overlaps a hit (:func:`.protection.masks_for`), and writing it keeps
    the text's length, so the hits' offsets hold on the masked text.
    """
    from kiro_crew.security.pass_compose import _map_back, _splice

    tag = redaction.REDACTED_CREDENTIAL_TAG
    hit_spans = [(h.start, h.end, tag) for h in hits]
    plain = _with_placeholders(text, masks)
    # A ``token=`` value a hit touches is claimed here, gaps and all, before the
    # passes read it: they would see it as tags and clear text mixed and size its
    # warning on that. Its gaps are spliced like hits, so the token parameter
    # pass skips the value and its one warning is the one written below.
    gap_spans, gap_warnings = _token_param_gaps(plain, hits, hit_spans)
    claimed = sorted(hit_spans + gap_spans)
    masked, rows = _splice(plain, claimed)
    pass_warnings: list[str] = []
    pass_rules: dict[int, tuple[str, str]] = {}
    pass1 = redaction._pattern_pass(masked, pass_warnings, pass_rules)
    # A pass-1 warning gives its span's length on the original text.
    pass_warnings = [
        f"Redacted credential pattern ({end - start} chars)"
        for start, end, _t in _map_back(pass1, rows)
    ]
    found, pass_warnings, pass_rules = redaction._later_passes(
        masked, list(pass1), pass_warnings, pass_rules, defer_token_param, judged_sri_values
    )
    moved = _map_back(found, rows)
    spans = _merge(claimed, moved)
    pass_warnings += gap_warnings

    rules: dict[int, tuple[str, str]] = {}
    for (m_start, _m_end, _t), (start, _end, _tag) in zip(found, moved, strict=True):
        rules.setdefault(start, pass_rules.get(m_start, ("credential_pattern", "")))
    for start, _end, _tag in gap_spans:
        rules.setdefault(start, ("token_parameter", ""))
    for h in hits:
        rules[h.start] = (old_rule(h.rule_id), "")
    kept = {s for s, _e, _t in spans}
    rules = {s: r for s, r in rules.items() if s in kept}
    for s in kept - rules.keys():
        rules[s] = ("credential_pattern", "")
    warnings = [f"Redacted credential pattern ({h.end - h.start} chars)" for h in hits]
    return spans, warnings + pass_warnings, rules


def credential_plan_via_table(
    text: str,
    defer_token_param: bool = False,
    judged_sri_values: Collection[str] = (),
) -> _Plan:
    """The credential plan with the structured detectors read first. Never raises.

    Arguments and result are ``redaction._credential_redaction_plan``'s; the
    owner-view bypass is decided by that caller before this runs.
    """
    try:
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        _load()
        read = _read(text)
        hits, masks = _secrets_of(read), _masks_of(read)
        if not hits and not masks:
            return redaction._main_plan(text, defer_token_param, judged_sri_values)
        return _combine(text, hits, masks, defer_token_param, judged_sri_values)
    except Exception as exc:
        return _fail_closed(text if isinstance(text, str) else "", exc)


# ── Stream ──


@dataclass(frozen=True, slots=True)
class StreamRead:
    """Where a stream buffer must be held for the detectors, in buffer offsets.

    ``cut`` is the furthest the buffer may be committed. When ``tail`` is set, a
    Secret unit starting at ``cut`` is decided while still running to the
    buffer's end: the stream replaces it and drops input to its end.
    """

    cut: int
    tail: str = ""


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


def stream_read(context: str, buf: str, *, boundary: bool = False) -> StreamRead | None:
    """The detectors' hold on ``buf`` read after ``context``; None with the switch off.

    None also when the detectors cannot load: the batch plan fails closed on the
    same error, so there is nothing to hold for.

    With ``boundary`` (a display boundary: a tool call, a prompt, the turn's end)
    the fixed :data:`~.stream.TAIL` hold is skipped; every unit still undecided and
    every hit the cut would split is held as before.
    """
    if not use_rule_table():
        return None
    try:
        _load()
        from kiro_crew.security.redaction_structured.model import SECRET
        from kiro_crew.security.redaction_structured.stream import TAIL

        with look_behind(context, buf):
            resolved, open_from, base = _resolved(buf, final=False)
    except Exception:
        return None
    n = base + len(buf)
    tail_hit = next((h for h in resolved if h.tier == SECRET and h.tail and h.end == n), None)
    if tail_hit is not None and (open_from is None or open_from >= tail_hit.start):
        return StreamRead(tail_hit.start - base, tail_hit.tail)
    cut = n if boundary else _tail_cut(buf, base, n - TAIL)
    if open_from is not None:
        cut = min(cut, open_from)
    cut = max(cut, base)
    moved = True
    while moved:
        moved = False
        for h in resolved:
            # A hit not committed yet keeps the text it was decided from (its
            # anchor) buffered, so the window that commits it decides it alike.
            first = h.start if h.anchor < 0 else min(h.start, h.anchor)
            if first < cut < h.end:
                cut = max(base, first)
                moved = True
    return StreamRead(cut - base)


def discard_run(tail: str, pad: int, buf: str) -> tuple[int, int, bool]:
    """``(dropped, pad, done)`` for a dropped unit's continuation in ``buf``.

    ``tail`` is :data:`~.model.TAIL_ALL` (the unit runs to the end of the text:
    drop everything) or :data:`~.model.TAIL_B64RUN` (drop the rest of a base64
    run and at most two ``=`` after it). ``pad`` counts the ``=`` already dropped.
    """
    from kiro_crew.security.redaction_structured.charsets import B64STD
    from kiro_crew.security.redaction_structured.model import TAIL_ALL

    if tail == TAIL_ALL:
        return len(buf), pad, False
    i = 0
    if pad == 0:
        i = len(buf) - len(buf.lstrip(B64STD))
    while i < len(buf) and buf[i] == "=" and pad < 2:
        pad += 1
        i += 1
    return i, pad, not (i == len(buf) and pad < 2)


def stream_context_max() -> int:
    """How much raw emitted text a stream keeps for :func:`look_behind`."""
    from kiro_crew.security.redaction_structured.stream import CONTEXT

    return CONTEXT
