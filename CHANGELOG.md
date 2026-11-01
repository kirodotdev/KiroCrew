## 0.3.0 — SSSP-BYO user-supplied icon update (icon-only candidate)
- Adopt the user-provided black-and-white horizontal SSSP-BYO logo without redesign. Store its original RGBA PNG byte-for-byte in `assets/logo_bw_text.png`.
- Render the same artwork in the KiroCrew app gallery, sidebar and BYO heading. Gallery uses an unbordered white square preview; the sidebar/heading SVG embeds the exact uploaded raster and inverts it in dark mode.
- Keep the host app id, backend routes, all Mode policies, integration logic and runtime configuration unchanged.
- Status: offline asset and packaging checks only; KiroCrew desktop visual validation is still required.

## 0.3.0 V16 — 0.11.1-rc.2 strict compatibility candidate (2026-11-01)

- Recognize KiroCrew 0.11.1 prerelease versions (`-rc.N` / `rcN`) in the guarded local Source audit. The previous V15 preflight blocked them before checking source.
- All installed 23 Crew semantic patch seams, cross-file import contracts, and the model picker DOM are still validated before any Crew source mutation. Unknown 0.11.2 / 0.12 releases remain blocked.
- Source-audited baseline remains 0.11.0-rc.1; 0.11.1-rc.2 is **not** independently source-audited or desktop-E2E-verified.
- No changes to Tiga/Dyna, Provider credentials, recovery, app icon, or the existing mode-switch reload workaround.

## 0.3.0 V15 compatibility hotfix (2026-11-01)

- Source audit and dashboard preflight read the UI timing JavaScript from the **candidate BYO App bundle**, not an obsolete copied Crew runtime. This repairs false incompatibility failures when site-packages has a stale/missing resource.
- Exact KiroCrew 0.11.1 can enter source-contract preflight; it is **not** declared upstream source-audited, and installation still fails closed if source or UI contracts differ. Future 0.11.2 / 0.12 remain blocked until their own review.
- Integration settings distinguish actual installed-source semantic checks from pinned upstream audit provenance in five UI languages.
- No modifications to routing policies, ACP wire model IDs, data records, backups or removal behavior.

## 2026-10-31 — 0.3.0 V13 Crew identity + first-turn native artifacts candidate
- Supply factual KiroCrew host identity and current BYO runtime Mode/Type in the provider system context on **every turn**. Preserve Crew-authored agent persona and distinguish concrete-model bypass from active Type routing.
- Advise the native inline `<mcwidget>` workflow on visualization requests; prefer Crew's verified widget Skill when available rather than silently writing an unrelated HTML file. Never claim that an artifact was saved without host confirmation, or invent numeric data for comparisons.
- Expose a small set of verified first-party `kirocrew-core` artifact tools on explicit artifact requests. Respect saved-Agent MCP permissions, verified live brokers and existing tool approval; do not auto-preload artifact deletion or rollback.
- Add multilingual intent, actual provider-prompt, Dyna/Type, authorization, provenance and safe tool-discovery tests. No independent artifact storage, no Mode routing changes, no ACP protocol or Durable Run changes.
- Candidate only: Windows/macOS/Linux Crew Widget auto-registration, cross-session editing, versioning and live MCP permission flows still require actual host end-to-end validation.

## 2026-10-31 — 0.3.0 V11 Provider preflight and diagnostic minimization candidate
- Moved slow Provider discovery outside the cross-process `providers.json` read-modify-write lock for configure and refresh. Both commits re-read current state within the lock; a refresh rejects changed endpoint, provider family, credential reference, or credential contents instead of persisting stale results.
- Restricted public Durable Run payloads to explicit status/identity/timing fields, lease status without owner token, and the model identifier consumed by the Records UI. No raw tool receipts, step objectives, checkpoints, or arbitrary metadata leave the diagnostics projection.
- Added deterministic slow-discovery, stale-endpoint/credential, and public-data-leak regression tests. Recovery ledger storage, route weights, ACP protocol, and model IDs remain unchanged.
- This is a candidate: Windows/macOS/Linux desktop E2E, live Provider and long-run stress tests remain outstanding. See `V11_OPTIMIZATION_REPORT_2026-10-31.md`.

## 2026-10-31 — 0.3.0 V10 bounded security and state-governance candidate
- Runtime Trace now masks known secret headers, embedded URL credentials, sensitive (including encoded) query keys, and auth fragments in both metadata and full diagnostic data. Existing previously written trace files are not modified.
- Turn telemetry history clearing shares the same cross-process file transaction lock as appends and trimming.
- Provider control-plane mutations serialize their entire read-modify-write transaction across processes using a stable sidecar lock; nested migration calls reuse the outer transaction.
- Durable Run ordinary clear and history pruning use a shared protective rule: only safely completed records are eligible; running/failed/cancelled and uncertain records remain intact.
- Added isolated concurrency, secret-scrubbing, and Durable Run regression tests. No Mode/Type routing, ACP protocol, user data or model ID migrations.
- Cross-platform Crew UI E2E and live-provider endurance testing remain unverified.

## 2026-10-31 — 0.3.0 Records Center phase 1 (read-only candidate)
- Unified Records overview/timeline over existing bounded Turn, Runtime Trace and Durable Run ledgers; retained counts and safe correlation IDs.
- Five-language Records Center copy and locale-aware time/number display. No Durable Run deletion or route-policy changes.
- Added `GET /records` with bounded, metadata-only events. Original `/diagnostics` and `/runtime-trace` interfaces remain unchanged.

## 2026-10-31 — 0.3.0 V5 模型目录同步观察与安全刷新（Windows E2E 待验收）

## 0.3.0 V6 model-sync draft visibility hotfix (2026-10-31)

- Skip hidden/invisible editor nodes when checking for unsent drafts before a guarded Crew reload; keep visible drafts and running turns protected.
- Add five injected-script regression scenarios and a Windows acceptance checklist. This is a release candidate, not a verified zero-reload React cache fix.


- Crew 主界面检测到后端返回 Dyna/Tiga 新模型目录时，若全部会话确认为空闲则自动刷新。
- 当 `/api/chat/slots` 不可用、会话忙碌或存在未发送草稿时，展示同步阻塞信息而不是静默跳过；避免强制中断任务。
- 两次自动重载后仍不收敛时给出提示而非无限循环；刷新防循环标记升级为 v3。
- 只读 `/mode-sync` 诊断识别 V5 注入脚本；不更改模型路由、模型 ID 或用户数据。
- 详细验证参见 `FIX_REPORT_0.3.0_MODEL_SYNC_V5_2026-10-31.md`；**未通过 Windows 实机端到端验收**。

## 0.3.0 追加修复候选 — Crew 模型目录与当前 Mode 对齐（2026-10-31）

- 修正此前的刷新收敛误判：composer 显示 Dyna 不等于菜单已经切到 Dyna，必须验证真实模型选项。
- 模型列表探测改用真实 ARIA option 路径；打开模型菜单后立即检查，提供两次限速安全重试及跨 Mode 的防重载状态清理。
- 新增只读 `/api/apps/kiro-byo/mode-sync` 和模式页异常提示，区分 Host 配置源差异、旧集成补丁和 React 模型缓存陈旧。
- 不修改 Policy Pack、权限、具体模型 ID 和 Durable Run。Windows KiroCrew 0.11.0-rc.5 实机 E2E 仍待验证。

## 0.3.0 追加修复候选 — Dyna → Tiga 空闲 ACP 会话同步（2026-10-31）

- 修复已有聊天回复结束后 ACP Provider 仍保持 warm，导致切换 Dyna → Tiga 时 `_kiro_byo_reconcile_slot_routing` 将该会话错误视为活跃并保留 Dyna Type 的缺陷；反向 Tiga → Dyna 也使用同一安全路径。
- 不再只改变聊天右下角的显示字段。跨 Mode 的空闲 ACP 会话经 Crew 原生会话重置事务及锁/忙碌/子代理检查之后，才提交新的 `kiro-byo-type:<mode>:<type>`；后续消息按新 Mode 创建 ACP 会话。
- 保留同 Mode 手动 Type、具体模型直连、官方后端、运行中的回合、链接会话和其他 App 的会话。重置拒绝或宿主接口不可用时，保留旧选择并记录失败，避免假显示；如用户与模式页之间仍有不一致，可检查 `host_sync` 状态并重新加载会话。
- 为重复切换、运行中、子代理、重置失败、缺失宿主接口及并发手动固定模型竞争增加独立回归测试。不改变 Tiga/Dyna Policy Pack、Provider 凭据、Durable Run 或原有 Model ID。
- Linux 分组回归：1249 通过，1 跳过，58 subtest 通过；13 项 Playwright/Chromium 几何测试在当前环境中未完成（单进程运行同样于浏览器测试附近超时），不得计为通过。Windows KiroCrew 实机 UI/真实 ACP 验收仍待完成。

## 0.3.0 修复候选 — Mode 切换同步与架构门禁（2026-10-31）

- 修复冷会话从 Tiga 切换 Dyna 后仍保存旧 Type 的状态协调问题；同 Mode 手动 Type、具体模型固定选择与活跃 ACP 会话不强制迁移，官方后端与 BYO 隔离。
- BYO 模式 PUT 成功后尝试同步 KiroCrew 冷会话状态，并从 App 广播跨界面模型目录刷新信号。0.11 注入脚本触发正常模型列表读取和宿主窗口重新验证，不强制刷新整页；React Query 缓存端到端仍需 Windows 验收。
- KiroCrew v0.11.0 正式版可进入受限源码预检候选集合，所有语义源码依赖及前端 DOM 校验继续失败关闭；不宣称 Windows E2E 通过，未来 0.12 不自动允许。
- 将兼容性检查中的独立导出契约验证提取为单独函数，使超过 100 行的函数从 55 降至 54，不更改架构预算。
- 142 个测试文件分六组回归：1260 通过、1 跳过、58 subtest 通过；单进程全量运行未获取正常完成结果（环境中断），不能替代 Windows 宿主测试。
- 保留 0.3.0 的产品版本号，通过 BUILD_INFO 的新运行时内容摘要区分修复构建。

## 0.3.0 — Dyna 三形态发布候选（2026-10-31）

- 将内置 Dyna 正式注册为可执行 Mode，形态显示顺序为 Miracle → Flash → Strong，默认形态仍为 Flash；Tiga 保持系统默认 Mode，直到用户主动切换。
- 三种 Type 分别引入完整路由、推理、独立复核和有界 Worker 协作策略，共用原有 Provider、ACP、权限、Trace、Durable Run 和安全边界。
- Kiro BYO 模式页根据实际当前 Mode 动态切换卡片和只读路由统计；消除异步加载旧 Mode 统计的视觉混用。
- Crew 模型选择基于当前活动 Mode 的有序 Type 列表，保留不变的 `kiro-byo-type:<mode>:<type>` 协议 ID。具体模型直连仍然不走 Type 路由。
- 内置 Dyna 策略只能随签名/受控发布安装包更新，自定义同名策略不允许覆盖；五语言文案基于既有模型名称和类型名称显示规则。
- 仍未完成 Windows KiroCrew 0.11.0-rc.5 端到端实机验收，尤其需实测宿主模型菜单刷新和真实多 Agent 效果。

## 0.2.5 — ACP V3 能力契约与 MCP 错误信息安全收口（2026-10-31）

- 新增 `scripts/acp_capability_audit.py`，静态检查 `initialize` 公布的 ACP 扩展与实际分发器是否一致，输出公开与未公开 Crew/旧兼容方法清单；发布检查自动执行该契约审计。不会自动暴露已有私有 API，也不冒充 `session/fork` 已实现。
- 为 MCP 2026 `input_required` 增加专用异常类别 `McpInputRequired`；保持拒绝未支持的交互输入、保留远端调用可能产生未知副作用的安全状态，不伪造 MCP Elicitation 功能。
- MCP 已派发调用的异常信息不再把不可信上游原文传入模型/会话 Trace。对协议、JSON-RPC、交互需求、连接故障返回稳定、无敏感数据的错误文本。工具正常返回的业务内容及原有 `isError` 状态仍按原规则处理。
- 补充安全故障注入、ACP 未声明扩展、未知方法返回 `-32601`、错误文本敏感数据不泄露回归测试；未触碰 Tiga/Dyna Policy Pack、Crew/UI/模型来源和 Durable Run 数据格式。
- Windows KiroCrew `0.11.0-rc.5` E2E、完整 MCP Elicitation、操作系统 Shell 沙箱均未在本轮实现或宣称通过。

## 0.2.5 — Architecture V7–V10 工具批次、恢复对账与稳定性收尾（2026-10-30）

- V7：工具批次保护停止分支独立为 `_handle_tool_guard_stop`，保留原有 Durable Run 先准入/后收据顺序、Worker handback 与灾难级上限；工具批次主方法从 246 行降至 191 行。
- V8：ACP Session 恢复时的孤儿 Durable Run 对账拆为 `_reconcile_restored_durable_runs`，保留活跃租约保护、不确定副作用分类、对账竞争处理；恢复主方法从 219 行降至 182 行。
- V9：新增 AST 依赖边界检查，约束七个路由/内容/执行叶模块禁止反向导入 ACP Server / Turn Runner，并接入发布检查。
- V10：新增 8 项故障路径、架构与跨系统稳定性检查；136 个测试文件分 16 组，全量回归 1,175 通过、1 跳过、58 个子测试通过。超 200 行 Python 函数预算收紧至 9。
- 产品 0.2.5、Tiga/Dyna 策略、具体模型直连、Crew 联动、五语言、UI 和 Provider 逻辑不变；仍需 KiroCrew 0.11.0-rc.5 Windows 实机验证。

## 0.2.5 — Architecture V6 Provider 循环终止边界（2026-10-30）

- 从 `turn_runner.py` 抽出 `provider_loop_terminal.py`，统一处理取消前置检查、最终 Provider 错误和内部异常收尾；仅在原有 `RecoveryPolicy` 决策之后执行，不承担重试、回退或工具重放。
- `_run_provider_tool_loop` 从 284 行降至 200 行，保留 10 条原有 `continue`、4 次逻辑步数回退及 Durable Run 安全边界；原 Provider/内部异常处理语句通过 AST 对比保持一致。
- 新增六项故障注入/契约测试，覆盖传输重试耗尽、无副作用的模型回退、可见流中断不回退、工具副作用后的内部异常保护、调用前取消和终止处理模块职责边界。
- 架构预算的超 200 行函数上限从 12 收紧为 11；发布检查新增终止处理模块的集成及 fail-closed 约束。没有更改 Tiga/Dyna 策略、Crew 联动、Provider 模型选择与 UI。

## 0.2.5 — Architecture V5 原生工具调度边界（2026-10-30）

- 将原生工具执行从 Turn Runner 拆到 `native_tool_dispatch.py`，保留原有权限、隔离、执行、审计顺序和 `_execute_native_tool` 对外方法。
- 拆分一次性目录授权与实际调用，新增 9 项故障注入测试，覆盖审批取消/转向、执行失败撤销授权、重复调用 ID 防重入等关键安全边界。
- 超过 200 行函数数从 13 降至 12；调整发布检查为实际 Mixin 结构验证。Dyna 保持禁用，Tiga、Crew、Durable Run 及五语言不变。
- 全量代码回归为 1,161 项通过、1 项跳过、58 个子测试通过；未进行 Windows Crew 0.11.0-rc.5 实机验证。

## 0.2.5 — 形态菜单顶部定位补丁（2026-10-30）

- 当 Crew 复用先前的滚动位置、选中 Multi Type 但形态标题已被裁掉时，重新打开会话模型菜单强制从 Type 组顶部显示；狭小窗口中只做必要补偿。
- 具体模型继续定位自身（包括长列表最末行），仅对会话模型下拉框生效，不干涉手动滚动及官方 / Mate / Settings 模型选择。
- 新增旧补丁确实会失败的 Chromium 反例测试，并回归四种形态与新建/复用 Portal。Mode、Type、Provider 路由均未修改。

## 0.2.5 — Crew 0.11 会话模型菜单重新打开时定位选中项（2026-10-30）

- 修复选中列表底部的具体模型后再次打开菜单却从顶部显示的问题：优先按 composer 真实选中标签定位，严格只滚动会话模型菜单的滚动视口，不滚动页面。
- 兼容 Portal 延迟挂载、隐藏复用、鼠标及键盘打开；手动滚动后不抢走位置，选中顶部 Type 时不会误滚到下方模型。
- 缩减 BYO 分组标题多占用的行外间距，避免将菜单撑得过长；不覆盖 Crew/Radix 的弹窗定位算法，Windows RC3 实机仍需验收。
- 保留 Tiga 四形态、Multi 默认、Type wire ID、具体模型的选择回调、官方 Auto 隔离、Mate 和 Settings 语义。

## 0.2.5 — Kiro 风格的单次工具操作描述（2026-10-29）

- 原生工具 Schema 增加可选 `description`：模型可为本次具体操作提供简短说明，按 BYO 界面语言（简体、繁体、英语、日语、韩语）请求生成；旧工具调用无需提供该字段。
- ACP `tool_call.title` 优先采用经长度、控制字符、命令形态与常见凭据筛选的操作说明；缺失或不合适则回退已有五语言工具类型标题。模型描述是非可信的展示提示，绝不参与授权、执行或审计身份。
- 工具事件继续保留原始 `rawInput`、`kind`、`toolCallId`、`_meta.kiro`，不改变原始命令、文件路径与已有权限路径；MCP 工具仍保持自有标题与真实身份，不挪用其参数中的同名字段。
- 原生工具 Schema 的展示语言按会话隔离，防止中日英等并发会话互相污染；新增 5 语言、合法与恶意描述、旧调用兼容、工具执行等回归测试。
- 尚未在 Windows Crew RC3 观察其是否最终采用 `title` 或 `rawInput.description`，需要实机验收；`auto` 模式仍按系统语言回退。

## 0.2.5 — ACP 工具标题五语言本地化（2026-10-29）

