"""One-page task console and Crew-source-of-truth regression contracts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kiro_byo.crew_linkage import background_execution_snapshot
from test_crew_linkage import live_linkage_routes

ROOT = Path(__file__).resolve().parents[1]


def _body(response):
    return response.status, json.loads(response.text)


def test_background_execution_never_confuses_a_route_with_success():
    rows = [
        {"selection_source": "background_auto", "outcome": "error", "error_kind": "AttributeError", "error_site": "worker.py:77:run",
         "timestamp": 123, "provider_id": "xiaomi", "served_model": "mimo-v2.6-flash", "input_tokens": 0,
         "output_tokens": 0, "total_ms": 21500},
        {"selection_source": "user_set_config", "outcome": "success", "served_model": "foreign"},
    ]
    report = background_execution_snapshot(rows)
    assert report["failed_recent"] == 1
    row = report["latest"]
    assert row["recorded_by"] == "kiro-byo"
    assert row["error_kind"] == "AttributeError"
    assert row["error_site"] == "worker.py:77:run"
    assert row["served_model"] == "mimo-v2.6-flash"
    assert row["upstream_verified"] is False
    assert len(report["recent"]) == 1
    assert background_execution_snapshot([])["latest"] is None


def test_task_console_ui_does_not_duplicate_chat_or_member_picker():
    js = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    chunk = js.split("function TasksPage(", 1)[1].split("function RecordsPage(", 1)[0]
    assert "chatCard" not in chunk
    assert "membersCard" not in chunk
    assert "previewSwitch" not in chunk
    assert "task_console.background" in chunk
    assert "task_console.schedules" in chunk
    assert "task_console.recent" in chunk
    assert "expected_model:t.model" in chunk
    assert "expected_model:background.model" in chunk
    assert "onNavigate?.('records')" in chunk
    assert "12000" in chunk


def test_stale_background_selection_cannot_overwrite_native_crew(live_linkage_routes):
    host = live_linkage_routes
    code, body = _body(asyncio.run(host.routes.update_background_model(
        host.request({"model": "byo-model", "expected_model": "different"}), None
    )))
    assert code == 400
    assert "changed" in body["error"]
    assert host.document["agent"]["role_models"]["background"] == "auto"
    code, body = _body(asyncio.run(host.routes.update_background_model(
        host.request({"model": "byo-model", "expected_model": "auto"}), None
    )))
    assert code == 200, body
    assert host.document["agent"]["role_models"]["background"] == "byo-model"
    assert body["background"]["verified"] is True


def test_stale_schedule_selection_cannot_overwrite_crew(live_linkage_routes):
    from types import SimpleNamespace
    host = live_linkage_routes
    request = host.request({"model": "byo-model", "expected_model": "old"})
    async def jobs(**kwargs):
        return [SimpleNamespace(id="example", model="new", script="", command="")]
    request.app["state"].crons.list_jobs_async = jobs
    code, body = _body(asyncio.run(host.routes.update_task_model(request, None)))
    assert code == 400
    assert "changed" in body["error"]


def test_official_mode_guards_context_and_auto_model(live_linkage_routes):
    host = live_linkage_routes
    host.document["agent"]["acp_backend"] = "kas"
    for method, payload in (
        (host.routes.update_background_model, {"model": "auto"}),
        (host.routes.update_task_model, {"model": ""}),
        (host.routes.update_task_execution, {"persistent_session": True, "minimal_context": False}),
    ):
        code, _ = _body(asyncio.run(method(host.request(payload), None)))
        assert code == 400


def test_telemetry_only_reports_byo_background_auto(live_linkage_routes, monkeypatch):
    host = live_linkage_routes
    class Store:
        def recent(self, limit):
            assert limit == 30
            return [
                {"selection_source": "user_set_config", "outcome": "success", "served_model": "chat"},
                {"selection_source": "background_auto", "outcome": "error", "error_kind": "AttributeError",
                 "provider_id": "xiaomi", "served_model": "mimo-v2.6-flash"},
            ]
    monkeypatch.setattr(host.routes, "TurnTelemetryStore", Store)
    code, payload = _body(asyncio.run(host.routes.get_tasks(host.request(), None)))
    assert code == 200, payload
    assert payload["background"]["model"] == "auto"
    assert payload["execution"]["latest"]["error_kind"] == "AttributeError"
    assert len(payload["execution"]["recent"]) == 1
    assert payload["config_source"] == "kirocrew_native"
    assert payload["main_backend"] == "kiro-byo"


def test_explicit_background_role_is_tracked_with_pinned_concrete_model():
    rows = [{"is_background_role": True, "selection_source": "host_session_new",
             "outcome": "success", "served_model": "pinned", "provider_id": "custom"},
            {"is_background_role": False, "selection_source": "user_set_config",
             "outcome": "success", "served_model": "chat"}]
    report = background_execution_snapshot(rows)
    assert len(report["recent"]) == 1
    assert report["latest"]["served_model"] == "pinned"
    assert report["latest"]["upstream_verified"] is True


def test_locked_crew_mutations_refuse_switch_to_official(live_linkage_routes, monkeypatch):
    """The main backend may change after BYO preflight but before locking."""
    import sys
    from kiro_crew.config import loader

    host = live_linkage_routes
    host.document['agents'] = {
        'Gridman': {'member_id': 'a', 'model': '', 'worker_model': ''},
    }
    before_background = host.document['agent']['role_models']['background']
    actual_update = loader.update_config_locked

    def change_harness_before_lock(path, *, mutate):
        host.document['agent']['acp_backend'] = 'kas'
        return actual_update(path, mutate=mutate)

    monkeypatch.setattr(loader, 'update_config_locked', change_harness_before_lock)
    for handler, payload in (
        (host.routes.update_chat_model, {'model': 'byo-model'}),
        (host.routes.update_member_model, {'model': 'byo-model', 'field': 'model'}),
        (host.routes.update_background_model, {'model': 'byo-model', 'expected_model': 'auto'}),
    ):
        host.document['agent']['acp_backend'] = 'kiro-byo'
        code, body = _body(asyncio.run(handler(host.request(payload), None)))
        assert code == 400, (handler.__name__, body)
        assert 'changed' in body['error']
        assert host.document['agent']['acp_backend'] == 'kas'
        assert host.document['agent']['model'] == 'chat-model'
        assert host.document['agents']['Gridman']['model'] == ''
        assert host.document['agent']['role_models']['background'] == before_background


def test_local_harness_override_rejected_even_if_base_is_byo(live_linkage_routes, tmp_path):
    host = live_linkage_routes
    # A higher-priority override makes the base config's main backend unsafe
    # to rely on, even if the synthetic preflight returns BYO.
    (tmp_path / 'config.local.json').write_text(
        json.dumps({'agent': {'acp_backend': 'kas'}}), encoding='utf-8'
    )
    code, body = _body(asyncio.run(host.routes.update_background_model(
        host.request({'model': 'byo-model'}), None
    )))
    assert code == 400
    assert 'config.local.json' in body['error']
    assert host.document['agent']['role_models']['background'] == 'auto'


def test_no_provider_hides_unrunnable_mode_type_in_tasks(live_linkage_routes, monkeypatch):
    from types import SimpleNamespace

    host = live_linkage_routes
    policy = SimpleNamespace(id='multi', display_name='Multi Type')
    pack = SimpleNamespace(display_name='Tiga', policies=[policy])
    plugin = SimpleNamespace(id='tiga', enabled=True, pack=pack)
    registry = SimpleNamespace(get=lambda name: plugin if name == 'tiga' else None)
    monkeypatch.setattr(host.routes, 'load_mode_registry', lambda: registry)
    empty = SimpleNamespace(mode_pack='tiga', public_models=[])
    monkeypatch.setattr(host.routes, 'load_config', lambda store: empty)
    assert host.routes._selection_rows(empty) == []
    assert host.routes._selection_ids(empty) == set()
    request = host.request({'model': 'kiro-byo-type:tiga:multi'})
    code, data = _body(asyncio.run(host.routes.update_background_model(request, None)))
    assert code == 400
    assert 'unknown' in data['error']

    # Once one concrete model becomes available, its Mode Types reappear.
    ready = SimpleNamespace(
        mode_pack='tiga', public_models=[('p/model', SimpleNamespace(name='M'))]
    )
    assert [row['kind'] for row in host.routes._selection_rows(ready)] == ['type', 'concrete']


def test_concurrent_byo_cron_edits_reject_second_stale_writer(live_linkage_routes, monkeypatch):
    from types import SimpleNamespace

    host = live_linkage_routes
    monkeypatch.setattr(host.routes, '_selection_ids', lambda _config: {'model-a', 'model-b'})
    current = {'model': ''}
    first_in_write = asyncio.Event()
    release_first = asyncio.Event()

    async def jobs(**kwargs):
        return [SimpleNamespace(id='example', model=current['model'], script='', command='')]

    async def update(job_id, *, model):
        if model == 'model-a':
            first_in_write.set()
            await release_first.wait()
        current['model'] = model
        return SimpleNamespace(id=job_id, model=model)

    request_a = host.request({'model': 'model-a', 'expected_model': ''})
    request_b = host.request({'model': 'model-b', 'expected_model': ''})
    for request in (request_a, request_b):
        request.app['state'].crons.list_jobs_async = jobs
        request.app['state'].crons.update_job_async = update

    async def scenario():
        first = asyncio.create_task(host.routes.update_task_model(request_a, None))
        await first_in_write.wait()
        second = asyncio.create_task(host.routes.update_task_model(request_b, None))
        await asyncio.sleep(0)
        release_first.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    code_a, _ = _body(first)
    code_b, data_b = _body(second)
    assert code_a == 200
    assert code_b == 400
    assert 'changed' in data_b['error']
    assert current['model'] == 'model-a'
