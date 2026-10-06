"""External registry indexes: fetch, the configured-branch rule, cache, load.

An index is a shallow clone of the registry repository, parsed from
``app-registry.json`` or synthesized from ``apps/*/app.json``. Fetch-then-swap keeps
a stale cache serving when a refresh fails. ``_owner_tier_confirmed`` is the one
install-time decision that re-reads an index fresh instead of trusting its cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.registry_pipeline import _FACADE, _facade
from kiro_crew.apps.registry_pipeline.caches import (
    _credential_free_external_registry_entries,
    _external_registry_cache_identity,
    _read_external_registry_cache,
    _remove_legacy_credential_registry_cache,
    _remove_legacy_name_keyed_registry_cache,
    _write_external_registry_cache,
)
from kiro_crew.apps.registry_pipeline.checkout import (
    _CLONE_TIMEOUT,
    _HOOKS_NEUTRALIZER_ARGV,
    _communicate_with_timeout,
    _git_fetch_branch,
    _rmtree_force_settled,
)
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _git_target_is_unsupported,
    _loggable_git_transport_output,
    _looks_like_git_url,
    _public_registry_name,
    _redact_url_userinfo,
    _same_git_target,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.sources import (
    _TRUST_OWNER,
    _context_clone_sandbox_mode,
    _effective_registries,
    _install_coordinates,
    _registry_trust_tier_of,
    _sel_credential_decision,
    _sel_fn,
)
from kiro_crew.apps.registry_pipeline.store_art import (
    _SAFE_BRANCH_RE,
    _prewarm_owner_tier_store_assets,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import minimal_env
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

logger = logging.getLogger(_FACADE)


async def _owner_tier_confirmed(entry: dict[str, Any]) -> bool:
    """True when an ``owner``-tier registry FRESHLY confirms *entry*'s clone URL.

    The credential escalation an organisation-wide registry needs cannot come from
    :func:`_is_owner_designated_repo`: that compares against the index URL itself,
    and a real catalog's apps live in other repos. But it also cannot simply
    believe the row, because the row reaching this point was read from
    ``_read_external_registry_cache`` — **agent-writable** content, as
    :func:`_resolve_registry_row` says of the same file when it refuses to resolve
    an install from it. Trusting the tier on a cached row would let anything able
    to write that cache name an arbitrary repo on the operator's own forge and
    have it cloned with the gateway's git identity: the confused-deputy read the
    anonymous posture exists to prevent, merely relocated from the index to its
    cache.

    So the tier is honoured only after a FRESH fetch of that registry's index
    confirms an entry whose clone URL is **byte-identical** to this one's. That
    mirrors the official catalog, whose install coordinates likewise never come
    from a cache. Consequences, all deliberate:

    - **Install only.** Callers are the explicit per-app install action. The
      automatic browse/refresh clones keep the credential-free posture, per
      :func:`anonymous_git_env`'s contract — they are not gated by any owner
      action, and a network round trip per listed row would be the wrong cost
      anyway. The ONE browse-time exception shares this function's authority
      rather than weakening it: ``store_art._prewarm_owner_tier_store_assets``
      runs on the rows a fresh index fetch just returned, in the same call, so it
      needs no re-fetch — the fresh index IS its input, never the cache.
    - **Fail closed, never fall back.** An unreachable index, a parse failure, a
      missing entry, or a URL that does not match exactly all return ``False``,
      which leaves the anonymous posture in place. The cost is availability on a
      path that already needs the network to clone.
    - **The fresh index is authority for the URL only.** It cannot promote the
      tier (that is read from operator/edition config) and it cannot widen the
      host set (``is_clone_host_trusted`` still gates the clone).
    - **Every install coordinate must match, not just the URL.** ``branch`` and
      ``subdirectory`` reach the clone from the same cached row, and
      :func:`_apply_configured_branch` forces the configured branch only onto
      **same-repo** entries — an owner-tier registry's apps are cross-repo by
      definition, so their branch declaration survives from the cache. Matching
      the URL alone would leave a poisoned row free to keep the curated URL and
      swap the ref, or point ``subdirectory`` at another app's directory in the
      same repo, and have either cloned with credentials and its setup script
      run. So the fresh row must agree on name, URL, branch AND subdirectory.
    """
    registry_name = entry.get("_registry")
    if not registry_name:
        return False

    effective_url = _entry_git_url(entry)
    if not effective_url:
        return False

    registry_name = str(registry_name)
    # Resolve trust AND the fresh-index coordinates from ONE immutable snapshot.
    # config.json and the index cache are both agent-writable, so resolving the
    # tier from one load (a bare `_registry_trust_tier(name)`) and the fetch
    # coordinates from a second, independent `_effective_registries()` load left a
    # window: a row could be swapped between the two reads, the tier passing on row
    # A while the fresh fetch ran against row B's coordinates and B's index
    # confirmed — B getting owner credentials it was never granted. Loading the
    # snapshot once and reading both the tier and the repo/branch off the SAME row
    # object closes that window.
    snapshot = await asyncio.to_thread(_effective_registries)
    reg = None
    for candidate in snapshot:
        if _public_registry_name(candidate) == registry_name:
            reg = candidate
            break
    if reg is None:
        return False

    if await asyncio.to_thread(_registry_trust_tier_of, reg) != _TRUST_OWNER:
        return False

    try:
        fresh = await _fetch_external_registry_index(reg.repo, reg.branch)
    except Exception:
        logger.warning(
            "owner-tier confirmation failed for %r; keeping the credential-free posture",
            _strip_git_target_userinfo(registry_name),
            exc_info=True,
        )
        _sel_credential_decision(
            "install_from_registry_owner_tier",
            effective_url,
            granted=False,
            reason="index_unreadable",
        )
        return False
    if not fresh:
        logger.info(
            "owner-tier registry %r could not be re-read; keeping the credential-free posture",
            _strip_git_target_userinfo(registry_name),
        )
        _sel_credential_decision(
            "install_from_registry_owner_tier",
            effective_url,
            granted=False,
            reason="index_unavailable",
        )
        return False

    fresh_rows = [row for row in fresh if isinstance(row, dict)]
    # Normalise the fresh rows the same way a cached row was normalised, so the
    # comparison is like-for-like rather than a branch-override artefact.
    _apply_configured_branch(fresh_rows, reg)

    wanted = _install_coordinates(entry)
    for row in fresh_rows:
        if _install_coordinates(row) == wanted:
            return True

    logger.warning(
        "owner-tier registry %r does not currently list app %r at %s (branch %r, subdir %r) — "
        "refusing the credential escalation",
        _strip_git_target_userinfo(registry_name),
        wanted[0],
        _redact_url_userinfo(wanted[1]),
        wanted[2],
        wanted[3],
    )
    # The load-bearing audit record: the local row claimed coordinates the
    # registry's own current index does not list, which is what a poisoned cache
    # looks like from here.
    _sel_credential_decision(
        "install_from_registry_owner_tier",
        wanted[1],
        granted=False,
        reason="coordinates_not_in_fresh_index",
    )
    return False


async def _fetch_external_registry_index(
    repo: str,
    branch: str,
) -> list[dict[str, Any]] | None:
    """Fetch app-registry.json from an external repo via a shallow git clone.

    *repo* is a git-cloneable URL (https/ssh/git/scp-style).  The repo is
    shallow-cloned into a throwaway temp directory.  If it contains an
    ``app-registry.json`` index, that is parsed and returned.  Otherwise the
    clone is scanned for ``apps/*/app.json`` and a synthetic index is built.

    Returns None on any failure (unreachable repo, invalid input, etc.) so a
    misconfigured external registry never crashes the listing path.

    Security controls:
    - Input validation: branch is regex-validated; only cloneable URLs accepted.
    - OS-level sandbox: wrap_argv with a trusted-host-gated mode
      (_clone_sandbox_mode). An SSH/scp remote on a well-known public forge or a
      user-configured registry host clones in "standard" mode (~/.ssh exposed so
      git can offer the owner's keys); any other remote stays "strict" (~/.ssh
      hidden) so a typo'd/hostile host is never offered the owner's SSH keys.
      https remotes never need ~/.ssh and always stay strict. Both modes unshare
      the user/mount namespaces and hide sensitive config dirs (.gnupg,
      .config/gcloud, ...).
    - Timeout + kill: _communicate_with_timeout() kills on timeout.
    - Read-only: only ``git clone`` (no write operations to the remote).
    - SEL audit (best-effort): start/outcome events logged when SEL is present.
    """
    # Input validation — reject values that could be used for command injection.
    if not _looks_like_git_url(repo):
        logger.warning("Rejecting non-cloneable external registry repo")
        return None
    if not _SAFE_BRANCH_RE.match(branch) or ".." in branch:
        logger.warning("Rejecting invalid branch name: %r", branch)
        return None

    if _git_target_is_unsupported(repo):
        logger.warning("external registry fetch refused an unsupported clone target")
        return None
    credential_target = repo
    git_url = _strip_git_target_userinfo(credential_target)
    credentialed_transport = credential_target != git_url

    # SEL audit: log external subprocess invocation for traceability (best-effort).
    def _sel_outcome(outcome: str) -> None:
        if _sel_fn is None:
            return
        try:
            _sel_fn().log_api_access(
                caller="registry",
                operation="fetch_external_registry",
                outcome=outcome,
                resources=(f"repo={_strip_git_target_userinfo(repo)} branch={branch}"),
            )
        except Exception as exc:
            logger.debug("SEL audit log failed for fetch_external_registry: %s", exc)

    _sel_outcome("started")

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-registry-")
        clone_path = Path(tmp_root)
        if credentialed_transport:
            # See `_git_fetch_branch`: a combined credentialed clone would let
            # checkout-time filters inherit the one-shot credential mapping.
            clone_path /= "branch"
            err = await _git_fetch_branch(
                git_url,
                branch,
                clone_path,
                [],
                credential_target=credential_target,
                clone_env=minimal_env(),
                sandbox_mode=_context_clone_sandbox_mode(git_url),
            )
            if err is not None:
                _sel_outcome("failed")
                return None
        else:
            clone_cmd = [
                "git",
                *_HOOKS_NEUTRALIZER_ARGV,
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                "--single-branch",
                git_url,
                tmp_root,
            ]
            sandboxed_cmd, _ = await wrap_argv_async(
                clone_cmd, mode=_context_clone_sandbox_mode(git_url), _prepare=wrap_argv
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=minimal_env(),
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            _, _ = await _communicate_with_timeout(proc, timeout=_CLONE_TIMEOUT)
            if proc.returncode != 0:
                _sel_outcome("failed")
                return None

        # Prefer an explicit app-registry.json index.
        index_path = clone_path / "app-registry.json"
        if index_path.is_file():
            try:
                data = json.loads(await asyncio.to_thread(index_path.read_text, "utf-8"))
                if isinstance(data, list):
                    # Keep only well-formed object entries — a malformed index
                    # item (e.g. a bare string) must never reach normalization.
                    _sel_outcome("success")
                    return _credential_free_external_registry_entries(
                        [item for item in data if isinstance(item, dict)]
                    )
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass

        # Fallback: scan for apps/*/app.json
        entries: list[dict[str, Any]] = []
        apps_dir = clone_path / "apps"
        if apps_dir.is_dir():
            for app_dir in sorted(apps_dir.iterdir()):
                if not app_dir.is_dir():
                    continue
                if not (app_dir / "app.json").is_file():
                    continue
                app_name = app_dir.name
                if not app_name or app_name in (".", ".."):
                    continue
                entries.append(
                    {
                        "name": app_name,
                        "repo": repo,
                        "branch": branch,
                        "subdirectory": f"apps/{app_name}",
                    }
                )
        result = entries if entries else None
        _sel_outcome("success" if result else "failed")
        return _credential_free_external_registry_entries(result) if result else None

    except (asyncio.TimeoutError, OSError):
        logger.debug("Failed to fetch external registry")
        _sel_outcome("failed")
        return None
    finally:
        if tmp_root:
            await _rmtree_force_settled(tmp_root)


