/** The chat-jump shortcut order, read back from the rendered rows after every commit
 *  and published to the store, plus the digit badges shown while the modifier is held. */
import { useState, useEffect, useRef, useMemo, type RefObject } from 'react'
import { SESSION_ROW_SELECTOR } from '../chat/sessionRowNav'
import { useDigitModifierHeld, jumpLabelFor } from '../../hooks/useKeyboardShortcuts'
import { setSidebarOrder } from '../../store/dashboardSlice'
import type { AppDispatch } from '../../store'
import type { Slot } from './types'

/** The rendered row order for the chat-jump shortcuts and its digit badges. */
export function useShortcutOrder({ sidebarRootRef, dispatch, localSlots }: {
  sidebarRootRef: RefObject<HTMLDivElement>
  dispatch: AppDispatch
  localSlots: Slot[]
}) {
  // The order the chat-jump/cycle shortcuts should follow — the rows AS
  // RENDERED, read back from the DOM after every commit. Reading the render
  // output (instead of re-deriving each lane's composition) means the
  // published order can never drift from what the user sees: folder tree
  // order, collapsed folders (children absent), filters, flat view and board
  // columns all fall out of document order for free. Every session row is
  // stamped data-session-row={key} in exactly one place (renderSessionRow);
  // history rows use a separate renderer and are never captured. The no-deps
  // effect runs after every commit but is double-guarded: setState bails on
  // an order-identical array, and an empty read (sidebar collapsed, or a
  // filter matching nothing) keeps the last-known order. For an unmounted
  // sidebar that preserves the pre-existing behavior; for a rendered sidebar
  // whose filter matches nothing it is a deliberate change from the old
  // memo (which published the empty list, falling back to store order) —
  // stale keys are dropped by both consumers, while backend insertion order
  // would be actively wrong.
  const [shortcutOrderKeys, setShortcutOrderKeys] = useState<string[]>([])
  // eslint-disable-next-line react-hooks/exhaustive-deps -- run-after-every-commit is the point: the order is READ BACK from the DOM, and any dep list would be a re-derivation that can drift from what actually rendered (the drift this effect exists to eliminate). `[]` would freeze the order at mount. The update chain terminates because shortcutOrderKeys only feeds row BADGES — it never adds, removes or inerts a data-session-row node — so the second pass reads an identical order and the setState updater returns `prev`, which React bails out on.
  useEffect(() => {
    const root = sidebarRootRef.current
    if (!root) return
    const rawKeys = Array.from(root.querySelectorAll(SESSION_ROW_SELECTOR))
      // A row inside a collapsed folder stays MOUNTED (FolderBody animates
      // height rather than unmounting) but is marked aria-hidden + inert —
      // the component's own visibility contract. Rows a user cannot see or
      // click must not be digit targets; the jump handler appends them after
      // the published list so cycling still reaches them. [inert] alone is
      // the canonical "hidden row" spelling (matching sessionRowsInScope);
      // FolderBody always sets it together with aria-hidden.
      .filter(el => !el.closest('[inert]'))
      .map(el => el.getAttribute('data-session-row') ?? '')
      .filter(Boolean)
    // Board view renders a multi-tag session once per matching column, so the
    // same key can appear several times in document order. Dedupe to FIRST
    // occurrence: the jump handler (orderSlotsBySidebar) already collapses to
    // first-wins, and the badge map must number the same list or a duplicated
    // row's badge and its digit's target drift apart.
    const keys = Array.from(new Set(rawKeys))
    if (keys.length === 0) return
    setShortcutOrderKeys(prev =>
      prev.length === keys.length && prev.every((v, i) => v === keys[i]) ? prev : keys,
    )
  })
  // Freeze the shortcut order while the jump modifier is held. Under a
  // last-activity sort, background agent events (touchSlotActivity recency
  // bumps) re-sort the list at any moment; without the freeze, the digits
  // reassign between the user aiming at a badge and pressing it, so the press
  // lands on whatever row REPLACED the one they read. Frozen, the badge map
  // and the published store order both derive from the same held snapshot:
  // badges travel with their rows if the visual order shifts mid-hold, and
  // the digit picks the session the user saw. Render-time ref write is the
  // same derived-state pattern ChatPage uses for filteredSlotsRef; the
  // `.length` guard re-arms the freeze if the modifier was held before the
  // first slots frame arrived.
  const digitModifierHeld = useDigitModifierHeld()
  const heldOrderRef = useRef<string[] | null>(null)
  if (!digitModifierHeld) heldOrderRef.current = null
  else heldOrderRef.current ??= (shortcutOrderKeys.length ? shortcutOrderKeys : null)
  const effectiveOrderKeys = heldOrderRef.current ?? shortcutOrderKeys
  // Publish to the store for useKeyboardShortcuts (which reads at keypress
  // time). Diff-guarded so slot-detail churn that doesn't reorder rows never
  // dispatches. Deliberately not cleared on unmount: a last-known display
  // order beats falling back to backend insertion order while the sidebar is
  // collapsed.
  const lastPublishedOrderRef = useRef('')
  useEffect(() => {
    const joined = effectiveOrderKeys.join('\n')
    if (joined === lastPublishedOrderRef.current) return
    lastPublishedOrderRef.current = joined
    dispatch(setSidebarOrder(effectiveOrderKeys))
  }, [effectiveOrderKeys, dispatch])

  // First sessions in shortcut order → their jump label ('1'–'9', then the
  // letter sequence — see jumpLabelFor), shown as row badges while the jump
  // modifier is held (Ctrl on Mac in Ctrl+digit mode, Alt elsewhere —
  // mirrors the jump chords).
  const shortcutDigitByKey = useMemo(() => {
    // Compact the frozen order exactly like the jump handler's
    // orderSlotsBySidebar does — drop keys whose session no longer exists —
    // BEFORE assigning labels. If a session closes mid-hold, the handler's
    // label N targets the Nth surviving frozen key; numbering the raw frozen
    // list instead would leave a row visibly badged "3" that chord 2 picks —
    // the exact badge/target drift this feature exists to prevent. The
    // `slots` prop is the existence basis (mirrors the handler's store
    // lookup), not the display list, so a mid-hold visibility change cannot
    // desynchronize the two consumers either.
    const live = new Set(localSlots.map(s => s.key))
    const m = new Map<string, string>()
    let idx = 0
    for (const k of effectiveOrderKeys) {
      const label = jumpLabelFor(idx)
      if (label === null) break
      if (!live.has(k)) continue
      // Letters badge unconditionally, including while a text field is
      // focused. Clicking a sidebar row autofocuses the composer, so a
      // typing-focus gate here made letters vanish the moment a session was
      // selected — the held-modifier overlay must always show the full
      // addressable range. (Letter CHORDS remain input-gated in the handler:
      // Ctrl+A/E/K are readline bindings on macOS and typing always wins.)
      m.set(k, label)
      idx++
    }
    return m
  }, [effectiveOrderKeys, localSlots])
  return { digitModifierHeld, shortcutDigitByKey }
}
