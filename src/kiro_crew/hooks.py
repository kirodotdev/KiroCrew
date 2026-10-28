"""Config-driven hook system for KiroCrew's message pipeline.

Hooks intercept messages and tool calls based on rules in config.json.
Supports declarative rules and executable script hooks with timeout/sandboxing.
"""

from __future__ import annotations

# Imports the owners read rather than this module's own body: every function they
# define runs on these globals (see :mod:`kiro_crew.hook_runtime`), so a name dropped
# here is a NameError on the moved line, not an unused import. Tests swap ``hooks.os``
# and ``hooks.sys`` wholesale to simulate Windows and macOS, which works only while the
# whole call graph reads them from here.
import asyncio  # noqa: F401 - read by the owners
import copy  # noqa: F401 - read by the owners
import errno  # noqa: F401 - read by the owners
import fnmatch  # noqa: F401 - read by the owners
import functools
import hashlib as _hashlib  # noqa: F401 - read by the owners
import json  # noqa: F401 - read by the owners
import logging
import os
import re
import stat as _stat  # noqa: F401 - read by the owners
import sys  # noqa: F401 - read by the owners
import tempfile  # noqa: F401 - read by the owners
import threading  # noqa: F401 - read by the owners
import time  # noqa: F401 - read by the owners
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence  # noqa: F401
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from dataclasses import replace as dataclasses_replace  # noqa: F401 - owners read it
from pathlib import Path
from typing import Any, Literal, NamedTuple

from kiro_crew import jsonl_util, pinned_fs, platform_compat, security, webhooks  # noqa: F401
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew._facade_compose import rebind as _facade_rebind

# The xattr ACL-carry policy is shared with atomic_write.atomic_write: both
# install a fresh inode and must reproduce the source's access controls or
# refuse. atomic_write is a leaf module (imported here transitively already via
# platform_compat), so importing these from there keeps one spelling of the
# policy without a cycle.
from kiro_crew.atomic_write import (  # noqa: F401 - read by the pinned_writes owner
    _XATTR_UNSUPPORTED_ERRNOS,
    _is_access_control_xattr,
    _should_carry_xattr,
)
from kiro_crew.config import paths as _config_paths

# ``_coerce_bool`` reads a hand-edited bool without the ``bool("false")`` trap;
# one copy, owned with the other config field coercers. Re-exported here because
# ``dashboard/handlers/security.py`` imports it from this module.
from kiro_crew.config.fields import _coerce_bool  # noqa: F401

# The owners the hook subsystem's rules live in, and the names they define. Every
# one is re-exported here unchanged: ``kiro_crew.hooks`` is the import path and the
# patch surface for all of them, and ``compose`` at the foot of this module runs each
# of their functions on these globals. See :mod:`kiro_crew.hook_runtime`.
from kiro_crew.hook_runtime import denied_commands as _owner_denied_commands
from kiro_crew.hook_runtime import descriptor_identity as _owner_descriptor_identity
from kiro_crew.hook_runtime import gate_tiers as _owner_gate_tiers
from kiro_crew.hook_runtime import governance_gate as _owner_governance_gate
from kiro_crew.hook_runtime import hook_dispatch as _owner_hook_dispatch
from kiro_crew.hook_runtime import hook_types as _owner_hook_types
from kiro_crew.hook_runtime import internal_reads as _owner_internal_reads
from kiro_crew.hook_runtime import pinned_writes as _owner_pinned_writes
from kiro_crew.hook_runtime import safe_reads as _owner_safe_reads
from kiro_crew.hook_runtime import script_hooks as _owner_script_hooks
from kiro_crew.hook_runtime import script_validation as _owner_script_validation
from kiro_crew.hook_runtime import search_targets as _owner_search_targets
from kiro_crew.hook_runtime import stream_caps as _owner_stream_caps
from kiro_crew.hook_runtime import tool_identity as _owner_tool_identity
from kiro_crew.hook_runtime import windows_paths as _owner_windows_paths
from kiro_crew.hook_runtime.denied_commands import (  # noqa: F401
    _governance_pinned_command_ids,
    effective_denied_regexes_from_config,
    hooks_config_from_config_dict,
    load_denied_commands_state,
    resolve_denied_notes,
    resolve_effective_denied_regexes,
    splice_denied_commands,
)
from kiro_crew.hook_runtime.descriptor_identity import (  # noqa: F401
    _darwin_case_alias_matches,
    _hardlink_alias_matches,
    _opened_file_matches_validated_path,
    _opened_path_within_root,
    _validated_name_holds,
)

# compose() runs the gate tiers on this module's globals, so every helper a tier
# calls (``_operator_confirms`` for the operator-grants tier included) must be
# bound here, not only in gate_tiers.
from kiro_crew.hook_runtime.gate_tiers import (  # noqa: F401
    GateFacts,
    _operator_confirms,
    _rule_exfil,
    _rule_sensitive_bash,
    _rule_sensitive_path,
    _tier_app_own_server,
    _tier_deny_rules,
    _tier_governance,
    _tier_mcp_auto_deny,
    _tier_operator_grants,
    _tier_param_paths,
    _tier_read_only,
    _tier_search_target,
    _tier_targets,
    _tier_unverifiable_shell,
    _tier_write_protected,
    gate_tier_problems,
    shell_rules,
)
from kiro_crew.hook_runtime.governance_gate import (  # noqa: F401
    _audit_governance,
    _audit_governance_hook_decision,
    _governance_denial,
    _script_hooks_capability_denied,
    _spawn_policy_denial,
)
from kiro_crew.hook_runtime.hook_dispatch import (  # noqa: F401
    fire_tool_hooks,
    get_global_hook_store,
    permission_pre_tool_block,
    persisted_hook_store,
    pre_tool_match_names,
    set_global_hook_store,
)
from kiro_crew.hook_runtime.hook_types import (  # noqa: F401
    AutoReplyHook,
    ContextRule,
    HookResult,
    HooksConfig,
    ToolHookResult,
    TransformHook,
    UserDeniedPattern,
)
from kiro_crew.hook_runtime.internal_reads import (  # noqa: F401
    _emit_internal_read_audit,
    emit_internal_read_audit,
    register_internal_read_path,
    safe_read_file_internal,
)
from kiro_crew.hook_runtime.pinned_writes import (  # noqa: F401
    _pinned_replace,
    safe_write_file_nolink,
    verified_replace_file_nolink,
)
from kiro_crew.hook_runtime.safe_reads import (  # noqa: F401
    PathDecline,
    safe_copy_file_nolink,
    safe_file_identity,
    safe_read_file,
    safe_read_file_bytes,
    safe_read_file_bytes_nolink,
    safe_read_file_bytes_with_identity,
    safe_read_prefix,
    screen_file_path,
    screen_windows_linked_ancestors,
    stat_identity,
    validate_file_path,
)
from kiro_crew.hook_runtime.script_hooks import (  # noqa: F401
    ScriptHookResult,
    ScriptHookStore,
    _hook_subprocess_env,
    run_script_hook,
)
from kiro_crew.hook_runtime.script_validation import (  # noqa: F401
    _normalize_hook_timeout,
    validate_hook_fields,
)
from kiro_crew.hook_runtime.search_targets import (  # noqa: F401
    _encode_search_field,
    _expand_home_vars,
    _is_search_shaped,
    _normalize_search_path,
    _search_deny_target,
)
from kiro_crew.hook_runtime.stream_caps import (  # noqa: F401
    _communicate_capped,
    _decode_capped,
    _read_capped_stream,
)
from kiro_crew.hook_runtime.tool_identity import (  # noqa: F401
    ToolCall,
    _app_owns_mcp_server,
    _builtin_app_for_agent,
    _context_matches,
    _has_global_inline_flags,
    _host_builtin_alias,
    _is_declared_builtin_mcp_server,
    _is_deepseek_read_only_builtin,
    _is_first_party_app,
    _is_first_party_read_only_mcp_tool,
    _is_harness_read_only_builtin,
    _is_host_read_only_builtin,
    _normalize_tool_name,
    _note_title_only_grant_pattern,
    _tool_matches,
    event_is_spawn_run,
    hook_gate_kwargs,
    identity_grant_covers_child,
    mcp_identity_ref,
    set_builtin_app_agents,
    set_builtin_app_mcp_servers,
    set_builtin_app_names,
    title_is_trusted_mcp_identity,
    untruncated_shell_title,
)
from kiro_crew.hook_runtime.windows_paths import (  # noqa: F401
    _fold_extended_length_local,
    _is_representable_path,
    _normalize_windows_link_target,
    is_unc_shape,
    unc_probe_allowed,
)

