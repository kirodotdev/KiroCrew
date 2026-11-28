"""The spawn and host-headroom tools answer without loading the sub-agent manager.

``kirocrew-core`` answers ``resource_status`` and the ``spawn_*`` tools by calling the
gateway; it never runs a sub-agent itself. So those tools read the sub-agent names they
need through ``kiro_crew.subagent_standalone`` and leave ``kiro_crew.subagent`` (the
manager, about 30 MB of a tool server's memory) unloaded. These tests pin that the
facade stays unloaded after each tool and the replies carry the usage line, refusal code
and each posture. In-process tests check that a facade patch reaches every reader.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
from test_mcp_slim_entry import _child_env

import kiro_crew.subagent as subagent
from kiro_crew import subagent_standalone
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: Each call: the host-memory figure (GB) pinned for it, the tool and its arguments.
#: ``resource_status`` runs at an ample, a tight and a critical figure (a tight or
#: critical posture also words the agent-slice ceiling). ``spawn_run`` runs twice: on a
#: plain spawn, and on a refusal that carries a wire code (the refusal-code readers).
_CALLS = (
    (64.0, "resource_status", {}),
    (3.0, "resource_status", {}),
    (1.0, "resource_status", {}),
    (64.0, "spawn_list", {}),
    (64.0, "spawn_status", {"agent_id": "abc123"}),
    (64.0, "spawn_run", {"task": "say hi"}),
    (64.0, "spawn_run", {"task": "say hi", "agent": "nope"}),
    (64.0, "spawn_steer", {"agent_id": "abc123", "message": "hi"}),
)

#: Checks the facade stays unloaded after each tool and replies carry the usage line,
#: refusal code and each posture; in-process tests check facade patch reach.
_PROBE = textwrap.dedent("""
    import json, sys
    from kiro_crew import mcp_core, resource_status
    from kiro_crew.mcp_shared import drive_deferred
    from kiro_crew.subagent_parts import host_memory

    calls = json.loads(sys.argv[1])
    refusal = {}
    figure = {"gb": 64.0}

    def _get(path, *args, **kwargs):
        return {
            "agents": [{"id": "a1", "task": "probe", "done": True, "turns": 2,
                        "elapsed": 75.5, "credits": 1.25, "started": 0}],
            "done": True, "result": "ok", "credits": 1.25, "elapsed": 75.5,
        }

    def _post(path, body=None, *args, **kwargs):
        if isinstance(body, dict) and body.get("agent") == "nope":
            return {"error": "unknown agent", "code": refusal["code"]}
        return {"id": "a1", "agent_id": "a1", "ok": True}

    mcp_core._get = _get
    mcp_core._post = _post
    resource_status._read_load_per_cpu = lambda _cpus: 0.1
    resource_status._read_agent_slice_tasks = lambda: (1, 100, 1)
    resource_status.read_memory_pressure_level = lambda: None
    resource_status.temp_roots = lambda: ["/probe-temp"]
    resource_status.temp_headroom = lambda root: resource_status.TempHeadroom(
        root, 50.0, 50.0, 10000
    )

    def run():
        out = []
        for gb, name, args in calls:
            figure["gb"] = gb
            reply = mcp_core._call_tool_inner(name, dict(args))
            text = reply if isinstance(reply, str) else drive_deferred(reply)
            out.append(text)
        return out

    from kiro_crew import subagent_standalone
    refusal["code"] = subagent_standalone.agent_refusal_codes()[2]
    host_memory._available_memory_gb = lambda **_kw: figure["gb"]
    host_memory.out_of_view_memory_pressure_file = lambda: None
    host_memory._agents_slice_available_gb = lambda: -1.0
    host_memory.check_memory_available = lambda **_kw: (True, figure["gb"])
    unloaded = run()
    facade_after_calls = "kiro_crew.subagent" in sys.modules
    print(json.dumps({"unloaded": unloaded, "facade_after_calls": facade_after_calls}))
    """)


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    home = tmp_path_factory.mktemp("home")
    (home / "config.json").write_text("{}", encoding="utf-8")
    env = _child_env(home, KIROCREW_HOME=str(home))
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(_CALLS)],
        env=env,
        cwd=str(home),
        capture_output=True,
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_tools_leave_the_manager_unloaded(probe: dict) -> None:
    assert len(probe["unloaded"]) == len(_CALLS)
    assert all(text for text in probe["unloaded"])  # every tool answered
    assert probe["facade_after_calls"] is False


def test_the_usage_line_and_the_refusal_code_reach_the_replies(probe: dict) -> None:
    status = probe["unloaded"][4]
    assert "credits · " in status  # format_subagent_usage ran
    refused, plain = probe["unloaded"][6], probe["unloaded"][5]
    assert refused != plain


def test_resource_status_ran_at_each_posture(probe: dict) -> None:
    ample, tight, critical = probe["unloaded"][:3]
    assert "Posture: AMPLE" in ample
    assert "Posture: TIGHT" in tight
    assert "Posture: CRITICAL" in critical


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("_available_memory_gb", {}),
        ("_agents_slice_available_gb", {}),
        ("_container_cgroup_available_gb", {}),
        ("out_of_view_memory_pressure_file", {}),
        ("pop_memory_check_cause", {}),
        ("check_memory_available", {"min_gb": 1.0}),
        ("_spawn_memory_floor_and_cost", {}),
        ("format_subagent_usage", {"credits": 1, "elapsed": 1}),
    ],
)
def test_a_patch_of_the_facade_reaches_each_reader(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict
) -> None:
    marker = object()
    monkeypatch.setattr(subagent, name, lambda *_a, **_kw: marker)
    assert getattr(subagent_standalone, name)(**args) is marker


def test_a_patch_of_the_facade_reaches_the_price_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagent, "_dedicated_start_price_gb", lambda *_a, **_kw: 7.5)
    monkeypatch.setattr(subagent, "_shared_start_price_gb", lambda _gb: 0.25)
    assert subagent_standalone._dedicated_start_price_gb(1.0, None, "") == 7.5
    assert subagent_standalone._shared_start_price_gb(1.0) == 0.25


def test_the_refusal_codes_are_the_facade_objects() -> None:
    names = ("AGENT_INTERNAL_CODE", "AGENT_NOT_AVAILABLE_CODE", "AGENT_NOT_FOUND_CODE")
    codes = subagent_standalone.agent_refusal_codes()
    assert codes == tuple(getattr(subagent, name) for name in names)


@pytest.mark.parametrize(
    ("name", "reader"),
    [
        ("REMOTE_COST_BUCKET_SUFFIX", "remote_cost_bucket_suffix"),
        ("MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE", "memory_cause_cgroup_usage_unreadable"),
    ],
)
def test_each_constant_reader_answers_the_facade_binding(
    monkeypatch: pytest.MonkeyPatch, name: str, reader: str
) -> None:
    assert getattr(subagent_standalone, reader)() == getattr(subagent, name)
    monkeypatch.setattr(subagent, name, "patched")
    assert getattr(subagent_standalone, reader)() == "patched"
