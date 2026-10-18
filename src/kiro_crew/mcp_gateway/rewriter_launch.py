"""The launch line of one server entry, as the rewriter reads and writes it.

Owner module of :mod:`kiro_crew.mcp_gateway.rewriter`, which re-exports every name
here; read and patch them there. This module owns:

* resolving a declared command on the gateway's search path, with its Windows
  casing and a ``cmd.exe``-safe spelling (:func:`_resolve_target_command`,
  :func:`_cmd_safe_command`);
* the managed control plane's own entry and its repair
  (:func:`_repair_control_plane_entry`, :func:`_owned_control_plane_env`);
* the markers and flags that say an entry already routes through the stub, and
  reading the target back out of such an entry (:func:`_wrapped_target`,
  :func:`_unwrapped_from_prewrapped`);
* :class:`_RewritePassNotes`, what one pass observed that decides whether it may be
  cached.

It logs under the rewriter's logger and never imports the facade.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any, Collection, Mapping

from kiro_crew import platform_compat
from kiro_crew.env import mcp_search_path, resolved_command_casing, spec_path_key
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.hashing import decode_target_args, expand_stub_flags

logger = logging.getLogger("kiro_crew.mcp_gateway.rewriter")


@dataclass
class _RewritePassNotes:
    """Observations from one full rewrite pass that decide cacheability.

    ``which_results`` records every ``shutil.which`` probe as
    ``(bare_command, search_path) -> resolved-or-""``, with Windows on-disk
    casing restored. The resolved path is an
    OUTPUT of filesystem state the stat-based fingerprint cannot see (a binary
    removed from, added to, or shadowed within an unchanged PATH), so the
    cache-hit path re-runs exactly these probes and compares — a disagreement
    in either direction forces the full rewrite.

    ``env_placeholder_seen`` records that a declared env contained a
    ``${VAR}``/``${env:VAR}`` reference. The resolved VALUE lands in the
    sidecar, so it is an input the stat-based fingerprint cannot see — an
    exported variable changing between boots would otherwise keep serving a
    sidecar expanded against the old environment (a rotated credential would
    silently keep flowing the old value for as long as no file changed).
    Rather than fingerprint the environment, such a pass is simply not cached:
    the placeholder case re-resolves on every boot and cannot go stale, while
    every spec without a placeholder keeps the cache untouched.

    ``sidecar_write_failed`` and ``source_read_failed`` mark transient I/O
    faults: the produced output set is incomplete for reasons that can clear
    without any fingerprinted input changing, so the run must not be cached
    (and a previous run's fingerprint must be removed, or it could still match
    and freeze the degraded state).
    """

    which_results: dict[str, str] = field(default_factory=dict)
    env_placeholder_seen: bool = False
    sidecar_write_failed: bool = False
    source_read_failed: bool = False
    # A source that vanished between the listing and the read. The skip is
    # deterministic, but the input signatures were taken while the file still
    # existed: caching would let the same file, restored with the same size and
    # mtime, match a fingerprint whose outputs lack its overlay.
    source_deleted_mid_pass: bool = False


# Separator inside a stored which-probe key (bare command NUL search-path).
# NUL is legal in JSON strings and cannot appear in either component.
_WHICH_KEY_SEP = "\0"


# Marker field set on rewritten MCP entries so repeat runs are idempotent.
_WRAPPER_MARKER = "_kirocrew_mcp_gateway_wrapped"

# Legacy marker from pre-fork naming; accepted on read for overlays written by
# older rewriter versions that haven't been regenerated yet.
_WRAPPER_MARKER_LEGACY = "_mc_mcp_gateway_wrapped"


# Read compatibility for overlays carrying delimiter-separated arguments.
_TARGET_ARGS_SEP = "|"
_TARGET_ARGS_FLAG = "--target-args-b64"
_TARGET_ARGS_FLAG_LEGACY = "--target-args"

#: The stub is launched as a module by the interpreter running Kiro Crew.
#: ``sys.executable`` is baked into the overlay rather than resolved at
#: launch time because kiro-cli strips env when it spawns MCP
#: subprocesses, so neither a propagated var nor a ``python3`` on PATH
#: that can import ``kiro_crew`` is guaranteed.
#:
#: Aliased from the package constant so the launch line and the cmdline
#: fingerprint the Sessions surface counts stubs by cannot drift apart.
_STUB_MODULE = STUB_MODULE


# cmd.exe metacharacters. kiro-cli launches MCP entries on Windows through
# ``cmd.exe /C``, which re-parses the assembled line: a quoted element beyond
# the first trips the outer quote-stripping rule ("starts with a quote and has
# more than two quotes -> drop the first and last"), ``%NAME%`` spans are
# expanded inside ANY token, quoted or not, with no escape available, and
# delayed expansion likewise expands ``!NAME!`` spans. Parentheses become live
# command-grouping characters the moment the stripped line leaves them unquoted
# (``C:\Program Files (x86)\...``). An element
# carrying one of these characters can therefore be destroyed before the
# child ever runs. The stub's own flags already cross cmd.exe safely inside
# the ``STUB_FLAGS_FLAG`` base64url envelope; this set exists for the
# elements that must stay plain text on the launch line. 8.3 short names are
# uppercase alphanumerics plus ``~`` and ``.``, so they can never carry ``!``
# or any other member of this set; successful short-form resolution converges.
_CMD_UNSAFE = frozenset(' "^%|&<>()!')

# Launch-argv elements already warned about as residual cmd.exe hazards, so
# the notice fires once per distinct element per process instead of once per
# wrapped server: the hazardous element is the process-constant interpreter
# path, and N agents x M servers of identical lines would bury the one-line
# diagnosis the guard exists to deliver. Same latch pattern as
# ``rewriter_env._collision_warned_keys``. Repeats drop to DEBUG.
_cmd_unsafe_warn_lock = threading.Lock()
_cmd_unsafe_warned_elements: set[str] = set()


def _reset_cmd_unsafe_warnings() -> None:
    """Clear the per-process residual-hazard warning latch (a test hook,
    mirroring :func:`_reset_collision_warnings`)."""
    with _cmd_unsafe_warn_lock:
        _cmd_unsafe_warned_elements.clear()


def _cmd_safe_command(path: str) -> str:
    """Return *path* in a spelling free of cmd.exe metacharacters.

    The wrapped entry's ``command`` is the one element of its launch line that
    is not carried inside the base64url stub-flags envelope, so it alone still
    crosses cmd.exe as plain text. Under a ``Program Files`` install the
    interpreter path contains a space, kiro-cli quotes it, and the line then
    survives only through cmd's exactly-two-quotes special case -- one more
    quoted element anywhere on the line and the outer quotes are stripped,
    turning the interpreter into ``C:\\Program``. A ``%`` in the path is worse:
    cmd expands ``%NAME%`` spans even inside quotes. Resolving to the 8.3
    short form (same file, no metacharacters) removes the hazard entirely.

    POSIX paths and already-safe paths are returned unchanged, and a short
    form that still carries a metacharacter is discarded in favour of the
    original. When no usable short form exists (8.3 generation can be
    disabled per volume) the original is returned and the caller's argv guard
    logs the residual hazard.

    The short form is re-resolved on every call, so a volume's 8.3
    availability changing under a running process is picked up by the next
    rewrite rather than pinned to a stale process-lifetime answer.
    """
    if not platform_compat.IS_WINDOWS or not path:
        return path
    if not _CMD_UNSAFE.intersection(path):
        return path
    short = platform_compat.short_path_name(path)
    if short and not _CMD_UNSAFE.intersection(short):
        return short
    return path


def _resolve_target_command(
    target_command: str,
    env_pairs: dict[str, Any],
    notes: _RewritePassNotes | None,
) -> str:
    """Resolve an MCP target command to an absolute path, or ``""``.

    A bare command that resolves on no searched directory ENOENTs on every
    pooled spawn, while kiro-cli's own spawn environment may still resolve it
    for the SESSION's exec. The daemon's PATH carries the managed launcher
    dirs (:func:`kiro_crew.env.mcp_runtime_path`), so a pooled backend
    searches them too; what this pass settles is the verdict itself, once at
    rewrite time and ahead of any spawn. The search is
    :func:`kiro_crew.env.mcp_search_path` — literally the same composition the
    MCP probe and the agent-config resolver use (spec ``env.PATH`` first, then
    the contributed MCP directories, then the augmented host PATH) — so a
    server that probes healthy on the dashboard can never ENOENT in gatewayd.

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
            # PATHEXT did not synthesize this spelling: it came from the
            # operator's spec. Preserve it exactly, including any deliberate
            # file or directory alias, and keep absolute paths outside the
            # bare-command probe cache as they were before schema 6.
            return target_command
        return ""
    # spec_path_key, not a literal "PATH" lookup: Windows-authored specs
    # legitimately spell it "Path" and the child's loader honours it.
    path_key = spec_path_key(env_pairs) if isinstance(env_pairs, dict) else None
    env_path = env_pairs.get(path_key, "") if path_key else ""
    # mcp_search_path is the canonical RESOLUTION composition the MCP probe and
    # the agent-config resolver also use: the spec's own env.PATH entries FIRST
    # (an operator pin must win), then the contributed MCP directories, then the
    # augmented host PATH. It also degrades a non-string PATH and dedups, so one
    # malformed hand-edited spec cannot abort the rewrite pass.
    search_path = mcp_search_path(env_path)
    resolved = resolved_command_casing(shutil.which(target_command, path=search_path))
    if notes is not None:
        notes.which_results[f"{target_command}{_WHICH_KEY_SEP}{search_path}"] = resolved
    return resolved


