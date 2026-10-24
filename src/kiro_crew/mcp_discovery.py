"""MCP server discovery — detects configured MCP servers and checks liveness.

Scans the agent config (``agents/defaults.json``) for ``mcpServers`` entries,
then optionally probes each server by spawning the command and sending an
MCP ``initialize`` handshake.

Used by the dashboard to show live MCP server badges and by the heartbeat
to auto-sync newly discovered servers into the agent config.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import ntpath
import os
import posixpath
import sys as _sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror, mcp_quarantine, platform_compat
from kiro_crew.env import denied_spec_env_keys, emit_env, spec_env_path
from kiro_crew.mcp_cleanup import mcp_entry_is_muted
from kiro_crew.mcp_discovery_config import (
    _load_agent_config,
    _load_mcp_json,
    _load_mcp_json_by_source,
    _spec_client_id,
    _spec_scopes,
    list_servers,
)
from kiro_crew.mcp_discovery_probe import _drop_temp_refusals, probe_server
from kiro_crew.mcp_discovery_probe_state import _prune_unresolvable, _sanitize_probe_error
from kiro_crew.mcp_discovery_server_info import _CC_SIDECAR_SURFACE, SCOPE_KIROCREW, McpServerInfo
from kiro_crew.mcp_provenance import ABSENT, resolve_write
from kiro_crew.user_json import has_json_comments, loads_user_json

logger = logging.getLogger(__name__)


# Warn once per crossing PER GATEWAY RUN, not per pass: the quarantine is re-read
# every pass, so warning on the STATE would reprint one line forever. This ledger is
# process memory while the quarantine is durable, so a crossing outliving a restart is
# announced again — right, because that run has told nobody. Pruning to what is
# quarantined now bounds it and self-heals it: a server must leave the store to re-cross.
_quarantine_warned: set[str] = set()


def _spawn_excluded() -> set[str]:
    """Servers to report from cache without spawning them again.

    The count comes from ``mcp_quarantine``, not a second counter here: that
    store is already the per-server consecutive-probe-failure ledger, already
    skips ``needs_auth``, and already has an operator reset. ``probe_all``'s
    caller folds each round's verdicts back into it, so this sees the previous
    pass. Reads a file, so callers run it off the loop; an unreadable store
    probes everything, because refusing would make one bad file a fleet outage.
    """
    try:
        snap = mcp_quarantine.snapshot()
    except Exception:
        logger.debug("cannot read MCP quarantine state; probing all", exc_info=True)
        return set()
    excluded = {name for name, st in snap.items() if st.get("failing")}
    _quarantine_warned.intersection_update(excluded)
    for name in sorted(excluded):
        if name in _quarantine_warned:
            continue
        _quarantine_warned.add(name)
        logger.warning(
            "MCP server %s failed %d consecutive probes, so discovery will no longer "
            "spawn it. Fix it, then clear it from the MCP panel (POST "
            "/api/mcp/quarantine/clear) — the exclusion outlives a gateway restart; "
            "or set agent.mcp_quarantine_after_failures to 0 to stop quarantining.",
            name,
            snap[name].get("fails") or 0,
        )
    return excluded


# Cap how many MCP servers we probe concurrently.  Each probe spawns a
# subprocess (or opens a remote connection) and resolves DNS on the event
# loop's default executor; an unbounded fan-out across 25+ servers floods that
# pool during a network blip and stalls the loop.
PROBE_MAX_CONCURRENCY = 5
#: Public because the shareability pre-flight bounds its own fan-out by the same
#: number: those spawns land in this executor too, so two independent caps would
#: let one pass flood the pool the other is protecting.


def _spawn_fingerprint(server: McpServerInfo) -> tuple[str, ...] | None:
    """The process a local probe of *server* starts, as a key; None when it starts none.

    One server is often configured under several names (an IDE entry, an
    ``mcp.json`` entry, a renamed copy), and each name is its own row. Probing
    every name at once starts the same heavy process several times in the same
    instant, and those copies compete for CPU until each misses the probe
    timeout -- which the quarantine then counts against a server that works.
    ``probe_all`` therefore runs probes that share this key one after another.

    The key is command plus args, the process that gets started. Env is left out
    on purpose: two names that differ only in a token still start the same
    package, and that cost is what this key groups. Each name is still probed
    with its own env and gets its own verdict; only the timing changes. A
    remote server opens a connection rather than a process, so it gets no key.
    """
    if server.is_remote or not isinstance(server.command, str) or not server.command:
        return None
    # ``args`` is unvalidated config JSON, and this runs outside the per-server
    # ``gather``, so it must not raise: a non-iterable value keys by its repr.
    raw_args = server.args or []
    try:
        args = tuple(str(a) for a in raw_args)
    except TypeError:
        args = (repr(raw_args),)
    return (server.command, *args)


async def probe_all() -> list[McpServerInfo]:
    """Discover and probe all configured MCP servers (bounded concurrency).

    Consent-disabled rows are never SPAWNED: probing runs the server process,
    and a disabled server must not run until the user enables it.

    They are still RETURNED, as ``status="disabled"`` rows with no handshake
    behind them. This result replaces the dashboard's server list wholesale
    (the table, the Connections cards and the in-row sign-in all paint the
    probe response over ``GET /api/mcp``), so a shape that left disabled rows
    out made every disabled server vanish on the first probe and reappear on
    the next page load -- the IDE reads the same config and keeps them as greyed
    rows. The withheld rows are never handed to ``probe_server`` at
    all: the no-spawn guarantee here does not rest on that function's own
    refusal arm, which stays as the last line of defence for every other entry
    point. This filter is also what keeps ``_prune_unresolvable`` keyed to the
    servers a probe can actually resolve.
    """
    rows = list_servers()
    servers = [s for s in rows if not s.disabled]
    withheld = [s for s in rows if s.disabled]
    for s in withheld:
        # The row shape ``probe_server``'s refusal arm produces: an unprobed
        # ``disabled`` status, no stale failure text, and ``tools`` left as the
        # last real probe stored them (still worth showing). Nothing is written
        # to the probe cache -- no probe ran.
        s.status = "disabled"
        s.error = ""
        _drop_temp_refusals(s)
    # Keep the warn-once ledger bounded by the config rather than by config
    # churn: a command edited to a different missing binary must not retain the
    # superseded string. Runs before the early return so emptying the config
    # (or disabling every server) also clears it.
    #
    # `command` is whatever the config JSON held (`spec.get("command", "")`,
    # unvalidated), so a malformed entry can be a dict or list. Those are
    # unhashable and would abort this whole pass — not just their own server —
    # because this runs outside the per-server `gather`. Only string commands
    # can be ledger keys anyway, so skip the rest and let each malformed server
    # keep failing in isolation inside `probe_server`.
    _prune_unresolvable({(s.name, s.command) for s in servers if isinstance(s.command, str)})
    if not servers:
        return withheld
    # Per-call semaphore: bounds the fan-out within this discovery pass while
    # binding to the currently-running loop (avoids import-time loop capture).
    sem = asyncio.Semaphore(PROBE_MAX_CONCURRENCY)
    # One lock per started process (see ``_spawn_fingerprint``), so names that
    # start the same command and args are probed one at a time while other
    # servers still run in parallel. Taken BEFORE the semaphore: an alias waiting
    # on its sibling must not hold one of the shared slots while it waits.
    same_process: dict[tuple[str, ...], asyncio.Lock] = {}
    for s in servers:
        key = _spawn_fingerprint(s)
        if key is not None:
            same_process.setdefault(key, asyncio.Lock())
    excluded = await asyncio.to_thread(_spawn_excluded)

    async def _guarded(s: McpServerInfo) -> McpServerInfo:
        # Left out of the SPAWN set only, and still returned: callers judge
        # freshness by comparing returned names against their own cache, so a
        # dropped row reads as brand-new every request and re-arms this fan-out.
        #
        # Returned as ``outdated``, not with the failure ``list_servers`` merged
        # on, because no handshake was attempted and a row must not present a
        # stale observation as a current one. ``_quarantine_verdicts`` folds these
        # rows into the very count that decided the exclusion, under the rule that
        # only a status reporting an attempt may move the counter: a re-reported
        # ``error`` would inflate that count with no probe behind it, and a later
        # threshold rise could then never release the server. ``outdated`` with no
        # error is what ``_get_cached`` gives any entry lacking a fresh result, so
        # this says now what the row says anyway once the TTL lapses.
        if s.name in excluded:
            s.status = "outdated"
            s.error = ""
            _drop_temp_refusals(s)
            return s
        key = _spawn_fingerprint(s)
        if key is None:
            async with sem:
                return await probe_server(s)
        async with same_process[key], sem:
            return await probe_server(s)

    results = await asyncio.gather(
        *(_guarded(s) for s in servers),
        return_exceptions=True,
    )
    out: list[McpServerInfo] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            servers[i].status = "error"
            servers[i].error = _sanitize_probe_error(r)
            logger.warning("MCP probe failed [%s]: %s", servers[i].name, servers[i].error)
            out.append(servers[i])
        else:
            out.append(r)  # type: ignore[arg-type]
    for s in out:
        _note_denied_env(s)
    # Same order as ``list_servers`` reported, so the probe response and
    # ``GET /api/mcp`` list the same servers in the same sequence.
    by_name = {s.name: s for s in out}
    by_name.update((s.name, s) for s in withheld)
    return [by_name[s.name] for s in rows]


def _note_denied_env(server: McpServerInfo) -> None:
    """Name the policy-dropped env keys on a FAILED probe.

    The probe strips loader/interpreter injection keys before spawning, so a
    server configured through one of them (a Python server using
    ``env.PYTHONPATH`` is the realistic case) can fail here while working in a
    session, where kiro-cli spawns it with no launcher of ours in the chain.
    The sanitizer logs each drop, but the person reading a red badge is looking
    at the dashboard, not the gateway log — without this the row reads as a
    probe bug instead of a deliberate boundary. Only annotates errors: on a
    success the drop changed nothing worth reporting.
    """
    if server.status != "error" or not server.error:
        return
    dropped = denied_spec_env_keys(server.env or {})
    if not dropped:
        return
    server.error = (
        f"{server.error} (probe dropped declared env "
        f"{', '.join(sorted(dropped))}: these execute in the sandbox launcher "
        f"before confinement, so the probe cannot honour them — a session still does)"
    )


def _commands_diverged(source_cmd: str, agent_cmd: str) -> bool:
    """Compare MCP commands accounting for path resolution.

    The agent config stores resolved absolute paths (e.g.
    /home/user/.local/bin/deep-research) while mcp.json stores the
    short name (deep-research). These refer to the same binary and
    should not trigger a sync.

    That basename comparison is a "same program" guess with no liveness in it,
    so it holds only while the resolved path still names a runnable file. An
    agent entry pinned to a version-stamped absolute path keeps its basename
    after the directory that held it is removed; reporting that pin as unchanged
    leaves the server to fail at spawn time with no earlier warning. A pin that
    does not resolve is therefore divergence.
    """
    if source_cmd == agent_cmd:
        return False
    # Two RESOLVED paths for one binary, differing only in separator flavour or
    # case (``C:\tools\srv.exe`` vs ``C:/Tools/SRV.exe``). Windows itself treats
    # those as the same file, so comparing the strings re-syncs forever.
    if (
        platform_compat.IS_WINDOWS
        and _names_a_location(source_cmd)
        and _names_a_location(agent_cmd)
    ):
        if ntpath.normcase(ntpath.normpath(source_cmd)) == ntpath.normcase(
            ntpath.normpath(agent_cmd)
        ):
            return False
    # If one is an absolute resolved path of the other, they match. Test both
    # path flavors regardless of host OS: on Windows ``os.path is ntpath`` and
    # would treat a POSIX-absolute config path (/usr/bin/server) as relative,
    # so a resolved-vs-short pair authored on POSIX would spuriously read as
    # diverged and trigger an endless re-sync.
    if _names_a_location(agent_cmd) and _basenames_match(agent_cmd, source_cmd):
        # Only the AGENT side is probed for liveness. mcp.json holds what the
        # user authored; the agent entry holds what some past resolution pinned,
        # so only the pin can rot on its own. Probing the source instead would
        # keep re-proposing a sync that re-resolves the same absent name every
        # pass, which is the endless re-sync this branch was added to prevent.
        return _pinned_command_missing(agent_cmd)
    if _names_a_location(source_cmd) and _basenames_match(source_cmd, agent_cmd):
        return False
    return True


def _pinned_command_missing(cmd: str) -> bool:
    """True when *cmd* pins an absolute path that cannot be executed now.

    Probed only when *cmd* is ROOTED IN A NAMED VOLUME on this host, which is
    narrower than :func:`_names_a_location` in the two ways that matter:

    * A path in the other OS's spelling is not this host's to judge. POSIX
      rejects ``C:\\tools\\srv`` on its own, since ``posixpath.isabs`` is False
      for it.
    * ``ntpath.isabs`` accepts a DRIVELESS root -- ``\\tools\\srv``, and a POSIX
      ``/usr/bin/srv`` in an ``mcp.json`` carried onto Windows -- which resolves
      against whichever drive happens to be current, so the filesystem cannot
      answer for it either. The resolver never writes one (``shutil.which``
      returns a drive-qualified path there), so a driveless agent command is an
      authored spelling rather than a pin, and calling it stale would re-sync a
      portable config on every pass.

    The test is the resolver's own, ``isfile`` plus ``X_OK``, as
    ``agent._resolve_command`` applies it to an absolute command -- deliberately
    not ``shutil.which``, which can report a perfectly good file as unresolvable
    inside a user-namespace sandbox. Asking a different question than the writer
    would let this report a live pin as gone and re-sync it forever, which is the
    failure the basename comparison exists to avoid. Nothing here searches
    ``PATH``, so no unrelated binary of the same name can vouch for a pin that
    is gone.
    """
    if not os.path.isabs(cmd):
        return False
    if platform_compat.IS_WINDOWS and not ntpath.splitdrive(cmd)[0]:
        return False
    return not (os.path.isfile(cmd) and os.access(cmd, os.X_OK))


def _envs_agree(agent_env: dict, source_env: dict) -> bool:
    """True when the agent config's ``env`` already carries *source_env*.

    Every source key must be present in the agent entry, EXCEPT that ``PATH`` is
    compared through :func:`kiro_crew.env.spec_env_path` — the agent config
    stores the expanded effective PATH while mcp.json stores the fragment the
    user authored, exactly as it stores a resolved absolute command against a
    bare name (see :func:`_commands_diverged`). Comparing the raw strings would
    read every already-synced server as diverged and re-sync it forever.

    A superset is fine: keys the agent entry adds on its own (a managed pin) are
    not the source's business.
    """
    for key, val in source_env.items():
        current = agent_env.get(key)
        if key == "PATH" and val:
            if current == val or current == spec_env_path(val):
                continue
            return False
        if current != val:
            return False
    return True


def _names_a_location(cmd: str) -> bool:
    """True when *cmd* is a path rather than a bare ``PATH`` lookup name.

    Broader than :func:`_is_abs_any` by one Windows case: ``ntpath.isabs``
    rejects a DRIVELESS root (``\\tools\\srv``) because it is absolute only
    relative to the current drive — yet such a string still names a location
    whose basename is meaningful. A relative path (``bin/srv``, ``./srv``) is
    deliberately NOT a location for this purpose: it designates a specific file
    relative to the CWD, so it must not match an unrelated rooted path that
    merely shares a basename.
    """
    if _is_abs_any(cmd):
        return True
    return platform_compat.IS_WINDOWS and cmd[:1] in ("/", "\\")


def _basenames_match(resolved: str, bare: str) -> bool:
    """True when *resolved*'s basename names the same binary as *bare*.

    On Windows the resolver (``shutil.which``, via ``agent._resolve_command``)
    appends the extension as ``PATHEXT`` spells it — commonly UPPER case — so
    ``npx`` resolves to ``...\\npx.CMD``. An exact basename comparison therefore
    reports every stdio MCP server as diverged forever, and each discovery pass
    re-syncs it. Fold the executable suffix and the case, both of which Windows
    itself ignores. POSIX keeps the exact comparison: paths are case-sensitive
    there and an extension is part of the name.
    """
    name = _basename_any(resolved)
    if name == bare:
        return True
    if not platform_compat.IS_WINDOWS:
        return False
    name, bare = name.casefold(), bare.casefold()
    if name == bare:
        return True
    stem, ext = ntpath.splitext(name)
    return bool(ext) and ext in _win_exec_suffixes() and stem == bare


# Executable suffixes Windows appends when resolving a bare command name. Read
# live from ``PATHEXT`` so a host that customizes it is honored; the fallback
# mirrors the Windows default for the pathological case of it being unset.
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _win_exec_suffixes() -> frozenset[str]:
    """Lower-cased ``PATHEXT`` suffixes."""
    raw = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    return frozenset(
        s for s in (part.strip().casefold() for part in raw.split(os.pathsep)) if s.startswith(".")
    )


def _is_abs_any(cmd: str) -> bool:
    """True if ``cmd`` is absolute under POSIX or Windows path rules."""
    return posixpath.isabs(cmd) or ntpath.isabs(cmd)


def _basename_any(cmd: str) -> str:
    """Basename of ``cmd`` under whichever path flavor treats it as absolute.

    A backslash-bearing string takes the Windows flavour even when
    ``ntpath.isabs`` is False, which a DRIVELESS root (``\\tools\\srv``) is.
    ``posixpath.basename`` does not know ``\\`` is a separator, so it would
    return the whole string and the basename comparison could never match.
    """
    if ntpath.isabs(cmd) or "\\" in cmd:
        return ntpath.basename(cmd)
    return posixpath.basename(cmd)


def _declared_source_owned(spec: dict) -> dict[str, Any]:
    """The source-owned keys *spec* actually declares, verbatim.

    The key set is imported from ``agent`` rather than restated, so the sync
    trigger cannot drift from the merge again: a key ``_merge_source_owned``
    stops reconciling stops firing a sync in the same commit.
    """
    from kiro_crew.agent import _SOURCE_OWNED_MCP_KEYS  # circular import

    return {k: spec[k] for k in _SOURCE_OWNED_MCP_KEYS if k in spec}


def _source_owned_diverged(existing: dict, info: McpServerInfo) -> bool:
    """True when a key the source DECLARES disagrees with the generated entry.

    Only a declared key is compared, because only a declared key is guaranteed
    to converge: every scope's merge copies a declared value onto the entry,
    while a key the source RETIRED is popped by ``_merge_source_owned`` for the
    kiro-global and provider scopes but left in place by the ``dict.update``
    merge the kirocrew scope uses. Firing on a retired key would therefore offer
    a sync that repeats on every poll for a server declared in that scope alone.
    """
    return any(existing.get(key) != value for key, value in info.source_owned.items())


def discover_servers_to_sync() -> list[McpServerInfo]:
    """Find MCP servers in mcp.json that need syncing to the agent config.

    Returns new servers not yet in the agent config, plus existing servers
    whose source-owned transport fields have diverged from mcp.json.
    """
    agent_cfg = _load_agent_config()
    agent_mcp = agent_cfg.get("mcpServers", {})
    agent_names = set(agent_mcp.keys())
    mcp_servers = _load_mcp_json()

    out: list[McpServerInfo] = []
    for name, spec in mcp_servers.items():
        if not isinstance(spec, dict):
            continue
        # The launch predicate: a muted entry -- ``true`` or a non-boolean, read
        # fail-closed -- is never offered, because syncing it is how it would
        # reach the agent config the sessions load.
        if mcp_entry_is_muted(spec):
            continue
        info = McpServerInfo(
            name=name,
            command=spec.get("command", ""),
            args=spec.get("args"),
            env=spec.get("env") or {},
            url=spec.get("url", ""),
            headers=spec.get("headers") or {},
            scopes=_spec_scopes(spec),
            client_id=_spec_client_id(spec),
            source="discovered",
            source_owned=_declared_source_owned(spec),
        )
        if name not in agent_names:
            out.append(info)
        else:
            # Args divergence is intentionally excluded. The merge now adopts a
            # source's ``args`` together with its ``env`` onto an entry running
            # the same program (``mcp_sources._merge_command_unit``), but the
            # emitted args are ``${VAR}``-expanded (``expand_spec_args``) while
            # the source holds the raw form, so a raw comparison would re-offer
            # a sync on every poll for a server whose args name a variable. An
            # args-only edit lands on the next rebuild instead; the ``env``
            # comparison below is the one the merge converges on.
            existing = agent_mcp[name]
            if not isinstance(existing, dict):
                continue
            if info.is_remote:
                existing_headers = existing.get("headers") or {}
                # scopes/clientId are source-owned transport fields like url and
                # headers: a registry Connect that adds or changes them must
                # re-sync, or the agent config keeps authorizing the old shape.
                #
                # Reaching this set is not permission to rewrite anything: each
                # consumer guards its own surface, so the two that Kiro Crew does
                # not own -- the kiro-global file and the Claude Code sidecar --
                # decide for themselves what an existing entry allows.
                if (
                    existing.get("url", "") != info.url
                    or existing_headers != info.headers
                    or _spec_scopes(existing) != info.scopes
                    or _spec_client_id(existing) != info.client_id
                    or _source_owned_diverged(existing, info)
                ):
                    out.append(info)
                continue
            existing_env = existing.get("env", {})
            if not isinstance(existing_env, dict):
                existing_env = {}
            if (
                not _envs_agree(existing_env, info.env)
                or _commands_diverged(info.command, existing.get("command", ""))
                or _source_owned_diverged(existing, info)
            ):
                out.append(info)
    return out


def sync_to_agent_config(servers: list[McpServerInfo]) -> bool:
    """Sync discovered MCP servers into the agent config.

    Delegates to ``install_agent()`` — the single authoritative merge function
    that reads all source files (``~/.kiro/crew/mcp.json``,
    ``~/.kiro/settings/mcp.json``), merges them with correct priority, resolves
    commands, normalizes each spec's ``env`` (see ``env.emit_env``), and writes
    the final agent config. There is deliberately no second registration path:
    a ``kiro-cli mcp add`` subprocess here would buy cosmetic parity with
    ``kiro-cli mcp list`` at the cost of an unsynchronized second writer of the
    same file with its own (unnormalized) env serialization, whose writes
    ``install_agent()`` rewrites moments later.

    Returns True if any servers were synced.
    """
    from kiro_crew.agent import install_agent  # circular import

    install_agent()

    # Audit: log which servers triggered the config rebuild
    try:
        from kiro_crew.sel import sel  # circular import

        sel().log_api_access(
            caller="system",
            operation="mcp_server_config_sync",
            outcome="ok",
            source="agent",
            resources=", ".join(s.name for s in servers),
        )
    except Exception:
        logger.debug("SEL audit log failed for mcp_server_config_sync", exc_info=True)

    return bool(servers)


# One mutex for the whole discover→write sequence. Two dashboard handlers run
# it (``POST /api/mcp/sync`` and the sessions-restart pre-sync), and each write
# is a read-modify-write of shared files — unserialized, two concurrent runs
# can both discover, then interleave writes and drop each other's changes.
_SYNC_MUTEX = threading.Lock()


def sync_discovered_servers() -> list[McpServerInfo]:
    """Reconcile the consumed configs with the sources, in one serialized step.

    The single entry point for "make the consumed configs match the sources":
    the agent-config rebuild runs UNCONDITIONALLY — ``install_agent()`` is the
    idempotent reconciler, and skipping it when discovery reports no new or
    diverged servers misses the changes discovery deliberately does not
    report, e.g. a source entry gaining ``disabled: true`` (discovery skips
    disabled entries, but the rebuild is what removes the server from the
    agent config). The Claude Code sidecar write is additive-only, so it runs
    just for the discovered delta. Everything happens under one lock so
    concurrent callers cannot interleave. Blocking file I/O — call via
    ``asyncio.to_thread`` from a handler.

    Returns the servers discovery flagged (new or diverged; empty when none —
    which, deliberately, does not imply nothing was written).
    """
    with _SYNC_MUTEX:
        to_sync = discover_servers_to_sync()
        sync_to_agent_config(to_sync)
        if to_sync:
            register_servers_for_cc(to_sync)
        return to_sync


def kirocrew_managed_names() -> set[str]:
    """Server names the dashboard store owns.

    A usable dict under the ``kirocrew`` scope (``<data home>/mcp.json``) is the
    one signal that Kiro Crew manages a name -- the same discriminator the
    agent-spec emit path uses for its OAuth hints, so management means one thing
    everywhere.

    This is the store-side half of the ownership predicate and a NECESSARY
    precondition for every write to a config surface we do not own -- the
    kiro-global ``mcp.json``, the Claude Code ``~/.mcp.json`` sidecar. Discovery
    merges ALL scopes, so a name present only in a user's global file reaches the
    sync set exactly like a managed one; without the gate a Kiro Crew sync would
    rewrite a server the user configured by hand.

    It is deliberately NOT sufficient. Managing a NAME says nothing about who
    wrote a given ENTRY, and the two answers differ per file -- an entry can be
    ours in the kiro-global file and the user's in the sidecar. That half is
    :func:`kiro_crew.mcp_provenance.resolve_write`, which reads the marker on the
    entry itself. Keeping this function name-only is what lets a single set answer
    for every surface without silently answering for the wrong one.

    A malformed store value is skipped for the same reason the merge skips it: it
    contributed nothing, so it cannot make the name ours.
    """
    by_source = _load_mcp_json_by_source()
    return {
        name for name, spec in by_source.get(SCOPE_KIROCREW, {}).items() if isinstance(spec, dict)
    }


def register_servers_for_cc(
    servers: list[McpServerInfo],
    mcp_json_path: Path | None = None,
) -> bool:
    """Register MCP servers in CC format (.mcp.json).

    Adds entries without removing existing ones. CC-side complement
    to sync_to_agent_config() which handles kiro-side registration.

    A remote entry is rewritten only when it carries our authorship marker --
    see the loop below. So is a stdio entry: the marker records who wrote an
    entry, which no transport makes knowable on its own.

    Returns True if any servers were added or updated.
    """
    if mcp_json_path is None:
        mcp_json_path = Path.home() / ".mcp.json"

    existing: dict = {}
    if mcp_json_path.is_file():
        try:
            text = mcp_json_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if has_json_comments(text):
            # Written back as plain JSON below, which would drop the comments.
            logger.warning(
                "Not registering MCP servers in %s: it has comments or trailing commas",
                mcp_json_path,
            )
            return False
        try:
            existing = loads_user_json(text)
        except json.JSONDecodeError:
            existing = {}

    mcp = existing.setdefault("mcpServers", {})
    changed = False
    # OAuth hints ride along when a remote is first registered, and only for a
    # name we own -- see kirocrew_managed_names.
    _managed = kirocrew_managed_names()

    for s in servers:
        if s.is_remote:
            entry: dict = {"url": s.url}
            if s.headers:
                entry["headers"] = s.headers
            if s.name in _managed:
                if s.scopes:
                    entry["scopes"] = list(s.scopes)
                if s.client_id:
                    entry["clientId"] = s.client_id
        else:
            entry = {"command": s.command, "args": s.args or [], "type": "stdio"}
            if s.env:
                # The sidecar is consumed by the external ``claude`` CLI, whose
                # env semantics this repo cannot observe — emit through the
                # shared normalization point so a declared PATH is complete
                # under the strictest (replace-per-key) reading. See emit_env.
                entry["env"] = emit_env(s.env)

        # This writer rebuilds an entry from scratch, so rewriting one we did not
        # author would drop the fields it does not reconstruct. The marker says
        # which ones those are: an entry we wrote re-syncs (its url or command
        # legitimately moves), an unmarked entry is the user's and stays add-only,
        # exactly as this surface behaved before the marker existed. The gate is
        # per ENTRY, not per transport -- a ``command`` makes authorship no more
        # knowable than a ``url`` does, and this loop rewrites a diverging stdio
        # entry in place, so a user's own server sharing a managed name reaches
        # the same collision. ``ABSENT`` rather than ``None``: a hand-edited file
        # can hold ``null`` under a name, and that occupies the name.
        resolved = resolve_write(
            name=s.name,
            on_disk=mcp.get(s.name, ABSENT),
            candidate=entry,
            store_managed=s.name in _managed,
            surface=_CC_SIDECAR_SURFACE,
        )
        if resolved is None:
            continue
        entry = resolved

        if s.name not in mcp or mcp[s.name] != entry:
            mcp[s.name] = entry
            changed = True
            logger.info("Registered MCP server for CC: %s", s.name)

    if changed:
        mcp_json_path.parent.mkdir(parents=True, exist_ok=True)
        from kiro_crew.agent import (
            _atomic_json_write,  # circular import: agent imports mcp_discovery
        )

        _atomic_json_write(mcp_json_path, existing)

    return changed


# This module is the facade of the MCP discovery owners, and every name is reached
# through it as ``kiro_crew.mcp_discovery.<name>``: a read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# module holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module.
# It keeps the discovery passes: ``probe_all`` and the discover-and-sync step with its
# Claude Code sidecar. New work goes to the owner of its responsibility:
#
#   ``mcp_discovery_server_info`` -- ``McpServerInfo``, its scopes and sources, and its redaction.
#   ``mcp_discovery_probe_state`` -- Protocol revision, timeouts, warning ledgers, probe cache.
#   ``mcp_discovery_managed`` -- Kiro Crew's managed servers: tables, shareability, repair.
#   ``mcp_discovery_config`` -- Reading the configured servers, and ``list_servers``.
#   ``mcp_discovery_remote`` -- Probing a remote (HTTP) server.
#   ``mcp_discovery_probe`` -- ``probe_server``: probing one server, a local one sandboxed.

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.mcp_discovery_server_info",
    "kiro_crew.mcp_discovery_probe_state",
    "kiro_crew.mcp_discovery_managed",
    "kiro_crew.mcp_discovery_config",
    "kiro_crew.mcp_discovery_remote",
    "kiro_crew.mcp_discovery_probe",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.mcp_discovery_config import (  # noqa: F401
        _MALFORMED_TRANSPORT_MAX,
        _display_transport,
        _mcp_names_from_file,
        _server_from_spec,
        configured_mcp_aliases,
    )
    from kiro_crew.mcp_discovery_managed import (  # noqa: F401
        _MANAGED_SERVER_NAMES,
        _MANAGED_SERVER_SUBCOMMANDS,
        _MANAGED_SERVER_TOOL_MODULES,
        _MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD,
        _MANAGED_SERVERS_CALLER_AWARE,
        _cached_managed_invocation,
        _fix_stale_managed_command,
        _is_first_party_managed_argv,
        _managed_tools_in_process,
        _resolved_managed_invocation,
        managed_server_is_session_bound,
        managed_tool_names_for_policy,
    )
    from kiro_crew.mcp_discovery_probe import (  # noqa: F401
        _MAX_BANNER_LINES,
        _probe_on_private_loop,
        _read_stdio_jsonrpc_response,
        _redact_temp_refusal_text,
        _temp_refusal_records,
    )
    from kiro_crew.mcp_discovery_probe_state import (  # noqa: F401
        _ISO_DATE,
        _PROBE_ERROR_MAX_CHARS,
        _PROBE_TEARDOWN_WAIT_SECS,
        _PROBE_TIMEOUT_SECS,
        _PROBE_TTL_SECS,
        MCP_CLIENT_PROTOCOL_VERSION,
        _cache_probe,
        _clear_unresolvable,
        _get_cached,
        _get_probe_timeout,
        _hashable_text,
        _managed_in_process_warned,
        _probe_cache,
        _probe_identity,
        _probe_sandbox_warned,
        _ProbeResult,
        _unresolvable_warned,
        _unresolved_error,
        _warn_managed_in_process_once,
        _warn_probe_sandbox_unavailable_once,
        _warn_unresolvable_once,
        cached_probe_is_current,
        downgrade_protocol_version,
        negotiated_protocol_version,
        probe_metadata,
        reset_unresolvable_warnings,
    )
    from kiro_crew.mcp_discovery_remote import (  # noqa: F401
        _CHALLENGE_PARAM_RE,
        _MAX_CHALLENGE_LEN,
        _MAX_REDIRECT_TARGET_LEN,
        _expand_header_placeholders,
        _header_reference_unresolved,
        _is_bearer_challenge,
        _needs_authorization,
        _probe_remote,
        _read_jsonrpc_response,
        _redirect_error,
        _runtime_grant_present,
    )
    from kiro_crew.mcp_discovery_server_info import (  # noqa: F401
        _CORE_SCOPE_ORDER,
        _MCP_AUTH_VALUE_RE,
        _MCP_CREDENTIAL_SUFFIX_MIN_LENGTH,
        _MCP_CREDENTIAL_UNANCHORED_MIN_LENGTH,
        _MCP_JSON_PATHS,
        _MCP_SOURCES,
        MCP_REDACTED_HEADER_VALUE,
        SCOPE_CC_GLOBAL,
        SCOPE_KIRO_GLOBAL,
        _extra_scope_sources,
        _extra_scopes,
        _mcp_credential_token_pattern,
        _mcp_json_paths,
        _mcp_sources,
        _scope_priority,
        redact_mcp_error,
        redact_mcp_headers,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.config.paths import (  # noqa: F401
        data_home,
        kiro_agents_dir,
    )
    from kiro_crew.env import (  # noqa: F401
        MCP_PATH_HINT,
        describe_search_path,
        expand_spec_args,
        mcp_search_path,
        resolved_command_casing,
        sanitize_spec_env,
        spec_path_key,
    )
    from kiro_crew.executors import (  # noqa: F401
        mcp_probe_executor,
    )
    from kiro_crew.hooks import (  # noqa: F401
        safe_read_file,
    )
    from kiro_crew.json_line import (  # noqa: F401
        parse_json_object_line,
    )
    from kiro_crew.mcp_cleanup import (  # noqa: F401
        invalid_disabled_flag,
        mcp_entry_is_registry_governed,
        warn_invalid_disabled,
    )
    from kiro_crew.mcp_gateway.hashing import (  # noqa: F401
        hash_command,
        hash_effective_env,
    )
    from kiro_crew.mcp_gateway.secret_uri import (  # noqa: F401
        remote_header_secret_ref_error,
    )
    from kiro_crew.mcp_grant import (  # noqa: F401
        grant_observed,
    )
    from kiro_crew.mcp_provenance import (  # noqa: F401
        WITHHELD_KEY,
        is_withheld_mute,
    )
    from kiro_crew.mcp_utils import (  # noqa: F401
        kiro_entry_client_id,
        kiro_entry_scopes,
        mcp_server_alias,
    )
    from kiro_crew.sandbox import (  # noqa: F401
        CANONICAL_TEMP_KEYS,
        SandboxUnavailableError,
        classify_declared_temp_env,
        classify_declared_temp_path,
        create_subprocess_limited,
        declared_temp_refusal_reasons,
        format_declared_temp_refusals,
        sandboxed_spawn_argv,
        sandboxed_spawn_argv_async,
    )
    from kiro_crew.security import (  # noqa: F401
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.user_json import (  # noqa: F401
        loads_user_jsonc,
    )
