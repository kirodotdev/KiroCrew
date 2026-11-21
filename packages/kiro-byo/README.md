# Kiro BYO

**当前维护版本：0.4.0**
**Current release: 0.4.0**
第三方、非官方的 KiroCrew provider-neutral ACP Harness，可连接自定义模型提供商，负责模型调用、Agent 工具循环、会话持久化、MCP 桥接与运行诊断。KiroCrew 仍负责会话编排、Crewmate 治理、权限界面、Side Chat 和宿主生命周期。

> **重要兼容性提示**：本版本只以 KiroCrew **v0.12.0 RC 与正式版及以上**为只读源码/界面契约预检候选。通过版本门槛不代表安装成功；只有本地接口检查通过才允许修改 Crew 文件。**Windows/macOS/Linux 真实桌面端到端集成尚未认证**。不要关闭安全预检或绕过失败。KiroCrew 0.11.x 及更早版本不在支持范围内；RC 版本仍需通过完整源码与 Dashboard 契约检查。

本仓库**只维护当前版本**。旧发布说明、阶段报告、过期教程和重复审计不放在工作树中；需要追溯时使用 Git 历史。说明更新时只修改本 README，不新增根目录 Markdown 报告。

### 0.3.5：修复 Crew Dashboard 推理强度接口的命名空间错误

Crew 0.12 的 `dashboard/chat_api/slot_effort.py` 实际处理函数会被 `chat_handlers.py` 的 facade composition 重新绑定；原 BYO 仅在 owner 模块级导入 `ACP_BACKEND_KIRO_BYO` 并定义私有辅助函数，调用时可能抛出 `NameError`，致使 `selection-capabilities` HTTP 500，用户界面反复报告推理强度无法验证。现改为在两个实际处理器的**函数局部**导入身份与 BYO-owned helpers，辅助函数自身也在局部导入后端身份。

安装器的写入前和提交后导入闭包检查现覆盖 facade 重绑定边界，拒绝缺少局部绑定的安装结果。已有 0.3.4 宿主文件允许通过严格 AST 对照升级，仅新增六处 BYO-owned 函数局部导入，不改动 Crew 自有业务逻辑。真实 0.12.0 源码生成的函数经模拟 facade 重绑定执行验证，固定 DeepSeek 模型返回 `low/high/max`。**仍未在用户 Windows 桌面端确认弹窗修复；官方原版 Crew 仍不具备无补丁第三方注册能力，Worker/MCP 未完成权限认证。**

### 0.3.4：Crew Gateway 启动导入闭包修复

修复 0.3.3 集成后导致 Crew 启动失败的 `ACP_BACKEND_KIRO_BYO` 导入来源不一致：`providers/mirrors/registry.py` 与 `config/loader_builders.py` 的 BYO 身份现在统一由 `agent_sdk.backends` 提供，不再依赖 R73 已退役的 `acp_backends.py` BYO 转发。已有 0.3.3 补丁的升级只允许迁移经过 AST 验证的旧身份导入；不会借此改写 Crew 其他逻辑。

安装器新增**写入前与提交后的 BYO 跨模块导入闭包检查**，并含真实 Python `ImportError` 复现测试，阻止相同类别的损坏源码进入 Crew。此更新仍属于现有受控宿主补丁方案，**不是零补丁原生 Harness 注册或 Worker/MCP 权限认证**。

### R74：退役宿主权限门展示补丁（20 项活动源码改写）

本候选不再改写 `src/kiro_crew/agent_sdk/tool_gate.py`：Crew 0.12.0 的原始实现已会对未知、未认证路由返回 `INDETERMINATE`，且 `is_enforced("kiro-byo")` 仍为 `False`。安装前新增**只读、拒绝未知变更**的 AST 安全契约检查，确认原版 Crew 的回退判断及已认证路由的静态边界；检查不符就拒绝安装，而不是伪造已认证的权限状态。

活动宿主源码改写从 **21 降为 20 项**，另保留 1 个新建 Harness 文件。R73→R74 的隔离安装、重复安装、卸载与故障拒绝路径保留旧安装回执、已安装快照和备份哈希核验。以正式版 Crew 0.12.0 源码和单独的 Dashboard 归档组合验证，20/20 源码变换通过；实际发行 ZIP 的升级往返与原始文件恢复也通过。新增 `scripts/verify_crew12_tool_gate_runtime.py` 对比真实 Crew Python 模块在集成前后的后端注册与权限状态：**原版仍拒绝 BYO，集成版虽可列出 BYO，但宿主工具权限仍未被认证为强制执行**。

**根本问题仍未解决**：Crew 尚未提供经过验证的第三方 Harness 开放身份注册、启动及工具权限强制执行接口。R74 不是无补丁原生接入，也没有完成真实桌面 Gateway、Worker/MCP、Cron/TaskRunner 端到端权限验收；不能在重要工作环境中当作安全认证版本使用。具体运行日志与风险说明见独立 R74 交付报告。

### R73：减少第二项宿主改写，并明确权限验证边界

在 R72 基础上，不再改写 Crew 的 `src/kiro_crew/acp_backends.py` 转发模块。BYO 在受控宿主适配层直接从 `agent_sdk.backends` 引入自己所属的后端 ID；活动 Crew 源文件改写从 **22 降为 21**（另新建一个 BYO Harness）。针对 R72 已安装的旧源码，只有旧快照、安装回执和原始备份能证明归属时，才能把该转发模块恢复为 Crew 原版；`acp/types.py` 与 `dashboard/handlers/agents.py` 的旧 BYO 导入采用完整 AST 对照升级，不允许以未知来源的更改冒充 BYO 补丁。

