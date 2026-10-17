"""Rewriting one agent spec's MCP servers into broker stubs.

Owner module of :mod:`kiro_crew.mcp_gateway.rewriter`, which re-exports every name
here; read and patch them there. This module owns:

* the per-spec pass (:func:`_rewrite_single_spec`) and the servers a settings file
  injects (:func:`_injectable_settings_servers`);
* the stub entry that replaces a routed server (:func:`_build_stub_entry`), with its
  Windows command-line budget, and the launch-approval check it runs first
  (:func:`_admit_launch`);
* :data:`UNPOOLABLE_SERVERS`, the servers left unwrapped by name.

It logs under the rewriter's logger and never imports the facade.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Mapping

from kiro_crew import platform_compat
from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_entry_is_registry_governed
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, encode_target_args
from kiro_crew.mcp_gateway.launch_approval import (
    LaunchApprovals,
    env_fingerprint,
    launch_fingerprint,
)
from kiro_crew.mcp_gateway.rewriter_env import (
    _expand_env_map,
    _must_stay_wrapped,
    _needs_private_backend,
    _normalized_env,
    _SidecarLedger,
    _withheld_env_count,
)
from kiro_crew.mcp_gateway.rewriter_launch import (
    _CMD_UNSAFE,
    _STUB_MODULE,
    _TARGET_ARGS_FLAG,
    _WRAPPER_MARKER,
    _WRAPPER_MARKER_LEGACY,
    _cmd_safe_command,
    _cmd_unsafe_warn_lock,
    _cmd_unsafe_warned_elements,
    _is_managed_launch,
    _repair_control_plane_entry,
    _resolve_target_command,
    _spec_args,
    _unwrapped_from_prewrapped,
)
from kiro_crew.mcp_gateway.rewriter_overlay import env_sidecar_dir_for_stubs, env_sidecar_name
from kiro_crew.mcp_gateway.secret_uri import routed_for_secret_reference, secret_reference_keys
from kiro_crew.mcp_provenance import WITHHELD_KEY, is_withheld_mute
from kiro_crew.mcp_utils import mcp_server_alias

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.rewriter_launch import _RewritePassNotes

logger = logging.getLogger("kiro_crew.mcp_gateway.rewriter")


# cmd.exe's ceiling is smaller than CreateProcessW's; warn without rejecting
# launchers that do not pass through cmd.exe.
_WINDOWS_CMD_LINE_LIMIT = 8191


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
    sidecars_written: _SidecarLedger | None = None,
    poolable: bool = False,
    identity_keys: Collection[str] = (),
    notes: _RewritePassNotes | None = None,
    read_buffer_limit: int = 0,
) -> dict[str, Any]:
    """Return the rewritten ``mcpServers[name]`` entry.

    ``target_command`` is the ALREADY-RESOLVED absolute backend command, and
    ``env_pairs`` the ALREADY-NORMALIZED declared env — callers run
    :func:`_resolve_target_command` / :func:`_normalized_env` first and skip
    the stub entirely when the command is unresolvable or (for a poolable
    entry) any declared key would be withheld from the shared backend. This
    function therefore never emits a stub whose pooled spawn is a guaranteed
    ENOENT or whose declared env is silently dropped. The one exception is a
    server :func:`_must_stay_wrapped` keeps wrapped, which must fail to start.

    Preserves ``autoApprove`` on the wrapped entry so kiro-cli still honours
    it at the UI layer. ``env`` is cleared on the wrapper — the stub passes
    env separately through its flags so the gateway can hash the
    post-substitution env into the PoolKey.
    """
    target_args: list[str] = _spec_args(original)
    auto_approve: list[str] = list(original.get("autoApprove", []) or [])

    # fmt: off
    stub_args: list[str] = [
        "--server", server_name,
        "--agent", agent_name,
        "--target-command", target_command,
        # Keep both argument boundaries and shell metacharacters inside the payload.
        f"{_TARGET_ARGS_FLAG}={encode_target_args(target_args)}",
        "--sandbox-mode", sandbox_mode,
        "--work-dir", str(work_dir),
        "--approval-mode", approval_mode,
        "--socket", str(socket_path),
        # The per-stream read ceiling. A stub that reads it off argv never
        # imports the config package, which is roughly 140 modules in a process
        # that exists once per session per MCP server. Carried like the other
        # config-derived values above (socket, approval mode, sandbox mode).
        #
        # Resolved ONCE per rewrite by the caller and passed in, not read here:
        # this value is baked into the overlay, so it is an input to
        # ``_rewrite_inputs_fingerprint`` as well, and the two must be the same
        # number. Reading config per entry would also let one pass write two
        # ceilings if the file changed under it, and the daemon sizes its own
        # reader from a single answer.
        "--read-limit", str(read_buffer_limit),
    ]
    # fmt: on
    if poolable:
        stub_args.append("--poolable")
    # Names only, never values — so this is safe on argv, which is
    # world-readable via /proc/<pid>/cmdline (the reason the env itself goes to a
    # 0600 sidecar instead). Only the names the ENTRY actually declares are
    # passed: the flag exists solely so the stub reproduces gatewayd's hash for
    # THIS server, and a fleet-wide list on every stub's argv would be noise that
    # also leaks which variables other servers care about. Sorted for a stable
    # argv, which keeps the overlay byte-identical across passes and so keeps the
    # rewrite fingerprint's skip path effective.
    entry_identity_keys = sorted(k for k in frozenset(identity_keys) if k in env_pairs)
    if entry_identity_keys:
        # A names-only list still needs encoding: its delimiter can be a pipe.
        stub_args.extend(["--pool-identity-env-b64", encode_target_args(entry_identity_keys)])
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
        #
        # ONE FILE PER (agent, server) DECLARATION, which is what makes the
        # file safe to hand to a connection-private backend: this file holds the
        # declaration's full env, rotating secrets included, and two agents
        # declaring one server with different secrets must never land on one
        # name. Naming it after a hash that EXCLUDES those secrets would do
        # exactly that, because ``effective_env_hash`` drops every
        # ``AWS_SECRET``/``AWS_SESSION``/``OAUTH`` key: both agents would write
        # one file, the second write would win, and the first agent's private
        # backend would be spawned with the second agent's credentials.
        env_file = env_dir / env_sidecar_name(agent_name, server_name)
        # One publish path: always staged, and whoever owns the ledger commits.
        # A caller that passed none is not deferring, so a local ledger is
        # committed before this returns.
        ledger = sidecars_written if sidecars_written is not None else _SidecarLedger()
        ledger.add(env_file.name)
        sidecar_ready = False
        try:
            # Protection BEFORE content, not after. The previous order wrote the
            # credentials with atomic_write(mode=0o600) -- inert on Windows --
            # and only then applied the DACL, so an icacls failure left a
            # readable file full of API keys on disk while the except clause
            # merely warned and the stub was still pointed at it. Applying the
            # descriptor to the temp file first means the secret never exists in
            # a readable file at all, and a failure happens before any secret
            # byte is written. os.replace preserves an explicit
            # (non-inherited) descriptor across the rename, which the ledger
            # performs once the referencing overlay is published.
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
                    # Resolve placeholders here (see _expand_env_map): the backend
                    # is spawned from this sidecar, not by kiro-cli.
                    fh.write(
                        json.dumps(
                            _expand_env_map(env_pairs, notes=notes, server=server_name),
                            sort_keys=True,
                        )
                    )
                # Staged, not published: the rewrite pass commits after this
                # agent's overlay write succeeds. The temp is already written and
                # already owner-only -- protection precedes content either way.
                ledger.stage(tmp, env_file)
                sidecar_ready = True
            finally:
                if fd_owned:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                if not sidecar_ready:
                    # Neither staged nor published: this temp is ours to remove.
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
        except OSError:
            logger.warning("rewriter: failed to write env sidecar %s", env_file)
        if sidecar_ready and sidecars_written is None:
            # Local ledger: nobody else will publish it.
            sidecar_ready = ledger.commit()
        if sidecar_ready:
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
                server_name,
                agent_name,
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
        if k
        not in (
            "command",
            "args",
            "env",
            "poolable",
            "autoApprove",
            _WRAPPER_MARKER,
            _WRAPPER_MARKER_LEGACY,
        )
    }
    # ``-P`` (``PYTHONSAFEPATH``) keeps the launch cwd off ``sys.path``:
    # kiro-cli starts the stub in the agent's SESSION cwd, a project checkout
    # Kiro Crew does not control, and a stdlib-named file there (``json.py``,
    # ``socket.py``) would otherwise be imported in place of the standard
    # library. The rewrite fingerprint's ``python_isolation_flags`` records the
    # same option prefix, so an overlay written without it is regenerated on
    # upgrade instead of served from cache.
    stub_argv = platform_compat.isolated_python_argv(
        "-P",
        "-m",
        _STUB_MODULE,
        f"{STUB_FLAGS_FLAG}={encode_target_args(stub_args)}",
    )
    wrapped.update(
        {
            _WRAPPER_MARKER: True,
            # _cmd_safe_command: the interpreter path is the ONE launch-line
            # element outside the stub-flags envelope, and under a
            # ``Program Files`` install it carries the space that makes cmd.exe
            # quote-stripping reachable (see the helper's docstring).
            "command": _cmd_safe_command(stub_argv[0]),
            # The helper's optional ``-s`` and ``-P`` precede
            # ``-m kiro_crew.mcp_gateway.stub``;
            # the stub's own flags follow as ONE encoded envelope.
            # Every value above is raw operator or
            # filesystem text -- the executable path, the work dir, the socket, the
            # sidecar path, the server and agent names, the autoApprove
            # identifiers -- and a CLI that launches this entry through cmd.exe
            # expands ``%NAME%`` inside any plain token, quoted or not, with no
            # escape available on that command line. Only ``--target-args-b64``
            # was encoded before, so ``python%X%.exe`` reached the stub as
            # ``pythonexpanded.exe`` and a tool name ``read%X%`` as ``readexpanded``
            # -- a different executable and a different approval set than the
            # operator wrote, and a different hash than the daemon registered. The
            # base64url alphabet has no ``%`` and no other cmd.exe metacharacter, so
            # the tokens inside arrive byte-for-byte; ``stub._parse_args`` and
            # ``_collect_target_env`` both splice them back through
            # ``expand_stub_flags`` before reading, and an older plain-flag overlay
            # still parses through the same path.
            # channel_id is NOT here: the overlay is written once at startup and is
            # session-agnostic, so it is appended per session by
            # ``session_servers.pooled_session_servers`` at ACP injection time,
            # where the value is in scope.
            "args": stub_argv[1:],
            # autoApprove must stay on the wrapper — kiro-cli reads it at the
            # permission-prompt UI layer, separately from the backend.
            "autoApprove": auto_approve,
            # env cleared — the backend receives env via the gateway's spawn,
            # not via kiro-cli's subprocess environment.
            "env": {},
        }
    )
    if platform_compat.IS_WINDOWS:
        # Diagnosable, not silent: 8.3 short-name generation can be disabled
        # per volume, in which case _cmd_safe_command had nothing safe to
        # return and the hazard is still on the line. Name the element and the
        # remedy so an operator reading the log can connect it to the
        # "connection closed: initialize response" the session will show.
        # Once per distinct element per process (the latch in rewriter_launch): the
        # hazardous element is the process-constant interpreter path, and a
        # repeat per wrapped server would bury the diagnosis.
        for element in (wrapped["command"], *wrapped["args"]):
            residual = _CMD_UNSAFE.intersection(element)
            if not residual:
                continue
            with _cmd_unsafe_warn_lock:
                first = element not in _cmd_unsafe_warned_elements
                _cmd_unsafe_warned_elements.add(element)
            log = logger.warning if first else logger.debug
            log(
                "rewriter: server %r launch argv element %r carries cmd.exe "
                "metacharacter(s) %s after normalisation; a CLI that spawns "
                "MCP servers through cmd.exe may fail to launch this stub. "
                "No usable 8.3 short form was available -- enable 8.3 name "
                "generation on the volume, or install to a path free of "
                "spaces and cmd.exe metacharacters.",
                server_name,
                element,
                "".join(sorted(residual)),
            )
        command_line = subprocess.list2cmdline([wrapped["command"], *wrapped["args"]])
        command_units = len(command_line.encode("utf-16-le")) // 2
        if command_units >= _WINDOWS_CMD_LINE_LIMIT:
            logger.warning(
                "rewriter: server %r generated command is %d UTF-16 units; "
                "cmd.exe limit is %d. Shorten server arguments if initialization fails.",
                server_name,
                command_units,
                _WINDOWS_CMD_LINE_LIMIT,
            )
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
        a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str) for a in args_val
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
    identity_keys: Collection[str] = (),
    read_buffer_limit: int = 0,
    inject_servers: dict[str, Any] | None = None,
    target_env: dict[str, str] | None = None,
    sidecars_written: _SidecarLedger | None = None,
    notes: _RewritePassNotes | None = None,
    approvals: LaunchApprovals | None = None,
) -> tuple[dict[str, Any], int]:
    """Return ``(new_spec, wrapped_count)``. Idempotent.

    ``approvals`` enforces content approval (see
    :mod:`kiro_crew.mcp_gateway.launch_approval`): a stubbed entry is wrapped
    only when its resolved launch is approved for its name, and an entry that
    arrives already wrapped is re-derived rather than trusted.

    ``inject_servers`` is a mapping of ``{name: raw_entry}`` of poolable
    servers sourced from the global ``settings/mcp.json`` that must be made
    available to *this* agent. Each is wrapped with **this agent's** name (so
    the stub carries the correct ``--agent`` identity) and added to the
    overlay unless the agent already declares a server of that name (the
    agent's own declaration always wins). This is how empty-``mcpServers``
    agents get pooled coverage WITHOUT relying on kiro-cli merging the global
    settings — which is what produced the duplicate, empty-``--agent`` stub.

    An agent whose spec sets ``includeMcpJson: false`` gets none of them:
    kiro-cli does not merge the global file into that agent, so there is no raw
    entry for a stub to take precedence over, and every wrapped overlay entry is
    sent at ``session/new``. Injecting there would mount servers the agent
    excluded, and a tool name one of them shares with the agent's own server
    leaves one of the two with no tools. Only a literal ``false`` opts out,
    because kiro-cli reads an absent flag as true.
    """
    agent_name = spec.get("name") or ""
    servers = spec.get("mcpServers") or {}
    if not isinstance(servers, dict):
        servers = {}
    inject = inject_servers or {}
    if spec.get("includeMcpJson") is False:
        inject = {}
    if not servers and not inject:
        return spec, 0

    new_servers: dict[str, Any] = {}
    # Launch signatures (command + args) of every server already wired into this
    # overlay. Consulted to skip injecting a poolable settings server whose resolved
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
        if approvals is not None and (
            entry.get(_WRAPPER_MARKER) is True or entry.get(_WRAPPER_MARKER_LEGACY) is True
        ):
            # The marker is spec text, so it cannot vouch for the target it
            # names. Re-derive the plain entry and let the checks below decide.
            entry = _unwrapped_from_prewrapped(entry)
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
        if mcp_entry_is_registry_governed(entry):
            # A registry-governed entry defers its launch to the administrator's
            # catalog, so there is nothing here to pool. Wrapping it produced a
            # stub the catalog overrides anyway (in registry mode it resolves the
            # entry by map key and supplies its own command) or that the client
            # drops outright (outside registry mode the marked entry is the one
            # dropped) -- while making the name a "stubbed name" the session
            # projections subtract, so the server reached a session as a live
            # local process with the marker governing nothing. Pass it through so
            # the client's own filter decides, like the mute below.
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        if (
            is_withheld_mute(entry)
            and mcp_entry_is_muted(entry)
            and routed_for_secret_reference(name, stub_servers)
        ):
            # The rebuild muted this entry only because it was not routed, and
            # this pass routes it (the launch probe asks before the routing is
            # saved). A wrapped server is started by the gateway, which resolves
            # the reference, so the mute is lifted here rather than read as the
            # user's: otherwise the launch the routing switch must approve is
            # never captured.
            entry = {k: v for k, v in entry.items() if k not in (WITHHELD_KEY, "disabled")}
        if mcp_entry_is_muted(entry):
            # Honour the user's mute: a server disabled in the agent spec must
            # never be wrapped into a live pooling stub. Read fail-closed, so a
            # non-boolean ``disabled`` mutes too -- reading only a literal ``True``
            # wrapped such an entry, and a wrapped name is subtracted from the
            # session projections as a broker stub before their own mute check
            # runs, which mounted the server the spec had silenced.
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
        if name not in stub_servers and not (
            secret_reference_keys(entry.get("env"))
            and routed_for_secret_reference(name, stub_servers)
        ):
            # A spec key is the slash-free alias, while the routing list may hold
            # the raw name. A server carrying a vault reference is routed under
            # either form, by the same helper the agent-spec rebuild asks before
            # it lets the server through; every other server keeps the
            # exact-name rule.
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        # A reserved kirocrew-* name launches the managed invocation, whatever
        # the spec spelled -- before resolution, so the target the stub hashes,
        # the target env the daemon spawns and the gate's expectation are one
        # value. See ``_repair_control_plane_entry``.
        entry = _repair_control_plane_entry(name, entry, agent_name)
        entry_env = _normalized_env(entry, context=f"server {name!r} for agent {agent_name!r}")
        resolved_cmd = _resolve_target_command(str(entry.get("command", "")), entry_env, notes)
        # See :func:`_must_stay_wrapped`: such a server is wrapped even when the
        # checks below would leave it unwrapped, so it fails closed instead.
        stay_wrapped = _must_stay_wrapped(entry_env)
        # Set only when a check below would have left the server unwrapped and
        # it is wrapped anyway; such a stub is not pooled.
        forced_wrap = False
        if not resolved_cmd and stay_wrapped:
            forced_wrap = True
            logger.warning(
                "rewriter: cannot resolve MCP command %r for opted-in server "
                "%r (agent %r) on the gateway search path, and its env holds a "
                "secret:// reference; wrapping it anyway so it fails to start "
                "rather than start with the reference unresolved.",
                entry.get("command", ""),
                name,
                agent_name,
            )
            resolved_cmd = str(entry.get("command", ""))
        if not resolved_cmd:
            # An unresolvable bare command means gatewayd's spawn is a
            # guaranteed ENOENT (it runs under the systemd --user PATH), and
            # emitting a stub anyway degrades EVERY session through a
            # spawn-fail → fallback-exec cycle. Leave the
            # entry unwrapped instead: kiro-cli's own spawn environment may
            # still resolve the name, and if it cannot, the failure surfaces
            # in the session where the operator can see it.
            logger.warning(
                "rewriter: cannot resolve MCP command %r for opted-in server "
                "%r (agent %r) on the gateway search path; leaving it "
                "unwrapped so the session launches it directly. Use an "
                "absolute path in the spec to pool it.",
                entry.get("command", ""),
                name,
                agent_name,
            )
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        withheld = (
            _withheld_env_count(entry_env, forward_env, identity_keys) if pooling_enabled else 0
        )
        # A server whose env names a vault secret is the one case where leaving it
        # unwrapped cannot work; see :func:`_needs_private_backend`.
        private = _needs_private_backend(withheld, entry_env)
        if withheld and not private:
            # A pooled backend is spawned WITHOUT
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
                name,
                agent_name,
                len(entry_env),
                (
                    "mcp_gateway.forward_declared_env is off — enable it to pool"
                    if not forward_env
                    else "rotating-secret/credential keys are never forwarded — "
                    "the backend must read them from disk to pool"
                ),
            )
            new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
            continue
        if not _admit_launch(
            approvals,
            name=name,
            agent_name=agent_name,
            entry=entry,
            resolved_cmd=resolved_cmd,
            entry_env=entry_env,
        ):
            if not stay_wrapped:
                new_servers[name] = {k: v for k, v in entry.items() if k != "poolable"}
                continue
            # A refused launch that stays wrapped is still never exec'd by the
            # gateway: ``filter_target_env`` drops its target, so it is refused.
            forced_wrap = True
        private = private or forced_wrap
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
            # per-server decision -- except a server that could not be shared
            # with its env intact and names a vault secret (``private`` above).
            poolable=pooling_enabled and not private,
            identity_keys=identity_keys,
            read_buffer_limit=read_buffer_limit,
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
                        alias,
                        agent_name,
                        inject_cmd,
                        existing_cmd,
                    )
                    continue
        entry_env = _normalized_env(
            entry, context=f"settings server {alias!r} for agent {agent_name!r}"
        )
        resolved_cmd = _resolve_target_command(str(entry.get("command", "")), entry_env, notes)
        inject_stay_wrapped = _must_stay_wrapped(entry_env)
        inject_forced = False
        if not resolved_cmd and inject_stay_wrapped:
            # Wrapped anyway, for the reason in :func:`_must_stay_wrapped`.
            resolved_cmd = str(entry.get("command", ""))
            inject_forced = True
        if not resolved_cmd:
            # Membership in ``inject`` was vetted by
            # _injectable_settings_servers (same resolver, same pass), so this
            # only fires on a filesystem race between the two probes. What
            # matters is NOT publishing a wrapped stub whose pooled spawn is a
            # guaranteed ENOENT. The RAW (unwrapped) copy written instead is
            # inert at ``session/new`` — only marker-carrying stub entries are
            # lifted (see ``session_servers.pooled_session_servers``) — so the
            # session's access to this server comes from kiro-cli's own merge
            # of the real settings file, exactly as if the server had never
            # been vetted; the raw copy only keeps the overlay mirroring the
            # injection set this pass decided on.
            logger.warning(
                "rewriter: settings server %r became unresolvable between "
                "vetting and injection; injecting it unwrapped into agent %r",
                alias,
                agent_name,
            )
            new_servers[alias] = {k: v for k, v in entry.items() if k != "poolable"}
            seen_targets.add(inject_sig)
            continue
        if not _admit_launch(
            approvals,
            name=alias,
            agent_name=agent_name,
            entry=entry,
            resolved_cmd=resolved_cmd,
            entry_env=entry_env,
        ):
            if not inject_stay_wrapped:
                # Not injected at all: kiro-cli's own merge of the real settings
                # file still gives the session this server, launched in-sandbox.
                continue
            inject_forced = True
        # The vetting pass let a withheld-env server through only for this reason.
        inject_private = inject_forced or (
            pooling_enabled
            and _needs_private_backend(
                _withheld_env_count(entry_env, forward_env, identity_keys), entry_env
            )
        )
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
            poolable=pooling_enabled and not inject_private,
            identity_keys=identity_keys,
            read_buffer_limit=read_buffer_limit,
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
    identity_keys: Collection[str] = (),
    notes: _RewritePassNotes | None = None,
) -> dict[str, Any]:
    """Return ``{raw_name: raw_entry}`` of stdio servers in the global
    ``settings/mcp.json`` that must be INJECTED into every per-agent overlay
    whose agent reads that file (an ``includeMcpJson: false`` agent does not;
    see :func:`_rewrite_single_spec`).

    Each returned server is wrapped with the receiving agent's own name, so the
    stub carries a correct ``--agent`` identity, and injected at ACP
    ``session/new`` — where a session-injected server takes precedence over the
    raw same-named entry kiro-cli merges from the real settings file (see
    ``session_servers.py``). That precedence is what prevents the duplicate /
    empty-``--agent`` collision; the settings file itself is never modified and
    no settings overlay is written.

    Servers NOT returned are left entirely to kiro-cli's own settings merge:
    HTTP/SSE servers need no stub, and an unstubbed, unresolvable, or
    env-withholding server keeps its pre-pooling behaviour (launched
    per-session with its own environment) rather than being pooled into a
    stub whose spawn would fail or run credential-less.

    Keys are the RAW settings names. Stub membership is tested under both the
    raw name and the slash-free alias, since the config may carry either
    spelling.
    """
    servers = settings_spec.get("mcpServers") or {}
    out: dict[str, Any] = {}
    if not isinstance(servers, dict):
        return out
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if mcp_entry_is_registry_governed(entry):
            # Never inject a catalog-governed server as a live stub, for the
            # reason the wrap guard states -- and here it would enter EVERY
            # agent's overlay at once.
            continue
        if mcp_entry_is_muted(entry):
            # Honour the user's mute: a server disabled in settings/mcp.json must
            # never be injected as a live stub (which would silently re-enable it
            # in every agent overlay). Fail-closed like the wrap guard above: the
            # two decide the same thing about the same field.
            continue
        if name in UNPOOLABLE_SERVERS:
            continue
        if entry.get(_WRAPPER_MARKER) is True:
            # Source settings should be raw; ignore an already-wrapped entry.
            continue
        if "command" not in entry:
            # HTTP/SSE — no stub needed; kiro-cli merges it from the real
            # settings file.
            continue
        if not (name in stub_servers or mcp_server_alias(name) in stub_servers):
            # Not stubbed: leave it to kiro-cli's own merge of the real
            # settings file, so the session launches it directly.
            continue
        # Same repair as the per-agent site: a reserved kirocrew-* name declared
        # here launches the managed invocation too, so the settings source and
        # the agent-spec source of one control plane cannot diverge (the ACP
        # projection already repairs both).
        entry = _repair_control_plane_entry(name, entry, "settings/mcp.json")
        entry_env = _normalized_env(entry, context=f"settings server {name!r}")
        if not _resolve_target_command(
            str(entry.get("command", "")), entry_env, notes
        ) and not _must_stay_wrapped(entry_env):
            # Settings edition of the unresolvable-command guard: an
            # unresolvable bare command must not be pooled into a stub whose
            # spawn is a guaranteed ENOENT. Leaving it out of the injection
            # set keeps the unpooled behaviour (kiro-cli merges the real
            # settings file and launches it with its own environment).
            logger.warning(
                "rewriter: cannot resolve MCP command %r for opted-in "
                "settings server %r on the gateway search path; leaving it "
                "to kiro-cli's own settings merge. Use an absolute path to "
                "pool it.",
                entry.get("command", ""),
                name,
            )
            continue
        withheld = (
            _withheld_env_count(entry_env, forward_env, identity_keys) if pooling_enabled else 0
        )
        if withheld and not _needs_private_backend(withheld, entry_env):
            # Settings edition of the withheld-env guard: pooling would
            # withhold part or all of this server's declared env and
            # crash-loop it.
            # Leave it raw.
            # Same CodeQL constraint as the per-agent site: log only the
            # total declared count, never the secret-predicate-derived one.
            logger.warning(
                "rewriter: opted-in settings server %r declares env "
                "(%d keys) of which some would be withheld from a shared "
                "backend (%s); leaving it to kiro-cli's own settings merge.",
                name,
                len(entry_env),
                (
                    "mcp_gateway.forward_declared_env is off — enable it to pool"
                    if not forward_env
                    else "rotating-secret/credential keys are never forwarded — "
                    "the backend must read them from disk to pool"
                ),
            )
            continue
        out[name] = entry
    return out


def _admit_launch(
    approvals: LaunchApprovals | None,
    *,
    name: str,
    agent_name: str,
    entry: Mapping[str, Any],
    resolved_cmd: str,
    entry_env: dict[str, Any],
) -> bool:
    """Whether a stubbed entry may be wrapped, i.e. launched by gatewayd outside the sandbox.

    ``approvals`` of ``None`` is a caller that enforces nothing (tests and
    tools that only render overlays). Otherwise the launch's content must be
    approved for this server name; a refusal leaves the entry unwrapped, so the
    session launches it inside its own sandbox.
    """
    if approvals is None:
        return True
    target_args = _spec_args(entry)
    fingerprint = launch_fingerprint(resolved_cmd, target_args, entry_env)
    derived_env_hash = env_fingerprint(_expand_env_map(entry_env))
    if not approvals.admit(
        name,
        fingerprint,
        managed=_is_managed_launch(name, entry),
        launch=(resolved_cmd, target_args),
        # The declared text, not the expansion: placeholders stay unexpanded so a
        # host secret behind ``${VAR}`` is never rendered on the dashboard.
        env=entry_env,
        derived_env_hash=derived_env_hash,
    ):
        # Names only: the args and env may carry tokens.
        logger.warning(
            "mcp launch approvals: stubbed server %r (agent %r) resolves to a launch "
            "the operator has not approved; leaving it unwrapped so the session "
            "launches it inside its sandbox. Re-approve it from the MCP page.",
            name,
            agent_name,
        )
        return False
    return True
