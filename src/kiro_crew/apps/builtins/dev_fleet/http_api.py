"""Authenticated HTTP adapters for the Dev Fleet backend."""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import json
import os
import time
from pathlib import Path

from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import fleet_state, live, repository, runtime, worktree_ops
from kiro_crew.apps.proxy_auth import raw_request_target

# --- standalone backend config ---
PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "dev-fleet")
_PROXY_HMAC_MAX_AGE_S = 60
_APP_SECRET: str | None = None


def _load_app_secret() -> str:
    """Load the app secret for proxy HMAC verification (once)."""
    global _APP_SECRET
    if _APP_SECRET is not None:
        return _APP_SECRET
    from kiro_crew.config.loader import config_dir

    secret_path = config_dir() / "apps" / APP_NAME / ".app_secret"
    if secret_path.is_file():
        _APP_SECRET = secret_path.read_text().strip()
    else:
        # Fallback: try the apps dir from manager
        try:
            from kiro_crew.apps.manager import app_dir

            alt = app_dir(APP_NAME) / ".app_secret"
            if alt.is_file():
                _APP_SECRET = alt.read_text().strip()
        except Exception:
            pass
    # Do NOT cache emptiness: the secret may be provisioned after this
    # backend starts (install race) — retry on the next request, matching
    # the gateway-side _get_app_secret semantics.
    return _APP_SECRET or ""


# =============================================================================
# aiohttp route handlers
# =============================================================================


async def _with_live_run_pointers(data: dict) -> dict:
    """Overlay the request-time run pointers onto a fleet snapshot.

    ``sync_run_id`` and each row's ``provision_run_id`` are how a freshly-mounted
    page reattaches its progress stepper to a run already in flight, but
    ``_build_fleet`` bakes them into the snapshot ``_FLEET_CACHE`` then serves
    stale-while-revalidate. A run started after that snapshot was built therefore
    stayed invisible for a full cache cycle plus a rebuild: the page showed no
    progress and left the button inviting a second press. Both pointers are
    in-memory reads -- a module global, and a dict copy plus ``_RUNS`` lookups --
    so reading them per request is cheap and always current.

    Copies rather than mutates: ``data`` and its rows are the cache's own
    objects, shared with every other in-flight request.
    """
    prov_rids = await fleet_state._provision_reattach_ids()
    rows = [
        {**wt, "provision_run_id": prov_rids.get(wt.get("name"))}
        for wt in data.get("worktrees", [])
    ]
    return {**data, "worktrees": rows, "sync_run_id": worktree_ops._SYNC_RID}


async def api_dev_fleet_fleet(request: web.Request) -> web.Response:
    fresh = request.query.get("fresh") == "1"
    try:
        data = (
            (await fleet_state._fleet_refresh()) if fresh else (await fleet_state._fleet_cached())
        )
    except repository.RepoNotConfigured:
        # Not an error: no checkout has been found yet. Reported as its own state
        # (and WITHOUT an `error` field) so the page can ask where the checkout is
        # instead of rendering a failure against a path the user never chose.
        return web.json_response({"worktrees": [], "needs_setup": True})
    except repository.RepoUnreadable as exc:
        # A checkout WAS named and git cannot read it: the page renders this as the
        # Discovery Error banner, naming the path because the user chose it.
        # Redacted like every other display string this module emits (and like the
        # middleware's copy of the same exception) — git stderr can carry a remote
        # URL with credentials in it.
        return web.json_response({"worktrees": [], "error": runtime._redact(str(exc))})
    except RuntimeError as exc:
        return web.json_response(
            {"worktrees": [], "error": str(exc)},  # _run_cmd already prefixes
        )
    return web.json_response(await _with_live_run_pointers(data))


async def api_dev_fleet_worktree(request: web.Request) -> web.Response:
    name = request.query.get("name")
    if not name:
        return web.json_response({"error": "missing 'name'"}, status=400)
    valid = await repository._valid_worktree_names()
    if name not in valid:
        return web.json_response({"error": f"unknown worktree: {name!r}"}, status=400)
    return web.json_response(await fleet_state._worktree_detail(name))


async def api_dev_fleet_pod_logs(request: web.Request) -> web.Response:
    name = request.query.get("name")
    if not name:
        return web.json_response({"error": "missing 'name'"}, status=400)
    valid = await repository._valid_worktree_names()
    if name not in valid:
        return web.json_response({"error": f"unknown worktree: {name!r}"}, status=400)
    try:
        n = int(request.query.get("n", "120"))
    except ValueError:
        n = 120
    n = max(1, min(n, 1000))
    return web.json_response(await worktree_ops._pod_logs(name, n))


