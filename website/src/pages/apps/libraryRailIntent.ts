/**
 * A request from the sidebar's app menu for the Library page to act on one app.
 *
 * It travels ONLY as router state, never as a query parameter, for the reason
 * `AppDetailPage` gives for `autoAction`: a cross-site page can navigate a
 * signed-in browser to any dashboard URL, and router state can only come from
 * an in-app `navigate()` call. Uninstall still opens the Library's own
 * confirmation dialog, so no request here removes anything by itself.
 */
export type LibraryRailAction = 'focus' | 'disable' | 'uninstall'

export interface LibraryRailIntent {
  app: string
  action: LibraryRailAction
}

const ACTIONS: readonly LibraryRailAction[] = ['focus', 'disable', 'uninstall']

/** The router state that carries *intent* to `/apps/library`. */
export function libraryRailState(intent: LibraryRailIntent): { railIntent: LibraryRailIntent } {
  return { railIntent: intent }
}

/** The intent in a location's router state, or null when it carries none. */
export function readLibraryRailIntent(state: unknown): LibraryRailIntent | null {
  if (!state || typeof state !== 'object') return null
  const raw = (state as { railIntent?: unknown }).railIntent
  if (!raw || typeof raw !== 'object') return null
  const { app, action } = raw as { app?: unknown; action?: unknown }
  if (typeof app !== 'string' || !app) return null
  if (!ACTIONS.includes(action as LibraryRailAction)) return null
  return { app, action: action as LibraryRailAction }
}
