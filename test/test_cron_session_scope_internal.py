"""Session-scoped cron ownership for the supervised internal-secret caller.

The supervising Kiro CLI creates/lists/removes crons on behalf of a `/crew cron`
subcommand over the internal-secret transport. Those jobs must be OWNED by the
calling session (its ``X-Session-Key``) so ``cron/list`` shows a session only
its own jobs and ``cron/removeAll`` never reaches another session's — the same
ownership the MCP cron tools enforce, now applied server-side for the REST
caller the KAS bridge uses.

The browser/dashboard path (no ``X-Internal-Secret``, no ``internal_auth``) must
stay byte-identical: it sets no owner on create, sees every job on list, and
still requires an explicit ``ids`` array to batch-delete.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.handlers.cron import (
    api_cron_batch_delete,
    api_crons,
    api_crons_create,
)
from test.body_stream_helpers import attach_body

pytestmark = pytest.mark.asyncio

OWNER = "cron:owner-session"
OTHER = "cron:other-session"


def _job(**over) -> CronJob:
    fields = {"id": "j1", "name": "poller", "message": "Check it."}
    fields.update(over)
    return CronJob(**fields)


def _create_request(*, internal_auth, session_key: str | None, body: dict) -> MagicMock:
    """A mock request for api_crons_create with controlled auth + headers."""
    add = AsyncMock(return_value=_job())
    state = SimpleNamespace(
        crons=SimpleNamespace(add_job_async=add),
        push_refresh=MagicMock(),
    )
    request = MagicMock()
    request.app = {"state": state}
    request.get = lambda k, d=None: internal_auth if k == "internal_auth" else d
    request.headers = {"X-Session-Key": session_key} if session_key is not None else {}
    # api_crons_create reads its body via read_bounded_json's capped path, which
    # drains request.content incrementally (NOT request.json / request.read), so
    # feed a real body stream through the shared harness helper.
    attach_body(request, body)
    return request, add


def _list_request(jobs, *, internal_auth, session_key: str | None) -> MagicMock:
    state = MagicMock()
    state.has_slot.return_value = False
    state.crons.list_jobs_async = AsyncMock(return_value=list(jobs))
    state.crons.is_running.return_value = False
    state.crons.running_since.return_value = None
    request = MagicMock()
    request.app = {"state": state}
    request.get = lambda k, d=None: internal_auth if k == "internal_auth" else d
    request.headers = {"X-Session-Key": session_key} if session_key is not None else {}
    return request


# -- POST /api/crons: create records the owning session ------------------------


async def test_internal_create_records_x_session_key_as_owner() -> None:
    req, add = _create_request(
        internal_auth=True,
        session_key=OWNER,
        body={"name": "poller", "message": "Check it.", "every": 3600},
    )
    resp = await api_crons_create(req)
    assert resp.status == 200
    assert add.await_args.kwargs["session_key"] == OWNER


async def test_internal_create_without_header_leaves_owner_unset() -> None:
    req, add = _create_request(
        internal_auth=True,
        session_key=None,
        body={"name": "poller", "message": "Check it.", "every": 3600},
    )
    resp = await api_crons_create(req)
    assert resp.status == 200
    # No session_key kwarg forwarded -> store default (ownerless), unchanged.
    assert "session_key" not in add.await_args.kwargs


async def test_browser_create_does_not_set_owner_from_header() -> None:
    # A browser POST is not internal_auth; even a spoofed header is ignored.
    req, add = _create_request(
        internal_auth=None,
        session_key=OTHER,
        body={"name": "poller", "message": "Check it.", "every": 3600},
    )
    resp = await api_crons_create(req)
    assert resp.status == 200
    assert "session_key" not in add.await_args.kwargs


# -- GET /api/crons: list is scoped to the owner for an internal caller --------


async def test_internal_list_returns_only_owned_jobs() -> None:
    jobs = [
        _job(id="a", session_key=OWNER),
        _job(id="b", session_key=OTHER),
        _job(id="c", session_key=OWNER),
        _job(id="d", session_key=""),  # ownerless: invisible to a scoped caller
    ]
    req = _list_request(jobs, internal_auth=True, session_key=OWNER)
    resp = await api_crons(req)
    ids = [j["id"] for j in json.loads(resp.body)["jobs"]]
    assert ids == ["a", "c"]


async def test_internal_list_without_header_owns_nothing() -> None:
    jobs = [_job(id="a", session_key=OWNER), _job(id="b", session_key=OTHER)]
    req = _list_request(jobs, internal_auth=True, session_key=None)
    resp = await api_crons(req)
    assert json.loads(resp.body)["jobs"] == []


async def test_browser_list_is_unfiltered() -> None:
    jobs = [_job(id="a", session_key=OWNER), _job(id="b", session_key=OTHER)]
    req = _list_request(jobs, internal_auth=None, session_key=None)
    resp = await api_crons(req)
    ids = sorted(j["id"] for j in json.loads(resp.body)["jobs"])
    assert ids == ["a", "b"]


# -- DELETE /api/crons: session-scoped remove-all ------------------------------


def _delete_request(jobs, *, internal_auth, session_key, body):
    remove_jobs = AsyncMock(side_effect=lambda ids, **kw: (list(ids), []))
    history = SimpleNamespace(delete_job_history=AsyncMock())
    state = SimpleNamespace(
        crons=SimpleNamespace(
            list_jobs_async=AsyncMock(return_value=list(jobs)),
            remove_jobs=remove_jobs,
            get_history=lambda: history,
        ),
        push_refresh=MagicMock(),
    )
    request = MagicMock()
    request.app = {"state": state}
    request.get = lambda k, d=None: internal_auth if k == "internal_auth" else d
    request.headers = {"X-Session-Key": session_key} if session_key is not None else {}
    # api_cron_batch_delete reads its body via read_bounded_json's capped path
    # (drains request.content), so feed a real body stream, not request.read.
    attach_body(request, body)
    return request, remove_jobs


async def test_internal_remove_all_scopes_to_owned_jobs() -> None:
    jobs = [
        _job(id="a", session_key=OWNER),
        _job(id="b", session_key=OTHER),
        _job(id="c", session_key=OWNER),
    ]
    req, remove_jobs = _delete_request(jobs, internal_auth=True, session_key=OWNER, body={})
    resp = await api_cron_batch_delete(req)
    assert resp.status == 200
    assert sorted(json.loads(resp.body)["deleted"]) == ["a", "c"]
    # Only the owned ids were handed to the store.
    assert sorted(remove_jobs.await_args.args[0]) == ["a", "c"]


async def test_internal_remove_all_with_no_owned_jobs_is_a_noop_not_a_wipe() -> None:
    jobs = [_job(id="b", session_key=OTHER)]
    req, remove_jobs = _delete_request(jobs, internal_auth=True, session_key=OWNER, body={})
    resp = await api_cron_batch_delete(req)
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": False, "deleted": [], "failed": []}
    remove_jobs.assert_not_awaited()


async def test_internal_remove_all_without_header_deletes_nothing() -> None:
    jobs = [_job(id="a", session_key=OWNER)]
    req, remove_jobs = _delete_request(jobs, internal_auth=True, session_key=None, body={})
    resp = await api_cron_batch_delete(req)
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": False, "deleted": [], "failed": []}
    remove_jobs.assert_not_awaited()


async def test_browser_delete_still_requires_explicit_ids() -> None:
    # No internal_auth: an empty body is a 400, not a session-scoped wipe.
    req, remove_jobs = _delete_request([], internal_auth=None, session_key=None, body={})
    resp = await api_cron_batch_delete(req)
    assert resp.status == 400
    remove_jobs.assert_not_awaited()


async def test_explicit_ids_path_unchanged_for_internal_caller() -> None:
    # An internal caller CAN still pass explicit ids; scoping only kicks in when
    # ids is absent.
    jobs = [_job(id="a", session_key=OWNER)]
    req, remove_jobs = _delete_request(
        jobs, internal_auth=True, session_key=OWNER, body={"ids": ["x", "y"]}
    )
    resp = await api_cron_batch_delete(req)
    assert resp.status == 200
    assert sorted(remove_jobs.await_args.args[0]) == ["x", "y"]
