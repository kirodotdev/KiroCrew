# Kiro BYO 0.2.3 — S0 基线核对 + S1-01 浏览器接入修复报告

**日期**：2026-10-23  
**版本显示**：始终 `0.2.3`；采用唯一 `BUILD_INFO.json` Build ID 辨认此构建。  
**对照 Crew**：`v0.10.0-insider.4` / `e9bfada`（源码层固定目标；**非** Windows 运行验证）。

## 1. 基线检查

两份用户提供的 ZIP：

- 安装包 `Kiro_BYO_v0.2.3_Language_Fix.zip`：SHA-256 `92cf6b47e06110f0a84cfc4f903e50aa615f419d2d3aa0c5f0cc9d31156b6582`
- 源码包 `Kiro_BYO_v0.2.3_Language_Fix_Source.zip`：SHA-256 `e4d033a0a83bf9790c464f626a644700f31a2543033784d9a6b449a58d5f7bf8`
- 两者共有 **108 个非目录文件**，所有共有文件的 SHA-256 **一致**；安装包不含源码包之外的额外非目录文件。因此选择上传的 Language Fix Source 为唯一规范基线。
- 与此前的 `模式单页布局修复_源码.zip` 比较，210 个共有文件中有 12 个不同，差异集中于语言逻辑、语言文案和相应测试；与 `总览排版修复_源码.zip` 相比有 15 个不同。未发现需要从旧源码包反向覆盖的新功能。
- 原有 Crew 0.10 自适应补丁、Tiga 四 Type、Dyna 三 Type 禁执行、模式一屏布局和语言文件均保留；未删除用户配置，也未更改现有 Mode/Type ID。

## 2. 实施变更（S1-01，仅浏览器子项）

变更文件：`src/kiro_byo/mcp_client.py`、`src/kiro_byo/turn_runner.py`、`src/kiro_byo/acp_server.py`。

1. `McpManager.crew_browser_tool_names()` 仅识别 **Crew 注入的 `kirocrew-core` MCP**，且工具名精确为 `browser`。此函数不伪造未授权工具，不读取 Crew 配置文件、Token 或 Cookie，也不把第三方同名工具提升为原生浏览器。
2. 若宿主已经授予此 `browser` 工具并允许 Agent 看到，BYO 每一模型回合直接将其 Schema 放进工具表。其他一般 Crew MCP 仍通过 `mcp__kiro_byo__tool_search` 延迟发现。
3. 用户明确要求交互浏览器，MCP 目录尚未加载时，最多等待 8 秒完成第一次发现；普通聊天不为此等待。若超时，仍保留原工具搜索途径，不能声称浏览器已经可用。
4. 只有工具确实出现在本回合的已授权工具表时，才告知模型优先使用 Crew 浏览器；**使用内置 Chromium 还是返回 Playwright CLI 回退建议完全由 Crew `browser` MCP 决定**，不在 BYO 实现平行回退或越权路径。
5. 保持已有会话的 `loaded_tool_names` 与限制不变；不新增模型路由或 Jev，不更改 Tiga、Dyna、Steering 状态机、工具审批执行路径。

参照官方文档：
- https://github.com/kirodotdev/KiroCrew/blob/main/docs/architecture/mcp.md
- https://github.com/kirodotdev/KiroCrew/blob/main/docs/system-specs/modules/browser.md

**注意**：这些文档当前 `main` 分支可能包含晚于固定 Crew Insider Tag 的内容，真正的兼容仍要在 Windows 目标构建中验证。

## 3. 测试

- 新增 `tests/test_crew_browser_parity.py`，验证：原生工具优先可见、Saved Agent 工具排除、第三方同名工具不提升、冷启动目录等待、运行时提示只在授权工具可见时注入。
- 与已有 MCP 和 ACP 控制平面测试合并：`74 passed`。
- 完整测试：**`715 passed, 1 skipped, 52 subtests passed`**；`0 failed`，约 25 秒。`python scripts/release_check.py` 通过。
- 发布脚本在安装 ZIP 和源码 ZIP 中写入一致的 `BUILD_INFO.json`，并生成 `SHA256SUMS.txt`；保留 UI、Core、ACP Agent `0.2.3` 版本。

## 4. 仍未解决及不能声称的事项

- **未做 Windows 桌面真实测试**，不声称已彻底解决“开了 Crew 内置浏览器但 BYO 没控制面板”的用户原始故障。
- 当前方案不能从 ACP 标准本身直接读取所有 Crew Dashboard 设置；工具是否可用以宿主的会话授权与 MCP 工具实际响应为准。Crew 配置同步、工具热重载与运行时能力快照为后续 S1 子项。
- Steering/Queue 的“真正消费确认”、断连补发和幂等边界尚未修复；沿用原 0.2.3 实现，作为下一张 S1 工单单独处理。
- Saved Agent 完整规格、队友、日程、权限门控、模型和上下文的 Windows 功能对照仍未验收。
- 目前不推进 Tiga 独立插件化、Dyna 执行、Jev 决策模型或重构 UI。

## 5. 升级、验证与回滚

1. 备份当前 BYO 安装与 Crew 配置/数据；记录已有 BYO 包 Build ID（没有 Build ID 的旧包用 ZIP SHA-256 标识）。
2. 导入本报告对应的**完整安装 ZIP**，保持 App ID 和展示版本 `0.2.3`。由于同版本覆盖可能缓存资源，彻底退出 Crew 后重新启动。
3. **不需要为单纯 BYO MCP 发现改动重新向 Crew 注入集成补丁**；如果完整替换 App 后 Crew 的适配器状态异常，使用 BYO 自带 `Install / Update integration` 做语义检查与受控更新，先查看备份，不要手动覆盖 Crew 核心。
4. 按 `CREW_PARITY_WINDOWS_RUNBOOK.md` 在 Windows 执行 E01、E02、E03、E04、E06、E15，重点确认 `kirocrew-core/browser` 及 Steering 消费事件。
5. 失败时先保存脱敏日志，停用 BYO 并切回官方；从备份恢复原 App，必要时在 BYO 集成管理器执行原生校验与回滚。**不要删除 Crew 队友、日程、会话数据库。**

## 6. 当前阶段进度

| 工单 | 进度 | 结论 |
|---|---|---|
| S0-01 A：唯一源码和安装包一致性 | 已通过静态核验 | 可以使用新 Build ID 继续开发 |
| S0-01 B：可重复打包与全量单测 | 已通过本地验证 | 仍需 Windows 环境记录 |
| S0-01 C：原生对照与已知故障复现 | **Blocked** | 无用户 Windows Crew 运行环境/脱敏事件日志 |
| S1-01：内置浏览器正确发现 | **代码与定向测试通过** | 真实 Crew MCP 与界面联动待验收 |
| S1：设置快照、Steering、权限闭环 | 未完成 | 后续迭代 |
| S2-S6 | 未开始 | 保持路线图既定顺序 |

本次升级是**可使用的分阶段候选构建**，而不是声称全功能 parity 已实现。
