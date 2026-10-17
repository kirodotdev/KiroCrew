"""Workflow budget bindings are host-owned, while script state stays mutable."""

from __future__ import annotations

from textwrap import indent

import pytest

from kiro_crew.workflows.runner import WorkflowRunner
from kiro_crew.workflows.validate import CORE_CTX_SURFACE, check_ctx_surface, validate

NOW = "2026-01-01T00:00:00Z"


def _script(body: str, *, signature: str = "ctx") -> str:
    return (
        'META = {"name": "budget-binding"}\n'
        f"async def workflow({signature}):\n" + indent(body, "    ") + "\n"
    )


@pytest.mark.parametrize("signature", ["ctx", "ctx, /", "ctx, /, optional=None"])
@pytest.mark.parametrize(
    "statement",
    [
        "ctx.budget = 7200",
        "ctx.budget: int = 7200",
        "ctx.budget += 1",
        "del ctx.budget",
        "ctx.budget, local = 7200, 1",
        "[ctx.budget, local] = [7200, 1]",
        "*ctx.budget, local = [7200, 1]",
        "local = ctx.budget = 7200",
        "for ctx.budget in [7200]:\n    pass",
        "def overwrite():\n    ctx.budget = 7200\noverwrite()",
    ],
)
def test_budget_rebinding_is_rejected_before_execution(statement: str, signature: str) -> None:
    result = validate(_script(statement, signature=signature))

    assert not result.ok
    assert any("line " in error and "ctx.budget is read-only" in error for error in result.errors)
    assert any("budget_total" in error for error in result.errors)


@pytest.mark.parametrize(
    "body",
    [
        "limit = ctx.budget.total\nreturn ctx.budget.remaining()",
        "return ctx.budget.spent()",
        "ctx.args['stage'] = 'audit'\nreturn ctx.args['stage']",
        "ctx.args = {'stage': 'audit'}\nreturn ctx.args",
        "local = {'budget': 7200}\nlocal['budget'] += 1\nreturn local",
        "def helper(ctx):\n    ctx.budget = 7200\nreturn 'helper has its own parameter'",
        "def helper(ctx, /):\n    del ctx.budget\nreturn 'helper has its own parameter'",
        "def helper(*, ctx):\n    ctx.budget = 7200\nreturn 'helper has its own parameter'",
        "async def helper(ctx):\n    ctx.budget = 7200\nreturn 'helper has its own parameter'",
    ],
)
def test_budget_guard_preserves_reads_and_unrelated_state(body: str) -> None:
    result = validate(_script(body))

    assert result.ok, result.errors


@pytest.mark.asyncio
async def test_runner_rejects_budget_rebinding_before_any_agent() -> None:
    calls: list[str] = []

    async def agent(prompt: str, opts: dict):
        calls.append(prompt)
        return "unexpected"

    result = await WorkflowRunner(agent_fn=agent, audit=lambda *_: None).run(
        _script("ctx.budget = 7200\nreturn await ctx.agent('audit')"),
        run_id="budget-invalid",
        now=NOW,
        budget_total=500000,
    )

    assert not result.ok
    assert calls == []
    assert [event.type for event in result.events] == ["run_started", "run_failed"]
    assert result.events[-1].data["where"] == "validate"
    assert "ctx.budget is read-only" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", ["owner.budget = 7200", "del owner.budget"])
@pytest.mark.parametrize("budget_total", [0, 500000])
async def test_runtime_budget_binding_cannot_be_replaced_through_helper(
    statement: str, budget_total: int
) -> None:
    calls: list[str] = []

    async def agent(prompt: str, opts: dict):
        calls.append(prompt)
        return "audit complete"

    source = (
        'META = {"name": "budget-helper"}\n'
        "def overwrite(owner):\n"
        f"    {statement}\n"
        "async def workflow(ctx):\n"
        "    original = ctx.budget\n"
        "    try:\n"
        "        overwrite(ctx)\n"
        "    except AttributeError:\n"
        "        pass\n"
        "    assert ctx.budget is original\n"
        "    ctx.args['stage'] = 'audit'\n"
        "    result = await ctx.agent(ctx.args['stage'])\n"
        "    return [result, ctx.budget.total, ctx.budget.spent(), ctx.budget.remaining()]\n"
    )
    assert validate(source).ok
    result = await WorkflowRunner(agent_fn=agent, audit=lambda *_: None).run(
        source,
        run_id="budget-helper",
        now=NOW,
        budget_total=budget_total,
    )

    if budget_total == 0:
        assert not result.ok
        assert result.events[-1].data["where"] == "ceiling"
        assert "budget exhausted" in result.error
        assert calls == []
    else:
        assert result.ok, result.error
        assert calls == ["audit"]
        assert result.result == ["audit complete", budget_total, 0, float(budget_total)]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_total", [None, 500000])
