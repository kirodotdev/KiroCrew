"""Load and query the curated official MCP provider registry."""

from __future__ import annotations

import ipaddress
import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, TypedDict, cast
from urllib.parse import urlsplit

from yarl import URL


class SmokeFixture(TypedDict):
    """A safe read-only tool invocation used by provider smoke tests."""

    tool: str
    args: dict[str, Any]


class L0Expectations(TypedDict):
    """OAuth discovery properties asserted by the account-free L0 probe.

    ``authorization_server`` is the provider's issuer identifier in FULL, not
    just its origin. RFC 8414 §2 makes the issuer an identity compared by exact
    string, and several providers put a path in it (Stripe advertises
    ``https://access.stripe.com/mcp``), so storing only the origin would let a
    tenant or realm substitution -- ``/tenant-a`` answering for ``/tenant-b`` --
    pass the check.

    ``verified_on`` says when ``l0_probe --record`` last captured these values
    from the live provider, and it is a REFRESH MARKER, not the provenance
    guarantee. A date in a file is self-attested: nothing stops a human typing
    one. What actually guarantees the baseline is the nightly probe, which
    re-derives every value from the live provider and trips the drift gate when
    the file disagrees. The date exists so a baseline nobody has re-derived in
    months gets noticed (see ``L0_VERIFICATION_WARN_AGE_DAYS``), not so it can
    be trusted on its own.
    """

    authorization_server: str
    dcr: bool
    pkce: bool
    verified_on: str


class AuthConfig(TypedDict, total=False):
    """How the OAuth client that talks to this provider comes into existence.

    ``mode`` is ``"dcr"`` (the default when the block is absent: kiro-cli
    registers a public client at runtime, RFC 7591) or ``"preregistered"``: the
    provider refuses dynamic registration, so an OPERATOR registers an OAuth app
    in the vendor console and hands Kiro Crew its client id (and, for a
    confidential client, its secret) through Settings → OAuth Apps. Which
    providers are which is a vendor fact, not a preference, so it lives in the
    registry rather than in config.

    The remaining keys are meaningful only for ``preregistered``:

    ``confidential`` -- the vendor requires a ``client_secret`` at the token
    endpoint. When false the vendor accepts a public PKCE client and the secret
    field is optional (Slack, once PKCE is enabled on the app).

    ``redirect_host`` / ``redirect_port`` -- a pre-registered app must name its
    redirect URI EXACTLY, so the loopback listener kiro-cli opens cannot pick a
    random port the way it does for a DCR client. The registry pins one port
    per provider (unique, so two providers warming at boot cannot collide on
    the bind) and the host the vendor accepts: ``127.0.0.1`` by default, the
    RFC 8252 §7.3 recommendation,
    or ``localhost`` for a vendor that only exempts that spelling from its
    https rule (HubSpot refuses IP literals outright). The path is the constant
    :data:`CALLBACK_PATH`, so every runbook can print the URI to register
    verbatim -- see :func:`redirect_uri`.

    ``registration_guide`` -- path under ``docs/guides/`` of the runbook that
    walks an operator through the vendor console. Surfaced by the "needs
    configuration" card and the Settings tab so the instruction is one click
    away from the field it fills in.
    """

    mode: str
    confidential: bool
    redirect_host: str
    redirect_port: int
    registration_guide: str


class _RequiredProviderFields(TypedDict):
    """Fields every registry entry must declare."""

    name: str
    slug: str
    tier: int
    mcp_url: str
    recommended_scopes: list[str]
    revoke_page_url: str
    docs_url: str
    gotcha_copy: str
    smoke_fixture: SmokeFixture
    l0_expectations: L0Expectations
    launch_gate_passed: bool
    vendor_approval_pending: bool
    # When ``revoke_page_url`` was last checked against the provider, and how.
    # A revoke link is a safety promise -- a stale one sends a user who wants
    # their grant gone to a 404 or to a page the grant does not appear on -- so
    # the check is dated in-tree and a test fails the build once it ages out.
    # The note records the strength of that check, because a logged-out HTTP
    # audit cannot see which surface actually lists the grant.
    revoke_verified_on: str
    revoke_verified_note: str


