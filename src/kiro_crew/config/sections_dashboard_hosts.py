"""Coercers for the hosts and links the dashboard resolves.

``browser_local_origins``, the GitLab, Jira and GitHub host lists, and ``link_patterns``.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
import re as _re
from collections.abc import Iterable
from urllib.parse import urlsplit as _urlsplit

from kiro_crew.config.sections_dashboard import (
    LINK_PATTERN_PATTERN_MAX_LEN,
    LINK_PATTERN_URL_MAX_LEN,
    LINK_PATTERNS_MAX,
    LinkPatternRule,
)

logger = logging.getLogger("kiro_crew.config.loader")


_GITLAB_HOST_NAME_RE = _re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

_BROWSER_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def coerce_browser_local_origins(raw: object) -> list[str]:
    """Coerce ``dashboard.browser_local_origins`` to canonical ``host:port`` entries.

    Fails closed: a non-list yields ``[]`` and an entry is dropped unless it is
    exactly one of the three loopback host spellings in
    :data:`_BROWSER_LOCAL_HOSTS` followed by a plain ASCII port in 1-65535. A
    scheme, path, userinfo, wildcard, a private/LAN address or a missing port is
    rejected rather than sanitized: a bare host would open every port on the
    machine (the gateway included), and widening past loopback is not what the
    setting is for. The port is rebuilt canonically (``08080`` -> ``8080``) so
    it compares equal to what ``urlsplit`` reports for the navigated URL.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        text = entry.strip().lower()
        host, sep, port_text = text.rpartition(":")
        if not sep or host not in _BROWSER_LOCAL_HOSTS:
            continue
        # The length cap runs before int(): a port of thousands of digits would
        # otherwise raise past Python's int-string limit and abort config load.
        if not (port_text.isascii() and port_text.isdigit() and len(port_text) <= 5):
            continue
        port = int(port_text)
        if not 0 < port < 65536:
            continue
        canonical = f"{host}:{port}"
        if canonical not in out:
            out.append(canonical)
    return out


def _coerce_gitlab_hosts(raw: object) -> list[str]:
    """Coerce the self-hosted GitLab allowlist to clean ``host[:port]`` entries.

    Fails closed: a non-list yields ``[]``, and an entry is dropped unless it is
    a bare lowercase-normalized hostname with an optional numeric port. Anything
    carrying a scheme, userinfo, path, query, or wildcard is rejected rather than
    sanitized, so a hand-edited config cannot smuggle a different target past the
    exact-match check the source-provider handler performs.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if not host or len(host) > 255:
            continue
        # Split the optional port BEFORE stripping trailing dots: an absolute-FQDN
        # entry with a port ("gitlab.example.:8443") keeps its dot in the middle of
        # the string, so stripping the whole entry first would leave it there and
        # the URL API's "gitlab.example:8443" could never match.
        name, sep, port_text = host.rpartition(":")
        if not sep:
            name, port_text = host, ""
        name = name.rstrip(".")
        # Hostname-only pattern here: the permissive one allows a trailing port,
        # so validating `name` with it would let a malformed "host:8443:443"
        # entry (whose last colon is split off as the port) silently authorize
        # "host:8443".
        if not name or not _GITLAB_HOST_NAME_RE.fullmatch(name):
            continue
        if sep:
            # A colon was present, so a port MUST follow and it must be a plain
            # run of ASCII digits. Fail closed on anything else rather than
            # authorize a host the operator never wrote:
            #   * "gitlab.example:"      -> empty port; without this it would
            #     fall through to the portless branch and grant the bare host.
            #   * "gitlab.example:+443"  -> int("+443") == 443 silently coerces.
            #   * "gitlab.example:1_000" -> int("1_000") == 1000 (underscores).
            #   * " 443", fullwidth digits, "0x10" -> also coerce or pass isdigit.
            # str.isdigit() alone accepts non-ASCII digit codepoints, so pair it
            # with isascii(); an empty string returns False for both.
            if not (port_text.isascii() and port_text.isdigit()):
                continue
            port = int(port_text)
            if not 0 < port < 65536:
                continue
            # Rebuild the port canonically: a configured "08443" would otherwise
            # be stored verbatim while both the browser URL API and the backend
            # normalize the URL's port to "8443", so the entry could never match.
            # The default HTTPS port is dropped entirely, matching the URL API.
            host = name if port == 443 else f"{name}:{port}"
        else:
            host = name
        # gitlab.com is always accepted and must not need an allowlist entry.
        if host in {"gitlab.com", "www.gitlab.com"} or host in out:
            continue
        out.append(host)
    return out


def _coerce_jira_hosts(raw: object) -> list[str]:
    """Coerce the self-hosted Jira allowlist — identical rules to GitLab hosts."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if not host or len(host) > 255:
            continue
        name, sep, port_text = host.rpartition(":")
        if not sep:
            name, port_text = host, ""
        name = name.rstrip(".")
        if not name or not _GITLAB_HOST_NAME_RE.fullmatch(name):
            continue
        if sep:
            if not (port_text.isascii() and port_text.isdigit()):
                continue
            port = int(port_text)
            if not 0 < port < 65536:
                continue
            host = name if port == 443 else f"{name}:{port}"
        else:
            host = name
        if host in out:
            continue
        out.append(host)
    return out


