"""Translate a Crew agent's ``allowedTools`` into a KAS inline permissions policy.

kiro-cli and KAS express "do not prompt for this" in two different currencies.
kiro-cli takes a flat allowlist of TOOL NAMES (``allowedTools``); KAS takes a
rules list keyed by CAPABILITY plus a resource glob::

    {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}

KAS declares ``allowedTools`` a CLI-only field, so the name has no counterpart —
but the CAPABILITY it is trying to express does, and that makes the translation
mechanical rather than a guess. Two properties of KAS's evaluator are what keep
it safe:

* An unmatched request resolves to ``ask``, so a tool this module cannot
  classify keeps prompting. Silence is the fail-closed direction, which is why
  an unmappable entry emits NO rule instead of a broad one.
* ``match`` omitted means "every resource" (KAS defaults it to ``['**']``).
  That is exactly what a kiro-cli ``allowedTools`` entry means — auto-approve
  the tool whatever it is pointed at — so the omission is faithful, not lax.

The vocabulary is deliberately narrow. A tool-name allowlist carries no resource
pattern, so every rule derived from it is unscoped — "allow the ``shell``
capability for any command" is the faithful translation of auto-approving
``execute_bash``, and that is precisely why the shell and filesystem families are
refused rather than translated (see :data:`WITHHELD_FROM_AUTO_APPROVE`). Auto-
approval means no permission request, and no permission request means the deny
floor and the sensitive-path check never run.

Both consumers use this one module so the wire projection
(``kas_agents.to_client_custom_agent``) and the on-disk spec Crew writes
(``agent.rebuild_agent_config``) cannot drift into disagreeing about what a
given ``allowedTools`` list means.

``allowedTools`` is the only input the DERIVED rules come from. A ``permissions``
block the spec's author wrote is a second input, and it travels only as a request
that is INTERSECTED with the ceiling-derived result -- never added to it. The four
rules that decide what survives are in :func:`merge_user_permissions`.

Reading the block is not a softening of the ceiling, it is the fix for the case
the derivation cannot cover: a pure-KAS agent authors ``permissions`` and no
``allowedTools``, so with the block dropped the field reaches the backend absent,
and absent resolves every request to ``ask`` -- a prompt for each of the calls the
author had just described a policy for. On disk the block is left untouched (it is
the user's file, and it applies when Crew is not injecting an agent); on the wire
it passes the same ceiling the derived rules do, or it does not travel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from kiro_crew.acp.kas_hook_mcp import (  # noqa: F401 - cached_mcp_tool_names is re-exported
    APPROVED_MCP_SERVER_PREFIX,
    auto_approved_mcp_servers,
    cached_mcp_tool_names,
)
from kiro_crew.acp.kas_settings_mcp import CREW_SETTINGS_MCP_KEY
from kiro_crew.acp.kas_spec_grants import (
    CREW_ANSWERED_PATHS_KEY,
    CREW_ANSWERED_READ_PATHS_KEY,
    CREW_SHELL_READONLY_KEY,
    READ_SETTINGS_KEYS,
    SHELL_SETTINGS_KEYS,
    WRITE_SETTINGS_KEYS,
)
from kiro_crew.acp.kas_subagent_rules import (  # noqa: F401 - trusted_agents_withheld re-exported
    _agent_glob,
    _available_agents_rules,
    trusted_agents_withheld,
)
from kiro_crew.acp.kas_tool_rows import (  # noqa: F401 - re-exported
    _HOOK_ONLY_ROWS,
    KAS_TOOL_IDS_BY_KIRO_TOOL,
    KAS_TOOL_MATCH_VOCABULARY,
    kas_tool_match_names,
)
from kiro_crew.acp.kas_unread_commands import placed_allowed_commands, unreadable_allowlist_entries
from kiro_crew.platform import allowed_commands as allowed_commands_mod
from kiro_crew.platform import allowed_paths as allowed_paths_mod
from kiro_crew.platform.tool_names import KIRO_TOOL_ALIASES, canonical_kiro_cli_tool_name
from kiro_crew.platform.tools_settings_aliases import (
    _TOOLS_SETTINGS_FAMILY,
    canonical_tools_settings,
)

logger = logging.getLogger(__name__)

#: Marks an MCP server (or one of its tools) in a Crew ``tools``/``allowedTools``
#: entry: ``@server`` for the whole server, ``@server/tool`` for one action.
_MCP_PREFIX = "@"

#: KAS's capability for any MCP-served tool.
_MCP_CAPABILITY = "mcp"

#: How an MCP tool's title spells it at the gate: ``mcp__<server>__<tool>``.
_MCP_TITLE_PREFIX = "mcp__"

#: Tool name -> KAS capability, mirroring KAS's own tool classification for the
#: built-in tools Crew's specs actually name. Deliberately NOT exhaustive over
#: KAS's table: an entry here is a promise that auto-approving the Crew tool and
#: allowing the KAS capability mean the same thing. Anything absent is treated as
#: unclassifiable and left to prompt (see the module docstring).
#:
#: Keyed on the names an ``allowedTools`` list carries, which are Crew's (and
#: kiro-cli's) names, not KAS's internal toolIds. The sub-agent tool is
#: ``use_subagent`` there; KAS calls the same tool ``invoke_sub_agent``, and that
#: spelling is deliberately NOT a key. The governance ceiling filters
#: ``allowedTools`` by ref before this table reads it, so a second spelling for
#: one tool would let a spec obtain under the alias the grant the ceiling withholds
#: under the Crew name. ``disclose_context`` stays: it is KAS's skill tool, Crew
#: has no name of its own for it, so there is no Crew spelling it could alias.
CAPABILITY_BY_TOOL: dict[str, str] = {
    # Network.
    "web_fetch": "web_fetch",
    "web_search": "web_search",
    # Sub-agents and skills.
    "use_subagent": "subagent",
    "disclose_context": "skill",
}

#: KAS toolId -> the Crew name ``allowedTools`` must use for the same tool. A
#: spec carrying the KAS spelling gets no grant (see :data:`CAPABILITY_BY_TOOL`
#: for why it is not an alias), so it is named at WARNING with the fix: the
#: spawn it meant to auto-approve now prompts, and the author should know why.
KAS_SPELLINGS: dict[str, str] = {"invoke_sub_agent": "use_subagent"}

#: Tools this module refuses to translate even though the capability exists.
#:
#: Auto-approval is not just "one fewer prompt" — it is the absence of a
#: permission request, and a Crew control that runs ON that request does not run
#: at all: the deny floor, the sensitive-path check, the ceiling's own last word.
#: For the shell and filesystem families the cost of losing those is the whole
#: blast radius (an arbitrary command, an overwritten file, a credential file
#: read), and the grant they would produce is unscoped, because a tool-name
#: allowlist carries no resource pattern to narrow it with.
#:
#: This is not a behaviour change for anything Crew ships: its own spec
#: deliberately keeps these OUT of ``allowedTools`` (see ``TestTheRealAllowlist``),
#: and on a governed host the ceiling withholds them anyway. What the refusal buys
#: is that a spec which asks for them — from an app manifest, or a hand edit on an
#: ungoverned host — cannot obtain here a KAS allow rule. No rule means ``ask``, so
#: every such call raises a permission request; Crew then answers the ones the spec
#: granted itself, after its own checks (:func:`crew_answered_builtins`, the gate's
#: ``spec-builtin-grant`` row), which is how the spec's grant still means "no
#: prompt" here without taking the call past the deny floor.
WITHHELD_FROM_AUTO_APPROVE: frozenset[str] = frozenset(
    {
        # Filesystem reads.
        "fs_read",
        "read",
        "read_file",
        "grep",
        "glob",
        "code",
        # Filesystem writes.
        "fs_write",
        "fs_append",
        "str_replace",
        "write",
        # Shell.
        "execute_bash",
        "execute_pwsh",
        "control_bash_process",
        "control_pwsh_process",
    }
)

#: The key a projected ``customAgents`` entry carries its
#: :func:`crew_answered_builtins` under, for the session's own record
#: (``harness.kas.KasHarness.record_session_projection``). Crew-only: KAS has no
#: such field, so :data:`CREW_ONLY_ENTRY_KEYS` is stripped before the wire
#: (``_dispatch.attach_kas_custom_agents``).
CREW_ANSWERED_BUILTINS_KEY = "_crewAnsweredBuiltins"

#: The key a projected entry carries its :func:`crew_answered_commands` under, for
#: the session's own record, like :data:`CREW_ANSWERED_BUILTINS_KEY`.
CREW_ANSWERED_COMMANDS_KEY = "_crewAnsweredCommands"

#: The key a projected entry carries its :func:`spec_shell_allowlist` under, for
#: the session's own record, like :data:`CREW_ANSWERED_COMMANDS_KEY`. Present only
#: when the spec sets ``denyByDefault``; the gate then refuses every shell call no
#: entry of the list matches.
CREW_SHELL_ALLOWLIST_KEY = "_crewShellAllowlist"

#: The key a projected entry carries its :func:`spec_write_allowlist` under, like
#: :data:`CREW_SHELL_ALLOWLIST_KEY`. Present only when the spec sets the write
#: tool's ``fallbackAction: "deny"``; the gate then refuses every file write no
#: entry of the list covers.
CREW_WRITE_ALLOWLIST_KEY = "_crewWriteAllowlist"

#: The key a projected entry carries the ``@server/tool`` refs of the write-class
#: MCP tools the batch write guard kept at ``ask`` under
#: (``batch_guard.withhold_write_auto_approval``). The session record reads them
#: with :data:`CREW_ANSWERED_BUILTINS_KEY`, and the gate's ``batch-guard-grant``
#: row answers them.
CREW_ANSWERED_WRITES_KEY = "_crewAnsweredWrites"

#: Where a projected entry keeps the ``@server/tool`` refs a PreToolUse hook's
#: ``ask`` took from auto-approval (``kas_hook_coverage.hook_held_grants``). Read
#: apart from :data:`CREW_ANSWERED_WRITES_KEY` (``kas_hook_coverage.projected_hook_held``):
#: the gate answers them only on a surface that fires the hooks.
CREW_ANSWERED_HOOK_HELD_KEY = "_crewAnsweredHookHeld"

#: The key of the grants Crew honours once its gate checks the spec's denies
#: (:mod:`kiro_crew.acp.kas_spec_denies`).
CREW_SPEC_DENIES_KEY = "_crewSpecDenies"

#: Keys of a projected ``customAgents`` entry that never reach the engine.
CREW_ONLY_ENTRY_KEYS: frozenset[str] = frozenset(
    {
        CREW_ANSWERED_BUILTINS_KEY,
        CREW_ANSWERED_COMMANDS_KEY,
        CREW_ANSWERED_HOOK_HELD_KEY,
        CREW_ANSWERED_WRITES_KEY,
        CREW_SPEC_DENIES_KEY,
        CREW_SHELL_ALLOWLIST_KEY,
        CREW_WRITE_ALLOWLIST_KEY,
        CREW_ANSWERED_PATHS_KEY,
        CREW_ANSWERED_READ_PATHS_KEY,
        CREW_SHELL_READONLY_KEY,
        CREW_SETTINGS_MCP_KEY,
    }
)


def _entry_crew_answered(entry: Any) -> frozenset[str]:
    """The :data:`CREW_ANSWERED_BUILTINS_KEY` list of one projected entry.

    Anything that is not a list of strings answers empty: a grant Crew answers
    without a prompt must come from the projection, never from a guess. The
    :data:`CREW_ANSWERED_WRITES_KEY` refs join it, read the same way: the
    ``@server/tool`` writes the batch write guard kept at ``ask``.
    """
    names: set[str] = set()
    for key in (CREW_ANSWERED_BUILTINS_KEY, CREW_ANSWERED_WRITES_KEY):
        raw = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(raw, list):
            names.update(n for n in raw if isinstance(n, str) and n)
    return frozenset(names)


def _entry_crew_answered_commands(entry: Any) -> tuple[str, ...]:
    """The :data:`CREW_ANSWERED_COMMANDS_KEY` patterns of one projected entry, read
    like :func:`_entry_crew_answered`: anything malformed answers empty."""
    raw = entry.get(CREW_ANSWERED_COMMANDS_KEY) if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return ()
    return tuple(p for p in raw if isinstance(p, str) and p)


def _entry_shell_allowlist(entry: Any) -> tuple[str, ...] | None:
    """The :data:`CREW_SHELL_ALLOWLIST_KEY` patterns of one projected entry.

    ``None`` when the entry has no such key (no ``denyByDefault``). A value that is
    present but not a list of strings reads as ``()``, which allows no command: a
    restriction is never lifted by a malformed record.
    """
    if not isinstance(entry, dict) or CREW_SHELL_ALLOWLIST_KEY not in entry:
        return None
    raw = entry[CREW_SHELL_ALLOWLIST_KEY]
    if not isinstance(raw, list):
        return ()
    return tuple(p for p in raw if isinstance(p, str) and p)


#: The ``toolsSettings`` keys kiro-cli reads its shell settings under. After
#: :func:`canonical_tools_settings` at most one of them is present.
_SHELL_SETTINGS_KEYS = SHELL_SETTINGS_KEYS

#: How many usable ``allowedCommands`` entries :func:`crew_answered_commands` keeps,
#: so the record the session carries stays bounded. The gate reads each entry once
#: (``allowed_commands._read_text``) and every entry has its own size bounds there,
#: so a shell call costs one match per entry.
_MAX_ALLOWED_COMMANDS = 1024


def allowed_command_pattern(raw: Any) -> str | None:
    """One kiro-cli ``allowedCommands`` entry as Crew records it, or ``None``.

    Only the syntax :mod:`kiro_crew.platform.allowed_commands` reads the same way
    kiro-cli does, and matches in linear time, is kept; any other entry
    is ``None`` and its commands keep prompting. Kept verbatim, as kiro-cli reads it.
    """
    if allowed_commands_mod.parse(raw) is None:
        return None
    assert isinstance(raw, str)
    return raw


def _usable_allowed_commands(tools_settings: Any) -> list[str]:
    """Every distinct usable ``allowedCommands`` entry of both shell keys, in order."""
    return [entry for _key, _at, entry in placed_allowed_commands(tools_settings)]


def _authored_answered_allows(authored_permissions: Any) -> list[tuple[str, list[str]]]:
    """``(capability, match)`` of each ``shell``/``fs_read``/``fs_write`` ``allow``
    in the spec's own ``permissions`` block that has no ``exclude``.

    Read as the merge reads the block (:func:`parse_user_permissions`, silently), so a
    block it refuses grants nothing here. An ``exclude`` narrows the grant in a way the
    gate cannot repeat, so such an allow keeps prompting.
    """
    rules = parse_user_permissions(authored_permissions, audit_decision=_asking_only) or []
    return [
        (rule["capability"], rule.get("match") or [])
        for rule in rules
        if rule["effect"] == "allow"
        and rule["capability"] in WITHHELD_CAPABILITIES
        and not rule.get("exclude")
    ]


def _shell_glob_pattern(glob: str) -> str | None:
    """A KAS shell ``match`` glob as an ``allowedCommands`` entry, or ``None``.

    ``*`` becomes ``.*`` (the reading :func:`_regex_to_glob` gives a deny the other
    way) and every other character is a literal. A glob holding any other glob syntax
    or a backslash is ``None``, so its commands keep prompting.
    """
    if "\\" in glob or set(glob) & (_GLOB_METACHARACTERS - {"*"}):
        return None
    body = "".join(
        ".*" if ch == "*" else f"\\{ch}" if ch in _REGEX_METACHARACTERS else ch for ch in glob
    )
    return allowed_command_pattern(body)


def crew_answered_commands(tools_settings: Any, authored_permissions: Any = None) -> list[str]:
    """The ``shell.allowedCommands`` patterns Crew answers a KAS shell request for,
    then the ``match`` globs of a ``shell`` ``allow`` in *authored_permissions*
    (:func:`_shell_glob_pattern`).

    A shell allow never reaches the KAS policy (:data:`WITHHELD_CAPABILITIES`),
    so each command raises a permission request; the gate's ``spec-builtin-grant``
    tier approves one whose recovered command fullmatches a pattern here, after
    every deny tier and governance (``gate_tiers._tier_spec_builtin_grant``).
    Pure: the caller asks the governance ceiling about the shell tool.

    :data:`_MAX_ALLOWED_COMMANDS` bounds the ``allowedCommands`` part alone, and the
    session-start notice names each entry past it. The authored globs are bounded
    by their own block (:func:`parse_user_permissions`), which is refused whole and
    named when it is too large, so none of them is ever cut here.
    """
    authored = [
        pattern
        for capability, match in _authored_answered_allows(authored_permissions)
        if capability == "shell"
        for glob in match
        if (pattern := _shell_glob_pattern(glob))
    ]
    patterns = [*_usable_allowed_commands(tools_settings)[:_MAX_ALLOWED_COMMANDS], *authored]
    return list(dict.fromkeys(patterns))


def shell_denies_by_default(tools_settings: Any) -> bool:
    """Whether *tools_settings* sets ``denyByDefault`` on either shell key.

    Any value other than ``False``/``None``/absent counts, so a malformed value
    restricts rather than lifts the restriction.
    """
    if not isinstance(tools_settings, dict):
        return False
    for key in _SHELL_SETTINGS_KEYS:
        settings = tools_settings.get(key)
        if isinstance(settings, dict) and settings.get("denyByDefault") not in (None, False):
            return True
    return False


#: The names :func:`allowed_tools_grant_shell` compares a folded ``allowedTools``
#: entry against: the shell tool's own name and its short spelling
#: (:data:`KIRO_TOOL_ALIASES`).
_SHELL_TOOL_NAMES = frozenset({"execute_bash", "shell"})

#: Prefix whose exact forms ``@builtin``, ``@builtin/`` and ``@builtin/*`` mean
#: "every built-in"; ``@builtin/<name>`` names one built-in by its exact name.
_BUILTIN_PREFIX = "@builtin"


def allowed_tools_grant_shell(allowed_tools: Any) -> bool:
    """Whether *allowed_tools* names the shell by name.

    True when an entry is ``@builtin`` / ``@builtin/`` / ``@builtin/*``, or when
    :func:`canonical_kiro_cli_tool_name` folds the whole entry to ``execute_bash``
    or ``shell``: bare ``executeCmd`` and ``execute_cmd`` count, and after
    ``@builtin/`` only the names kiro-cli accepts there (``execute_bash``,
    ``execute_cmd``, ``shell``). Any other entry, one holding ``*`` or ``?``
    included, grants nothing here, so the ``denyByDefault`` restriction stays
    (fail closed).
    """
    if not isinstance(allowed_tools, list):
        return False
    for raw in allowed_tools:
        if not isinstance(raw, str):
            continue
        entry = raw.strip()
        if entry in (_BUILTIN_PREFIX, f"{_BUILTIN_PREFIX}/", f"{_BUILTIN_PREFIX}/*"):
            return True
        if canonical_kiro_cli_tool_name(entry) in _SHELL_TOOL_NAMES:
            return True
    return False


def shell_deny_globs(policy: Any) -> set[str]:
    """The globs of *policy*'s plain whole shell ``deny`` rules.

    Only a plain whole deny counts: a rule with any other key (an ``exclude``
    carving commands out of it) does not prove the deny holds.
    """
    carried: set[str] = set()
    rules = policy.get("rules") if isinstance(policy, dict) else None
    for rule in rules if isinstance(rules, list) else ():
        if (
            isinstance(rule, dict)
            and set(rule) == {"capability", "match", "effect"}
            and rule["capability"] == "shell"
            and rule["effect"] == "deny"
            and isinstance(rule["match"], list)
        ):
            carried.update(m for m in rule["match"] if isinstance(m, str))
    return carried


def shell_deny_carried(raw: str, globs: set[str]) -> bool:
    """Whether the ``deniedCommands`` entry *raw* reaches KAS as one of *globs*
    (:func:`shell_deny_globs`) and holds no redirect character KAS never sees. A
    blank entry needs no rule."""
    if not raw.strip():
        return True
    glob = _regex_to_glob(raw.strip())
    return glob is not None and not set(glob) & _REDIRECT_CHARACTERS and glob in globs


def shell_denies_carried(tools_settings: Any, policy: Any) -> bool:
    """Whether every ``deniedCommands`` entry of both shell keys reaches *policy*
    as a KAS shell ``deny`` rule.

    False when an entry has no glob spelling (:func:`_regex_to_glob`), holds a
    redirect character KAS never sees (:data:`_REDIRECT_CHARACTERS`), is not a
    string, when the value is not a list, or when the translated glob is not among
    *policy*'s shell deny matches (a merge dropped it). Then the deny lives only in
    the spec, and lifting ``denyByDefault`` would let the command it names run.
    """
    if not isinstance(tools_settings, dict):
        return tools_settings is None
    carried = shell_deny_globs(policy)
    for key in _SHELL_SETTINGS_KEYS:
        settings = tools_settings.get(key)
        denied = settings.get("deniedCommands") if isinstance(settings, dict) else None
        if denied is None or (isinstance(denied, list) and not denied):
            continue
        if not isinstance(denied, list):
            return False
        for raw in denied:
            if not isinstance(raw, str) or not shell_deny_carried(raw, carried):
                return False
    return True


def spec_shell_allowlist(
    tools_settings: Any, allowed_tools: Any = None, policy: Any = None
) -> list[str] | None:
    """The only shell commands a ``denyByDefault`` spec lets run, or ``None``.

    ``None`` when neither shell key sets ``denyByDefault``, and when the spec's
    *allowed_tools* names the shell (:func:`allowed_tools_grant_shell`) and every
    ``deniedCommands`` entry rides the projected *policy* as a KAS deny rule
    (:func:`shell_denies_carried`). kiro-cli allows
    every command of an agent that lists the shell unless ``deniedCommands``
    denies it, and applies ``denyByDefault`` only to an agent that does not list
    the shell. Its commands then go through the gate's usual path and prompt or
    are answered there. Otherwise the usable
    ``allowedCommands`` patterns of both keys (:func:`crew_answered_commands`'s
    subset and bound), possibly empty, then each entry outside the subset
    (:func:`unreadable_allowlist_entries`), which matches nothing. The gate's
    ``spec-shell-allowlist`` tier REFUSES a shell call of the session's own agent
    that no pattern here matches, the way kiro-cli refuses it, on every approval
    policy. An entry outside the subset, or past the bound, allows nothing: its
    commands are refused, never let through (fail closed), and the session-start
    notice and the refusal name it.

    Read whatever the governance ceiling says: it is a restriction, not a grant.
    A command it keeps still prompts unless :func:`crew_answered_commands` answers it.
    """
    if not shell_denies_by_default(tools_settings):
        return None
    if allowed_tools_grant_shell(allowed_tools) and shell_denies_carried(tools_settings, policy):
        return None
    return crew_answered_commands(tools_settings) + unreadable_allowlist_entries(tools_settings)


#: The ``toolsSettings`` keys kiro-cli reads its file-write settings under.
_WRITE_SETTINGS_KEYS = WRITE_SETTINGS_KEYS

#: Bound on what :func:`spec_write_allowlist` keeps, like :data:`_MAX_ALLOWED_COMMANDS`.
_MAX_ALLOWED_PATHS = 64


def _write_falls_back_to_deny(settings: Any) -> bool:
    """Whether one write entry sets ``fallbackAction`` to ``"deny"``.

    Read case-insensitively, and a value that is not a string at all counts too, so
    a malformed value restricts rather than lifts the restriction. Any other string
    (``"interactive"``, the default) leaves the write prompting, as before.
    """
    if not isinstance(settings, dict) or "fallbackAction" not in settings:
        return False
    value = settings["fallbackAction"]
    if value is None:
        return False
    if not isinstance(value, str):
        return True
    return value.strip().casefold() == "deny"


def write_denies_by_default(tools_settings: Any) -> bool:
    """Whether *tools_settings* sets ``fallbackAction: "deny"`` on either write key."""
    if not isinstance(tools_settings, dict):
        return False
    return any(_write_falls_back_to_deny(tools_settings.get(k)) for k in _WRITE_SETTINGS_KEYS)


def _usable_allowed_paths(
    tools_settings: Any, keys: tuple[str, ...] = WRITE_SETTINGS_KEYS
) -> list[str]:
    """Every distinct usable ``allowedPaths`` entry of the *keys* entries (both write
    keys by default), in order."""
    if not isinstance(tools_settings, dict):
        return []
    out: list[str] = []
    for key in keys:
        settings = tools_settings.get(key)
        entries = settings.get("allowedPaths") if isinstance(settings, dict) else None
        for raw in entries if isinstance(entries, list) else ():
            pattern = allowed_paths_mod.parse(raw)
            if pattern is not None:
                out.append(pattern)
    return list(dict.fromkeys(out))


def spec_write_allowlist(tools_settings: Any) -> list[str] | None:
    """The only paths a ``fallbackAction: "deny"`` spec lets its write tool touch.

    ``None`` when neither write key sets it. Otherwise the usable ``allowedPaths``
    entries of both keys (the subset :mod:`kiro_crew.platform.allowed_paths` reads
    the way kiro-cli does, at most :data:`_MAX_ALLOWED_PATHS`), possibly empty, as
    the spec wrote them: the session handle anchors a relative entry at the
    session's directory. The gate's ``spec-write-allowlist`` tier REFUSES a file
    write of the session's own agent whose target none covers, the way kiro-cli
    refuses it, on every approval policy. An entry outside the subset, or past the
    bound, covers nothing: its writes are refused, never let through (fail
    closed), and the session-start notice names it.

    Read whatever the governance ceiling says: it is a restriction, not a grant. A
    write it covers still prompts, as every KAS write does.
    """
    if not write_denies_by_default(tools_settings):
        return None
    return _usable_allowed_paths(tools_settings)[:_MAX_ALLOWED_PATHS]


def crew_answered_paths(tools_settings: Any) -> list[str]:
    """The write ``allowedPaths`` Crew answers a KAS write for (``kas_spec_grants``),
    usable and bounded like :func:`spec_write_allowlist`. The caller asks the ceiling."""
    return _usable_allowed_paths(tools_settings)[:_MAX_ALLOWED_PATHS]


def _paths_not_all_carried(paths: Any, tools_settings: Any, tool: str) -> bool:
    """Whether some of *tool*'s ``allowedPaths`` cannot be answered: an entry is
    unusable, or the bound cuts the list. Those paths keep prompting."""
    keys = READ_SETTINGS_KEYS if tool in READ_SETTINGS_KEYS else WRITE_SETTINGS_KEYS
    return bool(paths) and (
        not isinstance(paths, list)
        or any(allowed_paths_mod.parse(p) is None for p in paths)
        or len(_usable_allowed_paths(tools_settings, keys)) > _MAX_ALLOWED_PATHS
    )


def crew_answered_read_paths(tools_settings: Any) -> list[str]:
    """The same for the read tool's ``allowedPaths`` (``fs_read`` / ``read``)."""
    return _usable_allowed_paths(tools_settings, READ_SETTINGS_KEYS)[:_MAX_ALLOWED_PATHS]


def crew_answered_builtins(allowed_tools: Any, authored_permissions: Any = None) -> frozenset[str]:
    """The :data:`WITHHELD_FROM_AUTO_APPROVE` built-ins *allowed_tools* grants, and
    each one whose capabilities an unscoped ``allow`` in *authored_permissions*
    covers (no ``match``, or an empty one: every resource, as KAS reads it).

    Withholding a built-in from the KAS policy keeps its calls at ``ask``, so
    each one raises ``session/request_permission`` and reaches Crew's gate. The
    spec's author still granted it, though, and on kiro-cli that grant means no
    prompt. This is the set Crew answers ITSELF at that request: the gate's
    ``spec-builtin-grant`` tier approves a call to one of these only after the
    deny floor, the sensitive-path check and the governance ceiling have run on
    it (``hook_runtime.gate_tiers._tier_spec_builtin_grant``). Nothing here
    reaches the wire as an allow rule.

    Pure: the caller asks the governance ceiling about each name
    (``governance.may_grant_builtin_at_gate_now``) and drops what it withholds.
    kiro-cli's short spellings (``shell``, ``read``, ``write``)
    are folded to the name a rule is written in, the way the gate reads a stamp.
    """
    out: set[str] = set()
    for raw in allowed_tools if isinstance(allowed_tools, list) else ():
        if not isinstance(raw, str):
            continue
        entry = raw.strip()
        name = KIRO_TOOL_ALIASES.get(entry, entry)
        if entry in WITHHELD_FROM_AUTO_APPROVE or name in WITHHELD_FROM_AUTO_APPROVE:
            out.add(name)
    granted = {cap for cap, match in _authored_answered_allows(authored_permissions) if not match}
    out.update(
        KIRO_TOOL_ALIASES.get(name, name)
        for name, capabilities in _ANSWERED_CAPABILITIES.items()
        if capabilities <= granted
    )
    return frozenset(out)


#: Each :data:`WITHHELD_FROM_AUTO_APPROVE` name -> the KAS capabilities its calls
#: are evaluated under. ``code`` is read under all three: it edits files and runs
#: commands, so a rule on any of them speaks about it.
_ANSWERED_CAPABILITIES: dict[str, frozenset[str]] = {
    **dict.fromkeys(("fs_read", "read", "read_file", "grep", "glob"), frozenset({"fs_read"})),
    **dict.fromkeys(("fs_write", "fs_append", "str_replace", "write"), frozenset({"fs_write"})),
    **dict.fromkeys(
        ("execute_bash", "execute_pwsh", "control_bash_process", "control_pwsh_process"),
        frozenset({"shell"}),
    ),
    "code": frozenset({"fs_read", "fs_write", "shell"}),
}


_ALL_ANSWERED_CAPABILITIES: frozenset[str] = frozenset({"fs_read", "fs_write", "shell"})


def _restricted_by_rules(rules: Any) -> set[str]:
    """The capabilities an ``ask``/``deny`` (or unreadable) rule list speaks about."""
    if not isinstance(rules, list):
        return set(_ALL_ANSWERED_CAPABILITIES)
    restricted: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            return set(_ALL_ANSWERED_CAPABILITIES)
        if rule.get("effect") == "allow":
            continue
        capability = rule.get("capability")
        if not isinstance(capability, str):
            return set(_ALL_ANSWERED_CAPABILITIES)
        restricted.update(META_CAPABILITY_EXPANSION.get(capability, (capability,)))
    return restricted


def authored_restricted_capabilities(
    authored_permissions: Any, tools_settings: Any
) -> frozenset[str]:
    """The KAS capabilities the spec's own author restricted, read from the RAW inputs.

    Read before any merge or translation, because both can drop what they cannot
    carry (a ``policies`` bundle refuses the whole block, an oversized or malformed
    block is refused, a ``toolsSettings`` key may have no KAS translation) and a
    dropped restriction must still keep its built-ins at the prompt:

    * ``permissions``: every rule whose effect is not ``allow`` restricts its
      capability (meta capabilities expanded), whatever its ``match`` scope. A block
      that is not exactly ``{"rules": [...]}`` with no ``policies`` bundle, or any
      rule that cannot be read, restricts all three.
    * ``toolsSettings``: any entry for one of these built-ins (in any kiro-cli
      spelling, :func:`canonical_tools_settings`) restricts its capabilities;
      ``@server`` entries are MCP and a known tool outside these three
      (``web_fetch``, ``subagent``, ``use_aws``) says nothing here. A key Crew
      cannot place, and a ``toolsSettings`` that is not an object, restrict all
      three (fail closed).
    """
    restricted: set[str] = set()
    if authored_permissions is not None:
        raw = authored_permissions
        if (
            not isinstance(raw, dict)
            or set(raw) - {"rules", "policies"}
            or raw.get("policies") not in (None, [])
        ):
            return _ALL_ANSWERED_CAPABILITIES
        restricted |= _restricted_by_rules(raw.get("rules"))
    if tools_settings not in (None, {}):
        if not isinstance(tools_settings, dict):
            return _ALL_ANSWERED_CAPABILITIES
        tools_settings, _ = canonical_tools_settings(tools_settings)
        for key in tools_settings:
            if not isinstance(key, str):
                return _ALL_ANSWERED_CAPABILITIES
            if key.startswith(_MCP_PREFIX):
                continue
            name = KIRO_TOOL_ALIASES.get(key, key)
            if name in _ANSWERED_CAPABILITIES:
                restricted |= _ANSWERED_CAPABILITIES[name]
            elif key not in _TOOLS_SETTINGS_FAMILY and key != "use_aws":
                return _ALL_ANSWERED_CAPABILITIES
    return frozenset(restricted & _ALL_ANSWERED_CAPABILITIES)


def withhold_answered_under_authored_rules(
    answered: Iterable[str],
    policy: dict[str, Any] | None,
    *,
    authored_permissions: Any = None,
    tools_settings: Any = None,
) -> frozenset[str]:
    """*answered* minus every built-in a restriction the spec's author wrote names.

    Two readings, and a built-in survives only if neither names its capability:
    the projected *policy* after every merge (any ``ask`` or ``deny`` rule), and the
    raw ``permissions`` block and ``toolsSettings``
    (:func:`authored_restricted_capabilities`), which still hold what a merge or
    translation dropped. An authored ``ask`` reaches Crew as a permission request
    like any other, and answering it with the ``allowedTools`` grant would override
    the author's own "ask me". A name with no capability row is withheld too.
    """
    rules = policy.get("rules") if isinstance(policy, dict) else None
    restricted = _restricted_by_rules(rules) if rules is not None else set()
    restricted |= authored_restricted_capabilities(authored_permissions, tools_settings)
    return frozenset(
        name
        for name in answered
        if name in _ANSWERED_CAPABILITIES and not (_ANSWERED_CAPABILITIES[name] & restricted)
    )


#: Glob syntax kiro-cli's ``allowedTools`` matcher and KAS's ``mcp`` rule both
#: read as "any run of characters" in the TOOL part of an ``@server/tool`` entry
#: (``@server/read_*``). kiro-cli matches with ``globset``; KAS compiles the rule
#: to a Cedar ``like`` (``cedar-compiler.ts`` ``globToCedarLike``), where ``*`` is
#: the only wildcard. Only this one travels; see :data:`_UNSHARED_GLOB_SYNTAX`.
_SHARED_GLOB_SYNTAX = frozenset("*")

#: Glob syntax the two matchers read differently. ``?`` is one character to
#: ``globset`` and a literal ``?`` to Cedar ``like``; brackets and braces are
#: ``globset`` syntax, and ``!`` is left out with them. An entry carrying any of these
#: means one thing to the list it was written on and something else here, so it
#: is never translated (see :func:`_mcp_pattern`).
_UNSHARED_GLOB_SYNTAX = frozenset("?[]{}!")

#: Every character either matcher treats as a glob. A SERVER name carrying any of
#: these is refused outright (see :func:`_mcp_pattern`).
_GLOB_METACHARACTERS = _SHARED_GLOB_SYNTAX | _UNSHARED_GLOB_SYNTAX


def _mcp_pattern(entry: str) -> str | None:
    """Resource glob for an ``@server`` / ``@server/tool`` entry.

    KAS addresses an MCP tool as ``<server>/<tool>``, so a bare server becomes a
    one-level glob, a named action becomes an exact match, and a tool-part ``*``
    glob (``@srv/query_*``) travels as written: ``*`` is "any run of characters"
    to both kiro-cli and KAS, so dropping it would make one line of text a grant
    on one backend and a prompt on the other — the asymmetry this module exists
    to avoid.

    ``None`` when the SERVER part is not one literal name. ``@*`` would become
    the pattern ``*/*``, which KAS resolves as every tool on every server, and a
    server-part glob is the one shape kiro-cli's own reading is not pinned down
    for. Translation must not be the step that widens a grant, so an entry we
    cannot read as one literal server is left to prompt instead of being guessed
    at. The tool part may carry only the shared ``*``; ``?``, bracket, brace and
    negation forms are refused for the same reason (:data:`_UNSHARED_GLOB_SYNTAX`).

    A backslash is refused anywhere: ``globset`` reads it as an escape, while KAS
    sends it to Cedar as a literal backslash, so ``a\\*b`` is one tool named
    ``a*b`` on kiro-cli and every ``a\\…b`` on KAS.
    """
    ref = entry[len(_MCP_PREFIX) :]
    server, slash, tool = ref.partition("/")
    if "\\" in ref or _GLOB_METACHARACTERS.intersection(server):
        return None
    if _UNSHARED_GLOB_SYNTAX.intersection(tool):
        return None
    return ref if slash else f"{ref}/*"


def _collapse_mcp_patterns(patterns: set[str]) -> list[str]:
    """Drop per-tool patterns already covered by their server's wildcard.

    ``@srv`` and ``@srv/one`` commonly appear together (the broad entry was
    added later and the narrow ones were never cleaned up). Emitting both is
    harmless to the evaluator but misleading to read, and a reviewer comparing
    the policy against the allowlist should not have to work out that one line
    subsumes another.
    """
    servers = {p[:-2] for p in patterns if p.endswith("/*")}
    return sorted(p for p in patterns if p.endswith("/*") or p.split("/", 1)[0] not in servers)


def allowed_tools_to_permissions(
    allowed_tools: Any,
    *,
    agent_id: str = "",
) -> dict[str, Any] | None:
    """Build a KAS inline permissions policy from a Crew ``allowedTools`` list.

    Returns ``None`` when there is nothing to say — no usable entries, or none
    that classify — so callers can omit the field entirely rather than sending
    an empty ``rules`` array. An empty policy and an absent one are NOT
    equivalent to read: absent says "this spec never described auto-approval",
    which is the truth in that case.

    Entries that cannot be classified are reported once, at debug, listing the
    names. They are not an error: they keep prompting, which is the same
    behaviour as having no policy at all, and a WARNING for a tool the user
    never asked to auto-approve would be noise.
    """
    if not isinstance(allowed_tools, list):
        return None

    mcp_patterns: set[str] = set()
    capabilities: set[str] = set()
    unclassified: list[str] = []
    withheld: list[str] = []

    for raw in allowed_tools:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = raw.strip()
        if entry == _BUILTIN_PREFIX or entry.startswith(f"{_BUILTIN_PREFIX}/"):
            unclassified.append(entry)  # a built-in ref, never an MCP server `builtin`
            continue
        if entry.startswith(_MCP_PREFIX):
            pattern = _mcp_pattern(entry)
            # `None` for a glob; `@` alone or `@/tool` names no server. Neither
            # describes a grant we can honour, so both keep prompting.
            if pattern is None or pattern.startswith("/"):
                unclassified.append(entry)
                continue
            mcp_patterns.add(pattern)
            continue
        if entry in WITHHELD_FROM_AUTO_APPROVE:
            withheld.append(entry)
            continue
        capability = CAPABILITY_BY_TOOL.get(entry)
        if capability is None:
            unclassified.append(entry)
            continue
        capabilities.add(capability)

    if unclassified:
        logger.debug(
            "agent %r: allowedTools entries with no KAS capability, left to prompt: %s",
            agent_id,
            ", ".join(sorted(unclassified)),
        )
    for entry in sorted(set(unclassified) & set(KAS_SPELLINGS)):
        logger.warning(
            "agent %r: allowedTools lists %r, which is KAS's own name for the tool; "
            "list %r instead to auto-approve it -- until then it prompts",
            agent_id,
            entry,
            KAS_SPELLINGS[entry],
        )
    if withheld:
        # Louder than `unclassified`, and separate from it: this one is a policy
        # decision the spec asked us to reverse, so a reader comparing the spec
        # against the projected policy should not have to guess which it was.
        logger.info(
            "agent %r: projecting no KAS allow rule for %s — an auto-approved call "
            "raises no permission request, so Crew's deny floor and sensitive-path "
            "check would not run for it; Crew answers those requests itself instead "
            "(see crew_answered_builtins)",
            agent_id,
            ", ".join(sorted(withheld)),
        )

    rules: list[dict[str, Any]] = []
    if mcp_patterns:
        rules.append(
            {
                "capability": _MCP_CAPABILITY,
                "match": _collapse_mcp_patterns(mcp_patterns),
                "effect": "allow",
            }
        )
    # `match` is deliberately omitted: a tool-name allowlist entry carries no
    # resource scope, and KAS reads a missing `match` as every resource.
    rules.extend({"capability": cap, "effect": "allow"} for cap in sorted(capabilities))

    if not rules:
        return None
    return {"rules": rules}


#: KAS's own capability vocabulary (``policy/capabilities.ts``
#: ``VALID_CAPABILITIES``). A user block naming anything else is refused whole: a
#: capability this module cannot place is one it cannot intersect with the ceiling
#: either, and KAS's parser skips such a rule fail-soft, so accepting it here
#: would relay a line that means nothing on either side.
KAS_CAPABILITIES: frozenset[str] = frozenset(
    {
        "all",
        "builtin",
        "filesystem",
        "fs_read",
        "fs_write",
        "shell",
        "web_fetch",
        "web_search",
        "mcp",
        "subagent",
        "skill",
        "power",
        "context",
        "diagnostics",
        "sandbox_network",
    }
)

#: The concrete capabilities KAS's ``builtin`` meta-capability stands for
#: (``policy/capabilities.ts`` ``BUILTIN``), kept in KAS's own order so the two
#: tables can be read side by side.
_BUILTIN_CAPABILITIES: tuple[str, ...] = (
    "fs_read",
    "fs_write",
    "shell",
    "web_fetch",
    "web_search",
    "subagent",
    "skill",
    "power",
    "context",
    "diagnostics",
)

#: Meta capability -> what KAS expands it into (``META_CAPABILITIES``).
#:
#: Expansion is what makes the refusal in :data:`WITHHELD_CAPABILITIES` total.
#: ``{"capability": "all", "effect": "allow"}`` is four words that carry ``shell``
#: and both filesystem capabilities inside them, so a rule read at face value
#: hands over exactly the family this module refuses when it is named outright.
META_CAPABILITY_EXPANSION: dict[str, tuple[str, ...]] = {
    "all": _BUILTIN_CAPABILITIES + (_MCP_CAPABILITY,),
    "builtin": _BUILTIN_CAPABILITIES,
    "filesystem": ("fs_read", "fs_write"),
}

#: :data:`WITHHELD_FROM_AUTO_APPROVE` in KAS's currency: the capabilities a user
#: ``allow`` cannot obtain here, whatever the ceiling says.
#:
#: The argument is the one already made for the tool names -- auto-approval is the
#: absence of a permission request, and Crew's deny floor and sensitive-path check
#: run ON that request -- and it does not weaken when the grant arrives as a
#: capability instead of a tool name. A user ``allow`` naming one of these is
#: dropped, which leaves the capability with no rule, and no rule is ``ask``; Crew's
#: gate then answers what it can read of it (:func:`_authored_answered_allows`).
#: This is the one place the merge is deliberately not symmetric with kiro-cli,
#: and it is the direction that keeps Crew's gate in the path.
WITHHELD_CAPABILITIES: frozenset[str] = frozenset({"shell", "fs_read", "fs_write"})

#: Capability -> EVERY ``allowedTools`` ref that asks the governance ceiling about
#: it. Inverted from :data:`CAPABILITY_BY_TOOL` rather than written out, because
#: the ceiling predicate speaks in refs and a user block speaks in capabilities;
#: two hand-kept tables would answer differently the first time one is extended.
#:
#: A TUPLE per capability, and not for symmetry: the source map is many-to-one, so
#: an inversion that keeps one ref per capability silently drops the others the
#: first time a second tool maps to one capability, and then asks the ceiling a
#: question about a ref the author's rule does not cover. Every ref is asked and
#: all must permit, because the capability grants all of them.
#:
#: A capability with no ref here cannot be put to the ceiling at all, and rule 3
#: needs an affirmative answer, so such a rule does not travel.
CEILING_REFS_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    capability: tuple(sorted(t for t, c in CAPABILITY_BY_TOOL.items() if c == capability))
    for capability in sorted(set(CAPABILITY_BY_TOOL.values()))
}

#: The keys KAS's permissions schema defines, at both levels
#: (``services/custom-agents/types.ts`` for disk, ``resolve-client-agents.ts``
#: for the wire -- the same shape twice).
_BLOCK_KEYS = frozenset({"rules", "policies"})
_RULE_KEYS = frozenset({"capability", "match", "exclude", "effect"})

#: The effects KAS's schema enumerates.
_EFFECTS = frozenset({"allow", "deny", "ask"})

#: How a merge reports one permission decision: the refs it is about, the outcome
#: (``withheld`` or ``relayed``) and the reason. Injected, because this module is a
#: leaf and the caller owns the audit contract -- and named as one alias so the
#: call shapes cannot drift apart.
_AuditDecision = Callable[[str, str, str], None]

#: Size bounds on an authored block, over which it is refused whole -- except a
#: ``match`` list, which is split instead (:func:`_split_authored_rule`).
#:
#: Not tidiness. KAS compiles each rule into Cedar statements and its own compiler
#: records that a condition tree past roughly 114 patterns overflows cedar-wasm's
#: stack -- ``match`` patterns are split into one statement each to stay shallow,
#: but ``exclude`` patterns are ANDed onto every statement and are NOT split, so a
#: long exclude list is the shape that still goes deep (``cedar-compiler.ts``
#: ``buildConditionVariants``). A crash there is a failed session, not a refused
#: rule, so the bound belongs on this side where the cost is one log line. The
#: pattern-count cap sits under KAS's own number with room to spare; the rule cap
#: and the length cap bound the projected payload, which travels on every
#: ``session/new``.
_MAX_RULES = 200
_MAX_PATTERNS_PER_RULE = 64
_MAX_PATTERN_LENGTH = 1024


def _asking_only(refs: str, outcome: str, reason: str) -> None:
    """The ``audit_decision`` of :func:`projection_accepts`: a question, not a refusal."""


def projection_accepts(raw: Any) -> bool:
    """Whether :func:`parse_user_permissions` would relay *raw* rather than refuse it whole.

    For a caller that must ask without the refusal being reported: a writer checking
    a block before it lands (one it then declines to write is reported nowhere, as
    the refusal it avoids never happens), and the session-start notice.
    """
    return parse_user_permissions(raw, audit_decision=_asking_only) is not None


def projection_dropped_rules(raw: Any) -> list[int]:
    """The indices of the ``allow`` rules a relayed *raw* leaves out for an ``exclude``
    too long to send, asked as quietly as :func:`projection_accepts`.

    For the session-start notice, so a grant that keeps prompting is named. Empty
    for a block refused whole, which that notice names as ``permissions`` instead.
    """
    dropped: list[int] = []
    if parse_user_permissions(raw, audit_decision=_asking_only, dropped=dropped) is None:
        return []
    return dropped


def _refuse_block(
    agent_id: str,
    why: str,
    audit_decision: _AuditDecision = lambda refs, outcome, reason: None,
) -> None:
    """Report a refused ``permissions`` block, at WARNING because it is the
    author's own file being declined rather than a grant quietly narrowed, and in
    the security event log because refusing a block the author wrote withholds
    every grant in it."""
    if audit_decision is _asking_only:
        return
    audit_decision("the authored `permissions` block", "withheld", f"refused: {why}")
    logger.warning(
        "agent %r: refusing its whole `permissions` block -- %s. None of it travels; "
        "the rules derived from `allowedTools` still do, and anything the block "
        "described keeps prompting",
        agent_id,
        why,
    )


#: What :func:`_parse_rule` returns for a rule left out on its own, the block kept.
_DROPPED: dict[str, Any] = {}


def _drop_rule(agent_id: str, where: str, count: int, audit_decision: _AuditDecision) -> None:
    """Report one authored ``allow`` left out for an ``exclude`` list too long to send."""
    if audit_decision is _asking_only:
        return
    audit_decision(
        f"the authored `permissions` {where}",
        "withheld",
        f"an allow whose `exclude` carries {count} patterns, over {_MAX_PATTERNS_PER_RULE}",
    )
    logger.warning(
        "agent %r: not relaying %s of its `permissions` block -- an allow whose "
        "`exclude` carries %d patterns, over the %d KAS's policy compiler stays "
        "shallow for. An exclude list cannot be split, so what that allow covered "
        "keeps prompting; the rest of the block still travels",
        agent_id,
        where,
        count,
        _MAX_PATTERNS_PER_RULE,
    )


def _split_authored_rule(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """*rule* as rules of at most :data:`_MAX_PATTERNS_PER_RULE` ``match`` patterns each.

    The same grant. KAS already compiles each ``match`` pattern into a statement of
    its own, with the rule's ``exclude`` ANDed onto every one, and decides on the
    most restrictive match whatever the order. So each part keeps the capability,
    the effect and the whole ``exclude``, and together the parts cover exactly the
    patterns the author listed. The parts stay next to each other, in order.
    """
    match = rule.get("match")
    if not isinstance(match, list) or len(match) <= _MAX_PATTERNS_PER_RULE:
        return [rule]
    return _chunked_rules(rule["capability"], rule["effect"], match, exclude=rule.get("exclude"))


def _parse_rule(
    entry: Any,
    index: int,
    agent_id: str,
    audit_decision: _AuditDecision,
) -> dict[str, Any] | None:
    """One rule of a user block, or ``None`` when the whole block must be refused.

    A ``match`` over :data:`_MAX_PATTERNS_PER_RULE` patterns is accepted here and
    split on the way out (:func:`_split_authored_rule`). An ``allow`` whose
    ``exclude`` is over that bound returns :data:`_DROPPED`: it cannot be split, and
    leaving one allow out only narrows the block.

    Every other failure here is fatal to the BLOCK, not to the rule: a block that is
    half relayed is a policy nobody wrote, and the half that survives is the half
    with no ``deny`` in it. KAS itself is stricter still in the same direction --
    an agent-profile policy it cannot parse fail-closes its engine to deny-all
    (``policy-session.ts`` ``parseAgentPermissions``) -- so refusing to forward is
    also the reading that keeps a malformed block from taking the session with it.

    An unknown key is a refusal too. KAS's schema ignores one, but a key this
    module cannot read may be the half of the rule that NARROWS it, and honouring
    the rest of a rule whose narrowing was dropped is how a grant widens.
    """
    where = f"rule {index}"
    if not isinstance(entry, dict):
        _refuse_block(
            agent_id,
            f"{where} is a {type(entry).__name__}, not an object",
            audit_decision=audit_decision,
        )
        return None
    unknown = sorted(str(key) for key in set(entry) - _RULE_KEYS)
    if unknown:
        _refuse_block(
            agent_id,
            f"{where} carries unknown key(s) {', '.join(unknown)}",
            audit_decision=audit_decision,
        )
        return None
    capability = entry.get("capability")
    if not isinstance(capability, str) or capability not in KAS_CAPABILITIES:
        _refuse_block(
            agent_id,
            f"{where} names no KAS capability ({capability!r})",
            audit_decision=audit_decision,
        )
        return None
    effect = entry.get("effect")
    # `isinstance` BEFORE the membership test, and not for tidiness: an unhashable
    # value (`"effect": ["allow"]`, the shape its array-valued siblings `match` and
    # `exclude` invite) raises `TypeError` out of a `frozenset` lookup, and nothing
    # on the path from here to session creation catches it -- the harness handles
    # only `KasAgentTranslationError` and `ForkGovernanceUnresolved`. A malformed
    # block must cost the block, never the session.
    if not isinstance(effect, str) or effect not in _EFFECTS:
        _refuse_block(
            agent_id,
            f"{where} has effect {effect!r}, not allow/deny/ask",
            audit_decision=audit_decision,
        )
        return None
    rule: dict[str, Any] = {"capability": capability}
    for key in ("match", "exclude"):
        if key not in entry:
            continue
        patterns = entry[key]
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) and p.strip() for p in patterns
        ):
            _refuse_block(
                agent_id,
                f"{where} `{key}` is not an array of patterns",
                audit_decision=audit_decision,
            )
            return None
        if key == "exclude" and len(patterns) > _MAX_PATTERNS_PER_RULE:
            # A long `match` is split later; a long `exclude` cannot be. Each
            # exclude is ANDed onto its rule, so two rules sharing a `match` with
            # halves of the list each let through what the other half excludes.
            # Dropping one rule only narrows when it is an `allow`: a dropped
            # `deny` or `ask` would let the block's allows cover what it guarded.
            if effect == "allow":
                _drop_rule(agent_id, where, len(patterns), audit_decision)
                return _DROPPED
            _refuse_block(
                agent_id,
                f"{where} `exclude` carries {len(patterns)} patterns, over the "
                f"{_MAX_PATTERNS_PER_RULE} KAS's policy compiler stays shallow for, "
                "and an exclude list cannot be split without changing what it means",
                audit_decision=audit_decision,
            )
            return None
        if any(len(p) > _MAX_PATTERN_LENGTH for p in patterns):
            _refuse_block(
                agent_id,
                f"{where} `{key}` carries a pattern over {_MAX_PATTERN_LENGTH} characters",
                audit_decision=audit_decision,
            )
            return None
        rule[key] = list(patterns)
    rule["effect"] = effect
    return rule


def parse_user_permissions(
    raw: Any,
    *,
    agent_id: str = "",
    audit_decision: _AuditDecision = lambda refs, outcome, reason: None,
    dropped: list[int] | None = None,
) -> list[dict[str, Any]] | None:
    """Rule 1: the author's ``permissions`` block, read against KAS's own shape.

    ``None`` for an absent block and for a refused one alike -- the caller's
    behaviour is the same either way, and the log line is what separates them.
    *dropped*, when given, collects the index of each rule left out alone.

    A ``policies`` reference refuses the block. It is well formed, and that is the
    problem: KAS expands a named bundle inline into ``allow`` rules held on the
    backend (``policy-parser.ts`` ``expandPolicies`` over ``presets.ts``), so the
    grant is not in this block at all and there is nothing here to intersect with
    the ceiling. The shipped ``dev-shell`` bundle alone carries shell allows,
    which is the one family rule 4 exists to refuse.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _refuse_block(
            agent_id,
            f"a permissions block must be an object, got {type(raw).__name__}",
            audit_decision=audit_decision,
        )
        return None
    unknown = sorted(str(key) for key in set(raw) - _BLOCK_KEYS)
    if unknown:
        _refuse_block(
            agent_id, f"unknown key(s) {', '.join(unknown)}", audit_decision=audit_decision
        )
        return None
    policies = raw.get("policies")
    if policies is not None and not isinstance(policies, list):
        # Tested for TYPE before emptiness: `""` and `0` are falsey, so a truth test
        # alone reads a malformed value as "no bundles" and forwards the rules
        # beside it -- the one shape where a refusal is skipped by accident.
        _refuse_block(
            agent_id, "`policies` must be an array of policy ids", audit_decision=audit_decision
        )
        return None
    if policies:
        _refuse_block(
            agent_id,
            "it references named policy bundles, whose rules live on the backend "
            "where Crew's ceiling cannot see them",
            audit_decision=audit_decision,
        )
        return None
    rules = raw.get("rules")
    if not isinstance(rules, list):
        _refuse_block(agent_id, "`rules` must be an array", audit_decision=audit_decision)
        return None
    if len(rules) > _MAX_RULES:
        _refuse_block(
            agent_id,
            f"it carries {len(rules)} rules, over the {_MAX_RULES} this projection "
            "relays -- the policy travels on every session",
            audit_decision=audit_decision,
        )
        return None
    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(rules):
        rule = _parse_rule(entry, index, agent_id, audit_decision)
        if rule is None:
            return None
        if rule is not _DROPPED:
            parsed.append(rule)
        elif dropped is not None:
            dropped.append(index)
    # The bound is on what travels, and a long `match` travels as several rules.
    sent = sum(len(_split_authored_rule(rule)) for rule in parsed)
    if sent > _MAX_RULES:
        _refuse_block(
            agent_id,
            f"it comes to {sent} rules once each `match` is split to "
            f"{_MAX_PATTERNS_PER_RULE} patterns, over the {_MAX_RULES} this projection "
            "relays -- the policy travels on every session",
            audit_decision=audit_decision,
        )
        return None
    return parsed


