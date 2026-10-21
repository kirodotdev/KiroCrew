"""Serving an app's art: the installed-art route and the git blob proxy.

``handle_app_art_file`` serves the manifest-declared art ``_declared_art_paths`` and
``_read_declared_art`` read. ``handle_blob_proxy`` and the ``_fetch_git_blob`` clone behind
it serve registry art, with the repository and ref validators (``_is_safe_repo_identifier``,
``_SAFE_REF_RE``), the fetch timeout and concurrency bound, and the blob-cache touch.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import stat
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.apps.manager import apps_dir, get_app_manifest
from kiro_crew.apps.registry import (
    _ART_IMAGE_EXTENSIONS,
    _ART_MANIFEST_FIELDS,
    _ART_MANIFEST_LIST_FIELDS,
    _ART_MAX_BYTES,
    _SAFE_PATH_RE,
    _blob_cache_dir,
    _blob_cache_key,
    _context_clone_sandbox_mode,
    _entry_git_url,
    _git_fetch_branch,
    _git_target_is_unsupported,
    _loggable_git_transport_output,
    _owner_designated_repo_target,
    _repo_key_owner_count,
    _sel_credential_grant,
    _strip_git_target_userinfo,
    anonymous_git_env,
    get_registry_app_by_repo,
    known_registry_repos,
    minimal_env,
)
from kiro_crew.apps.registry_pipeline.checkout import _HOOKS_NEUTRALIZER_ARGV
from kiro_crew.apps.routes_static import _CONTENT_TYPES
from kiro_crew.dashboard.conditional_get import conditional_response
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    is_reparse_point,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")

#: Store-art field names, image extension allowlist and size ceiling are ONE set
#: for all three art readers -- the installed-app route and the blob proxy in this module, and
#: the registry's owner-tier prewarm (``registry_pipeline.store_art``, which owns
#: them). The parity is load-bearing: a file one reader serves and another refuses
#: means the same app's art renders or 403s depending only on whether it happens to
#: be installed, or on which path its bytes arrived through.
#:
#: The field tuple is deliberately NOT a path filter rooted at the install
#: directory. That directory is the app's whole checkout and ``_ALLOWED_EXTENSIONS``
#: admits ``.json``, so a filter would also serve ``installed.json``, ``app.json``
#: and every other JSON in the tree -- a widening nobody asked for to display an icon.


def _declared_art_paths(name: str) -> set[str]:
    """The art paths *name*'s own installed manifest declares.

    Blocking (reads the manifest off disk) — callers must offload it.
    """
    manifest = get_app_manifest(name)
    if manifest is None:
        return set()
    extra = getattr(manifest, "extra", None)
    fields: dict[str, Any] = extra if isinstance(extra, dict) else {}
    declared: set[str] = set()
    for key in _ART_MANIFEST_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            declared.add(value[2:] if value.startswith("./") else value)
    for key in _ART_MANIFEST_LIST_FIELDS:
        values = fields.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value:
                declared.add(value[2:] if value.startswith("./") else value)
    return declared


def _read_declared_art(name: str, file_path: str) -> tuple[bytes, str] | None:
    """The BYTES of *file_path* and a validator for them, or None to refuse.

    Returning bytes rather than a path is the security-relevant part. Validating a
    path and then handing it to ``FileResponse`` opens it a SECOND time, so the app
    that owns this directory can swap a declared name for a symlink between the
    check and that open and have the gateway read the target instead — and the
    gateway is NOT sandboxed, so this would launder a read the app's own code can be
    refused. Checking a path and acting on a re-resolution of it is worse than no
    check, because it reports success.

    One open, validated as a DESCRIPTOR, is the only shape without that window.
    :func:`open_in_pinned_parent` does one ``openat`` per component carrying
    ``O_NOFOLLOW``, so a component swapped for a link after the parent was resolved
    fails instead of being followed, and the final name is refused if it is a link
    at all. Everything after that reads the descriptor, which cannot be re-pointed.

    One thread hop for the whole decision — manifest read, declaration check, open,
    stat and read — because every part is a blocking syscall and the gateway runs on
    one event loop (``no-blocking-call-on-event-loop``).
    """
    if file_path not in _declared_art_paths(name):
        return None
    root = apps_dir() / name
    target = root / file_path
    try:
        # Resolved ONCE, here, because `pin_parent` requires the CALLER to do it:
        # resolving inside would re-follow whatever an ancestor points at by then,
        # which is the mistake that makes a guard look defensible and do nothing.
        resolved_root = Path(os.path.realpath(root))
        resolved_parent = Path(os.path.realpath(target.parent))
        resolved_parent.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        # `RuntimeError` because `Path`/`realpath` resolution raises THAT -- not an
        # OSError -- on a symlink loop, and the app that plants one is the app whose
        # art this serves. All three mean the path is not servable, which is the same
        # answer as undeclared and missing.
        return None

    # `O_NONBLOCK` is not an optimisation, it is what makes the descriptor checks
    # REACHABLE. Opening a FIFO blocks until a writer appears, and this runs inside
    # `asyncio.to_thread` -- so an app declaring a FIFO as its icon path parks a
    # thread-pool worker forever, and enough such requests starve every other
    # blocking call in the gateway. Measured: the open hangs indefinitely without
    # this flag, returns immediately with it, and then `S_ISREG` below refuses the
    # FIFO. On an ordinary file it changes nothing -- the read returns the same bytes.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if supports_pinned_walk():
        try:
            fd = open_in_pinned_parent(
                str(resolved_parent),
                Path(file_path).name,
                flags=flags,
                mode=0o600,
                what="app art file",
            )
        except (PinnedPathRefusal, OSError, ValueError):
            # `ValueError` is NOT redundant with `OSError`: `os.open` raises it --
            # never an OSError -- for a name the OS layer cannot even encode. Two
            # reachable classes, both measured: an embedded NUL raises `ValueError`
            # ("embedded null character in path"), and a lone surrogate raises
            # `UnicodeEncodeError`, which is a ValueError SUBCLASS. Screening NUL at
            # the door would therefore be INCOMPLETE -- the surrogates carry no NUL.
            #
            # Reachable because such a name survives every earlier check: the
            # extension allowlist reads the suffix AFTER the bad byte
            # (`bad\x00.png` -> `.png`), and containment resolves the PARENT, which
            # is clean when the bad byte sits in the final component. So the first
            # thing that touches it is this open, and uncaught it is a 500 on a
            # route whose every other refusal is a clean status.
            return None
    else:
        # Windows: no `O_NOFOLLOW` and no descriptor-relative open, so the pinned
        # walk is unavailable. An unprivileged process there cannot create a FILE
        # symlink at all (that needs elevation, which is why `symlink_or_junction`
        # exists), so the reachable swap is a junction on an ancestor -- refused by
        # the reparse-point probe below. The window is narrowed rather than closed,
        # and it is narrowed against what the platform actually permits.
        try:
            if any(
                is_reparse_point(p)
                for p in (target, *target.parents)
                if p == resolved_root or resolved_root in p.parents or p == target
            ):
                return None
            fd = os.open(target, flags)
        except (OSError, ValueError):
            # Same unencodable-name classes as the pinned branch above: this open
            # takes the whole path rather than a name under a descriptor, so it is
            # reachable the same way and needs the same tuple.
            return None

    try:
        st = os.fstat(fd)
        # `st_nlink != 1` is the third gate, and it is the only one that can see a
        # HARDLINK. An alias shares the target's inode, so every path-based guard is
        # blind to it: `is_symlink()` is False, `realpath` yields the alias's own name
        # (so containment passes), and `O_NOFOLLOW` has no link to refuse. Measured:
        # a declared `assets/icon.webp` hardlinked to a file outside the install
        # directory opens cleanly, reports S_ISREG, sits under the size cap, and its
        # bytes are served with a 200 -- laundering, through an unsandboxed gateway, a
        # read the app's own sandboxed code can be refused.
        #
        # Checked on the DESCRIPTOR, which is what makes it race-free: this fd already
        # refers to the inode being judged. Every other descriptor-validated read in
        # the tree applies the same gate (`hooks.py`, `memory.py`, `spec_builder`,
        # `onboarding_scan.py`, `pinned_fs.copy_file_pinned`), so this route was the
        # outlier rather than a new rule.
        #
        # Inline rather than `pinned_fs.refuse_hardlink_alias`, which is the same
        # check behind an exception: that helper CLOSES the fd before raising, and this
        # function closes in a `finally`, so routing through it would double-close --
        # and a reused fd number makes that a worse bug than the one being fixed. The
        # inline spelling is what the sibling sites above use for the same reason:
        # their refusal is a return value, not a raise.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _ART_MAX_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(_ART_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _ART_MAX_BYTES:
        return None
    # Validator derived from the DESCRIPTOR we actually read, not from a second stat
    # of the path. `no-cache` means the browser revalidates every load, and the rail
    # renders on every load, so without a validator each one would be a full 200.
    validator = f'"{st.st_ino:x}-{st.st_size:x}-{st.st_mtime_ns:x}"'
    return data, validator


# ---------------------------------------------------------------------------
# Git blob proxy — serve images from a registry app's git repo
# ---------------------------------------------------------------------------


_BLOB_FETCH_TIMEOUT = 30  # seconds — shallow clone of a single-branch repo
_BLOB_FETCH_SEMAPHORE = asyncio.Semaphore(3)  # max 3 concurrent git fetches
# Bare-name repo identifier (legacy registry entries) — no scheme, no path.
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# https git URL: https://host[:port]/org/app[.git]. Host/path charset is
# restricted and shell metacharacters / traversal are rejected separately.
# Plaintext ``http://`` is deliberately NOT accepted: registry clones fetch an
# index + app manifests whose setup code later runs with gateway privileges
# (signatures are optional by default), so an unauthenticated transport would
# let a network (MITM) attacker swap in an attacker-controlled app. Require TLS
# for HTTP-style remotes; use an explicit ssh:// / scp form for private ones.
_SAFE_HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$")
# scp-style ssh remote: user@host:org/app[.git]
_SAFE_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+$")
# ssh:// URL form: ssh://[user@]host[:port]/org/app[.git]
# Userinfo is optional — userless ssh URLs (e.g. ssh://git.example.com/pkg/X) are
# a standard git form where ~/.ssh/config supplies the user.
_SAFE_SSH_URL_RE = re.compile(
    r"^ssh://(?:[A-Za-z0-9._\-]+@)?[A-Za-z0-9.\-]+(?::[0-9]+)?/[A-Za-z0-9._/\-]+$"
)
# `\Z`, not `$`: Python's `$` also matches immediately BEFORE a trailing newline, so with
# the `.match` calls in the blob handler a value like "main\n" passes -- and this feeds
# git argv. Same defect class as the catalog-side coordinate patterns; this is the blob
# handler's ref instance. (The class is wider than this file: other `$`-anchored
# request-path patterns exist elsewhere, e.g. papyrus's GIT_URL_RE.) The `path` grammar
# is `_SAFE_PATH_RE`, imported from the registry facade: the owner-tier prewarm applies
# the same object to a declared path before caching it, so the two cannot drift.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+\Z")


def _is_safe_repo_identifier(repo: str) -> bool:
    """Validate the blob-proxy ``repo`` query parameter.

    Registry entries are now full git URLs (``https://github.com/org/app``,
    ``git@host:org/app.git``), but legacy entries may still use a bare name.
    Accept either a bare token OR a vetted git URL — never an arbitrary string.

    Git URLs are validated against a restricted scheme/host/path charset and
    rejected outright if they contain shell metacharacters or ``..`` path
    traversal, so the value is safe to pass to ``git clone`` argv.
    """
    if not repo:
        return False
    # Reject shell metacharacters and traversal regardless of form.
    if ".." in repo or any(c in repo for c in " \t\n\r;|&$`<>()*?!\\\"'"):
        return False
    if _SAFE_REPO_RE.match(repo):
        return True
    if _SAFE_HTTPS_URL_RE.match(repo):
        return True
    if _SAFE_SCP_URL_RE.match(repo):
        return True
    if _SAFE_SSH_URL_RE.match(repo):
        return True
    return False


def _touch_served_blob(cache_path: Path) -> None:
    """Bump a served blob's mtime to now without touching a link's target.

    ``os.utime(follow_symlinks=False)`` where the platform supports it; elsewhere
    (Windows) the name must lstat as a plain regular file before it is touched.
    """
    if os.utime in os.supports_follow_symlinks:
        os.utime(cache_path, None, follow_symlinks=False)
        return
    if not stat.S_ISREG(os.lstat(cache_path).st_mode):
        return
    os.utime(cache_path, None)


async def handle_app_art_file(request: web.Request) -> web.Response:
    """GET /apps/{name}/art/{path:.*} — an installed app's own store art.

    The bytes of an installed app's icon, hero and screenshots are already on
    local disk, inside the directory the install created. Reaching them through
    ``/api/apps/blob`` instead means a git clone gated by an SSRF allowlist,
    which is why a catalog-listed app's art could 403 on a cold load: the
    allowlist is warmed by a network fetch that a page can outrun. Reading the
    file the gateway itself wrote has no such ordering, needs no network,
    survives a CDN outage, and adds no SSRF surface — the request never names a
    host.

    Mirrors :func:`handle_app_ui_file`'s shape (that route already serves an
    app's UI-bundle assets, and ``AppDetailPage`` already resolves a page icon
    through it) with two deliberate narrowings: images only, and the path must
    be one the app's manifest declares.
    """
    name = request.match_info["name"]
    file_path = request.match_info.get("path", "")
    # Cheap rejections first, before any syscall: these cannot be reached by a
    # declared path anyway, so answering here keeps a hostile request off the
    # thread pool entirely.
    if not file_path or ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path", "code": "art_path_invalid"}, status=400)
    ext = Path(file_path).suffix.lower()
    if ext not in _ART_IMAGE_EXTENSIONS:
        return web.json_response(
            {"error": f"file type {ext!r} not allowed", "code": "art_type_not_allowed"},
            status=403,
        )
    full_path = await asyncio.to_thread(_read_declared_art, name, file_path)
    if full_path is None:
        # One answer for "not declared", "outside the install dir", "not a plain
        # file", "over the size ceiling" and "missing", so a probe cannot use the
        # status to map which paths a manifest names. One `code` for the same
        # reason: a caller that could tell them apart from the code would have the
        # mapping the shared status withholds.
        return web.json_response({"error": "not found", "code": "art_not_found"}, status=404)
    data, validator = full_path
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    # `no-cache`, not a long max-age: an app update rewrites these bytes in place
    # under the same URL, so the browser must revalidate. The validator comes from
    # the descriptor the bytes were read from, so an unchanged icon still costs one
    # 304 rather than a full body — which matters because the rail re-renders on
    # every dashboard load.
    #
    # The CSP is load-bearing, not decoration. `.svg` is in the allowlist because an
    # SVG in an `<img>` is script-inert, but a TOP-LEVEL NAVIGATION to this URL is a
    # different thing: the response becomes a DOCUMENT on the dashboard's own origin,
    # and the dashboard's base CSP is deliberately permissive there
    # (`script-src 'self' 'unsafe-inline'`, so widget and MCP-app iframes can run
    # inline script), so it would NOT stop a scripted SVG an app declared as its art.
    # Same-origin script then reaches the authenticated dashboard API with the
    # viewer's session.
    #
    # `sandbox` with no tokens gives the document an opaque origin and no script at
    # all, and `default-src 'none'` stops it fetching anything; a response CSP does
    # not apply when the bytes are consumed as an `<img>` subresource, so the store's
    # own rendering is unaffected. `nosniff` matters because the Content-Type here is
    # derived from the EXTENSION, not the bytes: without it, art named `.png` whose
    # content is markup could still be sniffed into a document.
    #
    # Set on the response rather than in the middleware because the middleware uses
    # `setdefault` precisely so a handler can tighten its own answer.
    return conditional_response(
        request,
        data,
        content_type,
        etag=validator,
        cache_control="no-cache",
        extra_headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
    )


async def _fetch_git_blob(
    repo: str,
    ref: str,
    file_path: str,
    cache_path: Path,
    *,
    git_url: str,
    owner_designated: bool = False,
    credential_target: str | None = None,
) -> bool:
    """Fetch a single file from a registry app's git repo via a shallow clone.

    Public git hosts (GitHub, etc.) disable the ``git-upload-archive`` service
    used by ``git archive --remote``, so we instead perform a shallow
    ``git clone --depth 1 --branch <ref>`` into a throwaway temp directory
    (mirroring how :mod:`kiro_crew.apps.registry` already clones), read the
    requested file out of the checkout, and write it to the blob cache.

    ``git_url`` is the credential-free clone identity, a REQUIRED parameter
    supplied by the caller. It is never resolved here from ``repo``: this
    function performs no registry lookup. For the exact owner-designated case,
    ``credential_target`` may carry the matching config-only HTTP transport URL.
    The match is rechecked here before any spawn, and the split fetch helper gives
    that raw value only to the network subprocess; argv, checkout and cache
    identity retain ``git_url``. This paired handoff closes the TOCTOU window a
    second registry-row resolution would open without retaining credentials in
    the row itself.

    ``owner_designated`` extends the same-repo credential carve-out to this third
    clone chokepoint.  It is ``True`` only when the caller has
    confirmed — via the merged :func:`_owner_designated_repo_target` predicate,
    evaluated against the SAME entry ``git_url`` was resolved from — that the
    entry's clone URL is byte-identical to the owner-typed
    ``ExternalRegistryConfig.repo``.  In that case the confused-deputy defense
    does not apply (the owner designated exactly this URL by configuring the
    registry), so the clone uses owner credentials: ``minimal_env`` + the
    context clone sandbox mode, and the ssh-agent socket (``wrap_argv`` re-judges
    that against the tier), exactly like the manifest/clone chokepoints.
    A sibling repo on the same host is a *different* URL, so it never matches
    and stays anonymous + strict — the carve-out is URL-exact, not host-granular.
    The public identity and optional transport target must sanitize to the same
    URL, so the granted credentials and the repository they reach cannot
    disagree. The grant is SEL-audited against the public ``git_url``.
    """
    # ``git_url`` is the caller's once-resolved public clone identity. The caller
    # already rejects an unresolvable URL (the
    # ``blob_no_git_url`` early-return), so ``git_url`` is non-empty here; keep a
    # defensive guard rather than assume it.
    if not git_url:
        logger.debug(
            "No git URL resolvable for registry repo %r — skipping blob fetch",
            _strip_git_target_userinfo(repo),
        )
        return False
    if _git_target_is_unsupported(git_url):
        logger.warning("blob clone refused an unsupported clone target")
        return False
    # ``git_url`` is the public repository identity used by argv and the blob
    # cache. The optional raw target is recovered server-side from current
    # configuration only for an exact owner-designated match; it is never read
    # from the retained registry row. Accept a raw ``git_url`` as a compatibility
    # fallback for direct internal callers, then immediately split it here.
    credential_target = credential_target or git_url
    git_url = _strip_git_target_userinfo(git_url)
    if _strip_git_target_userinfo(credential_target) != git_url:
        logger.warning("blob clone credential target did not match repository identity")
        return False
    credentialed_transport = credential_target != git_url

    # SSRF gate: a configured external registry's (untrusted) index can list an
    # app ``repo`` pointing at an internal address (e.g. ``https://127.0.0.1/x``)
    # or an attacker-controlled host — and it passes both ``known_registry_repos``
    # and ``_is_safe_repo_identifier``. Browsing the App Store fetches icons
    # through this path automatically, so honoring such a value would drive
    # ``git clone`` against the loopback/internal network (authenticated backend
    # SSRF). Constrain the clone to an explicitly-trusted host (public forge or a
    # host the owner configured as a registry); this is rebinding-proof because
    # it gates on the hostname, not its resolvable IP.
    from kiro_crew.apps.registry import is_clone_host_trusted

    if not await asyncio.to_thread(is_clone_host_trusted, git_url):
        logger.warning(
            "Blob clone refused for repo=%r url=%r: host not in trusted forge/registry set (SSRF gate)",
            _strip_git_target_userinfo(repo),
            _strip_git_target_userinfo(git_url),
        )
        return False

    import tempfile

    tmp_root: str | None = None
    try:
        tmp_root = await asyncio.to_thread(tempfile.mkdtemp, prefix="kirocrew-blob-")
        # Credential posture for the browse-time icon/blob clone.  By default
        # this is an index-originated automatic clone, so it forces the strict
        # sandbox (~/.ssh hidden) and a credential-free env: a trusted-host repo
        # injected by an untrusted registry index can't be cloned with the
        # gateway's ambient git/ssh identity (confused-deputy defense — see
        # anonymous_git_env).  The same-repo carve-out flips BOTH knobs together
        # when the caller confirmed the blob's URL is byte-identical to the
        # owner-configured registry repo: minimal_env + the context clone
        # sandbox mode.  The strict sandbox hiding ~/.ssh is the load-bearing
        # defense (not the env), which is why env and sandbox mode move as a
        # pair — exactly as the merged manifest/clone chokepoints do.
        if owner_designated:
            # ``_context_clone_sandbox_mode`` reaches the same config subsystem the
            # two sibling calls in this function already offload (the SSRF-gate
            # ``is_clone_host_trusted`` above and the caller's
            # ``_owner_designated_repo_target``): it flows
            # ``_configured_registry_hosts`` -> ``_effective_registries`` ->
            # ``KiroCrewConfig.load`` (an unbounded ``read_text`` + ``json.loads`` +
            # ``jsonschema.validate`` on a cold/invalidated cache, e.g. right after
            # a registry refresh rewrites config).  ``_fetch_git_blob`` runs on the
            # gateway event loop during App Store browsing, so calling it inline
            # would freeze every concurrent chat turn and the liveness heartbeat —
            # offload it, exactly like the adjacent reads.
            clone_mode = await asyncio.to_thread(_context_clone_sandbox_mode, git_url)
            clone_env = minimal_env()
            # Escalating this clone from anonymous+strict to owner credentials is
            # a security-relevant permission decision — leave an SEL audit record,
            # mirroring the merged carve-out's grants at the manifest/install sites.
            await asyncio.to_thread(_sel_credential_grant, "app_blob_proxy", git_url)
        else:
            clone_mode = "strict"
            clone_env = anonymous_git_env()
        clone_root = Path(tmp_root)
        if credentialed_transport:
            # A combined credentialed clone performs fetch and checkout in one
            # process. Repository-controlled filters (and checkout-time hooks)
            # would therefore inherit the one-shot URL rewrite containing the
            # secret. Reuse the registry split: only `git fetch` receives that
            # environment; init, checkout and file reads use the credential-free
            # base environment.
            clone_root /= "branch"
            fetch_log: list[str] = []
            fetch_error = await _git_fetch_branch(
                git_url,
                ref,
                clone_root,
                fetch_log,
                credential_target=credential_target,
                clone_env=clone_env,
                sandbox_mode=clone_mode,
            )
            if fetch_error is not None:
                logger.debug(
                    "git fetch failed for %s/%s: %s",
                    _strip_git_target_userinfo(repo),
                    file_path,
                    _loggable_git_transport_output("\n".join(fetch_log), credentialed=True),
                )
                return False
        else:
            clone_cmd = [
                "git",
                *_HOOKS_NEUTRALIZER_ARGV,
                "clone",
                "--depth",
                "1",
                "--branch",
                ref,
                "--single-branch",
                git_url,
                tmp_root,
            ]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                clone_cmd,
                mode=clone_mode,
                forward_ssh_auth_sock=owner_designated,
                _prepare=wrap_argv,
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clone_env,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_BLOB_FETCH_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.warning(
                    "git clone timed out for %s/%s",
                    _strip_git_target_userinfo(repo),
                    file_path,
                )
                return False

            if proc.returncode != 0:
                logger.debug(
                    "git clone failed for %s/%s: %s",
                    _strip_git_target_userinfo(repo),
                    file_path,
                    (
                        _loggable_git_transport_output(
                            stderr.decode(errors="replace").strip(),
                            credentialed=False,
                        )
                        if stderr
                        else ""
                    ),
                )
                return False

        # Read the requested file from the checkout, guarding against escapes
        # out of the clone via symlinks or traversal.
        clone_root = await asyncio.to_thread(clone_root.resolve)
        blob_path = await asyncio.to_thread((clone_root / file_path).resolve)
        try:
            blob_path.relative_to(clone_root)
        except ValueError:
            logger.debug(
                "blob path escapes clone root for %s/%s",
                _strip_git_target_userinfo(repo),
                file_path,
            )
            return False
        if not await asyncio.to_thread(blob_path.is_file):
            return False
        data = await asyncio.to_thread(blob_path.read_bytes)
    except OSError as exc:
        logger.debug(
            "Failed to fetch blob from %s/%s: %s",
            _strip_git_target_userinfo(repo),
            file_path,
            _loggable_git_transport_output(str(exc), credentialed=credentialed_transport),
        )
        return False
    finally:
        if tmp_root:
            await asyncio.to_thread(platform_compat.rmtree_force, tmp_root)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(cache_path.write_bytes, data)
    return True


async def handle_blob_proxy(request: web.Request) -> web.Response:
    """GET /api/apps/blob — proxy image files from a registry app's git repo.

    Query params:
      repo  — registry repo identifier (matches a registry entry's ``repo``)
      path  — file path within the repo (e.g. "assets/icon/logo.png")
      ref   — git ref, defaults to "main"

    SECURITY: Only serves repos listed in the registry JSON (prevents SSRF).
    Caches fetched blobs to ~/.kiro/crew/cache/blobs/{repo}/{ref}/{path}.
    Only serves image file types.
    """
    repo = request.query.get("repo", "")
    file_path = request.query.get("path", "")
    # Resolve the registry entry once: it feeds both the branch fallback below
    # and the same-repo credential carve-out decision at fetch time.  The lookup
    # reads local sync caches only (never fetches), so it is cheap and safe here.
    entry = await asyncio.to_thread(get_registry_app_by_repo, repo) if repo else None
    # Look up the registry entry's branch; fall back to query param or main
    ref = request.query.get("ref", "")
    if not ref:
        ref = entry.get("branch", "main") if entry else "main"

    # Validate inputs
    if not repo or not file_path:
        return web.json_response({"error": "repo and path required"}, status=400)
    if not _is_safe_repo_identifier(repo):
        return web.json_response({"error": "invalid repo URL or name"}, status=400)
    if not _SAFE_PATH_RE.match(file_path):
        return web.json_response({"error": "invalid path characters"}, status=400)
    if not _SAFE_REF_RE.match(ref):
        return web.json_response({"error": "invalid ref"}, status=400)
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    # ``ref`` becomes a path segment in the blob cache tree
    # (``.../{repo_key}/{ref}/{file_path}``).  ``_SAFE_REF_RE`` permits ``.`` and
    # ``/``, so a value like ``../<other-repo-key>/main`` matches the regex; the
    # cache-root containment check below catches an escape OUT of the cache root
    # but NOT a ``..`` that stays UNDER the root while crossing into a DIFFERENT
    # repo's cache directory — a crafted ``ref`` would then yield a cache hit that
    # returns another repo's cached (possibly private) bytes without
    # authorization.  Reject any ``..`` segment or leading ``/`` in ``ref``
    # BEFORE the cache path is built or read from it, mirroring the
    # ``file_path`` guard above, so a ``ref`` can only ever name a flat branch
    # subtree under its own ``repo_key``.
    if ".." in ref or ref.startswith("/"):
        return web.json_response({"error": "invalid ref", "code": "blob_invalid_ref"}, status=400)
    # Block access to git internals and other hidden directories
    if any(seg.startswith(".") for seg in Path(file_path).parts):
        return web.json_response({"error": "hidden path segments not allowed"}, status=400)

    ext = Path(file_path).suffix.lower()
    if ext not in _ART_IMAGE_EXTENSIONS:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)

    # SECURITY: Only allow repos that appear in the registry (prevents SSRF)
    allowed = await asyncio.to_thread(known_registry_repos)
    if repo not in allowed:
        return web.json_response({"error": "repo not in registry"}, status=403)

    # Resolve the blob's PROVENANCE — the clone URL — BEFORE the cache lookup, so
    # the cache key can be bound to it.  A ``repo`` key alone is not stable
    # provenance: registry A (private) can cache a blob under key X, be removed,
    # and registry B later be configured reusing key X — then B's request would
    # hit A's cached private bytes.  Binding the cache key to the resolved clone
    # URL namespaces the cache by the URL the bytes were actually cloned from, so
    # a repo-key reuse across registries lands in a distinct directory (a miss +
    # a fresh clone of B's own URL) rather than serving A's stale bytes.
    #
    # Both resolutions here are PURE in-memory (no registry read, no event-loop
    # stall): ``_entry_git_url`` reads the already-loaded ``entry`` dict, and the
    # no-entry branch is a string-shape test on the already-validated ``repo``.
    # The SAME ``entry`` object also backs the ``owner_designated`` credential
    # decision below, so the URL that keys the cache, the URL authorized, and the
    # URL cloned are one value by identity.
    if entry is not None:
        clone_url = _entry_git_url(entry)
    else:
        # No bundled entry: an external (federated) registry whose ``repo`` is
        # itself a full git URL never resolves an ``entry`` (that lookup searches
        # bundled entries only), yet ``_is_safe_repo_identifier`` admits such
        # URLs.  Honor the validated URL directly, or external blobs become
        # unreachable.  A second ``get_registry_app_by_repo`` read would stall the
        # event loop and reopen the TOCTOU seam, so this is a string-shape test on
        # ``repo``, never a lookup.
        clone_url = (
            repo if ("://" in repo) or repo.startswith("git@") or repo.endswith(".git") else ""
        )
    if not clone_url:
        logger.debug(
            "No git URL resolvable for registry repo %r — skipping blob fetch",
            _strip_git_target_userinfo(repo),
        )
        return web.json_response(
            {"error": "failed to fetch blob", "code": "blob_no_git_url"}, status=502
        )

    # Check cache.  ``repo`` may now be a full git URL (containing ``/`` and
    # ``:``), so derive a flat, filesystem-safe, injective cache key rather than
    # using the raw value as a directory tree.  The key is bound to the resolved
    # ``clone_url`` (provenance) so a repo-key reuse across registries cannot
    # serve another registry's cached bytes.  The resolved-path check below still
    # guards against any escape out of the cache root.
    repo_key = _blob_cache_key(repo, clone_url)
    cache_path = _blob_cache_dir() / repo_key / ref / file_path

    # SECURITY: Verify resolved path stays within cache dir BEFORE any
    # filesystem side effects (mkdir).  We resolve the parent against the
    # cache root to catch symlink-based escapes.
    cache_root_resolved = await asyncio.to_thread(_blob_cache_dir().resolve)
    try:
        resolved_parent = await asyncio.to_thread(cache_path.parent.resolve)
    except OSError:
        resolved_parent = cache_path.parent
    try:
        resolved_parent.relative_to(cache_root_resolved)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)
    resolved = await asyncio.to_thread(cache_path.resolve)
    try:
        resolved.relative_to(cache_root_resolved)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Safe to create directories now that path is validated
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not await asyncio.to_thread(cache_path.is_file):
        # Same-repo credential carve-out, extended to the blob chokepoint:
        # only when the entry's clone URL is byte-identical to the owner-typed
        # registry repo does the clone get owner credentials.  Reuse the merged
        # predicate verbatim — no host normalization, no index-supplied URL trust;
        # a bundled entry (no ``_registry``) or a sibling repo on the same host
        # returns False and stays anonymous + strict.
        #
        # ``clone_url`` was resolved above from the SAME ``entry`` object the
        # credential decision is made against. The public identity remains the
        # cache key and argv URL; an exact raw config target is handed off
        # separately and accepted only when it sanitizes back to that identity.
        # The callee never re-resolves from ``repo``. This closes
        # a TOCTOU window: if the callee re-read the URL from ``repo`` a concurrent
        # registry refresh could, between this decision and the clone, swap the
        # entry backing ``repo`` to a private sibling — and the owner-credential
        # grant decided for the old URL would then clone the new one.  One read
        # of one entry makes ``owner_designated`` and ``clone_url`` describe the
        # same value by identity, not "by construction" across two reads.
        owner_designated = False
        owner_credential_target = ""
        if entry is not None:
            # The owner-credential grant must be scoped to the entry's CONFIGURED
            # branch, not an attacker-chosen ``ref``.  ``ref`` falls back to the
            # entry's ``branch`` only when the query param is empty; a caller can
            # otherwise supply any ``_SAFE_REF_RE``-valid ``ref`` (e.g.
            # ``iconPath=logo.png&ref=private``).  Without this gate the grant is
            # decided on the entry alone, so a crafted ``ref`` would drive an
            # owner-credentialed clone of an UNCONFIGURED (e.g. private) branch of
            # the owner's repo and serve its image bytes.  The configured branch
            # is the only ref the owner designated for this registry; require the
            # effective ``ref`` to equal it before honoring ``owner_designated``.
            # A differing ``ref`` is not rejected (the anonymous path still serves
            # a public branch) — it simply never attaches credentials.
            configured_branch = entry.get("branch", "main")
            if ref == configured_branch:
                # ``get_registry_app_by_repo`` selected this entry by ``repo`` key
                # alone (bundled first, then each external registry).  Provenance
                # is only unambiguous — and the owner-credential grant only
                # honestly attributable to the entry actually being served — when
                # exactly ONE configured source publishes that key.  If more than
                # one registry claims the same ``repo``, a request reachable
                # through registry B could resolve to registry A's owner-designated
                # entry, so downgrade to anonymous+strict (never grant) rather than
                # clone A's private repo with A's credentials on a B-reachable
                # request.  Only a single owner may reach
                # ``_owner_designated_repo_target``; the grant then stays gated on the
                # entry-scoped byte-identical URL check as before (no widening).
                owner_count = await asyncio.to_thread(_repo_key_owner_count, repo)
                if owner_count == 1:
                    owner_credential_target = await asyncio.to_thread(
                        _owner_designated_repo_target, entry
                    )
                    owner_designated = bool(owner_credential_target)
        async with _BLOB_FETCH_SEMAPHORE:
            # Re-check after acquiring semaphore (another request may have cached it)
            if not await asyncio.to_thread(cache_path.is_file):
                fetch_kwargs: dict[str, Any] = {
                    "git_url": clone_url,
                    "owner_designated": owner_designated,
                }
                if owner_credential_target and owner_credential_target != clone_url:
                    fetch_kwargs["credential_target"] = owner_credential_target
                ok = await _fetch_git_blob(
                    repo,
                    ref,
                    file_path,
                    cache_path,
                    **fetch_kwargs,
                )
                if not ok:
                    return web.json_response({"error": "failed to fetch blob"}, status=502)

    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    # Keep a SERVED file young. ``_gc_blob_cache_dir`` reclaims a blob-cache file whose
    # mtime has aged past the grace, and only the owner-tier prewarm re-publishes (and
    # so re-dates) a live row's art; an index-tier blob the proxy wrote once is never
    # re-published, so without this a file that is still being served every day would
    # age out and be reclaimed, then re-cloned on the next browse. Touching the mtime on
    # each serve makes "being served" keep a file young exactly as a rewrite does, so the
    # sweep only reclaims art that is neither served nor rewritten. Off the event loop:
    # ``os.utime`` is a blocking syscall (it can round-trip a network-homed data home),
    # and this handler runs on the loop. Best-effort: a serve must not fail because the
    # touch did (a read-only mount, a racing GC unlink), so any error is ignored -- the
    # file is served regardless, and at worst ages out one grace window later than it
    # might have. ``follow_symlinks=False``: the name was validated by the containment
    # check above, and a link planted at it afterwards must not have its TARGET touched.
    # Windows has no ``follow_symlinks=False`` for ``os.utime`` (it raises
    # NotImplementedError, which would skip every touch there), so where the flag is
    # unsupported the name is lstat-checked as a plain regular file first and touched
    # by name.
    try:
        await asyncio.get_running_loop().run_in_executor(None, _touch_served_blob, cache_path)
    except (OSError, NotImplementedError):
        pass
    sel().log_api_access(
        caller="dashboard",
        operation="app_blob_proxy",
        outcome="served",
        resources=f"repo={_strip_git_target_userinfo(repo)} path={file_path}",
    )
    return web.FileResponse(  # type: ignore[return-value]
        cache_path,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",  # 24h browser cache
        },
    )
