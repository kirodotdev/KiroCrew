"""Scenario tests for the read-only symptom probes behind ``diagnose_settings``.

Each scenario seeds one reported symptom into a private data home (and fake
gateway state where the symptom lives in memory) and pins the finding the
probe reports: its status, the evidence that names the cause, and the fix.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import change_card_catalog as catalog
from kiro_crew import diagnose_probes as dp

#: A credential-shaped value the redactor recognises; it must never reach a finding.
SECRET = "AKIA" + "Z7Q3XK2LM9PB4TWV"
HOUR_MS = 3600 * 1000


class _Status:
    def __init__(self, *, installed: bool = True, authenticated: bool = True) -> None:
        self.installed = installed
        self.authenticated = authenticated
        self.login_command = "kiro-cli login"
        self.docs_url = "https://kiro.dev/docs/cli/"


class _Service:
    def __init__(self, status: _Status, probed: bool = True) -> None:
        self._status = status
        self._has_probed = probed


class _Slot:
    def __init__(self, title: str, running: bool) -> None:
        self.title = title
        self.turn_running = running


class _State:
    def __init__(self, start_time: float, slots: dict[str, _Slot] | None = None) -> None:
        self.start_time = start_time
        self._slots = slots or {}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A private data home, kiro home and config, plus the in-memory seams."""
    from kiro_crew import agent_discovery, model_registry
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import sessions

    home = tmp_path / "crew"
    kiro = tmp_path / "kiro"
    (kiro / "agents").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(kiro))
    cfg = KiroCrewConfig()
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(model_registry, "advertised_models", lambda ns: ["model-a", "auto"])
    monkeypatch.setattr(agent_discovery, "_LIST_AGENTS_CACHE", {})
    monkeypatch.setattr(sessions, "_usage_cache", {"available": True, "percentage": 10.0})
    units: list[dict[str, Any]] = []
    monkeypatch.setattr(dp, "_crew_log_units", lambda now_ms: list(units))
    state = _State(start_time=time.time() + 60)
    app = {"state": state, "kiro_prerequisite_service": _Service(_Status())}
    return {"home": home, "agents": kiro / "agents", "cfg": cfg, "units": units, "app": app}


def _by_id(findings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in findings}


def _run(world: dict[str, Any], topic: str = "") -> dict[str, dict[str, Any]]:
    return _by_id(dp.run_probes(world["app"], topic))


