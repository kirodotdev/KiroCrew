"""The stale pre-owner session denial carries its own machine-readable label.

A dashboard token's subject is fixed at mint time as ``owner_id or
<bootstrap subject>`` and token refresh re-mints from the incoming subject, so
a session signed in before ``KIROCREW_OWNER_ID`` was configured carries
``local-app`` / ``local-startup`` forever. Once an owner exists the owner gate
denies that subject — correctly — and these tests pin that the denial is
labelled ``401 stale_session_reauth`` so the dashboard can prompt a re-sign-in,
while every other denied caller keeps its generic response:

  (a) a signed bootstrap subject on a dashboard-user request with an owner
      configured gets the distinct denial;
  (b) it is still a DENIAL — the guarded action never runs;
  (c) an unauthenticated / invalid caller keeps the generic response (the
      discriminator must not leak to an unauthenticated party);
  (d) an app-token caller is unaffected;
  (e) the owner-matching caller is unaffected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.dashboard.handlers import source_providers as source

STALE = source.STALE_OWNER_SESSION_CODE


@pytest.fixture(autouse=True)
def _mock_source_sel(monkeypatch):
    audit = MagicMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    return audit


def _mocked_request(
    *,
    user: object = "local-startup",
    app_claim: object = "",
    owner_id: str = "U_OWNER",
    include_user_claim: bool = True,
    include_app_claim: bool = True,
) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.owner_id = owner_id
    app["state"] = state
    request = make_mocked_request("POST", "/x", app=app)
    if include_user_claim:
        request["user"] = user
    if include_app_claim:
        request["app"] = app_claim
    return request


# ── the helper itself ────────────────────────────────────────────────────────


@pytest.mark.parametrize("subject", ["local-app", "local-startup"])
def test_signed_bootstrap_subject_with_owner_gets_distinct_denial(subject) -> None:
    resp = source.stale_owner_session_response(_mocked_request(user=subject))
    assert resp is not None
    assert resp.status == 401
    assert resp.text is not None
    assert f'"code": "{STALE}"' in resp.text


@pytest.mark.parametrize(
    "kwargs",
    [
        # No owner configured: the bootstrap subjects are the implicit owner and
        # are not stale — and when they ARE denied (mutations), stay generic.
        {"owner_id": ""},
        # App tokens keep their generic denial whatever their subject says.
        {"app_claim": "app-X"},
        # An absent app claim means the middleware never authenticated the
        # caller as a dashboard user: no discriminator for the unauthenticated.
        {"include_app_claim": False},
        {"app_claim": None},
        # A signed NON-bootstrap subject (e.g. an allowed Slack user's dashboard
        # token) is an ordinary non-owner, not a stale session.
        {"user": "U_SOMEONE_ELSE"},
        # No user claim at all.
        {"include_user_claim": False},
        {"user": ""},
    ],
)
def test_every_other_denied_caller_keeps_the_generic_response(kwargs) -> None:
    assert source.stale_owner_session_response(_mocked_request(**kwargs)) is None


# ── through the owner gate (source-provider mutation route) ─────────────────


def _app(
    *,
    user: str = "local-startup",
    app_name: object = "",
    owner_id: str = "U_OWNER",
    include_user_claim: bool = True,
    include_app_claim: bool = True,
) -> web.Application:
    @web.middleware
    async def fake_auth(request, handler):
        if include_user_claim:
            request["user"] = user
        if include_app_claim:
            request["app"] = app_name
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    state = MagicMock()
    state.owner_id = owner_id
    app["state"] = state
    app.router.add_post("/api/source/pull-request/resolve", source.api_pull_request_resolve)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["local-app", "local-startup"])
async def test_owner_gate_labels_stale_bootstrap_denial_and_grants_nothing(
    monkeypatch, subject
) -> None:
    """(a) + (b): the distinct label is still a denial — the mutation never runs."""
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)

    async with TestClient(TestServer(_app(user=subject))) as client:
        resp = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/1", "threadId": "T1"},
        )
        assert resp.status == 401
        payload = await resp.json()

    assert payload["code"] == STALE
    resolve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "app_kwargs",
    [
        # (c) unauthenticated / invalid callers: generic 403, no discriminator.
        {"include_user_claim": False},
        {"user": ""},
        {"user": "U_SOMEONE_ELSE"},
        # (d) app tokens: unchanged, even with a bootstrap subject.
        {"app_name": "app-X"},
        {"app_name": "app-X", "user": "local-app"},
        {"include_app_claim": False},
    ],
)
async def test_owner_gate_keeps_generic_denial_for_everyone_else(monkeypatch, app_kwargs) -> None:
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)

    async with TestClient(TestServer(_app(**app_kwargs))) as client:
        resp = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/1", "threadId": "T1"},
        )
        assert resp.status == 403
        payload = await resp.json()

    assert payload == {"error": "forbidden"}
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_matching_caller_is_unaffected(monkeypatch) -> None:
    """(e): the configured owner still passes the gate."""
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(source, "resolve_pull_request_thread", resolve)

    async with TestClient(TestServer(_app(user="U_OWNER"))) as client:
        resp = await client.post(
            "/api/source/pull-request/resolve",
            json={"url": "https://github.com/acme/repo/pull/1", "threadId": "T1"},
        )
        assert resp.status == 200

    resolve.assert_awaited_once()


# ── the sibling owner-gate deny sites the issue enumerates ──────────────────


def test_chat_gate_labels_stale_bootstrap_denial() -> None:
    """The Trust/YOLO switch gate (chat mode / slot approve / worktree /
    followup all share ``deny_non_dashboard_caller``)."""
    from kiro_crew.dashboard import chat_handlers as ch

    request = _mocked_request()
    with patch.object(ch, "sel", return_value=MagicMock()):
        resp = ch.deny_non_dashboard_caller(request, "chat_mode")
    assert resp is not None and resp.status == 401


def test_chat_gate_keeps_generic_denial_for_plain_non_owner() -> None:
    from kiro_crew.dashboard import chat_handlers as ch

    request = _mocked_request(user="U_SOMEONE_ELSE")
    with patch.object(ch, "sel", return_value=MagicMock()):
        resp = ch.deny_non_dashboard_caller(request, "chat_mode")
    assert resp is not None and resp.status == 403


def test_ask_question_gate_labels_stale_bootstrap_denial() -> None:
    from kiro_crew.dashboard.handlers import ask_question as aq

    request = _mocked_request()
    with patch.object(aq, "sel", return_value=MagicMock()):
        resp = aq._deny_non_owner(request, "ask_question")
    assert resp is not None and resp.status == 401


def test_browser_gate_labels_stale_bootstrap_denial() -> None:
    from kiro_crew.dashboard.handlers import messaging as msg

    request = _mocked_request()
    with patch.object(msg, "_sel", return_value=MagicMock()):
        resp = msg._deny_non_owner_browser_request(request, "browser.install")
    assert resp is not None and resp.status == 401


def test_cloud_gate_labels_stale_bootstrap_denial() -> None:
    from kiro_crew.dashboard import handlers_cloud as cloud

    request = _mocked_request()
    with patch.object(cloud, "_audit", MagicMock()):
        resp = cloud._guard(request, "cloud.setup")
    assert resp is not None and resp.status == 401
    # The distinct label, not _guard's own unauthenticated-401 or the generic
    # cloud_owner_only body.
    assert resp.text is not None and STALE in resp.text


@pytest.mark.asyncio
async def test_mcp_apps_gate_labels_stale_bootstrap_denial() -> None:
    from kiro_crew.dashboard.handlers import mcp_apps

    @web.middleware
    async def fake_auth(request, handler):
        request["user"] = "local-startup"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_post("/api/mcp-apps/call", mcp_apps.api_mcp_apps_call)

    with patch.object(mcp_apps, "SecurityEventLog", return_value=MagicMock()):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/mcp-apps/call",
                json={"spool_id": "s1", "tool": "t", "arguments": {}},
            )
            assert resp.status == 401
            payload = await resp.json()
    assert payload["code"] == STALE


@pytest.mark.asyncio
async def test_agents_gate_labels_stale_bootstrap_denial() -> None:
    from kiro_crew.dashboard.handlers import agents

    request = _mocked_request()
    with patch.object(agents, "_sel", return_value=MagicMock()):
        resp = await agents._require_owner(request, "agents.update")
    assert resp is not None and resp.status == 401
    assert resp.text is not None and STALE in resp.text


@pytest.mark.asyncio
async def test_aws_consent_gate_labels_stale_bootstrap_denial() -> None:
    from kiro_crew.dashboard.handlers import aws_consent as consent_handlers

    request = _mocked_request()
    with patch.object(consent_handlers.aws_consent, "audit_decision", MagicMock()):
        resp = consent_handlers._deny_non_owner(request, "aws_consent.read")
    assert resp is not None and resp.status == 401
    assert resp.text is not None and STALE in resp.text


@pytest.mark.asyncio
async def test_instances_search_gate_labels_stale_bootstrap_denial(monkeypatch) -> None:
    """The federated-search deny site, driven through the real handler with the
    route's outer feature gate passed so the owner check is what answers."""
    from kiro_crew.dashboard import handlers_instances as hi

    monkeypatch.setattr(hi, "_guard", lambda request, operation: None)
    monkeypatch.setattr(hi, "_audit", MagicMock())
    request = _mocked_request()
    resp = await hi.api_instances_search_sessions(request)
    assert resp.status == 401
    assert resp.text is not None and STALE in resp.text