def _bounded_mcp_servers(match: Any) -> list[str] | None:
    """The literal servers an ``mcp`` rule's ``match`` confines it to.

    ``None`` when the rule is not confined to servers that can be named: no
    ``match`` and an empty one both mean every resource to KAS
    (``cedar-compiler.ts`` ``isUnconditionalMatch``), and a glob in the SERVER
    part means servers that do not exist yet. The ceiling is asked per server, so
    a rule with no nameable server has no question to ask and does not travel.
    """
    if not isinstance(match, list) or not match:
        return None
    servers: list[str] = []
    for pattern in match:
        server = pattern.strip().split("/", 1)[0]
        if not server or _GLOB_METACHARACTERS.intersection(server):
            return None
        servers.append(server)
    return servers


def _deriver_could_emit(rule: dict[str, Any]) -> bool:
    """Whether :func:`allowed_tools_to_permissions` can express this rule itself.

    Exactly the shapes that function emits: an ``allow``, no ``exclude``, and either
    the one ``mcp`` rule whose ``match`` names literal servers, or a bare capability
    rule from :data:`CAPABILITY_BY_TOOL` with no ``match`` at all. Anything else -- a
    ``deny``, an ``ask``, a scoped or excluded ``allow``, a capability that table does
    not carry -- is something a tool-name allowlist cannot say, so it can only have
    been authored.

    Read on the VALUES, not on key presence. ``exclude: []`` excludes nothing and
    ``match: []`` matches every resource (``cedar-compiler.ts``
    ``isUnconditionalMatch``), so both rules mean exactly what the bare form means --
    and a key-presence test would read them as narrowed, hand the grant back to the
    block, and let two empty arrays carry a rule past the channel that owns it.
    """
    if rule["effect"] != "allow" or rule.get("exclude"):
        return False
    capability = rule["capability"]
    if capability == _MCP_CAPABILITY:
        return _bounded_mcp_servers(rule.get("match")) is not None
    return capability in CEILING_REFS_BY_CAPABILITY and not rule.get("match")


