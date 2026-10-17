"""The conductor's task-board template reads exactly what its contract declares.

The same gate ``test_pipeline_board_contract_parity`` holds for the pipeline board,
sized to this template: strip comments, take the one inline script, collect every
``d.<field>`` read and every ``t.<field>`` read inside the ``tasks.forEach`` body, and
compare with the flattened :class:`ConductorBoardPanel` in both directions. Like that
gate, it proves a field exists on both sides, not that its value is right.

The extractor REFUSES rather than returning a partial set when the template's shape
moves (root renamed, loop restructured), because a partial set reads as "the contract
is over-specified" instead of "the gate is broken".
"""

from __future__ import annotations

import json
import re
import typing
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent_panel
from kiro_crew.conductor_board_contract import (
    BOARD_CREW_NAME,
    BOARD_TEMPLATE_ID,
    STEPS,
    TASK_STATES,
    ConductorBoardPanel,
)


def _template_path() -> Path:
    return agent_panel.shipped_templates_dir() / f"{BOARD_TEMPLATE_ID}.html"


def _html() -> str:
    return _template_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# flatten the contract
# ---------------------------------------------------------------------------


def _is_typed_dict(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, dict) and hasattr(tp, "__annotations__")


def contract_keys(td: Any, prefix: str = "") -> set[str]:
    """Every leaf of a TypedDict tree, dotted, ``[]`` marking a list element."""
    out: set[str] = set()
    for name, tp in typing.get_type_hints(td).items():
        path = f"{prefix}{name}"
        if _is_typed_dict(tp):
            out |= contract_keys(tp, f"{path}.")
            continue
        if typing.get_origin(tp) is list:
            (item,) = typing.get_args(tp)
            if _is_typed_dict(item):
                out |= contract_keys(item, f"{path}[].")
                continue
        out.add(path)
    return out


# ---------------------------------------------------------------------------
# extract what the template reads
# ---------------------------------------------------------------------------


class ExtractionRefused(Exception):
    """The template does not have the shape the extractor reads."""


_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?<![:\w])//[^\n]*")
_TASK_LOOP = re.compile(r"\btasks\s*\.\s*forEach\s*\(\s*function\s*\(\s*t\s*\)\s*\{")


def _script(html: str) -> str:
    bodies = [m.group(1) for m in _SCRIPT.finditer(html) if m.group(1).strip()]
    if len(bodies) != 1:
        raise ExtractionRefused(f"expected one non-empty inline script, found {len(bodies)}")
    src = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), bodies[0])
    return _LINE_COMMENT.sub("", src)


def _body_at(src: str, open_at: int) -> str:
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at : i + 1]
    raise ExtractionRefused("unbalanced braces in the template script")


def _reads(src: str, var: str) -> set[str]:
    return set(re.findall(rf"\b{re.escape(var)}\s*\.\s*([A-Za-z_$][\w$]*)", src))


def island_keys(html: str) -> set[str]:
    src = _script(html)
    root = _reads(src, "d")
    if not root:
        raise ExtractionRefused("no reads of the island root 'd.' -- renamed?")
    if not re.search(r"\bvar\s+tasks\s*=\s*Array\.isArray\(\s*d\.tasks\s*\)", src):
        raise ExtractionRefused("expected 'var tasks = Array.isArray(d.tasks) ...' -- renamed?")
    loops = list(_TASK_LOOP.finditer(src))
    if len(loops) != 1:
        raise ExtractionRefused(f"expected one 'tasks.forEach(function(t){{', found {len(loops)}")
    body = _body_at(src, loops[0].end() - 1)
    task_fields = _reads(body, "t")
    if not task_fields:
        raise ExtractionRefused("the task loop reads no 't.' fields -- renamed?")
    return (root - {"tasks"}) | {f"tasks[].{f}" for f in task_fields}


def _js_array(src: str, name: str) -> list[str]:
    m = re.search(rf"\bvar\s+{name}\s*=\s*\[(.*?)\]", src, re.DOTALL)
    if not m:
        raise ExtractionRefused(f"no 'var {name} = [...]' in the template")
    return json.loads(f"[{m.group(1)}]")


def _js_object_keys(src: str, name: str) -> list[str]:
    m = re.search(rf"\bvar\s+{name}\s*=\s*\{{(.*?)\}}", src, re.DOTALL)
    if not m:
        raise ExtractionRefused(f"no 'var {name} = {{...}}' in the template")
    return re.findall(r'"([^"]+)"\s*:', m.group(1))


# ---------------------------------------------------------------------------
# the extractor's own preconditions
# ---------------------------------------------------------------------------


def test_the_extractor_reads_the_shipped_template() -> None:
    assert island_keys(_html()), "extractor returned nothing -- the gate would be vacuous"


