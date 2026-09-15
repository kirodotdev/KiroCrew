# Italian style guide

Normative rules for `src/i18n/locales/it.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- Accademia della Crusca — <https://accademiadellacrusca.it/>
- Mozilla Italian L10n style guide — <https://mozilla-l10n.github.io/styleguides/it/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>

---

## 1. Punctuation

| rule | example |
|---|---|
| Quotation marks: `«…»` or `"…"` | Both acceptable in software UI |
| No space before `:` `;` `?` `!` | Unlike French — `Sei sicuro?` not `Sei sicuro ?` |
| Apostrophe: typographic `'` (U+2019) | For elision: `l'utente`, `un'applicazione` |
| Ellipsis: `…` (U+2026) | |
| No trailing period on buttons/labels | `Salva`, not `Salva.` |

**Elision is mandatory** where Italian phonology requires it: `l'applicazione` (not `la
applicazione`), `un'interfaccia` (not `una interfaccia` — feminine only), `dell'utente`.

---

## 2. Spacing

Standard Latin spacing rules. No thin-space requirements.

---

## 3. Do not translate

Product names stay as-is. The list is in `glossary.json` under `dnt`.

Checked by `glossary.test.ts`.

---

## 4. Register and tone

- Use **tu** (informal), not **Lei** (formal) or **Voi** (archaic formal). Match the
  casual English voice.
- Imperative follows naturally: `Salva`, `Connettiti`, `Scegli`.

---

## 5. Plurals

CLDR defines **2 plural categories** for Italian:

| category | condition | example |
|---|---|---|
| one | n = 1 | `{{count}} file` |
| other | everything else | `{{count}} file` |

Note: some Italian nouns are invariable in plural (particularly foreign borrowings like
`file`, `server`). Use the correct Italian plural for native words: `impostazione` →
`impostazioni`.

Checked by `catalogParity.test.ts`.

---

## 6. Gender

Italian has masculine/feminine grammatical gender. For strings addressing an unknown-gender
user:

- Prefer **impersonal constructions** with `si`: `Si è connesso` or better `Connessione
  effettuata` (connection made — avoids the problem entirely).
- If a gendered form is unavoidable, use masculine as the grammatical default.

---

## 7. Accents

Accents on final vowels are **mandatory** and distinguish meaning:

| accented | unaccented | difference |
|---|---|---|
| `perché` | `perche` | wrong |
| `è` (is) | `e` (and) | different word |
| `più` | `piu` | wrong |
| `già` | `gia` | wrong |

- Grave accent (`) is standard on `a`, `i`, `o`, `u` and open `e/o`: `città`, `così`,
  `può`.
- Acute accent (´) only on closed `e`: `perché`, `poiché`, `affinché`.
- Missing accents are **errors**, not style choices.

---

## 8. Articles with foreign words

Foreign words take the masculine singular article determined by their initial sound:

| article | before | example |
|---|---|---|
| `il` | consonant | `il Markdown`, `il Docker` |
| `lo` | s+cons, z, gn, ps, x, y | `lo YAML`, `lo Slack` |
| `l'` | vowel | `l'OAuth` |

This matters when foreign product names appear in a sentence: `Connetti il tuo account
GitHub`, `Configura lo YAML`.

---

## 9. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
