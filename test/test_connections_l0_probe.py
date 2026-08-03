"""Hermetic tests for the Connections L0 metadata probe."""

import asyncio
import json
from copy import deepcopy

import pytest

from kiro_crew.connections import get_provider, l0_probe

MCP_URL = "https://mcp.example.com/mcp"
RESOURCE_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
ISSUER = "https://auth.example.com"
AUTHORIZATION_URL = "https://auth.example.com/.well-known/oauth-authorization-server"


class FakeContent:
    def __init__(self, data):
        self.data = data
        self.offset = 0
        self.bytes_read = 0

    async def read(self, limit):
        chunk = self.data[self.offset : self.offset + limit]
        self.offset += len(chunk)
        self.bytes_read += len(chunk)
        return chunk


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, body=None):
        self.status = status
        self.headers = headers or {}
        if body is None:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.routes[(method, url)]

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)


@pytest.fixture(autouse=True)
def forbid_real_http_client(monkeypatch):
    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("unit tests must not construct a real HTTP client")

    monkeypatch.setattr(l0_probe.aiohttp, "ClientSession", fail_if_constructed)


def provider(dcr=True, pkce=True, authorization_server_origin=ISSUER):
    item = deepcopy(get_provider("notion"))
    assert item is not None
    item["name"] = "Example"
    item["slug"] = "example"
    item["mcp_url"] = MCP_URL
    item["l0_expectations"] = {
        "authorization_server_origin": authorization_server_origin,
        "dcr": dcr,
        "pkce": pkce,
    }
    return item


def routes(*, dcr=True, pkce=True, resource=None):
    authorization = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "code_challenge_methods_supported": ["S256"] if pkce else [],
    }
    if dcr:
        authorization["registration_endpoint"] = f"{ISSUER}/register"
    return {
        ("POST", MCP_URL): FakeResponse(
            401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{RESOURCE_URL}"'}
        ),
        ("GET", RESOURCE_URL): FakeResponse(
            200,
            resource
            or {"resource": MCP_URL, "authorization_servers": [ISSUER]},
        ),
        ("GET", AUTHORIZATION_URL): FakeResponse(200, authorization),
    }


@pytest.mark.asyncio
async def test_success_validates_discovery_and_binds_every_request_timeout():
    session = FakeSession(routes())

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=3.5)

    assert result["ok"] is True
    assert all(result["checks"].values())
    assert [call[:2] for call in session.calls] == [
        ("POST", MCP_URL),
        ("GET", RESOURCE_URL),
        ("GET", AUTHORIZATION_URL),
    ]
    assert all(call[2]["timeout"].total == 3.5 for call in session.calls)
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert all(call[2]["auto_decompress"] is False for call in session.calls)
    assert all(
        call[2]["headers"]["Accept-Encoding"] == "identity" for call in session.calls
    )


def test_real_http_client_guard_is_active():
    with pytest.raises(AssertionError, match="must not construct a real HTTP client"):
        l0_probe.aiohttp.ClientSession()


@pytest.mark.asyncio
async def test_off_origin_resource_metadata_is_rejected_without_fetching():
    off_origin_url = "https://internal.example/.well-known/oauth-protected-resource"
    session = FakeSession(
        {
            ("POST", MCP_URL): FakeResponse(
                401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{off_origin_url}"'
                },
            )
        }
    )

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1)

    assert result["ok"] is False
    assert result["errors"] == [
        "unauthenticated challenge: resource_metadata origin does not match the MCP endpoint"
    ]
    assert [call[:2] for call in session.calls] == [("POST", MCP_URL)]


@pytest.mark.asyncio
async def test_off_origin_authorization_server_is_rejected_without_fetching():
    session = FakeSession(
        routes(
            resource={
                "resource": MCP_URL,
                "authorization_servers": ["https://internal.example/oauth"],
            }
        )
    )

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1)

    assert result["ok"] is False
    assert "authorization server origin does not match registry expectation" in result[
        "errors"
    ][0]
    assert [call[:2] for call in session.calls] == [
        ("POST", MCP_URL),
        ("GET", RESOURCE_URL),
    ]


