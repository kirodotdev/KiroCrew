/**
 * Isolated capture entry for removing a saved workflow (#18998).
 *
 * WHY ISOLATED: the Workflow library view lives under Customize > Workflows
 * behind a live gateway. This mounts the REAL WorkflowLibraryTab against the
 * real stylesheet, theme tokens and live i18n catalog, with the definition
 * routes answered by a fixture, so a frame documents the shipped control.
 *
 *   &scene=ok      — DELETE answers 200: the row leaves, the next one is selected
 *   &scene=last    — the library holds one workflow and DELETE answers 200: the
 *                    library is left empty under the "removed" note
 *   &scene=fails   — DELETE answers 500: the row stays and the error line shows
 *   &scene=pending — DELETE never answers: the button shows its in-flight state
 *   &scene=retry-pending — the first DELETE answers 500, the retry never
 *                    answers: the notice stays with its retry in flight
 *   &theme=dark|light
 *
 * The harness clicks the REAL Remove button and the REAL confirm button.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import WorkflowLibraryTab from '../src/pages/overview/WorkflowLibraryTab'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'ok'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-capture-scene', scene)

const SOURCE =
  'META = {"name": "debug-project", "description": "Investigate a failure"}\n' +
  'async def workflow(ctx):\n' +
  "    return await ctx.agent('debug the project')\n"

const definition = (id: string, slug: string, name: string, updated: string) => ({
  schema_version: 2,
  id,
  slug,
  name,
  description: 'Investigate a failure and name the root cause',
  created_at: updated,
  updated_at: updated,
  revision: 2,
  format: 'python',
  source: SOURCE,
  content_hash: 'fixture',
  derived_from: null,
  revisions: [],
})

let definitions = [
  definition('wfd_debug', 'debug-project', 'Debug Project', '2026-10-10T09:00:00Z'),
  ...(scene === 'last'
    ? []
    : [definition('wfd_triage', 'triage-tickets', 'Triage Tickets', '2026-10-09T09:00:00Z')]),
]

const originalFetch = window.fetch.bind(window)
let deleteCalls = 0
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  if (url.includes('/api/workflows/definitions/') && method === 'DELETE') {
    if (scene === 'pending') return new Promise<Response>(() => {})
    if (scene === 'retry-pending' && deleteCalls++ > 0) return new Promise<Response>(() => {})
    if (scene === 'fails' || scene === 'retry-pending') {
      const failed = json({ error: 'could not remove workflow', code: 'workflow_definition_write_failed' }, 500)
      // A Response built in the page has no url, which the browser sets on a real one.
      Object.defineProperty(failed, 'url', { value: new URL(url, location.href).href })
      return failed
    }
    const id = decodeURIComponent(url.split('/api/workflows/definitions/')[1] ?? '')
    definitions = definitions.filter((item) => item.id !== id)
    return json({ ok: true, id, removed_at: '2026-10-11T04:00:00Z' })
  }
  if (url.includes('/api/workflows/definitions')) return json({ definitions })
  return originalFetch(input as RequestInfo, init)
}) as typeof window.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter>
      <div className="min-h-screen bg-bg text-text p-6" data-capture-root>
        <WorkflowLibraryTab />
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
