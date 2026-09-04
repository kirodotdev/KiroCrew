"""Credential egress detection: URLs, OAuth tokens, IMDS and the egress gate.

The layer above output redaction. Redaction decides whether a run of text IS a
credential; this module decides whether a command or a URL is CARRYING one out,
so it reads redaction's shape predicates and redaction reads nothing from here.

Four surfaces: the URL and token layer (which URLs carry a credential in their
path or query, and the safe-diagnostic family that reports a finding as a
character-class shape rather than the bytes it matched), the data-egress command
gate, the IMDS address folder that collapses every alternate encoding of the
metadata address onto one dotted-quad, and the metadata check built on it.

The environment tier lives in ``denied_rules``: it resolves catalog rule ids to
row objects at import time, which is a dependency on the catalog rather than on
anything here.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import logging
import re
import socket
import string
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse

from kiro_crew.credential_patterns import AWS_KEY_ID
from kiro_crew.sel import SecurityEvent, SecurityEventLog

from .redaction import _contains_fixed_credential, _text_contains_bare_secret

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def exfil_query_min_len() -> int:
    """Public view of the long-query exfiltration threshold (chars)."""
    return _EXFIL_QUERY_MIN_LEN


# ── URL Exfiltration Detection ──
# Detects URLs whose path/query contain credential-like data. We flag the
# PAYLOAD, not the destination: any URL with secrets is suspicious regardless of
# host. The general redactors have one narrow carve-out for companion-supplied
# exact tenant hosts. A separate, opt-in carve-out for standard OAuth params is
# available only to ``oauth_url_contains_credential`` on the ACP banner path.
# Fixed/encoded credentials and heavy percent encoding remain unconditional.

# Host group (group 1) matches THREE host shapes so a raw-IP exfil destination
# is not silently skipped: a DNS name with a letter TLD, a raw
# IPv4 literal (``192.168.1.1``, incl. link-local/metadata ``169.254.169.254``),
# or a bracketed IPv6 literal (``[::1]``, ``[fd00::1]``). The prior regex required
# a ``.<letters>`` TLD, so ``http://169.254.169.254/latest/…/<secret>`` never
# matched _URL_RE and its path/query was never scanned. Group 3 stays the
# path+query so the scan/redact call sites are unchanged.
_URL_RE = re.compile(
    r"https?://"
    r"("
    r"[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}"  # DNS name with a letter TLD
    r"|\d{1,3}(?:\.\d{1,3}){3}"  # raw IPv4 literal
    r"|\[[0-9A-Fa-f:.]+\]"  # bracketed IPv6 literal (incl. IPv4-mapped ::ffff:d.d.d.d)
    # Group 3 = path AND/OR query. It must start with ``/`` (path) OR ``?``
    # (a query attached directly to the host, no path segment). The prior
    # ``/[...]*`` required a leading slash, so ``https://host?leak=<secret>``
    # yielded group(3)=None and both scan/redact bailed on ``qmark == -1``,
    # never inspecting the query — a real exfil bypass. ``[/?]`` admits both;
    # the ``path_and_query.find("?")`` split at the call sites is unchanged.
    r")(:\d+)?([/?][^\s)\"'>]*)?"
)

# Query string length threshold — normal URLs rarely exceed this
_EXFIL_QUERY_MIN_LEN = 200

# Patterns that indicate secrets or encoded data in query params
_EXFIL_PATTERNS = re.compile(
    r"(?:"
    r"[A-Za-z0-9+/=]{40,}"  # base64-like blob (40+ chars)
    r"|%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}"  # heavy URL-encoding (20+ encoded chars)
    f"|{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)

# Heavy URL-encoding detector — the same "20+ consecutive percent-encoded
# octets" branch carved out of _EXFIL_PATTERNS. This stays UNCONDITIONAL: the
# context-specific exemptions below skip only the base64-blob and query-length
# heuristics (which false-positive on legitimate document pointers or banner
# state/PKCE), NOT this detector, so a heavily encoded payload is still caught.
_EXFIL_PERCENT_RE = re.compile(
    r"%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}",
    re.IGNORECASE,
)

# Percent-decoding passes applied when re-scanning a URL for encoded
# credentials. More than one is required because a double-encoded payload
# survives a single pass; the bound stops a deliberately over-encoded URL from
# making the scan loop indefinitely.
_MAX_URL_DECODE_PASSES = 3

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
        # https://mcp.miro.com/.well-known/oauth-authorization-server. Not a
        # Connections registry entry; without this pair the fail-closed banner
        # blocks every attempt to connect the Miro remote MCP server.
        ("mcp.miro.com", "/authorize"),
        ("mcp.notion.com", "/authorize"),
        ("vercel.com", "/oauth/authorize"),
        # Industry-baseline batch 1 (registry entries, all launch-gated). Each
        # pair is the ``authorization_endpoint`` from the issuer's RFC 8414
        # document, the issuer itself reached via RFC 9728 discovery from the
        # registry ``mcp_url`` by the L0 probe. The kiro-cli-minted
        # corroboration the launch set carries lands with each provider's
        # manual launch-gate check.
        ("airtable.com", "/oauth2/v1/authorize"),
        ("api.supabase.com", "/v1/oauth/authorize"),
        ("mcp.canva.com", "/authorize"),
        ("mcp.paypal.com", "/authorize"),
        ("mcp.sentry.dev", "/oauth/authorize"),
        ("www.dropbox.com", "/oauth2/authorize"),
        # Figma's MCP authorization server (issuer api.figma.com) advertises a
        # consent page on www.figma.com, so the registry issuer host and the
        # consent host differ; both are listed -- the banner gate keys on the
        # consent URL while the registry test keys on the issuer host.
        ("api.figma.com", "/oauth/mcp"),
        ("www.figma.com", "/oauth/mcp"),
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
    module's import graph independent of the loader's: ``config/loader.py``
    itself imports ``security`` symbols function-locally to avoid a cycle, and
    a module-level import here would quietly re-arm that cycle the moment the
    loader hoists its own.
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
    if key in _load_operator_oauth_endpoints():
        _emit_oauth_extension_used_event(*key)
        return True
    return False


# S3 presigned URLs contain X-Amz-Signature (a 64-char hex string) that
# matches the base64-like blob pattern above.  These are intentional
# time-limited access tokens, not leaked credentials.  Skip the exfil
# check when ALL standard presigned-URL query params are present on an
# amazonaws.com domain.  Values are validated to prevent spoofing.
_S3_PRESIGNED_RE = re.compile(
    r"X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f".*X-Amz-Credential={AWS_KEY_ID}"  # shared spelling: credential_patterns
    r"(?:%2F|/)"
    r".*X-Amz-Expires=\d{1,6}"
    r".*X-Amz-Signature=[0-9a-f]{64}",
    re.IGNORECASE,
)

# Only these parameter keys are allowed in a presigned URL.  Any extra
# keys cause the fast-path to reject, falling through to normal checks.
_S3_PRESIGNED_PARAMS = frozenset(
    {
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
        "X-Amz-Security-Token",
    }
)


# Structural validators for presigned param values that would otherwise
# false-positive against _EXFIL_PATTERNS.  Each value is validated rather
# than exempted, so attacker-controlled data cannot be smuggled through.
_STS_TOKEN_RE = re.compile(r"^(?:FwoGZX|IQoJb3JpZ2lu)[A-Za-z0-9+/=%]{1,2000}$")
_CREDENTIAL_RE = re.compile(
    f"^{AWS_KEY_ID}"  # shared spelling: credential_patterns
    r"(?:%2F|/)[0-9]{8}"
    r"(?:%2F|/)[a-z0-9-]+(?:%2F|/)s3(?:%2F|/)aws4_request$"
)
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")

_STRUCTURAL_VALIDATORS = {
    "X-Amz-Credential": _CREDENTIAL_RE,
    "X-Amz-Signature": _SIGNATURE_RE,
    "X-Amz-Security-Token": _STS_TOKEN_RE,
}


def _is_safe_presigned(domain: str, query: str) -> bool:
    """Return True if the URL is a valid S3 presigned URL with no extra parameters."""
    if not domain.endswith(".amazonaws.com"):
        return False
    if not _S3_PRESIGNED_RE.search(query):
        return False
    params = parse_qs(query, keep_blank_values=True)
    if not _S3_PRESIGNED_PARAMS.issuperset(params.keys()):
        return False
    # Structurally validate params that would false-positive against
    # _EXFIL_PATTERNS.  No values are fully exempt — each is checked.
    for key, values in params.items():
        validator = _STRUCTURAL_VALIDATORS.get(key)
        if validator:
            for val in values:
                if not validator.match(val):
                    return False
        else:
            for val in values:
                if _EXFIL_PATTERNS.search(val):
                    return False
    return True


# Hard, unambiguous credential markers scanned across the FULL URL path+query
# — a real AWS key / SSH-or-PEM header / Slack token in a URL is
# exfil even to an otherwise-safe host, and even with no ``?`` query (secret in
# the PATH). Distinct from the broader _EXFIL_PATTERNS base64/length heuristics,
# which stay query-only (long base64 PATH segments — CDN asset ids, git object
# hashes — are benign).
_HARD_CREDENTIAL_RE = re.compile(
    r"(?:"
    f"{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)


def _exempt_exact_hosts() -> frozenset[str]:
    """Exact-match hosts that skip ONLY the exfil base64/length heuristics.

    Sourced from the active ``PlatformContext``'s ``CredentialPolicy`` — the
    public Default returns an empty set (no exemptions), a loaded companion
    supplies its trusted-tenant host list.  NEVER read from ``config.json``: an
    agent-writable exemption would be a hole in the redaction ceiling.

    Import is FUNCTION-LOCAL (deferred, mirroring the ``sel.py`` pattern) so
    ``security`` never reaches ``kiro_crew.platform`` at module-load time — the
    CPP import-direction invariant (``platform/defaults.py`` imports ``security``
    at top level).

    Degrade semantics: EVERY failure degrades to ``frozenset()`` — the empty set
    means MORE redaction (every host runs the heuristics), the SAFE direction
    here, and it is stricter than any companion-supplied exemption list could
    be.  This lookup can only ever RELAX the heuristics, so there is no
    fail-closed to protect: propagating an error would convert "redact slightly
    more aggressively" into "the calling operation aborts", which took down every
    pooled MCP backend spawn in ``gatewayd`` (an unbooted worker that calls
    ``redact()`` on the spawn-log and stderr-drain paths).  Deliberately INVERTED
    vs ``redact_via_context``'s propagation: that seam substitutes a companion's
    redaction for the baseline, so a missing context there must not fail open.

    NO-CONTEXT FAST PATH: when no context is INSTALLED this returns the empty set
    without resolving one, via ``installed_context()``.  That is not merely an
    optimization, it is the only way to keep this off the event loop.  Resolving
    would load config + discover plugin entry points, and on a non-standalone
    profile ``current_context()`` never memoizes its fail-closed verdict, so a
    per-line caller (``_pump_stderr`` redacting backend stderr) would re-pay that
    synchronous I/O for every line.  The answer is unchanged either way: the
    public ``DefaultCredentialPolicy`` exempts no hosts, so a lazily-composed
    standalone default yields this same empty set, and an unbooted
    non-standalone process must not be handed exemptions at all.

    A pre-method companion adapter (no ``exempt_exact_hosts``) degrades to the
    empty set via ``getattr`` rather than raising.  NO logging on the degrade
    path: this runs inside the stdio MCP servers whose stray writes corrupt the
    JSON-RPC stream.
    """
    from kiro_crew.platform.context import installed_context

    ctx = installed_context()
    if ctx is None:
        return frozenset()

    try:
        policy = ctx.credentials
        getter = getattr(policy, "exempt_exact_hosts", None)
        if getter is None:
            return frozenset()
        raw = getter()
        # Normalize INSIDE the guarded block: a buggy companion adapter may return
        # None or a set with non-string members, and callers (_exfil_exempt_hosts)
        # iterate + .lower() the result. If that raised outside this try, it would
        # break EVERY redaction path (chat/Slack/MCP/dashboard) instead of degrading
        # to maximum redaction. Keep only str members; anything malformed degrades
        # to the empty set (the SAFE direction — more redaction).
        return frozenset(h for h in raw if isinstance(h, str))
    except Exception:
        return frozenset()


def _exfil_exempt_hosts() -> frozenset[str]:
    """Companion exempt-host set normalized to lowercase for case-insensitive match.

    Hostnames are case-insensitive (RFC 4343); Office apps commonly emit
    mixed-case hosts (``Contoso.SharePoint.com``). _URL_RE captures the host
    verbatim, so both the captured host and the companion-supplied members must
    be lowercased before comparison or a legitimate document pointer to an
    exempted tenant is wrongly redacted. Delegates fail-closed / degrade
    semantics to _exempt_exact_hosts().
    """
    return frozenset(host.lower() for host in _exempt_exact_hosts())


# ── Kiro Crew's own Slack app-create deep link ──
# ``kirocrew manifest --url`` and ``GET /api/slack/manifest`` both hand the user
# Slack's new-app deep link carrying the bundled app manifest percent-encoded
# into ``manifest_yaml``. That payload is ~1.9 KB, so the aggregate query-length
# heuristic classifies it as exfiltration and the user is shown
# ``[REDACTED: suspicious URL to api.slack.com]`` instead of the link the setup
# guide tells them to click.
#
# The carve-out VALIDATES rather than trusts the destination: the decoded payload
# must reproduce the bundled template, so an approved (host, path) carries no
# arbitrary bytes. A different path, an extra or missing parameter, a repeated
# parameter, or a payload that does not rebuild the template all keep the full
# heuristics. This is deliberately NOT a host exemption: ``_exempt_exact_hosts``
# is companion-owned tenant trust, and widening it here would exempt every URL at
# api.slack.com including a model-authored one.
#
# The ALIAS is the one caller-controlled span, so it does NOT ride free: the
# caller feeds it back through the base64-blob heuristic (see
# ``_exfil_url_warning``) instead of zeroing the heuristic payload. Zeroing it was
# a real bypass — the alias slot accepted 64 chars of ``[A-Za-z0-9_-]``, which is
# wide enough for a 40-char alphanumeric secret, and ``_EXFIL_PATTERNS`` needs a
# 40+ char run to fire. ``slack_manifest.ALIAS_MAX`` (32) now makes such a run
# impossible AND the surviving span is still scanned, so an ``AKIA…`` id or an
# ``xox…`` token short enough to fit is caught on the alias alone.
#
# Residual, stated rather than implied: an alias of up to ALIAS_MAX chars that
# resembles no known credential is exempt from the base64/length heuristics. That
# opens no NEW capability — any URL at any host may already carry a query under
# _EXFIL_QUERY_MIN_LEN (200) chars without tripping either heuristic, so this
# span is strictly narrower than what is available without the carve-out.
#
# Every unconditional check runs BEFORE this point and is unaffected:
# hard-credential markers, canonical provider tokens, the multi-pass decode (and
# its fail-closed saturation branch), and heavy percent-encoding.
_SLACK_APP_CREATE_PARAMS = frozenset({"new_app", "manifest_yaml"})
# Single-slot cache for the derived pattern. A plain module constant would read
# packaged data at import time, which ``security`` avoids: it is imported by the
# stdio MCP servers, where import-time file I/O is on the critical path.
_slack_manifest_re_slot: list[re.Pattern[str] | None] = []


def _slack_manifest_payload_re() -> re.Pattern[str] | None:
    """Pattern matching the bundled Slack manifest rendered with any one alias.

    Derived from ``slack_manifest.stripped_template()`` — the SAME procedure both
    emitters use to build the payload — so the accepted payload cannot drift from
    the emitted one. Every ``{{ALIAS}}`` after the first must be the same alias
    (backreference), so a payload that varies them is rejected. Returns None when
    the template cannot be read, which fails closed (no exemption).
    """
    if _slack_manifest_re_slot:
        return _slack_manifest_re_slot[0]
    compiled: re.Pattern[str] | None = None
    try:
        from kiro_crew import slack_manifest

        rendered = slack_manifest.stripped_template()
        placeholder_token = slack_manifest.ALIAS_PLACEHOLDER
        alias_body = slack_manifest.ALIAS_PATTERN
    except Exception:
        rendered = ""
        placeholder_token = ""
        alias_body = ""
    if rendered and placeholder_token in rendered:
        parts = rendered.split(placeholder_token)
        pattern = re.escape(parts[0])
        for index, part in enumerate(parts[1:]):
            slot = f"(?P<alias>{alias_body})" if index == 0 else "(?P=alias)"
            pattern += slot + re.escape(part)
        compiled = re.compile(pattern)
    _slack_manifest_re_slot.append(compiled)
    return compiled


def _kirocrew_slack_app_link_alias(
    domain: str,
    path: str,
    query: str,
    *,
    is_https: bool,
    port: str,
) -> str | None:
    """The alias when this is our own Slack app-create link, else None.

    Returns the captured alias rather than a bool so the caller can keep that one
    caller-controlled span under the heuristics. An empty-string alias is
    impossible (the pattern requires at least one char), so a truthiness test on
    the result would be safe — but callers should compare against None to keep
    that dependence explicit.

    ``domain`` is expected already lowercased by the caller. HTTPS-only and no
    explicit port, matching the OAuth gate's posture.
    """
    if not is_https or port:
        return None
    from kiro_crew import slack_manifest

    if domain != slack_manifest.APP_CREATE_HOST or path != slack_manifest.APP_CREATE_PATH:
        return None
    params = parse_qs(query, keep_blank_values=True)
    # Exact param set — an extra parameter is the obvious smuggling shape, so a
    # superset is refused rather than ignored.
    if set(params) != _SLACK_APP_CREATE_PARAMS:
        return None
    if params["new_app"] != ["1"]:
        return None
    payloads = params["manifest_yaml"]
    if len(payloads) != 1:
        return None
    pattern = _slack_manifest_payload_re()
    if pattern is None:
        return None
    match = pattern.fullmatch(payloads[0])
    if match is None:
        return None
    return match.group("alias")


def _exfil_url_warning(
    domain: str,
    path_and_query: str,
    exempt_hosts: frozenset[str],
    *,
    port: str = "",
    is_https: bool = True,
    allow_safe_presigned: bool = True,
    allow_oauth_entropy: bool = False,
    _rule_out: list[str] | None = None,
) -> str | None:
    """Classify one matched URL — the single per-URL exfil verdict.

    Shared by scan_exfiltration_urls (which collects the warnings) and
    redact_exfiltration_urls (which redacts every URL that returns non-None), so
    the two paths can never drift. Returns the warning string, or None if clean.
    ``_rule_out`` receives only a stable rule id, never URL-derived text.
    """

    def trace(rule: str) -> None:
        if _rule_out is not None:
            _rule_out.append(rule)

    qmark = path_and_query.find("?")
    query = path_and_query[qmark + 1 :] if qmark != -1 else ""

    # Valid S3 presigned URLs carry AKIA in X-Amz-Credential legitimately. This
    # exemption is disabled for OAuth-banner validation.
    if allow_safe_presigned and query and _is_safe_presigned(domain, query):
        return None

    # Hard credential markers are unconditional across the full path/query.
    if _HARD_CREDENTIAL_RE.search(path_and_query):
        trace("exfil_hard_credential")
        return f"Suspicious URL with credential in path/query: {domain}"

    # Fixed credential signatures ANYWHERE in the full authority/path/query are
    # unconditional. This uses canonical provider-token patterns (GitHub,
    # Stripe, etc.) in addition to the older AWS/SSH/Slack hard floor, but NOT
    # the bare-secret entropy classifier that false-positives on OAuth state.
    full_payload = f"{domain}{port}{path_and_query}"
    if _contains_fixed_credential(full_payload):
        trace("exfil_fixed_credential")
        return f"Suspicious URL with credential in path/query: {domain}"

    # Decode the whole authority/path/query payload as one invariant. Component-
    # specific passes risk leaving newly handled URL structure outside the scan.
    # Decoding ONCE is not enough: a double-encoded payload ("%2542" -> "%42" ->
    # "B") survives a single pass, so decode until the text stops changing.
    # Bounded so a deliberately over-encoded URL cannot spin here.
    decoded_payload = full_payload
    for _ in range(_MAX_URL_DECODE_PASSES):
        next_payload = unquote_plus(decoded_payload)
        if next_payload == decoded_payload:
            break
        decoded_payload = next_payload
        if _HARD_CREDENTIAL_RE.search(decoded_payload) or _contains_fixed_credential(
            decoded_payload
        ):
            trace("exfil_encoded_credential")
            return f"Suspicious URL with encoded credential in path/query: {domain}"

    # Fail closed when the budget above ran out with layers still to go. A
    # payload that is STILL decodable was never seen in plaintext, and neither
    # remaining check covers it: the credential patterns match literal markers
    # rather than percent text, and _EXFIL_PERCENT_RE needs 20+ CONSECUTIVE
    # octets, which the intermediate forms of a wrapped payload ("%252520") do
    # not form. Treating saturation as clean therefore made the bound an escape
    # hatch -- wrap a credential in one more layer than the cap and it passed.
    # Raising the cap only moves that line, so the bound is priced as lost
    # precision (a pathologically encoded URL is refused) instead of lost
    # soundness. Benign traffic reaches a stable payload in one or two passes
    # and never gets here.
    if unquote_plus(decoded_payload) != decoded_payload:
        trace("exfil_decode_saturated")
        return f"Suspicious URL with encoded credential in path/query: {domain}"

    # Heavy percent-encoding is always suspicious, including inside a standard
    # OAuth parameter at an approved endpoint. It runs before either
    # host-sensitive heuristic exemption below.
    if _EXFIL_PERCENT_RE.search(path_and_query):
        trace("exfil_percent_encoding")
        return f"Suspicious URL with credential-like query data: {domain}"

    if qmark == -1:
        return None

    # Choose the exact payload that receives generic base64/entropy + aggregate
    # length heuristics. The OAuth-param carve-out is available ONLY to the
    # dedicated ACP banner-safety path. General text redactors leave the flag
    # false and remain strict for arbitrary agent/model text.
    _dom = domain.lower()
    _oauth_endpoint = (
        allow_oauth_entropy
        and is_https
        and not port
        and _approved_oauth_authorization_endpoint(_dom, path_and_query.split("?", 1)[0])
    )
    if _oauth_endpoint:
        # Names are matched literally and case-sensitively; encoded/mixed-case
        # aliases fail closed as unknown parameters.
        heuristic_query = "&".join(
            segment
            for segment in query.split("&")
            if segment.partition("=")[0] not in _OAUTH_QUERY_PARAMS
        )
    elif (
        _slack_alias := _kirocrew_slack_app_link_alias(
            _dom,
            path_and_query.split("?", 1)[0],
            query,
            is_https=is_https,
            port=port,
        )
    ) is not None:
        # Our own app-create link: the payload reproduces the bundled template,
        # so the constant bytes are what caused the false positive and are
        # excluded. The alias is the one caller-controlled span, so it STAYS
        # under the heuristics rather than riding free — zeroing this was a
        # bypass wide enough for a 40-char alphanumeric secret.
        heuristic_query = _slack_alias
    elif _dom in exempt_hosts:
        heuristic_query = ""
    else:
        heuristic_query = query

    if heuristic_query:
        # NO per-shape waiver on this gate, deliberately, and the same reasoning
        # forbids adding one. Two were tried for the prefilled GitHub issue link of
        # #7820 — one keyed to the validated SHAPE, one additionally pinned to this
        # project's own tracker — and both are exfiltration primitives, because what
        # reaches this function is MODEL-AUTHORED text:
        #
        #   Injected content steers the model into emitting a prefill URL whose
        #   ``body`` carries percent-encoded private context. The waiver skips this
        #   check, the link renders as the familiar "file an issue" affordance, the
        #   user submits it — and the issue is PUBLIC, so the attacker reads it.
        #
        # Pinning the repository does not help: this project's tracker is
        # world-readable by design. A URL's shape says nothing about who authored
        # it, and a marker placed IN the text travels in the channel the injection
        # already controls, so provenance has to come from a different channel.
        # It already does: ``diagnostics._issue_url`` builds the prefill link from
        # STRUCTURED fields and the dashboard renders its own anchor from
        # ``BundleResult.github_issue_url``, a JSON field no redactor scans
        # (Settings -> Report a Problem, the feedback pill). A link that never
        # enters model prose never needs a waiver, and ``terminal_issue_url`` is the
        # bounded variant for paths that DO get relayed through prose.
        #
        # To make a long legitimate URL render, narrow or replace this heuristic for
        # EVERY host on its own merits (#7820 also reports monitorportal.amazon.com)
        # — do not reintroduce a per-shape escape hatch. Pinned by
        # test_redaction_mirror_parity.py::TestPrefilledIssueCarveOutParity.
        if len(heuristic_query) >= _EXFIL_QUERY_MIN_LEN:
            trace("exfil_query_length")
            return (
                f"Suspicious URL with long query params ({len(heuristic_query)} chars): "
                f"{domain}{path_and_query[:60]}..."
            )
        if _EXFIL_PATTERNS.search(heuristic_query) or _EXFIL_PATTERNS.search(
            unquote_plus(heuristic_query)
        ):
            trace("exfil_query_pattern")
            return f"Suspicious URL with credential-like query data: {domain}"
    return None


def scan_exfiltration_urls(text: str) -> list[str]:
    """Scan text for URLs that may be exfiltrating data via query params.

    Flags the PAYLOAD, not the destination: fixed credentials and the
    base64/length heuristics inspect the URL path+query regardless of host. Only
    companion-supplied exact tenant hosts skip the base64/length heuristics here;
    the OAuth-param carve-out is disabled for this general text scanner. Returns
    list of warning strings, empty if clean.
    """
    exempt_hosts = _exfil_exempt_hosts()
    warnings: list[str] = []
    for match in _URL_RE.finditer(text):
        warning = _exfil_url_warning(
            match.group(1),
            match.group(3) or "",
            exempt_hosts,
            port=match.group(2) or "",
            is_https=match.group(0).lower().startswith("https://"),
        )
        if warning:
            warnings.append(warning)
    return warnings


#: Stable PREFIX of the substitution :func:`redact_exfiltration_urls` writes in
#: place of a suspicious URL. The full tag interpolates the redacted URL's
#: domain (``f"{EXFILTRATION_REDACTION_TAG_PREFIX}{domain}]"``), so unlike the
#: constant credential tags it cannot be equality-compared -- which is why it is
#: a PREFIX constant and deliberately NOT a member of
#: :data:`kiro_crew.security.redaction.CREDENTIAL_REDACTION_TAGS` (see that
#: tuple's docstring). A consumer that must detect this rewriter's
#: substitutions (the dashboard chat notice, issue #8132) prefix-counts THIS
#: constant; the substitution below is built from it so the two can never
#: drift.
EXFILTRATION_REDACTION_TAG_PREFIX = "[REDACTED: suspicious URL to "


def redact_exfiltration_urls(text: str) -> tuple[str, list[str]]:
    """Scan and redact suspicious exfiltration URLs from text.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings = scan_exfiltration_urls(text)
    if not warnings:
        return text, []

    exempt_hosts = _exfil_exempt_hosts()
    result = text
    for match in _URL_RE.finditer(text):
        domain = match.group(1)
        if _exfil_url_warning(
            domain,
            match.group(3) or "",
            exempt_hosts,
            port=match.group(2) or "",
            is_https=match.group(0).lower().startswith("https://"),
        ):
            result = result.replace(match.group(0), f"{EXFILTRATION_REDACTION_TAG_PREFIX}{domain}]")
    return result, warnings


