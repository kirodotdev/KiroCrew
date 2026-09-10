from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def driver():
    path = Path(__file__).parents[1] / "addons/ucam-synthetic-consumer/deploy.py"
    spec = importlib.util.spec_from_file_location("ucam_deploy_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path, driver, monkeypatch):
    root = tmp_path / "source"
    (root / "acp").mkdir(parents=True)
    (root / "subagent.py").write_text(
        """class Manager:
    async def run(self, info):
        use_session_sharing = (not info.keep) and self._should_use_session_sharing(info)
        named_agent = bool(info.agent and _AGENT_NAME_RE.fullmatch(info.agent))
        _groups = _context_groups_of(info)
        full_message = await build(
            context_groups=_groups,
        )
        if True:
            if True:
                if True:
                    async for _ev in client.stream(msg):
                        yield _ev
    retained_upstream_behavior = "not replaced"
"""
    )
    (root / "acp/client.py").write_text(
        """class Client:
    async def _send_request(self, method: str, params: dict) -> int:
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")
        req_id = self._next_req_id()
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\\n"
        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied("broken pipe") from exc
        self._last_activity = time.monotonic()
        return req_id
    retained_upstream_behavior = "not replaced"
"""
    )
    (root / "acp/runtime.py").write_text(
        (root / "acp/client.py")
        .read_text()
        .replace("_send_request", "send_request")
        .replace("self._next_req_id()", "self._next_id")
    )
    monkeypatch.setattr(
        driver,
        "EXPECTED",
        {name: driver.digest((root / name).read_bytes()) for name in driver.TARGETS},
    )
    return root


def prepare(driver, source, tmp_path):
    module = Path(__file__).parents[1] / "src/kiro_crew/ucam_consumer.py"
    bundle = tmp_path / "bundle"
    approval = driver.prepare(source, module, bundle)
    return bundle, approval


def test_prepare_is_read_only_and_preserves_upstream(driver, source, tmp_path):
    original = (source / "subagent.py").read_bytes()
    bundle, approval = prepare(driver, source, tmp_path)
    assert (source / "subagent.py").read_bytes() == original
    assert not (source / driver.MODULE_NAME).exists()
    patched = (bundle / "patched/subagent.py").read_text()
    assert 'retained_upstream_behavior = "not replaced"' in patched
    assert "frozenset() if ucam_run" in patched
    assert approval == driver.digest((bundle / "plan.json").read_bytes())


def test_apply_and_rollback_exact_bytes(driver, source, tmp_path):
    originals = {name: (source / name).read_bytes() for name in driver.TARGETS}
    bundle, approval = prepare(driver, source, tmp_path)
    driver.apply(source, bundle, approval)
    assert (source / driver.MODULE_NAME).exists()
    assert "ucam_receipt.check_lease()" in (source / "acp/client.py").read_text()
    driver.rollback(source, bundle, approval)
    assert all((source / name).read_bytes() == data for name, data in originals.items())
    assert (source / driver.MODULE_NAME).exists()


def test_live_drift_refuses_without_mutation(driver, source, tmp_path):
    bundle, approval = prepare(driver, source, tmp_path)
    changed = (source / "subagent.py").read_text() + "\nupstream_new = True\n"
    (source / "subagent.py").write_text(changed)
    with pytest.raises(RuntimeError, match="live_source_drift"):
        driver.apply(source, bundle, approval)
    assert (source / "subagent.py").read_text() == changed
    assert not (source / driver.MODULE_NAME).exists()


def test_bundle_drift_refuses(driver, source, tmp_path):
    bundle, approval = prepare(driver, source, tmp_path)
    (bundle / driver.MODULE_NAME).write_text("raise RuntimeError('tampered')\n")
    with pytest.raises(RuntimeError, match="module_bundle_drift"):
        driver.apply(source, bundle, approval)


def test_app_bundle_drift_refuses(driver, source, tmp_path):
    bundle, approval = prepare(driver, source, tmp_path)
    (bundle / "app/backend/routes.py").write_text("raise RuntimeError('tampered')\n")
    with pytest.raises(RuntimeError, match="app_bundle_drift"):
        driver.apply(source, bundle, approval)


def test_approval_required(driver, source, tmp_path):
    bundle, approval = prepare(driver, source, tmp_path)
    with pytest.raises(RuntimeError, match="plan_approval_mismatch"):
        driver.apply(source, bundle, "")


def test_rollback_will_not_revert_other_worker(driver, source, tmp_path):
    bundle, approval = prepare(driver, source, tmp_path)
    driver.apply(source, bundle, approval)
    changed = (source / "subagent.py").read_text() + "\nother_worker_edit = True\n"
    (source / "subagent.py").write_text(changed)
    with pytest.raises(RuntimeError, match="live_source_drift"):
        driver.rollback(source, bundle, approval)
    assert (source / "subagent.py").read_text() == changed
