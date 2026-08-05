import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

interface TagPopoverContextValue {
  /** The slot whose tag picker is open, or null when closed. */
  slotKey: string | null
  /** Open the tag picker for a slot. */
  open: (slotKey: string) => void
  /** Close the tag picker. */
  close: () => void
}

// Default is a no-op so a stray consumer rendered outside the provider degrades
// to "the picker never opens" rather than crashing. In the app the provider
// always wraps ChatPage, which hosts every trigger surface (the sidebar row
// menus + the chat-header menu) and the single <SlotTagPopover>.
const noop = () => {}
const TagPopoverContext = createContext<TagPopoverContextValue>({ slotKey: null, open: noop, close: noop })

/**
 * ChatPage-scoped holder for "which slot's tag picker is open". Both trigger
 * surfaces and the single <SlotTagPopover> render under ChatPage, so this keeps
 * the ephemeral open-state local to that subtree instead of in the global Redux
 * store. Context flows through Radix menu portals, so the "Tags…" item inside a
 * portaled menu still reaches this provider. `initialSlotKey` seeds the state
 * for tests/storybook.
 */
export function TagPopoverProvider({ children, initialSlotKey = null }: { children: ReactNode; initialSlotKey?: string | null }) {
  const [slotKey, setSlotKey] = useState<string | null>(initialSlotKey)
  const value = useMemo<TagPopoverContextValue>(() => ({
    slotKey,
    open: (k: string) => setSlotKey(k),
    close: () => setSlotKey(null),
  }), [slotKey])
  return <TagPopoverContext.Provider value={value}>{children}</TagPopoverContext.Provider>
}

export function useTagPopover(): TagPopoverContextValue {
  return useContext(TagPopoverContext)
}
