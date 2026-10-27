"""The crewmate worker: a source-defined base plus the crewmate's own additions.

``rfc-mate-owned-agent-config`` §5.1, §5.4. A crewmate's chat runs on
``kirocrew-mate-conductor`` and its hand-offs on ``kirocrew-mate-worker``, both
written from source on every start (``agent_materialization.service_agents``).
The crewmate's file, ``~/.kiro/mates/agents/<id>.json``, extends the WORKER only,
additively: its prompt after the base's, servers and skills the base lacks (a
base server always wins), and the ``allowedTools`` and ``tools`` selectors for
those servers. Its restrictions (``excludedTools``, ``disabledTools``, MCP-page
switch-offs, :func:`globally_disabled_refs`) narrow whoever declared them;
``model``, ``permissions`` and ``hooks`` are ignored. The merged spec is governed
and the projection re-applies the ceiling, so an addition never lifts the worker
above POLICY ∩ PROFILE. Stateless.
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


class WorkerKitUnreadable(Exception):
    """A crewmate's worker cannot be projected: something holding its limits is unreadable."""


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


def is_conductor_execution(execution: Any, config: Any = None) -> bool:
    """Whether *execution* (an attested ``ExecutionContext``) is a crewmate's conductor chat.

    Positive identity: a ``member`` selection that chats on the conductor. Given
    *config*, that is :func:`member_record_template` with the member's current
    binding, the rule its chat dispatches by, so a thread whose record kept an
    older template is still its crewmate's conductor. Without *config* only the
    record's own template is read. A worker the crewmate opened is a ``template``
    selection on the worker, so it answers False, as does a missing record.
    """
    if execution is None or getattr(execution, "selection_kind", "") != "member":
        return False
    template_id = getattr(execution, "template_id", "")
    if member_chat_template(template_id) == MATE_CONDUCTOR_AGENT:
        return True
    if config is None:
        return False
    bound = bound_kiro_agent_for(config, getattr(execution, "member_id", None))
    return member_record_template(template_id, bound) == MATE_CONDUCTOR_AGENT


def bound_kiro_agent_for(config: Any, member_id: Any) -> str:
    """The ``kiro_agent`` *member_id*'s row in *config* binds, else ``""``. In memory."""
    from kiro_crew.execution_context import member_config_for_id

    if not member_id:
        return ""
    try:
        _, binding = member_config_for_id(config, member_id)
    except Exception:  # noqa: BLE001 - an unknown or ambiguous id binds nothing
        return ""
    return str(getattr(binding, "kiro_agent", "") or "")


def member_record_template(template_id: str, bound_kiro_agent: str = "") -> str:
    """The agent a ``member`` record chats on, given its member's current binding.

    The conductor when *bound_kiro_agent* (the member's ``kiro_agent`` in config
    now) owns a crewmate file, whatever older template the record kept; else
    :func:`member_chat_template` on the record's own *template_id*. The one rule the
    chat (``session_agent_selection``) and :func:`is_conductor_execution` share.
    """
    if bound_kiro_agent and is_mate_owned_agent(bound_kiro_agent):
        return MATE_CONDUCTOR_AGENT
    return member_chat_template(template_id)


def member_chat_template(template_id: str) -> str:
    """The agent a ``member`` record's *template_id* chats on.

    A record naming the crewmate's own file chats on the conductor; any other
    template is returned as named.
    """
    if not template_id or template_id == MATE_CONDUCTOR_AGENT:
        return template_id
    return MATE_CONDUCTOR_AGENT if is_mate_owned_agent(template_id) else template_id


