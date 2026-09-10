"""Kiro agent spec -> the ACP ``session/new`` ``mcpServers`` array.

For a harness in :data:`~kiro_crew.acp_backends.ACP_BACKENDS_SESSION_MCP_ARRAY`,
the ``session/new`` / ``session/load`` ``mcpServers`` parameter is where Kiro
Crew's MCP servers come from and the only place: neither claude-agent-acp nor
codex-acp reads ``~/.kiro/agents/<name>.json``. kiro-cli reaches the same servers
through ``--agent``, which is why that backend passes no array at all. Without
the translation here such a session runs with ZERO Kiro Crew tools -- the harness
itself works (prompts, streaming, permissions) but ``send_message``,
``spawn_run``, ``cron_add`` and every user-installed server are simply absent.

Nothing here is Anthropic-specific by design: the module is keyed on the
capability, not on the harness, so the next adapter that reads no agent spec of
Crew's joins the set rather than growing a second translator. What is genuinely
per-adapter stays with that adapter's mirror -- codex narrows this output in
:mod:`kiro_crew.providers.mirrors.codex` (it drops any transport this session's
adapter did not advertise, and its child processes inherit no environment), and
the shape notes below are claude-agent-acp's own zod schema.

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

**The governing rule, stated once because each corner of it is easy to argue
separately: this module matches kiro-cli, and deviating in EITHER direction
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
from typing import Any, NamedTuple

from kiro_crew import agent as _agent_mod
from kiro_crew.agent import (
    _load_json,
    _mcp_registry_mode,
    agent_spec_path,
    ensure_agent_materialized,
    managed_mcp_spec_entry,
)
from kiro_crew.agent_discovery import _read_agent_spec, project_agent_files, project_agent_name
from kiro_crew.agent_sdk.mcp_refs import parse_tools_refs

logger = logging.getLogger(__name__)

# Crew's own control plane. Re-derived from the managed source of truth on every
# spawn so a stale hand-edited command in the spec cannot cost a claude session
# the tools it needs to report back to its channel at all. Both are always-on
# (no gate, not opt_in), so ``managed_mcp_spec_entry`` returns them unless the
# install is broken. Re-derived, not read from the spec, is also what keeps them
# out of the registry filter below: they are the host's own process, not a
# third-party server the admin's catalog governs.
#
# PUBLIC because the codex projection carries this session's identity onto these
# two entries and onto NOTHING else. Naming the same tuple twice is how the two
# decisions drift apart, and the safety of that carriage rests on this being the
# set the loop below REPLACES from the managed source: the element's command, args
# and env are Crew's own by construction, not the spec's.
CONTROL_PLANE_SERVERS = ("kirocrew-core", "kirocrew-cron")

# kiro-cli's enterprise-governance discriminator, mirrored rather than imported
# (``agent._MCP_REGISTRY_TYPE`` is private; a ratchet test pins the two equal).
_KIRO_REGISTRY_TYPE = "registry"


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


class _Unread:
    """Marker for "the caller did not hand in a parsed spec", distinct from None.

    ``None`` is a real answer -- "there is no spec" -- and the projection must be
    able to say it. If ``None`` also meant "please read it", a projection whose
    one read came back empty would have every helper re-read behind its back, and
    a spec that became readable between those reads would be translated by one
    helper while the allowlist, computed from the ``None``, granted everything: an
    unreferenced pooled stub mounted past a grant-all allowlist. The sentinel makes
    "not supplied" and "supplied, and it is nothing" different values.
    """


_UNREAD = _Unread()


def _global_settings() -> dict[str, Any]:
    """The user's global ``~/.kiro/settings/mcp.json``, ``{}`` when absent or bad.

    Read here because the dashboard's tool-off action (``/api/mcp/toggle-tool``)
    writes ``disabledTools`` to THIS file and to nothing else -- kiro-cli reads it
    for enforcement alongside the agent spec, and the spec rebuild copies a global
    entry into the spec only when the spec lacks that server, never onto Crew's
    own managed servers. So a restriction on ``kirocrew-core`` written the ordinary
    way lives only here, and a deny set read from the spec alone would miss the one
    path a user actually takes. Resolved through the ``agent`` module attribute at
    call time so a test can point it at a temp file the same way it points the
    agents directory.
    """
    try:
        return _load_json(_agent_mod._KIRO_MCP_JSON)
    except Exception:  # pragma: no cover - _load_json is already fail-soft
        logger.debug("session MCP: global settings unreadable", exc_info=True)
        return {}


class ToolsAllowlist(NamedTuple):
    """The spec's ``tools`` allowlist, as a value rather than a filter pass.

    Exists so the SAME allowlist can gate every element that reaches a session
    -- the translated entries inside :func:`session_mcp_servers`, and the pooled
    broker stubs a mirror appends beside them. A stub carries the name of the
    entry it rewrites, so a stub for a server the spec's ``tools`` never
    references is an unreferenced server mounted anyway, which is the exact
    thing the filter on the translated half exists to prevent.

    ``applies`` is False when there is no spec at all: then there is no allowlist
    to apply and everything stands, the same reading :func:`session_mcp_servers`
    takes for the control plane. A spec whose ``tools`` is missing or not a list
    is an EMPTY allowlist, not "no filter" (see the filter's own comment).

    Why an allowlist at all: kiro-cli loads a server only when ``tools`` references
    it (``@server`` or ``@server/tool``), so an ``mcpServers`` entry with no
    reference is declared but never mounted. An ACP array has no such indirection
    -- everything in it is mounted -- so the reference is applied here instead.
    Without it, an entry the user deliberately left unreferenced (the shape every
    ``opt_in`` grant uses, and what a narrowed-by-hand spec looks like) would come
    alive the moment the session ran on a mirrored backend.

    The refs are read through :func:`~kiro_crew.agent_sdk.mcp_refs.parse_tools_refs`
    rather than scanned here, so this module and the unresolved-ref detector cannot
    disagree about what an entry names -- a guard that read ``@srv`` where this
    read nothing would report a ref as unresolved while the server mounted, and the
    reverse would mount a server the guard called absent. Only the bare ``*``
    grants everything: ``@*`` is a server LITERALLY named ``*`` there, matching
    this repo's other readers. ``@builtin`` is not special-cased -- a server
    actually called ``builtin`` is mountable, and the namespace exclusion belongs
    to the guard asking whether a ref resolves.
    """

    applies: bool
    grant_all: bool
    refs: frozenset[str]

    def grants(self, name: str) -> bool:
        return (not self.applies) or self.grant_all or name in self.refs


def _tools_allowlist(spec: dict[str, Any] | None) -> ToolsAllowlist:
    """The allowlist *spec* declares; see :class:`ToolsAllowlist` for the two edges."""
    if spec is None:
        return ToolsAllowlist(applies=False, grant_all=False, refs=frozenset())
    tools = spec.get("tools")
    grant_all, refs = parse_tools_refs(tools if isinstance(tools, list) else [])
    return ToolsAllowlist(applies=True, grant_all=grant_all, refs=frozenset(refs))


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
    translation rather than to an unrelated agent listing; ``source`` is
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


def agent_spec_snapshot(
    agent: str | None, *, work_dir: str | Path | None = None
) -> dict[str, Any] | None:
    """The spec for *agent* exactly as this module's own translation reads it.

    Exported for the unresolved-ref detector's two callers --
    :mod:`kiro_crew.acp.mcp_ref_guard` at session establishment and
    ``agent_sdk.drivers.acp.agent_spec_mcp_refs`` for ``kirocrew doctor`` -- which
    have to judge the spec's ``tools`` refs against the array a session receives.
    Reading the file themselves would give them a SECOND resolution order, and
    either could then report a ref as unresolved because it read a different spec
    than the one the projection ran on -- see :func:`_agent_spec_for` for why the order (project
    checkout nearest, then user level) is load-bearing rather than incidental.

    Blocking, and never raises: callers run it off the event loop and treat
    ``None`` as "nothing to say".
    """
    return _agent_spec_for(agent, work_dir) if agent else None


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
    and de-duplicated so a re-seed produces a byte-identical file. The pairs come
    from :func:`session_mcp_disabled_tools`, so a restriction the dashboard wrote
    to the global settings file is carried too, not only one written in the spec.

    Note the asymmetry this does NOT close: a ``tools`` reference of the
    ``@server/tool`` form grants ONE tool on kiro-cli, while the array mounts the
    whole server here, and the set of tools to deny is not knowable without
    connecting to the server. Those extra tools still reach the host permission
    gate; they are a wider surface, not an ungated one.
    """
    return sorted(
        f"mcp__{server}__{tool}"
        for server, tool in session_mcp_disabled_tools(agent, work_dir=work_dir)
    )


def session_mcp_disabled_tools(
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    spec: dict[str, Any] | None | _Unread = _UNREAD,
    settings: dict[str, Any] | _Unread = _UNREAD,
) -> frozenset[tuple[str, str]]:
    """Every ``(server, tool)`` pair switched off for this session, from BOTH sources.

    The structured form of :func:`session_mcp_deny_rules`, which spells the same
    pairs as claude ``permissions.deny`` rules. Kept as PAIRS here because the
    ``mcp__server__tool`` spelling is lossy -- it splits on the last ``__``, so a
    tool name containing ``__`` reads back as part of the server -- and a consumer
    that compares against an identity the adapter reports as two separate fields
    (codex's ``rawInput.server`` / ``rawInput.tool``) must not go through it.

    No server is exempt, the control plane included. This set answers "what did
    the user switch off", and that is true of ``kirocrew-core`` exactly as it is
    of a third-party server: the dashboard writes the key on an ordinary tool-off
    action for any of them. What differs per server is how -- or whether -- a
    given backend can HONOUR it, and that is the caller's question, not this one's
    (see :func:`session_mcp_restricted_servers` for the one place the control
    plane is treated differently, and why).

    Two sources, unioned. The agent spec carries the key when a spec author wrote
    it or the rebuild copied a global entry in. The global ``settings/mcp.json``
    carries it when the dashboard's tool-off action wrote it -- and for Crew's own
    managed servers that is the ONLY place it can be, because the spec rebuild
    never copies a global entry onto a managed server (see
    :func:`_global_settings`). kiro-cli reads both for enforcement; so does this.
    A union is the only safe combination: a restriction can only ever restrict.

    ``spec`` and ``settings`` let a caller hand in what it has ALREADY parsed; an
    explicit ``None`` spec means "there is no spec" and is honoured as such, never
    re-read (see :class:`_Unread`). Never raises: an unreadable source switches
    nothing off from that source.
    """
    if isinstance(spec, _Unread):
        spec = _agent_spec_for(agent, work_dir) if agent else None
    if isinstance(settings, _Unread):
        settings = _global_settings()
    pairs: set[tuple[str, str]] = set()
    for source in (spec, settings):
        if not isinstance(source, dict):
            continue
        raw = source.get("mcpServers")
        if not isinstance(raw, dict):
            continue
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            disabled = entry.get("disabledTools")
            if not isinstance(disabled, list):
                continue
            for tool in disabled:
                if isinstance(tool, str) and tool:
                    pairs.add((str(name), tool))
    return frozenset(pairs)


def session_mcp_restricted_servers(disabled_tools: Collection[tuple[str, str]]) -> frozenset[str]:
    """Servers whose per-TOOL narrowing no transport can carry as an element.

    The sibling of :func:`session_mcp_deny_rules`, for a backend that has no file
    to put deny rules in. Same input, same reason to exist: ``disabledTools`` is a
    RESTRICTION, and a backend that forwards the server while dropping it widens
    the session's tool surface behind the user's back -- the dashboard writes that
    key on an ordinary tool-off action, and this repo already treats losing it as a
    defect ("dropping ``disabledTools`` on a save would silently widen the agent's
    tool surface").

    Claude re-applies the restriction as ``permissions.deny`` rules and so may keep
    the server. A backend with no deny channel has only one faithful option, which
    is to not mount the server at all -- so this returns the NAMES and lets that
    backend omit them. Withholding a server is an availability cost; forwarding an
    un-narrowed one is a capability the user switched off.

    **Crew's own control plane is exempt from WITHHOLDING, not from the
    restriction.** ``kirocrew-core`` / ``kirocrew-cron`` are re-derived from
    ``managed_mcp_spec_entry``, which emits only command/args/env, so a
    ``disabledTools`` on their spec entry never reaches the element -- and
    withholding the whole server on the strength of it would leave the session
    unable to report back to its channel at all, which is the exact defect this
    module exists to fix. The restriction itself is still honoured, on the one
    channel this transport does have: every call to one of these servers reaches
    Crew as a ``session/request_permission`` (their tools carry no annotations, so
    codex prompts for each), and the client answers a call to a switched-off tool
    with the adapter's reject option before anything runs. The pairs it checks
    come from :func:`session_mcp_disabled_tools`, on the same parse as this set.
    That channel is complete for the control plane and NOT for a third-party
    server -- a tool annotated ``readOnlyHint`` is auto-approved inside codex and
    never prompts -- which is why the two are treated differently here rather
    than both being mounted.

    ``disabled`` is deliberately NOT part of this. A disabled server is already
    absent from the array by a different mechanism: ``agent.build_agent_config``
    strips its ``@alias`` from ``tools``, and the allowlist filter in
    :func:`session_mcp_servers` mounts nothing ``tools`` does not name. Re-checking
    it here would be a second spelling of a rule that already holds.

    Derived from the ``(server, tool)`` PAIRS :func:`session_mcp_disabled_tools`
    resolved, never from the spec on its own, so the two decisions a restriction
    drives -- withhold the server, or refuse the call -- read the same sources. The
    dashboard's tool-off action writes to the global settings file and not to the
    spec, so a withhold set read from the spec alone would miss a third-party server
    narrowed the ordinary way; that server would mount, and the per-call refusal
    cannot reach it (a tool annotated ``readOnlyHint`` is approved inside codex
    without asking), so the switched-off tool would run. Its one caller is
    :func:`session_mcp_projection`, which hands in the pairs it already resolved
    from one read of each source. A server named only in the global file and not in
    the spec lands here too and is harmless: nothing mounts it. Free of I/O.
    """
    return frozenset(server for server, _tool in disabled_tools) - frozenset(CONTROL_PLANE_SERVERS)


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


class SessionMcpProjection(NamedTuple):
    """Everything a backend derives from the agent spec, from ONE parse of it."""

    #: The translated ``mcpServers`` array (:func:`session_mcp_servers`).
    servers: list[dict[str, Any]]
    #: Servers whose per-tool narrowing forces withholding them
    #: (:func:`session_mcp_restricted_servers`).
    restricted: frozenset[str]
    #: Every ``(server, tool)`` the spec switches off, no server exempt
    #: (:func:`session_mcp_disabled_tools`).
    disabled_tools: frozenset[tuple[str, str]]
    #: The ``tools`` allowlist the translated half was filtered by, so a caller
    #: appending elements of its own (pooled stubs) can hold them to the same one.
    allowlist: ToolsAllowlist


def session_mcp_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    work_dir: str | Path | None = None,
) -> SessionMcpProjection:
    """The array, the withhold set AND the per-tool restrictions, from ONE parse.

    A backend that translates the spec, withholds part of it on the strength of the
    spec, and refuses individual calls on the strength of the spec must derive all
    three from the same bytes. Independent parses of a user-writable file are a
    consistency window: an entry that gains ``disabledTools`` between two of them
    produces a restriction set that does not mention it and a translation that
    carries it, so the narrowed server mounts un-narrowed -- or a deny set that
    names a tool on a server the array, read a moment earlier, never mounted.

    Returning them from one call makes that structural rather than a convention. The
    alternative -- documenting that callers should thread a ``spec=`` through three
    functions -- is a rule a future caller can forget, and forgetting it is silent.
    The read's ANSWER is what is threaded, including "there is no spec": a helper
    handed ``None`` honours it rather than reading again (see :class:`_Unread`), so
    a spec that appears between this read and a helper's cannot be translated by
    one helper and ignored by another.

    Blocking (parses the spec and the global settings file once each), so callers
    run it off the event loop.
    """
    spec = _agent_spec_for(agent, work_dir) if agent else None
    settings = _global_settings()
    disabled_tools = session_mcp_disabled_tools(
        agent, work_dir=work_dir, spec=spec, settings=settings
    )
    return SessionMcpProjection(
        servers=session_mcp_servers(
            agent, stub_server_names=stub_server_names, work_dir=work_dir, spec=spec
        ),
        restricted=session_mcp_restricted_servers(disabled_tools),
        disabled_tools=disabled_tools,
        allowlist=_tools_allowlist(spec),
    )


def session_mcp_servers(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    work_dir: str | Path | None = None,
    spec: dict[str, Any] | None | _Unread = _UNREAD,
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
    the regression ``injection_server_names`` exists to detect. The KAS spec
    projection resolves the same set for the same reason; the caller owns the
    overlay, so it resolves the set and passes it down.

    ``spec`` lets a caller hand in a spec it has already parsed; see
    :func:`session_mcp_projection` for why a second parse is a consistency window
    rather than a cost.

    Blocking when it parses (so callers run it off the event loop). Deterministically
    ordered by server name, which keeps the array comparable across a session/new and
    the session/load that resumes it.
    """
    servers: dict[str, Any] = {}
    tools: Any = None
    if isinstance(spec, _Unread):
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

    for name in CONTROL_PLANE_SERVERS:
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
        allow = _tools_allowlist(spec)
        servers = {n: e for n, e in servers.items() if allow.grants(n)}

    out: list[dict[str, Any]] = []
    for name in sorted(servers):
        element = acp_server_element(name, servers[name])
        if element is not None:
            out.append(element)
    return out
