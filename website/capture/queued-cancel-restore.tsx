/**
 * Isolated capture entry for the cancel-queued composer restore.
 *
 * WHY ISOLATED: producing a queued card in a live session needs a running
 * turn on a live gateway, neither of which exists in a capture run. This
 * mounts the REAL QueueStack card and the REAL ChatInput against the real
 * stylesheet and theme tokens, with `fetch` stubbed at the network seam. The
 * card's content is the REAL producer output — prepareSendPayload serializes
 * the fixture exactly as send() would — and the `after` phase mirrors
 * handleCancelQueued's shipped restore: stash-first (the pre-send composer
 * state keyed by the queued content) with the SHIPPED restoreQueuedContent
 * parser as the fallback, so the path the screenshot documents is the
 * shipped one.
 *
 * PHASES: `?phase=after` (default) applies the shipped restore — typed text
 * back into the composer, attachments re-staged as chips. `?phase=before`
 * replays the base branch's cancel verbatim (`setInput(msg.content)`, no
 * re-staging), which was a single line, so the replay IS the base behavior
 * rather than a reconstruction of it.
 *
 * WHY THE READOUT PANEL: the staged-chip row proves re-staging visually, but
 * the marker text delta is easier to read with the component's own
 * controlled value printed verbatim below the composer.
 *
 * Language + theme come from the query string: ?lang=en&theme=dark
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ChatInput from '../src/components/ChatInput'
import QueueStack from '../src/components/QueueStack'
import { prepareSendPayload, restoreQueuedContent } from '../src/utils/fileTokens'
import type { ChatMessage } from '../src/api/client'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'
const phase = params.get('phase') || 'after'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    // A bare `{}` is not a safe universal stub: consumers that map over a list
    // throw and take the whole tree down with them (the slash-command menu is one).
    const body = /commands|skills|agents|models|sessions|files|artifacts/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// The REAL serialization a queued card holds: send() ran prepareSendPayload
// over the typed text + staged files, and the queue echoes that LLM-facing
// form back. One @-mentioned file (embedded marker) and one bare upload
// (standalone marker) cover both marker positions.
const TYPED = 'summarize @data.csv and flag anything odd'
const STAGED = ['/tmp/uploads/data.csv', '/tmp/uploads/notes.txt']
const SERIALIZED = prepareSendPayload(TYPED, STAGED).txt

// Mirrors ChatPage's queuedSendStash: send() keys the pre-serialization
// composer state by the exact queued content when the server queues a send,
// and cancel restores from it losslessly. The parser is the reload/other-tab
// fallback, exactly as in handleCancelQueued.
const stash = new Map<string, { raw: string; files: string[] }>([[SERIALIZED, { raw: TYPED, files: STAGED }]])

const QUEUED: ChatMessage = {
  role: 'queued', content: SERIALIZED, cls: 'msg msg-queued',
  ts: '2026-08-24T09:00:00Z', meta: { queueId: 'q-demo' },
}

function Harness() {
  const [draft, setDraft] = useState('')
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  const [queue, setQueue] = useState<ChatMessage[]>([QUEUED])

  const onCancel = (queueId: string) => {
    const msg = queue.find(m => (m.meta?.queueId as string) === queueId)
    if (msg?.content) {
      if (phase === 'before') {
        setDraft(msg.content)
      } else {
        const stashed = stash.get(msg.content)
        const { text, files } = stashed
          ? { text: stashed.raw, files: stashed.files }
          : restoreQueuedContent(msg.content)
        setDraft(text)
        if (files.length) setPendingFiles(prev => [...new Set([...prev, ...files])])
      }
    }
    setQueue(q => q.filter(m => (m.meta?.queueId as string) !== queueId))
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: 24, background: 'var(--bg)', minHeight: '100vh' }}>
      <div style={{ width: 760, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <QueueStack messages={queue} onCancel={onCancel} fuseBelow={false} />
        <ChatInput
          value={draft} onChange={setDraft} onSend={() => {}}
          connected pendingFiles={pendingFiles}
          onRemoveFile={p => setPendingFiles(prev => prev.filter(x => x !== p))}
        />
      </div>
      <div
        data-testid="restore-readout"
        style={{
          width: 760, padding: '12px 14px', borderRadius: 8,
          border: '1px solid var(--border)', background: 'var(--bg-hover)',
          font: '13px/1.6 ui-monospace, monospace', color: 'var(--text)',
        }}
      >
        <div style={{ color: 'var(--muted)', marginBottom: 6 }}>
          composer draft (verbatim) — staged files: <span data-testid="staged-count">{pendingFiles.length}</span>,
          markers in draft: <span data-testid="marker-count">{(draft.match(/\[attached_file \d+\]/g) || []).length}</span>
        </div>
        <div data-testid="draft-verbatim" style={{ whiteSpace: 'pre-wrap', color: 'var(--text-strong)' }}>{draft || '(empty)'}</div>
      </div>
    </div>
  )
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
