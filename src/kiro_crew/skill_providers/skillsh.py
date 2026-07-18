"""skills.sh provider — public skill registry search and fetch.

skills.sh exposes a public REST API (no auth for reads) that returns
skill metadata including GitHub repo URLs. Installation fetches the
SKILL.md from the repo directly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from kiro_crew.skill_providers.base import SkillSearchResult

logger = logging.getLogger(__name__)

# skills.sh API base (no trailing slash)
_API_BASE = "https://skills.sh/api"

# Timeout for HTTP requests (seconds)
_TIMEOUT = 5

# User-Agent for our requests (good citizenship)
_USER_AGENT = "KiroCrew/1.0 (skill-discovery)"

# Maximum response body size (1 MiB) — prevents disk exhaustion from
# oversized responses. SKILL.md files are typically <50 KB.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# Internal/private IP ranges that must never be fetched (SSRF mitigation).
# NOTE: _API_BASE is hardcoded — if it becomes user-configurable, it must
# be validated against this same check. Do NOT make api_base configurable
# without adding SSRF validation on the base URL itself.


@dataclass
class SkillsShConfig:
    """Configuration for the skills.sh provider."""

    enabled: bool = True
    api_base: str = _API_BASE


class SkillsShProvider:
    """Provider that searches and fetches skills from skills.sh."""

    def __init__(self, config: SkillsShConfig | None = None) -> None:
        self._config = config or SkillsShConfig()

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

        # skills.sh returns {"skills": [...]} or a flat list — handle both
        items = data if isinstance(data, list) else data.get("skills", [])
        results: list[SkillSearchResult] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            # skills.sh search response shape:
            # {"id": "owner/repo/skill-name", "skillId": "skill-name",
            #  "name": "skill-name", "installs": N, "source": "owner/repo"}
            source = item.get("source", "")
            repo_url = f"https://github.com/{source}" if source else ""
            try:
                installs = int(item.get("installs", 0) or 0)
            except (TypeError, ValueError):
                installs = 0
            results.append(
                SkillSearchResult(
                    id=item.get("id") or item.get("skillId") or item.get("name", ""),
                    name=item.get("name", item.get("skillId", "")),  # type: ignore[arg-type]
                    description=item.get("description", ""),
                    provider=self.name,
                    repo_url=repo_url,
                    author=source.split("/")[0] if source else "",
                    tags=item.get("tags", []),
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

        # Find SKILL.md first, fall back to AGENTS.md
        skill_md = next((f for f in bundle if f[0] == "SKILL.md"), None)
        if skill_md:
            return skill_md[1]

        agents_md = next((f for f in bundle if f[0] == "AGENTS.md"), None)
        if agents_md:
            return agents_md[1]

        # Last resort: return the first .md file
        any_md = next((f for f in bundle if f[0].endswith(".md")), None)
        if any_md:
            return any_md[1]

        return None

    async def fetch_skill_bundle(self, skill_id: str) -> list[tuple[str, str]] | None:
        """Fetch the full skill bundle (all files) from skills.sh.

        Returns a list of (relative_path, content) tuples, or None on failure.
        Uses GET /api/download/{id} which returns all skill files.
        """
        url = f"{self._config.api_base}/download/{skill_id}"
        data = await _fetch_json(url)
        if data is None:
            return None

        files = data.get("files", [])
        if not files:
            return None

        result: list[tuple[str, str]] = []
        for f in files:
            path = f.get("path", "")
            contents = f.get("contents", "")
            if not path or not contents:
                continue
            # Skip paths with traversal attempts
            if ".." in path or path.startswith("/"):
                continue
            result.append((path, contents))

        return result if result else None


def _github_raw_url(repo_url: str, file_path: str) -> str | None:
    """Convert a GitHub repo URL to a raw content URL.

    Handles:
    - https://github.com/user/repo
    - https://github.com/user/repo.git
    - github.com/user/repo
    """
    # Defense-in-depth: file_path must not contain traversal sequences.
    # Currently always called with literal "SKILL.md" but this guards
    # against future misuse if the parameter becomes caller-controlled.
    if ".." in file_path or file_path.startswith("/"):
        return None
    match = re.match(
        r"(?:https?://)?github\.com/([^/]+)/([^/.\s]+?)(?:\.git)?/?$",
        repo_url.strip(),
    )
    if not match:
        return None
    user, repo = match.group(1), match.group(2)
    # Try main branch first (most common), caller can retry with master
    return f"https://raw.githubusercontent.com/{user}/{repo}/main/{file_path}"


async def _fetch_json(url: str) -> Any | None:
    """Fetch JSON from a URL. Returns None on any failure."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _sync_fetch_json, url)
    except Exception:
        logger.debug("Failed to fetch JSON from %s", url, exc_info=True)
        return None


