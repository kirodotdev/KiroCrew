/**
 * Which apps' pages the shell keeps running, hidden (`KeptAppPagesProvider`).
 *
 * `AppHost` reloads the whole window when its app's files are replaced, and a
 * window reload ends every hidden kept page's work. Every host under the provider
 * reads this, wherever it is rendered (a routed page, a chat side-panel tab), and
 * shows its reload notice instead while another app's page is hidden, so the
 * reload is the person's own choice. A host outside the provider (a desktop
 * window, which is its own document) reads the empty default and reloads as before.
 */
import { createContext, useContext } from 'react'

export const HiddenKeptAppsContext = createContext<ReadonlySet<string>>(new Set())

/** Whether a page of an app other than *appName* is kept running, hidden. */
export function useReloadHeldFor(appName: string): boolean {
  const hidden = useContext(HiddenKeptAppsContext)
  for (const name of hidden) if (name !== appName) return true
  return false
}
