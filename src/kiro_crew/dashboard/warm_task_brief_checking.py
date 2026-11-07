"""The caption the warm task brief's indicator shows while it is prepared.

One short line saying what the brief checks and what it is about, so the owner
knows to wait. The gateway builds it from the same inputs the brief is written
from (the goal, the pull requests read live, the open work items, the running
worker chats); no model writes it and nothing is read just for it. It rides
the slot's display-only ``preparing`` flag, so the brief replaces it in place
and it drops with the flag on failure, cancel or a stale result.

The counts come first: a Bot thread draws the caption on one pinned line, so on
a narrow screen the end is cut, and the goal is the part the owner already
knows.
"""

from __future__ import annotations

_MAX_GOAL_CHARS = 60

# Per language: the line with counts, the line without any, the joiner, and
# one (singular, plural) pair per counted input. Languages without a plural
# form repeat one form. Same script pick as the brief's fixed lines.
_TEXT = {
    "en": (
        "Checking {parts} for “{goal}”.",
        "Catching you up on “{goal}”.",
        ", ",
        (
            ("{n} PR on GitHub", "{n} PRs on GitHub"),
            ("{n} task", "{n} tasks"),
            ("{n} chat", "{n} chats"),
        ),
    ),
    "zh": (
        "正在查看{parts}，整理“{goal}”的进展。",
        "正在整理“{goal}”的进展。",
        "、",
        (("GitHub 上的 {n} 个 PR",) * 2, ("{n} 个任务",) * 2, ("{n} 个对话",) * 2),
    ),
    "ja": (
        "{parts}を確認して「{goal}」をまとめています。",
        "「{goal}」の進み具合をまとめています。",
        "、",
        (("GitHub の PR {n} 件",) * 2, ("タスク {n} 件",) * 2, ("チャット {n} 件",) * 2),
    ),
    "ko": (
        "{parts} 확인 중 · “{goal}” 정리 중이에요.",
        "“{goal}” 진행 상황을 정리하고 있어요.",
        ", ",
        (("GitHub PR {n}개",) * 2, ("작업 {n}개",) * 2, ("대화 {n}개",) * 2),
    ),
    "ru": (
        "Проверяю: {parts} — по «{goal}».",
        "Собираю, как идут дела по «{goal}».",
        ", ",
        (("PR на GitHub: {n}",) * 2, ("задачи: {n}",) * 2, ("чаты: {n}",) * 2),
    ),
    "hi": (
        "{parts} जाँचे जा रहे हैं, “{goal}” के लिए।",
        "“{goal}” का हाल तैयार हो रहा है।",
        ", ",
        (("GitHub पर {n} PR",) * 2, ("{n} काम",) * 2, ("{n} चैट",) * 2),
    ),
    "bn": (
        "{parts} দেখা হচ্ছে, “{goal}”-এর জন্য।",
        "“{goal}”-এর খবর গোছানো হচ্ছে।",
        ", ",
        (("GitHub-এ {n}টি PR",) * 2, ("{n}টি কাজ",) * 2, ("{n}টি চ্যাট",) * 2),
    ),
}


def checking_text(language: str, goal: str, prs: int, tasks: int, chats: int) -> str:
    """The indicator caption in *language* (English when unknown).

    Zero counts are left out; with none left the line names only the goal.
    """
    with_parts, goal_only, joiner, forms = _TEXT.get(language, _TEXT["en"])
    short = " ".join(goal.split())
    if len(short) > _MAX_GOAL_CHARS:
        short = short[: _MAX_GOAL_CHARS - 1].rstrip() + "…"
    parts = [
        pair[n != 1].format(n=n)
        for pair, n in zip(forms, (prs, tasks, chats), strict=True)
        if n > 0
    ]
    if not parts:
        return goal_only.format(goal=short)
    return with_parts.format(parts=joiner.join(parts), goal=short)
