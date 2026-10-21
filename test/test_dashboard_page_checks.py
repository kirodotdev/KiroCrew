"""The gateway's own checks of a saved crewmate page (dashboard_page_checks).

Both come from round-5 pages: Cove bound its worker's item list and a time to text
cells (raw ``[]`` and ISO), and set light-only text colours, so its page was pale on
white in light mode -- with page verification skipped because the host had no browser.
"""

from __future__ import annotations

from typing import Any

from kiro_crew import crewmate_prompts
from kiro_crew.dashboard.handlers import dashboard_page_checks as checks


def _package(html: str, types: dict[str, Any], theme: dict[str, Any] | None = None) -> dict:
    return {
        "model": {"types": types},
        "view": {"html": html},
        "theme": theme if theme is not None else {"tokens": {}},
    }


_TYPES = {
    "work_items": {"type": "array", "source": {"fold": "work", "path": "items"}},
    "last_active": {"type": "timestamp", "source": {"fold": "work", "path": "last_entry_at"}},
    "goal_text": {"type": "text", "source": {"fold": "goal", "path": "goal"}},
}


def test_a_list_and_a_time_bound_to_text_cells_are_named():
    html = (
        '<p data-dashboard-field="work_items"></p>'
        '<p data-dashboard-field="last_active"></p>'
        '<p data-dashboard-field="goal_text"></p>'
    )
    warnings = checks.shape_warnings(_package(html, _TYPES))
    assert len(warnings) == 2
    assert warnings[0].startswith("last_active is timestamp")
    assert warnings[1].startswith("work_items is array")
    assert crewmate_prompts.text("dashboard.render_array") in warnings[1]


def test_a_list_read_by_the_page_script_is_not_named():
    html = "<ul id=items></ul><script>window.kirocrew.subscribe('work_items', draw)</script>"
    assert checks.shape_warnings(_package(html, _TYPES)) == []


def test_the_rendering_note_covers_each_type_a_text_cell_cannot_draw():
    assert set(checks.page_rendering()) == {"array", "object", "timestamp"}


def test_light_only_text_colours_are_flagged():
    """Cove: fixed light text and a translucent surface, the background left to the host."""
    cove = {
        "tokens": {
            "--kc-fg": "#e8ece7",
            "--kc-fg-muted": "#95a08f",
            "--kc-surface": "rgba(255,255,255,0.04)",
        }
    }
    assert checks.theme_warnings(_package("<p></p>", {}, cove)) == [
        crewmate_prompts.text("dashboard.theme_one_scheme")
    ]


def test_a_fixed_board_with_fixed_text_reads_in_both_schemes():
    """Briar's chalkboard: dark board and chalk text, both fixed, so no warning."""
    css = ".board{background:#20262a} .board h1{color:#f4f1e8}"
    assert checks.theme_warnings(_package("<p></p>", {}, {"tokens": {}, "css": css})) == []


def test_theme_variables_or_both_schemes_are_left_alone():
    plain = {"tokens": {}, "css": ".v{color:var(--text)} .b{background:var(--bg)}"}
    assert checks.theme_warnings(_package("<p></p>", {}, plain)) == []
    both = {
        "tokens": {},
        "css": ".v{color:#222} @media (prefers-color-scheme: dark){.v{color:#eee}}",
    }
    assert checks.theme_warnings(_package("<p></p>", {}, both)) == []


def test_a_one_scheme_colour_in_the_page_style_counts_too():
    html = "<style>.v{color:#f5f5f5}</style><p class=v></p>"
    assert checks.theme_warnings(_package(html, {})) != []
