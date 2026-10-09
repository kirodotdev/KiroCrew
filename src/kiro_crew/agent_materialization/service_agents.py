"""The agents Kiro Crew's own services drive: lite, guest, knowledge and research.

``kirocrew-lite`` is the cheap background helper; ``kirocrew-guest`` is the tool-less
agent a non-operator channel sender talks to, a trust boundary that mounts nothing;
``kirocrew-knowledge`` runs the Knowledge Library's extraction; ``kirocrew-research``
is the Research Lab's per-cycle worker, derived from the default template so it
inherits the governance ceiling. Each is rewritten on every rebuild.
"""

from __future__ import annotations

from kiro_crew import agent as agent_mod
from kiro_crew import agent_state
from kiro_crew.agent_files import (
    DASHBOARD_MANAGER_AGENT_FILENAME as _DASHBOARD_MANAGER_AGENT_FILENAME,
)
from kiro_crew.agent_files import GUEST_AGENT_FILENAME as _GUEST_AGENT_FILENAME
from kiro_crew.agent_files import KNOWLEDGE_AGENT_FILENAME as _KNOWLEDGE_AGENT_FILENAME
from kiro_crew.agent_files import LITE_AGENT_FILENAME as _LITE_AGENT_FILENAME
from kiro_crew.agent_files import RESEARCH_AGENT_FILENAME as _RESEARCH_AGENT_FILENAME
from kiro_crew.agent_materialization import auto_approve, managed_mcp


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
    agent_mod._atomic_json_write(guest_path, guest_config)


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
    agent_mod._atomic_json_write(lite_path, lite_config)
    # Cheap model for the claude_code (CC) provider. kiro-cli resolves the lite
    # model from `model` via --agent; the CC backend can't, so the provider
    # factory reads this cc_model for the lite agent. The kiro spec above uses
    # the resolved background role model (default "auto", entitlement-safe on
    # every tier); the CC seam needs a concrete model, so it falls back to the
    # cheap default when the role is unpinned. Stored in the sidecar (kiro spec
    # stays schema-clean).
    agent_state.set_cc_model("kirocrew-lite", agent_mod._background_cc_model())


def _install_knowledge_agent() -> None:
    """Generate and install the kirocrew-knowledge agent config.

    This agent is used by the Knowledge Library's LLMPool for document
    extraction. By default it uses the user's configured agent.model (so
    extraction runs on the same model as chat). If the user sets
    knowledge.extraction_model explicitly, that model is used instead —
    allowing a cheaper model for extraction without changing the chat default.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    path = agent_mod.kiro_agents_dir_path() / _KNOWLEDGE_AGENT_FILENAME

    # Resolve model: knowledge.extraction_model > agent.model > "auto"
    try:
        cfg = KiroCrewConfig.load()
        model = cfg.knowledge.extraction_model.strip()
        if not model:
            # Use the user's default model (same as chat).
            model = cfg.agent.model or "auto"
    except Exception:
        model = "auto"

    config: dict[str, object] = {
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

    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed knowledge agent config: %s (model=%s)", path, model)


def _without_guide_server(config: dict) -> dict:
    """*config* with the platform guide server, its ref and its grants removed.

    A background agent is driven by a loop, never by a person at the dashboard,
    so it has no one to show a guide or a change card to and keeps the narrower
    server set.
    """
    ref = f"@{agent_mod._GUIDE_SERVER}"
    servers = config.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop(agent_mod._GUIDE_SERVER, None)
    for key in ("tools", "allowedTools"):
        entries = config.get(key)
        if isinstance(entries, list):
            config[key] = [
                e
                for e in entries
                if not (e == ref or (isinstance(e, str) and e.startswith(ref + "/")))
            ]
    return config


def _install_research_agent() -> None:
    """Generate and install the kirocrew-research agent config.

    Derives from the kirocrew agent (MCP servers, security, tools) but swaps in a
    lean research-worker prompt + identity, and leaves out the platform guide
    server (:func:`_without_guide_server`). Used by the Research Lab app's
    autonudge loop to run one research cycle per turn.
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
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed research agent config: %s", path)


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
    written. This installer states its grants as literals rather than deriving them
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
            "template catalog, stages a preview, asks before keeping it, and "
            "rolls back to an earlier version. Never does the crewmate's own work."
        ),
        "model": model,
        "includeMcpJson": False,
        "prompt": agent_mod._DASHBOARD_MANAGER_SYSTEM_PROMPT,
        "tools": ["fs_read", "@kirocrew-panel"],
        "allowedTools": ["fs_read", *agent_mod._MEMBER_PANEL_GRANTS],
        "mcpServers": {"kirocrew-panel": managed_mcp._managed_opt_in_entry("mcp-panel")},
    }
    auto_approve._apply_allowed_tools_ceiling(config, source="_install_dashboard_manager_agent")
    agent_mod.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    path = agent_mod.kiro_agents_dir_path() / _DASHBOARD_MANAGER_AGENT_FILENAME
    agent_mod._atomic_json_write(path, config)
    agent_mod.logger.info("Installed dashboard-manager agent config: %s", path)
