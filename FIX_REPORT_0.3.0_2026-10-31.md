# Kiro BYO 0.3.0 独立修复报告（2026-10-31）

## 变更范围

| 来源问题 | 实施内容 | 验证结果 | 仍需验证 |
|---|---|---|---|
| BUG-001 | 冷会话失效的旧 Mode Type 迁移至当前 Mode 默认 Type | 真实注入 helper 执行测试通过 | Windows 会话菜单实际交互 |
| RISK-001B | Mode PUT 后 host slot 协调 + App 通知 + Crew 0.11 目录刷新信号 | Node.js 注入 JS 事件测试通过 | React Query 缓存同步及异源 WebView |
| COMPAT-002 | 0.11.0 正式版仅列入 source-contract 受控候选 | 合同缺失拒绝测试通过 | 完整上游源码、Windows 安装更新卸载 |
| QUALITY-003 | 抽离兼容性导出依赖契约验证，保留原预算 | 54/54 架构检查通过 | 后续架构治理 |
| CLEANUP-009 | 改正 Dyna 已上线但注释称未发布的误导 | 静态检查与测试通过 | 无 |

不修改 Tiga/Dyna 权重、内部模型 ID、真实用户 Provider 配置、ACP 权限、Durable Run Ledger 或已执行任务。未绕过任何预检。

## 回归验证

- 环境：Linux，Python 3.13，Node.js 22；未连接真实 Windows KiroCrew / Provider / MCP。
- 按 142 个 Python 测试文件拆分为六组运行，结果依次为：197、205、315、160、133、250 通过；总计 1260 通过，1 跳过，58 subtest 通过；所有分组退出码均为 0。
- `python scripts/architecture_guard.py --check`：PASS（55 → 54，预算 54）。
- `python scripts/release_check.py`：PASS。
- `node --check ui/index.mjs`：PASS。
- 原计划单进程完整 pytest 运行在测试环境中中断，不能宣称单次完整命令通过；保留各分组日志作为真实证据。

## 部署要求

1. 先备份用户的 Kiro BYO App 及 KiroCrew 安装目录，不要手动覆盖正在运行的 Python 宿主文件。
2. 使用安装包的原有集成管理流程，在 KiroCrew 关闭或遵照程序安全更新事务的条件下重新安装集成补丁。确认 `BUILD_INFO.json` 的 build_id 变化，必要时重新启动 KiroCrew。
3. 在同一个已经存在的会话：Tiga Multi → Dyna Flash → Tiga Multi；再验证 Dyna Miracle/Strong、具体模型直连、正在执行期间的切换、官方后端隔离以及三个配置面。
4. 0.11.0 正式版必须完整通过宿主源码语义与 UI 合同预检；任何拒绝应当保留日志而不是删除检查。

## 不属于本轮已修复的事项

- Windows 实机 E2E、真正的 React Query 缓存端到端、跨源 WebView 广播确认、跨进程崩溃注入和真实 Provider/MCP 长跑。
- `unknown_side_effect` 实际外部系统 exactly-once 保证、Policy Pack 内容哈希快照、OS 级 Windows Shell 沙盒。
- 前述审计中的 PERF-006、ARCH-007、POLICY-008 属于后续稳定维护工作，本轮未做高风险大改。

**状态：修复候选构建；不标记为正式稳定版。**
