"""GitHub pull-request FETCHER: reads one pull request's facts, judges nothing.

Every tick this module fetches the subject's current state and hands the whole
observation over. It holds no notion of "actionable": whether a tick is worth
the owning session's turn is answered by the wake judge against the loop's own
criteria, and the single deterministic mapping that remains -- a merged or
closed pull request ends the watch -- belongs to the auto-nudge core, which is
the layer that can act on it.

That split is why this file is mostly TRANSPORT. What a fetcher gets wrong is
not a misjudged signal, it is a reading that is silently incomplete, so the
work here is spent on:

- bounded retry with exponential backoff and jitter, per call;
- rate limits read off the response headers, so a tick backs off on a budget
  that is nearly spent rather than discovering that by being refused;
- pagination for check runs, against the API's own ``total_count`` -- a page
  that returns fewer rows than the count declares is detectable, and a rollup
  that carries no count at all is not;
- one status per observation: ``ok`` when every page was read, ``partial`` when
  something was read and something was not, ``unavailable`` when the subject
  could not be reached. A refusal becomes a status, never an exception.

``partial`` matters as much as ``unavailable``. A criterion about failing
checks means something different when the check list itself is short, and a
consumer cannot see that from the tallies, so the observation says so and the
core treats a partial reading as a target it could not read.

Comment and review BODIES are carried, clipped per item and in total. They are
the evidence a typed reading cannot produce: a reviewer's verdict sits in prose
while the lane that carried it reports success. Bodies leave this module in
memory only -- :meth:`PrObservation.bodies` is a separate call from
:meth:`PrObservation.as_facts`, which is the durable half and carries who said
something and when, never what. The judge's own per-item scrub is what screens
a body before it can reach a provider.

The bot's own comments are skipped. Without that the watch is a feedback loop:
the woken agent posts a disposition, the next tick reads a new comment and
wakes it to read what it just wrote.

Message format (the fetcher's configuration, read off ``ctx.message``): JSON
  {"repo": "owner/name", "pr": 123, "host": "github.com"}
Keys this fetcher does not read are ignored, so a watch armed by an earlier
build keeps working.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from kiro_crew.github_runner import resolve_gh, run_gh
from kiro_crew.irq import Probe, Tick, sanitize_label
from kiro_crew.monitoring.models import (
    MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET,
    MAX_MONITOR_CHECK_IDENTITY_CHARS,
    PULL_REQUEST_SUPERSEDED_CHECK_FIELD,
    PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY,
)
from kiro_crew.monitoring.pull_request import (
    ProviderErrorKind,
    classify_provider_error_text,
)

__all__ = [
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE",
    "TERMINAL_STATES",
    "CheckRow",
    "PrObservation",
    "PrWatchProbe",
    "Remark",
    "fetch",
]

#: SEL audit tag for every gh spawn this module makes.
_AUDIT_CALLER = "core:babysit-pr-watch"

#: Per-call wall-clock bound. One tick may make several calls, so the whole-tick
#: budget below is what stops a slow forge from holding the executor thread for
#: the product of the two.
_GH_TIMEOUT_SECS = 25.0

#: Whole-tick budget. Checked before each call: a call is not started once the
#: budget is spent, and the observation reports ``partial`` instead. Bounding the
#: tick rather than only the call is what keeps a paginated read from turning one
#: interval into ten minutes of subprocess time.
_TICK_BUDGET_SECS = 70.0

#: Attempts per call, including the first. Three is the shape of a transient
#: forge failure: one retry covers a dropped connection, two covers a brief 5xx,
#: and a third attempt on a call that failed twice is better spent reporting a
#: partial reading than held against the tick budget.
_MAX_ATTEMPTS = 3

#: Exponential backoff between attempts, with full jitter. Jitter matters because
#: several loops on one host tick on the same cadence: a fixed delay retries them
#: in step, which is the pattern a rate limiter is built to refuse.
_BACKOFF_BASE_SECS = 0.75
_BACKOFF_CAP_SECS = 6.0

#: Longest this module waits for a rate-limit window to reopen. Past it the tick
#: gives up and reports what it has: a reading delayed past its own interval
#: describes some later moment rather than the tick that asked for it.
_MAX_RATE_LIMIT_WAIT_SECS = 15.0

#: Requests left in the window below which a call backs off before being made.
#: A budget spent down to nothing refuses every caller on the host, including the
#: ones that are not loops, so a fetcher yields while a little is left.
_RATE_LIMIT_FLOOR = 2

#: Check-run rows per page, the API's own maximum. Fewer pages is fewer calls.
_CHECK_PAGE_SIZE = 100

#: Pages of check runs one tick reads. At the page size above this covers a board
#: an order of magnitude larger than any this repository produces, and it is what
#: stops a pathological subject from spending the whole tick budget on paging.
_MAX_CHECK_PAGES = 12

#: Comments and reviews carried, newest first. The judge reads the recent end:
#: a pull request twenty review rounds deep holds hundreds of remarks and the one
#: that needs an answer is at the tail.
_MAX_REMARKS = 6

#: One remark body. A reviewer's verdict and its first ask sit at the start of a
#: comment, so this clips rather than samples.
_MAX_BODY_CHARS = 700

#: Every retained body together. The per-item bound alone is a product: six
#: remarks at the item bound is more than the judge's whole state budget, so the
#: total is what actually bounds the carry and the oldest bodies are the ones it
#: sheds.
_MAX_TOTAL_BODY_CHARS = 2_800

#: How far back a remark still counts as recent enough to carry. A fetch bound,
#: reported as data: this module holds no memory, so it cannot know which remarks
#: a previous tick already carried, and a horizon is what keeps arming a watch on
#: a long-running pull request from carrying its whole history.
DEFAULT_REMARK_HORIZON_SECS = 5 * 3600.0

#: Observation statuses. ``partial`` is the one that matters to a consumer: the
#: subject was reached and the reading is incomplete.
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"

#: Pull-request states that END a watch. The vocabulary lives here because it is
#: GitHub's; the DECISION to stop watching on it lives in the auto-nudge core.
TERMINAL_STATES = ("MERGED", "CLOSED")

#: The one host a watch message may pin. Not a configuration point: a subject
#: inferred from a public GitHub URL pins this so a bare ``owner/name`` slug
#: cannot be re-pointed by an ambient ``GH_HOST``. Choosing an enterprise host
#: stays where this module already puts it -- the operator's own gh config.
_PINNABLE_HOST = "github.com"

#: Failing conclusions/states across the check-run and commit-status shapes.
_FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
#: Passing conclusions/states. NEUTRAL and SKIPPED gate nothing.
_PASSING = {"SUCCESS", "NEUTRAL", "SKIPPED"}
#: Superseded rows: a force-push twin or a re-run leftover.
_NOISE = {"CANCELLED", "STALE"}
#: Not settled yet; the empty string is a row carrying neither a conclusion nor a
#: state.
_PENDING = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", ""}

#: Bucket names, in the order a reader wants them.
BUCKETS = ("failing", "pending", "passing", "noise", "unknown")

#: Response-header names this module reads, lowercased.
_H_REMAINING = "x-ratelimit-remaining"
_H_RESET = "x-ratelimit-reset"
_H_RETRY_AFTER = "retry-after"


#: stderr signatures worth another attempt: a transport hiccup or a server fault.
_RETRYABLE_RE = re.compile(
    r"\b5\d\d\b|timeout|timed out|temporary failure|connection reset|"
    r"connection refused|EOF|broken pipe|server error|bad gateway|unavailable",
    re.IGNORECASE,
)

#: Control characters a body may not carry onward. Terminal escapes in third-party
#: prose reach a log, a transcript row and a notification, so they are stripped at
#: the point the prose enters the process.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


#: How long any provider-chosen string may be once the reading RETAINS it. The facts
#: become the durable monitor record and are re-serialised every tick, so a field a
#: third party names -- a forge node id, a timestamp as the forge spelled it, a state
#: word -- is unbounded text on disk until something clips it. One constant for the
#: whole population: a second number would drift, and the reader holding the smaller
#: one would disagree about what it is looking at.
_MAX_RETAINED_FIELD_CHARS = 200


def _bounded_field(value: object) -> str:
    """One retained provider-chosen string, clipped to :data:`_MAX_RETAINED_FIELD_CHARS`.

    Every string in the facts that a third party can choose passes through here at the
    point of RETENTION, which is the only place that can hold for fields added later.
    No digest suffix, unlike a check identity: these are ids and short state words
    whose prefix is already distinguishing, and a clip long enough to matter means the
    forge sent something no reader was going to use anyway.
    """
    text = str(value or "")
    return text[:_MAX_RETAINED_FIELD_CHARS]


def _bounded_check_identity(identity: str) -> str:
    """One check identity, clipped to the canonical bound with a digest suffix.

    The suffix is what keeps clipping from COLLIDING: two workflow names sharing a
    long prefix would otherwise clip to the same string and read as one lane, which
    is the failure a bound is supposed to avoid rather than introduce.
    """
    if len(identity) <= MAX_MONITOR_CHECK_IDENTITY_CHARS:
        return identity
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{identity[: MAX_MONITOR_CHECK_IDENTITY_CHARS - len(digest) - 1]}#{digest}"


def _sanitized_body_with_clip(value: object, limit: int = _MAX_BODY_CHARS) -> tuple[str, bool, str]:
    """*value* as bounded plain text, whether the CLIP shortened it, and its whole digest.

    The normalisation shortens as well: a CRLF pair becomes one newline, runs of blank
    lines collapse, and surrounding whitespace goes. So a length comparison against
    what the forge returned marks any comment written in a web editor as truncated,
    and that flag is durable -- it rides in the facts and renders to the judge as a
    clipped body. Only the final slice truncates, so only the final slice sets it.

    The digest is taken from the WHOLE normalised text, before the slice, and only the
    digest leaves here -- never the text beyond the clip. A digest of the kept prefix
    would be blind to an edit past the clip boundary, which is exactly the edit that
    asks for something in a long comment.
    """
    if not isinstance(value, str) or not value:
        return "", False, ""
    text = _CONTROL_RE.sub(" ", value.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit], len(text) > limit, _body_digest(text)


def sanitize_body(value: object, limit: int = _MAX_BODY_CHARS) -> str:
    """*value* as bounded plain text, or ``""``.

    Control characters go, runs of blank lines collapse, and the result is clipped
    to *limit*. The judge's own per-item scrub is what decides whether a body may
    be sent; this only makes it safe to hold, log and render. A caller that also
    needs to know whether the clip fired reads :func:`_sanitized_body_with_clip`,
    which this delegates to so the normalisation has one implementation.
    """
    return _sanitized_body_with_clip(value, limit)[0]


def _age_secs(raw: object, clock: float | None = None) -> float | None:
    """Seconds since an ISO-8601 GitHub timestamp, or ``None`` when unusable.

    ``None`` rather than 0 for anything unparseable, and a caller reads ``None`` as
    "cannot tell how old this is". A remark of unknown age assumed fresh would be
    carried on every tick for as long as the watch runs.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # GitHub spells UTC as a trailing Z, which fromisoformat rejects before
        # Python 3.11 -- normalize rather than depend on the interpreter version.
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = time.time() if clock is None else clock
    return max(0.0, now - stamp.timestamp())


