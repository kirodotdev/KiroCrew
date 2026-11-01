# 第六阶段：MCP 工具调用闭环与结构化结果修复

**审查基线：** `0.2.5+aa260d8c7781f241`（RC3 候选）  
**修改范围：** `src/kiro_byo/mcp_client.py`、`src/kiro_byo/mcp_tool_dispatch.py`、新增回归测试、版本说明。  
**保持不变：** Mode / Type / Tiga Sky/Multi/Power/Glitter、具体模型直连、ACP server、Mate/Worker 路由守卫、Crew 源码注入、Cron 持久化逻辑。

## 已修复

1. **F-01 — MCP 非 OSError 异常造成 `in_progress` 悬挂。** 已派发后的普通 `Exception` 现在进入同一失败终态，产出副作用不确定事件，不自动重放。对外错误仅包含异常类别，避免泄露 URL、路径及凭据。原先的 `OSError` 报错措辞保留。取消和 steer 的派发后状态逻辑保持不变；`KeyboardInterrupt/SystemExit` 仍按系统中断机制传播。
2. **F-02 — 成功的 `Error:` 文本误判失败。** 新增 `McpCallOutcome(text, is_error, dispatched)`，`McpManager.call_with_status` 使用 MCP 协议的 `isError: true` 标志；文本不参与状态判定。`McpManager.call` 仍返回旧版 model-facing 字符串以兼容其他调用者。已发起但失败的调用继续提示外部结果未知；派发前失败不会虚报远端副作用。
3. **旧代码兼容。** 对会话实例显式覆盖 `call()` 的旧适配器和测试夹具，调度器保留字符串调用通路（其返回值不能传递协议状态，仍有历史文本推断限制）。实际产品的标准 `McpManager` 使用结构化结果。

## 回归与验证

- 新增 12 个测试，覆盖协议成功/失败、正常 `Error:` 文本、MCP 已派发错误、派发前拒绝、普通异常脱敏/不重试、系统中断。
- **122 个测试文件分组验证：1068 passed、1 skipped、52 subtests passed**；连续一次性执行测试曾超时并出现旧测试桩拦截不兼容，随后修正兼容逻辑、按文件分组重新全部通过。不是 Windows E2E。
- Python 编译、JS 语法、release check、源代码/发布包完整性检查另行执行。
- 重点比对 `mode_router.py`、`protocol.py`、`acp_server.py`、`policy_packs/tiga.json`、`crew11_patch.py`，与 RC3 源码包逐字节一致。

## 未解决与不夸大的边界

- Crew 原生 Cron 页面与 BYO 编辑器的跨界面原子 CAS 仍需要上游事务支持，不得绕开 Crew 服务写内部数据。
- RC3 新版 Crew 的真实安装目录、Mate/Worker、模型菜单及 Windows E2E 未验证；通过测试并不证明 Crew RC3 已经兼容。
- 对只返回文本的第三方/测试 MCP 客户端，无法从字符串无损推断协议错误状态；建议改为 `call_with_status()`。
- 此次不大规模重构 `turn_runner.py` / `acp_server.py`，优先降低执行正确性风险。
