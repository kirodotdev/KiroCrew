/**
 * Supported UI languages — the single source of truth.
 *
 * Adding a language is a DATA change, not a code change — exactly three edits:
 *   1. add `src/i18n/locales/<code>.json` (same key set as `en.json`)
 *   2. add one entry here
 *   3. register the catalog in `src/i18n/index.ts` `CATALOGS`
 *
 * No component changes, and no test changes: `catalogParity.test.ts` derives its
 * cases from this list and reads catalogs from the runtime `CATALOGS` map, so a
 * new language automatically gains its own parity/placeholder/empty-value tests.
 * A half-added language therefore fails CI (naming the missing piece) instead of
 * silently rendering English.
 *
 * `label` is deliberately the language's own endonym (自称) — a user looking
 * for Chinese scans for "简体中文", not "Chinese (Simplified)". Endonyms are
 * NOT translated (they read the same in every UI language), which is why they
 * live here as plain strings rather than in the catalogs.
 */

export interface LanguageEntry {
  /** BCP-47 tag. Must match the catalog filename and the i18next resource key. */
  code: string
  /** Endonym shown in the picker — never translated. */
  label: string
}

/**
 * Ordered by global speaker count, so the picker's top entries are the ones
 * most users are looking for rather than an alphabetical accident.
 *
 * Right-to-left languages (Arabic, Urdu) are deliberately absent: the catalogs
 * would translate correctly, but the dashboard's layout is built from
 * physical-direction Tailwind utilities (`pl-*`, `left-*`, `text-left`) and
 * unmirrored directional icons, so an RTL locale would render readable text in
 * a visibly wrong shell. RTL needs `dir="rtl"` plus a logical-property
 * conversion first; shipping the catalog before that would be a worse
 * experience than English.
 */
export const SUPPORTED_LANGUAGES: readonly LanguageEntry[] = [
  { code: 'en', label: 'English' },
  { code: 'zh-CN', label: '简体中文' },
  { code: 'hi', label: 'हिन्दी' },
  { code: 'es', label: 'Español' },
  { code: 'fr', label: 'Français' },
  { code: 'bn', label: 'বাংলা' },
  { code: 'pt', label: 'Português' },
  { code: 'ru', label: 'Русский' },
  { code: 'de', label: 'Deutsch' },
  { code: 'it', label: 'Italiano' },
] as const

/** The fallback language. Its catalog is the key-set authority for all others. */
export const DEFAULT_LANGUAGE = 'en'

/**
 * Sentinel for "follow the browser" — persisted as the empty string so an
 * unset config field (the dataclass default) already means auto-detect, with
 * no migration needed for existing installs.
 */
export const AUTO_LANGUAGE = ''

export const SUPPORTED_CODES: readonly string[] = SUPPORTED_LANGUAGES.map(l => l.code)

export function isSupportedLanguage(code: string): boolean {
  return SUPPORTED_CODES.includes(code)
}

/** Endonym for a code, or the raw code when it isn't one we ship. */
export function languageLabel(code: string): string {
  return SUPPORTED_LANGUAGES.find(l => l.code === code)?.label ?? code
}
