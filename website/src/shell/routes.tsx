/**
 * Route elements the table in `App.tsx` composes. The `<Route>` entries stay in
 * `App.tsx` itself (the feature-map gate counts them there); what lives here is
 * what those entries mount: the lazy-page wrapper every route-only page rides,
 * and the redirects that keep an old bookmark's query string.
 */
import type { ComponentType } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import { lazyRetry } from '../components/lazyRetry'
import { i18nT } from '../i18n/t'

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
 *  `?view=mcp-servers` named the MCP Servers list, which is its own page. */
export function ConnectionsRedirect() {
  const { search, hash } = useLocation()
  // Literal targets: the UI index reads the first one as where this route lands.
  if (new URLSearchParams(search).get('view') !== 'mcp-servers') return <Navigate to={'/settings/connections' + search + hash} replace />
  return <Navigate to="/mcp" replace />
}
export function TasksRedirect() { const { search } = useLocation(); return <Navigate to={'/projects' + search} replace /> }
export function ChatRedirect() { const { search } = useLocation(); return <Navigate to={'/chat' + search} replace /> }
export function OrchestratedRedirect() { const { slug } = useParams(); const { search } = useLocation(); return <Navigate to={`/chat${slug ? '/' + slug : ''}${search}`} replace /> }
