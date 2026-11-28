"""An app-owned session keeps reach over the cron jobs its own session created.

The cron MCP server's ``cron_add`` stores the calling session as the job's
``session_key`` and stamps no ``created_by``. Its trigger and history requests name
that same session, so the gateway derives the session's app and holds the call to
``_refuse_foreign_app_job``. These tests pin that the gate admits such a job for an
internal caller naming that session, and still refuses every other unstamped job.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Through the facade: these names are bound by more than one cron handler module.
from kiro_crew.dashboard.handlers import cron as cron_shared

pytestmark = pytest.mark.asyncio

APP = "app-a"


class _Request(dict):
    """The two things the gate reads: request state and headers."""

    def __init__(self, *, headers: dict[str, str], **state: Any) -> None:
        super().__init__(state)
        self.headers = headers


def _state(jobs: dict[str, SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        crons=SimpleNamespace(get_job=jobs.get, get_job_async=AsyncMock(side_effect=jobs.get))
    )


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(cron_shared, "_audit_app_cron", lambda *a, **k: None)
    _floor_monkeypatch.setattr(cron_shared, "_owner_denial_response", lambda _r: "refused")


_JOBS = {
    "own": SimpleNamespace(created_by="", session_key="dashboard:app-slot"),
    "other": SimpleNamespace(created_by="", session_key="dashboard:owner-slot"),
    "stamped": SimpleNamespace(created_by=f"app:{APP}", session_key="cron:x"),
}


async def test_a_job_the_calling_session_created_is_admitted() -> None:
    req = _Request(headers={"X-Session-Key": "dashboard:app-slot"}, app=APP, internal_auth=True)
    assert await cron_shared._refuse_foreign_app_job(req, _state(_JOBS), ["own"], "op") is None


async def test_another_sessions_unstamped_job_is_still_refused() -> None:
    req = _Request(headers={"X-Session-Key": "dashboard:app-slot"}, app=APP, internal_auth=True)
    refused = await cron_shared._refuse_foreign_app_job(req, _state(_JOBS), ["own", "other"], "op")
    assert refused == "refused"


async def test_an_app_token_gains_nothing_from_a_session_header() -> None:
    # No internal grant: an app token's header is not a session the gateway named.
    req = _Request(headers={"X-Session-Key": "dashboard:app-slot"}, app=APP)
    refused = await cron_shared._refuse_foreign_app_job(req, _state(_JOBS), ["own"], "op")
    assert refused == "refused"


async def test_the_stamp_still_admits_without_a_session() -> None:
    req = _Request(headers={}, app=APP)
    assert await cron_shared._refuse_foreign_app_job(req, _state(_JOBS), ["stamped"], "op") is None


async def test_a_missing_job_is_refused() -> None:
    req = _Request(headers={"X-Session-Key": "dashboard:app-slot"}, app=APP, internal_auth=True)
    refused = await cron_shared._refuse_foreign_app_job(req, _state(_JOBS), ["gone"], "op")
    assert refused == "refused"


@pytest.mark.parametrize(
    "cached", [None, _JOBS["other"], _JOBS["own"]], ids=["missing", "nonmatching", "matching"]
)
@pytest.mark.parametrize("fresh", [_JOBS["own"], _JOBS["other"]], ids=["owned", "transferred"])
@pytest.mark.parametrize("internal_auth", [True, False], ids=["internal", "app-token"])
async def test_only_an_internal_caller_checks_fresh_session_ownership(
    cached: SimpleNamespace | None, fresh: SimpleNamespace, internal_auth: bool
) -> None:
    req = _Request(
        headers={"X-Session-Key": "dashboard:app-slot"}, app=APP, internal_auth=internal_auth
    )
    state = _state({"own": cached} if cached is not None else {})
    state.crons.get_job_async = AsyncMock(return_value=fresh)

    refused = await cron_shared._refuse_foreign_app_job(req, state, ["own"], "op")

    if internal_auth:
        assert refused == (None if fresh.session_key == "dashboard:app-slot" else "refused")
        state.crons.get_job_async.assert_awaited_once_with("own")
    else:
        assert refused == "refused"
        state.crons.get_job_async.assert_not_called()
