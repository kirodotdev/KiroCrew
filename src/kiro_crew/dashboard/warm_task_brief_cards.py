"""One card per pull request or task, built by the gateway from the fact
table (RFC section 3.10).

The gateway owns each card: its subject, title, PR pill, tags and the
"replaced by the HH:MM check" line come from facts, in the brief's language.
The model adds at most one sentence per card (``said``) and one ask; a
sentence that contradicts a fact on its card is dropped
(``warm_task_brief_claims``), and an ask's options come only from that
subject's ask fact (the conductor's goal-board ``asks`` entry, or a waiting
question or change card), so each option lands on the card it belongs to.
The headline must cite a bad fact when one exists, else the gateway's fixed
headline is used.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from kiro_crew.dashboard import warm_task_brief_claims as claims
from kiro_crew.dashboard import warm_task_brief_facts as facts_mod
from kiro_crew.dashboard import warm_task_brief_registry as registry
from kiro_crew.dashboard import warm_task_brief_wake as wake

# The most cards one welcome draws; the rest are counted.
MAX_CARDS = 8
_MAX_TITLE_CHARS = 120
_MAX_TEXT_CHARS = 200
_MAX_CONSEQUENCE_CHARS = 160
_MAX_OPTIONS = 6

_WORD_KEYS = (
    "conflicts", "failed", "running", "review_required", "changes_requested", "approved",
    "merged", "closed", "draft", "behind", "merge_blocked", "not_checked", "never_checked",
    "waits", "task_blocked", "task_question", "task_progress", "task_done", "task_accepted",
    "task_rejected", "task_abandoned", "chat_running", "chat_idle", "chat_interrupted",
    "chat_closed", "approvals", "file_missing", "file_ok", "worker_says", "replaced",
    "checked_at", "prs", "tasks", "unread", "more", "h_waits", "h_conflicts", "h_failed",
    "h_approvals", "h_changes", "h_closed", "h_none", "checks_unread",
)  # fmt: skip
# The gateway's own words, one "|"-separated row per language in _WORD_KEYS
# order, picked by the same script rule as the brief's fixed lines.
_WORD_ROWS = {
    "en": "has conflicts|{n} checks failed|{n} checks running|review required|changes requested"
    "|approved|merged|closed|draft|behind its base|merge blocked|not checked since {at}"
    "|never checked|waits on you|task · blocked|task · has a question|task · ongoing"
    "|task · reported done|task · accepted|task · rejected|task · abandoned|chat · running"
    "|chat · idle|chat · interrupted|chat · closed|{n} approvals waiting|file missing|file there"
    "|Worker says:|Older report replaced by the {at} check.|✓ Checked at {at}|PRs on GitHub: {n}"
    "|Tasks: {n}|{pr} not checked|+{n} more|Waiting on you: {n}."
    "|PRs with conflicts: {n}.|PRs with failed checks: {n}.|Approvals waiting in worker chats: {n}.|Changes requested on PRs: {n}.|Worker chats closed on open tasks: {n}.|Here is where things stand.|checks not fully read",
    "zh": "有冲突|{n} 项检查失败|{n} 项检查运行中|待审查|需要修改|已批准|已合并|已关闭|草稿"
    "|落后于基线|合并受阻|自 {at} 起未核实|从未核实|等你处理|任务 · 卡住了|任务 · 有问题待答"
    "|任务 · 进行中|任务 · 报告已完成|任务 · 已接受|任务 · 已拒绝|任务 · 已放弃|对话 · 运行中"
    "|对话 · 空闲|对话 · 中断|对话 · 已关闭|{n} 个批准待处理|文件不存在|文件存在|执行者说："
    "|较早的汇报已被 {at} 的核实取代。|✓ 核实于 {at}|GitHub · {n} 个 PR|任务列表 · {n} 项"
    "|{pr} 未核实|还有 {n} 项|{n} 件事等你处理。|{n} 个 PR 有冲突。|{n} 个 PR 有检查失败。"
    "|执行者对话里有 {n} 个批准在等你。|{n} 个 PR 需要修改。|{n} 个未完成任务的执行者对话已关闭。|这是现在的情况。|检查结果未读全",
    "ja": "競合あり|{n} 件のチェック失敗|{n} 件のチェック実行中|レビュー待ち|修正依頼あり"
    "|承認済み|マージ済み|クローズ済み|下書き|ベースより遅れ|マージ不可|{at} から未確認|未確認"
    "|あなた待ち|タスク · 止まっています|タスク · 質問あり|タスク · 進行中|タスク · 完了報告"
    "|タスク · 受理|タスク · 却下|タスク · 中止|チャット · 実行中|チャット · 待機中"
    "|チャット · 中断|チャット · 終了|{n} 件の承認待ち|ファイルなし|ファイルあり|担当者の報告:"
    "|古い報告は {at} の確認で置き換えました。|✓ {at} に確認|GitHub · PR {n} 件"
    "|タスク一覧 · {n} 件|{pr} は未確認|ほか {n} 件|{n} 件があなた待ちです。"
    "|{n} 件の PR に競合があります。|{n} 件の PR でチェックが失敗しています。"
    "|担当チャットで {n} 件の承認が待っています。|{n} 件の PR に修正依頼があります。|未完了タスクの担当チャット {n} 件が終了しています。|現在の状況です。|チェック結果を読み切れていません",
    "ko": "충돌 있음|검사 {n}개 실패|검사 {n}개 실행 중|리뷰 필요|변경 요청됨|승인됨|병합됨"
    "|닫힘|초안|기준보다 뒤처짐|병합 막힘|{at} 이후 확인 안 됨|확인한 적 없음|답변 대기"
    "|작업 · 막힘|작업 · 질문 있음|작업 · 진행 중|작업 · 완료 보고|작업 · 수락됨|작업 · 거절됨"
    "|작업 · 중단됨|대화 · 실행 중|대화 · 대기|대화 · 중단됨|대화 · 닫힘|승인 {n}개 대기"
    "|파일 없음|파일 있음|작업자 보고:|이전 보고는 {at} 확인으로 바뀌었어요.|✓ {at}에 확인함"
    "|GitHub · PR {n}개|작업 목록 · {n}개|{pr} 확인 안 됨|{n}개 더|{n}개가 답변을 기다려요."
    "|PR {n}개에 충돌이 있어요.|PR {n}개의 검사가 실패했어요.|작업자 대화에서 승인 {n}개가 기다려요.|PR {n}개에 변경 요청이 있어요.|끝나지 않은 작업의 작업자 대화 {n}개가 닫혔어요.|지금 상황이에요.|검사 결과를 다 읽지 못함",
    "ru": "есть конфликты|проверок не прошло: {n}|проверок идёт: {n}|нужно ревью"
    "|просят изменения|одобрено|влито|закрыто|черновик|отстаёт от базы|слияние заблокировано"
    "|не проверено с {at}|ни разу не проверено|ждёт вас|задача · застряла|задача · есть вопрос"
    "|задача · в работе|задача · сообщила о готовности|задача · принята|задача · отклонена"
    "|задача · брошена|чат · работает|чат · простаивает|чат · прерван|чат · закрыт"
    "|ждут одобрения: {n}|файла нет|файл есть|Исполнитель пишет:"
    "|Старый отчёт заменён проверкой в {at}.|✓ Проверено в {at}|GitHub · PR: {n}"
    "|список задач · {n}|{pr} не проверен|ещё {n}|Ждут вас: {n}.|PR с конфликтами: {n}."
    "|PR с упавшими проверками: {n}.|Одобрений ждут в чатах исполнителей: {n}.|PR с запрошенными изменениями: {n}.|Закрытых чатов исполнителей у открытых задач: {n}.|Вот как обстоят дела.|проверки прочитаны не полностью",
    "hi": "टकराव है|{n} जाँच विफल|{n} जाँच चल रही|समीक्षा बाकी|बदलाव माँगे गए|स्वीकृत"
    "|मर्ज हो गया|बंद|मसौदा|आधार से पीछे|मर्ज रुका|{at} से नहीं जाँचा|कभी नहीं जाँचा"
    "|आपका इंतज़ार|काम · अटका है|काम · सवाल है|काम · चल रहा|काम · पूरा बताया|काम · स्वीकार"
    "|काम · अस्वीकार|काम · छोड़ा गया|बातचीत · चल रही|बातचीत · खाली|बातचीत · रुकी|बातचीत · बंद"
    "|{n} मंज़ूरी बाकी|फ़ाइल नहीं है|फ़ाइल है|काम करने वाला कहता है:"
    "|पुरानी रिपोर्ट को {at} की जाँच ने बदल दिया।|✓ {at} पर जाँचा|GitHub · {n} PR"
    "|काम की सूची · {n}|{pr} नहीं जाँचा|{n} और|{n} चीज़ें आपका इंतज़ार कर रही हैं।"
    "|{n} PR में टकराव है।|{n} PR की जाँच विफल है।|काम करने वालों की बातचीत में {n} मंज़ूरी बाकी हैं।|{n} PR में बदलाव माँगे गए हैं।|अधूरे कामों की {n} बातचीत बंद है।|अभी की स्थिति यह है।|जाँच पूरी नहीं पढ़ी",
    "bn": "দ্বন্দ্ব আছে|{n}টি যাচাই ব্যর্থ|{n}টি যাচাই চলছে|রিভিউ বাকি|পরিবর্তন চাওয়া হয়েছে"
    "|অনুমোদিত|মার্জ হয়েছে|বন্ধ|খসড়া|ভিত্তির পেছনে|মার্জ আটকে আছে|{at} থেকে যাচাই হয়নি"
    "|কখনো যাচাই হয়নি|আপনার অপেক্ষায়|কাজ · আটকে আছে|কাজ · প্রশ্ন আছে|কাজ · চলছে"
    "|কাজ · শেষ বলেছে|কাজ · গৃহীত|কাজ · বাতিল|কাজ · ছেড়ে দেওয়া|কথোপকথন · চলছে"
    "|কথোপকথন · অলস|কথোপকথন · থেমে গেছে|কথোপকথন · বন্ধ|{n}টি অনুমোদন বাকি|ফাইল নেই"
    "|ফাইল আছে|কর্মী বলছে:|পুরনো রিপোর্ট {at}-এর যাচাইয়ে বদলে গেছে।|✓ {at}-এ যাচাই করা"
    "|GitHub · {n}টি PR|কাজের তালিকা · {n}টি|{pr} যাচাই হয়নি|আরও {n}টি"
    "|{n}টি জিনিস আপনার অপেক্ষায়।|{n}টি PR-এ দ্বন্দ্ব আছে।|{n}টি PR-এর যাচাই ব্যর্থ।"
    "|কর্মীদের কথোপকথনে {n}টি অনুমোদন বাকি।|{n}টি PR-এ পরিবর্তন চাওয়া হয়েছে।|অসমাপ্ত কাজের {n}টি কথোপকথন বন্ধ।|এখনকার অবস্থা এই।|যাচাই পুরো পড়া হয়নি",
}
WORDS = {
    language: dict(zip(_WORD_KEYS, row.split("|"), strict=True))
    for language, row in _WORD_ROWS.items()
}

Clean = Callable[[object, int], str]


def _tag(
    words: Mapping[str, str], key: str, fact: Mapping[str, Any], bad: bool = False, **kw: Any
) -> dict:
    return {"text": words[key].format(**kw), "bad": bad, "fact": fact["id"]}


def tags(card_facts: Sequence[Mapping[str, Any]], language: str) -> list[dict[str, Any]]:
    """The state of one card's subject as the gateway's own words, from its
    facts' values; ``bad`` marks the ones the owner must see."""
    words = WORDS[language]
    out: list[dict[str, Any]] = []
    for fact in card_facts:
        values, kind = fact.get("values") or {}, fact.get("kind")
        if kind == "pr":
            since = values.get("not_checked_since")
            if since:
                out.append(
                    _tag(
                        words,
                        "never_checked" if since == "never" else "not_checked",
                        fact,
                        at=since,
                    )
                )
                continue
            state = values.get("state")
            if state in ("merged", "closed", "draft"):
                out.append(_tag(words, str(state), fact))
            merge = {"conflicting": "conflicts", "blocked": "merge_blocked", "behind": "behind"}
            if values.get("mergeable") in merge:
                key = merge[values["mergeable"]]
                out.append(_tag(words, key, fact, bad=key == "conflicts"))
            checks = values.get("checks")
            if isinstance(checks, str) and checks:
                # The read did not finish: never shown as all clear.
                out.append(_tag(words, "checks_unread", fact))
            if isinstance(checks, Mapping):
                if checks.get("failed"):
                    out.append(_tag(words, "failed", fact, bad=True, n=checks["failed"]))
                if checks.get("pending"):
                    out.append(_tag(words, "running", fact, n=checks["pending"]))
            review = values.get("review")
            if review in ("review_required", "changes_requested", "approved"):
                out.append(_tag(words, str(review), fact, bad=review == "changes_requested"))
        elif kind == "task":
            state = values.get("state")
            key = f"task_{state}" if state in ("accepted", "rejected", "abandoned") else (
                f"task_{values.get('status')}" if values.get("status") in ("blocked", "question", "progress", "done") else ""
            )  # fmt: skip
            if key:
                out.append(_tag(words, key, fact))
        elif kind == "waits_on_you" and values.get("waits"):
            out.append(_tag(words, "waits", fact))
        elif kind == "chat" and values.get("state") in ("running", "idle", "interrupted", "closed"):
            # The fact's own verdict: a closed chat after a done report is a finish.
            out.append(_tag(words, f"chat_{values['state']}", fact, bad=bool(fact.get("bad"))))
        elif kind == "approval" and values.get("pending"):
            out.append(_tag(words, "approvals", fact, bad=True, n=values["pending"]))
        elif kind == "file" and "exists" in values:  # a wake check carries no stat
            out.append(_tag(words, "file_ok" if values.get("exists") else "file_missing", fact))
    return out


