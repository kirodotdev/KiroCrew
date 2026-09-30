"""What a cron run's tool-gate outcomes and its result add up to.

The per-run tally of approved, refused and unresolved tool calls and the refusal
summary it names, the banner put in front of a partially blocked result, and the
volatile-stripped hash and reminder windows that result dedup compares against.

Recording the verdict on the job (``_apply_gate_verdict``) stays in the facade,
beside the shared-death streak it clears: the runtime-death audit reads it there.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        _bare_tool_name,
        display_safe,
        hashlib,
        time,
    )


# Volatile patterns stripped before hashing cron results for dedup.
_VOLATILE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO timestamps
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",  # UUIDs
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\b\d{10,13}\b")
_EPOCH_WINDOW_SECS = 300  # strip epoch values within ±5 min of now
_SUCCESS_REMINDER_SECS = 86400  # post "still succeeding w/ same result" reminder every 24h
_FAILURE_REMINDER_SECS = 3600  # re-alert still-failing cron every 1h (louder than success dedup)
# Cap on the failure reason carried into a user-facing alert. Matches the cap
# _apply_gate_verdict already puts on job.last_error, so the bell and the cron
# row cannot disagree about how much of a long traceback the user is shown.
_CRON_FAILURE_DETAIL_CAP = 500


_NO_RESPONSE = "_No response._"


class _GateTally:
    """Tool-gate outcomes accumulated over one cron run.

    A cron whose every tool call was refused still gets prose back from the
    model, so the reply text alone cannot separate "did the work" from "was
    blocked at every step". Counting both arms is what makes that verdict
    available once the turn ends. A multi-agent run tallies across the whole
    sequence, because status and history are per-run, not per-agent.

    Only an unconditional security block counts as a refusal here. A governance
    denial and an unattended-approval timeout also arrive unapproved, but they
    describe the policy state or an absent approver rather than a defect in the
    job — and a job's failure counter drives auto-pause, which is durable.

    A refusal is reported whether or not other calls got through. The prose
    a run returns is the model's account of what it did, and a model whose
    final write was refused still reports the write as done: the refused
    call's work simply never happened, and the only record of it was the SEL
    audit row. ``all_blocked`` decides the FAILURE budget; ``partially_blocked``
    decides whether the run's status and its delivery name the refusal.
    """

    def __init__(self) -> None:
        self.refused: list[str] = []
        self.approved = 0
        self.unresolved = 0
        # An approved call whose bare tool name is ``send_message``. Titles
        # arrive as ``Running: @server/send_message`` (kiro-cli) or
        # ``mcp__server__send_message`` (ACP); ``_bare_tool_name`` strips
        # either wrapper so the comparison is on the name alone.
        self.delivered = False

    def note(self, title: str, approved: bool, security_blocked: bool) -> None:
        if approved:
            self.approved += 1
            if _bare_tool_name(title) == "send_message":
                self.delivered = True
        elif security_blocked:
            self.refused.append(title)
        else:
            self.unresolved += 1

    def empty_reply_placeholder(self) -> str:
        """Row text for a turn that returned no prose.

        A silent cron is told to reply with nothing and deliver through
        ``send_message``, so an empty reply is its normal shape -- but it is
        also the shape of a turn that died before its first tool call. The
        tally tells them apart: approved tool calls mean work happened, and a
        ``send_message`` among them means a delivery was attempted. Attempted,
        not confirmed: ``on_tool_gate`` fires at the permission decision and
        never sees the tool's result, so the text does not claim the message
        arrived.
        """
        if self.delivered:
            return (
                f"_Silent run completed -- delivery attempted via send_message"
                f" ({self.approved} tool call{'s' if self.approved != 1 else ''} ran)._"
            )
        if self.approved:
            return (
                f"_Completed with no reply text -- {self.approved} tool call"
                f"{'s' if self.approved != 1 else ''} ran._"
            )
        return _NO_RESPONSE

    @property
    def all_blocked(self) -> bool:
        """Every tool the turn attempted was security-blocked, and none ran.

        ``unresolved`` must be zero, not merely uncounted: a governance denial
        or an approval timeout alongside a security block leaves the run's real
        capability unknown — that tool might have succeeded with a looser policy
        or a present approver — so the run does not evidence a job that cannot
        work. Treating it as evidence would auto-pause a healthy job.
        """
        return bool(self.refused) and self.approved == 0 and self.unresolved == 0

    @property
    def partially_blocked(self) -> bool:
        """At least one tool was security-blocked while the run was not a
        total block: another call ran, or another refusal left the run's
        capability unknown. The refused call's work did not happen either
        way, so the run is reported, but it is not evidence of a job that
        cannot work and spends nothing from the failure budget."""
        return bool(self.refused) and not self.all_blocked

    def refusal_summary(self) -> str:
        """One redacted, capped line naming what the gate refused.

        Shared by the job's ``last_error`` and the banner on the delivered
        result, so the cron row and the notification cannot disagree about
        which call was lost. At most three titles are named so a run that
        tripped the gate many times still reads as one line.

        Titles are LLM-authored, and the banner lands in the result BODY,
        which the Slack leg posts as parsed mrkdwn without a mention defang
        (:func:`render_for_slack` redacts; it does not touch ``<!channel>``).
        So the line goes through :func:`display_safe`, the shared outbound
        display sink: display-form credential redaction plus the zero-width
        break in ``<!`` and ``@`` that stops a refused call titled
        ``<!channel>`` from paging a whole channel the moment the run reports
        it. The break is invisible on the dashboard cron row, so one spelling
        serves both surfaces.
        """
        named = ", ".join(t or "<untitled tool>" for t in self.refused[:3])
        if self.all_blocked:
            head = f"all {len(self.refused)} tool call(s) blocked by the security gate: "
        else:
            total = self.approved + self.unresolved + len(self.refused)
            head = f"{len(self.refused)} of {total} tool call(s) blocked by the security gate: "
        return display_safe(head + named)[:_CRON_FAILURE_DETAIL_CAP]


def _annotate_partial_block(result_text: str, tally: _GateTally) -> str:
    """Prefix a partially-blocked run's result with the refusal it carries.

    The result is what every delivery leg (dashboard bell and slot, channel,
    Slack) shows and what the dedup hash is taken over, so one prefix here is
    what puts the refused call in front of the user beside the prose that may
    claim the work was done. A fully blocked run is not annotated: its verdict
    is a failure alert in its own right, and a tool-free or clean run has
    nothing to name.
    """
    if not tally.partially_blocked:
        return result_text
    return f"⛔ {tally.refusal_summary()} — that work did not happen.\n\n{result_text}"


def _result_hash(text: str) -> str:
    """Normalize volatile data and return a 16-hex-char SHA-256 prefix.

    Strips ISO timestamps, UUIDs, and any 10–13 digit number that looks
    like an epoch timestamp (within ±5 minutes of now).  Non-epoch numeric
    IDs (account IDs, build IDs) are likely preserved because they would
    likely fall outside the time window.

    Truncated to 64 bits — sufficient for 1:1 comparison against a single
    previous hash (collision probability ~1/2^64 per comparison).
    """
    now = time.time()
    lo = now - _EPOCH_WINDOW_SECS
    hi = now + _EPOCH_WINDOW_SECS

    def _strip_epoch(m: re.Match) -> str:
        v = int(m.group())
        # 13 digits → millis, convert to seconds for comparison
        ts = v / 1000 if v > 9_999_999_999 else v
        return "" if lo <= ts <= hi else m.group()

    text = _VOLATILE_RE.sub("", text)
    text = _EPOCH_RE.sub(_strip_epoch, text)
    return hashlib.sha256(text.encode()).hexdigest()[:16]
