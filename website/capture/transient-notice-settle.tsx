/**
 * Evidence for #10141: a pending transient-retry notice settles to the
 * past-tense "Connection interrupted" when the ladder gives up, and the
 * resuming copy names the answer it is finishing.
 *
 * Mounts the REAL ChatMessageList with the REAL main-transcript registry
 * (`createTranscriptRenderers`), so every row is drawn by the same code a
 * reader sees. This file writes no Tailwind classes of its own beyond ones
 * that already live under src/ (capture/ is outside the Tailwind scan glob).
 *
 * Scenes, each a separate list so the runner can probe them one by one:
 *   give-up    [user, retrying, give_up]           — the issue's symptom
 *   abandoned  [user, retrying, user, assistant]   — the person moved on
 *   resuming   [user, assistant partial, resuming] — still pending, live
 *   restored   [user, retrying, assistant]         — unchanged control
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
  preloadedState: { ...base, chat: { ...base.chat, activeSlot: SLOT } },
})

let seq = 0
const msg = (role: string, content: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  role,
  content,
  cls: '',
  ts: `2026-10-29T00:00:${String(seq++).padStart(2, '0')}.000Z`,
  ...over,
})
// Real wire rows: English fallback content plus the `meta.notice` token
// (chat_utils.TRANSIENT_* in the gateway).
const retrying = () =>
  msg('error', '⟳ Connection unstable — retrying…', {
    cls: 'msg msg-err',
    meta: { kind: 'transient_retry', notice: 'transient_retrying' },
  } as Partial<ChatMessage>)
const resuming = () =>
  msg('error', '⟳ Connection unstable — resuming…', {
    cls: 'msg msg-err',
    meta: { kind: 'transient_retry', notice: 'transient_resuming' },
  } as Partial<ChatMessage>)
const giveUp = () =>
  msg('error', '⟳ Connection unstable — please try again.', {
    cls: 'msg msg-err',
    meta: { notice: 'transient_give_up' },
  } as Partial<ChatMessage>)

const SCENES: Array<{ id: string; label: string; messages: ChatMessage[] }> = [
  {
    id: 'give-up',
    label: 'Retry gave up',
    messages: [msg('user', 'Summarize the open PRs.'), retrying(), giveUp()],
  },
  {
    id: 'abandoned',
    label: 'Person moved on before an answer',
    messages: [
      msg('user', 'Summarize the open PRs.'),
      retrying(),
      msg('user', 'Never mind, what time is it in Seattle?'),
      msg('assistant', 'It is 7:11 PM in Seattle.'),
    ],
  },
  {
    id: 'resuming',
    label: 'Still finishing a cut-off answer',
    messages: [
      msg('user', 'Summarize the open PRs.'),
      msg('assistant', 'There are four open PRs. The first, #25220, adds'),
      resuming(),
    ],
  },
  {
    id: 'restored',
    label: 'Retry answered',
    messages: [msg('user', 'Summarize the open PRs.'), retrying(), msg('assistant', 'There are four open PRs.')],
  },
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
        <div data-capture-root className="bg-bg text-text" style={{ width: 760, ['--mc-content-width' as string]: '680px' }}>
          {SCENES.map(s => (
            <section key={s.id} data-scene={s.id} className="py-2">
              <div className="text-xs text-muted px-4 pt-2">{s.label}</div>
              <ChatMessageList messages={s.messages} running={false} contentWidth="680px" renderers={renderers} />
            </section>
          ))}
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
