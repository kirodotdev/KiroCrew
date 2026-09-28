import { createContext, useContext, type ReactNode } from 'react'

/**
 * How the chat page's sessions drawer gets the main-navigation rail on a phone.
 *
 * Below the mobile breakpoint the chat page shows ONE drawer: a 72px icon rail
 * (the app's main destinations) beside the sessions pane. The rail is the
 * shell's — its rows come from the same nav registry, badges and active-state
 * rules the desktop rail and the mobile nav drawer render, so it is rendered by
 * `App.tsx` and handed down here rather than rebuilt from a second list inside
 * the chat page. `App.tsx` provides a renderer only while the chat route is
 * mounted on a phone; every other surface reads `null` and renders no rail.
 *
 * A render FUNCTION, not a node: the drawer owns the close gesture, so it has
 * to say what a row tap that STAYS on the chat page does to it (`onActivate`
 * closes the drawer — the active Chat row, Search). A row that navigates away
 * unmounts the chat page and its drawer with it, and replaces the drawer's
 * duplicate history entry so Back returns to the chat; that is the renderer's
 * own rule, not an option here.
 */
export type MobileNavRailOptions = {
  /** Close the hosting drawer without navigating. */
  onActivate: () => void
}

export type MobileNavRailRenderer = (opts: MobileNavRailOptions) => ReactNode

export const MobileNavRailContext = createContext<MobileNavRailRenderer | null>(null)

export function useMobileNavRail(): MobileNavRailRenderer | null {
  return useContext(MobileNavRailContext)
}
