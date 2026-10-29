import { createContext, useContext, type ReactNode } from 'react'

/**
 * How the chat page's sessions drawer gets the main-navigation rail on a phone.
 *
 * Below the mobile breakpoint the chat page shows ONE drawer: a 72px icon rail
 * (the app's main destinations) beside the sessions pane. The rail is the
 * shell's — its rows come from the same nav registry, badges and active-state
 * rules the desktop rail and the mobile nav drawer render, so it is rendered by
 * `App.tsx` and handed down here rather than rebuilt from a second list inside
 * the chat page. `App.tsx` provides a renderer only while the chat route or the
 * Crewmates route is mounted on a phone (the Crewmates page hosts the rail the
 * same way, beside its roster drawer); every other surface reads `null` and
 * renders no rail.
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

/**
 * What the Crewmates page tells the shell, on a phone, about the top bar.
 *
 * The shell gives a phone `/members` visit the chat page's single top bar
 * while the page reports a crewmate or team on screen (`open`) or still being
 * read (`pending`, which is also where every visit starts). The page opens its
 * crewmate from an effect, so a bar keyed on the URL alone drew the logo row
 * for the frames before `?member=` landed, then dropped it -- a flicker on
 * every switch to the tab. Until the page reports `open` the shell keeps its
 * nav-drawer swipe on and puts a nav-drawer toggle in the bar, so the
 * navigation stays one swipe or one tap away while a slow read is still out.
 * `roster` gives the regular bar back: the page reports it
 * for the bare roster with nothing to open (an empty roster, a failed read, a
 * link that names no one) and on unmount, including through its error
 * boundary. `null` outside the shell (popout and embed frames).
 */
export type MobileRosterBarState = 'open' | 'pending' | 'roster'

export type MobileRosterBarReport = (state: MobileRosterBarState) => void

export const MobileRosterBarContext = createContext<MobileRosterBarReport | null>(null)

export function useMobileRosterBar(): MobileRosterBarReport | null {
  return useContext(MobileRosterBarContext)
}
