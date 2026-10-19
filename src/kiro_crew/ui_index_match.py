"""How ``find_ui`` reads a question: the index's vocabulary and the literal matcher.

The constants every layer of the dashboard-location index shares -- the packaged
file paths and size caps, the schema version, the tier, guide-policy and
requirement vocabularies -- and the matcher built on them: the per-locale question
words, stop phrases, bare verbs and UI nouns, NFKC normalisation and stemming, the
searchable ``_Text`` form of a label, the scores that compare a question with it,
and the ``_Index`` shape the matcher walks.

:mod:`kiro_crew.ui_index` stays the import path and the patch surface for every name
here (:mod:`kiro_crew._facade_mirror`); its module docstring states the matching
rules. ``INDEX_PATH`` and ``AUTO_INDEX_PATH`` resolve from this file's directory,
which is why the owners are top-level siblings of the facade. This module imports no
other part of the index.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The committed index (generated + curated tiers). This and
#: :data:`AUTO_INDEX_PATH` are the only files read; nothing a tool caller
#: passes becomes a path.
INDEX_PATH = Path(__file__).resolve().parent / "docs" / "ui-index.generated.json"
#: The build-time auto tier, shipped inside the dashboard bundle (never
#: committed). Optional: absent in a source checkout with no frontend build.
AUTO_INDEX_PATH = Path(__file__).resolve().parent / "static" / "dist" / "ui-index.auto.json"
_AUTO_ARTIFACT = "auto"
_AUTO_AVAILABLE = "available"
_AUTO_UNAVAILABLE = "unavailable"
#: The longest CJK label that is still "one word" for the auto tier (返回, 名称).
_AUTO_CJK_ONE_WORD_MAX = 2
SCHEMA_VERSION = 1
MAX_RESULTS = 8
#: Ceiling on the serialized response (UTF-8), applied to whole records. It
#: must be at least the bytes of :data:`_TOO_BIG` (the fixed answer when even
#: one record does not fit), which carries no index metadata for that reason.
MAX_RESPONSE_BYTES = 6 * 1024
#: The packaged index is read whole; a file past this size is not read at all.
MAX_INDEX_BYTES = 4 * 1024 * 1024
QUERY_MAX_CHARS = 200
DEFAULT_LOCALE = "en"
#: The dashboard shell (the top bar): a placement there is on every page, so it
#: has no route and no parent path. Kept under any ``surface`` filter.
SHELL_SURFACE = "shell"
_LITERAL_PREFIX = "literal:"
#: ``label_kind`` of a location whose on-screen label is runtime data (the
#: model chip shows the model's name): its ``label_key`` is a static
#: description, returned as ``description`` and never as a label to quote.
_DESCRIPTION = "description"
_REQUIREMENT_KINDS = frozenset({"shown_by", "viewport", "preview_flag", "condition"})
#: ``tier`` of a location. ``generated`` (pages, tabs, settings) and ``curated``
#: (registered controls) are proven whole; ``auto`` is an unregistered control
#: whose label and page are proven from source but whose exact place and
#: prerequisites are not (it carries ``conditions_unknown: true``). An index
#: without the field is read as curated.
_CURATED = "curated"
_AUTO = "auto"
#: A location's ``guide_policy``: ``point`` (a guide may point at it),
#: ``caution`` (a destructive control a guide may point at, with a warning:
#: the record says ``caution: true``), ``search-only`` (find_ui answers with
#: it with a ``ui.find`` ``find_ref`` instead of a planned guide) or ``deny``
#: (the agent's own ceiling, never guided). Only ``point`` and ``caution`` ever carry a ``ui.show``
#: ``guide_ref``; an auto entry is guidable only when it renders through a
#: reviewed primitive (see the generator).
_POINT = "point"
_CAUTION = "caution"
_GUIDABLE_POLICIES = frozenset({_POINT, _CAUTION})
_GUIDE_POLICIES = frozenset({_POINT, _CAUTION, "search-only", "deny"})
_TIERS = frozenset({"generated", _CURATED, _AUTO})
#: Match bases an auto location may answer with, and only at full coverage:
#: the whole label is in the question (question words aside). A question that
#: is only part of the label (``query_phrase``: "sign" inside "Open sign-in
#: page", whose other words are all question words) is never enough.
_AUTO_BASES = frozenset({"exact_label", "label_phrase"})
_FULL = 1.0 - 1e-9
_TOO_BIG: dict[str, Any] = {
    "status": "unavailable",
    "reason": "the best match does not fit the response limit",
    "results": [],
}

_LANG_RE = re.compile(r"\A[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?\Z")
_SURFACE_RE = re.compile(r"\A[a-z][a-z0-9-]{0,63}\Z")
_SPACE_RE = re.compile(r"\s+")

#: Question words that say nothing about WHICH location. They are dropped from
#: both the question and the label before coverage is measured, so "where are
#: my old chats" is about {old, chats}, and a label that explains one of the two
#: is not an answer. English always applies; a form in another space-separated
#: language also drops that language's list (never another language's, where
#: the same spelling can be a content word).
_STOPWORDS = frozenset(
    "a an the is are am be was were where wheres how do does did i me my mine we our "
    "can could should would to in on at of for from into with find found see show "
    "open get go look located location which what whats it its this that there here "
    "please you your option button setting settings page menu tab panel turn change "
    "switch need want have has".split()
)
_LOCALE_STOPWORDS: dict[str, frozenset[str]] = {
    code: frozenset(words.split())
    for code, words in {
        "es": "dónde donde está están esta estan cómo como mi mis tu tus el la los las un una "
        "de del en se puedo puede ver encontrar cambiar qué que es son hay por para",
        "fr": "où ou est sont comment mes mon ma le la les un une de des du dans je puis peux "
        "trouver voir changer quel quelle quels quelles qu que ce se trouve trouvent",
        "de": "wo ist sind wie meine mein meinen der die das den dem ein eine finde finden "
        "ich kann sehen ändern",
        "pt": "onde está estão esta estao como meus minhas meu minha o a os as um uma de do da "
        "em posso ver encontrar mudar fica ficam",
        "it": "dove è sono come miei mie mio mia il lo la i gli le un una di del della in "
        "posso trovare vedere cambiare trovo",
        "ru": "где мои мой моя мое моё находится находятся как найти посмотреть изменить можно "
        "я в на",
        "hi": "कहाँ कहां है हैं मेरी मेरा मेरे कैसे मैं को की का के में",
        "bn": "কোথায় আমার কীভাবে কিভাবে আছে আমি",
    }.items()
}
#: The same idea for CJK questions, removed as whole phrases (longest first)
#: from both sides before measuring phrase coverage. Single characters appear
#: only where they cannot be part of a label word in practice.
_CJK_STOP_PHRASES = tuple(
    sorted(
        {
            # zh
            "在哪里",
            "在哪儿",
            "在哪",
            "哪里",
            "哪儿",
            "怎么",
            "如何",
            "怎样",
            "我的",
            "找到",
            "查看",
            "打开",
            "可以",
            "的",
            "吗",
            "呢",
            "了",
            "请",
            "我",
            # Determiners: "this/these/that" in "copy this conversation".
            "这个",
            "这些",
            "那个",
            "那些",
            # ja
            "どこ",
            "ですか",
            "ありますか",
            "どうやって",
            "私の",
            "は",
            "の",
            "を",
            "に",
            # ko (multi-syllable only: single syllables are parts of words)
            "어디에",
            "어디",
            "있나요",
            "있어요",
            "어떻게",
            "나의",
        },
        key=lambda s: (-len(s), s),
    )
)
#: A candidate must explain at least this share of the question's content (and
#: the question this share of the label's): one shared word in a longer
#: question, or a generic word inside a longer label, is not an answer.
_MIN_COVERAGE = 0.6

#: Action verbs that name no object on their own. A question made only of
#: these ("add", "delete it", "删除") is about nothing in particular: the same
#: verb is a button in a dozen places, and answering with whichever label is an
#: exact match would be a confident guess. Matched on content tokens (question
#: words already dropped) for space-separated scripts, on the whole stripped
#: phrase for CJK.
_BARE_VERBS = frozenset(
    "add delete remove create new make edit update upgrade install uninstall close stop "
    "start run rename copy duplicate move enable disable save cancel clear refresh reload "
    "sync send upload download set reset mute unmute pin unpin select archive restore "
    "hide unhide expand collapse join leave connect disconnect share export import help".split()
)
_BARE_CJK_VERBS = frozenset(
    {
        "添加",
        "删除",
        "移除",
        "新建",
        "创建",
        "编辑",
        "修改",
        "更新",
        "升级",
        "安装",
        "卸载",
        "关闭",
        "停止",
        "开始",
        "运行",
        "重命名",
        "复制",
        "移动",
        "启用",
        "停用",
        "禁用",
        "保存",
        "取消",
        "清除",
        "清空",
        "刷新",
        "同步",
        "发送",
        "上传",
        "下载",
        "设置",
        "重置",
        "分享",
        "导出",
        "导入",
        "帮助",
    }
)
#: Words naming a kind of on-screen control ("the stop button"). With a bare
#: verb they say the question is about a control, never which one: see
#: :func:`_sole_control`.
_UI_NOUNS = frozenset({"button", "icon", "control"})
_CJK_UI_NOUNS = ("按钮", "图标", "按键")
#: Location kinds that are a control a person presses (not a page or a setting).
_CONTROL_KINDS = frozenset({"button", "menu-item", "toggle", "link", "disclosure"})
#: A registered picker (``kind: list``) a guide's ``select`` step points at.
_PICKER_KIND = "list"
#: ``match`` of an answer found through :func:`_sole_control`.
_VERB_CONTROL_BASIS = "verb_control"
#: Said with every non-English answer: which fields stay English, and what to do.
_PROSE_NOTE = (
    "labels are in the requested language; translate description, only_if, otherwise"
    " and needs_object"
)
#: Said instead when the labels follow the language the user's dashboard shows
#: (``label_lang``), which may differ from the language of the question.
_DASHBOARD_PROSE_NOTE = (
    "labels are in the language the user's dashboard shows: quote them unchanged; "
    "translate description, only_if, otherwise and needs_object into the reply's language"
)
#: ``locale_source`` when the labels follow the dashboard's own language.
_DASHBOARD_SOURCE = "dashboard"
#: Wording returned with a bare-verb ``no_match`` (English, like all prose here).
_NEEDS_OBJECT = "the question names an action but not what it acts on; ask what to {verb}"

# Score bands. A candidate below _MIN_SCORE is dropped before any refinement,
# so a parent match can only reorder real target matches, never create one.
_SCORE_ID = 1000
_SCORE_EXACT = 900
_SCORE_LABEL_IN_QUERY = 700
_SCORE_QUERY_IN_LABEL = 600
_SCORE_ALL_TOKENS = 500
_SCORE_CJK_BIGRAMS = 400
_SCORE_MOST_TOKENS = 300
_MIN_SCORE = 300
_PARENT_BONUS = 40
#: Match basis of a form that explains part of the question while its path
#: explains the rest; only returned when nothing matches by its own words.
_PATH_BASIS = "tokens_with_path"


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF  # Hiragana, Katakana
        or 0x3400 <= o <= 0x4DBF
        or 0x4E00 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xAC00 <= o <= 0xD7AF  # Hangul syllables
        or 0x1100 <= o <= 0x11FF
        or 0x3130 <= o <= 0x318F
    )


def normalize(text: str) -> str:
    """NFKC + casefold, every non letter/mark/number run collapsed to one space."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in folded)
    return _SPACE_RE.sub(" ", kept).strip()


