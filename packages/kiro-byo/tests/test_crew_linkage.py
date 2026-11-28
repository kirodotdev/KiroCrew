"""Platform-independent Crew linkage contract (no live Crew data altered)."""
from __future__ import annotations

from types import SimpleNamespace
import pytest

from kiro_byo.crew_linkage import (
    backend_id, role_pin, role_resolution, linkage_snapshot,
    check_member_sync, model_pin_allowed, linkage_change_plan,
)


def _agent(*, main="kiro-byo", member="kas", bg="auto", sub="sub-model", default="chat-model"):
    class Agent:
        acp_backend = main
        member_acp_backend = member
        role_models = {"background": bg, "subagent": sub}
        def resolve_model(self, role):
            return default if self.role_models.get(role, "auto") == "auto" else self.role_models[role]
    return Agent()


def test_linkage_reports_only_configured_defaults_not_claimed_runtime():
    status = linkage_snapshot(_agent())
    assert status["main"]["configured_backend"] == "kiro-byo"
    assert status["members"]["configured_backend"] == "kas"
    assert status["members"]["follows_main"] is False
    assert status["cron"]["runtime_backend"] == "kiro-byo"
    assert status["workflow"]["runtime_backend"] == "kiro-byo"
    assert status["active_sessions"]["runtime_backend"] == "not_inspected"
    assert status["follow_policy"] == "not_installed"


def test_role_pin_vs_effective_inherited_model():
    a = _agent(bg="auto", default="example-chat-model")
    assert role_pin(a, "background") == "auto"
    assert role_resolution(a, "background") == {
        "configured_model": "auto",
        "resolved_model": "example-chat-model",
        "selection": "inherited",
    }
    assert role_resolution(a, "subagent")["selection"] == "pinned"


def test_legacy_or_absent_fields_do_not_invent_member_or_running_state():
    status = linkage_snapshot(SimpleNamespace(acp_backend=""))
    assert status["main"]["configured_backend"] == "kiro"
    assert status["members"]["configured_backend"] == "unknown"
    assert status["background"]["resolved_model"] == "unknown"
    assert backend_id("") == "kiro"


@pytest.mark.parametrize("expected_main,expected_member,actual_main,actual_member", [
    ("kas", "kas", "kiro-byo", "kas"),
    ("kiro-byo", "kas", "kiro-byo", "kiro"),
    ("kiro-byo", "kas", "unknown", "kas"),
    ("", "kas", "kiro-byo", "kas"),
])
def test_member_sync_rejects_stale_or_unknown(expected_main,expected_member,actual_main,actual_member):
    with pytest.raises(ValueError):
        check_member_sync(expected_main,expected_member,actual_main,actual_member)


def test_member_sync_returns_explicit_main_without_changing_other_fields():
    assert check_member_sync("kiro-byo", "kas", "kiro-byo", "kas") == "kiro-byo"


def test_concrete_model_pin_only_in_byo_context_and_clearing_always_available():
    known = {"model-a"}
    for backend in ["kas", "kiro", "unknown"]:
        assert not model_pin_allowed(selected_backend=backend, model="model-a", byo_models=known)
        assert model_pin_allowed(selected_backend=backend, model="", byo_models=known)
        assert model_pin_allowed(selected_backend=backend, model="auto", byo_models=known)
    assert model_pin_allowed(selected_backend="kiro-byo", model="model-a", byo_models=known)
    assert not model_pin_allowed(selected_backend="kiro-byo", model="not-listed", byo_models=known)


def test_linkage_ui_routes_translations_and_no_global_force_switch():
    from pathlib import Path
    import json
    root = Path(__file__).resolve().parents[1]
    ui = (root / "ui/index.mjs").read_text(encoding="utf-8")
    routes = (root / "backend/routes.py").read_text(encoding="utf-8")
    # Legacy linkage endpoints remain for compatibility, but this simplified
    # one-page UI must NOT offer a second global Harness/member switcher.
    assert "api('/tasks')" in ui
    for url in ("/tasks/linkage", "/tasks/linkage/member/sync", "/tasks/linkage/preview", "/tasks/linkage/apply"):
        assert url in routes and url not in ui
    assert "get_task_linkage" in routes and "sync_member_backend" in routes
    assert "follow_policy" not in ui
    assert "task_console.schedules" in ui and "task_console.background" in ui
    for locale in ("zh-CN", "zh-TW", "ja", "en", "ko"):
        doc = json.loads((root / "src/kiro_byo/locales" / f"{locale}.json").read_text(encoding="utf-8"))
        for key in ("linkage.title", "linkage.sync_member", "linkage.crew_resolved", "linkage.preview_button", "linkage.confirm_apply", "linkage.member_fixed"):
            assert doc.get(key)


