/**
 * The detail rail of an expanded tool row, in every status, on both surfaces
 * that draw one: the main chat's store-connected `ToolCallLine` (left column)
 * and the SDK's store-free `ToolCallPill` (right column). Both take the rail
 * colour from `utils/toolRailColor.ts`; this page is the before/after
 * evidence that moving the mapping there changed no colour.
 *
 * Main-chat states are seeded through the store, as capture/tool-call-states.tsx
 * explains (`slotRunning` must be true, the 🚫 sibling must follow its row).
 * Embed states come from props and row shape: `meta.output` for done, a
 * `permission` row for awaiting approval, `meta.resolved: 'rejected'` for
 * rejected, the `autoDenied` prop for a gate block. Every embed row carries
 * `meta.input`, because only a payload row draws the rail.
 *
 * Every row starts expanded on the main side through `toolDisclosure`; the
 * capture script clicks the embed pills open.
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
import { ToolCallPill } from '../src/app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-read')) return Promise.resolve(new Response(null, { status: 404 }))
  if (url.startsWith('/api/link-meta')) return Promise.resolve(Response.json({}))
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

const SLOT = 'main'
let seq = 0
const ts = () => `2026-11-01T04:00:${String(seq++).padStart(2, '0')}.000Z`

// --- main chat (ToolCallLine through the real registry) --------------------
const pill = (id: string, label: string): ChatMessage =>
  ({ role: 'tool', content: `🔧 ${label}`, cls: '', ts: ts(), meta: { tool_call_id: id } })

const OK = pill('t_ok', 'done')
const RUN = pill('t_run', 'running')
const PEND = pill('t_pend', 'awaiting approval')
const REJ = pill('t_rej', 'rejected')
const DENY = pill('t_deny', 'auto-denied')
const ROWS: ChatMessage[] = [OK, RUN, PEND, REJ, DENY]

const SIBLINGS: ChatMessage[] = [
  { role: 'permission', content: '🔧 Running: git push', cls: '', ts: ts(), meta: { tool_call_id: 't_pend', approval_id: 'ap-1', tool_input: 'git push' } },
  { role: 'permission', content: '🔧 Running: rm -rf build', cls: '', ts: ts(), meta: { tool_call_id: 't_rej', approval_id: 'ap-2', resolved: 'rejected' } },
  { role: 'tool', content: '🚫 shell', cls: '', ts: ts(), meta: { tool_call_id: 't_deny' } },
]

const entry = (id: string, purpose: string, output: string | null) => ({
  type: 'tool', tool_call_id: id, text: 'shell', ts: 1_793_000_000_000, purpose, input: '{"command":"echo rail"}', output,
})

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
      slotRunning: true,
      messages: [...ROWS, ...SIBLINGS],
      toolLog: [
        entry('t_ok', 'done', 'rail'),
        entry('t_run', 'running', null),
        entry('t_pend', 'awaiting approval', null),
        entry('t_rej', 'rejected', null),
        entry('t_deny', 'auto-denied', null),
      ],
    },
  },
})

const toolDisclosure = Object.fromEntries(ROWS.map((m, i) => [`${m.ts}-${i}-tool`, true]))
const renderers = createTranscriptRenderers({
  slot: SLOT,
  onFileOpen: () => {},
  onFolderOpen: () => {},
  onOpenSubagentPanel: () => {},
  onToolDisclosureChange: () => {},
  toolDisclosure,
  appInPanel: false,
  onOpenApp: () => {},
})

// --- embed (store-free ToolCallPill) ----------------------------------------
const emb = (role: ChatMessage['role'], label: string, meta: Record<string, unknown> = {}): ChatMessage =>
  ({ role, content: `🔧 ${label}`, cls: '', ts: ts(), meta: { purpose: label, input: '{"command":"echo rail"}', ...meta } })

const EMBED: { msg: ChatMessage; autoDenied?: boolean }[] = [
  { msg: emb('tool', 'done', { output: 'rail' }) },
  { msg: emb('tool', 'running') },
  { msg: emb('permission', 'awaiting approval') },
  { msg: emb('permission', 'rejected', { resolved: 'rejected' }) },
  { msg: emb('tool', 'auto-denied'), autoDenied: true },
]

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div data-capture-root className="bg-bg text-text flex gap-4 p-4" style={{ width: 1100 }}>
          <div data-surface="main" className="flex-1 min-w-0" style={{ ['--mc-content-width' as string]: '520px' }}>
            <ChatMessageList messages={ROWS} running contentWidth="520px" renderers={renderers} />
          </div>
          <div data-surface="embed" className="flex-1 min-w-0 flex flex-col gap-2">
            {EMBED.map(({ msg, autoDenied }) => (
              <ToolCallPill key={msg.ts} message={msg} running autoDenied={autoDenied} />
            ))}
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
