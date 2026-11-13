import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

interface NewFolderMoveContextValue {
  /** The slot the "New folder…" dialog is open for, or null when closed. */
  slotKey: string | null
  /** Open the dialog for a slot: name a folder, create it, move the slot into it. */
  open: (slotKey: string) => void
  /** Close the dialog. */
  close: () => void
}

// Null outside the provider, unlike the tag popover's no-op default: a session
// menu rendered with no dialog host leaves its "New folder…" entry out rather
// than showing an entry that does nothing when picked.
const NewFolderMoveContext = createContext<NewFolderMoveContextValue | null>(null)

/**
 * ChatPage-scoped holder for "which slot's New folder… dialog is open". The
 * entry lives in the session menus' "Move to folder…" submenu, and a Radix menu
 * unmounts its content the moment an item is picked, so the dialog cannot live
 * inside the menu: it renders once, beside this provider in ChatPageFrame, and
 * the menus open it through this context (which flows through Radix menu portals).
 */
export function NewFolderMoveProvider({ children }: { children: ReactNode }) {
  const [slotKey, setSlotKey] = useState<string | null>(null)
  const value = useMemo<NewFolderMoveContextValue>(() => ({
    slotKey,
    open: (k: string) => setSlotKey(k),
    close: () => setSlotKey(null),
  }), [slotKey])
  return <NewFolderMoveContext.Provider value={value}>{children}</NewFolderMoveContext.Provider>
}

/** The dialog's open state, or null when no provider hosts the dialog. */
export function useNewFolderMove(): NewFolderMoveContextValue | null {
  return useContext(NewFolderMoveContext)
}
