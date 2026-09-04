# Japanese style guide

Normative rules for `src/i18n/locales/ja.json`. Where a rule is mechanically
checkable it is named alongside the test that enforces it; the rest are for whoever
reviews a translation PR.

Three governing principles:

1. **Keep the English a Japanese developer would type.** Brand names, protocol
   acronyms, service names and key legends stay in Latin script. Ordinary prose
   does not. Test: *would a Japanese engineer write this word in Latin letters in
   a design doc?* If yes, keep it.
2. **Translate the sentence, not the words.** Where a catalog value is a
   sentence *fragment*, translate for the sentence the user actually reads, not
   the fragment in isolation. Japanese word order rarely survives a fragment
   boundary — `present,` renders after its count, and a literal `プレゼント`
   ("a gift") is what fragment-blind translation produces.
3. **One concept, one word.** A product noun gets one Japanese rendering.
   Sense splits are fine when the English word carries unrelated senses
   (`memory` = product memory vs RAM).

Authorities cited:

- JTF 日本語標準スタイルガイド（翻訳用）— <https://www.jtf.jp/tips/styleguide>
- W3C JLReq (日本語組版処理の要件) — <https://www.w3.org/TR/jlreq/>
- Mozilla L10n general style guide — <https://mozilla-l10n.github.io/styleguides/mozilla_general/>

---

## §1 Punctuation

- **Full-width `、。（）「」！？` in Japanese prose.** An ASCII `,` or `.` between
  Japanese characters is the clearest signal a string was machine translated and
  never read.
- **Half-width is kept inside code**: commands, paths, filenames and extensions
  (`~/.kiro/crew`, `.ics`), identifiers and config keys (`pref.backend.framework`),
  version numbers (`v1.2.3`), numeric ranges, URLs, emails and token prefixes
  (`xoxb-`). A `?` opening a URL query and a `!` inside a shell snippet are code,
  not punctuation.
- **Quotation is `「」`**, nested as `『』`. Do not carry the English `“ ”` over.
  This applies to an interpolated value too: `「{{cmd}}」を信頼`, never
  `"{{cmd}}"を信頼` — the same action quoted two ways is visible to one user in
  one session.
- **Keep the dash the English uses.** Where the source has `—`, Japanese carries
  `—`, not an ASCII `-`. A hyphen inside a compound or a Markdown bullet is not a
  dash and is left alone.
- **Ellipsis** for pending states is `…`, glued to the preceding character:
  `インストール中…`. Never `...`.
- **No ideographic space (`U+3000`).** Layout is the stylesheet's job.

Checked by `jaStyle.test.ts` (ASCII comma, ASCII ellipsis, ASCII `!`/`?`,
ideographic space) and by `qa.test.ts` (bracket balance, no full-width Latin or
digits, no leading/trailing or doubled space).

### §1.1 Spacing at a script boundary

Put a half-width space between a Latin run and adjacent Japanese
(`Kiro Crew を起動`), except where the Latin run is the head of a compound the
language has absorbed (`MCPサーバー`). This one is **review-only**: the boundary
is a judgement call and a mechanical rule would fire on every absorbed compound.

---

## §2 Katakana

- **Compound loanwords are closed up, never spaced.** `ナレッジライブラリ`, not
  `ナレッジ ライブラリ`; `コードレビュー`, not `コード レビュー`. A space inside a
  katakana run is the Microsoft-style convention and this catalog does not use it —
  `サブエージェント` and `ワークフロー` set the pattern.
- **Keep the trailing long-vowel mark.** `サーバー`, `ユーザー`, `ブラウザー`,
  `フォルダー`, `コンピューター` — the JTF convention, not the older JIS truncation
  (`サーバ`, `フォルダ`). Words already ending in a long vowel are unaffected.
- **`・` separates enumerated items**, not the halves of one compound noun.

Checked by `jaStyle.test.ts`.

---

## §2.1 Terminology

