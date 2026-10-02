"""``mcp.json`` servers for sessions of an instance refused the shared spec.

The primary agent spec pins ``includeMcpJson: false``, so the spec rebuild is
what turns an ``mcp.json`` entry into a mounted server: it copies the entry into
``mcpServers`` and its ``@ref`` into ``tools``. An instance on a non-default
``KIROCREW_HOME`` that shares ``~/.kiro/agents`` with another install is refused
that rebuild (:func:`kiro_crew.agent._decline_shared_agent_home`), correctly --
the spec belongs to the other install and pins ITS home into every managed
entry. Without this module the refused instance's own ``mcp.json`` servers then
reach none of its sessions, while its dashboard probe shows them Online.

The refused rebuild therefore still runs the MCP half of the rebuild, in memory,
over this instance's own sources: the same merge, command resolution, alias
normalization and mute/disable handling a written spec would get
(the rebuild's own MCP passes, reached through :mod:`kiro_crew.agent`). The
result is held in this process's memory -- never in the shared agents dir, and
never in a file a session start would trust -- and recomputed when the sources
move. Nothing about the other install changes. It reaches a session by the one
channel each host is missing:

* **kiro-cli** mounts what its ``--agent`` spec declares, so the server itself
  is missing. :func:`session_servers` appends it to the ``session/new`` array,
  the session-level channel the gateway's broker stubs ride.
* **KAS** mounts ``~/.kiro/settings/mcp.json`` on its own, each server with its
  declared environment, but grants a tool only when the agent's ``tools`` names
  it. :func:`kas_tool_grants` adds that ``@name`` to the projected agent, and
  nothing is put on the wire: no command, argument or credential.

What is deliberately NOT carried:

* **Approvals of its own.** ``autoApprove`` is dropped and nothing is added to
  ``allowedTools``. The session's existing ``allowedTools`` applies to these
  servers exactly as it would if the rebuild had written them into the spec,
  which would ALSO have granted each one in ``allowedTools``. The security bar is
  parity with that written spec, not a stricter one. One exception follows from
  the spec being the other install's: its ``allowedTools`` never passed this
  instance's governance ceiling, so a server one of its grants covers (``*``,
  ``@name``, ``@name/<tool>``) is not delivered on kiro when this instance's
  ceiling would not keep that grant (:func:`_inherited_approval_exceeds_ceiling`).
* **Remote (url) servers.** Only a stdio element is known to be accepted on the
  session-level channel; a remote server is named in the gateway log instead.
* **Registry mode.** An injected, unmarked entry is dropped by the client under
  a registry ceiling, and nothing here can resolve a name against the catalog,
  so nothing is delivered (the same ceiling the broker stubs observe).
* **Per-tool restrictions.** An ACP array element mounts every tool, so an
  entry with a non-empty ``disabledTools`` is not delivered at all rather than
  delivered with the operator's switched-off tools live again.
* **Anything the session already has.** A name the shared spec declares, or a
  broker stub carries, is never delivered twice.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Collection
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_sdk.mcp_refs import parse_tools_refs
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes, validate_file_path
from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_entry_is_registry_governed
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.platform.governance import may_skip_gate_now

logger = logging.getLogger(__name__)

#: Bounds on what the projection retains, one per stored field (the repo rule
#: that a cap bounds memory only when every stored field is bounded too). Set far
#: above any real ``mcp.json`` -- a 64 KiB env value, a thousand args -- so they
#: guard the gateway's memory without refusing a configuration a user writes.
#: An entry past a bound is refused whole (a cut argument would launch a
#: different command) and counted in a gateway warning.
MAX_DELIVERED_SERVERS = 256
MAX_SERVER_ARGS = 1024
MAX_SERVER_ENV = 1024
MAX_FIELD_CHARS = 65536

PRIMARY_AGENT = Path(AGENT_FILENAME).stem

#: The last projection this process computed, keyed by the inputs it was
#: computed from. Process memory only: a file anyone in the data home can write
#: would let that writer choose the next session's launched command.
_MEMO: dict[str, Any] = {"fingerprint": None, "servers": {}, "native": frozenset()}
#: What the last supply audit recorded: the servers sessions were supplied and
#: the ones the governance ceiling withheld. Recorded per change, not per session.
_AUDITED: dict[str, Any] = {"supplied": {}, "withheld": frozenset()}
_MEMO_LOCK = threading.Lock()


#: Most skipped-server names one diagnostic line names, and the longest name it
#: shows; the rest are counted, not retained.
MAX_LOGGED_NAMES = 16
MAX_LOGGED_NAME_CHARS = 64


class _Skipped:
    """A count of skipped servers plus a bounded sample of their names."""

    __slots__ = ("count", "sample")

    def __init__(self) -> None:
        self.count = 0
        self.sample: list[str] = []

    def add(self, alias: str) -> None:
        self.count += 1
        if len(self.sample) < MAX_LOGGED_NAMES:
            self.sample.append(alias[:MAX_LOGGED_NAME_CHARS])

    def render(self) -> str:
        more = self.count - len(self.sample)
        return ", ".join(self.sample) + (f" (+{more} more)" if more > 0 else "")


def _compute_projection() -> tuple[dict[str, dict[str, Any]], bool, frozenset[str]]:
    """The projection, whether any source command failed to resolve, and the
    projected names KAS mounts natively under that same name.

    The third value is what :func:`kas_tool_grants` may grant: see
    :func:`_kas_native_names`.

    Runs the rebuild's MCP passes over an EMPTY base, so the result holds only
    what the sources contribute, resolved and normalized exactly as a written
    spec would hold it. Returns ``{alias: entry}`` for the user-installed stdio
    servers that pass every launch predicate; the shared spec is not consulted
    here (the session-side filter does that, against the file as it is then).
    """
    from kiro_crew import agent as agent_mod  # circular at module scope

    mcp_sources = agent_mod.mcp_sources
    mcp_aliases = agent_mod.mcp_aliases
    config: dict[str, Any] = {"mcpServers": {}, "tools": [], "allowedTools": []}
    sources = mcp_sources.merge_mcp_sources(config, audit=False)
    resolved = mcp_sources.resolve_mcp_servers(config, sources, agent_mod._resolve_mcp_command)
    mounted = mcp_aliases.normalize_server_keys(config, resolved.unresolved)
    # audit=False: this config is never written, so its grants are not records.
    mcp_sources.sync_shared_server_refs(config, sources, mounted, audit=False)

    user_aliases = _user_mount_aliases(sources, mounted)
    grant_all, refs = parse_tools_refs(config.get("tools"))
    referenced = set(refs)
    out: dict[str, dict[str, Any]] = {}
    remote = _Skipped()
    restricted = _Skipped()
    oversized = _Skipped()
    untimed = _Skipped()
    for alias, entry in sorted((config.get("mcpServers") or {}).items()):
        if alias not in user_aliases or not isinstance(entry, dict):
            continue
        if not (grant_all or alias in referenced):
            continue
        if mcp_entry_is_muted(entry) or mcp_entry_is_registry_governed(entry):
            continue
        if entry.get("url"):
            remote.add(alias)
            continue
        if not isinstance(entry.get("command"), str):
            continue
        if entry.get("disabledTools"):
            # The spec is the only carrier of a per-tool restriction; an ACP
            # array element would mount every tool the operator switched off.
            restricted.add(alias)
            continue
        launch = _launch_entry(entry)
        if launch is None or len(alias) > MAX_FIELD_CHARS or len(out) >= MAX_DELIVERED_SERVERS:
            oversized.add(alias)
            continue
        if "timeout" in entry:
            # An ACP element has no timeout field. Withholding the server would
            # be the original bug (nothing else mounts it here), so it is
            # delivered on kiro-cli's default timeout and the drop is named.
            untimed.add(alias)
        out[alias] = launch
    if remote.count:
        logger.warning(
            "Shared agent home is not writable from this data home; %d remote MCP "
            "server(s) cannot be delivered at session level and will not reach "
            "this instance's sessions: %s",
            remote.count,
            remote.render(),
        )
    if oversized.count:
        logger.warning(
            "Not delivering %d mcp.json server(s) at session level: past the "
            "projection bounds (%d servers, %d args or env entries, %d characters "
            "per field): %s",
            oversized.count,
            MAX_DELIVERED_SERVERS,
            MAX_SERVER_ARGS,
            MAX_FIELD_CHARS,
            oversized.render(),
        )
    if restricted.count:
        logger.warning(
            "Not delivering %d mcp.json server(s) at session level: a session-level "
            "element cannot carry their disabledTools, so delivering would turn "
            "switched-off tools back on: %s",
            restricted.count,
            restricted.render(),
        )
    if untimed.count:
        logger.warning(
            "Delivering %d mcp.json server(s) at session level on kiro-cli's default "
            "timeout: a session-level element cannot carry their configured "
            "timeout: %s",
            untimed.count,
            untimed.render(),
        )
    return out, bool(resolved.unresolved), _kas_native_names(sources, mounted, out)


def _kas_native_names(
    sources: Any, mounted: dict[str, str], projected: dict[str, Any]
) -> frozenset[str]:
    """Projected names that KAS itself mounts from ``~/.kiro/settings/mcp.json``.

    KAS reads that file on its own, whatever the agent's ``includeMcpJson``, and
    mounts each server under its raw name with its full declared environment; the
    agent's ``tools`` decides only whether the session may call it. A name
    qualifies only when the projected entry IS that file's entry under that name:
    declared there, not renamed by the alias pass, and not also declared by another
    scope (whose entry would be the projection's, while KAS mounts this file's).
    """
    kiro_global = sources.kiro_global if isinstance(sources.kiro_global, dict) else {}
    others: set[str] = set()
    for label, scope in sources.scopes:
        if label != "kiro-global" and isinstance(scope, dict):
            others.update(str(n) for n in scope)
            # By the alias the rebuild actually mounted: a distinct entry whose
            # alias collided was suffixed (``name-2``) and overrides nothing.
            others.update(mounted.get(str(n)) or mcp_server_alias(str(n)) for n in scope)
    native: set[str] = set()
    for raw in kiro_global:
        name = str(raw)
        if name not in projected or name in others:
            continue
        if (mounted.get(name) or mcp_server_alias(name)) != name:
            continue
        native.add(name)
    return frozenset(native)


def _user_mount_aliases(sources: Any, mounted: dict[str, str]) -> set[str]:
    """Concrete mount aliases of the servers a user installed through ``mcp.json``.

    Taken from the rebuild's own source-to-alias map, so a server the alias pass
    mounted under a collision suffix (``name-2``) is still recognised as the
    user's.
    """
    managed = set(sources.managed_names) | {mcp_server_alias(n) for n in sources.managed_names}
    aliases: set[str] = set()
    for _label, scope in sources.scopes:
        if not isinstance(scope, dict):
            continue
        for raw in scope:
            name = str(raw)
            if name in managed or mcp_server_alias(name) in managed:
                continue
            aliases.add(mounted.get(name) or mcp_server_alias(name))
    return aliases


def _bounded_str(value: Any) -> str | None:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_FIELD_CHARS else None


def _launch_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The launch fields of *entry* -- ``command``, ``args``, ``env`` -- or ``None``.

    They are all an ACP stdio element carries, and keeping nothing else means no
    grant (``autoApprove``) or bookkeeping key can ride along. ``None`` when a
    field is past its (generous) bound: refused whole, never truncated.
    """
    command = _bounded_str(entry["command"])
    raw_args = entry.get("args")
    args_in = list(raw_args) if isinstance(raw_args, (list, tuple)) else []
    raw_env = entry.get("env")
    env_in = raw_env if isinstance(raw_env, dict) else {}
    if command is None or len(args_in) > MAX_SERVER_ARGS or len(env_in) > MAX_SERVER_ENV:
        return None
    args = [_bounded_str(a) for a in args_in]
    env = {_bounded_str(k): _bounded_str(v) for k, v in env_in.items()}
    if None in args or None in env or None in env.values():
        return None
    return {"command": command, "args": args, "env": env}


