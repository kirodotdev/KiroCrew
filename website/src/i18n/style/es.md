# Spanish style guide

Normative rules for `src/i18n/locales/es.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- RAE (Real Academia Española) — <https://www.rae.es/>
- Mozilla Spanish L10n style guide — <https://mozilla-l10n.github.io/styleguides/es/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>

---

## 1. Punctuation

| rule | example |
|---|---|
| Inverted marks on questions/exclamations | `¿Estás seguro?` / `¡Listo!` |
| Quotation marks: `"…"` (double) | Not `«…»` — angular quotes are peninsular print convention |
| No Oxford comma | `rojo, azul y verde` — never `rojo, azul, y verde` |
| Ellipsis: `…` (U+2026) | Not three dots |
| No trailing period on buttons/labels | `Guardar`, not `Guardar.` |

The inverted `¿` and `¡` are **mandatory per RAE** even in short UI strings. A question
without `¿` is an error, not a style choice.

---

## 2. Spacing

Standard Latin spacing rules. No special handling required.

---

## 3. Do not translate

Product names stay as-is. The list is in `glossary.json` under `dnt`. Do not translate or
adapt: `KiroCrew`, `MCP`, `Slack`, `GitHub`, etc.

Checked by `glossary.test.ts`.

---

## 4. Register and tone

- Use **tú** (informal singular), not **usted**. The product's English voice is casual.
- This catalog targets **Latin American neutral** Spanish — not peninsular. When
  vocabulary diverges between regions, prefer the most universally understood term:
  - `computadora` over `ordenador` (peninsular)
  - `aplicación` over `app` (unless space-constrained in UI)
  - `correo electrónico` over `e-mail` or `mail`
- Avoid voseo (`vos tenés`) — it is regional (Argentina, parts of Central America).

---

## 5. Plurals

CLDR defines **2 plural categories** for Spanish:

| category | condition | example |
|---|---|---|
| one | n = 1 | `{{count}} archivo` |
| other | everything else | `{{count}} archivos` |

Straightforward. Checked by `catalogParity.test.ts`.

---

## 6. Gender

Spanish has masculine/feminine grammatical gender. For UI strings addressing an
unknown-gender user:

- **Prefer masculine as the grammatical default** (`Conectado`, not `Conectado/a`).
- Use inclusive reformulations where natural: `Se guardó la configuración` (impersonal)
  instead of `Has guardado…` (gendered verb agreement in compound tenses is masculine by
  default anyway).
- **Do not use** `@` or `x` endings (`todxs`, `usuari@s`) — they are not screen-reader
  accessible and are not recognized by RAE.

---

## 7. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
