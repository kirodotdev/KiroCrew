"""The ``dashboard_save`` reply as the agent reads it: the page check's verdict.

Split from ``test_dashboard_save_tool.py`` (file line cap). These are pure renders of
``mcp_panel._render_saved``; the route's half stays in that module.
"""

from __future__ import annotations

from typing import Any


def _verdict(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "data": "synthetic",
        "errors": [],
        "reason": "",
        "notes": [],
        **extra,
    }


def test_the_reply_lists_each_error_with_its_place_in_the_page():
    from kiro_crew import mcp_panel

    verification = _verdict(
        "failed",
        errors=[
            {
                "kind": "exception",
                "message": "TypeError: rows.map is not a function",
                "line": 4,
                "column": 8,
            },
            {"kind": "rejection", "message": "Error: later", "line": 9, "column": None},
            {
                "kind": "csp",
                "message": "Connecting to 'https://x.test/' violates ...",
                "line": None,
                "column": None,
            },
        ],
        omitted=2,
        notes=["This check runs offline, so these requests were refused: https://cdn.test/x.js."],
    )

    out = mcp_panel._render_saved({"fields": 2, "blocks": 0}, verification)

    assert "verification: failed" in out
    assert "raised 5 error(s)" in out
    assert "1. TypeError: rows.map is not a function (view.html line 4, column 8)" in out
    assert "2. unhandled promise rejection: Error: later (view.html line 9)" in out
    assert "3. refused by the frame's policy: Connecting to" in out
    assert "... and 2 more." in out
    assert "note: This check runs offline" in out
    assert "call dashboard_save again before you apply it" in out
    # The verdict leads: it is what the agent has to act on before anything else.
    assert out.index("verification: failed") < out.index("Next: call dashboard_apply")


def test_the_reply_says_what_a_pass_and_a_skip_mean():
    from kiro_crew import mcp_panel

    passed = mcp_panel._render_saved({}, _verdict("passed"))
    skipped = mcp_panel._render_saved({}, _verdict("skipped", reason="no browser here"))

    assert "verification: passed" in passed and "no script errors" in passed
    assert "verification: skipped -- no browser here." in skipped
    assert "The page was not verified" in skipped


def test_a_reply_without_a_verification_is_the_reply_it_always_was():
    from kiro_crew import mcp_panel

    base = mcp_panel._render_saved({"fields": 1, "blocks": 1})

    assert "verification" not in base
    assert mcp_panel._render_saved({"fields": 1, "blocks": 1}, None) == base
    assert mcp_panel._render_saved({"fields": 1, "blocks": 1}, "garbage") == base


def test_the_reply_says_what_a_timed_out_check_means():
    from kiro_crew import mcp_panel

    out = mcp_panel._render_saved(
        {"fields": 2, "blocks": 0},
        _verdict(
            "timed_out",
            reason="the check reached its 8 second limit and the browser was stopped",
            errors=[
                {
                    "kind": "timeout",
                    "message": "the page did not finish loading",
                    "line": None,
                    "column": None,
                }
            ],
        ),
    )

    assert "verification: timed_out -- the check reached its 8 second limit" in out
    assert "The page was not verified" in out
    assert "a loop or a wait that may never end" in out
    assert "1. the page did not finish loading" in out
    assert out.index("verification: timed_out") < out.index("Next: call dashboard_apply")


def test_the_reply_says_the_page_ran_over_a_synthetic_copy():
    from kiro_crew import mcp_panel

    passed = mcp_panel._render_saved({}, _verdict("passed"))
    failed = mcp_panel._render_saved(
        {},
        _verdict(
            "failed",
            errors=[{"kind": "exception", "message": "TypeError: x", "line": 2, "column": 1}],
        ),
    )
    plain = mcp_panel._render_saved({}, _verdict("passed", data=""))

    assert "ran once over a synthetic copy of your crewmate's current data" in passed
    assert "the same shape with invented values" in passed
    assert "ran once over a synthetic copy" in failed
    assert "your crewmate's data and raised" not in failed
    # A verdict that does not say what it ran over does not claim a copy.
    assert "synthetic" not in plain


def test_dashboard_save_describes_a_page_written_as_view_html() -> None:
    """A crewmate's page is view.html only: the tool must not teach a block layout."""
    from kiro_crew import mcp_panel

    (tool,) = [t for t in mcp_panel._list_tools() if t["name"] == "dashboard_save"]
    view = tool["inputSchema"]["properties"]["view"]["description"]
    assert '{"html": "..."}' in view and "data-dashboard-field" in view
    assert "using host or custom variables" in view
    assert "window.kirocrew.subscribe" in view and "needs no text cell" in view
    model = tool["inputSchema"]["properties"]["model"]["description"]
    assert "`array`" in model and "`object`" in model
    text = (tool["description"] + view).lower()
    assert "blocks" not in text and "block type" not in text and "block catalog" not in text


def test_the_crewmate_is_told_its_page_is_view_html() -> None:
    """The member protocol's page item names the same three parts the save tool takes."""
    from kiro_crew.context_assembly import member

    item = member._MEMBER_DASHBOARD_ITEM
    assert "fields, view.html and theme" in item
    assert "blocks" not in item


def test_the_tool_reply_carries_field_types_and_both_warnings() -> None:
    """The crewmate reads the MCP text, so the gateway's checks must reach it."""
    from kiro_crew import mcp_panel

    reply = {
        "saved": {"fields": 2},
        "verification": {
            "status": "skipped",
            "reason": "no browser on this host",
            "note": "The page was NOT run: no browser on this host.",
        },
        "fields": [
            {"field": "open_prs", "source": "manual", "type": "array"},
            {"field": "spend", "source": "fold:usage.total", "type": "number"},
        ],
        "manual_fields": ["open_prs"],
        "manual_note": "These fields change only when you write them.",
        "shape_warnings": ["open_prs is array but a data-dashboard-field cell shows it"],
        "theme_warnings": ["Your theme sets fixed colours for one scheme only"],
    }
    text = mcp_panel._render_saved(reply["saved"], reply["verification"], reply=reply)
    assert "The page was NOT run" in text
    assert "open_prs (array) -- manual" in text
    assert "spend (number) -- fold:usage.total" in text
    assert "These fields change only when you write them." in text
    assert "Shape warnings:\n  - open_prs is array" in text
    assert "Theme warnings:\n  - Your theme sets fixed colours" in text
    # The warnings come before the next step, which is still last.
    assert text.index("Theme warnings") < text.index("Next: call dashboard_apply")


def test_the_types_and_fields_replies_carry_the_rendering_advice() -> None:
    from kiro_crew import mcp_panel
    from kiro_crew.dashboard.handlers.dashboard_page_checks import page_rendering

    advice = page_rendering()
    types_text = mcp_panel._render_types(
        {"types": [{"name": "work", "keyed_by": "slot", "folded_through": 3}], "rendering": advice}
    )
    fields_text = mcp_panel._render_fields({"template": None, "rendering": advice})
    for text in (types_text, fields_text):
        assert "Drawing a type a data-dashboard-field cell shows raw:" in text
        assert f"  array: {advice['array']}" in text
    # With no page yet the crewmate is pointed at writing its own, not at a template.
    assert "dashboard_save it, then dashboard_apply it" in fields_text
    assert "adopt a template" not in fields_text
