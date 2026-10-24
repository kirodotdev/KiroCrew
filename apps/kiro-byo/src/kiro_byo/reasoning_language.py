"""Presentation-language policy for provider-visible reasoning.

Kiro BYO can stream provider-authentic thinking/reasoning summaries into Crew's
folded Thinking surface.  Different providers/models otherwise choose different
languages by default, so this module supplies one provider-neutral presentation
hint per turn.

This is intentionally *not* a request to reveal hidden chain-of-thought.  It only
controls the language of reasoning/thinking that an upstream already exposes,
and it keeps technical identifiers in their natural/original spelling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

_DEFAULT = "auto"

# Manual settings use ordinary BCP-47-ish values.  The UI exposes only the most
# common choices, while config remains forward-compatible with other locales.
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_ALIASES = {
    "auto": "auto",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "cn": "zh-CN",
    "chinese": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "tw": "zh-TW",
    "traditional-chinese": "zh-TW",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "english": "en",
    "ja": "ja",
    "ja-jp": "ja",
    "jp": "ja",
    "japanese": "ja",
    "ko": "ko",
    "ko-kr": "ko",
    "korean": "ko",
}

_LANGUAGE_NAMES = {
    "zh-CN": "Simplified Chinese",
    "zh-TW": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    "ar": "Arabic",
    "he": "Hebrew",
    "hi": "Hindi",
    "th": "Thai",
    "el": "Greek",
}

# Small lexical hints are only used after script detection.  They are not a
# translation dictionary and never touch user/model text.
_LATIN_HINTS: dict[str, frozenset[str]] = {
    "en": frozenset("the a an and or but is are was were be to of in on for with this that it you we i my your can could should would why how what when from as not do does please help check use using".split()),
    "es": frozenset("el la los las un una y o pero es son de del en para con este esta esto que por como puedo puedes ayuda revisar usar".split()),
    "fr": frozenset("le la les un une et ou mais est sont de des du en pour avec ce cette que pourquoi comment peux peut aide vérifier utiliser".split()),
    "de": frozenset("der die das ein eine und oder aber ist sind von in für mit diese dieser warum wie kann kannst bitte hilfe prüfen verwenden".split()),
    "pt": frozenset("o a os as um uma e ou mas é são de do da em para com este esta que por como posso pode ajuda verificar usar".split()),
    "it": frozenset("il lo la i gli le un una e o ma è sono di del in per con questo questa che perché come posso puoi aiuto verificare usare".split()),
}


# Simplified and Traditional share many Han characters.  Only distinctive
# variants are evidence.  The previous Traditional list accidentally contained
# shared characters such as "先", "路" and "案", so Simplified texts about
# "路线优先级" could be mislabelled as Traditional.
_TRADITIONAL_HINTS = frozenset(
    "這為與說會來時裡後開關調節顯示語還專業詞彙網軟體處應該問題實際體驗選擇個們從讓將對於構築優級別據線態現發學習寫讀檔"
)
_SIMPLIFIED_HINTS = frozenset(
    "这为与说会来时里后开关调节显示语还专业词汇网软体处应该问题实际体验选择个们从让将对于构筑优级别据线态现发学习写读档"
)
# Common conversation vocabulary missed by the original small heuristic.
_TRADITIONAL_HINTS |= frozenset("請檢幫設錯訊選項編輯預覽測試頁啟國獲簡陳點擊維護細刪轉換")
_SIMPLIFIED_HINTS |= frozenset("请检帮设错讯选项编辑预览测试页启国获简陈点击维护细删转换")
# Shared glyphs have zero script-discrimination value (for example "示").
_shared_han = _TRADITIONAL_HINTS & _SIMPLIFIED_HINTS
_TRADITIONAL_HINTS -= _shared_han
_SIMPLIFIED_HINTS -= _shared_han


def _chinese_variant_evidence(text: str) -> str:
    """Return a Chinese script variant only when actual distinctive glyphs occur.

    Neutral Han text intentionally returns "" so a preceding Traditional or
    Simplified conversation can keep its writing system across short replies.
    """
    traditional = sum(ch in _TRADITIONAL_HINTS for ch in text)
    simplified = sum(ch in _SIMPLIFIED_HINTS for ch in text)
    if traditional > simplified:
        return "zh-TW"
    if simplified > traditional:
        return "zh-CN"
    return ""

_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_LATIN_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’-][A-Za-zÀ-ÖØ-öø-ÿ]+)?")


@dataclass(frozen=True)
class ReasoningLanguageDecision:
    preference: str
    language: str
    source: str
    instruction: str


def normalize_reasoning_language(value: Any) -> str:
    """Normalize a configured presentation-language preference.

    ``auto`` follows the current conversation.  Unknown but syntactically sane
    locale tags are preserved instead of forcing a release every time a new
    language is requested.
    """
    text = str(value or _DEFAULT).strip()
    if not text:
        return _DEFAULT
    alias = _ALIASES.get(text.lower())
    if alias:
        return alias
    if _LOCALE_RE.fullmatch(text):
        parts = text.split("-")
        if len(parts) == 1:
            return parts[0].lower()
        return parts[0].lower() + "-" + "-".join(
            p.upper() if len(p) == 2 else p.title() for p in parts[1:]
        )
    return _DEFAULT


def _clean_for_detection(text: str) -> str:
    value = _CODE_FENCE_RE.sub(" ", str(text or ""))
    value = _INLINE_CODE_RE.sub(" ", value)
    value = _URL_RE.sub(" ", value)
    return value


def _script_counts(text: str) -> dict[str, int]:
    counts = {"han": 0, "kana": 0, "hangul": 0, "cyrillic": 0, "arabic": 0,
              "hebrew": 0, "devanagari": 0, "thai": 0, "greek": 0, "latin": 0}
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0xF900 <= cp <= 0xFAFF):
            counts["han"] += 1
        elif (0x3040 <= cp <= 0x30FF) or (0x31F0 <= cp <= 0x31FF):
            counts["kana"] += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            counts["hangul"] += 1
        elif (0x0400 <= cp <= 0x052F) or (0x2DE0 <= cp <= 0x2DFF) or (0xA640 <= cp <= 0xA69F):
            counts["cyrillic"] += 1
        elif (0x0600 <= cp <= 0x06FF) or (0x0750 <= cp <= 0x077F) or (0x08A0 <= cp <= 0x08FF):
            counts["arabic"] += 1
        elif 0x0590 <= cp <= 0x05FF:
            counts["hebrew"] += 1
        elif 0x0900 <= cp <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0E00 <= cp <= 0x0E7F:
            counts["thai"] += 1
        elif 0x0370 <= cp <= 0x03FF:
            counts["greek"] += 1
        elif ("A" <= ch <= "Z") or ("a" <= ch <= "z") or (0x00C0 <= cp <= 0x024F):
            counts["latin"] += 1
    return counts


def _latin_language(text: str) -> str:
    words = [w.lower() for w in _LATIN_WORD_RE.findall(text)]
    # A lone acronym / model name is not evidence that the user switched to
    # English. Let recent conversation language win instead.
    if len(words) <= 2:
        nontechnical = [w for w in words if len(w) >= 3 and not w.isupper()]
        if not nontechnical:
            return ""
    scores = {lang: sum(1 for word in words if word in hints) for lang, hints in _LATIN_HINTS.items()}
    best = max(scores, key=scores.get, default="en")
    if scores.get(best, 0) >= 2:
        return best
    # Ordinary Latin prose defaults to English; terse identifier-heavy text is
    # left undecided so conversation history can stabilize the choice.
    alpha_chars = sum(ch.isalpha() for ch in text)
    if len(words) >= 3 and alpha_chars >= 8:
        return "en"
    return ""


def classify_text_language(text: str) -> str:
    """Best-effort language classification for one user-authored text chunk."""
    cleaned = _clean_for_detection(text)
    if not cleaned.strip():
        return ""
    c = _script_counts(cleaned)
    if c["kana"] >= 1 and (c["kana"] + c["han"]) >= 2:
        return "ja"
    if c["hangul"] >= 2:
        return "ko"
    if c["cyrillic"] >= 2:
        return "ru"
    if c["arabic"] >= 2:
        return "ar"
    if c["hebrew"] >= 2:
        return "he"
    if c["devanagari"] >= 2:
        return "hi"
    if c["thai"] >= 2:
        return "th"
    if c["greek"] >= 2:
        return "el"

    # Mixed Chinese + technical English should stay Chinese. Two Han characters
    # are enough for phrases such as "check一下 ACP" even when acronyms dominate
    # the byte/letter count.
    cjk_latin = c["han"] + c["latin"]
    if c["han"] >= 2 and (c["han"] / max(1, cjk_latin) >= 0.06 or c["han"] >= 4):
        return _chinese_variant_evidence(cleaned) or "zh-CN"
    if c["han"] >= 1 and c["latin"] == 0:
        return _chinese_variant_evidence(cleaned) or "zh-CN"
    if c["latin"]:
        return _latin_language(cleaned)
    return ""


def _message_text(message: Any) -> str:
    if not isinstance(message, dict) or str(message.get("role") or "") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text"} and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return " ".join(parts)


def recent_user_texts(history: Iterable[Any], limit: int = 5) -> tuple[str, ...]:
    rows = [_message_text(message) for message in history]
    rows = [row for row in rows if row.strip()]
    return tuple(rows[-max(0, int(limit)):])


def detect_conversation_language(
    current_text: str,
    history: Iterable[Any] = (),
    *,
    fallback_language: str = "en",
) -> tuple[str, str]:
    """Resolve current author language, retaining script across neutral turns.

    A short technical-only turn uses previous user messages, and only then an
    explicitly selected UI language when available. The UI is never allowed to
    override a clear message in a different language.
    """
    current = classify_text_language(current_text)
    cleaned = _clean_for_detection(current_text)
    recent = recent_user_texts(history, 5)

    if current in {"zh-CN", "zh-TW"}:
        variant = _chinese_variant_evidence(cleaned)
        if variant:
            return variant, "current-message"
        # "你好", "继续"-like neutral text should not silently flip a zh-TW
        # conversation to zh-CN (or inherit unrelated English tool names).
        for prior in reversed(recent):
            previous = classify_text_language(prior)
            if previous in {"zh-CN", "zh-TW"}:
                distinctive = _chinese_variant_evidence(_clean_for_detection(prior))
                if distinctive:
                    return distinctive, (
                        "current-message" if distinctive == current else "conversation-history"
                    )
        if fallback_language in {"zh-CN", "zh-TW"}:
            return fallback_language, "ui-language-fallback"
        return "zh-CN", "current-message"

    # Explicit Japanese, Korean, other scripts or confident Latin prose win.
    if current:
        letters = sum(ch.isalpha() for ch in cleaned)
        if current in {"ja", "ko", "ru", "ar", "he", "hi", "th", "el"} or letters >= 8:
            return current, "current-message"

    candidates: list[str] = []
    if current:
        candidates.append(current)
    for prior in reversed(recent):
        lang = classify_text_language(prior)
        if lang:
            candidates.append(lang)
    if candidates:
        weights = (3.0, 2.0, 1.4, 1.0, 0.7, 0.5)
        scores: dict[str, float] = {}
        for index, lang in enumerate(candidates[: len(weights)]):
            scores[lang] = scores.get(lang, 0.0) + weights[index]
        return max(scores, key=scores.get), "conversation-history"
    fallback = normalize_reasoning_language(fallback_language)
    if fallback not in {"auto", ""}:
        return fallback, "ui-language-fallback"
    return "en", "fallback"


def _language_name(code: str) -> str:
    if code in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[code]
    base = code.split("-", 1)[0]
    return _LANGUAGE_NAMES.get(base, code)


def reasoning_language_instruction(language: str) -> str:
    """Build a provider-neutral system hint for user-visible reasoning."""
    code = normalize_reasoning_language(language)
    if code == "auto":
        code = "en"
    name = _language_name(code)
    script_rule = (
        "For Chinese, use Simplified Chinese characters (简体中文), not Traditional Chinese (繁體中文). "
        if code == "zh-CN" else
        "For Chinese, use Traditional Chinese characters (繁體中文), not Simplified Chinese (简体中文). "
        if code == "zh-TW" else ""
    )
    return (
        "Reasoning presentation language for this turn: "
        f"write all user-visible thinking/reasoning text, reasoning summaries, and explanatory "
        f"narration in {name} ({code}). {script_rule}Begin directly in {name}; do not default to English "
        "merely because internal technical analysis, source material, or identifiers use English. "
        "Keep established technical terms, protocol/API/model/product names, acronyms, "
        "code identifiers, parameter names, file paths, commands, mathematical notation, and "
        "quotations in their natural/original spelling whenever that is clearer or more conventional (for example "
        "ACP, LLM, Token, API, Provider, Router, configOptions). Do not mechanically translate "
        "technical terminology. This is a presentation preference only and is not a request to "
        "reveal hidden/private chain-of-thought beyond what the provider normally exposes. "
        "It does not override an explicit user request about the final answer language."
    )


def resolve_reasoning_language(
    preference: Any,
    current_text: str,
    history: Iterable[Any] = (),
    *,
    fallback_language: str = "en",
) -> ReasoningLanguageDecision:
    pref = normalize_reasoning_language(preference)
    if pref != "auto":
        language, source = pref, "configured"
    elif (explicit := explicit_response_language(current_text)):
        language, source = explicit, "explicit-user-request"
    else:
        language, source = detect_conversation_language(
            current_text, history, fallback_language=fallback_language
        )
    return ReasoningLanguageDecision(
        preference=pref,
        language=language,
        source=source,
        instruction=reasoning_language_instruction(language),
    )


SUPPORTED_LANGUAGE_PREFERENCES = frozenset({"auto", "zh-CN", "zh-TW", "en", "ja", "ko"})


def normalize_supported_language(value: Any) -> str:
    normalized = normalize_reasoning_language(value)
    if normalized not in SUPPORTED_LANGUAGE_PREFERENCES:
        raise ValueError("unsupported language preference")
    return normalized


# Explicit instructions in the user's current message outrank an automatic or
# fixed default for the final answer; quoted code/logs have already been stripped.
_EXPLICIT_RESPONSE = (
    (re.compile(r"(?:请|請)(?:用|以|使用)?\s*(?:简体中文|簡體中文)(?:回答|回复|回覆|作答|输出|輸出)"), "zh-CN"),
    (re.compile(r"(?:请|請)(?:用|以|使用)?\s*(?:繁體中文|繁体中文)(?:回答|回复|回覆|作答|输出|輸出)"), "zh-TW"),
    (re.compile(r"(?:请|請)(?:用|以|使用)?\s*(?:英文|英语|英語)(?:回答|回复|回覆|作答|输出|輸出)"), "en"),
    (re.compile(r"(?:请|請)(?:用|以|使用)?\s*(?:日语|日語|日本語)(?:回答|回复|回覆|作答|输出|輸出|説明)"), "ja"),
    (re.compile(r"(?:请|請)(?:用|以|使用)?\s*(?:韩语|韓語|韩文|韓文|한국어)(?:回答|回复|回覆|作答|输出|輸出)"), "ko"),
    (re.compile(r"\b(?:answer|respond|reply|write)\s+(?:in|using)\s+(?:English|Chinese|Japanese|Korean)\b", re.I), None),
    (re.compile(r"日本語で(?:答え|回答|返答|説明)|日本語でお願いします"), "ja"),
    (re.compile(r"(?:한국어|한글)로\s*(?:답변|대답|설명)"), "ko"),
)


def explicit_response_language(text: str) -> str:
    cleaned = _clean_for_detection(text)
    for pattern, language in _EXPLICIT_RESPONSE:
        match = pattern.search(cleaned)
        if match:
            if language:
                return language
            name = match.group(0).lower()
            return {"english":"en", "chinese":"zh-CN", "japanese":"ja", "korean":"ko"}.get(name.split()[-1], "")
    return ""


def resolve_response_language(
    preference: Any, current_text: str, history: Iterable[Any] = (),
    *, fallback_language: str = "en",
) -> tuple[str, str, str]:
    explicit = explicit_response_language(current_text)
    pref = normalize_supported_language(preference)
    if explicit:
        language, source = explicit, "explicit-user-request"
    elif pref == "auto":
        language, source = detect_conversation_language(
            current_text, history, fallback_language=fallback_language
        )
    else:
        language, source = pref, "configured"
    script_rule = (
        "Use Simplified Chinese characters throughout the answer; do not switch to Traditional Chinese. "
        if language == "zh-CN" else
        "Use Traditional Chinese characters throughout the answer; do not switch to Simplified Chinese. "
        if language == "zh-TW" else ""
    )
    instruction = (
        f"Write the final user-facing answer in {_language_name(language)} ({language}). "
        + script_rule
        + "Do not drift into another language because the tool output, model metadata, or source code is English. "
        "A clear request from the user in the current turn takes precedence over this default. "
        "This instruction applies only to natural-language answers: preserve tool call JSON, "
        "schema-bound structured output, source code, quoted text, file paths, protocol identifiers, "
        "and the language of content the user explicitly asks to reproduce. "
        "Do not alter tool inputs or outputs to enforce the language preference."
    )
    return language, source, instruction
