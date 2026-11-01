# Kiro BYO 0.2.3 — S3-03 故障注入与恢复准入状态机

日期：2026-10-24  
基线：`S3-02`（Build `0.2.3+2dd9979cb1692b0b`）  
目标：KiroCrew `v0.10.0-insider.4`  
产品版本：**0.2.3 不变**

## 本轮修改

1. **持久化恢复准入重校验**：`DurableRunStore.prepare_resume` 在受同一 Ledger 锁保护的状态变更中再次检查 Session 所有权、已完成状态、未核实工具副作用、原有恢复阶段，以及 `replan_required` 的策略限制。同一进程内两个请求就算先前同时通过了 RecoveryPolicy，也不能都创建恢复实例。重复请求会明确拒绝，而不会再把 `resume_count` 重复递增。
2. **崩溃后显式重新准入**：`resuming/retrying/replanning` 中的仍在运行的任务不可直接再次 `prepare_resume`。只有新进程/新会话按现有恢复流程将它调和为 `pending_restart`（或标记副作用不确定）后，才允许用户再次显式恢复。这不触发自动恢复。
3. **执行前 Ledger 建立失败即停止**：无论是新回合还是恢复回合，如果持久化 Step 或 Attempt 创建失败，BYO 不再继续运行模型和工具。无法开启 Durable Run 时会向 ACP 返回错误消息，不会伪装成成功进入执行阶段。
4. **注入式故障测试**：模拟 `os.replace` 写入中断，确认原记录仍完整、临时文件被清理；并发恢复准入只成功一次；完成任务与副作用不确定的任务不能被重新打开；模拟磁盘写入故障时不启动无跟踪运行。

## 未更改

- Crew 原生队友、日程、数据库、ACP 补丁；Tiga 的 Mode → Type → Model 和四形态；Dyna 禁用状态；模型路由评分、权限策略、语言及 UI。
- 未实现跨进程锁、远程工具的 exactly-once、断电级强制 `fsync`、远端 MCP 调用撤销或 Windows 端到端验收。对已经发出的工具调用必须保留“可能发生副作用”的保守判断。

## 测试记录

- 新增 `tests/test_s3_03_fault_state_machine.py`，核心故障注入及旧 Durable/ACP 测试集通过；详细结果以本轮生成时的实际日志为准。
- 单进程锁只能保证共享当前进程状态的准入原子性。两个互不协调的 BYO 进程共同写同一数据目录仍然属于待设计的跨进程所有权问题。
- ACP 连接真实掉线、Windows 强制进程退出以及远端工具返回未知等场景，需要后续实机故障演练。

## 下一步

先确认实际 Windows Crew 上的恢复状态机与取消行为，再开始 S4 Mode Registry 插件化。未经真实演练不得宣称完整 exactly-once 或原生功能全面对等。

## 最终本地回归（2026-10-24）

单次完整 `pytest -q` 在执行约 78% 时超时，没有得到完整结果，因此**不记为通过**。随后按测试文件划分为不重叠的两组独立执行：A 组 **474 passed, 1 skipped, 22 subtests passed**；B 组 **307 passed, 30 subtests passed**。两组合计 **781 passed, 1 skipped, 52 subtests passed**；另有重点 ACP/Durable 交叉检查 **111 passed**（与前述测试存在重复，不累加）。未出现失败断言。分组运行不完全等价于单进程长时间串行运行，原超时原因仍待排查。