def _user_allow_travels(
    rule: dict[str, Any],
    *,
    ceiling_permits: Callable[[str], bool],
    audit_decision: _AuditDecision,
    agent_id: str,
) -> bool:
    """Rules 3 and 4 for one user ``allow``.

    Read over the rule's EXPANSION, so a meta capability is answered for every
    concrete capability inside it and one refusal is enough to drop the rule. The
    rule is never rewritten down to its permitted part: narrowing a grant the
    author wrote and relaying the remainder produces a rule nobody authored.

    Every decision is reported through ``audit_decision`` as well as logged. It is
    a permission DECISION, and it is the ordinary case here rather than an edge one
    -- an authored block is the input this merge exists to read, and a governed host
    withholding a server is routine -- so the trail that the sibling derived path
    emits deliberately (``kas_agents._ceiling_permitted``) must cover this path too.
    The ``reason`` argument is what tells the classes apart in the log: a ceiling
    withhold, Crew's own standing refusal of the shell and filesystem families, and
    a whole-block refusal are three different decisions and a reader of the trail
    has to be able to see which one happened.
    """
    capabilities = META_CAPABILITY_EXPANSION.get(rule["capability"], (rule["capability"],))
    withheld = sorted(set(capabilities) & WITHHELD_CAPABILITIES)
    if withheld:
        logger.info(
            "agent %r: not relaying its `permissions` allow for %r -- %s cannot be "
            "auto-approved on this backend, because an auto-approved call raises no "
            "permission request and Crew's deny floor and sensitive-path check run on "
            "that request. It keeps prompting unless it is an unscoped shell/fs_read/"
            "fs_write allow, or a shell allow whose plain globs Crew's gate answers",
            agent_id,
            rule["capability"],
            ", ".join(withheld),
        )
        audit_decision(rule["capability"], "withheld", "shell/filesystem family, Crew policy")
        return False
    for capability in capabilities:
        if capability == _MCP_CAPABILITY:
            servers = _bounded_mcp_servers(rule.get("match"))
            if servers is None:
                logger.info(
                    "agent %r: not relaying its `permissions` allow for %r -- its "
                    "`match` names no literal server, so the ceiling cannot be asked "
                    "about the servers it would cover",
                    agent_id,
                    rule["capability"],
                )
                audit_decision(
                    rule["capability"], "withheld", "unbounded `match`, ceiling unaskable"
                )
                return False
            refused = sorted({s for s in servers if not ceiling_permits(f"{_MCP_PREFIX}{s}")})
            if refused:
                logger.info(
                    "agent %r: not relaying its `permissions` allow for %r -- the "
                    "governance ceiling withholds auto-approval for %s",
                    agent_id,
                    rule["capability"],
                    ", ".join(refused),
                )
                audit_decision(
                    ", ".join(f"{_MCP_PREFIX}{s}" for s in refused),
                    "withheld",
                    "governance ceiling",
                )
                return False
            continue
        refs = CEILING_REFS_BY_CAPABILITY.get(capability, ())
        if not refs:
            logger.info(
                "agent %r: not relaying its `permissions` allow for %r -- %s has no "
                "tool ref to put to the governance ceiling, and a grant travels only "
                "on an affirmative answer",
                agent_id,
                rule["capability"],
                capability,
            )
            audit_decision(capability, "withheld", "no tool ref, ceiling unaskable")
            return False
        withheld_refs = sorted(ref for ref in refs if not ceiling_permits(ref))
        if withheld_refs:
            logger.info(
                "agent %r: not relaying its `permissions` allow for %r -- the "
                "governance ceiling withholds auto-approval for %s",
                agent_id,
                rule["capability"],
                ", ".join(withheld_refs),
            )
            audit_decision(", ".join(withheld_refs), "withheld", "governance ceiling")
            return False
    return True