- ACP `tool_call.title` 采用 BYO 的界面语言（简体中文、繁體中文、English、日本語、한국어），工具名、命令参数、权限判定和 `_meta.kiro` 身份保持不变。
- Shell 默认显示中立的“执行终端命令”一类标题，不把不可信的原始命令猜成安全操作；未知工具保留原名，避免误导。
- BYO 界面语言选“自动”时，ACP 子进程只能按系统语言回退；尚无官方 Crew 实时前端语言字段可供获取，不能保证跟随 Crew 单独修改的语言设置。
- 补充五语言、MCP 身份、失败回退、Mode/Type 兼容测试；Crew 活动面板若选择呈现 rawInput 而非 title，仍需实际前端结构确认。

## 0.2.5 — Crew 0.11 模型菜单分组标题恢复（2026-10-29）

- 当新版 Crew 在挂载时已显示 `Sky Type` / `Multi Type` 等名称时，通过 `data-model-id` 识别 Type 行，恢复「形态 · 默认复合型，可手动切换」分组标题。
- 补充延迟挂载 DOM 回归测试，保持默认 Multi、原生 Settings 作用域及模型 wire ID 不变；无 Type / Model 语义改动。

## 0.2.5 — 第六阶段：MCP 调用状态与异常闭环（2026-10-29）

- MCP Manager 新增 `call_with_status()` 结构化响应，按协议 `isError` 而非 `Error:` 文本判断失败；旧版 `call() -> str` 保持可用。正常的 `Error:` 文本不会被误标为执行失败。
- 工具派发阶段统一捕获非致命异常（包括非 `OSError`），不暴露异常细节，写入 `failed` 终态与副作用不确定事件，禁止自动重试；`KeyboardInterrupt` / `SystemExit` 不被吞掉。
- 调用前不可执行的 MCP 状态与已发起远端调用但失败的状态分离：前者不虚报远端副作用不确定，后者保守提示核查外部状态。
- 尊重已有会话级 `call` 覆盖/测试桩；兼容传统只返回字符串的客户端，但不能从字符串获得可靠的协议错误标志。
- 新增结构化结果/异常回归测试；未修改 Tiga Mode / Type / 模型池、Crew 21 个源码适配点、Cron 原生读写与 ACP 主路由。
- 仍需 RC3 Windows 实机 Mate/Worker 验收；Crew 原生 Cron 与 BYO 之间的原子 CAS 仍未解决。

## 0.2.5 — Crew 0.11 RC2+ 受限候选预检（2026-10-29）

- 修复 `0.11.0-rc.3` 在源码预检前就被 `resolve_target()` 与适配器元数据的 RC1/RC2 白名单拦截的问题。RC2 及之后同一 0.11.0 系列的 RC 都可进入只读预检，稳定版及其他系列仍拒绝。
- 对所有新 RC **继续强制**验证 21 个语义注入点、overlay 未修改依赖的符号/导出/标记，以及实际安装的前端模型菜单 DOM 契约；任意不匹配均不得写入 Crew。
- 更新只读预检脚本、发布检查、RC 候选回归测试和构建元数据；RC1 仍是唯一源码审查基线，RC3 未获 Windows E2E 验证。
- `mode_router.py`、`protocol.py`、Tiga 策略、ACP 引擎、Mate/Worker 路由守卫、原生任务同步与前端显示代码均未改动。

## 0.2.5 — Crew 0.11.0-rc.2 预检式兼容支持（2026-10-29）

- RC2 不再因为 `resolve_target()` 的 RC1 版本硬编码直接失败；`0.11.0-rc.2` / `0.11.0rc2` 被列为**候选**（非已验收版本），RC1 仍为已检查的基线。
- 接入仍需全部 21 个 Crew Python 语义适配点、未修改模块的依赖契约及已安装 0.11 前端的模型菜单 DOM 契约均通过。任何一处缺失、变化或不收敛均拒绝安装，不回退到官方后端。
- RC2 继续使用 Crew 0.11 专属形态标签显示桥接，避免回退旧版前端补丁。新增只读预检脚本 RC2 支持与专门测试。
- 不修改 Tiga 四形态、Mode/Type 命名空间、Provider 路由、ACP 执行器及 Crew 原生设置双向同步行为。
- **限制：尚无 RC2 真实上游源码及 Windows E2E 验收；此构建并不保证当前用户的 RC2 源码一定通过契约检查。** 若检查失败须依据报告修复对应源码接口，不允许绕过安全检查。

## 0.2.5 — 第四阶段：Crew 0.11 RC1 成员后端补丁完整性防护（2026-10-29，构建更新）

- Stage 5: isolate Crew-governed MCP tool dispatch into a dedicated mixin without changing the TurnRunner entrypoint, Mode/Type policies or existing approval order; treat post-dispatch MCP transport exceptions as uncertain effects without auto-retry. See `CODE_REVIEW_FIX_STAGE5_2026-10-29.md`.

- 已安装的 BYO 成员会话路由守卫不能只凭注释 marker 视为有效：现在逐行核对完整的 fail-closed 守卫块，缺失/篡改立即抛出 PatchError，阻止静默回退官方后端。
- Crew 0.11 RC1 的 loader 成员默认后端守卫也不再只凭 marker 判断；必须保持完整 BYO 主后端接管逻辑，否则安装事务拒绝继续。
- 新增 `tests/test_crew11_guard_integrity_stage4.py` 共 8 项回归测试，覆盖已安装幂等、损坏/重复 marker、失效后端判定；验证 legacy 成员升级路径不受影响。
- 未修改 Mode/Type、四种 Tiga 形态、Provider 模型路由、ACP 执行器及 RC1 harness overlay。无 Windows E2E 验收，不将此项等同 Mate/Worker 真实执行链验证。
- 保留已知限制：Crew 原生 Cron 与 BYO 编辑器尚无跨界面原子 CAS。

## 0.2.5 — 第三阶段：Crew 原生 Cron 写入后校验（2026-10-29，构建更新）

- BYO 日程模型和 AI 执行上下文提交后，重新从 Crew 原生 Cron 服务读取任务并核对实际存储值，不再将 `update_job_async` 返回对象直接当作持久化成功证据；任务消失、存储值不一致时明确报错。
- 如果异步日程写入期间 Crew 主后端从 BYO 切走，拒绝将该请求报告为成功，并明确告知该写入可能已发生，应检查 Crew 原生任务；**不做危险的自动回滚**。
- 服务端拒绝对 Script/Command 日程修改 AI 会话上下文，与 BYO 任务 UI 的隐藏逻辑一致。
- 新增 6 项异常路径回归测试，继续保护原有的 `expected_model`、执行上下文过期检查和同 Gateway 串行提交。
- 完全不改动 Mode/Type、Tiga 四形态、具体模型直连、ACP 执行器、Crew 0.11 RC1 适配器、Mate/Worker 投影和 UI 文件。
- **仍未实现 Crew 原生编辑器与 BYO 之间的原子 Cron CAS**；读回验证是后置完整性检查，不是防止跨界面并发覆盖的替代品。Windows E2E / 真实 Provider 调用仍未验收。

## 0.2.5 — 第一阶段代码审查修复（2026-10-28，构建更新）

- 在 Crew 原生配置锁内复核主代理后端：聊天、Crew 队友 / Worker 和后台模型写入在后端已切回官方时拒绝提交；`config.local.json` 对主后端的覆盖会使 BYO 写入失败关闭，防止配置检查与实际写入之间的竞态。
- 在没有 BYO 可执行具体模型时，不再将启用 Mode 的 Type 列入任务页模型选项或写入白名单；与 Crew 主模型目录的可用性条件保持一致。
- 同一个 Gateway 事件循环内对 BYO 的 Cron 模型写入和执行上下文更新串行处理，旧页面使用 `expected_model` 的冲突检查不会被另一个 BYO 任务请求绕过。**仍未实现与 Crew 原生独立 Cron 写入者之间的原子 CAS**；需要官方 Cron 接口或同一存储事务支持，不能宣称该项完全解决。
- 修正无法读取 `VERSION` 时回退到过期版本号的问题；修正 RC1 Worker Kit 适配文档的过期描述。
- 新增针对并发后端切换、`config.local.json`、无 Provider 以及两个 BYO Cron 并发编辑的回归用例。全项目 pytest：1006 passed, 1 skipped, 52 subtests passed（本地隔离执行；Windows KiroCrew 真实运行仍未验收）。

## 0.2.5 — BYO 模型目录隔离修复（2026-10-28，构建更新）

- BYO 任务页的日程与后台模型下拉框只列出当前 BYO 模型目录里的 Type/具体模型，以及 Crew 原生“继承”或“Auto”配置项；历史官方或未知模型只作为只读告警显示，不能被再次选入 BYO。
- 主后端为官方时，BYO 任务页仅显示 Crew 当前原生模型值，不呈现可操作的 BYO 模型选择器；保留之前的不可写保护。
- Crew 原生模型发现的 BYO 适配不再将宿主的 `configured_default`（可能是旧官方模型）加入 BYO 模型目录。已成功读到空 Provider 目录时，也不再从旧 ACP 广告缓存复活过期模型。
- 保留原生 Cron 与 `agent.role_models.background` 的读写及 `expected_model` 冲突校验；保留之前的 Mate/Worker 执行路由保护。
- 对已有 0.2.5 安装中**已知的、未修改的旧 BYO 模型目录 helper**提供可验证的热升级兼容，未知第三方修改仍拒绝覆盖。
- 新增六项回归测试（其中 UI 测试实际执行组件逻辑、旧版 helper 使用语义升级模拟）；未进行 Windows KiroCrew RC1 实机端到端验收，官方计费亦未验证。

## 0.2.5 — Crew 0.11 RC1 Task Console unified patch (build-specific)

- One-page Tasks console now displays native Crew scheduled jobs and the background role only. Chat and crew-mate models remain in Crew's native UI; no duplicate global Harness switcher.
- The Task console reads native Crew config, schedule entries, and the current main Harness through one read endpoint; refreshes every 12 seconds. BYO-only edits write native Crew state. Official Harness mode is read-only.
- Optimistic checks reject stale background/scheduled-model selections. Script and command schedules do not have AI model pins.
- A BYO-only recent background execution card distinguishes configured Auto, Crew-resolved configuration, selected provider/model, and success/failure.
- Turn telemetry tags the actual Crew lightweight background role even for pinned concrete models and captures metadata-only error location (`file:line:function`) for new internal failures.
- The existing mate Conductor/Worker fail-closed routing safeguard is retained.
- **Limitations:** the screenshot's historical `AttributeError` cannot be repaired without its runtime traceback. Neither Windows live execution nor official credit billing has been verified; the new metadata will help isolate a fresh reproduction.

## 0.2.5 · Crew 0.11 RC1 Mate 2.0 older-BYO member recovery hotfix

- Fix `agent_sdk/backends_members.py` being rejected as an incompatible partial BYO patch when a previous successful Kiro BYO integration already registered member dispatch/panel but lacked Mate Worker Kit capability registration.
- Allow *only* the previous structural BYO signature (real backend import + both member capability declarations) and *only* the single deterministic Mate capability addition. The generated text must be idempotent and compilable; all other partial or unknown changes continue to fail closed.
- Preserve App version 0.2.5, original Crew/teammate/schedule model linkage and user data. This hotfix has unit/regression coverage but has **not** been verified against the user's actual Windows Crew tree.

## 0.2.5 (Crew 0.11.0-rc.1 compatibility candidate)

- Added session-scoped bounded Plan tool; improved historical MCP refusal diagnostics (Crew still owns permission).
- Kept 0.11.0-rc.1 exact-source installer gate and retired 0.10 path; no Windows desktop E2E claim.

## 0.2.5 · Crew 0.11 RC1 trust permission bridge and read-only Tool Center (candidate)

- A requested Crew MCP tool can revalidate against its existing, authenticated session-projected server before the ordinary ACP `session/request_permission` is sent. Missing/expired broker catalogues no longer force an immediate false-negative refusal; unmounted, unverified, excluded or Crew-rejected tools remain blocked.
- Verified-only tool search now waits (bounded) for the *live* Crew broker handshake rather than using a disk-cached catalogue as authority. No permission bypass, new host server or inferred grant is introduced.
- Emit precise `crew_binding_absent`, `crew_binding_unverified`, or `agent_scope_denied` refusal diagnostics. Add a read-only Tool Center displaying native tools, last-known Crew MCP tool names/health and recent refusals; historical data is expressly not a live grant. The UI never writes Crew authorization settings.
- Preserve the already-correct status bar, model picker, Tiga/Dyna routing, background Auto and Mate 2.0 safety guard. Crew trust/allow is still decided by Crew itself; real Windows `learn_add` and all Mate Worker tools remain pending E2E verification.

## 0.2.5 · Crew 0.11 RC1 picker/settings presentation update (candidate)

- In BYO conversation model menus, hide only the native `auto` and `auto:jev` rows when Type selections are available; keep the underlying IDs, backend default routing and Crew Settings Auto unchanged.
- Group Type choices separately from fixed provider models, deriving the Mode group caption from the installed policy pack; do not modify Tiga/Dyna policies.
- In Settings → Chat → Models, display `Tiga · Sky Type` and other friendly Mode/Type labels in both the default-model control and the selectable-model checkboxes without altering their React/API values.
- Keep the already-correct bottom status bar display logic unchanged. Scope to Crew 0.11 RC1 DOM seams and fail closed if the compiled frontend lacks those seams.
- Add cross-platform UI contract CI; real Windows desktop rendering, keyboard search semantics and native dropdown variations remain to be verified.

## 0.2.5

- Target **Crew 0.11.0-rc.1** (or `0.11.0rc1` version spelling) with a new isolated, fail-closed semantic adapter candidate. Crew 0.10 is retired as a supported installation target.
- Do not infer Mate 2.0 Conductor/Worker backend authority from the legacy global teammate default. Disable Stage 3 linkage mutations under Crew 0.11 until upstream integration tests validate that contract.
- Add non-destructive upstream-source preflight and CI checkout targeting the 0.11 RC1 tag. A successful version check is not proof of compatible Crew source structure.
- Preserve existing Tiga four Types, Dyna preview, provider configurations, ACP engine, Stage 7/8 safety and Stage 10 Mode lifecycle.
- **Candidate only:** the exact tagged Crew sources and Windows desktop E2E were unavailable in this build environment. Installation must fail closed on unknown source layouts.

## 0.2.3 · Stage 10 data-only Mode upgrade compatibility (engineering candidate)

- Prevent a managed upgrade of an **enabled** custom Mode from deleting its existing Type identifiers; stored Crew session/task Type selections may still refer to these IDs. Additions and policy parameter changes remain supported without disrupting the Mode identity.
- Allow intentional breaking Type migrations only after the custom Mode is explicitly disabled, preserving the existing no-implicit-enable rule. No claim that already-running work is automatically migrated or stopped.
- Verify that JSON policy updates appear on new registry reads while old immutable policy objects are not mutated; test actual routing/plan selection for all four Tiga Types and Dyna's fail-closed preview status.
- Add regression coverage to Windows, macOS and Linux CI (remote runs pending). Do not change Tiga/Dyna policy JSON, Crew member priority, or Auto/Jev model picker logic. No executable plugin hot-swap or external MCP exactly-once guarantee is added.

## 0.2.3 · Stage 8 engineering candidate (unreleased)

- Add atomic, persistent, per-Run tool-batch admission receipts. Deterministic opaque batch identifiers reject duplicated dispatch attempts under the same cross-process ledger lock and active Stage 7 owner lease.
- Verify completed receipts belong to the active Step/Attempt and reject duplicate or unmatched completion; block a successful Run terminal state while a tool admission remains unresolved.
- Retain the conservative unknown-side-effect recovery policy. These local receipts do **not** guarantee external HTTP/MCP operations execute exactly once, and no automatic replay is added.
- Add Linux-tested separate-process contention tests and a Windows/macOS/Linux CI matrix (remote CI and real Crew installation pending).

## 0.2.3 Stage 7 (engineering candidate; version unchanged)

- Introduce cross-process Run Lease ownership, periodic renewals and generation fencing for durable Run/Step/Attempt/Checkpoint writes.
- Prevent `session/new` / `session/load` from misclassifying live leased runs as crashed; allow explicit recovery only after lease expiry and safety checks.
- Deny late writes from an expired/replaced owner and prevent concurrent double-admission; never auto-replay a possibly side-effecting tool.
- Redact lease tokens from ACP and App diagnostics while leaving explicit Run cleanup semantics unchanged.
- Add three-OS regression matrix without changing Crew backend, Tiga, or Auto/Jev semantics.

## 0.2.3 Stage 6 (engineering candidate; version unchanged)

- Fence HTTP MCP session shutdown: bounded daemon network workers, prompt local cancellation and rejection of late responses; remote side effects remain unknown.
- Reject new HTTP MCP operations after closure and serialize concurrent JSON-RPC request IDs.
- Reject unknown durable Step/Attempt completion IDs without changing the persisted ledger, including completion under non-running steps.
- Add cross-platform regression test job; keep Crew backend selection, Stage 3 linkage, Tiga and Auto/Jev logic unchanged.

## 0.2.3 Stage 5 MCP shutdown integrity candidate (2026-10-25)

- Stop stdio MCP requests immediately when the session closes or the peer dies; no misleading 120-second pending call after closure.
- Fence each MCP process lifetime from stale reader messages and explicitly refuse post-close manager tool calls.
- Isolate stdio MCP subprocess groups and kill their owned process tree on close with cross-platform best effort and process-exit verification.
- Add red/green regression coverage and a Windows/macOS/Linux MCP shutdown CI matrix without altering the Crew native integration or Tiga.

## v0.2.3 — Stage 4 execution integrity candidate (same product version)

- Fix ProcessManager termination truth: after unsuccessful grace/force attempts, an OS-live process remains `running` and an explicit `ProcessError` is returned, rather than incorrectly freeing the process slot or claiming `killed`.
- Fail closed on late writes to completed/cancelled Durable Runs; preserve their persisted terminal status and revision, preventing delayed tool checkpoints/step updates from silently rewriting finished execution records.
- Admit new durable attempts only into running steps belonging to running Runs; prevent duplicate simultaneous attempts within one step. Recovery remains explicit and session-owned.
- Add red/green regression tests and a Windows/macOS/Linux CI job for these boundaries. Preserve Crew-native backend selection, Tiga Type/Model routing, and Stage 3 linkage semantics.
- Does not claim hard termination of unkillable OS processes, exactly-once tool effects, Crew `.5` verified integration, or real three-platform E2E. See `STAGE4_EXECUTION_INTEGRITY_REPORT.md`.

