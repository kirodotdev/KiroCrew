"""The crewmate worker: a source-defined base plus the crewmate's own additions.

A crewmate is two source-defined agents and one file it owns
(``rfc-mate-owned-agent-config`` §5.1, §5.4). Its chat runs on the conductor,
``kirocrew-mate-conductor``; work the conductor hands off runs in a worker
session on ``kirocrew-mate-worker``. Both specs are written from Crew's source
on every gateway start (``agent_materialization.service_agents``) and neither is
user-editable. The crewmate's file, ``~/.kiro/mates/agents/<id>.json``, extends
the WORKER only, and only additively:

* ``prompt`` is appended after the worker's own prompt, never in its place;
* ``mcpServers`` the base does not declare are added (a base server of the same
  name always wins, so a file cannot redefine a platform server);
* skills (``resources`` and ``skills`` entries) the base does not carry are added;
* ``allowedTools`` entries are taken only for the servers the file adds, and the
  ``tools`` list mounts each added server.

Restrictions narrow, whoever declared them: the file's ``excludedTools`` and
every ``disabledTools`` entry it names join the worker's ``excludedTools``, and
so does every tool the user switched off on the dashboard's MCP page for a
server the worker mounts (:func:`globally_disabled_refs`); an auto-approval
naming an excluded tool is dropped. Everything else in the file (``tools`` for
built-ins, ``model``, ``permissions``, ``hooks``) is ignored here. The merged spec then goes through
the governance sanitizer, and the projection re-applies the ceiling on its own
read (``kas_agents._ceiling_permitted``), so an addition never lifts the worker
above POLICY ∩ PROFILE. Crew's PreToolUse gate still evaluates every call.

Pure functions over parsed specs plus one resolver; nothing here holds state, so
the MCP-statelessness rule has nothing to guard.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kiro_crew import crewmate_prompts
from kiro_crew.agent_files import (
    MATE_CONDUCTOR_AGENT_FILENAME,
    MATE_WORKER_AGENT_FILENAME,
    is_mate_service_agent,
)
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: The agent a crewmate's worker sessions run on.
MATE_WORKER_AGENT = Path(MATE_WORKER_AGENT_FILENAME).stem
#: The agent a crewmate's own chat runs on.
MATE_CONDUCTOR_AGENT = Path(MATE_CONDUCTOR_AGENT_FILENAME).stem

#: The first line a worker's report opens with when the task needs a capability
#: it does not have; the crewmate's thread is told so on the report
#: (``mate_thread_wakes.report_prompt``).
MISSING_CAPABILITY_PREFIX = "MISSING CAPABILITY:"

#: The worker's own principles, appended to its source-defined prompt
#: (``service_agents._install_mate_worker_agent``). Its crewmate is the one that
#: can ask the user for a tool, so a gap goes back to it, named.
MATE_WORKER_PROMPT = crewmate_prompts.fill(
    "worker.principles", missing_prefix=MISSING_CAPABILITY_PREFIX
)

#: The same principles while a crewmate's Dynamic capabilities preview is off
#: under Crewmate 2.0: the worker still names what it could not do, but is asked
#: for no ``MISSING CAPABILITY:`` line, because no card follows from one.
MATE_WORKER_PROMPT_MANUAL = crewmate_prompts.text("worker.principles_manual")


def mate_worker_prompt(member: str | None = None) -> str:
    """The worker principles for crewmate *member* (:mod:`kiro_crew.crewmate_dynamic_capabilities`).

    The installed base takes the no-crewmate answer (off: the manual version);
    :func:`resolve_worker_spec` puts each crewmate's own in its worker's prompt.
    """
    from kiro_crew import crewmate_dynamic_capabilities

    if crewmate_dynamic_capabilities.restricted_for(member):
        return MATE_WORKER_PROMPT_MANUAL
    return MATE_WORKER_PROMPT


def _with_principles(spec: dict[str, Any], member_id: str | None) -> dict[str, Any]:
    """*spec* with the principles in its prompt swapped for crewmate *member_id*'s own."""
    prompt = spec.get("prompt")
    if not isinstance(prompt, str):
        return spec
    from kiro_crew import crewmate_dynamic_capabilities

    want = mate_worker_prompt(crewmate_dynamic_capabilities.member_name(member_id))
    for have in (MATE_WORKER_PROMPT, MATE_WORKER_PROMPT_MANUAL):
        if have != want and have in prompt:
            return {**spec, "prompt": prompt.replace(have, want)}
    return spec


