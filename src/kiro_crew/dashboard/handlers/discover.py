"""API handlers for multi-provider skill discovery.

Provides ``/api/skills/-/discover`` (search) and extends the existing
``/api/skills/-/install`` to support provider-based installation.

These handlers sit alongside the existing PromptFarm-specific handlers
in prompts.py — the discover endpoint is additive (new capability), while
the install handler is provider-aware (delegates to the right backend).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _get_skills
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel as _sel
from kiro_crew.skill_providers.base import ProviderRegistry
from kiro_crew.skill_providers.skillsh import SkillsShConfig, SkillsShProvider
from kiro_crew.skills import skills_dir as _skills_dir

logger = logging.getLogger(__name__)

# Slug validation for skill installation (filesystem safety).
_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


def _redact_external(text: str) -> str:
    """Scrub provider-sourced strings before returning them to the dashboard.

    Any skills.sh publisher controls these fields -- scan for credential
    patterns and exfiltration URLs per the security-controls guideline.
    Benign content passes through unchanged.
    """
    if not text:
        return text
    scrubbed, _ = redact_credentials(text)
    scrubbed, _ = redact_exfiltration_urls(scrubbed)
    return scrubbed


def _build_registry() -> ProviderRegistry:
    """Build the provider registry with all available providers.

    Called once at handler setup time. Providers check their own
    availability dynamically (config flags, auth state, etc.).
    """
    registry = ProviderRegistry()

    # skills.sh — always registered, enabled by default (public API, no auth)
    registry.register(SkillsShProvider(SkillsShConfig(enabled=True)))

    # PromptFarm — registered but availability depends on config. The existing
    # /api/skills/-/remote endpoint remains for backward compat; the discover
    # endpoint also includes PromptFarm results when configured.
    # NOTE: PromptFarm integration via discover is a future addition —
    # for now, the dedicated Browse PromptFarm modal remains the primary
    # path for PromptFarm skills. This keeps the initial PR scoped.

    return registry


# Module-level singleton — cheap to build, providers are stateless.
_registry: ProviderRegistry | None = None


def _get_registry() -> ProviderRegistry:
    """Lazy-init the global provider registry."""
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


async def api_skills_discover(request: web.Request) -> web.Response:
    """GET /api/skills/-/discover?q=<query>[&provider=<name>][&limit=N]

    Multi-provider skill search. Fans out to all available providers
    (or a specific one if ``provider`` param is given) and returns
    merged results with provider badges.

    Response shape:
    {
      "results": [
        {"id": "...", "name": "...", "description": "...", "provider": "skillsh",
         "display_provider": "skills.sh", "repo_url": "...", "author": "...",
         "installed": false, "tags": [...]}
      ],
      "providers": ["skillsh"]
    }
    """
    query = request.query.get("q", "").strip()
    provider_filter = request.query.get("provider", "").strip() or None
    try:
        # Clamp BOTH ends: the upper-only min() let limit<=0 through, where
        # merged[:limit] / items[:limit] silently drop results (limit=-1) or
        # return nothing (limit=0), and &limit=-1 hit the provider URL.
        limit = max(1, min(int(request.query.get("limit", "20")), 50))
    except ValueError:
        limit = 20

    if not query:
        return web.json_response({"results": [], "providers": []})

    registry = _get_registry()

    # Mark installed skills so the UI can show an "Installed" badge.
    # list_skills() walks the skills directory synchronously -- offload it.
    state = request.app["state"]
    skills = _get_skills(state)
    all_skills = await asyncio.to_thread(skills.list_skills)
    local_keys = {s["key"] for s in all_skills}

    results = await registry.search(query, provider=provider_filter, limit=limit)

    # Resolve installed state and build response items.
    items = []
    for r in results:
        # Check if a skill with a matching provider/slug key is already installed.
        # Use exact key match only — no suffix matching to avoid false positives
        # (e.g. "my-team/docker" matching a remote "docker" skill).
        slug = _slugify(r.id or r.name)
        expected_key = f"{r.provider}/{slug}" if slug else ""
        installed = r.installed or (expected_key and expected_key in local_keys)
        # All provider-sourced fields are attacker-controllable -- redact
        # before surfacing. Benign ids (owner/repo/slug) pass unchanged;
        # an id that trips the credential/exfiltration scanners would only
        # break install for that (malicious) entry, which is acceptable.
        items.append({
            "id": _redact_external(r.id),
            "name": _redact_external(r.name),
            "description": _redact_external(r.description),
            "provider": r.provider,
            "display_provider": _display_name(registry, r.provider),
            "repo_url": _redact_external(r.repo_url),
            "author": _redact_external(r.author),
            "installed": installed,
            # Defense-in-depth: providers should hand back a list[str] (see
            # SkillsShProvider.search), but a non-list/None or non-string tag
            # from any provider must not TypeError here and 500 the whole
            # search response for every provider.
            "tags": [
                _redact_external(t)
                for t in (r.tags if isinstance(r.tags, list) else [])
                if isinstance(t, str)
            ],
            "installs": r.installs,
        })

    active_providers = [p.name for p in registry.available_providers]

    _sel().log_tool_invocation(
        session_key=request.get("session_key", "dashboard"),
        tool_name="discover_skills",
        tool_kind="skill_provider_search",
        outcome="success",
        metadata={
            "query": query,
            "provider_filter": provider_filter or "all",
            "result_count": str(len(items)),
        },
    )
    return web.json_response({"results": items, "providers": active_providers})


async def api_skills_discover_install(request: web.Request) -> web.Response:
    """POST /api/skills/-/discover/install — install a skill from a provider.

    Request body:
    {
      "provider": "skillsh",
      "skill_id": "my-awesome-skill",
      "name": "optional-custom-slug"
    }

    Fetches the SKILL.md content from the provider and writes it to the
    local skills directory. Returns the installed skill's key.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    # Shape validation: valid JSON like `[]` has no .get(), and a non-string
    # field ({"provider": 1}) has no .strip() — either would 500. 400 instead.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "Request body must be a JSON object"}, status=400
        )
    for _field in ("provider", "skill_id", "name"):
        if not isinstance(body.get(_field, ""), str) and body.get(_field) is not None:
            return web.json_response(
                {"error": f"'{_field}' must be a string"}, status=400
            )

    provider_name = (body.get("provider") or "").strip()
    skill_id = (body.get("skill_id") or "").strip()
    custom_name = (body.get("name") or "").strip()
    # overwrite gates a destructive rmtree of an existing install, so demand a
    # real JSON boolean: bool("false") is True in Python, which would turn an
    # explicitly false-like value into consent to delete local edits.
    overwrite_raw = body.get("overwrite", False)
    if not isinstance(overwrite_raw, bool):
        return web.json_response(
            {"error": "'overwrite' must be a boolean"}, status=400
        )
    overwrite = overwrite_raw

    if not provider_name or not skill_id:
        return web.json_response(
            {"error": "Both 'provider' and 'skill_id' are required"}, status=400
        )

    registry = _get_registry()
    provider = registry.get(provider_name)
    if provider is None or not provider.is_available():
        return web.json_response(
            {"error": f"Provider '{provider_name}' is not available"}, status=404
        )

    # Determine the local slug for the installed skill.
    slug = _slugify(custom_name or skill_id)
    if not slug or not _SAFE_SLUG_RE.match(slug):
        return web.json_response(
            {"error": f"Cannot derive safe slug from '{skill_id}'"}, status=400
        )

    # Conflict check BEFORE the (slow) provider fetch: if the target skill
    # already exists locally, require an explicit overwrite flag so the UI
    # can prompt the user instead of silently clobbering local edits.
    key = f"{provider_name}/{slug}"
    state = request.app["state"]
    skills = _get_skills(state)
    existing_dir = _skills_dir() / key

    def _check_exists() -> bool:
        return existing_dir.exists() or skills.load_skill(key) is not None

    already_exists = await asyncio.to_thread(_check_exists)
    if already_exists and not overwrite:
        # Permission decision: refuse to clobber an existing install
        # without explicit user consent -- audit the denial.
        _sel().log_tool_invocation(
            session_key=request.get("session_key", "dashboard"),
            tool_name="install_skill_from_provider",
            tool_kind="skill_provider_install",
            outcome="denied",
            downstream_service=provider_name,
            resources=f"key={key}",
            error="already_installed_no_overwrite",
        )
        return web.json_response(
            {
                "error": f"Skill '{key}' is already installed",
                "code": "exists",
                "key": key,
            },
            status=409,
        )

    # Fetch full bundle from the provider (with timeout).
    # Bundle = all files (SKILL.md + rules/ + scripts/ etc.); falls back to single-file.
    bundle: list[tuple[str, str]] | None = None
    content: str | None = None
    try:
        if hasattr(provider, "fetch_skill_bundle"):
            bundle = await asyncio.wait_for(
                provider.fetch_skill_bundle(skill_id), timeout=15.0
            )
        if bundle is None:
            content = await asyncio.wait_for(
                provider.fetch_skill_content(skill_id), timeout=15.0
            )
    except asyncio.TimeoutError:
        logger.warning("Timeout fetching skill %s from %s", skill_id, provider_name)
        _sel().log_tool_invocation(
            session_key=request.get("session_key", "dashboard"),
            tool_name="install_skill_from_provider",
            tool_kind="skill_provider_install",
            outcome="error",
            downstream_service=provider_name,
            error="timeout",
        )
        return web.json_response({"error": "Fetch timed out"}, status=504)
    except Exception as exc:
        scrubbed, _ = redact_credentials(str(exc))
        scrubbed, _ = redact_exfiltration_urls(scrubbed)
        logger.warning("Failed to fetch skill %s from %s: %s", skill_id, provider_name, scrubbed)
        _sel().log_tool_invocation(
            session_key=request.get("session_key", "dashboard"),
            tool_name="install_skill_from_provider",
            tool_kind="skill_provider_install",
            outcome="error",
            downstream_service=provider_name,
            error=scrubbed,
        )
        return web.json_response({"error": "Failed to fetch skill from provider"}, status=502)

    if not bundle and not content:
        return web.json_response(
            {"error": f"Skill '{skill_id}' not found or empty on {provider_name}"}, status=404
        )

    # Size guard: total bundle must not exceed 5 MiB.
    max_bundle_size = 5 * 1024 * 1024
    if bundle:
        total_size = sum(len(c.encode("utf-8")) for _, c in bundle)
        if total_size > max_bundle_size:
            return web.json_response(
                {"error": "Skill bundle exceeds size limit (5 MiB)"}, status=413
            )
    elif content and len(content.encode("utf-8")) > max_bundle_size:
        return web.json_response(
            {"error": "Skill content exceeds size limit"}, status=413
        )

    # Write to local skills directory.
    file_count = 0
    if bundle:
        # Write full bundle: all files preserved in directory structure.
        # Disk I/O runs off-loop — a 76-file bundle would otherwise block
        # the event loop for the whole write.
        skill_dir = _skills_dir() / key

        def _write_bundle() -> int:
            skills_root = _skills_dir().resolve()
            # Containment must be validated BEFORE any destructive step (the
            # unlink/rmtree below), not only before the writes: a symlinked
            # PARENT (provider) directory makes skill_dir.exists() traverse
            # the link, so a late check would let rmtree delete at the
            # symlink target. Resolve the parent chain and re-append the leaf
            # name (deliberately NOT resolving the leaf — a leaf symlink is
            # handled by unlink, never followed).
            candidate = skill_dir.parent.resolve() / skill_dir.name
            try:
                candidate.relative_to(skills_root)
            except ValueError:
                logger.warning(
                    "Refusing bundle install outside skills root: %s", skill_dir
                )
                return 0
            # Symlink defense: if the skill dir itself is a symlink, every
            # containment check below resolves against the symlink TARGET, so
            # a pre-planted link would redirect the whole bundle write outside
            # the skills root (nested rel_paths traverse it via mkdir, and the
            # parent-symlink guard below misses not-yet-existing parents).
            # Remove the link itself — never follow it.
            if skill_dir.is_symlink():
                logger.warning("Replacing symlinked skill dir: %s", skill_dir)
                skill_dir.unlink()
            # Overwrite semantics: clear the previous install first so stale
            # files from an older bundle version don't linger. The user
            # explicitly consented via the 409 -> overwrite flow.
            if overwrite and skill_dir.exists():
                shutil.rmtree(skill_dir)
            skill_dir.mkdir(parents=True, exist_ok=True)
            resolved_root = skill_dir.resolve()
            # Belt-and-suspenders: the (now symlink-free) skill dir must
            # itself land under the canonical skills root.
            try:
                resolved_root.relative_to(skills_root)
            except ValueError:
                logger.warning(
                    "Refusing bundle write outside skills root: %s", skill_dir
                )
                return 0
            written = 0
            for rel_path, file_content in bundle:
                if ".." in rel_path or rel_path.startswith("/") or rel_path.startswith("./.."):
                    continue
                file_path = skill_dir / rel_path
                # Path traversal defense: resolve and verify containment
                try:
                    file_path.resolve().relative_to(resolved_root)
                except ValueError:
                    logger.warning("Skipping traversal path in bundle: %s", rel_path)
                    continue
                # Reject symlinks in parent chain
                if file_path.parent.exists() and file_path.parent.is_symlink():
                    logger.warning("Skipping symlink parent in bundle: %s", rel_path)
                    continue
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(file_content, encoding="utf-8")
                written += 1
            # Ensure SKILL.md exists (loader requires it for discovery).
            # If only AGENTS.md was provided, copy it as SKILL.md.
            if not (skill_dir / "SKILL.md").exists() and (skill_dir / "AGENTS.md").exists():
                (skill_dir / "SKILL.md").write_text(
                    (skill_dir / "AGENTS.md").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            return written

        file_count = await asyncio.to_thread(_write_bundle)
        # Invalidate the loader's cache so the skill is immediately discoverable.
        skills._invalidate_iter_cache()
        kind = "updated" if already_exists else "created"
        logger.info("Installed skill bundle %s: %d files", key, file_count)
    elif already_exists:
        await asyncio.to_thread(skills.update_skill, key, content)
        kind = "updated"
        file_count = 1
    else:
        created = await asyncio.to_thread(skills.create_skill, key, content)
        if not created:
            return web.json_response(
                {"error": f"Failed to create skill at '{key}'"}, status=500
            )
        kind = "created"
        file_count = 1

    _sel().log_tool_invocation(
        session_key=request.get("session_key", "dashboard"),
        tool_name="install_skill_from_provider",
        tool_kind="skill_provider_install",
        outcome="success",
        downstream_service=provider_name,
        resources=f"key={key}",
        metadata={"kind": kind, "skill_id": skill_id},
    )
    return web.json_response({
        "ok": True,
        "key": key,
        "slug": slug,
        "provider": provider_name,
        "kind": kind,
        "file_count": file_count,
    })


def _slugify(raw: str) -> str:
    """Convert a name to a filesystem-safe slug."""
    if not raw:
        return ""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip()).strip("-").lower()[:64].rstrip("-")
    return slug


