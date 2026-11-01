# Kiro BYO v0.2.3 — S3-01 Durable Run 恢复安全优化报告

日期：2026-10-24  
唯一基线：`Kiro_BYO_0.2.3_S2-05_后台模型交接_源码包.zip`  
对照 Crew：`v0.10.0-insider.4`（`e9bfada`）  
展示版本：`0.2.3`（Build ID 由发布脚本生成）

## 1. 实际代码缺口及本轮修复

| 缺口 | 修改 | 验证 |
|---|---|---|
| 超出 `max_runs` 时直接删除旧记录，可能删除仍在运行、失败或等待恢复的 Run | 只淘汰已完成记录，允许未完成记录临时超过历史保留数量上限 | `test_history_pruning_keeps_pending_failed_and_cancelled_runs` |
| 只保留最近 64 条检查点，旧的 `side_effect_risk=True` 可能被滚动清除 | 在 Run 根记录持久化单调 `side_effect_seen=True` | `test_risk_survives_checkpoint_rotation` |
| 老版本记录正好拥有 64 条检查点，无法证明较早阶段没有副作用 | 在没有单调风险标记的饱和窗口中保守拒绝恢复 | `test_legacy_full_window_cannot_prove_absence_of_old_side_effects` |
| 恢复策略可能只信任 `recovery_state`，绕开已保存的副作用证据 | 同时查验可用检查点/根记录风险标记 | `test_recovery_policy_blocks_side_effect_even_with_optimistic_state` |
| 正式 ACP 回合开始 Durable Run 失败时，关键检查点可能因为 Run ID 为空直接返回 | 真实 ACP 回合必须具备有效 Run ID 且存在持久化记录，才能越过关键工具风险边界 | `test_critical_checkpoint_requires_run_and_existing_ledger` |
| Pending 查询受 `max_runs` 历史保留值限制 | 扫描最多 500 条待处理状态，避免在保留策略收紧后把待恢复记录隐藏 | pending/prune 联合测试 |

`active_turn_id` 是真实 ACP Prompt 已建立会话事件锚点的证据；不经过 ACP Prompt 的低层测试调用不会被强制要求先建立完整 Durable Run，避免改变已存在的内部 Characterization API。

## 2. 当前仍不承诺的能力

- 没有实现跨机器/跨进程的 exactly-once 语义。
- 仍以原子替换 JSON 保存控制平面；没有完成 `fsync`、掉电模拟或目录同步的完整可靠性验收。
- 已派发到远端服务的工具调用不能自动回滚。未知结果必须核对外部状态。
- Crew 真实 Windows 会话恢复、日程、队友及任务队列仍未端到端验证。
- 没有更改 Tiga 四形态、Dyna 禁用、Model 路由、Provider、Crew UI 或 Crew 数据库。

## 3. 运行与发布验证

- 新增 5 项定向安全测试，并通过既有 Durable Recovery、Turn Runner 与全部 Python 回归。
- 全量结果：**771 passed, 1 skipped, 52 subtests passed**。
- 部分运行脚本因执行环境的调用超时未正常返回，但保存的 `pytest -vv` 日志包含完整终态和上述通过统计；正式 Windows 环境仍待验证。
- `scripts/release_check.py` 与发布包完整性检查由构建流程执行。

## 4. 下一轮 S3-02

重点做分阶段事件的有序关联、恢复原因诊断以及失败后谁拥有重试权，优先使用已有 `session_events`、`runtime_trace`、`durable_runs`，不再创建彼此冲突的第二套任务日志。继续保留 v0.2.3 与 Tiga/Dyna 不变。