class Provider(_RequiredProviderFields, total=False):
    """One official MCP provider exposed to the Connections experience.

    ``client_id`` is a VENDOR-DEFAULT public client id for a provider whose
    ``auth.mode`` is ``preregistered`` -- the value Kiro itself registered, if
    any. It is a PUBLIC identifier forwarded to the runtime as the remote
    entry's ``clientId``; the corresponding secret is never stored here. An
    operator-configured client (Settings → OAuth Apps) always takes precedence
    over it, and no entry sets it today, which is why the field is optional
    rather than required — an entry without it simply carries no default.

    ``auth`` declares how the OAuth client comes into being; see
    :class:`AuthConfig`. Absent means dynamic client registration, which is
    what every launch provider does, so only the exceptions carry the block.

    ``prerequisite_copy`` is the one string the Connections card renders as a
    warning before Connect. It exists only for a provider with a BLOCKING,
    actionable provider-side requirement — one whose absence makes a connect
    fail or silently yield no tools — and it must stay to one or two imperative
    sentences. Everything else a user might want to know (protocol mechanics,
    where the grant is listed, scope trivia) belongs in ``gotcha_copy``, which
    is reference material for docs and agents and is deliberately NOT rendered
    on cards: a warning box on every card means no effective warning on any.

    ``revoke_manual_path`` is the in-app navigation to the same page as
    ``revoke_page_url``, for a provider whose settings link is a single-page app
    that re-routes after sign-in and can land the user somewhere else. A URL
    cannot be made reliable against that from our side, so the card states the
    path too and the user always has a way through.

    ``tool_aliases`` maps a tool this provider exposes to the name it should take
    when another mounted provider exposes the same name. It is a DECLARATION, not
    an instruction: an alias fires only when the collision is real (see
    :mod:`kiro_crew.connections.tool_aliases`), so declaring one does not rename
    anything on its own. Optional because a provider whose tool names are already
    unique across the launch set needs none.

    ``category`` is the gallery grouping the provider belongs to, drawn from the
    closed ``PROVIDER_CATEGORIES`` vocabulary so two spellings of one bucket
    cannot split it. It is metadata for the catalog and the coverage report
    (which categories the registry covers against the ChatGPT / Claude connector
    directories), not a behavioural switch: nothing mints, mounts or gates on
    it. Optional so an entry predating the field stays a valid shape.
    """

    client_id: str
    auth: AuthConfig
    prerequisite_copy: str
    revoke_manual_path: str
    tool_aliases: dict[str, str]
    category: str


class RegistryValidationError(ValueError):
    """Raised when the committed provider registry is malformed."""


