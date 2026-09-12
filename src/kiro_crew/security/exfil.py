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
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse

from kiro_crew.credential_patterns import AWS_KEY_ID
from kiro_crew.sel import SecurityEvent, SecurityEventLog

from .redaction import _contains_fixed_credential, _text_contains_bare_secret
from .shell_normalizer import (
    _DATA_CONSUMER_PROGRAMS,
    _GLOB_CHARS_RE,
    BraceExpansionTooLarge,
    _argv_programs,
    _data_consumer_command_disqualified,
    _decode_shell_quoted_literals,
    _expand_brace_alternation,
    _glob_could_expand_to,
    _nested_shell_payloads,
    _pipes_into_evaluator,
    _program_basename,
    _shell_tokens,
    _split_unquoted_separators,
)

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
        # forbids adding one. Two were tried for the prefilled GitHub issue link —
        # one keyed to the validated SHAPE, one additionally pinned to this
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
        # EVERY host on its own merits (more than one host is reported this way)
        # — do not reintroduce a per-shape escape hatch. Pinned
        # by test_redaction_mirror_parity.py::TestPrefilledIssueCarveOutParity.
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
#: substitutions (the dashboard chat notice) prefix-counts THIS
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
# The gate is a deny gate over TWO views of the same command, and the views
# only ever ADD denials:
#
#     raw command text ──────► raw matchers ──────┐
#                                                 ├─► any hit ⇒ DENY
#     per-segment argv view ──► capability ───────┘
#     (shell-normalized)        matchers
#
# Raw matching runs first and covers the legacy/simple shapes — one scan, no
# tokenizing. The normalized view closes what re-spelling opens: a shell
# deletes quotes, empty-string splices and escaping backslashes before the
# program runs, so `--in''put` reaches gh as `--input`, `g\h` invokes gh, and
# `curl --data-bin''ary @f` posts a file — none of which the raw text shows.
# CLI-shaped rules therefore match the NORMALIZED view, where the command's
# real argv lives; the raw branch stays only for shapes that are not
# argv-shaped (the nc/ncat redirects and the /dev/tcp/ //dev/udp/ builtins).
# Both paths resolve every hit through the catalog (`_on`), so an operator
# opt-out governs either view, and a normalized pass that fails costs the
# pass, never the raw verdict — no command raw matching denies can stop being
# denied. Matching stays near-linear: the raw regexes are tempered, and the
# normalized pass is a fixed number of linear token walks per segment.

#: Which view of a command a rule matches: the text as written, or the
#: shell-normalized per-segment argv view (:func:`audit_bash_exfiltration`).
RAW_VIEW = "raw"
NORMALIZED_VIEW = "normalized"


class ExfilRule(NamedTuple):
    """One data-egress capability and every spelling family that expresses it.

    *views* declares which of the gate's two views the rule matches, and the
    matcher for each view lives right here on the rule: ``raw_glob`` (an
    fnmatch glob when it carries ``*``, else a case-insensitive substring) and
    ``raw_re`` (matched against the ORIGINAL text) for the raw view;
    ``token_match`` (a predicate over one segment's argv tokens plus the
    invocation map) for the normalized view. Declaring the view ON the rule is
    what retires the parallel label lists — a new rule cannot forget to say
    which view needs it, and a rule with a ``token_match`` automatically rides
    the same segment/tokenizing pass as every other normalized rule.
    """

    label: str
    rule_ids: "tuple[str, ...]"
    views: "frozenset[str]"
    raw_glob: "str | None" = None
    raw_re: "re.Pattern[str] | None" = None
    #: Substring raw matchers (legacy `_BASH_EXFIL_PATTERNS` entries): each is
    #: compiled with IGNORECASE and matched against the original text. A rule
    #: may carry both ``raw_re`` and ``raw_res`` — any hit denies. Case
    #: semantics are PER MATCHER: each pattern chooses IGNORECASE or
    #: case-sensitivity to match the capability it enforces (curl short
    #: flags are case-sensitive; wget long options are not).
    raw_res: "tuple[re.Pattern[str], ...] | None" = None
    row_discriminators: "tuple[tuple[str, str], ...]" = ()
    token_match: "Callable[[list[str], dict[str, list[int]]], bool] | None" = None


#: The programs whose CLI surface carries a file-egress capability this gate
#: enforces. The normalized pass only tokenizes segments where one of these
#: names appears, so ordinary commands pay no tokenizer cost.
_EXFIL_PROGRAMS = ("curl", "gh", "wget")

#: The ``gh`` subcommands that accept ``--input`` / ``--field``.

#: gh's persistent global flags that consume the NEXT token as their value and
#: may legitimately sit between the program and its subcommand (``gh --hostname
#: corp api …``, ``gh -t 30 api …``). The anchor walk skips each flag together
#: with its value; a flag glued with ``=`` carries the value in the same token.
#: Anything else the walk cannot recognize still anchors gh — fail closed.

#: curl's LONG body flags whose ``@``-sigil value names a LOCAL file. ``--data-raw``
#: is deliberately ABSENT: it is the one --data variant that does NOT interpret
#: a leading ``@`` as a file reference, so ``--data-raw @x`` posts the literal
#: string ``@x`` (never reads a file) — matching it would only add false
#: positives. The ``@`` sigil is the tell-tale of egress; a bare ``-d 'x=1'``
#: inline body has no ``@`` and is not matched. The short flag ``-d`` lives in
#: the matcher itself, because curl's SHORT flags are case-sensitive (see
#: :func:`_curl_body_file_tokens`).
_CURL_BODY_FILE_LONG_FLAGS = ("--data", "--data-binary", "--data-ascii", "--data-urlencode")

