"""Read-only AgentCore Gateway catalog for Settings debug.

Control plane (lazy ``bedrock-agentcore-control``): GetGateway,
ListGatewayTargets, GetGatewayTarget, SynchronizeGatewayTargets.

Data plane: MCP ``tools/list`` on workload + IAM inbound goes through
the same localhost SigV4 proxy kiro-cli uses (``ensure_workload_proxy``),
not a direct signed POST to the Gateway hostname. Login without a user
JWT skips tools with a hint — this page cannot borrow a chat session.
Workload catalog also vends-and-discards a WAT so a wrong identity
name is visible; the token never appears in the snapshot and is never
a Gateway bearer.

``ListOauth2CredentialProviders`` is account-wide Identity directory
and is not on this surface.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from kiro_crew.platform.agentcore_aws import (
    extra_available,
    probe_workload_identity,
    resolved_gateway_url,
    resolved_posture,
    resolved_workload_name,
)

logger = logging.getLogger(__name__)

CONTROL_CLIENT = "bedrock-agentcore-control"
GATEWAY_HOST_MARKER = ".gateway.bedrock-agentcore."
SNAPSHOT_OK = "ok"
SNAPSHOT_NO_URL = "no_url"
SNAPSHOT_EXTRA_MISSING = "extra_missing"
SNAPSHOT_UNUSABLE_URL = "unusable_url"
SNAPSHOT_AWS_DENIED = "aws_denied"
SNAPSHOT_NOT_FOUND = "not_found"
SNAPSHOT_AWS_ERROR = "aws_error"
TOOLS_SKIP_LOGIN = "login_needs_sign_in"
TOOLS_SKIP_MISMATCH = "authorizer_mismatch"
TOOLS_SKIP_UNREACHABLE = "tools_unreachable"
TOOLS_SKIP_PROXY = "proxy_unavailable"
TOOLS_DENIED = "tools_denied"
TOOLS_VIA_PROXY = "proxy"
_LOCAL_MCP_HOSTS = frozenset({"127.0.0.1", "localhost"})
AUTHORIZER_IAM = "AWS_IAM"
AUTHORIZER_JWT = "CUSTOM_JWT"
GATEWAY_READY = "READY"
LISTING_DEFAULT = "DEFAULT"
LISTING_DYNAMIC = "DYNAMIC"
INVOKE_ARN_PREFIX = "kirocrew-"
# Live GetGatewayTarget omits targetType; the type lives one level under
# mcp/http. listingMode and schema fields share that object — never treat
# those as the type (first-key walk would label a Lambda target MCP).
_MCP_CONFIG_TYPES = {
    "lambda": "LAMBDA",
    "mcpserver": "MCP_SERVER",
    "openapischema": "OPEN_API_SCHEMA",
    "smithymodel": "SMITHY_MODEL",
    "apigateway": "API_GATEWAY",
}
_HTTP_CONFIG_TYPES = {
    "agentcoreruntime": "AGENTCORE_RUNTIME",
}
SYNC_NOT_SUPPORTED = "not_syncable"
TARGET_DETAIL_MAX = 40
LIST_PAGE_MAX = 50
LIST_PAGES_MAX = 4
TOOLS_LIST_MAX = 200
TOOLS_LIST_TIMEOUT_SECS = 20.0
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CLIENT_NAME = "kirocrew-inspect"
SYNC_TARGET_ID_MAX = 128
_PENDING_AUTH_STATUSES = frozenset(
    {
        "CREATE_PENDING_AUTH",
        "UPDATE_PENDING_AUTH",
        "SYNCHRONIZE_PENDING_AUTH",
    }
)
_SYNCABLE_STATUSES = frozenset(
    {
        "READY",
        "SYNCHRONIZE_UNSUCCESSFUL",
        "UPDATE_UNSUCCESSFUL",
        "SYNCHRONIZING",
    }
)
_FORBIDDEN_STATUS_KEYS = frozenset(
    {"token", "secret", "bearer", "authorization", "password", "jwt"}
)


def parse_gateway_ref(url: str) -> dict[str, str] | None:
    """Return ``{id, region, host}`` from a Gateway MCP URL, or None."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    marker_at = host.find(GATEWAY_HOST_MARKER)
    if marker_at <= 0:
        return None
    gateway_id = host[:marker_at]
    rest = host[marker_at + len(GATEWAY_HOST_MARKER) :]
    region = rest.split(".", 1)[0]
    if not gateway_id or not region:
        return None
    return {"id": gateway_id, "region": region, "host": host}


