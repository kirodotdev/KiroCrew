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
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir, default_workspace_dir
from kiro_crew.deploy import engine
from kiro_crew.deploy import iam as iam_mod
from kiro_crew.deploy import pricing as pricing_mod
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.deploy.render import render_standalone
from kiro_crew.deploy.scan import Finding, is_credential_finding, scan_content, summarize
from kiro_crew.publish_governance import DEPLOY_WEB_PROVIDER_ID, publish_denied_reason
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import FieldSpec, ValidationError, validate_field

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


# --- restricted-session guard (consistent across all mutating endpoints) -----

def _deny_restricted(request: "web.Request", operation: str) -> "web.Response | None":
    """Return a 403 Response if the request comes from a restricted session.

    Applies to ALL mutating deploy endpoints: config PUT, deploy, recall, destroy,
    teardown, profiles POST/PUT/DELETE, and verify (back-fills registry = write).
    Read-only handlers (GET config, list, iam-policy, profiles GET) are exempt.
    Returns None when access is allowed.

    Fail-closed: if the app or state cannot be resolved, access is DENIED.
    Test fixtures must install a proper state on the request app (see
    test_deploy_web_handlers.py _NonRestrictedReq for the canonical pattern).
    """
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session

    app = getattr(request, "app", None)
    if app is None:
        _audit(operation, "", "denied", error="no app context")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403)
    state = app.get("state")
    if state is None:
        _audit(operation, "", "denied", error="missing dashboard state")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403)
    if _is_restricted_session(state, request):
        _audit(operation, "", "denied", error="restricted session")
        return web.json_response(
            {"error": "restricted session cannot perform this operation"}, status=403)
    return None


logger = logging.getLogger(__name__)


def _redact_text(s: str) -> str:
    """Redact credentials and internal URLs from any text returned in response bodies.

    Applied to scan summary strings, error messages, and any findings-carrying
    payload before it reaches LLM/dashboard callers. Defense-in-depth: even if
    _mask_credential is applied at Finding creation time, this catches any path
    where raw matched content leaks through.
    """
    s, _ = redact_credentials(s)
    s, _ = redact_exfiltration_urls(s)
    return s


