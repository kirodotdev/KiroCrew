import { usePersistedBool } from './usePersistedBool'

/**
 * localStorage key backing the plain-diff preference. Shared by the Display
 * settings toggle and every patch surface, so both read one spelling.
 */
export const PLAIN_DIFF_KEY = 'mc-diff-plain'

/**
 * When true, unified-patch surfaces render the raw patch text in a monospace
 * block instead of routing it through Pierre's syntax-highlighted diff.
 *
 * The saving is meant to be REAL, not just visual: turning colour off should not
 * leave the machinery that produces it running. So each surface declines as much
 * of it as it can — `PierrePatch` never requests the Pierre chunk at all, and
 * `PierreFilePairImpl` (which must still compute a diff from two file bodies)
 * declares both sides plain text and opts out of the highlight worker pool,
 * which is built on demand so that opting out prevents the workers from ever
 * spawning. Whole-FILE code views are deliberately untouched: this is a choice
 * about diffs, and their highlighting is the case the workers exist for.
 *
 * A per-CLIENT rendering choice, so it lives in localStorage next to
 * `mc-diff-split` rather than in the server config: the machine that paints the
 * diff is the one whose CPU and memory the highlighter spends, and a user who
 * turns the colour off on a laptop is not asking a shared gateway to change.
 *
 * Off by default — the highlighted diff stays what a new install shows.
 */
export function usePlainDiff() {
  return usePersistedBool(PLAIN_DIFF_KEY, false)
}