async def api_dev_fleet_run(request: web.Request) -> web.Response:
    rid = request.query.get("id")
    if not rid:
        return web.json_response({"error": "missing 'id'"}, status=400)
    async with runtime._RUNS_LOCK:
        run = runtime._RUNS.get(rid)
        snap = (
            dict(run, output=[runtime._redact(ln) for ln in list(run["output"])[-60:]])
            if run
            else None
        )
    if snap:
        return web.json_response(snap)
    return web.json_response({"error": "unknown run id"}, status=404)


async def api_dev_fleet_prune_candidates(request: web.Request) -> web.Response:
    return web.json_response(await worktree_ops._prune_candidates())


async def api_dev_fleet_prune_status(request: web.Request) -> web.Response:
    return web.json_response(await worktree_ops._prune_status())


async def api_dev_fleet_disk(request: web.Request) -> web.Response:
    return web.json_response(await fleet_state._disk())


def _audited(tool_name: str):
    """Audit every Dev Fleet mutation via SEL, exactly once per request.

    The decision is made at the single response boundary of the handler:
    2xx -> success, 4xx -> denied, 5xx/exception -> failure.  Target
    worktree name is read from the JSON body without consuming the stream
    (handlers re-parse independently); values are redacted before logging.
    """

    def _decorate(handler):
        async def _wrapped(request: web.Request) -> web.Response:
            target = ""
            try:
                if request.content_length and request.can_read_body:
                    raw = await request.read()  # cached; handler .json() re-reads it
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            t = parsed.get("name") or parsed.get("names") or parsed.get("path")
                            if isinstance(t, str):
                                target = t
                            elif isinstance(t, list):
                                target = ",".join(str(x) for x in t[:20])
                    except (ValueError, TypeError):
                        target = ""
            except Exception:
                target = ""
            try:
                resp = await handler(request)
            except Exception as exc:
                runtime._sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name=tool_name,
                    tool_kind="dev_fleet",
                    outcome="failure",
                    resources=runtime._redact(target),
                    error=type(exc).__name__,
                )
                raise
            try:
                payload = json.loads(resp.text or "{}")
            except (ValueError, TypeError, AttributeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if resp.status >= 500:
                outcome = "failure"
            elif resp.status >= 400:
                outcome = "denied"
            elif payload.get("ok") is False:
                # Handlers report refused/failed operations as {"ok": false}
                # with HTTP 200 -- audit them as denied, never success.
                outcome = "denied"
            else:
                outcome = "success"
            err = ""
            if outcome != "success":
                err = runtime._redact(str(payload.get("error", "")))[:200] or f"http_{resp.status}"
            runtime._sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name=tool_name,
                tool_kind="dev_fleet",
                outcome=outcome,
                resources=runtime._redact(target),
                error=err,
            )
            return resp

        _wrapped.__name__ = handler.__name__
        _wrapped.__doc__ = handler.__doc__
        return _wrapped

    return _decorate


@_audited("dev_fleet_sync")
async def api_dev_fleet_sync(request: web.Request) -> web.Response:
    result = await worktree_ops._sync()
    code = 409 if not result.get("ok") and "already running" in result.get("error", "") else 200
    return web.json_response(result, status=code)


async def _json_body(
    request: web.Request, *, code: str | None = None
) -> tuple[dict | None, web.Response | None]:
    """Parse a JSON object body; (body, None) on success, (None, 400) otherwise.

    Same 400-for-non-object / (body, None)-tuple contract as
    ``dashboard/handlers/_shared.read_bounded_json``; the one deliberate
    divergence is the optional ``code``: pass it for endpoints whose error
    contract promises a machine-readable ``code`` on every failure response, and
    the rejection then carries it alongside the human-readable ``error``.

    The catch covers the client-input failure set
    (``LookupError`` from an unknown ``charset=`` codec, ``RecursionError`` from a
    deeply nested body, ``ValueError`` from undecodable or non-JSON bytes) so a
    bad codec is a 400 rather than an uncaught 500, while a mid-read transport
    error still propagates as itself.
    """
    try:
        body = await request.json() if request.content_length else {}
    except (LookupError, RecursionError, ValueError):
        if code:
            return None, web.json_response({"error": "invalid JSON body", "code": code}, status=400)
        return None, web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        if code:
            return None, web.json_response(
                {"error": "body must be an object", "code": code}, status=400
            )
        return None, web.json_response({"error": "body must be an object"}, status=400)
    return body, None


