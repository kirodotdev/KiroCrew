import type { SettingEntry } from './settingsTypes'

/**
 * The deep link that opens one setting.
 *
 * Extra params (e.g. `channel=slack` for the Channels list-detail tab) must ride the
 * link: without them the target panel never mounts, so the highlight silently no-ops
 * and the user lands on the tab with nothing selected. Shared rather than rebuilt per
 * caller, because a second hand-written copy is how a reader loses `params`.
 */
export function settingsRoute(entry: SettingEntry): string {
  const extra = entry.params
    ? Object.entries(entry.params)
        .map(([k, v]) => `&${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
        .join('')
    : ''
  return `/settings?tab=${entry.tab}${extra}&highlight=${encodeURIComponent(entry.id)}`
}