async def test_script_can_read_budget_without_rebinding(budget_total: int | None) -> None:
    async def agent(prompt: str, opts: dict):
        return "audit complete"

    result = await WorkflowRunner(agent_fn=agent, audit=lambda *_: None).run(
        _script(
            "ctx.args['stage'] = 'audit'\n"
            "result = await ctx.agent(ctx.args['stage'])\n"
            "return [result, ctx.budget.total, ctx.budget.spent()]"
        ),
        run_id="budget-read",
        now=NOW,
        budget_total=budget_total,
    )

    assert result.ok, result.error
    assert result.result == ["audit complete", budget_total, 0]


@pytest.mark.asyncio
async def test_nested_helper_local_ctx_budget_is_ordinary_state() -> None:
    source = _script(
        "def helper():\n"
        "    ctx = Exception('local')\n"
        "    ctx.budget = 7200\n"
        "    return ctx.budget\n"
        "return helper()"
    )
    validation = validate(source)
    assert validation.ok, validation.errors

    async def agent(prompt: str, opts: dict):
        pytest.fail("ordinary helper state must not dispatch an agent")

    result = await WorkflowRunner(agent_fn=agent, audit=lambda *_: None).run(
        source, run_id="local-helper-budget", now=NOW
    )
    assert result.ok, result.error
    assert result.result == 7200


@pytest.mark.parametrize("helper_kind", ["def", "async def"])
@pytest.mark.parametrize(
    "body",
    [
        "ctx: Exception = Exception('local')\nctx.budget = 7200",
        "for ctx in [Exception('local')]:\n    ctx.budget = 7200",
        "try:\n    raise Exception('local')\nexcept Exception as ctx:\n    ctx.budget = 7200",
        "if (ctx := Exception('local')):\n    ctx.budget = 7200",
        "ctx = Exception('local')\n"
        "def inner():\n    nonlocal ctx\n    ctx.budget = 7200\ninner()",
    ],
    ids=["annotated", "loop", "except", "walrus", "nonlocal-to-helper"],
)
def test_helper_local_bindings_are_not_workflow_context(body: str, helper_kind: str) -> None:
    source = _script(f"{helper_kind} helper():\n" + indent(body, "    "))
    result = validate(source)
    assert result.ok, result.errors
    assert check_ctx_surface(source, CORE_CTX_SURFACE) == []
    # The host checker retains its conservative base walk, independent of ownership.
    assert check_ctx_surface(source, CORE_CTX_SURFACE - {"budget"})