## 0.2.3 · Crew/ACP Stage 2 contract-hardening candidate (2026-10-25)

- Legacy `session/set_model` no longer returns fake success when a session is unknown or `modelId` is missing; invalid requests return JSON-RPC `-32602` without updating state.
- Model/config selection now excludes concurrent prompt turns via the session's existing `turn_lock`, returning `-32002` rather than changing the provider/model midway through a task. Valid idle-session selections retain the previous wire response and config-update event.
- ACP conformance reports list legacy `session/set_model` separately from stable protocol methods; no expanded capability claims.
- Add a real cross-process stdio ACP smoke test, negative-path tests, and Windows/macOS/Linux ACP contract CI matrix. Existing Crew backend selection, member priority, BYO Auto/Jev display compatibility, Tiga and Stage 1 task-management semantics are unchanged.
- App/Source candidate archives only (not a production-certified build) until exact Crew host contracts and native three-platform end-to-end tests are run. See `ACP_STAGE2_IMPLEMENTATION_REPORT.md`.

## 0.2.3 · Global Audit Fix 02 (2026-10-24)

- ACP session snapshots gain cross-process revision fencing and fail-closed stale-write detection, including migration of legacy filenames without duplicate session-list entries.
- Cross-process active-turn exclusion prevents overlapping prompts for one session.
- Model/tool execution stops if the required user-event checkpoint cannot be saved.
- New KiroCrew v0.10.0-insider.5 release is a pending source-contract target; no unverified parity claim.
- CI checks both v0.10.0-insider.4 and v0.10.0-insider.5, and extends Windows process tests.
- See `CREW_0105_AUDIT_FIX02_REPORT.md` for validation boundaries.

## v0.2.3 — Global Audit Fix 01: cross-process integrity and recovery-state consistency (same version)

- Serialize Durable Run, AgentState, Workspace manifest, Runtime Trace and Turn Telemetry mutations across Windows/POSIX cooperating processes via `state_io.py` stable sidecar advisory locks; preserve atomic writes and fsync file bytes before rename.
- Add monotonically increasing Run ledger revisions; do not lose side-effect history in concurrent checkpoint updates.
- Reject cancelled and already-active Run recovery consistently; share one recovery eligibility function between `RecoveryPolicy`, durable admission, and resumable UI lists.
- Fail closed on corrupt AgentState/Workspace manifest instead of silently erasing it; treat AgentState as optional projection on ACP session restore.
- Add real spawned-process and file fault-injection regression tests. No Crew patch, Tiga routing, Dyna execution, Provider or main UI change.
- Windows KiroCrew real E2E and cross-process **execution owner** lease remain unresolved. See `GLOBAL_AUDIT_FIX_REPORT_2026-10-24.md`.

## v0.2.3 — S4-02 data-only Mode lifecycle and version checks (same product version)

- Add optional semantic `plugin_version` and `registry_api` declarations; reject incompatible Mode packs before activation while remaining compatible with legacy unversioned JSON.
- Provide explicit install/update, enable/disable, single-step rollback and content-hash status for custom Mode packs with atomic writes and advisory locking.
- Reject attempted built-in Tiga/Dyna lifecycle modifications, downgrades and post-update manual-edit conflicts. Preserve the previous active pack when an update fails.
- Keep Tiga four Types, Dyna non-execution, existing Policy Pack APIs, Crew ACP, Provider routing and UI unchanged. Managed lifecycle is available through Python API only at this stage.
- Windows KiroCrew parity test pending. See `S4_02_MODE_LIFECYCLE_REPORT.md`.

## v0.2.3 — S3-02 Run event correlation & recovery diagnostics (same version, new build)

- Repair sparse `RuntimeTraceStore.recent` filtered searches so Run/Turn/Session queries inspect the whole retained bounded trace rather than a small tail.
- Attach stable top-level Run correlation to accepted/rejected recovery requests; include checkpoint's persisted session-event anchor in the corresponding runtime trace.
- Provide a read-only, content-free Run diagnosis assembled from existing Durable Run records, RecoveryPolicy and optional Runtime Trace. Summarize Step/Attempt lineage, recovery decisions and side-effect risk without duplicating user prompts or tool arguments.
- Extend `_kiro/run/get` with a bounded correlated trace timeline and `_kiro/run/list` with a lightweight summary; keep existing wire fields intact.
- Preserve explicit-client recovery ownership; no automatic retry, no second task database, no Crew UI or Tiga/Dyna behavior change. Trace loss never proves safety.
- Full regression and release checks documented in `S3_02_EVENT_CORRELATION_REPORT.md`; Windows end-to-end comparison pending.

## v0.2.3 — S3-01 Durable Run recovery safety (same version, new build)

- Keep unfinished, failed and cancelled Run records when the completed-history retention cap is reached; protecting recovery evidence takes precedence over a strict file-count limit.
- Persist a monotonic `side_effect_seen` bit independent of the rolling 64-entry checkpoint window; classify legacy saturated windows without that bit as unsafe to replay.
- Reject resume/retry/replan when durable evidence shows unverified tool side effects, even if the recovery-state field has an optimistic value.
- Require an existing durable checkpoint for potentially mutating tool batches in real ACP prompt transactions; low-level non-ACP characterization calls remain unchanged.
- Expose pending Runs beyond the nominal completed-history retention cap (up to the existing bounded response limit).
- Retain Crew ownership, Tiga Mode/Type/Model, disabled Dyna, all Provider mappings and current UI. Does not claim cross-process exactly-once execution, power-loss fsync durability or Windows desktop parity.
- Verification: 771 passed, 1 skipped, 52 subtests in the complete Python suite; Windows E2E not verified. See `S3_01_DURABLE_SAFETY_REPORT.md`.

## v0.2.3 — S2-04 child task lifecycle & resume admission (same version, new build)

- Prevent concurrent resume requests from launching more than one execution for the same BYO child Agent task; use a separate admission lock rather than blocking the worker's task state lock.
- Make AgentRuntime Future registration atomic and reject duplicate submissions before an in-flight task can be overwritten in the registry.
- While a child is running, keep new continuation text in its existing mailbox. Return an explicit `inProgress` / `messageQueued` acknowledgement instead of implying the task has completed.
- When a queued child is cancelled before a worker slot is acquired, report `cancelled`, not an artificial provider failure.
- Keep Crew's own member/schedule queues, existing ACP integrations, Tiga routing and disabled Dyna unchanged. Do not treat this as full Crew background-task parity or durable exactly-once execution.
- Verification: `761 passed, 1 skipped, 52 subtests passed` in local automation; Windows Crew E2E pending. See `S2_04_CHILD_LIFECYCLE_REPORT.md`.

## v0.2.3 — S2-03 Saved Agent tool scope & restore hardening (same version, new build)

- Align with Crew 0.10 projected Saved Agent semantics: absent `tools` means **no tools**, not an unrestricted default tool surface; ordinary sessions without an Agent retain normal host-governed tool access.
- Enforce Saved Agent's native-tool visibility again at actual execution, prior to approval or side effects. Reject model-invented/stale forbidden native calls with `refused` and audit evidence.
- Treat a fresh `_meta.kiro` envelope on session restore as authoritative: do not revive a retired Saved Agent from BYO's saved record when Crew no longer projects it.
- Add targeted regression coverage. Automatic Python suite: 756 passed / 1 skipped / 52 subtests; Windows Crew end-to-end comparison remains pending. See `S2_03_TOOL_SCOPE_REPORT.md`.

## v0.2.3 — S2 native Saved Agent / Skills / MCP parity guards (same version, new build)

- Fail closed when KiroCrew requests a saved Agent not present in its current trusted projection, or supplies multiple Agents without an active identity; do not silently pick another Agent.
- Validate tool visibility/auto-approval projection types before starting session MCP commands. Keep intentionally empty Agent prompts valid while preserving their tool restrictions.
- Match qualified MCP glob selectors and exclusions; prevent native read/write tags from accidentally granting similarly named third-party MCP tools. Expand native tool visibility/exclusion globs only over known native tools.
- Surface a session-verified, currently allowed `kirocrew-core/skill_search` MCP tool directly alongside the previously implemented Crew browser; cached/unverified servers and third-party lookalikes never gain the privilege.
- Preserve Crew ownership of schedules/agents, Tiga Mode/Type/model routing, disabled Dyna, Steering, tool governance, language and existing UI. Windows end-to-end/native Saved Agent feature-completeness remain unverified. See `S2_NATIVE_PARITY_REPORT.md`.

## v0.2.3 — S2-01 Tiga routing explainability (same version, new build)

- Preserve Mode → Type → Model and Sky/Multi/Power/Glitter policy weights; no random model rotation and no router LLM.
- Persist content-free per-turn routing diagnostics: selected Type, tier/capability candidate pools, scores, exclusions, selection source, selected model vs actually served model.
- Fence restored sticky pins to this turn's executable in-tier/capability fallback pool rather than all diagnostic/review candidates.
- Make completed-turn records explain why the Type selected a model; retain route evidence when the transient trace is gone. Add UI labels for all five locales.
- Retain disabled Dyna and current KiroCrew integration; full Windows model-routing parity remains unverified. See `S2_01_TIGA_ROUTE_EXPLAIN_REPORT.md`.

## v0.2.3 — S1-04 Tool execution & permission race guards (same version, new build)

- Recheck Crew cancel/steering signals between approval and dispatch for native and MCP tools; reject after control changes instead of sending stale operations.
- Revalidate the exact MCP `(server_name, tool_name)` identity, live-session verification and Saved Agent allowlist after Crew approval; invalidate replaced/revoked grants before effects.
- Do not trust unverified MCP `readOnlyHint` or `idempotentHint` to classify an external call as replay-safe; preserve uncertain side-effect boundaries on failed/cancelled turns.
- Surface potentially unknown outcome for failed dispatched MCP calls, and diagnose requests completed after steering/cancel without claiming remote side effects were rolled back.
- Reject duplicate `call_id` within the same provider tool-call batch, not across separate turns; new focused tests and suite-order-independent session restoration test.
- **Evidence limit:** still no hard cancel for every MCP transport, no cross-restart exactly-once, and no Windows KiroCrew end-to-end verification. See `S1_04_TOOL_EXECUTION_GUARD_REPORT.md`.

## v0.2.3 — S1-02 Steering / Queue reliability (same version, new build)

- Preserve Crew ownership of idle/follow-up queue while fencing late steering at the last provider response candidate; late messages return `queued: false` instead of being acknowledged and silently discarded at turn teardown.
- Add session-scoped message-ID receipt tracking: duplicate steering requests no longer inject the same text twice; reuse of one ID with different text is rejected.
- Correlate `steering_queued` and `steering_injected` with the same `messageId` in the Kiro session-info metadata, retaining the compatible `kind` and `content` fields.
- Append injected corrections to the provider-facing conversation under the session lock; publish injection only after the transcript update. Emit `steering_cleared` for any accepted-but-uninjected message during terminal cleanup or cancellation.
- Reject new steering after cancellation/closing begins; bound the in-process correction queue and the recent receipt ledger. Existing member/browser permissions, Crew-provided tools, Tiga routing and disabled Dyna are unchanged.
- **Evidence boundary:** `steering_injected` means the message entered the next model-facing transcript, *not* proof that the model completed the requested work; on-disk session persistence remains best-effort. Windows Crew Steering/Queue E2E verification is still outstanding.

## v0.2.3 — S0 基线固定与 S1-01 Crew 浏览器能力适配（同版本新构建）

- 固定 Language Fix Source 为规范源码：用户提供的安装 ZIP 与源码 ZIP 内所有 108 个公共文件完全一致；本构建继续保留既有语言、Tiga 单屏、Dyna 禁用、Crew 0.10 兼容逻辑。
- 对 Crew 核心 MCP 中**确实由宿主授予**的 `browser` 工具直接展示工具 Schema；不依赖模型事先执行 `tool_search` 才能发现内置浏览器。
- 对明确的浏览器交互请求，冷会话最多等待 8 秒同步 MCP 目录；第三方同名工具不自动提升为 Crew 浏览器。
- 严格遵守 Saved Agent 工具白名单与排除项；是否使用内置面板、是否建议 Playwright 回退、是否拒绝浏览由 Crew 原有浏览器工具决策，不新造第二套浏览器或绕过审批。
- Build ID 与 Runtime SHA-256 写入 `BUILD_INFO.json`，仍保留 `0.2.3` 展示版本；增加独立回归测试与 Windows 对照验收手册。
- **范围说明**：这是 S1-01 的静态与模拟环境修复，不表示 Crew 设置快照、Steering/Queue 状态机及 Windows 端到端验收已经完成。

## v0.2.3 — 语言识别与回复一致性修复（版本号不变）

- 修复简体中文误判为繁体中文：使用具有区分性的字形，剔除简繁通用字；中性短句继承最近用户的简繁习惯。
- 自动语言识别遇到仅含技术缩写的短消息时，优先使用最近用户消息；没有有效历史时可回退到手动选择的界面语言。
- 用户明确指定回答语言时，自动推理语言与最终回答语言保持一致；手动指定的推理语言仍独立有效。
- 明确要求可见 Thinking/reasoning summary 和最终自然语言回复使用所选语言及简繁字形，避免跟随英文 API/工具描述漂移。
- Detached review 的输出也继续继承同一语言策略，不翻译原始工具参数、代码、专有名词、引用文本。
- 不更改 Tiga、Dyna、ACP、Crew 集成、Provider 或模型路由；保留版本号 v0.2.3。
- 提示：这是对上游模型的语言约束，不能保证所有 Provider 都服从，也不会改变隐藏的内部推理内容。

### 0.2.3 — Modes one-page layout restoration (2026-10-21; same version, UI only)

- Restore the original Tiga Modes screen to a single viewport: all four Type cards and the two flexible detail panels fit the available content area without an extra bottom Dyna section.
- Keep Dyna's built-in Flash / Strong / Miracle definitions and runtime activation guards untouched. Until the in-place preview selector is implemented, Dyna remains a disabled entry in the Mode dropdown rather than a second panel below Tiga.
- Put Tiga first in the Mode dropdown. This changes presentation only; no Mode activation, routing, Crew integration, provider or ACP logic is modified.

### 0.2.3 — Overview setup-card layout hotfix (2026-10-21; same version, UI only)

- The Overview "Getting Started" panel now places all three steps in compact, content-height cards at the top of the panel, keeping each action immediately below its explanation instead of pinned to the viewport bottom.
- Keep the system-status panel alongside the cards without full-height stretching. Layout responds to BYO's actual embedded width (three/two/one cards); translations can wrap without clipping.
- Provider setup, integration checks, Agent Harness actions, APIs, policies and Dyna preview are unchanged.

## 0.2.3 — Crew parity guards, safe harness switching, Dyna preview (2026-10-21)

- Preserve explicit Crew member `backend_override` (including the empty-string official Kiro backend) when BYO is globally selected; upgrade existing BYO-owned 0.2.2 injection in place.
- Reconcile 0.10 model routing per **actual live session Provider**, using the new configured default only for cold slots, preserving native and BYO sessions during harness switching; upgrade the earlier injected helper in place.
- Report unavailable BYO model catalogues as HTTP 503 rather than silently returning an apparently healthy empty list, while preserving known concrete and ACP fallback entries.
- Reserve Dyna with non-executable Flash / Strong / Miracle Type previews; prohibit activation through UI, persisted config, API/ACP router, or custom-pack ID override. Keep Tiga functional.
- Make the BYO Modes page preview scrollable without displacing the existing Tiga execution details; unify App, Python, ACP and worker version labels.
- Regression: 699 passed, 1 skipped, 47 subtests passed; real Windows KiroCrew end-to-end parity is not yet certified. See `RELEASE_NOTES_0.2.3.md`.

### 0.2.2 — Overview responsive two-column layout hotfix (same SemVer)

- Keep the Overview performance chart as the flexible **left** column (about 68–70% of the remaining Kiro BYO content width) and provider health as the **right** column; both cards stretch to the same remaining viewport height after the heading, alert and stats cards.
- Constrain only the provider list's body to an independent vertical scroll region, so additional providers do not stretch the page or the chart. Keep provider card header visible.
- Measure the SVG chart's allocated space with a local `ResizeObserver` so it fills the actual available plot area rather than letterboxing a fixed 600×260 viewBox. Preserve the real hourly samples and gap semantics.
- Use a container query on the embedded BYO content width to fall back to a stacked, page-scrollable layout when the KiroCrew host leaves insufficient room for two readable columns.
- Add Chromium browser tests for 1555, 1440, 1300, 1200 and 1100px host viewports, including 25 simulated providers. All changes are UI-only; routing, telemetry, ACP and cancellation remain unchanged.

### 0.2.2 — trend layout and cancellation diagnostics hotfix

- Align hourly latency samples with the successful-turn average (exclude cancelled/failed durations, while retaining their request counts).
- Remove forced full-height overview trend panel and match SVG dimensions to the available width. A lone hourly sample is shown as a point with an explicit explanation instead of a fabricated line.
- Expose the already-recorded `cancel_source` in the request list and request detail. The `client_session_cancel` indicator means an ACP client issued `session/cancel`, **not** that the user manually clicked Stop.
- Do not change Provider, ACP cancellation behavior, request outcomes, or historical records. Five-locale resources are maintained.

## 0.2.2 — Overview, retained task features and Sky response tuning (same version)

- Replace the misleading "当前执行" view (which inferred a live process from a persisted Durable Run) with a 24-hour request trend and measured runtime performance breakdown. Only completed/error telemetry contributes; empty hours stay empty, and the Records page retains individual request details.
- Store content-free route feature vectors inside the same bounded TurnTelemetry entries as completed calls. Each feature vector is trimmed and cleared with its request. The Modes panel no longer searches an unrelated 200-event RuntimeTrace tail for the last task feature.
- Make the request-history clear action clear both TurnTelemetry and RuntimeTrace (including trace-derived failure evidence) rather than leaving stale adaptive evidence or orphan feature displays; this does not delete KiroCrew sessions or durable recovery state.
- Retune Sky's built-in route scoring toward speed/economy over quality, while retaining capability gates, fast-tier routing and rare exceptional-risk review. This changes model preference, not upstream network/runtime guarantees.
- Show per-Type measured median completion time, plus aggregate upstream/tool timings on the overview, to distinguish provider latency from tool-bound scheduled jobs. KiroCrew-owned schedule execution logic remains unchanged.

