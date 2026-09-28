/**
 * Evidence for the chat error-state sweep, batch chat-1 (ErrorNotice migration).
 *
 * THE CHANGE: hand-written error surfaces in `website/src/pages/chat/**` — a
 * danger-tinted button label, a bare `text-danger` div, a hand-built
 * ring-danger box, a tooltip-only failure — now render through the shared
 * `ErrorNotice`, with the "Ask the agent" hand-off on where the surface holds
 * no draft.
 *
 * Scenes mount the REAL components against the real stylesheet, theme tokens
 * and live i18n catalog. Only `fetch` is stubbed (to reject), which is exactly
 * the failure the notice exists for. Nothing here re-implements a notice or a
 * string, so a frame proves what ships. The same harness renders the base
 * branch's markup when run against it (every prop it passes exists there),
 * which is how the "before" frames are produced.
 *
 *   ?scene=sheet        the four migrated surfaces stacked (default)
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import EarlierMessagesBar from '../src/pages/chat/EarlierMessagesBar'
import FolderPanel from '../src/pages/chat/FolderPanel'
import McpOAuthBanner from '../src/pages/chat/McpOAuthBanner'
import McpToolsPanel from '../src/pages/chat/McpToolsPanel'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

// Every API call fails: that IS the state under test. A rejected fetch is what
// useQuery surfaces as `error`, which is what the migrated notices render.
window.fetch = () =>
  Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))

// Retries would keep the panels in `isLoading` for the whole capture window;
// the settled error state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Scene({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section data-scene={label} className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-3 py-1.5 text-[11px] uppercase tracking-wider text-muted border-b border-border">{label}</div>
      <div className="p-3">{children}</div>
    </section>
  )
}

const root = createRoot(document.getElementById('root')!)
root.render(
  <Provider store={store}>
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <div
          data-capture-root
          className="flex flex-col gap-3"
          style={{ maxWidth: 720, margin: '0 auto', padding: 20, background: 'var(--bg)', color: 'var(--text)' }}
        >
          <Scene label="EarlierMessagesBar — history page request rejected">
            <EarlierMessagesBar loading={false} failed onLoad={() => {}} />
          </Scene>
          <Scene label="McpOAuthBanner — OAuth exchange failed">
            <McpOAuthBanner serverName="linear" oauthUrl="" completed={false} failed error="token endpoint returned 502" />
          </Scene>
          <Scene label="McpToolsPanel — server failed to start in this session">
            <McpToolsPanel
              servers={[{ name: 'github' }, { name: 'jira' }]}
              toolsByServer={{ github: { tools: ['search_issues'] }, jira: { tools: [] } }}
              loaded={new Set(['github:search_issues'])}
              toolSearchOn
              loading={false}
              sessionReport={{
                configured: ['github', 'jira'],
                ready: ['github'],
                failed: ['jira'],
                awaiting_auth: [],
                failures: { jira: 'spawn ENOENT: uvx not found on PATH' },
              }}
            />
          </Scene>
          <Scene label="FolderPanel — directory listing rejected">
            <div style={{ height: 220 }}>
              <FolderPanel path="/Users/me/ws" onClose={() => {}} />
            </div>
          </Scene>
        </div>
      </QueryClientProvider>
    </MemoryRouter>
  </Provider>,
)
