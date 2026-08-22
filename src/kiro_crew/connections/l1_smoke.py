"""L1: scheduled smoke run over providers that already hold a persisted grant.

Middle rung of the Connections launch ladder (L0 = the account-free metadata
probe, L2 = the manual UI gate); the full contract -- verdict table, runbook,
known gap -- lives in ``docs/architecture/design-notes/connections-l1-smoke.md``.
The invariants a reader must not miss: **Kiro Crew holds no token** (kiro-cli
injects the bearer inside its own process, so a managed provider's healthy reply
is an OAuth challenge -- graded ``GRANT_HELD``, never ``NEEDS_RECONSENT``, which
is reserved for rejections this process can attribute); **L1 never initiates
consent** (an absent grant is ``SKIPPED``, never a failure); and an all-skipped
sweep is ``vacuous``, not green. ``depth`` says how far each exchange got.

Grant presence is observed via :func:`~kiro_crew.connections.mint.grant_present`
-- paired artifacts stat-ed, never opened, so no token byte enters this process;
one implementation of kiro-cli's key mirror; no SEL read audit, an operator CLI
in ``l0_probe``'s class. Transport reuses ``mcp_discovery``'s reviewed plumbing
(private names imported on purpose so the challenge rule cannot drift), plus the
``Mcp-Session-Id`` echo and ``notifications/initialized`` a stateful server wants.

The sweep STOPS at ``tools/list`` and never issues ``tools/call``: invoking a
provider tool from here would reach the provider OUTSIDE the governed dispatch
path, so an enterprise-denied tool would be called anyway with no SEL event, and
an unaudited side channel is worse than a narrower verdict. ``fixture_tool`` is
therefore checked for ADVERTISEMENT only, which ``tools/list`` already answers;
exercising it belongs to the ACP-side slice where kiro-cli's governed dispatch
owns the call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence, TypedDict

import aiohttp

from kiro_crew.connections.mint import grant_key, grant_present, kiro_oauth_cache_dir
from kiro_crew.connections.registry import Provider, get_all_registry_providers
from kiro_crew.connections.tool_aliases import normalized_endpoint
from kiro_crew.mcp_discovery import (
    McpServerInfo,
    _needs_authorization,
    _read_jsonrpc_response,
    list_servers,
    redact_mcp_error,
)

__all__ = [
    "SmokeResult",
    "build_report",
    "configured_remote_servers",
    "grant_key",
    "grant_present",
    "kiro_oauth_cache_dir",
    "main",
    "run_l1",
    "smoke_all",
    "smoke_provider",
]

DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_SECONDS = 20.0
_REPORT_SCHEMA_VERSION = 1
_MCP_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "kirocrew-l1-smoke", "version": "1"}
_MAX_ERROR_CHARS = 200
# A provider's whole run -- two requests plus a notification -- gets a budget
# derived from the per-request one, bounding a stall-every-leg server.
_TOTAL_BUDGET_MULTIPLIER = 3

# JSON-RPC error substrings meaning "the grant no longer works" (not "the call
# was wrong"): only these grade NEEDS_RECONSENT past initialize, so a provider
# outage is never reported as a consent problem.
_RECONSENT_TOKENS = ("unauthorized", "invalid_token", "invalid_grant", "forbidden")

Verdict = Literal["PASS", "GRANT_HELD", "NEEDS_RECONSENT", "FAIL", "SKIPPED"]
Depth = Literal["tools_list", "challenge", "none"]

#: Non-failing verdicts: a skip is an unseeded box; a challenge is expected.
_HEALTHY: frozenset[str] = frozenset({"PASS", "GRANT_HELD", "SKIPPED"})
_VERDICTS: tuple[Verdict, ...] = ("PASS", "GRANT_HELD", "NEEDS_RECONSENT", "FAIL", "SKIPPED")


class SmokeResult(TypedDict):
    """Machine-readable L1 verdict for one registry provider. ``depth`` says how
    far the exchange got, so a ``GRANT_HELD`` (stopped at the challenge) is
    never inferred from the verdict vocabulary alone."""

    slug: str
    name: str
    verdict: Verdict
    depth: Depth
    installed: bool
    grant_present: bool
    credential_presented: bool
    fixture_tool: str
    fixture_tool_advertised: bool
    tools_listed: int
    errors: list[str]
    duration_ms: int


class _McpChallenge(Exception):
    """The endpoint is alive and asked to authenticate. Expected, not a fault."""


class _McpAuthError(Exception):
    """A credential this process presented was refused by the provider."""


class _McpCallError(Exception):
    """The exchange reached the provider but did not produce a usable result."""


def configured_remote_servers() -> dict[str, McpServerInfo]:
    """Configured remote MCP servers keyed by name, minus ``disabled`` entries --
    excluded for the reason ``probe_server`` refuses them: a second entry point
    must restate the consent gate or become a way around it (SKIPPED, untouched)."""
    return {
        server.name: server for server in list_servers() if server.is_remote and not server.disabled
    }


def _presents_credential(headers: dict[str, str]) -> bool:
    """Whether the server entry carries its own ``Authorization`` header -- the
    distinction the verdicts turn on: with no credential of our own a 401 is the
    provider correctly protecting itself; with one, the credential was refused."""
    return any(key.lower() == "authorization" for key in headers)


def _reconsent_error(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    message = str(error.get("message", "")).lower()
    return any(term in message for term in _RECONSENT_TOKENS)


def _result_payload(data: dict[str, Any]) -> dict[str, Any]:
    """A JSON-RPC ``result``, converting an ``error`` member into an exception."""
    error = data.get("error")
    if error is not None:
        if _reconsent_error(error):
            raise _McpAuthError(str(error.get("message", "unauthorized")))
        message = error.get("message", "unknown error") if isinstance(error, dict) else str(error)
        raise _McpCallError(str(message))
    result = data.get("result")
    if not isinstance(result, dict):
        raise _McpCallError("response carried no result object")
    return result


async def _post(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str | None]:
    """POST one JSON-RPC message and return its payload plus any session id.

    A notification carries no ``id`` and no body, so any 2xx is success there
    (servers answer ``notifications/initialized`` with 202); a request must be
    answered 200 with a payload. Redirects are not followed: a moved endpoint
    followed silently would smoke a different server than the registry names.
    """

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    is_notification = body.get("id") is None
    async with session.post(
        url, json=body, headers=headers, allow_redirects=False, timeout=timeout
    ) as response:
        if _needs_authorization(response.status, response.headers, headers):
            raise _McpChallenge(f"provider challenged with HTTP {response.status}")
        if response.status in (401, 403) and _presents_credential(headers):
            raise _McpAuthError(f"presented credential refused with HTTP {response.status}")
        # A tokenless 403 with no ``WWW-Authenticate`` is deliberately NOT
        # claimed here: a refusal this process cannot attribute to the grant
        # (edge proxy, geo block) falls through to the generic non-2xx FAIL.
        accepted = 200 <= response.status < 300 if is_notification else response.status == 200
        if not accepted:
            raise _McpCallError(f"provider returned HTTP {response.status}, expected 200")
        session_id = response.headers.get("Mcp-Session-Id")
        if is_notification:
            return {}, session_id
        return await _read_jsonrpc_response(response), session_id


async def _connect(
    session: aiohttp.ClientSession,
    server: McpServerInfo,
    *,
    timeout_seconds: float,
) -> dict[str, str]:
    """Run ``initialize`` and return the headers later requests must carry."""
    headers = {
        **server.headers,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
    }
    data, session_id = await _post(
        session,
        server.url,
        headers,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        },
        timeout_seconds=timeout_seconds,
    )
    _result_payload(data)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    await _post(
        session,
        server.url,
        headers,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        timeout_seconds=timeout_seconds,
    )
    return headers


async def _list_tools(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    *,
    timeout_seconds: float,
) -> list[str]:
    data, _ = await _post(
        session,
        url,
        headers,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        timeout_seconds=timeout_seconds,
    )
    tools = _result_payload(data).get("tools", [])
    if not isinstance(tools, list):
        raise _McpCallError("tools/list did not return a list")
    return [name for tool in tools if isinstance(tool, dict) and (name := tool.get("name", ""))]


def _blank_result(provider: Provider, **overrides: Any) -> SmokeResult:
    """A result with every field present, so no consumer sees a partial record."""
    result: SmokeResult = {
        "slug": provider["slug"],
        "name": provider["name"],
        "verdict": "FAIL",
        "depth": "none",
        "installed": False,
        "grant_present": False,
        "credential_presented": False,
        "fixture_tool": provider["smoke_fixture"]["tool"],
        "fixture_tool_advertised": False,
        "tools_listed": 0,
        "errors": [],
        "duration_ms": 0,
    }
    result.update(overrides)  # type: ignore[typeddict-item]
    return result


def _redacted_detail(error: BaseException, headers: object) -> str:
    """Redact THEN truncate, like ``_sanitize_probe_error``: truncating first
    bisects a reflected credential, and the configured-value scrubber matches
    whole values only -- the fragment would reach the artifact."""
    return redact_mcp_error(str(error), headers)[:_MAX_ERROR_CHARS]


async def smoke_provider(
    session: aiohttp.ClientSession,
    provider: Provider,
    server: McpServerInfo | None,
    *,
    timeout_seconds: float,
    cache_dir: Path | None = None,
) -> SmokeResult:
    """The L1 verdict for one provider. Never initiates consent."""
    started = time.monotonic()

    def elapsed() -> int:
        return round((time.monotonic() - started) * 1000)

    # grant_present's contract: off the loop AND under the per-request bound, so
    # a stalled mount answers as itself. Past here budget expiry only happens
    # inside the exchange (SKIPPED returns are synchronous) -- smoke_all relies on it.
    try:
        grant = await asyncio.wait_for(
            asyncio.to_thread(grant_present, provider["mcp_url"], cache_dir=cache_dir),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return _blank_result(
            provider,
            verdict="FAIL",
            installed=server is not None,
            errors=["grant presence check timed out; is the OAuth cache on a stalled mount?"],
            duration_ms=elapsed(),
        )
    if server is None:
        return _blank_result(
            provider,
            verdict="SKIPPED",
            grant_present=grant,
            errors=["provider is not a configured MCP server on this host"],
            duration_ms=elapsed(),
        )
    if normalized_endpoint(server.url) != normalized_endpoint(provider["mcp_url"]):
        # tool_aliases' rule: identity is proven by endpoint, never by the key.
        return _blank_result(
            provider,
            verdict="SKIPPED",
            grant_present=grant,
            errors=["configured server endpoint does not match the registry mcp_url"],
            duration_ms=elapsed(),
        )
    if not grant:
        return _blank_result(
            provider,
            verdict="SKIPPED",
            installed=True,
            errors=["no persisted grant on this host; L1 does not initiate consent"],
            duration_ms=elapsed(),
        )
    fixture_tool = provider["smoke_fixture"]["tool"]
    credentialed = _presents_credential(server.headers)
    verdict: Verdict = "PASS"
    depth: Depth = "none"
    advertised = False
    tools: list[str] = []
    errors: list[str] = []
    try:
        headers = await _connect(session, server, timeout_seconds=timeout_seconds)
        tools = await _list_tools(session, server.url, headers, timeout_seconds=timeout_seconds)
        depth = "tools_list"
        advertised = fixture_tool in tools
        if not advertised:
            raise _McpCallError(f"{fixture_tool} is no longer advertised by the provider")
    except _McpChallenge as error:
        # Grant on disk, endpoint alive and still protected -- the GRANT_HELD
        # rung's whole point; recorded as evidence of WHICH challenge.
        verdict, depth = "GRANT_HELD", "challenge"
        errors.append(_redacted_detail(error, server.headers))
    except _McpAuthError as error:
        verdict = "NEEDS_RECONSENT"
        errors.append(_redacted_detail(error, server.headers))
    except (_McpCallError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        verdict = "FAIL"
        errors.append(_redacted_detail(error, server.headers) or type(error).__name__)
    return _blank_result(
        provider,
        verdict=verdict,
        depth=depth,
        installed=True,
        grant_present=True,
        credential_presented=credentialed,
        fixture_tool_advertised=advertised,
        tools_listed=len(tools),
        errors=errors,
        duration_ms=elapsed(),
    )


async def smoke_all(
    session: aiohttp.ClientSession,
    providers: Sequence[Provider],
    servers: dict[str, McpServerInfo],
    *,
    concurrency: int,
    timeout_seconds: float,
    cache_dir: Path | None = None,
) -> list[SmokeResult]:
    """Smoke every provider with a hard cap on simultaneous exchanges."""
    semaphore = asyncio.Semaphore(concurrency)
    total_budget = timeout_seconds * _TOTAL_BUDGET_MULTIPLIER + 1

    async def limited(provider: Provider) -> SmokeResult:
        async with semaphore:
            started = time.monotonic()
            try:
                return await asyncio.wait_for(
                    smoke_provider(
                        session,
                        provider,
                        servers.get(provider["slug"]),
                        timeout_seconds=timeout_seconds,
                        cache_dir=cache_dir,
                    ),
                    timeout=total_budget,
                )
            except asyncio.TimeoutError:
                return _blank_result(
                    provider,
                    verdict="FAIL",
                    installed=provider["slug"] in servers,
                    # Budget expiry only happens inside the exchange (SKIPPED
                    # paths are synchronous, the stat separately bounded), so
                    # the grant was proven present -- the default False would
                    # falsify the field this rung is named after.
                    grant_present=True,
                    errors=["provider smoke run exceeded its total timeout"],
                    duration_ms=round((time.monotonic() - started) * 1000),
                )

    outcomes = await asyncio.gather(
        *(limited(provider) for provider in providers), return_exceptions=True
    )
    results: list[SmokeResult] = []
    for provider, outcome in zip(providers, outcomes):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome  # cancellation and exits must still propagate
            # One provider's stray defect must not erase the other rows --
            # WHICH provider regressed is the report's whole point.
            srv = servers.get(provider["slug"])
            detail = _redacted_detail(outcome, srv.headers if srv else {})
            results.append(
                _blank_result(
                    provider,
                    verdict="FAIL",
                    installed=provider["slug"] in servers,
                    errors=[detail or type(outcome).__name__],
                )
            )
        else:
            results.append(outcome)
    return results


def build_report(results: Sequence[SmokeResult], *, min_exercised: int = 0) -> dict[str, Any]:
    """Aggregate verdicts into the report the scheduled lane gates on: skips and
    challenges never fail the run (module docstring), and a run exercising fewer
    than ``min_exercised`` providers is ``vacuous`` and NOT ok -- an all-skipped
    sweep would otherwise be a green that established nothing."""

    counts = {verdict: 0 for verdict in _VERDICTS}
    for result in results:
        counts[result["verdict"]] += 1
    exercised = len(results) - counts["SKIPPED"]
    vacuous = exercised < min_exercised
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": all(result["verdict"] in _HEALTHY for result in results) and not vacuous,
        "vacuous": vacuous,
        "min_exercised": min_exercised,
        "exercised_count": exercised,
        "provider_count": len(results),
        "pass_count": counts["PASS"],
        "grant_held_count": counts["GRANT_HELD"],
        "needs_reconsent_count": counts["NEEDS_RECONSENT"],
        "failed_count": counts["FAIL"],
        "skipped_count": counts["SKIPPED"],
        "providers": list(results),
    }


async def run_l1(
    *, concurrency: int, timeout_seconds: float, min_exercised: int = 0
) -> dict[str, Any]:
    """Smoke every registry provider that already holds a grant on this host."""
    providers = get_all_registry_providers()
    # Off-loop AND bounded, like the grant stat: a stalled home must not wedge us.
    try:
        servers = await asyncio.wait_for(
            asyncio.to_thread(configured_remote_servers), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        raise RuntimeError(
            "server inventory read timed out; is the settings home on a stalled mount?"
        ) from None
    async with aiohttp.ClientSession() as session:
        results = await smoke_all(
            session,
            providers,
            servers,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
        )
    return build_report(results, min_exercised=min_exercised)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke the Connections providers that already hold a grant on this host."
    )
    parser.add_argument("--report", type=Path, default=Path("connections-l1-report.json"))
    parser.add_argument("--concurrency", type=_positive_int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--min-exercised",
        type=_non_negative_int,
        default=0,
        help=(
            "Fail the run unless at least N providers were non-SKIPPED. The lane passes "
            "1 so an unseeded box reports vacuous instead of a green proving nothing."
        ),
    )
    return parser


def _fatal_report(error: BaseException, *, min_exercised: int) -> dict[str, Any]:
    """A report for a failure that stopped the sweep before any provider ran --
    evidence about the HARNESS, not the providers: no per-provider verdicts."""
    report = build_report([], min_exercised=min_exercised)
    report["ok"] = False
    # Redact before report/stdout/artifact; {} engages the site-wide scanners.
    report["fatal_error"] = redact_mcp_error(f"{type(error).__name__}: {error}", {})
    return report


def _persist_report(path: Path, report: dict[str, Any]) -> None:
    """Write and echo the report -- INSIDE the loop on the happy path:
    ``asyncio.run``'s shutdown joins any worker still blocked on a stalled mount
    (bounded only on 3.12+), so the report must reach disk before that join or
    an honest FAIL row exists only in memory and the upload finds nothing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    async def _run_and_persist() -> dict[str, Any]:
        # Fatal path persists in-loop too, or an exception after a leaked worker
        # would persist only after the shutdown join -- round 2's trap, one seam over.
        try:
            report = await run_l1(
                concurrency=args.concurrency,
                timeout_seconds=args.timeout,
                min_exercised=args.min_exercised,
            )
        except Exception as error:  # noqa: BLE001 -- reported, never swallowed
            report = _fatal_report(error, min_exercised=args.min_exercised)
        await asyncio.to_thread(_persist_report, args.report, report)
        return report

    try:
        report = asyncio.run(_run_and_persist())
    except Exception as error:  # noqa: BLE001 -- loop setup / persist failures
        report = _fatal_report(error, min_exercised=args.min_exercised)
        _persist_report(args.report, report)
    if report.get("fatal_error"):
        print(f"FATAL: L1 did not run: {report['fatal_error']}")
    elif report["vacuous"]:
        print(
            f"VACUOUS: {report['exercised_count']} of {report['provider_count']} providers "
            f"exercised, {report['min_exercised']} required -- one-time consent click per "
            "provider; see docs/architecture/design-notes/connections-l1-smoke.md."
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
