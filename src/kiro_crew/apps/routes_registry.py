"""The app registry: browsing and refreshing it, installing from it, and its configured sources.

``handle_registry``, ``handle_registry_refresh``, ``handle_registry_install`` and
``handle_registry_install_stream``, and the registry list in ``config.json``
(``handle_registries``, ``handle_registries_refresh``, ``_write_registries_config``).

Part of the ``kiro_crew.apps.routes`` family: callers and tests reach every name here
as ``kiro_crew.apps.routes.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps import official_catalog
from kiro_crew.apps.manager import app_lifecycle_lock, get_app
from kiro_crew.apps.official_category_order import forget_cache as forget_category_order_cache
from kiro_crew.apps.official_category_order import load_category_order
from kiro_crew.apps.official_editorial import forget_cache as forget_editorial_cache
from kiro_crew.apps.official_editorial import load_sections
from kiro_crew.apps.registry import (
    _REGISTRY_TRUST_TIERS,
    _TRUST_INDEX,
    _TRUST_OWNER,
    _git_url_host,
    _pinned_registries,
    _registry_identity_key,
    _registry_trust_tier_of,
    _same_git_target,
    _strip_git_target_userinfo,
    get_server_platform,
    install_from_registry,
    list_catalog_apps,
    list_registry,
)
from kiro_crew.apps.routes_art import _SAFE_REPO_RE, _is_safe_repo_identifier
from kiro_crew.apps.routes_lifecycle import (
    _app_may_run_after_install,
    _register_app_off_loop,
    _start_backend_after_install,
    _suspend_app_for_session_approval_reconsent,
)
from kiro_crew.apps.update_notify import deliver_app_update_notes, prepare_app_update_notes
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.routes")


# ---------------------------------------------------------------------------
# Registry (browse & install from curated list)
# ---------------------------------------------------------------------------


async def _announce_app_updates(request: web.Request, rows: list[dict[str, Any]]) -> None:
    """Bell-note the app updates these listing rows reveal, once each.

    Called with CATALOG rows only. The seed fallback answers when the catalog
    is unreachable, and its rows can name an older version than the catalog
    does -- announcing from them would report a "new" version that is not the
    newest, and their absent rows say nothing about an app either way.

    The ledger half runs off the loop (disk); delivery runs on it, because
    ``state.notify`` off the serving loop persists outside the notification
    coordinator's FIFO executor. Both halves are total.
    """
    state = request.app.get("state")
    if state is None:
        return
    notes = await asyncio.to_thread(prepare_app_update_notes, rows)
    if notes:
        deliver_app_update_notes(state, notes)


async def handle_registry(request: web.Request) -> web.Response:
    """GET /api/apps/registry — list all apps available for installation."""
    # The published catalog is the storefront's source of truth when it is
    # reachable: its rows replace the seed + per-app manifest fetch. Offline the
    # catalog is empty and the seed listing answers instead.
    apps = await list_catalog_apps()
    if apps:
        await _announce_app_updates(request, apps)
    else:
        apps = await list_registry()
    # Published rail order and layout ride along on the response the store already
    # makes, rather than endpoints the page would have to wait on separately. They
    # are two documents with two caches, loaded CONCURRENTLY: each cold-cache load
    # does network I/O, and paying two fetch timeouts in series would delay the
    # whole store response. Both are presentation, so a failure degrades to the
    # client's own defaults per document and must never 500 the store -- the same
    # contract the catalog annotation keeps.
    order_result, sections_result = await asyncio.gather(
        asyncio.to_thread(load_category_order),
        asyncio.to_thread(load_sections),
        return_exceptions=True,
    )
    if isinstance(order_result, BaseException):
        logger.warning("ignoring the published category order", exc_info=order_result)
        category_order: list = []
    else:
        category_order = order_result
    if isinstance(sections_result, BaseException):
        logger.warning("ignoring the editorial sections", exc_info=sections_result)
        sections: list = []
    else:
        sections = sections_result
    return web.json_response(
        {
            "apps": apps,
            "serverPlatform": get_server_platform(),
            "categoryOrder": category_order,
            "editorialSections": sections,
        }
    )


async def handle_registry_refresh(request: web.Request) -> web.Response:
    """POST /api/app-store/refresh — drop the published-document caches.

    Drops the on-disk caches of all three published documents (catalog,
    category order, editorial), so the NEXT ``GET /api/apps/registry`` is
    rebuilt from fresh fetches. This exists because the caches degrade
    SILENTLY: a failed fetch overwrites the catalog cache with a failure
    sentinel and the store quietly falls back to the seed listing, and without
    an explicit refresh the user's only remedy is waiting out ``CACHE_TTL``.

    Deliberately OUTSIDE ``/api/apps/``: token_auth's ``_app_owns_path``
    grants an app token implicit ownership of everything under
    ``/api/apps/<its-own-name>/``, so a path like
    ``/api/apps/registry/refresh`` would hand any app that names itself
    ``registry`` the power to purge the shared catalog caches without
    declaring the permission. Under ``/api/app-store/`` no app name can
    collide, so an app token reaches this endpoint only through an explicit
    ``permissions.api`` grant.

    Two more deliberate shapes:

    - A POST, not a ``?refresh=1`` on the GET: deleting caches and triggering
      outbound fetches is a state change, and a state-changing GET is reachable
      by cross-site top-level navigation with a valid SameSite=Lax cookie --
      exactly the request the CSRF middleware never sees.
    - It only DROPS caches -- it never fetches. The follow-up GET pays the
      fetch on the exact same code path as a cold start, so refresh cannot
      behave differently from the load it is trying to repair.
    """
    # Off the event loop like every other disk touch on these routes; three
    # unlinks gathered because none depends on another.
    await asyncio.gather(
        asyncio.to_thread(official_catalog.forget_cache),
        asyncio.to_thread(forget_category_order_cache),
        asyncio.to_thread(forget_editorial_cache),
    )
    return web.json_response({"ok": True})


async def handle_registry_install(request: web.Request) -> web.Response:
    """POST /api/apps/registry/install — install an app from the registry.

    Clones the repo, runs the install script, and registers the app.
    This can take a while so the response includes a log of what happened.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_registry_install")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    if not name:
        return web.json_response({"error": "app name required"}, status=400)

    # One lock for the complete transaction: install_from_registry is
    # lock-free internally (asyncio.Lock is not reentrant), so this is the
    # single acquisition covering clone/build → copy → register → backend.
    async with app_lifecycle_lock(name):
        was_installed = await asyncio.to_thread(lambda: get_app(name) is not None)
        result = await install_from_registry(name)

        # Redact install log and error before returning to client — build output
        # may contain internal hostnames, package URLs, or credential fragments.
        if result.get("log"):
            from kiro_crew.security import redact

            cleaned_log = redact(result["log"])
            result["log"] = cleaned_log
        if result.get("error"):
            from kiro_crew.security import redact

            cleaned_err = redact(result["error"])
            result["error"] = cleaned_err

        if result.get("needsClientInstall"):
            return web.json_response(result, status=200)
        if not result.get("ok"):
            sel().log_api_access(
                caller="dashboard",
                operation="app_registry_install",
                outcome="failed",
                resources=name,
                error=result.get("error", ""),
            )
            return web.json_response(result, status=400)

        may_run = await _app_may_run_after_install(result["name"], fresh_install=not was_installed)
        if not may_run:
            # The install transaction owns the live state. An update that newly asks
            # for session control leaves the app disabled, but this also preserves a
            # user's existing disabled state without depending on response wording.
            reg = await _suspend_app_for_session_approval_reconsent(result["name"])
        else:
            # Auto-register resources
            reg = await _register_app_off_loop(result["name"])
            # Spawn the backend now so apps with a server are reachable immediately —
            # without this the backend only starts on the next gateway reboot (via
            # start_enabled_app_backends), leaving the app's UI with "no reachable
            # backend" until then. No-op for apps that declare no backend. Run in a
            # thread because start_app_backend blocks on a health-check poll.
            await _start_backend_after_install(result["name"])
    result["registration"] = reg.to_dict()
    sel().log_api_access(
        caller="dashboard", operation="app_registry_install", outcome="completed", resources=name
    )
    return web.json_response(result, status=201)


