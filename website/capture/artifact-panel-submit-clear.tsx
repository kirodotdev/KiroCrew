/** Isolated capture entry for the side-panel artifact submit-to-chat bar.
 *
 * WHY ISOLATED: the subject is the bar's lifecycle around a submission —
 * pending count before Submit, cleared after, and a later comment counted
 * alone. In a live gateway that arc needs a chat session, a seeded artifact
 * and durable comments; here the REAL `ArtifactPanel` mounts over a fetch
 * stub, so the bar's state comes from the component's own pending derivation
 * (`mc-cmt-sent:<slug>` sent-id tracking), not from a fixture of the bar.
 *
 * WHAT IS FAITHFUL: the real `ArtifactPanel`, its real `useFileArtifactComments`
 * layer, and the real Submit click path. The fetch boundary serves one markdown
 * artifact and a mutable durable-comment list; `window.__addComment()` appends
 * to that list and invalidates the comments query, modelling a comment added
 * after a submission. `onSubmitComments` records the formatted message so the
 * driver can assert what a submission actually carried.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank and the screenshot misrepresents the real UI.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import ArtifactPanel from '../src/components/ArtifactPanel'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
// ThemeProvider is the authority (see side-panel-pinned-views.tsx): seed the
// preference it reads, and set the attribute for the pre-effect first paint.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'quarterly-plan'
// Sent ids persist per artifact; clear them so every load starts at the
// pre-submission state regardless of a prior run in the same context.
localStorage.removeItem(`mc-cmt-sent:${SLUG}`)

const ARTIFACT = {
  slug: SLUG,
  name: 'Quarterly plan',
  kind: 'markdown',
  source: 'chat',
  description: 'Capture fixture',
  tags: [],
  version: 3,
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T01:00:00Z',
  content: [
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
  ].join('\n'),
}

let nextId = 4
const comments: object[] = [
  { id: 'c1', origin: 'local', scope: 'private', author: 'sam', is_agent: false, body: 'Name the two teams piloting this.', anchor: { quote: 'Pilot with one team' }, thread_id: 'c1', status: 'open', sync_state: 'local_only', created_at: '2026-06-01T02:00:00Z', updated_at: '2026-06-01T02:00:00Z' },
  { id: 'c2', origin: 'local', scope: 'private', author: 'sam', is_agent: false, body: 'Two weeks feels short — justify the window.', anchor: { quote: 'two weeks' }, thread_id: 'c2', status: 'open', sync_state: 'local_only', created_at: '2026-06-01T02:01:00Z', updated_at: '2026-06-01T02:01:00Z' },
  { id: 'c3', origin: 'local', scope: 'private', author: 'sam', is_agent: false, body: 'Add a metric definition for adoption.', anchor: { quote: 'adoption metrics' }, thread_id: 'c3', status: 'open', sync_state: 'local_only', created_at: '2026-06-01T02:02:00Z', updated_at: '2026-06-01T02:02:00Z' },
]

/** Fetch stub at the API boundary: the artifact read and the durable-comment
 *  list answer from the fixture above; every other dashboard read answers an
 *  empty payload so no code path hangs on a gateway that does not exist here. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  if (url.includes(`/api/artifacts/${SLUG}/comments`)) return json({ comments, remote_sync_error: null })
  if (url.includes(`/api/artifacts/${SLUG}`)) return json(ARTIFACT)
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** Submissions the panel sent to chat, readable by the driver. */
const submitted: string[] = []
;(window as unknown as { __submitted: string[] }).__submitted = submitted

/** Model a comment added AFTER a submission: append to the durable list and
 *  invalidate the comments query, exactly what a real post-then-refetch does. */
;(window as unknown as { __addComment: (body: string) => void }).__addComment = (body: string) => {
  comments.push({
    id: `c${nextId}`, origin: 'local', scope: 'private', author: 'sam', is_agent: false,
    body, anchor: { quote: 'rollout checklist' }, thread_id: `c${nextId}`, status: 'open',
    sync_state: 'local_only', created_at: '2026-06-01T03:00:00Z', updated_at: '2026-06-01T03:00:00Z',
  })
  nextId += 1
  qc.invalidateQueries({ queryKey: ['artifact-comments', SLUG] })
}

function Harness() {
  return (
    // A right-dock-sized frame, the width the panel occupies beside the chat.
    <div style={{ width: 460, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }} className="bg-bg text-text">
      <ArtifactPanel
        slug={SLUG}
        kind="markdown"
        content={ARTIFACT.content}
        onClose={() => {}}
        onSubmitComments={m => { submitted.push(m) }}
        connected
        embedded
      />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
