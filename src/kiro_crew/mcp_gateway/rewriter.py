"""Rewrite kiro agent specs so MCP servers route through the broker.

The rewriter reads ``~/.kiro/agents/*.json`` and ``*.md`` (the markdown form,
see :mod:`kiro_crew.agent_spec_format`) and writes modified JSON copies into
the overlay directory (``<config_dir>/mcp-gateway/agents/``). The overlay is
always ``<stem>.json`` whatever the source's form: ``session_servers.py`` looks
an agent's overlay up by ``<agent>.json``, and a markdown spec's servers must
be stubbed exactly like a JSON spec's or they would spawn direct, outside the
tool gate. The host
filesystem remains untouched — the broker stubs in these specs are injected
into each kiro-cli session over ACP ``session/new``, which outranks the
same-named entry in the agent spec (see ``session_servers.py``).

Servers in :data:`UNPOOLABLE_SERVERS` are left unwrapped. The set lists only
the credential vendors; the first-party servers that bind
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
import importlib as _importlib
import json
import logging
import os
import shutil
import sys
import sys as _sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection

from kiro_crew import __version__, _facade_mirror, platform_compat
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.atomic_write import atomic_write
from kiro_crew.env import mcp_search_path, resolved_command_casing
from kiro_crew.mcp_gateway.launch_approval import LaunchApprovals, filter_target_env
from kiro_crew.mcp_gateway.read_limits import config_read_buffer_limit
from kiro_crew.mcp_gateway.rewriter_env import (
    _collect_target_env,
    _SidecarLedger,
    forward_declared_env_enabled,
    pool_identity_env_keys,
)
from kiro_crew.mcp_gateway.rewriter_launch import (
    _WHICH_KEY_SEP,
    _WRAPPER_MARKER,
    _WRAPPER_MARKER_LEGACY,
    _cmd_safe_command,
    _managed_control_plane_signature,
    _RewritePassNotes,
)
from kiro_crew.mcp_gateway.rewriter_overlay import (
    _kept_artifacts_vouched,
    _kept_overlay_vouched,
    _overlay_inputs_unchanged,
    _source_sig,
    _stat_sig,
    env_sidecar_dir_for_stubs,
)
from kiro_crew.mcp_gateway.rewriter_spec import _injectable_settings_servers, _rewrite_single_spec
from kiro_crew.user_json import loads_user_json

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
# 5: the stub's flags ride inside one STUB_FLAGS_FLAG envelope, so a kept
# plain-flag overlay would keep exposing its raw metadata to cmd.exe.
# Deliberately NOT bumped for the retained legacy settings overlay: the
# per-agent overlay bytes do not depend on it, the leftover overlay file is
# retained and ignored (never consumed), and a stored ``settings_overlay``
# output signature is not rejected — it keeps vouching for the leftover's ACL
# relock — so an older fingerprint still validates correctly, and bumping would
# gratuitously defeat the transient-keep gate (which compares stored vs current
# inputs) on the first upgraded boot.
# 6: target commands and recorded probes carry their on-disk Windows casing.
# 7: the wrapped entry's command is normalised to a cmd.exe-safe spelling on
# Windows (8.3 short form when the interpreter path carries a metacharacter),
# and kept overlays must fingerprint that derived spelling to avoid launching
# through cmd.exe with a quote-stripped interpreter path.
# 8: a registry-governed or muted entry passes through unwrapped instead of
# becoming a broker stub. The inputs are unchanged for such an entry, so a kept
# overlay keeps a stub whose name ``session_servers.injection_server_names``
# still collects -- launching, at session level, the very server the marker
# hands to the administrator's catalog and the mute silences. The output shape
# for identical inputs is what changed, which is exactly what this knob rejects.
# 9: a reserved kirocrew-* entry launches the managed invocation, not the
# spec's command (``_repair_control_plane_entry``). That invocation is an input
# the spec files cannot see -- it moves on every upgrade and on a relocated
# install -- so it is fingerprinted as ``managed_control_plane`` below, and the
# bump regenerates every kept overlay whose stub still hashes the spec's command.
# 10: an agent whose spec sets ``includeMcpJson: false`` gets no injected
# settings stubs. Its inputs are unchanged, so a kept overlay would keep
# mounting the excluded servers at ``session/new``.
# 11: credential vendors joined ``UNPOOLABLE_SERVERS``. The denylist is not a
# fingerprint input, so a kept overlay from before would keep the vendor's stub
# and its backend would keep running outside every chat sandbox.
_FINGERPRINT_SCHEMA = 11

# The fingerprint inputs of the latest pass into each overlay directory, kept so
# a caller can read the settings a pass was built from without a second scan.
_PASS_INPUTS: dict[str, dict[str, Any]] = {}
_PASS_INPUTS_LOCK = threading.Lock()
# Bounded: scratch passes nobody reads back (launch resolution) must not pile up.
_PASS_INPUTS_MAX = 16


def take_pass_inputs(overlay_dir: Path) -> dict[str, Any] | None:
    """Pop the fingerprint inputs the latest pass into *overlay_dir* computed."""
    with _PASS_INPUTS_LOCK:
        return _PASS_INPUTS.pop(str(overlay_dir), None)


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
    identity_keys: Collection[str],
    read_buffer_limit: int,
) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of every input that can change
    :func:`rewrite_agents`'s output.

    Enumerated against the code, not guessed:

    * ``sources`` / ``settings`` — the parsed spec files (size+mtime+digest).
    * ``socket_path`` / ``work_dir`` — baked into stub argv and the PoolKey.
    * ``sandbox_mode`` / ``approval_mode`` / ``stub_servers`` /
      ``pooling_enabled`` — decide stub flags and which entries are shareable.
    * ``python`` records ``sys.executable``, which is baked into every overlay
      ``command``. ``python_isolation_flags`` records the effective option prefix
      the stub argv carries (``-P`` plus the optional ``-s`` derived by
      :func:`platform_compat.isolated_python_argv`), so an interpreter move, a
      user-site policy change, or a change to the stub's own interpreter
      options regenerates the overlays.
    * ``python_cmd_safe`` — the cmd.exe-safe spelling of ``sys.executable``
      actually written as the overlay ``command`` on Windows. A volume's 8.3
      name generation being toggled (or the alias stripped with ``fsutil
      8dot3name strip``) changes this without touching ``python`` or any
      other input, and a kept overlay would launch an interpreter path that
      does not resolve — so the derived spelling is fingerprinted alongside
      its source.
    * ``path_env`` / ``pathext`` / ``path_augment`` — feed the
      ``shutil.which`` resolution of bare command names (``path_augment`` is
      :func:`kiro_crew.env.mcp_search_path` over an empty spec PATH — the
      augmentation-and-dedup half of the search, which depends on ambient
      state like ``MISE_DATA_DIR`` and on ``mcp.extra_path_dirs`` that
      ``path_env`` cannot see, so editing that setting invalidates the
      cache instead of reusing a stale resolution). The
      other half of which()'s input — the CONTENTS of the searched
      directories — is not stat-able here; it is covered by the stored
      per-probe results, which the cache-hit path re-runs and compares (see
      :class:`_RewritePassNotes`).
    * ``forward_declared_env`` — decides whether an env-declaring server is
      pooled at all (the withheld-env pre-classification), so flipping the
      config flag must regenerate the overlays.
    * ``pool_identity_env`` — decides which secret-prefixed keys are hashed into
      the PoolKey and passed on stub argv, so editing the list must regenerate
      the overlays. Without this, naming a key would take effect only once some
      unrelated input changed, and until then the stub would keep hashing the old
      set while gatewayd hashed the new one — the coherence gate would refuse to
      forward, so the feature would silently not work.
    * ``read_buffer_limit`` — the per-stream read ceiling stamped into every
      stub's ``--read-limit``, for the same reason as ``pool_identity_env``:
      a kept overlay would keep launching stubs with the old ceiling.
    * ``schema`` / ``package`` — invalidate on rewriter logic changes.
    """
    sources: dict[str, list[Any] | None] = {
        p.name: _source_sig(p) for p in iter_agent_spec_files(source_dir)
    }
    return {
        "schema": _FINGERPRINT_SCHEMA,
        "package": __version__,
        "python": sys.executable,
        # The stub argv's option prefix, ``-P`` included (see _build_stub_entry).
        "python_isolation_flags": platform_compat.isolated_python_argv("-P")[1:],
        "python_cmd_safe": _cmd_safe_command(sys.executable),
        "path_env": os.environ.get("PATH", ""),
        "pathext": os.environ.get("PATHEXT", ""),
        "path_augment": mcp_search_path(""),
        "forward_declared_env": bool(forward_env),
        "pool_identity_env": sorted(frozenset(identity_keys)),
        # Written onto every stub's argv as ``--read-limit``, so raising
        # ``mcp_gateway.read_buffer_limit_bytes`` has to regenerate the overlays.
        # Without it a kept overlay keeps handing stubs the previous ceiling and
        # the new setting silently does nothing until some unrelated input
        # changes -- the same failure mode ``pool_identity_env`` above records.
        "read_buffer_limit": int(read_buffer_limit),
        "source_dir": str(source_dir),
        "overlay_dir": str(overlay_dir),
        "socket_path": str(socket_path),
        "work_dir": str(work_dir),
        "sandbox_mode": sandbox_mode,
        "approval_mode": approval_mode,
        "stub_servers": sorted(stub_set),
        "pooling_enabled": bool(pooling_enabled),
        # The launch a stubbed reserved name is rewritten TO (schema 9). A
        # kirocrew upgrade moves it while every spec file stays byte-identical;
        # ``package`` catches the version bump, this catches a same-version
        # relocation (a reinstall to another prefix, a payload moved) as well.
        "managed_control_plane": _managed_control_plane_signature(stub_set),
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
    overlay tree is removed exactly as on the full path.
    """
    outputs = stored["outputs"]
    overlay_sigs: dict[str, Any] = outputs["overlays"]
    sidecar_sigs: dict[str, Any] = outputs["sidecars"]
    env_dir = env_sidecar_dir_for_stubs(stubs_dir)
    try:
        for name, sig in overlay_sigs.items():
            if _stat_sig(overlay_dir / name) != sig:
                return None
        for name, sig in sidecar_sigs.items():
            if _stat_sig(env_dir / name) != sig:
                return None
    except OSError:
        return None

    # Re-run the recorded which() probes: a few directory stats per bare
    # command, no spec parsing. Closes the staleness gap in both directions
    # (resolved -> gone/different AND unresolved -> now-resolves).
    for key, recorded in stored["which"].items():
        bare, _, search_path = key.partition(_WHICH_KEY_SEP)
        try:
            current = resolved_command_casing(shutil.which(bare, path=search_path))
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
            spec = json.loads((overlay_dir / name).read_text(encoding="utf-8"))
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
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
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


def _relock_legacy_settings_overlay(
    overlay_dir: Path, stored: dict[str, Any] | None
) -> None:
    """Re-assert owner-only protection on the leftover legacy settings
    overlay — the ONE guard this pass keeps for that file.

    The pass never writes, reads, or deletes the leftover, but its ACL is
    re-tightened on every boot (a chmod / DACL edit changes no content
    signature) because the file carries the passed-through env (tokens / API
    keys) of non-poolable global servers. Dropping that repair would let a
    once-loosened ACL stay loosened forever.

    Provenance-gated: only a file whose live ``_stat_sig`` matches the
    fingerprint's recorded ``settings_overlay`` signature is touched —
    tightening, never deleting, and never a file the recorded signature cannot
    vouch for.

    Best-effort rather than fail-loud: nothing re-creates this file, so
    refusing the cache would force full rewrites forever without repairing
    anything; log and retry next pass instead.
    """
    if stored is None:
        return
    outputs = stored.get("outputs")
    sig = outputs.get("settings_overlay") if isinstance(outputs, dict) else None
    if sig is None:
        return
    legacy = overlay_dir.parent / "settings" / "mcp.json"
    try:
        if _stat_sig(legacy) != sig:
            return
        platform_compat.make_owner_only_dir(legacy.parent)
        if platform_compat.IS_POSIX:
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is OWNER-ONLY, the tightest traversable mode for a directory holding a credential-bearing file; the rule's suggested 0o644 would grant world-read and drop the execute bit a directory needs.  # noqa: E501
            os.chmod(legacy.parent, 0o700)
        platform_compat.restrict_to_owner(legacy)
    except OSError:
        logger.debug(
            "could not re-lock the legacy settings overlay; retrying next pass",
            exc_info=True,
        )


def _overlay_names_collide(overlay_dir: Path, first: str, second: str) -> bool:
    """Whether overlay names *first* and *second* are one file in *overlay_dir*.

    The two differ only by case, so they are one entry exactly when that
    directory folds case. Asked of the directory itself rather than of the
    platform: a same-file probe on the two spellings answers for the
    destination filesystem, and answers ``False`` on a case-sensitive one even
    when a stale overlay under the second spelling is present.
    """
    a, b = overlay_dir / first, overlay_dir / second
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


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
    approvals: LaunchApprovals | None = None,
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
        approvals: The operator's approved launch fingerprints
            (:mod:`kiro_crew.mcp_gateway.launch_approval`). When given, only an
            approved launch is wrapped, a cache-served result that names an
            unapproved target is discarded for a full pass, and the object
            records what the pass captured and refused for the caller to
            persist. ``None`` enforces nothing.

    Returns:
        A ``(results, target_env)`` tuple:

        * ``results``: mapping ``{agent_filename: wrapped_server_count}``.
          Agents with no MCP servers are omitted.
        * ``target_env``: mapping ``{KIROCREW_MCP_TARGET_<SERVER>: "cmd arg arg"}``
          suitable for ``GatewaySpec.mcp_target_env``. Gatewayd consults
          these when a stub registers, to find the real backend command
          to spawn for a new pool key.
    """
    # Deferred: ``agent_discovery`` reaches ``config.loader`` through ``hooks``,
    # and ``config.loader`` imports this module's path helpers at import time.
    from kiro_crew.agent_discovery import read_agent_spec_strict

    stub_set = stub_servers or frozenset()

    # An install upgraded from an older release can still carry a settings
    # overlay at ``<overlay_dir>/../settings/mcp.json``; this pass never
    # writes, reads, or DELETES it. Deliberately not swept: that leftover was
    # written owner-only via ``atomic_write(..., restrict_to_owner=True)``
    # into a 0o700 directory, its content is a subset copy of
    # the user's real ``~/.kiro/settings/mcp.json`` (same secrets, same disk,
    # same protection), and nothing reads it — so it is inert, not exposed.
    # An automated deleter, by contrast, is an attack surface: it must prove
    # the path is not the real settings file, not someone else's file under a
    # custom ``overlay_dir``, and not a symlink-redirected parent, and carry
    # deletion provenance across fingerprint rewrites. Leaving the file alone
    # has none of those failure modes; a user who wants it gone deletes it
    # once by hand. ONE guard is kept — the per-boot owner-only ACL relock
    # (see ``_relock_legacy_settings_overlay``, called below once the stored
    # fingerprint is loaded), because a loosened ACL is the one way the
    # leftover could stop being inert.

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
    # Same contract for the identity set: ONE resolved value per pass, recorded in
    # the fingerprint, handed to every consumer in it. gatewayd re-reads the same
    # helper at spawn rather than taking the stub's word for it.
    identity_keys = pool_identity_env_keys()
    # And for the read ceiling: ONE resolved value per pass, recorded in the
    # fingerprint and written onto every stub's argv. Resolving it per entry
    # would let a config edit mid-pass write two different ceilings, and leaving
    # it out of the fingerprint would let a kept overlay keep launching stubs
    # with the previous one after the operator raised the key.
    read_buffer_limit = config_read_buffer_limit()
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
        identity_keys=identity_keys,
        read_buffer_limit=read_buffer_limit,
    )
    if approvals is not None:
        current_inputs["launch_approvals"] = approvals.digest()
    with _PASS_INPUTS_LOCK:
        _PASS_INPUTS.pop(str(overlay_dir), None)
        _PASS_INPUTS[str(overlay_dir)] = current_inputs
        while len(_PASS_INPUTS) > _PASS_INPUTS_MAX:
            _PASS_INPUTS.pop(next(iter(_PASS_INPUTS)))
    stored = _load_fingerprint(fingerprint_path)
    # One call covers both paths below (cache hit returns early; the full
    # rewrite continues): re-tighten the leftover legacy settings overlay's
    # ACL when the stored fingerprint vouches for it.
    _relock_legacy_settings_overlay(overlay_dir, stored)
    if stored is not None and stored.get("inputs") == current_inputs:
        cached = _cached_rewrite_result(
            stored, overlay_dir=overlay_dir, stubs_dir=stubs_dir
        )
        if cached is not None and approvals is not None:
            # The overlays and sidecars behind the cache are files an agent can
            # edit along with the fingerprint that vouches for them, so the
            # cached targets are held to the same approval as a fresh pass.
            _kept, dropped = filter_target_env(cached[1], approvals)
            if dropped:
                logger.warning(
                    "mcp launch approvals: %d cached overlay target(s) are not "
                    "approved; regenerating the overlays",
                    len(dropped),
                )
                # Reset the whole refusal state this abandoned attempt left
                # behind, not the reasons alone: a stem still held in
                # ``refused_commands`` or ``incomplete_identities`` makes
                # ``refused_identities`` answer ``None`` for the full pass below,
                # and that record is stored without an ``expected_launch`` --
                # a server the operator can see refused and cannot approve.
                approvals.refused.clear()
                approvals.refused_commands.clear()
                approvals.incomplete_identities.clear()
                cached = None
        if cached is not None:
            return cached
    if approvals is not None:
        approvals.full_pass = True

    written: set[str] = set()
    written_sidecars = _SidecarLedger()
    results: dict[str, int] = {}
    target_env: dict[str, str] = {}
    notes = _RewritePassNotes()
    overlay_write_failed = False

    # Read the GLOBAL ~/.kiro/settings/mcp.json FIRST. kiro-cli merges this
    # file into every agent at runtime — any bare-name server declared here
    # bypasses the gateway unless wrapped (the "kirocrew-lite bypass" class of
    # bug: agents with empty mcpServers inherit the global's unwrapped entries).
    #
    # The fix is per-agent injection: each poolable settings server is added to
    # every agent's own overlay, wrapped with THAT agent's name, and the stub is
    # injected at ACP ``session/new``, where it takes precedence over the raw
    # same-named global entry kiro-cli merges (see ``session_servers.py``).
    # An agent whose spec sets ``includeMcpJson: false`` is skipped, since
    # kiro-cli merges nothing into it (see ``_rewrite_single_spec``).
    # Empty-``mcpServers`` agents get pooled coverage with the right identity,
    # and the duplicate / empty-``--agent`` collision never arises. The real
    # settings file is never modified, and no settings overlay is written:
    # non-poolable and HTTP/SSE servers keep merging from the real file exactly
    # as before pooling existed.
    settings_poolable: dict[str, Any] = {}
    settings_read_transient = False
    if kiro_settings_json.is_file():
        try:
            loaded = loads_user_json(kiro_settings_json.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings_poolable = _injectable_settings_servers(
                    loaded, stub_set,
                    pooling_enabled=pooling_enabled,
                    forward_env=forward_env,
                    identity_keys=identity_keys,
                    notes=notes,
                )
            # Valid JSON but not a dict: deterministic bad content, cacheable
            # (fixing it changes the stat signature); nothing to inject.
        except OSError as exc:
            # Transient read failure: same reasoning as the per-agent site —
            # do not cache a pass that treated an existing settings file as
            # absent, and keep the previous per-agent overlays.
            notes.source_read_failed = True
            settings_read_transient = True
            logger.warning("failed to read global mcp.json: %s", exc)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Content problem — cacheable; a fix changes the stat signature.
            logger.warning("failed to read global mcp.json: %s", exc)
    else:
        # ``is_file()`` answers False for a missing file AND for a stat fault
        # whose errno pathlib chooses to swallow, so it cannot be read as
        # "absent" on its own. Only SOME faults are swallowed: measured on
        # CPython 3.12, EACCES/EIO/EPERM propagate (the caller's ``except
        # Exception`` then abandons the pass without touching an overlay), while
        # ENOENT/EBADF/ENOTDIR/ELOOP return False. ENOTDIR and ELOOP are
        # reachable without the file being gone -- a directory component
        # momentarily replaced, an atomic directory swap, a symlink being
        # re-pointed -- and reading those as absent rewrote every overlay with an
        # empty injection set — the same degradation through the stat path
        # rather than the read path.
        #
        # So classify explicitly: only ``FileNotFoundError`` may mean absent
        # (deterministic, cacheable, nothing to inject); every other OSError
        # means unknown, which must stay uncacheable and keep the previous
        # per-agent overlays.
        try:
            kiro_settings_json.stat()
        except FileNotFoundError:
            pass  # confirmed absent: nothing to inject, cacheable
        except OSError as exc:
            notes.source_read_failed = True  # unknown: keep and retry
            settings_read_transient = True
            logger.warning("failed to stat global mcp.json: %s", exc)
        # A path that stats fine but is not a regular file (e.g. a directory
        # in its place) is a permanent misconfiguration, deterministic like
        # bad content: cacheable, nothing to inject.

    # Names of agents whose overlay could not be refreshed THIS PASS for a
    # TRANSIENT reason (source read failure, overlay write failure). The prune
    # keep-set is ``written | transient_keep``: a transient victim keeps its
    # previous, healthy overlay (stale-but-working beats no overlay at all),
    # while a deterministic skip (bad JSON, non-dict spec) prunes
    # exactly as a deleted source does — those passes are cacheable, and the
    # cached path's prune would sweep a kept-stale overlay one boot later
    # anyway, so keeping it here would make two boots over identical inputs
    # behave differently.
    transient_keep: set[str] = set()

    # A settings read that FAILED is not a settings file that declared nothing.
    # ``settings_poolable`` is empty on that path because the pass could not
    # ASK, so an agent overlay written from it reflects a fact this pass never
    # established: every globally-declared poolable server silently stops being
    # pooled for the rest of this gateway's lifetime. Its tools do not vanish --
    # the raw entry still merges from the real settings file -- but it runs
    # per-session and unpooled, with none of the identity the stub carries. The
    # pass is already uncacheable (``notes.source_read_failed`` was set at the
    # read site), so a restart self-heals; the degraded window is a whole
    # gateway lifetime.
    #
    # Refuse to rewrite instead, exactly as the per-agent transient read
    # failure below does -- but only where refusing PRESERVES something. An
    # agent that has a previous overlay keeps it: that overlay carries both its
    # own wrapped servers and the injected globals, so nothing is dropped. An
    # agent with NO previous overlay is still written, because there the empty
    # injection set is not the conflation this guards against -- no injected
    # copy exists to drop, and refusing would leave that agent with no overlay
    # at all, unpooling its OWN servers too. That is strictly worse than the
    # fault warrants, and worse than what this pass does today.
    #
    # The trade on a kept overlay is staleness in the other direction, and it is
    # bounded to the injection set alone: a keep is only honest when NOTHING
    # ELSE the overlay encodes has changed. Keeping one otherwise defers the
    # agent's own spec (an autoApprove entry removed, a server disabled) and the
    # policy knobs _build_stub_entry bakes into every stub argv (--sandbox-mode,
    # --approval-mode, --poolable, work dir, socket, identity keys) -- all
    # deliberate instructions, unlike a transient fault. So the keep is gated on
    # _overlay_inputs_unchanged: the stored fingerprint must show this pass's
    # inputs matching the ones that overlay was built from, every input except
    # the settings entry the failed read made unknowable. One comparison covers
    # every dimension, so a future fingerprinted input is covered without being
    # enumerated at this site.
    #
    # Gated on nothing else: with an empty stub set nothing is wrapped and
    # ``_injectable_settings_servers`` returns nothing whatever the file says, so
    # a keep and a rewrite produce identical bytes there. An extra
    # ``bool(stub_set)`` term would be unobservable -- no test can distinguish
    # it -- so the fingerprint comparison below carries the whole decision.
    injection_unknown = settings_read_transient
    # A keep SERVES artifacts this pass did not write, so it must carry the same
    # guarantees the other serve-without-writing path does. Vouch for the shared
    # set once: if the recorded sidecars cannot be validated and re-protected, no
    # keep is allowed at all and every agent goes through the
    # protect-before-content writers instead.
    if injection_unknown and not _kept_artifacts_vouched(
        stored, env_dir=env_sidecar_dir_for_stubs(stubs_dir)
    ):
        injection_unknown = False
        logger.warning(
            "global mcp.json unreadable this pass, but the recorded env sidecars "
            "could not be validated and re-protected: rewriting every agent "
            "overlay rather than serving artifacts this pass cannot vouch for"
        )
    if injection_unknown:
        logger.warning(
            "global mcp.json unreadable this pass: keeping the previous overlay "
            "of each agent whose other inputs are unchanged, instead of "
            "rewriting it without the servers that file declares (kept overlays "
            "stay in effect until a later pass succeeds)"
        )

    # Overlay destinations this pass has claimed, folded to one case. Two live
    # sources whose stems differ only by case (``Foo.json`` and ``foo.md`` on a
    # case-sensitive source directory) take two overlay files there -- but if
    # the overlay directory is case-insensitive they are ONE file, and the
    # second write would hand one agent the other's MCP servers. The listing
    # already sets a twin aside when the SOURCE directory folds case; this
    # guards the destination, which may sit on a different filesystem. A
    # destination is claimed only by a source that is kept or parsed: a source
    # skipped for bad content claims nothing, so it cannot cost its valid twin
    # the overlay (and the pruning of the twin's stale one) below.
    overlay_keys_claimed: dict[str, str] = {}
    for path in iter_agent_spec_files(source_dir):
        # The overlay is JSON whatever the source: ``session_servers`` resolves
        # ``<agent>.json``. The fingerprint's ``sources`` stays keyed by the
        # SOURCE name, so a markdown edit invalidates its overlay.
        overlay_name = f"{path.stem}.json"
        overlay_key = overlay_name.casefold()
        first_claim = overlay_keys_claimed.get(overlay_key)
        if first_claim is not None and _overlay_names_collide(
            overlay_dir, first_claim, overlay_name
        ):
            logger.warning(
                "skipping agent %s: its overlay %s is the same file as overlay %s on "
                "this case-insensitive overlay directory; rename one of the two agents",
                path.name,
                overlay_name,
                first_claim,
            )
            continue
        if (
            injection_unknown
            and (overlay_dir / overlay_name).is_file()
            and _overlay_inputs_unchanged(
                stored, current_inputs, source_name=path.name
            )
            and _kept_overlay_vouched(stored, overlay_dir=overlay_dir, name=overlay_name)
        ):
            # Keep without classifying: the spec is not read on this path, so a
            # source whose CONTENT is deterministically bad is kept too, unlike
            # the read below which prunes it. The pass is uncacheable, so the
            # next boot reads that source and prunes its overlay then.
            overlay_keys_claimed.setdefault(overlay_key, overlay_name)
            transient_keep.add(overlay_name)
            continue
        try:
            # The hardened reader keeps the transient/deterministic split the
            # two handlers below depend on, and refuses what a bare read would
            # follow: a symlink in this user-writable directory pointing at a
            # sensitive file, whose content would otherwise land in an overlay.
            spec = read_agent_spec_strict(path, operation="mcp_overlay_rewrite", source="unknown")
        except FileNotFoundError as exc:
            # Gone, not faulty: prune exactly as a source already absent when the
            # listing ran does. Ahead of the OSError arm, which would keep the
            # deleted agent's overlay for one more pass.
            notes.source_deleted_mid_pass = True  # signatures predate the deletion
            logger.warning("skipping agent %s: %s (deleted: overlay pruned)", path.name, exc)
            continue
        except OSError as exc:
            # Transient: the file stat'ed fine for the fingerprint but could
            # not be read. Readability can return without size/mtime changing,
            # so caching this incomplete pass would serve overlays missing
            # this agent forever. Mark the pass uncacheable, and keep the
            # agent's previous overlay.
            notes.source_read_failed = True
            overlay_keys_claimed.setdefault(overlay_key, overlay_name)
            transient_keep.add(overlay_name)
            logger.warning(
                "skipping agent %s: %s (previous overlay, if any, stays in "
                "effect until a later pass succeeds)",
                path.name,
                exc,
            )
            continue
        except ValueError as exc:
            # Deterministic: the CONTENT is bad (JSON, frontmatter, encoding, a
            # sensitive or oversized target), and fixing it changes the file's
            # stat signature, which invalidates the fingerprint — so this skip
            # is safe to cache.
            logger.warning("skipping agent %s: %s", path.name, exc)
            continue
        if not isinstance(spec, dict):
            continue
        # Parsed: this source owns the destination for the rest of the pass.
        overlay_keys_claimed.setdefault(overlay_key, overlay_name)
        # Guarantee a non-empty agent identity. The rewriter reads
        # ``~/.kiro/agents/*.json`` directly, and a user- or tool-dropped file
        # may omit ``name``. Without a name, ``_rewrite_single_spec`` derives
        # ``agent_name = ""`` and every wrapped stub carries ``--agent ""`` —
        # collapsing PoolKey identity across all such agents (cross-agent
        # backend-bucket sharing / isolation loss). Fall back to the file stem,
        # mirroring ``agent_parts/spec_upgrades.py`` (``data.get("name") or spec_path.stem``); any
        # stable non-empty identifier prevents the collapse.
        if not spec.get("name"):
            spec["name"] = path.stem
        try:
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
                identity_keys=identity_keys,
                read_buffer_limit=read_buffer_limit,
                inject_servers=settings_poolable,
                target_env=target_env,
                sidecars_written=written_sidecars,
                notes=notes,
                approvals=approvals,
            )
            _collect_target_env(new_spec.get("mcpServers", {}), target_env)
            target = overlay_dir / overlay_name
            try:
                # Atomic + owner-only: temp-file + os.replace (via atomic_write) so a
                # concurrent reader — the per-session stub injection resolves this
                # overlay at ACP ``session/new`` (see ``session_servers.py``; there
                # is no bind mount), and the cache-validation pass digests it —
                # never sees a truncated spec (which would make the agent's MCP
                # servers vanish mid-run). ``restrict_to_owner=True`` locks the temp
                # file down BEFORE the passed-through non-poolable / HTTP-SSE env
                # blocks (tokens / API keys) reach it — POSIX mode bits are a no-op
                # against NTFS ACLs, and a Windows-only post-rename lockdown would
                # leave them readable under the inherited DACL for the write
                # window. It implies 0o600 on POSIX. A lockdown
                # failure happens before the rename, so the OSError handler
                # below skips the overlay without ever publishing an unprotected
                # copy. Matches the env sidecar.
                atomic_write(target, json.dumps(new_spec, indent=2) + "\n", restrict_to_owner=True)
            except OSError as exc:
                logger.warning(
                    "failed to write overlay %s: %s (previous overlay, if any, "
                    "stays in effect until a later pass succeeds)",
                    target,
                    exc,
                )
                overlay_write_failed = True
                transient_keep.add(overlay_name)
                # The kept overlay carries the older argv, so drop this agent's
                # staged sidecars and leave it beside the env it was built
                # against. Their names stay in the ledger, so the sidecar prune
                # never sweeps the published files.
                written_sidecars.discard()
                continue
            if not written_sidecars.commit():
                # A staged sidecar could not be renamed into place. Same class as
                # a failed sidecar write: make the pass uncacheable so the next
                # one retries.
                notes.sidecar_write_failed = True
        finally:
            # Any exit from this iteration, a raise included, must reclaim staged
            # temps: their names start with a dot and pathlib's ``*.json`` glob
            # skips dotfiles, so no prune would ever see them. A discard after a
            # commit is a no-op.
            written_sidecars.discard()
        written.add(overlay_name)
        if wrapped:
            results[overlay_name] = wrapped

    if approvals is not None and notes.sidecar_write_failed:
        approvals.rebind_incomplete = True

    if approvals is not None and transient_keep:
        # A kept overlay's launches were never admitted this pass -- the keep
        # paths above skip the spec read, or abandon the agent after it -- so
        # the approval snapshot's live set is missing them. Say so, or a
        # ``${VAR}`` rebind by another agent declaring the same command would
        # retire the kept agent's own approved pair as unseen, and its kept
        # sidecar would fail the approval check at spawn time.
        approvals.live_incomplete = True

    # Prune stale overlay entries (user deleted or renamed an agent). The
    # keep-set answers "does this overlay's source still exist and did we
    # either refresh it or fail TRANSIENTLY?" — never bare write success,
    # which would conflate a transient failure with a deleted source and unlink
    # the previous, healthy overlay. Deterministic skips (bad JSON,
    # non-dict) stay OUT of the keep-set: their pass is cacheable, and the
    # cached-path prune keys on the stored outputs, so keeping them here
    # would let two boots over identical inputs disagree.
    for stale in overlay_dir.glob("*.json"):
        if stale.name not in written and stale.name not in transient_keep:
            try:
                stale.unlink()
            except OSError:
                pass

    # Every overlay that SURVIVES the prune must have its target mappings
    # published, or a kept overlay's stub resolves through the bare
    # server-name fallback — which, when two agents declare the same server
    # name with different args, is another agent's command. Harvest the kept
    # overlays' wrapped entries into ``target_env`` exactly as the cached
    # path does (``_cached_rewrite_result`` reconstructs from overlay files),
    # via ``setdefault`` inside ``_collect_target_env`` so entries from the
    # freshly-rewritten specs always win and the kept overlay only fills the
    # hash-keyed slots nothing else claimed. Unreadable/corrupt kept overlays
    # are skipped — the stub then degrades to the same fallback as before.
    for name in sorted(transient_keep):
        kept = overlay_dir / name
        try:
            kept_spec = json.loads(kept.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(kept_spec, dict):
            servers = kept_spec.get("mcpServers", {})
            if isinstance(servers, dict):
                _collect_target_env(servers, target_env)

    # Prune stale env sidecars (server removed / renamed / flipped
    # non-poolable) so old credential files don't accumulate on disk.
    # ``written_sidecars`` is enumeration-keyed, not write-success-keyed:
    # ``_build_stub_entry`` adds the sidecar name BEFORE attempting the write,
    # so a transient sidecar write failure keeps the previous sidecar on disk
    # (and ``notes.sidecar_write_failed`` makes the pass uncacheable). But on
    # any pass that KEPT a previous overlay (source read failure — the
    # victim's sidecar names are unknowable; overlay write failure — the kept
    # overlay may reference sidecars of servers the new spec renamed away),
    # the kept overlay still points ``--env-file`` at old names, and pruning
    # them would spawn its backends credential-less for the rest of this
    # gateway's lifetime. Skip the sidecar prune on such a pass: it is
    # already uncacheable, so the next boot re-enumerates and sweeps.
    env_dir = env_sidecar_dir_for_stubs(stubs_dir)
    if env_dir.is_dir() and not (notes.source_read_failed or overlay_write_failed):
        for stale in env_dir.glob("*.json"):
            if stale.name not in written_sidecars.names:
                try:
                    stale.unlink()
                except OSError:
                    pass

    total_wrapped = sum(results.values())

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
    elif notes.source_deleted_mid_pass:
        uncacheable = "source(s) deleted between listing and read"
    elif notes.sidecar_write_failed:
        uncacheable = "env sidecar write failure(s)"
    elif overlay_write_failed:
        uncacheable = "overlay write failure(s)"
    elif notes.env_placeholder_seen:
        # Not a fault: a declared env carried a ${VAR}/${env:VAR} reference, so
        # a sidecar's contents depend on the ENVIRONMENT as well as the spec
        # files. The environment is not a fingerprinted input, so caching this
        # pass would serve a sidecar expanded against a since-changed variable
        # (a rotated credential silently kept flowing the old value). Re-resolve
        # on every boot instead; specs with no placeholder still cache normally.
        uncacheable = "declared env contains ${VAR} placeholder(s)"
    # While the leftover legacy settings overlay survives, the provenance
    # that licenses its per-boot ACL relock lives ONLY in the stored
    # fingerprint's ``settings_overlay`` signature. Both fingerprint-
    # replacement paths below carry that signature forward while the file
    # exists — dropping it would silently end the relock guard for the rest of
    # the install's life. Never re-derived from the live file: a file edited
    # since that signature was recorded loses its vouching exactly as
    # it should.
    legacy_sig = None
    _stored_outputs = (stored or {}).get("outputs")
    if isinstance(_stored_outputs, dict):
        legacy_sig = _stored_outputs.get("settings_overlay")
    if legacy_sig is not None:
        try:
            if not (overlay_dir.parent / "settings" / "mcp.json").is_file():
                legacy_sig = None  # file gone: nothing left to vouch for
        except OSError:
            pass  # unknown: keep the signature, losing it is the worse error

    if uncacheable:
        logger.debug("rewriter: %s; not caching this rewrite", uncacheable)
        # Remove any fingerprint from an earlier successful run: it could
        # still match the current inputs (this rewrite may have been forced by
        # a missing output, not an input change) and would freeze the
        # degraded state instead of retrying.
        if legacy_sig is not None:
            # Replace rather than unlink: empty ``inputs`` can never match a
            # real pass (so no degraded state is served from cache), while the
            # relock provenance survives for the next pass.
            _store_fingerprint(
                fingerprint_path,
                inputs={},
                outputs={
                    "overlays": {},
                    "sidecars": {},
                    "settings_overlay": legacy_sig,
                },
                which={},
            )
        else:
            with contextlib.suppress(OSError):
                fingerprint_path.unlink(missing_ok=True)
    else:
        output_sigs: dict[str, Any] = {
            "overlays": {n: _stat_sig(overlay_dir / n) for n in sorted(written)},
            "sidecars": {n: _stat_sig(env_dir / n) for n in sorted(written_sidecars.names)},
        }
        if legacy_sig is not None:
            output_sigs["settings_overlay"] = legacy_sig
        # A None signature means an output vanished between write and stat —
        # storing it would produce a fingerprint the loader rejects anyway;
        # skip storing so the next boot simply rewrites.
        if (
            all(output_sigs["overlays"].values())
            and all(output_sigs["sidecars"].values())
        ):
            _store_fingerprint(
                fingerprint_path,
                inputs=current_inputs,
                outputs=output_sigs,
                which=notes.which_results,
            )

    return results, target_env


# --------------------------------------------------------------------------- #
# Compatibility facade. The rewriter's helpers live in the ``rewriter_*`` owner
# modules listed below, one responsibility each; this module keeps the pass
# (:func:`rewrite_agents`) and the fingerprint that decides whether a pass may be
# skipped. Every name stays readable as ``kiro_crew.mcp_gateway.rewriter.<name>``:
# this module answers a name it does not bind from the owner that defines it, and a
# write or delete through this module reaches every owner binding the same object
# (``kiro_crew._facade_mirror``), so a patch of
# ``kiro_crew.mcp_gateway.rewriter.<name>`` reaches every reader it reached when this
# was one module. Owners never import this module and log under its logger.
# --------------------------------------------------------------------------- #
#: The owner modules, lowest layer first: an owner imports only owners listed before
#: it, and the first holder of a name answers a read of it.
_REWRITER_OWNERS: tuple[str, ...] = (
    "kiro_crew.mcp_gateway.rewriter_launch",
    "kiro_crew.mcp_gateway.rewriter_env",
    "kiro_crew.mcp_gateway.rewriter_overlay",
    "kiro_crew.mcp_gateway.rewriter_spec",
)
for _owner in _REWRITER_OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _REWRITER_OWNERS, excluded=()).served
)
# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name != "TYPE_CHECKING"
)

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    # The names the unsplit module imported from the rest of kiro_crew and that only
    # an owner reads now, from their home modules.
    from kiro_crew.config.paths import config_dir  # noqa: F401
    from kiro_crew.env import spec_path_key  # noqa: F401
    from kiro_crew.mcp_cleanup import (  # noqa: F401
        KIROCREW_BIN_MCP_SERVERS,
        mcp_entry_is_muted,
        mcp_entry_is_registry_governed,
    )
    from kiro_crew.mcp_gateway import STUB_MODULE  # noqa: F401
    from kiro_crew.mcp_gateway.hashing import (  # noqa: F401
        STUB_FLAGS_FLAG,
        decode_target_args,
        encode_target_args,
        expand_stub_flags,
        hash_command,
        is_secret_env_key,
    )
    from kiro_crew.mcp_gateway.launch_approval import (  # noqa: F401
        env_fingerprint,
        launch_fingerprint,
    )
    from kiro_crew.mcp_gateway.manager import is_credential_env_key  # noqa: F401
    from kiro_crew.mcp_gateway.rewriter_env import (  # noqa: F401
        _ENV_VAR_PLACEHOLDER,
        _collision_warn_lock,
        _collision_warned_keys,
        _expand_env_map,
        _expand_env_placeholders,
        _must_stay_wrapped,
        _needs_private_backend,
        _normalized_env,
        _placeholder_source_env,
        _reset_collision_warnings,
        _withheld_env_count,
    )
    from kiro_crew.mcp_gateway.rewriter_launch import (  # noqa: F401
        _CMD_UNSAFE,
        _RESERVED_ENTRY_SPEC_KEYS,
        _STUB_MODULE,
        _TARGET_ARGS_FLAG,
        _TARGET_ARGS_FLAG_LEGACY,
        _TARGET_ARGS_SEP,
        _cmd_unsafe_warn_lock,
        _cmd_unsafe_warned_elements,
        _is_managed_launch,
        _managed_invocation,
        _owned_control_plane_env,
        _repair_control_plane_entry,
        _reset_cmd_unsafe_warnings,
        _resolve_target_command,
        _spec_args,
        _unwrapped_from_prewrapped,
        _wrapped_target,
    )
    from kiro_crew.mcp_gateway.rewriter_overlay import (  # noqa: F401
        default_overlay_dir,
        default_socket_path,
        env_sidecar_dir,
        env_sidecar_name,
        overlay_ready,
        records_dir,
        resolve_overlay_dir,
        runtime_dir,
    )
    from kiro_crew.mcp_gateway.rewriter_spec import (  # noqa: F401
        _WINDOWS_CMD_LINE_LIMIT,
        UNPOOLABLE_SERVERS,
        _admit_launch,
        _build_stub_entry,
        _hashable_args,
        is_unpoolable,
    )
    from kiro_crew.mcp_gateway.secret_uri import (  # noqa: F401
        routed_for_secret_reference,
        secret_reference_keys,
    )
    from kiro_crew.mcp_provenance import (  # noqa: F401
        WITHHELD_KEY,
        is_withheld_mute,
    )
    from kiro_crew.mcp_utils import mcp_server_alias  # noqa: F401
    from kiro_crew.sandbox import scrub_agent_denied_env  # noqa: F401
    from kiro_crew.security import is_sensitive_path  # noqa: F401
