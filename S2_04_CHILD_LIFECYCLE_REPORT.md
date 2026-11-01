# S2-04｜BYO 子 Agent 并发恢复与取消状态一致性

- **产品版本**：Kiro BYO `0.2.3`（同版本新 Build ID）
- **代码基线**：`Kiro_BYO_0.2.3_S2-03_Agent权限与恢复优化_源码包.zip`
- **Crew 对照版本**：`v0.10.0-insider.4` / `e9bfada`
- **验证范围**：本地源码、模拟并发测试和完整 Python 自动化；Windows Crew 实机尚未验证

## 发现的问题

BYO 内部的 `_kiro/agent/resume` 在目标子任务仍在运行时，原先可能再次调用 `AgentRuntime.run_group()`，向执行线程池提交同一个 `task_id` 的第二个 Future。这会让同一子任务生成两个执行实例，竞争上下文、状态回执和工具执行。独立的 `AgentRuntime.submit()` 也没有在任务已经有未完成 Future 时拒绝二次提交。

这与 Crew 原生队友/日程调度不是同一层概念。本轮修复范围是 BYO 自身的子任务，避免未来后台调用路径重用时带入此类并发错误。

## 改动

1. `src/kiro_byo/agent_runtime.py`：为每个 `AgentTask` 增加独立的非持久化恢复准入锁；Future 查询与注册在同一把 runtime 锁下完成，重复运行抛出 `AgentTaskBusyError`。
2. `src/kiro_byo/turn_runner.py`：恢复请求必须先取得任务执行准入；如果已有任务在执行，新的文字进入现有 mailbox（只有下一模型边界或后续明确恢复时才会消费），不会启动第二个 Worker；取消已发起时不得接受新的执行引导。
3. `src/kiro_byo/acp_server.py`：原有任务恢复控制平面增加 `inProgress` 和 `messageQueued` 标志，并把 `running` 作为非最终结果返回；不再让追加消息假装成已经执行完成的结果。
4. `src/kiro_byo/agent_runtime.py`：已排队但未启动的 Future 被取消时，`run_group` 明确报告 `cancelled`，不再误判为 Provider `failed`。
5. 新增 `tests/test_s2_04_child_lifecycle.py`：覆盖重复提交、并发恢复、ACP 回执、取消后的新增消息拒绝和排队 Future 取消。

## 本地测试证据

- 定向回归：`17 passed`（S2-04、既有 AgentRuntime、Subagent 和 S2 作用域测试）。
- 完整回归：**761 passed, 1 skipped, 52 subtests passed**。
- 重新打包后检查发行 / 源码 ZIP 的完整性、运行文件一致性与 Build ID。

## 未解决项与注意事项

- **此处的子 Agent 不等于 Crew 的队友（Member）或日程调度器。** Crew 原生任务、日程触发、角色模型和后台会话的真实执行与状态回传仍缺少 Windows 对照证据。
- `messageQueued=true` 仅表示进入 BYO 子任务的进程内 mailbox；不保证模型已经处理，也不意味着跨进程持久化。任务若在下一个模型边界前退出，消息要等后续恢复才能消费。
- 一旦 MCP/外部工具已发出请求，取消不能保证撤销副作用；跨进程 exactly-once 需要独立的持久化执行/幂等机制。
- `member_saved_agent` 仍不应宣称完整支持，且本轮不更改任何 Crew 源码集成补丁或数据库。
- Tiga Mode → Type → Model 四形态不变，Dyna 三形态继续禁用。

## 后续工作

1. 继续按 S2 验证 Crew Member/日程真实 session metadata 与 BYO Agent Projection 的完整程度。
2. 对 Crew 后台角色、取消与恢复、工作区和 MCP 权限开展真实多进程 E2E。
3. 满足这些条件后才更新相应官方能力声明；随后进入 S3 的 Durable Run、事件回放与故障恢复。

回滚：使用 S2-03 安装包与源码包恢复原构建（保留外部用户数据），不要删除 Crew 会话/队友/日程。
