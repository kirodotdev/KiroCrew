"""The structured detectors in front of the credential plan, behind an internal switch.

:func:`kiro_crew.security.redaction._credential_redaction_plan` is the one plan
every credential entry point splices (``redact_credentials``,
``redact_credentials_with_records``, ``pass_compose``). With the internal switch
(:func:`use_rule_table`) ON it calls :func:`credential_plan_via_table`:

1. the structured detectors (:mod:`.engine`) read the text and resolve their hits;
2. each Secret hit is replaced by
   :data:`~kiro_crew.security.redaction.REDACTED_CREDENTIAL_TAG`, and the plan's
   own passes (pass 1's fixed patterns, then passes 2-4) run on that text,
   unchanged;
3. their spans are moved back onto the original text (a span edge inside a tag
   widens to the whole hit the tag replaced) and merged with the Secret hits.

With the switch OFF nothing here runs and the plan is unchanged.

Shapes kept
-----------
* spans are ``(start, end, tag)``, sorted and disjoint;
* one ``Redacted credential pattern (N chars)`` warning per Secret hit, in text
  order, before the passes' own warnings;
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
It is OFF and there is no user setting for it.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Collection, Iterator
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
}


def old_rule(rule_id: str) -> str:
    """The pass-1 rule name a source card reads for a table rule id."""
    return _OLD_RULE.get(rule_id, "credential_pattern")


# ── Switch ──

_USE_RULE_TABLE = False
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


def _resolved(text: str, final: bool = True) -> tuple[list[Hit], int | None, int]:
    """``(resolved hits, open_from, base)`` over ``context + text``; offsets in that window."""
    from kiro_crew.security.redaction_structured.engine import resolve, scan

    pair = _LOOK_BEHIND.get()
    context = pair[0] if pair is not None and pair[1] == text else ""
    base = len(context)
    result = scan(context + text, final=final, origin=base)
    return resolve([h for h in result.hits if h.start >= base]), result.open_from, base


def secret_hits(text: str) -> list[Hit]:
    """The resolved Secret hits over ``text``, in text order, in ``text`` offsets."""
    from dataclasses import replace

    from kiro_crew.security.redaction_structured.model import SECRET

    resolved, _open, base = _resolved(text)
    return [
        replace(h, start=h.start - base, end=h.end - base) if base else h
        for h in resolved
        if h.tier == SECRET
    ]


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


def _combine(
    text: str,
    hits: list[Hit],
    defer_token_param: bool,
    judged_sri_values: Collection[str],
    token_param_prefixes: tuple[str, ...] | None = None,
) -> _Plan:
    """Run the plan's passes on ``text`` with each hit tagged; merge onto ``text``."""
    from kiro_crew.security.pass_compose import _map_back, _splice

    tag = redaction.REDACTED_CREDENTIAL_TAG
    hit_spans = [(h.start, h.end, tag) for h in hits]
    masked, rows = _splice(text, hit_spans)
    found, pass_warnings, pass_rules = redaction._main_plan(
        masked, defer_token_param, judged_sri_values, token_param_prefixes
    )
    moved = _map_back(found, rows)
    spans = _merge(hit_spans, moved)

    rules: dict[int, tuple[str, str]] = {}
    for (m_start, _m_end, _t), (start, _end, _tag) in zip(found, moved, strict=True):
        rules.setdefault(start, pass_rules.get(m_start, ("credential_pattern", "")))
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
    token_param_prefixes: tuple[str, ...] | None = None,
) -> _Plan:
    """The credential plan with the structured detectors read first. Never raises.

    Arguments and result are ``redaction._credential_redaction_plan``'s; the
    owner-view bypass is decided by that caller before this runs.
    """
    try:
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        _load()
        hits = secret_hits(text)
        if not hits:
            return redaction._main_plan(
                text, defer_token_param, judged_sri_values, token_param_prefixes
            )
        return _combine(text, hits, defer_token_param, judged_sri_values, token_param_prefixes)
    except Exception as exc:
        return _fail_closed(text if isinstance(text, str) else "", exc)


# ── Stream ──


@dataclass(frozen=True, slots=True)
class StreamRead:
    """Where a stream buffer must be held for the detectors, in buffer offsets.

    ``cut`` is the furthest the buffer may be committed. When ``tail`` is set, a
    Secret unit starting at ``cut`` is decided while still running to the
    buffer's end: the stream replaces it and drops input to its end.
    ``undecided`` is set when ``cut`` stops at a unit the detectors have not
    decided yet (an open unit, or a hit the cut would split), rather than only
    at the anchor-prefix margin.
    """

    cut: int
    tail: str = ""
    undecided: bool = False


def stream_read(context: str, buf: str) -> StreamRead | None:
    """The detectors' hold on ``buf`` read after ``context``; None with the switch off.

    None also when the detectors cannot load: the batch plan fails closed on the
    same error, so there is nothing to hold for.
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
    cut = n - TAIL
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
            first = h.start if h.anchor < 0 else min(h.start, h.anchor)
            if first < cut < h.end:
                cut = max(base, first)
                moved = undecided = True
    return StreamRead(cut - base, undecided=undecided)


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
