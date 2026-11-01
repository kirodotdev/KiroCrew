# Kiro BYO v0.2.3 · Global Audit Fix 02 / KiroCrew insider.5 发布跟踪

日期：2026-10-24。基线：Global Audit Fix 01 (`0.2.3+a6029fae7b3638c6`)；主要已审计 Crew 基线：`v0.10.0-insider.4` / `e9bfada`。

## 1. KiroCrew 新版本核对

GitHub 官方 `v0.10.0-insider.5` 预发行标签对应 `7b5d9b3`。公开 release notes 列出了 PR #23211（为 0.10.0 stable cut 去除 `rc` 版本后缀）与 PR #23505（`dompurify` 升级至 3.4.17）。其中 PR #23211 明确表示该 PR 没有更改代码/工作流，仅更改三个产品版本字符串。**这并不能代替对 .4 → .5 整个标签差异的逐文件校验**。

本地执行环境不能下载 GitHub 源码，因此本次没有声称已对 `.5` 实际运行语义适配器。`integration/kiro_crew/v0.10-adaptive/compatibility.json` 的 `verified_through` 继续锁定 `.4`，`BUILD_INFO.json` 将 `.5` 记录为 candidate/source_contract_pending。CI 已增设 `.4`、`.5` 双标签的 `verify_crew10_integration.py` 无写入源码契约验证；待 CI/Windows 跑过后再提升受审计基线。Crew 0.9 仍不支持。

## 2. 已修复问题

| 编号 | 等级 | 缺陷 | 修复与边界 |
| --- | --- | --- | --- |
| F08 | P1 | ACP Session JSON 采用线程级保护与整份覆盖，跨进程 stale snapshot 会擦除更新事件 | `storage_revision` 单调递增；稳定 sidecar `.lock` 内执行读取→对比→原子写入；revision/身份不匹配拒绝修改，不再静默覆盖 |
| F09 | P1/P2 | 两个 ACP 进程可能对同 Session 同时开始 Prompt | 独立的 `.turn.lock` 持有整个 Prompt，冲突响应 `-32002`；执行前对快照 revision 做二次确认。不是远程工具 exactly-once |
| F10 | P2 | 检查点写入失败时，执行路径可继续进入模型/工具 | `_persist_session` 返回落盘状态；关键用户消息锚点保存失败时回合终止，不调用模型和工具；内部普通 best-effort 保存仍显式记录失败 |
| F11 | P2 | `verified_family_regex` 把尚未验过源码的 `.5` 也显示为 `version_audited=True` | 审计标识现在只在明确 `verified_through` / `verified_versions` 命中时显示，通过适配器的代码检查与“正式审计”分别呈现 |
| F12 | P3 | Stale Session 对配置请求抛异常时可能作为 JSON-RPC 内部错误 `-32603` | `_safe_handle` 统一将 `SessionBusyError` 返回 `-32002`，不误导客户端为服务器崩溃 |
| F13 | P3 | CI 只比对 `.4` 和少量 Windows 进程测试 | `.4`、`.5` 双源码契约 job；扩充跨平台进程测试到 Session fencing 和 Global Integrity |
| F14 | P2 | 新 revision 校验初版可能阻断旧 sanitized-name Session 文件迁移；Session 列表可能重复显示旧/新记录 | 哈希路径不存在时验证同 ID 旧文件的隐含 revision 0；首次保存迁移；按 session ID 去重并优先新哈希路径 |

## 3. 测试方法

- 全量 pytest：**819 passed、1 skipped、52 subtests passed**（单次运行，0 failures）。本轮加入 `tests/test_session_snapshot_fencing.py` 6 个独立回归测试，另新增 1 个版本审计置信度测试。
- 测试包括两个 *spawn* 进程从相同 revision 并发提交：只允许一个成功，其余明确 stale；老消息不能被覆盖。
- 另一个独立进程持有 `.turn.lock` 时，Prompt 必须在模型侧执行前被拒绝；未保存检查点不能伪称成功。
- 兼容旧 schema-v3 无 revision 会话：加载时读作 revision 0，首次成功保存迁移为 revision 1。
- 原有 Tiga / Dyna / Provider / Crew 0.10 适配补丁均未变更。

## 4. 仍未解决的风险

1. **Windows 真实文件锁**：新增跨进程锁在 Linux 下验证，Windows CI 任务已增设但本地没有真实 Windows 执行证据。
2. **同一 Session 的进程所有权**：本轮为 turn 级执行锁与 snapshot fencing，而非长期会话租约 / 分布式 fencing token。跨设备、网络文件系统和不遵守锁的第三方写者不在保证内。
3. **工具副作用**：远端 MCP 请求可能已生效却未回执；不能安全自动重复，更没有分布式 exactly-once 承诺。
4. **Crew `.5` 实机**：需解压真实 `.5` 源码，运行无写入 source-contract 和 Windows 安装卸载/队友/权限/浏览器/Steering 场景，才能声称正式适配通过。
5. **会话和 Durable Run 非单事务**：独立检查点避免盲目继续，但不能消除电源中断时两份记录之间的所有一致性窗口。
6. **模块结构债**：`turn_runner.py` 和 `acp_server.py` 仍较大，建议分步提取 SessionPersistence / ToolAdmission / RecoveryService，不宜本轮大拆。

## 5. 安全升级约束

- 产品版本恒为 **0.2.3**，Build ID 自动由运行代码 fingerprint 计算。
- Crew 0.9 已退役；Crew 0.10 源码契约失败则拒绝安装。
- 这是一份新安装包/源码包，不会直接覆写电脑上的 Crew 或用户数据。
- Tiga `Sky/Multi/Power/Glitter` 不改、Dyna `Flash/Strong/Miracle` 仍不可执行。