def _sanitize_response(payload: Any) -> Any:
    """Recursively apply credential + exfiltration redaction to all str values in a response payload.

    Deploy handler error responses echo LLM-controlled values (local_dir,
    site_id, profile), which need BOTH redaction passes. This helper walks
    dict/list structures and applies _redact_text to every str leaf. Applied at
    the three chokepoint handlers (_handle_deploy, _handle_recall,
    _handle_destroy) so ALL paths through _do_* are covered in one place.
    """
    if isinstance(payload, str):
        return _redact_text(payload)
    if isinstance(payload, dict):
        return {k: _sanitize_response(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_sanitize_response(v) for v in payload]
    return payload


def _redact_profile_fields(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact credential-containing strings from profile entries before response.

    Applied at serialization in every handler that returns registry entries.
    SEL audit keeps the raw data; dashboard/API responses are sanitized.
    """
    out: list[dict[str, Any]] = []
    for p in profiles:
        entry: dict[str, Any] = {}
        for k, v in p.items():
            if isinstance(v, str) and v:
                v = _redact_text(v)
            entry[k] = v
        out.append(entry)
    return out


def _redact_pending_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively redact every string value in pending entries before response.

    Defense-in-depth: pending entries contain LLM-controlled fields
    (site_id, local_dir, scan_summary, profile) that could carry injected
    credential strings. Same pipeline as _redact_profile_fields.
    """
    out: list[dict[str, Any]] = []
    for e in entries:
        redacted: dict[str, Any] = {}
        for k, v in e.items():
            if isinstance(v, str) and v:
                v = _redact_text(v)
            elif isinstance(v, dict):
                # Recurse one level for nested dicts
                v = {
                    dk: _redact_text(dv) if isinstance(dv, str) and dv else dv
                    for dk, dv in v.items()
                }
            redacted[k] = v
        out.append(redacted)
    return out


# Data dir resolved via config_dir() so pods/tests isolate correctly.


def _data_dir() -> Path:
    return config_dir() / "deploy"


DEFAULT_REGION = engine.DEFAULT_REGION
_SITE_ID_MAX = 64


def _reaper_remediation(profile: str, region: str) -> str:
    """Exact operator command for the finite-TTL reaper precondition.

    Rendered with the request's real profile/region so the 409 payload is
    directly actionable. Installing the reaper is an operator step by design
    (the stack creates an IAM role, which KiroCrew never does itself).
    """
    parts = ["install-reaper.sh"]
    if profile:
        parts += ["--profile", profile]
    if region:
        parts += ["--region", region]
    return " ".join(parts)


def _audit(action: str, site_id: str, outcome: str, *, error: str = "") -> None:
    """Emit a SEL audit event for a deploy-web permission decision.

    deploy/recall/destroy create public internet infrastructure, delete
    resources, and make content world-readable — each confirmed action is a
    significant permission decision and MUST be recorded (§9.3).
    """
    sel().log_api_access(
        caller="core:deploy",
        operation=f"deploy.{action}",
        outcome=outcome,
        source="builtin-app",
        resources=site_id,
        error=error[:200] if error else "",
    )


def _safe_err(exc: BaseException) -> str:
    """Return a safe error message with credentials/URLs redacted.

    AWS CLI stderr can contain credential fragments (access key ids, session
    tokens, etc.) — never surface the raw exception in response payloads.
    """
    return _redact_text(str(exc))


# --- local_dir input validation (security-controls) ------------
# local_dir is request-supplied and LLM-influenceable via the chat-native skill,
# and flows to the filesystem + the `aws s3 sync` subprocess. Validate it with a
# validation.py schema (type/length/charset) and confine the resolved real path
# to an allow-listed root before any filesystem/subprocess use.
_LOCAL_DIR_RE = re.compile(r"^[A-Za-z0-9 _\-./~]+$")
_LOCAL_DIR_SPEC = FieldSpec(name="local_dir", type=str, max_len=4096, pattern=_LOCAL_DIR_RE)

# profile/region are LLM-influenceable (chat-native skill) and flow into subprocess
# argv (--profile/--region) on every aws call, so they get schema validation too.
# Both allow empty (clears profile / falls back to default region); the pattern is
# only enforced on non-empty values by validate_field. The profile charset
# ('+' admitted for IAM Identity Center derived names, leading '-' excluded,
# \Z anchor — #6055) is profiles.py's PROFILE_SPEC, aliased like REGION_SPEC
# below rather than re-spelled here.
_PROFILE_SPEC = profiles_mod.PROFILE_SPEC
_REGION_SPEC = profiles_mod.REGION_SPEC

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
    """Resolved directories a publishable local_dir may live under.

    Aligned with the subagent_cwd_allowed_roots config and the file-explorer
    authorization boundary — only the caller's known workspace roots are
    permitted, not arbitrary filesystem locations.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    roots: list[Path] = []
    try:
        cfg = KiroCrewConfig.load()
        configured = cfg.agent.subagent_cwd_allowed_roots
    except Exception:
        configured = []
    if configured:
        for r in configured:
            expanded = Path(os.path.expanduser(r))
            try:
                if expanded.exists():
                    roots.append(expanded.resolve())
            except OSError:
                pass
    # Fallback: if no config, allow ~/workplace and ~/workspace (standard devbox)
    if not roots:
        for cand in (Path.home() / "workplace", Path.home() / "workspace",
                     Path("/local/home") / os.environ.get("USER", "") / "workplace"):
            try:
                if cand.exists():
                    roots.append(cand.resolve())
            except OSError:
                pass
    # Always allow the agent's own config-dir workspace (e.g. ~/.kiro/crew/workspace).
    try:
        cdir_ws = default_workspace_dir()
        if cdir_ws.exists() and cdir_ws.resolve() not in roots:
            roots.append(cdir_ws.resolve())
    except OSError:
        pass
    # Also include any registered workspaces (cfg.workspaces[*].dir).
    try:
        cfg = KiroCrewConfig.load()
        for _ws_name, ws_cfg in cfg.workspaces.items():
            ws_dir = Path(os.path.expanduser(ws_cfg.dir))
            try:
                if ws_dir.exists():
                    resolved = ws_dir.resolve()
                    if resolved not in roots:
                        roots.append(resolved)
            except OSError:
                pass
    except Exception:
        pass
    # Always allow the deploy staging dir, where artifacts are staged for publish.
    try:
        sr = _staging_root()
        roots.append(sr.resolve())
    except OSError:
        pass
    return roots


def _staging_root() -> Path:
    """Return (and validate) the private staging root under config_dir.

    Raises RuntimeError if the path is a symlink or not owned by the current user.
    """
    import stat

    sr = config_dir() / "deploy-staging"
    os.makedirs(str(sr), mode=0o700, exist_ok=True)
    st = os.lstat(str(sr))
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(
            f"deploy staging root {sr} is a symlink — refusing (possible symlink attack)"
        )
    # Ownership check is POSIX-only: on Windows st_uid is a meaningless 0
    # and os.getuid() does not exist. The symlink refusal above plus the
    # user-profile staging location carry the protection there.
    if platform_compat.IS_POSIX and st.st_uid != os.getuid():
        raise RuntimeError(
            f"deploy staging root {sr} not owned by current user (uid {os.getuid()}, "
            f"owner {st.st_uid})"
        )
    return sr


def _stage_tree_safe(source: Path, staging_root: Path) -> Path:
    """Hook-gated staging copy with hardlink rejection.

    Replaces shutil.copytree: walks the source tree, reads each file through
    hooks.safe_read_file_bytes_nolink (O_NOFOLLOW + fstat nlink + is_sensitive_path gate), and
    rejects files with st_nlink > 1 (hardlinks that could stage sensitive
    content bypassing the symlink check). Dirs are recreated with 0o700.

    Returns the path to the staged tree root. Raises RuntimeError on rejection.
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    dst = staging_root / "tree"
    os.makedirs(str(dst), mode=0o700, exist_ok=True)

    for dirpath, dirnames, filenames in os.walk(str(source), followlinks=False):
        # A symlinked DIRECTORY appears in dirnames but is not
        # descended (followlinks=False) and carries no file entries — it would
        # silently vanish from the snapshot, deploying something different
        # from the approved tree. Reject explicitly instead.
        for dname in dirnames:
            dpath = Path(dirpath) / dname
            if dpath.is_symlink():
                raise RuntimeError(
                    f"symlink-in-tree: symlinked directory at {dpath} — deploy blocked"
                )
        rel_dir = os.path.relpath(dirpath, str(source))
        target_dir = dst / rel_dir if rel_dir != "." else dst
        os.makedirs(str(target_dir), mode=0o700, exist_ok=True)

        for fname in filenames:
            src_file = Path(dirpath) / fname
            # Reject symlinks (already handled upstream, defense in depth)
            if src_file.is_symlink():
                raise RuntimeError(
                    f"symlink-in-tree: symlink found at {src_file} — deploy blocked"
                )
            # Reject hardlinks (st_nlink > 1)
            try:
                st = os.lstat(str(src_file))
            except OSError:
                raise RuntimeError(
                    f"hardlink-in-tree: cannot stat {src_file} — deploy blocked"
                )
            if st.st_nlink > 1:
                raise RuntimeError(
                    f"hardlink-in-tree: {src_file} has nlink={st.st_nlink} "
                    f"(hardlinked file) — deploy blocked"
                )
            # Read through the hook gate (sensitive-path + O_NOFOLLOW).
            # The nolink variant fstat()s the OPENED descriptor and
            # rejects st_nlink > 1 / non-regular inodes — the lstat() above is
            # only a fast-path pre-check; the authoritative hardlink rejection
            # is pinned to the same inode that gets read (no lstat->open race).
            # within_root pins containment to the OPENED fd — a nested
            # dir swapped for a symlink after the walk cannot smuggle files
            # from outside the approved tree into the public deployment.
            try:
                data = safe_read_file_bytes_nolink(
                    str(src_file), within_root=str(source)
                )
            except FileTooLargeError as e:
                # tree cap (200 MiB) > per-file cap — surface as a
                # structured staging rejection (409), not an escaping 500.
                raise RuntimeError(
                    f"file-too-large: {src_file} exceeds the per-file read cap "
                    f"({e}) — deploy blocked"
                ) from e
            if data is None:
                raise RuntimeError(
                    f"staging-read-blocked: {src_file} rejected by safe_read_file_bytes_nolink "
                    f"— deploy blocked"
                )
            target_file = target_dir / fname
            target_file.write_bytes(data)

    return dst


def _compute_content_digest(src_dir: str) -> str:
    """Compute a deterministic content digest for a staged/source directory.

    Produces a sha256 hash of a sorted manifest of (relative_path, size, content_hash)
    per file. Used at preview time and re-verified at confirm time to detect
    content changes between preview and confirmation.
    """
    import hashlib

    root = Path(src_dir)
    entries: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                rel = str(p.relative_to(root))
                content = p.read_bytes()
                file_hash = hashlib.sha256(content).hexdigest()[:16]
                entries.append(f"{rel}:{len(content)}:{file_hash}")
            except OSError:
                pass
    manifest = "\n".join(entries)
    return hashlib.sha256(manifest.encode()).hexdigest()


def _compute_tree_size_global(root: Path) -> int:
    """Total bytes of all regular files in a directory tree. Blocking."""
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _is_within(path: Path, root: Path) -> bool:
    # normpath + prefix check is the canonical containment barrier (recognized
    # by static analyzers); equivalent to relative_to for absolute resolved paths.
    p = os.path.normpath(str(path))
    r = os.path.normpath(str(root))
    return p == r or p.startswith(r + os.sep)


# --- config (profile NAME only — §6.1) ------------------------------------
# v2: the source of truth is the multi-profile registry (profiles.py). The
# legacy GET/PUT /config endpoints stay as a shim over the registry's default
# entry so the deploy skill and any older client keep working unchanged.

def _load_config() -> dict[str, Any]:
    """Legacy single-profile view = the registry's default entry."""
    reg = profiles_mod.load_registry()
    entry = profiles_mod.get_entry(reg, reg["default"]) if reg["default"] else None
    if entry is None:
        return {"profile": "", "region": DEFAULT_REGION}
    return {"profile": entry["name"], "region": entry["region"] or DEFAULT_REGION}


def _save_config(profile: str, region: str) -> dict[str, Any]:
    """Legacy PUT /config = upsert an entry and make it the default."""
    with profiles_mod.locked_registry() as reg:
        if profile:
            entry = profiles_mod.get_entry(reg, profile)
            if entry is None:
                reg["profiles"].append(profiles_mod.make_entry(profile, region))
            else:
                entry["region"] = region or entry["region"] or DEFAULT_REGION
            reg["default"] = profile
        else:
            reg["default"] = ""
    return {"profile": profile, "region": region or DEFAULT_REGION}


class _ProfileResolveError(Exception):
    """Raised when a deploy request's profile choice can't be resolved."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("error", "profile resolve failed"))
        self.payload = payload


async def _resolve_profile(params: dict[str, Any]) -> tuple[str, str]:
    """Resolve the request's (optional) profile choice → (name, region).

    The ``profile`` param is LLM/user-influenceable and flows into subprocess
    argv, so it is schema-validated, and it must name a *registered* profile —
    deploys never run with an arbitrary unregistered name. Empty → default.
    Raises :class:`_ProfileResolveError` (callers return its payload as a 400).
    """
    raw = str(params.get("profile", "") or "")
    if raw:
        try:
            raw = validate_field(raw, _PROFILE_SPEC)
        except ValidationError as e:
            raise _ProfileResolveError({"error": f"invalid profile: {e}"}) from e
    resolved = await asyncio.to_thread(profiles_mod.resolve_profile, raw)
    if resolved is None:
        if raw:
            raise _ProfileResolveError(
                {"error": f"profile '{raw}' is not registered — add it in Profiles first."})
        raise _ProfileResolveError(
            {"error": "deploy-web is not configured — add an AWS profile first (Profiles)."})
    return resolved


def _safe_site_id(raw: str) -> str:
    """Normalize a site id to a tag/label-safe slug."""
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (raw or "").strip().lower())
    return s.strip("-_")[:_SITE_ID_MAX]


# --- testable core (no aiohttp Request) ------------------------------------


def _stage_artifact_html(kind: str, content: str, name: str) -> tuple[list, str, int]:
    """Render + scan + write an artifact to a temp dir. Blocking (filesystem +
    CPU) -- must be called via asyncio.to_thread, never directly on the loop.

    Raises ValueError for webapp-kind artifacts (not deployable HTML).
    """
    if kind == "webapp":
        raise ValueError(
            "webapp artifacts contain an app summary, not deployable HTML; "
            "deploy the app's built directory via local_dir instead"
        )
    html = render_standalone(kind, content, title=name)
    findings = scan_content(html)
    tmp_dir = tempfile.mkdtemp(prefix="deploy-web-")
    Path(tmp_dir, "index.html").write_text(html, encoding="utf-8")
    return findings, tmp_dir, len(html.encode("utf-8"))


def _dir_contains_sensitive(src: Path, resolved: Path) -> bool:
    """Recursive sensitive-path walk. Blocking -- call via asyncio.to_thread."""
    if is_sensitive_path(str(src)) or is_sensitive_path(str(resolved)):
        return True
    for p in resolved.rglob("*"):
        resolved_child = _safe_resolve(p)
        # A symlink pointing outside the tree is itself suspicious --
        # treat it as sensitive (fail closed, block the deploy).
        if not _is_within(resolved_child, resolved):
            return True
        if is_sensitive_path(str(resolved_child)):
            return True
    return False


def _scan_tree(src: Path) -> tuple[list, int]:
    """Recursive content scan + size sum. Blocking -- call via asyncio.to_thread."""
    from kiro_crew.hooks import safe_read_file_bytes

    findings: list = []
    resolved_root = _safe_resolve(src)
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        # Symlink containment: resolve the target and ensure it stays within
        # the source tree. A symlink pointing outside -> fail closed (finding
        # surfaced, target content NEVER read). This blocks attacks where an
        # LLM-supplied dir contains `link -> /etc/passwd` or similar.
        resolved_file = _safe_resolve(f)
        if not _is_within(resolved_file, resolved_root):
            findings.append(Finding(
                kind="symlink-escape",
                snippet=f"{f.name} -> {resolved_file} escapes source tree",
                line=0,
                severity="credential",
            ))
            continue
        try:
            file_size = f.stat().st_size
        except OSError:
            findings.append(Finding(
                kind="unreadable-file",
                snippet=f"{f.name} — cannot stat (permission denied or I/O error)",
                line=0,
                severity="credential",
            ))
            continue
        if file_size >= _SCAN_SIZE_LIMIT:
            # Fail closed: a file too large to scan must not slip through to a
            # public upload unexamined -- surface it as a finding instead.
            findings.append(Finding(
                kind="unscanned-large-file",
                snippet=f"{f.name} ({file_size} bytes) exceeds the scan size limit",
                line=0,
                severity="credential",
            ))
            continue
        # Read as BYTES first to handle binary files (images, fonts, etc.)
        # without crashing on UnicodeDecodeError. Binary files are identified by
        # null bytes in the first 8KiB — they skip content scanning but are NOT
        # exempt from sensitive-path rejection (already checked via safe_read_file_bytes).
        raw_bytes = safe_read_file_bytes(str(resolved_file))
        if raw_bytes is None:
            # safe_read_file_bytes returns None when is_sensitive_path rejects
            # or file is unreadable — treat as credential finding (fail closed).
            findings.append(Finding(
                kind="hook-denied",
                snippet=f"{f.name} — denied by file-read security gate",
                line=0,
                severity="credential",
            ))
            continue
        # Credential patterns MUST be evaluated on every file regardless
        # of binary classification. A NUL-prepended HTML/JS file is still parseable
        # by browsers and can contain secrets. Decode with errors="replace" so NUL
        # bytes become U+FFFD but AKIA patterns remain intact.
        content = raw_bytes.decode("utf-8", errors="replace")
        cred_findings = [f for f in scan_content(content) if is_credential_finding(f)]
        findings.extend(cred_findings)

        # Binary detection: null byte in first 8KiB -> skip non-credential
        # content scanning (internal-host, ARN, account-id heuristics produce
        # noise on binary files). Credential findings above are kept regardless.
        if b"\x00" in raw_bytes[:8192]:
            continue
        # Full content scan (non-credential findings) for text files only.
        non_cred_findings = [f for f in scan_content(content) if not is_credential_finding(f)]
        findings.extend(non_cred_findings)
    byte_size = 0
    for p in src.rglob("*"):
        if p.is_file():
            try:
                byte_size += p.stat().st_size
            except OSError:
                pass  # already recorded as unreadable-file finding above
    return findings, byte_size


async def _do_deploy(params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Resolve artifact/dir → render → scan-gate → confirm-gate → engine.deploy."""
    if os.name == "nt":
        return 400, {
            "error": "Artifact Deploy requires a POSIX shell (bash) for deploy scripts. "
            "Use WSL (Windows Subsystem for Linux) to run the Kiro Crew gateway on Windows."
        }
    try:
        profile, region = await _resolve_profile(params)
    except _ProfileResolveError as e:
        return 400, e.payload

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
    _staged_path: Path | None = None
    try:
        if artifact_slug:
            if local_dir:
                return 400, {"error": "provide exactly one of artifact_slug or local_dir"}
            if not _HAS_ARTIFACTS:
                return 500, {"error": "artifact store unavailable"}

            def _resolve_artifact(slug: str) -> tuple[list["Finding"], str, int]:
                """Blocking helper: store lookup + stage in one thread hop."""
                art = get_default_store().get(slug)
                return _stage_artifact_html(art.kind, art.content or "", art.name or "")

            try:
                findings, staged_dir, byte_size = await asyncio.to_thread(
                    _resolve_artifact, artifact_slug)
            except ArtifactNotFoundError:
                return 404, {"error": f"artifact '{artifact_slug}' not found"}
            except ValueError as e:
                return 400, {"error": str(e)}
            tmp_dir = staged_dir
            src_dir = staged_dir
        else:
            # Validate the LLM-influenceable path via a validation.py schema
            # (type/length/charset) BEFORE any filesystem/subprocess use.
            try:
                local_dir = validate_field(local_dir, _LOCAL_DIR_SPEC)
            except ValidationError as e:
                return 400, {"error": f"invalid local_dir: {e}"}
            # CodeQL path expression: string-level normalization barrier
            # BEFORE any Path construction — reject relative paths explicitly.
            local_dir_norm = os.path.normpath(os.path.expanduser(local_dir))
            if not os.path.isabs(local_dir_norm):
                return 400, {"error": "local_dir must be an absolute path"}
            src = Path(local_dir_norm)
            try:
                # follow symlinks once, up front (blocking syscall -> thread)
                resolved = await asyncio.to_thread(src.resolve)
            except OSError:
                return 400, {"error": f"local_dir not found: {local_dir}"}
            if not await asyncio.to_thread(resolved.is_dir):
                return 400, {"error": f"local_dir not found: {local_dir}"}
            # Confine the resolved real path to an allow-listed root — defends
            # against a symlinked / traversal path escaping to arbitrary
            # filesystem locations and being uploaded to a PUBLIC bucket.
            roots = await asyncio.to_thread(_allowed_local_roots)
            # Inline containment barrier (normpath+startswith) for static-analysis
            # recognition at this critical call site (CodeQL path expression).
            resolved_str = os.path.normpath(str(resolved))
            allowed = any(
                resolved_str == os.path.normpath(str(r))
                or resolved_str.startswith(os.path.normpath(str(r)) + os.sep)
                for r in roots
            )
            if not allowed:
                _audit("deploy", site_id, "denied", error="local_dir outside allowed roots")
                return 400, {"error": ("local_dir must resolve within your home or a "
                                       "standard workspace directory")}
            # Security (§9.3): refuse if the dir IS — or recursively contains — a
            # sensitive credential path (~/.aws, ~/.ssh, ...). Check the raw and
            # the symlink-resolved dir AND each child's resolved target, since
            # `aws s3 sync` follows symlinks — a symlink inside the dir pointing
            # at a credential file would otherwise be uploaded.
            if await asyncio.to_thread(_dir_contains_sensitive, src, resolved):
                _audit("deploy", site_id, "denied",
                       error="local_dir is or contains a sensitive credential path")
                return 400, {"error": "local_dir is or contains a sensitive credential path"}
            src = resolved

            # TOCTOU defense — stage an immutable snapshot before scan+deploy.
            # Size guard: refuse trees > 200 MiB before copying.
            _MAX_STAGE_BYTES = 200 * 1024 * 1024

            def _compute_tree_size(root: Path) -> int:
                total = 0
                for p in root.rglob("*"):
                    if p.is_file():
                        try:
                            total += p.stat().st_size
                        except OSError:
                            pass
                return total

            tree_size = await asyncio.to_thread(_compute_tree_size, src)
            if tree_size > _MAX_STAGE_BYTES:
                return 400, {"error": f"local_dir tree exceeds 200 MiB ({tree_size} bytes)"}

            def _stage_tree(source: Path) -> tuple[Path, Path]:
                """Blocking: ensure staging root, mkdtemp, copytree, symlink check.

                Returns (staged_path, staged_src). Raises if symlinks found in snapshot.

                Security (TOCTOU): pins the source directory inode with an
                O_DIRECTORY|O_NOFOLLOW fd before copying. A symlink swapped at the
                root between containment-check and copytree is rejected by
                O_NOFOLLOW (ELOOP). The fd pins the inode for the duration of the
                copy via /proc/self/fd/<n> (Linux). On non-Linux (macOS uses
                /dev/fd, others lack it entirely) falls back to a re-lstat check
                after copytree to detect a swap.
                """
                sr = _staging_root()
                sp = Path(tempfile.mkdtemp(prefix="deploy-stage-", dir=str(sr)))

                # EVERY failure after mkdtemp must remove sp — the
                # caller only learns the staging path on success, so a raise
                # here would otherwise leak the tree until disk exhaustion.
                try:
                    return _stage_tree_pinned(source, sp)
                except BaseException:
                    shutil.rmtree(str(sp), True)
                    raise

            def _stage_tree_pinned(source: Path, sp: Path):
                import errno as _errno

                # Capture pre-copy stat for the inode check.
                pre_stat = os.stat(str(source))

                # Attempt fd-pinned copy (Linux /proc/self/fd).
                proc_fd_available = os.path.isdir("/proc/self/fd")
                if proc_fd_available:
                    try:
                        dir_fd = os.open(
                            str(source),
                            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        )
                    except OSError as exc:
                        shutil.rmtree(str(sp), True)
                        if exc.errno in (
                            _errno.ELOOP,
                            _errno.ENOTDIR,
                            getattr(_errno, "EMLINK", -1),
                        ):
                            raise RuntimeError(
                                "symlink-in-tree: source directory was swapped to a "
                                "symlink between containment check and staging"
                            ) from exc
                        raise
                    try:
                        fd_stat = os.fstat(dir_fd)
                        if (fd_stat.st_ino, fd_stat.st_dev) != (pre_stat.st_ino, pre_stat.st_dev):
                            raise RuntimeError(
                                "symlink-in-tree: source directory inode changed "
                                "between containment check and staging (possible swap)"
                            )
                        # Stage through the hook-gated helper (per-file
                        # safe_read_file_bytes + hardlink rejection) instead of
                        # shutil.copytree, walking via the pinned dir fd so the
                        # root cannot be swapped mid-copy.
                        staged_copy = _stage_tree_safe(
                            Path(f"/proc/self/fd/{dir_fd}"), sp
                        )
                    finally:
                        os.close(dir_fd)
                else:
                    # Fallback (non-Linux): hook-gated copy + post-copy re-lstat.
                    staged_copy = _stage_tree_safe(source, sp)
                    post_stat = os.stat(str(source))
                    if (post_stat.st_ino, post_stat.st_dev) != (pre_stat.st_ino, pre_stat.st_dev):
                        shutil.rmtree(str(sp), True)
                        raise RuntimeError(
                            "symlink-in-tree: source directory inode changed "
                            "during staging (possible swap)"
                        )

                staged = Path(staged_copy)
                # Reject ANY symlink in the staged snapshot (fail closed)
                symlinks_found = [
                    str(p) for p in staged.rglob("*") if p.is_symlink()
                ]
                if symlinks_found:
                    shutil.rmtree(str(sp), True)
                    raise RuntimeError(
                        f"symlink-in-tree: {len(symlinks_found)} symlink(s) "
                        f"found in staged snapshot — deploy blocked"
                    )
                return sp, staged

            try:
                staged_path, staged_src = await asyncio.to_thread(_stage_tree, src)
            except RuntimeError as e:
                # Convert EVERY expected staging rejection into a structured
                # 409 (hardlink, hook-gate, and file-too-large rejections)
                # instead of letting them escape as raw 500s.
                _rejects = ("symlink-in-tree", "hardlink-in-tree",
                            "staging-read-blocked", "file-too-large")
                if any(tag in str(e) for tag in _rejects):
                    _audit("deploy", site_id, "scan-blocked",
                           error=str(e))
                    return 409, {
                        "blocked": True,
                        "reason": "scan",
                        "credential": True,
                        "message": str(e),
                    }
                raise
            _staged_path = staged_path
            try:
                # Scan the STAGED copy (not the live directory — eliminates TOCTOU)
                findings, byte_size = await asyncio.to_thread(_scan_tree, staged_src)
                src_dir = str(staged_src)
            except Exception:
                # Cleanup staging on error
                await asyncio.to_thread(shutil.rmtree, str(staged_path), True)
                raise

        # Pre-publish scan gate (block-and-warn) — §4.1/Q4.
        # Credential-severity findings can NEVER be overridden (hard 409) — mirrors
        # deploy.sh semantics. override_scan only clears info-class findings.
        credential_findings = [f for f in findings if is_credential_finding(f)]
        if credential_findings:
            _audit("deploy", site_id, "scan-blocked",
                   error=f"{len(credential_findings)} credential finding(s) — cannot be overridden")
            return 409, {"blocked": True, "reason": "scan",
                         "findings": _redact_text(summarize(credential_findings)),
                         "count": len(credential_findings),
                         "credential": True,
                         "message": "Credential/security findings cannot be overridden."}
        if findings and params.get("override_scan") is not True:
            _audit("deploy", site_id, "scan-blocked",
                   error=f"{len(findings)} finding(s)")
            # Carry the preview bindings so an override-confirm is
            # pinned to the scanned content and resolved identity (same
            # fields as the clean requires_confirm preview).
            scan_digest = await asyncio.to_thread(_compute_content_digest, src_dir)
            return 409, {"blocked": True, "reason": "scan", "findings": _redact_text(summarize(findings)),
                         "count": len(findings),
                         "content_digest": scan_digest,
                         "profile": profile, "region": region}

        # Validate ttl_hours BEFORE any AWS call — invalid value -> 400, no deploy.
        # Strict type check — reject strings ("12"), floats (12.5), and bools (True).
        raw_ttl = params.get("ttl_hours")
        if raw_ttl is None:
            ttl_hours = 72
        elif isinstance(raw_ttl, bool):
            return 400, {"error": f"ttl_hours must be an integer, got bool: {raw_ttl!r}"}
        elif isinstance(raw_ttl, int):
            ttl_hours = raw_ttl
        elif isinstance(raw_ttl, float):
            return 400, {"error": f"ttl_hours must be an integer (not float), got: {raw_ttl!r}"}
        else:
            return 400, {"error": f"ttl_hours must be an integer, got {type(raw_ttl).__name__}: {raw_ttl!r}"}
        if ttl_hours < 0:
            return 400, {"error": f"ttl_hours must be non-negative, got: {ttl_hours}"}
        if ttl_hours > 8760:
            return 400, {"error": f"ttl_hours must be 0-8760 (0=persistent, max 1 year), got: {ttl_hours}"}

        # Confirm-gate — publishing makes content world-readable (§9.3).
        if params.get("confirm") is not True:
            # Include resolved canonical profile+region and a content digest
            # in the preview response so pending_params can store them for staleness
            # checks at confirm time.
            content_digest = await asyncio.to_thread(_compute_content_digest, src_dir)
            return 200, {"requires_confirm": True, "public": True, "site_id": site_id,
                         "bytes": byte_size,
                         "scan": _redact_text(summarize(findings)) if findings else "clean",
                         "profile": profile, "region": region,
                         "content_digest": content_digest,
                         "message": "This publishes to your own AWS account. Confirm to proceed."}

        # When the caller supplies expected_content_digest on a
        # confirm=true request, re-compute the current digest and reject with
        # 409 stale_preview if content changed between preview and confirm.
        expected_digest = str(params.get("expected_content_digest", "")).strip()
        if expected_digest:
            current_digest = await asyncio.to_thread(_compute_content_digest, src_dir)
            if current_digest != expected_digest:
                return 409, {
                    "error": "content changed since preview",
                    "code": "stale_preview",
                }

        # Bind the previewed IDENTITY, not just content — a concurrent
        # default-profile/region change between preview and confirm would
        # otherwise publish to a different AWS account than the one previewed.
        expected_profile = str(params.get("expected_profile", "")).strip()
        if expected_profile and expected_profile != profile:
            return 409, {
                "error": "resolved profile changed since preview",
                "code": "stale_preview",
            }
        expected_region = str(params.get("expected_region", "")).strip()
        if expected_region and expected_region != region:
            return 409, {
                "error": "resolved region changed since preview",
                "code": "stale_preview",
            }

        # Resolve the SHARED base-stack bucket (same as deploy.sh / reaper)
        # BEFORE deploying — if ttl_hours > 0 the reaper MUST exist.
        manifest_bucket = ""
        try:
            base_rc, base_out, _ = await asyncio.to_thread(
                engine.run_aws,
                ["cloudformation", "describe-stacks",
                 "--stack-name", "kirocrew-deploy-base",
                 "--query", "Stacks[0].Outputs", "--output", "json",
                 "--region", region],
                profile, 15,
            )
            if base_rc == 0 and base_out:
                base_outputs = {o["OutputKey"]: o["OutputValue"] for o in json.loads(base_out)}
                manifest_bucket = base_outputs.get("BucketName", "")
        except Exception:  # noqa: BLE001
            pass

        if not manifest_bucket and ttl_hours != 0:
            # Precondition failures render the EXACT operator command
            # with the request's real profile/region so remediation is
            # copy-paste, not archaeology.
            return 409, {
                "error": (
                    "Finite-TTL deploys require the reaper base stack "
                    "(kirocrew-deploy-base). Use ttl_hours=0 for persistent "
                    "or install the reaper (install-reaper.sh)."
                ),
                "remediation": _reaper_remediation(profile, region),
            }

        # Finite-TTL also requires the reaper Lambda stack (separate from base)
        if ttl_hours != 0:
            try:
                reaper_rc, _, _ = await asyncio.to_thread(
                    engine.run_aws,
                    ["cloudformation", "describe-stacks",
                     "--stack-name", "kirocrew-deploy-reaper",
                     "--query", "Stacks[0].StackStatus", "--output", "text",
                     "--region", region],
                    profile, 15,
                )
            except Exception:  # noqa: BLE001
                reaper_rc = 1
            if reaper_rc != 0:
                return 409, {
                    "error": (
                        "Finite-TTL deploys require the reaper stack "
                        "(kirocrew-deploy-reaper). Install the reaper "
                        "(install-reaper.sh) or use ttl_hours=0 for persistent."
                    ),
                    "remediation": _reaper_remediation(profile, region),
                }

        result = await asyncio.to_thread(engine.deploy, site_id, src_dir, profile, region)
        _audit("deploy", site_id, "ok")

        # Write the .kirocrew-deploy.json manifest (same shape that deploy.sh
        # writes) so the reaper knows the TTL. Dashboard deploys persist
        # ttl_hours here.
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        if ttl_hours == 0:
            expires_iso = ""
        else:
            expires_iso = (now_utc + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest_data = json.dumps({
            "slug": site_id,
            "owner": os.environ.get("USER", ""),
            "created_at": now_iso,
            "expires_at": expires_iso,
            "persistent": ttl_hours == 0,
            "ttl_hours": str(ttl_hours),
            "arch": "engine",
            "bucket": result.get("bucket", ""),
            "distribution_id": result.get("distribution_id", ""),
            "oac_id": result.get("oac_id", ""),
        })
        bucket = result.get("bucket", "")
        manifest_write_failed = False
        if bucket:
            # Use base-stack bucket when available; fall back to per-site bucket
            # only for persistent deploys (ttl=0) where the reaper is not needed.
            target_bucket = manifest_bucket or bucket
            try:
                def _write_deploy_manifest() -> int:
                    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
                    try:
                        f.write(manifest_data)
                        f.close()
                        rc, _, _ = engine.run_aws(
                            ["s3", "cp", f.name,
                             f"s3://{target_bucket}/{site_id}/.kirocrew-deploy.json",
                             "--content-type", "application/json",
                             "--region", region],
                            profile, 15,
                        )
                        return rc
                    finally:
                        try:
                            os.unlink(f.name)
                        except OSError:
                            pass

                manifest_rc = await asyncio.to_thread(_write_deploy_manifest)
                if manifest_rc != 0:
                    manifest_write_failed = True
            except Exception:  # noqa: BLE001
                logger.warning("deploy manifest write failed for %s (exception)", site_id)
                manifest_write_failed = True

        if manifest_write_failed and ttl_hours != 0:
            _audit("deploy", site_id, "manifest_failed")
            # Attempt best-effort rollback (recall empties S3, makes URL → 404)
            rolled_back = False
            try:
                await asyncio.to_thread(
                    engine.recall, site_id, profile, region
                )
                rolled_back = True
                _audit("deploy", site_id, "manifest_failed_rollback_ok")
            except Exception as rb_err:  # noqa: BLE001
                logger.warning(
                    "manifest rollback recall failed for %s: %s", site_id, rb_err
                )
                _audit("deploy", site_id, "manifest_failed_rollback_err",
                       error=str(rb_err))
            return 502, {
                "error": ("deployed but TTL manifest upload failed — "
                          "TTL cannot be enforced"),
                "public_url": result.get("url", ""),
                "site_id": site_id,
                "rolled_back": rolled_back,
            }
        if manifest_write_failed and ttl_hours == 0:
            result["warning"] = "TTL manifest upload failed (non-critical for persistent deploys)"

        # Persist the strong-identity field into the artifact's
        # webapp_metadata so teardown can cross-verify the manifest belongs
        # to THIS deployment (slug alone is mutable/forgeable). Best-effort:
        # a metadata write failure must not fail a successful deploy.
        if artifact_slug and result.get("distribution_id"):
            def _persist_dist_id() -> None:
                store = get_default_store()
                art = store.get(artifact_slug)
                meta = art.webapp_metadata
                if meta is not None and meta.deploy_target is not None:
                    meta.deploy_target.distribution_id = str(
                        result.get("distribution_id", ""))[:128]
                    # event_type must be in artifacts.ALLOWED_EVENT_TYPES --
                    # "edited" is the metadata-update event; a custom name
                    # would raise and silently skip persistence.
                    store.update(artifact_slug, webapp_metadata=meta,
                                 actor="deploy", event_type="edited")
            try:
                await asyncio.to_thread(_persist_dist_id)
            except Exception as e:  # noqa: BLE001 -- best-effort persistence
                logger.warning(
                    "could not persist distribution_id into artifact %s: %s",
                    artifact_slug, e)

        return 200, result
    except engine.AWSError as e:
        _audit("deploy", site_id, "failure", error=str(e))
        return 502, {"error": _safe_err(e), "missing_statement": e.missing_statement}
    finally:
        if tmp_dir:
            await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)
        if _staged_path:
            await asyncio.to_thread(shutil.rmtree, str(_staged_path), True)


async def _do_recall(params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        profile, region = await _resolve_profile(params)
    except _ProfileResolveError as e:
        return 400, e.payload
    site_id = _safe_site_id(str(params.get("site_id", "")))
    if not site_id:
        return 400, {"error": "site_id is required"}
    try:
        if params.get("confirm") is not True:
            site = await asyncio.to_thread(engine.find_site_by_tag, site_id, profile, region)
            if not site:
                return 404, {"error": f"no site '{site_id}'"}
            return 200, {"requires_confirm": True, "action": "recall", "site_id": site_id,
                         "resources": site,
                         "message": ("Recall empties the site (URL → 404) but keeps the infra "
                                     "(reversible). Note: edge caches may serve briefly, and "
                                     "already-downloaded content cannot be recalled.")}
        # Mirror the destroy binding — recall empties the
        # bucket, so a site recreated between preview and confirm must not
        # be emptied under a stale dialog.
        exp_dist = str(params.get("expected_distribution_id", "") or "")
        exp_bucket = str(params.get("expected_bucket", "") or "")
        if exp_dist or exp_bucket:
            live = await asyncio.to_thread(
                engine.find_site_by_tag, site_id, profile, region)
            if not live:
                return 404, {"error": f"no site '{site_id}'"}
            if ((exp_dist and live.get("distribution_id", "") != exp_dist)
                    or (exp_bucket and live.get("bucket", "") != exp_bucket)):
                _audit("recall", site_id, "denied",
                       error="resource ids changed since preview")
                return 409, {"error": "site resources changed since preview — "
                                      "re-run the recall preview and confirm "
                                      "against the current resources"}
        result = await asyncio.to_thread(engine.recall, site_id, profile, region)
        _audit("recall", site_id, "ok")
        return 200, result
    except engine.AWSError as e:
        _audit("recall", site_id, "failure", error=str(e))
        return 502, {"error": _safe_err(e), "missing_statement": e.missing_statement}


async def _do_destroy(params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        profile, region = await _resolve_profile(params)
    except _ProfileResolveError as e:
        return 400, e.payload
    site_id = _safe_site_id(str(params.get("site_id", "")))
    if not site_id:
        return 400, {"error": "site_id is required"}
    try:
        if params.get("confirm") is not True:
            site = await asyncio.to_thread(engine.find_site_by_tag, site_id, profile, region)
            if not site:
                return 404, {"error": f"no site '{site_id}'"}
            return 200, {"requires_confirm": True, "action": "destroy", "site_id": site_id,
                         "resources": site, "destructive": True,
                         "message": (f"DESTROY will permanently delete bucket "
                                     f"'{site.get('bucket', '')}' and distribution "
                                     f"'{site.get('distribution_id', '')}'. This cannot be undone.")}
        # Bind the confirmed destroy to the resources shown at
        # preview — if the site was recreated between preview and confirm,
        # the live ids differ from the previewed ones and we refuse.
        exp_dist = str(params.get("expected_distribution_id", "") or "")
        exp_bucket = str(params.get("expected_bucket", "") or "")
        if exp_dist or exp_bucket:
            live = await asyncio.to_thread(
                engine.find_site_by_tag, site_id, profile, region)
            if not live:
                return 404, {"error": f"no site '{site_id}'"}
            if ((exp_dist and live.get("distribution_id", "") != exp_dist)
                    or (exp_bucket and live.get("bucket", "") != exp_bucket)):
                _audit("destroy", site_id, "denied",
                       error="resource ids changed since preview")
                return 409, {"error": "site resources changed since preview — "
                                      "re-run the destroy preview and confirm "
                                      "against the current resources"}
        result = await asyncio.to_thread(engine.destroy, site_id, profile, region)
        _audit("destroy", site_id, "ok")
        return 200, result
    except engine.AWSError as e:
        _audit("destroy", site_id, "failure", error=str(e))
        return 502, {"error": _safe_err(e), "missing_statement": e.missing_statement}


async def _do_list() -> tuple[int, dict[str, Any]]:
    """List sites across ALL registered profiles, tagging each with its profile.

    Attribution is by reachability, matching the stateless-by-tag model: the
    profile whose account a site lives in is the one that lists it. Sites are
    deduped by distribution id (two profiles pointing at the same account see
    the same sites once, attributed to the first profile that returned them).
    Per-profile failures degrade to a warning instead of failing the fleet.

    Bounded concurrency (semaphore=5) replaces serial iteration.
    """
    # engine.list_sites spawns via subprocess preexec_fn — unsupported
    # on native Windows (deploy features are POSIX-only). Return a structured
    # unsupported result instead of an uncaught exception.
    if os.name == "nt":
        return 200, {"sites": [], "configured": False,
                     "error": "deploy features are not supported on Windows"}

    reg = await asyncio.to_thread(profiles_mod.load_registry)
    if not reg["profiles"]:
        return 200, {"sites": [], "configured": False}

    _SEM = asyncio.Semaphore(5)

    async def _fetch_one(entry: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        async with _SEM:
            try:
                sites = await asyncio.to_thread(
                    engine.list_sites, entry["name"], entry["region"] or DEFAULT_REGION)
                return [({**s, "profile": entry["name"]}) for s in sites], None
            except engine.AWSError as e:
                return [], f"{entry['name']}: {_safe_err(e)}"
            except Exception as e:  # noqa: BLE001 -- per-profile isolation:
                # a single bad profile (unexpected KeyError, parse error) must
                # degrade to a warning, not fail the whole fleet listing.
                return [], f"{entry['name']}: {_safe_err(e)}"

    results = await asyncio.gather(*[_fetch_one(e) for e in reg["profiles"]])

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for sites, err in results:
        if err:
            errors.append(err)
            continue
        for s in sites:
            key = s.get("distribution_id") or f"{s['profile']}/{s.get('site_id', '')}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(s)
    payload: dict[str, Any] = {"sites": merged, "configured": True}
    if errors:
        payload["profile_errors"] = errors[:10]
    return 200, payload


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
    discovered = await asyncio.to_thread(profiles_mod.discover_aws_profiles)
    registered = {p["name"] for p in reg["profiles"]}
    # The blocking credential-redaction rule applies to EVERY
    # profile-related string in the response -- `default` and the discovered
    # `available` names were the two unredacted stragglers.
    return web.json_response({
        "profiles": _redact_profile_fields(reg["profiles"]),
        "default": _redact_text(str(reg["default"])),
        "available": [_redact_text(str(n))
                      for n in discovered if n not in registered],
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
    # profile that no longer exists (or was never registered), refuse to execute
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

        # Size guard (same _MAX_STAGE_BYTES as deploy path).
        _MAX_STAGE_BYTES_CONFIRM = 200 * 1024 * 1024
        tree_size = await asyncio.to_thread(_compute_tree_size_global, local_path)
        if tree_size > _MAX_STAGE_BYTES_CONFIRM:
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
    status, payload = await _do_deploy(params)
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
    previously-permitted deployment left behind.

    Runs the check in a worker thread: the admission path can initialize the SEL
    audit log, which on a fresh gateway does blocking file IO (trust-dir
    creation, key validation) and would otherwise stall the event loop for
    every request.
    """

    @functools.wraps(handler)
    async def _guarded(request: web.Request) -> web.StreamResponse:
        # circular import: the dashboard handler layer imports the deploy module
        # at registration time, so this is a downward import — same pattern as
        # `_is_restricted_session` above.
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