# Public so the baseline recorder (kiro_crew.connections.l0_record) rewrites the
# same file the loader reads, instead of re-deriving the path and diverging.
REGISTRY_PATH = Path(__file__).with_name("registry.json")
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Both sides of a ``tool_aliases`` entry name an MCP tool -- the key names the
# tool the server ships, the value the name it takes on a collision -- so both are
# restricted to the character set real MCP tool names use: no slashes (which would
# read as a server/tool split), no whitespace, no "@" (which prefixes a server
# ref). Validating the KEY too keeps an unmatchable declaration (" list_issues")
# from silently never firing.
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Name suffixes that can only mean "somewhere on this network". Matched as plain
# strings against the DIALLED host (see canonical_host), so no resolver is
# involved and no IDNA spelling can slip past them.
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
# How long a revoke-link verification stays trustworthy. Providers move these
# settings pages between redesigns, so a visible card carries a re-check
# deadline rather than a one-time claim.
REVOKE_VERIFICATION_MAX_AGE_DAYS = 180
# The L0 baseline needs re-deriving on its own schedule: a provider can change
# its authorization server or drop DCR, and an expectation nobody has re-derived
# in months describes a provider that may not exist.
#
# Two tiers, because the remedy needs live network and CI does not have it. The
# refresh is `python -m kiro_crew.connections.l0_probe --record` run by a human
# on a networked machine, then committed -- the nightly CANNOT do it (it is
# deliberately read-only, see .github/workflows/connections-l0.yml), so a single
# hard threshold would eventually fail every PR in the repo on a timer nobody
# reset. Instead the WARN tier surfaces the need with 30 days of lead time and
# the nightly goes red on it every night, where the signal reaches someone who
# can act; only a visible provider's baseline hard-fails the PR suite, and only
# after that lead time has been ignored.
L0_VERIFICATION_WARN_AGE_DAYS = 60
L0_VERIFICATION_MAX_AGE_DAYS = 90
_PROVIDER_FIELDS = {
    "name",
    "slug",
    "tier",
    "mcp_url",
    "recommended_scopes",
    "revoke_page_url",
    "docs_url",
    "gotcha_copy",
    "smoke_fixture",
    "l0_expectations",
    "launch_gate_passed",
    "vendor_approval_pending",
    "revoke_verified_on",
    "revoke_verified_note",
}
_SMOKE_FIXTURE_FIELDS = {"tool", "args"}
_L0_EXPECTATION_FIELDS = {"authorization_server", "dcr", "pkce", "verified_on"}
# Optional because only non-DCR providers need a pre-registered OAuth client.
# See Provider.client_id: GitHub is the sole intended consumer and stays unset
# until the Kiro app is registered, so absence must remain a valid entry shape.
# ``tool_aliases`` is optional for the same class of reason: a provider whose
# tool names do not collide with any other mounted provider declares none.
_OPTIONAL_PROVIDER_FIELDS = {
    "client_id",
    "auth",
    "prerequisite_copy",
    "revoke_manual_path",
    "tool_aliases",
    "category",
}
# ``auth`` block vocabulary. Closed on purpose: the runtime branches on the mode
# and the runbooks print the redirect host, so a misspelling must fail the load,
# not silently fall back to DCR against a provider that refuses it.
AUTH_MODE_DCR = "dcr"
AUTH_MODE_PREREGISTERED = "preregistered"
_AUTH_MODES = frozenset({AUTH_MODE_DCR, AUTH_MODE_PREREGISTERED})
_AUTH_FIELDS = {"mode", "confidential", "redirect_host", "redirect_port", "registration_guide"}
# The two loopback spellings kiro-cli's callback listener accepts
# (``RedirectUriConfig`` in its ``oauth_util``) and RFC 8252 §7.3 names.
# ``127.0.0.1`` first because the RFC prefers it; ``localhost`` exists for the
# vendors whose https exemption names only that host.
REDIRECT_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
DEFAULT_REDIRECT_HOST = "127.0.0.1"
# Fixed callback path. A constant rather than a per-provider field so a runbook
# and the runtime can never disagree about it; kiro-cli appends it verbatim.
CALLBACK_PATH = "/callback"
# Non-privileged and below the ephemeral range on every platform, so the pinned
# listener neither needs root nor races the OS's own outbound port allocation.
_REDIRECT_PORT_MIN = 1024
_REDIRECT_PORT_MAX = 49151
_REGISTRATION_GUIDE_PATTERN = re.compile(r"^oauth-app-registration/[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
# Closed vocabulary for ``Provider.category``. Mirrors the buckets the ChatGPT
# and Claude connector directories group by, so the coverage report can diff the
# registry against them bucket-for-bucket. Add a bucket here, never inline in
# the JSON, so a typo cannot mint a one-provider category.
PROVIDER_CATEGORIES: frozenset[str] = frozenset(
    {
        "collaboration-docs",
        "project-management",
        "developer-tools",
        "design",
        "file-storage",
        "data-analytics",
        "payments-finance",
        "calendar-email",
        "crm-sales",
    }
)


def _validation_error(index: int, message: str) -> RegistryValidationError:
    return RegistryValidationError(f"provider at index {index}: {message}")


def utc_today() -> date:
    """Today in UTC.

    Every date in ``l0_expectations`` is stamped in UTC by the recorder, so every
    comparison against one must also be UTC. Using the LOCAL date here made a
    fresh baseline invalid for anyone west of UTC: ``--record`` writes UTC's date,
    which is tomorrow from a UTC-8 machine's point of view, and the future-date
    guard rejected it until local midnight caught up.
    """

    return datetime.now(timezone.utc).date()


def _iso_date(value: object, index: int, field: str) -> date:
    """Require a real calendar date, not just the shape of one."""

    if not isinstance(value, str) or not _ISO_DATE_PATTERN.fullmatch(value):
        raise _validation_error(index, f"{field} must be a YYYY-MM-DD date")
    try:
        # Shape alone would accept 2026-02-31; the staleness gates compare this
        # to a real date, so reject anything that is not one.
        return date.fromisoformat(value)
    except ValueError as error:
        raise _validation_error(index, f"{field} must be a YYYY-MM-DD date") from error


def _normalized(host: str | None) -> str | None:
    """Fold the two differences we deliberately ignore: case and a trailing dot.

    Applied to BOTH sides of every host comparison, so it cannot hide a codec
    difference. ``localhost.`` and ``localhost`` are the same name to DNS, and
    treating them as different strings is exactly how a suffix check gets
    bypassed.
    """

    if not host:
        return None
    return host.rstrip(".").lower() or None


def canonical_host(hostname: str | None) -> str | None:
    """Return the host aiohttp will actually DIAL, or ``None`` if unusable.

    Derived FROM yarl rather than re-implemented, because a check on any other
    spelling of the host is not a check on the request. aiohttp resolves
    ``req.url.raw_host`` (connector.py), so building a URL and reading that field
    back makes the vetted bytes and the dialled bytes the same bytes BY
    CONSTRUCTION -- whatever yarl's codec does today or after an upgrade.

    Re-implementing it is not merely redundant, it was wrong. The stdlib ``idna``
    codec is IDNA2003; yarl prefers IDNA2008/UTS-46 via the ``idna`` package.
    They disagree on deviation characters, and the disagreement is exploitable in
    both directions: ``faß.de`` reads as ``fass.de`` to the stdlib while yarl
    dials ``xn--fa-hia.de``, a Greek final sigma maps to a different A-label
    entirely, and a ZWJ label the stdlib happily encodes makes yarl refuse the
    URL outright. Verified against the pinned yarl 1.24.5 / idna 3.18.

    ``None`` means "cannot be vetted, so must not be fetched": an empty host, a
    host yarl itself rejects, or one carrying authority punctuation. That last
    check matters because yarl would otherwise silently REINTERPRET a bare string
    -- it reads ``user@evil.com`` as userinfo plus host, and ``evil.com:8443`` as
    host plus port -- so a caller passing something that is not purely a host
    would get a canonical form that is not about the host it thought it passed.
    """

    if not hostname:
        return None
    candidate = hostname.strip()
    if not candidate or any(char in candidate for char in " \t\r\n/@?#"):
        return None
    if ":" in candidate:
        # A colon in a host is either an IPv6 literal or authority punctuation.
        # urlsplit().hostname hands IPv6 back unbracketed and yarl needs the
        # brackets, but bracketing anything else would make yarl accept the whole
        # string as one opaque host -- so only a real literal takes this path.
        literal = candidate.strip("[]")
        try:
            ipaddress.ip_address(literal)
        except ValueError:
            return None
        candidate = f"[{literal}]"
    try:
        dialed = URL(f"https://{candidate}/").raw_host
    except (ValueError, UnicodeError):
        return None
    return _normalized(dialed)


def is_local_host(hostname: str | None) -> bool:
    """Whether ``hostname`` is an IP literal or a name that cannot be a provider.

    Runs on the host that will be DIALLED (see :func:`canonical_host`), so an
    IDNA homoglyph cannot smuggle a loopback address past it. DNS-free on
    purpose: this decides whether a URL may be fetched at all, and a check that
    resolved the name would be both slower and racy -- the answer can change
    between the check and the request that follows it.

    Every IP LITERAL is refused, not just the private ranges. That is stricter
    than an SSRF range check and simpler to defend: no real provider publishes an
    issuer as an IP, and a reviewer reading the registry cannot tell a benign
    literal from an internal one at a glance.
    """

    host = canonical_host(hostname)
    if host is None:
        return True
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def auth_mode(provider: Provider) -> str:
    """The provider's OAuth client mode; absent block means DCR."""

    auth = provider.get("auth")
    if isinstance(auth, dict):
        return str(auth.get("mode") or AUTH_MODE_DCR)
    return AUTH_MODE_DCR


def is_preregistered(provider: Provider) -> bool:
    """Whether the provider needs an operator-registered OAuth client."""

    return auth_mode(provider) == AUTH_MODE_PREREGISTERED


def redirect_host(provider: Provider) -> str:
    """The loopback host the provider's pre-registered redirect URI names."""

    auth = provider.get("auth") or {}
    return str(auth.get("redirect_host") or DEFAULT_REDIRECT_HOST)


def redirect_uri(provider: Provider) -> str | None:
    """The EXACT redirect URI an operator registers for ``provider``.

    ``None`` for a DCR provider: its listener picks a free port per mint and the
    registration carries whatever kiro-cli chose, so there is nothing to print.
    The value is also what the runtime is told to pin (``oauth.redirectUri``),
    which is the whole point -- one string, derived once, used in both places.
    """

    if not is_preregistered(provider):
        return None
    port = provider["auth"]["redirect_port"]
    return f"http://{redirect_host(provider)}:{port}{CALLBACK_PATH}"


def _validate_auth(raw: object, index: int) -> None:
    """Validate an ``auth`` block; see :class:`AuthConfig` for the contract."""

    if not isinstance(raw, dict):
        raise _validation_error(index, "auth must be an object")
    fields = set(raw)
    if "mode" not in fields:
        raise _validation_error(index, "auth.mode is required")
    extra = fields - _AUTH_FIELDS
    if extra:
        raise _validation_error(index, f"auth has unknown fields: {', '.join(sorted(extra))}")
    mode = raw["mode"]
    if mode not in _AUTH_MODES:
        raise _validation_error(index, "auth.mode must be one of " + ", ".join(sorted(_AUTH_MODES)))
    if mode == AUTH_MODE_DCR:
        # The block exists to name the exception; a DCR provider declaring
        # pre-registration details would be two contradictory statements.
        if fields != {"mode"}:
            raise _validation_error(index, "auth with mode 'dcr' must carry no other fields")
        return

    missing = {"confidential", "redirect_port", "registration_guide"} - fields
    if missing:
        raise _validation_error(
            index, "preregistered auth is missing fields: " + ", ".join(sorted(missing))
        )
    if not isinstance(raw["confidential"], bool):
        raise _validation_error(index, "auth.confidential must be a boolean")
    port = raw["redirect_port"]
    if isinstance(port, bool) or not isinstance(port, int):
        raise _validation_error(index, "auth.redirect_port must be an integer")
    if not _REDIRECT_PORT_MIN <= port <= _REDIRECT_PORT_MAX:
        raise _validation_error(
            index,
            f"auth.redirect_port must be between {_REDIRECT_PORT_MIN} and {_REDIRECT_PORT_MAX}",
        )
    if "redirect_host" in fields and raw["redirect_host"] not in REDIRECT_HOSTS:
        raise _validation_error(
            index, "auth.redirect_host must be one of " + ", ".join(REDIRECT_HOSTS)
        )
    guide = raw["registration_guide"]
    if not isinstance(guide, str) or not _REGISTRATION_GUIDE_PATTERN.fullmatch(guide):
        raise _validation_error(
            index,
            "auth.registration_guide must be a docs/guides path of the form "
            "oauth-app-registration/<slug>.md",
        )


def _validate_provider(raw: object, index: int) -> Provider:
    if not isinstance(raw, dict):
        raise _validation_error(index, "must be an object")

    fields = set(raw)
    missing = _PROVIDER_FIELDS - fields
    extra = fields - _PROVIDER_FIELDS - _OPTIONAL_PROVIDER_FIELDS
    if missing:
        raise _validation_error(index, f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise _validation_error(index, f"unknown fields: {', '.join(sorted(extra))}")

    for optional in ("client_id", "prerequisite_copy", "revoke_manual_path"):
        if optional in raw:
            value = raw[optional]
            if not isinstance(value, str) or not value.strip():
                raise _validation_error(
                    index, f"{optional} must be a non-empty string when present"
                )
    if "category" in raw and raw["category"] not in PROVIDER_CATEGORIES:
        raise _validation_error(
            index, "category must be one of " + ", ".join(sorted(PROVIDER_CATEGORIES))
        )

    if "auth" in raw:
        _validate_auth(raw["auth"], index)

    # Bound before the ``tool_aliases`` block below, which validates each alias
    # against this provider's own slug prefix.
    slug = raw["slug"]
    if not isinstance(slug, str) or not _SLUG_PATTERN.fullmatch(slug):
        raise _validation_error(index, "slug must contain lowercase letters, numbers, and hyphens")

    if "tool_aliases" in raw:
        # A malformed alias map would reach the emitted agent spec, where an
        # empty or non-string alias makes kiro-cli reject the whole spec and the
        # agent loses every tool. Validate the shape here, at load, rather than
        # letting it fail at the far end.
        alias_map = raw["tool_aliases"]
        if not isinstance(alias_map, dict) or not alias_map:
            raise _validation_error(index, "tool_aliases must be a non-empty object when present")
        for tool, alias in alias_map.items():
            if not isinstance(tool, str) or not _TOOL_NAME_PATTERN.fullmatch(tool):
                raise _validation_error(
                    index,
                    "tool_aliases keys must be tool names of letters, digits, "
                    "underscores, and hyphens",
                )
            if not isinstance(alias, str) or not _TOOL_NAME_PATTERN.fullmatch(alias):
                raise _validation_error(
                    index,
                    f"tool_aliases[{tool}] must be a tool name of letters, digits, "
                    "underscores, and hyphens",
                )
            if alias != f"{slug}_{tool}":
                # The emission pass recognises its OWN already-written aliases
                # by re-deriving this exact name from the ref, which is what lets a
                # withdrawn declaration's stale entry be cleaned up WITHOUT the
                # prefix test that would also claim a user's hand-written alias.
                # An alias that is not its own derivation would be emitted and then
                # never recognised, so it would outlive its own declaration.
                raise _validation_error(
                    index,
                    f"tool_aliases[{tool}] must be exactly '{slug}_{tool}' so the "
                    "emission pass can re-derive it and recognise its own output",
                )
        # No uniqueness check is needed: with every alias pinned to
        # ``<slug>_<tool>``, two tools in one provider cannot collapse onto one
        # alias (their keys differ, so their aliases differ) and two providers
        # cannot collide (their slugs differ). Derivation subsumes it.

    for field in ("name", "slug", "mcp_url", "revoke_page_url", "docs_url", "gotcha_copy"):
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise _validation_error(index, f"{field} must be a non-empty string")

    if not isinstance(raw["revoke_verified_note"], str) or not raw["revoke_verified_note"].strip():
        raise _validation_error(index, "revoke_verified_note must be a non-empty string")

    _iso_date(raw["revoke_verified_on"], index, "revoke_verified_on")

    for field in ("mcp_url", "revoke_page_url", "docs_url"):
        if not cast(str, raw[field]).startswith("https://"):
            raise _validation_error(index, f"{field} must use HTTPS")

    tier = raw["tier"]
    if isinstance(tier, bool) or not isinstance(tier, int) or tier not in (1, 2, 3):
        raise _validation_error(index, "tier must be 1, 2, or 3")

    scopes = raw["recommended_scopes"]
    if not isinstance(scopes, list) or any(
        not isinstance(scope, str) or not scope.strip() for scope in scopes
    ):
        raise _validation_error(index, "recommended_scopes must be a list of strings")
    if len(scopes) != len(set(scopes)):
        raise _validation_error(index, "recommended_scopes must not contain duplicates")

    fixture = raw["smoke_fixture"]
    if not isinstance(fixture, dict) or set(fixture) != _SMOKE_FIXTURE_FIELDS:
        raise _validation_error(index, "smoke_fixture must contain exactly tool and args")
    if not isinstance(fixture["tool"], str) or not fixture["tool"].strip():
        raise _validation_error(index, "smoke_fixture.tool must be a non-empty string")
    if not isinstance(fixture["args"], dict):
        raise _validation_error(index, "smoke_fixture.args must be an object")

    expectations = raw["l0_expectations"]
    if not isinstance(expectations, dict) or set(expectations) != _L0_EXPECTATION_FIELDS:
        raise _validation_error(
            index,
            "l0_expectations must contain exactly authorization_server, dcr, "
            "pkce, and verified_on",
        )
    for field in ("dcr", "pkce"):
        if not isinstance(expectations[field], bool):
            raise _validation_error(index, f"l0_expectations.{field} must be a boolean")
    captured_on = _iso_date(expectations["verified_on"], index, "l0_expectations.verified_on")
    # A future stamp would push the entry past every freshness tier forever,
    # which is the one way a hand-typed date could evade being noticed. Compared
    # in UTC because that is what the recorder stamps -- see utc_today.
    if captured_on > utc_today():
        raise _validation_error(index, "l0_expectations.verified_on must not be in the future")

    # The issuer is the ONE URL the recorder is allowed to fetch (see
    # l0_record: an advertised issuer that differs is reported, never followed),
    # so its shape is a security boundary, not just a data check. A literal IP
    # is refused outright: a reviewer cannot tell 10.0.0.5 from a legitimate
    # host at a glance, and no real provider publishes one.
    authorization_server = expectations["authorization_server"]
    if not isinstance(authorization_server, str):
        raise _validation_error(index, "l0_expectations.authorization_server must be an HTTPS URL")
    try:
        authorization_parts = urlsplit(authorization_server)
        authorization_parts.port
    except ValueError as error:
        raise _validation_error(
            index, "l0_expectations.authorization_server must be an HTTPS URL"
        ) from error
    if (
        authorization_parts.scheme != "https"
        or authorization_parts.hostname is None
        or authorization_parts.username is not None
        or authorization_parts.password is not None
        or authorization_parts.query
        or authorization_parts.fragment
    ):
        raise _validation_error(index, "l0_expectations.authorization_server must be an HTTPS URL")
    if is_local_host(authorization_parts.hostname):
        raise _validation_error(
            index,
            "l0_expectations.authorization_server must not be an IP literal or a local host",
        )

    for field in ("launch_gate_passed", "vendor_approval_pending"):
        if not isinstance(raw[field], bool):
            raise _validation_error(index, f"{field} must be a boolean")

    launch_gate_passed = cast(bool, raw["launch_gate_passed"])
    vendor_approval_pending = cast(bool, raw["vendor_approval_pending"])
    if tier == 3:
        if not vendor_approval_pending:
            raise _validation_error(index, "Tier 3 providers must be vendor-approval pending")
        if launch_gate_passed:
            raise _validation_error(index, "Tier 3 providers cannot pass the launch gate")
    elif vendor_approval_pending:
        raise _validation_error(index, "vendor approval is only meaningful for Tier 3")

    return cast(Provider, raw)


def _load_registry(path: Path = REGISTRY_PATH) -> tuple[Provider, ...]:
    try:
        with path.open(encoding="utf-8") as registry_file:
            raw_registry = json.load(registry_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RegistryValidationError(f"could not load provider registry: {error}") from error

    if not isinstance(raw_registry, list):
        raise RegistryValidationError("provider registry root must be an array")

    providers: list[Provider] = []
    seen_slugs: set[str] = set()
    seen_ports: dict[int, str] = {}
    for index, raw_provider in enumerate(raw_registry):
        provider = _validate_provider(raw_provider, index)
        slug = provider["slug"]
        if slug in seen_slugs:
            raise _validation_error(index, f"duplicate slug: {slug}")
        seen_slugs.add(slug)
        if is_preregistered(provider):
            # Two providers on one port cannot both hold their listener when the
            # warm path mints them together at boot, and the loser's card would
            # fail in a way no runbook explains.
            port = provider["auth"]["redirect_port"]
            if port in seen_ports:
                raise _validation_error(
                    index, f"auth.redirect_port {port} is already used by {seen_ports[port]}"
                )
            seen_ports[port] = slug
        providers.append(provider)

    if not providers:
        raise RegistryValidationError("provider registry must not be empty")
    return tuple(providers)


_PROVIDERS = _load_registry()
_PROVIDERS_BY_SLUG = {provider["slug"]: provider for provider in _PROVIDERS}


def _copy_provider(provider: Provider) -> Provider:
    """Keep callers from mutating the process-wide validated registry."""

    return cast(Provider, deepcopy(provider))


def get_all_registry_providers() -> list[Provider]:
    """Return every registry entry, including launch-gated and vendor-blocked entries."""

    return [_copy_provider(provider) for provider in _PROVIDERS]


def get_all_providers() -> list[Provider]:
    """Return providers not blocked on vendor approval, in stable registry order."""

    return [
        _copy_provider(provider)
        for provider in _PROVIDERS
        if not provider["vendor_approval_pending"]
    ]


def get_provider(slug: str) -> Provider | None:
    """Return the provider matching ``slug``, or ``None`` when it is unknown."""

    provider = _PROVIDERS_BY_SLUG.get(slug)
    return _copy_provider(provider) if provider is not None else None


def get_visible_providers() -> list[Provider]:
    """Return providers the gallery shows.

    Two ways in. A provider whose launch gate passed and which is not
    vendor-blocked, as before. And a PRE-REGISTERED provider regardless of the
    launch gate, because its card is not yet an offer to connect: until an
    operator has configured a client it renders as an instruction ("an
    administrator must configure an OAuth app") and hiding an instruction is
    how the operator never learns the step exists. The launch gate still
    governs the entry's own quality claims (revoke link, L0 baseline), which is
    why a pre-registered entry keeps ``launch_gate_passed`` honest instead of
    flipping it to become visible. Vendor approval remains a hard hide in both
    cases: a card no per-install app can ever satisfy is a dead end, not an
    instruction.
    """

    return [
        _copy_provider(provider)
        for provider in _PROVIDERS
        if not provider["vendor_approval_pending"]
        and (provider["launch_gate_passed"] or is_preregistered(provider))
    ]


def get_preregistered_providers() -> list[Provider]:
    """Every registry entry that needs an operator-registered client, gated or not."""

    return [_copy_provider(provider) for provider in _PROVIDERS if is_preregistered(provider)]


def get_tier(n: int) -> list[Provider]:
    """Return all providers in tier ``n``.

    Raises:
        ValueError: If ``n`` is not one of the three supported tiers.
    """

    if isinstance(n, bool) or n not in (1, 2, 3):
        raise ValueError("tier must be 1, 2, or 3")
    return [_copy_provider(provider) for provider in _PROVIDERS if provider["tier"] == n]


def stale_l0_baselines(
    providers: Iterable[Provider], *, as_of: date, max_age_days: int
) -> dict[str, str]:
    """Return slug -> ``verified_on`` for baselines older than ``max_age_days``.

    Pure and scope-agnostic: WHICH providers to hold to a given age is the
    caller's decision, which is what lets the same age arithmetic serve both
    freshness tiers -- a warning over every entry, and a hard failure over only
    the visible ones. A baseline exactly ``max_age_days`` old is still fresh.
    """

    oldest_allowed = as_of - timedelta(days=max_age_days)
    return {
        provider["slug"]: provider["l0_expectations"]["verified_on"]
        for provider in providers
        if date.fromisoformat(provider["l0_expectations"]["verified_on"]) < oldest_allowed
    }
