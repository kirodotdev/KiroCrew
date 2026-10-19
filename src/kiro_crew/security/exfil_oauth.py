"""OAuth: the consent endpoints the classifier trusts, and the safe shape diagnostics.

A consent URL to an authorization endpoint carries high-entropy state and PKCE
values by design, so the banner path's OAuth carve-out applies only to an
endpoint approved here: the code-owned builtin set, the operator's keystone
extension file, or an endpoint an operator-configured MCP server's discovery
verified (held with a TTL and a cap). An approval from the operator file, and a
newly recorded discovered endpoint, are SEL-audited. A finding is reported
through the diagnostic family here as a rule id, a component and a
character-class shape, never as the bytes it matched.

:mod:`kiro_crew.security.exfil` is the import path of every name defined here.
"""

from __future__ import annotations

import json
import logging
import re
import string
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from kiro_crew.sel import SecurityEvent, SecurityEventLog

from .redaction import _contains_fixed_credential, _text_contains_bare_secret

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("kiro_crew.security.exfil")


_OAUTH_DIAGNOSTIC_PARAMETER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_OAUTH_URL_SYMBOLS = frozenset("-._~:/?#[]@!$&'()*+,;=")


@dataclass(frozen=True)
class OAuthUrlShapeProfile:
    """Non-sensitive character-class profile for one rejected URL component."""

    length: int
    ascii_uppercase: int
    ascii_lowercase: int
    digits: int
    percent_signs: int
    symbols: int
    other: int


@dataclass(frozen=True)
class OAuthUrlCredentialDiagnostic:
    """Privacy-safe explanation of the first OAuth URL rejection rule."""

    rule: str
    component: str
    parameter: str | None
    shape: OAuthUrlShapeProfile

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _oauth_char_class(char: str) -> str:
    if char in string.ascii_uppercase:
        return "ascii_uppercase"
    if char in string.ascii_lowercase:
        return "ascii_lowercase"
    if char in string.digits:
        return "digits"
    if char == "%":
        return "percent_signs"
    if char in _OAUTH_URL_SYMBOLS:
        return "symbols"
    return "other"


def _oauth_shape_profile(value: str) -> OAuthUrlShapeProfile:
    counts = Counter(_oauth_char_class(char) for char in value)
    return OAuthUrlShapeProfile(
        length=len(value),
        ascii_uppercase=counts["ascii_uppercase"],
        ascii_lowercase=counts["ascii_lowercase"],
        digits=counts["digits"],
        percent_signs=counts["percent_signs"],
        symbols=counts["symbols"],
        other=counts["other"],
    )


def _safe_oauth_parameter_name(name: str | None) -> str | None:
    if (
        name is None
        or name not in _OAUTH_QUERY_PARAMS
        or not _OAUTH_DIAGNOSTIC_PARAMETER_RE.fullmatch(name)
    ):
        return None
    if _contains_fixed_credential(name) or _text_contains_bare_secret(name):
        return None
    return name


def _oauth_diagnostic(
    rule: str,
    component: str,
    value: str,
    *,
    parameter: str | None = None,
) -> OAuthUrlCredentialDiagnostic:
    return OAuthUrlCredentialDiagnostic(
        rule=rule,
        component=component,
        parameter=_safe_oauth_parameter_name(parameter),
        shape=_oauth_shape_profile(value),
    )


def _oauth_query_diagnostic(
    rule: str,
    query: str,
    *,
    predicate: Callable[[str], bool] | None = None,
    decoder: Callable[[str], str] | None = None,
    fallback: bool = True,
) -> OAuthUrlCredentialDiagnostic | None:
    segments = query.split("&")
    for segment in segments:
        key, separator, value = segment.partition("=")
        if not separator:
            continue
        candidate = decoder(value) if decoder is not None else value
        if predicate is not None and predicate(candidate):
            return _oauth_diagnostic(rule, "query_parameter", candidate, parameter=key)
        if predicate is None and len(segments) == 1:
            return _oauth_diagnostic(rule, "query_parameter", candidate, parameter=key)
    if not fallback:
        return None
    target = decoder(query) if decoder is not None else query
    return _oauth_diagnostic(rule, "query", target)


