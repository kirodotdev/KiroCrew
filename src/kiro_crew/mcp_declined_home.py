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

Every session start builds ONE decision record (:class:`_Decision`) from the
one spec it loads; delivery returns that record's servers and the SEL audit
(:func:`_record_decision`) records that record, so neither reads a spec of its
own. The record travels with the session: a later mode switch is judged against
it (:func:`replacement_refusal`) and never rebuilds it.

**Removal once each instance has a private agents dir.** This module is a
stopgap: an instance with its own agents dir always writes its own spec and is
never refused. When that fix lands, delete together:

* this module and ``test/test_mcp_declined_home.py``;
* ``_kiro_session_servers`` in ``acp/client_mcp_array.py`` (the
  ``AcpClient`` session/new append) and its entry in
  ``test/test_client_bindings.py``;
* ``AcpRuntime._with_declined_home_servers`` and its create/load call sites in
  ``acp/runtime.py``, plus the ``declined_decision`` it carries to
  ``_activate_mode_bracketed`` and the session handle,
  ``AcpRuntime._refuse_if_delivery_exceeds`` and
  ``AcpRuntime.refuse_mode_beyond_delivery``;
* ``AcpSessionHandle.declined_decision`` and its checks in
  ``AcpSessionHandle.set_mode`` (``acp/session_handle.py``);
* the ``kas_tool_grants`` call in ``KasHarness.session_extras``
  (``acp/harness/kas.py``);
* the ``refresh_projection`` / ``clear_projection`` calls in ``agent.py``;
* the "Refused shared agent home: the session channel" section of
  ``docs/architecture/mcp.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Collection
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, NamedTuple

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
_MEMO: dict[str, Any] = {
    "fingerprint": None,
    "servers": {},
    "native": frozenset(),
    "refusals": (),
}
#: What the audit last recorded, per change and not per session: ``supplied``
#: holds, per channel, the names a decision supplied; ``refusals`` holds, per
#: refusal kind, the identity it last recorded.
_AUDITED: dict[str, Any] = {"supplied": {}, "refusals": {}}
_MEMO_LOCK = threading.Lock()


#: Most skipped-server names one diagnostic line names, and the longest name it
#: shows; the rest are counted, not retained.
MAX_LOGGED_NAMES = 16
MAX_LOGGED_NAME_CHARS = 64


#: The name a refusal of the whole delivery records: which servers it covered is
#: what could not be computed.
_EVERY = ("every mcp.json server",)


class _Skipped:
    """A count of skipped servers plus a bounded sample of their names.

    ``digest`` covers every full name added, in order, so two sets whose shown
    samples agree are still told apart.
    """

    __slots__ = ("_hash", "count", "sample")

    def __init__(self) -> None:
        self.count = 0
        self.sample: list[str] = []
        self._hash = hashlib.sha256()

    def add(self, alias: str) -> None:
        self.count += 1
        self._hash.update(alias.encode("utf-8", "surrogatepass") + b"\0")
        if len(self.sample) < MAX_LOGGED_NAMES:
            self.sample.append(alias[:MAX_LOGGED_NAME_CHARS])

    def digest(self) -> str:
        return self._hash.hexdigest()

    def render(self) -> str:
        more = self.count - len(self.sample)
        return ", ".join(self.sample) + (f" (+{more} more)" if more > 0 else "")


class _Refusal(NamedTuple):
    """One kind of refusal in a decision: what it refused and why."""

    kind: str
    reason: str
    names: _Skipped


class _Decision(NamedTuple):
    """What one session start decided for one channel, built ONCE at enforcement
    time from the spec that session loads.

    Delivery returns exactly ``supplied`` and the SEL audit records exactly this
    record (:func:`_record_decision`); neither reads a spec, so what a session
    gets and what the trail says it got cannot come from two observations. Every
    refusal kind the channel can make is present, an empty one included, so a
    refusal that cleared is recorded again when it returns.
    """

    scope: str
    supplied: tuple[str, ...]
    refusals: tuple[_Refusal, ...]


#: Why the governance ceiling withholds a server from kiro delivery.
_CEILING_REASON = (
    "the shared spec's allowedTools pre-approve them beyond this instance's governance ceiling"
)
_UNREADABLE_REASON = "the shared spec is unreadable"