def merge_user_permissions(
    derived: dict[str, Any] | None,
    raw: Any,
    *,
    ceiling_permits: Callable[[str], bool],
    audit_decision: _AuditDecision = lambda refs, outcome, reason: None,
    allowlist_present: bool = True,
    agent_id: str = "",
    source: str = "authored `permissions`",
) -> dict[str, Any] | None:
    """The ``allowedTools``-derived policy, with the author's own block folded in.

    Four rules, and the order they are applied in is the order they are numbered:

    1. The block is parsed against KAS's shape and refused whole when it does not
       fit (:func:`parse_user_permissions`). No salvage: a partially accepted
       block is a policy nobody wrote. Two size cases are not misfits: a
       ``match`` too long for one rule is sent as several, and an ``allow`` whose
       ``exclude`` is too long is left out alone, which only narrows.
    2. A user ``deny`` or ``ask`` travels unconditionally. Narrowing is always
       safe, and an author who writes ``deny`` is entitled to be obeyed even where
       the ceiling would have allowed.
    3. A user ``allow`` travels only when the ceiling permits the same capability
       AND the same resource scope. For an ``mcp`` rule both halves are one
       question -- the resource IS a server, and ``may_skip_gate_now("@server")``
       is the predicate that answers for a server, per-tool rules included. For a
       builtin capability the ref is the tool name the ceiling knows.
    4. The shell and filesystem families do not travel as an ``allow`` at all
       (:data:`WITHHELD_CAPABILITIES`), which leaves them with no rule, and no
       rule is ``ask``; Crew's gate answers what it can read of them instead.

    ``allowlist_present`` says whether the spec carries an ``allowedTools`` list, and
    it decides which channel owns a grant that BOTH could express. A rule the
    derivation could have emitted itself (:func:`_deriver_could_emit`) is dropped when
    the list is there, because the list is the governed input and it is re-derived on
    every projection -- so a ``permissions`` block that has gone stale against it,
    including one Crew's own seeder wrote and then preserved
    (``agent._seed_kas_permissions``), cannot put a revoked grant back. With no list at
    all there is no derivation to be authoritative, so such a rule is the author's own
    and travels: that is the pure-KAS agent this merge exists for. The residue is named
    rather than hidden -- a spec whose ``allowedTools`` key was deleted outright while a
    stale block stayed behind reads as authored, and for Crew's own managed specs that
    state does not survive the next ``rebuild_agent_config``.

    ``ceiling_permits`` and ``audit_decision`` are injected rather than imported so
    this module stays a leaf that depends on nothing else in the package; the caller
    passes :func:`kiro_crew.platform.governance.may_skip_gate_now`, which fails
    closed, and its own security-event-log writer. ``audit_decision`` defaults to a
    no-op so a caller that only wants the translation is not forced to supply one,
    and it is called with ``(refs, reason)`` -- the refs the ceiling refused and why
    -- rather than with a formatted sentence, so the event's wording belongs to the
    module that owns the audit contract.

    The surviving user rules are placed BEFORE the derived ones. Under KAS's
    evaluator the position carries no meaning: rules compile to Cedar permit /
    forbid statements and the decision is the most restrictive match, ``deny`` over
    ``ask`` over ``allow``, with an unmatched request resolving to ``ask``
    (``policy-engine.ts``, ``cedar-compiler.ts``). The order is chosen so the array
    does not depend on that reading being right -- every derived rule is an
    ``allow``, so putting the author's rules first is also the safe order under a
    first-match-wins evaluator, where a user ``deny`` would otherwise lose to a
    derived ``allow`` it was written to override. One order that is correct under
    both readings beats a proof that only one reading is live.
    """
    rules = parse_user_permissions(raw, agent_id=agent_id, audit_decision=audit_decision)
    if not rules:
        return derived
    kept: list[dict[str, Any]] = []
    for rule in rules:
        if rule["effect"] == "allow":
            if allowlist_present and _deriver_could_emit(rule):
                logger.info(
                    "agent %r: not relaying its `permissions` allow for %r -- this spec "
                    "has an `allowedTools` list, which is the governed input for a grant "
                    "of that shape and is re-derived every projection, so the block "
                    "cannot add one the list does not carry",
                    agent_id,
                    rule["capability"],
                )
                audit_decision(rule["capability"], "withheld", "derivable from `allowedTools`")
                continue
            if not _user_allow_travels(
                rule,
                ceiling_permits=ceiling_permits,
                audit_decision=audit_decision,
                agent_id=agent_id,
            ):
                continue
        kept.append(rule)
    if not kept:
        return derived
    audit_decision(
        ", ".join(f"{r['capability']}:{r['effect']}" for r in kept),
        "relayed",
        f"{source}, ceiling cleared",
    )
    base = list(derived["rules"]) if derived else []
    # Split only now: each decision above is about the rule the author wrote, so
    # an allow travels or stays home whole rather than in permitted parts.
    sent = [part for rule in kept for part in _split_authored_rule(rule)]
    return {"rules": sent + base}