BYO ACP 工具权限请求不再接受未声明的旧式 `allow` 选择或畸形授权响应。宿主权限路由诊断现在保守返回 **`INDETERMINATE`**，因为 Crew 的 `tool_gate.is_enforced("kiro-byo")` 仍为 **`False`**。这意味着**即使补丁版 Crew 可以注册、选择并启动 BYO，也不等于 Worker/MCP 工具权限已获宿主强制执行验证**。原版 Crew 对未知 BYO Harness 的 `register_selectable_backend` 依旧拒绝，**无补丁的第三方原生注册仍未实现**。

基于用户提供的 0.12.0 正式版源码和 Dashboard ZIP，静态及隔离安装契约 **21/21 通过**。完整测试集 **1,806 通过、2 跳过**；实际 R72→R73 离线升级、重复安装与卸载恢复了 **23 个原始文件哈希**（包括 Dashboard 入口），并清理 BYO 自建 Harness。新增 `scripts/probe_crew12_runtime_registration.py` 与 `scripts/verify_r72_to_r73_upgrade.py` 可复查上述结论。**真实 Windows/macOS/Linux Crew Gateway、Worker/MCP 权限、Cron/TaskRunner 端到端测试仍未通过认证，不建议部署到重要工作环境。**

### Crew 0.12.0 正式版源码审计（R71）

已用用户提供的 `KiroCrew-0.12.0-BYO-source.zip` 进行 **23/23 源文件语义变换检查**，并修正 `api_models` 迁入 `models_route` 后旧模型发现补丁误拒绝的问题。单独新增 `scripts/verify_crew12_source_subset.py`，可在缺少编译 UI 的源码子集上执行**只读**验证，不赋予安装许可。**第三方原生 Harness API 仍缺失、23 个 Crew 文件改写依赖仍在、桌面端 E2E 未通过认证。** 原源码 ZIP 缺少 `static/dist/index.html`，安装器的完整 Dashboard 契约预检仍会拒绝。此前详细审计作为独立 R71 交付报告保存，不混入当前工作树。

### R72：缩减一项 Crew 宿主改写（源码候选）

已将 BYO 的冷会话模式同步调用改为直接访问 `dashboard.chat_api.slot_effort`，不再通过 `dashboard/chat_handlers.py` 的 BYO 特殊转发。活动宿主源码变换清单由 **23 项减少为 22 项**；新增旧 R71 集成的恢复计划：必须确认旧安装记录、已安装快照及 Crew 原始备份后，才能在升级事务中恢复这个已退役的改写文件。确认失败时拒绝覆盖，升级后卸载清单不再包含该文件。新增测试验证干净安装、恢复计划及各类拒绝情形。

**安全尚未验收**：现有 BYO `ACP_PERMISSION_GATE` 路由在 Crew 0.12.0 中仍是 BYO 注入的声明，而不是宿主可核验的权限门强制执行机制。R72 不宣称通过真实 Worker 权限/E2E，也不能将 22 个补丁视为 Crew 官方支持的第三方注册入口。**单独的源码子集不包含前端资源，仍会被严格预检拒绝**；见下节关于补充 Dashboard ZIP 的隔离验证。

### Crew 0.12.0 Dashboard 补包离线验证（R72 补充）

针对用户另行提供的 `KiroCrew-0.12.0-Dashboard.zip`，将 Windows 风格 `dist\...` 条目安全映射到**临时隔离目录**的 `src/kiro_crew/static/dist/...`，与 `KiroCrew-0.12.0-BYO-source.zip` 的真实源码组合后，严格 `_adapter_matches` **22/22 通过**。在这个临时宿主目录中实际执行 `install_from_app`、重复安装和 `uninstall_from_app`，均成功；22 个原始 Crew Python 文件及 Dashboard `index.html` 共 23 个原始文件在卸载后恢复了各自哈希，BYO 新建的 Harness 文件也被移除。ACP 子进程、Tiga/Dyna 和 Crew Work Ledger 源码验收通过。

新增 `scripts/verify_crew12_dashboard_archives.py`，可重复执行上述组合归档验证：

```bash
python scripts/verify_crew12_dashboard_archives.py --crew-source-zip KiroCrew-0.12.0-BYO-source.zip --dashboard-zip KiroCrew-0.12.0-Dashboard.zip --output combined-dashboard-audit.json
```

该脚本**不会写入用户安装的 Crew**，也**不会把独立归档重新打包成声称来自官方的发行物**。检测到 Dashboard `index.html` 引用 `/logo.png`，但补包中缺少该资源，报告为静态资源警告。两个 ZIP 是否确实来自同一官方构建尚无独立证据；真实 Windows Crew Gateway、桌面 UI、Worker/MCP deny/allow 仍未验证，**不应将离线安装与 ACP 测试当作生产兼容认证**。

### 原生 Harness 接入检查（R67）

**请勿将 ACP 握手成功等同于原生接入成功。** BYO 的 Settings → KiroCrew 现在分别显示宿主源码契约与原生 Harness 注册证据。若提示“仅核心已知后端”，表示 Crew 的内部注册函数仍受后端身份与工具权限路由限制，并不代表外部 App 已获得注册许可。**这项检查是只读诊断，不会修改 Crew，也不能替代真实的 Mate/Cron/TaskRunner 验收。**

开发者可在源码包中用 `python scripts/inspect_crew_host.py --crew-archive /path/to/KiroCrew-source.zip` 检查上游源码压缩包，或用 `--crew-package /path/to/kiro_crew` 检查安装目录。工具不会导入宿主 Python，ZIP 的关键源码成员只在内存中分析；输出附带档案 SHA256，但**不证明 GitHub tag 的来源真实性**。原有更严格的 `scripts/verify_crew12_integration.py` 同时报告原生注册证据和源码补丁预检，未知接口仍拒绝安装。


