from __future__ import annotations

from pathlib import Path

from kiro_byo.runtime_trace import RuntimeTraceStore, trace_mode


def test_runtime_trace_metadata_mode_records_shape_without_content(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KIRO_BYO_RUNTIME_TRACE", raising=False)
    store = RuntimeTraceStore(tmp_path / "events.jsonl", max_records=100)
    store.record(
        "provider.round.started",
        session_id="s1",
        data={"provider_id": "test", "model": "m1"},
        content={"messages": [{"role": "user", "content": "secret prompt"}]},
    )
    row = store.recent(1)[0]
    assert trace_mode() == "metadata"
    assert row["event"] == "provider.round.started"
    assert row["session_id"] == "s1"
    assert "secret prompt" not in str(row)
    assert row["content"]["messages"] == {"type": "list", "length": 1}


def test_runtime_trace_full_mode_redacts_credentials_but_keeps_debug_content(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "full")
    store = RuntimeTraceStore(tmp_path / "events.jsonl", max_records=100)
    store.record(
        "acp.inbound",
        session_id="s2",
        content={
            "payload": {
                "prompt": "inspect this exact prompt",
                "api_key": "sk-never-write-me",
                "headers": {"Authorization": "Bearer hidden", "X-Trace": "visible"},
            }
        },
    )
    row = store.recent(1)[0]
    assert row["mode"] == "full"
    assert row["content"]["payload"]["prompt"] == "inspect this exact prompt"
    assert row["content"]["payload"]["api_key"] == "<redacted>"
    assert row["content"]["payload"]["headers"]["Authorization"] == "<redacted>"
    assert row["content"]["payload"]["headers"]["X-Trace"] == "visible"
    assert "sk-never-write-me" not in store.path.read_text(encoding="utf-8")
    assert "Bearer hidden" not in store.path.read_text(encoding="utf-8")


def test_runtime_trace_off_mode_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "off")
    store = RuntimeTraceStore(tmp_path / "events.jsonl", max_records=100)
    store.record("turn.completed", session_id="s3", data={"outcome": "success"})
    assert not store.path.exists()
    assert store.recent() == []


def test_runtime_trace_is_bounded_and_filterable_by_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "metadata")
    store = RuntimeTraceStore(tmp_path / "events.jsonl", max_records=100)
    for index in range(130):
        store.record("event", session_id="keep" if index % 2 else "other", data={"index": index})
    all_rows = store.recent(500)
    assert len(all_rows) == 100
    kept = store.recent(20, session_id="keep")
    assert kept
    assert all(row["session_id"] == "keep" for row in kept)


def test_runtime_trace_correlates_events_by_durable_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "metadata")
    store = RuntimeTraceStore(tmp_path / "events.jsonl", max_records=100)
    store.record("turn.started", session_id="s1", run_id="run-a", step_id="step-a", attempt_id="attempt-a")
    store.record("provider.round.started", session_id="s1", run_id="run-b")
    store.record("turn.completed", session_id="s1", run_id="run-a")
    rows = store.recent(20, run_id="run-a")
    assert [row["event"] for row in rows] == ["turn.started", "turn.completed"]
    assert rows[0]["step_id"] == "step-a"
    assert rows[0]["attempt_id"] == "attempt-a"
