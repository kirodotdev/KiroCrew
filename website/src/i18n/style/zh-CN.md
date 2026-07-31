# Simplified Chinese style guide

Normative rules for `src/i18n/locales/zh-CN.json`. Where a rule is mechanically
checkable it is named alongside the test that enforces it; the rest are for whoever
reviews a translation PR.

Three governing principles:

1. **Keep the English a Chinese developer would type.** Brand names, protocol
   acronyms, service names and key legends stay in Latin script. Ordinary prose
   does not. Test: *would a Chinese engineer write this word in Latin letters in
   a design doc?* If yes, keep it.
2. **Translate the sentence, not the words.** Where a catalog value is a
   sentence *fragment*, translate for the sentence the user actually reads, not
   the fragment in isolation.
3. **One concept, one word — where practical.** A product noun should have a
   consistent Chinese rendering. Sense splits are fine when the English word
   carries unrelated senses (`memory` = product memory vs RAM).

Authorities cited:

- W3C CLReq — <https://www.w3.org/TR/clreq/>
- GB/T 15834—2011 (punctuation standard)
- Mozilla L10n general style guide — <https://mozilla-l10n.github.io/styleguides/mozilla_general/>

---

## §1 Punctuation

- **Full-width `，。：；？！（）、` between or beside CJK.** ASCII `,` or `.`
  between Chinese characters is the clearest signal a string was machine
  translated and never read.
- **Half-width is kept inside code**: commands, paths, filenames and extensions
  (`~/.kiro/crew`, `.yaml`), identifiers and config keys
  (`pref.backend.framework`), version numbers (`v1.2.3`), numeric ranges,
  URLs, emails and token prefixes (`xoxb-`).
- **Wrapper follows the sentence, content keeps its script**:
  `Piper 语速（length scale）`.
- **Ellipsis** for pending states is the full-width `…`, glued to the preceding
  character: `正在安装…`.
- **Parentheses** never mix styles within one value — a half-width opener
  married to a full-width closer renders as `(…）`. Both halves belong in the
  same key.
- **Quotes** are curly `" "`. Corner brackets `「 」` are not used. A quoted
  English UI label keeps its English inside Chinese quotes:
  `请使用"From Spec"标签页`.
- **CJK ↔ Latin spacing**: one ASCII space between a CJK character and an
  adjacent Latin letter, digit or `$`-prefixed number — `MCP 服务器`, `第 3 轮`.
  No space between CJK and full-width punctuation, and none between two CJK
  characters.
- **Trailing punctuation matches the English.** If the English has no `.`, the
  Chinese gets no `。`.
- **Em dash** `—` is preserved 1:1 with the English, spaced on both sides.
  Never `——`, never `-`.
- **Menu paths** use `→` with spaces: `设置 → 聊天`.

### §1.1 Never store full-width Latin letters or digits

Full-width **punctuation** is correct; full-width **alphanumerics** are not. CLReq:
*"现今在文本储存时，应避免使用该区段的拉丁字母及数字字符，交由排版引擎处理"*.

Write `MCP 服务器 3 个`, never `ＭＣＰ服务器３个`.

Checked by `qa.test.ts` → `fullwidth-alphanumeric` (gates outright at zero).

---

## §2 Terminology

Preferred renderings for product concepts. These are conventions, not CI gates —
a reviewer should check consistency but context may require variation.

| English | Preferred | Avoid |
|---|---|---|
| session | 会话 | 进程, 对话 |
| workspace | 工作区 | 工作空间, 工作台 |
| artifact | 工件 | 制品, 产物 |
| agent / subagent | 代理 / 子代理 | 智能体, 子智能体 |
| skill | 技能 | 技巧 |
| cron job / scheduled job | 定时任务 | 计划任务 |
| thread | 话题 | 线程 (reads as OS thread) |
| turn | 轮次 | 回合 |
| message | 消息 | 信息 |
| dashboard | 仪表板 | 仪表盘, 控制台 |
| sidebar | 侧边栏 | 侧栏 |
| preferences | 偏好设置 | 偏好 |
| pinned | 已置顶 | 已固定 |
| resolved | 已解决 | 已处理 |

**Sense splits** (context-dependent, both are correct):

- `Jobs` (cron) 定时任务 vs `Task` 任务
- `Apply` 应用更改 vs `App` 应用
- `Settings` 设置 vs `Setup` 安装设置
- `Show` 展开 vs `Display` 显示
- `live` 实时 vs `Running` 运行中
- `Directory` 目录路径 vs `Contents` 目录

**Measure words** are required where English uses a bare plural: `N 个文件`,
`N 个工具`, `N 次运行`, `N 轮`.

---

## §3 Do not translate

Product names stay in Latin script. The list is in `glossary.json` under `dnt`:
`KiroCrew`, `Kiro`, `Slack`, `Discord`, `MCP`, `GitHub`, `Playwright`, etc.

Also stays in English: AWS service names, key legends (Enter, Shift, ⌘),
`main`/`origin`/`HEAD`, paths, filenames, config keys, and `cron` (the syntax —
the feature is 定时任务).

Checked by `glossary.test.ts`.

---

## §4 Register and tone

- Address the user as **你**, not **您**. The product voice is casual.
- Button and menu labels are bare imperative verb-object — no 请, no trailing `。`.
- Drop `请` unless the English actually says "please".
- Prefer omitting the subject over `你的` when ownership is obvious.
- Never `进行` + verb (`进行设置检查` → `检查安装状态`).
- **At most two `的` per clause** — three is genitive stacking.
- Never `如果…的话`; never a translated `这将` (use `会` or drop it).
- `该` as demonstrative → `此`, but `该` as modal "should" (`应该`) stays.
- Avoid gratuitous `被` passive; prefer active or topic-comment.
- **Progressive**: `正在X…` for work in progress (`正在安装…`); `X中` only for
  short status chips (`运行中`).

---

## §5 Plurals

Chinese has exactly one CLDR plural category: **`other`**. A counted key uses
`_one` + `_other` in `en.json` and **only `_other`** in `zh-CN.json`. Emitting
`_one` for zh-CN creates a form i18next can never select.

Checked by `catalogParity.test.ts`.

---

## §6 Known gap — sentence fragments

The extraction codemod converted plain string literals, so a JSX sentence
containing a variable became several independently translated keys. 244 sentences
are currently split across 417 keys, pinning Chinese to English clause order.

Fixing requires recomposing each sentence into one key with `<Trans>` or
`{{named}}` interpolation. Until then, translate fragments for the *rendered*
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

Everything in §1 (beyond what QA catches), §2 and §4 is review-only — the
judgements a human has to make.
