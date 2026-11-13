"""The warm welcome, written by the gateway (RFC ``rfc-delayed-task-brief.md`` 3.11).

A short, plain recap in the owner's language, in four parts: the goal, what needs
the owner, what is being done now, and what comes after. Every state, count and
time is the gateway's own read: the work ledger, the Board's "waits on you" test
(``awaits_person``) and this welcome's live pull-request read. The why, each option
with what it leads to and the pick are judgement, so they come only from the
conductor's ``ask`` record on the item (``work_ask_record``); an item
waiting with no record says plainly that they were not written yet. No model writes
any of it, and nothing here says who wrote a record.

:func:`render` returns markdown; every ledger string reaches it through *clean*.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.work_vocab import (
    WORK_ASK_LABEL_CHARS,
    WORK_ASK_OUTCOME_CHARS,
    WORK_ASK_WHY_CHARS,
)

_MAX_GOAL = 300
_MAX_TITLE = 200
# The ask record's own caps (work_vocab), so the welcome never cuts what the store kept.
_MAX_WHY = WORK_ASK_WHY_CHARS
_MAX_LABEL = WORK_ASK_LABEL_CHARS
_MAX_OUTCOME = WORK_ASK_OUTCOME_CHARS

_KEYS = (
    "goal", "needs", "nothing", "why", "pick", "not_written", "card_not_written",
    "options_below", "doing", "next", "plan", "checked", "question_card", "change_card",
    "waiting", "progress", "blocked", "question", "done", "started",
    "merged", "closed", "draft", "open", "conflicts", "failed", "pending", "passed",
    "not_read", "changes_requested", "approved", "review_required", "not_checked", "never",
    "reply", "github", "tap_reply",
)  # fmt: skip
# One "|"-separated row per language, in _KEYS order.
_ROWS = {
    "en": "Goal|Needs you|Nothing needs you right now.|Why|Recommended"
    "|The reason and options were not written yet."
    "|Why it needs you and what each option leads to were not written yet."
    "|The choices are in the buttons below.|Being done now|Comes after|plan|checked {at}"
    "|question card|change card|waiting on you|in progress|blocked|has a question"
    "|reported done|started, no report yet|merged|closed|draft|open|conflicts|{n} checks failed"
    "|{n} checks running|checks passed|checks not fully read|changes requested|approved"
    "|review required|not read since {at}|not read|answered a follow-up|GitHub {at}|Tap an option below to reply.",
    "zh": "目标|需要你|现在不需要你做什么。|原因|推荐|原因和选项还没写。"
    "|为什么要你处理、每个选项会怎样，还没写。|选项在下面的按钮里。|正在做|之后|计划|{at} 核实"
    "|问题卡片|变更卡片|等你处理|进行中|卡住了|有问题|报了完成|已开始，还没汇报|已合并|已关闭"
    "|草稿|开着|有冲突|{n} 项检查失败|{n} 项检查在跑|检查全过|检查没读全|要求修改|已批准|等审阅"
    "|{at} 后没读到|没读到|回答了追问|GitHub {at}|点下面的选项，就是发出你的回复。",
    "ja": "目標|あなたの番|今あなたにお願いすることはありません。|理由|おすすめ"
    "|理由と選択肢はまだ書かれていません。|あなたが必要な理由と各選択肢の結果はまだ書かれていません。"
    "|選択肢は下のボタンにあります。|進行中の作業|この後|予定|{at} 確認|質問カード|変更カード"
    "|あなた待ち|進行中|止まっています|質問あり|完了報告|開始済み、報告はまだ|マージ済み"
    "|クローズ|ドラフト|オープン|競合あり|{n} 件のチェック失敗|{n} 件のチェック実行中"
    "|チェック成功|チェック未読|修正依頼|承認済み|レビュー待ち|{at} から未読|未読|追加の質問に回答|GitHub {at}|下の選択肢を押すと、それがあなたの返信になります。",
    "ko": "목표|확인 필요|지금은 하실 일이 없어요.|이유|추천|이유와 선택지는 아직 안 적혔어요."
    "|왜 필요한지, 각 선택지가 어떻게 되는지 아직 안 적혔어요.|선택지는 아래 버튼에 있어요."
    "|지금 하는 일|다음 계획|계획|{at} 확인|질문 카드|변경 카드|답변 대기|진행 중|막힘|질문 있음"
    "|완료 보고|시작함, 아직 보고 없음|병합됨|닫힘|초안|열림|충돌|검사 {n}개 실패|검사 {n}개 실행 중"
    "|검사 통과|검사 다 못 읽음|변경 요청|승인됨|리뷰 필요|{at} 이후 못 읽음|못 읽음|추가 질문에 답함|GitHub {at}|아래 선택지를 누르면 그대로 답장이 보내져요.",
    "ru": "Цель|Нужно от вас|Сейчас от вас ничего не нужно.|Почему|Совет"
    "|Причина и варианты ещё не записаны.|Почему нужно ваше решение и к чему ведёт каждый вариант,"
    " ещё не записано.|Варианты — в кнопках ниже.|Делается сейчас|Дальше|план|проверено в {at}"
    "|карточка вопроса|карточка изменения|ждёт вас|в работе|застряло|есть вопрос|сдано"
    "|начато, отчёта нет|слит|закрыт|черновик|открыт|конфликты|упало проверок: {n}"
    "|идёт проверок: {n}|проверки прошли|проверки прочитаны не все|просят правки|одобрен"
    "|ждёт ревью|не прочитано с {at}|не прочитано|ответил на уточнение|GitHub {at}|Нажмите вариант ниже, чтобы ответить.",
    "hi": "लक्ष्य|आपसे चाहिए|अभी आपको कुछ नहीं करना है।|क्यों|सुझाव"
    "|कारण और विकल्प अभी लिखे नहीं गए।|आपकी ज़रूरत क्यों है और हर विकल्प से क्या होगा, अभी लिखा नहीं गया।"
    "|विकल्प नीचे के बटनों में हैं।|अभी चल रहा है|आगे|योजना|{at} पर जाँचा|सवाल कार्ड|बदलाव कार्ड"
    "|आपका इंतज़ार|चल रहा|अटका है|सवाल है|पूरा बताया|शुरू हुआ, रिपोर्ट नहीं|मर्ज हुआ|बंद|ड्राफ़्ट"
    "|खुला|टकराव|{n} जाँच विफल|{n} जाँच चल रही|जाँच पास|जाँच पूरी नहीं पढ़ी|बदलाव माँगे|मंज़ूर"
    "|समीक्षा बाकी|{at} से नहीं पढ़ा|नहीं पढ़ा|आगे के सवाल का जवाब दिया|GitHub {at}|जवाब देने के लिए नीचे कोई विकल्प दबाएँ।",
    "bn": "লক্ষ্য|আপনার কাছে দরকার|এখন আপনার কিছু করার নেই।|কেন|পরামর্শ"
    "|কারণ আর বিকল্প এখনও লেখা হয়নি।|কেন আপনাকে দরকার আর প্রতিটি বিকল্পে কী হবে, এখনও লেখা হয়নি।"
    "|বিকল্পগুলো নিচের বোতামে আছে।|এখন যা চলছে|এরপর|পরিকল্পনা|{at}-এ যাচাই|প্রশ্ন কার্ড"
    "|পরিবর্তন কার্ড|আপনার অপেক্ষায়|চলছে|আটকে আছে|প্রশ্ন আছে|শেষ জানিয়েছে|শুরু হয়েছে, রিপোর্ট নেই"
    "|মার্জ হয়েছে|বন্ধ|খসড়া|খোলা|দ্বন্দ্ব|{n}টি যাচাই ব্যর্থ|{n}টি যাচাই চলছে|যাচাই পাস"
    "|যাচাই পুরো পড়া হয়নি|পরিবর্তন চাওয়া|অনুমোদিত|রিভিউ বাকি|{at} থেকে পড়া হয়নি|পড়া হয়নি|পরের প্রশ্নের উত্তর দিয়েছে|GitHub {at}|উত্তর দিতে নিচের একটি বিকল্প চাপুন।",
}
WORDS: dict[str, dict[str, str]] = {
    lang: dict(zip(_KEYS, row.split("|"), strict=True)) for lang, row in _ROWS.items()
}
# Two words joined, per script.
_SEP = {"zh": "，", "ja": "、"}
_COLON = {"zh": "：", "ja": "："}
_PARENS = {"zh": ("（", "）"), "ja": ("（", "）")}
# Words of one fact list, per script.
_LIST = {"zh": "、", "ja": "・"}

_TASK_STATUSES = ("progress", "blocked", "question", "done", "reply")


def pr_words(entry: Mapping[str, Any] | None, words: Mapping[str, str]) -> list[str]:
    """One live read as words, bad facts first. A failed read says since when.
    Value words are case-folded: GitHub answers ``MERGED`` as readily as ``merged``."""
    if entry is None:
        return []
    if entry.get("checked") is not True:
        since = str(entry.get("not_checked_since") or "never")
        return [words["never"] if since == "never" else words["not_checked"].format(at=since)]
    bad: list[str] = []
    good: list[str] = []
    state = str(entry.get("state") or "").lower()
    if state in ("merged", "draft", "open"):
        good.append(words[state])
    elif state == "closed":
        bad.append(words["closed"])
    if str(entry.get("mergeable") or "").lower() in ("conflicting", "dirty"):
        bad.append(words["conflicts"])
    review = str(entry.get("review") or "").lower()
    if review == "changes_requested":
        bad.append(words[review])
    elif review in ("approved", "review_required"):
        good.append(words[review])
    checks = entry.get("checks")
    if isinstance(checks, Mapping):
        failed, pending = _count(checks.get("failed")), _count(checks.get("pending"))
        if failed:
            bad.append(words["failed"].format(n=failed))
        if pending:
            good.append(words["pending"].format(n=pending))
        if not failed and not pending and not checks.get("unknown") and checks.get("passed"):
            good.append(words["passed"])
    elif isinstance(checks, str) and checks:
        good.append(words["not_read"])
    return bad + good


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _pr_part(
    item: Mapping[str, Any],
    live: Mapping[str, Mapping[str, Any]],
    words: Mapping[str, str],
    language: str,
) -> str:
    """``repo#N: words`` for the item's ledger PR when this welcome read it, else ""."""
    ref = inputs.ledger_ref(item)
    entry = live.get(ref.url) if ref is not None else None
    if ref is None or entry is None:
        return ""
    said = pr_words(entry, words)
    # The PR's own read time: a cached read keeps the time GitHub was read, never
    # the time the ledger was.
    if entry.get("checked") is True and entry.get("read_at"):
        said.append(words["github"].format(at=entry["read_at"]))
    colon = _COLON.get(language, ": ")
    return f"{ref.repo}#{ref.number}" + (
        colon + _LIST.get(language, ", ").join(said) if said else ""
    )


