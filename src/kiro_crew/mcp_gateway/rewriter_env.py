"""The env a routed server is launched with, as the rewriter decides it.

Owner module of :mod:`kiro_crew.mcp_gateway.rewriter`, which re-exports every name
here; read and patch them there. This module owns:

* normalising a declared env and counting what a shared backend would withhold
  (:func:`_normalized_env`, :func:`_withheld_env_count`), and the two vault-secret
  rules that follow from it (:func:`_needs_private_backend`,
  :func:`_must_stay_wrapped`);
* ``${VAR}`` / ``${env:VAR}`` placeholder expansion against the filtered process
  env (:func:`_expand_env_placeholders`, :func:`_expand_env_map`);
* the env sidecars a pass stages and commits (:class:`_SidecarLedger`), and the
  target env the gateway is handed, with its once-per-process collision warning
  (:func:`_collect_target_env`);
* the two config reads that shape pooled env: :func:`forward_declared_env_enabled`
  and :func:`pool_identity_env_keys`.

It logs under the rewriter's logger and never imports the facade.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Mapping

from kiro_crew.mcp_gateway.hashing import hash_command, is_secret_env_key
from kiro_crew.mcp_gateway.manager import is_credential_env_key
from kiro_crew.mcp_gateway.rewriter_launch import (
    _WRAPPER_MARKER,
    _WRAPPER_MARKER_LEGACY,
    _wrapped_target,
)
from kiro_crew.mcp_gateway.secret_uri import secret_reference_keys
from kiro_crew.sandbox import scrub_agent_denied_env

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.rewriter_launch import _RewritePassNotes

logger = logging.getLogger("kiro_crew.mcp_gateway.rewriter")


# Normalized ``KIROCREW_MCP_TARGET_<SERVER>`` keys already warned about as a
# base-name collision, so the notice fires once per distinct colliding key per
# process instead of once per rewrite pass. ``_collect_target_env`` runs on
# every pass (one call per agent spec, plus one per kept overlay), so a
# genuinely single ambiguous config would otherwise print the same WARNING line
# on every boot pass -- a wall of identical text an operator cannot count
# distinct problems from. Guarded by ``_collision_warn_lock`` so two threads
# rewriting at once cannot both report the same key. Repeats drop to DEBUG.
_collision_warn_lock = threading.Lock()
_collision_warned_keys: set[str] = set()


def _reset_collision_warnings() -> None:
    """Clear the per-process env-key collision warning latch.

    A test hook: the module-level ``_collision_warned_keys`` set persists for
    the life of the process, so a test that asserts on the WARNING record must
    reset it between cases to see the first-occurrence notice again.
    """
    with _collision_warn_lock:
        _collision_warned_keys.clear()


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
            context,
            type(declared).__name__,
        )
    return {}


def _needs_private_backend(withheld: int, entry_env: Mapping[str, Any]) -> bool:
    """True when a routed server must get a connection-private backend to run at all.

    A server whose env would be partly withheld from a SHARED backend is normally
    left unwrapped, so the session launches it with its declared env. That cannot
    work when the env names a vault secret: the session cannot read the vault, so
    the server would start with the literal ``secret://`` text as its credential.
    A private backend receives the whole declared env
    (``daemon/launch._declared_env_for_private_backend``) and is spawned by the
    gateway, which is the one place the reference is resolved.
    """
    return bool(withheld) and bool(secret_reference_keys(entry_env))


def _must_stay_wrapped(entry_env: Mapping[str, Any]) -> bool:
    """True when a routed server must be wrapped even if it cannot be served yet.

    Only a stub keeps a session from launching the agent spec's own entry, and
    that entry starts the server with the literal ``secret://`` text. So a routed
    server whose env names a vault secret is wrapped even when its command does
    not resolve on the gateway search path or its launch is not approved: the
    gateway then refuses it (no approved target, or a failed spawn whose stub
    fallback refuses the reference), and the server does not start.
    """
    return bool(secret_reference_keys(entry_env))


def _withheld_env_count(
    entry_env: dict[str, Any],
    forward_env: bool,
    identity_keys: Collection[str] = (),
) -> int:
    """How many declared env keys a shared pooled backend would NOT receive.

    The pooling bargain is "the backend starts with your declared env"; any
    withheld key can be the one the server dies without, so a non-zero count
    disqualifies the entry from pooling. With forwarding
    off every key is withheld. With forwarding on, gatewayd's forwarder still
    drops rotating-secret keys (excluded from the PoolKey, so co-tenants can
    disagree on their values) and the daemon's own credential-scrub set —
    mirroring ``gatewayd._declared_non_secret_env`` exactly, so this
    classifier never promises an env the forwarder will refuse to apply.

    ``identity_keys`` is :func:`pool_identity_env_keys`, and a named key stops
    being withheld here for the same reason the forwarder starts applying it: it
    is now inside ``effective_env_hash``. The two sides consult ONE resolved set
    so they cannot disagree, and because that helper already drops
    credential-scrub names, a name can never be un-withheld here while the
    forwarder still refuses it.

    That mirror is load-bearing BECAUSE of the default flip: with forwarding off
    this function short-circuits on ``len(entry_env)`` and the forwarder is never
    consulted, so the two could not disagree. With forwarding on they must agree
    key for key, which ``test_the_eligibility_count_matches_the_forwarder``
    pins by construction.
    """
    if not forward_env:
        return len(entry_env)
    identity = frozenset(identity_keys)
    return sum(
        1
        for k in entry_env
        if (is_secret_env_key(k) and k not in identity) or is_credential_env_key(k)
    )


# Expand ${VAR}/${env:VAR} in a brokered server's declared env, matching
# kiro-cli's expander (crates/agent/src/agent/util/mod.rs). Needed because the
# broker spawns the stub, not the real server, so kiro-cli never expands the
# declared env; gatewayd/the stub spawn the backend from the sidecar that
# ``rewriter_spec._build_stub_entry`` writes. Resolving once at write time keeps
# that sidecar the single hash source both the stub's effective_env_hash and
# gatewayd's coherence re-hash read, so the PoolKey gate holds.
_ENV_VAR_PLACEHOLDER = re.compile(r"\$\{(?:env:)?([^}]+)\}")


def _placeholder_source_env() -> dict[str, str]:
    """The environment view a placeholder may dereference.

    The rewrite pass runs in the gateway parent process, whose ``os.environ``
    holds the channel tokens ``load_credentials()`` seeds plus the operator's
    raw shell env — and agent specs are agent-writable, so an unfiltered lookup
    lets ``{"TOKEN": "${env:AWS_SECRET_ACCESS_KEY}"}`` smuggle a credential
    VALUE past the key-name forwarding filters into a pooled backend.

    Dropping :func:`is_secret_env_key` + :func:`is_credential_env_key` names
    mirrors the declared-KEY double filter (``gatewayd._declared_non_secret_env``),
    so a value the forwarder would refuse under its own name cannot ride in
    under another. Dropping :func:`scrub_agent_denied_env` keys matches what
    kiro-cli's own expander sees: the ACP spawn scrubs those before kiro-cli
    starts, so they are misses there and must be misses here too.
    """
    return scrub_agent_denied_env(
        {
            k: v
            for k, v in os.environ.items()
            if not (is_secret_env_key(k) or is_credential_env_key(k))
        }
    )


def _expand_env_placeholders(
    value: str,
    *,
    notes: _RewritePassNotes | None = None,
    source: Mapping[str, str] | None = None,
    server: str | None = None,
) -> str:
    """Resolve ``${VAR}`` / ``${env:VAR}`` from *source* (default: the filtered
    :func:`_placeholder_source_env` view), leaving an unresolved reference as a
    literal ``${VAR}`` (kiro-cli parity, including dropping the ``env:`` prefix
    on a miss). A reference to a credential-filtered name is the same miss,
    logged so the operator can tell a refusal from a typo.

    Encountering any reference marks the pass uncacheable via *notes* (see
    ``_RewritePassNotes.env_placeholder_seen``) — the environment is not a
    fingerprinted input, so a resolved value must never be served from cache.
    """
    env_view = _placeholder_source_env() if source is None else source

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        if notes is not None:
            notes.env_placeholder_seen = True
        resolved = env_view.get(name)
        if resolved is None:
            if name in os.environ:
                logger.warning(
                    "declared env placeholder ${%s} names a credential-filtered "
                    "variable; left as a literal",
                    name,
                )
            else:
                logger.warning(
                    "declared env placeholder %r for MCP server %r is unset; " "left as a literal",
                    name,
                    server,
                )
            return f"${{{name}}}"
        return resolved

    return _ENV_VAR_PLACEHOLDER.sub(_sub, value)


def _expand_env_map(
    env_pairs: dict[str, Any],
    *,
    notes: _RewritePassNotes | None = None,
    server: str | None = None,
) -> dict[str, Any]:
    """Expand placeholders in string values only; non-str values pass through
    (both readers ``str()``-coerce them identically, keeping the PoolKey hash
    coherent). The source view is built once for the whole map."""
    source = _placeholder_source_env()
    return {
        k: (
            _expand_env_placeholders(v, notes=notes, source=source, server=server)
            if isinstance(v, str)
            else v
        )
        for k, v in env_pairs.items()
    }


class _SidecarLedger:
    """Sidecar names this pass enumerated, plus writes staged for commit.

    ``names`` feeds the sidecar prune and the fingerprint. A staged write is
    renamed into place only after the overlay that references it is published,
    so a kept overlay never pairs with the next generation's env; the caller
    commits or discards per agent, so nothing is pending afterwards.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()
        self._staged: list[tuple[str, Path]] = []

    def add(self, name: str) -> None:
        """Record that *name* is a sidecar this pass owns."""
        self.names.add(name)

    def stage(self, tmp: str, final: Path) -> None:
        """Hand over a fully-written, already-protected temp file for commit."""
        self._staged.append((tmp, final))

    def commit(self) -> bool:
        """Publish every staged sidecar; False if any rename failed.

        A failed rename also removes whatever sits at the published name, so the
        overlay already naming it reads as "declares no env" (both readers
        degrade to ``{}``) instead of picking up another generation's values.
        """
        ok = True
        while self._staged:
            tmp, final = self._staged.pop()
            try:
                os.replace(tmp, final)
            except OSError as exc:
                ok = False
                logger.warning("rewriter: failed to commit env sidecar %s: %s", final, exc)
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                with contextlib.suppress(OSError):
                    os.unlink(final)
        return ok

    def discard(self) -> None:
        """Drop every staged sidecar, leaving the published names untouched."""
        while self._staged:
            tmp, _final = self._staged.pop()
            with contextlib.suppress(OSError):
                os.unlink(tmp)


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
        target = _wrapped_target(entry)
        if target is not None:
            target_cmd, raw_target_args = target
            # This is a matched quote/split codec, never a shell command.
            spec = " ".join(shlex.quote(p) for p in [target_cmd, *raw_target_args])
            # Bare server-name key: first-wins fallback. Two DISTINCT server
            # names can normalize to the same key ("my-server" vs "my_server",
            # case variants). The args-hashed key below is authoritative at
            # resolve time, but warn on a base collision so a genuinely
            # ambiguous config is visible rather than silently first-wins.
            existing = target_env.get(env_key)
            if existing is not None and existing != spec:
                with _collision_warn_lock:
                    first = env_key not in _collision_warned_keys
                    if first:
                        _collision_warned_keys.add(env_key)
                if first:
                    logger.warning(
                        "mcp-gateway rewriter: KIROCREW_MCP_TARGET env-key collision on "
                        "%s (distinct server names normalize identically); the "
                        "args-hashed key is used at resolve time, base stays "
                        "first-wins",
                        env_key,
                    )
                else:
                    logger.debug(
                        "mcp-gateway rewriter: KIROCREW_MCP_TARGET env-key collision on "
                        "%s again (already reported this pass or a prior one)",
                        env_key,
                    )
            target_env.setdefault(env_key, spec)
            # Args-disambiguated key: idempotent per (server, command+args), so
            # divergent same-named servers do not collide on first-wins.
            hashed_key = env_key + "__" + hash_command(target_cmd, raw_target_args)
            target_env[hashed_key] = spec


