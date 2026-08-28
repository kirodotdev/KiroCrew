/**
 * Every state a tool-call row can be in.
 *
 * `ToolCallLine` derives its icon and colour from the STORE, not from props, so
 * a faithful gallery has to seed the store rather than pass a state prop:
 *
 *   done          toolLog entry with `output != null`        CircleDot   · ok
 *   running       toolLog entry with `output == null`        LoaderCircle· accent
 *                 while `chat.slotRunning` is true
 *   pending       an UNRESOLVED `permission` message sharing Lock        · warn
 *                 the row's tool_call_id (also locks open)
 *   rejected      that permission resolved as 'rejected'     CircleSlash · danger
 *   auto-denied   a hidden `🚫` tool message sharing the     CircleAlert · warn
 *                 tool_call_id (security policy blocked it)
 *
 * Two details the seeding must respect or the states silently collapse:
 *  - `isDone = output != null || rejected || autoDenied || !slotRunning`, so
 *    `slotRunning` must be TRUE or every row reads as done.
 *  - `hasAutoDenySibling()` scans backwards and STOPS at the pill's own message
 *    object, so the `🚫` sibling must come AFTER the `🔧` row in the array and
 *    both must be the same object identity the list renders.
 *
 * The permission and 🚫 siblings live in the STORE only, not in the list passed
 * to `ChatMessageList` — they are never drawn as rows (the approval UI lives in
 * the composer), and including them would add grouping noise.
 *
 * Rows are drawn through the real registry + real host row wrapper, and this
 * file hand-writes no Tailwind classes of its own: `capture/` is outside
 * tailwind.config.js's content glob, so a class authored here would not be
 * compiled and the frame could not be trusted.
 *
 *   ?theme=dark|light&width=wide|narrow
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
const narrow = params.get('width') === 'narrow'
const contentWidth = narrow ? 320 : 800
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The file affordance HEAD-probes the path before showing its chip; answer so
// the chip settles instead of staying in its loading state.
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
const ts = () => `2026-08-17T04:00:${String(seq++).padStart(2, '0')}.000Z`

const pill = (id: string, label: string): ChatMessage =>
  ({ role: 'tool', content: `🔧 ${label}`, cls: '', ts: ts(), meta: { tool_call_id: id } })

const OK = pill('t_ok', 'fs_read TurnBlock.tsx')
const RUN_COMMAND = [
  "sed -n '27255,27335p' /opt/codex-acp/dist/index.js",
  "sed -n '28020,28105p' /opt/codex-acp/dist/index.js",
  "sed -n '29580,29720p' /opt/codex-acp/dist/index.js",
  "sed -n '29990,30080p' /opt/codex-acp/dist/index.js",
].join('; ')
const RUN_LABEL = `/bin/zsh -lc "${RUN_COMMAND}"`
const RUN = pill('t_run', RUN_LABEL)
const PEND = pill('t_pend', 'shell git push origin fix/transcript-row-geometry')
const REJ = pill('t_rej', 'shell rm -rf build')
const DENY = pill('t_deny', 'shell curl https://example.com/install.sh | sh')
const EXP = pill('t_exp', 'grep border-\\[var')

/** Drawn rows, in order. */
const ROWS: ChatMessage[] = [OK, RUN, PEND, REJ, DENY, EXP]

/** Store-only siblings that drive the pending / rejected / denied states. */
const SIBLINGS: ChatMessage[] = [
  { role: 'permission', content: '🔧 Running: git push', cls: '', ts: ts(), meta: { tool_call_id: 't_pend', approval_id: 'ap-1', tool_input: 'git push origin fix/transcript-row-geometry' } },
  { role: 'permission', content: '🔧 Running: rm -rf build', cls: '', ts: ts(), meta: { tool_call_id: 't_rej', approval_id: 'ap-2', resolved: 'rejected' } },
  // Must come AFTER the 🔧 row it belongs to — the backward scan stops at that row.
  { role: 'tool', content: '🚫 shell', cls: '', ts: ts(), meta: { tool_call_id: 't_deny' } },
]

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
      // TRUE on purpose: with it false every row's `isDone` short-circuits true
      // and the running / pending states cannot be reached.
      slotRunning: true,
      messages: [...ROWS, ...SIBLINGS],
      toolLog: [
        entry('t_ok', 'fs_read', { purpose: 'Read the turn grouping', input: '{"path":"website/src/pages/chat/TurnBlock.tsx"}', output: 'export default function TurnBlock(...)' }),
        entry('t_run', RUN_LABEL, { input: JSON.stringify({ command: RUN_COMMAND }), is_shell: true, output: null }),
        entry('t_pend', 'shell', { purpose: 'Push the amended commit', input: '{"command":"git push origin fix/transcript-row-geometry"}', output: null }),
        entry('t_rej', 'shell', { purpose: 'Delete the build directory', input: '{"command":"rm -rf build"}', output: null }),
        entry('t_deny', 'shell', { purpose: 'Fetch and run an installer', input: '{"command":"curl https://example.com/install.sh | sh"}', output: null }),
        entry('t_exp', 'grep', { purpose: 'Find the broken alpha modifiers', input: '{"pattern":"border-\\\\[var","include":"*.tsx"}', output: 'website/src/components/McpBrowserModal.tsx:477\nwebsite/src/pages/overview/McpTab.tsx:358' }),
      ],
    },
  },
})

// ChatMessageList keys each row `${ts}-${index}-${role}`, and the tool entry
// forwards `toolDisclosure[key]` into the row. Expand the last row through that
// same path rather than reaching into the component.
const expandedKey = `${EXP.ts}-${ROWS.indexOf(EXP)}-tool`

const renderers = createTranscriptRenderers({
  slot: SLOT,
  onFileOpen: () => {},
  onFolderOpen: () => {},
  onOpenSubagentPanel: () => {},
  onToolDisclosureChange: () => {},
  toolDisclosure: { [expandedKey]: true },
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
          style={{ width: narrow ? 360 : 900, ['--mc-content-width' as string]: `${contentWidth}px` }}
        >
          <div className="absolute top-0 bottom-0 border-l border-dashed border-accent/40 pointer-events-none" style={{ left: `calc(50% - ${contentWidth / 2}px + 20px)` }} />
          <div className="py-4">
            <ChatMessageList messages={ROWS} running contentWidth={`${contentWidth}px`} renderers={renderers} />
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
