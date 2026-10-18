"""Fixtures and builders shared by ``test_source_providers.py`` and the topic
modules split out of it.

A plain module, not a conftest: each of those modules imports what it uses by name.
Every one of them imports all three autouse fixtures, so each keeps the autouse set
the single module had. ``_mock_source_sel`` is declared under ``name=`` and imported
as ``_mock_source_sel_fixture``, so a test that requests it by its fixture name does
not shadow the import (flake8 F811).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import source_providers as source


@pytest.fixture(autouse=True, name="_mock_source_sel")
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse SEL stub of test_source_providers.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _mock_source_sel_fixture(monkeypatch):
    audit = MagicMock()
    monkeypatch.setattr(source, "_sel", lambda: audit)
    return audit


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse visibility recorder of test_source_providers.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _no_visibility_refresh_side_task(monkeypatch):
    """Keep the repo-visibility refresh out of tests that are not about it.

    A cache write-through schedules ``_refresh_repo_visibility`` as a detached
    task; nothing in this module awaits it, so it ran on into the NEXT test and
    past this one's pins. Its ``_run_provider`` resolves the provider CLI on a
    worker thread, and that resolution reads ``workspace_root()``, which reached
    ``config_dir()`` after ``KIROCREW_HOME`` had been unpinned and created the
    operator's real ``~/.kiro/crew`` (third side-effect audit, four tests here).
    No test in this module asserts anything about visibility
    (``test_public_repo_chip_status.py`` owns that surface and drains the set
    itself), so the scheduler is a recorder here: the calls are observable, the
    task is never spawned, and nothing outlives the test.
    """
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        source,
        "schedule_visibility_refresh",
        lambda urls, *a, **k: scheduled.append((tuple(urls), a, k)),
    )
    yield scheduled
    for task in list(source._VISIBILITY_TASKS):
        if not task.get_loop().is_closed():
            task.cancel()
    source._VISIBILITY_TASKS.clear()


def _rollup_reply(rows: list[dict], head: str = "abc123") -> dict:
    """A one-page `gh api graphql` rollup reply carrying *rows* at *head*."""
    nodes = [
        {"__typename": "StatusContext" if "context" in row else "CheckRun", **row} for row in rows
    ]
    contexts = {
        "totalCount": len(nodes),
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes,
    }
    commit = {"oid": head, "statusCheckRollup": {"contexts": contexts}}
    pull = {"headRefOid": head, "commits": {"nodes": [{"commit": commit}]}}
    return {"data": {"repository": {"pullRequest": pull}}}


def _app(
    *,
    user: str = "U_OWNER",
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
    app.router.add_post("/api/source/pull-request", source.api_pull_request_source)
    app.router.add_post("/api/source/pull-request/checks", source.api_pull_request_checks)
    app.router.add_post("/api/source/pull-request/status", source.api_pull_request_status)
    app.router.add_post("/api/source/pull-request/resolve", source.api_pull_request_resolve)
    app.router.add_post("/api/source/pull-request/unresolve", source.api_pull_request_unresolve)
    app.router.add_post("/api/source/pull-request/reply", source.api_pull_request_reply)
    app.router.add_post("/api/source/pull-request/comment", source.api_pull_request_comment)
    app.router.add_post("/api/source/pull-request/auto-merge", source.api_pull_request_auto_merge)
    app.router.add_post("/api/source/pull-request/ready", source.api_pull_request_ready)
    app.router.add_post("/api/source/issue", source.api_issue_source)
    app.router.add_post("/api/source/contributors", source.api_app_contributors)
    return app


@pytest.fixture(autouse=True)
def _clear_issue_cache():
    source._ISSUE_CACHE.clear()
    source._ISSUE_FETCH_INFLIGHT.clear()
    source._ISSUE_FETCH_TASKS.clear()
    yield
    source._ISSUE_CACHE.clear()
    source._ISSUE_FETCH_INFLIGHT.clear()
    source._ISSUE_FETCH_TASKS.clear()


# An issue URL: every pull-request-only entry point must refuse it.
_ISSUE_URL = "https://github.com/acme/repo/issues/12"
