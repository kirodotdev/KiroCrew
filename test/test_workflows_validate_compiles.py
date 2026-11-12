"""``validate`` reports a script the compiler refuses, not only one ``ast.parse``
refuses, so authoring regenerates it instead of launching a run that fails at
``exec``. See docs/system-specs/modules/workflows.md, "Compile-stage syntax errors".
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.validate import validate

HEAD = 'META = {"name": "x"}\nasync def workflow(ctx):\n'


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("    return 1\nreturn 2\n", "'return' outside function"),
        ("    return 1\nawait ctx.agent('a')\n", "'await' outside function"),
        ("    def f():\n        nonlocal q\n    return 1\n", "no binding for nonlocal 'q'"),
        # The runner compiles under ``from __future__ import annotations``.
        ("    return 1\ndef g(x: (yield)):\n    pass\n", "cannot be used within an annotation"),
    ],
)
def test_a_script_the_compiler_refuses_is_not_ok(body: str, message: str) -> None:
    result = validate(HEAD + body)
    assert result.ok is False
    assert any(
        e.startswith("syntax error: ") and message in e for e in result.errors
    ), result.errors


def test_a_script_that_compiles_stays_ok() -> None:
    result = validate(HEAD + "    x = await ctx.agent('a')\n    return x\n")
    assert result.ok is True, result.errors


REFUSED = HEAD + "    return 1\nreturn 2\n"
WARNING = "syntax error: 'return' outside function (<workflow>, line 4)"


def _service(tmp_path):
    from kiro_crew.workflows.library import WorkflowDefinitionLibrary
    from kiro_crew.workflows.service import WorkflowService

    return WorkflowService(
        sessions=None, persist=False, definition_library=WorkflowDefinitionLibrary(tmp_path)
    )


def test_validate_keeps_the_compile_error_out_of_save_errors() -> None:
    result = validate(REFUSED)
    assert result.ok is False
    assert result.compile_errors == [WARNING]
    assert result.save_errors == []


def test_save_and_update_keep_saving_and_return_the_compile_error_as_a_warning(tmp_path):
    svc = _service(tmp_path)
    saved = svc.save_definition(REFUSED, name="x")
    assert saved["ok"] is True, saved
    assert saved["warnings"] == [WARNING]
    definition = saved["definition"]
    updated = svc.update_definition(
        definition["id"], source=REFUSED + "\n", expected_revision=definition["revision"]
    )
    assert updated["ok"] is True, updated
    assert updated["warnings"] == [WARNING]
    clean = svc.save_definition(HEAD + "    return 1\n", name="y")
    assert clean["ok"] is True and "warnings" not in clean


@pytest.mark.asyncio
async def test_start_still_refuses_the_script() -> None:
    from kiro_crew.workflows.service import WorkflowService

    out = await WorkflowService(sessions=None, persist=False).start(REFUSED)
    assert out == {"error": WARNING, "errors": [WARNING]}


@pytest.mark.asyncio
async def test_the_save_card_accepts_what_the_library_saves() -> None:
    from kiro_crew.change_card_catalog_kinds import KIND_WORKFLOW_SAVE
    from kiro_crew.dashboard.change_cards_readers import _workflow_context

    assert await _workflow_context(KIND_WORKFLOW_SAVE, {"source": REFUSED}, {}, None) == {}
