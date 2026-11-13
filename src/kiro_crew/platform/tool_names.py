"""The two engines' built-in tool vocabularies, and how one is read as the other.

Crew's agent specs and an operator's ``auto_deny_tools`` / ``tools``-scope rules
are written in kiro-cli's vocabulary (``fs_read``, ``fs_write``, ``grep``,
``glob``). KAS registers the same work under different tool ids and matches an
allowlist entry against a tool's exact id with no translation (its only alias is
the ``execute_bash`` <-> ``execute_pwsh`` shell pair). Against the recorded
registry (``test/fixtures/kas_builtin_tool_ids.json``): ``fs_read``, ``grep`` and
``glob`` are not KAS tool ids at all (``fs_read`` exists there only as a policy
CAPABILITY name), so each mounts nothing; ``fs_write`` IS a registered KAS id --
the create/overwrite tool -- and also the capability name KAS files ``str_replace``,
``fs_append`` and ``delete_file`` under, so a bare ``fs_write`` mounts one tool of
the family and none of the rest. So a name written for kiro-cli names less on KAS
than it does on kiro-cli unless something translates it.

Three readings live here, and all are consumed:

* :data:`KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME` -- the MOUNT direction.
  ``kas_agents._project_tools`` carries each kiro-cli name with its family
  beside it, so a spec mounts on KAS what it mounts on kiro-cli.
* :data:`KIRO_CLI_NAME_BY_KAS_TOOL` -- the POLICY direction.
  ``hooks.on_tool_call`` evaluates a KAS id under its kiro-cli name as well, so
  a deny or a ``tools``-scope ceiling an operator wrote as ``fs_write`` binds to
  ``str_replace`` exactly where it binds to ``fs_write``. A mount widened in one
  vocabulary and a rule read in the other is how the operator's own control
  stops applying.
* :data:`KIRO_TOOL_ALIASES` -- kiro-cli's OWN short spellings.
  kiro-cli stamps ``shell``, ``read`` and ``write`` on the built-ins a spec and a
  rule call ``execute_bash``, ``fs_read`` and ``fs_write``; the KAS projection
  matches a spec entry under either spelling and ``hooks.on_tool_call`` reads a
  stamp under the one a rule is written in.

A LEAF on purpose: stdlib only, so both the ACP projection and the PreToolUse
gate can import it with no cycle (``hooks`` cannot import from ``kiro_crew.acp``
without pulling the runtime).
"""

from __future__ import annotations

from typing import Any

#: kiro-cli built-in tool name -> the KAS built-in tool ids that do the same work.
#:
#: Per kiro-cli name, the KAS built-ins that do what that name does on kiro-cli
#: AND that KAS registers on a standalone ACP session
#: (``acp-workspace-connection.ts`` built-ins + ``createAcpFsTools``): kiro-cli's
#: ``fs_read`` reads files and lists directories; its ``fs_write`` creates,
#: replaces, inserts and appends. The family is bounded by what the kiro-cli
#: tool CAN DO, not by KAS's capability table: KAS files ``delete_file`` under
#: ``fs_write`` too, but kiro-cli's ``fs_write`` cannot delete, so a spec that
#: mounts it never granted deletion -- and under a trust mode that skips the
#: prompt, a mounted tool runs. ``delete_file`` is mounted only when a spec
#: names it. Mounting is still not approval: the whole filesystem family is
#: refused an auto-approve rule by
#: ``kiro_crew.acp.kas_permissions.WITHHELD_FROM_AUTO_APPROVE``, so every member
#: here reaches Crew's PreToolUse gate and its sensitive-path floor, whichever
#: engine named it.
#:
#: Measured against kiro-agent (KAS) as recorded in
#: ``test/fixtures/kas_builtin_tool_ids.json`` (engine commit, kiro-cli version,
#: and the two id spaces the engine has); ``test/test_tool_names.py`` pins both
#: tables to that recording, so an engine rename fails a test before it fails
#: permissive at the gate. Re-measure per engine bump: the fixture's note says how.
#:
#: Every kiro-cli release the tables have been verified against (the fixture's
#: ``verified_kiro_cli_versions``, ascending, its first entry the release the id
#: tables were first read on -- the fixture's ``kiro_cli_version``; a test pins
#: the two equal). This is
#: what ``kirocrew doctor`` and the KAS session start compare the installed
#: kiro-cli against: a release on the list is verified, any other warns, since
#: it ships a KAS that may rename, add or lack a built-in and the policy
#: direction fails permissive on an unknown id. Re-measuring per release and
#: appending here is a step of the engine-bump checklist in the fixture's note,
#: so an exact pin does not warn on every routine release until someone notices.
KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI: tuple[tuple[int, int, int], ...] = (
    (2, 24, 0),
    (2, 24, 1),
    (2, 27, 1),
)