#: Every capability an ``allow`` can reach KAS for from this module: the ones
#: :data:`CAPABILITY_BY_TOOL` carries, and MCP. The shell and filesystem families
#: never travel as an allow (:data:`WITHHELD_CAPABILITIES`); a hook on them still
#: needs an ``ask`` (:data:`HOOK_GATEABLE_CAPABILITIES`).
AUTO_APPROVABLE_CAPABILITIES: tuple[str, ...] = tuple(
    sorted(set(CAPABILITY_BY_TOOL.values()) | {_MCP_CAPABILITY})
)

#: Every capability a PreToolUse hook can need an ``ask`` on: the auto-approvable
#: ones and the shell and filesystem families. Those never travel as an allow,
#: but KAS still decides their calls without a permission request when a saved
#: session answer, a KAS default or a user-scope rule allows them. An explicit
#: ``ask`` outranks all three, so the call reaches Crew's gate, where the hook runs.
HOOK_GATEABLE_CAPABILITIES: tuple[str, ...] = tuple(
    sorted(set(AUTO_APPROVABLE_CAPABILITIES) | WITHHELD_CAPABILITIES)
)

#: :data:`HOOK_GATEABLE_CAPABILITIES` without MCP: what ``@builtin`` covers.
_BUILTIN_GATEABLE_CAPABILITIES: tuple[str, ...] = tuple(
    c for c in HOOK_GATEABLE_CAPABILITIES if c != _MCP_CAPABILITY
)

