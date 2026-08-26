import { fmtRelative, toDate } from '../i18n/format'

/** A timestamp this far ahead of us is clock skew, not the future. */
const SKEW_TOLERANCE_MS = 60_000

/**
 * Human-readable relative time from a unix timestamp (seconds).
 *
 * Delegates to the locale-aware seam (`i18n/format.ts`) rather than building the
 * string from a ladder of English template literals, which have no locale to
 * consult and would render `2m ago` / `3h ago` in every language. `fmtRelative`
 * asks CLDR, so the same instant reads `2分钟前` in Chinese and `vor 2 Minuten`
 * in German.
 *
 * English output is unchanged for seconds, minutes, hours and multi-day gaps
 * (`45s ago`, `2m ago`, `3h ago`, `5d ago`). Two deltas, both deliberate:
 * sub-10-second now reads `now` rather than `just now`, and exactly one day back
 * reads `yesterday` rather than `1d ago` — CLDR words those idiomatically, which
 * is the point of using it.
 */
export function timeAgo(ts: number): string {
  // Guard against a missing/unparseable timestamp: callers that derive ts from
  // an absent or bad date pass 0 / NaN, which would otherwise render as a
  // garbage age (ts=0 → ~20602d). Sub-second positives aren't meaningful either,
  // so the `< 1` floor is kept rather than delegating to `toDate`, which accepts
  // any positive epoch. `--` stays the sentinel rather than format.ts's em dash
  // so existing callers' column widths are unaffected.
  if (!ts || !Number.isFinite(ts) || ts < 1) return '--'
  const at = toDate(ts)
  if (!at) return '--'

  // A slightly-future timestamp is a skewed clock, not a scheduled event, and
  // this helper only ever renders ages. Collapse it to "now" rather than letting
  // `fmtRelative` report "in 12s" for a row that already happened. Beyond the
  // tolerance the future IS shown, so a badly wrong clock stays visible.
  const now = Date.now()
  if (at.getTime() > now && at.getTime() - now < SKEW_TOLERANCE_MS) return fmtRelative(now, { now })

  return fmtRelative(at, { now })
}