## 0.2.2 — Mode-level sidebar card + responsive navigation fix (same version)

- Correct the sidebar status card to describe the active **Mode Pack** instead of the active Type: dynamically use the active pack's public name and description (Tiga now; future Dyna packs need no UI changes). Do not show Type names, worker limits or Type-specific review policy in the compact card.
- Keep mode-neutral integration, provider/model and native-tool counts, and link to mode settings. Update Tiga's public mode description to be mode-level and Type-free, without changing execution policy.
- Separate the scrollable navigation region from the docked mode summary card to prevent the card from colliding with the last navigation buttons on shorter windows; preserve the compact horizontal navigation at narrow widths.

## 0.2.2 — Management UI polish + failure-state color pass (same version)

- Replace geometric Unicode navigation placeholders with consistent semantic outlined SVG icons in the Kiro BYO App sidebar; no new icon library or runtime dependency.
- Widen the desktop management rail and enlarge the bottom-left Mode card using the selected policy's real public catalog metadata: new-session default Type, short explanation, delegation/parallelism/review policy, integration/tool status, and navigation to the detailed Mode page. Sidebar remains scrollable on short windows; selected navigation is marked by a short rounded accent inside the active row, with matching icon tint and visible keyboard focus.
- Apply danger-red consistently to failure/error counts and labels in overview, sidebar, records, four-Type usage, account queries, task status and legacy local diagnostics; keep cancelled/interrupted/retry/missing-credentials in the warning category. All changes are presentation-only; schedule-task Type guard and model routing are unchanged.

## 0.2.2 — Scheduled-task model picker hotfix (A only; same version)

- Exclude scheduled-task editor dialogs from chat-only Type presentation, including dialogs opened above a chat route and model popovers rendered through a portal. Hide the whole Type option row, make non-chat Type options inert, and restore the normal chat picker on dialog close.
- Preserve KiroCrew's one stable shared catalogue, native Auto/concrete models, SPA navigation, and Type selections in real chat pickers. No changes to routing, telemetry, or the four-Type task-feature panel in this hotfix.

## 0.2.2 — ACP lifecycle and stdout safety (same version, new build)

- Reject concurrent session replacement or restore while a turn owns the runtime; protect reused session IDs with lifecycle reservations.
- Retire sessions atomically, and defer MCP/native close until the outgoing turn has quiesced rather than force-closing after a timeout.
- Make outbound stdout failures terminal and cap queued frames at 2048; preserve nonblocking enqueue under turn/state locks.
- Add event-controlled concurrency regressions and `LOCK_AND_SESSION_OWNERSHIP.md`.

## 0.2.2

- **Interactive Type advertisement hotfix (same version):** Kiro BYO now advertises the active Mode's namespaced Type selections in each interactive ACP session, with the session's real Type wire id as `models.currentModelId` and as the model config `currentValue`. This makes `Tiga — Sky/Multi/Power/Glitter` pass KiroCrew's per-session model allowlist before `session/set_model` is forwarded. The shared/global catalogue and the `kirocrew-lite` background worker remain `auto + concrete models` only, so Type routing ids do not leak into background/system-task model surfaces. Config updates preserve the same truthful session-scoped catalogue after every Type/concrete switch.

- **Provider-boundary refactor (same version):** remove remaining vendor-id control flow from `config.py`, `provider_admin.py`, `account_status.py` and `provider_products.py`. Historical official endpoint migrations, commercial product/profile differences, credential-family hints/rejections and account-query strategy selection are now catalogue/capability data. The generic layers ask for verified endpoint/product capabilities instead of branching on Gemini/MiniMax/Xiaomi/DeepSeek/Moonshot names.
- **Responses transport capabilities (same version):** replace provider-brand conditionals inside `providers/responses_api.py` with immutable `ResponsesCapabilities` descriptors. Stateful continuation, retryable response-id expiry, cache headers, store/cache policy, reasoning wire style and sampling-field stripping are now transport capabilities; `registry.py` derives every shared Responses adapter from that capability registry.
- **Mistral shared Chat/SSE engine (same version):** reduce `mistral_chat.py` from 402 to 223 lines by injecting a `ChatContentCodec` into `chat_completions_common.run_native_chat_completion`. Mistral keeps its native ThinkChunk/TextChunk merge/replay semantics while request opening, SSE parsing, usage accounting, tool-call deltas, interruption and provider-context serialization are owned by the shared engine. Boundary tests now prevent these three abstractions from regressing back into vendor branches or duplicated streaming loops.

- **Turn-loop structural refactor (same version):** split the historical ~995-line `_run_provider_tool_loop` into a 273-line orchestration method plus focused routing/round/tool/telemetry/post-stage helpers. A `_ProviderLoopState` dataclass now owns cross-round mutable state, while all ten historical `continue` edges remain explicit in the main loop with the original four `step -= 1` rewinds preserved exactly. The existing fail-closed durable checkpoint, cancellation partial-text recovery, worker hand-back, retry/fallback and ExecutionGuard semantics are unchanged and regression-tested.
- **Declarative provider catalogue (same version):** move provider presets, transport labels, routing scores and verified model capability profiles out of `provider_catalog.py` into versioned `src/kiro_byo/catalog/provider_catalog.v1.json`. The Python module now performs load/validation/query/merge only; declarative model match rules support exact/family matching so adding a profiled model no longer requires editing Python branches. Every routing score row carries a source/note field and malformed bundled data fails at load with a precise schema path.
- **Provider catalogue layering cleanup:** `ProviderPreset.public_dict()` now serializes preset-owned fields only; Platform/Product/Profile enrichment moved to the `public_presets()` API assembly layer, preserving the public payload while removing the data-class -> product-catalog dependency inversion.
- **Latest-tree test/release alignment:** update stale tests from the renamed provider-neutral `tool_specs()` API, follow the moved `task_profile_for_signals()` ownership in `mode_router.py`, preserve the new recovery-busy `-32002` contract, and fix one stale `_official_endpoint_for_preset` reference in the current source tree.

- **Shared-catalogue / SPA hotfix (same version, supersedes the earlier boundary-reload workaround):** Kiro BYO now returns one stable `Types + concrete models` catalogue for KiroCrew 0.10 instead of changing `/api/models` by page. The dashboard no longer wraps `window.fetch`, never calls `window.location.reload()`, and keeps every KiroCrew rail/page transition SPA-native. Non-Chat surfaces hide BYO Type sentinels only at presentation time.
- **Existing-chat Jev migration (same version):** when the configured harness changes to Kiro BYO, stale native `Auto (Jev)` slot state (`model=auto`, `jev_route=true`) is reconciled in place to the active Mode default Type (for Tiga, normally Multi) and broadcast to the current composer. The user no longer needs to close/reopen a conversation. Explicit concrete-model pins are preserved. The reverse transition clears BYO-private Type sentinels before another harness sees them.
- **Insider.4 contract lock (same version):** retain the cross-platform `index.html`/`data-model-id` presentation seam and the composed-owner backend adapter, now explicitly audited against KiroCrew `v0.10.0-insider.4` commit `e9bfada`. Windows, macOS and Linux use the same semantic adapter; no Vite hash, minified helper shape, drive letter or OS install path is part of compatibility detection.
- **Regression coverage (same version):** add a navigation test proving BYO preserves the original `fetch` function and performs zero full-page reloads across Chat/Settings/Agents transitions, plus executable slot-routing tests for Jev→Type migration, concrete-pin preservation, reverse Type cleanup and reconciliation idempotence.

- **KiroCrew 0.10 dashboard compatibility hotfix (same version):** remove the compiled `withJevRoute` / Auto(Jev) bundle patch entirely. Real Insider builds may inline, split, lower or rename that helper differently across Vite/Rollup outputs, so a minified callable is no longer treated as an integration contract.
- **Surface-pure shared catalogue (same version):** interactive Chat pages explicitly tag `/api/models` and receive the active Mode Types plus concrete models; non-Chat pages receive concrete models only. Crossing the Chat/non-Chat SPA boundary performs one controlled reload so KiroCrew's shared React-Query `availableModels` cache is rebuilt for the destination surface. Chat-to-Chat and non-Chat-to-non-Chat navigation stays SPA-native.
- **Cross-platform dashboard integration (same version):** dashboard installation now modifies only the BYO-owned block in `static/dist/index.html` after proving the authored `data-model-id` DOM contract. No `App-*.js`, content hash, minified identifier, Windows path, macOS bundle path or Linux package path participates in compatibility detection.
- **Fail-closed contract retained:** a missing `static/dist`, missing `index.html`, malformed shell insertion point or disappearance of the authored `data-model-id` picker contract still aborts before commit. Unknown compiled helper shapes no longer matter because no compiled helper is patched.
- **Regression coverage:** add 0.10-style fixtures with intentionally unrecognizable/split helper shapes, arbitrary chunk names, idempotent HTML-only installation, Chat model tagging, and a boundary-reload simulation proving no reload occurs for Chat-to-Chat navigation.

- **Background-system-task integration + observability cleanup (same version):** the BYO Tasks page now exposes KiroCrew's host-owned `agent.role_models.background` as a dedicated “后台系统任务” model control instead of pretending title/status-card/summary work is a scheduled cron. `kirocrew-lite` background `auto` is materialized to one concrete BYO model through the current Mode/Type policy rather than entering interactive Type orchestration, and the dashboard auto-title path now honors the same background-role pin when Kiro BYO is the active harness. The system overview renames “当前任务” to “当前执行” and shows the active Type plus a separate Run id.
- **Host-compatibility surface simplification (same version):** keep only the actually managed KiroCrew row in the Settings host-compatibility card. Generic ACP conformance remains in the separate protocol-check card and unverified third-party host rows are no longer presented as product integrations.
- **KiroCrew Insider.4 source audit (same version):** re-audit the semantic adapter against the user-supplied `0.10.0-insider.4` source tree, including the moved/composed chat seams and the new background-title seam; the adapter verifier compiles every transformed owner and passes the Crew-authoritative registry/capability contract before `verified_through` is advanced.
- **KiroCrew 0.10 shared-catalogue/Chat-overlay correction (same version):** align the BYO Type projection with Insider.4's actual frontend source contract. KiroCrew deliberately uses one `['available-models', provider.id]` React-Query value for Chat, Settings, Agents and other model surfaces, so BYO Type sentinels are now captured from a Chat-tagged `/api/models` response and removed before that shared query resolves. The compiled Chat-only `withJevRoute` seam receives the active Mode Types as a local overlay instead. Settings therefore derives its native count/options from `auto + concrete provider models` (four configured MiMo models => five entries), while the conversation picker keeps the active Mode's Types and concrete models with native Auto/Jev suppressed. This is Mode-pack-driven rather than Tiga-count-driven, so future Dyna Types use the same boundary.
- **Chat/Settings surface-boundary hotfix (same version):** stop treating every route containing a `chat` segment as the interactive conversation surface. KiroCrew Settings -> Chat is now explicitly excluded, so only the real conversation route receives `kiro_byo_surface=chat`; global Default/Selectable model settings keep `auto` plus concrete provider models and never receive Tiga Type sentinels. A fail-closed stale-cache guard also hides any cached Type row that survives SPA navigation into Settings.
- **Picker visual-stability hotfix (same version):** scope the dashboard MutationObserver to actual Type-picker mutations instead of redecorating the whole picker after unrelated transcript/context/capability DOM updates. Decoration writes are idempotent and frame-batched, and Type pickers reserve stable scrollbar space, reducing visible refresh/jump when KiroCrew refreshes per-selection context or capability UI. No context-window value or model capability is fabricated.
- **Integration fixed-point repair (same version):** make generated KiroCrew seams converge to an inspector-proven `patched` state before commit instead of assuming every owned `upgradeable` seam finishes in exactly one rewrite. The transaction now performs a second semantic commit audit after byte verification, preventing `install_from_app()` from returning success while the immediate App verification still reports `upgradeable`.
- **Dashboard guard fixed-point repair (same version):** stabilize the Type/model safety guard in memory before writing `static/dist/index.html`, then re-audit the committed dashboard shell before installing the runtime. Integration status now reports a missing-or-stale guard accurately instead of describing every mismatch as simply uninstalled.
- **Type/model boundary cleanup (same version):** keep KiroCrew model-only surfaces real-model-only while restoring Tiga Types to the interactive `/chat` picker through a chat-scoped integration projection. Default/Background/Subagent/Fallback/App model pickers no longer treat Sky/Multi/Power/Glitter as real models, and a non-chat UI guard prevents short-lived shared-cache carry-over after SPA navigation. The Type wire ids remain a KiroCrew chat transport shim only; product semantics stay `Mode != Type != Model`.
- **Mode/Type UI semantics:** system overview and the sidebar current-configuration card now report the global Mode as `Tiga` only. Sky/Multi/Power/Glitter cards on the Modes page are view-only: clicking a card changes only the inspected Type, survives the periodic status refresh, and never writes the global configuration. A new chat still defaults to the Mode pack's default Type (Multi for Tiga), while a chat can select another Type independently.
- **Scheduled-task model/context routing:** add a BYO Tasks page between Accounts and Records that reads KiroCrew's existing schedules and lets each job leave its model unpinned (`Auto`) or pin one real provider model. The page also exposes KiroCrew's existing persistent-session/minimal-context execution flags so long-running jobs can choose fresh/minimal context instead of indefinitely replaying history. Scheduling remains KiroCrew-owned; its native model-unavailable fallback semantics are preserved rather than silently reimplemented.
- **Routing diagnostics and thinking hard locks:** request records now expose start/end timing, session, selection source/request and thinking preference/effective mode, and failed requests surface the provider failure reason directly in the detail drawer. Provider metadata refresh preserves explicit Thinking On/Off/Auto, and the generic OpenAI-compatible transport either maps an explicit Off safely or fails closed instead of silently ignoring the user lock.
- **UI / Tiga refinement (same version):** make failed-mode usage counts use the danger/red state, stack ACP protocol and host compatibility cards vertically, render Glitter's real heterogeneous strong-lead/fast-worker scope as `mixed`, and replace the awkward “未物理测试” label with “未实机验证” without claiming certification.
- **Type-specific parallel budgets:** keep Sky/Multi at 1/2 while raising Power/Glitter to 3/4 maximum read-only workers respectively. These are ceilings only; the planner still keeps narrow or write-heavy work single-worker/single-writer as appropriate.
- **KiroCrew 0.10 compatibility:** add a separately source-audited `v0.10-adaptive` adapter for `0.10.0-insider.4`. The ACP/runtime/backend registry seams remain semantically compatible; the dashboard effort bridge now patches the moved `dashboard/chat_api/slot_effort.py` owner and imports its BYO helpers into `dashboard/chat_handlers.py`, matching KiroCrew 0.10's `chat_api.compose` namespace contract.
- **Fail-closed 0.10 composition contract:** verify the composed-owner rebinding seam and the unmodified overlay dependencies before any write. Unknown owner/composition shapes are refused instead of guessed.
- **Regression coverage:** add a 0.10 dry-run verifier, adapter unit tests, and installation/rollback validation while retaining the existing 0.9 adapter unchanged.

## 0.2.0

- **Productized one-screen management UI:** replace the older vertically stacked/debug-oriented surface with the compact Overview / Providers / Models / Modes / Accounts / Records / Settings information architecture. The removed Tasks and standalone Integration pages are not product surfaces in the final 0.2.0 UI.
- **Truthful Modes observability:** Type cards and detail panels read execution semantics from the Policy Pack/runtime projection instead of duplicating policy tables in JavaScript. Recent task-shape values come from real `model.route.selected` signals; missing evidence renders as unknown rather than attractive fixed percentages. The right-hand panel shows the selected Type's real model usage over the latest 60 hours.
- **Records instead of a debug-console-first Diagnostics page:** request outcomes, token/cache usage, routing evidence and durable recovery are presented as user-readable records. Raw Runtime Trace JSON remains available only as drill-down evidence. Internal Agent/Worker/Reviewer terminology is not exposed as a top-level product concept.
- **Provider/model/account surfaces:** Providers use shared native transports where wire formats match; Models are grouped by Provider with verified/declared capabilities; Accounts only reports official account/quota facts that the configured credential can actually query and leaves unknown facts unknown.
- **Settings consolidation:** KiroCrew integration and ACP conformance move under Settings. The standalone Integration navigation entry, external-ACP-Agent management UI and workspace diff/apply management UI are removed; their underlying runtime/control-plane code remains isolated from the normal product surface where still required by ACP execution contracts.
- **Management/backend contract hardening:** remove unused workspace/external-agent management HTTP routes, trim the bounded records projection to data actually consumed by the UI, reject editor/patch backup artifacts at build/release time, and add release checks for undefined UI pages and UI/backend route drift.
- **0.1.7 runtime retained:** deterministic adaptive routing, Policy Pack schema v4 orchestration, bounded internal execution, durable Run recovery, canonical session events and provider-neutral ACP semantics continue underneath 0.2.0. The UI productization does not create a second execution engine.
- **Version bump:** App/package/ACP surfaces move from `0.1.7` to `0.2.0`.

## 0.1.7

- **Agent Fabric completion batch:** add a bounded `AgentTeam` coordination layer over the existing Agent Runtime with a stable Leader/member roster, shared task board, limited team messaging, dynamic spawn/recycle, and explicit `max_agents` / `max_parallel` / `max_depth` limits. Dynamic workers and specialists remain read-only by default; nested spawn is bounded rather than swarm-like. Internal/package/App/ACP version remains `0.1.7`.
- **Agent Fabric completion batch:** add isolated multi-writer candidates through fail-closed Git worktrees. Writer AgentTasks receive a private NativeToolRuntime rooted in the worktree; candidates can be inspected, checked and compared independently, and one explicitly selected diff can be applied only to a clean parent checkout after `git apply --check`. There is no automatic conflict resolution, implicit merge or commit.
- **Agent Fabric completion batch:** add external ACP Agent workers behind the same `AgentTask` / `AgentResult` contract as raw-model children. A local `config/acp_agents.json` catalog can launch a configured stdio ACP Agent, initialize it, create a child session, stream its visible agent message and project the result back to the Leader. External ACP workers are read-only at the current trust boundary and permission requests fail closed by default.
- **Agent Fabric completion batch:** separate generic ACP-v1 host semantics from KiroCrew `_meta.kiro` model/effort/thinking hints with explicit host adapters. Add a protocol-level conformance report and `scripts/acp_conformance.py` process smoke harness with generic/KiroCrew/Zed/JetBrains/Qwen Code profile labels. These named profiles describe protocol expectations and do not claim physical certification against every proprietary client/version.
- **Agent Fabric completion batch:** extend Diagnostics/Agent projection with backend, workspace and Fabric/conformance metadata; exclude `data/agent_workspaces` from all archives; close isolated writer NativeToolRuntime instances on every terminal child path; preserve generation `0` for a dynamically spawned AgentTask's first execution and increment only on later continuation/resume.

