import { usePersistedBool } from './usePersistedBool'

/**
 * localStorage key backing the diff-layout preference. Shared by the Display
 * settings toggle and every diff surface (chat diff blocks, file-change cards,
 * side panel, markdown panel), so they all read one spelling and one default.
 */
const DIFF_SPLIT_KEY = 'mc-diff-split'

/**
 * When true, diff surfaces open side-by-side (two columns); when false, they
 * open as a single unified column. Each surface seeds its initial layout from
 * this value, and a per-block toggle still overrides it for that one block.
 *
 * A per-CLIENT rendering choice, so it lives in localStorage next to
 * `mc-diff-plain` rather than in the server config: the layout a diff opens in
 * belongs to the machine reading it, not to a shared gateway.
 *
 * Defaults to true (side-by-side) -- the value a new install has always shown.
 */
export function useDiffSplit() {
  return usePersistedBool(DIFF_SPLIT_KEY, true)
}
