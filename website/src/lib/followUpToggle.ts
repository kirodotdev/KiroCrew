/**
 * Pure text transforms for the follow-up option chips shared by ChatPage and
 * ChatPane (#7616). Un-toggling a chip must remove ONLY the suffix the chips
 * themselves appended — never user-typed text that merely equals it — so the
 * chips OWN a recorded span of the composer draft rather than matching by
 * content.
 *
 * The owned span is `{ base, options }`:
 *   - `base`    the draft as it stood before the first chip appended,
 *   - `options` the chip labels appended after it, in order, kept as an ARRAY.
 *
 * Options are an array, never a delimiter-joined string, because a label may
 * legally contain ", " whenever the option list is "|"-separated
 * (`[OPTIONS: Yes, proceed | No, wait]`, see parseOptions). Joining for removal
 * and splitting back would corrupt such a label; the array never splits.
 *
 * Every function here is PURE: it reads the current draft and ownership and
 * returns the next ones. The caller advances its ownership ref and sets the
 * draft SYNCHRONOUSLY in the click handler (not in a render-time state updater),
 * so nothing depends on a React functional updater running exactly once —
 * StrictMode double-invokes updaters, and a transform that mutated ownership as
 * a side effect inside one would rebase on a stale draft (the #7616 F2 defect).
 */

/** The span the chips own at the tail of the composer draft. */
export interface OwnedSuffix {
  /** The draft as it stood before the first chip appended. */
  base: string
  /** The chip labels appended after `base`, in order. Never joined for storage. */
  options: string[]
}

/** The rendered tail an owned span produces: base + ", " + each option. */
function renderTail(owned: OwnedSuffix): string {
  const suffix = owned.options.join(', ')
  if (!owned.base) return suffix
  return suffix ? owned.base + ', ' + suffix : owned.base
}

export interface ToggleResult {
  /** The composer draft after the toggle. */
  value: string
  /** The owned span after the toggle (null when the chips own nothing). */
  owned: OwnedSuffix | null
}

/**
 * Append one option to the draft and record it as owned.
 *
 * Extends the previously-owned span only when it is still intact at the tail of
 * the live draft; if the user edited it, the current draft becomes the new base
 * so appends keep tracking real text and never resurrect a stale base.
 * Ownership is established ONLY here, so text the user types is never owned and
 * the un-toggle path can never remove it.
 */
export function appendFollowUpOption(prev: string, owned: OwnedSuffix | null, option: string): ToggleResult {
  if (owned && owned.options.length > 0 && prev === renderTail(owned)) {
    const nextOwned: OwnedSuffix = { base: owned.base, options: [...owned.options, option] }
    return { value: renderTail(nextOwned), owned: nextOwned }
  }
  const base = prev.trimEnd()
  const nextOwned: OwnedSuffix = { base, options: [option] }
  return { value: renderTail(nextOwned), owned: nextOwned }
}

/**
 * Remove one option from the draft, by OWNERSHIP not content.
 *
 * The draft is only changed when its tail still equals the exact owned span; if
 * the user edited it, the text is left untouched (the caller still un-highlights
 * the chip). Only the owned option array is consulted for what remains, so a
 * comma-bearing label is removed as one element and user text equal to the
 * suffix is never touched.
 */
export function removeFollowUpOption(prev: string, owned: OwnedSuffix | null, option: string): ToggleResult {
  if (!owned) return { value: prev, owned: null }
  if (prev !== renderTail(owned)) return { value: prev, owned }  // user edited — leave text
  const options = owned.options.filter(p => p !== option)
  if (options.length === 0) {
    return { value: owned.base, owned: owned.base ? { base: owned.base, options: [] } : null }
  }
  const nextOwned: OwnedSuffix = { base: owned.base, options }
  return { value: renderTail(nextOwned), owned: nextOwned }
}

