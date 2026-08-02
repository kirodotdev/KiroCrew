"""Rewrite kiro agent JSON so MCP servers route through the broker.

The rewriter reads ``~/.kiro/agents/*.json`` and writes modified copies into
the overlay directory (``<config_dir>/mcp-gateway/agents/``). The host
filesystem remains untouched — the broker stubs in these specs are injected
into each kiro-cli session over ACP ``session/new``, which outranks the
same-named entry in the agent spec (see ``session_servers.py``).

Servers in :data:`UNPOOLABLE_SERVERS` are left unwrapped because they bind
to ``KIROCREW_SESSION_KEY`` and cannot be safely shared across sessions.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_gateway.hashing import hash_command, is_secret_env_key
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

# Reserved for MCP servers that explicitly opt out of broker routing even
# when they could support it (e.g. dev/diagnostic servers that want the
# operator to see one process per session). The preferred signalling path is
# a backend NOT advertising ``kirocrew.caller-identity`` in its initialize
# response — gatewayd then refuses to pool it. This hardcoded set exists
# only for servers that cannot be changed in lockstep (e.g. third-party MCPs
# shipped by teams that haven't adopted the caller-identity extension yet).
UNPOOLABLE_SERVERS: frozenset[str] = frozenset()

# Marker field set on rewritten MCP entries so repeat runs are idempotent.
_WRAPPER_MARKER = "_mc_mcp_gateway_wrapped"


# Argument separator for the stub's ``--target-args`` flag. `|` is
# printable, preserved through argv, and not legal in a kiro MCP command
# path. If a real MCP arg contains `|`, override via stub's
# ``--target-args-sep`` flag (not used here; not a problem in practice).
_TARGET_ARGS_SEP = "|"

#: The stub is launched as a module by the interpreter running KiroCrew.
#: ``sys.executable`` is baked into the overlay rather than resolved at
#: launch time because kiro-cli strips env when it spawns MCP
#: subprocesses, so neither a propagated var nor a ``python3`` on PATH
#: that can import ``kiro_crew`` is guaranteed.
_STUB_MODULE = "kiro_crew.mcp_gateway.stub"


def _build_stub_entry(
    *,
    stubs_dir: Path,
    server_name: str,
    agent_name: str,
    original: dict[str, Any],
    socket_path: Path,
    work_dir: Path,
    sandbox_mode: str,
    approval_mode: str,
    sidecars_written: set[str] | None = None,
) -> dict[str, Any]:
    """Return the rewritten ``mcpServers[name]`` entry.

    Preserves ``autoApprove`` on the wrapped entry so kiro-cli still honours
    it at the UI layer. ``env`` is cleared on the wrapper — the stub passes
    env separately through its flags so the gateway can hash the
    post-substitution env into the PoolKey.
    """
    target_command = original.get("command", "")
    target_args: list[str] = [str(a) for a in original.get("args", []) or []]
    # ``~/.kiro/agents/*.json`` is hand-editable, so ``env`` can legally parse as
    # a non-dict (e.g. ``"env": [{}]``). Normalize to {} rather than trusting the
    # annotation: the secret-key scan below calls ``str.startswith`` on every
    # key, so a list of dicts would raise AttributeError out of
    # ``_build_stub_entry`` and abort the ENTIRE rewrite pass — disabling pooling
    # for every agent because one spec was malformed.
    _declared_env = original.get("env", {}) or {}
    env_pairs: dict[str, Any] = _declared_env if isinstance(_declared_env, dict) else {}
    if _declared_env and not isinstance(_declared_env, dict):
        logger.warning(
            "rewriter: server %r for agent %r has a non-object 'env' (%s); "
            "ignoring it",
            server_name, agent_name, type(_declared_env).__name__,
        )
    auto_approve: list[str] = list(original.get("autoApprove", []) or [])

    # Resolve bare command names to absolute paths. gatewayd spawns the backend
    # outside kiro-cli's PATH, so a
    # bare command like "slack-mcp" fails with ENOENT on ``Command::spawn``.
    # Search the spec env PATH then the host PATH. Leave unresolved bare
    # names as-is (gatewayd will error and the stub falls back) so we
    # don't silently upgrade broken specs.
    if target_command and not os.path.isabs(target_command):
        env_path = env_pairs.get("PATH", "") if isinstance(env_pairs, dict) else ""
        search_path = os.pathsep.join(
            filter(None, [env_path, os.environ.get("PATH", "")])
        )
        resolved = shutil.which(target_command, path=search_path)
        if resolved:
            target_command = resolved
        else:
            logger.warning(
                "rewriter: could not resolve MCP command %r for server %r; "
                "leaving as bare name (gatewayd will likely ENOENT)",
                target_command, server_name,
            )

    stub_args: list[str] = [
        "--server", server_name,
        "--agent", agent_name,
        "--target-command", target_command,
        # Use ``=`` so argparse treats the `|`-joined value as the flag's
        # value even when it contains `--` (e.g. `--skill-paths|...`).
        f"--target-args={_TARGET_ARGS_SEP.join(target_args)}",
        "--sandbox-mode", sandbox_mode,
        "--work-dir", str(work_dir),
        "--approval-mode", approval_mode,
        "--socket", str(socket_path),
    ]
    if env_pairs:
        secret_key_count = sum(1 for k in env_pairs if is_secret_env_key(k))
        if forward_declared_env_enabled():
            # Forwarding is ON: the non-secret keys ARE applied to the pooled
            # backend (gatewayd merges them at spawn). Only the rotating-secret
            # keys remain unappliable, because they are excluded from
            # ``effective_env_hash`` — co-tenants of one backend can disagree on
            # their values, so there is no single correct value to apply.
            if secret_key_count:
                # Log NO value derived from the env block — not the key names,
                # not the matched prefixes, and not the count. CodeQL taints any
                # expression computed by iterating a secret-bearing env block
                # (clear-text logging of sensitive information), and the server
                # + agent names are enough for the operator to find the spec.
                logger.warning(
                    "rewriter: pooled server %r for agent %r declares "
                    "rotating-secret env key(s) that are NOT applied to the "
                    "shared pooled backend — they are excluded from the PoolKey, "
                    "so co-tenant sessions may disagree on the value. The backend "
                    "must read them from disk, or set poolable:false.",
                    server_name, agent_name,
                )
        else:
            # Forwarding is OFF (the default): the declared env is folded into
            # the PoolKey hash (so differing-env sessions never share a backend)
            # but is NOT applied to the pooled backend — gatewayd spawns it with
            # the daemon's own scrubbed environment. A server that genuinely
            # depends on its declared env will misbehave when pooled.
            logger.warning(
                "rewriter: pooled server %r for agent %r declares a non-empty env "
                "(%d keys); the declared env is NOT applied to the shared pooled "
                "backend (spawned with the daemon's scrubbed env). Enable "
                "mcp_gateway.forward_declared_env to apply the non-secret keys, "
                "or set poolable:false if this server depends on that env.",
                server_name, agent_name, len(env_pairs),
            )
        # JSON-encode env so values containing ',' or '=' round-trip
        # intact. A prior CSV serialisation ``K=V,K2=V2`` silently
        # truncated any value with a ',' in it — e.g. JAVA_OPTS='-Xmx1g,-Xms512m'
        # — which is a real risk since ``~/.kiro/agents/*.json`` is
        # user-editable. Stub's parser mirrors this (see ``_parse_env_json``).
        # Write env to a 0600 sidecar rather than onto argv: env blocks in
        # ~/.kiro/agents/*.json routinely hold tokens/API keys, and argv is
        # world-readable via /proc/<pid>/cmdline. The stub reads --env-file to
        # fold the declared env into the PoolKey hash, so two agents that differ
        # solely by a server's env get separate backends; when
        # ``mcp_gateway.forward_declared_env`` is enabled gatewayd ALSO reads
        # this sidecar at spawn and applies its non-secret keys to the backend.
        env_dir = env_sidecar_dir_for_stubs(stubs_dir)
        # make_owner_only_dir, not mkdir + chmod(0o700): the mode argument is
        # inert on Windows, where the DACL is the only carrier of access, so a
        # bare chmod left the directory holding credential sidecars readable by
        # every local principal. Also tightens a directory created before this
        # guarantee existed.
        platform_compat.make_owner_only_dir(env_dir)
        # env_sidecar_name() and not a sanitize-each-component-then-join rule:
        # joining sanitized components with a single '.' does fix the
        # ('agent-a', 'server-b.c') vs ('agent-a.b', 'server-c') ambiguity, but
        # sanitization is itself lossy, so an agent declaring both 'foo.bar' and
        # 'foo_bar' still collides. The shared helper appends a digest of the RAW
        # components, which is injective, and gatewayd's reader recomputes that
        # same helper — so writer and reader can never disagree on the name.
        env_file = env_dir / env_sidecar_name(agent_name, server_name)
        if sidecars_written is not None:
            sidecars_written.add(env_file.name)
        wrote_sidecar = False
        try:
            # Protection BEFORE content, not after. The previous order wrote the
            # credentials with atomic_write(mode=0o600) -- inert on Windows --
            # and only then applied the DACL, so an icacls failure left a
            # readable file full of API keys on disk while the except clause
            # merely warned and the stub was still pointed at it. Applying the
            # descriptor to the temp file first means the secret never exists in
            # a readable file at all, and a failure happens before any secret
            # byte is written. os.replace preserves an explicit
            # (non-inherited) descriptor across the rename.
            fd, tmp = tempfile.mkstemp(
                prefix=f".{env_file.stem}-", suffix=".json", dir=str(env_dir)
            )
            fd_owned = True
            try:
                platform_compat.fchmod_safe(fd, 0o600)
                if not platform_compat.IS_POSIX:
                    platform_compat.restrict_to_owner(tmp)
                with os.fdopen(fd, "w") as fh:
                    # fdopen took ownership of the descriptor; its context
                    # manager closes it. Tracked so the finally below does not
                    # double-close (and does close it when an earlier step
                    # raised).
                    fd_owned = False
                    fh.write(json.dumps(env_pairs, sort_keys=True))
                os.replace(tmp, env_file)
                wrote_sidecar = True
            finally:
                if fd_owned:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                if not wrote_sidecar:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
        except OSError:
            logger.warning("rewriter: failed to write env sidecar %s", env_file)
        if wrote_sidecar:
            stub_args.extend(["--env-file", str(env_file)])
        else:
            # No protected sidecar, so nothing to point the stub at. In the
            # pooled path this only changes the PoolKey hash (the declared env
            # is never applied to a shared backend anyway -- see the warning
            # above), so the server simply gets its own partition. Passing a
            # path we failed to protect, or one that does not exist, would be
            # worse.
            logger.warning(
                "rewriter: pooling %r for agent %r without an env sidecar",
                server_name, agent_name,
            )
    if auto_approve:
        # JSON (not CSV): a tool identifier containing a ',' would split into
        # two names under CSV, changing the permission surface hashed into
        # autoapprove_set_hash. Same bug class already fixed for env. The stub's
        # _parse_auto_approve reads JSON (with a CSV back-compat fallback).
        stub_args.extend(["--auto-approve", json.dumps(sorted(auto_approve))])

    # Preserve operator-set passthrough fields (timeout, type,
    # initializationOptions, disabledTools, vendor keys, ...) that kiro-cli
    # honours; a fixed-shape return silently dropped them, so e.g. a declared
    # `timeout` was lost and a slow pooled backend timed out where the
    # un-pooled config did not. Override only the pooling-relevant keys below.
    wrapped: dict[str, Any] = {
        k: v
        for k, v in original.items()
        if k not in ("command", "args", "env", "poolable", "autoApprove", _WRAPPER_MARKER)
    }
    wrapped.update({
        _WRAPPER_MARKER: True,
        "command": sys.executable,
        # ``-m kiro_crew.mcp_gateway.stub`` leads; the stub's own flags follow.
        # channel_id is NOT here: the overlay is written once at startup and is
        # session-agnostic, so it is appended per session by
        # ``session_servers.pooled_session_servers`` at ACP injection time,
        # where the value is in scope.
        "args": ["-m", _STUB_MODULE, *stub_args],
        # autoApprove must stay on the wrapper — kiro-cli reads it at the
        # permission-prompt UI layer, separately from the backend.
        "autoApprove": auto_approve,
        # env cleared — the backend receives env via the gateway's spawn,
        # not via kiro-cli's subprocess environment.
        "env": {},
    })
    return wrapped


def _hashable_args(args_val: Any) -> tuple[str, ...]:
    """Coerce an agent-JSON ``args`` list into a hashable tuple of strings for
    the target-dedup key. A malformed ``args: [{...}]`` (list of objects) would
    otherwise leave unhashable dicts in ``tuple(args)`` and raise TypeError out
    of ``_rewrite_single_spec``, aborting the whole rewrite pass for every other
    agent. Stringifying non-string elements keeps one bad spec from breaking
    the rest."""
    if not isinstance(args_val, list):
        return ()
    return tuple(
        a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
        for a in args_val
    )


def _rewrite_single_spec(
    spec: dict[str, Any],
    *,
    stubs_dir: Path,
    socket_path: Path,
    work_dir: Path,
    sandbox_mode: str,
    approval_mode: str,
    poolable_servers: frozenset[str],
    inject_servers: dict[str, Any] | None = None,
    target_env: dict[str, str] | None = None,
    sidecars_written: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Return ``(new_spec, wrapped_count)``. Idempotent.

    ``inject_servers`` is a mapping of ``{name: raw_entry}`` of poolable
    servers sourced from the global ``settings/mcp.json`` that must be made
    available to *this* agent. Each is wrapped with **this agent's** name (so
    the stub carries the correct ``--agent`` identity) and added to the
    overlay unless the agent already declares a server of that name (the
    agent's own declaration always wins). This is how empty-``mcpServers``
    agents get pooled coverage WITHOUT relying on kiro-cli merging the global
    settings — which is what produced the duplicate, empty-``--agent`` stub.
    """
    agent_name = spec.get("name") or ""
    servers = spec.get("mcpServers") or {}
    if not isinstance(servers, dict):
        servers = {}
    inject = inject_servers or {}
    if not servers and not inject:
        return spec, 0

    new_servers: dict[str, Any] = {}
    # Launch signatures (command + args) of every server already wired into this
    # overlay. Used to skip injecting a poolable settings server whose resolved
    # target is identical to one already present under a different name — which
    # would otherwise spawn a duplicate backend (e.g. a slash-named server and
    # its slash-free alias both pointing at the same proxy command).
    seen_targets: set[tuple[str, tuple[str, ...]]] = set()
    wrapped = 0
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            new_servers[name] = entry
            continue
        declared_cmd = entry.get("command")
        if declared_cmd:
            seen_targets.add((declared_cmd, _hashable_args(entry.get("args"))))
        if name in UNPOOLABLE_SERVERS:
            # Leave unchanged — these bind to KIROCREW_SESSION_KEY.
            new_servers[name] = entry
            continue
        if entry.get(_WRAPPER_MARKER) is True:
            # Already wrapped (idempotency).
            new_servers[name] = entry
            wrapped += 1
            continue
        if "command" not in entry:
            # HTTP/SSE MCP entries — already shareable by nature, skip.
            new_servers[name] = entry
            continue
        if entry.get("disabled") is True:
            # Honour the user's mute: a server explicitly disabled in the agent
            # spec must never be wrapped into a live pooling stub.
            # _build_stub_entry returns a fixed shape and would DROP ``disabled``,
            # silently re-enabling the muted server in the overlay. Pass the
            # entry through unchanged (minus the internal ``poolable`` hint) so
            # kiro-cli still sees it disabled. Mirrors the settings-inject guard
            # in _injectable_settings_servers.
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        is_poolable = entry.get("poolable") is True or name in poolable_servers
        if not is_poolable:
            # Opt-in pooling: a stdio MCP is unpooled (per-session, as today)
            # unless its author/operator declares it stateless via poolable:true
            # OR the dashboard-managed allowlist (config mcp_gateway.poolable_servers)
            # names it. Safe by default — non-declared MCPs are treated as
            # stateful. Strip the per-entry flag so kiro-cli sees a clean entry.
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        new_servers[name] = _build_stub_entry(
            stubs_dir=stubs_dir,
            server_name=name,
            agent_name=agent_name,
            original=entry,
            socket_path=socket_path,
            work_dir=work_dir,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            sidecars_written=sidecars_written,
        )
        wrapped += 1

    # Inject poolable servers sourced from the global settings, wrapped with
    # THIS agent's identity. The agent's own declaration wins on name clash —
    # so a server already wrapped above is never duplicated here.
    #
    # Match the per-agent copy under EITHER its raw key or the slash-free alias
    # kiro requires: _sync_mcp_to_agent stores synced servers under
    # mcp_server_alias(name) (e.g. "npm:@playwright/mcp" -> "playwright-mcp")
    # while settings keeps the raw key. Normalising both sides prevents
    # injecting a redundant second wrapped entry for slash-named servers, and
    # injecting under the alias keeps the entry @-referenceable in tools/
    # allowedTools, mirroring how _sync_mcp_to_agent writes it.
    for name, entry in inject.items():
        alias = mcp_server_alias(name)
        if name in UNPOOLABLE_SERVERS or alias in UNPOOLABLE_SERVERS:
            # UNPOOLABLE is checked by raw name in the per-agent loop and in
            # _injectable_settings_servers, but injection keys the wrapped
            # entry under `alias`. A slash-named server denylisted under one
            # form (raw vs alias) while the config supplies the other would
            # otherwise slip through here — check both forms.
            continue
        if name in new_servers or alias in new_servers:
            continue
        if not isinstance(entry, dict) or "command" not in entry:
            continue
        inject_sig = (entry["command"], _hashable_args(entry.get("args")))
        if inject_sig in seen_targets:
            # Same resolved target already wired under another name — pooling it
            # again would launch a duplicate backend. Skip.
            continue
        # Guard against target-command divergence. gatewayd resolves a backend
        # command from MC_MCP_TARGET_<SERVER>, keyed only by server name with
        # first-wins (alphabetical filename) resolution. If an earlier agent
        # already populated the target env for this server with a DIFFERENT
        # absolute command, injecting here would create a stub whose PoolKey
        # hashes this command but which gatewayd would spawn under the other —
        # a hash that lies about the running binary. Skip + warn instead.
        # (Only compared for absolute-path commands to avoid false positives
        # from bare-name vs resolved-path mismatches.)
        if target_env is not None:
            env_key = "MC_MCP_TARGET_" + alias.replace("-", "_").upper()
            existing = target_env.get(env_key)
            if existing:
                existing_cmd = shlex.split(existing)[0] if existing else ""
                inject_cmd = str(entry.get("command", ""))
                if (
                    existing_cmd.startswith("/")
                    and inject_cmd.startswith("/")
                    and existing_cmd != inject_cmd
                ):
                    logger.warning(
                        "rewriter: skipping injection of %r into agent %r — "
                        "target command %r diverges from already-resolved %r "
                        "(same server name, different binary)",
                        alias, agent_name, inject_cmd, existing_cmd,
                    )
                    continue
        new_servers[alias] = _build_stub_entry(
            stubs_dir=stubs_dir,
            server_name=alias,
            agent_name=agent_name,
            original=entry,
            socket_path=socket_path,
            work_dir=work_dir,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            sidecars_written=sidecars_written,
        )
        wrapped += 1
        seen_targets.add(inject_sig)

    new_spec = dict(spec)
    new_spec["mcpServers"] = new_servers
    return new_spec, wrapped