@dataclass(frozen=True)
class CheckRow:
    """One check identity as this tick read it.

    Attributes:
        name: Workflow-qualified display identity, so two workflows sharing a
            check name stay two rows.
        bare: The name GitHub's own interface shows.
        bucket: One of :data:`BUCKETS`.
        started_at: The row's start time, or ``""``.
    """

    name: str
    bare: str
    bucket: str
    started_at: str = ""


@dataclass(frozen=True)
class Remark:
    """One thing said about the pull request, with its body.

    Attributes:
        kind: ``comment`` or ``review``.
        ident: The forge's own id, stable across ticks.
        author: Login, sanitized.
        at: ISO-8601 timestamp as the forge spelled it.
        age_s: Seconds since *at*, as this tick measured it.
        verdict: A review's state (``APPROVED``, ``CHANGES_REQUESTED``, ...);
            ``""`` for a comment.
        body: Bounded plain text, possibly empty -- a review may carry a state
            and no prose.
        clipped: True when *body* is shorter than what the forge returned.
        whole_body_digest: Digest of the WHOLE normalised body, taken before the
            clip. It is what tells an edited remark from an unchanged one, and a
            digest of the clipped text could not: editing a long comment past the
            clip boundary leaves the kept prefix byte-identical, so the reading
            would match and the wake it asks for would be suppressed.
    """

    kind: str
    ident: str
    author: str
    at: str
    age_s: float
    verdict: str = ""
    body: str = ""
    clipped: bool = False
    whole_body_digest: str = ""


