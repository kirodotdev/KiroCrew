# Kiro BYO 0.2.5 — Architecture V6 / Provider Loop Terminal Boundary

**Date:** 2026-10-30  
**Baseline:** `Kiro_BYO_0.2.5_Architecture_V5_Source.zip` (`0.2.5+fd599417c3d70cd7`)  
**Product version:** `0.2.5` (unchanged)  
**Target host:** KiroCrew `0.11.0-rc.5` (Windows end-to-end unverified)

## 目的、根因与修改范围

V5 的 `TurnRunnerMixin._run_provider_tool_loop` 仍有 284 行：除十条明确的执行循环控制边外，还内嵌三个独立的终止处理——调用前取消、Provider 错误与内部异常。它们涉及会话回滚/保留、响应结算、遥测和错误事件。这是一项**架构耦合风险**，本次没有发现能够独立复现的生产故障，因此不声称修复了此前未确认的 Provider 行为缺陷。

本次只做**职责归属调整**。真正决定同模型重试、干净失败后回退的 `RecoveryPolicy.live_provider_failure`、副作用判断以及工具批次链路，全部保留原文件和原位置。终止处理模块不能再次决策重试或回退。

## 修改文件

- `src/kiro_byo/provider_loop_terminal.py`（新增）：`ProviderLoopTerminalMixin` 承接取消前终止、最终 ProviderError、内部异常的原始收尾逻辑。
- `src/kiro_byo/turn_runner.py`：接入 Mixin，将原终止分支替换为委托调用；十个历史 `continue` 分支、四个 `state.step -= 1` 和单一 `state.step += 1` 保持原状。
- `tests/test_provider_loop_failures_v6.py`（新增）：6 项安全/回归测试。
- `scripts/release_check.py`：检查新的 Mixin 接线、终止分支标记，并拒绝终止模块包含回退/重试策略。
- `config/architecture_budget.json`：收紧 200 行以上函数预算至 11、`_run_provider_tool_loop` 上限至 200、主文件上限至 4421。
- `CHANGELOG.md` 与本审计报告：记录更改及已验证/未验证范围。

## 重要不变量

1. `RecoveryPolicy.live_provider_failure` 是回退/重试唯一策略入口（本次路径范围内）；终止处理永远不重放请求与工具。
2. 已开始输出内容的 Provider 请求不得走“干净失败”回退。原 `pristine` 计算和判定未改。
3. 工具已发生副作用后，错误收尾必须调用 `preserve_side_effects=state.side_effect_risk_seen`；不能截断实际已执行动作的历史。
4. 取消先于 Provider 发起时不得调用 Provider，仍按原约定发回一次 `cancelled` 结果。
5. Provider 和内部异常最终仅生成一次 ACP 请求结果、保留真实错误遥测；内部异常 Runtime Trace 仍只包含错误类型、代码位置和模型信息，不加入任意异常消息字段。
6. Tiga Sky/Multi/Power/Glitter 策略、具体模型直连、Dyna 禁用、Crew 模型选择双向联动和 Durable Run Ledger 均未修改。

## 源码等价核对

- 使用 AST 检查，V5 的 ProviderError / Exception 处理块中除原函数尾部 `return` 外的所有 Python 语句，在 V6 新模块中完全一致。
- 取消收尾函数逐句迁移；改动仅为调用委托，执行仍位于原 `try` 和原 Provider 调用之前。
- 执行循环控制契约测试验证 10 个 `continue`、4 个递减与 1 个递增；没有更改跨轮控制步数逻辑。

## 架构指标

| 指标 | V5 | V6 |
|---|---:|---:|
| `_run_provider_tool_loop` | 284 | 200 |
| `turn_runner.py` | 4,504 | 4,421 |
| 超过 200 行的 Python 函数 | 12 | 11 |
| 超过 100 行的 Python 函数 | 54 | 54 |
| 新的 `provider_loop_terminal.py` | — | 131 行 |

## 测试结果

- 133 个 `test_*.py` 文件，分 15 组独立运行；**1,167 passed，1 skipped，另 58 个 unittest subtests passed**，完成的组没有 assertion failure 或 timeout。
- 新增六项 V6 测试通过：重试耗尽后单次结算；按策略回退；部分流已显示时不回退；写入工具后内部异常保留副作用历史且不重放；调用前取消不请求 Provider；分支职责静态检查。
- 原有 `test_turn_loop_refactor_contract.py` 的历史控制流结构测试通过。
- `scripts/release_check.py` PASS；`scripts/architecture_guard.py --check` PASS；`compileall` PASS；`node --check ui/index.mjs` PASS。
- 构建打包后的 ZIP 验证结果和 SHA256 见交付校验清单。

## 限制与后续风险

- 尚未完成 KiroCrew `0.11.0-rc.5` Windows 实机、ACP 进程重启、真实 Provider API、网络代理中断实测。自动测试及受控模拟不能替代这些验证。
- Durable Run 仍使用原有的保守副作用不确定策略，不承诺跨进程 exactly-once。
- 本次是终止处理职责收敛，不意味着 Provider 回退/限流策略存在已确认的待修复缺陷，也没有重新设计路由语义。
- Dyna Flash/Strong/Miracle 仍禁止启用，不对其正式执行策略做兼容承诺。
