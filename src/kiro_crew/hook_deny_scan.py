"""Find the script hooks the shell deny set will refuse, before one fires.

``run_script_hook`` refuses a hook whose command the shell deny set rejects, and a
refused PreToolUse hook blocks every tool call it matches. That refusal happens at
fire time, so an upgrade that starts vetting hooks (or a session that moves to a
backend where Crew fires the agent spec's own hooks) can leave an agent unable to do
anything with no earlier sign. This module runs the same check
(:func:`kiro_crew.hooks.shell_deny_reason`) over the hooks on disk, for
``kirocrew doctor`` and for one warning at gateway start.

What it reports names the hook and the rule, never the hook command: a command line
can carry paths or tokens, and the refusal's first line already says which pattern
matched. Agent-spec hooks are run by Crew only on the backends in
``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``; kiro-cli runs them itself and does not vet
them, so the report says so rather than claiming every session is affected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many agent spec files one scan reads. The agents directory is the user's;
#: an unbounded walk would make doctor and the startup check cost what it holds.
MAX_SPEC_FILES = 500

#: The label for the dashboard Hooks page's own hooks.
HOOKS_PAGE = "Hooks page"


@dataclass(frozen=True)
class RefusedHook:
    """One hook the deny set will refuse: where it lives, when it fires, and why."""

    source: str
    event: str
    hook_name: str
    rule: str


def _rule(reason: str) -> str:
    """The refusal's first line, which names the matched pattern."""
    return reason.splitlines()[0].strip() if reason else ""


def _check(source: str, hooks: list) -> list[RefusedHook]:
    from kiro_crew.hooks import shell_deny_reason

    out: list[RefusedHook] = []
    for hook in hooks:
        command = getattr(hook, "command", "") or ""
        if not command or not getattr(hook, "enabled", True):
            continue
        reason = shell_deny_reason(command)
        if reason:
            name = getattr(hook, "name", "") or getattr(hook, "id", "")
            out.append(RefusedHook(source, getattr(hook, "event", ""), name, _rule(reason)))
    return out


def _spec_hooks(agents_dir: Path) -> list[tuple[str, list]]:
    """Each installed agent's script hooks, one entry per agent the roster lists.

    The roster (:func:`kiro_crew.agent_discovery.list_agents`) already reads both
    spec forms (``.json`` and ``.md``), names each agent by its declared name and
    drops a spec another one shadows, so each agent is read once, from the file it
    actually loads.
    """
    from kiro_crew.agent_discovery import list_agents, read_agent_spec_strict
    from kiro_crew.agent_sdk.spec_hooks import spec_script_hooks

    found: list[tuple[str, list]] = []
    try:
        agents = list_agents(agents_dir)
    except Exception:  # noqa: BLE001 - an unreadable roster has no hooks to report
        logger.debug("hook deny scan: could not list agents in %s", agents_dir, exc_info=True)
        return found
    for info in agents[:MAX_SPEC_FILES]:
        if not info.filename:
            continue
        path = agents_dir / info.filename
        try:
            spec = read_agent_spec_strict(path, operation="hook_deny_scan", source="doctor")
        except (OSError, ValueError):
            logger.debug("hook deny scan: could not read %s", path, exc_info=True)
            continue
        if isinstance(spec, dict) and spec.get("hooks"):
            hooks = spec_script_hooks(info.name, spec)
            if hooks:
                found.append((info.name, hooks))
    return found


def refused_hooks(agents_dir: Path | None = None) -> list[RefusedHook]:
    """Every saved hook the shell deny set refuses: Hooks-page hooks, then agent specs.

    *agents_dir* defaults to the kiro agents directory. Blocking: it reads files and
    runs the deny regexes. A source that cannot be read is skipped, so a scan never
    raises on the user's files.
    """
    from kiro_crew.hooks import persisted_hook_store

    out: list[RefusedHook] = []
    try:
        out.extend(_check(HOOKS_PAGE, persisted_hook_store().list_all()))
    except Exception:  # noqa: BLE001 - an unreadable store is reported elsewhere
        logger.debug("hook deny scan: Hooks-page hooks unreadable", exc_info=True)
    if agents_dir is None:
        from kiro_crew.config.paths import kiro_agents_dir

        agents_dir = kiro_agents_dir()
    for agent_id, hooks in _spec_hooks(agents_dir):
        # repr: the file stem is the user's, and a terminal must not act on it.
        out.extend(_check(f"agent {agent_id!r}", hooks))
    return out


def describe(hook: RefusedHook) -> str:
    """One line for doctor and the log: source, event, hook and the refusing rule."""
    return f"{hook.source}: {hook.event} hook {hook.hook_name!r} is refused ({hook.rule})"


def warn_refused_hooks() -> None:
    """Log one WARNING per hook the deny set will refuse. Never raises.

    Run once after the gateway is ready, off the loop.
    """
    try:
        found = refused_hooks()
    except Exception:  # noqa: BLE001 - a startup diagnostic must not fail the gateway
        logger.debug("hook deny scan failed", exc_info=True)
        return
    for hook in found:
        logger.warning(
            "%s. A refused PreToolUse hook blocks every tool call it matches; "
            "agent-spec hooks are refused only where Crew runs them (KAS, goose, "
            "opencode). Run 'kirocrew doctor' for the list.",
            describe(hook),
        )
