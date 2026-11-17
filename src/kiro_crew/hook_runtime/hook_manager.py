"""The hook pipeline's front door: ``HookManager``, which runs the message hooks,
consults the tool gate (``judge`` and its keyword form ``on_tool_call``), follows the
``hooks`` config section and resolves the effective deny set for callers outside the
gate.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`. ``judge`` and
``on_tool_call`` are wrapped by ``_fail_closed_on_gate_crash`` here and wrapped again on
the facade after ``compose``: the body a wrapper holds sits in its closure, which
``compose`` does not reach, so only the facade's wrapper runs the body on the facade's
globals.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _GATE_TABLE_PROBLEMS,
        GATE_CRASH_REASON,
        GATE_TABLE_UNSOUND_REASON,
        GATE_TIERS,
        GateFacts,
        HookResult,
        HooksConfig,
        ToolCall,
        ToolHookResult,
        as_spec_denied_grant,
        bounded_judgment,
        current_context,
        hooks_config_from_config_dict,
        logger,
        resolve_denied_notes,
        resolve_effective_denied_regexes,
        sel,
    )


def _fail_closed_on_gate_crash(judge: Any) -> Any:
    """Turn an exception out of the tool gate into a visible security deny.

    The gate parses untrusted command text, and a parser can raise on input
    nobody anticipated (a NUL byte once made the inline-payload lexer raise
    ``SystemError``). An exception that escapes ``HookManager.judge`` (and so
    ``on_tool_call``) reaches each caller's own handling -- some refuse with a
    vague reason, some let the turn fail, and on the dashboard the call was
    reported as aborted by the user. Refusing here, in the one place every
    surface consults, makes the outcome the same everywhere: fail closed, and
    say why.

    ``PlatformCompositionError`` still propagates: it means the host itself is
    mis-composed, which the gate re-raises on purpose so a broken install is
    loud instead of degrading one call at a time. ``functools.wraps`` keeps the
    wrapped signature visible to ``inspect``.
    """

    @functools.wraps(judge)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> ToolHookResult:
        try:
            result: ToolHookResult = judge(self, *args, **kwargs)
            return result
        except Exception as exc:
            from kiro_crew.platform.context import PlatformCompositionError

            if isinstance(exc, PlatformCompositionError):
                raise
            logger.exception("tool gate raised while judging a call; refusing it")
            return ToolHookResult.deny(GATE_CRASH_REASON.format(error=type(exc).__name__))

    return wrapper


class HookManager:
    """Process messages and tool calls through config-driven rules."""

    def __init__(self, config: HooksConfig | None = None):
        self._config = config or HooksConfig()
        self._config_sub: object | None = None

    def reload(self, config: HooksConfig) -> None:
        """Hot-reload hooks config."""
        self._config = config

    def watch_config(self) -> object:
        """Re-read the ``hooks`` section from config.json on every config write.

        Opt-in rather than automatic, because a DERIVED manager must not follow
        config: the heartbeat-scoped manager (``_build_heartbeat_hooks``)
        deliberately drops the user's ``auto_approve_tools`` so
        the heartbeat allowlist (``_heartbeat_approval``) is the sole approval
        authority, and re-reading the
        section would hand that widening straight back. Only the primary
        interactive manager -- the one the gateway builds from config in the first
        place -- calls this; the heartbeat manager is re-derived from it each cycle,
        so it inherits the reload without subscribing.

        The keystone opt-out state is spliced in by
        :func:`hooks_config_from_config_dict`, so a ``config.json`` write never
        reverts a Settings>Security change (and vice versa).

        Idempotent: a repeated call on the same manager is a no-op returning the
        existing subscription, so a caller that cannot easily tell whether a
        given manager already watches config cannot stack a duplicate applier.
        """
        if self._config_sub is not None:
            return self._config_sub
        from kiro_crew.config import live

        self._config_sub = live.subscribe(
            "hooks", callback=self._on_config_change, name="HookManager"
        )
        return self._config_sub

    def _on_config_change(self, change: object) -> None:
        before = self._config
        after = hooks_config_from_config_dict(getattr(change, "new").hooks)
        self.reload(after)

        def _grants(cfg: HooksConfig) -> set[tuple[str, str]]:
            return {("tools", p) for p in cfg.auto_approve_tools} | {
                ("identities", p) for p in cfg.auto_approve_tool_identities
            }

        # A hooks reload can WIDEN what runs without a prompt, and config.json is
        # writable by an auto-approved agent shell, so an approval-set change is
        # SEL-audited the way the channel transports audit an allow-list reload:
        # by COUNT and flag, never by tool name (governance still caps the set).
        added = len(_grants(after) - _grants(before))
        removed = len(_grants(before) - _grants(after))
        flags = {
            "sources": (before.auto_approve_sources != after.auto_approve_sources),
            "subagent_spawn": (
                before.auto_approve_subagent_spawn != after.auto_approve_subagent_spawn
            ),
            "subagent_tools": (
                before.auto_approve_subagent_tools != after.auto_approve_subagent_tools
            ),
            "confirm_git_writes": (before.confirm_git_writes != after.confirm_git_writes),
        }
        flipped = sorted(k for k, v in flags.items() if v)
        # Removing a ``confirm_tools`` pattern widens too: the gate stops asking
        # for the calls it named.
        confirm_added = len(set(after.confirm_tools) - set(before.confirm_tools))
        confirm_removed = len(set(before.confirm_tools) - set(after.confirm_tools))
        confirm_note = (
            f" confirm_added={confirm_added} confirm_removed={confirm_removed}"
            if confirm_added or confirm_removed
            else ""
        )
        if added or removed or flipped or confirm_note:
            logger.warning(
                "hooks: auto-approval set changed via config reload (+%d/-%d tool(s), "
                "flags: %s, confirm: +%d/-%d)",
                added,
                removed,
                ",".join(flipped) or "none",
                confirm_added,
                confirm_removed,
            )
            sel().log_api_access(
                caller="config",
                operation="hook_manager.reconfigure",
                outcome="auto_approve_changed",
                source="hooks",
                resources=(
                    f"added={added} removed={removed} size={len(_grants(after))}"
                    + (f" flags={','.join(flipped)}" if flipped else "")
                    + confirm_note
                ),
            )

    @property
    def auto_approve_subagent_spawn(self) -> bool:
        return self._config.auto_approve_subagent_spawn

    @property
    def auto_approve_subagent_tools(self) -> bool:
        return self._config.auto_approve_subagent_tools

    @property
    def confirm_git_writes(self) -> bool:
        return self._config.confirm_git_writes

    # ── Message hooks ──

    def on_message(self, text: str) -> HookResult:
        """Run message hooks. Returns first match or passthrough."""
        lower = text.lower()

        # Auto-replies (first match wins)
        for ar_hook in self._config.auto_replies:
            if ar_hook.exact:
                if lower == ar_hook.pattern.lower():
                    return HookResult.reply(ar_hook.reply)
            else:
                if ar_hook.pattern.lower() in lower:
                    return HookResult.reply(ar_hook.reply)

        # Transforms (first match wins)
        for tf_hook in self._config.transforms:
            if tf_hook.pattern.lower() in lower:
                modified = text
                if tf_hook.prefix:
                    modified = f"{tf_hook.prefix}\n{modified}"
                if tf_hook.suffix:
                    modified = f"{modified}\n{tf_hook.suffix}"
                return HookResult.modify(modified)

        # Context injection (all matching rules)
        injected: list[str] = []
        for rule in self._config.context_rules:
            if any(t.lower() in lower for t in rule.triggers):
                injected.append(rule.context)
        if injected:
            return HookResult.inject_context("\n\n".join(injected))

        return HookResult.passthrough()

    # ── Tool hooks ──

    @_fail_closed_on_gate_crash
    def judge(
        self,
        call: ToolCall,
        *,
        session_key: str = "",
        agent: str = "",
        app: str = "",
        classifier_only: bool = False,
        trust_reads: bool = False,
        deny_waivers: frozenset[str] = frozenset(),
        pre_tool_hooks_fire: bool = False,
    ) -> ToolHookResult:
        """Whether *call* is auto-approved, denied, or handled normally.

        Walks :data:`GATE_TIERS` in table order and returns the first verdict a tier
        hands back, stamped with that tier's name in ``ToolHookResult.tier`` (a
        per-target tier stamps the rule that refused). No tier decides: ``allow``,
        which falls through to the surface's own approval path.

        What holds for every call, whatever the table holds:

        * every deny tier runs before every grant tier, and the read-only
          classifier runs last -- the table's kinds are monotone, which
          ``gate_tier_problems`` checks when this module loads, so a grant can
          never re-admit something a deny blocked (an unsound table refuses every
          call with :data:`GATE_TABLE_UNSOUND_REASON`);
        * the platform context is read ONCE (:class:`GateFacts`), so a live ceiling
          refresh cannot split one call's verdict across two policy states;
        * exactly one ``approval_decisions`` counter is emitted, because every tier
          returns through ``ToolHookResult``'s counted factories (none under
          :func:`uncounted_gate`);
        * nothing here resolves a program name or touches the filesystem beyond the
          path checks: it is synchronous and called ON the event loop, so the
          name-grant check runs off-loop in the surfaces;
        * a tier that raises turns into a visible security deny
          (``_fail_closed_on_gate_crash``); ``PlatformCompositionError`` propagates.

        ``session_key`` / ``agent`` / ``app`` identify the calling surface so the
        governance ceiling ∩ active-profile can be resolved and a tool/MCP call
        denied even when the kiro agent config granted it (the governance headline
        behavior). They default to ``""``; a caller that supplies identity opts into
        per-surface governance.

        ``classifier_only`` drops the GRANT tiers -- the app-own-server rule and the
        operator's ``auto_approve_tools`` globs -- so the only auto-approve left is
        the read-only classifier's, which carries ``read_only=True`` on the result. A
        grant vouches for the caller and says nothing about the call's effect;
        ``ToolApprovalPolicy.READ_ONLY`` (the side chat) has no approver behind it,
        so a grant honoured there would execute a mutating tool. Every deny tier and
        governance still run, and a call a grant would have approved is classified
        on its own merits instead of being refused outright (a grant that shadows a
        read costs nothing, a grant that shadows a write approves nothing). Default
        ``False``: a caller with an interactive approver keeps the grants.

        ``trust_reads`` says the surface's Reads approval mode is on for this call
        (the dashboard slot's ``_trust_reads``). It unlocks exactly one more
        read-only proof in the classifier: an MCP call whose host-trusted
        ``(server, tool)`` pair is in :data:`_FIRST_PARTY_READ_ONLY_MCP_TOOLS`,
        Kiro Crew's own read-only tools. Nothing else reads it -- not a deny tier,
        not governance, not a grant -- so it can only turn a prompt into a
        ``read_only`` auto-approve for one of those tools, never the reverse, and
        an operator ``confirm_tools`` match still asks first. Default ``False``:
        every other mode and surface judges an MCP call as before.

        ``deny_waivers`` are the refusal keys a person overrode on the dashboard
        approval card (``docs/request-for-change/rfc-deny-session-override.md``).
        Only the ``deny-rules`` tier reads them, after dropping every
        governance-pinned key, and each one it uses is audited or refuses; every
        other tier judges the call exactly as without them. Default empty: only
        the dashboard's override path and the transport floor it approves through
        pass any.

        ``pre_tool_hooks_fire`` says the surface runs the PreToolUse hooks on this
        request whatever the gate answers (the chat, subagent and task-runner turn
        loops). Only the ``batch-guard-grant`` row reads it, to answer
        ``ToolCall.spec_hook_held``; ``False`` keeps those calls asking.
        """
        if _GATE_TABLE_PROBLEMS:
            return ToolHookResult.deny(GATE_TABLE_UNSOUND_REASON)
        facts = GateFacts(
            self,
            call,
            session_key=session_key,
            agent=agent,
            app=app,
            classifier_only=classifier_only,
            trust_reads=trust_reads,
            deny_waivers=frozenset(deny_waivers),
            pre_tool_hooks_fire=pre_tool_hooks_fire,
        )
        # One shared resolver budget for every path this call names; it binds only
        # off the event loop, where the loop's own allowance does not apply.
        with bounded_judgment():
            for tier in GATE_TIERS:
                verdict = tier.judge(facts, tier)
                if verdict is not None:
                    if not verdict.tier:
                        verdict.tier = tier.name
                    return verdict
        return ToolHookResult.allow(facts.ask_note)

    @_fail_closed_on_gate_crash
    def on_tool_call(
        self,
        tool_name: str,
        *,
        session_key: str = "",
        agent: str = "",
        app: str = "",
        tool_kind: str = "",
        raw_params: dict | None = None,
        diff_path: str = "",
        command: str | None = None,
        is_shell: bool = False,
        mcp_server_name: str = "",
        mcp_tool_name: str = "",
        mcp_identity_trusted: bool = False,
        mcp_identity_unreadable: bool = False,
        mcp_identity_unreadable_reason: str = "",
        kas_builtin_ids: bool = False,
        spec_granted_builtins: frozenset[str] = frozenset(),
        spec_hook_held: frozenset[str] = frozenset(),
        spec_granted_commands: tuple[str, ...] = (),
        spec_shell_allowlist: tuple[str, ...] | None = None,
        spec_write_allowlist: tuple[str, ...] | None = None,
        spec_write_cwd: str = "",
        spec_denied_grant: Any = None,
        spec_unjudged_denies: tuple[dict[str, Any] | str, ...] = (),
        spec_shell_readonly: bool = False,
        spec_granted_paths: tuple[str, ...] = (),
        spec_granted_read_paths: tuple[str, ...] = (),
        harness_builtin_tool: str = "",
        deepseek_tool_name: str = "",
        spawn_target: str = "",
        resolved_agent: str = "",
        classifier_only: bool = False,
        trust_reads: bool = False,
        deny_waivers: frozenset[str] = frozenset(),
        pre_tool_hooks_fire: bool = False,
    ) -> ToolHookResult:
        """:meth:`judge`, for a caller that holds the call as keywords.

        ``tool_name`` is the call's display title; every other keyword is the
        :class:`ToolCall` field of the same meaning (``tool_kind`` -> ``kind``,
        ``mcp_server_name`` -> ``mcp_server``, ``mcp_tool_name`` -> ``mcp_tool``,
        ``mcp_identity_trusted`` -> ``identity_trusted``, ``mcp_identity_unreadable``
        -> ``identity_unreadable``, ``mcp_identity_unreadable_reason`` ->
        ``identity_unreadable_reason``) or a surface argument of
        :meth:`judge`. A dispatcher holding a permission event passes
        ``**hook_gate_kwargs(event)``, so an enforcement-relevant event field is
        threaded once. Each field's security meaning is documented on
        :class:`ToolCall`; what the gate does with them, on :meth:`judge` and in
        :data:`GATE_TIERS`. Wrapped like :meth:`judge`, so a call that cannot even
        bind (a stray keyword, a surface argument passed positionally) is refused as
        a gate crash rather than raised into the caller's own handling.
        """
        return self.judge(
            ToolCall(
                title=tool_name,
                kind=tool_kind,
                raw_params=raw_params,
                diff_path=diff_path,
                command=command,
                is_shell=is_shell,
                mcp_server=mcp_server_name,
                mcp_tool=mcp_tool_name,
                identity_trusted=mcp_identity_trusted,
                identity_unreadable=mcp_identity_unreadable,
                identity_unreadable_reason=mcp_identity_unreadable_reason,
                kas_builtin_ids=kas_builtin_ids,
                spec_granted_builtins=frozenset(spec_granted_builtins),
                spec_hook_held=frozenset(spec_hook_held),
                spec_granted_commands=tuple(spec_granted_commands),
                spec_shell_allowlist=(
                    None if spec_shell_allowlist is None else tuple(spec_shell_allowlist)
                ),
                spec_write_allowlist=(
                    None if spec_write_allowlist is None else tuple(spec_write_allowlist)
                ),
                spec_write_cwd=spec_write_cwd,
                spec_denied_grant=as_spec_denied_grant(spec_denied_grant),
                spec_unjudged_denies=tuple(spec_unjudged_denies),
                spec_shell_readonly=spec_shell_readonly is True,
                spec_granted_paths=tuple(spec_granted_paths),
                spec_granted_read_paths=tuple(spec_granted_read_paths),
                harness_builtin_tool=harness_builtin_tool,
                deepseek_tool_name=deepseek_tool_name,
                spawn_target=spawn_target,
                resolved_agent=resolved_agent,
            ),
            session_key=session_key,
            agent=agent,
            app=app,
            classifier_only=classifier_only,
            trust_reads=trust_reads,
            deny_waivers=deny_waivers,
            pre_tool_hooks_fire=pre_tool_hooks_fire,
        )

    def _effective_denied(self, ctx: object) -> list[str]:
        """Resolve the effective regex-tier denied set for this call.

        Combines the still-enabled built-in rules (after applying
        ``disable_all`` / ``disabled_ids``, with governance-pinned rule ids
        force-re-added) with the user's own enabled ``user_added`` regexes. The
        result is passed to ``authority.is_denied(..., denied_regexes=)``; the
        glob-tier ``auto_deny_tools`` still travel through ``extra_patterns``.
        """
        return resolve_effective_denied_regexes(self._config, ctx)

    def _denied_notes(self) -> dict[str, str]:
        """Operator notes for the user patterns in the effective denied set.

        Passed alongside ``denied_regexes`` so a refusal can carry the operator's
        own remediation line. Empty dict when nothing is annotated, which is the
        pre-existing behavior (reason = the bare pattern).
        """
        return resolve_denied_notes(self._config)

    def operator_denied_regexes(self) -> list[str]:
        """The operator's own enabled ``user_added`` regexes, and nothing else.

        The part of :meth:`effective_denied_regexes` the operator authored: no
        shipped built-in and no governance pin. Always a list, never ``None``,
        because ``security.is_denied`` reads ``None`` as "every built-in". These
        are the regex-tier patterns the file-search synthesized target meets, and
        they judge the title kiro-cli sent when ``untruncated_shell_title`` rebuilt
        it -- in the gate's deny-rules tier and in
        ``llm_helpers._resolve_permission`` -- where a shipped rule only misreads
        the cut but an operator's rule may have been written against exactly that
        text.
        """
        return [p.pattern for p in self._config.denied_commands_user_added if p.enabled]

    def effective_denied_regexes(self, *, include_governance_pins: bool = True) -> list[str]:
        """Public accessor for the effective regex-tier denied set.

        Resolves the platform context itself, so callers outside the tool-call
        gate (e.g. ``llm_helpers._resolve_permission`` on the cron / Slack /
        workflow / heartbeat surfaces) can honor the SAME user opt-out +
        governance-pin state that ``on_tool_call`` enforces, instead of failing
        closed to all built-ins and re-introducing "disabled but still blocked".

        Pass ``include_governance_pins=False`` only to CLASSIFY a deny that has
        already been decided by the pinned set — never to decide one. See
        ``resolve_effective_denied_regexes``.
        """
        return resolve_effective_denied_regexes(
            self._config, current_context(), include_governance_pins=include_governance_pins
        )
