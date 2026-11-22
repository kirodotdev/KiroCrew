"""An app's tool audit rows through ``POST /api/apps/{name}/_sel``.

A backend records a tool use as an ordinary ``api_access`` row, operation
``tool.<name>``, which the route files under the app's own namespace
(``<app id>.tool.<name>``). There is no tool-specific route method, so an app cannot
write the gateway's ``tool_invocation`` or ``governance_decision`` kinds, name another
app, or make a row look like a gateway one.

An ``operation`` is normalized and its row is kept: credentials are redacted, every
character outside ``[A-Za-z0-9_.-]`` becomes ``_``, the result is clipped to 64
characters, and an empty one is stored as ``unnamed``. No newline or control character
reaches a stored row.

``critical`` stays a flag on that one row. In ``sel.py`` it only makes the write
synchronous and re-raises a filesystem failure (``log`` and ``log_api_access``); no
alert and no owner prompt hangs off it, which the last test pins at the route.
"""

# flake8: noqa: F811  -- ``rec`` is a fixture imported from test_sel_routes
from __future__ import annotations

import pytest
from test_sel_routes import _A, _B, _client, _post, _tok, no_part_of, rec  # noqa: F401


def _api(operation, **extra):
    return {
        "method": "log_api_access",
        "kwargs": {"operation": operation, "outcome": "ok", **extra},
    }


@pytest.mark.asyncio
async def test_a_tool_row_is_an_api_access_row_under_the_apps_tool_namespace(rec) -> None:
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), _api("tool.git", resources="r")) == 204
    assert rec.rows == [
        (
            "log_api_access",
            {
                "caller": f"app:{_A}",
                "source": "app",
                "operation": f"{_A}.tool.git",
                "outcome": "ok",
                "resources": "r",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["log_app_tool", "log_tool_invocation", "log_governance_decision"]
)
async def test_an_app_cannot_write_any_method_but_api_access(rec, method) -> None:
    body = {"method": method, "kwargs": {"name": "git", "outcome": "ok"}}
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), body) == 400
    assert rec.rows == []


@pytest.mark.asyncio
async def test_a_spoofed_app_id_in_the_body_is_ignored(rec) -> None:
    async with _client() as client:
        token = _tok(_A)
        for key in ("app", "app_id", "caller"):
            assert await _post(client, _A, token, _api("tool.git", **{key: _B})) == 400
        # The operation is the only text that reaches the row, and it is prefixed.
        assert await _post(client, _A, token, _api(f"tool.{_B}.x")) == 204
    assert [kw["operation"] for _n, kw in rec.app_rows()] == [f"{_A}.tool.{_B}.x"]
    assert [kw["caller"] for _n, kw in rec.app_rows()] == [f"app:{_A}"]


@pytest.mark.asyncio
async def test_critical_on_an_app_tool_row_only_flags_its_own_api_access_row(rec) -> None:
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), _api("tool.git", critical=True)) == 204
    # One api_access row carrying the flag; no gateway tool or governance writer ran.
    assert [(name, kw["critical"]) for name, kw in rec.rows] == [("log_api_access", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "stored"),
    [
        ("", "unnamed"),
        ("a b", "a_b"),
        ("a\nb", "a_b"),
        ("a\x00b", "a_b"),
        ("a/b", "a_b"),
        ("a:b", "a_b"),
        ("https://u:p@h/x", "https___u_p_h_x"),
        ("é", "_"),
        ("X" * 65, "X" * 64),
    ],
)
async def test_an_operation_outside_the_charset_or_length_is_normalized_and_kept(
    rec, operation, stored
) -> None:
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), _api(operation)) == 204
    assert [kw["operation"] for _n, kw in rec.app_rows()] == [f"{_A}.{stored}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [7, None, ["x"]])
async def test_an_operation_that_is_not_a_string_is_a_400(rec, operation) -> None:
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), _api(operation)) == 400
    assert rec.rows == []


@pytest.mark.asyncio
async def test_a_mixed_case_operation_is_taken_and_kept_as_written(rec) -> None:
    """md-notebook's existing ``md_notebook.settings.autoSync`` must keep its row history."""
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), _api("md_notebook.settings.autoSync")) == 204
    assert [kw["operation"] for _n, kw in rec.app_rows()] == [f"{_A}.md_notebook.settings.autoSync"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["AKIAIOSFODNN7EXAMPLE", "xoxb-1234567890-abcdefghij"])
async def test_a_credential_shaped_operation_gets_its_row_redacted(rec, operation) -> None:
    """The row is kept and no part of the credential reaches it."""
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), _api(operation)) == 204
    (row,) = rec.app_rows()
    assert no_part_of(operation, row[1]["operation"])
    assert row[1]["operation"].startswith(f"{_A}.")


@pytest.mark.asyncio
async def test_a_dotted_64_character_operation_is_taken(rec) -> None:
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), _api("a-b_c.0" * 9 + "x")) == 204