def kas_vocabulary_drift(version: tuple[int, int, int] | None) -> str:
    """How an installed kiro-cli stands against the measured vocabulary release.

    ``""`` when *version* is one of :data:`KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI`;
    ``"older"`` below the earliest verified release, ``"newer"`` above the
    latest, ``"unverified"`` for a release between two verified ones that was
    itself never measured; ``"unknown"`` when no version could be established.
    ONE comparison shared by ``kirocrew doctor`` (its advisory row) and the KAS
    session start (a once-per-process warning), so the two surfaces cannot
    disagree on what counts as drift. An unverified release ships a KAS that
    may rename, add or lack a built-in; the mount direction then fails silent
    and the policy direction fails permissive, which is why drift is worth
    saying where an operator will see it without running a command.
    """
    if version is None:
        return "unknown"
    if version in KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI:
        return ""
    if version < KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[0]:
        return "older"
    if version > KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[-1]:
        return "newer"
    return "unverified"


def kas_verified_releases() -> str:
    """The verified releases as one comma-separated string, for a doctor row or a
    log line -- ``"2.24.0, 2.24.1, 2.27.1"``."""
    return ", ".join(".".join(str(p) for p in v) for v in KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI)


#: ``web_search`` is the one family member KAS does not register as a local
#: built-in: the engine serves web search through its remote tool wrapper, which
#: prefixes the tool's name (``idPrefix = "remote_"``), so the id an allowlist must
#: spell is ``remote_web_search`` (the fixture's ``remote`` list). Without the row
#: a spec's ``web_search`` matched no KAS id and the tool was never mounted.
#:
#: ``use_subagent`` is kiro-cli's sub-agent tool; KAS registers the same tool as
#: ``invoke_sub_agent`` (capability ``subagent``). Without the row a spec that
#: lists ``use_subagent`` got no sub-agent tool on KAS, so it could not delegate
#: to the agents its ``toolsSettings.subagent.availableAgents`` names.
#:
#: ``execute_bash`` is a KAS id too, but KAS splits kiro-cli's shell in two: the
#: command runner and a separate tool that starts, reads and stops a long-running
#: command (``control_bash_process``, or ``control_pwsh_process`` on a Windows
#: shell; the fixture's ``process_control`` list). Without the row a spec that
#: names ``execute_bash`` -- every Bot worker -- could run a command on KAS but
#: never keep a dev server up while it works. Both ids are listed because the
#: engine picks one by the session's shell; the gate reads both as the shell.
KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME: dict[str, tuple[str, ...]] = {
    "execute_bash": ("control_bash_process", "control_pwsh_process"),
    "fs_read": ("read_file", "list_directory"),
    "glob": ("file_search",),
    "grep": ("grep_search",),
    "fs_write": ("str_replace", "fs_append"),
    "web_search": ("remote_web_search",),
    "use_subagent": ("invoke_sub_agent",),
}