def _oauth_url_payload_diagnostic(
    rule: str,
    url: str,
    target: str,
    predicate: Callable[[str], bool],
    *,
    decoder: Callable[[str], str] | None = None,
) -> OAuthUrlCredentialDiagnostic:
    try:
        parsed = urlparse(url)
        if parsed.query:
            query_diagnostic = _oauth_query_diagnostic(
                rule,
                parsed.query,
                predicate=predicate,
                decoder=decoder,
                fallback=False,
            )
            if query_diagnostic is not None:
                return query_diagnostic
        for component, value in (
            ("scheme", parsed.scheme),
            ("authority", parsed.netloc),
            ("path", parsed.path),
            ("path_params", parsed.params),
            ("fragment", parsed.fragment),
        ):
            candidate = decoder(value) if decoder is not None else value
            if candidate and predicate(candidate):
                return _oauth_diagnostic(rule, component, candidate)
    except Exception:
        pass
    return _oauth_diagnostic(rule, "url", target)


# Exact, code-owned OAuth authorization endpoints whose standard front-channel
# parameters may legitimately contain high-entropy state/PKCE values on the ACP
# banner-safety path. This is deliberately NOT configurable and never uses
# suffix matching: an agent-owned
# setting or ``api.notion.com.attacker.example`` must not lower the redaction
# ceiling. Paths are exact and case-sensitive; explicit ports and HTTP are not
# exempted.
_OAUTH_AUTHORIZATION_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("accounts.google.com", "/o/oauth2/v2/auth"),
        ("api.notion.com", "/v1/oauth/authorize"),
        ("app.asana.com", "/-/oauth_authorize"),
        ("auth.atlassian.com", "/authorize"),
        ("github.com", "/login/oauth/authorize"),
        ("linear.app", "/oauth/authorize"),
        ("login.microsoftonline.com", "/common/oauth2/v2.0/authorize"),
        ("slack.com", "/oauth/v2/authorize"),
        # MCP-server authorization servers. A provider's *MCP* server usually
        # runs its own authorization server, distinct from the classic web-OAuth
        # endpoint above -- so the pairs above are NOT sufficient for the
        # Connections launch set. Each pair below was taken from the provider's
        # own advertised `authorization_endpoint` (RFC 8414 metadata reached via
        # RFC 9728 protected-resource discovery from the registry's mcp_url) and
        # independently corroborated by an authorize URL kiro-cli actually
        # minted. A launch provider missing from this set cannot be connected at
        # all: its banner fails closed with "authentication failed: URL
        # contained credential or exfiltration pattern", which is how the gap
        # was found. Every entry added to the Connections registry needs its
        # MCP authorization server here too.
        ("access.stripe.com", "/mcp/oauth2/authorize"),
        ("gitlab.com", "/oauth/authorize"),
        ("mcp.auth.mail.superhuman.com", "/oauth2/authorize"),
        ("mcp.linear.app", "/authorize"),
        # Miro's authorization_endpoint per its RFC 8414 metadata at
        # https://mcp.miro.com/.well-known/oauth-authorization-server. Also a
        # Connections registry entry; without this pair the fail-closed banner
        # blocks every attempt to connect the Miro remote MCP server.
        ("mcp.miro.com", "/authorize"),
        ("mcp.notion.com", "/authorize"),
        ("vercel.com", "/oauth/authorize"),
        # Industry-baseline batches (registry entries, all launch-gated). Each
        # pair is the ``authorization_endpoint`` from the issuer's RFC 8414
        # document, the issuer itself reached via RFC 9728 discovery from the
        # registry ``mcp_url`` by the L0 probe. The kiro-cli-minted
        # corroboration the launch set carries lands with each provider's
        # manual launch-gate check.
        ("airtable.com", "/oauth2/v1/authorize"),
        ("api.supabase.com", "/v1/oauth/authorize"),
        ("auth.prisma.io", "/authorize"),
        ("bindings.mcp.cloudflare.com", "/oauth/authorize"),
        ("huggingface.co", "/oauth/authorize"),
        ("mcp.amplitude.com", "/authorize"),
        ("mcp.canva.com", "/authorize"),
        ("mcp.neon.tech", "/api/authorize"),
        ("mcp.paypal.com", "/authorize"),
        ("mcp.postman.com", "/authorize"),
        ("mcp.sentry.dev", "/oauth/authorize"),
        ("mcp.squareup.com", "/authorize"),
        ("mcp.webflow.com", "/oauth/authorize"),
        ("mcp.zapier.com", "/oauth/authorize"),
        ("netlify-mcp.netlify.app", "/oauth-server/auth"),
        ("todoist.com", "/oauth/authorize"),
        ("www.dropbox.com", "/oauth2/authorize"),
        # Two issuers advertise a consent page on a different host than the
        # issuer itself: Figma's MCP authorization server (issuer api.figma.com)
        # sends the user to www.figma.com, Mixpanel's (issuer
        # mcp.mixpanel.com/mcp) to mixpanel.com. Only the consent host is
        # listed, because the banner gate keys on the URL the browser opens;
        # the registry test maps issuer host to consent host for these two.
        ("www.figma.com", "/oauth/mcp"),
        ("mixpanel.com", "/oauth/authorize"),
    }
)

