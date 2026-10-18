"""The pull-request read, checks, resolve, auto-merge and ready HTTP handlers: the
owner gate and local-token subjects, request validation, the audit record of a failure
or cancellation, and the response each handler maps. The status, issue, reply and
comment endpoints are tested beside their topics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

from kiro_crew.dashboard.handlers import source_providers as source


def _pin_local_owner_gate(monkeypatch) -> None:
    """Admit the request at the host-provenance gate ``api_token_local`` consults.

    The real gate resolves the caller's PID from the kernel's live loopback
    connection table (``platform_compat.get_tcp_peer_pid`` reads ``/proc/net/tcp``
    and every ``/proc/<pid>/fd``) and fails closed with 403 whenever that table is
    changing or ambiguous. Under xdist, sibling workers churn that table, so the
    host is an input to the test. The tests below pin the token's subject and
    claims, not provenance; the gate's own verdicts are pinned in ``test_member_memory_auth.py`` and
    ``test_dashboard_handlers_core_coverage_security_endpoints.py``.
    Patched on its defining module, which the handler imports at call time.
    """
    from kiro_crew.member_memory_auth import LocalOwnerProvenance

    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.local_owner_bootstrap_provenance",
        lambda _request: LocalOwnerProvenance(),
    )


@pytest.mark.asyncio
async def test_local_token_uses_configured_owner_subject(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import core

    generate = MagicMock(return_value="owner-token")
    audit = MagicMock()
    monkeypatch.setattr(core, "generate_token", generate)
    monkeypatch.setattr(core, "_sel", lambda: audit)
    _pin_local_owner_gate(monkeypatch)
    app = web.Application()
    app["local_secret"] = "local-secret"
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_get("/api/token/local", core.api_token_local)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/token/local?ttl=15m", headers={"X-Local-Secret": "local-secret"}
        )
        assert response.status == 200
        payload = await response.json()

    assert payload == {"token": "owner-token", "expires_in": 900}
    generate.assert_called_once_with("U_OWNER", ttl_seconds=900, extra=None)


@pytest.mark.asyncio
async def test_local_token_carries_embed_parent_port_claim(monkeypatch) -> None:
    """?embed_parent_port=<port> is baked into the token as a signed claim so the
    embedded remote can authorize that loopback parent origin in frame-ancestors."""
    from kiro_crew.dashboard.handlers import core

    generate = MagicMock(return_value="owner-token")
    monkeypatch.setattr(core, "generate_token", generate)
    monkeypatch.setattr(core, "_sel", lambda: MagicMock())
    _pin_local_owner_gate(monkeypatch)
    app = web.Application()
    app["local_secret"] = "local-secret"
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_get("/api/token/local", core.api_token_local)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/token/local?ttl=15m&embed_parent_port=5476",
            headers={"X-Local-Secret": "local-secret"},
        )
        assert response.status == 200

    generate.assert_called_once_with(
        "U_OWNER", ttl_seconds=900, extra={"embed_parent_port": "5476"}
    )


@pytest.mark.asyncio
async def test_local_token_uses_local_owner_subject_without_configured_owner(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import core

    generate = MagicMock(return_value="local-token")
    audit = MagicMock()
    monkeypatch.setattr(core, "generate_token", generate)
    monkeypatch.setattr(core, "_sel", lambda: audit)
    _pin_local_owner_gate(monkeypatch)
    app = web.Application()
    app["local_secret"] = "local-secret"
    state = MagicMock()
    state.owner_id = ""
    app["state"] = state
    app.router.add_get("/api/token/local", core.api_token_local)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/token/local?ttl=15m", headers={"X-Local-Secret": "local-secret"}
        )
        assert response.status == 200
        payload = await response.json()

    assert payload == {"token": "local-token", "expires_in": 900}
    generate.assert_called_once_with("local-app", ttl_seconds=900, extra=None)


@pytest.mark.parametrize("subject", ["local-app", "local-startup"])
@pytest.mark.asyncio
async def test_local_dashboard_subjects_can_act_without_configured_owner(
    monkeypatch, subject
) -> None:
    pull = {"url": "https://github.com/acme/repo/pull/12", "checks": []}
    fetch_pull = AsyncMock(return_value=pull)
    fetch_checks = AsyncMock(return_value=[])
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "fetch_pull_request", fetch_pull)
    monkeypatch.setattr(source, "fetch_pull_request_checks", fetch_checks)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)

    app = _app(user=subject, owner_id="")
    async with TestClient(TestServer(app)) as client:
        detail_response = await client.post("/api/source/pull-request", json={"url": pull["url"]})
        checks_response = await client.post(
            "/api/source/pull-request/checks", json={"url": pull["url"]}
        )
        resolve_response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": pull["url"], "threadId": "PRRT_thread1"},
        )

        assert detail_response.status == 200
        assert await detail_response.json() == pull
        assert checks_response.status == 200
        assert await checks_response.json() == {"checks": []}
        # The mutation passes on the same identity the reads did: with no owner
        # configured, a signed machine-local subject IS the owner.
        assert resolve_response.status == 200
        assert (await resolve_response.json())["resolved"] is True

    fetch_pull.assert_awaited_once_with(pull["url"], refresh=False)
    fetch_checks.assert_awaited_once_with(pull["url"])
    resolve.assert_awaited_once_with(pull["url"], "PRRT_thread1")

    request = _ResolveRequest()
    request.app["state"].owner_id = ""
    request._claims["user"] = subject
    assert source.is_owner_dashboard_request(request)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("app_kwargs", "reason"),
    [
        ({"owner_id": "", "user": "U_OTHER"}, "owner_not_configured"),
        ({"include_app_claim": False}, "app_token_not_allowed"),
        ({"app_name": None}, "app_token_not_allowed"),
        ({"app_name": "app-X"}, "app_token_not_allowed"),
        ({"user": "U_OTHER"}, "non_owner"),
        ({"user": ""}, "non_owner"),
        ({"include_user_claim": False}, "non_owner"),
    ],
)
@pytest.mark.parametrize(
    ("endpoint", "fetch_name", "operation"),
    [
        ("/api/source/pull-request", "fetch_pull_request", "source.pull_request.read"),
        (
            "/api/source/pull-request/checks",
            "fetch_pull_request_checks",
            "source.pull_request.checks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_handlers_require_explicit_owner_dashboard_claims(
    monkeypatch,
    _mock_source_sel,
    app_kwargs,
    reason,
    endpoint,
    fetch_name,
    operation,
) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, fetch_name, fetch)
    secret = "ghp_" + "a" * 36
    raw_url = f"https://github.com/acme/repo/pull/1?token={secret}"

    async with TestClient(TestServer(_app(**app_kwargs))) as client:
        response = await client.post(endpoint, json={"url": raw_url})
        payload = await response.json()

    assert response.status == 403
    assert payload == {"error": "forbidden"}
    fetch.assert_not_awaited()
    call = _mock_source_sel.log_api_access.call_args
    assert call.kwargs["operation"] == operation
    assert call.kwargs["outcome"] == "denied"
    assert call.kwargs["error"] == reason
    assert raw_url not in str(call)
    assert secret not in str(call)


@pytest.mark.asyncio
async def test_read_handler_allows_local_token_when_no_owner_configured(
    monkeypatch, _mock_source_sel
) -> None:
    """Local single-user install (no owner): the local dashboard token
    (subject ``local-app``, empty app claim) may use the credential-backed
    provider so viewing a PR diff does not require Slack/owner setup."""
    fetch = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(source, "fetch_pull_request", fetch)
    url = "https://github.com/acme/repo/pull/1"

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post("/api/source/pull-request", json={"url": url})
        assert response.status == 200
        assert (await response.json()) == {"ok": True}

    fetch.assert_awaited_once_with(url, refresh=False)


@pytest.mark.asyncio
async def test_read_handler_denies_non_local_subject_when_no_owner(
    monkeypatch, _mock_source_sel
) -> None:
    """No owner + a non ``local-app`` subject (e.g. a stale owner-minted token)
    still fails closed — the fallback is scoped to the genuine local token."""
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_pull_request", fetch)

    async with TestClient(TestServer(_app(owner_id="", user="U_OWNER", app_name=""))) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://github.com/acme/repo/pull/1"}
        )
        assert response.status == 403
        assert (await response.json()) == {"error": "forbidden"}

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_handler_allows_local_token_when_no_owner(monkeypatch) -> None:
    """The resolve mutation runs for a signed local subject with no owner, and
    the audit records it as completed under that subject rather than denied."""
    resolve = AsyncMock(return_value=None)
    audit = MagicMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/1", "threadId": "PRRT_1"},
        )
        assert response.status == 200
        assert (await response.json())["resolved"] is True

    resolve.assert_awaited_once_with("https://github.com/acme/repo/pull/1", "PRRT_1")
    audit.log_api_access.assert_called_once_with(
        caller="local-app",
        operation="source.pull_request.resolve",
        outcome="completed",
        source="dashboard",
        error="",
    )


@pytest.mark.parametrize(
    ("handler", "fetch_name", "operation"),
    [
        (
            source.api_pull_request_source,
            "fetch_pull_request",
            "source.pull_request.read",
        ),
        (
            source.api_pull_request_checks,
            "fetch_pull_request_checks",
            "source.pull_request.checks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_handlers_audit_cancellation_while_reading_body(
    monkeypatch, handler, fetch_name, operation
) -> None:
    audit = MagicMock()
    fetch = AsyncMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, fetch_name, fetch)
    request = _ResolveRequest(json_error=source.asyncio.CancelledError())

    with pytest.raises(source.asyncio.CancelledError):
        await handler(request)

    fetch.assert_not_awaited()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation=operation,
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.parametrize(
    ("handler", "fetch_name", "operation"),
    [
        (
            source.api_pull_request_source,
            "fetch_pull_request",
            "source.pull_request.read",
        ),
        (
            source.api_pull_request_checks,
            "fetch_pull_request_checks",
            "source.pull_request.checks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_handlers_audit_cancellation_during_provider_fetch(
    monkeypatch, handler, fetch_name, operation
) -> None:
    audit = MagicMock()
    fetch = AsyncMock(side_effect=source.asyncio.CancelledError())
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, fetch_name, fetch)
    request = _ResolveRequest({"url": "https://github.com/acme/repo/pull/12"})

    with pytest.raises(source.asyncio.CancelledError):
        await handler(request)

    fetch.assert_awaited_once()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation=operation,
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.parametrize(
    ("handler", "fetch_name"),
    [
        (source.api_pull_request_source, "fetch_pull_request"),
        (source.api_pull_request_checks, "fetch_pull_request_checks"),
    ],
)
@pytest.mark.asyncio
async def test_read_handler_cancellation_survives_source_audit_failure(
    monkeypatch, handler, fetch_name
) -> None:
    audit = MagicMock()
    audit.log_api_access.side_effect = OSError("audit filesystem unavailable")
    fetch = AsyncMock(side_effect=source.asyncio.CancelledError())
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, fetch_name, fetch)
    request = _ResolveRequest({"url": "https://github.com/acme/repo/pull/12"})

    with pytest.raises(source.asyncio.CancelledError):
        await handler(request)

    fetch.assert_awaited_once()
    audit.log_api_access.assert_called_once()


@pytest.mark.asyncio
async def test_owner_denial_survives_source_audit_failure(monkeypatch) -> None:
    audit = MagicMock()
    audit.log_api_access.side_effect = OSError("audit filesystem unavailable")
    fetch = AsyncMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, "fetch_pull_request", fetch)
    request = _ResolveRequest({"url": "https://github.com/acme/repo/pull/12"})
    request._claims["user"] = "U_OTHER"

    response = await source.api_pull_request_source(request)  # type: ignore[arg-type]

    assert response.status == 403
    fetch.assert_not_awaited()
    audit.log_api_access.assert_called_once()


@pytest.mark.asyncio
async def test_handler_returns_validation_error() -> None:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request", json={"url": "https://example.com/pr/1"}
        )
        assert response.status == 400
        assert "Only github.com" in (await response.json())["error"]


@pytest.mark.asyncio
async def test_handler_returns_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(
        source,
        "fetch_pull_request",
        AsyncMock(side_effect=source.SourceProviderError("gh is not authenticated")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request",
            json={"url": "https://github.com/acme/repo/pull/1"},
        )
        assert response.status == 503
        assert (await response.json())["error"] == "gh is not authenticated"


@pytest.mark.asyncio
async def test_checks_handler_returns_normalized_checks(monkeypatch) -> None:
    checks = [
        {
            "name": "test",
            "workflow": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "bucket": "passed",
            "url": "",
            "startedAt": "",
            "completedAt": "",
        }
    ]
    fetch = AsyncMock(return_value=checks)
    monkeypatch.setattr(source, "fetch_pull_request_checks", fetch)
    url = "https://github.com/acme/repo/pull/12"

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/pull-request/checks", json={"url": url})
        assert response.status == 200
        assert (await response.json()) == {"checks": checks}

    fetch.assert_awaited_once_with(url)


@pytest.mark.asyncio
async def test_checks_handler_returns_validation_error() -> None:
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/checks", json={"url": "https://example.com/pr/1"}
        )
        assert response.status == 400
        assert "Only github.com" in (await response.json())["error"]


@pytest.mark.asyncio
async def test_checks_handler_returns_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(
        source,
        "fetch_pull_request_checks",
        AsyncMock(side_effect=source.SourceProviderError("gh is not authenticated")),
    )
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/checks",
            json={"url": "https://github.com/acme/repo/pull/1"},
        )
        assert response.status == 503
        assert (await response.json())["error"] == "gh is not authenticated"


@pytest.mark.asyncio
async def test_resolve_handler_success(monkeypatch) -> None:
    resolver = AsyncMock(return_value=None)
    audit = MagicMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    monkeypatch.setattr(source, "_sel", lambda: audit)
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "PRRT_thread1"},
        )
        assert response.status == 200
        assert (await response.json())["resolved"] is True
    resolver.assert_awaited_once_with("https://github.com/acme/repo/pull/12", "PRRT_thread1")
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="completed",
        source="dashboard",
        error="",
    )


@pytest.mark.asyncio
async def test_resolve_handler_audits_provider_failure_without_provider_text(monkeypatch) -> None:
    secret = "ghp_" + "a" * 36
    resolver = AsyncMock(side_effect=source.SourceProviderError(f"provider failed {secret}"))
    audit = MagicMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "PRRT_thread1"},
        )
        assert response.status == 503

    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="provider_error",
    )
    assert secret not in str(audit.log_api_access.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "action_name"),
    [
        ("/api/source/pull-request/auto-merge", "enable_pull_request_auto_merge"),
        ("/api/source/pull-request/ready", "mark_pull_request_ready"),
    ],
)
async def test_action_handlers_allow_local_token_when_no_owner(
    monkeypatch, _mock_source_sel, path: str, action_name: str
) -> None:
    """A signed machine-local subject is the owner when none is configured.

    The negative control is
    ``test_no_owner_mutation_denies_non_local_subjects``: the allowance is the
    local dashboard identity, not the absence of an owner.
    """
    action = AsyncMock(return_value=None)
    monkeypatch.setattr(source, action_name, action)

    async with TestClient(TestServer(_app(owner_id="", user="local-app", app_name=""))) as client:
        response = await client.post(path, json={"url": "https://github.com/acme/repo/pull/1"})
        assert response.status == 200

    # Auto-merge carries the consent flag, ready does not; both must have run.
    assert action.await_args.args == ("https://github.com/acme/repo/pull/1",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "app_kwargs",
    [
        # A non-local subject must not learn which denial class it hit.
        {"owner_id": "", "user": "U_OTHER", "app_name": ""},
        # Nor an app token, even one carrying a local-shaped subject.
        {"owner_id": "", "user": "local-app", "app_name": "app-X"},
        # Nor an unauthenticated caller with no user claim at all.
        {"owner_id": "", "user": "", "app_name": ""},
    ],
)
async def test_no_owner_mutation_denies_non_local_subjects(
    monkeypatch, _mock_source_sel, app_kwargs: dict
) -> None:
    """The no-owner allowance is the signed machine-local dashboard identity.

    Every other caller keeps the generic forbidden body, which is also what
    stops the response from disclosing the install's owner state.
    """
    action = AsyncMock()
    monkeypatch.setattr(source, "enable_pull_request_auto_merge", action)

    async with TestClient(TestServer(_app(**app_kwargs))) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/1"},
        )
        assert response.status == 403
        assert (await response.json()) == {"error": "forbidden"}

    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_merge_handler_success_reports_method(monkeypatch) -> None:
    action = AsyncMock(return_value="squash")
    audit = MagicMock()
    monkeypatch.setattr(source, "enable_pull_request_auto_merge", action)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/auto-merge",
            json={"url": "https://github.com/acme/repo/pull/12", "confirmImmediateMerge": True},
        )
        assert response.status == 200
        assert (await response.json()) == {"autoMerge": True, "mergeMethod": "squash"}

    action.assert_awaited_once_with(
        "https://github.com/acme/repo/pull/12", confirm_immediate_merge=True
    )
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.auto_merge",
        outcome="completed",
        source="dashboard",
        error="",
    )


@pytest.mark.asyncio
async def test_ready_handler_success(monkeypatch) -> None:
    action = AsyncMock(return_value=None)
    audit = MagicMock()
    monkeypatch.setattr(source, "mark_pull_request_ready", action)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/ready",
            json={"url": "https://github.com/acme/repo/pull/12"},
        )
        assert response.status == 200
        assert (await response.json()) == {"ready": True}

    action.assert_awaited_once_with("https://github.com/acme/repo/pull/12")
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.ready",
        outcome="completed",
        source="dashboard",
        error="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "action_name", "operation"),
    [
        (
            "/api/source/pull-request/auto-merge",
            "enable_pull_request_auto_merge",
            "source.pull_request.auto_merge",
        ),
        (
            "/api/source/pull-request/ready",
            "mark_pull_request_ready",
            "source.pull_request.ready",
        ),
    ],
)
async def test_action_handlers_audit_provider_failure_without_provider_text(
    monkeypatch, path: str, action_name: str, operation: str
) -> None:
    secret = "ghp_" + "a" * 36
    audit = MagicMock()
    monkeypatch.setattr(
        source,
        action_name,
        AsyncMock(side_effect=source.SourceProviderError(f"provider failed {secret}")),
    )
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(path, json={"url": "https://github.com/acme/repo/pull/12"})
        assert response.status == 503

    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation=operation,
        outcome="failed",
        source="dashboard",
        error="provider_error",
    )
    assert secret not in str(audit.log_api_access.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "action_name"),
    [
        ("/api/source/pull-request/auto-merge", "enable_pull_request_auto_merge"),
        ("/api/source/pull-request/ready", "mark_pull_request_ready"),
    ],
)
async def test_action_handlers_return_400_for_rejected_requests(
    monkeypatch, _mock_source_sel, path: str, action_name: str
) -> None:
    monkeypatch.setattr(source, action_name, AsyncMock(side_effect=ValueError("not allowed here")))

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(path, json={"url": "https://github.com/acme/repo/pull/12"})
        assert response.status == 400
        assert (await response.json()) == {"error": "not allowed here"}


class _ResolveRequest:
    """Minimal authenticated request stub for cancellation audit tests."""

    def __init__(self, body=None, *, json_error=None) -> None:
        state = MagicMock()
        state.owner_id = "U_OWNER"
        self.app = {"state": state}
        self._claims = {"user": "U_OWNER", "app": ""}
        self._body = body
        self._json_error = json_error

    def get(self, key, default=None):
        return self._claims.get(key, default)

    def __contains__(self, key) -> bool:
        return key in self._claims

    def __getitem__(self, key):
        return self._claims[key]

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body


@pytest.mark.asyncio
async def test_resolve_handler_audits_cancellation_while_reading_body(monkeypatch) -> None:
    audit = MagicMock()
    resolver = AsyncMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    request = _ResolveRequest(json_error=source.asyncio.CancelledError())

    with pytest.raises(source.asyncio.CancelledError):
        await source.api_pull_request_resolve(request)  # type: ignore[arg-type]

    resolver.assert_not_awaited()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.asyncio
async def test_resolve_handler_audits_cancellation_during_mutation(monkeypatch) -> None:
    audit = MagicMock()
    resolver = AsyncMock(side_effect=source.asyncio.CancelledError())
    monkeypatch.setattr(source, "_sel", lambda: audit)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    request = _ResolveRequest(
        {
            "url": "https://github.com/acme/repo/pull/12",
            "threadId": "PRRT_thread1",
        }
    )

    with pytest.raises(source.asyncio.CancelledError):
        await source.api_pull_request_resolve(request)  # type: ignore[arg-type]

    resolver.assert_awaited_once()
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="request_cancelled",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "app_name", "owner_id", "reason"),
    [
        ("U_OTHER", "", "U_OWNER", "non_owner"),
        ("U_OWNER", "source-app", "U_OWNER", "app_token_not_allowed"),
        ("U_OWNER", "", "", "owner_not_configured"),
    ],
)
async def test_resolve_handler_denies_non_owner_app_and_unconfigured_owner(
    monkeypatch, user: str, app_name: str, owner_id: str, reason: str
) -> None:
    resolver = AsyncMock(return_value=None)
    audit = MagicMock()
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolver)
    monkeypatch.setattr(source, "_sel", lambda: audit)

    async with TestClient(
        TestServer(_app(user=user, app_name=app_name, owner_id=owner_id))
    ) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "PRRT_thread1"},
        )

    assert response.status == 403
    resolver.assert_not_awaited()
    audit.log_api_access.assert_called_once_with(
        caller=user,
        operation="source.pull_request.resolve",
        outcome="denied",
        source="dashboard",
        error=reason,
    )


@pytest.mark.asyncio
async def test_read_handler_allows_configured_dashboard_owner(monkeypatch) -> None:
    payload = {"provider": "github", "url": "https://github.com/acme/repo/pull/12"}
    fetch = AsyncMock(return_value=payload)
    monkeypatch.setattr(source, "fetch_pull_request", fetch)

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/pull-request", json={"url": payload["url"]})
        assert response.status == 200
        assert await response.json() == payload

    fetch.assert_awaited_once_with(payload["url"], refresh=False)


@pytest.mark.asyncio
async def test_resolve_handler_rejects_bad_thread_id(monkeypatch) -> None:
    audit = MagicMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/12", "threadId": "bad id"},
        )
        assert response.status == 400
    audit.log_api_access.assert_called_once_with(
        caller="U_OWNER",
        operation="source.pull_request.resolve",
        outcome="failed",
        source="dashboard",
        error="invalid_request",
    )