#: A short-option cluster ENDING in curl's upload flag (`-sT`): the ``T``
#: consumes the next argv word as its file operand. CASE-SENSITIVE — curl's
#: upload flag is uppercase, and folding in a lowercase ``-t``/``-f`` would
#: match unrelated options (`sort -f`, `curl --trace-time`).
_CURL_T_CLUSTER_RE = re.compile(r"-[A-Za-z]*T\Z")
#: Cluster with the upload flag GLUED to its operand (`-sTfile`, `-Tfile`):
#: curl runs `T file` — the letters after T are value text, not flags.
_CURL_T_GLUED_RE = re.compile(r"-[A-Za-z]*T\S+")

#: Same cluster shape for curl's multipart flag (`-sF`). CASE-SENSITIVE like
#: ``-T``: ``-F`` is curl's uppercase short flag, and matching a lowercase
#: ``-f`` (`sort -f … @x`, `curl -f @file`) would false-positive.
_CURL_F_CLUSTER_RE = re.compile(r"-[A-Za-z]*F\Z")
#: Cluster with the multipart flag GLUED to its value (`-sFk=@f`): same
#: value-text reading as the `-T` glued form.
_CURL_F_GLUED_RE = re.compile(r"-[A-Za-z]*F[^\s=]*(?:=@|=<)")

#: The body flag GLUED to its ``@``-sigil value, short flag or short-option
#: cluster (``-d@f``, ``-d=@f``, ``-sd@f``). Anchored at the token's START —
#: an option cluster begins there, while a URL or a value carrying ``-d@``
#: mid-word is data, not a flag. Matched against the RAW token so the trailing
#: ``d`` is CASE-SENSITIVE: curl's ``-D`` dumps response headers to a file and
#: reads no body, so ``-D@hdrs`` must not fold into ``-d@f``.
_CURL_D_GLUED_RE = re.compile(r"-[A-Za-z]*d(?:=@|@)")

#: A short-option cluster that ENDS in the body flag (``-d``, ``-sd``): the
#: non-value flags before it consume nothing, so curl reads the body from the
#: NEXT token (``-sd @f``). Trailing ``d`` is CASE-SENSITIVE like the glued
#: spelling — a cluster ending in ``-D`` writes headers, it reads no body.
_CURL_D_CLUSTER_RE = re.compile(r"-[A-Za-z]*d\Z")


def _glob_admitted_programs(base: str) -> "list[str]":
    """The exfil programs a glob- or brace-spelled program *base* could name.

    The shell resolves a glob or brace group in the PROGRAM NAME before exec
    (``cur[l]`` runs curl, ``c{url,at}`` runs curl or cat), so a literal
    comparison records no invocation and the capability behind the name goes
    unguarded. Admissibility is decided with
    ``shell_normalizer._glob_could_expand_to`` for the bracket/asterisk/
    question classes; brace ALTERNATION is expanded exactly (``_expand_brace_
    alternation``), because the shared glob helper folds a brace group to
    ``.*`` — coarse enough to admit ``cat`` from ``c{url,at}`` but not
    ``curl``. The alternation expansion is bounded: a pattern whose product
    would blow past the budget raises, and the overflow reads fail-closed
    (every program admitted), never as "expands to nothing". A base with no
    glob characters at all admits nothing, which is the branch every ordinary
    token takes.
    """
    if not _GLOB_CHARS_RE.search(base):
        return []
    names: "list[str]" = []
    try:
        variants = _expand_brace_alternation(base)
    except BraceExpansionTooLarge:
        # Fail closed: an expansion past the budget (a stacked brace token
        # that would materialize millions of strings) is never allowed to
        # read as "expands to nothing" — that reading is the bypass. Treat
        # the name as admitting every exfil program and let the matchers
        # decide; the command pays for the scrutiny, not the expansion.
        return list(_EXFIL_PROGRAMS)
    for name in _EXFIL_PROGRAMS:
        if _glob_could_expand_to(base, (name,)):
            names.append(name)
            continue
        for variant in variants:
            if variant.lower() == name or _glob_could_expand_to(variant, (name,)):
                names.append(name)
                break
    return names