One concept, one word. A katakana transliteration is the wrong choice when the
resulting string is ambiguous in Japanese — `write access` → `ライトアクセス`
reads as "light" or "right" access far sooner than "write".

| English | use | never |
|---|---|---|
| write access | 書き込みアクセス | ライトアクセス |
| present (attending) | 在席 | プレゼント |
| open (a free slot) | 空き | 開く |

Checked by `jaStyle.test.ts`, which only judges a key whose **English** carries
the cue — so a legitimate `ライト` ("light theme") is never caught.

---

## §3 Do not translate

Product names stay in Latin script. The list is in `glossary.json` under `dnt`:
`KiroCrew` / `Kiro Crew`, `Kiro`, `Slack`, `Discord`, `MCP`, `GitHub`, `Playwright`, etc.

Also stays in English: AWS service names, key legends (Enter, Shift, ⌘),
`main`/`origin`/`HEAD`, paths, filenames, config keys, and `cron` (the syntax —
the feature is 定期実行).

Checked by `glossary.test.ts`.

---

## §4 Register and tone

- **です・ます throughout.** Never だ・である in UI copy — the register shift is
  jarring next to the surrounding sentences.
- **Kana, not kanji, for auxiliaries and formal words**: `ください` (not `下さい`),
  `できる` (not `出来る`), `ある`/`いる` (not `有る`/`居る`), `こと` (not `事`).
- Button and menu labels are 体言止め — a bare noun phrase with no trailing `。`
  (`新規セッション`, `すべて既読にする`). A trailing `。` on a label is a bug.
- A descriptive caption may also be 体言止め (`アクティビティなし。`); that is not
  a register violation, it is the standard form for a UI description.
- Drop `〜してください` unless the English actually says "please".
- Prefer omitting the subject over `あなたの` when ownership is obvious.
- Avoid the `〜を行う` circumlocution (`設定の確認を行う` → `設定を確認`).
- Avoid gratuitous `〜されます` passive; prefer active or topic-comment.
- **Progressive**: `〜しています…` or `〜中…` for work in progress (`接続中…`).

The だ・である ban and the kanji-auxiliary list are checked by `jaStyle.test.ts`;
the rest is review-only.

---

## §5 Plurals

Japanese has exactly one CLDR plural category: **`other`**. A counted key uses
`_one` + `_other` in `en.json` and **only `_other`** in `ja.json`. Emitting `_one`
for ja creates a form i18next can never select.

Checked by `catalogParity.test.ts`.

---

## §6 Known gap — sentence fragments

The extraction codemod converted plain string literals, so a JSX sentence
containing a variable became several independently translated keys, pinning
Japanese to English clause order. Japanese suffers more than most: the language
puts the operand before the predicate, so a `prefix + {value} + suffix` triple
cannot be reordered at all.

Fixing requires recomposing each sentence into one key with `<Trans>` or
`{{named}}` interpolation — `components.trustDropdown.trust_this_command` is the
worked example. Until then, translate fragments for the *rendered* sentence. New
copy must not add fragments: one key per sentence.

---

## §7 What is mechanically enforced

| rule | gate |
|---|---|
| no ASCII `,` / `...` / `!` / `?` after Japanese | `jaStyle.test.ts` |
| em dash kept where the English has one | `jaStyle.test.ts` |
| interpolated values quoted with 「」 | `jaStyle.test.ts` |
| no ideographic space | `jaStyle.test.ts` |
| no space inside a katakana compound | `jaStyle.test.ts` |
| trailing long-vowel mark kept | `jaStyle.test.ts` |
| no だ・である ending, no kanji auxiliaries | `jaStyle.test.ts` |
| balanced brackets and quotes, incl. mixed width | `qa.test.ts` |
| no full-width Latin or digits | `qa.test.ts` |
| no leading/trailing space, no doubled space | `qa.test.ts` |
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (1: other) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |

Everything in §1.1, §3 and the rest of §4 is review-only — the judgements a human
has to make.
