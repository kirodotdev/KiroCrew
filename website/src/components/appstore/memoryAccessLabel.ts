import { i18nT } from '../../i18n/t'

// One whole sentence per manifest `permissions.memory` value. `true` and the
// documented strings get their own wording; any other string is shown as it is
// inside the sentence, so a new value never renders blank.
const MEMORY_ACCESS_KEYS = {
  'app-scoped': 'pages.appDetailPage.memory_app_scoped',
  shared: 'pages.appDetailPage.memory_shared',
} as const

/** The App detail permissions row for a truthy `permissions.memory` value. */
export function memoryAccessLabel(value: boolean | string): string {
  const text = String(value)
  // The installed manifest stores a boolean `true` as the string "True".
  if (text.toLowerCase() === 'true') return i18nT('pages.appDetailPage.memory_yes')
  return Object.hasOwn(MEMORY_ACCESS_KEYS, text)
    ? i18nT(MEMORY_ACCESS_KEYS[text as keyof typeof MEMORY_ACCESS_KEYS])
    : i18nT('pages.appDetailPage.memory_value', { value: text })
}