# Markerless 40-character values collide with OAuth entropy only for these
# authorization-request fields. ``code_verifier`` is intentionally absent: it
# is sent to the token endpoint, not on this front channel.
_OAUTH_ENTROPY_QUERY_PARAMS = frozenset({"code_challenge", "nonce", "state"})

# The exemption is bounded to shapes the protocol itself can emit, so an
# AWS-secret-shaped run cannot ride a front-channel parameter into the blanked
# set. base64url (RFC 4648 s5) emits `-`/`_` and never `+`/`/`, and an S256
# challenge is base64url of a 32-byte digest -- exactly 43 characters.
_OAUTH_S256_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _oauth_entropy_form_is_protocol_shaped(key: str, form: str) -> bool:
    """Return True when ONE decoded form of a value keeps a protocol shape."""
    if key == "code_challenge":
        return bool(_OAUTH_S256_CHALLENGE_RE.fullmatch(form))
    return "+" not in form and "/" not in form


def _oauth_entropy_value_is_protocol_shaped(key: str, value: str) -> bool:
    """Return True when *value* has a shape OAuth entropy can legitimately take.

    EVERY decoded form must keep the shape, not just the raw one. Decoding once
    is not enough for the same reason it is not enough in `_exfil_url_warning`:
    a double-encoded payload (`%252F` -> `%2F` -> `/`) survives a single pass,
    so a raw-plus-one-decode test would let the base64-standard alphabet smuggle
    an AWS-secret-shaped run into the blanked set. Decode until the text stops
    changing, bounded by `_MAX_URL_DECODE_PASSES` so an over-encoded value
    cannot spin here.
    """
    candidate = value
    for _ in range(_MAX_URL_DECODE_PASSES):
        if not _oauth_entropy_form_is_protocol_shaped(key, candidate):
            return False
        decoded = unquote(candidate)
        if decoded == candidate:
            return True
        candidate = decoded
    # Budget ran out with a layer still to go. A value that is STILL decodable
    # was never seen in plaintext, so it cannot earn the exemption: refuse it
    # and let the markerless scan judge the value as written.
    return False


