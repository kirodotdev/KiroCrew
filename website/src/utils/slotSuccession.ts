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

/** Oldest UNREFERENCED entries are evicted first; a `Map` iterates in insertion order. */
const MAX_TRACKED = 64

/** How many in-flight operations still resolve through a slot. A count, because two uploads
 *  started from the same slot settle independently. */
const pinned = new Map<string, number>()

/**
 * Every key an in-flight operation can still walk through.
 *
 * Pinning one slot is not enough: a completion resolves a CHAIN, so evicting an intermediate
 * edge would strand it just as evicting its own would. Doubles as the cycle guard, since a key
 * already collected is not walked twice.
 */
function referencedKeys(): Set<string> {
  const out = new Set<string>()
  for (const start of pinned.keys()) {
    let current: string | undefined = start
    while (current !== undefined && !out.has(current)) {
      out.add(current)
      current = successors.get(current)
    }
  }
  return out
}

/**
 * Hold `slot`'s mapping against eviction until the caller releases it.
 *
 * Callers MUST release in a `finally`: a leaked pin makes its chain permanently unevictable,
 * which is the unbounded growth `MAX_TRACKED` exists to prevent.
 */
export function pinSlotSuccession(slot: string | null | undefined): void {
  if (!slot) return
  pinned.set(slot, (pinned.get(slot) ?? 0) + 1)
}

/** Release one pin taken by `pinSlotSuccession`. */
export function releaseSlotSuccession(slot: string | null | undefined): void {
  if (!slot) return
  const held = pinned.get(slot)
  if (held === undefined) return
  if (held <= 1) pinned.delete(slot)
  else pinned.set(slot, held - 1)
}

export function recordSlotSuccession(from: string, to: string): void {
  if (!from || !to || from === to) return
  if (successors.size >= MAX_TRACKED) {
    // Reclaims the oldest entry no operation needs. A fully-referenced table may exceed the cap,
    // bounded then by in-flight work -- dropping a referenced row is the loss this bound is not for.
    const referenced = referencedKeys()
    for (const key of successors.keys()) {
      if (!referenced.has(key)) {
        successors.delete(key)
        break
      }
    }
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
  // Bounded by the LIVE table size rather than `MAX_TRACKED`, because a table whose every entry
  // is referenced may exceed the cap; that many hops still exhausts any acyclic chain in it.
  for (let hop = 0; hop < successors.size; hop++) {
    const next = successors.get(current)
    if (!next || seen.has(next)) return current
    seen.add(next)
    current = next
  }
  // Unreachable for any chain this table can hold, and kept as the fail-closed backstop: a
  // still-continuing chain means `current` is a KNOWN-retired intermediate, so refuse it.
  return successors.get(current) ? null : current
}

export function clearSlotSuccession(): void {
  successors.clear()
  pinned.clear()
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
