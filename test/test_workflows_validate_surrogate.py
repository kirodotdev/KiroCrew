"""A script holding a lone UTF-16 surrogate is a validation error, not a crash.

JSON allows ``"\\ud800"`` and a decoded request body can carry it; encoding it
raised ``UnicodeEncodeError`` out of ``validate``, which the routes turned into a
500. See docs/system-specs/modules/workflows.md, ``validate``.
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.service import WorkflowService
from kiro_crew.workflows.validate import validate

SOURCE = 'META = {"name": "x"}\nasync def workflow(ctx):\n    return "\ud800"\n'


def test_validate_reports_an_unpaired_surrogate() -> None:
    result = validate(SOURCE)
    assert result.ok is False
    assert result.errors == ["script contains an unpaired surrogate character"]


@pytest.mark.asyncio
async def test_start_answers_an_unpaired_surrogate_with_a_validation_error() -> None:
    svc = WorkflowService(sessions=None, persist=False)
    out = await svc.start(SOURCE)
    assert out == {
        "error": "script contains an unpaired surrogate character",
        "errors": ["script contains an unpaired surrogate character"],
    }
