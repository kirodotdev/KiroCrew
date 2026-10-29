/**
 * Isolated capture entry for the workflow-library change cards
 * (`workflow.save`, `workflow.update`).
 *
 * WHY ISOLATED: a pending card exists only while an agent's turn has just
 * proposed it, which no capture run has. This mounts the REAL ChangeCard against
 * the real stylesheet, theme tokens and live i18n catalog, fed the card the
 * gateway's `build_preview` returns for each kind (rows, risk, plan), so the
 * frames show the name, the whole script and, for an update, the diff.
 *
 * Scene comes from the query string: ?scene=save|update|applied&theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ChangeCard from '../src/cards/ChangeCard'
import type { Card } from '../src/api/cards'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'save'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const V1 = [
  'META = {"name": "investigate", "description": "Find the cause of a failure"}',
  '',
  'async def workflow(ctx):',
  '    notes = await ctx.agent("Reproduce the failure: " + ctx.args.get("input", ""))',
  '    return await ctx.agent("Name the root cause in one paragraph:\\n" + notes)',
].join('\n')
const V2 = V1.replace('Name the root cause in one paragraph', 'Name the root cause and the fix in one paragraph')
const DIFF = [
  '--- revision 2',
  '+++ revision 3',
  '@@ -3,4 +3,4 @@',
  ' async def workflow(ctx):',
  '     notes = await ctx.agent("Reproduce the failure: " + ctx.args.get("input", ""))',
  '-    return await ctx.agent("Name the root cause in one paragraph:\\n" + notes)',
  '+    return await ctx.agent("Name the root cause and the fix in one paragraph:\\n" + notes)',
].join('\n')

const now = Date.now()
const base = {
  id: 'card-capture-1', slot_key: 'chat-capture', revision: 1, status: 'pending' as const,
  risk: 'code_exec' as const, editable: [], created_at: now - 60_000, expires_at: now + 23 * 3600_000,
  reason: 'You asked me to keep the IDE recipes in your library, so they show under Customize → Workflows and run as /workflow.',
}
const save: Card = {
  ...base, kind: 'workflow.save',
  title: 'Save workflow “Investigate” to the library',
  params: { source: V1, name: 'Investigate', description: 'Find the cause of a failure', slug: 'investigate' },
  changes: [
    { field: 'name', label: 'Name', after: 'Investigate' },
    { field: 'description', label: 'Description', after: 'Find the cause of a failure' },
    { field: 'workflow_command', label: 'Command', after: '/workflow investigate' },
    { field: 'workflow_source', label: 'Script', after: V1 },
  ],
  plan: { apply: [{ method: 'POST', path: '/api/workflows/definitions', body: { source: V1, name: 'Investigate', description: 'Find the cause of a failure', slug: 'investigate' } }], undo: null },
  undo_unavailable_reason: 'no_identity_check',
}
const update: Card = {
  ...base, kind: 'workflow.update',
  title: 'Save a new revision of workflow “Investigate”',
  params: { workflow: 'investigate', source: V2 },
  changes: [
    { label: 'Workflow', after: '/workflow investigate' },
    { label: 'Revision', before: 2, after: 3 },
    { field: 'workflow_diff', label: 'Changes to the script', after: DIFF },
    { field: 'workflow_source', label: 'Script', after: V2, detail: true },
  ],
  plan: { apply: [{ method: 'PATCH', path: '/api/workflows/definitions/wfd_0123456789abcdef', body: { source: V2, expected_revision: 2 } }], undo: null },
  undo_unavailable_reason: 'no_identity_check',
}
const applied: Card = { ...save, status: 'applied', result: { summary: 'Done: Save workflow “Investigate” to the library' } }
const card = { save, update, applied }[scene as 'save' | 'update' | 'applied'] ?? save

initI18n('en')
const queryClient = new QueryClient()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width: 720, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
          <ChangeCard card={card} store={() => {}} refresh={() => {}} remove={() => {}} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
