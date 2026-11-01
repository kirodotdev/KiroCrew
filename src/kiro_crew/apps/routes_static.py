"""An installed app's UI bundle, and the dev-mode toggle that picks its UI root.

``handle_app_ui_file`` with the bounded, pinned file open and the streaming it uses,
the content types the UI and art routes answer with, and ``handle_app_dev_mode``.

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from email.utils import formatdate
from pathlib import Path

from aiohttp import web

from kiro_crew.apps.dev_mode import dev_mode_granted_root, is_dev_mode_cached
from kiro_crew.apps.manager import apps_dir
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.conditional_get import is_not_modified
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    fd_real_path,
    is_reparse_point,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


# ---------------------------------------------------------------------------
# Static file serving for app UI bundles
# ---------------------------------------------------------------------------

_ALLOWED_EXTENSIONS = frozenset(
    {
        ".mjs",
        ".js",
        ".css",
        ".json",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".woff",
        ".woff2",
        ".ttf",
        ".map",
    }
)

_CONTENT_TYPES = {
    ".mjs": "application/javascript",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json",
}


#: Ceiling on one UI-bundle file this route will serve. With streaming (see
#: :func:`handle_app_ui_file`) the ceiling does not bound gateway memory —
#: per-request memory is one :data:`_UI_STREAM_CHUNK` regardless of file size —
#: it bounds the WORK one unauthenticated request can command (bytes read and
#: sent per request; the route bypasses token auth). Measured reality: the
#: largest UI asset a bundled app in this tree ships is ~9 KB
#: (``website/public/apps/agent-worlds/ui/index.mjs`` — the scaffold's vite
#: config externalizes react/react-dom/the SDK, so bundles stay small). 8 MiB
#: is ~900x that, matches the posture already accepted for ``_ART_MAX_BYTES``
#: in routes_art.py, and still clears any plausible self-bundled entry chunk. The one
#: class it can refuse is the sourcemap of an app that bundles a very heavy
#: dependency — a ``.map`` 404 degrades only devtools debugging of that app,
#: never the app itself.
_UI_MAX_BYTES = 8 * 1024 * 1024

#: Read granularity when streaming a UI file from its validated descriptor.
#: This — not the file size — is what one in-flight request pins in memory, so
#: N slow clients cost N chunks, not N files. 256 KiB keeps the thread-hop
#: count low (an 8 MiB worst case is 32 hops) while staying far below any
#: amplification concern.
_UI_STREAM_CHUNK = 256 * 1024

#: Max concurrent requests HOLDING AN OPEN DESCRIPTOR on this route. Acquired
#: before `_open_ui_file` and released after the descriptor closes: bounding
#: only the streaming loop would let every QUEUED request already hold an fd
#: while waiting for a slot, so slow-paced unauthenticated GETs could walk the
#: gateway to `EMFILE`. Under this scope at most 8 descriptors exist at once
#: and everyone else waits fd-less. It also bounds the per-chunk `to_thread`
#: hops on the SHARED default executor (same reason `_BLOB_FETCH_SEMAPHORE`
#: bounds git fetches). Refusals and body-less 304s hold a slot only for
#: microseconds; 8 comfortably covers a dashboard loading assets in parallel.
_UI_STREAM_SEMAPHORE = asyncio.Semaphore(8)

#: Wall-clock ceiling on the body-writing phase of one UI-file response, and so
#: on how long one client can hold a `_UI_STREAM_SEMAPHORE` permit while pacing
#: the writes itself. Without a deadline the 8 permits are a head-of-line queue
#: an UNAUTHENTICATED caller controls: 8 sockets that connect, take one chunk
#: and then stop reading pin every permit (and descriptor) for as long as they
#: stay connected, and every app UI on the host stops loading. The default
#: matches `_BLOB_FETCH_TIMEOUT` in routes_art.py — 30s is the ceiling the
#: app routes treat as a dead peer — and it is ~100x the budget a real transfer
#: needs: `_UI_MAX_BYTES` is 8 MiB, so even the largest servable file finishes
#: inside it at ~280 KB/s, over a loopback connection to the dashboard. Expiry
#: stops the write loop; the enclosing `finally` still closes the descriptor, so
#: the permit is released. An operator whose link is slower than that raises the
#: deadline with `agent.apps_ui_stream_timeout_secs` (clamped to [5, 600] at
#: load time); this constant is what a config that cannot be read falls back to.
#: The knob has no off switch on purpose — an unbounded permit IS the wedge
#: described above.
_UI_STREAM_TIMEOUT_DEFAULT = 30  # seconds


def _ui_stream_timeout_secs() -> int:
    """The operator's body-transfer deadline for this route, in seconds.

    Reads `agent.apps_ui_stream_timeout_secs`, which the config loader clamps to
    [5, 600]. The bounds are NOT re-checked here — one owner for them — but a
    value that is not a positive whole number is refused, because the only
    alternative to a usable deadline on this route is no deadline at all, and
    that is the unauthenticated head-of-line wedge `_UI_STREAM_TIMEOUT_DEFAULT`
    documents.

    Blocking: a config-cache MISS reads and validates `config.json`, so callers
    resolve this through `asyncio.to_thread` and never on the event loop.
    """
    try:
        value = getattr(
            KiroCrewConfig.load().agent,
            "apps_ui_stream_timeout_secs",
            _UI_STREAM_TIMEOUT_DEFAULT,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable config keeps the default
        logger.warning(
            "agent.apps_ui_stream_timeout_secs: config load failed (%s); "
            "serving this UI file with the %ds default deadline",
            exc,
            _UI_STREAM_TIMEOUT_DEFAULT,
        )
        return _UI_STREAM_TIMEOUT_DEFAULT
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        logger.warning(
            "agent.apps_ui_stream_timeout_secs=%r is not a positive whole number "
            "of seconds; serving this UI file with the %ds default deadline",
            value,
            _UI_STREAM_TIMEOUT_DEFAULT,
        )
        return _UI_STREAM_TIMEOUT_DEFAULT
    return value


def _open_ui_file(name: str, file_path: str) -> tuple[int, os.stat_result] | str:
    """An OPEN validated descriptor for *file_path* under *name*'s ui/ root
    plus its ``fstat``, or a refusal code: ``"invalid"`` (containment failure
    -> 400, the status this route has always answered escapes with) or
    ``"not_found"`` (-> 404). On the tuple path the CALLER owns closing the fd.

    Handing back the descriptor rather than a path is the security-relevant
    part, and it is the same fix :func:`_read_declared_art` carries:
    validating a path and then handing it to ``FileResponse`` opens it a SECOND
    time, so the app that owns this directory can swap a validated name for a
    symlink between the check and that open and have the gateway read the
    target instead — and the gateway is NOT sandboxed, so this launders a read
    the app's own code can be refused. This route serves the SAME app-owned
    directory with a BROADER extension allowlist (``.json``, ``.mjs``), so it
    must not keep the weaker open. One open, validated as a DESCRIPTOR, is the
    only shape without that window; everything after it reads the fd, which
    cannot be re-pointed.

    One thread hop for the whole decision — resolve, containment, open and
    stat — because every part is a blocking syscall and the gateway runs on one
    event loop (``no-blocking-call-on-event-loop``).
    """
    ui_root = apps_dir() / name / "ui"
    target = ui_root / file_path
    try:
        # Resolved ONCE, here, because `pin_parent` requires the CALLER to do
        # it: resolving inside would re-follow whatever an ancestor points at by
        # then. The ui root itself is resolved THROUGH: the documented dev-mode
        # setup links ui/ at the developer's source tree, so the ROOT being a
        # link is legitimate — containment is proven against wherever it really
        # lands, not against the link's own name.
        resolved_root = Path(os.path.realpath(ui_root))
        # But a root that lands OUTSIDE the app's own install directory is only
        # legitimate under the OPERATOR's dev-mode grant. Without this check an
        # app could ship `ui` as a symlink to a credential directory
        # (`ui -> ~/.docker`) and have this UNAUTHENTICATED route (the
        # `/apps/{name}/ui/` token-auth bypass) serve `config.json` — `.json`
        # is in the allowlist — laundering a read the app's own sandboxed code
        # is refused. `dev_mode_granted_root`, not `is_dev_mode`: the latter
        # reads only the app's own writable `installed.json`, which the app
        # could edit to authorize itself — the grant is the operator record
        # at the apps ROOT (written by the dev-mode toggle, never by the
        # startup reconcile, and refused outright for sensitive targets), and
        # it BINDS the specific resolved root granted: the current root must
        # EQUAL it, so a grant left behind by a crash, an update, or a
        # reinstall authorizes only the exact tree the operator approved,
        # never wherever `ui` points now. The disk reads are paid only on
        # this exceptional path.
        #
        # The containment ANCHOR resolves only the gateway-owned apps root
        # and then appends the literal `name` — it must NOT re-resolve
        # through the app's own entry (`realpath(apps_dir()/name)`): the two
        # realpath calls in this function would then race, and an app
        # alternating its install entry between them could get an escaping
        # `resolved_root` accepted as "inside the install". The apps root
        # itself is not app-writable, so this anchor cannot be swapped; an
        # install entry that IS a link makes its resolved ui root land
        # outside this anchor and take the grant path like any other escape.
        resolved_install = Path(os.path.realpath(apps_dir())) / name
        try:
            resolved_root.relative_to(resolved_install)
        except ValueError:
            if str(resolved_root) != dev_mode_granted_root(name):
                return "invalid"
        resolved_parent = Path(os.path.realpath(target.parent))
        # The PARENT containment is load-bearing, not belt-and-braces:
        # `pin_parent`'s contract is that a component swapped BEFORE parent
        # resolution is followed by that resolution, so an already-symlinked
        # ancestor is caught only here.
        resolved_parent.relative_to(resolved_root)
        # The full path too: a link at the FINAL name whose target escapes the
        # root is answered 400 like every other escape (the contract this route
        # has always had). This is a pre-check, not the enforcement — the pinned
        # open below refuses ANY link at the final name, escaping or not.
        Path(os.path.realpath(target)).relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        # `RuntimeError` because path resolution raises THAT — not an OSError —
        # on a symlink loop, and the app that plants one is the app whose UI
        # this serves.
        return "invalid"

    # `O_NONBLOCK` is what makes the descriptor checks REACHABLE, not an
    # optimisation: opening a FIFO blocks until a writer appears, and this runs
    # inside `asyncio.to_thread`, so an app shipping a FIFO as a UI asset would
    # park a thread-pool worker forever. With the flag the open returns
    # immediately and `S_ISREG` below refuses it. On a plain file it changes
    # nothing. Same rationale as `_read_declared_art`.
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
                what="app UI bundle file",
            )
        except (PinnedPathRefusal, OSError, ValueError):
            # `ValueError` is NOT redundant with `OSError`: `os.open` raises it
            # — never an OSError — for a name the OS layer cannot encode (an
            # embedded NUL, a lone surrogate). Such a name survives every
            # earlier check: the extension allowlist reads the suffix AFTER the
            # bad byte, and containment resolves the PARENT, which is clean when
            # the bad byte sits in the final component.
            return "not_found"
    else:
        # Windows: no `O_NOFOLLOW` and no descriptor-relative open, so the
        # pinned walk is unavailable. An unprivileged process there cannot
        # create a FILE symlink at all, so the reachable swap is a junction —
        # refused by the reparse-point probe. Same degradation as the art
        # route; the ui-root link (dev mode) sits ABOVE the resolved root and
        # is deliberately outside the screen. The probe is name-based, so a
        # junction planted BETWEEN the probe and the open would still be
        # followed — which is why the DESCRIPTOR's own final path is
        # validated below, after the open, closing the residual window on
        # the descriptor rather than the name.
        try:
            if any(
                is_reparse_point(p)
                for p in (target, *target.parents)
                if p == resolved_root or resolved_root in p.parents or p == target
            ):
                return "not_found"
            fd = os.open(target, flags)
        except (OSError, ValueError):
            return "not_found"
        # Race-free containment on the OPENED handle: resolve the descriptor's
        # final path (`GetFinalPathNameByHandleW` there; /proc/F_GETPATH on
        # the POSIX hosts the tests run on) and require it to still sit under
        # the resolved root. Fail closed when it cannot be read — on this
        # branch the descriptor is the only trustworthy witness.
        fd_real = fd_real_path(fd)
        if fd_real is None:
            os.close(fd)
            return "not_found"
        try:
            Path(fd_real).relative_to(resolved_root)
        except ValueError:
            os.close(fd)
            return "not_found"

    try:
        st = os.fstat(fd)
        # `st_nlink != 1` is the one gate that can see a HARDLINK: the alias
        # shares the target's inode, so `is_symlink()` is False, `realpath`
        # yields the alias's own name (containment passes), and `O_NOFOLLOW`
        # has no link to refuse. Checked on the DESCRIPTOR, which is what makes
        # it race-free. Inline rather than `pinned_fs.refuse_hardlink_alias`
        # for the same double-close reason `_read_declared_art` documents.
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > _UI_MAX_BYTES:
            os.close(fd)
            return "not_found"
    except OSError:
        os.close(fd)
        return "not_found"
    return fd, st


async def handle_app_ui_file(request: web.Request) -> web.StreamResponse:
    """GET /apps/{name}/ui/{path:.*} — serve app UI bundle files.

    Serves bytes STREAMED from a pinned descriptor (see :func:`_open_ui_file`)
    rather than handing a validated path to ``FileResponse``, which re-opens it
    and re-introduces the check-then-reopen window the art route also closes.
    Streaming rather than buffering is itself load-bearing: this route
    is UNAUTHENTICATED (the ``/apps/{name}/ui/`` token-auth bypass), so a
    buffered body would let N outstanding requests each pin a whole file in
    gateway memory — with streaming, per-request memory is one chunk
    (:data:`_UI_STREAM_CHUNK`) regardless of file size or client speed.
    Behaviour contract preserved: 400 on ``..``/absolute/escaping paths, 403 on
    a disallowed extension, 404 on a missing file, Content-Type from
    ``_CONTENT_TYPES``, and conditional requests (If-None-Match /
    If-Modified-Since) still answer a body-less 304.
    """
    name = request.match_info["name"]
    file_path = request.match_info.get("path", "")
    if ".." in file_path or file_path.startswith("/"):
        return web.json_response({"error": "invalid path"}, status=400)
    ext = Path(file_path).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return web.json_response({"error": f"file type {ext!r} not allowed"}, status=403)
    # The semaphore is acquired BEFORE the descriptor exists and released only
    # after it is closed. Bounding just the streaming loop would cap streams at
    # 8 while every QUEUED request already held an open fd waiting for a slot —
    # an unauthenticated client could then drive the gateway to `EMFILE` with
    # slow-paced GETs. Under this scope, at most 8 requests hold a descriptor
    # at any instant and everyone else waits fd-less. The refusal paths inside
    # (400/403/404, body-less 304) hold their slot only microseconds.
    async with _UI_STREAM_SEMAPHORE:
        result = await asyncio.to_thread(_open_ui_file, name, file_path)
        if result == "invalid":
            return web.json_response({"error": "invalid path"}, status=400)
        if isinstance(result, str):
            return web.json_response({"error": "not found"}, status=404)
        fd, st = result
        try:
            content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
            # Dev-mode apps: never cache — the file-watch live-reload reloads on
            # every change and must always see the latest bytes. `is_dev_mode_cached`
            # is the watcher-maintained in-memory flag, so this hot path does NO
            # disk IO on the event loop for the mode lookup
            # (no-blocking-call-on-event-loop). Everything else: no-cache (NOT
            # no-store) — the browser may cache but MUST revalidate each load. The
            # validators are derived from the DESCRIPTOR being served, not a second
            # stat of the path, so unchanged files stay a body-less 304 while app
            # updates are picked up on a plain refresh. A long
            # ``public,max-age=...`` instead would serve an app's UI stale for that
            # whole window after an update.
            cache = "no-store" if is_dev_mode_cached(name) else "no-cache"
            etag_value = f"{st.st_ino:x}-{st.st_size:x}-{st.st_mtime_ns:x}"
            # `nosniff` because the Content-Type is derived from the EXTENSION, not
            # the bytes; the CSP neuters a scripted `.svg` opened as a TOP-LEVEL
            # document on the dashboard's own origin (a response CSP does not apply
            # when the bytes are consumed as a subresource, so module/style/img
            # loads are unaffected). Same pair, same reasons, as the art route.
            headers = {
                "Cache-Control": cache,
                "ETag": f'"{etag_value}"',
                "Last-Modified": formatdate(st.st_mtime, usegmt=True),
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
                # Read by app frames (apps/app_frame.py): an
                # opaque-origin sandbox loads the app's module in CORS mode. Only
                # the "null" origin is answered, never with credentials, and these
                # bytes are already served to anyone without a token.
                "Vary": "Origin",
            }
            if request.headers.get("Origin") == "null":
                headers["Access-Control-Allow-Origin"] = "null"
            # The shared compare reads aiohttp's parsed accessors, not raw header
            # strings: If-None-Match may carry a list, a weak `W/"..."` form, or
            # `*` (RFC 9110 §13.1.2 weak comparison), and If-Modified-Since is
            # evaluated only when no If-None-Match was sent (§13.1.3), with the
            # HTTP-date parsing that forces UTC (a raw `parsedate_to_datetime`
            # hands back a NAIVE datetime for `-0000`/asctime forms, which
            # `.timestamp()` then reads as server-LOCAL time — a stale 304 for
            # up to a whole UTC offset after an app update). Mirrors what
            # `FileResponse` did.
            if is_not_modified(request, headers["ETag"], last_modified=st.st_mtime):
                return web.Response(status=304, headers=headers)
            # Resolved HERE rather than at import: the operator's deadline is read
            # per request so an edit applies without a gateway restart. Off the
            # event loop because a config-cache miss reads and validates
            # `config.json` (no-blocking-call-on-event-loop), and after the
            # validator checks above so a refusal or a body-less 304 never pays
            # the hop. The hop is inside the `_UI_STREAM_SEMAPHORE` scope, so it
            # is bounded on the shared default executor like the read hops below.
            stream_timeout = await asyncio.to_thread(_ui_stream_timeout_secs)
            resp = web.StreamResponse(status=200, headers={**headers, "Content-Type": content_type})
            # Length pinned to the fstat that was validated: a file the app GROWS
            # after the open must not stream past the length the client was told,
            # so the loop below caps at `remaining` as well as EOF.
            resp.content_length = st.st_size
            try:
                await resp.prepare(request)
                remaining = st.st_size
                # The enclosing `_UI_STREAM_SEMAPHORE` scope (acquired before the
                # open, released after the close) is what bounds this loop's
                # `to_thread` hops on the shared default executor — no second
                # acquisition here: a nested acquire under the same semaphore
                # would deadlock once 8 holders each waited for a 9th permit.
                # Bounded by wall clock as well as by `remaining`. The permit is
                # held across this loop, so a client that stops draining its
                # socket keeps it (and the descriptor) for as long as it stays
                # connected, and 8 such clients wedge this route for every app
                # UI on the host. The open stays OUTSIDE this scope on purpose:
                # cancelling a `to_thread` call cannot recall a descriptor the
                # worker thread already opened, so a deadline around the open
                # would leak the fd it is meant to protect.
                async with asyncio.timeout(stream_timeout):
                    while remaining > 0:
                        chunk = await asyncio.to_thread(
                            os.read, fd, min(_UI_STREAM_CHUNK, remaining)
                        )
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        await resp.write(chunk)
                    await resp.write_eof()
            except TimeoutError:
                # The deadline expired mid-body. Headers are already sent, so
                # stop writing and drop the connection: the client is left with
                # a body short of the announced `Content-Length`, which is the
                # right outcome for a reader that stopped accepting bytes.
                # Abort discards buffered writes before aiohttp runs its
                # post-handler `write_eof`; `force_close` only disables keep-alive.
                transport = request.transport
                if transport is not None and not transport.is_closing():
                    transport.abort()
                resp.force_close()
            except (ConnectionResetError, ConnectionAbortedError):
                # A tab can close at any response boundary. The descriptor is
                # still closed by the shielded finally below; the disconnect is
                # routine client behavior, not an application error.
                pass
            return resp
        finally:
            # Off the loop: `os.close` is on the no-blocking-call-on-event-loop
            # deny list (it can block in the kernel), and this `finally` runs on
            # the loop for every request, error paths included. `shield` is
            # load-bearing, not decoration: a client disconnect CANCELS this
            # handler, and an unshielded `to_thread` awaited during cancellation
            # can have its work item cancelled while still queued — the close
            # never runs and the descriptor leaks, on an UNAUTHENTICATED route
            # where repeated connect-then-drop would walk the gateway into
            # RLIMIT_NOFILE. Shielded, the close task runs to completion even
            # when this await is interrupted.
            await asyncio.shield(asyncio.to_thread(os.close, fd))


async def handle_app_dev_mode(request: web.Request) -> web.Response:
    """POST /api/apps/{name}/dev — toggle dev mode (body: {"enabled": bool}).

    Metadata-only change (installed.json); the dev-mode watcher picks it up
    within one poll interval, so no gateway restart is needed.

    This route deliberately carries NO way to confirm an out-of-install ui
    root: app UI bundles run as same-origin modules with the dashboard's own
    credentials, so any request-body confirmation flag would be data the app
    controls, not operator attestation — an app could self-grant serving an
    arbitrary non-sensitive directory over the unauthenticated UI route by
    POSTing to its own toggle. Enabling dev mode on such a root therefore
    always answers 400 here (``code:
    "dev_mode_out_of_install_confirmation_required"``, naming the CLI
    command); confirmation is supplied only from the gateway host via
    ``kirocrew app dev <name> --confirm-out-of-install-root``.
    """
    from kiro_crew.apps.dev_mode import set_dev_mode
    from kiro_crew.dashboard.handlers._shared import body_not_object_error

    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    not_object = body_not_object_error(body)
    if not_object is not None:
        return not_object
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)
    # set_dev_mode does blocking filesystem IO (reads/writes installed.json and
    # the dev sentinel) — offload it so the gateway event loop never stalls.
    result = await asyncio.to_thread(set_dev_mode, name, enabled)
    if "error" in result:
        if result.get("code") == "locked_by_policy":
            return web.json_response(
                {"error": result["error"], "code": "locked_by_policy"}, status=409
            )
        return web.json_response(result, status=404 if "not installed" in result["error"] else 400)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="app_dev_mode",
        outcome="ok",
        resources=f"{name} enabled={enabled}",
    )
    return web.json_response(result)