#: "everything", "everywhere", "anything", "anywhere": one meaning for a search
#: ("search everything" is the "Search everywhere" button). Only these four:
#: "someone"/"somewhere" and the like say something else.
_ALL_WORDS = re.compile(r"\A(?:every|any)(?:thing|where)\Z")


def _stem(token: str) -> str:
    if _ALL_WORDS.match(token):
        return "every"
    if token.isascii() and len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


_stop_cache: dict[str, frozenset[str]] = {}


def _stopwords(locale: str) -> frozenset[str]:
    """English plus ``locale``'s question words, raw and stemmed (tokens are stemmed)."""
    hit = _stop_cache.get(locale)
    if hit is None:
        words = _STOPWORDS | _LOCALE_STOPWORDS.get(locale, frozenset())
        hit = frozenset(words | {_stem(w) for w in words})
        _stop_cache[locale] = hit
    return hit


def _strip_cjk(compact: str) -> str:
    for phrase in _CJK_STOP_PHRASES:
        compact = compact.replace(phrase, "")
    return compact


def _bare_verb(q: _Text, locale: str) -> str | None:
    """The verb when the question is only action verbs (no object), else None."""
    if q.cjk:
        return q.core if q.core in _BARE_CJK_VERBS else None
    if q.content and q.content <= _BARE_VERBS:
        return " ".join(tok for tok in q.tokens if tok in q.content)
    return None


