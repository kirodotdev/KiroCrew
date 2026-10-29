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
 * Scene comes from the query string: ?scene=save|update|update-far|applied&theme=dark|light
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
  'RULES = """',
  '-- never delete files',
  '"""',
  '',
  'async def workflow(ctx):',
  '    notes = await ctx.agent("Reproduce the failure: " + ctx.args.get("input", ""))',
  '    return await ctx.agent(RULES + "Name the root cause in one paragraph, then list the files you read and the commands you ran:\\n" + notes)',
].join('\n')
const V2 = V1
  .replace('Name the root cause in one paragraph', 'Name the root cause and the fix in one paragraph')
  .replace('-- never delete files', '++ also delete the build cache')
// The gateway's header-free diff of V1 -> V2: a source line starting with `--`
// or `++` is a removed or added line like any other, and a long line stays one
// row and scrolls sideways. Its one hunk opens with the separator row naming
// the lines it spans.
const DIFF = [
  '\u22ef lines 1-9 \u22ef',
  ' META = {"name": "investigate", "description": "Find the cause of a failure"}',
  ' ',
  ' RULES = """',
  '--- never delete files',
  '+++ also delete the build cache',
  ' """',
  ' ',
  ' async def workflow(ctx):',
  '     notes = await ctx.agent("Reproduce the failure: " + ctx.args.get("input", ""))',
  '-    return await ctx.agent(RULES + "Name the root cause in one paragraph, then list the files you read and the commands you ran:\\n" + notes)',
  '+    return await ctx.agent(RULES + "Name the root cause and the fix in one paragraph, then list the files you read and the commands you ran:\\n" + notes)',
].join('\n')

// Two far-apart hunks: a change inside the `confirmed` guard, then an unguarded
// call added twenty lines further down. The gateway opens each hunk with a
// separator row naming the lines it spans, so the added call never reads as
// part of the guarded block above it.
const FAR_V1 = [
  'META = {"name": "cleanup", "description": "Tidy the build output"}',
  '',
  'async def workflow(ctx):',
  '    if ctx.args.get("confirmed"):',
  '        await ctx.agent("List the stale build folders")',
  '    report = await ctx.agent("Summarize the build log")',
  ...Array.from({ length: 20 }, (_, i) => `    step${i + 1} = await ctx.agent("Check step ${i + 1} of the build")`),
  '    return report',
]
const FAR_V2 = [...FAR_V1]
FAR_V2[4] = '        await ctx.agent("List the stale build folders and their sizes")'
FAR_V2.splice(23, 0, '    await ctx.agent("Delete every stale build folder")')
const FAR_DIFF = [
  '\u22ef lines 2-8 \u22ef',
  ' ',
  ' async def workflow(ctx):',
  '     if ctx.args.get("confirmed"):',
  '-        await ctx.agent("List the stale build folders")',
  '+        await ctx.agent("List the stale build folders and their sizes")',
  '     report = await ctx.agent("Summarize the build log")',
  '     step1 = await ctx.agent("Check step 1 of the build")',
  '     step2 = await ctx.agent("Check step 2 of the build")',
  '\u22ef lines 21-27 \u22ef',
  '     step15 = await ctx.agent("Check step 15 of the build")',
  '     step16 = await ctx.agent("Check step 16 of the build")',
  '     step17 = await ctx.agent("Check step 17 of the build")',
  '+    await ctx.agent("Delete every stale build folder")',
  '     step18 = await ctx.agent("Check step 18 of the build")',
  '     step19 = await ctx.agent("Check step 19 of the build")',
  '     step20 = await ctx.agent("Check step 20 of the build")',
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
  plan: { apply: [{ method: 'POST', path: '/api/workflows/definitions', body: { source: V1, name: 'Investigate', description: 'Find the cause of a failure', slug: 'investigate', exact_slug: true } }], undo: null },
  undo_unavailable_reason: 'no_identity_check',
}
const update: Card = {
  ...base, kind: 'workflow.update',
  title: 'Save a new revision of workflow “Investigate”',
  params: { workflow: 'investigate', source: V2 },
  changes: [
    { field: 'workflow_command', label: 'Command', after: '/workflow investigate' },
    { label: 'Revision', before: 2, after: 3 },
    { field: 'workflow_diff', label: 'Changes to the script', after: DIFF },
    { field: 'workflow_source', label: 'Script', after: V2, detail: true },
  ],
  plan: { apply: [{ method: 'PATCH', path: '/api/workflows/definitions/wfd_0123456789abcdef', body: { source: V2, expected_revision: 2 } }], undo: null },
  undo_unavailable_reason: 'no_identity_check',
}
const updateFar: Card = {
  ...base, kind: 'workflow.update',
  title: 'Save a new revision of workflow “Cleanup”',
  params: { workflow: 'cleanup', source: FAR_V2.join('\n') },
  changes: [
    { field: 'workflow_command', label: 'Command', after: '/workflow cleanup' },
    { label: 'Revision', before: 4, after: 5 },
    { field: 'workflow_diff', label: 'Changes to the script', after: FAR_DIFF },
    { field: 'workflow_source', label: 'Script', after: FAR_V2.join('\n'), detail: true },
  ],
  plan: { apply: [{ method: 'PATCH', path: '/api/workflows/definitions/wfd_00000000cleanup0', body: { source: FAR_V2.join('\n'), expected_revision: 4 } }], undo: null },
  undo_unavailable_reason: 'no_identity_check',
}
const applied: Card = { ...save, status: 'applied', result: { summary: 'Done: Save workflow “Investigate” to the library' } }
const card = { save, update, 'update-far': updateFar, applied }[scene as 'save' | 'update' | 'update-far' | 'applied'] ?? save

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
