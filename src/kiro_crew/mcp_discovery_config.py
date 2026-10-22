"""Reading the configured MCP servers.

The agent config and the ``mcp.json`` sources are loaded per scope, each spec
becomes an ``McpServerInfo``, and ``list_servers`` merges them in scope priority
and attaches each server's cached probe verdict.

:mod:`kiro_crew.mcp_discovery` stays the import path and the patch surface for
every name here; that module's comment lists the owners.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.hooks import safe_read_file
from kiro_crew.mcp_cleanup import (
    invalid_disabled_flag,
    mcp_entry_is_muted,
    mcp_entry_is_registry_governed,
    warn_invalid_disabled,
)
from kiro_crew.mcp_discovery_managed import _fix_stale_managed_command
from kiro_crew.mcp_discovery_probe_state import _get_cached, _probe_identity, probe_metadata
from kiro_crew.mcp_discovery_server_info import (
    SCOPE_CC_GLOBAL,
    SCOPE_KIRO_GLOBAL,
    SCOPE_KIROCREW,
    McpServerInfo,
    _extra_scope_sources,
    _extra_scopes,
    _mcp_json_paths,
    _mcp_sources,
    _scope_priority,
)
from kiro_crew.mcp_provenance import WITHHELD_KEY, is_withheld_mute
from kiro_crew.mcp_utils import kiro_entry_client_id, kiro_entry_scopes, mcp_server_alias
from kiro_crew.user_json import loads_user_jsonc

logger = logging.getLogger("kiro_crew.mcp_discovery")


def _load_agent_config(*, user_home: Path | None = None) -> dict[str, Any]:
    """Load the agent config to read mcpServers.

    Merges mcpServers from project-dir (if set), bundled defaults.json,
    AND the installed kirocrew.json — because defaults.json may not have
    mcpServers (they're added dynamically at install time by ``kirocrew setup``).
    """
    configs: list[dict[str, Any]] = []

    # Project-dir override (development)
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj:
        p = Path(proj) / "agents" / "defaults.json"
        if p.is_file():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    configs.append(loaded)
            except (json.JSONDecodeError, OSError):
                pass

    # Bundled defaults.json (fallback when no project-dir)
    if not configs:
        bundled = Path(__file__).resolve().parent / "config" / "defaults.json"
        if bundled.is_file():
            try:
                loaded = json.loads(bundled.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    configs.append(loaded)
            except (json.JSONDecodeError, OSError):
                pass

    # Installed agent config (always check for mcpServers)
    from kiro_crew.agent import AGENT_FILENAME  # circular import: agent imports mcp_discovery
    from kiro_crew.agent_discovery import _read_agent_spec

    installed = (
        (user_home / ".kiro" / "agents") if user_home else kiro_agents_dir()
    ) / AGENT_FILENAME
    if installed.is_file():
        # The agents dir is user-writable and shared with other tools, so this
        # goes through the hardened agent-spec reader (size cap,
        # sensitive-symlink screen, non-object rejection, SEL denial event)
        # rather than a bare read_text + json.loads. It also closes a hole the
        # old ``except`` could not: ``encoding="utf-8"`` on a non-UTF-8 spec
        # raises UnicodeDecodeError, a ValueError rather than an OSError or a
        # JSONDecodeError, so it escaped this handler entirely. ``None``
        # degrades as absent, exactly as the swallowed exceptions did.
        loaded = _read_agent_spec(
            installed,
            operation="mcp_discovery_agent_config",
            source="unknown",
        )
        if loaded is not None:
            configs.append(loaded)

    if not configs:
        return {}

    # Merge: use first config as base, merge mcpServers from all sources
    merged = dict(configs[0])
    first_servers = merged.get("mcpServers")
    mcp: dict[str, Any] = dict(first_servers) if isinstance(first_servers, dict) else {}
    for cfg in configs[1:]:
        servers = cfg.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, spec in servers.items():
            if name not in mcp:
                mcp[name] = spec
    merged["mcpServers"] = mcp
    return merged


def _mcp_names_from_file(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = loads_user_jsonc(safe_read_file(str(path)))
    except (json.JSONDecodeError, OSError, TypeError):
        return set()
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return set()
    return {name for name in servers if isinstance(name, str)}


def configured_mcp_aliases(*, data_home: Path, user_home: Path) -> set[str]:
    """Return canonical names reserved by every effective Kiro Crew MCP source."""
    names: set[str] = set()
    agent_servers = _load_agent_config(user_home=user_home).get("mcpServers", {})
    if isinstance(agent_servers, dict):
        names.update(name for name in agent_servers if isinstance(name, str))

    names.update(_mcp_names_from_file(data_home / "mcp.json"))
    names.update(_mcp_names_from_file(user_home / ".kiro" / "settings" / "mcp.json"))

    from kiro_crew.platform.context import current_context, safe_context_call

    extra_servers: dict[str, dict] = safe_context_call(
        lambda: dict(current_context().mcp_tooling.extra_mcp_servers()),
        fallback_factory=dict,
        log_message="extra_mcp_servers lookup failed; collision scan using core sources only",
    )
    names.update(name for name in extra_servers if isinstance(name, str))
    for scope in _extra_scopes():
        names.update(_mcp_names_from_file(scope.global_json))
        if scope.agent_mcp_file is not None:
            names.update(_mcp_names_from_file(scope.agent_mcp_file))
    return {mcp_server_alias(name) for name in names}


def _load_mcp_json_by_source() -> dict[str, dict[str, Any]]:
    """Return ``{scope: {name: spec}}`` keyed by scope name.

    Reads every well-known MCP config location and bucketizes servers by
    their origin scope.  Unlike :func:`_load_mcp_json`, no cross-source
    merging happens — callers that need per-scope presence use this.

    Iterates the core :data:`_MCP_SOURCES` (path + scope pairs) PLUS any
    provider scopes contributed by the platform seam
    (:func:`_extra_scope_sources`), so discovery scans exactly what
    apply/uninstall manage and paths/scope labels can never drift.  When tests
    monkeypatch :data:`_MCP_JSON_PATHS` to a shorter tuple for isolation, the
    corresponding scopes are recovered by looking up each patched path; any
    unknown path falls back to :data:`SCOPE_KIROCREW`.
    """
    result: dict[str, dict[str, Any]] = {
        SCOPE_KIROCREW: {},
        SCOPE_KIRO_GLOBAL: {},
        SCOPE_CC_GLOBAL: {},
    }
    extra_sources = _extra_scope_sources()
    for _, scope in extra_sources:
        result.setdefault(scope, {})
    path_to_scope = {p: scope for p, scope in _mcp_sources()}
    path_to_scope.update({p: scope for p, scope in extra_sources})
    scan_paths: tuple[Path, ...] = tuple(_mcp_json_paths()) + tuple(p for p, _ in extra_sources)
    for p in scan_paths:
        scope = path_to_scope.get(p, SCOPE_KIROCREW)
        if not p.is_file():
            continue
        try:
            # JSONC-tolerant: Kiro itself reads these files with comments, so a
            # commented-out server must not drop every other server in the file.
            data = loads_user_jsonc(safe_read_file(str(p)))
        except (json.JSONDecodeError, OSError) as exc:
            # PermissionError (subclass of OSError) is raised by
            # safe_read_file when is_sensitive_path() blocks the read.
            logger.warning("Failed to load MCP config from %s: %s", p, exc)
            continue
        if not isinstance(data, dict):
            continue
        servers = data.get("mcpServers", {})
        if isinstance(servers, dict):
            # Merge instead of overwriting — if two paths resolve to the
            # same scope (legitimate duplicates, or tests that monkeypatch
            # _MCP_JSON_PATHS with fallback-scoped paths), setdefault keeps
            # first-wins semantics within the scope.
            bucket = result[scope]
            for name, spec in servers.items():
                bucket.setdefault(name, spec)
    return result


def _load_mcp_json() -> dict[str, Any]:
    """Load and merge mcpServers from all well-known mcp.json locations.

    Earlier paths take precedence — if the same server name appears in
    multiple files, the first definition wins (via ``setdefault``).
    Retained for callers that only need a merged view; use
    :func:`_load_mcp_json_by_source` when per-scope presence matters.
    """
    merged: dict[str, Any] = {}
    by_source = _load_mcp_json_by_source()
    # Iteration order = priority (setdefault is a no-op once populated):
    # kirocrew-specific file > kiro global > any seam provider globals.
    # Matches rebuild_agent_config's merge order in agent_parts/spec_rebuild.py.
    for scope in _scope_priority(by_source):
        for name, spec in by_source.get(scope, {}).items():
            merged.setdefault(name, spec)
    return merged


def _spec_scopes(spec: dict) -> list[str]:
    """Requested OAuth scopes from a spec, dropping anything malformed.

    On-disk specs are untrusted (hand-edited files, other tools), so a
    non-list or a list with non-string members degrades to "no scopes"
    rather than propagating a bad shape into the agent config.

    Reads kiro-cli's ``oauthScopes`` as well as Kiro Crew's internal ``scopes``:
    files we emit for kiro-cli carry the former, so a discovery pass that knew
    only the latter would report a scoped server as unscoped.
    """
    return kiro_entry_scopes(spec)


def _spec_client_id(spec: dict) -> str:
    """Public OAuth client id from a spec, or "" when absent/malformed.

    Accepts kiro-cli's nested ``oauth.clientId`` as well as the internal
    top-level key, for the same round-trip reason as ``_spec_scopes``.
    """
    return kiro_entry_client_id(spec)


#: Longest text a malformed transport value is shown as; the row is a display,
#: not a copy of the config, and the Edit action opens the real JSON.
_MALFORMED_TRANSPORT_MAX = 120


def _display_transport(value: Any) -> str:
    """*value* as the table may render it: a string as is, anything else as
    bounded, redacted JSON text.

    ``command`` and ``url`` reach the page as React children
    (``{s.command || s.url}``), and a value that is not a string -- a hand-edited
    ``"command": {"not": "a string"}`` -- throws there and takes the whole
    Connections page down. A disabled entry is the case that matters: it is
    never probed, so nothing else ever inspects the value, and the row exists
    precisely to show the user a config that needs their attention. The text is
    the value's own JSON so the malformation is visible; an object can carry
    what a string never did here -- a header map with a token, a URL with a
    credential in it -- so the site-wide scanners (``redact_credentials``,
    ``redact_exfiltration_urls``, the same pair ``redact_mcp_error`` runs) go
    over the COMPLETE text first, and the display cap applies to the redacted
    result, never the other way round.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = type(value).__name__
    from kiro_crew.security import redact

    text = redact(text)
    if len(text) > _MALFORMED_TRANSPORT_MAX:
        text = text[:_MALFORMED_TRANSPORT_MAX] + "..."
    return text


def _server_from_spec(name: str, spec: dict, source: str) -> McpServerInfo:
    return McpServerInfo(
        name=name,
        command=_display_transport(spec.get("command", "")),
        args=spec.get("args", []),
        env=spec.get("env", {}),
        url=_display_transport(spec.get("url", "")),
        headers=spec.get("headers", {}),
        scopes=_spec_scopes(spec),
        client_id=_spec_client_id(spec),
        source=source,
        registry_pointer=(
            mcp_entry_is_registry_governed(spec) and not spec.get("command") and not spec.get("url")
        ),
    )


def list_servers() -> list[McpServerInfo]:
    """Return all known MCP servers from agent config + mcp.json + CC global.

    Merges cached probe results so status/tools survive across requests.
    Populates ``presence`` for each server with booleans for whether the
    server appears in each of the three scope config files.

    Servers that live only in a provider global (e.g. a user added one via
    ``kiro-cli mcp add`` or directly to ``~/.claude.json``) still show up
    on the dashboard so users get a full inventory from one page.
    """
    servers: dict[str, McpServerInfo] = {}
    disabled_in_agent: set[str] = set()
    # Canonical names a RAW scope switches off with a LITERAL ``true``. A
    # disabled row whose name is not here is off only because of non-boolean
    # values (read fail-closed), and says so (``disabled_reason``, step 3d).
    # The agent config is deliberately NOT a source for this: that file is the
    # rebuild's output, and the rebuild writes ``disabled: true`` for a raw value
    # it read fail-closed (``"false"``, ``null``) -- counting its ``true`` would
    # dress a provider-global ``"disabled": "false"`` up as a deliberate disable
    # after one rebuild. Every row this function marks disabled is marked from a
    # raw scope's spec (step 3c and the introduce arms), so the raw scopes are
    # the whole provenance.
    boolean_true: set[str] = set()

    # 1. From agent config (mcpServers key)
    agent_cfg = _load_agent_config()
    for name, spec in agent_cfg.get("mcpServers", {}).items():
        if isinstance(spec, dict):
            # The rebuild's own withheld mute is not the user switching the
            # server off: it lifts once the server is routed, and routing is
            # turned on from this table, so the row is listed as declared.
            if mcp_entry_is_muted(spec) and not is_withheld_mute(spec):
                disabled_in_agent.add(name)
            else:
                if is_withheld_mute(spec):
                    spec = {k: v for k, v in spec.items() if k not in (WITHHELD_KEY, "disabled")}
                # Re-resolve stale managed MCP server paths at runtime
                _fix_stale_managed_command(name, spec)
                servers[name] = _server_from_spec(name, spec, "agent")

    # 2. From scope-tagged mcp.json sources, in priority order so highest-
    #    priority scope populates disabled_tools first and lower scopes
    #    don't overwrite it.  Order = kirocrew-specific > Kiro global >
    #    any seam provider globals, matching rebuild_agent_config.
    by_source = _load_mcp_json_by_source()
    disabled_tools_claimed: set[str] = set()
    for scope in _scope_priority(by_source):
        for name, spec in by_source.get(scope, {}).items():
            if not isinstance(spec, dict):
                continue
            # ``disabled`` is a boolean or absent. Anything else (``"false"``,
            # ``1``, ``"yes"``) is a config error, reported once per
            # (server, value shape) by the shared reporter in ``mcp_cleanup``
            # -- the same one the agent rebuild calls, so the operator hears it
            # once from whichever path ran first. The decision below is
            # ``mcp_entry_is_muted(spec)``, the launch predicate the gateway and
            # the session projections read, which is FAIL-CLOSED: the entry takes
            # the disabled arms exactly as a literal ``true`` would, so the row
            # the table shows as Disabled is the server no session starts.
            # Reading the string as "enabled" here would have listed -- and
            # spawned -- a server the user tried to silence.
            invalid, flag = invalid_disabled_flag(spec)
            if invalid:
                warn_invalid_disabled(name, flag, scope)
            disabled = mcp_entry_is_muted(spec)
            # Introduce the server first (if new) so the disabledTools
            # carry below applies to both new and existing entries.  Without
            # this ordering, the highest-priority scope's disabledTools is
            # dropped for new servers because `name in servers` is False
            # before insertion, letting a lower-priority scope's value
            # overwrite the (empty) default on a later iteration.
            if not disabled and name not in servers and name not in disabled_in_agent:
                servers[name] = _server_from_spec(name, spec, "mcp.json")
            elif scope == SCOPE_KIROCREW and disabled and name not in servers:
                # Consent-disabled entries (registry installs and custom adds
                # land with ``disabled: true`` until the user enables them)
                # live ONLY in the Kiro Crew scope. They must still get a row:
                # the enable action in this table IS the consent step the
                # install flow points at — an invisible server can never be
                # consented to. The row is marked disabled and excluded from
                # probing (see probe_all).  ``disabled_in_agent`` is NOT
                # consulted here: config sync mirrors this very entry into the
                # agent file as ``disabled: true``, so the agent-side flag is
                # the same signal, not an independent user override.
                info = _server_from_spec(name, spec, "mcp.json")
                info.disabled = True
                servers[name] = info
            elif disabled and name not in servers and name in disabled_in_agent:
                # Switched off from the dashboard: ``/api/mcp/toggle`` writes
                # ``disabled: true`` into the scope that holds the server AND
                # onto the agent entry — the agent-side marker is what stops a
                # running kiro-cli session's server. Step 1 skipped that agent
                # entry, so without this arm the row would vanish the moment the
                # user disabled it, leaving nothing to re-enable from. The row is
                # built from the agent entry, which holds the full spec: the
                # scope's copy may be the bare ``{"disabled": true}`` stub the
                # toggle creates for a server it found nowhere else.
                info = _server_from_spec(name, agent_cfg["mcpServers"][name], "agent")
                info.disabled = True
                servers[name] = info
            elif disabled and name not in servers:
                # Disabled in a SHARED scope (the Kiro-global ``mcp.json`` the IDE
                # edits, or a provider global) and held by no other source: the
                # two arms above cover a Kiro Crew store entry and an agent entry
                # stamped by the toggle, and nothing else ever introduces this
                # one. Discovery skips disabled entries and the rebuild never adds
                # a disabled shared server to the agent config, so without this
                # arm the server is listed only while a pre-disable agent entry
                # survives and vanishes on the first sync -- read as data loss,
                # not as a filter, because the IDE keeps showing the same entry
                # as a greyed "Disabled" row. The row is marked disabled
                # and never probed: ``probe_server`` refuses on the flag.
                # ``disabled`` is the launch predicate's read, so a non-boolean
                # ``"disabled": "false"`` lands here too, reported above and
                # told apart on the row by ``disabled_reason`` (step 3d).
                info = _server_from_spec(name, spec, "mcp.json")
                info.disabled = True
                servers[name] = info

            # Per-tool disables: first-scope-wins.  Use "disabledTools" in
            # spec (key presence) rather than truthiness so an explicit
            # "disabledTools": [] (user intent: "all tools enabled") is
            # respected and prevents lower-priority scopes from overwriting.
            if name in servers and "disabledTools" in spec and name not in disabled_tools_claimed:
                servers[name].disabled_tools = spec.get("disabledTools", [])
                disabled_tools_claimed.add(name)

    # 3. Compute per-scope presence.
    #
    #    MC presence = "will this load in Kiro Crew sessions after the next
    #    rebuild".  Because ``rebuild_agent_config`` inherits from both
    #    provider globals, a server present in any scope source (or already
    #    in the current merged agent config) counts as MC green unless
    #    Kiro Crew has an explicit ``disabled: true`` override.
    #    Kiro/CC presence = raw membership in that provider's global config.
    agent_names = set(agent_cfg.get("mcpServers", {}).keys())
    kirocrew_own = by_source.get(SCOPE_KIROCREW, {})
    # Every scope other than kirocrew is a raw-membership global scope
    # (Kiro/CC/any seam-contributed provider). Derive them from by_source so a
    # companion scope is reported in presence rather than omitted — an omitted
    # scope is read as False by the frontend and DELETED on the next apply.
    global_scopes = [s for s in _scope_priority(by_source) if s != SCOPE_KIROCREW]
    for name, server in servers.items():
        mc_disabled = mcp_entry_is_muted(kirocrew_own.get(name))
        in_any_source = name in agent_names or any(
            name in by_source.get(scope, {}) for scope in by_source
        )
        presence: dict[str, bool] = {SCOPE_KIROCREW: in_any_source and not mc_disabled}
        for scope in global_scopes:
            presence[scope] = name in by_source.get(scope, {})
        server.presence = presence

    # 3b. Canonicalize: fold a server keyed by a slash/colon name into its
    #     mcp_server_alias() form so a server registered under BOTH its raw key
    #     (e.g. "npm:@playwright/mcp") and its alias ("playwright-mcp") is
    #     reported as one logical server instead of two rows / two probes. This
    #     is read-only canonicalization — no config file is modified. Slash-free
    #     names alias to themselves, so non-scoped servers are unaffected. When
    #     both forms are present, presence flags are unioned and the entry whose
    #     own key is already the canonical alias is kept as the representative.
    canonical_servers: dict[str, McpServerInfo] = {}
    for name, server in servers.items():
        canon = mcp_server_alias(name)
        rep = canonical_servers.get(canon)
        if rep is None:
            server.name = canon
            canonical_servers[canon] = server
            continue
        union = {
            scope: bool(rep.presence.get(scope)) or bool(server.presence.get(scope))
            for scope in rep.presence
        }
        chosen = server if name == canon else rep
        chosen.name = canon
        chosen.presence = union
        canonical_servers[canon] = chosen
    servers = canonical_servers

    # 3c. Consent is per SCOPE: a ``disabled: true`` ANYWHERE withholds the
    #     spawn, not only the branch above that INTRODUCES a Kiro-Crew-scope row
    #     which exists nowhere else. ``/api/mcp/toggle`` writes the flag into the
    #     Kiro-global ``mcp.json``, and a row that step 1 already introduced from
    #     the agent config would otherwise keep ``disabled = False`` and stay
    #     probeable: the user switches a server off in the dashboard and
    #     discovery still spawns it.
    #
    #     Runs AFTER 3b so both sides are canonical. Scope dicts are keyed by the
    #     RAW name, so a server configured as ``npm:@playwright/mcp`` is reported
    #     as ``playwright-mcp`` — matching before canonicalization would miss the
    #     raw-keyed disable whenever the agent config retained the canonical row.
    #
    #     Only ever SETS the flag, so scope priority is irrelevant: one disable
    #     is enough, and no scope can re-enable what another disabled. The flag
    #     now IS the safety property (``probe_server`` refuses on it), which is
    #     why populating it correctly matters more than when each caller filtered
    #     rows for itself. Read with the launch predicate, so a non-boolean value
    #     on an existing row's shared entry flags the row (fail-closed) exactly
    #     as the sessions refuse to start it. A mute from any scope but the Kiro
    #     Crew store is also recorded as ``disabled_in_shared``: the table's
    #     stamping holds only two of the scope maps and needs this verdict to
    #     tell a provider-global disable from a store-only one.
    for scope, scope_specs in by_source.items():
        for raw_name, spec in scope_specs.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("disabled") is True:
                boolean_true.add(mcp_server_alias(raw_name))
            if not mcp_entry_is_muted(spec):
                continue
            row = servers.get(mcp_server_alias(raw_name))
            if row is not None:
                row.disabled = True
                if scope != SCOPE_KIROCREW:
                    row.disabled_in_shared = True

    # 3d. WHY a row is off, for the table. A row that no RAW scope switches off
    #     with a literal ``true`` is off only because of non-boolean values, read
    #     fail-closed above; it says so, because the operator's fix is to repair
    #     the value where it sits, not to flip a switch the row does not have.
    #     ``boolean_true`` is filled from the raw scopes alone -- see its
    #     definition for why the agent config's mirror must not count.
    for name, row in servers.items():
        if row.disabled and name not in boolean_true:
            row.disabled_reason = "invalid"

    # 4. Merge cached probe results
    for s in servers.values():
        # The identity is computed from the row just built out of CURRENT config,
        # so an entry probed under an edited command, url or env is not served.
        identity = _probe_identity(s)
        status, tools, error, probed_at, probe_mode = _get_cached(s.name, identity)
        s.status = status
        s.tools = tools
        s.error = error
        s.probed_at = probed_at
        s.probe_mode = probe_mode
        # Read through ``probe_metadata`` rather than widening ``_get_cached``'s
        # tuple, and taken even from an expired entry: a server that demanded
        # OAuth an hour ago still demands it, so the wording should not regress
        # to the vaguer form the moment the TTL lapses.
        #
        # Expired is not the same as mismatched, though, and this read has to
        # apply the identity check too: a remote moved from endpoint A to
        # endpoint B would otherwise render B as "sign-in required" on the
        # strength of A's handshake, which is the one wording the user cannot
        # tell apart from a real challenge.
        cached = probe_metadata(s.name)
        if cached is not None and cached.identity == identity:
            s.auth_challenge = cached.auth_challenge
            s.auth_grant_present = cached.auth_grant_present
            s.temp_refusals = [dict(r) for r in cached.temp_refusals]
        # A registry pointer never has a probe result of its own, so without this
        # it reads "unknown" until the first probe pass -- the same row the probe
        # reports, stated without waiting for one. See ``probe_server``.
        if s.registry_pointer and not s.command and not s.disabled:
            s.status = "registry"
            s.error = ""

    return list(servers.values())
