/** Isolated capture entry for Code Review Sage's publish-failure notice.
 *
 * WHY ISOLATED: the subject is one state of one bar — a publish that the
 * gateway refuses. Reaching it in a live gateway needs a GitHub pull request, a
 * completed Sage run and a real pending draft on it, and then a refusal that
 * only a server-side denial produces. Here the REAL `DraftReviewActions` mounts
 * over a fetch stub, so the notice is rendered by the component's own
 * `publishMut.isError` branch rather than by a fixture of the notice.
 *
 * WHAT IS FAITHFUL: the real `DraftReviewActions`, its real
 * `pullRequestErrorDetails` parsing of the refusal body, and the real
 * `ErrorNotice` it now renders — icon, title, message and the scoped
 * "Ask the agent about this failure" hand-off. The fetch boundary
 * serves one pending draft and answers the submit with a 422 refusal body, the
 * shape a rejected publish carries.
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
import DraftReviewActions from '../src/apps/code-review-sage/components/DraftReviewActions'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/6420'
const REVIEW_ID = 'PRR_draft_1'

const DRAFT = {
  reviewId: REVIEW_ID,
  body: 'Two notes on the retry path, both non-blocking.',
  comments: [{ path: 'src/kiro_crew/retry.py', line: 88, body: 'Bound this loop.' }],
  commitId: 'c0ffee1234567890',
  headSha: 'c0ffee1234567890',
  stale: false,
  contentRedacted: false,
  autoMergeArmed: false,
  contentDigest: 'sha256:draft-digest',
  staleDismissalEnabled: true,
}

// The refusal a publish actually carries: the provider's own words in an error
// envelope, which is what `pullRequestErrorDetails` reads back out of the body.
// Deliberately a refusal that leaves the draft where it is — a "no longer
// pending" message would contradict the bar's own "Pending" line one row above
// and make the frame read as two states at once.
const REFUSAL = JSON.stringify({
  error: 'gh: your token cannot submit reviews on this repository — it is missing the pull request write scope.',
})

const json = (body: unknown, status = 200) => new Response(
  typeof body === 'string' ? body : JSON.stringify(body),
  { status, headers: { 'content-type': 'application/json' } },
)

globalThis.fetch = (async (input: RequestInfo | URL) => {
  const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url
  if (path.includes('/api/source/pull-request/pending-review')) return json(DRAFT)
  if (path.includes('/api/source/pull-request/submit-review')) return json(REFUSAL, 422)
  return json({})
}) as typeof fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    {/* QueryClientProvider OUTSIDE ThemeProvider: the theme hook reads through
        react-query too, so the other order leaves it without a client. */}
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter>
          <div
            data-testid="scene"
            className="bg-bg text-text p-6"
            style={{ width: 560 }}
          >
            <DraftReviewActions url={PR_URL} draftDelivered expectedReviewId={REVIEW_ID} />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
