import type { SettingEntry } from './settingsTypes'
import { SUBNAV_PARAM, SUBNAV_LEGACY_PARAMS } from '../subNavParams'

/**
 * The deep link that opens one setting.
 *
 * Extra params (e.g. `channel=slack` for the Channels list-detail tab) must ride the
 * link: without them the target panel never mounts, so the highlight silently no-ops
 * and the user lands on the tab with nothing selected. Shared rather than rebuilt per
 * caller, because a second hand-written copy is how a reader loses `params`.
 *
 * Legacy second-level keys in the registry (`channel`, `section`) are translated to
 * the canonical `sub` here, at the single write path: the panels honour the aliases
 * on read for old bookmarks, but a freshly minted link must carry the name the
 * navigation shell keys its level test on — an alias-bearing link renders BOTH the
 * shell's back bar and the SubNav's on a phone.
 */
export function settingsRoute(entry: SettingEntry): string {
  const legacy: readonly string[] = SUBNAV_LEGACY_PARAMS
  const extra = entry.params
    ? Object.entries(entry.params)
        .map(([k, v]) => `&${encodeURIComponent(legacy.includes(k) ? SUBNAV_PARAM : k)}=${encodeURIComponent(v)}`)
        .join('')
    : ''
  return `/settings?tab=${entry.tab}${extra}&highlight=${encodeURIComponent(entry.id)}`
}