def _compute_projection() -> (
    tuple[dict[str, dict[str, Any]], bool, frozenset[str], tuple[_Refusal, ...]]
):
    """The projection, whether any source command failed to resolve, the
    projected names KAS mounts natively under that same name, and the servers
    the projection itself refused.

    The third value is what :func:`kas_tool_grants` may grant: see
    :func:`_kas_native_names`. The fourth is carried into every kiro decision
    (:func:`_kiro_decision`), which is what records it; nothing here audits.

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
    refusals = (
        _Refusal("remote", "a remote server cannot be delivered at session level", remote),
        _Refusal("oversized", "the entry is past the session-level projection bounds", oversized),
        _Refusal(
            "restricted", "a session-level element cannot carry its disabledTools", restricted
        ),
    )
    return (
        out,
        bool(resolved.unresolved),
        _kas_native_names(sources, mounted, out),
        refusals,
    )


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


def _refresh() -> tuple[dict[str, dict[str, Any]], frozenset[str], tuple[_Refusal, ...]]:
    """The memoized projection, its KAS-native names and the projection's own
    refusals, recomputed when stale. Records nothing: a session start's decision
    does (:func:`_record_decision`)."""
    fingerprint = _source_fingerprint()
    with _MEMO_LOCK:
        hit = _MEMO["fingerprint"] == fingerprint and _commands_still_runnable(_MEMO["servers"])
        if hit:
            servers, native, refusals = dict(_MEMO["servers"]), _MEMO["native"], _MEMO["refusals"]
    if not hit:
        servers, unresolved, native, refusals = _compute_projection()
        with _MEMO_LOCK:
            # A command that did not resolve may resolve later without any source
            # or PATH change (an interpreter installed in place), so such a pass is
            # not memoized: the next session start re-resolves, as a written
            # rebuild would.
            _MEMO["fingerprint"] = None if unresolved else fingerprint
            _MEMO["servers"] = servers
            _MEMO["native"] = native
            _MEMO["refusals"] = refusals
    return dict(servers), native, refusals


_KIRO_SCOPE = "kiro session-level delivery"
_KAS_SCOPE = "KAS session tool grants"


def _skipped(aliases: Collection[str]) -> _Skipped:
    names = _Skipped()
    for alias in sorted(aliases):
        names.add(alias)
    return names


def _record_decision(decision: _Decision) -> None:
    """The SEL audit of one decision: every refusal it holds, then what it supplied.

    The ONLY place either is recorded, and it reads nothing but *decision*, so the
    trail states what the session was given. Each is recorded per change, not per
    session. Never raises.
    """
    try:
        for refusal in decision.refusals:
            _audit_refusal(refusal.kind, refusal.names, refusal.reason, decision.scope)
        _audit_supplied(decision.scope, decision.supplied)
    except Exception:
        logger.warning("refused-home decision audit failed", exc_info=True)


def _audit_refusal(kind: str, names: _Skipped, reason: str, scope: str) -> None:
    """The ``denied`` SEL record for one refusal kind of a decision.

    Written when what *kind* refuses changed since its last record, so a refusal
    that repeats on every session start is recorded per change; a warning names
    it each time where the decision is built. The identity is marked recorded
    only once the write succeeded, so a failed write is retried by the next
    identical decision. Never raises.
    """
    # The full refusal identity, never the truncated sample shown in the record,
    # held per channel: the same kind on kiro and on KAS are two refusals.
    key = (names.count, names.digest(), reason, scope)
    with _MEMO_LOCK:
        seen = _AUDITED["refusals"]
        if seen.get((scope, kind), (0, _skipped(()).digest(), reason, scope)) == key:
            return
    if names.count and not _record_withheld(names, reason, scope):
        return
    with _MEMO_LOCK:
        _AUDITED["refusals"][(scope, kind)] = key


def _record_withheld(names: _Skipped, reason: str, scope: str) -> bool:
    """Write one ``denied`` record; whether the write succeeded. Never raises."""
    try:
        from kiro_crew import agent as agent_mod  # circular at module scope

        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_declined_home_withheld",
            outcome="denied",
            source="install_agent",
            resources=f"{names.render()} withheld from {scope}",
            error=reason,
        )
    except Exception:
        logger.warning("refused-home withheld audit record failed", exc_info=True)
        return False
    return True


def _audit_supplied(scope: str, supplied: Collection[str]) -> None:
    """One SEL record per change to what a channel's sessions are supplied, as a
    written spec's ``mcp_tools_added`` would record its tool-surface change --
    including a change that withdraws servers, so the trail shows removals too.
    The new set becomes the baseline only once the write succeeded, so a failed
    write is retried by the next identical decision. Never raises."""
    now = frozenset(supplied)
    with _MEMO_LOCK:
        previous = _AUDITED["supplied"].get(scope, frozenset())
        if previous == now:
            return
    names = _skipped(now)
    withdrawn = _skipped(previous - now)
    shown = names.render() if names.count else "no mcp.json servers"
    detail = f"; withdrawn: {withdrawn.render()}" if withdrawn.count else ""
    logger.info(
        "Shared agent home is not writable from this data home; supplying %d "
        "mcp.json server(s) to %s directly: %s%s",
        names.count,
        scope,
        shown,
        detail,
    )
    try:
        from kiro_crew import agent as agent_mod  # circular at module scope

        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_declined_home_supplied",
            outcome="ok",
            source="install_agent",
            resources=f"{shown} supplied to {scope}{detail} (refused shared home)",
        )
    except Exception:
        logger.warning("refused-home supply audit record failed", exc_info=True)
        return
    with _MEMO_LOCK:
        _AUDITED["supplied"][scope] = now


def clear_projection() -> None:
    """Forget the projection once this instance owns its spec again."""
    with _MEMO_LOCK:
        _MEMO["fingerprint"] = None
        _MEMO["servers"] = {}
        _MEMO["native"] = frozenset()
        _MEMO["refusals"] = ()
        _AUDITED["supplied"] = {}
        _AUDITED["refusals"] = {}


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
    return _spec_view(spec)


def _spec_view(spec: Any) -> tuple[set[str], list[str]] | None:
    """What *spec* mounts and pre-approves; ``None`` if it is not a usable spec."""
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


#: ``session_servers(spec=...)`` default: no prepared view, so the session loads
#: the shared spec file itself and the check reads that file.
LIVE_SPEC: Any = object()


def session_spec(projection: Any, agent: str | None) -> Any:
    """The spec a kiro session under *projection* loads, for ``session_servers``.

    With a prepared native view the session runs that frozen copy, so delivery is
    checked against it; a projection with no view for *agent* answers ``None``,
    which delivers nothing. Without a projection the session reads the file.
    """
    if projection is None:
        return LIVE_SPEC
    return projection.specs.get(agent or PRIMARY_AGENT)


def session_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    present: Collection[str] = (),
    spec: Any = LIVE_SPEC,
) -> list[dict[str, Any]]:
    """ACP ``session/new`` elements for this session's undelivered servers.

    ``present`` is every name the caller's array already carries (the broker
    stubs); a server under that name, raw or aliased, is never added again.
    Empty -- the pre-existing behaviour -- unless ALL of these hold: the session
    runs the user-level primary agent, the shared spec write is refused right
    now, no registry ceiling applies, and *spec* -- the one the session loads,
    see :func:`session_spec` -- is readable. Blocking
    file I/O; callers are already off the event loop. Never raises.
    """
    return session_delivery(agent, work_dir=work_dir, present=present, spec=spec)[0]


def session_delivery(
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    present: Collection[str] = (),
    spec: Any = LIVE_SPEC,
) -> tuple[list[dict[str, Any]], _Decision | None]:
    """:func:`session_servers` plus the decision record they came from.

    A caller that later activates a replacement spec on the same session keeps
    the record and hands it to :func:`replacement_refusal`, so the switch is
    judged against what the session was given rather than by deciding again.
    ``None`` when no decision was made (the gates above returned early).
    Never raises.
    """
    try:
        return _session_servers(agent, work_dir=work_dir, present=present, spec=spec)
    except Exception:
        logger.warning("session-level mcp.json delivery failed; delivering none", exc_info=True)
        failed = _failed_decision(_KIRO_SCOPE, "kiro_error", _KIRO_ERROR_REASON)
        _record_decision(failed)
        return [], failed


#: Why a session holding a decision may not activate a replacement spec.
_REPLACEMENT_REASON = (
    "the replacement spec's allowedTools pre-approve servers this session was "
    "delivered beyond this instance's governance ceiling"
)
_REPLACEMENT_UNREADABLE_REASON = (
    "the replacement spec is unreadable, so the approvals it gives the servers "
    "this session was delivered cannot be checked"
)
_REPLACEMENT_SCOPE = "a kiro session's mode switch"


def replacement_refusal(
    decision: _Decision | None, projection: Any, agent: str | None
) -> str | None:
    """Why a session started under *decision* must not switch to *agent*; ``None`` if it may.

    *decision* is the record :func:`session_delivery` returned at session start
    and is NOT rebuilt: what the session was delivered and refused stays exactly
    what that record says, and so does its audit. A mode switch activates
    *agent*'s spec -- *projection*'s frozen view of it, or the file when there is
    no projection -- without delivering anything,
    so the one question left is whether that spec pre-approves a server the
    record supplied beyond this instance's governance ceiling. If it does, or it
    cannot be read, the caller ends the session before activation and one
    ``denied`` SEL record names the servers. Fails closed; never raises.
    """
    if decision is None or not decision.supplied:
        return None
    try:
        if projection is not None:
            view = _spec_view(session_spec(projection, agent))
        elif (agent or PRIMARY_AGENT) == PRIMARY_AGENT:
            view = _shared_spec_view()
        else:
            # Without a projection another agent's spec is a file this module
            # never reads, so its approvals cannot be checked.
            view = None
        if view is None:
            exceeds, reason = _skipped(decision.supplied), _REPLACEMENT_UNREADABLE_REASON
        else:
            _declared, allowed = view
            exceeds = _skipped(
                [a for a in decision.supplied if _inherited_approval_exceeds_ceiling(a, allowed)]
            )
            reason = _REPLACEMENT_REASON
    except Exception:
        logger.warning("refused-home replacement check failed; refusing", exc_info=True)
        exceeds, reason = _skipped(decision.supplied), _REPLACEMENT_UNREADABLE_REASON
    if not exceeds.count:
        return None
    logger.warning("ending a kiro session at its mode switch: %s (%s)", exceeds.render(), reason)
    _record_withheld(exceeds, reason, _REPLACEMENT_SCOPE)
    return (
        f"This session was given {exceeds.render()} from mcp.json because this "
        f"instance cannot write the shared agent spec, and {reason}. The session was "
        "ended rather than run those servers' tools without approval."
    )


_KIRO_ERROR_REASON = "the session-level delivery failed"
_KAS_ERROR_REASON = "the tool grant failed"
_KAS_WORKSPACE_REASON = (
    "the session's workspace mcp.json declares the same name, so KAS may mount "
    "that checkout's server under it"
)
_KAS_FOREIGN_REASON = (
    "KAS mounts only ~/.kiro/settings/mcp.json servers, under their own name, so a "
    "grant would not reach this entry"
)


def _failed_decision(scope: str, kind: str, reason: str) -> _Decision:
    """A decision that refused everything because it could not be computed."""
    return _Decision(scope, (), (_Refusal(kind, reason, _skipped(_EVERY)),))


def _session_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None,
    present: Collection[str],
    spec: Any = LIVE_SPEC,
) -> tuple[list[dict[str, Any]], _Decision | None]:
    from kiro_crew.agent import (  # circular at module scope
        _decline_shared_agent_home,
        _mcp_registry_mode,
        _project_shadow_of,
    )

    if (agent or PRIMARY_AGENT) != PRIMARY_AGENT:
        return [], None
    if work_dir and (
        _project_shadow_of(PRIMARY_AGENT, work_dir, markdown_specs=False, dispatchable_only=True)
        is not None
    ):
        # The checkout declares its own agent of this name: the session is not
        # running the user-level spec this projection supplements.
        return [], None
    if _decline_shared_agent_home(audit=False) is None:
        # The instance writes its own spec, which already mounts every server.
        return [], None
    if _mcp_registry_mode():
        return [], None
    projection, _native, refused = _refresh()
    decision = _kiro_decision(projection, refused, spec=spec, present=present)
    _record_decision(decision)
    elements = [
        {
            "name": alias,
            "command": projection[alias]["command"],
            "args": list(projection[alias]["args"]),
            "env": _env_pairs(projection[alias]["env"]),
        }
        for alias in decision.supplied
    ]
    return elements, decision


def _kiro_decision(
    projection: dict[str, dict[str, Any]],
    refused: tuple[_Refusal, ...],
    *,
    spec: Any,
    present: Collection[str],
) -> _Decision:
    """What a kiro session start is supplied and refused, from ONE read of the spec
    the session loads -- the only spec read for a kiro session.

    The mounted names and the inherited approvals come from that spec: the native
    view's frozen copy when one was prepared, so a later edit to the shared file
    decides neither what runs under the copy nor what the trail records.
    """
    view = _shared_spec_view() if spec is LIVE_SPEC else _spec_view(spec)
    unreadable = _Skipped()
    ceiling = _Skipped()
    supplied: list[str] = []
    if view is None:
        # Cannot tell what the spec already mounts; delivering could register a
        # server twice, so deliver nothing.
        for alias in sorted(projection):
            unreadable.add(alias)
    else:
        declared, allowed = view
        taken = declared | {mcp_server_alias(str(n)) for n in present} | set(present)
        for alias in sorted(projection):
            if alias in taken:
                continue
            if _inherited_approval_exceeds_ceiling(alias, allowed):
                ceiling.add(alias)
                continue
            supplied.append(alias)
    if ceiling.count:
        logger.warning(
            "not delivering %d mcp.json server(s) to kiro sessions: %s (%s)",
            ceiling.count,
            ceiling.render(),
            _CEILING_REASON,
        )
    return _Decision(
        _KIRO_SCOPE,
        tuple(supplied),
        (
            *refused,
            _Refusal("ceiling", _CEILING_REASON, ceiling),
            _Refusal("unreadable_spec", _UNREADABLE_REASON, unreadable),
            _Refusal("kiro_error", _KIRO_ERROR_REASON, _Skipped()),
        ),
    )


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
        _record_decision(_failed_decision(_KAS_SCOPE, "kas_error", _KAS_ERROR_REASON))
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
    projection, native, refused = _refresh()
    declared = _declared_names(spec)
    if declared is None:
        return []
    decision = _kas_decision(
        projection,
        native,
        declared,
        tools,
        refused,
        workspace=_workspace_names(work_dir),
        present=present,
    )
    _record_decision(decision)
    return [f"@{name}" for name in decision.supplied]


def _kas_decision(
    projection: dict[str, dict[str, Any]],
    native: frozenset[str],
    declared: set[str],
    tools: list[Any],
    refused: tuple[_Refusal, ...],
    *,
    workspace: set[str],
    present: Collection[str],
) -> _Decision:
    """What a KAS session start is granted and refused, from the spec the caller
    loaded (its ``tools`` and mounted names, passed in) -- no spec is read here.

    The projection's own refusals (*refused*) are carried in, as on kiro: a
    server the projection refused is refused on KAS too, and only this record
    can say so."""
    taken = declared | {mcp_server_alias(str(n)) for n in present} | set(present)
    _, named = parse_tools_refs(tools)
    supplied: list[str] = []
    withheld = _Skipped()
    foreign = _Skipped()
    for name in sorted(projection):
        if name in taken or _spec_names_server(named, name):
            # A spec that names the server in ANY form already decided what it
            # grants: a whole-server ``@name`` on top of ``@name/read_mail`` would
            # widen the operator's per-tool allowlist to every tool.
            continue
        if name not in native:
            # KAS does not mount this entry under this name, so a grant would
            # reach a different server or none.
            foreign.add(name)
            continue
        if "*" in workspace or name in workspace:
            withheld.add(name)
            continue
        supplied.append(name)
    if withheld.count:
        logger.warning(
            "Not granting %d mcp.json server(s) to KAS sessions: the session's "
            "workspace mcp.json declares the same name, so KAS may mount that "
            "checkout's server under it: %s",
            withheld.count,
            withheld.render(),
        )
    if foreign.count:
        logger.warning(
            "Not granting %d mcp.json server(s) to KAS sessions: %s: %s",
            foreign.count,
            _KAS_FOREIGN_REASON,
            foreign.render(),
        )
    return _Decision(
        _KAS_SCOPE,
        tuple(supplied),
        (
            *refused,
            _Refusal("kas_workspace", _KAS_WORKSPACE_REASON, withheld),
            _Refusal("kas_not_native", _KAS_FOREIGN_REASON, foreign),
            _Refusal("kas_error", _KAS_ERROR_REASON, _Skipped()),
        ),
    )


def _spec_names_server(named: Collection[str], alias: str) -> bool:
    """Whether a ``tools`` server segment (from :func:`parse_tools_refs`) names *alias*.

    Every spelling counts -- ``@alias``, ``@alias/<tool>`` and a glob segment
    such as ``@out*/read_mail`` -- matched raw or by its alias. A glob errs
    toward "named", which withholds a grant rather than widening one.
    """
    for server in named:
        if server == alias or mcp_server_alias(server) == alias:
            return True
        if _is_glob(server) and fnmatchcase(alias, server):
            return True
    return False


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
