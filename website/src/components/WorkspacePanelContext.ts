import { createContext } from 'react'

/** The routed chat reports when search occupies the workspace panel's place. */
export const WorkspacePanelContext = createContext<(open: boolean) => void>(() => {})

/** Null outside the dashboard, where file previews retain their own fullscreen.
 *  The workspace panel renders the fullscreen control from this: it is the
 *  panel's own action, so it lives in the panel's action group, not the shell's. */
export const WorkspaceFullscreenContext = createContext<{
  fullscreen: boolean
  exit: () => void
  toggle: () => void
} | null>(null)
