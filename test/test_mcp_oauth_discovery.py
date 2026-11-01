"""Discovery-verified OAuth consent endpoints for operator-configured MCP servers.

A remote MCP server added straight to ``mcp.json`` whose authorization server is
outside the builtin and operator endpoint sets fails closed at the banner gate
unless discovery verifies it, because its PKCE ``code_challenge`` and ``state``
read as bare secrets. These tests pin ``kiro_crew.mcp_oauth_discovery``: the
endpoint is approved only when RFC 9728 / RFC 8414 discovery from the CONFIGURED
server URL advertises the exact ``(host, path)`` of the consent URL, and nothing
else about the gate moves.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest
from turn_harness import TurnScript, run_turn

from kiro_crew import link_unfurl
from kiro_crew import mcp_oauth_discovery as discovery
from kiro_crew import security
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_runner import _drain_session_init_oauth_requests
from kiro_crew.dashboard.chat_utils import _redact_meta_for_role
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.security import exfil
from kiro_crew.sel import SecurityEventLog
from kiro_crew.testing.clock import ManualClock

MCP_URL = "https://mcp.unlisted-idp.example/mcp"
ISSUER = "https://auth.unlisted-idp.example/"
AUTHORIZE = "https://auth.unlisted-idp.example/oauth/authorize"

# High-entropy values shaped like the ones kiro-cli mints: an S256 challenge is
# 43 base64url characters, state is a 43-character token_urlsafe(32).
_CHALLENGE = "Qm9xVf3kLr8sT1uWz0aYcD6eHgJ2nPq5RtXv7BnMk4E"
_STATE = "Zr8pW2kQ9xL4mN7vB1cT6yH3sD0fG5jK8aE2uRwXq9P"


def _consent_url(endpoint: str = AUTHORIZE, **extra: str) -> str:
    params = {
        "response_type": "code",
        "client_id": "mcp_7f3a9c2e41b84d0f9e6a5c3b2d1e0f4a",
        "redirect_uri": "http://127.0.0.1:49152/oauth/callback",
        "scope": "mcp:read",
        "state": _STATE,
        "code_challenge": _CHALLENGE,
        "code_challenge_method": "S256",
        "resource": MCP_URL,
    }
    params.update(extra)
    return f"{endpoint}?{urlencode(params)}"


def _documents(
    *,
    prm: bool = True,
    issuer: str = ISSUER,
    advertised_issuer: str | None = None,
    authorize: str = AUTHORIZE,
) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    if prm:
        docs["https://mcp.unlisted-idp.example/.well-known/oauth-protected-resource/mcp"] = {
            "resource": MCP_URL,
            "authorization_servers": [issuer],
        }
    base = issuer.rstrip("/")
    origin = base.split("/", 3)
    as_url = f"{origin[0]}//{origin[2]}/.well-known/oauth-authorization-server" + (
        "/" + origin[3] if len(origin) > 3 else ""
    )
    docs[as_url] = {
        "issuer": advertised_issuer or issuer,
        "authorization_endpoint": authorize,
        "token_endpoint": f"{base}/oauth/token",
    }
    return docs


_PUBLIC_IP = "93.184.216.34"


def _public_vet(url: str) -> link_unfurl.VettedUrl:
    return link_unfurl.vet_unfurl_url(url, resolve=lambda _host, _port: [_PUBLIC_IP])


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch):
    # A fresh discovered-endpoint set per test, restored by the floor fixture.
    _floor_monkeypatch.setattr(exfil, "_OAUTH_DISCOVERED_ENDPOINTS", {})
    _floor_monkeypatch.setattr(discovery, "_NEGATIVE", {})
    _floor_monkeypatch.setattr(exfil, "_load_operator_oauth_endpoints", lambda: frozenset())
    _floor_monkeypatch.setattr(exfil, "SecurityEventLog", MagicMock())


@pytest.fixture
def configured(monkeypatch):
    """Configure servers in the two trusted sources: (agent spec, settings registry)."""

    def _set(agent: dict | None = None, registry: dict | None = None) -> None:
        monkeypatch.setattr(discovery, "_servers_from_agent_spec", lambda: dict(agent or {}))
        monkeypatch.setattr(
            discovery, "_servers_from_settings_registry", lambda: dict(registry or {})
        )

    _set()
    return _set


@pytest.fixture
def served(monkeypatch):
    """Serve metadata documents by URL and record every fetch."""
    fetched: list[str] = []
    docs: dict[str, dict[str, Any]] = {}

    async def _get_json(_session, url, _timeout):
        fetched.append(url)
        if url not in docs:
            raise ValueError(f"{url} returned HTTP 404, expected 200")
        return docs[url]

    monkeypatch.setattr(discovery, "_get_json", _get_json)
    # The real vet, answered by a fixed public address instead of DNS.
    monkeypatch.setattr(discovery, "vet_unfurl_url", _public_vet)

    def _serve(new_docs: dict[str, dict[str, Any]]) -> list[str]:
        docs.clear()
        docs.update(new_docs)
        return fetched

    return _serve


def test_precondition_the_unlisted_endpoint_is_rejected_today():
    assert security.oauth_url_contains_credential(_consent_url())
    assert security.oauth_rejection_is_endpoint_exemptible(_consent_url())


@pytest.mark.asyncio
async def test_discovery_match_approves_the_endpoint(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents())
    assert await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    assert not security.oauth_url_contains_credential(_consent_url())
    exfil.SecurityEventLog.return_value.log.assert_called_once()
    event = exfil.SecurityEventLog.return_value.log.call_args.args[0]
    assert event.event_type == "oauth_endpoint_discovered"
    assert event.metadata["mcp_url"] == MCP_URL


@pytest.mark.asyncio
async def test_agent_spec_is_a_trusted_source(configured, served):
    configured(agent={"idp": {"url": MCP_URL}})
    served(_documents())
    assert await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())


@pytest.mark.asyncio
async def test_advertised_endpoint_on_another_path_is_not_approved(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents(authorize="https://auth.unlisted-idp.example/other/authorize"))
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    assert security.oauth_url_contains_credential(_consent_url())


@pytest.mark.asyncio
async def test_consent_url_on_a_host_the_server_does_not_advertise_is_refused(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents())
    evil = _consent_url("https://evil.example.com/oauth/authorize")
    assert not await discovery.approve_discovered_oauth_endpoint("idp", evil)
    assert security.oauth_url_contains_credential(evil)


@pytest.mark.asyncio
async def test_issuer_mismatch_in_server_metadata_is_refused(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents(advertised_issuer="https://auth.unlisted-idp.example/tenant-b"))
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())


@pytest.mark.asyncio
async def test_server_without_protected_resource_metadata_uses_its_origin(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    origin_authorize = "https://mcp.unlisted-idp.example/authorize"
    served(
        _documents(prm=False, issuer="https://mcp.unlisted-idp.example", authorize=origin_authorize)
    )
    assert await discovery.approve_discovered_oauth_endpoint("idp", _consent_url(origin_authorize))


@pytest.mark.asyncio
async def test_oidc_discovery_is_the_fallback(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    docs = _documents()
    as_url = "https://auth.unlisted-idp.example/.well-known/oauth-authorization-server"
    docs["https://auth.unlisted-idp.example/.well-known/openid-configuration"] = docs.pop(as_url)
    served(docs)
    assert await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())


@pytest.mark.asyncio
async def test_unconfigured_server_makes_no_request(configured, served):
    fetched = served(_documents())
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    assert fetched == []


@pytest.mark.asyncio
async def test_conflicting_configured_urls_make_no_request(configured, served):
    configured(
        agent={"idp": {"url": MCP_URL}},
        registry={"idp": {"url": "https://other.example/mcp"}},
    )
    fetched = served(_documents())
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    assert fetched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_url",
    ["http://mcp.unlisted-idp.example/mcp", "https://127.0.0.1/mcp", "https://localhost/mcp"],
)
async def test_unsafe_configured_url_makes_no_request(configured, served, configured_url):
    configured(registry={"idp": {"url": configured_url}})
    fetched = served(_documents())
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    assert fetched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        # Refused for reasons an approved endpoint does not relax.
        _consent_url() + "#frag",
        _consent_url(evil="AKIAIOSFODNN7EXAMPLE"),
        _consent_url("https://auth.unlisted-idp.example:8443/oauth/authorize"),
        _consent_url("http://auth.unlisted-idp.example/oauth/authorize"),
    ],
    ids=["fragment", "fixed-credential", "explicit-port", "http"],
)
async def test_urls_refused_for_other_reasons_stay_refused_with_no_request(configured, served, url):
    configured(registry={"idp": {"url": MCP_URL}})
    fetched = served(_documents())
    assert not await discovery.approve_discovered_oauth_endpoint("idp", url)
    assert fetched == []
    assert security.oauth_url_contains_credential(url)


@pytest.mark.asyncio
async def test_approved_endpoint_still_scans_unknown_parameters(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents())
    assert await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    smuggled = _consent_url(exfil="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert security.oauth_url_contains_credential(smuggled)


@pytest.mark.asyncio
async def test_discovery_error_leaves_the_gate_unchanged(configured, monkeypatch):
    configured(registry={"idp": {"url": MCP_URL}})

    async def _boom(_mcp_url):
        raise RuntimeError("network down")

    monkeypatch.setattr(discovery, "_discover", _boom)
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    assert security.oauth_url_contains_credential(_consent_url())


@pytest.mark.asyncio
async def test_already_accepted_url_makes_no_request(configured, served):
    configured(registry={"linear": {"url": "https://mcp.linear.app/mcp"}})
    fetched = served({})
    url = _consent_url("https://mcp.linear.app/authorize")
    assert not security.oauth_url_contains_credential(url)
    assert not await discovery.approve_discovered_oauth_endpoint("linear", url)
    assert fetched == []


@pytest.mark.asyncio
async def test_a_failed_verification_is_not_repeated_within_the_window(configured, served):
    """A slow or wrong provider costs one discovery per window, not one per banner."""
    configured(registry={"idp": {"url": MCP_URL}})
    fetched = served(_documents(authorize="https://auth.unlisted-idp.example/elsewhere"))
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    first = len(fetched)
    assert first > 0
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    assert len(fetched) == first


@pytest.mark.asyncio
async def test_a_failure_is_retried_after_the_window(configured, served, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch, discovery)
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents(authorize="https://auth.unlisted-idp.example/elsewhere"))
    assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
    served(_documents())
    clock.advance(discovery._NEGATIVE_TTL_SECONDS + 1)
    assert await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())


@pytest.mark.asyncio
async def test_failure_cache_keys_are_fixed_size(configured, served):
    """Huge server names or consent paths must not grow the capped cache's rows."""
    name = "s" * 100_000
    long_path = "/" + "p" * 6000
    configured(registry={name: {"url": MCP_URL}})
    served(_documents())
    url = _consent_url("https://auth.unlisted-idp.example" + long_path)
    assert not await discovery.approve_discovered_oauth_endpoint(name, url)
    assert discovery._NEGATIVE
    assert all(len(key) == 64 for key in discovery._NEGATIVE)


