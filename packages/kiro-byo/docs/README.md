# Kiro BYO 文档

Kiro BYO 的开发文档与版本记录。

> **Kiro BYO 是什么**：面向 KiroCrew 的独立 ACP Agent Harness。在 KiroCrew 里，它与官方后端（Kiro CLI、Claude Code、Codex 等）**平级**，是唯一"选它 = 解锁所有模型"的后端——因为 Crew 自身不能随意换模型，官方后端一个绑一个模型来源，而 BYO 一个后端接所有模型，由 Tiga / Dyna 两大 Mode、七种 Type 智能路由。
>
> 职责边界：**Crew 决定谁工作、如何组织；BYO 决定用什么模型能力、以什么策略可靠完成；模型服务提供脑子。**

## 目录结构

```
docs/
├─ README.md          ← 本文件（文档索引）
└─ changelog/         ← 版本记录与开发日志（按时间 / 版本）
   ├─ 0.3.8-release-notes.md
   └─ 2026-11-07-stability-fixes-and-patch-slimming.md
```

## 版本记录

| 日期 / 版本 | 内容 | 文件 |
|---|---|---|
| 0.3.8 | 0.3.8 发布说明 | [0.3.8-release-notes.md](changelog/0.3.8-release-notes.md) |
| 2026-11-07 | 稳定性修复与补丁收敛（迈向 0.4）| [2026-11-07-stability-fixes-and-patch-slimming.md](changelog/2026-11-07-stability-fixes-and-patch-slimming.md) |

## 迈向 0.4 的主线

0.4 的核心不是加功能，是 **瘦身 + 稳定 + 说得清**：

1. **瘦身**：砍耦合与死代码（不砍功能），从约 42,000 行逼近约 35,000 行。
2. **立规矩防复发**：每加一个东西先问"能不能不改官方 Crew 源码"；改不了的，明确标成"自维护扩展层，非官方原生"。
3. **接仪表盘 UI**：把路由盘 / 概览 / 记录中心接进界面，让 BYO 第一次"看得懂"。
4. **实机验收 + 简洁交付包**，再上 GitHub。

详见最新一篇 changelog。
