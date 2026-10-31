/**
 * What the chat's side panel lends a `contributes.panelTabs` body.
 *
 * A tab body is an app bundle with no way to reach the panel it sits in: it
 * cannot open a file in the panel's editor or know which project the chat is
 * about. The host passes both through this context, so a tab that lists
 * locations (a search, an outline, a server's answer) can open one where the
 * reader expects, next to itself, rather than only naming a path.
 *
 * Absent outside a side-panel tab: a routed app page has no panel, and
 * `useSidePanelHost()` returns `null` there so a bundle can render for both.
 */
import { createContext, useContext } from 'react'

export interface SidePanelHost {
  /** The chat's project directory, when it has one. */
  projectDir?: string
  /**
   * Open a file as a tab in this panel, at a 1-based line when given. The
   * path is handed to the host, which applies its own reachability rules; a
   * refused path shows the host's notice, not the app's.
   */
  openFile: (path: string, opts?: { line?: number }) => void
}

export const SidePanelHostContext = createContext<SidePanelHost | null>(null)

/** The side panel a tab body is mounted in, or `null` on any other surface. */
export function useSidePanelHost(): SidePanelHost | null {
  return useContext(SidePanelHostContext)
}
