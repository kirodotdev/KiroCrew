"""Regression tests for V10 security and cross-process state transactions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.runtime_trace import RuntimeTraceStore
from kiro_byo.telemetry import TurnTelemetryStore


def test_trace_masks_secret_headers_nested_urls_and_encoded_query(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "full")
    store = RuntimeTraceStore(tmp_path / "events.jsonl")
    store.record("route.selected", data={
        "x-goog-api-key": "BAD_GOOGLE_TOKEN",
        "nested": [{"X-Auth-Token": "BAD_AUTH_TOKEN"}],
        "url": "https://name:BAD_PASS@provider.test/api?api%5Fkey=BAD_URL_TOKEN&mode=tiga",
        "error": "Authorization: Bearer BAD_BEARER",
    }, content={"url": "https://provider.test/v1?token=BAD_CONTENT_TOKEN&n=42"})
    raw = store.path.read_text(encoding="utf-8")
    for secret in ("BAD_GOOGLE_TOKEN", "BAD_AUTH_TOKEN", "BAD_PASS", "BAD_URL_TOKEN", "BAD_BEARER", "BAD_CONTENT_TOKEN"):
        assert secret not in raw
    row = store.recent()[0]
    assert row["data"]["url"].endswith("&mode=tiga")
    assert row["data"]["x-goog-api-key"] == "<redacted>"


def test_trace_metadata_mode_also_redacts_data_and_retains_shapes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "metadata")
    store = RuntimeTraceStore(tmp_path / "events.jsonl")
    store.record("provider.failure", data={"url": "https://example.test/?token=BAD"}, content={"body": "private"})
    raw = store.path.read_text(encoding="utf-8")
    assert "?token=BAD" not in raw
    assert '"chars":7' in raw


def test_durable_clear_removes_only_safe_completed_records(tmp_path: Path) -> None:
    store = DurableRunStore(tmp_path / "runs")
    active = store.create_run("s-active")
    failed = store.create_run("s-failed")
    store.finish_run(failed["run_id"], status="failed", recovery_state="retryable")
    completed = store.create_run("s-completed")
    store.finish_run(completed["run_id"], status="completed")
    uncertain = store.create_run("s-unknown")
    store.finish_run(uncertain["run_id"], status="completed", recovery_state="unknown_side_effect")
    assert store.clear() == 1
    assert store.get(completed["run_id"]) is None
    for protected in (active, failed, uncertain):
        assert store.get(protected["run_id"]) is not None


def _spawn(code: str, *args: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[1] / "src"), env.get("PYTHONPATH", "")])
    return subprocess.Popen([sys.executable, "-c", code, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_telemetry_clear_waits_for_cross_process_writer(tmp_path: Path) -> None:
    path = tmp_path / "turns.jsonl"
    marker = tmp_path / "ready"
    code = '''
from pathlib import Path
from kiro_byo.state_io import _file_transaction_lock
import sys, time
path, marker = map(Path, sys.argv[1:])
with _file_transaction_lock(path):
    path.write_text('\\n', encoding='utf-8')
    marker.touch()
    time.sleep(0.55)
'''
    p = _spawn(code, str(path), str(marker))
    try:
        deadline = time.monotonic() + 6
        while not marker.exists() and time.monotonic() < deadline and p.poll() is None:
            time.sleep(0.015)
        assert marker.exists(), p.communicate(timeout=2)[1]
        before = time.monotonic()
        assert TurnTelemetryStore(path).clear() == 1
        assert time.monotonic() - before > 0.15
    finally:
        p.communicate(timeout=6)
        assert p.returncode == 0


def test_provider_configuration_read_modify_write_is_cross_process_atomic(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text('{"providers":[],"mode":{"id":"tiga","type":"multi"}}', encoding="utf-8")
    code = '''
from pathlib import Path
from kiro_byo import provider_admin as pa
import sys, time
path = Path(sys.argv[1])
name = sys.argv[2]
pa.config_path = lambda: path
@pa._serialized_config_mutation
def update():
    raw = pa._read_raw()
    time.sleep(0.23)
    raw[name] = True
    pa._write_raw(raw)
update()
'''
    p1 = _spawn(code, str(path), "first")
    p2 = _spawn(code, str(path), "second")
    for p in (p1, p2):
        stdout, stderr = p.communicate(timeout=8)
        assert p.returncode == 0, (stdout, stderr)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["first"] and data["second"]
