"""Kiro agent spec -> the ACP ``session/new`` ``mcpServers`` array.

For a harness in :data:`~kiro_crew.acp_backends.ACP_BACKENDS_SESSION_MCP_ARRAY`,
the ``session/new`` / ``session/load`` ``mcpServers`` parameter is where MCP
servers come from and the only place: claude-agent-acp, its one member today,
does not read ``~/.kiro/agents/<name>.json``. kiro-cli reaches the same servers
through ``--agent``, which is why that backend passes no array at all. Without
the translation here such a session runs with ZERO Kiro Crew tools -- the harness
itself works (prompts, streaming, permissions) but ``send_message``,
``spawn_run``, ``cron_add`` and every user-installed server are simply absent.

Nothing here is Anthropic-specific by design: the module is keyed on the
capability, not on the harness, so the next adapter that reads no agent spec
joins the set rather than growing a second translator.

The agent spec stays the single source of truth; there is no second,
claude-shaped registry to keep in sync. It is read per spawn, so installing or
toggling an MCP server takes effect on the NEXT session with no gateway restart.
Nothing here raises: a missing or malformed spec degrades to Crew's own control
plane, never to a failed spawn.

Shape notes -- these are claude-agent-acp's zod schema rather than anything in
the ACP spec at large:

* ``env`` (stdio) and ``headers`` (http/sse) are REQUIRED arrays of
  ``{"name", "value"}`` objects. Omitting either fails ``session/new`` outright
  with ``-32602 Invalid params (expected array, received undefined)``, so they
  are always emitted -- empty when there is nothing to carry.
* A url-bearing entry is routed by ``type``. Without one the adapter takes the
  stdio branch and rejects the entry for having no ``command``, so the transport
  is always spelled out.
* kiro-cli-only keys (``timeout``, ``disabledTools``, ``autoApprove``) cannot
  ride along in an element. ``disabledTools`` is a RESTRICTION, so dropping it
  outright would widen the tool surface; it comes back as a
  ``permissions.deny`` rule instead (see :func:`session_mcp_deny_rules`).
  ``autoApprove`` is dropped deliberately, not for want of a mapping:
  Claude's nearest equivalent is a ``permissions.allow`` entry, and a
  pre-approved tool is one Claude never asks about -- so the call never reaches
  the host ``canUseTool`` gate that carries the deny floor, the sensitive-path
  check and the governance ceiling. Every MCP call on this backend is gated.

**The governing rule, stated once, because three review rounds each rediscovered
one corner of it: this module matches kiro-cli, and deviating in EITHER direction
is the defect.** Granting what kiro-cli would drop widens the session's tool
surface behind the user's back; withholding what kiro-cli would keep removes
capability from a session with no error to explain it. Two consequences that are
otherwise easy to argue backwards:

* **Which specs are resolved.** kiro-cli resolves ``--agent`` against the project
  checkout's ``.kiro/agents/*.json`` as well as ``~/.kiro/agents/``, so this
  module must too, project-nearest first. Resolving only the user level looked
  conservative and was the opposite: a project-only agent found no spec, so its
  ``tools`` allowlist never ran and the control plane mounted unrestricted --
  a user-declared restriction dropped silently. See :func:`_agent_spec_for`.
* **What the registry ceiling governs.** Registry mode withholds every
  SPEC-DECLARED server, because nothing here can resolve a marker against the
  admin's catalog. It does NOT withhold Crew's own control plane, which is
  re-derived from the managed source rather than read from the spec. That is
  kiro-cli parity, not an exemption: ``agent._install_agent_spec`` stamps the
  managed servers with ``"type": "registry"`` precisely so the client keeps them
  under registry mode, and ``agent._mcp_registry_mode`` records their
  disappearance as the defect ("the features they carry (``spawn_run``,
  ``cron_add``, ``learn_add``, ...) disappear with no local error"). Withholding
  them HERE would make this backend stricter than kiro-cli and reproduce that
  failure on the installs least able to diagnose it. The control plane is still
  subject to the ``tools`` allowlist, which is the restriction that does apply.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any

from kiro_crew.agent import (
    _mcp_registry_mode,
    agent_spec_path,
    ensure_agent_materialized,
    managed_mcp_spec_entry,
)
from kiro_crew.agent_discovery import _read_agent_spec, project_agent_files, project_agent_name

logger = logging.getLogger(__name__)

# Crew's own control plane. Re-derived from the managed source of truth on every
# spawn so a stale hand-edited command in the spec cannot cost a claude session
# the tools it needs to report back to its channel at all. Both are always-on
# (no gate, not opt_in), so ``managed_mcp_spec_entry`` returns them unless the
# install is broken. Re-derived, not read from the spec, is also what keeps them
# out of the registry filter below: they are the host's own process, not a
# third-party server the admin's catalog governs.
_CONTROL_PLANE_SERVERS = ("kirocrew-core", "kirocrew-cron")

# kiro-cli's enterprise-governance discriminator, mirrored rather than imported
# (``agent._MCP_REGISTRY_TYPE`` is private; a ratchet test pins the two equal).
_KIRO_REGISTRY_TYPE = "registry"

# ``tools`` entries that grant every MCP server rather than naming one. Only the
# bare ``*`` -- kiro's configuration reference documents ``*``, ``@builtin``,
# ``@server`` and ``@server/tool`` for ``tools`` and reserves globs for
# ``allowedTools``, and this repo's own reader (``connections.tool_aliases``)
# parses ``@*`` as a server LITERALLY named ``*``. Treating ``@*`` as grant-all
# here would mount every declared server on this backend while kiro-cli mounted
# none of them.
_GRANT_ALL_TOOL_REFS = frozenset({"*"})


def _acp_pairs(raw: Any) -> list[dict[str, str]]:
    """A kiro-agent-JSON ``env``/``headers`` mapping in ACP's array-of-pairs form.

    Values are stringified because the adapter's schema types them as strings
    while the agent spec is hand-editable JSON, where a port number or a boolean
    is an easy thing to write.
    """
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def acp_server_element(name: str, spec: Any) -> dict[str, Any] | None:
    """One ``mcpServers`` entry as a claude-agent-acp array element.

    ``None`` when the entry declares no usable transport -- neither a ``url`` nor
    a ``command``. Skipping is the right outcome there: an element the adapter
    rejects fails the whole ``session/new``, taking every other server with it.
    """
    if not isinstance(spec, dict):
        return None
    url = spec.get("url")
    if isinstance(url, str) and url:
        # Only ``sse`` is distinguished; anything else (including a missing
        # ``type``) is streamable HTTP, which is the adapter's own default and
        # the shape every modern remote server speaks.
        stype = "sse" if spec.get("type") == "sse" else "http"
        return {
            "name": name,
            "type": stype,
            "url": url,
            "headers": _acp_pairs(spec.get("headers")),
        }
    command = spec.get("command")
    if not isinstance(command, str) or not command:
        logger.debug("session MCP: skipping %r -- entry declares no command and no url", name)
        return None
    # Only a sequence is iterated. The spec is hand-editable JSON, so ``"args":
    # 8080`` or ``"args": "--flag"`` is an easy thing to write -- and iterating a
    # number raises ``TypeError`` while iterating a string would explode it into
    # one argument per character. Nothing in this module may raise: the exception
    # would travel out through ``session_mcp_servers`` and fail the whole
    # ``session/new``, costing the session every OTHER server as well.
    raw_args = spec.get("args")
    args = [
        a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
        for a in (raw_args if isinstance(raw_args, (list, tuple)) else ())
    ]
    if raw_args and not isinstance(raw_args, (list, tuple)):
        logger.warning(
            "session MCP: %r declares a non-list args (%s); launching it with none",
            name,
            type(raw_args).__name__,
        )
    return {
        "name": name,
        "command": command,
        "args": args,
        "env": _acp_pairs(spec.get("env")),
        "type": "stdio",
    }


def _tools_grant(tools: list[Any], name: str) -> bool:
    """True when a spec's ``tools`` list mounts MCP server *name*.

    kiro-cli loads a server only when ``tools`` references it (``@server`` or
    ``@server/tool``), so an ``mcpServers`` entry with no reference is declared
    but never mounted. The claude array has no such indirection -- everything in
    it is mounted -- so the reference is applied here instead. Without this, an
    entry the user deliberately left unreferenced (the shape every ``opt_in``
    grant uses, and what a narrowed-by-hand spec looks like) would come alive the
    moment the session happened to run on claude.
    """
    ref = f"@{name}"
    prefix = f"{ref}/"
    for item in tools:
        if not isinstance(item, str):
            continue
        if item in _GRANT_ALL_TOOL_REFS or item == ref or item.startswith(prefix):
            return True
    return False


def _project_spec_path_for(agent: str, work_dir: str | Path | None) -> Path | None:
    """The project checkout's spec for *agent*, or ``None``.

    ``<work_dir>/.kiro/agents/*.json`` is the only project location kiro-cli
    itself resolves ``--agent`` against, so it is the only one whose names are
    dispatchable and therefore the only one this module honours.
    ``project_agent_files`` already refuses a sensitive project root and
    ``project_agent_name`` applies the same declared-name-beats-filename order
    kiro-cli lists by, so neither rule is restated here.

    Never raises: an unreadable checkout resolves to no project spec rather than
    failing the spawn.
    """
    if not work_dir:
        return None
    try:
        for spec in project_agent_files(work_dir):
            if project_agent_name(spec) == agent:
                return spec
    except OSError:
        logger.debug("session MCP: could not scan %s for project agents", work_dir, exc_info=True)
    return None


def _agent_spec_for(agent: str, work_dir: str | Path | None = None) -> dict[str, Any] | None:
    """The materialized kiro spec for *agent*, or ``None`` when unreadable.

    **Project-nearest first.** kiro-cli resolves ``--agent`` against the project
    checkout as well as the user level, so a project-only agent must not read as
    "no spec": that dropped its ``tools`` allowlist and mounted the control plane
    unrestricted, which is a user-declared restriction lost rather than a default
    applied. The project spec therefore wins when both declare the name, the way
    a nearer config layer normally does.

    Materializes first: a source checkout that skipped setup has no spec on disk
    at all, and the claude spawn path -- unlike kiro-cli's ``--agent`` one -- has
    no other reason to write it. Best-effort and never raises.

    Reads through ``agent_discovery._read_agent_spec``, the module's documented
    ONE reader, rather than parsing the file here: the agents directory is
    user-writable and shared with other tools, so the guards it applies are the
    point -- a symlink whose resolved target is sensitive
    (``kirocrew.json -> ~/.aws/credentials``) is refused and audited, an oversized
    file is refused at the size cap instead of being read into memory during a
    spawn, and non-UTF-8 bytes or non-object JSON come back as ``None``. The
    labels name THIS surface so a refusal is attributed to the session-MCP
    translation rather than to an unrelated agent listing (#6722); ``source`` is
    ``"unknown"`` because a session is started from every channel Crew has.
    """
    ensure_agent_materialized(agent)
    project = _project_spec_path_for(agent, work_dir)
    if project is not None:
        return _read_agent_spec(project, operation="session_mcp_project_agent", source="unknown")
    try:
        path = agent_spec_path(agent)
    except ValueError:
        # Two specs declare this name, so which one is live is undefined. No
        # answer is the honest one; the control plane still loads below.
        logger.warning("session MCP: ambiguous agent spec for %r", agent, exc_info=True)
        return None
    if path is None:
        logger.info(
            "session MCP: no spec on disk for agent %r; loading Crew's control plane only", agent
        )
        return None
    return _read_agent_spec(path, operation="session_mcp_servers", source="unknown")


def session_mcp_deny_rules(agent: str | None, *, work_dir: str | Path | None = None) -> list[str]:
    """Claude ``permissions.deny`` rules re-applying the spec's per-TOOL narrowing.

    ``disabledTools`` is a kiro-cli-only key, so it cannot ride along in the
    array element -- but it is a RESTRICTION, and dropping a restriction while
    forwarding the server that carries it widens the session's tool surface
    behind the user's back. The dashboard writes that key when someone turns an
    individual tool off, and the repo already treats losing it as a defect
    elsewhere ("dropping ``disabledTools`` on a save would silently widen the
    agent's tool surface"). Claude has no per-server allowlist, but it does have
    ``permissions.deny``, which is evaluated ahead of every allow rule and of the
    host callback, so the disabled tool is refused rather than merely asked
    about.

    Returned as rules for the settings writer rather than applied here: this
    module owns the array, ``settings.local.json`` belongs to the client. Ordered
    and de-duplicated so a re-seed produces a byte-identical file.

    Note the asymmetry this does NOT close: a ``tools`` reference of the
    ``@server/tool`` form grants ONE tool on kiro-cli, while the array mounts the
    whole server here, and the set of tools to deny is not knowable without
    connecting to the server. Those extra tools still reach the host permission
    gate; they are a wider surface, not an ungated one.
    """
    spec = _agent_spec_for(agent, work_dir) if agent else None
    if spec is None:
        return []
    raw = spec.get("mcpServers")
    if not isinstance(raw, dict):
        return []
    rules: set[str] = set()
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        disabled = entry.get("disabledTools")
        if not isinstance(disabled, list):
            continue
        for tool in disabled:
            if isinstance(tool, str) and tool:
                rules.add(f"mcp__{name}__{tool}")
    return sorted(rules)


def _registry_mode() -> bool:
    """Whether the operator declared this install registry-governed.

    Wrapped so a config-plane failure cannot be read as "no ceiling declared".
    Registry mode is a CEILING: while it is on, every server in the spec is
    withheld, because this backend cannot resolve a registry marker against the
    admin's catalog. An unreadable declaration is therefore read as GOVERNED
    rather than as the ungoverned default -- guessing "off" launches the
    session's unmarked local servers past a ceiling the operator may well have
    set, which is the one outcome the ceiling exists to prevent. The cost is
    stated rather than hidden: an install whose config plane is broken loses its
    session MCP surface until the read succeeds again, and the warning says so.
    """
    try:
        return _mcp_registry_mode()
    except Exception:  # pragma: no cover - defensive; the helper is fail-soft
        logger.warning(
            "session MCP: could not read registry mode; treating this install as "
            "registry-governed and withholding every agent-spec server, so an "
            "unmarked local server cannot launch past a ceiling that may be in "
            "force. This session runs without its agent-spec MCP servers.",
            exc_info=True,
        )
        return True


def session_mcp_servers(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    work_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """The ACP ``mcpServers`` array for a session running as *agent*.

    Called only for a backend in ``ACP_BACKENDS_SESSION_MCP_ARRAY``; every other
    harness reads the same spec itself and gets an empty array.

    *stub_server_names* are the servers that will ALSO arrive in this array as
    MCP-gateway broker stubs, which the caller appends after this list. A stub
    carries the SAME name as the agent-spec entry it wraps (it is a rewrite of
    that entry), so emitting both would put two elements with one ``name`` into
    a single array: either the raw entry shadows the stub and the session
    bypasses the broker, or both register and every pooled backend runs twice --
    the #927 regression ``injection_server_names`` exists to detect. The KAS spec
    projection resolves the same set for the same reason; the caller owns the
    overlay, so it resolves the set and passes it down.

    Blocking (reads the agent spec), so callers run it off the event loop.
    Deterministically ordered by server name, which keeps the array comparable
    across a session/new and the session/load that resumes it.
    """
    servers: dict[str, Any] = {}
    tools: Any = None
    spec = _agent_spec_for(agent, work_dir) if agent else None
    if spec is not None:
        raw = spec.get("mcpServers")
        if isinstance(raw, dict):
            servers = {str(k): v for k, v in raw.items()}
        tools = spec.get("tools")

    # kiro-cli's registry filter is SYMMETRIC (see ``agent._mcp_registry_mode``),
    # but only ONE half of it is reproducible here, and the asymmetry decides the
    # safe direction rather than being papered over:
    #
    # * OUTSIDE registry mode, kiro-cli drops the entries that CARRY the marker.
    #   That half is mirrored exactly: the marker is on the entry, the decision
    #   needs nothing else, and mirroring it is what keeps this backend from
    #   launching servers kiro-cli refuses.
    # * INSIDE registry mode, kiro-cli resolves each marked entry against the
    #   ADMIN'S CATALOG by map key, drops the ones the catalog does not list, and
    #   applies the catalog's own command override. None of that is available
    #   here: only kiro-cli fetches the registry URL, and it persists neither the
    #   URL nor the catalog, so nothing on disk can say whether a marked entry is
    #   authorized or whether its local command is the one the admin published.
    #   An entry that cannot be positively authorized is therefore WITHHELD, not
    #   launched -- a governed install must not have its policy decided by
    #   whichever harness the session happened to run on, and a local
    #   ``"type": "registry"`` marker is a line any user can add to a spec.
    #
    # In registry mode that leaves nothing from the spec, since the unmarked
    # entries are the ones kiro-cli drops. Crew's own control plane is re-added
    # below and is deliberately NOT subject to this: it is the host's own
    # process, re-derived from the managed source rather than read from the
    # user-editable spec, and withholding it would leave the session unable to
    # report back to its channel at all -- the exact defect this module exists to
    # fix. The residual difference from kiro-cli is stated for what it is: an
    # administrator who omits ``kirocrew-core`` from the catalog has it dropped
    # there and kept here, one host-owned server wider; every third-party server
    # goes the other way, withheld here and possibly mounted there.
    registry_mode = _registry_mode()
    for name, entry in list(servers.items()):
        marked = isinstance(entry, dict) and entry.get("type") == _KIRO_REGISTRY_TYPE
        if registry_mode:
            logger.info(
                "session MCP: withholding server %r -- registry mode is on and %s",
                name,
                (
                    "this backend cannot resolve the marker against the admin's catalog"
                    if marked
                    else "the entry carries no registry marker, so kiro-cli drops it too"
                ),
            )
            servers.pop(name)
        elif marked:
            logger.info(
                "session MCP: withholding server %r -- registry mode is off and the entry"
                " carries the registry marker, so kiro-cli drops it too",
                name,
            )
            servers.pop(name)

    for name in _CONTROL_PLANE_SERVERS:
        managed = managed_mcp_spec_entry(name)
        if managed is not None:
            servers[name] = managed

    for name in stub_server_names:
        if servers.pop(str(name), None) is not None:
            logger.debug(
                "session MCP: yielding %r to its broker stub, which the caller appends", name
            )

    # A spec's ``tools`` is the allowlist, so once a spec EXISTS the filter always
    # runs -- a missing or non-list ``tools`` is an EMPTY allowlist, not "no
    # filter". The spec is hand-editable JSON, so `"tools": "@srv"` is an easy
    # mistake, and skipping the filter on it would mount every declared server,
    # including an ``opt_in`` one the user deliberately left unreferenced, the
    # moment the session happened to run on claude. Failing closed matches
    # kiro-cli, which mounts a server only when ``tools`` names it and so grants
    # nothing from a spec that references nothing; the warning is what keeps a
    # typo from being silent. The control plane is deliberately NOT exempt --
    # kiro-cli drops ``kirocrew-core`` from a spec whose ``tools`` stops naming
    # it, and this backend must not re-grant what kiro-cli would drop
    # (``test_a_spec_that_drops_the_reference_still_drops_the_server``); with no
    # spec at all there is no allowlist to apply and the control plane stands.
    if spec is not None:
        if tools is not None and not isinstance(tools, list):
            logger.warning(
                "session MCP: agent spec %r has a non-list 'tools' (%s); treating it as an"
                " empty allowlist, so this session mounts NO MCP server -- fix the spec",
                agent,
                type(tools).__name__,
            )
        grants = tools if isinstance(tools, list) else []
        servers = {n: e for n, e in servers.items() if _tools_grant(grants, n)}

    out: list[dict[str, Any]] = []
    for name in sorted(servers):
        element = acp_server_element(name, servers[name])
        if element is not None:
            out.append(element)
    return out