def _spec_args(entry: Mapping[str, Any]) -> list[str]:
    """An entry's ``args`` as strings, iterating only a list or tuple.

    Agent JSON is hand-editable, so ``"args": 8080`` or ``"args": "--flag"`` is
    an easy thing to write; a comprehension over it raises ``TypeError`` out of
    ``_rewrite_single_spec`` and aborts the rewrite pass for EVERY agent, leaving
    the broker unstarted (the same class ``_hashable_args`` and ``_normalized_env``
    guard against). A non-sequence reads as no args.
    """
    raw = entry.get("args")
    return [str(a) for a in raw] if isinstance(raw, (list, tuple)) else []


def _managed_invocation(name: str) -> dict[str, Any] | None:
    """The command/args Kiro Crew itself would launch *name* with, or ``None``.

    ``include_opt_in`` because the question here is what a granted reserved
    name IS, not whether a rebuild would grant it -- the spec already did.
    """
    # Function-local on purpose, not a circular import: ``gatewayd`` imports
    # this module at boot and deliberately keeps ``kiro_crew.agent`` OFF the
    # daemon's boot path (see ``CONTROL_PLANE_BACKENDS`` there, read from the
    # ``mcp_cleanup`` leaf for exactly this reason, and the same lazy import in
    # ``gatewayd._spawns_own_control_plane``). A top-level import here would
    # put it back. Runs once per reserved entry per rewrite pass, not per spawn.
    from kiro_crew.agent import managed_mcp_spec_entry

    try:
        return managed_mcp_spec_entry(name, include_opt_in=True)
    except Exception:  # pragma: no cover - defensive; the callee already catches
        logger.debug("rewriter: managed invocation for %r unavailable", name, exc_info=True)
        return None