# OAuth 2.0 / OIDC front-channel parameters whose values are expected to be
# opaque and high-entropy. The banner-only exemption is valid ONLY at an exact
# endpoint above. Every unknown parameter still receives the full query
# heuristics, even when it shares an otherwise-approved authorization URL.
_OAUTH_QUERY_PARAMS = frozenset(
    {
        "access_type",
        "acr_values",
        "allow_signup",
        "audience",
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "display",
        "domain_hint",
        "id_token_hint",
        "login",
        "login_hint",
        "max_age",
        "nonce",
        "prompt",
        "redirect_uri",
        "request_uri",
        "resource",
        "response_mode",
        "response_type",
        "scope",
        "state",
        "team",
        "ui_locales",
        "user_scope",
    }
)

# ── Operator-owned OAuth endpoint extension (keystone oauth_endpoints.json) ──
# ``_OAUTH_AUTHORIZATION_ENDPOINTS`` above is deliberately code-owned and
# exact-match, but that leaves no remedy short of a code release when a user's
# identity provider (Okta, Auth0, self-hosted OIDC, tenant-scoped Entra) is not
# in the launch set: its real consent URL routinely exceeds the query-length
# heuristic and the gate fails closed. The extension below restores an
# OPERATOR-owned escape hatch without weakening the ceiling for the agent:
#
# * the file lives on ``_CREW_SECRET_LEAVES`` (read+write keystone), so the
#   agent can neither read nor author its own trust widening;
# * a missing/unreadable/corrupt/non-object file yields the EMPTY set — a
#   mangled file must never widen trust (same posture as
#   ``computer_use.enable_state.load_state``);
# * every entry is strictly validated (exact host+path, no wildcards, no
#   ports/userinfo/percent-escapes, no ``..``), and invalid entries are
#   SKIPPED with a warning rather than failing the whole file;
# * HTTPS-only / no-explicit-port stays enforced by the gate logic at both
#   call sites and is NOT relaxable via the file;
# * the exemption granted is identical to the builtin set's: only the
#   base64-blob/query-length heuristics on known ``_OAUTH_QUERY_PARAMS`` are
#   skipped — fixed-credential patterns, heavy percent-encoding, userinfo,
#   fragments, backslashes, and unknown-param heuristics remain unconditional.
_ENDPOINT_EXTENSION_ENTRIES_KEY = "additional_authorization_endpoints"