#: The sub-agent tools a crewmate's conductor does not mount. Its hand-off is a
#: worker session (``session_create`` / ``session_send`` /
#: ``session_read_message``), so ``spawn_run`` and the tools that only act on a
#: run it started are taken out of the conductor's tool list
#: (``excludedTools``, which both engines apply), and the spawn gate refuses a
#: spawn whose parent runs on the conductor
#: (``subagent_parts.agent_policy.conductor_spawn_refusal``).
MATE_CONDUCTOR_EXCLUDED_TOOLS = tuple(
    f"@kirocrew-core/{tool}"
    for tool in (
        "spawn_run",
        "spawn_sub_agents",
        "spawn_continue",
        "spawn_steer",
        "spawn_status",
        "spawn_release",
        "spawn_list",
        # These start work of their own; on the conductor that work goes to
        # the worker instead.
        "task_run",
        "workflow_run",
    )
)


def is_conductor_execution(execution: Any) -> bool:
    """Whether *execution* (an attested ``ExecutionContext``) is a crewmate's conductor chat.

    Positive identity: a ``member`` selection running the conductor template. A
    worker the crewmate opened is a ``template`` selection on the worker, so it
    answers False, as does a missing record.
    """
    return (
        execution is not None
        and getattr(execution, "selection_kind", "") == "member"
        and member_chat_template(getattr(execution, "template_id", "")) == MATE_CONDUCTOR_AGENT
    )


def member_chat_template(template_id: str) -> str:
    """The agent a ``member`` record's *template_id* chats on.

    The rule ``resolve_session_agent_bindings`` applies to every member turn: a
    record naming the crewmate's own file chats on the conductor. A record
    written while the dispatch snapshot did not know that file yet names the
    file itself; read through here it is still the conductor, so the chat, the
    hand-off and the spawn gate agree. Any other template is returned as named,
    and so is every template while Crewmate 2.0 is off (:mod:`kiro_crew.crewmate_v2`).
    """
    from kiro_crew import crewmate_v2

    if not crewmate_v2.enabled():
        return template_id
    if not template_id or template_id == MATE_CONDUCTOR_AGENT:
        return template_id
    return MATE_CONDUCTOR_AGENT if is_mate_owned_agent(template_id) else template_id


def is_legacy_binding(name: str, binding: Any) -> bool:
    """Whether ``config.agents[name]`` is a legacy built-in binding, not a crewmate.

    Positive signature: a ``builtin`` binding that never got a member identity
    (an older install's ``assistant``). The roster surfaces do not list it as a
    crewmate. The reserved ``default`` member is excluded by name, and a
    user-created crewmate on shared memory (``source`` ``kirocrew``, no id yet)
    stays listed.
    """
    if name == "default" or getattr(binding, "source", "") != "builtin":
        return False
    return not str(getattr(binding, "member_id", "") or "").strip()


def conductor_mcp_servers() -> frozenset[str]:
    """The MCP servers a crewmate's conductor may mount: the gateway's own.

    The conductor's tool set is fixed by the gateway, whatever servers the user
    has: these are the platform servers Crew itself launches
    (``agent._MANAGED_MCP_SERVERS``). Every other server, the user's included,
    reaches the crewmate's worker only, through its own file.
    """
    from kiro_crew import agent as agent_mod

    return frozenset(agent_mod._MANAGED_MCP_SERVERS)


#: The built-in tools a crewmate's conductor does not mount: the shell, the
#: general file writer, and ``code`` (governance files it under
#: ``filesystem.write`` because it writes files and can shell out). The
#: conductor answers, reads, plans and reviews; anything that changes a file or
#: runs a command is a worker's. Its own writes go through MCP tools (the
#: ledger, the dashboard page, ``save_notes``, the goal tools). Taken out of
#: ``tools`` and ``allowedTools`` and named in ``excludedTools``, which both
#: engines subtract (KAS expands ``fs_write`` to its whole edit family).
MATE_CONDUCTOR_WITHHELD_BUILTINS = ("execute_bash", "fs_write", "code")

