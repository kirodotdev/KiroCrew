/**
 * Where an in-flight async result should land when the slot that started it has been REPLACED.
 *
 * An upload captures its slot at click time and writes to that slot on completion. A mode
 * switch retires that slot and creates a successor, so a capture overlapping the switch lands
 * its attachment in a bucket the switch has just deleted -- the file is uploaded, charged, and
 * unreachable. The switch records the succession here so the completion can resolve the slot
 * that is actually alive.
 *
 * Chains are followed, because two switches in a row make the first successor stale too, and
 * both the chain length and the table are bounded: a long-lived tab must not accumulate a slot
 * map, and a cycle -- which a re-used slot key could produce -- must not spin.
 */

const successors = new Map<string, string>()

/** Oldest entries are evicted first; a `Map` iterates in insertion order. */
const MAX_TRACKED = 64

/** Bounds the walk even if the table somehow describes a cycle. */
const MAX_CHAIN = 16

export function recordSlotSuccession(from: string, to: string): void {
  if (!from || !to || from === to) return
  if (successors.size >= MAX_TRACKED) {
    const oldest = successors.keys().next().value
    if (oldest !== undefined) successors.delete(oldest)
  }
  successors.set(from, to)
}

/**
 * The live slot for `slot`, following any recorded replacements. Absence passes through
 * unchanged so callers can hand the result straight to `fileLandingSlot`, which already
 * treats a missing slot as "drop".
 */
export function resolveSlotSuccession(slot: string | null | undefined): string | null | undefined {
  if (!slot) return slot
  let current = slot
  const seen = new Set<string>([current])
  for (let hop = 0; hop < MAX_CHAIN; hop++) {
    const next = successors.get(current)
    if (!next || seen.has(next)) break
    seen.add(next)
    current = next
  }
  return current
}

export function clearSlotSuccession(): void {
  successors.clear()
}

/**
 * Forget a recorded replacement, for a deletion that was REJECTED.
 *
 * The record is written before the delete is awaited, so a completion landing during the
 * await retargets. If the delete then fails, the original slot is still alive and still owns
 * its work -- so retargeting its uploads to the replacement would be the same loss in the
 * other direction.
 */
export function forgetSlotSuccession(from: string): void {
  if (!from) return
  successors.delete(from)
}
