"""A Bot's worker sessions opened as one of its owner-listed agent templates.

A Bot's chat runs on the conductor and hands its work to its WORKER
(:mod:`kiro_crew.mate_worker`). The owner may also list agent templates the Bot
may open a worker as (``agents.<bot>.worker_agents``). A ``session_create`` from
the Bot's chat that names one of them opens a *delegate worker*: that template's
own spec (prompt, ``tools``, ``allowedTools``, ``toolsSettings``, MCP servers) on
the Bot's workspace and memory store, attributed on its execution record
(``ExecutionContext.worker_agent``).

The stricter side wins, and neither side widens the other:

- the template keeps every limit of its own: a tool it does not list stays
  unavailable, an ``excludedTools`` entry stays excluded, a ``deniedCommands``
  entry stays denied;
- the Bot's limits are added on top (:func:`narrow_to_bot`): a pre-approval the
  Bot's own worker does not hold is dropped, so that call asks; the Bot's
  ``excludedTools`` and denied lists are added; an allow list (``allowedCommands``
  and the like) of a tool the Bot does not pre-approve whole keeps only the
  entries the Bot's own list also holds; the Bot's ``ask`` and ``deny``
  permission rules are appended;
- the session's approval mode is the Bot's Profile Permissions, seeded and
  re-applied as for its own worker (``dashboard.crewmate_approval``), and the
  global policy (approval-mode clamp, held limits, revocations) still decides
  every call.

The one addition is the work ledger (``kirocrew-work``, its brief and report
verbs), which every Bot worker carries so it can report on the item it is bound
to. An unlisted template is refused (``worker_agent_not_allowed``), never run as
the plain worker. With no list set, nothing here changes what a Bot opens.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How many templates one Bot may list, and how long a name may be.
WORKER_AGENTS_MAX = 32
WORKER_AGENT_NAME_MAX = 128

#: ``toolsSettings`` keys that GRANT (the call runs without asking): narrowed to
#: the Bot's own entries unless the Bot pre-approves the tool whole.
_GRANT_KEYS = ("allowedCommands", "allowedPaths", "trustedAgents", "allowedServices")
#: ``toolsSettings`` keys that DENY: the Bot's entries are added.
_DENY_KEYS = ("deniedCommands", "deniedPaths")


class WorkerAgentNotAllowed(ValueError):
    """The named template is not on the Bot's ``worker_agents`` list."""


def worker_agents_refusal(raw: Any) -> str | None:
    """Why *raw* is not a valid ``worker_agents`` value for the owner route, else ``None``."""
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT

    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        return "worker_agents must be a list of agent names"
    names = [a.strip() for a in raw]
    if len(names) > WORKER_AGENTS_MAX:
        return f"worker_agents lists at most {WORKER_AGENTS_MAX} agents"
    for name in names:
        if not name or len(name) > WORKER_AGENT_NAME_MAX:
            return f"each worker_agents entry is 1 to {WORKER_AGENT_NAME_MAX} characters"
        if name in (MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT):
            return f"{name} is a Bot's own service agent, not a template to delegate to"
    return None


def delegate_for(config: Any, bot_name: str, requested: str) -> str:
    """The template a Bot's ``session_create`` naming *requested* opens as, else ``""``.

    ``""`` keeps today's hand-off to the Bot's own worker: no name, the worker's
    own names, the Bot's own name, or a Bot with no ``worker_agents`` set.
    Raises :class:`WorkerAgentNotAllowed` when the Bot has a list and
    *requested* is not on it, so the call is refused rather than run as the
    plain worker. Another Bot's name is ``""`` too: the caller keeps its own
    peer resolution for it.
    """
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    record = config.agents.get(bot_name) if bot_name else None
    allowed = list(getattr(record, "worker_agents", None) or [])
    name = requested.strip()
    if not allowed or not name or name in (MATE_WORKER_AGENT, "kirocrew-worker", bot_name):
        return ""
    if name in allowed:
        return name
    if name in config.agents:
        return ""
    raise WorkerAgentNotAllowed(
        f"{name!r} is not one of this Bot's worker agents ({', '.join(allowed)}). "
        "Name one of those, or omit agent for the Bot's own worker. Only the owner "
        "can change the list, on the Bot's settings page."
    )