def _oauth_credential_scan_target(
    url: str,
    query: str,
    *,
    approved_endpoint: bool,
) -> str:
    """Blank entropy-bearing OAuth values before the markerless URL scan.

    Fixed credential signatures are checked against the raw and decoded URL
    before this target is built. At an exact approved endpoint, only the
    code-owned state, nonce, and PKCE challenge fields are omitted from the
    markerless bare-secret heuristic, and only when the value carries a shape
    the protocol can emit (see
    :func:`_oauth_entropy_value_is_protocol_shaped`). Other recognized values,
    parameter names, unknown parameters, and every non-query URL component
    remain in the scan target.
    """
    if not approved_endpoint or not query:
        return url

    sanitized_segments: list[str] = []
    for key, separator, value in (segment.partition("=") for segment in query.split("&")):
        approved_value = (
            bool(separator)
            and key in _OAUTH_ENTROPY_QUERY_PARAMS
            and _oauth_entropy_value_is_protocol_shaped(key, value)
        )
        sanitized_segments.append(
            f"{key}{separator}" if approved_value else f"{key}{separator}{value}"
        )

    query_start = url.find("?")
    if query_start == -1:
        return url
    fragment_start = url.find("#", query_start + 1)
    suffix = "" if fragment_start == -1 else url[fragment_start:]
    sanitized_query = "&".join(sanitized_segments)
    return url[: query_start + 1] + sanitized_query + suffix


