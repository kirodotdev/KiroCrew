/** settingsPath — the ONE builder for a hand-authored Settings deep link.
 *
 *  Navigation state lives in PATH SEGMENTS (`/settings/<tab>[/<sub>]`,
 *  segment[0] = tab, segment[1] = a SubNav's second-level selection) — the
 *  query carries only non-navigation params plus the `highlight` id that
 *  useSettingHighlight flashes. Shared rather than rebuilt per caller,
 *  because a second hand-written copy is how a reader loses the second
 *  level (the exact drift that predated subNavParams).
 *
 *  Kept in its own tiny module (like subNavParams) so non-Settings consumers
 *  — a chat dropdown, an update pill — can build a route without dragging the
 *  command-palette or SubNav module graphs into their bundle.
 */
import { toPathSegment } from './subNavParams'

/** Query key carrying the setting id to flash — read by useSettingHighlight. */
const HIGHLIGHT_QUERY_KEY = 'highlight'

export interface SettingsTarget {
  /** First-level tab key (matches SettingsPage TABS), e.g. 'security'. */
  tab: string
  /** Second-level SubNav selection, e.g. 'approval' under 'security'. */
  sub?: string | null
  /**
   * Setting to scroll to and flash: a registry id ('chat.default-model') or
   * the `key:<configKey>` form useSettingHighlight also accepts.
   */
  highlight?: string
  /** Extra query params the target panel needs to mount, before highlight. */
  params?: Record<string, string>
}

/**
 * Build the deep link `/settings/<tab>[/<sub>]?…[&highlight=<id>]`.
 *
 * toPathSegment encodes each value as exactly one segment and rejects the
 * dot-only values URL normalization would resolve against the tree — a
 * crafted value can neither mint fake depth nor escape /settings. An invalid
 * tab falls back to the /settings root and an invalid sub falls back to the
 * tab root (each level drops independently, the same fallback an unknown key
 * gets on read), so the result is always a safe internal route.
 */
export function settingsPath(target: SettingsTarget): string {
  const tabSeg = toPathSegment(target.tab)
  const subSeg = target.sub != null ? toPathSegment(target.sub) : null
  const path = tabSeg
    ? subSeg
      ? `/settings/${tabSeg}/${subSeg}`
      : `/settings/${tabSeg}`
    : '/settings'
  // Manual encodeURIComponent assembly, not URLSearchParams: the segment
  // codec and the highlight consumer round-trip %20, while URLSearchParams
  // would mint '+' for spaces.
  const pairs = Object.entries(target.params ?? {}).map(
    ([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`
  )
  if (target.highlight != null) {
    pairs.push(`${HIGHLIGHT_QUERY_KEY}=${encodeURIComponent(target.highlight)}`)
  }
  return pairs.length ? `${path}?${pairs.join('&')}` : path
}
