from __future__ import annotations

from kiro_crew.dashboard.warm_task_brief_checking import checking_text


def test_names_each_nonzero_count_first_then_the_goal():
    assert checking_text("en", "Ship the fix", 2, 3, 1) == (
        "Checking 2 PRs on GitHub, 3 tasks, 1 chat for “Ship the fix”."
    )


def test_zero_counts_drop_and_none_left_names_only_the_goal():
    assert checking_text("en", "Ship", 1, 0, 0) == "Checking 1 PR on GitHub for “Ship”."
    assert checking_text("en", "Ship", 0, 0, 0) == "Catching you up on “Ship”."


def test_follows_the_owner_language_and_falls_back_to_english():
    assert (
        checking_text("zh", "修复", 2, 3, 0)
        == "正在查看GitHub 上的 2 个 PR、3 个任务，整理“修复”的进展。"
    )
    assert checking_text("xx", "Ship", 0, 1, 0) == "Checking 1 task for “Ship”."


def test_a_long_or_multiline_goal_is_cut_to_a_few_words():
    text = checking_text("en", "word " * 40 + "\nsecond line {n}", 0, 0, 0)
    goal = text.split("“")[1].split("”")[0]
    assert "\n" not in goal and len(goal) <= 60 and goal.endswith("…")
    # Owner text is a value, never a format string.
    assert checking_text("en", "{parts} {goal}", 0, 1, 0) == "Checking 1 task for “{parts} {goal}”."
