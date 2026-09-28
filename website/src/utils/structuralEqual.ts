/**
 * One structural-equality comparator for the JSON-shaped payloads Redux slices
 * receive from the server.
 *
 * Why it lives here rather than inside a slice: two reducers need the exact same
 * guarantees for the same reason — deciding whether an incoming payload renders
 * identically to the one already in the store, so the reducer can leave state
 * untouched and every consumer keeps its existing reference. `chatSlice` uses it
 * for message `meta`/`variants` (via `sameMessage`), `dashboardSlice` for whole
 * slot rows (via `applySlots`). A second private copy in the second slice would
 * be a second set of key-order and field-agnostic guarantees to keep in sync,
 * and the two would drift.
 *
 * Two properties are load-bearing, not incidental:
 *
 * **Key-order independent.** One side is a fresh server payload; the other may
 * be an object an in-place reducer has since patched, and a patch can append a
 * key the payload spells earlier. A serialization compare (`JSON.stringify`)
 * would call those unequal forever and silently give back the wholesale
 * replacement this exists to avoid.
 *
 * **Field-agnostic.** A comparator that listed a type's fields would stop seeing
 * a newly added one and report equal on a payload that actually changed — that
 * pins stale content on screen, which is a correctness bug, where an extra
 * re-render is only a cost.
 */
export function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false
  const aArr = Array.isArray(a), bArr = Array.isArray(b)
  if (aArr !== bArr) return false
  if (aArr && bArr) return a.length === b.length && a.every((v, i) => jsonEqual(v, b[i]))
  const ak = Object.keys(a), bk = Object.keys(b)
  if (ak.length !== bk.length) return false
  return ak.every(k => Object.prototype.hasOwnProperty.call(b, k)
    && jsonEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]))
}