@dataclass(frozen=True)
class PrObservation:
    """One tick's complete reading of one pull request.

    Two accessors rather than one dict, because the two halves have different
    lifetimes. :meth:`as_facts` is what a caller may keep: who said something and
    when, every typed fact, every completeness flag. :meth:`bodies` is prose a
    third party wrote, and it stays in the process that fetched it.
    """

    repo: str
    pr: int
    host: str
    status: str
    observed_at: float
    incomplete: tuple[str, ...] = ()
    state: str = ""
    draft: bool | None = None
    mergeability: str = ""
    merge_state: str = ""
    review_decision: str = ""
    head: str = ""
    merged_at: str = ""
    checks: tuple[CheckRow, ...] = ()
    checks_declared: int = 0
    checks_read: int = 0
    checks_complete: bool = True
    remarks: tuple[Remark, ...] = ()
    remarks_total: int = 0

    @property
    def subject(self) -> str:
        """``owner/name#123``."""
        return f"{self.repo}#{self.pr}"

    @property
    def reached(self) -> bool:
        """Whether the subject was read at all this tick."""
        return self.status in (STATUS_OK, STATUS_PARTIAL)

    @property
    def is_terminal(self) -> bool:
        """Whether the pull request has ended. Only ever true of a read subject.

        The auto-nudge core is what acts on this; the property only states the
        fact, and states it from the two independent signals the forge gives so a
        missing ``state`` on a merged pull request still reads as terminal.
        """
        if not self.reached:
            return False
        return bool(self.merged_at) or self.state in TERMINAL_STATES

    @property
    def merged(self) -> bool:
        """Whether the end was a merge rather than a close.

        The two are not the same ending: a merge needs nothing further, while a
        close without a merge leaves a question -- reopen or abandon -- and a
        consumer that calls both a success tells the owner "nothing to do" about
        the one case that needs them.
        """
        return bool(self.merged_at) or self.state == "MERGED"

    def bucket(self, name: str) -> tuple[str, ...]:
        """The check identities in one bucket, sorted for a stable reading."""
        return tuple(sorted(row.name for row in self.checks if row.bucket == name))

    def _bounded_buckets(self) -> dict[str, list[str]]:
        """The buckets as RETAINED, bounded the way the canonical writer bounds them.

        These identities are third-party strings -- a contributor names their own
        workflows and jobs -- and they are kept, not merely rendered: the facts become
        the durable monitor record and are rewritten on every tick. A fork matrix can
        carry up to ``_MAX_CHECK_PAGES * _CHECK_PAGE_SIZE`` rows, so an unbounded list
        here puts hundreds of kilobytes of provider-chosen text into that record.

        The caps and the overflow marker are the canonical writer's own names rather
        than new literals, because this is one population with one bound: two numbers
        for it would drift, and the reader holding the smaller one would disagree about
        what a complete board is. Truncation is said out loud in the retained dict, so
        a reader cannot mistake a clipped board for a whole one.

        The displaced bucket is bounded on its own terms, the way the canonical writer
        bounds it. A displaced row carries no verdict, so however many of them a head
        accumulates every live row is still measured: they are kept OUT of the overflow
        test, because a board whose live lanes were all read is complete beside any
        number of them. And their own cut spends its last slot on the canonical
        displaced sentinel rather than on the board-wide one, so a reader sees that
        THIS list was clipped without being told the board was short.
        """
        live = {
            "failed": list(self.bucket("failing")),
            "pending": list(self.bucket("pending")),
            "passed": list(self.bucket("passing")),
            "unknown": list(self.bucket("unknown")),
        }
        displaced = list(self.bucket("noise"))
        overflow = not self.checks_complete or any(
            len(values) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET for values in live.values()
        )
        bounded = {
            state: [
                _bounded_check_identity(value)
                for value in values[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET]
            ]
            for state, values in live.items()
        }
        if overflow:
            bounded["unknown"] = [
                *bounded["unknown"][: MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET - 1],
                "checks:incomplete",
            ]
        bounded_displaced = [
            _bounded_check_identity(value)
            for value in displaced[:MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET]
        ]
        if len(displaced) > MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET:
            bounded_displaced = [
                *bounded_displaced[: MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET - 1],
                PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY,
            ]
        bounded[PULL_REQUEST_SUPERSEDED_CHECK_FIELD] = bounded_displaced
        return bounded

    def as_facts(self) -> dict[str, object]:
        """The durable half: typed facts and remark metadata, no bodies.

        Key names follow the canonical monitor observation wherever the two say
        the same thing (``state``, ``mergeability``, ``review_decision``,
        ``head_revision``, ``checks``, ``checks_complete``), so a reader written
        for that object reads this one without a second spelling.

        Every key here has a reader, and the claim is checkable: the typed facts and
        the remark metadata are read by the collector that builds the judge's evidence,
        and every one of them is also read by the core's reading-digest, which is what
        makes an unchanged subject quiet. A field no consumer reads is a claim the
        reading cannot back, so there is none. What describes the FETCH rather than the
        subject is not here at all: what the window had left stays on the transport,
        which is the one place it is read -- to yield a call on a nearly-spent window.
        """
        facts: dict[str, object] = {
            "kind": "gh-pr",
            "target": self.subject,
            "observation_status": self.status,
            "observed_at": self.observed_at,
            "state": _bounded_field(self.state),
            "mergeability": _bounded_field(self.mergeability),
            "merge_state": _bounded_field(self.merge_state),
            "review_decision": _bounded_field(self.review_decision),
            "head_revision": _bounded_field(self.head),
            "checks": self._bounded_buckets(),
            "checks_complete": self.checks_complete,
            "checks_declared": self.checks_declared,
            "checks_read": self.checks_read,
            "remarks": [
                {
                    "kind": remark.kind,
                    "id": _bounded_field(remark.ident),
                    "author": _bounded_field(remark.author),
                    "at": _bounded_field(remark.at),
                    "age_s": round(remark.age_s, 3),
                    "verdict": _bounded_field(remark.verdict),
                    # Read by the core's reading-digest: an EDITED body changes this
                    # and so changes the reading, which is what stops an edit that asks
                    # for something being taken for an unchanged subject. Taken from the
                    # WHOLE body before either clip, because a digest of the kept prefix
                    # is identical after an edit past the clip boundary -- the long
                    # comment whose tail now asks for something would read as unchanged.
                    # A character count would say nothing this does not, and ``clipped``
                    # already says the body was cut.
                    "body_digest": remark.whole_body_digest,
                    "clipped": remark.clipped,
                }
                for remark in self.remarks
            ],
            "remarks_total": self.remarks_total,
        }
        if self.draft is not None:
            facts["draft"] = self.draft
        if self.incomplete:
            facts["incomplete"] = [_bounded_field(note) for note in self.incomplete]
        return facts

    def bodies(self) -> dict[str, str]:
        """Remark id to body, for the one tick that fetched them. Memory only."""
        return {remark.ident: remark.body for remark in self.remarks if remark.body}


