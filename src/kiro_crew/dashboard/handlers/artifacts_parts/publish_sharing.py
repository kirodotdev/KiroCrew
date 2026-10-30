"""Publishing and sharing: publish, sharing updates, unpublish, refresh, reprobe,
pull-latest, upstream status, overwrite-remote and the publish-provider list.

Every route but the provider list reaches a publish provider through
:mod:`kiro_crew.publish_sync`; publish, the sharing update, unpublish, refresh, reprobe
and pull-latest answer a provider failure with ``_sync_error_response``.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactValidationError, get_default_store
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import (
    _SERIALIZE_REDACTED_KEYS,
    _redact_remote_response,
    _redact_text,
    _serialize,
)
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _audit,
    _err,
    _json_response,
    _non_owner_refusal,
    _notify_artifact_update,
    _publish_governance_denied,
    _read_json_body,
    _run_off_loop,
    logger,
)
from kiro_crew.publish_provider import (
    DEFAULT_PROVIDER,
    KindSupport,
    NotPublishedError,
    PublishConflictError,
    PublishDeniedError,
    PublishError,
    PublishUnavailableError,
    list_providers,
)

# Publish-provider name grammar. Upstream imports this from ``validation`` where
# it also backs the MCP publish-tool FieldSpecs; the public fork's validation
# module doesn't carry those tools, so the constraint lives here at the sole
# HTTP boundary that accepts a provider name. ``\Z``-anchored like
# ``_SESSION_KEY_RE`` in route_plumbing: matched raw with ``.match``, so a ``$`` anchor would
# admit a trailing newline.
_ARTIFACT_PROVIDER_RE = re.compile(r"^[a-z0-9-]{1,32}\Z")


# ── Publishing / sharing ─────────────────────────────────────────────────────
_VALID_VISIBILITY = ("PRIVATE", "SHARED", "PUBLIC")


def _validate_sharing_body(body: dict[str, Any]) -> tuple[str, list[str]]:
    """Extract and validate (visibility, shared_with) from a request body.

    Raises ``ArtifactValidationError`` (→ 400) on any problem.
    """
    visibility = body.get("visibility") or "PRIVATE"
    if visibility not in _VALID_VISIBILITY:
        raise ArtifactValidationError("visibility must be PRIVATE, SHARED, or PUBLIC")
    shared_with = body.get("shared_with") or []
    if not isinstance(shared_with, list) or not all(isinstance(a, str) for a in shared_with):
        raise ArtifactValidationError("shared_with must be a list of alias strings")
    if visibility == "SHARED" and not shared_with:
        raise ArtifactValidationError(
            "SHARED visibility requires at least one alias in shared_with"
        )
    return visibility, shared_with


def _sync_error_response(
    tool: str, request: web.Request, slug: str, exc: Exception
) -> web.Response:
    """Map a publishing-provider sync exception to an audited HTTP error response."""
    if isinstance(exc, ArtifactNotFoundError):
        status, outcome = 404, "error"
    elif isinstance(exc, ArtifactValidationError):
        status, outcome = 400, "denied"
    elif isinstance(exc, PublishUnavailableError):
        status, outcome = 503, "error"
    elif isinstance(exc, PublishConflictError):
        status, outcome = 409, "error"
    elif isinstance(exc, NotPublishedError):
        status, outcome = 409, "denied"
    elif isinstance(exc, PublishDeniedError):
        status, outcome = 403, "denied"
    elif isinstance(exc, PublishError):
        status, outcome = 502, "error"
    else:
        status, outcome = 500, "error"
    # The exception text can originate from untrusted publishing-provider MCP responses
    # — redact credentials / exfiltration URLs before it reaches the dashboard
    # AND the SEL audit log (security-controls).
    safe_msg = _redact_text(str(exc))
    _audit(tool=tool, request=request, outcome=outcome, error=safe_msg, extra={"slug": slug})
    return _err(safe_msg, status=status)


async def api_artifact_publish(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/publish — publish (or re-publish) to a
    registered publish destination.

    Body: ``{visibility, shared_with[]}``. Returns the full serialized artifact
    (now carrying the ``publication`` block). A side-panel file that isn't yet
    an artifact is auto-saved first by the frontend (POST /api/artifacts), so
    this endpoint is always slug-based.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_publish")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_publish",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot publish artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
        visibility, shared_with = _validate_sharing_body(body)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # Provider is validated generically (any registered provider); the share
    # picker only offers providers whose kind_support() != UNSUPPORTED.
    requested_provider = body.get("provider") if isinstance(body, dict) else None
    provider_name = requested_provider or DEFAULT_PROVIDER
    if not isinstance(provider_name, str) or not _ARTIFACT_PROVIDER_RE.match(provider_name):
        return _err("provider must match ^[a-z0-9-]{1,32}$")
    # Resolve the EFFECTIVE destination BEFORE the governance gate. For an
    # already-published artifact, publish_sync.publish() ignores provider_name
    # and re-pushes to publication.provider — so the gate must evaluate THAT
    # provider, not the (default) requested one, or a re-publish with no explicit
    # provider would gate on the default provider and permit bytes to a DENIED existing
    # destination. Mirrors api_artifact_update_sharing (which gates on the
    # existing publication's provider).
    try:
        # ≤25 MiB store read — offload off the event loop.
        existing_pub = (await _run_off_loop(lambda: get_default_store().get(slug))).publication
    except ArtifactNotFoundError:
        existing_pub = None
    # Reject an explicit provider switch on an already-published artifact rather
    # than silently ignoring it. publish() reuses the existing
    # publication's provider, so honoring a switch here would leave the original
    # remote orphaned — require an explicit unpublish first.
    if (
        requested_provider
        and existing_pub is not None
        and existing_pub.provider
        and requested_provider != existing_pub.provider
    ):
        return _err(
            f"artifact is already published to {existing_pub.provider!r}; "
            f"unpublish it before publishing to {requested_provider!r}",
            status=409,
        )
    # Effective provider: the existing publication's (re-publish dispatches to it)
    # else the requested/default. This is the destination bytes actually go to.
    effective_provider = (
        existing_pub.provider if existing_pub and existing_pub.provider else provider_name
    )
    # Governance chokepoint (Plane-C): the capabilities.publish ceiling + the
    # operator destination allowlist gate publishing here — the host PreToolUse
    # gate never sees this HTTP action. Runs BEFORE any provider dispatch.
    gov_denial = await _run_off_loop(
        lambda: _publish_governance_denied(request, effective_provider)
    )
    if gov_denial is not None:
        _audit(
            tool="artifact_publish",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": effective_provider},
        )
        return _err(gov_denial, status=403)
    is_mcp = request.headers.get("X-Internal-Secret") is not None
    actor = "agent" if is_mcp else "user"
    try:
        await publish_sync.publish(
            slug,
            visibility=visibility,
            shared_with=shared_with,
            actor=actor,
            provider_name=provider_name,
        )
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_publish", request, slug, exc)
    _audit(
        tool="artifact_publish",
        request=request,
        outcome="success",
        extra={"slug": slug, "visibility": visibility, "provider": effective_provider},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_update_sharing(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/sharing — change visibility / shared-with.

    Body: ``{visibility, shared_with[]}``. No re-upload. Returns the serialized
    artifact with the updated publication block.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_update_sharing")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_update_sharing",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot change artifact sharing", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
        visibility, shared_with = _validate_sharing_body(body)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    # Changing sharing (e.g. PRIVATE -> PUBLIC) is an outbound-publish mutation,
    # so it MUST pass the same capabilities.publish governance gate as the
    # initial publish — otherwise an already-published artifact could be widened
    # to public after policy revocation. Gate on the existing publication's
    # provider (default provider when the block hasn't loaded).
    try:
        existing_pub = (await _run_off_loop(lambda: get_default_store().get(slug))).publication
    except ArtifactNotFoundError:
        existing_pub = None
    share_provider = (
        existing_pub.provider if existing_pub and existing_pub.provider else DEFAULT_PROVIDER
    )
    gov_denial = await _run_off_loop(lambda: _publish_governance_denied(request, share_provider))
    if gov_denial is not None:
        _audit(
            tool="artifact_update_sharing",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": share_provider},
        )
        return _err(gov_denial, status=403)
    try:
        await publish_sync.update_sharing(slug, visibility=visibility, shared_with=shared_with)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_update_sharing", request, slug, exc)
    _audit(
        tool="artifact_update_sharing",
        request=request,
        outcome="success",
        extra={"slug": slug, "visibility": visibility},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_unpublish(request: web.Request) -> web.Response:
    """DELETE /api/artifacts/{slug}/publish — remove from the publishing provider.

    Deletes the published artifact (best-effort) and clears the local
    publication block. Returns the serialized artifact (now with
    ``publication: null``).
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_unpublish")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_unpublish",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot unpublish artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        await publish_sync.unpublish(slug)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except Exception as exc:
        return _sync_error_response("artifact_unpublish", request, slug, exc)
    _audit(
        tool="artifact_unpublish",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_refresh_sharing(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/publish/refresh — reconcile local sharing
    state with the live destination.

    Pulls the destination's current visibility / shared-with (e.g. after the
    user changed them directly in the provider's UI) and updates the stored
    publication so the dashboard reflects truth. Gated like other mutations
    since it can update meta.json.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_refresh_sharing")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot refresh artifact sharing", status=403)
    slug = request.match_info.get("slug", "")
    try:
        await publish_sync.refresh_publication(slug)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_refresh_sharing",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover — refresh is best-effort
        return _sync_error_response("artifact_refresh_sharing", request, slug, exc)
    _audit(
        tool="artifact_refresh_sharing",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_reprobe_notice(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/publish/reprobe-notice — re-check the
    destination's serving state and clear a stale publish notice.

    A publish can record a "still rolling out" ``notice`` that later resolves on
    its own, but nothing on the happy path revisits it, so the amber banner
    would persist forever after the link works. This re-probes the provider and
    reconciles ``notice`` / ``notice_code`` to the current truth (clears them
    only when the condition has actually cleared). Gated like other mutations
    since it can update meta.json.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_reprobe_notice")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_reprobe_notice",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot reprobe artifact notice", status=403)
    slug = request.match_info.get("slug", "")
    try:
        await publish_sync.reprobe_notice(slug)
        art = await _run_off_loop(lambda: get_default_store().get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_reprobe_notice",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_reprobe_notice",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover — reprobe is best-effort
        return _sync_error_response("artifact_reprobe_notice", request, slug, exc)
    _audit(
        tool="artifact_reprobe_notice",
        request=request,
        outcome="success",
        extra={"slug": slug},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_pull_latest(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/pull-latest — pull upstream into a fork."""
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_pull_latest")
    if owner_denied is not None:
        return owner_denied

    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_pull_latest",
            request=request,
            outcome="denied",
            error="restricted session",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot pull latest", status=403)

    slug = request.match_info.get("slug", "")
    store = get_default_store()
    try:
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)

    if art.publication is None and art.fork_metadata is None:
        return _err("artifact does not track an upstream", status=400)

    # Delegate to the unified pull engine — works for a fork's origin lineage
    # AND my own publication (a collaborator edited my cloud copy). ``source``
    # (publication|origin|auto) selects which tracked upstream to pull. The
    # engine pulls into a NEW local snapshot, never auto-republishes, and
    # surfaces a conflict (never clobbers) for an owned copy with unsynced
    # local edits. Read-only ingress — no publish governance gate (no bytes
    # leave the box).
    source = request.rel_url.query.get("source", "auto")
    try:
        # ``pull_upstream`` is best-effort for provider/network failures (it
        # returns a result dict, never raises for those), so the realistic
        # raises here are store/registry errors: a concurrent delete during the
        # remote-fetch window (ArtifactNotFoundError → 404) or an unregistered
        # provider (PublishUnavailableError → 503). Map those like the other
        # sync handlers instead of collapsing every error into a 502.
        result = await publish_sync.pull_upstream(slug, source=source)
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        return _sync_error_response("artifact_pull_latest", request, slug, exc)
    except PublishUnavailableError as exc:
        return _sync_error_response("artifact_pull_latest", request, slug, exc)
    except Exception as exc:  # pragma: no cover — pull is best-effort
        _audit(
            tool="artifact_pull_latest",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(_redact_text(str(exc)), status=502)

    _audit(
        tool="artifact_pull_latest",
        request=request,
        outcome="success",
        extra={"slug": slug, "pulled": bool(result.get("pulled"))},
    )
    # A pull lands upstream content as a new local snapshot.
    if result.get("pulled"):
        _notify_artifact_update(state, slug, art.version)
    # ``_serialize`` already ran the redactors over the (≤25 MiB) content body
    # and the other LLM-originated fields, so don't rescan ``content`` — that
    # double pass is a redundant multi-second regex scan on the event loop.
    payload = _redact_remote_response(
        _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
    )
    payload["pull_result"] = _redact_remote_response(result)
    return _json_response(payload)


async def api_artifact_upstream_status(request: web.Request) -> web.Response:
    """GET /api/artifacts/{slug}/upstream-status — cheap (metadata-only) check
    of whether the tracked upstream has changes to pull. Read-only; drives the
    detail page's non-blocking pull-available / conflict banner. Best-effort —
    a provider failure reports ``tracked`` with ahead/conflict defaulted False
    so opening an artifact never blocks on the network."""
    slug = request.match_info.get("slug", "")
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_upstream_status",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": slug},
        )
        return _err("restricted session cannot query upstream status", status=403)
    try:
        status = await publish_sync.upstream_status(slug)
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_upstream_status",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_upstream_status",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    _audit(
        tool="artifact_upstream_status",
        request=request,
        outcome="success",
        extra={"slug": slug, "upstream_ahead": bool(status.get("upstream_ahead"))},
    )
    return _json_response(status)


async def api_artifact_overwrite_remote(request: web.Request) -> web.Response:
    """POST /api/artifacts/{slug}/overwrite-remote — force the local content to
    become the remote's current version even when the remote moved ahead,
    WITHOUT pulling the remote's (possibly untrusted) bytes into the local
    store. The superseded remote version stays in the provider's history (no
    delete-version primitive). See ``publish_sync.overwrite_upstream``.

    Egress chokepoint: bytes leave the box to the tracked destination, and
    ``publish_sync.overwrite_upstream`` has NO internal gate (``push_version``
    is ungated), so the ``capabilities.publish`` governance ceiling is enforced
    HERE — on the resolved ``publication.provider`` — before any provider
    dispatch (same fail-closed gate as publish / update-sharing).
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_overwrite_remote")
    if owner_denied is not None:
        return owner_denied
    slug = request.match_info.get("slug", "")
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": slug},
        )
        return _err("restricted session cannot overwrite the remote", status=403)
    store = get_default_store()
    try:
        existing = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    # Resolve the destination the bytes actually go to: the existing
    # publication's provider. An unpublished artifact can't be overwritten —
    # publish_sync reports that cleanly, but it must not bypass the gate, so
    # gate on the default provider name in that case.
    overwrite_provider = (
        existing.publication.provider
        if existing.publication is not None and existing.publication.provider
        else DEFAULT_PROVIDER
    )
    gov_denial = await _run_off_loop(
        lambda: _publish_governance_denied(request, overwrite_provider)
    )
    if gov_denial is not None:
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="denied",
            error=gov_denial,
            extra={"slug": slug, "provider": overwrite_provider},
        )
        return _err(gov_denial, status=403)
    try:
        result = await publish_sync.overwrite_upstream(slug)
        art = await _run_off_loop(lambda: store.get(slug))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except PublishDeniedError as exc:
        return _sync_error_response("artifact_overwrite_remote", request, slug, exc)
    except Exception as exc:  # pragma: no cover — overwrite is best-effort
        _audit(
            tool="artifact_overwrite_remote",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(_redact_text(str(exc)), status=502)
    _audit(
        tool="artifact_overwrite_remote",
        request=request,
        outcome="success",
        extra={"slug": slug, "overwritten": bool(result.get("overwritten"))},
    )
    payload = _redact_remote_response(
        _serialize(art, include_content=True), already_redacted=_SERIALIZE_REDACTED_KEYS
    )
    payload["overwrite_result"] = _redact_remote_response(result)
    return _json_response(payload)


# ── Provider negotiation ─────────────────────────────────────────
def _sharing_model_dict(sm: Any) -> dict[str, Any]:
    return {
        "supports_private": sm.supports_private,
        "supports_shared": sm.supports_shared,
        "supports_public": sm.supports_public,
        "principal_kind": sm.principal_kind,
        "supports_roles": sm.supports_roles,
        "supports_expiration": sm.supports_expiration,
        "programmable": sm.programmable,
        "out_of_band_url": sm.out_of_band_url,
    }


async def api_artifact_publish_providers(request: web.Request) -> web.Response:
    """GET /api/artifacts/publish-providers?kind=<kind> — available publishing
    providers with per-kind support + sharing/sync/discovery descriptors.

    Drives the share-panel picker: the FE shows a provider selector only when
    >1 *available* provider can host the artifact's kind (``kind_support !=
    unsupported``), and renders the right sharing controls per provider. Read-
    only; no mutation, so no restricted-session gate (matches the list endpoint).
    """
    kind = request.query.get("kind") or "widget"
    out: list[dict[str, Any]] = []
    for p in list_providers():
        try:
            avail = p.available()
            # A not-yet-installed provider still shows when it can self-install
            # on first publish (ensure_ready) — hiding it entirely would make
            # the destination undiscoverable until the user installs by hand.
            if not avail and not p.installable():
                continue
            ks = p.kind_support(kind)
            sm = p.sharing_model()
            sy = p.sync_model()
            dm = p.discovery_model()
        except Exception as exc:  # pragma: no cover — a flaky provider must not break the picker
            logger.warning("publish-providers: skipping %r: %s", getattr(p, "name", "?"), exc)
            continue
        out.append(
            {
                "name": p.name,
                "display_name": p.display_name,
                "capabilities": sorted(c.value for c in p.capabilities()),
                "kind_support": ks.value,
                "capable": ks != KindSupport.UNSUPPORTED,
                # False + present in this list ⇒ installs on first publish; the
                # FE may surface an "installs on first use" hint.
                "available": avail,
                # The remedy text for `available: false`. Without it the picker can only
                # send the user somewhere generic, and a provider's own hint is the only
                # thing that knows WHICH action makes it available.
                "install_hint": str(getattr(p, "install_hint", "") or ""),
                # Whether the published link is served with no authentication. The
                # FE gates the public-exposure warning and the acknowledgment modal
                # on it; a destination that stores content privately declares False
                # so the flow stops telling the user their content is on the open
                # internet. Coerced to a real bool so a stub attribute cannot leak a
                # non-JSON value into the response.
                "public_reachable": bool(p.public_reachable),
                "sharing_model": _sharing_model_dict(sm),
                "sync_model": {
                    "authority": sy.authority,
                    "concurrency": sy.concurrency,
                    "collab_mode": sy.collab_mode,
                },
                "discovery_model": {
                    "list_mine": dm.list_mine,
                    "list_shared_with_me": dm.list_shared_with_me,
                    "list_public": dm.list_public,
                    "full_text_search": dm.full_text_search,
                    "pull_by_id": dm.pull_by_id,
                },
            }
        )
    return _json_response({"providers": out, "kind": kind})
