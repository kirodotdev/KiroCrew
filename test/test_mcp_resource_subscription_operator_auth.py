"""Who may act on a resource subscription it does not own (RFC Phase 4).

``rfc-mcp-resource-subscriptions.md`` §4.8. The internal secret is readable inside
the agent sandbox, so neither it nor a missing ``X-Session-Key`` says who is
calling. Pinned here:

* the operator route behind ``kirocrew resource-subs remove`` refuses a caller
  with no session header that the kernel peer check cannot verify as the host
  owner, and changes nothing;
* the CLI on the host passes that real check over a real socket, and prints the
  refusal's code when it does not;
* a session route refuses a session key the transport does not attest.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_mcp_resource_subscription_tools import (  # noqa: F401 -- fixtures ride along
    OTHER,
    URI,
    _AuditLog,
    _call,
    _clean_service,
    _offline_service,
    _patch_gateway_request,
    _record,
    _send_without_the_peer_proof,
    audit,
    recognized,
    switch,
)

from kiro_crew import cli_resource_subs
from kiro_crew.dashboard.handlers import resource_subscriptions as routes
from kiro_crew.mcp_gateway.resource_subscription_store import ResourceSubscriptionStore


@pytest.mark.asyncio
async def test_a_missing_session_header_is_not_operator_authority(
    tmp_path: Path, audit: _AuditLog  # noqa: F811 -- the imported fixture
) -> None:
    """A caller holding only the internal secret cannot remove another session's.

    The secret is readable inside the agent sandbox, so a sandboxed agent running
    ``kirocrew resource-subs remove`` arrives here with the secret and no session
    header. The mocked request has no kernel-identified peer, so the real owner
    check cannot verify it as the host owner, and the removal must be refused.
    """
    victim = "rsub_victim0000000"
    service = await _offline_service(tmp_path, _record(OTHER, (URI,), (victim,)))
    store = ResourceSubscriptionStore(tmp_path / "subs.json")

    got = await _call(
        routes.api_resource_subscriptions_operator,
        "POST",
        "/api/resource-subscriptions/operator",
        sk="",
        body={"subscription_id": victim},
    )

    assert got["_status"] == 403, got
    assert got["code"] == "operator_unverified"
    assert service.find_subscription(OTHER, victim) is not None
    assert store.load()[0].subscriptions[URI].id == victim
    assert audit.events[-1] == ("resource_subs_operator", "denied", "unverified_host_owner")


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch", "recognized")
async def test_an_unattested_session_key_cannot_remove_that_sessions_subscription(
    tmp_path: Path, audit: _AuditLog  # noqa: F811 -- the imported fixture
) -> None:
    """Naming another session's key is not being that session.

    The holder of the internal secret can read a key off a transcript and send it.
    Without the transport vouching for the key, the removal is refused and the
    subscription stays.
    """
    victim = "rsub_victim0000000"
    service = await _offline_service(tmp_path, _record(OTHER, (URI,), (victim,)))

    got = await _call(
        routes.api_resource_unsubscribe,
        "POST",
        "/api/resource-subscriptions/remove",
        sk=OTHER,
        attested=False,
        body={"subscription_id": victim},
    )

    assert (got["ok"], got["refused"]) == (False, "identity"), got
    assert service.find_subscription(OTHER, victim) is not None
    assert audit.events[-1] == ("resource_unsubscribe", "denied", "unattested_session")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="the namespace check is Linux's leg")
async def test_the_cli_on_the_host_passes_the_real_owner_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI's own request, over a real socket, is verified from the kernel peer.

    The peer is this test process, an ordinary host process in the gateway's own
    namespaces, so the real check admits it and the removal goes through.
    """

    @web.middleware
    async def internal(request: web.Request, handler: Any) -> web.StreamResponse:
        request["internal_auth"] = True
        return await handler(request)

    victim = "rsub_hostowner00000"
    service = await _offline_service(tmp_path, _record(OTHER, (URI,), (victim,)))
    app = web.Application(middlewares=[internal])
    app.router.add_post(
        "/api/resource-subscriptions/operator", routes.api_resource_subscriptions_operator
    )
    server = TestServer(app)
    monkeypatch.setattr("kiro_crew.config.loader.read_local_secret", lambda *_a: "")
    monkeypatch.setattr(
        "kiro_crew.port_resolution.resolve_client_port_ex", lambda _port: (server.port, None)
    )
    _send_without_the_peer_proof(monkeypatch)
    await server.start_server()
    try:
        code = await asyncio.to_thread(cli_resource_subs._remove, victim)
    finally:
        await server.close()

    assert code == 0, capsys.readouterr().err
    assert service.find_subscription(OTHER, victim) is None


def test_the_cli_names_the_owner_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(OTHER, (URI,), ("rsub_refused000000",)))
    monkeypatch.setattr(cli_resource_subs, "_store", lambda: store)
    body = json.dumps({"error": "not the host owner", "code": routes.OPERATOR_UNVERIFIED})
    refusal = urllib.error.HTTPError(
        "http://127.0.0.1:5476", 403, "Forbidden", {}, io.BytesIO(body.encode())  # type: ignore[arg-type]
    )
    _patch_gateway_request(monkeypatch, refusal)

    with pytest.raises(SystemExit) as stopped:
        cli_resource_subs._remove("rsub_refused000000")

    assert stopped.value.code == 1
    assert store.load()[0].subscriptions[URI].id == "rsub_refused000000"
    err = capsys.readouterr().err
    assert "HTTP 403 (operator_unverified): not the host owner" in err