def _source_fingerprint() -> list[Any]:
    """What the projection was computed from, cheaply: each source file's stat
    signature plus the search ``PATH``.

    A refused instance re-attempts the rebuild on every refresh poll, and the
    projection runs the full MCP passes (command resolution, audit events). An
    unchanged fingerprint skips them; any edit to an ``mcp.json`` scope, or a
    ``PATH`` change that can move command resolution, recomputes.
    """
    from kiro_crew import agent as agent_mod  # circular at module scope

    files = [agent_mod._KIRO_MCP_JSON, agent_mod._user_dir() / "mcp.json"]
    files.extend(agent_mod.mcp_sources._extra_mcp_scope_globals())
    sig: list[Any] = []
    for path in files:
        try:
            st = Path(path).stat()
            sig.append([str(path), st.st_mtime_ns, st.st_size])
        except OSError:
            sig.append([str(path), None, None])
    sig.append(os.environ.get("PATH", ""))
    return sig


def refresh_projection() -> dict[str, dict[str, Any]]:
    """The projection for the current sources, recomputed only when they moved.

    Called from the refused rebuild (boot, a Sync, a sessions restart, a
    dashboard MCP change, the refresh poll) and from every session start, so a
    session always sees what the sources say NOW. The result lives in process
    memory only and is recomputed from the protected ``mcp.json`` sources the
    written spec would itself be built from; nothing on disk is trusted.
    """
    return dict(_refresh()[0])


