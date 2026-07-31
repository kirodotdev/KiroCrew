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
word. When a Latin-script term (product name) appears inside Devanagari text, a natural
visual break already exists at the script boundary — **do not add extra spaces** around
Latin runs.

Write: `KiroCrewसे कनेक्ट करें` or `KiroCrew से कनेक्ट करें` — a single space after the
Latin term is acceptable because Hindi uses spaces between words, but do not double-space.

**Font fallback**: ensure CSS `font-family` lists a Devanagari font (Noto Sans Devanagari)
before the Latin fallback so conjuncts render correctly.

---

## 3. Do not translate

Product names stay in Latin script. The list is in `glossary.json` under `dnt`.
`KiroCrew`, `MCP`, `Slack`, `GitHub` etc. must appear verbatim — do not transliterate
into Devanagari (not `किरोक्रू`).

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
| one | i = 0, 1 | `{{count}} फ़ाइल` (0 files, 1 file) |
| other | everything else | `{{count}} फ़ाइलें` (2+ files) |

Note: Hindi `one` includes **zero** — `0 फ़ाइल` is grammatically correct. i18next handles
selection via `_one` / `_other` key suffixes.

Checked by `catalogParity.test.ts` which enforces exactly 2 categories.

---

## 6. Gender

Hindi has grammatical gender (masculine/feminine) with no neuter. Past-tense verbs and
adjectives agree with the subject's gender:

- `आपका कॉन्फ़िगरेशन सहेजा गया` (masculine)
- `आपकी फ़ाइल सहेजी गई` (feminine)

For UI strings addressing the user (whose gender is unknown), **prefer masculine default
or infinitive constructions** (`सहेजना` → "saving") that avoid gender agreement entirely.

This is a known limitation — Hindi cannot address an unknown-gender user without choosing.

---

## 7. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no full-width alphanumerics | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
