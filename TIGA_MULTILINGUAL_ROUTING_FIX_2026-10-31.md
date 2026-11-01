# Kiro BYO 0.2.5 — Tiga 五语言路由识别修复（2026-10-31）

## 基线与修复范围

- 基线：`Kiro_BYO_0.2.5_Tiga_Deep_Optimization_Source.zip`（`0.2.5+d18b6c38407e4e9f`）。
- 本次只修改共享任务识别器 `src/kiro_byo/mode_router.py` 的自然语言识别词形，添加 `tests/test_tiga_language_parity.py` 和可复现审计脚本 `scripts/tiga_suite.py`。
- **没有调整** Tiga 四种 Type 的权重、候选池、执行规划、Worker/Reviewer 限制和工具安全规则；`tiga.json` 与 `dyna.json` 的字节及 SHA256 与基线相同。Dyna 保持 `enabled=false`。

## 已复现根因及修复

相近代码仓库审查任务被识别为不同的任务类型：繁体中文和韩文没有匹配到 `_CODE`，同时繁体中文的“並”与韩文 `하고` / `하며` 未被 `_CLAUSE_JOIN` 识别；导致任务复杂度偏低，进而影响 Multi 选模和 Power 的 Reviewer 计划。

此次在原有有限正则启发式中补充了繁体中文、日文和韩文的代码词汇、连接词、明确文件操作与仓库目标的识别。**仍是启发式，不是语言模型语义识别，也不声称所有表达方式都已覆盖。**

一项新增反例最初失败：在“請說明程式碼審查的重要性”中，如果把 `程式碼` 直接当作工具目标，那么纯解释性问答也会误触发工具调用。已通过限制工具目标必须具有更明确的仓库/文件语义修复；该反例及其他四语言问答均通过。

### 前后对比（固定合成模型池）

| 语言 | 之前复杂度 / 任务类型 | 之后复杂度 / 任务类型 | Multi 选模 / Power 计划（之后） |
|---|---|---|---|
| 简中 | 0.62 / code | 0.62 / code | balanced / lead-review |
| 繁中 | 0.30 / tool | 0.62 / code | balanced / lead-review |
| 英文 | 0.62 / code | 0.62 / code | balanced / lead-review |
| 日文 | 0.58 / tool | 0.70 / code | balanced / lead-review |
| 韩文 | 0.30 / tool | 0.68 / code | balanced / lead-review |

当前五语言复杂度范围 0.62～0.70，跨度 0.08；四 Type 对这组任务生成的模型 ID 和执行拓扑一致。数字并非真实模型的质量、运行成本或成功率。

## 测试结果

- 新增五语言回归：正例、工具需求、简单闲聊、解释性反例、Crew 包装隔离及四 Type 选模/执行计划一致性。
- 定向测试：`97 passed, 11 subtests passed`。
- 全量文件覆盖：140 个 `tests/test_*.py`，分为 19 个常规分组和一个单独拆开的浏览器分组运行；共 **1245 passed, 1 skipped, 58 subtests passed**，没有断言失败。单次连续运行的 Chromium 组发生执行环境超时，之后各测试文件分别运行完成；**这不是一次全套 pytest 命令完整结束的结果**。
- `python -m compileall`、`node --check ui/index.mjs`、`scripts/architecture_guard.py` 和 `scripts/release_check.py` 通过。
- 扩展合成审计：32 个场景 × 4 Type = 128 次决策、18 次硬能力/候选池检查、5 语言 × 4 Type = 20 次对照决策；审计状态 pass，0 次实际 Provider / Tool 调用。

复现：

```bash
PYTHONPATH=src python scripts/tiga_suite.py . > tiga_results.json
PYTHONPATH=src pytest -q tests/test_tiga_language_parity.py tests/test_mode_router.py tests/test_tool_intent_routing.py
python scripts/release_check.py
```

## 未解决和实机验证要求

1. Multi 对部分高风险任务选用快速模型及 Glitter 简单任务安排协作的问题，仍需真实质量/成本数据；没有基于合成数据擅自调整策略。
2. 韩文、繁中等更广泛表达仍需要真实用户请求与人工评测来持续增加覆盖，避免过拟合单个测试句。
3. 尚未在 KiroCrew `0.11.0-rc.5` Windows 宿主中完成真实 ACP、工具、权限与恢复验收。
4. 当前禁用的 Dyna 没有正式启用，也没有进行 Dyna 三种 Type 的产品级验收。

修复只改善路由识别的数据质量；不会更改用户对工具操作的审批、失败关闭、会话恢复与 Durable Run 副作用边界。