def _commands_still_runnable(servers: dict[str, dict[str, Any]]) -> bool:
    """Whether every memoized resolved command still names a runnable file.

    The source fingerprint covers ``mcp.json`` and ``PATH``, not the binaries a
    resolution picked: one removed while another remains on ``PATH`` must be
    re-resolved rather than launched from the memo. A stat per server.
    """
    for entry in servers.values():
        command = entry.get("command")
        if not isinstance(command, str) or not os.path.isabs(command):
            continue
        if not (os.path.isfile(command) and os.access(command, os.X_OK)):
            return False
    return True


def _refresh() -> tuple[dict[str, dict[str, Any]], frozenset[str]]:
    """The memoized projection and its KAS-native names, recomputed when stale."""
    fingerprint = _source_fingerprint()
    with _MEMO_LOCK:
        hit = _MEMO["fingerprint"] == fingerprint and _commands_still_runnable(_MEMO["servers"])
        if hit:
            servers, native = dict(_MEMO["servers"]), _MEMO["native"]
    if not hit:
        servers, unresolved, native = _compute_projection()
        with _MEMO_LOCK:
            # A command that did not resolve may resolve later without any source
            # or PATH change (an interpreter installed in place), so such a pass is
            # not memoized: the next session start re-resolves, as a written
            # rebuild would.
            _MEMO["fingerprint"] = None if unresolved else fingerprint
            _MEMO["servers"] = servers
            _MEMO["native"] = native
    _audit_supply(servers)
    return dict(servers), native


