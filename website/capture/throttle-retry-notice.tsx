/**
 * Evidence: a retry the gateway runs because the model provider throttled the
 * request is worded as throttling, pending and settled, never as a connection
 * problem; a plain upstream 5xx retry keeps its connection wording.
 *
 * Mounts the REAL ChatMessageList with the REAL main-transcript registry
 * (`createTranscriptRenderers`), so every row is drawn by the same code a
 * reader sees.
 *
 * Scenes, each a separate list so the runner can probe them one by one:
 *   pending    [user, throttle retrying]                       — still retrying
 *   answered   [user, throttle retrying x3, assistant]         — the issue's symptom
 *   resuming   [user, assistant partial, throttle resuming]  — post-token throttle
 *   gave-up    [user, throttle retrying, throttle give_up]     — Stop in backoff
 *   plain-5xx  [user, retrying, assistant]                     — unchanged control
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

const theme = new URLSearchParams(location.search).get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
const SLOT = 'main'

let seq = 0
const msg = (role: string, content: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  role,
  content,
  cls: '',
  ts: `2026-11-05T00:00:${String(seq++).padStart(2, '0')}.000Z`,
  ...over,
})
// Real wire rows: English fallback content plus the `meta.notice` token
// (chat_utils.TRANSIENT_* in the gateway).
const throttled = () =>
  msg('error', '⟳ The model provider is throttling requests — retrying…', {
    cls: 'msg msg-err',
    meta: { kind: 'transient_retry', notice: 'transient_throttle_retrying' },
  } as Partial<ChatMessage>)
const retrying = () =>
  msg('error', '⟳ Connection unstable — retrying…', {
    cls: 'msg msg-err',
    meta: { kind: 'transient_retry', notice: 'transient_retrying' },
  } as Partial<ChatMessage>)
const throttleResuming = () =>
  msg('error', '⟳ The model provider is throttling requests — resuming…', {
    cls: 'msg msg-err',
    meta: { kind: 'transient_retry', notice: 'transient_throttle_resuming' },
  } as Partial<ChatMessage>)
const throttleGiveUp = () =>
  msg('error', '⟳ The model provider is throttling requests — please try again.', {
    cls: 'msg msg-err',
    meta: { notice: 'transient_throttle_give_up' },
  } as Partial<ChatMessage>)

const ASK = 'Summarize the open PRs.'
const SCENES: Array<{ id: string; label: string; messages: ChatMessage[] }> = [
  { id: 'pending', label: 'Throttled, still retrying', messages: [msg('user', ASK), throttled()] },
  {
    id: 'answered',
    label: 'Throttled three times, then answered',
    messages: [msg('user', ASK), throttled(), throttled(), throttled(), msg('assistant', 'There are four open PRs.')],
  },
  {
    id: 'resuming',
    label: 'Throttled mid-answer, finishing it',
    messages: [msg('user', ASK), msg('assistant', 'There are four open PRs. The first one adds'), throttleResuming()],
  },
  { id: 'gave-up', label: 'Throttled, retry gave up', messages: [msg('user', ASK), throttled(), throttleGiveUp()] },
  {
    id: 'plain-5xx',
    label: 'Upstream 5xx, then answered (unchanged)',
    messages: [msg('user', ASK), retrying(), msg('assistant', 'There are four open PRs.')],
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
