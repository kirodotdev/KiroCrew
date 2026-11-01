# Kiro BYO 0.3.0 双向 Mode 切换修复报告（2026-10-31）

## 用户报告与根因

用户在 Dyna Flash 中与旧会话聊天后，将 BYO 管理页面切回 Tiga，但聊天页右下角仍显示 Dyna Flash。

此前 `_KIRO_BYO_SLOT_RECONCILE_HELPER` 把 `state.sessions.get_provider(session_key) is not None` 误用为「会话正在执行」的判据；Crew 会在回合已完成后仍保留 ACP Provider，故 Mode 跨界面迁移仅对尚未创建 Provider 的 cold slot 生效。单纯改写 `slot.model` 又会让 UI 与实际 ACP 执行上下文不一致。

## 代码修改

- `src/kiro_byo/crew10_patch.py`：将 live-provider 是否存在与真实 busy 条件分离。仅在旧 Type 的 Mode 与 BYO 当前 Mode 不同、会话空闲且非链接/他方 App 时，使用 Crew 原生 `_reset_slot_session_or_warn` 安全结束旧 ACP 会话，之后提交当前 Mode 的默认 Type。
- 服从 Crew 原生锁顺序：`slot._lock` → `_slot_switch_session_lock(session_key)` → `slot._model_pick_lock`；前后核验 session key、slot 身份、provider 身份及选模快照，并使用宿主原生 `_switch_target_busy`、`_subagents_attached_response`。
- 调用原生 reset 的前后按宿主 pick 事务逻辑提交或回滚 model、jev_route、model_pick_gen，避免只修改显示而不改变真实运行。成功后发送 slot 状态变更及上下文重置信号。失败时保留原选择，不强制关闭正在执行的任务。
- 不更改 `backend/routes.py` 已有 Mode PUT → Host 协调 → App `BroadcastChannel` 通知的链路，也不修改 Tiga/Dyna Policy Pack 和内部 ID。
- `tests/test_crew10_adapter.py`：新增已完成回合但仍保留 Provider 的双向切换回归，包括保护运行中/子代理/手动模型/他方 App、拒绝重置回滚、宿主契约不可用、并发用户固定模型选项等。

## 测试证据

Linux Python 3.13.5，测试只访问隔离源副本、不连接实际 Provider 或 KiroCrew。运行命令：

```bash
PYTHONPATH=src:. pytest -q tests/test_crew10_adapter.py tests/test_023_parity_guards.py tests/test_030_dyna_release.py tests/test_crew11_model_labels.py
python scripts/architecture_guard.py --check
python scripts/release_check.py
node --check ui/index.mjs
```

定向测试：46 项通过；架构门禁 54/54 通过；发布元数据与 UI JS 检查通过。全套测试在当前环境的 Playwright/Chromium 浏览器用例附近超时，没有假报通过。排除该浏览器文件后，分组测试通过 1249 项，跳过 1 项，58 个 subtest 通过；另外 13 个 Playwright 用例未完成、未验证。详细命令和日志保存在测试证据包内。

## Windows 必须验收

1. 先备份 BYO App 与 Crew 宿主，按既有安全安装流程重装集成补丁并完全重启 Crew。确认 BUILD_INFO 已切换为新 build_id，否则旧注入脚本仍可能在运行。
2. **同一个已回答的旧会话**：Dyna Flash → Tiga Multi → Dyna Flash；新会话也应采用当前默认 Type。不要把历史消息上的 Dyna Type 标签当作当前选择；历史记录应保留原执行标记。
3. 已回答旧会话选择 Dyna Strong 后切换 Tiga，应该成为 Tiga Multi；切回 Dyna 默认 Flash。当前 Mode 手动 Type 不受重复刷新覆盖，具体模型直连保持不变。
4. 工作执行中切换 Mode 不得中断旧回合；之后可重新进入会话使空闲迁移发生。若宿主返回 host_unavailable，须检查安装集成状态和模型菜单缓存。
5. Windows 0.11.0-rc.5 / 0.11.0 正式版兼容仍不宣称 E2E 通过，未来 0.12 必须重新源代码契约检查。

**状态：修复候选，不是正式稳定版。**