def _audit_supply(servers: dict[str, dict[str, Any]]) -> None:
    """Record a change to what sessions are supplied, after the ceiling filter.

    The ceiling decision depends on the shared spec and this instance's ceiling as
    well as the projection, so it is re-evaluated on every refresh (a memo hit
    included) and recorded only when its outcome changed. A server the ceiling
    withholds is never listed as supplied; its refusal is its own record.
    """
    withheld = _ceiling_withheld(servers)
    supplied = {k: v for k, v in servers.items() if k not in withheld}
    with _MEMO_LOCK:
        previous = _AUDITED["supplied"]
        previous_withheld = _AUDITED["withheld"]
        _AUDITED["supplied"] = supplied
        _AUDITED["withheld"] = withheld
    if supplied != previous:
        _audit_projection(supplied, previous)
    newly = withheld - previous_withheld
    if newly:
        _audit_withheld(newly)


def _ceiling_withheld(servers: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Aliases the governance ceiling keeps from kiro delivery. Never raises."""
    try:
        view = _shared_spec_view()
    except Exception:
        logger.warning("could not read the shared spec for the supply audit", exc_info=True)
        return frozenset()
    if view is None:
        return frozenset()
    _declared, allowed = view
    return frozenset(a for a in servers if _inherited_approval_exceeds_ceiling(a, allowed))


def _audit_withheld(aliases: frozenset[str]) -> None:
    """One ``denied`` SEL record naming the servers the ceiling withheld. Never raises."""
    names = _Skipped()
    for alias in sorted(aliases):
        names.add(alias)
    reason = (
        "the shared spec's allowedTools pre-approve them beyond this instance's "
        "governance ceiling"
    )
    logger.warning(
        "not delivering %d mcp.json server(s) to kiro sessions: %s (%s)",
        names.count,
        names.render(),
        reason,
    )
    try:
        from kiro_crew import agent as agent_mod  # circular at module scope

        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_declined_home_withheld",
            outcome="denied",
            source="install_agent",
            resources=f"{names.render()} withheld from kiro session-level delivery",
            error=reason,
        )
    except Exception:
        logger.warning("refused-home withheld audit record failed", exc_info=True)


def _audit_projection(
    servers: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]]
) -> None:
    """One SEL record per change to what sessions are supplied, as a written
    spec's ``mcp_tools_added`` would record its tool-surface change -- including
    a change that withdraws servers, so the trail shows removals too. Never raises."""
    names = _Skipped()
    for alias in sorted(servers):
        names.add(alias)
    withdrawn = _Skipped()
    for alias in sorted(set(previous) - set(servers)):
        withdrawn.add(alias)
    if not names.count and not withdrawn.count:
        return
    supplied = names.render() if names.count else "no mcp.json servers"
    detail = f"; withdrawn: {withdrawn.render()}" if withdrawn.count else ""
    logger.info(
        "Shared agent home is not writable from this data home; supplying %d "
        "mcp.json server(s) to sessions directly: %s%s",
        names.count,
        supplied,
        detail,
    )
    try:
        from kiro_crew import agent as agent_mod  # circular at module scope

        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_declined_home_supplied",
            outcome="ok",
            source="install_agent",
            resources=(
                f"{supplied} supplied to sessions{detail} (refused shared home: "
                "session-level on kiro, tools grant on KAS)"
            ),
        )
    except Exception:
        logger.warning("refused-home supply audit record failed", exc_info=True)


def clear_projection() -> None:
    """Forget the projection once this instance owns its spec again."""
    with _MEMO_LOCK:
        _MEMO["fingerprint"] = None
        _MEMO["servers"] = {}
        _MEMO["native"] = frozenset()
        _AUDITED["supplied"] = {}
        _AUDITED["withheld"] = frozenset()


def _shared_spec_view() -> tuple[set[str], list[str]] | None:
    """What the shared primary spec mounts and pre-approves; ``None`` if unreadable.

    The mounted aliases are there so nothing is registered twice: a name the
    spec mounts reaches the session through the spec. The ``allowedTools``
    entries come from the SAME read, because a delivered server runs under the
    approvals the session's child loaded from that spec.
    """
    from kiro_crew.agent import kiro_agents_dir_path  # circular at module scope

    path = kiro_agents_dir_path() / AGENT_FILENAME
    if not path.is_file():
        return set(), []
    spec = _read_agent_spec(path, operation="declined_home_session_mcp", source="unknown")
    if not isinstance(spec, dict):
        return None
    declared = _declared_names(spec)
    if declared is None:
        return None
    raw = spec.get("allowedTools")
    allowed = [e.strip() for e in raw if isinstance(e, str)] if isinstance(raw, list) else []
    return declared, allowed


def _inherited_approval_exceeds_ceiling(alias: str, allowed: Collection[str]) -> bool:
    """Whether the shared spec pre-approves *alias* in a way this instance would not.

    The shared spec is the other install's, so its ``allowedTools`` never passed
    this instance's governance ceiling. A server delivered to a session that
    loaded such a grant runs its tools without reaching the PreToolUse gate, so
    each grant covering the server (``*``, ``@name``, ``@name/<tool>``, or a glob
    that can match one of them) is asked
    the question this instance's own ``allowedTools`` writers ask
    (:func:`~kiro_crew.platform.governance.may_skip_gate_now`, fail-closed). A
    single "no" withholds the server.
    """
    for entry in allowed:
        if entry == "*" or (_is_glob(entry) and _glob_covers(entry, alias)):
            # A glob can match every tool the server has, so the question is the
            # whole-server one.
            ref = f"@{alias}"
        elif entry.startswith("@") and not _is_glob(entry):
            server = entry[1:].split("/", 1)[0]
            if not server or mcp_server_alias(server) != alias:
                continue
            ref = entry
        else:
            continue
        if not may_skip_gate_now(ref):
            return True
    return False


def _is_glob(entry: str) -> bool:
    return any(c in entry for c in "*?[")


def _glob_covers(pattern: str, alias: str) -> bool:
    """Whether glob *pattern* can match ``@alias`` or any ``@alias/<tool>``.

    kiro-cli expands ``allowedTools`` entries as globs, and its tool names are
    not known here, so the answer errs toward "covers": an ``@``-pattern covers
    the server when its server segment matches the alias, and a pattern that
    does not start with ``@`` covers it when it starts with a wildcard (``*_delete``
    can match ``@alias/mail_delete``). Only a literal non-``@`` first character
    rules a bare pattern out, since no MCP ref can then match it.
    """
    if fnmatchcase(f"@{alias}", pattern):
        return True
    if pattern.startswith("@"):
        return fnmatchcase(alias, pattern[1:].split("/", 1)[0])
    return pattern[:1] in ("*", "?", "[")


def _declared_names(spec: Any) -> set[str] | None:
    """Server aliases *spec* (already read) mounts; ``None`` when it is not a spec.

    Mounted means declared in ``mcpServers`` AND referenced from ``tools``: an
    entry with no ref is declared but never mounted (the shape the rebuild
    leaves for a muted server), so it must not count as already present.
    """
    if not isinstance(spec, dict):
        return None
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return set()
    grant_all, refs = parse_tools_refs(spec.get("tools"))
    referenced = {mcp_server_alias(str(r)) for r in refs}
    declared = {mcp_server_alias(str(n)) for n in servers}
    return declared if grant_all else declared & referenced


def _env_pairs(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def session_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    present: Collection[str] = (),
) -> list[dict[str, Any]]:
    """ACP ``session/new`` elements for this session's undelivered servers.

    ``present`` is every name the caller's array already carries (the broker
    stubs); a server under that name, raw or aliased, is never added again.
    Empty -- the pre-existing behaviour -- unless ALL of these hold: the session
    runs the user-level primary agent, the shared spec write is refused right
    now, no registry ceiling applies, and the shared spec is readable. Blocking
    file I/O; callers are already off the event loop. Never raises.
    """
    try:
        return _session_servers(agent, work_dir=work_dir, present=present)
    except Exception:
        logger.warning("session-level mcp.json delivery failed; delivering none", exc_info=True)
        return []


def _session_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None,
    present: Collection[str],
) -> list[dict[str, Any]]:
    from kiro_crew.agent import (  # circular at module scope
        _decline_shared_agent_home,
        _mcp_registry_mode,
        _project_shadow_of,
    )

    if (agent or PRIMARY_AGENT) != PRIMARY_AGENT:
        return []
    if work_dir and (
        _project_shadow_of(PRIMARY_AGENT, work_dir, markdown_specs=False, dispatchable_only=True)
        is not None
    ):
        # The checkout declares its own agent of this name: the session is not
        # running the user-level spec this projection supplements.
        return []
    if _decline_shared_agent_home(audit=False) is None:
        # The instance writes its own spec, which already mounts every server.
        return []
    if _mcp_registry_mode():
        return []
    projection = refresh_projection()
    if not projection:
        return []
    view = _shared_spec_view()
    if view is None:
        # Cannot tell what the spec already mounts; delivering could register a
        # server twice, so deliver nothing.
        return []
    declared, allowed = view
    taken = declared | {mcp_server_alias(str(n)) for n in present} | set(present)
    return [
        {
            "name": alias,
            "command": entry["command"],
            "args": list(entry["args"]),
            "env": _env_pairs(entry["env"]),
        }
        for alias, entry in sorted(projection.items())
        if alias not in taken and not _inherited_approval_exceeds_ceiling(alias, allowed)
    ]


def kas_tool_grants(
    agent: str | None,
    *,
    spec: Any,
    work_dir: str | Path | None = None,
    present: Collection[str] = (),
) -> list[str]:
    """``@name`` refs a KAS session must add to ``tools`` for its refused-home servers.

    KAS mounts ``~/.kiro/settings/mcp.json`` itself, with each server's own
    environment, whatever the agent spec says; what it withholds is the GRANT,
    which comes only from the agent's ``tools``. So on KAS nothing is delivered
    on the wire -- no command, argument or credential crosses it -- and the fix is
    the visibility grant a written spec's ``tools`` would have carried.

    A grant reveals the tools and adds no approval: ``allowedTools`` is
    untouched and applies as it would to a written spec. *spec* is the agent spec
    the caller ALREADY loaded to build the session's permissions: its ``tools``
    and ``mcpServers`` decide the grant, so the grant and the permissions come
    from one observation.
    A ref its ``tools`` already carries, or a ``"*"`` that covers every ref, is not added
    again. *present* is every name the session-level array carries (the broker
    stubs). Same gates as :func:`session_servers`, plus: only a server KAS mounts
    under the projected name (:func:`_kas_native_names`), and never one a
    workspace ``mcp.json`` under *work_dir* also declares, since KAS may mount
    that checkout's server under the name instead. Never raises.
    """
    try:
        return _kas_tool_grants(agent, spec=spec, work_dir=work_dir, present=present)
    except Exception:
        logger.warning("refused-home tool grants failed; granting none", exc_info=True)
        return []


def _kas_tool_grants(
    agent: str | None,
    *,
    spec: Any,
    work_dir: str | Path | None,
    present: Collection[str],
) -> list[str]:
    from kiro_crew.agent import (  # circular at module scope
        _decline_shared_agent_home,
        _mcp_registry_mode,
    )

    if (agent or PRIMARY_AGENT) != PRIMARY_AGENT:
        return []
    tools = spec.get("tools") if isinstance(spec, dict) else None
    if tools == "*" or not isinstance(tools, list) or "*" in tools:
        # An absent or malformed list is projected as no access at all; adding
        # refs would invent an allowlist nobody wrote.
        return []
    if _decline_shared_agent_home(audit=False) is None:
        return []
    if _mcp_registry_mode():
        return []
    projection, native = _refresh()
    if not projection:
        return []
    declared = _declared_names(spec)
    if declared is None:
        return []
    taken = declared | {mcp_server_alias(str(n)) for n in present} | set(present)
    workspace = _workspace_names(work_dir)
    have = {t for t in tools if isinstance(t, str)}
    out: list[str] = []
    withheld = _Skipped()
    for name in sorted(projection):
        if name in taken or name not in native or f"@{name}" in have:
            continue
        if "*" in workspace or name in workspace:
            withheld.add(name)
            continue
        out.append(f"@{name}")
    if withheld.count:
        logger.warning(
            "Not granting %d mcp.json server(s) to KAS sessions: the session's "
            "workspace mcp.json declares the same name, so KAS may mount that "
            "checkout's server under it: %s",
            withheld.count,
            withheld.render(),
        )
    return out


def _workspace_names(work_dir: str | Path | None) -> set[str]:
    """Server names a checkout's ``.kiro/settings/mcp.json`` declares, raw and aliased.

    Read through the credential gate (``validate_file_path`` +
    ``safe_read_file_bytes``: bounded, sensitive-path checked on the resolved
    target, refusing a link to a credential file) because the checkout is
    untrusted and this read runs in the gateway. A file the gate refuses or that
    does not parse answers ``{"*"}``: it may declare any name, so the caller
    grants none.
    """
    if not work_dir:
        return set()
    path = Path(work_dir) / ".kiro" / "settings" / "mcp.json"
    if validate_file_path(str(path)) is None:
        return {"*"}
    try:
        path.lstat()
    except FileNotFoundError:
        return set()
    except OSError:
        return {"*"}
    try:
        raw = safe_read_file_bytes(str(path))
    except (FileTooLargeError, OSError):
        return {"*"}
    if raw is None:
        return {"*"}
    if not raw.removeprefix(b"\xef\xbb\xbf").strip():
        return set()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {"*"}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return set()
    return {str(n) for n in servers} | {mcp_server_alias(str(n)) for n in servers}
