/**
 * Capture entry: the Bot editor's Abilities section when its capabilities read
 * fails. Mounts the REAL editor (`useCrewEditor` + `CrewEditorModal`) inside the
 * real leave-guard provider, with every API answered locally:
 *
 * - ?scenario=timeout  the capabilities GET answers 503 `reason: timed_out`;
 *                      a Try again is held open forever (the in-progress state).
 * - ?scenario=kit      the capabilities GET answers a Bot that owns its file, and
 *                      the worker-kit GET answers 503 `reason: timed_out`.
 * ?theme=dark|light
 */
import { useState, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseConnected } from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'

import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import CrewEditorModal from '../src/components/crew/CrewEditorModal'
import { useCrewEditor } from '../src/components/crew/useCrewEditor'
import {
  NavigationLeaveGuardProvider,
  useGuardedLeave,
  useRegisterNavigationLeaveGuard,
} from '../src/components/NavigationLeaveGuard'
import type { KiroCrewAgent } from '../src/api/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scenario = params.get('scenario') === 'kit' ? 'kit' : 'timeout'
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const BOT = { name: 'pixel', kiro_agent: 'kirocrew-mate-worker', workspace: 'default', memory_store: 'default' }
const TIMED_OUT = { error: 'capabilities_unavailable', code: 'capabilities_unavailable', reason: 'timed_out' }
const MATE_VIEW = {
  schema_version: 1, member: 'pixel', mode: 'mate', revision: 'rev-1',
  template: { name: 'kirocrew-mate-worker', source: 'builtin', scope: 'global', available: true },
  rows: [], connections: [], skills: [], parent_changes: [],
  runtime: { status: 'applied', saved_revision: 'rev-1', sessions: [] },
  worker: { rows: [
    { section: 'tools', id: 'fs_read', label: 'fs_read', state: 'inherited', present: true, value: true },
    { section: 'tools', id: 'execute_bash', label: 'execute_bash', state: 'inherited', present: true, value: true },
  ] },
}

let capabilityReads = 0
const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }))
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.href).pathname
  if (path.endsWith('/capabilities')) {
    capabilityReads += 1
    if (scenario === 'kit') return json(200, MATE_VIEW)
    // The first read times out; a Try again is held, as a still-slow read is.
    if (capabilityReads === 1) return json(503, TIMED_OUT)
    return new Promise<Response>(() => {})
  }
  if (path.endsWith('/worker-kit')) return json(503, { ...TIMED_OUT, detail: '' })
  if (path.startsWith('/api/')) {
    console.log('[capture] stub', path)
    return json(200, {})
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

// The editor's own reads, answered with one neutral demo Bot.
Object.assign(api, {
  kirocrewAgents: async () => ({ agents: [BOT], default_agent: 'pixel' }),
  agentsInstalled: async () => [{ name: 'kirocrew-mate-worker' }],
  workspaces: async () => ({ workspaces: [{ name: 'default' }] }),
  kirocrewConfig: async () => ({ memory_stores: { default: {} } }),
  agentResolvedModel: async () => ({ model: '', pinned: false, kiro_agent: 'kirocrew-mate-worker' }),
  models: async () => [],
  crons: async () => ({ jobs: [] }),
  webhooks: async () => ({ tokens: [], switch_on: true }),
  memoryPreferences: async () => ({ content: '' }),
  memoryProjects: async () => ({ content: '' }),
})

await initI18n()

function Host(): ReactNode {
  const [editing, setEditing] = useState('pixel')
  const agentsQuery = useQuery({ queryKey: ['kirocrew-agents'], queryFn: () => api.kirocrewAgents() })
  const leave = useGuardedLeave()
  const ctl = useCrewEditor({
    editingName: editing,
    agents: (agentsQuery.data?.agents || []) as KiroCrewAgent[],
    agentsLoaded: agentsQuery.data !== undefined,
    defaultAgent: agentsQuery.data?.default_agent || '',
    refetchAgents: () => {},
    onClose: () => setEditing(''),
    leave,
  })
  // The same guard the Bots page registers: an editor with unsaved panes
  // refuses the leave and raises its own discard confirm.
  useRegisterNavigationLeaveGuard(() => {
    if (!editing || ctl.dirtyPanes.size === 0) return true
    ctl.requestClose()
    return false
  })
  return <CrewEditorModal ctl={ctl} />
}

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
})
store.dispatch(sseConnected())

createRoot(document.getElementById('root')!).render(
  <Provider store={store}><MemoryRouter><QueryClientProvider client={client}>
    <ThemeProvider>
      <NavigationLeaveGuardProvider>
        <div className="h-screen bg-bg text-text"><Host /></div>
      </NavigationLeaveGuardProvider>
    </ThemeProvider>
  </QueryClientProvider></MemoryRouter></Provider>,
)