# Bounds the accepted set AND the validation walk (the entry list is sliced to
# this before iteration), so a pathological file cannot amplify into an
# unbounded parse/warn loop or turn the endpoint check into a large probe.
_ENDPOINT_EXTENSION_CAP = 50

# Strict DNS-name shape for an operator entry, matched against the
# lowercase-normalized host: dot-separated LDH labels ending in a letter TLD.
# The letter-TLD requirement rejects raw IPv4 literals; the character class
# rejects wildcards, schemes, ports, userinfo, percent-escapes, whitespace,
# backslashes, and bracketed IPv6. Empty labels reject leading/trailing dots.
# The lookahead bounds total length to the DNS maximum.
_OAUTH_EXTENSION_HOST_RE = re.compile(
    r"\A(?=.{1,253}\Z)"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"\.[a-z]{2,}\Z"
)

# Paths are exact and case-sensitive (same semantics as the builtin set).
_OAUTH_EXTENSION_PATH_MAX_LEN = 512

# Rejected anywhere in an operator path entry: query/fragment/path-param
# delimiters and percent-escapes would let one entry smuggle structure the
# exact-match comparison is not built to normalize, and ``..`` plus backslash
# invite parser-differential games. The comparison is byte-exact, so a benign
# provider path never needs any of these.
_OAUTH_EXTENSION_PATH_BAD = (";", "?", "#", "%", "\\", "..")


def _valid_oauth_extension_path(path: str) -> bool:
    """True when *path* is safe to compare exactly against a consent URL path."""
    if not path.startswith("/") or len(path) > _OAUTH_EXTENSION_PATH_MAX_LEN:
        return False
    if any(marker in path for marker in _OAUTH_EXTENSION_PATH_BAD):
        return False
    return not any(ch.isspace() for ch in path)


# Memo for the parsed extension file, keyed on the file's identity + stat
# (path, mtime_ns, size) so a hand-edit takes effect on the next check without
# a gateway restart, while repeated checks against an unchanged file cost one
# ``stat`` instead of a read+parse+validate pass. (path, None) memoizes the
# absent-file case; any stat/read error bypasses the memo and fails soft.
_OAUTH_EXTENSION_MEMO: dict[tuple[str, tuple[int, int] | None], frozenset[tuple[str, str]]] = {}


def _load_operator_oauth_endpoints() -> frozenset[tuple[str, str]]:
    """Load the operator's OAuth-endpoint extension set (fail-soft to EMPTY).

    Reads ``<config_dir>/oauth_endpoints.json`` and returns the validated
    ``(lowercase host, exact path)`` pairs. Absent, unreadable, corrupt, or
    non-object files — and any entry that fails the strict per-entry
    validation — yield nothing: a mangled extension file must never widen
    trust. The ``config.loader`` import stays function-local to keep this
    module's import graph independent of the loader's: ``config/loader_snapshots.py``
    and ``config/loader_pipeline.py`` import ``security`` symbols function-locally to
    avoid a cycle, and a module-level import here would quietly re-arm that cycle the
    moment the loader hoists its own.
    """
    from kiro_crew.config import loader as config_loader

    try:
        path = config_loader.oauth_endpoints_path()
        try:
            stat = path.stat()
            stat_key: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stat_key = None
        memo_key = (str(path), stat_key)
        cached = _OAUTH_EXTENSION_MEMO.get(memo_key)
        if cached is not None:
            return cached
        if stat_key is None:
            _OAUTH_EXTENSION_MEMO.clear()
            _OAUTH_EXTENSION_MEMO[memo_key] = frozenset()
            return frozenset()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("oauth_endpoints.json unreadable; ignoring extension file", exc_info=True)
        return frozenset()

    approved = _validate_operator_oauth_entries(raw)
    # One live entry per file: the memo never outgrows a handful of keys, but a
    # test suite that rewrites the file hundreds of times should not accrete.
    _OAUTH_EXTENSION_MEMO.clear()
    _OAUTH_EXTENSION_MEMO[memo_key] = approved
    return approved