# Canonical home of the descriptor-path primitive (Windows fail-closed branch
# included). The module-local alias is load-bearing: the gate helpers below
# call it through this module's global, which the seam tests monkeypatch to
# simulate a host where a descriptor's path cannot be read.
from kiro_crew.pinned_fs import fd_real_path as _fd_real_path  # noqa: F401
from kiro_crew.platform import current_context, redact_via_context
from kiro_crew.platform.governance import (
    CU_CLASS_OBSERVE,
    computer_use_action_classes,
    computer_use_action_from_title,
)

# The bounded, depth-aware target-path walk lives one layer below both this
# module and governance (see the re-export note where the keystone consumers
# are defined). Imported here so ``hooks.TARGET_PATH_KEYS`` / ``hooks.TargetPaths``
# / ``hooks.target_paths`` (and the work caps) stay importable at their historic
# names; hooks keeps its HARD-DENY reading of ``TargetPaths.truncated``.
from kiro_crew.platform.tool_names import (  # noqa: F401  (read by hook_runtime owners)
    DEEPSEEK_READ_ONLY_TOOLS,
    KIRO_TOOL_ALIASES,
    deepseek_policy_alias,
    policy_alias_split,
    policy_aliases,
)
from kiro_crew.platform.tool_paths import (  # noqa: F401  (re-exported for callers)
    _TARGET_PATH_MAX_NODES,
    _TARGET_PATH_MAX_PATHS,
    TARGET_PATH_KEYS,
    TargetPaths,
    edit_target_candidates,
    is_edit_call,
    target_paths,
)
from kiro_crew.security import (  # noqa: F401 - the path owners read these
    audit_bash_exfiltration,
    is_sensitive_bash_command,
    is_sensitive_path,
    is_sensitive_write_path,
    is_unverifiable_path_refusal,
    sensitive_path_refusal,
    sensitive_write_path_refusal,
)
from kiro_crew.security.readonly_bash import (  # noqa: F401 - the owners read them
    command_invokes_git,
    is_read_only_bash,
)
from kiro_crew.sel import sel
from kiro_crew.session_directive import CORE_MCP_SERVER  # noqa: F401
from kiro_crew.validation import _bounded_pattern_search  # noqa: F401

logger = logging.getLogger(__name__)


# ── Hook Results ──

# Message hook action constants (backward compat — prefer direct string comparison)
HOOK_PASSTHROUGH = "passthrough"
HOOK_REPLY = "reply"
HOOK_MODIFY = "modify"
HOOK_INJECT_CONTEXT = "inject_context"

# Tool hook action constants
TOOL_ALLOW = "allow"
TOOL_AUTO_APPROVE = "auto_approve"
TOOL_DENY = "deny"

# Script hook events (aligned with Kiro CLI)
HOOK_EVENT_AGENT_SPAWN = "AgentSpawn"
HOOK_EVENT_USER_PROMPT_SUBMIT = "UserPromptSubmit"
HOOK_EVENT_PRE_TOOL_USE = "PreToolUse"
HOOK_EVENT_POST_TOOL_USE = "PostToolUse"
HOOK_EVENT_STOP = "Stop"

#: The events the gateway itself fires. ``ScriptHookStore.fire`` has a call site
#: for each one, and ``steering-and-hooks.md`` documents their exit-code
#: contract. Membership here is what makes an event a *lifecycle* event.
HOOK_EVENTS = (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_STOP,
)

# Triggers a Kiro Agent session owns that the gateway has no lifecycle call site
# for. They are authorable and persisted, and NO EVENT FIRES ANY OF THEM: no call
# site fires one and no other reader consumes this tuple. That is why they are a
# separate tuple rather than new members of ``HOOK_EVENTS`` -- an event in that
# tuple carries a promise that something calls ``fire`` for it, and these carry
# none.
#
# "No event fires them" is not "the command cannot run": the dashboard's Test
# endpoint runs a STORED hook's command on demand and never consults this tuple
# (``handlers/hooks.py`` ``api_hook_test`` -> ``run_script_hook``), so Test works on
# one of these exactly as it does on a fired event.
#
# They are not equidistant from running, and a reader planning the delivery side
# needs the difference. A Kiro Agent requests hooks by trigger name over ACP from
# a fixed set of seven (``acp/kas_wire.py``'s ``ACP_HOOK_TRIGGERS``), and only
# ``preTaskExecution`` and ``postTaskExecution`` are in it; the file and manual
# triggers are absent, so a Kiro Agent does not ask for those four at all today.
#
# Where the six names come from, since no call site here fires them: each is the
# PascalCase rendering of a trigger name Kiro's own hook schema carries, so the
# delivery round maps a documented name rather than inventing one. Kiro documents
# both spellings of each -- the ``when.type`` name a legacy hook file uses, which
# is also the ACP spelling for the two above, and the standalone v1 hook-file
# trigger -- at kiro.dev/docs/ide/whats-new-v1/hooks:
#
#   preTaskExecution  -> PreTaskExec        postTaskExecution -> PostTaskExec
#   fileCreated       -> PostFileCreate     fileEdited        -> PostFileSave
#   fileDeleted       -> PostFileDelete     userTriggered     -> (none)
#
# Two consequences for the delivery round. It has two vocabularies to map, not
# one, and the names here match the left column. And the manual trigger is the
# furthest from arriving of the six: it has no v1 equivalent at all, so an
# existing manual hook stays runnable as a legacy one while a new one cannot be
# authored in that schema -- the Test button is its whole run path here.
HOOK_EVENT_PRE_TASK_EXECUTION = "PreTaskExecution"
HOOK_EVENT_POST_TASK_EXECUTION = "PostTaskExecution"
HOOK_EVENT_FILE_CREATED = "FileCreated"
HOOK_EVENT_FILE_EDITED = "FileEdited"
HOOK_EVENT_FILE_DELETED = "FileDeleted"
HOOK_EVENT_USER_TRIGGERED = "UserTriggered"

HOOK_EVENTS_KAS_ONLY = (
    HOOK_EVENT_PRE_TASK_EXECUTION,
    HOOK_EVENT_POST_TASK_EXECUTION,
    HOOK_EVENT_FILE_CREATED,
    HOOK_EVENT_FILE_EDITED,
    HOOK_EVENT_FILE_DELETED,
    HOOK_EVENT_USER_TRIGGERED,
)

#: The subset a Kiro Agent session actually asks its client for. It requests
#: hooks by trigger name from a fixed set of seven, and only these two of the six
#: are in it -- so these wait on Kiro Crew answering that request, while the file
#: and manual triggers are not asked for at all. The dashboard marks the two
#: groups differently because the distance to running is different, and it reads
#: the split from here rather than restating it in copy.
HOOK_EVENTS_AGENT_REQUESTED = (
    HOOK_EVENT_PRE_TASK_EXECUTION,
    HOOK_EVENT_POST_TASK_EXECUTION,
)

#: Every event a hook may be authored against and persisted under. This is the
#: authoring vocabulary -- the dashboard form's options, the create/update
#: schemas, and the store's own load and save gates all read this set, so an
#: event absent from it is refused at authoring time and dropped on reload.
HOOK_EVENTS_ALL = HOOK_EVENTS + HOOK_EVENTS_KAS_ONLY

#: Every ``matcher_mode`` a script hook may be authored with; ``glob`` is the
#: default. ``validate_hook_fields`` refuses any other value at create and update,
#: as it refuses an unknown event, and ``ScriptHook.from_dict`` loads one already
#: on disk as ``glob``. The Hooks page's ``MATCHER_MODES`` and the create/update
#: schemas' allow-lists are pinned to this tuple by test.
HOOK_MATCHER_MODES = ("glob", "regex", "contains")


#: Set by :func:`uncounted_gate`; read by ``ToolHookResult._count`` and
#: ``_audit_governance``.
_GATE_UNCOUNTED: ContextVar[bool] = ContextVar("kirocrew_gate_uncounted", default=False)


@contextmanager
def uncounted_gate():
    """Consult the gate without emitting the approval-decision counter.

    For a second consultation of a request whose first one was already counted
    (the ACP transport's permission floor). The verdict is unaffected. The
    governance tier writes no ``governance_decision`` audit row either: this
    consultation carries no caller identity and its caller discards a policy
    deny, so a row here would record a denial for a call that ran. The
    consumer's own identity-bearing consultation writes that row.
    """
    token = _GATE_UNCOUNTED.set(True)
    try:
        yield
    finally:
        _GATE_UNCOUNTED.reset(token)


