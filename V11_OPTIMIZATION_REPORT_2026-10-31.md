# Kiro BYO 0.3.0 V11 — 有边界的架构优化与修复报告

## 范围与基线

- 基线：V10 `0.3.0+22232bd3a9b2f844`，在独立副本操作；未接触真实 Provider 密钥、用户账本或桌面安装。
- 保留：Tiga/Dyna 的 Policy Pack、默认 Type、内部模型 ID、ACP 工具权限、运行恢复及五语言 UI 行为。
- 本轮目标：缩短 Provider 配置锁等待；让 Durable Run 对外诊断返回最小必要字段；补充故障注入和发布验证。

## 已修改

1. `src/kiro_byo/provider_admin.py`：`configure_provider` 先做连接验证及模型发现，再进入受跨进程锁保护的 `_commit_configured_provider`，锁内重新读取当前配置并进行凭据迁移与持久化。慢速网络不再占用配置锁。
2. 同文件：`refresh_provider_models` 先读取连接快照并完成模型发现，再由 `_commit_refreshed_provider_models` 进入配置锁；若 Provider 已删除、端点、凭据引用、Provider 家族、凭据内容发生变化，拒绝提交旧目录并要求重新刷新。保持模型思考偏好的原有合并逻辑。
3. `src/kiro_byo/durable_runs.py`：`public_record()` 仅公开必要字段；排除任意 metadata、step objective、checkpoint 与 tool receipt。保留 UI 需要的 `metadata.model_id` 和 `lease.active` 等已有字段。恢复策略和对账仍只读取真实持久账本。
4. `tests/test_v11_governance.py`：隔离环境测试公开数据脱敏、网络发现期间跨线程 Mode 更新不被锁阻塞，以及 Provider 端点或密钥变化时旧刷新提交被拒绝。

## 发现的其他技术债（本轮未贸然修改）

- Crew 0.11 前端注入脚本通过大段 Python 内嵌 JS 实现，直接拆分可能影响静态源码补丁匹配。建议建立明确的宿主脚本接口契约和浏览器 E2E 后再进行资源模块化。
- `turn_runner.py` / `acp_server.py` 含有不可随意拆散的事务、安全与恢复边界；没有可量化的收益与等价测试前，不建议通过简单拆文件降低行数。
- `providers.json` 的跨进程 sidecar 锁是合作式 advisory lock；外部不使用该锁的写入者不受保护。新流程不能提供跨文件凭据库与 JSON 文件的严格原子事务。
- 客户端“点击发送→最后一帧可见”的耗时仍未独立埋点；服务器 Turn 耗时不能冒充它。
- Crew 原生无黑屏热更新、Windows/macOS/Linux 实机验收、长期真实 Provider/ACP/MCP 故障压测尚未完成。

## 数据安全与兼容风险

- 本轮有意收紧外部 `public_record` 的字段：若第三方插件曾依赖公开的 raw steps/checkpoints/任意 metadata，必须改为使用已经脱敏的 `diagnostics` 结构；这属于安全强化，需在宿主实机验收。
- Provider 模型刷新期间发生相关配置变更会抛出可理解的重试错误；不再静默覆盖新配置。
- 历史 Run 数据仍完整保留，不执行清理和迁移；仅限制公开诊断输出。

## 验证结果

- `PYTHONPATH=src:. pytest -q tests/test_v11_governance.py`：**4 passed**；包括竞态、元数据保密和慢速发现非阻塞。
- 全部 `tests/test_*.py` **154 个测试文件按 24 文件一组共 7 组执行，1,349 passed、1 skipped、58 subtests passed**。每组正常退出 0。此为完整测试文件的**分组回归**，不是单进程全量通过，也未重新测量覆盖率。
- `python scripts/architecture_guard.py --check`：通过，超过 100 行函数 **54/54**，超过 200 行函数 **9/9**，未放宽预算。
- `python scripts/release_check.py --metadata-only`、`python -m compileall -q src backend scripts`、`node --check ui/index.mjs`：通过。
- `python scripts/build_release.py --output-dir ...`：成功，运行时代码指纹 `aaf9da18ba53683b3b8757b9e1170f437d9ae93d9922af3a52f57dd9211e60b6`，简写 `0.3.0+aaf9da18ba53683b`。
- **未验证**：Windows/macOS/Linux Crew 实机、真实 Provider/MCP 操作、长时压力与完整 UI 渲染耗时。历史测试覆盖率不得复用为 V11 的实测覆盖率。

## 工程判断

V11 是针对已确认技术债的局部修复，不是覆盖所有未知 Bug 的保证。没有证据表明应该继续大规模重构 Harness；建议下一阶段针对宿主 E2E 和长周期并发恢复作专门验证。
