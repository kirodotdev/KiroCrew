"""Design Critique — backend API routes.

Registered at gateway startup by the ``BUILTIN_NAMES`` loop in
``dashboard/routes/system.py`` (via the package re-export in
``design_critique/__init__.py``).

These endpoints do every step that needs a shell or the filesystem — cloning a
repo, discovering its routes, and rendering screens to PNGs — server-side, so the
LLM agent never has to. The agent is then only ever asked to reason over finished
images with no tools, which is why it can no longer stall on a tool-approval
prompt that the app panel has nowhere to show.

Routes (browser-facing, same-origin authed):

  GET  /api/apps/design-critique/method    -> the critique method text to inline
  POST /api/apps/design-critique/discover  -> {kind,value} -> candidate screens
  POST /api/apps/design-critique/render     -> {kind,value,handle,picks} -> PNGs
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import urlparse

from aiohttp import web

import kiro_crew
from kiro_crew import link_unfurl, platform_compat, sandbox
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.config.paths import config_dir
from kiro_crew.security import (
    DENIED_ROOT_PARTS,
    is_sensitive_path,
    path_contains_sensitive,
)

logger = logging.getLogger(__name__)

APP_NAME = "design-critique"
_PREFIX = f"/api/apps/{APP_NAME}"

# Resolved from the installed package, in-process — never via a `python3 -c
# "import kiro_crew"` SHELL command, which the gateway's own security filter
# hard-blocks because the string contains the package name.
_SKILL_DIR = (
    Path(kiro_crew.__file__).parent / "apps/builtins/design_critique/skills/design-critique"
)
_SCRIPTS_DIR = _SKILL_DIR / "scripts"

# A route path or discovery id can be anything; collapse to a safe filename stem.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Credential dot-dirs a local target may never be, shared with design_tweak's
# local-path guard via kiro_crew.security. The is_sensitive_path floor covers the
# crew home + governance trust root; these are the plain dot-dirs it does not
# enumerate.
_DENIED_ROOT_PARTS = DENIED_ROOT_PARTS

# Discover/capture scripts emit a small JSON manifest; this per-pipe cap guards a
# runaway child (a pathological repo / huge render) from buffering into an OOM.
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024

# Upper bounds on operator-supplied request fields, so an absurd URL/path/ref or a
# flood of picks is refused with 400 before it reaches the filesystem or a
# subprocess argv (where an overlong value raises OSError -> HTTP 500).
_MAX_FIELD_LEN = 4096
_MAX_PICKS = 100

# First render on a machine without Google Chrome downloads a Chromium build
# (once, into a home cache). macOS dashboard users have Chrome, so the scripts'
# channel:'chrome' path skips the download — but keep the ceiling high enough
# that a cold first run still completes rather than being killed mid-download.
_DISCOVER_TIMEOUT = 180
_CAPTURE_TIMEOUT = 900
_CLONE_TIMEOUT = 180
# Clone dirs are reused between discover and render, then swept when stale.
_CLONE_TTL_SEC = 60 * 60

# ── background job registry ──
#
# discover/render used to run inline in the request, so a browser navigating away
# mid-scan cancelled the fetch and the scan with it. Now the heavy work runs in a
# DETACHED asyncio task keyed by a job id: the request returns the id immediately
# and the client polls a GET for the result, so a disconnect can no longer stop
# the scan. Records live in memory only (a critique is transient) and are swept on
# the same ~1h TTL the clone dirs use.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SEC = 60 * 60


def _sweep_jobs() -> None:
    now = time.time()
    with _JOBS_LOCK:
        stale = [k for k, v in _JOBS.items() if now - v.get("created_at", now) > _JOB_TTL_SEC]
        for k in stale:
            _JOBS.pop(k, None)


def _get_job(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        rec = _JOBS.get(job_id)
        # Return a shallow copy of the reportable fields only — never the task
        # handle, which is not JSON-serializable and is kept solely to hold a
        # reference so the detached task is not garbage-collected mid-run.
        if rec is None:
            return None
        return {"status": rec["status"], "result": rec["result"], "error": rec["error"]}


def _finish_job(job_id: str, *, result: Any = None, error: str | None = None) -> None:
    with _JOBS_LOCK:
        rec = _JOBS.get(job_id)
        if rec is None:
            return
        if error is not None:
            rec["status"] = "error"
            rec["error"] = error
        else:
            rec["status"] = "done"
            rec["result"] = result


# ── discovery-probe PNG cache ──
#
# The discover probe (capture-build.mjs over the first <=20 routes) is the slowest
# step of a critique, and its PNGs cover most of what /render is then asked to
# capture — so re-capturing a picked route the probe already rendered doubles
# exactly the latency the probe was meant to remove. The probe PNGs are therefore
# retained past discover with a handle-keyed route->PNG map recorded here, and
# /render reuses one for any picked route the probe covered, shelling out only for
# the rest.
#
# Guarded by its own lock, mirroring the _JOBS registry. Each entry is:
#   {"dir": <retained probe dir>, "routes": {<route>: <abs png path>},
#    "created_at": <time.time()>, "build_dir": <abs build output served>,
#    "served_signature": <_served_signature() digest>}
_PROBE_CACHE: dict[str, dict[str, Any]] = {}
_PROBE_CACHE_LOCK = threading.Lock()
# Ordering stamp of the newest discovery known to have STARTED for each handle, as a
# strictly increasing monotonic_ns value. This is what makes a cache write ordered:
# a write is installed only if no newer discovery of the same handle has begun, so an
# older capture can never land on top of a newer one.
#
# It has to live outside the cache entry, because every discovery evicts the handle's
# entry before it captures — so a stamp kept in the entry would be gone by the time a
# slower, older discovery tried to write, which is precisely when it is needed. The
# stamp is therefore a HIGH-WATER MARK that eviction RAISES rather than clears.
#
# Only stable "local:<path>" handles can genuinely interleave (a repo handle is a
# fresh clone id per discovery), but the guard is unconditional: the cost is one int,
# and a rule that holds for one kind of handle only is a rule nobody can rely on.
# Bounded by _sweep_clones, which drops stamps no live discovery could still be using.
_PROBE_STARTS: dict[str, int] = {}
# How long a cached probe may still be REUSED — deliberately much shorter than the
# retained dir's own lifetime, which is _CLONE_TTL_SEC and belongs to _sweep_clones.
# _served_signature proves the served build output has not changed, but a built SPA
# can fetch live data at runtime, and no filesystem token can see that. Sizing the
# reuse window to the interactive discover -> pick -> render flow bounds how stale a
# reused capture can be; past it the pick is captured fresh, which is cheap insurance
# exactly when the gap has grown big enough to matter. So an entry can be too old to
# reuse while its dir is still on disk — that ordering is the point.
_PROBE_REUSE_TTL_SEC = 10 * 60


def _probe_put(
    handle: str,
    claim: int,
    dir_path: str,
    routes: dict[str, str],
    build_dir: str,
    served_signature: str,
) -> None:
    # Store a retained probe under its discovery handle. A falsy handle (a failed
    # clone returns "") has no /render counterpart to key against, so skip it.
    if not handle:
        return
    # `claim` is the stamp _probe_claim handed this discovery. A newer discovery of the
    # same handle having started since means THIS capture is the older of the two, and
    # last-writer-wins would let it replace the newer one — including replacing a map
    # that correctly withheld a route the newer probe found behind a login gate, whose
    # staleness token still matches because an auth state is not served bytes. So the
    # write is dropped and this discovery's own dir goes with it: nothing may reuse a
    # capture that lost the ordering.
    with _PROBE_CACHE_LOCK:
        superseded = claim < _PROBE_STARTS.get(handle, 0)
        prev = None if superseded else _PROBE_CACHE.get(handle)
        if not superseded:
            _PROBE_CACHE[handle] = {
                "dir": dir_path,
                "routes": dict(routes),
                "created_at": time.time(),
                "build_dir": build_dir,
                "served_signature": served_signature,
            }
    # rmtree is blocking IO; this runs inside the discover to_thread worker, so it stays
    # off the event loop. The `prev` drop is belt-and-braces: reaching it requires the
    # slot to be occupied while this discovery holds the newest claim, which the
    # superseded check above already refuses to any other writer. It is kept so that
    # _probe_put orphans no directory on its own, without depending on that argument.
    if superseded:
        shutil.rmtree(dir_path, ignore_errors=True)
    elif prev is not None and prev.get("dir") and prev["dir"] != dir_path:
        shutil.rmtree(prev["dir"], ignore_errors=True)


def _probe_claim(handle: str) -> int:
    """Register a starting discovery for ``handle`` and evict what it supersedes.

    Returns the ordering stamp the caller MUST hand back to ``_probe_put``; a write
    carrying a stamp older than the newest claim is refused.

    Eviction is the point of calling this first: a prior entry must not survive as a
    fallback, or /render would serve the earlier capture for a project whose discovery
    just failed — a gate appeared on every screen, the probe timed out, the build output
    went away. The old token can still match in those cases, so nothing downstream would
    notice. rmtree is blocking IO, so callers on the event loop must reach this through
    ``asyncio.to_thread``.
    """
    if not handle:
        return 0
    stamp = time.monotonic_ns()
    with _PROBE_CACHE_LOCK:
        # monotonic_ns is non-decreasing rather than strictly increasing, and two claims
        # taken inside one clock tick would compare equal — which _probe_put reads as
        # "not superseded". Force the map strictly upward so ties resolve to the later
        # claimant.
        prior = _PROBE_STARTS.get(handle, 0)
        if stamp <= prior:
            stamp = prior + 1
        _PROBE_STARTS[handle] = stamp
        rec = _PROBE_CACHE.pop(handle, None)
    if rec is not None and rec.get("dir"):
        shutil.rmtree(rec["dir"], ignore_errors=True)
    return stamp


def _probe_get(handle: str) -> dict[str, Any] | None:
    # Return a shallow copy of a still-reusable entry, or None. Copying under the
    # lock keeps a caller from mutating the shared record or racing a sweep. The
    # cutoff is _PROBE_REUSE_TTL_SEC, not the dir's longer _CLONE_TTL_SEC lifetime:
    # an entry whose dir is still on disk can already be too old to reuse.
    if not handle:
        return None
    now = time.time()
    with _PROBE_CACHE_LOCK:
        rec = _PROBE_CACHE.get(handle)
        if rec is None:
            return None
        if now - rec.get("created_at", now) > _PROBE_REUSE_TTL_SEC:
            return None
        return {
            "dir": rec["dir"],
            "routes": dict(rec["routes"]),
            "created_at": rec["created_at"],
            "build_dir": rec["build_dir"],
            "served_signature": rec["served_signature"],
        }


# Files under a build output that one staleness token will stat. A build tree is
# normally hundreds to a couple of thousand files, and one bounded walk of it is far
# cheaper than the browser capture the reuse skips. A tree bigger than this yields NO
# token at all rather than a partial one, so reuse is refused and the pick falls back
# to a fresh capture.
_SIGNATURE_MAX_FILES = 20_000


class _ServedToken(NamedTuple):
    """What one walk of a build output yields.

    ``digest`` is the staleness token compared across /discover and /render.

    ``newest_mtime_ns`` is only used at discover, to detect a build that landed while
    the probe was capturing: the token is necessarily taken AFTER the capture (the
    manifest is what names the build dir), so without this the digest would describe
    post-build bytes while the PNGs depict pre-build ones, and /render would match and
    serve them. It is a high-water mark over both files AND directories — a deletion
    leaves no file behind to carry a recent mtime, so files alone would miss one.
    """

    digest: str
    newest_mtime_ns: int


def _served_signature(build_dir: Path) -> _ServedToken | None:
    """Staleness token for the bytes a reused capture actually depicts.

    capture-build.mjs never builds anything: it serves the already-built output it
    finds under the project dir, so a reused PNG depicts ``build_dir``'s contents,
    NOT the project root's. Signing the project root would miss the case this guard
    exists for, because a directory's own st_mtime moves only when its direct
    entries change and an ordinary rebuild writes underneath an already-existing
    ``dist/``.

    The token digests every file the static server will actually serve — each one's
    relative path, size and ``st_mtime_ns`` — so an ordinary write moves it, including
    an in-place overwrite of a nested file under an unchanged name. The walk is sorted,
    so the token describes the tree rather than the order the filesystem happened to
    enumerate it in.

    It is metadata, not content: a rewrite that keeps a file's byte count identical AND
    restores its original ``st_mtime_ns`` (a deliberate ``utime``, or an archive
    extracted with preserved timestamps) is not detected. Hashing the bytes instead
    would mean reading the whole build output on every /render, which can cost more
    than the capture the reuse saves, and the reuse window (``_PROBE_REUSE_TTL_SEC``)
    already bounds how long such a rewrite could matter. Size-and-mtime is the same
    token build tools themselves rely on.

    "Will actually serve" is capture-build.mjs's own index, and the two enumerations
    have to agree: signing something the server never serves yields false mismatches
    and needless re-captures, while MISSING something it does serve lets a stale PNG
    through. That script indexes with a readdir Dirent test that counts neither a
    symlinked directory nor a symlinked file as one, and skips dot-entries, so none
    of those is ever reachable over the preview server — and none is signed here.

    Returns ``None`` whenever the served set cannot be read in full: missing,
    unreadable, or larger than ``_SIGNATURE_MAX_FILES``. A caller MUST treat ``None``
    as "unknown" rather than "unchanged" and refuse reuse — a token over part of a
    tree is no evidence about the rest.

    This walks and stats a caller-supplied path that may be a stale NFS/UNC mount,
    so callers on the event loop MUST reach it through ``asyncio.to_thread``.
    """

    def _fail(exc: OSError) -> None:
        # os.walk swallows errors by default, which would silently yield a token over
        # the part of the tree it could read. Turn any of them into "no token".
        raise exc

    digest = hashlib.blake2b(digest_size=16)
    newest = 0
    seen = 0
    try:
        for root, dirs, files in os.walk(build_dir, onerror=_fail):
            # Prune in place so the walk itself skips what the server will not serve.
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".") and not os.path.islink(os.path.join(root, d))
            )
            # Each directory's own mtime feeds ``newest_mtime_ns`` but NOT the digest.
            # It has to feed the former because a DELETION leaves no file behind to
            # carry a recent mtime, so a served file removed while the probe was
            # capturing would otherwise not raise the high-water mark and the
            # mid-capture check would miss it. It stays out of the digest because the
            # digest describes the served bytes, and a directory's mtime also moves for
            # changes that leave those bytes identical.
            newest = max(newest, os.stat(root).st_mtime_ns)
            rel_root = os.path.relpath(root, build_dir)
            for name in sorted(files):
                path = os.path.join(root, name)
                if name.startswith(".") or os.path.islink(path):
                    continue
                seen += 1
                if seen > _SIGNATURE_MAX_FILES:
                    return None
                st = os.stat(path)
                newest = max(newest, st.st_mtime_ns)
                entry = f"{rel_root}/{name}\0{st.st_size}\0{st.st_mtime_ns}\0"
                digest.update(entry.encode("utf-8", "surrogateescape"))
    except OSError:
        return None
    return _ServedToken(digest.hexdigest(), newest)


def _probe_build_dir(reported: Any, directory: Path) -> Path | None:
    # The build output capture-build.mjs reports it served, as an absolute path
    # inside the project dir. It is only ever stat()ed — never served, never handed
    # to the client — but keep it under the directory the handler already validated:
    # a token taken over some unrelated tree would stand still and permit reuse of a
    # stale PNG for the whole TTL. None (no reuse) for anything that fails to match.
    if not isinstance(reported, str) or not reported:
        return None
    candidate = Path(reported)
    if not candidate.is_absolute():
        return None
    # capture-build.mjs resolves a relative project path against ITS cwd, which it
    # inherits from the gateway, so anchor the containment check the same way.
    # Comparing an absolute manifest path against a relative `directory` would never
    # match and would silently disable reuse for every relative local target.
    # abspath, not resolve: lexical, so no filesystem IO on a path that may be a
    # stale mount.
    base = Path(os.path.abspath(directory))
    return candidate if candidate.is_relative_to(base) else None


def _start_job(work: Callable[[], Awaitable[dict[str, Any]]]) -> str:
    """Run ``work`` in a detached task and return a job id to poll for its result."""
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status": "running",
            "result": None,
            "error": None,
            "created_at": time.time(),
        }

    async def _runner() -> None:
        try:
            _finish_job(job_id, result=await work())
        except Exception as exc:  # noqa: BLE001 — any failure is reported via the record
            logger.warning("design-critique job %s failed: %s", job_id, exc)
            _finish_job(job_id, error=str(exc))

    # Detached on purpose: not tied to the request's task, so the client
    # disconnecting (navigate-away) cannot cancel it. Keep a reference on the
    # record so the loop does not garbage-collect the task before it finishes.
    task = asyncio.ensure_future(_runner())
    with _JOBS_LOCK:
        rec = _JOBS.get(job_id)
        if rec is not None:
            rec["task"] = task
    return job_id


def _uploads_dir() -> Path:
    # Read at call time, not import: KIROCREW_HOME can relocate the data home and
    # a dev instance does exactly that.
    d = config_dir() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clones_dir() -> Path:
    d = _uploads_dir() / "dc-clones"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sweep_clones() -> None:
    # TTL-sweep the throwaway transient dirs under uploads: each cloned repo under
    # dc-clones/, plus per-probe (dc-probe-*) dirs. NOTE: dc-render-* is deliberately
    # NOT swept — a saved critique's history entries reference those screenshot paths,
    # so deleting them on a later scan would break history images (data loss).
    now = time.time()
    victims: list[Path] = []
    clones = _clones_dir()
    if clones.exists():
        victims += [c for c in clones.iterdir() if c.is_dir()]
    uploads = _uploads_dir()
    if uploads.exists():
        victims += [c for c in uploads.iterdir() if c.is_dir() and c.name.startswith("dc-probe-")]
    removed: list[str] = []
    for child in victims:
        try:
            if now - child.stat().st_mtime > _CLONE_TTL_SEC:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(str(child))
        except OSError:
            continue
    cutoff_ns = time.monotonic_ns() - int(_CLONE_TTL_SEC * 1_000_000_000)
    with _PROBE_CACHE_LOCK:
        # A retained probe dir (dc-probe-*) is swept above on the same TTL, but its
        # _PROBE_CACHE entry would otherwise outlive the dir and hand /render a
        # route->png path for a directory that no longer exists. Purge any cache entry
        # whose retained dir was just removed, so the cache never serves a missing PNG.
        if removed:
            stale_handles = [
                h
                for h, rec in _PROBE_CACHE.items()
                # Equality is exhaustive: a record's dir is always the dc-probe-*
                # dir _probe_put was handed, a direct child of uploads, and every
                # sweep victim is a top-level dir under the same parent.
                if rec.get("dir") in removed
            ]
            for h in stale_handles:
                _PROBE_CACHE.pop(h, None)
        # Ordering stamps are not tied to a dir, so they need their own bound or the map
        # would grow one entry per repo discovery forever (each is a fresh clone id).
        # _CLONE_TTL_SEC (3600s) is over three times _DISCOVER_TIMEOUT + _CAPTURE_TIMEOUT
        # (1080s), so a dropped stamp cannot belong to a discovery still able to write.
        for h in [h for h, stamp in _PROBE_STARTS.items() if stamp < cutoff_ns]:
            _PROBE_STARTS.pop(h, None)


def _tool(name: str) -> str | None:
    # Resolve to an absolute path and invoke children by it, so a shim planted
    # later on PATH cannot intercept the gateway's node/git.
    if name == "git":
        # git is a system tool: resolve it off PATH entirely via the shared
        # trusted resolver, so an agent-planted PATH shim can never be spawned.
        return platform_compat.trusted_git_bin()
    p = shutil.which(name)
    if not p or not os.path.isabs(p):
        return None
    # node legitimately lives outside the trusted system dirs (nvm, Homebrew), so
    # it stays PATH-resolved to preserve resolution on normal dev/prod installs —
    # but reject a hit inside a root THIS app can write (the crew data home and
    # the session scratch dir), which is where its own flow could drop a shim.
    # System / nvm / Homebrew node is outside those, so behavior is unchanged.
    try:
        resolved = Path(p).resolve()
        writable_roots = [config_dir().resolve()]
        for _var in ("KIROCREW_SCRATCH", "TMPDIR"):
            _v = os.environ.get(_var)
            if _v:
                try:
                    writable_roots.append(Path(_v).resolve())
                except OSError:
                    continue
        for _root in writable_roots:
            if resolved == _root or _root in resolved.parents:
                return None
    except OSError:
        return None
    return p


def _node() -> str | None:
    return _tool("node")


def _is_sensitive_dir(p: Path) -> bool:
    # A local target must never let the renderer walk a credential directory.
    # `is_sensitive_path` / `path_contains_sensitive` cover the crew home and the
    # governance trust root; the explicit dot-dir set mirrors design_tweak's
    # local-path guard for the credential dirs that floor does not enumerate.
    s = str(p)
    if is_sensitive_path(s) or path_contains_sensitive(s):
        return True
    return any(part in _DENIED_ROOT_PARTS for part in p.parts)


def _is_http_url(u: str) -> bool:
    # Only http(s) may be rendered — a file:// or other scheme would turn the
    # renderer into a server-side read primitive for local files. A malformed URL
    # (e.g. a bad IPv6 authority like "http://[::1") makes urlparse itself raise
    # ValueError, so refuse rather than let it crash discovery.
    try:
        return urlparse(u).scheme in ("http", "https")
    except ValueError:
        return False


async def _resolve_vetted(u: str, allow_loopback: bool = True) -> list[str] | None:
    # Resolve the host and vet every address, returning the vetted IPs (so a
    # caller can PIN to them and avoid a second, unvetted resolution) or None if
    # any address is internal. A localhost PREVIEW is supported so LOOPBACK is
    # allowed for kind=url; a repo CLONE passes allow_loopback=False. Everything
    # else internal is rejected — link-local (incl. 169.254.169.254 metadata),
    # private, reserved, multicast, unspecified. Public hosts are allowed.
    if not _is_http_url(u):
        return None
    try:
        p = urlparse(u)
        if not p.hostname:
            return None
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        # A malformed authority (e.g. a non-numeric or out-of-range port) must be
        # refused, not raised — .port throws ValueError on a bad port.
        return None
    # The loopback exemption (localhost preview) applies ONLY when the operator
    # TYPED a loopback host — not when an arbitrary hostname resolves to loopback,
    # which would let an attacker name front a localhost service.
    typed = (p.hostname or "").strip("[]").lower()
    typed_loopback = typed == "localhost"
    try:
        typed_loopback = typed_loopback or ipaddress.ip_address(typed).is_loopback
    except ValueError:
        pass
    try:
        # DNS is blocking, so resolve off the event loop.
        infos = await asyncio.to_thread(socket.getaddrinfo, p.hostname, port, 0, socket.SOCK_STREAM)
    except OSError:
        return None
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None
        if ip.is_loopback:
            if allow_loopback and typed_loopback:
                ips.append(addr)
                continue
            return None
        # Delegate the public/non-public decision to the single owner of the
        # address rule (link_unfurl.address_is_not_public): it fails closed, and
        # already handles the is_global carve-outs a per-channel re-enumeration
        # keeps missing — CGNAT 100.64.0.0/10, IPv6 site-local fec0::/10 (which
        # reports is_global True), plus canonicalize_ip's 6to4 / IPv4-mapped
        # normalization. Loopback is handled above because this app allows a
        # localhost preview, which that owner (correctly) rejects.
        if link_unfurl.address_is_not_public(addr):
            return None
        ips.append(addr)
    return ips or None


async def _url_target_allowed(u: str, allow_loopback: bool = True) -> bool:
    # SSRF guard for URL discovery/render. Thin bool wrapper over _resolve_vetted.
    return (await _resolve_vetted(u, allow_loopback)) is not None


def _bad_request(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


def _require_enabled(handler):
    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"},
                status=403,
            )
        return await handler(request)

    return _wrapped


async def _json_object(
    request: web.Request,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    try:
        body = await request.json()
    except ValueError:
        return None, _bad_request("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return None, _bad_request("body must be a JSON object", "body_not_object")
    return body, None


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf), False
        buf.extend(chunk)
        if len(buf) > limit:
            return bytes(buf[:limit]), True


async def _run(
    cmd: list[str], timeout: int, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a subprocess off the event loop, killing it if it overruns time or output."""
    # Route through the sandbox chokepoint (security-review 92e24570): cmd is
    # agent-influenced (repo URL / local path / preview URL), so the child gets OS
    # filesystem isolation + a credential-scrubbed env on top of the PATH pin and
    # git-config neutralization _script_env already applies. "standard" mode leaves
    # network egress and exec intact — the git clone still reaches its pinned IP and
    # the node/Chromium render still runs — hiding only credential dirs, not the
    # clone/render dirs, the bundled scripts, or the project under review.
    wrapped, run_env, cleanup = await sandbox.sandboxed_spawn_argv_async(
        cmd,
        mode="standard",
        env=env,
        _prepare=sandbox.sandboxed_spawn_argv,
    )
    try:
        # create_subprocess_limited applies the RLIMIT ceiling post-exec (fork-bomb /
        # FD / mem / CPU) that the sandbox audit requires of every routed spawn.
        proc = await sandbox.create_subprocess_limited(
            *wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=run_env,
            # Own process group so the tree kill in kill_and_reap reaches the whole
            # child tree (the node capture script's Chromium), not just node.
            start_new_session=True,
        )
        try:
            # Read both pipes concurrently (avoids a full-buffer deadlock) with a
            # per-pipe cap: the scripts emit a small JSON manifest, so a child that
            # floods stdout/stderr is a runaway and must not buffer into an OOM.
            (out, out_over), (err, err_over) = await asyncio.wait_for(
                asyncio.gather(
                    _read_capped(proc.stdout, _MAX_OUTPUT_BYTES),
                    _read_capped(proc.stderr, _MAX_OUTPUT_BYTES),
                ),
                timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # A timeout OR a cancelled request (the browser navigated away mid-scan)
            # must not leave the git/Playwright child tree running server-side.
            # kill_and_reap kills the whole tree (POSIX process group + Windows
            # taskkill /T) and reaps under a bound.
            await platform_compat.kill_and_reap(proc)
            raise
        if out_over or err_over:
            await platform_compat.kill_and_reap(proc)
            logger.warning(
                "design-critique subprocess output exceeded %d bytes; killed",
                _MAX_OUTPUT_BYTES,
            )
        else:
            await proc.wait()
        return (
            proc.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )
    finally:
        # Unlink the temp launcher/profile the sandbox materialized for this spawn.
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)