#: The conductor-only ``kirocrew-mate`` tools the worker does not mount: the
#: worker does the work, and a capability it lacks is reported back to the
#: conductor, which alone reads the catalog and asks the user for an addition.
MATE_WORKER_EXCLUDED_TOOLS = (
    "@kirocrew-mate/request_capabilities",
    "@kirocrew-mate/find_capabilities",
)

#: The parts of a crewmate's file that extend the worker. A template copied in as
#: the worker's extra kit (``mate_worker_kit``) copies exactly these.
ADDITION_KEYS = ("prompt", "mcpServers", "resources", "skills", "allowedTools")


def is_mate_owned_agent(kiro_agent: str) -> bool:
    """Whether *kiro_agent* names a crewmate-owned file and nothing at the user level.

    A lookup on the snapshot :func:`kiro_crew.config.loader.refresh_materialized_agents`
    publishes; the crewmate writers add their file to it as they write it
    (``mate_config.write_mate_spec``). A name the snapshot does not know at
    all is rescanned first when called off the loop; on the loop a rescan is
    scheduled and the answer is False for that call, so the binding runs as named.
    """
    import asyncio

    from kiro_crew.config import loader

    if not kiro_agent:
        return False
    if loader._MATERIALIZED_AGENTS_READY and kiro_agent in loader._MATERIALIZED_MATE_ONLY:
        return True
    known = loader._MATERIALIZED_AGENTS_READY and kiro_agent in loader._MATERIALIZED_AGENTS
    if not known:
        # Not in the snapshot at all: a file another process wrote (the command
        # line's create) or one written before any scan. Off the loop it is
        # rescanned here; on the loop a rescan is scheduled and this one call
        # answers from the snapshot as it is.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loader.refresh_materialized_agents(heal_default=False)
        else:
            loader.schedule_materialized_agents_refresh()
            return False
    return kiro_agent in loader._MATERIALIZED_MATE_ONLY


def member_chat_agent(kiro_agent: str) -> str:
    """The agent a crewmate's own chat runs on, for its bound ``kiro_agent``.

    The conductor when the binding owns a crewmate file
    (:func:`is_mate_owned_agent`), else the bound name through
    ``dispatch_kiro_agent``. The crewmate file never reaches the chat. With
    Crewmate 2.0 off (:mod:`kiro_crew.crewmate_v2`) it is always the bound name.
    """
    from kiro_crew import crewmate_v2
    from kiro_crew.config.loader import dispatch_kiro_agent

    if crewmate_v2.enabled() and is_mate_owned_agent(kiro_agent):
        return MATE_CONDUCTOR_AGENT
    return dispatch_kiro_agent(kiro_agent)


#: Keys a platform-server entry may carry and still say nothing of its own: an
#: on/off switch with no transport.
_BARE_OVERRIDE_KEYS = frozenset({"disabled"})


def drop_partial_platform_servers(spec: Any) -> list[str]:
    """Remove from *spec* each platform-server entry that is only an on/off switch.

    Platform servers (:func:`conductor_mcp_servers`) come from the gateway, whatever
    a template carries. An entry such as ``"kirocrew-mate": {"disabled": false}``
    names one with no ``command`` or ``url``; a session that ran the file would wait
    for a server it cannot launch. An entry that carries anything else (a
    transport, ``disabledTools``) is left as it is, and so is a switch-OFF
    (``{"disabled": true}``): it launches nothing, and it is the crewmate's
    restriction on the base's server (:func:`disabled_base_servers`). Edits *spec*
    in place and returns the names it removed.
    """
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    if not isinstance(servers, dict):
        return []
    platform = conductor_mcp_servers()
    dropped = [
        name
        for name, entry in servers.items()
        if name in platform
        and isinstance(entry, dict)
        and set(entry) <= _BARE_OVERRIDE_KEYS
        and not mcp_entry_is_muted(entry)
    ]
    for name in dropped:
        del servers[name]
    return dropped


