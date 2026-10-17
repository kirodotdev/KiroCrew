/**
 * Isolated capture entry for the Dashboard structure locked switch on the
 * crewmate's Profile card (PR #18687).
 *
 * Mounts the REAL CrewProfileSettings and stubs every API call the component
 * makes: the agent record (approval mode, model), the resolved-model read, the
 * available-models list, and the dashboard-body read that tells the switch
 * whether the page is locked.
 *
 * ?theme=dark|light   palette (default dark)
 * ?locked=1|0         whether the gateway says the page is locked (default 0)
 * ?flipError=1        if set, the lock-flip mutation returns 500 (shows the error notice)
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import { MEMBERS_ROSTER_QUERY_KEY } from '../src/api/membersQuery'
import type { MemberRosterRow } from '../src/api/client'
import CrewProfileSettings from '../src/pages/members/CrewProfileSettings'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const locked = params.get('locked') === '1'

localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MEMBER: MemberRosterRow = {
  name: 'oncall', slug: 'oncall', slot_key: 'member-oncall', running: false,
  model: '', approval_mode: 'trust', kiro_agent: 'kirocrew', memory_store: 'default',
  workspace: 'default',
} as MemberRosterRow

const MODELS = [{ name: 'claude-opus-5' }, { name: 'claude-haiku-4' }]

const DASHBOARD_BODY = {
  instance_version: 2,
  template: { id: 'fixture-board', version: 1 },
  html: '<!doctype html><p>Board</p>',
  manifest: { id: 'fixture-board', version: 1, title: 'Board', fields: [] },
  state: 'live',
  structure_locked: locked,
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/agents/oncall/resolved-model')) {
    return Promise.resolve(new Response(JSON.stringify({ model: 'claude-opus-5' }), { status: 200, headers: { 'content-type': 'application/json' } }))
  }
  if (url.includes('/api/models')) {
    return Promise.resolve(new Response(JSON.stringify(MODELS), { status: 200, headers: { 'content-type': 'application/json' } }))
  }
  if (url.includes('/dashboard')) {
    // Lock-flip: if ?flipError=1, fail the POST so the component shows the error notice.
    if (params.get('flipError') === '1' && init?.method?.toUpperCase() === 'POST') {
      return Promise.resolve(new Response(JSON.stringify({ error: 'conflict' }), { status: 500, headers: { 'content-type': 'application/json' } }))
    }
    return Promise.resolve(new Response(JSON.stringify(DASHBOARD_BODY), { status: 200, headers: { 'content-type': 'application/json' } }))
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
