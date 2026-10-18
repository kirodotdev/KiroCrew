/**
 * Isolated capture entry for the Discover panel when a skill provider could not
 * search and said why.
 *
 * `window.fetch` answers the discover endpoints so the REAL SkillBrowserModal
 * renders its real states:
 *   - ?state=rate-limited  -> every provider failed: the GitHub provider reports
 *     its rate limit and skills.sh did not answer; zero results. The reasons
 *     replace "No skills found".
 *   - ?state=partial       -> skills.sh answered with rows, GitHub could not be
 *     reached; the rows show with the reason above them. (A bare owner/repo
 *     that GitHub cannot find is an empty result, not a reason.)
 *   - ?state=empty         -> both answered ok with zero rows: an empty catalog
 *     still reads as "No skills found".
 *   - ?state=preview-error -> one GitHub row; its preview answers with the rate
 *     limit, so the detail pane shows the reason (the script selects the row).
 *   - ?state=install-refused -> the same row; install answers 422 with the
 *     provider's refusal, shown in the detail pane (the script presses Install).
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import SkillBrowserModal from '../src/components/SkillBrowserModal'
import type { DiscoveredSkill, DiscoverSkillsResponse } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const state = params.get('state') || 'rate-limited'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const ROW: DiscoveredSkill = {
  id: 'acme/skills/release-notes',
  name: 'release-notes',
  provider: 'skillsh',
  display_provider: 'skills.sh',
  description: 'Draft release notes from merged pull requests.',
  installed: false,
  installs: 1240,
}

const RESPONSES: Record<string, DiscoverSkillsResponse> = {
  'rate-limited': {
    results: [],
    providers: ['skillsh', 'github'],
    provider_outcomes: [
      { name: 'skillsh', display_name: 'skills.sh', status: 'timeout' },
      {
        name: 'github',
        display_name: 'GitHub repo',
        status: 'error',
        message: "GitHub is limiting requests. Try again in about 12 minutes.",
      },
    ],
  },
  partial: {
    results: [ROW],
    providers: ['skillsh', 'github'],
    provider_outcomes: [
      { name: 'skillsh', display_name: 'skills.sh', status: 'ok' },
      {
        name: 'github',
        display_name: 'GitHub repo',
        status: 'error',
        message: 'Could not reach GitHub (HTTP 503).',
      },
    ],
  },
  empty: {
    results: [],
    providers: ['skillsh', 'github'],
    provider_outcomes: [
      { name: 'skillsh', display_name: 'skills.sh', status: 'ok' },
      { name: 'github', display_name: 'GitHub repo', status: 'ok' },
    ],
  },
}

// The two detail-pane states start from one GitHub row the user selects.
const GITHUB_ROW: DiscoveredSkill = {
  id: 'acme/release-notes@0a1b2c3d4e5f60718293a4b5c6d7e8f901234567:skills/release-notes',
  name: 'release-notes',
  provider: 'github',
  display_provider: 'GitHub repo',
  description: '',
  installed: false,
  installs: 0,
}
RESPONSES['preview-error'] = {
  results: [GITHUB_ROW],
  providers: ['github'],
  provider_outcomes: [{ name: 'github', display_name: 'GitHub repo', status: 'ok' }],
}
RESPONSES['install-refused'] = RESPONSES['preview-error']

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()
  if (url.includes('/api/skills/-/discover/install') && method === 'POST') {
    // What the install handler returns for a provider refusal: its reason and code.
    return json(422, {
      error: "Can't install acme/release-notes:skills/release-notes: 'assets/demo.gif' is over the 1 MB limit.",
      code: 'refused',
    })
  }
  if (url.includes('/api/skills/-/discover/preview')) {
    if (state === 'preview-error') {
      return json(200, {
        name: '', description: '', content: '', files: [], file_count: 0,
        error: "GitHub is limiting requests. Try again in about 12 minutes.",
      })
    }
    return json(200, { name: 'release-notes', description: 'Draft release notes from merged pull requests.', content: '# release-notes\n', files: ['SKILL.md'], file_count: 1 })
  }
  if (url.includes('/api/skills/-/discover')) return json(200, RESPONSES[state] ?? RESPONSES['rate-limited'])
  return json(200, {})
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ minHeight: '100vh', background: 'var(--bg)' }}>
          <SkillBrowserModal open onClose={() => {}} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