def _server_of(ref: str) -> str:
    """The server a ``@server`` / ``@server/tool`` reference names, else ``""``."""
    if not ref.startswith("@"):
        return ""
    return ref[1:].split("/", 1)[0]


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _servers(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}


def disabled_tool_refs(servers: Any) -> list[str]:
    """``@server/tool`` for every tool an ``mcpServers`` entry's ``disabledTools`` turns off.

    The KAS customAgents wire drops ``disabledTools`` from a server entry
    (``kas_agents._KAS_DISCARDED_ENTRY_KEYS``), so the restriction is re-expressed
    in a field the wire carries and the engine applies: ``excludedTools``, which
    takes the tool out of the session's tool list. Only string tool names count.
    """
    refs: list[str] = []
    for name, entry in _servers(servers).items():
        for tool in _strings(entry.get("disabledTools")):
            ref = f"@{name}/{tool}"
            if tool.strip() and ref not in refs:
                refs.append(ref)
    return refs


def worker_additions(base: dict[str, Any], mate_spec: dict[str, Any]) -> dict[str, Any]:
    """What *mate_spec* adds on top of the worker *base*.

    Anything the base already carries is not an addition, so a file holding a
    full copy of a template that matches the base (the default template) adds
    nothing. The prompt's addition is what it carries beyond the base's own
    prompt text: a prompt that opens with the base's text adds only the rest.
    """
    base_servers = _servers(base.get("mcpServers"))
    added_servers = {
        name: copy_json_document(entry)
        for name, entry in _servers(mate_spec.get("mcpServers")).items()
        if name not in base_servers
    }
    base_prompt = base.get("prompt") if isinstance(base.get("prompt"), str) else ""
    raw_prompt = mate_spec.get("prompt")
    prompt = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
    own = (base_prompt or "").strip()
    if own and prompt and own.startswith(prompt):
        # The base opens with this whole prompt (the worker base is the default
        # template's prompt plus the worker's own principles), so it adds nothing.
        prompt = ""
    elif own and prompt.startswith(own):
        # A template built on the default one opens with the base's own text;
        # only what follows it is an addition, or the worker would read it twice.
        prompt = prompt[len(own) :].strip()
    additions: dict[str, Any] = {"prompt": prompt, "mcpServers": added_servers}
    for key in ("resources", "skills"):
        have = set(_strings(base.get(key)))
        additions[key] = [s for s in dict.fromkeys(_strings(mate_spec.get(key))) if s not in have]
    additions["allowedTools"] = [
        ref
        for ref in dict.fromkeys(_strings(mate_spec.get("allowedTools")))
        if _server_of(ref) in added_servers
    ]
    # A restriction, not an addition: the file's per-tool limits count for EVERY
    # server it names, a base server included, because the server entry the base
    # wins with must not carry the file's narrowing away.
    additions["disabledTools"] = disabled_tool_refs(mate_spec.get("mcpServers"))
    # The file's own ``excludedTools`` is a restriction too, kept whole: a tool it
    # takes out stays out of the worker, whoever declared it.
    additions["excludedTools"] = list(dict.fromkeys(_strings(mate_spec.get("excludedTools"))))
    # A whole-server switch-off on a server the base declares is a restriction as
    # well. Only the NAME is carried: the base's entry keeps its transport, and the
    # merge mutes it. (A server the file adds keeps its own ``disabled`` above.)
    additions["disabledServers"] = disabled_base_servers(base, mate_spec)
    return additions


def disabled_base_servers(base: dict[str, Any], mate_spec: dict[str, Any]) -> list[str]:
    """The base servers *mate_spec* switches off whole (``disabled``, read fail-closed).

    Read with :func:`kiro_crew.mcp_cleanup.mcp_entry_is_muted`, the launch
    decision's own reading, so the merge mutes exactly what a launch would skip.
    """
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    base_servers = _servers(base.get("mcpServers"))
    return [
        name
        for name, entry in _servers(mate_spec.get("mcpServers")).items()
        if name in base_servers and mcp_entry_is_muted(entry)
    ]


