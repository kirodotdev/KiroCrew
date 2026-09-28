/**
 * Evidence for the feature-video cache row in Settings ▸ Chat.
 *
 * WHY ISOLATED: reaching `/settings?tab=chat` through the full SPA needs a live
 * gateway plus a dashboard credential, and without one the shell renders its
 * prerequisite gate instead of the panel — worse evidence than none. This mounts
 * the REAL `ChatPanel` against the REAL stylesheet, theme tokens and live i18n
 * catalog, with the panel's own queries seeded, so the row, its wording and its
 * control are the shipped ones.
 *
 * Scenes, selected with `?scene=`:
 *
 *   ?scene=cached      part of the release is on disk and downloads are permitted:
 *                      the counts, the release, and the manual control.
 *   ?scene=downloading a clip is being fetched right now. The line names it and
 *                      the control is unavailable, because pressing it again
 *                      would queue the same pass twice.
 *   ?scene=policy-off  downloads are forbidden. The line says so and there is NO
 *                      control in any state — the "hidden, not greyed" claim.
 *
 * `?theme=` picks the palette (default `kiro-dark`).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { ChatPanel } from '../src/pages/settings/ChatPanel'
import { store } from '../src/store'
import { initI18n } from '../src/i18n'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'cached'

document.documentElement.setAttribute('data-theme', params.get('theme') || 'kiro-dark')
applyFallbackTheme()
initI18n('en')

/** The three readings the row can show, as the status route reports them. */
const STATUS = {
  cached: {
    enabled: true, download_enabled: true, release: '2026.09.1',
    cached: 2, total: 3, downloading: null,
  },
  downloading: {
    enabled: true, download_enabled: true, release: '2026.09.1',
    cached: 1, total: 3, downloading: 'monitor-loops',
  },
  'policy-off': {
    enabled: true, download_enabled: false, release: '2026.09.1',
    cached: 3, total: 3, downloading: null,
  },
}[scene] ?? null

/**
 * Transport-only stub, keyed by path.
 *
 * The panel still goes through `api` and react-query; this only gives those
 * requests something to resolve against. Answering by PATH rather than seeding
 * query keys is what keeps the harness from breaking every time the panel adds a
 * read: an unknown `/api/...` gets an empty object rather than a hang.
 */
const realFetch = window.fetch.bind(window)
const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), {
  status: 200, headers: { 'Content-Type': 'application/json' },
}))
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/feature-videos/status')) return json(STATUS)
  if (url.includes('/api/feature-videos/fetch-all')) return json({ ok: true })
  if (url.includes('/api/dashboard/config')) {
    return json({
      restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false,
      default_memory_mode: 'persistent', widget_density: 'more', verbosity: 'default',
      quick_send: false, session_grid: false, tail_fork_enabled: false, link_previews: false,
      mcp_app_panel: false, auto_open_git_panel: false, session_card_source_links: true,
      folder_suggestions_enabled: true, use_builtin_browser: true,
    })
  }
  if (url.includes('/api/tips/status')) return json({ enabled_config: true, opted_out: false })
  if (url.includes('/api/models')) return json([{ model_name: 'auto', description: 'Default' }])
  if (url.includes('/api/config')) {
    return json({ agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' } })
  }
  if (url.startsWith('/api/')) return json({})
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        {/* Width mirrors the settings content column, so wrapping matches production. */}
        <div data-capture-root className="bg-bg text-text min-h-screen p-8">
          <div className="max-w-[760px]">
            <ChatPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