@dataclass
class _Response:
    """One gh call's outcome, headers included."""

    rc: int
    stdout: str = ""
    stderr: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _parse_headers(raw: str) -> tuple[dict[str, str], str]:
    """Split ``gh api --include`` output into ``(headers, body)``.

    gh writes the status line, then the headers, then a blank line, then the body.
    Line endings are normalized first: HTTP spells them CRLF, so a blank line is
    ``\\r\\n\\r\\n`` and contains no ``\\n\\n`` at all -- splitting on the latter
    alone finds no header block, and every header is then lost silently while the
    body still looks intact.

    A payload with no header block at all is returned whole as the body, because a
    caller that guessed wrong about the shape must not lose the response.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    head, sep, body = text.partition("\n\n")
    if not sep or not head.lower().startswith("http/"):
        return {}, raw
    headers: dict[str, str] = {}
    for line in head.splitlines()[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return headers, body


class _Transport:
    """Every gh call one tick makes, with the tick's budget and its counters.

    One object per tick rather than a module function, because the things a
    fetcher has to get right here are all per-tick: how much wall clock is left,
    how many attempts have been spent, and what the rate-limit headers said on the
    call before this one. Passing those between free functions is the shape that
    loses one of them.
    """

    def __init__(self, host: str = "", *, budget_secs: float = _TICK_BUDGET_SECS) -> None:
        self.host = host
        self.deadline = time.monotonic() + max(1.0, budget_secs)
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: float | None = None

    @property
    def budget_left(self) -> float:
        return self.deadline - time.monotonic()

    def _sleep(self, seconds: float) -> None:
        """Wait, never past the tick's own budget. A separate seam for tests."""
        bounded = max(0.0, min(seconds, self.budget_left))
        if bounded > 0:
            time.sleep(bounded)

    def _note_headers(self, headers: dict[str, str]) -> None:
        """Remember what the window said, so the NEXT call can yield before it is refused."""
        raw_remaining = headers.get(_H_REMAINING)
        if raw_remaining is not None:
            try:
                self.rate_limit_remaining = int(raw_remaining)
            except ValueError:
                self.rate_limit_remaining = None
        raw_reset = headers.get(_H_RESET)
        if raw_reset is not None:
            try:
                self.rate_limit_reset = float(raw_reset)
            except ValueError:
                self.rate_limit_reset = None

    def _rate_limit_wait(self, headers: dict[str, str]) -> float:
        """How long to wait for the window, from the response's own numbers.

        ``retry-after`` wins when present: a secondary rate limit says in seconds
        how long to hold off, and it is the only signal for a limit that carries
        no reset epoch. Otherwise the reset epoch is turned into a delay. An absent
        or unusable value falls back to the ordinary backoff, because guessing a
        long wait from no information is how a tick spends its whole budget asleep.
        """
        raw_after = headers.get(_H_RETRY_AFTER) or ""
        try:
            after = float(raw_after)
        except ValueError:
            after = -1.0
        if after >= 0:
            return min(after, _MAX_RATE_LIMIT_WAIT_SECS)
        reset = self.rate_limit_reset
        if headers.get(_H_RESET):
            try:
                reset = float(headers[_H_RESET])
            except ValueError:
                reset = self.rate_limit_reset
        if reset is None:
            return -1.0
        return min(max(0.0, reset - time.time()), _MAX_RATE_LIMIT_WAIT_SECS)

    def _yield_to_window(self) -> bool:
        """Back off BEFORE a call when the window is nearly spent.

        Returns False when the window cannot be waited out inside the tick's
        budget, and the caller then reports a partial reading rather than spending
        an attempt it knows will be refused.
        """
        remaining = self.rate_limit_remaining
        if remaining is None or remaining > _RATE_LIMIT_FLOOR:
            return True
        reset = self.rate_limit_reset
        wait = 0.0 if reset is None else max(0.0, reset - time.time())
        if wait > min(_MAX_RATE_LIMIT_WAIT_SECS, self.budget_left):
            return False
        self._sleep(wait)
        # The window has reopened, so the remembered floor describes a spent window
        # that is gone; forget it and let the next response state the new one.
        self.rate_limit_remaining = None
        return True

    def call(self, args: list[str]) -> _Response:
        """One bounded, audited gh call with retry. Never raises.

        Routed through :func:`github_runner.run_gh` -- the repo's single gh spawn
        chokepoint: the binary is the validated absolute path, the child gets the
        minimal gh-scoped environment, and every invocation leaves an SEL audit
        record.

        ``host`` is pinned because this module addresses its subject as a bare
        ``owner/name`` slug and never passes ``--hostname``. ``GH_HOST`` is one of
        the variables the runner forwards, so on a machine configured for an
        enterprise host the same slug resolves to a DIFFERENT repository, where a
        pull request of that number could be merged.
        """
        if self.budget_left <= 0:
            return _Response(rc=1, stderr="tick budget spent before the call")
        if not self._yield_to_window():
            return _Response(rc=1, stderr="rate-limit window does not reopen inside the tick")
        attempt = 0
        last = _Response(rc=1, stderr="no attempt made")
        while attempt < _MAX_ATTEMPTS:
            attempt += 1
            # Re-read the deadline per attempt, and bound the call by what is ACTUALLY
            # left. A floor under this would let the last attempt of a nearly-spent
            # tick outlive the deadline the whole budget exists to enforce: with 0.2s
            # left a one-second floor hands a hanging call five times the remaining
            # tick. A timeout too small to complete is the honest answer there -- the
            # tick is over, and the reading says so through its status.
            remaining = self.budget_left
            if remaining <= 0:
                last = _Response(rc=1, stderr="tick budget spent before the attempt")
                break
            try:
                proc = run_gh(
                    [resolve_gh(), *args],
                    timeout=min(_GH_TIMEOUT_SECS, remaining),
                    audit_caller=_AUDIT_CALLER,
                    pin_host=self.host,
                )
                headers, body = _parse_headers(proc.stdout or "")
                last = _Response(proc.returncode, body, proc.stderr or "", headers)
                self._note_headers(headers)
            except Exception as exc:  # SetupError, timeout, OSError
                last = _Response(rc=1, stderr=f"{type(exc).__name__}: {exc}"[:400])
            if last.ok:
                return last
            if attempt >= _MAX_ATTEMPTS or self.budget_left <= 0:
                return last
            stderr = last.stderr
            # The SAME marker set the provider readers classify this binary's
            # refusals with. A second one here would be a second answer to one
            # question, and the two drifted in both directions while they existed.
            if classify_provider_error_text(stderr) is ProviderErrorKind.RATE_LIMITED:
                wait = self._rate_limit_wait(last.headers)
                self._sleep(wait if wait >= 0 else self._backoff(attempt))
                continue
            if _RETRYABLE_RE.search(stderr) or not stderr:
                # An empty stderr with a non-zero exit says nothing about the
                # cause, and a transport fault is the common reason for it, so it
                # is retried. A refusal that names itself and is not transient --
                # no such pull request, no credential -- is not: retrying spends
                # the tick's budget to be refused in the same words.
                self._sleep(self._backoff(attempt))
                continue
            return last
        return last

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential with full jitter, capped."""
        ceiling = min(_BACKOFF_CAP_SECS, _BACKOFF_BASE_SECS * (2 ** (attempt - 1)))
        return random.uniform(0.0, ceiling)

    def api(self, path: str, *, paginated_page: int = 0) -> _Response:
        """A REST call, with headers, optionally one page of a paginated list."""
        target = path
        if paginated_page:
            join = "&" if "?" in path else "?"
            target = f"{path}{join}per_page={_CHECK_PAGE_SIZE}&page={paginated_page}"
        return self.call(["api", "--include", "-H", "Accept: application/vnd.github+json", target])


def _bucket(item: dict) -> tuple[str, str]:
    """``(check name, bucket)`` for one check-run or commit-status row.

    Tolerant across both shapes: a check run carries ``status``/``conclusion``, a
    commit status carries ``state``.
    """
    name = sanitize_label(item.get("name") or item.get("context") or "")
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if status and status != "COMPLETED" and not conclusion:
        return name, "pending"
    if conclusion in _FAILING:
        return name, "failing"
    if conclusion in _PASSING:
        return name, "passing"
    if conclusion in _NOISE:
        return name, "noise"
    if conclusion in _PENDING:
        return name, "pending"
    # A vocabulary this build does not know is reported as such rather than folded
    # into a bucket it might not belong to. The judge can be told "one lane reports
    # something unrecognised"; it cannot un-see a row filed as passing.
    return name, "unknown"


#: A check run's details URL for a GitHub Actions job, whose run id identifies the
#: workflow run the job belongs to.
_ACTIONS_RUN_URL = re.compile(r"/actions/runs/(\d+)(?:/|$)")


def _details_of(item: dict) -> str:
    """The row's details URL under either the REST or the GraphQL spelling."""
    return str(item.get("detailsUrl") or item.get("details_url") or item.get("target_url") or "")


