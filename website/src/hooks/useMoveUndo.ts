import { useCallback, useEffect, useRef, useState } from 'react'

import { MOVE_UNDO_MS, type MovedItem } from '../components/MoveUndoBar'

/** A live offer: one {@link MovedItem} plus the lifecycle the hook enforces. */
export type MoveUndoOffer = MovedItem & {
  /** Identity of THIS offer. Every hold, remainder and undo is keyed to it. */
  id: number
  /** One-way: false until the server acknowledges, never true→false. */
  live: boolean
  /** Latched when a placement neither we nor the origin made is observed. */
  superseded: boolean
}

/**
 * The surface-specific half of the primitive: how to read where an item is, how
 * to move it, and whether a folder still exists.
 *
 * All three are called during effects and callbacks, so a caller must keep them
 * referentially stable (`useCallback`) for the same reason the pre-extraction
 * code depended on `slots` directly: a fresh identity every render re-runs the
 * reconciliation effect, and re-running it is only free because it is idempotent.
 */
export type MoveUndoDeps = {
  /**
   * Where `itemKey` sits right now.
   *
   * `null` means unfiled root. `undefined` means the item is GONE — that
   * distinction is load-bearing: a closed session has nothing to put back and
   * must retire the offer, while an unfiled one is a legitimate placement.
   */
  locate: (itemKey: string) => string | null | undefined
  /**
   * Performs the move. Must be optimistic (so the destination is visible at
   * once) and must call `onCommitted` only once the SERVER has acknowledged.
   */
  apply: (itemKey: string, folderId: string | null, opts?: { onCommitted?: () => void }) => void
  /** Whether a folder id is still real; undo degrades to unfiled when it is not. */
  folderExists: (folderId: string) => boolean
}

export type MoveUndoController = {
  /** The current offer, or `null`. Render the bar only when `offer.live`. */
  offer: MoveUndoOffer | null
  /** Perform a drag-move AND park its inverse. A no-op move arms nothing. */
  arm: (moved: MovedItem) => void
  /** Replay the inverse of `offerId`, if that is still the live offer. */
  undo: (offerId: number) => void
  /**
   * Retire the current offer (pending or live) without replaying anything.
   * One-way to gone, like every other exit. For a surface that owns SEVERAL
   * controllers over one visual slot: arming one dismisses the others, so a
   * displaced offer is retired rather than merely hidden — a hidden-but-live
   * offer would resurrect when the winner retires, and its exiting bar would
   * hold a second ⌘Z listener able to undo a move the user no longer sees.
   */
  dismiss: () => void
  /** Props the bar needs from its owner: the remainder, and the hold channel. */
  bar: {
    remainingMs: number
    paused: boolean
    onHoldChange: (held: boolean) => void
  }
}

// Offer identity. Module-scoped and monotonic ACROSS hook instances, so two
// controllers sharing one visual slot can never mint equal ids — Date.now()
// could, within one millisecond, and equal ids make "which offer is newer"
// undecidable at exactly the moment two surfaces raced.
let nextOfferId = 1

/**
 * "Last reversible move + its inverse", owned in one place.
 *
 * A drag is the one folder move a user can make WITHOUT naming the destination:
 * drop an item a row off and it disappears into a folder they never chose, with
 * nothing on screen saying where it went. So every DRAG-initiated move parks its
 * inverse here and {@link MoveUndoBar} offers it back. Moves that name their
 * destination (a "Move to folder…" menu) do not arm it.
 *
 * ## Why this is a hook and not a copy per surface
 *
 * The offer's lifecycle is the whole feature, and it is almost entirely made of
 * guards against races that are invisible in a happy-path read:
 *
 *  - `live` flips true only on the server's ACKNOWLEDGEMENT. Waiting is
 *    load-bearing, not caution: the move is optimistic, so the store shows the
 *    destination immediately, and an offer that went live on that would let the
 *    user undo while the original PATCH is still in flight. Undo's write would
 *    be applied against the old folder and the original write would then land —
 *    silently reversing the undo the user just asked for.
 *  - Once live, the offer is DROPPED — never re-validated — the moment live
 *    state stops matching. Deriving visibility from live state instead let a
 *    dropped offer come back: drag A→B, then move B→C→B from a row menu, and the
 *    old A inverse matched again and would have overwritten the newer,
 *    intentional move.
 *  - `superseded` latches a third-party placement seen during the pending
 *    window, because by ack time live state may match the destination again (a
 *    move away and back) and nothing later could tell.
 *
 * Every surface that arms an undo needs all three, and a second hand-rolled copy
 * would get one of them subtly wrong. Callers supply only {@link MoveUndoDeps}.
 */
