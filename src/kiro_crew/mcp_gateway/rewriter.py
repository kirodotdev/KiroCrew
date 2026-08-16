"""Rewrite kiro agent JSON so MCP servers route through the broker.

The rewriter reads ``~/.kiro/agents/*.json`` and writes modified copies into
the overlay directory (``<config_dir>/mcp-gateway/agents/``). The host
filesystem remains untouched — the broker stubs in these specs are injected
into each kiro-cli session over ACP ``session/new``, which outranks the
same-named entry in the agent spec (see ``session_servers.py``).

Servers in :data:`UNPOOLABLE_SERVERS` are left unwrapped. The set is empty, so
nothing is excluded through it; the first-party servers that bind
``KIROCREW_SESSION_KEY`` stay unwrapped only because nothing lists them in the
stub allowlist.

The rewrite is fingerprint-cached: a content-signature snapshot of every input is
kept at ``<overlay_dir>/.rewrite-fingerprint``, and a boot whose inputs all
match serves the previous run's overlays (and its cached ``target_env``)
instead of re-parsing, re-resolving and re-writing everything. The prune
pass runs on both paths. Any doubt — torn file, missing output, unresolved
command — falls through to the full rewrite.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import __version__, platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.env import spec_env_path, spec_path_key
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.hashing import hash_command, is_secret_env_key
from kiro_crew.mcp_gateway.manager import is_credential_env_key
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

# Fingerprint of the last completed rewrite, stored inside the overlay dir so
# an unchanged boot can skip re-parsing every agent spec, re-resolving every
# command through ``shutil.which`` and re-writing every overlay file. The name
# deliberately has NO ``.json`` suffix: ``pathlib``'s ``glob("*.json")``
# matches dotfiles, so a ``.json``-suffixed name would be deleted by the
# stale-overlay prune pass and would make ``overlay_ready()`` report an empty
# overlay dir as ready.
_FINGERPRINT_NAME = ".rewrite-fingerprint"

# Bump when the rewrite's OUTPUT shape changes for identical inputs (new stub
# flags, changed overlay layout, ...) so upgraded installs regenerate instead
# of serving overlays produced by older logic. The package version is also in
# the fingerprint, so a release bump invalidates regardless; this constant is
# the explicit knob for in-development changes.
_FINGERPRINT_SCHEMA = 3


@dataclass
class _RewritePassNotes:
    """Observations from one full rewrite pass that decide cacheability.

    ``which_results`` records every ``shutil.which`` probe as
    ``(bare_command, search_path) -> resolved-or-""``. The resolved path is an
    OUTPUT of filesystem state the stat-based fingerprint cannot see (a binary
    removed from, added to, or shadowed within an unchanged PATH), so the
    cache-hit path re-runs exactly these probes and compares — a disagreement
    in either direction forces the full rewrite.

    ``sidecar_write_failed`` and ``source_read_failed`` mark transient I/O
    faults: the produced output set is incomplete for reasons that can clear
    without any fingerprinted input changing, so the run must not be cached
    (and a previous run's fingerprint must be removed, or it could still match
    and freeze the degraded state).
    """

    which_results: dict[str, str] = field(default_factory=dict)
    sidecar_write_failed: bool = False
    source_read_failed: bool = False


# Separator inside a stored which-probe key (bare command NUL search-path).
# NUL is legal in JSON strings and cannot appear in either component.
_WHICH_KEY_SEP = "\0"

# Reserved for MCP servers that explicitly opt out of the broker even
# when they could support it (e.g. dev/diagnostic servers that want the
# operator to see one process per session).
#
# Empty, and nothing is excluded through it today. The intended signalling path
# — a backend that does not advertise ``kirocrew.caller-identity`` in its
# initialize response being refused for pooling — is NOT implemented: gatewayd
# parses that capability only to decide whether to inject caller identity, and
# no code path declines to pool a backend for lacking it. So this set is the
# only mechanism of its kind that exists, and it is the one to reach for while
# that is true, not a fallback for servers that cannot adopt the extension.
UNPOOLABLE_SERVERS: frozenset[str] = frozenset()

# Marker field set on rewritten MCP entries so repeat runs are idempotent.
_WRAPPER_MARKER = "_kirocrew_mcp_gateway_wrapped"

# Legacy marker from pre-fork naming; accepted on read for overlays written by
# older rewriter versions that haven't been regenerated yet.
_WRAPPER_MARKER_LEGACY = "_mc_mcp_gateway_wrapped"


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
#:
#: Aliased from the package constant so the launch line and the cmdline
#: fingerprint the Sessions surface counts stubs by cannot drift apart.
_STUB_MODULE = STUB_MODULE


def _resolve_target_command(
    target_command: str,
    env_pairs: dict[str, Any],
    notes: _RewritePassNotes | None,
) -> str:
    """Resolve an MCP target command to an absolute path, or ``""``.

    gatewayd spawns backends from the systemd ``--user`` environment, whose
    ``PATH`` lacks the toolbox / user-local bin dirs a login shell has — so a
    bare command that resolves fine for the SESSION's own exec ENOENTs on
    every pooled spawn: 79% of all measured fallbacks. The search is
    :func:`kiro_crew.env.spec_env_path` — literally the same composition the
    MCP probe and the agent-config resolver use (spec ``env.PATH`` first, then
    the augmented host PATH) — so a server that probes healthy on the
    dashboard can never ENOENT in gatewayd.

    An absolute command is accepted only when it exists and is executable
    (the same predicate ``agent.py``'s config resolver applies). Any command
    that resolves nowhere returns ``""`` — the caller must NOT emit a
    stub for it: kiro-cli's own spawn environment may still resolve it, so the
    entry is left for the session to launch directly instead of degrading
    through a guaranteed-ENOENT pooled spawn on every session.

    Every ``shutil.which`` probe is recorded in ``notes.which_results`` so the
    fingerprint cache-hit path re-runs and compares it (binary removed /
    added / shadowed invalidates the cache in both directions).
    """
    if not target_command:
        return ""
    if os.path.isabs(target_command):
        # Same predicate as ``agent.py::_resolve_command``: an absolute path
        # must exist and be executable, or the entry is left unwrapped — a
        # dead absolute path would ENOENT identically in gatewayd and in the
        # session, so failing it in the session (visible) beats a per-session
        # pooled-spawn-then-fallback cycle.
        if os.path.isfile(target_command) and os.access(target_command, os.X_OK):
            return target_command
        return ""
    # spec_path_key, not a literal "PATH" lookup: Windows-authored specs
    # legitimately spell it "Path" and the child's loader honours it.
    path_key = spec_path_key(env_pairs) if isinstance(env_pairs, dict) else None
    env_path = env_pairs.get(path_key, "") if path_key else ""
    # spec_env_path is the canonical composition the MCP probe and the
    # agent-config resolver also use: the spec's own env.PATH entries FIRST
    # (an operator pin must win), then the augmented host PATH. It also
    # degrades a non-string PATH and dedups, so one malformed hand-edited
    # spec cannot abort the rewrite pass.
    search_path = spec_env_path(env_path)
    resolved = shutil.which(target_command, path=search_path)
    if notes is not None:
        notes.which_results[
            f"{target_command}{_WHICH_KEY_SEP}{search_path}"
        ] = resolved or ""
    return resolved or ""


def _normalized_env(entry: dict[str, Any], *, context: str = "") -> dict[str, Any]:
    """Return the entry's declared ``env`` as a dict (``{}`` for malformed).

    ``~/.kiro/agents/*.json`` is hand-editable, so ``env`` can legally parse
    as a non-dict (e.g. ``"env": [{}]``). Downstream code iterates keys with
    ``str.startswith``, so a list of dicts would raise AttributeError and
    abort the ENTIRE rewrite pass — disabling pooling for every agent because
    one spec was malformed. Normalize to ``{}`` instead, warning when
    *context* names the offending entry.
    """
    declared = entry.get("env", {}) or {}
    if isinstance(declared, dict):
        return declared
    if context:
        logger.warning(
            "rewriter: %s has a non-object 'env' (%s); ignoring it",
            context, type(declared).__name__,
        )
    return {}


def _withheld_env_count(entry_env: dict[str, Any], forward_env: bool) -> int:
    """How many declared env keys a shared pooled backend would NOT receive.

    The pooling bargain is "the backend starts with your declared env"; any
    withheld key can be the one the server dies without, so a non-zero count
    disqualifies the entry from pooling (issue #3495 cause B). With forwarding
    off every key is withheld. With forwarding on, gatewayd's forwarder still
    drops rotating-secret keys (excluded from the PoolKey, so co-tenants can
    disagree on their values) and the daemon's own credential-scrub set —
    mirroring ``gatewayd._declared_non_secret_env`` exactly, so this
    classifier never promises an env the forwarder will refuse to apply.
    """
    if not forward_env:
        return len(entry_env)
    return sum(
        1
        for k in entry_env
        if is_secret_env_key(k) or is_credential_env_key(k)
    )


def _build_stub_entry(
    *,
    stubs_dir: Path,
    server_name: str,
    agent_name: str,
    original: dict[str, Any],
    env_pairs: dict[str, Any],
    target_command: str,
    socket_path: Path,
    work_dir: Path,
    sandbox_mode: str,
    approval_mode: str,
    sidecars_written: set[str] | None = None,
    poolable: bool = False,
    notes: _RewritePassNotes | None = None,
) -> dict[str, Any]:
    """Return the rewritten ``mcpServers[name]`` entry.

    ``target_command`` is the ALREADY-RESOLVED absolute backend command, and
    ``env_pairs`` the ALREADY-NORMALIZED declared env — callers run
    :func:`_resolve_target_command` / :func:`_normalized_env` first and skip
    the stub entirely when the command is unresolvable or (for a poolable
    entry) any declared key would be withheld from the shared backend. This
    function therefore never emits a stub whose pooled spawn is a guaranteed
    ENOENT (issue #3495 cause A) or whose declared env is silently dropped
    (cause B).

    Preserves ``autoApprove`` on the wrapped entry so kiro-cli still honours
    it at the UI layer. ``env`` is cleared on the wrapper — the stub passes
    env separately through its flags so the gateway can hash the
    post-substitution env into the PoolKey.
    """
    target_args: list[str] = [str(a) for a in original.get("args", []) or []]
    auto_approve: list[str] = list(original.get("autoApprove", []) or [])

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
    if poolable:
        stub_args.append("--poolable")
    if env_pairs:
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
            # Transient fault: the overlay written this pass omits --env-file,
            # and an old sidecar may still exist at this name — so an
            # existence check cannot detect the degradation. Mark the pass
            # uncacheable so the next boot retries the write.
            if notes is not None:
                notes.sidecar_write_failed = True
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
        if k not in ("command", "args", "env", "poolable", "autoApprove",
                     _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY)
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
    stub_servers: frozenset[str],
    pooling_enabled: bool = True,
    forward_env: bool = False,
    inject_servers: dict[str, Any] | None = None,
    target_env: dict[str, str] | None = None,
    sidecars_written: set[str] | None = None,
    notes: _RewritePassNotes | None = None,
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
        if entry.get(_WRAPPER_MARKER) is True or entry.get(_WRAPPER_MARKER_LEGACY) is True:
            # Already wrapped (idempotency). Upgrade to new marker on re-emit.
            upgraded = dict(entry)
            upgraded.pop(_WRAPPER_MARKER_LEGACY, None)
            upgraded[_WRAPPER_MARKER] = True
            new_servers[name] = upgraded
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
        # The stub is opt-in per server, and ``mcp_gateway.stub_servers`` is the
        # ONLY thing that opts one in. An unstubbed server passes through
        # untouched, so the session launches it directly — the same process
        # topology as running with no broker at all, and no stub process to pay
        # for. Strip only the internal ``poolable`` hint, which is ours and not
        # kiro-cli's.
        #
        # A spec-level ``poolable: true`` deliberately does NOT opt a server in
        # any more. It cannot: the broker and the session's overlay are both
        # gated on the config list, and teaching those gates to read agent specs
        # would put filesystem IO behind every ``KiroCrewConfig.load()`` (244
        # call sites, uncached). Honouring it only in this function produced a
        # stub nothing pointed at, and a dashboard row that read "stub" for a
        # server that had none. One source of truth instead.
        if name not in stub_servers:
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        entry_env = _normalized_env(
            entry, context=f"server {name!r} for agent {agent_name!r}"
        )
        resolved_cmd = _resolve_target_command(
            str(entry.get("command", "")), entry_env, notes
        )
        if not resolved_cmd:
            # Fix for issue #3495 cause A: an unresolvable bare command means
            # gatewayd's spawn is a guaranteed ENOENT (it runs under the
            # systemd --user PATH). Emitting a stub anyway degraded EVERY
            # session through a spawn-fail → fallback-exec cycle. Leave the
            # entry unwrapped instead: kiro-cli's own spawn environment may
            # still resolve the name, and if it cannot, the failure surfaces
            # in the session where the operator can see it.
            logger.warning(
                "rewriter: cannot resolve MCP command %r for opted-in server "
                "%r (agent %r) on the gateway search path; leaving it "
                "unwrapped so the session launches it directly. Use an "
                "absolute path in the spec to pool it.",
                entry.get("command", ""), name, agent_name,
            )
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        withheld = _withheld_env_count(entry_env, forward_env) if pooling_enabled else 0
        if withheld:
            # Fix for issue #3495 cause B: a pooled backend is spawned WITHOUT
            # part (or, with forwarding off, all) of the env this spec
            # declares. A server that needs a withheld key dies at prime on
            # every session — breaker trips, stub falls back, and the crash
            # loop re-discovers the same policy fact forever. Pre-classify
            # instead: leave the entry unwrapped (the session applies the
            # declared env itself), and say exactly which knob — or which key
            # class — blocks pooling.
            # Log NO value derived from applying the secret predicate to the
            # env block (CodeQL taints such expressions as clear-text logging
            # of sensitive information) — only the total declared count.
            logger.warning(
                "rewriter: opted-in server %r (agent %r) declares env "
                "(%d keys) of which some would be withheld from a shared "
                "backend (%s); leaving it unwrapped so the session launches "
                "it with its declared env.",
                name, agent_name, len(entry_env),
                "mcp_gateway.forward_declared_env is off — enable it to pool"
                if not forward_env
                else "rotating-secret/credential keys are never forwarded — "
                "the backend must read them from disk to pool",
            )
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        new_servers[name] = _build_stub_entry(
            stubs_dir=stubs_dir,
            server_name=name,
            agent_name=agent_name,
            original=entry,
            env_pairs=entry_env,
            target_command=resolved_cmd,
            socket_path=socket_path,
            work_dir=work_dir,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            sidecars_written=sidecars_written,
            # Sharing is global over the stub set: being stubbed is the only
            # per-server decision, so there is nothing further to consult here.
            poolable=pooling_enabled,
            notes=notes,
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
        # The stub is opt-in here too, from the same single source. A settings
        # level server nobody listed is left for the session to launch itself, so
        # this path cannot reintroduce the stub-per-server default through the
        # back door — and a spec-level ``poolable: true`` cannot either.
        if not (name in stub_servers or alias in stub_servers):
            continue
        inject_sig = (entry["command"], _hashable_args(entry.get("args")))
        if inject_sig in seen_targets:
            # Same resolved target already wired under another name — pooling it
            # again would launch a duplicate backend. Skip.
            continue
        # Guard against target-command divergence. gatewayd resolves a backend
        # command from KIROCREW_MCP_TARGET_<SERVER>, keyed only by server name with
        # first-wins (alphabetical filename) resolution. If an earlier agent
        # already populated the target env for this server with a DIFFERENT
        # absolute command, injecting here would create a stub whose PoolKey
        # hashes this command but which gatewayd would spawn under the other —
        # a hash that lies about the running binary. Skip + warn instead.
        # (Only compared for absolute-path commands to avoid false positives
        # from bare-name vs resolved-path mismatches.)
        if target_env is not None:
            env_key = "KIROCREW_MCP_TARGET_" + alias.replace("-", "_").upper()
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
        entry_env = _normalized_env(
            entry, context=f"settings server {alias!r} for agent {agent_name!r}"
        )
        resolved_cmd = _resolve_target_command(
            str(entry.get("command", "")), entry_env, notes
        )
        if not resolved_cmd:
            # Membership in ``inject`` was vetted by
            # _injectable_settings_servers (same resolver, same pass), so this
            # only fires on a filesystem race between the two probes. The
            # entry was already DROPPED from the settings overlay on the
            # strength of that vetting — injecting it RAW (unwrapped) keeps
            # the server's tools alive for the session; skipping would make
            # it vanish entirely.
            logger.warning(
                "rewriter: settings server %r became unresolvable between "
                "vetting and injection; injecting it unwrapped into agent %r",
                alias, agent_name,
            )
            new_servers[alias] = {k: v for k, v in entry.items() if k != "poolable"}
            seen_targets.add(inject_sig)
            continue
        new_servers[alias] = _build_stub_entry(
            stubs_dir=stubs_dir,
            server_name=alias,
            agent_name=agent_name,
            original=entry,
            env_pairs=entry_env,
            target_command=resolved_cmd,
            socket_path=socket_path,
            work_dir=work_dir,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            sidecars_written=sidecars_written,
            poolable=pooling_enabled,
            notes=notes,
        )
        wrapped += 1
        seen_targets.add(inject_sig)

    new_spec = dict(spec)
    new_spec["mcpServers"] = new_servers
    return new_spec, wrapped


def _injectable_settings_servers(
    settings_spec: dict[str, Any],
    stub_servers: frozenset[str],
    *,
    pooling_enabled: bool = True,
    forward_env: bool = False,
    notes: _RewritePassNotes | None = None,
) -> dict[str, Any]:
    """Return ``{raw_name: raw_entry}`` of stdio servers in the global
    ``settings/mcp.json`` that must be RELOCATED out of the settings overlay
    into a per-agent one.

    The returned set does double duty: every name in it is injected per-agent
    AND dropped from the settings overlay. Those two must be the SAME set — a
    server dropped from settings but not injected anywhere simply disappears,
    taking its MCP tools with it, which is strictly worse than either stubbing
    it or leaving it alone. So the stub opt-in is applied HERE, once, rather
    than at the injection loop, where filtering would silently desync the two.

    These are exactly the servers that, if wrapped in BOTH the settings
    overlay and a per-agent overlay, collide on name inside kiro-cli (two
    same-named stubs — one with the correct ``--agent``, one with an empty
    ``--agent`` because settings has no ``name``). By relocating them into
    each agent's own overlay (with the right identity) and dropping them from
    the settings overlay, the duplicate disappears. HTTP/SSE settings servers
    are NOT returned — they need no stub and stay raw in the settings overlay,
    merging globally.

    Keys are the RAW settings names, because the caller filters ``src_servers``
    (raw-keyed) with this set. Stub membership is tested under both the raw name
    and the slash-free alias, since the config may carry either spelling.
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
        if not (name in stub_servers or mcp_server_alias(name) in stub_servers):
            # Not stubbed: leave it RAW in the settings overlay so the session
            # launches it directly. Relocating it here would delete it from the
            # only overlay that still lists it.
            continue
        entry_env = _normalized_env(entry, context=f"settings server {name!r}")
        if not _resolve_target_command(str(entry.get("command", "")), entry_env, notes):
            # Issue #3495 cause A, settings edition: an unresolvable bare
            # command must not be relocated into a stub whose pooled spawn is
            # a guaranteed ENOENT. Leaving it raw in the settings overlay
            # preserves the pre-pooling behaviour (kiro-cli merges and
            # launches it with its own environment).
            logger.warning(
                "rewriter: cannot resolve MCP command %r for opted-in "
                "settings server %r on the gateway search path; leaving it "
                "raw in the settings overlay. Use an absolute path to pool it.",
                entry.get("command", ""), name,
            )
            continue
        withheld = _withheld_env_count(entry_env, forward_env) if pooling_enabled else 0
        if withheld:
            # Issue #3495 cause B, settings edition: pooling would withhold
            # part or all of this server's declared env and crash-loop it.
            # Leave it raw.
            # Same CodeQL constraint as the per-agent site: log only the
            # total declared count, never the secret-predicate-derived one.
            logger.warning(
                "rewriter: opted-in settings server %r declares env "
                "(%d keys) of which some would be withheld from a shared "
                "backend (%s); leaving it raw in the settings overlay.",
                name, len(entry_env),
                "mcp_gateway.forward_declared_env is off — enable it to pool"
                if not forward_env
                else "rotating-secret/credential keys are never forwarded — "
                "the backend must read them from disk to pool",
            )
            continue
        out[name] = entry
    return out


def _stat_sig(path: Path) -> list[Any] | None:
    """Return ``[size, mtime_ns, sha256]`` for *path*, or ``None`` if it
    cannot be read. Size and nanosecond mtime are cheap discriminators, but
    neither is sufficient alone or together: a same-size write can land inside
    one filesystem timestamp tick (coarse on some filesystems), and a
    ``chmod`` changes neither — so the content digest is what makes a
    signature collision impossible for changed bytes. The files signed here
    are small JSON documents, so hashing them is microseconds against the
    parse+resolve+write pass the fingerprint exists to skip."""
    try:
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, digest]


def _rewrite_inputs_fingerprint(
    *,
    source_dir: Path,
    settings_path: Path,
    overlay_dir: Path,
    socket_path: Path,
    work_dir: Path,
    sandbox_mode: str,
    approval_mode: str,
    stub_set: frozenset[str],
    pooling_enabled: bool,
    forward_env: bool,
) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of every input that can change
    :func:`rewrite_agents`'s output.

    Enumerated against the code, not guessed:

    * ``sources`` / ``settings`` — the parsed spec files (size+mtime+digest).
    * ``socket_path`` / ``work_dir`` — baked into stub argv and the PoolKey.
    * ``sandbox_mode`` / ``approval_mode`` / ``stub_servers`` /
      ``pooling_enabled`` — decide stub flags and which entries are shareable.
    * ``python`` — ``sys.executable`` is baked into every overlay ``command``,
      so a moved/upgraded interpreter must regenerate the overlays.
    * ``path_env`` / ``pathext`` / ``path_augment`` — feed the
      ``shutil.which`` resolution of bare command names (``path_augment`` is
      :func:`kiro_crew.env.spec_env_path` over an empty spec PATH — the
      augmentation-and-dedup half of the search, which depends on ambient
      state like ``MISE_DATA_DIR`` that ``path_env`` cannot see). The
      other half of which()'s input — the CONTENTS of the searched
      directories — is not stat-able here; it is covered by the stored
      per-probe results, which the cache-hit path re-runs and compares (see
      :class:`_RewritePassNotes`).
    * ``forward_declared_env`` — decides whether an env-declaring server is
      pooled at all (issue #3495 cause B pre-classification), so flipping the
      config flag must regenerate the overlays.
    * ``schema`` / ``package`` — invalidate on rewriter logic changes.
    """
    sources: dict[str, list[Any] | None] = {
        p.name: _stat_sig(p) for p in sorted(source_dir.glob("*.json"))
    }
    return {
        "schema": _FINGERPRINT_SCHEMA,
        "package": __version__,
        "python": sys.executable,
        "path_env": os.environ.get("PATH", ""),
        "pathext": os.environ.get("PATHEXT", ""),
        "path_augment": spec_env_path(""),
        "forward_declared_env": bool(forward_env),
        "source_dir": str(source_dir),
        "overlay_dir": str(overlay_dir),
        "socket_path": str(socket_path),
        "work_dir": str(work_dir),
        "sandbox_mode": sandbox_mode,
        "approval_mode": approval_mode,
        "stub_servers": sorted(stub_set),
        "pooling_enabled": bool(pooling_enabled),
        "sources": sources,
        "settings": _stat_sig(settings_path),
    }


def _load_fingerprint(path: Path) -> dict[str, Any] | None:
    """Load and validate a stored fingerprint. NEVER raises: a missing,
    unreadable, torn, or malformed file returns ``None``, which callers treat
    as "do the full rewrite" — unreadable must never mean "match"."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    inputs = data.get("inputs")
    outputs = data.get("outputs")
    which = data.get("which")
    if not (
        isinstance(inputs, dict)
        and isinstance(outputs, dict)
        and isinstance(which, dict)
    ):
        return None
    overlays = outputs.get("overlays")
    sidecars = outputs.get("sidecars")
    if not (isinstance(overlays, dict) and isinstance(sidecars, dict)):
        return None

    def _valid_sig(sig: Any) -> bool:
        return (
            isinstance(sig, list)
            and len(sig) == 3
            and isinstance(sig[0], int)
            and isinstance(sig[1], int)
            and isinstance(sig[2], str)
        )

    for name, sig in (*overlays.items(), *sidecars.items()):
        # Names are joined onto the overlay/sidecar dirs below; refuse
        # anything that could escape them, so a corrupted or tampered file
        # degrades to a full rewrite instead of probing arbitrary paths.
        if not isinstance(name, str) or "/" in name or "\\" in name or name in (".", ".."):
            return None
        if not _valid_sig(sig):
            return None
    settings_sig = outputs.get("settings_overlay")
    if settings_sig is not None and not _valid_sig(settings_sig):
        return None
    if not all(
        isinstance(k, str) and _WHICH_KEY_SEP in k and isinstance(v, str)
        for k, v in which.items()
    ):
        return None
    return data


def _cached_rewrite_result(
    stored: dict[str, Any],
    *,
    overlay_dir: Path,
    stubs_dir: Path,
) -> tuple[dict[str, int], dict[str, str]] | None:
    """Serve the previous rewrite's result without redoing the work.

    Returns ``None`` (caller falls through to the full rewrite) unless every
    output the previous run produced still exists WITH the size+mtime+digest it
    was recorded with — a deleted or edited overlay/sidecar must be
    regenerated, not skipped over (an edited overlay would diverge from the
    cached ``target_env``: the stub's PoolKey would hash the edited command
    while gatewayd spawns the recorded one). The previous run's
    ``shutil.which`` probes are also re-run and compared: directory contents
    are which() input the stat fingerprint cannot see, so a target binary
    removed, moved between PATH prefixes, or newly shadowed forces the full
    rewrite instead of serving a dead absolute path forever.

    On success the prune passes still run (stat-only), so a stray file in the
    overlay tree is removed exactly as on the full path — including a
    leftover settings overlay whose source is gone.
    """
    outputs = stored["outputs"]
    overlay_sigs: dict[str, Any] = outputs["overlays"]
    sidecar_sigs: dict[str, Any] = outputs["sidecars"]
    env_dir = env_sidecar_dir_for_stubs(stubs_dir)
    settings_overlay_file = overlay_dir.parent / "settings" / "mcp.json"
    try:
        for name, sig in overlay_sigs.items():
            if _stat_sig(overlay_dir / name) != sig:
                return None
        for name, sig in sidecar_sigs.items():
            if _stat_sig(env_dir / name) != sig:
                return None
        settings_sig = outputs.get("settings_overlay")
        if settings_sig is not None:
            if _stat_sig(settings_overlay_file) != settings_sig:
                return None
        elif settings_overlay_file.is_file():
            # The previous run produced no settings overlay, yet one exists —
            # e.g. its deletion failed transiently on the full path (Windows
            # sharing violation). Removed global MCP servers must not stay
            # active: retry the deletion, and refuse the cache if it survives.
            try:
                settings_overlay_file.unlink()
            except OSError:
                return None
    except OSError:
        return None

    # Re-run the recorded which() probes: a few directory stats per bare
    # command, no spec parsing. Closes the staleness gap in both directions
    # (resolved -> gone/different AND unresolved -> now-resolves).
    for key, recorded in stored["which"].items():
        bare, _, search_path = key.partition(_WHICH_KEY_SEP)
        try:
            current = shutil.which(bare, path=search_path) or ""
        except OSError:
            return None
        if current != recorded:
            return None

    # Re-assert owner-only protection on EVERY artifact the cached result
    # serves — a chmod / DACL edit changes no stat-or-digest signature, and
    # on Windows the file DACL (not the containing directory) is what carries
    # access. The invariant is FAIL-LOUD end to end: every call in this block
    # raises on failure, and any failure falls through to the full rewrite —
    # a lockdown that cannot be re-asserted must never be served from cache.
    # That is why ``restrict_to_owner`` (raises on both platforms) is used for
    # files rather than ``chmod_safe`` (logs-and-continues on POSIX), and why
    # the POSIX directory modes are re-applied with a raw ``os.chmod`` after
    # ``make_owner_only_dir`` (which warns-and-continues). Windows directory
    # DACLs stay best-effort inside ``make_owner_only_dir``: there the file
    # DACL is the carrier of access, and every file is fail-loud below.
    try:
        if env_dir.is_dir():
            platform_compat.make_owner_only_dir(env_dir)
            if platform_compat.IS_POSIX:
                # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is OWNER-ONLY, the tightest traversable mode for this credential-sidecar directory; the rule's suggested 0o644 would grant world-read and drop the execute bit a directory needs. Raw os.chmod (not make_owner_only_dir alone) because this path must FAIL LOUD into the full rewrite.  # noqa: E501
                os.chmod(env_dir, 0o700)
        protected: list[Path] = [
            *(overlay_dir / n for n in overlay_sigs),
            *(env_dir / n for n in sidecar_sigs),
            overlay_dir / _FINGERPRINT_NAME,
        ]
        if settings_sig is not None:
            platform_compat.make_owner_only_dir(settings_overlay_file.parent)
            if platform_compat.IS_POSIX:
                # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- same as the env_dir site above: 0o700 is owner-only and fail-loud is required.  # noqa: E501
                os.chmod(settings_overlay_file.parent, 0o700)
            protected.append(settings_overlay_file)
        for artifact in protected:
            platform_compat.restrict_to_owner(artifact)
    except OSError:
        # The full rewrite re-creates each artifact through its own
        # protect-before-content writers, whose failure handling marks the
        # pass uncacheable.
        return None

    # The prune pass runs even when the rewrite is skipped: a deleted agent
    # spec changes the fingerprint (its file leaves the stat set) and takes the
    # full path, but a foreign file in the overlay tree does not, and must
    # still be swept.
    for stale in overlay_dir.glob("*.json"):
        if stale.name not in overlay_sigs:
            try:
                stale.unlink()
            except OSError:
                pass
    if env_dir.is_dir():
        for stale in env_dir.glob("*.json"):
            if stale.name not in sidecar_sigs:
                try:
                    stale.unlink()
                except OSError:
                    pass

    # Reconstruct the result from the just-validated OVERLAYS rather than
    # trusting a payload stored in the fingerprint. The overlays are the
    # executable authority either way — kiro-cli sessions receive their stub
    # argv directly — so rebuilding ``target_env`` from them means the
    # fingerprint carries no command material at all: tampering with it can
    # at worst skip a rewrite, never inject a command that is not already in
    # the overlay files. Iteration is sorted by name to match the full path's
    # sorted source glob, so ``setdefault`` first-wins resolution is
    # byte-identical to a fresh rewrite.
    results: dict[str, int] = {}
    target_env: dict[str, str] = {}
    try:
        for name in sorted(overlay_sigs):
            spec = json.loads((overlay_dir / name).read_text())
            servers = spec.get("mcpServers", {}) if isinstance(spec, dict) else {}
            if not isinstance(servers, dict):
                servers = {}
            wrapped = sum(
                1
                for entry in servers.values()
                if isinstance(entry, dict)
                and (
                    entry.get(_WRAPPER_MARKER) is True
                    or entry.get(_WRAPPER_MARKER_LEGACY) is True
                )
            )
            if wrapped:
                results[name] = wrapped
            _collect_target_env(servers, target_env)
    except (OSError, json.JSONDecodeError):
        return None

    logger.info(
        "mcp-gateway rewriter: inputs unchanged since last rewrite — "
        "serving cached overlays (%d agent file(s), %d target env var(s), overlay=%s)",
        len(overlay_sigs),
        len(target_env),
        overlay_dir,
    )
    return results, target_env


def _store_fingerprint(
    path: Path,
    *,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    which: dict[str, str],
) -> None:
    """Persist the rewrite fingerprint atomically, protection BEFORE content.

    The payload holds only input/output signatures and which-probe results —
    never the ``target_env`` command material, which the cache-hit path
    reconstructs from the validated overlays — but it follows the env-sidecar
    protect-before-content pattern rather than plain ``atomic_write`` anyway
    (``atomic_write``'s ``mode=`` is applied pre-write on POSIX but is inert
    on Windows, where the DACL is the only carrier of access). A torn or
    failed write must be unreadable-as-JSON (→ full rewrite), never readable
    as a match; any failure is logged and swallowed — the only consequence is
    a full rewrite on the next boot.
    """
    payload = {
        "inputs": inputs,
        "outputs": outputs,
        "which": which,
    }
    wrote = False
    try:
        fd, tmp = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
        )
        fd_owned = True
        try:
            platform_compat.fchmod_safe(fd, 0o600)
            if not platform_compat.IS_POSIX:
                platform_compat.restrict_to_owner(tmp)
            with os.fdopen(fd, "w") as fh:
                fd_owned = False  # fdopen owns the descriptor now
                fh.write(json.dumps(payload, sort_keys=True))
            os.replace(tmp, path)
            wrote = True
        finally:
            if fd_owned:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if not wrote:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
    except OSError:
        logger.debug(
            "rewriter: could not persist rewrite fingerprint at %s "
            "(next boot does a full rewrite)",
            path,
            exc_info=True,
        )


def rewrite_agents(
    *,
    source_dir: Path,
    overlay_dir: Path,
    socket_path: Path,
    work_dir: Path,
    sandbox_mode: str = "auto",
    approval_mode: str = "interactive",
    stub_servers: frozenset[str] | None = None,
    pooling_enabled: bool = True,
) -> tuple[dict[str, int], dict[str, str]]:
    """Populate ``overlay_dir`` with rewritten copies of ``source_dir/*.json``.

    Never modifies ``source_dir``. Idempotent — safe to call on every
    Kiro Crew startup. When no input changed since the last completed run
    (see :func:`_rewrite_inputs_fingerprint`) the rewrite loop is skipped and
    the cached ``(results, target_env)`` is returned; the stale-file prune
    still runs on that path.

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
        stub_servers: Server names from ``config.mcp_gateway.stub_servers``.
            A stdio server gets a stub when its name is in this set — that list is
            the ONLY trigger. A per-agent-spec ``poolable: true`` is retired and
            deliberately ignored here: both real gates (the broker start gate and
            the session overlay) read the config list, so honouring the spec key
            produced a stub nothing pointed at. It is still stripped before the
            entry reaches kiro-cli, and still reported as ``entry_poolable`` for
            information only. An unstubbed server is left untouched for the
            session to launch itself, which is what keeps the default free of both
            a daemon and a stub process. ``None`` is treated as an empty set,
            meaning nothing is rewritten at all.
        pooling_enabled: ``config.mcp_gateway.enabled``. Sharing is global over
            the stub set: when ``False`` no stub is marked shareable, so each
            connection gets its own backend while the stubs stay in place — the
            state that lets a stubbed server render UI without co-tenancy.

    Returns:
        A ``(results, target_env)`` tuple:

        * ``results``: mapping ``{agent_filename: wrapped_server_count}``.
          Agents with no MCP servers are omitted.
        * ``target_env``: mapping ``{KIROCREW_MCP_TARGET_<SERVER>: "cmd arg arg"}``
          suitable for ``GatewaySpec.mcp_target_env``. Gatewayd consults
          these when a stub registers, to find the real backend command
          to spawn for a new pool key.
    """
    stub_set = stub_servers or frozenset()
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

    # Skip the whole rewrite when nothing that feeds it changed since the last
    # completed run. The fingerprint stats and digests only the small JSON
    # inputs (no JSON parsing, no per-server ``shutil.which``, no writes), so
    # an unchanged warm boot pays a few reads instead of the full
    # parse+resolve+write pass. Any read/validation failure
    # falls through to the full rewrite — unreadable never means "match".
    kiro_settings_json = source_dir.parent / "settings" / "mcp.json"
    fingerprint_path = overlay_dir / _FINGERPRINT_NAME
    # Read the forwarding flag ONCE per pass: it now decides whether an
    # env-declaring server is pooled at all (not just warning text), so every
    # consumer in this pass must see the same value, and the fingerprint must
    # record it (a flip regenerates the overlays).
    forward_env = forward_declared_env_enabled()
    current_inputs = _rewrite_inputs_fingerprint(
        source_dir=source_dir,
        settings_path=kiro_settings_json,
        overlay_dir=overlay_dir,
        socket_path=socket_path,
        work_dir=work_dir,
        sandbox_mode=sandbox_mode,
        approval_mode=approval_mode,
        stub_set=stub_set,
        pooling_enabled=pooling_enabled,
        forward_env=forward_env,
    )
    stored = _load_fingerprint(fingerprint_path)
    if stored is not None and stored.get("inputs") == current_inputs:
        cached = _cached_rewrite_result(
            stored, overlay_dir=overlay_dir, stubs_dir=stubs_dir
        )
        if cached is not None:
            return cached

    written: set[str] = set()
    written_sidecars: set[str] = set()
    results: dict[str, int] = {}
    target_env: dict[str, str] = {}
    notes = _RewritePassNotes()
    overlay_write_failed = False

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
    settings_src_spec: dict[str, Any] | None = None
    settings_poolable: dict[str, Any] = {}
    if kiro_settings_json.is_file():
        try:
            loaded = json.loads(kiro_settings_json.read_text())
            if isinstance(loaded, dict):
                settings_src_spec = loaded
                settings_poolable = _injectable_settings_servers(
                    loaded, stub_set,
                    pooling_enabled=pooling_enabled,
                    forward_env=forward_env,
                    notes=notes,
                )
        except OSError as exc:
            # Transient read failure: same reasoning as the per-agent site —
            # do not cache a pass that treated an existing settings file as
            # absent (it would also have pruned the settings overlay).
            notes.source_read_failed = True
            logger.warning("failed to read global mcp.json: %s", exc)
        except json.JSONDecodeError as exc:
            # Content problem — cacheable; a fix changes the stat signature.
            logger.warning("failed to read global mcp.json: %s", exc)

    for path in sorted(source_dir.glob("*.json")):
        try:
            spec = json.loads(path.read_text())
        except OSError as exc:
            # Transient: the file stat'ed fine for the fingerprint but could
            # not be read. Readability can return without size/mtime changing,
            # so caching this incomplete pass would serve overlays missing
            # this agent forever. Mark the pass uncacheable.
            notes.source_read_failed = True
            logger.warning("skipping agent %s: %s", path.name, exc)
            continue
        except json.JSONDecodeError as exc:
            # Deterministic: the CONTENT is bad, and fixing it changes the
            # file's stat signature, which invalidates the fingerprint — so
            # this skip is safe to cache.
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
            stub_servers=stub_set,
            pooling_enabled=pooling_enabled,
            forward_env=forward_env,
            inject_servers=settings_poolable,
            target_env=target_env,
            sidecars_written=written_sidecars,
            notes=notes,
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
            overlay_write_failed = True
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
            overlay_write_failed = True
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
    # Persist the fingerprint so the next unchanged boot skips this pass.
    # ``current_inputs`` was stat'ed BEFORE the files were read: if a file
    # changed in between, the stored stats are older than the content the
    # overlays reflect, the next boot's stat mismatches, and the rewrite runs
    # again — an extra rewrite, never a stale overlay. Not cached when any
    # transient fault left the output set incomplete (a later boot must retry
    # even though no fingerprinted input changed).
    uncacheable = ""
    if notes.source_read_failed:
        uncacheable = "transient source read failure(s)"
    elif notes.sidecar_write_failed:
        uncacheable = "env sidecar write failure(s)"
    elif overlay_write_failed:
        uncacheable = "overlay write failure(s)"
    if uncacheable:
        logger.debug("rewriter: %s; not caching this rewrite", uncacheable)
        # Remove any fingerprint from an earlier successful run: it could
        # still match the current inputs (this rewrite may have been forced by
        # a missing output, not an input change) and would freeze the
        # degraded state instead of retrying.
        with contextlib.suppress(OSError):
            fingerprint_path.unlink(missing_ok=True)
    else:
        output_sigs: dict[str, Any] = {
            "overlays": {n: _stat_sig(overlay_dir / n) for n in sorted(written)},
            "sidecars": {
                n: _stat_sig(env_dir / n) for n in sorted(written_sidecars)
            },
            "settings_overlay": (
                _stat_sig(settings_overlay_path)
                if settings_overlay_path is not None
                else None
            ),
        }
        # A None signature means an output vanished between write and stat —
        # storing it would produce a fingerprint the loader rejects anyway;
        # skip storing so the next boot simply rewrites.
        if (
            all(output_sigs["overlays"].values())
            and all(output_sigs["sidecars"].values())
            and (
                settings_overlay_path is None
                or output_sigs["settings_overlay"] is not None
            )
        ):
            _store_fingerprint(
                fingerprint_path,
                inputs=current_inputs,
                outputs=output_sigs,
                which=notes.which_results,
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
    """Populate ``target_env`` with ``KIROCREW_MCP_TARGET_<SERVER>`` entries
    for every wrapped server in ``mcp_servers``.

    Two kinds of entry are written per wrapped server:

    * ``KIROCREW_MCP_TARGET_<SERVER>`` — first-wins across calls, kept as a
      backward-compatible fallback for any pool key whose
      ``command_args_hash`` has no disambiguated entry.
    * ``KIROCREW_MCP_TARGET_<SERVER>__<command_args_hash>`` — one per distinct
      (server, command+args) combination. Two agents that declare the same
      server name with DIFFERENT ``--target-args`` (e.g. ``example-mcp`` with
      ``--include-tool-tags code-review,default`` vs a restricted
      ``--include-tools …`` list) each get their own entry, so
      ``gatewayd.env_target_resolver`` spawns the command matching the
      caller's pool key instead of whichever agent sorted first
      alphabetically. The hash matches ``PoolKey.command_args_hash``.
    """
    for server_name, entry in mcp_servers.items():
        if not isinstance(entry, dict) or not (
            entry.get(_WRAPPER_MARKER) or entry.get(_WRAPPER_MARKER_LEGACY)
        ):
            continue
        env_key = "KIROCREW_MCP_TARGET_" + server_name.replace("-", "_").upper()
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
                    "mcp-gateway rewriter: KIROCREW_MCP_TARGET env-key collision on "
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


def runtime_dir() -> Path:
    """Directory holding the gateway's per-host runtime records.

    Derived from the data home, NOT from ``mcp_gateway.socket_path``. That field
    is empty until the broker has been configured, and the shareability records
    have to exist BEFORE that: their whole purpose is to tell an operator who has
    not enabled stubbing yet whether it is safe to. Keying off the socket made
    the feature inert for exactly the audience it serves.

    Same directory the socket itself defaults into, so when a broker does run its
    files sit alongside these.
    """
    home = os.environ.get("KIROCREW_HOME")
    base = Path(home) if home else config_dir()
    return base / "mcp-gateway"


def default_socket_path() -> Path:
    """Return the default gateway unix socket path."""
    return runtime_dir() / "gateway.sock"


def records_dir(socket_path: str | Path = "") -> Path:
    """Where per-host gateway records live, for BOTH the writer and the reader.

    gatewayd writes next to its actual socket; the dashboard has to read the same
    place, and it may run when no socket is configured at all. One resolver keeps
    those two from diverging — a custom ``socket_path`` would otherwise have the
    daemon writing the hazard ledger somewhere the page never looks.

    Emptiness is tested on the STRING form, and a bare ``"."`` counts as unset:
    ``Path("")`` constructs to ``PosixPath(".")``, so an empty Path is
    indistinguishable from an explicit one and branching on truthiness alone
    would resolve an unconfigured socket to the current working directory.
    A real relative socket (``./gateway.sock``) is unaffected — its string form
    is the filename, not ``"."``.
    """
    as_str = str(socket_path or "")
    if not as_str or as_str == ".":
        return runtime_dir()
    return Path(as_str).parent
