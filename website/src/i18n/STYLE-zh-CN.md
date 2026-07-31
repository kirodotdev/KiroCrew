# Simplified-Chinese style guide (`zh-CN`)

Normative. `zhStyle.test.ts` enforces the mechanical rules here, so a violation
fails CI rather than accumulating. Section numbers below are the ones the test
names cite.

Three governing principles:

1. **One concept, one word.** A product noun gets exactly one Chinese rendering
   across the whole catalog. A sense split is allowed only where the English
   word itself carries two unrelated senses (`memory` = product memory vs RAM),
   and every split is listed in §2.
2. **Keep the English a Chinese developer would type.** Brand names, protocol
   acronyms, service names and key legends stay in Latin script. Ordinary prose
   does not. Test: *would a Chinese engineer write this word in Latin letters in
   a design doc?* If yes, keep it.
3. **Translate the sentence, not the words.** Where a catalog value is a
   sentence *fragment*, translate for the sentence the user actually reads, not
   the fragment in isolation.

---

## §1 Punctuation

- **Full-width `，。：；？！（）、` between or beside CJK.** ASCII `,` or `.`
  between Chinese characters is the clearest signal a string was machine
  translated and never read.
- **Half-width is kept inside code**: commands, paths, filenames and extensions
  (`~/.kiro/crew`, `.yaml`), identifiers and config keys
  (`pref.backend.framework`), version numbers (`v1.2.3`), numeric ranges,
  URLs, emails and token prefixes (`xoxb-`). `stripCode()` in the test mirrors
  this carve-out list.
- **Wrapper follows the sentence, content keeps its script**:
  `Piper 语速（length scale）`.
- **Ellipsis** for pending states is the full-width `…`, glued to the preceding
  character: `正在安装…`. An ellipsis meaning *omission* inside a code sample
  stays as authored.
- **Parentheses** never mix styles within one value — a half-width opener
  married to a full-width closer renders as `(…）`. A sentence fragment may
  legitimately open a bracket it never closes; only a mixed-style imbalance is
  a defect.
- **Quotes** are curly `“ ”`. Corner brackets `「 」` are not used. A quoted
  English UI label keeps its English inside Chinese quotes:
  `请使用“From Spec”标签页`.
- **CJK ↔ Latin spacing**: one ASCII space between a CJK character and an
  adjacent Latin letter, digit or `$`-prefixed number — `MCP 服务器`, `第 3 轮`.
  No space between CJK and full-width punctuation, and none between two CJK
  characters.
- **Trailing punctuation matches the English.** A label followed by a value
  ends in `：` and nothing else. If the English has no `.`, the Chinese gets no
  `。`.
- **Em dash** `—` is preserved 1:1 with the English, spaced on both sides.
  Never `——`, never `-`.
- **Menu paths** use `→` with spaces: `设置 → 聊天`.

## §2 Terminology

One canonical rendering per concept. The enforced subset:

| English | Canonical | Never |
|---|---|---|
| session | 会话 | 进程, 对话 |
| workspace | 工作区 | 工作空间, 工作台 |
| artifact | 工件 | 制品, 产物 |
| agent / subagent | 代理 / 子代理 | 智能体, 子智能体 |
| skill | 技能 | 技巧 |
| cron job / scheduled job | 定时任务 | 计划任务 |
| thread | 话题 | 线程 (reads as an OS thread) |
| turn | 轮次 | 回合 |
| message | 消息 | 信息 |
| dashboard | 仪表板 | 仪表盘, 控制台 |
| sidebar | 侧边栏 | 侧栏 |
| preferences | 偏好设置 | 偏好 |
| usage | 用量 | 使用量 |
| pinned | 已置顶 | 已固定 |
| resolved | 已解决 | 已处理 |
| reject / rejected | 拒绝 / 已拒绝 | 驳回 |
| effort (reasoning) | 强度 | 投入度 |
| WeCom | 企业微信 | WeCom |

**Stays in English**: KiroCrew, Kiro, Slack, Discord, Telegram, Webex,
Microsoft Teams, MCP, ACP, API, SDK, CLI, URL, CI, IAM, ARN, PR, cron (the
syntax — the feature is 定时任务), AWS service names, Playwright/Vite/React/
TypeScript, key legends (Enter, Shift, ⌘), `main`/`origin`/`HEAD`, and paths,
filenames and config keys. `issue` stays English for the GitHub object in Issue
Radar; it becomes 问题 when it means a generic problem.

**Sense splits that must not be collapsed**: `Jobs` (cron) 定时任务 vs `Task`
任务 · `Apply` 应用更改 vs `App` 应用 · `Set` 设定 vs `Setup` 安装设置 vs
`Settings` 设置 · `Show` 展开 vs `Display` 显示 · `Done` 已完成 vs `Finished`
已结束 · `live` 实时 vs `Running` 运行中 · `Directory` 目录路径 vs `Contents`
目录 · `Origin:` 安装来源： vs `Sources:` 引用来源：.

**Measure words** are required where English uses a bare plural: `N 个文件`,
`N 个工具`, `N 个令牌`, `N 次运行`, `N 次工具调用`, `N 轮`, `N 个定时任务`.

**Progressive aspect**: `正在X…` for work the system is doing (`正在安装…`);
`X中` only for short status chips in tables and badges (`运行中`, `失败中`).

## §3 Tone

Neutral-technical, second person 你, imperative for actions.

- Button and menu labels are bare imperative verb-object — no 请, no trailing `。`.
- **Never 您.** The catalog is uniformly 你; mixing registers reads worse than
  either one consistently.
- Drop `请` unless the English actually says "please". `请` also *starts*
  ordinary words (请求 = request, 请勿 = do not) — never split those.
- Prefer omitting the subject over `你的` when ownership is obvious.
- Never `进行` + verb (`进行设置检查` → `检查安装状态`).
- **At most two `的` per clause**; three is genitive stacking and always has a
  shorter native phrasing.
- Never `如果…的话`; never a translated `这将` (use `会` or drop it).
- `该` as a demonstrative becomes `此` — but `该` is also the modal "should"
  (`我该用`, `应该`), which must be left alone.
- Avoid gratuitous `被` passive; prefer active or topic-comment.

## §4 Plural forms

Chinese has exactly one CLDR plural category: **`other`**. So a counted key is
`key_one` + `key_other` in `en.json` and **only `key_other`** in `zh-CN.json`.
Emitting `key_one` for `zh-CN` creates a form i18next can never select, and
makes the catalog look like it handles counting when it cannot.

A key merely *ending* in `_one` because its English sentence ends with the word
"one" (`click_new_to_create_one`) is a slug artifact, not a plural form.

## §5 Known gap — sentence fragments

The extraction codemod converted plain string literals, so a JSX sentence
containing a variable or an inline element became **several** independently
translated keys. 244 sentences are currently split across 417 keys, which pins
the Chinese to English clause order because the slots are fixed by the JSX.

Fixing this requires recomposing each sentence into one key and rendering it
with `<Trans>` (named components, not indexed `<0>`) or `{{named}}`
interpolation, so a translator can reorder freely. Until a fragment group is
recomposed, translate its pieces for the *rendered* sentence and accept that
the ordering cannot be fully fixed within the fragment.

New user-visible copy must not add fragments: keep one key per sentence.
