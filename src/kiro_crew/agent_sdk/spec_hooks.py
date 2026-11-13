"""Run an agent spec's own ``hooks`` from Crew's turn loop, for a harness that cannot.

kiro-cli reads the spec off disk and runs its ``hooks`` itself. KAS takes the agent
over the wire as ``_meta.kiro.customAgents``, whose schema has no slot for them
(``acp.kas_agents.UNSUPPORTED_SPEC_KEYS``), so on KAS nothing runs them. The backends
in ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS`` get them from Crew instead: the turn loop
already fires the five Crew hook events for every backend, and this module turns the
spec's field into :class:`kiro_crew.hooks.ScriptHook` records that ride the same
``ScriptHookStore.fire`` call. So a spec hook meets exactly the gates a Hooks-page
hook meets -- ``capabilities.script_hooks`` and its SEL audit, the shell deny list,
the sandboxed spawn, the timeout -- and none that a Hooks-page hook does not. One
rule differs: on PreToolUse a spec hook keeps kiro-cli's exit-code contract, where
exit 2 blocks and a crashed or timed-out hook warns
(:attr:`kiro_crew.hook_runtime.script_hooks.ScriptHookResult.pre_tool_blocks`),
because that is how the same spec behaves on the kiro-cli backend. A hook a policy
refused to run blocks, with the refusal as the reason: it is a guard that gave no
verdict, not a broken one.

Membership is the whole double-fire guard. A non-member's harness runs the field
itself, so the turn loop asks :func:`spec_script_hooks` only when the session's
capabilities say Crew owns the job.

claude and codex are members of ``ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS``
alone. Neither harness is handed the spec, so Crew fires the events that cannot
refuse a call (:data:`OBSERVER_HOOK_EVENTS`) and drops ``preToolUse``
(:func:`observer_spec_hooks`): each approves some calls without asking, so a
gating hook would be skipped on exactly those. Nothing there gates on the spec, so
a spec that cannot be read costs its hooks, never a tool call.

Both spec shapes are read:

* the object form kiro-cli uses and Crew materializes, ``{event: [{command,
  matcher?, timeout_ms?}]}`` on the five camelCase event names;
* the array of KAS hook documents, validated by
  :func:`kiro_crew.agent.normalize_spec_hooks`. ``enabled: false`` is skipped, and
  so is ``confirm: true``, because no prompt can be shown from here; an ``agent``
  action and a trigger with no Crew event are skipped too.

A tool matcher is written in kiro-cli's tool names (``execute_bash``, ``fs_write``),
and KAS names its tools differently. On a PreToolUse permission request the turn
loop therefore matches these hooks against
:func:`spec_hook_tool_names` of the id KAS states for the call, not against the
call's title; a call KAS names no tool for is matched on
its title, as a Hooks-page hook is. A PostToolUse is matched the same way, on the id
:func:`post_tool_hook_tool_id` gives the finished call: KAS's tool-call frame names no
built-in tool, so that is the id the call's permission request stated, else KAS's
shell tool for a call classified as a shell command. A tool matcher the table does
not know is kept, matched against KAS's id as written, and warned about once.

The result is cached by the field's content, so a spec that stays the same costs
one conversion and logs its warnings once, not once per turn.

The turn loop is not the only place a tool runs: a subagent run and a task-runner
step fire the same hook store for their own tool calls. :func:`turn_spec_hooks`
answers for those, keyed on the SUBAGENT's agent and its own provider, so a kiro-cli
subagent under a KAS parent still gets nothing from Crew.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.agent_sdk.backend_mcp_ability import ability_for
from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.agent_sdk.kas_project_hooks import (  # noqa: F401,E402 - re-exported
    CRON_SESSION_KEY_PREFIX,
    KasProjectHooks,
    _consented_kas_project_hooks,
    _live_bot_owns,
    kas_hook_checkout,
    kas_project_hooks_state,
)

# Re-exported for the modules that import these from spec_hooks; they live in a
# sibling only to keep this module under its line cap. The sibling imports this
# module's seams lazily, so the two load in either order.
from kiro_crew.agent_sdk.spec_hooks_reproject import (  # noqa: E402,F401
    ClaimToken,
    UnattendedClaim,
    acquire_with_token,
    close_reprojection_gap,
    live_session_entry,
    make_claim_token,
    reproject_claimed_session,
    reproject_unattended_claim,
)
from kiro_crew.hooks import (
    _HOOK_STREAM_CAP_BYTES,
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    _normalize_hook_timeout,
)
from kiro_crew.security.paths import MAX_SCANNABLE_COMMAND_CHARS

if TYPE_CHECKING:
    from kiro_crew.providers.mirrors.base import Concern

logger = logging.getLogger(__name__)

#: kiro-cli's object-form event names, onto the events the hook store fires.
_OBJECT_EVENT_TO_HOOK_EVENT = {
    "agentSpawn": HOOK_EVENT_AGENT_SPAWN,
    "userPromptSubmit": HOOK_EVENT_USER_PROMPT_SUBMIT,
    "preToolUse": HOOK_EVENT_PRE_TOOL_USE,
    "postToolUse": HOOK_EVENT_POST_TOOL_USE,
    "stop": HOOK_EVENT_STOP,
}

#: The events Crew fires on a backend that fires only the spec hooks that cannot
#: refuse a tool call (``crew_fires_spec_observer_hooks`` without
#: ``crew_fires_spec_hooks``): every event but PreToolUse.
OBSERVER_HOOK_EVENTS = frozenset(
    {
        HOOK_EVENT_AGENT_SPAWN,
        HOOK_EVENT_USER_PROMPT_SUBMIT,
        HOOK_EVENT_POST_TOOL_USE,
        HOOK_EVENT_STOP,
    }
)

#: The events whose matcher names a tool. kiro-cli reads a matcher on these only,
#: so a matcher on any other event is dropped rather than applied to the context.
_TOOL_EVENTS = frozenset({HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_POST_TOOL_USE})

#: Bound on the hooks taken from one spec, so a runaway spec cannot make every turn
#: spawn without limit, set high enough that an installed agent's set runs whole.
_MAX_SPEC_HOOKS = 64

#: Bound on a retained command: the longest command the shell gate scans, so a
#: spec hook is held to the same ceiling as a shell tool call's command and every
#: one it keeps is scanned whole by the deny list before it spawns. kiro-cli runs
#: an object-form hook of any length, so a lower bound here would drop on KAS a
#: hook the same spec runs on kiro-cli. A hook document is still held to its own
#: validator's payload cap before it reaches this.
_MAX_COMMAND_LEN = MAX_SCANNABLE_COMMAND_CHARS

#: Bound on the conversion cache. A spec edit adds an entry, so the cache is
#: cleared rather than grown once it reaches this.
_CACHE_MAX = 64

#: The matcher kinds kiro-cli reads that the object form's character set refuses:
#: a name or glob with ``?``, ``@builtin``, ``@builtin/<glob>``, ``@<server>`` and
#: ``@<server>/<glob>``. A spec hook meets them on every backend Crew fires spec hooks
#: for, because :func:`spec_hook_kind_names` gives each call those names. Dropping one
#: would retire a guard kiro-cli runs. The server is one literal name, never a glob,
#: which would widen the guard across servers; the tool part is a glob of ``*`` and
#: ``?``. Only this conversion accepts them: the merge that writes kiro-cli's
#: materialized spec keeps its own rule.
_SPEC_MATCHER_RE = re.compile(r"[A-Za-z0-9_.*?\-]+|@[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.*?\-]+)?")

#: The hooks concern's id on a backend's MCP ability card
#: (:func:`kiro_crew.agent_sdk.backend_mcp_ability.concern_id` of ``Concern.HOOKS``).
_HOOKS_CONCERN = "hooks"

#: Per spec content: the hooks that run, and how many ``confirm: true`` documents
#: were skipped (the session-start notice names the count).
_cache: dict[tuple[str, str], tuple[tuple[ScriptHook, ...], int]] = {}

#: The origin tag a user standalone-hook-file hook carries, distinct from a spec
#: hook's ``project`` / ``user`` origin, so a reader can tell a ``~/.kiro/hooks``
#: file hook apart from an agent spec's own ``hooks`` field.
_USER_FILE_ORIGIN = "user_file"

#: Bound on a single standalone hook file read, so a large or runaway file cannot
#: be slurped whole. A v1 hook file is a small JSON object; 256 KiB is far above
#: any real one and still refuses a pathological file.
_MAX_HOOK_FILE_BYTES = 256 * 1024

#: Bound on the standalone hook files scanned in one directory, so a directory
#: packed with files cannot make session start walk without limit. The per-file
#: document cap and ``_MAX_SPEC_HOOKS`` still bound what actually fires.
_MAX_HOOK_FILES = 64

#: Per standalone hook FILE: the converted hooks keyed by (path, content digest),
#: so an unchanged file is converted -- and its skipped documents audited -- once
#: rather than on every turn. Bounded like :data:`_cache`.
_user_file_cache: dict[tuple[str, str], tuple[ScriptHook, ...]] = {}

#: Per standalone hook FILE: whether its own per-file cap truncated it (it had at
#: least one convertible hook beyond the cap), keyed like :data:`_user_file_cache`,
#: so a cap overflow is noted once per unchanged file rather than recomputed every
#: turn. A document skipped for being disabled, confirm-only, an unknown trigger or
#: a bad matcher is not a cap truncation and never flips this.
_user_file_doc_counts: dict[tuple[str, str], bool] = {}

#: Snapshots (one digest of the directory's files + their content) whose cap
#: overflow has already been warned about and SEL-audited, so a cap drop is
#: reported once per changed snapshot rather than on every turn. Bounded.
_user_file_truncation_audited: set[str] = set()


def _diagnostic(value: object) -> str:
    """A spec-supplied value for a log line: escaped, then redacted."""
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _hook_diagnostic

    return _hook_diagnostic(value)


def _matcher_ok(matcher: object) -> bool:
    """The object form's matcher rules, plus the kinds in :data:`_SPEC_MATCHER_RE`.

    Both are length-capped. The extra kinds are accepted here only: kiro-cli runs
    its spec's ``hooks`` itself and never reaches this conversion, so the
    materialized spec's rule stays as it is.
    """
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _MAX_MATCHER_LEN, _hook_matcher_ok

    if _hook_matcher_ok(matcher):
        return True
    return (
        isinstance(matcher, str)
        and len(matcher) <= _MAX_MATCHER_LEN
        and _SPEC_MATCHER_RE.fullmatch(matcher) is not None
    )


def _matcher_names_a_kas_tool(matcher: str) -> bool:
    """Whether ``matcher`` can match any name in the kiro-cli/KAS/goose/opencode tables."""
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_MATCH_VOCABULARY
    from kiro_crew.acp.kas_permissions import KAS_TOOL_MATCH_VOCABULARY, SPEC_HOOK_TOOL_TAGS
    from kiro_crew.hooks import _tool_matches

    vocabulary = (
        KAS_TOOL_MATCH_VOCABULARY | HARNESS_TOOL_MATCH_VOCABULARY | set(SPEC_HOOK_TOOL_TAGS)
    )
    return (
        matcher == "*"
        # ``@builtin``, an MCP server or tool: the gate builds these names itself.
        or matcher.startswith("@")
        or any(_tool_matches(matcher, name) for name in vocabulary)
    )


def spec_hook_kind_names(names: Sequence[str], *, mcp_server: str) -> tuple[str, ...]:
    """The kind names a spec hook matcher also meets for a call known by *names*.

    *names* are the call's trusted names (the harness's, its canonical identity,
    its ``@server/tool`` forms). One call can carry more than one reading: opencode's
    fused ``apply_patch`` is a native tool and a ``<server>_<tool>`` split at once,
    so each reading adds its own kinds and none is dropped.

    * An MCP reading (*mcp_server*, an ``@server/tool`` or a ``mcp__`` name) adds
      the ``@mcp`` tag, and ``@<server>`` for each server named.
    * A built-in reading adds ``@builtin``, ``@builtin/<name>`` for each name, and
      each :data:`kiro_crew.acp.kas_permissions.SPEC_HOOK_TOOL_TAGS` tag that
      selects one of the names, so ``read`` meets ``grep`` as on kiro-cli. With no
      MCP reading every name is built-in; with a trusted *mcp_server* none is;
      beside an unconfirmed split, only a name a tool table spells counts, so an
      MCP tool's bare name is not taken for a built-in.

    For spec hooks only: a Hooks-page hook keeps the names it always had.
    """
    # circular import: see _matcher_names_a_kas_tool.
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_MATCH_VOCABULARY
    from kiro_crew.acp.kas_permissions import KAS_TOOL_MATCH_VOCABULARY, SPEC_HOOK_TOOL_TAGS

    present = [n for n in names if n]
    mcp_forms = [n for n in present if n.startswith("@") or n.lower().startswith("mcp__")]
    servers = [mcp_server] if mcp_server else []
    servers += [n[1:].split("/", 1)[0] for n in mcp_forms if n.startswith("@") and "/" in n]
    is_mcp = bool(servers or mcp_forms)
    out: list[str] = []
    if is_mcp:
        out += [*(f"@{server}" for server in servers if server), "@mcp"]
    # A server the harness named is trusted: the call is that server's tool, and its
    # bare name is not a built-in. Only a split the harness did not confirm (a fused
    # opencode title) keeps a built-in reading too.
    plain = [] if mcp_server else [n for n in present if n not in mcp_forms]
    if is_mcp:
        vocabulary = KAS_TOOL_MATCH_VOCABULARY | HARNESS_TOOL_MATCH_VOCABULARY
        plain = [n for n in plain if n in vocabulary]
    if plain:
        lowered = {n.lower() for n in plain}
        tags = [tag for tag, tools in SPEC_HOOK_TOOL_TAGS.items() if lowered.intersection(tools)]
        out += ["@builtin", *(f"@builtin/{n}" for n in plain), *tags]
    return tuple(dict.fromkeys(out))


def spec_hook_tool_names(tool_id: str) -> tuple[str, ...] | None:
    """The names a spec hook's tool matcher meets for a call the harness named ``tool_id``.

    A goose or opencode id is qualified by its backend (``goose#shell``) and read
    through that backend's table
    (:func:`kiro_crew.acp.harness_tool_names.harness_tool_match_names`); any other id
    is KAS's own, read through
    :func:`kiro_crew.acp.kas_permissions.kas_tool_match_names`. Both are read through
    this module because application code reaches the ACP layer only via
    ``agent_sdk``. ``None`` when the harness named no tool, so the caller keeps
    matching on the call's title, as for a Hooks-page hook, rather than treating
    the hook as not applying.
    """
    if not tool_id:
        return None
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.harness_tool_names import harness_tool_match_names
    from kiro_crew.acp.kas_permissions import kas_tool_match_names

    harness_names = harness_tool_match_names(tool_id)
    if harness_names is not None:
        return harness_names
    return kas_tool_match_names(tool_id)


#: The hook store's event names, as kiro-cli writes them in ``hook_event_name``.
_HOOK_EVENT_TO_OBJECT_EVENT = {v: k for k, v in _OBJECT_EVENT_TO_HOOK_EVENT.items()}

#: Every kiro-cli built-in a call can be named by: the row keys of
#: ``KAS_TOOL_IDS_BY_KIRO_TOOL`` and ``harness_tool_names.HARNESS_TOOL_TABLES``, and
#: every tool a kiro-cli tag selects (``SPEC_HOOK_TOOL_TAGS``; ``code`` reaches a
#: call as KAS's own id, outside every row), as
#: kiro-cli's V2 engine names it on a hook's stdin: ``(tool_name, tool_tags)`` from
#: ``BuiltInToolName``'s display name and ``category_tags`` plus ``@builtin``
#: (``agent/src/agent/tools/mod.rs``, ``agent_config/parse.rs`` ``tags``).
KIRO_CLI_BUILTIN_HOOK_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "execute_bash": ("shell", ("shell", "@builtin")),
    "fs_read": ("read", ("read", "@builtin")),
    "fs_write": ("write", ("write", "@builtin")),
    "grep": ("grep", ("read", "@builtin")),
    "glob": ("glob", ("read", "@builtin")),
    "web_fetch": ("web_fetch", ("web", "@builtin")),
    "web_search": ("web_search", ("web", "@builtin")),
    "use_subagent": ("subagent", ("subagent", "@builtin")),
    "code": ("code", ("read", "write", "@builtin")),
}


def _explicit_mcp_name(name: str) -> str | None:
    """*name* when it is an ``@server/tool`` form; else ``None``.

    Only the ``@builtin/<name>`` kind names are not MCP: a server whose own name
    starts with ``builtin`` (``@builtin__docs/delete_file``) is one.
    """
    if name.startswith("@") and not name.startswith("@builtin/"):
        server, sep, tool = name[1:].partition("/")
        return name if sep and server and tool else None
    return None


def _fused_mcp_name(name: str) -> str | None:
    """A ``mcp__server__tool`` *name* split at its first ``__``; else ``None``.

    A guess: a server whose own name holds ``__`` splits wrongly, so this is read
    only when no ``@server/tool`` form names the call.
    """
    if name.lower().startswith("mcp__"):
        server, sep, tool = name[5:].partition("__")
        return f"@{server}/{tool}" if sep and server and tool else None
    return None


def _kas_write_id(tool_names: Sequence[str]) -> str:
    """The KAS id of a call whose kiro-cli name is ``fs_write``.

    The first name is the kiro-cli row; a KAS id other than ``fs_write`` in that row
    (``str_replace``, ``fs_append``, ``delete_file``) follows it. A call none
    follows is ``fs_write`` itself, or a harness's own write tool.
    """
    # circular import: see spec_hook_tool_names.
    from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL

    row = KAS_TOOL_IDS_BY_KIRO_TOOL.get("fs_write") or ()
    return next((n for n in tool_names[1:] if n in row and n != "fs_write"), "fs_write")


def _kiro_cli_write_input(tool_input: dict, kas_id: str) -> dict:
    """*tool_input* of a write call, plus the keys kiro-cli's ``write`` states.

    kiro-cli V2's ``write`` input is tagged by ``command``
    (``agent/src/agent/tools/fs_write.rs``): ``create`` with ``path`` and
    ``content``, ``strReplace`` with ``path``, ``oldStr``, ``newStr`` and
    ``replaceAll``. KAS's ``fs_write`` states ``path`` and ``text``, and its
    ``str_replace`` ``path``, ``oldStr``, ``newStr`` and ``replace_all``
    (``tools/default/fs-write.ts``, ``str-replace.ts``). So a content or path guard
    written for kiro-cli finds its keys. Only those two map: KAS's ``fs_append``
    also states ``path`` and ``text``, and calling it ``create`` would tell a hook
    the fragment is the whole file, so it, like ``delete_file``, keeps its input
    as it is. KAS's own keys stay, and a key the input already has is never
    overwritten.
    """
    out = dict(tool_input)
    if kas_id == "str_replace" and "oldStr" in tool_input and "newStr" in tool_input:
        out.setdefault("command", "strReplace")
        if "replace_all" in tool_input:
            out.setdefault("replaceAll", tool_input["replace_all"])
    elif kas_id == "fs_write" and "text" in tool_input:
        out.setdefault("command", "create")
        out.setdefault("content", tool_input["text"])
    return out


def kiro_cli_hook_payload(payload: dict, event: str, tool_names: Sequence[str] | None) -> dict:
    """*payload* as kiro-cli writes an agent spec hook's stdin.

    Crew runs on kiro-cli's V2 engine (its ACP client allowlist pins ``kirocrew``
    there), and V2 sends every spec hook, object or array form, the same shape
    (``agent/src/agent/task_executor/mod.rs`` ``run_command_hook``): a camelCase
    ``hook_event_name``, the tool's full name (``shell``, ``read``, ``write``, or
    ``@server/tool``) with its ``tool_tags``, and a Stop's ``assistant_response``.
    A spec hook reads those here too, so a script that branches on them takes the
    branch it takes on kiro-cli. Crew's own keys stay, so a script reading them
    still works.

    *tool_names* are the names the hook's matcher met (``extra_hooks_tool_names``);
    without them the tool fields are left as they are. The FIRST name decides what
    the call is, because every table puts the call's own reading first: a native
    tool's kiro-cli row name (``fs_write`` for opencode's ``apply_patch`` and for
    goose's ``developer`` write alike), an MCP split's ``@server/tool``, or KAS's
    bare id. A first name in :data:`KIRO_CLI_BUILTIN_HOOK_NAMES` is that built-in.
    Any other call is an MCP tool when any name is one: an ``@server/tool`` form
    (built from the trusted server, so it is exact) wins over a fused
    ``mcp__server__tool`` name, which is split only when no such form exists. With
    neither, the call keeps its first name with no tags. The kind names a matcher
    meets (``@builtin``, ``@server``) never decide it.
    """
    out = dict(payload)
    out["hook_event_name"] = _HOOK_EVENT_TO_OBJECT_EVENT.get(event, event)
    if event == HOOK_EVENT_STOP:
        out["assistant_response"] = payload.get("assistant_text", "")
    if not tool_names:
        return out
    first = tool_names[0]
    builtin = KIRO_CLI_BUILTIN_HOOK_NAMES.get(first)
    if builtin is not None:
        out["tool_name"], tags = builtin
        out["tool_tags"] = list(tags)
        if first == "fs_write" and isinstance(payload.get("tool_input"), dict):
            out["tool_input"] = _kiro_cli_write_input(
                payload["tool_input"], _kas_write_id(tool_names)
            )
        return out
    mcp = next((full for n in tool_names if (full := _explicit_mcp_name(n))), None) or next(
        (full for n in tool_names if (full := _fused_mcp_name(n))), None
    )
    if mcp is not None:
        out["tool_name"] = mcp
        out["tool_tags"] = [mcp, f"@{mcp[1:].split('/', 1)[0]}", "@mcp"]
        return out
    out["tool_name"] = first
    out["tool_tags"] = []
    return out


def envelope_named_tool_id(tool_id: str) -> bool:
    """Whether *tool_id* is a call on a harness whose ids come only from Crew's gate envelope.

    On such a harness (Strands) an MCP server's tool is named by its MCP forms
    alone, so the shared matcher never adds its bare name.
    """
    if not tool_id:
        return False
    # circular import: see spec_hook_tool_names.
    from kiro_crew.acp.harness_tool_names import (
        ENVELOPE_NAMED_TOOL_BACKENDS,
        split_harness_tool_id,
    )

    parts = split_harness_tool_id(tool_id)
    return parts is not None and parts[0] in ENVELOPE_NAMED_TOOL_BACKENDS


def post_tool_hook_tool_id(provider: object, *, harness_tool_id: str, is_shell: bool) -> str:
    """The tool id a finished call's PostToolUse spec hooks match on, or "".

    kiro-cli matches a postToolUse hook against its own tool name. KAS's tool-call
    frame names no built-in tool (its ``_meta.kiro.toolId`` is set for a few
    non-built-in ones only) and titles the call in prose, so the title alone
    meets no tool matcher a spec writes. The call is named by what the turn
    learned of it instead: the id its permission request stated
    (*harness_tool_id*), else, for a call the frame classified as a shell
    command, KAS's shell tool, which :func:`spec_hook_tool_names` reads as
    kiro-cli's ``execute_bash`` -- the one tool kiro-cli runs a command with.

    claude names its shell call the same way, as its frames carry no tool id. A codex
    ``tool_call`` event carries its own id (``codex#exec_command``, ``codex#apply_patch``,
    :func:`kiro_crew.acp._dispatch_parts.tool_call_identity.codex_tool_name`), so the
    fallback names only a codex call that event left unnamed; a codex MCP call is
    kind ``execute`` too, but its MCP marker classifies it not shell.

    "" on a backend whose harness runs the spec's hooks itself, and for a call
    nothing names, which keeps the title matching it had.
    """
    if not capabilities_of(provider).crew_fires_spec_observer_hooks:
        return ""
    if harness_tool_id:
        return harness_tool_id
    if not is_shell:
        return ""
    # circular import: see spec_hook_tool_names.
    from kiro_crew.acp.harness_tool_names import ENVELOPE_NAMED_TOOL_BACKENDS
    from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL

    # On Strands the shell verdict a card carries is the CLI's guess from the tool
    # name, so only the id the gate envelope stated names the call.
    if capabilities_of(provider).backend in ENVELOPE_NAMED_TOOL_BACKENDS:
        return ""

    shell_ids = KAS_TOOL_IDS_BY_KIRO_TOOL.get("execute_bash") or ()
    return shell_ids[0] if shell_ids else ""


class PostToolIdentity(NamedTuple):
    """What a finished call's PostToolUse hooks are matched on, kept per call.

    The harness id (:func:`post_tool_hook_tool_id`), and the call's canonical tool
    name and trusted MCP server, so a spec matcher written ``@server/tool`` meets an
    MCP call by those forms as it does on PreToolUse
    (``hooks.pre_tool_match_names``).
    """

    tool_id: str = ""
    tool_identity: str = ""
    mcp_server: str = ""


def post_tool_identity(
    provider: object,
    event: object,
    *,
    is_shell: bool,
    earlier: PostToolIdentity | None = None,
) -> PostToolIdentity | None:
    """*event*'s :class:`PostToolIdentity`, or ``None`` where Crew fires no
    PostToolUse spec hooks.

    *event* is the call's ``tool_call`` frame or its permission request; a field
    the event leaves empty keeps what *earlier* recorded for the same call.
    """
    if not capabilities_of(provider).crew_fires_spec_observer_hooks:
        return None
    base = earlier or PostToolIdentity()
    stated = getattr(event, "harness_tool_id", "") or ""
    tool_id = post_tool_hook_tool_id(provider, harness_tool_id=stated, is_shell=is_shell)
    identity = getattr(event, "tool_name", "") or ""
    server = getattr(event, "mcp_server_name", "") or ""
    return PostToolIdentity(
        tool_id=tool_id or base.tool_id,
        tool_identity=identity if isinstance(identity, str) and identity else base.tool_identity,
        mcp_server=server if isinstance(server, str) and server else base.mcp_server,
    )


def _reject(agent_id: str, event: object, value: object, reason: str) -> None:
    """Warn about, and SEL-audit, a spec hook that will not run.

    The same audit the kiro-cli merge writes for a hook it leaves out
    (``agent._sel_hook_rejected``, which redacts before it truncates), because what
    runs differs from what was authored either way. The conversion is cached by
    content, so an unchanged spec audits once, not once per turn.
    """
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _sel_hook_rejected

    logger.warning(
        "agent %r: spec hook on %s does not run on this backend: %s",
        agent_id,
        _diagnostic(event),
        reason,
    )
    _sel_hook_rejected(str(event), str(value), f"spec hook for {agent_id}: {reason}")


class OverlongHookCommand(NamedTuple):
    """A spec hook left out because its command is over its form's limit.

    ``event`` is the event or trigger as the spec spells it, ``matcher`` its
    matcher ("" when none), ``length`` the command's length in characters and
    ``limit`` the bound it passed: :data:`_MAX_COMMAND_LEN` for the object form,
    the document validator's payload cap for a hook document.
    """

    event: str
    matcher: str
    length: int
    limit: int


def _overlong_reason(matcher: object, length: int, limit: int) -> str:
    """The log reason for a command over the limit, naming the fix."""
    shown = _diagnostic(matcher) if isinstance(matcher, str) and matcher else "none"
    return (
        f"command is {length:,} characters (matcher {shown}), over the "
        f"{limit:,}-character limit; put the logic in a script file "
        "and call that script from the hook"
    )


def _overlong_commands(spec: dict[str, Any]) -> list[OverlongHookCommand]:
    """The spec's hooks that are left out only because their command is too long.

    Reads either shape (see the module docstring). An entry with an unknown event
    or trigger is left out, since it is dropped for that reason first.
    """
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import (
        _KAS_TRIGGER_CANONICAL,
        _MAX_HOOK_PAYLOAD_LEN,
        _MAX_SPEC_HOOK_DOCUMENTS,
        _event_for_hook_trigger,
    )

    value = spec.get("hooks")
    found: list[OverlongHookCommand] = []
    if isinstance(value, dict):
        for key, entries in value.items():
            event = _OBJECT_EVENT_TO_HOOK_EVENT.get(key) if isinstance(key, str) else None
            if event is None or not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if isinstance(command, str) and len(command) > _MAX_COMMAND_LEN:
                    matcher = entry.get("matcher") if event in _TOOL_EVENTS else None
                    found.append(
                        OverlongHookCommand(
                            key,
                            matcher if isinstance(matcher, str) else "",
                            len(command),
                            _MAX_COMMAND_LEN,
                        )
                    )
    elif isinstance(value, list):
        # The normalizer's own document slice and trigger aliases, so a hook it
        # would drop for another reason is not blamed on its length.
        for doc in value[:_MAX_SPEC_HOOK_DOCUMENTS]:
            if not isinstance(doc, dict):
                continue
            trigger = doc.get("trigger")
            canonical = (
                _KAS_TRIGGER_CANONICAL.get(trigger.lower()) if isinstance(trigger, str) else None
            )
            action = doc.get("action")
            if (
                not isinstance(action, dict)
                or action.get("type") != "command"
                or not _event_for_hook_trigger(canonical)
            ):
                continue
            command = action.get("command")
            # The normalizer drops a document over its payload cap before
            # :func:`_hook` sees it, so the document's own cap is the limit here.
            if isinstance(command, str) and len(command) > _MAX_HOOK_PAYLOAD_LEN:
                matcher = doc.get("matcher")
                found.append(
                    OverlongHookCommand(
                        str(trigger),
                        matcher if isinstance(matcher, str) else "",
                        len(command),
                        _MAX_HOOK_PAYLOAD_LEN,
                    )
                )
    return found


def overlong_spec_hook_commands(
    agent_id: str,
    project_dir: str | None = None,
    *,
    session_key: str = "",
    kas_checkout: str | None = None,
) -> list[OverlongHookCommand]:
    """The active spec's hooks that do not run because their command is too long.

    For the session-start notice. Reads the spec :func:`crew_fired_spec_hooks`
    reads, *kas_checkout* included (a KAS session's folder agent's own hooks when
    the folder's choice lets them run); raises when it cannot be read.
    """
    return _overlong_commands(
        _agent_spec(agent_id, project_dir, session_key, kas_checkout=kas_checkout)
    )


def _hook(
    agent_id: str, event: str, index: int, entry: dict, timeout: int, max_output_size: int = 0
) -> ScriptHook | None:
    command = entry.get("command")
    if isinstance(command, str) and len(command) > _MAX_COMMAND_LEN:
        _reject(
            agent_id,
            event,
            command,
            f"command is {len(command)} characters, past the {_MAX_COMMAND_LEN} limit; "
            "move it into a script file and call that from the hook",
        )
        return None
    if not isinstance(command, str) or not command.strip():
        _reject(agent_id, event, command, "no usable command")
        return None
    matcher = entry.get("matcher") if event in _TOOL_EVENTS else None
    if matcher is not None and not _matcher_ok(matcher):
        # Dropped whole, as the materialized spec's merge drops it: running the
        # hook with no matcher would widen it to every tool.
        _reject(agent_id, event, command, "invalid matcher")
        return None
    if (
        event in _TOOL_EVENTS
        and isinstance(matcher, str)
        and matcher
        and not _matcher_names_a_kas_tool(matcher)
    ):
        # Kept, not dropped: the matcher still meets a harness tool of that name (an
        # MCP tool, a built-in no table has a row for), and dropping it would retire
        # a guard the harness can serve. Said once, since a kiro-cli-only name
        # (``use_aws``) never meets a harness call.
        logger.warning(
            "agent %r: spec hook matcher %s on %s names no tool in Crew's tool tables; "
            "it runs only for a harness tool id it matches as written",
            agent_id,
            _diagnostic(matcher),
            _diagnostic(event),
        )
    return ScriptHook(
        id=f"spec:{agent_id}:{event}:{index}",
        name=f"{agent_id} spec hook ({event} #{index + 1})",
        event=event,
        matcher=matcher if isinstance(matcher, str) else "",
        command=command,
        timeout=timeout,
        max_output_size=max_output_size,
    )


def _output_limit(value: object) -> int:
    """An object-form entry's ``max_output_size`` in bytes, or ``0`` for none.

    kiro-cli reads a positive byte count. Any other value, a bool included, leaves
    only the stream cap, and a larger count is clamped to that cap, which bounds
    every hook's output anyway.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, _HOOK_STREAM_CAP_BYTES)
    return 0


def _from_object_form(agent_id: str, hooks: dict, past_cap: list[str]) -> list[ScriptHook]:
    out: list[ScriptHook] = []
    for key, entries in hooks.items():
        event = _OBJECT_EVENT_TO_HOOK_EVENT.get(key) if isinstance(key, str) else None
        if event is None or not isinstance(entries, list):
            _reject(agent_id, key, entries, "not a hook event list")
            continue
        for index, entry in enumerate(entries):
            if len(out) >= _MAX_SPEC_HOOKS:
                past_cap.append(str(key))
                continue
            if not isinstance(entry, dict):
                _reject(agent_id, event, entry, "entry is not an object")
                continue
            timeout_ms = entry.get("timeout_ms")
            timeout = (
                _normalize_hook_timeout(math.ceil(timeout_ms / 1000))
                if isinstance(timeout_ms, (int, float)) and not isinstance(timeout_ms, bool)
                else _normalize_hook_timeout(None)
            )
            hook = _hook(
                agent_id, event, index, entry, timeout, _output_limit(entry.get("max_output_size"))
            )
            if hook is None:
                continue
            cache_ttl = entry.get("cache_ttl_seconds")
            if (
                isinstance(cache_ttl, (int, float))
                and not isinstance(cache_ttl, bool)
                and cache_ttl > 0
            ):
                # Named, not honoured: honouring it needs a per-session result
                # cache, and without one the hook still runs correctly, only more
                # often. The conversion is cached by content, so an unchanged spec
                # is named once. Worded for every caller: the notice for a backend
                # where spec hooks run nowhere reads this conversion too.
                logger.warning(
                    "agent %r: spec hook on %s sets cache_ttl_seconds, which Crew does "
                    "not honour; where Crew runs the hook, it runs on every event",
                    agent_id,
                    _diagnostic(key),
                )
            out.append(hook)
    return out


def _from_documents(
    agent_id: str,
    hooks: list,
    unconfirmable: list[int],
    cap_overflow: list[int] | None = None,
    past_cap: list[str] | None = None,
) -> list[ScriptHook]:
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _event_for_hook_trigger, normalize_spec_hooks

    # The normalizer drops a document whose command is too long before this loop
    # sees it, so name the hook and the fix here.
    for overlong in _overlong_commands({"hooks": hooks}):
        logger.warning(
            "agent %r: spec hook on %s does not run on this backend: %s",
            agent_id,
            _diagnostic(overlong.event),
            _overlong_reason(overlong.matcher, overlong.length, overlong.limit),
        )
    out: list[ScriptHook] = []
    for index, doc in enumerate(normalize_spec_hooks(hooks, matcher_ok=_matcher_ok)):
        if past_cap is not None and len(out) >= _MAX_SPEC_HOOKS:
            past_cap.append(str(doc.get("trigger")))
            continue
        if len(out) > _MAX_SPEC_HOOKS:
            break
        trigger = doc.get("trigger")
        raw_action = doc.get("action")
        action: dict = raw_action if isinstance(raw_action, dict) else {}
        command = action.get("command")
        if doc.get("enabled") is False:
            _reject(agent_id, trigger, command, "hook is disabled")
            continue
        if doc.get("confirm") is True:
            unconfirmable.append(index)
            _reject(
                agent_id,
                trigger,
                command,
                "hook asks to be confirmed, which Crew cannot prompt for",
            )
            continue
        event = _OBJECT_EVENT_TO_HOOK_EVENT.get(_event_for_hook_trigger(trigger) or "")
        if event is None or action.get("type") != "command":
            _reject(agent_id, trigger, command, "no Crew hook event can run this trigger or action")
            continue
        # The document's timeout is in seconds, clamped like a Hooks-page hook's.
        hook = _hook(
            agent_id,
            event,
            index,
            {"command": command, "matcher": doc.get("matcher")},
            _normalize_hook_timeout(doc.get("timeout")),
        )
        if hook is not None:
            # Pure side-count: a document that converts to a runnable hook while
            # the kept list is already at the cap is recorded for the caller that
            # asked (``cap_overflow``) and still appended, so the cap / break and
            # what is kept are exactly as on the no-counter path. The spec-hook
            # callers pass no list and ``_convert`` audits the overflow as before.
            if cap_overflow is not None and len(out) >= _MAX_SPEC_HOOKS:
                cap_overflow.append(index)
            out.append(hook)
    return out


def spec_script_hooks(agent_id: str, spec: dict[str, Any]) -> list[ScriptHook]:
    """The spec's own ``hooks`` as script hooks the hook store can fire.

    Empty when the spec carries none. Either shape is read (see the module
    docstring); anything else is warned about, audited once, and yields nothing.
    """
    return list(_convert(agent_id, spec)[0])


def project_hook_files_present(project_dir: str | None) -> bool:
    """Whether the session's checkout has standalone ``<project>/.kiro/hooks/*.json``.

    These files do NOT run in a dashboard KAS session: a project hook is a command
    from a checkout, gated on the trust verdict Crew cannot read
    (:func:`kiro_crew.acp.session_mcp._project_mcp_trusted`). This only reports that
    such files exist, so a session-start notice can say they are withheld rather
    than let them fail silently -- the same shape as the withheld project spec
    hooks notice. Best-effort: any read error answers ``False`` (no notice) rather
    than failing the turn.
    """
    if not project_dir:
        return False
    try:
        hooks_dir = Path(project_dir) / ".kiro" / "hooks"
        return any(p.is_file() for p in hooks_dir.glob("*.json"))
    except OSError:
        return False


def _user_hooks_dir() -> Path | None:
    """The directory standalone user hook files are read from, or ``None``.

    ``agent.kiro_hooks_dir`` in ``~/.kiro/crew/config.json`` (default
    ``~/.kiro/hooks``), resolved through the ``kiro_crew.agent`` facade
    (:func:`kiro_crew.agent.resolved_kiro_hooks_dir`) and required to live under
    the user's HOME and outside a sensitive directory -- the same containment the
    ``~/.kiro/hooks`` script autoimport applies. An unresolvable or out-of-HOME
    setting answers ``None`` (fail closed), never an unchecked path. The user's own
    files need no per-workspace trust gate, the way project files do: this is the
    user's HOME, not a checkout.
    """
    # Deferred import: ``agent`` imports ``hooks``, which this module imports at
    # load time, so resolving the dir at call time avoids the import cycle.
    from kiro_crew import agent as agent_mod

    try:
        return agent_mod.resolved_kiro_hooks_dir()
    except Exception:  # noqa: BLE001 - a missing dir must never fail the turn
        logger.debug("user hook files: could not resolve the hooks directory", exc_info=True)
        return None


def _hook_file_bytes(path: Path, within_root: Path) -> bytes | None:
    """Bytes of one hook file, read symlink- and containment-safe, or ``None``.

    Reads through :func:`kiro_crew.hooks.safe_read_file_bytes_nolink` pinned to
    *within_root* and :data:`_MAX_HOOK_FILE_BYTES`: a symlink at the final
    component, a hardlinked inode, a non-regular file, a target escaping the hooks
    directory, a sensitive path, or an oversized file is refused and read as
    ``None``. So a ``*.json`` entry that is a link to a credential file is never
    read as a hook file. A read failure degrades to ``None`` rather than raising.
    """
    from kiro_crew.hooks import safe_read_file_bytes_nolink

    try:
        return safe_read_file_bytes_nolink(
            str(path), str(within_root), max_bytes=_MAX_HOOK_FILE_BYTES
        )
    except Exception:  # noqa: BLE001 - a hand-placed file must never fail a turn
        logger.debug("user hook file %s could not be read safely", _diagnostic(str(path)))
        return None


def _hook_documents_from_bytes(path: Path, raw: bytes) -> list | None:
    """The ``hooks`` array of one v1 hook file's bytes, or ``None`` for none.

    A JSON error, a non-object top level, or a ``hooks`` that is not a list skips
    the file with one log line rather than raising. The ``version`` field is not
    required: the document validator downstream accepts or rejects each entry.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.info("user hook file %s is not valid JSON, skipping", _diagnostic(str(path)))
        return None
    if not isinstance(data, dict):
        logger.info("user hook file %s is not a JSON object, skipping", _diagnostic(str(path)))
        return None
    hooks = data.get("hooks")
    if not isinstance(hooks, list) or not hooks:
        return None
    return hooks


def user_hook_files() -> list[ScriptHook]:
    """The user's standalone ``~/.kiro/hooks/*.json`` hooks, as script hooks.

    These v1 files are what ``kiro-cli chat --agent-engine v3`` runs. Each file's
    ``hooks`` array is converted through the SAME document path a spec's array form
    uses (:func:`_from_documents`): only ``action.type == "command"`` documents on
    a known trigger (SessionStart/AgentSpawn, UserPromptSubmit, PreToolUse,
    PostToolUse, Stop) become hooks; an ``agent`` action, an unknown trigger, a
    disabled or ``confirm: true`` document, or a malformed entry is skipped and
    logged once, never guessed. The returned hooks ride the turn's ``extra_hooks``,
    so they meet the shell deny list, the SEL audit and ``capabilities.script_hooks``
    like a Hooks-page hook, and carry ``spec_origin="user_file"`` so PreToolUse
    follows kiro-cli's exit-code contract -- a delivered exit 2 blocks the tool; a
    crash or timeout does not -- the same as an agent spec's own hooks, which is the
    parity this exists for.

    Only the USER directory is read, symlink- and containment-safe. Project
    ``<project>/.kiro/hooks/*.json`` are not read here; they stay off on the trust
    verdict Crew cannot read for a checkout
    (:func:`kiro_crew.acp.session_mcp._project_mcp_trusted`). The total is bounded
    by :data:`_MAX_SPEC_HOOKS`, the same cap a spec's hooks share.

    Each file's conversion is cached by its path and content digest, so an
    unchanged file is converted -- and its skipped documents audited -- once rather
    than on every turn. Never raises: a missing directory, or an unreadable or
    malformed file, degrades to fewer hooks rather than a failed turn.
    """
    hooks_dir = _user_hooks_dir()
    if hooks_dir is None:
        return []
    try:
        files = sorted(p for p in hooks_dir.glob("*.json") if p.is_file())
    except OSError:
        logger.debug("user hook files: could not list %s", _diagnostic(str(hooks_dir)))
        return []
    out: list[ScriptHook] = []
    dropped_files = max(0, len(files) - _MAX_HOOK_FILES)
    # A cap truncated the hooks that run. The per-file side-count (``cap_overflow``)
    # only knows a file had at least one convertible hook beyond the cap -- not how
    # many -- and the global cap below only knows it stopped appending, so this is
    # a flag, not a count. A disabled / confirm-only / unknown-trigger / bad-matcher
    # document took its own reject path and is never a cap truncation.
    hooks_truncated = False
    # Digest of the whole directory snapshot. EVERY file name is in the key (names
    # only past the cap), so a file added beyond the cap changes the snapshot and
    # is reported; the content digest of each READ file is folded in too, so an
    # edit within the cap is reported. One report per CHANGED snapshot, not per
    # turn.
    snapshot = hashlib.sha256()
    for name in (p.name for p in files):
        snapshot.update(name.encode("utf-8", "replace"))
        snapshot.update(b"\0")
    for path in files[:_MAX_HOOK_FILES]:
        raw = _hook_file_bytes(path, hooks_dir)
        if raw is None:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        snapshot.update(digest.encode("ascii"))
        key = (str(path), digest)
        cached = _user_file_cache.get(key)
        if cached is None:
            documents = _hook_documents_from_bytes(path, raw)
            if not documents:
                converted: tuple[ScriptHook, ...] = ()
                over_cap = False
            else:
                # ``_from_documents`` runs the same enabled/trigger/action/matcher
                # screening a spec array gets; ``cap_overflow`` records a document
                # it would have converted but for the per-file cap, so a disabled or
                # unknown-trigger document is never read as a cap truncation.
                agent_id = f"userfile:{path.stem}"
                overflow: list[int] = []
                converted = tuple(
                    dataclasses.replace(h, spec_origin=_USER_FILE_ORIGIN)
                    for h in _from_documents(agent_id, documents, [], overflow)
                )
                over_cap = bool(overflow)
            if len(_user_file_cache) >= _CACHE_MAX:
                _user_file_cache.clear()
                _user_file_doc_counts.clear()
            _user_file_cache[key] = converted
            _user_file_doc_counts[key] = over_cap
            cached = converted
        if _user_file_doc_counts.get(key):
            hooks_truncated = True
        for hook in cached:
            if len(out) >= _MAX_SPEC_HOOKS:
                # Global cap across files: more convertible hooks than will run.
                hooks_truncated = True
                break
            out.append(hook)
    if dropped_files or hooks_truncated:
        _audit_user_file_truncation(snapshot.hexdigest(), dropped_files, hooks_truncated)
    return out


def _audit_user_file_truncation(snapshot: str, dropped_files: int, hooks_truncated: bool) -> None:
    """Warn and SEL-audit that a cap truncated the user's hook files or hooks, once
    per changed snapshot.

    A silent cut reads downstream as "the user has no more hooks", which is a
    different fact, and a dropped PreToolUse guard neither runs nor withholds
    auto-approval. The number of hooks a cap drops is not known here -- the loader
    stops once the cap is reached -- so the message names the cap, not an exact
    dropped count. Keyed on the directory snapshot digest so an unchanged set is
    reported once, not on every turn.
    """
    if snapshot in _user_file_truncation_audited:
        return
    if len(_user_file_truncation_audited) >= _CACHE_MAX:
        _user_file_truncation_audited.clear()
    _user_file_truncation_audited.add(snapshot)
    parts = []
    if dropped_files:
        parts.append(
            f"more than {_MAX_HOOK_FILES} files (only the first {_MAX_HOOK_FILES} are read)"
        )
    if hooks_truncated:
        parts.append(f"more than {_MAX_SPEC_HOOKS} hooks (only the first {_MAX_SPEC_HOOKS} run)")
    reason = (
        "user hook files exceed the caps: "
        + " and ".join(parts)
        + "; the rest will not run or gate auto-approval"
    )
    logger.warning("user hook files: %s", reason)
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _sel_hook_rejected

    _sel_hook_rejected("user_hook_files", "caps", reason)


def merge_user_hook_files(provider: object, base_hooks: list[ScriptHook]) -> list[ScriptHook]:
    """*base_hooks* (the spec's own hooks) plus the user's standalone files, on KAS.

    The ONE place every KAS turn -- dashboard, sub-agent and task-runner -- folds
    :func:`user_hook_files` into the hooks it fires, so a user's
    ``~/.kiro/hooks/*.json`` hook runs on each of them and the projection's
    auto-approve withhold (which also lists these files) always has a hook to fire.
    Returns *base_hooks* unchanged off KAS: goose and opencode are in
    ``crew_fires_spec_hooks`` too but do not read a user's standalone files.

    A user-file hook whose ``(event, command, matcher)`` already appears in
    *base_hooks* is dropped -- the spec / Hooks-page hook wins -- so a JSON file
    whose command is a ``*.sh`` script the autoimport already folded into the spec
    is not fired twice.
    """
    # Deferred: ``backends`` imports modules that reach this one, so resolve the
    # backend id at call time like :func:`spec_project_dir` does.
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    # Positive identity test (harness gate H5): name the one backend this folds in,
    # so a harness added later does not inherit it by an inequality.
    if capabilities_of(provider).backend == ACP_BACKEND_KAS:
        seen = {(h.event, h.command, h.matcher) for h in base_hooks}
        merged = list(base_hooks)
        for hook in user_hook_files():
            key = (hook.event, hook.command, hook.matcher)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hook)
        return merged
    return base_hooks


def _convert(agent_id: str, spec: dict[str, Any]) -> tuple[tuple[ScriptHook, ...], int]:
    value = spec.get("hooks")
    if not value:
        return (), 0
    try:
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
    except (TypeError, ValueError):
        digest = ""
    key = (agent_id, digest)
    if digest and key in _cache:
        return _cache[key]
    unconfirmable: list[int] = []
    past_cap: list[str] = []
    if isinstance(value, dict):
        hooks = _from_object_form(agent_id, value, past_cap)
    elif isinstance(value, list):
        hooks = _from_documents(agent_id, value, unconfirmable, past_cap=past_cap)
    else:
        _reject(agent_id, "hooks", value, "hooks is neither an object nor an array")
        hooks = []
    if past_cap:
        # Named, not just counted: a PreToolUse entry past the cap is a guard
        # that neither runs nor withholds auto-approval.
        _reject(
            agent_id,
            "hooks",
            len(past_cap),
            f"only the first {_MAX_SPEC_HOOKS} spec hooks run; {len(past_cap)} more "
            f"are not read ({', '.join(sorted(set(past_cap)))})",
        )
    result = (tuple(hooks), len(unconfirmable))
    if digest:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = result
    return result


def _agent_spec(
    agent_id: str,
    project_dir: str | None = None,
    session_key: str = "",
    *,
    kas_checkout: str | None = None,
) -> dict[str, Any]:
    """*agent_id*'s spec, or ``{}`` when it has none on disk.

    *project_dir* is the session's checkout on a backend that resolves its spec
    project-nearest first (goose, opencode; see ``overlay_project_scope``). There a
    project spec of that name is the one the session runs, but its hooks are
    commands, so they are read only when the checkout is trusted to choose them
    (``session_mcp.trusted_project_agent_spec``, the verdict that also gates its
    ``mcpServers``). An untrusted one yields the user-level spec of that name, and
    a project spec that cannot be read raises. KAS reads the user level alone, so
    it passes none.

    A KAS mode switch can move a session to one of KAS's own built-in modes
    (``vibe``), which has no Crew spec: it carries no spec hooks, and the Hooks
    page's hooks still apply to it. Reading that as an unreadable spec would
    refuse every call the session makes and leave it unusable. A spec that exists
    but cannot be read still raises, so callers keep failing closed on it.

    Given *session_key*, a crewmate's worker is read as that
    session's merged spec (``mate_worker.mate_service_scope_spec``), the one its
    projection was handed, so the hooks its crewmate's file carries are the ones
    that fire.

    *kas_checkout* is a KAS session's own folder (:func:`kas_hook_checkout`): a
    project-only agent's ``hooks`` are read from it when the folder's project-hooks
    choice is on (:func:`_consented_kas_project_hooks`).
    """
    return _agent_spec_with_origin(agent_id, project_dir, session_key, kas_checkout=kas_checkout)[0]


def _agent_spec_with_origin(
    agent_id: str,
    project_dir: str | None = None,
    session_key: str = "",
    *,
    kas_checkout: str | None = None,
) -> tuple[dict[str, Any], str]:
    """:func:`_agent_spec` and where it came from: ``"project"`` or ``"user"``."""
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.kas_agents import agent_spec_absent, load_agent_spec
    from kiro_crew.acp.session_mcp import trusted_project_agent_spec
    from kiro_crew.agent_files import is_mate_service_agent
    from kiro_crew.config.paths import spec_dir_for

    if session_key and is_mate_service_agent(agent_id):
        from kiro_crew.mate_worker import mate_service_scope_spec

        scoped = mate_service_scope_spec(agent_id, session_key)
        if scoped is not None:
            merged, _path = scoped
            if merged is None:
                raise ValueError(f"the session spec for agent {agent_id!r} could not be read")
            # The crewmate folder is not the sealed user tree, so its hooks stay
            # vetted as a project spec's are (see :func:`_user_dir_origin`).
            return merged, "project"

    if project_dir:
        declared, spec = trusted_project_agent_spec(agent_id, project_dir)
        if declared:
            if spec is None:
                raise ValueError(f"the project spec for agent {agent_id!r} could not be read")
            return spec, "project"
    agents_dir = spec_dir_for(agent_id)
    if agent_spec_absent(agents_dir, agent_id):
        if kas_checkout:
            consented = _consented_kas_project_hooks(agent_id, kas_checkout)
            if consented is not None:
                return consented, "project"
        return {}, "user"
    return load_agent_spec(agents_dir, agent_id), _user_dir_origin(agents_dir)


def _user_dir_origin(agents_dir: Path) -> str:
    """``"user"`` when *agents_dir* is the sealed kiro agents tree and no spec file
    in it is a symlink, else ``"project"``.

    The read-only seal covers :func:`kiro_agents_dir` itself, not a sibling tree
    (the mates directory) nor a symlink's target, so a spec from either could be
    agent-writable and keeps its hooks deny-list vetted. Any symlink counts, even
    one that resolves inside the tree today: a link it passes through may be
    agent-writable and retargeted after the spec was read. Whether an entry IS a
    symlink cannot change, because the directory itself is read-only.
    """
    from kiro_crew.config.paths import kiro_agents_dir

    try:
        root = agents_dir.resolve(strict=True)
        if root != kiro_agents_dir().resolve(strict=True):
            return "project"  # only the kiro agents tree is sealed read-only
        for entry in agents_dir.iterdir():
            if entry.suffix not in (".json", ".md"):
                continue
            if entry.is_symlink():
                return "project"
    except OSError:
        return "project"
    return "user"


def kas_spec_load_error(agents_dir: Path, agent_id: str) -> str | None:
    """Why KAS session creation would refuse *agent_id*'s spec, or ``None``.

    Runs KAS's two LOADING steps and nothing else: ``load_agent_spec``, then
    ``resolve_prompt`` for a non-blank prompt. Either raising
    ``KasAgentTranslationError`` refuses a KAS session, and its message is
    returned. It stops before ``to_client_custom_agent``, which consults the
    governance ceiling and writes SEL records. Any other exception propagates.
    """
    # circular import: kas_agents imports this module at load time.
    from kiro_crew.acp.kas_agents import (
        KasAgentTranslationError,
        KasPromptFileUnreadable,
        load_agent_spec,
        resolve_prompt,
    )
    from kiro_crew.acp.kas_prompt import spec_prompt

    try:
        spec = load_agent_spec(agents_dir, agent_id)
        prompt = spec_prompt(spec)
        # A blank or absent prompt takes resolve_prompt's constant fallback and
        # cannot fail; skipping it avoids that function's warning per probe.
        if not (prompt is None or (isinstance(prompt, str) and not prompt.strip())):
            resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    except KasPromptFileUnreadable:
        # KAS runs the agent on its default prompt (``build_kas_custom_agents``).
        return None
    except KasAgentTranslationError as exc:
        return str(exc)
    return None


def withheld_project_spec_hooks(agent_id: str, project_dir: str | None) -> bool:
    """Whether *project_dir*'s own spec for *agent_id* sets ``hooks`` that do not run.

    The agent-SDK face of ``session_mcp.project_spec_hooks_withheld``, for the
    session-start notice. ``False`` when there is no checkout (KAS).
    """
    if not project_dir:
        return False
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.session_mcp import project_spec_hooks_withheld

    return project_spec_hooks_withheld(agent_id, project_dir)


def spec_project_dir(provider: object) -> str | None:
    """The checkout *provider*'s session resolves its agent spec against, or ``None``.

    The session's own work dir on a backend that resolves project-nearest first,
    as its MCP projection does (``agent_sdk.backends.overlay_project_scope``);
    ``None`` on one that reads the user level alone (KAS).
    """
    from kiro_crew.agent_sdk.backends import overlay_project_scope

    cwd = getattr(provider, "cwd", "")
    if not isinstance(cwd, str) or not cwd:
        return None
    scope = overlay_project_scope(capabilities_of(provider).backend, cwd)
    work_dir = scope.get("work_dir")
    return work_dir if isinstance(work_dir, str) and work_dir else None


def crew_fired_spec_hooks(
    agent_id: str,
    project_dir: str | None = None,
    *,
    translates_tools_settings: bool = False,
    session_key: str = "",
    discards_mcp_oauth: bool = False,
    withholds_oauth_secret: bool = False,
    governance_session_key: str = "",
    governance_app: str | None = None,
    kas_checkout: str | None = None,
) -> tuple[list[ScriptHook], list[str], int]:
    """The active agent spec's hooks as script hooks, the keys nothing carries, and
    how many ``confirm: true`` hooks are skipped.

    Reads the spec the KAS projection reads, through the same reader
    (:func:`kiro_crew.acp.kas_agents.load_agent_spec`). The second and third values
    are for the session-start notice: the spec keys a KAS session runs without, and
    the hooks that wait for a confirmation Crew cannot ask for. Raises when the spec
    cannot be read; the caller fails PreToolUse closed on that. *project_dir* is
    :func:`spec_project_dir` of the session. *translates_tools_settings* is true
    only for a KAS session, the one backend that receives ``toolsSettings`` as
    ``permissions`` rules. *discards_mcp_oauth* is true only for a KAS session whose
    engine Crew knows drops a remote MCP server's ``oauth`` block.
    *withholds_oauth_secret* is true for every KAS session: its projection sends
    nothing, on any engine, from a block that names a ``clientSecret`` or keeps no
    key it can send. *session_key*
    reads a crewmate's worker as that session's merged spec (:func:`_agent_spec`).
    *governance_session_key* and *governance_app* are the session and app the KAS
    projection judged a ``trustedAgents`` allow for, so the notice judges it alike.
    *kas_checkout* is :func:`kas_hook_checkout` of the session.
    """
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.kas_agents import spec_keys_without_carrier

    spec, origin = _agent_spec_with_origin(
        agent_id, project_dir, session_key, kas_checkout=kas_checkout
    )
    hooks, unconfirmable = _convert(agent_id, spec)
    lost = spec_keys_without_carrier(
        spec,
        translates_tools_settings=translates_tools_settings,
        discards_mcp_oauth=discards_mcp_oauth,
        withholds_oauth_secret=withholds_oauth_secret,
        session_key=governance_session_key,
        governance_app=governance_app,
    )
    # Copies, so the cached conversion keeps no origin: the same hooks can come
    # from a user-level spec in one session and a project spec in another.
    tagged = [dataclasses.replace(h, spec_origin=origin) for h in hooks]
    return tagged, lost, unconfirmable


def spec_hooks_run_nowhere(provider: object) -> bool:
    """Whether a session on *provider* runs none of its agent spec's ``hooks``.

    True where the backend's mirror rules the hooks concern ``no-channel`` or
    ``withheld``, or where a ``broker-only`` backend (pi) has no mirror to send the
    field and Crew does not fire it (``backend_mcp_ability._broker_only_dispositions``).
    False where the harness runs the field itself (kiro-cli), where Crew fires it
    (``crew_fires_spec_hooks``) or all of it but ``preToolUse``
    (``crew_fires_spec_observer_hooks``, claude and codex, whose ``preToolUse``
    entries get their own notice), and for a backend with no ruling, about which
    nothing is established.
    """
    caps = capabilities_of(provider)
    if caps.crew_fires_spec_hooks or caps.crew_fires_spec_observer_hooks:
        return False
    ability = ability_for(caps.backend)
    return _HOOKS_CONCERN in ability.no_channel or _HOOKS_CONCERN in ability.withheld


def _mirror_rules_unapplied(provider: object, concern: Concern) -> bool:
    """Whether *provider*'s mirror rules *concern* ``withheld`` or ``no-channel``.

    False for a backend with no mirror and for one whose rulings cannot be read,
    about which nothing is established.
    """
    # circular import: the mirrors package reaches back into agent_sdk.
    from kiro_crew.providers.mirrors import mirror_for
    from kiro_crew.providers.mirrors.base import Disposition

    try:
        mirror = mirror_for(capabilities_of(provider).backend)
        ruling = mirror.rulings().get(concern) if mirror is not None else None
    except Exception:  # noqa: BLE001 - an unreadable ruling establishes nothing
        return False
    return ruling is not None and ruling.disposition in (
        Disposition.WITHHELD,
        Disposition.NO_CHANNEL,
    )


def spec_steering_read_nowhere(provider: object) -> bool:
    """Whether a session on *provider* loads none of its spec's ``file://`` steering.

    True where the backend's mirror rules ``resources`` ``withheld`` or
    ``no-channel``: the harness is handed no spec, and Crew's own steering load
    serves only the default agent, on Claude Code and Codex
    (``ACP_BACKENDS_CREW_LOADS_SPEC_STEERING``,
    ``providers.mirrors.base.RESOURCES_NO_READER``). False for a backend with no
    mirror (kiro-cli and KAS read ``resources`` themselves) and for one whose
    rulings cannot be read, about which nothing is established.
    """
    # circular import: the mirrors package reaches back into agent_sdk.
    from kiro_crew.providers.mirrors.base import Concern

    return _mirror_rules_unapplied(provider, Concern.RESOURCES)


def spec_tools_settings_applied_nowhere(provider: object) -> bool:
    """Whether a session on *provider* applies none of its spec's ``toolsSettings``.

    True where the backend's mirror rules the ``toolsSettings`` concern
    ``withheld`` or ``no-channel``. False for a backend with no mirror (kiro-cli
    reads the field itself, and KAS receives it as ``permissions`` rules and gate
    allowlists) and for one whose rulings cannot be read, about which nothing is
    established.
    """
    # circular import: the mirrors package reaches back into agent_sdk.
    from kiro_crew.providers.mirrors.base import Concern

    return _mirror_rules_unapplied(provider, Concern.TOOLS_SETTINGS)


def spec_tools_settings_unapplied(
    agent_id: str, project_dir: str | None = None, *, gate_allowlists: bool = False
) -> list[str]:
    """The active agent spec's ``toolsSettings`` names a session that is not handed it runs without.

    For the session-start notice on a backend where
    :func:`spec_tools_settings_applied_nowhere` holds; empty when the spec sets
    nothing such a session loses
    (:func:`~kiro_crew.agent_sdk.tools_settings_reach.tools_settings_unapplied`).
    Read through the spec reader the hooks use, so it is the spec the session
    runs. Raises when the spec cannot be read. *project_dir* is
    :func:`spec_project_dir` of the session. *gate_allowlists* is passed through.
    """
    from kiro_crew.agent_sdk.tools_settings_reach import tools_settings_unapplied

    return tools_settings_unapplied(
        _agent_spec(agent_id, project_dir).get("toolsSettings"), gate_allowlists=gate_allowlists
    )


def spec_steering_resources(agent_id: str, project_dir: str | None = None) -> list[str]:
    """The ``file://`` entries of the active agent spec's ``resources``, in order.

    For the session-start notice on a backend where
    :func:`spec_steering_read_nowhere` holds. Read through the spec reader the
    hooks use, so it is the spec the session runs. Raises when the spec cannot be
    read. *project_dir* is :func:`spec_project_dir` of the session.
    """
    resources = _agent_spec(agent_id, project_dir).get("resources")
    if not isinstance(resources, list):
        return []
    return [r for r in resources if isinstance(r, str) and r.startswith("file://")]


def spec_hook_events(agent_id: str, project_dir: str | None = None) -> list[str]:
    """The events the active agent spec has hooks on, as an agent spec spells them.

    Each event is named once, in kiro-cli's order. Read through the conversion Crew
    fires from, so an entry that would not run where Crew fires the field (disabled,
    waiting for a confirmation, malformed) is not named. For the session-start
    notice on a backend where :func:`spec_hooks_run_nowhere` holds. Raises when the
    spec cannot be read. *project_dir* is :func:`spec_project_dir` of the session.
    """
    hooks, _unconfirmable = _convert(agent_id, _agent_spec(agent_id, project_dir))
    present = {hook.event for hook in hooks}
    return [name for name, event in _OBJECT_EVENT_TO_HOOK_EVENT.items() if event in present]


def _fires_observer_spec_hooks_only(provider: object) -> bool:
    """Whether Crew fires *provider*'s spec hooks on :data:`OBSERVER_HOOK_EVENTS` only.

    True on claude and codex: neither is handed the spec, and each approves some
    calls without a permission request, so a ``preToolUse`` hook could not gate them.
    """
    caps = capabilities_of(provider)
    return caps.crew_fires_spec_observer_hooks and not caps.crew_fires_spec_hooks


def observer_spec_hooks(hooks: list[ScriptHook]) -> list[ScriptHook]:
    """*hooks* without the ones on an event that can refuse a tool call."""
    return [hook for hook in hooks if hook.event in OBSERVER_HOOK_EVENTS]


def post_tool_matchers_no_builtin_meets(provider: object, hooks: Sequence[ScriptHook]) -> list[str]:
    """The ``postToolUse`` matchers of *hooks* that no built-in tool on *provider* meets.

    codex only. Its built-in calls are named through its row in
    ``harness_tool_names.HARNESS_TOOL_TABLES``, so a matcher that names a kiro-cli
    built-in outside that row (``fs_read``, ``grep``, ``glob``, ``web_fetch``,
    ``use_subagent``, ``code``), by its key or by the name kiro-cli's stdin gives it
    (:data:`KIRO_CLI_BUILTIN_HOOK_NAMES`), meets no built-in codex call. It still
    meets an MCP tool with the same name, because ``hooks.pre_tool_match_names``
    adds an MCP call's bare tool name, so the notice says the hook runs only there
    (:func:`unmet_post_tool_matchers_sentence`). A pattern or an MCP matcher is not
    listed. Empty on every other backend.
    """
    # circular import: see spec_hook_tool_names.
    from kiro_crew.acp.harness_tool_names import CODEX_TOOL_IDS_BY_KIRO_TOOL
    from kiro_crew.acp_backends import ACP_BACKEND_CODEX

    if capabilities_of(provider).backend == ACP_BACKEND_CODEX:
        codex_names = {n for ids in CODEX_TOOL_IDS_BY_KIRO_TOOL.values() for n in ids}
        unmet = {
            name
            for kiro_name, (stdin_name, _tags) in KIRO_CLI_BUILTIN_HOOK_NAMES.items()
            if kiro_name not in CODEX_TOOL_IDS_BY_KIRO_TOOL
            for name in (kiro_name, stdin_name)
            if name not in codex_names
        }
        matchers = [h.matcher for h in hooks if h.event == HOOK_EVENT_POST_TOOL_USE]
        return list(dict.fromkeys(m for m in matchers if m in unmet))
    return []


#: The prefix of a :func:`unmet_post_tool_matcher_key`.
_UNMET_POST_TOOL_KEY_PREFIX = "hooks.postToolUse["


def unmet_post_tool_matcher_key(matcher: str) -> str:
    """The spec key that names a ``postToolUse`` *matcher* no built-in tool meets.

    For :attr:`TurnSpecHooks.lost`, so a subagent, task-runner or cron turn names
    it in the notice it already gives for such keys (:func:`spec_keys_notice`),
    which says it with :func:`unmet_post_tool_matchers_sentence`.
    """
    return f"{_UNMET_POST_TOOL_KEY_PREFIX}{matcher}]"


def unmet_post_tool_matchers_sentence(agent: str, matchers: Sequence[str]) -> str:
    """The notice sentence for ``postToolUse`` *matchers* no built-in tool meets.

    The hooks are not said to have no effect: an MCP tool with the same name still
    meets them (:func:`post_tool_matchers_no_builtin_meets`).
    """
    return (
        f"Agent {agent} has postToolUse hooks for {', '.join(matchers)}. No built-in "
        "tool on this backend has that name, so these hooks run only after a call to "
        "an MCP tool with the same name."
    )


class TurnSpecHooks(NamedTuple):
    """What a subagent, task-runner, cron or channel turn needs to gate its permission requests."""

    #: The agent spec's own hooks, as script hooks.
    hooks: list[ScriptHook]
    #: The session's workspace, the ``extra_hooks_cwd`` the hooks run in
    #: (``None`` for the gateway's own).
    cwd: str | None
    #: The spec could not be read: every permission request is refused, as the chat
    #: turn loop does, because a deny hook that was never loaded gave no verdict.
    unreadable: bool
    #: Crew runs the spec's PreToolUse hooks itself on this turn's permission
    #: requests, because its backend's projection sent the calls they cover there.
    #: False for a backend that runs the spec's hooks itself. The Hooks page's
    #: hooks gate a permission request either way.
    gated: bool
    #: The spec keys nothing carries to this backend, for :func:`spec_keys_notice`.
    lost: tuple[str, ...] = ()


_NOT_GATED = TurnSpecHooks([], None, False, False)


def spec_keys_notice(agent: str, keys: list[str]) -> str:
    """The session-start notice for spec keys this backend never receives.

    A remote MCP server's ``oauth`` block (``mcpServers.<name>.oauth``) gets one
    more sentence, because what it costs is sign-in, not a setting. So do a refused
    ``permissions`` block and an ``allow`` it leaves out (``permissions.rules[<i>]``),
    since what they grant keeps asking, and, last, each unreadable ``allowedCommands``
    entry (``<key>.allowedCommands: <entry> (<reason>)``): the other entries apply
    unless the list is also named whole. A ``hooks.postToolUse[<matcher>]`` key
    gets :func:`unmet_post_tool_matchers_sentence` instead of "no effect". The
    entries past the bound Kiro Crew applies get their own sentence
    (``kas_unread_commands.over_limit_notice``).
    """
    from kiro_crew.acp.kas_spec_denies import GATE_JUDGED_MARK
    from kiro_crew.acp.kas_unread_commands import over_limit_notice
    from kiro_crew.agent_sdk.kas_tool_view import allow_exclude_limit
    from kiro_crew.agent_sdk.tools_settings_reach import GATE_READ_MARK as GATE
    from kiro_crew.mcp_oauth_projection import oauth_servers_in
    from kiro_crew.platform.allowed_commands import UNREADABLE_ENTRY_MARK as MARK

    over_limit, keys = over_limit_notice(agent, keys)
    unread = [key for key in keys if MARK in key]
    keys = [key for key in keys if MARK not in key]
    whole = {key.partition(MARK)[0] + ".allowedCommands" for key in unread} & {
        key.removesuffix(GATE) for key in keys
    }
    others = "" if whole or over_limit else "; the agent's other entries still apply"
    entries = (
        f"Agent {agent} has allowedCommands entries Kiro Crew cannot read: {'; '.join(unread)}. "
        "Each allows nothing, so a command only it matches asks for approval, or is refused "
        f"when denyByDefault is set{others}. Run kirocrew doctor to list them."
    )
    named = ([entries] if unread else []) + ([over_limit] if over_limit else [])
    if named and not keys:
        return "ℹ️ " + " ".join(named)
    # Crew's own gate still refuses what a marked allowlist leaves out, so it is
    # never said to have no effect.
    allowlists = [k.removesuffix(GATE) for k in keys if k.endswith(GATE)]
    # A deny Crew's gate refuses is not "no effect" either.
    judged = [k.removesuffix(GATE_JUDGED_MARK) for k in keys if k.endswith(GATE_JUDGED_MARK)]
    rest = [k for k in keys if not k.endswith(GATE) and not k.endswith(GATE_JUDGED_MARK)]
    # A postToolUse matcher no built-in tool meets still runs on an MCP tool with
    # the same name, so it is not said to have no effect.
    unmet = [k for k in rest if k.startswith(_UNMET_POST_TOOL_KEY_PREFIX) and k.endswith("]")]
    rest = [k for k in rest if k not in unmet]
    sentences = []
    if rest:
        sentences.append(
            f"Agent {agent} sets {' and '.join(rest)}, which this backend does not "
            "receive, so they have no effect in this session."
        )
    if allowlists:
        sentences.append(
            f"Agent {agent} sets {' and '.join(allowlists)}, which this backend does not "
            "receive. Kiro Crew's own approval gate still enforces them: it refuses what "
            "they do not cover, and what they cover may ask for approval."
        )
    if judged:
        sentences.append(
            f"Agent {agent} sets {' and '.join(judged)}, which this backend does not "
            "receive. Kiro Crew's own approval gate refuses a plain command they name, "
            "and each command of a plain chain (&&, ||, ;, |). It does not judge any "
            "other command, such as one with $(...), a backtick, a redirect, a newline, "
            "a trailing &, a tab, a ( ) group, or a quote, # or [[ in a chain, so such "
            "a command runs without a refusal."
        )
    if unmet:
        matchers = [k[len(_UNMET_POST_TOOL_KEY_PREFIX) : -1] for k in unmet]
        sentences.append(unmet_post_tool_matchers_sentence(agent, matchers))
    notice = "ℹ️ " + " ".join(sentences)
    servers = oauth_servers_in(keys)
    if servers:
        noun = "server" if len(servers) == 1 else "servers"
        notice += f" Sign-in to MCP {noun} {', '.join(servers)} will not work on this backend yet."
    if "permissions" in keys:
        notice += (
            " A permissions block is not sent at all when it names a policies bundle "
            "or is malformed or too large. With a policies bundle, the agent's shell "
            "and file tools also ask for approval even where allowedTools grants them."
        )
    if any(key.startswith("permissions.rules[") for key in keys):
        notice += (
            f" An allow rule whose exclude list has more than {allow_exclude_limit()} "
            "patterns is not sent, so what it allows asks for approval."
        )
    if any(".deniedCommands: " in key for key in rest):
        notice += (
            " Kiro Crew does not check those deniedCommands entries, so a command only they "
            "name is not refused on this backend."
        )
    for sentence in named:
        notice += f" {sentence}"
    return notice


def session_agent(provider: object, agent_id: str) -> str:
    """The agent whose spec a turn on *provider* meets.

    The one the caller named, which is what the turn runs (an in-turn switch
    passes the agent it switched to). A turn that names none runs the runtime's
    default, and only the session records which that is, so it falls back to the
    agent the session is running (see ``AcpSessionHandle.kas_projected_agent``).
    """
    if agent_id:
        return agent_id
    projected = getattr(provider, "kas_projected_agent", "")
    return projected if isinstance(projected, str) else ""


def provider_session_key(provider: object) -> str:
    """The session key *provider* runs, or ``""``.

    A shared runtime's client carries it itself; a dedicated ``AcpProvider``
    (``spawn_run(keep=True)``) holds it on its ``_client``.
    """
    for holder in (provider, getattr(provider, "_client", None)):
        raw = getattr(holder, "_session_key", None)
        if isinstance(raw, str) and raw:
            return raw
    return ""


async def turn_spec_hooks(
    provider: object, agent_id: str, *, project_hooks: bool = True
) -> TurnSpecHooks:
    """The spec hooks a subagent or task-runner turn gates on, and how.

    *provider* and *agent_id* are the turn's OWN: a subagent runs its own agent on
    its own backend, so a kiro-cli subagent under a KAS parent is not gated here and
    its spec is not read, as its harness already runs the field. The agent is
    resolved through :func:`session_agent`; when none can be named, every
    permission request is refused, since whose hooks apply is unknown.

    On a backend that fires only :data:`OBSERVER_HOOK_EVENTS` (claude, codex) the turn
    is not gated: it gets those hooks, and an agent that cannot be named or a
    spec that cannot be read leaves it with none.

    *project_hooks* false (a cron turn) never reads a KAS folder agent's own
    hooks, whatever the folder's choice; the user-level spec's hooks still fire.
    """
    if _fires_observer_spec_hooks_only(provider):
        return await _observer_turn_spec_hooks(provider, agent_id)
    if not capabilities_of(provider).crew_fires_spec_hooks:
        return _NOT_GATED
    cwd = getattr(provider, "cwd", "")
    work_dir = cwd if isinstance(cwd, str) and cwd else None
    agent_id = session_agent(provider, agent_id)
    if not agent_id:
        logger.warning("no agent is known for this KAS turn; its tool calls are blocked")
        return TurnSpecHooks([], work_dir, True, True)
    # A crewmate's worker fires its session's merged spec, as the chat turn does.
    session_key = provider_session_key(provider)
    # The keys the session runs without, read as the chat turn reads them.
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.kas_engine import discards_mcp_oauth

    on_kas = capabilities_of(provider).backend == ACP_BACKEND_KAS
    try:
        hooks, lost, _unconfirmable = await asyncio.to_thread(
            functools.partial(
                crew_fired_spec_hooks,
                agent_id,
                spec_project_dir(provider),
                translates_tools_settings=on_kas,
                discards_mcp_oauth=on_kas and discards_mcp_oauth(provider),
                withholds_oauth_secret=on_kas,
                session_key=session_key,
                kas_checkout=kas_hook_checkout(provider) if project_hooks else None,
            )
        )
    except Exception:  # noqa: BLE001 - the caller fails permission requests closed
        logger.warning(
            "agent spec hooks for %r could not be read; tool calls are blocked",
            agent_id,
            exc_info=True,
        )
        return TurnSpecHooks([], work_dir, True, True)
    # The user's standalone ~/.kiro/hooks/*.json files run on every KAS turn, not
    # only the dashboard chat turn, so a sub-agent or task-runner turn fires them
    # too (and the auto-approve withhold always has a hook to fire). KAS only; the
    # helper returns the spec hooks unchanged off KAS.
    try:
        hooks = await asyncio.to_thread(merge_user_hook_files, provider, hooks)
    except Exception:  # noqa: BLE001 - a hook-file read must never fail the turn
        logger.debug("user hook files could not be merged for this turn", exc_info=True)
    return TurnSpecHooks(hooks, work_dir, False, True, tuple(lost))


async def _observer_turn_spec_hooks(provider: object, agent_id: str) -> TurnSpecHooks:
    """:func:`turn_spec_hooks` on a backend that fires only the observer events."""
    cwd = getattr(provider, "cwd", "")
    work_dir = cwd if isinstance(cwd, str) and cwd else None
    agent_id = session_agent(provider, agent_id)
    if not agent_id:
        return _NOT_GATED
    try:
        hooks, _lost, _unconfirmable = await asyncio.to_thread(
            functools.partial(
                crew_fired_spec_hooks,
                agent_id,
                spec_project_dir(provider),
                session_key=provider_session_key(provider),
            )
        )
    except Exception:  # noqa: BLE001 - no hook here gates, so none runs this turn
        logger.warning(
            "agent spec hooks for %r could not be read; none run this turn",
            agent_id,
            exc_info=True,
        )
        return _NOT_GATED
    observed = observer_spec_hooks(hooks)
    # A postToolUse matcher no built-in tool on this backend meets is named with
    # the keys the turn's notice lists (``spec_keys_notice``).
    unmet = post_tool_matchers_no_builtin_meets(provider, observed)
    lost = tuple(unmet_post_tool_matcher_key(m) for m in unmet)
    return TurnSpecHooks(observed, work_dir, False, False, lost)


async def hook_projection_stale(
    provider: object, agent_id: str, *, project_hooks: bool = True
) -> bool:
    """Whether *provider*'s live session auto-approves a capability a PreToolUse hook
    now covers.

    A KAS session keeps the agent batch it registered, and that batch withheld
    auto-approval only for what the hooks covered then. A hook added since (on the
    Hooks page or in the spec) would never see an auto-approved call, so the
    session has to be re-projected before its next turn. False on a backend that
    runs the spec's hooks itself, and when the session records no batch. An agent
    with no spec on disk (a KAS built-in mode) has no spec hooks, so only the Hooks
    page's hooks count; a spec that exists but cannot be read answers True:
    re-projecting is the safe direction. A crewmate's worker is read
    as the provider's own session's merged spec (:func:`_agent_spec`). Each entry
    of the batch is checked on its own, a registered sub-agent target's included:
    its calls run inside this agent's turn under its own entry's permissions.
    """
    if not capabilities_of(provider).crew_fires_spec_hooks:
        return False
    approved = getattr(provider, "kas_auto_approved_capabilities", None)
    if approved is None:
        return False
    from kiro_crew.acp.kas_subagent_targets import registered_unprompted

    parts = registered_unprompted(approved, getattr(provider, "kas_registered_agents", None))
    if not parts:
        return False
    agent_id = session_agent(provider, agent_id)
    if not agent_id:
        return True
    session_key = provider_session_key(provider)

    def _covered() -> set[str]:
        # circular import: the ACP layer imports the config loader, which sits
        # below this module; resolved at call time like the other seams here.
        from kiro_crew.acp.kas_agents import pre_tool_hook_matchers
        from kiro_crew.acp.kas_hook_mcp import approved_mcp_servers
        from kiro_crew.acp.kas_permissions import (
            cached_mcp_tool_names,
            hook_gated_auto_approved,
            hook_gated_capabilities,
        )

        # A built-in mode with no spec: only the Hooks page's hooks count.
        checkout = kas_hook_checkout(provider) if project_hooks else None
        spec = _agent_spec(agent_id, None, session_key, kas_checkout=checkout)
        matchers = pre_tool_hook_matchers(agent_id, spec)
        still: set[str] = set()
        for part in parts:
            covered = hook_gated_capabilities(
                matchers,
                mcp_servers=approved_mcp_servers(part),
                mcp_tool_names=cached_mcp_tool_names,
            )
            still |= hook_gated_auto_approved(covered, part)
        return still

    try:
        still_approved = await asyncio.to_thread(_covered)
    except Exception:  # noqa: BLE001 - re-projecting is the safe direction
        logger.warning("PreToolUse hook coverage for %r could not be read; re-projecting", agent_id)
        return True
    return bool(still_approved)


#: :func:`invalidate_stale_kas_session`'s answers.
PROJECTION_FRESH = "fresh"
PROJECTION_RESET = "reset"
PROJECTION_BUSY = "busy"

#: Resets :func:`reproject_claimed_session` makes before it gives up. One is the
#: ordinary case; a second covers a hook edit landing during the first.
_MAX_REPROJECTIONS = 2


class StaleProjectionError(RuntimeError):
    """A claimed KAS session still auto-approves what a PreToolUse hook covers."""


def running_agent(provider: Any, fallback: str) -> str:
    """The agent *provider*'s session runs now, else *fallback*.

    A KAS mode switch moves the session to another agent and the handle records
    it (``kas_projected_agent``), so a step retried on the same session is gated by
    that agent's hooks, not by the agent the step was started with. A session that
    records no agent (kiro-cli, or no batch) answers *fallback*.
    """
    recorded = getattr(provider, "kas_projected_agent", "")
    return recorded if isinstance(recorded, str) and recorded else fallback


async def refuse_stale_switch(provider: Any, agent_id: str) -> None:
    """End the turn when a mid-turn agent switch left the session stale.

    A KAS mode switch activates a batch KAS already holds, so what the new agent
    auto-approves was fixed when the session started, and a PreToolUse hook that
    covers it would never see its calls: they never reach the permission request.
    The session cannot be re-projected mid-turn, so the turn is cancelled and
    :class:`StaleProjectionError` raised; the next claim finds the session stale
    and resets it (:func:`invalidate_stale_kas_session`). A switch that leaves
    nothing a hook covers auto-approved lets the turn go on.
    """
    if not await hook_projection_stale(provider, agent_id):
        return
    logger.warning(
        "agent %r: a mid-turn switch runs a batch that auto-approves a capability "
        "a PreToolUse hook covers; ending the turn",
        agent_id,
    )
    try:
        await provider.cancel()
    except Exception:  # noqa: BLE001 - the refusal below ends the turn anyway
        logger.debug("cancel after a stale agent switch failed", exc_info=True)
    raise StaleProjectionError(
        f"the switch to agent {agent_id!r} runs permissions its PreToolUse hooks "
        "cannot see; the turn was stopped and the next one starts a fresh session"
    )


async def invalidate_stale_kas_session(sessions: Any, session_key: str, agent_id: str) -> str:
    """End *session_key*'s idle KAS session when its projection has gone stale.

    See :func:`hook_projection_stale`. The claim that follows registers a fresh
    batch. Answers :data:`PROJECTION_FRESH` when nothing needs doing (no live
    session, or its batch still matches the hooks), :data:`PROJECTION_RESET` when
    it was reset, and :data:`PROJECTION_BUSY` when it is stale but another turn
    holds it. BUSY is not a pass: the caller's claim waits for that turn and must
    then go through :func:`reproject_claimed_session`, which decides on the
    session the claim actually holds.
    """
    provider = sessions.get_provider(session_key)
    if provider is None or not await hook_projection_stale(provider, agent_id):
        return PROJECTION_FRESH
    logger.info(
        "agent %r: a PreToolUse hook now covers a capability the live session "
        "auto-approves; re-projecting it before the next turn",
        agent_id,
    )
    try:
        reset = await sessions.reset(session_key, skip_if_busy=True)
    except Exception:  # noqa: BLE001 - the claim-time re-check still decides
        logger.warning("re-projection reset for %s failed", session_key, exc_info=True)
        return PROJECTION_BUSY
    return PROJECTION_RESET if reset else PROJECTION_BUSY


async def replace_stale_shared_session(
    provider: Any,
    agent_id: str,
    recreate: Callable[[], Awaitable[Any]],
) -> Any:
    """*provider*, or a replacement for it when its batch is already stale.

    For a session a run creates and owns outright (a subagent's session on its
    parent's shared runtime), where no other turn can hold it: a stale one is shut
    down and *recreate* registers a fresh batch. Raises
    :class:`StaleProjectionError` when it is still stale after
    :data:`_MAX_REPROJECTIONS` replacements.
    """
    for _ in range(_MAX_REPROJECTIONS):
        if not await hook_projection_stale(provider, agent_id):
            return provider
        logger.info(
            "agent %r: the new shared session auto-approves a capability a "
            "PreToolUse hook now covers; replacing it before the run",
            agent_id,
        )
        await provider.shutdown()
        provider = await recreate()
    if await hook_projection_stale(provider, agent_id):
        await provider.shutdown()
        raise StaleProjectionError(
            f"agent {agent_id!r}: the shared session could not be re-projected for "
            "the PreToolUse hooks now in place; the run was not started"
        )
    return provider