_GITHUB_PUBLIC_HOSTS = frozenset({"github.com", "www.github.com"})


def _coerce_github_hosts(raw: object, *, claimed: Iterable[str] = ()) -> list[str]:
    """Coerce the GitHub Enterprise allowlist to clean hostname entries.

    Entries follow the self-hosted GitLab grammar (see
    :func:`_coerce_gitlab_hosts`), so the same malformed entries are dropped and
    the same canonical form reaches the exact-match check. github.com needs no
    entry and is dropped, as is gitlab.com, which that grammar already refuses.
    An entry with a non-default port is refused with a warning: ``gh`` rejects a
    port in ``--hostname``, so such a host can never be reached, and pinning the
    bare name instead would aim the call at a different port.

    ``claimed`` holds the hosts another provider allowlist already owns
    (``gitlab_hosts``, ``jira_hosts``). Such an entry is refused here with a
    warning rather than kept: one host cannot name two providers, and keeping it
    on the GitLab or Jira side means this list never changes how those resolve.
    """
    taken = set(claimed)
    out: list[str] = []
    for host in _coerce_gitlab_hosts(raw):
        if host in _GITHUB_PUBLIC_HOSTS:
            continue
        if ":" in host:
            logger.warning(
                "dashboard.github_hosts entry %r names a port; gh cannot reach a "
                "GitHub host on a non-default port, so it is ignored",
                host,
            )
            continue
        if host in taken:
            logger.warning(
                "dashboard.github_hosts entry %r is also listed in gitlab_hosts or "
                "jira_hosts; it is ignored as a GitHub Enterprise host",
                host,
            )
            continue
        out.append(host)
    return out


#: Bitbucket Cloud hosts. Cloud has its own API and is never a Server instance.
_BITBUCKET_CLOUD_HOSTS = frozenset({"bitbucket.org", "www.bitbucket.org", "api.bitbucket.org"})


def _coerce_bitbucket_server_hosts(raw: object, *, claimed: Iterable[str] = ()) -> list[str]:
    """Coerce the Bitbucket Server allowlist to clean ``host[:port]`` entries.

    Entries follow the self-hosted GitLab grammar (see :func:`_coerce_gitlab_hosts`),
    ports included, because Server commonly listens on a non-default port. A
    Bitbucket Cloud host is dropped: Cloud speaks a different API. ``claimed``
    holds the hosts another provider allowlist already owns; such an entry is
    refused with a warning so this list never changes how those resolve.
    """
    taken = set(claimed)
    out: list[str] = []
    for host in _coerce_gitlab_hosts(raw):
        if host in _BITBUCKET_CLOUD_HOSTS:
            continue
        if host in taken:
            logger.warning(
                "dashboard.bitbucket_server_hosts entry %r is also listed in gitlab_hosts, "
                "jira_hosts or github_hosts; it is ignored as a Bitbucket Server host",
                host,
            )
            continue
        out.append(host)
    return out


