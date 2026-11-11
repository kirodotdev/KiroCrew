import { createContext, useContext } from 'react'

export interface RowSelection {
  /** Is this session picked for a batch action? */
  has: (slotKey: string) => boolean
  /** Pick the session, or drop it when it is picked. */
  toggle: (slotKey: string) => void
}

// Null outside the Sessions sidebar: a session menu on a surface with no row
// list (the chat header) leaves its Select entry out.
export const RowSelectionContext = createContext<RowSelection | null>(null)

/** The sidebar's batch selection, or null where no row list hosts one. */
export function useRowSelection(): RowSelection | null {
  return useContext(RowSelectionContext)
}
