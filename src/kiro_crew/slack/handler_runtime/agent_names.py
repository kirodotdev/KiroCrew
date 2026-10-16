"""Which kiro agent a native Slack thread can name.

The project agents a working tree defines (``.kiro/agents/`` plus Slack's own legacy
``*.agent-spec.json``), the resolution of a typed name against those, the user-level
kiro specs, kiro-cli's built-in agents and the companion-plugin agents, and the full
list the ``!agent`` / ``!ta`` commands print. Each read goes through the hardened
spec readers in ``kiro_crew.agent_discovery``; the callers run them off the loop. The
companion-plugin walk itself, ``_iter_cc_agent_names``, is the facade's.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        KIRO_CLI_BUILTIN_AGENTS,
        SensitiveAgentSpecPathError,
        _iter_cc_agent_names,
        agent_spec_stems,
        is_markdown_spec,
        iter_agent_spec_files,
        kiro_agents_dir,
        project_agent_files,
        project_agent_name,
        read_agent_spec_strict,
    )


def _discover_project_agents(
    project_dir: str | None, *, operation: str = "slack_project_agents"
) -> list[Path]:
    """Return agent JSON files from <project_dir>/.kiro/ and .kiro/agents/.

    Delegates to :func:`agent_discovery.project_agent_files`, the one implementation
    now shared with the dashboard picker, ``spawn_run`` validation and per-turn agent
    resolution. ``include_legacy=True`` is passed HERE and only here: Slack's
    ``*.agent-spec.json`` convention predates ``.kiro/agents/`` and is kept for
    continuity, but kiro-cli cannot activate such a name, so no dispatch surface may
    offer it.

    *operation* names the Slack request whose scan this is, so a sensitive-project-dir
    denial is attributed to the listing or the name resolution rather than to this
    shared helper. The channel is fixed: every route here is Slack.
    """
    return project_agent_files(
        project_dir, include_legacy=True, operation=operation, source="slack"
    )


def _resolve_agent_name(name: str, project_dir: str | None = None) -> str | None:
    """Resolve an agent name to its internal name via suffix matching.

    Searches project-local .kiro/ first (if project_dir set), then ~/.kiro/agents/.
    Returns the resolved name, or None if not found.
    """
    # Project-local agents take priority — kiro-cli resolves --agent against its
    # cwd before the user-level dir, so a project agent is the one that would run.
    # Prefilter on the FILENAME first: the async callers hand this to a thread,
    # but reading every spec to compare its declared name would still make a
    # checkout with many agents or slow storage slow to answer. At most the one
    # matching file is read, to return the name it declares.
    for spec in _discover_project_agents(project_dir, operation="slack_resolve_agent"):
        stem = spec.stem.removesuffix(".agent-spec")
        if stem != name and spec.stem != name:
            continue
        return project_agent_name(spec)

    agents_dir = kiro_agents_dir()
    specs = (
        sorted(iter_agent_spec_files(agents_dir), key=lambda f: (len(f.stem), f.stem))
        if agents_dir.is_dir()
        else []
    )
    match = next(
        (f for f in specs if f.stem == name or f.stem.endswith(f"-{name}")),
        None,
    )
    if not match:
        # kiro-cli's own agents ship inside its binary, so no spec file names
        # them; they resolve as themselves (see KIRO_CLI_BUILTIN_AGENTS).
        if name in KIRO_CLI_BUILTIN_AGENTS:
            return name
        # Fallback: search companion-backend cc-plugins agents
        cc_match = _resolve_cc_agent_name(name)
        return cc_match
    try:
        # The hardened reader resolves the path, vets the target and opens it
        # with no reparse in ONE step, so a symlink swapped in between a check
        # and the read is refused rather than followed.
        data = read_agent_spec_strict(match, operation="slack_resolve_agent", source="slack")
    except SensitiveAgentSpecPathError:
        # A file whose target the path gate refuses is no agent at all, as it
        # was when the path check ran here.
        return None
    except (ValueError, OSError):
        # ValueError covers bad JSON, bad frontmatter and a non-UTF-8 read. A
        # broken JSON spec still occupies its name, as it always has; a
        # markdown file that does not parse is not a spec at all (a README,
        # notes), the same rule the listing applies, so it does not resolve.
        return None if is_markdown_spec(match) else match.stem
    if not isinstance(data, dict):
        return None if is_markdown_spec(match) else match.stem
    declared = data.get("name")
    return declared if isinstance(declared, str) and declared else match.stem


def _resolve_cc_agent_name(name: str, cc_plugins_dir: Path | None = None) -> str | None:
    """Return *name* if a cc-plugins agent declares it, else None."""
    for agent_name in _iter_cc_agent_names(cc_plugins_dir):
        if agent_name == name:
            return agent_name
    return None


def _list_all_agent_names(cc_plugins_dir: Path | None = None) -> str:
    """Return a comma-separated list of all available agent names.

    Merges the ``~/.kiro/agents`` spec stems (see
    :func:`kiro_crew.agent_discovery.agent_spec_stems`) with the cc-plugins
    agents from :func:`_iter_cc_agent_names`. The internal ``kirocrew-lite``
    variant is hidden. Returns ``"(none found)"`` when empty. Reads every
    markdown candidate to decide whether it is a spec, so the async callers
    run it in a thread rather than on the event loop.

    Note: this listing is unioned across both agent sources, but *activation*
    is not. cc-plugins (companion-backend) agents only actually load when
    ``agent.provider=claude_code``; under the kiro-cli provider a ``!ta`` to a
    cc-plugins name resolves and is recorded, but the next kiro session looks
    for ``~/.kiro/agents/<name>.json`` and falls back if it is absent. Switch
    the provider to ``claude_code`` to run cc-plugins agents.
    """
    names: list[str] = []
    agents_dir = kiro_agents_dir()
    if agents_dir.is_dir():
        # Hide the internal kirocrew-lite variant from BOTH sources — a
        # ~/.kiro/agents/kirocrew-lite.json would otherwise leak into the list.
        names.extend(
            stem
            for stem in agent_spec_stems(agents_dir, operation="slack_list_agents", source="slack")
            if stem != "kirocrew-lite"
        )
    seen = set(names)
    for agent_name in _iter_cc_agent_names(cc_plugins_dir):
        if agent_name not in seen and agent_name != "kirocrew-lite":
            names.append(agent_name)
            seen.add(agent_name)
    return ", ".join(names) if names else "(none found)"
