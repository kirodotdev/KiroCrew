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

Two readings live here, one per direction, and both are consumed:

* :data:`KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME` -- the MOUNT direction.
  ``kas_agents._project_tools`` carries each kiro-cli name with its family
  beside it, so a spec mounts on KAS what it mounts on kiro-cli.
* :data:`KIRO_CLI_NAME_BY_KAS_TOOL` -- the POLICY direction.
  ``hooks.on_tool_call`` evaluates a KAS id under its kiro-cli name as well, so
  a deny or a ``tools``-scope ceiling an operator wrote as ``fs_write`` binds to
  ``str_replace`` exactly where it binds to ``fs_write``. A mount widened in one
  vocabulary and a rule read in the other is how the operator's own control
  stops applying.

A LEAF on purpose: stdlib only, so both the ACP projection and the PreToolUse
gate can import it with no cycle (``hooks`` cannot import from ``kiro_crew.acp``
without pulling the runtime).
"""

from __future__ import annotations

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
#: The kiro-cli version the recording was made on, restated here because the
#: fixture is test data and the runtime needs the number: ``kirocrew doctor``
#: warns when the installed kiro-cli is NEWER than this, since a newer engine
#: may have renamed or added a built-in the tables do not know, and the policy
#: direction fails permissive on an unknown id. A test pins this constant to
#: the fixture so the two cannot disagree.
KAS_TOOL_IDS_MEASURED_ON_KIRO_CLI: tuple[int, int, int] = (2, 24, 0)

KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME: dict[str, tuple[str, ...]] = {
    "fs_read": ("read_file", "list_directory"),
    "glob": ("file_search",),
    "grep": ("grep_search",),
    "fs_write": ("str_replace", "fs_append"),
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
KIRO_CLI_NAME_BY_KAS_TOOL: dict[str, str] = {
    **{
        kas_id: kiro_cli_name
        for kiro_cli_name, family in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.items()
        for kas_id in family
    },
    "delete_file": "fs_write",
    "run_command": "execute_bash",
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
POLICY_ALIAS_DENY_ONLY_IDS: frozenset[str] = frozenset({"delete_file"})


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
    for entry in entries:
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