def raw_cards(value: object, clean: Clean) -> tuple[dict[str, Any], ...]:
    """The model's ``cards``, text cleaned; not yet checked against facts."""
    out = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("subject"), str):
            continue
        said: Mapping[str, Any] = raw["said"] if isinstance(raw.get("said"), Mapping) else {}
        ask: Mapping[str, Any] = raw["ask"] if isinstance(raw.get("ask"), Mapping) else {}
        ids = said.get("facts")
        given = ask.get("options")
        out.append(
            {
                "subject": raw["subject"].strip(),
                "said": {
                    "text": clean(said.get("text"), _MAX_TEXT_CHARS),
                    "kind": said["kind"] if said.get("kind") in facts_mod.LINE_KINDS else "none",
                    "facts": [str(i) for i in ids] if isinstance(ids, list) else [],
                },
                "ask": {
                    "what": clean(ask.get("what"), _MAX_TEXT_CHARS),
                    "why": clean(ask.get("why"), _MAX_TEXT_CHARS),
                    "options": [
                        {
                            "label": str(o.get("label") or "").strip(),
                            "consequence": clean(o.get("consequence"), _MAX_CONSEQUENCE_CHARS),
                        }
                        for o in (given if isinstance(given, list) else [])
                        if isinstance(o, Mapping)
                    ],
                    "recommended": str(ask.get("recommended") or "").strip(),
                    "recommended_why": clean(ask.get("recommended_why"), _MAX_TEXT_CHARS),
                },
            }
        )
    return tuple(out[: MAX_CARDS * 2])


