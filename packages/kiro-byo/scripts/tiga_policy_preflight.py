#!/usr/bin/env python3
"""Offline, credential-free Tiga four-Type route/plan contract preflight.

This deterministic synthetic replay measures *decisions*, not response quality,
real Provider latency, token spend, or KiroCrew host compatibility. It uses a
private temporary HOME so user telemetry and custom policy overrides cannot
contaminate the result. Policy weights and route decisions are read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec  # noqa: E402
from kiro_byo.mode_router import build_execution_plan, route_model  # noqa: E402


CASES = (
    ("greeting", "你好", False, False),
    ("code", "帮我修复 Python 函数的 bug，并说明测试结果", False, True),
    ("cross_module", "审查大型项目架构，找出多个模块的缺陷、实现修复并补上回归测试", False, True),
    ("vision", "请查看这张图片并分析内容", True, False),
)


def _catalog() -> KiroByoConfig:
    models = (
        ModelSpec("synthetic", "Synthetic", "fast", "Fast", tier="fast", tool_use=True,
                  routing_quality=.57, routing_speed=.90, routing_economy=.91, input_modalities=("text",)),
        ModelSpec("synthetic", "Synthetic", "balanced", "Balanced", tier="balanced", tool_use=True,
                  routing_quality=.77, routing_speed=.72, routing_economy=.69, input_modalities=("text", "image")),
        ModelSpec("synthetic", "Synthetic", "strong", "Strong", tier="strong", tool_use=True,
                  routing_quality=.94, routing_speed=.41, routing_economy=.24, input_modalities=("text", "image")),
    )
    return KiroByoConfig((ProviderSpec("synthetic", "Synthetic", "openai-compatible", "http://invalid.local/v1", "", models),))


def audit() -> dict:
    # The live user's store and policy override are never read.
    previous_home = os.environ.get("KIRO_BYO_HOME")
    try:
        with TemporaryDirectory(prefix="kiro-byo-tiga-preflight-") as tmp:
            os.environ["KIRO_BYO_HOME"] = tmp
            entries = []
            errors = []
            cfg = _catalog()
            for name, prompt, vision, tools in CASES:
                for policy in ("sky", "multi", "power", "glitter"):
                    result = route_model(cfg, pack="tiga", policy=policy, prompt=prompt,
                                         history=[], has_image=vision, tools_required=tools,
                                         tools_available=True)
                    if result is None:
                        errors.append(f"{name}/{policy}: no eligible route")
                        continue
                    plan = build_execution_plan(result)
                    model_id = result.model.model_id
                    # Hard-policy invariants: Sky may cross tiers ONLY for
                    # missing capability; Power and Glitter never do.
                    if policy in {"power", "glitter"} and model_id != "strong":
                        errors.append(f"{name}/{policy}: strong-pool isolation broken")
                    if policy == "sky" and not vision and model_id != "fast":
                        errors.append(f"{name}/{policy}: fast-pool isolation broken")
                    if policy == "sky" and vision and model_id == "fast":
                        errors.append(f"{name}/{policy}: image capability gate broken")
                    entries.append({
                        "task": name, "type": policy, "selected_model": model_id,
                        "topology": plan.topology,
                        "roles": [agent.role for agent in plan.agents],
                        "fallback_candidates": [row.model.model_id for row in result.execution_fallbacks],
                    })
            digest = hashlib.sha256((ROOT / "src/kiro_byo/policy_packs/tiga.json").read_bytes()).hexdigest()
            return {
                "scope": "synthetic_decision_replay_only", "real_provider_invocations": 0,
                "type_count": 4, "case_count": len(CASES),
                "tiga_policy_sha256": digest,
                "status": "pass" if not errors else "fail",
                "checks": entries, "errors": errors,
            }
    finally:
        if previous_home is None:
            os.environ.pop("KIRO_BYO_HOME", None)
        else:
            os.environ["KIRO_BYO_HOME"] = previous_home


def main() -> int:
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
