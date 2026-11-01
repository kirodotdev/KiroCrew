# Kiro BYO 0.2.3 · Stage 8 Tool Admission Fencing — 工程候选版

基线：Stage 7 完整交付包中的 `Kiro_BYO_0.2.3-source.zip`。只修改隔离副本，未修改用户电脑中的 KiroCrew 与数据。继续使用同一公开版本号 0.2.3，通过独立 build fingerprint 区分构建。

## 本次确认并修复

1. Stage 7 的 Run Lease 能保护所有权，但同一 Run 同一步骤的工具批次在实际发出执行前，没有 **持久化的唯一准入记录**。Stage 7 源码可在同一 Run 中重复记录同一个 tool batch 准入；本次先编写失败测试，再修复。
2. `DurableRunStore.checkpoint()` 在同一跨进程事务内登记 `tool_batches[opaque_batch_id]`，并拒绝重复 `tool_batch_admitted`。准入必须具备活跃 Run、活跃 Step、活跃 Attempt 与有效租约；每个 Run 限制最多 2048 个登记批次，超限拒绝新执行而不删除历史证据。
3. `tool_batch_completed` 必须匹配已登记的批次、Step 和 Attempt，禁止无来源完成和重复完成。已登记却未得到完成回执的 Run 不能被标为成功完成。
4. 执行入口生成 `SHA-256(run_id + step_id + logical_round)` 的不透明稳定批次标识，不持久化工具参数、工具名称、模型提示词或密钥。工具批次原有风险标记 `side_effect_seen` 保留；无法确证的外部写入仍拒绝自动恢复。
5. 新增线程竞态、独立 OS 进程竞态、租约/步骤/执行尝试、迟到完成、成功终态和敏感内容排除回归。CI 加入 Linux/macOS/Windows 以及 Python 3.11/3.12/3.13 的专项矩阵（未在远程 CI 实际运行）。

## 范围与安全边界

- 这不是 HTTP/MCP 外部工具的端到端幂等保证。批次记录保证的是**同一持久 Run 中的本地准入去重和账本闭合**。远端系统收到请求后是否产生副作用，仍需其自身幂等键/状态查询/人工核验，不得盲目自动重放。
- 如果执行成功但落盘“批次完成”失败，该 Run 会保留未解决的准入证据，不能虚报持久化成功。不会删除用户的 Crew 队友、记忆和日程配置。
- 没有改写 Crew 原生 ACP 后端优先级、Kiro/Jev 机制、Tiga 模型路由和 Stage 3 的任务 UI 联动逻辑。
- 尚未完成真正的 Windows/macOS/Linux **KiroCrew 安装端到端测试**，也未完成 Crew `v0.10.0-insider.5` 上游完整源码合同核验。禁止宣称正式兼容。

## 测试

- 首轮五项新增断言在 Stage 7 基线上失败；修复后通过。
- 追加跨进程竞争与“未完成回执不得封存成功”测试后，Linux 本地全量 `PYTHONPATH=src pytest -q` **896 passed, 1 skipped, 52 subtests passed**，退出码 `0`，约 34.86s。
- `python -m compileall -q src backend scripts tests` 通过；`python scripts/release_check.py` 通过。
- CI 仅增加跨平台检查入口，实际远程运行与三系统 E2E 仍待验收。

## 与 Stage 7 的源文件差异（源代码树，不含临时缓存）

新增：
- tests/test_stage8_tool_batch_receipts.py

修改：
- .github/workflows/ci.yml
- CHANGELOG.md
- scripts/build_release.py
- src/kiro_byo/durable_runs.py
- src/kiro_byo/turn_runner.py

删除：
- 无

## 下一阶段建议

优先对接可提供幂等语义的外部服务（明确能力声明后才传递请求幂等键），并构建可故障注入的单机/跨进程 MCP 工具测试；保留“结果未知时需人工核验”的安全规则。随后执行三系统真实 Crew 安装、升级、卸载、恢复及原生回归。
