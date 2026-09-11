"""One semantic core for the chat session a cron job delivers into.

Two surfaces name a job's target session: ``POST /api/crons`` records one when
the job is created, and ``kirocrew cron adopt`` repoints an existing job. They
apply different POLICY on purpose -- the REST route accepts only a live
dashboard slot, while adopt hands a job to any namespace an operator can name --
but policy is the only thing they may disagree about. What a target string
*means* has to be one answer: how a bare name is namespaced, and why a given
target cannot receive delivery.

Those had drifted into two copies with different behaviour, which is the failure
this module exists to make unrepresentable: the namespacing rule was spelled out
twice, and because a dashboard conversation's transcript is filed under the
NAMESPACED key, the CLI's "is this a real session?" probe -- which addressed it
by bare slot name -- reported no such session for sessions that were
demonstrably there. A caller that asks this module instead of re-deriving the
rule cannot drift from the other caller, and a rule that changes here changes
for both.

Deliberately free of both aiohttp and :class:`~kiro_crew.cron.CronService`: the
gateway handler, the CLI and the delivery path all import it, so anything it
touched would become a dependency of all three. Refusals are returned as data
(:class:`TargetProblem`) and each caller renders them in its own idiom -- an
HTTP 400 with a stable ``code``, or a line on stderr.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The one namespace whose targets the delivery path can resolve to a chat tab.
#: Ownership reaches further than delivery does -- a ``slack:`` key can own and
#: manage a job -- so this prefix is about where results can LAND, not about who
#: may manage the job.
DASHBOARD_PREFIX = "dashboard:"


@dataclass(frozen=True)
class TargetProblem:
    """A reason a target session cannot receive a job's results.

    ``code`` is the stable machine-readable reason shared by every surface, so
    the REST route's error body and the CLI's diagnosis cannot disagree about
    what went wrong. ``message`` is a complete sentence for a human, and never
    interpolates un-redacted caller text: a caller with a redaction context
    passes the already-safe spelling in as ``display``.
    """

    code: str
    message: str


def namespace_target(raw: str) -> str:
    """The canonical session key for the target *raw* names.

    A bare name gains the :data:`DASHBOARD_PREFIX` that every delivery consumer
    strips back off, so adding it here is their exact inverse and needs no
    lookup. Anything already carrying a namespace passes through untouched --
    including a non-dashboard one, because deciding whether that namespace is
    ACCEPTABLE is the caller's policy, not this function's.

    A name with no namespace at all could never equal any caller's session key,
    so it would only ever produce a job nobody can own; that is why the bare
    form is namespaced rather than stored as given.
    """
    target = raw.strip()
    if not target:
        return ""
    if ":" in target:
        return target
    return f"{DASHBOARD_PREFIX}{target}"


def is_dashboard_target(key: str) -> bool:
    """True when *key* names a dashboard chat session.

    The delivery path resolves only these to a tab, so this is the predicate
    that separates "results land in a conversation" from "this key can own the
    job but its output has nowhere to go".
    """
    return key.startswith(DASHBOARD_PREFIX)


def dashboard_slot_of(key: str) -> str:
    """The slot name inside a dashboard target *key*.

    The inverse of :func:`namespace_target` for the dashboard case, and the
    spelling the live-state lookups take.
    """
    return key.removeprefix(DASHBOARD_PREFIX)


def check_dashboard_target(
    key: str,
    *,
    max_len: int,
    slot_exists: bool,
    linked_session_key: str,
    display: str,
) -> TargetProblem | None:
    """Diagnose a dashboard target, or return None when it can receive delivery.

    Ordered so a cheap value check cannot be reached past a state lookup, and so
    each refusal names the narrowest true reason.

    The length cap applies to the NAMESPACED key, because that is what the store
    persists and bounds. Validating the bare input alone leaves the namespace's
    own characters unbudgeted, so a slot name within a prefix-length of the cap
    overflows it downstream -- and there the overflow surfaces as an uncaught
    error rather than a refusal.

    ``linked_session_key`` carries the conversation the tab is DISPLAYING, which
    is not always the tab's own key: a channel-born chat keeps the channel's
    key, and a cron tab carries ``cron:<job id>``. For those, this target is not
    the conversation's key, so the chat could receive a delivery it has no way
    to list or cancel. Refused rather than stored under either key, since only a
    dashboard key resolves for delivery and only the linked key owns.
    """
    if len(key) > max_len:
        return TargetProblem(
            code="invalid_session_key",
            message=f"session_key too long once namespaced (max {max_len})",
        )
    slot = dashboard_slot_of(key)
    if not slot or not slot_exists:
        return TargetProblem(
            code="unknown_session",
            message=f"no such chat session: {display!r}",
        )
    if linked_session_key and linked_session_key != key:
        return TargetProblem(
            code="linked_session_not_targetable",
            message=(
                "that chat session belongs to another conversation; use "
                "`kirocrew cron adopt` to target it"
            ),
        )
    return None


def unsupported_namespace_problem() -> TargetProblem:
    """The refusal for a target whose namespace delivery cannot reach.

    Its own function rather than a branch in :func:`check_dashboard_target`,
    because the two callers reach this conclusion from opposite directions: the
    REST route refuses the target outright, while adopt accepts it and instead
    declines to PROMISE delivery. One wording, two policies.
    """
    return TargetProblem(
        code="unsupported_session_namespace",
        message=(
            "session_key must name a dashboard chat session; use "
            "`kirocrew cron adopt` for other session namespaces"
        ),
    )
