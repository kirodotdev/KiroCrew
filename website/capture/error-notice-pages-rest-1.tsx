/**
 * Evidence for the dashboard error-state sweep, batch pages-rest-1 (ErrorNotice
 * migration of top-level `website/src/pages/*.tsx`).
 *
 * THE CHANGE: hand-written error surfaces on these pages — a `bg-danger/10`
 * banner with an "Error" heading, a read failure dressed as an EmptyState, a
 * `role="alert"` div with inline danger styles, a rejected fetch reduced to a
 * "not found" page — now render through the shared `ErrorNotice`, with the
 * "Ask the agent" hand-off on where the surface holds no draft.
 *
 * Scenes mount the REAL pages against the real stylesheet, theme tokens and
 * live i18n catalog. Only `fetch` is stubbed (to reject), which is exactly the
 * failure the notices exist for; every query on these pages therefore settles
 * in its error state. Nothing here re-implements a notice or a string, so a
 * frame proves what ships. The same harness renders the base branch's markup
 * when run against it (it passes no prop that does not exist there), which is
 * how the "before" frames are produced.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import HooksPage from '../src/pages/HooksPage'
import AppPage from '../src/pages/AppPage'
import DevFleetPage from '../src/pages/DevFleetPage'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

// Every API call fails: that IS the state under test. A rejected fetch is what
// useQuery surfaces as `error` (and what a raw `.catch` used to swallow), which
// is what the migrated notices render.
window.fetch = () =>
  Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))

// Retries would keep the pages in `isLoading` for the whole capture window;
// the settled error state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

function Scene({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section data-scene={label} className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-3 py-1.5 text-[11px] uppercase tracking-wider text-muted border-b border-border">{label}</div>
      <div className="p-3 relative">{children}</div>
    </section>
  )
}

const root = createRoot(document.getElementById('root')!)
root.render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div
        data-capture-root
        className="flex flex-col gap-3"
        style={{ maxWidth: 860, margin: '0 auto', padding: 20, background: 'var(--bg)', color: 'var(--text)' }}
      >
        <Scene label="HooksPage — hook list and provider hooks requests rejected">
          <MemoryRouter initialEntries={['/hooks']}>
            <div style={{ maxHeight: 420, overflow: 'hidden' }}>
              <HooksPage embedded />
            </div>
          </MemoryRouter>
        </Scene>
        <Scene label="AppPage — app record request rejected (was: rendered as “not found”)">
          <MemoryRouter initialEntries={['/app/ledger-lens']}>
            <div style={{ minHeight: 120 }} className="flex flex-col">
              <Routes>
                <Route path="/app/:name" element={<AppPage />} />
              </Routes>
            </div>
          </MemoryRouter>
        </Scene>
        <Scene label="DevFleetPage — fleet discovery request rejected">
          <MemoryRouter initialEntries={['/dev-fleet']}>
            <div style={{ maxHeight: 620, overflow: 'hidden' }}>
              <DevFleetPage />
            </div>
          </MemoryRouter>
        </Scene>
      </div>
    </QueryClientProvider>
  </Provider>,
)