#: kiro-cli's ``@builtin`` matcher: every built-in tool, never an MCP one.
HOOK_BUILTIN_MATCHER = "@builtin"

#: kiro-cli's ``@mcp`` tag: every MCP tool.
HOOK_MCP_TAG = "@mcp"

#: kiro-cli tool -> the KAS capability its calls are evaluated under, for the
#: tools :data:`CAPABILITY_BY_TOOL` leaves out because they are never auto-approved
#: (:data:`WITHHELD_FROM_AUTO_APPROVE`). A hook on one of them still needs an
#: ``ask``. Read by the hook coverage only. ``code`` is under all three.
_HOOK_CAPABILITIES_BY_TOOL: dict[str, frozenset[str]] = _ANSWERED_CAPABILITIES

#: kiro-cli's tool tags a spec hook matcher may name, as kiro-cli's agent reads
#: them (``agent/src/agent/mod.rs`` ``hook_matcher_tag_groups_and_names``): each tag
#: -> the kiro-cli tools it selects. For spec hooks only; the shared alias table
#: (:data:`KIRO_TOOL_ALIASES`) is not changed by it.
SPEC_HOOK_TOOL_TAGS: dict[str, tuple[str, ...]] = {
    "read": ("fs_read", "grep", "glob", "code"),
    "write": ("fs_write", "code"),
    "shell": ("execute_bash",),
    "web": ("web_fetch", "web_search"),
    "subagent": ("use_subagent",),
}