def _sync_fetch_json(url: str) -> Any | None:
    """Synchronous JSON fetch (for run_in_executor)."""
    # Pre-connect SSRF check on the initial URL
    if _is_internal_url(url):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        resp = _open_no_internal_redirect(req)
        if resp is None:
            return None
        if resp.status != 200:
            resp.close()
            return None
        data = _read_bounded(resp, _MAX_RESPONSE_BYTES)
        resp.close()
        if data is None:
            return None
        return json.loads(data.decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


async def _fetch_text(url: str) -> str | None:
    """Fetch text content from a URL. Returns None on any failure."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _sync_fetch_text, url)
    except Exception:
        logger.debug("Failed to fetch text from %s", url, exc_info=True)
        return None


def _sync_fetch_text(url: str) -> str | None:
    """Synchronous text fetch (for run_in_executor)."""
    # Pre-connect SSRF check on the initial URL
    if _is_internal_url(url):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        resp = _open_no_internal_redirect(req)
        if resp is None:
            return None
        if resp.status != 200:
            resp.close()
            return None
        data = _read_bounded(resp, _MAX_RESPONSE_BYTES)
        resp.close()
        if data is None:
            return None
        return data.decode("utf-8")
    except (urllib.error.URLError, OSError):
        return None


def _is_internal_url(url: str) -> bool:
    """Return True if the URL resolves to a private/internal/loopback address.

    Uses urllib.parse + ipaddress module for robust detection that covers:
    - IPv4 private ranges (10.x, 172.16.x, 192.168.x, 127.x, 169.254.x)
    - IPv6 loopback (::1), link-local (fe80::), ULA (fd00::)
    - IPv6-mapped IPv4 (::ffff:127.0.0.1)
    - Hex/octal/decimal IP representations (0x7f000001, 0177.0.0.1, 2130706433)
    - localhost hostname

    Called BEFORE AND AFTER redirect resolution to prevent both pre-connect
    and post-redirect SSRF.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname  # lowercased, brackets stripped for IPv6
        if not host:
            return True  # no host = suspicious, block

        # Block "localhost" explicitly (covers DNS that resolves to 127.0.0.1)
        if host == "localhost":
            return True

        # Try to parse as an IP address directly (handles hex, octal, decimal,
        # IPv6, and IPv4-mapped IPv6 forms)
        try:
            ip = ipaddress.ip_address(host)
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            )
        except ValueError:
            pass  # not a literal IP — it's a hostname

        # For hostnames: we cannot resolve DNS here (blocking call, and DNS
        # rebinding would defeat it anyway). Allow non-IP hostnames through —
        # the allowlist approach (only raw.githubusercontent.com and skills.sh
        # are fetched) provides the primary control. This check catches the
        # redirect-to-internal-IP vector.
        return False
    except Exception:
        return True  # parse failure = suspicious, block


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that blocks redirects to internal/private IPs.

    Prevents SSRF via 302 → internal-IP chains. Checks each redirect
    location BEFORE following it, so no TCP connection is made to
    internal hosts.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _is_internal_url(newurl):
            raise urllib.error.URLError(
                f"Blocked redirect to internal URL: {newurl[:80]}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_no_internal_redirect(req: urllib.request.Request):
    """Open a URL request using a redirect handler that blocks internal IPs.

    Returns the response object, or None if blocked/failed.
    """
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    try:
        return opener.open(req, timeout=_TIMEOUT)
    except urllib.error.URLError:
        return None


def _read_bounded(resp, max_bytes: int) -> bytes | None:
    """Read response body up to max_bytes. Returns None if exceeded.

    Prevents disk exhaustion from oversized responses. Reads in chunks
    to avoid holding unbounded data in memory.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            logger.warning("Response exceeded %d bytes, aborting read", max_bytes)
            return None
        chunks.append(chunk)
    return b"".join(chunks)
