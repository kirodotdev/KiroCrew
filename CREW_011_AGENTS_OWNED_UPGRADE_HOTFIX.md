# Kiro BYO 0.2.5 — Crew 0.11 RC1 agents.py 安全升级补丁

## 现场症状

BYO → 设置 → KiroCrew 集成提示 `src/kiro_crew/dashboard/handlers/agents.py (partial BYO source detected: restore pristine Crew before retrying)`。

## 根因

本项目之前发放的 BYO 0.2.5 包拥有两个不同版本的 `_kiro_byo_effective_member_backend` 辅助函数。启用“主 BYO 代理强制队友执行使用 BYO”后，辅助函数发生合法变更，但 Crew 0.11 RC1 检查器只接受完全相等，误将之前安装的旧函数判为外来部分补丁。

## 修复

新增针对 `agents.py` 的旧 BYO 集成所有权验证：真实 BYO 模型目录函数、Crew 的 BYO 模型选择分支、成员模型权限调用点以及 BYO 后端导入必须同时成立；已存在的 BYO 辅助函数必须符合本版或历史发行版的已知实现；转换后 Crew 原生代码的 AST 不发生变化；转换结果必须可重复应用且幂等。通过后标记 `upgradeable`，允许原地升级。未知部分补丁、异常辅助函数内容仍拒绝安装，且可使用干净备份恢复。

## 保留功能与边界

保留前版任务页布局、计划日程与后台模型同步、队友 BYO 路由保护、错误诊断与集成事务保护。不改动数据目录；不删除任何供应商及模型设置。

自动化安装/兼容专项测试 90 项通过。全量测试本轮执行超时；未在用户 Windows Crew 0.11 RC1 的真实 `agents.py` 上验证，无法保证用户机器上不存在其他历史或第三方残留。