#: Shell command a KAS ``deny`` rule names alongside a hook's unscoped shell
#: ``ask``. KAS denies a shell command its parser cannot read only when no rule
#: asks for every command, or when any shell ``deny`` rule exists
#: (``policy-engine.ts`` ``unparseable``). The hook's ask would turn that deny into
#: a permission request for a command no gate can read; this rule keeps it a deny.
#: It names a command nobody runs, so it denies nothing else.
UNPARSEABLE_SHELL_GUARD = "kirocrew-unparseable-shell-guard"


def is_unparseable_shell_guard(rule: Any) -> bool:
    """Whether *rule* is the :data:`UNPARSEABLE_SHELL_GUARD` ``deny`` the hook asks
    carry: Crew's own rule, never one the agent's author wrote, so a reader asking
    what the author restricted skips it."""
    return isinstance(rule, dict) and rule == {
        "capability": "shell",
        "match": [UNPARSEABLE_SHELL_GUARD],
        "effect": "deny",
    }


#: Prefix of a :func:`hook_gated_capabilities` entry naming the MCP resource
#: pattern a hook covers, as KAS's ``<server>/<tool>`` resource
#: (``mcp:srv/get_creds``, or ``mcp:srv/*`` for a whole server), rather than the
#: whole ``mcp`` capability.
HOOK_GATED_MCP_PREFIX = "mcp:"

#: Prefix of an :func:`auto_approved_capabilities` entry naming an MCP resource
#: pattern a scoped ``ask`` or ``deny`` holds back from the ``mcp`` grant
#: (``mcp-held:srv/tool``).
HELD_MCP_PREFIX = "mcp-held:"


def auto_approved_capabilities(policy: Any) -> frozenset[str]:
    """The capabilities *policy* auto-approves, as KAS resolves it.

    An ``allow`` grants its capability (a meta capability grants its expansion),
    and an unscoped ``ask`` or ``deny`` on the same capability outranks it, since
    KAS resolves the most restrictive matching rule. A scoped ``ask`` or ``deny``
    leaves the rest of the capability approved, so it cancels nothing here: the
    answer errs toward "still auto-approved", which is the side that makes the
    caller re-project.

    While ``mcp`` is approved, each pattern a scoped ``mcp`` ``ask`` or ``deny``
    names is also listed, as a :data:`HELD_MCP_PREFIX` entry, so
    :func:`hook_gated_auto_approved` can tell a per-tool withhold already in
    place from one still missing, and each server the grant is confined to, as a
    :data:`APPROVED_MCP_SERVER_PREFIX` entry, so coverage can be re-read against
    the same servers (:func:`approved_mcp_servers`).
    """
    return _approved_under(policy, ())


def unprompted_capabilities(policy: Any) -> frozenset[str]:
    """The capabilities whose calls *policy* lets KAS decide without asking Crew.

    :func:`auto_approved_capabilities`, plus every
    :data:`HOOK_GATEABLE_CAPABILITIES` entry no unscoped ``ask`` or ``deny``
    holds: with no rule of its own, a capability's call can still be decided by a
    session answer Crew saved, a KAS default or a user-scope rule, none of which
    the projection sees. This is what a live session is checked against when the
    PreToolUse hooks change (:func:`hook_gated_auto_approved`), so a hook on the
    shell, or on a session with no ``permissions`` at all, re-projects it.
    """
    return _approved_under(policy, HOOK_GATEABLE_CAPABILITIES)


def _approved_under(policy: Any, unprompted: Iterable[str]) -> frozenset[str]:
    """:func:`auto_approved_capabilities` with *unprompted* granted before any rule."""
    rules = policy.get("rules") if isinstance(policy, dict) else None
    allowed: set[str] = set(unprompted)
    cancelled: set[str] = set()
    held: set[str] = set()
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, dict) or not isinstance(rule.get("capability"), str):
            continue
        capabilities = META_CAPABILITY_EXPANSION.get(rule["capability"], (rule["capability"],))
        if rule.get("effect") == "allow":
            allowed.update(capabilities)
        elif rule.get("effect") in ("ask", "deny") and not rule.get("exclude"):
            if not rule.get("match"):
                cancelled.update(capabilities)
            elif rule["capability"] == _MCP_CAPABILITY:
                held.update(_patterns(rule["match"]))
    approved = allowed - cancelled
    if _MCP_CAPABILITY in approved:
        approved.update(HELD_MCP_PREFIX + pattern for pattern in held)
        servers = auto_approved_mcp_servers(policy)
        approved.update(APPROVED_MCP_SERVER_PREFIX + server for server in servers or ())
    return frozenset(approved)


# --- kiro-cli ``toolsSettings`` -> KAS ``permissions`` -------------------------
#
# KAS declares ``toolsSettings`` a CLI-only field and replaces it with
# ``permissions``; its migration guide maps each field onto a capability rule.
# The translation below follows that guide, with two limits of its own:
#
# * Only what can be said faithfully is translated. A kiro-cli regex becomes a
#   glob only when it is a literal plus ``.*``; anything else is reported, never
#   guessed at, because a guessed deny can silently stop matching.
# * No ALLOW is emitted. The gap being closed is lost restrictions; an allow
#   left behind only means the call keeps prompting, and shell/filesystem allows
#   are a family Crew never auto-approves here (:data:`WITHHELD_CAPABILITIES`).
#   Every allow key is reported as not carried instead.
#
# The block this produces is fed to :func:`merge_user_permissions` like an
# authored one, so its denies travel as written.

#: Characters that keep a regex from being read as a literal. ``*`` is here too:
#: outside ``.*`` it is a quantifier on the character before it.
_REGEX_METACHARACTERS = frozenset(".^$*+?()[]{}|\\")

#: Shell redirect operators. KAS matches a shell rule against the command's
#: words, and a redirect and its target are not among them, so a deny written
#: to match one never sees it.
_REDIRECT_CHARACTERS = frozenset("<>")


