"""deploy-web backend — config + deploy/recall/destroy/list endpoints.

Mechanism: deterministic Python builtin module (design §8). HTTP endpoints (called
by the Web Deploy UI page) drive the ``engine`` which shells to the ``aws`` CLI with
``--profile`` (never boto3). The deploy mechanics are NOT LLM tools.

Approval model (§9.3): publishing creates a **public** URL and destroy/recall mutate
public infra, so each is a two-call **confirm-gate** — the first call returns a preview
that echoes exactly what will happen (resources, scan findings, public nature); the
client must re-call with ``confirm`` to proceed. Pre-publish scan (§4.1/Q4) blocks
publishing on any secret/internal-data finding unless the client passes
``override_scan`` (explicit "publish anyway"). These are plain HTTP endpoints, not
registered tools, so they can never appear in heartbeat/cron tool safe-sets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.deploy_web import engine
from kiro_crew.apps.builtins.deploy_web import iam as iam_mod
from kiro_crew.apps.builtins.deploy_web.render import render_standalone
from kiro_crew.apps.builtins.deploy_web.scan import scan_content, summarize
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel
from kiro_crew.validation import FieldSpec, ValidationError, validate_field

try:
    from kiro_crew.artifacts import ArtifactNotFoundError, get_default_store
    _HAS_ARTIFACTS = True
except ImportError:  # pragma: no cover - defensive
    _HAS_ARTIFACTS = False

logger = logging.getLogger(__name__)

APP_DIR = Path.home() / ".kirocrew" / "apps" / "deploy-web"
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_REGION = engine.DEFAULT_REGION
_SITE_ID_MAX = 64


def _audit(action: str, site_id: str, outcome: str, *, error: str = "") -> None:
    """Emit a SEL audit event for a deploy-web permission decision.

    deploy/recall/destroy create public internet infrastructure, delete
    resources, and make content world-readable — each confirmed action is a
    significant permission decision and MUST be recorded (§9.3).
    """
    sel().log_api_access(
        caller="app:deploy-web",
        operation=f"deploy_web.{action}",
        outcome=outcome,
        source="builtin-app",
        resources=site_id,
        error=error[:200] if error else "",
    )


# --- local_dir input validation (AutoSDE f-* security-controls) ------------
# local_dir is request-supplied and LLM-influenceable via the chat-native skill,
# and flows to the filesystem + the `aws s3 sync` subprocess. Validate it with a
# validation.py schema (type/length/charset) and confine the resolved real path
# to an allow-listed root before any filesystem/subprocess use.
_LOCAL_DIR_RE = re.compile(r"^[A-Za-z0-9 _\-./~]+$")
_LOCAL_DIR_SPEC = FieldSpec(name="local_dir", type=str, max_len=4096, pattern=_LOCAL_DIR_RE)

# profile/region are LLM-influenceable (chat-native skill) and flow into subprocess
# argv (--profile/--region) on every aws call, so they get schema validation too.
# Both allow empty (clears profile / falls back to default region); the pattern is
# only enforced on non-empty values by validate_field.
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROFILE_SPEC = FieldSpec(name="profile", type=str, max_len=128, pattern=_PROFILE_RE)
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d+$")
_REGION_SPEC = FieldSpec(name="region", type=str, max_len=32, pattern=_REGION_RE)

# artifact_slug is LLM-influenceable (chat-native skill) and is used in a store
# lookup, so validate it like the other request inputs.
_ARTIFACT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ARTIFACT_SLUG_SPEC = FieldSpec(name="artifact_slug", type=str, max_len=128, pattern=_ARTIFACT_SLUG_RE)

# Pre-publish scan: skip files larger than this (likely binary/media) when
# scanning a local_dir's full contents.
_SCAN_SIZE_LIMIT = 2 * 1024 * 1024  # 2 MiB


def _safe_resolve(p: Path) -> Path:
    """Resolve a path (following symlinks); fall back to the raw path on error."""
    try:
        return p.resolve()
    except OSError:
        return p


def _allowed_local_roots() -> list[Path]:
    """Resolved directories a publishable local_dir may live under."""
    roots: list[Path] = []
    for cand in (Path.home(), Path("/local/home"), Path("/home"),
                 Path("/tmp"), Path("/workplace"), Path("/workspace")):
        try:
            if cand.exists():
                roots.append(cand.resolve())
        except OSError:
            pass
    return roots


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# --- config (profile NAME only — §6.1) ------------------------------------

def _load_config() -> dict[str, Any]:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"profile": "", "region": DEFAULT_REGION}
    return {"profile": str(cfg.get("profile", "")),
            "region": str(cfg.get("region", "") or DEFAULT_REGION)}


def _save_config(profile: str, region: str) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {"profile": str(profile or ""), "region": str(region or DEFAULT_REGION)}
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return cfg


def _safe_site_id(raw: str) -> str:
    """Normalize a site id to a tag/label-safe slug."""
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (raw or "").strip().lower())
    return s.strip("-_")[:_SITE_ID_MAX]


# --- testable core (no aiohttp Request) ------------------------------------

async def _do_deploy(params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Resolve artifact/dir → render → scan-gate → confirm-gate → engine.deploy."""
    cfg = _load_config()
    profile, region = cfg["profile"], cfg["region"]
    if not profile:
        return 400, {"error": "deploy-web is not configured — set an AWS profile first (Setup)."}

    site_id = _safe_site_id(str(params.get("site_id", "")))
    if not site_id:
        return 400, {"error": "site_id is required"}

    artifact_slug = str(params.get("artifact_slug", "")).strip()
    local_dir = str(params.get("local_dir", "")).strip()
    if not artifact_slug and not local_dir:
        return 400, {"error": "provide artifact_slug or local_dir"}
    if artifact_slug:
        # Validate the LLM-influenceable slug before the artifact-store lookup.
        try:
            artifact_slug = validate_field(artifact_slug, _ARTIFACT_SLUG_SPEC)
        except ValidationError as e:
            return 400, {"error": f"invalid artifact_slug: {e}"}

    tmp_dir: str | None = None
    try:
        if artifact_slug:
            if not _HAS_ARTIFACTS:
                return 500, {"error": "artifact store unavailable"}
            try:
                art = get_default_store().get(artifact_slug)
            except ArtifactNotFoundError:
                return 404, {"error": f"artifact '{artifact_slug}' not found"}
            html = render_standalone(art.kind, art.content or "", title=art.name or "")
            findings = scan_content(html)
            tmp_dir = tempfile.mkdtemp(prefix="deploy-web-")
            Path(tmp_dir, "index.html").write_text(html, encoding="utf-8")
            src_dir = tmp_dir
            byte_size = len(html.encode("utf-8"))
        else:
            # Validate the LLM-influenceable path via a validation.py schema
            # (type/length/charset) BEFORE any filesystem/subprocess use.
            try:
                local_dir = validate_field(local_dir, _LOCAL_DIR_SPEC)
            except ValidationError as e:
                return 400, {"error": f"invalid local_dir: {e}"}
            src = Path(local_dir).expanduser()
            try:
                resolved = src.resolve()  # follow symlinks once, up front
            except OSError:
                return 400, {"error": f"local_dir not found: {local_dir}"}
            if not resolved.is_dir():
                return 400, {"error": f"local_dir not found: {local_dir}"}
            # Confine the resolved real path to an allow-listed root — defends
            # against a symlinked / traversal path escaping to arbitrary
            # filesystem locations and being uploaded to a PUBLIC bucket.
            if not any(_is_within(resolved, r) for r in _allowed_local_roots()):
                _audit("deploy", site_id, "denied", error="local_dir outside allowed roots")
                return 400, {"error": ("local_dir must resolve within your home or a "
                                       "standard workspace directory")}
            # Security (§9.3): refuse if the dir IS — or recursively contains — a
            # sensitive credential path (~/.aws, ~/.ssh, ...). Check the raw and
            # the symlink-resolved dir AND each child's resolved target, since
            # `aws s3 sync` follows symlinks — a symlink inside the dir pointing
            # at a credential file would otherwise be uploaded.
            if (is_sensitive_path(str(src)) or is_sensitive_path(str(resolved))
                    or any(is_sensitive_path(str(_safe_resolve(p)))
                           for p in resolved.rglob("*"))):
                _audit("deploy", site_id, "denied",
                       error="local_dir is or contains a sensitive credential path")
                return 400, {"error": "local_dir is or contains a sensitive credential path"}
            src = resolved
            # Scan EVERY file (not just index.html) — `aws s3 sync` uploads the
            # whole tree, so the pre-publish gate (§4.1/Q4) must cover it all.
            # Skip large files (likely binary/media) for performance.
            findings = []
            for f in src.rglob("*"):
                if f.is_file() and f.stat().st_size < _SCAN_SIZE_LIMIT:
                    try:
                        findings.extend(scan_content(f.read_text(encoding="utf-8", errors="ignore")))
                    except OSError:
                        pass
            src_dir = str(src)
            byte_size = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())

        # Pre-publish scan gate (block-and-warn) — §4.1/Q4.
        if findings and not params.get("override_scan"):
            _audit("deploy", site_id, "scan-blocked",
                   error=f"{len(findings)} finding(s)")
            return 409, {"blocked": True, "reason": "scan", "findings": summarize(findings),
                         "count": len(findings)}

        # Confirm-gate — publishing makes content world-readable (§9.3).
        if not params.get("confirm"):
            return 200, {"requires_confirm": True, "public": True, "site_id": site_id,
                         "bytes": byte_size,
                         "scan": summarize(findings) if findings else "clean",
                         "message": "This will publish to a PUBLIC URL on your own AWS. Confirm to proceed."}

        result = await asyncio.to_thread(engine.deploy, site_id, src_dir, profile, region)
        _audit("deploy", site_id, "ok")
        return 200, result
    except engine.AWSError as e:
        _audit("deploy", site_id, "failure", error=str(e))
        return 502, {"error": str(e), "missing_statement": e.missing_statement}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _do_recall(params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    cfg = _load_config()
    if not cfg["profile"]:
        return 400, {"error": "not configured"}
    site_id = _safe_site_id(str(params.get("site_id", "")))
    if not site_id:
        return 400, {"error": "site_id is required"}
    try:
        if not params.get("confirm"):
            site = await asyncio.to_thread(engine.find_site_by_tag, site_id, cfg["profile"], cfg["region"])
            if not site:
                return 404, {"error": f"no site '{site_id}'"}
            return 200, {"requires_confirm": True, "action": "recall", "site_id": site_id,
                         "resources": site,
                         "message": ("Recall empties the site (URL → 404) but keeps the infra "
                                     "(reversible). Note: edge caches may serve briefly, and "
                                     "already-downloaded content cannot be recalled.")}
        result = await asyncio.to_thread(engine.recall, site_id, cfg["profile"], cfg["region"])
        _audit("recall", site_id, "ok")
        return 200, result
    except engine.AWSError as e:
        _audit("recall", site_id, "failure", error=str(e))
        return 502, {"error": str(e), "missing_statement": e.missing_statement}


async def _do_destroy(params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    cfg = _load_config()
    if not cfg["profile"]:
        return 400, {"error": "not configured"}
    site_id = _safe_site_id(str(params.get("site_id", "")))
    if not site_id:
        return 400, {"error": "site_id is required"}
    try:
        if not params.get("confirm"):
            site = await asyncio.to_thread(engine.find_site_by_tag, site_id, cfg["profile"], cfg["region"])
            if not site:
                return 404, {"error": f"no site '{site_id}'"}
            return 200, {"requires_confirm": True, "action": "destroy", "site_id": site_id,
                         "resources": site, "destructive": True,
                         "message": (f"DESTROY will permanently delete bucket "
                                     f"'{site.get('bucket', '')}' and distribution "
                                     f"'{site.get('distribution_id', '')}'. This cannot be undone.")}
        result = await asyncio.to_thread(engine.destroy, site_id, cfg["profile"], cfg["region"])
        _audit("destroy", site_id, "ok")
        return 200, result
    except engine.AWSError as e:
        _audit("destroy", site_id, "failure", error=str(e))
        return 502, {"error": str(e), "missing_statement": e.missing_statement}


async def _do_list() -> tuple[int, dict[str, Any]]:
    cfg = _load_config()
    if not cfg["profile"]:
        return 200, {"sites": [], "configured": False}
    try:
        sites = await asyncio.to_thread(engine.list_sites, cfg["profile"], cfg["region"])
        return 200, {"sites": sites, "configured": True}
    except engine.AWSError as e:
        return 502, {"error": str(e), "missing_statement": e.missing_statement}


# --- aiohttp adapters ------------------------------------------------------

async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


async def _handle_get_config(_request: web.Request) -> web.Response:
    return web.json_response(_load_config())


async def _handle_put_config(request: web.Request) -> web.Response:
    body = await _json_body(request)
    # Validate the LLM-influenceable profile/region before persisting — both flow
    # into subprocess argv (--profile/--region) on every later aws call.
    try:
        profile = validate_field(str(body.get("profile", "")), _PROFILE_SPEC)
        region = validate_field(str(body.get("region", "")), _REGION_SPEC)
    except ValidationError as e:
        return web.json_response({"error": f"invalid config: {e}"}, status=400)
    return web.json_response(_save_config(profile, region))


async def _handle_deploy(request: web.Request) -> web.Response:
    status, payload = await _do_deploy(await _json_body(request))
    return web.json_response(payload, status=status)


async def _handle_recall(request: web.Request) -> web.Response:
    status, payload = await _do_recall(await _json_body(request))
    return web.json_response(payload, status=status)


async def _handle_destroy(request: web.Request) -> web.Response:
    status, payload = await _do_destroy(await _json_body(request))
    return web.json_response(payload, status=status)


async def _handle_list(_request: web.Request) -> web.Response:
    status, payload = await _do_list()
    return web.json_response(payload, status=status)


async def _handle_iam_policy(request: web.Request) -> web.Response:
    """Generate the least-privilege IAM policy text for the user to apply (Option A)."""
    custom = request.query.get("custom_domain", "").lower() in ("1", "true", "yes")
    return web.json_response({"policy": iam_mod.policy_json(include_custom_domain=custom)})


async def _handle_verify(_request: web.Request) -> web.Response:
    """Read-only reachability check (NOT full verification, §9.3/Q3)."""
    cfg = _load_config()
    if not cfg["profile"]:
        return web.json_response({"reachable": False, "note": "No AWS profile configured yet."},
                                 status=400)
    result = await asyncio.to_thread(iam_mod.reachability_check, cfg["profile"])
    return web.json_response(result)


def register_routes(app: web.Application) -> None:
    """Mount deploy-web routes under /api/apps/deploy-web (in-process builtin)."""
    r = app.router
    r.add_get("/api/apps/deploy-web/config", _handle_get_config)
    r.add_put("/api/apps/deploy-web/config", _handle_put_config)
    r.add_get("/api/apps/deploy-web/iam-policy", _handle_iam_policy)
    r.add_post("/api/apps/deploy-web/verify", _handle_verify)
    r.add_post("/api/apps/deploy-web/deploy", _handle_deploy)
    r.add_post("/api/apps/deploy-web/recall", _handle_recall)
    r.add_post("/api/apps/deploy-web/destroy", _handle_destroy)
    r.add_get("/api/apps/deploy-web/sites", _handle_list)
