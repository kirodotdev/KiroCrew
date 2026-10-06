"""The React build's static routes.

The per-request ``static/dist`` resolution and its confinement, the build subdirectory
prefixes, and the app window entries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _APP_WINDOWS_SUBDIR,
        APP_WINDOW_URL_PREFIX,
        _vendor_preflight_handler,
        logger,
        register_app_window_paths,
    )


def discover_app_window_entries(windows_root: Path) -> list[tuple[str, Path]]:
    """Enumerate app window entries as ``(route_path, file)``.

    An app ships standalone HTML windows as ``<windows_root>/<app>/<name>.html``
    and they are served at ``/app-windows/<app>/<name>.html`` — the same two
    segments, so the URL and the file agree by construction.

    An earlier revision served them FLAT at ``/<app>-<name>.html``, which is
    ambiguous the moment either name contains a hyphen: app ``foo`` + window
    ``bar-baz`` and app ``foo-bar`` + window ``baz`` both spell
    ``/foo-bar-baz.html``. That cost two pieces of machinery — a collision
    refusal here, and a middleware in ``vite.config.ts`` that guessed the split
    by trying each hyphen position, which could resolve to the WRONG file rather
    than refuse. Keeping the boundary in the URL deletes the whole class, so
    neither exists any more. The duplicate check below is retained as a cheap
    invariant: with distinct path segments the filesystem cannot produce two
    identical routes, so a hit means the convention changed under us.

    Returned paths come from the enumerated FILES; the request path is never used
    to build a filesystem path, so there is no traversal surface.
    """
    if not windows_root.is_dir():
        return []
    root = windows_root.resolve()
    out: list[tuple[str, Path]] = []
    claimed: dict[str, Path] = {}
    for entry in sorted(windows_root.glob("*/*.html")):
        # Confine the enumerated file to the build tree. The glob cannot walk out
        # on its own, but a symlink planted inside `dist/` could, and this function
        # hands every result to `web.FileResponse` — an unconditional read of
        # whatever the path points at. Resolving and comparing also makes the
        # barrier visible to dataflow analysis, which reported this join as a path
        # injection precisely because the safety was structural rather than stated.
        resolved = entry.resolve()
        if root not in resolved.parents:
            logger.error(
                "App window entry %s resolves outside the build tree (%s) — refusing "
                "to serve it.",
                entry,
                root,
            )
            continue
        route_path = f"/{APP_WINDOW_URL_PREFIX}/{entry.parent.name}/{entry.stem}.html"
        prior = claimed.get(route_path)
        if prior is not None:  # pragma: no cover - unreachable by construction
            logger.error(
                "App window entry %s collides with %s on route %s — refusing to "
                "register the second. Two files cannot share this route, so the "
                "path convention has drifted.",
                entry,
                prior,
                route_path,
            )
            continue
        claimed[route_path] = resolved
        out.append((route_path, resolved))
    return out


def _window_entry_handler(
    dist_dir: Path, entry: str
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """A handler that serves ONE enumerated window file, ``src/apps/<entry>``.

    The file is resolved through ``dist_dir`` per request
    (:func:`_resolve_dist_file`), like every other build route, so a staging
    step that re-points ``static/dist`` does not leave the window on a tree that
    has since been swept.

    A factory rather than the usual default-argument idiom
    (``async def h(req, _file=entry)``). Both avoid the late-binding capture bug
    in a loop, but the default-argument form puts the path in a REQUEST
    HANDLER'S SIGNATURE — so it reads, to a human and to dataflow analysis
    alike, as something a request could supply, and `py/path-injection` flagged
    it as exactly that. Here the path is a closure cell fixed at registration and
    the handler takes only the request, which is what is actually true: these
    routes are built from files enumerated at startup and the request path never
    reaches the filesystem.
    """

    async def _serve(_request: web.Request) -> web.StreamResponse:
        return await _serve_dist_file(dist_dir, _APP_WINDOWS_SUBDIR, entry)

    return _serve


def _resolve_dist_file(dist_dir: Path, subdir: str, tail: str) -> Path | None:
    """The file ``tail`` names under ``dist_dir/subdir``, or ``None``.

    ``dist_dir`` is resolved HERE, per request, not at registration: on a
    source checkout ``static/dist`` is a link that staging re-points (to
    ``website/dist``, or to a fresh immutable copy), and a route resolved once at
    startup would keep serving the old target while ``index.html`` -- read
    through ``static/dist`` per request -- references the new one's chunks.

    Confined like aiohttp's ``add_static`` with ``follow_symlinks=False``: the
    resolved file must sit inside the resolved directory, so ``..`` and a link
    inside the build that points out of it answer ``None``. A tail that is
    absolute, or carries a drive or a UNC anchor, is refused before any
    filesystem call, as aiohttp's static handler refuses it: on Windows,
    resolving a UNC tail would already reach for that network share.
    ``/assets`` falls back to the build root when the build has no ``assets/``
    directory, as its static mount always did.
    """
    if _is_anchored(tail):
        return None
    base = dist_dir / subdir
    if subdir == "assets" and not base.is_dir():
        base = dist_dir
    try:
        root = base.resolve(strict=True)
        path = (root / tail).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if root not in path.parents or not path.is_file():
        return None
    return path


def _is_anchored(tail: str) -> bool:
    """Whether ``tail`` names a root, a drive or a UNC share on either platform."""
    return tail.startswith(("/", "\\")) or bool(PureWindowsPath(tail).anchor)


def _dist_file_handler(
    dist_dir: Path, subdir: str
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """A GET/HEAD handler serving ``dist_dir/subdir`` through :func:`_resolve_dist_file`.

    The resolution is a handful of ``lstat`` calls per request, run off the event
    loop as aiohttp's own static handler does (:func:`_serve_dist_file`).
    """

    async def _serve(request: web.Request) -> web.StreamResponse:
        return await _serve_dist_file(dist_dir, subdir, request.match_info["tail"])

    return _serve


async def _serve_dist_file(dist_dir: Path, subdir: str, tail: str) -> web.StreamResponse:
    """Resolve ``tail`` off the event loop and serve it, or a no-store 404.

    ``web.FileResponse`` still picks a precompressed ``.br``/``.gz`` sibling and
    answers ranges and conditional requests.
    """
    path = await asyncio.get_running_loop().run_in_executor(
        None, _resolve_dist_file, dist_dir, subdir, tail
    )
    if path is None:
        # Returned, not raised, so the header middleware still marks it
        # no-store: a cached 404 for a hashed chunk outlives the gap.
        return web.Response(status=404, text="404: Not Found")
    return web.FileResponse(path)


def _register_dist_static_routes(app: web.Application, dist_dir: Path) -> None:
    """Register static routes for the React ``dist/`` build on ``app``.

    Extracted from ``start_dashboard`` so the route wiring (which subdirectories
    of the build get served at which prefix) is unit-testable without standing
    up the full gateway. Every prefix is registered whether or not the build has
    that subdirectory yet, and resolved per request
    (:func:`_resolve_dist_file`): a build that lands after the gateway started,
    or a staging step that re-points ``static/dist``, is served at once. App
    window entries are the exception: they are enumerated here, at start, so a
    window first built after start has no route until a restart.
    """
    # Each build subdirectory at its own prefix; without a route each would fall
    # through to the SPA fallback, and the browser would parse index.html as a
    # module, a font or an image. Literal paths, so the shell-exclusion drift
    # guard (test_token_auth) sees every one.
    # Vite's content-hashed chunks.
    app.router.add_get("/assets/{tail:.+}", _dist_file_handler(dist_dir, "assets"))
    app.router.add_get("/sprites/{tail:.+}", _dist_file_handler(dist_dir, "sprites"))
    # The self-hosted AWS Diatype family, referenced by absolute
    # url('/fonts/...') in @font-face ("invalid sfntVersion" without it).
    app.router.add_get("/fonts/{tail:.+}", _dist_file_handler(dist_dir, "fonts"))
    # Vendor shims for the app import map (react, react-dom, react/jsx-runtime).
    app.router.add_get("/vendor/{tail:.+}", _dist_file_handler(dist_dir, "vendor"))
    # App Store brand assets: builtin app icons and hero images, referenced by
    # absolute url('/app-assets/...') from each builtin's app.json.
    app.router.add_get("/app-assets/{tail:.+}", _dist_file_handler(dist_dir, "app-assets"))
    # PNA/CORS preflight for /vendor, forward-compat: a private-network
    # preflight OPTIONS would otherwise 405 and fail the widget iframe's runtime
    # load closed if Chrome starts sending one for this initiator class (today
    # it blocks at the CORS layer without a preflight — see
    # _vendor_preflight_handler).
    app.router.add_route("OPTIONS", "/vendor/{tail:.*}", _vendor_preflight_handler)

    # App window entries — separate Vite bundles an app ships as standalone
    # HTML windows, loaded by a shell window rather than the SPA router. The
    # SOURCE html lives inside the app's own folder (website/src/apps/<app>/
    # <name>.html) so each app stays one self-contained folder, and Vite
    # mirrors that path into dist. Each discovered entry is served at
    # /<app>-<name>.html: a flat, stable url the loading shell can hard-code,
    # independent of where the file sits in dist. (In dev the Vite server
    # answers the same urls via the `app-window-urls` rewrite in
    # vite.config.ts, so one url works against either server.)
    #
    # Routes are registered from the files enumerated HERE, at startup; the
    # request path never becomes a filesystem path, so there is no
    # traversal surface. The same enumeration feeds the SPA-shell fallback
    # exclusion (token_auth.register_app_window_paths): the fallback answers
    # UNAUTHENTICATED GETs so the token bootstrap can load, and a window entry
    # left inside it would be shadowed by an unauthenticated dashboard shell.
    # Registering both from one loop makes route/exclusion drift impossible.
    #
    # A missing entry is not a small failure: the SPA fallback would answer
    # with the dashboard shell, so the window would open showing a full
    # dashboard instead of its own UI.
    windows_root = dist_dir / _APP_WINDOWS_SUBDIR
    window_paths: list[str] = []
    for route_path, entry in discover_app_window_entries(windows_root):
        rel = f"{entry.parent.name}/{entry.name}"
        app.router.add_get(route_path, _window_entry_handler(dist_dir, rel))
        window_paths.append(route_path)
    register_app_window_paths(window_paths)
    logger.info("Serving React build from %s", dist_dir)
