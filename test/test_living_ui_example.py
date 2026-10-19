"""Pin the living-ui example widget to the rules it shows.

``examples/build-queue.html`` is the widget the skill's rule V names as one that
passes the visual self-check, so an author copies from it. Each check reads the
example and the file that owns the rule: the toolkit blocks it copies
(``toolkit/living-ui.js``, read through the block-copy script), the theme
variables it may name (the widget frame's source) and the still-first rule
(its markup alone shows the settled picture).

How it moves and how it looks are checked in a browser, through the harness.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "living-ui"
EXAMPLE = SKILL_DIR / "examples" / "build-queue.html"
COPIER = ROOT / "scripts" / "copy_living_ui_blocks.py"
WIDGET_SRCDOC = ROOT / "website" / "src" / "lib" / "widgetSrcdoc.ts"
# `#rgb` through `#rrggbbaa`, and the functional colour notations.
_COLOUR_LITERAL = re.compile(r"#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(", re.I)


@pytest.fixture(scope="module")
def blocks() -> dict[str, str]:
    copier = load_skill_script("copy_living_ui_blocks_for_the_example", COPIER)
    return dict(copier.read_blocks(copier.TOOLKIT.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def example() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


class _Elements(HTMLParser):
    """Each element's tag, attributes and own text, in document order."""

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, dict[str, Any], list[str]]] = []
        self._open: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        text: list[str] = []
        self.found.append((tag, dict(attrs), text))
        if tag not in self._VOID:
            self._open.append(text)

    def handle_endtag(self, tag: str) -> None:
        if self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        if self._open:
            self._open[-1].append(data)


def _markup(example: str) -> list[tuple[str, dict[str, Any], str]]:
    """The elements before the script, which is what a reader sees if it fails."""
    parser = _Elements()
    parser.feed(example.split("<script>")[0])
    return [(tag, attrs, "".join(text).strip()) for tag, attrs, text in parser.found]


def test_each_helper_it_uses_is_a_toolkit_block_copied_unchanged(
    blocks: dict[str, str], example: str
) -> None:
    copied = re.findall(r"^LivingUI\.([A-Za-z]+) = ", example, re.M)
    assert copied, "the example copies no toolkit helper; update this test"
    for name in copied:
        assert name in blocks, f"the example defines LivingUI.{name}, which the toolkit lacks"
        assert blocks[name] in example, f"the example's {name} block differs from the toolkit's"
    called = set(re.findall(r"LivingUI\.([A-Za-z]+)\(", example))
    assert called <= set(copied), f"calls helpers it does not carry: {called - set(copied)}"


def test_its_style_carries_the_toolkit_css_unchanged(blocks: dict[str, str], example: str) -> None:
    rules = re.search(r"LivingUI\.css = `\n(.*?)`;", blocks["css"], re.S)
    assert rules, "the css block no longer holds one template string; update this test"
    style = re.search(r"<style>(.*?)</style>", example, re.S)
    assert style, "the example has no <style>; update this test"
    for rule in filter(None, rules.group(1).splitlines()):
        assert rule in style.group(1), f"the example's style lacks: {rule}"


def test_names_no_colour_and_only_theme_variables(example: str) -> None:
    assert not _COLOUR_LITERAL.findall(example), "the example names a literal colour"
    source = WIDGET_SRCDOC.read_text(encoding="utf-8")
    block = re.search(r"export const THEME_VAR_NAMES = \[(.*?)\] as const", source, re.S)
    assert block, "THEME_VAR_NAMES not found in widgetSrcdoc.ts; update this test"
    allowed = set(re.findall(r"'(--[a-z-]+)'", block.group(1)))
    used = set(re.findall(r"var\((--[A-Za-z0-9-]+)", example))
    assert used, "the example names no theme variable; update this test"
    assert used <= allowed, f"not theme variables: {sorted(used - allowed)}"


def test_the_markup_alone_is_the_settled_picture(example: str) -> None:
    elements = _markup(example)
    lit = [attrs for _, attrs, _ in elements if "data-lui-on" in attrs]
    assert len(lit) == 1, f"{len(lit)} elements are lit in the markup, not one"
    assert "data-lui-dim" in lit[0], "the lit element is not one of the dimmed parts"
    stage = [attrs for _, attrs, _ in elements if attrs.get("role") == "slider"]
    assert len(stage) == 1, "the example has no single slider stage"
    assert stage[0].get("tabindex") == "0"
    assert {"aria-valuemin", "aria-valuemax", "aria-label"} <= set(stage[0])
    readout = [text for _, attrs, text in elements if attrs.get("class") == "lui-readout"]
    assert len(readout) == 1, "the example has no single readout"
    count, phase = readout[0].split(" jobs · ")
    assert count == stage[0]["aria-valuenow"]
    assert stage[0]["aria-valuetext"] == f"{count} jobs, {phase}"
    live = [text for _, attrs, text in elements if attrs.get("aria-live") == "polite"]
    assert live == [""], "the polite live region must be one, and empty at load"