#: The reason a call is refused when the gate itself raised while judging it.
#: Says plainly that the refusal is a gate defect, not a rule the call broke and
#: not a user action -- without it the host surfaces kiro-cli's generic
#: "User denied tool execution" and the model concludes the user cancelled.
GATE_CRASH_REASON = (
    "Blocked: the safety check crashed while judging this call ({error}), so the "
    "call was refused and nothing ran. This is a Kiro Crew bug, not a policy rule "
    "and not a user action."
)


#: The reason every call is refused when ``GATE_TIERS`` is not a sound gate table
#: (``_GATE_TABLE_PROBLEMS``). Like :data:`GATE_CRASH_REASON`, it says the refusal is
#: a gate defect rather than a rule the call broke or a user action.
GATE_TABLE_UNSOUND_REASON = (
    "Blocked: the safety check's tier table failed its own consistency check, so every "
    "call is refused and nothing ran. This is a Kiro Crew bug, not a policy rule and not "
    "a user action."
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


# ── Gate table types ──

#: What a ``GATE_TIERS`` row may do, in the only order rows may run: a security
#: deny, a policy deny (``ToolHookResult.deny_policy``), a grant (whose body
#: steps aside under ``classifier_only``), and the read-only classifier.
GateTierKind = Literal["deny", "deny_policy", "grant", "classify"]
_GATE_TIER_KINDS: tuple[str, ...] = ("deny", "deny_policy", "grant", "classify")


class GateRule(NamedTuple):
    """One check a per-target tier applies to each security target.

    ``check`` returns a refusal reason or ``None``. ``shell_rule`` names the
    ``kiro_crew.security`` attribute the check applies to a shell command line, for
    the rules :data:`SHELL_DENY_TIERS` lists; ``""`` for a rule that is not one.
    """

    name: str
    check: Callable[[GateFacts, str], str | None]
    shell_rule: str = ""


class GateTier(NamedTuple):
    """One row of the tool gate, run by ``HookManager.judge`` in table order.

    ``judge`` is handed the call's :class:`GateFacts` and its own row, and returns a
    verdict through ``ToolHookResult``'s counted factories or ``None`` to let the
    next row decide. ``rules`` makes a per-target tier: every rule against every
    security target, target-major. ``shell_rule`` is as on :class:`GateRule`, for a
    tier that is itself one shell check.
    """

    name: str
    kind: GateTierKind
    judge: Callable[[GateFacts, GateTier], ToolHookResult | None]
    shell_rule: str = ""
    rules: tuple[GateRule, ...] = ()


_BUNDLED_AUTO_APPROVE_TOOLS: list[str] = []


# ── Spawn auto-approve identity: ``event_is_spawn_run`` in hook_runtime/tool_identity.py ──


# ── HookManager ──


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
        # A hooks reload can WIDEN what runs without a prompt, and config.json is
        # writable by an auto-approved agent shell, so an approval-set change is
        # SEL-audited the way the channel transports audit an allow-list reload:
        # by COUNT and flag, never by tool name (governance still caps the set).
        added = len(set(after.auto_approve_tools) - set(before.auto_approve_tools))
        removed = len(set(before.auto_approve_tools) - set(after.auto_approve_tools))
        flags = {
            "sources": (before.auto_approve_sources != after.auto_approve_sources),
            "subagent_spawn": (
                before.auto_approve_subagent_spawn != after.auto_approve_subagent_spawn
            ),
            "subagent_tools": (
                before.auto_approve_subagent_tools != after.auto_approve_subagent_tools
            ),
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
                    f"added={added} removed={removed} size={len(after.auto_approve_tools)}"
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
        )
        for tier in GATE_TIERS:
            verdict = tier.judge(facts, tier)
            if verdict is not None:
                if not verdict.tier:
                    verdict.tier = tier.name
                return verdict
        return ToolHookResult.allow()

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
        kas_builtin_ids: bool = False,
        harness_builtin_tool: str = "",
        deepseek_tool_name: str = "",
        spawn_target: str = "",
        resolved_agent: str = "",
        classifier_only: bool = False,
        trust_reads: bool = False,
    ) -> ToolHookResult:
        """:meth:`judge`, for a caller that holds the call as keywords.

        ``tool_name`` is the call's display title; every other keyword is the
        :class:`ToolCall` field of the same meaning (``tool_kind`` -> ``kind``,
        ``mcp_server_name`` -> ``mcp_server``, ``mcp_tool_name`` -> ``mcp_tool``,
        ``mcp_identity_trusted`` -> ``identity_trusted``, ``mcp_identity_unreadable``
        -> ``identity_unreadable``) or a surface argument of
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
                kas_builtin_ids=kas_builtin_ids,
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


# ACP semantic tool kinds treated as read-only for the non-shell auto-approve
# branch. Deliberately minimal — excludes "search"/"edit"/"execute"/"delete"/
# "move"; add conservatively (auto-approving trusts an agent-supplied field).
_READ_ONLY_TOOL_KINDS: frozenset[str] = frozenset({"read", "fetch"})

# Built-in tools the HOST knows to be read-only, keyed by the non-model-authored
# ``_meta.kiro.toolName`` identity kiro-cli stamps on every tool call it serves.
# This is the ONLY non-shell read-only proof ``on_tool_call`` accepts under
# ``classifier_only`` (``ToolApprovalPolicy.READ_ONLY``, the side chat): the ACP
# ``kind`` and the title are agent-influenced and may narrow but never prove.
# Every name here maps to a read scope in ``governance.BUILTIN_TOOL_SCOPES``
# (``filesystem.read`` / ``network.egress``) and never to ``filesystem.write``
# or ``commands`` — ``test_host_read_only_builtins_map_only_to_read_scopes``
# (``test/test_hooks_trusted_identity.py``) pins that, so a write-capable built-in (``code``,
# ``fs_write``) cannot join by mistake. Add conservatively: an entry here runs
# unattended on a surface with no approver.
_HOST_READ_ONLY_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {"fs_read", "glob", "grep", "web_fetch", "web_search"}
)

# Kiro Crew's OWN read-only MCP tools, keyed by the ``(server, tool)`` pair the
# transport stamped (``_meta.kiro.mcpServerName`` / ``toolName``), so the same
# tool name on any other server matches nothing. The read-only tier reads this
# ONLY for a surface that passed ``trust_reads=True`` to ``judge`` (the
# dashboard's Reads approval mode); under every other mode and on every other
# surface an MCP call is as unprovable as before. Each entry is one of the core
# server's tools whose handler (``mcp_tools/``) reads the user's own Kiro Crew
# data and calls nothing beyond this gateway: ``memory_recall``
# (``GET /api/memory/recall``), ``search_chat_history`` / ``get_chat_session``
# (the transcript store), ``skill_search`` (the installed-skill catalog) and
# ``local_knowledge_search`` (the local knowledge index; its embedder is
# in-process). "Read-only" here is a claim about the USER'S world, which is
# what the approval card guards: the only writes on these paths are the
# gateway's own housekeeping of its derived indexes (a lazy build, a schema
# migration or a repair of the skill, history, knowledge or notebook index --
# ``SkillSearchIndex.sync``, ``history_index._ensure_open``, the knowledge
# store's migrate step, ``_repair_notebook_index``), which the dashboard pages
# that read the same data run with no card at all; none writes a user file,
# memory content or anything outside the gateway's data directory, and none
# spends model tokens. ``autonudge_judge`` keeps NO MCP allowlist for its own
# reason -- it labels whether a turn changed anything at all, where an index
# build counts -- and that reason does not transfer here. ``list_sessions`` is
# deliberately absent: its ``summarize`` argument asks the gateway to spend
# model tokens, and this set is matched on identity, never on arguments. Closed
# and first-party: no config, app or edition adds to it.
# ``test_first_party_read_only_mcp_tools_are_core_reads``
# (``test/test_hooks_trusted_identity.py``) pins every entry to a tool the core
# server really serves and to none of its write tools.
_FIRST_PARTY_READ_ONLY_MCP_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {
        (CORE_MCP_SERVER, "memory_recall"),
        (CORE_MCP_SERVER, "search_chat_history"),
        (CORE_MCP_SERVER, "get_chat_session"),
        (CORE_MCP_SERVER, "skill_search"),
        (CORE_MCP_SERVER, "local_knowledge_search"),
    }
)


# Semantic kinds known to mutate/execute. DOCUMENTATION ONLY — the gate does not
# branch on this set, and must not start: `tool_kind` arrives verbatim from the ACP
# `kind` field, so any denylist of mutating kinds is incomplete by construction
# (`kind="other"` is a real value that a denylist auto-approves). The
# auto-approve decision is an ALLOW-list on `_READ_ONLY_TOOL_KINDS` instead, and
# every other non-empty kind falls through to interactive approval.
#
# Kept because it records which kinds we have actually seen mutate — useful when
# judging whether a new kind belongs in the read-only set — and because deleting a
# named constant is how the next reader loses that context.
_WRITE_TOOL_KINDS: frozenset[str] = frozenset(
    {"edit", "execute", "delete", "move", "write", "create"}
)


# Canonical ``<app>:<server>`` names DECLARED in shipped builtin manifests,
# populated ONCE at gateway boot via ``set_builtin_app_mcp_servers`` (see the
# dashboard startup). Parallel to ``_BUILTIN_APP_NAMES`` and kept as a plain
# module global for the same reason — the PreToolUse gate does ZERO filesystem
# I/O; the shipped-manifest scan happens once at boot, off the event loop. Names
# are casefolded on ingest so the gate lookup is a pure set membership test.
# Empty until warmed → fail-closed: an unrecognised server name never
# auto-approves. This is what stops an undeclared ``<app>:evil`` entry that
# landed in the MUTABLE global MCP config from being trusted just because its
# prefix matches a first-party app.
_BUILTIN_APP_MCP_SERVERS: frozenset[str] = frozenset()


# Builtin (first-party) app names, populated ONCE at gateway boot via
# ``set_builtin_app_names`` (see the dashboard startup). Kept as a plain
# module global — NOT derived on the per-tool-call path — so the PreToolUse gate
# does ZERO filesystem I/O: scanning the shipped-manifest tree on the event loop
# (even once, before an lru_cache warmed) would stall every gateway task. Names
# are casefolded on ingest so the gate lookup is a pure set membership test.
# Empty until warmed → fail-closed: an app whose provenance is not yet known is
# treated as third-party and its own-server calls simply prompt (never wrongly
# auto-approved). Boot runs on the startup thread, well before any tool call.
_BUILTIN_APP_NAMES: frozenset[str] = frozenset()


# Agent name → owning builtin app, populated ONCE at gateway boot via
# ``set_builtin_app_agents``. Parallel to ``_BUILTIN_APP_NAMES`` /
# ``_BUILTIN_APP_MCP_SERVERS`` and kept as a plain module global for the same
# reason — the PreToolUse gate does ZERO filesystem I/O. Keys are casefolded on
# ingest so the lookup is a pure dict hit. Empty until warmed → fail-closed: an
# unrecognised agent yields no app identity and its own-server calls simply
# prompt, exactly as before this map existed.
_BUILTIN_APP_AGENTS: dict[str, str] = {}


def _cu_read_only_auto_approve(tool_name: str) -> bool:
    """True when *tool_name* is a computer-use OBSERVATION tool and the feature is on.

    Two independent conditions, both required:

    * the action is classified ``observe`` by the code-owned table in
      ``platform/governance.py`` (never by a title heuristic, and never by a
      private copy of the table — the class table is the single source of truth);
    * the keystone primary enable says the feature is on, so a disabled feature's
      tools are not silently pre-approved.

    Fail-CLOSED (False on any error): failing to auto-approve merely falls through
    to interactive approval, which is the safe direction.
    """
    action = computer_use_action_from_title(tool_name)
    if not action:
        return False
    if CU_CLASS_OBSERVE not in computer_use_action_classes(action):
        return False
    try:
        # Deferred deliberately: ``enable_state`` imports ``config.loader``, which
        # hooks.py keeps OFF its module import path (the loader fires the data-home
        # migration and pulls the whole config stack). Reached only after the
        # cheap prefix + class tests above, so an ungoverned host with no
        # computer-use traffic never pays for it.
        from kiro_crew.computer_use import enable_state

        return enable_state.is_enabled()
    except Exception:
        logger.debug("computer-use enable-state probe failed", exc_info=True)
        return False


# Display prefixes that kiro-cli ACP adds to tool titles
_TOOL_TITLE_PREFIXES = ("Running: ", "Reading ")

# The marker kiro-cli appends when it cuts a long shell call's title (today
# after the command's first 197 characters; see ``untruncated_shell_title``).
_TITLE_TRUNCATION_MARKER = "..."

# ACP semantic tool kind for a file write/edit (fs_write / code). The kind that
# carries a real target path in ``raw_params['path']`` and maps to the
# ``filesystem.write`` scope. Used to gate the write-only config-file protection
# so reads are not affected.
_EDIT_TOOL_KIND = "edit"

# Fixed prefix of the synthesized file-search deny target. A NAMESPACE, not a trust
# boundary: it exists so a rule can address a search's SCOPE distinctly from a command
# line. The display title is a deny target in its own right, so a title quoting this
# prefix trips such a rule too — an over-block, identical to the title tier for every
# other rule, and it grants nothing.
_SEARCH_DENY_PREFIX = "file-search"

# ``operation`` values that walk a tree WITHOUT carrying a ``pattern``. Enumerated by
# name, so a tool with a novel recursive argument shape is not recognized — see the
# residual limits in ``_search_deny_target``.
_RECURSIVE_SEARCH_OPERATIONS: frozenset[str] = frozenset(
    {
        "search_symbols",
        "search_codebase_map",
        "generate_codebase_overview",
        "find_references",
    }
)

# The canonical field carrying the search ROOT. Normalized before emission so one
# spelling of a tree reaches a rule (``_normalize_search_path``); ``max_depth`` is a
# number and needs no such treatment.
_SEARCH_PATH_FIELD = "path"

# The sensitive-path keystone's target extraction — ``TARGET_PATH_KEYS``, the
# ``_TARGET_PATH_MAX_PATHS`` / ``_TARGET_PATH_MAX_NODES`` work caps, the
# ``TargetPaths`` list-subclass carrying ``truncated``, and the bounded,
# depth-aware ``target_paths`` walk — now live in
# ``kiro_crew.platform.tool_paths`` (imported at module top) so the governance
# intersection plane can share the SAME traversal. governance cannot import
# hooks — hooks imports governance — so the walk moved DOWN a layer that both
# import. The names are re-exported from ``hooks`` (see the top-level import)
# unchanged so the keystone consumers below and any caller that imports them
# from ``hooks`` keep working. hooks still applies HARD-DENY semantics to
# ``truncated`` (deny an unverifiable scan); governance applies its own
# permit-by-default policy to the same flag.


# The SCOPE-bearing arguments of a file search as ``(canonical, accepted spellings)``,
# in a fixed order so the synthesized target is deterministic. Scope is the root walked
# and the depth cap — NOT what is being looked for. ``pattern`` and ``include`` are
# model-authored free text and are deliberately NOT emitted: a value can mint a field
# it is not (a pattern containing ``max_depth=`` silences a rule keyed on the absence
# of a cap), and a benign search whose pattern is ``DROP TABLE`` would match a
# command-oriented built-in rule. ``pattern`` is read by the shape gate only.
#
# Each key is read under every spelling kiro-cli may echo — our schemas declare
# snake_case but some calls come back in ``rawInput`` camelCased (the same split
# ``TOOL_PURPOSE_KEYS`` handles) — and emitted under the canonical name, so a rule
# spells each field once. A missed spelling INVERTS a rule rather than weakening it:
# with no ``max_depth`` field, a rule keyed on the absence of a cap fires on a search
# that carries one.
#
# The emitted string is a PUBLIC GRAMMAR: operators author deny rules against these
# field names, this order, and this encoding, so changing any of them silently breaks
# or inverts rules already installed. Treat additions as append-only.
_SEARCH_DENY_ARG_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (_SEARCH_PATH_FIELD, ("path", "file_path", "filePath")),
    ("max_depth", ("max_depth", "maxDepth")),
)

# What a value must not contribute to the flat ``key=value …`` grammar: ``=`` mints a
# field name and whitespace mints a field boundary, so a value carrying either could
# forge a field the call does not have. ``%`` is escaped first so the encoding is
# unambiguous. It is lossy by design — every whitespace character collapses to ``%20``
# — because a rule matches a shape and never needs the original bytes back.
_SEARCH_DENY_ESCAPES: tuple[tuple[str, str], ...] = (("%", "%25"), ("=", "%3D"))

# The ONLY environment variables substituted into a search root. This is an allowlist
# because the value is MODEL-AUTHORED and the synthesized target is AUDITED: a denied
# target is written to the security event log as the ``operation`` field, so
# ``os.path.expandvars`` — which dereferences any variable — let an agent pick a path
# prefix it knew a rule refuses, append ``$AWS_SECRET_ACCESS_KEY``, and have the deny
# it deliberately triggered write the expanded secret to a readable log. That turns the
# deny matcher itself into an exfiltration primitive.
#
# These names are safe to expand because their value is the home path, which is
# precisely what a home-scoped rule matches on — expanding them reveals nothing the
# target would not already carry. Every other variable stays literal, which under-matches
# rather than over-matches: a rule keyed on an absolute prefix simply does not fire, the
# same fail-safe direction as the relative-root decision.
_SEARCH_HOME_VARS: tuple[str, ...] = ("HOME", "USERPROFILE")


#: (pattern, identity) pairs already reported by ``_note_title_only_grant_pattern``,
#: bounded so a misconfigured pattern over a long session cannot grow it without limit.
_TITLE_ONLY_GRANT_NOTED: set[tuple[str, str]] = set()
_TITLE_ONLY_GRANT_NOTED_CAP = 512


_unc_data_home_root_cache: tuple[tuple[object, ...], Path | None] | None = None


def _unc_data_home_root() -> Path | None:
    """The data home as a UNC-gate trusted root, memoized per configuration.

    The twin of :func:`_unc_agents_root`, and it exists for the same reason.
    ``data_home()`` is cheap only on its *default-home* branch: with
    ``KIROCREW_HOME`` set it calls ``_valid_override_home()`` FIRST, on every
    call, and that does ``Path(override).expanduser().resolve()`` --
    filesystem I/O, and on a UNC-shaped override an SMB touch. ``config_dir()``
    memoizes, but that memo sits BEHIND the predicate, so it never covers this.
    Measured at this PR's head: three ``protected_ref_spans()`` calls produced
    three resolves of the override.

    That is the one configuration this gate has to be fast in. A roaming
    profile is exactly when ``KIROCREW_HOME`` points at a share, so the
    per-call resolve lands on the host whose latency the gate promises never to
    depend on -- and :func:`unc_probe_allowed` is reached from
    ``iter_local_refs``, which ``telegram.renderer._rotate_on_length`` runs
    INLINE on the event loop against a documented 7-15 us/KB budget.

    Resolves through :func:`peek_data_home`, NOT :func:`data_home`: this module
    primes the memo at import time, and ``data_home()`` on a first resolution
    delegates to ``config_dir()`` -- ``mkdir`` plus the recovery-breadcrumb
    write. The gate only needs to know WHERE the root is (a path-prefix trust
    check), so importing this module must not create directories or write
    breadcrumbs -- that maintenance belongs to ``ensure_data_home()`` at process
    start. ``peek_data_home()`` applies the SAME override predicate, so reader
    and writer agree on the root, and reads nothing else.

    Memoized on the RAW ``KIROCREW_HOME`` value plus the accessor identity and
    the resolved-home cache the default branch reads -- so an env change, a
    monkeypatched accessor or a reset of the resolution cache all invalidate
    naturally.

    A computation failure memoizes ``None`` (root absent, gate stays total),
    for the reason :func:`_unc_agents_root` gives: the failure being avoided is
    a per-call resolve that can block on an SMB timeout, and the degraded state
    -- UNC attachment paths refused -- is the safe one.
    """
    global _unc_data_home_root_cache
    key: tuple[object, ...] = (
        os.environ.get("KIROCREW_HOME"),
        _config_paths.peek_data_home,
        getattr(_config_paths, "_resolved_home", None),
    )
    cached = _unc_data_home_root_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        root: Path | None = _config_paths.peek_data_home()
    except (ValueError, OSError, RuntimeError):
        root = None
    _unc_data_home_root_cache = (key, root)
    return root


_unc_agents_root_cache: tuple[tuple[object, ...], Path | None] | None = None


def _unc_agents_root() -> Path | None:
    """The kiro agents dir as a UNC-gate trusted root, memoized per configuration.

    ``kiro_agents_dir()`` resolves ``KIRO_HOME`` (``Path.resolve()`` --
    filesystem I/O, and on a UNC-shaped override an SMB touch), so consulting
    it per gate check would put blocking I/O -- and exactly the network access
    this gate promises not to make -- on every validation, including async
    callers. Memoized on the RAW ``KIRO_HOME`` env value plus the accessor
    and override-hook identities, so the resolution
    runs once per configuration and a monkeypatched or hot-swapped accessor
    invalidates naturally. Mirrors how ``data_home()`` keeps its own hot path
    cheap.

    A computation failure memoizes ``None`` (root absent, gate stays total):
    deterministic-per-configuration beats self-healing here, because the
    failure mode being avoided is a per-call resolve that can block on an SMB
    timeout, and the degraded state -- UNC agent specs refused -- is the safe
    one. Recovery is an env change or process restart.
    Benign write race under threads: last-writer-wins on an idempotent value.
    """
    global _unc_agents_root_cache
    key: tuple[object, ...] = (
        os.environ.get("KIRO_HOME"),
        _config_paths.kiro_agents_dir,
        getattr(_config_paths, "_agents_dir_override", None),
    )
    cached = _unc_agents_root_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        # Same admission basis as data_home(): the gateway itself writes the
        # managed agent specs here. The PROJECT-level agents dir is
        # deliberately NOT admitted -- an arbitrary project directory is not
        # gateway-written, so admitting it would widen the trust boundary.
        root: Path | None = _config_paths.kiro_agents_dir()
    except (ValueError, OSError, RuntimeError):
        # A broken home resolution must not take the gate down with it: the
        # two always-computable roots still apply and the function stays
        # total (True/False, never a propagated error).
        root = None
    _unc_agents_root_cache = (key, root)
    return root


# Prime the memo at import time: without this the FIRST gate check after
# process start -- or after a ``KIRO_HOME`` change --
# still pays the resolving accessor on whatever thread asked, which on an async
# validation path is the event loop. Import of this module happens at process
# start, off the loop, so the one resolution per configuration lands there.
# Best-effort: a failure here memoizes root-absent exactly as a lazy miss would.
_unc_agents_root()
# Same priming for the data home, for the same reason: the first gate check
# after start (or after a ``KIROCREW_HOME`` change) would otherwise pay the
# override resolve on whatever thread asked, which on the inline classifier
# path is the event loop.
_unc_data_home_root()
#: Upper bound on the Windows link chain validate_file_path will walk
#: hop-by-hop before refusing. Covers both linked ancestors and the leaf.
#: Mirrors the kernels' own symlink-resolution ceilings (Linux SYMLOOP_MAX
#: chains resolve to ELOOP at 40): a longer chain is refused rather than
#: probed.
_WINDOWS_LINK_CHAIN_MAX = 40

#: Component-depth ceiling for the Windows link screens in
#: validate_file_path. The ancestor walk costs one lstat per component, so an
#: adversarially deep path (thousands of one-letter components fit inside the
#: 32K long-path limit) would turn the screen itself into an event-loop
#: stall. Deeper paths are refused outright, never probed -- no legitimate
#: dashboard file I/O path approaches this depth.
_MAX_SCREENED_PATH_DEPTH = 255

#: Fully qualified local Windows target: a drive letter FOLLOWED by a
#: separator. `D:x` (no separator) is drive-relative and deliberately not
#: matched -- it resolves against D:'s own per-drive CWD.
_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")

#: Any drive-letter prefix, separator or not -- used to tell drive-relative
#: (`D:x`) apart from plain relative (`x`).
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _screen_windows_links(
    target: str,
    read_errors: list[OSError] | None = None,
    *,
    follow_leaf: bool = True,
) -> str | None:
    """Replace Windows links with screened targets before ``realpath``.

    ``first_linked_ancestor`` walks root-first without traversing a link.
    Reading that link's own reparse metadata is safe. Replacing the linked
    prefix with its vetted target preserves the remaining child path while
    avoiding the blanket rejection of benign local junctions.

    ``read_errors``, when given, collects the ``OSError`` of a link whose own
    target could not be read. The path is refused either way; the list tells a
    caller that this refusal comes from a read that did not complete, not from
    a target the screen does not accept.

    ``follow_leaf=False`` rewrites linked ancestors only and returns as soon as
    none is left, so a caller with its own leaf-link rule applies that rule to
    the returned path's leaf. See :func:`screen_windows_linked_ancestors`.
    """
    for _ in range(_WINDOWS_LINK_CHAIN_MAX):
        if target.count("\\") + target.count("/") > _MAX_SCREENED_PATH_DEPTH:
            return None

        linked = platform_compat.first_linked_ancestor(target)
        if linked is not None:
            try:
                raw_target = os.readlink(linked)  # lgtm[py/path-injection]
            except OSError as exc:
                if read_errors is not None:
                    read_errors.append(exc)
                return None
            except ValueError:
                return None
            try:
                suffix = os.path.relpath(target, linked)
            except (OSError, ValueError):
                return None
            if suffix == ".." or suffix.startswith(".." + os.sep):
                return None
            normalized = _normalize_windows_link_target(linked, raw_target)
            if normalized is None:
                return None
            target = os.path.normpath(os.path.join(normalized, suffix))
            continue

        if not follow_leaf or not platform_compat.is_link_or_junction(target):
            return target
        try:
            raw_target = os.readlink(target)  # lgtm[py/path-injection]
        except OSError as exc:
            if read_errors is not None:
                read_errors.append(exc)
            return None
        normalized = _normalize_windows_link_target(target, raw_target)
        if normalized is None:
            return None
        target = normalized

    return None


MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB safety cap


class FileTooLargeError(Exception):
    """Raised when a file exceeds MAX_FILE_BYTES or a caller's own byte cap.

    *size* is the measured size in bytes when the raiser knows it, else ``None``.
    """

    def __init__(self, *args: object, size: int | None = None) -> None:
        super().__init__(*args)
        self.size = size


# ---------------------------------------------------------------------------
# Internal authorized reads of sensitive paths
# ---------------------------------------------------------------------------
#
# The default ``safe_read_file`` / ``safe_read_file_bytes`` paths refuse any
# path that ``is_sensitive_path`` flags. A small set of **internal system**
# operations legitimately need to read a file ``is_sensitive_path`` blocks.
# Rather than have those callers reach for ``Path.read_bytes`` directly --
# which would scatter sensitive-path reads across the codebase and make the
# audit story ad-hoc -- they go through ``safe_read_file_internal(read_id)``,
# which consults this hardcoded allowlist, performs the read, and emits an SEL
# audit event on every outcome.
#
# Adding a new entry is a security-review event: it widens the set of sensitive
# reads that can happen outside the deny rule. Each entry's comment must justify
# why the read is system infrastructure (the bytes leaving the process never
# reach an LLM/agent surface) rather than LLM/agent-mediated content.
#
# The `backend-security-controls` rule requires reads of
# "user- or LLM-influenced paths" to pass is_sensitive_path() and explicitly
# EXEMPTS "trusted fixed-path internal ... reads". Every read_id here maps to a
# HARDCODED constant path (never derived from user/LLM/config input), the read
# is SEL-audited on every outcome and fail-closed (a success whose audit cannot
# be persisted returns None), the open is O_NOFOLLOW + fstat, and the target
# stores are themselves classified sensitive in security._SENSITIVE_HOME_DIRS
# so agent file tools cannot reach them. This is the sanctioned fixed-path
# internal case the rule exempts, not a weakening of the keystone.
_INTERNAL_READ_ALLOWLIST: dict[str, str] = {
    # ``kiro_crew.dashboard.handlers.kiro_usage_api`` reads the kiro-cli SSO
    # access token to authenticate a single ``GetUsageLimits`` call to the
    # hardcoded CodeWhisperer RTS endpoint
    # (``codewhisperer.us-east-1.amazonaws.com``) that powers the dashboard
    # credit-usage pill -- the same API the Kiro IDE credit meter uses. The
    # token bytes go only to that AWS endpoint over TLS; only the parsed numeric
    # usage dict returns to the process, and it is run through
    # ``redact_credentials``/``redact_exfiltration_urls`` before caching, so the
    # credential never reaches an LLM/agent surface. The operator already
    # trusted KiroCrew with the session by running ``kiro-cli login`` outside
    # any agent loop. (On Linux the live token lives in the kiro-cli SQLite
    # store, which is not a sensitive path; these JSON entries cover the IDE /
    # older kiro-cli cache layout.)
    "kiro_usage_api.sso_token_cli": ".aws/sso/cache/kiro-auth-token-cli.json",
    "kiro_usage_api.sso_token_ide": ".aws/sso/cache/kiro-auth-token.json",
}


# Registry of sanctioned audit-only credential accesses: read_id -> the
# credential-bearing location it covers. Both classes below owe the same SEL
# audit trail as ``_INTERNAL_READ_ALLOWLIST``, and neither can route through
# ``safe_read_file_internal`` -- which returns the CONTENT of a FIXED sensitive
# path:
#
#   1. A live secret at a path that is NOT classified sensitive, so the
#      sensitive-path gate does not apply to it at all.
#   2. A presence-only access under a classified directory at a per-subject
#      COMPUTED name: there is no content to return and no fixed relative path
#      to register, so the gate has nothing to act on -- but the access is still
#      first-party contact with a credential store and still owes a trail.
#
# Every entry requires the same security-review justification discipline as
# ``_INTERNAL_READ_ALLOWLIST``.
_AUDIT_ONLY_READ_IDS: dict[str, str] = {
    # kiro-cli / amazon-q SQLite auth stores: live SSO bearer token on Linux.
    # Read read-only by ``kiro_crew.dashboard.handlers.kiro_usage_api`` for the
    # single hardcoded GetUsageLimits call (see the kiro_usage_api.sso_token_*
    # justification in _INTERNAL_READ_ALLOWLIST -- identical posture, different
    # storage layout).
    "kiro_usage_api.sqlite_token": ".local/share/{kiro-cli,amazon-q}/data.sqlite3",
    # Same store, read by ``kiro_crew.kiro_cli.signed_in_via_idc`` to answer one
    # question for the enterprise MCP-governance diagnostic: did this identity come
    # from Identity Center? Only the two non-secret ``auth.idc.*`` marker rows are
    # selected, and only their COUNT leaves the function -- no token row is read and
    # no value is returned. The audit is owed regardless, because the file holds
    # live credential material whatever this reader touches.
    "kiro_cli.idc_identity_probe": ".local/share/kiro-cli/data.sqlite3",
    # Same store, read by ``kiro_crew.kiro_prerequisite.identity_fingerprint`` to
    # answer one question on the turn path: does the account signed in NOW differ
    # from the one the running kiro-cli children loaded? Only stable account
    # claims participate (start_url, region, oauth_flow, scopes, client_id) plus
    # the non-secret ``auth.*`` / ``api.codewhisperer.*`` marker rows; the values
    # are hashed and only a digest leaves the function. The rotating and secret
    # fields (access_token, refresh_token, expires_at, client_secret) are excluded
    # by an ALLOWLIST, so a field added to the blob later cannot join by default.
    # Audited on the observation a caller acts on rather than per poll -- the
    # reader holds a short cache -- for the same reason as the mint entry below.
    "kiro_prerequisite.identity_fingerprint": ".local/share/kiro-cli/data.sqlite3",
    # Same store, read read-only by
    # ``kiro_crew.apps.builtins.aws_control.backend.backup._export_cli_conversations``
    # to copy ONLY the terminal conversation allowlist (its chat tables) into
    # the off-host sessions archive. No token row is read and no credential value
    # leaves the function -- the export writes a fresh database of the allowlisted
    # tables alone -- but the file holds live bearer tokens whatever this reader
    # touches, so opening it owes the same trail as every other reader here.
    # Audited on every outcome (the store was opened) and fail-closed on success:
    # a conversation export whose access cannot be recorded is dropped from the
    # archive rather than shipped unaudited.
    "aws_control.conversation_export": ".local/share/{kiro-cli,amazon-q}/data.sqlite3",
    # Class 2. kiro-cli's MCP OAuth artifact cache under ``~/.aws/sso/cache``.
    # ``kiro_crew.mcp_grant.grant_present`` STATS the paired
    # ``<sha256(mcp_url)>.token.json`` / ``.registration.json`` artifacts to learn
    # whether kiro-cli already holds a grant for ONE endpoint. The files are never
    # opened, so no token material can enter the process.
    #
    # TWO callers, and the second is the wider one: the mint's consent-completion
    # signal (curated registry providers only), and ``mcp_discovery``'s remote
    # probe, which asks for ANY url the user configured whenever a probe meets an
    # OAuth challenge. So the reasoning cannot rest on the url being
    # registry-declared. What keeps it sound for arbitrary input is the key: the
    # name is a sha256 over the url's normalized origin and path, so no caller can
    # express a path outside this directory, name a file it did not derive, or
    # smuggle a credential from the url into the filename. The digest is also why
    # the widened caller set adds no read surface -- both callers can only ever
    # probe for the pair belonging to the url they already hold.
    #
    # Audited on the observation a caller acts on, not per poll, and the two
    # callers differ on which observations those are. The mint polls for a grant
    # to APPEAR, so only its TRUE is acted on and recorded. The probe reads once
    # and renders either answer -- an absent pair is what produces "Sign-in
    # required" -- so it opts into recording the negative too, as ``missing``.
    # See ``mcp_grant.grant_observed`` for why that boundary is not fail-closed,
    # and note that neither caller logs the url itself (the probe logs the server
    # name, the mint warning logs the key) because a user-supplied endpoint can
    # carry a credential in its userinfo or query string.
    "connections_mint.oauth_grant_presence": ".aws/sso/cache/<sha256(mcp_url)>.token.json",
    # Class 2, same artifacts and same posture as the mint entry above: the
    # status module (``kiro_crew.connections.status``) STATS the identical
    # paired grant artifacts to answer the dashboard's authorization question.
    # A separate id, not a reuse of the mint's, so the SEL trail says which
    # surface looked. Audited only on the acted-on observation -- the stamping
    # of a provider's first-connect timestamp -- never per poll sweep; see
    # ``status.reconcile_connected_since`` for why that boundary is
    # best-effort rather than fail-closed (stats only, no bytes returned).
    "connections_status.oauth_grant_presence": ".aws/sso/cache/<sha256(mcp_url)>.token.json",
    # Class 2, same artifacts and same posture again: the premint endpoint
    # (``dashboard.handlers.connections.api_connections_premint``) reaches the warm
    # engine's candidate scan, which STATS the paired grant artifacts for every
    # registry provider to decide which ones still need a URL minted.
    #
    # A separate id from the mint's and the status module's, so the trail names the
    # surface that looked. ONE event per activation rather than one per candidate:
    # the scan is a single pass whose N answers feed exactly one act decision (spawn
    # the shared warm process, or do not), so per-candidate events would over-count
    # one observation and, because this entry point marks its events critical, drain
    # the queue N times for a single page mount. Audited only when the endpoint ACTS
    # -- an empty candidate set returns early without spawning, persisting, or
    # reporting any grant answer, so a page mounted against a fully-authorized
    # gallery writes nothing. Best-effort rather than fail-closed for the same reason
    # as the two entries above (stats only, no bytes returned); see
    # ``api_connections_premint`` for why refusing would be the worse failure.
    "connections_premint.oauth_grant_presence": ".aws/sso/cache/<sha256(mcp_url)>.token.json",
}


# ── Script Hooks ──

# Inclusive bounds for a script hook's subprocess timeout, in seconds. Mirrors
# the API schema (``validation.HOOK_CREATE_SCHEMA`` min_val=1/max_val=300); kept
# here so the same bound is enforced at EVERY persistence boundary — create,
# update, and deserialization — not only when a value arrives over the dashboard
# API. A 0 (or negative) timeout makes ``asyncio.wait_for`` fire immediately, and
# an unbounded one lets a hook wedge a turn for as long as it likes; both are
# outcomes a hand-edited or older ``hooks.json`` could otherwise reintroduce.
HOOK_TIMEOUT_MIN = 1
HOOK_TIMEOUT_MAX = 300
HOOK_TIMEOUT_DEFAULT = 30

# Events on which a standalone skills-only hook (no command) actually fires: only
# UserPromptSubmit / AgentSpawn synthesize the "Load skills:" directive in
# ``ScriptHookStore.fire()``. On any other event the directive has no consumer,
# so pairing skills with one is a config that saves but never fires.
_SKILLS_ONLY_EVENTS = (HOOK_EVENT_USER_PROMPT_SUBMIT, HOOK_EVENT_AGENT_SPAWN)

# A global Python inline-flag directive at the very start of a pattern. Only this
# form replaces the matcher-wide case-insensitive default. Scoped forms such as
# ``(?i:...)`` and ``(?-i:...)`` govern their group only, so the caller still
# prepends ``(?i)`` for the rest of the expression.
_GLOBAL_INLINE_FLAGS_RE = re.compile(r"^\(\?[aiLmsux]+\)")


# Env keys a script-hook subprocess may inherit from the gateway. A script hook
# runs an operator/agent-authored command through ``/bin/sh -c`` (POSIX) or
# ``cmd /c`` (Windows), so it needs only what the shell and an ordinary command
# require to run — an interpreter/tool on PATH, HOME, locale, TLS trust, and a
# proxy — plus the two hook-metadata variables ``_hook_subprocess_env``
# (``hook_runtime/script_hooks.py``) injects. Inheriting the
# whole gateway environment (``{**os.environ, ...}``) also handed every hook the
# gateway's AWS keys, model/provider keys, OAuth tokens, and connection strings,
# which a hostile or careless command could echo straight back through stdout,
# stderr, or the audit trail. This is the same strict-allowlist boundary the
# authenticated ``gh``/``glab`` spawns cross (``_PROVIDER_BASE_ENV_KEYS`` in
# ``dashboard/source_providers/runner.py``); a variable a hook genuinely needs
# is added here by name, never by opening the gate to the whole environment. A
# key absent from the host environment is simply not forwarded — the allowlist
# is a filter, not a set of required keys — so a minimal container is unaffected.
_HOOK_BASE_ENV_KEYS: frozenset[str] = frozenset(
    {
        # A hook subprocess sees ONLY these ambient keys (plus the two
        # KIROCREW_HOOK_* metadata vars). A hook that depended on an ambient var
        # NOT listed here (e.g. VIRTUAL_ENV, PYTHONPATH, JAVA_HOME, AWS_PROFILE,
        # nvm/pyenv vars) will fail after upgrade with a "works in my terminal,
        # fails in the hook" symptom — the fix is to add that key here by name.
        # This fail-closed direction is deliberate: the allowlist is the
        # secret-egress boundary, so widening it is a per-key security decision.
        # Shell / command resolution. PATH is what lets ``/bin/sh -c "python …"``
        # find the interpreter; the Windows spellings mirror it there.
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        # Home / user profile — a hook command that reads or writes under ``~``.
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        # Data home — a hook that invokes the ``kirocrew`` CLI (or otherwise
        # reads the instance's data dir) must resolve the gateway's overridden
        # home, not the default ``~/.kiro/crew``. It is a path, not a credential,
        # so preserving it does not widen the secret-egress boundary this env
        # allowlist exists to close.
        "KIROCREW_HOME",
        # Temp dir — a hook that stages a scratch file.
        "TMPDIR",
        "TEMP",
        "TMP",
        # Locale — so a hook's output encoding matches the host.
        "LANG",
        "LC_ALL",
        # TLS trust may be required by network clients. Proxy URLs are omitted:
        # HTTP(S)_PROXY commonly embeds userinfo credentials, and script hooks
        # are an untrusted execution boundary. NO_PROXY carries host patterns,
        # not credentials, and is safe to preserve.
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "NO_PROXY",
        "no_proxy",
    }
)


@dataclass
class ScriptHook:
    """Executable hook that runs a shell command on a trigger event.

    Exit-code contract:
    - Exit 0: success (stdout → context for AgentSpawn/UserPromptSubmit;
      a delivered "allow" for PreToolUse)
    - Exit 2: deny tool (PreToolUse only, stderr → LLM)
    - Any other exit — including timeout, crash, or an unexecutable command:
      PreToolUse BLOCKS the tool (fail closed; the block detail prefers
      ``ScriptHookResult.error``, then stderr, then "exited with code N").
      Every other event stays warn-only (stderr shown to user). There is no
      per-hook advisory/fail-open opt-out.
    """

    id: str = ""
    name: str = ""
    event: str = HOOK_EVENT_USER_PROMPT_SUBMIT
    matcher: str = ""  # tool matcher for PreToolUse/PostToolUse (empty = all tools)
    matcher_mode: str = (
        "glob"  # "glob" (fnmatch, default), "regex" (re.search), "contains" (case-insensitive pipe-delimited substrings)
    )
    command: str = ""  # shell command to execute
    skills: list = field(
        default_factory=list
    )  # skill keys to inject when matched (no subprocess needed)
    timeout: int = 30  # seconds (Kiro CLI default is 30s)
    enabled: bool = True
    last_run: float = 0.0
    last_status: str = ""  # "ok", "error", "timeout", "blocked"
    last_error: str = ""  # human-readable reason for the most recent non-ok status
    run_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScriptHook":
        # Support legacy "pattern" field as fallback for "matcher"
        matcher = data.get("matcher", data.get("pattern", ""))
        skills_raw = data.get("skills", [])
        skills = skills_raw if isinstance(skills_raw, list) else []
        # Redact + truncate a persisted last_error on load: hooks.json is
        # operator-writable and an agent-written (or hand-edited) error can carry
        # a credential. It flows from_dict() -> /api/hooks -> the dashboard
        # InfoTip, so it is an output boundary and must be scrubbed here too, not
        # only at write time. Non-string values default to "".
        raw_last_error = data.get("last_error", "")
        last_error = (
            redact_via_context(raw_last_error)[:500]
            if isinstance(raw_last_error, str) and raw_last_error
            else ""
        )
        # Normalize the timeout on load. hooks.json is hand-editable and older
        # files predate the 1–300 bound, so a missing / non-int / out-of-range
        # value is clamped to a safe in-range value here rather than persisted
        # verbatim to later fire a 0-second (immediate) or unbounded timeout.
        # Deserialization is fail-soft on purpose (a malformed hook must load,
        # not abort the whole store); the raising `validate_hook_fields` is what
        # rejects a bad value at the create/update boundary. `event` is left as
        # written so an unknown event is visibly inert rather than silently
        # remapped.
        timeout = _normalize_hook_timeout(data.get("timeout", HOOK_TIMEOUT_DEFAULT))
        event = data.get("event", HOOK_EVENT_USER_PROMPT_SUBMIT)
        # Drop a matcher stored against an event no event fires, the same way the
        # timeout above is clamped. ``validate_hook_fields`` refuses that pairing at
        # the create/update boundary, and a hand-edited file can carry it anyway --
        # so keeping it would load a hook that cannot be edited or even disabled
        # without editing the file again, because update re-validates the MERGED
        # fields and would meet the stored matcher. Normalizing here means the store
        # never holds the combination and update never sees it. The matcher is the
        # part with no meaning on these events; the hook itself is kept.
        if matcher and event in HOOK_EVENTS_KAS_ONLY:
            logger.warning(
                "hook %s on %s carried a matcher; dropping it (no event fires this, "
                "so there is no payload to filter)",
                data.get("id", "?"),
                event,
            )
            matcher = ""
        # An unknown `matcher_mode` loads as glob, unlike an unknown event: glob is
        # what `_context_matches` runs any other mode as, so loading it as glob
        # changes nothing about how the hook matches. It also keeps the hook
        # editable, because `update` validates the MERGED fields and would
        # otherwise meet the stored value.
        matcher_mode = data.get("matcher_mode", "glob")
        if matcher_mode not in HOOK_MATCHER_MODES:
            logger.warning(
                "hook %s carried an unknown matcher_mode; loading it as glob",
                data.get("id", "?"),
            )
            matcher_mode = "glob"
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            name=data.get("name", ""),
            event=event,
            matcher=matcher,
            matcher_mode=matcher_mode,
            command=data.get("command", ""),
            skills=[str(s) for s in skills if isinstance(s, str)],
            timeout=timeout,
            enabled=data.get("enabled", True),
            last_run=data.get("last_run", 0.0),
            last_status=data.get("last_status", ""),
            last_error=last_error,
            run_count=data.get("run_count", 0),
        )


# ── Script hook output caps ──
#
# ``await proc.communicate(...)`` buffers BOTH pipes in memory until EOF, so a
# buggy or hostile hook can emit unbounded stdout/stderr and OOM (or stall) the
# gateway for every session before the 500-char presentation limit is ever
# applied. ``run_script_hook`` instead drains each stream incrementally and keeps
# only the first ``_HOOK_STREAM_CAP_BYTES`` bytes, while continuing to read (and
# discard) the rest so the child can never block on a full pipe. The cap is
# generously above the 500-char field we surface, so
# the retained prefix is always enough to decode and truncate for display, yet
# small enough that a runaway hook cannot exhaust memory.
_HOOK_STREAM_CAP_BYTES = 64 * 1024
# Marker appended to a decoded stream when its raw bytes exceeded the cap, so
# truncation is visible rather than silent.
_HOOK_TRUNCATION_MARKER = "\n…[output truncated]"


# ── Script Hook Store (persistence) ──

_HOOKS_FILE = "hooks.json"


# -- Global script hook store accessor --
# Set by dashboard server.py / handlers.py when the store is initialized.
# Allows any module (task_executor, llm_helpers, subagent) to fire script hooks
# without needing a reference to DashboardState.

_global_script_hook_store: ScriptHookStore | None = None


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.hooks.<name>`` reaches it wherever it lives; see
# :mod:`kiro_crew.hook_runtime`. Run once, after this body has bound every name --
# including the two UNC root memos primed above, which stay here because a priming
# call to an owner function would run before this line and write its memo into the
# owner's namespace.
_facade_compose(
    globals(),
    (
        _owner_denied_commands,
        _owner_descriptor_identity,
        _owner_gate_tiers,
        _owner_governance_gate,
        _owner_hook_dispatch,
        _owner_internal_reads,
        _owner_pinned_writes,
        _owner_safe_reads,
        _owner_script_validation,
        _owner_search_targets,
        _owner_stream_caps,
        _owner_tool_identity,
        _owner_windows_paths,
        _owner_hook_types,
        _owner_script_hooks,
    ),
)

# ``compose`` rebinds functions, not the generator a ``@contextmanager`` wraps, so the
# store's mutation guard is rebound here: a patch of ``kiro_crew.hooks.<name>`` reaches
# it as it reaches every other store method.
setattr(
    ScriptHookStore,
    "_atomic_mutation",
    contextmanager(
        _facade_rebind(vars(ScriptHookStore)["_atomic_mutation"].__wrapped__, globals())
    ),
)


# The tool gate, row by row, in the order ``HookManager.judge`` runs it. Built after
# ``compose`` so each row holds the rebound tier function, which reads this module's
# globals -- built before it, a row would keep the owner's original, and a patch of
# ``hooks.<name>`` would not reach the tier. The kinds are monotone (every deny before
# every grant, the classifier last). What each tier checks is documented on its body
# in :mod:`kiro_crew.hook_runtime.gate_tiers`.
GATE_TIERS: tuple[GateTier, ...] = (
    GateTier("unverifiable-shell", "deny", _tier_unverifiable_shell),
    GateTier(
        "targets",
        "deny",
        _tier_targets,
        rules=(
            GateRule("sensitive-path", _rule_sensitive_path),
            GateRule("sensitive-bash", _rule_sensitive_bash, "is_sensitive_bash_command"),
            GateRule("exfil", _rule_exfil, "audit_bash_exfiltration"),
        ),
    ),
    GateTier("param-paths", "deny", _tier_param_paths),
    GateTier("write-protected", "deny", _tier_write_protected),
    GateTier("deny-rules", "deny", _tier_deny_rules, "is_denied"),
    GateTier("mcp-auto-deny", "deny", _tier_mcp_auto_deny),
    GateTier("search-target", "deny", _tier_search_target),
    GateTier("governance", "deny_policy", _tier_governance),
    GateTier("app-own-server", "grant", _tier_app_own_server),
    GateTier("operator-grants", "grant", _tier_operator_grants),
    GateTier("read-only", "classify", _tier_read_only),
)

#: Why ``GATE_TIERS`` is not a sound gate table (``gate_tier_problems``), checked once
#: as this module loads; empty for the shipped table. A problem is not RAISED here: an
#: import-time refusal fails every test at collection, the one that names the problem
#: included. ``HookManager.judge`` refuses every call instead (fail closed), and
#: ``test/test_gate_tiers.py`` names the problem.
_GATE_TABLE_PROBLEMS: tuple[str, ...] = tuple(gate_tier_problems(GATE_TIERS))
if _GATE_TABLE_PROBLEMS:
    logger.error(
        "hooks.GATE_TIERS is not a sound gate table, so every tool call is refused: %s",
        "; ".join(_GATE_TABLE_PROBLEMS),
    )

# The checks of ``GATE_TIERS`` that judge a shell command line, as (tier name,
# ``kiro_crew.security`` attribute), in table order: ``shell_rules(GATE_TIERS)``
# written out. A LITERAL on purpose -- ``scripts/deny_diff.py`` reads it from this
# file with ``ast.literal_eval``, because the denial differential's parent process
# never imports the product it compares. No path rule: the gate reads a PATH there
# and a shell command is command text, which the gate deliberately does not match
# paths in (the OS sandbox holds the credential stores away from the shell).
# ``test/test_gate_tiers.py`` pins it equal to the projection.
SHELL_DENY_TIERS: tuple[tuple[str, str], ...] = (
    ("sensitive-bash", "is_sensitive_bash_command"),
    ("exfil", "audit_bash_exfiltration"),
    ("deny-rules", "is_denied"),
)