def _noun_verb(q: _Text) -> str | None:
    """The one verb of a question that is that verb plus a control noun, else None.

    "stop button" and "停止按钮" qualify; "stop", "stop start button" and
    "stop the reply" do not (no noun, two verbs, a real object).
    """
    if q.cjk:
        for noun in _CJK_UI_NOUNS:
            if q.core.endswith(noun) and q.core[: -len(noun)] in _BARE_CJK_VERBS:
                return q.core[: -len(noun)]
        return None
    rest = q.content - _UI_NOUNS
    if len(rest) == 1 and rest <= _BARE_VERBS and _UI_NOUNS & set(q.tokens):
        return next(iter(rest))
    return None


def _sole_control(idx: _Index, verb: str, code: str) -> tuple[str, str | None] | None:
    """(location id, label key) of the ONE control whose own label starts with ``verb``.

    Only a control's label counts, never a search term or a setting: "stop"
    starts Stop generation alone, so "stop button" is that control; "delete"
    starts two labels, so "delete button" names nothing in particular.
    """
    hits: dict[str, str | None] = {}
    for loc_id, per in idx.texts.items():
        loc = idx.by_id[loc_id]
        # An auto entry never makes a control the sole answer: its place is unknown.
        if loc.get("kind") not in _CONTROL_KINDS or loc.get("tier") == _AUTO:
            continue
        for t in per.get(code, []):
            if t.term:
                continue
            if t.compact.startswith(verb) if t.cjk else t.tokens[:1] == (verb,):
                hits.setdefault(loc_id, t.key)
    if len(hits) != 1:
        return None
    return next(iter(hits.items()))