def _apply_configured_branch(entries: list[dict[str, Any]], reg, *, warn: bool = False) -> None:
    """Force the operator-configured registry branch onto same-repo entries.

    The registry index is cloned and parsed from exactly ``reg.branch``, so a
    same-repo entry declaring a different branch describes a state that does
    not exist on the ref the operator asked for (e.g. a pre-merge entry
    declaring ``main``); honouring it makes install clone a ref where the
    app's subdirectory is missing. The declared value is index-controlled
    (untrusted) content, while ``reg.branch`` already passed the branch regex
    gate before the fetch, so the override also narrows what a registry index
    can make the installer clone.

    A cross-repo entry — one whose effective clone URL differs from the
    configured registry repo — keeps its declaration: its branch names a ref
    in ANOTHER repository, about which ``reg.branch`` carries no information.
    The comparison is byte-identical string equality, matching the
    owner-designated carve-out semantics (no normalization, no host-level
    matching). A cross-repo entry with no usable declared branch still
    inherits ``reg.branch`` (an explicit JSON ``null`` counts as absent, so
    ``None`` can never flow to the clone coordinates).

    Runs at fetch finalisation AND on every cache read that feeds a branch
    consumer, so a cache written before the registry's branch config changed
    (or by a version that honoured per-app declarations) cannot keep an
    overridden branch alive until the next refresh. ``warn`` is set only on
    the fetch path so a divergent declaration is logged once per refresh
    rather than on every lookup.
    """
    for entry in entries:
        declared_branch = entry.get("branch")
        if _same_git_target(_entry_git_url(entry), reg.repo):
            if warn and declared_branch is not None and declared_branch != reg.branch:
                logger.warning(
                    "External registry %s entry %r declares branch %r; using the "
                    "configured registry branch %r",
                    _public_registry_name(reg),
                    entry.get("name"),
                    declared_branch,
                    reg.branch,
                )
            entry["branch"] = reg.branch
        elif not declared_branch:
            entry["branch"] = reg.branch


