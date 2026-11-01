# Kiro BYO v0.2.3 — S3-02 事件关联与恢复诊断报告

日期：2026-10-24  
唯一源码基线：S3-01 Durable 恢复安全版  
目标 Crew：`v0.10.0-insider.4`（`e9bfada`）  
产品版本：`0.2.3`（构建指纹由 `BUILD_INFO.json` 单独标识）

## 目标与不变约束

不新增第二套任务持久化状态：Session Event Log 仍然是对话语义的依据；Durable Run JSON 仍是恢复控制平面；Runtime Trace 是有保留上限、可关闭的诊断信息，**不可用于推断没有发生过副作用**。保留 Crew 管理权、Tiga 四形态、模型路由、Dyna 不可执行及原有 UI。

## 已实施

1. **Runtime Trace 跨回合事件可检索性**：在 `session_id` / `turn_id` / `run_id` 过滤查询中扫描整个**已有保留上限**，避免长任务的早期事件因查询 limit 很小而丢失；不会扫描无限历史，也不扩大日志存储。
2. **统一关联键**：运行恢复请求与拒绝使用顶层 `run_id`；检查点追踪带上持久化写入时的精确 `checkpoint_event_seq`，并保留现有 `run_id`、`step_id`、`attempt_id`、`turn_id`。
3. **恢复拒绝原因码**：策略拒绝、用户事件锚点缺失或不符、输入无法恢复等路径记录结构化、非正文的拒绝原因；诊断日志写入失败不应导致 ACP 执行额外失败。
4. **只读 Run 诊断视图**：新增 `run_diagnostics.py`，组合既有 Durable Run + RecoveryPolicy + 可选 Runtime Trace，提供最新 Step/Attempt、最多 32 个历史尝试、用户/会话事件锚点、是否存在副作用风险、`resume/retry/replan` 允许状态和理由。
5. **Run/Get 诊断**：现有 `_kiro/run/get` 响应新增 `diagnostics`，携带最多 40 条**去正文**事件摘要；Run/List 新增轻量诊断，但不为每条运行记录反复扫描日志。旧字段继续保留。
6. **权限/重试归属**：诊断显示恢复需要调用方显式请求，绝不把“已记录请求”当作“已经完成恢复”。在真正执行入口仍要重新计算 RecoveryPolicy。

## 证据与限制

- 定向测试：`tests/test_s3_02_run_diagnostics.py`、既有 `test_runtime_trace.py`、`test_durable_recovery.py` 等，覆盖长日志稀疏关联、去正文、失败恢复拒绝、无 Trace 时仍遵守持久化风险证据。
- 全量 Python 自动化：**776 passed, 1 skipped, 52 subtests passed**；`scripts/build_release.py` 发布检查通过，安装包/源码包共有运行文件一致。
- Trace 是有界且可关闭的：**查询不到记录既不说明没执行，也不构成允许重试的证据**。
- 无跨进程 exactly-once、真正已发送 MCP 调用的撤销、掉电级 fsync 保证、Windows Crew 端到端验收。
- `run/get` 仅提供诊断，不安排自动重试，不更改既有运行中任务的身份和模型。

## 下一阶段建议：S3-03

做故障注入和恢复状态机一致性检查：Durable 写入中断、ACP 流断连、Crew 发起取消后远端工具完成、恢复中再次断连。之后才开始 S4 Mode Registry 插件化；Windows 实机集中验收继续保留未完成标记。