def _injectable_settings_servers(
    settings_spec: dict[str, Any],
    poolable_servers: frozenset[str],
) -> dict[str, Any]:
    """Return ``{name: raw_entry}`` of poolable stdio servers in the global
    ``settings/mcp.json`` that must be injected per-agent instead of left in
    the settings overlay.

    These are exactly the servers that, if wrapped in BOTH the settings
    overlay and a per-agent overlay, collide on name inside kiro-cli (two
    same-named stubs — one with the correct ``--agent``, one with an empty
    ``--agent`` because settings has no ``name``). By relocating them into
    each agent's own overlay (with the right identity) and dropping them from
    the settings overlay, the duplicate disappears. Non-poolable and HTTP/SSE
    settings servers are NOT returned — they stay raw in the settings overlay
    and merge globally as before.
    """
    servers = settings_spec.get("mcpServers") or {}
    out: dict[str, Any] = {}
    if not isinstance(servers, dict):
        return out
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("disabled") is True:
            # Honour the user's mute: a server explicitly disabled in
            # settings/mcp.json must never be injected as a live stub (which
            # would silently re-enable it in every agent overlay).
            continue
        if name in UNPOOLABLE_SERVERS:
            continue
        if entry.get(_WRAPPER_MARKER) is True:
            # Source settings should be raw; ignore an already-wrapped entry.
            continue
        if "command" not in entry:
            # HTTP/SSE — shareable, no stub needed; leave in settings overlay.
            continue
        is_poolable = entry.get("poolable") is True or name in poolable_servers
        if not is_poolable:
            continue
        out[name] = entry
    return out


