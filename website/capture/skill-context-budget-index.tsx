/**
 * Isolated capture entry for the Context budget screen's startup-index line
 * (#42713): "N of M skills listed by name at session start".
 *
 * `window.fetch` answers `GET /api/skills/-/budget` so the REAL
 * SkillContextBudget renders against the real stylesheet and theme tokens:
 *   - ?state=truncated -> a 212-skill library of which the index names 143; the
 *     line carries the skill_search / `always: true` hint.
 *   - ?state=complete  -> 12 of 12; the line states the count with no hint.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import SkillContextBudget from '../src/pages/overview/SkillContextBudget'
import type { SkillBudgetResponse, SkillBudgetRow } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const state = params.get('state') || 'truncated'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const row = (key: string, chars: number, deliveries: number, origin: SkillBudgetRow['origin']): SkillBudgetRow => ({
  key,
  name: key,
  description: `Procedure for ${key.replace(/-/g, ' ')}.`,
  origin,
  size_bytes: Math.round(chars / Math.max(deliveries, 1)),
  deliveries,
  chars,
  inject_on_trigger: true,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: 2.5,
})

const ROWS = [
  row('release-notes', 48_120, 12, 'user'),
  row('web-verify', 21_600, 9, 'builtin'),
  row('oncall-handoff', 6_400, 2, 'user'),
]

const RESPONSES: Record<string, SkillBudgetResponse> = {
  truncated: {
    window_days: 30,
    total_chars: ROWS.reduce((s, r) => s + (r.chars ?? 0), 0),
    rows: ROWS,
    index_budget_chars: 24_750,
    index_listed: 143,
    index_total: 212,
  },
  complete: {
    window_days: 30,
    total_chars: ROWS.reduce((s, r) => s + (r.chars ?? 0), 0),
    rows: ROWS,
    index_budget_chars: 24_750,
    index_listed: 12,
    index_total: 12,
  },
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
window.fetch = async (input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/skills/-/budget')) return json(200, RESPONSES[state] ?? RESPONSES.truncated)
  return json(200, {})
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter>
      <div data-capture-root style={{ minHeight: '100vh', background: 'var(--bg)', padding: 16 }}>
        <SkillContextBudget onBack={() => {}} />
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
