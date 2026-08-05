"""Tests for YAML workflow decomposition (decompose_yaml + _check_acyclic)."""

from __future__ import annotations

import pytest

from kiro_crew.task_planner import _check_acyclic, decompose_yaml, plan_to_yaml

# ── Happy path ──


def test_valid_yaml_with_deps():
    yaml = """
agents:
  setup:
    prompt: "create dirs"
  build:
    depends_on: [setup]
    prompt: "run build"
  test:
    depends_on: [build]
    prompt: "run tests"
"""
    tasks = decompose_yaml(yaml)
    assert len(tasks) == 3
    assert tasks[0].depends_on == []
    assert tasks[1].depends_on == [1]  # setup index
    assert tasks[2].depends_on == [2]  # build index


def test_shell_fallback():
    yaml = """
agents:
  runner:
    shell: "echo hello"
"""
    tasks = decompose_yaml(yaml)
    assert "echo hello" in tasks[0].description


def test_agent_with_all_allowed_keys():
    yaml = """
agents:
  worker:
    agent: my-agent
    timeout: 10m
    depends_on: []
    description: My Worker
    prompt: do stuff
    shell: fallback
"""
    tasks = decompose_yaml(yaml)
    assert len(tasks) == 1
    assert tasks[0].title == "My Worker"


# ── Cycle detection ──

def test_cycle_a_b_a():
    yaml = """
agents:
  a:
    depends_on: [b]
    prompt: x
  b:
    depends_on: [a]
    prompt: y
"""
    with pytest.raises(ValueError, match="[Cc]ycle"):
        decompose_yaml(yaml)


def test_self_dependency():
    yaml = """
agents:
  a:
    depends_on: [a]
    prompt: x
"""
    with pytest.raises(ValueError, match="[Cc]ycle"):
        decompose_yaml(yaml)


# ── Unknown dependency ──

def test_unknown_dependency():
    yaml = """
agents:
  a:
    depends_on: [nonexistent]
    prompt: x
"""
    with pytest.raises(ValueError, match="unknown agent"):
        decompose_yaml(yaml)


# ── Bad keys ──

def test_bad_keys_rejected():
    yaml = """
agents:
  a:
    prompt: x
    unknown_key: foo
"""
    with pytest.raises(ValueError, match="unknown keys"):
        decompose_yaml(yaml)


# ── Empty / malformed input ──

def test_empty_string():
    with pytest.raises(ValueError, match="agents"):
        decompose_yaml("")


def test_none_yaml():
    with pytest.raises(ValueError, match="agents"):
        decompose_yaml("null")


def test_missing_agents_key():
    with pytest.raises(ValueError, match="agents"):
        decompose_yaml("foo: bar")


def test_agents_not_a_dict():
    with pytest.raises(ValueError, match="mapping"):
        decompose_yaml("agents: hello")


def test_empty_agents():
    with pytest.raises(ValueError, match="empty"):
        decompose_yaml("agents: {}")


# ── Size limits ──

def test_yaml_too_large():
    big = "agents:\n  a:\n    prompt: " + "x" * (256 * 1024 + 1)
    with pytest.raises(ValueError, match="too large"):
        decompose_yaml(big)


def test_too_many_agents():
    lines = ["agents:"]
    for i in range(51):
        lines.append(f"  agent_{i}:")
        lines.append(f"    prompt: task {i}")
    with pytest.raises(ValueError, match="Too many agents"):
        decompose_yaml("\n".join(lines))


# ── _check_acyclic directly (iterative DFS) ──

def test_check_acyclic_no_cycle():
    from kiro_crew.task_models import Task
    tasks = [
        Task(index=1, title="a", description="", depends_on=[]),
        Task(index=2, title="b", description="", depends_on=[1]),
    ]
    _check_acyclic(tasks)  # should not raise


def test_check_acyclic_deep_chain():
    """Iterative DFS handles chains deeper than Python's recursion limit."""
    from kiro_crew.task_models import Task
    n = 1500
    # Each task depends on the *next* one so DFS from task 1 must chase the full chain (stack depth n).
    tasks = [Task(index=i, title=f"t{i}", description="", depends_on=[i + 1] if i < n else []) for i in range(1, n + 1)]
    _check_acyclic(tasks)  # should not raise RecursionError


# ── YAML type coercion guards ──

def test_non_string_agent_name():
    yaml = "agents:\n  123:\n    prompt: x\n"
    with pytest.raises(ValueError, match="must be a string"):
        decompose_yaml(yaml)


