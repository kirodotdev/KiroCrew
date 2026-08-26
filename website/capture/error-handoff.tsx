/**
 * Evidence for the error-to-agent hand-off coverage extension.
 *
 * THE CHANGE: error surfaces that previously rendered a bare red text line
 * (a dead end) now render the shared `ErrorNotice` with the "Ask the agent"
 * hand-off, so a failure the user cannot fix carries its context into chat.
 *
 * Scenes mount the REAL components (`JobLogsView`, `ExecutionsView`) against
 * the real stylesheet, theme tokens and live i18n catalog, with only `fetch`
 * stubbed to reject — exactly the failure the notice exists for. Nothing here
 * re-implements the notice, its icon, or its strings, so a frame proves what
 * ships.
 *
 *   ?scene=joblogs      job history load failure + cancel failure
 *   ?scene=executions   execution history load failure
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ExecutionsView from '../src/components/ExecutionsView'
import JobLogsView from '../src/components/JobLogsView'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'executions' ? 'executions' : 'joblogs'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

// Every API call fails: that IS the state under test. A rejected fetch is what
// useQuery surfaces as `error`, which is what the migrated notice renders.
window.fetch = () =>
  Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))

// Retries would keep the view in `isLoading` for the whole capture window;
// the settled error state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const root = createRoot(document.getElementById('root')!)
root.render(
  <QueryClientProvider client={qc}>
    <div
      data-capture-root
      style={{ maxWidth: 860, margin: '0 auto', padding: 24, background: 'var(--bg)', minHeight: 320 }}
    >
      {scene === 'joblogs' ? (
        <JobLogsView jobId="daily-briefing" cancelError="Failed to cancel: run already finished" />
      ) : (
        <ExecutionsView />
      )}
    </div>
  </QueryClientProvider>,
)