export default function useMoveUndo({ locate, apply, folderExists }: MoveUndoDeps): MoveUndoController {
  const [offer, setOffer] = useState<MoveUndoOffer | null>(null)

  // Read through a ref, not the closure: AnimatePresence keeps the retired bar
  // mounted for its 150ms exit, and that instance still holds the props (and the
  // captured state) it had while live. A click or ⌘Z in that window would fire a
  // stale undo and overwrite the newer placement, so the offer's identity is
  // re-checked against CURRENT state at invocation time.
  const offerRef = useRef(offer)
  offerRef.current = offer

  const arm = useCallback((moved: MovedItem) => {
    // A drop back onto the folder the item already sits in is not a move —
    // arming undo for it would offer to undo nothing.
    if (moved.fromFolderId === moved.toFolderId) return
    const id = nextOfferId++
    setOffer({ ...moved, id, live: false, superseded: false })
    // No failure branch is needed: a move that never lands never acknowledges,
    // so the offer never goes live and the deadline clears the record. There is
    // no path from "failed" back to a visible bar.
    apply(moved.itemKey, moved.toFolderId, {
      onCommitted: () => setOffer(m => (
        m && m.id === id
          // A mismatch latched during the pending window means someone else's
          // move landed inside it, so this inverse is already stale — drop the
          // offer instead of arming it on an ack that is no longer the last word.
          ? (m.superseded ? null : { ...m, live: true })
          : m
      )),
    })
  }, [apply])

  const dismiss = useCallback(() => { setOffer(null) }, [])

  const undo = useCallback((offerId: number) => {
    const current = offerRef.current
    if (!current || current.id !== offerId) return
    // Unconditional write, matching every other folder move in the product (the
    // row menus, the session header, the drag itself). The offer's own lifecycle
    // is what keeps it honest: it arms only on the server's acknowledgement, the
    // pending window latches any placement it did not make, and an armed offer is
    // dropped the moment live state stops matching its destination. What remains
    // is a move this client has not been told about yet — the same broadcast gap
    // every other write here lives with, where a wrong undo is visible on screen
    // and re-correctable.
    //
    // A `fromFolderId` whose folder was DELETED meanwhile is degraded to unfiled
    // rather than replayed: the endpoint rejects an unknown id with 400, and the
    // sidebar already renders an unknown folder as unfiled, so posting it would
    // leave Undo doing nothing at all.
    const origin = current.fromFolderId && folderExists(current.fromFolderId)
      ? current.fromFolderId
      : null
    apply(current.itemKey, origin)
    setOffer(null)
  }, [folderExists, apply])

  // The deadline lives HERE, not in the bar: an offer whose optimistic move
  // never became visible (the request failed and rolled back) has no bar to run
  // a timer, and must still die on the same clock rather than linger where a
  // later, unrelated move could make it match again.
  // Suspended while the pointer is over the bar or focus is inside it: the
  // deadline must not expire under a hand that is already reaching for Undo,
  // which would take the affordance away from exactly the slower reader it
  // exists for — and the footer shifts up into the spot the button just left.
  // The hold and the remainder are both keyed to the OFFER they belong to, and
  // an id that does not match the live offer reads as "full, running". A new
  // drag therefore cannot inherit a suspended clock (the pointer never leaves a
  // bar that is REPLACED, so nothing else would clear the hold) or a part-spent
  // window — by construction, rather than by a reset a later edit could forget.
  // That cross-offer case carries no test: a second drag needs a board drop
  // zone, and the zones unmount once the first move lands. Hence the shape above
  // over an explicit reset — there is no branch left to get wrong.
  const [heldOffer, setHeldOffer] = useState<number | null>(null)
  const [spent, setSpent] = useState<{ id: number; remaining: number } | null>(null)
  const paused = offer != null && heldOffer === offer.id
  const remainingMs = offer && spent?.id === offer.id ? spent.remaining : MOVE_UNDO_MS
  const deadlineRef = useRef(0)
  useEffect(() => {
    if (!offer) return
    if (paused) {
      setSpent({ id: offer.id, remaining: Math.max(0, deadlineRef.current - Date.now()) })
      return
    }
    deadlineRef.current = Date.now() + remainingMs
    const timer = setTimeout(() => setOffer(null), remainingMs)
    return () => clearTimeout(timer)
    // Keyed on the offer's id and the hold ALONE: flipping `live` must not
    // restart the clock, and neither must the remainder this effect writes when
    // it freezes — that write is the input to the NEXT resume, not a new window.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offer?.id, paused])

  useEffect(() => {
    if (!offer) return
    const here = locate(offer.itemKey)
    if (here === undefined) { setOffer(null); return }   // item gone — nothing to put back
    if (offer.live) {
      if (here !== offer.toFolderId) setOffer(null)
      return
    }
    // Still PENDING the server's acknowledgement. Two placements are legitimate
    // here: the ORIGIN (our optimistic write has not been applied or was rolled
    // back) and the DESTINATION (it has). Any third value is another client's
    // move landing inside our window, and it must not be forgotten just because
    // the offer is not armed yet: the ack that follows would otherwise arm an
    // inverse that now overwrites that newer placement. Latch it instead.
    if (here !== offer.fromFolderId && here !== offer.toFolderId && !offer.superseded) {
      setOffer(m => (m && m.id === offer.id ? { ...m, superseded: true } : m))
    }
  }, [offer, locate])

  // Carries the id that was current when THIS callback was handed to a bar, so a
  // retiring bar's hover reports the retired offer (which matches nothing and
  // reads as "full, running") instead of freezing a newer offer's clock.
  const currentId = offer?.id ?? null
  const onHoldChange = useCallback((held: boolean) => {
    setHeldOffer(held ? currentId : null)
  }, [currentId])

  return { offer, arm, undo, dismiss, bar: { remainingMs, paused, onHoldChange } }
}
