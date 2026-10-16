"""Where the rewriter's output lives, and whether a kept overlay still vouches for itself.

Owner module of :mod:`kiro_crew.mcp_gateway.rewriter`, which re-exports every name
here; read and patch them there. This module owns:

* the locations the rewriter writes and the gateway reads: the overlay directory
  (:func:`default_overlay_dir`, :func:`resolve_overlay_dir`, :func:`overlay_ready`),
  the env sidecars beside it (:func:`env_sidecar_dir`, :func:`env_sidecar_name`)
  and the runtime directory (:func:`runtime_dir`, :func:`default_socket_path`,
  :func:`records_dir`);
* the content signatures a fingerprint is built from (:func:`_stat_sig`,
  :func:`_source_sig`);
* the checks that a kept overlay and its artifacts are still the ones the last pass
  wrote (:func:`_overlay_inputs_unchanged`, :func:`_kept_artifacts_vouched`,
  :func:`_kept_overlay_vouched`).

It never imports the facade.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.env import resolved_command_casing
from kiro_crew.mcp_gateway.rewriter_launch import _WHICH_KEY_SEP
from kiro_crew.security import is_sensitive_path


def _overlay_inputs_unchanged(
    stored: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    source_name: str,
) -> bool:
    """Return ``True`` when every input the overlay for *source_name* was built
    from still matches this pass -- everything EXCEPT the settings entry.

    Consulted to decide whether a previous overlay may be KEPT on a pass that cannot
    establish the injection set. Keeping one defers every input that overlay
    encodes, not only the injection: the agent's own spec (an ``autoApprove``
    entry removed, a server disabled) and the policy knobs
    ``_build_stub_entry`` bakes into each stub's argv (``--sandbox-mode``,
    ``--approval-mode``, ``--poolable``, the work dir, the socket, the identity
    keys). A revocation or a tightened policy is a deliberate instruction and
    must not wait for the next boot, so a keep is only honest when nothing it
    would defer has changed.

    The settings entry is the ONE input compared conditionally, and only because
    a transient fault is what makes it unanswerable: ``_rewrite_inputs_fingerprint``
    signs that file with ``_stat_sig``, which returns ``None`` when it cannot be
    read, and that ``None`` is why control reaches the rewrite loop instead of the
    cached early return. So it is skipped when this pass could not sign the file --
    demanding a match there would refuse every keep on exactly the path this
    protects. When the signature IS available and differs, the file demonstrably
    changed and the keep is refused: read_text and read_bytes are separate calls,
    so a fault can hit one and not the other, and a settings revocation kept alive
    by a stale injected entry is an absorbed instruction, not a deferred edit.
    Every other input is compared unconditionally, so a new fingerprinted input is
    covered without being enumerated here.

    ``False`` whenever the answer cannot be established: no stored fingerprint
    (one is unlinked at the end of every uncacheable pass, so two consecutive
    faulty passes cannot keep), a malformed one, or a source this pass could not
    sign. The caller then rewrites, which is the pre-existing behaviour.
    """
    if stored is None:
        return False
    prev = stored.get("inputs")
    if not isinstance(prev, dict):
        return False
    for key in set(prev) | set(current):
        if key == "sources":
            continue
        if key == "settings":
            cur_settings = current.get("settings")
            if cur_settings is None:
                continue  # unsignable this pass: the unanswerable input
            if prev.get("settings") != cur_settings:
                return False  # signable AND changed: a real, known difference
            continue
        if prev.get(key) != current.get(key):
            return False
    prev_sources = prev.get("sources")
    cur_sources = current.get("sources")
    if not isinstance(prev_sources, dict) or not isinstance(cur_sources, dict):
        return False
    sig = cur_sources.get(source_name)
    # A ``None`` signature means this pass could not read or stat that source,
    # so "unchanged" is not established -- never assume it.
    return sig is not None and prev_sources.get(source_name) == sig


def _kept_artifacts_vouched(
    stored: dict[str, Any] | None,
    *,
    env_dir: Path,
) -> bool:
    """Validate and re-protect the SHARED artifacts a keep would serve.

    A keep serves files this pass did not write, which is the same position
    ``_cached_rewrite_result`` is in -- and that path does not merely check
    existence. It compares every recorded output against ``_stat_sig`` (a
    tampered or edited artifact must be regenerated, not served) and re-asserts
    owner-only protection on each one, because a chmod or DACL edit changes no
    size, mtime or digest and so is invisible to a signature. A keep must offer
    the same guarantees or it becomes a way to have a tampered overlay served,
    and a loosened sidecar ACL left unrepaired, by inducing one transient fault.

    This covers the pass-wide set: the env sidecar directory, every recorded
    sidecar, and the recorded ``shutil.which`` probes. A kept overlay still
    points ``--env-file`` at those sidecars, the sidecar prune is skipped on this
    pass, and mapping sidecars to individual agents would require parsing stub
    argv -- so if the set cannot be vouched for, no keep is allowed and every
    agent is rewritten through the protect-before-content writers. Per-overlay
    validation is separate; see ``_kept_overlay_vouched``.

    The which() re-probe is here for the same reason the cached path has it, and
    it is not covered by any signature: directory contents are which() input the
    stat fingerprint cannot see, and a kept overlay's stub argv embeds the
    ABSOLUTE path a previous pass resolved. A target binary removed, moved
    between PATH prefixes, or newly shadowed would otherwise leave the kept
    overlay launching a dead path for the rest of the gateway's lifetime.

    Fail-loud like the cached path: ``restrict_to_owner`` raises on both
    platforms, and any failure returns ``False`` rather than serving an
    artifact whose protection could not be re-asserted.
    """
    if stored is None:
        return False
    outputs = stored.get("outputs")
    if not isinstance(outputs, dict):
        return False
    sidecar_sigs = outputs.get("sidecars")
    if not isinstance(sidecar_sigs, dict):
        return False
    which_probes = stored.get("which")
    if not isinstance(which_probes, dict):
        return False
    try:
        for name, sig in sidecar_sigs.items():
            if _stat_sig(env_dir / name) != sig:
                return False
        if env_dir.is_dir():
            platform_compat.make_owner_only_dir(env_dir)
            if platform_compat.IS_POSIX:
                # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is OWNER-ONLY, the tightest traversable mode for this credential-sidecar directory; the rule's suggested 0o644 would grant world-read and drop the execute bit a directory needs. Raw os.chmod (not make_owner_only_dir alone) because this path must FAIL LOUD into the full rewrite, matching _cached_rewrite_result.  # noqa: E501
                os.chmod(env_dir, 0o700)
        for name in sidecar_sigs:
            platform_compat.restrict_to_owner(env_dir / name)
    except OSError:
        return False
    # Same comparison the cached path makes, for the same reason.
    for key, recorded in which_probes.items():
        bare, _, search_path = key.partition(_WHICH_KEY_SEP)
        try:
            current = resolved_command_casing(shutil.which(bare, path=search_path))
        except OSError:
            return False
        if current != recorded:
            return False
    return True


def _kept_overlay_vouched(
    stored: dict[str, Any] | None,
    *,
    overlay_dir: Path,
    name: str,
) -> bool:
    """Validate and re-protect ONE overlay a keep would serve.

    The per-agent half of :func:`_kept_artifacts_vouched`: the overlay must
    still carry the size+mtime+digest the previous run recorded for it, and its
    owner-only protection must be re-assertable. An overlay with no recorded
    signature is refused too -- there is nothing to compare it against, and an
    unvouched artifact must be regenerated rather than served.
    """
    if stored is None:
        return False
    outputs = stored.get("outputs")
    if not isinstance(outputs, dict):
        return False
    overlay_sigs = outputs.get("overlays")
    if not isinstance(overlay_sigs, dict):
        return False
    sig = overlay_sigs.get(name)
    if sig is None:
        return False
    target = overlay_dir / name
    try:
        if _stat_sig(target) != sig:
            return False
        platform_compat.restrict_to_owner(target)
    except OSError:
        return False
    return True


def _stat_sig(path: Path) -> list[Any] | None:
    """Return ``[size, mtime_ns, sha256]`` for *path*, or ``None`` if it
    cannot be read. Size and nanosecond mtime are cheap discriminators, but
    neither is sufficient alone or together: a same-size write can land inside
    one filesystem timestamp tick (coarse on some filesystems), and a
    ``chmod`` changes neither — so the content digest is what makes a
    signature collision impossible for changed bytes. The files signed here
    are small JSON documents, so hashing them is microseconds against the
    parse+resolve+write pass the fingerprint exists to skip.

    For Crew's OWN files (settings, overlays, sidecars); an agent SOURCE is
    signed by :func:`_source_sig`, which reads through the hardened gate."""
    try:
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, digest]


def _source_sig(path: Path) -> list[Any] | None:
    """:func:`_stat_sig` for a file in the user-writable agents directory.

    The resolved target is checked against ``is_sensitive_path`` and the bytes
    come through ``hooks.safe_read_file_bytes`` (size-capped, no-reparse open):
    a symlink dropped beside the specs and pointing at a credential file must not
    be read -- not even to digest it, since the digest of a small secret would be
    stored in the fingerprint file. Such a source signs as ``None``, the same as
    one that cannot be read, and the rewrite loop's own hardened read then
    refuses it deterministically.
    """
    # Deferred: hooks reaches config.loader, which imports this module's path
    # helpers at import time.
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes

    try:
        real = path.resolve(strict=True)
        if is_sensitive_path(str(real)):
            return None
        st = path.stat()
        raw = safe_read_file_bytes(str(real))
    except (OSError, RuntimeError, FileTooLargeError):
        return None
    if raw is None:
        return None
    return [st.st_size, st.st_mtime_ns, hashlib.sha256(raw).hexdigest()]


def overlay_ready(overlay_dir: Path) -> bool:
    """Return ``True`` if ``overlay_dir`` has at least one readable JSON."""
    if not overlay_dir.is_dir():
        return False
    try:
        return any(p.is_file() for p in overlay_dir.glob("*.json"))
    except OSError:
        return False


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

    THE AGENT STAYS IN THE NAME EVEN THOUGH IT IS NOT A POOL DIMENSION, and the
    two facts do not conflict. This file carries one declaration's FULL env,
    rotating secrets included, and it is read back unfiltered for a
    connection-private backend. The pool dimension available instead,
    ``effective_env_hash``, deliberately EXCLUDES every
    ``AWS_SECRET``/``AWS_SESSION``/``OAUTH`` key, so two agents declaring one
    server with the same non-secret env and different credentials hash equal:
    naming the file after that hash would put both declarations on one name,
    the second write would win, and the first agent's private backend would be
    handed the second agent's credentials. One name per declaration makes that
    substitution unrepresentable. Pool identity is unaffected either way --
    nothing here reaches :class:`~kiro_crew.mcp_gateway.pool.PoolKey`, so two
    agents declaring a server identically still share one backend and simply
    read their own byte-identical copy of this file.

    Single source of truth for the naming rule: the rewriter writes the sidecar
    and ``gatewayd`` reads it back by recomputing this name from the agent its
    Register frame named plus the PoolKey's ``server_name``, so a change here
    moves both ends at once. Sidecars written under an older naming scheme are
    pruned as stale by ``rewrite_agents`` (it deletes any ``env/*.json`` it did
    not just write).
    """

    def _san(s: str) -> str:
        return "".join(c if (c.isalnum() or c in "_-") else "_" for c in s)

    digest = hashlib.sha256(f"{agent_name}\0{server_name}".encode("utf-8")).hexdigest()[:12]
    return f"{_san(agent_name)}.{_san(server_name)}.{digest}.json"


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