@pytest.fixture
def live_linkage_routes(monkeypatch, tmp_path):
    """Exercise the actual aiohttp handlers against a synthetic host.

    This runs on Linux/macOS/Windows without installing or editing KiroCrew.
    """
    import importlib.util
    import json
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pkg = "_kiro_byo_linkage_host_test"
    root_pkg = types.ModuleType(pkg)
    root_pkg.__path__ = [str(root)]
    backend_pkg = types.ModuleType(pkg + ".backend")
    backend_pkg.__path__ = [str(root / "backend")]
    fake_kiro = types.ModuleType("kiro_crew")
    fake_kiro.__path__ = []
    fake_apps = types.ModuleType("kiro_crew.apps")
    fake_apps.__path__ = []
    fake_rr = types.ModuleType("kiro_crew.apps.route_registry")
    fake_rr.AppRoute = lambda *args, **kwargs: (args, kwargs)
    fake_config = types.ModuleType("kiro_crew.config")
    fake_config.__path__ = []
    loader = types.ModuleType("kiro_crew.config.loader")
    loader_document = types.ModuleType("kiro_crew.config.loader_document")
    sdk = types.ModuleType("kiro_crew.agent_sdk")
    sdk.__path__ = []
    backends = types.ModuleType("kiro_crew.agent_sdk.backends")
    backends.ACP_BACKENDS_MEMBER_DISPATCH = {"", "kas", "kiro-byo"}
    backends.ACP_BACKENDS_MATE_WORKER_KIT = {"kas", "kiro-byo"}
    backends.ACP_BACKENDS_KNOWN = {"", "kas", "kiro-byo"}
    backends.ACP_BACKEND_LAUNCH = {"": object(), "kas": object(), "kiro-byo": object()}
    document = {"agent": {"acp_backend": "kiro-byo", "member_acp_backend": "kas", "model": "chat-model", "role_models": {"background": "auto"}}}

    class HostAgent:
        def __init__(self, raw):
            self.acp_backend = raw.get("acp_backend", "")
            member_raw = raw.get("member_acp_backend", "")
            if member_raw:
                self.member_acp_backend = member_raw
            elif self.acp_backend == "kiro-byo":
                self.member_acp_backend = "kiro-byo"
            else:
                self.member_acp_backend = "kas"
            self.role_models = raw.get("role_models", {})
            self.model = raw.get("model", "auto")

        def resolve_model(self, role):
            val = self.role_models.get(role) or "auto"
            return self.model if val == "auto" else val

    class KiroCrewConfig:
        @staticmethod
        def load():
            agents = {
                name: SimpleNamespace(
                    member_id=str(row.get("member_id") or ""),
                    display_name=str(row.get("display_name") or ""),
                    model=str(row.get("model") or ""),
                    worker_model=str(row.get("worker_model") or ""),
                )
                for name, row in (document.get("agents") or {}).items()
                if isinstance(row, dict)
            }
            return SimpleNamespace(agent=HostAgent(document["agent"]), agents=agents)

    def update_config_locked(path, *, mutate):
        # Copy to preserve transaction semantics on validation failure.
        clone = json.loads(json.dumps(document))
        mutate(clone)
        document.clear()
        document.update(clone)

    loader.KiroCrewConfig = KiroCrewConfig
    loader.config_path = lambda: tmp_path / "config.json"
    loader.update_config_locked = update_config_locked
    loader_document.member_acp_backend_declared = lambda: bool(document.get("agent", {}).get("member_acp_backend", ""))
    fake_modules = {
        pkg: root_pkg, pkg + ".backend": backend_pkg,
        "kiro_crew": fake_kiro, "kiro_crew.apps": fake_apps,
        "kiro_crew.apps.route_registry": fake_rr,
        "kiro_crew.config": fake_config, "kiro_crew.config.loader": loader,
        "kiro_crew.config.loader_document": loader_document,
        "kiro_crew.agent_sdk": sdk, "kiro_crew.agent_sdk.backends": backends,
    }
    for key, value in fake_modules.items():
        monkeypatch.setitem(sys.modules, key, value)

    spec = importlib.util.spec_from_file_location(pkg + ".backend.routes", root / "backend/routes.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_ensure_app_state", lambda ctx: tmp_path)
    monkeypatch.setattr(module, "load_config", lambda store: SimpleNamespace(public_models=[("byo-model", SimpleNamespace(name="Model A"))]))
    monkeypatch.setattr(module, "default_credential_store", lambda: None)

    class Request:
        def __init__(self, body=None, job_id="example", member="Gridman"):
            self.body = body
            self.match_info = {"job_id": job_id, "member": member}
            self.app = {"state": SimpleNamespace(crons=SimpleNamespace(list_jobs_async=self._jobs, update_job_async=self._update_job))}
        async def json(self):
            return self.body
        async def _jobs(self, **kwargs):
            return []
        async def _update_job(self, job_id, **kwargs):
            return SimpleNamespace(id=job_id, model=kwargs.get("model", ""), persistent_session=True, minimal_context=False)

    return SimpleNamespace(routes=module, document=document, backends=backends, request=Request)


