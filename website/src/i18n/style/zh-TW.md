# Traditional Chinese (Taiwan) style guide

Normative rules for `src/i18n/locales/zh-TW.json`. Where a rule is mechanically
checkable it is named alongside the test that enforces it; the rest are for whoever
reviews a translation PR.

**This catalog is not a script conversion of `zh-CN.json`.** Converting 设置 to
設置 yields Traditional glyphs that a Taiwanese reader still reads as foreign
software, because the Taiwanese word is 設定. Every value is translated from the
English source. Consult `zh-CN.json` only for what a key *means* in the product,
never for how to say it.

The terminology below is this catalog's convention, chosen to match common
Taiwanese software usage and this product's UI meaning. It does not claim to be
the single canonical rendering. Its purpose is consistency.

Three governing principles (shared with `zh-CN.md`):

1. **Keep the English a Taiwanese developer would type.** Brand names, protocol
   acronyms, service names and key legends stay in Latin script, and so do terms
   Taiwanese engineers normally write in Latin, such as `API`, `token`, `commit`
   and `prompt`. Translate ordinary nouns and verbs.
2. **Translate the sentence, not the words.** When a value is a sentence
   fragment, translate it for the sentence the user actually reads.
3. **One concept, one word, where practical.** Sense splits are fine when the
   English word carries unrelated senses (product `memory` vs RAM).

Authorities: W3C CLReq <https://www.w3.org/TR/clreq/>; 教育部《重訂標點符號手冊》;
國家教育研究院 樂詞網 <https://terms.naer.edu.tw/>.

---

## §1 Punctuation

- **Use full-width `，。：；？！（）、` between or beside CJK characters.** Keep
  half-width inside code: commands, paths, filenames, config keys, version
  numbers, URLs and token prefixes (`xoxb-`).
- **Quotes are corner brackets `「 」`, nested as `「…『…』…」`.** This is a real
  difference from `zh-CN`, which uses curly `“ ”`. A quoted English UI label keeps
  its English inside the brackets: `請使用「From Spec」分頁`.
- **Ellipsis** is the full-width `…`, attached to the preceding character:
  `正在安裝…`.
- **Never mix parenthesis widths in one value**, because `(…）` renders broken.
- **CJK ↔ Latin spacing**: put one ASCII space between a CJK character and an
  adjacent Latin letter or digit (`MCP 伺服器`, `第 3 輪`). Use no space next to
  full-width punctuation.
- **Trailing punctuation matches the English.** If the English has no `.`, the
  translation gets no `。`.
- **Em dash** `—` maps 1:1 to the English, with a space on each side. Never write
  `——`.
- **Menu paths** use `→` with spaces: `設定 → 聊天`.
- **Never store full-width Latin letters or digits** (`ＭＣＰ`, `３`). Enforced by
  `qa.test.ts` (`fullwidth-alphanumeric`).

---

## §2 Terminology

| English | Write | Avoid |
|---|---|---|
| settings | 設定 | 設置 |
| save | 儲存 | 保存 |
| user | 使用者 | 用戶 |
| software | 軟體 | 軟件 |
| server | 伺服器 | 服務器 |
| queue | 佇列 | 隊列 |
| workspace | 工作區 | 工作空間 |
| session | 工作階段 | 會話 |
| file / folder | 檔案 / 資料夾 | 文件 / 文件夾 |
| default | 預設 | 默認 |
| network | 網路 | 網絡 |
| create | 建立 | 創建 |
| run / running (process) | 執行 / 執行中 | 運行 |
| load / loading | 載入 / 載入中 | 加載 |
| link | 連結 | 鏈接 |
| data | 資料 | 數據 |
| message | 訊息 | 消息, 信息 |
| information | 資訊 | 信息 |
| account | 帳號 / 帳戶 | 賬號 / 賬戶 |
| log (noun) | 記錄 / 記錄檔 | 日誌 |
| cache | 快取 | 緩存 |
| port | 連接埠 | 端口 |
| thread (OS) | 執行緒 | 線程 |
| thread (chat) | 討論串 | 話題 |
| enable / disable | 啟用 / 停用 | 激活 / 禁用 |
| support (verb) | 支援 | 支持 |
| via | 透過 | 通過 |
| source code | 程式碼 | 代碼, 源代碼 |
| memory (RAM) | 記憶體 | 內存 |
| memory (product feature) | 記憶 | 記憶體 |
| screen | 螢幕 | 屏幕 |
| video / audio | 影片 / 音訊 | 視頻 / 音頻 |
| agent / subagent | 代理 / 子代理 | 智能體 |
| artifact | 產出物 | 產物, 工件 |
| cron job / schedule | 排程工作 / 排程 | 定時任務 |
| task | 任務 | |
| turn | 輪次 | 回合 |
| dashboard | 儀表板 | 控制台 |
| sidebar | 側邊欄 | 側欄 |
| pinned | 已釘選 | 已置頂 |
| provider | 供應商 | 提供商 |
| app | 應用程式 | 應用 (bare) |
| steering | 引導設定 | 轉向 |
| usage | 用量 | 使用情況 |
| tunnel | 通道 | 隧道 |
| instance | 執行個體 | 實例 |
| credentials | 憑證 | |
| permission | 權限 | |
| approve | 核准 | 批准 |

`代碼` itself is not wrong: Taiwan uses it for an identifier (`錯誤代碼`,
`語言代碼`). It is wrong only where the English means source code. `用戶端`
(client) and `租用戶` (tenant) are correct Taiwanese compounds.

**Measure words** are required where English uses a bare plural: `N 個檔案`,
`N 個工具`, `N 次執行`.

---

## §3 Do not translate

Product names in `glossary.json` (`dnt`) stay in Latin script. The same goes for
AWS service names, key legends (Enter, Shift, ⌘), `main`/`origin`/`HEAD`, paths,
filenames, config keys, and `cron` when it names the syntax (the feature itself is
排程). Enforced by `glossary.test.ts`.

---

## §4 Register and tone

- Address the user as **你**, not **您**. The product voice is casual.
- Button and menu labels are bare imperative verb-object, with no 請 and no
  trailing `。`.
- Drop `請` unless the English actually says "please".
- Never use `進行` + verb, never `如果…的話`, and never a translated `這將` (use
  `會`, or drop it).
- Avoid gratuitous `被` passives and stacks of three `的`.
- **Progressive**: `正在X…` for work in progress; `X中` only for short status
  chips (`執行中`).
- Keep *Resume* (繼續執行 / 恢復) and *Continue* (繼續) distinct, as
  `TRANSLATION-PROMPT.md` requires.

---

## §5 Plurals

Chinese has one CLDR plural category, **`other`**. A counted key carries only
`_other` in `zh-TW.json`. Enforced by `catalogParity.test.ts`.

---

## §6 What is mechanically enforced

| rule | gate |
|---|---|
| key parity, placeholders, no empty values, CLDR plurals | `catalogParity.test.ts` |
| balanced brackets, no full-width Latin, whitespace | `qa.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| changed values written in Han, not English passthrough | `check-source-strings.mjs` (`TARGET_SCRIPTS`) |
| destructive-confirm operands quoted with `「 」` | `destructiveConfirm.test.ts` |
| no Simplified-only characters | `zhTWStyle.test.ts` |
| none of the unambiguous Mainland forms in §2's "Avoid" column | `zhTWStyle.test.ts` |
| corner-bracket quotes, never curly, and balanced | `zhTWStyle.test.ts` |

Everything else in §1, §2 and §4 is review-only, because it needs a human to
judge it.
