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
from typing import Any
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
    for child in victims:
        try:
            if now - child.stat().st_mtime > _CLONE_TTL_SEC:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


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
        for s in probe.get("screens") or []:
            seeable[str(s.get("route"))] = True
        if probe.get("blockedBy"):
            b = probe["blockedBy"]
            cannot_see.append(
                f"{b.get('onScreens', '')} of {b.get('ofScreens', '')} screens blocked by a {b.get('likely', 'gate')}."
            )
        if probe.get("buildDir") is None and probe.get("notes"):
            cannot_see.extend(str(n) for n in probe["notes"])
        # The probe images are throwaway (only the manifest is used); drop the dir
        # now rather than wait for the TTL sweep — off the event loop, since a
        # recursive delete is blocking IO.
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


async def _render_capture_job(
    kind: str,
    cmd: list[str],
    out_dir: Path,
    refs: list[str],
    labels: list[str],
) -> dict[str, Any]:
    """Run the capture subprocess (detached) and shape its output for the client.

    The synchronous validation and the SSRF/handle checks already ran in the
    handler; this only executes the vetted command and parses it, so a client
    disconnect cannot cancel the capture.
    """
    screens: list[dict[str, Any]] = []
    could_not_see: list[str] = []
    try:
        rc, out, cerr = await _run(cmd, _CAPTURE_TIMEOUT, env=await asyncio.to_thread(_script_env))
        if kind in ("repo", "local"):
            try:
                cap = json.loads(out)
            except ValueError as exc:
                raise RuntimeError("could not render the selected screens") from exc
            by_route = {str(s.get("route")): s for s in (cap.get("screens") or [])}
            for i, ref in enumerate(refs):
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
        # A dc-render-* dir is referenced by history ONLY when a screen succeeded.
        # On any no-screen exit (failed capture, empty result) nothing references
        # it and the TTL sweep skips dc-render-*, so drop it here to keep repeated
        # failures from exhausting upload storage.
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
        csv = ",".join(r for r in refs if r)
        uploads_base = await asyncio.to_thread(_uploads_dir)
        out_dir = uploads_base / f"dc-render-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
        cmd = [
            node,
            str(_SCRIPTS_DIR / "capture-build.mjs"),
            str(directory),
            f"--routes={csv}",
            f"--out={out_dir}",
            "--full",
        ]
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
