# Traditional Chinese (Taiwan) style guide

Normative rules for `src/i18n/locales/zh-TW.json`. Where a rule is mechanically
checkable it is named alongside the test that enforces it; the rest are for whoever
reviews a translation PR.

**This catalog is not a script conversion of `zh-CN.json`.** That is the single
most important rule here, and it is a rule about *wording*, not about glyphs.
Converting 设置 to 設置 produces a Traditional-script string that a Taiwanese
reader still reads as foreign software — the word is 設定. Every value is
translated from the English source; `zh-CN.json` is consulted only as secondary
context, for what a key *means* in the product, never for how to say it. §2 is
the vocabulary that separates the two, and §2.1 is the list a reviewer greps for.

Three governing principles, inherited from `zh-CN.md` because they are properties
of the product rather than of the script:

1. **Keep the English a Taiwanese developer would type.** Brand names, protocol
   acronyms, service names and key legends stay in Latin script. Ordinary prose
   does not. Test: *would a Taiwanese engineer write this word in Latin letters in
   a design doc?* If yes, keep it. Taiwan keeps more of these than the Mainland
   does — `API`, `MCP`, `token`, `cache` are read in Latin — but the catalog still
   translates ordinary nouns and verbs.
2. **Translate the sentence, not the words.** Where a catalog value is a sentence
   *fragment*, translate for the sentence the user actually reads, not the
   fragment in isolation.
3. **One concept, one word — where practical.** A product noun should have a
   consistent Chinese rendering. Sense splits are fine when the English word
   carries unrelated senses (`memory` = product memory vs RAM).

Authorities cited:

- W3C CLReq — <https://www.w3.org/TR/clreq/>
- 教育部《重訂標點符號手冊》修訂版 (punctuation)
- 國家教育研究院 樂詞網 (terminology) — <https://terms.naer.edu.tw/>
- Mozilla L10n general style guide — <https://mozilla-l10n.github.io/styleguides/mozilla_general/>

---

## §1 Punctuation

Identical in mechanism to `zh-CN.md` §1 — the QA gates are shared — with two
Taiwan-specific differences, marked **TW**.

- **Full-width `，。：；？！（）、` between or beside CJK.** ASCII `,` or `.`
  between Chinese characters is the clearest signal a string was machine
  translated and never read.
- **Half-width is kept inside code**: commands, paths, filenames and extensions
  (`~/.kiro/crew`, `.yaml`), identifiers and config keys
  (`pref.backend.framework`), version numbers (`v1.2.3`), numeric ranges,
  URLs, emails and token prefixes (`xoxb-`).
- **Wrapper follows the sentence, content keeps its script**:
  `Piper 語速（length scale）`.
- **Ellipsis** for pending states is the full-width `…`, glued to the preceding
  character: `安裝中…`.
- **Parentheses** never mix styles within one value — a half-width opener
  married to a full-width closer renders as `(…）`. Both halves belong in the
  same key.
- **TW — quotes are corner brackets `「 」`**, nested as `「…『…』…」`. This is
  where Taiwan and the Mainland genuinely differ: `zh-CN.md` mandates curly
  `" "` and forbids `「 」`; Taiwan convention is the reverse, and the Ministry of
  Education handbook is explicit about it. A quoted English UI label keeps its
  English inside the brackets: `請使用「From Spec」分頁`.
- **CJK ↔ Latin spacing**: one ASCII space between a CJK character and an
  adjacent Latin letter, digit or `$`-prefixed number — `MCP 伺服器`, `第 3 輪`.
  No space between CJK and full-width punctuation, and none between two CJK
  characters. Same rule as `zh-CN`, kept identical on purpose: the QA gates and
  the reviewer's eye should not have to hold two spacing conventions.
- **Trailing punctuation matches the English.** If the English has no `.`, the
  Chinese gets no `。`.
- **Em dash** `—` is preserved 1:1 with the English, spaced on both sides.
  Never `——`, never `-`.
- **Menu paths** use `→` with spaces: `設定 → 聊天`.

### §1.1 Never store full-width Latin letters or digits

