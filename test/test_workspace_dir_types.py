"""A workspace ``dir`` that is not a string is refused with 400, not a 500.

``api_workspaces_create`` and ``api_workspaces_update`` hand ``dir`` to ``Path()``
and to set lookups, which raise ``TypeError`` on a number, list, object, bool or
null. ``name`` and ``copy_from`` already answer ``400 invalid_field_type`` for
the same input; ``dir`` now does too, and every string keeps its answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_workspace_crud_handlers import _CFGDIR, _DATAHOME, _LOAD, _cfg, _req

from kiro_crew.dashboard.handlers import api_workspaces_create, api_workspaces_update

_NOT_STRINGS = [5, 1.5, True, None, ["a"], {"a": 1}]


def _expect_type_refusal(resp) -> None:
    assert resp.status == 400, resp.body
    assert json.loads(resp.body) == {"error": "dir must be a string", "code": "invalid_field_type"}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", _NOT_STRINGS)
async def test_create_with_a_non_string_dir_is_400(tmp_path: Path, value) -> None:
    with (
        patch(_LOAD, return_value=_cfg()),
        patch(_CFGDIR, return_value=tmp_path),
        patch(_DATAHOME, return_value=tmp_path),
    ):
        try:
            resp = await api_workspaces_create(_req({"name": "fresh", "dir": value}))
        except TypeError as exc:
            pytest.fail(f"a non-string dir raised {exc!r}")
    _expect_type_refusal(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", _NOT_STRINGS)
async def test_create_copying_with_a_non_string_dir_is_400(tmp_path: Path, value) -> None:
    with (
        patch(_LOAD, return_value=_cfg()),
        patch(_CFGDIR, return_value=tmp_path),
        patch(_DATAHOME, return_value=tmp_path),
    ):
        try:
            resp = await api_workspaces_create(
                _req({"name": "fresh", "copy_from": "default", "dir": value})
            )
        except TypeError as exc:
            pytest.fail(f"a non-string dir raised {exc!r}")
    _expect_type_refusal(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", _NOT_STRINGS)
async def test_update_with_a_non_string_dir_is_400(tmp_path: Path, value) -> None:
    with (
        patch(_LOAD, return_value=_cfg()),
        patch(_CFGDIR, return_value=tmp_path),
        patch(_DATAHOME, return_value=tmp_path),
    ):
        try:
            resp = await api_workspaces_update(_req({"dir": value}, match_info={"name": "default"}))
        except TypeError as exc:
            pytest.fail(f"a non-string dir raised {exc!r}")
    _expect_type_refusal(resp)
