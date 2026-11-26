/**
 * Isolated capture entry for the mcp.project_server card: a server that a folder's
 * own MCP file names, held back when the chat started. A new one shows the file,
 * the command and the env; a changed one shows the command before and after, and an
 * unchanged env as `unchanged`.
 *
 * A long one names a server at the 256-character limit, to show the button wraps.
 *
 * Scene comes from the query string: ?scene=new|changed|settings|long&theme=dark|light&width=720|360
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ChangeCard from '../src/cards/ChangeCard'
import { CardAuthorContext, CARD_AUTHOR_DEFAULT } from '../src/cards/cardAuthor'
import type { Card } from '../src/api/cards'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'new'
const width = Number(params.get('width') || 720)

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const now = Date.now()
const base = {
  id: 'card-capture-1', slot_key: 'chat-capture', revision: 1, status: 'pending' as const,
  risk: 'code_exec' as const, editable: [], created_at: now - 60_000, expires_at: now + 22 * 3600_000,
  kind: 'mcp.project_server' as const,
  params: { folder: '/home/me/src/shop', file: '.mcp.json', server: 'db-tools', fingerprint: 'a'.repeat(64) },
  plan: {
    apply: [
      { method: 'POST', path: '/api/mcp/project-servers/approve', body: {} },
    ],
    undo: null,
  },
}
// Same wording as the backend's _RESTART_NOTE in change_card_project_mcp.py.
const restart = (server: string) => `Allowing restarts every open chat so ${server} loads; history is kept.`
const RESTART = restart('db-tools')
const fresh: Card = {
  ...base,
  title: "Start db-tools from this folder's .mcp.json?",
  reason: "Held back: you haven't allowed this server in this folder yet.",
  changes: [
    { label: 'File', after: '/home/me/src/shop/.mcp.json' },
    { label: 'Runs', after: 'npx -y @acme/db-tools --readonly' },
    { label: 'Environment', add: ['DB_URL=postgres://localhost/shop'] },
    { label: 'Open chats', after: RESTART },
  ],
}
const changed: Card = {
  ...base,
  title: "db-tools changed in this folder's .mcp.json. Start it?",
  reason: 'Held back: its command changed since you allowed it here.',
  changes: [
    { label: 'File', after: '/home/me/src/shop/.mcp.json' },
    { label: 'Runs', before: 'npx -y @acme/db-tools --readonly', after: 'node ./scripts/db-tools.js' },
    { label: 'Environment', after: 'unchanged' },
    { label: 'Open chats', after: RESTART },
  ],
}
const settings: Card = {
  ...base,
  title: "db-tools changed in this folder's .mcp.json. Start it?",
  reason: 'Held back: its settings changed since you allowed it here.',
  changes: [
    { label: 'File', after: '/home/me/src/shop/.mcp.json' },
    { label: 'Environment', add: ['DB_URL=postgres://db.internal/shop'], remove: ['DB_READONLY'] },
    { label: 'Open chats', after: RESTART },
  ],
}
// MAX_SERVER_NAME_CHARS in project_mcp_approvals.py.
const longName = 'db-tools-' + 'x'.repeat(256 - 'db-tools-'.length)
const long: Card = {
  ...fresh,
  params: { ...base.params, server: longName },
  title: `Start ${longName} from this folder's .mcp.json?`,
  changes: [...fresh.changes.slice(0, -1), { label: 'Open chats', after: restart(longName) }],
}
const card = { new: fresh, changed, settings, long }[scene as 'new' | 'changed' | 'settings' | 'long'] ?? fresh

initI18n('en')
const queryClient = new QueryClient()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
          <CardAuthorContext.Provider value={CARD_AUTHOR_DEFAULT}>
            <ChangeCard card={card} store={() => {}} refresh={() => {}} remove={() => {}} />
          </CardAuthorContext.Provider>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
