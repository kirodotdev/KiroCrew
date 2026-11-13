import { i18nT } from '../i18n/t'

// Display words for the installed record's origin, resources and lifecycle
// values. A value outside these maps is shown as it is.
const ORIGIN_LABEL_KEYS = {
  builtin: 'pages.appDetailPage.origin_builtin',
  registry: 'pages.appDetailPage.origin_registry',
  local: 'pages.appDetailPage.origin_local',
  external: 'pages.appDetailPage.origin_external',
} as const
const MANAGER_LABEL_KEYS = {
  gateway: 'pages.appDetailPage.managed_by_gateway',
  app: 'pages.appDetailPage.managed_by_app',
  locked: 'pages.appDetailPage.lifecycle_locked',
} as const

export function originLabel(value: string): string {
  return Object.hasOwn(ORIGIN_LABEL_KEYS, value)
    ? i18nT(ORIGIN_LABEL_KEYS[value as keyof typeof ORIGIN_LABEL_KEYS])
    : value
}

export function managerLabel(value: string): string {
  return Object.hasOwn(MANAGER_LABEL_KEYS, value)
    ? i18nT(MANAGER_LABEL_KEYS[value as keyof typeof MANAGER_LABEL_KEYS])
    : value
}
