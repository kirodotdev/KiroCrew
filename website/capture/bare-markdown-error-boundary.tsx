/**
 * Isolated capture entry for issue #10620, residual 3: the two surfaces that
 * rendered `MarkdownRenderer` bare -- channel message bodies and the
 * notification detail body -- now sit inside the per-item `MessageErrorBoundary`
 * the chat transcript already uses.
 *
 * WHY ISOLATED: the scene is "one item's markdown crashed". The real renderer
 * has no known crash on current main and a security follow-up must not publish
 * one, so the capture script aliases the renderer module to
 * `stubs/CrashingMarkdownRenderer.tsx`, which throws on one marker string and
 * forwards everything else to the real renderer. The surface, the boundary,
 * the fallback, the stylesheet and i18n are all production code; the gateway
 * calls the channel page makes on mount are answered from fixtures below.
 *
 * Scenes (?scene=), each with ?theme=dark|light:
 *   channel       the REAL ChannelPage over a fixture channel: one poisoned
 *                 message between two healthy ones. Expected: the poisoned row
 *                 shows the boundary fallback, its neighbours and the header
 *                 render normally.
 *   notification  the REAL NotificationDetailPanel over a poisoned body.
 *                 Expected: header, actions and the fallback; no app crash.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import ChannelPage from '../src/pages/ChannelPage'
import NotificationDetailPanel from '../src/components/notifications/NotificationDetailPanel'
import type { Notification } from '../src/types'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'channel'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Same marker the crash stand-in throws on. Spelled here rather than imported
// so this entry compiles unchanged against the real renderer too.
const CRASH = '__capture_crash_marker__'

const CHANNEL = {
  id: 'ch-incident',
  topic: 'Incident 4412 — checkout latency',
  members: {
    a1: { id: 'a1', role: 'Orchestrator', agent_name: 'kirocrew', state: 'listening', listen_mode: 'mention', approval_policy: 'writes', session_key: 'k1' },
    a2: { id: 'a2', role: 'Logs Agent', agent_name: 'kirocrew', state: 'done', listen_mode: 'mention', approval_policy: 'writes', session_key: 'k2' },
  },
  messages: [
    { id: 'm1', from_id: 'human', from_role: 'You', content: 'Checkout p99 doubled at 14:02. @Logs Agent what changed?', msg_type: 'mention', timestamp: 1_757_950_920 },
    { id: 'm2', from_id: 'a2', from_role: 'Logs Agent', content: `Pulled the deploy log: **release 2026.09.15-3** rolled at 14:00.\n\n${CRASH}`, msg_type: 'progress', timestamp: 1_757_950_980 },
    { id: 'm3', from_id: 'a1', from_role: 'Orchestrator', content: 'Correlates with the rollout. Proposing a rollback to `2026.09.15-2` and a hold on the pipeline.', msg_type: 'progress', timestamp: 1_757_951_040 },
  ],
}

const NOTIFICATION: Notification = {
  kind: 'agent',
  title: 'Nightly report finished',
  body: `Report attached below.\n\n${CRASH}`,
  ts: '1757951100.000',
}

// The page's own gateway calls: a capture page has no backend, so answer the
// channel list, presets and detail from the fixture, and every other /api/
// call with an empty object.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  if (url.includes('/api/channels/presets')) return json({ presets: [] })
  if (url.includes('/api/channels/')) return json(CHANNEL)
  if (url.includes('/api/channels')) return json({ channels: [CHANNEL] })
  if (url.includes('/api/agents')) return json({ agents: [{ name: 'kirocrew' }], default: 'kirocrew' })
  if (url.includes('/api/')) return json({})
  return realFetch(input, init)
}) as typeof window.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[scene === 'channel' ? '/channels' : '/notifications']}>
        <div style={{ background: 'var(--bg)', color: 'var(--text)', height: '100vh' }} data-capture-root>
          {scene === 'channel'
            ? <ChannelPage />
            : <div style={{ maxWidth: 720, height: '100%' }}><NotificationDetailPanel n={NOTIFICATION} onClose={() => {}} /></div>}
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
