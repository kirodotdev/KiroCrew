"""Mermaid flattening: every edge a line draws reaches the reader, on every channel.

One shared reading (:func:`kiro_crew.messaging.markup.flatten_mermaid_body`)
feeds both the WhatsApp renderer and Slack's ``_convert_mermaid``, so each case
runs through both: a fix that reaches only one channel fails here.
"""

from __future__ import annotations

import pytest

from kiro_crew.messaging.markup import flatten_mermaid_body
from kiro_crew.slack import format as slack_format


def _shared(body: str) -> str:
    return flatten_mermaid_body(body)


def _slack(body: str) -> str:
    return slack_format._convert_mermaid(f"```mermaid\n{body}\n```")


RENDERERS = [pytest.param(_shared, id="shared"), pytest.param(_slack, id="slack")]


@pytest.mark.parametrize("render", RENDERERS)
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "graph TD\nA[Start] --> B[Mid] --> C[End]",
            "  Start → Mid\n  Mid → End",
            id="a_chain_draws_one_edge_per_arrow",
        ),
        pytest.param(
            "flowchart LR\nA -->|yes| B -->|no| C",
            "  A → (yes) B\n  B → (no) C",
            id="each_arrow_in_a_chain_keeps_its_own_label",
        ),
        pytest.param(
            "graph TD\nA --> B & C",
            "  A → B\n  A → C",
            id="an_and_group_target_fans_out",
        ),
        pytest.param(
            "graph TD\nA & B --> C",
            "  A → C\n  B → C",
            id="an_and_group_source_fans_in",
        ),
        pytest.param(
            "graph TD\nA[One] & B --> C{Two} & D",
            "  One → Two\n  One → D\n  B → Two\n  B → D",
            id="groups_on_both_sides_draw_every_pair",
        ),
        pytest.param(
            "graph TD\nA(Round label) & B --> C",
            "  A → C\n  B → C",
            id="a_round_label_is_not_read_as_more_nodes",
        ),
        pytest.param(
            "graph TD\nA[Start] -->|go| B[Done]",
            "  Start → (go) Done",
            id="a_single_edge_keeps_its_reading",
        ),
        pytest.param(
            "graph TD\nA[Start]; A --> B[Done]\nC --> D",
            "  Start → Done\n  C → D",
            id="a_bare_node_and_edge_can_share_a_line",
        ),
        pytest.param(
            "graph TD\nA --> B; B --> C",
            "  A → B\n  B → C",
            id="semicolon_separated_edges_are_all_shown",
        ),
        pytest.param(
            "graph TD\nA[x; y] --> B",
            "  x; y → B",
            id="a_semicolon_inside_a_label_stays_in_the_label",
        ),
        pytest.param(
            "graph TD\nA --> B;",
            "  A → B",
            id="a_trailing_semicolon_keeps_the_edge",
        ),
        pytest.param(
            "sequenceDiagram\nA->>+B: start\nB-->>-A: done",
            "  A → B: start\n  B ⇒ A: done",
            id="activation_markers_are_not_part_of_the_target",
        ),
    ],
)
def test_every_edge_a_line_draws_is_shown(render, body, expected):
    assert render(body) == expected


@pytest.mark.parametrize("render", RENDERERS)
def test_a_comment_that_quotes_an_edge_draws_nothing(render):
    out = render("graph TD\n%% A --> B --> C\nC --> D")
    assert out == "  C → D"


@pytest.mark.parametrize("render", RENDERERS)
def test_a_trailing_arrow_drops_only_itself(render):
    out = render("graph TD\nA --> B -->\nC --> D")
    assert out == "  A → B\n  C → D"


def _fan(sources: int, targets: int) -> str:
    """A one-line flowchart whose two ``&`` groups draw ``sources * targets`` edges."""
    left = " & ".join(f"S{index}" for index in range(sources))
    right = " & ".join(f"T{index}" for index in range(targets))
    return f"graph TD\n{left} --> {right}"


@pytest.mark.parametrize("render", RENDERERS)
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_fan(100, 100), id="one_line_over_the_cap"),
        pytest.param(_fan(20, 25) + "\nX --> Y", id="lines_adding_up_past_the_cap"),
    ],
)
def test_a_diagram_over_the_edge_cap_is_shown_as_written(render, body):
    expected = body if render is _slack else ""
    assert render(body) == expected


@pytest.mark.parametrize("render", RENDERERS)
def test_a_diagram_at_the_edge_cap_still_flattens(render):
    out = render(_fan(20, 25))
    lines = out.split("\n")
    assert len(lines) == 500
    assert lines[0] == "  S0 → T0"
    assert lines[-1] == "  S19 → T24"