def _bigrams(compact: str) -> frozenset[str]:
    return frozenset(compact[i : i + 2] for i in range(len(compact) - 1))


def _one_word(t: _Text) -> bool:
    """Whether a label form is one word, so an AUTO entry may not answer with it.

    Space-separated scripts: one token after folding ("Back", "Name", "Save";
    "Sign-in" is two). CJK, which has no spaces to count: a label of at most
    :data:`_AUTO_CJK_ONE_WORD_MAX` characters (返回, 名称, 名前), the length
    of a single common word. A one-word label on one page names one of many
    look-alike controls, and an auto entry has nothing else (no terms, no
    place) to tell them apart; generated and curated locations are not
    affected.
    """
    if t.cjk:
        return len(t.compact) <= _AUTO_CJK_ONE_WORD_MAX
    return len(t.tokens) <= 1


@dataclass(frozen=True)
class _Text:
    """One searchable form (a label or a curated search term) in one locale.

    ``content`` is the token set minus that locale's question words and
    ``core`` the CJK compact form minus question phrases: what coverage is
    measured on.
    """

    norm: str
    compact: str
    cjk: bool
    tokens: tuple[str, ...]
    content: frozenset[str]
    core: str
    term: bool = False
    #: The catalog key a label form came from (None for a search term).
    key: str | None = None

    @classmethod
    def of(
        cls, raw: str, locale: str = DEFAULT_LOCALE, *, term: bool = False, key: str | None = None
    ) -> _Text:
        norm = normalize(raw)
        compact = norm.replace(" ", "")
        cjk = any(_is_cjk(c) for c in compact)
        tokens = tuple(_stem(t) for t in norm.split(" ") if t)
        stop = _stopwords(locale)
        content = frozenset(t for t in tokens if t not in stop)
        core = _strip_cjk(compact)
        return cls(norm, compact, cjk, tokens, content, core, term, key)