def _body_digest(body: str) -> str:
    """A short stable digest of one remark body, or ``""`` for an empty one.

    Recorded on the durable reading so a quiet tick leaves something a reader can
    check the judge's answer against. The bodies themselves are held for one tick and
    written nowhere, which is what makes a WRONG quiet hard to examine afterwards:
    the record would say a remark existed and nothing about what it said. A digest
    keeps the prose out of the record while still identifying it -- the same trade the
    structured reader's own comment fingerprint makes.
    """
    if not body:
        return ""
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:12]


def _run_id_of(item: dict) -> str:
    """The Actions workflow-run id this row belongs to, or ``""``.

    Read from the details URL, which is the only place the REST check-runs payload
    carries it. A row from another app has none.
    """
    match = _ACTIONS_RUN_URL.search(_details_of(item))
    return match.group(1) if match else ""


def _ambiguous_runs(rows: list[dict]) -> set[str]:
    """Run ids whose workflow must be resolved, because a job name is shared.

    One check name appearing under two Actions runs is the only case where the
    workflow is needed: either the runs are two workflow files that must stay two
    rows, or two runs of one workflow that must fold, and nothing else on the row
    says which. A name appearing once needs no qualifier at all, so the common
    board resolves nothing and spends no call -- measured at 40 rows across 19 runs
    with no shared name.
    """
    by_name: dict[str, set[str]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        run_id = _run_id_of(item)
        if not run_id:
            continue
        by_name.setdefault(sanitize_label(item.get("name") or ""), set()).add(run_id)
    return {run for runs in by_name.values() if len(runs) > 1 for run in runs}


def _resolve_workflows(
    transport: _Transport, repo: str, rows: list[dict]
) -> tuple[dict[str, str], bool]:
    """``({run id: workflow label}, every needed run resolved)``.

    The workflow's identity is what a shared job name needs and the check-runs
    payload does not carry: every Actions row wears the same app slug, so the slug
    cannot separate two workflow files that each define a job of that name, and the
    run id separates two runs of one workflow, which is the pair that must fold.
    Resolving the run to its workflow separates the first pair and keeps the second
    together. The workflow's PATH is the label because it is unique, while two
    workflow files may share a display name.
    """
    resolved: dict[str, str] = {}
    whole = True
    for run_id in sorted(_ambiguous_runs(rows)):
        response = transport.api(f"repos/{repo}/actions/runs/{run_id}")
        if not response.ok:
            whole = False
            continue
        try:
            payload = json.loads(response.stdout)
        except (json.JSONDecodeError, RecursionError):
            whole = False
            continue
        if not isinstance(payload, dict):
            whole = False
            continue
        label = sanitize_label(str(payload.get("path") or payload.get("name") or ""))
        workflow_id = payload.get("workflow_id")
        if label:
            resolved[run_id] = label
        elif isinstance(workflow_id, int):
            resolved[run_id] = f"workflow {workflow_id}"
        else:
            whole = False
    return resolved, whole


def _workflow_of(item: dict, resolved: dict[str, str] | None = None) -> str:
    """The identity qualifier for one row, so same-named checks stay distinct.

    An Actions row is qualified by the workflow its run belongs to, and by nothing
    when that is unresolved: the app slug every Actions row carries would be a false
    qualifier, since it reads as "these rows are one lane" on exactly the rows it
    cannot tell apart. A row posted by an external app carries the app's own slug,
    which does separate apps; one carrying neither is discriminated by the stable
    prefix of its details URL -- host plus first path segment.
    """
    named = sanitize_label(item.get("workflowName") or "")
    if named:
        return named
    slug = sanitize_label((item.get("app") or {}).get("slug") or "")
    run_id = _run_id_of(item)
    if run_id:
        return (resolved or {}).get(run_id, "")
    if slug:
        return slug
    details = _details_of(item)
    if not details:
        return ""
    parsed = urlparse(details)
    path = parsed.path.strip("/")
    segment = path.split("/", 1)[0] if path else ""
    return sanitize_label(f"{parsed.netloc}/{segment}" if segment else parsed.netloc)


#: Which bucket survives when duplicate rows for one identity cannot be ordered by
#: time. A row saying "something may be wrong or unfinished" outranks an "all good"
#: row. The reading itself says so too: an unresolved identity is one of the notes
#: that make the whole observation ``partial``, which is what a consumer reads.
_UNDATED_RANK = {"failing": 4, "unknown": 3, "pending": 2, "passing": 1, "noise": 0}


def _family_of(item: dict) -> str:
    """Which sequence a row came from: ``"status"`` for a commit status, else ``"check"``.

    Part of the fold identity because the two are separate sequences in the forge's
    own model, so a check run and a commit status sharing a name are never one lane
    however alike their rows look. Without this they can fold: a status carrying no
    target URL has no qualifier, and neither has a check run whose name was
    unambiguous, so both land on the same key and recency lets the later row stand
    for the earlier one across families.

    Read from the shape rather than passed in, so a caller cannot forget it: a commit
    status names itself with ``context`` and carries a ``state``, where a check run
    carries ``name`` with ``status`` / ``conclusion``.
    """
    if "context" in item and "name" not in item:
        return "status"
    return "check"


def _collapse(
    rows: list, resolved: dict[str, str] | None = None
) -> tuple[tuple[CheckRow, ...], int]:
    """Fold raw rows into one :class:`CheckRow` per identity, newest per name.

    Returns the rows and how many raw rows were folded. Deduping is DATA, not a
    judgment: a re-run leaves both the old row and the new one on the board, so a
    tally over raw rows counts a superseded attempt as a current one. Recency is
    the arbiter in both directions -- a re-run green supersedes a stale red and a
    re-run red supersedes a stale green -- and ISO-8601 timestamps order lexically,
    so a string compare picks the newer row.

    Recency may arbitrate only where one identity means one lane. Two rows of one
    workflow are that; two rows whose workflow could not be resolved are not, and
    neither are a check run and a commit status sharing a name, so those take the
    conservative path below rather than letting the later row stand for the earlier
    one.
    """
    per_key: dict[tuple[str, str, str], CheckRow] = {}
    unordered: set[tuple[str, str, str]] = set()
    runs: dict[tuple[str, str, str], str] = {}
    seen = 0
    for item in rows:
        if not isinstance(item, dict):
            continue
        seen += 1
        name, bucket = _bucket(item)
        workflow = _workflow_of(item, resolved)
        started = str(
            item.get("startedAt") or item.get("started_at") or item.get("created_at") or ""
        )
        bare = name or "(unnamed check)"
        qualified = f"{workflow} / {bare}" if workflow and workflow != bare else bare
        key = (_family_of(item), workflow, bare)
        run_id = _run_id_of(item)
        previous = per_key.get(key)
        if previous is None:
            per_key[key] = CheckRow(qualified, bare, bucket, started)
            runs[key] = run_id
            continue
        if not workflow and run_id and runs.get(key) not in ("", run_id):
            # Two rows under one identity are one lane only where that identity is a
            # workflow. These carry none and come from different runs, so they may be
            # two lanes wearing one name and the later row is not necessarily a re-run
            # of the earlier one. Recency loses its meaning for the whole key.
            unordered.add(key)
        if key not in unordered and started and previous.started_at:
            if started >= previous.started_at:
                per_key[key] = CheckRow(qualified, bare, bucket, started)
            continue
        # Recency cannot arbitrate -- neither row is dated, or the identity is not
        # known to be one lane. Keep the more conservative bucket and say the row is
        # a choice rather than a reading.
        if _UNDATED_RANK.get(bucket, 0) > _UNDATED_RANK.get(previous.bucket, 0):
            per_key[key] = CheckRow(qualified, bare, bucket, started or previous.started_at)
        else:
            per_key[key] = CheckRow(
                previous.name, previous.bare, previous.bucket, previous.started_at
            )
    return tuple(per_key.values()), seen


def _remarks(data: dict, clock: float) -> tuple[tuple[Remark, ...], int]:
    """Comments and reviews inside the horizon, newest first, with bodies.

    Returns the retained remarks and how many the payload held in total, so a
    consumer can tell "three remarks" from "three of forty". The bot's own comments
    are skipped, and the total does not count them: they are not remarks anyone is
    waiting on an answer to.
    """
    collected: list[Remark] = []
    total = 0
    for raw in data.get("comments") or []:
        if not isinstance(raw, dict) or raw.get("viewerDidAuthor"):
            continue
        ident = str(raw.get("id") or "")
        age = _age_secs(raw.get("createdAt"), clock)
        if not ident or age is None:
            continue
        total += 1
        if age > DEFAULT_REMARK_HORIZON_SECS:
            continue
        body, body_clipped, body_digest = _sanitized_body_with_clip(raw.get("body"))
        collected.append(
            Remark(
                kind="comment",
                ident=f"comment:{ident}",
                author=sanitize_label((raw.get("author") or {}).get("login")) or "someone",
                at=str(raw.get("createdAt") or ""),
                age_s=age,
                body=body,
                clipped=body_clipped,
                whole_body_digest=body_digest,
            )
        )
    for raw in data.get("reviews") or []:
        if not isinstance(raw, dict) or raw.get("viewerDidAuthor"):
            continue
        ident = str(raw.get("id") or "")
        age = _age_secs(raw.get("submittedAt"), clock)
        if not ident or age is None:
            continue
        total += 1
        if age > DEFAULT_REMARK_HORIZON_SECS:
            continue
        body, body_clipped, body_digest = _sanitized_body_with_clip(raw.get("body"))
        collected.append(
            Remark(
                kind="review",
                ident=f"review:{ident}",
                author=sanitize_label((raw.get("author") or {}).get("login")) or "someone",
                at=str(raw.get("submittedAt") or ""),
                age_s=age,
                verdict=sanitize_label(raw.get("state")) or "REVIEW",
                body=body,
                clipped=body_clipped,
                whole_body_digest=body_digest,
            )
        )
    collected.sort(key=lambda remark: remark.age_s)
    kept: list[Remark] = []
    spent = 0
    for remark in collected[:_MAX_REMARKS]:
        if spent + len(remark.body) > _MAX_TOTAL_BODY_CHARS:
            room = max(0, _MAX_TOTAL_BODY_CHARS - spent)
            # The remark is KEPT with its body clipped to what is left, rather than
            # dropped: who said something and when is the part a consumer cannot
            # reconstruct, and a review carrying a verdict and no room for its prose
            # is still the signal that a review arrived.
            remark = Remark(
                remark.kind,
                remark.ident,
                remark.author,
                remark.at,
                remark.age_s,
                remark.verdict,
                remark.body[:room],
                True,
                # Carried, not recomputed: this is the SECOND clip on the same body and
                # the digest has to keep describing the whole one. Recomputing it here
                # would make the budget's own pressure look like an edited comment, and
                # taking it from the twice-clipped text would hide a real edit.
                remark.whole_body_digest,
            )
        spent += len(remark.body)
        kept.append(remark)
    return tuple(kept), total


def _parse_config(message: object) -> tuple[str, int, str]:
    """``(repo, pr, host)`` from a watch message, or raise ``ValueError``.

    Every refusal here is permanent -- a malformed message cannot become valid --
    so the caller converts it to a removed watch rather than a retried tick.
    """
    try:
        params = json.loads(str(message or "") or "{}")
    except (json.JSONDecodeError, RecursionError) as exc:
        # RecursionError as well as a decode error: deeply nested JSON blows the
        # interpreter stack inside json.loads and is not a JSONDecodeError, so it
        # would escape uncaught instead of ending the watch, and a cron that raises
        # every tick is auto-paused.
        raise ValueError("pr watch message is not valid JSON") from exc
    if not isinstance(params, dict):
        raise ValueError("pr watch message must be a JSON object")
    repo = params.get("repo") or ""
    pr = params.get("pr")
    # owner/name ONLY -- no host segment. A host inside the watch parameters would
    # let whoever composes the message point a credentialed gh call at an arbitrary
    # server; an enterprise host is selected by the operator's own trusted gh
    # configuration, never by data.
    if not (isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo)):
        raise ValueError('pr watch needs {"repo": "owner/name"}')
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise ValueError('pr watch needs {"pr": positive int}')
    # Deliberately NOT an arbitrary hostname, for the same reason: the only
    # producer passes one constant, so the contract IS the constant. It pins the
    # public host against an ambient GH_HOST rather than choosing a host.
    raw_host = params.get("host")
    host = str(raw_host or "").strip().lower()
    if host and host != _PINNABLE_HOST:
        raise ValueError(f"pr watch host, when given, must be {_PINNABLE_HOST!r}")
    return repo, pr, host