def _exfil_invocations(tokens: "list[str]") -> "dict[str, list[int]]":
    """Token indexes where one of :data:`_EXFIL_PROGRAMS` is actually INVOKED.

    A program token names an invocation when it sits in COMMAND position for
    its command: the boundary walk attributes every token to the program it
    belongs to, skipping leading ``VAR=value`` assignments. A program name in
    ARGUMENT position is a mention, not an execution — ``echo gh api --input
    secret.json`` prints words, it runs nothing — so the data-consumer
    exemption keeps such text inert. The exemption is withdrawn wherever the
    words could still run: the command pipes into a shell or evaluator, a
    substitution occupies program position, or a ``$( … )`` / backtick
    substitution sits anywhere in the segment (its output becomes argv, so a
    program spelled inside it really executes). Fail-closed the other way too:
    a non-empty owner that is NOT a known data consumer (``sudo gh …``, ``ssh
    host gh …``) counts as an invocation, and a glob- or brace-spelled program
    name (``cur[l]``, ``g[h]``, ``c{url,at}``) counts as an invocation of every
    program it could expand to — the shell resolves the name before exec, so
    expandability, not literal equality, is what admits it — and an
    expansion-held name (``$a`` after ``a=curl``) admits every program, since
    which one runs is unknowable from the text.
    """
    programs = _argv_programs(tokens)
    out: "dict[str, list[int]]" = {name: [] for name in _EXFIL_PROGRAMS}
    disqualified = _data_consumer_command_disqualified(tokens)
    substituted = any("$(" in tok or "`" in tok for tok in tokens)
    for i, tok in enumerate(tokens):
        owner = programs[i].lower()
        if not owner:
            # Past a comment marker or another argv-terminating boundary: the
            # shell never executes it, so it is text, not a command.
            continue
        raw_base = _program_basename(tok)
        base = raw_base.lower()
        if base in out:
            names = [base]
        elif "$" in raw_base:
            # An expansion-held program name (`a=curl; $a -d @f …`) resolves at
            # runtime to a name this static walk cannot read, and the glob
            # classes do not cover `$` — so without this branch the token
            # admits nothing and the invocation check goes quiet over a
            # command that runs one of these programs. Fail closed: the name
            # could be any of them, so every program's matchers get a vote.
            names = list(_EXFIL_PROGRAMS)
        else:
            names = _glob_admitted_programs(raw_base)
            if not names:
                continue
        for name in names:
            if (
                owner != name
                and owner in _DATA_CONSUMER_PROGRAMS
                and not disqualified
                and not substituted
            ):
                continue
            out[name].append(i)
    return out


def _curl_body_file_tokens(tokens: "list[str]", invocations: "dict[str, list[int]]") -> bool:
    """curl reading a POST body from a LOCAL FILE (the ``@`` sigil).

    One walk covers every documented spelling: glued (``-d@f``, ``-d=@f``,
    including inside a short-option cluster, ``-sd@f``), space-separated
    (``-d @f``, ``--data-binary @f`` — curl long options accept BOTH `` @``
    and ``=@`` separators), and ``=``-joined (``--data=@f``). Matched on the
    argv view, so the shell has already resolved quoting — ``--data-bin''ary
    @f`` arrives here as ``--data-binary``. Short flags compare RAW and long
    flags lowercased: curl's short flags are CASE-SENSITIVE, and ``-D`` (dump
    response headers to a file — it writes, never reads a body) must not fold
    into ``-d`` and deny ``curl -D @hdrs`` for a capability it does not have.
    """
    if not invocations["curl"]:
        return False
    for i, tok in enumerate(tokens):
        # Short flags are case-sensitive; compare the raw token. A cluster
        # ending in the body flag reads its operand from the next token.
        if tok == "-d" or _CURL_D_CLUSTER_RE.fullmatch(tok):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt.startswith("@") or nxt.startswith("=@"):
                return True
        elif _CURL_D_GLUED_RE.match(tok):
            return True
        # Long options: curl accepts any UNAMBIGUOUS prefix of a long option
        # (`--data-bin` runs as `--data-binary`), so canonicalize the token
        # against the body-file flag table before the folded comparison.
        # A token spelling `=`-joined keeps its value attached.
        low = tok.lower()
        canonical = low
        if low.startswith("--") and low not in _CURL_BODY_FILE_LONG_FLAGS:
            name, sep, value = low.partition("=")
            matches = [f for f in _CURL_BODY_FILE_LONG_FLAGS if f.startswith(name)]
            if len(matches) == 1:
                canonical = matches[0] + sep + value
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        for flag in _CURL_BODY_FILE_LONG_FLAGS:
            if canonical.startswith(flag + "=@"):
                return True
        if canonical in _CURL_BODY_FILE_LONG_FLAGS and (
            nxt.startswith("@") or nxt.startswith("=@")
        ):
            return True
        # --data-urlencode reads the file named after the field's ``@``
        # (``--data-urlencode name@f`` / ``--data-urlencode=name@f``); the
        # name-only and name=value spellings read no file, so the ``@``
        # sigil decides.
        if low.startswith("--data-urlencode="):
            if "@" in low[len("--data-urlencode=") :]:
                return True
        elif low == "--data-urlencode":
            if "@" in nxt:
                return True
    return False


def _curl_upload_tokens(tokens: "list[str]", invocations: "dict[str, list[int]]") -> bool:
    """curl pushing a LOCAL FILE to a remote (``-T <file>`` / ``--upload-file``).

    Covers the long flag glued or space-separated (``--upload-file=f``,
    ``--upload-file f``), the bare short flag with its operand as the next
    token, the glued short spelling (``-Tfile``) and short-option clusters
    (``-sT file``). ``-T`` matching is CASE-SENSITIVE — lowercase long options
    such as ``--trace-time`` and short clusters ending in ``-t`` are unrelated
    options. A ``-T`` with no operand uploads nothing and is not matched,
    mirroring the operand requirement of the raw spelling.
    """
    if not invocations["curl"]:
        return False
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low == "--upload-file" or low.startswith("--upload-file="):
            return True
        if tok.startswith("-T"):
            if len(tok) > 2 or i + 1 < len(tokens):
                return True
        elif _CURL_T_GLUED_RE.match(tok):
            return True  # glued cluster: `-sTfile` uploads `file`
        elif _CURL_T_CLUSTER_RE.fullmatch(tok) and i + 1 < len(tokens):
            return True
    return False


