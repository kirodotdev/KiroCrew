# Kiro BYO i18n terminology — 0.2.2

Five supported UI locales: `zh-CN`, `zh-TW`, `en`, `ja`, `ko`. These are UI
presentation values only and MUST NOT replace route or ACP wire identifiers.

| Concept | zh-CN | zh-TW | English | 日本語 | 한국어 |
|---|---|---|---|---|---|
| Provider | 供应商 | 供應商 | Provider | Provider（プロバイダー） | Provider(공급자) |
| Model | 模型 | 模型 | Model | モデル | 모델 |
| Mode | Mode（模式） | Mode（模式） | Mode | Mode（モード） | Mode(모드) |
| Type | Type（形态） | Type（形態） | Type | Type（タイプ） | Type(유형) |
| Route | 路由 | 路由 | Routing | ルーティング | 라우팅 |
| Request | 请求 | 請求 | Request | リクエスト | 요청 |
| Response | 回答 | 回答 | Response | 応答 | 응답 |
| Visible reasoning | 可见推理 | 可見推理 | Visible reasoning | 表示される推論 | 표시되는 추론 |
| System overview | 系统总览 | 系統總覽 | System overview | システム概要 | 시스템 개요 |
| Integration | 集成 | 整合 | Integration | 連携 | 통합 |
| Checkpoint | 检查点 | 檢查點 | Checkpoint | チェックポイント | 체크포인트 |

Terms deliberately kept verbatim: Kiro BYO, KiroCrew, Tiga, Sky, Multi, Power,
Glitter, Dyna (when installed), ACP, LLM, API, JSON, SDK, Token, Provider IDs,
model IDs, tool names, endpoints, wire fields and Type selection IDs.

The translations live in `src/kiro_byo/locales/*.json` and UI lookups are
performed through `tr(key, parameters)` in `ui/index.mjs` with `en` fallback.
Historical `ui.<hash>` keys preserve stable identity when the visible text
changes; newer semantic keys (`settings.*`, `mode.*`, `health.*`, `alert.*`,
`chart.*`, `error.*`) are preferred for further development.
