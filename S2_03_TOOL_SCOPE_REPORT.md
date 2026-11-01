# S2-03｜Saved Agent 工具可见性与会话恢复安全边界

- **版本**：Kiro BYO 0.2.3（新 Build ID，不变更产品 SemVer）
- **上游基线**：KiroCrew `v0.10.0-insider.4` / `e9bfada`
- **源代码基线**：S2-02 原生功能兼容优化源码包
- **验证日期**：2026-10-24
- **验证等级**：本地 Python 自动化与源码契约对照；**未运行 Windows Crew 端到端**

## 与官方契约对齐

官方 `agent-host-contract.md` 在 KAS `customAgents` 表格中规定：传入的 Agent 定义若省略 `tools`，不应默认获得工具。S2-02 的 `AgentProjection.unrestricted` 曾把已激活、但没有 `tools_declared` 的旧 Agent 视为拥有完整工具表面，使得错误投影和旧会话有扩大工具可见性的风险。

另外，`TurnRunnerMixin._active_tools_for_turn()` 仅在发给模型的 schema 中过滤原生工具；此前 `_execute_native_tool()` 对模型返回的调用进行参数和沙箱校验后，未独立核对 Saved Agent 的可见名单。工具可见范围应当是执行约束，而非仅供模型参考的提示。

## 本轮改动

1. `src/kiro_byo/agent_projection.py`：激活的 Saved Agent 只有明确列出的工具可见。`tools` 缺失与显式 `[]` 均不授予工具；普通无 Agent 会话仍按 Crew/本地执行策略提供默认工具。保留 `tools_declared` 元信息用于迁移诊断，但不再因此授予更多工具。
2. `src/kiro_byo/turn_runner.py`：原生工具执行函数通过 `session.native.specs()` 计算本会话可用工具，并使用 `session.agent.allowed_native_names()` 在发出权限请求和任何实际执行前再次检查。违反 Agent 限制时报告 `refused` 并记录审计，不发生文件写入等副作用。
3. `src/kiro_byo/acp_server.py`：恢复会话时，只要新请求带 Crew `_meta.kiro` 元信息，就将当前 Agent 投影视为权威；即使缺少 `customAgents` 数组，也不从旧会话 JSON 中重新启用历史 Agent。完全不带 Crew 元信息的旧 ACP 客户端保留兼容式恢复，但不复用自动批准清单。
4. `tests/test_s2_lifecycle_scope.py`：新增四类定向回归，包括真实文件副作用拦截、缺省工具、MCP 自动批准与工具可见性的独立性，以及会话恢复后已撤回 Agent 不复活。
5. `tests/test_agent_projection.py`：调整旧的“缺省工具默认全量”测试，改为官方兼容的 fail-closed 预期。

## 自动化结果

- 针对修改的定向组：`41 passed`。
- 完整 Python 测试：**756 passed、1 skipped、52 subtests passed**，无失败；首次全量测试由于测试夹具顺序造成静态方法绑定错误，修复隔离后重新执行通过。
- 无 Windows KiroCrew 真实队友/日程/后台任务 E2E 结果；不能据此认定 S2 已完全对等。

## 行为兼容提醒

这是一次有意的权限收紧。如果旧 Saved Agent 定义只给出 Prompt、没有 `tools`，升级后不再自动拥有默认工具。需要使用工具的 Agent 应由 Crew 的受信会话投影显式声明（例如 `read`、`@kirocrew-core/browser`）。不建议在 BYO 内部伪造或扩大清单。

## 后续待办

- 验证 Crew 对实际成员、定时任务、后台角色使用的会话元信息及投影完整性（Windows A/B）。
- 补齐 Saved Agent 其他尚未传递的字段及其明示的降级提示；无真实实现前继续不声明完整 `member_saved_agent`。
- 核实任务取消/恢复时的工具权限、日程生命周期与 MCP 结果归属。
- 继续保留 Tiga 四形态及其动态选模、Dyna 三形态禁用预置、不引入 Jev。

## 参考

- https://github.com/kirodotdev/KiroCrew/blob/e9bfada/docs/system-specs/modules/agent-host-contract.md
- https://github.com/kirodotdev/KiroCrew/blob/e9bfada/docs/system-specs/modules/harness-parity.md
