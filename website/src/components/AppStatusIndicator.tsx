import type { AppNavStatus } from '../store/appStatusSlice'

/** The active tones that render an indicator. "neutral" (idle) and any unknown
 *  tone render nothing, so an idle or misreported app shows no indicator. */
const ACTIVE_TONES = new Set(['busy', 'positive', 'caution', 'critical'])

/**
 * Reflects an app's reported runtime status on its sidebar row (issue #520,
 * design Option A): a small colored dot. Tone colors come from the theme via
 * the `--nav-status-*` custom properties; the `busy` tone pulses. The status
 * label is not drawn inline — it is the dot's hover tooltip and accessible name
 * (`title`/`aria-label`), so the rail stays uncluttered.
 *
 * Placement matches the sibling ActivityIndicator/BadgeIndicator:
 *   - collapsed rail: a corner dot on the icon.
 *   - expanded rail: a dot vertically centered at the row's right edge.
 */
export function AppStatusIndicator({
  status,
  collapsed = false,
}: {
  status: AppNavStatus | null
  collapsed?: boolean
}) {
  if (!status || !ACTIVE_TONES.has(status.tone)) return null
  const tone = status.tone
  const label = status.label || tone
  const pulse = tone === 'busy' ? ' app-status-dot--pulse' : ''
  const place = collapsed ? 'app-status-dot--corner' : 'app-status-dot--edge'
  return (
    <span
      className={`app-status-dot ${place} app-status-dot--${tone}${pulse}`}
      role="img"
      aria-label={label}
      title={label}
      data-tone={tone}
    />
  )
}
