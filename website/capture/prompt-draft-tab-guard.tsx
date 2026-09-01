/**
 * Isolated capture entry for the Prompts tab-switch draft guard.
 *
 * WHY ISOLATED: the surface is a Capabilities pane whose list comes from the
 * gateway's prompt store, and the thing under test is what survives an UNMOUNT
 * driven by the rail. A capture page has no gateway behind it, so the two
 * /api/prompts reads are stubbed and everything else is real: the REAL
 * SidePanelLayout (the rail, and the leave guard this change adds), the REAL
 * PromptsTab with its real form and dirty predicates, the real stylesheet and
 * theme tokens.
 *
 * The rail here mirrors CapabilitiesPage's own shape for the three tabs that
 * matter -- each pane conditionally rendered, which is what makes a tab switch
 * an unmount in the first place.
 *
 * The confirm itself is the browser's native dialog and cannot appear in a page
 * screenshot; the capture script asserts it fired and with which message, and
 * these frames show the OUTCOME of each answer.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import SidePanelLayout from '../src/components/SidePanelLayout'
import { PromptsTab } from '../src/pages/overview'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

/** Two prompts, enough for the list pane to look like a real install rather
 *  than an empty state (the empty state hides the list column entirely). */
const PROMPTS = [
  {
    name: 'release-notes', fullName: 'release-notes',
    description: 'Draft release notes from a milestone',
    path: '~/.kiro/prompts/release-notes.md', package: '', source: 'global',
  },
  {
    name: 'triage', fullName: 'triage',
    description: 'Triage an inbound bug report',
    path: '~/.kiro/prompts/triage.md', package: '', source: 'global',
  },
]

const DETAIL = {
  content: '---\ndescription: Triage an inbound bug report\n---\n\nRead the report and classify it.\n',
  redacted: false,
  lossy: false,
  hash: 'a'.repeat(64),
}

// The pane's own reads. An unanswered /api/prompts leaves the list in its
// loading state, which photographs nothing.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  if (url.includes('/api/prompts/')) {
    return new Response(JSON.stringify(DETAIL), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  if (url.includes('/api/prompts')) {
    return new Response(JSON.stringify(PROMPTS), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  if (url.includes('/api/')) {
    return new Response('{}', {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  return realFetch(input, init)
}) as typeof window.fetch

const TABS = [
  { key: 'prompts', label: 'Prompts', icon: null, description: 'Saved prompts you can run in any chat', group: 'Knowledge & instructions' },
  { key: 'steering', label: 'Steering', icon: null, description: 'Always-on instructions for the agent', group: 'Knowledge & instructions' },
  { key: 'hooks', label: 'Hooks', icon: null, description: 'Run something when an event fires', group: 'Automation' },
]

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/capabilities?tab=prompts']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', height: '100vh', display: 'flex' }}
          data-capture-root
        >
          <SidePanelLayout title="Agent Capabilities" tabs={TABS}>
            {tab => <>
              {tab === 'prompts' && <PromptsTab />}
              {tab === 'steering' && <div data-testid="other-pane" style={{ padding: 8 }}>Steering pane</div>}
              {tab === 'hooks' && <div data-testid="other-pane" style={{ padding: 8 }}>Hooks pane</div>}
            </>}
          </SidePanelLayout>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
