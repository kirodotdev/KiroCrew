"""The agent-facing ``app_request`` tool and its bounded transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from app_agent_routes_helpers import (  # noqa: F401 - autouse fixture registered by import
    _isolated_token_state,
    _manifest,
)

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.constants import APP_REQUEST_HEADER, APP_REQUEST_HEADER_VALUE
from kiro_crew.mcp_tools import apps as apps_tools
from kiro_crew.validation import (
    _APP_REQUEST_APP_RE,
    _APP_REQUEST_PATH_RE,
    APP_REQUEST_SCHEMA,
    ValidationError,
    validate_tool_args,
)

_REAL_MCP_POST = apps_tools.mcp_core._post


# ---------------------------------------------------------------------------
# app_request tool
# ---------------------------------------------------------------------------


@pytest.fixture
def declared_app(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = AppManifest.from_dict(
        _manifest(agentRoutes=["GET /subscriptions", "DELETE /subscriptions/{id}"])
    )
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda name: manifest if name == "slack-poller" else None,
    )
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "slack-poller", "enabled": True},
    )


def _write_advertisement_app(name: str, *, enabled: bool, agent_routes: list[str]) -> Path:
    import json

    from kiro_crew.apps import manager as apps_manager

    root = apps_manager.app_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    (root / apps_manager.INSTALLED_META_FILENAME).write_text(
        json.dumps({"name": name, "enabled": enabled}), encoding="utf-8"
    )
    (root / apps_manager.APP_MANIFEST_FILENAME).write_text(
        json.dumps(_manifest(name=name, agentRoutes=agent_routes)), encoding="utf-8"
    )
    return root


def test_app_request_advertisement_is_hidden_with_no_apps() -> None:
    from kiro_crew.apps import manager as apps_manager

    apps_manager.apps_dir().mkdir(parents=True, exist_ok=True)
    (apps_manager.apps_dir() / "README").write_text("not an app", encoding="utf-8")
    (apps_manager.apps_dir() / "cache").mkdir()

    assert "app_request" not in {spec["name"] for spec in apps_tools.advertised()}


@pytest.mark.parametrize(("enabled", "listed"), [(True, True), (False, False)])
def test_app_request_advertisement_follows_enabled_state(enabled: bool, listed: bool) -> None:
    _write_advertisement_app("slack-poller", enabled=enabled, agent_routes=["GET /status"])

    assert ("app_request" in {spec["name"] for spec in apps_tools.advertised()}) is listed


@pytest.mark.parametrize(("state", "listed"), [(False, False), (None, True), (True, True)])
def test_app_request_scan_takes_enablement_from_screened_installed_json(
    monkeypatch: pytest.MonkeyPatch, state: bool | None, listed: bool
) -> None:
    from kiro_crew.apps import manager as apps_manager

    _write_advertisement_app("slack-poller", enabled=True, agent_routes=["GET /status"])
    real_owner_json = apps_manager._owner_json

    def _owner_json(path: Path) -> dict[str, Any] | None:
        if path.name == apps_manager.INSTALLED_META_FILENAME:
            return None if state is None else {"name": "slack-poller", "enabled": state}
        return real_owner_json(path)

    def _unscreened_enablement(_name: str) -> bool | None:
        raise AssertionError("the agent-routes scan must use the screened reader")

    monkeypatch.setattr(apps_manager, "_owner_json", _owner_json)
    monkeypatch.setattr(apps_tools, "app_enabled_state", _unscreened_enablement)

    assert apps_tools._some_enabled_app_may_declare_agent_routes() is listed


def test_app_request_scan_never_opens_a_refused_enablement_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import manager as apps_manager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = _write_advertisement_app("slack-poller", enabled=True, agent_routes=["GET /status"])
    protected_target = tmp_path / ".aws" / ".env"
    protected_target.parent.mkdir()
    protected_target.write_text('{"name":"slack-poller","enabled":false}', encoding="utf-8")
    installed_path = root / apps_manager.INSTALLED_META_FILENAME
    installed_path.unlink()
    installed_path.symlink_to(protected_target)
    assert apps_manager._screened_spec_path(installed_path) is None

    unscreened_reads: list[str] = []

    def _unscreened_enablement(name: str) -> bool | None:
        unscreened_reads.append(name)
        raise AssertionError("the refused enablement target must not be opened")

    monkeypatch.setattr(apps_tools, "app_enabled_state", _unscreened_enablement)

    assert apps_tools._some_enabled_app_may_declare_agent_routes() is True
    assert unscreened_reads == []


def test_app_request_advertisement_never_opens_a_refused_manifest_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import manager as apps_manager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = _write_advertisement_app("slack-poller", enabled=True, agent_routes=["GET /status"])
    protected_target = tmp_path / ".aws" / ".env"
    protected_target.parent.mkdir()
    protected_target.write_text('{"privateKey":"synthetic"}', encoding="utf-8")
    manifest_path = root / apps_manager.APP_MANIFEST_FILENAME
    manifest_path.unlink()
    manifest_path.symlink_to(protected_target)
    assert apps_manager._screened_spec_path(manifest_path) is None

    opened: list[Path] = []

    def _record_open(_cls: type[AppManifest], path: Path) -> AppManifest:
        opened.append(path)
        raise ValueError("refused manifest must not be opened")

    monkeypatch.setattr(AppManifest, "from_json_file", classmethod(_record_open))

    listed = {spec["name"] for spec in apps_tools.advertised()}
    assert opened == []
    assert "app_request" in listed


def _identified(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "require_strict_session_key",
        lambda refusal, server="kirocrew-core": (key, "") if key else ("", refusal),
    )


def _forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> dict:
        raise AssertionError("no HTTP call expected")

    for verb in ("_get", "_post", "_put", "_patch", "_delete"):
        monkeypatch.setattr(apps_tools.mcp_core, verb, _boom)


def test_app_request_refuses_a_screened_enablement_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import manager as apps_manager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = _write_advertisement_app("slack-poller", enabled=True, agent_routes=["GET /status"])
    protected_target = tmp_path / ".aws" / ".env"
    protected_target.parent.mkdir()
    protected_target.write_text('{"name":"slack-poller","enabled":true}', encoding="utf-8")
    installed_path = root / apps_manager.INSTALLED_META_FILENAME
    installed_path.unlink()
    installed_path.symlink_to(protected_target)
    assert apps_manager._screened_spec_path(installed_path) is None

    unscreened_read = MagicMock(side_effect=AssertionError("unscreened read must not run"))
    unscreened_enabled = MagicMock(side_effect=AssertionError("unscreened enablement must not run"))
    monkeypatch.setattr(apps_manager, "_read_installed", unscreened_read)
    monkeypatch.setattr(apps_manager, "is_app_enabled", unscreened_enabled)
    _identified(monkeypatch, "dashboard:chat-1")
    _forbid_http(monkeypatch)

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/status"}
    )

    assert out.startswith("Error:") and "not an enabled installed app" in out
    unscreened_read.assert_not_called()
    unscreened_enabled.assert_not_called()


def test_app_request_accepts_a_screened_enabled_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.apps import manager as apps_manager

    _write_advertisement_app("slack-poller", enabled=True, agent_routes=["GET /status"])
    unscreened_read = MagicMock(side_effect=AssertionError("unscreened read must not run"))
    unscreened_enabled = MagicMock(side_effect=AssertionError("unscreened enablement must not run"))
    monkeypatch.setattr(apps_manager, "_read_installed", unscreened_read)
    monkeypatch.setattr(apps_manager, "is_app_enabled", unscreened_enabled)
    _identified(monkeypatch, "dashboard:chat-1")
    get = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(apps_tools.mcp_core, "_get", get)

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/status"}
    )

    assert out.startswith("OK GET /status")
    get.assert_called_once()
    unscreened_read.assert_not_called()
    unscreened_enabled.assert_not_called()


_APP_REQUEST_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.mark.parametrize("app_name", ["..", "a/b", "../slack-poller"])
def test_app_request_refuses_an_app_name_outside_the_apps_dir(
    monkeypatch: pytest.MonkeyPatch, app_name: str
) -> None:
    owner_json = MagicMock(return_value={"name": "slack-poller", "enabled": True})
    monkeypatch.setattr("kiro_crew.apps.manager._owner_json", owner_json)
    _identified(monkeypatch, "dashboard:chat-1")
    _forbid_http(monkeypatch)

    out = apps_tools.app_request(
        "app_request", {"app": app_name, "method": "GET", "path": "/status"}
    )

    assert out.startswith("Error:") and "not an enabled installed app" in out
    owner_json.assert_not_called()


def test_app_request_redacts_a_credential_path_on_an_undeclared_route(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    _forbid_http(monkeypatch)
    path = f"/missing/{_APP_REQUEST_TOKEN}"

    out = apps_tools.app_request(
        "app_request", {"app": "missing-app", "method": "GET", "path": path}
    )

    assert out.startswith("Error:") and "credential material" in out
    assert _APP_REQUEST_TOKEN not in out


def test_app_request_refuses_a_declared_credential_path_before_transport(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    _forbid_http(monkeypatch)
    path = f"/subscriptions/{_APP_REQUEST_TOKEN}"

    out = apps_tools.app_request(
        "app_request",
        {"app": "slack-poller", "method": "DELETE", "path": path},
    )

    assert out.startswith("Error:") and "did not reach the app or gateway" in out
    assert _APP_REQUEST_TOKEN not in out


@pytest.mark.parametrize(
    "response",
    [
        {"token": _APP_REQUEST_TOKEN},
        {"error": _APP_REQUEST_TOKEN},
        {"error": _APP_REQUEST_TOKEN, "refused": True},
        {"error": _APP_REQUEST_TOKEN, "transport_error": True},
    ],
    ids=["success", "app-error", "gateway-refusal", "transport-error"],
)
def test_app_request_redacts_complete_success_and_error_results(
    monkeypatch: pytest.MonkeyPatch, declared_app: None, response: dict[str, Any]
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_get",
        lambda path, session_key=None, max_response_bytes=None, **_kwargs: response,
    )

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )

    assert _APP_REQUEST_TOKEN not in out


def test_app_request_redacts_footerless_multiline_pem_response(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    pem_dashes = "-" * 5
    key_body_lines = tuple(f"FakeKeyBodyLine{i}" + "A" * 44 for i in range(1, 4))
    private_key = pem_dashes + "BEGIN " + "PRIVATE KEY" + pem_dashes + "\n"
    private_key += "\n".join(key_body_lines)
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_get",
        lambda path, session_key=None, max_response_bytes=None, **_kwargs: {
            "privateKey": private_key
        },
    )

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )

    assert all(line not in out for line in key_body_lines)


def test_app_request_leaves_an_undeclared_route_to_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = AppManifest.from_dict(_manifest(agentRoutes=[]))
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest",
        lambda name: manifest if name == "issue-radar" else None,
    )
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "issue-radar", "enabled": True},
    )
    _identified(monkeypatch, "dashboard:chat-1")
    put = MagicMock(return_value={"error": "HTTP 403: Forbidden"})
    monkeypatch.setattr(apps_tools.mcp_core, "_put", put)

    out = apps_tools.app_request(
        "app_request",
        {
            "app": "issue-radar",
            "method": "PUT",
            "path": "/investigation",
            "body": {"findings": None},
        },
    )

    assert put.call_args.kwargs["extra_headers"] == {APP_REQUEST_HEADER: APP_REQUEST_HEADER_VALUE}
    assert out.startswith("Error:") and "HTTP 403: Forbidden" in out


def test_app_request_refuses_a_disabled_app_locally(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "slack-poller", "enabled": False},
    )
    _forbid_http(monkeypatch)
    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )
    assert out.startswith("Error:") and "not an enabled installed app" in out


def test_app_request_refuses_an_unidentified_caller(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "")
    _forbid_http(monkeypatch)
    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )
    assert out.startswith("Error:") and "directly-identified session" in out


def test_app_request_refuses_a_channel_agent(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "channel:C1:agent")
    _forbid_http(monkeypatch)
    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )
    assert "not available to channel agents" in out


def test_app_request_sends_the_strict_key_on_a_declared_route(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    calls: list[tuple[str, Any, Any, Any, Any]] = []

    def _fake_delete(
        path: str,
        body: Any = None,
        *,
        session_key: str | None = None,
        mark_transport_error: bool = False,
        max_response_bytes: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        assert mark_transport_error is True
        calls.append((path, body, session_key, max_response_bytes, extra_headers))
        return {"ok": True}

    monkeypatch.setattr(apps_tools.mcp_core, "_delete", _fake_delete)
    out = apps_tools.app_request(
        "app_request",
        {"app": "slack-poller", "method": "DELETE", "path": "/subscriptions/42"},
    )
    assert calls == [
        (
            "/api/apps/slack-poller/subscriptions/42",
            None,
            "dashboard:chat-1",
            apps_tools._APP_REQUEST_RESPONSE_CAP,
            {APP_REQUEST_HEADER: APP_REQUEST_HEADER_VALUE},
        )
    ]
    assert out.startswith("OK DELETE /subscriptions/42") and '"ok": true' in out


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_every_verb_sends_the_strict_key(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    manifest = AppManifest.from_dict(_manifest(agentRoutes=[f"{method} /items"]))
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "slack-poller", "enabled": True},
    )
    _identified(monkeypatch, "dashboard:chat-1")
    _forbid_http(monkeypatch)
    calls: list[tuple[str, Any, Any, Any]] = []

    def _fake(
        path: str,
        *_a: Any,
        session_key: str | None = None,
        max_response_bytes: int | None = None,
        extra_headers: dict[str, str] | None = None,
        **_k: Any,
    ) -> dict:
        calls.append((path, session_key, max_response_bytes, extra_headers))
        return {"ok": True}

    monkeypatch.setattr(apps_tools.mcp_core, f"_{method.lower()}", _fake)
    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": method, "path": "/items"}
    )
    assert calls == [
        (
            "/api/apps/slack-poller/items",
            "dashboard:chat-1",
            apps_tools._APP_REQUEST_RESPONSE_CAP,
            {APP_REQUEST_HEADER: APP_REQUEST_HEADER_VALUE},
        )
    ]
    assert out.startswith(f"OK {method} /items")


def test_app_request_marker_reaches_the_outgoing_headers(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    sent_headers: dict[str, str] = {}

    def _capture(_path: str, **kwargs: Any) -> dict[str, Any]:
        sent_headers.update(kwargs["headers"])
        return {"ok": True}

    monkeypatch.setattr(apps_tools.mcp_core, "_send", _capture)

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )

    assert sent_headers[APP_REQUEST_HEADER] == APP_REQUEST_HEADER_VALUE
    assert sent_headers["X-Session-Key"] == "dashboard:chat-1"
    assert out.startswith("OK GET /subscriptions")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_app_transport_failure_reports_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    manifest = AppManifest.from_dict(_manifest(agentRoutes=[f"{method} /items"]))
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda _name: manifest)
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "slack-poller", "enabled": True},
    )
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_resolve_api_target",
        lambda: ("http://127.0.0.1:5476", ""),
    )

    def _fail_after_send(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("read timeout")

    monkeypatch.setattr(apps_tools.mcp_core, "_api_urlopen", _fail_after_send)
    if method == "POST":
        monkeypatch.setattr(apps_tools.mcp_core, "_post", _REAL_MCP_POST)
    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": method, "path": "/items"}
    )

    assert "outcome unknown" in out
    assert "may have been applied" in out
    assert "read state back with a GET instead of resending" in out
    assert "answered" not in out


def test_app_request_renders_gateway_refusal_as_safe_to_retry(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_get",
        lambda path, session_key=None, max_response_bytes=None, **_kwargs: {
            "error": "gateway not reachable",
            "refused": True,
        },
    )

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )

    assert "refused before reaching the gateway" in out
    assert "safe to retry" in out
    assert "outcome unknown" not in out
    assert "answered" not in out


def test_get_app_transport_failure_keeps_the_existing_error_rendering(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_get",
        lambda path, session_key=None, max_response_bytes=None, **_kwargs: {
            "error": "read timeout"
        },
    )

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )

    assert out == "Error: slack-poller answered GET /subscriptions with: read timeout"
    assert "outcome unknown" not in out


class _OversizedResponse:
    def __init__(self, body: bytes, content_length: str | None = None) -> None:
        self.body = body
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        self.read_amounts: list[int | None] = []

    def __enter__(self) -> "_OversizedResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        self.read_amounts.append(amount)
        return self.body if amount is None else self.body[:amount]

    def close(self) -> None:
        return None


def _bounded_response(monkeypatch: pytest.MonkeyPatch, response: _OversizedResponse) -> None:
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_resolve_api_target",
        lambda: ("http://127.0.0.1:5476", ""),
    )
    monkeypatch.setattr(apps_tools.mcp_core, "_api_urlopen", lambda *_a, **_k: response)


def test_bounded_response_rejects_a_truncated_declared_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _OversizedResponse(b'{"ok": true}', content_length="999")
    _bounded_response(monkeypatch, response)

    out = apps_tools.mcp_core._send(
        "/api/x",
        headers={},
        mark_transport_error=True,
        max_response_bytes=1024,
    )

    assert out == {
        "error": "response ended before its declared Content-Length",
        "transport_error": True,
    }


def test_mutating_app_request_reports_a_truncated_response_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = AppManifest.from_dict(_manifest(agentRoutes=["POST /items"]))
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda _name: manifest)
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "slack-poller", "enabled": True},
    )
    _identified(monkeypatch, "dashboard:chat-1")
    response = _OversizedResponse(b"ok", content_length="3")
    _bounded_response(monkeypatch, response)
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    monkeypatch.setattr(apps_tools.mcp_core, "_post", _REAL_MCP_POST)

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "POST", "path": "/items"}
    )

    assert "outcome unknown" in out
    assert "response ended before its declared Content-Length" in out
    assert "answered" not in out
    assert not out.startswith("OK")


def test_bounded_response_accepts_a_matching_declared_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"ok": true}'
    response = _OversizedResponse(body, content_length=str(len(body)))
    _bounded_response(monkeypatch, response)

    out = apps_tools.mcp_core._send(
        "/api/x",
        headers={},
        mark_transport_error=True,
        max_response_bytes=1024,
    )

    assert out == {"ok": True}


def test_app_request_treats_an_empty_bounded_2xx_body_as_success(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    response = _OversizedResponse(b"")
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_resolve_api_target",
        lambda: ("http://127.0.0.1:5476", ""),
    )
    monkeypatch.setattr(apps_tools.mcp_core, "_api_urlopen", lambda *_a, **_k: response)

    out = apps_tools.app_request(
        "app_request",
        {"app": "slack-poller", "method": "DELETE", "path": "/subscriptions/42"},
    )

    assert response.read_amounts == [apps_tools._APP_REQUEST_RESPONSE_CAP + 1]
    assert out == "OK DELETE /subscriptions/42\n{}"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_app_request_returns_bounded_non_json_2xx_as_success(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    manifest = AppManifest.from_dict(_manifest(agentRoutes=[f"{method} /items"]))
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda _name: manifest)
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "slack-poller", "enabled": True},
    )
    _identified(monkeypatch, "dashboard:chat-1")
    response = _OversizedResponse(b"ok")
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_resolve_api_target",
        lambda: ("http://127.0.0.1:5476", ""),
    )
    monkeypatch.setattr(apps_tools.mcp_core, "_api_urlopen", lambda *_a, **_k: response)
    if method == "POST":
        monkeypatch.setattr(apps_tools.mcp_core, "_post", _REAL_MCP_POST)

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": method, "path": "/items"}
    )

    assert response.read_amounts == [apps_tools._APP_REQUEST_RESPONSE_CAP + 1]
    assert out == f'OK {method} /items\n{{"text": "ok"}}'


def test_app_request_reports_an_oversized_2xx_post_as_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = AppManifest.from_dict(_manifest(agentRoutes=["POST /items"]))
    monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda _name: manifest)
    monkeypatch.setattr(
        "kiro_crew.apps.manager._owner_json",
        lambda path: {"name": "slack-poller", "enabled": True},
    )
    _identified(monkeypatch, "dashboard:chat-1")
    response = _OversizedResponse(b"x" * (apps_tools._APP_REQUEST_RESPONSE_CAP + 1))
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_resolve_api_target",
        lambda: ("http://127.0.0.1:5476", ""),
    )
    monkeypatch.setattr(apps_tools.mcp_core, "_api_urlopen", lambda *_a, **_k: response)
    monkeypatch.setattr(apps_tools.mcp_core, "_post", _REAL_MCP_POST)

    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "POST", "path": "/items"}
    )

    assert response.read_amounts == [apps_tools._APP_REQUEST_RESPONSE_CAP + 1]
    assert "outcome unknown" in out
    assert f"exceeds the {apps_tools._APP_REQUEST_RESPONSE_CAP}-byte limit" in out
    assert "read state back with a GET instead of resending" in out
    assert "answered" not in out


def test_app_request_rejects_an_oversized_body_before_json_decoding(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    response = _OversizedResponse(
        b'{"rows":"' + b"x" * apps_tools._APP_REQUEST_RESPONSE_CAP + b'"}'
    )
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_resolve_api_target",
        lambda: ("http://127.0.0.1:5476", ""),
    )
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_api_urlopen",
        lambda *_a, **_k: response,
    )

    out = apps_tools.app_request(
        "app_request",
        {"app": "slack-poller", "method": "GET", "path": "/subscriptions"},
    )

    assert response.read_amounts == [apps_tools._APP_REQUEST_RESPONSE_CAP + 1]
    assert f"exceeds the {apps_tools._APP_REQUEST_RESPONSE_CAP}-byte limit" in out
    assert "truncated" not in out


def test_app_request_rejects_an_oversized_error_body_before_json_decoding(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    response = _OversizedResponse(
        b'{"error":"' + b"x" * apps_tools._APP_REQUEST_RESPONSE_CAP + b'"}'
    )
    error = apps_tools.mcp_core.urllib.error.HTTPError(
        "http://127.0.0.1:5476/api/apps/slack-poller/subscriptions",
        500,
        "Internal Server Error",
        cast(Any, {}),
        cast(Any, response),
    )
    monkeypatch.setattr(apps_tools.mcp_core, "_internal_secret", lambda: "secret")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_resolve_api_target",
        lambda: ("http://127.0.0.1:5476", ""),
    )

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(apps_tools.mcp_core, "_api_urlopen", _raise)

    out = apps_tools.app_request(
        "app_request",
        {"app": "slack-poller", "method": "GET", "path": "/subscriptions"},
    )

    assert response.read_amounts == [apps_tools._APP_REQUEST_RESPONSE_CAP + 1]
    assert f"exceeds the {apps_tools._APP_REQUEST_RESPONSE_CAP}-byte limit" in out


def test_app_request_surfaces_the_app_error(
    monkeypatch: pytest.MonkeyPatch, declared_app: None
) -> None:
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_get",
        lambda path, session_key=None, max_response_bytes=None, **_kwargs: {"error": "not found"},
    )
    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )
    assert out.startswith("Error:") and "not found" in out


@pytest.mark.parametrize("payload", [{"error": "x" * 70_000}, {"rows": ["x" * 70_000]}])
def test_app_request_caps_error_and_success_bodies_alike(
    monkeypatch: pytest.MonkeyPatch, declared_app: None, payload: dict[str, Any]
) -> None:
    """A non-2xx body is the app's text too: the same cap bounds it."""
    _identified(monkeypatch, "dashboard:chat-1")
    monkeypatch.setattr(
        apps_tools.mcp_core,
        "_get",
        lambda path, session_key=None, max_response_bytes=None, **_kwargs: payload,
    )
    out = apps_tools.app_request(
        "app_request", {"app": "slack-poller", "method": "GET", "path": "/subscriptions"}
    )
    assert "truncated (70" in out
    assert len(out) < apps_tools._APP_REQUEST_RESPONSE_CAP + 200


