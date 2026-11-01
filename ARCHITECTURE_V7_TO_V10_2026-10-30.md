# Kiro BYO 0.2.5 — Architecture V7–V10 综合验收报告

**日期**：2026-10-30  
**基线**：`Kiro_BYO_0.2.5_Architecture_V6_Source.zip`，构建 `0.2.5+8346e8d372b32033`  
**产品版本**：`0.2.5`（保持不变）  
**目标宿主**：KiroCrew `0.11.0-rc.5`（Windows 实机未验证）

## V7 — 工具批次停止与控制分支

**确认的结构性问题**：`TurnRunnerMixin._handle_tool_round`（246 行）将工具批次准入、执行、后执行收据、取消、执行保护的停止/Worker 交还和正常循环控制混合在同一函数。没有复现到新的产品功能错误。

**修改**：将原 `batch_outcome.stop_requested` 的停止/Worker handback 语句整体迁入同类 `_handle_tool_guard_stop`（61 行），`_handle_tool_round` 仅在同一位置委托调用，降至 **191 行**。保留执行顺序，先持久化 `tool_batch_admitted`，调用工具，再持久化 `tool_batch_completed`，最后才处理保护停止。未改变 `batch_id`、工具副作用风险、批次重放保护、取消和回退。

**验证**：以 V6 原始源码为参照，对迁移的 `if batch_outcome.stop_requested` 语句和新方法主体进行 AST 对比，语句树完全一致；新增两项针对 Worker handback 和灾难级调用上限的故障路径测试。原有后执行收据失败不允许重放测试保持通过。

## V8 — ACP Session 恢复与 Durable Ledger 对账

**确认的结构性问题**：`AcpServer._restore_session_impl`（219 行）同时承担恢复会话配置、资源初始化、Session 组装、孤儿 Durable Run 对账和实际发布。将 Durable Run 的恢复判定与 Session 组装分开，可以防止维护时错误跳过活跃租约/不确定副作用的保护。

**修改**：将原始孤儿 Run 对账逻辑整体迁入 `_reconcile_restored_durable_runs`（40 行），`_restore_session_impl` 降至 **182 行**。原位置仍在 `session/resume` 和 `selection.resolved` 事件之后、`_put_session(restoring=True)` 之前。保留 `is_lease_active`、`restart_recovery_state`、`mark_recovery` 的原始调用顺序，不触发自动重放。

**验证**：AST 语句对比与 V6 一致；新增两项测试，覆盖活跃租约跳过、对账竞争失败跳过、不确定副作用分类不被清洗，以及可选 Agent projection 存储故障不阻碍 Run 分类。

## V9 — 依赖方向约束

**确认的架构风险**：此前仅依靠行数预算和文件名/特定文本验证，不能阻止新的叶子模块反向导入 Turn Runner / ACP Server，再次形成依赖环。

**修改**：新增 `scripts/boundary_guard.py`，通过 Python AST 扫描七个约定的叶模块（`routing_events`、`prompt_routing`、`provider_loop_terminal`、`native_tool_dispatch`、`acp_content`、`mode_router`、`recovery_policy`）。禁止静态导入 `turn_runner`、`acp_server` 或 App backend，并将检查接入 `scripts/release_check.py`。新增两项架构契约测试检查禁用的绝对/相对导入以及合法字符串不误报。

**约束范围**：这是明确界定的七个叶模块的静态导入守卫，不是全项目依赖无环证明，不扫描动态 import、插件 runtime 或 JavaScript。

## V10 — 全链路回归与发布门槛

- 新增两个跨系统稳定性测试：请求失败/取消不使健康 Runtime 变黄、Route Trace/独立 Durable Ledger 与不确定副作用状态不丢失；Dyna 仍禁用。
- 保留此前的真实 Tiga Multi Type 路由记录、Dyna 预检三形态可控模拟、具体模型直连、请求清理与 Trace/Durable Ledger 隔离、ACP/MCP、Crew Linkage、五语言、原生工具权限等已有测试。
- **全量测试**：136 个 `test_*.py` 文件分 16 组执行，合计 **1,175 passed / 1 skipped / 58 unittest subtests passed**。全部 16 组返回成功，未将无结果的执行视为通过。
- `compileall`、`node --check ui/index.mjs`、`scripts/architecture_guard.py --check`、`scripts/boundary_guard.py`、`scripts/release_check.py` 均通过。
- 与 V6 字节比较，`src/kiro_byo/policy_packs/tiga.json`、`dyna.json`、五语言 JSON、`ui/index.mjs` 均完全相同。没有改变 Mode/Type 选择策略、模型直连语义、Crew 覆盖文件或 Provider 代码。

## 架构预算与可维护性

| 度量 | V6 | V10 |
|---|---:|---:|
| `_handle_tool_round` | 246 | **191** |
| `_restore_session_impl` | 219 | **182** |
| 超过 200 行 Python 函数 | 11 | **9** |
| 超过 100 行 Python 函数 | 54 | **54** |
| `turn_runner.py` 文件行数 | 4,421 | 4,429 |
| `acp_server.py` 文件行数 | 3,624 | 3,628 |

两份核心文件增加的少量行来自命名明确的新方法和注释，不代表生产逻辑增加。文件行数预算已**显式调整**为当前基线，而非静默放宽；超 200 行函数总量上限从 11 收紧至 9，同时新增两个入口及两个辅助方法的函数级预算。此轮没有强行对剩余 9 个超过 200 行的函数进行重构；是否继续拆分由可靠性与职责边界决定。

## 本轮实际修改文件

- `src/kiro_byo/turn_runner.py` — V7 工具停止职责拆分
- `src/kiro_byo/acp_server.py` — V8 会话恢复 Durable Run 对账拆分
- `scripts/boundary_guard.py` — V9 新增依赖守卫
- `scripts/release_check.py` — V9 发布门槛集成
- `config/architecture_budget.json` — V7/V8/V9 收紧预算与新方法追踪
- `tests/test_architecture_v7_v8_safety.py` — 四项故障路径测试
- `tests/test_architecture_v9_boundary.py` — 两项依赖契约测试
- `tests/test_architecture_v10_stability.py` — 两项跨系统检查
- `CHANGELOG.md`、本报告 — 变更记录

## 剩余风险与验收边界

1. **未做 KiroCrew 0.11.0-rc.5 Windows 实机验证**：ACP 进程重启、真实 Provider 故障、实际 UI 模型联动/权限提示、运行服务红黄灯，均需宿主联机环境。不可宣称已经实现端到端实机验收。
2. 受控模拟不承诺网络、文件系统或跨进程工具调用“恰好一次”。`unknown_side_effect` 的恢复始终需要保守处理和用户确认。
3. Dyna Flash / Strong / Miracle 仍只保留预检配置，正式执行、协作与安全策略尚未设计完整；此次没有启用 Dyna。
4. 代码检查不能证明所有跨模块语义等价，不能代替长期运行和宿主兼容测试；保留强约束回归并优先修复真实缺陷。
5. 此轮以完成四项架构治理与自动化验收为止，下一阶段应进入**基于真实使用问题的维护**，避免只为降低行数继续重构。

本报告不含用户凭据、私人本地配置或真实 Provider API 数据。
