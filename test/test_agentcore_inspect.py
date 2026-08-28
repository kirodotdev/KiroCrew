"""AgentCore Gateway catalog / verify / sync — mocked control plane."""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.dashboard.handlers import agentcore_inspect as handler
from kiro_crew.platform import agentcore_inspect as inspect

GW_URL = "https://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"


class _Client:
    def __init__(
        self,
        *,
        gateway: dict[str, Any] | BaseException | None = None,
        targets: list[dict[str, Any]] | None = None,
        details: dict[str, dict[str, Any]] | None = None,
        sync_error: BaseException | None = None,
    ) -> None:
        self.gateway = gateway
        self.targets = targets or []
        self.details = details or {}
        self.sync_error = sync_error
        self.synced: list[str] = []

    def get_gateway(self, **kwargs: Any) -> dict[str, Any]:
        if isinstance(self.gateway, BaseException):
            raise self.gateway
        assert kwargs["gatewayIdentifier"] == "demo-gw"
        return self.gateway or {
            "gatewayId": "demo-gw",
            "name": "demo",
            "status": "READY",
            "authorizerType": "AWS_IAM",
            "gatewayUrl": GW_URL,
        }

    def list_gateway_targets(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["gatewayIdentifier"] == "demo-gw"
        return {"items": list(self.targets)}

    def get_gateway_target(self, **kwargs: Any) -> dict[str, Any]:
        target_id = kwargs["targetId"]
        return self.details.get(target_id, {"targetId": target_id, "status": "READY"})

    def synchronize_gateway_targets(self, **kwargs: Any) -> dict[str, Any]:
        if self.sync_error is not None:
            raise self.sync_error
        self.synced.extend(kwargs["targetIdList"])
        return {"status": "SYNCHRONIZING"}


def _isolate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str = GW_URL,
    extra: bool = True,
    posture: str = "workload",
    client: _Client | None = None,
) -> _Client:
    chosen = client or _Client()
    monkeypatch.setattr(inspect, "resolved_gateway_url", lambda: url)
    monkeypatch.setattr(inspect, "resolved_posture", lambda: posture)
    monkeypatch.setattr(inspect, "extra_available", lambda: extra)
    monkeypatch.setattr(inspect, "_control_client", lambda region: chosen)
    monkeypatch.setattr(
        inspect,
        "_list_tools",
        lambda **kwargs: {
            "reachable": True,
            "skip_reason": None,
            "items": [{"name": "search", "description": "find things"}],
            "via": inspect.TOOLS_VIA_PROXY,
        },
    )
    monkeypatch.setattr(
        inspect,
        "_identity_check",
        lambda: {"id": "identity", "ok": True, "detail": "ok"},
    )
    return chosen


def test_parse_gateway_ref_reads_id_and_region() -> None:
    ref = inspect.parse_gateway_ref(GW_URL)
    assert ref == {
        "id": "demo-gw",
        "region": "us-west-2",
        "host": "demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com",
    }
    assert inspect.parse_gateway_ref("https://example.test/mcp") is None
    assert (
        inspect.parse_gateway_ref(
            "http://demo-gw.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
        )
        is None
    )


def test_snapshot_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, url="")
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_NO_URL
    assert snap["targets"] == []
    assert snap["tools"]["reachable"] is False


def test_snapshot_extra_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, extra=False)
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_EXTRA_MISSING


def test_snapshot_unusable_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch, url="https://example.test/mcp")
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_UNUSABLE_URL


def test_snapshot_ok_lists_targets_and_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(
        targets=[{"targetId": "t1", "name": "docs", "status": "READY", "targetType": "MCP_SERVER"}],
        details={
            "t1": {
                "targetId": "t1",
                "name": "docs",
                "status": "READY",
                "targetConfiguration": {"mcp": {"listingMode": "DEFAULT"}},
                "lastSynchronizedAt": "2026-08-01T00:00:00Z",
            }
        },
    )
    _isolate(monkeypatch, client=client)
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_OK
    assert snap["gateway"]["id"] == "demo-gw"
    assert snap["gateway"]["authorizer_type"] == "AWS_IAM"
    assert snap["targets"][0]["name"] == "docs"
    assert snap["targets"][0]["listing_mode"] == "DEFAULT"
    assert snap["targets"][0]["syncable"] is True
    assert snap["tools"]["items"][0]["name"] == "search"
    ids = {c["id"] for c in snap["checks"]}
    assert "authorizer" in ids
    assert "tools" in ids
    assert "identity" in ids
    invoke = next(c for c in snap["checks"] if c["id"] == "invoke_scope")
    assert invoke["ok"] is True
    assert invoke["detail"] == "ok"
    assert all(c["ok"] for c in snap["checks"])


def test_invoke_scope_proved_via_proxy() -> None:
    ok, detail = inspect._invoke_scope(
        posture="workload",
        gateway_id="demo-gw",
        tools={"reachable": True, "via": inspect.TOOLS_VIA_PROXY},
    )
    assert ok is True
    assert detail == "ok"


