# Kiro BYO 0.3.0 — 后端与 Mode/Type 双向同步修复（候选）

日期：2026-10-31。目标：KiroCrew 0.11.0-rc.5 / v0.11.0 源码合同；Windows 实机仍待验证。

## 触发与根因

用户实机发现两类问题：切换 Dyna/Tiga 后右下角 Type 不变；在 KiroCrew 官方与 BYO 后端切换时同样残留旧后端、旧会话模型。

1. 聊天状态协调误把**既存 ACP Provider**报告的旧后端当作目标后端，而不是取 `_configured_backend_for_slot(slot)` 返回的当前 Crew 配置。这会在双向后端切换时跳过应当执行的安全重置。
2. 官方 `/api/models` 不进入 `_kiro_byo_models`，过去的协调入口只挂在 BYO 模型列表分支，切回官方后不会触发协调。
3. 前端 Mode 守望器仅识别 `kiro-byo-type:*`，不识别官方后端“没有 BYO Type”的有效模型目录；在页面刚加载、选择器未打开时也可能遗漏旧会话 composer chip。
4. BYO App 的 BroadcastChannel 不保证与宿主 React 同一传输域；访问 `/api/models` 的单独 fetch 不会刷新 React Query。该版本仍使用仅在无进行中任务、无未发送草稿时允许的单次页面刷新作为兼容性兜底，不直接操纵 React 的选模状态。

## 最小更改

- `src/kiro_byo/crew10_patch.py`：取 Crew 重新解析的目标后端，并将已有 warm Provider 仅视为旧运行时事实。针对 Type/Auto 路由哨兵，在可证实空闲且获取原生锁后，先执行原生 ACP 会话安全重置，再提交新 Mode Type 或清除不属于官方后端的 BYO Type。正在运行、关联子代理、正在审批、重置失败等情况保持原状态。
- `src/kiro_byo/crew_patch_common.py`：在 Host 的 `api_models()` 入口中统一触发同一个会话协调器；覆盖官方与 BYO 两个模型目录分支。可幂等升级旧补丁，不改 Kiro 原有返回结构。源 AST 校验缺失的宿主入口时拒绝猜测。
- `src/kiro_byo/crew11_model_labels.py`：为模型守望器引入 `official` 与 `byo:<mode>` 两类状态。官方分支需由 `GET /api/config/kirocrew` 验证当前选中后端，防止把 BYO 模型获取失败误判为官方。保留忙碌/草稿/模型目录不完整/请求失败时的失败关闭、单次重载防循环（直到视图与后台状态收敛才重新允许对同一目标重载）和只读 UI 检测。

## 已运行的验证

- 20 项官方 ↔ Dyna/Tiga 的浏览器 JS 注入模拟通过，覆盖空闲、繁忙、草稿、槽位 API 失败、配置 API 失败。
- 8 项（4 种切换 × 忙碌与空闲）warm ACP Session 的原生重置行为模拟通过；另 1 项模型 API 宿主补丁编译、幂等与顺序检查通过。
- 较早定向测试 55 项通过。全项目常规分组回归：1,273 项通过、1 项跳过、58 个 subtest 通过；另有独立浏览器几何测试在隔离环境运行超时，未计为通过，单进程整套未完成。
- `scripts/architecture_guard.py --check` 通过（超过 100 行的函数为 54，预算 54）。`scripts/release_check.py` 通过。

## 未覆盖与重要边界

- **未在用户 Windows KiroCrew 0.11.0-rc.5 实机验证**，不能声称 UI 与实际 ACP 运行已经彻底同步；安装需通过 BYO 的 Install/Update Integration 并完全重启 KiroCrew 以加载 Python 源码补丁。
- Browser 兜底是重载主界面，而非调用 React Query 内部 API；需要 Windows 实机确认 Host CSP、脚本注入和安装构建指纹确实激活。
- 为保留具体模型直连/持久化选择的安全边界，不擅自替换用户显式选定的具体模型。跨后端时如果当前显式模型不被新后端支持，仍需由 Crew 宿主模型选择校验处理，不将此误记为已修复。
- 正在运行的会话不会被强制切换；只有其空闲且原生重置成功时才同步 Type。不能仅以界面标签声称执行后端已切换。
- 浏览器页面与模型目录不一致时，如遇连续刷新失败，应检查当前运行中的 Gateway 是否确实加载本构建，并查看 `/api/models`、只读取 `agent.acp_backend` 的 `/api/config/kirocrew`（不要分享完整配置）、BYO `host_sync` 诊断。

## 手动验收建议

在同一**已完成回答**的普通聊天中，依次测试 Tiga Multi → Dyna Flash → Tiga Multi；然后官方默认 → BYO 默认 → 官方默认。每次确认宿主菜单、会话右下角、下一条真实消息所用后端及执行 Trace 一致。分别测试运行中切换和具体模型直连是否被保护。旧回答的历史标签应保持原样。
