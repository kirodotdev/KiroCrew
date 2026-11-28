"""The Crew API reference pages are what the spec renders.

``scripts/generate_crew_api_reference.py`` writes ``docs/reference/crew-api/`` from the
spec under ``docs/request-for-change/assets/crew-api/``. A change to the spec regenerates
the pages in the same change: ``python scripts/generate_crew_api_reference.py``.

The spec in the unit tests below is invented.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "generate_crew_api_reference.py"
_spec = importlib.util.spec_from_file_location("generate_crew_api_reference", _SCRIPT)
assert _spec is not None and _spec.loader is not None
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)


def _docs() -> dict[str, dict[str, Any]]:
    widget = {"$ref": "./components.yaml#/components/schemas/Widget"}
    public = {
        "tags": [{"name": "widgets", "description": "Widgets"}],
        "paths": {
            "/v1/widgets/{widget_id}": {
                "get": {
                    "summary": "Read a widget",
                    "tags": ["widgets"],
                    "security": [
                        {"delegatedCredential": ["widgets:read"]},
                        {"firstPartySession": []},
                    ],
                    "parameters": [
                        {
                            "name": "widget_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {"application/json": {"schema": widget}},
                        },
                        "404": {"$ref": "./components.yaml#/components/responses/NotFound"},
                    },
                    "x-crew-status": "available",
                    "x-crew-available-to": "owner",
                    "x-crew-stability": "preview",
                },
                "delete": {
                    "summary": "Delete a widget",
                    "tags": ["widgets"],
                    "security": [{"firstPartySession": []}],
                    "responses": {"204": {"description": "Deleted"}},
                },
            }
        },
    }
    components = {
        "components": {
            "schemas": {
                "Widget": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "description": "The widget's id | <opaque>"},
                        "size": {"type": "string", "enum": ["small", "large"]},
                        "kept": {"const": True},
                        "mode": {"enum": ["auto", "none"]},
                    },
                },
                "Error": {"type": "object", "properties": {"error": {"type": "object"}}},
                "WidgetList": {
                    "allOf": [
                        {"required": ["data"], "properties": {"data": {"type": "array"}}},
                        {"properties": {"data": {"type": "array", "items": {"type": "string"}}}},
                    ]
                },
            },
            "responses": {
                "NotFound": {
                    "description": "No such widget",
                    "content": {"application/json": {"examples": {"not_found": {}}}},
                }
            },
        }
    }
    return {
        "crew-public.yaml": public,
        "crew-owner.yaml": {"tags": [], "paths": {}},
        "components.yaml": components,
    }


def test_the_committed_pages_are_what_the_spec_renders() -> None:
    stale = generator.stale()
    assert stale == [], (
        "docs/reference/crew-api/ differs from the Crew API spec in these files; "
        f"run `python scripts/generate_crew_api_reference.py`: {stale}"
    )


def test_an_operation_is_marked_available_only_with_the_spec_mark() -> None:
    pages = generator.render(_docs())

    index = pages["README.md"]
    assert (
        "| [`GET /v1/widgets/{widget_id}`](public-widgets.md#get-v1widgetswidget_id) "
        "| Read a widget | Available now: owner only | preview |" in index
    )
    assert (
        "| [`DELETE /v1/widgets/{widget_id}`](public-widgets.md#delete-v1widgetswidget_id) "
        "| Delete a widget | Planned | stable |" in index
    )


def test_an_available_operation_says_which_callers_it_serves() -> None:
    docs = _docs()
    read = docs["crew-public.yaml"]["paths"]["/v1/widgets/{widget_id}"]["get"]

    read["x-crew-available-to"] = ["owner", "app"]
    assert (
        "| Status | Available now: owner and apps |" in generator.render(docs)["public-widgets.md"]
    )

    read["x-crew-available-to"] = "anyone"
    with pytest.raises(ValueError, match="x-crew-available-to"):
        generator.render(docs)


def test_what_is_not_served_yet_is_named_at_the_top_and_on_its_row() -> None:
    docs = _docs()
    read = docs["crew-public.yaml"]["paths"]["/v1/widgets/{widget_id}"]["get"]
    read["requestBody"] = {
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "fast": {"type": "boolean", "description": "Skips the queue."},
                        "mode": {"enum": ["auto", "none"]},
                        "name": {"type": "string"},
                    },
                }
            }
        }
    }
    read["responses"]["200"]["headers"] = {"X-Widget-Quota": {"schema": {"type": "integer"}}}
    read["x-crew-not-served-yet"] = {
        "request-body": ["fast", "mode=none"],
        "response-headers": ["X-Widget-Quota"],
    }

    page = generator.render(docs)["public-widgets.md"].split("## DELETE ")[0]

    assert (
        "Not served yet:\n\n- Request body fields: `fast`, `mode=none`.\n"
        "- Response headers: `X-Widget-Quota`.\n" in page
    )
    assert "| `fast` | boolean | no | **Not served yet.** Skips the queue. |" in page
    assert "| `mode` | `auto`, `none` | no | **Not served yet:** `mode=none`.  |" in page
    assert "| `name` | string | no |  |" in page
    assert "| `X-Widget-Quota` | integer | **Not served yet.**  |" in page

    read["x-crew-not-served-yet"] = {"request-cookies": ["x"]}
    with pytest.raises(ValueError, match="x-crew-not-served-yet"):
        generator.render(docs)


def test_a_page_says_where_each_field_lives_and_who_may_call() -> None:
    page = generator.render(_docs())["public-widgets.md"]
    read, delete = page.split("## DELETE ")

    assert "holds the scope `widgets:read`" in read and "The owner" in read
    assert "An app" not in delete
    path_at, query_at = read.index("Path parameters:"), read.index("Query parameters:")
    assert path_at < read.index("| `widget_id` | string | yes |") < query_at
    assert query_at < read.index("| `limit` | integer | no |")
    assert "Response body fields (`application/json`), [Widget](schemas.md#widget):" in read
    assert "| `id` | string | yes | The widget's id \\| \\<opaque> |" in read
    assert "| `size` | string: `small`, `large` | no |  |" in read
    assert "| `kept` | `true` | no |  |" in read
    assert "| `mode` | `auto`, `none` | no |  |" in read
    assert "| 404 | `not_found` | No such widget |" in read


def test_every_named_schema_has_a_section_a_type_links_to() -> None:
    pages = generator.render(_docs())

    assert "## Widget\n" in pages["schemas.md"] and "## Error\n" in pages["schemas.md"]


def test_errors_are_listed_by_status_and_a_type_names_each_shape_once() -> None:
    docs = _docs()
    read = docs["crew-public.yaml"]["paths"]["/v1/widgets/{widget_id}"]["get"]
    read["responses"]["400"] = {"description": "Bad request"}
    schemas = docs["components.yaml"]["components"]["schemas"]
    schemas["Widget"]["properties"]["picture"] = {
        "oneOf": [
            {"type": "object", "properties": {"url": {"type": "string"}}},
            {"type": "object", "properties": {"file_id": {"type": "string"}}},
        ]
    }
    schemas["Widget"]["properties"]["owner"] = {
        "description": "Who made it.",
        "allOf": [{"$ref": "#/components/schemas/Error"}, {"type": "object"}],
    }
    schemas["Error"]["description"] = "About the error object."
    schemas["Widget"]["properties"]["last_error"] = {"$ref": "#/components/schemas/Error"}

    page = generator.render(docs)["public-widgets.md"].split("## DELETE ")[0]

    assert page.index("| 400 |") < page.index("| 404 |")
    assert "| `picture` | object | no |  |" in page
    assert "| `owner` | [Error](schemas.md#error) | no | Who made it. |" in page
    assert "| `last_error` | [Error](schemas.md#error) | no |  |" in page


def test_a_field_two_parts_of_an_allof_name_is_listed_once() -> None:
    section = generator.render(_docs())["schemas.md"].split("## WidgetList\n")[1]

    assert section.count("| `data` |") == 1
    assert "| `data` | array of string | yes |  |" in section


def test_a_bar_in_a_value_does_not_end_its_table_cell() -> None:
    docs = _docs()
    widget = docs["components.yaml"]["components"]["schemas"]["Widget"]
    widget["properties"]["mode"] = {"enum": ["a|b", "none"]}

    section = generator.render(docs)["schemas.md"].split("## Widget\n")[1]

    # Four cells, as the heading has: the bar is escaped, not read as a cell's end.
    assert "| `mode` | `a\\|b`, `none` | no |  |" in section


def test_a_directory_among_the_pages_is_named_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "drafts").mkdir()

    assert "drafts" in generator.stale(out_dir=tmp_path)

    monkeypatch.setattr(generator, "OUT_DIR", tmp_path)
    assert generator.main([]) == 1
    assert "drafts" in capsys.readouterr().out
    assert [path.name for path in tmp_path.iterdir()] == ["drafts"]


def test_a_type_says_its_range_and_its_default() -> None:
    docs = _docs()
    widget = docs["components.yaml"]["components"]["schemas"]["Widget"]
    widget["properties"]["count"] = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
    widget["properties"]["depth"] = {"type": "integer", "minimum": 0}

    section = generator.render(docs)["schemas.md"].split("## Widget\n")[1]

    assert "| `count` | integer (1 to 100, default `20`) | no |  |" in section
    assert "| `depth` | integer (at least 0) | no |  |" in section


def test_an_error_code_is_the_one_its_example_carries() -> None:
    docs = _docs()
    not_found = docs["components.yaml"]["components"]["responses"]["NotFound"]
    not_found["content"]["application/json"]["examples"] = {
        "not_found_widget": {"value": {"error": {"code": "not_found"}}},
        "not_found_shelf": {"value": {"error": {"code": "not_found"}}},
        "gone": {},
    }

    page = generator.render(docs)["public-widgets.md"]

    # The body's code once, not the examples' names; a bare example by its name.
    assert "| 404 | `not_found`, `gone` | No such widget |" in page


def test_an_error_row_names_the_headers_it_declares_that_are_not_served() -> None:
    docs = _docs()
    read = docs["crew-public.yaml"]["paths"]["/v1/widgets/{widget_id}"]["get"]
    not_found = docs["components.yaml"]["components"]["responses"]["NotFound"]
    not_found["headers"] = {
        "X-Widget-Quota": {"schema": {"type": "integer"}},
        "X-Request-Id": {"schema": {"type": "string"}},
    }
    row = "| 404 | `not_found` | No such widget |"
    marked = (
        "| 404 | `not_found` | **Not served yet:** `X-Widget-Quota`. "
        "Where this note says the response carries them, that is the planned behaviour. "
        "No such widget |"
    )

    # Without the mark the shared response reads as the spec has it.
    assert row in generator.render(docs)["public-widgets.md"]

    read["x-crew-not-served-yet"] = {"response-headers": ["X-Widget-Quota"]}
    page = generator.render(docs)["public-widgets.md"]
    assert marked in page and row not in page

    # A mark that names a header this response does not declare leaves the row alone.
    read["x-crew-not-served-yet"] = {"response-headers": ["X-Other"]}
    assert row in generator.render(docs)["public-widgets.md"]
