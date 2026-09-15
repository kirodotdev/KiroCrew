"""The in-band deny notice: what the model is told when a tool call is refused
by the HOST rather than by the person.

A rejected permission reaches the model as kiro-cli's generic "User denied tool
execution", with no channel for the host to say more. This module builds the
correcting notice (:func:`build_refusal_steer_notice`) and steers it into the
running turn before the rejection goes back on the wire
(:func:`steer_refusal_notice`), which is what makes it race-free: while the
``session/request_permission`` is unanswered the turn is provably in flight.

A leaf on purpose. The dashboard chat runner, the native Slack handler and the
channel-neutral ``messaging.TurnDriver`` all steer the same notice, and the
messaging package may not import the dashboard (``test_messaging_import_purity``),
so the builder lives here and ``dashboard.state`` re-exports it. Imports only the
constants leaf, ``deny_guidance`` and ``security``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.constants import (
    DENY_CAUSE_APPROVAL_NO_BUDGET,
    DENY_CAUSE_APPROVAL_TIMEOUT,
    DENY_CAUSE_APPROVAL_UNDELIVERABLE,
    DENY_CAUSE_BATCH_CASCADE,
    DENY_CAUSE_HOOK_ERROR,
    DENY_CAUSE_INVALID_NAME,
    DENY_CAUSE_POLICY,
    STEER_NOTICE_BOUND_SECS,
)
from kiro_crew.deny_guidance import remediation_for
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: Why a tool call was denied, for the in-band notice's cause-specific wording.
#: The DENY_CAUSE_* names themselves live in ``kiro_crew.constants`` (imported
#: above and re-exported here) so the messaging core can name a cause without
#: importing this module. The notice's INVARIANT half — that this was not a user
#: action, the generic string it is correcting, and the instruction to decide
#: inside this turn — is identical for every cause; only the clause naming the
#: cause and the guidance about what to do next differ. Kept as data rather than
#: a near-copy of the notice per cause so the invariant half cannot drift between
#: them, which is the half doing the actual work of overwriting the model's wrong
#: conclusion.

#: cause → (clause completing "The tool call you just made …", what to do next).
_DENY_CAUSE_TEXT: dict[str, tuple[str, str]] = {
    DENY_CAUSE_POLICY: (
        "was blocked by a Kiro Crew safety policy",
        "use an allowed alternative (for a shell command, a read-only variant), use "
        "a different tool, or — if the block is correct and you genuinely cannot "
        "proceed — say so and stop with the reason.",
    ),
    DENY_CAUSE_INVALID_NAME: (
        "was refused because its tool name failed validation",
        "reissue the call with a name that passes validation. The action itself was "
        "never judged, so do not abandon it or look for a different approach on this "
        "evidence — and do not repeat the same malformed name.",
    ),
    DENY_CAUSE_HOOK_ERROR: (
        "could not be authorized because a PreToolUse hook raised while deciding it",
        "treat this as a host fault, not a verdict on the action: nothing judged the "
        "call itself. Retrying the identical call is reasonable once; if it faults "
        "again, say what happened rather than working around it silently.",
    ),
    DENY_CAUSE_BATCH_CASCADE: (
        "was auto-declined along with every remaining call in its batch, because "
        "the host declined an earlier tool of the same batch",
        "nothing judged these calls themselves — the group was cut short as a "
        "whole. Address what declined that earlier tool (the reason above), then "
        "re-issue the calls you still need; if you genuinely cannot proceed "
        "without them, say so and stop with the reason.",
    ),
    DENY_CAUSE_APPROVAL_TIMEOUT: (
        "was auto-declined because its approval prompt expired unanswered",
        "nobody answered within the window, so the action itself was never judged — "
        "do not abandon it or route around it on this evidence. State the "
        "permission you need and why, then continue with what you can do without "
        "it. Do not immediately reissue the same call: the person who did not "
        "answer is still away, and re-prompting re-arms the same wait for the "
        "same silence.",
    ),
    DENY_CAUSE_APPROVAL_NO_BUDGET: (
        "was auto-declined because the turn had no budget left to host its approval prompt",
        "the prompt was never shown, so the action itself was never judged — do "
        "not abandon it or route around it on this evidence. State the "
        "permission you need and why, then continue with what you can do "
        "without it. Do not immediately reissue the same call: this turn cannot "
        "host an approval wait, so the identical call would be declined the "
        "same way.",
    ),
    DENY_CAUSE_APPROVAL_UNDELIVERABLE: (
        "was auto-declined because its approval prompt could not be delivered "
        "to the operator's channel",
        "delivery failed, so the action itself was never judged — do not "
        "abandon it or route around it on this evidence. State the permission "
        "you need and why, then continue with what you can do without it.",
    ),
}


def build_refusal_steer_notice(
    title: str,
    reason: str,
    *,
    cause: str = DENY_CAUSE_POLICY,
    credential_tool_hint: str = "",
) -> str:
    """Body of the in-band deny notice steered into the RUNNING turn.

    Sent BEFORE the permission rejection goes back on the wire, which is what
    makes it race-free: while the ``session/request_permission`` is still
    unanswered the turn is provably in flight, so the steer is queued rather than
    dropped, and kiro-cli folds it in at the next model-inference boundary — the
    one immediately after the rejected tool resolves. The model therefore learns
    why inside the SAME turn and no recovery continuation is needed.

    The notice must correct an attribution the model has already been handed:
    a rejected permission is reported to the model as a generic tool failure with
    no channel for the host to say more (ACP's permission response carries only
    ``outcome``/``optionId``). Naming kiro-cli's exact wording — measured against
    kiro-cli 2.19.1 — is what lets the model overwrite the wrong conclusion rather
    than hold both, and attributing the quote to that backend keeps the sentence
    true on another steer-capable harness whose wording has not been measured.
    ``title``/``reason`` must already be redacted by the caller.

    *cause* selects the wording. The distinction is not cosmetic: a policy block
    is a verdict the model must route around, an invalid tool name is the model's
    own malformed output and is the one case it can simply fix, a hook fault
    judged nothing at all, a batch cascade cut the group short without judging
    its members, and an expired approval prompt means nobody answered. Telling
    the model "safety policy" for any non-policy cause would send it looking for
    an allowed alternative to an action nobody refused.
    An unknown cause degrades to the policy wording rather than raising: a wrong
    noun is recoverable, and losing the notice would hand the model back
    kiro-cli's "user denied" with nothing to correct it.

    Returns "" when there is nothing to say, so a caller can treat the empty
    string as "no notice was sent" and fall back to the recovery continuation.
    """
    if not (title or "").strip() and not (reason or "").strip():
        return ""
    clause, guidance = _DENY_CAUSE_TEXT.get(cause, _DENY_CAUSE_TEXT[DENY_CAUSE_POLICY])
    what = f"{title}: {reason}" if reason else title
    # Class-specific remediation, for the policy cause only. The non-policy
    # causes judged nothing about the action — an invalid tool name is the
    # model's own malformed output, a hook fault is a host fault, a cascaded
    # batch member was never reached, and an expired approval prompt was simply
    # never answered — so naming a sanctioned alternative there would answer a
    # question nobody asked and imply the action itself had been refused.
    remediation = (
        remediation_for(reason, title, credential_tool_hint=credential_tool_hint)
        if cause == DENY_CAUSE_POLICY
        else ""
    )
    tail = f"\n\nHow to do this properly: {remediation}" if remediation else ""
    # "host notice", not "policy notice": the tag has to be true for every
    # cause, and only one of them IS a policy. Naming the ACTOR is also what the
    # notice exists to do — the model has just been told the user denied this, and
    # every sentence after this one is spent correcting that.
    return (
        f"[Kiro Crew host notice] The tool call you just made {clause}. "
        "This was NOT a user action — the user did not "
        "cancel, reject, or interrupt anything. The tool result you were handed for "
        "it is generic and wrong about who denied it — on kiro-cli it reads "
        '"User denied tool execution".\n\n'
        f"Blocked: {what}\n\n"
        "Do not apologise for a cancellation and do not ask the user whether to "
        f"retry. Decide and continue in this same turn: {guidance}{tail}"
    )


async def steer_refusal_notice(
    provider: Any,
    title: str,
    reason: str,
    *,
    cause: str,
    bound_secs: float = STEER_NOTICE_BOUND_SECS,
) -> str:
    """Steer a deny notice into the RUNNING turn, best-effort and bounded.

    The one spelling of "probe the capability, redact, build, send within a
    bound" shared by the deny sites that have no dashboard slot to render into:
    the native Slack handler's approval-timeout arm and the messaging
    ``TurnDriver``'s. (``chat_runner._steer_policy_notice`` adds the dashboard's
    display row and credential hint on top of the same steps.)

    Must be awaited while the ``session/request_permission`` is still
    unanswered — see :func:`build_refusal_steer_notice` for why that ordering is
    what makes the notice race-free. Opt-in by positive capability
    (``provider.supports_steer``), never by harness identity; ``getattr`` because
    the reject paths also run against minimal test doubles. *title* is redacted
    here (it is provider-authored text); *reason* is the caller's own wording.

    Returns the notice that was written, or ``""`` when nothing was sent: no
    capability, nothing to say, a steer failure, or the bound expiring. Every
    ``Exception`` is swallowed at debug — the caller's reject must still run —
    while ``CancelledError`` propagates, because the caller is the one that
    knows how to answer the wire while it unwinds.
    """
    if not getattr(provider, "supports_steer", False):
        return ""
    try:
        title_safe, _ = redact_exfiltration_urls(title or "")
        title_safe, _ = redact_credentials(title_safe)
        notice = build_refusal_steer_notice(title_safe, reason, cause=cause)
        if not notice:
            return ""
        sent = await asyncio.wait_for(provider.steer(notice), timeout=bound_secs)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("deny-cause steer notice failed; the reject still runs", exc_info=True)
        return ""
    # A provider that answers False did not write the notice; report that
    # honestly rather than claim an explanation the model never received.
    return notice if sent is not False else ""
