"""GATE F2 — contract conformance for the dynamic-workflows frozen ``ctx`` surface.

This is the canary that locks the FROZEN contract in
``kiro_crew/workflows/__init__.py`` (the ``WorkflowContext`` Protocol, the Port
protocols, the exception set, and the run-event schema). Every other
dynamic-workflows tributary — runner, DSL, UI app, schema enforcement, native
primitives — fans out from that contract, so a silent signature/event change here
would break consumers downstream without warning.

If a case below turns RED, you changed the contract. That is allowed only as an
explicit *re-freeze*: update ``workflows/__init__.py``, the module spec
(``docs/system-specs/modules/workflows.md``), AND this test in the same change
(see ``docs/system-specs/modules/workflow-gates.md`` gate F2 and the dir-scoped
``src/kiro_crew/workflows/AGENTS.md``). If you did NOT intend a re-freeze, revert
the contract edit — do not "fix" the expectation here.

No implementation is exercised here: this asserts the *shape* of the public
freeze, which is exactly what M1's downstream units depend on.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Callable

import pytest

import kiro_crew.workflows as wf

# --------------------------------------------------------------------------- #
# Expected public surface (the freeze). Keep these literals in lockstep with
# workflows/__init__.py + the module spec. Editing one without the others is a
# re-freeze violation and this module is where it surfaces.
# --------------------------------------------------------------------------- #

EXPECTED_ALL = {
    "WorkflowContext",
    "Budget",
    "CronPort",
    "MemoryPort",
    "LearnPort",
    "KnowledgePort",
    "BudgetExceeded",
    "WorkflowEvent",
    "EVENT_TYPES",
    "AgentResult",
    "Thunk",
}

EXPECTED_EVENT_TYPES = (
    "run_started",
    "phase_started",
    "agent_started",
    "agent_progress",
    "agent_finished",
    "log",
    "budget_update",
    "approval_requested",
    "run_finished",
    "run_failed",
    "run_cancelled",
)

# Data attributes declared on the WorkflowContext Protocol (annotations, not
# methods). These are the inputs/ports the runner must populate on every ctx.
EXPECTED_CTX_ATTRS = {
    "args",
    "now",
    "owner_dm",
    "budget",
    "cron",
    "memory",
    "learn",
    "knowledge",
}

# Per-method expected signature on WorkflowContext: (is_async, ordered param list).
# Each param is (name, kind, has_default). ``self`` is included so an accidental
# drop of a leading positional is caught. ``*stages`` is VAR_POSITIONAL.
P = inspect.Parameter
EXPECTED_CTX_METHODS: dict[str, tuple[bool, list[tuple[str, str, bool]]]] = {
    "agent": (
        True,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("prompt", "POSITIONAL_OR_KEYWORD", False),
            ("label", "KEYWORD_ONLY", True),
            ("phase", "KEYWORD_ONLY", True),
            ("schema", "KEYWORD_ONLY", True),
            ("model", "KEYWORD_ONLY", True),
            ("agent", "KEYWORD_ONLY", True),
            ("effort", "KEYWORD_ONLY", True),
            ("cwd", "KEYWORD_ONLY", True),
            ("session", "KEYWORD_ONLY", True),
            ("nudge", "KEYWORD_ONLY", True),
        ],
    ),
    "parallel": (
        True,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("thunks", "POSITIONAL_OR_KEYWORD", False),
        ],
    ),
    "pipeline": (
        True,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("items", "POSITIONAL_OR_KEYWORD", False),
            ("stages", "VAR_POSITIONAL", False),
        ],
    ),
    "workflow": (
        True,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("name", "POSITIONAL_OR_KEYWORD", False),
            ("args", "POSITIONAL_OR_KEYWORD", True),
        ],
    ),
    "phase": (
        False,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("title", "POSITIONAL_OR_KEYWORD", False),
        ],
    ),
    "log": (
        False,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("message", "POSITIONAL_OR_KEYWORD", False),
        ],
    ),
    "nudge": (
        False,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("idle_secs", "KEYWORD_ONLY", False),
            ("message", "KEYWORD_ONLY", False),
            ("max_cycles", "KEYWORD_ONLY", True),
        ],
    ),
    "approve": (
        True,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("prompt", "POSITIONAL_OR_KEYWORD", False),
        ],
    ),
    "send_slack": (
        True,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("target", "POSITIONAL_OR_KEYWORD", False),
            ("text", "POSITIONAL_OR_KEYWORD", False),
        ],
    ),
    "send_message": (
        True,
        [
            ("self", "POSITIONAL_OR_KEYWORD", False),
            ("channel", "POSITIONAL_OR_KEYWORD", False),
            ("text", "POSITIONAL_OR_KEYWORD", False),
        ],
    ),
}

# Port protocols: name -> (is_async, ordered param list) for each declared method.
EXPECTED_PORT_METHODS: dict[str, dict[str, tuple[bool, list[tuple[str, str, bool]]]]] = {
    "Budget": {
        "spent": (False, [("self", "POSITIONAL_OR_KEYWORD", False)]),
        "remaining": (False, [("self", "POSITIONAL_OR_KEYWORD", False)]),
    },
    "CronPort": {
        "ensure": (
            False,
            [
                ("self", "POSITIONAL_OR_KEYWORD", False),
                ("name", "POSITIONAL_OR_KEYWORD", False),
                ("cron_expr", "KEYWORD_ONLY", False),
                ("workflow", "KEYWORD_ONLY", False),
                ("kw", "VAR_KEYWORD", False),
            ],
        ),
        "add": (
            False,
            [
                ("self", "POSITIONAL_OR_KEYWORD", False),
                ("name", "POSITIONAL_OR_KEYWORD", False),
                ("every_secs", "KEYWORD_ONLY", True),
                ("cron_expr", "KEYWORD_ONLY", True),
                ("workflow", "KEYWORD_ONLY", False),
                ("kw", "VAR_KEYWORD", False),
            ],
        ),
        "remove": (
            False,
            [
                ("self", "POSITIONAL_OR_KEYWORD", False),
                ("name", "POSITIONAL_OR_KEYWORD", False),
            ],
        ),
    },
    "MemoryPort": {
        "get": (
            False,
            [
                ("self", "POSITIONAL_OR_KEYWORD", False),
                ("key", "POSITIONAL_OR_KEYWORD", False),
                ("default", "POSITIONAL_OR_KEYWORD", True),
            ],
        ),
        "set": (
            False,
            [
                ("self", "POSITIONAL_OR_KEYWORD", False),
                ("key", "POSITIONAL_OR_KEYWORD", False),
                ("value", "POSITIONAL_OR_KEYWORD", False),
            ],
        ),
    },
    "LearnPort": {
        "add": (
            False,
            [
                ("self", "POSITIONAL_OR_KEYWORD", False),
                ("rule", "POSITIONAL_OR_KEYWORD", False),
                ("scope", "POSITIONAL_OR_KEYWORD", True),
            ],
        ),
    },
    "KnowledgePort": {
        "search": (
            True,
            [
                ("self", "POSITIONAL_OR_KEYWORD", False),
                ("query", "POSITIONAL_OR_KEYWORD", False),
            ],
        ),
    },
}


def _sig_params(func: Callable[..., object]) -> list[tuple[str, str, bool]]:
    """Return [(name, kind_name, has_default)] for a function's parameters."""
    params = inspect.signature(func).parameters.values()
    return [(p.name, p.kind.name, p.default is not P.empty) for p in params]


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #


