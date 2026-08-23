/**
 * Isolated capture entry for the Enter an IME guard declines.
 *
 * WHY ISOLATED: the composer normally lives in a chat session with a live
 * gateway and an open slot, neither of which exists in a capture run. This
 * mounts the REAL ChatInput against the real stylesheet and theme tokens with
 * `fetch` stubbed at the network seam. The behaviour under review is entirely
 * client-side — a keydown handler and the browser's default action for a key
 * nobody consumed — so the path the screenshot documents is the shipped one.
 *
 * WHY THE READOUT PANEL: the whole delta is whether a line break landed in the
 * draft, which a bare screenshot of a textarea renders as a few pixels of
 * height. The panel shows the component's own controlled value with the break
 * made visible, so the difference is readable rather than measured.
 *
 * Language + theme come from the query string: ?lang=zh-CN&theme=dark
 * The capture script drives the composition events and presses Enter for real,
 * because the newline is a DEFAULT ACTION — a synthetic keydown would not
 * produce it and the shot would prove nothing.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ChatInput from '../src/components/ChatInput'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'zh-CN'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    // A bare `{}` is not a safe universal stub: consumers that map over a list
    // throw and take the whole tree down with them (the slash-command menu is one).
    // Nothing here is under review, so answer list endpoints with an empty list.
    const body = /commands|skills|agents|models|sessions|files|artifacts/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [draft, setDraft] = useState('')
  const [sends, setSends] = useState(0)
  const lines = draft.split('\n')
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: 24, background: 'var(--bg)', minHeight: '100vh' }}>
      <div style={{ width: 760 }}>
        <ChatInput value={draft} onChange={setDraft} onSend={() => setSends(n => n + 1)} connected sendOnEnter="enter" />
      </div>
      <div
        data-testid="draft-readout"
        style={{
          width: 760, padding: '12px 14px', borderRadius: 8,
          border: '1px solid var(--border)', background: 'var(--bg-hover)',
          font: '13px/1.6 ui-monospace, monospace', color: 'var(--text)',
        }}
      >
        <div style={{ color: 'var(--muted)', marginBottom: 6 }}>
          composer draft — <span data-testid="line-count">{lines.length}</span> line(s), sends: <span data-testid="send-count">{sends}</span>
        </div>
        <div style={{ whiteSpace: 'pre-wrap', color: 'var(--text-strong)' }}>
          {lines.map((l, i) => (
            <div key={i}>
              {l}
              {i < lines.length - 1 && <span style={{ color: 'var(--danger)' }}>⏎</span>}
            </div>
          ))}
        </div>
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