def _fetch_core(transport: _Transport, repo: str, pr: int) -> dict | None:
    """The pull request's own fields, comments and reviews. ``None`` when unread."""
    response = transport.call(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "state,mergedAt,mergeable,mergeStateStatus,reviewDecision,isDraft,"
            "headRefOid,comments,reviews",
        ]
    )
    if not response.ok:
        return None
    try:
        data = json.loads(response.stdout)
    except (json.JSONDecodeError, RecursionError):
        # Same pair as the message parse. A pathologically nested API response reads
        # as "could not observe the subject" rather than raising out of the tick.
        return None
    return data if isinstance(data, dict) else None


def _fetch_counted(
    transport: _Transport, path: str, rows_key: str, label: str
) -> tuple[list[dict], int, bool, str]:
    """``(rows, declared, complete, note)`` for one list endpoint that counts itself.

    Paginated against the API's own ``total_count``. That number is the reason this
    reads endpoints carrying it rather than the rollup gh serves beside the pull
    request: the rollup is a bare array, so a truncated read of it is undetectable,
    while a count that disagrees with the rows returned says so.

    One function for both sequences on purpose. Check runs and commit statuses are
    different endpoints answering the same question -- is a gate red -- and either
    read short presents a short board as whole. Two copies of this logic is how one
    of them ends up without the count check, which is a failing gate omitted from a
    reading that reports itself complete.
    """
    rows: list[dict] = []
    declared = 0
    page = 0
    while page < _MAX_CHECK_PAGES:
        page += 1
        response = transport.api(path, paginated_page=page)
        if not response.ok:
            return rows, declared, False, f"{label} page {page} unread"
        try:
            payload = json.loads(response.stdout)
        except (json.JSONDecodeError, RecursionError):
            return rows, declared, False, f"{label} page {page} unparsable"
        if not isinstance(payload, dict):
            return rows, declared, False, f"{label} page {page} malformed"
        try:
            declared = max(declared, int(payload.get("total_count") or 0))
        except (TypeError, ValueError):
            # An unusable count cannot bound the read, so the reading is reported
            # incomplete rather than trusted at whatever the pages happened to give.
            return rows, declared, False, f"{label} page {page} declares no usable count"
        batch = payload.get(rows_key)
        if not isinstance(batch, list):
            return rows, declared, False, f"{label} page {page} carries no rows"
        rows.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < _CHECK_PAGE_SIZE or len(rows) >= declared > 0:
            break
    else:
        return rows, declared, False, f"{label} exceed the page bound"
    if declared and len(rows) < declared:
        return rows, declared, False, f"{label} read {len(rows)} of {declared}"
    return rows, declared, True, ""


