/**
 * Isolated capture entry for Design Tweak's refused dispatch (chat-core P2).
 *
 * WHY ISOLATED: a refused dispatch needs the Design Tweak backend to seal a
 * request (`POST /apps/design-tweak/api/send`), the chat host to adopt the
 * app's slot (`POST /api/chat/slots`) and then to REFUSE the turn (`POST
 * /api/chat?ws=1` -> 409 "slot agent mismatch") -- none exists in a capture
 * run. This mounts the REAL page over the REAL `api.ts` -> `sendChatMessage`
 * -> chat-core `sendTurn` path, with fetch stubbed to serve one project, one
 * draft request with one comment, and to refuse the send with the same body
 * shape the backend answers with. Nothing re-implements the notice or its
 * strings.
 *
 * What it documents: before this branch a `{ok:false}` refusal inside a 200 --
 * or any non-JSON 2xx -- was marked DELIVERED and the sealed edits were lost.
 * Now a refusal is a `SEND_REFUSED` rejection, rendered through the page's
 * existing `bridgeError` ErrorNotice as `Send failed: <server reason>`, and
 * the request stays actionable in the rail for a resend.
 *
 * Query: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import DesignTweak from '../src/apps/design-tweak/DesignTweakPage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const project = { id: 'acme-site', path: '/home/dev/acme-site', name: 'Acme Site' }
const request = {
  id: 'req-7',
  number: 7,
  status: 'draft',
  projectId: project.id,
  projectRoot: project.path,
  createdAt: '2026-09-15T18:02:00Z',
  comments: [
    {
      cid: 'c1', index: 1, status: 'draft',
      comment: 'Make the hero heading one size smaller on phones; it wraps to three lines.',
      element: 'h1.hero-title', projectId: project.id, projectRoot: project.path,
      createdAt: '2026-09-15T18:02:00Z',
    },
  ],
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const u = new URL(url, location.origin)
  const p = u.pathname
  // The app's own backend.
  if (p === '/apps/design-tweak/api/projects') return json({ projects: [project], activeId: project.id, serving: true })
  if (p === '/apps/design-tweak/api/queue') return json({ pending: [request] })
  if (p === '/apps/design-tweak/api/history') return json({ history: [] })
  if (p === '/apps/design-tweak/api/health') return json({ status: 'ok', app: 'design-tweak', dataDir: '/home/dev/.kirocrew/apps/design-tweak' })
  // The seal: the backend returns the sealed snapshot the prompt is built from.
  if (p === '/apps/design-tweak/api/send') return json({ ok: true, request: { ...request, status: 'sent', sentAt: '2026-09-15T18:03:00Z' } })
  // The chat host adopts the app's slot and binds it to the project ...
  if (p === '/api/chat/slots' && init?.method === 'POST') return json({ key: 'design-tweak-acme-site', messages: 0 })
  if (p.startsWith('/api/chat/slots/')) return json({})
  // ... and refuses the turn.
  if (p === '/api/chat' && init?.method === 'POST') return json({ error: 'slot agent mismatch', code: 'slot_agent' }, 409)
  if (p.startsWith('/apps/design-tweak/api/') || p.startsWith('/api/')) return json({})
  return realFetch(input, init)
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width: '100vw', height: '100vh', background: 'var(--bg)' }}>
          <DesignTweak />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
