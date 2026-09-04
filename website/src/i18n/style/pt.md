# Portuguese style guide

Normative rules for `src/i18n/locales/pt.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- Acordo Ortográfico da Língua Portuguesa (2009) — <https://www.portaldalinguaportuguesa.org/>
- Mozilla Portuguese L10n style guide — <https://mozilla-l10n.github.io/styleguides/pt-BR/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>

---

## 1. Regional variant

This catalog uses the bare `pt` BCP 47 tag. We target **Brazilian Portuguese (pt-BR)**
conventions:

- Largest Portuguese-speaking population (~215M vs ~10M for Portugal).
- Dominant variant in tech documentation and software.

If European Portuguese users are added later, a separate `pt-PT.json` would be created.

**Vocabulary**: prefer Brazilian forms when they diverge from European Portuguese:

| use (BR) | not (PT) | meaning |
|---|---|---|
| computador | ordenador | computer |
| tela | ecrã | screen |
| baixar | descarregar | download |
| arquivo | ficheiro | file |
| celular | telemóvel | mobile phone |

---

## 2. Punctuation

Standard Latin punctuation. No special rules beyond English.

| rule | example |
|---|---|
| Quotation marks: `"…"` outer, `'…'` inner | Not `«…»` (that is European Portuguese) |
| Ellipsis: `…` (U+2026) | Not three dots |
| No trailing period on buttons/labels | `Salvar`, not `Salvar.` |

---

## 3. Do not translate

Product names stay as-is. The list is in `glossary.json` under `dnt`.

Checked by `glossary.test.ts`.

---

## 4. Register and tone

- Use **você** — the standard informal Brazilian second-person form.
- Do **not** use `tu` (regional, sounds odd in most of Brazil) or `o senhor/a senhora`
  (overly formal).
- Imperative: prefer the você-form (`Salve`, `Conecte`, `Escolha`).

---

## 5. Plurals

CLDR defines **2 plural categories** for Portuguese:

| category | condition | example |
|---|---|---|
| one | i = 1 | `{{count}} arquivo` |
| other | everything else | `{{count}} arquivos` |

Note: `one` is only for integer 1 (1.0 triggers `other`). Zero takes `other`:
`0 arquivos`. Checked by `catalogParity.test.ts`.

---

## 6. Accents

All accents are mandatory. The 2009 Acordo Ortográfico is the reference:

- Eliminates the trema (previously used in some words like `frequente`).
- Removes the circumflex from some double-vowel forms (`voo`, not `vôo`).
- Accents on capitals are mandatory: `Área`, `Último`.

---

## 7. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
