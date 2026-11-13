/**
 * Evidence: a pending transient-retry notice settles to the
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
 *   other      [user, retrying, ❌ other error]     — failed another way
 *   resuming   [user, assistant partial, resuming] — still pending, live
 *   restored   [user, retrying, assistant]         — unchanged control
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
    id: 'other',
    label: 'Retry failed with another error',
    messages: [msg('user', 'Summarize the open PRs.'), retrying(), msg('error', '❌ Internal error', { cls: 'msg msg-err' })],
  },
  {
    id: 'resuming',
    label: 'Still finishing a cut-off answer',
    messages: [
      msg('user', 'Summarize the open PRs.'),
      msg('assistant', 'There are four open PRs. The first one adds'),
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
