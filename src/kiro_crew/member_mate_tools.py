"""The crewmate's own-record tools (``kirocrew-mate``) on a crewmate's spec.

Two questions about that one server, answered in one place:

* :func:`spec_has_mate_tools` / :func:`binding_has_mate_tools` -- whether a
  crewmate's sessions load the server, so the text Crew writes for it (its
  identity, its first welcome, the goal nudges) names the tools only when they
  are there, and points the user at the Goals tab when they are not.
* :func:`grant_mate_tools_to_new_member` -- the creation-time outcome for the
  server. A crewmate on its own file gets ``kirocrew-mate`` from the gateway
  (its conductor and its worker base both declare it), so the grant writes
  nothing to that file and answers ``platform_provided``. A platform server is
  never written into a crewmate's file by creation; an entry that names one
  with no transport is dropped wherever the file is written or read
  (:func:`kiro_crew.mate_worker.drop_partial_platform_servers`).

Blocking IO throughout; callers run it off the event loop.

The same spec read answers one question about the crewmate's other tools:
:func:`spec_has_file_write` / :func:`binding_has_file_write`, whether its sessions
mount a built-in that writes files, so the briefing instruction does not tell a
crewmate on a no-write template (the conductors) to keep a file it cannot write.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.platform.tool_names import (
    KIRO_TOOL_ALIASES,
    expand_kiro_cli_tool_exclusions,
    expand_kiro_cli_tool_names,
)

logger = logging.getLogger(__name__)

MATE_SERVER = "kirocrew-mate"
_MATE_REF = f"@{MATE_SERVER}"
# A spec file stem: no separator, no leading dot, so it cannot leave its directory.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")

# Outcomes of :func:`grant_mate_tools_to_new_member`; none of them writes anything.
GRANTED = "granted"
TEMPLATE_HAS_IT = "template_has_it"
TEMPLATE_MUTES_IT = "template_mutes_it"
DEFAULT_TEMPLATE = "default_template"
NOT_A_MEMBER = "not_a_member"
ALREADY_ENROLLED = "already_enrolled"
POLICY_WITHHOLDS = "policy_withholds"
SERVER_UNAVAILABLE = "server_unavailable"
BACKEND_UNSUPPORTED = "backend_unsupported"
MATE_FILE_MISSING = "mate_file_missing"
PLATFORM_PROVIDED = "platform_provided"
FAILED = "failed"


def _referenced(tools: Any) -> bool:
    """Whether a ``tools`` list mounts the server: its ref, one of its tools, or ``*``."""
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(t, str) and (t in ("*", _MATE_REF) or t.startswith(_MATE_REF + "/"))
        for t in tools
    )


def _server_entry(spec: Any) -> Any:
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    return servers.get(MATE_SERVER) if isinstance(servers, dict) else None


def spec_has_mate_tools(spec: Any) -> bool:
    """Whether *spec* launches ``kirocrew-mate`` and mounts its tools."""
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    entry = _server_entry(spec)
    if not isinstance(entry, dict) or mcp_entry_is_muted(entry):
        return False
    return _referenced(spec.get("tools"))


def _read_template(name: str, workspace: str) -> dict | None:
    """The spec file named *name* a session in *workspace* runs, project scope first.

    Only the exact ``<name>.json`` / ``<name>.md`` candidates are read, never a
    scan for a declared ``name``: this answers a wording question and must not
    resolve a different template than the session's own reader would. A
    candidate that exists but cannot be read answers ``None`` rather than
    falling through to another scope.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.config.loader import default_project_dir
    from kiro_crew.config.paths import project_agents_dir

    if not _SAFE_NAME.fullmatch(name):
        return None
    project = default_project_dir(workspace)
    roots = [project_agents_dir(project)] if project else []
    roots.append(agent_mod.kiro_agents_dir_path())
    for root in roots:
        for suffix in (".json", ".md"):
            path = root / f"{name}{suffix}"
            if path.is_file() or path.is_symlink():
                if not agent_mod._spec_path_is_safe(path, root):
                    return None
                return agent_mod._read_spec_capped(path)
    return None


def binding_has_mate_tools(binding: Any) -> bool:
    """Whether the crewmate *binding* (a ``config.agents`` row) has the mate tools.

    Read from the spec its sessions run (its private copy once enrolled). A spec
    that cannot be read answers True, keeping the wording every crewmate had
    before this check existed: the question only chooses text.
    """
    name = getattr(binding, "kiro_agent", "")
    if not isinstance(name, str) or not name:
        return True
    try:
        spec = _read_template(name, getattr(binding, "workspace", "") or "")
    except Exception:
        logger.debug("spec for %r unreadable; keeping the mate wording", name, exc_info=True)
        return True
    return True if spec is None else spec_has_mate_tools(spec)