async def handle_registry_install_stream(request: web.Request) -> web.StreamResponse:
    """POST /api/apps/registry/install-stream — SSE streaming install.

    Same logic as ``handle_registry_install`` but streams log lines as
    Server-Sent Events in real-time, giving the user full transparency
    into what's happening during the (often slow) install process.

    Event types:
      ``log``   — a single log line (data: string)
      ``done``  — install finished (data: JSON with ok, name, error, etc.)

    The original ``/api/apps/registry/install`` endpoint is unchanged —
    CLI and other callers are not affected.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "app_registry_install")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = body.get("name", "")
    if not name:
        return web.json_response({"error": "app name required"}, status=400)

    # Set up SSE response
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    try:
        await resp.prepare(request)
    except (ConnectionResetError, ConnectionAbortedError):
        # The client vanished between sending the request and the stream
        # opening; nothing has been installed yet, so just hang up quietly.
        return resp

    # Create a queue-backed log collector so install_from_registry streams
    # each log line as it's appended — zero changes to the install logic.
    from kiro_crew.apps.registry import StreamingLogLines

    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=500)
    streaming_log = StreamingLogLines(queue)

    async def _send_sse(event: str, data: str) -> None:
        """Write a single SSE frame.

        Multi-line data is split into multiple ``data:`` lines per the
        SSE spec (each line prefixed with ``data: ``).  This prevents
        newline injection from breaking the event stream framing.
        """
        try:
            # SSE spec: multi-line data uses one "data:" prefix per line
            lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            payload = f"event: {event}\n"
            for line in lines:
                payload += f"data: {line}\n"
            payload += "\n"
            await resp.write(payload.encode("utf-8"))
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    async def _finish_stream() -> None:
        """Close the SSE stream, tolerating a client that already left.

        A browser tab closed mid-install makes ``write_eof`` raise
        ``ClientConnectionResetError`` ("Cannot write to closing
        transport", a ``ConnectionResetError`` subclass), which would
        otherwise escape the handler and be logged by aiohttp as an
        unhandled server error for a routine client disconnect.
        """
        try:
            await resp.write_eof()
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    async def _drain_queue() -> None:
        """Forward queued log lines to the SSE stream until sentinel."""
        from kiro_crew.security import redact

        while True:
            line = await queue.get()
            if line is None:
                break  # sentinel — install finished
            cleaned = redact(line)
            await _send_sse("log", cleaned)

    # Run install + drain concurrently. The complete lifecycle transaction —
    # install, resource registration, backend start — runs under one per-app
    # lock (install_from_registry is lock-free internally).
    async def _locked_install() -> dict[str, Any]:
        async with app_lifecycle_lock(name):
            was_installed = await asyncio.to_thread(lambda: get_app(name) is not None)
            r = await install_from_registry(name, log_lines=streaming_log)
            if r.get("ok") and not r.get("needsClientInstall"):
                may_run = await _app_may_run_after_install(
                    r["name"], fresh_install=not was_installed
                )
                if not may_run:
                    # Match the non-stream and update routes: live enabled state,
                    # rather than notice wording, decides whether resources may run.
                    reg = await _suspend_app_for_session_approval_reconsent(r["name"])
                    r["registration"] = reg.to_dict()
                else:
                    reg = await _register_app_off_loop(r["name"])
                    # Spawn the backend immediately (see handle_registry_install) so
                    # the app is reachable without a gateway reboot. No-op for
                    # backend-less apps.
                    await _start_backend_after_install(r["name"])
                    r["registration"] = reg.to_dict()
            return r

    install_task = asyncio.create_task(_locked_install())
    drain_task = asyncio.create_task(_drain_queue())

    try:
        result = await install_task
    except Exception as exc:
        result = {"ok": False, "name": name, "error": str(exc)}
    finally:
        # Signal the drain loop to stop, then wait for it to flush.
        # Use blocking put — put_nowait raises QueueFull if the queue
        # is at capacity, which would prevent the sentinel from being
        # delivered and hang _drain_queue forever.
        await queue.put(None)
        await drain_task

    # Redact the final log and error fields before sending to client —
    # error may contain internal hostnames, git URLs, or credential
    # fragments from subprocess failures.
    if result.get("log"):
        from kiro_crew.security import redact

        cleaned_log = redact(result["log"])
        result["log"] = cleaned_log
    if result.get("error"):
        from kiro_crew.security import redact

        cleaned_err = redact(result["error"])
        result["error"] = cleaned_err

    if result.get("needsClientInstall"):
        await _send_sse("done", json.dumps(result))
        await _finish_stream()
        return resp

    if not result.get("ok"):
        sel().log_api_access(
            caller="dashboard",
            operation="app_registry_install_stream",
            outcome="failed",
            resources=name,
            error=result.get("error", ""),
        )
        await _send_sse("done", json.dumps(result))
        await _finish_stream()
        return resp

    # Resource registration + backend start already ran inside the locked
    # transaction above; result carries "registration".
    sel().log_api_access(
        caller="dashboard",
        operation="app_registry_install_stream",
        outcome="completed",
        resources=name,
    )
    await _send_sse("done", json.dumps(result))
    await _finish_stream()
    return resp


def _derive_registry_name(repo: str) -> str:
    """Derive a safe display name from a git URL (host + path slugified).

    Used when a URL registry is added without an explicit ``name`` — defaulting
    ``name=repo`` (the legacy behavior) would make two URL registries with
    disallowed name characters collide.  Strips the scheme + userinfo, drops a
    trailing ``.git``, and slugifies host+path to ``[A-Za-z0-9_-]`` so
    ``https://github.com/acme/apps`` becomes ``github-com-acme-apps``.

    A short stable hash of the ORIGINAL ``repo`` is appended so two distinct
    URLs whose slugs collide (e.g. ``…/org/a-b`` and ``…/org/a_b`` both slugify
    to ``…-org-a-b``) never derive the same name — and therefore never share an
    ``_external_registry_cache_path`` cache file, which would otherwise let one
    registry's fetch clobber the other's index.
    """
    s = repo.strip()
    # Strip URL scheme (https://, ssh://, git://, git+ssh://, ...).
    s = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", s)
    # Strip leading userinfo (scp-style ``user@host:path`` or ssh userinfo).
    s = re.sub(r"^[^/@]+@", "", s)
    # Drop a trailing ``.git``.
    s = re.sub(r"\.git$", "", s)
    # Slugify everything that is not alphanumeric to a single dash.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-") or "registry"
    # Disambiguate on the original repo so distinct URLs never collide.
    digest = hashlib.sha256(repo.strip().encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


async def handle_registries(request: web.Request) -> web.Response:
    """GET/PUT /api/apps/registries — manage external federated registries."""
    if request.method == "GET":
        config = KiroCrewConfig.load()

        # Operator rows report the tier in FORCE for them, never the one the row
        # declares: `registry._registry_trust_tier` reads `owner` only from a
        # build-pinned row or from the operator's keystone grant
        # (`registry_trust.json`, written through `/api/security/trusted-registries`),
        # since `config.json` is agent-writable and a hand-edited `owner` there is
        # a claim the runtime does not honour. `label`/`review` are reported empty
        # for the same reason: they are claims only the build may make, so an
        # operator row makes neither. The tier is read off each row object, never
        # by name, so two rows sharing a name each report their own grant; a row
        # whose name a pinned registry takes is not served and reads `index`. The
        # lookup reads the grant file, so it runs off the event loop.
        def _operator_rows() -> list[dict[str, Any]]:
            pinned_keys = {_registry_identity_key(p.name or p.repo) for p in _pinned_registries()}
            return [
                {
                    "name": r.name,
                    "repo": _strip_git_target_userinfo(r.repo),
                    "branch": r.branch,
                    "trust": (
                        _TRUST_INDEX
                        if _registry_identity_key(r.name or r.repo) in pinned_keys
                        else _registry_trust_tier_of(r)
                    ),
                    "label": "",
                    "review": "",
                }
                for r in config.registries
            ]

        registries = await asyncio.to_thread(_operator_rows)
        # Edition-pinned registries are reported SEPARATELY and read-only. They
        # are not part of ``registries`` because PUT replaces that list verbatim:
        # a GET→edit→PUT round-trip would persist an edition default into the
        # operator's config.json, where a later edition change cannot move it.
        # The client renders these as non-editable rows.
        pinned = [
            {
                "name": r.name,
                "repo": _strip_git_target_userinfo(r.repo),
                "branch": r.branch,
                "trust": r.trust,
                # Display metadata the build owns. `label` never replaces `name`
                # in the payload: the client needs the id to key its per-registry
                # app counts and refresh calls, and shows the label beside it.
                "label": r.label,
                "review": r.review,
            }
            for r in _pinned_registries()
        ]
        sel().log_api_access(
            caller="dashboard",
            operation="registries.read",
            outcome="success",
            resources=f"count={len(registries)} pinned={len(pinned)}",
        )
        return web.json_response({"registries": registries, "pinned": pinned})

    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "registries.update")
    if denied is not None:
        return denied

    def _deny(msg: str, resources: str = "") -> web.Response:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="denied",
            resources=resources or msg,
        )
        return web.json_response({"error": msg}, status=400)

    # PUT — replace the entire registries list
    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid JSON body")

    entries = body.get("registries")
    if not isinstance(entries, list):
        return _deny("registries must be an array")

    # Validate each entry
    validated: list[dict[str, str]] = []
    # Names whose entry tried to claim `label`/`review`. Recorded, not refused —
    # see the drop comment at the `validated.append` below.
    stripped_claims: list[str] = []
    _blocked_repos = {"KiroCrew"}  # brand-ok: the GitHub repository name, not prose
    # Keyed the same way `_effective_registries` decides a contest — by the cache
    # file the registry would use, not the raw string. Comparing raw names here
    # would let `Official` past the guard against a pinned `official`, persist it,
    # and then have the merge drop BOTH as contested: exactly the inert-registry
    # outcome this guard exists to prevent, now with the operator's own row lost too.
    _pinned_names = {_registry_identity_key(r.name or r.repo) for r in _pinned_registries()}
    _pinned_repos = {r.repo for r in _pinned_registries()}
    for entry in entries:
        if not isinstance(entry, dict):
            return _deny("each registry must be an object")
        repo = str(entry.get("repo", "")).strip()
        if not repo:
            return _deny("repo is required")
        public_repo = _strip_git_target_userinfo(repo)
        # Accept a bare name (legacy — kept for companion resolution) OR a
        # vetted full git URL. Reuse the blob-proxy validator, which rejects
        # shell metacharacters / traversal and owner/repo shorthand.
        if not _is_safe_repo_identifier(repo):
            return _deny(f"invalid repo URL or name: {public_repo!r}", f"repo={public_repo}")
        if repo in _blocked_repos:
            return _deny(
                f"{public_repo!r} is the core registry — no need to add it",
                f"blocked_repo={public_repo}",
            )
        if any(_same_git_target(repo, pinned_repo) for pinned_repo in _pinned_repos):
            return _deny(
                f"{public_repo!r} is already provided by this build",
                f"pinned_repo={public_repo}",
            )
        # Bare names default the display name to the repo (legacy). Full URLs
        # derive a safe slug from host+path so two URL registries never collide
        # on a default name.
        default_name = repo if _SAFE_REPO_RE.match(repo) else _derive_registry_name(repo)
        name = str(entry.get("name", "")).strip() or default_name
        if not re.match(r"^[A-Za-z0-9_\-. ]+$", name):
            return _deny(f"invalid registry name: {name!r}", f"name={name}")
        branch = str(entry.get("branch", "main")).strip() or "main"
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-./]*$", branch) or ".." in branch:
            return _deny(f"invalid branch name: {branch!r}", f"branch={branch}")
        # `trust` is accepted only as `index` for an operator row, and that is the
        # value stored. `registry._registry_trust_tier` never reads `owner` off a
        # config row — `config.json` is agent-writable, so a tier persisted here
        # could never be honoured. The operator lifts a row to `owner` through
        # Settings > Security (`/api/security/trusted-registries`), which writes
        # the keystone `registry_trust.json` keyed by the row's repository, so
        # there is correspondingly no tier to PRESERVE across a replace-all PUT: an
        # omitted value simply means `index`, which is what a stored row always is.
        raw_trust = entry.get("trust")
        trust = _TRUST_INDEX if raw_trust is None else (str(raw_trust).strip() or _TRUST_INDEX)
        if trust not in _REGISTRY_TRUST_TIERS:
            return _deny(f"invalid registry trust: {trust!r}", f"trust={trust}")
        if trust == _TRUST_OWNER:
            return _deny(
                "the trusted tier is supplied by this build or granted in "
                "Settings > Security, not by configuration",
                f"owner_trust_refused={name}",
            )
        # A name an edition-pinned registry already owns is refused rather than
        # persisted: `_effective_registries` drops a same-named operator row, so
        # storing it would leave a registry in config.json that never loads and
        # whose per-row refresh 404s, with nothing telling the operator why.
        if _registry_identity_key(name) in _pinned_names:
            return _deny(
                f"{name!r} is the name of a registry this build provides — choose another",
                f"pinned_name_collision={name}",
            )
        # `label` and `review` are DROPPED rather than stored, mirroring `trust`:
        # both are claims about a registry that only the build may make, and
        # `config.json` is agent-writable, so a value persisted here would let a
        # hand-edited file relabel a source or stamp it "Reviewed by the Kiro Crew
        # team" in the UI. Dropped rather than refused with a 400, because unlike
        # `trust: owner` there is no grant to withhold — the fields are display
        # text, so the save still does what the operator asked and simply carries
        # no claim. The drop is recorded in the audit event below so it is not
        # silent to anyone reading the log.
        if str(entry.get("label", "")).strip() or str(entry.get("review", "")).strip():
            stripped_claims.append(name)
        validated.append({"name": name, "repo": repo, "branch": branch, "trust": trust})

    # The registry list is part of config.json, so its read-modify-write must
    # serialize against BOTH config writer generations, not just one: the sidecar
    # advisory flock that ``update_config_locked`` takes, and the loop-side
    # ``_get_config_lock`` asyncio lock that the legacy dashboard writers (agents
    # endpoint, updates.py, security.py, messaging.py, mcp.py, core.py STT) still
    # serialize on ALONE. Holding only the flock does not exclude that family, so
    # a concurrent PUT there could commit from a snapshot taken before this write
    # and silently revert it — the lost update ``config/loader_document.py`` warns about
    # when it says such callers must ALSO hold the in-process asyncio lock.
    # ``run_config_write`` is the one entry point that holds both, and it still
    # hands the blocking work to a worker, so the loop never stalls.
    #
    # Call-time import, deliberately, and NOT under AUTOSDE ``top-level-imports``'
    # circular-import exception -- there is no cycle on this edge today; every
    # import order was checked.  The reason is layering and load cost, both
    # checkable:
    #
    #   * No module in ``src/kiro_crew/apps/`` imports ``kiro_crew.dashboard`` at
    #     module scope.  Not one.  ``apps`` sits below ``dashboard``, and hoisting
    #     this would create the package's first load-time edge in the wrong
    #     direction -- into a package that already imports back into this one
    #     (``dashboard/routes/system.py``, ``dashboard/handlers/security.py``).
    #     ``apps.manager`` already defers its ``apps.routes`` import for a cycle
    #     that IS real; widening the graph here makes that worse, not better.
    #   * Measured: hoisting pulls 117 additional modules into every importer of
    #     ``apps.routes`` (479 -> 596), and loads the whole ``dashboard.chat_utils``
    #     tree on paths that today never touch it.
    #
    # Same reasoning, in routes.py, as ``handle_app_uninstall``'s lazy
    # ``_get_config_lock`` import; ``run_config_write`` itself defers that import
    # for an outright cycle.
    from kiro_crew.dashboard.chat_utils import run_config_write

    cfg = Path(config_path())
    try:
        newly_trusted = await run_config_write(_write_registries_config, cfg, validated)
    except ConfigReadError as exc:
        if isinstance(exc.__cause__, OSError):
            sel().log_api_access(
                caller="dashboard",
                operation="registries.update",
                outcome="failed",
                resources=f"config read error: {exc.__cause__}",
            )
            return web.json_response({"error": f"cannot read config: {exc.__cause__}"}, status=500)
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="failed",
            resources="config.json malformed",
        )
        return web.json_response(
            {"error": "config.json is malformed — fix it before updating registries"},
            status=500,
        )
    except OSError as exc:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.update",
            outcome="failed",
            resources=f"config write error: {exc}",
        )
        return web.json_response(
            {
                "error": f"cannot write config: {exc}",
                "code": "registries_config_write_failed",
            },
            status=500,
        )

    # The per-host trust grants are audited by ``_write_registries_config`` itself,
    # on the worker thread that committed them -- see the comment there for why
    # this cannot be done here.  What remains is the API-outcome event, which
    # correctly belongs to the request: if the caller was cancelled it never runs,
    # because the request did not succeed.
    newly_trusted_hosts = [host for host, _repo in newly_trusted]

    sel().log_api_access(
        caller="dashboard",
        operation="registries.update",
        outcome="success",
        resources=(
            f"count={len(validated)} repos="
            f"{','.join(_strip_git_target_userinfo(r['repo']) for r in validated)}"
            + (f" stripped_build_claims={','.join(stripped_claims)}" if stripped_claims else "")
        ),
    )
    # The response echoes exactly what was stored, so a client that sent a
    # `label`/`review` sees them absent and can tell the claim did not stick.
    public_registries = [
        {**row, "repo": _strip_git_target_userinfo(row["repo"])} for row in validated
    ]
    return web.json_response(
        {
            "ok": True,
            "registries": public_registries,
            "newlyTrustedHosts": newly_trusted_hosts,
        }
    )


def _write_registries_config(cfg: Path, validated: list[dict[str, str]]) -> list[tuple[str, str]]:
    """Replace the registry trust set under the config lock, off the loop.

    Emits a per-host ``registries.host_trust_granted`` SEL event after, and only
    after, the write commits, and returns each newly trusted ``(host, repo)`` pair
    for the response body.  Host discovery belongs inside the locked mutation:
    comparing with a pre-lock snapshot would race a concurrent config writer and
    could report a grant that was not new.

    Why the returned hosts are audited at all: a configured registry host is fed
    into the loosened-sandbox / SSH-clone trust set (see
    ``registry._configured_registry_hosts``) AND its apps become installable with
    gateway privileges, so admitting a host is a genuine trust grant, not just a
    config edit.  The generic ``registries.update`` event does not record WHICH
    host gained trust, leaving an unreconstructable audit gap; a distinct per-host
    ``registries.host_trust_granted`` event lets incident response always establish
    when and how a host entered the trust set.  Comparison is
    against the PRIOR on-disk config rather than the freshly validated list, so
    re-saving an unchanged list emits nothing.
    """
    newly_trusted: list[tuple[str, str]] = []

    def _mutate(data: dict) -> dict:
        # ``or []`` deliberately treats a legacy explicit null as an empty list,
        # keeping PUT as the repair path for that otherwise valid JSON value.
        prior = data.get("registries") or []
        prior_hosts = {
            host
            for row in prior
            if isinstance(row, dict) and (host := _git_url_host(str(row.get("repo", ""))))
        }
        seen_hosts: set[str] = set()
        for row in validated:
            host = _git_url_host(row["repo"])
            if host and host not in prior_hosts and host not in seen_hosts:
                seen_hosts.add(host)
                newly_trusted.append((host, _strip_git_target_userinfo(row["repo"])))
        data["registries"] = validated
        return data

    update_config_locked(cfg, mutate=_mutate)

    # Audit HERE, in the worker, and not in the async caller.  ``run_config_write``
    # shields this thread and drains it across cancellation, then re-raises
    # ``CancelledError`` and DISCARDS the return value.  A gateway shutdown or a
    # client disconnect landing in that window therefore commits the trust grant
    # and never reaches an audit emitted by the caller -- persisted trust with no
    # ``host_trust_granted`` record, which is precisely the reconstruction gap
    # this event exists to close.  Emitting on the thread that completed the write
    # binds the event to the commit: the two cannot disagree, because nothing can
    # interrupt a thread between these two statements.
    #
    # Still strictly after ``update_config_locked`` returns, so a failed write
    # raises before any grant is announced.  SEL is thread-safe and adapts to an
    # off-loop caller (``_on_event_loop``), and a non-critical event swallows
    # filesystem errors rather than raising, so this cannot turn a committed
    # write into a 500.
    for host, public_repo in newly_trusted:
        sel().log_api_access(
            caller="dashboard",
            operation="registries.host_trust_granted",
            outcome="success",
            resources=f"host={host} repo={public_repo}",
        )
    return newly_trusted


async def handle_registries_refresh(request: web.Request) -> web.Response:
    """POST /api/apps/registries/refresh — bust registry caches and re-warm.

    Optional JSON body ``{"repo": "<git-url-or-name>"}`` refreshes only the
    registry whose ``.repo`` matches; omit/empty to refresh all. The blocking
    cache-bust + re-fetch is offloaded inside ``refresh_registries`` (async).
    """
    from kiro_crew.apps.registry import refresh_registries

    caller = request.get("user", "dashboard")
    repo: str | None = None
    body_bytes = await request.read()
    if body_bytes:
        try:
            body = json.loads(body_bytes)
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        # A non-empty body MUST decode to an object. A valid-but-non-object
        # payload (e.g. ``[]`` or ``"foo"``) would otherwise leave ``repo=None``
        # and refresh EVERY configured registry — an unintended fan-out of git
        # clones / cache writes from a malformed request. Reject it as a 400.
        if not isinstance(body, dict):
            return web.json_response({"error": "request body must be a JSON object"}, status=400)
        raw = body.get("repo")
        if raw is not None:
            repo = str(raw).strip() or None

    if repo is not None and not _is_safe_repo_identifier(repo):
        public_repo = _strip_git_target_userinfo(repo)
        sel().log_api_access(
            caller=caller,
            operation="registries.refresh",
            outcome="denied",
            resources=f"repo={public_repo}",
        )
        return web.json_response({"error": f"invalid repo: {public_repo!r}"}, status=400)

    result = await refresh_registries(repo)
    if result.get("not_found"):
        public_repo = _strip_git_target_userinfo(repo or "")
        sel().log_api_access(
            caller=caller,
            operation="registries.refresh",
            outcome="not_found",
            resources=f"repo={public_repo}",
        )
        return web.json_response(
            {"error": f"no configured registry matches repo: {public_repo!r}"},
            status=404,
        )
    sel().log_api_access(
        caller=caller,
        operation="registries.refresh",
        outcome="success" if result.get("ok") else "partial",
        resources=(
            f"refreshed={len(result.get('refreshed', []))} "
            f"failed={len(result.get('failed', []))} apps={result.get('apps')}"
        ),
    )
    return web.json_response(result)