def _curl_multipart_tokens(tokens: "list[str]", invocations: "dict[str, list[int]]") -> bool:
    """curl / gh multipart upload reading a field from a LOCAL FILE.

    The two tools whose flag this is — gh's ``-F field=@file`` is the same
    upload — so the match is scoped to their invocations and a bare
    ``-Fk=@f`` in some other program's argv (``echo -Fk=@f``) stays inert.
    One walk covers the space-separated (``-F key=@f`` — ANY field name,
    including curl's SPACED names behind quotes, ``-F 'foo bar=@f'``, which
    ``shlex`` fuses into one token), ``=``-joined (``--form=k=@f``,
    ``--form="k=@f"``) and glued (``-Fk=@f``) spellings, plus short-option
    clusters (``-sF 'k=@f'``). ``--form-string`` is EXCLUDED — it never reads
    a file (``--form-string k=@f`` posts the literal string ``@f``). ``-F``
    matching is CASE-SENSITIVE like curl's flag; a lowercase ``-f`` is the
    fail flag, not a form field.
    """
    if not (invocations["curl"] or invocations["gh"]):
        return False
    for i, tok in enumerate(tokens):
        if tok.startswith("--form-string"):
            continue
        if tok == "--form" or tok == "-F":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if "=@" in nxt or "=<" in nxt:
                return True
        elif tok.startswith("--form=") or tok.startswith("-F"):
            if "=@" in tok or "=<" in tok:
                return True
        elif _CURL_F_GLUED_RE.match(tok):
            return True  # glued cluster: `-sFk=@f` uploads `f`
        elif _CURL_F_CLUSTER_RE.fullmatch(tok):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if "=@" in nxt or "=<" in nxt:
                return True
    return False


def _gh_body_file_tokens(tokens: "list[str]", invocations: "dict[str, list[int]]") -> bool:
    """gh reading a request body / field from a LOCAL FILE.

    Scoped to a gh INVOCATION followed by one of the ``gh`` subcommands that
    accepts the flags (``api``/``repo``/``release``) — the boundary walk and
    program-name peel make that decision on the argv view, so path-qualified
    (``/usr/bin/gh``, ``./gh``), escaped (``g\\h``), quote-spliced (``g''h``)
    and substitution-wrapped (``$(gh …)``, backticks) spellings all resolve to
    the same invocation, while a bare ``gh`` inside a word (``xgh``, ``nightly
    gh-sync``, ``hover api``) or quoted decoy (``-H "xgh api"``) names no
    invocation at all. The anchor tolerates gh's persistent global flags (and
    their values) between program and subcommand — ``gh --hostname corp api …``
    anchors like ``gh api …`` — and any unrecognized word there anchors too,
    fail-closed.

    ``--input`` denies unless its value is EXACTLY a lone ``-`` — stdin, which
    reads no local file. The shell deletes quotes before gh sees the value, so
    ``'-'`` arrives here as ``-`` and stays allowed, while ``'- secret.json'``
    — a real file whose name starts with ``- `` — and any longer dash-leading
    value (``--input=-body.json``) deny. ``--field`` denies when its value
    carries the ``=@`` file sigil; the argv view preserves gh's SPACED field
    specs behind quotes (``--field 'foo bar=@f'``) as one fused token, which a
    raw scan halting at the space cannot reach.
    """
    # Scan only each gh SUBCOMMAND invocation window (gh api/repo/release …):
    # `gh --input x` is not a gh capability (`gh extension exec demo --input x`
    # passes --input to the extension, not to gh), so the flag scan must not
    # reach past the invoked subcommand's own argument list. Each window ends
    # at the next program word; windows are disjoint, so one linear pass over
    # every window stays linear overall.
    # Every invocation's window ends at the segment end, so the EARLIEST
    # invocation that reaches a recognized subcommand defines the single scan
    # start: `--input`/`--field` anywhere in the shared tail is within that
    # invocation's reach, and one pass over the tail stays linear no matter
    # how many `gh api` anchors repeat (the 16k-anchor watchdog shape).
    scan_start = None
    for gh_i in invocations["gh"]:
        j = gh_i + 1
        while j < len(tokens) and tokens[j].lower() not in _GH_SUBCOMMANDS:
            j += 1
        if j < len(tokens):
            scan_start = j + 1
            break
    if scan_start is None:
        return False  # no recognized subcommand -> no gh file-body capability
    end = len(tokens)
    for i in range(scan_start, end):
        tok = tokens[i]
        low = tok.lower()
        if low == "--input" or low == "--input=":
            # An empty value reads nothing; the lone dash is stdin.
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
            if value and value != "-":
                return True
        elif low.startswith("--input="):
            if low[len("--input=") :] != "-":
                return True
        if low == "--field" or low == "--field=":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if "=@" in nxt:
                return True
        elif low.startswith("--field="):
            if "=@" in low[len("--field=") :]:
                return True
    return False


def _wget_post_file_tokens(tokens: "list[str]", invocations: "dict[str, list[int]]") -> bool:
    """wget uploading a LOCAL FILE as the POST body (``--post-file``)."""
    if not invocations["wget"]:
        return False
    # wget accepts unambiguous long-option prefixes (`--post-f=file` runs as
    # `--post-file=file`), so a token is a match if the option text before
    # any `=` is an unambiguous prefix of `--post-file`.
    for tok in tokens:
        low = tok.lower()
        name, sep, _value = low.partition("=")
        if name == "--post-file" or (name.startswith("--") and "--post-file".startswith(name)):
            return True
    return False