def inspect_snapshot(*, include_tools: bool = True) -> dict[str, Any]:
    """Live catalog + checks. Never raises; codes are the operator contract."""
    url = resolved_gateway_url()
    posture = resolved_posture()
    workload_name = resolved_workload_name()
    if not url:
        return _empty_snapshot(SNAPSHOT_NO_URL, posture=posture, workload_name=workload_name)
    if not extra_available():
        return _empty_snapshot(
            SNAPSHOT_EXTRA_MISSING, posture=posture, url=url, workload_name=workload_name
        )
    ref = parse_gateway_ref(url)
    if ref is None:
        return _empty_snapshot(
            SNAPSHOT_UNUSABLE_URL, posture=posture, url=url, workload_name=workload_name
        )

    client = _control_client(ref["region"])
    if client is None:
        return _empty_snapshot(
            SNAPSHOT_EXTRA_MISSING, posture=posture, url=url, workload_name=workload_name
        )

    try:
        raw_gateway = client.get_gateway(gatewayIdentifier=ref["id"])
    except Exception as exc:
        code = _classify_aws_error(exc)
        logger.warning("GetGateway failed (%s)", code, exc_info=True)
        return _empty_snapshot(
            code,
            posture=posture,
            url=url,
            gateway_id=ref["id"],
            workload_name=workload_name,
        )

    gateway = _gateway_view(raw_gateway, ref)
    targets, targets_error = _list_targets(client, ref["id"])
    tools = _empty_tools(TOOLS_SKIP_UNREACHABLE)
    if include_tools:
        tools = _list_tools(
            url=url,
            region=ref["region"],
            posture=posture,
            authorizer=str(gateway.get("authorizer_type") or ""),
        )
    checks = _build_checks(
        url=url,
        ref=ref,
        posture=posture,
        gateway=gateway,
        extra=True,
        tools=tools,
    )
    if include_tools:
        checks.append(_tools_check(tools))
    checks.append(_identity_check())
    return _scrub(
        {
            "code": SNAPSHOT_OK,
            "posture": posture or None,
            "workload_name": workload_name,
            "gateway_url": url,
            "gateway": gateway,
            "targets": targets,
            "targets_error": targets_error,
            "tools": tools,
            "checks": checks,
        }
    )


def synchronize_target(target_id: str) -> dict[str, Any]:
    """Ask the Gateway to refresh one DEFAULT target's cached catalog.

    Returns ``{code, target_id}``. Does not wait for SYNCHRONIZING to finish.
    """
    cleaned = (target_id or "").strip()
    if not cleaned or len(cleaned) > SYNC_TARGET_ID_MAX:
        return {"code": "invalid_target", "target_id": cleaned}
    url = resolved_gateway_url()
    if not url:
        return {"code": SNAPSHOT_NO_URL, "target_id": cleaned}
    if not extra_available():
        return {"code": SNAPSHOT_EXTRA_MISSING, "target_id": cleaned}
    ref = parse_gateway_ref(url)
    if ref is None:
        return {"code": SNAPSHOT_UNUSABLE_URL, "target_id": cleaned}
    client = _control_client(ref["region"])
    if client is None:
        return {"code": SNAPSHOT_EXTRA_MISSING, "target_id": cleaned}
    try:
        client.synchronize_gateway_targets(gatewayIdentifier=ref["id"], targetIdList=[cleaned])
    except Exception as exc:
        code = _classify_aws_error(exc)
        if "not supported for synchronization" in str(exc).lower():
            code = SYNC_NOT_SUPPORTED
        logger.warning("SynchronizeGatewayTargets failed (%s)", code, exc_info=True)
        return {"code": code, "target_id": cleaned}
    return {"code": "accepted", "target_id": cleaned}


def _empty_snapshot(
    code: str,
    *,
    posture: str = "",
    url: str = "",
    gateway_id: str = "",
    workload_name: str = "",
) -> dict[str, Any]:
    checks = [
        {"id": "url", "ok": bool(url), "detail": code if not url else "ok"},
        {"id": "extra", "ok": extra_available(), "detail": code},
        {"id": "reachable", "ok": False, "detail": code},
        {"id": "ready", "ok": False, "detail": code},
        {"id": "authorizer", "ok": False, "detail": code},
        {"id": "url_match", "ok": False, "detail": code},
        {"id": "invoke_scope", "ok": False, "detail": code},
        {"id": "tools", "ok": False, "detail": code},
        {"id": "identity", "ok": False, "detail": code},
    ]
    gateway: dict[str, Any] | None = None
    if gateway_id:
        gateway = {
            "id": gateway_id,
            "name": "",
            "status": "",
            "authorizer_type": "",
            "gateway_url": "",
            "status_reasons": [],
        }
    return _scrub(
        {
            "code": code,
            "posture": posture or None,
            "workload_name": workload_name,
            "gateway_url": url,
            "gateway": gateway,
            "targets": [],
            "targets_error": None if code == SNAPSHOT_NO_URL else code,
            "tools": _empty_tools(code),
            "checks": checks,
        }
    )


