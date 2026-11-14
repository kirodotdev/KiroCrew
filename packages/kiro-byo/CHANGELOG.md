# Changelog

本文件记录 Kiro BYO 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本遵循语义化版本。

> Kiro BYO 是面向 KiroCrew 的独立 ACP Agent Harness：在 KiroCrew 里与官方后端（Kiro CLI、Claude Code、Codex 等）平级，一个后端接入所有模型，由 Tiga / Dyna 两大 Mode、七种 Type 智能路由。

## [0.4.0] — 2026-11-07（稳定 + 瘦身 + 规范化）

0.4 的定义不是加功能，而是**瘦身 + 稳定 + 说得清**：全量测试本机全绿（1888 通过 / 0 失败 / 77 合理跳过）、代码不再无意义膨胀、结构能一眼看懂。

### 安全修复
- 原生工具沙箱越界：移除 `native_specs.py` 中把整棵 KiroCrew 数据主目录隐式加为允许根的逻辑。现在原生工具只能访问会话 cwd + 用户显式配置的 `allowed_roots` + 客户端每次声明的 `additionalDirectories`，对齐 ACP「额外目录由客户端显式声明」契约，杜绝跨会话 / 跨工作区读写。

### 稳定性修复
- MCP 子进程泄漏竞态：会话启动 MCP 子进程期间若被关闭，刚创建的子进程不再成为拖住进程不退的僵尸。新增持久关闭标志，`_launch_process` 在 Popen 后即检查并清理整个子进程树（Windows `taskkill /T`）；区分「会话真关闭」与「握手失败可重连」两种 close 语义。效果：原本卡死 110 秒跑不完的 ACP 测试套件，现 21 秒跑完并干净退出。
- 取消 / 插话（cancel/steer）卡死与语义对齐：修复取消信号在极小窗口到达时被误判为中断、导致终结响应发不出的卡死；并对齐官方 KiroCrew / Kiro CLI 语义——被取消的 turn 中，尚未随干净 turn 确认的 steer 回滚作废（cleared），不再谎报为已注入。
- 测试脚手架跨平台修复：`acp_conformance.py` 的 `Process` 用 `shlex.split(posix=False)` 在 Windows 拆分命令，不再把可执行文件路径里的反斜杠（如 `C:\...\python.exe`）当转义符吃掉，`WinError 2 找不到文件` 消失。两个真实 ACP 子进程端到端冒烟（Mode/Type/Model 线协议、work-link 往返）由此在 Windows 本机真实通过，而非跳过。
- 出队背压测试稳定化：队列上限背压测试显式关闭无关的 `runtime_trace` 落盘，避免每次 send 的磁盘 I/O 在慢文件系统上拖慢 2055 次发送、使写出线程停滞窗口先于队列填满触发，从而稳定验证「队列满 → queue exceeded」而非误报「stdout has failed」。产品背压逻辑本身未改。

### 架构收敛（减少对官方 Crew 源码的补丁依赖）
- 对官方 Crew 的改写文件从 20 个收敛到 17 个：
  - 退役 turn 计时显示补丁（`turn_dispatch_gates` / `turn_stats`，纯显示口径，与接入无关）。
  - 退役 `member_sessions.py` 补丁（现役仅剩一行注释，真实成员路由已在 `loader_builders.py`；官方后端选择优先级本就正确）。走正规退役轨道，升级时自动清除旧版遗留的强制守卫。
  - 退役 Crew 0.9 时代 split-backend 远古迁移死代码（基线只认 0.12+，该路径已走不到）。
- 架构棘轮诚实收口：`architecture_budget.json` 的 `core_python_lines` 基线由 46355 如实上调到 46455（净增约 100 行），对应上述取消回滚、MCP 竞态、沙箱越界三组必要安全修复，以及恢复被误删的 `advertised_native_tool_names` 再导出。注释写明这些是正确性代码而非臃肿；棘轮防的是无意义膨胀，不是必要安全修复。真正的大模块瘦身（约 42,000 → 约 35,000 行）留给专门的重构轮次。同步清理已确证的死 import。

### 新增（后端，未改界面）
- Web 管理界面新增三个只读数据端点：`/api/records`（统一运行记录）、`/api/overview`（状态总览）、`/api/routing-dashboard`（Tiga 路由可视化），为后续仪表盘 UI 做准备。现有界面风格未改动。

### 文档与工程规范化
- 文档目录由「开发与升级说明」规范为 `docs/`，开发记录归入 `docs/changelog/`。
- 新增 `docs/README.md` 文档索引、根目录 `CHANGELOG.md`。
- 修复 `.gitignore` 乱码并补全（打包产物、Python 缓存、密钥配置、实验目录、编辑器杂项）；清理 `__pycache__` / `.pytest_cache` 缓存；去除文件 BOM。

### 0.4 之后（下一轮）
- 真实 Crew Gateway 实机端到端验收（当前为单元 / 集成测试）。
- 巨型模块内部系统性瘦身（`turn_runner` / `acp_server` / `integration_manager` / `mode_router` / `native_tools`，目标约 42,000 → 约 35,000 行）。
- 仪表盘前端页面接入（三个只读 API 端点已就绪，严格复用现有界面风格）。
- 小米 MiMo 深化：评估接入其 Token Plan 订阅套餐（`providers/xiaomi_mimo.py` 已有基础）。
- 立「不再改官方源码则标自维护层」的防复发规矩。

---

## [0.3.8]
- 修复 Crew 主 Harness 与 Mate 的路由切换逻辑：官方 ↔ BYO 双向默认跟随，保留 Mate 独立绑定与会话级覆盖，修复 Dashboard 模型归属判断。
- 旧补丁安装、卸载与原文件恢复回归。

## [0.3.7]
- 整合 BYO 侧改动，提供简洁安装版、源码版与开发说明（仍为旧 Crew 兼容安装架构）。

## [0.3.6]
- rc.3：修复模型身份、按名称猜测性能、生成模态路由与思考能力约束问题。
- rc.2：精简部分旧安装代码，恢复架构预算检查。
- rc.1：修复 ACP 模型切换、配置响应与会话锁释放之间的竞态，新增真实 ACP 自检。

## [0.3.5]
- 修复 Dashboard 处理函数重新绑定后，推理强度辅助依赖不可见的问题。

## [0.3.4]
- 修复旧补丁退役后遗留的跨模块常量导入错误，避免 Gateway 启动失败。

## [0.3.3]
- 扩大 Crew 0.12 RC 版本兼容检查范围。

## [0.3.1]
- 架构审计重构与审计文档。

## [0.3.0]
- 架构重构、MCP 加固、记录中心、图标更新；大量修复报告归档。

## [0.2.5]
- 早期发布版本。