#: KAS built-in tool id -> the kiro-cli name a policy about it is written under.
#:
#: The inverse of the mount table, plus two ids no mount family carries:
#:
#: * ``delete_file`` -- KAS classifies it as the ``fs_write`` capability and an
#:   operator who denied ``fs_write`` denied writes. That alias may ONLY deny
#:   (:data:`POLICY_ALIAS_DENY_ONLY_IDS`): the kiro-cli ``fs_write`` tool cannot
#:   delete, so an allow-mode rule naming ``fs_write`` grants the mount family
#:   and nothing more -- exactly what :data:`KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME`
#:   says -- and a deletion is admitted only by a rule naming ``delete_file``.
#: * ``run_command`` -- KAS's shell tool, the id its permission request stamps
#:   (``_meta.kiro.toolId``, recorded live in
#:   ``test/fixtures/acp_frames/kas/session.jsonl``). It needs no mount entry
#:   (``execute_bash`` is a KAS id too and mounts on both engines) but it does
#:   need a policy entry, or ``auto_deny_tools: ["execute_bash"]`` binds for
#:   reads and writes on KAS and not for the shell.
#:
#: Names the two engines share (``fs_write``, ``execute_bash``, ``web_fetch``,
#: ``code``) need no entry: they already ARE the policy spelling.
#:
#: A ``remote_<name>`` key here also exempts that id from the KAS identity
#: disagreement refusal when KAS's remote tool wrapper serves it
#: (``permission_requests._is_kas_remote_builtin``), so adding one widens
#: that check too.
KIRO_CLI_NAME_BY_KAS_TOOL: dict[str, str] = {
    # First, so the derived ``execute_bash`` row (``kas_tool_rows``) still leads
    # with the shell's own id: a PostToolUse call that stated no id falls back to
    # that row's first entry (``spec_hooks``), and it must stay ``run_command``.
    "run_command": "execute_bash",
    **{
        kas_id: kiro_cli_name
        for kiro_cli_name, family in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.items()
        for kas_id in family
    },
    "delete_file": "fs_write",
}

#: KAS ids whose policy alias may DENY the call but never ADMIT it.
#:
#: Every other alias names the SAME capability under another spelling
#: (``str_replace`` is one verb of ``fs_write``; ``run_command`` is the shell
#: ``execute_bash`` registers as), so an allow-mode rule written under the
#: kiro-cli name is the operator granting that very tool. ``delete_file`` is
#: not: kiro-cli's ``fs_write`` has no delete verb, which is why the mount table
#: leaves it out of the ``fs_write`` family, and the policy reading must draw
#: the same line -- an allow-mode ``tools: ["fs_write"]`` written for kiro-cli
#: must not become a deletion grant on KAS. A deny under ``fs_write`` still
#: reaches it, because an operator who denied writes denied deletes.
#:
#: The process-control tools are deny-only for a different reason: they are
#: mounted beside ``execute_bash``, but their ``start`` runs a command the
#: shell's command checks (``allowedCommands``, the recovered-command row) do not
#: read. So a deny under ``execute_bash`` reaches them, and an ``execute_bash``
#: grant leaves them at the prompt, as it did before they were mounted.
POLICY_ALIAS_DENY_ONLY_IDS: frozenset[str] = frozenset(
    {"delete_file", "control_bash_process", "control_pwsh_process"}
)

#: kiro-cli's short spelling of a built-in -> the name a spec or a rule is written in.
#:
#: kiro-cli's legacy ``shell`` spec key, which is also the ``_meta.kiro.toolName``
#: it stamps on its shell built-in (recorded live in
#: ``test/fixtures/acp_frames/kiro/session.jsonl``), and the short ``read`` /
#: ``write`` names KAS's own tool table classifies as ``fs_read`` / ``fs_write``.
#: A value is only ever a name ``governance.BUILTIN_TOOL_SCOPES`` already governs:
#: the deny rules, governance and the read-only proof all resolve a stamp through
#: this table (``hooks._host_builtin_alias``), and the proof's own allowlist then
#: decides what a resolved name may prove.
KIRO_TOOL_ALIASES: dict[str, str] = {
    "shell": "execute_bash",
    "read": "fs_read",
    "write": "fs_write",
}