async def _fetch_and_cache_external_registry(reg) -> list[dict[str, Any]] | None:
    """Fetch a registry's index, normalize entries, and write the cache.

    Returns the fresh entries on success (cache overwritten), or ``None`` on a
    fetch failure — in which case the caller decides whether to fall back to a
    stale cache. Because the cache is only overwritten on success, a transient
    forge/network failure leaves the prior (stale) cache intact ("stale >
    missing"): this is the fetch-then-swap contract the refresh path relies on.
    """
    # An unnamed legacy registry used its raw URL as the old cache identity,
    # which exposed HTTP userinfo in the filename. Remove that exact artifact
    # even when this is a fresh fetch with no preceding cache read — and the
    # pre-identity name-keyed caches with it (both filename forms), so a
    # credential-bearing artifact is reclaimed even when the fetch below
    # fails. No reader derives any of these paths any more.
    _remove_legacy_credential_registry_cache(reg.repo)
    _remove_legacy_name_keyed_registry_cache(reg)
    public_registry_repo = _strip_git_target_userinfo(reg.repo)
    name = _public_registry_name(reg)
    entries = await _fetch_external_registry_index(reg.repo, reg.branch)
    if entries is None:
        return None
    # Defensively drop malformed (non-dict) index items before normalization:
    # a configured repo can return a valid JSON array containing a non-object
    # (e.g. ``["oops"]``), and ``entry.setdefault(...)`` on a str would raise
    # AttributeError — which, on the refresh path, escapes as an HTTP 500.
    entries = _credential_free_external_registry_entries(
        [e for e in entries if isinstance(e, dict)]
    )
    # Path-safety gate: an external registry index is untrusted input, so every
    # entry passes the name and subdirectory gates BEFORE it is cached or listed.
    # The gates live in the facade (see ``registry._admit_fetched_index_entries``),
    # reached at call time.
    admitted: list[dict[str, Any]] = _facade()._admit_fetched_index_entries(entries, name)
    entries = admitted
    # Ensure each entry has gitUrl/repo set (for install_from_registry), then
    # apply the operator-configured branch policy (see _apply_configured_branch:
    # same-repo entries get reg.branch forced with a divergence warning;
    # cross-repo entries keep their declaration).
    for entry in entries:
        entry.setdefault("gitUrl", public_registry_repo)
        entry.setdefault("repo", public_registry_repo)
        entry["_registry"] = name
    _apply_configured_branch(entries, reg, warn=True)
    await asyncio.to_thread(
        _write_external_registry_cache, _external_registry_cache_identity(reg), entries
    )
    return entries


