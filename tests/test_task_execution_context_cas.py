"""Task execution context concurrency checks, independent of Mode/Type routing.

The App uses Crew's native Cron storage. These checks fence BYO-originated
stale writes, not independent Crew dashboard writes occurring after the read.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from test_crew_linkage import live_linkage_routes


def unpack(response):
    return response.status, json.loads(response.text)


def make_job(*, persistent=True, minimal=False):
    return SimpleNamespace(id='example', model='', script='', command='',
                           persistent_session=persistent, minimal_context=minimal)


def test_context_stale_after_native_crew_edit_is_rejected(live_linkage_routes):
    host = live_linkage_routes
    current = make_job(persistent=False, minimal=False)
    calls = []

    async def jobs(**kwargs):
        return [current]

    async def write(job_id, **kwargs):
        calls.append(kwargs)
        return current

    request = host.request({
        'persistent_session': True, 'minimal_context': True,
        'expected_persistent_session': True,
        'expected_minimal_context': False,
    })
    request.app['state'].crons.list_jobs_async = jobs
    request.app['state'].crons.update_job_async = write
    status, payload = unpack(asyncio.run(host.routes.update_task_execution(request, None)))
    assert status == 400 and 'changed' in payload['error']
    assert calls == []
    assert current.persistent_session is False


def test_context_current_snapshot_can_write_with_original_boolean_flags(live_linkage_routes):
    host = live_linkage_routes
    current = make_job()

    async def jobs(**kwargs):
        return [current]

    async def write(job_id, **kwargs):
        current.persistent_session = kwargs['persistent_session']
        current.minimal_context = kwargs['minimal_context']
        return current

    request = host.request({
        'persistent_session': False, 'minimal_context': True,
        'expected_persistent_session': True,
        'expected_minimal_context': False,
    })
    request.app['state'].crons.list_jobs_async = jobs
    request.app['state'].crons.update_job_async = write
    status, payload = unpack(asyncio.run(host.routes.update_task_execution(request, None)))
    assert status == 200, payload
    assert payload['task']['persistent_session'] is False
    assert payload['task']['minimal_context'] is True


def test_context_expected_flags_must_be_boolean(live_linkage_routes):
    host = live_linkage_routes
    for bad in ('false', 0, None, [], {}):
        request = host.request({
            'persistent_session': False, 'minimal_context': False,
            'expected_persistent_session': bad,
        })
        status, payload = unpack(asyncio.run(host.routes.update_task_execution(request, None)))
        assert status == 400 and 'must be boolean' in payload['error']


def test_concurrent_byo_context_writes_refuse_second_stale_writer(live_linkage_routes):
    host = live_linkage_routes
    current = make_job()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def jobs(**kwargs):
        return [current]

    async def write(job_id, **kwargs):
        if kwargs['minimal_context']:
            entered.set()
            await release.wait()
        current.persistent_session = kwargs['persistent_session']
        current.minimal_context = kwargs['minimal_context']
        return current

    def request(persistent, minimal):
        req = host.request({
            'persistent_session': persistent, 'minimal_context': minimal,
            'expected_persistent_session': True,
            'expected_minimal_context': False,
        })
        req.app['state'].crons.list_jobs_async = jobs
        req.app['state'].crons.update_job_async = write
        return req

    async def scenario():
        first = asyncio.create_task(host.routes.update_task_execution(request(True, True), None))
        await entered.wait()
        second = asyncio.create_task(host.routes.update_task_execution(request(False, False), None))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert unpack(first)[0] == 200
    status, payload = unpack(second)
    assert status == 400 and 'changed' in payload['error']
    assert current.persistent_session is True and current.minimal_context is True


def test_context_rejects_non_byo_mode_even_with_matching_expectations(live_linkage_routes):
    host = live_linkage_routes
    host.document['agent']['acp_backend'] = 'kas'
    request = host.request({
        'persistent_session': True, 'minimal_context': False,
        'expected_persistent_session': True, 'expected_minimal_context': False,
    })
    status, payload = unpack(asyncio.run(host.routes.update_task_execution(request, None)))
    assert status == 400 and 'Harness' in payload['error']
