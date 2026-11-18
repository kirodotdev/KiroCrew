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

The URL rule tables (``exfil_url_rules``), the OAuth endpoint set and shape
diagnostics (``exfil_oauth``) and the data-egress command gate (``exfil_bash``)
live in owner modules beside this one. This module is the import path of every
name they define, and keeps the URL classifier, the blocked-link records, the
URL redactor, the OAuth credential diagnosis and the IMDS folder and check.
A read through this module answers with the owner's object, and a write
reaches every owner that binds the name (:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import ast
import bisect
import contextlib
import importlib as _importlib
import ipaddress
import logging
import re
import socket
import sys as _sys
import time  # noqa: F401  (a test clock installs here and the mirror carries it to exfil_oauth)
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse

from .exfil_git_object import _GIT_OBJECT_NAME_RE, _exfil_blob_hit  # noqa: F401
from .exfil_oauth import (  # noqa: F401
    _OAUTH_EXTENSION_HOST_RE,
    _OAUTH_QUERY_PARAMS,
    OAuthUrlCredentialDiagnostic,
    _approved_oauth_authorization_endpoint,
    _log_operator_list_use,
    _oauth_diagnostic,
    _oauth_query_diagnostic,
    _oauth_url_payload_diagnostic,
    _operator_client_id_shaped,
    _operator_listed_client_id_query,
    _valid_oauth_extension_path,
)
from .exfil_scan_span import _walk_flagged_span
from .exfil_url_rules import (  # noqa: F401  # _URL_RE: kiro_crew.security serves it from here
    _EXFIL_PATTERNS,
    _EXFIL_PERCENT_RE,
    _EXFIL_QUERY_MIN_LEN,
    _HARD_CREDENTIAL_RE,
    _MAX_URL_DECODE_PASSES,
    _URL_RE,
    _URL_SCAN_RE,
    _heavy_percent_encoding,
    _is_safe_presigned,
    _url_scan_candidates,
)
from .redaction import (
    _contains_fixed_credential,
    _credential_redaction_plan,
    _splice,
    _text_contains_bare_secret,
)
from .redaction_entropy import _outlook_web_link_tail
from .redaction_switch import always_scan

logger = logging.getLogger(__name__)


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
_slack_manifest_re_slot: list[tuple[re.Pattern[str], ...] | None] = []


def _slack_manifest_variant_re(slack_manifest: Any, combo: frozenset[str]) -> re.Pattern[str]:
    """Pattern for one switch combination's stripped manifest with any one alias."""
    rendered = slack_manifest.stripped_template(combo)
    placeholder, alias_body = slack_manifest.ALIAS_PLACEHOLDER, slack_manifest.ALIAS_PATTERN
    command_slot = (re.escape(slack_manifest.COMMAND_PLACEHOLDER), "(?P<command>[a-z0-9_-]+)")
    if placeholder not in rendered or slack_manifest.COMMAND_PLACEHOLDER not in rendered:
        raise ValueError("manifest template lacks its alias or command placeholder")
    parts = rendered.split(placeholder)
    pattern = re.escape(parts[0]).replace(*command_slot)
    for index, part in enumerate(parts[1:]):
        slot = f"(?P<alias>{alias_body})" if index == 0 else "(?P=alias)"
        pattern += slot + re.escape(part).replace(*command_slot)
    return re.compile(pattern)


def _slack_manifest_payload_re() -> tuple[re.Pattern[str], ...] | None:
    """One exact pattern per switch combination, built by the emitters' own
    ``stripped_template()``; ``{{ALIAS}}`` repeats are backreferences, ``{{COMMAND}}``
    is pinned by the caller. A variant that does not build is left out (refused)."""
    if not _slack_manifest_re_slot:
        built: list[re.Pattern[str]] = []
        try:
            from kiro_crew import slack_manifest

            for combo in slack_manifest.feature_sets():
                with contextlib.suppress(Exception):
                    built.append(_slack_manifest_variant_re(slack_manifest, combo))
        except Exception:
            pass
        _slack_manifest_re_slot.append(tuple(built) or None)
    return _slack_manifest_re_slot[0]


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
    for pattern in _slack_manifest_payload_re() or ():
        match = pattern.fullmatch(payloads[0])
        if match is not None and match["command"] == slack_manifest.slash_command(match["alias"]):
            return match.group("alias")
    return None


def _exfil_url_warning(
    domain: str,
    path_and_query: str,
    exempt_hosts: frozenset[str],
    *,
    port: str = "",
    is_https: bool = True,
    allow_safe_presigned: bool = True,
    allow_oauth_entropy: bool = False,
    assume_oauth_endpoint: bool = False,
    _rule_out: list[str] | None = None,
) -> str | None:
    """Classify one matched URL — the single per-URL exfil verdict.

    Shared by scan_exfiltration_urls (which collects the warnings) and
    redact_exfiltration_urls (which redacts every URL that returns non-None), so
    the two paths can never drift. Returns the warning string, or None if clean.
    ``_rule_out`` receives only a stable rule id, never URL-derived text.
    ``assume_oauth_endpoint`` answers a counterfactual, never a verdict: it
    treats the endpoint as if it were on the OAuth allowlist WITHOUT consulting
    the builtin set or the operator file, so a caller can ask "would this URL
    pass if the operator added its endpoint?". HTTPS-only / no-explicit-port
    are not assumed away, because the allowlist never relaxes them either.
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
    # host-sensitive heuristic exemption below. Words encoded in the PATH are
    # not counted, except on the OAuth admission gate, which stays strict.
    if _heavy_percent_encoding(path_and_query, allow_path_text=not allow_oauth_entropy):
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
        and (
            assume_oauth_endpoint
            or _approved_oauth_authorization_endpoint(_dom, path_and_query.split("?", 1)[0])
        )
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
    elif (_owa := _outlook_web_link_tail(_dom, path_and_query, port, is_https)) is not None:
        heuristic_query = _owa  # Outlook's own `webLink`: see _outlook_web_link_tail
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
        if _exfil_blob_hit(heuristic_query) or _exfil_blob_hit(unquote_plus(heuristic_query)):
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
    for match in _URL_SCAN_RE.finditer(text):
        flagged = _flagged_url_span(match, exempt_hosts)
        if flagged:
            warnings.append(flagged[3])
    return warnings


def refuses(text: str, redactor: Callable[[str], str]) -> bool:
    """Whether a refusal gate refuses *text*: a credential *redactor* masks, or a flagged URL."""
    with always_scan():
        masked = redactor(text) != text
    return masked or bool(scan_exfiltration_urls(text))


def _flagged_url_span(
    match: re.Match[str], exempt_hosts: frozenset[str], rules: list[str] | None = None
) -> tuple[re.Match[str], str, str, str] | None:
    """The span to replace for one scan match: :func:`exfil_scan_span._walk_flagged_span`."""

    def judge(domain: str, path_and_query: str, port: str, is_https: bool) -> str | None:
        return _exfil_url_warning(
            domain, path_and_query, exempt_hosts, port=port, is_https=is_https, _rule_out=rules
        )

    def slack_alias(domain: str, path: str, query: str, port: str, is_https: bool) -> str | None:
        return _kirocrew_slack_app_link_alias(domain, path, query, is_https=is_https, port=port)

    return _walk_flagged_span(match, exempt_hosts, judge, slack_alias)


def redact_exfiltration_urls(text: str) -> tuple[str, list[str]]:
    """Mask the credential VALUES inside the URLs of *text*; every other character stays.

    A URL is never replaced whole. Its scheme, host, path and every parameter that is
    not a credential stay readable; only a value the rule table reads as a credential
    (a token, a signature, the password in the userinfo, a value that decodes to one)
    is replaced, exactly as :func:`kiro_crew.security.redaction.redact_credentials`
    replaces it. Outside every URL the text is returned as written. Returns
    ``(text, warnings)``; a warning counts a value and never carries one.

    The BLOCKING decisions over URLs (:func:`scan_exfiltration_urls` over a command,
    :func:`oauth_url_contains_credential`) are separate and unchanged.
    """
    urls = [m.span() for m in _URL_SCAN_RE.finditer(text)]
    if not urls:
        return text, []
    spans, _warnings, _rules = _credential_redaction_plan(text)
    if not spans:
        return text, []
    starts = [start for start, _end in urls]
    kept = []
    for start, end, tag in spans:
        i = bisect.bisect_right(starts, start) - 1
        if i >= 0 and end <= urls[i][1]:
            kept.append((start, end, tag))
    if not kept:
        return text, []
    warnings = [f"Redacted credential in a URL ({end - start} chars)" for start, end, _t in kept]
    return _splice(text, kept), warnings


# Markerless 40-character values collide with OAuth entropy only for these
# authorization-request fields (``code_verifier`` goes to the token endpoint;
# a dynamically registered public ``client_id`` may be 43-char base64url).
_OAUTH_ENTROPY_QUERY_PARAMS = frozenset({"client_id", "code_challenge", "nonce", "state"})

# The exemption is bounded to shapes the protocol itself can emit, so an
# AWS-secret-shaped run cannot ride a front-channel parameter into the blanked
# set. base64url (RFC 4648 s5) emits `-`/`_` and never `+`/`/`; an S256
# challenge and a dynamic client id must be exactly 43 base64url characters.
_OAUTH_S256_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _oauth_entropy_form_is_protocol_shaped(key: str, form: str) -> bool:
    """Return True when ONE decoded form of a value keeps a protocol shape."""
    if key in {"client_id", "code_challenge"}:
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
    operator_listed: bool = False,
) -> str:
    """Blank entropy-bearing OAuth values before the markerless URL scan.

    Fixed credential signatures are checked against the raw and decoded URL
    before this target is built. At an exact approved endpoint, only the
    code-owned state, nonce, PKCE challenge and client_id fields are omitted from the
    markerless bare-secret heuristic, and only when the value carries a shape
    the protocol can emit (see :func:`_oauth_entropy_value_is_protocol_shaped`).
    An operator-listed endpoint also omits a token-safe client_id.
    Other recognized values, parameter names, unknown
    parameters, and every non-query URL component remain in the scan target.
    """
    if not approved_endpoint or not query:
        return url

    sanitized_segments: list[str] = []
    for key, separator, value in (segment.partition("=") for segment in query.split("&")):
        approved_value = (
            bool(separator)
            and key in _OAUTH_ENTROPY_QUERY_PARAMS
            and _oauth_entropy_value_is_protocol_shaped(key, value)
        ) or (operator_listed and key == "client_id" and _operator_client_id_shaped(value))
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


def diagnose_oauth_url_credential(
    url: str, *, assume_approved_endpoint: bool = False, assume_operator_listed: bool = False
) -> OAuthUrlCredentialDiagnostic | None:
    """Return a safe rejection signature, never URL/value bytes or derivatives.

    ``assume_approved_endpoint`` runs the SAME gate as if the endpoint were approved
    but NOT operator-listed (builtin or MCP-discovered); ``assume_operator_listed``
    as if listed in ``oauth_endpoints.json``. Neither reads a source. A rejection
    surface uses them to tell whether discovery or the operator file would clear
    THIS rejection; the unconditional rules (fixed credentials, userinfo, fragments,
    path params, heavy percent-encoding, http, explicit port) still reject.
    """
    assumed = assume_approved_endpoint or assume_operator_listed
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

    host_l = parsed.hostname.lower()
    approved_endpoint = (
        parsed.scheme.lower() == "https"
        and not port
        and (assumed or _approved_oauth_authorization_endpoint(host_l, parsed.path))
    )
    operator_listed = approved_endpoint and (
        assume_operator_listed
        if assumed
        else _operator_listed_client_id_query(host_l, parsed.path, parsed.query)
    )
    scan_target = _oauth_credential_scan_target(
        url, parsed.query, approved_endpoint=approved_endpoint, operator_listed=operator_listed
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
        assume_oauth_endpoint=assumed,
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
        query_decoder = None if _exfil_blob_hit(heuristic_query) else unquote_plus
        return _oauth_query_diagnostic(
            rule,
            heuristic_query,
            predicate=_exfil_blob_hit,
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


def oauth_rejection_is_endpoint_exemptible(url: str, *, operator_listed: bool = False) -> bool:
    """True when *url* is rejected today AND would pass with its endpoint allowlisted.

    By default the endpoint is assumed approved but not operator-listed (what MCP
    discovery asks); ``operator_listed=True`` asks whether an ``oauth_endpoints.json``
    entry would clear it, which a rejection surface must know before naming that remedy.
    Rules the allowlist never relaxes (fixed credential, userinfo, fragment, path params,
    heavy percent-encoding, ``http``, explicit port) still refuse under the assumption.
    Only the first call reads the operator file, through the ordinary gate (memoized
    stat), so callers treat this like the gate itself: run it off the event loop.
    """
    if diagnose_oauth_url_credential(url) is None:
        return False
    cleared = diagnose_oauth_url_credential(
        url, assume_approved_endpoint=True, assume_operator_listed=operator_listed
    )
    return cleared is None


# ── IP Canonicalization (IMDS bypass prevention) ──
# Attackers bypass IMDS checks by encoding 169.254.169.254 in alternate forms:
#   - Decimal:   2852039166 (single 32-bit integer)
#   - Hex:       0xa9fea9fe or 0xa9.0xfe.0xa9.0xfe
#   - Octal:     0251.0376.0251.0376
#   - IPv6-mapped: ::ffff:169.254.169.254 or ::ffff:a9fe:a9fe
#   - Mixed:     169.254.0xa9.0376
# canonicalize_ip converts ALL these to dotted-quad for uniform matching.


def _octet_literal_value(part: str) -> "int | None":
    """Value of ONE literal dotted-quad octet in the OS resolver's bases, or None.

    ``inet_aton`` and ``curl`` read a leading-zero octet as C-style OCTAL (``0251``
    is 169, not 251) and ``0x`` as hex; ``int(part, 0)`` does not accept the bare
    ``0NNN`` octal spelling, so it is handled first. Returns None for an empty or
    non-numeric octet. This is the ONE octet parser -- ``canonicalize_ip`` and the
    IMDS shell-expansion gate both call it, so a leading-zero or hex octet reads the
    same way on every path.

    EVERY conversion runs inside the try: a very long digit string trips CPython's
    integer-string length limit with ValueError, and that must read as "not a
    numeric octet" (None), never crash the gate -- the same fail-soft the inline
    parser in ``canonicalize_ip`` had before this was extracted.
    """
    try:
        if len(part) > 1 and part[0] == "0" and part[1:].isdigit():
            # Octal only when every trailing digit is octal; a stray 8/9 is
            # decimal, exactly as inet_aton reads it.
            if all(c in "01234567" for c in part[1:]):
                return int(part, 8)
            return int(part)
        return int(part, 0)  # decimal, or 0x-prefixed hex
    except (ValueError, OverflowError):
        return None


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
            # The ONE octet parser (reads octal/hex in the resolver's bases); None
            # is an unparseable octet, so this candidate is not a dotted IP.
            val = _octet_literal_value(part)
            if val is None:
                valid = False
                break
            octets.append(val)

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

# ── IMDS via shell EXPANSION (CWE-185 / CWE-693) ──
# The deny view deliberately stops BEFORE shell expansion, so a dotted token that
# splices an expansion into an octet -- ``169.254.169.25$((2+2))``,
# ``169.254.169.$((100+154))`` -- reaches this gate with that octet UNEXPANDED, and
# the literal ``169.254.169.254`` the canonicalizer looks for never appears, while
# bash still produces the real bytes at run time.
#
# This gate refuses ONLY what it can resolve STATICALLY, without running anything: an
# octet made of literal digits and pure-ARITHMETIC expansions (``$((..))`` / ``$[..]``,
# evaluated by ``_safe_arith`` -- ``+ - * / %`` over PLAIN-DECIMAL and ``0x``-HEX int
# constants only, via a Python ``ast``, size-bounded). If the four octets resolve to
# exactly ``169.254.169.254`` the token is refused. Any octet whose value is UNKNOWN
# without running the shell -- a ``$VAR``, ``${..}``, a ``$(..)`` command substitution,
# a backtick -- does not resolve and is LEFT ALONE. Unknown value means allow: a benign
# ``for o in 123 253; do ping 169.254.169.$o; done`` reachability check, a subnet
# gateway (``${A}.${B}.${C}.254``), a version string (``${MAJOR}.${MINOR}.${PATCH}``)
# and a regex end-anchor (``grep '^nameserver 169.254.169.253$'``) all pass.
#
# ``_safe_arith`` reads a Python ``ast``, NOT a bash arithmetic emulator, so three
# arithmetic forms that bash DOES resolve statically to ``.254`` are deliberately NOT
# emulated and are documented RESIDUALS alongside the ``$VAR`` / ``$(..)`` / backtick
# spellings: a leading-zero OCTAL literal (``$((0376))``), a base-N literal
# (``$((16#fe))``, ``$((2#11111110))``), and a value that only reaches 254 by 64-bit
# wraparound (``$((18446744073709551870))``). Emulating bash arithmetic faithfully
# (octal, base-N, two's-complement overflow, C division) would grow this evaluator
# without bound and trade a new divergence either way; the agreed scope is the plain
# decimal / ``0x``-hex arithmetic a Python ``ast`` computes identically to bash. The
# literal deny tier still catches a fully-resolved literal octet (``169.254.169.0376``
# is read as octal by ``_octet_literal_value`` and blocked); only the ARITHMETIC
# spelling of these bases is the residual. An octet holding any unresolvable expansion
# (``169.254.169.$x``) is allowed, not blocked, so the gate refuses no benign command.
# The dotted-quad octets of _IMDS_IP, as ints, for positional comparison.
_IMDS_OCTETS = tuple(int(o) for o in _IMDS_IP.split("."))
#: Characters of a dotted IP token (decimal, hex digits, ``x``, the dot); never ``$``.
_IP_TOKEN_CHARS = frozenset("0123456789abcdefABCDEFxX.")
#: Cap on an arithmetic expression fed to the safe evaluator, so a crafted
#: ``$((...))`` cannot drive parse/eval cost up; a real IMDS octet needs a few chars.
_ARITH_MAX_LEN = 256


def _arith_span(command: str, i: int) -> "tuple[int, str] | None":
    """If a bash arithmetic expansion starts at *i*, return ``(end, expr)``, else None.

    Handles ``$((expr))`` (balanced parens, so a nested ``$((100+(154)))`` is spanned
    whole) and the older ``$[expr]`` form. *end* is the index just past the span;
    *expr* is the inner text handed to the safe evaluator. A command substitution
    ``$(..)`` (single paren) is NOT arithmetic and returns None.
    """
    n = len(command)
    if i + 1 >= n or command[i] != "$":
        return None
    # Bound every lookahead to the arithmetic cap: an unbalanced ``$((`` must not
    # scan to end-of-string, or a command of many ``$((`` would be quadratic.
    limit = min(n, i + _ARITH_MAX_LEN + 4)
    if command[i + 1 : i + 3] == "((":
        depth = 0
        j = i + 1
        while j < limit:
            if command[j] == "(":
                depth += 1
            elif command[j] == ")":
                depth -= 1
                if depth == 0:
                    return (j + 1, command[i + 3 : j - 1])  # strip ``$((`` and ``))``
            j += 1
        return None  # unbalanced within the cap -> not a resolvable span
    if command[i + 1] == "[":
        close = command.find("]", i + 2, limit)
        if close == -1:
            return None
        return (close + 1, command[i + 2 : close])
    return None


def _eval_arith(node: ast.AST) -> "int | None":
    """Evaluate a parsed arithmetic node to an int, or None if it is not pure integer
    arithmetic over ``+ - * / %`` (shell ``$((..))`` is integer, truncating)."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval_arith(node.operand)
        if v is None:
            return None
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
    ):
        left = _eval_arith(node.left)
        right = _eval_arith(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            return None  # shell errors on /0; treat as unresolvable
        # Truncate toward zero in INTEGERS only. ``int(left / right)`` goes through a
        # float and loses precision above 2**53, so a large plain-decimal expression
        # bash resolves to .254 would not (over- or under-resolving the octet); this
        # matches bash's C-style integer division and remainder exactly.
        q = abs(left) // abs(right)
        if (left < 0) != (right < 0):
            q = -q
        if isinstance(node.op, ast.Mod):
            return left - right * q  # C-style remainder, matches shell
        return q  # truncating integer division toward zero, like the shell
    return None


def _safe_arith(expr: str) -> "int | None":
    """Value of a plain-decimal / ``0x``-hex integer expression over ``+ - * / %``, or
    None otherwise. This is a Python ``ast``, NOT a bash arithmetic emulator: a leading-
    zero octal literal, a base-N (``N#..``) literal and 64-bit wraparound are NOT read
    (they are documented residuals -- see the module note). No names, calls, attributes
    or other operators run."""
    expr = expr.strip()
    if not expr or len(expr) > _ARITH_MAX_LEN:
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        return None
    return _eval_arith(tree.body)


def _resolve_octet(octet: str) -> "int | None":
    """Resolve one dotted octet to its int value, substituting arithmetic spans; None
    if any non-arithmetic expansion (``$VAR``/``${..}``/``$(..)``/backtick) leaves it
    with an UNKNOWN run-time value, or the result is not a valid octet literal."""
    out: list[str] = []
    i, n = 0, len(octet)
    while i < n:
        span = _arith_span(octet, i)
        if span is not None:
            end, expr = span
            value = _safe_arith(expr)
            if value is None:
                return None
            out.append(str(value))
            i = end
        elif octet[i] == "$" or octet[i] == "`":
            return None  # unknown run-time value -> do not block
        else:
            out.append(octet[i])
            i += 1
    return _octet_literal_value("".join(out))


def _arith_ip_tokens(command: str) -> "list[str]":
    """Maximal dotted tokens made of IP characters and whole arithmetic spans. A token
    stops at the first character that is neither (so a ``$VAR``/``$(..)``/backtick ends
    it); the scan is one linear pass, each arithmetic span one balanced lookahead."""
    tokens: list[str] = []
    n = len(command)
    i = 0
    while i < n:
        if command[i] in _IP_TOKEN_CHARS or _arith_span(command, i) is not None:
            start = i
            while i < n:
                span = _arith_span(command, i)
                if span is not None:
                    i = span[0]
                elif command[i] in _IP_TOKEN_CHARS:
                    i += 1
                else:
                    break
            tokens.append(command[start:i])
        else:
            i += 1
    return tokens


def _imds_access_via_expansion(command: str) -> str | None:
    """Refuse a dotted token that STATICALLY resolves to ``169.254.169.254``.

    See the module note above. Only literals and pure-arithmetic ``$((..))`` / ``$[..]``
    expansions resolve; a ``$VAR`` / ``${..}`` / ``$(..)`` / backtick octet has an
    unknown value and is LEFT ALONE (unknown -> allow). The denial names the
    UNEXPANDED token, never any expanded bytes. Returns the reason or None.

    Scope: the four-part dotted form, per GHSA-x2m8-5ff9-f479. The 2-/3-part inet_aton
    short forms are outside this check.
    """
    if "$((" not in command and "$[" not in command:
        return None  # nothing statically resolvable here -> the canonicalizer owns it
    for token in _arith_ip_tokens(command):
        octets = token.split(".")  # arithmetic spans carry no dot, so this is safe
        if len(octets) == 5 and octets[-1] == "":
            octets = octets[:-1]  # a single trailing dot (FQDN root) the resolver drops
        if len(octets) != 4:
            continue
        values: list[int] = []
        for octet in octets:
            value = _resolve_octet(octet)
            if value is None:
                break
            values.append(value)
        if values == list(_IMDS_OCTETS):
            return (
                f"Blocked: command accesses IMDS endpoint "
                f"(169.254.169.254 via shell expansion '{token}')"
            )
    return None


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
    # Quick reject: no literal IP candidate. A token whose octets are ALL shell
    # expansions (``$A.$B.$C.254``) still has no literal candidate, so the
    # expansion check runs even on this path rather than short-circuiting None.
    candidates = _IP_CANDIDATE_RE.findall(command)
    if not candidates:
        return _imds_access_via_expansion(command)

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
    # No literal encoding matched; a per-octet shell-expansion spelling still can.
    return _imds_access_via_expansion(command)


# This module is the import path of every name the egress layer offers. The URL classifier,
# the blocked-link records, the URL redactor, the OAuth credential diagnosis and the IMDS
# check above stay here; the rule tables, the OAuth endpoint set and the command gate live
# in the owner modules below, and a write through this module (a patch) reaches every
# module holding the name (:mod:`kiro_crew._facade_mirror`). The header also binds the
# owner names ``display_safety`` reads here.
# Owners never import this module. New work goes to the owner of its responsibility:
#   ``exfil_url_rules`` -- the URL and query patterns and the presigned-URL validator
#   ``exfil_git_object`` -- the git object name carve-out from the base64-blob test
#   ``exfil_scan_span`` -- which span of a URL match is judged and replaced
#   ``exfil_oauth`` -- the OAuth endpoint set, its audit events and the shape diagnostics
#   ``exfil_bash`` -- the data-egress and reverse-shell command gate
from kiro_crew import _facade_mirror  # noqa: E402

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.security.exfil_url_rules",
    "kiro_crew.security.exfil_git_object",
    "kiro_crew.security.exfil_scan_span",
    "kiro_crew.security.exfil_oauth",
    "kiro_crew.security.exfil_bash",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.security.exfil_bash import (  # noqa: F401
        _BASH_EXFIL_PATTERNS,
        _BASH_EXFIL_RES,
        _BASH_EXFIL_ROW_DISCRIMINATORS,
        _BASH_EXFIL_RULE_BY_LABEL,
        _BASH_EXFIL_RULE_BY_PATTERN,
        _CARVEOUT_VERBS,
        _DATA_EXFIL_FILE_BODY_RULE,
        _data_at_carveout_allows,
        _exfil_rule_id_for_match,
        _inert_line_allows,
        audit_bash_exfiltration,
    )
    from kiro_crew.security.exfil_oauth import (  # noqa: F401
        _ENDPOINT_EXTENSION_CAP,
        _ENDPOINT_EXTENSION_ENTRIES_KEY,
        _OAUTH_AUTHORIZATION_ENDPOINTS,
        _OAUTH_DIAGNOSTIC_PARAMETER_RE,
        _OAUTH_DISCOVERED_CAP,
        _OAUTH_DISCOVERED_ENDPOINTS,
        _OAUTH_DISCOVERED_MCP_URL_MAX_LEN,
        _OAUTH_DISCOVERED_TTL_SECONDS,
        _OAUTH_EXTENSION_AUDITED,
        _OAUTH_EXTENSION_MEMO,
        _OAUTH_EXTENSION_PATH_BAD,
        _OAUTH_EXTENSION_PATH_MAX_LEN,
        _OAUTH_OPERATOR_CLIENT_ID_CHARS,
        _OAUTH_URL_SYMBOLS,
        OAuthUrlShapeProfile,
        _discovered_oauth_endpoint_approved,
        _emit_oauth_discovered_endpoint_event,
        _emit_oauth_extension_used_event,
        _load_operator_oauth_endpoints,
        _oauth_char_class,
        _oauth_shape_profile,
        _operator_listed_oauth_endpoint,
        _safe_oauth_parameter_name,
        _validate_operator_oauth_entries,
        record_discovered_oauth_endpoint,
    )
    from kiro_crew.security.exfil_scan_span import (  # noqa: F401
        _SlackAlias,
        _tail_after_exempt_span,
        _UrlJudge,
    )
    from kiro_crew.security.exfil_url_rules import (  # noqa: F401
        _CREDENTIAL_RE,
        _NON_ASCII_RUN_RE,
        _S3_PRESIGNED_PARAMS,
        _S3_PRESIGNED_RE,
        _SIGNATURE_RE,
        _STRUCTURAL_VALIDATORS,
        _STS_TOKEN_RE,
        _URL_DISPLAY_STOP_RE,
        _URL_HOST,
        _URL_PUNCTUATION_TAIL_RE,
        _URL_SCHEME,
        _URL_START_RE,
        _embedded_url_matches,
        _is_encoded_path_text,
        exfil_query_min_len,
    )
if TYPE_CHECKING:  # project names it serves from outside its owners, from their homes
    from kiro_crew.credential_patterns import (  # noqa: F401
        AWS_KEY_ID,
    )
    from kiro_crew.security.denied_rules import (  # noqa: F401
        _INERT_SEARCH_VERBS,
        _exception_eligible,
    )
    from kiro_crew.security.shell_commands import read_commands as _read_commands  # noqa: F401
    from kiro_crew.sel import (  # noqa: F401
        SecurityEvent,
        SecurityEventLog,
        sel,
    )
