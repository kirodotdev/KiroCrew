"""Workflow marker rows read as one human line in a linked channel thread."""

from __future__ import annotations

import pytest

from kiro_crew.messaging import workflow_markers as wm

TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "row, line",
    [
        (f"Start workflow `deploy` in `/w/app` (launch {TOKEN})", "Starting workflow deploy"),
        (f"Start workflow `` in `/w/app` (launch {TOKEN})", "Starting workflow workflow"),
        (f"Reconnect workflow runs (reconnect {TOKEN})", "Reconnecting workflow runs"),
        (
            f"[Workflow `deploy` completed] Kiro Agent's reply follows (observe {TOKEN})",
            "Workflow deploy completed",
        ),
        (f"[Workflow `deploy` aborted] reply (observe {TOKEN})", "Workflow deploy stopped"),
        (
            f"[Workflow `deploy` waiting] reply (observe {TOKEN})",
            "Workflow deploy is waiting for you",
        ),
        (f"[Workflow `` unknownword] reply (observe {TOKEN})", "Workflow workflow ended"),
    ],
)
def test_a_marker_row_becomes_its_human_line(row, line):
    assert wm.mirror_line(row) == line
    assert wm.mirror_text(row) == line


@pytest.mark.parametrize(
    "text",
    [
        "Start workflow `deploy` in `/w/app` (launch nottoken)",
        "Reconnect workflow runs",
        "an ordinary message",
        "",
    ],
)
def test_other_text_is_left_alone(text):
    assert wm.mirror_line(text) is None
    assert wm.mirror_text(text) == text


def test_a_non_string_is_not_a_marker():
    assert wm.mirror_line(None) is None  # type: ignore[arg-type]
