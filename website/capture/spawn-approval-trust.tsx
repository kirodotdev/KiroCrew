/**
 * Isolated capture entry for the spawn-approval card's action row (#5400).
 *
 * WHY ISOLATED: a spawn approval only exists inside a live chat session with a
 * pending subagent tool call — neither exists in a capture run. This mounts
 * the REAL ActivityViewer (whose ApprovalEntry renders the card under review)
 * against the real stylesheet, theme tokens and live i18n catalog, feeding it
 * a pending spawn approval through its own `toolLog` prop — the same seam the
 * chat page uses — so the card photographed is the shipped one.
 *
 * The defect this documents: spawn approvals resolve through the one-shot
 * `resolveApproval` endpoint, which has no trust verb, yet the card offered
 * trust tiers and reported "Trusted". The after-frame shows the honest set —
 * Approve / Reject only.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ActivityViewer from '../src/pages/chat/ActivityViewer'
import type { ToolActivity } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Stub the network seam: the capture server has no gateway, and every request
// the viewer makes on mount is incidental to the card under review.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// A pending spawn approval, exactly as the SSE stream delivers one: a shell
// tool call awaiting a decision (approval_type !== 'chat').
const toolLog: ToolActivity[] = [
  {
    type: 'approval',
    text: 'Running: git push origin feature',
    ts: Date.now(),
    approval_id: 'ap-capture',
    approval_type: 'spawn',
  },
]

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        {/* The activity viewer's real habitat is the chat page's right dock. */}
        <div data-capture-root style={{ width: 420, height: '100vh', marginLeft: 'auto', display: 'flex', flexDirection: 'column', borderLeft: '1px solid var(--border)', background: 'var(--bg)' }}>
          <ActivityViewer
            subagents={{}}
            toolLog={toolLog}
            open
            onToggle={() => {}}
            slot="capture-slot"
            view="subagents"
          />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
