# Kiro BYO 0.2.5 — 第二阶段：Cron 上下文防过期提交与 Mode/Type 完整性检查

日期：2026-10-28
基线：第一阶段修复版 `0.2.5+b2c76705e48e338b`
目标：KiroCrew `v0.11.0-rc.1`

## 本轮实现

1. `backend/routes.py` 的 `update_task_execution` 支持两个可选布尔值：
   - `expected_persistent_session`
   - `expected_minimal_context`

   在 BYO 现有 `_CRON_APP_WRITE_LOCK` 内，先从 Crew 原生 Cron 服务读取当前任务。只有两个前置状态仍然匹配，才调用 `update_job_async`。字段类型错误、任务丢失、状态变化均会拒绝提交。写入后检查返回的两个布尔字段是否与目标一致，避免把不一致结果标为成功。

2. `ui/index.mjs` 的 BYO 任务控制台在提交“持久会话/精简上下文”变化时，附带当前显示的两个期望值；出错时使用现有刷新和错误提示，不额外镜像 Crew 配置。

3. `tests/test_task_execution_context_cas.py` 增加 5 项测试，覆盖 Crew 原生编辑后数据过期、正确值写入、非法 expected 类型、BYO 同 Gateway 并发和官方后端拒绝写入。

## Mode / Type / Provider 的保护边界

本轮未修改 `src/kiro_byo/protocol.py`、`mode_router.py`、`acp_server.py`、`mode_registry.py`、`mode_lifecycle.py`、`provider_admin.py`、`crew11_patch.py`、`crew_dashboard_patch.py`、`policy_packs/tiga.json` 等形态和模型路由关键文件。与第一阶段的这些文件逐字节相同。

不改变 Tiga 的 Sky / Multi / Power / Glitter 形态、默认 Multi、`kiro-byo-type:<mode>:<type>` 标识、具体 Provider 模型选择绕过 Type 策略的行为；不启用尚未开放的 Dyna/Gaia。

## 未解决的边界（重要）

**这不是跨 Crew 原生页面的 Cron 原子 CAS。** 官方 RC1 公共 Cron 更新 API 并未确认提供带 `expected` 的原子条件写入。BYO 自己的锁无法约束 Crew 原生日程编辑器/其他进程在 BYO 读取后写入。新增的预检查可以拒绝已经发生的冲突，但对“检查和写入之间”的跨 UI 并发仍有 TOCTOU 窗口。要从根上解决，需核对并扩展 Crew 所有写入路径共用的事务能力，而不能假装多读一次等价于原子更新。

安装器、Mate/Worker 执行与 Windows RC1 UI 未在本轮进行实机测试；未改这些系统。

## 验证

- 新增 + Mode / Type + Crew 联动重要用例：`60 passed`。
- 模型、任务相关组合：`65 passed, 11 subtests passed`。
- 全部测试文件按不重叠的四组运行：`378 + 235 + 134 + 264 = 1011 passed`，`1 skipped`，`6 + 16 + 9 + 21 = 52 subtests passed`。
- 一次性运行的全套 pytest 在约 75%~86% 期间出现停滞而超时，因此**不标记为一次性全套通过**；四组覆盖全部测试文件且均无失败。
- `python -m compileall -q src backend scripts`、`node --check ui/index.mjs`、`python scripts/release_check.py` 通过。
- 源码和安装 ZIP 完整性验证通过；无 `.git`、`.pyc`、`__pycache__`。

## 升级提示

保留 `0.2.5` 的产品版本号，用 `BUILD_INFO.json` 里的不同 `build_id` 区分构建。先备份本地项目和 KiroCrew 配置；完整源码 ZIP 可以合并覆盖本地 Git 工作树，保留 `.git`。安装 ZIP 是 KiroCrew App 安装包，不是 Crew 官方安装程序。