def _managed_control_plane_signature(stub_set: Collection[str]) -> dict[str, list[Any]]:
    """``{name: [command, args]}`` for every stubbed reserved name that resolves.

    The fingerprint input behind schema 9: what ``_repair_control_plane_entry``
    launches, per name, so a moved managed binary regenerates the overlays.
    """
    out: dict[str, list[Any]] = {}
    for name in sorted(KIROCREW_BIN_MCP_SERVERS):
        if name not in stub_set:
            continue
        managed = _managed_invocation(name)
        if isinstance(managed, dict) and managed.get("command"):
            out[name] = [str(managed["command"]), _spec_args(managed)]
    return out


def _repair_control_plane_entry(
    name: str, entry: dict[str, Any], agent_name: str
) -> dict[str, Any]:
    """Re-derive a reserved ``kirocrew-*`` entry's launch from the managed source.

    A spec's ``command`` for one of Kiro Crew's own servers cannot be authored
    correctly by hand: the managed path embeds the data home and the installed
    version (Toolbox: ``~/.toolbox/tools/kirocrew/<ver>/bin/kirocrew``; macOS
    payload: ``.../backend-dist/kirocrew-backend-<arch>/bin/kirocrew``), so the
    only hand-writable spelling is the bare launcher ``kirocrew``. That
    resolves through the shared Toolbox dispatcher (``toolbox-exec``), a
    different file from the versioned binary, and
    ``gatewayd._spawns_own_control_plane`` -- which compares the spawned
    binary by realpath against exactly this managed entry -- then denies the
    session token: every ``kirocrew-core`` / ``kirocrew-cron`` tool answers
    ``identity_unattested`` while the server LOOKS mounted. A
    spec pinned to a versioned path fails the same way one upgrade later, when
    that binary is reaped.

    So a reserved name never launches what the spec says; it launches what the
    managed source of truth says, the way ``agent._enforce_managed_mcp_ownership``
    rewrites the disk entry and the codex/opencode projections REPLACE theirs.
    This weakens nothing downstream: the daemon's gate still runs on the
    command about to be exec'd, which is now the one it was written to accept,
    and a spec that named a THIRD-PARTY binary under a reserved name gets our
    binary, not the token for theirs. The grant itself stays the spec's: an
    entry the spec does not carry is not conjured here, and the restriction
    fields it declares (``_RESERVED_ENTRY_SPEC_KEYS``) carry over. The launch is
    the managed declaration's, a spec ``autoApprove`` is dropped (kiro-cli
    honours it before Kiro Crew's PreToolUse gate runs), and the declared
    ``env`` is held to the managed-entry ownership rule
    (:func:`_owned_control_plane_env`) before the forwarding rules see it,
    exactly as the disk and ACP consumers of this population do.

    ``None`` from the managed source (name not managed, ``spec_gate`` closed,
    invocation unresolvable) leaves the entry as declared; the daemon's gate
    then rules on it as before.
    """
    if name not in KIROCREW_BIN_MCP_SERVERS:
        return entry
    managed = _managed_invocation(name)
    if not isinstance(managed, dict) or not managed.get("command"):
        return entry
    declared_cmd = str(entry.get("command", ""))
    declared_args = _spec_args(entry)
    managed_cmd = str(managed["command"])
    managed_args = _spec_args(managed)
    if declared_cmd != managed_cmd or declared_args != managed_args:
        # The declared ``args`` never reach the log: a spec may carry a token in
        # them, and this warning persists in the gateway's log. The command and
        # the argument COUNT are enough to find the entry; the managed
        # invocation is ours, so it is safe to print in full.
        logger.warning(
            "rewriter: agent %r declares reserved server %r as command %s with %d "
            "argument(s); launching the managed invocation %s instead so the daemon "
            "can attest it (a hand-authored command for a kirocrew-* server cannot "
            "match the installed binary across users or upgrades)",
            agent_name,
            name,
            repr(declared_cmd) if declared_cmd else "<none>",
            len(declared_args),
            shlex.join([managed_cmd, *managed_args]),
        )
    # Composed from the managed source OUTWARD, never by copying the spec and
    # swapping two keys. Three rounds of review found spec-controlled fields
    # riding into our binary through that copy (a launcher-choosing ``env``, a
    # ``KIROCREW_*`` override, an ``autoApprove`` that kiro-cli honours before
    # Kiro Crew's PreToolUse gate ever runs), one field per round. The class is
    # the copy, so this is the disk writer's shape instead
    # (``agent._enforce_managed_mcp_ownership``): the launch is ours; the
    # restriction fields kiro-cli reads for the spec's grant carry over
    # (``_RESERVED_ENTRY_SPEC_KEYS``); ``env`` is held to the ownership rule;
    # ``autoApprove`` is dropped and named -- a hand-written grant on a reserved
    # name would approve our tools inside kiro-cli and skip the governance gate,
    # and no managed declaration carries one to apply instead; every other key
    # is dropped and named.
    repaired: dict[str, Any] = {"command": managed_cmd, "args": managed_args}
    for key in _RESERVED_ENTRY_SPEC_KEYS:
        if key in entry:
            repaired[key] = entry[key]
    declared_env = entry.get("env")
    if isinstance(declared_env, dict) and declared_env:
        owned_env = _owned_control_plane_env(declared_env, name=name, agent_name=agent_name)
        if owned_env:
            repaired["env"] = owned_env
    for dropped in sorted(set(entry) - set(repaired) - {"env", "autoApprove", "poolable"}):
        logger.warning(
            "rewriter: dropping %r from reserved server %r (agent %r): a managed entry"
            " carries only the launch, the restriction fields and an owned env",
            dropped,
            name,
            agent_name,
        )
    if "autoApprove" in entry:
        logger.warning(
            "rewriter: reserved server %r (agent %r) declares autoApprove; kiro-cli would"
            " honour it ahead of the PreToolUse gate, so the spec's is not applied",
            name,
            agent_name,
        )
    return repaired if repaired != entry else entry


