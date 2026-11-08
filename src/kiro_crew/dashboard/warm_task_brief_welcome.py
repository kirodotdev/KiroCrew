"""The warm welcome, rendered by the gateway (RFC ``rfc-delayed-task-brief.md`` 3.10.4).

Every state, count, tag, time and "since you left" row here comes from a read the
gateway itself made, each with its read time: the work ledger, the "waits on you"
test the Board uses (``awaits_person``), the pull-request live read and the
``objects`` it records. The conductor's judgement (why, the options with what each
does, its pick) comes from its goal-board ``asks`` record for the item and is shown
as the conductor's words with the time it wrote them. The model states nothing, and
no model text is shown.

Words are the gateway's, in the welcome's one language (:data:`WORDS`). The card
draws the stored strings as plain text.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kiro_crew.dashboard import warm_task_brief_inputs as inputs

MAX_CARDS = 3
MAX_CHANGES = 3
_MAX_TITLE = 200
_MAX_WHY = 400
_MAX_OPTION = 160
_MAX_OUTCOME = 240
_MAX_OPTIONS = 6

_KEYS = (
    "count", "nothing", "said", "pick", "since", "more", "board", "open_board",
    "never", "not_checked", "github", "report", "decision", "ledger", "wake",
    "merged", "closed", "draft", "open", "conflicts", "failed", "pending", "passed",
    "not_read", "changes_requested", "approved", "review_required",
    "accepted", "rejected", "abandoned", "question", "blocked", "progress", "done",
    "question_card", "change_card", "checked",
)  # fmt: skip
_ROWS = {
    "en": "Waiting on you: {n}|Nothing waits on you|Suggested {at}|Recommended"
    "|Since you left|+{n} more on the Board|Board: {w} waiting on you"
    "|Open the Board →|never checked|not checked since {at}|GitHub {at}|worker report {at}"
    "|decision {at}|task list {at}|Wake at {at} checked:|merged|closed|draft|open"
    "|conflicts|{n} failed|{n} running|checks passed|checks not fully read|changes requested"
    "|approved|review required|accepted|rejected|abandoned|has a question|blocked|in progress"
    "|reported done|question card|change card|checked {at}",
    "zh": "{n} 件事等你|没有事等你|建议（{at}）|推荐|你走后|+{n} 项变化，看板上有"
    "|看板：{w} 项等你|打开看板 →|没查过|{at} 后没查过|GitHub {at}|任务汇报 {at}"
    "|决定 {at}|任务表 {at}|唤醒 {at} 查过：|已合并|已关闭|草稿|开着|有冲突|{n} 项失败"
    "|{n} 项在跑|检查全过|检查没读全|要求修改|已批准|等审阅|已完成|被拒|已放弃|有问题|卡住|进行中"
    "|报完成|问题卡片|变更卡片|{at} 核实",
    "ja": "{n} 件があなた待ち|あなた待ちはありません|提案（{at}）|おすすめ|離れていた間"
    "|ほか {n} 件はボードに|ボード：{w} 件あなた待ち|ボードを開く →|未確認"
    "|{at} から未確認|GitHub {at}|作業報告 {at}|判断 {at}|タスク表 {at}"
    "|起動して {at} に確認：|マージ済み|クローズ|ドラフト|オープン|競合あり|{n} 件失敗"
    "|{n} 件実行中|チェック成功|チェック未読|修正依頼|承認済み|レビュー待ち|完了|却下|中止"
    "|質問あり|止まっています|進行中|完了報告|質問カード|変更カード|{at} 確認",
    "ko": "{n}건이 기다려요|기다리는 일이 없어요|제안 ({at})|추천|자리를 비운 사이"
    "|+{n}건은 보드에|보드: {w}건 대기|보드 열기 →|확인 안 함|{at} 이후 확인 안 함"
    "|GitHub {at}|작업 보고 {at}|결정 {at}|작업 목록 {at}|깨어나 {at}에 확인:|병합됨|닫힘"
    "|초안|열림|충돌|{n}개 실패|{n}개 실행 중|검사 통과|검사 다 못 읽음|변경 요청|승인됨|리뷰 필요"
    "|완료|거절|중단|질문 있음|막힘|진행 중|완료 보고|질문 카드|변경 카드|{at} 확인",
    "ru": "Ждут вас: {n}|Ничего не ждёт вас|Предложено в {at}|Рекомендуется"
    "|Пока вас не было|Ещё {n} на доске|Доска: ждут вас {w}|Открыть доску →"
    "|не проверено|не проверено с {at}|GitHub {at}|отчёт исполнителя {at}"
    "|решение {at}|список задач {at}|Проверка в {at}:|слит|закрыт|черновик"
    "|открыт|конфликты|упало {n}|идёт {n}|проверки прошли|проверки прочитаны не все"
    "|просят правки|одобрен|ждёт ревью|принято|отклонено|брошено|есть вопрос|застряло"
    "|в работе|сдано|карточка вопроса|карточка изменения|проверено в {at}",
    "hi": "{n} काम आपका इंतज़ार कर रहे हैं|कुछ भी आपका इंतज़ार नहीं कर रहा|सुझाव ({at})"
    "|सुझाया गया|आपके जाने के बाद|+{n} और बोर्ड पर|बोर्ड: {w} आपके लिए"
    "|बोर्ड खोलें →|जाँचा नहीं|{at} से नहीं जाँचा|GitHub {at}|कार्य रिपोर्ट {at}"
    "|फ़ैसला {at}|कार्य सूची {at}|जागकर {at} पर जाँचा:|मर्ज हुआ|बंद|ड्राफ़्ट"
    "|खुला|टकराव|{n} विफल|{n} चल रहे|जाँच पास|जाँच पूरी नहीं पढ़ी|बदलाव माँगे|मंज़ूर"
    "|समीक्षा बाकी|स्वीकार|अस्वीकार|छोड़ा|सवाल है|अटका|चल रहा|पूरा बताया|सवाल कार्ड|बदलाव कार्ड|{at} पर जाँचा",
    "bn": "{n}টি কাজ আপনার অপেক্ষায়|কিছুই আপনার অপেক্ষায় নেই|পরামর্শ ({at})|প্রস্তাবিত"
    "|আপনি যাওয়ার পর|আরও {n}টি বোর্ডে|বোর্ড: {w}টি আপনার জন্য|বোর্ড খুলুন →"
    "|যাচাই হয়নি|{at} থেকে যাচাই হয়নি|GitHub {at}|কাজের রিপোর্ট {at}|সিদ্ধান্ত {at}"
    "|কাজের তালিকা {at}|জেগে {at}-এ যাচাই:|মার্জ হয়েছে|বন্ধ|খসড়া|খোলা|দ্বন্দ্ব|{n}টি ব্যর্থ"
    "|{n}টি চলছে|যাচাই পাস|যাচাই পুরো পড়া হয়নি|পরিবর্তন চাওয়া|অনুমোদিত|রিভিউ বাকি|গৃহীত"
    "|বাতিল|ছেড়ে দেওয়া|প্রশ্ন আছে|আটকে আছে|চলছে|শেষ জানিয়েছে|প্রশ্ন কার্ড|পরিবর্তন কার্ড|{at}-এ যাচাই",
}
WORDS: dict[str, dict[str, str]] = {
    lang: dict(zip(_KEYS, row.split("|"), strict=True)) for lang, row in _ROWS.items()
}

_TASK_WORDS = {
    "accepted",
    "rejected",
    "abandoned",
    "question",
    "blocked",
    "progress",
    "done",
}
_BAD_TASK = {"rejected", "abandoned", "blocked"}


def _tag(text: str, bad: bool = False) -> dict[str, Any]:
    return {"text": text, "bad": bad}


def pr_tags(entry: Mapping[str, Any] | None, words: Mapping[str, str]) -> list[dict[str, Any]]:
    """A live read as tags, bad ones first. A failed read is one tag saying since when."""
    if entry is None:
        return []
    if entry.get("checked") is not True:
        since = str(entry.get("not_checked_since") or "never")
        text = words["never"] if since == "never" else words["not_checked"].format(at=since)
        return [_tag(text)]
    tags: list[dict[str, Any]] = []
    state = str(entry.get("state") or "").lower()
    if state in ("merged", "closed", "draft", "open"):
        tags.append(_tag(words[state], bad=state == "closed"))
    if str(entry.get("mergeable") or "").lower() in ("conflicting", "dirty"):
        tags.append(_tag(words["conflicts"], bad=True))
    review = str(entry.get("review") or "").lower()
    if review in ("changes_requested", "approved", "review_required"):
        tags.append(_tag(words[review], bad=review == "changes_requested"))
    checks = entry.get("checks")
    if isinstance(checks, Mapping):
        failed, pending = checks.get("failed") or 0, checks.get("pending") or 0
        if failed:
            tags.append(_tag(words["failed"].format(n=failed), bad=True))
        if pending:
            tags.append(_tag(words["pending"].format(n=pending)))
        if not failed and not pending and not checks.get("unknown") and checks.get("passed"):
            tags.append(_tag(words["passed"]))
    elif isinstance(checks, str) and checks:
        tags.append(_tag(words["not_read"]))
    return sorted(tags, key=lambda t: not t["bad"])


def pr_closed(entry: Mapping[str, Any] | None) -> bool:
    """Whether a good read shows the pull request merged or closed."""
    return bool(
        entry
        and entry.get("checked") is True
        and str(entry.get("state") or "").lower() in ("merged", "closed")
    )


def task_tag(item: Mapping[str, Any], words: Mapping[str, str]) -> dict[str, Any] | None:
    state, status = item.get("state"), item.get("status")
    word = state if state in _TASK_WORDS else status if status in _TASK_WORDS else None
    return _tag(words[word], bad=word in _BAD_TASK) if word else None


def _read_line(entry: Mapping[str, Any] | None, words: Mapping[str, str]) -> str:
    if entry is not None and entry.get("checked") is True and entry.get("read_at"):
        return words["github"].format(at=entry["read_at"])
    return ""


def _pr_cell(item: Mapping[str, Any], live: Mapping[str, Mapping[str, Any]]) -> tuple:
    ref = inputs.ledger_ref(item)
    if ref is None:
        return None, None
    entry = live.get(ref.url)
    # Only a PR this welcome read is named, with its link; any other is not named.
    if entry is None:
        return None, None
    return {"label": f"{ref.repo}#{ref.number}", "url": ref.url}, entry


def ask_said(
    record: Mapping[str, Any] | None,
    words: Mapping[str, str],
    clean: Callable[[object, int], str],
) -> dict[str, Any] | None:
    """The conductor's record for one card, as its words with their time."""
    if not record:
        return None
    options = [
        {"label": clean(o.get("label"), _MAX_OPTION), "outcome": clean(o.get("outcome"), _MAX_OUTCOME)}
        for o in record.get("options") or []
        if isinstance(o, Mapping)
    ]  # fmt: skip
    options = [o for o in options if o["label"]][:_MAX_OPTIONS]
    why = clean(record.get("why"), _MAX_WHY)
    pick = clean(record.get("pick"), _MAX_OPTION)
    if pick not in {o["label"] for o in options}:
        pick = ""
    at = inputs.clock(inputs.parse_epoch(record.get("written_at")))
    if not (why or options) or not at:
        return None
    return {
        "by": words["said"].format(at=at),
        "why": why,
        "options": options,
        "pick": pick,
        "pick_why": clean(record.get("pick_why"), _MAX_OUTCOME) if pick else "",
        "pick_label": words["pick"],
    }