@_audited("dev_fleet_worktree_remove")
async def api_dev_fleet_worktree_remove(request: web.Request) -> web.Response:
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return web.json_response({"error": "'name' must be a non-empty string"}, status=400)
    valid = await repository._valid_worktree_names()
    if name not in valid:
        return web.json_response({"error": f"unknown worktree: {name!r}"}, status=400)
    force = body.get("force")
    if force is not None and not isinstance(force, bool):
        return web.json_response({"error": "force must be a boolean"}, status=400)
    discard = body.get("discard_untracked_paths")
    if discard is not None and not (
        isinstance(discard, list) and all(isinstance(p, str) and p for p in discard)
    ):
        return web.json_response(
            {
                "code": "invalid_discard_paths",
                "error": (
                    "discard_untracked_paths must be a list of non-empty strings "
                    "naming the untracked files that were shown to the user"
                ),
            },
            status=400,
        )
    return web.json_response(
        await worktree_ops._worktree_remove(name, force is True, discard_untracked_paths=discard)
    )


@_audited("dev_fleet_prune_run")
async def api_dev_fleet_prune_run(request: web.Request) -> web.Response:
    try:
        body = await request.json() if request.content_length else {}
    except ValueError:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "body must be an object"}, status=400)
    raw_names = body.get("names") or []
    if not isinstance(raw_names, list) or not all(isinstance(n, str) for n in raw_names):
        return web.json_response(
            {"ok": False, "error": "'names' must be a list of strings"}, status=400
        )
    raw_force = body.get("force_names") or []
    if not isinstance(raw_force, list) or not all(isinstance(n, str) for n in raw_force):
        return web.json_response(
            {
                "ok": False,
                "code": "invalid_force_names",
                "error": "'force_names' must be a list of strings",
            },
            status=400,
        )
    raw_discard = body.get("discard_untracked_paths") or {}
    if not isinstance(raw_discard, dict) or not all(
        isinstance(k, str)
        and k
        and isinstance(v, list)
        and all(isinstance(p, str) and p for p in v)
        for k, v in raw_discard.items()
    ):
        return web.json_response(
            {
                "ok": False,
                "code": "invalid_discard_paths",
                "error": (
                    "'discard_untracked_paths' must map a worktree name to the "
                    "list of untracked files that were shown to the user"
                ),
            },
            status=400,
        )
    valid = await repository._valid_worktree_names()
    force_set: set[str] = set()
    discard_map: dict[str, list[str]] = {}
    # Both inputs override the prune preview, so both are screened against the
    # protected set. Screening only ``force_names`` would leave the discard map
    # as an unguarded second door to the same removal.
    overrides = [*raw_force, *(n for n in raw_discard if n not in raw_force)]
    if overrides:
        # Guard: never force-remove the main checkout, the currently live
        # worktree, or a staged cutover target (removing a staged target
        # would leave live_target.json pointing at a missing checkout,
        # silently abandoning the pending restart).
        # Per-item _MAKE_LIVE_LOCK is held inside _prune_one for each forced
        # removal (recheck + _worktree_remove atomically), preventing a
        # concurrent /make-live from staging between check and deletion.
        live_path = await live._live_worktree_path()
        live_name = Path(live_path).name if live_path else None
        staged_path = live._staged_target()
        staged_name = Path(staged_path).name if staged_path else None
        guarded: set[str] = set()
        for nm in overrides:
            wt, _ = await repository._find_worktree(nm)
            if wt and wt.get("is_main"):
                guarded.add(nm)
            elif live_name and nm == live_name:
                guarded.add(nm)
            elif staged_name and nm == staged_name:
                guarded.add(nm)
        if guarded:
            return web.json_response(
                {
                    "ok": False,
                    "code": "protected_worktree",
                    "error": f"cannot force-remove protected worktrees: {', '.join(sorted(guarded))}",
                },
                status=400,
            )
        force_set = {n for n in raw_force if n in valid}
        discard_map = {n: v for n, v in raw_discard.items() if n in valid}
    # Merge both lists: regular + forced (forced items skip the prunable verdict).
    all_names = [n for n in raw_names if n in valid]
    for fn in overrides:
        if fn in valid and fn not in all_names:
            all_names.append(fn)
    if not all_names:
        return web.json_response(
            {"ok": False, "code": "no_valid_names", "error": "no valid names"}, status=400
        )
    if force_set or discard_map:
        return web.json_response(
            await worktree_ops._prune_run(
                all_names, force_names=force_set, discard_paths=discard_map
            )
        )
    return web.json_response(await worktree_ops._prune_run(all_names))