class TestTrustedConfigSources:
    def test_project_defaults_are_not_a_trust_source(self, tmp_path, monkeypatch):
        """A writable checkout's ``agents/defaults.json`` must never anchor trust."""
        from kiro_crew.config import paths

        project = tmp_path / "project"
        (project / "agents").mkdir(parents=True)
        (project / "agents" / "defaults.json").write_text(
            '{"mcpServers": {"idp": {"url": "https://attacker.example/mcp"}}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(project))
        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
        assert discovery._servers_from_agent_spec() == {}

    def test_installed_spec_is_read(self, tmp_path, monkeypatch):
        from kiro_crew.agent import AGENT_FILENAME
        from kiro_crew.config import paths

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / AGENT_FILENAME).write_text(
            '{"name": "kirocrew", "mcpServers": {"idp": {"url": "%s"}}}' % MCP_URL,
            encoding="utf-8",
        )
        monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
        assert discovery._servers_from_agent_spec()["idp"]["url"] == MCP_URL

    def test_settings_registry_follows_kiro_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kh"))
        assert (
            discovery._kiro_settings_mcp_json()
            == (tmp_path / "kh" / "settings" / "mcp.json").resolve()
        )


class TestFetchVetting:
    @pytest.mark.asyncio
    async def test_a_url_the_vet_refuses_is_never_fetched(self, configured, served, monkeypatch):
        configured(registry={"idp": {"url": MCP_URL}})
        fetched = served(_documents())

        def _refuse(_url):
            raise link_unfurl.UnfurlRejected("blocked_url")

        monkeypatch.setattr(discovery, "vet_unfurl_url", _refuse)
        assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
        assert fetched == []
        assert security.oauth_url_contains_credential(_consent_url())

    @pytest.mark.asyncio
    async def test_a_name_resolving_to_a_private_address_is_never_fetched(
        self, configured, served, monkeypatch
    ):
        configured(registry={"idp": {"url": MCP_URL}})
        fetched = served(_documents())
        monkeypatch.setattr(
            discovery,
            "vet_unfurl_url",
            lambda url: link_unfurl.vet_unfurl_url(
                url, resolve=lambda _h, _p: [_PUBLIC_IP, "169.254.169.254"]
            ),
        )
        assert not await discovery.approve_discovered_oauth_endpoint("idp", _consent_url())
        assert fetched == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://auth.unlisted-idp.example:80/.well-known/oauth-authorization-server",
            "http://auth.unlisted-idp.example/.well-known/oauth-authorization-server",
        ],
        ids=["https-port-80", "http"],
    )
    async def test_anything_but_https_on_443_is_never_fetched(self, served, url):
        fetched = served({url: {"issuer": ISSUER}})
        with pytest.raises(link_unfurl.UnfurlRejected):
            await discovery._fetch_json(url)
        assert fetched == []

    @pytest.mark.asyncio
    async def test_the_fetch_uses_the_vetted_url(self, served):
        url = "https://auth.unlisted-idp.example/.well-known/oauth-authorization-server"
        fetched = served({url: {"issuer": ISSUER}})
        assert await discovery._fetch_json(url) == {"issuer": ISSUER}
        assert fetched == [url]


