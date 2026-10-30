"""The agents Kiro Crew's own services drive: lite, guest, knowledge and research.

``kirocrew-lite`` is the cheap background helper; ``kirocrew-guest`` is the tool-less
agent a non-operator channel sender talks to, a trust boundary that mounts nothing;
``kirocrew-knowledge`` runs the Knowledge Library's extraction; ``kirocrew-research``
is the Research Lab's per-cycle worker, derived from the default template so it
inherits the governance ceiling. Each is rewritten on every rebuild, and the
knowledge spec also before each knowledge worker spawn when the file on disk is not
its template (:func:`repair_knowledge_agent_spec`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery, agent_state
from kiro_crew.agent_files import (
    DASHBOARD_MANAGER_AGENT_FILENAME as _DASHBOARD_MANAGER_AGENT_FILENAME,
)
from kiro_crew.agent_files import GUEST_AGENT_FILENAME as _GUEST_AGENT_FILENAME
from kiro_crew.agent_files import KNOWLEDGE_AGENT_FILENAME as _KNOWLEDGE_AGENT_FILENAME
from kiro_crew.agent_files import LITE_AGENT_FILENAME as _LITE_AGENT_FILENAME
from kiro_crew.agent_files import MATE_CONDUCTOR_AGENT_FILENAME as _MATE_CONDUCTOR_AGENT_FILENAME
from kiro_crew.agent_files import MATE_WORKER_AGENT_FILENAME as _MATE_WORKER_AGENT_FILENAME
from kiro_crew.agent_files import RESEARCH_AGENT_FILENAME as _RESEARCH_AGENT_FILENAME
from kiro_crew.agent_files import TEAM_LEAD_AGENT_FILENAME as _TEAM_LEAD_AGENT_FILENAME
from kiro_crew.agent_materialization import auto_approve, managed_mcp, skill_resources
from kiro_crew.user_json import strict_json_equal


def _install_guest_agent() -> None:
    """Write the tool-less ``kirocrew-guest`` config a non-operator sender talks to.

    Separate from ``kirocrew-lite`` on purpose: the lite agent is the background
    helper (titles, extraction) and may one day need a tool; this one is a trust
    boundary and never may. Same model as the operator's chat so an admitted
    sender gets an ordinary answer, never a background worker's minimal default.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        model = KiroCrewConfig.load().agent.model or "auto"
    except Exception:
        model = "auto"
    guest_path = agent_mod.kiro_agents_dir_path() / _GUEST_AGENT_FILENAME
    guest_config = {
        "name": "kirocrew-guest",
        "model": model,
        "tools": [],
        "mcpServers": {},
        # Pinned: kiro-cli defaults this to True and would spawn every server in
        # the user-level mcp.json for a session that must mount nothing.
        "includeMcpJson": False,
        "prompt": agent_mod.GUEST_AGENT_PROMPT,
    }
    auto_approve.write_governed_spec(
        guest_path, guest_config, source="_install_guest_agent", kas_policy="none"
    )


def _exclude(config: dict, refs: tuple[str, ...]) -> None:
    """Add *refs* to *config*'s ``excludedTools``, keeping what it already lists."""
    have = config.get("excludedTools")
    kept = [t for t in have if isinstance(t, str)] if isinstance(have, list) else []
    config["excludedTools"] = list(dict.fromkeys([*kept, *refs]))


def _write_mate_service_spec(filename: str, config: dict, source: str) -> None:
    """Write a mate conductor/worker spec into ``~/.kiro/mates/agents/``.

    Not ``~/.kiro/agents/``: kiro-cli lists every spec there, and neither of
    these is an agent anyone picks (``agent_files.MATE_SERVICE_AGENT_FILENAMES``).
    An earlier build wrote them to ``~/.kiro/agents/``; the copy it left is
    removed here (:func:`_remove_user_level_copy`).
    """
    from kiro_crew.config import paths as paths_mod

    mates = paths_mod.mate_agents_dir()
    mates.mkdir(parents=True, exist_ok=True)
    path = mates / filename
    # Keep the operator's inherited deny rules, as the other derived agents do.
    auto_approve.write_governed_spec(path, config, source=source, kas_policy="inherit_narrowing")
    agent_mod.logger.info("Installed %s: %s", filename, path)
    _remove_user_level_copy(filename, config)


