/**
 * Evidence for the Settings > Display credit-meter fallback toggle (#7627).
 *
 * THE CHANGE: `dashboard.usage_text_scrape_enabled` is now in the config PATCH
 * allowlist, and the View card carries a `SettingsToggle` for it plus the
 * panel's inline `ErrorNotice` for a rejected save.
 *
 * The scene mounts the REAL `DisplayPanel` from `src/` against the real
 * stylesheet, theme tokens and live i18n catalog, with only `fetch` stubbed to
 * answer what the gateway answers. Nothing here re-implements the row or any
 * string, so a frame proves what ships.
 *
 *   ?scene=off     a config that has never carried the key -- the off default
 *   ?scene=on      the key stored as a real boolean true
 *   ?scene=reject  PATCH refused, so a click renders the rollback + error line
 *   ?scene=forbidden  PATCH refused 403 owner_only -- the permission message
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { UIModeProvider } from '../src/hooks/useUIMode'
import { ZoomProvider } from '../src/hooks/ZoomProvider'
import { DisplayPanel } from '../src/pages/settings/DisplayPanel'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = (params.get('scene') || 'off') as 'off' | 'on' | 'reject' | 'forbidden'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/**
 * What GET /api/config/kirocrew returns per scene. `off` omits the key
 * entirely, which is the state every install starts in: the row must read OFF
 * from an absent key, not from a stored `false`.
 */
const DASHBOARD = {
  off: { recent_tint_count: 3, terminal: { shell: '' } },
  on: { recent_tint_count: 3, terminal: { shell: '' }, usage_text_scrape_enabled: true },
  reject: { recent_tint_count: 3, terminal: { shell: '' } },
  forbidden: { recent_tint_count: 3, terminal: { shell: '' } },
}[scene]

globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()
  if (url.includes('/api/config/kirocrew')) {
    if (method === 'PATCH') {
      // The rejection this row's error line is for: the gateway refused the write.
      if (scene === 'reject') return Promise.resolve(json({ error: 'field not editable' }, 400))
      // Enabling is owner-only, so a non-owner caller is refused with the
      // standard code rather than a transient failure.
      if (scene === 'forbidden') {
        return Promise.resolve(
          json({ error: 'owner authorization required', code: 'owner_only' }, 403),
        )
      }
      return Promise.resolve(json({ ok: true }))
    }
    return Promise.resolve(json({ dashboard: DASHBOARD }))
  }
  return Promise.resolve(json({}, 404))
}) as typeof fetch

// Retries would keep the panel in its skeleton for the whole capture window;
// the settled state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

const root = createRoot(document.getElementById('root')!)
root.render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ThemeProvider>
          <UIModeProvider>
            <ZoomProvider>
              <div
                data-capture-root
                style={{ maxWidth: 860, margin: '0 auto', padding: 24, background: 'var(--bg)', minHeight: 360 }}
              >
                <DisplayPanel />
              </div>
            </ZoomProvider>
          </UIModeProvider>
        </ThemeProvider>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