def _fetch_checks(transport: _Transport, repo: str, head: str) -> tuple[list[dict], int, bool, str]:
    """``(rows, declared, complete, note)`` for one head's check runs."""
    return _fetch_counted(
        transport, f"repos/{repo}/commits/{head}/check-runs", "check_runs", "check runs"
    )


def _fetch_statuses(
    transport: _Transport, repo: str, head: str
) -> tuple[list[dict], int, bool, str]:
    """``(rows, declared, complete, note)`` for one head's commit statuses.

    A separate sequence from check runs, read because a required gate can be
    published as a commit status and appears on no check-runs page at all -- so a
    reading without them is short by exactly the rows most likely to be gating, and
    a read of only their first page is short in the same way with nothing saying so.
    """
    return _fetch_counted(transport, f"repos/{repo}/commits/{head}/status", "statuses", "statuses")


def fetch(message: object, *, budget_secs: float = _TICK_BUDGET_SECS) -> PrObservation:
    """One pull request's current facts. Never raises for a forge failure.

    A ``ValueError`` still escapes for a message that can never be valid, because
    that is a watch to remove rather than a tick to retry.
    """
    repo, pr, host = _parse_config(message)
    transport = _Transport(host, budget_secs=budget_secs)
    clock = time.time()

    def observation(status: str, incomplete: tuple[str, ...] = (), **fields: Any) -> PrObservation:
        return PrObservation(
            repo=repo,
            pr=pr,
            host=host,
            status=status,
            observed_at=clock,
            incomplete=incomplete,
            **fields,
        )

    data = _fetch_core(transport, repo, pr)
    if data is None:
        return observation(STATUS_UNAVAILABLE, ("pull request unread",))

    head = str(data.get("headRefOid") or "")
    state = str(data.get("state") or "").upper()
    merged_at = str(data.get("mergedAt") or "")
    draft = data.get("isDraft")
    remarks, remarks_total = _remarks(data, clock)
    common: dict[str, Any] = {
        "state": state,
        "draft": draft if isinstance(draft, bool) else None,
        "mergeability": str(data.get("mergeable") or "").upper(),
        "merge_state": str(data.get("mergeStateStatus") or "").upper(),
        "review_decision": str(data.get("reviewDecision") or "").upper(),
        "head": head,
        "merged_at": merged_at,
        "remarks": remarks,
        "remarks_total": remarks_total,
    }

    if merged_at or state in TERMINAL_STATES:
        # An ended pull request runs no more checks, so the board is not read: the
        # only consumer of this observation stops the watch on it. Reported as a
        # complete reading, because nothing is missing from it.
        return observation(STATUS_OK, **common)
    if not head:
        return observation(STATUS_PARTIAL, ("head revision unknown",), **common)

    incomplete: list[str] = []
    check_rows, declared, complete, note = _fetch_checks(transport, repo, head)
    if note:
        incomplete.append(note)
    workflows, workflows_whole = _resolve_workflows(transport, repo, check_rows)
    if not workflows_whole:
        # Without a workflow identity two same-named lanes cannot be told apart, so
        # the fold below keeps the conservative row and the reading says it is short
        # of what it needs rather than presenting a guess as whole.
        incomplete.append("workflow identity unresolved for some check runs")
    status_rows, status_declared, statuses_ok, status_note = _fetch_statuses(transport, repo, head)
    if status_note:
        incomplete.append(status_note)
    checks, folded = _collapse([*check_rows, *status_rows], workflows)
    return observation(
        STATUS_PARTIAL if incomplete else STATUS_OK,
        tuple(incomplete),
        checks=checks,
        checks_declared=declared + status_declared,
        checks_read=folded,
        checks_complete=complete and statuses_ok and workflows_whole,
        **common,
    )