def is_legacy_binding(name: str, binding: Any) -> bool:
    """Whether ``config.agents[name]`` is a legacy built-in binding, not a crewmate.

    Positive signature: a ``builtin`` binding that never got a member identity
    (an older install's ``assistant``, the built-in ``kirocrew-conductor``).
    ``GET /api/agents`` lists one only once its DM thread holds a message, the
    rule ``GET /api/members`` applies to every non-dashboard-created row. The
    reserved ``default`` member is excluded by name, and a user-created crewmate
    on shared memory (``source`` ``kirocrew``, no id yet) is not legacy.
    """
    if name == "default":
        return False
    if getattr(binding, "source", "") != "builtin":
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

    Read from the materialized-agents snapshot, which the crewmate writers update.
    An unknown name is rescanned off the loop. On the loop a rescan is scheduled
    and the answer comes from :func:`_mate_file_on_disk`, two file stats, so a
    crewmate created a moment ago (its file written by another process) chats on
    the conductor and never on its own file.
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
        # answers from the two files a full scan would weigh, not a rescan.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loader.refresh_materialized_agents(heal_default=False)
        else:
            loader.schedule_materialized_agents_refresh()
            return _mate_file_on_disk(kiro_agent)
    return kiro_agent in loader._MATERIALIZED_MATE_ONLY


def _mate_file_on_disk(kiro_agent: str) -> bool:
    """Whether ``<mates>/<kiro_agent>.json`` exists and ``<agents>/<kiro_agent>.json`` does not.

    The on-loop answer for a name the snapshot does not know yet: two ``stat``
    calls, no directory scan. A user-level file of that name wins, the rule the
    full scan applies, so a real user agent is never read as a crewmate. The mate
    conductor and worker share the crewmate folder and are never a crewmate's file.
    A name that is not a plain file stem, or a stat that fails, answers False.
    """
    from kiro_crew.agent_files import is_mate_service_agent
    from kiro_crew.config import paths as paths_mod

    if is_mate_service_agent(kiro_agent) or Path(kiro_agent).name != kiro_agent:
        return False
    if kiro_agent in (".", ".."):
        return False
    try:
        if (paths_mod.kiro_agents_dir() / f"{kiro_agent}.json").exists():
            return False
        return (paths_mod.mate_agents_dir() / f"{kiro_agent}.json").is_file()
    except (OSError, ValueError):
        return False


def member_chat_agent(kiro_agent: str) -> str:
    """The agent a crewmate's own chat runs on, for its bound ``kiro_agent``.

    The conductor when the binding owns a crewmate file
    (:func:`is_mate_owned_agent`), else the bound name through
    ``dispatch_kiro_agent``. The crewmate file never reaches the chat.
    """
    from kiro_crew.config.loader import dispatch_kiro_agent

    if is_mate_owned_agent(kiro_agent):
        return MATE_CONDUCTOR_AGENT
    return dispatch_kiro_agent(kiro_agent)


#: Keys a platform-server entry may carry and still say nothing of its own: an
#: on/off switch with no transport.
_BARE_OVERRIDE_KEYS = frozenset({"disabled"})