#: Every data-egress capability the gate enforces, in evaluation order: the
#: raw view first (globs before regexes), then the normalized view. Each rule
#: names the catalog rows it enforces so an operator opt-out is honoured on
#: both paths — see :func:`audit_bash_exfiltration`.
#: gh subcommands whose argument lists accept `--input`/`--field` file bodies.
_GH_SUBCOMMANDS = frozenset({"api", "repo", "release"})

#: Basenames whose glob-spelled variants name NO command-capable program:
#: `curl[0-9]` expands to curl0..curl9 — none of them reads `-d @f` as a
#: capability, so the raw tier's flag shapes would only false-positive.
_GLOB_INERT_BASENAMES = ("curl", "wget", "echo", "cat", "ls")


def _glob_known_inert(word: str) -> bool:
    """True if a glob-spelled word provably names a NON-capability program.

    Fail-closed by design: a glob word whose literal prefix is NOT a known
    inert program (`/usr/bin/python[0-9]`, `ba[s]h`, unknown wrappers) keeps
    the raw tier live — the wrapper may execute its payload, so every raw
    matcher still runs.
    """
    basename = word.rsplit("/", 1)[-1]
    if "[" not in basename:
        return False
    prefix = basename.split("[", 1)[0].lower()
    return prefix in _GLOB_INERT_BASENAMES


def fnmatch_glob_regex(pattern: str) -> str:
    """Compile a base-catalog fnmatch glob to an equivalent regex.

    The base exfil catalog expressed multi-spelling shapes as fnmatch globs
    (`"-F *=@"` = any field name, ` @` separator). Carrying them verbatim as
    regexes keeps the raw view's reach identical to base: `*` becomes `.*`,
    every other character matches literally.
    """
    return ".*".join(re.escape(part) for part in pattern.split("*"))


_EXFIL_RULES: "tuple[ExfilRule, ...]" = (
    ExfilRule(
        label="reverse shell /dev/tcp",
        rule_ids=("reverse-shell-devtcp",),
        views=frozenset({RAW_VIEW}),
        raw_glob="/dev/tcp/",
    ),
    ExfilRule(
        label="reverse shell /dev/udp",
        rule_ids=("reverse-shell-devtcp",),
        views=frozenset({RAW_VIEW}),
        raw_glob="/dev/udp/",
    ),
    ExfilRule(
        # netcat reading a local file via input redirect — `nc host port < file`
        # AND `nc host port <file` (no space after `<`, a valid shell redirect).
        # `nc`/`ncat` is anchored at a word boundary so `sync`/`func` etc. do
        # not match. Case-insensitive (command name).
        label="nc/ncat file redirect",
        rule_ids=("data-exfil-nc-file-redirect",),
        views=frozenset({RAW_VIEW}),
        raw_re=re.compile(r"(?:^|\s)nc(?:at)?\s+\S.*<", re.IGNORECASE),
    ),
    ExfilRule(
        # netcat reverse shell `nc -e <prog>` / `ncat -e <prog>`, word-boundary
        # anchored so `rsync -e ssh` and `vnc -e` do not match.
        label="nc/ncat reverse shell",
        rule_ids=("reverse-shell-nc", "reverse-shell-ncat"),
        views=frozenset({RAW_VIEW}),
        raw_re=re.compile(r"(?:^|\s)nc(?:at)?\s+-e\b", re.IGNORECASE),
        row_discriminators=(("ncat", "reverse-shell-ncat"), ("nc", "reverse-shell-nc")),
    ),
    ExfilRule(
        label="curl POST body from file (-d/--data)",
        rule_ids=("data-exfil-curl-file-body",),
        # RAW view kept alongside normalized: a nested shell payload
        # (`bash -c 'curl -d @secret …'`) never reaches command position in
        # the outer argv, so the raw substring matcher is the only layer that
        # still sees it. The match is anchored to a literal `curl` program
        # word. The substring set is main's legacy curl-body pattern list
        # (`-d @`, `--data=@`, …), which matches the payload text regardless
        # of command position. Both views deny; either hit refuses.
        views=frozenset({RAW_VIEW, NORMALIZED_VIEW}),
        # CASE-SENSITIVE on purpose: curl's short flags are case-sensitive and
        # `-D` dumps response HEADERS (writes, never reads a body) — folding it
        # in via IGNORECASE (base's substring behaviour) denied
        # `curl -D @hdrs` for a capability it does not have. The lowercased
        # glob view below keeps the case-insensitive long-form reach.
        raw_res=tuple(
            re.compile(re.escape(p))
            for p in (
                "-d @",
                "-d@",
                "-d=@",
                "--data @",
                "--data=@",
                "--data-binary @",
                "--data-binary=@",
                "--data-ascii @",
                "--data-ascii=@",
                "--data-urlencode @",
                "--data-urlencode=@",
            )
        ),
        token_match=_curl_body_file_tokens,
    ),
    ExfilRule(
        label="curl file upload (-T/--upload-file)",
        rule_ids=("data-exfil-curl-upload",),
        views=frozenset({RAW_VIEW, NORMALIZED_VIEW}),
        # Base's raw regex carried verbatim (CASE-SENSITIVE `-T`, no `@`
        # requirement — `curl -T f url` uploads a named file just as well):
        raw_re=re.compile(r"\bcurl\b.*(?:^|\s)-T\s*\S"),
        raw_res=(re.compile(r"(?:^|\s)--upload-file[ =]", re.IGNORECASE),),
        token_match=_curl_upload_tokens,
    ),
    ExfilRule(
        label="curl/gh multipart file upload (-F/--form)",
        rule_ids=("data-exfil-curl-multipart-upload",),
        views=frozenset({RAW_VIEW, NORMALIZED_VIEW}),
        # Base fnmatch globs ("-F *=@", "--form *=@") carried verbatim:
        # any field name, ` @` separator required (the local-file sigil).
        raw_res=(
            # Case-sensitive: curl short flags are case-sensitive, and an
            # IGNORECASE `-f` match would deny benign `curl -f k=@x`.
            re.compile(fnmatch_glob_regex("-F *=@")),
            re.compile(fnmatch_glob_regex("--form *=@")),
        ),
        token_match=_curl_multipart_tokens,
    ),
    # Same catalog row as curl's `-d`/`--data` spellings: gh reading a request
    # body from a LOCAL FILE is the same capability — a body pulled off disk
    # and pushed to a remote — so one toggle governs both.
    ExfilRule(
        label="gh request body from file (--input / --field)",
        rule_ids=("data-exfil-curl-file-body",),
        # Normalized-only BY DESIGN (tests pin `gh api --input -`, quoted
        # stdin, and non-gh `--input` as allowed): the raw layer cannot
        # express the anchor+flag+`@`-value co-occurrence without re-running
        # the tokenizer, so nested-shell gh payloads are covered by keeping
        # the gh program in _EXFIL_PROGRAMS for the invocation scan.
        views=frozenset({NORMALIZED_VIEW}),
        token_match=_gh_body_file_tokens,
    ),
    ExfilRule(
        label="wget file upload (--post-file)",
        rule_ids=("data-exfil-wget-post-file",),
        views=frozenset({RAW_VIEW, NORMALIZED_VIEW}),
        raw_res=(re.compile(r"(?:^|\s)--post-file\b", re.IGNORECASE),),
        token_match=_wget_post_file_tokens,
    ),
)


