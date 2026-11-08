import { i18nT } from '../i18n/t'
import { fmtPercent, fmtCompact } from '../i18n/format'

/** Clamp a raw context percentage to a safe display integer [0, 100]. */
export function contextPctClamped(pct: number): number {
  return Math.round(Math.min(Math.max(Number.isFinite(pct) ? pct : 0, 0), 100))
}

/** Returns the CSS colour variable for a context usage percentage. */
export function contextColor(pct: number): string {
  const p = contextPctClamped(pct)
  return p >= 90 ? 'var(--danger)' : p >= 75 ? 'var(--warn)' : 'var(--accent)'
}

/** Builds the context tooltip string. Shared so the bar and its parent pill show identical text.
 *  `unknown` is a slot with no reading yet, which is said as such rather than as 0%. */
export function contextTip(pct: number, unknown = false): string {
  if (unknown) return i18nT('components.contextBar.context_unknown')
  return i18nT('components.contextBar.context_pct', { pct: contextPctClamped(pct) })
}

/**
 * Format a token count for display via the i18n seam's compact notation
 * (locale-aware: `1.2K` in en, `1.5万` in zh, `1.5 Tsd.` in de). Non-positive /
 * non-finite inputs render as a plain zero.
 */
export function fmtTokens(n: number): string {
  return Number.isFinite(n) && n > 0 ? fmtCompact(n) : fmtCompact(0)
}

export interface ContextReadoutOptions {
  /** Include the percentage segment ("48%"). */
  showPct?: boolean
  /** Include the token-usage segment ("96K/200K"). */
  showTokens?: boolean
  /** True when used/total are estimated, not exact (prefixes "~"). */
  approx?: boolean
}

/**
 * Compose the inline context readout from independent toggles. The percentage
 * and token-usage segments are joined by " · " when both are shown:
 *   both   → "48% · 96K/200K"
 *   pct    → "48%"
 *   tokens → "96K/200K"
 *   none   → ""
 * Percentage and token counts are localized through the i18n seam
 * (`src/i18n/format.ts`), so digits, the "%" placement and the compact suffix
 * follow the active language.
 */
export function composeContextReadout(
  pct: number,
  used: number,
  total: number,
  { showPct, showTokens, approx }: ContextReadoutOptions,
): string {
  const parts: string[] = []
  if (showPct) parts.push(fmtPercent(contextPctClamped(pct) / 100))
  // Skip the token segment when the window size is unknown (total <= 0):
  // rendering "96K/0" would be nonsense, so degrade to the remaining segments.
  if (showTokens && Number.isFinite(total) && total > 0) parts.push(`${approx ? '~' : ''}${fmtTokens(used)}/${fmtTokens(total)}`)
  return parts.join(' · ')
}

/** Compact horizontal context-usage bar for the input bar. */
export default function ContextBar(
  { pct, width = 40, height = 3, unknown = false }:
    { pct: number; width?: number; height?: number; unknown?: boolean },
) {
  // No reading yet: an empty track, never a measured 0% fill.
  const p = unknown ? 0 : contextPctClamped(pct)
  const fill = contextColor(pct)
  const tip = contextTip(pct, unknown)
  const r = height / 2
  return (
    <span title={tip} aria-label={tip} className="inline-flex shrink-0">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="block">
        {unknown
          // A dashed track reads as "no reading", where a solid empty one is a measured 0%.
          ? <line x1="0" y1={r} x2={width} y2={r} stroke="var(--text)" strokeOpacity="0.3" strokeWidth={height} strokeDasharray={`${height} ${height}`} data-testid="context-bar-unknown" />
          : <rect x="0" y="0" width={width} height={height} rx={r} ry={r} fill="var(--text)" opacity="0.15" />}
        <rect x="0" y="0" width={(width * p) / 100} height={height} rx={r} ry={r} fill={fill} style={{ transition: 'width 500ms' }} />
      </svg>
    </span>
  )
}