@pytest.mark.parametrize(
    "body",
    [
        "ctx.budget = 7200",
        "nonlocal ctx\nctx.budget = 7200",
        "nonlocal ctx\nctx = ctx\nctx.budget = 7200",
        "nonlocal ctx\ndel ctx.budget",
        "def unrelated():\n    ctx = Exception('local')\nctx.budget = 7200",
        "[ctx for ctx in []]\nctx.budget = 7200",
    ],
    ids=["closure", "nonlocal", "nonlocal-store", "nonlocal-delete", "nested", "comprehension"],
)
@pytest.mark.parametrize("module_binding", ["", "ctx = Exception('module local')\n"])
def test_helper_references_to_entrypoint_ctx_remain_protected(
    body: str, module_binding: str
) -> None:
    source = module_binding + _script("def helper():\n" + indent(body, "    ") + "\nhelper()")
    result = validate(source)
    assert not result.ok
    assert any("ctx.budget is read-only" in error for error in result.errors)
    assert check_ctx_surface(source, CORE_CTX_SURFACE - {"budget"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_binding",
    [
        "ctx = Exception('module local')",
        "ctx: Exception = Exception('module local')",
        "ctx, other = Exception('module local'), None",
        "for ctx in [Exception('module local')]:\n    pass",
        "[(ctx := item) for item in [Exception('module local')]]",
        "def ctx():\n    pass",
        "def setup(value=(ctx := Exception('module local'))):\n    pass",
        "def setup():\n    global ctx\n    ctx = Exception('module local')\nsetup()",
    ],
    ids=["assign", "annotated", "unpack", "loop", "walrus", "function", "default", "global-store"],
)
async def test_module_bound_global_ctx_is_ordinary_state(module_binding: str) -> None:
    source = _script(
        "def helper():\n"
        "    global ctx\n"
        "    ctx.budget = 7200\n"
        "    return ctx.budget\n"
        "return helper()"
    )
    source = module_binding + "\n" + source
    validation = validate(source)
    assert validation.ok, validation.errors
    assert check_ctx_surface(source, CORE_CTX_SURFACE) == []
    # The host checker retains its conservative base walk, independent of ownership.
    assert check_ctx_surface(source, CORE_CTX_SURFACE - {"budget"})

    async def agent(prompt: str, opts: dict):
        pytest.fail("global helper state must not dispatch an agent")

    result = await WorkflowRunner(agent_fn=agent, audit=lambda *_: None).run(
        source, run_id="global-helper-budget", now=NOW
    )
    assert result.ok, result.error
    assert result.result == 7200


@pytest.mark.parametrize("statement", ["ctx.budget = 7200", "del ctx.budget"])
@pytest.mark.parametrize(
    "module_prefix",
    [
        "",
        "global ctx\n",
        "ctx: Exception\n",
        "[ctx for ctx in []]\n",
        "def unrelated():\n    ctx = Exception('local')\n",
    ],
    ids=["injected", "declaration", "annotation-only", "comprehension-local", "helper-local"],
)
@pytest.mark.asyncio
async def test_global_budget_binding_is_protected_at_runtime(
    statement: str, module_prefix: str
) -> None:
    source = module_prefix + _script(
        "def helper():\n    global ctx\n" + indent(statement, "    ") + "\noriginal = ctx.budget\n"
        "try:\n    helper()\nexcept AttributeError:\n    pass\n"
        "assert ctx.budget is original\nreturn ctx.budget.total"
    )
    validation = validate(source)
    assert validation.ok, validation.errors  # Globals are outside the authoring aid.
    assert check_ctx_surface(source, CORE_CTX_SURFACE) == []

    async def agent(prompt: str, opts: dict):
        pytest.fail("budget protection must not dispatch an agent")

    result = await WorkflowRunner(agent_fn=agent, audit=lambda *_: None).run(
        source, run_id="global-budget", now=NOW, budget_total=500000
    )
    assert result.ok, result.error
    assert result.result == 500000
    assert [event.type for event in result.events] == ["run_started", "run_finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_prefix",
    ["", "if False:\n    ctx = Exception('unused')\n", "ctx = Exception('module local')\n"],
    ids=["unrebound", "unexecuted-binding", "module-owned"],
)
async def test_runner_rejects_global_unwired_port_before_dispatch(module_prefix: str) -> None:
    statement = "await ctx.send_message('channel', 'message')"
    diagnostic = "ctx.send_message"
    calls: list[str] = []

    async def agent(prompt: str, opts: dict):
        calls.append(prompt)
        return "unexpected"

    source = module_prefix + _script(
        "async def helper():\n"
        "    global ctx\n"
        + indent(statement, "    ")
        + "\nawait ctx.agent('must not dispatch before validation')\nawait helper()"
    )
    result = await WorkflowRunner(agent_fn=agent, audit=lambda *_: None).run(
        source, run_id="unrebound-global", now=NOW
    )
    assert not result.ok
    assert result.events[-1].data["where"] == "validate"
    assert calls == []
    assert [event.type for event in result.events] == ["run_started", "run_failed"]
    assert diagnostic in result.error
    assert validate(source).ok  # Port availability is host-specific.
    assert check_ctx_surface(source, CORE_CTX_SURFACE)
    assert check_ctx_surface(source, CORE_CTX_SURFACE | {"send_message"}) == []


@pytest.mark.parametrize(
    "body, unavailable",
    [
        ("def helper(ctx):\n    return ctx.get('key')\nreturn helper({})", False),
        ("def helper(ctx, /):\n    return ctx.get('key')\nreturn helper({})", False),
        ("def helper():\n    ctx = {}\n    return ctx.get('key')\nreturn helper()", True),
    ],
    ids=["parameter", "posonly-parameter", "helper-local"],
)
def test_host_surface_preserves_base_parameter_only_shadowing(body: str, unavailable: bool) -> None:
    source = _script(body)
    validation = validate(source)
    assert validation.ok, validation.errors
    errors = check_ctx_surface(source, CORE_CTX_SURFACE)
    assert bool(errors) is unavailable
    if unavailable:
        assert len(errors) == 1
        assert "ctx.get is not available" in errors[0]