class PrWatchProbe(Probe):
    """Fetches one pull request each tick and keeps the observation on itself.

    The kernel this plugs into is asked for exactly two things that a stateless
    fetcher cannot provide: an epoch, so its dedupe memory resets when the subject
    gets a new revision, and the consecutive-failure backstop, which is what turns
    a run of unreadable ticks into one report that the watch is blind. Neither is
    a judgment about whether this tick matters.

    So :meth:`observe` returns NO observations. The facts live on
    :attr:`observation`, which the driver reads after the kernel returns, and the
    decision lives with the driver and the judge.
    """

    repo: str
    pr: int
    host: str
    #: This tick's reading, or ``None`` before the first :meth:`observe`.
    observation: PrObservation | None = None

    def identity(self, ctx: object) -> tuple[str, str]:
        if not getattr(ctx, "in_process", False):
            # A script cron reaching this probe is driving a retired script. Refusing
            # is the point: a fetcher hands its reading to a judge that runs in the
            # gateway, so a watch driven from a subprocess would poll forever and
            # report nothing.
            #
            # The TYPE is load-bearing. ``irq.run`` converts a ``ValueError`` from
            # this method into ``Done``, which the scheduler answers by delivering the
            # message and deleting the job -- and the job record is the only durable
            # trace that a watch was ever armed, so deleting it destroys the evidence
            # along with the watch. Any other exception propagates instead, and the
            # scheduler counts it: the message lands in ``last_error`` and the job is
            # auto-paused once the consecutive-failure threshold is reached. It stays
            # listed, paused, saying what to re-arm with. A malformed cron message
            # keeps raising ``ValueError`` below, because there the watch is
            # unrecoverable rather than merely driven the wrong way.
            raise RuntimeError(
                "the gh-pr script driver is retired; re-arm this watch with "
                "monitor_start from the session that owns it"
            )
        self.repo, self.pr, self.host = _parse_config(getattr(ctx, "message", ""))
        self.observation = None
        return ("gh-pr", f"{self.repo}#{self.pr}")

    def observe(self, ctx: object) -> Tick:
        """Fetch, publish the observation, and report only what the kernel needs."""
        result = fetch(json.dumps({"repo": self.repo, "pr": self.pr, "host": self.host}))
        self.observation = result
        if not result.reached:
            return Tick(fetch_ok=False, detail="; ".join(result.incomplete))
        pending = len(result.bucket("pending"))
        detail = (
            f"{result.status}: {len(result.bucket('failing'))} failing, {pending} pending, "
            f"{len(result.remarks)} remark(s), head {result.head[:9]}"
        )
        return Tick(epoch=result.head, observations=[], pending=pending, detail=detail)


if __name__ == "__main__":  # pragma: no cover -- not an entry point
    print("gh_pr is a Kiro Crew probe module, not a script.")
    sys.exit(2)
