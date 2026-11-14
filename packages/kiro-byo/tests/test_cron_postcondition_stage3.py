"""Native-Cron postconditions and switch detection, without changing Mode/Type.

The checks detect persistence failures and a raced backend switch. They cannot
provide true cross-UI CAS without support from Crew's native Cron transaction.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from test_crew_linkage import live_linkage_routes


def result(response):
    return response.status, json.loads(response.text)


def job(*, model="", script="", persistent=True, minimal=False):
    return SimpleNamespace(
        id="example", model=model, script=script, command="",
        persistent_session=persistent, minimal_context=minimal,
    )


def test_model_update_reply_is_not_proof_of_native_persistence(live_linkage_routes):
    host = live_linkage_routes
    current = job()
    req = host.request({"model": "byo-model", "expected_model": ""})

    async def jobs(**kwargs):
        return [current]

    async def fake_update(job_id, *, model):
        # Crew produced a plausible response but its store was never changed.
        return job(model=model)

    req.app["state"].crons.list_jobs_async = jobs
    req.app["state"].crons.update_job_async = fake_update
    status, payload = result(asyncio.run(host.routes.update_task_model(req, None)))
    assert status == 400 and "mismatch" in payload["error"]
    assert current.model == ""


def test_context_update_reply_is_not_proof_of_native_persistence(live_linkage_routes):
    host = live_linkage_routes
    current = job()
    req = host.request({
        "persistent_session": False, "minimal_context": True,
        "expected_persistent_session": True, "expected_minimal_context": False,
    })

    async def jobs(**kwargs):
        return [current]

    async def fake_update(job_id, **kwargs):
        return job(persistent=False, minimal=True)

    req.app["state"].crons.list_jobs_async = jobs
    req.app["state"].crons.update_job_async = fake_update
    status, payload = result(asyncio.run(host.routes.update_task_execution(req, None)))
    assert status == 400 and "mismatch" in payload["error"]
    assert current.persistent_session is True and current.minimal_context is False


def test_script_cron_context_does_not_write(live_linkage_routes):
    host = live_linkage_routes
    req = host.request({"persistent_session": False, "minimal_context": True})
    calls = []

    async def jobs(**kwargs):
        return [job(script="script.py:run")]

    async def fake_update(job_id, **kwargs):
        calls.append(kwargs)
        return job()

    req.app["state"].crons.list_jobs_async = jobs
    req.app["state"].crons.update_job_async = fake_update
    status, payload = result(asyncio.run(host.routes.update_task_execution(req, None)))
    assert status == 400 and "Script/Command" in payload["error"]
    assert calls == []


def test_mode_switch_during_cron_model_update_is_reported(live_linkage_routes):
    host = live_linkage_routes
    current = job()
    req = host.request({"model": "byo-model", "expected_model": ""})

    async def jobs(**kwargs):
        return [current]

    async def write(job_id, **kwargs):
        current.model = kwargs["model"]
        host.document["agent"]["acp_backend"] = "kas"
        return current

    req.app["state"].crons.list_jobs_async = jobs
    req.app["state"].crons.update_job_async = write
    status, payload = result(asyncio.run(host.routes.update_task_model(req, None)))
    assert status == 400 and "may already have been saved" in payload["error"]
    assert current.model == "byo-model"


def test_mode_switch_during_context_update_is_reported(live_linkage_routes):
    host = live_linkage_routes
    current = job()
    req = host.request({
        "persistent_session": False, "minimal_context": True,
        "expected_persistent_session": True, "expected_minimal_context": False,
    })

    async def jobs(**kwargs):
        return [current]

    async def write(job_id, **kwargs):
        current.persistent_session = kwargs["persistent_session"]
        current.minimal_context = kwargs["minimal_context"]
        host.document["agent"]["acp_backend"] = "kas"
        return current

    req.app["state"].crons.list_jobs_async = jobs
    req.app["state"].crons.update_job_async = write
    status, payload = result(asyncio.run(host.routes.update_task_execution(req, None)))
    assert status == 400 and "may already have been saved" in payload["error"]


def test_missing_task_after_native_update_is_not_success(live_linkage_routes):
    host = live_linkage_routes
    req = host.request({"model": "byo-model", "expected_model": ""})
    current = [job()]

    async def jobs(**kwargs):
        return current

    async def write(job_id, **kwargs):
        current.clear()
        return job(model=kwargs["model"])

    req.app["state"].crons.list_jobs_async = jobs
    req.app["state"].crons.update_job_async = write
    status, payload = result(asyncio.run(host.routes.update_task_model(req, None)))
    assert status == 400 and "vanished" in payload["error"]