def diagnose_oauth_url_credential(url: str) -> OAuthUrlCredentialDiagnostic | None:
    """Return a safe rejection signature, never URL/value bytes or derivatives."""
    if not url:
        return None

    decoded_url = unquote(url)
    if "\\" in url:
        return _oauth_url_payload_diagnostic(
            "backslash_raw",
            url,
            url,
            lambda value: "\\" in value,
        )
    if "\\" in decoded_url:
        return _oauth_url_payload_diagnostic(
            "backslash_decoded",
            url,
            decoded_url,
            lambda value: "\\" in value,
            decoder=unquote,
        )
    if _contains_fixed_credential(url):
        return _oauth_url_payload_diagnostic(
            "fixed_credential_raw",
            url,
            url,
            _contains_fixed_credential,
        )
    if _contains_fixed_credential(decoded_url):
        return _oauth_url_payload_diagnostic(
            "fixed_credential_decoded",
            url,
            decoded_url,
            _contains_fixed_credential,
            decoder=unquote,
        )

    try:
        parsed = urlparse(url)
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return _oauth_diagnostic("parse_error", "url", url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return _oauth_diagnostic("invalid_endpoint", "scheme", parsed.scheme)
    if not parsed.hostname:
        return _oauth_diagnostic("invalid_endpoint", "authority", parsed.netloc)

    # Browsers and RFC-style parsers disagree on userinfo handling.
    if "@" in parsed.netloc:
        return _oauth_diagnostic(
            "userinfo",
            "userinfo",
            parsed.netloc.rpartition("@")[0],
        )
    decoded_netloc = unquote(parsed.netloc)
    if "@" in decoded_netloc:
        return _oauth_diagnostic(
            "userinfo",
            "userinfo",
            decoded_netloc.rpartition("@")[0],
        )

    approved_endpoint = (
        parsed.scheme.lower() == "https"
        and not port
        and _approved_oauth_authorization_endpoint(parsed.hostname.lower(), parsed.path)
    )
    scan_target = _oauth_credential_scan_target(
        url,
        parsed.query,
        approved_endpoint=approved_endpoint,
    )
    for candidate, suffix, decoder in (
        (scan_target, "raw", None),
        (unquote(scan_target), "decoded", unquote),
    ):
        if _contains_fixed_credential(candidate):
            return _oauth_url_payload_diagnostic(
                f"credential_scan_fixed_{suffix}",
                url,
                candidate,
                _contains_fixed_credential,
                decoder=decoder,
            )
        if _text_contains_bare_secret(candidate):
            return _oauth_url_payload_diagnostic(
                f"credential_scan_bare_secret_{suffix}",
                url,
                candidate,
                _text_contains_bare_secret,
                decoder=decoder,
            )

    # Provider consent URLs need neither path params nor fragments. Keep these
    # parser-differential forms fail-closed after the whole URL has been scanned.
    if parsed.params:
        return _oauth_diagnostic("path_params", "path_params", parsed.params)
    if ";" in parsed.path:
        return _oauth_diagnostic("path_semicolon", "path", parsed.path)
    if parsed.fragment:
        return _oauth_diagnostic("fragment", "fragment", parsed.fragment)

    path_and_query = parsed.path
    if parsed.query:
        path_and_query += f"?{parsed.query}"
    rules: list[str] = []
    warning = _exfil_url_warning(
        parsed.hostname,
        path_and_query,
        frozenset(),
        port=port,
        is_https=parsed.scheme.lower() == "https",
        allow_safe_presigned=False,
        allow_oauth_entropy=True,
        _rule_out=rules,
    )
    if warning is None:
        return None
    rule = rules[0] if rules else "exfil_unknown"

    heuristic_query = parsed.query
    if approved_endpoint:
        heuristic_query = "&".join(
            segment
            for segment in parsed.query.split("&")
            if segment.partition("=")[0] not in _OAUTH_QUERY_PARAMS
        )
    else:
        slack_alias = _kirocrew_slack_app_link_alias(
            parsed.hostname.lower(),
            parsed.path,
            parsed.query,
            is_https=parsed.scheme.lower() == "https",
            port=port,
        )
        if slack_alias is not None:
            heuristic_query = slack_alias

    if rule == "exfil_query_length":
        return _oauth_query_diagnostic(rule, heuristic_query)
    if rule == "exfil_query_pattern":
        query_decoder: Callable[[str], str] | None = (
            None if _EXFIL_PATTERNS.search(heuristic_query) else unquote_plus
        )
        return _oauth_query_diagnostic(
            rule,
            heuristic_query,
            predicate=lambda value: bool(_EXFIL_PATTERNS.search(value)),
            decoder=query_decoder,
        )
    if rule == "exfil_hard_credential":
        return _oauth_url_payload_diagnostic(
            rule,
            url,
            url,
            lambda value: bool(_HARD_CREDENTIAL_RE.search(value)),
        )
    if rule == "exfil_fixed_credential":
        return _oauth_url_payload_diagnostic(
            rule,
            url,
            url,
            _contains_fixed_credential,
        )
    if rule == "exfil_percent_encoding":
        return _oauth_url_payload_diagnostic(
            rule,
            url,
            url,
            lambda value: bool(_EXFIL_PERCENT_RE.search(value)),
        )

    target = url
    if rule in {"exfil_encoded_credential", "exfil_decode_saturated"}:
        for _ in range(_MAX_URL_DECODE_PASSES):
            decoded = unquote_plus(target)
            if decoded == target:
                break
            target = decoded
    return _oauth_diagnostic(rule, "url", target)


def oauth_url_contains_credential(url: str) -> bool:
    """Return True when an ACP-provided OAuth banner URL is unsafe."""
    diagnostic = diagnose_oauth_url_credential(url)
    if diagnostic is None:
        return False
    shape = diagnostic.shape
    logger.warning(
        "OAuth URL rejected rule=%s component=%s parameter=%s "
        "length=%d upper=%d lower=%d digits=%d percent=%d symbols=%d other=%d",
        diagnostic.rule,
        diagnostic.component,
        diagnostic.parameter or "-",
        shape.length,
        shape.ascii_uppercase,
        shape.ascii_lowercase,
        shape.digits,
        shape.percent_signs,
        shape.symbols,
        shape.other,
    )
    return True


# Data-egress / reverse-shell command shapes — the exfiltration-specific subset
# of SUSPICIOUS_BASH_PATTERNS. These are enforced at the
# tool-invocation gate (denied), unlike the full SUSPICIOUS_BASH_PATTERNS list
# which stays advisory: that list also carries destructive-but-local shapes
# (rm -rf, dd if=, chmod on system dirs, DROP TABLE) that a user may legitimately
# run in their own workspace, so hard-denying all of them at the gate would break
# ordinary use. This subset is narrowly the "push local data OUT / open a shell
# to a remote" shapes, where a hijacked-agent block is worth the rare false
# positive.
#
# Entries containing `*` are fnmatch globs (`*<pat>*`); the rest are
# case-insensitive substrings, so they fire regardless of intervening flags /
# token layout — `curl -d @f`, `curl -s -d @f`, `curl --data-binary @f` all
# match. The `@` sigil on curl body/upload flags means "read from a local file"
# (the tell-tale of egress); a bare `-d 'x=1'` inline body has no `@` and is not
# matched. curl long options accept BOTH ` @` and `=@` separators, so both are
# listed. `--data-raw` is deliberately EXCLUDED: it is the one --data variant
# that does NOT interpret a leading `@` as a file reference, so `--data-raw @x`
# posts the literal string `@x` (never reads a file) — including it would only
# add false positives. Multipart uploads use a glob (`-F *=@`) so ANY field name
# matches, not just a field literally named `file` (`curl -F x=@secret` exfils
# just as well).
_BASH_EXFIL_PATTERNS: list[str] = [
    "-d @",  # curl POST body read from a local file (space + `=` separators)
    "-d@",
    "-d=@",
    "--data @",
    "--data=@",
    "--data-binary @",
    "--data-binary=@",
    "--data-ascii @",
    "--data-ascii=@",
    "--data-urlencode @",  # also reads a local file when the value starts with @
    "--data-urlencode=@",
    "-F *=@",  # curl multipart file upload, any field name (glob)
    "--form *=@",
    "--upload-file",  # curl upload, long form
    "wget --post-file",  # wget file upload
    "/dev/tcp/",  # bash builtin reverse shell (>/dev/tcp/host/port)
    "/dev/udp/",
]

# Exfil shapes where whitespace or flag CASE around an operator matters, so a
# plain lowercased substring/glob would either miss a no-space variant or
# false-positive. Matched via regex against the ORIGINAL (non-lowercased)
# command. Each entry is (compiled pattern, human label).
_BASH_EXFIL_RES: list[tuple[re.Pattern[str], str]] = [
    # netcat reading a local file via input redirect — `nc host port < file` AND
    # `nc host port <file` (no space after `<`, a valid shell redirect that the
    # old `nc * < ` glob missed). `nc`/`ncat` is anchored at a word boundary so
    # `sync`/`func` etc. do not match. Case-insensitive (command name).
    (re.compile(r"(?:^|\s)nc(?:at)?\s+\S.*<", re.IGNORECASE), "nc/ncat file redirect"),
    # netcat reverse shell `nc -e <prog>` / `ncat -e <prog>`. `nc`/`ncat` is
    # anchored at a word boundary so `rsync -e ssh` (contains `nc -e`) and
    # `vnc -e` do NOT match; a plain substring `"nc -e"` false-positived on them.
    (re.compile(r"(?:^|\s)nc(?:at)?\s+-e\b", re.IGNORECASE), "nc/ncat reverse shell"),
    # curl upload short form `-T <file>` / `-Tfile` (no space). CASE-SENSITIVE
    # `-T`: curl's upload flag is uppercase, so this does NOT match lowercase long
    # options such as `--trace-time`. `-T` must begin at a word boundary.
    (re.compile(r"\bcurl\b.*(?:^|\s)-T\s*\S"), "curl -T upload"),
]


# Which catalog rule each always-on exfil branch enforces, so a denial maps back
# to a rule id and an operator opt-out is honoured. Patterns/labels absent from
# these maps stay unconditional.
_BASH_EXFIL_RULE_BY_PATTERN: dict[str, str] = {
    "-d @": "data-exfil-curl-file-body",
    "-d@": "data-exfil-curl-file-body",
    "-d=@": "data-exfil-curl-file-body",
    "--data @": "data-exfil-curl-file-body",
    "--data=@": "data-exfil-curl-file-body",
    "--data-binary @": "data-exfil-curl-file-body",
    "--data-binary=@": "data-exfil-curl-file-body",
    "--data-ascii @": "data-exfil-curl-file-body",
    "--data-ascii=@": "data-exfil-curl-file-body",
    "--data-urlencode @": "data-exfil-curl-file-body",
    "--data-urlencode=@": "data-exfil-curl-file-body",
    "-F *=@": "data-exfil-curl-multipart-upload",
    "--form *=@": "data-exfil-curl-multipart-upload",
    "--upload-file": "data-exfil-curl-upload",
    "wget --post-file": "data-exfil-wget-post-file",
    "/dev/tcp/": "reverse-shell-devtcp",
    "/dev/udp/": "reverse-shell-devtcp",
}

# A single regex can span more than one catalog row, so this maps to a TUPLE. The
# gate attributes each MATCH to one of those rows and honours that row's own
# toggle — see _exfil_rule_id_for_match.
_BASH_EXFIL_RULE_BY_LABEL: dict[str, tuple[str, ...]] = {
    "nc/ncat file redirect": ("data-exfil-nc-file-redirect",),
    "nc/ncat reverse shell": ("reverse-shell-nc", "reverse-shell-ncat"),
    "curl -T upload": ("data-exfil-curl-upload",),
}

#: For a label whose regex spans several catalog rows, the token that identifies
#: WHICH row a given match belongs to. Ordered longest-first so ``ncat`` is tested
#: before ``nc`` — the reverse would classify every ``ncat`` hit as ``nc``.
_BASH_EXFIL_ROW_DISCRIMINATORS: dict[str, tuple[tuple[str, str], ...]] = {
    "nc/ncat reverse shell": (("ncat", "reverse-shell-ncat"), ("nc", "reverse-shell-nc")),
}


def _exfil_rule_id_for_match(label: str, matched: str, rule_ids: tuple[str, ...]) -> str:
    """The catalog row a single exfil match belongs to.

    One regex can cover more than one row, and the operator toggles rows, not
    regexes — so a match has to be attributed before its toggle can be honoured.
    Falls back to the label's first row when nothing discriminates, which keeps the
    single-row labels (the common case) on their existing behaviour and never
    returns an id outside ``rule_ids``.
    """
    low = matched.lower()
    for token, rid in _BASH_EXFIL_ROW_DISCRIMINATORS.get(label, ()):
        if token in low and rid in rule_ids:
            return rid
    return rule_ids[0]


def audit_bash_exfiltration(
    command: str, *, enabled_ids: "frozenset[str] | None" = None
) -> str | None:
    """Return a denial reason if *command* matches a data-egress / reverse-shell
    shape that must be blocked at the tool-invocation gate, else None.

    Scoped to _BASH_EXFIL_PATTERNS / _BASH_EXFIL_RES (exfil/reverse-shell only) so
    it can be wired into the deny path in ``hooks.on_tool_call`` without blocking
    benign local commands. The broader :func:`audit_bash_command` stays advisory.

    Every branch carries the id of the catalog rule it enforces, so *enabled_ids*
    lets the caller honour an operator opt-out: a branch whose rule the operator
    disabled is skipped. ``None`` (the default) means ALL enabled — fail-closed,
    which is what keeps the callers that hold no effective set (cron command
    vetting, computer-use input vetting) at full strength without a change.
    """
    lower = command.lower()

    def _on(rule_id: str) -> bool:
        return enabled_ids is None or rule_id in enabled_ids

    for pattern in _BASH_EXFIL_PATTERNS:
        rule_id = _BASH_EXFIL_RULE_BY_PATTERN.get(pattern, "")
        if rule_id and not _on(rule_id):
            continue
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
        elif pat in lower:
            return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
    for rx, label in _BASH_EXFIL_RES:
        rule_ids = _BASH_EXFIL_RULE_BY_LABEL.get(label, ())
        if not rule_ids:
            if rx.search(command):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
            continue
        # A label can span more than one catalog row (one regex covers both the nc
        # and ncat rules). Denying while EITHER is enabled defeats the operator:
        # switching `reverse-shell-nc` off left `nc` blocked by its sibling. So
        # resolve each MATCH to the row it actually belongs to and honour that
        # row's own toggle. Every match is examined, not just the first, because a
        # command can carry both spellings and the leading one may be the disabled
        # row while the other is still enforced.
        for m in rx.finditer(command):
            matched_id = _exfil_rule_id_for_match(label, m.group(0), rule_ids)
            if _on(matched_id):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
        continue
    return None


# ── IP Canonicalization (IMDS bypass prevention) ──
# Attackers bypass IMDS checks by encoding 169.254.169.254 in alternate forms:
#   - Decimal:   2852039166 (single 32-bit integer)
#   - Hex:       0xa9fea9fe or 0xa9.0xfe.0xa9.0xfe
#   - Octal:     0251.0376.0251.0376
#   - IPv6-mapped: ::ffff:169.254.169.254 or ::ffff:a9fe:a9fe
#   - Mixed:     169.254.0xa9.0376
# canonicalize_ip converts ALL these to dotted-quad for uniform matching.


def canonicalize_ip(s: str) -> str:
    """Convert an IP address in any encoding to dotted-quad (a.b.c.d).

    Handles:
    - Standard dotted-quad (passthrough)
    - Single decimal integer (e.g. 2852039166)
    - Hex integer (e.g. 0xa9fea9fe)
    - Octal/hex per-octet (e.g. 0251.0376.0251.0376 or 0xa9.0xfe.0xa9.0xfe)
    - IPv6-mapped IPv4 (e.g. ::ffff:169.254.169.254 or ::ffff:a9fe:a9fe)

    Returns the dotted-quad string on success, or the original string unchanged
    if it cannot be parsed as an IP address.
    """
    s = s.strip()
    if not s:
        return s

    # Try IPv6-mapped IPv4: ::ffff:... forms
    if s.startswith("::ffff:") or s.startswith("::FFFF:"):
        try:
            addr = ipaddress.ip_address(s)
            if hasattr(addr, "ipv4_mapped") and addr.ipv4_mapped:
                return str(addr.ipv4_mapped)
            if isinstance(addr, ipaddress.IPv6Address):
                mapped = addr.ipv4_mapped
                if mapped:
                    return str(mapped)
        except (ValueError, AttributeError):
            pass

    # Try standard dotted-quad with possible hex/octal octets
    parts = s.split(".")
    if 1 <= len(parts) <= 4:
        octets: list[int] = []
        valid = True
        for part in parts:
            try:
                # Handle C-style octal (0NNN without 'o' prefix) which Python 3
                # int(x, 0) doesn't recognize. Must check before int(x, 0).
                if len(part) > 1 and part[0] == "0" and part[1:].isdigit():
                    # Could be octal (0251) or just "00" etc.
                    if all(c in "01234567" for c in part[1:]):
                        val = int(part, 8)
                    else:
                        # Has 8 or 9 -- not valid octal, treat as decimal
                        val = int(part)
                else:
                    # int() with base=0 handles: decimal, 0x hex
                    val = int(part, 0)
                octets.append(val)
            except (ValueError, OverflowError):
                valid = False
                break

        if valid:
            if len(octets) == 1:
                # Single integer: 2852039166 -> 4 octets
                val = octets[0]
                if 0 <= val <= 0xFFFFFFFF:
                    return str(ipaddress.IPv4Address(val))
            elif len(octets) == 4:
                # Four octets (each 0-255)
                if all(0 <= o <= 255 for o in octets):
                    return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}"
            elif len(octets) in (2, 3):
                # inet_aton "short" forms the OS resolver / curl accept but which
                # neither ipaddress nor the 1-/4-octet branches above canonicalize:
                #   a.b     -> a.(b as 24-bit)     e.g. 169.16689662  -> 169.254.169.254
                #   a.b.c   -> a.b.(c as 16-bit)   e.g. 169.254.43518 -> 169.254.169.254
                # Resolve them exactly as the OS does via inet_aton (which also
                # rejects out-of-range forms like 169.254.11207422), so an IMDS
                # SSRF cannot slip through in a 2-/3-part encoding. The last octet
                # carries the remaining low-order bytes, so a decimal/hex value up
                # to 0xFFFFFF (3-part) / 0xFFFFFFFF (2-part) is legal — validate the
                # leading octets are single bytes, then defer to inet_aton.
                if all(0 <= o <= 255 for o in octets[:-1]):
                    try:
                        return socket.inet_ntoa(socket.inet_aton(s))
                    except OSError:
                        pass

    # Try parsing as a plain integer (no dots) -- decimal or hex
    try:
        val = int(s, 0)
        if 0 <= val <= 0xFFFFFFFF:
            return str(ipaddress.IPv4Address(val))
    except (ValueError, OverflowError):
        pass

    # Try full ipaddress parsing as fallback
    try:
        addr = ipaddress.ip_address(s)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(addr)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
    except ValueError:
        pass

    return s


