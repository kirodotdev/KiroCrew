# Hindi style guide

Normative rules for `src/i18n/locales/hi.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- W3C Indic Layout Requirements — <https://www.w3.org/TR/ilreq/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>
- Mozilla L10n general style guide — <https://mozilla-l10n.github.io/styleguides/mozilla_general/>

---

## 1. Punctuation

Devanagari has its own sentence-ending punctuation:

| use | not | note |
|---|---|---|
| `।` (purna viram U+0964) | `.` | sentence-final only |
| `,` | — | comma is the same Latin comma |
| `?` | — | question mark is shared |
| `!` | — | exclamation is shared |
| `:` `;` | — | same as Latin |

- **No full stop on buttons or labels.** A button reading `डाउनलोड करें` needs no trailing
  `।`.
- Quotation marks: use `"…"` (double) then `'…'` (single). Hindi does not have its own
  quotation mark convention distinct from Latin.

---

## 2. Spacing and script mixing

Devanagari uses a shirorekha (headline stroke) that visually connects characters within a
word. At a Latin/Devanagari word boundary, use normal Hindi word spacing.

Write `Kiro Crew से कनेक्ट करें`: keep one space before the postposition. Do not glue it as
`Kiro Crewसे`, and do not double-space.

**Font fallback**: ensure CSS `font-family` lists a Devanagari font (Noto Sans Devanagari)
before the Latin fallback so conjuncts render correctly.

---

## 3. Do not translate

Product names stay in Latin script. The canonical list in `glossary.json` includes
`KiroCrew`, `MCP`, `Slack`, `GitHub`, and others. The prose brand `Kiro Crew` also remains
unchanged; do not transliterate it into Devanagari (not `किरोक्रू`).

Checked by `glossary.test.ts`.

---

## 4. Register and tone

- Address the user as **तुम** (informal), not **आप** (formal/honorific). The English
  product voice is direct and casual; आप reads as overly deferential.
- Verb forms follow from तुम: `करो`, `देखो`, `चुनो` (imperative).
- Avoid English loanwords where a natural Hindi word exists, but prefer the loanword if
  the Hindi equivalent is obscure or literary (`डाउनलोड` over `अधोभारण`).

---

## 5. Plurals

CLDR defines **2 plural categories** for Hindi:

| category | condition | example |
|---|---|---|
| one | i = 0 or n = 1 | `0 फ़ाइल`, `0.5 फ़ाइल`, `1 फ़ाइल` |
| other | everything else | `1.1 फ़ाइलें`, `2 फ़ाइलें` |

Note: Hindi `one` includes **zero and decimals whose integer part is zero**. i18next
handles selection via `_one` / `_other` key suffixes.

Checked by `catalogParity.test.ts` which enforces exactly 2 categories.

---

## 6. Gender

Hindi has grammatical gender (masculine/feminine) with no neuter. Past-tense verbs and
adjectives agree with the subject's gender:

- `तुम्हारा कॉन्फ़िगरेशन सहेजा गया` (masculine)
- `तुम्हारी फ़ाइल सहेजी गई` (feminine)

For UI strings addressing the user (whose gender is unknown), **prefer masculine default
or infinitive constructions** (`सहेजना` → "saving") that avoid gender agreement entirely.

This is a known limitation — Hindi cannot address an unknown-gender user without choosing.

---

## 7. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| sentence-final Latin-period debt does not exceed 30 | `hiStyle.test.ts` |
| formal-address debt does not exceed 118 | `hiStyle.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no full-width alphanumerics | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