def has_additions(additions: dict[str, Any]) -> bool:
    """Whether *additions* would change the worker at all."""
    return any(additions.get(key) for key in ADDITION_KEYS)


def merge_worker_spec(base: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    """The worker spec: *base* with *additions* layered on, then governed.

    *base* must carry its prompt as literal text (the caller resolves a
    ``file://`` prompt first); the result's prompt is literal too. The base is
    never mutated.
    """
    spec = copy_json_document(base)
    spec["name"] = MATE_WORKER_AGENT
    extra = additions.get("prompt") or ""
    if extra:
        own = spec.get("prompt") if isinstance(spec.get("prompt"), str) else ""
        spec["prompt"] = f"{own.rstrip()}\n\n{extra}" if own else extra
    servers = _servers(spec.get("mcpServers"))
    added = _servers(additions.get("mcpServers"))
    for name, entry in added.items():
        servers.setdefault(name, copy_json_document(entry))
    # The file's switch-off of a base server mutes the base's own entry; the
    # transport is never taken from the file. The KAS projection does not declare
    # a muted entry (``kas_agents``), so the worker does not launch it.
    muted = [name for name in _strings(additions.get("disabledServers")) if name in servers]
    for name in muted:
        servers[name] = {**servers[name], "disabled": True}
    spec["mcpServers"] = servers
    for key in ("resources", "skills"):
        extra_items = _strings(additions.get(key))
        if extra_items:
            spec[key] = list(dict.fromkeys([*_strings(spec.get(key)), *extra_items]))
    tools = _strings(spec.get("tools"))
    for name in added:
        if f"@{name}" not in tools and "*" not in tools:
            tools.append(f"@{name}")
    spec["tools"] = tools
    allowed = _strings(spec.get("allowedTools"))
    for ref in _strings(additions.get("allowedTools")):
        if _server_of(ref) in added and ref not in allowed:
            allowed.append(ref)
    # A muted server's approvals grant nothing; drop them so none outlives the mute.
    allowed = [ref for ref in allowed if _server_of(ref) not in muted]
    # Per-tool limits, from the base's own entries and the file's, are hidden from
    # the session rather than refused at its start: each disabled tool joins
    # ``excludedTools`` and loses any auto-approval naming it.
    off = list(
        dict.fromkeys([*disabled_tool_refs(servers), *_strings(additions.get("disabledTools"))])
    )
    excluded = list(
        dict.fromkeys(
            [*_strings(spec.get("excludedTools")), *_strings(additions.get("excludedTools")), *off]
        )
    )
    if excluded:
        spec["excludedTools"] = excluded
        allowed = [ref for ref in allowed if ref not in excluded]
        for ref in excluded:
            if not ref.startswith("@") or "/" not in ref:
                continue
            server, _, tool = ref[1:].partition("/")
            entry = servers.get(server)
            approve = entry.get("autoApprove") if isinstance(entry, dict) else None
            if isinstance(approve, list) and tool in approve:
                entry["autoApprove"] = [t for t in approve if t != tool]
    spec["allowedTools"] = allowed
    # The ceiling applies to the MERGED spec, not to the file alone: a grant the
    # file carries for a server it adds is withheld here exactly as it would be
    # on any other spec Crew writes.
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    sanitize_agent_config_governance(spec)
    return spec


def mate_file_for_member(member_id: str, config: Any = None) -> str:
    """The name of the crewmate file *member_id*'s binding owns, else ``""``.

    The binding's ``kiro_agent`` names the file; it counts only when the name is
    declared in the crewmate folder and NOT at the user level (a user-level spec
    of the same name is someone's custom agent, never this crewmate's additions).
    Blocking file I/O; callers run it off the event loop.
    """
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.execution_context import member_config_for_id

    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    try:
        _, binding = member_config_for_id(config, member_id)
    except Exception:  # noqa: BLE001 - an unknown or ambiguous id owns no file
        return ""
    name = getattr(binding, "kiro_agent", "") or ""
    if not name or is_mate_service_agent(name):
        # The conductor and worker specs share the crewmate folder; neither is
        # any crewmate's file.
        return ""
    if not agent_specs.agent_spec_absent(paths_mod.kiro_agents_dir(), name):
        return ""
    if agent_specs.agent_spec_absent(paths_mod.mate_agents_dir(), name):
        return ""
    return name


def globally_disabled_refs(server_names: Any) -> list[str]:
    """``@server/tool`` for each tool the user switched off on the dashboard's MCP page.

    The page's per-tool switch writes ``disabledTools`` into the user's MCP config
    (``api_mcp_toggle_tool``); kiro-cli reads that file, but the KAS wire carries
    only what the agent spec declares, so for the worker the switch is applied
    here, to every server in *server_names* the worker mounts. Read through
    :func:`kiro_crew.mcp_discovery.list_servers`, the same merge (first scope
    wins) the MCP page shows. An unreadable config adds nothing. Blocking.
    """
    names = set(server_names or ())
    if not names:
        return []
    try:
        from kiro_crew.mcp_discovery import list_servers

        rows = list_servers()
    except Exception:  # noqa: BLE001 - no readable config means no switch to honour
        logger.warning("MCP config unreadable; the worker keeps its own tool limits", exc_info=True)
        return []
    refs: list[str] = []
    for row in rows:
        if getattr(row, "name", None) not in names:
            continue
        for tool in getattr(row, "disabled_tools", None) or []:
            if isinstance(tool, str) and tool.strip():
                refs.append(f"@{row.name}/{tool}")
    return list(dict.fromkeys(refs))


def _with_global_tool_offs(base: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    """*additions* with the dashboard's per-tool switches for every mounted server."""
    names = [*_servers(base.get("mcpServers")), *_servers(additions.get("mcpServers"))]
    refs = globally_disabled_refs(names)
    if not refs:
        return additions
    out = dict(additions)
    out["disabledTools"] = list(dict.fromkeys([*_strings(additions.get("disabledTools")), *refs]))
    return out


def resolve_worker_spec(base: dict[str, Any], member_id: str | None) -> dict[str, Any]:
    """The worker spec a session of *member_id*'s worker runs on, its principles its own.

    :func:`_resolve_worker_spec` with the worker principles in the prompt set by
    the crewmate's Dynamic capabilities preview (:func:`mate_worker_prompt`).
    """
    return _with_principles(_resolve_worker_spec(base, member_id), member_id)


def _resolve_worker_spec(base: dict[str, Any], member_id: str | None) -> dict[str, Any]:
    """The worker spec a session of *member_id*'s worker runs on.

    *base* is the source-defined worker spec with its prompt already literal.
    With no member, or a member whose binding owns no crewmate file, the result
    is the base under governance; an unreadable file is logged and skipped, so a
    broken addition degrades to the base rather than to a refused session.
    """
    if not member_id:
        return merge_worker_spec(base, _with_global_tool_offs(base, {}))
    name = mate_file_for_member(member_id)
    if not name:
        return merge_worker_spec(base, _with_global_tool_offs(base, {}))
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod

    mate_dir = paths_mod.mate_agents_dir()
    try:
        mate_spec = agent_specs.load_agent_spec(mate_dir, name)
        prompt = mate_spec.get("prompt")
        # Resolved only when the file names a prompt: ``resolve_prompt`` answers
        # a prompt-less spec with the KAS fallback text, which is not an addition.
        if isinstance(prompt, str) and prompt.strip():
            mate_spec["prompt"] = agent_specs.resolve_prompt(
                mate_spec, agent_id=name, agents_dir=mate_dir
            )
        else:
            mate_spec["prompt"] = ""
    except agent_specs.AgentSpecUnreadable:
        logger.warning(
            "crewmate %r: its worker additions file %r is unreadable; the worker runs "
            "its source base only",
            member_id,
            name,
            exc_info=True,
        )
        return merge_worker_spec(base, _with_global_tool_offs(base, {}))
    additions = worker_additions(base, mate_spec)
    return merge_worker_spec(base, _with_global_tool_offs(base, additions))


def is_own_worker_execution(execution: Any) -> bool:
    """Whether *execution* is a crewmate's own worker: the worker on that crewmate's store.

    Positive identity from the attested record ``session_create`` binds at the
    child's birth: a ``template`` selection running :data:`MATE_WORKER_AGENT`
    with a member id. Every other record, and no record, answers False, and so
    does every record while Crewmate 2.0 is off (no kit, no backend route).
    """
    from kiro_crew import crewmate_v2

    return (
        crewmate_v2.enabled()
        and execution is not None
        and getattr(execution, "selection_kind", "") == "template"
        and getattr(execution, "template_id", "") == MATE_WORKER_AGENT
        and bool(getattr(execution, "member_id", None))
    )


def note_worker_session(session_key: str, execution: Any) -> bool:
    """Publish *session_key* as a crewmate's worker when its record says so.

    What makes the backend selection (``members.select_provider_backend``) route
    the worker to its crewmate's backend: the selection is an in-memory test, and
    this is the one writer, called wherever the record is read before a session
    is allocated. Returns whether the key is a worker.
    """
    from kiro_crew.members import note_crewmate_worker_session

    if not is_own_worker_execution(execution):
        return False
    note_crewmate_worker_session(session_key)
    return True


def worker_session_spec(literal_base: dict[str, Any], session_key: str) -> dict[str, Any]:
    """The spec a crewmate's worker session on *session_key* runs: base plus its kit.

    The one harness-neutral seam for the worker kit. *literal_base* is the
    source-defined worker spec with its prompt already literal (each harness
    resolves the prompt its own way); the crewmate comes from the session's own
    execution record, never from a name the session supplies, so the additions
    are always the dispatching crewmate's own. With no record, or no member on
    it, the result is the base under governance. A harness reads this through
    its projection only when it can carry a per-session spec
    (``ACP_BACKENDS_MATE_WORKER_KIT``); a worker allocated on any other backend
    is refused before spawn, never run on the bare base. Blocking file I/O.
    """
    from kiro_crew.execution_context import read_session_execution

    member_id: str | None = None
    try:
        execution = read_session_execution(session_key) if session_key else None
    except Exception:  # noqa: BLE001 - an unreadable record adds nothing
        logger.warning(
            "worker session %s: execution record unreadable; projecting the worker base",
            session_key,
            exc_info=True,
        )
        execution = None
    if execution is not None:
        member_id = execution.member_id
    return resolve_worker_spec(literal_base, member_id)


def crewmate_worker_backend(agent_cfg: Any) -> str:
    """The backend a crewmate's worker runs on: its crewmate's, through the one gate.

    ``agent.member_acp_backend`` as the loader reads it (a stored ``kiro`` already
    reads as KAS) crossed through ``resolve_selected_backend``, the same gate
    ``members.select_provider_backend`` applies to the member route.
    """
    from kiro_crew.acp_backends import resolve_selected_backend

    return resolve_selected_backend(getattr(agent_cfg, "member_acp_backend", ""))


def worker_kit_backend_refusal(backend: str) -> str | None:
    """Why a crewmate's worker cannot run on *backend*, or ``None`` when it can.

    Opt-in membership (harness-parity H6): only a backend in
    ``ACP_BACKENDS_MATE_WORKER_KIT`` carries the worker's per-session spec (its
    kit, the crewmate's additions and the tools it switches off), so a worker on
    any other backend would run the bare on-disk base with the crewmate's
    restrictions lost. The text names the backends that can carry it.
    """
    from kiro_crew.acp_backends import ACP_BACKENDS_MATE_WORKER_KIT, backend_to_stored

    if backend in ACP_BACKENDS_MATE_WORKER_KIT:
        return None
    able = ", ".join(sorted(backend_to_stored(b) for b in ACP_BACKENDS_MATE_WORKER_KIT))
    return (
        f"a crewmate's worker runs on its crewmate's backend "
        f"({backend_to_stored(backend)}), which cannot carry the worker's own tools, "
        f"servers and switched-off tools for one session; set agent.member_acp_backend "
        f"to {able}"
    )
