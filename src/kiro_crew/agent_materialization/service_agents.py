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
from kiro_crew.agent_files import GUEST_AGENT_FILENAME as _GUEST_AGENT_FILENAME
from kiro_crew.agent_files import KNOWLEDGE_AGENT_FILENAME as _KNOWLEDGE_AGENT_FILENAME
from kiro_crew.agent_files import LITE_AGENT_FILENAME as _LITE_AGENT_FILENAME
from kiro_crew.agent_files import RESEARCH_AGENT_FILENAME as _RESEARCH_AGENT_FILENAME


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


def _install_research_agent() -> None:
    """Generate and install the kirocrew-research agent config.

    Derives from the kirocrew agent (MCP servers, security, tools) but swaps in a
    lean research-worker prompt + identity. Used by the Research Lab app's
    autonudge loop to run one research cycle per turn.
    """
    config = agent_mod.build_agent_config()
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