def _coerce_gerrit_hosts(raw: object, *, claimed: Iterable[str] = ()) -> list[str]:
    """Coerce the Gerrit allowlist to clean ``host[:port]`` entries.

    Entries follow the self-hosted GitLab grammar (see :func:`_coerce_gitlab_hosts`),
    ports included, because a Gerrit instance commonly listens on a non-default
    port. Gerrit has no public default host, so nothing is accepted implicitly.
    ``claimed`` holds the hosts another provider allowlist already owns; such an
    entry is refused with a warning so this list never changes how those resolve.
    """
    taken = set(claimed)
    out: list[str] = []
    for host in _coerce_gitlab_hosts(raw):
        if host in taken:
            logger.warning(
                "dashboard.gerrit_hosts entry %r is also listed in gitlab_hosts, jira_hosts, "
                "github_hosts or bitbucket_server_hosts; it is ignored as a Gerrit host",
                host,
            )
            continue
        out.append(host)
    return out


def _coerce_link_patterns(raw: object) -> list[LinkPatternRule]:
    """Coerce the transcript link rules, skipping malformed entries.

    Same fail-open-per-entry discipline as the host allowlists: a hand-edited
    bad entry is dropped rather than failing the whole config load. Regex
    VALIDITY is deliberately not checked here -- the pattern is compiled by the
    browser in the JavaScript dialect, and Python's ``re`` accepts/rejects a
    different language; the frontend skips rules that fail to compile.
    """
    if not isinstance(raw, list):
        return []
    out: list[LinkPatternRule] = []
    seen: set[str] = set()
    for entry in raw:
        if len(out) >= LINK_PATTERNS_MAX:
            break
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        url = entry.get("url")
        if not isinstance(pattern, str) or not isinstance(url, str):
            continue
        # Whitespace in a regex is load-bearing (`PROJ-\d+ ` and `PROJ-\d+`
        # match different text), so the pattern is stored EXACTLY as authored;
        # strip() decides only whether it is blank. URLs are the opposite:
        # the validator below refuses whitespace in the authority and the
        # template is expanded, not matched, so edge-trimming cannot change
        # meaning.
        url = url.strip()
        if not pattern.strip() or len(pattern) > LINK_PATTERN_PATTERN_MAX_LEN:
            continue
        if len(url) > LINK_PATTERN_URL_MAX_LEN:
            continue
        # http(s) only: the rewrite mints anchors into every transcript, so a
        # javascript:/file: template must never survive to the renderer even
        # though the frontend re-checks. link_pattern_url_ok also mirrors the
        # renderer's origin-stability rule (no userinfo, no '{match}' in the
        # authority) so what the config stores is what actually renders.
        if not link_pattern_url_ok(url):
            continue
        # First-wins on duplicate patterns. The settings PUT rejects
        # duplicates outright, so this only meets hand-edited config files —
        # where dropping the shadowed twin beats dropping the whole list.
        if pattern in seen:
            continue
        seen.add(pattern)
        out.append(LinkPatternRule(pattern=pattern, url=url))
    return out


def link_pattern_url_ok(url: str) -> bool:
    """True when *url* is a link-pattern template the renderer will accept.

    Mirrors the frontend's ``normaliseHref``: beyond the http(s) + ``{match}``
    floor, the renderer substitutes two DIFFERENT canary tokens and requires
    the same origin both times, which rejects a ``{match}`` sitting in the
    authority (where the token could steer the host) and any userinfo (which
    ``safeHttpUrl`` refuses outright). Enforcing the same rules here keeps the
    save honest: without them a template like ``https://{match}.example/x`` or
    ``https://u:p@host/{match}`` stores fine, renders nothing, and shows no
    warning anywhere.
    """
    # Scheme case-insensitively, like the browser's URL parser the editor
    # validates with: `HTTPS://x/{match}` must not pass the inline check and
    # then die at the PUT with no warning. urlsplit below lowercases the
    # scheme itself, so the origin comparison already agrees.
    if not url.lower().startswith(("https://", "http://")) or "{match}" not in url:
        return False
    try:
        origins = set()
        for canary in ("aaa", "bbb"):
            parts = _urlsplit(url.replace("{match}", canary))
            # Userinfo never survives the renderer's safeHttpUrl, and the
            # browser's URL parser refuses whitespace in the authority that
            # Python's urlsplit tolerates — either way the rule would store
            # fine and silently never linkify.
            if "@" in parts.netloc or not parts.hostname:
                return False
            if any(ch.isspace() for ch in parts.netloc):
                return False
            origins.add((parts.scheme, parts.hostname, parts.port))
        return len(origins) == 1
    except ValueError:
        return False
