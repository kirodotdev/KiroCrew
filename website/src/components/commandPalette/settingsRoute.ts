import type { SettingEntry } from './settingsTypes'
import { SUBNAV_PARAM, SUBNAV_LEGACY_PARAMS } from '../subNavParams'
import { settingsPath } from '../settingsPath'

/**
 * The deep link that opens one registry setting:
 * `/settings/<tab>[/<sub>]?…&highlight=<id>`.
 *
 * This is the REGISTRY adapter over the shared settingsPath builder: it
 * extracts the second-level selection from the entry's params (`sub`, or the
 * legacy `channel`/`section` aliases — canonical wins, the same precedence
 * the panels apply on read) and rides every OTHER param on the query string.
 * A second-level key must become the second path segment: without it the
 * target list-detail panel never mounts, so the highlight silently no-ops
 * and the user lands on the tab with nothing selected.
 */
export function settingsRoute(entry: SettingEntry): string {
  const params = entry.params ?? {}
  const sub =
    params[SUBNAV_PARAM] ??
    SUBNAV_LEGACY_PARAMS.map(k => params[k]).find(v => v != null) ??
    null
  const subLevelKeys: readonly string[] = [SUBNAV_PARAM, ...SUBNAV_LEGACY_PARAMS]
  const extra = Object.fromEntries(
    Object.entries(params).filter(([k]) => !subLevelKeys.includes(k))
  )
  return settingsPath({ tab: entry.tab, sub, highlight: entry.id, params: extra })
}