def drop_partial_platform_servers(spec: Any) -> list[str]:
    """Remove from *spec* each platform-server entry that is only an on/off switch.

    The gateway supplies platform servers (:func:`conductor_mcp_servers`); an entry
    like ``{"disabled": false}`` with no transport would never launch. Anything
    else, and a switch-OFF (a restriction, :func:`disabled_base_servers`), stays.
    Edits *spec* in place; returns the names removed.
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


def server_selectors(spec: dict[str, Any], names: Any) -> list[str]:
    """The ``tools`` entries of *spec* that select a tool on one of the servers *names*.

    ``@server`` mounts the whole server and ``@server/tool`` one tool of it, so
    a template that lists only ``@notes/read`` gives ``notes`` that one tool. A
    ``*`` in *spec*'s tools selects every server whole. A server *spec* never
    selects gets nothing: declaring a server is not mounting it.
    """
    wanted = set(names)
    tools = _strings(spec.get("tools"))
    if "*" in tools:
        return [f"@{name}" for name in names]
    return [ref for ref in dict.fromkeys(tools) if _server_of(ref) in wanted]


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
    # The file's own selectors for the servers it adds, so a template that
    # mounts one tool of a server does not hand the worker the whole server.
    additions["tools"] = server_selectors(mate_spec, added_servers)
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
    if "*" not in tools:
        for ref in _strings(additions.get("tools")):
            if _server_of(ref) in added and ref not in tools:
                tools.append(ref)
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

    The KAS wire carries only the agent spec, so the page's ``disabledTools``
    switch is applied here to *server_names*, read through ``list_servers`` as
    the page shows it. An unreadable config adds nothing. Blocking.
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


def resolve_worker_spec(
    base: dict[str, Any], member_id: str | None, *, refuse_tool_limits: bool = False
) -> dict[str, Any]:
    """The worker spec a session of *member_id*'s worker runs on.

    *base* has its prompt literal. No member, or a member not on its own file, gives
    the governed base less its revocations. A binding on its own file whose file is
    absent or shadowed, or an unreadable file or record, raises
    :class:`WorkerKitUnreadable` (fail closed). With *refuse_tool_limits* (a host
    that cannot honour single-tool limits), a file that sets any refuses with
    ``capability_tool_limits_unsupported``, judged on the same read the merge uses.
    """
    from kiro_crew import mate_approval_revocations as revocations

    if not member_id:
        return merge_worker_spec(base, _with_global_tool_offs(base, {}))
    name = mate_file_for_member(member_id)
    if not name:
        claimed = _claimed_file(member_id)
        if claimed:
            raise WorkerKitUnreadable(
                f"crewmate {member_id!r}: its own file {claimed!r} is missing or shadowed "
                f"by a user-level agent, so its worker would run without its limits"
            )
        return revocations.strip(
            merge_worker_spec(base, _with_global_tool_offs(base, {})),
            _revoked_or_refuse(member_id, _binding_alias(member_id)),
        )
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
    except agent_specs.AgentSpecUnreadable as exc:
        logger.warning(
            "crewmate %r: its worker file %r is unreadable; refusing the worker",
            member_id,
            name,
            exc_info=True,
        )
        raise WorkerKitUnreadable(
            f"crewmate {member_id!r}: its worker file {name!r} cannot be read, so its "
            f"worker would run without the crewmate's own limits; fix or re-save the file"
        ) from exc
    if refuse_tool_limits and tool_limits_of(mate_spec):
        from kiro_crew.session_capabilities import CapabilityStartupError

        raise CapabilityStartupError("capability_tool_limits_unsupported")
    additions = worker_additions(base, mate_spec)
    # An approval the owner took away in Approvals stays away, base-granted or not.
    return revocations.strip(
        merge_worker_spec(base, _with_global_tool_offs(base, additions)),
        _revoked_or_refuse(member_id, name),
    )


def _binding_alias(member_id: str) -> str:
    """The roster key of *member_id*'s binding (its file id), ``""`` when unknown."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import member_config_for_id

    try:
        return member_config_for_id(KiroCrewConfig.load(), member_id)[0]
    except Exception:  # noqa: BLE001 - an unknown or ambiguous id has no binding
        return ""