# A single raw regex can span more than one catalog row, so each rule's ids are
# a TUPLE. The gate attributes each MATCH to one of those rows and honours that
# row's own toggle — see _exfil_rule_id_for_match.


def _exfil_rule_id_for_match(label: str, matched: str, rule_ids: tuple[str, ...]) -> str:
    """The catalog row a single exfil match belongs to.

    One regex can cover more than one row, and the operator toggles rows, not
    regexes — so a match has to be attributed before its toggle can be honoured.
    Falls back to the label's first row when nothing discriminates, which keeps
    the single-row labels (the common case) on their existing behaviour and
    never returns an id outside ``rule_ids``.
    """
    for rule in _EXFIL_RULES:
        if rule.label != label:
            continue
        low = matched.lower()
        # Ordered longest-first so ``ncat`` is tested before ``nc`` — the
        # reverse would classify every ``ncat`` hit as ``nc``.
        for token, rid in rule.row_discriminators:
            if token in low and rid in rule_ids:
                return rid
        break
    return rule_ids[0]


_CARRIER_RECURSION_LIMIT = 8
_CARRIER_PAYLOAD_BUDGET = 64


def audit_bash_exfiltration(
    command: str, *, enabled_ids: "frozenset[str] | None" = None
) -> str | None:
    """Public deny-gate entry: see the inner implementation."""
    try:
        return _audit_bash_exfiltration(
            command, enabled_ids=enabled_ids, _depth=0, _budget=_CARRIER_PAYLOAD_BUDGET
        )
    except Exception:
        # The gate's contract is infallible: a caller (tool approval) must
        # receive a VERDICT, never an exception. Fail closed.
        return "Blocked: command matches data-exfiltration pattern (carrier audit budget exhausted)"