async def _pod_name_action(request: web.Request, action) -> web.Response:
    """Helper: validate name from body, call action(name)."""
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return web.json_response({"error": "'name' must be a non-empty string"}, status=400)
    # _find_worktree rejects ambiguous basenames (two checkouts sharing a
    # name) — a bare set-membership check would collapse them and let the
    # action land on whichever checkout git lists first.
    target, ferr = await repository._find_worktree(name)
    if target is None:
        return web.json_response({"error": ferr}, status=400)
    return web.json_response(await action(name))


@_audited("dev_fleet_pod_up")
async def api_dev_fleet_pod_up(request: web.Request) -> web.Response:
    return await _pod_name_action(request, worktree_ops._pod_up)


@_audited("dev_fleet_pod_down")
async def api_dev_fleet_pod_down(request: web.Request) -> web.Response:
    return await _pod_name_action(request, worktree_ops._pod_down)


@_audited("dev_fleet_pod_restart")
async def api_dev_fleet_pod_restart(request: web.Request) -> web.Response:
    return await _pod_name_action(request, worktree_ops._pod_restart)


@_audited("dev_fleet_pod_token")
async def api_dev_fleet_pod_token(request: web.Request) -> web.Response:
    return await _pod_name_action(request, worktree_ops._pod_token)


@_audited("dev_fleet_pod_provision")
async def api_dev_fleet_pod_provision(request: web.Request) -> web.Response:
    return await _pod_name_action(request, worktree_ops._pod_provision)


@_audited("dev_fleet_pod_provision_dismiss")
async def api_dev_fleet_pod_provision_dismiss(request: web.Request) -> web.Response:
    body, err = await _json_body(request, code="invalid_body")
    if err is not None:
        return err
    assert body is not None
    name = body.get("name")
    run_id = body.get("run_id")
    if not isinstance(name, str) or not name:
        return web.json_response(
            {"error": "'name' must be a non-empty string", "code": "invalid_name"}, status=400
        )
    if not isinstance(run_id, str) or not run_id:
        return web.json_response(
            {"error": "'run_id' must be a non-empty string", "code": "invalid_run_id"}, status=400
        )
    target, ferr = await repository._find_worktree(name)
    if target is None:
        return web.json_response({"error": ferr, "code": "invalid_worktree"}, status=400)
    result = await worktree_ops._pod_provision_dismiss(name, run_id)
    if result.get("ok"):
        return web.json_response(result, status=200)
    return web.json_response(
        {
            "ok": False,
            "error": result.get("error", "cannot dismiss provision"),
            "code": "provision_dismiss_conflict",
        },
        status=409,
    )


@_audited("dev_fleet_rebase")
async def api_dev_fleet_rebase(request: web.Request) -> web.Response:
    return await _pod_name_action(request, worktree_ops._rebase)


# =============================================================================
# HMAC Proxy Middleware (fail-closed)
# =============================================================================


@web.middleware
async def hmac_proxy_middleware(request: web.Request, handler) -> web.Response:
    """Verify X-KiroCrew-Proxy HMAC on every request except /health.

    Message format matches routes.py signing:
      msg = "<timestamp>:<METHOD>:<raw request-target>:<sha256(body)>"
    Fail-closed: missing/invalid/expired signature -> 401.
    """
    if request.path == "/health":
        return await handler(request)

    def _deny(reason: str) -> web.Response:
        # Every auth decision lands in the tamper-evident SEL trail — an
        # HMAC denial is a permission decision like any handler outcome.
        try:
            runtime._sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="dev-fleet:proxy-hmac",
                tool_kind="dev_fleet",
                outcome="denied",
                resources=f"{request.method} {request.path}",
                error=reason,
            )
        except Exception:  # noqa: BLE001 — auditing must never mask the 401
            runtime.logger.warning("dev-fleet: SEL emit failed for HMAC denial")
        return web.json_response({"error": reason}, status=401)

    secret = _load_app_secret()
    if not secret:
        # Fail closed, no exceptions: an unauthenticated backend must never
        # serve mutation routes (a local-user bypass here reaches worktree
        # removal / rebase / gateway restart).
        return _deny("no app secret configured — HMAC verification impossible")

    header = request.headers.get("X-KiroCrew-Proxy")
    if not header:
        return _deny("missing X-KiroCrew-Proxy header")

    parts = header.split(":", 1)
    if len(parts) != 2:
        return _deny("malformed X-KiroCrew-Proxy header")

    ts_str, sig_received = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return _deny("invalid timestamp in proxy header")

    now = int(time.time())
    if abs(now - ts) > _PROXY_HMAC_MAX_AGE_S:
        return _deny("proxy signature expired")

    # Reconstruct the signed message exactly as routes.py builds it
    body = await request.read() if request.can_read_body else b""
    body_hash = hashlib.sha256(body).hexdigest()
    # The gateway signs the RAW percent-encoded request-target; recompute over
    # the same wire bytes, never the decoded path + query_string (which diverge
    # on percent-encodable characters and would fail closed with 401).
    msg = f"{ts_str}:{request.method}:{raw_request_target(request)}:{body_hash}"

    expected_sig = _hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not _hmac_mod.compare_digest(sig_received, expected_sig):
        return _deny("invalid proxy signature")

    try:
        return await handler(request)
    except repository.RepoUnreadable as exc:
        # Ordered before RepoNotConfigured: it is a SUBCLASS of the same base, so
        # a broader handler first would swallow it and report the wrong code.
        # A checkout was named and cannot be managed (git cannot read it, or it is
        # a readable directory without the Kiro Crew markers) — distinct code so a
        # client can tell "the path you gave me is wrong" from "tell me where it is".
        return web.json_response(
            {"ok": False, "code": "repo_unreadable", "error": runtime._redact(str(exc))},
            status=409,
        )
    except repository.RepoNotConfigured as exc:
        # One boundary for every route rather than a catch per handler: any route
        # that resolves a worktree goes through _repo(), and a missed handler would
        # answer a first-run click with an uncaught 500 and a generic "failed"
        # toast — the worst possible message in the state that has the least
        # context. /fleet catches both of these first and keeps its own payload
        # shapes, so neither reaches here from /fleet.
        return web.json_response(
            {"ok": False, "code": "repo_not_configured", "error": str(exc)},
            status=409,
        )


