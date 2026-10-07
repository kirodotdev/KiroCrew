"""A workflow step's spawn resolves to the slot of the chat that started the run.

The spawn approval callback auto-approves only when its slot resolver names a
trusted slot. A workflow step's session is ``<prefix>:<run_id>:...`` (per-call,
pooled, unpooled or memory-scoped worker), which no tab displays, so the resolver
must map it through the workflow registry to the run's recorded origin
``session_key``. An unknown run or a blank origin must resolve to ``""`` so the
spawn still prompts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.slack import gateway as gw
from kiro_crew.workflows.registry import RunHandle, RunRegistry


def _service(*handles: RunHandle) -> SimpleNamespace:
    """A workflow service exposing a REAL registry, so ``get`` keeps its contract."""
    registry = RunRegistry(store=None)
    for handle in handles:
        registry.register(handle, persist=False)
    return SimpleNamespace(registry=registry)


@pytest.mark.parametrize(
    "parent",
    [
        "wf:run1:0",
        "wf:run1:12",
        "wf-pool:run1:w3",
        "wf-unpooled:run1:0",
        "wf-worker:run1:" + "a" * 64,
    ],
)
def test_step_parent_resolves_to_origin_slot(parent: str) -> None:
    svc = _service(RunHandle(run_id="run1", name="w", session_key="dashboard:chat-7"))
    expected = gw.subagent_event_slot("dashboard:chat-7")
    assert expected == "chat-7"
    assert gw._spawn_parent_slot(parent, svc) == expected


def test_unknown_run_prompts() -> None:
    svc = _service(RunHandle(run_id="run1", name="w", session_key="dashboard:chat-7"))
    assert gw._spawn_parent_slot("wf:other:0", svc) == ""
    assert gw._spawn_parent_slot("wf-pool:other:w0", svc) == ""


def test_blank_origin_prompts() -> None:
    svc = _service(
        RunHandle(run_id="blank", name="w", session_key=""),
        RunHandle(run_id="none", name="w", session_key=None),  # type: ignore[arg-type]
    )
    assert gw._spawn_parent_slot("wf:blank:0", svc) == ""
    assert gw._spawn_parent_slot("wf:none:0", svc) == ""


def test_no_workflow_service_prompts() -> None:
    assert gw._spawn_parent_slot("wf:run1:0", None) == ""


def test_registry_failure_prompts() -> None:
    def _boom(run_id: str) -> RunHandle:
        raise RuntimeError(f"registry down for {run_id}")

    svc = SimpleNamespace(registry=SimpleNamespace(get=MagicMock(side_effect=_boom)))
    assert gw._spawn_parent_slot("wf:run1:0", svc) == ""
    svc.registry.get.assert_called_once_with("run1")


def test_non_wf_parent_unchanged() -> None:
    def _no_lookup(run_id: str) -> RunHandle:
        raise AssertionError(f"non-wf key looked up run {run_id}")

    svc = SimpleNamespace(registry=SimpleNamespace(get=MagicMock(side_effect=_no_lookup)))
    for key in ("dashboard:chat-7", "cron:job1", "slack:1785370133.085469", "wf-author:x:0"):
        assert gw._spawn_parent_slot(key, svc) == gw.subagent_event_slot(key)
    assert gw._spawn_parent_slot("", svc) == ""
    svc.registry.get.assert_not_called()