def _ask(
    model: Mapping[str, Any] | None, labels: list[str], facts: list, clean: Clean
) -> dict | None:
    """One card's ask: the source *labels*, with the model's consequences and
    recommendation only when it copied them exactly, in order (section 3.9.3),
    and its words only when they contradict no fact on the card."""
    shown = [clean(label, _MAX_TEXT_CHARS) for label in labels[:_MAX_OPTIONS]]
    model = model or {}
    what, why = str(model.get("what") or ""), str(model.get("why") or "")
    written = [(o["label"], o["consequence"]) for o in model.get("options") or []]
    # Every model word in the ask, consequences and the pick's reason included.
    said = [what, why, str(model.get("recommended_why") or ""), *(c for _, c in written)]
    if claims.contradicts("\n".join(said), facts):
        what, why, written = "", "", []
        model = {**model, "recommended_why": ""}
    copied = bool(labels) and [label for label, _ in written] == [label.strip() for label in labels]
    if not (what or shown):
        return None
    pick = next(
        (
            s
            for s, label in zip(shown, labels)
            if copied and label.strip() == model.get("recommended")
        ),
        "",
    )
    return {
        "what": what,
        "why": why,
        "options": [
            {"label": s, "consequence": written[i][1] if copied else ""}
            for i, s in enumerate(shown)
        ],
        "recommended": pick,
        "recommended_why": str(model.get("recommended_why") or "") if pick else "",
    }


