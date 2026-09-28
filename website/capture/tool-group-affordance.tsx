/**
 * Both collapsed-tool-group affordances in the app-sdk host, one above the
 * other (#9699).
 *
 * The app-sdk `ChatMessageList` is the one host that renders BOTH surfaces:
 * a settled turn folds its tool rows behind `TurnBlock`'s toggle, and a run of
 * reasoning rows outside a turn renders as a `CollapsibleToolGroup`. The frame
 * is the evidence that the two are the same pill — one glyph, one wrench, one
 * ring — photographed in one viewport so a reader compares them without
 * flipping between files.
 *
 * WHY IT MOUNTS ChatMessageList RATHER THAN THE TWO COMPONENTS. Each affordance
 * then reaches the frame through the real grouping (a turn only forms with >2
 * items and a working step; a reasoning run only groups outside one), the real
 * renderer registry (`createTranscriptRenderers`, so an expanded group shows
 * ThinkingBlock rows, not an empty well) and the real host row wrapper — the
 * geometry a member DM or embed reader actually sees. This file also
 * hand-writes NO Tailwind classes:
 * `capture/` is outside the Tailwind content glob, so a class authored here is
 * never compiled and would make the frame unfalsifiable.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The real store, unseeded: neither surface reads slot state. The endpoints
// MarkdownRenderer probes for path-like code and link unfurls are answered by
// the runner's Playwright routes (see scripts/capture-tool-group-affordance.mjs).
const SLOT = 'main'

let seq = 0
/** Distinct ts per row: ChatMessageList keys rows off it. */
const msg = (role: string, content: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  role,
  content,
  cls: '',
  ts: `2026-09-27T00:00:${String(seq++).padStart(2, '0')}.000Z`,
  ...over,
})

/**
 * Host surface 1 — a settled turn: user prompt, two tool calls, the answer.
 * Four items with working steps, so ChatMessageList wraps it as a turn and
 * TurnBlock folds the two 🔧 rows behind its toggle: "2 tool calls".
 */
const TURN_MESSAGES: ChatMessage[] = [
  msg('user', 'Which two files import the transcript row keys?'),
  msg('tool', '🔧 grep', {
    meta: { tool_call_id: 't1', purpose: 'Search for rowKeys imports', input: '{"pattern":"transcript/rowKeys"}', output: '2 matches' },
  }),
  msg('tool', '🔧 fs_read', {
    meta: { tool_call_id: 't2', purpose: 'Read the TurnBlock import block', input: '{"path":"website/src/pages/chat/TurnBlock.tsx"}', output: 'ok' },
  }),
  msg('assistant', 'Two files: `TurnBlock.tsx` and `ChatMessageList.tsx`. Both take `uniqueRowKeys` from `chat-core/transcript/rowKeys`.'),
]

/**
 * Host surface 2 — a reasoning run with no turn around it: user prompt, then
 * two thinking rows and nothing else. One grouped item never forms a turn, so
 * ChatMessageList renders the group directly as a CollapsibleToolGroup —
 * the same "2 tool calls" pill a member DM shows over a folded group.
 */
const GROUP_MESSAGES: ChatMessage[] = [
  msg('user', 'Why do the two hosts look different?'),
  msg('thinking', 'The turn toggle and the group header are two components with two styles for one concept.'),
  msg('thinking', 'Rendering both through one pill removes the split without changing what either folds.'),
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
          className="bg-bg text-text"
          style={{ width: 760, ['--mc-content-width' as string]: '700px' }}
        >
          <div className="py-4" data-capture-surface="turn">
            <ChatMessageList messages={TURN_MESSAGES} running={false} contentWidth="700px" renderers={renderers} />
          </div>
          <div className="py-4" data-capture-surface="group">
            <ChatMessageList messages={GROUP_MESSAGES} running={false} contentWidth="700px" renderers={renderers} />
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