def _current_ask(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The item's ``ask`` record. The caller has already cleared a record that a
    later report made stale, by the ledger's own event order
    (``work_ledger_board.ask_is_current``)."""
    record = item.get("ask")
    return record if isinstance(record, Mapping) else None


def _task_word(item: Mapping[str, Any], words: Mapping[str, str]) -> str:
    status = item.get("status")
    return words[status] if status in _TASK_STATUSES else words["started"]


def _join(parts: Sequence[str], language: str) -> str:
    return _SEP.get(language, ", ").join(p for p in parts if p)


def _ask_lines(
    record: Mapping[str, Any] | None,
    words: Mapping[str, str],
    clean: Callable[[object, int], str],
    colon: str,
) -> list[str]:
    """The record's why, each option with what it leads to, and the pick, indented
    under its item; one plain line when no record was written."""
    if not isinstance(record, Mapping):
        return [f"  - {words['not_written']}"]
    lines: list[str] = []
    why = clean(record.get("why"), _MAX_WHY)
    if why:
        lines.append(f"  - {words['why']}{colon}{why}")
    labels: list[str] = []
    for option in record.get("options") or []:
        if not isinstance(option, Mapping):
            continue
        label = clean(option.get("label"), _MAX_LABEL)
        if not label:
            continue
        labels.append(label)
        outcome = clean(option.get("outcome"), _MAX_OUTCOME)
        lines.append(f"  - {label}" + (f"{colon}{outcome}" if outcome else ""))
    pick = clean(record.get("pick"), _MAX_LABEL)
    if pick and pick in labels:
        because = clean(record.get("pick_why"), _MAX_OUTCOME)
        lines.append(f"  - {words['pick']}{colon}{pick}" + (f" — {because}" if because else ""))
    return lines or [f"  - {words['not_written']}"]


def render(
    *,
    language: str,
    goal: str,
    items: Sequence[Mapping[str, Any]],
    awaits: Mapping[str, bool],
    waiting: Sequence[Mapping[str, Any]],
    live: Sequence[Mapping[str, Any]],
    checked_at: str,
    clean: Callable[[object, int], str],
    terminal: Callable[[object], bool],
) -> str:
    """The welcome as markdown: goal, what needs you, being done now, comes after.

    *awaits* is the Board's ``awaits_person`` per open item id; *waiting* the
    thread's other open questions (question cards, change cards and a reply's
    ``[OPTIONS:]`` line); *live* this welcome's PR reads; *checked_at* the HH:MM
    the ledger was read. The ``[OPTIONS:]`` chip row is not drawn here: the caller
    appends main's copy unchanged."""
    words = WORDS.get(language, WORDS["en"])
    colon = _COLON.get(language, ": ")
    left, right = _PARENS.get(language, (" (", ")"))
    by_url = {str(e.get("url")): e for e in live if e.get("url")}
    checked = words["checked"].format(at=checked_at) if checked_at else ""
    open_items = [i for i in items if i.get("item_id") and not terminal(i.get("state"))]

    needs: list[str] = []
    asked_labels: list[set[str]] = []
    doing: list[str] = []
    after: list[str] = []
    for item in open_items:
        record = _current_ask(item)
        # The item's own name; the record's ``what`` names it only where it waits on
        # the owner, since a running or planned item is not that question.
        title = clean(item.get("title") or item.get("item_id"), _MAX_TITLE)
        pr = _pr_part(item, by_url, words, language)
        if awaits.get(str(item["item_id"])):
            title = clean((record or {}).get("what"), _MAX_TITLE) or title
            state = _join([words["waiting"], pr, checked], language)
            needs.append(f"- {title}{left}{state}{right}")
            needs.extend(_ask_lines(record, words, clean, colon))
            if record:
                # Cleaned like the reply's labels below, so the two compare alike.
                asked_labels.append(
                    {clean(o.get("label"), _MAX_LABEL) for o in record.get("options") or [] if isinstance(o, Mapping)}
                )  # fmt: skip
        elif item.get("worker_session_key") or item.get("status"):
            state = _join([_task_word(item, words), pr, checked], language)
            doing.append(f"- {title}{left}{state}{right}")
        else:
            after.append(f"- {title}{left}{words['plan']}{right}")
    for entry in waiting:
        kind = entry.get("kind")
        text = clean(entry.get("text"), _MAX_TITLE)
        labels = [clean(o, _MAX_LABEL) for o in entry.get("options") or []]
        labels = [label for label in labels if label]
        if kind == "options":
            # A reply's own choices that an item's record already explains are not
            # listed twice; any other reply's choices sit in the chip row below.
            if labels and any(set(labels) <= seen for seen in asked_labels):
                continue
            # A reply that was only its choices has no text: never a bare "-",
            # which markdown reads as a heading underline.
            needs.append(f"- {text}" if text else f"- {words['options_below']}")
            if text:
                needs.append(f"  - {words['options_below']}")
            continue
        if kind not in ("question", "card"):
            continue
        tag = words["change_card" if kind == "card" else "question_card"]
        needs.append(f"- {text}{left}{tag}{right}" if text else f"- {tag}")
        needs.extend(f"  - {label}" for label in labels)
        needs.append(f"  - {words['card_not_written']}")

    lines = [f"**{words['goal']}**{colon}{clean(goal, _MAX_GOAL)}", "", f"**{words['needs']}**"]
    lines.extend(needs or [words["nothing"]])
    if doing:
        lines += ["", f"**{words['doing']}**", *doing]
    if after:
        lines += ["", f"**{words['next']}**", *after]
    return "\n".join(lines)