def _empty_tools(skip: str) -> dict[str, Any]:
    return {"reachable": False, "skip_reason": skip, "items": [], "via": None}


def _control_client(region: str) -> Any | None:
    try:
        import boto3
    except ImportError:
        return None
    kwargs: dict[str, str] = {}
    if region:
        kwargs["region_name"] = region
    return boto3.client(CONTROL_CLIENT, **kwargs)


def _classify_aws_error(exc: BaseException) -> str:
    name = type(exc).__name__
    code = ""
    raw = getattr(exc, "response", None)
    if isinstance(raw, dict):
        err = raw.get("Error")
        if isinstance(err, dict):
            code = str(err.get("Code") or "")
    blob = f"{name} {code} {exc}".lower()
    if "accessdenied" in blob or "unauthorized" in blob or "forbidden" in blob:
        return SNAPSHOT_AWS_DENIED
    if "resourcenotfound" in blob or "notfound" in blob:
        return SNAPSHOT_NOT_FOUND
    return SNAPSHOT_AWS_ERROR


def _gateway_view(raw: Any, ref: dict[str, str]) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    reasons = data.get("statusReasons") or data.get("status_reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    return {
        "id": str(data.get("gatewayId") or data.get("gatewayIdentifier") or ref["id"]),
        "name": str(data.get("name") or ""),
        "status": str(data.get("status") or ""),
        "authorizer_type": str(data.get("authorizerType") or data.get("authorizer_type") or ""),
        "gateway_url": str(data.get("gatewayUrl") or data.get("gateway_url") or ""),
        "status_reasons": [str(item) for item in reasons if item],
    }


def _list_targets(client: Any, gateway_id: str) -> tuple[list[dict[str, Any]], str | None]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    try:
        for _ in range(LIST_PAGES_MAX):
            kwargs: dict[str, Any] = {
                "gatewayIdentifier": gateway_id,
                "maxResults": LIST_PAGE_MAX,
            }
            if token:
                kwargs["nextToken"] = token
            resp = client.list_gateway_targets(**kwargs)
            raw_items = resp.get("items") if isinstance(resp, dict) else None
            if raw_items is None and isinstance(resp, dict):
                raw_items = resp.get("targets")
            if not isinstance(raw_items, list):
                raw_items = []
            for raw in raw_items:
                if isinstance(raw, dict):
                    items.append(_target_stub(raw))
            token = resp.get("nextToken") if isinstance(resp, dict) else None
            if not token:
                break
    except Exception as exc:
        code = _classify_aws_error(exc)
        logger.warning("ListGatewayTargets failed (%s)", code, exc_info=True)
        return items, code

    for stub in items[:TARGET_DETAIL_MAX]:
        target_id = str(stub.get("target_id") or "")
        if not target_id:
            continue
        try:
            detail = client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        except Exception:
            logger.debug("GetGatewayTarget failed for %s", target_id, exc_info=True)
            continue
        _merge_target_detail(stub, detail if isinstance(detail, dict) else {})
    return items, None


def _target_stub(raw: dict[str, Any]) -> dict[str, Any]:
    status = str(raw.get("status") or "")
    return {
        "target_id": str(raw.get("targetId") or raw.get("target_id") or ""),
        "name": str(raw.get("name") or ""),
        "target_type": _target_type(raw),
        "status": status,
        "listing_mode": "",
        "last_synchronized_at": _iso_time(
            raw.get("lastSynchronizedAt") or raw.get("last_synchronized_at")
        ),
        "pending_auth": status in _PENDING_AUTH_STATUSES,
        "authorization_url": None,
        "syncable": False,
        "status_reasons": [
            str(item)
            for item in (raw.get("statusReasons") or raw.get("status_reasons") or [])
            if item
        ],
    }