def _display_name(registry: ProviderRegistry, provider_name: str) -> str:
    """Get the human-readable display name for a provider."""
    p = registry.get(provider_name)
    return p.display_name if p else provider_name


async def api_skills_discover_preview(request: web.Request) -> web.Response:
    """GET /api/skills/-/discover/preview?provider=<name>&id=<skill_id>

    Fetches the SKILL.md (frontmatter + full body) and the bundle file
    list for a skill without installing it. Used by the detail panel to
    show the real summary, the full skill body, and what would be
    installed.

    Response shape:
    {
      "description": "React and Next.js performance optimization...",
      "name": "vercel-react-best-practices",
      "license": "MIT",
      "author": "vercel",
      "content": "<full SKILL.md markdown>",
      "files": ["SKILL.md", "rules/react.md", ...],
      "file_count": 76
    }
    """
    provider_name = request.query.get("provider", "").strip()
    skill_id = request.query.get("id", "").strip()

    if not provider_name or not skill_id:
        return web.json_response(
            {"error": "Both 'provider' and 'id' are required"}, status=400
        )

    registry = _get_registry()
    provider = registry.get(provider_name)
    if provider is None or not provider.is_available():
        return web.json_response(
            {"error": f"Provider '{provider_name}' is not available"}, status=404
        )

    _empty = {"description": "", "name": "", "content": "", "files": [], "file_count": 0}

    # Prefer the bundle fetch: one request yields both the SKILL.md content
    # and the full file manifest for the detail panel.
    content: str | None = None
    files: list[str] = []
    try:
        if hasattr(provider, "fetch_skill_bundle"):
            bundle = await asyncio.wait_for(
                provider.fetch_skill_bundle(skill_id), timeout=10.0
            )
            if bundle:
                files = [p for p, _ in bundle]
                skill_md = next((c for p, c in bundle if p == "SKILL.md"), None)
                content = skill_md or next(
                    (c for p, c in bundle if p.endswith(".md")), None
                )
        if content is None:
            content = await asyncio.wait_for(
                provider.fetch_skill_content(skill_id), timeout=10.0
            )
    except (asyncio.TimeoutError, Exception):
        _sel().log_tool_invocation(
            session_key=request.get("session_key", "dashboard"),
            tool_name="preview_skill_from_provider",
            tool_kind="skill_provider_preview",
            outcome="error",
            downstream_service=provider_name,
            error="fetch_failed",
        )
        return web.json_response(_empty)

    if not content:
        _sel().log_tool_invocation(
            session_key=request.get("session_key", "dashboard"),
            tool_name="preview_skill_from_provider",
            tool_kind="skill_provider_preview",
            outcome="error",
            downstream_service=provider_name,
            error="empty_content",
        )
        return web.json_response(_empty)

    # Parse YAML frontmatter to extract description
    meta = _parse_frontmatter(content)
    _sel().log_tool_invocation(
        session_key=request.get("session_key", "dashboard"),
        tool_name="preview_skill_from_provider",
        tool_kind="skill_provider_preview",
        outcome="success",
        downstream_service=provider_name,
        metadata={"skill_id": skill_id, "file_count": str(len(files))},
    )
    # Cap preview content to keep the response light; the full bundle is
    # still installed intact regardless of this display cap.
    max_preview = 64 * 1024
    # Provider-sourced fields (including the full SKILL.md body) are
    # attacker-controllable -- redact before returning to the dashboard.
    return web.json_response({
        "description": _redact_external(meta.get("description", "")),
        "name": _redact_external(meta.get("name", "")),
        "license": _redact_external(meta.get("license", "")),
        "author": _redact_external(meta.get("author", "")),
        "content": _redact_external(content[:max_preview]),
        "files": [_redact_external(f) for f in files[:200]],
        "file_count": len(files),
    })


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter key-value pairs from a SKILL.md."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    frontmatter = content[4:end]
    result: dict = {}
    for line in frontmatter.split("\n"):
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result
