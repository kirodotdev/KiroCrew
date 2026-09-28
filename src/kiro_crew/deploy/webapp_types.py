"""WebApp artifact metadata types (kind="webapp").

An ``webapp`` artifact represents a *deployed application* rather than renderable
content: it carries the deploy target, front/back architecture, lifecycle/TTL,
a cost estimate, and a teardown handle. The dashboard renders it as an infra
control card instead of an iframe/preview.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from typing import List as _List

# ── Constants ────────────────────────────────────────────────────────────────

_WEBAPP_META_STR_MAX = 2048
_WEBAPP_META_RESOURCES_MAX = 64
_WEBAPP_META_ESTIMATES_MAX = 16


# ── Helper functions ─────────────────────────────────────────────────────────

def _capped_str(v: Any, default: str, max_len: int = _WEBAPP_META_STR_MAX) -> str:
    """Return v as a string capped at max_len, or default if not a string."""
    if not isinstance(v, str):
        return default
    return v[:max_len]


def _as_int(v: Any, default: int) -> int:
    """Coerce v to int or return default."""
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return default
        try:
            return int(v)
        except (OverflowError, ValueError):
            return default
    return default


def _as_float(v: Any, default: float) -> float:
    """Coerce v to float or return default."""
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            return default
        return float(v)
    return default


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class WebAppDeployTarget:
    """Where the app is deployed. ``provider`` is free-form for non-AWS producers."""

    provider: str = "aws"
    account: str = ""
    region: str = ""
    public_url: str = ""
    profile: str = ""
    distribution_id: str = ""  # strong identity for manifest cross-verify


@dataclass
class WebAppArchitecture:
    """Front/back architecture. Rows with empty strings are omitted by the UI."""

    tier: str = "static"  # "static" | "api" | "stateful"
    frontend: str = ""
    backend: str = ""
    state: str = ""
    resources: _List[dict] = field(default_factory=list)


@dataclass
class WebAppLifecycle:
    """Deploy lifecycle. ``expires_at`` is ``None`` for persistent deploys."""

    created_at: str = ""
    expires_at: str | None = None
    persistent: bool = False
    ttl_hours: int = 72
    status: str = "live"  # "live" | "deploying" | "expired" | "error" | "draft"


@dataclass
class WebAppCost:
    """Estimated cost. ``model`` is TTL-window (ephemeral) or monthly (persistent)."""

    model: str = "ttl-window"
    window_hours: int = 72
    estimates: _List[dict] = field(default_factory=list)
    idle_usd: float = 0.0
    note: str = "estimate, not the AWS bill"


@dataclass
class WebAppTeardown:
    """How a human-triggered cancel deletes the deployment."""

    method: str = "reaper-lambda"
    handle: str = ""
    reversible: bool = False


@dataclass
class WebAppMetadata:
    """Structured truth for a ``kind="webapp"`` artifact (a deployed application)."""

    slug: str = ""
    origin_session: str = ""
    # Local copy of the app source tree (the dir that was/would be deployed).
    # Powers the dashboard's local preview channel: the gateway serves the
    # app's static build from this dir so the card can render the app without
    # touching the remote deployment. LLM-influenceable via the artifact API,
    # so it is re-validated against the allow-listed local roots at SERVE
    # time (see dashboard/handlers/webapp_preview.py) — never trusted as-is.
    app_dir: str = ""
    deploy_target: WebAppDeployTarget = field(default_factory=WebAppDeployTarget)
    architecture: WebAppArchitecture = field(default_factory=WebAppArchitecture)
    lifecycle: WebAppLifecycle = field(default_factory=WebAppLifecycle)
    cost: WebAppCost = field(default_factory=WebAppCost)
    teardown: WebAppTeardown = field(default_factory=WebAppTeardown)


def webapp_metadata_from_dict(raw: Any) -> "WebAppMetadata | None":
    """Tolerant-load WebAppMetadata from a meta.json sub-dict.

    Returns None when the field is absent (all non-app artifacts) or not a dict.
    Unknown keys are ignored; missing keys fall back to dataclass defaults.
    """
    if not isinstance(raw, dict):
        return None

    def _as_dict(v: Any) -> dict:
        return v if isinstance(v, dict) else {}

    dt = _as_dict(raw.get("deploy_target"))
    ar = _as_dict(raw.get("architecture"))
    lc = _as_dict(raw.get("lifecycle"))
    co = _as_dict(raw.get("cost"))
    td = _as_dict(raw.get("teardown"))
    exp = lc.get("expires_at")
    res_raw = ar.get("resources")
    res_list = res_raw[:_WEBAPP_META_RESOURCES_MAX] if isinstance(res_raw, list) else []
    est_raw = co.get("estimates")
    est_list = est_raw[:_WEBAPP_META_ESTIMATES_MAX] if isinstance(est_raw, list) else []
    return WebAppMetadata(
        slug=_capped_str(raw.get("slug"), ""),
        origin_session=_capped_str(raw.get("origin_session"), ""),
        app_dir=_capped_str(raw.get("app_dir"), "", 4096),
        deploy_target=WebAppDeployTarget(
            provider=_capped_str(dt.get("provider"), "aws"),
            account=_capped_str(dt.get("account"), ""),
            region=_capped_str(dt.get("region"), ""),
            public_url=_capped_str(dt.get("public_url"), ""),
            profile=_capped_str(dt.get("profile"), "", 128),
            # parse the strong-identity field -- without this the
            # teardown cross-verify would always see "" (dead check).
            distribution_id=_capped_str(dt.get("distribution_id"), "", 128),
        ),
        architecture=WebAppArchitecture(
            tier=_capped_str(ar.get("tier"), "static"),
            frontend=_capped_str(ar.get("frontend"), ""),
            backend=_capped_str(ar.get("backend"), ""),
            state=_capped_str(ar.get("state"), ""),
            resources=[
                {
                    "type": _capped_str(r.get("type"), "", 128),
                    "id": _capped_str(r.get("id"), "", 512),
                }
                for r in res_list
                if isinstance(r, dict)
            ],
        ),
        lifecycle=WebAppLifecycle(
            created_at=_capped_str(lc.get("created_at"), ""),
            expires_at=_capped_str(exp, "") if exp else None,
            persistent=bool(lc.get("persistent", False)),
            ttl_hours=_as_int(lc.get("ttl_hours"), 72),
            status=_capped_str(lc.get("status"), "live"),
        ),
        cost=WebAppCost(
            model=_capped_str(co.get("model"), "ttl-window"),
            window_hours=_as_int(co.get("window_hours"), 72),
            estimates=[
                {"views": _as_int(e.get("views"), 0), "usd": _as_float(e.get("usd"), 0.0)}
                for e in est_list
                if isinstance(e, dict)
            ],
            idle_usd=_as_float(co.get("idle_usd"), 0.0),
            note=_capped_str(co.get("note"), "estimate, not the AWS bill"),
        ),
        teardown=WebAppTeardown(
            method=_capped_str(td.get("method"), "reaper-lambda"),
            handle=_capped_str(td.get("handle"), ""),
            reversible=bool(td.get("reversible", False)),
        ),
    )
