"""deploy-web backend — config + deploy/recall/destroy/list endpoints.

Mechanism: deterministic Python builtin module (design §8). HTTP endpoints (called
by the Artifact Deploy UI page) drive the ``engine`` which shells to the ``aws`` CLI with
``--profile`` (never boto3). The deploy mechanics are NOT LLM tools.

Approval model (§9.3): publishing creates a **public** URL and destroy/recall mutate
public infra, so each is a two-call **confirm-gate** — the first call returns a preview
that echoes exactly what will happen (resources, scan findings, public nature); the
client must re-call with ``confirm`` to proceed. Pre-publish scan (§4.1/Q4) blocks
publishing on any secret/internal-data finding unless the client passes
``override_scan`` (explicit "publish anyway"). These are plain HTTP endpoints, not
registered tools, so they can never appear in heartbeat/cron tool safe-sets.
"""

# This module is the deploy routes' facade: the dashboard server, the artifact tools, the
# webapp preview handler and tests import and patch every name through it, as
# ``kiro_crew.deploy.handlers.<name>``. A read of a name an owner holds is served from that
# owner, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every module
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. It keeps the
# aiohttp layer: the internal-secret gate and JSON body parsing, every route handler, the
# teardown route's reaper check and manifest expiry (``_check_reaper_installed``,
# ``_expire_manifest_best_effort``), ``_cloud_gated`` and ``register_routes``. New work goes to
# the owner of its responsibility:
#
#   ``handlers_guards`` -- restricted-session denial, response redaction, SEL audit, remediation
#   ``handlers_content`` -- what may be published: input specs, confinement, staging, the scan
#   ``handlers_sites`` -- the AWS identity, and the deploy, recall, destroy and list operations

from __future__ import annotations

import asyncio
import functools
import importlib as _importlib
import json
import logging
import os
import shutil
import sys as _sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.deploy import engine
from kiro_crew.deploy import iam as iam_mod
from kiro_crew.deploy import pricing as pricing_mod
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.deploy.handlers_content import (
    _MAX_STAGE_BYTES,
    _allowed_local_roots,
    _compute_content_digest,
    _compute_tree_size_global,
    _dir_contains_sensitive,
    _stage_artifact_html,
)
from kiro_crew.deploy.handlers_guards import (
    _audit,
    _deny_restricted,
    _reaper_remediation,
    _redact_pending_entries,
    _redact_profile_fields,
    _redact_text,
    _safe_err,
    _sanitize_response,
)
from kiro_crew.deploy.handlers_sites import (
    _PROFILE_SPEC,
    _REGION_SPEC,
    DEFAULT_REGION,
    _do_deploy,
    _do_destroy,
    _do_list,
    _do_recall,
    _load_config,
    _ProfileResolveError,
    _resolve_profile,
    _save_config,
)
from kiro_crew.publish_governance import DEPLOY_WEB_PROVIDER_ID, publish_denied_reason
from kiro_crew.sandbox import SandboxUnavailableError
from kiro_crew.validation import ValidationError, validate_field

try:
    from kiro_crew.artifacts import (
        ArtifactError,
        ArtifactNotFoundError,
        ArtifactValidationError,
        get_default_store,
    )
    _HAS_ARTIFACTS = True
except ImportError:  # pragma: no cover - defensive
    _HAS_ARTIFACTS = False


logger = logging.getLogger(__name__)


# --- aiohttp adapters ------------------------------------------------------

def _is_internal_secret_request(request: web.Request) -> bool:
    """True when the request was authenticated via X-Internal-Secret (MCP tool path).

    The server enforces preview-only semantics for internal-secret callers:
    confirm and override_scan are stripped server-side so the MCP tool can never
    bypass the two-call confirm gate or override scan findings — those actions
    require human interaction through the dashboard UI.
    """
    return "X-Internal-Secret" in request.headers


# Default-deny allowlist for internal-secret callers.
# Only these handler operations are reachable via MCP tool path (X-Internal-Secret).
# ANY new handler added to register_routes is DENIED unless explicitly listed here.
_INTERNAL_ALLOWED_HANDLERS: frozenset[str] = frozenset({"deploy", "list", "pricing"})

_INTERNAL_DENIED_ATTR = "_internal_denied"


def _internal_denied(func):  # type: ignore[no-untyped-def]
    """Decorator: deny requests authenticated via X-Internal-Secret.

    Applied to every /api/deploy handler that is NOT in _INTERNAL_ALLOWED_HANDLERS.
    A new handler without this decorator (and not in the allowlist) will trip the
    registration-time assertion in register_routes.
    """
    @functools.wraps(func)
    async def _wrapper(request: web.Request) -> web.Response:
        if _is_internal_secret_request(request):
            op = getattr(func, "__name__", "unknown").removeprefix("_handle_")
            _audit(op, "", "denied", error="internal-secret caller — handler not in allowlist")
            return web.json_response(
                {"error": "this endpoint is not available to internal-secret callers"},
                status=403,
            )
        return await func(request)

    setattr(_wrapper, _INTERNAL_DENIED_ATTR, True)
    return _wrapper


def _strip_confirm_for_internal(request: web.Request, params: dict[str, Any]) -> dict[str, Any]:
    """Strip confirm/override_scan from params when request is internal-secret authenticated."""
    if _is_internal_secret_request(request):
        params.pop("confirm", None)
        params.pop("override_scan", None)
    return params


