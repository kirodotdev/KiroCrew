/**
 * Boundary key carrying FULL data identity (#3702).
 *
 * `ErrorBoundary` latches its error state, so the key must change whenever the
 * row's data changes — that is what remounts a boundary whose card threw once
 * the registry payload is corrected. Keying on selected fields (name, version,
 * icon, …) is whack-a-mole: the fix re-latches whenever the crashing field is
 * one the key does not carry (e.g. a same-version icon correction). Serializing
 * the whole row makes "any field changed" the remount condition, and an
 * identical refetch produces the identical string, so nothing remounts
 * spuriously. Rows come from React Query's cache via `useMemo`, so references
 * are stable across unrelated re-renders — the WeakMap makes the serialization
 * once per distinct row object.
 *
 * Shared by the Discover and Library pages (the PR1 App Store split), so the
 * remount contract cannot drift between the two lists.
 */
const cardKeyCache = new WeakMap<object, string>()

export function cardDataKey(row: object): string {
  let key = cardKeyCache.get(row)
  if (key === undefined) {
    key = JSON.stringify(row)
    cardKeyCache.set(row, key)
  }
  return key
}
