# Kiro BYO 0.2.5 第一阶段修复与未完成项目

日期：2026-10-28  
基线：用户上传的 Kiro BYO 0.2.5，包含上一轮 BYO 模型目录隔离修复（`0.2.5+5334c549a2b139aa`）。  
目标：KiroCrew `v0.11.0-rc.1`，产品版本保持 `0.2.5`。

## 已处理

1. `backend/routes.py`：`_require_byo_in_locked_document` 在 `update_config_locked(..., mutate=...)` 的 **Crew 配置锁内**验证 `agent.acp_backend == kiro-byo`，并拒绝已存在的 `config.local.json` 主后端覆盖。聊天、队友/Worker 和后台三个模型入口均执行此校验。原先的前置模型白名单、写后 Crew 原生配置验证仍在。
2. `backend/routes.py`：`_selection_rows` 只有存在至少一个 BYO `public_models` 时才展示 Mode Type，防止空 Provider 列表时任务页提供无法执行的 Type。写入校验复用 `_selection_ids` 因而同步生效。
3. `backend/routes.py`：增加 `_CRON_APP_WRITE_LOCK`，在一个 Gateway 事件循环中串行处理 BYO 模型及 Cron 执行上下文写入。由 BYO 发起的相同 `expected_model` 双重提交会由第二个请求检测并拒绝。
4. 无法读取 `VERSION` 时返回 `unknown` 而不是伪造 `0.9.13`；更新 Crew 0.11 RC1 Worker Kit 的过期说明。

## 关键剩余风险（不可误报为修复完成）

- **Crew 与 BYO 跨界面 Cron CAS 仍未解决**：Crew 原生 Cron 编辑不使用 BYO 的 `_CRON_APP_WRITE_LOCK`。当 Crew 在 BYO 的 `list_jobs_async` 与 `update_job_async` 之间写入时，BYO 仍存在覆盖窗口。真正的原子防冲突必须由同一 Cron 存储锁/版本条件写入支持。当前发布包保持现有可用能力，但不作“已彻底防止并发覆盖”的声明。
- `config.local.json` 自身被另一个进程并发修改、以及未公开的更高优先级环境覆盖，仍可能发生文件级竞态。拒绝已存在的本地覆盖并使用 Crew 配置锁已消除主要的基准配置 TOCTOU，但不等于跨所有配置层全局原子。
- Mate/Worker 全链路运行、Windows RC1 实机界面及 Provider 请求/官方用量尚未验收。
- `turn_runner.py`、`acp_server.py` 和安装器的大函数拆分列为下一阶段；未在本轮大改，以保持运行行为稳定。

## 回归验证

- `python -m pytest -q --disable-warnings --maxfail=1`：**1006 passed, 1 skipped, 52 subtests passed**。
- 新的四组独立测试覆盖锁内模式变更、主后端本地覆盖、空 Provider、BYO 同 Gateway Cron 并发提交。
- `python -m compileall -q src backend scripts`：通过。
- `python scripts/release_check.py`：发布打包时重新运行。
- 本地修改及测试均未连接或更改真实 Windows KiroCrew 用户配置。

## 推荐下一阶段

优先获取 KiroCrew RC1 原生 Cron 事务实现并支持跨 UI 的原子 `expected_model` 写入，然后在 Windows 完成普通聊天、Crew 队友/Worker、Cron、后台的真实后端/模型目录验收；最后再按模块做小步重构。
