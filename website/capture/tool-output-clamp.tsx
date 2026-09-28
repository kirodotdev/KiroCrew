/**
 * A tool row whose result came in above `TOOL_OUTPUT_MAX_CHARS`.
 *
 * The live tool log clamps each result to head + tail with both cuts snapped
 * to a line break and records the seam as `output_cut` (`clampToolOutput` in
 * store/chatSlice.ts); the details panel renders the localized
 * `…(N characters truncated — …)` marker at that seam. This entry does NOT
 * preload a clamped string: it seeds
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
 *   ?row=output (default) | edit
 *
 * `row=edit` renders the other clamped surface: an `edit`-kind row whose INPUT
 * is a long-line create diff over the ceiling but under the 400-line card cap.
 * The clamp records `input_cut`, so `presentToolDiff` must route the row to
 * the truncated summary chip (never a complete-looking patch card) and the
 * details panel's Input pane shows the marker at the seam.
 */
import { createRoot } from 'react-dom/client'
import { combineReducers, configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import chatReducer, { sseToolActivity, sseToolResult } from '../src/store/chatSlice'
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
const row = params.get('row') === 'edit' ? 'edit' : 'output'

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

const EDIT_ID = 't_edit'
const EDIT_PATH = '/home/u/proj/dist/app.min.js'
const EDIT_ROW: ChatMessage = {
  role: 'tool', content: `🔧 fs_write ${EDIT_PATH}`, cls: '',
  ts: '2026-09-14T22:00:01.000Z', meta: { tool_call_id: EDIT_ID },
}

/** One ~48 000-character minified line: `seedN;seedN+1;…`. No slashes, same
 *  reason as `block` above. */
const longLine = (seed: string): string =>
  Array.from({ length: 4000 }, (_, i) => `${seed}${i}`).join(';')

// A create diff of three ~48k lines: ~145k characters, 6 lines. Over the
// 64k ceiling, far under the 400-line card cap — the shape that rendered as a
// complete card once the clamp stopped writing a marker line into the text.
// The middle line carries the sentinel the clamp must drop.
const BIG_DIFF = [
  `--- ${EDIT_PATH}`,
  `+++ ${EDIT_PATH}`,
  '@@ -0,0 +1,3 @@',
  '+' + longLine('var head'),
  '+' + longLine('ELIDED_SENTINEL'),
  '+' + longLine('var tail'),
  '',
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
      // The edit row is shown after its turn: no output ever arrives for a
      // write, and an idle slot is what marks the row done.
      slotRunning: row === 'output',
      messages: [row === 'edit' ? EDIT_ROW : ROW],
      toolLog: [row === 'edit'
        ? {
          type: 'tool', tool_call_id: EDIT_ID, text: 'fs_write', kind: 'edit', ts: 1_789_000_000_000,
          purpose: 'Write the minified bundle', input: '', output: null,
        }
        : {
          type: 'tool', tool_call_id: ID, text: 'shell', ts: 1_789_000_000_000,
          purpose: 'Run the suite verbosely', input: '{"command":"npm run test -- --reporter=verbose"}', output: null,
        }],
    },
  },
})

// Before the render below: the details panel reads the marker through `i18nT`,
// and an uninitialized i18next hands back the bare key tail instead.
initI18n('en')

// The payload arrives through the same reducers the websocket feed uses: the
// result for the shell row, the populated `tool_call_update` frame for the
// edit row (claude-agent-acp sends the diff in that second-phase frame).
if (row === 'edit') {
  store.dispatch(sseToolActivity({
    slot: SLOT, tool: 'fs_write', kind: 'edit', purpose: 'Write the minified bundle',
    input_preview: BIG_DIFF, tool_call_id: EDIT_ID, is_update: true,
  }))
} else {
  store.dispatch(sseToolResult({ slot: SLOT, output: BIG_OUTPUT, tool_call_id: ID }))
}
const ACTIVE_ROW = row === 'edit' ? EDIT_ROW : ROW
const RAW = row === 'edit' ? BIG_DIFF : BIG_OUTPUT
const storedEntry = store.getState().chat.toolLog[0]

// Expand through the host-owned disclosure path. ChatMessageList keys a row
// `${ts}-${index}-${role}` and the tool renderer folds the tool_call_id in via
// `toolDisclosureKey`, so this is the same key the shipped transcript reads.
const expandedKey = toolDisclosureKey(ACTIVE_ROW, `${ACTIVE_ROW.ts}-0-tool`)

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
          data-row={row}
          data-raw-length={RAW.length}
          data-input-cut={storedEntry.input_cut ? JSON.stringify(storedEntry.input_cut) : ''}
          className="bg-bg text-text relative"
          style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
        >
          <div className="py-4">
            <ChatMessageList messages={[ACTIVE_ROW]} running={row === 'output'} contentWidth="800px" renderers={renderers} />
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