def _result(response):
    import json
    return response.status, json.loads(response.text)


def test_linkage_api_status_and_manual_member_sync(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    r = host.routes
    code, state = _result(asyncio.run(r.get_task_linkage(host.request(), None)))
    assert code == 200
    assert state["linkage"]["members"]["configured_backend"] == "kas"
    code, result = _result(asyncio.run(r.sync_member_backend(host.request({"expected_main": "kiro-byo", "expected_member": "kas"}), None)))
    assert code == 200
    assert result["changed"] is True and result["effective"] is True
    assert "member_acp_backend" not in host.document["agent"]
    assert host.document["agent"]["acp_backend"] == "kiro-byo"
    # No Crew agents, sessions or scheduling data were written.
    assert set(host.document) == {"agent"}
    assert host.document["agent"]["role_models"] == {"background": "auto"}


def test_member_sync_stale_ui_and_missing_capability_are_atomic(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    host.document["agent"]["acp_backend"] = "kas"
    before = dict(host.document["agent"])
    code, payload = _result(asyncio.run(host.routes.sync_member_backend(host.request({"expected_main": "kiro-byo", "expected_member": "kas"}), None)))
    assert code == 400
    assert host.document["agent"] == before
    host.document["agent"]["acp_backend"] = "kiro-byo"
    host.backends.ACP_BACKENDS_MEMBER_DISPATCH.remove("kiro-byo")
    code, payload = _result(asyncio.run(host.routes.sync_member_backend(host.request({"expected_main": "kiro-byo", "expected_member": "kas"}), None)))
    assert code == 400
    assert host.document["agent"]["member_acp_backend"] == "kas"


def test_background_auto_inherits_model_without_false_error(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    host.document["agent"]["model"] = "chat-model-not-auto"
    code, payload = _result(asyncio.run(host.routes.update_background_model(host.request({"model": "auto"}), None)))
    assert code == 200, payload
    assert payload["background"]["model"] == "auto"
    assert payload["background"]["resolved_model"] == "chat-model-not-auto"
    assert payload["background"]["verified"] is True
    assert host.document["agent"]["role_models"]["background"] == "auto"


def test_non_byo_host_blocks_concrete_background_and_schedule_model(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    host.document["agent"]["acp_backend"] = "kas"
    code, _ = _result(asyncio.run(host.routes.update_background_model(host.request({"model": "byo-model"}), None)))
    assert code == 400
    assert host.document["agent"]["role_models"]["background"] == "auto"
    code, _ = _result(asyncio.run(host.routes.update_task_model(host.request({"model": "byo-model"}), None)))
    assert code == 400
    # Official mode is strictly read-only in the BYO task console, even when
    # clearing a pin, to avoid overwriting Crew native choices.
    code, response = _result(asyncio.run(host.routes.update_task_model(host.request({"model": ""}), None)))
    assert code == 400


def test_script_command_cron_are_reported_without_model_and_refuse_pin(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    script_job = SimpleNamespace(
        id="example", name="Script task", enabled=True, schedule=None, timezone="",
        script="~/crons/check.py:run", command="", model="", persistent_session=False,
        minimal_context=False, last_status="", last_error="", last_run_ts=0,
    )
    req = host.request({"model": "byo-model"})
    async def jobs(**kwargs):
        return [script_job]
    req.app["state"].crons.list_jobs_async = jobs
    code, data = _result(asyncio.run(host.routes.get_tasks(req, None)))
    assert code == 200
    assert data["tasks"][0]["execution_kind"] == "script"
    assert data["background"]["model"] == "auto"
    assert data["background"]["resolved_model"] == "chat-model"
    code, data = _result(asyncio.run(host.routes.update_task_model(req, None)))
    assert code == 400
    assert "Script/Command" in data["error"]
    command_job = SimpleNamespace(script="", command="df -h")
    assert host.routes._task_execution_kind(command_job) == "command"
    assert host.routes._task_execution_kind(SimpleNamespace(script="", command="")) == "agent"


def test_known_local_override_refuses_writes_before_mutation(live_linkage_routes, tmp_path):
    import asyncio
    import json
    host = live_linkage_routes
    override = tmp_path / "config.local.json"
    override.write_text(json.dumps({"agent": {"member_acp_backend": "kas", "role_models": {"background": "another-model"}}}), encoding="utf-8")
    before = json.loads(json.dumps(host.document))
    req = host.request({"expected_main": "kiro-byo", "expected_member": "kas"})
    code, error = _result(asyncio.run(host.routes.sync_member_backend(req, None)))
    assert code == 400 and "config.local.json" in error["error"]
    code, error = _result(asyncio.run(host.routes.update_background_model(host.request({"model": "byo-model"}), None)))
    assert code == 400 and "config.local.json" in error["error"]
    assert host.document == before


def test_unified_model_writes_share_crew_source_of_truth(live_linkage_routes, monkeypatch):
    import asyncio
    host = live_linkage_routes
    host.document["agents"] = {
        "Gridman": {"member_id": "m1", "display_name": "Gridman", "model": "", "worker_model": ""}
    }
    monkeypatch.setattr(host.routes, "_selection_ids", lambda config: {"byo-model", "kiro-byo-type:tiga:sky"})

    code, out = _result(asyncio.run(host.routes.update_chat_model(host.request({"model": "kiro-byo-type:tiga:sky"}), None)))
    assert code == 200 and out["chat"]["verified"] is True
    assert host.document["agent"]["model"] == "kiro-byo-type:tiga:sky"

    code, out = _result(asyncio.run(host.routes.update_member_model(host.request({"field": "model", "model": "kiro-byo-type:tiga:sky"}), None)))
    assert code == 200 and out["member"]["verified"] is True
    assert host.document["agents"]["Gridman"]["model"] == "kiro-byo-type:tiga:sky"

    code, out = _result(asyncio.run(host.routes.update_member_model(host.request({"field": "worker_model", "model": "auto"}), None)))
    assert code == 200 and out["member"]["model"] == "auto"
    assert host.document["agents"]["Gridman"]["worker_model"] == "auto"


def test_type_selection_allowed_for_schedule_and_background(live_linkage_routes, monkeypatch):
    import asyncio
    host = live_linkage_routes
    type_id = "kiro-byo-type:tiga:sky"
    monkeypatch.setattr(host.routes, "_selection_ids", lambda config: {"byo-model", type_id})
    code, out = _result(asyncio.run(host.routes.update_background_model(host.request({"model": type_id}), None)))
    assert code == 200 and out["background"]["model"] == type_id
    req = host.request({"model": type_id})
    stored_job = SimpleNamespace(id="example", model="", script="", command="")
    async def jobs(**kwargs):
        return [stored_job]
    async def update(job_id, *, model):
        stored_job.model = model
        return stored_job
    req.app["state"].crons.list_jobs_async = jobs
    req.app["state"].crons.update_job_async = update
    code, out = _result(asyncio.run(host.routes.update_task_model(req, None)))
    assert code == 200 and out["task"]["model"] == type_id


def test_unified_model_writes_disabled_when_main_harness_is_not_byo(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    host.document["agent"]["acp_backend"] = "kas"
    before = dict(host.document["agent"])
    code, _ = _result(asyncio.run(host.routes.update_chat_model(host.request({"model": "auto"}), None)))
    assert code == 400
    assert host.document["agent"] == before


def test_stage3_plan_respects_native_member_policy_and_cron_pin():
    base = linkage_snapshot(_agent())
    plan = linkage_change_plan(base, target_main="kas", member_mode="follow",
        main_backends={"kiro", "kas", "kiro-byo"}, member_backends={"kiro", "kas", "kiro-byo"},
        cron_models=[{"id":"a", "model":"byo-model", "execution_kind":"agent"},
                     {"id":"b", "model":"byo-model", "execution_kind":"script"}], byo_models={"byo-model"})
    assert plan["after"] == {"main":"kas", "member":"kas"}
    assert list(plan["changes"]) == ["agent.acp_backend"]
    assert plan["cron_model_conflicts"] == [{"id":"a", "model":"byo-model"}]
    assert plan["persistent_follow"] is False
    assert plan["scope"] == "future_default_sessions_only"
    fixed = linkage_change_plan(base, target_main="kiro", member_mode="fixed", fixed_member="kiro-byo",
        main_backends={"kiro", "kas", "kiro-byo"}, member_backends={"kiro", "kas", "kiro-byo"})
    assert fixed["after"] == {"main":"kiro", "member":"kiro-byo"}
    assert set(fixed["changes"]) == {"agent.acp_backend", "agent.member_acp_backend"}
    with pytest.raises(ValueError):
        linkage_change_plan(base, target_main="other", member_mode="follow", main_backends={"kiro"}, member_backends={"kiro"})


def test_stage3_preview_and_apply_optimistic_transaction(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    r = host.routes
    cmd = {"target_main":"kas", "member_mode":"follow"}
    code, shown = _result(asyncio.run(r.preview_linkage_switch(host.request(cmd), None)))
    assert code == 200
    preview = shown["preview"]
    assert preview["after"] == {"main":"kas", "member":"kas"}
    assert host.document["agent"]["acp_backend"] == "kiro-byo"  # preview never writes
    code, result = _result(asyncio.run(r.apply_linkage_switch(host.request({**cmd,"preview_id":preview["preview_id"]}), None)))
    assert code == 200 and result["changed"] is True and result["effective"] is True
    assert host.document["agent"]["acp_backend"] == "kas"
    assert "member_acp_backend" not in host.document["agent"]
    assert host.document["agent"]["role_models"] == {"background":"auto"}
    # A stale/consumed preview can no longer change the host.
    code, _ = _result(asyncio.run(r.apply_linkage_switch(host.request({**cmd,"preview_id":preview["preview_id"]}), None)))
    assert code == 400


def test_stage3_preview_refuses_stale_and_local_override(live_linkage_routes, tmp_path):
    import asyncio, json
    host = live_linkage_routes
    r = host.routes
    cmd = {"target_main":"kiro", "member_mode":"follow"}
    code, shown = _result(asyncio.run(r.preview_linkage_switch(host.request(cmd), None)))
    assert code == 200
    host.document["agent"]["member_acp_backend"] = "kiro-byo"
    before = json.loads(json.dumps(host.document))
    code, _ = _result(asyncio.run(r.apply_linkage_switch(host.request({**cmd,"preview_id":shown["preview"]["preview_id"]}), None)))
    assert code == 400 and before == host.document
    host.document["agent"]["member_acp_backend"] = "kas"
    (tmp_path / "config.local.json").write_text('{"agent":{"acp_backend":"kiro-byo"}}',encoding="utf-8")
    code, shown = _result(asyncio.run(r.preview_linkage_switch(host.request(cmd), None)))
    assert code == 200
    before = json.loads(json.dumps(host.document))
    code, out = _result(asyncio.run(r.apply_linkage_switch(host.request({**cmd,"preview_id":shown["preview"]["preview_id"]}), None)))
    assert code == 400 and before == host.document
    assert "config.local.json" in out["error"]


def test_stage3_unregistered_and_type_mismatch_do_not_write(live_linkage_routes):
    import asyncio, json
    host = live_linkage_routes
    before = json.loads(json.dumps(host.document))
    host.backends.ACP_BACKENDS_KNOWN.remove("kas")
    for body in [{"target_main":"kas","member_mode":"follow"},
                 {"target_main":"kiro-byo", "member_mode":"invalid"},
                 {"target_main":None,"member_mode":"follow"}]:
        code, _ = _result(asyncio.run(host.routes.preview_linkage_switch(host.request(body), None)))
        assert code == 400
    assert host.document == before


def test_stage3_detects_role_pins_not_just_cron():
    base = linkage_snapshot(_agent(bg="byo-model", sub="foreign-model"))
    outgoing = linkage_change_plan(base, target_main="kas", member_mode="keep",
        main_backends={"kiro-byo","kas"}, member_backends={"kas"}, byo_models={"byo-model"})
    assert outgoing["role_model_conflicts"] == [{"role":"background","model":"byo-model"}]
    incoming = linkage_change_plan(base, target_main="kiro-byo", member_mode="keep",
        main_backends={"kiro-byo","kas"}, member_backends={"kas"}, byo_models={"byo-model"})
    assert incoming["role_model_conflicts"] == [{"role":"subagent","model":"foreign-model"}]


def test_stage3_effective_mismatch_rolls_back_both_native_fields(live_linkage_routes, monkeypatch):
    import asyncio, json, sys
    host = live_linkage_routes
    loader = sys.modules['kiro_crew.config.loader']
    old_load = loader.KiroCrewConfig.load
    cmd = {"target_main":"kas","member_mode":"follow"}
    code, shown = _result(asyncio.run(host.routes.preview_linkage_switch(host.request(cmd), None)))
    assert code == 200
    before = json.loads(json.dumps(host.document))
    def overridden_load():
        conf = old_load()
        # After writing, simulate a higher-priority runtime/environment value.
        if host.document['agent']['acp_backend'] == 'kas':
            conf.agent.acp_backend = 'kiro-byo'
        return conf
    monkeypatch.setattr(loader.KiroCrewConfig, 'load', staticmethod(overridden_load))
    code, out = _result(asyncio.run(host.routes.apply_linkage_switch(host.request({**cmd,'preview_id':shown['preview']['preview_id']}), None)))
    assert code == 400 and 'rolled back' in out['error']
    assert host.document == before


def test_stage3_member_fixed_can_change_both_fields_atomically(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    cmd = {"target_main":"kiro","member_mode":"fixed","fixed_member":"kiro-byo"}
    code, shown = _result(asyncio.run(host.routes.preview_linkage_switch(host.request(cmd), None)))
    assert code == 200
    assert len(shown['preview']['changes']) == 2
    code, out = _result(asyncio.run(host.routes.apply_linkage_switch(host.request({**cmd,'preview_id':shown['preview']['preview_id']}), None)))
    assert code == 200 and out['effective'] is True
    assert host.document['agent']['acp_backend'] == ''
    assert host.document['agent']['member_acp_backend'] == 'kiro-byo'


def test_stage3_known_but_unlaunchable_host_is_not_switchable(live_linkage_routes):
    import asyncio
    host = live_linkage_routes
    host.backends.ACP_BACKEND_LAUNCH.pop('kas')
    code, body = _result(asyncio.run(host.routes.get_task_linkage(host.request(), None)))
    assert code == 200
    assert 'kas' not in body['available_main']
    code, _ = _result(asyncio.run(host.routes.preview_linkage_switch(host.request({'target_main':'kas','member_mode':'follow'}), None)))
    assert code == 400
    assert host.document['agent']['acp_backend'] == 'kiro-byo'
