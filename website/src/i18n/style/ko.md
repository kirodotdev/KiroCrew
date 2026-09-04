# Korean style guide

Normative rules for `src/i18n/locales/ko.json`. Where a rule is mechanically
checkable it is named alongside the test that enforces it; the rest are for whoever
reviews a translation PR.

Three governing principles:

1. **Keep the English a Korean developer would type.** Brand names, protocol
   acronyms, service names and key legends stay in Latin script. Ordinary prose
   does not. Test: *would a Korean engineer write this word in Latin letters in a
   design doc?* If yes, keep it.
2. **Translate the sentence, not the words.** Where a catalog value is a sentence
   *fragment*, translate for the sentence the user actually reads, not the
   fragment in isolation. Korean puts the predicate last, so a
   `prefix + {value} + suffix` triple cannot be reordered inside its own key.
3. **One concept, one word.** A product noun gets one Korean rendering. Sense
   splits are fine when the English word carries unrelated senses (`memory` =
   product memory vs RAM).

Authorities cited:

- 국립국어원 한글 맞춤법 · 문장부호 — <https://kornorms.korean.go.kr/>
- 국립국어원 외래어 표기법 — <https://kornorms.korean.go.kr/regltn/regltnView.do?regltn_code=0003>
- Mozilla L10n general style guide — <https://mozilla-l10n.github.io/styleguides/mozilla_general/>

---

## §1 Punctuation

Korean is the **opposite of Japanese and Chinese here**: 한글 맞춤법 prescribes
half-width `.` `,` `?` `!` even between Hangul characters. Full-width
`。 ， 、 ！ ？ （ ）` in a Korean string is a sign the value was carried over from
`ja.json` or `zh-CN.json` and never re-read.

- **Half-width `. , ? !` in Korean prose**, with a following space inside a
  sentence and none before.
- **Ellipsis is `…`**, glued to the preceding character: `설치 중…`. Never `...`.
- **Quotation is `‘ ’`**, nested as `“ ”`. Use it for an interpolated operand too:
  `‘{{cmd}}’ 신뢰`, never `"{{cmd}}" 신뢰` — the same action quoted two ways is
  visible to one user in one session.
- **Keep the dash the English uses.** Where the source has `—`, Korean carries
  `—`, not an ASCII `-`. A hyphen inside a compound or a Markdown bullet is not a
  dash and is left alone.
- **Enumerated nouns are joined with `·`** (가운뎃점), not `、` and not `/`.
- **No ideographic space (`U+3000`).** Layout is the stylesheet's job.
- **Half-width stays inside code**: commands, paths, filenames and extensions
  (`~/.kiro/crew`, `.ics`), identifiers and config keys
  (`pref.backend.framework`), version numbers (`v1.2.3`), numeric ranges, URLs,
  emails and token prefixes (`xoxb-`).

Checked by `koStyle.test.ts` (full-width punctuation, ASCII ellipsis, quote pair,
ideographic space) and by `qa.test.ts` (bracket balance, no full-width Latin or
digits, no leading/trailing or doubled space).

### §1.1 Hangul-only

**No 한자 and no kana.** Korean UI copy is written in Hangul; a CJK ideograph or a
kana character in `ko.json` is either an untranslated Japanese value or a
Chinese one, not a stylistic choice. This is the one rule that makes the
Japanese-source translation pipeline safe to run — it fails on a leaked source
value rather than shipping it.

Checked by `koStyle.test.ts`.

---

## §2 Spacing (띄어쓰기)

Korean spacing is grammatical, not typographic, and it is where a machine
translation shows first.

- **의존명사 takes a space**: `할 수 있습니다`, not `할수 있습니다`; `한 개`, not
  `한개`.
- **A 조사 attaches with no space**, including to a Latin run: `Slack에서`,
  `GitHub을`, `MCP를`. Never `Slack 에서`. A 접미사 (`Transcribe용`), a 단위명사 after
  a numeral (`N개`) and a 하다/되다 conjugation on a Latin stem (`POST할 수 있습니다`)
  all bind the same way.