def rewrite_agents(
    *,
    source_dir: Path,
    overlay_dir: Path,
    socket_path: Path,
    work_dir: Path,
    sandbox_mode: str = "auto",
    approval_mode: str = "interactive",
    poolable_servers: frozenset[str] | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    """Populate ``overlay_dir`` with rewritten copies of ``source_dir/*.json``.

    Never modifies ``source_dir``. Idempotent — safe to call on every
    KiroCrew startup.

    Args:
        source_dir: Usually ``~/.kiro/agents/``.
        overlay_dir: Usually ``<config_dir>/mcp-gateway/agents/``. Created
            if missing. Cleared of stale files not in ``source_dir``.
        socket_path: Absolute path to the gateway unix socket.
        work_dir: Default cwd passed to the stub (and used in PoolKey
            hashing). Created if missing; gatewayd sets it as the backend
            process's ``current_dir``.
        sandbox_mode: Value from ``config.agent.sandbox`` — fed through
            so the stub's PoolKey matches KiroCrew's sandbox policy.
        approval_mode: Value from ``config.agent.approval_mode`` — same.
        poolable_servers: Server names from ``config.mcp_gateway.poolable_servers``.
            A stdio server is pooled when its name is in this set OR its entry
            sets ``poolable: true``. ``None`` is treated as an empty set.

    Returns:
        A ``(results, target_env)`` tuple:

        * ``results``: mapping ``{agent_filename: wrapped_server_count}``.
          Agents with no MCP servers are omitted.
        * ``target_env``: mapping ``{MC_MCP_TARGET_<SERVER>: "cmd arg arg"}``
          suitable for ``GatewaySpec.mcp_target_env``. Gatewayd consults
          these when a stub registers, to find the real backend command
          to spawn for a new pool key.
    """
    pool_set = poolable_servers or frozenset()
    if not source_dir.is_dir():
        logger.warning("agent source dir missing: %s", source_dir)
        return {}, {}

    platform_compat.make_owner_only_dir(overlay_dir)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("failed to create work_dir %s: %s", work_dir, exc)

    # Per-agent stub scaffolding lives here: the env sidecars written by
    # _build_stub_entry. There is no launcher script -- the overlay entry runs
    # the interpreter directly (see _STUB_MODULE), and channel_id, the one value
    # that would otherwise require a launcher, is injected per session over ACP
    # instead.
    stubs_dir = overlay_dir.parent / "stubs"
    platform_compat.make_owner_only_dir(stubs_dir)
    written: set[str] = set()
    written_sidecars: set[str] = set()
    results: dict[str, int] = {}
    target_env: dict[str, str] = {}

    # Read the GLOBAL ~/.kiro/settings/mcp.json FIRST. kiro-cli merges this
    # file into every agent at runtime — any bare-name server declared here
    # bypasses the gateway unless wrapped (the "kirocrew-lite bypass" class of
    # bug: agents with empty mcpServers inherit the global's unwrapped entries).
    #
    # Wrapping the poolable servers in the settings overlay too does NOT work
    # — settings/mcp.json has no "name", so those stubs get an
    # empty ``--agent`` AND collide (same name) with the correctly-wrapped
    # per-agent copy, double-spawning inside kiro-cli (server_init_failure).
    #
    # The fix is two-sided: INJECT each poolable settings server into
    # every agent's own overlay (wrapped with that agent's name), and DROP it
    # from the settings overlay. Empty-mcpServers agents then get pooled
    # coverage with the right identity, and no name ever appears wrapped in
    # both overlays. Non-poolable / HTTP settings servers stay raw in settings.
    kiro_settings_json = source_dir.parent / "settings" / "mcp.json"
    settings_src_spec: dict[str, Any] | None = None
    settings_poolable: dict[str, Any] = {}
    if kiro_settings_json.is_file():
        try:
            loaded = json.loads(kiro_settings_json.read_text())
            if isinstance(loaded, dict):
                settings_src_spec = loaded
                settings_poolable = _injectable_settings_servers(loaded, pool_set)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("failed to read global mcp.json: %s", exc)

    for path in sorted(source_dir.glob("*.json")):
        try:
            spec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping agent %s: %s", path.name, exc)
            continue
        if not isinstance(spec, dict):
            continue
        # Guarantee a non-empty agent identity. The rewriter reads
        # ``~/.kiro/agents/*.json`` directly, and a user- or tool-dropped file
        # may omit ``name``. Without a name, ``_rewrite_single_spec`` derives
        # ``agent_name = ""`` and every wrapped stub carries ``--agent ""`` —
        # collapsing PoolKey identity across all such agents (cross-agent
        # backend-bucket sharing / isolation loss). Fall back to the file stem,
        # mirroring ``agent.py`` (``data.get("name") or spec_path.stem``); any
        # stable non-empty identifier prevents the collapse.
        if not spec.get("name"):
            spec["name"] = path.stem
        new_spec, wrapped = _rewrite_single_spec(
            spec,
            stubs_dir=stubs_dir,
            socket_path=socket_path,
            work_dir=work_dir,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            poolable_servers=pool_set,
            inject_servers=settings_poolable,
            target_env=target_env,
            sidecars_written=written_sidecars,
        )
        _collect_target_env(new_spec.get("mcpServers", {}), target_env)
        target = overlay_dir / path.name
        try:
            # Atomic + 0600: temp-file + os.replace (via atomic_write) so a
            # live session reading this overlay through the bind-mount never
            # sees a truncated spec (which would make the agent's MCP servers
            # vanish mid-run), and the passed-through non-poolable / HTTP-SSE
            # env blocks (tokens / API keys) are never world-readable. Matches
            # the env sidecar and settings overlay.
            atomic_write(target, json.dumps(new_spec, indent=2) + "\n", mode=0o600)
            if not platform_compat.IS_POSIX:
                platform_compat.restrict_to_owner(target)
        except OSError as exc:
            logger.warning("failed to write overlay %s: %s", target, exc)
            continue
        written.add(path.name)
        if wrapped:
            results[path.name] = wrapped

    # Prune stale overlay entries (user deleted or renamed an agent).
    for stale in overlay_dir.glob("*.json"):
        if stale.name not in written:
            try:
                stale.unlink()
            except OSError:
                pass

    # Prune stale env sidecars (server removed / renamed / flipped
    # non-poolable) so old credential files don't accumulate on disk.
    env_dir = env_sidecar_dir_for_stubs(stubs_dir)
    if env_dir.is_dir():
        for stale in env_dir.glob("*.json"):
            if stale.name not in written_sidecars:
                try:
                    stale.unlink()
                except OSError:
                    pass

    total_wrapped = sum(results.values())

    # Write the settings overlay with the poolable servers REMOVED — they were
    # injected per-agent above (with correct identities). Non-poolable and
    # HTTP/SSE servers stay raw here and continue to merge into every agent at
    # runtime, exactly as before pooling existed. This guarantees no server
    # name is ever wrapped in both a per-agent overlay and the settings
    # overlay, eliminating the duplicate-stub / empty-``--agent`` collision.
    settings_overlay_path = None
    settings_overlay_dir = overlay_dir.parent / "settings"
    settings_overlay_file = settings_overlay_dir / "mcp.json"
    if settings_src_spec is not None:
        platform_compat.make_owner_only_dir(settings_overlay_dir)
        settings_overlay_path = settings_overlay_file
        try:
            src_servers = settings_src_spec.get("mcpServers")
            new_settings = dict(settings_src_spec)
            if isinstance(src_servers, dict):
                # Drop poolable servers (relocated per-agent) and strip internal
                # rewriter markers from the passed-through entries so a polluted
                # or stale source can't leak ``_mc_mcp_gateway_wrapped`` /
                # ``poolable`` into the overlay (harmless today since kiro-cli
                # tolerates unknown fields, but a future strict parser would trip).
                new_settings["mcpServers"] = {
                    name: (
                        {k: v for k, v in entry.items()
                         if k not in (_WRAPPER_MARKER, "poolable")}
                        if isinstance(entry, dict) else entry
                    )
                    for name, entry in src_servers.items()
                    if name not in settings_poolable
                }
            else:
                # Malformed source (mcpServers not a dict): normalize rather
                # than propagate the broken shape into a freshly-written overlay.
                new_settings["mcpServers"] = {}
            # Atomic + 0600: temp-file + os.replace (via atomic_write) so a
            # live session reading this overlay through the bind-mount never
            # sees a truncated mcp.json (which would make its MCP servers
            # vanish mid-run), and the passed-through non-poolable / HTTP-SSE
            # env blocks (tokens / API keys) are never world-readable. Matches
            # the env sidecar and per-agent overlay.
            atomic_write(
                settings_overlay_path,
                json.dumps(new_settings, indent=2) + "\n",
                mode=0o600,
            )
            if not platform_compat.IS_POSIX:
                platform_compat.restrict_to_owner(settings_overlay_path)
            logger.info(
                "mcp-gateway rewriter: global mcp.json overlay written, "
                "%d poolable server(s) relocated to per-agent overlays (overlay=%s)",
                len(settings_poolable), settings_overlay_path,
            )
        except OSError as exc:
            logger.warning("failed to write global mcp.json overlay: %s", exc)
            settings_overlay_path = None
    else:
        # Source settings/mcp.json absent (deleted between runs): prune any
        # previously-written settings overlay, mirroring the per-agent
        # stale-prune above so the overlay tree doesn't accumulate cruft.
        if settings_overlay_file.is_file():
            try:
                settings_overlay_file.unlink()
            except OSError:
                pass

    logger.info(
        "mcp-gateway rewriter: %d agent file(s), %d MCP server(s) wrapped total, "
        "%d target env var(s) (overlay=%s)",
        len(written),
        total_wrapped,
        len(target_env),
        overlay_dir,
    )
    # NOTE: the settings overlay path (when present) is bind-mounted by
    # ``sandbox.py`` via a fixed location derived from the overlay dir —
    # callers do not need to thread it back through ``results``. Keeping
    # ``results`` as a pure ``dict[str, int]`` matches the declared return
    # type and avoids smuggling heterogeneous values through a sentinel key.
    return results, target_env


def _collect_target_env(
    mcp_servers: dict[str, Any],
    target_env: dict[str, str],
) -> None:
    """Populate ``target_env`` with ``MC_MCP_TARGET_<SERVER>`` entries
    for every wrapped server in ``mcp_servers``.

    Two kinds of entry are written per wrapped server:

    * ``MC_MCP_TARGET_<SERVER>`` — first-wins across calls, kept as a
      backward-compatible fallback for any pool key whose
      ``command_args_hash`` has no disambiguated entry.
    * ``MC_MCP_TARGET_<SERVER>__<command_args_hash>`` — one per distinct
      (server, command+args) combination. Two agents that declare the same
      server name with DIFFERENT ``--target-args`` (e.g. ``example-mcp`` with
      ``--include-tool-tags code-review,default`` vs a restricted
      ``--include-tools …`` list) each get their own entry, so
      ``gatewayd.env_target_resolver`` spawns the command matching the
      caller's pool key instead of whichever agent sorted first
      alphabetically. The hash matches ``PoolKey.command_args_hash``.
    """
    for server_name, entry in mcp_servers.items():
        if not isinstance(entry, dict) or not entry.get(_WRAPPER_MARKER):
            continue
        env_key = "MC_MCP_TARGET_" + server_name.replace("-", "_").upper()
        args = entry.get("args", []) or []
        target_cmd: str | None = None
        target_args_str = ""
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--target-command" and i + 1 < len(args):
                target_cmd = str(args[i + 1])
                i += 2
                continue
            if isinstance(a, str) and a.startswith("--target-args="):
                target_args_str = a.split("=", 1)[1]
            i += 1
        if target_cmd:
            # Target args arrive separated by ``_TARGET_ARGS_SEP`` (the same
            # constant _build_stub_entry joins them with). Split on it rather
            # than a hardcoded literal so this reconstruction — which feeds
            # hash_command — stays in lock-step with the stub's PoolKey hash if
            # the separator ever changes. Quote each one (incl. the command)
            # before space-joining so env_target_resolver's shlex.split
            # round-trips args containing embedded spaces. The old
            # ``replace("|"," ")`` split such an arg into multiple tokens,
            # corrupting the backend command line.
            raw_target_args = (
                target_args_str.split(_TARGET_ARGS_SEP) if target_args_str else []
            )
            spec = " ".join(shlex.quote(p) for p in [target_cmd, *raw_target_args])
            # Bare server-name key: first-wins fallback. Two DISTINCT server
            # names can normalize to the same key ("my-server" vs "my_server",
            # case variants). The args-hashed key below is authoritative at
            # resolve time, but warn on a base collision so a genuinely
            # ambiguous config is visible rather than silently first-wins.
            existing = target_env.get(env_key)
            if existing is not None and existing != spec:
                logger.warning(
                    "mcp-gateway rewriter: MC_MCP_TARGET env-key collision on "
                    "%s (distinct server names normalize identically); the "
                    "args-hashed key is used at resolve time, base stays "
                    "first-wins", env_key,
                )
            target_env.setdefault(env_key, spec)
            # Args-disambiguated key: idempotent per (server, command+args), so
            # divergent same-named servers no longer collide on first-wins.
            hashed_key = env_key + "__" + hash_command(target_cmd, raw_target_args)
            target_env[hashed_key] = spec


def overlay_ready(overlay_dir: Path) -> bool:
    """Return ``True`` if ``overlay_dir`` has at least one readable JSON."""
    if not overlay_dir.is_dir():
        return False
    try:
        return any(p.is_file() for p in overlay_dir.glob("*.json"))
    except OSError:
        return False


def is_wrapped_entry(entry: Any) -> bool:
    """Diagnostic helper: ``True`` iff ``entry`` was produced by the rewriter."""
    return isinstance(entry, dict) and entry.get(_WRAPPER_MARKER) is True


def default_overlay_dir() -> Path:
    """Return ``$KIROCREW_HOME/mcp-gateway/agents`` (follows ``config_dir``)."""
    home = os.environ.get("KIROCREW_HOME")
    base = Path(home) if home else config_dir()
    return base / "mcp-gateway" / "agents"


def resolve_overlay_dir(configured: str = "") -> Path:
    """Return the EFFECTIVE overlay dir: the configured value, else the default.

    Single source of truth for the ``mcp_gateway.overlay_dir`` fallback, shared
    by the gateway boot path and by ``gatewayd`` (which must resolve the same
    directory to find declared-env sidecars).
    """
    return Path(configured) if configured else default_overlay_dir()


def env_sidecar_dir_for_stubs(stubs_dir: Path) -> Path:
    """Return the declared-env sidecar directory inside a stub overlay tree."""
    return stubs_dir / "env"


def env_sidecar_dir(overlay_dir: Path) -> Path:
    """Return the declared-env sidecar directory for ``overlay_dir``.

    The stub overlay tree is a SIBLING of the agents overlay dir
    (``<base>/mcp-gateway/{agents,stubs}``), so the sidecars live at
    ``<base>/mcp-gateway/stubs/env``. Shared with ``gatewayd`` so the writer and
    the reader can never disagree about where sidecars live.
    """
    return env_sidecar_dir_for_stubs(overlay_dir.parent / "stubs")


def env_sidecar_name(agent_name: str, server_name: str) -> str:
    """Return the declared-env sidecar FILE NAME for ``(agent, server)``.

    Shape: ``<sanitized-agent>.<sanitized-server>.<digest>.json``.

    The sanitized components stay in the name so an operator can identify the
    file, but they are NOT what makes it unique — sanitization is lossy (every
    non-``[A-Za-z0-9_-]`` char, including ``.``, becomes ``_``), so servers
    ``foo.bar`` and ``foo_bar`` declared by the same agent would otherwise BOTH
    map to ``agent.foo_bar.json``: the second write clobbers the first and one
    server is handed the other's environment. The trailing 12-hex SHA-256 of the
    NUL-delimited RAW components restores injectivity, so distinct
    ``(agent, server)`` pairs can never share a file.

    Single source of truth for the naming rule: the rewriter writes the sidecar
    and ``gatewayd`` reads it back by recomputing this name from the PoolKey's
    ``agent_name``/``server_name``, so a change here moves both ends at once.
    Sidecars written under an older naming scheme are pruned as stale by
    ``rewrite_agents`` (it deletes any ``env/*.json`` it did not just write).
    """

    def _san(s: str) -> str:
        return "".join(c if (c.isalnum() or c in "_-") else "_" for c in s)

    digest = hashlib.sha256(
        f"{agent_name}\0{server_name}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{_san(agent_name)}.{_san(server_name)}.{digest}.json"


def forward_declared_env_enabled() -> bool:
    """Return ``mcp_gateway.forward_declared_env`` (default ``False``).

    Function-local config import: ``config.loader`` imports THIS module at its
    own module top level, so a top-level import here would be circular. Mirrors
    ``backend._mcp_apps_enabled``. Fails CLOSED — an unreadable config means the
    declared env is not forwarded.
    """
    try:
        # circular import: config.loader imports THIS module at its own top level
        # (for default_overlay_dir / default_socket_path), so a module-scope
        # import here would be a cycle. Mirrors backend._mcp_apps_enabled.
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(KiroCrewConfig.load().mcp_gateway.forward_declared_env)
    except Exception:
        logger.debug("rewriter: config unreadable; declared-env forwarding off", exc_info=True)
        return False


def default_socket_path() -> Path:
    """Return the default gateway unix socket path."""
    home = os.environ.get("KIROCREW_HOME")
    base = Path(home) if home else config_dir()
    return base / "mcp-gateway" / "gateway.sock"
