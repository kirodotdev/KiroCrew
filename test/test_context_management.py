"""Tests for context_management module."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def tmp_config(tmp_path):
    with patch("kiro_crew.context_management.config_dir", return_value=tmp_path):
        yield tmp_path


def test_cap_result_file_no_truncation(tmp_path):
    from kiro_crew.context_management import cap_result_file

    p = tmp_path / "small.md"
    p.write_text("short content")
    assert cap_result_file(p) is False
    assert p.read_text() == "short content"


def test_cap_result_file_truncates(tmp_path):
    from kiro_crew.context_management import RESULT_FILE_MAX_BYTES, cap_result_file

    p = tmp_path / "big.md"
    p.write_bytes(b"x" * (RESULT_FILE_MAX_BYTES + 10000))
    assert cap_result_file(p) is True
    assert p.stat().st_size <= RESULT_FILE_MAX_BYTES + 200  # marker overhead
    content = p.read_text()
    assert "truncated" in content


def test_cap_streaming_text_short():
    from kiro_crew.context_management import cap_streaming_text

    assert cap_streaming_text("short") == "short"


def test_cap_streaming_text_long():
    from kiro_crew.context_management import STREAMING_TEXT_MAX_CHARS, cap_streaming_text

    text = "a" * (STREAMING_TEXT_MAX_CHARS + 1000)
    result = cap_streaming_text(text)
    assert len(result) <= STREAMING_TEXT_MAX_CHARS + 20
    assert result.startswith("…(truncated)")


def test_cap_history():
    from kiro_crew.context_management import HISTORY_MAX_ENTRIES, cap_history

    entries = [{"i": i} for i in range(HISTORY_MAX_ENTRIES + 100)]
    result = cap_history(entries)
    assert len(result) == HISTORY_MAX_ENTRIES
    assert result[0]["i"] == 100  # oldest kept


def test_check_session_budget_under(tmp_path):
    from kiro_crew.context_management import check_session_budget

    (tmp_path / "agent-a.md").write_text("small")
    assert check_session_budget(tmp_path) is False


def test_check_session_budget_over(tmp_path):
    from kiro_crew.context_management import SESSION_MAX_BYTES, check_session_budget

    (tmp_path / "agent-a.md").write_bytes(b"x" * (SESSION_MAX_BYTES + 1))
    assert check_session_budget(tmp_path) is True


def test_evict_completed_agents():
    from kiro_crew.context_management import evict_completed_agents

    agents = {}
    for i in range(60):
        agents[f"a{i}"] = SimpleNamespace(done=True, started=float(i))
    evicted = evict_completed_agents(agents, max_retained=50)
    assert evicted == 10
    assert len(agents) == 50
    assert "a0" not in agents  # oldest evicted
    assert "a59" in agents  # newest kept


def test_evict_skips_running():
    from kiro_crew.context_management import evict_completed_agents

    agents = {
        "running": SimpleNamespace(done=False, started=0.0),
        "done1": SimpleNamespace(done=True, started=1.0),
    }
    evicted = evict_completed_agents(agents, max_retained=1)
    assert evicted == 0  # only 1 completed, within limit


def test_cleanup_stale_sessions(tmp_config):
    import time

    from kiro_crew.context_management import cleanup_stale_sessions

    sessions_dir = tmp_config / "sessions"
    sessions_dir.mkdir()
    old = sessions_dir / "old-session"
    old.mkdir()
    (old / "history.jsonl").write_text("{}")
    # Make it old
    import os

    old_time = time.time() - 86400 * 10
    os.utime(old / "history.jsonl", (old_time, old_time))

    new = sessions_dir / "new-session"
    new.mkdir()
    (new / "history.jsonl").write_text("{}")

    cleaned = cleanup_stale_sessions()
    assert cleaned == 1
    assert not old.exists()
    assert new.exists()


def test_orchestration_tracker_failure_limit():
    from kiro_crew.context_management import OrchestrationTracker

    t = OrchestrationTracker()
    assert t.record_failure("task-a") is False  # 1
    assert t.record_failure("task-a") is False  # 2
    assert t.record_failure("task-a") is True  # 3 — limit reached
    assert t.failure_count("task-a") == 3


def test_orchestration_tracker_success_resets():
    from kiro_crew.context_management import OrchestrationTracker

    t = OrchestrationTracker()
    t.record_failure("task-a")
    t.record_failure("task-a")
    t.record_success("task-a")
    assert t.failure_count("task-a") == 0
    assert t.record_failure("task-a") is False  # reset to 1


def test_orchestration_tracker_stage_timeout():
    from kiro_crew.context_management import OrchestrationTracker

    t = OrchestrationTracker(stage_timeout_seconds=10)
    assert t.is_stage_timed_out() is False  # no stage started
    t.record_round(1)  # starts timer
    assert t.is_stage_timed_out() is False  # just started
    # Simulate elapsed time
    t._stage_start = time.monotonic() - 11
    assert t.is_stage_timed_out() is True


def test_orchestration_tracker_timeout_zero_disables():
    from kiro_crew.context_management import OrchestrationTracker

    t = OrchestrationTracker(stage_timeout_seconds=0)
    t.record_round(1)
    t._stage_start = time.monotonic() - 9999
    assert t.is_stage_timed_out() is False  # disabled


def test_orchestration_tracker_timeout_human():
    from kiro_crew.context_management import OrchestrationTracker

    assert OrchestrationTracker(stage_timeout_seconds=90).timeout_human == "1m30s"
    assert OrchestrationTracker(stage_timeout_seconds=60).timeout_human == "1m"
    assert OrchestrationTracker(stage_timeout_seconds=45).timeout_human == "45s"
    assert OrchestrationTracker(stage_timeout_seconds=1800).timeout_human == "30m"


def test_stage_timeout_resets_after_guidance():
    from kiro_crew.context_management import OrchestrationTracker

    t = OrchestrationTracker(stage_timeout_seconds=10)
    t.record_round(1)  # starts timer
    assert t._stage_start > 0
    t.reset_after_guidance()  # clears timer (task failure path)
    assert t._stage_start == 0.0
    t.record_round(1)  # must restart timer — core fix
    assert t._stage_start > 0
    assert not t.is_stage_timed_out()


# ── Orchestration tracker: additional coverage ──────────────────────


def test_tracker_round_limit():
    from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

    t = OrchestrationTracker()
    for _ in range(MAX_STAGE_ROUNDS - 1):
        assert t.record_round(1) is False
    assert t.record_round(1) is True  # limit reached
    assert t.round_count(1) == MAX_STAGE_ROUNDS


def test_tracker_escalation_and_force_fail():
    from kiro_crew.context_management import (
        MAX_STAGE_ROUNDS,
        OrchestrationTracker,
    )

    t = OrchestrationTracker()
    # First escalation: hit round limit, then reset
    for _ in range(MAX_STAGE_ROUNDS):
        t.record_round(1)
    assert t.has_escalated
    t.reset_after_guidance()
    assert t.round_count(1) == 0
    assert not t.is_force_failed(1)

    # Second escalation: hit round limit again, then reset → force-fail
    for _ in range(MAX_STAGE_ROUNDS):
        t.record_round(1)
    t.reset_after_guidance()
    assert t.is_force_failed(1)


def test_tracker_current_stage_default():
    from kiro_crew.context_management import OrchestrationTracker

    t = OrchestrationTracker()
    assert t.current_stage == 1  # default when no rounds recorded


def test_tracker_stop():
    from kiro_crew.context_management import OrchestrationTracker

    t = OrchestrationTracker()
    assert not t.stopped
    t.stop()
    assert t.stopped


def test_tracker_reset_clears_task_failures():
    from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker

    t = OrchestrationTracker()
    t.record_failure("task-a")
    t.record_failure("task-a")
    # Need to hit round limit to trigger has_escalated
    for _ in range(MAX_STAGE_ROUNDS):
        t.record_round(1)
    t.reset_after_guidance()
    assert t.failure_count("task-a") == 0


# ── looks_like_plan ─────────────────────────────────────────────────


def test_looks_like_plan_true():
    from kiro_crew.context_management import looks_like_plan

    assert looks_like_plan("Phase 1: Setup\n- Install deps\nPhase 2: Build\n- Compile") is True


def test_looks_like_plan_true_numbered_bold():
    from kiro_crew.context_management import looks_like_plan

    assert (
        looks_like_plan("1. **Analysis**: check\n2. **Implementation**: code\n3. **Test**: verify")
        is True
    )


def test_looks_like_plan_true_stage_keyword():
    from kiro_crew.context_management import looks_like_plan

    assert looks_like_plan("Stage 1: Setup\n- Install deps\nStage 2: Build\n- Compile") is True


def test_looks_like_plan_false_single_match():
    from kiro_crew.context_management import looks_like_plan

    assert looks_like_plan("Step 1: Do something\nThen do other things") is False


def test_looks_like_plan_false_no_matches():
    from kiro_crew.context_management import looks_like_plan

    assert looks_like_plan("Here's what happened: the build failed because of a typo.") is False


# ── looks_like_plan: shapes that are numbered but are not plans ──────
#
# Every false positive here buys an LLM rephrase on the cheap background session
# (2-8s) whose only possible answer is NOT_A_PLAN. The filter stays loose on
# purpose -- prose that genuinely reads like a plan is the downstream call's job
# -- so what these pin is the one mechanical distinction available without a
# model: a plan numbers its steps 1, 2, 3, and text that merely contains numbers
# does not.


def test_looks_like_plan_false_two_item_bold_list():
    """ "1. **Yes** / 2. **No**" is a two-option write-up, not a plan.

    The bold-list shape carries no stage vocabulary at all, so it needs a longer
    run than the `Stage N:` shape before it counts. A run that MIXES the shapes
    qualifies at two, because the stage line in it is the vocabulary this one
    lacks -- see ``test_plan_detection_breadth.py``.
    """
    from kiro_crew.context_management import looks_like_plan

    assert looks_like_plan("1. **Yes** we can.\n2. **No** we cannot.") is False


def test_looks_like_plan_false_bold_list_not_starting_at_one():
    """A findings excerpt numbered from the middle of a longer list."""
    from kiro_crew.context_management import looks_like_plan

    text = "3. **Alpha** did X\n4. **Beta** did Y\n5. **Gamma** did Z"
    assert looks_like_plan(text) is False


def test_looks_like_plan_false_bold_list_all_numbered_one():
    """Markdown renders `1.` repeated as an ordered list; it is not a sequence."""
    from kiro_crew.context_management import looks_like_plan

    assert looks_like_plan("1. **Alpha**\n1. **Beta**\n1. **Gamma**") is False


def test_looks_like_plan_false_stage_lines_not_starting_at_one():
    """Prose walking through the middle of a process it already introduced."""
    from kiro_crew.context_management import looks_like_plan

    text = "Step 2: the lexer runs.\nStep 3: the parser builds the AST."
    assert looks_like_plan(text) is False


def test_looks_like_plan_false_repeated_same_stage_number():
    """Two worked examples of the same step counted as two matches before."""
    from kiro_crew.context_management import looks_like_plan

    text = "Step 1: run it with --dry-run.\n\nStep 1: run it for real."
    assert looks_like_plan(text) is False


def test_looks_like_plan_false_markdown_headings():
    """A write-up whose sections are headings is document structure."""
    from kiro_crew.context_management import looks_like_plan

    assert looks_like_plan("## Step 1: Parse\n## Step 2: Emit") is False


def test_looks_like_plan_true_four_stage_lines():
    """Preservation: a real header-less plan still reaches the rephrase."""
    from kiro_crew.context_management import looks_like_plan

    text = "Phase 1: Survey\nPhase 2: Build\nPhase 3: Verify\nPhase 4: Ship"
    assert looks_like_plan(text) is True


# ── cap_rephrase_input ──────────────────────────────────────────────


def test_cap_rephrase_input_leaves_a_plan_alone():
    from kiro_crew.context_management import REPHRASE_INPUT_MAX_CHARS, cap_rephrase_input

    plan = "📋 Plan for: x\n\nStage 1: Do it\n- step\n\n[OPTION: Go | Go All | Cancel]"
    assert len(plan) < REPHRASE_INPUT_MAX_CHARS
    assert cap_rephrase_input(plan) == plan


def test_cap_rephrase_input_caps_a_long_turn():
    from kiro_crew.context_management import cap_rephrase_input

    out = cap_rephrase_input("x" * 40_000)

    assert len(out) < 40_000
    assert "[...truncated" in out


def test_cap_rephrase_input_keeps_both_ends():
    """Head AND tail: the plan is usually at the end, the subject at the start."""
    from kiro_crew.context_management import REPHRASE_INPUT_MAX_CHARS, cap_rephrase_input

    text = "HEAD" + ("x" * (REPHRASE_INPUT_MAX_CHARS * 3)) + "TAIL"

    out = cap_rephrase_input(text)

    assert out.startswith("HEAD")
    assert out.endswith("TAIL")


def test_cap_rephrase_input_never_exceeds_its_own_cap():
    """RED BEFORE: the marker was added on top of a full cap's worth of text.

    The marker is part of what the model receives, so a cap that budgets only the
    source text states a ceiling the function does not honour.
    """
    from kiro_crew.context_management import REPHRASE_INPUT_MAX_CHARS, cap_rephrase_input

    for size in (REPHRASE_INPUT_MAX_CHARS + 1, REPHRASE_INPUT_MAX_CHARS * 4, 10_000_000):
        out = cap_rephrase_input("q" * size)
        assert len(out) <= REPHRASE_INPUT_MAX_CHARS, f"{size} chars in -> {len(out)} out"


def test_cap_rephrase_input_is_tail_heavy():
    from kiro_crew.context_management import REPHRASE_INPUT_MAX_CHARS, cap_rephrase_input

    text = "a" * (REPHRASE_INPUT_MAX_CHARS * 3)

    out = cap_rephrase_input(text)
    head, _, tail = out.partition("\n\n[...truncated")

    assert len(head) < len(tail)


@pytest.mark.asyncio
async def test_rephrase_plan_sends_a_capped_prompt():
    """RED BEFORE: the whole assistant turn went into the prompt.

    Asserted on the prompt the LLM actually receives, because the cap is only
    worth anything at that boundary.
    """
    from kiro_crew.context_management import REPHRASE_INPUT_MAX_CHARS, rephrase_plan

    huge = "y" * (REPHRASE_INPUT_MAX_CHARS * 4)
    with patch("kiro_crew.llm_helpers.stream_and_collect", new_callable=AsyncMock) as mock_stream:
        mock_stream.return_value = "NOT_A_PLAN"
        await rephrase_plan(huge, ["No header"], AsyncMock(), might_not_be_plan=True)

    prompt = mock_stream.call_args.args[1]
    assert huge not in prompt
    assert "[...truncated" in prompt


@pytest.mark.asyncio
async def test_rephrase_plan_caps_the_reformat_prompt_too():
    """Both prompts in this function carry the turn; both must be capped."""
    from kiro_crew.context_management import REPHRASE_INPUT_MAX_CHARS, rephrase_plan

    huge = "z" * (REPHRASE_INPUT_MAX_CHARS * 4)
    with patch("kiro_crew.llm_helpers.stream_and_collect", new_callable=AsyncMock) as mock_stream:
        mock_stream.return_value = ""
        await rephrase_plan(huge, ["Missing footer"], AsyncMock())

    prompt = mock_stream.call_args.args[1]
    assert huge not in prompt


# ── rephrase_plan (might_not_be_plan) ───────────────────────────────


@pytest.mark.asyncio
async def test_rephrase_plan_not_a_plan_returns_none():
    """When LLM returns NOT_A_PLAN: prefix, rephrase_plan returns None."""
    from kiro_crew.context_management import rephrase_plan

    client = AsyncMock()
    client.send_message = AsyncMock(return_value=None)

    with patch("kiro_crew.llm_helpers.stream_and_collect", new_callable=AsyncMock) as mock_stream:
        mock_stream.return_value = "NOT_A_PLAN"
        result = await rephrase_plan(
            "some analysis text", ["No header"], client, might_not_be_plan=True
        )
    assert result is None


@pytest.mark.asyncio
async def test_rephrase_plan_is_a_plan_returns_reformatted():
    """When LLM returns a valid plan, rephrase_plan returns it."""
    from kiro_crew.context_management import rephrase_plan

    reformatted = "📋 Plan for: task\n\nStage 1: Do it\n- step\n\n[OPTION: Go | Go All | Cancel]"
    with patch("kiro_crew.llm_helpers.stream_and_collect", new_callable=AsyncMock) as mock_stream:
        mock_stream.return_value = reformatted
        result = await rephrase_plan(
            "Phase 1: Do it", ["No header"], AsyncMock(), might_not_be_plan=True
        )
    assert result == reformatted


# ── validate_plan_format ────────────────────────────────────────────


def test_validate_plan_format_valid():
    from kiro_crew.context_management import validate_plan_format

    plan = '📋 Plan for: "test"\n\nStage 1: Setup\n- task\n\nStage 2: Build\n- task\n\n[OPTION: Go | Go All | Cancel]'
    has_plan, valid, issues = validate_plan_format(plan)
    assert has_plan and valid and not issues


def test_validate_plan_format_no_header():
    from kiro_crew.context_management import validate_plan_format

    has_plan, valid, issues = validate_plan_format("Stage 1: Setup\n[OPTION: Go | Cancel]")
    assert not has_plan


def test_validate_plan_format_no_stages():
    from kiro_crew.context_management import validate_plan_format

    has_plan, valid, issues = validate_plan_format(
        '📋 Plan for: "test"\n\n[OPTION: Go | Go All | Cancel]'
    )
    assert has_plan and not valid
    assert any("Stage" in i for i in issues)


def test_validate_plan_format_no_option():
    from kiro_crew.context_management import validate_plan_format

    has_plan, valid, issues = validate_plan_format('📋 Plan for: "test"\n\nStage 1: Setup\n- task')
    assert has_plan and not valid
    assert any("OPTION" in i for i in issues)


def test_validate_plan_format_non_sequential_stages():
    from kiro_crew.context_management import validate_plan_format

    plan = '📋 Plan for: "test"\n\nStage 1: A\nStage 3: B\n\n[OPTION: Go | Go All | Cancel]'
    has_plan, valid, issues = validate_plan_format(plan)
    assert has_plan and not valid
    assert any("sequential" in i.lower() for i in issues)


# ── strip_plan_markers ──────────────────────────────────────────────


def test_strip_plan_markers():
    from kiro_crew.context_management import strip_plan_markers

    plan = '📋 Plan for: "test"\n\nStage 1: Setup\n- install deps\n\n[OPTION: Go | Go All | Cancel]'
    stripped = strip_plan_markers(plan)
    assert "📋" not in stripped
    assert "[OPTION:" not in stripped
    assert "install deps" in stripped