- **Retry / Resume / Replan + durable-recovery batch:** add a single provider-neutral `RecoveryPolicy` used by live turns, Subagents and persisted Runs. Clean retryable provider failures may perform one bounded same-model retry before fallback/replan; non-retryable provider failures become `replan_required` rather than being mislabeled as transport-retryable. Internal/package/App/ACP version remains `0.1.7`.
- **Retry / Resume / Replan + durable-recovery batch:** turn the durable ledger into an executable recovery boundary. `_kiro/run/retry`, `_kiro/run/resume` and `_kiro/run/replan` reconstruct the original prompt from its persisted `user/message` event, rewind to the recorded transcript boundary only after winning the session turn lock, close orphaned attempts/steps as `interrupted`, and continue under the same stable Run id with explicit recovery counters/events.
- **Retry / Resume / Replan + durable-recovery batch:** make Type replan semantically real by clearing only the stale Tiga route pin before fresh routing, while concrete-model selections remain pinned and can only retry/resume. Recovery requests are revalidated again inside the acquired turn boundary so a concurrent ordinary prompt cannot be rewound by an out-of-band recovery request.
- **Retry / Resume / Replan + durable-recovery batch:** checkpoint mutating tool admission before execution. A restart that crosses that boundary is classified `unknown_side_effect` and recovery fails closed instead of replaying an uncertain write. Clean/read-only interrupted Runs surface safe recovery methods through ACP session metadata and Diagnostics.
- **Retry / Resume / Replan + durable-recovery batch:** add one bounded transient-provider retry to isolated read-only Subagents and regression coverage for clean cross-process resume, transcript rewind, Type replan, turn-lock races, side-effect rejection and host-visible recovery metadata. Background scheduling remains owned by KiroCrew.

- **Agent observability / durable-run batch:** project Subagent lifecycle state into a bounded cross-process metadata snapshot so the Kiro BYO management UI can show real `task_id`, role, provider/model, continuation generation and which ACP-native controls (`send` / `cancel` / `resume`) are valid without duplicating child transcripts or queued message text. The authoritative control path remains the live ACP session boundary.
- **Agent observability / durable-run batch:** add a provider-neutral durable execution ledger with explicit `Run → Step → Attempt → Checkpoint` identities. Every real prompt turn now creates a durable Run, checkpoints the ready boundary, completed subagent groups and tool batches, then closes the attempt/step/run with a recovery state. Checkpoints reference the canonical session event log instead of copying conversation content.
- **Agent observability / durable-run batch:** mark interrupted non-terminal Runs as `pending_restart` on session reload and mark stale in-process child projections as paused rather than pretending their isolated transcript survived a process restart. Automatic cross-process replay remains intentionally disabled until side-effect-safe resume/replan semantics are implemented.
- **Agent observability / durable-run batch:** expose recent Agent Runtime and Durable Run state in Diagnostics plus ACP `_kiro/run/list` / `_kiro/run/get` inspection endpoints. Runtime state directories are excluded from both release/source archives. Internal/package/App/ACP version remains `0.1.7`.

- **Subagent Runtime / parallel-continuation batch:** promote planned workers into real child tasks with stable `task_id`, plan-local names, isolated transcript snapshots, per-child sticky concrete model pins, read-only tool contracts, cancellation tokens, mailbox continuation, structured `AgentResult` hand-back and bounded in-memory lifecycle state. Child transcripts are not merged wholesale into the parent; only explicit findings are handed back to the Leader. Internal/package/App/ACP version remains `0.1.7`.
- **Subagent Runtime / parallel-continuation batch:** allow broad, parallelisable, low-write work to plan up to two read-only workers under the existing single-writer authority rule. Multi uses fast children when its adaptive delegation gate admits them; Power uses strong children; Glitter uses fast children. Distinct eligible models are preferred for sibling workers, with safe same-model reuse when the configured pool has only one teammate.
- **Subagent Runtime / parallel-continuation batch:** execute admitted child model calls concurrently through a bounded `AgentRuntime` while serialising shared NativeToolRuntime execution to avoid sandbox/audit races. Child tools remain fail-closed: only BYO-owned native gathering capabilities are admitted in this batch; mutating tools and third-party/MCP tools remain outside the child boundary. Parent cancellation interrupts children and session close tears down the child executor.
- **Subagent Runtime / parallel-continuation batch:** add in-process continuation controls (`_kiro/agent/list`, `_kiro/agent/send`, `_kiro/agent/cancel`, `_kiro/agent/resume`). A completed child can be continued by the same `task_id` with its retained isolated context and pinned model; messages sent to a running child are consumed at the next model boundary. Process-restart durability is intentionally deferred to the later Run/Checkpoint layer.
- **Subagent Runtime / parallel-continuation batch:** account child token/provider-round usage in the parent turn, emit Agent Runtime Inspector/session lifecycle events, and add concurrency, isolation, stable-identity, continuation, multi-worker planning and hand-back regression coverage.

- **Unified Agent Runtime / four-Type orchestration batch:** add provider-neutral `TaskProfile`, `ExecutionPlan`, `AgentSpec`, `AgentTask`, `AgentResult` and a bounded in-memory `AgentRegistry`. The Turn Engine now consumes a generic execution graph and records role lifecycle events instead of treating Glitter collaboration as a Type-specific execution branch. Internal/package/App/ACP version remains `0.1.7`.
- **Unified Agent Runtime / four-Type orchestration batch:** promote the built-in Tiga Policy Pack to schema v4 with data-driven orchestration objectives, worker pools/budgets and bounded single-writer constraints while retaining schema-v1/v2/v3 compatibility. Sky stays latency-first/single by default; Multi can admit a fast read-only worker for broad low-write gathering; Power can admit a strong read-only worker plus strong verification; Glitter maps to the same generic strong Leader + fast read-only Worker + strong Reviewer runtime.
- **Unified Agent Runtime / four-Type orchestration batch:** replace scalar-only orchestration decisions with a deterministic multi-dimensional task shape (`breadth`, `parallelizability`, `risk`, `tool_intensity`, `write_intensity`, `context_demand`, `verification_value`, `latency_sensitivity`) while keeping existing model-capability gates and adaptive model scoring intact. Broad read-only work can be delegated; mutation-heavy work stays with the single writer.
- **Unified Agent Runtime / four-Type orchestration batch:** add Selection Truth to session state/persistence and Runtime Inspector events. Every selection records `type` vs `concrete`, source/requested id, and whether Tiga routing is actually applied; explicit concrete-model pins remain supported but can no longer masquerade as an active Type in diagnostics.
- **Unified Agent Runtime / four-Type orchestration batch:** add regression/release-contract coverage for Agent lifecycle identity/bounds, four-Type execution graphs, fast-vs-strong worker pools, write-heavy delegation suppression, Selection Truth persistence/switching, and the invariant that generic execution planning contains no Type-name branches. Parallel/background child execution remains intentionally out of scope for this batch.

- **Recovery / hardening batch:** normalize premature HTTP/SSE EOF, `IncompleteRead`, socket/timeout and connection-open failures into provider-neutral transport errors instead of leaking raw exceptions to ACP `-32603`. A pristine transport failure receives one bounded same-model retry before the existing Type fallback policy is considered.
- **Recovery / hardening batch:** make every failed provider/tool turn return the session to a reusable state. Read-only/pristine turns roll back transactionally; turns that admitted potentially mutating tools preserve their transcript and add a recovery boundary so the next model verifies unknown external side effects instead of blindly replaying writes. Unexpected harness exceptions use the same recovery boundary rather than poisoning the conversation.
- **Recovery / hardening batch:** classify native/MCP tool side effects conservatively (`pure`, `read`, idempotent/non-idempotent/unknown write), add explicit failed/cancelled/rollback/runtime events, and record the source plus elapsed time of incoming `session/cancel` notifications for investigation of the observed ~45-second cancellation cluster.
- **Recovery / hardening batch:** extend metadata-only diagnostics with error categories, transport retry/fallback counters and per Provider/model health summaries. Transport errors now count as diagnostic failures instead of disappearing behind the ACP catch-all.
- **Recovery / hardening batch:** strengthen the existing Reasoning Language generation hint so user-visible reasoning begins in the resolved conversational language and does not default to English merely because technical material is English. This remains a provider-side presentation hint: no reasoning translation or hidden chain-of-thought request is introduced.
- **Recovery / hardening batch:** add regression coverage for truncated streaming/non-streaming bodies, clean retry, partial-stream rollback, post-mutation recovery, continued use of the same session after an unexpected runtime exception, cancellation-source tracing, provider-health aggregation and the stronger visible-reasoning language contract. Internal/package/App/ACP version remains `0.1.7`.

- **Hotfix 2:** close the remaining Type-effort presentation race in KiroCrew's model picker. Crew keys `selection-capabilities` by slot rather than the selected model, so after switching from an effort-capable concrete model to a Tiga Type the previous `effort_supported=true` answer can remain rendered while the invalidated query refetches. The BYO-owned dashboard guard now uses the picker's authoritative selected `kiro-byo-type:*` row to hide only the embedded effort block during that stale-capability window.
- **Hotfix 2:** preserve KiroCrew's native concrete-model effort UI unchanged. The browser guard performs no provider/model capability fetch or name heuristic; selecting any concrete model removes the Type annotation and Crew continues to render its native `更快 ↔ 更聪明` control from live capabilities. Cold/live backend Type suppression and the REST `effort_managed_by_type` boundary remain the protocol/state authorities.

- **Hotfix 1:** harden the KiroCrew 0.9 dashboard integration against same-version Insider source reshaping. The `chat_handlers.py` transformer now locates cold/live capability and reasoning-effort seams by AST semantics rather than exact local names/one-line source strings, so harmless `_live`/`backend` renames, wrapping and formatter changes no longer make `0.9.0-insider.7` appear incompatible.
- **Hotfix 1:** keep the integration fail-closed at the semantic boundary. If Crew removes or materially restructures the effort commit contract, installation still refuses the patch instead of guessing an insertion point; already-owned older BYO dashboard seams remain upgradeable in place.

- Promote the built-in Tiga pack to **Policy Pack schema v3** so Type character is not hidden in Router Core magic numbers. Complexity-response shares, runtime-evidence shaping/blend, fit biases, capability-fallback scoring, verifier/reviewer scoring, Glitter worker routing and same-provider affinity are now data-driven policy facts; schema-v1/v2 custom packs keep compatibility defaults.
- Keep the hard-coded part intentionally small and correctness-oriented: image/tool/context eligibility, explicit user Thinking locks, concrete model capability truth and fail-closed tier semantics remain core invariants. Model preference/tuning remains policy/runtime data.
- Add **Tiga Adaptive Routing v1** without a Router Model. Sky/Multi/Power/Glitter remain deterministic, inspectable Policy-Pack strategies; hard capability gates still run before scoring.
- Blend real recent runtime evidence into model ranking. Recent TTFT/total latency dynamically adjusts effective Speed, while bounded global and same-task success history contributes a separate Reliability signal. Recent observations decay more slowly than old ones and low-sample evidence is automatically shrunk toward static priors.
- Keep epistemic boundaries explicit: Quality and Economy remain provider/curated catalogue priors until Kiro BYO has trustworthy provider-neutral measurements. Runtime success is not mislabeled as model intelligence and token volume is not mislabeled as cost.
- Add deterministic coarse Task Profiles (`Code`, `Vision`, `Tool`, `Long Context`, `Research`, `Casual`) for task-local reliability evidence. No semantic Router LLM, random exploration or hidden model-selection call is introduced.
- Extend metadata-only TurnTelemetry to schema v2 with Mode/Type, task profile, selected routing score and runtime confidence. Existing telemetry remains readable; older Type turns can still be identified from their namespaced selection id.
- Add a bounded mtime-keyed adaptive snapshot cache so Glitter/relay subroutes reuse one telemetry snapshot instead of rereading the ledger for every worker invocation.
- Add a read-only `/routing` App API and integrate Adaptive Routing directly into the existing **Modes** page. The UI shows four Type internals, learned-turn/model counts, Static Prior -> Runtime Evidence -> Effective model profiles, runtime latency/reliability statistics and recent route traces/candidates. It intentionally exposes no routing-weight controls.
- Add regression/release-contract coverage for adaptive latency/reliability behavior, Task Profile classification, read-only routing observability and preservation of all existing Tiga Thinking/Effort/Glitter boundaries.

## 0.1.6

- Add a provider-neutral **Reasoning Language Policy** for user-visible Thinking/reasoning presentation. `Auto` follows the user's current conversational language using the current message plus recent user turns rather than the KiroCrew UI locale.
- Preserve technical terminology in its natural/original form instead of mechanically translating ACP, LLM, Token, API/provider/model names, code identifiers, parameter names, paths, commands and mathematical notation.
- Keep the policy explicitly separate from hidden/private chain-of-thought: it is a generation-time presentation hint for reasoning text/summaries the upstream model already exposes, with no post-hoc translation pass and no request for additional private reasoning.
- Resolve the language once per turn and reuse it across the primary model, tool-loop continuation rounds, Tiga relay workers and detached verifier/reviewer stages. Mid-turn user steering re-resolves the policy for subsequent rounds.
- Add `Settings -> 推理语言` to both the KiroCrew App UI and localhost management UI with Auto, Simplified Chinese, Traditional Chinese, English, Japanese and Korean manual choices. The persisted preference is provider-neutral and does not alter Thinking enable/disable, effort, routing or an explicit final-answer language request.
- Add script/lexical Auto detection for Chinese (Simplified/Traditional), Japanese, Korean, English and several additional writing systems/languages; terse technical-only turns such as `ACP?` inherit recent user-language context instead of being mistaken for English.
- Add release-contract and regression coverage for language switching, mixed Chinese/English technical prompts, management persistence, real ACP provider-message injection and the hidden-reasoning boundary.

## 0.1.5

- **Hotfix 1:** fix the remaining live-session Type effort leak in KiroCrew. Once a BYO ACP provider was prewarmed/live, Crew treated the backend as effort-capable and its generic "supported but no advertised levels" fallback synthesized a default effort slider even though the active Tiga Type intentionally published no session-wide `effortLevel`. The Crew semantic bridge now identifies an active BYO Type before that fallback and reports effort as known-but-not-user-selectable; concrete-model sessions still use their live ACP vocabulary unchanged.
- **Hotfix 1:** reject KiroCrew REST reasoning-effort writes while a BYO Type is active, before Crew can persist a stale slot-level effort. This mirrors the existing BYO ACP protocol rejection and makes cold UI, live UI, REST state and Type routing agree. Existing 0.1.5 `chat_handlers.py` integration is recognized as an in-place upgrade rather than a foreign partial patch.
- Tiga Types now own reasoning effort internally; the Crew effort slider is shown only for fixed concrete-model sessions.
- Type routing no longer treats stale/manual effort as a candidate constraint, and upgrading clears legacy Type effort locks.
- Per-invocation effort maps only to the selected model's advertised levels; models without effort remain eligible.
- Deep-thinking capability boundaries remain independent: user On/Off is hard, Auto is Type-adaptive, fixed-on/unsupported/unknown states are never overridden.
- Crew cold-session and live ACP capability surfaces now agree on hiding effort for Types while preserving concrete-model controls.

# Kiro BYO Changelog

## 0.2.3 · Stage 3 linkage candidate (2026-10-25)

- Add a Crew-authoritative one-time ACP backend switch preview and apply workflow, with follow/keep/fixed member handling. Only `agent.acp_backend` and `agent.member_acp_backend` are written together under Crew config lock.
- Detect incompatible Agent Cron / background / Subagent model pins without rewriting them; do not infer running session, TaskRunner, or Heartbeat backends.
- Bind apply to a configuration + Cron model preview fingerprint; reject stale requests and local config overlays. Verify effective results after write, and perform compare-and-swap rollback on mismatch.
- Keep the existing manual member sync, official member precedence, ACP Stage 2 error handling, and Auto/Jev dashboard hiding logic unchanged.
- Add localized UI in five languages and cross-platform synthetic-host regression tests; real Windows/macOS/Linux Crew E2E remains unverified.


## 0.2.3 · Backend Linkage Stage 1 Candidate (2026-10-25)

- Extend Tasks with a read-only, provenance-conscious Crew backend linkage overview. Expose the main backend, shared member backend, Cron's per-task/session decision boundary, background/subagent configured-vs-resolved model, and runtime/workflow/heartbeat caveats.
- Add an explicit, confirmed one-time member backend sync using Crew's own `agent.member_acp_backend`. Verify host registry membership, stale UI state and known local overrides; do not install an automatic override of Crew's selection logic.
- Correct `role_models.background=auto` verification (raw model vs resolved model), protect BYO-only model pins when the host's selected backend is not BYO, and keep foreign existing model pins visible rather than silently switching them.
- Hide AI model controls on Script/Command Cron jobs. Translate new UI in five existing locales.
- Add dedicated linkage API tests and macOS to Linux/Windows CI process/linkage checks; no native platform E2E or insider.5 source-contract assertion is made.
- Document limitations and next-stage conditions in `BACKEND_LINKAGE_STAGE1_REPORT.md`. This same-SemVer build remains a separate candidate, not a replacement for the original Fix 03 source.

## 0.1.4