# ── IMDS Access Detection ──
# The AWS Instance Metadata Service at 169.254.169.254 (link-local) exposes
# IAM role credentials via /latest/meta-data/iam/security-credentials/.
# Any HTTP client (not just curl/wget) hitting this IP must be blocked.

# Regex to extract potential IP addresses from a command string.
# Captures dotted-quad, hex/octal per-octet, bare integers, IPv6-mapped forms.
# One component of a dotted literal, in EVERY base the C resolver accepts: hex
# (``0x..``), C-style octal (a leading ``0``) or decimal. A digit run covers
# octal and decimal alike, so leading zeros are admitted in EVERY position.
# Spelling the bases per-position (the previous form) meant a MIXED encoding
# such as ``169.254.0251.0376`` matched no branch whole, so the token reached
# ``canonicalize_ip`` TRUNCATED and folded to a harmless address while the OS
# resolver still routed the full token to IMDS.
#
# UNBOUNDED on purpose. A length cap here is not a safety measure, it is the
# very defect being fixed: any cap truncates a padded spelling of the same
# address into a DIFFERENT, harmless one, so the gate fails open on
# ``0x0a9fea9fe`` and ``169.254.0x00000000a9.0376`` (glibc ``inet_aton``
# accepts both and routes them to IMDS). These are plain character classes
# with no nested quantifier, so an unbounded run is linear -- bounding buys no
# ReDoS protection and costs the match. The canonicalizer stays the strict
# half (it returns the input unchanged for anything that is not a real
# address), so admitting more candidates can only ever ADD a denial.
_IP_COMPONENT = r"(?:0[xX][0-9a-fA-F]+|\d+)"
_IP_CANDIDATE_RE = re.compile(
    r"(?:"
    r"::ffff:[0-9a-fA-Fx.:]+|"  # IPv6-mapped
    r"[0-9a-fA-F]{1,4}:[0-9a-fA-F:]{2,}|"  # native IPv6 literal (colon run, e.g. fd00:ec2::254)
    # 2-, 3- and 4-part dotted forms, any base per component. The trailing
    # component of a 2-/3-part inet_aton "short" form packs the remaining
    # low-order bytes, so it must be captured WHOLE (not just the tail) for
    # canonicalize_ip to resolve it; the greedy repeat takes every component
    # present, so the full token always wins over a shorter prefix.
    rf"{_IP_COMPONENT}(?:\.{_IP_COMPONENT}){{1,3}}|"
    r"0[xX][0-9a-fA-F]+|"  # bare hex integer, unbounded (see _IP_COMPONENT)
    # Bare single-integer form. NOT capped: a zero-padded/octal spelling of the
    # same address is longer (``025177524776`` is IMDS), and a cap truncates it
    # into a different, harmless address.
    r"\d{7,}"
    r")"
)