Full-width **punctuation** is correct; full-width **alphanumerics** are not. CLReq:
*"現今在文本儲存時，應避免使用該區段的拉丁字母及數字字符，交由排版引擎處理"*.

Write `MCP 伺服器 3 個`, never `ＭＣＰ伺服器３個`.

Checked by `qa.test.ts` → `fullwidth-alphanumeric` (gates outright at zero).

---

## §2 Terminology

The table below is the *product* vocabulary. §2.1 is the general software
vocabulary, and it is the one that decides whether this catalog reads as Taiwanese
software or as a converted Mainland catalog.

| English | Preferred | Avoid |
|---|---|---|
| session | 工作階段 | 会话/會話, 進程, 對話 |
| workspace | 工作區 | 工作空間, 工作台 |
| artifact | 產出物 | 工件, 製品, 產物 |
| agent / subagent | 代理 / 子代理 | 智慧體, 智能體 |
| skill | 技能 | 技巧 |
| cron job / scheduled job | 排程工作 | 定時任務, 計劃任務 |
| thread | 討論串 | 線程 (reads as OS thread), 話題 |
| turn | 輪次 | 回合 |
| message | 訊息 | 消息, 信息 |
| dashboard | 儀表板 | 儀表盤, 控制台 |
| sidebar | 側邊欄 | 側欄 |
| preferences | 偏好設定 | 偏好設置, 首選項 |
| pinned | 已釘選 | 已置頂, 已固定 |
| resolved | 已解決 | 已處理 |
| queue | 佇列 | 隊列, 排隊 |
| memory (product) | 記憶 | 內存, 記憶體 (that is RAM) |
| memory (RAM) | 記憶體 | 內存 |
| steering | 引導設定 | 操舵, 轉向 |
| usage | 用量 | 使用情況 |
| provider | 供應商 | 提供商, 提供者 |
| app | 應用程式 | 應用, 程序 |
| knowledge | 知識庫 | 知識 (bare, when it names the feature) |
| run (noun) | 執行紀錄 | 運行 |
| job | 工作 | 作業, 任務 (that is `task`) |

**Sense splits** (context-dependent, both are correct):

- `Jobs` (cron) 排程工作 vs `Task` 任務
- `Apply` 套用 vs `App` 應用程式
- `Settings` 設定 vs `Setup` 安裝設定
- `Show` 展開 vs `Display` 顯示
- `live` 即時 vs `Running` 執行中
- `Directory` 目錄路徑 vs `Contents` 目錄

**Measure words** are required where English uses a bare plural: `N 個檔案`,
`N 個工具`, `N 次執行`, `N 輪`.

### §2.1 Mainland wording, converted or not, is still Mainland wording

Left column is the trap: it is what a character conversion of `zh-CN.json`
produces. It is already Traditional script and it is still wrong.

Rows marked **gated** are checked by `zhTWStyle.test.ts`; the rest are review-only,
because they are homographs of correct Taiwanese words and a substring match on
them reports more false positives than defects. `代碼` is the clearest example:
Mainland for *source code*, but standard Taiwanese for an *identifier* (`錯誤代碼`,
`語言代碼`), so only the unambiguous compound `源代碼` is gated. `用戶` is gated, but
only after `租用戶` (tenant) and `用戶端` (client) are excluded — both are correct
Taiwanese. `文件` cannot be decided from the value at all: it means *document* in
Taiwan, so it is wrong only where the English said "file".