def _unit(slot: str, entries: list[tuple[str, int, dict[str, Any]]]) -> dict[str, Any]:
    return {
        "unit": "u-" + slot,
        "slot": slot,
        "open": True,
        "entries": [{"type": t, "time": ts, "data": d} for t, ts, d in entries],
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Healthy state
# --------------------------------------------------------------------------- #


def test_a_healthy_install_reports_no_problem_or_warning(world):
    findings = dp.run_probes(world["app"])
    assert [f["id"] for f in sorted(findings, key=lambda f: f["id"])] == sorted(
        pid for pid, _ in dp.PROBES
    )
    assert {f["status"] for f in findings} <= {dp.OK, dp.UNKNOWN}
    assert all("fix" not in f for f in findings)


def test_probe_ids_are_the_twelve_reported_symptoms():
    ids = [pid for pid, _ in dp.PROBES]
    assert len(ids) == len(set(ids)) == 12
    assert {fn.__name__ for _, fn in dp.PROBES} == {f"probe_{pid}" for pid in ids}


# --------------------------------------------------------------------------- #
# One scenario per probe
# --------------------------------------------------------------------------- #


def test_hidden_model_named_in_the_question_is_a_problem(world):
    world["cfg"].dashboard.model_picker_hidden_models = ["fable-1", "other-2"]
    f = _run(world, "fable")["hidden_models"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["matched"] == ["fable-1"]
    assert f["evidence"]["setting_id"] == "chat.selectable-models"
    # The fix is a card through the Settings page's own one-item unhide.
    assert f["fix"] == {
        "card": {
            "kind": "setting.change",
            "params": {"setting_id": "chat.selectable-models", "op": "remove", "item": "fable-1"},
        }
    }
    assert catalog.validate_params("setting.change", f["fix"]["card"]["params"]) == (
        f["fix"]["card"]["params"]
    )
    unmatched = _run(world)["hidden_models"]
    assert unmatched["status"] == dp.WARN
    assert "op 'remove'" in unmatched["fix"]["steps"][0]


def test_agent_installed_after_gateway_start_is_listed_without_a_restart(world):
    # The agent list re-reads the directory on change, so a spec written after
    # start is offered at once: no problem, and no restart advice.
    world["app"]["state"].start_time = time.time() - 600
    (world["agents"] / "team-helper.json").write_text(json.dumps({"name": "team-helper"}))
    f = _run(world)["agent_picker_stale"]
    assert f["status"] == dp.OK
    assert [s["name"] for s in f["evidence"]["specs"]] == ["team-helper"]
    assert f["evidence"]["specs"][0]["listed"] is True
    assert "restart" not in json.dumps(f).lower()


def test_agent_spec_the_list_cannot_use_is_a_problem_without_restart_advice(world):
    world["app"]["state"].start_time = time.time() - 600
    (world["agents"] / "broken.json").write_text("{not json")
    f = _run(world)["agent_picker_stale"]
    assert f["status"] == dp.PROBLEM
    assert [s["name"] for s in f["evidence"]["specs"] if not s["listed"]] == ["broken"]
    assert not any("Restart" in s for s in f["fix"]["steps"])


def test_agent_picker_is_unknown_without_gateway_state(world):
    world["app"]["state"] = None
    assert _run(world)["agent_picker_stale"]["status"] == dp.UNKNOWN


def test_pinned_model_the_account_does_not_offer(world):
    world["cfg"].agent.model = "model-x"
    f = _run(world)["model_pin_unavailable"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["pins"] == [
        {"holder": "agent.model", "kind": "config", "model": "model-x"}
    ]
    assert f["fix"] == {
        "card": {"kind": "setting.change", "params": {"path": "agent.model", "value": "auto"}}
    }


def test_model_pins_are_unknown_before_any_session_reported_models(world, monkeypatch):
    from kiro_crew import model_registry

    world["cfg"].agent.model = "model-x"
    monkeypatch.setattr(model_registry, "advertised_models", lambda ns: [])
    assert _run(world)["model_pin_unavailable"]["status"] == dp.UNKNOWN


def test_config_naming_a_deprecated_agent_spec(world):
    from kiro_crew.agent import DEPRECATED_AGENT_SPECS

    old, new = next(iter(DEPRECATED_AGENT_SPECS.items()))
    world["cfg"].agent.default_agent = old
    f = _run(world)["deprecated_agent_spec"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["uses"] == [
        {"holder": "agent.default_agent", "name": old, "replacement": new}
    ]
    assert new in f["fix"]["steps"][0]


def _write_crons(home: Path, jobs: list[dict[str, Any]]) -> None:
    (home / "crons.json").write_text(json.dumps({"jobs": jobs}))


def test_cron_whose_last_run_errored(world):
    when = time.time() - 3600
    _write_crons(
        world["home"],
        [
            {
                "id": "j1",
                "name": "digest",
                "last_status": "error",
                "last_run_ts": when,
                "last_error": "agent not found: helper\nTraceback ...",
            },
            {"id": "j2", "name": "fine", "last_status": "ok"},
            {"id": "j3", "name": "paused by me", "last_status": "error", "user_paused": True},
        ],
    )
    f = _run(world)["cron_failing"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["jobs"] == [
        {
            "id": "j1",
            "name": "digest",
            "error": "agent not found: helper",
            "when": dp._iso(when),
            "auto_paused": False,
        }
    ]
    assert "schedule.update" in f["fix"]["steps"][1]


def test_cron_failing_on_its_timezone_gets_a_schedule_card(world):
    _write_crons(
        world["home"],
        [
            {
                "id": "j1",
                "name": "tz",
                "last_status": "error",
                "timezone": "Mars/Base",
                "last_error": "unknown timezone 'Mars/Base'",
            }
        ],
    )
    f = _run(world)["cron_failing"]
    assert f["fix"] == {
        "card": {"kind": "schedule.update", "params": {"id": "j1", "fields": {"timezone": ""}}}
    }


def test_unloadable_cron_store_is_a_problem(world):
    (world["home"] / "crons.json").write_text("{not json")
    assert _run(world)["cron_failing"]["status"] == dp.PROBLEM


def test_agent_spec_mcp_command_that_no_longer_exists(world, tmp_path):
    gone = str(tmp_path / "removed" / "bin" / "server")
    spec = {"name": "team-helper", "mcpServers": {"team": {"command": gone}}}
    (world["agents"] / "team-helper.json").write_text(json.dumps(spec))
    f = _run(world)["agent_spec_dead_paths"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["dead"] == [
        {
            "spec": "team-helper.json",
            "managed": False,
            "server": "team",
            "where": "command",
            "path": gone,
        }
    ]
    assert "Reinstall" in f["fix"]["steps"][0]


def test_signed_out_kiro_cli_explains_failing_turns(world):
    world["app"]["kiro_prerequisite_service"] = _Service(_Status(authenticated=False))
    world["units"].append(
        _unit(
            "dashboard:chat-1",
            [("turn/completed", _now_ms(), {"turn": 1, "stop_reason": "failed"})],
        )
    )
    f = _run(world)["kiro_cli_auth"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"] == {"installed": True, "authenticated": False, "failed_turns_24h": 1}
    assert f["fix"]["steps"] == [
        "On the gateway host run: kiro-cli login",
        "Start a new chat after signing in.",
    ]


def test_kiro_cli_auth_is_unknown_before_the_first_check(world):
    world["app"]["kiro_prerequisite_service"] = _Service(_Status(), probed=False)
    assert _run(world)["kiro_cli_auth"]["status"] == dp.UNKNOWN


def _memory_db(home: Path, blobs: list[bytes | None]) -> None:
    db = sqlite3.connect(home / "memory.db")
    db.execute(
        "CREATE TABLE episodic_memories (id TEXT PRIMARY KEY, text TEXT, embedding BLOB, "
        "is_deleted INTEGER DEFAULT 0)"
    )
    for i, blob in enumerate(blobs):
        db.execute("INSERT INTO episodic_memories VALUES (?, 't', ?, 0)", (str(i), blob))
    db.commit()
    db.close()


def test_stored_vectors_of_a_different_dimension(world):
    world["cfg"].memory.embedding_dim = 1024
    _memory_db(world["home"], [b"\0" * 4096, b"\0" * 1536, None])
    f = _run(world)["embedding_coverage"]
    assert f["status"] == dp.PROBLEM
    table = f["evidence"]["tables"]["episodic_memories"]
    assert {k: table[k] for k in ("rows", "without_embedding", "wrong_dimension")} == {
        "rows": 3,
        "without_embedding": 1,
        "wrong_dimension": 1,
    }
    assert sorted(table["stored_dimensions"]) == [384, 1024]
    assert "Nothing here deletes" in f["fix"]["steps"][0]


def test_rows_without_embeddings_are_a_warning(world):
    _memory_db(world["home"], [None])
    assert _run(world)["embedding_coverage"]["status"] == dp.WARN


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_remote_crew_that_does_not_answer(world):
    port = _closed_port()
    instances = {"instances": [{"id": "r1", "name": "lab box", "local_port": port}]}
    (world["home"] / "instances.json").write_text(json.dumps(instances))
    f = _run(world)["remote_crew_unreachable"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["unreachable"] == [{"name": "lab box", "local_port": port}]
    assert "Remote Crew" in f["fix"]["steps"][0]


def test_turn_running_for_half_an_hour(world):
    world["app"]["state"]._slots = {
        "chat-1": _Slot("Release notes", True),
        "chat-2": _Slot("x", False),
    }
    started = _now_ms() - 30 * 60 * 1000
    world["units"].append(_unit("dashboard:chat-1", [("turn/started", started, {"turn": 4})]))
    f = _run(world)["long_running_slot"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["sessions"] == [
        {"session": "Release notes", "key": "chat-1", "minutes": 30}
    ]
    assert any("private window" in s for s in f["fix"]["steps"])


def test_a_finished_turn_is_not_long_running(world):
    world["app"]["state"]._slots = {"chat-1": _Slot("t", True)}
    old = _now_ms() - 30 * 60 * 1000
    world["units"].append(
        _unit(
            "dashboard:chat-1",
            [
                ("turn/started", old, {"turn": 1}),
                ("turn/completed", old + 1, {"turn": 1}),
                ("turn/started", _now_ms(), {"turn": 2}),
            ],
        )
    )
    assert _run(world)["long_running_slot"]["status"] == dp.OK


def test_recent_tool_failures_grouped_by_tool(world):
    now = _now_ms()
    entries = [
        (
            "tool/completed",
            now - i,
            {"name": "tool_search", "server": "builder", "status": "completed", "is_error": True},
        )
        for i in range(3)
    ]
    entries.append(("tool/completed", now, {"name": "read", "server": "", "status": "completed"}))
    entries.append(("turn/refused", now, {"reason": "not_authorized"}))
    entries.append(("tool/completed", now - 30 * HOUR_MS, {"name": "old", "is_error": True}))
    world["units"].append(_unit("dashboard:chat-1", entries))
    f = _run(world)["recent_tool_failures"]
    assert f["status"] == dp.PROBLEM
    assert f["evidence"]["groups"] == [
        {"kind": "tool", "what": "builder/tool_search", "outcome": "completed", "count": 3},
        {"kind": "turn_refused", "what": "", "outcome": "not_authorized", "count": 1},
    ]


def test_used_up_plan_and_unreadable_balance(world, monkeypatch):
    from kiro_crew.dashboard.handlers import sessions

    monkeypatch.setattr(
        sessions,
        "_usage_cache",
        {
            "available": True,
            "percentage": 100.0,
            "credits_used": 50.0,
            "credits_plan": 50.0,
            "credits_overage": 0,
            "email": "someone@example.com",
        },
    )
    f = _run(world)["usage_limit"]
    assert f["status"] == dp.PROBLEM
    assert "email" not in f["evidence"]
    monkeypatch.setattr(sessions, "_usage_cache", {"available": False, "reason": "signed_out"})
    f = _run(world)["usage_limit"]
    assert f["status"] == dp.WARN
    assert f["evidence"]["reason"] == "signed_out"


# --------------------------------------------------------------------------- #
# Safety and bounds
# --------------------------------------------------------------------------- #


def _seed_every_problem(world: dict[str, Any], tmp_path: Path) -> None:
    world["cfg"].dashboard.model_picker_hidden_models = ["fable-1"]
    world["cfg"].agent.model = "model-x"
    world["app"]["state"].start_time = time.time() - 600
    spec = {
        "name": "team-helper",
        "model": "model-y",
        "mcpServers": {
            "team": {
                "command": str(tmp_path / "gone" / "server"),
                "env": {"API_TOKEN": str(tmp_path / "gone" / SECRET)},
            }
        },
    }
    (world["agents"] / "team-helper.json").write_text(json.dumps(spec))
    _write_crons(
        world["home"],
        [
            {
                "id": "j1",
                "name": "digest",
                "last_status": "error",
                "last_error": f"key {SECRET} rejected",
            }
        ],
    )
    _memory_db(world["home"], [b"\0" * 8])
    world["app"]["kiro_prerequisite_service"] = _Service(_Status(authenticated=False))


def test_no_finding_carries_a_secret(world, tmp_path):
    _seed_every_problem(world, tmp_path)
    findings = dp.run_probes(world["app"], "fable")
    text = json.dumps(findings)
    assert SECRET not in text
    assert "mcpServers" not in text
    assert sum(f["status"] == dp.PROBLEM for f in findings) >= 6


def test_findings_stay_bounded(world):
    world["cfg"].dashboard.model_picker_hidden_models = [f"m-{i}" * 40 for i in range(50)]
    f = _run(world)["hidden_models"]
    assert len(f["evidence"]["hidden"]) == dp._LIST_MAX
    assert all(len(m) <= dp._TEXT_MAX for m in f["evidence"]["hidden"])


def test_a_slow_or_failing_probe_reports_unknown(world, monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def slow(ctx):
        try:
            assert release.wait(5), "slow probe was not released"
            return dp.finding("slow", dp.OK, "late")
        finally:
            finished.set()

    def broken(ctx):
        raise RuntimeError("boom " + SECRET)

    monkeypatch.setattr(dp, "PROBES", (("slow", slow), ("broken", broken)))
    try:
        findings = _by_id(dp.run_probes(world["app"], timeout=0.2, total=1.0))
    finally:
        release.set()
        assert finished.wait(5), "slow probe did not finish"
    # A run that waited for the slow probe would carry its OK finding.
    assert findings["slow"]["status"] == dp.UNKNOWN
    assert findings["broken"]["status"] == dp.UNKNOWN
    assert SECRET not in json.dumps(findings)


def _snapshot(*roots: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                st = p.lstat()
                out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


def test_no_probe_writes_to_disk(world, tmp_path):
    _seed_every_problem(world, tmp_path)
    for filename in _snapshot(tmp_path):
        os.utime(filename, ns=(1_000_000_000, 1_000_000_000), follow_symlinks=False)
    before = _snapshot(tmp_path)
    findings = dp.run_probes(world["app"], "fable")
    assert sum(f["status"] == dp.PROBLEM for f in findings) >= 6
    assert _snapshot(tmp_path) == before


def test_diagnose_settings_carries_the_findings(world, monkeypatch):
    from kiro_crew import context
    from kiro_crew.dashboard import change_cards as cards

    class _Mem:
        def read_history_entries(self, since=None):
            return []

    monkeypatch.setattr(
        context.ContextBuilder, "get_memory_for", staticmethod(lambda *a, **k: _Mem())
    )
    world["cfg"].dashboard.model_picker_hidden_models = ["fable-1"]
    result = cards.diagnose_settings("fable", world["app"])
    assert result["findings"][0]["id"] == "hidden_models"
    assert result["findings"][0]["status"] == dp.PROBLEM