def _remove_user_level_copy(filename: str, config: dict) -> None:
    """Remove the ``~/.kiro/agents/<filename>`` an earlier build of this branch wrote.

    That build rewrote the file on every start with the ``name`` and
    ``description`` the installer sets, so a file carrying both is its own and
    is deleted. Anything else at that name (a symlink, an unreadable file, or a
    spec whose name or description someone changed) is the user's and is kept,
    with one warning: a spec there is what kiro-cli lists, and Crew does not
    read it. Never raises.
    """
    import json
    import os

    path = agent_mod.kiro_agents_dir_path() / filename
    try:
        if not os.path.lexists(path):
            return
        ours = False
        if path.is_file() and not path.is_symlink():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            ours = (
                isinstance(data, dict)
                and data.get("name") == config.get("name")
                and data.get("description") == config.get("description")
            )
        if ours:
            path.unlink()
            agent_mod.logger.info(
                "Removed %s left in %s by an earlier build", filename, path.parent
            )
        else:
            agent_mod.logger.warning(
                "%s is not the spec Kiro Crew generated, so it is kept; Kiro Crew now "
                "keeps that spec in %s and does not read this file",
                path,
                path.parent.parent / "mates" / "agents",
            )
    except OSError:
        agent_mod.logger.warning("could not remove %s", path, exc_info=True)


def _install_mate_conductor_agent() -> None:
    """Generate and install the ``kirocrew-mate-conductor`` agent config.

    The agent a crewmate's own chat runs on, derived from the default template
    like ``kirocrew-research``. It excludes the sub-agent tools, ``task_run`` and
    ``workflow_run`` (``mate_worker.MATE_CONDUCTOR_EXCLUDED_TOOLS``), keeps only
    the platform servers (``mate_worker.conductor_mcp_servers``) with
    ``includeMcpJson`` off, and never reads the crewmate's own file.
    """
    from kiro_crew.mate_worker import (
        MATE_CONDUCTOR_EXCLUDED_TOOLS,
        conductor_mcp_servers,
    )

    config = agent_mod.build_agent_config()
    config["name"] = "kirocrew-mate-conductor"
    config["description"] = "The agent a crewmate's own chat runs on."
    # The conductor dispatches and the worker does the work: its hand-off is a
    # worker session, so the sub-agent tools are not in its tool list.
    # Its MCP servers are the gateway's own, whatever servers the user has:
    # user servers reach the worker only, through the crewmate's file.
    platform = conductor_mcp_servers()

    def _kept(ref: str) -> bool:
        # Built-ins stay; a server ref survives only for a platform server.
        return isinstance(ref, str) and (
            not ref.startswith("@") or ref[1:].split("/", 1)[0] in platform
        )

    servers = config.get("mcpServers")
    config["mcpServers"] = (
        {name: entry for name, entry in servers.items() if name in platform}
        if isinstance(servers, dict)
        else {}
    )
    config["includeMcpJson"] = False
    for key in ("tools", "allowedTools"):
        listed = config.get(key)
        if isinstance(listed, list):
            config[key] = [t for t in listed if _kept(t)]
    _exclude(config, MATE_CONDUCTOR_EXCLUDED_TOOLS)
    _write_mate_service_spec(
        _MATE_CONDUCTOR_AGENT_FILENAME, config, "_install_mate_conductor_agent"
    )


