"""Read a Crew agent spec from application code without importing the ACP layer.

The crewmate modules (``mate_config``, ``mate_worker``, ``mate_worker_kit``,
``mate_capability_catalog``, ``mate_config_migration``, and the heartbeat's ``goal_reminder``) read
custom-agent specs the way a session would: the hardened reader in
``acp.kas_agents`` and the per-session MCP switch-off in ``acp.session_mcp``.
Application code may not import ``kiro_crew.acp``
(``scripts/check_agent_sdk_boundary.py``), so this module is the one seam they
reach those reads through.

It translates rather than re-exports: a spec the reader refuses raises
:class:`AgentSpecUnreadable` here, not the ACP layer's
``KasAgentTranslationError``, so a consumer holds no ACP class. Every ACP import
is made at call time, both because ``kiro_crew.acp`` imports back into
``agent_sdk`` at load time and so that a test patching the ACP function on its
own module is seen here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AgentSpecUnreadable(ValueError):
    """An agent spec exists in a form the reader refuses, or is missing.

    The message is the reader's own, so a caller can quote it unchanged.
    """


def load_agent_spec(agents_dir: Path, agent_id: str) -> dict[str, Any]:
    """*agent_id*'s spec in *agents_dir*, as a copy the caller owns. Blocking.

    The same read a KAS session runs (``acp.kas_agents.load_agent_spec``: a spec
    declaring ``name == agent_id`` wins over the file named for it). Raises
    :class:`AgentSpecUnreadable` when there is no readable spec.
    """
    from kiro_crew.acp import kas_agents

    try:
        return kas_agents.load_agent_spec(agents_dir, agent_id)
    except kas_agents.KasAgentTranslationError as exc:
        raise AgentSpecUnreadable(str(exc)) from exc


def resolve_prompt(spec: dict[str, Any], *, agent_id: str, agents_dir: Path) -> str:
    """*spec*'s prompt as literal text, reading a ``file://`` prompt. Blocking.

    ``acp.kas_agents.resolve_prompt``: a relative path is anchored to
    *agents_dir*, and a prompt-less spec answers the KAS fallback text. Raises
    :class:`AgentSpecUnreadable` when the prompt cannot be read.
    """
    from kiro_crew.acp import kas_agents

    try:
        return kas_agents.resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    except kas_agents.KasAgentTranslationError as exc:
        raise AgentSpecUnreadable(str(exc)) from exc


def agent_spec_absent(agents_dir: Path, agent_id: str) -> bool:
    """Whether *agents_dir* holds no spec at all for *agent_id*. Blocking.

    A spec that exists but cannot be read is NOT absent. Raises ``OSError`` when
    the directory cannot be scanned (``acp.kas_agents.agent_spec_absent``).
    """
    from kiro_crew.acp import kas_agents

    return kas_agents.agent_spec_absent(agents_dir, agent_id)


def session_mcp_server_is_disabled(name: str, agent: str | None) -> bool:
    """Whether MCP server *name* is switched off whole for a session as *agent*.

    ``acp.session_mcp.session_mcp_server_is_disabled``: blocking, and never
    raises (an unreadable source switches nothing off).
    """
    from kiro_crew.acp import session_mcp

    return session_mcp.session_mcp_server_is_disabled(name, agent)


def authored_spec_is_toolless(agent: str, work_dir: Any = None) -> bool:
    """Whether the spec *agent* resolves to in *work_dir* mounts nothing. Blocking.

    ``acp.client_mcp_array.authored_agent_spec_is_toolless``: project level
    first, then user level, judged by ``spec_is_toolless``. Fails closed.
    """
    from kiro_crew.acp import client_mcp_array

    return client_mcp_array.authored_agent_spec_is_toolless(agent, work_dir)


def authored_spec_toolless_verdict(agent: str, work_dir: Any = None) -> bool | None:
    """Like :func:`authored_spec_is_toolless`, but ``None`` when no spec exists. Blocking.

    ``False`` means a spec exists and it mounts something, or it exists and the
    reader will not answer for it: two specs declaring the name (ambiguous), a
    resolved spec the hardened reader refuses, or an ``<agent>.json`` /
    ``<agent>.md`` that is hardlinked, symlinked, oversized or unparsable. Each is
    still something the harness may load, so a caller refuses it before starting
    a session. ``None`` is kept for no file claiming the name, by filename or by
    declared name, in the user-level or project agents directory; that is left to
    the caller's post-start confirmation.
    """
    import os

    from kiro_crew.acp import client_mcp_array, session_mcp
    from kiro_crew.agent import agent_spec_path, kiro_agents_dir_path
    from kiro_crew.agent_spec_format import agent_spec_candidates
    from kiro_crew.config.paths import project_agents_dir

    spec = session_mcp.agent_spec_snapshot(agent, work_dir=work_dir)
    if spec is not None:
        return client_mcp_array.spec_is_toolless(spec)
    try:
        if agent_spec_path(agent) is not None:
            return False
    except ValueError:
        return False
    if session_mcp._project_spec_path_for(agent, work_dir) is not None:
        return False
    dirs = [kiro_agents_dir_path()]
    if work_dir:
        dirs.append(project_agents_dir(work_dir))
    present = any(
        os.path.lexists(candidate)
        for directory in dirs
        for candidate in agent_spec_candidates(directory, agent)
    )
    return False if present else None


#: ``tools`` entries that give the agent the shell (exactly, or with every built-in).
_SHELL_GRANTING_TOOLS = frozenset(
    {
        "*",
        "@builtin",
        "@builtin/*",
        "execute_bash",
        "shell",
        "@builtin/execute_bash",
        "@builtin/shell",
    }
)

#: The ``toolsSettings`` shell keys that only widen what runs without a prompt.
_UNRESTRICTING_SHELL_SETTINGS = frozenset({"allowedCommands", "autoAllowReadonly", "allowReadOnly"})


def shell_hook_match_names() -> tuple[str, ...]:
    """Every name a PreToolUse matcher can meet for a shell call, on any harness.

    Built from the tables, never a list of its own: the kiro-cli shell names
    (``kas_permissions.SPEC_HOOK_TOOL_TAGS["shell"]`` and their
    ``KIRO_TOOL_ALIASES``), Crew's shell titles and names
    (``tool_call_title.SHELL_TOOL_NAMES`` / ``SHELL_STUB_TITLES``), and for KAS's
    shell ids (``KAS_TOOL_IDS_BY_KIRO_TOOL``) and every harness table's
    (``harness_tool_names.HARNESS_TOOL_TABLES``) the names the hook dispatcher
    matches that call by (``hook_dispatch.pre_tool_match_names``: both the
    Hooks-page and the spec readings, kinds such as ``@builtin/<id>`` included).
    """
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_ID_SEPARATOR, HARNESS_TOOL_TABLES
    from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL, SPEC_HOOK_TOOL_TAGS
    from kiro_crew.agent_sdk.spec_hooks import spec_hook_kind_names
    from kiro_crew.hooks import pre_tool_match_names
    from kiro_crew.platform.tool_names import KIRO_TOOL_ALIASES
    from kiro_crew.tool_call_title import SHELL_STUB_TITLES, SHELL_TOOL_NAMES

    kiro = set(SPEC_HOOK_TOOL_TAGS["shell"])
    kiro |= {alias for alias, name in KIRO_TOOL_ALIASES.items() if name in kiro}
    names = [*sorted(kiro), *sorted(SHELL_TOOL_NAMES), *sorted(SHELL_STUB_TITLES)]
    harness_ids = [i for k in sorted(kiro) for i in KAS_TOOL_IDS_BY_KIRO_TOOL.get(k, ())]
    harness_ids += [
        f"{backend}{HARNESS_TOOL_ID_SEPARATOR}{tool_id}"
        for backend, table in sorted(HARNESS_TOOL_TABLES.items())
        for k in sorted(kiro)
        for tool_id in table.get(k, ())
    ]
    for kiro_name in sorted(kiro):
        for harness_id in harness_ids:
            every, spec = pre_tool_match_names(
                "", tool_identity=kiro_name, harness_tool_id=harness_id
            )
            names += [*every, *(spec or ())]
    # Kinds of the built-in names only: an ``@builtin/...`` reading is not an MCP one.
    names += spec_hook_kind_names([n for n in names if not n.startswith("@")], mcp_server="")
    return tuple(dict.fromkeys(names))


def hook_matcher_covers_shell(matcher: Any, extra_names: tuple[str, ...] = ()) -> bool:
    """Whether a PreToolUse hook *matcher* can cover a shell call. Fails closed.

    An absent, empty or non-string matcher covers every tool. Otherwise the
    matcher is compared, as a case-insensitive glob, with every shell name
    (:func:`shell_hook_match_names`) and *extra_names* (a call's titles).
    """
    import fnmatch

    if not isinstance(matcher, str) or not matcher.strip():
        return True
    pattern = matcher.strip().lower()
    names = (*shell_hook_match_names(), *extra_names)
    return pattern == "*" or any(fnmatch.fnmatchcase(n.lower(), pattern) for n in names)


def _names_shell(entry: Any) -> bool:
    """Whether a ``tools``/``excludedTools`` entry can stand for the shell."""
    if not isinstance(entry, str):
        return True
    return hook_matcher_covers_shell(entry) if entry.strip() else False


def _restricting_shell_settings(tools_settings: Any) -> str | None:
    from kiro_crew.platform.tools_settings_aliases import canonical_tools_settings

    if tools_settings in (None, {}):
        return None
    if not isinstance(tools_settings, dict):
        return "toolsSettings is not an object"
    canonical, _ = canonical_tools_settings(tools_settings)
    for key in ("shell", "execute_bash"):
        settings = canonical.get(key)
        if settings is None:
            continue
        if not isinstance(settings, dict):
            return f"toolsSettings.{key} is not an object"
        if settings.get("denyByDefault", False) is not False:
            return f"toolsSettings.{key}.denyByDefault"
        if settings.get("deniedCommands") not in (None, []):
            return f"toolsSettings.{key}.deniedCommands"
        other = sorted(str(k) for k in settings if k not in _UNRESTRICTING_SHELL_SETTINGS)
        if other:
            return f"toolsSettings.{key}.{other[0]}"
    return None


def _restricting_spec_hook(hooks: Any) -> str | None:
    from kiro_crew.agent import _event_for_hook_trigger

    if not hooks:
        return None
    if isinstance(hooks, dict):
        entries = hooks.get("preToolUse")
        if entries is None:
            return None
        if not isinstance(entries, list):
            return "hooks.preToolUse is not a list"
        for entry in entries:
            if not isinstance(entry, dict) or hook_matcher_covers_shell(entry.get("matcher")):
                return "hooks.preToolUse"
        return None
    if not isinstance(hooks, list):
        return "hooks is neither an object nor an array"
    for doc in hooks:
        if not isinstance(doc, dict):
            return "a hooks document is not an object"
        event = _event_for_hook_trigger(doc.get("trigger"))
        if event in (None, "preToolUse") and hook_matcher_covers_shell(doc.get("matcher")):
            return "a PreToolUse hooks document"
    return None


def _restricting_shape(spec: dict[str, Any]) -> str | None:
    """The first way *spec* restricts its own shell, or ``None`` when it does not."""
    from kiro_crew.acp.kas_permissions import authored_restricted_capabilities

    tools = spec.get("tools")
    if not isinstance(tools, list) or not any(
        isinstance(t, str) and t in _SHELL_GRANTING_TOOLS for t in tools
    ):
        return "its tools do not include the shell"
    excluded = spec.get("excludedTools")
    if excluded is not None and (
        not isinstance(excluded, list) or any(_names_shell(e) for e in excluded)
    ):
        return "excludedTools"
    shape = _restricting_shell_settings(spec.get("toolsSettings"))
    if shape is not None:
        return shape
    permissions = spec.get("permissions")
    if permissions is not None and "shell" in authored_restricted_capabilities(permissions, None):
        return "a permissions rule that denies or asks for the shell"
    return _restricting_spec_hook(spec.get("hooks"))


def spec_shell_restriction(agent: str, work_dir: Any = None) -> str | None:
    """How *agent*'s spec restricts its shell, or ``None`` for an unrestricted shell. Blocking.

    Every spec a session of *agent* could run is judged, and any restriction in any
    of them refuses: the one the direct client resolves its own allowlists from
    (``spec_tools_allowlists.session_allowlist_spec``, project-nearest), the one
    KAS is handed (``harness.kas.resolve_projected_spec``, user level first), and
    the checkout's own spec when it declares the agent. A short shape name is
    returned when no spec claims the agent, when ``tools`` does not name the shell
    (or ``excludedTools`` does), when the shell's ``toolsSettings`` sets
    ``denyByDefault``, ``deniedCommands`` or any key that is not only a grant, when
    a ``permissions`` rule denies or asks for the shell (meta capabilities and an
    unreadable block included), and when a ``hooks`` PreToolUse entry can match
    the shell. No matcher is re-implemented: any such shape refuses.

    Raises :class:`AgentSpecUnreadable` when a file claims the name but no spec
    reads from it (the by-name scan skips an unparsable file, so its bare filename
    is checked too), when any candidate's read fails (a stale derived spec, an
    ambiguous name, a user-level spec KAS cannot load), or when the checkout
    declares the agent in a spec that cannot be read.
    """
    from kiro_crew.acp.harness.kas import resolve_projected_spec
    from kiro_crew.acp.kas_agents import KasAgentSpecAbsent
    from kiro_crew.acp.session_mcp import project_agent_spec
    from kiro_crew.acp.spec_tools_allowlists import session_allowlist_spec
    from kiro_crew.agent import kiro_agents_dir_path, session_agent_spec_path
    from kiro_crew.agent_spec_format import agent_spec_candidates
    from kiro_crew.config.paths import spec_dir_for

    if not agent:
        return "no agent is named"
    try:
        mirrored = session_allowlist_spec(agent, work_dir)
        declared, project = project_agent_spec(agent, work_dir)
        try:
            projected: Any = resolve_projected_spec(kiro_agents_dir_path(), agent, work_dir)[0]
        except KasAgentSpecAbsent:
            projected = None
        claimed = bool(
            declared
            or session_agent_spec_path(agent) is not None
            or any(
                p.is_file()
                for p in agent_spec_candidates(spec_dir_for(agent, kiro_agents_dir_path()), agent)
            )
        )
    except Exception as exc:  # noqa: BLE001 -- any failed read leaves the spec unread
        raise AgentSpecUnreadable(str(exc) or type(exc).__name__) from exc
    if declared and project is None:
        raise AgentSpecUnreadable(f"the project spec for agent {agent!r} cannot be read")
    candidates = [s for s in (mirrored, projected, project) if isinstance(s, dict)]
    if not candidates:
        if claimed:
            raise AgentSpecUnreadable(f"the spec for agent {agent!r} exists but cannot be read")
        return "no spec is found for it"
    for spec in candidates:
        shape = _restricting_shape(spec)
        if shape is not None:
            return shape
    return None


def sweep_kas_launch_snapshots(boot_time: float) -> bool:
    """Remove the KAS MCP launch snapshots last written a week before *boot_time*.

    ``acp.kas_env_launch.sweep_stale``: the gateway's boot path starts it once,
    on a background task, with the time it took after the previous run's agent
    processes were ended. Blocking file I/O.

    Skipped, and ``False`` returned, while ``kiro_session_pids.txt`` still names
    another owner process: every KAS runtime records itself there under the pid
    of the process that started it (a gateway or a ``kirocrew chat`` CLI on the
    same data home), and ``cleanup_orphaned_sessions`` has already removed every
    entry it could confirm dead, so an owner left there may still run a session
    whose launcher reads a snapshot this sweep would delete. This process's own
    entries do not count. An unreadable ledger raises, so the caller skips too.
    """
    import os

    from kiro_crew.acp import kas_env_launch
    from kiro_crew.session_pid import retained_gateway_pids

    others = retained_gateway_pids() - {os.getpid()}
    if others:
        import logging

        logging.getLogger(__name__).info(
            "KAS launch snapshot sweep skipped: %d other owner process(es) still hold "
            "agent runtimes on this data home",
            len(others),
        )
        return False
    kas_env_launch.sweep_stale(boot_time)
    return True


def session_spec_grants(agent: str) -> set[str] | None:
    """The ``allowedTools`` entries and governed ``autoApprove`` refs of *agent*'s
    user-level spec, or ``None`` when none reads. Blocking.

    A project checkout's spec is never read: a cloned repository could plant one
    that grants what the session's own spec withholds."""
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.kas_auto_approve import auto_approve_refs

    spec = session_mcp.agent_spec_snapshot(agent, work_dir=None)
    if not isinstance(spec, dict):
        return None
    allowed = spec.get("allowedTools")
    listed = allowed if isinstance(allowed, list) else []
    return {e for e in listed if isinstance(e, str)} | set(auto_approve_refs(spec))


def deferred_call_arguments(params: Any, server: str, tool: str) -> tuple[Any, bool]:
    """``(arguments the server receives, readable)`` for a permission request's input.

    A KAS Tool Search call arrives as ``{"tool_id": "<server>::<tool>", "arguments":
    {...}}`` (``_meta`` beside them allowed); the server gets only the inner
    arguments. The permission gate's own reading, so callers judge the same thing.
    """
    from kiro_crew.acp._dispatch import _workspace_call_arguments

    return _workspace_call_arguments(params, server, tool)
