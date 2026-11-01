# Kiro BYO 0.2.5 — KiroCrew 0.11.0-rc.2 候选兼容修复

日期：2026-10-29  
基线：第五阶段 `0.2.5+6f9d9920e6903762`  
当前目标：Crew 0.11.0-rc.1（已源代码审查）；Crew 0.11.0-rc.2（条件兼容候选）

## 背景与问题

截图中的 Crew 已更新至 `0.11.0-rc.2`，而旧 BYO 在 `resolve_target()` 有严格的 RC1 白名单。兼容 JSON 的 `allowed_crew_versions` 也限定 RC1，因此即使 RC2 的内部结构相同也会被先行拒绝。此外，原有 `_dashboard_discovery()` 对 RC2 选择了旧版前端适配器，存在错用模型菜单显示补丁的风险。

## 修改文件

- `src/kiro_byo/integration_manager.py`：新增 RC2 候选集合；允许其进入 21 个源码点严格校验；对候选 RC2 额外验证已安装前端模型选项 DOM 契约；继续禁止任何不兼容 Crew 源码被直接写入。
- `integration/kiro_crew/v0.11-rc1/compatibility.json`：增加 `semantic_candidate_versions`，**不**将 RC2 标为 `verified_versions`；保留 RC1 源码 SHA256 固定基线。
- `src/kiro_byo/crew11_model_labels.py`：说明适用于满足结构检查的 Crew 0.11 RC 候选，逻辑未更改。
- `scripts/verify_crew11_integration.py`：可以针对 RC2 的本机实际 `kiro_crew` 源码做只读预检，区分候选检查与 RC1 源码审计。
- `scripts/release_check.py`、`scripts/build_release.py`、兼容文档、`CHANGELOG.md`：记录候选与验证状态，不误标正式通过。
- `tests/test_crew11_candidate.py`、新增 `tests/test_crew11_rc2_provisional.py`：覆盖候选识别、旧版拒绝、缺失源码/前端拦截、形态显示桥接选择及合成语义通过路径。

## 不变的部分

Tiga 的 `Sky/Multi/Power/Glitter`、默认 Multi、Type wire ID、具体模型直连、Provider 与 ACP 执行器、Crew 原生日程和后台双向同步、Mate/Worker fail-closed 保护均未改写。

## 验证

- 全项目 pytest：**1033 passed, 1 skipped, 52 subtests passed**（Linux 隔离运行）。
- Python compileall、Node JS 语法、发行元数据一致性均通过。
- 源码 ZIP、App 安装 ZIP 和 wheel 均通过逐项归档完整性校验；已核对新增 RC2 兼容逻辑、构建版本号以及 Mode/Type 受保护文件。

## 保留限制

**RC2 完整源码目前未公开获取，且本环境不能访问用户本机的 KiroCrew 运行目录。** 这是保留安全校验的“条件安装”包，而不是已认证 RC2 的全量适配。若新版本修改任一语义适配点或 model-picker UI，仍会提示无法兼容；此时需提供本地安装目录中的 `kiro_crew` 源码以审查确切差异。不要强制替换、跳过校验，也不能据此保证官方积分不会产生或 Worker 实机 E2E 已通过。

## 本地更新

将完整源码包解压后合并覆盖本地项目，保留 `.git` 和本地 Provider/密钥配置。通过 KiroCrew 的 App 更新安装新包，重新启动 Crew Gateway，查看 BYO → 设置 → KiroCrew 的“兼容与预检”状态。若需要手动只读诊断：

```
PYTHONPATH=src python scripts/verify_crew11_integration.py --crew-package /path/to/kiro_crew --crew-version 0.11.0-rc.2
```

无法通过预检时，错误报告会指出最先发现的不兼容源码/前端结构，不应该强行继续安装。
