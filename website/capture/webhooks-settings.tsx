/**
 * Isolated capture entry for the Settings → Webhooks panel.
 *
 * WHY ISOLATED: reaching /settings?tab=webhooks through the full SPA needs a live
 * gateway plus a dashboard credential — without one the shell renders the Kiro CLI
 * prerequisite gate instead of Settings, which is worse evidence than none. This
 * mounts the REAL WebhooksPanel against the REAL stylesheet and theme tokens, with
 * a server snapshot seeded into the same ['webhooks', 'settings-summary'] query key
 * the panel reads in production.
 *
 * Scene + theme come from the query string: ?scene=enabled&theme=dark
 * Scenes mirror the three states the badge distinguishes:
 *   enabled     — switch on, tokens exist
 *   no-tokens   — switch on, none minted yet (the first-run case)
 *   off         — operator flipped the kill switch, tokens retained
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { WebhooksPanel } from '../src/pages/settings/WebhooksPanel'
import '../src/index.css'

// Loopback on purpose: this string can reach a committed screenshot, and
// `website/capture/` sits outside scrub-lint's roots.
const URL_ = 'http://127.0.0.1:6776/api/hooks/agent'
const NOW = Date.now() / 1000

const LIMITS = {
  session_key_prefix: 'hook:',
  message_max: 49999,
  timeout_default: 599,
  timeout_max: 3593,
  max_concurrent: 6,
  body_max_bytes: 262144,
  signature_window_seconds: 300,
}

const TOKENS = [
  {
    id: 'tok_a', label: 'Review Bot', display_prefix: 'kc_whk_4f2b', last4: '1f3a',
    created_at: NOW - 3 * 86400, last_used_at: NOW - 480,
    require_signature: true, legacy: false,
  },
  {
    id: 'tok_b', label: 'CI callback', display_prefix: 'kc_whk_91de', last4: 'b231',
    created_at: NOW - 86400, last_used_at: null,
    require_signature: false, legacy: false,
  },
]

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'enabled'
const theme = params.get('theme') || 'dark'

const tokens = scene === 'no-tokens' ? [] : TOKENS
const switchOn = scene !== 'off'
const view = {
  enabled: switchOn && tokens.length > 0,
  switch_on: switchOn,
  has_tokens: tokens.length > 0,
  url: URL_,
  slots: { in_use: 2, max: 6 },
  limits: LIMITS,
  tokens,
  contexts: [],
  runs: [],
}

document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

// `staleTime: Infinity` matters: the panel's query refetches on mount otherwise,
// the harness has no gateway to answer it, and the failed refetch replaces the
// seeded snapshot with an error state — capturing the error card instead of the
// scene asked for.
const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})
qc.setQueryData(['webhooks', 'settings-summary'], view)

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {/* Width mirrors the Settings content column so wrapping matches production. */}
        <div className="bg-bg text-text min-h-screen p-8">
          <div className="max-w-[760px]">
            <WebhooksPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
