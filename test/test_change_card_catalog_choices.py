"""Every arm of four three-way choices in ``change_card_catalog``.

Each picks one of three values: the risk label a ``template.update`` card
shows, the bucket a capability impact item lands in, the empty value a
``template.update`` Undo writes back for a field the template did not have,
and how one key reads in a dashboard-config memory line. Pure functions, no
I/O.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import change_card_catalog as catalog


@pytest.mark.parametrize(
    ("before", "after", "risk"),
    [
        # A grant only added widens, a grant only removed tightens, and an
        # edit that does both still widens: widen outranks tighten.
        ({"allowedTools": ["a"]}, {"allowedTools": ["a", "b"]}, catalog.RISK_WIDEN),
        ({"allowedTools": ["a", "b"]}, {"allowedTools": ["a"]}, catalog.RISK_TIGHTEN),
        ({"allowedTools": ["a"]}, {"allowedTools": ["b"]}, catalog.RISK_WIDEN),
        # Only ``allowedTools`` moves the label; any other field is normal.
        ({"tools": ["a"]}, {"tools": ["b"]}, catalog.RISK_NORMAL),
        ({"model": "m1"}, {"model": "m2"}, catalog.RISK_NORMAL),
    ],
)
def test_a_template_card_risk_follows_its_allowed_tools_diff(
    before: dict[str, Any], after: dict[str, Any], risk: str
) -> None:
    p = catalog.validate_params("template.update", {"template": "builder", "fields": after})
    preview = catalog.build_preview(
        "template.update", p, {"exists": True, "fields": before}, {"members": []}
    )
    assert preview["risk"] == risk


def test_capability_impact_rows_bucket_added_removed_and_changed_items() -> None:
    rows = catalog._impact_rows(
        [
            {"section": "tools", "id": "a", "change": "added"},
            {"section": "tools", "id": "b", "change": "removed"},
            {"section": "tools", "id": "c", "change": "changed"},
            {"section": "mcpServers", "id": "d", "change": "removed"},
        ]
    )
    assert rows == [
        {"label": "tools", "add": ["a"], "remove": ["b"], "after": "changed: c"},
        {"label": "mcpServers", "add": [], "remove": ["d"]},
    ]


def test_a_template_undo_writes_an_empty_value_for_a_field_the_template_lacked() -> None:
    params = {
        "template": "builder",
        "fields": {"model": "m", "tools": ["t"], "allowedTools": ["x"], "skills": ["s"]},
    }
    # Only a missing or None value is replaced. A stored falsy value (a
    # hand-edited spec's empty ``tools`` string) is written back as it was.
    before = {"fields": {"tools": "", "allowedTools": ["kept"], "model": None}}
    undo, reason = catalog.build_undo("template.update", params, before, [{}], 1)
    assert reason is None
    assert undo == [
        {
            "method": "PATCH",
            "path": "/api/agents/detail/builder",
            "body": {"model": "", "tools": "", "allowedTools": ["kept"], "skills": []},
        }
    ]


def test_a_dashboard_config_memory_line_reads_each_kind_of_key() -> None:
    setting = catalog.DASHBOARD_SETTINGS["chat.quick-send"][0]
    list_add = catalog.DASHBOARD_LIST_SETTINGS["chat.selectable-models"][1]
    line = catalog.describe_manual_change(
        ("PUT", "/api/dashboard/config"),
        {},
        {setting: True, list_add: "model-x", "zz_unlisted": "anything"},
        {setting: False},
    )
    # A registered setting shows before and after, a list delta shows the item
    # it carries, and any other key is named without its value.
    assert line == (
        "changed dashboard setting "
        "model_picker_hidden_models_add model-x, quick_send false→true, zz_unlisted"
    )