def _contains_run(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _ratio(part: str, whole: str) -> float:
    """Share of ``whole`` that ``part`` (a substring of it) explains."""
    return 1.0 if not whole else min(1.0, len(part) / len(whole))


def _cjk_rest(q: _Text, t: _Text, context: tuple[_Text, ...]) -> float:
    """Share of the CJK question the label explains, once the phrases its
    ancestors explain are taken out of the question."""
    rest = q.core
    if t.core and t.core in rest:
        head, _, tail = rest.partition(t.core)
        for c in context:
            if len(c.core) >= 2 and c.core not in t.core:
                head, tail = head.replace(c.core, "", 1), tail.replace(c.core, "", 1)
        rest = head + t.core + tail
    return _ratio(t.core, rest)


def _score(
    q: _Text, t: _Text, *, phrase_only: bool, context: tuple[_Text, ...] = ()
) -> tuple[int, str, float]:
    """(score, basis, coverage). ``q`` must be built for ``t``'s locale.

    The score comes from the form ``t`` alone: a location must match by its own
    label or term. ``context`` (the labels of the location's ancestors in the
    path) only helps coverage, so "voice model" is fully explained by Voice >
    Model while "chat about dinner" stays unexplained. Coverage is how much of
    the question the form and its ancestors explain, and how much of the form
    the question explains; a match below :data:`_MIN_COVERAGE` is never
    returned, whatever its score.
    """
    if not t.norm:
        return 0, "", 0.0
    if q.norm == t.norm:
        return _SCORE_EXACT, "exact_label", 1.0
    if t.cjk or q.cjk:
        if len(t.compact) >= 2 and t.compact in q.compact:
            return (
                _SCORE_LABEL_IN_QUERY + min(len(t.compact), 50),
                "label_phrase",
                _cjk_rest(q, t, context),
            )
        if len(q.compact) >= 2 and q.compact in t.compact:
            return _SCORE_QUERY_IN_LABEL, "query_phrase", _ratio(q.core, t.core)
        t_grams, q_grams = _bigrams(t.core), _bigrams(q.core)
        if t.cjk and not phrase_only and len(t_grams) >= 2:
            covered = len(t_grams & q_grams) / len(t_grams)
            if covered >= 0.75:
                return (
                    _SCORE_CJK_BIGRAMS + int(covered * 50),
                    "phrase_overlap",
                    _jaccard(t_grams, q_grams),
                )
        return 0, "", 0.0
    # Words an ancestor's label explains leave the question before coverage is
    # measured; every other question word must still be explained by the form.
    ancestors: frozenset[str] = frozenset().union(*(c.content for c in context))
    coverage = _jaccard(q.content - (ancestors - t.content), t.content)
    # When the form and its path together explain EVERY question word ("import
    # an artifact": Import from a file, under Artifacts), the path's words also
    # count on the form's side, so a form's own extra word ("file") does not
    # sink it. A question word nobody explains ("dinner") keeps the strict share.
    by_path = (ancestors & q.content) - t.content
    if by_path and q.content <= t.content | by_path:
        coverage = max(coverage, _jaccard(q.content, t.content | by_path))
    q_content = tuple(tok for tok in q.tokens if tok in q.content)
    if len(t.norm) >= 3 and _contains_run(q.tokens, t.tokens):
        return _SCORE_LABEL_IN_QUERY + min(len(t.norm), 50), "label_phrase", coverage
    if q_content and len(" ".join(q_content)) >= 3 and _contains_run(t.tokens, q_content):
        return _SCORE_QUERY_IN_LABEL, "query_phrase", coverage
    if phrase_only or not t.content:
        return 0, "", 0.0
    matched = len(t.content & q.content)
    if matched == len(t.content):
        return _SCORE_ALL_TOKENS + 10 * matched, "tokens", coverage
    if matched >= 2 and matched / len(t.content) >= 0.6:
        return _SCORE_MOST_TOKENS + 10 * matched, "tokens", coverage
    # The form matches part of the question and the path the rest ("Import from
    # a file" under Artifacts for "import an artifact"). The form must still
    # carry a question word and at least half of its own words: the path only
    # completes a match, it never makes one.
    if (
        matched >= 1
        and by_path
        and q.content <= t.content | by_path
        and matched / len(t.content) >= 0.5
    ):
        return _SCORE_MOST_TOKENS + 10 * matched, _PATH_BASIS, coverage
    return 0, "", 0.0


@dataclass
class _Index:
    raw: dict[str, Any]
    by_id: dict[str, dict[str, Any]]
    locales: tuple[str, ...]
    surfaces: frozenset[str]
    labels: dict[str, dict[str, str]]
    #: location id -> locale -> searchable forms (label, aliases, then search terms).
    texts: dict[str, dict[str, list[_Text]]] = field(default_factory=dict)
    conditions: dict[str, str] = field(default_factory=dict)
    reveal_states: dict[str, str] = field(default_factory=dict)
    #: (location id, locale) -> its ancestors' label forms, built on first use.
    contexts: dict[tuple[str, str], tuple[_Text, ...]] = field(default_factory=dict)
    #: Whether the build-time auto tier was merged in, and why not.
    auto_status: str = _AUTO_UNAVAILABLE
    auto_reason: str | None = "no auto tier file was read"
    auto_scope: str | None = None
