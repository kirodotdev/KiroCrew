/**
 * Isolated capture entry for the Webhooks page.
 *
 * WHY ISOLATED: driving /webhooks through the full SPA needs a live gateway plus a
 * dashboard session credential; a half-stubbed shell renders its error boundary
 * instead of the page, which is worse evidence than none. This mounts the REAL
 * WebhooksPage against the REAL stylesheet and theme tokens, with the server
 * snapshot seeded into the same ['webhooks'] query cache the page reads in
 * production.
 *
 * Scene + theme come from the query string: ?scene=enabled&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import WebhooksPage from '../src/pages/WebhooksPage'
import '../src/index.css'

// Loopback on purpose: this string is rendered into the captured screenshots,
// which are committed to the PR branch, and `website/capture/` sits outside
// scrub-lint's roots — so a real host pasted here would ride into the public
// tree unchecked, in the fixture AND baked into the PNGs.
const URL_ = 'http://127.0.0.1:6776/api/hooks/agent'
const LIMITS = {
  session_key_prefix: 'hook:',
  message_max: 49999,
  timeout_default: 599,
  timeout_max: 3593,
  max_concurrent: 6,
  body_max_bytes: 262144,
  signature_window_seconds: 300,
}
const NOW = Date.now() / 1000

const TOKENS = [
  {
    id: 'wht_7f3a91', label: 'Review Bot', display_prefix: 'kc_whk_4f2b', last4: '1f3a',
    created_at: NOW - 86400 * 3, last_used_at: NOW - 480, legacy: false,
    require_signature: true,
  },
  {
    id: 'wht_ad2be9', label: 'CI callback', display_prefix: 'kc_whk_91de', last4: 'b231',
    created_at: NOW - 86400, last_used_at: null, legacy: false,
    require_signature: false,
  },
]

const CONTEXTS = [
  {
    hook_id: 'review:pr-123', session_key: 'hook:review:pr-123', registered_at: NOW - 480,
    age_seconds: 480, freshness: 'fresh',
    context_summary:
      'Reviewing PR #123 (fix: dedupe check-runs) in worktree kirocrew-wt-check-dedupe. '
      + 'Round 2 findings addressed: identity collision between legacy commit statuses and '
      + 'check-runs, discriminator bug in the dedupe helper. Awaiting the static-analysis '
      + "bot's next pass; when it reports, triage each finding, fix legitimate Critical/High, "
      + 'push a new revision, and report status back.',
    context_chars: 412,
  },
  {
    hook_id: 'deploy:prod-4471', session_key: 'hook:deploy:prod-4471', registered_at: NOW - 21600,
    age_seconds: 21600, freshness: 'stale',
    context_summary: 'Deploy 4471 to prod queued; awaiting pipeline result before the smoke check.',
    context_chars: 208,
  },
  {
    hook_id: 'ci:build-88', session_key: 'hook:ci:build-88', registered_at: NOW - 172800,
    age_seconds: 172800, freshness: 'expired',
    context_summary: 'Build 88 flaky on the integration suite; retry once and report.',
    context_chars: 96,
  },
]

const RUNS = [
  {
    id: 'run_a1', hook_id: 'review:pr-123', session_key: 'hook:review:pr-123', name: 'Review Bot',
    outcome: 'completed', started_at: NOW - 480, duration_ms: 41200, result_chars: 3172,
    token_id: 'wht_7f3a91', delivered: true, detail: 'Delivered to notifications + Slack DM',
  },
  {
    id: 'run_a2', hook_id: 'deploy:prod-4471', session_key: 'hook:deploy:prod-4471', name: 'Deploy',
    outcome: 'timeout', started_at: NOW - 3060, duration_ms: 599000, result_chars: 7980,
    token_id: 'wht_7f3a91', delivered: true, detail: 'Hit the 599s ceiling; partial output kept',
  },
  {
    id: 'run_a3', hook_id: 'ci:build-88', session_key: 'hook:ci:build-88', name: 'CI callback',
    outcome: 'error', started_at: NOW - 10800, duration_ms: 12400, result_chars: 640,
    token_id: 'wht_ad2be9', delivered: false, detail: 'Runtime error in turn — tool exited 1',
  },
  {
    id: 'run_a4', hook_id: 'review:pr-119', session_key: 'hook:review:pr-119', name: 'Review Bot',
    outcome: 'rejected_capacity', started_at: NOW - 18000, duration_ms: 0, result_chars: 0,
    token_id: 'wht_7f3a91', delivered: false, detail: 'Rejected — 6 runs already in flight',
  },
  {
    id: 'run_a5', hook_id: null, session_key: null, name: 'unknown',
    outcome: 'unauthorized', started_at: NOW - 32400, duration_ms: 0, result_chars: 0,
    token_id: null, delivered: false, detail: 'Bad bearer token from 10.0.4.19',
  },
]

const SCENES: Record<string, Record<string, unknown>> = {
  enabled: {
    enabled: true, switch_on: true, has_tokens: true, url: URL_,
    slots: { in_use: 2, max: 6 }, limits: LIMITS,
    tokens: TOKENS, contexts: CONTEXTS, runs: RUNS,
  },
  empty: {
    enabled: false, switch_on: true, has_tokens: false, url: URL_,
    slots: { in_use: 0, max: 6 }, limits: LIMITS,
    tokens: [], contexts: [], runs: [],
  },
  off: {
    enabled: false, switch_on: false, has_tokens: true, url: URL_,
    slots: { in_use: 0, max: 6 }, limits: LIMITS,
    tokens: TOKENS, contexts: CONTEXTS,
    runs: [
      {
        id: 'run_off', hook_id: 'review:pr-123', session_key: 'hook:review:pr-123',
        name: 'Rejected', outcome: 'disabled', started_at: NOW - 120, duration_ms: 0,
        result_chars: 0, token_id: null, delivered: false,
        detail: 'Inbound webhooks are switched off in the dashboard',
      },
      ...RUNS,
    ],
  },
}

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'enabled'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
qc.setQueryData(['webhooks'], SCENES[scene] ?? SCENES.enabled)

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {/* Mirror the real shell's width contract: App wraps pages in a
            `flex-1 min-w-0` container, so a page can never exceed the viewport.
            Without `width`/`minWidth` here the harness let flex children grow
            past it, which made narrow-viewport captures show overflow the real
            app does not have. */}
        <div
          style={{
            background: 'var(--bg)',
            color: 'var(--text)',
            height: '100vh',
            width: '100vw',
            minWidth: 0,
            overflow: 'hidden',
            display: 'flex',
          }}
        >
          <WebhooksPage />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
