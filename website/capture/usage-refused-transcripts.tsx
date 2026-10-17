/**
 * Isolated capture entry for the usage page's refused-transcript warning (#6733).
 *
 * WHY ISOLATED: the warning only shows when the backend's sessions scan refused
 * transcripts, which on a real host means a Windows roaming-profile (UNC) home.
 * Booting the full SPA plus a backend in that state is impractical, so the scene
 * stubs ONLY the one endpoint UsageTab reads -- `/api/usage/kiro` -- and renders
 * the real UsageTab through the real acp provider adapter, so the banner is the
 * component's own output, not a mock of it.
 *
 * scene=refused  -> total 0 with refused_transcripts > 0 (the UNC-home shape)
 * scene=clean    -> normal counts, no warning (the control)
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
const scene = params.get('scene') || 'refused'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const zeroPeriod = { sessions: 0, messages: 0, tool_calls: 0 }
const refusedPayload = {
  username: 'alice',
  sessions: {
    total_sessions: 0,
    total_messages: 0,
    total_tool_calls: 0,
    all_time_sessions: 0,
    daily_history: [],
    today: zeroPeriod,
    this_week: zeroPeriod,
    this_month: zeroPeriod,
    avg_msgs_per_session: 0,
    avg_tools_per_session: 0,
    refused_transcripts: 342,
  },
  billing: {},
}
const cleanPayload = {
  username: 'alice',
  sessions: {
    total_sessions: 18,
    total_messages: 240,
    total_tool_calls: 96,
    all_time_sessions: 60,
    daily_history: [{ date: '2026-09-06', sessions: 3, messages: 40, tool_calls: 12 }],
    today: { sessions: 2, messages: 20, tool_calls: 8 },
    this_week: { sessions: 9, messages: 120, tool_calls: 48 },
    this_month: { sessions: 18, messages: 240, tool_calls: 96 },
    avg_msgs_per_session: 13.3,
    avg_tools_per_session: 5.3,
    refused_transcripts: 0,
  },
  billing: {},
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/usage/kiro')) {
    const body = scene === 'clean' ? cleanPayload : refusedPayload
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

async function main() {
  initI18n('en')
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRoot(document.getElementById('root')!)
  root.render(
    <div className="min-h-screen bg-bg text-text p-8" style={{ maxWidth: 720 }}>
      <QueryClientProvider client={qc}>
        <ProviderProvider>
          <UsageTab />
        </ProviderProvider>
      </QueryClientProvider>
    </div>,
  )
}

void main()