def _install_mate_worker_agent() -> None:
    """Generate and install the ``kirocrew-mate-worker`` agent config.

    The agent a crewmate's worker sessions run on: the session its chat opens
    with ``session_create`` to hand work off. Derived from the default template
    like the mate conductor above, so its base prompt, tools and ``mcpServers``
    are the ones a crewmate's hand-off session ran on before it had a worker of
    its own. This file is the worker's whole SOURCE BASE; a crewmate's own
    additions (``~/.kiro/mates/agents/<id>.json``) are layered on at projection
    time by :mod:`kiro_crew.mate_worker`, never written here, so one file serves
    every crewmate.
    """
    from kiro_crew.mate_worker import (
        MATE_WORKER_AGENT,
        MATE_WORKER_EXCLUDED_TOOLS,
        MATE_WORKER_PROMPT,
    )

    config = agent_mod.build_agent_config()
    config["name"] = MATE_WORKER_AGENT
    config["description"] = "The agent a crewmate's worker sessions run on."
    # The worker's own principles ride after the shared base prompt.
    principles = MATE_WORKER_PROMPT
    base_prompt = config.get("prompt") if isinstance(config.get("prompt"), str) else ""
    config["prompt"] = f"{base_prompt.rstrip()}\n\n{principles}" if base_prompt else principles
    # Asking the user for a capability is the conductor's: a worker reports a
    # missing tool back to the chat that opened it.
    _exclude(config, MATE_WORKER_EXCLUDED_TOOLS)
    _write_mate_service_spec(_MATE_WORKER_AGENT_FILENAME, config, "_install_mate_worker_agent")


def _install_lite_agent_fallback() -> None:
    """Write a bare kirocrew-lite config (cheap background agent)."""
    lite_path = agent_mod.kiro_agents_dir_path() / _LITE_AGENT_FILENAME
    lite_config = {
        "name": "kirocrew-lite",
        "model": agent_mod._background_agent_model(),
        "tools": [],
        "mcpServers": {},
        "prompt": "",
    }
    # Mounts nothing, so the tail has no grant to govern; what it adds here is the
    # ``includeMcpJson`` pin, without which kiro-cli merges the global ``mcp.json``.
    auto_approve.write_governed_spec(
        lite_path, lite_config, source="_install_lite_agent_fallback", kas_policy="none"
    )
    # Cheap model for the claude_code (CC) provider. kiro-cli resolves the lite
    # model from `model` via --agent; the CC backend can't, so the provider
    # factory reads this cc_model for the lite agent. The kiro spec above uses
    # the resolved background role model (default "auto", entitlement-safe on
    # every tier); the CC seam needs a concrete model, so it falls back to the
    # cheap default when the role is unpinned. Stored in the sidecar (kiro spec
    # stays schema-clean).
    agent_state.set_cc_model("kirocrew-lite", agent_mod._background_cc_model())


