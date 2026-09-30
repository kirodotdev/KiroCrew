/**
 * Route elements the table in `App.tsx` composes. The `<Route>` entries stay in
 * `App.tsx` itself (the feature-map gate counts them there); what lives here is
 * what those entries mount: the lazy-page wrapper every route-only page rides,
 * and the redirects that keep an old bookmark's query string.
 */
import { lazy, useState, Suspense, type ComponentType } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import ErrorBoundary from '../components/ErrorBoundary'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
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
 */
export function lazyPage(load: () => Promise<{ default: ComponentType }>): ComponentType {
  // Shared by every mount, so a page whose chunk already loaded renders on the
  // next visit without suspending again. Replaced only by a retry.
  let shared = lazy(load)
  function LazyPage() {
    const [{ Page, attempt }, setLoadState] = useState(() => ({ Page: shared, attempt: 0 }))
    // React.lazy caches a rejected loader. A new wrapper per attempt makes the
    // retry perform another import instead of rendering the cached rejection.
    return (
      <ErrorBoundary
        key={attempt}
        scope="lazy-route"
        fallback={(error) => (
          <div className="flex h-full items-center justify-center p-8">
            <ErrorNotice
              title={i18nT('components.errorBoundary.lazy_page_load_failed')}
              message={error.message}
              askAgent
              footer={(
                <div className="flex items-center gap-2">
                  <Btn onClick={() => {
                    shared = lazy(load)
                    setLoadState(current => ({ Page: shared, attempt: current.attempt + 1 }))
                  }}>
                    {i18nT('components.errorBoundary.try_again')}
                  </Btn>
                  <Btn onClick={() => window.location.reload()}>
                    {i18nT('components.errorBoundary.reload_page')}
                  </Btn>
                </div>
              )}
            />
          </div>
        )}
      >
        <Suspense fallback={null}><Page /></Suspense>
      </ErrorBoundary>
    )
  }
  return LazyPage
}

export function TasksRedirect() { const { search } = useLocation(); return <Navigate to={'/projects' + search} replace /> }
export function ChatRedirect() { const { search } = useLocation(); return <Navigate to={'/chat' + search} replace /> }
export function OrchestratedRedirect() { const { slug } = useParams(); const { search } = useLocation(); return <Navigate to={`/chat${slug ? '/' + slug : ''}${search}`} replace /> }
