/**
 * Style inventory for every chat transcript row kind.
 *
 * This is the evidence the style-convergence proposal is argued from: the rows
 * currently span four horizontal padding steps, two radii and two font sizes,
 * and several bypass the theme tokens for raw Tailwind palette colours.
 *
 * WHY IT MOUNTS ChatMessageList RATHER THAN EACH CARD. Two reasons, and the
 * second is the one that matters:
 *
 *  1. Every row then goes through the REAL registry (`createTranscriptRenderers`
 *     merged over the SDK defaults) and the REAL host row wrapper (`ctx.row` /
 *     `ctx.wrapper`), so the frame shows the row as a reader sees it — including
 *     whether the rows actually share a left edge.
 *  2. It means this file hand-writes NO Tailwind classes. `tailwind.config.js`
 *     scans `['./index.html', './src/**\/*.{ts,tsx}']`; `capture/` is outside
 *     that glob, so a class authored HERE is never compiled. A row hand-built in
 *     a capture file therefore renders unstyled whatever its classes say, which
 *     makes it unfalsifiable evidence — it cannot distinguish a real styling
 *     defect from a file Tailwind never read. Only classes that already live
 *     under `src/` are trustworthy in a captured frame.
 *
 * The dashed line marks the column's text edge. The runner's DOM probe reports
 * each row's box offset from it, its own padding, radius and font size.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { combineReducers, configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import chatReducer from '../src/store/chatSlice'
import instancesReducer from '../src/store/instancesSlice'
import { store as realStore } from '../src/store'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// MarkdownRenderer probes path-like inline code and unfurls links; neither
// endpoint exists here and a pending probe leaves a chip mid-load.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-read')) {
    return Promise.resolve(new Response(null, { status: 200, headers: { 'X-Path-Kind': 'file' } }))
  }
  if (url.startsWith('/api/link-meta')) return Promise.resolve(Response.json({}))
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

const SLOT = 'main'

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
      activeSlot: SLOT,
      workflowRuns: {
        wf_1: {
          run_id: 'wf_1', name: 'Deep research: pizza origins', phase: 'synthesis',
          lastLog: 'merging 4 agent reports', status: 'running',
        },
      },
      subagents: {
        a1: {
          id: 'a1', task: 'audit the transcript rows', agent: 'kirocrew',
          status: 'running', streaming: '', lastTool: 'fs_read', startedAt: 0, elapsed: 4200,
        },
        a2: {
          id: 'a2', task: 'audit the turn grouping', agent: 'kirocrew',
          status: 'done', streaming: '', lastTool: '', startedAt: 0, elapsed: 9100,
        },
      },
      subagentQueued: { [SLOT]: 0 },
    },
  },
})

let seq = 0
/** Distinct ts per row: ChatMessageList keys rows off it. */
const msg = (role: string, content: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  role,
  content,
  cls: '',
  ts: `2026-08-17T00:00:${String(seq++).padStart(2, '0')}.000Z`,
  ...over,
})

const MESSAGES: ChatMessage[] = [
  msg('user', 'Audit how every transcript row is styled.'),
  msg('thinking', 'Each row owns its own padding, so they should be photographed against one ruler.'),
  msg('tool', '🔧 fs_read ToolCallLine.tsx', {
    meta: { tool_call_id: 't1', purpose: 'Read the pill root class', input: '{"path":"ToolCallLine.tsx"}', output: 'ok' },
  }),
  msg('tool', '🔧 workflow_run', { meta: { input: '{"intent":"pizza origins"}', output: 'Started workflow run `wf_1`' } }),
  msg('tool', '🔧 spawn_run', {
    meta: { output: 'Spawned 2 subagent(s).\n  a1 (kirocrew): audit rows\n  a2 (kirocrew): audit turns' },
  }),
  msg('assistant', [
    '[Workflow completion event]',
    'Workflow `pizza-origins` (wf_1) → **finished**',
    '',
    '### Result',
    'Naples, 18th century.',
  ].join('\n')),
  msg('subagent', [
    '[Subagent completion event]',
    'Agent `a1` (kirocrew) completed ✅',
    'Task: Audit the transcript rows',
    '',
    'Reported padding, radius and font size per row.',
  ].join('\n')),
  msg('file', JSON.stringify({
    filename: 'q3-report.pdf', description: 'Q3 cost breakdown', size: 248320, content_type: 'application/pdf',
  })),
  msg('nudge', '[auto-nudge cycle 3]\nCheck CI on the open PR.', { meta: { nudge: { cycle: 3 } } }),
  msg('inject', '[Tool refusal — automatic recovery]\n  - shell: Blocked by security policy: rm -rf /.*'),
  msg('error', '⟳ Connection lost — please retry.'),
  msg('notice', 'Switched to model claude-opus-5.'),
  msg('assistant', '', { kind: 'stop_event', meta: { state: 'stopped' } }),
  msg('mcp_oauth', '', {
    meta: { server_name: 'github-mcp', oauth_url: 'https://example.com/login/oauth/authorize?client_id=abc' },
  }),
  msg('assistant', [
    'Four padding steps, two radii, two font sizes.',
    '',
    '- the majority is `px-3 py-2`, `rounded-md`, 13px',
    '- three rows bypass the theme for a raw palette colour',
  ].join('\n')),
]

const renderers = createTranscriptRenderers({
  slot: SLOT,
  onFileOpen: () => {},
  onFolderOpen: () => {},
  onOpenSubagentPanel: () => {},
  onToolDisclosureChange: () => {},
  toolDisclosure: {},
  appInPanel: false,
  onOpenApp: () => {},
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div
          data-capture-root
          className="bg-bg text-text relative"
          style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
        >
          {/* The column's text edge: a 50px page gutter plus the 20px of px-5. */}
          <div
            className="absolute top-0 bottom-0 border-l border-dashed border-accent/40 pointer-events-none"
            style={{ left: 'calc(50% - 400px + 20px)' }}
          />
          <div className="py-4">
            <ChatMessageList messages={MESSAGES} running={false} contentWidth="800px" renderers={renderers} />
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