def knowledge_agent_model() -> str:
    """The model the kirocrew-knowledge agent runs on.

    ``knowledge.extraction_model``, else ``agent.model`` (extraction runs on the
    same model as chat), else ``"auto"``. One rule for both readers: the installer
    writes it into the spec, which kiro-cli reads natively, and ``LLMPool`` hands
    it to a harness that reads no spec of Crew's.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
        model = cfg.knowledge.extraction_model.strip()
        if not model:
            # Use the user's default model (same as chat).
            model = cfg.agent.model or "auto"
    except Exception:
        model = "auto"
    return model


def _knowledge_agent_config() -> dict[str, Any]:
    """The ``kirocrew-knowledge`` spec as both of its writers build it, before the tail.

    Its model follows :func:`knowledge_agent_model`, so the installer and the
    pre-spawn repair write the same document.
    """
    # Resolve model: knowledge.extraction_model > agent.model > "auto"
    model = knowledge_agent_model()

    return {
        "name": "kirocrew-knowledge",
        "description": (
            "Dedicated agent for knowledge extraction, categorization, " "and summarization."
        ),
        "model": model,
        "includeMcpJson": False,
        "prompt": agent_mod._KNOWLEDGE_SYSTEM_PROMPT,
        "mcpServers": {},
        "tools": [],
    }


def _install_knowledge_agent() -> None:
    """Generate and install the kirocrew-knowledge agent config.

    This agent is used by the Knowledge Library's LLMPool for document
    extraction. By default it uses the user's configured agent.model (so
    extraction runs on the same model as chat). If the user sets
    knowledge.extraction_model explicitly, that model is used instead —
    allowing a cheaper model for extraction without changing the chat default.
    """
    path = agent_mod.kiro_agents_dir_path() / _KNOWLEDGE_AGENT_FILENAME
    config = _knowledge_agent_config()
    auto_approve.write_governed_spec(
        path, config, source="_install_knowledge_agent", kas_policy="none"
    )
    agent_mod.logger.info("Installed knowledge agent config: %s (model=%s)", path, config["model"])


#: How many differing top-level keys a repair warning names. Enough for any real
#: rewrite; the bound keeps a file stuffed with keys from flooding the log line.
_REPAIR_DRIFT_KEYS_SHOWN = 8


def repair_knowledge_agent_spec() -> bool:
    """Rewrite ``kirocrew-knowledge.json`` from its template unless the file already is it.

    The Knowledge Library's pool feeds untrusted document text to this agent, and
    ``knowledge.llm_pool.AcpWorker.start`` refuses to start a worker until the spec
    the harness runs under is confirmed to declare ``"tools": []``. The spec is Crew's
    own and is re-derived, never preserved (``rfc-team-lead-default-agent.md``), but
    only the rebuild re-derives it: a tool that rewrites every file in the agents
    directory (a launcher adding its own MCP server, say) leaves the pool refusing
    every batch until the next rebuild. The pool calls this before each worker spawn,
    so the confirmation reads the template.

    The file is compared parsed against the governed template, so a re-serialized
    copy of the same spec is left alone and a clean file costs one read. Any other
    file -- a key added, a value changed, a file that is missing or cannot be read as
    a spec -- is rewritten through the same governed tail the installer uses, with a
    WARNING naming the top-level keys that differed and never their values: an
    injected server's ``env`` may hold a credential.

    Returns True when it rewrote the file. Never raises: a write this data home may
    not make (another home owns the agents directory) or one that fails leaves the
    file as it is, and the caller's confirmation still decides whether a worker
    starts. Blocking; async callers run it with ``asyncio.to_thread``.
    """
    path = agent_mod.kiro_agents_dir_path() / _KNOWLEDGE_AGENT_FILENAME
    try:
        config = _knowledge_agent_config()
        auto_approve.govern_spec(
            config,
            agent_filename=path.name,
            source="repair_knowledge_agent_spec",
            kas_policy="none",
        )
        drift = _spec_drift(path, config)
        if drift is None:
            return False
        auto_approve.write_governed_spec(
            path, config, source="repair_knowledge_agent_spec", kas_policy="none"
        )
    except agent_mod.SharedAgentHomeRefused:
        # The write guard already warned, once per process, that this home does not
        # own the directory; repeating it on every spawn adds nothing.
        agent_mod.logger.debug("knowledge agent spec repair declined: %s", path, exc_info=True)
        return False
    except Exception:
        agent_mod.logger.warning(
            "Could not rewrite the knowledge agent spec %s from its template", path, exc_info=True
        )
        return False
    agent_mod.logger.warning(
        "Knowledge agent spec %s was not the spec Kiro Crew writes (%s); rewrote it from "
        "the template so the knowledge pool's zero-tool check can pass",
        path,
        drift,
    )
    return True


def _spec_drift(path: Path, expected: dict[str, Any]) -> str | None:
    """How the spec at *path* differs from *expected*, or ``None`` when it does not.

    Read through the hardened reader, so a link planted at the name is screened
    before anything follows it. Every failure to read a spec counts as drift: the
    file is Crew's own, and the governed write replaces the directory entry rather
    than writing through it.
    """
    try:
        on_disk = agent_discovery.read_agent_spec_strict(
            path, operation="knowledge_spec_repair", source="unknown"
        )
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError) as exc:
        return f"unreadable: {type(exc).__name__}"
    if not isinstance(on_disk, dict):
        return "not a JSON object"
    # Type-strict: a ``0`` where the template has ``false`` is a different file to
    # kiro-cli's schema, though Python's ``==`` calls them equal.
    if strict_json_equal(on_disk, expected):
        return None
    keys = sorted(
        str(key)
        for key in on_disk.keys() | expected.keys()
        if key not in on_disk
        or key not in expected
        or not strict_json_equal(on_disk[key], expected[key])
    )
    shown = [repr(key)[:64] for key in keys[:_REPAIR_DRIFT_KEYS_SHOWN]]
    more = len(keys) - len(shown)
    return "differs in " + ", ".join(shown) + (f" and {more} more" if more else "")


def _without_guide_server(config: dict) -> dict:
    """*config* with the guide and member platform servers, refs and grants removed.

    A background agent is driven by a loop, never by a person at the dashboard,
    so it has no one to show a guide or a change card to, is nobody's crewmate,
    and keeps the narrower server set.
    """
    refs = tuple(f"@{s}" for s in (agent_mod._GUIDE_SERVER, agent_mod._MATE_SERVER))
    servers = config.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop(agent_mod._GUIDE_SERVER, None)
        servers.pop(agent_mod._MATE_SERVER, None)
    for key in ("tools", "allowedTools"):
        entries = config.get(key)
        if isinstance(entries, list):
            config[key] = [
                e
                for e in entries
                if not any(
                    e == ref or (isinstance(e, str) and e.startswith(ref + "/")) for ref in refs
                )
            ]
    return config


def _install_research_agent() -> None:
    """Generate and install the kirocrew-research agent config.

    Built from :func:`build_agent_config` -- the shipped template, the operator's
    ``~/.kiro/crew/agent.json`` and the managed servers -- with a lean
    research-worker prompt + identity, and without the platform guide server
    (:func:`_without_guide_server`). Used by the Research Lab app's autonudge loop
    to run one research cycle per turn.

    It deliberately does NOT read the installed ``kirocrew.json`` the way
    ``_install_worker_agent`` does (``_installed_default_spec``): servers, app
    registrations and the model a user added there stay off this spec. The loop
    runs unattended with tool auto-approval, so widening what it can reach is a
    governance decision, not a mirror to keep in sync. A campaign's own model
    pick is pinned on its worker slot instead.
    """
    config = _without_guide_server(agent_mod.build_agent_config())
    config["name"] = "kirocrew-research"
    config["description"] = (
        "Autonomous research worker — runs one research cycle per turn "
        "in a Research Lab campaign loop."
    )
    config["prompt"] = agent_mod._RESEARCH_SYSTEM_PROMPT
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _RESEARCH_AGENT_FILENAME
    # ``build_agent_config`` carries the operator's ``agent.json`` block, and this spec
    # has always carried it: keep its ``deny``/``ask`` rules, derive the allows.
    auto_approve.write_governed_spec(
        path, config, source="_install_research_agent", kas_policy="inherit_narrowing"
    )
    agent_mod.logger.info("Installed research agent config: %s", path)


def _mount_team_lead_dispatch(config: dict) -> None:
    """Mount the dispatch surface a team lead needs, in place.

    ``build_agent_config`` skips every ``opt_in`` server, so a spec derived from it
    carries no ``kirocrew-dashboard`` (``session_create``, ``chat_folder_*``) and no
    ``kirocrew-work`` (``work_ledger_*``). Naming them in ``tools`` alone is a DEAD
    grant: kiro-cli mounts nothing for a server the spec declares but does not
    configure. So both entries are built here through
    ``managed_mcp._managed_opt_in_entry`` -- the call the dashboard-manager and
    worker installers make -- which carries the two fields that fail silently:
    ``"type": "registry"`` (a registry-mode client drops an entry without it) and
    the ``KIROCREW_HOME`` pin (without it session control acts on a different
    session store than the one it reports on).

    The verbs auto-approved on them are the goal conductor's own tuples,
    ``_CONDUCTOR_DASHBOARD_GRANTS`` and ``_LEDGER_CONDUCTOR_WORK_GRANTS``, reused
    rather than restated. The installer writes the result through the shared tail
    (``auto_approve.write_governed_spec``), which puts the whole ``allowedTools`` list
    back through the governance ceiling, strips every inherited server's ungoverned
    ``autoApprove`` and derives the KAS ``permissions`` block from the filtered list,
    as the conductor and worker installers do. A withheld verb stays mounted and
    prompts.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["kirocrew-dashboard"] = managed_mcp._managed_opt_in_entry("mcp-dashboard")
    servers["kirocrew-work"] = managed_mcp._managed_opt_in_entry("mcp-work")
    config["mcpServers"] = servers
    tools = [t for t in config.get("tools") or [] if isinstance(t, str)]
    for ref in ("@kirocrew-dashboard", "@kirocrew-work"):
        if ref not in tools:
            tools.append(ref)
    config["tools"] = tools
    allowed = [t for t in config.get("allowedTools") or [] if isinstance(t, str)]
    grants = (*agent_mod._CONDUCTOR_DASHBOARD_GRANTS, *agent_mod._LEDGER_CONDUCTOR_WORK_GRANTS)
    config["allowedTools"] = list(dict.fromkeys((*allowed, *grants)))


