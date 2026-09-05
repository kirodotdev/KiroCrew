// Extension composition root — the one file a downstream edition owns. Imported
// FIRST (before store/providers/App) so seam registrations run before render.
// Empty in the stock build. See website/src/extensions.ts.
import './extensions'
import { startMemoryWatch } from './lib/memoryWatch'
import React, { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { withCommitProfiler, installCommitProfilerConsoleApi } from './lib/commitProfiler'
import { Provider } from 'react-redux'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { store } from './store'
import { BrandingProvider } from './hooks/useBranding'
import { ProviderProvider } from './providers'
import { ThemeProvider } from './hooks/useTheme'
import { UIModeProvider } from './hooks/useUIMode'
import ThemeExperienceLayer from './components/ThemeExperienceLayer'
import { NavigationLeaveGuardProvider, NavigationBackGuard } from './components/NavigationLeaveGuard'
import { initRum } from './rum'
import { isEmbeddedPane } from './lib/embedded'
// i18n must initialize before the first render — a component rendering ahead of
// init would emit its bare translation key instead of text. The `/all` entry is
// what registers every language; plain `./i18n` is English-only, so importing it
// here would render English for every user whatever language they picked.
import { initI18n } from './i18n/all'
import { LanguageProvider } from './i18n/LanguageProvider'
import App from './App'
import { queryClient } from './api/queryClient'
import ErrorBoundary from './components/ErrorBoundary'
import DashboardBootstrap from './components/DashboardBootstrap'
import { installPageZoomSuppression } from './utils/pageZoom'
import { installStaleShellHeal } from './lib/staleShellHeal'
import 'katex/dist/katex.min.css'
import './index.css'
import './styles/cli-mode.css'
// Register shared modules for federated app bundles (must be before any app loads)
import './app-sdk/shared-modules'

// Initialize RUM as early as possible
initRum(__APP_VERSION__)

// Seeded from localStorage (written by the inline bootstrap in index.html) so
// the very first paint is already in the right language; LanguageProvider then
// reconciles against the server-authoritative config value.
initI18n()

// Page zoom is off on touch: the shell is an application, not a document. The
// viewport meta and the root `touch-action` cover Blink/Gecko; this covers
// WebKit, which ignores both for user gestures. Installed before render so the
// very first pinch is already suppressed. See utils/pageZoom.ts.
installPageZoomSuppression()
// Detect and break out of a stale service-worker shell (see the module doc).
installStaleShellHeal()

// Auto-recover from stale lazy-chunk errors after a frontend rebuild.
// Vite fires `vite:preloadError` on window when a dynamic import() of a
// hashed chunk 404s -- this happens when a tab loaded an old entry bundle
// before a rebuild, then lazy-loads a page whose hash has since changed
// (e.g. "Failed to fetch dynamically imported module: .../SomePage-<hash>.js").
// Reloading pulls the fresh index.html + chunk map, which self-heals the tab.
// Guarded by a short-lived sessionStorage timestamp so a genuinely-missing
// chunk (persistent 404) can't trigger an infinite reload loop.
window.addEventListener('vite:preloadError', (event) => {
  const AT_KEY = 'vite-preload-reloaded-at'
  const N_KEY = 'vite-preload-reload-count'
  const COOLDOWN_MS = 10_000
  const MAX_RELOADS = 3
  let last = 0
  let count = 0
  try {
    last = Number(sessionStorage.getItem(AT_KEY) || 0)
    count = Number(sessionStorage.getItem(N_KEY) || 0)
  } catch { /* storage blocked (privacy/partitioned) — treat as a first attempt */ }
  // Bail (let the error surface via ErrorBoundary) if we reloaded very recently
  // (tight-loop guard) OR have already reloaded too many times this session
  // (a genuinely-missing chunk whose reload round-trip keeps exceeding the
  // cooldown must not loop forever).
  if (Date.now() - last < COOLDOWN_MS || count >= MAX_RELOADS) return
  let persisted = false
  try {
    sessionStorage.setItem(AT_KEY, String(Date.now()))
    sessionStorage.setItem(N_KEY, String(count + 1))
    persisted = true
  } catch { /* storage blocked */ }
  // Only auto-reload if we could PERSIST the guard. If storage is blocked we
  // cannot count reloads, so a genuinely-missing chunk would loop forever —
  // let the error surface via ErrorBoundary instead.
  if (!persisted) return
  // Prevent Vite from throwing the unhandled preload error before we reload.
  event.preventDefault()
  window.location.reload()
})

// Accessibility: runtime DOM scanning in dev mode (logs violations to console)
if (import.meta.env.DEV) {
  // The `meta-viewport` rule is deliberately NOT waived, even though this shell ships
  // `maximum-scale=1, user-scalable=no` and axe therefore reports a critical WCAG 1.4.4
  // finding on every dev render. A waiver was written and removed; do not re-add one.
  // The finding is not noise — it is the only recurring reminder that suppressing page
  // zoom is an accessibility trade nobody has yet accepted in writing, and "nobody can
  // action it" was wrong: it is a decision, and a decision stays owed.
  // See the page-zoom section of website/docs/page-layout.md for the policy.
  import('react-dom').then(ReactDOM => import('@axe-core/react').then(axe => axe.default(React, ReactDOM, 1000)))
}

// Warm the Pierre code/diff renderer while the tab is idle: loading the chunk
// creates the module-level highlight worker pool, so the first code surface a
// user opens paints immediately instead of paying chunk + worker + grammar
// startup on click.
//
// NOT in an embedded remote-instance pane. Each warm pane is a full copy of this
// SPA in its own realm, and every realm that evaluates PierreImpl spawns
// PIERRE_WORKER_POOL_SIZE workers, each loading its own highlighter bundle + WASM
// regex engine. With a warm-set cap that tracks the connected crews (automatic,
// bounded at 8) that is 4 workers x up to 8 panes eagerly-spawned in one renderer
// process, and the background panes paint
// nothing, so most of them buy no responsiveness at all. Observed consequence: the
// renderer accumulated 20 DedicatedWorker threads and was killed by a V8 fatal
// abort raised on one of them, taking the whole window black.
//
// Panes are not left slower than before in any case a user can see: the lazy
// import in pierre/index.tsx still creates the pool on the first real code
// surface, so a pane the user actually opens a diff in pays exactly the
// pre-warm cost it used to pay on click.
const idle: (cb: () => void) => void =
  typeof requestIdleCallback === 'function' ? cb => requestIdleCallback(cb) : cb => setTimeout(cb, 2000)
if (!isEmbeddedPane()) {
  idle(() => { import('./pierre/PierreImpl').catch(() => { /* warmed on first use instead */ }) })
}

const WorldsPopout = lazy(() => import('./pages/WorldsPopout'))

// Sample this renderer's memory trajectory so a V8 cage OOM has a before, not
// just an after. Reports V8 external memory (backing stores + external strings),
// which is where EVERY ArrayBuffer lands regardless of which API created it --
// unlike the constructor wrap this replaces, which saw one of ~25 allocation
// paths in one realm. Cheap (four integers per 5s), and no-ops when there is no
// main process to report to (a plain-browser dashboard).
startMemoryWatch('main')

// Debug-only, and inert unless explicitly armed with ?profile=commits. When
// disarmed withCommitProfiler returns the children untouched, so no Profiler
// element enters the tree on a normal load.
installCommitProfilerConsoleApi()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary root scope="app-shell">
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <LanguageProvider>
            <ThemeProvider>
              <UIModeProvider>
                <ThemeExperienceLayer />
                <NavigationLeaveGuardProvider>
                  <BrowserRouter>
                    {/* Inside the router (it navigates) and outside the routes
                        (it must survive every route change). Renders nothing,
                        and stays out of the history stack entirely until a page
                        publishes work at stake. */}
                    <NavigationBackGuard />
                    <Routes>
                      <Route path="/worlds-popout" element={<BrandingProvider><ProviderProvider><Suspense fallback={null}><WorldsPopout /></Suspense></ProviderProvider></BrandingProvider>} />
                      <Route
                        path="*"
                        element={(
                          <BrandingProvider>
                            <ProviderProvider>
                              <DashboardBootstrap>{withCommitProfiler('app', <App />)}</DashboardBootstrap>
                            </ProviderProvider>
                          </BrandingProvider>
                        )}
                      />
                    </Routes>
                  </BrowserRouter>
                </NavigationLeaveGuardProvider>
              </UIModeProvider>
            </ThemeProvider>
          </LanguageProvider>
        </Provider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
