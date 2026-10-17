/**
 * Evidence for the MCP Servers table's in-place sign-in on user-added servers.
 *
 * Mounts the REAL McpTab out of `src/` against a stubbed `fetch`, so the before
 * and after frames differ only by which version of the table's source is on disk
 * (Tailwind scans `src/`, never `capture/`). The stub serves three remote rows a
 * user added outside the Connections registry:
 *
 *   - `docs-internal`: OAuth challenge, nobody signed in
 *   - `wiki-internal`: OAuth challenge, a grant is held
 *   - `tickets-internal`: OAuth challenge, the server answered 401
 *
 * Every other API call answers an empty object. A click on Sign in answers with
 * a waiting mint whose approval URL is a placeholder.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import McpTab from '../src/pages/overview/McpTab'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const row = (name: string, extra: Record<string, unknown>) => ({
  name,
  command: '',
  url: `https://${name}.example.com/mcp`,
  status: 'needs_auth',
  source: 'mcp.json',
  enabled: true,
  tools: [],
  authChallenge: true,
  ownerSignIn: true,
  ...extra,
})

const SERVERS = [
  row('docs-internal', { authGrantPresent: false }),
  row('wiki-internal', { authGrantPresent: true }),
  row('tickets-internal', { status: 'error', error: 'HTTP 401' }),
]

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

window.fetch = async (input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.origin).pathname
  if (path === '/api/mcp') return json(SERVERS)
  if (path === '/api/mcp/scopes') return json({ scopes: [] })
  if (path === '/api/connections/mint') {
    return json({ ok: true, slug: 'docs-internal', state: 'waiting', token: 't', oauth_url: 'https://docs-internal.example.com/authorize' })
  }
  return json({})
}

initI18n('en')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text p-5" style={{ width: 1180 }}>
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpTab />
      </QueryClientProvider>
    </MemoryRouter>
  </div>,
)