- **Hotfix 5:** fix KiroCrew's cold-session reasoning-effort surface at the actual native endpoint. Before the first ACP provider exists, Crew normally reports selection capabilities as unknown and hides its own effort selector; the Kiro BYO semantic adapter now bridges that cold interval from the authoritative BYO model catalog, while live sessions continue to use ACP-advertised `configOptions`.
- **Hotfix 5:** keep cold effort discovery provider/model/vocabulary neutral. Concrete selections expose only their exact `selectable_effort_levels`; Type selections expose the ordered union that can actually route under that Type with each level as a hard constraint. Future safe effort names are registered through Crew's native validation seam before the user can select them. Models with no adjustable effort remain explicitly known-without-effort, and unknown catalog selections fail closed.
- **Hotfix 4:** make deep-thinking capability handling future-mode-safe and fully fact-driven. Provider-declared new `reasoning.mode` names are preserved instead of being collapsed to `unknown`; unfamiliar modes still fail closed unless explicit `supported` / `can_disable` facts prove a switch. A declared `toggle` mode itself proves switchability without fabricating effort levels.
- **Hotfix 4:** fix the Models UI distinction between `fixed on` and `supported but switchability unknown`, and make native Cohere/Qianfan thinking bodies prefer each concrete model's advertised enable/disable wire literals over hard-coded values.
- **Hotfix 3:** decouple Crew effort visibility from provider family and thinking-mode names. Any concrete model that explicitly advertises at least two effort levels can expose the native Crew selector, including adaptive-thinking Claude and future/custom 2/4/5-level vocabularies; unknown capability remains fail-closed.
- **Hotfix 2:** stop shadowing KiroCrew's native reasoning-effort selector from injected dashboard JavaScript. The live ACP session `configOptions` are now the single authority: DeepSeek-class models that actually advertise low/high/max render Crew's native control, while binary-thinking-only models such as MiMo omit the effort option and therefore still render no fake strength selector.
- **Hotfix 2:** remove the redundant per-card “思考力度 …（在 KiroCrew 对话右下角调整）” sentence from the Models page; the compact capability row remains sufficient.
- **Hotfix 2:** make the Kiro BYO chat footer's elapsed time measure the whole visible turn from runner entry through completion, including pre-dispatch session/context/cold-start work, instead of resetting the clock just before provider streaming. Other KiroCrew backends keep their upstream timing semantics.
- **Hotfix 1:** make KiroCrew reasoning-effort visibility fail closed per concrete model instead of inheriting a backend-wide capability. `/api/models` now carries private BYO capability metadata; the injected Crew dashboard guard leaves the native effort UI untouched only for dynamic Types or concrete models with at least two explicitly selectable effort levels. Binary-thinking-only models such as MiMo keep Auto / On / Off in BYO but no longer show a fake low/high/max-style strength control in Crew. Unknown/new models also hide the effort UI until live discovery, a verified official profile, or a local override proves selectable levels.
- **Hotfix 1:** clear an explicit session effort lock when the user switches to a concrete model that cannot express that level, while preserving the same explicit effort when switching to a Type where it remains a valid routing constraint. This prevents stale `high/max` state from leaking across concrete-model capability boundaries.
- **Hotfix 1:** clarify the BYO Models page so binary thinking switches read as “no effort levels (deep thinking on/off only)” instead of the ambiguous “no adjustable strength / undeclared”, and add regression coverage for DeepSeek -> MiMo switching plus the Crew dashboard capability guard.
- Put **reasoning effort** on the KiroCrew session/config-option seam instead of the Kiro BYO management control: compatible models advertise `effortLevel` to Crew, so the official conversation-side effort UI can render low/high/max (or the provider's verified vocabulary). Kiro BYO continues to own only the per-model **deep-thinking Auto / On / Off** preference.
- Fix official DeepSeek V4.1 Flash capability enrichment for saved/discovered aliases such as `deepseek-v4.1-flash` / `DeepSeek-V4.1-Flash`; the current official API exposes low/high/max reasoning effort with high as default. Official-endpoint enrichment may also fill verified facts from the official display name when the wire/catalogue id differs, while live provider facts still win.
- Add end-to-end regression coverage for Type -> concrete model -> `config_option_update`: the update must contain `effortLevel`, must not contain the BYO deep-thinking toggle, and must preserve the provider's effort vocabulary/default. Strengthen the Crew 0.9 integration test for `ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`.
- Clarify the Models UI: deep thinking remains configurable in Kiro BYO; reasoning effort is labelled separately and points users to KiroCrew's conversation-side control.
- Keep the release/source dual-package workflow and exclude generated `*.egg-info` metadata from both archives.

## 0.1.3

- Hotfix a concrete-model prompt regression introduced during the 0.1.3 Turn Engine refactor: routed Type turns populate a `RouteDecision`, but concrete-model turns intentionally bypass routing. The request coordinator now initializes the optional routing decision explicitly so switching from a Tiga Type to a concrete model no longer raises `UnboundLocalError: decision`. Add an end-to-end regression that switches Type -> concrete model and starts a prompt.
- Refactor the live Agent loop around explicit provider-neutral Turn phases (`PREPARE / ROUTE / INVOKE / STREAM / TOOL / CONTINUE / POST_STAGE / COMPLETE`) and freeze the pre-refactor behavior with characterization tests before moving code. Provider round execution, tool-batch execution and provider interruption/steering handling now have explicit boundaries instead of sharing one monolithic control path.
- Promote typed session events to the durable backend truth. Schema-v3 session files derive transcript, sticky routing pins and provider continuation projections from events; schema-v1/v2 sessions remain loadable and migrate forward on save.
- Split Policy Pack v2 into user-facing semantics and machine execution policy. The assistant/UI can still explain Tiga Types on demand, while the Turn Engine receives only a compiled product-neutral `ExecutionIntent`; Type selection remains user-owned in KiroCrew and no model/tool setter is introduced.
- Add narrow `ModelRouter`, `ProviderInvoker`, `ToolExecutor`, `SessionProjection`, `PostStageRunner` and `ReasoningResolver` capability seams without introducing an everything-is-a-plugin framework. Add static dependency-direction regression tests so provider protocol modules, model intelligence and Turn Engine leaf contracts cannot silently grow upward/provider-brand dependencies.
- Add field-level routing metadata provenance/confidence for quality, speed, economy and tier roles. Authority is explicit local override > live provider discovery > verified curated profile > historical cache > generic model-name inference. Historical cache is demoted below curated facts; low-confidence routing hints are damped and name inference remains a last-resort prior rather than catalogue truth.
- Formalize dual-package release engineering. `scripts/build_release.py` builds a slim end-user `release.zip`, a complete audit/development `source.zip`, a wheel and `SHA256SUMS.txt`, while verifying that the runtime-bearing release/source file sets have identical content fingerprints and excluding mutable user state/caches/build artifacts.
- Promote the audited Core Refinement tree to **0.1.3** without adding Dyna/Gaia/Cosmos, new Tiga Types, a second multi-agent scheduler or a general plugin runtime.

## 0.1.2

- Make backend session state event-sourced. Durable schema v2 persists one append-only typed event log and derives provider messages on load; rollback, review replacement, steering and compaction are represented as new events instead of rewriting the durable history. Schema-v1 sessions migrate in memory and upgrade on their next save. Runtime-owned transcript mutations now commit through the same event helpers, while a compatibility sync records direct legacy mutations explicitly rather than dropping them.
- Keep the default harness path thin. Multi remains the default all-model Auto policy but automatic second-model verification is now reserved for exceptional complexity (`>=0.92`) or explicit `xhigh/max`; Sky's rare fast-pool verifier moves to `>=0.97` or explicit maximum reasoning. Ordinary Sky/Multi work therefore stays on the normal model -> tool -> model loop instead of being over-orchestrated. Power and Glitter retain their intentionally stronger verification semantics.
- Split the detached verification/review phase out of the main provider/tool loop into an explicit post-stage seam. Sky, Multi and Power now share the same normal agent loop; Glitter still adds collaboration only through its declared relay policy. This reduces control-path coupling without weakening provider continuation, tool permissions, steering, execution guards or Tiga routing.
- Add canonical session lifecycle events for session start/resume, turn start/end, model selection, tool calls/results and review stages. Runtime Trace records the nearest session event sequence for correlation while remaining a bounded diagnostic surface rather than session truth.
- Promote the audited tree to 0.1.2 while preserving KiroCrew ownership boundaries, Provider/Product/Profile, dynamic model catalogues, many-to-many model routing roles, user-owned Type selection and per-model Thinking/Effort locks.

## 0.1.1

- Run an end-to-end simplification audit before promoting the 0.1 line. Ordinary inference turns now receive no Mode/Type product prompt at all; the authoritative installed Mode/Type catalogue is injected only when the user explicitly asks about Kiro BYO/Tiga forms. This reduces prompt pollution and keeps default Multi behavior closer to a transparent provider/model router instead of making unrelated agent work reason about product UI semantics.
- Broaden current-turn tool-intent recognition for natural local inspection without returning to the old “tools are installed, therefore tools are required” bug. Requests such as checking a Windows drive, reviewing a project directory, locating cache/temp/log files, or equivalent Chinese/Japanese phrasing now gate to a tool-capable model when usable tools exist, while conceptual requests such as algorithm analysis remain tool-free.
- Persist sticky Mode/Type route pins with provider-neutral session state. Glitter can therefore keep the same eligible Strong leader across ACP session reloads; restored pins are still revalidated against the current routed candidate set and never bypass capability gates.
- Harden copied-runtime freshness for the branded 0.1.x line. A matching public version without the integration build fingerprint is now treated as stale instead of being accepted merely by version string, while historical pre-brand integration records retain their compatibility behavior.
- Promote the audited tree to 0.1.1 without changing the Provider/Product/Profile, dynamic model catalogue, many-to-many routing-pool, permission, or KiroCrew ownership boundaries established in 0.1.0.

## 0.1.0

- Make Tiga routing roles many-to-many and dynamic-catalog aware: models may advertise multiple `routing.tiers` plus quality/speed/economy hints, live provider facts win over curated fallbacks, and saved discovery/cache metadata preserves those roles. This fixes strong models such as GPT-6.1 Sol being misclassified as generic `balanced` solely because their names lack `pro/max/opus`, while allowing dual-role endpoints such as MiMo Pro UltraSpeed to participate in both strong and fast pools.
- Lock the product contract that a Tiga Type is a model-pool/execution strategy, not a fixed model assignment. Sky, Multi, Power and Glitter may each use multiple concrete models when their verification/collaboration policy calls for it, without changing Type.

- Complete the final 0.1.0 edge audit without changing the version. Provider/model defaults are repaired transactionally when catalogue refresh or provider deletion removes the selected model; Mode changes no longer erase independent concrete-model defaults; official Product/Profile identity is reconciled from the selected official endpoint rather than trusting stale cached commercial metadata; and local management mutations are serialized and committed with unique fsynced temporary files so concurrent ThreadingHTTPServer requests cannot overwrite one another.
- Harden background/runtime persistence. Metadata refresh now uses compare-before-replace semantics so a stale background discovery snapshot cannot clobber a newer explicit provider edit, while durable ACP sessions use collision-resistant hashed filenames, per-write unique temporaries and ordered in-process writes with backward-compatible reads/deletes of the legacy sanitized filename format.
- Finish the data/code boundary for fast-moving provider products: Product metadata may declare credential prefixes for future plans/regions that reuse an installed adapter, but an override cannot mint a new executable wire protocol or smuggle a different adapter through a newly declared Platform. The Type-picker group caption is also derived from each Policy Pack's declared default Type instead of hard-coding Tiga/Multi in CSS.
- Add `FINAL_AUDIT_0.1.0.md` and final regression coverage for stale defaults, concurrent management writes, background refresh races, Product/Profile reconciliation, future-plan credential routing, malicious adapter metadata, session-id filename collisions and dynamic Policy Pack captions. Final source-tree QA passes 448 tests + 42 subtests, branch coverage 70%, release metadata, compileall, JavaScript syntax, wheel build and fresh virtualenv install.
- Rework model/catalogue handling around current domestic/international platform patterns without changing 0.1.0. Model identity is now distinct from the inference invocation target (model / endpoint / deployment / inference profile); rich paginated catalogues normalize modalities, function-calling, reasoning, context/output limits and lifecycle; live provider facts beat curated fallback facts, while explicit local overrides remain final authority. Catalogue resolution is live → saved cache → profile-declared bundled fallback → manual ids, and unknown capabilities remain unknown rather than false.
- Make fast-moving products/models data-driven. `provider_products.override.json` can add plans/regions that reuse an installed protocol adapter, while `model_catalog.override.json` can describe a newly launched/private model without code. Xiaomi Token Plan now attempts live discovery before fallback, accepts future chat models without a generation allowlist, excludes obvious ASR/TTS-only products from the agent picker, and its current bundled agent fallback reflects the official V2.6 + retiring V2.5 text-agent set rather than assuming two models. Add `MODEL_CATALOG_ARCHITECTURE.md` plus regression tests for Model Studio-style pagination, plan overlays, invocation targets, catalogue cache/outages, lifecycle changes and stale-metadata precedence.
- Separate provider protocol code from commercial connection identity. `provider_products.py` introduces Platform → Product → Profile metadata while existing `preset_id` remains a 0.1.0 compatibility alias. Configured instances persist/infer `platform_id`, `product_id`, `profile_id` and `entitlement`; Xiaomi pay-as-you-go and Token Plan (CN/SGP/AMS) share the same `xiaomi-mimo-chat` adapter, and MiniMax region endpoints are profiles rather than new adapters. The management API now exposes the structured catalogue alongside legacy presets.
- Complete the follow-up provider-adapter audit: replace per-vendor registry forwarding classes with one `_ModuleAdapter`, and route ordinary JSON POST opening for OpenAI-compatible, Anthropic Messages, Mistral Chat and Cohere V2 through provider-neutral `http_sse.post_json`. Only Responses and Gemini retain protocol-local request opening because their continuation-expiry fallback needs the original HTTP status code.
- Stabilize the KiroCrew Type/model picker headings so selection re-renders no longer remove and re-add margin-bearing markers, eliminating the visible scrollbar-thumb jump. The caption now states the actual default: “形态 · 默认复合型，可手动切换”.
- Collapse the duplicated Zhipu/Moonshot/Qwen/Xiaomi native Chat Completions wire stacks onto two provider-neutral layers without changing 0.1.0. `http_sse.py` now owns stdlib HTTP/SSE interruption, error normalization and response mechanics, while `chat_completions_common.py` owns the shared OpenAI-shaped native message replay, usage, streamed reasoning and tool-call assembly. The four vendor adapters now contain only real request-policy differences (thinking/effort/sampling/tool quirks) and total 416 lines instead of 1,493; provider-specific interruption types are preserved rather than leaking `OpenAICompatibleInterrupted` across adapters.
- Refine the four Tiga Types without changing 0.1.0: Sky keeps capability-only cross-tier fallback; Multi remains all-tier task-aware Auto; Power now fails closed when no capable strong-tier model exists instead of silently downgrading; Glitter requires a strong-tier leader/final reviewer and re-routes each gathering worker from the concrete subtask across eligible fast-tier models. Same-provider affinity is only a small preference, and per-model Thinking preferences participate as routing tie-breakers without overriding user locks.
- Keep Type changes as a KiroCrew control-plane action rather than a prompt-level prohibition. Tiga defaults to Multi; Sky/Power/Glitter are manual picker choices. Runtime Mode/Type metadata explains the active policy but no longer injects “never switch”/“user-owned” command text into the model prompt; there is no model/tool path that mutates the session Type.
- Make Multi's task-difficulty signal language-resilient instead of depending primarily on a growing English/Chinese complexity-keyword list. Request shape (information density, clause fan-out, sentence/list structure, structured constraints and code) is now the main short-request signal; task vocabulary is only a weak hint. Equivalent substantive Chinese/English/Japanese/Spanish requests stay in the same complexity band while short greetings remain cheap.
- Make Glitter's `adaptive_per_call` reasoning genuinely **per subtask** for relay workers. Each worker invocation now derives a local reasoning signal from the most recent gathering tool batch, its fan-out and bounded result volume instead of inheriting the original turn's complexity. A hard overall task can therefore delegate a mechanical read with thinking off, while a later multi-file/high-volume gathering subtask can independently enable thinking/effort. Runtime Trace records the per-round reasoning complexity; explicit user Thinking/Effort locks remain authoritative.
- Freeze the 2026-10-14 continuation baseline from the last complete 0.1.0 archive (396 tests + 38 subtests passing before changes), and separate tool **availability** from current-turn tool **requirement**. Type routing no longer treats a Personal Agent merely having tools configured as proof that every turn needs tool use; neutral pre-prompt routes are tool-free, explicit external web/file/shell actions still gate to tool-capable models when usable tools exist, Runtime Trace records both dimensions, and compatibility callers may continue using `tools_expected` with its corrected requirement semantics. Add multilingual/current-tool regression coverage without changing the 0.1.0 version.
- Replace the fixed 24-round turn stopper and Glitter's fixed six-worker-round handback with an adaptive execution guard. Productive long runs may continue; repeated identical tool+argument+result work and short action cycles first receive an internal re-plan request and stop only if stagnation continues. A very high emergency tool-call backstop remains for catastrophic runaway protection. Runtime Trace records threshold, re-plan, handback and halt events.
- Add per-model deep-thinking policy controls: `Auto / On / Off` for models that can actually switch thinking. Explicit On/Off are hard user locks; Auto delegates each invocation to the selected Type. Fixed-on, fixed-off and unknown-capability models expose disabled controls rather than fabricated choices. Reasoning effort remains an independent axis and is only selectable when the concrete model exposes real effort levels.
- Stream provider-authentic reasoning/thinking into Crew's thinking lane without mirroring normal answer content. Provider continuation state remains private/model-scoped, and fallback is disabled after visible reasoning has begun so one model's thinking cannot precede another model's final answer.
- Make Auto thinking and Auto effort policy-driven per invocation. Tiga uses speed-biased Sky, task-adaptive Multi, reliability-biased Power, and per-call collaborative Glitter; explicit user settings and concrete model capability always win.

- Namespace virtual Type wire ids as `kiro-byo-type:<mode-id>:<type-id>` and reject the old unnamespaced form. Type identity is now Mode-local, so future Dyna/Gaia packs can expose different Type sets or reuse local Type ids without collision. Dashboard Type labels/order are generated from installed pack metadata instead of hardcoding Tiga's four forms.

- Under Kiro BYO, show the Type outside the picker list as "<Mode> — <Type>" (e.g. "Tiga — Multi") instead of the raw wire id: composer model chip, status bar, and the "将 … 置顶到 default 代理" pin row. Done by a small label script inside the existing BYO `index.html` guard; it only rewrites visible text matching `kiro-byo-type:*`, so the official backend is unaffected. Labels come from all installed Mode packs.
- Under Kiro BYO, a session with no pinned model no longer shows Crew's "auto": `GET /api/agents/resolved-model` now reports the active Mode's default Type (Tiga: Multi), the same Type a new BYO session actually runs. Other backends and pinned models pass through Crew's answer unchanged, so switching back to Kiro restores "auto". Implemented as a BYO-owned wrapper rebound in `dashboard/handlers/agents.py`; `tests/test_type_labels.py` covers it.
- Fix false "Integration 已安装 / 运行正常" after Install/Update while the Gateway still runs pre-install code. The `.pth` hook runs in every Python started from Crew's interpreter (the Kiro BYO ACP child, MCP servers, scripts), and each of them overwrote `activation.json` with its own PID, so the App concluded a restart had happened. Only `python -m kiro_crew gateway` now records activation (`process_role.is_crew_gateway_process`), and inside the Gateway an integration counts as active only when the activation record belongs to the serving process and that process did not perform the install. Symptom fixed: BYO selected, but the picker kept old in-memory `/api/models` code (no Sky/Multi/Power/Glitter Types) with no restart banner.
- Add `tests/test_activation_truth.py`: process-role and activation-truth regressions for the incident, plus an opt-in end-to-end test (`KIRO_BYO_E2E_CREW=1`, Crew's bundled python) that calls Crew's real `api_models` through `KiroCrewConfig` backend resolution and asserts the four `kiro-byo-type:*` rows come first with no `auto` / `auto:jev`.

- Fix KiroCrew 0.9.0-insider.6 integration against the shipped dashboard: stop matching minified `withAutoFirst` / Jev helper bodies, validate the stable `data-model-id` picker DOM contract instead, and inject a backend-conditional HTML/CSS guard. This removes the false `expected one dashboard Auto helper asset, found 0` failure while keeping official Auto/Jev untouched outside BYO.
- Fix KiroCrew BYO model-picker semantics: expose the active Mode's Type rows before concrete BYO models, and suppress Crew-native `Auto` / `Auto (Jev)` only when a `kiro-byo-type:*` list is present.
- Allow a provably Kiro BYO-owned patched Crew seam with a missing pristine backup to stop blocking upgrades; destructive uninstall remains fail-closed when a pristine baseline cannot be proven.
- Keep Mode as its own management navigation surface instead of nesting it inside Models.

- Rename the product/display brand from **Kiro BYO** to **Kiro BYO**, subtitle **ACP Harness for KiroCrew**, and unify the unreleased project's internal identity at the same time: backend/application ID `kiro-byo`, Python package `kiro_byo`, CLI `kiro-byo`, `KIRO_BYO_*` environment variables, BYO config/data locations, Crew overlay module names, and session/backend identity.
- Clarify the architecture boundary: Kiro BYO is a provider-neutral Agent Backend that sits alongside the official Kiro CLI under KiroCrew; KiroCrew chooses the Agent Backend, while Kiro BYO chooses the Provider/Model. It is not a Kiro CLI child backend and not a KAS wrapper.
- Preserve the existing semantic compatibility probe and the Crew-authoritative 9/10 capability contract; no provider/model ownership or member-backend inheritance behavior changes in this branding release.
- Add a bounded provider-neutral Runtime Inspector event stream without changing 0.1.0. `TurnTelemetryStore` remains metadata-only; the new `RuntimeTraceStore` records ACP/provider/tool lifecycle events in `metadata` mode by default, supports explicit local `full` content tracing with recursive credential redaction, and can be disabled with the `KIRO_BYO_RUNTIME_TRACE` environment variable.
- Re-audit Personal/custom-agent fit: Kiro BYO continues to consume Crew-owned JSON/Markdown agent projections (prompt, tool visibility, allowed/excluded tools, MCP and resources) on new/load while leaving hooks, saved-member lifecycle and Provider/Model ownership in their existing layers. `member_saved_agent` remains deliberately unclaimed.
- Separate **Mode / Type / Model** in the user interaction model. The Kiro BYO management page selects the active Mode (Tiga today; Dyna/Gaia reserved) and only displays its Types. KiroCrew's conversation picker receives the active Mode's Type entries (`Sky Type`, `Multi Type`, `Power Type`, `Glitter Type`) ahead of real provider models; Type choice is per-session, while concrete-model choice bypasses Mode routing. Remove the obsolete `kiro-byo-mode` virtual model sentinel.

> Historical entries below retain the **Kiro BYO** name because that was the project name when those releases and audits were written.

## 1.0.0

- Fix a false KiroCrew incompatibility on the member-backend seam without changing 1.0.0. The adapter no longer depends on the exact text/formatting of `members.select_provider_backend`; it inserts a narrow AST-validated Kiro BYO inheritance guard and leaves every non-Kiro-BYO path under Crew ownership. Compatibility is now version-advisory rather than exact-release-pinned: newer semver-like Crew builds enter full semantic preflight and, if all source contracts still validate, are accepted as `semantic compatible / unaudited version` instead of being rejected solely for a version bump. Runtime self-heal now validates the same saved overlay dependency contract before writing after a Crew update.
- Re-audit the complete 1.0.0 release tree against the current KiroCrew `v0.9.0-insider.6` commit `b4cf148` (2026-10-13). The 9/10 capability claim remains correct; `member_saved_agent` is still deliberately unclaimed. Refresh the Crew source-contract CI target from insider.5 to insider.6, isolate process-lifecycle tests from the app home, and add fail-closed release checks so mutable session/telemetry/audit state or live provider/native-tool configuration cannot be shipped accidentally.
- Correct stale historical/current wording around projected agents: ACP active-mode acknowledgement verifies the session-specific active projection but does not imply the full saved-member capability contract.
- Harden the adaptive Crew integration's fail-closed boundary: the owned Kiro BYO Harness now has an AST-validated contract for 35 executable Crew bindings across 15 unmodified source files plus 3 verified dynamic facade exports, so moved symbols cannot be masked by stale comments/strings. Remove vendor-specific DeepSeek/provider-label patch anchors in favor of Crew's native/default backend symbols.
- Fix Xiaomi MiMo Token Plan as a first-class official connection path without changing the 1.0.0 version. Token Plan `tp-`/`ttp-` credentials now use Xiaomi's subscription Base URLs (`token-plan-cn/sgp/ams.xiaomimimo.com/v1`) while retaining the native MiMo Chat adapter, reasoning/tool continuation, and official V2.6 Pro/Flash model profiles. The UI auto-selects Token Plan when a Token Plan key is pasted, and mismatched pay-as-you-go vs subscription credentials fail with an actionable message instead of an opaque upstream auth error.
- Fix the Overview 2×2 card alignment at the actual CSS specificity root cause: the generic `.ka-panel+.ka-panel{margin-top:12px}` stacking rule outranked the old per-card `margin:0`, shifting grid items 2–4. Overview cards now use a later, equally/more specific grid-child reset and fill their fixed row track. Add App-bundle/UI fingerprint diagnostics so stale installed copies can be distinguished from a development checkout and the separate site-packages ACP runtime.
- Promote the hardened Kiro BYO line to 1.0.0 while keeping KiroCrew 0.9 as the currently verified host contract; Kiro BYO and KiroCrew version numbers are independent.
- Make mid-turn steering cooperative across provider generation, permission waits and native process execution. A user steer interrupts an in-flight shell/Python/test process tree, marks remaining same-plan tool calls as skipped, injects the new user instruction and replans without cancelling the whole session.
- Add host-mediated directory-scope escalation for explicit out-of-root filesystem work. The user can allow a single call or the current session; sensitive paths remain hard-denied and grants are never persisted as global roots.
- Tune the provider-neutral Agent Harness for fast-first project reconnaissance: prefer structured filesystem tools and batched reads, avoid unsolicited exhaustive scans/tests/builds, and keep shell for genuine process execution.
- Add Xiaomi MiMo V2.6 as a first-class native provider adapter, including thinking on/off semantics, streaming/tool-call handling and provider-native `reasoning_content` continuation across Agent tool turns.
- Redesign the App information hierarchy across Overview, Providers, Models, Accounts, Diagnostics and Integration. Diagnostics now separates request health from token/cache usage, empty statistical samples render as unknown rather than fake 0 ms values, advanced provider fields are folded out of the primary connection flow, and responsive layouts waste less space.
- Refine the final 1.0.0 management UI without changing the version: Overview now uses one true 2×2 equal-track grid instead of independently stacked columns, Native Tools are summarized by count, model capabilities are summarized from live metadata, sidebar counters are compact/localized, and wrapped model fact grids retain proper row separators on narrower windows.
- Fix whole-Crew backend inheritance for KiroCrew `v0.9.0-insider.6` without changing the 1.0.0 version. When `agent.acp_backend` is `kiro-byo`, Crew member/teammate sessions now resolve to the same Kiro BYO harness instead of falling through the separate `agent.member_acp_backend` axis; switching away from Kiro BYO restores Crew's native member-backend behavior. Kiro BYO now declares Crew's existing member-dispatch/member-panel mounts and consumes their per-session flags, with no provider or model names crossing the integration boundary.
- Re-project the active Crew agent on Kiro BYO `session/load` through the same narrow shared-runtime resume seam used by KAS. Authorization grants remain non-persistent and are rebuilt from the fresh Crew projection, preventing stale member/tool grants from surviving restart.
- Add genuine KiroCrew Markdown-agent support without changing 1.0.0. Kiro BYO now joins insider.6's `ACP_BACKENDS_MARKDOWN_AGENT_SPECS` and matching user-level-only spec scope because its harness delegates discovery/parsing/prompt resolution to Crew's KAS agent resolver and consumes the resulting projection on `session/new` / `session/load`. `member_saved_agent` remains deliberately unclaimed: insider.6 defines it as loading the full saved member spec, including the member template's model pin, while Kiro BYO intentionally keeps Provider/Model selection inside the backend.
- Keep the provider boundary regression gate and KiroCrew `v0.9.0-insider.6` semantic integration audit. Unknown upstream Crew source shapes still fail closed before any integration write.
- Centralize runtime HTTP/MCP identity strings on the package version so future releases cannot silently advertise stale User-Agent/clientInfo versions.

## 0.9.19

- Fix the Provider page render regression where `customEndpoint` was referenced before definition; custom official-endpoint detection is now explicit and the preset form also tolerates asynchronous preset discovery safely.
- Add an App-local React error boundary so a future Kiro BYO view render failure is contained inside the Kiro BYO surface instead of escalating to KiroCrew's whole-App crash boundary.
- Re-audit the adaptive integration against KiroCrew `v0.9.0-insider.5` (`dbdb3e7`, 2026-10-12), surface that source-audit target in Integration status, and extend the semantic verifier to validate the current process-reclaim name derived from `ACP_BACKEND_LAUNCH`.
- Add an automated provider-boundary regression gate: the ACP Agent Harness may consume the provider-neutral facade but cannot import concrete vendor adapters or branch on vendor identity.
- Add provider wire-format roundtrip regression coverage for both normal JSON responses and streaming/SSE paths across the major native adapter families, protecting text streaming, tool-call assembly, usage accounting and provider-native continuation/reasoning state.

- Add Xiaomi MiMo as a first-class provider using the official OpenAI-shaped Chat API, preserving `reasoning_content` across tool turns, supporting the V2.6 thinking toggle and multimodal capability metadata, and filtering the mixed Xiaomi model catalogue to the current V2.6 Agent-capable Pro / Flash / Pro-Ultraspeed models.

- Upgrade verified official providers to higher-fidelity transports without changing the 0.9.19 package version: Responses-family paths for DeepSeek, OpenAI, xAI, Ark, Qianfan and Groq; Anthropic Messages; Gemini Interactions; Cohere Chat V2; Mistral Chat with ThinkChunk replay; Kimi Chat with preserved reasoning/tool state; GLM Chat with native reasoning extensions; MiniMax's recommended Messages-compatible path; provider-aware Qwen Chat plus model-scoped Responses for supported Qwen 3.5–3.8 and current Tencent TokenHub Hunyuan models. StepFun stays on its documented OpenAI Chat API rather than receiving a fabricated transport.
- Preserve provider reasoning/tool continuation data between agent steps, enable documented provider caching/state features (including Qwen Session cache, xAI/Mistral prompt-cache keys, Anthropic/MiniMax prompt caching and provider Responses state), and safely fall back to OpenAI Compatible on custom relay endpoints. Diagnostics now separate cache-read hits from cache-write/creation tokens instead of collapsing them into one counter.
- Keep deep-thinking state and reasoning effort independent per model: exact provider-specific effort vocabularies are published, unavailable Off controls are omitted, and effort controls disappear while deep thinking is explicitly Off. Add current OpenAI GPT-5.6/GPT-6, Grok 4.3-4.7, Claude 5.x and Mistral reasoning profiles without fabricating unsupported levels.
- Expand provider presets and model capability discovery while keeping provider/model features capability-driven rather than fabricated.
- Redesign the KiroCrew App UI into separate Overview, Providers, Models, Accounts, Diagnostics, and Integration surfaces, including multimodal/reasoning capability display.
- Support multiple independent instances of the same provider family (for example multiple OpenAI accounts/endpoints), each with its own credential reference, model catalog, account status, and routing id.
- Add a fail-closed Integration uninstall path that restores exact pristine KiroCrew seams, removes the Kiro BYO runtime/bootstrap, and can optionally purge provider credentials and local Kiro BYO data.
- Preserve the 0.9.19 package/version surface; this is a packaging/documentation synchronization of the current release rather than a version bump.

## 0.9.18

- Fix a Web native-tool boundary bug discovered by real KiroCrew Cron execution: a DNS/SSRF rejection during `web_fetch` preflight previously leaked `WebToolError` past `NativeToolRuntime`, escalated into ACP `-32603`, and aborted the entire Agent turn. Web policy rejections are now normalized to `NativeToolError`, producing an ordinary failed tool result so the model can choose another source instead of killing the Cron job.
- Make DNS answer selection compatible with split/multi-address resolver results without weakening IP pinning: if a lookup contains at least one genuine public address, Kiro BYO pins and connects only to a validated public candidate and ignores unused blocked siblings. A pure blocked result remains rejected.
- Preserve the narrow RFC2544 Fake-IP exception from 0.9.16. Pure Fake-IP DNS remains supported; Fake-IP mixed with blocked answers remains fail-closed because final routing is delegated to the transparent proxy/TUN rather than to a directly validated public address.
- Improve Web rejection diagnostics by reporting the hostname plus blocked destination categories (`loopback`, `private`, `link-local`, `multicast`, `unspecified`, `special-use`) instead of the ambiguous generic `local/private` message.
- Add ACP-level regression coverage proving that a blocked `web_fetch` is returned to the model as a tool failure and does not become a prompt-fatal error.

## 0.9.17

- Fix integration readiness so a stale copied ACP runtime can no longer be reported as Ready after an App-only upgrade.
- Compare the live `site-packages/kiro_byo/_bootstrap_state.json` runtime version with the current App `VERSION`; mismatches are now `No / update required`.
- Surface the live runtime/App version mismatch in the Integration UI.
- This closes the failure mode where the App showed 0.9.16 while Cron still executed 0.9.15-or-earlier WebTool code.

# Changelog

## 0.2.2 · 2026-10-18 — Overview / Task Features / Sky responsiveness

- The overview no longer treats stale durable Run records as live execution. It displays real 24-hour request-volume buckets, completion rate, latency and provider health; no synthetic points are inserted.
- Task feature vectors are stored as content-free fields in the bounded completed-turn telemetry record, instead of queried from the last 200 runtime-trace events. Clearing or trimming a request also removes its feature vector; clearing request history now clears the associated trace and adaptive evidence too.
- Sky's built-in policy gives speed greater scoring priority while keeping eligibility checks, fast-tier restrictions and exceptional-risk verification. Model speed is not guaranteed; service latency, tools and task content remain external variables.
- Per-Type recent usage shows measured median completion time, enabling Sky/Multi comparisons. The overview shows upstream-versus-tools timing to help diagnose slow scheduled tasks. No host-owned KiroCrew scheduler logic was modified.



- **Shared-cache model-count hotfix (same version):** KiroCrew 0.10 reuses the chat model catalogue in Settings, so hidden Mode Type sentinels could leave `全部模型 (N)` inflated (for example 4 MiMo models + Auto displayed as 9 after a four-Type Tiga chat response). The dashboard guard now passively observes `/api/models` responses, derives the concrete provider-model count without mutating chat data, and repairs only Settings model-count controls to `Auto + concrete models`. The conversation picker, Mode Type roster, and future Dyna/policy packs are unchanged.
- **Hotfix 3:** make Crew reasoning-effort visibility independent from the provider's thinking-mode vocabulary. Any concrete model that explicitly advertises two or more selectable effort values now exposes exactly those values, including adaptive-thinking Claude models and future/custom providers; provider name, reasoning-mode label, level count and low/high/max naming are not hard-coded. Add OpenAI, Anthropic adaptive-mode, 2/4/5-level custom-vocabulary, and contradictory-capability regression coverage.
## 0.9.16

- Fix Web Fetch/Search compatibility with transparent TUN/Fake-IP DNS environments by accepting RFC 2544 `198.18.0.0/15` only when it is returned by DNS for a hostname; literal benchmark addresses remain blocked.
- Preserve SSRF fail-closed behavior for loopback, private, link-local, CGNAT, ULA, and other special-use destinations; mixed Fake-IP plus genuinely blocked answers are still rejected.
- Prefer a genuine public DNS answer when both public and Fake-IP answers are present, while preserving single-resolution IP pinning and TLS SNI hostname validation.
- Update the native Web User-Agent to the actual release version.

## 0.9.15

- Fix backend-scoped Crew agent model pins for Kiro BYO models. When the active chat backend is `kiro-byo` but `agent.member_acp_backend` uses a different model namespace (for example KAS), Kiro BYO now detects a Kiro-BYO-only model before Crew probes the unrelated member backend entitlement catalog. The request is refused with an accurate model-scope explanation instead of the misleading “not available on your account; choose Claude/GPT…” message.
- Preserve Crew's model-pin safety contract: the patch does **not** bypass `_pin_entitlement_backend`, does not write an incompatible `agents.<name>.model`, does not silently change `member_acp_backend`, and does not claim member-dispatch capabilities. Current-session Kiro BYO model selection remains unchanged.
- Extend the Crew 0.9 semantic verifier with the model-scope guard so an older 0.9.14 `agents.py` integration is recognized as incomplete and regenerated from its proven pristine backup during Install / Update integration.
- Document an upstream UI limitation: Crew 0.9's current model picker always renders the “set as agent default” action based on current-session withholding only; Kiro BYO can safely reject an incompatible cross-namespace pin server-side, but does not patch compiled Crew frontend assets to hide that row.

## 0.9.14

- Fix KiroCrew theme integration on Windows/WebView: resolve native form-control polarity from Crew `data-mode`, so system/custom dark themes render native provider dropdowns in dark mode.
- Stop styling native `<option>` popups with app colors; the host/WebView now owns native menu rendering once `color-scheme` is correct.
- Fix overly bright sidebar metric wells by using Crew `--bg` + `--border` roles instead of the `--muted` ramp.
- Use Crew `--panel` / `--bg-hover` roles for secondary/hover surfaces.

## 0.9.13

- Fixed KiroCrew integration upgrades so a Kiro BYO-created harness file from the previous successful install is recognized by its exact runtime snapshot and can be safely replaced by the new version; genuinely foreign bytes at the same path still fail closed.
- Diagnostics are now user-manageable: the persistent telemetry ledger is bounded to the most recent 500 metadata-only turns, and the Kiro BYO App exposes a **Clear diagnostics** action.
- Reworked the App UI to consume KiroCrew theme CSS variables (`--bg`, `--card`, `--text`, `--muted`, `--border`, `--input`, `--accent`, status roles) and `useTheme()` rather than inventing independent light/dark colors. Native form controls now receive the host color scheme.
- Retains the 0.9.12 Process Model, Code Intelligence, Crew `allowedTools` projection fix, DNS-rebinding-hardened Web Fetch, and adaptive Crew 0.9 integration.

## 0.9.12-dev.2 (development)

- Fixed KiroCrew unattended/cron tool execution parity at both sides of the adapter: the Crew `KiroByoHarness` now carries the active saved agent's *safe* `allowedTools` projection, and Kiro BYO consumes that projection. Exact/glob-matched native and MCP tools emit normal tool lifecycle updates but skip `session/request_permission`, matching official Kiro semantics; unmatched tools still enter Crew's permission gate.
- The Crew-side projection is fail-closed rather than a raw copy: each no-prompt candidate must survive the live governance ceiling, Crew's own KAS `allowedTools` permission classifier, and PreToolUse-hook withholding. Broad shell/filesystem grants that would bypass Crew's deny/sensitive-path floor stay prompt-only.
- The local native sandbox, schema validation, and hard-deny policy run before any `allowedTools` grant, so auto-approval cannot widen filesystem/process safety boundaries. Authorization grants are deliberately not persisted by Kiro BYO; `session/load` requires a fresh Crew projection or falls back to prompting.
- Added regression coverage for the real failure shape (`@kirocrew-core` / `skill_search` in an unattended surface), MCP wildcard matching, native allowed-tools matching, and non-granted tools remaining gated.

- Corrected the development version spelling to strict SemVer (`0.9.12-dev.2`) so KiroCrew App installation accepts `app.json`. The release metadata checker now validates SemVer explicitly, preventing Python-only PEP 440 spellings such as `0.9.12dev1` from reaching a Drop-in ZIP.
- Added a session-owned long-running Process Model with stable `process_id`,
  bounded stdout/stderr ring buffers, stdin control, status/read/terminate/kill
  actions, process-group/tree cleanup, concurrent-process limits and bounded
  completed-process history. ACP session replacement/close/delete and server
  shutdown now close native runtimes so background processes cannot outlive the
  session that owns them.
- Added `execute_shell_process` and `control_shell_process` under the existing
  Crew-owned `shell` permission capability. `session/cancel` deliberately cancels
  the current turn without implicitly terminating persistent dev servers.
- Added a Code Intelligence adapter with `code_symbols` and optional `code_query`.
  Python symbol/overview extraction has an explicit stdlib-AST fallback;
  JavaScript/TypeScript and raw structural queries require Tree-sitter.
- Tree-sitter is an optional `code-intel` dependency set rather than a silent App
  install. `code_query` is hidden from advertised runtime tools unless the full
  Tree-sitter core + Python/JavaScript/TypeScript grammar set imports successfully.
- Updated saved-agent `read`/`shell` visibility tags so the new Harness-owned tools
  obey Crew projection and explicit `tools=[]` continues to disable all Native/MCP
  tools. Read-only native policy preserves structural code reads but removes both
  one-shot and long-running shell execution.
- Added Process Model, session-resource cleanup, Code Intelligence/fallback and
  capability-honesty tests. CI now has dedicated Windows/Linux Process Model jobs
  and a networked Tree-sitter-extra job in addition to the existing core, static
  analysis and Crew 0.9 source-contract gates.

## 0.9.11rc1 (release candidate)

- Re-audited Kiro BYO against the latest public KiroCrew 0.9 pre-release,
  `v0.9.0-insider.3` (`7cb49e9`, published 2026-10-10), and the public
  `release/0.9.0` branch. Both expose project metadata `0.9.0-rc.1` and the same
  registry/capability architecture inspected here. Public stable remains v0.8.0,
  while `main` has already advanced to 0.10.0; `main` is therefore only
  forward-looking architecture evidence, not the 0.9 runtime target.
- Updated the Crew 0.9 adaptive transformer for the release-candidate backend
  registry contract. Current 0.9 source uses `agent_sdk/backends.py` as the
  capability truth source and `register_selectable_backend()` as the selectability
  extension point. Kiro BYO now uses that dynamic contract when present and falls
  back to the legacy `BASELINE_SELECTABLE_BACKENDS` seam only for older compatible
  0.9 Insider source shapes.
- Added evidence-backed support for Crew's `HONOR_ZERO_TOOL_BAN` capability when the
  upstream capability exists. Kiro BYO satisfies it through its own saved-agent
  projection: explicit `tools=[]` hides both native and MCP tools. This is distinct
  from Crew's mirrored `AcpClient` / `SessionProjection.zero_tools` enforcement path.
- Preserved the deliberate orchestration boundary: Kiro BYO still does not claim
  Crew's full member saved-agent, member dispatch, or markdown-agent contracts merely
  to increase the legacy capability-card score. Crew owns those higher-level product
  and orchestration surfaces.
- Hardened native Web Fetch/Search against DNS-rebinding TOCTOU by resolving and
  validating a destination once per request hop, then connecting the socket directly
  to that validated `sockaddr`. HTTPS preserves the original hostname for HTTP Host,
  TLS SNI, and certificate hostname verification.
- Redirects are resolved, validated, and pinned again hop-by-hop. Mixed public/private
  DNS answers fail closed. Ambient proxy environment variables are intentionally not
  consumed by the pinned transport; a future proxy mode must use an explicitly trusted
  egress-proxy contract.
- Added `ARCHITECTURE_BOUNDARY.md` to make Crew-owned, Kiro-BYO-owned, shared-contract,
  and explicitly-out-of-scope responsibilities normative before future parity work.
- Added release engineering gates: synchronized release metadata checks, branch-aware
  coverage configuration, CI test/build/wheel-smoke jobs, and configured Ruff/mypy
  static-analysis jobs. The current local environment cannot install Ruff/mypy because
  outbound package installation is unavailable, so this release candidate does not
  claim those static-analysis jobs passed locally.
- Current local RC evidence: `140 passed, 5 subtests passed`; branch coverage 61%
  against a 60% fail-under gate; `compileall` succeeds; release metadata check passes;
  wheel build and clean-venv CLI/import smoke tests pass.
- Final `0.9.11` remains blocked on execution-level validation against the user's exact
  installed Crew 0.9 package: semantic verifier, App integration install, runtime
  status, startup self-repair, and transactional rollback. A source audit is not
  recorded as a runtime PASS.

## 0.9.10

- Expanded the native Agent Harness tool surface from 6 to 11 tools with
  `read_files`, `list_directory`, `delete_file`, `web_fetch`, and `web_search`
  while preserving the existing `read`, `glob`, `grep`, `write`, `edit`, and
  `shell` contracts.
- Added Kiro V3 visibility tags for the new read/write/web tools and legacy
  aliases (`readFiles`, `listDirectory`, `deleteFile`, `webFetch`, `webSearch`)
  without moving authorization out of KiroCrew.
- Added dependency-free native web execution with SSRF-sensitive destination
  checks, redirect re-validation, response-size/time limits, text/HTML extraction
  and a lightweight public search backend. Local/private/link-local destinations
  and embedded URL credentials fail closed.
- Added batched multi-file reads, explicit directory listing, and regular-file-only
  deletion with the same allowed-root/sensitive-path sandbox used by existing
  filesystem tools.
- Extended `native_tools.json` hard policy with `web_enabled`, web timeout, and
  web response-size limits. Per-tool trust remains Crew-owned.
- Kept the single-screen Overview compact when the native tool count grows by
  summarizing tool families instead of rendering all tool names in the stat row.
- Verification baseline: 131 source tests pass; the supplied KiroCrew
  0.9.0-insider.1 source still reports clean semantic seams, shared
  `AcpRuntime=True`, legacy `AcpClient` spawn=False, native Kiro 8/10 and
  Kiro BYO 7/10. Full transactional install and two-file startup self-repair
  (one semantic seam plus the create-only harness) are also verified.

## 0.9.9

- Refactored the KiroCrew App management surface into a persistent status/navigation sidebar plus a compact single-screen Overview. The sidebar now keeps health, Provider/model counts, recent turns, Crew/Integration state, credential backend and native-tool count visible while managing Providers, Diagnostics or Integration.
- Added a backend-owned `overview` status snapshot (`schema_version: 1`) so health/alert policy is computed once in Python instead of duplicated in the React UI. The snapshot derives `ready`, `needs_integration`, `repairable`, `needs_provider`, `degraded` and `incompatible` states from real runtime, provider, integration and telemetry facts without exposing secrets.
- Removed the remaining KiroCrew 0.8 integration overlays, semantic patchers,
  manual PowerShell overlay installers and their dedicated tests/docs.
- Restricted App integration to the Crew 0.9 `adaptive09` semantic transformer
  plus the create-only `KiroByoHarness` module; static fingerprint overlays are
  no longer a supported install path.
- Kept Crew's shared `HarnessAdapter` / `AcpRuntime` architecture authoritative
  and corrected the advertised capability result to the verifiable **7/10**.
  `member_saved_agent` remains intentionally unclaimed until Kiro BYO can satisfy
  Crew's complete saved-member activation contract.
- Split ACP dispatch and the prompt/tool runtime into smaller handlers. The main
  `_handle_prompt_request()` coordinator is reduced from 739 lines to 113 lines;
  provider/tool looping and native/MCP/deferred tool execution are isolated.
- Hardened MCP modern/legacy negotiation: `-32022 UnsupportedProtocolVersion`
  no longer causes blind legacy fallback, and modern `tools/list` cache metadata
  (`ttlMs`, `cacheScope`) is validated.
- Retained transactional integration preflight, exact rollback and startup
  semantic self-repair for known-clean Crew 0.9 seams.
- Updated Windows/App documentation so all supported installation instructions
  go through `Kiro BYO -> Integration`.
- Verification baseline: 122 source tests pass and the supplied
  KiroCrew 0.9.0-insider.1 source reports clean semantic seams, shared
  `AcpRuntime=True`, legacy `AcpClient` spawn=False, native Kiro 8/10 and
  Kiro BYO 7/10.

### 0.2.2 settings layout hotfix (2026-10-18)
- Fix horizontal scrolling in the embedded KiroCrew integration settings panel caused by non-shrinking two-column setting rows.
- Let setting columns and integration metadata shrink and wrap safely, and stack the settings navigator in narrower host windows without changing runtime/session behavior.
- Add optional Chromium viewport regression coverage for the settings panel (1555–1100px host widths).

### 0.2.2 management and schedule selector corrective patch (2026-10-18)
- Keep failure/error/account-exception labels neutral while coloring failure **values** red and success **values** green, including zero. Apply the rule to the legacy standalone management view as well.
- Fix Overview trend's data source: use full status/diagnostics hourly summaries (with ops summary as a fallback), not a partial Overview diagnostic projection; only chart actual recorded hourly latency observations.
- Stop scheduled-task Model and Context selectors overflowing their cards with shrinkable grid columns, constrained triggers and a container-width responsive fallback.
- Harden the KiroCrew schedule-edit Type guard for unmarked React portals, plain-text Type rows and model menus mounted *after* the editor; the chat Type picker remains available outside the editor. No shared model catalogue mutations, compiled bundle patches or page reloads.
- Add regression coverage including an optional real Chromium test for asynchronous task editor/model-list ordering.

### 0.2.3 — S1-03 Crew capability sync (2026-10-23, same product version)
- Added per-session live MCP broker verification instead of treating persisted Crew tool schemas as active grants.
- Added bounded host catalogue refresh and non-secret capability snapshots at prompt boundaries.
- Rejects unverified/revoked MCP tools and prunes stale deferred tool selections.
- On explicit browser requests, waits for the actual Crew core session handshake rather than cached catalogue readiness.
- Explicitly withdrawn Saved Agent projections are not resurrected from durable session history.
- Kept Tiga/Dyna semantics, App ID, Crew integration patches, and product version unchanged.

### 0.2.3 — S2-05 Crew background role handoff (2026-10-24, same product version)
- Normalize KiroCrew `kirocrew-lite` background `auto`/Type selections to a concrete model already at `session/new`, matching the existing subsequent `session/set_model` semantics.
- Preserve host-selected concrete models and interactive Tiga Type routing; only the explicit Crew lite-agent projection activates this behavior.
- Normalize legacy persisted lite-worker Type sessions on restore while preserving persisted concrete choices.
- Add regression coverage for startup, restore and host precedence without changing KiroCrew integration patches or claiming full schedule/member parity.

### 2026-10-24 · 0.2.3 S3-03 故障注入与恢复状态机加固

- Durable Run 恢复在 Ledger 原子更新阶段二次判定，避免同进程重复准入和进行中恢复重放。
- Step/Attempt 持久化创建失败时停止本轮 Provider 与工具执行，明确返回运行错误。
- 新增 S3-03 写入中断、并发恢复、副作用阻断等故障注入测试；不改变 Tiga/Dyna、Crew 数据或界面。
- 实机故障恢复、跨进程 exactly-once 及远程副作用撤销不在本轮验证范围内。

### 2026-10-24 · 0.2.3 S4-01 Mode Registry 兼容层
- 新增 data-only Mode Registry，将磁盘 Policy Pack 发现与模式激活/解析分离。
- 旧 API、Tiga 四 Type 的动态选模与会话标识保持兼容；Dyna 三 Type 仍仅预览、不可执行。
- 保留用户 Policy Pack 热加载，不开放第三方任意 Python 代码执行。
- 新增注册表回归测试；Crew Windows 端到端验收另行进行。

### 2026-10-24 · 0.2.3 S4-03 Tiga 内置 Mode 插件迁移

- Added explicit built-in Mode plug-in declarations for Tiga and preview-only Dyna.
- Validate built-in Mode/Type IDs, default Type and activation state before registry admission; incompatible resources fail closed.
- Kept Tiga/Dyna policy JSON byte-identical to S4-02 and preserved Mode → Type → Model, Crew selection IDs and legacy Policy Pack API.
- Retained JSON-only custom Mode hot reload and legacy manual Tiga override compatibility; Dyna remains unavailable for execution.
- Did not change Provider, ACP, tool permissions, Crew integration patch, UI, or product version.

### 2026-10-24 · 0.2.3 Crew 0.9 退役 / Crew 0.10-only 兼容清理

- 删除 `v0.9-adaptive` 适配目录和 `verify_crew09_integration.py`，最低 Crew 要求提升到 `0.10.0`。
- `resolve_target()` 显式限定 Crew 0.10 家族，移除 `adaptive09` 分发分支；启动修复拒绝 0.9 旧快照。
- 0.10 仍使用的通用语义变换函数更名为 `crew_shared_patch.py`，保留原有 0.10 行为。
- CI 上游源码验证改为 0.10；新增旧版拒绝测试，当前发行说明同步更新。
- 不自动卸载既有 Crew 0.9 补丁；应在原版本中先卸载，然后升级 Crew 再安装新版。Windows 实机验收仍待完成。

### 2026-10-24 · 0.2.3 Global Audit Fix 03 · Crew 队友兼容修复
- 恢复 Crew 原生的队友私聊后端选择优先级；`agent.acp_backend=kiro-byo` 不再覆盖 `agent.member_acp_backend`。
- 旧 BYO-owned `members.py` selector 及队友模型 entitlements helper 可原位升级，不对未知 Crew 源码强制改写。
- 单独识别有会话身份的 Crew member dispatch/panel MCP：缺省/空 Saved Agent `tools` 不再意外屏蔽 Crew 的会话级控制面，同时坚持 excludedTools、真实连接、权限请求和派发前复核。
- 同名 MCP 服务声明拒绝；Crew member 服务缓存不再被当作真实连接验证；无完整 Agent 投影但有 member mount 时默认收紧原生及外部 MCP 可见性。
- 不增加完整 Saved Agent 能力声明、不改 Tiga/Dyna/Provider 策略和 Crew 数据库；Windows 真实集成验收仍待完成。

### 0.3.0 V12 client-latency observer candidate (2026-10-31)
- Add a separate conservative client-observed send-to-reply timing readout for the Crew 0.11 chat; do not overwrite server Turn or Provider telemetry.
- Fail closed when a live turn cannot be uniquely associated with the visible chat, and preserve existing host install/change isolation.
- Keep Windows/macOS/Linux Electron UI verification pending; this is not a claim of pixel-perfect end-to-end timing.
