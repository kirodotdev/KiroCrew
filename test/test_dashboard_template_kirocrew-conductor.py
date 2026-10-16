"""Static checks for the built-in Mate task board."""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew.dashboard_templates import catalog
from kiro_crew.dashboard_templates.manifest import load_template

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin/kirocrew-conductor"
GOAL_BOARD = ROOT / "src/kiro_crew/dashboard_templates/builtin/goal-board/manifest.json"

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
    goal_board = json.loads(GOAL_BOARD.read_text(encoding="utf-8"))["fields"]
    fields = _fields()
    for name in ("goal", "round", "last_entry_at", "items", "omitted", "forgotten"):
        assert fields[name]["source"] == goal_board[name]["source"], name


def test_needs_you_is_the_only_field_the_conductor_writes() -> None:
    fields = _fields()
    agentic = sorted(name for name, field in fields.items() if field["source"] == {"agentic": True})
    assert agentic == ["needs_you"]
    assert fields["needs_you"]["type"] == "array"


def test_chat_notes_keep_their_old_bindings_but_are_never_rendered() -> None:
    fields = _fields()
    source = _source()
    assert fields["note"]["source"] == {"fold": "panel", "path": "data"}
    assert fields["note_at"]["source"] == {"fold": "panel", "path": "published_at"}
    assert '<span data-dashboard-field="note" hidden>' in source
    assert '<span data-dashboard-field="note_at" hidden>' in source
    assert "bag().note" not in source


def test_needs_you_precedes_goal_and_closed_task_details() -> None:
    source = _source()
    action = source.index('id="cb-you"')
    goal = source.index('class="overview"')
    details = source.index('id="cb-details"')
    assert action < goal < details
    assert re.search(r'<details id="cb-details">\s*<summary>', source)
    assert "summary::after" in source
    assert "show_tasks: 'Show tasks'" in source
    start = source.index('<details id="cb-details"')
    assert " open" not in source[start : start + 80]


def test_only_action_and_rejected_rows_render_a_worker_summary() -> None:
    script = _script()
    action = script.split("function drawYou(")[1].split("function forgotten(")[0]
    tasks = script.split("function drawTasks(")[1].split("function drawDetailsLabel(")[0]
    assert "it.summary" in action
    assert "reading[0] === 'bad' && summary" in tasks


def test_no_value_reaches_the_dom_as_markup() -> None:
    source = _source()
    for sink in HTML_SINKS:
        assert sink not in source, sink


def test_the_one_host_message_only_fills_the_chat_box() -> None:
    script = _script()
    assert re.findall(r"type:\s*'([\w:-]+)'", script) == ["kirocrew-dashboard:act"]
    assert script.count("postMessage(") == 1
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "window.open", "href"):
        assert banned not in script, banned


def test_action_subtitles_preserve_provenance_and_intent() -> None:
    script = _script()
    assert "a.indexOf(name) !== -1" in script
    assert "written('needs_you')" in script
    assert "From · Your crewmate" in script
    assert "From · Work record" in script
    assert "Approval needed" in script
    assert "Adds a reply draft to chat. Nothing is sent until you send it." in script
    assert "Nothing sends until" not in script


def test_hidden_omissions_and_pr_numbers_keep_their_meaning() -> None:
    source = _source()
    script = _script()
    assert ".cb #cb-more[hidden] { display: none; }" in source
    assert "older updates are in chat" in script
    assert "i18n('ask_older')" in script
    assert "querySelectorAll('button.reply, .hint')" in script
    assert "sub hint" in script
    assert "A worker's done report is still only a claim" in source
    assert "Omitted counts dropped work-log updates" in source
    assert "i18n('pr', { n: String(pr) })" in script


def test_progress_has_an_accessible_native_meter() -> None:
    script = _script()
    for name in ("role", "aria-label", "aria-valuemin", "aria-valuemax", "aria-valuenow"):
        assert f"track.setAttribute('{name}'" in script


def test_reduced_motion_stops_every_animation_and_transition() -> None:
    match = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}",
        _style(),
        re.DOTALL,
    )
    assert match, "no prefers-reduced-motion block"
    rule = match.group(1)
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
    assert "minmax(min(100%, 240px), 1fr)" in style
    assert not re.search(r"(?<![-\w])width:\s*\d{3,}px", style)


def test_conductor_guidance_uses_each_workers_own_action_detail() -> None:
    skill = (ROOT / "src/kiro_crew/builtin_skills/goal-conductor/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "each worker `question` or `blocked` report supplies its own" in skill
    assert "Mirror each worker" not in skill
    assert "kiro_crew.conductor_board_contract" in skill
    assert "{needs_you, done, updated, next, repo?, tasks:" in skill
    assert "`Code` / `Test` / `PR` / `CI` / `Done`" in skill


def test_member_prompt_does_not_send_panel_notes_to_this_page() -> None:
    source = (ROOT / "src/kiro_crew/context_assembly/member.py").read_text(encoding="utf-8")
    assert "On `kirocrew-conductor`, put" in source
    assert "does not draw the panel note" in source