def _merge_target_detail(stub: dict[str, Any], detail: dict[str, Any]) -> None:
    inner = detail.get("target") if isinstance(detail.get("target"), dict) else detail
    if not isinstance(inner, dict):
        return
    status = str(inner.get("status") or stub["status"])
    stub["status"] = status
    stub["pending_auth"] = status in _PENDING_AUTH_STATUSES
    target_type = _target_type(inner)
    if target_type:
        stub["target_type"] = target_type
    mode = _walk_str(inner, "listingMode") or _walk_str(inner, "listing_mode")
    if mode:
        stub["listing_mode"] = mode.upper()
    synced = _iso_time(inner.get("lastSynchronizedAt") or inner.get("last_synchronized_at"))
    if synced:
        stub["last_synchronized_at"] = synced
    reasons = inner.get("statusReasons") or inner.get("status_reasons")
    if isinstance(reasons, list):
        stub["status_reasons"] = [str(item) for item in reasons if item]
    if _authorization_url(inner):
        # Consent URL allowlist is a later PR. Do not surface an IdP URL yet.
        stub["authorization_url"] = None
    stub["syncable"] = (
        stub["listing_mode"] == LISTING_DEFAULT
        and status in _SYNCABLE_STATUSES
        and stub["target_type"] in {"MCP_SERVER", "MCP", ""}
    )


def _target_type(raw: dict[str, Any]) -> str:
    direct = raw.get("targetType") or raw.get("target_type")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().upper()
    config = raw.get("targetConfiguration") or raw.get("target_configuration")
    return _infer_target_type_from_config(config)


def _infer_target_type_from_config(config: Any) -> str:
    """Map Create/GetGatewayTarget config shape to a catalog type.

    Control-plane list/get often omit ``targetType``. The live AWS_IAM
    Lambda shape is ``{mcp: {lambda: ...}}`` — walking the first key
    would report ``MCP``. Skip listingMode / schema siblings.
    """
    if not isinstance(config, dict) or not config:
        return ""
    mcp = config.get("mcp")
    if isinstance(mcp, dict):
        for candidate in mcp:
            mapped = _MCP_CONFIG_TYPES.get(str(candidate).replace("_", "").lower())
            if mapped:
                return mapped
    http = config.get("http")
    if isinstance(http, dict):
        for candidate in http:
            mapped = _HTTP_CONFIG_TYPES.get(str(candidate).replace("_", "").lower())
            if mapped:
                return mapped
        if http:
            return str(next(iter(http.keys()))).upper()
    top = next(iter(config.keys()))
    return str(top).upper() if top not in {"mcp", "http"} else ""


def _walk_str(data: Any, needle: str) -> str:
    if isinstance(data, dict):
        if needle in data and isinstance(data[needle], str) and data[needle].strip():
            return data[needle].strip()
        for value in data.values():
            found = _walk_str(value, needle)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _walk_str(value, needle)
            if found:
                return found
    return ""


def _authorization_url(data: Any) -> str | None:
    found = _walk_str(data, "authorizationUrl") or _walk_str(data, "authorization_url")
    if found.startswith("https://"):
        return found
    return None


