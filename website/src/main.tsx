// Extension composition root — the one file a downstream edition owns. Imported
// FIRST (before store/providers/App) so seam registrations run before render.
// Empty in the stock build. See website/src/extensions.ts.
import './extensions'
import React, { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { withCommitProfiler, installCommitProfilerConsoleApi } from './lib/commitProfiler'
import { Provider } from 'react-redux'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { store } from './store'
import { BrandingProvider } from './hooks/useBranding'
import { ProviderProvider } from './providers'
import { VoiceSessionProvider } from './providers/VoiceSessionProvider'
import { ThemeProvider } from './hooks/useTheme'
import { UIModeProvider } from './hooks/useUIMode'
import ThemeExperienceLayer from './components/ThemeExperienceLayer'
import { initRum } from './rum'
// i18n must initialize before the first render — a component rendering ahead of
// init would emit its bare translation key instead of text.
import { initI18n } from './i18n'
import { LanguageProvider } from './i18n/LanguageProvider'
import App from './App'
import { queryClient } from './api/queryClient'
import ErrorBoundary from './components/ErrorBoundary'
import DashboardBootstrap from './components/DashboardBootstrap'
import 'katex/dist/katex.min.css'
import 'monaco-editor/esm/vs/base/browser/ui/codicons/codicon/codicon.css'
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
  import('react-dom').then(ReactDOM => import('@axe-core/react').then(axe => axe.default(React, ReactDOM, 1000)))
}

const WorldsPopout = lazy(() => import('./pages/WorldsPopout'))

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
                <BrowserRouter>
                  <Routes>
                    <Route path="/worlds-popout" element={<BrandingProvider><ProviderProvider><Suspense fallback={null}><WorldsPopout /></Suspense></ProviderProvider></BrandingProvider>} />
                    <Route
                      path="*"
                      element={(
                        <BrandingProvider>
                          <ProviderProvider>
                            <DashboardBootstrap><VoiceSessionProvider>{withCommitProfiler('app', <App />)}</VoiceSessionProvider></DashboardBootstrap>
                          </ProviderProvider>
                        </BrandingProvider>
                      )}
                    />
                  </Routes>
                </BrowserRouter>
              </UIModeProvider>
            </ThemeProvider>
          </LanguageProvider>
        </Provider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
