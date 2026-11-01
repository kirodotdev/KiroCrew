# Kiro BYO 0.2.3 · 全局代码/结构审计与集中修复报告（2026-10-24）

**基线**：2026-10-24 交付的 `Kiro_BYO_0.2.3_Crew_0.10专用_源码包.zip`，构建 `0.2.3+dfd9bca55ff90e18`。与历史 S4-03 独立；本轮所有改动在新工作目录内完成，原发布包没有被覆盖。

**适配范围**：KiroCrew `v0.10.0-insider.4` 为主要目标；不恢复 0.9 适配。**产品版本保持 0.2.3**。Tiga Sky / Multi / Power / Glitter 与 Dyna 只读禁执行的策略均未修改。

## 1. 结论

上轮独立审计发现的 Durable Run 跨进程丢失写入/副作用标记（P1）和恢复政策不一致（P2），本轮已完成代码修复与自动化故障注入验证。进一步排查发现相同的进程内锁假设也存在于 Agent 任务状态、Workspace 清单、Runtime Trace 和 Turn Telemetry；已在这些**确实有读改写或裁剪竞争**的入口复用稳定侧车文件锁。

**本轮不宣称“全局零 Bug”或“生产级完全对等”**。跨进程 ACP 会话所有权、远程工具外部副作用与 Windows KiroCrew 原生验收仍是显著缺口。

## 2. 已修复项目与证据

| 编号 | 优先级 | 旧行为 | 本轮改动 | 验证 |
|---|---|---|---|---|
| F01 | P1 | `durable_runs.py` 仅 RLock，双进程各写一次最终可能只剩一条；副作用证据可能被覆写 | 新 `state_io.py` 统一跨进程稳定 `.lock` 文件；所有 Run 读改写在持锁期间重新读取；写入后修订号单调增长 | `test_two_processes_preserve_all_checkpoint_updates_and_side_effect`（spawn 独立进程） |
| F02 | P2 | `cancelled` 任务可 Resume；`running/clean` 策略宣称安全、最终准入却拒绝 | `DurableRunStore.recovery_rejection()` 作为唯一状态判断函数，RecoveryPolicy、`prepare_resume` 和 `resumable` 共用；已取消任务必须创建新 Run | `test_recovery_policy_and_transaction_agree_on_terminal_and_active_states` |
| F03 | P2 | 文件写入只 `os.replace`，缺少替换前刷盘 | 新 `_atomic_json_write` 写入/flush/fsync 后才 `replace`，POSIX 尽可能 fsync 目录，Windows 不声称具备可移植目录刷盘能力 | 模拟 fsync 失败时保留原 JSON；原有 replace 故障注入仍通过 |
| F04 | P2 | `AgentStateStore` 同一 tasks.json 多进程读改写会互相覆盖 | 与 Run 共用文件锁/原子写；损坏 JSON 不自动当成空文件覆盖，投影不可用时只降级可观测性 | 8 个 spawn 进程同时 upsert；损坏清单完整保留 |
| F05 | P2 | Workspace manifest.json 多进程登记可丢失工作区租约；损坏清单可能当空清单覆盖 | 写操作跨进程上锁、原子写入；清单损坏明确拒绝覆盖 | 现有 Workspace 回归＋损坏文件测试 |
| F06 | P2/P3 | Runtime Trace / Telemetry 的 append→trim 只有线程锁，多进程可能相互覆盖 | 两个 JSONL 事件源新增跨进程锁覆盖 append/trim/clear；临时文件名避免冲突 | 4 个 spawn 进程合计 40 条 Trace、40 条 Telemetry，均保留 |
| F07 | P3 | Run 与 AgentState 各维护不同的原子 JSON 写逻辑 | 提取 `state_io.py`，把跨平台文件锁与原子写集中管理 | 全量 Python 回归＋发布审计 |

**语义与限制**：锁是遵约写者之间的本地文件系统 advisory lock；只保证读改写串行化，不代表远端 MCP exactly-once。Windows `msvcrt.locking` 分支尚未在真实 Windows 文件系统跑过；网络共享盘也需单独评估锁可靠性。单调修订号不是 CAS/全局 owner lease。

## 3. 结构债务与仍需处理的问题

