# Kiro BYO 0.2.3 · Crew 队友原生兼容专项修复（Global Audit Fix 03）

日期：2026-10-24；基线：`Global Audit Fix 02`（Build `0.2.3+08b275fc111e0f39`）。
目标：KiroCrew 0.10，优先审计 `.4`；`.5` 的公开队友选择契约已对照，但 Windows 实机和 `.5` 完整源码合同检查仍待完成。

## 修复项

### 1. 尊重 Crew 原生队友 Harness 选择

文件：`src/kiro_byo/crew_shared_patch.py`、`src/kiro_byo/crew_patch_common.py`。

过去 BYO 注入 `members.py::select_provider_backend` 的早返回：在全局 `agent.acp_backend=kiro-byo` 时，队友私聊默认被强制改为 BYO，忽略 `agent.member_acp_backend`。Crew 官方顺序实际是 **显式 per-session override → member 专用后端 → 普通会话全局后端**。

现已移除该覆盖。新的注入标记不改变原函数结果，旧 BYO 0.2.x 已安装的 owned early-return 可被明确识别为 `upgradeable` 并安全移除；遇到非 BYO 来源、未知旧重写则拒绝猜测性改写。队友模型 entitlements 页面相应的 helper 也按 member 的专用后端解析，旧 helper 允许原位升级。

**使用方式**：普通聊天选 BYO 不意味着队友随之自动切 BYO。要让队友运行 BYO，需要在 Crew 支持的配置中显式选择 `agent.member_acp_backend=kiro-byo`，且通过 Crew 自身的可选/治理检查。反之选择 KAS/Claude 等不会被 BYO 覆盖。已运行中的 Crew 会话是否切换按 Crew 的会话生命周期执行，不能仅改全局配置判断。

### 2. 将 Crew 原生队友会话 MCP 与 Saved Agent 普通工具分开

文件：`src/kiro_byo/mcp_client.py`、`src/kiro_byo/turn_runner.py`、`src/kiro_byo/agent_projection.py`。

Crew 0.10 使用 **会话级** 的 `kirocrew-dashboard`（分派/控制）和 `kirocrew-panel`（队友面板）MCP 作为队友控制平面。这和保存的 Agent 自行声明的 `tools` 是不同的能力来源。旧 BYO 将 Saved Agent `tools` 缺省或 `[]` 时的零工具规则直接应用到全部 MCP，阻断了 Crew 为队友单独挂载的能力。

新的有限例外：

- 只识别 Crew 专用的两个会话服务器名称，以及对应 `mcp-dashboard`/`mcp-panel` stdio 子命令与 `KIROCREW_SESSION_KEY=member-*` 身份承载；同名的普通服务器不获得例外。
- 显式 `excludedTools` 仍优先于会话挂载；不会向 Agent 开放原生 Shell、写文件或任何第三方 MCP。
- 队友 MCP 必须经过 **本会话实际启动握手** 才能出现在可执行工具集合；工具缓存不构成实时授权；首轮对这些服务等待连接完成。
- **未收到完整 Agent 投影但能识别到 Crew 队友会话挂载时**，默认不开放 BYO 原生读/写/Shell 或任意第三方 MCP；只保留 Crew 的会话级控制/面板入口。如果完全没有队友挂载且 Crew 未提供其他成员身份证据，无法仅从标准 ACP 消息可靠判断它是队友，仍需宿主协议对等验证。
- Crew `session/request_permission` 执行前仍是必要门槛；队友专用服务器不使用 `allowedTools` 绕过授权。批准期间如果 MCP 连接或工具已撤回，派发前再次拒绝。
- 两个同名 MCP 声明直接报错，避免后一个影子定义覆盖前一个又借用其准入证据。

Crew 控制平面自身还会检查 session token、身份与权限。这些限制仅针对由 Crew 传入的 session MCP 参数，不表示任意第三方调用获得后台任务分派权限。

### 3. 验证

- 覆盖：member 后端独立选择、显式 override、旧补丁原位升级、Agent 工具约束、会话挂载识别、伪造同名拒绝、live verification、host permission、撤回竞态、重复服务器、面板/分派相互隔离。
- 兼容回归：现有 ACP、Provider、Tiga、Dyna、配置以及 Crew 0.10 语义补丁测试。
- 实际测试数据在本轮发行记录中，不将 Python 模拟用例说成 Windows 端到端已验证。

## 明确未解决的边界

1. BYO **仍未声明** Crew `ACP_BACKENDS_MEMBER_CAPABILITIES`：**不能**称为完整 Saved Agent 规范加载（包括独立模型 pin、完整私有记忆和所有配套工具限制）。不以虚假的能力位绕过 Crew 的原生准入逻辑。
2. Crew 0.10 insider.5 的 Windows/Electron 创建队友、任务分派、私有记忆、面板和后台连续运行，尚无实机证据；安装器需通过源码形状检查。
3. 真实长任务的跨进程 exactly-once、远程副作用回滚与 Cron 全面原生对等不在本轮承诺范围。
4. 本轮不改变 Crew 队友列表、数据库、已有队友记录，不创建 BYO 的第二套队友系统。

## 参考：上游 Crew 官方契约

- `v0.10.0-insider.5/src/kiro_crew/members.py`：`select_provider_backend()` 及 `member_dispatch_session_server`/`member_panel_session_server`。
- `v0.10.0-insider.5/src/kiro_crew/agent_sdk/backends.py`：`ACP_BACKENDS_MEMBER_CAPABILITIES` 与 `ACP_BACKENDS_MEMBER_DISPATCH` 分离。

正式实机验收矩阵：默认 KAS 队友不应被 BYO 截获；显式 BYO 队友可聊、可见经本会话挂载的控制工具、权限撤销有效；重启/恢复后身份和工具不越权；禁用的工具不得因 `tools=[]` 例外重新出现。
