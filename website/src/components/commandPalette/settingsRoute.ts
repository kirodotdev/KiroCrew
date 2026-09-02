import type { SettingEntry } from './settingsTypes'
import { SUBNAV_PARAM, SUBNAV_LEGACY_PARAMS, toPathSegment } from '../subNavParams'

/** Query key carrying the setting id to flash — read by useSettingHighlight. */
const HIGHLIGHT_QUERY_KEY = 'highlight'

/**
 * The deep link that opens one setting: `/settings/<tab>[/<sub>]?…&highlight=<id>`.
 *
 * Navigation state lives in PATH SEGMENTS (segment[0] = tab, segment[1] = a
 * SubNav's second-level selection) — the query carries only non-navigation
 * params. Shared rather than rebuilt per caller, because a second hand-written
 * copy is how a reader loses the second level.
 *
 * A second-level key in the registry (`sub`, or the legacy `channel`/`section`
 * aliases) becomes the second path segment here, at the single write path:
 * without it the target list-detail panel never mounts, so the highlight
 * silently no-ops and the user lands on the tab with nothing selected. The
 * canonical `sub` wins over the historical aliases — the same precedence the
 * panels apply on read. Any OTHER entry param rides the query string, before
 * the highlight. Segments are encoded so a crafted registry value cannot mint
 * fake depth.
 */
export function settingsRoute(entry: SettingEntry): string {
  const params = entry.params ?? {}
  const sub =
    params[SUBNAV_PARAM] ??
    SUBNAV_LEGACY_PARAMS.map(k => params[k]).find(v => v != null) ??
    null
  const subLevelKeys: readonly string[] = [SUBNAV_PARAM, ...SUBNAV_LEGACY_PARAMS]
  const extra = Object.entries(params)
    .filter(([k]) => !subLevelKeys.includes(k))
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}&`)
    .join('')
  // toPathSegment encodes each value as exactly one segment and rejects the
  // dot-only values URL normalization would resolve against the tree — a
  // crafted registry value can neither mint fake depth nor escape /settings.
  const tabSeg = toPathSegment(entry.tab)
  const subSeg = sub != null ? toPathSegment(sub) : null
  const path = tabSeg
    ? subSeg
      ? `/settings/${tabSeg}/${subSeg}`
      : `/settings/${tabSeg}`
    : '/settings'
  return `${path}?${extra}${HIGHLIGHT_QUERY_KEY}=${encodeURIComponent(entry.id)}`
}
