"""Audit SDK — the app-facing half of the security event log.

An app that acts on the user's behalf against something outside the machine makes
decisions worth reconstructing later: it refused a write because the remote said
no, it published a document, it declined a path as sensitive. Those belong in the
same append-only SEL log the gateway's own decisions land in, or "who changed what,
and what was refused" is answerable for the gateway and unanswerable for the app
half of the same operation.

Before this seam the only route was ``from kiro_crew.sel import sel`` — a
built-in-only surface, so an installed app pinned to it breaks silently when the
wheel moves. Unlike the redaction seam there was no copy-it alternative: an app
cannot write this log itself, so the absence was not a drifting control but a
missing one.

**Attribution is SDK-minted, never a parameter.** ``caller_identity`` is derived
from the app name the context was built with, so there is no ``caller=`` argument
to pass the wrong value into — the common way attribution goes wrong is a caller
that copies the wrong constant, and that is closed. This mirrors ``job_sdk``'s
checkpoint rule: a fact the SDK owns needs no sanitizing.

It is **cooperative, not unforgeable**, and the distinction matters for anyone
reading a row as evidence. Hook code runs inside the gateway process with full
filesystem access, so an app can construct ``AuditSDK("other-app")`` directly, or
reach ``sel()`` itself, and write whatever it likes. What this seam removes is the
accidental misattribution; a deliberate one is already outside what an in-process
API can prevent, and the boundary that would prevent it is process isolation.

**Best-effort.** ``record`` swallows a write failure: an audit sink that is full or
unwritable must not take down the operation it only describes, and an app that let
it raise would fail a user's publish because logging failed. A fail-closed variant
is deliberately absent until an app needs one — ``sel().log_api_access(critical=True)``
remains the route for the day a consent grant or safety override has to refuse an
action it could not log.

**``outcome`` is not narrowed to a vocabulary.** The gateway's own writers use
``denied``, ``allowed``, ``ok``, ``completed``, ``rejected`` and more, and nothing
filters the log by outcome, so constraining an app to a subset would make its rows
less precise than its siblings' — and rewriting an unrecognised value would
mislabel the very fact being recorded.

It IS redacted and clipped — but in ``log_api_access``, not here. That is a
deliberate placement: this seam is what turns ``outcome`` from an in-tree constant
into caller text, and the fix belongs at the boundary every filler crosses rather
than in one SDK that happens to notice. Redaction is the identity function on every
real outcome spelling, so no existing row changes, and the log is append-only and
served over ``/api/sel/events``, so a secret landing there has no recovery path.
Narrowing to a vocabulary would have been a different and worse move; scrubbing is
not.
"""

from __future__ import annotations

import logging

from ..sel import sel

logger = logging.getLogger(__name__)


class AuditSDK:
    """App-scoped writes to the security event log.

    Reached as ``ctx.audit``. Requires no permission: an app cannot use it to
    obtain anything, only to state what it did, and an app unable to audit would
    simply not audit — leaving the gap this seam closes.
    """

    def __init__(self, app_name: str) -> None:
        self._app_name = app_name
        #: `app:<name>` matches how `cron_sdk` tags ownership, so one convention
        #: identifies app-originated rows across cron jobs and audit events.
        self._caller = f"app:{app_name}"

    def record(
        self,
        operation: str,
        outcome: str,
        *,
        resources: str = "",
        error: str = "",
    ) -> None:
        """Append one event. Never raises.

        ``operation`` is the app's own verb (``"publish"``, ``"pull"``); it is
        prefixed with the app name so two apps cannot collide on a bare
        ``"publish"``. Use this for the ordinary case — every permission decision
        and every write worth reconstructing.

        ``outcome`` keeps whatever word the app chose (see the module docstring).
        Every field an app fills here is redacted downstream by `log_api_access`,
        so this method adds no scrubbing of its own.
        """
        try:
            sel().log_api_access(
                caller=self._caller,
                operation=f"{self._app_name}.{operation}",
                outcome=outcome,
                source="app",
                resources=resources,
                error=error,
            )
        except Exception:
            # Best-effort: auditing must not break the operation it describes.
            #
            # `operation` is deliberately NOT interpolated. It is app-supplied text,
            # and on this branch the SEL write is what just failed -- so the value
            # never went through the scrubbing `log_api_access` applies, while this
            # line does reach `gateway.log`. A credential an app put in its verb
            # would persist there, in a file the SEL redaction never covers. The app
            # name is a manifest identifier and is safe to name; the app knows which
            # of its own calls failed from the traceback.
            logger.debug("app %s: audit emit failed", self._app_name, exc_info=True)