def _said(model: Mapping[str, Any] | None, facts: list, hint: bool, words: Mapping) -> dict | None:
    """The model's sentence for a card: it cites only facts on the card and
    contradicts none of them. Citing none, it is the worker's report and says so."""
    said = (model or {}).get("said") or {}
    text, ids = said.get("text") or "", list(dict.fromkeys(said.get("facts") or []))
    kind = said.get("kind") or "none"
    mine = {f["id"]: f for f in facts}
    # The line rule (``typed_lines``): a typed sentence cites a fact of its
    # kind, and one citing a wake check names that check's kind.
    typed = facts_mod.typed_lines([{"text": text, "kind": kind, "facts": ids}], facts)
    woke = {mine[i]["kind"] for i in ids if i in mine and mine[i].get("source") == "wake"}
    if not text or not typed or (woke and woke != {kind}) or claims.contradicts(text, facts):
        return None
    if not ids:
        return {"text": text, "facts": [], "by": words["worker_says"]} if hint else None
    return {"text": text, "facts": ids}


def build(
    table: Sequence[Mapping[str, Any]],
    model_cards: Sequence[Mapping[str, Any]],
    language: str,
    clean: Clean,
) -> tuple[list[dict[str, Any]], int]:
    """The cards and how many past ``MAX_CARDS`` were left out. One card per
    work item that needs attention or that the model wrote about, per pull
    request no item holds, and per waiting question or change card."""
    words = WORDS[language]
    prs = {f["subject"]: f for f in table if f.get("kind") == "pr"}
    # How many tasks hold each PR: PR-keyed model words go to a task only
    # when it is the PR's one holder, else they stay on the PR's own card.
    holders: dict[str, int] = {}
    for fact in table:
        held_pr = (fact.get("values") or {}).get("pr") if fact.get("kind") == "task" else ""
        if held_pr:
            holders[held_pr] = holders.get(held_pr, 0) + 1
    asks = {f["subject"]: f for f in table if f.get("kind") == "ask"}
    written: dict[str, Mapping[str, Any]] = {}
    for card in model_cards:
        written.setdefault(card["subject"], card)  # one card, one ask, per subject
    replaced = registry.superseded(table)
    # A wake check names no subject: any card's sentence may cite one.
    checks = [f for f in table if f.get("source") == "wake"]
    held: set[str] = set()
    cards: list[tuple[bool, dict[str, Any]]] = []
    for task in (f for f in table if f.get("kind") == "task"):
        item_id = str(task.get("item_id"))
        subject = str((task.get("values") or {}).get("pr", ""))
        pr = prs.get(subject)
        # The model may key the item's card by its PR: both name one card.
        alias = pr is not None and holders.get(subject) == 1
        model = written.get(item_id) or (written.get(subject) if alias else None)
        mine = [f for f in table if f.get("item_id") == item_id and f["kind"] not in ("ask",)]
        facts = ([pr] if pr else []) + mine
        waits = any(f["kind"] == "waits_on_you" and f["values"].get("waits") for f in mine)
        urgent = waits or any(f.get("bad") for f in facts)
        values = task.get("values") or {}
        # A closed item asks nothing: it shows only when urgent or written about.
        closed = values.get("state") in ("accepted", "rejected", "abandoned")
        asking = values.get("status") in ("question", "blocked") and not closed
        if not (urgent or model or item_id in asks or asking):
            continue
        if alias:
            held.add(subject)  # only once its one holder's card is drawn
        card = _card(item_id, clean(task.get("label"), _MAX_TITLE_CHARS), facts, pr, language)
        hint = bool((task.get("values") or {}).get("reported"))
        # A closed task asks nothing, whatever the model wrote for it.
        model = {**model, "ask": {}} if closed and model else model
        _fill(
            card, model, facts + checks, None if closed else asks.get(item_id), hint, words, clean
        )
        if item_id in replaced:
            card["replaced"] = words["replaced"].format(at=replaced[item_id].get("read_at", ""))
        cards.append((urgent, card))
    for subject, pr in prs.items():
        if subject in held:
            continue
        card = _card(subject, clean(pr.get("label"), _MAX_TITLE_CHARS), [pr], pr, language)
        _fill(card, written.get(subject), [pr, *checks], None, False, words, clean)
        cards.append((bool(pr.get("bad")), card))
    for subject, ask in asks.items():
        if ask.get("source") != "waiting":
            continue
        card = _card(subject, clean(ask.get("label"), _MAX_TITLE_CHARS), [], None, language)
        _fill(card, written.get(subject), checks, ask, False, words, clean)
        cards.append((True, card))
    ordered = [card for urgent, card in cards if urgent] + [
        card for urgent, card in cards if not urgent
    ]
    return ordered[:MAX_CARDS], max(0, len(ordered) - MAX_CARDS)