| 优先级 | 待处理项 | 为什么这轮没有强行改 | 下一阶段验收建议 |
|---|---|---|---|
| P1（发布验收） | **ACP Session 状态跨进程所有权缺失**。`acp_server.py` 持有本地 `session.state_lock`，保存的是整份事件/会话快照；若两个 ACP 进程写同一个 Session，简单文件锁仍可能让后写的过期快照覆盖前者 | 需要宿主进程所有权契约、epoch/租约及事件序列冲突拒绝；不能仅靠文件写锁解决 | 双进程同一 Session 并发加载/追加时，拒绝第二写者或调和版本冲突，严禁丢 user/tool 事件 |
| P1（验收） | Crew 0.10 原生日程、队友、Saved Agents、浏览器、Steering/Queue 和权限真实 E2E | 本地只有源码单元测试，不等于 Windows 宿主契约验证 | Windows `v0.10.0-insider.4` 安装/卸载、取消/恢复/队友调度、权限撤回矩阵 |
| P2 | Durable Run 跨进程执行**唯一所有者/租约**尚未实现；进程锁仅保护 JSON 事务 | 同一 Run 可能由两个进程轮流取得短期写锁并**同时实际调用外部工具** | 租约持有者、fencing token、执行前二次确认，未知副作用 fail closed |
| P2 | ACP Session 写入与 Event Log 的断电一致性/断线恢复 | 目前 session 保存独立于 Durable Ledger，没有完整事务 | 断电/kill -9 故障注入；源事件与 checkpoint 不能互相伪造安全性 |
| P2 | `WorkspaceManager.apply_candidate()` 从 parent clean 检查到 git apply 有竞态；工作树创建成功后 manifest 写入失败可遗留 Git worktree | Git 工作树和仓库状态属于外部副作用，不能靠 JSON 原子写原样修好 | 为 Git 变更引入单仓库执行排他锁；失败清理并允许事后 reconcile |
| P2/P3 | `mode_registry.py` ↔ `policy_packs.py` 懒加载循环依赖；legacy Tiga JSON 覆盖可越过内置契约校验 | 属于设计向后兼容，直接改会改变旧用户策略 | 抽出 schema/discovery 无环模块；标记 legacy_override 来源和生效 hash |
| P3 | `turn_runner.py` ~4968 行、`acp_server.py` ~3600 行、`integration_manager.py` ~1772 行；职责过于集中 | 单次大规模拆分会把控制面/权限/状态机回归风险放大 | 优先抽 RunRecoveryService、ToolAdmission、SessionPersistence；逐边界迁移，不改变 ACP 接口 |
| P3 | `mcp_client.py` 等其他缓存、集成补丁的写入流程，尚未全部完成跨进程一致性演练；`except Exception` 容错边界较宽 | 多为 best-effort cache/集成安装事务，各自语义不同；机械套锁反可能死锁 | 按入口逐一注入失败、审计敏感数据暴露与锁次序 |
| P3 | Crew 0.9 注释和小型遗留兼容语义尚残留 | 已经不分发 0.9 适配器，历史语义可能还用于 ACP 客户端 | 核验真实 Crew 0.10 后清除死代码；不要只凭名字删除 |

**明确不更改**：Crew 宿主数据库、现有 Crew 0.10 补丁、Provider API、Tiga 模型评分/形态、Dyna 禁用、用户历史会话格式、前端设计。

## 4. 回归、故障注入与发布依据

- 单次完整 Python 回归：**812 passed、1 skipped、52 subtests passed**。新增 `tests/test_global_integrity.py` 7 项。
- 状态机：已完成/已取消/运行中不可直接恢复，策略与最终准入保持一致。
- 多进程故障：两进程的副作用风险标志无丢失；8 进程任务投影保留；4 进程 JSONL 事件和遥测均保留。
- 文件故障：fsync 失败时不覆盖原 Run；AgentState、Workspace 清单损坏时拒绝无声覆盖。
- 版本元数据：`VERSION`、`pyproject`、App、Python package、Agent 均是 `0.2.3`。
- **1 项跳过**为要求真实 Windows KiroCrew 的 E2E 场景（不能伪称已经验证）。

本轮没有测 Windows 上实际的文件锁语义、断电持久化、真实远程 MCP 工具 exactly-once，也没有宣称全局零 Bug。

## 5. 建议执行顺序

1. 在此次修复版上做 Windows `0.10.0-insider.4` 集中验收，首先验证文件锁/取消/恢复和 Crew 工具权限。
2. 提出 ACP Session 与 Run 的进程所有权契约（generation/fencing token），先写竞争测试，再改持久化格式。
3. 单独梳理 `turn_runner` 的工具准入与 `acp_server` 的 Session 恢复，把最复杂的职责拆成可测试服务。
4. 完成 Crew 原生队友/日程/后台任务生命周期对等，再进入 S5 Dyna UI。历史 0.9 不再维护。
