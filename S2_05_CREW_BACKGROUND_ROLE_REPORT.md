# S2-05｜Crew 后台角色模型交接与会话恢复

- 产品显示版本：`0.2.3`（独立 Build ID）
- 唯一源码基线：`Kiro_BYO_0.2.3_S2-04_子Agent生命周期_源码包.zip`
- Crew 对照目标：`v0.10.0-insider.4`（`e9bfada`）
- 验证性质：源码审计、模拟 Crew ACP 投影、自动化回归；Windows Crew 实际端到端尚未验证
- 构建标识：`0.2.3+1d7ef57a3f714109`

## 本轮确认的缺陷

`kirocrew-lite` 的轻量后台任务本应直接使用一个实际执行模型，不能暴露 Tiga 的复合 Type。此前 `session/set_model("auto")` 有专门处理，但 `session/new` 在收到 Crew `_meta.kiro.modelId = "auto"` 时没有这一处理：它会保留 BYO 默认 Type。由此产生“会话开始的模型语义”和“之后切换 Auto 的模型语义”不一致。老的 Session 记录还可能持久化这类后台 Type。

这项缺陷仅在宿主明确投影 `kirocrew-lite` Saved Agent 时成立。不能单凭 `_bg`、`cron:`、`taskrunner:` 等 session key 推断权限或后台角色，更不能声称所有 Crew 调度角色都已完整映射。

## 本轮改动

1. `src/kiro_byo/acp_server.py`：先解析并验证宿主投影的 Saved Agent，再决定新会话初始模型；如果身份明确为 `kirocrew-lite` 且所选模型是 `auto` 或有效 Tiga Type，就解析为一个实际模型后再发布 ACP Session。
2. 显式选择的**具体模型**保持宿主权威，不被 BYO 的背景默认策略覆盖。
3. 普通对话、队友和其他无 `kirocrew-lite` 身份的 Session 继续保留原先的 Mode → Type → Model；不因为 session key 中含有 `bg`、`cron` 或 `task` 就关闭 Tiga。
4. 恢复历史中意外保存 Tiga Type 的 `kirocrew-lite` 会话时，迁移为当前有效的具体模型；已保存的具体模型不做重新路由。
5. 背景模型决策使用该 Session 明确选定的 Type 策略，而不是无条件退回全局默认 Type。
6. 新增 `tests/test_s2_05_crew_background_lifecycle.py`，覆盖上述初始化、宿主具体模型优先级、普通聊天与恢复场景。

## 安全和兼容边界

- 本次只修 BYO ACP 层的轻量背景**模型交接**，不修改 Crew 的日程数据库、调度器、队友数据、集成补丁模板或 UI。
- 没有新建持久化任务队列，也不对已经发出的 MCP 副作用做无法保证的回滚。
- 不改变 Tiga 四形态各自的路由评分及 Dyna 三形态的禁用状态；不接入 Jev 或额外的决策模型。
- 未增加任何 Saved Agent 能力宣称：`member_saved_agent` 完整能力仍须逐项证明。
- Crew 各类 `cron`、TaskRunner、后台队友和工作流的停止、失败重试、恢复、模型角色继承必须在真实 Windows 安装上分别验证。`kirocrew-lite` 不代表这些任务全部。

## 验证结果

- 定向测试：`27 passed`（包括 5 条新增生命周期测试及 Type 语义回归）。
- 全量 Python：`766 passed, 1 skipped, 52 subtests passed`。
- `Windows_e2e = not_verified`；发布包 `BUILD_INFO.json` 用独立指纹区分同为 `0.2.3` 的构建。

## 下一步

剩余 S2 原生功能对等仍未验收。先在真实 Crew `v0.10.0-insider.4` 中验证：`_bg` 轻量任务、定时任务、TaskRunner、队友后台会话、错误重试、权限弹窗期间取消与会话恢复。如果定位出更具体的宿主差异，修订适配器再进行桌面认证；之后进入 S3 的 Durable Run、事件回放与故障恢复。

回滚：使用 S2-04 已验证安装包和源码包恢复 BYO 代码；保留 Crew 用户会话与定时任务数据，不直接清库。
