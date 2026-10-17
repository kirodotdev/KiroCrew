/**
 * Isolated capture entry for a Code Review Sage report row re-banding when its
 * last red finding is dismissed (issue #18573).
 *
 * WHY ISOLATED: a finished review with findings needs a gateway, a GitHub token
 * and a reviewer run. This mounts the REAL RunDetail (report view, band chips,
 * finding cards) behind the REAL SageProvider against the real stylesheet, theme
 * tokens and live i18n catalog, and stubs only `fetch` -- the network boundary --
 * so the app's own api module, query client and derivations all run as they ship.
 *
 * The stub is STATEFUL in the one way the backend is: a POST to `/dismiss`
 * records the dismissal on the run, and the next GET of `/report` serves the
 * row re-banded (red -> yellow) with the band tallies rebuilt, exactly as
 * `report.reband()` does. So a frame taken after the harness CLICKS the real
 * "Dismiss" control documents the shipped wiring, not a hand-posed state.
 *
 * Query string: `&theme=dark|light`.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import RunDetail from '../src/apps/code-review-sage/components/RunDetail'
import { SageProvider, useSage } from '../src/apps/code-review-sage/context'
import type { ReportRow, Run, RunReport } from '../src/apps/code-review-sage/lib/types'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const RUN_ID = 'run-reband'

/** Three rows, modelled on a real triage pass. The owner/repo is fictional on
 *  purpose: a capture fixture should not read as though it came from a real
 *  repository. `CR-8902` is red ONLY for its one red finding (blast MEDIUM,
 *  design risk medium), so dismissing that finding drops it to yellow and, with
 *  its lower score, below `CR-8903`. `CR-8901` is red for blast=LARGE and keeps
 *  its band whatever is dismissed. */
function row(partial: Partial<ReportRow> & Pick<ReportRow, 'change_id' | 'title'>): ReportRow {
  const n = partial.change_id.replace('CR-', '')
  return {
    url: `https://github.com/acme/monorepo/pull/${n}`,
    platform: 'github',
    band: 'green', why: '', score: 0,
    design_risk: 'low', blast: 'SMALL', red: 0, yellow: 0,
    deep_reviewed: true, gate_verdict: 'PASS',
    design_headline: '', problem: '', why_it_matters: '', solution_assessment: '',
    rationale: '', ship_comment: '', findings: [],
    ...partial,
  }
}

const FINDING_RED = {
  dimension: 'Correctness', severity: 'red' as const, file: 'sage_lib/report.py', line: 281,
  headline: 'The stored band is served after its inputs change',
  observation: 'classify() runs once when the report is written; a dismissal later changes counts and nothing recomputes.',
  consequence: 'A row whose only red finding was dismissed stays in Needs review.',
  suggestion: 'Recompute the band from the record with dismissed findings removed from counts.',
}
const FINDING_YELLOW = (file: string, headline: string) => ({
  dimension: 'Maintainability', severity: 'yellow' as const, file, line: 42, headline,
  observation: 'Two definitions of one key format.', consequence: 'They can drift apart.',
  suggestion: 'Define it once and import it.',
})

const ROWS: ReportRow[] = [
  row({
    change_id: 'CR-8901', title: 'Refuse a junctioned runs root in the follow-up guard',
    band: 'red', why: 'blast=LARGE + 1× 🔴', score: 55, design_risk: 'medium', blast: 'LARGE',
    red: 1, yellow: 0, gate_verdict: 'CONCERNS',
    design_headline: 'The guard follows a junction before it checks the root.',
    findings: [{ ...FINDING_RED, file: 'sage_lib/followup.py', line: 118,
      headline: 'The root check runs after the junction is resolved' }],
  }),
  row({
    change_id: 'CR-8902', title: 'Re-band a report row when its findings are dismissed',
    band: 'red', why: '1× 🔴', score: 35, design_risk: 'medium', blast: 'MEDIUM',
    red: 1, yellow: 1, gate_verdict: 'CONCERNS',
    design_headline: 'The band is derived once and read back after its inputs change.',
    findings: [FINDING_RED, FINDING_YELLOW('backend/routes.py', 'A second regex for the finding key')],
  }),
  row({
    change_id: 'CR-8903', title: 'Retire the review pool when the app is disabled',
    band: 'yellow', why: 'blast=MEDIUM + 2× 🟡', score: 30, design_risk: 'medium', blast: 'MEDIUM',
    red: 0, yellow: 2, gate_verdict: 'CONCERNS',
    design_headline: 'The pool outlives the app that owns it.',
    findings: [FINDING_YELLOW('sage_lib/review_pool.py', 'end_batch is never awaited on disable'),
      FINDING_YELLOW('backend/routes.py', 'The disable hook does not drain the pool')],
  }),
]

