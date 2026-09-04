/**
 * Visual evidence for "transcript row geometry belongs to the host".
 *
 * WHY ISOLATED: reaching these four cards in a live session means provoking a
 * workflow run, a spawn wave and both completion events in one transcript, with
 * a sibling row adjacent for comparison. That is not reproducible on demand, and
 * a half-seeded ChatPage draws its error boundary instead of the rows.
 *
 * WHAT IS FAITHFUL is the COLUMN GEOMETRY, since that is the whole claim. Rows
 * are wrapped in the literal host wrapper (`px-5 mx-auto w-full py-1` under
 * `--mc-content-width`, ChatPage.tsx:6522 / ChatMessageList.tsx:152), and the
 * components are the real ones.
 *
 * The `before` scene adds a SECOND wrapper of exactly that shape around each
 * card. That is not a mock of the bug — it is literally what the pre-fix code
 * did, since the card applied the same classes at its own root inside the host's
 * wrapper. `after` renders the current code through one wrapper only.
 *
 * The dashed line marks the column's text edge, so a row that does not start on
 * it is misaligned with its siblings.
 *
 *   ?scene=before|after &theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort
 *   node scripts/capture-transcript-row-geometry.mjs http://127.0.0.1:6814 \
 *     ../temp-screenshots/transcript-row-geometry
 */
import { createRoot } from 'react-dom/client'
import { combineReducers, configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { initI18n } from '../src/i18n'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import chatReducer from '../src/store/chatSlice'
import instancesReducer from '../src/store/instancesSlice'
import { store as realStore } from '../src/store'

import WorkflowRunCard from '../src/pages/chat/WorkflowRunCard'
import SubagentRunCard from '../src/pages/chat/SubagentRunCard'
import WorkflowCompletionCard from '../src/pages/chat/WorkflowCompletionCard'
import SubagentCompletionCard from '../src/pages/chat/SubagentCompletionCard'
import NudgeCard from '../src/pages/chat/NudgeCard'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The completion cards mount MarkdownRenderer when expanded, which probes
// path-like inline code and unfurls links. Neither endpoint exists here, and a
// pending probe leaves a chip mid-load, so answer both deterministically.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-read')) {
    return Promise.resolve(new Response(null, { status: 200, headers: { 'X-Path-Kind': 'file' } }))
  }
  if (url.startsWith('/api/link-meta')) return Promise.resolve(Response.json({}))
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

// The two run cards read live progress from the store rather than from props, so
// the representative "1 running / 1 done" state has to exist there.
const rootReducer = combineReducers({
  dashboard: dashboardReducer,
  notifications: notificationsReducer,
  chat: chatReducer,
  instances: instancesReducer,
})
const base = realStore.getState()
const store = configureStore({
  reducer: rootReducer,
  preloadedState: {
    ...base,
    chat: {
      ...base.chat,
      activeSlot: 'main',
      workflowRuns: {
        wf_1: {
          run_id: 'wf_1',
          name: 'Deep research: pizza origins',
          phase: 'synthesis',
          lastLog: 'merging 4 agent reports',
          status: 'running',
        },
      },
      subagents: {
        a1: {
          id: 'a1', task: 'audit the transcript cards', agent: 'kirocrew',
          status: 'running', streaming: '', lastTool: 'fs_read', startedAt: 0, elapsed: 4200,
        },
        a2: {
          id: 'a2', task: 'audit the turn grouping', agent: 'kirocrew',
          status: 'done', streaming: '', lastTool: '', startedAt: 0, elapsed: 9100,
        },
      },
      subagentQueued: { main: 0 },
    },
  },
})

const msg = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ts: '2026-08-17T00:00:00.000Z', meta })

const COLUMN = { maxWidth: 'var(--mc-content-width, 900px)' } as const

/**
 * One transcript row.
 *
 * `nested` reproduces the pre-fix arrangement by adding the second wrapper the
 * card used to carry itself — same classes, same clamp, one level in.
 */
function Row({ label, nested, children }: { label: string; nested?: boolean; children: ReactNode }) {
  return (
    <>
      <div className="px-5 mx-auto w-full" style={COLUMN}>
        <div className="text-[10px] uppercase tracking-wider text-accent/70 pt-2 pb-0.5 font-mono">{label}</div>
      </div>
      <div data-row={label} className="px-5 mx-auto w-full py-1" style={COLUMN}>
        {nested
          ? <div className="px-5 mx-auto w-full py-0.5" style={COLUMN}><div data-card>{children}</div></div>
          : <div data-card>{children}</div>}
      </div>
    </>
  )
}

const WF_COMPLETION = [
  '[Workflow completion event]',
  'Workflow `pizza-origins` (wf_1) → **finished**',
  '',
  '### Result',
  'Naples, 18th century. Four sources agree.',
].join('\n')

const SA_COMPLETION = [
  '[Subagent completion event]',
  'Agent `a1` (kirocrew) completed ✅',
  'Task: Audit the transcript cards',
  '',
  'Reported per-component props and root classes.',
].join('\n')

/** The four cards under test, plus two untouched siblings as the alignment reference. */
const ROWS: [string, ReactNode, boolean][] = [
  ['nudge (reference row)', <NudgeCard message={msg('user', '[auto-nudge cycle 3]\nCheck CI on the open PR.', { nudge: { cycle: 3 } })} disclosureKey="g-n" />, false],
  ['workflow_run', <WorkflowRunCard runId="wf_1" message={msg('tool', '🔧 workflow_run', { input: '{"intent":"pizza origins"}' })} />, true],
  ['spawn_run', <SubagentRunCard slot="main" launch={{ ids: ['a1', 'a2'], announced: 2 }} />, true],
  ['workflow completion', <WorkflowCompletionCard message={msg('assistant', WF_COMPLETION)} disclosureKey="g-wc" />, true],
  ['subagent completion', <SubagentCompletionCard message={msg('subagent', SA_COMPLETION)} disclosureKey="g-sc" />, true],
  ['error (reference row)', <ErrorCard content="⟳ Connection lost — please retry." onContinue={() => {}} />, false],
]

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div
          data-capture-root
          data-scene={scene}
          className="bg-bg text-text relative"
          style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
        >
          {/* The column's text edge: a 50px page gutter plus the 20px of px-5. */}
          <div
            className="absolute top-0 bottom-0 border-l border-dashed border-accent/40 pointer-events-none"
            style={{ left: 'calc(50% - 400px + 20px)' }}
          />
          <div className="py-4">
            {ROWS.map(([label, node, affected], i) => (
              <Row key={i} label={label} nested={scene === 'before' && affected}>{node}</Row>
            ))}
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
