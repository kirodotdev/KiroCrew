/**
 * Evidence for the restart-required notice on MCP Management -> Servers.
 *
 * THE CHANGE: flipping a server's stub switch used to cycle the whole broker
 * inside the request, which froze the page behind its one page-wide `busy` for
 * as long as the daemon's shutdown budget. The toggle now records the allowlist
 * and returns, so the page has to say the change is waiting for a gateway
 * restart -- and say it WITHOUT looking like a failure, because an operator who
 * is told to restart has nothing to fix.
 *
 * The scene mounts the REAL `McpManagement` page from `src/` against the real
 * stylesheet, theme tokens and live i18n catalog, and drives the real switch:
 * the notice in the frame is the component's own render and its text comes from
 * the catalog through `i18nT`, so a frame also proves the new key resolves.
 * Nothing here re-implements the notice, its classes or its strings.
 *
 * `fetch` is stubbed rather than the api module so the page's real query and
 * mutation paths run: the stub POST answers exactly what the backend now
 * answers (`applied: false` with `restart_required: true`).
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { McpManagement } from '../src/pages/settings/McpManagement'
import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** One server, stubbable and not yet stubbed -- the state an operator is in
 * when they reach for the switch this PR changes. */
const SERVERS = {
  servers: [
    {
      name: 'alpha-mcp',
      stub: false,
      can_stub: true,
      in_allowlist: false,
      entry_poolable: false,
      agents: ['kirocrew'],
      transport: 'stdio',
      denylisted: false,
    },
  ],
}

const STATUS = {
  enabled: false,
  stub: [] as string[],
  stub_count: 0,
  running: false,
  ping_ok: false,
  supported: true,
}

/** Set once the stub POST has been served. The allowlist really is persisted by
 * that call, so the page's follow-up refetch must see the server as stubbed --
 * otherwise the frame shows a switch snapping back to off beside "Saved", which
 * is the opposite of what ships and would read as a failed write. The switch
 * showing the STORED state while the notice explains the running state is
 * exactly the distinction this change introduces. */
let persisted = false

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/mcp-gateway/servers/stub')) {
    persisted = true
    // Verbatim the shipped response: persisted, not live, restart pending.
    return Promise.resolve(json({ ok: true, name: 'alpha-mcp', stub: true, applied: false, restart_required: true }))
  }
  if (url.includes('/api/mcp-gateway/servers')) {
    return Promise.resolve(json({ servers: [{ ...SERVERS.servers[0], stub: persisted, in_allowlist: persisted }] }))
  }
  if (url.includes('/api/mcp-gateway/status')) {
    return Promise.resolve(json({ ...STATUS, stub: persisted ? ['alpha-mcp'] : [], stub_count: persisted ? 1 : 0 }))
  }
  if (url.includes('/api/')) {
    // List endpoints must answer arrays: the page maps over several of them, and
    // an object stub crashes the tree so the harness would shoot an error
    // boundary instead of the surface.
    return Promise.resolve(json(/verdict|servers|agents|skills/.test(url) ? [] : {}))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter>
      <div data-capture-root className="bg-bg text-text w-[1000px] p-6">
        <McpManagement />
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
