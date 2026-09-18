/**
 * registryOrder — one display order for registry rows, shared by every surface
 * that lists them (the External Registries card and the App Store SOURCES rail).
 *
 * ## Why a shared helper and not a sort at each call site
 *
 * The two surfaces list the SAME registries. A user who reads "Community" last in
 * one place and second in the other has to work out whether the two lists even
 * describe the same sources. One helper makes the order a property of the data,
 * so neither surface can drift from the other.
 *
 * ## The order, and why it is this way round
 *
 * `curated` first, unreviewed next, `community` last. The rail is read top-down,
 * so the row a user is most likely to install from without checking anything sits
 * where they look first, and the one that carries "not vetted" sits where they
 * have already read the safer options. Sorting the other way would put the
 * least-reviewed source in the most-trusted position.
 *
 * ## Display only
 *
 * This never touches the backend order. `_effective_registries` resolves a
 * duplicate `name` by edition order, and re-ordering here cannot change that: the
 * sort is applied to rows the backend has already resolved. It is also **stable**
 * — rows sharing a tier keep the relative order the backend sent, so today's
 * unreviewed rows (and every operator row) render exactly as they do now.
 */

/** Any row carrying a review tier — both surfaces' row types satisfy this. */
export type ReviewRanked = { review?: string }

/**
 * Rank for a review value: lower sorts earlier.
 *
 * An UNKNOWN value ranks with the unreviewed middle rather than with either
 * named tier. A future core could add a tier this build does not know, and
 * guessing it into the curated group would advertise a review that may not have
 * happened; guessing it into `community` would defame a source. The middle rank
 * makes no claim, which matches what the badge does with the same value.
 */
export function reviewRank(review: string | undefined): number {
  if (review === 'curated') return 0
  if (review === 'community') return 2
  return 1
}

/**
 * Sort registry rows by review tier, returning a NEW array.
 *
 * Copies rather than sorting in place: the input is normally a React Query cache
 * array, and mutating it would reorder the cached value other consumers read.
 * `Array.prototype.sort` is stable per spec (ES2019+), which is what preserves
 * the backend's relative order inside each tier.
 */
export function orderByReview<T extends ReviewRanked>(rows: readonly T[]): T[] {
  return [...rows].sort((a, b) => reviewRank(a.review) - reviewRank(b.review))
}
