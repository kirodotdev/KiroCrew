# German style guide

Normative rules for `src/i18n/locales/de.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- Duden — <https://www.duden.de/>
- DIN 5008 (Schreib- und Gestaltungsregeln) — typography standard
- Mozilla German L10n style guide — <https://mozilla-l10n.github.io/styleguides/de/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>

---

## 1. Punctuation

| rule | example |
|---|---|
| Quotation marks: `„…"` (low-high) | `„Speichern"` — not `"Speichern"` |
| Nested quotes: `‚…'` (low-high single) | `„Klicke auf ‚Weiter'"` |
| Comma before relative clauses: **mandatory** | `Die Datei, die geladen wurde` |
| No Oxford comma | `rot, blau und grün` |
| No trailing period on buttons/labels | `Speichern`, not `Speichern.` |

**Number formatting** (relevant for displayed values, not catalog strings):
- Decimal separator: `,` (comma)
- Thousands separator: `.` (period) or thin space
- `1.234,56` not `1,234.56`

---

## 2. Capitalization

**All nouns are capitalized.** This is the most distinctive rule of German orthography and
is not optional:

- `die Einstellungen` (settings), `der Benutzer` (user), `das Netzwerk` (network)
- Adjectives are lowercase unless part of a proper noun.
- **UI button labels**: sentence case with noun capitalization —
  `Konfiguration exportieren` (export configuration), not `konfiguration exportieren`.

---

## 3. Do not translate

Product names stay as-is. The list is in `glossary.json` under `dnt`.

Checked by `glossary.test.ts`.

---

## 4. Register and tone

- Use **du** (informal), not **Sie** (formal). Match the casual English voice.
- **Lowercase** `du` in UI text — the capitalized `Du` is letter-writing convention, not
  software convention (Duden 2024 confirms either is acceptable; we pick lowercase for
  consistency).
- Imperative: `Speichere`, `Verbinde dich`, `Wähle aus`.

---

## 5. Plurals

CLDR defines **2 plural categories** for German:

| category | condition | example |
|---|---|---|
| one | n = 1 | `{{count}} Datei` |
| other | everything else | `{{count}} Dateien` |

Checked by `catalogParity.test.ts`.

---

## 6. Compound words

German forms compounds freely. The **Deppenleerzeichen** (idiot's space) — a space inside
a compound — is a common error in software localization:

| correct | incorrect |
|---|---|
| `Datenschutzeinstellungen` | `Datenschutz Einstellungen` |
| `Benutzeroberfläche` | `Benutzer Oberfläche` |
| `Slack-Integration` | `Slack Integration` |

Rules:
- Native German compounds: **no space, no hyphen** (`Sicherheitseinstellungen`).
- Compounds with a foreign proper noun: **hyphen** (`Slack-Integration`, `GitHub-Konto`).
- Compounds with an abbreviation: **hyphen** (`MCP-Server`, `API-Schlüssel`).

---

## 7. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
