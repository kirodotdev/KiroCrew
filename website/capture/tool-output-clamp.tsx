/**
 * A tool row whose result came in above `TOOL_OUTPUT_MAX_CHARS`.
 *
 * The live tool log clamps each result to head + `…(N characters truncated — …)`
 * + tail, with both cuts snapped to a line break (`clampToolOutput` in
 * store/chatSlice.ts). This entry does NOT preload a clamped string: it seeds
 * the row with `output: null` and then dispatches the real `sseToolResult`
 * action with a 120 000-character result, so the frame shows what the reducer
 * actually stores. The driver scrolls the Output panel to the marker and
 * asserts the elided middle is gone, the count matches, and the rows touching
 * the marker are whole.
 *
 * Marker lines are deliberately distinct — `HEAD-`, `ELIDED-SENTINEL`, `TAIL-` —
 * so a screenshot can be read against the text: the sentinel sits at
 * character ~70 000, inside the slice the clamp drops.
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
import chatReducer, { sseToolResult } from '../src/store/chatSlice'
import instancesReducer from '../src/store/instancesSlice'
import { store as realStore } from '../src/store'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createTranscriptRenderers, toolDisclosureKey } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'main'
const ID = 't_big'

const ROW: ChatMessage = {
  role: 'tool', content: '🔧 shell npm run test -- --reporter=verbose', cls: '',
  ts: '2026-09-14T22:00:00.000Z', meta: { tool_call_id: ID },
}

/** `n` lines of `prefix case NNNNN passed`, about 40 000 characters per block.
 *  No slashes: a path-shaped token would make the file-chip affordance probe
 *  every line, and the frame would never settle. */
const block = (prefix: string, n: number): string =>
  Array.from({ length: n }, (_, i) => `${prefix} case ${String(i + 1).padStart(5, '0')} passed`).join('\n')

// ~40k head, ~40k middle (with the sentinel), ~40k tail = ~120k, well over the
// 64k ceiling. The clamp keeps the first 48k and the last 12k.
const BIG_OUTPUT = [
  block('HEAD-', 2000),
  block('MID--', 1000),
  'ELIDED-SENTINEL this line is inside the dropped middle',
  block('MID--', 1000),
  block('TAIL-', 2000),
  'exit status: 0',
].join('\n')

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
      messages: [ROW],
      toolLog: [{
        type: 'tool', tool_call_id: ID, text: 'shell', ts: 1_789_000_000_000,
        purpose: 'Run the suite verbosely', input: '{"command":"npm run test -- --reporter=verbose"}', output: null,
      }],
    },
  },
})

// Before the dispatch below: the clamp reads its marker through `i18nT`, and
// an uninitialized i18next hands back the bare key tail instead.
initI18n('en')

// The result arrives through the same reducer the websocket feed uses.
store.dispatch(sseToolResult({ slot: SLOT, output: BIG_OUTPUT, tool_call_id: ID }))

// Expand through the host-owned disclosure path. ChatMessageList keys a row
// `${ts}-${index}-${role}` and the tool renderer folds the tool_call_id in via
// `toolDisclosureKey`, so this is the same key the shipped transcript reads.
const expandedKey = toolDisclosureKey(ROW, `${ROW.ts}-0-tool`)

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

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div
          data-capture-root
          data-raw-length={BIG_OUTPUT.length}
          className="bg-bg text-text relative"
          style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
        >
          <div className="py-4">
            <ChatMessageList messages={[ROW]} running contentWidth="800px" renderers={renderers} />
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