#: kiro-cli's other names for a built-in -> the name Crew's tables are keyed on.
#: kiro-cli parses each of these as the same built-in in ``tools``,
#: ``allowedTools`` and ``excludedTools`` (``BuiltInToolName``'s strum
#: spellings, ``crates/agent/src/agent/tools/mod.rs``); read as written, KAS would
#: mount, grant and exclude nothing for them. Folded before the governance ceiling
#: is asked, so the ceiling judges the one Crew name and an alias cannot carry a
#: grant past it. ``shell``, ``read`` and ``write`` are not here: KAS reads them
#: itself, as tool tags, and folding ``read`` to ``fs_read`` would mount less.
KIRO_CLI_TOOL_SPELLINGS: dict[str, str] = {
    "subagent": "use_subagent",
    "agent_crew": "use_subagent",
    "fsRead": "fs_read",
    "fsWrite": "fs_write",
    "executeCmd": "execute_bash",
    "execute_cmd": "execute_bash",
}

#: The names kiro-cli accepts after ``@builtin/``: each built-in's runtime
#: aliases (``BuiltInToolTrait::aliases``), the only ``@builtin/<name>`` forms its
#: permission check trusts. Limited to built-ins Crew can place on KAS.
_BUILTIN_REF_NAMES: frozenset[str] = frozenset(
    {
        "fs_read",
        "read",
        "fs_write",
        "write",
        "execute_bash",
        "execute_cmd",
        "shell",
        "grep",
        "glob",
        "code",
        "web_fetch",
        "web_search",
        "agent_crew",
    }
)

_BUILTIN_REF_PREFIX = "@builtin/"

#: What ``@builtin`` (and ``*``) grants in an ``allowedTools`` list: every
#: kiro-cli built-in some Crew reader can place on KAS, under Crew's name for it.
#: Each still meets the governance ceiling one by one, and the shell and
#: filesystem ones are answered by Crew's gate, never by a KAS allow rule.
ALLOWED_TOOLS_BUILTINS: tuple[str, ...] = (
    "fs_read",
    "fs_write",
    "execute_bash",
    "grep",
    "glob",
    "code",
    "web_fetch",
    "web_search",
    "use_subagent",
)

#: kiro-cli's ``allowedTools`` forms for "every built-in" (``permissions.rs``
#: ``evaluate_tool_permission``).
_ALL_BUILTINS_REFS: frozenset[str] = frozenset({"@builtin", "@builtin/", "@builtin/*"})


def canonical_kiro_cli_tool_name(entry: str) -> str:
    """*entry* under the name Crew's tables use (:data:`KIRO_CLI_TOOL_SPELLINGS`).

    ``@builtin/<name>`` is read as the built-in ``<name>`` names when ``<name>`` is
    one kiro-cli accepts there (:data:`_BUILTIN_REF_NAMES`), short spellings
    included (``@builtin/write`` is ``fs_write``, never KAS's wider ``write`` tag);
    any other ``@builtin/`` entry is returned as written.
    """
    if entry.startswith(_BUILTIN_REF_PREFIX):
        name = entry[len(_BUILTIN_REF_PREFIX) :]
        if name in _BUILTIN_REF_NAMES:
            entry = KIRO_TOOL_ALIASES.get(name, name)
    return KIRO_CLI_TOOL_SPELLINGS.get(entry, entry)


