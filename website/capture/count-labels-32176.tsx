/**
 * Isolated capture entry for #32176: the Dev Fleet row count and the local
 * storage debug key counts, which used to glue a raw number to a fixed plural
 * label ("1 rows", "1 keys").
 *
 * Mounts the REAL DevFleetPage (with `fetch` stubbed at the network seam to
 * serve a `/fleet` payload) or the REAL LocalStorageDebug page (against a
 * localStorage seeded here), so the count strings render exactly as they do in
 * production.
 *
 * Query string: ?scene=<scene>&lang=en|ru&theme=dark|light
 *   fleet-one      — a fleet of one worktree (main): "1 row"
 *   fleet-many     — a fleet of four worktrees, filterable: "4 rows" / "2 / 4 rows"
 *   storage-one    — one localStorage key: "1 key"
 *   storage-many   — 205 keys, over the 200-row cap: "205 keys" + the overflow notice
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import DevFleetPage from '../src/apps/dev-fleet/DevFleetPage'
import LocalStorageDebug from '../src/pages/LocalStorageDebug'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'fleet-one'
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NOW = Math.floor(Date.now() / 1000)
const MAIN = { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', last_updated_at: NOW - 1800 }
const wt = (name: string, branch: string) => ({
  name, is_main: false, running: false, has_dist: true, behind: 1, branch, last_updated_at: NOW - 3600,
})
const FLEET = {
  base_branch: 'main',
  worktrees: scene === 'fleet-one'
    ? [MAIN]
    : [MAIN, wt('wt-docs-typo', 'fix/docs-typo'), wt('wt-docs-links', 'fix/docs-links'), wt('wt-pod-logs', 'feat/pod-logs')],
  pods_available: true,
}

if (scene.startsWith('storage')) {
  localStorage.clear()
  const n = scene === 'storage-one' ? 1 : 205
  for (let i = 0; i < n; i++) localStorage.setItem(`demo-key-${String(i).padStart(3, '0')}`, 'x'.repeat(24))
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/fleet')) {
    return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n(lang)
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[scene.startsWith('storage') ? '/' : '/dev-fleet']}>
        <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', overflow: 'auto', padding: scene.startsWith('storage') ? 16 : 0 }}>
          {scene.startsWith('storage') ? <LocalStorageDebug /> : <DevFleetPage />}
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
