"""skills.sh provider — public skill registry search and fetch.

skills.sh exposes a public REST API (no auth for reads) that returns
skill metadata including GitHub repo URLs. Installation reads the skill's
files out of the registry's own download bundle (``fetch_skill_bundle``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from kiro_crew.security import canonicalize_ip
from kiro_crew.skill_providers.base import (
    SkillBadFormat,
    SkillFetchError,
    SkillNotFound,
    SkillRateLimited,
    SkillSearchResult,
    SkillTooLarge,
    SkillUnreachable,
    SkillUpstreamStatus,
)

logger = logging.getLogger(__name__)

# skills.sh API base (no trailing slash)
_API_BASE = "https://skills.sh/api"

# Timeout for HTTP requests (seconds)
_TIMEOUT = 5

# User-Agent for our requests (good citizenship)
_USER_AGENT = "KiroCrew/1.0 (skill-discovery)"

# Maximum SEARCH response body size (1 MiB) — ``_read_bounded`` accumulates the
# body in memory, so this bounds the bytes one fetch RETAINS. It is not a
# peak-memory figure: joining the chunks and decoding them each allocate another
# copy. A search page is a few KB per row; a 1 MiB response is already an
# anomaly.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# Maximum DOWNLOAD (bundle) body size (10 MiB). A bundle carries every file of a
# skill as JSON, and real public bundles (a slide-deck skill with its assets)
# run past 2 MiB, so the search budget cannot serve it. ``_read_bounded`` still
# aborts mid-stream the moment the running total crosses this, so no more than
# this is ever retained. The install handler's total-size guard
# (``discover.py``) is set to the same figure: the two are one budget, applied
# once on the wire and once on the decoded files.
_MAX_BUNDLE_BYTES = 10 * 1024 * 1024

# Per-chunk read size while draining a response body (64 KiB).
_HTTP_READ_CHUNK_BYTES = 64 * 1024


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
        try:
            data = await _fetch_json(url, _MAX_RESPONSE_BYTES)
        except SkillFetchError:
            # A search is best-effort fan-out: a failing registry contributes
            # no rows rather than failing the aggregate.
            logger.debug("skills.sh search failed", exc_info=True)
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
        Raises the same :class:`SkillFetchError` family as
        ``fetch_skill_bundle``; ``None`` means the bundle holds no markdown.
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

        Returns a list of (relative_path, content) tuples. Raises a
        :class:`SkillFetchError` subclass for every DEFINITIVE failure — the
        bundle is above ``_MAX_BUNDLE_BYTES`` (``SkillTooLarge``, with the size),
        the registry answered 404 (``SkillNotFound``), 429
        (``SkillRateLimited``) or another non-2xx (``SkillUpstreamStatus``),
        the body was not a JSON bundle (``SkillBadFormat``), or no response
        could be obtained at all (``SkillUnreachable``). Returns ``None`` only
        for a malformed id, which no request is made for. Uses
        GET /api/download/{id} which returns all skill files.
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
        try:
            data = await _fetch_json(url, _MAX_BUNDLE_BYTES)
        except SkillNotFound:
            # Re-raised with the id the caller asked for, so the message names
            # the skill rather than the URL.
            raise SkillNotFound(skill_id, self.display_name) from None
        # skills.sh is untrusted external input: an error/maintenance payload (or
        # a CDN interposing its SPA HTML) can be valid JSON that is not an object,
        # so guard the shape before .get() — a bare data.get() on a list/str/number
        # raises AttributeError. Mirrors the isinstance guard in search() above.
        if not isinstance(data, dict):
            raise SkillBadFormat(self.display_name, "unexpected payload instead of a bundle")

        files = data.get("files")
        if not isinstance(files, list) or not files:
            raise SkillBadFormat(self.display_name, "empty bundle")

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

        if not result:
            raise SkillBadFormat(self.display_name, "bundle with no usable files")
        return result


async def _fetch_json(url: str, max_bytes: int) -> Any:
    """Fetch JSON from a URL off-loop, retaining at most *max_bytes* of body.

    Raises a :class:`SkillFetchError` subclass for every failure; never returns
    ``None`` for one. Anything else the worker raises (a bug, a cancelled
    executor) is reported as ``SkillUnreachable`` so callers see one family.
    """
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, _sync_fetch_json, url, max_bytes
        )
    except SkillFetchError:
        raise
    except Exception:
        logger.debug("Failed to fetch JSON from %s", url, exc_info=True)
        raise SkillUnreachable("skills.sh") from None


def _sync_fetch_json(url: str, max_bytes: int) -> Any:
    """Synchronous JSON fetch (for run_in_executor).

    Classifies the outcome instead of collapsing it: an SSRF-refused or
    unreachable URL, a non-2xx status (404 / 429 / other), a body over
    *max_bytes* (abandoned mid-stream, size reported from Content-Length when
    the registry sent one), and a non-JSON body each raise their own
    :class:`SkillFetchError`. Nothing from the remote body reaches a message.
    """
    provider = "skills.sh"
    # Pre-connect SSRF check on the initial URL
    if _is_internal_url(url):
        raise SkillUnreachable(provider)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        resp = _open_no_internal_redirect(req)
    except urllib.error.HTTPError as exc:
        # urlopen raises on 4xx/5xx; the status is the whole signal we keep.
        raise _status_error(exc.code, provider) from None
    if resp is None:
        raise SkillUnreachable(provider)
    try:
        if resp.status != 200:
            raise _status_error(resp.status, provider)
        declared = _declared_length(resp)
        if declared is not None and declared > max_bytes:
            # The registry told us up front; no need to read a byte of it.
            raise SkillTooLarge(declared, max_bytes)
        data, read = _read_bounded(resp, max_bytes)
    finally:
        resp.close()
    if data is None:
        # Aborted mid-stream past the budget. The bytes actually read are the
        # only trustworthy figure (a lower bound); the declared length is
        # untrusted and is used only for the pre-read refusal above.
        raise SkillTooLarge(read, max_bytes, aborted=True)
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SkillBadFormat(provider, "non-JSON response") from None


def _status_error(status: int, provider: str) -> SkillFetchError:
    if status == 404:
        return SkillNotFound("requested skill", provider)
    if status == 429:
        return SkillRateLimited(provider)
    return SkillUpstreamStatus(status, provider)


def _declared_length(resp) -> int | None:
    """Content-Length as an int, or None when absent or unparseable."""
    raw = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _audit_ssrf_blocked(url: str, host: str, canonical_host: str) -> None:
    """Emit a SEL audit event for a blocked SSRF-to-internal-IP attempt.

    Best-effort: a security event log failure must never turn the SSRF *defense*
    into a crash, so every error is swallowed. Imported lazily to avoid a
    module-load cycle (sel -> ... -> skill_providers).
    """
    try:
        from kiro_crew.sel import sel  # circular import: sel -> ... -> skill_providers

        detail = host if host == canonical_host else f"{host} -> {canonical_host}"
        sel().log_api_access(
            caller="skillsh",
            operation="ssrf_blocked",
            outcome="blocked",
            source="skill_provider",
            resources=f"{detail} ({url[:120]})",
        )
    except Exception:  # noqa: BLE001 — auditing must never break the guard
        logger.debug("SEL audit of blocked SSRF failed", exc_info=True)


def _is_internal_url(url: str) -> bool:
    """Return True if the URL resolves to a private/internal/loopback address.

    Uses urllib.parse + ipaddress module for robust detection that covers:
    - IPv4 private ranges (10.x, 172.16.x, 192.168.x, 127.x, 169.254.x)
    - IPv6 loopback (::1), link-local (fe80::), ULA (fd00::)
    - IPv6-mapped IPv4 (::ffff:127.0.0.1)
    - Hex/octal/decimal/short-form IP encodings (0x7f000001, 0177.0.0.1,
      2130706433, 127.1) — normalized via ``canonicalize_ip`` before parsing
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

        # Normalize alternate IPv4 encodings the OS resolver / libc inet_aton
        # accept but ipaddress.ip_address() rejects — hex (0x7f000001), octal
        # (0177.0.0.1), 32-bit decimal (2130706433), and short forms (127.1).
        # Without this, ip_address() raises ValueError on those, we fall through
        # to the hostname branch, and a redirect to e.g. http://2852039166/ (==
        # 169.254.169.254, the cloud instance metadata endpoint) is treated as
        # "not internal" — an SSRF-to-metadata credential-read bypass.
        # canonicalize_ip (security.py) is the same hardened resolver used by the
        # bash-command metadata gate; it returns the dotted-quad for any encoding,
        # or the input unchanged for a real hostname.
        canonical_host = canonicalize_ip(host)

        # Try to parse as an IP address directly (now covers hex/octal/decimal/
        # short forms via canonicalize_ip, plus IPv6 and IPv4-mapped IPv6).
        try:
            ip = ipaddress.ip_address(canonical_host)
            internal = (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            )
            if internal:
                # A URL naming an internal IP literal is a genuine SSRF attempt
                # (a legitimate skills.sh/github fetch never targets one). Emit a
                # SEL audit event so blocked attempts are visible to audit tooling
                # — especially the metadata-via-encoded-IP redirect vector this
                # guard closes. canonical_host may differ from host (e.g.
                # 0xa9fea9fe -> 169.254.169.254), so log both.
                _audit_ssrf_blocked(url, host, canonical_host)
            return internal
        except ValueError:
            pass  # not a literal IP — it's a hostname

        # For hostnames: we cannot resolve DNS here (blocking call, and DNS
        # rebinding would defeat it anyway). Non-IP hostnames pass THIS check;
        # the redirect handler below additionally enforces _ALLOWED_HOSTS, so a
        # redirect to an arbitrary DNS name that resolves to a private address
        # is blocked by allowlist rather than by resolution.
        return False
    except Exception:
        return True  # parse failure = suspicious, block


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
    """True iff *url* is HTTPS on an explicitly allowlisted host."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme == "https" and (parsed.hostname or "") in _ALLOWED_HOSTS
    except Exception:
        return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that only follows redirects to allowlisted HTTPS hosts.

    Prevents SSRF via 30x chains two ways: internal/private IP literals are
    rejected (_is_internal_url), and — because a hostname can't be safely
    resolved here (DNS rebinding) — any host outside _ALLOWED_HOSTS is
    rejected outright. Checks run BEFORE following, so no TCP connection is
    ever made to a disallowed target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _is_internal_url(newurl) or not _is_allowed_host(newurl):
            raise urllib.error.URLError(
                f"Blocked redirect to disallowed URL: {newurl[:80]}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_no_internal_redirect(req: urllib.request.Request):
    """Open a URL request using a redirect handler that blocks internal IPs.

    Returns the response object, or None if blocked/failed.
    """
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    try:
        return opener.open(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError:
        # A 4xx/5xx is a real answer from the registry; the caller classifies
        # it by status. Re-raised, never folded into the "no response" None.
        raise
    except urllib.error.URLError:
        return None


def _read_bounded(resp, max_bytes: int) -> tuple[bytes | None, int]:
    """Read response body up to max_bytes; ``(body, bytes_read)``.

    The check is against the RUNNING total, so an oversized body is abandoned
    mid-stream rather than accumulated whole — *max_bytes* bounds the bytes
    retained here, not just a verdict on the finished body. On abort the body
    is ``None`` and the count is what was read before stopping: a lower bound
    on the true size, and the only size figure that is not remote-controlled.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(_HTTP_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            logger.warning("Response exceeded %d bytes, aborting read", max_bytes)
            return None, total
        chunks.append(chunk)
    return b"".join(chunks), total
