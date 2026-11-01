# Kiro BYO 0.2.5 — 第四阶段修复说明

日期：2026-10-29
前一构建：`0.2.5+9db33ff4357a7272`
目标上游：KiroCrew `v0.11.0-rc.1`

## 改动范围

- `src/kiro_byo/crew_shared_patch.py`：安装在 Crew `members_parts/member_sessions.py` 中的 BYO 成员路由保护不再以注释 marker 作为唯一依据。既有保护必须与实际生成的完整保护块一致（禁止官方 override、注册探测失败关闭、始终路由 BYO），否则报错并停止升级。
- `src/kiro_byo/crew11_patch.py`：Crew `config/loader_builders.py` 中的 BYO 成员默认后端接管逻辑同样校验完整性；不再将残缺旧补丁视为已安装。
- `tests/test_crew11_guard_integrity_stage4.py`：新增 8 项安全回归，覆盖完整守卫幂等、残缺守卫拒绝、后端判断篡改及重复标记。

## 不变的产品语义

- 官方后端时保持 Crew 原生模型与已保存偏好；BYO 为主后端时只使用 BYO 候选与有效新成员路由，不自动修改官方保存的设置。
- `Mode / Type / concrete model` 三层语义不变，Tiga Sky、Multi、Power、Glitter 及默认 Multi 不变。
- 已对第三阶段基线的 `protocol.py`、`mode_router.py`、`acp_server.py`、`mode_registry.py`、`mode_lifecycle.py`、`provider_admin.py`、`crew_dashboard_patch.py`、`policy_packs/tiga.json`、`integration/.../harness/kiro_byo.py`、`ui/index.mjs` 共 10 个文件逐字节比对，内容一致。

## 验证

- 专项（Crew Mate 与 Mode/Type）：112 passed。
- 共 118 个 `test_*.py` 文件分 4 组：266 + 363 + 187 + 209 = **1025 passed，1 skipped，52 subtests passed**，无失败。
- Python `compileall`、前端 `node --check`、`scripts/release_check.py` 均通过。
- 本阶段未进行 Windows KiroCrew 实机测试、真实 BYO Provider 调用或官方计费用量核查。并未宣称 Mate/Worker E2E 已完成。

## 保留的限制

- Crew 原生 Cron 编辑器与 BYO 页面之间仍缺少统一原子 CAS，现有 BYO 内部锁和写后复核不能完全防止跨界面覆盖。
- 对 Crew 0.11 RC1 的源文件注入保持较高耦合；此项改进是篡改检测，而不是消除 21 个兼容注入点。
- 需要在真实 Windows 上验证安装、升级、Mate Worker Kit、模型菜单及双向同步。

## 本地安装

源码包作为项目文件合并覆盖，保留现有 `.git` 和用户数据；App 安装包用于 KiroCrew 中更新 BYO。请在安装前备份当前版本。`VERSION`、App、Python 包和协议版本号继续保持 `0.2.5`，通过 `BUILD_INFO.json` 新指纹识别本轮构建。
