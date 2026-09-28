/**
 * Evidence for the chat error-state sweep, batch chat-2 (ErrorNotice migration).
 *
 * THE CHANGE: hand-written error surfaces in `website/src/pages/chat/**` — a
 * `text-red-500` line inside a toggle button, a Centered icon+title+body
 * failure screen, a refused stop that only ever reached `console.warn` — now
 * render through the shared `ErrorNotice`, with the "Ask the agent" hand-off
 * on where the surface holds no draft.
 *
 * Scenes mount the REAL components against the real stylesheet, theme tokens
 * and live i18n catalog. Only `fetch` is stubbed (to reject), which is exactly
 * the failure the notices exist for. Nothing here re-implements a notice or a
 * string, so a frame proves what ships. The same harness renders the base
 * branch's markup when run against it (every prop it passes exists there),
 * which is how the "before" frames are produced.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SessionSummaryTab from '../src/pages/chat/SessionSummaryTab'
import SubagentProgressBar from '../src/pages/chat/SubagentProgressBar'
import WorkflowSidebarRow from '../src/pages/chat/WorkflowSidebarRow'
import { store } from '../src/store'
import { setActiveSlot, sseSubagentSpawn } from '../src/store/chatSlice'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

// Every API call fails: that IS the state under test. A rejected fetch is what
// useQuery surfaces as `error`, and what a refused stop lands in `.catch`.
window.fetch = () =>
  Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))

// Retries would keep the panels in `isLoading` for the whole capture window;
// the settled error state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const SLOT = 'chat-capture'
store.dispatch(setActiveSlot(SLOT))
store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'sa-1', task: 'Audit the settings pages for hand-written error surfaces', agent: 'kirocrew-lite' }))
store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'sa-2', task: 'Summarize the CI log for run 8704', agent: 'kirocrew-lite' }))

function Scene({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section data-scene={label} className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-3 py-1.5 text-[11px] uppercase tracking-wider text-muted border-b border-border">{label}</div>
      <div className="p-3">{children}</div>
    </section>
  )
}

const root = createRoot(document.getElementById('root')!)
root.render(
  <Provider store={store}>
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <div
          data-capture-root
          className="flex flex-col gap-3"
          style={{ maxWidth: 720, margin: '0 auto', padding: 20, background: 'var(--bg)', color: 'var(--text)' }}
        >
          <Scene label="WorkflowSidebarRow — backend reports the run failed">
            <WorkflowSidebarRow
              row={{
                run_id: 'wf_000042',
                name: 'nightly fix-loop analysis',
                status: 'failed',
                event_count: 17,
                error: "KeyError: 'baseline' while folding the culprit table",
              }}
            />
          </Scene>
          <Scene label="SessionSummaryTab — summary request rejected">
            <div className="relative" style={{ height: 200 }}>
              <SessionSummaryTab slot={SLOT} />
            </div>
          </Scene>
          <Scene label="SubagentProgressBar — per-row Stop refused by the backend">
            <SubagentProgressBar slot={SLOT} />
          </Scene>
        </div>
      </QueryClientProvider>
    </MemoryRouter>
  </Provider>,
)