- **A Latin run and the Hangul that follows it take a space** when that Hangul is
  a noun rather than a 조사: `MCP 서버`, `Git 저장소`, `Kiro Crew 실행`. Never
  `MCP서버`. A 단위명사 after a numeral is the documented exception and stays
  closed up (`3개`, `2번`).

The 의존명사 list, the 조사/noun boundary and the ideographic space are checked by
`koStyle.test.ts`. The rest of 띄어쓰기 is **review-only** — the general rule needs
a parser, and a regex approximation would fire on correct copy.

### §2.1 Placeholder 조사

A 조사 whose form depends on the preceding syllable's final consonant cannot be
chosen when the preceding token is an interpolation. Write **both forms**:

| after `}}` | write | never |
|---|---|---|
| 을/를 | `을(를)` | `를` |
| 이/가 | `이(가)` | `가` |
| 은/는 | `은(는)` | `는` |
| 와/과 | `와(과)` | `와` |
| (으)로 | `(으)로` | `로` |

`{{name}}이(가) 완료되었습니다.` — a fixed `가` renders `세션가` for half the
values that reach it.

Prefer restructuring over the parenthesis where the sentence allows it
(`완료: {{name}}`), but never guess the 조사.

**A fragment must not OPEN with a 조사.** Where a component renders
`<operand> <fragment>` it always puts a space between the two, so a value
beginning with a particle draws it detached from its noun — `Kiro Crew 을(를)
제거하고`. That is an orthography error, and the space is not the catalog's to
remove. Omit the particle (`Issue Radar에서 제거하고`), or lead with an em dash
where omission alone would be ambiguous (`— 게이트웨이 호스트에서 인증되지
않았습니다`).

Both checked by `koStyle.test.ts`.

---

## §2.2 Terminology

One concept, one word. 외래어 표기법 settles the spelling; where industry usage
and the 표기법 disagree, this table is the decision.

| English | use | never |
|---|---|---|
| message | 메시지 | 메세지 |
| thread | 스레드 | 쓰레드 |
| content | 콘텐츠 | 컨텐츠 |
| data | 데이터 | 데이타 |
| application | 애플리케이션 | 어플리케이션 |
| schedule | 스케줄 | 스케쥴 |
| cache | 캐시 | 캐쉬 |
| browser | 브라우저 | 브라우져 |
| access | 액세스 | 억세스 |
| workflow | 워크플로 | 워크플로우 |
| user | 사용자 | 유저 |
| write access | 쓰기 권한 | 라이트 액세스 |
| present (attending) | 참석 | 선물 |
| open (a free slot) | 빈 시간 | 열기 |

The 외래어 spellings are checked unconditionally by `koStyle.test.ts`; the last
three are checked only where the **English** carries the cue, so a legitimate
`열기` ("Open a file") is never caught.

---

## §3 Do not translate

Product names stay in Latin script. The list is in `glossary.json` under `dnt`:
`KiroCrew` / `Kiro Crew`, `Kiro`, `Slack`, `Discord`, `MCP`, `GitHub`, `Playwright`, etc.

Also stays in English: AWS service names, key legends (Enter, Shift, ⌘),
`main`/`origin`/`HEAD`, paths, filenames, config keys, and `cron` (the syntax —
the feature is 정기 실행).

Checked by `glossary.test.ts`.

---

## §4 Register and tone

- **합니다체 for statements, `~하세요` for instructions.** `저장되었습니다.`,
  `다시 시도하세요.` Never 한다체 (`저장된다`) or 해체 (`저장했어`) — the register
  shift is jarring next to the surrounding sentences.
- **No 이중 피동.** `되어집니다`, `보여집니다`, `쓰여집니다` are two passives
  stacked; write `됩니다`, `표시됩니다`, `작성됩니다`.