def _iso_time(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return str(value.isoformat())
        except Exception:
            return ""
    text = str(value).strip()
    return text


def _invoke_scope(
    *,
    posture: str,
    gateway_id: str,
    tools: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Whether this crew can call this Gateway.

    Proved when tools/list just went through the SigV4 proxy
    (``InvokeGateway`` succeeded for this credential). Prefix is a
    fallback when tools were not proved. Login skips — JWT inbound,
    not IAM Invoke. A data-plane 401/403 is invoke-not even on a
    ``kirocrew-*`` id. This does not widen IAM: CFN instance + successor
    still grant Invoke only on ``gateway/kirocrew-*``.
    """
    if posture == "login":
        return True, "ok"
    payload = tools or {}
    if payload.get("skip_reason") == TOOLS_DENIED:
        return False, "invoke_denied"
    if bool(payload.get("reachable")) and payload.get("via") == TOOLS_VIA_PROXY:
        return True, "ok"
    if gateway_id.startswith(INVOKE_ARN_PREFIX):
        return True, "ok"
    return False, "not_kirocrew_prefixed"


def _build_checks(
    *,
    url: str,
    ref: dict[str, str],
    posture: str,
    gateway: dict[str, Any],
    extra: bool,
    tools: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    authorizer = str(gateway.get("authorizer_type") or "")
    status = str(gateway.get("status") or "")
    reported = str(gateway.get("gateway_url") or "")
    url_match = _urls_match(url, reported) if reported else True
    authorizer_ok = _authorizer_matches(posture, authorizer)
    invoke_ok, invoke_detail = _invoke_scope(posture=posture, gateway_id=ref["id"], tools=tools)
    return [
        {"id": "url", "ok": True, "detail": "ok"},
        {"id": "extra", "ok": extra, "detail": "ok" if extra else SNAPSHOT_EXTRA_MISSING},
        {"id": "reachable", "ok": True, "detail": "ok"},
        {
            "id": "ready",
            "ok": status == GATEWAY_READY,
            "detail": status or SNAPSHOT_AWS_ERROR,
        },
        {
            "id": "authorizer",
            "ok": authorizer_ok,
            "detail": authorizer or "unknown",
        },
        {
            "id": "url_match",
            "ok": url_match,
            "detail": "ok" if url_match else "mismatch",
        },
        {
            "id": "invoke_scope",
            "ok": invoke_ok,
            "detail": invoke_detail,
        },
    ]


def _authorizer_matches(posture: str, authorizer: str) -> bool:
    if posture == "workload":
        return authorizer == AUTHORIZER_IAM
    if posture == "login":
        return authorizer == AUTHORIZER_JWT
    return False


def _urls_match(configured: str, reported: str) -> bool:
    left = urlparse(configured)
    right = urlparse(reported)
    if not right.hostname:
        return True
    return (left.hostname or "").lower() == right.hostname.lower()


def _list_tools(
    *,
    url: str,
    region: str,
    posture: str,
    authorizer: str,
) -> dict[str, Any]:
    if posture == "login":
        return _empty_tools(TOOLS_SKIP_LOGIN)
    if authorizer and authorizer != AUTHORIZER_IAM:
        return _empty_tools(TOOLS_SKIP_MISMATCH)
    # Same localhost proxy kiro-cli uses. A direct SigV4 to the Gateway
    # hostname can stay green while that agent path is down.
    from kiro_crew.platform.agentcore_sigv4 import ensure_workload_proxy

    listen = ensure_workload_proxy(url)
    if not listen:
        return _empty_tools(TOOLS_SKIP_PROXY)
    try:
        session_id = _mcp_initialize(listen, region)
        items = _mcp_tools_list(listen, region, session_id=session_id)
    except _ToolsDenied:
        return _empty_tools(TOOLS_DENIED)
    except Exception:
        logger.warning("Gateway tools/list via SigV4 proxy failed", exc_info=True)
        return _empty_tools(TOOLS_SKIP_UNREACHABLE)
    return {
        "reachable": True,
        "skip_reason": None,
        "items": items[:TOOLS_LIST_MAX],
        "via": TOOLS_VIA_PROXY,
    }


def _tools_check(tools: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "tools",
        "ok": bool(tools.get("reachable")),
        "detail": tools.get("skip_reason") or "ok",
    }


def _identity_check() -> dict[str, Any]:
    """Workload WAT probe. Login skips. Token never enters the snapshot."""
    probed = probe_workload_identity()
    return {
        "id": "identity",
        "ok": bool(probed.get("ok")),
        "detail": str(probed.get("detail") or ""),
    }


class _ToolsDenied(Exception):
    """Data-plane 401/403 — inbound auth rejected the inspect call."""


def _mcp_initialize(url: str, region: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1"},
        },
    }
    headers, body = _mcp_post(url, region, payload)
    _parse_mcp_json(body)
    for key, value in headers.items():
        if key.lower() == "mcp-session-id" and value:
            return value
    return ""


def _mcp_tools_list(url: str, region: str, *, session_id: str) -> list[dict[str, str]]:
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    headers, body = _mcp_post(url, region, payload, session_id=session_id)
    del headers
    parsed = _parse_mcp_json(body)
    result = parsed.get("result") if isinstance(parsed, dict) else None
    raw_tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(raw_tools, list):
        return []
    items: list[dict[str, str]] = []
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        items.append(
            {
                "name": name,
                "description": str(raw.get("description") or "").strip(),
            }
        )
    return items


def _mcp_post(
    url: str,
    region: str,
    payload: dict[str, Any],
    *,
    session_id: str = "",
) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    del region  # proxy signs with the upstream Gateway region
    host = (urlparse(url).hostname or "").lower()
    if host not in _LOCAL_MCP_HOSTS:
        raise ValueError("inspect MCP post is localhost-only (SigV4 proxy)")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            request, timeout=TOOLS_LIST_TIMEOUT_SECS
        ) as resp:
            return dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise _ToolsDenied(str(exc.code)) from exc
        raise


def _parse_mcp_json(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk.startswith("{"):
                data = json.loads(chunk)
                if isinstance(data, dict):
                    return data
    return {}


def _scrub(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop accidental credential-shaped keys before they leave the module."""

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): walk(item)
                for key, item in value.items()
                if str(key).lower() not in _FORBIDDEN_STATUS_KEYS
            }
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(payload)
