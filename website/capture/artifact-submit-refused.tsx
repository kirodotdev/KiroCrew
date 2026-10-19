/** Isolated capture entry for #12320: a refused batch send, and the draft action.
 *
 * WHY ISOLATED: a refused send needs a chat that refuses, and the page sidebar
 * needs a bound companion session. Here both hosts mount over stubs:
 *
 * - left: the REAL `CommentsSidebar` as the standalone page renders it (340px,
 *   stacked `SubmitBar`, "Draft a prompt in chat"). Its Submit answers with the
 *   page's refusal shape: the server's reason string.
 * - right: the REAL `ArtifactPanel` (the chat side panel) over a fetch stub.
 *   Its `onSubmitComments` answers `false`, the chat send's refused verdict.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import ArtifactPanel from '../src/components/ArtifactPanel'
import { CommentsSidebar } from '../src/components/CommentsSidebar'
import { SubmitBar } from '../src/components/ArtifactSubmitBar'
import type { ArtifactComment } from '../src/types'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'quarterly-plan'
localStorage.removeItem(`mc-cmt-sent:${SLUG}`)

const ARTIFACT = {
  slug: SLUG, name: 'Quarterly plan', kind: 'markdown', source: 'chat', description: 'Capture fixture',
  tags: [], version: 3, created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T01:00:00Z',
  content: '# Quarterly plan\n\nShip the review workflow, then measure adoption for two weeks.\n\n- Pilot with one team\n- Review adoption metrics',
}

const mk = (id: string, body: string, quote: string) => ({
  id, origin: 'local', scope: 'private', author: 'sam', is_agent: false, body, anchor: { quote },
  thread_id: id, status: 'open', sync_state: 'local_only',
  created_at: '2026-06-01T02:00:00Z', updated_at: '2026-06-01T02:00:00Z',
}) as unknown as ArtifactComment
const comments = [
  mk('c1', 'Name the two teams piloting this.', 'Pilot with one team'),
  mk('c2', 'Two weeks feels short. Justify the window.', 'two weeks'),
]

/** API stub: the artifact and its comments from the fixture, `{}` for any other read. */
const passThrough = window.fetch.bind(window)
window.fetch = (input, init) => {
  const href = String(input instanceof Request ? input.url : input)
  if (!href.includes('/api/')) return passThrough(input, init)
  const body = href.includes(`/api/artifacts/${SLUG}/comments`) ? { comments, remote_sync_error: null }
    : href.includes(`/api/artifacts/${SLUG}`) ? ARTIFACT : {}
  return Promise.resolve(Response.json(body))
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const noop = () => {}

function Harness() {
  return (
    <div className="bg-bg text-text" style={{ display: 'flex', gap: 24, padding: 16, height: '100vh', boxSizing: 'border-box' }}>
      <div data-testid="page-sidebar" style={{ width: 340, flexShrink: 0 }}>
        <CommentsSidebar
          comments={comments}
          onAdd={noop} onReply={noop} onResolve={noop} onMarkReview={noop} onDelete={noop} onRefresh={noop} onClose={noop}
          onAskAgent={noop}
          submitBar={<SubmitBar count={2} submitting={false} stacked onSubmit={() => Promise.resolve('slot agent mismatch')} />}
          pendingSubmitIds={new Set(['c1', 'c2'])}
          containerStyle={{ height: 'calc(100vh - 32px)' }}
        />
      </div>
      <div data-testid="side-panel" style={{ width: 460, flexShrink: 0, borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>
        <ArtifactPanel slug={SLUG} kind="markdown" content={ARTIFACT.content} onClose={noop}
          onSubmitComments={() => Promise.resolve(false)} connected embedded />
      </div>
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
