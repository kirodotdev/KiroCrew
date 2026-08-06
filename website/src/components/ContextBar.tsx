import { i18nT } from '../i18n/t'

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
  return i18nT('components.contextBar.context_pct', { pct: contextPctClamped(pct) })
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