def delegate_for_caller(caller_execution: Any, requested: str) -> tuple[str, str]:
    """``(template, "")`` to open, ``("", "")`` for the Bot's own worker, or ``("", why)``.

    For a ``session_create`` from a Bot's conductor chat (the caller already
    hands off to its worker). The Bot comes from the caller's attested record.
    Blocking config read. An unreadable config refuses: the named template may
    or may not be listed, and running the plain worker instead is the silent
    fallback this list must never make.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.crewmate_approval import crewmate_name_for

    try:
        config = KiroCrewConfig.load()
        bot = crewmate_name_for(config, caller_execution)
    except Exception:  # noqa: BLE001 - see above
        logger.warning("worker agents: config unreadable", exc_info=True)
        return "", "this Bot's worker agents cannot be read right now; try again"
    try:
        return delegate_for(config, bot, requested), ""
    except WorkerAgentNotAllowed as exc:
        return "", str(exc)


def delegate_worker_agent(execution: Any) -> str:
    """The template *execution* runs as when it is a Bot's delegate worker, else ``""``."""
    worker_agent = str(getattr(execution, "worker_agent", "") or "")
    if (
        worker_agent
        and getattr(execution, "selection_kind", "") == "template"
        and getattr(execution, "template_id", "") == worker_agent
        and getattr(execution, "member_id", None)
    ):
        return worker_agent
    return ""


def is_bot_worker_execution(execution: Any) -> bool:
    """Whether *execution* is one of a Bot's workers: its own, or a delegate worker."""
    from kiro_crew.mate_worker import is_own_worker_execution

    return is_own_worker_execution(execution) or bool(delegate_worker_agent(execution))


def session_delegate(agent: str | None, session_key: str) -> bool:
    """Whether *session_key* is a delegate worker running *agent*. Blocking; never raises."""
    if not agent or not session_key:
        return False
    from kiro_crew.execution_context import read_session_execution

    try:
        return delegate_worker_agent(read_session_execution(session_key)) == agent
    except Exception:  # noqa: BLE001 - an unreadable record is not a delegate
        return False


def bound_worker_agent(worker_session_key: str | None) -> str:
    """The delegate template the ledger-bound *worker_session_key* runs as, else ``""``.

    Read from the session's attested execution record, never from the ledger, so
    the attribution cannot be written by a worker and needs no stored copy that a
    rebuild could lose. The ledger holds the folded key (``chat-X``), the record
    the transcript key (``dashboard:chat-X``); both are tried. Blocking; never raises.
    """
    from kiro_crew.execution_context import read_session_execution

    key = str(worker_session_key or "")
    for candidate in (key, f"dashboard:{key}") if key and ":" not in key else (key,):
        if not candidate:
            continue
        try:
            found = delegate_worker_agent(read_session_execution(candidate))
        except Exception:  # noqa: BLE001 - an unreadable record names no template
            found = ""
        if found:
            return found
    return ""