def _claimed_file(member_id: str, config: Any = None) -> str:
    """The file id *member_id*'s binding points at as its own, ``""`` when it does not."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import member_config_for_id

    try:
        alias, binding = member_config_for_id(config or KiroCrewConfig.load(), member_id)
    except Exception:  # noqa: BLE001 - an unknown or ambiguous id claims no file
        return ""
    return alias if getattr(binding, "kiro_agent", "") == alias else ""


def _claimed_crewmate_file(member_id: str, config: Any = None) -> str:
    """The crewmate file *member_id* had and lost, ``""`` for any other binding.

    Positive identity, not a guess from what does not resolve: the binding's
    ``kiro_agent`` is its own alias AND the crewmate owner record names that id
    (:func:`mate_config.crewmate_file_on_record`; every crewmate file write
    records it, and nothing removes it). A self-named binding to an installed,
    workspace-declared or built-in agent was never a crewmate file, so it keeps
    the member route. An owner record that cannot be read keeps a self-named
    binding refused. Blocking file I/O.
    """
    from kiro_crew.mate_config import MateConfigError, crewmate_file_on_record

    claimed = _claimed_file(member_id, config)
    if not claimed:
        return ""
    try:
        return claimed if crewmate_file_on_record(claimed) else ""
    except MateConfigError:
        logger.warning("crewmate owner record unreadable; %r stays refused", claimed)
        return claimed


def _revoked_or_refuse(member_id: str, file_id: str) -> list[str]:
    from kiro_crew import mate_approval_revocations as revocations

    try:
        return revocations.revoked(file_id)
    except revocations.RevocationsUnreadable as exc:
        raise WorkerKitUnreadable(
            f"crewmate {member_id!r}: the approvals its owner revoked cannot be read "
            f"(revoked-approvals.json), so its worker would run them again; fix the file"
        ) from exc


def mate_service_scope_spec(agent: str, session_key: str) -> tuple[dict | None, Path] | None:
    """``(spec or None when unreadable, path)`` the crewmate conductor's or worker's
    skill scope reads, else ``None``: their specs live in the crewmate folder, which
    the roster does not list. A worker's is its session's merged spec."""
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod

    if agent not in (MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT):
        return None
    path = paths_mod.mate_agents_dir() / f"{agent}.json"
    try:
        spec = agent_specs.load_agent_spec(path.parent, agent)
        if agent == MATE_WORKER_AGENT:
            spec = worker_session_spec({**spec, "prompt": ""}, session_key)
    except (agent_specs.AgentSpecUnreadable, WorkerKitUnreadable):
        return None, path
    return spec, path


def is_own_worker_execution(execution: Any) -> bool:
    """Whether *execution* is a crewmate's own worker: the worker on that crewmate's store.

    Positive identity from the attested record ``session_create`` binds at the
    child's birth: a ``template`` selection running :data:`MATE_WORKER_AGENT`
    with a member id. Every other record, and no record, answers False.
    """
    return (
        execution is not None
        and getattr(execution, "selection_kind", "") == "template"
        and getattr(execution, "template_id", "") == MATE_WORKER_AGENT
        and bool(getattr(execution, "member_id", None))
    )


def cron_worker_execution(execution: Any, config: Any = None) -> Any:
    """The worker execution a crewmate's scheduled agent turn runs, else ``None``.

    A crewmate's ``member`` cron capture runs on its worker, member, store, mode
    and app kept. A crewmate whose own file is gone runs on the worker too
    (:func:`_claimed_crewmate_file`), so the worker's own refusal
    (:func:`resolve_worker_spec`) stops it rather than the conductor running it.
    ``None`` for any other record.
    Blocking config read.
    """
    member_id = getattr(execution, "member_id", None)
    if (
        execution is None
        or getattr(execution, "selection_kind", "") != "member"
        or not member_id
        or not (
            mate_file_for_member(member_id, config=config)
            or _claimed_crewmate_file(member_id, config)
        )
    ):
        return None
    # The hand-off's own spelling (``session_create``): a template selection on
    # the worker under its own name, the member id kept.
    return execution.with_template(MATE_WORKER_AGENT, MATE_WORKER_AGENT)


def note_worker_session(session_key: str, execution: Any) -> bool:
    """Publish *session_key* as a crewmate's worker when its record says so.

    What makes the backend selection (``members.select_provider_backend``) route
    the worker to its crewmate's backend: the selection is an in-memory test, and
    this is the one writer, called wherever the record is read before a session
    is allocated and when an agent switch records a new selection. Returns whether
    the key is a worker. Any other record (a session switched onto another
    template) drops the key, so the session leaves the crewmate's backend with its
    worker template.
    """
    from kiro_crew.members import forget_crewmate_worker_session, note_crewmate_worker_session

    if not is_own_worker_execution(execution):
        forget_crewmate_worker_session(session_key)
        return False
    note_crewmate_worker_session(session_key)
    return True


def worker_session_spec(
    literal_base: dict[str, Any], session_key: str, *, refuse_tool_limits: bool = False
) -> dict[str, Any]:
    """The spec a crewmate's worker session on *session_key* runs: base plus its kit.

    The harness-neutral seam; *literal_base* has its prompt literal. The crewmate
    comes from the session's own execution record, never a supplied name; a session
    with no record or no member is refused (:class:`WorkerKitUnreadable`). KAS's
    projection reads it, and so does the session MCP projection the member-capable
    mirrored hosts read (``acp.session_mcp.session_mcp_projection``, keyed by the
    session). Blocking.
    """
    from kiro_crew.execution_context import read_session_execution

    member_id: str | None = None
    try:
        execution = read_session_execution(session_key) if session_key else None
    except Exception as exc:  # noqa: BLE001 - any unreadable record refuses
        logger.warning(
            "worker session %s: execution record unreadable; refusing the worker",
            session_key,
            exc_info=True,
        )
        raise WorkerKitUnreadable(
            f"worker session {session_key}: its execution record cannot be read, so "
            f"its crewmate (and that crewmate's limits) is unknown"
        ) from exc
    if execution is not None:
        member_id = execution.member_id
    if not member_id:
        raise WorkerKitUnreadable(
            f"worker session {session_key}: it names no crewmate, so whose limits apply is unknown"
        )
    if refuse_tool_limits:
        return resolve_worker_spec(literal_base, member_id, refuse_tool_limits=True)
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

    Only ``ACP_BACKENDS_MATE_WORKER_KIT`` carry the per-session spec (H6).
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


def crewmate_tool_limited_servers(member_id: str) -> tuple[str, ...]:
    """What crewmate *member_id*'s own file narrows for its worker, sorted.

    The member rule on the file (``tool_limited_servers``: an entry's
    ``disabledTools``), plus every entry of its own ``excludedTools`` (as a copied
    kit keeps them): the server an ``@server`` / ``@server/tool`` names, or the
    native tool itself. The worker spec carries all of them as ``excludedTools``
    (:func:`worker_additions`), which only ``ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS``
    honour. A server the file mutes whole is not counted. Blocking.
    """
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod

    name = mate_file_for_member(member_id) if member_id else ""
    if not name:
        return ()
    try:
        spec = agent_specs.load_agent_spec(paths_mod.mate_agents_dir(), name)
    except agent_specs.AgentSpecUnreadable:
        return ()
    return tool_limits_of(spec)


def tool_limits_of(spec: dict[str, Any]) -> tuple[str, ...]:
    """What crewmate file *spec* narrows for its worker (see
    :func:`crewmate_tool_limited_servers`), from this one parse."""
    from kiro_crew.agent_capabilities import tool_limited_servers
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    limited = set(tool_limited_servers(spec))
    servers = _servers(spec.get("mcpServers"))
    for ref in _strings(spec.get("excludedTools")):
        server = _server_of(ref)
        if not server:
            # A native tool: the mirrored hosts project no exclusion for it either.
            if ref.strip():
                limited.add(ref.strip())
        elif not (server in servers and mcp_entry_is_muted(servers[server])):
            limited.add(server)
    return tuple(sorted(limited))


def worker_prompt_in_context_block(session_key: str) -> bool:
    """Whether worker *session_key* gets its prompt only from the context block.

    Answered by the session's own live provider, recorded at allocation
    (``mate_worker_prompt_in_context``, membership of its backend in
    ``ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT``), never by the config, which
    can name another backend while this session runs.
    """
    from kiro_crew.members import crewmate_worker_prompt_in_context

    return crewmate_worker_prompt_in_context(session_key)


def member_worker_spec(member: str) -> dict[str, Any]:
    """The spec a worker session of *member* would run on. Blocking."""
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod

    try:
        # Generated into the crewmate folder (``agent_files.MATE_SERVICE_AGENT_FILENAMES``).
        base = agent_specs.load_agent_spec(paths_mod.mate_agents_dir(), MATE_WORKER_AGENT)
    except agent_specs.AgentSpecUnreadable:
        logger.warning("the worker spec is unreadable; reading an empty worker")
        base = {"name": MATE_WORKER_AGENT, "tools": [], "mcpServers": {}}
    # Read for its tools and skills, never the prompt.
    base = {**base, "prompt": ""}
    return resolve_worker_spec(base, _roster_member_id(member))


def _roster_member_id(member: str) -> str:
    """The persisted ``member_id`` of the crewmate named *member* (its roster key).

    The worker merge reads the crewmate's file by member id, and a crewmate's
    name and id differ (``Briar`` / ``briar``). A name the roster does not hold
    is passed on as it is. Blocking.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        record = KiroCrewConfig.load().agents.get(member)
    except Exception:  # noqa: BLE001 - an unreadable config resolves nothing
        return member
    member_id = getattr(record, "member_id", "") if record is not None else ""
    return member_id if isinstance(member_id, str) and member_id else member