def _validate_operator_oauth_entries(raw: object) -> frozenset[tuple[str, str]]:
    """Strictly validate a parsed extension document into ``(host, path)`` pairs."""
    if not isinstance(raw, dict):
        logger.warning("oauth_endpoints.json is not a JSON object; ignoring it")
        return frozenset()
    entries = raw.get(_ENDPOINT_EXTENSION_ENTRIES_KEY)
    if not isinstance(entries, list):
        if entries is not None:
            logger.warning(
                "oauth_endpoints.json: %r is not a list; ignoring it",
                _ENDPOINT_EXTENSION_ENTRIES_KEY,
            )
        return frozenset()
    if len(entries) > _ENDPOINT_EXTENSION_CAP:
        logger.warning(
            "oauth_endpoints.json: %d entries exceed the cap (%d); extra entries ignored",
            len(entries),
            _ENDPOINT_EXTENSION_CAP,
        )

    approved: set[tuple[str, str]] = set()
    for entry in entries[:_ENDPOINT_EXTENSION_CAP]:
        host = entry.get("host") if isinstance(entry, dict) else None
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(host, str) or not isinstance(path, str):
            logger.warning(
                "oauth_endpoints.json: skipping malformed entry (need host+path strings)"
            )
            continue
        host_norm = host.lower()
        if not _OAUTH_EXTENSION_HOST_RE.fullmatch(host_norm) or not _valid_oauth_extension_path(
            path
        ):
            # The host is operator-authored config, not secret material, and
            # naming it is what makes the warning actionable.
            logger.warning(
                "oauth_endpoints.json: skipping invalid endpoint entry host=%r", host[:64]
            )
            continue
        approved.add((host_norm, path))
    return frozenset(approved)


# Per-process dedupe for the extension-used audit event, so repeated checks of
# the same URL (every banner emit/redraw re-validates) do not spam the SEL.
_OAUTH_EXTENSION_AUDITED: set[tuple[str, str]] = set()


def _emit_oauth_extension_used_event(host: str, path: str) -> None:
    """SEL-audit that an OPERATOR extension entry approved a consent endpoint.

    Best-effort: an audit failure must not break the user's ability to
    authorize their MCP server — the operator explicitly allowlisted the
    endpoint, so the approval stands regardless of audit success.
    """
    if (host, path) in _OAUTH_EXTENSION_AUDITED:
        return
    _OAUTH_EXTENSION_AUDITED.add((host, path))
    try:
        # Function-local for the same loader-cycle reason as
        # _load_operator_oauth_endpoints.
        from kiro_crew.config import loader as config_loader

        SecurityEventLog().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="oauth_endpoint_extension_used",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation="oauth_banner_check",
                outcome="allowed",
                resources=f"{host}{path}",
                metadata={
                    "host": host,
                    "path": path,
                    "file": str(config_loader.oauth_endpoints_path()),
                    "mechanism": "OAUTH_ENDPOINT_EXTENSION",
                },
            )
        )
    except Exception:
        logger.debug(
            "SEL audit failed for oauth_endpoint_extension_used (allow stands)",
            exc_info=True,
        )


def _approved_oauth_authorization_endpoint(host: str, path: str) -> bool:
    """Exact-match endpoint approval for the banner-only OAuth entropy carve-out.

    Union of the code-owned builtin set and the operator's keystone extension,
    computed at check time so a hand-edited file takes effect without a
    restart. The builtin set is consulted first so the common providers never
    touch the disk; an approval that came from an operator entry is SEL-audited
    (deduped per process). Callers keep enforcing HTTPS-only / no-explicit-port
    — this helper only answers endpoint identity.
    """
    key = (host.lower(), path)
    if key in _OAUTH_AUTHORIZATION_ENDPOINTS:
        return True
    if _discovered_oauth_endpoint_approved(key):
        return True
    if key in _load_operator_oauth_endpoints():
        _emit_oauth_extension_used_event(*key)
        return True
    return False