- Button and menu labels are 명사형 with no trailing `.` (`새 세션`,
  `모두 읽음으로 표시`). A trailing `.` on a label is a bug.
- A descriptive caption may be 명사형 too (`활동 없음`); that is not a register
  violation, it is the standard form for a UI description.
- Drop `~해 주세요` unless the English actually says "please".
- **Do not mark plurals with `~들`.** Korean nouns are number-neutral;
  `파일들을 선택` is a translation artifact of English `files`.
- Prefer omitting the subject over `당신의` when ownership is obvious.
- Avoid the `~을 수행합니다` circumlocution (`설정 확인을 수행합니다` →
  `설정을 확인합니다`).
- **Progressive**: `~하고 있습니다` or `~ 중…` for work in progress (`연결 중…`).
- `~십시오`, never `~십시요`. After a consonant stem the ending is `습니다`, never
  the pre-1988 `읍니다` (`있습니다`, not `있읍니다`) — but after a stem ending in ㅡ,
  `읍니다` is correct and expected (모으다 → `모읍니다`).

### §4.1 Destructive confirmations are 하십시오체

A dialog that asks the user to consent to losing something ends in
**`~하시겠습니까?`**, never `~할까요?`. `삭제할까요?` and `삭제하시겠습니까?` on two
dialogs for the same operation is the drift a user meets inside one session, and
the softer form reads as a suggestion at the exact moment the copy has to read as
a warning.

`~할까요?` stays correct for a **non-destructive offer** — `무엇을 조사할까요?`,
`이전 채팅을 계속할까요?`. These are different speech acts, and Korean marks the
difference; flattening both into one form would be its own defect.

Checked by `koStyle.test.ts`, which judges only keys whose **English** is a
question carrying a destructive verb, so an offer is never caught.

The 한다체 ban, the 이중 피동 list, the `~십시요`/`읍니다` misspellings and the
label full stop are checked by `koStyle.test.ts`; the rest is review-only.

---

## §5 Plurals

Korean has exactly one CLDR plural category: **`other`**. A counted key uses
`_one` + `_other` in `en.json` and **only `_other`** in `ko.json`. Emitting `_one`
for ko creates a form i18next can never select.

Checked by `catalogParity.test.ts`.

---

## §6 Known gap — sentence fragments

The extraction codemod converted plain string literals, so a JSX sentence
containing a variable became several independently translated keys, pinning
Korean to English clause order. Korean suffers as much as Japanese: the predicate
comes last, so a `prefix + {value} + suffix` triple cannot be reordered at all.

Fixing requires recomposing each sentence into one key with `<Trans>` or
`{{named}}` interpolation — `components.trustDropdown.trust_this_command` is the
worked example. Until then, translate fragments for the *rendered* sentence. New
copy must not add fragments: one key per sentence.

---

## §7 What is mechanically enforced

| rule | gate |
|---|---|
| no full-width `。，、！？（）` | `koStyle.test.ts` |
| no ASCII `...` after Hangul | `koStyle.test.ts` |
| interpolated values quoted with ‘ ’ | `koStyle.test.ts` |
| no ideographic space | `koStyle.test.ts` |
| no 한자 and no kana | `koStyle.test.ts` |
| 의존명사 spaced, Latin/noun boundary spaced | `koStyle.test.ts` |
| placeholder 조사 written in both forms | `koStyle.test.ts` |
| a fragment never opens with a 조사 | `koStyle.test.ts` |
| 외래어 표기법 spellings | `koStyle.test.ts` |
| no 한다체 ending, no 이중 피동 | `koStyle.test.ts` |
| destructive confirmations end in `~하시겠습니까?` | `koStyle.test.ts` |
| balanced brackets and quotes, incl. mixed width | `qa.test.ts` |
| no full-width Latin or digits | `qa.test.ts` |
| no leading/trailing space, no doubled space | `qa.test.ts` |
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (1: other) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |

Everything in §2 beyond the checked list, §3 and the rest of §4 is review-only —
the judgements a human has to make.