def test_all_exports_exact() -> None:
    """``__all__`` is the public freeze and must not drift silently."""
    assert set(wf.__all__) == EXPECTED_ALL
    for name in EXPECTED_ALL:
        assert hasattr(wf, name), f"workflows.{name} is exported but missing"


def test_budget_exceeded_is_exception() -> None:
    assert issubclass(wf.BudgetExceeded, Exception)


# --------------------------------------------------------------------------- #
# WorkflowContext Protocol shape
# --------------------------------------------------------------------------- #


def test_ctx_data_attributes_exact() -> None:
    """The non-method (data/port) attributes declared on the Protocol."""
    annotated = set(getattr(wf.WorkflowContext, "__annotations__", {}))
    assert annotated == EXPECTED_CTX_ATTRS


def test_ctx_method_set_exact() -> None:
    """No ctx method added/removed without a re-freeze touching this test."""
    actual = {
        name
        for name, member in vars(wf.WorkflowContext).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }
    assert actual == set(EXPECTED_CTX_METHODS)


@pytest.mark.parametrize("method_name", sorted(EXPECTED_CTX_METHODS))
def test_ctx_method_signature(method_name: str) -> None:
    """Each ctx primitive matches its frozen signature (param names/kinds/defaults)."""
    is_async, expected_params = EXPECTED_CTX_METHODS[method_name]
    func = getattr(wf.WorkflowContext, method_name)
    assert (
        asyncio.iscoroutinefunction(func) is is_async
    ), f"{method_name}: async-ness changed (expected async={is_async})"
    assert _sig_params(func) == expected_params


