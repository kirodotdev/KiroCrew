# Kiro BYO 0.2.5 — ACP V3 能力契约 / MCP 故障安全专项

## 基线与范围

- 输入基线：`Kiro_BYO_0.2.5_Crew_Model_Names_Fix_Source.zip`，原构建 `0.2.5+c092f914223b5453`。
- 本次重点：可直接落地、无需 KiroCrew 宿主新增接口的协议契约与 MCP 故障保护。
- 不启用 Dyna，不调整 Tiga Type / BYO Provider 路由、不重构 Crew、UI、工具授权或 Durable Run 存储。

## 已确认的缺陷与改动

1. **未可信化的 MCP 异常原文进入模型侧**。原 `McpManager.call_with_status()` 对远程 `McpError` 直接返回 `f"Error: {exc}"`，含服务端 JSON-RPC 文本及 URL 的异常因此会进入 ToolResult、Agent 上下文和可能的运行追踪。现对已经派发的请求返回不含第三方细节的固定错误类别文本；继续标注 `is_error=True`、`dispatched=True`，不把远端失败误判为未调用。
2. **MCP 交互输入缺少清晰的分类**。原 MCP 2026 `input_required` 与其他协议错误都使用 `McpProtocolError`。新 `McpInputRequired` 子类保留明确的原因识别，但不将原始 `inputRequests` / `requestState` 附给模型；由于 Crew 交互投影尚未实现，继续失败关闭，不能自动重试未知副作用调用。
3. **ACP 公开能力缺少自动映射校验**。新 `scripts/acp_capability_audit.py` 从实际 `AcpServer.handle()` 的 AST 读取分发方法，与 `initialize` 能力和运行时自检结果比较，审计将 Crew 私有和旧兼容方法与外部可发现方法明确区分。发布检查增设阻断项，防止声明了未实现的方法，且不会为满足清单而自动扩大公开 API。

## 测试重点

- 带令牌的远程 URL、JSON-RPC 异常、MCP 协议异常、`requestState` 等不再进入模型可见错误信息。
- 工具已发出但失败时，Durable/Trace 路径继续形成 `unknown_write` 的保守收据。
- ACP `session/fork` 未实现，未知方法以 JSON-RPC `-32601` 拒绝；`_kiro/run/*` 等 Crew 私有扩展保留兼容，但不自动公开。
- 原有 MCP `isError`、流式返回、Provider/Tiga 以及会话恢复逻辑维持原有测试覆盖。

## 非目标与实际限制

- 不支持 MCP Elicitation 交互表单/OAuth 以及跨回合多轮输入；这要求先确认 KiroCrew 宿主能力与受控安全状态机。
- 不实现 Windows Shell OS 沙箱；当前 sandbox 的工作区约束不等于完整操作系统隔离。
- 不实现 `session/fork` 或复制官方 CLI 的内部 Workflow。
- 源码测试无法代替 Windows KiroCrew `0.11.0-rc.5` 的真实环境与权限测试。

本报告应与本次构建生成的 `BUILD_INFO.json`、测试汇总及 SHA256 校验文件共同使用。
