/**
 * Isolated capture entry for the Usage tab's Daily History credits columns (#3371).
 *
 * WHY ISOLATED: the two new columns depend on the per-day credits the backend
 * sums from the usage shards AND on whether a plan allowance parsed, and a real
 * host shows only one of those states at a time. The scene stubs ONLY the one
 * endpoint UsageTab reads -- `/api/usage/kiro` -- and renders the real UsageTab
 * through the real acp provider adapter, so every cell is the component's own
 * output, not a mock of it. Same shape as capture/usage-refused-transcripts.tsx.
 *
 * scene=plan    -> a Pro plan with a 10000-credit allowance; the "(%)" column
 *                  divides by it, and one day deliberately exceeds 100%
 * scene=noplan  -> no billing plan parsed; the "(%)" column shows a dash
 * theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { ProviderProvider } from '../src/providers'
import UsageTab from '../src/pages/overview/UsageTab'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'plan'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Newest day last, as the backend orders it; the table reverses. One day with
// credits but no transcript (a background slot), one day over 100% of the plan.
const dailyHistory = [
  { date: '2026-09-16', sessions: 4, messages: 58, tool_calls: 21, credits: 312.4 },
  { date: '2026-09-17', sessions: 0, messages: 0, tool_calls: 0, credits: 18.75 },
  { date: '2026-09-18', sessions: 7, messages: 133, tool_calls: 64, credits: 1204.1 },
  { date: '2026-09-19', sessions: 2, messages: 19, tool_calls: 5, credits: 96.02 },
  { date: '2026-09-20', sessions: 9, messages: 240, tool_calls: 118, credits: 10480.5 },
  { date: '2026-09-21', sessions: 3, messages: 41, tool_calls: 12, credits: 0 },
  { date: '2026-09-22', sessions: 5, messages: 77, tool_calls: 30, credits: 655.93 },
]
const sessions = {
  total_sessions: 30,
  total_messages: 568,
  total_tool_calls: 250,
  all_time_sessions: 212,
  daily_history: dailyHistory,
  today: { sessions: 5, messages: 77, tool_calls: 30 },
  this_week: { sessions: 19, messages: 377, tool_calls: 165 },
  this_month: { sessions: 30, messages: 568, tool_calls: 250 },
  avg_msgs_per_session: 18.9,
  avg_tools_per_session: 8.3,
  refused_transcripts: 0,
}
const planPayload = {
  username: 'alice',
  sessions,
  billing: {
    credits_used: 12767.7,
    credits_plan: 10000,
    credits_overage: 2767.7,
    percentage: 128,
    plan: 'Pro',
    resets: '2026-10-01',
  },
}
const noPlanPayload = { username: 'alice', sessions, billing: {} }

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/usage/kiro')) {
    const body = scene === 'noplan' ? noPlanPayload : planPayload
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

async function main() {
  initI18n('en')
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRoot(document.getElementById('root')!)
  root.render(
    <div className="min-h-screen bg-bg text-text p-4 sm:p-8" style={{ maxWidth: 720 }}>
      <QueryClientProvider client={qc}>
        <ProviderProvider>
          <UsageTab />
        </ProviderProvider>
      </QueryClientProvider>
    </div>,
  )
}

void main()