#: Spec fields a repaired reserved entry keeps from the SPEC: they narrow the
#: grant kiro-cli applies (mute, per-tool disable, timeout) and, being the
#: spec's, cannot widen what our binary does. ``command``/``args`` are the
#: managed declaration's, ``autoApprove`` is dropped; ``env`` goes through
#: :func:`_owned_control_plane_env`. Mirrors the allow-list half of
#: ``agent._MANAGED_MCP_ENTRY_KEYS``; a key absent here is dropped, not carried.
_RESERVED_ENTRY_SPEC_KEYS: tuple[str, ...] = ("type", "timeout", "disabled", "disabledTools")


def _owned_control_plane_env(
    declared: dict[str, Any], *, name: str, agent_name: str
) -> dict[str, str]:
    """A reserved name's declared ``env``, held to the managed-entry ownership rule.

    The launch is ours, so the environment that launch receives answers to the
    same rule the other two consumers of this population apply --
    ``agent._enforce_managed_mcp_ownership`` for the disk entry and
    ``acp.session_mcp._managed_element_env`` for the ACP element -- in the same
    order: ``sanitize_spec_env`` drops the loader channels and Kiro Crew's own
    reserved namespace (a spec-declared ``KIROCREW_APPROVAL_MODE=auto`` would
    otherwise reach a tokened control plane and let its subagents skip
    approval), then the home-deriving and launcher-exec classes go. What
    survives is the ordinary variable an operator may legitimately declare;
    the managed env itself is the daemon's own and needs no pin here.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.env import sanitize_spec_env

    # ``declared.items()`` as-is: ``sanitize_spec_env`` validates key and value
    # types itself, and stringifying first would turn a malformed value (a dict,
    # ``None``) into a live variable instead of a dropped one.
    env = sanitize_spec_env(declared.items())
    for key in [k for k in env if k.upper() in agent_mod._HOME_DERIVING_ENV_KEYS]:
        env.pop(key, None)
        logger.warning(
            "rewriter: dropping %r from reserved server %r (agent %r): it would move the"
            " data home this control plane shares with the gateway",
            key,
            name,
            agent_name,
        )
    for key in [k for k in env if k.upper() in agent_mod._LAUNCHER_EXEC_ENV_KEYS]:
        env.pop(key, None)
        logger.warning(
            "rewriter: dropping %r from reserved server %r (agent %r): it would choose what"
            " this control plane executes rather than configure it",
            key,
            name,
            agent_name,
        )
    return env


def _wrapped_target(entry: Mapping[str, Any]) -> tuple[str, list[str]] | None:
    """The ``(target command, target args)`` a wrapped stub entry launches, or ``None``.

    Same splice and precedence as the stub's parser, so this reads exactly what
    the stub would hand gatewayd -- and a plain-flag overlay identically.
    """
    args = expand_stub_flags(entry.get("args", []) or [])
    target_cmd: str | None = None
    target_args_b64: str | None = None
    target_args_legacy = ""
    target_args_sep = _TARGET_ARGS_SEP
    i = 0
    while i < len(args):
        token = str(args[i])
        flag, equals, value = token.partition("=")
        if flag in {
            "--target-command",
            _TARGET_ARGS_FLAG,
            _TARGET_ARGS_FLAG_LEGACY,
            "--target-args-sep",
        }:
            if not equals:
                if i + 1 >= len(args):
                    break
                i += 1
                value = str(args[i])
            if flag == "--target-command":
                target_cmd = value
            elif flag == _TARGET_ARGS_FLAG:
                target_args_b64 = value
            elif flag == _TARGET_ARGS_FLAG_LEGACY:
                target_args_legacy = value
            else:
                target_args_sep = value
        i += 1
    if not target_cmd:
        return None
    if target_args_b64 is not None:
        raw_target_args = decode_target_args(target_args_b64)
    else:
        raw_target_args = target_args_legacy.split(target_args_sep) if target_args_legacy else []
    return target_cmd, raw_target_args


def _is_managed_launch(name: str, entry: Mapping[str, Any]) -> bool:
    """Whether *entry* is the managed invocation of a reserved ``kirocrew-*`` name.

    Such a launch is derived by :func:`_repair_control_plane_entry` from Kiro
    Crew's own install, not from anything agent-writable, so it needs no
    operator approval of its content.
    """
    if name not in KIROCREW_BIN_MCP_SERVERS:
        return False
    managed = _managed_invocation(name)
    if not isinstance(managed, dict) or not managed.get("command"):
        return False
    return str(entry.get("command", "")) == str(managed["command"]) and _spec_args(
        entry
    ) == _spec_args(managed)


def _unwrapped_from_prewrapped(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Re-derive a plain entry from one that arrived already carrying the wrapper.

    The marker is an input the spec author controls, so it proves nothing: the
    target it names goes back through the ordinary stub decision instead of
    being trusted. Its declared env is not recovered -- the ``--env-file`` path
    is spec-chosen too, and reading it would copy an arbitrary file into a
    sidecar. An entry whose target cannot be read loses the marker and is left
    for the session to launch.
    """
    kept = {
        k: v
        for k, v in entry.items()
        if k not in ("command", "args", "env", "poolable", _WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY)
    }
    try:
        target = _wrapped_target(entry)
    except ValueError:
        target = None
    if target is None:
        return {
            k: v for k, v in entry.items() if k not in (_WRAPPER_MARKER, _WRAPPER_MARKER_LEGACY)
        }
    command, args = target
    kept["command"] = command
    kept["args"] = list(args)
    return kept