def member_has_mate_tools(member: str) -> bool:
    """:func:`binding_has_mate_tools` for the configured crewmate *member*."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        binding = KiroCrewConfig.load().agents.get(member)
    except Exception:
        logger.debug("config unreadable; keeping the mate wording", exc_info=True)
        return True
    return True if binding is None else binding_has_mate_tools(binding)


#: The built-ins that write a file, in kiro-cli's names: governance's write class
#: (``platform.governance.BUILTIN_TOOL_SCOPES`` files ``code`` there beside
#: ``fs_write``, since it edits files as well as shelling out). The shell is not
#: one: a template that mounts it with no writer (``kirocrew-conductor``) mounts it
#: for named scripts, and text that sent a crewmate there to keep a file would talk
#: it past that template's no-write property.
_FILE_WRITE_TOOLS = frozenset({"fs_write", "code"})
#: kiro-cli's short names for those tools, onto the name above (``write``), taken
#: from the shared alias table so a new spelling of a writer counts here too.
#: ``test_member_briefing_file_tools`` joins ``_FILE_WRITE_TOOLS`` with governance,
#: so a writer added there fails the test instead of reading as no writer here.
_FILE_WRITE_ALIASES: dict[str, str] = {
    alias: name for alias, name in KIRO_TOOL_ALIASES.items() if name in _FILE_WRITE_TOOLS
}
#: ``tools`` entries that mount every built-in, the file writers with them.
_ALL_BUILTINS = frozenset({"*", "@builtin"})


def _named_tools(value: Any) -> list[str]:
    """The string entries of a ``tools``-shaped list, a writer's alias folded onto it."""
    if not isinstance(value, list):
        return []
    return [_FILE_WRITE_ALIASES.get(t, t) for t in value if isinstance(t, str)]


def spec_has_file_write(spec: Any) -> bool:
    """Whether *spec* mounts a built-in that writes files.

    Read the way :func:`kiro_crew.agent_sdk.tool_search.spec_grants_tool_search`
    reads a spec: ``"*"`` (the whole ``tools`` value or an entry) and ``@builtin``
    mount every built-in, otherwise a writer must be named, and ``excludedTools`` is
    subtracted afterwards, where only a listed ``"*"`` removes everything. A name
    mounts, and an exclusion withholds, what the shared KAS readings say it does
    (``platform.tool_names``): ``fs_write`` brings ``str_replace`` and ``fs_append``,
    so a spec written in KAS ids counts too. An absent or malformed ``tools`` mounts
    nothing, which is what the KAS projection sends.
    """
    if not isinstance(spec, dict):
        return False
    excluded = _named_tools(spec.get("excludedTools"))
    if "*" in excluded:
        return False
    writers = set(expand_kiro_cli_tool_names(sorted(_FILE_WRITE_TOOLS)))
    tools = spec.get("tools")
    entries = ["*"] if tools == "*" else _named_tools(tools)
    mounted = (
        writers if _ALL_BUILTINS.intersection(entries) else set(expand_kiro_cli_tool_names(entries))
    )
    return bool((mounted & writers) - set(expand_kiro_cli_tool_exclusions(excluded)))


def binding_has_file_write(binding: Any) -> bool:
    """Whether the crewmate *binding*'s sessions mount a built-in that writes files.

    Read through the resolver :func:`binding_has_mate_tools` uses, from the spec
    the sessions run: a row that binds no template runs the default one, as
    :func:`kiro_crew.config.loader.member_template_id` says. A spec that cannot be
    read answers True, keeping the briefing wording every crewmate had before this
    check existed.
    """
    name = getattr(binding, "kiro_agent", "")
    if not isinstance(name, str):
        return True
    name = name or Path(AGENT_FILENAME).stem
    try:
        spec = _read_template(name, getattr(binding, "workspace", "") or "")
    except Exception:
        logger.debug("spec for %r unreadable; keeping the file-tools wording", name, exc_info=True)
        return True
    return True if spec is None else spec_has_file_write(spec)


def grant_mate_tools_to_new_member(
    member: str,
    *,
    catalog: Callable[[str], dict[str, str]] | None = None,
) -> str:
    """The mate-tools outcome for the just-created crewmate *member*. Never raises.

    ``platform_provided`` for a crewmate on its own file: the gateway supplies
    the server, so nothing is written. ``mate_file_missing`` when the eager copy
    did not land, ``default_template`` and ``not_a_member`` as named.
    """
    try:
        return _grant(member)
    except Exception:
        logger.warning("mate tools were not added to new crewmate %r", member, exc_info=True)
        return FAILED


def _grant(member: str) -> str:
    """The outcome for crewmate *member*; writes nothing to its own file."""
    from kiro_crew.agent_files import AGENT_FILENAME
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
    from kiro_crew.mate_config import has_mate_config

    _invalidate_config_cache()
    binding = KiroCrewConfig.load().agents.get(member)
    if binding is None or member == "default":
        return NOT_A_MEMBER
    if binding.kiro_agent == Path(AGENT_FILENAME).stem:
        return DEFAULT_TEMPLATE
    # The eager copy must have run first and repointed the binding at the mate
    # id. If it did not (its best-effort copy failed), there is no owned file to
    # edit; leave the shared pointer as-is rather than minting one here.
    if binding.kiro_agent != member or not has_mate_config(member):
        return MATE_FILE_MISSING
    # A crewmate on its own file chats on the conductor, which the gateway
    # builds with ``kirocrew-mate`` whatever the template carried, and its file
    # only extends the worker, whose base declares the server and always wins.
    # So nothing is written: an entry here could only be an override with no
    # transport, which a session that ran the file would wait on and never start.
    return PLATFORM_PROVIDED
