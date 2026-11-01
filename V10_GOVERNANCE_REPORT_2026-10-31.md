# Kiro BYO 0.3.0 — V10 有边界的安全与架构治理

**审计基线**：V9 Records Center `0.3.0+b0a8e96753ee107a`。本轮版本号仍为 `0.3.0`；构建标识由实际运行时代码 SHA-256 产生，不冒充已通过所有平台的稳定版本。

## 执行原则

- 只从 V9 副本修改并构建；未操作用户现有配置、遥测、会话或 Durable Run 文件。
- 未修改 Tiga/Dyna Policy Pack、模型权重、路由 ID、Provider 适配行为、ACP 消息格式、Crew 前端切换机制和五语言键。
- 仅修复经独立审计复现的四处边界；不为了减少总行数而拆分 Harness。

## 变更明细与安全边界

| 编号 | 修改位置 | 修复内容 | 仍存在的限制 |
|---|---|---|---|
| AUD-001 | `src/kiro_byo/runtime_trace.py` | 扩大敏感请求头识别；脱敏多层数据、URL userinfo、包含 URL 编码键名的敏感查询参数与已知身份验证片段 | 已有的历史日志不会自动回写或清理；`full` 仍可能包含其他用户明文内容，限本地调试使用 |
| AUD-003 | `src/kiro_byo/telemetry.py` | `clear()` 使用与 `record()` 相同的进程内锁 + 稳定 sidecar 跨进程锁 | 非合作外部程序直接写文件仍不受锁约束 |
| AUD-004 | `src/kiro_byo/provider_admin.py` | 在修改事务入口包住完整读改写，递归的凭据迁移沿用外层锁，避免重复加锁死锁 | Provider 网络发现和密钥库操作在部分路径下仍发生于长事务内，可能造成管理请求等待；建议后续独立抽取无副作用的发现阶段、再提交 |
| AUD-002 | `src/kiro_byo/durable_runs.py` | `clear()` 只清除明确成功完成且无未知风险/活跃租约的记录；`_prune_locked()` 复用同一判断 | 若记录损坏，清理会保留文件而不是猜测安全状态 |

## 兼容及风险

1. `DurableRunStore.clear()` **行为有意更保守**。此前依赖“删除所有记录”的测试现已更新为验证保留保护记录。管理端不能再用普通清理请求删除未确认副作用的账本，这是安全保证，不是功能退化。
2. `provider_admin` 不改公开函数签名，原有 `providers.json` 格式不变；新增 `.lock` sidecar 是锁文件，绝不能在运行时手工删除。
3. 配置锁仍是 advisory（依赖合作进程都使用同一事务入口）；`_write_raw()` 是私有底层写入函数，第三方直接调用无法享受读改写事务隔离。
4. 新写入 Runtime Trace 做脱敏，但既有完整追踪文件在用户确认前不会被擅自删除。怀疑旧日志含凭据时，建议备份后经产品现有的诊断清理入口处理。
5. 没有改 Crew 版本准入；0.11.0 / rc、Stable、Insider、Nightly 及 Windows/macOS/Linux 的真实宿主 E2E 仍需分别验证。

## 值得精简但本轮不动的技术债

- **Crew 注入脚本**：`crew11_model_labels.py` 的内嵌 JS 较大，需先建立对应 DOM/React Query 接口契约和浏览器测试，再考虑提取独立可验证资源；不直接改宿主压缩产物。
- **Turn Runner 与 ACP**：存在有价值的事务性执行边界，不以行数为指标拆分。优先审查重复状态判定与跨模块转发的真实运行成本。
- **Provider 发现事务**：避免在跨进程锁内等待慢速网络是下一项值得做的性能治理，但需要两阶段提交与凭据回滚设计，本轮不冒险实施。
- **Durable 诊断字段**：建议后续缩窄 `public_record` 的 metadata 投影；不与普通历史清理耦合。
- **历史兼容逻辑**：不能以静态“无调用”直接删除 Crew 0.10/0.11 兼容入口，需要可核实的发布支持范围和卸载/回滚测试。

## 结论

四项缺陷是正式发布前应当修复的边界问题。本轮通过有限代码变动建立安全保证，**没有证据支持为“精简”而复制或推倒现有 Agent Harness**。跨平台真实 UI、长周期并发压力、真实 Provider/MCP 工具副作用仍属于待验收项。

## 本轮实际验证结果

- `PYTHONPATH=src:. pytest -q tests/test_v10_storage_governance.py tests/test_runtime_trace.py tests/test_s3_01_durable_safety.py tests/test_provider_instances.py tests/test_telemetry.py tests/test_030_windows_config_replace_retry.py`：**32 passed**。
- 全测试目录（`tests/test_*.py`，共 153 个文件）按 24 个文件一组执行：**7 组均退出码 0，共 1,345 passed、1 skipped、58 subtests passed**；组别原始输出见证据包。相对 V9 多出的 5 项为新故障注入测试。**这是全部测试文件的分组回归，不是单进程全量通过**。
- 一次单进程全套运行未在执行窗口内完成，约 25% 后停止；后续逐项输出运行到约 45% 时也没有在指定时间内完成。此次不能把上述两次中断视为通过或功能失败。
- `python scripts/architecture_guard.py --check`：通过，**54/54** 个超过 100 行函数，9/9 超过 200 行函数；未放宽阈值。
- `python scripts/release_check.py --metadata-only`、`python -m compileall -q src backend scripts`、`node --check ui/index.mjs`：通过。
- `python scripts/build_release.py --output-dir <path>`：安装 ZIP、源码 ZIP 与通用 Python wheel 构建成功，并校验运行时代码指纹。
- 本次未重新测量 coverage；V9 审计的 74% 不能冒充 V10 新覆盖率。

**未验证**：Windows/macOS/Linux KiroCrew 实机 UI、跨系统真实文件锁行为、真实 Provider/MCP 端到端调用、长时并发和缓存/内存稳定性。

## 后续最小优先工作

1. 用受控进程模拟慢速 Provider 发现，评估配置锁的实际排队等待并设计“外部发现→受锁提交”的两阶段事务。
2. 用明确的白名单实现 Durable Run 诊断输出的最小可见字段，保护用户内容。
3. 分别在 Windows/macOS/Linux 对文件锁、配置迁移、UI 模式切换与恢复状态做真实 E2E。
4. 审核老宿主补丁的删除条件，不擅自删除历史兼容代码。
