"""Every backend reader of a protocol lead reads both spellings.

Current gateways write a plain word (``Tool:``, ``Error:``, ``completed``);
saved history keeps the glyph older gateways wrote. Each reader is fed both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import crew_timeline, task_reporter
from kiro_crew.dashboard.system_notices import is_subagent_completion_row
from kiro_crew.messaging.driver import is_internal_compaction_notice
from kiro_crew.tool_row_prefix import strip_tool_row_lead, tool_row, tool_row_kind


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("\U0001f527 bash", "call"),
        ("Tool: bash", "call"),
        ("\u2705 bash", "approved"),
        ("Approved: bash", "approved"),
        ("\U0001f6ab bash — Blocked", "refused"),
        ("Refused: bash — Blocked", "refused"),
        ("bash", None),
    ],
)
def test_tool_row_kind_reads_both_leads(content: str, kind: str | None) -> None:
    assert tool_row_kind(content) == kind
    if kind:
        assert strip_tool_row_lead(content).startswith("bash")


def test_tool_row_writes_the_word() -> None:
    assert tool_row("call", "bash") == "Tool: bash"
    assert tool_row("approved", "bash") == "Approved: bash"
    assert tool_row("refused", "bash — x") == "Refused: bash — x"


@pytest.mark.parametrize("content", ["\u274c boom", "Error: boom"])
def test_crew_timeline_failure_reads_both_leads(content: str) -> None:
    assert crew_timeline._is_failure({"role": "assistant", "content": content})


def test_crew_timeline_failure_ignores_plain_text() -> None:
    assert not crew_timeline._is_failure({"role": "assistant", "content": "Errors happen"})


@pytest.mark.parametrize("lead", ["\u2705", "Passed"])
def test_task_checkpoint_reads_both_leads(tmp_path: Path, lead: str) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("x", encoding="utf-8")
    (tmp_path / task_reporter.PROGRESS_FILE).write_text(
        f"- {lead} **Task 1:** Build it (attempts: 1)\n- Failed **Task 2:** Other\n",
        encoding="utf-8",
    )
    assert task_reporter.load_checkpoint(spec) == {"build it"}


@pytest.mark.parametrize(
    "header",
    [
        "completed \u2705",
        "completed",
        "failed: lost to gateway restart",
        "\u274c lost to gateway restart",
        "stopped by user",
        "interrupted: cut off mid-turn by gateway restart",
    ],
)
def test_subagent_completion_row_reads_both_headers(header: str) -> None:
    content = f"[Subagent completion event]\nAgent `ab12` {header}\nTask: look\n\nbody"
    assert is_subagent_completion_row(content, None)


@pytest.mark.parametrize(
    "tally",
    [
        "1 \u2705 · 0 \u274c · 0 \u23f9 of 1 agents.",
        "1 completed · 0 failed · 0 stopped of 1 agents.",
    ],
)
def test_subagent_wave_row_reads_both_tallies(tally: str) -> None:
    content = f"[Subagent batch completion event]\nBatch results 1/1 — wave finished: {tally}\n\nx"
    assert is_subagent_completion_row(content, None)


def test_subagent_header_without_outcome_is_not_a_completion() -> None:
    assert not is_subagent_completion_row("[Subagent completion event]\nAgent `ab12`\n", None)


@pytest.mark.parametrize(
    "text",
    ["\u2705 Conversation compacted: summary", "Conversation compacted: summary"],
)
def test_driver_reserves_both_compaction_leads(text: str) -> None:
    assert is_internal_compaction_notice(text)
