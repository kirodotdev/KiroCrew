"""Per-turn model routing for Kiro BYO modes.

Kiro BYO exposes a backend-owned virtual mode selection, separate from concrete
provider models. This module keeps the decision deterministic and inspectable:
hard capability gates
run first, then a policy-specific score ranks the remaining configured models.
No provider is hard-coded; optional model routing hints can override the neutral
heuristics used when a provider does not publish price/performance metadata.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .config import KiroByoConfig, ModelSpec, ProviderSpec
from .agent_runtime import AgentSpec, ExecutionPlan, TaskProfile
from .adaptive_routing import current_snapshot, blend_speed, reliability_adjustment
from .execution_intent import ExecutionIntent, compile_execution_intent
from .policy_packs import (
    ExecutionPolicy, PolicyDefinition, PolicyPackError, RoutingProfile, ScoreWeights,
    default_pack, get_policy_pack, resolve_policy,
)
from .reasoning_control import ReasoningInvocation, resolve_thinking_mode, thinking_auto_suggestion

# Generic tier words only; no vendor/model id is special-cased. They are used as
# a last-resort prior when providers do not publish routing metadata.
_FAST = re.compile(r"(?:^|[-_. ])(flash|haiku|mini|nano|lite|fast|speed|ultraspeed|air)(?:$|[-_. ])", re.I)
_STRONG = re.compile(r"(?:^|[-_. ])(pro|opus|max|ultra|premier|reasoner|reasoning)(?:$|[-_. ])", re.I)
_BALANCED = re.compile(r"(?:^|[-_. ])(plus|sonnet|standard|medium)(?:$|[-_. ])", re.I)
_CODE = re.compile(r"```|\b(?:code|coding|debug|refactor|compile|compiler|test|pytest|typescript|javascript|python|rust|java|c\+\+|sql|git)\b|代码|编程|调试|重构|编译|测试|程式碼|原始碼|程式|測試|編譯|重構|コード|プログラム|テスト|リファクタ|코드|프로그래밍|테스트|디버깅|리팩터링", re.I)
_COMPLEX = re.compile(r"\b(?:analy[sz]e|reason|prove|derive|architecture|design|investigate|compare|audit|research|multi[- ]?step)\b|分析|推理|证明|推导|架构|设计|调研|审计|比较|多步", re.I)
_SIMPLE = re.compile(r"\b(?:translate|rewrite|summari[sz]e|format|rename|spell|define|what is|who is)\b|翻译|改写|总结|格式化|改名|是什么|叫什么", re.I)
# Complexity must not hinge on an ever-growing vocabulary of task nouns.  The
# following grammar/shape markers are deliberately *not* semantic difficulty
# keywords: they identify that a request contains multiple clauses/constraints
# in several common writing systems, while the fallback structural signals
# (sentences, lists, code, quoted/structured data and information density) work
# for languages not named here as well.
_CLAUSE_JOIN = re.compile(
    r"\b(?:and|then|while|whereas|plus|also|after|before|und|dann|sowie|"
    r"et|puis|ainsi\s+que|y|luego|adem[aá]s|e|depois|и|затем)\b"
    r"|(?:并且|并|以及|同时|然后|而且|且|随后|並且|並|以及|同時|然後|隨後)"
    r"|(?:そして|それから|かつ|また|さらに|したうえで|した上で)"
    r"|(?:그리고|또한|및|하고|하며|한\s*뒤|이후)",
    re.I,
)
_SENTENCE_BREAK = re.compile(r"[.!?。！？；;]+|\n+")
_LIST_ITEM = re.compile(r"(?m)^\s*(?:[-*+•]|\d+[.)、]|[A-Za-z][.)])\s+")
_STRUCTURED_MARK = re.compile(r"(?:->|=>|==|!=|<=|>=|::|\|\||&&)|[：:]|[()（）\[\]【】{}<>《》“”\"']")
_DENSE_SCRIPT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CURRENT_REQUEST = re.compile(r"\[CURRENT USER REQUEST[^\]]*\]", re.I)
_USER_MESSAGE_BLOCK = re.compile(
    r"---\s*USER MESSAGE BEGIN\s*---\s*(.*?)\s*---\s*USER MESSAGE END\s*---",
    re.I | re.S,
)
_WRAPPER_HINT = re.compile(
    r"(?:CURRENT USER REQUEST|USER MESSAGE (?:BEGIN|END)|SYSTEM (?:MESSAGE|PROMPT)|MEMORY|<system|\[SYSTEM)",
    re.I,
)

# Tool intent is deliberately separate from tool *availability*. Merely exposing
# tools to an agent must not force every turn onto a tool-capable model. These
# patterns only detect explicit external actions; general task complexity is
# handled independently below.
_TOOL_INTRINSIC = re.compile(
    r"\b(?:browse|look\s+up|web\s+search|search\s+(?:the\s+)?web|google|fetch\s+(?:the\s+)?url|download|upload|run\s+(?:the\s+)?command|execute\s+(?:the\s+)?command)\b"
    r"|(?:请|帮我|麻烦|去|需要你)?(?:联网|上网|浏览网页|搜索网页|检索网页|网上查|搜索一下|查一下)"
    r"|(?:ウェブ|Web|ネット)(?:で)?(?:検索|調べ)|検索して|調べて(?:ください|ほしい)|ブラウズして",
    re.I,
)
_TOOL_ACTION = re.compile(
    r"\b(?:open|read|inspect|check|review|audit|examine|look\s+at|take\s+a\s+look|find|locate|edit|modify|write|create|delete|remove|rename|list|scan|clean|cleanup|run|execute|query|send|schedule)\b"
    r"|(?:打开|读取|查看|看看|看一下|检查|审查|审计|查找|找出|找一下|编辑|修改|写入|创建|删除|移除|重命名|列出|扫描|清理|运行|执行|查询|发送|安排|審查|檢查|查閱|讀取|修改|寫入|新增|刪除)"
    r"|(?:開いて|読む|読んで|見て|見てください|確認して|調査して|監査して|探して|編集して|修正して|書き込|作成して|削除して|名前を変|一覧|スキャン|クリーンアップ|実行して|問い合わせ|送信して|予定)"
    r"|(?:검토|확인|수정|추가|삭제|실행|검색|조사|작성)(?:하|해|할)|(?:읽어|열어|고쳐)",
    re.I,
)
_TOOL_TARGET = re.compile(
    r"\b(?:file|folder|directory|path|repository|repo|codebase|workspace|drive|disk|storage|desktop|downloads?|appdata|terminal|shell|command|database|email|calendar|webpage|website|url|link|browser)\b"
    r"|(?:文件|文件夹|目录|路径|仓库|代码库|工作区|磁盘|硬盘|系统盘|盘符|存储|桌面|下载|终端|命令|数据库|邮件|邮箱|日历|网页|网站|链接|浏览器|檔案|資料夾|目錄|儲存庫|專案|資料庫)"
    r"|(?:ファイル|フォルダ|ディレクトリ|パス|リポジトリ|コードベース|ワークスペース|ドライブ|ディスク|ストレージ|デスクトップ|ダウンロード|ターミナル|シェル|コマンド|データベース|メール|カレンダー|ウェブページ|サイト|URL|リンク|ブラウザ)"
    r"|(?:파일|폴더|디렉터리|저장소|리포지토리|코드|프로젝트|작업\s*공간|데이터베이스|터미널|명령|웹사이트|브라우저)",
    re.I,
)
_TOOL_EXPLICIT = re.compile(r"\btool\b|工具|ツール", re.I)
_CONTINUE_CUE = re.compile(r"\b(?:continue|carry\s+on|keep\s+going|resume)\b|继续|接着|往下|続けて|再開して", re.I)
_PATH_OR_URL = re.compile(r"(?:https?://|file://|(?:^|\s)(?:[A-Za-z]:[\\/]|/)\S+|[A-Za-z]\s*(?:盘|ドライブ))", re.I)
_WRITE_ACTION = re.compile(
    r"\b(?:edit|modify|write|create|delete|remove|rename|refactor|fix|implement|commit|merge|deploy|install|uninstall|send|schedule)\b"
    r"|(?:编辑|修改|写入|创建|删除|移除|重命名|重构|修复|提交|合并|部署|安装|卸载|发送|安排|实现(?:一下|这个|功能|代码|方案|方法|接口|类|函数)|編輯|寫入|建立|刪除|修復|新增|部署|安裝)"
    r"|(?:編集|修正|書き込|作成|削除|名前を変|リファクタ|修復|コミット|マージ|デプロイ|インストール|送信|実装(?:して|を|する))"
    r"|(?:수정|추가|삭제|작성|구현|배포|설치|제거|리팩터링)(?:하|해|할)|고쳐",
    re.I,
)
_RISK_ACTION = re.compile(
    r"\b(?:production|prod|security|credential|secret|permission|delete|remove|deploy|payment|billing|database|migration|root|admin)\b"
    r"|(?:生产环境|线上|安全|凭据|密钥|权限|删除|部署|支付|账单|数据库|迁移|管理员|系统文件)"
    r"|(?:本番|セキュリティ|資格情報|秘密|権限|削除|デプロイ|支払|請求|データベース|移行|管理者)",
    re.I,
)
_LATENCY_CUE = re.compile(r"\b(?:quick|quickly|fast|brief|short|simple|just)\b|(?:快速|尽快|简单|简短|直接)|(?:早く|速く|簡単|短く)", re.I)
_BREADTH_CUE = re.compile(
    r"\b(?:all|every|whole|entire|across|compare|multiple|several|repository|codebase|global|comprehensive|audit)\b"
    r"|(?:全部|所有|全局|整个|多个|多份|比较|全面|仓库|代码库|审计|排查|全域|整個|多個|多份|比較|全面|儲存庫|審查)"
    r"|(?:全部|すべて|全体|複数|比較|包括的|リポジトリ|コードベース|監査)"
    r"|(?:전체|모든|여러|저장소|리포지토리|여러\s*모듈)",
    re.I,
)


@dataclass(frozen=True)
class RoutingSignals:
    prompt_chars: int
    history_chars: int
    history_messages: int
    has_image: bool
    tools_available: bool
    tools_required: bool
    reasoning_effort: str
    code_task: bool
    complexity: float
    estimated_input_tokens: int
    task_profile: str = "casual"
    breadth: float = 0.0
    parallelizability: float = 0.0
    risk: float = 0.0
    tool_intensity: float = 0.0
    write_intensity: float = 0.0
    context_demand: float = 0.0
    verification_value: float = 0.0
    latency_sensitivity: float = 0.0

    @property
    def tools_expected(self) -> bool:
        """Compatibility alias for older policy/test callers.

        Historically this field meant "the agent has tools". It now means
        "this turn actually requires tool use". New code should use
        ``tools_required`` and ``tools_available`` explicitly.
        """
        return self.tools_required


def task_profile_for_signals(signals: "RoutingSignals") -> str:
    """Return one stable primary task class for a RoutingSignals value.

    This is a pure classification of RoutingSignals, so it lives next to the
    type it classifies rather than in adaptive_routing (which consumes a task
    profile but does not define what one is). Fields are read directly off the
    frozen dataclass -- no getattr indirection -- because the shape is known.
    """
    if signals.has_image:
        return "vision"
    if signals.code_task:
        return "code"
    if signals.tools_required:
        return "tool"
    if signals.estimated_input_tokens >= 64_000 or signals.history_chars >= 80_000:
        return "long_context"
    if signals.complexity >= 0.68:
        return "research"
    return "casual"


@dataclass(frozen=True)
class CandidateScore:
    provider: ProviderSpec
    model: ModelSpec
    score: float
    quality: float
    speed: float
    economy: float
    reasons: tuple[str, ...]
    reliability: float = 0.94
    runtime_samples: int = 0
    runtime_confidence: str = "none"
    static_speed: float = 0.0
    runtime_speed: float | None = None


@dataclass(frozen=True)
class RouteDecision:
    pack: str
    pack_label: str
    policy: str
    policy_label: str
    policy_definition: ExecutionPolicy
    execution_intent: ExecutionIntent
    provider: ProviderSpec
    model: ModelSpec
    signals: RoutingSignals
    candidates: tuple[CandidateScore, ...]
    reason: tuple[str, ...]
    # Provider/network failover candidates. For tier-locked policies this list
    # intentionally excludes out-of-tier models whenever an in-tier model can
    # run the turn. Out-of-tier models are capability fallbacks, not outage
    # fallbacks.
    execution_fallbacks: tuple[CandidateScore, ...] = ()
    # Content-free exclusions help explain why another configured model was not eligible.
    exclusions: tuple[tuple[str, str, str], ...] = ()
    selection_source: str = "policy_ranked"


@dataclass(frozen=True)
class ReviewTarget:
    provider: ProviderSpec
    model: ModelSpec
    reason: str
    role: str = "review"
    reasoning_intent: str = "inherit"
    pack: str = ""
    pack_label: str = ""
    policy: str = ""
    policy_label: str = ""
    requires_substantive_work: bool = False


def _resolved_policy(pack_id: str, policy_id: str) -> tuple[str, str, PolicyDefinition]:
    """Resolve a pack/policy without teaching the router any pack-specific names."""
    pack_key = str(pack_id or "").strip().lower()
    policy_key = str(policy_id or "").strip().lower()
    if pack_key:
        pack = get_policy_pack(pack_key)
        if pack is None:
            raise PolicyPackError(f"policy pack {pack_key!r} is not installed")
    else:
        pack = default_pack()
    if not pack.enabled:
        raise PolicyPackError(f"policy pack {pack.id!r} is not available yet")
    policy = pack.policy(policy_key) if policy_key else pack.policy(pack.default_policy)
    if policy is None:
        policy = pack.policy(pack.default_policy)
    if policy is None:
        raise PolicyPackError(f"policy pack {pack.id!r} has no usable default policy")
    return pack.id, pack.display_name, policy


def normalize_policy(value: str, pack_id: str = "") -> str:
    return _resolved_policy(pack_id, value)[2].id


def policy_label(value: str, pack_id: str = "") -> str:
    return _resolved_policy(pack_id, value)[2].display_name


def pack_label(pack_id: str) -> str:
    return _resolved_policy(pack_id, "")[1]


def pack_short_label(pack_id: str) -> str:
    key = str(pack_id or "").strip().lower()
    if key:
        pack = get_policy_pack(key)
        if pack is None:
            raise PolicyPackError(f"policy pack {key!r} is not installed")
    else:
        pack = default_pack()
    if not pack.enabled:
        raise PolicyPackError(f"policy pack {pack.id!r} is not available yet")
    return pack.short_name


def _routing_user_text(value: str) -> tuple[str, bool]:
    """Return the user-authored slice used only for routing heuristics.

    KiroCrew may wrap the actual request in system instructions, memories and
    formatting rules before handing it to ACP. Those wrappers must still be
    sent upstream, but they must not make a casual request look like a coding
    task. Known markers are parsed first. If a future Crew wrapper is detected
    but cannot be parsed, use only a bounded tail for routing rather than
    keyword-scanning the whole system envelope.
    """
    raw = str(value or "")
    if not raw:
        return "", False
    markers = list(_CURRENT_REQUEST.finditer(raw))
    if markers:
        tail = raw[markers[-1].end():]
        blocks = _USER_MESSAGE_BLOCK.findall(tail)
        return ((blocks[-1] if blocks else tail).strip(), True)
    blocks = _USER_MESSAGE_BLOCK.findall(raw)
    if blocks:
        return blocks[-1].strip(), True
    if _WRAPPER_HINT.search(raw):
        # Safe degradation for an unrecognised/new Crew wrapper. The current
        # request is normally at the end; a bounded tail prevents tens of KB of
        # system/memory prose from triggering code/complexity keywords.
        return raw[-2400:].strip(), True
    return raw, False


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        )
    return ""


def _information_units(text: str) -> float:
    """Approximate request information density without assuming whitespace.

    Space-delimited languages contribute word-like units. CJK/Kana/Hangul
    scripts are dense and commonly omit spaces, so roughly two visible script
    characters count as one unit. This is intentionally a *shape* estimate,
    not language detection and not a semantic classifier.
    """
    value = str(text or "")
    dense = len(_DENSE_SCRIPT.findall(value))
    sparse = _DENSE_SCRIPT.sub(" ", value)
    words = len(_WORD.findall(sparse))
    return float(words) + dense / 2.0


def _request_shape_bonus(text: str) -> float:
    """Return a language-resilient difficulty bonus from request structure.

    Task-specific words are intentionally not the primary signal. The same
    multi-clause/structured request should therefore land in roughly the same
    band when written in Chinese, Japanese, English or an unrecognised
    language. Lexical semantic hints remain a small secondary signal below.
    """
    value = str(text or "").strip()
    if not value:
        return 0.0

    units = _information_units(value)
    bonus = 0.0
    if units >= 7:
        bonus += 0.12
    if units >= 18:
        bonus += 0.08
    if units >= 48:
        bonus += 0.08
    if units >= 140:
        bonus += 0.08

    sentence_parts = [part for part in _SENTENCE_BREAK.split(value) if part.strip()]
    if len(sentence_parts) >= 2:
        bonus += 0.08
    if len(sentence_parts) >= 4:
        bonus += 0.06

    joins = len(_CLAUSE_JOIN.findall(value))
    # Japanese polite requests commonly end in ``してください``; counting that
    # single ``して`` as a second clause would inflate simple one-action turns.
    # Two or more occurrences, however, usually include a connective form such
    # as ``レビューして…修正してください``. Keep only the excess occurrence(s).
    joins += max(0, value.count("して") - 1)
    if joins >= 1 and units >= 6:
        bonus += 0.20
    if joins >= 2:
        bonus += 0.06

    list_items = len(_LIST_ITEM.findall(value))
    if list_items >= 2:
        bonus += 0.10
    if list_items >= 5:
        bonus += 0.06

    # Multiple quoted/typed/relational fragments usually mean constraints or
    # structured inputs. One colon alone is too weak to matter.
    marks = len(_STRUCTURED_MARK.findall(value))
    if marks >= 2:
        bonus += 0.05
    if marks >= 6:
        bonus += 0.05
    return min(0.48, bonus)


def _execution_dimensions(
    text: str,
    *,
    complexity: float,
    history_chars: int,
    history_messages: int,
    tools_required: bool,
    code_task: bool,
    estimated_input_tokens: int,
) -> dict[str, float]:
    """Derive orchestration dimensions without a router LLM.

    These values intentionally describe execution shape rather than subject
    matter. They are bounded hints used by policy packs; hard capability gates
    remain elsewhere.
    """
    value = str(text or "")
    units = _information_units(value)
    sentence_count = len([part for part in _SENTENCE_BREAK.split(value) if part.strip()])
    list_items = len(_LIST_ITEM.findall(value))
    joins = len(_CLAUSE_JOIN.findall(value)) + max(0, value.count("して") - 1)

    breadth = 0.08
    if units >= 18:
        breadth += 0.10
    if units >= 48:
        breadth += 0.12
    if sentence_count >= 3:
        breadth += 0.10
    if list_items >= 2:
        breadth += 0.18
    if list_items >= 5:
        breadth += 0.12
    if joins >= 2:
        breadth += 0.10
    breadth_hits = len(_BREADTH_CUE.findall(value))
    if breadth_hits >= 1:
        breadth += 0.22
    if breadth_hits >= 2:
        breadth += 0.12
    if breadth_hits >= 3:
        breadth += 0.08
    inline_separators = value.count("、") + value.count(",") + value.count("，")
    if inline_separators >= 2:
        breadth += 0.14
    if inline_separators >= 5:
        breadth += 0.08
    if history_messages >= 12:
        breadth += 0.06
    breadth = max(0.0, min(1.0, breadth))

    write_intensity = 0.0
    if _WRITE_ACTION.search(value):
        write_intensity += 0.48
    if code_task and tools_required:
        write_intensity += 0.18
    if re.search(r"\b(?:delete|remove|deploy|commit|merge)\b|(?:删除|部署|提交|合并)|(?:削除|デプロイ|コミット|マージ)", value, re.I):
        write_intensity += 0.24
    write_intensity = max(0.0, min(1.0, write_intensity))

    tool_intensity = 0.0
    if tools_required:
        tool_intensity = 0.62
        if _TOOL_ACTION.search(value) and (_TOOL_TARGET.search(value) or _PATH_OR_URL.search(value)):
            tool_intensity += 0.18
        if list_items >= 3 or breadth >= 0.6:
            tool_intensity += 0.10
    tool_intensity = max(0.0, min(1.0, tool_intensity))

    risk = 0.10 + complexity * 0.18 + write_intensity * 0.34
    if _RISK_ACTION.search(value):
        risk += 0.28
    if code_task and complexity >= 0.7:
        risk += 0.08
    risk = max(0.0, min(1.0, risk))

    parallelizability = breadth * 0.70 + tool_intensity * 0.20 - write_intensity * 0.45
    if list_items >= 3:
        parallelizability += 0.12
    parallelizability = max(0.0, min(1.0, parallelizability))

    context_demand = min(1.0, max(estimated_input_tokens / 120000.0, history_chars / 180000.0))
    verification_value = (complexity * 0.42 + risk * 0.34 + write_intensity * 0.18 + (0.06 if code_task else 0.0))
    verification_value = max(0.0, min(1.0, verification_value))

    latency_sensitivity = max(0.0, 0.62 - complexity * 0.38 - risk * 0.12)
    if _LATENCY_CUE.search(value):
        latency_sensitivity += 0.28
    latency_sensitivity = max(0.0, min(1.0, latency_sensitivity))
    return {
        "breadth": breadth,
        "parallelizability": parallelizability,
        "risk": risk,
        "tool_intensity": tool_intensity,
        "write_intensity": write_intensity,
        "context_demand": context_demand,
        "verification_value": verification_value,
        "latency_sensitivity": latency_sensitivity,
    }


def _tool_schema_name(tool: object) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return str(tool.get("name") or "").strip()


def infer_tools_required(
    prompt: str,
    *,
    tools: Sequence[dict] = (),
    history: Sequence[dict] = (),
) -> bool:
    """Infer whether the *current turn* needs an external tool.

    This intentionally does not answer whether tools are installed. It only
    recognizes explicit external actions (web/files/shell/etc.), direct tool
    references, or a short "continue" after an immediately preceding tool
    interaction. Ambiguous analytical requests remain tool-free for routing.
    """
    text, _ = _routing_user_text(str(prompt or ""))
    if not text.strip():
        return False

    folded = text.casefold()
    for tool in tools:
        name = _tool_schema_name(tool)
        if not name:
            continue
        # Exact function-name mentions are a strong structured signal and work
        # regardless of the surrounding natural language.
        token = re.escape(name.casefold())
        if re.search(rf"(?<![a-z0-9_.:-]){token}(?![a-z0-9_.:-])", folded):
            return True

    if _TOOL_INTRINSIC.search(text):
        return True
    if _TOOL_EXPLICIT.search(text) and _TOOL_ACTION.search(text):
        return True
    if _TOOL_ACTION.search(text) and (_TOOL_TARGET.search(text) or _PATH_OR_URL.search(text)):
        return True

    if _CONTINUE_CUE.search(text):
        # Preserve tool continuity only when the recent transcript actually
        # contains tool traffic. A generic "continue" in a prose discussion
        # must not become a tool requirement.
        for message in list(history)[-6:]:
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") == "tool" or message.get("tool_calls"):
                return True
    return False


def build_signals(
    prompt: str,
    history: Sequence[dict],
    *,
    has_image: bool,
    tools_required: bool | None = None,
    tools_available: bool | None = None,
    tools_expected: bool | None = None,
    reasoning_effort: str = "",
) -> RoutingSignals:
    raw_prompt = str(prompt or "")
    text, _wrapped = _routing_user_text(raw_prompt)
    # ``tools_expected`` remains accepted for source compatibility, but its old
    # "tools are installed" meaning is retired. Production callers pass the
    # two explicit dimensions below.
    if tools_required is None:
        tools_required = bool(tools_expected)
    if tools_available is None:
        # Legacy ``tools_expected=True`` implied both dimensions. Keep that
        # behavior only for external/old callers; production code passes the
        # explicit availability bit.
        tools_available = bool(tools_expected)
    history_chars = 0
    raw_history_chars = 0
    history_messages = 0
    for msg in history:
        if not isinstance(msg, dict):
            continue
        content = _message_text(msg.get("content"))
        raw_history_chars += len(content)
        # Provider-private/tool/system traffic is not user-visible task history
        # and should not influence task-complexity routing.
        if str(msg.get("role") or "") not in {"user", "assistant"}:
            continue
        if not content:
            continue
        clean, _ = _routing_user_text(content) if msg.get("role") == "user" else (content, False)
        history_chars += len(clean)
        history_messages += 1
    chars = len(text)
    effort = str(reasoning_effort or "").lower()
    complexity = 0.18
    # Language-resilient request shape is the main short-request signal.  This
    # prevents a substantial Chinese/Japanese/etc. request from being scored as
    # trivial merely because its exact verbs are absent from an English-centric
    # keyword table.
    complexity += _request_shape_bonus(text)
    if chars > 1200:
        complexity += 0.12
    if chars > 5000:
        complexity += 0.12
    if history_chars > 12000:
        complexity += 0.10
    if history_chars > 60000:
        complexity += 0.10
    # Long-running work: many turns already spent on the same task.
    if len(history) > 30:
        complexity += 0.06
    if _COMPLEX.search(text):
        # Semantic vocabulary is only a weak hint now; it must never be the
        # sole reason a task is considered difficult.
        complexity += 0.08
    code_match = bool(_CODE.search(text))
    complex_match = bool(_COMPLEX.search(text))
    if code_match:
        complexity += 0.12
    # A request that is both explicitly analytical/multi-step and code-oriented
    # is materially harder even when no tools are available. This replaces the
    # old false signal where merely *having* tools inflated every request.
    if code_match and complex_match:
        complexity += 0.06
    # Tool *availability* is not evidence that this request is difficult.
    # Only a required tool that is actually available becomes a capability gate;
    # neither dimension inflates task complexity by itself.
    if has_image:
        complexity += 0.08
    if effort in {"high", "xhigh", "max"}:
        complexity += 0.22
    elif effort in {"medium"}:
        complexity += 0.10
    if _SIMPLE.search(text) and chars < 1000:
        complexity -= 0.14
    complexity = max(0.0, min(1.0, complexity))
    # Capability/context gating must still reflect the bytes actually sent to
    # the provider. Only *difficulty heuristics* use the cleaned user view.
    estimated = max(1, math.ceil((len(raw_prompt) + raw_history_chars) / 3.6))
    dimensions = _execution_dimensions(
        text, complexity=complexity, history_chars=history_chars, history_messages=history_messages,
        tools_required=bool(tools_required), code_task=code_match, estimated_input_tokens=estimated,
    )
    base = RoutingSignals(
        prompt_chars=chars,
        history_chars=history_chars,
        history_messages=history_messages,
        has_image=bool(has_image),
        tools_available=bool(tools_available),
        tools_required=bool(tools_required),
        reasoning_effort=effort,
        code_task=code_match,
        complexity=complexity,
        estimated_input_tokens=estimated,
        **dimensions,
    )
    # Task classes are intentionally coarse and deterministic. They are used
    # only to compare this model with its own recent outcomes on similar work;
    # no Router LLM is introduced.
    return RoutingSignals(**{**base.__dict__, "task_profile": task_profile_for_signals(base)})


def _routing_fact_meta(model: ModelSpec, field: str) -> tuple[str, str]:
    for name, source, confidence in getattr(model, "routing_provenance", ()) or ():
        if str(name) == field:
            return str(source or "unknown"), str(confidence or "unknown")
    return (
        str(getattr(model, "routing_source", "unknown") or "unknown"),
        str(getattr(model, "routing_confidence", "unknown") or "unknown"),
    )


def _hint(model: ModelSpec, name: str, default: float) -> float:
    value = getattr(model, name, None)
    if isinstance(value, (int, float)):
        bounded = max(0.0, min(1.0, float(value)))
        field = name.removeprefix("routing_")
        _source, confidence = _routing_fact_meta(model, field)
        if confidence == "low":
            # Low-confidence metadata (typically heuristic/operator-imported)
            # nudges ranking but cannot override a neutral prior as strongly as
            # live/curated facts. Medium/high confidence remains undamped.
            return default + (bounded - default) * 0.50
        return bounded
    return default


def _model_priors(model: ModelSpec) -> tuple[float, float, float, list[str]]:
    label = f"{model.name} {model.model_id}"
    quality, speed, economy = 0.58, 0.58, 0.58
    reasons: list[str] = []
    if _FAST.search(label):
        speed += 0.25; economy += 0.24; quality -= 0.08
        reasons.append("low-confidence fast name prior")
    if _STRONG.search(label):
        quality += 0.25; speed -= 0.10; economy -= 0.10
        reasons.append("low-confidence quality name prior")
    if _BALANCED.search(label):
        quality += 0.08; speed += 0.04; economy += 0.03
        reasons.append("low-confidence balanced name prior")
    if model.reasoning_supported is True:
        quality += 0.08
        reasons.append("reasoning capable")
    if len(model.effort_supported_levels) >= 3:
        quality += 0.05
    if model.context_window:
        if model.context_window >= 1_000_000:
            quality += 0.04
        elif model.context_window < 64_000:
            quality -= 0.04
    routing_source = str(getattr(model, "routing_source", "unknown") or "unknown")
    routing_confidence = str(getattr(model, "routing_confidence", "unknown") or "unknown")
    if any(isinstance(value, (int, float)) for value in (model.routing_quality, model.routing_speed, model.routing_economy)):
        reasons.append(f"routing metadata {routing_source}/{routing_confidence}")
    return (
        _hint(model, "routing_quality", max(0.0, min(1.0, quality))),
        _hint(model, "routing_speed", max(0.0, min(1.0, speed))),
        _hint(model, "routing_economy", max(0.0, min(1.0, economy))),
        reasons,
    )


def _eligible(model: ModelSpec, signals: RoutingSignals) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    modalities = {str(x).lower() for x in model.input_modalities}
    if signals.has_image and modalities and "image" not in modalities:
        return False, ["image input unsupported"]
    if signals.tools_required and signals.tools_available and model.tool_use is False:
        return False, ["tool use unsupported"]
    if model.context_window and signals.estimated_input_tokens > int(model.context_window * 0.92):
        return False, ["context window too small"]
    # Low-level callers may still supply an explicit effort as a hard capability
    # constraint. Since 0.1.5 the normal Tiga Type UI never does this: Types own
    # effort internally and choose it only after routing to a concrete model.
    # Keeping this gate makes the router safe for diagnostics/custom callers.
    if signals.reasoning_effort:
        requested = str(signals.reasoning_effort).lower()
        supported = {str(x).lower() for x in model.effort_supported_levels}
        fixed = str(model.reasoning_fixed_level or "").lower()
        if requested not in supported and requested != fixed:
            return False, ["requested effort unsupported"]
        reasons.append("requested effort supported")
    if signals.has_image and "image" in modalities:
        reasons.append("image capable")
    if signals.tools_required and signals.tools_available and model.tool_use is True:
        reasons.append("tool capable")
    return True, reasons


def model_tiers(model: ModelSpec) -> tuple[str, ...]:
    """Routing roles a model may serve: fast / balanced / strong.

    Roles are many-to-many. A model can legitimately be both strong+balanced or
    strong+fast; Tiga Types consume pools of models and are never bound to one
    concrete model. Explicit operator metadata wins. Generic inference exists
    only as a backwards-compatible fallback for old/custom catalogues.
    """
    manual = str(getattr(model, "tier", "") or "").strip().lower()
    if manual in {"fast", "balanced", "strong"}:
        return (manual,)

    explicit: list[str] = []
    for raw in getattr(model, "routing_tiers", ()) or ():
        tier = str(raw or "").strip().lower()
        if tier in {"fast", "balanced", "strong"} and tier not in explicit:
            explicit.append(tier)
    if explicit:
        return tuple(explicit)

    q, s, e = model.routing_quality, model.routing_speed, model.routing_economy
    hints = [value for value in (q, s, e) if isinstance(value, (int, float))]
    inferred: list[str] = []
    if hints:
        quality = float(q) if isinstance(q, (int, float)) else 0.58
        speed = float(s) if isinstance(s, (int, float)) else 0.58
        economy = float(e) if isinstance(e, (int, float)) else 0.58
        fast_score = (speed + economy) / 2
        if quality >= 0.80:
            inferred.append("strong")
        if fast_score >= 0.78:
            inferred.append("fast")
        # Balanced means a useful quality/latency/cost compromise, not merely
        # "we could not classify it". A near-flagship value model may therefore
        # be both strong and balanced.
        if (quality >= 0.68 and fast_score >= 0.60) or not inferred:
            inferred.append("balanced")
        return tuple(dict.fromkeys(inferred))

    label = f"{model.name} {model.model_id}"
    if _FAST.search(label):
        inferred.append("fast")
    if _STRONG.search(label):
        inferred.append("strong")
    if not inferred or _BALANCED.search(label):
        inferred.append("balanced")
    return tuple(dict.fromkeys(inferred))


def model_tier(model: ModelSpec) -> str:
    """Primary routing role for backwards-compatible labels/debug output."""
    return model_tiers(model)[0]


def model_in_tier(model: ModelSpec, pool: str) -> bool:
    pool = str(pool or "any").strip().lower()
    return pool == "any" or pool in model_tiers(model)


def _in_pool(model: ModelSpec, pool: str) -> bool:
    return model_in_tier(model, pool)


def _reasoning_alignment(
    policy: ExecutionPolicy | ExecutionIntent, model: ModelSpec, signals: RoutingSignals,
    *, role: str = "lead", stage: str = "planning", routing: RoutingProfile | None = None,
) -> tuple[float, list[str]]:
    """Small score adjustment for the user's per-model Thinking preference.

    The preference itself remains a hard per-model lock. This only stops a Type
    whose character clearly wants thinking ON/OFF from needlessly preferring a
    peer model that the user has locked in the opposite state. It is deliberately
    a small tie-breaker: capabilities and the Type's quality/speed/economy goal
    still dominate routing.
    """
    profile = getattr(policy, "auto_reasoning", None)
    strategy = str(getattr(profile, "thinking", "inherit") or "inherit")
    context = ReasoningInvocation(
        complexity=float(getattr(signals, "complexity", 0.0) or 0.0),
        code_task=bool(getattr(signals, "code_task", False)),
        tools_expected=bool(getattr(signals, "tools_required", False)),
        has_image=bool(getattr(signals, "has_image", False)),
        role=role,
        stage=stage,
    )
    suggested = thinking_auto_suggestion(strategy, context)
    if suggested not in {"on", "off"}:
        return 0.0, []
    effective = resolve_thinking_mode(
        model, getattr(model, "thinking_preference", "auto"), suggested
    )
    if not effective:
        return 0.0, []
    fit = getattr(routing or getattr(policy, "routing", None), "fit", None)
    if effective == suggested:
        bonus = (
            float(getattr(fit, "thinking_on_align_bonus", 0.035))
            if suggested == "on" else float(getattr(fit, "thinking_off_align_bonus", 0.025))
        )
        return bonus, [f"thinking preference aligns with {suggested}"]
    penalty = (
        float(getattr(fit, "thinking_on_conflict_penalty", 0.050))
        if suggested == "on" else float(getattr(fit, "thinking_off_conflict_penalty", 0.035))
    )
    return -penalty, [f"thinking preference locked {effective}"]


def _weights_from_routing(routing: RoutingProfile, complexity: float) -> tuple[float, float, float]:
    q = float(routing.quality)
    s = float(routing.speed)
    e = float(routing.economy)
    shift = routing.complexity_shift
    if shift.mode == "adaptive":
        delta = (complexity - float(getattr(shift, "center", 0.5))) * shift.strength
        q += delta
        s -= delta * float(getattr(shift, "speed_share", 0.55))
        e -= delta * float(getattr(shift, "economy_share", 0.45))
    elif shift.mode == "threshold" and complexity > shift.threshold:
        q += shift.quality_add
        # Keep the original speed/economy ratio while buying more quality.
        rest = max(1e-9, s + e)
        s -= shift.quality_add * (s / rest)
        e -= shift.quality_add * (e / rest)
    q, s, e = max(0.0, q), max(0.0, s), max(0.0, e)
    total = q + s + e
    if total <= 0:
        return (1.0, 0.0, 0.0)
    return q / total, s / total, e / total


def _weights(policy: ExecutionPolicy | ExecutionIntent, complexity: float) -> tuple[float, float, float]:
    return _weights_from_routing(policy.routing, complexity)


def _adaptive_strengths(routing: RoutingProfile, q_w: float, s_w: float) -> tuple[float, float, int]:
    profile = routing.adaptive
    if not profile.enabled:
        return 0.0, 0.0, int(profile.history_limit)
    speed_alpha = profile.speed_alpha_base + profile.speed_alpha_speed_weight * s_w
    speed_alpha = max(profile.speed_alpha_min, min(profile.speed_alpha_max, speed_alpha))
    reliability = profile.reliability_base + profile.reliability_quality_weight * q_w
    reliability = max(profile.reliability_min, min(profile.reliability_max, reliability))
    return float(speed_alpha), float(reliability), int(profile.history_limit)


def _score_with_weights(row: CandidateScore, weights: ScoreWeights) -> float:
    return (
        float(weights.quality) * row.quality
        + float(weights.speed) * row.speed
        + float(weights.economy) * row.economy
        + float(weights.reliability) * row.reliability
    )

def route_model(
    config: KiroByoConfig,
    *,
    policy: str | None = None,
    pack: str | None = None,
    prompt: str,
    history: Sequence[dict],
    has_image: bool,
    tools_required: bool | None = None,
    tools_available: bool | None = None,
    tools_expected: bool | None = None,
    reasoning_effort: str = "",
) -> RouteDecision | None:
    signals = build_signals(
        prompt, history, has_image=has_image, tools_required=tools_required,
        tools_available=tools_available, tools_expected=tools_expected,
        reasoning_effort=reasoning_effort,
    )
    pack_id, pack_name, policy_definition = _resolved_policy(pack or config.mode_pack, policy or config.mode_type)
    policy_id = policy_definition.id
    policy_label = policy_definition.display_name
    policy_def = policy_definition.execution
    intent = compile_execution_intent(policy_definition)
    q_w, s_w, e_w = _weights(intent, signals.complexity)
    speed_alpha_cap, reliability_strength, history_limit = _adaptive_strengths(intent.routing, q_w, s_w)
    adaptive_profile = intent.routing.adaptive
    adaptive = current_snapshot(
        task_profile=signals.task_profile,
        limit=history_limit,
        profile=adaptive_profile,
    )
    # Runtime blending intensity belongs to policy data. The Router Core only
    # applies the generic blend/clamp algorithm.
    ranked: list[CandidateScore] = []
    exclusions: list[tuple[str, str, str]] = []
    for provider in config.providers:
        for model in provider.models:
            ok, reasons = _eligible(model, signals)
            if not ok:
                exclusions.append((provider.id, model.model_id, "; ".join(reasons)))
                continue
            quality, static_speed, economy, prior_reasons = _model_priors(model)
            reasons.extend(prior_reasons)
            evidence = adaptive.for_model(provider.id, model.model_id)
            speed = blend_speed(
                static_speed, evidence, max_alpha=speed_alpha_cap, profile=adaptive_profile
            )
            fit = reliability_adjustment(
                evidence, strength=reliability_strength, profile=adaptive_profile
            )
            if evidence.samples:
                reasons.append(f"runtime evidence {evidence.samples} turns/{evidence.confidence}")
                if evidence.runtime_speed is not None:
                    reasons.append("recent latency blended")
                if evidence.task_samples:
                    reasons.append(f"{signals.task_profile} history {evidence.task_samples}")
            fit_profile = intent.routing.fit
            if signals.reasoning_effort in {"high", "xhigh", "max"}:
                if model.reasoning_supported is True:
                    fit += fit_profile.deep_reasoning_bonus
                    reasons.append("requested deep reasoning")
                elif model.reasoning_supported is False:
                    fit -= fit_profile.reasoning_unsupported_penalty
            if signals.reasoning_effort:
                # Eligibility above already guarantees exact vocabulary support.
                fit += fit_profile.effort_match_bonus
            if signals.code_task and model.tool_use is True:
                fit += fit_profile.code_tool_bonus
            # Form-specific capability preferences (data-driven, no model names).
            # Long work needs room: favour large windows in proportion to how
            # much of the conversation is already history.
            context_bias = float(getattr(intent.routing, "context_bias", 0.0) or 0.0)
            if context_bias and model.context_window:
                window_score = min(1.0, math.log10(max(model.context_window, 1)) / 6.0)
                fit += context_bias * window_score
                if window_score >= 0.9:
                    reasons.append("long-context fit")
            tool_bias = float(getattr(intent.routing, "tool_bias", 0.0) or 0.0)
            if tool_bias and signals.tools_required and signals.tools_available:
                if model.tool_use is True:
                    fit += tool_bias
                    reasons.append("reliable tool use")
                elif model.tool_use is None:
                    fit -= tool_bias * 0.5
            # Unknown capability is allowed but never preferred over a verified fit.
            if signals.has_image and not model.input_modalities:
                fit -= fit_profile.image_unknown_penalty
                reasons.append("image capability unknown")
            if signals.tools_required and signals.tools_available and model.tool_use is None:
                fit -= fit_profile.tool_unknown_penalty
                reasons.append("tool capability unknown")
            alignment, alignment_reasons = _reasoning_alignment(
                intent, model, signals, role="lead", stage="planning"
            )
            fit += alignment
            reasons.extend(alignment_reasons)
            score = q_w * quality + s_w * speed + e_w * economy + fit
            ranked.append(CandidateScore(
                provider, model, score, quality, speed, economy, tuple(reasons),
                reliability=evidence.reliability, runtime_samples=evidence.samples,
                runtime_confidence=evidence.confidence, static_speed=static_speed,
                runtime_speed=evidence.runtime_speed,
            ))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row.score, row.provider.id, row.model.model_id))
    # Form pool: Sky stays in the fast tier, Power in the strong tier. In-tier
    # models always come first (for the winner AND for provider-error
    # fallbacks); an out-of-tier model is used only when no in-tier model can
    # run this turn (e.g. image input, context size).
    pool = str(getattr(intent.routing, "tier_pool", "any") or "any")
    selection_pool = str(getattr(intent.routing, "primary_tier_pool", "") or pool)
    outside_pool = False
    execution_ranked = list(ranked)
    if selection_pool != "any":
        in_pool = [row for row in ranked if _in_pool(row.model, selection_pool)]
        out_pool = [row for row in ranked if not _in_pool(row.model, selection_pool)]
        if in_pool:
            # Keep out-of-tier rows in ``candidates`` for independent review and
            # Glitter teammate discovery, but never use them for a transient
            # ProviderError fallback.
            ranked = in_pool + out_pool
            execution_ranked = list(in_pool)
        else:
            fallback_mode = str(getattr(intent.routing, "tier_fallback", "capability") or "capability")
            if fallback_mode == "strict":
                # The user selected a Type whose identity depends on this tier.
                # Do not silently turn Power/Glitter-like policies into another
                # Type just because a different tier happens to be capable.
                return None
            outside_pool = True
            fallback_weights = intent.routing.fallback_scoring
            if fallback_weights is not None:
                ranked = sorted(
                    out_pool,
                    key=lambda r: (-_score_with_weights(r, fallback_weights), r.provider.id, r.model.model_id),
                )
            elif selection_pool == "fast":
                # Compatibility behaviour for older packs that do not declare
                # fallback scoring in data. Built-in Tiga v3 does declare it.
                ranked = sorted(out_pool, key=lambda r: (-(0.55 * r.speed + 0.45 * r.economy), r.provider.id, r.model.model_id))
            else:
                ranked = sorted(out_pool, key=lambda r: (-r.quality, -r.score, r.provider.id, r.model.model_id))
            execution_ranked = list(ranked)
    winner = ranked[0]
    reason = [
        f"{pack_name} / {policy_label} policy",
        f"task complexity {signals.complexity:.2f} ({signals.task_profile})",
        f"weights quality={q_w:.2f} speed={s_w:.2f} economy={e_w:.2f}",
        f"adaptive runtime evidence {adaptive.turns} recent observations / {adaptive.observed_models} models",
    ]
    if selection_pool != "any":
        reason.append(
            f"no {selection_pool}-tier model can run this turn; outside-tier fallback"
            if outside_pool else f"{selection_pool}-tier pool"
        )
    if signals.tools_required:
        reason.append(
            "external tool required and available"
            if signals.tools_available else "external tool required but unavailable"
        )
    reason.extend(winner.reasons[:4])
    return RouteDecision(
        pack_id, pack_name, policy_id, policy_label, policy_def, intent,
        winner.provider, winner.model, signals, tuple(ranked), tuple(reason),
        tuple(execution_ranked), tuple(exclusions),
    )


def route_diagnostics(decision: RouteDecision, *, served_provider: str = "", served_model: str = "") -> dict:
    """Compact, content-free explanation attached to a *completed* turn.

    Mode/Type is the user-selected strategy. Provider/model identifies the
    actual resource consumed. Retain both; never imply varied model usage is
    desirable in its own right. No prompt text, tokens or tool payloads here.
    """
    routing = decision.execution_intent.routing
    tier = str(routing.primary_tier_pool or routing.tier_pool or "any")
    pool_rows = [r for r in decision.candidates if tier == "any" or model_in_tier(r.model, tier)]
    selected_id = (decision.provider.id, decision.model.model_id)
    served_id = (served_provider or selected_id[0], served_model or selected_id[1])
    # Decision.candidates is ranked by policy. Sticky session pins can select a
    # lower-ranked eligible candidate; provider failover may then serve another.
    source = ("provider_fallback" if served_id != selected_id else decision.selection_source)
    candidates = [
        {"provider_id": row.provider.id, "model": row.model.model_id,
         "name": row.model.name, "score": round(row.score, 5),
         "quality": round(row.quality, 4),
         "speed": round(row.speed, 4),
         "economy": round(row.economy, 4),
         "runtime_samples": row.runtime_samples,
         "runtime_confidence": row.runtime_confidence,
         "tier_match": tier == "any" or model_in_tier(row.model, tier),
         "selected": (row.provider.id, row.model.model_id) == selected_id,
         "served": (row.provider.id, row.model.model_id) == served_id}
        for row in decision.candidates[:12]
    ]
    return {
        "pack": decision.pack, "policy": decision.policy,
        "policy_label": decision.policy_label,
        "selection_source": source,
        "primary_tier": tier, "eligible_models": len(decision.candidates),
        "in_tier_models": len(pool_rows),
        "excluded_models": [
            {"provider_id": provider_id, "model": model_id, "reason": reason}
            for provider_id, model_id, reason in decision.exclusions[:12]
        ],
        "selected_model": {"provider_id": selected_id[0], "model": selected_id[1]},
        "served_model": {"provider_id": served_id[0], "model": served_id[1]},
        "reasons": list(decision.reason[:8]),
        "candidates": candidates,
    }


def route_collaboration_model(
    config: KiroByoConfig,
    policy: ExecutionPolicy | ExecutionIntent,
    signals: RoutingSignals,
    *,
    leader_provider: ProviderSpec,
    leader_model: ModelSpec,
    excluded_models: set[tuple[str, str]] | None = None,
) -> CandidateScore | None:
    """Route one relay/worker invocation from the *current subtask* signals.

    This is intentionally separate from the leader candidate order. A worker is
    rescored for the concrete gathering subtask every time it is invoked, using
    speed, economy, quality, tool capability, context and the user's per-model
    Thinking preference. Provider affinity is only a small continuity bonus,
    never a hard restriction. If the requested collaboration tier has no
    eligible model, the lead simply keeps control.
    """
    orchestration = getattr(policy, "orchestration", None)
    tier_pool = str(
        getattr(orchestration, "worker_tier_pool", "")
        or getattr(policy, "collaboration_tier_pool", "fast") or "fast"
    )
    complexity = max(0.0, min(1.0, float(getattr(signals, "complexity", 0.0) or 0.0)))
    worker_routing = getattr(policy, "collaboration_routing", None)
    if worker_routing is not None:
        q_w, s_w, e_w = _weights_from_routing(worker_routing, complexity)
        tier_pool = str(worker_routing.primary_tier_pool or worker_routing.tier_pool or tier_pool)
        speed_alpha_cap, reliability_strength, history_limit = _adaptive_strengths(worker_routing, q_w, s_w)
        worker_fit = worker_routing.fit
    else:
        # Orchestration v4 can route workers without a second full RoutingProfile.
        # Older relay packs retain the historical fast-worker formula.
        objective = str(getattr(orchestration, "objective", "") or "").lower()
        if objective == "latency":
            q_w, s_w, e_w = 0.18, 0.52, 0.30
            speed_alpha_cap, reliability_strength = 0.72, 0.16
        elif objective == "quality":
            q_w, s_w, e_w = 0.76, 0.14, 0.10
            speed_alpha_cap, reliability_strength = 0.38, 0.30
        elif objective == "capability":
            q_w, s_w, e_w = 0.68, 0.18, 0.14
            speed_alpha_cap, reliability_strength = 0.42, 0.28
        elif objective == "value":
            q_w, s_w, e_w = 0.48, 0.28, 0.24
            speed_alpha_cap, reliability_strength = 0.54, 0.24
        else:
            q_w = 0.22 + 0.30 * complexity
            s_w = 0.46 - 0.14 * complexity
            e_w = max(0.0, 1.0 - q_w - s_w)
            speed_alpha_cap = 0.66
            reliability_strength = 0.18 + 0.14 * q_w
        history_limit = 500
        worker_fit = getattr(getattr(policy, "routing", None), "fit", None)
    worker_adaptive = worker_routing.adaptive if worker_routing is not None else policy.routing.adaptive
    adaptive = current_snapshot(
        task_profile=getattr(signals, "task_profile", "") or task_profile_for_signals(signals),
        limit=history_limit,
        profile=worker_adaptive,
    )
    ranked: list[CandidateScore] = []
    excluded = set(excluded_models or ())
    for provider in config.providers:
        for model in provider.models:
            if (provider.id, model.model_id) == (leader_provider.id, leader_model.model_id):
                continue
            if (provider.id, model.model_id) in excluded:
                continue
            if tier_pool != "any" and not _in_pool(model, tier_pool):
                continue
            ok, reasons = _eligible(model, signals)
            if not ok:
                continue
            quality, static_speed, economy, prior_reasons = _model_priors(model)
            reasons.extend(prior_reasons)
            evidence = adaptive.for_model(provider.id, model.model_id)
            speed = blend_speed(
                static_speed, evidence, max_alpha=speed_alpha_cap, profile=worker_adaptive
            )
            fit = reliability_adjustment(
                evidence, strength=reliability_strength, profile=worker_adaptive
            )
            if evidence.samples:
                reasons.append(f"worker runtime evidence {evidence.samples} turns/{evidence.confidence}")
            worker_tool_bias = float(getattr(worker_routing, "tool_bias", 0.08) if worker_routing is not None else 0.08)
            if signals.tools_required and signals.tools_available:
                if model.tool_use is True:
                    fit += worker_tool_bias
                    reasons.append("worker tool capability verified")
                elif model.tool_use is None:
                    fit -= float(getattr(worker_fit, "tool_unknown_penalty", 0.06))
                    reasons.append("worker tool capability unknown")
            if signals.code_task:
                fit += float(getattr(worker_fit, "code_quality_bonus", 0.04)) * quality
            if model.context_window:
                needed = max(1, int(getattr(signals, "estimated_input_tokens", 1) or 1))
                headroom = min(1.0, float(model.context_window) / float(max(needed * 2, 1)))
                fit += float(getattr(worker_fit, "context_headroom_bonus", 0.025)) * headroom
            if provider.id == leader_provider.id:
                # Continuity/cache/message-shape locality is useful, but a much
                # better worker from another provider is allowed to win.
                fit += float(getattr(policy, "collaboration_provider_affinity", 0.025))
                reasons.append("leader-provider affinity")
            alignment, alignment_reasons = _reasoning_alignment(
                policy, model, signals, role="worker", stage="gathering", routing=worker_routing
            )
            fit += alignment
            reasons.extend(alignment_reasons)
            score = q_w * quality + s_w * speed + e_w * economy + fit
            ranked.append(CandidateScore(
                provider, model, score, quality, speed, economy, tuple(reasons),
                reliability=evidence.reliability, runtime_samples=evidence.samples,
                runtime_confidence=evidence.confidence, static_speed=static_speed,
                runtime_speed=evidence.runtime_speed,
            ))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row.score, row.provider.id, row.model.model_id))
    return ranked[0]


def _independent_candidate(
    decision: RouteDecision, *, objective: str, reviewer: str = "any",
    scoring: ScoreWeights | None = None, prefer_different_provider: bool = True,
) -> CandidateScore | None:
    alternatives = [
        row for row in decision.candidates
        if row.provider.id != decision.provider.id or row.model.model_id != decision.model.model_id
    ]
    primary = next(
        (
            row for row in decision.candidates
            if row.provider.id == decision.provider.id and row.model.model_id == decision.model.model_id
        ),
        decision.candidates[0] if decision.candidates else None,
    )
    if reviewer == "not_weaker" and primary is not None:
        # A weaker model must never check (and possibly rewrite) a stronger
        # model's answer. Without a peer, the primary reviews its own draft.
        alternatives = [row for row in alternatives if row.quality >= primary.quality]
        if not alternatives:
            return primary
    pool_name = str(getattr(decision.execution_intent.routing, "tier_pool", "any") or "any")
    if pool_name != "any":
        in_pool = [row for row in alternatives if _in_pool(row.model, pool_name)]
        if in_pool:
            alternatives = in_pool
        elif primary is not None:
            # Tier-locked review never borrows another out-of-tier model. If a
            # Sky/Power primary itself is a capability fallback, self-review is
            # the only allowed outside-tier verification.
            return primary
    if primary is not None and prefer_different_provider:
        # Prefer a reviewer from a different provider: independent training
        # and serving stacks catch errors a sibling model would share.
        foreign = [row for row in alternatives if row.provider.id != primary.provider.id]
        if foreign:
            alternatives = foreign
    pool = alternatives or list(decision.candidates[:1])
    if not pool:
        return None
    if scoring is not None:
        return max(pool, key=lambda x: (_score_with_weights(x, scoring), x.score, x.model.context_window or 0))
    # Compatibility presets for schema-v1/v2 custom packs. Built-in Tiga v3
    # supplies explicit scoring and therefore never relies on these constants.
    if objective == "economy":
        return max(pool, key=lambda x: (0.40*x.speed + 0.34*x.economy + 0.16*x.quality + 0.10*x.reliability, x.score))
    if objective == "value":
        return max(pool, key=lambda x: (0.47*x.quality + 0.21*x.speed + 0.20*x.economy + 0.12*x.reliability, x.score))
    return max(pool, key=lambda x: (0.82*x.quality + 0.18*x.reliability, x.score, x.model.context_window or 0))


def _secondary_triggered(
    policy: ExecutionPolicy | ExecutionIntent, signals: RoutingSignals, profile: TaskProfile | None = None
) -> bool:
    secondary = policy.secondary
    if secondary is None:
        return False
    trigger = secondary.trigger
    if trigger.always:
        return True
    profile = profile or TaskProfile.from_signals(signals)
    checks = []
    if trigger.complexity_gte is not None:
        checks.append(profile.complexity >= trigger.complexity_gte)
    if getattr(trigger, "breadth_gte", None) is not None:
        checks.append(profile.breadth >= trigger.breadth_gte)
    if getattr(trigger, "risk_gte", None) is not None:
        checks.append(profile.risk >= trigger.risk_gte)
    if getattr(trigger, "verification_value_gte", None) is not None:
        checks.append(profile.verification_value >= trigger.verification_value_gte)
    if getattr(trigger, "write_intensity_gte", None) is not None:
        checks.append(profile.write_intensity >= trigger.write_intensity_gte)
    if trigger.code:
        checks.append(signals.code_task)
    if trigger.tools:
        checks.append(signals.tools_required)
    if trigger.code_and_tools:
        checks.append(signals.code_task and signals.tools_required)
    if trigger.reasoning_any:
        checks.append(signals.reasoning_effort in set(trigger.reasoning_any))
    return any(checks) if checks else False


def _worker_enabled(policy: ExecutionIntent, profile: TaskProfile) -> bool:
    orchestration = policy.orchestration
    mode = str(orchestration.worker_mode or "none")
    if mode == "none" or orchestration.max_agents < 2:
        return False
    if mode == "always":
        return True
    # Adaptive delegation is intentionally conservative in this foundation
    # release. It only admits an isolated read-only helper when task shape says
    # there is broad gathering work and little mutation risk.
    return (
        profile.breadth >= orchestration.worker_breadth_gte
        and profile.parallelizability >= orchestration.worker_parallelizability_gte
        and profile.tool_intensity >= orchestration.worker_tool_intensity_gte
        and profile.write_intensity <= orchestration.worker_write_intensity_lte
    )


def build_execution_plan(decision: RouteDecision) -> ExecutionPlan:
    """Compile routing evidence into a product-neutral Agent Runtime plan.

    The planner never branches on Tiga/Sky/Multi/Power/Glitter names. Policy
    data sets the objective and orchestration budget; task shape decides whether
    optional worker/reviewer roles create enough value to exist.
    """
    policy = decision.execution_intent
    profile = TaskProfile.from_signals(decision.signals)
    orchestration = policy.orchestration
    secondary = policy.secondary
    target: ReviewTarget | None = None
    rationale: list[str] = [f"{decision.pack_label} / {decision.policy_label} execution policy"]

    worker = _worker_enabled(policy, profile)
    if worker:
        rationale.append(
            f"read-only worker admitted: breadth={profile.breadth:.2f}, "
            f"parallelizability={profile.parallelizability:.2f}, tools={profile.tool_intensity:.2f}"
        )

    if secondary is not None and _secondary_triggered(policy, decision.signals, profile):
        row = _independent_candidate(
            decision,
            objective=secondary.objective,
            reviewer=getattr(secondary, "reviewer", "any"),
            scoring=getattr(secondary, "scoring", None),
            prefer_different_provider=bool(getattr(secondary, "prefer_different_provider", True)),
        )
        if row is not None:
            same = row.provider.id == decision.provider.id and row.model.model_id == decision.model.model_id
            reason = secondary.self_reason if same else secondary.reason
            target = ReviewTarget(
                row.provider, row.model, reason, secondary.role, secondary.reasoning_intent,
                decision.pack, decision.pack_label, decision.policy, decision.policy_label,
                bool(getattr(secondary, "requires_substantive_work", False)),
            )
            rationale.append(secondary.rationale)

    if secondary is None:
        mode = "single_pass"
    else:
        mode = secondary.multi_mode if target is not None else secondary.single_mode

    agents: list[AgentSpec] = [AgentSpec(
        role="lead",
        name="lead",
        objective=str(orchestration.objective or "value"),
        model_policy="primary",
        tier_pool=str(policy.routing.primary_tier_pool or policy.routing.tier_pool or "any"),
        tool_policy="full",
        authority="writer",
        reasoning_intent=policy.primary_reasoning,
        sticky_model=True,
        required=True,
        context_policy="shared_parent",
    )]
    worker_count = 0
    if worker:
        # Parallel children are only useful when the task is actually splittable.
        # A narrow task keeps one helper even when the policy's hard budget is
        # larger. Reviewer/lead slots count against max_agents so the plan can
        # never silently exceed its declared orchestration budget.
        reserved = 1 + (1 if target is not None else 0)
        worker_budget = max(1, int(orchestration.max_agents) - reserved)
        wants_parallel = (
            profile.parallelizability >= 0.62
            and profile.breadth >= 0.58
            and profile.write_intensity <= 0.28
        )
        worker_count = min(
            worker_budget,
            max(1, int(orchestration.max_parallel) if wants_parallel else 1),
        )
        if worker_count > 1:
            rationale.append(f"parallel read-only subagents admitted: {worker_count}")
        worker_objectives = (
            "gather primary evidence and unresolved implementation facts",
            "independently inspect alternate evidence, edge cases and contradictions",
            "inspect cross-cutting dependencies and missing evidence",
            "stress-test assumptions and look for integration or regression risks",
        )
        for index in range(worker_count):
            objective_suffix = worker_objectives[min(index, len(worker_objectives) - 1)]
            agents.append(AgentSpec(
                role="worker",
                name=f"worker-{index + 1}",
                objective=f"{str(orchestration.objective or 'value')}: {objective_suffix}",
                model_policy="collaboration",
                tier_pool=str(orchestration.worker_tier_pool or "fast"),
                tool_policy="read_only",
                authority="assist",
                reasoning_intent=str(orchestration.worker_reasoning_intent or "economy"),
                sticky_model=True,
                required=False,
                max_turns=6,
                context_policy="isolated_snapshot",
            ))
    if target is not None:
        agents.append(AgentSpec(
            role=target.role,
            name=target.role,
            objective=secondary.objective if secondary is not None else "quality",
            model_policy="secondary",
            tier_pool=str(policy.routing.tier_pool or "any"),
            tool_policy="none",
            authority="review",
            reasoning_intent=target.reasoning_intent,
            sticky_model=True,
            required=bool(target.role == "review"),
        ))

    topology = "single"
    if worker_count > 1 and target is not None:
        topology = "leader-parallel-workers-review"
    elif worker_count > 1:
        topology = "leader-parallel-workers"
    elif worker and target is not None:
        topology = "leader-worker-review"
    elif worker:
        topology = "leader-worker"
    elif target is not None:
        topology = "lead-review"
    return ExecutionPlan(
        decision=decision, mode=mode, post_stage=target, rationale=tuple(rationale),
        task_profile=profile, agents=tuple(agents), topology=topology,
        max_agents=max(1, int(orchestration.max_agents)),
        max_parallel=max(1, int(orchestration.max_parallel)),
        max_depth=max(1, int(orchestration.max_depth)),
    )


def glitter_review_target(decision: RouteDecision) -> ReviewTarget | None:
    """Legacy helper: return the secondary target only when its generic role is review."""
    target = build_execution_plan(decision).post_stage
    return target if target is not None and target.role == "review" else None