def test_a_field_named_only_in_a_comment_is_not_read() -> None:
    html = _html().replace("<script>", "<script>/* d.ghost_field t.ghost */\n// d.other_ghost\n", 1)
    keys = island_keys(html)
    assert "ghost_field" not in keys and "other_ghost" not in keys


def test_a_renamed_task_loop_is_refused() -> None:
    html = _html().replace("tasks.forEach(function(t){", "tasks.forEach(function(task){", 1)
    with pytest.raises(ExtractionRefused):
        island_keys(html)


def test_a_second_inline_script_is_refused() -> None:
    with pytest.raises(ExtractionRefused):
        island_keys(_html() + "<SCRIPT>d.extra;</SCRIPT>")


def test_the_contract_flattener_walks_the_task_list() -> None:
    assert "tasks[].pr" in contract_keys(ConductorBoardPanel)
    assert "tasks" not in contract_keys(ConductorBoardPanel)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_the_board_template_id_is_reachable_through_crew_selection() -> None:
    """A file named for no crew is rendered by nobody while every check here stays
    green, so the id is asserted through the real selection function."""
    assert agent_panel.template_for_crew(BOARD_CREW_NAME) == BOARD_TEMPLATE_ID
    assert BOARD_TEMPLATE_ID != agent_panel.DEFAULT_TEMPLATE_ID
    assert _template_path().is_file()


def test_the_template_reads_exactly_the_contract_declares() -> None:
    read = island_keys(_html())
    declared = contract_keys(ConductorBoardPanel)
    assert read - declared == set(), f"template reads undeclared fields: {sorted(read - declared)}"
    assert declared - read == set(), f"contract declares unread fields: {sorted(declared - read)}"


def test_an_undeclared_template_field_is_caught() -> None:
    html = _html().replace(
        "var upd = str(d.updated);", "var upd = str(d.updated) + str(d.owner);", 1
    )
    assert "owner" in island_keys(html) - contract_keys(ConductorBoardPanel)


def test_a_contract_field_the_template_ignores_is_caught() -> None:
    html = _html().replace("var nx = str(d.next);", 'var nx = "";', 1)
    assert "next" in contract_keys(ConductorBoardPanel) - island_keys(html)


def test_the_templates_states_are_the_contracts_states() -> None:
    """Field names match by the gate above; the VALUES a state may take do not, so a
    state the conductor writes that the template does not map would silently render
    in the neutral tone."""
    assert sorted(_js_object_keys(_script(_html()), "STATES")) == sorted(TASK_STATES)


def test_the_templates_steps_are_the_contracts_steps_in_order() -> None:
    assert _js_array(_script(_html()), "STEPS") == list(STEPS)


# ---------------------------------------------------------------------------
# rules for this template's behaviour
# ---------------------------------------------------------------------------


def test_all_motion_stops_under_reduced_motion() -> None:
    html = _html()
    m = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}", html, re.DOTALL
    )
    assert m, "no prefers-reduced-motion block"
    rule = m.group(1)
    assert "*::before" in rule and "*::after" in rule
    assert "animation:none !important" in rule
    assert "transition:none !important" in rule


def test_the_template_opens_nothing_and_fetches_nothing() -> None:
    """No links: the crew webview sandbox withholds allow-popups, so a new-tab link is
    a dead click. No network: the frame CSP closes egress and nothing here needs it."""
    src = _script(_html())
    for banned in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "window.open",
        "import(",
        'createElement("a")',
        "href",
    ):
        assert banned not in src, f"the template uses {banned!r}"
    assert "<a " not in _html().lower()


def test_colours_come_from_theme_variables() -> None:
    """Every colour literal sits in a var() fallback, so the host theme always wins."""
    css = re.search(r"<style>(.*?)</style>", _html(), re.DOTALL)
    assert css
    for line in css.group(1).splitlines():
        for hexval in re.findall(r"#[0-9a-fA-F]{3,8}\b", line):
            assert re.search(
                rf"var\(--[\w-]+,\s*{re.escape(hexval)}\)", line
            ), f"colour {hexval} is not a theme-variable fallback: {line.strip()}"


def test_a_pr_with_a_repo_asks_the_host_to_open_it() -> None:
    """The sandbox has no allow-popups: a PR opens only through the host's bridge,
    which honours exactly a github.com pull-request URL. No repo, no button."""
    src = _script(_html())
    assert 'parent.postMessage({ type: "kirocrew-dashboard:open", url: url }, "*")' in src
    assert src.count("postMessage(") == 1
    assert r"/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[0-9]+\/?$/" in src
    assert '"https://github.com/" + repo + "/pull/" + prNum[1]' in src
    assert "if (prNum && PR_URL.test(prUrl)){" in src
    assert 'el("button", "pr", "PR #" + prNum[1] + " on GitHub \\u2197")' in src
    assert '} else if (pr) card.appendChild(el("span", "pr",' in src
