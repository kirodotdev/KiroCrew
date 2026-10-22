"""The texts a crewmate reads before it writes its Dashboard page, and the claims they make.

Pinned as claims, not as prose: each assertion names one rule a pod round showed a
page break when the text dropped it -- a page that copied a metaphor-free list after
"no borrowed look", a bar part-way full for a running worker, a raw ``session/opened``,
a home-directory path in the title, a row clipped at 375px, ``0.0`` credits read from
another session -- or the motion rule: subtle motion by default, held still only for a reader who set
reduced motion.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew import mcp_panel
from kiro_crew.dashboard.goal_reminder import DASHBOARD_BUILD_LINE
from kiro_crew.dashboard.handlers import dashboard_page_checks as checks
from kiro_crew.dashboard_templates.page_look import PAGE_LOOK

SKILL = Path(__file__).resolve().parents[1] / "src/kiro_crew/builtin_skills/dashboard/SKILL.md"


def _skill() -> str:
    return " ".join(SKILL.read_text("utf-8").split())


def _save_tool() -> dict:
    (tool,) = [t for t in mcp_panel._list_tools() if t["name"] == "dashboard_save"]
    return tool


def _view() -> str:
    return _save_tool()["inputSchema"]["properties"]["view"]["description"]


def test_the_build_line_and_the_save_tool_carry_one_look_text():
    """One shared text, so the two places read before a first page cannot drift."""
    assert PAGE_LOOK in DASHBOARD_BUILD_LINE
    assert PAGE_LOOK in _view()


def test_the_look_invites_a_character_of_its_own():
    flat = " ".join(PAGE_LOOK.split())
    assert "choose your own theme from the project and its context" in flat
    assert "Backgrounds, wallpaper, texture and expressive frames" in flat
    assert "Subtle ambient motion and animation run constantly while the page is in view" in flat
    assert "not only when a value changes" in flat
    assert "pause them while hidden" in flat
    skill = _skill()
    assert "That freedom includes a theme, atmosphere and visual metaphor" in skill
    assert "rather than a reason to remove the page's character" in skill


def test_the_honesty_rules_hold_in_every_text():
    look = " ".join(PAGE_LOOK.split())
    assert (
        "progress needs a real measure, not an arbitrary fraction assigned to running or idle"
        in look
    )
    assert "agree with the folded numbers" in look
    skill = _skill()
    assert "a real denominator" in skill and "stay distinct from zero" in skill
    assert "Running or idle alone says nothing about how much of a task is complete" in skill
    assert "an unknown type gets a truthful general phrase" in skill


def test_ids_paths_and_agent_free_text_stay_off_the_visible_page():
    look = " ".join(PAGE_LOOK.split())
    assert "internal ids, session keys and file-system paths out of view" in look
    assert "free text an agent wrote into a fold is shown as a short plain sentence" in look
    skill = _skill()
    assert (
        "keeping internal ids, session keys and file-system paths out of the visible page" in skill
    )
    assert "never an absolute or home-directory path" in skill
    assert "show it as a short plain sentence" in skill


def test_rows_fit_a_narrow_screen():
    look = " ".join(PAGE_LOOK.split())
    assert "a label and its value wrap or stack" in look
    assert "nothing is clipped or scrolls sideways" in look
    assert "at 320px a label and its value wrap or stack" in _skill()


def test_reduced_motion_holds_still_only_for_the_reader_who_set_it():
    """Motion is the default for every reader; ``prefers-reduced-motion: reduce`` is the
    one case that stills it, and no text tells a crewmate to hold motion back for
    everyone."""
    for text in (_skill(), DASHBOARD_BUILD_LINE, _view(), PAGE_LOOK):
        low = " ".join(text.split()).lower()
        assert "prefers-reduced-motion: reduce" in low
        assert "every value jumps" in low and "ambient layers hold still" in low
        assert "motion off under" not in low
    assert "every other reader gets the motion" in " ".join(PAGE_LOOK.split()).lower()
    assert "do not hold it back by default" in " ".join(_skill().split())
    assert "run constantly while the page is in view, not only when a value changes" in " ".join(
        _skill().split()
    )


def test_workstreams_and_the_session_scope_of_usage_are_described():
    skill = _skill()
    assert "`workstreams` is the measured project history a page most often misses" in skill
    assert "describe the live session only" in skill
    assert "spend across the project is each workstream's `credits`" in skill
    assert "workstreams for progress, hourly history and project spend" in DASHBOARD_BUILD_LINE
    assert "tools and usage for the live session's activity" in DASHBOARD_BUILD_LINE


def test_milestones_and_sampled_snapshots_may_reach_the_page():
    skill = _skill()
    assert "including relevant goals and milestones when they reveal progress" in skill
    assert "Show each milestone's reading beside its target, as text" in skill
    assert "never something the page" in skill
    assert "an arbitrary prose target is not a denominator" in skill
    assert "Label its source and observation time visibly" in skill
    assert "goal for relevant milestones" in DASHBOARD_BUILD_LINE
    assert "with its source and observation time visible" in DASHBOARD_BUILD_LINE


def test_redesign_once_when_work_grew_never_every_turn():
    assert "it is never a reason to redraw it every turn" in _skill()


def test_saving_stores_and_applying_publishes():
    description = _save_tool()["description"]
    assert (
        "SAVE a draft" in description and "dashboard_apply puts it live separately" in description
    )
    assert "PUT IT LIVE" not in description


def test_the_view_text_tells_text_cells_from_drawing():
    view = _view()
    assert "Text cells can use" in view and "drawings subscribe through" in view
    assert "a script-only field needs no text cell" in view
    model = _save_tool()["inputSchema"]["properties"]["model"]["description"]
    assert "`array`" in model and "`object`" in model


def test_colour_is_paired_and_custom_variables_are_real():
    skill = _skill()
    assert "a defined custom variable is a normal design tool" in skill
    assert "An undefined variable uses its fallback" in skill
    assert "Pair surface and text colours" in skill
    theme = _save_tool()["inputSchema"]["properties"]["theme"]["description"]
    assert "inline data: URL" in theme


def test_the_save_warnings_name_a_risk_and_a_fix_not_a_verdict():
    for line in (checks.THEME_ONE_SCHEME, checks.PAINTED_SURFACE):
        assert "light and dark" in line
    assert "removing colour is not the goal" in checks.THEME_ONE_SCHEME
    assert "not a measured contrast failure" in checks.PAINTED_SURFACE
    assert "A fixed colour alone is not a contrast failure" in checks.UNDEFINED_VAR
    every = " ".join(
        (checks.THEME_ONE_SCHEME, checks.PAINTED_SURFACE, checks.UNDEFINED_VAR,
         checks.MANUAL_FIELDS_NOTE, checks.SHAPE_WARNING, *checks.RENDERING.values())
    )  # fmt: skip
    for verdict in ("unreadable", "disappears", "stale the moment", "reads in one scheme only"):
        assert verdict not in every, verdict
    assert "can drive charts" in checks.RENDERING["array"]
    note = checks.MANUAL_FIELDS_NOTE
    assert "show their source and observation time" in note
    assert "Arrays and objects can carry measured samples" in note


#: Common typeface names no page text may recommend: type is paired, never named.
TYPEFACES = (
    "inter", "roboto", "helvetica", "arial", "georgia", "times new roman", "segoe",
    "sf pro", "menlo", "consolas", "courier", "fira", "jetbrains", "source sans",
    "source code", "ibm plex", "noto", "open sans", "lato", "montserrat", "playfair",
    "merriweather", "garamond", "futura", "avenir", "verdana", "tahoma", "pingfang",
    "microsoft yahei", "-apple-system", "blinkmacsystemfont",
)  # fmt: skip


#: Language and script names the type rule may not single out: Crew serves many locales.
LANGUAGES = (
    "chinese", "japanese", "korean", "cjk", "han", "hanzi", "kanji", "hangul", "kana",
    "latin", "cyrillic", "arabic", "hebrew", "devanagari", "bengali", "thai", "greek",
    "english", "german", "french", "spanish", "russian", "hindi", "portuguese", "italian",
)  # fmt: skip


def test_type_is_paired_by_role_and_no_text_names_a_typeface():
    look = " ".join(PAGE_LOOK.split())
    skill = _skill()
    for text in (look, skill):
        assert "var(--font-sans)" in text and "var(--font-mono)" in text
        assert "at most two" in text
        assert "tabular-nums" in text
        assert "any one script" in text and "each script's system face" in text
        assert "a style the user names wins" in text.lower()
    assert "never for body text or headings" in skill
    assert "Build hierarchy with size, weight and colour, not with another family" in skill
    for text in (skill, DASHBOARD_BUILD_LINE, _view(), PAGE_LOOK):
        low = text.lower()
        for face in TYPEFACES + LANGUAGES:
            assert not re.search(rf"(?<![\w-]){re.escape(face)}(?![\w-])", low), face
