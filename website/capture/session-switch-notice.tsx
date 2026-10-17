/**
 * Isolated capture entry for the session-switch boundary row (#9086).
 *
 * Renders the REAL ChatPane against the real stylesheet, theme tokens and i18n
 * catalog, with a short conversation seeded in the store exactly as the slot
 * detail load would. In the `after` scene the transcript carries the row a
 * mid-conversation model switch appends (`meta.kind: session_switch`) between
 * two turns; the `before` scene is the same conversation without the row, and
 * the `variants` scene carries one row per switch (agent, model, reasoning
 * effort, workspace) so every wording is seen in place.
 *
 * Query: ?theme=dark|light  ?state=before|after|variants
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPane from '../src/components/ChatPane'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { hydrateSlotMessages } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('state') || 'after'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

const SLOT = 'chat-1-switch-notice'
const NOTICE = 'Model switched to claude-opus-4.8. Replies from here on use the new model.'
const VARIANTS = [
  '🔄 Switched to agent: kirocrew-worker',
  NOTICE,
  'Reasoning effort switched to high. Replies from here on use the new reasoning effort.',
  'Workspace switched to release-review. Replies from here on use the new workspace.',
]
const notices = scene === 'after' ? [NOTICE] : scene === 'variants' ? VARIANTS : []

const rows = [
  { role: 'user', content: 'Summarize the open review threads on the release branch.', cls: 'msg msg-u', ts: '2026-10-17T10:00:00Z' },
  { role: 'assistant', content: 'Three threads are open: two on the migration script and one on the changelog wording.', cls: 'msg msg-a', ts: '2026-10-17T10:00:08Z' },
  ...notices.map((content, i) => ({
    role: 'assistant',
    content,
    cls: 'msg msg-a',
    ts: `2026-10-17T10:01:0${i}Z`,
    meta: { kind: 'session_switch' },
  })),
  { role: 'user', content: 'Draft a reply to the changelog thread.', cls: 'msg msg-u', ts: '2026-10-17T10:01:30Z' },
  { role: 'assistant', content: 'Here is a draft: “Agreed — the entry now names the flag and the default it replaces.”', cls: 'msg msg-a', ts: '2026-10-17T10:01:40Z' },
]

async function mount() {
  store.dispatch(sseSlots([{ key: SLOT, title: 'release review', messages: rows.length, running: false }] as never))
  store.dispatch(
    hydrateSlotMessages({ slot: SLOT, messages: rows, hasMore: false, total: rows.length, running: false } as never),
  )
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="h-screen bg-bg text-text p-4" data-capture-page>
            <div className="h-full max-w-[760px] mx-auto" data-capture-root>
              <ChatPane slotKey={SLOT} frameless />
            </div>
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

void mount()
