# Kiro BYO 0.2.3 · Stage 7 — 跨进程 Run Lease / 崩溃恢复安全

日期：2026-10-25。**基线：Stage 6 完整 ZIP 中的 Source ZIP**；原 Stage 6 包保持原样。本版本是独立工程候选版，不是已完成三平台实机验收的正式版。

## 目标与边界

解决两个独立 Harness 进程可能同时认为自己拥有同一个 Durable Run 的问题。每个新 Run 在原子账本里登记不可预测的 owner token、generation 和过期时间。该令牌不向 ACP/管理后台暴露。租约判定在现有跨平台 advisory file lock 内进行，而不是 UI 或单进程内存锁。

- 新运行 Run、Step、Attempt、Checkpoint、完成和状态写入都执行租约令牌+有效期校验。旧 token、过期 token 或无 token 均被拒绝，并且不递增持久化 revision。
- Run 所有者每 20 秒续租，续租过期或失败会阻止之后需要关键前置 Checkpoint 的工具批次继续执行。终态后释放租约有效期；失败记录需依现有 RecoveryPolicy 进行显式恢复。
- 另一进程在 `session/load`/`session/new` 看到活跃租约时，不把仍在运行的 Run 误标记为 orphan；对并发续租/恢复竞态采用账本内二次检验。
- 仅租约**过期**且经 RecoveryPolicy 允许时，显式恢复可以生成 generation+1 的新租约。两个竞争恢复申请最多一个成功，旧执行者的任何迟到写入均被拒绝。
- 已见/可能发生外部副作用的 Run 保持 `unknown_side_effect`，不会因租约过期而被自动重试。旧未租赁历史运行记录仍支持兼容性恢复；恢复时会纳入新租约。
- Run 诊断及后台仪表板仅返回 generation、active、expires_at；不返回 owner token。自动历史修剪继续仅删除 `completed` 状态；显式 `clear()` 保持 Stage 6 的原有接口语义，不在本阶段修改。

## 修改范围

- `src/kiro_byo/durable_runs.py`：原子租约验证、续租、代际 fencing、受保护的恢复和原有历史修剪、公开诊断投影。
- `src/kiro_byo/turn_runner.py`：实际回合租约签发、心跳、每次账本写入的 owner token、安全工具准入和完结释放。
- `src/kiro_byo/acp_server.py`：会话恢复时尊重其他进程的有效租约；对外去除令牌。
- `backend/routes.py`：后台诊断接口去除租约令牌。
- `tests/test_stage7_run_lease.py`：原子所有权、活跃租约拒绝、过期接管、迟到写入、并发争抢、子进程及诊断去密等。
- `.github/workflows/ci.yml`：新增 Linux/macOS/Windows × Python 3.11/3.12/3.13 专项矩阵。

## 验证和明确未完成项

新增测试在 Stage 6 上先复现了旧行为：前 5 项失败、1 项旧兼容测试通过。实现后相关测试与两批 Python 回归通过。最终计数、命令及退出码以交付 ZIP 内 TEST_RESULTS 为准。Python 编译、JS 语法、release_check、两个子 ZIP CRC 和 SHA-256 会在交付前核验。

**限制：** 这是同一机器/一致文件系统上的 advisory lock + wall-clock TTL 防并发；不同机器时钟偏差和远程文件系统锁语义尚未验证。Lease 防止不安全的账本写入和新工具准入，**不保证已发出的远程 Tool Call 恰好执行一次**，不对已发生的外部操作提供回滚。真正的跨机器分布式 Lease、远端幂等 Key、Crash 后端到端恢复仍需独立设计、真实测试和外部依赖支持。Windows/macOS 的 CI 任务已配置但本地没有这些系统的实际运行结果，Crew 0.10 insider.5 合同和真实三平台安装/E2E 仍是待验收项。

本版未更改 Tiga、Auto/Jev 隐藏、Provider 选择、Crew 双后端原生优先级、Cron 模型及 Stage 3 联动逻辑。
