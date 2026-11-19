/**
 * Route elements the table in `App.tsx` composes. The `<Route>` entries stay in
 * `App.tsx` itself (the feature-map gate counts them there); what lives here is
 * what those entries mount: the lazy-page wrapper every route-only page rides,
 * and the redirects that keep an old bookmark's query string.
 */
import { useEffect, type ComponentType } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import { SquareTerminal } from 'lucide-react'
import { lazyRetry } from '../components/lazyRetry'
import { EmptyState } from '../components/ui'
import { useViewerIsNotOwner } from '../hooks/sharedCrew/useSharedCrew'
import { isBottomTerminalOpen, openBottomTerminal } from '../hooks/useBottomTerminal'
import { i18nT } from '../i18n/t'
import { shareTargetChatQuery } from '../utils/shareTarget'

/**
 * A route page loaded on first navigation, rendering nothing until its chunk
 * arrives. The Suspense boundary lives inside the returned component, so the
 * `<Route>` entries that mount these pages read the same as for an eager page.
 *
 * The chunk fetch can reject (gateway unreachable, stale chunk after a rebuild
 * once main.tsx's `vite:preloadError` reload guard has bailed). React surfaces
 * a rejected lazy import as a render throw, and an eager page could never
 * fail that way -- so the page carries its own route-scoped ErrorBoundary:
 * the route area shows the recoverable error card while the shell (rail,
 * top bar, other routes) stays mounted instead of the throw reaching the
 * root `app-shell` boundary in main.tsx and replacing the whole dashboard.
 * The card's Try Again and Reload page come from `lazyRetry`.
 */
export function lazyPage(load: () => Promise<{ default: ComponentType }>): ComponentType {
  return lazyRetry(load, {
    layout: 'page',
    title: () => i18nT('components.errorBoundary.lazy_page_load_failed'),
    scope: 'lazy-route',
  })
}

/** `/connections`: the Connections page is the Settings tab; the old
 *  `?view=mcp-servers` named the MCP Servers list, now Customize → MCP Servers. */
export function ConnectionsRedirect() {
  const { search, hash } = useLocation()
  // Literal targets: the UI index reads the first one as where this route lands.
  if (new URLSearchParams(search).get('view') !== 'mcp-servers') return <Navigate to={'/settings/connections' + search + hash} replace />
  return <Navigate to="/capabilities?tab=mcp-servers" replace />
}
/** `/mcp`: MCP Servers is a Customize tab beside Skills. `?view=` and the
 *  hash ride along, so `/mcp?view=sharing` still opens the Sharing view. */
export function McpRedirect() {
  const { search, hash } = useLocation()
  const rest = new URLSearchParams(search)
  rest.delete('tab')
  const query = rest.toString()
  // Literal target first: the UI index reads it as where this route lands.
  return <Navigate to={'/capabilities?tab=mcp-servers' + (query ? '&' + query : '') + hash} replace />
}
export function TasksRedirect() { const { search } = useLocation(); return <Navigate to={'/projects' + search} replace /> }
export function ChatRedirect() { const { search } = useLocation(); return <Navigate to={'/chat' + search} replace /> }
export function OrchestratedRedirect() { const { slug } = useParams(); const { search } = useLocation(); return <Navigate to={`/chat${slug ? '/' + slug : ''}${search}`} replace /> }

/** `/terminal`: the Terminal is the docked panel, not a page. The owner lands on
 *  chat with the panel open; a teammate, whom the Terminal is never offered to,
 *  gets a short page saying so instead of being dropped on a chat with no word.
 *  Nothing happens until the owner check answers, so neither case flashes. */
export function TerminalRoute() {
  const { search } = useLocation()
  const { notOwner, owner, failed } = useViewerIsNotOwner()
  const opens = !notOwner && (owner || failed)
  useEffect(() => {
    if (opens && !isBottomTerminalOpen()) openBottomTerminal()
  }, [opens])
  if (notOwner) {
    return (
      <div className="flex-1 flex items-center justify-center p-6" data-testid="terminal-owner-only">
        <EmptyState
          icon={<SquareTerminal size={20} />}
          title={i18nT('app.terminal')}
          subtitle={i18nT('app.terminal_owner_only')}
        />
      </div>
    )
  }
  if (!opens) return null
  return <Navigate to={'/chat' + search} replace />
}

/** `/share`: the installed app's share target. A share from another app opens a new chat with the shared text in its composer, unsent. */
export function ShareTargetRedirect() { const { search } = useLocation(); return <Navigate to={'/chat' + shareTargetChatQuery(search)} replace /> }
