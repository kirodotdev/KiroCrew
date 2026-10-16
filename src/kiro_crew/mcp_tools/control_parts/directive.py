"""The session-directive emitter and the tools whose whole effect is one directive.

:func:`_emit_directive` encodes a validated directive into the tool result and publishes
the call out of band. ``nothing_to_do``, ``set_project``, ``reset_conversation``,
``chat_tag`` and ``suggest_followup`` validate their arguments and return its marker; the
session-aware consumer applies the directive to its own session, so none of them resolves
a session identity. The monitoring tools emit theirs through the same function.

This module is an owner of :mod:`kiro_crew.mcp_tools.control`, which serves every name
here and stays the import path and the patch seam: a patch through it reaches this module
too (:mod:`kiro_crew._facade_mirror`). It never imports the facade.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import mcp_core, session_directive
from kiro_crew.validation import (
    CHAT_TAG_SCHEMA,
    NOTHING_TO_DO_SCHEMA,
    RESET_CONVERSATION_SCHEMA,
    SET_PROJECT_SCHEMA,
    SUGGEST_FOLLOWUP_SCHEMA,
    validate_tool_args,
)


def _emit_directive(kind: str, args: dict[str, Any], human: str) -> str:
    """Encode a validated directive AND publish it out of band; return the text.

    Two delivery paths, one each way round:

    * The MARKER in the returned text is the original path. A consumer that can
      verify the call's ``_meta.kiro`` identity (kiro-cli) decodes and applies it
      from there, exactly as before — this function changes nothing for that
      backend.
    * The out-of-band POST is the provider-neutral path. ``_post`` already carries
      ``X-Session-Key`` (and the gateway kernel-verifies that claim on the unix
      socket), so the gateway parks the payload for the RIGHT session without the
      model's tool result being trusted for anything. What travels is the CALL
      (tool name + raw arguments), never the payload: the gateway re-runs this
      tool on those arguments to derive the payload and computes the claim key
      itself, and the consumer recomputes that key from the ``tool_call``
      frame — so neither the result body's shape nor a caller-authored payload
      decides what lands. A backend that emits no ``_meta.kiro`` identity has
      no other way to reach its own control plane.

    Order matters: encode FIRST. ``encode`` refuses an oversized payload by
    returning a marker-less error string, and a refused directive must NOT be
    published — otherwise the model is told "nothing was applied" while a record
    sits waiting to apply it.

    Fail-soft on the POST, and SILENT by design. An older gateway with no such
    route, or one that is simply down, must not turn a working tool call into an
    error: the marker is already in hand and the kiro-cli path still works. There
    is no log line because this module runs as a stdio MCP server, where the
    process's own streams are the protocol channel — and because the failure that
    matters is reported at the CONSUMER, which is the side that knows whether a
    directive actually landed.
    """
    out = session_directive.encode(kind, args, human)
    # STRUCTURAL test, not a content test: encode either produced a marker or it
    # refused. Asking ``is_refusal(out)`` instead matched any payload that merely
    # CONTAINED the refusal token — so a stop whose reason quoted that token was
    # classified as a refusal, skipped the publish and the vouch, and had its real
    # marker defanged downstream, losing the stop. That is the same
    # "infer provenance from imitable content" mistake this gate exists to remove.
    if not session_directive.has_marker(out):
        return out
    # Gateway-side derivation (mcp_core.derive_directive) re-runs this very
    # handler and wants the validated payload, not a POST.
    if mcp_core.capture_directive(kind, args):
        return out
    _tool = mcp_core.current_call_name()
    if not _tool:
        # Not inside a ``_call_tool`` dispatch (a direct handler call, e.g. from a
        # test): there is no call to report, and an empty one would only be
        # refused by the gateway as not derivable.
        return out
    try:
        # The gateway is sent the CALL, not the payload: the tool's name and the
        # raw arguments it was invoked with (recorded in _call_tool before
        # validation). The gateway re-derives the payload by re-running the tool
        # and computes the claim digest itself, so a caller who can reach the
        # route controls only what the victim's own call would produce.
        mcp_core._post(
            "/api/session-directive",
            {
                "tool": _tool,
                "raw_args": mcp_core.current_call_raw_args(),
            },
        )
    except Exception:
        pass
    # POSITIVE PROVENANCE: this is the one place a real marker is produced, so it
    # is the one place that can say so. Without it the outermost gate has to infer
    # authenticity from the bytes, which a rejection echoing a model-chosen
    # argument name can imitate.
    return session_directive.vouch(out)


def nothing_to_do(name: str, args: dict[str, Any]) -> str:
    """The deliberate quiet exit of a turn, as a session directive.

    Stateless like every directive: the tool validates its one optional field
    and returns a marker; the session-aware consumer (chat_runner, TurnDriver)
    applies it against ITS OWN session and records the applied directive as the
    turn's terminal output, so the empty-response recovery does not treat the
    textless end as a failed generation. No surface gate and no identity gate:
    a quiet end is meaningful on every surface (dashboard, channel, cron wake),
    and it mutates nothing a wrong identity could misdirect — a sub-agent's call
    flows through the sub-agent's own runner (and a native child's result frame
    is refused by the consumer's identity gate), so it can never end its
    parent's turn.
    """
    args = validate_tool_args(args, NOTHING_TO_DO_SCHEMA)
    note = str(args.get("note") or "").strip()
    payload: dict[str, Any] = {"note": note} if note else {}
    return _emit_directive(
        "nothing_to_do",
        payload,
        "Quiet end requested for this turn. Write nothing and call nothing "
        "after this: the turn is over. The consumer records the quiet step; "
        "if it refuses, the normal empty-reply handling applies instead.",
    )


def set_project(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SET_PROJECT_SCHEMA)
    # Stateless: the session-aware consumer (chat_runner) applies the
    # project change to ITS OWN slot — no session identity resolved here.
    return _emit_directive(
        "set_project",
        {"project": args.get("path", ""), "clear": bool(args.get("clear"))},
        "Project change requested for this session; if the path is valid "
        "and permitted it takes effect on the next message (cold-start with "
        "the new CWD and project steering). An invalid or sensitive path is "
        "rejected when this turn's result is processed.",
    )


def reset_conversation(name: str, args: dict[str, Any]) -> str:
    validate_tool_args(args, RESET_CONVERSATION_SCHEMA)
    # Stateless: the session-aware consumer (chat_runner) queues the discard
    # against ITS OWN slot — no session identity resolved here. The payload is
    # empty because there is nothing to choose: a caller asking for a clean
    # context always wants a clean one, and the HTTP route carries a replay flag
    # for the rare caller that does not.
    return _emit_directive(
        "reset_conversation",
        {},
        "Conversation reset requested for this session; if this turn is "
        "user-facing it takes effect at a turn boundary, and the next message "
        "starts with no memory of this conversation. The transcript is not "
        "deleted — write down anything that must carry forward.",
    )


def chat_tag(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, CHAT_TAG_SCHEMA)
    # Stateless: the session-aware consumer (chat_runner) applies the tag change
    # to ITS OWN slot — no session identity resolved here. The payload carries
    # only the requested change; the consumer resolves ids against the live
    # vocabulary, enforces the per-tag agent policy, and returns the resulting
    # tag list (which is also this tool's READ path for the session's tags).
    payload: dict[str, Any] = {}
    if args.get("set_state"):
        payload["set_state"] = args["set_state"]
    if args.get("add"):
        payload["add"] = args["add"]
    if args.get("remove"):
        payload["remove"] = args["remove"]
    return _emit_directive(
        "chat_tag",
        payload,
        "Board tag change requested for this session; if the tags are "
        "agent-writable it takes effect when this turn's result is processed, "
        "and the result reports the session's resulting tags. A human-only tag, "
        "an unknown tag, or a headless caller (cron/subagent) is refused.",
    )


def suggest_followup(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SUGGEST_FOLLOWUP_SCHEMA)
    items = args.get("items") or []
    # Stateless: the session-aware consumer (chat_runner) broadcasts
    # the card to ITS OWN slot; no session identity resolved here. The card
    # is broadcast-only (dropped if no client attached), so the confirmation
    # stays cautious — restate the follow-ups in reply text if they matter.
    return _emit_directive(
        "suggest_followup",
        {"items": items},
        "Follow-up card requested for this session. It is delivered to a "
        "connected dashboard client only; if none is attached the card is "
        "dropped, so restate the follow-ups in your reply text if they "
        "must not be lost. End your turn now.",
    )