def expand_kiro_cli_allowed_tools(entries: list[Any], mcp_servers: Any) -> list[str]:
    """An ``allowedTools`` list under Crew's names, with kiro-cli's wide forms spelled out.

    ``@builtin`` / ``@builtin/`` / ``@builtin/*`` become :data:`ALLOWED_TOOLS_BUILTINS`;
    ``*`` becomes those plus ``@<server>`` for each server in *mcp_servers* (the
    spec's own ``mcpServers``). Every other entry is folded by
    :func:`canonical_kiro_cli_tool_name`. Non-string and blank entries are dropped;
    order-preserving and duplicate-free.

    shortcut: ``*`` covers only the spec's own servers, not ones KAS adds later
    (``mcp.json`` under ``includeMcpJson``); those keep prompting. Extend when a
    projection-time list of every mounted server exists.
    """
    servers = (
        [s for s in mcp_servers if isinstance(s, str) and s]
        if isinstance(mcp_servers, dict)
        else []
    )
    out: list[str] = []
    for raw in entries:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = raw.strip()
        if entry == "*":
            names: list[str] = [*ALLOWED_TOOLS_BUILTINS, *(f"@{s}" for s in servers)]
        elif entry in _ALL_BUILTINS_REFS:
            names = list(ALLOWED_TOOLS_BUILTINS)
        else:
            names = [canonical_kiro_cli_tool_name(entry)]
        out.extend(n for n in names if n not in out)
    return out


def expand_kiro_cli_tool_names(entries: list[str]) -> list[str]:
    """*entries* with each kiro-cli built-in followed by its KAS family members.

    The MOUNT reading: bounded by what the kiro-cli tool can do, so ``fs_write``
    brings ``str_replace`` and ``fs_append`` and never ``delete_file``.
    Order-preserving and duplicate-free; entries the table does not name (MCP
    ``@refs``, KAS-native ids, names shared by both engines) pass through as
    written. Pure, so both list projections and their tests share one reading.
    """
    return _expand(entries, KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME)


def expand_kiro_cli_tool_exclusions(entries: list[str]) -> list[str]:
    """*entries* with each kiro-cli built-in followed by every KAS id governed
    under that name -- the EXCLUSION reading.

    Exclusion has the opposite fail-safe polarity from mounting. A mount family
    stops at the kiro-cli tool's own verbs so a spec grants nothing it did not
    ask for; an exclusion must reach everything a rule under that name governs,
    or a spec that excludes ``fs_write`` on kiro-cli leaves ``delete_file``
    mounted on KAS. So this reading is the inverse of
    :data:`KIRO_CLI_NAME_BY_KAS_TOOL` restricted to REGISTERED ids -- the mount
    family plus ``delete_file`` under ``fs_write``, the same widening the gate
    applies -- and omits permission-frame-only ids (``run_command``), which the
    engine's exclusion matcher cannot see; their registered owner
    (``execute_bash``) is the entry that withholds them.
    """
    return _expand(entries, _EXCLUSION_FAMILY_BY_KIRO_CLI_NAME)