def _install_team_lead_agent() -> None:
    """Generate and install the kirocrew-team-lead agent config.

    Derives from the kirocrew agent (MCP servers, security, tools) and swaps in the
    team-lead charter + identity, leaving out the platform guide server
    (:func:`_without_guide_server`) exactly as research does. On top of that surface
    it mounts the two opt-in servers dispatch needs, ``kirocrew-dashboard`` and
    ``kirocrew-work`` (:func:`_mount_team_lead_dispatch`), granting the goal
    conductor's existing verb tuples through the governance ceiling.

    Rewritten on every boot, like ``kirocrew-research`` and ``kirocrew-knowledge``:
    a spec this installer wrote is replaced in place with no ceremony -- no ownership
    digest, no refusal and no start gate, which is the whole point of the overwrite
    design. An operator who wants a lead with different rules copies it to a different
    agent name.
    """
    config = _without_guide_server(agent_mod.build_agent_config())
    config["name"] = "kirocrew-team-lead"
    config["description"] = (
        "Owns a goal end to end and runs a team on it — splits it into work-ledger "
        "items, does the small focused ones itself, dispatches a session for every "
        "other one, and patrols that fleet."
    )
    config["prompt"] = agent_mod._TEAM_LEAD_SYSTEM_PROMPT
    # The three skills this agent is told to read: ``team-lead`` ("read it before
    # acting on a goal"), ``goal-conductor`` for the patrol procedure the charter
    # shares with the conductors, and ``dashboard`` because a lead is a crewmate and
    # the crewmate protocol sends it there to change its own page. Without the
    # mapping its search scope is empty and none of the three can be found.
    skill_resources.map_prompt_skills(config, "team-lead", "goal-conductor", "dashboard")
    _mount_team_lead_dispatch(config)
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _TEAM_LEAD_AGENT_FILENAME
    auto_approve.write_governed_spec(path, config, source="_install_team_lead_agent")
    agent_mod.logger.info("Installed team lead agent config: %s", path)


