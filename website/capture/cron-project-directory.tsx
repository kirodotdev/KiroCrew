/**
 * Isolated capture entry for the Schedule job form's `Project directory` field:
 * the merged project roster, the collision marker, the roster-failure notice and
 * the three field-scoped save errors.
 *
 * WHY ISOLATED: the real surface is the Schedule page's job dialog, which needs
 * the whole app shell (and a live gateway with a token) to reach. `JobForm` needs
 * only a query client and a router, so stubbing those two renders the REAL
 * component -- real classes, real Tailwind output, real theme tokens, real i18n
 * strings. Modelled on `cron-mode-advice.tsx`, which does the same for the
 * minimal-context control.
 *
 * WHAT IS STUBBED, AND WHAT IS NOT: only the HTTP responses. `window.fetch` is
 * replaced so `/api/agents?project_path=` answers with a roster the scene chose;
 * everything that turns that roster into pixels is the shipped code. In
 * particular the `overrides global` marker is NOT seeded -- `JobForm` derives the
 * shadowed set from the payload and `AgentSelector` decides whether to render it,
 * so a frame cannot document a marker the shipped logic would not produce. The
 * payload deliberately mirrors the real endpoint's shape, where every project row
 * carries `source: "kirocrew"` and an EMPTY description (the discovery layer
 * returns names only) -- a fixture that invented `source: "project"` is exactly
 * how the collision test passed against a payload the backend cannot send.
 *
 * `layout="vertical"` mirrors SchedulePage's own call site. Scenes 01-05 leave
 * `job` undefined, which is the CREATE form; the save-error scenes pass a `job`
 * so the form owns its Save button and can be submitted into a failing PATCH.
 *
 * Scene via query string: ?scene=<name>   Theme via ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import JobForm from '../src/components/JobForm'
import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'field'
const theme = params.get('theme') === 'light' ? 'kiro-light' : 'kiro-dark'
document.documentElement.setAttribute('data-theme', theme)

const PROJECT = '/tmp/kc-collision-demo'

/** The configured (global) side. `release-checklist` is the one a project also
 *  declares, so it is the collision; `default` is the control that must stay
 *  unmarked, which is what proves the marker tracks a collision rather than
 *  merely being a project row. */
const GLOBAL_ROWS = [
  { name: 'default', scope: 'global', source: 'kirocrew', description: '', is_default: true },
  {
    name: 'release-checklist',
    scope: 'global',
    source: 'kirocrew',
    description: 'Configured agent for release checks',
  },
]

/** The bound directory's roster, shaped exactly as the endpoint sends it: the
 *  colliding name comes back as a PROJECT row and the shadowed configured row is
 *  gone, which is the backend half of this change. */
const PROJECT_ROWS = [
  { name: 'default', scope: 'global', source: 'kirocrew', description: '', is_default: true },
  { name: 'release-checklist', scope: 'project', source: 'kirocrew', description: '' },
  { name: 'repo-smoke', scope: 'project', source: 'kirocrew', description: '' },
]

const json = (body: unknown, status = 200) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )

/** Which backend rejection the save scenes provoke. The strings are the raw
 *  backend ones on purpose: `friendlyProjectPathError` maps them to the
 *  field-scoped copy, so the frame proves the mapping rather than the copy. */
const SAVE_ERRORS: Record<string, string> = {
  'save-absolute': 'project_path must be an absolute path',
  'save-sensitive': 'project_path refers to a sensitive path',
  'save-missing': 'project_path must be an existing directory',
}

const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()

  if (url.startsWith('/api/agents/sync')) return json({ ok: true })
  if (url.startsWith('/api/agents')) {
    // The roster-failure scene fails ONLY the project-scoped fetch: the notice
    // has to sit beside the field while the rest of the form stays usable.
    if (scene === 'roster-error' && url.includes('project_path')) return json({}, 500)
    const bound = url.includes('project_path')
    return json({ agents: bound ? PROJECT_ROWS : GLOBAL_ROWS, default_agent: 'default' })
  }
  if (url.startsWith('/api/recent-projects')) return json({ dirs: [PROJECT, '/srv/checkouts/api'] })
  if (url.startsWith('/api/browse-dirs')) {
    return json({
      path: '/tmp',
      parent: '/',
      dirs: [
        { name: 'kc-collision-demo', path: PROJECT },
        { name: 'kc-collision-demo-b', path: '/tmp/kc-collision-demo-b' },
        { name: 'build-cache', path: '/tmp/build-cache' },
      ],
    })
  }
  if (url.startsWith('/api/models')) return json({ models: [] })
  // A save in a save-error scene is the point of the scene.
  if (url.startsWith('/api/cron') && method !== 'GET') {
    const err = SAVE_ERRORS[scene]
    if (err) return json({ error: err }, 400)
    return json({ ok: true })
  }
  if (url.startsWith('/api/')) return json({})
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

initI18n('en')

/** The edit shape the save-error scenes need: a job the form can submit. Only the
 *  fields `parseJobDefaults` reads are set, and they mirror `messageJob()` in
 *  src/test/JobForm.saveError.test.tsx -- `cron_expr` carries the schedule, not
 *  `schedule`, and a form whose schedule does not parse never submits at all. */
const EDIT_JOB = {
  id: 'job-1',
  name: 'Nightly release check',
  message: 'Run the release checklist and report what is not ready.',
  agent: 'default',
  schedule: '',
  cron_expr: '0 9 * * *',
  enabled: true,
} as never

function Scene() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const isSave = scene in SAVE_ERRORS
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div className="bg-bg p-6 text-text">
          <div
            data-capture-root
            className="flex flex-col gap-3 rounded-xl border border-border-strong bg-card p-5"
            style={{ width: 620 }}
          >
            <div className="text-[15px] font-semibold text-text-strong">
              {isSave ? EDIT_JOB.name : i18nT('pages.schedulePage.new_job')}
            </div>
            <JobForm
              {...(isSave ? { job: EDIT_JOB } : { externalSubmit: true })}
              agents={GLOBAL_ROWS as never[]}
              defaultAgent="default"
              onSaved={() => {}}
              layout="vertical"
            />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