def _expand(entries: list[str], table: dict[str, tuple[str, ...]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        entry = canonical_kiro_cli_tool_name(raw)
        for name in (entry, *table.get(entry, ())):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


#: The exclusion family: every REGISTERED KAS id :data:`KIRO_CLI_NAME_BY_KAS_TOOL`
#: files under each kiro-cli name, keyed over every policy owner (so
#: ``execute_bash`` has an entry too), in policy-table order with ``delete_file``
#: last. Permission-frame-only ids are omitted on purpose: ``run_command`` is
#: what the shell REPORTS on ``request_permission``, not what it registers as,
#: and the engine's ``excludedTools`` matcher compares against registered ids
#: (``tools/tool-filter.ts``), so emitting it would exclude nothing --
#: ``execute_bash`` in the entry already withholds the shell.
_PERMISSION_FRAME_ONLY_IDS: frozenset[str] = frozenset({"run_command"})
_EXCLUSION_FAMILY_BY_KIRO_CLI_NAME: dict[str, tuple[str, ...]] = {
    kiro_cli_name: tuple(
        kas_id
        for kas_id, owner in KIRO_CLI_NAME_BY_KAS_TOOL.items()
        if owner == kiro_cli_name and kas_id not in _PERMISSION_FRAME_ONLY_IDS
    )
    for kiro_cli_name in dict.fromkeys(KIRO_CLI_NAME_BY_KAS_TOOL.values())
}


#: DeepSeek Harness read-only built-in -> ``(kiro-cli policy name, target parameter)``.
#:
#: dsh's ACP bridge states the dispatched tool's own name as a ``tool_call`` frame's
#: ``title`` and names nothing else about the call; every MCP tool it serves is
#: ``mcp__<server>__<tool>``, so a bare name here is its own built-in (recorded live in
#: ``test/fixtures/acp_frames/deepseek/side-read-only-live.jsonl``). The policy name is
#: what a rule about the tool is written under. The target parameter is the argument
#: naming what the tool reads. dsh resolves a missing (``grep``, ``glob``) or
#: relative one against the session's work dir, which the gate cannot see, so only
#: an absolute target is proved. The owner is the stock dsh package that registers
#: the tool; the name proves the tool only on a session whose launch showed that
#: package owns it (``agent_sdk.tool_gate.deepseek_read_only_owner_issue``).
DEEPSEEK_READ_ONLY_TOOLS: dict[str, tuple[str, str, str]] = {
    "read": ("fs_read", "file_path", "@deepseek-ai/dsh-tool-fs"),
    "grep": ("grep", "path", "@deepseek-ai/dsh-tool-fs-search"),
    "glob": ("glob", "path", "@deepseek-ai/dsh-tool-fs-search"),
}


def deepseek_policy_alias(tool_name: str) -> str:
    """The kiro-cli policy name of a DeepSeek Harness read built-in, else ``""``."""
    entry = DEEPSEEK_READ_ONLY_TOOLS.get(tool_name)
    return entry[0] if entry else ""


#: pi's read-only built-ins, by the bare name pi's gate extension states, -> the
#: kiro-cli built-in that does the same job (``ls`` lists a directory, as
#: ``fs_read`` does). For the pi Side Chat seam to map from.
PI_READ_ONLY_BUILTINS: dict[str, str] = {
    "read": "fs_read",
    "grep": "grep",
    "find": "glob",
    "ls": "fs_read",
}

#: Every harness whose permission TITLE is its bare tool name -> that name's
#: kiro-cli name, so governance asks the title and ``harness_builtin_tool`` as one
#: tool. claude is not here: its permission title carries the arguments
#: (``Read .env``), so it never equals a bare name. Only consulted once a seam
#: has already stated ``harness_builtin_tool``; it proves nothing alone.
HARNESS_READ_POLICY_NAMES: dict[str, str] = {**PI_READ_ONLY_BUILTINS}


def is_harness_read_alias(native: str, builtin: str) -> bool:
    """True when *native* is a harness's own name for the kiro-cli *builtin*."""
    return bool(builtin) and HARNESS_READ_POLICY_NAMES.get(native) == builtin


def policy_aliases(*identities: str) -> tuple[str, ...]:
    """The kiro-cli policy names for whichever of *identities* are KAS tool ids.

    Empty for anything the table does not name, so a caller can append the
    result to its deny / governance targets unconditionally. Duplicate-free and
    never echoes an input: the caller already evaluates those.
    """
    out: list[str] = []
    for identity in identities:
        alias = KIRO_CLI_NAME_BY_KAS_TOOL.get(identity)
        if alias and alias not in out and alias not in identities:
            out.append(alias)
    return tuple(out)


def policy_alias_split(identity: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """*identity*'s policy aliases as ``(may_admit, deny_only)``.

    ``may_admit`` are spellings of the same capability, asked as ONE identity
    with the raw id (a permitted spelling admits the call). ``deny_only`` are
    spellings whose rules may refuse the call but never grant it
    (:data:`POLICY_ALIAS_DENY_ONLY_IDS`). Both empty for an id the table does
    not name. Exactly one side carries each alias.
    """
    aliases = policy_aliases(identity)
    if identity in POLICY_ALIAS_DENY_ONLY_IDS:
        return (), aliases
    return aliases, ()
