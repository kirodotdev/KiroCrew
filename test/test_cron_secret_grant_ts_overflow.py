"""``PUT /api/crons/{id}/secrets`` answers 400 for an ``expected_ts`` no float can hold.

JSON integers have no width limit, and ``float()`` of a 400-digit one raises
``OverflowError``. The approve and deny paths both converted ``expected_ts``
with ``float()``, so such a body answered 500. The pending request stays as it
was, and an ordinary timestamp is handled as before.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cron_secret_env_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _grant_script,
    cron_home_fixture,
)

from kiro_crew.config.loader import config_dir as loader_config_dir
from kiro_crew.cron import CronService
from kiro_crew.cron_script import compute_secret_env_pin, delivery_fingerprint
from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

_HUGE = 10**400
_MAPPING = {"MY_TOKEN": "slack-sandbox"}


@pytest.fixture(autouse=True)
def _owner_view(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _app(svc: CronService) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(crons=svc, push_refresh=lambda *a, **k: None)
    app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
    return app


def _pending_job(svc: CronService, cron_home) -> str:
    spec = _grant_script(cron_home)
    job = svc.add_job(
        "j", "args", every_secs=3600, script=spec, session_key="dashboard:conftest-slot"
    )
    pin = compute_secret_env_pin(
        spec,
        "",
        "args",
        job_id=job.id,
        grant=_MAPPING,
        domain="pending",
        delivery=delivery_fingerprint("dashboard:conftest-slot"),
    )
    svc.update_job(
        job.id,
        secret_env_pending=dict(_MAPPING),
        secret_env_pending_pin=pin,
        secret_env_pending_ts=1.0,
    )
    return job.id


async def _put(svc: CronService, job_id: str, body: str) -> tuple[int, str]:
    with patch("kiro_crew.dashboard.handlers.cron._sel"):
        async with TestClient(TestServer(_app(svc))) as client:
            resp = await client.put(
                f"/api/crons/{job_id}/secrets",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            return resp.status, await resp.text()


def _body(flag: str, ts: object) -> str:
    # json.dumps writes the big integer out in full, as a client would send it.
    return json.dumps(
        {
            flag: True,
            "expected_secret_env": _MAPPING,
            "expected_ts": ts,
            "expected_source_sha256": "0" * 64,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["approve_pending", "deny_pending"])
async def test_an_unrepresentable_expected_ts_is_a_400(cron_home, flag: str) -> None:
    svc = CronService(base_dir=loader_config_dir())
    job_id = _pending_job(svc, cron_home)

    status, text = await _put(svc, job_id, _body(flag, _HUGE))

    assert status == 400, text
    assert json.loads(text)["code"] == "invalid_secret_env"
    job = CronService(base_dir=svc._dir).get_job(job_id)
    assert job is not None
    assert job.secret_env_pending == _MAPPING


@pytest.mark.asyncio
async def test_a_deny_with_the_displayed_ts_still_clears_the_request(cron_home) -> None:
    svc = CronService(base_dir=loader_config_dir())
    job_id = _pending_job(svc, cron_home)

    status, text = await _put(svc, job_id, _body("deny_pending", 1.0))

    assert status == 200, text
    job = CronService(base_dir=svc._dir).get_job(job_id)
    assert job is not None
    assert job.secret_env_pending == {}