# --------------------------------------------------------------------------- #
# Port protocols
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("port_name", sorted(EXPECTED_PORT_METHODS))
def test_port_runtime_checkable(port_name: str) -> None:
    """Ports are @runtime_checkable so the runner can duck-type wrappers."""
    port = getattr(wf, port_name)
    # runtime_checkable protocols expose _is_runtime_protocol = True.
    assert getattr(port, "_is_runtime_protocol", False), f"{port_name} must stay @runtime_checkable"


@pytest.mark.parametrize(
    "port_name,method_name",
    [(p, m) for p, methods in EXPECTED_PORT_METHODS.items() for m in methods],
)
def test_port_method_signature(port_name: str, method_name: str) -> None:
    is_async, expected_params = EXPECTED_PORT_METHODS[port_name][method_name]
    func = getattr(getattr(wf, port_name), method_name)
    assert asyncio.iscoroutinefunction(func) is is_async
    assert _sig_params(func) == expected_params


# --------------------------------------------------------------------------- #
# Run-event schema (JSON only — BSC12)
# --------------------------------------------------------------------------- #


def test_event_types_exact_and_ordered() -> None:
    """The runner emits exactly these event types; UI/resume key off them."""
    assert wf.EVENT_TYPES == EXPECTED_EVENT_TYPES


def test_event_round_trips_through_json() -> None:
    """``WorkflowEvent`` (de)serializes via to_json/from_json — never pickle."""
    ev = wf.WorkflowEvent(
        run_id="wf_abc",
        seq=3,
        ts="2026-06-18T00:00:00Z",
        type="agent_finished",
        data={"agent_id": "a1", "ok": True, "result_summary": "done"},
    )
    obj = ev.to_json()
    assert obj == {
        "run_id": "wf_abc",
        "seq": 3,
        "ts": "2026-06-18T00:00:00Z",
        "type": "agent_finished",
        "data": {"agent_id": "a1", "ok": True, "result_summary": "done"},
    }
    back = wf.WorkflowEvent.from_json(obj)
    assert back == ev


def test_event_envelope_keys_exact() -> None:
    """The on-wire envelope is exactly run_id/seq/ts/type/data."""
    ev = wf.WorkflowEvent(run_id="wf_x", seq=0, ts="t", type="run_started")
    assert set(ev.to_json()) == {"run_id", "seq", "ts", "type", "data"}


@pytest.mark.parametrize("event_type", EXPECTED_EVENT_TYPES)
def test_every_event_type_serializes(event_type: str) -> None:
    ev = wf.WorkflowEvent(run_id="wf_x", seq=1, ts="t", type=event_type)
    assert ev.to_json()["type"] == event_type


def test_unknown_event_type_rejected_on_serialize() -> None:
    """An out-of-contract event type must fail loudly, not ship silently."""
    ev = wf.WorkflowEvent(run_id="wf_x", seq=1, ts="t", type="not_a_real_event")
    with pytest.raises(ValueError):
        ev.to_json()


def test_from_json_defaults_missing_data_to_empty_dict() -> None:
    obj = {"run_id": "wf_x", "seq": 2, "ts": "t", "type": "log"}
    ev = wf.WorkflowEvent.from_json(obj)
    assert ev.data == {}
