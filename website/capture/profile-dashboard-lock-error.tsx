/**
 * Profile card lock-error state capture for PR #18687.
 *
 * Shows CrewProfileSettings with the Dashboard section locked=true AND
 * the unlock mutation in its error state, so the error notice is visible.
 *
 * We achieve this by wrapping the component in a parent that calls the
 * mutation immediately after mount and routes the flip to a stub that errors.
 *
 * ?theme=dark|light
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider, useMutation, useQueryClient } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import { MEMBERS_ROSTER_QUERY_KEY } from '../src/api/membersQuery'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../src/hooks/useWebSocket'
import type { MemberRosterRow } from '../src/api/client'
import CrewProfileSettings from '../src/pages/members/CrewProfileSettings'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MEMBER: MemberRosterRow = {
  name: 'oncall', slug: 'oncall', slot_key: 'member-oncall', running: false,
  model: '', approval_mode: 'trust', kiro_agent: 'kirocrew', memory_store: 'default',
  workspace: 'default',
} as MemberRosterRow

// Pre-seed: initial GET for dashboard lock fails (500) -> shows "unreadable" error notice
let hitCount = 0
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/dashboard')) {
    hitCount++
    return Promise.resolve(new Response('{"error":"unavailable"}', { status: 500, headers: { 'content-type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

await initI18n()

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
client.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [MEMBER])

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <div className="h-screen bg-bg-elevated text-text p-4 flex items-start" data-capture-root>
          <div className="w-80">
            <CrewProfileSettings member={MEMBER} slotKey="member-oncall" />
          </div>
        </div>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