def test_non_string_depends_on_entry():
    yaml = "agents:\n  a:\n    depends_on: [1]\n    prompt: x\n"
    with pytest.raises(ValueError, match="must be strings"):
        decompose_yaml(yaml)


def test_bool_agent_name():
    yaml = "agents:\n  yes:\n    prompt: x\n"
    with pytest.raises(ValueError, match="must be a string"):
        decompose_yaml(yaml)


def test_bool_depends_on_entry():
    yaml = "agents:\n  a:\n    depends_on: [yes]\n    prompt: x\n"
    with pytest.raises(ValueError, match="must be strings"):
        decompose_yaml(yaml)


def test_depends_on_null_treated_as_empty():
    yaml = "agents:\n  a:\n    depends_on:\n    prompt: x\n"
    tasks = decompose_yaml(yaml)
    assert tasks[0].depends_on == []


def test_decompose_yaml_without_pyyaml(monkeypatch):
    """ImportError path when PyYAML is not installed."""
    import builtins

    real_import = builtins.__import__

    def mock_import(name, *a, **kw):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    with pytest.raises(ImportError, match="PyYAML is required"):
        decompose_yaml("agents:\n  a:\n    prompt: x\n")


# ── plan_to_yaml (export, inverse of decompose_yaml) ──


def test_plan_to_yaml_roundtrips_titles_and_dag():
    """Serialize → decompose reconstructs the same titles and dependency structure."""
    from kiro_crew.task_models import Task

    tasks = [
        Task(index=1, title="Set up DB", description="create schema"),
        Task(index=2, title="Wire API", description="build endpoints", depends_on=[1]),
        Task(index=3, title="Add tests", description="write tests", depends_on=[1, 2]),
    ]
    y = plan_to_yaml(tasks)
    rt = decompose_yaml(y)
    assert [t.title for t in rt] == ["Set up DB", "Wire API", "Add tests"]
    # index 1 has no deps; 2 depends on 1; 3 depends on 1 and 2 (by name → index)
    assert rt[0].depends_on == []
    assert rt[1].depends_on == [1]
    assert sorted(rt[2].depends_on) == [1, 2]


def test_plan_to_yaml_dedups_duplicate_titles():
    """Two tasks with the same title get distinct agent keys (foo / foo-2)."""
    from kiro_crew.task_models import Task

    tasks = [
        Task(index=1, title="Do work", description="a"),
        Task(index=2, title="Do work", description="b", depends_on=[1]),
    ]
    y = plan_to_yaml(tasks)
    assert "do-work:" in y and "do-work-2:" in y
    rt = decompose_yaml(y)
    assert len(rt) == 2
    assert rt[1].depends_on == [1]  # dedup didn't break the dep mapping


def test_plan_to_yaml_extracts_agent_timeout_preamble():
    """A description carrying the import preamble round-trips back into agent/timeout keys."""
    from kiro_crew.task_models import Task

    tasks = [Task(index=1, title="Build", description="Agent: coder\nTimeout: 30m\n\nrun the build")]
    y = plan_to_yaml(tasks)
    assert "agent: coder" in y
    assert "timeout: 30m" in y
    assert "run the build" in y
    # the preamble is not double-wrapped into the prompt
    assert "Agent: coder" not in y


def test_plan_to_yaml_preamble_multiparagraph_prompt_not_mis_split():
    """DOTALL must not let a blank-line-containing prompt bleed into the timeout
    capture — agent/timeout are single-line, the prompt keeps all paragraphs."""
    from kiro_crew.task_models import Task

    desc = "Agent: coder\nTimeout: 30m\n\nFirst paragraph.\n\nSecond paragraph."
    tasks = [Task(index=1, title="Build", description=desc)]
    y = plan_to_yaml(tasks)
    # timeout stays exactly "30m" (would be a quoted/block scalar under the bug)
    assert "timeout: 30m" in y
    assert "agent: coder" in y
    # both paragraphs survive in the prompt
    assert "First paragraph." in y
    assert "Second paragraph." in y


def test_plan_to_yaml_blank_title_falls_back_to_task_index():
    from kiro_crew.task_models import Task

    tasks = [Task(index=5, title="", description="something")]
    y = plan_to_yaml(tasks)
    assert "task-5:" in y


def test_plan_to_yaml_empty_raises():
    with pytest.raises(ValueError, match="no tasks"):
        plan_to_yaml([])
