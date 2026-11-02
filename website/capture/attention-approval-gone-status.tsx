/**
 * Isolated capture entry for the command-center attention card
 * (`AttentionCard`) showing a GONE approval as STATUS, not an error (#18203).
 *
 * WHY ISOLATED: the card only exists in the command center with a live pending
 * approval whose decision then fails as gone. This mounts the REAL card with a
 * QueryClient + store, feeds it a pending approval, and answers its Approve
 * press with the gateway's terminal "no pending approval" (404) via a stubbed
 * resolve endpoint — so `expired` flips true and the card renders its inline
 * `StatusNotice` in exactly the state #18203 is about.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import AttentionCard from '../src/pages/chat/command-center/AttentionCard'
import type { AttentionItem } from '../src/pages/chat/command-center/model'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Every approval resolve answers 404 "no pending approval" — the terminal
// refusal that makes the card read `expired`.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/approvals/') || url.includes('/approve')) {
    return Promise.resolve(new Response('{"error":"no pending approval"}', {
      status: 404, headers: { 'Content-Type': 'application/json' },
    }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

const item: AttentionItem = {
  id: 'att-18203',
  slot: 'child',
  kind: 'approval',
  approval: {
    id: 'ap-18203',
    slot: 'child',
    tool: 'shell',
    tool_input: 'rm -rf build && npm run build',
    tool_purpose: 'Clear the stale build tree and rebuild',
  } as AttentionItem['approval'],
  approvalMode: 'normal',
}

function Scene() {
  useEffect(() => {
    const t = setTimeout(() => {
      document.querySelectorAll<HTMLButtonElement>('[data-capture-root] button').forEach((b) => {
        if (/^\s*Approve( once)?\s*$/.test(b.textContent || '')) b.click()
      })
    }, 50)
    return () => clearTimeout(t)
  }, [])
  return (
    <div data-capture-root style={{ maxWidth: 420, margin: '24px auto' }}>
      <AttentionCard item={item} title="Worker" />
    </div>
  )
}

await initI18n()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
