import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

interface MergeBackDialogContextValue {
  /** The fork whose merge dialog is open, or null when it is closed. */
  forkKey: string | null
  open: (forkKey: string) => void
  close: () => void
}

// Null outside the provider, and the menu entry renders nothing then: a visible
// "Merge into parent…" row that does nothing when clicked is worse than no row.
// ChatPage mounts the provider where it offers a fork, so the embedded chat
// surfaces that offer no fork offer no merge either.
const MergeBackDialogContext = createContext<MergeBackDialogContextValue | null>(null)

/**
 * ChatPage-scoped holder for "which fork's merge dialog is open". The triggers
 * (each session menu) and the single <MergeBackDialog> host render under
 * ChatPage, so the open state stays local to that subtree. Context flows through
 * Radix menu portals, so a menu item inside a portaled menu still reaches it.
 */
export function MergeBackDialogProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const [forkKey, setForkKey] = useState<string | null>(null)
  const value = useMemo<MergeBackDialogContextValue>(() => ({
    forkKey,
    open: (key: string) => setForkKey(key),
    close: () => setForkKey(null),
  }), [forkKey])
  return <MergeBackDialogContext.Provider value={enabled ? value : null}>{children}</MergeBackDialogContext.Provider>
}

export function useMergeBackDialog(): MergeBackDialogContextValue | null {
  return useContext(MergeBackDialogContext)
}
