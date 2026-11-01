# Kiro BYO 0.2.5 · Crew 0.11 双向模型联动修复

## 目标

当 Crew 主代理后端为 `kiro-byo` 时，Kiro BYO「任务」页面直接读取和写入 Crew 的原生配置，不维护第二份模型镜像：

- 普通聊天：`agent.model`
- Crew 队友：`agents.<name>.model`
- Mate Worker：`agents.<name>.worker_model`
- 计划日程：`CronJob.model`
- AI 后台：`agent.role_models.background`

Crew 原生界面修改后，BYO 下次读取同一个配置源即可看到；BYO 修改时直接写 Crew 配置/服务，因此 Crew 原生页面读取到相同值。主 Harness 不是 BYO 时，BYO 模型写入接口拒绝操作。

## 关键修复

1. 任务 API 返回 BYO 当前 Mode 的 Type 选择和具体模型，允许 Crew 的原生 string model 字段保存 `kiro-byo-type:<mode>:<type>`。
2. 新增普通聊天模型写入接口，直接修改 Crew `agent.model`。
3. 新增按队友写入自身模型与 Worker 模型的接口，保留空值=继承、Worker `auto`=后端默认的 Crew 原生语义。
4. 日程与 background role 不再错误限制为“只能具体模型”；主后端为 BYO 时可选择 Tiga Type 或具体模型。
5. BYO 任务页新增普通聊天与队友模型视图；队友自身模型和 Worker 模型分别显示/修改。
6. 新建/编辑队友的 Crew 0.11 友好模型名称补丁继续保留，不改变 wire model id。
7. 记录页将 `background_auto` 分类显示为“后台 · Auto”，不再把 Crew 标题/状态/摘要等后台请求误写为“具体模型”。如果后台请求显式带 Type，则显示后台 Type。
8. 现有队友后端联动保持：未显式固定 member backend 时随 BYO 主后端；显式覆盖优先；切回官方后未固定队友恢复 Crew 默认。

## “具体模型”现象的结论

此前截图中 `具体模型 → mimo-v2.6-flash` 且来源为 `background_auto` 的请求，不是主聊天从 Tiga Type 退化。Crew 的 lightweight background agent (`kirocrew-lite`) 会把 background `auto` 或 Type 在会话边界物化为一个具体模型，以避免短小后台任务进入完整多模型编排。修复的是记录页的分类文案，而不是强行改变这一执行语义。

## 验证

- 8 批完整 Python 回归：970 passed, 1 skipped, 52 subtests passed；全部批次正常退出。
- 新增双向配置测试：普通聊天、Mate 自身、Worker、Cron、background Type 写入与非 BYO 主后端拒绝。
- Python compileall / JavaScript syntax / release metadata 检查通过。
- 用户提供的 KiroCrew 0.11.0-rc.1 源码 ZIP 精确契约：41 个 Python 源文件匹配，21 个生成 seam，0 issue。
- Windows 桌面端实际 E2E 仍需用户安装候选包后确认。
