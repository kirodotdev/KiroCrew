"""The ``kirocrew-conductor`` built-in: the conductor's board as a dashboard page.

What a regex can pin is pinned here; the page's behaviour on a real field bag is
driven in ``website/src/test/conductorBoardPage.test.ts``. The data comes from three
places, and each is checked: the ``work`` fold for tasks, one agentic field for the
conductor's own needs-you lines, and the conductor's own ``panel`` record for its
last note.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew.dashboard_templates import catalog
from kiro_crew.dashboard_templates.manifest import load_template

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin/kirocrew-conductor"
GOAL_BOARD = ROOT / "src/kiro_crew/dashboard_templates/builtin/goal-board/manifest.json"

#: Every DOM API that parses a string as HTML.
HTML_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "createContextualFragment",
    "parseFromString",
    "setHTMLUnsafe",
    "parseHTMLUnsafe",
    "srcdoc",
)


def _source() -> str:
    return (DIRECTORY / "template.html").read_text(encoding="utf-8")


def _style() -> str:
    return _source().split("<style>")[1].split("</style>")[0]


def _script() -> str:
    return _source().split("<script>")[1].split("</script>")[0]


def _fields() -> dict[str, dict[str, object]]:
    raw = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    return dict(raw["fields"])


def test_the_registry_serves_it_under_the_conductors_slug() -> None:
    entry = catalog.load_one("kirocrew-conductor")
    manifest, _ = load_template(DIRECTORY)
    assert entry.id == manifest.id == "kirocrew-conductor"


def test_the_board_is_bound_to_the_work_fold_the_way_goal_board_binds_it() -> None:
    """The live half: a conductor that skips a publish still has a current board."""
    goal_board = json.loads(GOAL_BOARD.read_text(encoding="utf-8"))["fields"]
    fields = _fields()
    for name in ("goal", "round", "last_entry_at", "items", "omitted", "forgotten"):
        assert fields[name]["source"] == goal_board[name]["source"], name


def test_the_meter_counts_the_closed_tasks_the_fold_forgot() -> None:
    """The work fold drops closed items past its window and counts them in
    ``forgotten``; a meter reading ``items`` alone undercounts on a long goal."""
    assert _fields()["forgotten"]["source"] == {"fold": "work", "path": "forgotten"}
    meter = _script().split("function drawMeter(")[1].split("\n      function ")[0]
    assert "forgotten()" in meter
    for key in ("gone.items", "gone.accepted"):
        assert key in meter, key
    assert 'data-dashboard-field="forgotten"' in _source()


def test_needs_you_is_the_one_field_the_conductor_writes() -> None:
    fields = _fields()
    agentic = sorted(n for n, f in fields.items() if f["source"] == {"agentic": True})
    assert agentic == ["needs_you"]
    assert fields["needs_you"]["type"] == "array"


def test_the_note_is_the_published_panel_data() -> None:
    """The dashboard RFC's rule: a page reads the published panel as data."""
    assert _fields()["note"]["source"] == {"fold": "panel", "path": "data"}
    assert _fields()["note_at"]["source"] == {"fold": "panel", "path": "published_at"}


def test_no_value_reaches_the_dom_as_markup() -> None:
    source = _source()
    for sink in HTML_SINKS:
        assert sink not in source, sink


def test_its_one_host_message_only_fills_the_chat_box() -> None:
    """The Reply button posts ``act``, which the host turns into composer text and
    never sends; it is the bridge goal-board uses. No other channel exists."""
    script = _script()
    assert re.findall(r"type:\s*'([\w:-]+)'", script) == ["kirocrew-dashboard:act"]
    assert script.count("postMessage(") == 1
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "window.open", "href"):
        assert banned not in script, banned


def test_the_crewmates_words_are_labelled_as_written() -> None:
    """The needs-you tag comes from the host's agentic list, read as a list."""
    script = _script()
    assert "a.indexOf(name) !== -1" in script
    assert "written('needs_you')" in script
    # The note is always the crewmate's own publish, so its tag is unconditional.
    note = script.split("function drawNote(")[1].split("\n      function ")[0]
    assert "el('span', 'tag', i18n('wrote'))" in note


def test_reduced_motion_stops_every_animation_and_transition() -> None:
    m = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}", _style(), re.DOTALL
    )
    assert m, "no prefers-reduced-motion block"
    rule = m.group(1)
    for selector in (".cb *", ".cb *::before", ".cb *::after"):
        assert selector in rule, selector
    assert "animation: none !important" in rule
    assert "transition: none !important" in rule


def test_every_animation_names_a_keyframes_this_page_defines() -> None:
    style = _style()
    used = set(re.findall(r"animation:\s*(cb-[\w-]+)", style))
    defined = set(re.findall(r"@keyframes\s+(cb-[\w-]+)", style))
    assert used and used == defined


def test_it_has_a_light_theme_and_fits_a_narrow_pane() -> None:
    style = _style()
    assert "@media (prefers-color-scheme: light)" in style
    # One column at 320px: the card grid's minimum never exceeds the pane.
    assert "minmax(min(100%, 240px), 1fr)" in style
    assert not re.search(r"(?<![-\w])width:\s*\d{3,}px", style)


def test_refresh_motion_is_tied_to_new_state_not_each_redraw() -> None:
    """A refill keeps the page document; its own renderer must do the same."""
    style = _style()
    script = _script()
    assert ".cb .band {" in style and "animation: cb-glow" in style
    assert ".cb .dot {" in style and "animation: cb-blink" in style
    assert "var taskNodes = {};" in script
    assert "if (signature === youSignature) return;" in script
    assert "if (freshTrack)" in script
    assert "window.addEventListener('DOMContentLoaded', render, { once: true });" in script
    assert "window.addEventListener('kirocrew-dashboard', render);" in script
    assert "window.addEventListener('load', render);" not in script
    assert "window.addEventListener('message'" not in script