async def _load_external_registries() -> list[dict[str, Any]]:
    """Load app entries from all configured external registries.

    Reads the ``registries`` config field and fetches each repo's index.
    Results are cached for 1 hour. Each entry is tagged with its registry
    source for UI grouping.
    """
    registries = await asyncio.to_thread(_effective_registries)
    if not registries:
        return []

    all_entries: list[dict[str, Any]] = []
    # Registries whose index was FRESHLY fetched in this load, with the in-memory
    # rows that fetch returned. Their store art is prewarmed AFTER the gather
    # below settles, not inside ``_load_one``: the prewarm's provenance gate reads
    # every sibling registry's cache, and the siblings are fetched concurrently,
    # so a prewarm run inside the per-registry load would find the siblings'
    # caches still unwritten on a first load and skip every row as ambiguous.
    # After the gather each fresh sibling has written its cache. The rows handed
    # over are still the fresh in-memory lists -- a cache hit never reaches this
    # list, so a poisoned cache row can never reach the prewarm.
    fresh: list[tuple[Any, list[dict[str, Any]]]] = []

    async def _load_one(reg) -> list[dict[str, Any]]:
        cache_name = _external_registry_cache_identity(reg)
        public_name = _public_registry_name(reg)

        # Try cache first
        cached = await asyncio.to_thread(_read_external_registry_cache, cache_name)
        if cached is not None:
            for entry in cached:
                entry["_registry"] = public_name
            # Repair caches written before a branch-config change (or by a
            # version that honoured per-app declarations) — see helper.
            _apply_configured_branch(cached, reg)
            return cached

        # Fetch from repo (writes the cache on success).
        entries = await _fetch_and_cache_external_registry(reg)
        if entries is not None:
            fresh.append((reg, entries))
            return entries

        # Fall back to stale cache (stale > missing)
        stale = await asyncio.to_thread(
            _read_external_registry_cache,
            cache_name,
            ignore_ttl=True,
        )
        if stale is not None:
            for entry in stale:
                entry["_registry"] = public_name
            _apply_configured_branch(stale, reg)
            return stale
        logger.warning(
            "Failed to load external registry %s from %s",
            public_name,
            _strip_git_target_userinfo(reg.repo),
        )
        return []

    results = await asyncio.gather(
        *[_load_one(reg) for reg in registries],
        return_exceptions=True,
    )
    for reg, result in zip(registries, results, strict=True):
        if isinstance(result, list):
            all_entries.extend(result)
        elif isinstance(result, Exception):
            logger.warning(
                "External registry load failed: %s",
                _loggable_git_transport_output(
                    str(result),
                    credentialed=_strip_git_target_userinfo(reg.repo) != reg.repo,
                ),
            )

    if fresh:
        # One bounded batch per fresh owner-tier registry, run together so the
        # listing waits at most one batch budget (plus its cancellation cleanup)
        # for art, however many registries were fresh. A failing prewarm is
        # logged and never fails the listing: the rows are already in hand.
        outcomes = await asyncio.gather(
            *[_prewarm_owner_tier_store_assets(reg, entries) for reg, entries in fresh],
            return_exceptions=True,
        )
        for (reg, _entries), outcome in zip(fresh, outcomes, strict=True):
            if isinstance(outcome, Exception):
                logger.warning(
                    "store art prewarm for registry %s failed: %s",
                    _public_registry_name(reg),
                    _loggable_git_transport_output(
                        str(outcome),
                        credentialed=_strip_git_target_userinfo(reg.repo) != reg.repo,
                    ),
                )

    return all_entries
