/**
 * The tool row's MCP App slot: app live, and app recorded but not viewable.
 *
 * `ToolCallLine` decides between the two from the STORE and the row's persisted
 * meta, never from a prop, so a faithful frame has to seed both:
 *
 *   app live       `chat.mcpApps[<slot>\u001F<id>]` holds the render payload
 *                  -> the sandboxed iframe mounts
 *   app recorded   `meta.mcp_app` true and NO payload for that id
 *                  -> the notice renders where the frame would have been
 *   same, in panel  as above with `appInPanel`, the side-panel build: no tab
 *                  survives a reload, so the notice replaces the reopen control
 *   ordinary       neither
 *                  -> nothing below the row, unchanged
 *
 * The last row is the control: it is what every non-app tool call keeps looking
 * like.
 *
 * Rows are drawn through the REAL row component inside the transcript's own row
 * wrapper, one per row rather than through `ChatMessageList`, because the list
 * collapses a finished turn into a "3 tool calls" summary and hides the very
 * slot this frame is about. This file hand-writes no Tailwind classes of its
 * own: `capture/` is outside tailwind.config.js's content glob, so a class
 * authored here would not be compiled and the frame could not be trusted.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { combineReducers, configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { ThemeProvider } from '../src/hooks/useTheme'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import chatReducer, { sseMcpAppRender } from '../src/store/chatSlice'
import instancesReducer from '../src/store/instancesSlice'
import { store as realStore } from '../src/store'
import ToolCallLine from '../src/pages/chat/ToolCallLine'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
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
/** The transcript's own row column, so the notice sits where it really does. */
const COLUMN = { maxWidth: 'var(--mc-content-width, 900px)' } as const

let seq = 0
const ts = () => `2026-09-19T04:00:${String(seq++).padStart(2, '0')}.000Z`

const pill = (id: string, label: string, meta: Record<string, unknown> = {}): ChatMessage =>
  ({ role: 'tool', content: `🔧 ${label}`, cls: '', ts: ts(), meta: { tool_call_id: id, ...meta } })

// The lead marker rides with the flag, because the backend writes the two in one
// update: a row the turn flagged is a row whose lead was decided at the same time.
// Seeding the flag alone left this row unable to draw its notice at all once its
// payload was evicted, which the live-swap capture below is what exposed.
//
// It carries `mcp_server` for the same reason, one step further on: the live row's
// notice branch is only reached AFTER an eviction, so nothing exercised it until the
// swap was driven. `_tool_identity_fields` persists the server on every row whose
// backend sends the identity, so a real row reaching this branch NAMES its app --
// seeding it bare made the evicted row fall to the generic wording, which is a state
// the live path does not produce.
const LIVE = pill('t_live', 'create_view', {
  mcp_app: true,
  mcp_app_lead: true,
  mcp_server: 'excalidraw',
})
// The reloaded row carries the server identity, so it draws the NAMED notice --
// the main post-reload case. The side-panel row deliberately does not, so the
// same frame also shows the generic fallback that covers rows persisted before
// the identity fields existed. Both carry `mcp_app_lead`, because the client
// draws the notice only on the row the BACKEND marked as its call's lead: one
// notice per lost app, so a call whose rows are several does not stack them.
// `LIVE` needs no lead -- it renders the app itself, not a notice about a
// missing one.
const GONE = pill('t_gone', 'create_view', {
  mcp_app: true,
  mcp_app_lead: true,
  mcp_server: 'excalidraw',
})
const PANEL = pill('t_panel', 'create_view', { mcp_app: true, mcp_app_lead: true })
const PLAIN = pill('t_plain', 'fs_read ToolCallLine.tsx')

const ROWS: { msg: ChatMessage; appInPanel?: boolean }[] = [
  { msg: LIVE },
  { msg: GONE },
  { msg: PANEL, appInPanel: true },
  { msg: PLAIN },
]

/**
 * The app the live row is showing. SVG with presentation attributes only: the
 * per-app CSP is `default-src 'none'`, so a stylesheet would be blocked and the
 * frame would paint blank while looking like a rendering bug.
 */
