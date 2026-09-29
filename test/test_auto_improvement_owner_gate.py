"""Owner gate on the auto-improvement routes that drive agents or publish as the owner.

Each route here either turns the owner's agents loose on a repository (clone,
config, run, calibrate, PR watcher) or publishes under the owner's identity
(one-click commit push, draft pull request), or destroys or redirects the owner's
work (forget, purge, stop, session links), or spends the owner's forge credential
(PR status). Every case drives the real handler
and replaces the first thing its body does with a recorder, so a refused caller
is proven to stop at the gate, before any request parsing or disk read.

Three callers per route: the dashboard owner (reaches the body), a non-owner
holding a dashboard token with ``app == ""`` (403), and another app's token (403).
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.auto_improvement.backend import routes, store

OWNER = "owner-subject"

#: Routes that let the caller steer the owner's agents.
_AGENT_ROUTES = [
    ("PUT", "_handle_put_config", "auto_improvement.put_config"),
    ("POST", "_handle_setup_clone", "auto_improvement.setup_clone"),
    ("POST", "_handle_run_start", "auto_improvement.run_start"),
    ("POST", "_handle_calibrate", "auto_improvement.calibrate"),
    ("POST", "_handle_watcher_start", "auto_improvement.watcher_start"),
    # A read, but its reconcile sweep starts watchers and can mark drafts ready.
    ("GET", "_handle_watchers", "auto_improvement.watchers_list"),
]

#: Routes that publish under the owner's git or GitHub identity.
_PUBLISH_ROUTES = [
    ("POST", "_handle_commit", "auto_improvement.commit"),
    ("POST", "_handle_draft_pr", "auto_improvement.draft_pr"),
]

#: Routes that erase, stop or re-point the owner's work.
_OWNER_WORK_ROUTES = [
    ("POST", "_handle_forget", "auto_improvement.forget"),
    ("POST", "_handle_purge", "auto_improvement.purge"),
    ("POST", "_handle_purge_dead", "auto_improvement.purge_dead"),
    ("POST", "_handle_run_stop", "auto_improvement.run_stop"),
    ("POST", "_handle_watcher_stop", "auto_improvement.watcher_stop"),
    ("PUT", "_handle_save_session", "auto_improvement.session_save"),
    ("DELETE", "_handle_delete_session", "auto_improvement.session_delete"),
]

#: A read that fetches a pull request with the owner's forge credential.
_CREDENTIAL_ROUTES = [
    ("GET", "_handle_pr_status", "auto_improvement.pr_status"),
]

_ALL_ROUTES = _AGENT_ROUTES + _PUBLISH_ROUTES + _OWNER_WORK_ROUTES + _CREDENTIAL_ROUTES


class _Reached(Exception):
    """Raised by the recorder once a handler has passed its gate."""


def _request(method: str, *, user: str, app_claim: str) -> web.Request:
    app = web.Application()
    app["state"] = mock.MagicMock(owner_id=OWNER)
    request = make_mocked_request(
        method,
        "/api/apps/auto-improvement/x?url=https://github.com/o/r/pull/1",
        match_info={"fp": "abc123", "key": "pr-o_r-1"},
        app=app,
    )
    request["app"] = app_claim
    request["user"] = user
    return request


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Replace every handler's first body step, and the allowed-decision audit."""
    seen: dict[str, list[Any]] = {"body": [], "audit": []}

    async def _json_body(_request: web.Request) -> dict:
        seen["body"].append("json_body")
        raise _Reached

    def _validated_fp(_request: web.Request) -> tuple[str, None]:
        seen["body"].append("validated_fp")
        raise _Reached

    def _read_json(*_a: Any, **_k: Any) -> dict:
        seen["body"].append("read_json")
        raise _Reached

    monkeypatch.setattr(routes, "_json_body", _json_body)
    monkeypatch.setattr(routes, "_validated_fp", _validated_fp)

    def _get_registry() -> None:
        seen["body"].append("watcher_registry")
        raise _Reached

    monkeypatch.setattr(routes.pr_watchers, "get_registry", _get_registry)

    def _sync_sink(label: str) -> Any:
        def _sink(*_a: Any, **_k: Any) -> None:
            seen["body"].append(label)
            raise _Reached

        return _sink

    async def _fetch_pr_status(*_a: Any, **_k: Any) -> dict:
        seen["body"].append("fetch_pr_status")
        raise _Reached

    monkeypatch.setattr(routes.ledger_admin, "purge_dead", _sync_sink("purge_dead"))
    monkeypatch.setattr(routes.runner, "get_supervisor", _sync_sink("supervisor"))
    monkeypatch.setattr(store, "delete_session", _sync_sink("delete_session"))
    monkeypatch.setattr(routes.pr_checks, "fetch_pr_status", _fetch_pr_status)
    monkeypatch.setattr(store, "read_json", _read_json)
    monkeypatch.setattr(
        routes,
        "_audit_owner_route_allowed_sync",
        lambda caller, operation: seen["audit"].append((caller, operation)),
    )
    return seen


async def _call(name: str, request: web.Request) -> web.StreamResponse | None:
    try:
        return await getattr(routes, name)(request)
    except _Reached:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("method,name,operation", _ALL_ROUTES, ids=[r[1] for r in _ALL_ROUTES])
async def test_the_owner_reaches_the_handler_body(
    recorder: dict[str, list[Any]], method: str, name: str, operation: str
) -> None:
    await _call(name, _request(method, user=OWNER, app_claim=""))
    assert recorder["body"], f"{name} never reached its body for the owner"
    assert recorder["audit"] == [(OWNER, operation)]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,name,operation", _ALL_ROUTES, ids=[r[1] for r in _ALL_ROUTES])
async def test_a_non_owner_dashboard_token_is_refused_at_the_gate(
    recorder: dict[str, list[Any]], method: str, name: str, operation: str
) -> None:
    response = await _call(name, _request(method, user="U0NONOWNER", app_claim=""))
    assert response is not None and response.status == 403
    assert recorder["body"] == [], f"{name} ran its body for a non-owner"
    assert recorder["audit"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,name,operation", _ALL_ROUTES, ids=[r[1] for r in _ALL_ROUTES])
async def test_an_app_token_is_refused_at_the_gate(
    recorder: dict[str, list[Any]], method: str, name: str, operation: str
) -> None:
    response = await _call(name, _request(method, user=OWNER, app_claim="some-app"))
    assert response is not None and response.status == 403
    assert recorder["body"] == [], f"{name} ran its body for an app token"
    assert recorder["audit"] == []
