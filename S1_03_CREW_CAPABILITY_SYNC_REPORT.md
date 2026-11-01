# Kiro BYO 0.2.3 — S1-03 Crew 能力快照与会话同步

**记录日期：** 2026-10-23  
**目标 Crew：** `v0.10.0-insider.4`（`e9bfada`）  
**状态：** 代码实现 + 定向测试通过；Windows 端到端未验证，Crew 全部设置热同步尚未实现。

## 设计边界

KiroCrew 通过 ACP v1 的 `session/new`、`session/load`、`session/resume` 提供会话 MCP Servers、工作区根目录和 Agent Projection。当前通用 ACP v1 没有推送全部 Crew Dashboard 设置的标准实时接口。本轮**不读取私有 Crew 设置文件、不猜测开关、不伪造浏览器或工具授权**，只根据宿主真正投影的 MCP 服务器和会话 Agent 限制工作。

## 已实施

1. **会话级实际授权与缓存目录区分**：`McpManager._live_verified_servers` 由当前会话内 MCP Broker 成功启动/发现后记录；磁盘缓存中存在某工具的 Schema 不再被视为当前会话已获得该工具。Crew 内置服务器启动失败时，从实际模型可见列表中移除对应工具，不回退展示未认证的缓存。
2. **工具调用二次校验**：真实 MCP 执行前验证当前 Agent 的工具白名单和 Crew MCP 的会话验证状态；被撤回或未验证的工具明确拒绝，而不是让模型尝试其他未授权路径。
3. **Crew 能力快照**：创建不含 Session Token、HTTP Header、命令行和用户提示词的临时快照：已投影服务器、已验证服务器、等待确认服务器、失败服务器、当前可用工具名以及 Crew `browser` 是否可用。变化时写入运行时诊断事件；不保存成另一套权限数据库。
4. **工具目录更新及失效清理**：对已验证的 Crew stdio Broker 在回合边界以间隔限制刷新 `tools/list`（默认间隔 60 秒，单个刷新过程有限时）；删除已失效的 Deferred Tool 引用。工具列表刷新失败会撤销该 Server 的已验证状态，直到再次以当前会话身份成功验证。
5. **浏览器首回合鉴权等待**：显式浏览器交互任务等待真实 Crew Core MCP 初始化，不能再被缓存中的“目录已就绪”误导。继续由 Crew 决定内置浏览器面板或降级方式。
6. **Saved Agent 重加载保护**：`session/load` 明确收到新的空 Agent 列表时，不使用旧会话文件中残留的 Saved Agent 配置；新投影优先，授权不来自持久化历史。
7. **内部兼容**：继续支持旧的 ACP/测试工具桥接口；没有动 Tiga、Dyna、Provider 策略或 Crew 集成源码模板。

## 代码范围

- `src/kiro_byo/mcp_client.py`：已验证会话能力、快照、刷新、失效保护。
- `src/kiro_byo/mcp_transports.py`：stdio MCP 已验证会话下的限时 `tools/list` 刷新。
- `src/kiro_byo/turn_runner.py`：回合前同步、工具名单过滤、权限二次检查、浏览器首回合验证。
- `src/kiro_byo/acp_server.py`：仅临时能力快照指纹；显式撤回 Saved Agent 时不恢复旧配置。
- `tests/test_crew_capability_sync.py`：新增七项相关回归；`tests/test_crew_browser_parity.py`：更新浏览器验证边界。

## 核对结果

- **132 passed / 0 failed**：MCP、Crew 浏览器、控制平面、ACP v3、Agent Projection、Turn Runner 回归等定向测试。
- `compileall` Python 编译检查通过。
- `release_check.py` 的版本及产品元数据校验通过。
- **尚未执行全量测试；未在 Windows Crew 真机验证。** 不把 S1 全阶段宣称完成。

## 仍需解决

- 用户在 Crew Dashboard 改动的全部设置，不会自动被当前 ACP v1 Session 推送；需要 Crew 提供实际变更通知/重新投影机制或通过 `session/load`、新会话明确刷新。当前刷新只覆盖已存在的授权 MCP Broker `tools/list` 目录。
- 当前基于间隔的 Tool Catalogue 刷新存在小范围延迟，且 MCP 异常时可能暂时隐藏工具；不能据此保证“任意设置实时生效”。
- 下一步 S1-04：工具执行审批状态机、取消/中断时副作用边界、拒绝与失败恢复；S2 再做 Saved Agent/队友及其它 Crew 功能全面对等。

## 回滚与安装

本轮仅修改 BYO 内部 Python 运行时。更新 App ZIP 并完全重启 Crew，通常不需要重新注入现有 0.10 Crew 集成补丁。保留上轮 S1-02 安装包可回滚。请勿删除 Crew 的队友、日程、会话数据。

**补充：** 对 Crew MCP 目录刷新失败的会话不永久禁用。失败时立即撤销未验证工具；下一次允许刷新时可重新以同一会话身份握手/查询，成功后才恢复工具可见性。新增失败后恢复测试。