def _covered(ref: str, grants: list[str]) -> bool:
    """Whether the Bot's pre-approvals *grants* cover the tool reference *ref*."""
    for grant in grants:
        if grant == ref or grant == "*":
            return True
        if grant.startswith("@") and "/" not in grant and ref.startswith(grant + "/"):
            return True
        if "*" in grant and fnmatch.fnmatchcase(ref, grant):
            return True
    return False


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def narrow_to_bot(spec: dict[str, Any], bot_spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* (the template's) with the limits of *bot_spec* (the Bot's worker) added.

    Pure; returns a new dict. Never adds a tool, a server or a pre-approval the
    template did not have; see the module docstring for each rule.
    """
    from kiro_crew import mate_approval_revocations as revocations
    from kiro_crew.user_json import copy_json_document

    out = copy_json_document(spec)
    bot_grants = _strings(bot_spec.get("allowedTools"))
    bot_servers = bot_spec.get("mcpServers")
    if isinstance(bot_servers, dict):
        for server, entry in bot_servers.items():
            approve = entry.get("autoApprove") if isinstance(entry, dict) else None
            bot_grants.extend(f"@{server}/{t}" for t in _strings(approve))

    # Pre-approvals: only what the Bot's own worker also pre-approves.
    refs = _strings(out.get("allowedTools"))
    servers = out.get("mcpServers")
    if isinstance(servers, dict):
        for server, entry in servers.items():
            approve = entry.get("autoApprove") if isinstance(entry, dict) else None
            refs.extend(f"@{server}/{t}" for t in _strings(approve))
    out = revocations.strip(out, [r for r in refs if not _covered(r, bot_grants)])

    # Exclusions: the template's and the Bot's.
    excluded = _strings(out.get("excludedTools"))
    for ref in _strings(bot_spec.get("excludedTools")):
        if ref not in excluded:
            excluded.append(ref)
    if excluded:
        out["excludedTools"] = excluded

    # Per-tool settings: grants narrowed, denies added.
    settings = out.get("toolsSettings")
    settings = dict(settings) if isinstance(settings, dict) else {}
    bot_settings = bot_spec.get("toolsSettings")
    bot_settings = bot_settings if isinstance(bot_settings, dict) else {}
    for tool in set(settings) | set(bot_settings):
        mine = settings.get(tool)
        mine = dict(mine) if isinstance(mine, dict) else {}
        theirs = bot_settings.get(tool)
        theirs = theirs if isinstance(theirs, dict) else {}
        whole = _covered(str(tool), bot_grants)
        for key in _GRANT_KEYS:
            if key in mine and not whole:
                kept = (
                    [v for v in mine[key] if v in theirs.get(key, [])]
                    if isinstance(mine[key], list)
                    else []
                )
                mine[key] = kept
        for key in _DENY_KEYS:
            added = _strings(theirs.get(key))
            if added:
                mine[key] = list(dict.fromkeys([*_strings(mine.get(key)), *added]))
        if mine:
            settings[tool] = mine
    if settings:
        out["toolsSettings"] = settings

    # KAS permission rules: the Bot's asks and denies hold here too.
    bot_perms = bot_spec.get("permissions")
    bot_rules = bot_perms.get("rules") if isinstance(bot_perms, dict) else None
    stricter = [
        r
        for r in (bot_rules if isinstance(bot_rules, list) else [])
        if isinstance(r, dict) and r.get("effect") in ("ask", "deny")
    ]
    if stricter:
        perms = out.get("permissions")
        perms = dict(perms) if isinstance(perms, dict) else {}
        rules = list(perms.get("rules") or []) if isinstance(perms.get("rules"), list) else []
        rules.extend(r for r in stricter if r not in rules)
        perms["rules"] = rules
        out["permissions"] = perms
    return out


def with_work_ledger(spec: dict[str, Any], bot_spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* with the work ledger mounted, pre-approving only what the Bot's worker does."""
    from kiro_crew.agent_materialization.service_agents import _mount_work_ledger
    from kiro_crew.agent_parts.spec_grants import _WORKER_WORK_GRANTS

    grants = tuple(
        g for g in _WORKER_WORK_GRANTS if _covered(g, _strings(bot_spec.get("allowedTools")))
    )
    _mount_work_ledger(spec, grants)
    return spec


def bot_worker_spec(session_key: str, **host: bool) -> dict[str, Any]:
    """The spec the Bot behind *session_key* gives its own worker: the ceiling here.

    Raises :class:`~kiro_crew.mate_worker.WorkerKitUnreadable` when it cannot be
    built, so a delegate is refused rather than run without its Bot's limits.
    *host* is ``worker_session_spec``'s host flags: a host that cannot carry
    the Bot's limits refuses the delegate exactly as it refuses the worker.
    """
    from kiro_crew.agent_sdk import agent_specs
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.mate_worker import MATE_WORKER_AGENT, WorkerKitUnreadable, worker_session_spec

    try:
        base = agent_specs.load_agent_spec(paths_mod.mate_agents_dir(), MATE_WORKER_AGENT)
    except agent_specs.AgentSpecUnreadable as exc:
        raise WorkerKitUnreadable(
            f"delegate worker {session_key}: its Bot's worker base cannot be read, so "
            "the Bot's limits are unknown"
        ) from exc
    return worker_session_spec({**base, "prompt": ""}, session_key, **host)


def delegate_session_spec(
    spec: dict[str, Any], agent: str, session_key: str, **host: bool
) -> dict[str, Any]:
    """*spec* for *session_key* when it is a Bot's delegate worker on *agent*, else *spec*.

    Raises :class:`~kiro_crew.mate_worker.WorkerKitUnreadable` when the Bot's
    limits cannot be read (fail closed), and whatever :func:`bot_worker_spec`
    raises for a host that cannot carry them. Blocking.
    """
    if not session_delegate(agent, session_key):
        return spec
    bot_spec = bot_worker_spec(session_key, **host)
    return with_work_ledger(narrow_to_bot(spec, bot_spec), bot_spec)
