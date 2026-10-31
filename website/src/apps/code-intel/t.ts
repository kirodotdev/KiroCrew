/**
 * Code-intel's strings. An app owns its catalogs: the host shares only locale
 * selection (`activeLocale`) and the repaint signal (`useLanguageGeneration`), so
 * this module reads the app's own files, the same ones its backend reads for the
 * sentences it answers a binding with (`code_intel/i18n.py`). They live in the
 * Python package so one copy serves both sides and ships with the app.
 */
import { activeLocale } from '@kirocrew/app-sdk'
import bn from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/bn.json'
import de from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/de.json'
import en from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/en.json'
import es from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/es.json'
import fr from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/fr.json'
import hi from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/hi.json'
import it from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/it.json'
import ja from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/ja.json'
import ko from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/ko.json'
import pt from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/pt.json'
import ru from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/ru.json'
import zhCN from '../../../../src/kiro_crew/apps/builtins/code_intel/locales/zh-CN.json'

type Catalog = Record<string, string>
const CATALOGS: Record<string, Catalog> = { bn, de, en, es, fr, hi, it, ja, ko, pt, ru, 'zh-CN': zhCN }

export type Key = keyof typeof en

/** The catalog for the dashboard's language; a region tag falls back to its language, then English. */
function catalog(): Catalog {
  const locale = activeLocale()
  return CATALOGS[locale] ?? CATALOGS[locale.split('-')[0]] ?? en
}

/** The string for `key`, with `{{name}}` placeholders filled from `values`. */
export function tr(key: Key, values: Record<string, string | number> = {}): string {
  const text = catalog()[key] ?? en[key] ?? key
  return text.replace(/\{\{\s*(\w+)\s*\}\}/g, (m, name: string) => (name in values ? String(values[name]) : m))
}

/** `external_one` for one where the language has the form, else `external_other`. */
export function plural(stem: 'external', count: number): string {
  const forms: Record<typeof stem, [Key, Key]> = { external: ['external_one', 'external_other'] }
  const [one, other] = forms[stem]
  return tr(count === 1 && one in catalog() ? one : other, { count })
}