## 1. 快速安装与使用

1. 备份 KiroCrew 数据和 BYO 配置，结束正在运行的 Agent 任务。
2. 在 [GitHub Releases](https://github.com/ZXCharlotte486/kiro-byo/releases) 取得与 `VERSION` 相同版本的 `Kiro_BYO_<版本>-release.zip`。解压得到包含 `app.json` 的 **Kiro BYO** 文件夹；不要与旧目录混合覆盖。
3. 在 KiroCrew 中按 App 安装/更新该目录，打开 **Kiro BYO → Settings（设置）→ KiroCrew → Install/Update integration**。
4. 查看源码和界面契约预检结果。若预检失败，请停止、保留备份，**不要强行修改 KiroCrew 文件**。通过后完成集成，再彻底退出并重启 KiroCrew。
5. 打开 **Kiro BYO → Providers**，添加预设或自定义端点、API Key，保存并发现可用模型。模式可选 Tiga，其 Type 包括 Sky、Multi、Power、Glitter（默认 Multi）。
6. 在 KiroCrew **Settings → Agent Harness → Kiro BYO** 中切换后端，然后**新建会话**；既有会话继续归属其原始后端。

密钥由系统凭据存储管理（Windows Credential Manager / macOS Keychain / Linux Secret Service）；不应把真实密钥提交到 `config/`。使用源码仓库时，复制并编辑 `config/*.example.json` 对应的配置，真实配置不进入 Git。

### 自定义连接：中转站与本地模型

在 **Providers → 自定义 / 中转 / 本地** 添加连接。不新增独立的「本地模型」Provider 分类；它与各类兼容中转站共用现有协议引擎、模型卡、Tiga/Dyna 路由与模型级 Thinking 设置。API Key 可选（但私有局域网或公网服务可能要求认证），不能因为连接在本地就默认有工具调用或可切换深度思考能力。

常见的 OpenAI-compatible 基础地址示例：

- Ollama：`http://127.0.0.1:11434/v1`
- LM Studio：`http://127.0.0.1:1234/v1`
- vLLM：`http://127.0.0.1:8000/v1`
- llama.cpp：按所启动服务的监听地址配置，例如 `http://127.0.0.1:9931/v1`
- 中转站：使用服务商明确提供的 OpenAI-compatible Base URL。即使模型 ID 与某家官方模型相同，也不自动继承官方能力档案或原生协议。

先启动本地服务，再填写 API URL 并测试连接；若服务器没有兼容的 `/models`，可展开「高级设置」手动填模型 ID。`127.0.0.1` 是 **BYO 后端进程所在机器/容器** 的回环地址，远程机器或容器部署需要使用实际可达地址。不要把没有鉴权的本地推理服务直接暴露到公网；本地/中转请求会按用户配置传输模型输入内容。

### 安装时的安全边界

- BYO 负责执行 Provider 请求、ACP 工具调用、MCP 中转和运行记录；Crew 负责用户权限与信任策略。
- `allowed_roots`、`read_only`、`web_enabled` 和超时/资源上限属于执行限制，不能替代 Crew 的授权界面。Shell 在操作系统用户权限下执行，**不是 OS 级沙箱**。
- BYO 对兼容性未知的 Crew 源码采取拒绝修改策略。安装/卸载事务只回滚**本次尝试修改**的文件；预检发现其他程序已修改的文件时，不得用旧快照覆盖其内容。运行包、启动引导和集成记录也必须在失败后恢复到事务前状态。安装失败不要删除 Crew 模块，请使用 BYO 的集成/回滚界面恢复。
- 卸载同样采取拒绝优先策略：记录的 Crew 文件路径必须严格位于包内，不允许路径穿越、符号链接或相互冲突的动作；在开始写入前重新核对文件身份，发现其他扩展/进程修改时拒绝卸载并保留现场。该校验不是跨进程操作系统级文件锁，真实宿主环境中仍应避免并发安装与更新。
- `KIRO_BYO_RUNTIME_TRACE=metadata`（默认）仅保留元数据；`full` 可能记录工作区敏感内容，仅用于受控本地调试。

## 2. 当前功能

- **Agent Harness**：基于 ACP 的会话、流式回答、工具调用、取消/转向及恢复。
- **Providers/Models**：统一适配不同提供商 API，模型能力以实际发现和明确配置为依据，不对未知能力作保证。
- **Modes**：Policy Pack 定义 Tiga 的 Sky / Multi / Power / Glitter 路由和受控执行策略；用户所选 Type 与显式模型固定选择有不同的作用域。
- **未来形态的扩展契约**：内置 Mode 命名空间统一受注册表保护；`gaia`、`cosmos` 已预留，但尚未提供形态策略、不会出现在 UI 或参与模型路由。只有完整内置声明与策略包匹配后才能启用。历史 `tiga.json` 手动覆盖保持兼容，其余内置/预留 ID 不允许自定义 JSON 冒名替换。
- **Native tools + MCP**：工具仅在当前会话授权、实际加载与验证后可用；刷新失败必须撤销过时授权。
- **Records/Overview/Accounts/Settings**：查看模型使用、执行状态、请求诊断和集成情况，不把本地审计当作桌面端到端认证。
- **持久会话**：以 session events 为规范持久化事实来源，消息、选模等运行态由事件投影恢复；历史 schema 数据仅作为迁移输入，不作为并行的持久事实。
- **Work Ledger 关联**：包含 BYO 侧关联接口和诊断映射，**官方 Crew Host 自动任务 ID 注入尚未接线**，不声称已实现正式 Crew Work Ledger 端到端联动。
- **Crew 注意力诊断（R27）**：Records → Crew 工作将「工具副作用状态不确定、需要核实」和「普通执行异常」分开统计；后者不自动变成催促用户的待办。500 条扫描窗口饱和时不会宣称全局无异常。所有数据均为 BYO 的只读执行证据，Crew 的真实任务状态、人工问答和审核仍由 Crew 掌管。
- **R68 模式/形态守护**：新增 `scripts/acp_mode_type_smoke.py`（独立 ACP 进程、Tiga/Dyna 双模式、虚拟 Type 与固定模型切换及持久化、不合规跨模式 ID 拒绝、Crew 轻量后台会话不得选择虚拟 Type）。`scripts/crew_byo_acceptance.py --smoke` 同时报告这一组验证，但这既不是模型供应商在线验证，也不是 Crew 桌面宿主端到端认证。
- **R28～R32 连续开发**：新增 `scripts/crew_byo_acceptance.py`（隔离运行的真实 BYO ACP 子进程检查 + 可选本地 Crew 源码前置检查），以及 `scripts/crew_work_link_smoke.py`（真实 ACP 工作关联绑定/解绑/恢复流程检查）。这些工具明确区分 BYO 子进程可用、Crew 源码契约和真实 Gateway/Mate/Cron/TaskRunner 端到端认证，不会把前两者当作第三者。
- **核心控制器拆分（R29、R31）**：Durable Run 生命周期迁入 `durable_run_lifecycle.py`；ACP 思考选项/推理档位与模型能力归一化迁入 `reasoning_options.py`。保留原 `TurnRunnerMixin`、`AcpServer` 的方法调用接口和锁/租约保护。
- **工作注意力分类（R30）**：在原 `attention.items` 保持向后兼容的同时，新增有界的 `safetyVerificationItems`、`executionDiagnosticItems`，Records 界面分别显示副作用核实与普通故障；这些都只是执行证据，并不代表 Crew 已生成需要人处理的任务。

- **R33～R36 连续开发**：修复进程历史清理中向仍有读取线程的管道执行跨线程关闭、可能导致 ACP 循环卡住的问题，并加入继承输出管道的后代进程回归；将会话压缩和 Agent 计划生命周期分别迁至 `session_compaction.py`、`agent_plan_lifecycle.py`，不更改宿主任务权限；加强 Crew Work Link 能力协商，要求匹配实际绑定方法名，非空关联必须有任务 ID。真实 Crew `rc.6` Gateway、Mate、Cron、TaskRunner 联调仍未完成。


- **R41～R46 兼容修复**：加强 KiroCrew 0.12.0-rc.6 Worker 策略的 KAS 比较符静态检查；对 Mistral、Cohere、Gemini Interactions 与 Responses 的旧 `extra_body` 思考参数执行与明确 Off 设置一致的清理，防止历史缓存覆盖用户偏好。Mistral 复用共享 Chat 请求封装；新增请求构建/配置不可变性测试，并改善进程回归用例的孤儿子进程测试清理。以上检查不是官方 API 在线测试，也不是 Crew 宿主 E2E 认证。

### 模型形态的实际选模规则

- 选中 **Type（Tiga/Dyna 形态）** 时，BYO 会按任务信号、硬能力门槛、形态档位、模型路由元数据及可归因运行记录逐回合评分；选中**具体模型**时则按用户选择直接调用，形态路由不接管。Glitter 的会话粘性属于刻意设计。
- Tiga 的 Sky 优先 `fast`，Power/Glitter 严格限制 `strong`；Dyna 的 Miracle/Flash 在合格模型中按各自权重比较，Strong 严格限制 `strong`。**没有经过证明的中转站新模型默认 `balanced`**，名称中出现 pro/flash 不能作为可靠档位事实。
- 对低风险、低写入强度且评分非常接近的候选，Sky/Multi/Miracle/Flash 可执行**有界冷启动采样**（策略包配置 `cold_start_*`），优先补充缺少运行数据的模型；高风险任务、能力不足或差距显著的候选不会被强行轮换；只有形态本身允许 `capability` 降级、且无本档合格模型时，才可在备用档低风险探索，并明确标注其档位尚未验证。对同一会话和相同请求，结果可重复。该机制只产生更多*可归因的速度和可靠性证据*，不声称能自动评估答案质量。
- Dyna 的运行证据可以将无权威档位的模型归入 `fast`，或在速度/可靠性退化后退回 `balanced`；**只凭速度和成功率不能将模型升级成 `strong`**。旧版学习记录中以速度误判的 `strong` 会按 `balanced` 看待，手动档位/可信质量元数据不受影响。
- 每回合可通过运行 Trace / 路由诊断查看 `selection_source`、候选得分、原始/实际使用的 Provider 与 Model、排除原因及是否启用 `bounded_cold_start`。这些记录不保存任务正文。

### 深度思考的权限与传输边界

- 在 BYO 的**具体模型**页面配置的 `Auto / On / Off` 属于模型级设置。选择 Type 后，无论它被选作主模型、子 Agent 或复核模型，该设置都不得被形态偏好覆盖；仅 `Auto` 交给形态决策。
- 深度思考开关与推理力度是两条独立信号：执行核心只转发有效的 `thinking_mode` 和具体模型明确支持的努力档位，不能将 `enabled`、`disabled` 或 `none` 当成高低档位发送。
- 官方/中转端点必须通过其相应 Provider Adapter 将开关转换成已核实的 API 请求。对不支持切换或缺乏可信映射的中转接口，不能假装已成功切换；需要保留供应商默认行为，或拒绝无法保证的明确要求。
- 模型/Provider 能力边界优先于形态建议；变更这些规则必须运行 `test_auto_reasoning_policy.py`、`test_native_provider_transports.py` 以及架构边界检查。


### 前端与后端一致性

- 内嵌 App 的 `ui/index.mjs` 通过 `/api/apps/kiro-byo` 调用 `backend/routes.py` 注册的接口。模型卡的 `Auto / On / Off` 写入具体 Provider/Model，并在 `/status` 和 `/providers` 返回同一持久化值；模型 ID 中包含 `/` 时必须进行 URI 编码。
- 对不可切换深度思考的模型，前端禁用开关，后端仍独立拒绝非法写入。前端标记“保存成功”前必须获得后端成功响应；后端只返回脱敏后的供应商数据。无效 JSON 请求返回 400，而不是伪装成服务端内部故障。
- 本地自动验证：`tests/test_app_frontend_backend_contract.py` 为真实 aiohttp HTTP 契约测试，`test_thinking_preferences.py` 验证设置会影响形态执行；浏览器布局用例单独验证。**这些检查不能代替在 KiroCrew 桌面客户端中验证真实会话。**

## 3. 开发仓库布局

```text
README.md              当前唯一主说明
VERSION                当前版本单一权威值
app.json               KiroCrew App 元数据
BUILD_INFO.json        当前构建候选元数据（构建时刷新）
pyproject.toml          Python 项目、依赖及测试配置
src/kiro_byo/          ACP Harness、Providers、Tools、Sessions、Policy Packs
backend/               KiroCrew App 后端接口
ui/                    App 前端
assets/                 App 资源
integration/            当前 KiroCrew 0.12+ 适配器及其运行资产
config/                 非敏感配置样例
scripts/                检查、构建和集成验证脚本
tests/                  按实际行为/模块组织的回归测试
.github/workflows/ci.yml  CI 检查
```

**不要把 `dist/`、`build/`、测试缓存、真实配置或本地凭据纳入 Git。** App 的运行包和源码包由构建脚本从同一工作树生成，版本与运行资源指纹必须相同。

## 4. 开发、验证和打包

需要 Python **3.11+**。在源码根目录执行：

```bash
python -m pip install -e '.[dev]'
python scripts/project_hygiene.py
python scripts/release_check.py --metadata-only
python -m pytest -q -m 'not browser'
python scripts/release_check.py
python scripts/build_release.py
```

Windows PowerShell 中可把单引号命令参数改为 `".[dev]"`。浏览器测试还需要 Playwright/Chromium，CI 在独立 browser-ui job 运行，**不能通过跳过测试冒充通过**。CI 还检查 Python 3.11/3.12/3.13、静态检查、多个操作系统上的关键生命周期测试，以及上游源码只读契约；这些都不能仅凭本机测试宣布已通过。

构建成功后 `dist/` 包含：

- `Kiro_BYO_<版本>-release.zip`：可以交给 KiroCrew 安装的 App 本体；
- `Kiro_BYO_<版本>-source.zip`：当前工作树中可维护的源码、测试和 CI；
- `kiro_byo-<版本>-py3-none-any.whl`：供 Python 方式安装的 Wheel；
- `SHA256SUMS.txt`：校验三份产物。

**不手工修改发行包内容**。修改源码后重新执行上述构建流程，并以生成的文件为准。

## 5. 架构契约与维护原则

### 边界和会话事件

KiroCrew 是宿主会话权限、Crews、Hooks 和 UI 的权威；Kiro BYO 是 Provider transport、Agent/tool loop、native/MCP tools 和 BYO 侧事件持久化的权威。任何跨边界改动都必须有契约测试和失败回滚路径。

会话持久化通过 `src/kiro_byo/session_events.py` 的规范事件和事件投影完成。恢复时不能依赖旧的并行 `messages` / route pin 状态真相；事件投影方法应保持可重放、可测试。新建会话和恢复会话的遗留 Run 分类共用 `_reconcile_restored_durable_runs`，不维护并行的状态恢复入口。

**执行内核的当前职责边界（当前版本）：**

- `acp_server.py` 协调 ACP 请求/输出；`acp_tool_bridge.py` 专管工具事件、MCP 身份展示和经宿主批准的权限请求（展示元数据不构成授权）；`session_lifecycle.py` 承担会话登记、取消和等待退役（关闭资源前必须取得 Turn 独占锁）；`session_store.py` 负责带修订号栅栏的会话快照读写及旧格式迁移，磁盘写入失败不得覆盖新版本记录。
- `run_recovery.py` 专管 Durable Run 诊断和安全重试/恢复；Work Trace 仅提供显式任务 ID 的执行证据，**BYO Run 完成不代表 Crew Ticket 完成**，关联 ID 绝不提供权限；`turn_runner.py` 协调单次 Turn、模型/工具循环和 Durable Run；`tool_batch_execution.py` 在已有授权与 Durable Run 准入之后执行工具批次，维护取消/Steer、重复调用 ID 和执行保护语义；`provider_loop_terminal.py` 统一记录终结状态与遥测；`subagent_execution.py` 专管子 Agent 的隔离执行、并发及固定模型继续运行。子 Agent 调用仍经过与父任务一致的可注入 Provider 边界。主模型、复核和子 Agent 的 Token / 缓存读写 / 推理用量在 `turn_loop_types.py` 统一累积；未上报的指标不得伪装为供应商报告了零值。
- `reasoning_control.py` 决定模型级 Thinking On/Off/Auto 与力度的语义，Provider 适配器才负责生成最终请求字段。以上执行子模块禁止反向导入 `acp_server` / `turn_runner`。
- **模型元数据只维护一套字段映射**：`provider_catalog.py` 统一序列化已发现模型、思考能力及路由证据；`provider_admin.py`、`metadata_refresh.py`、`config.py`、`discovery.py` 调用共享映射。后台刷新保留模型级 Thinking 偏好，不把路由推测当成已验证能力。官方模型档位覆盖只通过 `discovery.profile_official_discovered_models()` 处理一次，前台管理与后台刷新共用同一实现；不得分别复制能力映射。
- **共享请求与权限实现**：`providers/http_sse.py` 提供纯传输层的 JSON/SSE 默认头，官方 Responses、Gemini、Cohere 和 Chat 适配器依各自认证方式扩展；`mcp_client.py` 的已验证 Crew MCP 工具筛选共用一条按会话认证和 Agent 权限过滤的路径，任何缓存元数据不得形成授权。`scripts/duplication_audit.py` 可重复输出核心源码规模与精确重复函数清单，但不能将形似代码自动判定为可删。
- **Provider 失败与取消控制**：用户 Cancel / Steer 优先于网络错误分类和自动重试；即使取消关闭连接造成普通传输异常，也必须按控制请求收尾，不得切换备用模型或重新发起请求。Provider 的普通不可用错误在无已执行副作用时仍可由形态选择备用模型；已明确归类为凭证、请求或模型级推理开关映射错误的拒绝不允许静默 failover。状态上下文过期的 Responses / Gemini 仅在没有取消时尝试一次无状态重发；两者现在共用 `providers/http_sse.py` 的单次重试与错误归一化，HTTP 鉴权拒绝不得触发重发。五种原生协议的续接数据必须同时匹配原始 `provider_id` 和 `transport`（禁止将其他类型强制转换成字符串后误认）。外部工具的 exactly-once 不由此保证。
- `config/architecture_budget.json` 是必须随源码提交的 CI 预算文件；拆分完成后收紧巨型模块的预算，不允许通过新建“v2”控制器恢复重复实现。


### 持久化事务与后端一致性

Durable Run 由 BYO 的 `durable_runs.py` 持有状态真相。一次 Turn 的 Attempt、Step 和 Run 终结现在在**同一份磁盘记录、同一个互斥事务与租约核验**下原子提交；故障不会留下仅有部分层级被宣告完成的记录。存储层拒绝尚有运行中 Attempt 的 Step 结束、运行中 Step 的 Run 结束，且成功提交前必须具备对应工具批次回执。若最终落盘失败，运行状态保留为未终结以供审查，**不会提前结束 Agent Plan、写入 `turn/end` 或发布 `turn.completed`**；用户将收到明确提示。**这并不保证外部工具执行 exactly-once**：真正发生过的写入仍需通过回执和人工核对判定。 进一步禁止已失败的 Run 接受迟到执行写入：Checkpoint、Attempt、Step、Run 的正常状态变更仅允许在 `running` 状态中进行；失败后的恢复分类仍可单独标记。带 Step/Attempt 身份的 Checkpoint 必须匹配当前运行中的执行；未收到 `tool_batch_completed` 回执时，不能提前把所属 Attempt 或 Step 宣告成功。异常或不完整回执在提交成功前按不安全处理。

KiroCrew 对任务与权限负责，BYO 只对自己的执行状态负责；0.12+ 适配器只复用经 AST 语义检查的当前共享变换，并不通过退役版本模块转发。旧版迁移兼容由独立的入口保留，不允许放宽源码审计。

### DSH Super-agent（2026-10-31）架构借鉴范围

参考 [DeepSeek Harness 团队的研究原型](https://github.com/deepseek-ai/dsh-superagent-prototype) 中「授权的 Ticket 与执行 Assignment 分离、执行状态不能代替任务状态、只让人关注需要处理的异常」原则。本版本仅将这些原则应用到已有 BYO Run/Work Trace 边界，不集成 DSH/Cordis，也**不实现** GitHub/飞书 Trigger、自动 Eval、跨进程常驻 Router 或自动分派 Worker。后者属于未来经过宿主授权和实测后由 KiroCrew 实现的编排功能。

`_kiro/work/list` 是只读、限量的执行证据窗口，不是任务队列；`_kiro/run/resume|retry|replan` 必须匹配当前 Session 的 Run、通过恢复策略及规范用户消息锚点校验才能重启，关联的 `taskId` 不授予重试权限。自动恢复与真实 KiroCrew Work Ledger 联动尚未上线。

KiroCrew 0.12+ 的 source-guarded 安装路径从 `crew12_patch.py` 进入 `crew_current_patch.py`，共用 `crew_slot_patch.py` 中经过验证的模型槽位、深度思考和标题变换。旧版 `crew10_patch.py` 和 `crew11_patch.py` 已移除；现有测试直接验证当前共享变换。未知上游源码仍拒绝修改。

### 更新规矩（CI 强制）

1. 根目录只允许 `README.md` 这一份 Markdown；不再建 `FIX_REPORT_vN.md`、`AUDIT_RN.md`、`RELEASE_NOTES_vN.md` 或临时总结。
2. 功能变化同步修改现有模块与对应测试；不要按版本复制一组 `test_030_*`、`test_031_*`、`test_032_*`。
3. 版本从 `VERSION` 校验并同步到 App、Package 和 ACP 协议；测试不得写死当前版本号。
4. 原生 Provider 与通用 OpenAI-compatible 共享 `providers/chat_completions_common.py` 的 HTTP/SSE 响应引擎、工具 delta 与结束标记校验；通用中转的旧 `reasoning` 字段、无原生上下文回放等差异由明确的响应 Profile 处理。各适配器只维护经验证的请求策略与异常类型。`extra_body` 的嵌套配置每次请求隔离，不得再复制聊天循环。 文本压缩用的旧 `chat_completion` 接口由 `providers/base.py` 的同一兼容工厂提供，始终动态转发到当前 `chat_completion_turn`，不得在每家 Provider 重复包装。
5. 代码删除/重构必须先保留行为回归与兼容性测试；大模块按稳定边界逐步拆分，不能为了文件数而牺牲运行正确性。
6. 每次修改在 PR 或 commit 中说明变更和验证证据，不新增永久报告。通过 Git history 查看过去，而不是把过去的每份报告留在 HEAD。

## 6. 当前已知限制与排错

| 现象 | 处理方式 |
|---|---|
| KiroCrew 源码/DOM 预检失败 | 停止安装，不绕过安全校验；核对当前 Crew 构建对应的接口。 |
| 切换后端后旧聊天没有变化 | 预期行为：新建会话生效。 |
| Provider 模型未列出或工具不可用 | 检查凭据、服务商响应及当前会话授权；不要复用过期 MCP 授权缓存。 |
| 浏览器能联网，但 BYO `web_search` 超时 | 检查 KiroCrew 进程代理环境、TUN/DNS 与路由；在受控环境可使用 `KIROAPI_WEB_PROXY` 指定本机 HTTP/SOCKS5 代理。 |
| CI 只有 Ubuntu 某些 MCP 用例失败 | 先检查测试对 `time.monotonic()` 刷新冷却的假设；`_last_host_refresh = 0` 不保证已过冷却期。 |
| 需要确认真实桌面兼容性 | 执行 KiroCrew 实机预检和端到端工具/会话测试。当前版本尚未通过正式实机认证。 |

开发问题与变更讨论请使用 [GitHub Issues](https://github.com/ZXCharlotte486/kiro-byo/issues) 和 PR。本文只描述 **当前 0.3.4 工作树**，不保留旧版本教程或旧发布宣称。

### KiroCrew v0.12.0 stable+ integration boundary (R65)

KiroCrew **v0.12.0 RC/stable or later** is eligible for strict source preflight; future v0.12.1 or v0.13.x builds are
**candidates, not automatically compatible**. The installer inspects actual
installed Crew source, checks security policy and ACP seams, verifies the
semantic transform converges, and refuses unknown changes without patching.

Mate Worker restrictions that only KAS can enforce (`hooks`, `permissions`,
`toolsSettings`) remain enforced at the Crew boundary. BYO requires the host's
`worker_session_spec(..., refuse_spec_policy=True)` contract and **never falls
back** to the earlier projection when that contract is missing.

The model-discovery transform no longer requires the old literal
`resolves_model_from_advertised_list` branch. It accepts only a structurally
recognizable `api_models` handler with unique backend/config bindings and a
convergent, source-guarded transformation; other shapes fail closed.

**Limit:** User-provided 0.12.0 source and the separately provided Dashboard
archive pass the offline source contract and isolated install/uninstall tests.
Their official release provenance and same-build identity are not independently
verified. Real Windows/macOS/Linux KiroCrew Gateway/Mate/Cron/TaskRunner/Work
Ledger end-to-end integration is **not certified**.


> R64 engineering update: completed process history pruning never waits for live pipe readers; routing and model capability catalogue lookup share a first-match resolver, provider instance lookups reuse one helper, and discovered-model reasoning mirrors share one mapping. The official KiroCrew host E2E and browser cases are still not certified.

### R75 experimental external Harness admission (not active)

R75 adds a **source-only** upstream-interface experiment at
`experiments/crew_external_harness_registry.py`, plus a read-only runtime
constructor probe (`scripts/verify_crew12_runtime_path.py`). Neither installs
a replacement Crew registry nor alters BYO's Tiga/Dyna/provider execution.

The reference registry refuses a third-party backend unless the trusted host
reports launch, OS/process confinement, per-tool gate enforcement and Worker
policy wiring. A successful fake-host unit test is **not** host enforcement
evidence. Crew 0.12.0 still rejects BYO without source modifications, and the
R75 install path still changes **20 Crew source files**. The source archives
used for offline validation cannot substitute for full Gateway/desktop testing.

### R76 experimental Crew external ACP control-plane bridge (upstream candidate only)

`experiments/crew_external_acp_probe.py` is a generic, stdlib-only ACP v1
transport prototype. Its **byte-identical Crew-side upstream candidate** is
`src/kiro_crew/agent_sdk/external_acp_probe.py` in the separately supplied
Crew review patch. Run `scripts/verify_crew_external_acp_bridge.py
--crew-source /path/to/KiroCrew-0.12.0` to test the **actual Crew-source module**
against isolated BYO Tiga/Dyna ACP subprocesses without provider credentials.

A host-approved descriptor carries a distinct backend ID, explicit binary/argv,
operator-approved executable hash, working directory and minimal child environment.
The transport can initialize, create/load/close/cancel a session and change an
advertised model. It supports atomic switching between separate ACP processes.
It does **not** send prompts, mount MCP, grant ACP permission requests, or allow
Worker/Cron/TaskRunner execution. No Crew production backend registry or UI is
modified by the prototype; the existing source-patch installer still edits 20
Crew source files and still refuses pre-release Crew versions. A Python interpreter
hash alone does NOT authenticate the `kiro_byo` package or its imports, and this
POC does NOT enforce OS sandboxing. Do not treat a passing probe as tool safety.

This is a tested upstream-facing transport building block, **not an installable
no-patch native integration**. Full support for all 0.12 RC/stable and future
releases remains a goal that needs a Crew-owned production extension, verified
permissions, and real Windows host tests. No model API calls are made by this probe.


### 0.3.6 RC — ACP 推理强度与响应完成栅栏

0.3.6-rc.1 将 `session/set_config_option` 和 `session/set_model` 的终止响应推迟到释放会话配置锁后，避免 Crew 在成功响应后立刻设置 DeepSeek 努力等级时偶发收到 `-32002 session has an active turn`。错误响应也使用相同完成栅栏。

可直接运行 `kiro-byo verify-acp --json`，以独立、脱敏的临时配置和两个真实 BYO ACP 子进程验证 Tiga/Dyna、固定模型切换、公布的推理强度、会话恢复和取消通知。如需指定固定 DeepSeek 模型：

```shell
kiro-byo verify-acp --json --model DeepSeek-V4.1-Flash --model DeepSeek-V4-Pro
```

此命令不会请求模型服务、启动工具或 MCP，不会修改 Crew 用户配置。验证级别仅为 BYO 真实 ACP stdio 子进程，不包含 Crew 真实 Gateway、Dashboard 的 selection-capabilities HTTP 请求，也不证明 Worker/MCP 的宿主权限强制执行。Crew 原生第三方 Harness 契约尚未获得认证；历史 20 文件补丁未移除。

### 0.3.8：Crew ACP 官方 ↔ BYO 双向切换与队友默认跟随

- 全局 `agent.acp_backend` 切为 BYO 时，未独立绑定的队友新会话有效地选择 BYO；切回官方 Kiro/KAS 时恢复 Crew 官方队友后端，不沿用旧 BYO 全局成员默认值。
- 删除 BYO 曾在 `select_provider_backend()` 之前强制早返回的分支；保留 Crew 原生会话级 override 与独立队友 pin 的优先级。
- Dashboard 模型归属检查使用与新会话相同的双向默认跟随规则，避免 UI 与路由配置打架。
- 严格支持已安装 0.3.7 的源码锚点升级，拒绝被用户修改过或不完整的旧 BYO 宿主改写，不会覆盖其他未知补丁。
- **仅影响新启动/重建的会话**：正在执行的队友会话不得自动转移到另一个 ACP 进程；切换后应安全结束旧工作并重新开启会话。
- 当前仍为 Crew 0.12 旧兼容安装链，原生无补丁接入和 Windows Gateway/Provider 端到端尚未认证。

### 0.3.7：会话级宿主工具审批与简洁升级包

- **BYO 实际运行代码升级**：在现有 Native/MCP 工具门禁中支持 ACP 会话选项 `host_tool_approval=required`；设置后预授权的 `allowedTools` 也不能跳过会话的宿主审批请求。禁止同一会话降级，恢复会话时须由宿主重新设置。
- **保留原有能力**：Tiga/Dyna、模型身份与输出模态安全检查、推理强度控制和旧版 Crew 安装/卸载链未删除。
- **尚未完成 Crew 原生准入认证**：这项 BYO 配置是供受信宿主使用的协议能力，**不能单独证明 Crew 生产工具、Worker/MCP 权限已经强制执行**。官方无补丁第三方 Harness 注册未实现；20 文件兼容层依然存在。不要忽略安装预检拒绝，Windows 桌面端仍需验收。

### 0.3.6-rc.3 / R79：身份、输出协议与思考约束修复

- **模型显示名称不作身份依据**：官方能力和路由预置只按确切模型 ID 匹配，禁止用容易伪装的 `name` 继承其他模型能力；未知部署可用明确的本地覆盖配置提供事实，身份本身仍需要服务商背书。
- **名称不再作为模型性能评分输入**：`Flash / Pro / Opus` 等文字不再暗中影响速度或质量猜测；仍保留来源清晰的路由声明、运行证据及可用能力检查。
- **ACP 文本路由拒绝非文本输出专用模型**：明确只有图片、音频或视频输出的模型不会误入 Tiga/Dyna 普通聊天链。未知输出信息仍可保守尝试，并且不等于保证成功。
- **已声明能力优先**：当存在明确支持所需图片输入/工具的模型时，未知能力候选不会靠高分挤走它；没有任何已声明支持者时保持原有尽力尝试行为。
- **思考强度 fail-closed**：未知/否定的思考支持状态，不再仅凭矛盾的 effort 列表就发布或发送强度；用户 `自动/开/关` 与厂商固定开关限制照旧。
- **主线保持未完成**：Crew 0.12.0 官方已证实未知 Harness 不能直接注册、未验证权限路由不能强行设为可选；本版保持 20 文件源兼容层和权限拒绝边界，绝不冒充无补丁原生安装。

### 0.3.6-rc.2 / R78：兼容层收敛（不等于原生无补丁）

- 删除 Crew 0.12 适配器中永远不会使用的固定源码哈希快捷路径；所有 generated seams 继续要求语义收敛并通过 `compile()`。
- 合并宿主安装事务中的单文件写入与回滚登记逻辑，写入前即记录需要回滚的文件。
- 复用 Dashboard 发现逻辑；维护独立回归确保未改变安装、升级、卸载行为。
- 核心 Python 代码 46,382 → 46,355 行；架构预算上限与 20 文件宿主改写上限自动校验。
- 原生 Crew Harness 注册、权限链以及 Windows 端到端仍未经过验证；不得宣称此候选版解决了无补丁接入。