@pytest.mark.asyncio
async def test_off_origin_redirect_is_not_followed():
    configured_routes = routes()
    configured_routes[("GET", RESOURCE_URL)] = FakeResponse(
        302, headers={"Location": "https://internal.example/metadata"}
    )
    session = FakeSession(configured_routes)

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1)

    assert result["ok"] is False
    assert "returned HTTP 302, expected 200" in result["errors"][0]
    assert [call[:2] for call in session.calls] == [
        ("POST", MCP_URL),
        ("GET", RESOURCE_URL),
    ]


@pytest.mark.asyncio
async def test_oversized_metadata_stops_after_limit_sentinel_byte():
    configured_routes = routes()
    oversized = FakeResponse(200, body=b"{" + b" " * l0_probe._MAX_METADATA_BYTES)
    configured_routes[("GET", RESOURCE_URL)] = oversized
    session = FakeSession(configured_routes)

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1)

    assert result["ok"] is False
    assert f"exceeded the {l0_probe._MAX_METADATA_BYTES}-byte metadata limit" in result[
        "errors"
    ][0]
    assert oversized.content.bytes_read == l0_probe._MAX_METADATA_BYTES + 1
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_compressed_metadata_is_rejected_before_body_read():
    configured_routes = routes()
    compressed = FakeResponse(
        200,
        body=b"compressed bytes are never decoded",
        headers={"Content-Encoding": "gzip"},
    )
    configured_routes[("GET", RESOURCE_URL)] = compressed
    session = FakeSession(configured_routes)

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1)

    assert result["ok"] is False
    assert "unsupported Content-Encoding 'gzip'" in result["errors"][0]
    assert compressed.content.bytes_read == 0
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_absent_dcr_and_pkce_pass_when_registry_expects_absence():
    result = await l0_probe.probe_provider(
        FakeSession(routes(dcr=False, pkce=False)),
        provider(dcr=False, pkce=False),
        timeout_seconds=1,
    )

    assert result["ok"] is True
    assert result["checks"]["dcr_expectation"] is True
    assert result["checks"]["pkce_expectation"] is True


@pytest.mark.asyncio
async def test_registration_and_pkce_policy_flips_fail_conformance():
    result = await l0_probe.probe_provider(
        FakeSession(routes(dcr=False, pkce=False)), provider(), timeout_seconds=1
    )

    assert result["ok"] is False
    assert result["checks"]["authorization_server_metadata"] is True
    assert result["checks"]["dcr_expectation"] is False
    assert result["checks"]["pkce_expectation"] is False
    assert result["errors"] == [
        "DCR advertised=False, expected=True",
        "PKCE S256 advertised=False, expected=True",
    ]


@pytest.mark.asyncio
async def test_malformed_protected_resource_discovery_stops_authorization_probe():
    session = FakeSession(
        routes(resource={"resource": MCP_URL, "authorization_servers": []})
    )

    result = await l0_probe.probe_provider(session, provider(), timeout_seconds=1)

    assert result["ok"] is False
    assert result["checks"]["unauthenticated_challenge"] is True
    assert result["checks"]["protected_resource_metadata"] is False
    assert "authorization_servers must be a non-empty list" in result["errors"][0]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_probe_all_caps_provider_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def fake_probe(_session, item, *, timeout_seconds):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            "slug": item["slug"],
            "name": item["name"],
            "ok": True,
            "checks": {},
            "errors": [],
            "duration_ms": 1,
        }

    monkeypatch.setattr(l0_probe, "probe_provider", fake_probe)
    providers = [provider() for _ in range(6)]
    results = await l0_probe.probe_all(
        object(), providers, concurrency=2, timeout_seconds=1
    )

    assert len(results) == 6
    assert peak == 2


def test_main_writes_machine_report_and_returns_nonzero(tmp_path, monkeypatch):
    failed = {
        "slug": "example",
        "name": "Example",
        "ok": False,
        "checks": {},
        "errors": ["failed"],
        "duration_ms": 1,
    }
    report = l0_probe.build_report([failed])

    async def fake_run_probe(**_kwargs):
        return report

    monkeypatch.setattr(l0_probe, "run_probe", fake_run_probe)
    report_path = tmp_path / "report.json"

    assert l0_probe.main(["--report", str(report_path)]) == 1
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
