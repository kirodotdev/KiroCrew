/**
 * Evidence for the dashboard error-state sweep, batch pages-rest-1 (ErrorNotice
 * migration of top-level `website/src/pages/*.tsx`).
 *
 * THE CHANGE: hand-written error surfaces on these pages — a `bg-danger/10`
 * banner with an "Error" heading, a read failure dressed as an EmptyState, a
 * `role="alert"` div with inline danger styles, a rejected fetch reduced to a
 * "not found" page, a failure modal with an OK button, a `last_error` hidden in
 * a tooltip — now render through the shared `ErrorNotice`, with the "Ask the
 * agent" hand-off on where the surface holds no draft.
 *
 * Scenes mount the REAL pages against the real stylesheet, theme tokens and
 * live i18n catalog. `fetch` is stubbed: every request rejects, except that the
 * hooks list resolves with one hook whose last run failed — so the HooksPage
 * scene shows a real row (its `last_error` chevron and, once the runner clicks
 * Test, the failed hook-test notice) while its other reads fail. Nothing here
 * re-implements a notice or a string, so a frame proves what ships. The same
 * harness renders the base branch's markup when run against it (it passes no
 * prop that does not exist there), which is how the "before" frames are
 * produced.
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
import ChannelPage from '../src/pages/ChannelPage'
import AgentsPage from '../src/pages/AgentsPage'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

// One hook whose last run failed, so the list is not empty and the persisted
// `last_error` has a row to hang off. Everything else fails.
const HOOK = {
  id: 'hook-fmt', name: 'fmt', event: 'PostToolUse', matcher: 'write', matcher_mode: 'glob',
  command: 'npm run fmt', skills: [], timeout: 30, enabled: true,
  last_run: Math.floor(Date.now() / 1000) - 120, last_status: 'error', run_count: 12,
  last_error: 'npm ERR! code ELIFECYCLE — fmt exited with status 2',
}

// Every other API call fails: that IS the state under test. A rejected fetch is
// what useQuery surfaces as `error` (and what a raw `.catch` used to swallow),
// which is what the migrated notices render.
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method ?? 'GET').toUpperCase()
  if (url.endsWith('/api/hooks') && method === 'GET') {
    return Promise.resolve(new Response(JSON.stringify({ hooks: [HOOK] }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  return Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))
}) as typeof fetch

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
        style={{ maxWidth: 1000, margin: '0 auto', padding: 20, background: 'var(--bg)', color: 'var(--text)' }}
      >
        <Scene label="HooksPage — provider hooks rejected; one hook whose last run failed; Test rejected">
          <MemoryRouter initialEntries={['/hooks']}>
            <div style={{ maxHeight: 820, overflow: 'hidden' }}>
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
        <Scene label="ChannelPage — channel list and presets requests rejected (was: silent empty list)">
          <MemoryRouter initialEntries={['/channels']}>
            <div style={{ maxHeight: 360, overflow: 'hidden' }} className="flex flex-col">
              <ChannelPage />
            </div>
          </MemoryRouter>
        </Scene>
        <Scene label="AgentsPage — installed agents, sub-agents and usage requests rejected">
          <MemoryRouter initialEntries={['/agents']}>
            <div style={{ maxHeight: 520, overflow: 'hidden' }}>
              <AgentsPage embedded />
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