def _card(
    subject: str, title: str, facts: list, pr: Mapping | None, language: str
) -> dict[str, Any]:
    card: dict[str, Any] = {"subject": subject, "title": title, "facts": [f["id"] for f in facts]}
    if pr is not None:
        number = str(pr.get("pr") or "").lstrip("#")
        checked = "not_checked_since" not in (pr.get("values") or {})
        card["pr"] = {
            "label": f"#{number}",
            "url": f"https://github.com/{pr.get('repo')}/pull/{number}",
            "checked": checked,
        }
    card["tags"] = tags(facts, language)
    return card


def _fill(card: dict, model: Mapping | None, facts: list, ask: Mapping | None,
          hint: bool, words: Mapping, clean: Clean) -> None:  # fmt: skip
    said = _said(model, facts, hint, words)
    if said:
        card["said"] = said
    labels = [str(o) for o in (ask.get("values") or {}).get("options") or []] if ask else []
    made = _ask((model or {}).get("ask") if model else None, labels, facts, clean)
    if made:
        card["ask"] = made


def fixed_headline(table: Sequence[Mapping[str, Any]], language: str) -> str:
    """The gateway's own headline from the facts: what waits on the owner and
    which pull requests are in trouble. "" when nothing is."""
    words = WORDS[language]
    counts = {
        "h_waits": sum(1 for f in table if f["kind"] == "waits_on_you" and f["values"].get("waits")),
        "h_conflicts": sum(1 for f in table if f["kind"] == "pr" and f["values"].get("mergeable") == "conflicting"),
        "h_failed": sum(
            1 for f in table
            if f["kind"] == "pr" and isinstance(f["values"].get("checks"), Mapping) and f["values"]["checks"].get("failed")
        ),
        "h_approvals": sum(f["values"].get("pending") or 0 for f in table if f["kind"] == "approval"),
        "h_changes": sum(
            1 for f in table if f["kind"] == "pr" and f["values"].get("review") == "changes_requested"
        ),
        "h_closed": sum(1 for f in table if f["kind"] == "chat" and f.get("bad")),
    }  # fmt: skip
    # Chinese and Japanese sentences run on with no space between them.
    joiner = "" if language in ("zh", "ja") else " "
    return joiner.join(words[key].format(n=n) for key, n in counts.items() if n)