def _change_source(item: Mapping[str, Any], words: Mapping[str, str]) -> tuple[float, str]:
    stamps = {
        key: epoch
        for key in ("last_report_at", "decided_at", "closed_at")
        if (epoch := inputs.parse_epoch(item.get(key))) is not None
    }
    if not stamps:
        return 0.0, ""
    newest = max(stamps, key=lambda key: stamps[key])
    word = {
        "last_report_at": "report",
        "decided_at": "decision",
        "closed_at": "ledger",
    }[newest]
    return stamps[newest], words[word].format(at=inputs.clock(stamps[newest]))


def render(
    *,
    language: str,
    items: Sequence[Mapping[str, Any]],
    awaits: Mapping[str, bool],
    waiting: Sequence[Mapping[str, Any]],
    live: Sequence[Mapping[str, Any]],
    asks: Mapping[str, Mapping[str, Any]],
    anchor: float | None,
    terminal: Callable[[object], bool],
    clean: Callable[[object, int], str],
    conductor: str = "",
    checked_at: str = "",
    waiting_total: int = 0,
) -> dict[str, Any]:
    """The welcome as ``meta.brief`` version 4, every word the gateway's. Each item
    card says when its facts were checked (*checked_at*, HH:MM); a card is static
    once posted. *waiting_total* is the owner's whole pending count, of which
    *waiting* is only the capped list shown."""
    words = WORDS.get(language, WORDS["en"])
    by_url = {str(e.get("url")): e for e in live if e.get("url")}
    must: list[dict[str, Any]] = []
    moved: list[tuple[int, float, dict[str, Any]]] = []
    shown: set[str] = set()
    for item in items:
        item_id = str(item.get("item_id") or "")
        if terminal(item.get("state")) or not awaits.get(item_id):
            continue
        pr, entry = _pr_cell(item, by_url)
        tags = pr_tags(entry, words)
        title = clean((asks.get(item_id) or {}).get("what"), _MAX_TITLE) or clean(
            item.get("title") or item_id, _MAX_TITLE
        )
        if pr_closed(entry):
            # A merged or closed PR drops every ask about it: a change, not a must-do.
            # Ordered like every other change: bad facts first, so a merge never
            # pushes a failure out of the three rows shown.
            bad = any(t["bad"] for t in tags)
            moved.append((0 if bad else 1, inputs.parse_epoch(item.get("last_report_at")) or 0.0,
                          {"tags": tags, "text": title, "source": _read_line(entry, words)}))  # fmt: skip
            shown.add(item_id)
            continue
        # The item id lets the client hide the card live once the item stops
        # waiting on the owner (the Board's own ``awaits_person``).
        card: dict[str, Any] = {
            "kind": "item",
            "item_id": item_id,
            "title": title,
            "tags": tags,
        }
        if (tag := task_tag(item, words)) is not None:
            card["tags"] = [*tags, tag]
        if pr:
            card["pr"] = pr
            card["read"] = _read_line(entry, words)
        # A card is static once posted (RFC 3.11.1): it says when it was checked.
        if checked_at:
            card["checked"] = words["checked"].format(at=checked_at)
        if (said := ask_said(asks.get(item_id), words, clean)) is not None:
            card["said"] = said
        must.append(card)
        shown.add(item_id)
    for entry_w in waiting:
        kind = entry_w.get("kind")
        if kind not in ("question", "card", "options"):
            continue
        # An open [OPTIONS:] ask waits on the owner too. Its labels stay on the chip
        # bar alone (RFC 3.10 Q3: a card never takes options from the free line).
        options = (
            []
            if kind == "options"
            else [clean(o, _MAX_OPTION) for o in entry_w.get("options") or []]
        )
        must.append(
            {
                "kind": kind,
                "title": clean(entry_w.get("text"), _MAX_TITLE),
                "tags": [_tag(words["change_card" if kind == "card" else "question_card"])],
                "options": [o for o in options if o][:_MAX_OPTIONS],
            }
        )
    for item in items:
        item_id = str(item.get("item_id") or "")
        if item_id in shown or not inputs.changed_since(item, anchor):
            continue
        pr, entry = _pr_cell(item, by_url)
        tags = pr_tags(entry, words)
        if (tag := task_tag(item, words)) is not None:
            tags = [*tags, tag]
        at, source = _change_source(item, words)
        bad = any(t["bad"] for t in tags)
        text = clean(item.get("title") or item_id, _MAX_TITLE)
        moved.append((0 if bad else 1, at, {"tags": tags, "text": text, "source": source}))
    moved.sort(key=lambda row: (row[0], -row[1]))
    changes = [row for _, _, row in moved]
    open_items = [i for i in items if not terminal(i.get("state"))]
    # The waiting list is capped before it gets here; the count is not.
    count = len(must) + max(0, waiting_total - len(waiting))
    meta: dict[str, Any] = {
        "version": 4,
        "language": language,
        "count": count,
        "count_line": words["count"].format(n=count) if count else words["nothing"],
        "cards": must[:MAX_CARDS],
        "changes_title": words["since"],
        "changes": changes[:MAX_CHANGES],
        "more": words["more"].format(n=len(changes) - MAX_CHANGES) if len(changes) > MAX_CHANGES else "",
    }  # fmt: skip
    if conductor and (items or asks):
        waits = sum(1 for i in open_items if awaits.get(str(i.get("item_id"))))
        meta["board"] = {
            "text": words["board"].format(w=waits),
            "link": words["open_board"],
            "conductor": conductor,
        }
    return meta


