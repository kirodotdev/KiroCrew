/**
 * Evidence: the KAS signed-out row draws in the reader's language, with its
 * sign-in button kept.
 *
 * Mounts the REAL ChatMessageList with the REAL main-transcript registry
 * (`createTranscriptRenderers`), so the row is drawn by the code a reader sees.
 *
 * Scenes:
 *   token    the row as the gateway writes it now (`meta.notice`)
 *   history  the same sentence written before the token existed
 *
 *   ?theme=dark|light&lang=en|ja
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const lang = params.get('lang') || 'en'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
const SLOT = 'main'

const WIRE =
  "KAS runs through kiro-cli's relay. Not signed in to Kiro. Sign in from Settings → Agent Harness → Kiro sign-in, then start a new chat."
let seq = 0
const msg = (role: string, content: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  role,
  content,
  cls: '',
  ts: `2026-11-05T00:00:${String(seq++).padStart(2, '0')}.000Z`,
  ...over,
})

const SCENES: Array<{ id: string; messages: ChatMessage[] }> = [
  {
    id: 'token',
    messages: [
      msg('user', 'こんにちは'),
      msg('error', WIRE, {
        cls: 'msg msg-err',
        meta: { kind: 'auth_required', notice: 'kas_signed_out', notice_args: { route: 'relay' } },
      } as Partial<ChatMessage>),
    ],
  },
  {
    id: 'history',
    messages: [
      msg('user', 'こんにちは'),
      msg('error', WIRE, { cls: 'msg msg-err', meta: { kind: 'auth_required' } } as Partial<ChatMessage>),
    ],
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
  onOpenSignIn: () => {},
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n(lang)

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div data-capture-root className="bg-bg text-text" style={{ width: 760, ['--mc-content-width' as string]: '680px' }}>
          {SCENES.map(s => (
            <section key={s.id} data-scene={s.id} className="py-2">
              <ChatMessageList messages={s.messages} running={false} contentWidth="680px" renderers={renderers} />
            </section>
          ))}
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
