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

/**
 * Slot id from context ONLY, with no Redux fallback. For leaf renderers that
 * must stay mountable outside the app shell (DiffBlock renders in bare unit
 * tests and could be embedded elsewhere): with a provider they get the slot,
 * without one they get null and degrade gracefully. ChatPage provides the
 * slot at its root, deriving the value through useSlotId so an enclosing
 * pane's provider is respected rather than shadowed.
 */
export function useContextSlotId(): string | null {
  return useContext(SlotContext) ?? null
}

/**
 * Marks a subtree whose COMPOSER drains inline review-comment drafts (renders
 * the pending bar and attaches drafts to the next send). DiffBlock offers the
 * drafting gutter only inside such a subtree: a surface with its own send
 * path that does not drain (ChatPane's split-view panes, SideChat) would
 * otherwise let drafts accumulate invisibly and never send. Default false, so
 * a new chat-like surface has to opt in by wiring the drain first.
 */
const ReviewSurfaceContext = createContext<boolean>(false)

/** Marks a subtree as a review surface (`value` defaults to true). A host
 * whose composer DRAINS review drafts provides `true` at its root, then
 * re-provides `value={false}` around any nested chat surface whose composer
 * does NOT drain — split-view panes, the side chat — because a plain boolean
 * context can only be overridden, never scoped, and those hosts mount inside
 * the draining page's tree. One owner for "is this a review surface": every
 * gutter, chip, bar and send-enablement decision reads this context and
 * nothing else. */
export function ReviewSurfaceProvider({ children, value = true }: { children: ReactNode; value?: boolean }) {
  return <ReviewSurfaceContext.Provider value={value}>{children}</ReviewSurfaceContext.Provider>
}

/** True when the nearest chat surface drains review-comment drafts. */
export function useReviewSurface(): boolean {
  return useContext(ReviewSurfaceContext)
}
