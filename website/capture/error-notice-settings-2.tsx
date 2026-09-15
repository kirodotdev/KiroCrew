/**
 * Evidence for batch settings-2 of the ErrorNotice sweep (AUTOSDE
 * `errors-use-error-notice`).
 *
 * THE CHANGE: the settings panels' hand-written error surfaces (a warn-toned
 * `<p>` for a channel that failed to start, a `text-danger` span beside Save, a
 * silently-empty list when its read failed) now render through the shared
 * `ErrorNotice`, carrying the `askAgent` decision: ON where nothing can be lost
 * (a read failure on a card with no draft), OFF beside the unsaved token /
 * allow-list draft, with a `No hand-off` comment naming it.
 *
 * Scenes mount the REAL panels from `src/` against the real stylesheet, theme
 * tokens and live i18n catalog, with only `fetch` stubbed to answer what the
 * backend answers in each failure. Nothing here re-implements a notice or a
 * string, so a frame proves what ships. The same harness runs unchanged against
 * `origin/main` for the "before" frames.
 *
 *   ?scene=webex      connect_error at the header + a rejected save (PUT 500)
 *   ?scene=secrets    list read failure (GET 500) — previously rendered as an empty list
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { WebexPanel } from '../src/pages/settings/WebexPanel'
import { SecretsPanel } from '../src/pages/settings/SecretsPanel'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'secrets' ? 'secrets' : 'webex'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** What the gateway returns for a configured Webex bot whose websocket cannot reach the API. */
const WEBEX_CONFIG = {
  connected: false,
  connect_error: 'Network is unreachable (webexapis.com:443)',
  configured: true,
  read_only: false,
  bot_token_set: true,
  bot_token_preview: 'Y2lz…3Nz',
  enabled: true,
  allowed_emails: ['alex@example.com'],
  allow_group_rooms: false,
  allowed_room_ids: [],
  reply_in_thread: true,
  soft_threshold_pct: 80,
  hard_threshold_pct: 90,
  session_folder: '',
}

globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()
  if (url.includes('/api/webex/config')) {
    if (method === 'GET') return Promise.resolve(json(WEBEX_CONFIG))
    // The failed save this sweep is about: the token the user just pasted was
    // rejected, so the draft in the field is exactly what did NOT persist.
    return Promise.resolve(json({ error: 'Webex rejected the bot token: 401 Unauthorized' }, 500))
  }
  if (url.includes('/api/secrets')) {
    return Promise.resolve(json({ error: 'vault is locked: encryption key unavailable' }, 500))
  }
  return Promise.resolve(json({}, 404))
}) as typeof fetch

// Retries would keep the panel in its skeleton for the whole capture window;
// the settled error state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const root = createRoot(document.getElementById('root')!)
root.render(
  <QueryClientProvider client={qc}>
    <div
      data-capture-root
      style={{ maxWidth: 860, margin: '0 auto', padding: 24, background: 'var(--bg)', minHeight: 320 }}
    >
      {scene === 'secrets' ? <SecretsPanel /> : <WebexPanel />}
    </div>
  </QueryClientProvider>,
)
