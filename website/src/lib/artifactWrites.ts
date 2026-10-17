/**
 * Which artifacts currently have a write IN FLIGHT.
 *
 * This exists for one decision: whether it is safe to delete a just-created blank
 * document the user is leaving. The server can see every write it has already
 * applied, so it does not need help with those — it re-checks the record under its
 * own lock. The single fact the server cannot know is that a request is on the
 * wire and has not landed yet, which is exactly what this tracks.
 *
 * It is deliberately central rather than a flag each handler sets. The previous
 * design asked every write path on the artifact page to announce itself, and four
 * separate review rounds each found another path that did not: a folder move in a
 * child component, a live-edit flush, a snapshot, a revert, comment resolution.
 * Every one was the same defect — an unannounced write let a document be deleted
 * while its own PATCH was still in the air — and fixing them one at a time left
 * the next new write path free to reintroduce it. Counting requests where they are
 * actually issued cannot be forgotten by a future call site.
 *
 * Counted rather than boolean because writes overlap: a tag edit and a rename can
 * both be outstanding, and the first to return must not clear the second's guard.
 */

const pending = new Map<string, number>()

/** Record that a mutating request for `slug` has been issued. */
export function beginArtifactWrite(slug: string): void {
  if (!slug) return
  pending.set(slug, (pending.get(slug) ?? 0) + 1)
}

/** Record that a mutating request for `slug` has settled (succeeded OR failed). */
export function endArtifactWrite(slug: string): void {
  if (!slug) return
  const next = (pending.get(slug) ?? 0) - 1
  if (next > 0) pending.set(slug, next)
  else pending.delete(slug)
}

/**
 * Does `slug` have at least one write still on the wire?
 *
 * A failed request also clears, which is correct: the server never applied it, so
 * the record it re-reads is authoritative either way.
 */
export function hasPendingArtifactWrite(slug: string): boolean {
  return (pending.get(slug) ?? 0) > 0
}

/** Test-only reset. */
export function __resetArtifactWrites(): void {
  pending.clear()
}
