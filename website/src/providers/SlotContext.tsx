import { createContext, useContext, type ReactNode } from 'react'
import { useAppSelector } from '../store'

/**
 * SlotContext carries the slot key for a single chat pane.
 *
 * The native session grid mounts N <ChatPane> subtrees, each wrapped in a
 * <SlotProvider slotId={key}>. Components inside a pane read THEIR pane's slot
 * via useSlotId() instead of the single global `s.chat.activeSlot`. This is the
 * seam that de-globalizes activeSlot without forking ChatPage / ChatInput.
 *
 * Backward compatibility: when NO provider is present (the normal single-pane
 * /chat page), useSlotId() falls back to the global focused slot
 * (`s.chat.activeSlot`), so every existing call site behaves exactly as before.
 *
 * The `undefined` sentinel is load-bearing: it distinguishes "no SlotProvider in
 * the tree" (fall back to global) from "a SlotProvider that supplies a null slot"
 * (an intentionally empty pane). Do not default it to null.
 */
const SlotContext = createContext<string | null | undefined>(undefined)

export function SlotProvider({ slotId, children }: { slotId: string | null; children: ReactNode }) {
  return <SlotContext.Provider value={slotId}>{children}</SlotContext.Provider>
}

/**
 * The slot key this component should bind to:
 *  - inside a <SlotProvider>: the pane's slotId (may be null for an empty pane);
 *  - outside one (single-pane page): the global focused slot s.chat.activeSlot.
 *
 * Note: activeSlot is read unconditionally to keep hook order stable; panes that
 * supply their own slotId simply ignore it. activeSlot changes are infrequent so
 * the extra subscription is negligible.
 */
export function useSlotId(): string | null {
  const ctx = useContext(SlotContext)
  const globalActive = useAppSelector((s) => s.chat.activeSlot)
  return ctx === undefined ? globalActive : ctx
}

/** True when rendering inside a multi-pane grid cell (a SlotProvider is present). */
export function useIsPaneScoped(): boolean {
  return useContext(SlotContext) !== undefined
}
