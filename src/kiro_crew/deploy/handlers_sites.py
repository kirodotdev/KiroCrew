"""The site operations behind the deploy routes, callable without an aiohttp request.

``_do_deploy`` resolves the profile and the content, applies the pre-publish scan gate and
the confirm gate, checks for the finite-TTL reaper, publishes through ``engine.deploy``,
writes the TTL manifest and records the deployment in the artifact's ``webapp_metadata``.
``_do_recall`` and ``_do_destroy`` confirm-gate taking a site down, and ``_do_list`` lists
the sites of every registered profile. Each returns ``(status, payload)`` for a route in
:mod:`kiro_crew.deploy.handlers` to answer with. What they and the routes share about the
AWS identity lives here too: the profile and region input specs, the registry-backed
profile resolution (``_resolve_profile``) and the legacy single-profile config view
(``_load_config``, ``_save_config``).

It logs under the route module's name, ``kiro_crew.deploy.handlers``, so a log filter or
capture keyed on that name still sees these records.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.deploy import engine
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.deploy.handlers_content import (
    _ARTIFACT_SLUG_SPEC,
    _LOCAL_DIR_SPEC,
    _MAX_STAGE_BYTES,
    WebAppRootError,
    _allowed_local_roots,
    _compute_content_digest,
    _compute_tree_size_global,
    _dir_contains_sensitive,
    _scan_tree,
    _stage_artifact_html,
    _stage_tree_safe,
    _staging_root,
    resolve_webapp_public_dir,
)
from kiro_crew.deploy.handlers_guards import (
    _audit,
    _base_remediation,
    _reaper_remediation,
    _redact_text,
    _safe_err,
)
from kiro_crew.deploy.scan import Finding, is_credential_finding, summarize
from kiro_crew.publish_governance import deploy_target_denied_reason
from kiro_crew.validation import ValidationError, validate_field

try:
    from kiro_crew.artifacts import ArtifactNotFoundError, get_default_store

    _HAS_ARTIFACTS = True
except ImportError:  # pragma: no cover - defensive
    _HAS_ARTIFACTS = False

logger = logging.getLogger("kiro_crew.deploy.handlers")


# profile/region are LLM-influenceable (chat-native skill) and flow into subprocess
# argv (--profile/--region) on every aws call, so they get schema validation too.
# Both allow empty (clears profile / falls back to default region); the pattern is
# only enforced on non-empty values by validate_field. The profile charset
# ('+' admitted for IAM Identity Center derived names, leading '-' excluded,
# \Z anchor) is profiles.py's PROFILE_SPEC, aliased like REGION_SPEC
# below rather than re-spelled here.
_PROFILE_SPEC = profiles_mod.PROFILE_SPEC
_REGION_SPEC = profiles_mod.REGION_SPEC


DEFAULT_REGION = engine.DEFAULT_REGION
_SITE_ID_MAX = 64


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


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, so this module's name must not leak into it.
_ProfileResolveError.__module__ = "kiro_crew.deploy.handlers"


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
                {"error": f"profile '{raw}' is not registered — add it in Profiles first."}
            )
        raise _ProfileResolveError(
            {"error": "deploy-web is not configured — add an AWS profile first (Profiles)."}
        )
    return resolved


def _safe_site_id(raw: str) -> str:
    """Normalize a site id to a tag/label-safe slug."""
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (raw or "").strip().lower())
    return s.strip("-_")[:_SITE_ID_MAX]


# --- testable core (no aiohttp Request) ------------------------------------


async def _do_deploy(
    params: dict[str, Any], *, session_key: str = ""
) -> tuple[int, dict[str, Any]]:
    """Resolve artifact/dir → render → scan-gate → confirm-gate → engine.deploy.

    *session_key* is the caller's session, which picks the governance profile the
    publish lists are asked under. It never comes from *params* (the body).
    """
    if os.name == "nt":
        return 400, {
            "error": "Artifact Deploy requires a POSIX shell (bash) for deploy scripts. "
            "Use WSL (Windows Subsystem for Linux) to run the Kiro Crew gateway on Windows."
        }
    try:
        profile, region = await _resolve_profile(params)
    except _ProfileResolveError as e:
        return 400, e.payload

    # The policy's publish lists (audience, regions, accounts), asked before any
    # content is staged or uploaded, on preview and again on confirm. Off the
    # loop: it reads policy and profiles from disk.
    denied = await asyncio.to_thread(deploy_target_denied_reason, session_key, region)
    if denied:
        _audit("deploy", str(params.get("site_id", "")), "denied", error=denied)
        return 403, {
            "error": f"public web deploy is disabled by policy: {denied}",
            "code": "publish_destination_disabled",
        }

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
        # A kind=webapp artifact carries a real on-disk app tree, not deployable
        # HTML, so it resolves to a static root and then travels the SAME route a
        # local_dir deploy takes below. That route owns the staging snapshot, the
        # symlink and sensitive-path refusals, the size guard and the content
        # scan, and a webapp needs every one of them. None means "not a webapp":
        # the artifact's own content is staged as standalone HTML, unchanged.
        webapp_root: Path | None = None
        # The slug's generation as read by the SAME lookup that supplies the
        # content being deployed. Capturing it any later leaves a window where a
        # delete-and-recreate is invisible: the original snapshot would deploy
        # while the fence recorded the replacement's generation, so the
        # replacement would be handed the deployment's URL and lifecycle.
        deployed_generation: str = ""
        if artifact_slug:
            if local_dir:
                return 400, {"error": "provide exactly one of artifact_slug or local_dir"}
            if not _HAS_ARTIFACTS:
                return 500, {"error": "artifact store unavailable"}

            def _resolve_webapp(slug: str) -> tuple[Path | None, str]:
                """Blocking helper: generation + webapp root in one thread hop."""
                # ONE read feeds both the generation and the content root, so
                # the identity always describes the bytes being deployed. An
                # absent or blank generation means the fence cannot be applied,
                # not that it passes: the writeback skips the comparison.
                art = get_default_store().get(slug)
                generation = getattr(art, "created_at", "") or ""
                return resolve_webapp_public_dir(slug, art), generation

            try:
                webapp_root, deployed_generation = await asyncio.to_thread(
                    _resolve_webapp, artifact_slug
                )
            except ArtifactNotFoundError:
                return 404, {"error": f"artifact '{artifact_slug}' not found"}
            except WebAppRootError as e:
                # A webapp whose root is not publishable: say WHICH precondition
                # failed. The caller turns this into the "Deploy via agent"
                # affordance rather than a dead button. Plain sentence in
                # `error`, field/directory names in `details`.
                payload: dict[str, Any] = {"error": str(e), "code": "webapp_root_unavailable"}
                if getattr(e, "details", ""):
                    payload["details"] = e.details
                return 400, payload
        if artifact_slug and webapp_root is None:

            def _resolve_artifact(slug: str) -> tuple[list["Finding"], str, int, str]:
                """Blocking helper: store lookup + stage in one thread hop."""
                art = get_default_store().get(slug)
                findings, staged, size = _stage_artifact_html(
                    art.kind, art.content or "", art.name or ""
                )
                return findings, staged, size, getattr(art, "created_at", "") or ""

            try:
                findings, staged_dir, byte_size, deployed_generation = await asyncio.to_thread(
                    _resolve_artifact, artifact_slug
                )
            except ArtifactNotFoundError:
                return 404, {"error": f"artifact '{artifact_slug}' not found"}
            except ValueError as e:
                return 400, {"error": str(e)}
            tmp_dir = staged_dir
            src_dir = staged_dir
        else:
            # A webapp root is resolved SERVER-side from the artifact's own
            # metadata and already confined to an allow-listed root, so it skips
            # _LOCAL_DIR_SPEC's charset: that pattern is there to sanitize
            # caller-supplied text, and applying it to a resolved project path
            # would refuse a legitimate directory name containing '+' or
            # non-ASCII characters. Every filesystem check below — normalization,
            # absoluteness, containment, the sensitive-path walk, the staging
            # snapshot and the scan — still runs on it unchanged.
            if webapp_root is not None:
                local_dir = str(webapp_root)
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
                return 400, {
                    "error": (
                        "local_dir must resolve within your home or a "
                        "standard workspace directory"
                    )
                }
            # Security (§9.3): refuse if the dir IS — or recursively contains — a
            # sensitive credential path (~/.aws, ~/.ssh, ...). Check the raw and
            # the symlink-resolved dir AND each child's resolved target, since
            # `aws s3 sync` follows symlinks — a symlink inside the dir pointing
            # at a credential file would otherwise be uploaded.
            if await asyncio.to_thread(_dir_contains_sensitive, src, resolved):
                _audit(
                    "deploy",
                    site_id,
                    "denied",
                    error="local_dir is or contains a sensitive credential path",
                )
                return 400, {"error": "local_dir is or contains a sensitive credential path"}
            src = resolved

            # TOCTOU defense — stage an immutable snapshot before scan+deploy.
            # Size guard: refuse trees > 200 MiB before copying.
            tree_size = await asyncio.to_thread(_compute_tree_size_global, src)
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
                        staged_copy = _stage_tree_safe(Path(f"/proc/self/fd/{dir_fd}"), sp)
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
                # Reject ANY link in the staged snapshot (fail closed).
                # ``is_link_or_junction``, not ``Path.is_symlink()``: a Windows
                # junction is a reparse point ``is_symlink()`` answers False for,
                # so this second fence passed one. Enumeration is not the hazard
                # here — the scan only reads names, and the staging walk writes
                # plain dirs and files, so a link at this point is a racer's.
                symlinks_found = [
                    str(p) for p in staged.rglob("*") if platform_compat.is_link_or_junction(p)
                ]
                if symlinks_found:
                    shutil.rmtree(str(sp), True)
                    raise RuntimeError(
                        f"symlink-in-tree: {len(symlinks_found)} link(s) "
                        f"found in staged snapshot — deploy blocked"
                    )
                return sp, staged

            try:
                staged_path, staged_src = await asyncio.to_thread(_stage_tree, src)
            except RuntimeError as e:
                # Convert EVERY expected staging rejection into a structured
                # 409 (hardlink, hook-gate, and file-too-large rejections)
                # instead of letting them escape as raw 500s.
                _rejects = (
                    "symlink-in-tree",
                    "hardlink-in-tree",
                    "staging-read-blocked",
                    "file-too-large",
                )
                if any(tag in str(e) for tag in _rejects):
                    _audit("deploy", site_id, "scan-blocked", error=str(e))
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
            _audit(
                "deploy",
                site_id,
                "scan-blocked",
                error=f"{len(credential_findings)} credential finding(s) — cannot be overridden",
            )
            return 409, {
                "blocked": True,
                "reason": "scan",
                "findings": _redact_text(summarize(credential_findings)),
                "count": len(credential_findings),
                "credential": True,
                "message": "Credential/security findings cannot be overridden.",
            }
        if findings and params.get("override_scan") is not True:
            _audit("deploy", site_id, "scan-blocked", error=f"{len(findings)} finding(s)")
            # Carry the preview bindings so an override-confirm is
            # pinned to the scanned content and resolved identity (same
            # fields as the clean requires_confirm preview).
            scan_digest = await asyncio.to_thread(_compute_content_digest, src_dir)
            # fmt: off
            return 409, {"blocked": True, "reason": "scan", "findings": _redact_text(summarize(findings)),
                         "count": len(findings),
                         "content_digest": scan_digest,
                         "profile": profile, "region": region}
            # fmt: on

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
            return 400, {
                "error": f"ttl_hours must be an integer, got {type(raw_ttl).__name__}: {raw_ttl!r}"
            }
        if ttl_hours < 0:
            return 400, {"error": f"ttl_hours must be non-negative, got: {ttl_hours}"}
        if ttl_hours > 8760:
            return 400, {
                "error": f"ttl_hours must be 0-8760 (0=persistent, max 1 year), got: {ttl_hours}"
            }

        # Confirm-gate — publishing makes content world-readable (§9.3).
        if params.get("confirm") is not True:
            # Include resolved canonical profile+region and a content digest
            # in the preview response so pending_params can store them for staleness
            # checks at confirm time.
            content_digest = await asyncio.to_thread(_compute_content_digest, src_dir)
            return 200, {
                "requires_confirm": True,
                "public": True,
                "site_id": site_id,
                "bytes": byte_size,
                "scan": _redact_text(summarize(findings)) if findings else "clean",
                "profile": profile,
                "region": region,
                "content_digest": content_digest,
                "message": "This publishes to your own AWS account. Confirm to proceed.",
            }

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
                [
                    "cloudformation",
                    "describe-stacks",
                    "--stack-name",
                    "kirocrew-deploy-base",
                    "--query",
                    "Stacks[0].Outputs",
                    "--output",
                    "json",
                    "--region",
                    region,
                ],
                profile,
                15,
            )
            if base_rc == 0 and base_out:
                base_outputs = {o["OutputKey"]: o["OutputValue"] for o in json.loads(base_out)}
                manifest_bucket = base_outputs.get("BucketName", "")
        except Exception:  # noqa: BLE001
            pass

        if not manifest_bucket and ttl_hours != 0:
            # Two audiences, two fields. `error` is the sentence a non-AWS person
            # reads in the banner: what happened, and what they can do next.
            # `details` carries the stack and parameter names, which mean nothing
            # to that reader but are exactly what an operator (or the MCP tool's
            # LLM caller, which needs ttl_hours=0 to retry correctly) acts on;
            # the UI shows it behind a Details toggle. `remediation` is the
            # runnable command. `code` is what the dashboard keys its two
            # affordances off, so the wording stays free to change without
            # silently turning the buttons off.
            return 409, {
                "error": (
                    f"This deploy is set to expire in {ttl_hours} hours, but your "
                    "AWS account has no auto-cleanup installed yet. Deploy it as "
                    "permanent instead, or install auto-cleanup first."
                ),
                "code": "reaper_required",
                "details": (
                    "Finite-TTL deploys require the reaper base stack "
                    f"(kirocrew-deploy-base), which was not found in {region} "
                    "(or could not be read with this profile). Use ttl_hours=0 "
                    "for persistent, or create the base stack first and then "
                    "install the reaper (install-reaper.sh exits while the base "
                    "is missing). install-reaper.sh also needs the "
                    "kirocrew-deploy-app-boundary IAM policy and credentials "
                    "that may create IAM roles."
                ),
                "remediation": _base_remediation(profile, region),
            }

        # Finite-TTL also requires the reaper Lambda stack (separate from base)
        if ttl_hours != 0:
            try:
                reaper_rc, _, _ = await asyncio.to_thread(
                    engine.run_aws,
                    [
                        "cloudformation",
                        "describe-stacks",
                        "--stack-name",
                        "kirocrew-deploy-reaper",
                        "--query",
                        "Stacks[0].StackStatus",
                        "--output",
                        "text",
                        "--region",
                        region,
                    ],
                    profile,
                    15,
                )
            except Exception:  # noqa: BLE001
                reaper_rc = 1
            if reaper_rc != 0:
                return 409, {
                    "error": (
                        f"This deploy is set to expire in {ttl_hours} hours, but "
                        "your AWS account's auto-cleanup is not finished "
                        "installing. Deploy it as permanent instead, or finish "
                        "installing auto-cleanup first."
                    ),
                    "code": "reaper_required",
                    "details": (
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
        manifest_data = json.dumps(
            {
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
            }
        )
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
                            [
                                "s3",
                                "cp",
                                f.name,
                                f"s3://{target_bucket}/{site_id}/.kirocrew-deploy.json",
                                "--content-type",
                                "application/json",
                                "--region",
                                region,
                            ],
                            profile,
                            15,
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
                await asyncio.to_thread(engine.recall, site_id, profile, region)
                rolled_back = True
                _audit("deploy", site_id, "manifest_failed_rollback_ok")
            except Exception as rb_err:  # noqa: BLE001
                logger.warning("manifest rollback recall failed for %s: %s", site_id, rb_err)
                _audit("deploy", site_id, "manifest_failed_rollback_err", error=str(rb_err))
            return 502, {
                "error": ("deployed but TTL manifest upload failed — " "TTL cannot be enforced"),
                "public_url": result.get("url", ""),
                "site_id": site_id,
                "rolled_back": rolled_back,
            }
        if manifest_write_failed and ttl_hours == 0:
            result["warning"] = "TTL manifest upload failed (non-critical for persistent deploys)"

        # Persist the deployment into the artifact's webapp_metadata:
        # `distribution_id` is the strong-identity field teardown cross-verifies
        # the manifest against (slug alone is mutable/forgeable), and the
        # public_url / lifecycle / profile fields are what flip the artifact card
        # and the Deployments table out of "not deployed".
        #
        # This is done HERE rather than by the caller because every route that
        # deploys an artifact needs it: a dashboard-initiated deploy has no agent
        # to write these fields on its behalf, so server-side is what lets the
        # direct confirm and the pending confirm both inherit them.
        #
        # Best-effort: a metadata write failure must not fail a successful deploy.
        if artifact_slug and (result.get("distribution_id") or result.get("url")):

            def _persist_deployment() -> None:
                store = get_default_store()
                art = store.get(artifact_slug)
                # Only the generation whose content was staged and deployed gets
                # the deploy identity.
                if deployed_generation and getattr(art, "created_at", "") != deployed_generation:
                    logger.warning(
                        "deploy writeback skipped for %s: the slug holds a "
                        "different artifact than the one deployed",
                        artifact_slug,
                    )
                    return
                meta = art.webapp_metadata
                if meta is None:
                    return
                if meta.deploy_target is not None:
                    if result.get("distribution_id"):
                        meta.deploy_target.distribution_id = str(result.get("distribution_id", ""))[
                            :128
                        ]
                    if result.get("url"):
                        meta.deploy_target.public_url = str(result.get("url", ""))[:2048]
                    if profile:
                        meta.deploy_target.profile = profile[:128]
                    if region:
                        meta.deploy_target.region = region[:128]
                if meta.lifecycle is not None:
                    meta.lifecycle.status = "live"
                    meta.lifecycle.created_at = now_iso
                    meta.lifecycle.ttl_hours = ttl_hours
                    meta.lifecycle.persistent = ttl_hours == 0
                    # None, not "", is this field's documented persistent value.
                    meta.lifecycle.expires_at = expires_iso or None
                # event_type must be in artifacts.ALLOWED_EVENT_TYPES --
                # "edited" is the metadata-update event; a custom name
                # would raise and silently skip persistence.
                store.update(
                    artifact_slug, webapp_metadata=meta, actor="deploy", event_type="edited"
                )

            try:
                await asyncio.to_thread(_persist_deployment)
            except Exception as e:  # noqa: BLE001 -- best-effort persistence
                logger.warning(
                    "could not persist deployment into artifact %s: %s", artifact_slug, e
                )

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
            return 200, {
                "requires_confirm": True,
                "action": "recall",
                "site_id": site_id,
                "resources": site,
                "message": (
                    "Recall empties the site (URL → 404) but keeps the infra "
                    "(reversible). Note: edge caches may serve briefly, and "
                    "already-downloaded content cannot be recalled."
                ),
            }
        # Mirror the destroy binding — recall empties the
        # bucket, so a site recreated between preview and confirm must not
        # be emptied under a stale dialog.
        exp_dist = str(params.get("expected_distribution_id", "") or "")
        exp_bucket = str(params.get("expected_bucket", "") or "")
        if exp_dist or exp_bucket:
            live = await asyncio.to_thread(engine.find_site_by_tag, site_id, profile, region)
            if not live:
                return 404, {"error": f"no site '{site_id}'"}
            if (exp_dist and live.get("distribution_id", "") != exp_dist) or (
                exp_bucket and live.get("bucket", "") != exp_bucket
            ):
                _audit("recall", site_id, "denied", error="resource ids changed since preview")
                return 409, {
                    "error": "site resources changed since preview — "
                    "re-run the recall preview and confirm "
                    "against the current resources"
                }
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
            return 200, {
                "requires_confirm": True,
                "action": "destroy",
                "site_id": site_id,
                "resources": site,
                "destructive": True,
                "message": (
                    f"DESTROY will permanently delete bucket "
                    f"'{site.get('bucket', '')}' and distribution "
                    f"'{site.get('distribution_id', '')}'. This cannot be undone."
                ),
            }
        # Bind the confirmed destroy to the resources shown at
        # preview — if the site was recreated between preview and confirm,
        # the live ids differ from the previewed ones and we refuse.
        exp_dist = str(params.get("expected_distribution_id", "") or "")
        exp_bucket = str(params.get("expected_bucket", "") or "")
        if exp_dist or exp_bucket:
            live = await asyncio.to_thread(engine.find_site_by_tag, site_id, profile, region)
            if not live:
                return 404, {"error": f"no site '{site_id}'"}
            if (exp_dist and live.get("distribution_id", "") != exp_dist) or (
                exp_bucket and live.get("bucket", "") != exp_bucket
            ):
                _audit("destroy", site_id, "denied", error="resource ids changed since preview")
                return 409, {
                    "error": "site resources changed since preview — "
                    "re-run the destroy preview and confirm "
                    "against the current resources"
                }
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
    # Deploy features are POSIX-only, so native Windows gets a structured
    # unsupported result before engine.list_sites runs any AWS call.
    if os.name == "nt":
        return 200, {
            "sites": [],
            "configured": False,
            "error": "deploy features are not supported on Windows",
        }

    reg = await asyncio.to_thread(profiles_mod.load_registry)
    if not reg["profiles"]:
        return 200, {"sites": [], "configured": False}

    _SEM = asyncio.Semaphore(5)

    async def _fetch_one(entry: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        async with _SEM:
            try:
                sites = await asyncio.to_thread(
                    engine.list_sites, entry["name"], entry["region"] or DEFAULT_REGION
                )
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
