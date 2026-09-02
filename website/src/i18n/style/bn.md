# Bengali style guide

Normative rules for `src/i18n/locales/bn.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- W3C Bangla Gap Analysis — <https://www.w3.org/TR/bengali-gap-analysis/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>
- Mozilla L10n general style guide — <https://mozilla-l10n.github.io/styleguides/mozilla_general/>

---

## 1. Punctuation

| use | not | note |
|---|---|---|
| `।` (dari / purna viram U+0964) | `.` | sentence-final |
| `,` | — | same Latin comma |
| `?` `!` `:` `;` | — | same as Latin |

- **No dari on buttons or labels.** A button reading `ডাউনলোড` needs no trailing `।`.
- Quotation marks: use `"…"` then `'…'` (same as English).

---

## 2. Script and numerals

Bengali has its own digit set (০১২৩৪৫৬৭৮৯) but **use Western Arabic digits (0–9)** in
this catalog. Reasons:

- Placeholder values like `{{count}}` produce Western digits at runtime.
- Mixing `৩ টি ফাইল` with `{{count}} টি ফাইল` where count renders as `3` creates visual
  inconsistency.
- `Intl.NumberFormat('bn')` can add native digits at the formatting layer if desired
  later — the catalog should store the number-agnostic form.

---

## 3. Spacing and conjuncts

Bengali has complex conjunct consonants (যুক্তাক্ষর) that are rendered by the shaping
engine. Do not insert zero-width joiners/non-joiners unless required for disambiguation.
No special spacing is needed between Bengali and Latin text — a normal word space suffices.

---

## 4. Do not translate

Product names stay in Latin script. The list is in `glossary.json` under `dnt`. Do not
transliterate into Bengali script (not `কিরোক্রু`).

Checked by `glossary.test.ts`.

---

## 5. Register and tone

- Address the user as **তুমি** (informal), not **আপনি** (formal/honorific). Match the
  casual English voice.
- Verb forms follow from তুমি: `করো`, `দেখো`, `বেছে নাও`.
- Avoid excessively Sanskritized vocabulary where a common Bangla word exists (`ব্যবহারকারী`
  is fine for "user" — it is established; but prefer `ফাইল` over `নথি` for "file").

---

## 6. Plurals

CLDR defines **2 plural categories** for Bengali:

| category | condition | example |
|---|---|---|
| one | i = 0, 1 | `{{count}} টি ফাইল` |
| other | everything else | `{{count}} টি ফাইল` |

Note: Bengali uses classifiers (টি, গুলি) rather than noun inflection for plurals. The
same noun form often works for both categories, but the classifier or verb may change.
Bengali `one` includes **zero** (same as Hindi).

Checked by `catalogParity.test.ts`.

---

## 7. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