# ── Discovered OAuth endpoints (verified from an operator-configured MCP server) ──
# Filled only by ``mcp_oauth_discovery``; trust anchor, bounds and why this does
# not lower the agent's ceiling: docs/system-specs/modules/security.md,
# "Discovery-verified consent endpoints".
_OAUTH_DISCOVERED_TTL_SECONDS = 24 * 60 * 60
_OAUTH_DISCOVERED_CAP = 64
# Bound on the server URL copied into the SEL event; nothing else retains it.
_OAUTH_DISCOVERED_MCP_URL_MAX_LEN = 512
# (host, path) -> monotonic expiry. Host and path are bounded by the validators.
_OAUTH_DISCOVERED_ENDPOINTS: dict[tuple[str, str], float] = {}


def _discovered_oauth_endpoint_approved(key: tuple[str, str]) -> bool:
    """Whether *key* was verified by discovery and has not expired."""
    expiry = _OAUTH_DISCOVERED_ENDPOINTS.get(key)
    if expiry is None:
        return False
    if expiry <= time.monotonic():
        _OAUTH_DISCOVERED_ENDPOINTS.pop(key, None)
        return False
    return True


def record_discovered_oauth_endpoint(host: str, path: str, *, mcp_url: str) -> bool:
    """Approve an authorization endpoint verified by MCP OAuth discovery.

    Called ONLY by ``kiro_crew.mcp_oauth_discovery`` after it matched the
    endpoint against the metadata advertised for an operator-configured MCP
    server. Returns False (and records nothing) for a host or path the operator
    file's validators would refuse. A newly recorded endpoint is SEL-audited
    with the server URL it was verified from.
    """
    host_norm = host.lower()
    if not _OAUTH_EXTENSION_HOST_RE.fullmatch(host_norm) or not _valid_oauth_extension_path(path):
        return False
    key = (host_norm, path)
    fresh = not _discovered_oauth_endpoint_approved(key)
    _OAUTH_DISCOVERED_ENDPOINTS.pop(key, None)
    while len(_OAUTH_DISCOVERED_ENDPOINTS) >= _OAUTH_DISCOVERED_CAP:
        # A default: a worker thread may expire the same key concurrently.
        _OAUTH_DISCOVERED_ENDPOINTS.pop(next(iter(_OAUTH_DISCOVERED_ENDPOINTS)), None)
    _OAUTH_DISCOVERED_ENDPOINTS[key] = time.monotonic() + _OAUTH_DISCOVERED_TTL_SECONDS
    if fresh:
        _emit_oauth_discovered_endpoint_event(host_norm, path, mcp_url)
    return True


def _emit_oauth_discovered_endpoint_event(host: str, path: str, mcp_url: str) -> None:
    """Best-effort SEL record that discovery approved a consent endpoint."""
    if len(mcp_url) > _OAUTH_DISCOVERED_MCP_URL_MAX_LEN:
        mcp_url = mcp_url[: _OAUTH_DISCOVERED_MCP_URL_MAX_LEN - 1] + "…"
    try:
        SecurityEventLog().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="oauth_endpoint_discovered",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation="oauth_banner_check",
                outcome="allowed",
                resources=f"{host}{path}",
                metadata={
                    "host": host,
                    "path": path,
                    "mcp_url": mcp_url,
                    "mechanism": "OAUTH_ENDPOINT_DISCOVERY",
                },
            )
        )
    except Exception:
        logger.debug("SEL audit failed for oauth_endpoint_discovered (allow stands)", exc_info=True)
