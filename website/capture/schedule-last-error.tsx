/**
 * Isolated capture entry for the Schedule page's `Last Error` notice on a
 * MESSAGE job -- the surface this change newly makes visible, and the one a
 * project-bound job's named skip reason actually lands on.
 *
 * WHY THIS ENTRY EXISTS: the notice was previously gated on `job.script`, so a
 * message job's `last_error` was reachable only as the status cell's `title`
 * attribute -- a hover tooltip, and no signal at all in the desktop app. A
 * tooltip cannot be photographed, which is precisely why the state had no
 * evidence: UX Review asked for a still of the notice itself.
 *
 * WHAT IS STUBBED, AND WHAT IS NOT: only the HTTP responses. `window.fetch` is
 * replaced so `/api/crons` answers with one job carrying a `last_error`;
 * everything that turns that field into pixels is the shipped code. In
 * particular the notice is NOT seeded -- `SchedulePage` decides whether to
 * render it from `job.last_error` alone, and picks the log styling from
 * `job.script`, so a frame cannot document a notice the shipped condition would
 * not produce. The reason strings are the ones `_agent_unresolved_message`
 * emits, pasted verbatim rather than paraphrased.
 *
 * Providers mirror the app's own shell (store, theme, router, query client), the
 * same set `renderWithProviders` gives this page under test.
 *
 * Scene via query string: ?scene=<name>   Theme via ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SchedulePage from '../src/pages/SchedulePage'
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'shadowed'
document.documentElement.dataset.theme = params.get('theme') || 'dark'

/**
 * The two reason strings the gateway writes, verbatim from
 * `_agent_unresolved_message` in src/kiro_crew/slack/gateway.py. Copied rather
 * than paraphrased: a frame whose text drifts from the shipped sentence is
 * evidence for a message that does not exist.
 */
const REASONS: Record<string, string> = {
  shadowed:
    "Agent 'default' is defined in project directory '/Users/you/projects/myrepo' and "
    + "would shadow this job's Crew Member, so the run was skipped rather than started "
    + "under the member's own private memory. Rename the project's agent or unbind the member.",
  missing:
    "Agent 'release-checklist' not found in project directory '/Users/you/projects/myrepo'",
}

const JOB = {
  id: 'job-capture-1',
  name: 'Nightly release check',
  message: 'Run the release checklist and report what is not ready.',
  schedule: 'cron 0 9 * * *',
  enabled: true,
  // A MESSAGE job: no `script`, which is the whole point -- this is the shape
  // whose last_error used to have nowhere to render.
  project_path: '/Users/you/projects/myrepo',
  agent: 'default',
  last_status: 'error',
  last_error: REASONS[scene] ?? REASONS.shadowed,
  last_run_ts: 1_770_000_000,
  run_never_started: true,
}

const EMPTY = { jobs: [] as unknown[] }

const realFetch = window.fetch.bind(window)
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  if (url.includes('/api/crons/history')) return json({ runs: [] })
  // `/api/cron-folders`, NOT `/api/crons/folders`: the page maps the response
  // directly, so a wrong path here fell through to the generic `{jobs: []}` and
  // crashed the whole page with `folders.map is not a function`.
  if (url.includes('/api/cron-folders')) return json([])
  if (url.includes('/api/crons')) return json({ jobs: [JOB] })
  if (url.includes('/api/models')) return json([])
  // Project rows carry source "kirocrew" and an EMPTY description, mirroring the
  // real endpoint: the discovery layer returns names only.
  if (url.includes('/api/agents')) return json({ agents: [], default_agent: 'default' })
  if (url.startsWith('/api/')) return json(EMPTY)
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/schedule']}>
          <SchedulePage />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
