"""The work-ledger body reader refuses ``NaN``, ``Infinity`` and an overflowing float.

Python's ``json`` parser accepts all three, but they are not JSON. A body that
carries one reached a ledger field, and every later strict-JSON read of the
board then failed. ``_json_object`` now parses strictly and answers 400
``invalid_json`` instead.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.dashboard.handlers.work_ledger_refusals import _json_object


class _Req:
    """The one ``web.Request`` method ``_json_object`` calls, over a fixed body."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def json(self, *, loads: Any = json.loads) -> Any:
        return loads(self._text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        '{"acceptance": NaN}',
        '{"pr": Infinity}',
        '{"pr": -Infinity}',
        '{"artifacts": {"n": 1e999}}',
    ],
)
async def test_a_non_finite_number_is_invalid_json(text):
    body, refusal = await _json_object(_Req(text))  # type: ignore[arg-type]
    assert body is None
    assert refusal is not None and refusal.status == 400
    assert json.loads(refusal.text).get("code") == "invalid_json"


@pytest.mark.asyncio
async def test_finite_numbers_still_parse():
    body, refusal = await _json_object(_Req('{"pr": 12, "x": 1.5, "y": -2e3}'))  # type: ignore[arg-type]
    assert refusal is None
    assert body == {"pr": 12, "x": 1.5, "y": -2000.0}
