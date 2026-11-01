# BYO 0.2.3 全局可靠性修复 · Windows 0.10 集中验收

1. 使用本轮安装包与 `KiroCrew v0.10.0-insider.4`。对正在使用的 Crew 数据先备份。旧 Crew 0.9 不再安装或测试。
2. 安装/卸载检查：验证源码契约匹配、ACP 连接、恢复无异常，卸载后保留用户配置与会话。
3. Run 写竞争：从两个独立进程对同一个测试 Run 并发 checkpoint；预期两次检查点保留、`revision` 增加；一旦副作用风险为真，不能变假。
4. AgentState/Telemetry/RuntimeTrace：启动两个 ACP 实例并发写入不同测试任务、事件；预期不丢记录，且没有数据文件格式损坏。
5. Resume/Retry/Replan：已取消、已完成、运行中 Run 不得直接重新执行；失败且没有副作用的 Run 在显式允许时可恢复，存在副作用风险的 Run 必须拒绝。
6. Crew native parity：浏览器发现、工具权限撤销、Steering/Queue、Saved Agent、Skills、MCP、队友、新建/取消/重试日程与后台任务；比对 Crew 原生 Harness 行为。
7. Dyna 三形态仍只读不可执行，Tiga 的四形态选模记录和实际模型一致；所有入口不允许绕开 Dyna 限制。
8. 断线/kill -9/错误磁盘与网络共享盘属于单独故障测试；本地文件锁不保证远端副作用 exactly-once。

实际结果须记录 BYO Build ID、Crew commit、测试平台、每例原生日志、预期/观察及失败复现。未经 Windows 验收，不标记生产级对等。