def _audit_bash_exfiltration(
    command: str,
    *,
    enabled_ids: "frozenset[str] | None" = None,
    _depth: int = 0,
    _budget: int = _CARRIER_PAYLOAD_BUDGET,
) -> str | None:
    """Return a denial reason if *command* matches a data-egress / reverse-shell
    shape that must be blocked at the tool-invocation gate, else None.

    The deny gate over the module's two views (see the block comment on
    :data:`_EXFIL_RULES`): the raw text matches first and returns immediately;
    the shell-normalized per-segment argv view can only ADD denials. Scoped to
    the exfil/reverse-shell rules so it can be wired into the deny path in
    ``hooks.on_tool_call`` without blocking benign local commands. The broader
    :func:`audit_bash_command` stays advisory.

    Every rule carries the id of the catalog row(s) it enforces, so
    *enabled_ids* lets the caller honour an operator opt-out: a rule whose row
    the operator disabled is skipped on BOTH views. ``None`` (the default)
    means ALL enabled — fail-closed, which is what keeps the callers that hold
    no effective set (cron command vetting, computer-use input vetting) at
    full strength without a change.
    """
    lower = command.lower()
    if _depth > _CARRIER_RECURSION_LIMIT or _budget <= 0:
        # Budget exhausted on adversarially nested carriers: fail closed.
        return (
            "Blocked: command matches data-exfiltration pattern "
            "(carrier nesting beyond audit budget)"
        )

    def _on(rule_id: str) -> bool:
        return enabled_ids is None or rule_id in enabled_ids

    # 1. Raw view: the text as written. Globs/substrings first, then regexes —
    # every match resolves to its catalog row before its toggle is honoured. A
    # label can span more than one row (one regex covers both the nc and ncat
    # rules): denying while EITHER is enabled would defeat the operator, so
    # each match is attributed and every match is examined, not just the
    # first, because a command can carry both spellings and the leading one
    # may be the disabled row while the other is still enforced.
    for rule in _EXFIL_RULES:
        if RAW_VIEW not in rule.views:
            continue
        if rule.raw_glob is not None:
            pat = rule.raw_glob.lower()
            hit = ("*" in pat and fnmatch.fnmatch(lower, f"*{pat}*")) or pat in lower
            if hit and _on(rule.rule_ids[0]):
                return f"Blocked: command matches data-exfiltration pattern '{rule.raw_glob}'"
        elif rule.raw_re is not None:
            for m in rule.raw_re.finditer(command):
                matched_id = _exfil_rule_id_for_match(rule.label, m.group(0), rule.rule_ids)
                if _on(matched_id):
                    return f"Blocked: command matches data-exfiltration pattern ({rule.label})"
        for rx in rule.raw_res or ():
            # A glob-spelled PROGRAM word (`curl[0-9]`) belongs to the
            # normalized pass; the substring view would misread it as a
            # literal curl — UNLESS the glob names a shell carrier
            # (`/bin/ba[s]h -c '…'`), whose quoted payload still needs the
            # raw-tier recursion downstream. Suppress the raw skip only for
            # such carrier programs; a glob in a later argument is unrelated.
            first_word = command.split(None, 1)[0] if command.strip() else ""
            if _GLOB_CHARS_RE.search(first_word) and _glob_known_inert(first_word):
                break
            if rx.search(command):
                matched_id = _exfil_rule_id_for_match(rule.label, rx.pattern, rule.rule_ids)
                if _on(matched_id):
                    return f"Blocked: command matches data-exfiltration pattern ({rule.label})"

    # 2. Normalized view. A shell deletes quotes, empty-string splices and
    # escaping backslashes before the program runs, so `--in''put` reaches gh
    # as `--input` while the raw scan reads the `''` and moves on — a spelling
    # gap that turns a guardrail into a suggestion. Each rule's token_match
    # runs against the per-segment argv view _shell_tokens renders
    # (CASE-PRESERVED, because the `-F`/`-T` rules are deliberately
    # case-sensitive and a lowercased view would deny `sort -f … @x`). The
    # segment split is the QUOTE-AWARE one: a separator inside quotes is data
    # (`-H "X:a&b"` is one header), so splitting on it would cut the `gh`
    # invocation apart from the flag that follows and rebuild two views
    # neither of which matches — the spelling gap would just move rather than
    # close. A pass that fails costs the view, never the raw verdict — no
    # command the raw view denies can stop being denied. Gated on the
    # programs' anchors so ordinary commands pay no tokenizer cost.
    #
    # Backslash is stripped alongside the quotes for the same reason the gate
    # runs at all: a shell drops an escaping backslash before the program runs
    # (`g\h` invokes gh, `c\url` invokes curl), so a backslash-escaped program
    # name must open this gate exactly like a quote-spliced one. Stripping is
    # a deny-gate over-approximation: it can only open the gate on MORE
    # commands, never close it on one the quote-only view admitted.
    # The prefilter reads the DECODED text, not the literal one: an ANSI-C or
    # locale-spelled program name ($'\x63url' runs curl) carries no program
    # substring until the shell's quote decoding is applied, and skipping the
    # normalized pass over it would drop a deny the raw gate misses.
    stripped = (
        _decode_shell_quoted_literals(command)
        .lower()
        .replace("'", "")
        .replace('"', "")
        .replace("\\", "")
    )
    # Line continuations (`g\<newline>h`) hide the program name from every
    # view below: the fold joins the name before the substrings are tested.
    folded = command.replace("\\\n", "")
    if folded != command:
        stripped = (
            _decode_shell_quoted_literals(folded)
            .lower()
            .replace("'", "")
            .replace('"', "")
            .replace("\\", "")
        )
    # If glob characters appear at all, pay the tokenizer and let the
    # invocation check decide — the matchers still require a real invocation,
    # so the widened gate only widens what gets LOOKED at, never what gets
    # denied on its own. A `$` in the command names a program through
    # expansion (`a=g;b=h;"$a$b"` runs gh), carries no program substring in
    # any decoded view, and routes to the expansion-held branch downstream.
    if (
        any(name in stripped for name in _EXFIL_PROGRAMS)
        or _GLOB_CHARS_RE.search(stripped)
        or "$" in stripped
    ):
        for seg in _split_unquoted_separators(command):
            if not seg.strip():
                continue
            # Fold line continuations before tokenizing: the shell removes
            # `\<newline>` while READING, so `g\<nl>h` is one word `gh` —
            # shlex would otherwise keep the split name and hide the anchor.
            seg = seg.replace("\\\n", "")
            try:
                tokens = _shell_tokens(seg)
            except Exception:
                continue
            if not tokens:
                continue
            invocations = _exfil_invocations(tokens)
            if not any(invocations.values()):
                continue
            for rule in _EXFIL_RULES:
                if NORMALIZED_VIEW not in rule.views or rule.token_match is None:
                    continue
                if rule.token_match(tokens, invocations) and _on(rule.rule_ids[0]):
                    return f"Blocked: command matches data-exfiltration pattern ({rule.label})"

    # 3. Carrier payloads: `bash -c '…'`, `eval '…'`, herestrings, `env -S`,
    # `$SHELL -c` etc. keep the inner command out of the outer argv's command
    # position, so neither view above sees it. The shared extractor
    # (:func:`_nested_shell_payloads`) recognizes every carrier spelling by
    # construction; each literal payload is audited recursively as its own
    # command. A failing pass costs the pass, never the raw verdict.
    try:
        all_tokens = _shell_tokens(command.replace("\\\n", ""))
        pipes_into_evaluator = _pipes_into_evaluator(all_tokens)
    except Exception:
        all_tokens, pipes_into_evaluator = [], False
    # Bounded same-command assignments: `CMD='gh api --input f'; bash -c "$CMD"`
    # carries the payload through a variable, so a literal-only rescan misses
    # it. Resolve simple NAME=literal pairs (single-quoted, double-quoted, or
    # bare, no expansions) and substitute their uses before auditing carriers.
    assignments: "dict[str, str]" = {}

    def _collect(m: "re.Match[str]") -> str:
        # Shell semantics: the LATEST assignment before use wins.
        name, value = m.group("name"), m.group("value")
        assignments[name] = value
        return " "

    assign_re = re.compile(
        r"(?<![=\w])(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
        r"(?P<value>'[^']*\n?|\"[^\"]*\n?\"|[^\s|;&<>()\n']*)"
    )
    expanded = assign_re.sub(_collect, command)
    for name, value in assignments.items():
        if value.startswith(("'", '"')):
            value = value[1:-1]
        # `${NAME}` needs no trailing boundary; bare `$NAME` must not eat
        # into a longer name (`$CMDX` is not `$CMD`).
        expanded = re.sub(rf"\$(?:\{{{name}\}}|{name}(?!\w))", value.replace("\\", "\\"), expanded)
    # Only pay for the expansion pass when a variable is actually USED and at
    # least one assignment was collected — otherwise plain `key=value`-looking
    # text (URLs, flags) would re-trigger this tier forever.
    if assignments and "$" in command and expanded != command:
        # The expanded text carries payloads the original spelling hides:
        # audit it as its own command (depth-guarded via the public wrapper).
        verdict = _audit_bash_exfiltration(
            expanded, enabled_ids=enabled_ids, _depth=_depth + 1, _budget=_budget
        )
        if verdict is not None:
            return verdict
    for seg in _split_unquoted_separators(command):
        if not seg.strip():
            continue
        try:
            tokens = _shell_tokens(seg.replace("\\\n", ""))
            payloads = _nested_shell_payloads(tokens)
        except Exception:
            continue
        for payload in payloads:
            if not payload.strip():
                continue
            verdict = _audit_bash_exfiltration(
                payload, enabled_ids=enabled_ids, _depth=_depth + 1, _budget=_budget - 1
            )
            if verdict is not None:
                return verdict
        # Pipe-to-evaluator carriers: `printf %s '<script>' | bash` hands the
        # literal to a shell as stdin — the script never appears in any argv.
        # When this segment pipes into an evaluator, its last quoted literal
        # is the executed script; audit it recursively. A segment with no
        # quoted literal (the canonical `curl … | bash` installer) has no
        # visible script and yields no verdict here.
        if not pipes_into_evaluator:
            continue
        # The last quoted literal in the writing segment is the executed
        # script; audit it recursively. No quoted literal (the canonical
        # `curl … | bash` installer) yields no verdict here.
        try:
            quoted = list(re.finditer(r"(['\"])(.*?)\1", seg, re.DOTALL))
        except Exception:
            quoted = []
        if not quoted:
            continue
        # The script may be ASSEMBLED from several arguments
        # (`printf %s 'gh api ' '--input f' | bash` concatenates stdout), so
        # audit the complete literal stream, not just the final piece. For
        # printf, the FIRST quoted literal is the FORMAT — it does not reach
        # stdout (its % specifiers are replaced by the later arguments), so
        # it is excluded; a stale `%s` prefix would corrupt the audited view.
        writer_head = seg.split(None, 1)[0].strip().lower() if seg.strip() else ""
        views = ["".join(m.group(2) for m in quoted)]
        if writer_head == "printf" and len(quoted) > 1:
            # The format literal may contribute only `%`-specifier positions
            # that the later arguments fill — the arguments alone are what a
            # `%s`-style format emits. Audit BOTH readings: the joined stream
            # and the arguments-without-format; whichever the shell really
            # produces, one of the two views contains it.
            views.insert(0, "".join(m.group(2) for m in quoted[1:]))
        for script in views:
            if not script.strip():
                continue
            verdict = _audit_bash_exfiltration(
                script, enabled_ids=enabled_ids, _depth=_depth + 1, _budget=_budget - 1
            )
            if verdict is not None:
                return verdict
        continue

    # 4. Process substitutions (`>( … )` / `<( … )`): the body runs as its own
    # command concurrently, but the token pass sees it as an echo/arg token —
    # the data-consumer exemption then hides the gh invocation. Audit each
    # body recursively as its own command so its own rules fire.
    # Nesting-aware extraction: `>(gh api "$(echo x)" --input f)` carries
    # parentheses inside the body, which a flat `[^()]*` regex cannot span.
    # Walk the command once, tracking paren DEPTH after each `>(`/`<(` opener,
    # and take the body as the span to the MATCHING closer.
    procsub_bodies: "list[str]" = []
    try:
        for m in re.finditer(r"[<>]\(", command):
            depth = 1
            j = m.end()
            while j < len(command) and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                procsub_bodies.append(command[m.end() : j - 1])
    except Exception:
        procsub_bodies = []
    for body in procsub_bodies:
        if body.strip():
            verdict = _audit_bash_exfiltration(
                body, enabled_ids=enabled_ids, _depth=_depth + 1, _budget=_budget - 1
            )
            if verdict is not None:
                return verdict
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