def headline(
    text: str, cited: Sequence[str], table: Sequence[Mapping[str, Any]], language: str
) -> str:
    """*text* when it contradicts no fact in the table and, when a bad fact
    exists, cites one; else the gateway's fixed headline."""
    by_id = {f["id"]: f for f in table}
    facts = [by_id[i] for i in cited if i in by_id]
    bad = [f for f in table if f.get("bad")]
    # A headline speaks for the whole welcome: no fact may say its opposite.
    if claims.contradicts(text, table) or (bad and not any(f.get("bad") for f in facts)):
        # Never the rejected text: every bad kind is counted, else a plain line.
        return fixed_headline(table, language) or WORDS[language]["h_none"]
    return text


def check_line(table: Sequence[Mapping[str, Any]], language: str) -> tuple[str, ...]:
    """The one line naming what was checked and when; it replaces the old
    per-fact footnotes."""
    words = WORDS[language]
    now = next((f["values"]["now"] for f in table if f["kind"] == "time"), "")
    read = [f for f in table if f["kind"] == "pr" and "not_checked_since" not in f["values"]]
    unread = [f for f in table if f["kind"] == "pr" and "not_checked_since" in f["values"]]
    tasks = sum(1 for f in table if f["kind"] == "task")
    parts = [words["checked_at"].format(at=now)] if now else []
    parts += [words["prs"].format(n=len(read))] if read else []
    parts += [words["tasks"].format(n=tasks)] if tasks else []
    parts += [words["unread"].format(pr=str(f.get("pr") or "")) for f in unread]
    dropped = next((f["values"].get("dropped") for f in table if f["kind"] == "time"), 0)
    parts += [words["more"].format(n=dropped)] if dropped else []
    return tuple(parts)


