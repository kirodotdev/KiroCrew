/**
 * Evidence harness for reasoning bursts across tool calls.
 *
 * WHY ISOLATED: the defect only appears in a turn that reasons, calls a tool,
 * then reasons again — a shape you cannot provoke on demand in a live session,
 * and one whose reasoning is never persisted (the backend broadcasts
 * `chat_thinking` and drops it), so it cannot be replayed from history either.
 *
 * WHAT IS FAITHFUL is the STATE, because the defect lives in a reducer, not in
 * a component: the frame sequence below is dispatched through the real
 * `chatSlice` actions in the same order `useWebSocket` dispatches them, the row
 * list is read back out of the real store, and the row order is the real
 * `groupDisplayItems` output. Only the row-renderer switch is local — ChatPage's
 * own renderMessage needs a fully-seeded page to mount — and it renders the same
 * components ChatPage does, unmocked.
 *
 * Rows are rendered flat rather than through TurnBlock so the reasoning blocks
 * are photographable; TurnBlock's collapse is a separate, orthogonal control.
 *
 *   ?theme=dark|light &refresh=1   (refresh=1 also replays the chat_done refetch)
 */
import { createRoot } from 'react-dom/client'
import { combineReducers, configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import chatReducer, {
  setActiveSlot, sseChatMessage, sseThinkingChunk, refreshSlot,
} from '../src/store/chatSlice'
import instancesReducer from '../src/store/instancesSlice'
import { store as realStore } from '../src/store'

import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import ThinkingBlock from '../src/pages/chat/ThinkingBlock'
import ToolCallLine from '../src/pages/chat/ToolCallLine'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { groupDisplayItems } from '../src/pages/chat/groupDisplayItems'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const withRefresh = params.get('refresh') === '1'

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

const rootReducer = combineReducers({
  dashboard: dashboardReducer,
  notifications: notificationsReducer,
  chat: chatReducer,
  instances: instancesReducer,
})
const base = realStore.getState()
const store = configureStore({ reducer: rootReducer, preloadedState: { ...base } })

const SLOT = 'main'
const TS = '2026-08-17T04:30:00.000Z'
const tool = (name: string, id: string, purpose: string, output: string) => ({
  slot: SLOT, role: 'tool', content: `🔧 ${name}`, ts: TS,
  meta: { tool_call_id: id, purpose, input: `{"pattern":"${name}"}`, output },
})

/** The WS frame order for one think → tool → think → tool → think → answer turn. */
store.dispatch(setActiveSlot(SLOT))
store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'thinking block 的分段对吗？', ts: TS }))
store.dispatch(sseThinkingChunk({ slot: SLOT, content: 'BURST 1 — ' }))
store.dispatch(sseThinkingChunk({ slot: SLOT, content: 'reasoning that leads to the FIRST tool call.' }))
store.dispatch(sseChatMessage(tool('grep', 't1', 'find the reducer', '4 matches in chatSlice.ts')))
store.dispatch(sseThinkingChunk({ slot: SLOT, content: 'BURST 2 — ' }))
store.dispatch(sseThinkingChunk({ slot: SLOT, content: 'reasoning about what grep returned.' }))
store.dispatch(sseChatMessage(tool('fs_read', 't2', 'read the scan-back loop', 'sseThinkingChunk(state, action) { …')))
store.dispatch(sseThinkingChunk({ slot: SLOT, content: 'BURST 3 — reasoning before the answer.' }))
store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Root cause is the scan-back loop.', seq: 0 }))
store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'Root cause is the scan-back loop.', ts: TS }))

/** chat_done refetches the slot; reasoning is client-only, so this is where a
 *  preserved block either stays put or drifts to the bottom of the transcript. */
if (withRefresh) {
  store.dispatch(refreshSlot.fulfilled(
    {
      key: SLOT,
      messages: [
        { role: 'user', content: 'thinking block 的分段对吗？', cls: '', ts: TS },
        { role: 'tool', content: '🔧 grep', cls: '', ts: TS, meta: { tool_call_id: 't1' } },
        { role: 'tool', content: '🔧 fs_read', cls: '', ts: TS, meta: { tool_call_id: 't2' } },
        { role: 'assistant', content: 'Root cause is the scan-back loop.', cls: 'msg msg-a', ts: TS },
      ],
      running: false, stopping: false, hasMore: false, total: 4,
    } as unknown as Parameters<typeof refreshSlot.fulfilled>[0],
    'cap', SLOT,
  ))
}

const messages: ChatMessage[] = store.getState().chat.messages
const { turns } = groupDisplayItems(messages)
/** Flatten the real grouping back to rows: the turn container is what collapse
 *  acts on, and collapse is not what this frame is about. */
const rows: ChatMessage[] = turns.flatMap(t =>
  t.kind === 'turn'
    ? t.items.flatMap(it => (it.kind === 'single' ? [it.msg] : it.msgs))
    : t.kind === 'single' ? [t.msg] : [])

const trace = rows.map(m => (m.role === 'thinking' ? 'thinking' : m.role)).join(' → ')
const bursts = rows.filter(m => m.role === 'thinking').length

function renderRow(m: ChatMessage, i: number) {
  const key = `r${i}`
  if (m.role === 'thinking') return <ThinkingBlock key={key} content={m.content} disclosureKey={key} />
  if (m.role === 'tool') return <ToolCallLine key={key} message={m} running={false} disclosureKey={key} />
  if (m.role === 'user') {
    return (
      <UserMessage
        key={key}
        content={m.content}
        renderContent={(c) => <MarkdownRenderer content={c} softBreaks />}
        timestamp="04:30"
        timestampTitle="2026-08-17 04:30:00 UTC"
        canEdit={false}
        messageIndex={i}
        messageTs={m.ts}
        slotKey={SLOT}
        slotTitle="Thinking bursts"
        onEditResend={() => {}}
      />
    )
  }
  return (
    <AssistantMessage
      key={key}
      content={m.content}
      isStreaming={m.role === 'streaming'}
      timestamp="04:31"
      timestampTitle="2026-08-17 04:31:00 UTC"
      messageTs={m.ts}
      slotKey={SLOT}
      slotTitle="Thinking bursts"
      showFooter={false}
      linkPreviews={false}
    />
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div
          data-capture-root
          data-trace={trace}
          data-bursts={String(bursts)}
          className="bg-bg text-text"
          style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
        >
          <div className="px-5 mx-auto w-full pt-3 pb-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
            <div className="text-[10px] uppercase tracking-wider text-accent/70 font-mono">
              {bursts} reasoning block(s){withRefresh ? ' · after chat_done refresh' : ''}
            </div>
            <div className="text-[10px] text-muted font-mono pt-0.5">{trace}</div>
          </div>
          <div className="py-2">
            {rows.map((m, i) => (
              <div
                key={i}
                data-row={m.role}
                className="px-5 mx-auto w-full py-1"
                style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
              >
                {renderRow(m, i)}
              </div>
            ))}
          </div>
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
