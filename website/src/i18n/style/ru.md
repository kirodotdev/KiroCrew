# Russian style guide

Normative rules for `src/i18n/locales/ru.json`. Where a rule is mechanically checkable
it is named alongside the test that enforces it; the rest are for translation reviewers.

- Мильчин А.Э., Чельцова Л.К. «Справочник издателя и автора» — typography authority
- Gramota.ru — <https://gramota.ru/> (grammar and usage)
- Mozilla Russian L10n style guide — <https://mozilla-l10n.github.io/styleguides/ru/>
- Unicode CLDR Plural Rules — <https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html>

---

## 1. Punctuation

| rule | example |
|---|---|
| Quotation marks: `«…»` outer, `„…"` inner | `«Нажмите „Сохранить"»` |
| Em dash `—` with spaces for parenthetical | `Файл — не найден` |
| Ellipsis: three dots `...` | Not the Unicode `…` character (per Russian typographic tradition) |
| No trailing period on buttons/labels | `Сохранить`, not `Сохранить.` |

---

## 2. Spacing

Standard Latin spacing rules apply. No thin-space requirements in UI text (thin space
before `%` and units is correct in book typography but unnecessary in a software catalog).

---

## 3. Do not translate

Product names stay in Latin script and **must not be declined** into Russian grammatical
cases:

| correct | incorrect |
|---|---|
| `в GitHub` | `в Гитхабе` |
| `через Slack` | `через Слэк` |
| `настройки Kiro Crew` | `настройки КироКрю` |

Russian routinely declines borrowed common nouns (пул-реквест → пул-реквеста) — that is
fine for common nouns. But DNT proper nouns from `glossary.json` must remain verbatim.

Checked by `glossary.test.ts`.

---

## 4. Register and tone

- Use **ты** (informal), not **вы/Вы** (formal). Match the casual English voice.
- Imperative: `Сохрани`, `Подключись`, `Выбери` (ты-form).

---

## 5. Plurals

**Critical: Russian has the most complex plural system among our languages.** CLDR defines
**4 plural categories**:

| category | condition | example |
|---|---|---|
| one | n%10=1, n%100≠11 | `{{count}} файл` (1, 21, 31…) |
| few | n%10=2-4, n%100≠12-14 | `{{count}} файла` (2, 3, 4, 22…) |
| many | n%10=0 or n%10=5-9 or n%100=11-14 | `{{count}} файлов` (0, 5–20, 25…) |
| other | fractional numbers | `{{count}} файла` (1.5, 2.7…) |

Every `{{count}}` key **must** have `_one`, `_few`, `_many`, `_other` suffixes. Missing
any category means i18next falls back to English for those numbers.

Checked by `catalogParity.test.ts` which enforces exactly 4 categories for `ru`.

---

## 6. Gender

Russian has three grammatical genders (masculine, feminine, neuter). Past-tense verbs
agree with subject gender: `файл загружен` (m) vs `страница загружена` (f).

For strings addressing the user (unknown gender):
- Prefer **infinitive constructions**: `Нажать для сохранения` (press to save).
- Or **imperative** (gender-neutral in Russian): `Сохрани` works for any gender.
- Avoid past tense with gendered agreement when addressing the user directly.

---

## 7. What is mechanically enforced

| rule | gate |
|---|---|
| placeholder parity with English | `catalogParity.test.ts` |
| correct CLDR plural categories (**4**) | `catalogParity.test.ts` |
| do-not-translate terms present | `glossary.test.ts` |
| balanced delimiters | `qa.test.ts` |
| no leading/trailing whitespace | `qa.test.ts` |
