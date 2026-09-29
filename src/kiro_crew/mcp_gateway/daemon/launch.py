"""What the daemon launches for a PoolKey: the command, the env, and the approval.

Target resolution reads the ``KIROCREW_MCP_TARGET_<SERVER>`` map baked into the
daemon's environment and spawns only a launch the operator approved
(:mod:`~kiro_crew.mcp_gateway.launch_approval`). The declared-env projection reads
the rewriter's ``0600`` sidecar and forwards only what is coherent with the PoolKey
and approved. One acquisition reads ONE approval snapshot, carried into each
worker thread by :data:`_LAUNCH_APPROVAL_SNAPSHOT`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Optional

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.env import _SPEC_ENV_DENIED_PREFIXES
from kiro_crew.mcp_gateway import launch_approval
from kiro_crew.mcp_gateway.daemon import logger
from kiro_crew.mcp_gateway.daemon.control_plane import CONTROL_PLANE_BACKENDS
from kiro_crew.mcp_gateway.hashing import hash_command, hash_effective_env, non_secret_env
from kiro_crew.mcp_gateway.manager import _scrub_sensitive_env, is_credential_env_key
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.rewriter import env_sidecar_dir, env_sidecar_name, resolve_overlay_dir
from kiro_crew.sandbox import _PYTHON_ENV_PREFIXES

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway import gatewayd as facade
else:
    from kiro_crew.mcp_gateway.daemon import facade


# --- Type aliases -----------------------------------------------------------

#: A ``target_resolver`` takes a :class:`PoolKey` and returns the
#: ``(command, args, env, work_dir)`` tuple that spawns the backend, or
#: ``None`` if the server is unknown. The default resolver looks up
#: ``KIROCREW_MCP_TARGET_<SERVER>`` env vars (accepts legacy ``MC_MCP_TARGET_<SERVER>``
#: for backward compatibility; matches the Rust PoC and existing
#: rewriter wiring); tests inject their own resolver to avoid env-coupling.
TargetResolver = Callable[
    [PoolKey],
    Optional[tuple[str, list[str], dict[str, str], str]],
]

# One acquisition's immutable approval snapshot is propagated into each worker
# thread that resolves its command and declared env. Direct synchronous callers
# see ``None`` and retain the public helper's load-on-call behavior.
_LAUNCH_APPROVAL_SNAPSHOT: ContextVar[Optional[launch_approval.LaunchApprovals]] = ContextVar(
    "mcp_gateway_launch_approval_snapshot", default=None
)


class _TargetUnknown(RuntimeError):
    """Resolver returned no mapping — treated as a clean Register rejection
    rather than an internal error."""


def _declared_non_secret_env(pool_key: PoolKey) -> dict[str, str]:
    """Return the FORWARDABLE declared env for a SHARED ``pool_key``, or ``{}``.

    Reads the ``0600`` sidecar the rewriter wrote for this ``(agent, server)``
    and applies two independent filters:

    1. :func:`hashing.non_secret_env` — drops rotating-secret keys. Those are
       excluded from ``effective_env_hash``, so co-tenants of one backend can
       disagree on their values and no single value is correct to apply.
    2. :func:`manager.is_credential_env_key` — drops every key the daemon's own
       credential scrub removes (``AWS_ACCESS``, ``SSH_AUTH_SOCK``,
       ``GNUPGHOME``, ``GIT_ASKPASS``). This list is broader than (1), so
       forwarding never re-introduces a credential that ``_scrub_sensitive_env``
       deliberately stripped.

    What survives is operator-declared, non-secret, and part of the PoolKey —
    every session sharing this backend agrees on it by construction.

    A name in ``mcp_gateway.pool_identity_env`` survives (1) BECAUSE it is part
    of the PoolKey: :func:`rewriter.pool_identity_env_keys` is the authoritative
    read, the same one the coherence gate in :func:`_declared_env_pairs` uses, so
    the sentence above stays true rather than being weakened. It cannot bypass
    (2) — that helper drops credential-scrub names before returning them.

    BLOCKING: reads a file. Callers must run it off the event loop.
    """
    identity_keys = facade.pool_identity_env_keys()
    pairs = _declared_env_pairs(pool_key, identity_keys)
    return {
        k: v
        for k, v in non_secret_env(pairs, identity_keys=identity_keys).items()
        if not is_credential_env_key(k)
    }


def _read_declared_env_sidecar(pool_key: PoolKey) -> Optional[dict[str, str]]:
    """Return the raw declared env sidecar for ``pool_key``, or ``None``.

    ``None`` means no readable JSON object exists. The values are unfiltered and
    ungated; :func:`_declared_env_pairs` and :func:`env_target_resolver` apply
    their own checks.

    BLOCKING: reads a file. Callers must run it off the event loop.
    """
    try:
        overlay_dir = resolve_overlay_dir(KiroCrewConfig.load().mcp_gateway.overlay_dir)
    except Exception:
        logger.debug("declared-env: config unreadable; using default overlay dir", exc_info=True)
        overlay_dir = resolve_overlay_dir()
    path = env_sidecar_dir(overlay_dir) / env_sidecar_name(
        pool_key.agent_name, pool_key.server_name
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # No sidecar for this key: the server declared no env. Not an error.
        return None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("declared-env: sidecar %s is not valid JSON; ignoring", path)
        return None
    if not isinstance(decoded, dict):
        logger.warning("declared-env: sidecar %s is not a JSON object; ignoring", path)
        return None
    return {str(k): str(v) for k, v in decoded.items() if k}


def _declared_env_pairs(pool_key: PoolKey, identity_keys: Collection[str]) -> dict[str, str]:
    """Return the declared env sidecar's contents for ``pool_key``, or ``{}``.

    Unfiltered, but coherence-gated: a sidecar whose contents do not hash to
    ``pool_key.effective_env_hash`` yields ``{}``. Callers apply whatever
    co-tenancy filtering their acquisition path requires.

    ``identity_keys`` is REQUIRED rather than read here, so the caller's ONE
    snapshot of ``pool_identity_env_keys()`` governs both the hash recomputed
    below and whatever filtering the caller then applies. Reading it here as well
    would make those two decisions two different observations of a file an
    operator can edit at any moment: the gate could accept a sidecar under one
    set while the caller filtered under another, and the wider of the two would
    decide what reaches the backend. Passing it in makes that mismatch
    unrepresentable instead of merely unlikely.

    BLOCKING: reads a file. Callers must run it off the event loop.
    """
    pairs = _read_declared_env_sidecar(pool_key)
    if pairs is None:
        return {}
    # COHERENCE GATE — the invariant that makes forwarding safe must be
    # ENFORCED, not assumed. The stub hashed the sidecar as it read it at ITS
    # start; this read happens later, at cold spawn. An operator editing
    # ``mcpServers.<name>.env`` makes ``rewrite_agents`` rewrite the sidecar
    # while already-running stubs keep their old PoolKey (an adopted daemon can
    # hold such a stub across a gateway restart). A crash/idle-reap respawn
    # would then apply the NEW values to a backend keyed by the OLD hash — so
    # co-tenants would run under configuration they never declared, exactly what
    # the PoolKey partition exists to prevent.
    #
    # Recomputing the hash here and requiring equality closes that window. The
    # construction mirrors the stub's ``_parse_env_json`` (str-coerced keys and
    # values, empty keys dropped) so a coherent sidecar always matches.
    #
    # ``identity_keys`` comes from the OPERATOR's config, never from the Register
    # frame, and is the CALLER's single snapshot -- see this function's docstring
    # for why it is a parameter rather than a second read. The stub was handed the
    # same set on its argv only so it could compute this hash; a stub that claims a
    # different set produces a hash this line does not reproduce, so the mismatch
    # branch runs and nothing is forwarded. That is what keeps "which secrets may
    # reach a shared backend" an operator decision while leaving the stub the
    # untrusted client it is documented to be — and it needs no new check, because
    # the gate that already guards a spec edited mid-session guards a lying stub
    # identically.
    if hash_effective_env(pairs, identity_keys=identity_keys) != (pool_key.effective_env_hash):
        logger.warning(
            "declared-env: sidecar for %r no longer matches the PoolKey it was "
            "hashed under (the spec was edited after this session started); "
            "skipping forwarding for this backend",
            pool_key.server_name,
        )
        return {}
    # APPROVAL GATE. The coherence gate above compares the sidecar with a hash
    # the STUB reported, and both are agent-writable, so together they prove
    # only that two agent-controlled inputs agree. The env forwarded here runs
    # outside the sandbox, so it must be one the gateway derived from an
    # operator-approved launch (``launch_approval``). Fails closed.
    if not _launch_approved_from_snapshot(
        pool_key.server_name,
        pool_key.command_args_hash,
        launch_approval.env_fingerprint(pairs),
    ):
        logger.warning(
            "declared-env: sidecar for %r does not match an approved launch; "
            "skipping forwarding for this backend",
            pool_key.server_name,
        )
        return {}
    return pairs


def _declared_env_for_private_backend(pool_key: PoolKey) -> dict[str, str]:
    """Return the declared env for a CONNECTION-PRIVATE backend, or ``{}``.

    A private backend has exactly one stub, so both filters that
    :func:`_declared_non_secret_env` applies are inapplicable by construction:
    there is no co-tenant that could disagree on a rotating secret's value, and
    the credential scrub exists to stop one session's credentials reaching
    another session's backend. Here the declaring session and the only consuming
    session are the same one.

    Nor is this gated on ``forward_declared_env``: post-flip that switch is an
    escape hatch for disabling forwarding fleet-wide, not a gate on accepting a
    co-tenancy hazard — the hazard it once gated is closed by construction for
    pooled backends (only keys inside ``effective_env_hash`` are forwarded, and
    the coherence gate re-checks the sidecar against that hash at spawn). Note
    the per-server opt-out is membership in ``mcp_gateway.stub_servers``, which is
    the only stub trigger; this flag is the coarser fleet-wide spelling.
    Withholding the env here would instead be a regression — the same server
    spawned without a gateway gets its declared env from the agent runtime, so a
    private backend that silently dropped it would break servers that work today.

    The coherence gate still applies: a sidecar edited after this session
    started yields ``{}`` rather than values the running stub never hashed.

    BLOCKING: never call this on the event loop.
    """
    return _declared_env_pairs(pool_key, facade.pool_identity_env_keys())


def _declared_env_to_forward(pool_key: PoolKey) -> dict[str, str]:
    """Return the declared env to apply to a cold-spawned backend, or ``{}``.

    Combines the opt-in flag check with the sidecar read so the whole thing is
    ONE blocking unit the caller can hand to a single ``asyncio.to_thread`` —
    both halves read config / touch the filesystem and must stay off the event
    loop. Fails closed: flag off, unreadable config, or unreadable sidecar all
    yield ``{}``.

    BLOCKING: never call this on the event loop.
    """
    if not facade.forward_declared_env_enabled():
        return {}
    return facade._declared_non_secret_env(pool_key)


def resolvable_target_stems(env: Optional[dict[str, str]] = None) -> list[str]:
    """The set of target-env STEMS this daemon can resolve, sorted.

    A stem is the env key with its prefix and any ``__<command_args_hash>``
    suffix removed -- e.g. both ``KIROCREW_MCP_TARGET_KIROCREW_CORE`` and
    ``KIROCREW_MCP_TARGET_KIROCREW_CORE__61774e20...`` yield ``KIROCREW_CORE``.

    Reported on the ``pong`` reply so an adopting :class:`GatewayManager` can
    tell whether an incumbent daemon's env still covers the servers the current
    config wants stubbed. This is the ONLY way to see that: the daemon's target
    map is baked into its process env at spawn (``manager._spawn_once``) and a
    frozen :class:`GatewaySpec` is never re-applied to an adopted survivor, so a
    daemon that predates a ``stub_servers`` change serves a stale map forever.

    Deliberately reports STEMS rather than server names. Recovering a name would
    mean undoing ``upper().replace("-", "_")``, which is lossy -- ``my-server``
    and ``my_server`` normalize identically (the rewriter warns about exactly
    that collision). Both sides comparing stems needs no such guess.
    """
    source = os.environ if env is None else env
    # ``target_env_stem`` strips the prefix (canonical or the legacy
    # ``MC_MCP_TARGET_``) and the args-disambiguated suffix, so a hashed-only
    # entry still reports the server it serves.
    stems = {stem for key in source if (stem := launch_approval.target_env_stem(key))}
    return sorted(stems)


def _approval_env_identity(pool_key: PoolKey) -> str:
    """The full-env approval identity of the launch behind ``pool_key``.

    A readable sidecar coherent with ``pool_key.effective_env_hash`` yields the
    complete declared env's fingerprint, the identity the approval stores. In
    every other case the PoolKey hash stands, which matches an approval only
    when that approval's env carried no secret-prefixed key.

    BLOCKING: reads a file. Callers must run it off the event loop.
    """
    pairs = _read_declared_env_sidecar(pool_key)
    if pairs is not None and (
        hash_effective_env(pairs, identity_keys=facade.pool_identity_env_keys())
        == pool_key.effective_env_hash
    ):
        return launch_approval.env_fingerprint(pairs)
    return pool_key.effective_env_hash


def _launch_approved_from_snapshot(
    server_name: str,
    command_hash: str,
    derived_env_hash: str,
) -> bool:
    """Check the acquisition snapshot, loading only for synchronous callers."""
    approvals = _LAUNCH_APPROVAL_SNAPSHOT.get()
    if approvals is None:
        return launch_approval.launch_approved(server_name, command_hash, derived_env_hash)
    return approvals.admits_launch(
        launch_approval.target_stem(server_name), command_hash, derived_env_hash
    )


def env_target_resolver(pool_key: PoolKey) -> Optional[tuple[str, list[str], dict[str, str], str]]:
    """Look up ``KIROCREW_MCP_TARGET_<SERVER>`` in the process env and return the
    spawn tuple, or ``None`` if no mapping is set.

    Wire format: ``KIROCREW_MCP_TARGET_SLACK_MCP="slack-mcp --stdio"``.
    The server name is upper-cased with ``-`` replaced by ``_``. Env is
    inherited from the gateway process with ``KIROCREW_CHANNEL_ID``
    overlaid when the pool key carries one — this keeps cron / send_message
    fallbacks pointed at the correct channel on a per-pool-key basis.

    Defense-in-depth: env is scrubbed through
    :func:`kiro_crew.mcp_gateway.manager._scrub_sensitive_env` so even if
    the gateway process somehow inherited credential vars, backends won't.
    """
    base = "KIROCREW_MCP_TARGET_" + pool_key.server_name.upper().replace("-", "_")
    # Accept the legacy MC_MCP_TARGET_ prefix for overlays/daemons written by
    # older versions that haven't been regenerated.
    legacy_base = "MC_MCP_TARGET_" + pool_key.server_name.upper().replace("-", "_")
    # Prefer the args-disambiguated entry (written by
    # rewriter._collect_target_env) so two agents that share a server name but
    # declare different --target-args each spawn their OWN backend command,
    # instead of resolving to whichever agent sorted first alphabetically. Fall
    # back to the bare server-name entry for older overlays predating the
    # disambiguated keys.
    spec = (
        os.environ.get(base + "__" + pool_key.command_args_hash)
        or os.environ.get(base)
        or os.environ.get(legacy_base + "__" + pool_key.command_args_hash)
        or os.environ.get(legacy_base)
    )
    if not spec:
        return None
    parts = shlex.split(spec)
    if not parts:
        return None
    command, *args = parts
    # The mapping above is spec-derived; the server name that selected it is
    # not proof of what it runs. Spawn only a command+args the operator
    # approved for this name (``launch_approval``). ``None`` is the clean
    # "no target" rejection, so the stub falls back to a launch inside the
    # session sandbox. Fails closed on a missing or unreadable store.
    if not _launch_approved_from_snapshot(
        pool_key.server_name,
        hash_command(command, args),
        _approval_env_identity(pool_key),
    ):
        logger.warning(
            "mcp-gateway: refusing to spawn %r: its launch is not an approved one",
            pool_key.server_name,
        )
        return None
    env = _scrub_sensitive_env(dict(os.environ))
    # A reserved control plane receives the session token only after every
    # launcher-injection namespace is gone. Third-party backends never receive
    # that token and keep settings outside the four Python interpreter roots
    # that can make them load Kiro Crew's packages instead of their own.
    denied_env_prefixes = (
        _SPEC_ENV_DENIED_PREFIXES
        if pool_key.server_name in CONTROL_PLANE_BACKENDS
        else tuple(_PYTHON_ENV_PREFIXES)
    )
    for key in tuple(env):
        if any(key.upper().startswith(prefix) for prefix in denied_env_prefixes):
            env.pop(key, None)
    # No KIROCREW_CHANNEL_ID is exported into the backend env. Copying it from
    # PoolKey.channel_id would only make sense while a backend was owned by one
    # channel. A pooled backend serves several channels, so a
    # single channel baked into its environment at spawn would be actively
    # wrong — it would tell the server it belongs to whichever channel happened
    # to spawn it first. The channel is delivered PER CALL instead, in
    # _meta.kirocrew.caller (see _inject_caller_meta).
    return command, args, env, pool_key.work_dir


def _resolve_once_home() -> str:
    """The data home whose resolve-once store this daemon reads.

    Mirrors the socket-path resolution so the daemon and the gateway that filled
    the store agree on where it lives.
    """
    home = os.environ.get("KIROCREW_HOME")
    return str(Path(home) if home else facade._config_dir())


def resolve_once_resolver(inner: TargetResolver) -> TargetResolver:
    """Wrap ``inner`` so an already-resolved npm spec launches without npm.

    An ``npx`` target re-asks the registry what its spec means on every launch.
    When the gateway has pre-resolved that spec into its store, this substitutes
    the recorded entry point, turning the launch into a plain ``node`` exec with
    no network and no dependency resolution.

    Purely a substitution: env and work_dir are whatever ``inner`` computed, so
    the PoolKey's env hash still describes what is spawned. Anything not
    pre-resolved -- a non-npm command, a spec never prefetched, a store entry
    that has gone stale on disk -- passes through untouched, so this can only
    remove work from the launch path, never add a failure to it.
    """

    def _resolver(pool_key: PoolKey) -> Optional[tuple[str, list[str], dict[str, str], str]]:
        target = inner(pool_key)
        if target is None:
            return None
        command, args, env, work_dir = target
        try:
            launch = facade.resolved_launch(facade._resolve_once_home(), command, args)
        except Exception:  # pragma: no cover — a store read must never break spawn
            logger.debug("resolve-once lookup failed; using npm launcher", exc_info=True)
            return target
        if launch is None:
            return target
        resolved_command, resolved_args = launch
        logger.info(
            "resolve-once: %s launching pre-resolved tree instead of %s",
            pool_key.server_name,
            os.path.basename(command),
        )
        return resolved_command, resolved_args, env, work_dir

    return _resolver


def _call_with_approval_snapshot(
    callback: Callable[[PoolKey], Any],
    pool_key: PoolKey,
    approvals: launch_approval.LaunchApprovals,
) -> Any:
    """Run one resolver callback against an immutable approval snapshot."""
    token = _LAUNCH_APPROVAL_SNAPSHOT.set(approvals)
    try:
        return callback(pool_key)
    finally:
        _LAUNCH_APPROVAL_SNAPSHOT.reset(token)


async def _resolve_target_off_loop(
    resolver: TargetResolver,
    pool_key: PoolKey,
    approvals: Optional[launch_approval.LaunchApprovals] = None,
) -> Optional[tuple[str, list[str], dict[str, str], str]]:
    """Resolve one backend target without blocking the gateway event loop."""
    snapshot = approvals
    if snapshot is None:
        snapshot = await asyncio.to_thread(launch_approval.load_approvals)
    return await asyncio.to_thread(_call_with_approval_snapshot, resolver, pool_key, snapshot)
