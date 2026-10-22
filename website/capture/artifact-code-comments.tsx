/** Isolated capture entry for line-anchored comments on artifact code blocks.
 *
 * Mounts the REAL `ArtifactDetailPage` over a fetch stub at the API boundary
 * (the same pattern as artifact-selection-composer.tsx): a markdown artifact
 * with one TypeScript fence, and two code-line threads on it — one already
 * read, one unread — so the chips render in both states inside Pierre's
 * shadow DOM. A comment POST is recorded on `window.__posted` and appended.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import ArtifactDetailPage from '../src/pages/ArtifactDetailPage'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'retry-helper'
localStorage.removeItem(`mc-cmt-sent:${SLUG}`)
// Thread c1 is read, c2 is not: the chips show both states.
localStorage.setItem(`mc-cmt-read:${SLUG}`, JSON.stringify(['c1']))

const MARKDOWN = [
  '# Retry helper',
  '',
  'Wraps a call with capped exponential backoff.',
  '',
  '```ts',
  'export async function retry<T>(fn: () => Promise<T>, tries = 3): Promise<T> {',
  '  let delay = 200',
  '  for (let i = 0; i < tries; i++) {',
  '    try { return await fn() } catch (e) { if (i === tries - 1) throw e }',
  '    await new Promise(r => setTimeout(r, delay))',
  '    delay = Math.min(delay * 2, 5000)',
  '  }',
  '  throw new Error("unreachable")',
  '}',
  '```',
  '',
  'Callers pass an idempotent function.',
].join('\n')

/** A code-line anchor exactly as the page builds one: source offsets, the
 *  source line as quote, raw-source context around it. */
function codeAnchor(line: string) {
  const at = MARKDOWN.indexOf(line)
  return {
    quote: line,
    prefix: MARKDOWN.slice(Math.max(0, at - 32), at),
    suffix: MARKDOWN.slice(at + line.length, at + line.length + 32),
    start_offset: at,
    end_offset: at + line.length,
  }
}

const ARTIFACT = {
  slug: SLUG, name: 'Retry helper', kind: 'markdown', source: 'chat', description: 'Capture fixture', tags: [],
  version: 3, created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T01:00:00Z', content: MARKDOWN,
}

let nextId = 3
const comments: object[] = [
  { id: 'c1', origin: 'local', scope: 'private', author: 'sam', is_agent: false, body: 'Start lower: 100ms is enough for the first retry.', anchor: codeAnchor('  let delay = 200'), thread_id: 'c1', status: 'open', sync_state: 'local_only', created_at: '2026-06-01T02:00:00Z', updated_at: '2026-06-01T02:00:00Z' },
  { id: 'c2', origin: 'local', scope: 'private', author: 'alex', is_agent: false, body: 'Add jitter so callers do not retry in lockstep.', anchor: codeAnchor('    delay = Math.min(delay * 2, 5000)'), thread_id: 'c2', status: 'open', sync_state: 'local_only', created_at: '2026-06-01T02:30:00Z', updated_at: '2026-06-01T02:30:00Z' },
]

const posted: unknown[] = []
;(window as unknown as { __posted: unknown[] }).__posted = posted

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  const method = (init?.method ?? 'GET').toUpperCase()
  if (url.includes(`/api/artifacts/${SLUG}/comments`)) {
    if (method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}')) as { text: string; anchor?: object; scope?: string }
      posted.push(body)
      const id = `c${nextId++}`
      comments.push({
        id, origin: 'local', scope: body.scope ?? 'private', author: 'you', is_agent: false, body: body.text,
        anchor: body.anchor, thread_id: id, status: 'open', sync_state: 'local_only',
        created_at: '2026-06-01T03:00:00Z', updated_at: '2026-06-01T03:00:00Z',
      })
      return json({ ok: true, id })
    }
    return json({ comments, remote_sync_error: null })
  }
  if (url.includes(`/api/artifacts/${SLUG}/versions`)) return json({ slug: SLUG, versions: [3] })
  if (url.includes(`/api/artifacts/${SLUG}/events`)) return json({ slug: SLUG, events: [] })
  if (url.includes(`/api/artifacts/${SLUG}`)) return json(ARTIFACT)
  if (url.includes('/api/artifact-folders')) return json({ folders: [] })
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[`/artifacts/${SLUG}`]}>
          <div style={{ width: '100%', minHeight: '100vh' }} className="bg-bg text-text">
            <Routes>
              <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
            </Routes>
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