def test_configured_url_query_is_never_audited(configured):
    configured(registry={"idp": {"url": MCP_URL + "?api_key=abc"}})
    assert discovery._mcp_endpoint(discovery.configured_mcp_url("idp")) == MCP_URL


class TestRecordDiscoveredEndpoint:
    @pytest.mark.parametrize(
        "host,path",
        [
            ("127.0.0.1", "/authorize"),
            ("localhost", "/authorize"),
            ("a.example", "/x?y"),
            ("a.example", "/../x"),
            ("a.example", "authorize"),
        ],
    )
    def test_shapes_the_operator_file_refuses_are_refused(self, host, path):
        assert not security.record_discovered_oauth_endpoint(host, path, mcp_url=MCP_URL)
        assert not exfil._OAUTH_DISCOVERED_ENDPOINTS

    def test_entries_expire(self, monkeypatch):
        clock = ManualClock()
        clock.install(monkeypatch, exfil)
        assert security.record_discovered_oauth_endpoint(
            "auth.unlisted-idp.example", "/oauth/authorize", mcp_url=MCP_URL
        )
        clock.advance(exfil._OAUTH_DISCOVERED_TTL_SECONDS - 1)
        assert not security.oauth_url_contains_credential(_consent_url())
        clock.advance(2)
        assert security.oauth_url_contains_credential(_consent_url())

    def test_set_is_capped_oldest_first(self):
        for i in range(exfil._OAUTH_DISCOVERED_CAP + 3):
            assert security.record_discovered_oauth_endpoint(
                f"idp{i}.example", "/authorize", mcp_url=MCP_URL
            )
        assert len(exfil._OAUTH_DISCOVERED_ENDPOINTS) == exfil._OAUTH_DISCOVERED_CAP
        assert ("idp0.example", "/authorize") not in exfil._OAUTH_DISCOVERED_ENDPOINTS

    def test_rows_retain_no_server_url_and_the_audit_is_bounded(self):
        long_url = "https://mcp.example/" + "a" * 5000
        security.record_discovered_oauth_endpoint(
            "auth.unlisted-idp.example", "/oauth/authorize", mcp_url=long_url
        )
        assert all(isinstance(v, float) for v in exfil._OAUTH_DISCOVERED_ENDPOINTS.values())
        event = exfil.SecurityEventLog.return_value.log.call_args.args[0]
        assert len(event.metadata["mcp_url"]) == exfil._OAUTH_DISCOVERED_MCP_URL_MAX_LEN

    def test_re_recording_audits_once(self, monkeypatch, sel_private_root):
        monkeypatch.setattr(exfil, "SecurityEventLog", SecurityEventLog)
        for _ in range(3):
            security.record_discovered_oauth_endpoint(
                "auth.unlisted-idp.example", "/oauth/authorize", mcp_url=MCP_URL
            )
        log = sel_private_root / "security_events.jsonl"
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1, rows
        assert "auth.unlisted-idp.example" in json.dumps(rows[0])


