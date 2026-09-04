/**
 * Isolated capture entry for Dev Fleet provision reattach-after-reload.
 *
 * WHY ISOLATED: reaching /dev-fleet through the full SPA needs a live gateway
 * with real worktrees and an in-flight provision — none of which exist in a
 * capture run; a half-stubbed shell renders its prerequisite gate, which is
 * worse evidence than none. This mounts the REAL DevFleetPage against the real
 * stylesheet and theme tokens, with `fetch` stubbed at the network seam to
 * serve the same `/fleet` and `/run` payload shapes the backend sends. The
 * reattach path under review — the fleet-driven useEffect, the poll loop, the
 * seeded log accumulator — therefore executes exactly as it does after a real
 * page reload; the stub replaces the backend, not the component.
 *
 * Scene + theme come from the query string: ?scene=running&theme=dark
 * Scenes mirror the two states the reattach restores:
 *   running — reload mid-provision: stepper + live log resume from the payload
 *   failed  — reload after a failed provision: red strip + log auto-expanded
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import DevFleetPage from '../src/pages/DevFleetPage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'running'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NOW = Date.now() / 1000

const RUNNING_LINES = [
  '[provision] creating venv \u2026',
  'Collecting build dependencies',
  'Installing collected packages: kiro-crew',
  '[provision] building dist \u2026',
  'vite v5.4.20 building for production...',
  'transforming (412) src/pages/DevFleetPage.tsx',
]

const FAILED_LINES = [
  '[provision] building dist \u2026',
  'vite v5.4.20 building for production...',
  "error during build:",
  "RollupError: Could not resolve './missing-module' from src/pages/DevFleetPage.tsx",
  'npm ERR! code 1',
  'npm ERR! command failed: npm run build',
]

const RID = scene === 'failed' ? 'run-prov-failed01' : 'run-prov-live0001'

const FLEET = {
  base_branch: 'main',
  worktrees: [
    {
      name: 'main', is_main: true, running: false, has_dist: true, behind: 0,
      branch: 'main', last_updated_at: NOW - 1800,
    },
    {
      name: 'kc-wt-oauth-device-flow', is_main: false, running: false,
      has_dist: false, has_venv: true, behind: 2, branch: 'feat/oauth-device-flow',
      last_updated_at: NOW - 3600, provision_run_id: RID,
      pr: { number: 3180, state: 'OPEN', url: 'https://github.com/kirodotdev/KiroCrew/pull/3180', isDraft: false },
    },
    {
      name: 'kc-wt-slack-scope-probe', is_main: false, running: true, port: 7791,
      health: 200, has_dist: true, has_venv: true, behind: 0,
      branch: 'fix/slack-scope-probe', last_updated_at: NOW - 7200,
    },
  ],
  pods_available: true,
}

const RUN = scene === 'failed'
  ? { status: 'done', exit_code: 1, output: FAILED_LINES, started: NOW - 214 }
  : { status: 'running', output: RUNNING_LINES, started: NOW - 87 }

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/fleet')) {
    return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/disk')) {
    return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/run?id=' + RID)) {
    return Promise.resolve(new Response(JSON.stringify(RUN), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dev-fleet']}>
        <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
          <DevFleetPage />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
