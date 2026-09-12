"""``api_mediated_secret_request`` — the HOST-side mediation endpoint.

The in-sandbox ``kirocrew-secrets`` tool cannot read the vault (managed MCP
servers share the agent's mount namespace, where ``.vault`` and
``secret_request_policy.json`` are masked). So the tool forwards the non-secret
request intent to this endpoint in the unsandboxed dashboard process, which is
the only place the vault is readable. These tests pin that the endpoint:

* runs the blocking vault-read + dispatch OFF the event loop,
* returns ONLY the sanitized response fields,
* turns a fail-closed mediation error into a safe, secret-free 400, and
* denies a request with no resolvable session identity.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import sessions as sessions_mod
from kiro_crew.secrets_mediation.dispatch import SanitizedResponse
from kiro_crew.secrets_mediation.policy import PolicyError

SESSION = "dashboard:reviewer-slot"


def _request(
    payload: dict[str, Any], *, session: str = SESSION, internal_auth: bool = True
) -> MagicMock:
    request = MagicMock()
    request.headers = {"X-Session-Key": session} if session else {}

    async def _json() -> Any:
        return payload

    request.json = _json
    request.app = {"state": MagicMock()}
    request.get = lambda key, default=None: internal_auth if key == "internal_auth" else default
    return request


def _body(response: Any) -> Any:
    return json.loads(response.body.decode("utf-8"))


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    # internal_memory_scope returns (scope, refusal); no private boundary here.
    async def _scope(*_a: Any, **_k: Any) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(sessions_mod, "internal_memory_scope", _scope)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: MagicMock())
    monkeypatch.setattr(sessions_mod, "config_dir", lambda: "/nonexistent-config-dir")


@pytest.mark.asyncio
async def test_happy_path_returns_only_sanitized_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    call_threads: list[int] = []

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:
        call_threads.append(threading.get_ident())
        assert req.secret_name == "STRIPE_KEY"
        return SanitizedResponse(
            status=200,
            headers={"content-type": "application/json"},
            body='{"ok":true}',
            truncated=False,
            final_url_origin="https://api.stripe.com",
        )

    monkeypatch.setattr(sessions_mod, "perform_mediated_request", fake_perform)

    loop_thread = threading.get_ident()
    resp = await sessions_mod.api_mediated_secret_request(
        _request(
            {"secret_name": "STRIPE_KEY", "method": "GET", "url": "https://api.stripe.com/v1/x"}
        )
    )
    assert resp.status == 200
    assert _body(resp) == {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": '{"ok":true}',
        "truncated": False,
        "final_url_origin": "https://api.stripe.com",
    }
    # The blocking vault-read + dispatch must not run on the loop thread.
    assert call_threads and loop_thread not in call_threads


@pytest.mark.asyncio
async def test_mediation_error_becomes_a_safe_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:
        raise PolicyError(
            "Secret 'STRIPE_KEY' is authorized only for https://api.stripe.com, not "
            "https://evil.example. The request was refused before the secret was read."
        )

    monkeypatch.setattr(sessions_mod, "perform_mediated_request", fake_perform)

    resp = await sessions_mod.api_mediated_secret_request(
        _request({"secret_name": "STRIPE_KEY", "method": "GET", "url": "https://evil.example/x"})
    )
    assert resp.status == 400
    payload = _body(resp)
    assert payload["code"] == "mediation_refused"
    assert "refused before the secret was read" in payload["error"]


@pytest.mark.asyncio
async def test_missing_session_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    resp = await sessions_mod.api_mediated_secret_request(
        _request(
            {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}, session=""
        )
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "X-Session-Key required"


@pytest.mark.asyncio
async def test_missing_secret_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    resp = await sessions_mod.api_mediated_secret_request(
        _request({"method": "GET", "url": "https://api.example.com"})
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "secret_name is required"


@pytest.mark.asyncio
async def test_a_non_internal_secret_caller_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential-bearing egress must require the proven internal-secret
    authority — a browser cookie that reaches the strict path (or a mixed-mode
    reclassification) must not drive a mediated request that skips MCP approval.
    Fails without the ``internal_auth is True`` guard at the top of the handler.
    """
    _patch_common(monkeypatch)

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:  # pragma: no cover
        raise AssertionError("dispatch must not run for a non-internal caller")

    monkeypatch.setattr(sessions_mod, "perform_mediated_request", fake_perform)
    resp = await sessions_mod.api_mediated_secret_request(
        _request(
            {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
            internal_auth=False,
        )
    )
    assert resp.status == 403
    assert _body(resp)["code"] == "internal_auth_required"


def test_sandboxed_tool_has_no_import_path_to_the_vault() -> None:
    """``mcp_secrets`` runs in the agent sandbox where the vault is masked.

    It MUST forward to the host endpoint rather than read the vault itself, so
    its module must not import the vault or the host-only dispatch module. This
    pins the fix for the placement bug: putting ``SecretVault``/
    ``perform_mediated_request`` back into the sandboxed process would make every
    call fail at runtime (empty vault) and is exactly the regression to catch.
    """
    import ast
    from pathlib import Path

    import kiro_crew.mcp_secrets as mcp_secrets_mod

    source = Path(mcp_secrets_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    forbidden = {
        "kiro_crew.secrets",
        "kiro_crew.secrets.SecretVault",
        "kiro_crew.secrets_mediation.dispatch",
        "kiro_crew.secrets_mediation.policy",
        "kiro_crew.secrets_mediation.ssrf",
    }
    leaked = imported & forbidden
    assert not leaked, f"the sandboxed tool must not import the vault/dispatch: {leaked}"