def _regex_to_glob(pattern: str) -> str | None:
    """A kiro-cli regex (anchored ``\\A..\\z``) as a KAS glob, or ``None``.

    Only ``.*`` and backslash-escaped punctuation are understood; any other regex
    syntax returns ``None`` so the caller reports it instead of guessing. A
    literal character KAS would read as glob syntax (``*?[]{}!``) has no glob
    spelling, so it returns ``None`` too.
    """
    body = pattern.strip()
    for prefix in ("\\A", "^"):
        body = body.removeprefix(prefix)
    for suffix in ("\\z", "$"):
        if body.endswith(suffix) and not body.endswith("\\" + suffix):
            body = body[: -len(suffix)]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if body.startswith(".*", i):
            out.append("*")
            i += 2
        elif ch == "\\" and i + 1 < len(body) and not body[i + 1].isalnum():
            if body[i + 1] in _GLOB_METACHARACTERS:
                return None
            out.append(body[i + 1])
            i += 2
        elif ch in _REGEX_METACHARACTERS or ch in _GLOB_METACHARACTERS:
            return None
        else:
            out.append(ch)
            i += 1
    return "".join(out) or None


def _url_glob(raw: str) -> str | None:
    """A ``web_fetch.blocked`` entry as a KAS host glob, or ``None``. KAS matches
    web_fetch rules against the URL's host, so a pattern that names a scheme or a
    path can never match there."""
    glob = _regex_to_glob(raw)
    return glob if glob is not None and not set(glob) & set("/:") else None


def _patterns(value: Any) -> list[str]:
    return (
        [p.strip() for p in value if isinstance(p, str) and p.strip()]
        if isinstance(value, list)
        else []
    )


def _chunked_rules(
    capability: str,
    effect: str,
    patterns: list[str],
    *,
    exclude: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One rule per :data:`_MAX_PATTERNS_PER_RULE` patterns, so no rule is refused for size.

    *exclude*, when given, is carried whole on every part.
    """
    step = _MAX_PATTERNS_PER_RULE
    narrowing = {} if exclude is None else {"exclude": list(exclude)}
    return [
        {"capability": capability, "match": patterns[i : i + step], **narrowing, "effect": effect}
        for i in range(0, len(patterns), step)
    ]


def tools_settings_to_permissions(
    tools_settings: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Translate a kiro-cli ``toolsSettings`` object into a KAS ``permissions`` block.

    Returns ``(block, not_carried)``: the block (``None`` when no rule came out)
    and the ``<tool>.<key>`` paths that have no faithful rule, so the caller can
    name exactly what this session runs without.
    """
    if not isinstance(tools_settings, dict):
        return None, []
    rules: list[dict[str, Any]] = []
    lost: list[str] = []
    star_subtrees: list[tuple[str, str, list[str]]] = []
    # Allows go in last (below), so an over-long list can never sink the denies.
    allow_groups: list[tuple[str, list[dict[str, Any]]]] = []

    def converted(where: str, values: Any, convert: Callable[[str], str | None]) -> list[str]:
        out: list[str] = []
        for raw in _patterns(values):
            glob = convert(raw)
            # An over-long pattern would make the merge refuse the WHOLE block,
            # so it is dropped and reported here instead.
            if glob is None or len(glob) > _MAX_PATTERN_LENGTH:
                lost.append(where)
            else:
                out.append(glob)
        return list(dict.fromkeys(out))

    for tool in sorted(tools_settings, key=str):
        settings = tools_settings[tool]
        family = _TOOLS_SETTINGS_FAMILY.get(tool) if isinstance(tool, str) else None
        if not isinstance(settings, dict):
            if settings:
                lost.append(str(tool))
            continue
        if family is None:
            lost.extend(f"{tool}.{key}" for key in sorted(settings, key=str) if settings[key])
            continue
        # Only shapes run live on KAS are carried: the plain deny lists, and the
        # `subagent` rules below (a deny with an `exclude`, and a name-scoped
        # allow). The deny-all-but shapes on a command or a path, where
        # `exclude` has not been run, are enforced by Crew's gate instead: the
        # shell's `denyByDefault` and the write tool's `fallbackAction: "deny"`
        # (below).
        handled: set[str] = set()

        if family == "shell":
            handled.add("deniedCommands")
            denied = converted(
                f"{tool}.deniedCommands", settings.get("deniedCommands"), _regex_to_glob
            )
            rules.extend(_chunked_rules("shell", "deny", denied))
            # kiro-cli matches the regex over the whole command line; KAS matches
            # the glob over the command's words, which do not include a redirect
            # or its target. The deny still travels, since it does stop
            # the character inside a word, but the entry is named as not carried.
            if any(set(glob) & _REDIRECT_CHARACTERS for glob in denied):
                lost.append(f"{tool}.deniedCommands")
            # No rule either: Crew's gate refuses every shell call the spec's
            # usable `allowedCommands` do not match (`spec_shell_allowlist`).
            handled.add("denyByDefault")
            # No rule: Crew answers the shell requests these match itself, at
            # the gate (`crew_answered_commands`). Only an entry it cannot use
            # is named.
            handled.add("allowedCommands")
            allowed = settings.get("allowedCommands")
            # Named when an entry is unusable, and when the bound cuts the list:
            # those commands keep prompting. The notice then names each such entry
            # in place of the key (`name_unreadable_entries`).
            if allowed and (
                not isinstance(allowed, list)
                or any(allowed_command_pattern(p) is None for p in allowed)
                or len(_usable_allowed_commands(tools_settings)) > _MAX_ALLOWED_COMMANDS
            ):
                lost.append(f"{tool}.allowedCommands")
            # No rule: Crew's read-only row answers a read-only command, also
            # under `denyByDefault` (`kas_spec_grants`). A non-boolean is named.
            if isinstance(settings.get("autoAllowReadonly"), bool):
                handled.add("autoAllowReadonly")
        elif family in ("read", "write"):
            capability = "fs_read" if family == "read" else "fs_write"
            handled.add("deniedPaths")
            # kiro-cli globs and KAS's filesystem globs share one syntax. A path
            # not already ending in a wildcard also gets a `/**` twin, so a bare
            # directory denies what is inside it, not just the directory entry.
            # Denying a file's non-existent subtree is harmless: wider, never looser.
            # A path ending in one `*` gets the twin too: kiro-cli's globset `*`
            # crosses `/` and KAS's minimatch `*` stops at it, so `/etc/*` alone
            # would leave `/etc/a/b` open on KAS. `/etc/*/**` closes it.
            denied_paths = converted(f"{tool}.deniedPaths", settings.get("deniedPaths"), str)
            fits = [p for p in denied_paths if len(p) + 3 <= _MAX_PATTERN_LENGTH]
            subtrees = [f"{p.rstrip('/')}/**" for p in fits if not p.endswith("*")]
            denied_paths = list(dict.fromkeys(denied_paths + subtrees))
            rules.extend(_chunked_rules(capability, "deny", denied_paths))
            if tool in READ_SETTINGS_KEYS:
                # No rule: Crew's gate answers a covered read (`crew_answered_read_paths`).
                handled.add("allowedPaths")
                if _paths_not_all_carried(settings.get("allowedPaths"), tools_settings, tool):
                    lost.append(f"{tool}.allowedPaths")
            # Kept apart so they never cost the denies above their place: they
            # travel only when the whole block still fits (below).
            star_twins = [
                f"{p}/**"
                for p in fits
                if p.endswith("*")
                and not p.endswith(("**", "\\*"))
                and f"{p}/**" not in denied_paths
            ]
            if star_twins:
                star_subtrees.append((f"{tool}.deniedPaths", capability, star_twins))
            if family == "write":
                # `interactive` is the default: nothing is lost by leaving it out.
                # `deny` travels as no rule: Crew's gate refuses every write the
                # usable `allowedPaths` do not cover (`spec_write_allowlist`).
                handled.add("fallbackAction")
                # No rule: Crew's gate answers a covered write (`crew_answered_paths`).
                # Named when an entry is unusable or the bound cuts the list.
                handled.add("allowedPaths")
                if _paths_not_all_carried(settings.get("allowedPaths"), tools_settings, tool):
                    lost.append(f"{tool}.allowedPaths")
                if settings.get("fallbackAction") not in (
                    None,
                    "",
                    "interactive",
                ) and not _write_falls_back_to_deny(settings):
                    lost.append(f"{tool}.fallbackAction")
        elif family == "web_fetch":
            # KAS matches a host only, so a `blocked` regex is carried here as a host
            # deny or named, and `trusted` (an allow) is named. Crew's gate grants a
            # plain-URL `trusted` entry itself (`kas_spec_denies.web_fetch_gate`); the
            # session notice drops it then, and a crewmate's policy check reads only
            # this KAS side.
            handled.add("blocked")
            rules.extend(
                _chunked_rules(
                    "web_fetch",
                    "deny",
                    converted(f"{tool}.blocked", settings.get("blocked"), _url_glob),
                )
            )
        elif family == "subagent":
            # KAS's `subagent` capability matches the sub-agent's name (its consent
            # names it as the resource), and it is the shape KAS itself writes for
            # trusted sub-agents (`{capability: "subagent", match: [ids], effect:
            # "allow"}`). Run live on kiro-cli 2.35.1: a deny over "*"
            # with the target in `exclude` let the call reach its permission
            # request, the same deny with another name excluded refused it, and
            # an allow naming it ran it unprompted.
            handled.update({"availableAgents", "trustedAgents"})
            if "availableAgents" in settings:
                rules.extend(
                    _available_agents_rules(
                        f"{tool}.availableAgents", settings["availableAgents"], lost
                    )
                )
            # An allow, so the merge puts it to the governance ceiling (the refs
            # that name the sub-agent tool) and drops it when the ceiling refuses.
            trusted = converted(f"{tool}.trustedAgents", settings.get("trustedAgents"), _agent_glob)
            allows = _chunked_rules("subagent", "allow", trusted)
            allow_groups.append((f"{tool}.trustedAgents", allows))

        lost.extend(
            f"{tool}.{key}"
            for key in sorted(settings, key=str)
            if key not in handled and settings[key]
        )

    if len(rules) > _MAX_RULES:
        return None, sorted(set(lost) | {"(all: too many rules)"})
    # The trailing-`*` subtree twins widen denies that already travel. Over the
    # rule bound they would sink the WHOLE block, so they are left out and the
    # key is named instead: the originals still deny what KAS's `*` reaches.
    for where, capability, twins in star_subtrees:
        extra = _chunked_rules(capability, "deny", twins)
        if len(rules) + len(extra) > _MAX_RULES:
            lost.append(where)
        else:
            rules.extend(extra)
    # Each key's allows whole, or named and left prompting: every deny still travels.
    for where, allows in allow_groups:
        if len(rules) + len(allows) > _MAX_RULES:
            lost.append(where)
        else:
            rules.extend(allows)
    return ({"rules": rules} if rules else None), sorted(set(lost))


# The PreToolUse hook coverage lives in its own module by size; these names stay
# importable from here, where every caller has always read them. Imported last:
# that module reads this one's tables.
from kiro_crew.acp.kas_hook_coverage import (  # noqa: E402,F401
    hook_gated_auto_approved,
    hook_gated_capabilities,
    hook_held_grants,
    withhold_hook_gated_auto_approval,
)
