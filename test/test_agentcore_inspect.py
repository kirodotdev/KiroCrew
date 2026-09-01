"""AgentCore Gateway catalog / verify / sync — mocked control plane."""

from __future__ import annotations

import inspect as pyinspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import kiro_crew
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


def test_snapshot_classifies_control_client_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(monkeypatch)

    def _boom(_region: str) -> None:
        raise inspect._ControlClientError(inspect.SNAPSHOT_AWS_ERROR)

    monkeypatch.setattr(inspect, "_control_client", _boom)
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_AWS_ERROR


def test_synchronize_classifies_control_client_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate(monkeypatch)

    def _boom(_region: str) -> None:
        raise inspect._ControlClientError(inspect.SNAPSHOT_AWS_ERROR)

    monkeypatch.setattr(inspect, "_control_client", _boom)
    result = inspect.synchronize_target("t1")
    assert result["code"] == inspect.SNAPSHOT_AWS_ERROR


def test_control_client_wraps_construction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom(Exception):
        pass

    fake = type(
        "Boto",
        (),
        {
            "client": staticmethod(
                lambda *_a, **_k: (_ for _ in ()).throw(Boom("profile not found"))
            )
        },
    )
    monkeypatch.setitem(sys.modules, "boto3", fake)
    with pytest.raises(inspect._ControlClientError) as caught:
        inspect._control_client("us-east-1")
    assert caught.value.code == inspect.SNAPSHOT_AWS_ERROR


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


def test_mcp_post_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        headers = {"Content-Type": "application/json"}

        def read(self, n: int = -1) -> bytes:
            assert n == inspect.TOOLS_LIST_MAX_BYTES + 1
            return b"x" * n

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(inspect, "loopback_urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.proxy_auth_headers",
        lambda _key: {"Authorization": "Bearer test"},
    )
    with pytest.raises(ValueError, match="exceeds size limit"):
        inspect._mcp_post(
            "http://127.0.0.1:18765/mcp",
            "us-west-2",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )


def test_mcp_post_accepts_body_at_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b'{"ok":true}'

    class _Resp:
        headers = {"Content-Type": "application/json"}

        def read(self, n: int = -1) -> bytes:
            assert n == inspect.TOOLS_LIST_MAX_BYTES + 1
            return payload

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(inspect, "loopback_urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.proxy_auth_headers",
        lambda _key: {"Authorization": "Bearer test"},
    )
    headers, body = inspect._mcp_post(
        "http://127.0.0.1:18765/mcp",
        "us-west-2",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert body == payload
    assert headers["Content-Type"] == "application/json"


def test_mcp_post_uses_loopback_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient HTTP_PROXY must not see X-Kirocrew-Proxy-* credentials."""
    seen: list[object] = []

    class _Resp:
        headers = {"Content-Type": "application/json"}

        def read(self, n: int = -1) -> bytes:
            return b'{"ok":true}'

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _open(req: object, timeout: float = 0) -> _Resp:
        del timeout
        seen.append(req)
        return _Resp()

    def _forbid_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("default urlopen")

    monkeypatch.setattr(inspect, "loopback_urlopen", _open)
    monkeypatch.setattr(inspect.urllib.request, "urlopen", _forbid_urlopen)
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.proxy_auth_headers",
        lambda _key: {"X-Kirocrew-Proxy-Token": "secret"},
    )
    inspect._mcp_post(
        "http://127.0.0.1:18765/mcp",
        "us-west-2",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert seen


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


def test_scrub_redacts_credential_strings() -> None:
    from kiro_crew.platform.agentcore_inspect import _scrub

    secret = "AKIAIOSFODNN7EXAMPLE"
    out = _scrub({"description": f"tool {secret} docs"})
    dumped = json.dumps(out)
    assert secret not in dumped


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
    monkeypatch.setattr(
        handler,
        "_refuse_disabled_capability",
        lambda request, operation: None,
    )


@pytest.mark.asyncio
async def test_handler_get_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _handler_isolate(monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_inspect.inspect_snapshot",
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
async def test_handler_refuses_when_capability_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_refuse_non_owner", lambda request, operation: None)

    class _Denied:
        permitted = False

    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda *a, **k: _Denied(),
    )
    resp = await handler.api_agentcore_gateway_get(_Req())
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "agentcore_disabled"


@pytest.mark.asyncio
async def test_handler_sync_requires_target_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _handler_isolate(monkeypatch)
    resp = await handler.api_agentcore_gateway_sync(_Req({}))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_target"


def test_dashboard_handlers_import_does_not_load_inspect(tmp_path: Path) -> None:
    """Gateway boot imports ``dashboard.handlers``; the catalog stays lazy."""
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("COV_CORE_SOURCE", None)
    env.pop("COVERAGE_PROCESS_START", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys\n"
            "import kiro_crew.dashboard.handlers  # noqa: F401\n"
            "print(json.dumps({\n"
            "  'inspect': 'kiro_crew.dashboard.handlers.agentcore_inspect' in sys.modules,\n"
            "  'platform': 'kiro_crew.platform.agentcore_inspect' in sys.modules,\n"
            "}))\n",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["inspect"] is False
    assert payload["platform"] is False


def test_system_routes_lazy_load_inspect() -> None:
    from kiro_crew.dashboard.routes import system as routes

    src = pyinspect.getsource(routes.register)
    assert "handlers.api_agentcore_gateway_get" not in src
    assert "_lazy_agentcore" in src


def test_inspect_handlers_offload_owner_gate_and_audit() -> None:
    """SEL first-use mkdirs; owner + audit must not run on the loop."""
    gate = pyinspect.getsource(handler._owner_gate)
    audit = pyinspect.getsource(handler._audit_async)
    assert "asyncio.to_thread(_refuse_non_owner" in gate
    assert "asyncio.to_thread(_refuse_disabled_capability" in gate
    assert "asyncio.to_thread(" in audit
    for fn in (
        handler.api_agentcore_gateway_get,
        handler.api_agentcore_gateway_verify,
        handler.api_agentcore_gateway_sync,
    ):
        src = pyinspect.getsource(fn)
        assert "await _owner_gate(" in src
        assert "await _audit_async(" in src
        assert "_refuse_non_owner(" not in src
        assert "_audit(" not in src.replace("_audit_async(", "")


def test_parse_gateway_ref_rejects_unusable_hosts() -> None:
    assert inspect.parse_gateway_ref("") is None
    assert inspect.parse_gateway_ref("http://gw.example/mcp") is None
    assert inspect.parse_gateway_ref("https://example.com/mcp") is None
    parsed = inspect.parse_gateway_ref(GW_URL)
    assert parsed is not None
    assert parsed["id"] == "demo-gw"
    assert parsed["region"] == "us-west-2"


def test_inspect_snapshot_unusable_and_missing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        inspect, "resolved_gateway_url", lambda: "https://not-a-gateway.example/mcp"
    )
    monkeypatch.setattr(inspect, "resolved_posture", lambda: "workload")
    monkeypatch.setattr(inspect, "resolved_workload_name", lambda: "crew")
    monkeypatch.setattr(inspect, "extra_available", lambda: True)
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_UNUSABLE_URL

    monkeypatch.setattr(inspect, "resolved_gateway_url", lambda: GW_URL)
    monkeypatch.setattr(inspect, "_control_client", lambda region: None)
    snap = inspect.inspect_snapshot()
    assert snap["code"] == inspect.SNAPSHOT_EXTRA_MISSING


def test_handler_audit_logs_and_swallows_sel_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []

    class _Sel:
        def log_api_access(self, **kwargs: object) -> None:
            logged.append(str(kwargs.get("operation")))

    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: _Sel())
    handler._audit(_Req(), operation=handler.OP_GET, outcome="success", resources="ok")
    assert logged == [handler.OP_GET]

    def _boom() -> None:
        raise RuntimeError("sel down")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", _boom)
    handler._audit(_Req(), operation=handler.OP_GET, outcome="denied", error="non_owner")


@pytest.mark.asyncio
async def test_handler_verify_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _handler_isolate(monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_inspect.inspect_snapshot",
        lambda include_tools=True: {"code": "ok", "targets": []},
    )
    resp = await handler.api_agentcore_gateway_verify(_Req())
    assert resp.status == 200
    assert json.loads(resp.text)["code"] == "ok"


@pytest.mark.asyncio
async def test_handler_non_owner_and_stale_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import web

    monkeypatch.setattr(handler, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda _req: False,
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.stale_owner_session_response",
        lambda _req: None,
    )
    resp = await handler.api_agentcore_gateway_get(_Req())
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "dashboard_owner_required"

    stale = web.json_response({"code": "stale_session_reauth"}, status=401)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.stale_owner_session_response",
        lambda _req: stale,
    )
    resp = await handler.api_agentcore_gateway_get(_Req())
    assert resp.status == 401
    assert json.loads(resp.text)["code"] == "stale_session_reauth"


@pytest.mark.asyncio
async def test_handler_refuses_when_governance_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_refuse_non_owner", lambda request, operation: None)

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("governance down")

    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        _boom,
    )
    resp = await handler.api_agentcore_gateway_get(_Req())
    assert resp.status == 403
    assert json.loads(resp.text)["code"] == "agentcore_disabled"


@pytest.mark.asyncio
async def test_handler_sync_rejects_bad_json_and_non_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _handler_isolate(monkeypatch)
    resp = await handler.api_agentcore_gateway_sync(_Req(ValueError("bad json")))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_json"

    resp = await handler.api_agentcore_gateway_sync(_Req(["not", "an", "object"]))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_handler_sync_maps_synchronize_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _handler_isolate(monkeypatch)
    codes = [
        ("accepted", 200),
        ("aws_denied", 403),
        ("not_found", 404),
        ("aws_error", 502),
        ("invalid_target", 400),
    ]
    for code, status in codes:
        monkeypatch.setattr(
            "kiro_crew.platform.agentcore_inspect.synchronize_target",
            lambda _tid, _code=code: {"code": _code},
        )
        resp = await handler.api_agentcore_gateway_sync(_Req({"target_id": " t1 "}))
        assert resp.status == status
        assert json.loads(resp.text)["code"] == code


def test_synchronize_target_maps_preconditions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inspect, "resolved_gateway_url", lambda: "")
    assert inspect.synchronize_target("t1")["code"] == inspect.SNAPSHOT_NO_URL

    monkeypatch.setattr(inspect, "resolved_gateway_url", lambda: GW_URL)
    monkeypatch.setattr(inspect, "extra_available", lambda: False)
    assert inspect.synchronize_target("t1")["code"] == inspect.SNAPSHOT_EXTRA_MISSING

    monkeypatch.setattr(inspect, "extra_available", lambda: True)
    monkeypatch.setattr(inspect, "resolved_gateway_url", lambda: "https://example.com/mcp")
    assert inspect.synchronize_target("t1")["code"] == inspect.SNAPSHOT_UNUSABLE_URL

    monkeypatch.setattr(inspect, "resolved_gateway_url", lambda: GW_URL)
    monkeypatch.setattr(inspect, "_control_client", lambda region: None)
    assert inspect.synchronize_target("t1")["code"] == inspect.SNAPSHOT_EXTRA_MISSING