@pytest.mark.parametrize(
    "args",
    [
        {"app": "Slack", "method": "GET", "path": "/x"},
        {"app": "slack-poller", "method": "HEAD", "path": "/x"},
        {"app": "slack-poller", "method": "GET", "path": "/x?y=1"},
        {"app": "slack-poller", "method": "GET", "path": "/a%2Fb"},
        {"app": "slack-poller", "method": "GET", "path": "x"},
        {"app": "slack-poller", "method": "GET", "path": "/x", "body": {"a": 1}},
    ],
)
def test_app_request_schema_refuses_malformed_arguments(args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        validate_tool_args(args, APP_REQUEST_SCHEMA)


@pytest.mark.parametrize(
    ("pattern", "value"),
    [
        (_APP_REQUEST_PATH_RE, "/items/42\n"),
        (_APP_REQUEST_APP_RE, "slack-poller\n"),
    ],
)
def test_app_request_patterns_refuse_a_trailing_newline(pattern: Any, value: str) -> None:
    """``$`` matches before a final newline; the patterns must end at the string."""
    assert pattern.match(value) is None


def test_app_request_schema_accepts_get_with_an_empty_body() -> None:
    """An empty object carries nothing, so a GET may pass one."""
    cleaned = validate_tool_args(
        {"app": "slack-poller", "method": "GET", "path": "/subscriptions", "body": {}},
        APP_REQUEST_SCHEMA,
    )
    assert cleaned["method"] == "GET"


def test_app_request_schema_accepts_a_well_formed_call() -> None:
    cleaned = validate_tool_args(
        {"app": "slack-poller", "method": "POST", "path": "/subscriptions", "body": {"a": 1}},
        APP_REQUEST_SCHEMA,
    )
    assert cleaned["path"] == "/subscriptions" and cleaned["body"] == {"a": 1}


def test_app_request_is_advertised_with_a_handler() -> None:
    schemas = {schema["name"]: schema for schema in apps_tools.schemas()}
    assert "app_request" in schemas
    description = schemas["app_request"]["description"]
    assert "safe to retry" in description
    assert "unknown outcome" in description
    assert "gateway's or app's answer" in description
    assert apps_tools.HANDLERS["app_request"] is apps_tools.app_request