def text(meta: Mapping[str, Any], framing: str = "") -> str:
    """The welcome as plain text, for every reader of ``content`` rather than the card."""
    lines = [meta["count_line"] + (f" · {meta['cards'][0]['title']}" if meta.get("cards") else "")]
    if framing:
        lines.append(framing)
    for card in meta.get("cards") or []:
        tags = " · ".join(t["text"] for t in card.get("tags") or [])
        head = " ".join(x for x in ((card.get("pr") or {}).get("label", ""), card["title"]) if x)
        lines.append(
            f"- {head}"
            + (f" [{tags}]" if tags else "")
            + (f" ({card['read']})" if card.get("read") else "")
            + (f" ({card['checked']})" if card.get("checked") else "")
        )
        said = card.get("said")
        if said:
            if said["why"]:
                lines.append(f"  {said['by']} {said['why']}")
            for option in said["options"]:
                star = " *" if option["label"] == said["pick"] else ""
                lines.append(
                    f"  · {option['label']}{star}"
                    + (f": {option['outcome']}" if option["outcome"] else "")
                )
            if said["pick"]:
                lines.append(
                    f"  {said['pick_label']}: {said['pick']}"
                    + (f" ({said['pick_why']})" if said["pick_why"] else "")
                )
        for option in card.get("options") or []:
            lines.append(f"  · {option}")
    if meta.get("changes"):
        lines.append(f"{meta['changes_title']}:")
        for row in meta["changes"]:
            tags = " · ".join(t["text"] for t in row["tags"])
            lines.append(
                f"- {row['text']}"
                + (f" [{tags}]" if tags else "")
                + (f" ({row['source']})" if row["source"] else "")
            )
        if meta.get("more"):
            lines.append(meta["more"])
    for check in meta.get("checks") or []:
        lines.append(check)
    if meta.get("board"):
        lines.append(meta["board"]["text"])
    return "\n".join(lines)  # fmt: skip
