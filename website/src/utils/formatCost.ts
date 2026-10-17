import { fmtCurrency } from '../i18n/format'

/**
 * Format a USD cost for display at a precision a user can actually act on.
 *
 * Every cost surface (Usage, daily-chart, deploy, Agents) formats the same
 * concept the same way, so two screens showing one number never look like they
 * disagree. Four decimals imply a precision no decision needs — "$0.0231" and
 * "≤ $0.0004" read as noise where "$0.02" and "<$0.01" carry the information.
 *
 * Rules: 2dp, a `<$0.01` floor for non-zero dust, and `~$0` for a true zero
 * (an exact-zero cost is a different fact from "too small to show").
 * Non-finite / missing input renders as an em dash rather than "$NaN".
 *
 * NOTE: for MONEY only. Similarity scores (VectorMemoryCard) legitimately want
 * 2–3dp — do not route those through here.
 *
 * Formatting goes through `Intl.NumberFormat`'s currency style, so the symbol's
 * POSITION and the decimal separator follow the language: `$12.50` in English,
 * `12,50 $` in German. The `<$0.01` and `~$0` sentinels keep their own shape
 * because they are threshold statements rather than amounts; the amount inside
 * the floor sentinel is still formatted, so the separator stays consistent with
 * the numbers around it.
 */
export function formatCost(usd: number | null | undefined): string {
  if (usd == null || !Number.isFinite(usd)) return '—'
  if (usd === 0) return '~$0'
  // Negative costs aren't a real domain value; surface the sign rather than
  // silently flooring a bad input to <$0.01.
  const abs = Math.abs(usd)
  const sign = usd < 0 ? '-' : ''
  if (abs < 0.01) return `${sign}<${fmtCurrency(0.01)}`
  return `${sign}${fmtCurrency(abs)}`
}
