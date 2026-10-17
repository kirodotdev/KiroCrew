/**
 * Isolated capture entry for the Usage tab's estimated Kiro CLI tokens: the
 * Estimated Tokens card and the Daily History "Est. tokens" column.
 *
 * WHY ISOLATED: the figures come from the backend's scan of kiro-cli session
 * documents, and a real host shows only its own state (one month, one
 * completeness case). The scene stubs ONLY the one endpoint UsageTab reads --
 * `/api/usage/kiro` -- and renders the real UsageTab through the real acp
 * provider adapter, so every cell is the component's own output. Same shape as
 * capture/usage-daily-credits.tsx.
 *
 * scene=full        -> both months populated, every session document read
 * scene=incomplete  -> unreadable_sessions > 0, so the card warns that the
 *                      totals leave those session documents out
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
const scene = params.get('scene') || 'full'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Newest day last, as the backend orders it; the table reverses. One day with
// an estimate but no transcript start (a session that ran past midnight).
const dailyHistory = [
  { date: '2026-09-26', sessions: 4, messages: 58, tool_calls: 21, credits: 312.4, est_tokens: 182_400_000 },
  { date: '2026-09-27', sessions: 0, messages: 0, tool_calls: 0, credits: 18.75, est_tokens: 9_100_000 },
  { date: '2026-09-28', sessions: 7, messages: 133, tool_calls: 64, credits: 1204.1, est_tokens: 611_000_000 },
  { date: '2026-09-29', sessions: 2, messages: 19, tool_calls: 5, credits: 96.02, est_tokens: 41_700_000 },
  { date: '2026-09-30', sessions: 9, messages: 240, tool_calls: 118, credits: 3763.5, est_tokens: 411_900_000 },
  { date: '2026-10-01', sessions: 6, messages: 158, tool_calls: 128, credits: 5223.16, est_tokens: 504_500_000 },
  { date: '2026-10-02', sessions: 3, messages: 56, tool_calls: 49, credits: 752.47, est_tokens: 104_400_000 },
]
const sessions = {
  total_sessions: 31,
  total_messages: 664,
  total_tool_calls: 385,
  all_time_sessions: 212,
  daily_history: dailyHistory,
  today: { sessions: 3, messages: 56, tool_calls: 49 },
  this_week: { sessions: 9, messages: 214, tool_calls: 177 },
  this_month: { sessions: 9, messages: 214, tool_calls: 177 },
  avg_msgs_per_session: 21.4,
  avg_tools_per_session: 12.4,
  refused_transcripts: 0,
  estimated_tokens: {
    this_month: { input: 628_117_402, output: 74_916, requests: 1978 },
    last_month: { input: 9_343_308_603, output: 4_084_537, requests: 30825 },
    unreadable_sessions: scene === 'incomplete' ? 3 : 0,
  },
}
const body = {
  username: 'alice',
  sessions,
  billing: {
    credits_used: 5975.63,
    credits_plan: 10000,
    percentage: 60,
    plan: 'Pro',
    resets: '2026-11-01',
  },
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/usage/kiro')) {
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
