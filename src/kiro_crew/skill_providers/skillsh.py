"""skills.sh provider — public skill registry search and fetch.

skills.sh exposes a public REST API (no auth for reads) that returns
skill metadata including GitHub repo URLs. Installation reads the skill's
files out of the registry's own download bundle (``fetch_skill_bundle``).

The SSRF screen, the redirect allowlist and the bounded body read all live in
``_http`` and are shared with every other provider; the names re-exported below
are thin bindings of this provider's own allowlist and audit label onto that one
implementation.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

from kiro_crew.skill_providers import _http
from kiro_crew.skill_providers.base import SkillSearchResult

logger = logging.getLogger(__name__)

# skills.sh API base (no trailing slash)
_API_BASE = "https://skills.sh/api"

# Timeout for HTTP requests (seconds) — see ``_http.TIMEOUT_SECS``.
_TIMEOUT = _http.TIMEOUT_SECS

# User-Agent for our requests (good citizenship)
_USER_AGENT = _http.USER_AGENT

# Maximum response body size (1 MiB). Rationale in ``_http.MAX_RESPONSE_BYTES``.
_MAX_RESPONSE_BYTES = _http.MAX_RESPONSE_BYTES

# Per-chunk read size while draining a response body (64 KiB).
_HTTP_READ_CHUNK_BYTES = _http.READ_CHUNK_BYTES


def _s(v: Any) -> str:
    """Coerce one provider-supplied value to str — non-strings become ''.

    skills.sh rows are external input, and a non-string that survives into a
    ``SkillSearchResult`` crashes a consumer far from here: a numeric ``id``
    reaches ``_slugify``'s ``raw.strip()`` in the discover handler and 500s the
    request. Coercing to '' is what lets one falsiness test at the call site
    drop the row; this helper never drops anything itself.
    """
    return v if isinstance(v, str) else ""


@dataclass
class SkillsShConfig:
    """Configuration for the skills.sh provider."""

    enabled: bool = True

    # This module's SSRF host allowlist does not reach the base: `_is_allowed_host`
    # gates redirect targets only, so an initial URL built from this field is
    # checked by `_is_internal_url` alone. That rejects internal, private and
    # loopback addresses, but it does not require HTTPS and does not hold the host
    # to `_ALLOWED_HOSTS`. Any caller that lets a user set this must validate the
    # base URL itself before constructing the provider. The platform `discovery`
    # policy allowlist that `api_base` below feeds is a separate, policy-level gate.
    api_base: str = _API_BASE


class SkillsShProvider:
    """Provider that searches and fetches skills from skills.sh."""

    def __init__(self, config: SkillsShConfig | None = None) -> None:
        self._config = config or SkillsShConfig()

    @property
    def api_base(self) -> str:
        """The registry base URL this provider fetches from.

        Public because the platform ``discovery`` policy allowlists a registry by
        URL rather than by name: the name is a self-chosen label, while the base
        URL is what determines where skill content comes from.
        """
        return self._config.api_base

    @property
    def name(self) -> str:
        return "skillsh"

    @property
    def display_name(self) -> str:
        return "skills.sh"

    def is_available(self) -> bool:
        return self._config.enabled

    async def search(self, query: str, *, limit: int = 20) -> list[SkillSearchResult]:
        """Search skills.sh catalog via their public API."""
        if not query.strip():
            return []

        url = f"{self._config.api_base}/search?q={urllib.parse.quote(query)}&limit={limit}"
        data = await _fetch_json(url)
        if data is None:
            return []

        # skills.sh returns {"skills": [...]} or a flat list — handle both.
        # Guard the scalar case too: a JSON string/number body ("maintenance",
        # an error page) is not a list and has no .get() (AttributeError), and
        # {"skills": null} yields a non-list -> items[:limit] raises TypeError.
        # Either way ProviderRegistry.search would swallow it and silently zero
        # ALL skillsh results. Coerce anything that isn't a list to [] instead.
        raw_items = data.get("skills") if isinstance(data, dict) else data
        items: list[Any] = raw_items if isinstance(raw_items, list) else []
        results: list[SkillSearchResult] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            # skills.sh search response shape:
            # {"id": "owner/repo/skill-name", "skillId": "skill-name",
            #  "name": "skill-name", "installs": N, "source": "owner/repo"}
            source = _s(item.get("source"))
            repo_url = f"https://github.com/{source}" if source else ""
            try:
                installs = int(item.get("installs", 0) or 0)
            except (TypeError, ValueError):
                installs = 0
            skill_ident = _s(item.get("id")) or _s(item.get("skillId")) or _s(item.get("name"))
            if not skill_ident:
                continue  # entry without a usable string identifier — drop it
            # A non-string tag reaches the discover handler's per-field
            # redactor and 500s the whole response, so drop it here.
            raw_tags = item.get("tags", [])
            tags = [t for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
            results.append(
                SkillSearchResult(
                    id=skill_ident,
                    name=_s(item.get("name")) or _s(item.get("skillId")),
                    description=_s(item.get("description")),
                    provider=self.name,
                    repo_url=repo_url,
                    # `source` is the registry's "owner/repo" identifier, not a
                    # filesystem path, so the separator is always "/". Take the
                    # owner segment without building the whole list.
                    author=source.partition("/")[0],
                    tags=tags,
                    installs=installs,
                )
            )
        return results

    async def fetch_skill_content(self, skill_id: str) -> str | None:
        """Fetch the SKILL.md content for a skill via skills.sh download API.

        Uses GET /api/download/{id} which returns a JSON bundle with all
        skill files. We extract SKILL.md (or AGENTS.md as fallback) from
        the bundle. For full bundle installation, use fetch_skill_bundle().
        """
        bundle = await self.fetch_skill_bundle(skill_id)
        if bundle is None:
            return None

        # SKILL.md first, then AGENTS.md, then any .md. First match in bundle
        # order wins at each tier, so a bundle carrying two SKILL.md entries
        # resolves deterministically to the earlier one.
        for wanted in ("SKILL.md", "AGENTS.md"):
            named = next((f for f in bundle if f[0] == wanted), None)
            if named:
                return named[1]
        any_md = next((f for f in bundle if f[0].endswith(".md")), None)
        return any_md[1] if any_md else None

    async def fetch_skill_bundle(self, skill_id: str) -> list[tuple[str, str]] | None:
        """Fetch the full skill bundle (all files) from skills.sh.

        Returns a list of (relative_path, content) tuples, or None on failure.
        Uses GET /api/download/{id} which returns all skill files.
        """
        # The skills.sh id is an "owner/repo/skill" path whose slashes are real
        # path segments. The download route is /api/download/{owner}/{repo}/{skill},
        # so the slashes MUST survive into the URL (safe="/"). Encoding them
        # (safe="") collapses the id into a single segment, misses the API route,
        # and skills.sh returns its HTML SPA page instead of the JSON bundle, so
        # the install surfaces as "not found or empty on skillsh". We still block
        # traversal and smuggling: reject any empty, "." or ".." segment (which
        # also covers a leading, trailing, or doubled slash), and quote() keeps
        # encoding "?", "#", space, and similar so a query string cannot be
        # smuggled in.
        if not skill_id or any(seg in ("", ".", "..") for seg in skill_id.split("/")):
            logger.debug("Rejecting malformed skill_id for download: %r", skill_id)
            return None
        url = f"{self._config.api_base}/download/{urllib.parse.quote(skill_id, safe='/')}"
        data = await _fetch_json(url)
        # skills.sh is untrusted external input: an error/maintenance payload (or
        # a CDN interposing its SPA HTML) can be valid JSON that is not an object,
        # so guard the shape before .get() — a bare data.get() on a list/str/number
        # raises AttributeError and the caller converts a clean not-found into a
        # misleading 502 + spurious error audit. Mirrors the isinstance guard in
        # search() above.
        if not isinstance(data, dict):
            return None

        files = data.get("files")
        if not isinstance(files, list) or not files:
            return None

        result: list[tuple[str, str]] = []
        for f in files:
            # Each entry and its path/contents are attacker-controllable. A
            # non-dict entry crashes at f.get(); a truthy non-string contents
            # (e.g. a JSON number) survives the checks below and later blows up
            # the install handler's `c.encode("utf-8")` with AttributeError; a
            # non-string path raises TypeError at the `".." in path` check.
            # Coerce/drop instead of trusting, matching search()'s idiom.
            if not isinstance(f, dict):
                continue
            path = f.get("path", "")
            contents = f.get("contents", "")
            if not isinstance(path, str) or not isinstance(contents, str):
                continue
            if not path or not contents:
                continue
            # Skip paths with traversal attempts
            if ".." in path or path.startswith("/"):
                continue
            result.append((path, contents))

        return result if result else None


async def _fetch_json(url: str) -> Any | None:
    """Fetch JSON from a URL. Returns None on any failure.

    Calls the module-global ``_sync_fetch_json`` so a test may patch this
    provider's fetch without reaching into ``_http``.
    """
    return await _http.run_off_loop(lambda: _sync_fetch_json(url))


def _sync_fetch_json(url: str) -> Any | None:
    """Synchronous JSON fetch (for the executor), bounded and SSRF-screened."""
    return _http.sync_fetch_json(
        url,
        allowed_hosts=_ALLOWED_HOSTS,
        internal_check=_is_internal_url,
        headers={"User-Agent": _USER_AGENT},
        max_bytes=_MAX_RESPONSE_BYTES,
    )


def _audit_ssrf_blocked(url: str, host: str, canonical_host: str) -> None:
    """Emit a SEL audit event for a blocked SSRF-to-internal-IP attempt here.

    A separate function rather than a direct ``_http.audit_ssrf_blocked``
    reference so this provider's audit label is fixed in one place and so a test
    can observe the guard firing by patching this name.
    """
    _http.audit_ssrf_blocked("skillsh", url, host, canonical_host)


def _is_internal_url(url: str) -> bool:
    """This provider's binding of the shared internal-address screen.

    Reads ``_audit_ssrf_blocked`` from the module globals at call time, so
    patching that name observes the guard.
    """
    return _http.is_internal_url(url, audit=_audit_ssrf_blocked)


# Hosts a fetch may be REDIRECTED to; the initial URL is checked by
# `_is_internal_url` alone (see `SkillsShConfig.api_base`). Every request this
# module makes starts at the configured skills.sh API base, so the GitHub hosts
# are here only as redirect targets of the download endpoint, which serves
# bundle payloads from GitHub's raw, media and objects CDNs. A redirect to ANY
# other host —
# including an internal DNS name that would resolve to a private address
# (DNS-rebinding style SSRF) — is refused. Keep this list tight: add hosts
# only for a concrete, observed redirect target.
_ALLOWED_HOSTS = frozenset(
    {
        "skills.sh",
        "www.skills.sh",
        "github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "media.githubusercontent.com",
        "codeload.github.com",
    }
)


def _is_allowed_host(url: str) -> bool:
    """True iff *url* is HTTPS on a host this provider may be redirected to."""
    return _http.is_allowed_host(url, _ALLOWED_HOSTS)


def _open_no_internal_redirect(req):  # type: ignore[no-untyped-def]
    """Open a request with redirects held to ``_ALLOWED_HOSTS``. None if blocked."""
    return _http.open_guarded(
        req, allowed_hosts=_ALLOWED_HOSTS, internal_check=_is_internal_url
    )


def _read_bounded(resp, max_bytes: int) -> bytes | None:
    """Read a response body up to *max_bytes*. None if exceeded."""
    return _http.read_bounded(resp, max_bytes)
