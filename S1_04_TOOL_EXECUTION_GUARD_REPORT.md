# Kiro BYO v0.2.3 · S1-04 工具权限与执行闭环（第一批）

**日期**：2026-10-24  
**基线**：S1-03 Build ID `0.2.3+4af4160ddc1eb032`  
**目标 KiroCrew**：`v0.10.0-insider.4` / `e9bfada`  
**产品版本**：保持 `0.2.3`，使用新构建指纹区分包内容。

## 本批已实施

1. **批准不等于可继续执行**：工具审批完成后、真正派发前再次读取会话的取消与 Steering 信号。若任务已经改变，则发出 `tool.dispatch.blocked`，工具不会启动。原生工具的临时目录授权会按原来的生命周期撤销；目录授权申请结束后也在扩大授权之前二次检查。
2. **MCP 审批/派发 TOCTOU 校验**：在 `session/request_permission` 之后，再次验证相同 public name 是否仍指向原来的 `(server_name, tool_name)`，当前 Saved Agent 是否仍允许，Crew MCP 当前会话是否仍被验证。变更或撤权时拒绝派发，不借用旧审批。
3. **执行副作用 fail-closed**：外部 MCP 自报的 `readOnlyHint`、`idempotentHint` 不再作为“绝对无副作用”的可信依据；在无法由宿主证明时，按 `unknown_write` 保留失败/取消后的上下文以减少盲目重试。
4. **MCP 结果不确定时提示核对**：已派发的 MCP 调用若返回 `Error:`，BYO 向模型明确提示实际效果可能不确定，应核实外部状态后再尝试；同时产生不包含参数正文的 `tool.effect.uncertain` 诊断事件。工具已派发期间发生 cancel/steer 时也产生不确定副作用事件，并回报中断状态，但不宣称撤销外部操作。
5. **单批次重复调用保护**：同一模型输出批次内，重复的 `call_id` 第二次不派发。该保护刻意只覆盖单批，避免将不同回合的相同模型生成 ID 误认为同一次操作。
6. **回归测试隔离**：修复旧会话恢复测试对其他测试组留下的静态方法替身的依赖，避免套件运行顺序决定结果。不是产品运行逻辑修改。

## 修改范围

- `src/kiro_byo/turn_runner.py`：执行前拦截、审批后 MCP 身份核验、副作用分类与回执、批次去重。
- `tests/test_s104_tool_execution_guard.py`：新增 8 项定向测试，涵盖取消、Steering、MCP 授权/身份变化、原生文件写入、失败结果和批次重复。
- `tests/test_crew_capability_sync.py`：测试隔离修复。
- `CHANGELOG.md`、`RELEASE_NOTES_0.2.3.md` 和路线图：仅更新说明与验收进度。

## 验证结果

- 本地完整 `pytest -q`：**736 passed, 1 skipped, 52 subtests passed**。
- Python `compileall` 通过；发布检查、压缩包运行时 SHA 核对单独运行。
- 未在真实 Windows Crew 中验收。这不是完全符合官方 Harness 权限/工具契约的证明。

## 未完成的边界（重要）

- 已派发的 MCP RPC 无统一的端到端强制取消保证：远端可能已经发生文件修改、消息发送等不可逆动作。
- 当前没有跨进程/重启的通用外部工具幂等令牌与 ACK 协议；`call_id` 批内去重不能提供全局 exactly-once。
- MCP 工具真实效果的分类仍保守；待 Crew 提供可验证的工具效果/授权协议后，可针对真实只读工具精细化，而不是采信 MCP 注解。
- 需要在 Windows 通过拒绝、撤权、取消、Steering、网络中断、真实外部副作用逐项验收。
- Crew 全设置热同步和全功能 Saved Agent / 队友 / 后台任务对等仍在后续阶段。

## 安装与回滚

- 本轮只更改 BYO App 自身运行时，不修改 Crew 集成补丁模板、Crew 数据库或 App ID。通常替换 ZIP、完整重启 Crew 即可；不需要重新注入 Crew 补丁。
- 保留 `S1-03` 源码与安装包作为回退点；不删除现有 Crew 任务、队友或会话数据。
- 升级后先在隔离工作区验证有副作用的工具调用，尤其是自动化外部操作。
