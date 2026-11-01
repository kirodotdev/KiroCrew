# Kiro BYO 0.3.0 — Mode / 模型目录不一致专项修复（第 4 次候选）

日期：2026-10-31。**Windows KiroCrew 实机端到端状态：未验证。**

## 问题

实机截图显示 BYO Mode 页面选择 **Dyna**，但 Crew 聊天模型菜单仍包含 **Tiga 的 Sky/Multi/Power/Glitter**，而当前 Dyna Flash 行提示“目前未提供”。此前多次只靠会话协调和浏览器刷新推断已修复，未拿到真实宿主目录/缓存各层证据。

## 本次静态确认的缺陷

1. `src/kiro_byo/crew11_model_labels.py` 将“当前选中模型 chip 的 Mode 与服务端吻合”当作收敛条件。对于 **Dyna chip + Tiga picker** 的截图场景，这是错误信号，可能清除重载防循环状态，使重试行为不正确。
2. 前端检测选项时只使用特定 `button[role=option]` 子树选择器，对不同但合法的 ARIA option DOM 结构不够兼容。
3. 旧防重载 `sessionStorage` 标志以目标 Mode 为单位长期保留，一旦同目标上次失败，返回相同 Mode 可能一直不能再尝试刷新。
4. 后台 BYO App 与 KiroCrew `GET /api/models` 由不同调用入口读取配置；此前没有足够的只读诊断区分目录来源错误、安装旧补丁和 React 前端缓存过期。

## 修改

- 修订宿主模型目录检测：从真实的 `[data-model-id^="kiro-byo-type:"]` option 检查活动 Type，而不是只信 composer chip；在菜单 option 新增时立即检查。
- 仅在**菜单本身**与目标 Mode 一致时清除防重试状态。限定每次目标 Mode 最多 2 次安全重载，间隔至少 15 秒；切到不同 Mode 时清除旧目标的陈旧重试记录，不强制重载正在执行任务或带草稿的页面。
- 新增只读 `GET /api/apps/kiro-byo/mode-sync`，返回 BYO App 与 Crew 的持久化配置文件路径/摘要、Mode/Type、当前宿主后端、已安装模型目录处理器及 v4 同步脚本标记。**不读取或返回 Provider 密钥，也不改写用户配置、会话或 Durable Run。**
- BYO 模式页在诊断发现配置来源不一致或仍为旧同步补丁时，给出可见提示。

## 验证范围

- 独立 Node VM 测试复现 **Dyna 已选中 + 菜单仍为 Tiga**；确认有限重试、返回相同 Mode 不会被旧记录永久锁死，也不会进入无限重载。
- 宿主同步相关回归共 88 项通过（含新定向测试），JS 语法、Python 编译、架构预算及 release_check 通过。
- 实机端到端未验证。*只读诊断可告诉我们是服务端模型目录陈旧还是前端 React 缓存残留；不要将此离线构建误认为彻底解决。*

## 实机观察与后续定位

1. 安装时务必通过 BYO「更新集成」覆盖 KiroCrew 的 Python 入口和 `static/dist/index.html` BYO-owned 注入段，然后完全退出 KiroCrew/Gateway 再重启。
2. 在 Dyna 模式下访问同源只读路径 `/api/apps/kiro-byo/mode-sync`，核对 `catalogue_comparison`、`installed_host_patches`。前者若为 `mismatch`，应先解决两个配置目录不一致；若 `mode_watch_script=false`，宿主补丁未正确升级。
3. 若以上两项均正常，但菜单仍是 Tiga，继续比对同源 `/api/models` 返回的 Type ID：返回 `kiro-byo-type:tiga:*` 说明是服务端模型目录；返回 `kiro-byo-type:dyna:*` 说明是浏览器模型缓存/注入脚本生效问题。
4. 对所有新发送消息验证真实 ACP Type，而非仅凭右下角文字；进行中的任务始终不强制迁移，旧的历史执行标签不可随新模式改写。

不改动路由策略、Type 顺序/默认值、具体模型 ID、权限系统、Provider 密钥与持久化账本。未来 KiroCrew 0.12 未验证，不纳入自动兼容承诺。
