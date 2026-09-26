/** Isolated capture entry for the artifact surfaces' type-first selection composer.
 *
 * WHY ISOLATED: the subject is what happens the moment text is selected on an
 * artifact — the comment box opens at once, the anchored comment lands in the
 * durable store, and the sidebar / highlight show it. In a live gateway that
 * needs a saved artifact and a comment store; here the REAL hosts mount over a
 * fetch stub at the API boundary, so every state on screen is the components'
 * own (`SelectionToolbar`'s composer, the hosts' `onOpen` anchor resolution, the
 * real post → refetch → overlay path).
 *
 * WHAT IS FAITHFUL: the real `ArtifactDetailPage` (the full artifact page) or
 * the real `ArtifactPanel` (the chat side panel), their real comment layers,
 * the real `SelectionToolbar`, and for a widget artifact the real sandboxed
 * iframe with the real in-iframe comment bridge — the driver selects text
 * INSIDE the frame and the bridge relays it. The fetch boundary serves one
 * artifact and a mutable durable-comment list; a comment POST is recorded on
 * `window.__posted` (so the driver can assert the anchor that was sent) and
 * appended to the list, exactly what a real post-then-refetch does.
 *
 * Query string: ?theme=dark|light&host=page|panel&kind=markdown|widget
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank and the screenshot misrepresents the real UI.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import ArtifactPanel from '../src/components/ArtifactPanel'
import ArtifactDetailPage from '../src/pages/ArtifactDetailPage'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const host = params.get('host') === 'panel' ? 'panel' : 'page'
const kind = params.get('kind') === 'widget' ? 'widget' : 'markdown'
// ThemeProvider is the authority (see side-panel-pinned-views.tsx): seed the
// preference it reads, and set the attribute for the pre-effect first paint.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'quarterly-plan'
// Read / sent tracking persists per artifact; clear so every load starts clean.
localStorage.removeItem(`mc-cmt-sent:${SLUG}`)
localStorage.removeItem(`mc-cmt-read:${SLUG}`)

const MARKDOWN = [
  '# Quarterly plan',
  '',
  'Ship the review workflow, then measure adoption for two weeks before',
  'expanding the rollout to the remaining teams.',
  '',
  '## Milestones',
  '',
  '- Draft the rollout checklist',
  '- Pilot with one team',
  '- Review adoption metrics',
].join('\n')

const WIDGET = [
  '<div style="font-family: system-ui, sans-serif; padding: 24px; line-height: 1.6">',
  '<h1 style="font-size: 20px; margin: 0 0 12px">Quarterly plan</h1>',
  '<p>Ship the review workflow, then measure adoption for two weeks before expanding the rollout to the remaining teams.</p>',
  '<h2 style="font-size: 16px; margin: 16px 0 8px">Milestones</h2>',
  '<ul><li>Draft the rollout checklist</li><li>Pilot with one team</li><li>Review adoption metrics</li></ul>',
  '</div>',
].join('')

const ARTIFACT = {
  slug: SLUG,
  name: 'Quarterly plan',
  kind,
  source: 'chat',
  description: 'Capture fixture',
  tags: [],
  version: 3,
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T01:00:00Z',
  content: kind === 'widget' ? WIDGET : MARKDOWN,
}

let nextId = 2
const comments: object[] = [
  { id: 'c1', origin: 'local', scope: 'private', author: 'sam', is_agent: false, body: 'Name the two teams piloting this.', anchor: { quote: 'Pilot with one team', prefix: 'Draft the rollout checklist', suffix: 'Review adoption metrics' }, thread_id: 'c1', status: 'open', sync_state: 'local_only', created_at: '2026-06-01T02:00:00Z', updated_at: '2026-06-01T02:00:00Z' },
]

/** Every comment POST the hosts made, for the driver's anchor assertions. */
const posted: unknown[] = []
;(window as unknown as { __posted: unknown[] }).__posted = posted

/** Fetch stub at the API boundary: the artifact reads and the durable-comment
 *  list answer from the fixture above; a comment POST is recorded and appended;
 *  the sandboxed document mint answers with a blob URL of the html the host
 *  built (so the frame runs the REAL in-iframe bridge); every other read
 *  answers an empty payload so no code path hangs on a gateway that does not
 *  exist here. */
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const json = (body: unknown) => new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  })
  const method = (init?.method ?? 'GET').toUpperCase()
  if (url.includes('/api/sandbox-doc') && method === 'POST') {
    const { html } = JSON.parse(String(init?.body ?? '{}')) as { html: string }
    return json({ url: URL.createObjectURL(new Blob([html], { type: 'text/html' })) })
  }
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

function Harness() {
  if (host === 'panel') {
    return (
      // A right-dock-sized frame, the width the panel occupies beside the chat.
      <div style={{ width: 460, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }} className="bg-bg text-text">
        <ArtifactPanel
          slug={SLUG}
          kind={kind}
          content={ARTIFACT.content}
          onClose={() => {}}
          onSubmitComments={() => {}}
          connected
          embedded
        />
      </div>
    )
  }
  return (
    <div style={{ width: '100%', minHeight: '100vh' }} className="bg-bg text-text">
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      </Routes>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[`/artifacts/${SLUG}`]}>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