# =============================================================================
# Health endpoint
# =============================================================================


async def api_health(request: web.Request) -> web.Response:
    # Served at BOTH /health (HMAC-exempt, gateway-internal liveness poll) and
    # /api/health (proxied, reached by the browser at /apps/dev-fleet/api/health).
    # ``start_id`` lets the dashboard's restart handshake wait for the NEW
    # gateway process rather than "a 200 came back" (see _gateway_start_id).
    # None-safe: a platform that cannot report identity returns None here and
    # the frontend degrades to reload-on-first-response instead of hanging.
    return web.json_response({"status": "ok", "start_id": await live._gateway_start_id()})


@_audited("dev_fleet_restart_gateway")
async def api_dev_fleet_restart_gateway(request: web.Request) -> web.Response:
    result = await live._restart_gateway()
    return web.json_response(result)


@_audited("dev_fleet_make_live")
async def api_dev_fleet_make_live(request: web.Request) -> web.Response:
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None
    path = body.get("path")
    if not isinstance(path, str) or not path:
        return web.json_response({"error": "'path' must be a non-empty string"}, status=400)
    dry_run = body.get("dry_run")
    if dry_run is not None and not isinstance(dry_run, bool):
        return web.json_response({"error": "dry_run must be a boolean"}, status=400)
    expected_staged = body.get("expected_staged")
    if expected_staged is not None and (
        not isinstance(expected_staged, str) or not expected_staged or "\x00" in expected_staged
    ):
        return web.json_response(
            {
                "code": "invalid_expected_staged",
                "error": "expected_staged must be a non-empty string " "without NUL bytes",
            },
            status=400,
        )
    return web.json_response(
        await live._make_live(path, dry_run is True, expected_staged=expected_staged)
    )


__all__ = (
    "APP_NAME",
    "PORT",
    "_APP_SECRET",
    "_PROXY_HMAC_MAX_AGE_S",
    "_audited",
    "_json_body",
    "_load_app_secret",
    "_pod_name_action",
    "_with_live_run_pointers",
    "api_dev_fleet_disk",
    "api_dev_fleet_fleet",
    "api_dev_fleet_make_live",
    "api_dev_fleet_pod_down",
    "api_dev_fleet_pod_logs",
    "api_dev_fleet_pod_provision",
    "api_dev_fleet_pod_provision_dismiss",
    "api_dev_fleet_pod_restart",
    "api_dev_fleet_pod_token",
    "api_dev_fleet_pod_up",
    "api_dev_fleet_prune_candidates",
    "api_dev_fleet_prune_run",
    "api_dev_fleet_prune_status",
    "api_dev_fleet_rebase",
    "api_dev_fleet_restart_gateway",
    "api_dev_fleet_run",
    "api_dev_fleet_sync",
    "api_dev_fleet_worktree",
    "api_dev_fleet_worktree_remove",
    "api_health",
    "hmac_proxy_middleware",
)
