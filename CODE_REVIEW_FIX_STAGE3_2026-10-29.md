# Kiro BYO 0.2.5 — 第三阶段修复报告

日期：2026-10-29
基线：第二阶段 `0.2.5+1c76bfe1b7fba21d`
目标 KiroCrew：`v0.11.0-rc.1`

## 本轮实际修改

1. `backend/routes.py`：新增 `_native_cron_job`，在日程模型和上下文更新前后读取 Crew 原生 Cron 任务。写入后检查**原生列表持久化状态**，不再仅依赖 `update_job_async` 返回对象。对任务丢失或值不一致发出明确错误。
2. 同文件：新增 `_verify_byo_after_cron_write`。在异步写入后再次检查 Crew 主后端。如果期间切到官方，则返回可能已提交的冲突诊断，不执行无条件回滚，防止覆盖别人的编辑。
3. 同文件：执行上下文更新在服务端拒绝 Script/Command 类型，与前端一致。
4. `tests/test_cron_postcondition_stage3.py`：6 项新增异常路径测试，包括伪成功返回、任务消失、后端在写入中切走和非 Agent 任务拒绝；`tests/test_crew_linkage.py` 原有 Type 日程测试模拟真实持久化而非只模拟返回对象。

## 不得破坏的 Mode / Type 约束

对照第二阶段，以下 11 个关键文件 SHA256 内容一致：`protocol.py`、`mode_router.py`、`acp_server.py`、`mode_registry.py`、`mode_lifecycle.py`、`provider_admin.py`、`crew11_patch.py`、`crew_dashboard_patch.py`、`policy_packs/tiga.json`、Crew RC1 BYO harness overlay 和 `ui/index.mjs`。

本轮未变更 Tiga Sky / Multi / Power / Glitter、默认 Multi、`kiro-byo-type:<mode>:<type>` 命名空间、具体 Provider 模型直连绕过 Type 策略、Dyna/Gaia 状态。相关专项测试 `99 passed`。

## 运行结果

- Cron、Crew 联动、Mate 关键回归：`53 passed`。
- Mode/Type/Crew RC1 适配专项：`99 passed`。
- 项目全部 117 个 `test_*.py` 文件按不重叠的 4 组运行：**269 + 245 + 218 + 285 = 1017 passed，1 skipped，52 subtests passed**。
- Python `compileall`、JS `node --check`、`scripts/release_check.py`：均通过。
- 未做用户 Windows KiroCrew RC1 实机测试、完整执行路径计费核查或真实 Provider 接口测试。未对用户 GitHub 做任何修改。

## 重要局限

**尚无 Crew 原生 Cron 与 BYO 任务页的跨界面原子条件写入（CAS）。** `_CRON_APP_WRITE_LOCK` 仅用于 BYO Gateway 内部串行请求。写入后重新读取能发现未保存/被立即覆盖、切后端也能检测，但无法从根本上阻止 Crew 原生 UI 在 `list_jobs_async` 与 `update_job_async` 之间发生的并发覆盖。真正的原子 CAS 需要上游 Crew Cron 存储事务能力或经过源码审计的双方共同写锁，不能在第三方 App 内伪造。

在后端切换与 Cron 修改恰好并发时，BYO 现在会返回明确提示：**写入可能已经发生**。用户需要刷新 Crew 原生任务再决定是否改回，不能把失败状态理解为保证未写入。App 不做危险回滚。

## 本地覆盖

备份旧目录，在本地 Git 工作树上解压完整源码 ZIP 并合并覆盖，保留 `.git`。安装 ZIP 仅供 BYO App 安装/更新，不是 Crew 官方安装包。产品 SemVer 保留 `0.2.5`，用 `BUILD_INFO.json` 的 build_id 区分此次构建。