async def _json_body(
    request: web.Request,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Parse a JSON *object* body, following the ``read_bounded_json`` 400 contract.

    Returns ``(body, None)`` on success, or ``(None, error_response)`` when the
    caller should return early. Deploy has no app-specific reason to diverge from
    the shared dashboard contract (``dashboard/handlers/_shared.py::read_bounded_json``),
    so it answers a body-shape 400 for BOTH a malformed body and a valid-JSON body
    that is not an object. Otherwise a bad-shape request collapses to ``{}`` and
    surfaces as a downstream field error (e.g. ``400 invalid config: ...``) instead
    of the body-shape mistake it actually is.

    The catch spans the full client-input failure set: an unknown ``charset=`` codec
    raises ``LookupError`` and undecodable bytes raise ``UnicodeDecodeError`` (a
    ``ValueError``, not a ``json.JSONDecodeError``), so catching only the latter would
    let those escape as a 500. Transport/disconnect errors are deliberately NOT caught
    (no bare ``Exception``) so a client disconnect propagates rather than being reported
    as a client mistake.
    """
    try:
        body = await request.json()
    except (LookupError, RecursionError, ValueError):
        return None, web.json_response(
            {"error": "invalid JSON body", "code": "invalid_json"}, status=400
        )
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "body must be a JSON object", "code": "body_not_object"}, status=400
        )
    return body, None


@_internal_denied
async def _handle_get_config(_request: web.Request) -> web.Response:
    """Deploy configuration, plus whether this installation may deploy at all.

    ``cloudDeploymentEnabled`` is what lets the frontend hide the surface instead
    of rendering a page whose every button 403s. Deliberately NOT gated itself: a
    denied read here would leave the UI unable to explain why deployment is
    unavailable.
    """
    # circular import: the dashboard handler layer imports the deploy module at
    # registration time, so this is a downward import.
    from kiro_crew.dashboard.handlers._shared import admits_cloud_deployment

    cfg = await asyncio.to_thread(_load_config)
    # In a worker thread: the admission path can initialize the SEL audit log,
    # which is blocking file IO on a fresh gateway.
    enabled = await asyncio.to_thread(admits_cloud_deployment, "aws")
    if isinstance(cfg, dict):
        cfg = {**cfg, "cloudDeploymentEnabled": enabled}
        # The setup guide's optional auto-cleanup step needs a command the user
        # can actually paste, and the browser cannot resolve where the skill is
        # installed — the path differs between a ~/.kirocrew and a ~/.kiro/crew
        # root. Resolved here through the same helper the reaper 409 uses, so the
        # guide and the refusal can never print different commands. Profile and
        # region are left to the page, which knows the user's current selection.
        cfg = {**cfg, "reaperInstallScript": _reaper_remediation("", "")}
    return web.json_response(cfg)


@_internal_denied
async def _handle_put_config(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "config_update")
    if denied:
        return denied
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None  # _json_body returns (dict, None) on success
    try:
        profile = validate_field(str(body.get("profile", "")), _PROFILE_SPEC)
        region = validate_field(str(body.get("region", "")), _REGION_SPEC)
    except ValidationError as e:
        return web.json_response({"error": f"invalid config: {e}"}, status=400)
    result = await asyncio.to_thread(_save_config, profile, region)
    _audit("config_update", profile, "allowed")
    # Config responses echo profile/region strings — route through
    # the sanitize chokepoint like every other deploy response.
    return web.json_response(_sanitize_response(result))


async def _handle_deploy(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "deploy")
    if denied:
        return denied
    # Publish-governance ceiling for THIS destination. The provider registry
    # already hides the button when this denies, but a hidden button is not a
    # control — the endpoint is reachable directly (and by the MCP preview path),
    # so the decision is re-made here. Same chokepoint as artifact publish, so an
    # operator has one place to close every publish destination.
    #
    # Off the loop: the decision reads the trust-root policy, every governance
    # profile, and config.json from disk. On a slow or contended data home that
    # walk would stall the gateway and its heartbeat for every caller, not just
    # this request — the provider-registry call site is offloaded for the same
    # reason.
    reason = await asyncio.to_thread(
        publish_denied_reason, request, DEPLOY_WEB_PROVIDER_ID
    )
    if reason:
        _audit("deploy", "", "denied", error=reason)
        return web.json_response(
            {
                "error": f"public web deploy is disabled by policy: {reason}",
                "code": "publish_destination_disabled",
            },
            status=403,
        )
    params, err = await _json_body(request)
    if err is not None:
        return err
    assert params is not None  # _json_body returns (dict, None) on success
    params = _strip_confirm_for_internal(request, params)
    status, payload = await _do_deploy(params)
    return web.json_response(_sanitize_response(payload), status=status)


@_internal_denied
async def _handle_recall(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "recall")
    if denied:
        return denied
    params, err = await _json_body(request)
    if err is not None:
        return err
    assert params is not None  # _json_body returns (dict, None) on success
    status, payload = await _do_recall(params)
    return web.json_response(_sanitize_response(payload), status=status)


@_internal_denied
async def _handle_destroy(request: web.Request) -> web.Response:
    denied = _deny_restricted(request, "destroy")
    if denied:
        return denied
    params, err = await _json_body(request)
    if err is not None:
        return err
    assert params is not None  # _json_body returns (dict, None) on success
    status, payload = await _do_destroy(params)
    return web.json_response(_sanitize_response(payload), status=status)


async def _handle_list(_request: web.Request) -> web.Response:
    status, payload = await _do_list()
    return web.json_response(payload, status=status)


@_internal_denied
async def _handle_iam_policy(request: web.Request) -> web.Response:
    """Generate the least-privilege IAM policy text for the user to apply (Option A)."""
    custom = request.query.get("custom_domain", "").lower() in ("1", "true", "yes")
    tier = request.query.get("tier", "static")
    if tier not in ("static", "fullstack"):
        return web.json_response({"error": "tier must be 'static' or 'fullstack'"}, status=400)
    payload: dict[str, Any] = {
        "policy": iam_mod.policy_json(include_custom_domain=custom, tier=tier),
    }
    if tier == "fullstack":
        # fullstack requires the permissions-boundary policy to exist
        # BEFORE the first deploy — iam:CreateRole is conditioned on it.
        payload["boundary_policy"] = json.dumps(
            iam_mod.boundary_policy_document(), indent=2)
        payload["boundary_policy_name"] = iam_mod.BOUNDARY_POLICY_NAME
        payload["boundary_note"] = (
            f"Create this policy once as '{iam_mod.BOUNDARY_POLICY_NAME}' — "
            "app roles are created with it as a permissions boundary, which "
            "caps their effective permissions."
        )
    return web.json_response(payload)


@_internal_denied
async def _handle_verify(request: web.Request) -> web.Response:
    """Read-only reachability check (NOT full verification, §9.3/Q3).

    Accepts an optional ``profile`` in the body to verify a specific registered
    profile; empty body verifies the default. A success back-fills the entry's
    ``account`` + ``verified_at`` in the registry (display metadata only).
    """
    if os.name == "nt":
        return web.json_response(
            {"reachable": False, "error": "Deploy scripts require a POSIX shell — use WSL."},
            status=400,
        )
    denied = _deny_restricted(request, "verify")
    if denied:
        return denied
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None  # _json_body returns (dict, None) on success
    try:
        profile, _region = await _resolve_profile(body)
    except _ProfileResolveError as e:
        return web.json_response({"reachable": False, **e.payload}, status=400)
    result = await asyncio.to_thread(iam_mod.reachability_check, profile)
    if result.get("reachable"):
        def _backfill_verify() -> None:
            with profiles_mod.locked_registry() as reg:
                entry = profiles_mod.get_entry(reg, profile)
                if entry is not None:
                    entry["account"] = str(result.get("account", "")) or entry["account"]
                    entry["verified_at"] = profiles_mod.now_iso()

        await asyncio.to_thread(_backfill_verify)
        # Back-filling account/verified_at is a registry write — audited
        # like every other registry mutation in this file.
        _audit("profile_verify", profile, "allowed")
    return web.json_response(_sanitize_response({**result, "profile": profile}))


async def _handle_pricing(request: web.Request) -> web.Response:
    """Live unit prices for cost estimates.

    Read-only: queries the AWS Pricing API through the resolved profile and
    returns per-unit USD prices with a ``source`` marker (``live`` vs
    ``fallback``). Estimate consumers (skill contract, cost surfaces) use
    these instead of the hardcoded MVP table when available. Failures of any
    kind degrade to the fallback table -- an estimate must never 500.
    """
    denied = _deny_restricted(request, "pricing")
    if denied:
        return denied
    query = dict(request.rel_url.query)
    try:
        profile, region = await _resolve_profile(query)
    except _ProfileResolveError as e:
        return web.json_response(e.payload, status=400)
    prices = await asyncio.to_thread(pricing_mod.get_unit_prices, profile, region)
    _audit("pricing", profile, "allowed")
    return web.json_response(_sanitize_response({
        "region": region,
        "prices": prices.to_dict(),
    }))


# --- profile control plane (§ multi-profile) --------------------------------

@_internal_denied
async def _handle_profiles_get(_request: web.Request) -> web.Response:
    """Registry + AWS-CLI-discovered profile names (names only, read-only)."""
    reg = await asyncio.to_thread(profiles_mod.load_registry)
    try:
        discovered = await asyncio.to_thread(profiles_mod.discover_aws_profiles)
    except SandboxUnavailableError as exc:
        # Discovery spawns the AWS CLI through wrap_argv, and on a host with no
        # sandbox backend (`unshare` EPERM) and allow_unsandboxed_exec unset
        # that raises. SandboxUnavailableError subclasses RuntimeError -- the
        # shape nothing here caught -- so it escaped and aiohttp answered its
        # bare "Server got itself in trouble" 500. This is the one degraded
        # cause no retry clears, so it must not be an anonymous 500: answer the
        # typed 503 the chat path renders for the same condition
        # (dashboard/handlers/agents.py api_models), carrying the machine
        # -readable code plus the probe detail and remedy the exception has.
        logger.warning(
            "deploy-web: profile discovery refused by the sandbox "
            "(kind=%s, detail=%s); returning 503. Retrying will not clear this.",
            exc.kind, exc.detail,
        )
        return web.json_response(
            {
                "error": "profile discovery unavailable: no sandbox backend on this host",
                "code": "sandbox_unavailable",
                "kind": exc.kind,
                "detail": exc.detail,
                "remedy": exc.remedy,
            },
            status=503,
        )
    registered = {p["name"] for p in reg["profiles"]}
    # The blocking credential-redaction rule applies to EVERY
    # profile-related string in the response -- `default` and the discovered
    # `available` names were the two unredacted stragglers.
    return web.json_response({
        "profiles": _redact_profile_fields(reg["profiles"]),
        "default": _redact_text(str(reg["default"])),
        # `discovered or []` deliberately does NOT carry the could-not-ask state:
        # this endpoint feeds a profile picker, and both readers hide their
        # section on an empty list, so a field for the difference would ship with
        # no consumer. Narrowing `None` here is still required -- iterating it
        # raised TypeError. The distinction an operator acts on lives on the
        # aws-control profile routes, which answer 503 or 501 instead.
        "available": [_redact_text(str(n))
                      for n in (discovered or []) if n not in registered],
    })


@_internal_denied
async def _handle_profiles_post(request: web.Request) -> web.Response:
    """Register a profile; with ``create=true`` also write the AWS config block.

    The write path is the significant permission decision here (it defines how
    future deploys obtain credentials), so it is SEL-audited and confined to
    ``aws configure set`` on an allowlisted key set (see profiles.py).
    """
    denied = _deny_restricted(request, "profile_create")
    if denied:
        return denied
    body, body_err = await _json_body(request)
    if body_err is not None:
        return body_err
    assert body is not None  # _json_body returns (dict, None) on success
    try:
        name = validate_field(str(body.get("name", "")), _PROFILE_SPEC)
        region = validate_field(str(body.get("region", "")) or DEFAULT_REGION, _REGION_SPEC)
    except ValidationError as e:
        return web.json_response({"error": f"invalid profile: {e}"}, status=400)
    # validate_field skips the pattern check on an empty string and
    # _PROFILE_SPEC is not required, so "" sails through — and downstream
    # engine._aws() OMITS --profile for an empty name, meaning
    # ``aws configure set`` would silently rewrite the user's DEFAULT AWS
    # profile. Reject empty explicitly before any side effect.
    if not name:
        _audit("profile_create", "", "denied-empty-name")
        return web.json_response({"error": "invalid profile: name must not be empty"}, status=400)

    # Fail-fast on capacity BEFORE any side-effects (create_aws_profile writes ~/.aws/config)
    reg_pre = await asyncio.to_thread(profiles_mod.load_registry)
    is_new = profiles_mod.get_entry(reg_pre, name) is None
    if is_new and len(reg_pre["profiles"]) >= 50:
        return web.json_response({"error": "profile registry is full (50)"}, status=400)

    if body.get("create") is True:
        err = await asyncio.to_thread(
            profiles_mod.create_aws_profile, name, region,
            account=str(body.get("account", "") or ""),
            role=str(body.get("role", "") or ""))
        _audit("profile_create", name, "denied" if err else "allowed", error=err or "")
        if err:
            # Raw CLI stderr may carry credential fragments -- redact before
            # returning to the dashboard (the SEL audit above keeps the raw text).
            return web.json_response({"error": _safe_err(Exception(err))}, status=400)

    # Atomic RMW under lock — concurrent POST/PUT/DELETE won't lose each other's writes.
    # The capacity check must be re-verified INSIDE the lock. The
    # pre-lock check above is only a fast-fail before the create_aws_profile
    # side effect; two concurrent POSTs at 49 profiles could both pass it,
    # append to 51, and load_registry()'s truncate-to-50 would silently drop
    # one registered profile on the next read.
    def _post_rmw() -> tuple[dict, str]:
        with profiles_mod.locked_registry() as reg:
            fresh_is_new = profiles_mod.get_entry(reg, name) is None
            if fresh_is_new and len(reg["profiles"]) >= 50:
                return reg, "profile registry is full (50)"
            if fresh_is_new:
                reg["profiles"].append(profiles_mod.make_entry(name, region))
            if body.get("default") or not reg["default"]:
                reg["default"] = name
        return reg, ""

    reg, cap_err = await asyncio.to_thread(_post_rmw)
    if cap_err:
        _audit("profile_register", name, "denied", error=cap_err)
        return web.json_response({"error": cap_err}, status=400)
    # Registration itself is a permission decision (the name becomes deployable),
    # so it is audited unconditionally — separate from the create write-path audit.
    _audit("profile_register", name, "allowed")
    return web.json_response({
        "profiles": _redact_profile_fields(reg["profiles"]),
        "default": _redact_text(str(reg["default"])),
    })


@_internal_denied
async def _handle_profiles_put(request: web.Request) -> web.Response:
    """Edit a registered profile's region/note, or make it the default."""
    denied = _deny_restricted(request, "profile_update")
    if denied:
        return denied
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None  # _json_body returns (dict, None) on success
    # match_info name is LLM/user-influenceable — validate like the POST path
    # before it reaches the registry lookup / error strings / audit call.
    try:
        name = validate_field(request.match_info.get("name", ""), _PROFILE_SPEC)
    except ValidationError as e:
        return web.json_response({"error": _redact_text(f"invalid profile name: {e}")}, status=400)

    # Pre-validate body fields before taking the lock (avoids holding it during
    # user-error returns)
    new_region: str | None = None
    if "region" in body:
        try:
            new_region = validate_field(str(body["region"]), _REGION_SPEC)
        except ValidationError as e:
            return web.json_response({"error": _redact_text(f"invalid region: {e}")}, status=400)

    # Atomic RMW under lock
    def _put_rmw() -> dict | str:
        with profiles_mod.locked_registry() as reg:
            entry = profiles_mod.get_entry(reg, name)
            if entry is None:
                return f"profile '{name}' not registered"
            if new_region is not None:
                entry["region"] = new_region
            if "note" in body:
                entry["note"] = str(body["note"])[:256]
            if body.get("default"):
                reg["default"] = name
        return reg

    result = await asyncio.to_thread(_put_rmw)
    if isinstance(result, str):
        return web.json_response({"error": result}, status=404)
    # Changing region/default alters which AWS identity/region future deploys
    # run with — a permission decision, so it is SEL-audited like profile_create.
    _audit("profile_update", name, "allowed")
    return web.json_response({
        "profiles": _redact_profile_fields(result["profiles"]),
        "default": _redact_text(str(result["default"])),
    })


@_internal_denied
async def _handle_profiles_delete(request: web.Request) -> web.Response:
    """Remove a profile from the registry ONLY — never touches ~/.aws/config."""
    denied = _deny_restricted(request, "profile_delete")
    if denied:
        return denied
    # match_info name is LLM/user-influenceable — validate like the POST path.
    try:
        name = validate_field(request.match_info.get("name", ""), _PROFILE_SPEC)
    except ValidationError as e:
        return web.json_response({"error": _redact_text(f"invalid profile name: {e}")}, status=400)
    reg = await asyncio.to_thread(profiles_mod.load_registry)
    if profiles_mod.get_entry(reg, name) is None:
        return web.json_response({"error": _redact_text(f"profile '{name}' not registered")}, status=404)

    # Atomic RMW under lock
    def _delete_rmw() -> dict | str:
        with profiles_mod.locked_registry() as reg:
            if profiles_mod.get_entry(reg, name) is None:
                return f"profile '{name}' not registered"
            reg["profiles"] = [p for p in reg["profiles"] if p["name"] != name]
            if reg["default"] == name:
                reg["default"] = reg["profiles"][0]["name"] if reg["profiles"] else ""
        return reg

    result = await asyncio.to_thread(_delete_rmw)
    if isinstance(result, str):
        return web.json_response({"error": result}, status=404)
    # Removing a deployable identity (and possibly reassigning the default) is
    # a permission decision — SEL-audited like profile_create.
    _audit("profile_delete", name, "allowed")
    return web.json_response({
        "profiles": _redact_profile_fields(result["profiles"]),
        "default": _redact_text(str(result["default"])),
    })


def _check_reaper_installed(profile: str, region: str) -> bool:
    """Check if the kirocrew-deploy-reaper stack exists in the account.

    Returns True if describe-stacks succeeds (stack exists), False otherwise.
    Blocking — must be called via to_thread.
    """
    rc, _out, _err = engine.run_aws(
        ["cloudformation", "describe-stacks",
         "--stack-name", "kirocrew-deploy-reaper",
         "--region", region],
        profile, 10,
    )
    return rc == 0


async def _expire_manifest_best_effort(art: Any) -> str:
    """Best-effort rewrite of the S3 deploy manifest to expires_at=now.

    Uses the artifact's recorded profile and region via the same engine.run_aws
    path that the deploy itself uses. Returns one of:
    - "expired-now": manifest rewritten, reaper collects on next sweep
    - "skipped": the metadata records NO usable deploy identity (no
      deploy_target/profile/slug) — there is nothing to expire; the caller may
      tombstone (this is distinct from a failed write)
    - "unreachable": a usable identity exists but the expiry write FAILED —
      the caller must fail retryably instead of tombstoning, or a finite-TTL
      deployment would keep running until its original TTL while the UI
      reports success

    Security: profile and region are validated through the registry before use —
    a forged metadata.slug or metadata.profile cannot inject unvalidated input
    into subprocess argv.
    """
    meta = art.webapp_metadata
    if meta is None:
        return "skipped"

    # ALWAYS use the validated artifact slug, never metadata slug (which could
    # be forged to target a different deployment's manifest).
    slug = art.slug
    if not slug:
        return "skipped"

    # No deploy_target or no recorded profile means the artifact never
    # captured a usable deploy identity — there is no manifest we could expire.
    if meta.deploy_target is None or not meta.deploy_target.profile:
        return "skipped"

    # Validate the profile through the registry — if the metadata records a
    # profile that does not exist (or was never registered), refuse to execute
    # aws CLI with unvalidated input.
    raw_profile = meta.deploy_target.profile
    raw_region = meta.deploy_target.region or engine.DEFAULT_REGION

    # Registry check: the profile must be a currently-registered name.
    reg = await asyncio.to_thread(profiles_mod.load_registry)
    entry = profiles_mod.get_entry(reg, raw_profile) if raw_profile else None
    if entry is None:
        _audit("teardown-manifest", slug, "unreachable",
               error=f"profile '{raw_profile}' not in registry")
        return "unreachable"
    profile = entry["name"]

    # Validate region with existing spec before passing to subprocess.
    try:
        region = validate_field(raw_region, _REGION_SPEC)
    except ValidationError:
        _audit("teardown-manifest", slug, "unreachable",
               error=f"invalid region '{raw_region}'")
        return "unreachable"

    # Find the bucket from the base stack (same pattern as deploy.sh).
    try:
        rc, out, _ = await asyncio.to_thread(
            engine.run_aws,
            ["cloudformation", "describe-stacks",
             "--stack-name", "kirocrew-deploy-base",
             "--query", "Stacks[0].Outputs", "--output", "json",
             "--region", region],
            profile, 15,
        )
        if rc != 0 or not out:
            logger.warning("teardown manifest-expiry: cannot read base stack (rc=%d)", rc)
            _audit("teardown-manifest", slug, "unreachable", error="base stack read failed")
            return "unreachable"
        outputs = {o["OutputKey"]: o["OutputValue"] for o in json.loads(out)}
        bucket = outputs.get("BucketName", "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("teardown manifest-expiry: %s", exc)
        _audit("teardown-manifest", slug, "unreachable", error=str(exc)[:200])
        return "unreachable"

    if not bucket:
        _audit("teardown-manifest", slug, "unreachable", error="missing bucket")
        return "unreachable"

    # PATCH the existing manifest instead of replacing it — a fresh
    # write drops arch/bucket/distribution_id/oac_id, sending engine-arch
    # deployments down the wrong reaper path (S3-prefix-only) and leaking the
    # distribution + per-site bucket. Download, patch ONLY the expiry fields
    # (preserving unknown fields too), and fall back to a fresh write only if
    # no existing manifest can be read.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_doc: dict[str, Any] = {}
    try:
        rc_m, out_m, _ = await asyncio.to_thread(
            engine.run_aws,
            ["s3", "cp", f"s3://{bucket}/{slug}/.kirocrew-deploy.json", "-",
             "--region", region],
            profile, 15,
        )
        if rc_m == 0 and out_m:
            parsed = json.loads(out_m)
            if isinstance(parsed, dict):
                manifest_doc = parsed
    except Exception as exc:  # noqa: BLE001 — read failure handled fail-closed below
        logger.debug("teardown manifest read failed: %s", exc)

    # Fail-closed: a forged webapp artifact whose
    # slug matches another deployment must NOT be able to expire it.
    #  - No readable manifest -> REFUSE (a fresh expired manifest under an
    #    attacker-chosen slug key would direct the reaper at someone else's
    #    infra; nothing legitimate needs a fresh-write here).
    #  - Identity check requires BOTH ids nonempty AND equal. Deploys in this
    #    PR always persist distribution_id on both sides, so a missing id is
    #    itself a red flag, not a compat case.
    if not manifest_doc:
        _audit("teardown-manifest", slug, "denied",
               error="no existing manifest readable (fail closed)")
        return "unreachable"
    meta_dist_id = getattr(meta.deploy_target, "distribution_id", "") if meta.deploy_target else ""
    manifest_dist_id = manifest_doc.get("distribution_id", "")
    if not meta_dist_id or not manifest_dist_id or meta_dist_id != manifest_dist_id:
        _audit("teardown-manifest", slug, "identity_mismatch",
               error=f"metadata.distribution_id={meta_dist_id!r} != manifest.distribution_id={manifest_dist_id!r}")
        return "unreachable"

    manifest_doc["expires_at"] = now_iso
    manifest_doc["persistent"] = False
    manifest_doc["ttl_hours"] = "0"
    expired_manifest = json.dumps(manifest_doc)

    def _write_manifest_tmp() -> str:
        """Write manifest JSON to a temp file. Blocking — run via to_thread."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        try:
            f.write(expired_manifest)
            f.close()
            return f.name
        except BaseException:
            f.close()
            try:
                os.unlink(f.name)
            except OSError:
                pass
            raise

    tmp_path = ""
    try:
        tmp_path = await asyncio.to_thread(_write_manifest_tmp)
        rc, _, err = await asyncio.to_thread(
            engine.run_aws,
            ["s3", "cp", tmp_path,
             f"s3://{bucket}/{slug}/.kirocrew-deploy.json",
             "--content-type", "application/json",
             "--region", region],
            profile, 15,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("teardown manifest-expiry put failed: %s", exc)
        _audit("teardown-manifest", slug, "unreachable", error=str(exc)[:200])
        return "unreachable"
    finally:
        if tmp_path:
            try:
                await asyncio.to_thread(os.unlink, tmp_path)
            except OSError:
                pass

    if rc != 0:
        logger.warning("teardown manifest-expiry: S3 put rc=%d err=%s", rc, err[:200])
        _audit("teardown-manifest", slug, "unreachable", error=f"rc={rc}")
        return "unreachable"

    _audit("teardown-manifest", slug, "expired-now")
    return "expired-now"


@_internal_denied
async def _handle_teardown(request: web.Request) -> web.Response:
    """Human-triggered cancel of a kind="webapp" artifact.

    Owner-only (denied for restricted sessions), confirm-gated. v1 marks the
    artifact as a tombstone (lifecycle.status="expired", kept as deploy history)
    and returns the teardown handle + resources so the in-account reaper
    performs the actual infrastructure deletion. Core issues no AWS calls itself.
    """
    if os.name == "nt":
        return web.json_response(
            {"error": "Artifact Deploy requires a POSIX shell (bash) for deploy scripts. "
             "Use WSL (Windows Subsystem for Linux) to run the Kiro Crew gateway on Windows."},
            status=400,
        )
    if not _HAS_ARTIFACTS:
        return web.json_response({"error": "artifacts module unavailable"}, status=500)

    # ── Restricted-session deny (shared helper, consistent across all mutating endpoints) ──
    denied = _deny_restricted(request, "teardown")
    if denied:
        return denied

    # ── Server-side confirm gate ──
    try:
        raw = await request.read()
        body = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    if body.get("confirm") is not True:
        return web.json_response(
            {"error": "confirm=true required in request body"}, status=400)

    slug = request.match_info.get("slug", "")
    # ── Load artifact read-only FIRST to get metadata for manifest expiry ──
    try:
        art = await asyncio.to_thread(get_default_store().get, slug)
    except ArtifactNotFoundError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=404)
    except ArtifactError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=500)

    # Validate the artifact is a webapp with metadata before proceeding.
    if getattr(art, "kind", None) != "webapp":
        _audit("teardown", slug, "denied", error="not a webapp")
        return web.json_response({"error": "not a webapp artifact"}, status=400)

    meta = art.webapp_metadata

    # ── Reaper prerequisite check ──
    # The teardown flow relies on the in-account reaper Lambda to clean up
    # infrastructure after the manifest expires. Without the reaper, tombstoning
    # the artifact would leave the infra running with no automated cleanup path.
    profile_for_check = (meta.deploy_target.profile if meta and meta.deploy_target else None)
    region_for_check = (meta.deploy_target.region if meta and meta.deploy_target else None) or engine.DEFAULT_REGION

    # ── Registry validation ──
    # Profile and region come from LLM-writable webapp_metadata. Validate them
    # through the profiles registry BEFORE passing to any subprocess.
    if profile_for_check:
        from kiro_crew.deploy import profiles as profiles_mod
        from kiro_crew.validation import ValidationError

        reg = await asyncio.to_thread(profiles_mod.load_registry)
        entry = profiles_mod.get_entry(reg, profile_for_check)
        if entry is None:
            _audit("teardown", slug, "denied",
                   error=f"artifact metadata names an unregistered profile: {profile_for_check!r}")
            return web.json_response(
                {"error": "artifact metadata names an unregistered profile"},
                status=409,
            )
        # Use the validated profile name from registry
        profile_for_check = entry["name"]

        # Validate region format
        try:
            profiles_mod.validate_field(region_for_check, profiles_mod.REGION_SPEC)
        except ValidationError:
            _audit("teardown", slug, "denied",
                   error=f"artifact metadata has invalid region: {region_for_check!r}")
            return web.json_response(
                {"error": "artifact metadata has invalid region"},
                status=409,
            )

    if profile_for_check:
        reaper_installed = await asyncio.to_thread(
            _check_reaper_installed, profile_for_check, region_for_check)
        if not reaper_installed:
            _audit("teardown", slug, "denied", error="reaper not installed")
            return web.json_response({
                "error": (
                    "reaper not installed — run install-reaper.sh or "
                    "teardown.sh from your terminal"
                ),
                "reaper_missing": True,
            }, status=409)

    # ── Best-effort S3 manifest expiry FIRST (before tombstone) ──
    # An unreachable manifest means the reaper cannot see an immediate
    # expiry — for BOTH persistent and finite-TTL deploys. Tombstoning anyway
    # would report success while a finite-TTL deployment keeps running (and
    # billing) until its ORIGINAL TTL, with the card's live URL hidden and the
    # Tear down retry disabled. Fail retryable in both cases; the card keeps
    # its Tear down button until expiry is actually written.
    manifest_status = await _expire_manifest_best_effort(art)

    if manifest_status == "unreachable":
        _audit("teardown", slug, "retry-later",
               error="manifest unreachable — teardown not applied")
        return web.json_response({
            "error": "manifest unreachable — teardown not applied, retry later",
            "manifest": "unreachable",
            "retry": True,
        }, status=502)

    # ── NOW tombstone the artifact (manifest is already expired) ──
    try:
        art = await asyncio.to_thread(get_default_store().mark_webapp_expired, slug)
    except ArtifactNotFoundError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=404)
    except ArtifactValidationError as exc:
        _audit("teardown", slug, "denied", error=str(exc))
        return web.json_response({"error": str(exc)}, status=400)
    except ArtifactError as exc:
        _audit("teardown", slug, "error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=500)

    # Serialize through the redaction path so handle/resource ids can't bypass.
    from kiro_crew.dashboard.handlers.artifacts import _serialize

    serialized = _serialize(art, include_content=True)
    redacted_meta = serialized.get("webapp_metadata") or {}
    redacted_teardown = redacted_meta.get("teardown") or {}
    redacted_resources = (redacted_meta.get("architecture") or {}).get("resources", [])
    teardown = {
        "method": redacted_teardown.get("method", ""),
        "handle": redacted_teardown.get("handle", ""),
        "resources": redacted_resources,
    }

    _audit("teardown", slug, "allowed")
    return web.json_response({
        "ok": True,
        "artifact": serialized,
        "teardown": teardown,
        "manifest": manifest_status,
        "note": "the in-account reaper will remove infrastructure on its next sweep",
    })


# ── Pending confirmations ──────────────────────────────────────────────


@_internal_denied
async def _handle_pending_list(request: web.Request) -> web.Response:
    """GET /api/deploy/pending — list pending deploy confirmations.

    Only cookie/token-authenticated callers allowed (rejects internal-secret).
    All string values are recursively redacted to prevent credential
    leakage through LLM-controlled scan_summary/local_dir/profile fields.
    """
    from kiro_crew.deploy.pending import list_pending
    entries = await asyncio.to_thread(list_pending)
    return web.json_response({"pending": _redact_pending_entries(entries)})


@_internal_denied
async def _handle_pending_confirm(request: web.Request) -> web.Response:
    """POST /api/deploy/pending/{id}/confirm — execute a pending deploy.

    Re-runs _do_deploy with stored params + confirm=true. Only cookie/token-auth.
    Uses atomic claim_pending to prevent double-deploy from concurrent confirms.
    Validates that the profile and content have not changed since preview.
    """
    denied = _deny_restricted(request, "pending_confirm")
    if denied:
        return denied
    # Publish-governance ceiling, re-made at confirm time: a pending entry
    # created before the operator closed the destination must NOT still be
    # confirmable. Checked BEFORE claim_pending so a denied confirm leaves the
    # entry intact rather than consuming it. Off the loop for the same reason as
    # the deploy handler — the decision reads policy, profiles and config from disk.
    reason = await asyncio.to_thread(
        publish_denied_reason, request, DEPLOY_WEB_PROVIDER_ID
    )
    if reason:
        _audit("pending_confirm", request.match_info.get("id", ""), "denied", error=reason)
        return web.json_response(
            {
                "error": f"public web deploy is disabled by policy: {reason}",
                "code": "publish_destination_disabled",
            },
            status=403,
        )
    entry_id = request.match_info["id"]
    from kiro_crew.deploy.pending import add_pending, claim_pending
    entry = await asyncio.to_thread(claim_pending, entry_id)
    if not entry:
        _audit("pending_confirm", entry_id, "denied",
               error="not found, expired, or already claimed")
        return web.json_response(
            {"error": "pending entry not found, expired, or already claimed"}, status=409
        )

    # Staleness check — re-resolve profile and compare to stored canonical.
    stored_profile = entry.get("profile", "")
    stored_region = entry.get("region", "")
    try:
        current_profile, current_region = await _resolve_profile(
            {"profile": stored_profile}
        )
    except _ProfileResolveError:
        current_profile, current_region = "", ""
    if stored_profile and (current_profile != stored_profile or current_region != stored_region):
        # Profile resolution changed — re-add entry and reject.
        await asyncio.to_thread(add_pending, entry)
        _audit("pending_confirm", entry_id, "denied",
               error="profile drift since preview")
        return web.json_response(
            {"error": "profile changed since preview — re-run preview to confirm with the "
             "current configuration"},
            status=409,
        )

    # Content digest staleness check for local_dir deploys.
    # Re-validate path confinement + sensitive-path + size guard at confirm time
    # (the directory could have been swapped or grown between preview and confirm).
    stored_digest = entry.get("content_digest", "")
    if stored_digest and entry.get("local_dir"):
        local_dir = entry["local_dir"]
        local_path = Path(os.path.normpath(os.path.expanduser(local_dir)))

        # Consolidate ALL blocking confinement pre-checks into ONE
        # asyncio.to_thread hop (is_dir + resolve + allowed-roots + sensitive).
        def _confirm_confinement_check(
            lp: Path,
        ) -> str | None:
            """Blocking: validate directory exists, is confined, and is clean.

            Returns None on success; returns an error string on failure.
            """
            if not lp.is_dir():
                return "local_dir no longer exists — re-run preview"
            resolved = lp.resolve()
            resolved_str = os.path.normpath(str(resolved))
            roots = _allowed_local_roots()
            allowed = any(
                resolved_str == os.path.normpath(str(r))
                or resolved_str.startswith(os.path.normpath(str(r)) + os.sep)
                for r in roots
            )
            if not allowed:
                return "local_dir outside allowed roots at confirm time"
            if _dir_contains_sensitive(lp, resolved):
                return "local_dir contains sensitive paths at confirm time"
            return None

        confinement_error = await asyncio.to_thread(_confirm_confinement_check, local_path)
        if confinement_error:
            await asyncio.to_thread(add_pending, entry)
            _audit("pending_confirm", entry_id, "denied",
                   error=confinement_error)
            return web.json_response({"error": confinement_error}, status=409)

        # Size guard, the same _MAX_STAGE_BYTES cap as the deploy path.
        tree_size = await asyncio.to_thread(_compute_tree_size_global, local_path)
        if tree_size > _MAX_STAGE_BYTES:
            await asyncio.to_thread(add_pending, entry)
            _audit("pending_confirm", entry_id, "denied",
                   error=f"size guard exceeded ({tree_size} bytes)")
            return web.json_response(
                {"error": f"local_dir tree exceeds 200 MiB at confirm time ({tree_size} bytes)"},
                status=409,
            )

        # The early live-path digest read (Path.read_bytes on an
        # LLM-controlled path, racy vs staging) is removed — _do_deploy
        # verifies expected_content_digest against the SAFELY STAGED snapshot
        # (symlink-free, hooks-gated), which is the authoritative check.

    # Content-digest staleness check for ARTIFACT deploys too — the
    # artifact can be edited (artifact_update) between preview and confirm,
    # deploying different content than was approved. Re-stage the CURRENT
    # artifact content and compare its digest with the one stored at preview.
    if stored_digest and entry.get("artifact_slug") and _HAS_ARTIFACTS:
        def _current_artifact_digest(slug: str) -> str | None:
            """Blocking: stage current artifact content, digest it, clean up."""
            try:
                art = get_default_store().get(slug)
                _f, staged, _b = _stage_artifact_html(
                    art.kind, art.content or "", art.name or "")
            except Exception:
                return None  # deleted / not stageable — let _do_deploy report it
            try:
                return _compute_content_digest(staged)
            finally:
                shutil.rmtree(staged, ignore_errors=True)

        current = await asyncio.to_thread(
            _current_artifact_digest, entry["artifact_slug"])
        if current is not None and current != stored_digest:
            await asyncio.to_thread(add_pending, entry)
            _audit("pending_confirm", entry_id, "denied",
                   error="content digest mismatch (artifact modified)")
            return web.json_response(
                {"error": "content changed since preview — the artifact was "
                 "modified after approval; re-run preview"},
                status=409,
            )

    # Build params from stored entry and force confirm=true
    params: dict[str, Any] = {
        "site_id": entry.get("site_id", ""),
        "confirm": True,
    }
    if entry.get("artifact_slug"):
        params["artifact_slug"] = entry["artifact_slug"]
    if entry.get("local_dir"):
        params["local_dir"] = entry["local_dir"]
    if entry.get("profile"):
        params["profile"] = entry["profile"]
    if entry.get("region"):
        params["region"] = entry["region"]
    if entry.get("ttl_hours") is not None:
        params["ttl_hours"] = entry["ttl_hours"]
    # Bind the stored (previewed) identity so _do_deploy's own
    # comparison closes the registry-change race after approval.
    if entry.get("profile"):
        params["expected_profile"] = entry["profile"]
    if entry.get("region"):
        params["expected_region"] = entry["region"]
    # Pass expected_content_digest into _do_deploy so the staged
    # snapshot is verified at deploy time (TOCTOU between handler check above
    # and _do_deploy's own staging). The handler's early check is fast-fail;
    # this param is the authoritative content-integrity gate.
    if stored_digest:
        params["expected_content_digest"] = stored_digest
    # Entries flagged override_scan_required were scan-blocked at preview
    # by OVERRIDABLE (non-credential) findings. Confirming them requires the
    # human to send override_scan=true explicitly (the "deploy anyway" action
    # in the dashboard) — otherwise _do_deploy re-blocks on the same findings.
    # This handler is @_internal_denied, so the flag can only originate from a
    # cookie/token-authenticated human, never from the MCP caller.
    if entry.get("override_scan_required"):
        # Deliberate divergence from the _json_body 400 contract: the pending
        # entry was already popped above, so answering a body-shape 400 here
        # would drop it. A malformed or absent body therefore means "no
        # override" — _do_deploy re-blocks on the same findings, which is the
        # conservative outcome — rather than an error response.
        body, _body_err = await _json_body(request)
        if body is not None and body.get("override_scan") is True:
            params["override_scan"] = True
            _audit("pending_confirm", entry_id, "allowed",
                   error="human override_scan on non-credential findings")
    # `_do_deploy` converts an engine.AWSError into a 502 itself, but anything
    # else it raises -- a subprocess.TimeoutExpired from a slow aws call, an OSError
    # from the staging tree -- would escape past the re-add below. The entry has
    # already been CLAIMED at this point, so an escaping exception loses it: the
    # row disappears from the card, the response carries no usable body, and the
    # user is left with neither a retry nor a reason. Catch everything, put the
    # entry back, and answer with a plain sentence plus the exception text in
    # `details` (the banner/Details split every other refusal here uses).
    try:
        status, payload = await _do_deploy(params)
    except Exception as exc:  # noqa: BLE001 -- the entry must survive any failure
        await asyncio.to_thread(add_pending, entry)
        _audit("pending_confirm", entry_id, "failure", error=f"{type(exc).__name__}: {exc}")
        logger.exception("pending confirm %s failed before returning a status", entry_id)
        # The dict stays a visible literal and only the exception text is
        # redacted: `test_error_code_contract.py` ratchets bodies it cannot read
        # statically, so wrapping the whole dict in a call hides the `code` from
        # the gate even though the field is right there.
        return web.json_response(
            {
                "error": (
                    "The deploy stopped partway and it is not known whether "
                    "anything was published, so check the Deployments list "
                    "before retrying. The entry is back in Pending "
                    "confirmations."
                ),
                "code": "deploy_interrupted",
                "details": _redact_text(f"{type(exc).__name__}: {exc}"),
            },
            status=502,
        )
    if status != 200 or payload.get("requires_confirm"):
        # Deploy failed — re-add entry so user can retry
        await asyncio.to_thread(add_pending, entry)
    return web.json_response(_sanitize_response(payload), status=status)


@_internal_denied
async def _handle_pending_dismiss(request: web.Request) -> web.Response:
    """POST /api/deploy/pending/{id}/dismiss — discard a pending deploy.

    Only cookie/token-authenticated callers allowed (rejects internal-secret).
    Restricted sessions are denied. Audit trail recorded.
    """
    if _is_internal_secret_request(request):
        _audit("pending_dismiss", request.match_info.get("id", ""), "denied",
               error="internal-secret caller")
        return web.json_response({"error": "forbidden for MCP callers"}, status=403)
    denied = _deny_restricted(request, "pending_dismiss")
    if denied:
        return denied
    entry_id = request.match_info["id"]
    from kiro_crew.deploy.pending import remove_pending
    removed = await asyncio.to_thread(remove_pending, entry_id)
    if not removed:
        _audit("pending_dismiss", entry_id, "not_found")
        return web.json_response({"error": "pending entry not found or expired"}, status=404)
    _audit("pending_dismiss", entry_id, "ok")
    return web.json_response({"ok": True})


def _cloud_gated(handler):
    """Refuse a deploy PROVISIONING mutation when the platform withholds it.

    Applied at REGISTRATION rather than inside each handler: several endpoints
    reach AWS, and a per-handler check is a list the next endpoint can silently be
    added without. Wrapping here means a new provisioning route is gated by being
    listed below, next to the assertion that already forces every handler to
    declare its MCP exposure.

    WITHDRAWAL IS NEVER GATED. ``recall``, ``destroy`` and ``teardown`` tear down
    infrastructure that already exists and take a public URL OFF the internet, so
    gating them would strand exposure created while deployment was still permitted:
    an operator who disables cloud deployment would be unable to remove a live site
    through the supported API. A control that blocks provisioning must not also
    block undoing it.

    Read endpoints stay open for the same family of reasons — ``/api/deploy/config``
    is what tells the frontend to hide the surface, and ``list`` / ``pricing`` /
    ``iam-policy`` disclose no infrastructure while letting an operator see what a
    already-permitted deployment left behind.

    Runs the check in a worker thread: the admission path can initialize the SEL
    audit log, which on a fresh gateway does blocking file IO (trust-dir
    creation, key validation) and would otherwise stall the event loop for
    every request.
    """

    @functools.wraps(handler)
    async def _guarded(request: web.Request) -> web.StreamResponse:
        # circular import: the dashboard handler layer imports the deploy module
        # at registration time, so this is a downward import — same pattern as
        # `_is_restricted_session` in `handlers_guards._deny_restricted`.
        from kiro_crew.dashboard.handlers._shared import admits_cloud_deployment

        if not await asyncio.to_thread(admits_cloud_deployment, "aws"):
            return web.json_response(
                {
                    "error": "cloud deployment is disabled for this installation",
                    "code": "cloud_deployment_denied",
                },
                status=403,
            )
        return await handler(request)

    # Explicit marker rather than relying on ``__wrapped__``: the pre-existing
    # ``@_internal_denied`` decorator also wraps handlers, so a wrapper check
    # cannot tell the two apart. ``test_provisioning_routes_are_gated_but_
    # withdrawal_is_not`` reads this off the live route table.
    _guarded._cloud_gated = True  # type: ignore[attr-defined]
    return _guarded


def register_routes(app: web.Application) -> None:
    """Mount deploy routes under /api/deploy/* (core module)."""
    r = app.router
    r.add_get("/api/deploy/config", _handle_get_config)
    r.add_put("/api/deploy/config", _cloud_gated(_handle_put_config))
    r.add_get("/api/deploy/profiles", _handle_profiles_get)
    r.add_post("/api/deploy/profiles", _cloud_gated(_handle_profiles_post))
    r.add_put("/api/deploy/profiles/{name}", _cloud_gated(_handle_profiles_put))
    r.add_delete("/api/deploy/profiles/{name}", _cloud_gated(_handle_profiles_delete))
    r.add_get("/api/deploy/iam-policy", _handle_iam_policy)
    r.add_post("/api/deploy/verify", _cloud_gated(_handle_verify))
    r.add_get("/api/deploy/pricing", _handle_pricing)
    r.add_post("/api/deploy/deploy", _cloud_gated(_handle_deploy))
    # Withdrawal is deliberately UNGATED — see _cloud_gated. These take a live
    # public URL off the internet, so blocking them would strand exposure created
    # while deployment was still permitted.
    r.add_post("/api/deploy/recall", _handle_recall)
    r.add_post("/api/deploy/destroy", _handle_destroy)
    r.add_get("/api/deploy/list", _handle_list)
    r.add_post("/api/deploy/teardown/{slug}", _handle_teardown)
    # ── Pending confirmations ──
    r.add_get("/api/deploy/pending", _handle_pending_list)
    r.add_post("/api/deploy/pending/{id}/confirm", _cloud_gated(_handle_pending_confirm))
    r.add_post("/api/deploy/pending/{id}/dismiss", _handle_pending_dismiss)

    # ── Registration-time assertion: every handler must be in the allowlist
    # or carry the @_internal_denied decorator. A new handler that forgets both
    # will crash at startup, not silently grant MCP callers access.
    _REGISTERED_HANDLERS = {
        "deploy": _handle_deploy,
        "list": _handle_list,
        "config_get": _handle_get_config,
        "config_put": _handle_put_config,
        "profiles_get": _handle_profiles_get,
        "profiles_post": _handle_profiles_post,
        "profiles_put": _handle_profiles_put,
        "profiles_delete": _handle_profiles_delete,
        "iam_policy": _handle_iam_policy,
        "verify": _handle_verify,
        "recall": _handle_recall,
        "destroy": _handle_destroy,
        "teardown": _handle_teardown,
        "pending_list": _handle_pending_list,
        "pending_confirm": _handle_pending_confirm,
        "pending_dismiss": _handle_pending_dismiss,
        "pricing": _handle_pricing,
    }
    for op_name, handler in _REGISTERED_HANDLERS.items():
        if op_name in _INTERNAL_ALLOWED_HANDLERS:
            continue  # allowed for internal-secret callers
        if not getattr(handler, _INTERNAL_DENIED_ATTR, False):
            raise AssertionError(
                f"deploy handler '{op_name}' is neither in _INTERNAL_ALLOWED_HANDLERS "
                f"nor decorated with @_internal_denied — internal-secret callers would "
                f"have unrestricted access. Add the decorator or add the operation to "
                f"the allowlist."
            )

    # ── Deprecated compat aliases (old /api/apps/deploy-web/* surface) ──────────
    # Exact-match dict for static endpoints — Location values are literals,
    # no user data flows into the header (CodeQL URL redirection).
    _COMPAT_STATIC_MAP: dict[str, str] = {
        "deploy": "/api/deploy/deploy",
        "recall": "/api/deploy/recall",
        "destroy": "/api/deploy/destroy",
        "sites": "/api/deploy/list",
        "config": "/api/deploy/config",
        "manifest": "/api/deploy/config",
        "iam-policy": "/api/deploy/iam-policy",
        "verify": "/api/deploy/verify",
    }

    async def _compat_redirect(request: web.Request) -> web.Response:
        tail = request.match_info.get("tail", "")
        # Static endpoint: redirect with literal Location from the dict.
        if tail in _COMPAT_STATIC_MAP:
            raise web.HTTPTemporaryRedirect(location=_COMPAT_STATIC_MAP[tail])
        # teardown/<slug>: user data in the slug — no redirect (no Location header).
        if tail.startswith("teardown/"):
            return web.json_response(
                {"error": "moved", "use": f"/api/deploy/{tail}"},
                status=404,
            )
        # Everything else: plain 404.
        raise web.HTTPNotFound()

    r.add_route("*", "/api/apps/deploy-web/{tail:.*}", _compat_redirect)


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.deploy.handlers_guards",
    "kiro_crew.deploy.handlers_content",
    "kiro_crew.deploy.handlers_sites",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.deploy.handlers_content import (  # noqa: F401
        _ARTIFACT_SLUG_RE,
        _ARTIFACT_SLUG_SPEC,
        _LOCAL_DIR_RE,
        _LOCAL_DIR_SPEC,
        _SCAN_SIZE_LIMIT,
        WebAppRootError,
        _data_dir,
        _is_within,
        _safe_resolve,
        _scan_tree,
        _stage_tree_safe,
        _staging_root,
        resolve_webapp_public_dir,
    )
    from kiro_crew.deploy.handlers_guards import (  # noqa: F401
        _base_remediation,
    )
    from kiro_crew.deploy.handlers_sites import (  # noqa: F401
        _SITE_ID_MAX,
        _safe_site_id,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import (  # noqa: F401
        platform_compat,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        config_dir,
    )
    from kiro_crew.deploy.render import (  # noqa: F401
        render_standalone,
    )
    from kiro_crew.deploy.scan import (  # noqa: F401
        Finding,
        is_credential_finding,
        scan_content,
        summarize,
    )
    from kiro_crew.security import (  # noqa: F401
        is_sensitive_path,
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.sel import (  # noqa: F401
        sel,
    )
    from kiro_crew.validation import (  # noqa: F401
        FieldSpec,
    )