def _install_dashboard_manager_agent() -> None:
    """Generate and install the ``kirocrew-dashboard-manager`` subagent config.

    The agent a crewmate hands page work to. It is a SERVICE agent and not a
    conductor: it is installed here, beside guest and knowledge, because like them
    it mounts a fixed minimal surface rather than deriving the kirocrew agent's
    whole governance ceiling. Carrying nothing it does not need is the point -- it
    runs on a request forwarded from a chat it did not read.

    The mounted surface is ``@kirocrew-panel`` and ``fs_read`` and nothing else.

    * ``@kirocrew-panel`` whole, auto-approved verb by verb from
      ``_MEMBER_PANEL_GRANTS`` -- the SAME tuple the crewmate gets, reused rather
      than copied, so the two cannot drift into a surface this agent may call and
      its caller may not.

      The server is also CONFIGURED here, in ``mcpServers``, and not only declared
      in ``tools``. Neither spec-writing loop emits an ``opt_in`` server, so an
      installer that grants one builds the entry itself through
      ``managed_mcp._managed_opt_in_entry`` -- the same call
      ``worker_agent`` makes for ``@kirocrew-work``. Declared without being
      configured, the grant is DEAD: kiro-cli reports "MCP servers unusable in
      this session - declared by the agent spec but not configured", mounts no
      tool, and this agent is dispatched with nothing to answer with.
    * ``fs_read`` because the two skills it works from are files it must read, and
      because the fold catalogue it must not guess a path out of is one of them.
    * No ``fs_write`` and no ``execute_bash``. Every page this agent produces goes
      through ``dashboard_preview``, which validates the pair and stages it where
      only ``dashboard_apply`` can commit it. A file-writing tool would let it put
      a template into the user catalogue directly, skipping the validation and the
      person's yes -- which are the two things the preview step exists to be.
    * No ``@kirocrew-core`` and no ``@kirocrew-dashboard``: it neither dispatches
      work nor reads anybody's sessions. A page is all it touches.

    The assembled ``allowedTools`` goes through the governance ceiling before it is
    written, in the shared tail (``auto_approve.write_governed_spec``), which also
    derives the KAS policy from the filtered list. This installer states its grants as literals rather than deriving them
    from ``build_agent_config``, so it inherits no filter, and ``allowedTools`` is the
    one list whose entries never reach the PreToolUse gate: a ceiling that withholds
    one of the panel verbs from the crewmate has to withhold it here too, or this
    agent becomes the way around it. A withheld ref stays MOUNTED and its calls go
    through the gate.

    Model follows the user's chat model, like the guest agent's: a page is prose
    and markup written for a person to read, not a background extraction.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        model = KiroCrewConfig.load().agent.model or "auto"
    except Exception:
        model = "auto"
    config: dict[str, object] = {
        "name": agent_mod.DASHBOARD_MANAGER_AGENT_NAME,
        "description": (
            "Changes ONE crewmate's Dashboard page on request: searches the "
            "template catalog, puts the one that fits live, and rolls back to "
            "an earlier version. Never does the crewmate's own work."
        ),
        "model": model,
        "includeMcpJson": False,
        "prompt": agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT,
        "tools": ["fs_read", "@kirocrew-panel"],
        "allowedTools": ["fs_read", *agent_mod._MEMBER_PANEL_GRANTS],
        "mcpServers": {"kirocrew-panel": managed_mcp._managed_opt_in_entry("mcp-panel")},
    }
    # The one skill this prompt names ("load the ``dashboard`` skill first"). This
    # spec mounts no search tool on purpose, and the mapping does not add one: on
    # the kiro backend kiro-cli loads a mapped ``SKILL.md`` itself when the subagent
    # is spawned with ``--agent``, and with native skill projection on, the
    # projection answers a mapped spec with a read-only search mount it does not
    # auto-approve. Either way the document arrives without this installer granting
    # a verb, which is why the mapping is the whole change here.
    skill_resources.map_prompt_skills(config, "dashboard")
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _DASHBOARD_MANAGER_AGENT_FILENAME
    auto_approve.write_governed_spec(path, config, source="_install_dashboard_manager_agent")
    agent_mod.logger.info("Installed dashboard-manager agent config: %s", path)