class _FakeAcpClient:
    def __init__(self, pending):
        self._pending = list(pending)

    def pop_pending_oauth_requests(self):
        out, self._pending = self._pending, []
        return out


class _FakeProviderClient:
    def __init__(self, pending):
        self.client = _FakeAcpClient(pending)


@pytest.mark.asyncio
async def test_session_init_banner_carries_a_discovered_consent_url(configured, served):
    """End to end: the drain verifies first, so the banner is live, not refused."""
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents())
    slot = _ChatSlot("s1")
    client = _FakeProviderClient([{"serverName": "idp", "oauthUrl": _consent_url()}])
    await _drain_session_init_oauth_requests(MagicMock(), slot, client)
    assert len(slot.messages) == 1
    meta = slot.messages[0]["meta"]
    assert meta.get("failed") is None
    assert meta["oauth_url"] == _consent_url()
    # Every later render re-gates the URL; it must survive that too.
    assert _redact_meta_for_role("mcp_oauth", dict(meta))["oauth_url"] == _consent_url()


async def _mid_turn_banner_meta() -> dict[str, Any]:
    """Run a turn whose stream carries a mid-session OAuth request; return its banner."""
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_MCP_OAUTH_REQUEST, server_name="idp", oauth_url=_consent_url()),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
    )
    banners = [row for row in record.window if row.get("role") == "mcp_oauth"]
    assert len(banners) == 1
    return dict(banners[0]["meta"])


@pytest.mark.asyncio
async def test_mid_turn_banner_for_a_discovered_endpoint_is_live(configured, served):
    """A token that expires mid-session reaches the banner through the turn's stream."""
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents())
    meta = await _mid_turn_banner_meta()
    assert meta.get("rejected_url") is None
    assert meta["oauth_url"] == _consent_url()


@pytest.mark.asyncio
async def test_mid_turn_banner_without_a_match_is_still_refused(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents(authorize="https://auth.unlisted-idp.example/elsewhere"))
    meta = await _mid_turn_banner_meta()
    assert meta["rejected_url"] is True


@pytest.mark.asyncio
async def test_session_init_banner_without_a_match_is_still_refused(configured, served):
    configured(registry={"idp": {"url": MCP_URL}})
    served(_documents(authorize="https://auth.unlisted-idp.example/elsewhere"))
    slot = _ChatSlot("s1")
    client = _FakeProviderClient([{"serverName": "idp", "oauthUrl": _consent_url()}])
    await _drain_session_init_oauth_requests(MagicMock(), slot, client)
    assert slot.messages[0]["meta"]["rejected_url"] is True
