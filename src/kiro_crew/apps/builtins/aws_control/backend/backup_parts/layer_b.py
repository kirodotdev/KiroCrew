"""The operator's Layer B grant for the sessions archive, its scope, and its audit.

Layer B is the unredacted model context: the kiro-cli half of the sessions archive
and the terminal conversation export. The grant, the scope marker recorded with it,
and the SEL events for both the write and each run's decision live here. The reads
fail closed: a value this code does not understand never widens what leaves.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import (
    _account_state,
    _account_view,
    _locked_state_update,
)
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE_MODULE)


#: Per-account key in the state document holding the operator's Layer B decision
#: for the sessions archive. Named here rather than spelled inline because the
#: reader, the writer and the test that pins the default all have to agree on it,
#: and a typo in any one of them would read as "not permitted" -- a silent OFF is
#: the failure this constant exists to make impossible.
SESSIONS_LAYER_B_KEY = "sessionsIncludeLayerB"


#: Per-account key recording WHICH SCOPE the operator's Layer B grant was made
#: under. The grant itself is one boolean and stays one boolean -- this is not a
#: second toggle and gives the operator nothing new to set. It exists because the
#: grant's meaning widened: a grant recorded before the terminal conversation
#: export was disclosed authorized this product's own ``cli`` session files, and
#: reading it as also authorizing the terminal's chat tables would ship host-wide
#: terminal context on a consent that never mentioned it, off-host and
#: unrecallable. Written by :func:`set_sessions_layer_b` only when the caller NAMES
#: this scope in the request: a bare enable carries no evidence of what the operator
#: was shown, so an idempotent retry, an automation, and a client rendering older copy
#: are indistinguishable from a deliberate re-consent, and none of them may widen what
#: leaves the machine.
SESSIONS_LAYER_B_SCOPE_KEY = "sessionsLayerBScope"


#: The one scope value that covers the conversation export. Matched EXACTLY: an
#: absent marker, a different string, or a non-string all read as cli-only. That is
#: the fail-closed direction, and it is the direction a stored value this code does
#: not understand must take -- widening on an unrecognised marker is how a consent
#: boundary stops holding.
SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS = "cli+conversations"


#: The operator's standing permission for the sessions archive to carry Layer B --
#: the byte-exact, unredacted kiro-cli context window (``<sid>.json`` +
#: ``<sid>.jsonl`` under :func:`kiro_sessions_dir`), and the table-scoped export
#: of the terminal's own conversation store (the ``conversations/`` root, see
#: :func:`_export_cli_conversations`). Both are what a model actually held,
#: unredacted, which is the property this permission prices, so one permission
#: covers both and neither has a second path around it. Default OFF, so an archive
#: carries the crew transcript half only unless the operator has chosen otherwise.
#:
#: Why a gate here at all. Layer B is strictly more sensitive than the transcript
#: it accompanies: the transcript is what was DISPLAYED, with display-time
#: redaction applied, while Layer B is what the model actually held, unredacted.
#: It also cannot be redacted on the way out -- the thinking blocks inside it
#: carry a provider signature over their own content, so rewriting one invalidates
#: the conversation -- which leaves exactly two choices, byte-exact or absent.
#: ``dashboard.export_include_layer_b`` puts the same choice in the operator's
#: hands for the file-export path, but this permission is deliberately NOT a
#: ``config.json`` key like that one.
#:
#: WHY NOT ``config.json``. That file is writable by any auto-approved agent
#: shell, so a permission stored there is one a prompt-injected agent can grant
#: itself: edit the key, wait for the owner to run a sessions backup, and the
#: unredacted context uploads with no consent -- an outcome nothing can recall,
#: because an object already in a bucket cannot be un-sent. An authorization
#: whose subject can write it is not an authorization. The repo's own
#: ``CredentialPolicy.exempt_exact_hosts`` docstring states the rule: such a
#: value is "NEVER sourced from ``config.json``". So this one lives in the app's
#: state document, ``backup.json``, which sits inside the already-fenced
#: :data:`STATE_DIR_LEAF` directory on the read+write keystone floor
#: (``security._CREW_SECRET_LEAVES``) -- the same placement, and for the same
#: reason, as the ``nightly`` bit beside it, which authorizes unattended PAID
#: uploads. An agent can write no path in that directory, and the only writer is
#: the owner-gated ``POST /backup/{account}/layer-b`` handler, which opens the
#: file directly rather than through the agent tool gate, so the operator's
#: toggle still works.
#:
#: PER ACCOUNT, like ``nightly`` and unlike the export key, because the risk this
#: permission prices is the destination: the archive lands in one account's
#: bucket, so granting it for that bucket must not grant it for another the
#: operator adds later.
#:
#: Default OFF rather than ON, even though the destination is the operator's own
#: bucket, because the bucket is not provably a single operator's: this app
#: supports several installs writing one drive and says so
#: (:data:`ORIGIN_UNVERIFIED` exists because "anyone who can write to the bucket
#: can write to a name"), so a co-writer can reach an archive here. Being wrong
#: in the OFF direction costs a restore its full-fidelity resume until the
#: operator flips one toggle, and the run record states that it happened. Being
#: wrong in the ON direction puts unredacted context somewhere it cannot be
#: recalled from. Only one of those is recoverable.
#:
#: An unreadable state file or a non-boolean value reads as OFF, for the reason
#: :func:`nightly_enabled` gives for the same posture: a document this function
#: cannot understand must not widen what leaves the machine, and a backup that
#: still runs without Layer B is better than one that fails.
def sessions_layer_b_enabled(account: str) -> bool:
    """Whether the operator has enabled Layer B for *account*'s sessions archive.

    Default False; enable through the owner-gated
    ``POST /api/apps/aws-control/backup/{account}/layer-b``.

    **What this permission covers, stated here because it is the grant's own
    description.** Two payloads ride on it, and they differ in REACH rather than in
    sensitivity class. The ``cli`` half is this product's own kiro-cli session files.
    The ``conversations/`` export is the terminal's own chat tables from its state
    store, which records every interactive kiro-cli use on the host -- including work
    that has nothing to do with this product's sessions. An operator reading only
    "unredacted context in the sessions archive" would price the first and receive
    both, so the second is named.

    One permission for both is the recorded decision, not an omission: the gate is
    priced by the payload's sensitivity CLASS, and both are the byte-exact model
    context window. The grant's SCOPE is what distinguishes them, and it is recorded
    on the grant itself -- see :data:`SESSIONS_LAYER_B_SCOPE_KEY` and
    :func:`layer_b_grant_covers_conversations`. A grant recorded before the
    conversation export was disclosed covers the ``cli`` half only.
    """
    raw = _account_view(account).get(SESSIONS_LAYER_B_KEY, False)
    return raw if isinstance(raw, bool) else False


def layer_b_grant_covers_conversations(account: str) -> bool:
    """Whether *account*'s Layer B grant was recorded with the conversation export in scope.

    Both conditions are required, read from ONE view of the state document so the two
    halves cannot come from different moments: the grant is on, and it carries
    :data:`SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS`.

    **A grant with no scope marker reads as ``cli``-only, always.** That is the whole
    point of the marker rather than an edge case in it: such a grant was recorded when
    the permission's own description covered this product's session files, so reading
    it as covering the terminal's chat tables would ship every interactive kiro-cli use on
    the host off-host on a consent that never named them, and an object already in a
    bucket cannot be recalled. An operator re-confirming through the existing
    owner-gated endpoint gets the wider scope; nothing new is added for them to set.

    Anything unrecognised -- a different string, a non-string, a missing key -- is
    ``cli``-only for the same reason :func:`sessions_layer_b_enabled` reads an
    unparseable value as OFF: a document this code cannot understand must not widen
    what leaves the machine.
    """
    view = _account_view(account)
    if view.get(SESSIONS_LAYER_B_KEY, False) is not True:
        return False
    return view.get(SESSIONS_LAYER_B_SCOPE_KEY) == SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS


def set_sessions_layer_b(account: str, enabled: bool, *, scope: str | None = None) -> None:
    """Record the operator's Layer B decision for *account*.

    Raises ``OSError`` when the existing state could not be read, exactly as
    :func:`set_nightly` does: a permission the caller believes it stored and the
    next read contradicts is worse than a loud failure.

    **The wider scope is stamped only when the CALLER ASKS FOR IT BY NAME**, through
    *scope*. An enable whose *scope* is ABSENT records the grant and keeps the stored
    marker ONLY while the grant was already in force -- nothing about it changed, so
    neither widening nor narrowing was requested. An enable that turns the grant ON
    clears the marker instead: a marker describes the grant that was in force when it
    was written, so a grant being re-established cannot inherit it. The document can
    hold ``enabled=false`` together with a marker, so that pairing must not become a
    host-wide grant on a bare ``{"enabled": true}``. An enable naming a scope this code
    does not recognise is a different request and CLEARS the marker: the caller said
    what they wanted and it was not the conversation export, so an already-wide grant
    must not stay wide for them.

    The request shape is what makes this necessary: the route accepts a bare
    ``{"enabled": true}``, which carries no evidence of what the operator was shown, so
    an idempotent retry, an automation, and a client still rendering older copy all look
    identical to a deliberate re-consent. Deriving consent from the act of enabling would
    let any of those widen what leaves the machine, and the archive that follows cannot
    be recalled.

    A transition test -- stamp only when the grant goes from off to on -- closes the
    retry but NOT a first enable from a stale client, where the operator reads older
    copy and the grant silently covers the whole host. Requiring the caller to name the
    scope closes both, because it is the only form in which the request itself carries
    the decision.

    A disable removes the marker with the grant, so a later enable cannot inherit a
    scope from a decision that was withdrawn.

    Both directions file a SEL event carrying what was decided -- see
    :func:`_audit_layer_b_grant`. A widening that only the state file records is a
    consent decision an incident review cannot read without that file, and a narrowing
    is equally part of the consent history.
    """
    resulting = {"scope": ""}

    def mutate(state: dict[str, Any]) -> None:
        entry = _account_state(state, account)
        # Read BEFORE the assignment below overwrites it: whether the grant was already
        # in force is what decides if an unscoped enable may keep the stored marker.
        # Compared with ``is True`` to match the reader, so a corrupted stored value
        # counts as OFF and enabling over it is a transition that clears the marker.
        was_enabled = entry.get(SESSIONS_LAYER_B_KEY) is True
        entry[SESSIONS_LAYER_B_KEY] = bool(enabled)
        if not enabled:
            entry.pop(SESSIONS_LAYER_B_SCOPE_KEY, None)
        elif scope is None:
            # The field was ABSENT, which is no statement about scope -- so the marker
            # may be KEPT, but only while nothing about the grant changed. A marker
            # describes the grant that was in force when it was written, so an enable
            # that RE-ESTABLISHES the grant cannot inherit it: the stored value belongs
            # to a decision other than the one this call puts in force. An off-to-on
            # transition therefore clears it, and only an already-on grant preserves it.
            if not was_enabled:
                entry.pop(SESSIONS_LAYER_B_SCOPE_KEY, None)
        elif scope == SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS:
            entry[SESSIONS_LAYER_B_SCOPE_KEY] = SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        else:
            # The caller NAMED a scope and it is not this one, so they did not ask for
            # the conversation export. Absent and unrecognised are different requests
            # and must not collapse: leaving the marker here would keep an already-wide
            # grant wide for a caller that asked for something else entirely, which is
            # the widening-without-a-request this field exists to stop.
            entry.pop(SESSIONS_LAYER_B_SCOPE_KEY, None)
        stored = entry.get(SESSIONS_LAYER_B_SCOPE_KEY, "")
        resulting["scope"] = stored if isinstance(stored, str) else ""

    _locked_state_update(mutate)
    _audit_layer_b_grant(account, bool(enabled), resulting["scope"])


def _audit_layer_b_grant(account: str, enabled: bool, scope: str) -> None:
    """Record the operator's grant WRITE, with its direction and resulting scope.

    :func:`_audit_layer_b_decision` records the decision a BACKUP RUN observed. This
    records the moment the operator made it, which is a different event and the one a
    consent question actually asks about: who widened this grant, and when.

    The route that calls this is already audited as an API access, but that event
    carries the operation and the path and not which way the decision went, so learning
    what the grant became means reading the state file -- the on-disk dependency the
    decision audit exists to remove. Both facts are therefore in ``resources``.

    A NARROWING is filed too, on the same footing. A withdrawal is as much part of the
    consent history as a grant: a review reconstructing what an archive was allowed to
    carry on a given night needs the revocation as well as the grant, and filing only
    widenings would leave the log reading as though a permission that was withdrawn is
    still in force.

    ``dashboard-owner`` matches the attribution the owner-gated route uses for its own
    events, and that route is this permission's only writer.

    ``successful`` for both directions, and best-effort like every audit in the engine:
    a failed record must never be what stops an operator's decision from persisting,
    which is why this runs after the state write rather than before it.
    """
    # The grant term is part of this, not just the marker. A withdrawn grant covers
    # nothing whatever a marker says, which is the same pair
    # `layer_b_grant_covers_conversations` reads -- and reading only the marker here made
    # the event truthful only because the disable branch happens to remove it. That is a
    # dependency on a decision made elsewhere in the function, and an event that reported
    # `conversations=allowed` for a withdrawal would misstate the one thing a consent
    # review comes to this event for.
    covered = enabled and scope == SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
    try:
        sel().log_api_access(
            caller="dashboard-owner",
            operation="aws_control.backup_layer_b_grant",
            outcome="successful",
            source="aws-control",
            resources=(
                f"account={account} grant={'granted' if enabled else 'withdrawn'} "
                f"conversations={'allowed' if covered else 'withheld'}"
            )[:200],
        )
    except Exception:
        logger.debug("aws-control Layer B grant audit failed", exc_info=True)


def _audit_layer_b_decision(
    account: str, layer_b: bool, *, conversations: bool, caller: str
) -> None:
    """Record which way the Layer B decision went, at the point it is made.

    The permission decides whether unredacted model context leaves the machine,
    so an incident review asking "was Layer B in the archive that went out on
    Tuesday" needs an answer that does not depend on the run record still being
    on disk. Every other access decision in the engine reaches the SEL through
    :func:`_refuse_upload`, but that helper only fires on a REFUSAL -- so the
    ALLOW direction, which is the one that ships the bytes, was the only decision
    here leaving no event at all.

    ``conversations`` is carried for that same reason and is not derivable from
    ``layer_b``. The grant's SCOPE is a second consent decision: a permitted run
    whose grant predates the conversation export ships the ``cli`` half and withholds
    the terminal conversations, and an event saying only ``layer_b=allowed`` describes
    that run identically to one that shipped both. The run record does carry
    ``layer_b_scope``, but this function exists precisely so a consent question has an
    answer that survives the run record being gone, so reading the scope from disk
    would put the audit back on the dependency it is here to remove.

    ``successful`` for both directions, because the decision itself succeeded
    either way; which way it went is in ``resources``. Filing a withhold as
    ``denied`` would put a configuration the operator chose in the same bucket as
    a refused upload and devalue every real denial in the log.

    Same event shape, caller threading and best-effort posture as
    :func:`_refuse_upload`: ``caller`` is passed in so an unattended nightly run
    is not recorded against the dashboard owner, and a failed audit must never be
    what stops a backup.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="aws_control.backup_layer_b_decision",
            outcome="successful",
            source="aws-control",
            resources=(
                f"account={account} layer_b={'allowed' if layer_b else 'withheld'} "
                f"conversations={'allowed' if conversations else 'withheld'}"
            )[:200],
        )
    except Exception:
        logger.debug("aws-control Layer B decision audit failed", exc_info=True)
