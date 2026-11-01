#!/usr/bin/env python3
"""Credential-free Tiga decision and execution-plan audit (not a quality benchmark).

Run with `python tiga_suite.py <project-directory>`. No network or external accounts.
The fixture models are synthetic; this does NOT execute any Provider or tool.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import os
import sys

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
sys.path.insert(0, str(ROOT / "src"))
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import build_execution_plan, route_model, model_in_tier

TYPES = ("sky", "multi", "power", "glitter")

@dataclass(frozen=True)
class Case:
    key: str
    group: str
    prompt: str
    image: bool = False
    tools: bool = False
    history: str = ""
    effort: str = ""

# 32 independent scenarios: no grade is inferred from the text or the chosen model.
CASES = (
    Case("greeting_zh", "simple", "你好"),
    Case("greeting_en", "simple", "Hello!"),
    Case("greeting_ja", "simple", "こんにちは"),
    Case("greeting_ko", "simple", "안녕하세요"),
    Case("short_fact", "simple", "请简短解释什么是电阻。"),
    Case("short_summary", "simple", "一句话总结这个概念：负反馈。"),
    Case("quick_calculation", "simple", "计算 17 × 19，直接给结果。"),
    Case("short_translation", "simple", "Translate this sentence to Japanese: Good morning."),
    Case("code_explain", "code", "解释这段 Python 代码：```python\nprint(sum(range(10)))\n```"),
    Case("code_bug", "code", "修复 Python 函数的异常处理 bug，补充一个回归测试。", tools=True),
    Case("code_refactor", "code", "重构三个模块，确保公开 API 不变并补充自动化测试。", tools=True),
    Case("code_security", "code", "全面审计权限检查、凭据处理和数据库事务，并验证失败回滚。", tools=True),
    Case("code_review_en", "code", "Audit several modules, identify regression risks and propose tests.", tools=True),
    Case("code_review_ja", "code", "複数のモジュールを監査し、問題を修正して回帰テストを追加してください。", tools=True),
    Case("code_review_ko", "code", "여러 모듈을 검토하고 문제를 수정하며 회귀 테스트를 작성해 주세요.", tools=True),
    Case("write_script", "code", "写一个安全处理日志的 Bash 脚本并解释边界条件。", tools=True),
    Case("research_brief", "research", "比较三种数据库的优缺点并给出选择依据。"),
    Case("research_deep", "research", "比较所有候选架构、分析权衡、设计实验、验证假设并形成报告。"),
    Case("project_multi", "research", "全面审查整个代码仓库，调查多处缺陷，设计补丁并运行测试。", tools=True),
    Case("project_long", "research", "请审查所有模块和接口，逐项验证行为，输出完整迁移方案。", tools=True, history="历史上下文。" * 9000),
    Case("high_risk_delete", "risk", "删除生产数据库中的重复记录；必须先解释回滚方案。", tools=True),
    Case("high_risk_secrets", "risk", "检查生产环境的密钥、权限与审计记录，不要擅自写入。", tools=True),
    Case("ops_deploy", "risk", "制定部署和安全检查方案，核对每一步，然后执行。", tools=True),
    Case("finance_report", "risk", "核验账单数据，分析差异并给出可追溯计算过程。", tools=True),
    Case("image_caption", "vision", "描述图片里的结构。", image=True),
    Case("image_code", "vision", "看这张截图并找出代码异常。", image=True),
    Case("image_audit", "vision", "请分析图中的电路，解释风险并提出验证步骤。", image=True),
    Case("image_research", "vision", "请比较两张图片中的项目进展并生成详细审查报告。", image=True),
    Case("tool_web", "tool", "打开浏览器搜索文档，核对版本后报告。", tools=True),
    Case("tool_file", "tool", "读取本地日志并总结错误分布。", tools=True),
    Case("tool_write", "tool", "修改配置文件，运行测试后保存结果。", tools=True),
    Case("large_context", "context", "提炼长会话中的关键决定与未解决问题。", history="此前的会话。" * 15000),
)


def catalog(*, without: frozenset[str] = frozenset(), no_fast_vision=False) -> KiroByoConfig:
    base = (
        ModelSpec("fixture", "Fixture", "fast", "Fast", tier="fast", tool_use=True,
                  context_window=64_000, routing_quality=.56, routing_speed=.94,
                  routing_economy=.94, input_modalities=("text",)),
        ModelSpec("fixture", "Fixture", "fast-vision", "Fast Vision", tier="fast", tool_use=True,
                  context_window=128_000, routing_quality=.62, routing_speed=.86,
                  routing_economy=.83, input_modalities=("text", "image")),
        ModelSpec("fixture", "Fixture", "balanced", "Balanced", tier="balanced", tool_use=True,
                  context_window=256_000, routing_quality=.78, routing_speed=.72,
                  routing_economy=.68, input_modalities=("text", "image")),
        ModelSpec("fixture", "Fixture", "strong", "Strong", tier="strong", tool_use=True,
                  context_window=500_000, routing_quality=.95, routing_speed=.40,
                  routing_economy=.22, input_modalities=("text",)),
        ModelSpec("fixture", "Fixture", "strong-vision", "Strong Vision", tier="strong", tool_use=True,
                  context_window=1_000_000, routing_quality=.92, routing_speed=.43,
                  routing_economy=.22, input_modalities=("text", "image")),
    )
    models = tuple(model for model in base if model.model_id not in (without | ({"fast-vision"} if no_fast_vision else set())))
    return KiroByoConfig((ProviderSpec("fixture", "Fixture", "openai-compatible", "http://invalid.local/v1", "", models),))


def main() -> int:
    errors = []
    soft_notes = []
    rows = []
    oldhome = os.environ.get("KIRO_BYO_HOME")
    try:
        with TemporaryDirectory(prefix="tiga-suite-") as tmp:
            os.environ["KIRO_BYO_HOME"] = tmp
            cfg = catalog()
            for c in CASES:
                for typ in TYPES:
                    route = route_model(cfg, pack="tiga", policy=typ, prompt=c.prompt,
                                        history=([{"role": "user", "content": c.history}] if c.history else []),
                                        has_image=c.image, tools_required=c.tools, tools_available=True,
                                        reasoning_effort=c.effort)
                    if route is None:
                        errors.append(f"{c.key}/{typ}: unexpected missing route in complete fixture pool")
                        continue
                    plan = build_execution_plan(route)
                    mids = [row.model.model_id for row in route.execution_fallbacks]
                    if typ in ("power", "glitter"):
                        if not model_in_tier(route.model, "strong") or any(not model_in_tier(m.model, "strong") for m in route.execution_fallbacks):
                            errors.append(f"{c.key}/{typ}: escaped strong tier")
                    if typ == "sky":
                        if not model_in_tier(route.model, "fast") or any(not model_in_tier(m.model, "fast") for m in route.execution_fallbacks):
                            errors.append(f"{c.key}/{typ}: escaped fast tier despite eligible fast candidate")
                    if c.image and "image" not in route.model.input_modalities:
                        errors.append(f"{c.key}/{typ}: unsupported image selected")
                    if c.tools and route.model.tool_use is False:
                        errors.append(f"{c.key}/{typ}: no tool capability")
                    if route.model.context_window and route.signals.estimated_input_tokens > int(route.model.context_window * .92):
                        errors.append(f"{c.key}/{typ}: insufficient context")
                    if not plan.agents or plan.agents[0].role != "lead":
                        errors.append(f"{c.key}/{typ}: missing lead")
                    if len(plan.agents) > plan.max_agents:
                        errors.append(f"{c.key}/{typ}: max_agents exceeded")
                    if any(a.role == "worker" and (a.tool_policy != "read_only" or a.authority != "assist") for a in plan.agents):
                        errors.append(f"{c.key}/{typ}: unsafe worker authority")
                    if any(a.role in {"review", "verify"} and a.tool_policy != "none" for a in plan.agents):
                        errors.append(f"{c.key}/{typ}: unsafe reviewer tool policy")
                    if typ == "glitter" and not any(a.role == "review" for a in plan.agents):
                        errors.append(f"{c.key}/{typ}: expected review stage missing")
                    rows.append({
                        "task": c.key, "group": c.group, "type": typ,
                        "model": route.model.model_id, "topology": plan.topology,
                        "agent_count": len(plan.agents), "workers": sum(a.role == "worker" for a in plan.agents),
                        "has_review": any(a.role in {"review", "verify"} for a in plan.agents),
                        "complexity": round(route.signals.complexity,3), "risk": round(route.signals.risk,3),
                        "breadth": round(route.signals.breadth,3), "parallelizability": round(route.signals.parallelizability,3),
                        "task_profile": route.signals.task_profile, "fallbacks": mids,
                    })
            # Explicit negative/absence scenarios: hard behavior must fail closed
            # where a policy promises no tier downgrade.
            for scenario, special, kwargs, expected in (
                ("no_strong", catalog(without=frozenset({"strong", "strong-vision"})), {}, {"power": None, "glitter": None}),
                ("no_fast", catalog(without=frozenset({"fast", "fast-vision"})), {}, {"sky": "balanced"}),
                ("vision_fast_missing", catalog(no_fast_vision=True), {"has_image": True}, {"sky": "balanced"}),
                ("strong_vision_missing", catalog(without=frozenset({"strong-vision"})), {"has_image": True}, {"power": None,"glitter": None}),
            ):
                for typ, want in expected.items():
                    selected = route_model(special, pack="tiga", policy=typ, prompt="请分析图片中的情况" if kwargs else "你好",
                                           history=[], tools_required=False, tools_available=True,
                                           has_image=bool(kwargs.get("has_image", False)))
                    actual = selected.model.model_id if selected else None
                    if actual != want:
                        errors.append(f"{scenario}/{typ}: expected {want!r} got {actual!r}")
                    rows.append({"task": scenario, "group": "hard_gate", "type": typ,
                                 "model": actual, "expected_model": want, "invariant_ok": actual == want})
            # Negative capability gates: the router cannot choose a model that
            # explicitly lacks a capability, or fits no context window.
            def negative(scenario, models, prompt, *, vision=False, tools=False, history=None):
                nofit = KiroByoConfig((ProviderSpec("negative", "Negative", "openai-compatible", "http://invalid.local/v1", "", tuple(models)),))
                for typ in TYPES:
                    route = route_model(nofit, pack="tiga", policy=typ, prompt=prompt,
                                        history=history or [], has_image=vision,
                                        tools_required=tools, tools_available=True)
                    result = None if route is None else route.model.model_id
                    if result is not None:
                        errors.append(f"{scenario}/{typ}: unexpected eligible model {result}")
                    rows.append({"task":scenario,"group":"hard_gate","type":typ,
                                 "model":result,"expected_model":None,"invariant_ok":result is None})

            negative("all_no_vision", [ModelSpec("negative","Negative","text-only","Text Only",
                     tier="strong", tool_use=True, input_modalities=("text",))],
                     "分析上传的图片", vision=True)
            negative("all_no_tools", [ModelSpec("negative","Negative","no-tools","No Tools",
                     tier="strong", tool_use=False, input_modalities=("text",))],
                     "执行文件工具操作", tools=True)
            negative("all_context_too_short", [ModelSpec("negative","Negative","short","Short",
                     tier="strong",tool_use=True,context_window=1000,input_modalities=("text",))],
                     "请总结这些长文本", history=[{"role":"user","content":"context"*1000}])
            # Compare intended policy directions, do not mistake these for quality measures.
            by = {(r["task"],r["type"]):r for r in rows if r["group"] != "hard_gate"}
            glitter_simple = sum(1 for c in CASES if c.group == "simple" and by[(c.key, "glitter")]["has_review"])
            if glitter_simple:
                soft_notes.append(f"Glitter plans a review for {glitter_simple}/{sum(c.group=='simple' for c in CASES)} simple tasks; real benefit unverified")
            # Cross-language task estimate spread is only descriptive.
            complexity = {typ: sorted((r["complexity"] for r in rows if r.get("type")==typ and r.get("group")=="simple")) for typ in TYPES}
            summaries = {}
            for typ in TYPES:
                these = [r for r in rows if r.get("type")==typ and r.get("group")!="hard_gate"]
                summaries[typ] = {"selected_models": dict(Counter(r["model"] for r in these)),
                                  "topologies": dict(Counter(r["topology"] for r in these)),
                                  "worker_tasks":sum(r["workers"]>0 for r in these),
                                  "review_tasks":sum(r["has_review"] for r in these)}
            # Same intent phrased across the five UI languages. This is an
            # observational parity probe, not a fabricated ground-truth grade.
            paired = {
                "zh-CN": "审查代码仓库、修复错误，并为三个模块添加回归测试。",
                "zh-TW": "審查程式碼儲存庫、修復錯誤，並為三個模組新增回歸測試。",
                "en": "Review the code repository, fix errors, and add regression tests for three modules.",
                "ja": "コードリポジトリを確認して、不具合を修正し、三つのモジュールに回帰テストを追加してください。",
                "ko": "코드 저장소를 검토하고 오류를 수정하며 세 모듈에 회귀 테스트를 추가해 주세요.",
            }
            parity = {}
            for language, prompt in paired.items():
                parity[language] = {}
                for typ in TYPES:
                    route = route_model(cfg, pack="tiga", policy=typ, prompt=prompt,
                                        history=[], has_image=False, tools_required=True, tools_available=True)
                    if route is None:
                        errors.append(f"language/{language}/{typ}: unexpected missing route")
                        continue
                    plan = build_execution_plan(route)
                    parity[language][typ] = {
                        "complexity": round(route.signals.complexity,3),
                        "task_profile": route.signals.task_profile,
                        "selected_model": route.model.model_id,
                        "topology": plan.topology,
                    }
            complexity_spread = max(v["multi"]["complexity"] for v in parity.values()) - min(v["multi"]["complexity"] for v in parity.values())
            if complexity_spread > 0.15:
                soft_notes.append(f"Matched five-language requests differ in complexity by {complexity_spread:.2f}; check routing parity before tuning weights")
            pack_path=ROOT/"src/kiro_byo/policy_packs/tiga.json"
            result={"status":"pass" if not errors else "fail", "scope":"synthetic_decisions_only",
                    "provider_calls":0,"tool_calls":0,"cases":len(CASES),"standard_decisions":len(CASES)*len(TYPES),
                    "hard_gate_checks":sum(r["group"]=="hard_gate" for r in rows),
                    "policy_sha256":hashlib.sha256(pack_path.read_bytes()).hexdigest(),
                    "errors":errors,"observations":soft_notes,"summary":summaries,"language_parity":parity,"rows":rows}
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if not errors else 1
    finally:
        if oldhome is None: os.environ.pop("KIRO_BYO_HOME", None)
        else: os.environ["KIRO_BYO_HOME"] = oldhome

if __name__ == "__main__":
    raise SystemExit(main())