| Do not write | Write | gate |
|---|---|---|
| 設置 | 設定 | gated |
| 默認 | 預設 | gated |
| 軟件 | 軟體 | gated |
| 硬件 | 硬體 | gated |
| 代碼 | 程式碼 | review — only `源代碼` is gated |
| 內存 | 記憶體 | gated |
| 信息 (as "message") | 訊息 | gated |
| 文件夾 | 資料夾 | gated |
| 文件 (as "file") | 檔案 | review |
| 服務器 | 伺服器 | gated |
| 網絡 | 網路 | gated |
| 加載 | 載入 | gated |
| 用戶 | 使用者 | gated (excludes 租用戶 / 用戶端) |
| 創建 | 建立 | gated |
| 保存 | 儲存 | gated |
| 運行 | 執行 | gated |
| 隊列 | 佇列 | gated |
| 鏈接 | 連結 | gated |
| 視頻 | 影片 | gated |
| 音頻 | 音訊 | gated |
| 數據 | 資料 | gated |
| 日誌 | 紀錄 / 記錄檔 | gated |
| 賬號 / 賬戶 | 帳號 / 帳戶 | gated |
| 屏幕 | 螢幕 | gated |
| 打印 | 列印 | gated |
| 端口 | 連接埠 | gated |
| 進程 | 程序 (OS process) | review |
| 線程 | 執行緒 | gated |
| 緩存 | 快取 | gated |
| 質量 (as "quality") | 品質 | review |
| 優化 | 最佳化 | gated |
| 激活 | 啟用 | gated |
| 禁用 | 停用 | gated |
| 支持 (as "supports") | 支援 | review |
| 通過 (as "via") | 透過 | review |
| 提示 (as "prompt") | 提示詞 when it names the LLM prompt | review |

`文件` is the sharpest of these: in Taiwan it means a *document*, so
`文件不存在` for "file not found" reads as a different sentence. Use `檔案`.

---

## §3 Do not translate

Product names stay in Latin script. The list is in `glossary.json` under `dnt`:
`KiroCrew` / `Kiro Crew`, `Kiro`, `Slack`, `Discord`, `MCP`, `GitHub`, `Playwright`, etc.

Also stays in English: AWS service names, key legends (Enter, Shift, ⌘),
`main`/`origin`/`HEAD`, paths, filenames, config keys, and `cron` (the syntax —
the feature is 排程工作).

Checked by `glossary.test.ts`.

---

## §4 Register and tone

- Address the user as **你**, not **您**. The product voice is casual — same
  choice as `zh-CN`, and it is the ordinary register for Taiwanese developer
  tools.
- Button and menu labels are bare imperative verb-object — no 請, no trailing `。`.
- Drop `請` unless the English actually says "please".
- Prefer omitting the subject over `你的` when ownership is obvious.
- Never `進行` + verb (`進行設定檢查` → `檢查安裝狀態`).
- **At most two `的` per clause** — three is genitive stacking.
- Never `如果…的話`; never a translated `這將` (use `會` or drop it).
- `該` as demonstrative → `此`, but `該` as modal "should" (`應該`) stays.
- Avoid gratuitous `被` passive; prefer active or topic-comment.
- **Progressive**: `正在X…` for work in progress (`正在安裝…`); `X中` only for
  short status chips (`執行中`).
- Prefer the Taiwanese verb-object idiom over a Mainland calque: `按一下` for a
  UI click in running prose, `點` only in tight chrome where width matters.

---

## §5 Plurals

Chinese has exactly one CLDR plural category: **`other`**. A counted key uses
`_one` + `_other` in `en.json` and **only `_other`** in `zh-TW.json`. Emitting
`_one` for zh-TW creates a form i18next can never select.

Checked by `catalogParity.test.ts`.

---

## §6 Known gap — sentence fragments

Inherited from the English corpus, not introduced here: the extraction codemod
converted plain string literals, so a JSX sentence containing a variable became
several independently translated keys. Translate fragments for the *rendered*
sentence. New copy must not add fragments: one key per sentence.

---

## §7 What is mechanically enforced

| rule | gate |
|---|---|
| balanced brackets and quotes, incl. mixed width | `qa.test.ts` |
| no full-width Latin or digits | `qa.test.ts` |
| no leading/trailing space, no doubled space | `qa.test.ts` |
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (1: other) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| no Simplified-only characters | `zhTWStyle.test.ts` |
| no §2.1 Mainland wording | `zhTWStyle.test.ts` |
| corner-bracket quotes, not curly | `zhTWStyle.test.ts` |

`zhTWStyle.test.ts` is what makes the "not a script conversion" rule a gate rather
than a sentence: the Simplified-character check catches an unconverted paste, and
the §2.1 wordlist catches the much likelier failure of a *converted* one. Everything
in §1 beyond what QA catches, and everything in §4, is review-only — the judgements
a human has to make.