_IMDS_IP = "169.254.169.254"
# Native IPv6 IMDS endpoint (dual-stack EC2). The IPv4 gate above misses this
# because canonicalize_ip returns native IPv6 unchanged; mirrors embeddings.py's
# SSRF gate which also blocks it (CWE-918 dual-stack parity).
_IMDS_IPV6 = "fd00:ec2::254"


def _check_imds_access(command: str, *, enabled_ids: "frozenset[str] | None" = None) -> str | None:
    """Detect attempts to access the IMDS endpoint via any encoding.

    Returns denial reason if IMDS access detected, None otherwise.

    Enforces ``credential-exfil-imds-any``, so *enabled_ids* lets the caller
    honour an operator opt-out of that rule. The two curl/wget IMDS rows are
    deliberately NOT consulted: they are verb-anchored and match only the literal
    dotted quad, so gating on them would silently narrow this check from "any verb,
    any encoding" to "curl or wget, literal IP". ``None`` means all enabled.
    """
    if enabled_ids is not None and "credential-exfil-imds-any" not in enabled_ids:
        return None
    # Quick reject: no IP-like candidate in command
    candidates = _IP_CANDIDATE_RE.findall(command)
    if not candidates:
        return None

    try:
        imds_v6: ipaddress.IPv6Address | None = ipaddress.ip_address(_IMDS_IPV6)  # type: ignore[assignment]
    except ValueError:  # pragma: no cover - constant is a valid literal
        imds_v6 = None
    for candidate in candidates:
        canonical = canonicalize_ip(candidate)
        if canonical == _IMDS_IP:
            # Found IMDS IP -- block regardless of tool since even echo
            # piped into nc could exfil credentials from the metadata service
            return (
                f"Blocked: command accesses IMDS endpoint "
                f"(169.254.169.254 via encoding '{candidate}')"
            )
        # Native IPv6 IMDS endpoint (fd00:ec2::254) — reachable over IPv6 on
        # dual-stack hosts; the IPv4 canonicalization above never matches it.
        # ipaddress equality normalizes compressed/expanded forms.
        if imds_v6 is not None:
            try:
                if ipaddress.ip_address(candidate.strip("[]")) == imds_v6:
                    return (
                        f"Blocked: command accesses IMDS endpoint "
                        f"(fd00:ec2::254 via '{candidate}')"
                    )
            except ValueError:
                pass
    return None
