"""Account-free STATIC-METADATA conformance probe for official MCP providers.

Scope, stated first because the name invites a bigger reading: this probe checks
the STATIC OAuth metadata documents a provider publishes, and nothing else. It
fetches the protected-resource metadata at its well-known path (RFC 9728) and the
authorization-server metadata that names (RFC 8414), then diffs what the provider
advertises against the ``l0_expectations`` baseline committed in the registry.

It does NOT check challenge shape -- whether an unauthenticated request answers
401 with a well-formed ``WWW-Authenticate: Bearer resource_metadata=...`` -- and
a green run says nothing about it. Two of the seven registry providers fail that
today while serving perfectly good metadata (Stripe omits the parameter, Vercel
answers HTTP 500), so it needs its own baseline of known exceptions and is
deliberately a separate follow-up (L0b). Do not read this probe as covering it.

Every request is an unauthenticated GET of a static document: no credentials, no
accounts, no tokens, and nothing written to a provider, which is what lets this
run on a scheduled CI job with zero secrets.

ONE INVARIANT holds in both modes: the only authorization-server URL the probe
ever fetches is the one COMMITTED in the registry. It never dereferences the
issuer a provider advertises at runtime. A provider is an untrusted input, and
following its pointer would let a compromised one aim the runner at an arbitrary
or internal host -- and, in record mode, stamp that host into the registry. An
advertised issuer that differs from the committed one is a finding, reported for
human approval; see :mod:`kiro_crew.connections.l0_record`.

``--record`` refreshes the baseline from the live provider (DCR, PKCE, and the
capture date); the issuer itself only ever moves in a reviewed commit. Otherwise
the probe fetches and diffs: with ``--state`` the exit code is gated on
CONSECUTIVE failures (:mod:`kiro_crew.connections.l0_drift`) so one flaky night
never reports drift, and without it the exit code reflects this run alone, which
is what a human debugging a single provider locally wants. A FATAL error -- one
that stops the probe before it reaches any provider -- always exits non-zero
immediately and leaves the streak state untouched, because it is evidence about
the probe, not about seven providers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from kiro_crew.connections import l0_drift
from kiro_crew.connections.l0_record import ObservedBaseline, RecordError, record_baselines
from kiro_crew.connections.registry import (
    L0_VERIFICATION_WARN_AGE_DAYS,
    REGISTRY_PATH,
    L0Expectations,
    Provider,
    get_all_registry_providers,
    is_local_host,
    stale_l0_baselines,
    utc_today,
)

DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_METADATA_BYTES = 16 * 1024
_READ_CHUNK_BYTES = 4096
_REPORT_SCHEMA_VERSION = 3
_PROBE_SCOPE = "static-metadata"
_WELL_KNOWN_AUTHORIZATION = "/.well-known/oauth-authorization-server"
_WELL_KNOWN_RESOURCE = "/.well-known/oauth-protected-resource"


class ProbeResult(TypedDict):
    """Machine-readable result for one registry provider."""

    slug: str
    name: str
    ok: bool
    checks: dict[str, bool]
    errors: list[str]
    observed: ObservedBaseline | None
    duration_ms: int


def _https_url(value: object, field: str) -> str:
    """Require an absolute, credential-free HTTPS URL on a non-local host."""

    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError as error:
        raise ValueError(f"{field} must contain a valid port") from error
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ValueError(f"{field} must be an absolute HTTPS URL")
    if is_local_host(parts.hostname):
        raise ValueError(f"{field} must not be an IP literal or a local host")
    return value


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.hostname is None:
        raise ValueError("URL must contain a hostname")
    return parts.scheme.lower(), parts.hostname.lower(), parts.port or 443


def _validate_issuer(value: object, field: str = "issuer") -> str:
    """Validate an issuer identifier and return it VERBATIM.

    RFC 8414 §2 makes the issuer an identity compared by "simple string
    comparison" -- code point for code point. So this deliberately normalizes
    NOTHING: no host lowercasing, no dropping of an explicit ``:443``, no
    trailing-slash trimming. Every one of those would make two distinct issuer
    strings compare equal, which is exactly the substitution the comparison
    exists to catch, and an authorization server that answers for
    ``https://Auth.Example.com`` when we committed ``https://auth.example.com``
    has told us something we should not silently absorb.

    The one transformation in the module is the single trailing slash removed
    when CONSTRUCTING the well-known URL (see ``_authorization_metadata_url``),
    which is a path-insertion detail and never touches a compared value.
    """

    url = _https_url(value, field)
    if urlsplit(url).query:
        raise ValueError(f"{field} must not carry a query string")
    return url


def _resource_metadata_urls(mcp_url: str) -> list[str]:
    """Candidate protected-resource metadata URLs for an MCP endpoint (RFC 9728).

    Path-insertion first, then the origin root, which is the order RFC 9728 §3.1
    defines and what a real client tries. GitLab needs the second form for one of
    its two MCP endpoints, so both are load-bearing rather than belt-and-braces.
    """

    parts = urlsplit(mcp_url)
    path = parts.path.rstrip("/")
    candidates = [urlunsplit((parts.scheme, parts.netloc, _WELL_KNOWN_RESOURCE + path, "", ""))]
    root = urlunsplit((parts.scheme, parts.netloc, _WELL_KNOWN_RESOURCE, "", ""))
    if root not in candidates:
        candidates.append(root)
    return candidates


def _authorization_metadata_url(issuer: str) -> str:
    """The RFC 8414 §3.1 well-known URL for an issuer identifier."""

    parts = urlsplit(issuer)
    path = _WELL_KNOWN_AUTHORIZATION + parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def _get_json(
    session: aiohttp.ClientSession, url: str, timeout_seconds: float
) -> dict[str, Any]:
    """GET a JSON metadata document under a hard size bound.

    Redirects are not followed and compressed responses are refused. Both are
    deliberate and both score a redirect-fronted or gzip-forcing well-known
    endpoint as a FAILURE: see the runbook in
    .github/workflows/connections-l0.yml for why, and what to do about it.
    """

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(
        url,
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        allow_redirects=False,
        auto_decompress=False,
        timeout=timeout,
    ) as response:
        if response.status != 200:
            raise ValueError(f"{url} returned HTTP {response.status}, expected 200")
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            raise ValueError(f"{url} returned unsupported Content-Encoding {encoding!r}")

        # Bounded read: an endpoint we do not control must not be able to stream
        # an arbitrarily large body into the runner.
        body = bytearray()
        while True:
            remaining = _MAX_METADATA_BYTES + 1 - len(body)
            chunk = await response.content.read(min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > _MAX_METADATA_BYTES:
                raise ValueError(f"{url} exceeded the {_MAX_METADATA_BYTES}-byte limit")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError(f"{url} did not return JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{url} returned a non-object JSON document")
    return cast(dict[str, Any], payload)


def _resource_matches(resource: object, mcp_url: str) -> bool:
    """Whether a protected-resource document covers ``mcp_url``.

    ``resource`` is a single string in RFC 9728, and that is what most providers
    send. GitLab sends an ARRAY of the endpoints one document covers; accepting
    that is tolerating a spec deviation on a field we only use to confirm the
    document is about the right host, while the field we actually baseline
    (``authorization_servers``) stays unambiguous.
    """

    values = resource if isinstance(resource, list) else [resource]
    origins = set()
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            origins.add(_origin(_https_url(value, "protected resource")))
        except ValueError:
            continue
    return _origin(mcp_url) in origins


def _read_resource_metadata(document: dict[str, Any], mcp_url: str) -> str:
    """Validate protected-resource metadata; return the issuer it ADVERTISES.

    The returned value is reported, never fetched -- see the module invariant.
    """

    if not _resource_matches(document.get("resource"), mcp_url):
        raise ValueError("protected resource metadata does not cover the MCP endpoint")
    servers = document.get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        raise ValueError("authorization_servers must be a non-empty list")
    return _validate_issuer(servers[0], "advertised authorization server")


def _read_authorization_metadata(
    document: dict[str, Any], requested_issuer: str
) -> tuple[bool, bool]:
    """Validate authorization-server metadata; return (DCR, PKCE-S256).

    ``issuer`` is compared to ``requested_issuer`` by exact code-point equality
    per RFC 8414 §2, so a tenant or realm path substitution on the same origin is
    caught -- and so is a host-case or explicit-port variant, which a normalizing
    comparison would wave through.
    """

    issuer = _validate_issuer(document.get("issuer"), "issuer")
    if issuer != requested_issuer:
        raise ValueError(
            f"authorization metadata issuer {issuer!r} is not the requested "
            f"issuer {requested_issuer!r}"
        )
    _https_url(document.get("authorization_endpoint"), "authorization_endpoint")
    _https_url(document.get("token_endpoint"), "token_endpoint")

    registration = document.get("registration_endpoint")
    if registration is not None:
        _https_url(registration, "registration_endpoint")
    methods = document.get("code_challenge_methods_supported", [])
    if not isinstance(methods, list) or any(not isinstance(item, str) for item in methods):
        raise ValueError("code_challenge_methods_supported must be a list of strings")
    return registration is not None, "S256" in methods


def _result(
    provider: Provider,
    *,
    checks: dict[str, bool],
    errors: list[str],
    observed: ObservedBaseline | None,
    duration_ms: int,
) -> ProbeResult:
    return {
        "slug": provider["slug"],
        "name": provider["name"],
        "ok": not errors and bool(checks) and all(checks.values()),
        "checks": checks,
        "errors": errors,
        "observed": observed,
        "duration_ms": duration_ms,
    }


async def probe_provider(
    session: aiohttp.ClientSession,
    provider: Provider,
    *,
    timeout_seconds: float,
    record: bool = False,
) -> ProbeResult:
    """Probe one provider without credentials and aggregate every check.

    The authorization metadata is always fetched from the COMMITTED issuer, in
    both modes. With ``record`` set, DCR and PKCE are captured rather than
    asserted, and an advertised issuer that differs from the committed one is
    captured for human approval instead of failing -- the recorder refuses to
    write it either way.
    """

    started = time.monotonic()
    checks = {
        "protected_resource_metadata": False,
        "authorization_server_metadata": False,
        "dcr_expectation": False,
        "pkce_expectation": False,
    }
    errors: list[str] = []
    committed: L0Expectations = provider["l0_expectations"]
    observed: ObservedBaseline | None = None

    try:
        committed_issuer = _validate_issuer(
            committed["authorization_server"], "committed authorization server"
        )
    except ValueError as error:
        # Defence in depth: the registry validator already refuses this shape, so
        # reaching here means the loader was bypassed.
        return _result(
            provider,
            checks=checks,
            errors=[f"committed baseline is unusable: {error}"],
            observed=None,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    advertised: str | None = None
    for candidate in _resource_metadata_urls(provider["mcp_url"]):
        try:
            metadata = await _get_json(session, candidate, timeout_seconds)
            advertised = _read_resource_metadata(metadata, provider["mcp_url"])
            checks["protected_resource_metadata"] = True
            # A later candidate succeeding means the earlier miss was the
            # expected path-insertion probe, not a finding worth reporting.
            errors.clear()
            break
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            errors.append(f"protected resource discovery: {error}")

    if advertised is not None and advertised != committed_issuer:
        message = (
            f"advertised authorization server {advertised!r} differs from the "
            f"committed {committed_issuer!r}"
        )
        if record:
            # Captured so the recorder can report it for approval; the fetch
            # below still goes to the committed issuer.
            print(f"  {provider['slug']}: {message} -- needs human approval")
        else:
            errors.append(message)

    if checks["protected_resource_metadata"]:
        try:
            metadata = await _get_json(
                session, _authorization_metadata_url(committed_issuer), timeout_seconds
            )
            dcr, pkce = _read_authorization_metadata(metadata, committed_issuer)
            checks["authorization_server_metadata"] = True
            observed = {
                "authorization_server": advertised or committed_issuer,
                "dcr": dcr,
                "pkce": pkce,
            }
            if record:
                checks["dcr_expectation"] = True
                checks["pkce_expectation"] = True
            else:
                checks["dcr_expectation"] = dcr == committed["dcr"]
                checks["pkce_expectation"] = pkce == committed["pkce"]
                if not checks["dcr_expectation"]:
                    errors.append(f"DCR advertised={dcr}, expected={committed['dcr']}")
                if not checks["pkce_expectation"]:
                    errors.append(f"PKCE S256 advertised={pkce}, expected={committed['pkce']}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            errors.append(f"authorization server discovery: {error}")

    return _result(
        provider,
        checks=checks,
        errors=errors,
        observed=observed,
        duration_ms=round((time.monotonic() - started) * 1000),
    )


async def probe_all(
    session: aiohttp.ClientSession,
    providers: Sequence[Provider],
    *,
    concurrency: int,
    timeout_seconds: float,
    record: bool = False,
) -> list[ProbeResult]:
    """Probe providers with a hard cap on simultaneous provider request chains."""

    semaphore = asyncio.Semaphore(concurrency)
    # Each provider makes up to three sequential requests, so the per-request
    # timeout does not bound the chain. This does.
    budget = timeout_seconds * 3 + 1

    async def limited(provider: Provider) -> ProbeResult:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    probe_provider(
                        session, provider, timeout_seconds=timeout_seconds, record=record
                    ),
                    timeout=budget,
                )
            except asyncio.TimeoutError:
                return _result(
                    provider,
                    checks={},
                    errors=["provider probe exceeded its total timeout"],
                    observed=None,
                    duration_ms=round(budget * 1000),
                )

    return list(await asyncio.gather(*(limited(provider) for provider in providers)))


def build_report(
    results: Sequence[ProbeResult], *, fatal: BaseException | None = None
) -> dict[str, Any]:
    """Render probe results into the report the workflow uploads."""

    failed = sum(not result["ok"] for result in results)
    report: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        # Named in the report so a reader cannot mistake a green run for
        # challenge-shape coverage. See the module docstring.
        "scope": _PROBE_SCOPE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": fatal is None and failed == 0,
        "provider_count": len(results),
        "failed_count": failed,
        "providers": list(results),
    }
    if fatal is not None:
        report["fatal_error"] = f"{type(fatal).__name__}: {fatal}"
    return report


async def run_probe(
    *, concurrency: int, timeout_seconds: float, record: bool = False
) -> list[ProbeResult]:
    """Probe every registry entry, launch-gated or not."""

    async with aiohttp.ClientSession(auto_decompress=False) as session:
        return await probe_all(
            session,
            get_all_registry_providers(),
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
            record=record,
        )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Account-free static OAuth metadata conformance probe."
    )
    parser.add_argument("--report", type=Path, default=Path("connections-l0-report.json"))
    parser.add_argument("--concurrency", type=_positive_int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="streak file carried between runs; enables the consecutive-failure gate",
    )
    parser.add_argument(
        "--drift-threshold",
        type=_positive_int,
        default=l0_drift.DEFAULT_DRIFT_THRESHOLD,
        help="consecutive failing runs before a provider is reported as drifted",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="refresh DCR, PKCE and the capture date from the live providers",
    )
    parser.add_argument(
        "--prior-run-expected",
        action="store_true",
        help=(
            "a previous run of this workflow exists, so an absent state file means "
            "the artifact was lost rather than that nothing has ever run"
        ),
    )
    parser.add_argument(
        "--fail-on-stale",
        action="store_true",
        help=(
            "also fail when a baseline is overdue for re-recording; intended for "
            "the nightly, so the reminder lands there instead of on every PR"
        ),
    )
    return parser


def _staleness(*, fail_on_stale: bool) -> dict[str, Any]:
    """Which baselines are overdue for a re-record, and what to do about it.

    Reported unconditionally because it costs nothing and a reader of the report
    should not have to go compute it. Only the NIGHTLY turns it into a failure
    (``--fail-on-stale``): the remedy needs live network, so a red PR suite would
    be an outage nobody in CI could clear, while a red nightly reaches someone
    who can run one command.
    """

    overdue = stale_l0_baselines(
        get_all_registry_providers(),
        as_of=utc_today(),
        max_age_days=L0_VERIFICATION_WARN_AGE_DAYS,
    )
    return {
        "warn_after_days": L0_VERIFICATION_WARN_AGE_DAYS,
        "enforced": fail_on_stale,
        "overdue": overdue,
        "runbook": (
            "run `python -m kiro_crew.connections.l0_probe --record` on a "
            "networked machine and commit src/kiro_crew/connections/registry.json"
        ),
    }


def _run_record(args: argparse.Namespace) -> int:
    """Refresh baselines from the live providers. Returns a process exit code."""

    results = asyncio.run(
        run_probe(concurrency=args.concurrency, timeout_seconds=args.timeout, record=True)
    )
    observed = {
        result["slug"]: result["observed"]
        for result in results
        if result["ok"] and result["observed"] is not None
    }
    try:
        # utc_today, not the local date: the registry's future-date guard compares
        # in UTC, so stamping locally would make a fresh baseline invalid for
        # anyone east of UTC and reject one for anyone west of it.
        outcome = record_baselines(REGISTRY_PATH, observed, utc_today())
    except RecordError as error:
        print(f"record failed: {error}")
        return 1

    print(f"reached {len(observed)} of {len(results)} providers")
    print(f"refreshed: {', '.join(outcome.changed) if outcome.changed else '(no change)'}")
    for slug, issuer in sorted(outcome.needs_approval.items()):
        print(
            f"NEEDS APPROVAL {slug}: provider now advertises issuer {issuer}. "
            "Not written. Update l0_expectations.authorization_server in a "
            "reviewed commit if this move is legitimate, then re-run --record."
        )
    unreachable = [result for result in results if result["slug"] not in observed]
    for result in unreachable:
        print(f"could not reach {result['slug']}: {'; '.join(result['errors'])}")
    return 1 if unreachable or outcome.needs_approval else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.record:
        return _run_record(args)

    def emit(report: dict[str, Any]) -> None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="")

    try:
        results = asyncio.run(
            run_probe(concurrency=args.concurrency, timeout_seconds=args.timeout)
        )
    except Exception as error:
        # A fatal error stopped the probe before it reached any provider, so it
        # is evidence about the PROBE, not about the providers. Advancing seven
        # streaks would spend two nights of the drift budget on a broken runner
        # and then blame the providers; leave the state exactly as it was and
        # fail now, where it is legible.
        report = build_report([], fatal=error)
        report["gate"] = "fatal"
        emit(report)
        print(f"FATAL: probe did not run: {report['fatal_error']} (streak state untouched)")
        return 1

    report = build_report(results)
    report["staleness"] = _staleness(fail_on_stale=args.fail_on_stale)
    ok = bool(report["ok"])
    if args.state is None:
        report["gate"] = "immediate"
    else:
        prior = l0_drift.load_streaks(
            args.state, prior_run_expected=args.prior_run_expected
        )
        if prior.discarded is not None:
            # A dropped artifact resets streaks and would otherwise hide the
            # third failing night. Say so in the log AND in the report.
            print(f"WARNING: prior streak state discarded ({prior.discarded}); starting over")
        streaks = l0_drift.update_streaks(
            prior.streaks, {result["slug"]: result["ok"] for result in results}
        )
        report["gate"] = "drift"
        report["drift"] = l0_drift.verdict(
            streaks, threshold=args.drift_threshold, prior=prior
        )
        l0_drift.write_state(args.state, streaks)
        ok = bool(report["drift"]["ok"])

    overdue = report["staleness"]["overdue"]
    if overdue:
        print(f"STALE BASELINES: {overdue} -- {report['staleness']['runbook']}")
        if args.fail_on_stale:
            ok = False

    emit(report)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