def _script_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # a private/bad repo must fail, never prompt
    # Pin the child PATH to just the resolved toolchain dirs (node — and the npm
    # that ships beside it — plus git), so a nested tool cannot pick up an
    # attacker-planted shim from an inherited, writable PATH prefix. Derived from
    # shutil.which, so it carries no hardcoded system path and stays portable.
    dirs: list[str] = []
    for tool in (_tool("node"), _tool("git")):
        if tool:
            d = os.path.dirname(tool)
            if d not in dirs:
                dirs.append(d)
    if dirs:
        env["PATH"] = os.pathsep.join(dirs)
    # Neutralize inherited git config for the clone: an agent-written ~/.gitconfig
    # (a credential.helper, core.hooksPath, or core.fsmonitor) would otherwise run
    # a command on the gateway's `git clone`, outside the agent sandbox. Point the
    # global and system config at the null device so ONLY the explicit `-c` flags
    # on the clone command apply. Harmless for the node capture scripts.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    # Drop inherited askpass helpers too: GIT_ASKPASS / SSH_ASKPASS name an
    # executable git runs to answer a credential or host-key challenge, so an
    # attacker-controlled clone endpoint could trigger one. Unset here so a
    # challenge fails (with GIT_TERMINAL_PROMPT=0) instead of running a helper.
    for _askpass in ("GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(_askpass, None)
    return env


def _slug(text: str) -> str:
    s = _UNSAFE.sub("-", (text or "").strip("/")).strip("-")
    return s or "screen"


def _route_segments(path: str) -> list[str]:
    # Route paths are POSIX-style URLs, so parse them as URLs (portable), not with
    # a literal "/" split. Drops the leading "/", params (:id) and wildcards (*).
    return [
        p
        for p in PurePosixPath(path or "").parts
        if p not in ("", "/") and not p.startswith(":") and p != "*"
    ]


def _label_for(path: str) -> str:
    # A route becomes a one/two-word label the picker can show; "/" is the home.
    if not path or path == "/":
        return "Home"
    seg = _route_segments(path)
    return (seg[-1] if seg else "Home").replace("-", " ").replace("_", " ")[:18].strip() or "Home"


def _group_for(path: str) -> str:
    seg = _route_segments(path)
    return seg[0] if seg else "root"


# ── GET /method ──


async def _handle_method(request: web.Request) -> web.Response:
    # The critique rubric used to be fetched by the agent with fs_read (a tool
    # call that stalls in the panel). Serve the checklist here so the frontend
    # can inline it into the tool-free prompt instead.
    def _read() -> dict[str, str]:
        checklist = (_SKILL_DIR / "frameworks/main-checklist.md").read_text(encoding="utf-8")
        return {"checklist": checklist}

    try:
        payload = await asyncio.to_thread(_read)
    except OSError as exc:
        logger.warning("design-critique: method files unreadable: %s", exc)
        return web.json_response(
            {"error": "method files not found", "code": "method_missing"}, status=500
        )
    return web.json_response(payload)


# ── POST /discover ──


async def _discover_from_dir(directory: Path, handle: str) -> dict[str, Any]:
    """Run discover-routes + a capture probe against a local/cloned directory."""
    # Re-discovering a handle supersedes whatever was cached under it, so claim it FIRST
    # — which evicts the prior entry and stamps this discovery's place in the order —
    # and let a successful probe install the replacement. Claiming on the way out instead
    # would be skipped by every path that does not reach the end: a timeout, a node/git
    # failure, an early return, an exception the job wrapper catches. A stale entry
    # surviving any of those is invisible downstream, because its token can still match
    # and /render would serve the earlier capture for a project whose discovery just
    # failed. The stamp is what stops the reverse hazard: this discovery may be the
    # slower of two on the same handle, and _probe_put refuses a write that a newer
    # claim has already superseded. A repo handle is a fresh clone id, so nothing is
    # evicted there — the stamp is still taken, so the rule needs no per-kind exception.
    claim = await asyncio.to_thread(_probe_claim, handle)
    node = await asyncio.to_thread(_node)
    if node is None:
        return {
            "framework": "",
            "note": "",
            "blocked": {
                "reason": "other",
                "detail": "node is not installed on this machine, so screens cannot be discovered.",
            },
            "screens": [],
            "flows": [],
            "cannotSee": [],
            "handle": handle,
        }

    rc, out, err = await _run(
        [node, str(_SCRIPTS_DIR / "discover-routes.mjs"), str(directory)],
        _DISCOVER_TIMEOUT,
        env=await asyncio.to_thread(_script_env),
    )
    try:
        disc = json.loads(out)
    except ValueError:
        logger.warning("design-critique discover-routes bad JSON: %s", err[:300])
        disc = {"framework": "", "routing": "none", "routes": [], "notes": []}

    routes = disc.get("routes") or []
    # Probe which routes actually render, so canSee is grounded rather than guessed.
    seeable: dict[str, bool] = {}
    cannot_see: list[str] = []
    if routes:
        probe_base = await asyncio.to_thread(_uploads_dir)
        probe_out = probe_base / f"dc-probe-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(probe_out.mkdir, parents=True, exist_ok=True)
        csv = ",".join(str(r.get("path", "")) for r in routes[:20] if r.get("path"))
        # Read before the capture starts: any served file written at or after this
        # instant means the build output changed while the probe was screenshotting it.
        probe_started_ns = time.time_ns()
        prc, pout, perr = await _run(
            [
                node,
                str(_SCRIPTS_DIR / "capture-build.mjs"),
                str(directory),
                f"--routes={csv}",
                f"--out={probe_out}",
            ],
            _CAPTURE_TIMEOUT,
            env=await asyncio.to_thread(_script_env),
        )
        try:
            probe = json.loads(pout)
        except ValueError:
            probe = {}
        route_png_map: dict[str, str] = {}
        for s in probe.get("screens") or []:
            route = str(s.get("route"))
            seeable[route] = True
            # Cache the route->PNG path so /render can reuse this capture instead
            # of re-rendering the same route. Only screens with both a route and a
            # path are usable; the path is absolute, inside probe_out.
            #
            # A screen the probe caught under a login / consent / onboarding overlay is
            # NOT cached. /render raises its own gate warning from the capture it runs,
            # and a fully-covered render runs no capture at all — so reusing a gate
            # screenshot would hand the critic a picture of the wall with that warning
            # silently missing. Leaving the route uncovered restores both. This keys on
            # the per-screen `overlay` rather than the manifest's `blockedBy` summary,
            # which the script only sets when one overlay covers >=60% of screens: a
            # single gated route has an overlay but no blockedBy, and would slip past.
            #
            # `fullPageCoverage` is what makes reuse LOSSLESS rather than merely fast.
            # /render captures with --full and the probe without it, so a probe PNG of a
            # page taller than the viewport holds strictly less than the render it would
            # replace, and the critic would judge a page whose lower half it cannot see.
            # The script sets this flag only when the page's scroll height fit the
            # viewport, i.e. when the two capture modes produce the same pixels. A route
            # that overflowed is simply left uncovered and /render captures it fresh with
            # --full, exactly as it did before any reuse existed. Absent (an older
            # manifest) is falsy, so reuse fails closed toward capturing.
            png = s.get("path")
            if (
                s.get("route") is not None
                and png
                and not s.get("overlay")
                and s.get("fullPageCoverage")
            ):
                route_png_map[route] = str(png)
        if probe.get("blockedBy"):
            b = probe["blockedBy"]
            cannot_see.append(
                f"{b.get('onScreens', '')} of {b.get('ofScreens', '')} screens blocked by a {b.get('likely', 'gate')}."
            )
        if probe.get("buildDir") is None and probe.get("notes"):
            cannot_see.extend(str(n) for n in probe["notes"])
        # When the probe produced at least one usable screen AND a staleness token
        # over the build output it served, RETAIN the dir and cache its route->PNG
        # map keyed by `handle` so /render can reuse those PNGs instead of
        # re-capturing (see _PROBE_CACHE). The retained dir is a dc-probe-* dir, so
        # _sweep_clones TTL-sweeps it (and purges the matching cache entry) on the
        # ~1h _CLONE_TTL_SEC window; its fresh mkdir mtime starts that clock.
        #
        # The probe deliberately stays WITHOUT --full while /render runs WITH it. The
        # reason is blast radius, not screenshot cost: this ONE subprocess renders up to
        # 20 routes of an unknown project under a single shared _CAPTURE_TIMEOUT, and a
        # full-page screenshot of an unknown page is unbounded (a long or virtualised
        # list can make it enormous or very slow). One pathological route would then
        # exhaust the shared budget, the manifest would not parse, and EVERY route would
        # come back canSee=False — losing all of discovery to improve a subset of
        # renders. /render's --full runs on the two or three routes the user picked, with
        # the same budget and far more headroom per route, which is why the flag belongs
        # there and not here.
        #
        # That asymmetry does NOT reach a critique, because only screens the script
        # certified as `fullPageCoverage` enter route_png_map above: reuse is confined to
        # pages that fit the viewport, where a --full capture would have produced the
        # same pixels. So a covered pick is a substitution of equals, and a page too tall
        # to reuse losslessly is re-captured with --full instead of being silently
        # truncated. Making the probe itself pass --full would buy the same fidelity at
        # exactly the blast radius above.
        build_dir = _probe_build_dir(probe.get("buildDir"), directory)
        token = (
            await asyncio.to_thread(_served_signature, build_dir)
            if route_png_map and build_dir is not None
            else None
        )
        if token is not None and token.newest_mtime_ns >= probe_started_ns:
            # A build landed while the probe was capturing. The token has to be taken
            # after the capture (the manifest is what names the build dir), so it
            # describes the NEW bytes while the PNGs depict the old ones — and /render
            # would find it matching and serve them. Refuse to cache instead.
            token = None
        if build_dir is not None and token is not None:
            await asyncio.to_thread(
                _probe_put,
                handle,
                claim,
                str(probe_out),
                route_png_map,
                str(build_dir),
                token.digest,
            )
        else:
            # No usable screen, no readable build output to take a staleness token
            # over, or a capture the token cannot vouch for: either way there is
            # nothing that can be reused safely, so drop the dir now (off the event
            # loop — a recursive delete is blocking IO) rather than leak it to the
            # TTL sweep.
            await asyncio.to_thread(shutil.rmtree, probe_out, ignore_errors=True)

    screens = []
    for i, r in enumerate(routes):
        path = str(r.get("path", ""))
        if not path:
            continue
        # Default False: a route the probe returned no image for is not renderable
        # (marking it renderable walks the user into a render that always fails).
        can = seeable.get(path, False)
        screens.append(
            {
                # Index-prefixed so distinct routes that _slug() collapses onto the
                # same stem ('/a/b' and '/a-b' both -> 'a-b') keep distinct ids;
                # otherwise the frontend resolves both picks to the latter screen.
                "id": f"{i:02d}-{_slug(path)}",
                "label": _label_for(path),
                "ref": path,
                "group": _group_for(path),
                "canSee": bool(can),
                "why": "" if can else "needs a build or a running server to render",
            }
        )

    # Loose flows by top-level group, marked a guess since no navigation was seen.
    flows = []
    by_group: dict[str, list[str]] = {}
    for s in screens:
        by_group.setdefault(s["group"], []).append(s["id"])
    for group, ids in by_group.items():
        if len(ids) > 1:
            flows.append(
                {
                    "label": group,
                    "why": "grouped by shared top-level path",
                    "basis": "guess",
                    "screenIds": ids,
                }
            )

    return {
        "framework": disc.get("framework", ""),
        "note": (disc.get("notes") or [""])[0] if disc.get("notes") else "",
        "blocked": None,
        "screens": screens,
        "flows": flows,
        "cannotSee": cannot_see,
        "handle": handle,
    }


async def _discover_repo_job(value: str, vetted: list[str], git_bin: str) -> dict[str, Any]:
    """Clone a vetted repo (detached) and discover its screens."""
    # Pin git's DNS to the addresses we just vetted. Without this git does its own
    # fresh resolution in a separate process, so a TTL-0 name that answered public
    # to our check could answer a private / loopback address to git (DNS
    # rebinding). curloptResolve keeps the hostname (TLS SNI/cert intact) but forces
    # the connection to a vetted IP.
    pu = urlparse(value)
    pin_port = pu.port or (443 if pu.scheme == "https" else 80)
    pin_args: list[str] = []
    for ip in vetted:
        pin_args += ["-c", f"http.curloptResolve={pu.hostname}:{pin_port}:{ip}"]
    clone_id = uuid.uuid4().hex[:12]
    target = _clones_dir() / clone_id
    rc, out, cerr = await _run(
        # Pin remote-helper transports off and pass the URL after `--` so a
        # crafted value can neither run a helper nor be read as an option.
        [
            git_bin,
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.allow=user",
            # The one-time SSRF check validates only the typed URL; without this
            # git would follow a 3xx to a private address after that check.
            "-c",
            "http.followRedirects=false",
            *pin_args,
            "clone",
            "--depth",
            "1",
            "--",
            value,
            str(target),
        ],
        _CLONE_TIMEOUT,
        env=await asyncio.to_thread(_script_env),
    )
    if rc != 0 or not await asyncio.to_thread(target.exists):
        # A failed or timed-out clone can leave a partial repo behind; remove it
        # now instead of waiting for the TTL sweep, so repeated failures cannot
        # accumulate and exhaust disk (ENOSPC). This does not change the success
        # path — a good clone is kept and rendered as before.
        await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
        # GitHub says "Repository not found" for both a missing repo and a private
        # one you cannot read; do not guess which.
        return {
            "framework": "",
            "note": "",
            "blocked": {
                "reason": "no-access",
                "detail": (cerr or "git clone failed").strip()[:500]
                + " (the repository may not exist, or it is private and cannot be read).",
            },
            "screens": [],
            "flows": [],
            "cannotSee": [],
            "handle": "",
        }
    return await _discover_from_dir(target, handle=clone_id)


async def _handle_discover(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if body is None:
        return err or _bad_request("invalid JSON", "invalid_json")
    kind = str(body.get("kind") or "").strip()
    value = str(body.get("value") or "").strip()
    if not kind or not value:
        return _bad_request("kind and value are required", "missing_field")
    if len(value) > _MAX_FIELD_LEN:
        return _bad_request("value is too long", "field_too_long")

    await asyncio.to_thread(_sweep_clones)
    _sweep_jobs()

    if kind == "figma":
        # Exporting Figma frames needs the Figma desktop tools, which only exist
        # inside an agent — and an agent in an app panel is exactly what stalls.
        # Route the user to frame-image export, which runs the working image path.
        return web.json_response(
            {
                "framework": "Figma",
                "note": "",
                "blocked": {
                    "reason": "figma-export-needed",
                    "detail": "Export the frames you want critiqued as PNGs and drop them in as screenshots — that runs the same critique without needing the Figma desktop app.",
                },
                "screens": [],
                "flows": [],
                "cannotSee": [],
                "handle": "",
            }
        )

    if kind == "repo":
        vetted = await _resolve_vetted(value, allow_loopback=False)
        if vetted is None:
            # Only http(s) repository URLs to a non-internal host. Rejects the git
            # remote-helper RCE (`ext::sh -c …`), file://, git://, ssh://, option
            # injection (`--upload-pack=…`), SSRF to an internal/private host, AND
            # loopback (a repo clone has no localhost-preview use). Returned
            # synchronously — it is an input rejection, not heavy work.
            return web.json_response(
                {
                    "framework": "",
                    "note": "",
                    "blocked": {
                        "reason": "no-access",
                        "detail": "only http(s) repository URLs to a public host are supported.",
                    },
                    "screens": [],
                    "flows": [],
                    "cannotSee": [],
                    "handle": "",
                }
            )
        git_bin = _tool("git")
        if git_bin is None:
            return web.json_response(
                {
                    "framework": "",
                    "note": "",
                    "blocked": {
                        "reason": "no-access",
                        "detail": "git is not available on this host.",
                    },
                    "screens": [],
                    "flows": [],
                    "cannotSee": [],
                    "handle": "",
                }
            )
        # The clone + probe is the heavy step; run it detached so a client
        # disconnect cannot cancel it. The vetted IPs (for the DNS-rebinding pin)
        # travel into the job so no second, unvetted resolution happens.
        return web.json_response(
            {"job": _start_job(lambda: _discover_repo_job(value, vetted, git_bin))}
        )

    if kind == "local":
        p = Path(value).expanduser()
        if await asyncio.to_thread(_is_sensitive_dir, p):
            return web.json_response(
                {
                    "framework": "",
                    "note": "",
                    "blocked": {
                        "reason": "other",
                        "detail": "that path is protected and can't be read.",
                    },
                    "screens": [],
                    "flows": [],
                    "cannotSee": [],
                    "handle": "",
                }
            )
        try:
            exists = await asyncio.to_thread(p.exists)
        except OSError:
            # An overlong/invalid path can make .exists() raise (e.g. ENAMETOOLONG
            # on 3.12); treat it as "no such path" rather than a 500.
            exists = False
        if not exists:
            return web.json_response(
                {
                    "framework": "",
                    "note": "",
                    "blocked": {"reason": "not-found", "detail": f"no such path: {value}"},
                    "screens": [],
                    "flows": [],
                    "cannotSee": [],
                    "handle": "",
                }
            )
        # A local checkout is used in place; its path IS the render handle. The
        # discover probe still shells out to node, so run it detached.
        return web.json_response(
            {"job": _start_job(lambda: _discover_from_dir(p, handle=f"local:{p}"))}
        )

    if kind == "url":
        if not await _url_target_allowed(value):
            return web.json_response(
                {
                    "framework": "",
                    "note": "",
                    "blocked": {
                        "reason": "other",
                        "detail": "that URL can't be reviewed — use an http(s) address that isn't an internal/private host.",
                    },
                    "screens": [],
                    "flows": [],
                    "cannotSee": [],
                    "handle": "",
                }
            )
        # A served URL: treat the page itself as one screen. Link-crawling is left
        # out on purpose — the user can add more screenshots after the first read.
        return web.json_response(
            {
                "framework": "live site",
                "note": "one page discovered; add more screenshots to widen the review",
                "blocked": None,
                "screens": [
                    {
                        "id": "page",
                        "label": "Page",
                        "ref": value,
                        "group": "site",
                        "canSee": True,
                        "why": "",
                    }
                ],
                "flows": [],
                "cannotSee": [],
                "handle": f"url:{value}",
            }
        )

    return _bad_request(f"unknown kind: {kind}", "bad_kind")


# ── POST /render ──


def _adopt_reused(reused: dict[str, str], out_dir: Path) -> dict[str, str]:
    """Copy each reused probe PNG into ``out_dir``; return ref -> the copy's path.

    A returned screen path is kept FOREVER by a saved critique's history entry, which
    is why the TTL sweep exempts dc-render-*. A probe PNG has the opposite lifetime:
    its dc-probe-* dir is swept on _CLONE_TTL_SEC and is deleted outright when the
    same local project is re-discovered. Handing the probe path straight to the
    client would therefore make saved critiques lose their screenshots, so the bytes
    are adopted into the render dir and only the copy is ever returned. The copy
    costs milliseconds; the capture subprocess is still skipped.

    A copy that fails (the probe dir was swept, or rmtree'd by a concurrent local
    re-discovery, between the handler's exists() check and here) is omitted from the
    result. The caller must then treat that ref as UNCOVERED and capture it fresh:
    an omission means "nothing has rendered this pick yet", never "this pick cannot
    be seen". Only the source's basename is used, so a manifest path cannot place the
    copy outside ``out_dir``.
    """
    copies: dict[str, str] = {}
    for i, (ref, src) in enumerate(reused.items()):
        dest = out_dir / f"reused-{i}-{Path(src).name}"
        try:
            shutil.copyfile(src, dest)
        except OSError:
            continue
        copies[ref] = str(dest)
    return copies


async def _render_capture_job(
    kind: str,
    cmd: list[str] | None,
    out_dir: Path,
    refs: list[str],
    labels: list[str],
    adopted: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the capture subprocess (detached) and shape its output for the client.

    The synchronous validation and the SSRF/handle checks already ran in the
    handler; this only executes the vetted command and parses it, so a client
    disconnect cannot cancel the capture.

    ``adopted`` maps a pick's ref -> the copy of a probe PNG the handler already
    adopted into ``out_dir`` (see _adopt_reused). Those (repo/local) refs are absent
    from ``cmd``'s route list, and are merged in at the pick's original step order —
    so /render pays the capture cost only for the routes the probe did not cover. A
    ref the handler failed to adopt is in ``cmd``'s route list instead, never here.
    ``cmd`` is None when every pick is already adopted: there is then no capture to
    run at all.
    """
    adopted_paths = adopted or {}
    screens: list[dict[str, Any]] = []
    could_not_see: list[str] = []
    try:
        out = ""
        if cmd is not None:
            rc, out, cerr = await _run(
                cmd, _CAPTURE_TIMEOUT, env=await asyncio.to_thread(_script_env)
            )
        if kind in ("repo", "local"):
            cap: dict[str, Any] = {}
            if cmd is not None:
                try:
                    cap = json.loads(out)
                except ValueError as exc:
                    raise RuntimeError("could not render the selected screens") from exc
            by_route = {str(s.get("route")): s for s in (cap.get("screens") or [])}
            for i, ref in enumerate(refs):
                # A covered pick is served from its adopted copy (viewport, not
                # --full) at its original step; the rest are mapped by route from the
                # fresh capture, and anything neither adopted nor captured is not
                # seeable.
                copy = adopted_paths.get(ref)
                if copy:
                    screens.append({"step": i + 1, "label": labels[i], "path": copy})
                    continue
                s = by_route.get(ref)
                if s and s.get("path"):
                    screens.append({"step": i + 1, "label": labels[i], "path": s["path"]})
                else:
                    could_not_see.append(labels[i])
            if cap.get("blockedBy"):
                could_not_see.append("a login or consent gate blocked some screens")
        else:  # url
            step = 0
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                step += 1
                if rec.get("ok") and rec.get("file"):
                    screens.append(
                        {
                            "step": step,
                            "label": labels[min(step - 1, len(labels) - 1)],
                            "path": rec["file"],
                        }
                    )
                else:
                    could_not_see.append(str(rec.get("label") or rec.get("route") or "a page"))
        return {"screens": screens, "couldNotSee": could_not_see}
    finally:
        # A dc-render-* dir is referenced by history ONLY when a screen succeeded —
        # every returned path lives in out_dir, adopted copies included. On any
        # no-screen exit (failed capture, empty result) nothing references it and the
        # TTL sweep skips dc-render-*, so drop it here to keep repeated failures from
        # exhausting upload storage.
        if not screens:
            await asyncio.to_thread(shutil.rmtree, out_dir, ignore_errors=True)


async def _handle_render(request: web.Request) -> web.Response:
    body, err = await _json_object(request)
    if body is None:
        return err or _bad_request("invalid JSON", "invalid_json")
    kind = str(body.get("kind") or "").strip()
    value = str(body.get("value") or "").strip()
    handle = str(body.get("handle") or "").strip()
    picks = body.get("picks")
    if not isinstance(picks, list) or not picks:
        return _bad_request("picks must be a non-empty array", "missing_picks")
    if not all(isinstance(p, dict) for p in picks):
        return _bad_request("each pick must be an object", "bad_pick")
    if len(picks) > _MAX_PICKS:
        return _bad_request("too many selections", "too_many_picks")

    node = await asyncio.to_thread(_node)
    if node is None:
        return _bad_request("node is not installed on this machine", "node_missing")

    refs = [str(p.get("ref") or p.get("id") or "") for p in picks]
    labels = [str(p.get("label") or "").strip() or _label_for(refs[i]) for i, p in enumerate(picks)]

    # A NUL in any string that becomes a subprocess argv entry makes
    # create_subprocess_exec raise ValueError (HTTP 500). Refuse up front.
    if any("\x00" in s for s in (*refs, value, handle)):
        return _bad_request("a selection contains an invalid character", "bad_ref")
    if (
        len(value) > _MAX_FIELD_LEN
        or len(handle) > _MAX_FIELD_LEN
        or any(len(r) > _MAX_FIELD_LEN for r in refs)
    ):
        return _bad_request("a field is too long", "field_too_long")

    _sweep_jobs()

    # Resolve the render target and build the capture command SYNCHRONOUSLY so every
    # bad-input 400 (bad/expired handle, protected path, internal URL, bad kind) is
    # returned before a job starts and before any output dir is created. Only the
    # capture subprocess itself runs detached, in _render_capture_job.
    if kind in ("repo", "local"):
        try:
            if kind == "repo":
                # `handle` comes from the client; a crafted "../.." must not let the
                # render escape the clones dir. Resolve and require containment.
                # resolve()/_clones_dir()'s mkdir touch the filesystem, so run them
                # off the event loop (a stale NFS/UNC mount must not freeze it).
                clones = await asyncio.to_thread(lambda: _clones_dir().resolve())
                directory = await asyncio.to_thread(lambda: (clones / handle).resolve())
                if not directory.is_relative_to(clones):
                    return _bad_request("invalid clone handle", "bad_handle")
            else:
                directory = Path(
                    handle[len("local:") :] if handle.startswith("local:") else value
                ).expanduser()
                if await asyncio.to_thread(_is_sensitive_dir, directory):
                    return _bad_request(
                        "that path is protected and can't be read.", "protected_path"
                    )
            exists = await asyncio.to_thread(directory.exists)
        except OSError:
            # An overlong/invalid path can make resolve()/exists() raise; treat it
            # as an expired handle rather than a 500.
            return _bad_request(
                "the discovered project is no longer available; run discovery again",
                "handle_expired",
            )
        if not exists:
            return _bad_request(
                "the discovered project is no longer available; run discovery again",
                "handle_expired",
            )

        # Reuse the discovery probe's PNGs where we can. The probe cached a
        # route->PNG map at /discover; reuse a cached PNG for any picked route it
        # captured (whose file still exists) — but only when the build output the
        # probe served still carries the token recorded then, so a rebuild between
        # /discover and /render is re-captured rather than served a stale image.
        # Reused PNG paths come from the probe dir the server created under uploads,
        # so they need no extra path validation beyond the exists() check. Every
        # filesystem touch goes through asyncio.to_thread, like the rest of this
        # handler, so a stale NFS/UNC mount cannot freeze the gateway loop. This
        # lookup runs AFTER all the synchronous validations above (NUL,
        # field-length, handle containment / sensitive dir, exists), never before.
        #
        # The key is derived from the target that was just VALIDATED, never from the
        # raw handle. A local render takes `directory` from `value` whenever the
        # handle is not a "local:<path>" one, so keying off the handle there would
        # read a different project's entry and hand back its screenshots. A repo
        # render derives `directory` from the handle itself and containment-checks
        # it, so for repos the handle IS the target. /discover records local entries
        # under exactly this "local:<expanded path>" spelling.
        cache_key = handle if kind == "repo" else f"local:{directory}"
        reused: dict[str, str] = {}
        cached = _probe_get(cache_key)
        if cached is not None:
            token = await asyncio.to_thread(_served_signature, Path(cached["build_dir"]))
            # A missing token means "unknown", not "unchanged", so it must not satisfy
            # the match: a build output that no longer stats is no evidence it stands
            # still. Only the digest is compared — newest_mtime_ns is a discover-time
            # concern. The stored side is never None: _probe_put is only reached with a
            # real digest.
            if token is not None and cached["served_signature"] == token.digest:
                cached_routes = cached["routes"]
                for ref in refs:
                    if not ref:
                        continue
                    png = cached_routes.get(ref)
                    if png and await asyncio.to_thread(os.path.exists, png):
                        reused[ref] = png

        # An out dir is created either way: the reused PNGs are adopted into it so
        # every path this render hands the client (and history keeps) lives in the
        # never-swept dc-render-* dir.
        uploads_base = await asyncio.to_thread(_uploads_dir)
        out_dir = uploads_base / f"dc-render-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)

        # Adopt BEFORE deciding what is uncovered, so a copy that fails falls back to a
        # fresh capture instead of losing the pick. The probe dir can be swept or
        # rmtree'd by a concurrent local re-discovery in the window after the
        # os.path.exists check above, and a pick whose adoption fails is simply a pick
        # nothing has yet rendered — the same situation as one the probe never covered.
        # Treating it as "could not see" instead would make a render that always
        # worked before return zero screens. A copy is milliseconds and goes through
        # to_thread like every other filesystem touch here.
        copies = await asyncio.to_thread(_adopt_reused, reused, out_dir)
        uncovered = [r for r in refs if r and r not in copies]

        # A capture command is built only when something is actually uncovered; None
        # means every pick is already adopted in out_dir, so no node subprocess runs.
        capture_cmd: list[str] | None = None
        if uncovered:
            capture_cmd = [
                node,
                str(_SCRIPTS_DIR / "capture-build.mjs"),
                str(directory),
                f"--routes={','.join(uncovered)}",
                f"--out={out_dir}",
                "--full",
            ]
        return web.json_response(
            {
                "job": _start_job(
                    lambda: _render_capture_job(kind, capture_cmd, out_dir, refs, labels, copies)
                )
            }
        )
    elif kind == "url":
        base = value or (handle[len("url:") :] if handle.startswith("url:") else "")
        routes = [r for r in refs if r]
        # capture-site wants same-origin route paths under one base; if a pick is a
        # full URL use it as the base with a single "/" route.
        if len(routes) == 1 and routes[0].startswith("http"):
            base, routes = routes[0], ["/"]
        vetted = await _resolve_vetted(base)
        if vetted is None:
            return _bad_request(
                "that URL can't be rendered (internal/private hosts are blocked)", "bad_url"
            )
        # Pin the browser's DNS for the base host to a vetted IP so a rebinding name
        # cannot resolve to an internal address on Chromium's own lookup (mirrors
        # the git-clone curloptResolve pin).
        bhost = urlparse(base).hostname or ""
        uploads_base = await asyncio.to_thread(_uploads_dir)
        out_dir = uploads_base / f"dc-render-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
        cmd = [
            node,
            str(_SCRIPTS_DIR / "capture-site.mjs"),
            f"--base={base}",
            f"--routes={','.join(routes) or '/'}",
            f"--out={out_dir}",
            "--full",
        ]
        if bhost:
            cmd.append(f"--resolve={bhost}:{','.join(vetted)}")
    else:
        return _bad_request(f"cannot render kind: {kind}", "bad_kind")

    return web.json_response(
        {"job": _start_job(lambda: _render_capture_job(kind, cmd, out_dir, refs, labels))}
    )


# ── GET /discover?job= and /render?job= ──


async def _handle_job_status(request: web.Request) -> web.Response:
    job_id = request.query.get("job", "")
    rec = _get_job(job_id)
    if rec is None:
        return web.json_response(
            {"status": "error", "error": "unknown job", "code": "unknown_job"}, status=404
        )
    if rec["status"] == "error":
        return web.json_response({"status": "error", "error": rec["error"]})
    return web.json_response({"status": rec["status"], "result": rec["result"]})


# ── Registration ──


def register_routes(app: web.Application) -> None:
    """Register Design Critique routes on the gateway's aiohttp Application."""
    app.router.add_get(f"{_PREFIX}/method", _require_enabled(_handle_method))
    app.router.add_post(f"{_PREFIX}/discover", _require_enabled(_handle_discover))
    app.router.add_post(f"{_PREFIX}/render", _require_enabled(_handle_render))
    # GET with ?job=<id> polls a detached discover/render job started by the POST.
    app.router.add_get(f"{_PREFIX}/discover", _require_enabled(_handle_job_status))
    app.router.add_get(f"{_PREFIX}/render", _require_enabled(_handle_job_status))
