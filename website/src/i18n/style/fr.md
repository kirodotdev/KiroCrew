# French style guide

Normative rules for `src/i18n/locales/fr.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- Académie française — <https://www.academie-francaise.fr/>
- OQLF (Office québécois de la langue française) — <https://vitrinelinguistique.oqlf.gouv.qc.ca/>
- Mozilla French L10n style guide — <https://mozilla-l10n.github.io/styleguides/fr/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>

---

## 1. Punctuation

**The single most common error in French localization is spacing before double
punctuation.** French requires a **narrow no-break space** (U+202F) before `;` `:` `?` `!`
and inside `«…»` guillemets.

| character | spacing | example |
|---|---|---|
| `;` `:` `?` `!` | U+202F before | `Êtes-vous sûr\u202f?` |
| `«` | U+202F after | `«\u202fOui\u202f»` |
| `»` | U+202F before | |
| `.` `,` | no space before | `Enregistré.` |

- **Use U+202F**, not a regular space (U+0020) — a regular space can line-break, leaving
  `?` alone at the start of a line.
- **Not enforced by CI yet** — candidate for Phase 2. Reviewers must check manually.
- Apostrophe: use typographic `'` (U+2019), not ASCII `'` (U+0027): `l'utilisateur`.
- Quotation marks: `«\u202f…\u202f»` (outer), `"…"` (inner/nested).
- No trailing period on buttons/labels.

---

## 2. Spacing

The thin-space rule in §1 is the critical rule. Otherwise standard Latin spacing.

---

## 3. Do not translate

Product names stay as-is. The list is in `glossary.json` under `dnt`.

Checked by `glossary.test.ts`.

---

## 4. Register and tone

- Use **tu/toi** (informal), not **vous** (formal). Match the casual English voice.
- Imperative: `Enregistre`, `Connecte-toi`, not `Enregistrez`, `Connectez-vous`.

---

## 5. Plurals

CLDR defines **2 plural categories** for French:

| category | condition | example |
|---|---|---|
| one | n = 0, 1 | `{{count}} fichier` |
| other | n ≥ 2 | `{{count}} fichiers` |

Note: French `one` includes **zero** (`0 fichier` is correct). Checked by
`catalogParity.test.ts`.

---

## 6. Gender

French has masculine/feminine grammatical gender. Same guidance as Spanish: prefer
masculine as the grammatical default or impersonal reformulations (`La configuration a
été enregistrée` rather than `Tu as enregistré…`).

---

## 7. Accents on capitals

**All accents are mandatory on capital letters.** `État`, `À propos`, `Éléments` — never
`Etat`, `A propos`, `Elements`. This is official Académie française policy (1990 spelling
reforms confirmed it). Missing accent on a capital is an error.

---

## 8. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (2) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |

**Not yet enforced**: narrow no-break space before double punctuation (Phase 2 candidate).
