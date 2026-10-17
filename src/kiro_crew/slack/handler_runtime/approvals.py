"""The tool-approval prompts the native Slack path posts and what a click on one means.

The Block Kit prompt, the pending and linked-slot registry entries a click resolves, the
durable Trust a linked dashboard card may grant and the dashboard prompt that mirrors a
Slack one, and the linked-click and late-Trust branches of ``handle_interaction``. The
registries themselves are module state of :mod:`kiro_crew.slack.handler`, and the
functions that answer the wire (``_request_approval``, ``_reject_orphaned_tool``,
``_steer_host_deny``, ``handle_interaction``) stay there because
``test_messaging_deny_notice`` reads every ``reject_tool`` site and its steer window in
that file.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _ACTION_APPROVE,
        _ACTION_REJECT,
        _ACTION_TRUST,
        _SLACK_SECTION_TEXT_LIMIT,
        _TRUNCATION_MARKER,
        LLMEvent,
        LLMProvider,
        SessionManager,
        SlackClientOps,
        Stats,
        _dashboard_state,
        _linked_approvals,
        add_trusted_session,
        effective_session_key,
        is_allowed_user,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
    )


class _PendingApproval:
    __slots__ = ("provider", "request_id", "session_key", "future")

    def __init__(self, provider: LLMProvider, request_id: str | int, session_key: str = "") -> None:
        self.provider = provider
        self.request_id = request_id
        self.session_key = session_key
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()


class _LinkedApproval:
    """A tool-approval prompt posted to Slack on behalf of a *linked dashboard
    slot*.

    Unlike :class:`_PendingApproval`, this entry does NOT own the ACP backend
    answer. For a Slack-linked dashboard session the consumer that actually
    calls ``approve_tool`` / ``reject_tool`` is the dashboard's ``_run_chat``
    loop, which is parked on the slot's approval *future*. A Slack button click
    here must therefore ONLY resolve that future (via
    ``state.resolve_approval``); the dashboard loop then answers the backend
    exactly once. Calling ``approve_tool`` from here too would answer the
    JSON-RPC request twice.

    ``trust_grantable`` carries the server-side durable-grant proof that was true
    of the dashboard card when the prompt was mirrored (see
    :func:`_linked_trust_grantable`). It defaults to False so an entry built
    without it -- and therefore every path that never established the proof --
    cannot grant Trust.
    """

    __slots__ = ("request_id", "session_key", "trust_grantable")

    def __init__(
        self,
        request_id: str | int,
        session_key: str,
        trust_grantable: bool = False,
    ) -> None:
        self.request_id = request_id
        self.session_key = session_key
        self.trust_grantable = trust_grantable


class _LinkedApprovalEvent:
    """Minimal event shim for :func:`_build_approval_blocks`.

    The dashboard's permission event (``AcpEvent``) and the Slack-native
    ``LLMEvent`` have different shapes, so adapt the few fields the block
    builder reads: ``request_id``, ``title``, ``tool_input``, ``tool_purpose``.
    """

    __slots__ = ("request_id", "title", "tool_input", "tool_purpose")

    def __init__(self, request_id: str | int, title: str, tool_input: str = "") -> None:
        self.request_id = request_id
        self.title = title
        self.tool_input = tool_input
        self.tool_purpose = ""


def _linked_slots_for(session_key: str) -> list[Any]:
    """Every live dashboard slot whose turns run on *session_key*.

    Keyed by the slot's EFFECTIVE session key, the one derivation the dashboard's
    own trust resolver uses: a linked cron/workflow or channel-surfaced slot runs
    under its ``linked_session_key``, not under ``dashboard:{key}``, so matching on
    the raw slot key would miss exactly the slots this Slack mirror serves.

    Empty when there is no dashboard state, no slots, or no match — every caller
    treats that as "cannot act on this session".
    """
    slots = getattr(_dashboard_state, "_slots", None) if _dashboard_state is not None else None
    if not slots:
        return []
    return [slot for slot in list(slots.values()) if effective_session_key(slot) == session_key]


def _linked_trust_grantable(session_key: str, request_id: str | int) -> bool:
    """Whether the dashboard card behind a linked approval may grant durable Trust.

    ``chat_runner`` stamps ``trust_grantable`` onto a pending permission card only
    when the call is unredacted and its grant scope is fully derivable, precisely so
    an alternate approval surface cannot offer a durable grant merely because it
    received a pending card; the dashboard resolver refuses a grant whose card lacks
    the bit (``pattern_underivable``). This Slack mirror IS such a surface, so it
    re-derives the same server-side proof from the owning slot's card rather than
    treating the click as authority.

    Fail-closed: no dashboard state, no owning slot, no card, or any raise -> no
    Trust, leaving the prompt allow-once/reject exactly as before.
    """
    try:
        # Reuse the dashboard resolver's own card reader, so the two surfaces cannot
        # disagree about what a card says. Imported at call time: chat_handlers ->
        # chat_runner -> this module, so a module-level import would close a cycle
        # (same reason as ``_run_chat`` below).
        from kiro_crew.dashboard.chat_handlers import _get_pattern_from_pending

        for slot in _linked_slots_for(session_key):
            if _get_pattern_from_pending(slot, str(request_id), "trust_grantable") == "1":
                return True
    except Exception:
        logger.warning(
            "Could not derive linked trust proof (session=%s req=%s); withholding Trust",
            session_key,
            request_id,
            exc_info=True,
        )
    return False


def _grant_linked_trust(linked_entry: _LinkedApproval, sessions: SessionManager | None) -> bool:
    """Grant durable session Trust for a linked slot. True only on a REAL grant.

    All three halves, or none. A linked slot's own tool approvals are re-decided per
    event by ``chat_runner._slot_is_trusted``, which reads ``slot._trust``; the
    session ``approval_policy`` is the half a spawned subagent inherits, and
    ``chat_runner`` rewrites it from ``_persistable_session_policy(slot, ...)`` on
    every session create/resume — so a policy-only write would be erased at the next
    turn and a ``_trust``-only write would never reach subagents. The third is the
    shared ``messaging.session_trust`` mapping, which the channel ``TurnDriver``
    reads: a channel-born slot's own Slack thread is driven by
    ``slack/transport_dispatch``, not by the dashboard chat runner, so without it a
    Slack-typed follow-up re-prompts for every tool. Written under the slot's
    EFFECTIVE session key, the same key the dashboard resolver grants under.

    Fail-closed, and deliberately in the opposite order to the dashboard resolver
    (which writes ``_trust`` first and lets a raise become a 500): the fallible
    policy write goes FIRST — via the shared grant's ``strict`` mode, which undoes
    its own in-memory half and re-raises — so a failure leaves no slot silently
    trusted while the caller reports the click as denied. Returns False — no grant
    at all — when the card carries no durable-grant proof, when there is no session
    manager to hold the subagent half, when no live slot owns the session, or on any
    raise.
    """
    if not linked_entry.trust_grantable or sessions is None:
        return False
    try:
        slots = _linked_slots_for(linked_entry.session_key)
        if not slots:
            return False
        # Through the shared channel-neutral grant, which owns BOTH the mapping the
        # channel driver reads and the parent approval_policy a subagent reads --
        # the same seam every other trust path in this module goes through, rather
        # than poking `set_approval_policy` here. The mapping half is not optional:
        # a CHANNEL-BORN slot's turns run on the channel's own session key, and its
        # thread is deliberately absent from ``_slack_to_slot`` (see
        # ``state.get_or_create_slot``), so a Slack-typed follow-up is driven by
        # ``slack/transport_dispatch`` -- whose TurnDriver gates auto-approval on
        # ``is_session_trusted``, never on the session policy. Without it the user
        # is re-prompted for every tool on the very thread they granted Trust from.
        #
        # ``strict`` is what keeps this fail-closed: the policy write is the
        # fallible half, and the default grant swallows its failure, which would
        # report a partial grant to the clicker as "Trusted".
        add_trusted_session(linked_entry.session_key, sessions, strict=True)
        for slot in slots:
            slot._trust = True
    except Exception:
        logger.warning(
            "Failed to grant linked session trust (session=%s)",
            linked_entry.session_key,
            exc_info=True,
        )
        return False
    return True


async def post_linked_approval(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    request_id: str | int,
    session_key: str,
    title: str,
    tool_input: str = "",
) -> str | None:
    """Mirror a dashboard tool-approval prompt into a linked Slack thread.

    Posts Approve / Reject buttons threaded under ``thread_ts`` and registers a
    :class:`_LinkedApproval` keyed by ``channel:ts`` so a button click resolves
    the dashboard slot's approval future (see :func:`handle_interaction`).

    Returns the Slack message ts on success, or ``None`` if the post failed.
    The caller (dashboard ``_run_chat``) treats ``None`` as "delivery failed"
    and surfaces it rather than silently parking on an unanswerable prompt.

    Trust ("Trust session") is offered only when BOTH hold:

    * the mirror target is a DM (``channel`` starts with ``D``) — the native path's
      blast-radius rule, since trust escalates the whole session; and
    * the dashboard card carries the server's durable-grant proof
      (:func:`_linked_trust_grantable`).

    Without both, the prompt stays Approve / Reject, which is still enough to
    guarantee it is answerable from Slack.

    The verdict is recorded on the registry entry and re-consulted at click time
    (:func:`_grant_linked_trust`), so the grant rests on what this process derived
    when it rendered the prompt — never on the ``action_id`` the Slack payload
    carries, which a rendered button does not make authoritative.
    """
    # title / tool_input are LLM-generated (the tool-use request). Slack is an
    # external surface, so scrub them the same way every other outbound LLM
    # string is scrubbed before posting — the dashboard path already redacts
    # these via perm_meta, but this Slack mirror must do its own redaction.
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    tool_input, _ = redact_exfiltration_urls(tool_input)
    tool_input, _ = redact_credentials(tool_input)
    event = _LinkedApprovalEvent(request_id, title, tool_input)
    trust_grantable = channel.startswith("D") and _linked_trust_grantable(session_key, request_id)
    # _build_approval_blocks is typed for AcpEvent but only reads the four
    # attributes the shim provides (request_id/title/tool_input/tool_purpose).
    blocks = _build_approval_blocks(event, is_dm=trust_grantable)  # type: ignore[arg-type]
    try:
        approval_ts = await slack.post_blocks(
            channel, blocks, "Manual approval required", thread_ts
        )
    except Exception:
        logger.warning(
            "Failed to post linked approval prompt to Slack (session=%s req=%s)",
            session_key,
            request_id,
            exc_info=True,
        )
        return None
    _linked_approvals[f"{channel}:{approval_ts}"] = _LinkedApproval(
        request_id, session_key, trust_grantable
    )
    return approval_ts


def resolve_linked_approval(channel: str, approval_ts: str) -> None:
    """Drop a linked-approval registry entry (after the dashboard resolved it)."""
    _linked_approvals.pop(f"{channel}:{approval_ts}", None)


def _build_approval_blocks(event: LLMEvent, is_dm: bool = True, source: str = "") -> list[dict]:
    """Build Block Kit blocks for tool approval prompt.

    Args:
        event: The permission-request event from the LLM provider.
        is_dm: True when posting to a DM (adds Trust button).
        source: Optional label for background agents (e.g. "subagent",
            "cron").  Prefixed to the header so users can tell main-agent
            approvals apart from background ones.

    Shows the full command text (from tool_input) in a code block so users
    can see exactly what will run before approving.  Falls back to the
    truncated title when tool_input is unavailable.

    In DMs: Approve / Trust / Reject
    In group channels: Approve / Reject only (Trust excluded
    to limit blast radius — it escalates permissions for the session).
    YOLO is owner-only via ``!yolo on`` command — no button.
    """
    # Slack Block Kit requires button `value` to be a string. ACP backends
    # (e.g. claude-agent-acp) issue integer JSON-RPC request ids, so coerce —
    # an int value makes Slack reject the whole post with `invalid_blocks`.
    # The interactive handler matches on channel:msg_ts and acts on the stored
    # `_PendingApproval.request_id`, so the button value itself is display-only.
    req_value = str(event.request_id)
    buttons: list[dict] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve"},
            "style": "primary",
            "action_id": _ACTION_APPROVE,
            "value": req_value,
        },
    ]
    if is_dm:
        buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Trust session"},
                "action_id": _ACTION_TRUST,
                "value": req_value,
            },
        )
    buttons.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reject"},
            "style": "danger",
            "action_id": _ACTION_REJECT,
            "value": req_value,
        },
    )

    blocks: list[dict] = []

    tag = f"[{source}] " if source else ""
    title_safe, _ = redact_exfiltration_urls(event.title)
    title_safe, _ = redact_credentials(title_safe)
    footer = f":lock: {tag}*{title_safe}*"
    if event.tool_purpose:
        purpose, _ = redact_exfiltration_urls(event.tool_purpose)
        purpose, _ = redact_credentials(purpose)
        footer += f" — {purpose}"

    # When full tool_input is available, show a simple header and the
    # complete command in a code block below.
    # When tool_input is missing, fall back to the truncated title.
    if event.tool_input:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔐 *{tag}Tool approval requested:*"},
            },
        )
        # Security: scan for exfiltration URLs and credentials before posting
        sanitized, _ = redact_exfiltration_urls(event.tool_input)
        sanitized, _ = redact_credentials(sanitized)
        # Truncate with marker if exceeds Slack limit
        if len(sanitized) > _SLACK_SECTION_TEXT_LIMIT:
            detail = (
                sanitized[: _SLACK_SECTION_TEXT_LIMIT - len(_TRUNCATION_MARKER)]
                + _TRUNCATION_MARKER
            )
        else:
            detail = sanitized
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{detail}```"},
            },
        )

    blocks.append({"type": "actions", "elements": buttons})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def _resolve_linked_click(
    linked_entry: _LinkedApproval,
    key: str,
    action_id: str,
    user_id: str,
    sessions: SessionManager | None,
) -> str:
    """Resolve a click on a linked dashboard slot's prompt; return the action it took.

    The dashboard's ``_run_chat`` owns the ACP answer (it is parked on the slot's
    approval future), so this resolves ONLY that future, through
    ``state.resolve_approval``, and never calls ``approve_tool`` / ``reject_tool``:
    that would answer the JSON-RPC request twice. Anything that isn't an explicit
    reject approves THIS call; a Trust click additionally widens the session, and only
    a widening that actually took counts as an approval.
    """
    approved = action_id != _ACTION_REJECT
    trusted = False
    if action_id == _ACTION_TRUST:
        trusted = _grant_linked_trust(linked_entry, sessions)
        # A Trust click that could not grant must NOT be quietly downgraded to
        # a one-shot approve and labelled "Trusted": that reports a security
        # state the session does not have. Deny instead, so the user sees the
        # escalation fail and retries.
        approved = trusted
        if trusted:
            logger.info("Trust mode ON (linked) for session %s", linked_entry.session_key)
        else:
            logger.warning("Refusing linked trust click for session %s", linked_entry.session_key)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.trust_linked",
            outcome="allowed" if trusted else "denied",
            source="slack",
            resources=linked_entry.session_key,
            error="" if trusted else "trust_grant_unavailable",
        )
    resolved = False
    if _dashboard_state is not None and hasattr(_dashboard_state, "resolve_approval"):
        try:
            resolved = bool(
                _dashboard_state.resolve_approval(str(linked_entry.request_id), approved)  # type: ignore[attr-defined]
            )
        except Exception:
            logger.warning(
                "Failed to resolve linked approval (req=%s)",
                linked_entry.request_id,
                exc_info=True,
            )
    _linked_approvals.pop(key, None)
    sel().log_api_access(
        caller=user_id,
        operation="slack.interactive.approval_linked",
        outcome="allowed" if approved else "denied",
        source="slack",
        resources=linked_entry.session_key,
        error="" if resolved else "future_not_found",
    )
    if approved:
        Stats().inc_tool_approval()
    else:
        Stats().inc_tool_denial()
    if trusted:
        return _ACTION_TRUST
    return _ACTION_APPROVE if approved else _ACTION_REJECT


async def _grant_late_trust(
    channel: str,
    thread_ts: str,
    user_id: str,
    slack: SlackClientOps | None,
    sessions: SessionManager | None,
) -> str | None:
    """Grant Trust for a thread whose approval was already resolved, or refuse.

    Replicates handle_message's session-key derivation (the thread, then a linked
    dashboard session's override), and grants only to an allowed user who owns the
    thread. Returns ``_ACTION_TRUST`` on a grant and ``None`` on every refusal, each
    audited.
    """
    if not is_allowed_user(user_id):
        logger.warning("Rejecting late trust click from non-allowed user %s", user_id)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.trust_late",
            outcome="denied",
            source="slack",
            error="unauthorized user",
        )
        return None
    # Verify clicking user owns this thread (prevents privilege escalation)
    if not slack:
        logger.warning(
            "Rejecting late trust click: cannot verify thread ownership (no slack client)"
        )
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.trust_late",
            outcome="denied",
            source="slack",
            error="no_slack_client",
        )
        return None
    try:
        msgs = await slack.fetch_thread_replies(channel, thread_ts, limit=1)
        thread_owner = msgs[0].get("user", "") if msgs else ""
    except Exception:
        logger.warning("Failed to verify thread ownership for %s", thread_ts, exc_info=True)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.trust_late",
            outcome="denied",
            source="slack",
            error="thread_ownership_check_failed",
        )
        return None
    if not thread_owner or thread_owner != user_id:
        logger.warning("Rejecting late trust click: user %s is not thread owner", user_id)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.trust_late",
            outcome="denied",
            source="slack",
            error="not_thread_owner",
        )
        return None
    # Imported at call time on purpose: tests patch
    # ``kiro_crew.session.SessionMap`` to drive the fail-closed path, and
    # only a call-time rebind observes that patch.
    from kiro_crew.session import SessionMap

    session_key = thread_ts
    try:
        linked = SessionMap().get_session_for_thread(thread_ts)
        if linked:
            session_key = linked
    except Exception:
        logger.warning(
            "SessionMap lookup failed for thread %s; refusing to grant trust",
            thread_ts,
            exc_info=True,
        )
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.trust_late",
            outcome="denied",
            source="slack",
            error="session_map_lookup_failed",
        )
        return None
    # Through the shared grant, which owns BOTH halves: the in-memory
    # mapping the driver reads and the parent approval_policy a subagent
    # reads (see subagent.py). Poking the container directly would let a
    # revoke clear one half and leave the other, so the two are not
    # separable at a call site.
    add_trusted_session(session_key, sessions)
    logger.info("Trust mode ON (late click) for session %s", session_key)
    sel().log_api_access(
        caller=user_id,
        operation="slack.interactive.trust_late",
        outcome="allowed",
        source="slack",
        resources=session_key,
    )
    return _ACTION_TRUST
