"""An app's ``app.json`` for the store: where it is read from, fetched and merged.

Entry subdirectories are gated lexically and joined with a symlink-resolving
containment check. The manifest comes from the verified persistent checkout or a
throwaway clone, under the credential posture the entry earns. The merge projects
index rows through ``_REGISTRY_ROW_KEYS`` and rewrites store art through the blob
proxy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.caches import _read_manifest_cache, _write_manifest_cache
from kiro_crew.apps.registry_pipeline.checkout import (
    _CLONE_TIMEOUT,
    _clone_branch_matches,
    _clone_origin_matches,
    _communicate_with_timeout,
    _git_fetch_branch,
    _git_fetch_commit,
    _rmtree_force_settled,
)
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _git_target_is_unsupported,
    _git_transport_env,
    _loggable_git_transport_output,
    _looks_like_git_url,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.recovery import app_source_dir
from kiro_crew.apps.registry_pipeline.sources import (
    _context_clone_sandbox_mode,
    _owner_designated_repo_target,
    _sel_credential_grant,
    is_clone_host_trusted,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import anonymous_git_env, minimal_env
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

logger = logging.getLogger(_FACADE)


def _is_safe_registry_subdir(subdir: Any) -> bool:
    """True if *subdir* is a safe, contained relative path for a registry entry.

    An external registry index is untrusted and controls the entire entry,
    including ``subdirectory`` — which is later joined to the throwaway clone
    dir, the persistent app-source dir, and the manifest read path. An absolute
    or ``..`` value would escape those roots and let an attacker-selected
    ``app.json`` (→ ``setup.onInstall``) be read/executed. Empty/missing means
    the repo root (safe). Rejects non-strings, NUL, backslashes (Windows/UNC
    separators), absolute paths (POSIX ``/…`` or drive-letter ``C:…``), and any
    ``.``/``..`` path segment. Purely lexical; the use-site
    :func:`_contained_join` adds a symlink-resolving containment check as
    defense-in-depth.
    """
    if subdir in (None, ""):
        return True
    if not isinstance(subdir, str):
        return False
    if "\x00" in subdir or "\\" in subdir:
        return False
    if subdir.startswith("/") or (len(subdir) >= 2 and subdir[1] == ":"):
        return False
    return not any(seg in ("..", ".") for seg in subdir.split("/"))


def _contained_join(root: Path, subdir: str) -> Path | None:
    """Join *subdir* under *root*, returning the symlink-resolved result only if
    it stays within *root*; ``None`` on any escape.

    Defense-in-depth companion to :func:`_is_safe_registry_subdir`: the lexical
    gate rejects ``..``/absolute values before an entry is cached/listed, and
    this resolves symlinks so a hostile clone containing e.g. ``sub -> /etc``
    cannot smuggle a read outside the clone root at use time. Returns *root*
    unchanged for an empty *subdir*.
    """
    if not subdir:
        return root
    try:
        base = root.resolve()
        target = (root / subdir).resolve()
    except (OSError, RuntimeError):
        # What non-strict ``Path.resolve`` raises: ``OSError`` for a path it
        # cannot walk, ``RuntimeError`` for a symlink loop (POSIX re-raises ELOOP
        # as one). A loop is an escape that resolves nowhere, so it fails closed
        # like every other escape -- the callers that re-check containment after
        # a third-party script wrote to the checkout depend on this returning
        # rather than raising.
        return None
    if target.is_relative_to(base):
        # ``target`` is textually contained, but on Windows a self-pointing
        # reparse point (``pkg -> pkg``) is collapsed LEXICALLY by non-strict
        # ``resolve`` -- it never walks the link, so a loop slips through here as
        # a contained-looking path that a caller would then read/write THROUGH.
        # POSIX already raised above; Windows does not, so re-resolve strictly to
        # force the OS to walk the target. The distinction that matters:
        #   - ``FileNotFoundError`` -- the path simply does not exist. That is a
        #     legitimate state some callers rely on (the rollback path re-checks
        #     containment of ``app.json`` after it has been removed, and needs a
        #     contained path back so the restore proceeds), so preserve the
        #     pre-existing contract of returning the contained path; every caller
        #     does its own existence check downstream.
        #   - any OTHER resolution error -- a loop, a component that is not a
        #     directory, a permission wall -- is a path that does not truly
        #     resolve, so fail closed. A self-pointing loop is exactly this case:
        #     the link exists, so it is not FileNotFoundError, and walking it
        #     raises on both platforms.
        try:
            target.resolve(strict=True)
        except FileNotFoundError:
            return target
        except (OSError, RuntimeError):
            return None
        return target
    return None


async def _fetch_app_manifest(
    repo: str,
    branch: str,
    subdirectory: str = "",
    app_name: str = "",
    git_url: str = "",
    *,
    owner_designated: bool = False,
    commit: str = "",
) -> dict[str, Any] | None:
    """Fetch app.json for an app from its source repo (lightweight).

    Tries, in order:
      1. The persistent clone under ``~/.kiro/crew/app-sources/{app_name}/``
         (if the app was already cloned by a previous install).
      2. A throwaway shallow clone of *git_url* into a temp directory, from
         which only ``app.json`` is read (the clone is then discarded).

    Returns the parsed app.json dict, or None on failure.  All failures are
    swallowed (returns None) so a missing/unreachable repo never crashes the
    listing path on a vanilla machine. *subdirectory* is an untrusted
    index-controlled value; it is joined via :func:`_contained_join` so an
    absolute/``..``/symlink value can never read outside the clone root.

    *owner_designated*: when True (same-repo credential carve-out), the
    clone uses ``minimal_env()`` + context sandbox mode instead of the
    default anonymous+strict posture. Only set when the entry's effective
    clone URL is byte-identical to the owner-configured registry repo URL.

    *commit*: a pinned commit. When set, the manifest is read from THAT tree
    rather than a branch tip, and the local fast path compares commits instead of
    branch names. This matters on the install path specifically: this manifest is
    what the admission gate inspects, so reading it from a branch tip while the
    install fetches a pinned commit would gate one tree and install another --
    and a pinned row carries no branch at all, so the branch would silently be
    the ``"main"`` default.
    """
    credential_target = git_url or repo
    if _git_target_is_unsupported(credential_target):
        logger.warning("registry manifest fetch refused an unsupported clone target")
        return None
    git_url = _strip_git_target_userinfo(credential_target)
    credentialed_transport = credential_target != git_url

    # Try persistent clone first (already installed).
    #
    # The persisted clone is keyed on app NAME only, so a registry replacement
    # can leave a checkout of a DIFFERENT repo sitting here under the same
    # name. Its app.json must not stand in for the manifest of the repo we are
    # about to clone: the caller feeds this manifest to the admission gate, and
    # the install that follows discards a stale checkout and re-clones from
    # *git_url* (see _git_clone_or_pull). Trusting the stale copy would admit
    # repo A's manifest and then run repo B's code. So the local copy is only
    # used when the clone's origin still is git_url; otherwise fall through to
    # the throwaway clone of git_url, which always describes what gets cloned.
    if app_name and not commit:
        # A PINNED entry gets no local fast path at all.
        #
        # The persistent checkout is agent-writable, and `app.json` there can be
        # edited without HEAD moving -- so a commit comparison attests where the tree
        # was placed, never what it now holds. This manifest is what the admission and
        # platform gates read, so a local edit bypasses `installMode`/`os`
        # restrictions and gets the tree built server-side. It is the same reason a
        # pinned install never reuses an existing checkout; the rule belongs here too.
        #
        # The cost is a shallow single-commit fetch per pinned listing, which the
        # pinned branch below already performs.
        clone_dir = app_source_dir(app_name)
        manifest_dir = _contained_join(clone_dir, subdirectory)
        local_manifest = manifest_dir / "app.json" if manifest_dir is not None else None
        fresh_enough = await _clone_branch_matches(clone_dir, branch)
        if (
            local_manifest is not None
            and local_manifest.is_file()
            and await _clone_origin_matches(clone_dir, git_url)
            and fresh_enough
        ):
            try:
                content = await asyncio.to_thread(local_manifest.read_text, "utf-8")
                return json.loads(content)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass

    if not _looks_like_git_url(git_url):
        # Not a cloneable URL (e.g. empty or a bare name on a public machine).
        return None
    # SSRF gate: only clone from explicitly-trusted hosts. An untrusted external
    # registry index can list an app repo pointing at an internal address; this
    # listing path clones automatically, so it must not honor such a host.
    # is_clone_host_trusted() loads config from disk (KiroCrewConfig.load), so
    # run it off the event loop to avoid blocking all gateway tasks.
    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        logger.debug(
            "manifest clone refused for %r: host not in trusted forge/registry set (SSRF gate)",
            _strip_git_target_userinfo(git_url),
        )
        return None

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-manifest-")
        # Credential posture for the manifest fetch. Default: anonymous+strict
        # (confused-deputy defense — see anonymous_git_env). Same-repo
        # carve-out: when owner_designated is True the clone URL is the
        # owner-configured registry repo itself, so the confused-deputy
        # argument does not apply — use owner credentials + context sandbox.
        if owner_designated:
            clone_env = minimal_env()
            sandbox_mode = _context_clone_sandbox_mode(git_url)
            _sel_credential_grant("fetch_app_manifest", git_url)
        else:
            clone_env = anonymous_git_env()
            sandbox_mode = "strict"

        if commit:
            # Read the manifest from the pinned tree. `--branch` cannot take a
            # commit id, and on the install path this manifest is what the
            # admission gate inspects -- gating a branch tip while installing a
            # pinned commit would check one tree and install another.
            fetch_log: list[str] = []
            # A NONEXISTENT child, not `tmp_root` itself. `tmp_root` already exists
            # (TemporaryDirectory created it), and `_git_fetch_commit` refuses a
            # destination that exists but is not a checkout -- the guard that stops it
            # from adopting, and later deleting, a directory it did not create. Handing
            # it `tmp_root` made every pinned manifest fetch fail, which left the
            # admission and platform-compatibility gates with no manifest at all.
            fetch_dest = Path(tmp_root) / "pinned"
            checkout_root = fetch_dest
            err = await _git_fetch_commit(
                git_url,
                commit,
                fetch_dest,
                fetch_log,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if err is not None:
                logger.debug(
                    "manifest fetch of pinned commit failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    str(err),
                )
                return None
        elif credentialed_transport:
            # A credentialed `git clone` performs both the network fetch and the
            # checkout in one process. The checkout may launch an inherited filter
            # selected by the fetched `.gitattributes`, which would inherit the
            # one-shot URL rewrite. Split the operations and give only fetch the
            # credential-bearing environment.
            fetch_log = []
            fetch_dest = Path(tmp_root) / "branch"
            checkout_root = fetch_dest
            err = await _git_fetch_branch(
                git_url,
                branch,
                fetch_dest,
                fetch_log,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=sandbox_mode,
            )
            if err is not None:
                logger.debug(
                    "manifest fetch of branch failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    str(err),
                )
                return None
        else:
            clone_cmd = [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                "--single-branch",
                git_url,
                tmp_root,
            ]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                clone_cmd, mode=sandbox_mode, _prepare=wrap_argv
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            transport_env = _git_transport_env(credential_target, git_url, clone_env)
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=transport_env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            _, stderr = await _communicate_with_timeout(proc, timeout=_CLONE_TIMEOUT)
            if proc.returncode != 0:
                logger.debug(
                    "manifest clone failed for %s: %s",
                    _strip_git_target_userinfo(git_url),
                    _loggable_git_transport_output(
                        stderr.decode(errors="replace").strip(),
                        credentialed=credentialed_transport,
                    ),
                )
                return None
            checkout_root = Path(tmp_root)
        # Containment is measured from the root the tree actually landed in, which
        # differs by branch: the pinned fetch uses a child of `tmp_root` so it gets a
        # destination it created, the branch clone uses `tmp_root` itself.
        manifest_dir = _contained_join(checkout_root, subdirectory)
        if manifest_dir is None:
            # Untrusted index subdirectory escaped the clone root (absolute,
            # ``..``, or a symlink resolving outside tmp_root) — refuse.
            return None
        manifest_path = manifest_dir / "app.json"
        if not manifest_path.is_file():
            return None
        content = await asyncio.to_thread(manifest_path.read_text, "utf-8")
        return json.loads(content)
    except (asyncio.TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug(
            "Failed to fetch app.json from %s: %s",
            _strip_git_target_userinfo(git_url),
            _loggable_git_transport_output(str(exc), credentialed=credentialed_transport),
        )
        return None
    finally:
        if tmp_root:
            await _rmtree_force_settled(tmp_root)


async def _resolve_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    """Merge registry entry with its remote app.json manifest.

    Returns the entry enriched with display fields from app.json.
    Registry fields (name, repo, branch, managed, detectInstalled) take
    precedence; everything else comes from app.json.
    """
    name = entry.get("name", "")
    repo = entry.get("repo", "")
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")
    git_url = _entry_git_url(entry)

    if not git_url:
        return entry

    # Try cache first — keyed on the entry's full source coordinates, so a
    # row configured for another branch/repo/subdirectory can never answer.
    cached = await asyncio.to_thread(_read_manifest_cache, entry)
    if cached:
        return _merge_manifest(entry, cached)

    # Fetch from repo
    # Same-repo credential carve-out: if the entry's clone URL matches the
    # owner-configured registry repo, use owner credentials for the manifest
    # fetch (the confused-deputy defense does not apply to the owner's own URL).
    owner_target = await asyncio.to_thread(_owner_designated_repo_target, entry)
    manifest = await _fetch_app_manifest(
        repo,
        branch,
        subdirectory,
        app_name=name,
        git_url=owner_target or git_url,
        owner_designated=bool(owner_target),
    )
    if manifest:
        await asyncio.to_thread(_write_manifest_cache, entry, manifest)
        return _merge_manifest(entry, manifest)

    # No manifest available — return entry as-is (minimal info). The failed
    # fetch attaches NOTHING: the source-scoped cache read above already
    # missed, and there is deliberately no name-only fallback that could
    # attach a manifest cached for another branch or repository.
    logger.info("Could not fetch app.json for %s — showing minimal info", name)
    return entry


#: Keys a registry index row may contribute to a merged app-store row.
#
# An index row is UNTRUSTED content — an external registry's index is
# user-supplied JSON — so the merge projects these names explicitly instead of
# spreading the row. Spreading shipped every key an index chose to invent
# straight to the browser, which both grew the payload with fields no consumer
# reads and gave an index a channel for keys the client never validated.
#
# Each name here has a reader: ``name`` is identity; ``gitUrl`` / ``repo`` /
# ``branch`` / ``subdirectory`` are the clone coordinates
# (``_entry_git_url``, ``install_from_registry``); ``resources`` selects the
# self-managed install path; ``detectInstalled`` is the pre-install probe;
# ``managed`` is the legacy registry-only flag; ``featured`` is the Discover
# spotlight flag (kept only on non-external rows by ``_apply_trust_fields``);
# ``_registry`` is the server-attached source tag; ``_index_author`` is the
# author snapshot ``_apply_trust_fields`` consumes for the verified mark.
#
# Display fields are deliberately ABSENT: they come from the fetched
# ``app.json`` below, so an index cannot publish display copy for an app whose
# manifest says otherwise. Install-status and trust fields are also absent —
# ``_enrich_with_install_status`` and ``_apply_trust_fields`` run after this
# and stamp them server-side.

_REGISTRY_ROW_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "gitUrl",
        "repo",
        "branch",
        # `_catalog` must survive the merge or the row goes back through the
        # per-app manifest clone it exists to make unnecessary.
        #
        # `commit` survives for data fidelity only, NOT as an authorization: this
        # projection also builds rows from an external registry's index, so the
        # value here is index-controlled. `install_from_registry` reads the pin only
        # for `_is_catalog_row`, which no index row can satisfy.
        "commit",
        "_catalog",
        "subdirectory",
        "resources",
        "detectInstalled",
        "managed",
        "featured",
        # GitHub star count baked into the row by the publisher (git-type
        # third-party apps only). Reader: the frontend App Store list/detail
        # display. Display-only — ``_apply_trust_fields`` sanitizes it to a
        # non-negative int on EVERY row (the allowlist is not the only exit:
        # a failed manifest fetch passes the row through unchanged).
        "stargazersCount",
        "_registry",
        "_index_author",
    }
)


def _store_asset_path(subdirectory: Any, asset_path: Any) -> Any:
    """Repo-root-relative path of a store-card asset declared in ``app.json``.

    The manifest is read from ``_contained_join(clone_dir, subdirectory)``, so
    every art path it declares (``iconPath``, ``heroImage*``, ``screenshots*``)
    is relative to that directory -- while ``/api/apps/blob`` resolves ``path``
    against the repo root. This is the store-card reader's join; the field
    itself keeps its meaning, because the installed-app reader
    (``handle_app_art_file``) resolves the same value against the install
    directory, where the subdirectory has already been stripped by the install.

    Containment is preserved rather than re-derived: a ``subdirectory`` the
    lexical gate :func:`_is_safe_registry_subdir` rejects (absolute, ``..``,
    backslash) is NOT joined, so the join never manufactures a traversing path
    -- such entries are dropped before listing anyway, and the bare path here
    is exactly what the store built before. Empty or ``.`` means the repo root
    (unchanged), an absolute path or URL is left untouched, and the join is a
    plain posix join with no normalisation, so a ``..`` inside the asset path
    still reaches the blob route's own rejection unchanged.
    """
    if not asset_path or not isinstance(asset_path, str) or not isinstance(subdirectory, str):
        return asset_path
    subdir = subdirectory.rstrip("/")
    if subdir in ("", "."):
        return asset_path
    if not _is_safe_registry_subdir(subdir):
        return asset_path
    if asset_path.startswith("/") or "://" in asset_path:
        return asset_path
    return posixpath.join(subdir, asset_path)


def _merge_manifest(entry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Merge app.json fields into a registry entry.

    Registry-only fields (``_REGISTRY_ROW_KEYS``) are preserved from the entry.
    Everything else comes from app.json, with the blob proxy URL pattern
    applied to image paths -- each joined under the entry's ``subdirectory``
    first (:func:`_store_asset_path`), the directory the manifest was read from.
    """
    raw_repo = entry.get("repo", "")
    repo = _strip_git_target_userinfo(raw_repo) if isinstance(raw_repo, str) else ""
    result = {k: v for k, v in entry.items() if k in _REGISTRY_ROW_KEYS}
    if isinstance(result.get("repo"), str):
        result["repo"] = _strip_git_target_userinfo(result["repo"])
    subdirectory = entry.get("subdirectory", "")

    def _blob_url(asset_path: str) -> str:
        return f"/api/apps/blob?repo={repo}&path={_store_asset_path(subdirectory, asset_path)}"

    # Top-level display fields from app.json
    for key in (
        "displayName",
        "description",
        "version",
        "author",
        "tags",
        "highlights",
        "useCases",
        "configuration",
        "license",
        "minKiroCrewVersion",
    ):
        if key in manifest:
            result[key] = manifest[key]

    # Runtime fields go under "manifest" — matches the installed app
    # data structure so the frontend can always read app.manifest.*
    manifest_fields: dict[str, Any] = {}
    for key in (
        "agents",
        "skills",
        "crons",
        "mcpServers",
        "permissions",
        "setup",
        "ui",
        "openCommand",
    ):
        if key in manifest:
            manifest_fields[key] = manifest[key]
    if manifest_fields:
        result["manifest"] = manifest_fields

    # Platform config from app.json
    if "platform" in manifest:
        result["platform"] = manifest["platform"]

    # Icon — convert a manifest-relative path to a blob proxy URL, joined under
    # the entry's ``subdirectory`` (the directory app.json was read from) so the
    # blob path names the file where it actually lives in the repo.
    #
    # Only ``iconPath`` (repo-relative) is honoured, never a manifest-declared
    # ``iconUrl``: an index-fetched manifest is untrusted content, and copying an
    # absolute URL out of it would let a third party point the store's <img> at
    # any host it likes. Rewriting a repo-relative path keeps every icon fetch
    # on our own proxy, which enforces the extension allowlist and the
    # trusted-host gate.
    icon_path = manifest.get("iconPath", "")
    if icon_path and repo:
        result["iconUrl"] = _blob_url(icon_path)
    # Dark-appearance variant. Raster icons have fixed bytes, so an app that
    # must read well on both backgrounds ships two files; first-party
    # ``/app-assets/`` SVGs are inlined and repaint from theme tokens instead.
    icon_path_dark = manifest.get("iconPathDark", "")
    if icon_path_dark and repo:
        result["iconUrlDark"] = _blob_url(icon_path_dark)
    # Lucide fallback icon from manifest extra fields
    if manifest.get("icon"):
        result["icon"] = manifest["icon"]

    # Screenshots — convert repo-relative paths to blob proxy URLs
    screenshots = manifest.get("screenshots", [])
    if screenshots and repo:
        result["screenshots"] = [_blob_url(p) for p in screenshots]

    # Screenshots dark — convert repo-relative paths to blob proxy URLs
    screenshots_dark = manifest.get("screenshotsDark", [])
    if screenshots_dark and repo:
        result["screenshotsDark"] = [_blob_url(p) for p in screenshots_dark]

    # Hero images — convert repo-relative paths to blob proxy URLs
    hero = manifest.get("heroImage", "")
    if hero and repo:
        result["heroImage"] = _blob_url(hero)
    hero_dark = manifest.get("heroImageDark", "")
    if hero_dark and repo:
        result["heroImageDark"] = _blob_url(hero_dark)
    # Detail-page hero images (wide banner ratio) — convert repo-relative paths
    # to blob proxy URLs. The detail page prefers these over the (near-square)
    # Browse-card hero so the wide banner isn't cropped.
    hero_detail = manifest.get("heroImageDetail", "")
    if hero_detail and repo:
        result["heroImageDetail"] = _blob_url(hero_detail)
    hero_detail_dark = manifest.get("heroImageDetailDark", "")
    if hero_detail_dark and repo:
        result["heroImageDetailDark"] = _blob_url(hero_detail_dark)

    return result