const APP_HTML = [
  '<!doctype html><title>diagram</title>',
  '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="440" viewBox="0 0 640 300">',
  '<rect width="640" height="300" fill="#ffffff"/>',
  '<rect x="60" y="100" width="180" height="96" rx="14" fill="#ffffff" stroke="#1f2933" stroke-width="3"/>',
  '<text x="150" y="156" font-family="sans-serif" font-size="34" fill="#1f2933" text-anchor="middle">A</text>',
  '<rect x="400" y="100" width="180" height="96" rx="14" fill="#ffffff" stroke="#1f2933" stroke-width="3"/>',
  '<text x="490" y="156" font-family="sans-serif" font-size="34" fill="#1f2933" text-anchor="middle">B</text>',
  '<line x1="245" y1="148" x2="386" y2="148" stroke="#1f2933" stroke-width="3"/>',
  '<polygon points="386,140 400,148 386,156" fill="#1f2933"/>',
  '</svg>',
].join('')

const PAYLOAD = {
  session_key: SLOT,
  tool_call_id: 't_live',
  server: 'excalidraw',
  tool: 'create_view',
  html: APP_HTML,
  csp: '',
  permissions: [],
  spool_id: '0'.repeat(32),
  callback_secret: '',
  structured_content: null,
  tool_input: null,
  result_content: null,
}

const entry = (id: string, text: string, over: Record<string, unknown> = {}) => ({
  type: 'tool', tool_call_id: id, text, ts: 1_789_000_000_000, ...over,
})

const DREW = 'Done - two rounded rectangles labeled A and B connected by an arrow.'

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
      messages: ROWS.map(r => r.msg),
      // Only the live row has a payload. The other app rows' ids are
      // deliberately absent, which is the state a reloaded transcript is in.
      mcpApps: { [`${SLOT}\u001Ft_live`]: PAYLOAD },
      toolLog: [
        // The LIVE row only. `toolLog` is runtime-only and starts empty on a
        // reload (`chatSlice.ts`: "bucket's toolLog/subagents are runtime-only
        // and start empty"), so giving a reloaded row a log entry would have
        // been unfaithful -- and it masked the branch under test, because the
        // log entry supplies the server identity when it is present and the
        // row's persisted meta supplies it when it is not.
        entry('t_live', 'create_view', { purpose: 'Draw the two-box diagram', input: '{"elements":2}', output: DREW }),
        entry('t_plain', 'fs_read', { purpose: 'Read the tool row', input: '{"path":"website/src/pages/chat/ToolCallLine.tsx"}', output: 'export default memo(function ToolCallLine(...)' }),
      ],
    },
  },
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

/** Drive the REAL eviction, for the capture that photographs the live swap.
 *
 *  Rendering a new app into the same slot is what drops the oldest payload
 *  (`chatSlice.sseMcpAppRender` trims to a per-slot cap), so the notice that
 *  appears here arrives the way it does in a real session -- through the shipped
 *  reducer -- rather than through a delete written for the photograph.
 *
 *  The cap is module-private, so this dispatches until the live row's payload is
 *  gone instead of repeating the number and drifting from it. Returns how many
 *  renders it took, or -1 if the payload outlived the safety bound, so the
 *  caller can fail rather than photograph an unchanged row.
 */
;(window as unknown as { __kcEvictLiveApp?: () => number }).__kcEvictLiveApp = () => {
  const liveKey = `${SLOT}\u001Ft_live`
  for (let i = 0; i < 200; i++) {
    if (!store.getState().chat.mcpApps[liveKey]) return i
    store.dispatch(sseMcpAppRender({ ...PAYLOAD, session_key: SLOT, tool_call_id: `t_evict_${i}` }))
  }
  return -1
}

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <div
            data-capture-root
            className="bg-bg text-text relative"
            style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
          >
            <div className="py-4">
              {ROWS.map(({ msg, appInPanel }) => (
                <div
                  key={String(msg.meta?.tool_call_id)}
                  data-row={String(msg.meta?.tool_call_id)}
                  className="px-4 mx-auto w-full py-1"
                  style={COLUMN}
                >
                  <ToolCallLine
                    message={msg}
                    running={false}
                    slot={SLOT}
                    appInPanel={appInPanel}
                    onOpenApp={() => {}}
                  />
                </div>
              ))}
            </div>
          </div>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
