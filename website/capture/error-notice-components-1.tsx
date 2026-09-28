/**
 * Evidence for the components-1 batch of the ErrorNotice sweep.
 *
 * THE CHANGE: error surfaces under `src/components/` that rendered a
 * hand-written red line / bare `role="alert"` box (a dead end) now render the
 * shared `ErrorNotice`, with the "Ask the agent" hand-off wherever the
 * surface holds no unsaved draft.
 *
 * Scenes mount the REAL components against the real stylesheet, theme tokens
 * and live i18n catalog, with only `fetch` stubbed to reject — exactly the
 * failure the notice exists for. Nothing here re-implements the notice, its
 * icon, or its strings, so a frame proves what ships. The same entry runs
 * unchanged against the base branch to produce the "before" frames.
 *
 *   ?scene=webhooks    CrewWebhookSection — GET /api/webhooks rejected
 *   ?scene=wake        CrewWakeSection — GET /api/crons rejected
 *   ?scene=footer      ManageAgentsFooter — default-agent write rejected
 *   ?scene=executions  ExecutionsView — job list AND history rejected
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import CrewWebhookSection from '../src/components/CrewWebhookSection'
import CrewWakeSection from '../src/components/CrewWakeSection'
import ExecutionsView from '../src/components/ExecutionsView'
import { ManageAgentsFooter } from '../src/components/AgentDropdownList'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'webhooks'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

// Every API call fails: that IS the state under test. A rejected fetch is what
// useQuery surfaces as `error`, which is what the migrated notice renders.
// The one exception is the executions scene, where the run HISTORY resolves and
// only the job LIST (GET /api/crons) rejects — the base branch rendered that
// failure as nothing at all (deleted jobs silently un-marked), which is the
// silent-failure class this batch also closes.
const now = Math.floor(Date.now() / 1000)
const HISTORY = {
  runs: [
    { job_id: 'j1', job_name: 'gh-autofix-dispatcher', run_id: 'r1', started_at: now - 600, duration_ms: 4210, status: 'success', trigger: 'cron', summary: 'Dispatched 2 issues' },
    { job_id: 'j2', job_name: 'nightly-digest', run_id: 'r2', started_at: now - 7200, duration_ms: 1890, status: 'failure', trigger: 'manual', summary: 'Gateway timeout' },
  ],
}
window.fetch = ((input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (scene === 'executions' && url.includes('/api/crons/history')) {
    return Promise.resolve(new Response(JSON.stringify(HISTORY), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  return Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))
}) as typeof window.fetch

// Retries would keep the view in `isLoading` for the whole capture window;
// the settled error state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Scene() {
  if (scene === 'wake') return <CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />
  if (scene === 'footer') {
    return (
      <div className="w-[320px] rounded-lg border border-border bg-bg-elevated">
        <ManageAgentsFooter onManage={() => {}} error />
      </div>
    )
  }
  if (scene === 'executions') return <ExecutionsView />
  return <CrewWebhookSection crew="kirocrew-autofix" />
}

const root = createRoot(document.getElementById('root')!)
root.render(
  <QueryClientProvider client={qc}>
    <MemoryRouter>
      <div
        data-capture-root
        style={{ maxWidth: 860, margin: '0 auto', padding: 24, background: 'var(--bg)', minHeight: 240 }}
      >
        <Scene />
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