const RUN: Run = {
  run_id: RUN_ID, repo: 'acme/monorepo',
  changes: ROWS.map((r) => r.url), change_ids: ROWS.map((r) => r.change_id),
  status: 'done',
  started_at: new Date(Date.now() - 3_600_000).toISOString(),
  finished_at: new Date(Date.now() - 3_300_000).toISOString(),
  progress: Object.fromEntries(ROWS.map((r) => [r.change_id, {
    phase: 'done', counts: { red: r.red, yellow: r.yellow }, expected: r.findings!.length + 1,
  }])),
  summary: { ok: true, changes: 3, deep_reviewed: 3, result_records: 3,
    report: { bands: { red: 2, yellow: 1, green: 0 }, total: 3 } },
  dismissed: {},
}

/** The backend's `reband()`, in miniature: a row red only for its finding
 *  count drops a band once that finding is dismissed; every other reason holds. */
function rebanded(): RunReport {
  const rows = ROWS.map((r) => {
    const gone = Object.keys(RUN.dismissed?.[r.change_id] ?? {})
    if (!gone.length) return r
    let red = r.red
    for (const key of gone) {
      const i = Number(key.replace('finding:', ''))
      if (r.findings?.[i]?.severity === 'red') red -= 1
    }
    if (red > 0 || r.blast === 'LARGE') return r
    return { ...r, band: 'yellow' as const, why: 'blast=MEDIUM', score: r.score - 15 }
  })
  const rank = { red: 0, yellow: 1, green: 2 }
  rows.sort((a, b) => (rank[a.band] - rank[b.band]) || (b.score - a.score))
  const bands = { red: 0, yellow: 0, green: 0 }
  for (const r of rows) bands[r.band] += 1
  return { run_id: RUN_ID, status: 'done', ready: true, bands, rows,
    generated_at: RUN.finished_at!, total: rows.length, report_slug: null }
}

// Stub the NETWORK, not the app: every module under test runs for real.
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const json = (body: unknown) => new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  })
  if (url.endsWith(`/runs/${RUN_ID}/dismiss`) && init?.method === 'POST') {
    const body = JSON.parse(String(init.body)) as {
      change_id: string; key: string; reason?: string; dismissed?: boolean
    }
    const per = { ...(RUN.dismissed?.[body.change_id] ?? {}) }
    if (body.dismissed === false) delete per[body.key]
    else per[body.key] = { reason: body.reason ?? '' }
    RUN.dismissed = { ...RUN.dismissed, [body.change_id]: per }
    const report = rebanded()
    RUN.summary = { ...RUN.summary, report: { bands: report.bands, total: report.total } }
    return json({ ok: true, run_id: RUN_ID, change_id: body.change_id, key: body.key,
      dismissed: body.dismissed !== false })
  }
  if (url.endsWith(`/runs/${RUN_ID}/report`)) return json(rebanded())
  if (url.endsWith(`/runs/${RUN_ID}`)) return json({ run: RUN })
  if (url.endsWith('/runs')) return json({ runs: [RUN], pool: null, reviewer: null })
  if (url.includes('/settings')) {
    return json({ settings: { model: null, effort: 'medium', active_namespaces: [], max_concurrent: 2 }, models: [] })
  }
  return json({ repos: [], runs: [], prs: [], count: 0 })
}) as typeof fetch

function Harness() {
  const { runs } = useSage()
  const run = runs.find((r) => r.run_id === RUN_ID)
  // The detail pane's real width, so the cards wrap the way they ship.
  return (
    <div className="h-screen bg-bg text-text flex" data-capture-root>
      <div className="w-[760px] flex flex-col min-h-0">
        {run ? <RunDetail run={run} /> : null}
      </div>
    </div>
  )
}

initI18n('en')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>
      <SageProvider initialRunId={RUN_ID}>
        <Harness />
      </SageProvider>
    </QueryClientProvider>
  </MemoryRouter>,
)