def forward_declared_env_enabled() -> bool:
    """Return ``mcp_gateway.forward_declared_env`` (default ``True``).

    Function-local config import: ``config.loader`` imports the rewriter, and with
    it this module, at its own module top level, so a top-level import here would
    be circular. Mirrors
    ``backend._mcp_apps_enabled``. Fails CLOSED — an unreadable config means the
    declared env is not forwarded, which also leaves the server unwrapped rather
    than pooling it without the env it declares.
    """
    try:
        # circular import: config.loader imports the rewriter, and with it this
        # module, at its own top level (for default_overlay_dir /
        # default_socket_path), so a module-scope import here would be a cycle.
        # Mirrors backend._mcp_apps_enabled.
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(KiroCrewConfig.load().mcp_gateway.forward_declared_env)
    except Exception:
        logger.debug("rewriter: config unreadable; declared-env forwarding off", exc_info=True)
        return False


def pool_identity_env_keys() -> frozenset[str]:
    """Return ``mcp_gateway.pool_identity_env`` as an effective key set.

    The AUTHORITATIVE source for which env variables an operator has declared
    pool-identity-relevant. Every consumer that must agree on this set reads it
    HERE: the rewriter (to hash and to count withheld keys) and ``gatewayd`` (to
    re-hash the sidecar and to decide what to forward). The stub is handed the
    resolved set on its command line instead of reading config itself, and its
    copy carries no authority — ``hash_effective_env`` explains how the coherence
    gate turns a disagreeing stub into a refusal to forward.

    Names matched by :func:`manager.is_credential_env_key` are DROPPED. That
    scrub is a separate and broader guard — it keeps one session's credentials
    out of another session's backend in the per-session topology too — and this
    setting is not a way to lift it. Filtering here rather than at each consumer
    is what stops a half-state where a name is folded into the hash but still
    refused by the forwarder, which would leave the entry unpoolable anyway while
    silently re-partitioning it on every rotation.

    Fails CLOSED to the empty set: an unreadable config means nothing is opted
    in, which is exactly today's behaviour.
    """
    try:
        # circular import: config.loader imports the rewriter, and with it this
        # module, at its own top level (for default_overlay_dir /
        # default_socket_path), so a module-scope import here would be a cycle.
        # Mirrors forward_declared_env_enabled.
        from kiro_crew.config.loader import KiroCrewConfig

        declared = KiroCrewConfig.load().mcp_gateway.pool_identity_env or []
    except Exception:
        logger.debug("rewriter: config unreadable; no pool-identity env keys", exc_info=True)
        return frozenset()
    kept: set[str] = set()
    for name in declared:
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if is_credential_env_key(name):
            logger.warning(
                "mcp_gateway.pool_identity_env names %r, which the daemon's own "
                "credential scrub removes; ignoring it (that scrub is not lifted "
                "by this setting)",
                name,
            )
            continue
        kept.add(name)
    return frozenset(kept)
