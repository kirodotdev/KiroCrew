"""Scrub SDK — the app-facing half of Kiro Crew's outbound content controls.

An app that sends content somewhere the user did not type it — an external
document store, a ticket, a wiki — needs the same credential and exfiltration
redaction the gateway applies on its own boundaries. Before this SDK the only
ways to get it were to import ``kiro_crew.security`` (a built-in-only surface: an
app pinned to it breaks silently for anyone who upgrades the wheel, because
built-ins move in lockstep with it and an installed app does not) or to copy the
patterns into the app. Both are worse than a seam, and the copy is worse than the
import: a redaction pattern set that drifts from the one the gateway enforces is a
security control that looks present and is not.

**It redacts through the ACTIVE policy, not the baseline.** The text is finished
with :func:`kiro_crew.platform.context.redact_via_context`, the canonical shim
every egress site routes through, so a loaded companion's extra token and cookie
regexes apply. Calling ``security.redact*`` alone here would be companion-blind:
on such a host a companion-only token would survive the seam and reach whatever
the app published, which is irreversible. Baseline redaction runs FIRST, only to
enumerate what it recognised (see :class:`ScrubResult`); the active policy then
runs over that result, so the text that leaves is never less redacted than the
gateway's own boundaries.

**Fail-closed, and an app must not swallow it.** ``redact_via_context`` re-raises
``PlatformCompositionError`` on a host that could not compose its companion, so
``outbound`` propagates it rather than quietly downgrading to the OSS baseline. An
app that catches it and publishes anyway defeats the control; the correct handling
is to abandon the publish.

**One method, deliberately.** Neither single pass is exposed on its own and no
inbound path check rides along: an app that wants half of a redaction wants
something the seam should not make easy, and every extra method here is public
surface on a published package that cannot be withdrawn.

**No permission gates it, deliberately.** Every other SDK on ``AppContext`` grants
a capability and is therefore ``None`` until the manifest asks for it. This one
only ever REMOVES data from a string the app already holds, so there is nothing to
withhold — and a gate would be actively harmful: an app refused the seam does not
stop sending the content, it ships its own regexes instead, which is the outcome
the seam exists to prevent. It is always populated, so an app can rely on it
without a ``None`` check that would otherwise become a silent no-redaction path.

**What it does not do.** It does not decide WHETHER to send, and it is not a
sanitizer for content coming IN. Redaction is lossy and one-way: the returned text
is what may leave. It also does not tell an app WHICH value was removed — see
:class:`ScrubResult` for why that is withheld rather than merely unimplemented.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..platform.context import redact_via_context as _redact_via_context
from ..security import redact_with_findings as _redact_with_findings


@dataclass(frozen=True)
class ScrubResult:
    """Text safe to send, plus COUNTS of what was removed to make it so.

    Counts rather than the underlying passes' warning strings, and that is a
    security decision rather than a simplification. Those strings are not
    uniformly payload-free: the credential pass reports shapes only
    (``"Redacted credential pattern (20 chars)"``), but the exfiltration pass
    embeds the offending domain in every branch and, on the long-query branch, the
    first 60 characters of the path and query — a real fragment of the URL it just
    removed, and exactly where a token that escaped the credential patterns would
    sit. Handing that to an app as something safe to log would move a secret out of
    the published text and into the app's logs, which is no better and often worse.
    A count cannot carry a payload however core later phrases a warning, so the
    property holds without depending on that phrasing staying benign.

    The two counts stay apart because a credential and an exfiltration URL are
    different disclosures to the user, with different remedies (re-enter the secret
    vs re-check the destination).

    ``redacted`` is set by the SDK rather than derived from ``text != original``,
    and it accounts for the active policy as well as the baseline: a companion that
    removes a token the baseline never recognised sets it while both counts stay
    zero. It never answers False when something was removed, which is the direction
    that matters.
    """

    text: str
    credentials_removed: int
    urls_removed: int
    redacted: bool


class ScrubSDK:
    """App-scoped access to the outbound redaction pass.

    Reached as ``ctx.scrub``. Stateless, side-effect free and app-agnostic: it
    carries no app identity because nothing it does is attributed or recorded, so
    one instance would serve every app equally.
    """

    def outbound(self, text: str) -> ScrubResult:
        """Redact *text* for sending off-machine, reporting what was removed.

        Call this before writing app content anywhere outside the machine. Raises
        ``PlatformCompositionError`` on a host whose companion could not compose —
        do not publish when it does.
        """
        # Baseline first, to count what it recognised; the active policy then
        # finishes the text so a companion's extra patterns are applied on top.
        # The other order would leave nothing for the baseline pass to report.
        # Only the LENGTHS cross the boundary -- the warning strings themselves
        # are dropped here, deliberately (see ScrubResult).
        stripped, credential_warnings, url_warnings = _redact_with_findings(text)
        final = _redact_via_context(stripped)
        return ScrubResult(
            text=final,
            credentials_removed=len(credential_warnings),
            urls_removed=len(url_warnings),
            redacted=bool(credential_warnings or url_warnings) or final != stripped,
        )
