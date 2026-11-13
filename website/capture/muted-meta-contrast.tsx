/**
 * Capture entry for #26268 / #26283 / #26598: the REAL AssistantMessage footer
 * (credits line + timestamp) in any built-in palette, on the page surface
 * (`bg-bg`, the main chat row), a card (`bg-card`), a raised panel
 * (`bg-bg-elevated`) and a hovered row (`bg-bg-hover`).
 *
 * Query string: ?theme=<palette name> (default `dark`)
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../src/store'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import '../src/index.css'

const asked = new URLSearchParams(location.search).get('theme') ?? ''
const theme = /^[a-z0-9-]+$/.test(asked) ? asked : 'dark'
const mode = theme === 'light' || theme.endsWith('-light') ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)
document.documentElement.setAttribute('data-mode', mode)

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  return realFetch(input, init)
}) as typeof globalThis.fetch
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const Reply = () => (
  <AssistantMessage content="The muted token now clears 4.5:1 on the card." isStreaming={false} slotRunning={false}
    messageTs="2026-10-30T17:06:00Z" timestamp="17:06" timestampTitle="Today 17:06"
    turnStats={{ elapsed_ms: 2_700, credits: 0.13, model: 'claude-opus-4.8' }} />
)

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div className="bg-bg text-text p-4 grid grid-cols-2 gap-4" style={{ width: 860 }} data-capture-root>
          <div className="min-w-0 p-3" data-testid="on-bg"><Reply /></div>
          <div className="min-w-0 p-3 bg-card rounded-xl" data-testid="on-card"><Reply /></div>
          <div className="min-w-0 p-3 bg-bg-elevated rounded-xl" data-testid="on-bg-elevated"><Reply /></div>
          <div className="min-w-0 p-3 bg-bg-hover rounded-xl" data-testid="on-bg-hover"><Reply /></div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
