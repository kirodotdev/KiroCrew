/**
 * Isolated capture entry for the Code Review Sage label filter (issue #3788).
 *
 * WHY ISOLATED: the chip row only exists once a repo is active and its open PRs
 * have come back from `gh`, which needs a gateway, a GitHub token and a repo
 * that actually uses labels. This mounts the REAL PrPickList behind the REAL
 * SageProvider against the real stylesheet, theme tokens and live i18n catalog,
 * and stubs only `fetch` — the network boundary — so the app's own api module,
 * query client and derivations all run exactly as they ship.
 *
 * Scene comes from the query string:
 *   ?scene=unfiltered   the chip row at rest, nothing selected
 *   ?scene=filtered     two chips pressed, list narrowed by their OR
 *   &theme=dark|light
 *
 * The `filtered` scene is produced by the harness CLICKING the real chips, not
 * by forcing component state, so a frame documents the shipped wiring.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import PrPickList from '../src/apps/code-review-sage/components/PrPickList'
import { SageProvider, useSage } from '../src/apps/code-review-sage/context'
import { initI18n } from '../src/i18n/all'
import { useEffect, useRef } from 'react'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const REPO = { owner: 'acme', repo: 'monorepo' }

/** Label sets modelled on real open PRs, so the frame shows the shapes a reviewer
 *  will actually meet -- including a multi-word label with a colon and a PR
 *  carrying none. The owner/repo is fictional on purpose: a capture fixture
 *  should not read as though it came from a real repository. */
const PRS = [
  ['Filter the PR list by GitHub label', ['area: apps', 'enhancement'], false],
  ['Refuse a junctioned runs root in the follow-up guard', ['area: apps', 'bug'], true],
  ['Retire the review pool when the app is disabled', ['bug'], false],
  ['Safely parallelize run-scoped reviews', ['enhancement', 'fork'], false],
  ['Drop the shadowed binding in validation.py', [], true],
].map(([title, labels, reviewed], i) => ({
  url: `https://github.com/acme/monorepo/pull/${8900 + i}`,
  number: 8900 + i,
  title,
  head_sha: `deadbeef${i}`,
  author: ['vlj91', 'leonlaiyc', 'dwu96', 'Premshay', 'bolichen97'][i],
  updated_at: new Date(Date.now() - (i + 1) * 5400_000).toISOString(),
  draft: false,
  change_id: `CR-${8900 + i}`,
  reviewed,
  reviewed_stale: false,
  reviewed_at: reviewed ? new Date(Date.now() - 7200_000).toISOString() : '',
  labels,
}))

// Stub the NETWORK, not the app: every module under test runs for real.
const ROUTES: Record<string, unknown> = {
  'repo-prs': { repo: 'acme/monorepo', prs: PRS, count: PRS.length },
  'pinned-repos': { repos: [REPO] },
  'recent-repos': { repos: [] },
  runs: { runs: [] },
  settings: { settings: { model: null, effort: 'medium', active_namespaces: [], max_concurrent: 2 }, models: [] },
}

window.fetch = (async (input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const hit = Object.keys(ROUTES).find((k) => url.includes(k))
  return new Response(JSON.stringify(hit ? ROUTES[hit] : {}), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}) as typeof fetch

function Harness() {
  const { setActiveRepo } = useSage()
  const armed = useRef(false)
  useEffect(() => {
    // Arm ONCE: re-activating on every render would refetch forever.
    if (!armed.current) {
      armed.current = true
      setActiveRepo(REPO)
    }
  }, [setActiveRepo])
  // The middle column's real width, so the chip row wraps the way it ships.
  return (
    <div className="h-screen bg-bg text-text flex" data-capture-root>
      <div className="w-[420px] border-r border-border flex flex-col min-h-0">
        <PrPickList />
      </div>
    </div>
  )
}

initI18n('en')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>
      <SageProvider initialRunId={null}>
        <Harness />
      </SageProvider>
    </QueryClientProvider>
  </MemoryRouter>,
)
