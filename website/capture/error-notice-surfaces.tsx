/**
 * Isolated capture entry for the failure surfaces this change moved onto
 * `ErrorNotice`, in their FAILED state.
 *
 * WHY ISOLATED: each state is the outcome of a request that failed, which a
 * capture run has no backend to fail. `window.fetch` is replaced with a stub
 * that answers the exact endpoints these surfaces call, so the REAL components
 * run their real mutation code and land in their real error branches:
 *   - WorkflowSourcePanel: `sourceError` from props (load failure), and the
 *     rerun POST answered 400 with a two-line `errors` body (the backend's
 *     validation rejection). The capture script opens the panel, presses Edit,
 *     then Re-run.
 *   - WorkflowsPage: the first `/validate` answers `ok: false` (the validator's
 *     rejection under the editor), the second answers 503 (`validateMutation.error`,
 *     the request-failed notice), later ones pass so Run proceeds, then `/run`
 *     answers 500 and `runMutation.error` is set.
 *   - WorkspacePicker: `/api/workspaces` (create) answered with `{ error }`, so
 *     `requestError` is set after the script chooses a directory and presses Create;
 *     the "name is required" hint stays a plain hint and is NOT part of this state.
 *   - SkillBrowserModal (its own page, `?surface=skill-browser`, because the
 *     modal is a full-viewport overlay): the discover GET answers two results
 *     and the install POST answers 500, so pressing a row's Install lands the
 *     row in `phase.step === 'error'` — the inline notice on the row, which
 *     does not change the selection — and selecting that row then shows the
 *     detail pane's notice, the one carrying the `askAgent` hand-off.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import WorkflowSourcePanel from '../src/apps/workflows/WorkflowSourcePanel'
import WorkflowsPage from '../src/apps/workflows/WorkflowsPage'
import WorkspacePicker from '../src/components/WorkspacePicker'
import SkillBrowserModal from '../src/components/SkillBrowserModal'
import type { DiscoveredSkill } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const surface = params.get('surface') || 'page'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const FAILED_SKILL: DiscoveredSkill = {
  id: 'acme/skills/release-notes',
  name: 'release-notes',
  provider: 'skillsh',
  display_provider: 'skills.sh',
  description: 'Draft release notes from merged pull requests.',
  installed: false,
  installs: 1240,
}
const OTHER_SKILL: DiscoveredSkill = {
  id: 'acme/skills/release-checklist',
  name: 'release-checklist',
  provider: 'skillsh',
  display_provider: 'skills.sh',
  description: 'Walk the pre-release checklist and file what is missing.',
  installed: false,
  installs: 310,
}

/* Answer every request the mounted surfaces make, each with the failure the
 * capture documents. Anything else gets an empty 200 so unrelated loaders settle. */
let validateCalls = 0
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()
  if (url.includes('/rerun') && method === 'POST') {
    return json(400, {
      error: 'invalid script',
      errors: ['line 3: `ctx.agent` is not a phase; use `ctx.phase(...)`', 'line 9: unterminated string literal'],
    })
  }
  // First Validate press: the validator rejects (the notice under the editor).
  // Second: the request itself fails (the request-failed notice). Later ones
  // pass so Run can proceed to its own failure.
  if (url.endsWith('/validate') && method === 'POST') {
    validateCalls += 1
    if (validateCalls === 1) return json(200, { ok: false, errors: ['line 2: `ctx.agent` is not a phase; use `ctx.phase(...)`'] })
    if (validateCalls === 2) return json(503, { error: 'workflow validator unavailable' })
    return json(200, { ok: true, errors: [] })
  }
  if (url.endsWith('/run') && method === 'POST') return json(500, { error: 'workflow runner unavailable' })
  if (url.includes('/api/workspaces') && method === 'POST') {
    return json(200, { error: 'A workspace named "docs" already exists in this directory.' })
  }
  if (url.includes('/api/browse-dirs')) return json(200, { path: '/home/me/projects', parent: '/home/me', dirs: [{ name: 'docs', path: '/home/me/projects/docs' }] })
  if (url.includes('/api/skills/-/discover/install') && method === 'POST') {
    return json(500, { error: "npm exited 1: EACCES: permission denied, mkdir '/opt/skills'" })
  }
  if (url.includes('/api/skills/-/discover/preview')) {
    return json(200, { name: 'release-notes', description: FAILED_SKILL.description, content: '# release-notes\n\nDraft release notes from merged pull requests.\n', files: ['SKILL.md'], file_count: 1 })
  }
  if (url.includes('/api/skills/-/discover')) return json(200, { results: [FAILED_SKILL, OTHER_SKILL], providers: ['skillsh'] })
  return json(200, {})
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })


function WorkspacePickerHost() {
  const anchorRef = useRef<HTMLButtonElement | null>(null)
  return (
    <div style={{ position: 'relative', minHeight: 420 }}>
      <button ref={anchorRef} type="button" className="px-2 py-1 text-[12px] text-muted">workspace anchor</button>
      <WorkspacePicker open onOpenChange={() => {}} anchorRef={anchorRef} onCreated={() => {}} />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        {surface === 'skill-browser' ? (
          <div data-capture-root style={{ minHeight: '100vh', background: 'var(--bg)' }}>
            <SkillBrowserModal open onClose={() => {}} />
          </div>
        ) : (
        <div data-capture-root style={{ width: 820, margin: '16px auto', background: 'var(--bg)', padding: 16, display: 'flex', flexDirection: 'column', gap: 24 }}>
          <section data-capture-section="workflow-source-panel">
            <WorkflowSourcePanel run_id="wf_ui_1758400000" source={'def run(ctx):\n    ctx.agent("plan", "draft the outline")\n'} sourceError="GET /api/apps/workflows/runs/wf_ui_1758400000 → 502" />
          </section>
          <section data-capture-section="workspace-picker">
            <WorkspacePickerHost />
          </section>
          <section data-capture-section="workflows-page" style={{ height: 720 }}>
            <WorkflowsPage />
          </section>
        </div>
        )}
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
