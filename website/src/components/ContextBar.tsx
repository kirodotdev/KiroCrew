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

/** Builds the context tooltip string. Shared so the bar and its parent pill show identical text. */
export function contextTip(pct: number): string {
  return `Context: ${contextPctClamped(pct)}%`
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
  if (showTokens) parts.push(`${approx ? '~' : ''}${fmtTokens(used)}/${fmtTokens(total)}`)
  return parts.join(' · ')
}

/** Compact horizontal context-usage bar for the input bar. */
export default function ContextBar(
  { pct, width = 40, height = 3 }:
    { pct: number; width?: number; height?: number },
) {
  const p = contextPctClamped(pct)
  const fill = contextColor(pct)
  const tip = contextTip(pct)
  const r = height / 2
  return (
    <span title={tip} aria-label={tip} className="inline-flex shrink-0">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="block">
        <rect x="0" y="0" width={width} height={height} rx={r} ry={r} fill="var(--text)" opacity="0.15" />
        <rect x="0" y="0" width={(width * p) / 100} height={height} rx={r} ry={r} fill={fill} style={{ transition: 'width 500ms' }} />
      </svg>
    </span>
  )
}
