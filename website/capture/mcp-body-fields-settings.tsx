/**
 * Isolated capture entry for Settings → Security → "Message body fields".
 *
 * Mounts the REAL `SecurityPanel` with the gateway reads seeded into the query
 * keys the panel reads, as `trusted-registries-settings.tsx` does.
 *
 * Scene + theme come from the query string: ?scene=refused&theme=dark
 *   refused — a field the gate refused for size, with "Allow this field from now on"
 *   allowed — the same field after the owner allowed it, with Remove
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { SecurityPanel } from '../src/pages/settings/SecurityPanel'
import type { McpBodyFieldsData } from '../src/api/client'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'refused'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const DEFAULTS = [
  { server: 'gmail', tool: 'send_email', field: 'body' },
  { server: 'gmail', tool: 'draft_email', field: 'body' },
  { server: 'outlook', tool: 'send_email', field: 'body' },
  { server: 'outlook', tool: 'draft_email', field: 'body' },
  { server: 'slack', tool: 'post_message', field: 'text' },
  { server: 'notion', tool: 'create_page', field: 'content' },
]
const ROW = { server: 'team-mail', tool: 'send_message', field: 'html_body' }
const DATA: McpBodyFieldsData = scene === 'allowed'
  ? { defaults: DEFAULTS, owner: [{ ...ROW, added_at: '2026-11-05T21:10:00+00:00' }], refused: [] }
  : { defaults: DEFAULTS, owner: [], refused: [{ ...ROW, refused_at: '2026-11-05T21:04:00+00:00' }] }

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})
qc.setQueryData(['mcp-body-fields'], DATA)
qc.setQueryData(['denied-commands'], {
  builtins: [{ id: 'sample-rule', pattern: 'sample', enabled: true }],
  user_added: [],
  disable_all: false,
  effective_count: 1,
  governance_locked: false,
})
qc.setQueryData(['kirocrewConfig'], { agent: { yolo_duration: '6h', apps_allow_third_party: false } })
qc.setQueryData(['tailnet-status'], {
  enabled: false,
  governance_pinned: false,
  host: '',
  origin: '',
  resolved_at: 0,
  state: 'off',
})

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings?tab=security&sub=bodyFields']}>
        <div className="bg-bg text-text min-h-screen p-8">
          <div className="max-w-[1080px]" data-capture-root>
            <SecurityPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
