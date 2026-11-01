# S2｜KiroCrew 原生功能兼容性矩阵（2026-10-24）

**产品版本**：Kiro BYO 0.2.3（同版本新 Build ID）  
**对照基线**：KiroCrew v0.10.0-insider.4 (`e9bfada`)  
**前序基线**：S2-01（Tiga 动态路由和记录可解释性）  
**结论等级**：已实现且本地可自动验证 ≠ Windows Crew 实际端到端对等。

## 不可改变的产品约束

- KiroCrew 管理成员、日程、队友、Skills、浏览器与权限；BYO 仅作为平级 ACP Harness 接入。
- Tiga `Mode → Type → Model` 保留 Sky / Multi / Power / Glitter，仍根据策略和任务动态选模，不采用随机轮换，也不引入 Jev。
- Dyna 继续预置 Flash / Strong / Miracle，但禁止执行；不改变模式页的一屏布局。
- 禁止自动加载另一名 Saved Agent 的 Prompt / 权限；不得因格式错误而退化成默认全工具模式。
- 不自行生成 Crew 原生权限或 Skills/Memory/日程数据。

## 分项验收矩阵

| 模块 | 源码路径及本轮证据 | 现阶段结论 | Windows A/B |
| --- | --- | --- | --- |
| Saved Agent 身份选择 | `agent_projection.py` 对 `modeId` 精确匹配；多 Agent 未指定活动 ID 则拒绝 | 本轮加固、本地测试 | 未验证 |
| Saved Agent Prompt、工具可见性 | 会话投影 + `turn_runner.py`；允许有意为空的 Prompt 仍执行工具限制 | 已有实现 + 本轮边界修复 | 未验证 |
| Saved Agent `allowedTools` | 来源必须是当前 Crew Session；跨恢复不持久化自动批准 | 已有保护、回归通过 | 未验证 |
| `excludedTools` 和 MCP 白名单 | 支持工具名、@server、@server/tool、通配排除；调用前校验 | 本轮补齐，单测覆盖 | 未验证 |
| Skills | 仅从已验证的 `kirocrew-core/skill_search` 显示工具；仍受 Agent 白名单和 Crew 审批约束 | 本轮补齐发现链路 | 未验证 |
| 内置浏览器 | S1-01 live Crew MCP browser 直接暴露，不从缓存伪造授权 | 已有代码保护 | 未验证 |
| 队友/子 Agent 分派 | ACP `/agent/*` 和隔离任务池；工作区角色隔离 | 已有实现，相关回归通过 | 未验证 |
| 日程/后台任务 | Crew 调度持有日程数据，BYO 消费投影的会话和 MCP；不自建 Scheduler | 接口已存在；实际角色/重试/取消待对照 | 未验证 |
| 工作区和附加目录 | `cwd`、`additionalDirectories` 按 ACP 会话校验；恢复不读取旧权限当新授权 | 已有代码保护 | 未验证 |
| 记忆、附件与历史 | Crew MCP 按需发现；ACP 多轮与图像输入/历史投影回归 | 已有基础；复杂附件待对照 | 未验证 |
| 上下文压缩/恢复 | `/compact`、持久事件和 session/load；不能自动恢复所有外部副作用 | 已有实现；外部恢复待对照 | 未验证 |
| 流式 Thinking/状态/语言 | ACP session/update、已修正简繁与可见推理语言 | 代码级测试存在 | 未验证 |
| 模型目录与形态 | S2-01 路由证据、能力筛选、Sticky Pin 执行池限制 | 已有实现 | 未验证 |

## 本轮实际改动

1. **身份不能猜**：`modeId` 指定但未收到对应 Saved Agent 时，返回 ACP 参数错误；多项 Agent 定义缺少活动身份时也拒绝，而不是按列表顺序选择。
2. **权限投影必须可解析**：错误的 `customAgents` 结构、非字符串工具清单和错误 Prompt 类型拒绝创建会话；校验在初始化 MCP 服务器前进行。允许空 Prompt，但保留显式空工具列表的限制。
3. **通配模式与源隔离**：MCP 允许/排除可准确匹配 `@server/tool_*` 与 `mcp__server__tool_*`；原生工具排除支持别名和 glob；原生 `read` tag 不得误授权第三方同名 MCP 工具。
4. **Skills 发现对等**：已验证且被 Crew 当前会话授予的 `kirocrew-core/skill_search` 不再完全依赖模型猜搜索关键字；第三方同名工具或离线缓存不会被提升为 Crew 核心能力。
5. **保留所有先前改造**：S1-01～S1-04、S2-01、语言修复与禁用 Dyna，不调整 Tiga 权重或额外注入决策模型。

## 未完成或不能据此宣称对等的事项

- 不能把 Saved Agent 的全部原生字段（例如引擎专属资源、Hooks、工具设置）假装映射完成；当前 BYO 未宣布 `member_saved_agent` 完整能力。
- 实际 Windows KiroCrew 下成员模板切换、独立模型绑定、Schedule 触发/重试、持久记忆读写、Skills 内容加载、浏览器面板联动仍未逐项确认。
- 仅靠 ACP `session/new/load` 的 MCP 清单不能感知 Crew 未投影的所有设置；不宣称热同步已彻底解决。
- S2 需要真实官方/BYO A/B 执行记录作为最终验收证据；本轮只属于 **S2 的代码与自动化测试完成一轮加固**。

## 后续 Windows A/B 必检场景

1. 相同 Agent 模板使用官方 ACP 与 BYO：验证 Prompt、工作区、工具白名单、排除项及模型选择一致。
2. 队友创建→配置→私聊→分派→取消→重启恢复，比较两条 Harness 的记录和副作用。
3. 新建日程→自动触发→失败重试→取消，核对 Crew 任务 ID、工具调用与真实执行 Provider。
4. Skills 可见→搜索→执行；切换工具开关后不要求重装 BYO；禁用时无调用。
5. 内置浏览器、记忆、文件附件、上下文压缩、Steering / Queue 同任务对照。
6. 任一权限被拒或撤回后检查：不能换另一工具绕过；会话不能恢复被撤回的旧授权。

## 参考

- 上游固定版本 `v0.10.0-insider.4` 的 `docs/system-specs/modules/agent-host-contract.md` 和 `acp-client.md`。
- 工程路线图 `Kiro_BYO_Harness_Parity_升级路线图_v1.5_执行记录.md` 以及本轮附加进度。