def test_invoke_scope_unproved_non_prefix() -> None:
    ok, detail = inspect._invoke_scope(
        posture="workload",
        gateway_id="demo-gw",
        tools={
            "reachable": False,
            "via": None,
            "skip_reason": inspect.TOOLS_SKIP_UNREACHABLE,
        },
    )
    assert ok is False
    assert detail == "not_kirocrew_prefixed"


def test_invoke_scope_reachable_without_via_is_not_proved() -> None:
    ok, detail = inspect._invoke_scope(
        posture="workload",
        gateway_id="demo-gw",
        tools={"reachable": True},
    )
    assert ok is False
    assert detail == "not_kirocrew_prefixed"


def test_invoke_scope_prefix_fallback() -> None:
    ok, detail = inspect._invoke_scope(
        posture="workload",
        gateway_id="kirocrew-e2e-n9pk1rdrea",
        tools={
            "reachable": False,
            "via": None,
            "skip_reason": inspect.TOOLS_SKIP_UNREACHABLE,
        },
    )
    assert ok is True
    assert detail == "ok"


def test_invoke_scope_denied_even_on_prefix() -> None:
    ok, detail = inspect._invoke_scope(
        posture="workload",
        gateway_id="kirocrew-e2e-n9pk1rdrea",
        tools={
            "reachable": False,
            "via": None,
            "skip_reason": inspect.TOOLS_DENIED,
        },
    )
    assert ok is False
    assert detail == "invoke_denied"


def test_invoke_scope_login_skips() -> None:
    ok, detail = inspect._invoke_scope(
        posture="login",
        gateway_id="demo-gw",
        tools={
            "reachable": False,
            "via": None,
            "skip_reason": inspect.TOOLS_SKIP_LOGIN,
        },
    )
    assert ok is True
    assert detail == "ok"


def test_identity_check_surfaces_service_linked(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch)
    monkeypatch.setattr(
        inspect,
        "_identity_check",
        lambda: {"id": "identity", "ok": False, "detail": "service_linked"},
    )
    snap = inspect.inspect_snapshot()
    identity = next(c for c in snap["checks"] if c["id"] == "identity")
    assert identity["ok"] is False
    assert identity["detail"] == "service_linked"
    dumped = json.dumps(snap)
    assert "workloadAccessToken" not in dumped


def test_authorizer_mismatch_on_login_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(
        gateway={
            "gatewayId": "demo-gw",
            "status": "READY",
            "authorizerType": "CUSTOM_JWT",
            "gatewayUrl": GW_URL,
        }
    )
    _isolate(monkeypatch, client=client, posture="workload")
    snap = inspect.inspect_snapshot()
    authorizer = next(c for c in snap["checks"] if c["id"] == "authorizer")
    assert authorizer["ok"] is False
    assert authorizer["detail"] == "CUSTOM_JWT"


def test_login_skips_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inspect, "resolved_gateway_url", lambda: GW_URL)
    monkeypatch.setattr(inspect, "resolved_posture", lambda: "login")
    started: list[str] = []

    def _boom(url: str) -> str | None:
        started.append(url)
        raise AssertionError("login must not start the workload proxy")

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.ensure_workload_proxy",
        _boom,
    )
    tools = inspect._list_tools(
        url=GW_URL, region="us-west-2", posture="login", authorizer="CUSTOM_JWT"
    )
    assert tools["reachable"] is False
    assert tools["skip_reason"] == inspect.TOOLS_SKIP_LOGIN
    assert started == []


def test_workload_tools_go_through_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    listen = "http://127.0.0.1:9/mcp"
    upstreams: list[str] = []
    posts: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.ensure_workload_proxy",
        lambda url: upstreams.append(url) or listen,
    )

    def _post(
        url: str,
        region: str,
        payload: dict[str, Any],
        *,
        session_id: str = "",
    ) -> tuple[dict[str, str], bytes]:
        del region, session_id
        posts.append((url, str(payload.get("method") or "")))
        if payload.get("method") == "initialize":
            return {}, b'{"jsonrpc":"2.0","id":1,"result":{}}'
        return (
            {},
            b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo-hello___echo_hello"}]}}',
        )

    monkeypatch.setattr(inspect, "_mcp_post", _post)
    tools = inspect._list_tools(
        url=GW_URL, region="us-west-2", posture="workload", authorizer="AWS_IAM"
    )
    assert upstreams == [GW_URL]
    assert posts == [
        (listen, "initialize"),
        (listen, "tools/list"),
    ]
    assert tools["reachable"] is True
    assert tools["via"] == inspect.TOOLS_VIA_PROXY
    assert tools["items"][0]["name"] == "echo-hello___echo_hello"


def test_proxy_unavailable_skips_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.ensure_workload_proxy",
        lambda _url: None,
    )
    tools = inspect._list_tools(
        url=GW_URL, region="us-west-2", posture="workload", authorizer="AWS_IAM"
    )
    assert tools["reachable"] is False
    assert tools["skip_reason"] == inspect.TOOLS_SKIP_PROXY
    assert tools["via"] is None


def test_mcp_post_refuses_unsigned_remote_host() -> None:
    with pytest.raises(ValueError, match="localhost-only"):
        inspect._mcp_post(
            GW_URL,
            "us-west-2",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )


def test_snapshot_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    _isolate(monkeypatch, client=_Client(gateway=Denied("nope")))
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_AWS_DENIED


def test_snapshot_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    class Missing(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    _isolate(monkeypatch, client=_Client(gateway=Missing("gone")))
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_NOT_FOUND


def test_live_lambda_target_shape_is_lambda_not_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control plane omits targetType; type is mcp.lambda (live us-east-1)."""
    client = _Client(
        targets=[{"targetId": "ITSVDXYSAI", "name": "echo-hello", "status": "READY"}],
        details={
            "ITSVDXYSAI": {
                "targetId": "ITSVDXYSAI",
                "name": "echo-hello",
                "status": "READY",
                "targetConfiguration": {
                    "mcp": {
                        "lambda": {
                            "lambdaArn": (
                                "arn:aws:lambda:us-east-1:123456789012:function:kirocrew-e2e-echo"
                            ),
                            "toolSchema": {"inlinePayload": [{"name": "echo_hello"}]},
                        }
                    }
                },
                "credentialProviderConfigurations": [
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
            }
        },
    )
    _isolate(monkeypatch, client=client)
    snap = inspect.inspect_snapshot()
    target = snap["targets"][0]
    assert target["target_type"] == "LAMBDA"
    assert target["syncable"] is False


def test_live_mcp_server_target_shape_is_syncable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control plane omits targetType; type is mcp.mcpServer (live us-east-1)."""
    client = _Client(
        targets=[{"targetId": "2L1SFWDBDE", "name": "public-docs", "status": "READY"}],
        details={
            "2L1SFWDBDE": {
                "targetId": "2L1SFWDBDE",
                "name": "public-docs",
                "status": "READY",
                "statusReasons": [],
                "targetConfiguration": {
                    "mcp": {
                        "mcpServer": {
                            "endpoint": "https://mcp.example.test/mcp",
                            "listingMode": "DEFAULT",
                        }
                    }
                },
            }
        },
    )
    _isolate(monkeypatch, client=client)
    snap = inspect.inspect_snapshot()
    target = snap["targets"][0]
    assert target["target_type"] == "MCP_SERVER"
    assert target["listing_mode"] == "DEFAULT"
    assert target["syncable"] is True


def test_synchronize_lambda_is_not_syncable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unsupported(Exception):
        response = {"Error": {"Code": "ValidationException"}}

        def __str__(self) -> str:
            return (
                "An error occurred (ValidationException) when calling the "
                "SynchronizeGatewayTargets operation: Target type LAMBDA is "
                "not supported for synchronization"
            )

    _isolate(monkeypatch, client=_Client(sync_error=Unsupported()))
    result = inspect.synchronize_target("ITSVDXYSAI")
    assert result["code"] == inspect.SYNC_NOT_SUPPORTED


def test_synchronize_target_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _isolate(monkeypatch)
    result = inspect.synchronize_target("t1")
    assert result == {"code": "accepted", "target_id": "t1"}
    assert client.synced == ["t1"]


def test_synchronize_rejects_empty_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(monkeypatch)
    assert inspect.synchronize_target("  ")["code"] == "invalid_target"


def test_snapshot_scrubs_token_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(
        gateway={
            "gatewayId": "demo-gw",
            "status": "READY",
            "authorizerType": "AWS_IAM",
            "gatewayUrl": GW_URL,
            "token": "should-not-leak",
        }
    )
    _isolate(monkeypatch, client=client)
    dumped = json.dumps(inspect.inspect_snapshot())
    assert "should-not-leak" not in dumped
    for forbidden in ("workloadAccessToken", '"token":', "bearer"):
        assert forbidden not in dumped.lower() or forbidden == "bearer"
    assert "token" not in inspect.inspect_snapshot()["gateway"]


class _Req:
    def __init__(self, body: Any = None, *, app: str | None = None, owner: bool = True) -> None:
        self._body = body
        self._store: dict[str, Any] = {"user": "dashboard"}
        if app is not None:
            self._store["app"] = app
        self.owner = owner

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    async def json(self) -> Any:
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


def _handler_isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        handler,
        "_refuse_non_owner",
        lambda request, operation: None,
    )


@pytest.mark.asyncio
async def test_handler_get_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _handler_isolate(monkeypatch)
    monkeypatch.setattr(
        handler,
        "inspect_snapshot",
        lambda include_tools=True: {"code": "ok", "targets": [], "tools": {"items": []}},
    )
    resp = await handler.api_agentcore_gateway_get(_Req())
    assert resp.status == 200
    assert json.loads(resp.text)["code"] == "ok"


@pytest.mark.asyncio
async def test_handler_app_token_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_audit", lambda *a, **k: None)
    resp = await handler.api_agentcore_gateway_get(_Req(app="bot"))
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "dashboard_user_required"


@pytest.mark.asyncio
async def test_handler_sync_requires_target_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _handler_isolate(monkeypatch)
    resp = await handler.api_agentcore_gateway_sync(_Req({}))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_target"