def owed(cards: Sequence[Mapping[str, Any]], table: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the owner owes something, from the checked facts first: a wait
    on them, a pending approval or a waiting question card, asked or not."""
    return any(c.get("ask") for c in cards) or any(
        (f["kind"] == "waits_on_you" and f["values"].get("waits"))
        or (f["kind"] == "approval" and f["values"].get("pending"))
        or (f["kind"] == "ask" and f.get("source") == "waiting")
        for f in table
    )


def texts(cards: Sequence[Mapping[str, Any]]) -> list[str]:
    """The model-written strings the cards carry, and the option labels, for
    the grounding scans."""
    out: list[str] = []
    for card in cards:
        out.append(str((card.get("said") or {}).get("text") or ""))
        ask = card.get("ask") or {}
        out += [str(ask.get(k) or "") for k in ("what", "why", "recommended", "recommended_why")]
        for option in ask.get("options") or []:
            out += [str(option.get("label") or ""), str(option.get("consequence") or "")]
    return [t for t in out if t]


def gateway_only(cards: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """*cards* with every model word dropped: tags, titles and source option
    labels stay (section 3.9.6)."""
    out = []
    for card in cards:
        kept = {k: v for k, v in card.items() if k not in ("said", "ask")}
        labels = [o["label"] for o in (card.get("ask") or {}).get("options") or []]
        if labels:
            kept["ask"] = {"what": "", "why": "", "options": [{"label": lb, "consequence": ""} for lb in labels],
                           "recommended": "", "recommended_why": ""}  # fmt: skip
        out.append(kept)
    return tuple(out)


def cleaned(table: Sequence[Mapping[str, Any]], clean: Clean) -> tuple[dict[str, Any], ...]:
    """*table* with every label and text value cleaned like a model field:
    titles, reports' paths and offered answers are agent-written text."""

    def one(value: Any) -> Any:
        if isinstance(value, str):
            return clean(value, _MAX_TEXT_CHARS)
        return [one(v) for v in value] if isinstance(value, list) else value

    return tuple(
        {**fact, "label": one(fact.get("label") or ""),
         "values": {k: one(v) for k, v in (fact.get("values") or {}).items()}}
        for fact in table
    )  # fmt: skip


def with_wake_checks(
    brief: Any, text: str, calls: wake.Calls | None, table: Sequence[Mapping[str, Any]]
) -> tuple[Any, list[dict[str, Any]]]:
    """``wake.with_checks`` over the cards: each card's ``said`` ids stand in
    for a line's, so a proven check (``cN``) takes its ``fN`` there too."""
    said = tuple({**c["said"], "facts": list(c["said"]["facts"])} for c in brief.cards)
    lined, checked = wake.with_checks(
        replace(brief, lines=said), text, calls, [dict(f) for f in table]
    )
    cards = tuple(
        {**card, "said": {**card["said"], "facts": line["facts"]}}
        for card, line in zip(brief.cards, lined.lines)
    )
    return replace(brief, cards=cards), checked


def apply(brief: Any, table: Sequence[Mapping[str, Any]], language: str, clean: Clean) -> Any:
    """*brief* as a v3 card brief: gateway cards with the model's checked
    words, the headline rule, the check line and the status the facts allow."""
    built, more = build(table, brief.cards, language, clean)
    status = brief.status
    if owed(built, table) and status in ("on_track", "done"):
        status = "needs_you"
    # The older free fields are not drawn on a card brief, so they are not kept.
    legacy: dict[str, Any] = dict.fromkeys(("asked", "remaining"), "")
    legacy |= dict.fromkeys(("done", "needs_you", "next", "extra", "asks", "lines"), ())
    return replace(
        brief,
        **legacy,
        version=3,
        language=language,
        status=status,
        headline=headline(brief.headline, brief.headline_facts, table, language),
        cards=tuple(built),
        more=more,
        check=check_line(table, language),
    )


def text(brief: Any, status_label: str) -> str:
    """The cards as plain text, for every reader of ``content``."""
    words = WORDS.get(brief.language, WORDS["en"])
    lines = [f"{status_label}: {brief.headline}"]
    if brief.check:
        lines.append(" · ".join(brief.check))
    for card in brief.cards:
        pill = f"{card['pr']['label']} " if card.get("pr") else ""
        tagged = " · ".join(t["text"] for t in card.get("tags") or [])
        lines.append(f"- {pill}{card['title']}" + (f" [{tagged}]" if tagged else ""))
        said = card.get("said")
        if said:
            by = str(said.get("by") or "")
            # A full-width colon carries its own space.
            by += " " if by and not by.endswith("：") else ""
            lines.append(f"  {by}{said['text']}")
        if card.get("replaced"):
            lines.append(f"  {card['replaced']}")
        ask = card.get("ask") or {}
        if ask.get("what"):
            lines.append(f"  → {ask['what']}" + (f" ({ask['why']})" if ask.get("why") else ""))
        for option in ask.get("options") or []:
            star = "★ " if option["label"] == ask.get("recommended") else ""
            tail = f": {option['consequence']}" if option.get("consequence") else ""
            lines.append(f"    - {star}{option['label']}{tail}")
    if brief.more:
        lines.append(words["more"].format(n=brief.more))
    return "\n".join(lines)
