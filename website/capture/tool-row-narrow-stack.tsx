/**
 * A tool row whose label is long and whose file chip is present — the pair the
 * pill layout has to share a line with.
 *
 * The row's own width is NOT fixed here: the wrapper is full-width and the
 * content cap comes from `--mc-content-width`, which `?colw=` sets. That is what
 * lets one fixture separate the two axes a reader might confuse. The viewport
 * and the column are NOT the same measurement in this app — `ChatPane` sets
 * `--mc-content-width: 100%`, so a quarter-width pane in the session grid is a
 * ~350px column at a 1440px viewport — and the defect follows the COLUMN. A
 * fixture pinned to one width (as tool-call-states.tsx is, at 900px) cannot
 * exercise either.
 *
 * Seeding notes carried over from tool-call-states.tsx, which are what make the
 * row render its real shape rather than a plausible-looking fallback:
 *  - the chip HEAD-probes its path, so `/api/file-read` must answer 200 or the
 *    chip never appears and the fixture proves nothing about a two-item row;
 *  - `simplifiedToolNames` (default on) is what puts the agent's PROSE purpose
 *    in the label — the case where the label is long enough to be starved;
 *  - `slotRunning` stays false so every row reads done: no shimmer, no spinner,
 *    so a screenshot is deterministic.
 *
 * This file hand-writes no Tailwind classes: `capture/` is outside
 * tailwind.config.js's content glob, so a class authored here would not be
 * compiled and the frame could not be trusted.
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
// The column cap, standing in for whichever container the row is rendered into:
// the 800px main chat column by default, or a session-grid pane's own width.
const colw = params.get('colw') || '800px'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

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
let seq = 0
const ts = () => `2026-08-25T04:00:${String(seq++).padStart(2, '0')}.000Z`

const pill = (id: string, label: string): ChatMessage =>
  ({ role: 'tool', content: `🔧 ${label}`, cls: '', ts: ts(), meta: { tool_call_id: id } })

const LONG = pill('t_long', 'fs_write')
const SHORT = pill('t_short', 'fs_read')
const ROWS: ChatMessage[] = [LONG, SHORT]

const LONG_PATH = 'src/main/resources/recipes/membership_actions_show_star_skeleton.ion'
const SHORT_PATH = 'website/src/pages/chat/TurnBlock.tsx'

const entry = (id: string, text: string, over: Record<string, unknown> = {}) => ({
  type: 'tool', tool_call_id: id, text, ts: 1_786_000_000_000, ...over,
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
      slotRunning: false,
      messages: ROWS,
      toolLog: [
        entry('t_long', 'fs_write', {
          purpose: 'Editing membership_actions_show_star_skeleton.ion',
          input: JSON.stringify({ path: LONG_PATH }),
          output: 'ok',
        }),
        entry('t_short', 'fs_read', {
          purpose: 'Reading the turn grouping',
          input: JSON.stringify({ path: SHORT_PATH }),
          output: 'export default function TurnBlock(...)',
        }),
      ],
    },
  },
})

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
          className="bg-bg text-text w-full"
          style={{ ['--mc-content-width' as string]: colw }}
        >
          <div className="py-4">
            <ChatMessageList messages={ROWS} contentWidth={colw} renderers={renderers} />
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
