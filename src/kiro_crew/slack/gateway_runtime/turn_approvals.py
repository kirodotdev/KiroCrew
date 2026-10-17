"""The tool-approval callbacks unattended and channel turns ask.

The interactive approval that races the dashboard prompt against the Slack DM, the
check for an attached dashboard client, and the heartbeat's read-only gate.

Orchestrator fields read: ``_approval_mode``, ``_cfg``, ``_owner_id``,
``autonudge_svc``, ``dashboard_state``, ``sessions``, ``slack``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import re

    from kiro_crew.slack.gateway import (
        _BACKGROUND_APPROVAL_SOURCES,
        HEARTBEAT_KEY,
        Callable,
        DashboardState,
        GatewayOrchestrator,
        LLMEvent,
        SpawnApprovalUnreachable,
        ToolApprovalCallback,
        _auto_approve_reason,
        _is_heartbeat_safe_tool,
        _is_read_only_tool,
        _slot_is_trusted,
        command_invokes_git,
        dashboard_slot_key,
        is_read_only_bash,
        logger,
        name_grant,
        redact,
        redact_credentials,
        redact_exfiltration_urls,
        safety_override,
        sel,
    )


def _dashboard_client_attached(self: GatewayOrchestrator) -> bool:
    """Report whether an attached dashboard client could answer a prompt NOW.

    Asked at exactly one place: the dashboard-only fallback in
    ``_interactive_approval``. Reaching it means Slack has already had its
    turn — either no owner DM was configured, or posting the prompt to it
    raised and the callback fell through — so the question left is only about
    the dashboard, and a Slack term here would report a surface that
    demonstrably did NOT receive the prompt.

    Counts DASHBOARD-USER sockets, not every ``/api/ws`` registration. An app
    token registers as a client too, and the broadcast chokepoint sends it an
    owner-surface frame only if its manifest declared that event -- so an open
    app UI is not somebody who can answer the prompt, for the same reason a
    configured-but-failed Slack DM is not.

    Deliberately narrow, because a false "attached" merely restores today's
    behaviour while a false "detached" refuses a spawn a human WOULD have
    approved:

    * an unreadable client count is treated as attached;
    * no ``dashboard_state`` at all is genuinely no surface, but the caller
      reaches its own no-UI branch before asking, so that answer never
      refuses anything.
    """
    if self.dashboard_state is None:
        return False
    try:
        return int(self.dashboard_state.dashboard_user_ws_count()) > 0
    except Exception:
        logger.debug(
            "dashboard_user_ws_count failed; treating the dashboard as attached",
            exc_info=True,
        )
        return True


def _interactive_approval(
    self: GatewayOrchestrator,
    source: str,
    slot_resolver: Callable[[str], str] | None = None,
    nudge_key: str = "",
    *,
    raise_when_unreachable: bool = False,
) -> ToolApprovalCallback:
    """Return an approval callback that races dashboard vs Slack DM.

    Uses the same rich Block Kit message as the main-agent approval flow
    so users see full command text, security redactions, and Trust-session
    controls for background agents too.

    ``nudge_key`` names the monitoring loop whose cycle this callback serves,
    when one does. A channel-bound loop's turns are approved here rather than
    through the dashboard runner, so without it an unanswered prompt on this
    path records no evidence and such a loop keeps waking, being declined and
    spending its cycle cap -- while the expiry notice still promises a stop.

    ``raise_when_unreachable`` opts this callback into raising
    ``SpawnApprovalUnreachable`` instead of parking on a prompt no surface
    received. Only the spawn gate sets it, because only the spawn gate has a
    terminal path that can report the refusal to the calling agent; a mid-run
    tool approval parks on the prompt instead.
    """

    is_background = source in _BACKGROUND_APPROVAL_SOURCES

    async def _approve(event: LLMEvent, parent_session_key: str = "") -> bool:
        request_id = str(event.request_id)
        # Low-fidelity CHILD request: the structured security context is
        # absent, so every field a content-matching shortcut below would
        # judge (title, read-only classification, trust patterns) is
        # agent-authored. Unless its canonical MCP identity is verified
        # (``_child_grant_eligible`` below), such a request may ONLY be
        # approved by the human prompt at the end of this callback —
        # every non-human auto-approve shortcut (auto_approve_sources,
        # --approval yolo/reads, YOLO override, slot trust) is skipped
        # for it. Strict ``is True``: real events
        # (AcpEvent) return a genuine bool; anything else (e.g. a mock
        # or a foreign event type) must not accidentally enter the
        # restricted path on a truthy non-bool.
        _child_lf = getattr(event, "child_low_fidelity", False) is True
        # Hoisted grant-eligibility — see
        # AcpEvent.child_unconditional_grant_eligible for which shortcuts
        # below may honor it (per-source auto-approve, --approval yolo,
        # the YOLO override, slot trust) and which must not (the 'reads'
        # mode MATCHES the agent-authored title). The outer
        # ``not _child_lf`` short-circuit keeps a foreign event type or
        # mock — which never entered the restricted path via the strict
        # ``_child_lf`` probe — eligible without consulting an attribute
        # it may not have; the property is only reached for a genuinely
        # low-fidelity event, with the same strict ``is True`` rationale
        # as ``_child_lf``.
        _child_grant_eligible = (not _child_lf) or (
            getattr(event, "child_unconditional_grant_eligible", False) is True
        )
        # Background callers pass the authoritative parent session key. Prefer it
        # over a request-ID resolver because tool permission IDs are opaque UUIDs,
        # unlike spawn approvals (``spawn:<agent_id>``). Treating a tool request ID
        # as an agent ID loses the dashboard slot and hides the approval prompt.
        # ``dashboard_slot_key`` answers "which tab shows this conversation?", so a
        # channel-born session gets its prompt in the tab it is open in too.
        parent_slot = dashboard_slot_key(parent_session_key)

        # NO heuristic fallback. A background caller (cron / taskrunner /
        # autonudge) that supplies neither an authoritative parent session
        # nor a ``slot_resolver`` has no owning conversation, and there is
        # no way to guess one. Borrowing "the first slot that is running"
        # hijacked an unrelated chat and was wrong in three directions at
        # once:
        #   * the prompt surfaced in a conversation that never raised it,
        #     with a truncated label and no provenance;
        #   * the Trust control resolved against that innocent slot, so
        #     trusting a cron's command granted blanket auto-approval to
        #     the borrowed session (and did nothing for the cron);
        #   * conversely, a borrowed slot that already had trust enabled
        #     silently auto-approved the background command below —
        #     privilege the cron was never granted.
        # Unowned approvals now carry slot="" and are surfaced ONLY on the
        # global approvals surface (notification feed / /api/approvals).
        if parent_slot:
            approval_slot = parent_slot
        elif slot_resolver:
            try:
                approval_slot = slot_resolver(request_id) or ""
            except Exception:
                logger.warning("slot_resolver failed for %s", request_id, exc_info=True)
                approval_slot = ""
        else:
            approval_slot = ""

        # Per-source auto-approve (e.g. cron, taskrunner, subagent)
        if source in self._cfg.hooks.get("auto_approve_sources", []):
            if not _child_grant_eligible:
                # The operator explicitly configured this source to run
                # UNATTENDED — nobody is watching the interactive window,
                # so parking a low-fidelity child request there would
                # stall the run for the full approval timeout and then
                # deny anyway. Fail closed fast instead (an approve is
                # still never allowed on agent-authored context).
                logger.warning(
                    "Fast-denying low-fidelity child request under "
                    "auto-approve source %s (unattended; title is "
                    "agent-authored)",
                    source,
                )
                return False
            logger.info("Auto-approving tool %s from source %s", event.title, source)
            return True

        # CLI --approval flag override (composable test mode).
        # 'yolo' auto-approves all; 'reads' auto-approves read-only tools;
        # 'interactive' falls through to the standard flow.
        # 'yolo' is an UNCONDITIONAL grant (consumes no event data) so a
        # verified-identity child qualifies; 'reads' classifies the
        # agent-authored TITLE, so it requires the composite fidelity.
        if self._approval_mode in ("yolo", "reads") and _child_grant_eligible:
            # A shell command is a 'reads' grant only when the read-only
            # classifier accepts its own text and it runs no git, whatever its
            # title says: the title is agent-authored, and git reads
            # `.git/config`, whose `core.fsmonitor` / `core.hooksPath` name a
            # program no text classifier sees.
            _shell_command = getattr(event, "shell_command", None) or ""
            _reads_git = bool(
                getattr(event, "is_shell", False)
                and (not is_read_only_bash(_shell_command) or command_invokes_git(_shell_command))
            )
            approve = self._approval_mode == "yolo" or (
                self._approval_mode == "reads"
                and not _child_lf
                and not _reads_git
                and _is_read_only_tool(event.title or "")
            )
            if approve and self._approval_mode == "reads":
                # 'reads' is a NAME-shaped grant: it classifies the title,
                # and the shell resolves the command's program names again
                # through a PATH that can lead with agent-writable
                # directories — the same tier the dashboard's trust-reads
                # rung verifies. A refused name falls through to the
                # interactive prompt below (never a hard block), so a
                # PATH-shadowed program cannot ride the reads grant on an
                # unattended cron/autonudge turn. 'yolo' is unconditional
                # (consumes no event data) and stays unverified by design.
                _ng_refusal = await name_grant.refusal_for_event(event)
                if _ng_refusal is not None:
                    logger.warning(
                        "declining a reads-mode auto-approve: %s; the "
                        "request falls through to the interactive prompt",
                        _ng_refusal.log_text,
                    )
                    name_grant.log_decline(
                        source="background",
                        session_key=parent_session_key,
                        event=event,
                        refusal=_ng_refusal,
                        tier="cli_approval_reads",
                        metadata={"caller_source": source},
                        sel_factory=sel,
                    )
                    approve = False
            if approve:
                # Emit a SEL audit event so the audit trail records WHICH
                # mode auto-approved the tool. Downstream sites already
                # log the invocation itself; this captures the decision.
                try:
                    _safe = redact(event.title or "")
                    sel().log_api_access(
                        caller=f"cli:approval={self._approval_mode}",
                        operation=f"{source}.cli_approval_auto_approve",
                        outcome="ok",
                        resources=_safe,
                    )
                except Exception:
                    logger.warning(
                        "SEL audit failed for cli --approval auto-approve", exc_info=True
                    )
                return True

        # Check both YOLO sources: Slack handler (!yolo on) and dashboard UI
        if safety_override().is_active() and _child_grant_eligible:
            return True

        if self.dashboard_state:
            # Check if the parent slot is trusted (not all slots).
            # The parent comes from the authoritative parent session key or
            # an explicit slot_resolver -- never from a guess. When a
            # slot_resolver exists but returns falsy we do NOT fall back to
            # the all-slots rule: if the explicit resolver cannot find the
            # parent, widening trust scope would be unsound.

            def _sel_log(*, caller: str, operation: str, outcome: str, resources: str = "") -> None:
                try:
                    sel().log_api_access(
                        caller=caller,
                        operation=operation,
                        outcome=outcome,
                        resources=resources,
                    )
                except Exception:
                    logger.warning("SEL audit failed for trust check", exc_info=True)

            _safe_title = redact(event.title)

            _parent_slot_key = approval_slot or None

            if _parent_slot_key:
                _ps = (self.dashboard_state._slots or {}).get(_parent_slot_key)
                # The same verdict the slot's own tool approvals take: the
                # human's session flag OR a live SafetyOverride scoped grant,
                # re-checked here per request and never renewed from here.
                _ps_trusted = bool(_ps) and _slot_is_trusted(_ps)
                _ps_via_scope = _ps_trusted and _auto_approve_reason(_ps, False) == "trust_scope"
                if _ps_trusted and not _child_grant_eligible:
                    # Slot IS trusted; the fidelity gate is what blocks
                    # the auto-approve. A distinct audit reason — an
                    # auditor reading "not_trusted" for a trusted slot
                    # would reconstruct the wrong cause.
                    _sel_log(
                        caller=f"slot:{_parent_slot_key}",
                        operation=f"{source}.scoped_trust_blocked_low_fidelity_child",
                        outcome="not_auto_approved",
                        resources=_safe_title,
                    )
                elif _ps_via_scope:
                    _sel_log(
                        caller=f"slot:{_parent_slot_key}",
                        operation=f"{source}.trust_scope_auto_approve",
                        outcome="ok",
                        resources=f"scope:{getattr(_ps, '_trust_scope', '')} {_safe_title}",
                    )
                    return True
                elif _ps_trusted:
                    _sel_log(
                        caller=f"slot:{_parent_slot_key}",
                        operation=f"{source}.scoped_trust_auto_approve",
                        outcome="ok",
                        resources=_safe_title,
                    )
                    return True
                elif _ps:
                    _sel_log(
                        caller=f"slot:{_parent_slot_key}",
                        operation=f"{source}.scoped_trust_not_trusted",
                        outcome="not_auto_approved",
                        resources=_safe_title,
                    )
                else:
                    _sel_log(
                        caller=f"slot:{_parent_slot_key}",
                        operation=f"{source}.scoped_trust_slot_not_found",
                        outcome="not_auto_approved",
                        resources=_safe_title,
                    )
            elif not slot_resolver:
                # No owning slot and no resolver at all. There is NO
                # implicit trust path here: an unowned background command
                # always prompts.
                #
                # An "all open conversations are trusted" rule does not
                # narrow this enough to be safe: for a single-user dashboard
                # with one trusted chat open -- the typical state -- `all()`
                # is trivially satisfied, so a cron's command would be
                # silently auto-approved with no prompt: privilege the job
                # was never granted, justified by trust the user granted to a
                # conversation the job has nothing to do with.
                #
                # Session trust means "auto-approve tools for THIS chat
                # session". An unattended job is not this session, so no
                # amount of session trust should speak for it. Operators who
                # do want a source to run unprompted have the explicit
                # opt-in above (``hooks.auto_approve_sources``), which is
                # consent for that source rather than a side effect of
                # trusting a chat.
                _sel_log(
                    caller=f"source:{source}",
                    operation=f"{source}.unowned_no_implicit_trust",
                    outcome="not_auto_approved",
                    resources=_safe_title,
                )
            else:
                # Resolver existed but failed -- fall through to interactive approval
                _sel_log(
                    caller=f"source:{source}",
                    operation=f"{source}.scoped_trust_fallthrough",
                    outcome="not_auto_approved",
                    resources=_safe_title,
                )

        # Post approval buttons to Slack DM if available
        if self.slack and self._owner_id:
            try:
                # Resolve parent thread context for threaded approval prompts
                thread_ts: str | None = None
                channel: str | None = None
                if parent_session_key and self.sessions:
                    channel = self.sessions.get_channel(parent_session_key)
                    thread_ts = self.sessions.get_thread(parent_session_key)
                    if not thread_ts and channel:
                        # Slack ts format: "{epoch_seconds}.{microseconds}" — pure digits + one dot
                        if re.fullmatch(r"\d+\.\d+", parent_session_key):
                            thread_ts = parent_session_key
                    if not thread_ts:
                        # A threaded cron does not claim its own inherited
                        # thread, so cron:<id> has no recorded outbound
                        # anchor and the ts-shaped fallback above cannot match
                        # it. Resolve the job's own thread_ts so the approval
                        # buttons a threaded run is blocked on thread under
                        # the human's thread instead of posting top-level.
                        thread_ts = self._cron_outbound_thread(parent_session_key)
                is_dm = not channel
                if not channel:
                    channel = await self.slack.open_dm(self._owner_id)
                    thread_ts = None
                from kiro_crew.slack.handler import (
                    _build_approval_blocks,
                    _pending_approvals,
                    _PendingApproval,
                )

                blocks = _build_approval_blocks(event, is_dm=is_dm, source=source)
                title_safe, _ = redact_exfiltration_urls(event.title)
                title_safe, _ = redact_credentials(title_safe)
                fallback = f"🔐 [{source}] Approve: {title_safe}?"
                approval_ts = await self.slack.post_blocks(
                    channel, blocks, fallback, thread_ts  # type: ignore[arg-type]
                )

                # Create a pending approval that the interactive handler can resolve.
                # Use a dummy provider — the actual approve/reject is handled by
                # returning True/False from this callback.
                pending = _PendingApproval(
                    provider=None,  # type: ignore[arg-type]
                    request_id=request_id,
                    session_key=parent_session_key,
                )
                key = f"{channel}:{approval_ts}"
                _pending_approvals[key] = pending

                # Also request via dashboard if available
                dashboard_future = None
                if self.dashboard_state:
                    dashboard_future = asyncio.ensure_future(
                        self.dashboard_state.request_approval(
                            request_id,
                            source,
                            event.title,
                            tool_input=event.tool_input,
                            tool_purpose=event.tool_purpose,
                            slot=approval_slot,
                            is_background=is_background,
                        )
                    )

                    # When dashboard resolves, also resolve the Slack future
                    def _on_dashboard_done(fut: asyncio.Future) -> None:  # type: ignore[type-arg]
                        if fut.cancelled() or fut.exception():
                            return
                        result = "approved" if fut.result() else "rejected"
                        if not pending.future.done():
                            pending.future.set_result(result)

                    dashboard_future.add_done_callback(_on_dashboard_done)

                # Wait for either Slack or dashboard approval. Background
                # sources (no human present) deny-fast on a short window
                # instead of burning the full 2h human window.
                approval_timeout = (
                    DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
                    if is_background
                    else DashboardState._APPROVAL_TIMEOUT
                )
                try:
                    outcome = await asyncio.wait_for(pending.future, timeout=approval_timeout)
                    # Answered on Slack or the dashboard: a loop paused for
                    # approval in this thread resumes.
                    if nudge_key:
                        from kiro_crew.autonudge import release_approval_hold_for

                        release_approval_hold_for(nudge_key, why="an approval was answered")
                except asyncio.TimeoutError:
                    outcome = "rejected"
                    # Nobody answered on either surface -- this branch also
                    # cancels the dashboard future below, so it is the single
                    # authoritative "unanswered" point for a channel-bound
                    # loop's cycle. Record it so the loop stops on its next
                    # wake instead of spending the rest of its cap.
                    if nudge_key:
                        try:
                            svc = self.autonudge_svc
                            if svc is not None:
                                svc.notify_approval_stalled(nudge_key)
                        except Exception:
                            logger.debug("autonudge.notify_approval_stalled failed", exc_info=True)
                finally:
                    _pending_approvals.pop(key, None)
                    # Resolve dashboard approval if Slack responded first
                    if self.dashboard_state:
                        self.dashboard_state.resolve_approval(request_id, outcome == "approved")
                    if dashboard_future and not dashboard_future.done():
                        dashboard_future.cancel()

                # Clean up Slack message
                try:
                    status = "✅ Approved" if outcome == "approved" else "🚫 Rejected"
                    await self.slack.update_message(
                        channel, approval_ts, text=f"🔐 *{title_safe}* — {status}"
                    )
                except Exception:
                    pass

                return outcome == "approved"
            except Exception:
                logger.debug("Slack approval failed, falling back to dashboard", exc_info=True)

        # Fallback: dashboard only
        if self.dashboard_state:
            # The single park-with-nobody-attached point. Every non-human
            # shortcut above has already been evaluated and skipped, and the
            # Slack branch either was not taken or fell through after failing
            # to post — so "nobody received this prompt" is now the only
            # remaining reading, which is what makes the check sound here and
            # nowhere else.
            if raise_when_unreachable and not self._dashboard_client_attached():
                raise SpawnApprovalUnreachable("no dashboard client is connected")
            return await self.dashboard_state.request_approval(
                request_id,
                source,
                event.title,
                tool_input=event.tool_input,
                tool_purpose=event.tool_purpose,
                slot=approval_slot,
                is_background=is_background,
            )
        if _child_lf:
            # No human surface answered and none of the (skipped)
            # shortcuts may speak for an agent-authored request:
            # fail closed.
            return False
        return True  # no UI → auto-approve

    return _approve


# ------------------------------------------------------------------
# Heartbeat tool approval — strict allowlist, no UI prompt
# ------------------------------------------------------------------
async def _heartbeat_approval(
    self: GatewayOrchestrator, event: LLMEvent, _parent_session_key: str = ""
) -> bool:
    """Tool-approval callback for heartbeat sessions.

    Heartbeat runs unattended on a timer — there is no human to click an
    approval button.  We auto-approve only tools whose TRUSTED identity
    ``_is_heartbeat_safe_tool`` accepts (the core heartbeat allowlist and the
    edition's pinned additions, each qualified to its owning server; a
    read-only, git-free shell command) and reject everything else with a
    SEL audit event.

    This is the "Option A" mitigation for the heartbeat security review
    on blanket ``AUTO_APPROVE`` was rejected because polled
    external content (CR comments, ticket bodies) is untrusted; a strict
    name-based allowlist gives heartbeat the tool access it needs while
    keeping the write surface closed to deny-by-default.

    Both approve and deny outcomes emit SEL audit events
    (``log_tool_invocation``) so operators can audit every permission
    decision made on behalf of an unattended heartbeat session.
    """
    title = (event.title or "").strip()
    # Tool titles are LLM-originated input. Redact before any external
    # surface — SEL audit AND dashboard-visible logger warnings —
    # per the security-controls "never trust LLM output" guideline.
    safe_title = redact(title)
    # The audit row names what the decision keyed on. On a trusted event that
    # is the ``server/tool`` identity (a real ``fs_read`` displays as ``Read``,
    # so the title alone would record a different name than the one judged);
    # on an untrusted event the trusted fields prove nothing, so the redacted
    # title -- the possibly forged input that was refused -- stays the label.
    audit_label = safe_title or "<unknown>"
    if event.mcp_identity_trusted and event.tool_name:
        audit_label = (
            f"{event.mcp_server_name}/{event.tool_name}"
            if event.mcp_server_name
            else event.tool_name
        )

    def _audit(outcome: str, *, critical: bool = False, **metadata: str) -> None:
        """Emit a SEL ``log_tool_invocation`` event.

        With ``critical=True`` the write is synchronous and raises on
        failure — callers must decide whether the underlying permission
        decision can proceed without an audit trail. The approve path
        passes ``critical=True`` and treats SEL failure as fatal
        (deny-by-default, preserve audit invariant). The deny path
        tolerates SEL failure because the tool is rejected regardless.
        """
        sel().log_tool_invocation(
            session_key=HEARTBEAT_KEY,
            source="heartbeat",
            agent="kirocrew-heartbeat",
            tool_name=audit_label,
            tool_kind=event.tool_kind,
            outcome=outcome,
            request_id=event.request_id,
            metadata=metadata or None,
            critical=critical,
        )

    if _is_heartbeat_safe_tool(event):
        # A shell command passed on its text; the program names in it still
        # resolve through a PATH that can lead with agent-writable directories,
        # so run the name-grant check every other unattended auto-approve path
        # runs (``llm_helpers._resolve_permission``). A refusal is a deny here,
        # this session's own no-approver fallback.
        if event.is_shell:
            _ng_refusal = await name_grant.refusal_for_event(event)
            if _ng_refusal is not None:
                logger.warning(
                    "declining a heartbeat shell auto-approve: %s; the request "
                    "falls through to deny-by-default",
                    _ng_refusal.log_text,
                )
                name_grant.log_decline(
                    source="heartbeat",
                    session_key=HEARTBEAT_KEY,
                    agent="kirocrew-heartbeat",
                    event=event,
                    refusal=_ng_refusal,
                    tier="heartbeat_safe_tool",
                    sel_factory=sel,
                )
                try:
                    _audit("denied", reason="name_grant_refused")
                except Exception:
                    logger.warning(
                        "SEL audit failed on heartbeat name-grant deny path — "
                        "tool was still rejected",
                        exc_info=True,
                    )
                return False
        # Fail-closed: if SEL is down we cannot record the auto-approve
        # decision, and unattended sessions must not run tools without
        # an auditable permission record. Deny rather than approve
        # silently (security-controls deny-by-default). critical=True
        # forces a synchronous SEL write so a filesystem failure reaches
        # this except instead of being swallowed by the async writer.
        # Offloaded to a worker thread: the critical write does blocking
        # file IO + a Condition.wait() drain, which must not run on the
        # gateway event loop (no-blocking-call-on-event-loop). The
        # exception still propagates through await, preserving fail-closed.
        try:
            await asyncio.to_thread(
                _audit, "auto_approved", critical=True, reason="in_heartbeat_safe_tools"
            )
        except Exception:
            logger.warning(
                "SEL audit failed on heartbeat approve path — "
                "denying tool to preserve audit-or-deny invariant",
                exc_info=True,
            )
            return False
        return True

    # Reject + audit. Logged via the same SEL channel as the interactive
    # approval path so operators can see what got blocked and decide
    # whether to extend the heartbeat allowlist. SEL failure here is
    # tolerated because the tool is denied regardless — the safety
    # property the audit protects (no unaudited tool runs) is preserved.
    # The deny reason splits on the gate's own first two checks: a trusted
    # identity AND a non-empty tool name. A backend whose adapter stamps no
    # ``_meta`` identity yields a cache hit on empty names, which sets the
    # trusted flag with no name behind it, so the flag alone cannot tell
    # that backend apart. Either missing half gets ``no_trusted_identity``:
    # on such a backend every heartbeat tool call lands here, and the
    # operator reads it as a backend property, not a list to extend.
    untrusted = not (event.mcp_identity_trusted and event.tool_name)
    try:
        _audit(
            "denied",
            reason="no_trusted_identity" if untrusted else "not_in_heartbeat_safe_tools",
        )
    except Exception:
        logger.warning(
            "SEL audit failed on heartbeat deny path — " "tool was still rejected",
            exc_info=True,
        )
    if untrusted:
        logger.warning(
            "Heartbeat blocked tool call: %s (this backend published no trusted tool "
            "identity, so every heartbeat tool call is denied on it; heartbeat runs "
            "text-only here)",
            audit_label,
        )
    else:
        logger.warning(
            "Heartbeat blocked tool call: %s (not on the heartbeat allowlist)",
            safe_title or "<unknown>",
        )
    return False
