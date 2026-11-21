/**
 * Isolated capture entry for the `mcp.server` change card: an agent's
 * `request_server` ask to add, change or remove an MCP server, with its
 * Use in open chats now option.
 *
 * WHY ISOLATED: a pending card exists only while an agent's turn has just
 * proposed it, which no capture run has. This mounts the REAL ChangeCard with
 * the real stylesheet, theme tokens and live i18n catalog; the card data is
 * mocked in the shape the gateway sends.
 *
 * Scene from the query string: ?scene=add|folder|unshared|change|command|remove&theme=dark|light&width=720|360
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ChangeCard from '../src/cards/ChangeCard'
import type { Card } from '../src/api/cards'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'add'
const width = Number(params.get('width') || 720)

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const now = Date.now()
const entry = { command: 'npx', args: ['-y', '@acme/weather-mcp'], env: { CITY: 'Paris' } }
const base = {
  id: 'card-capture-mcp', slot_key: 'chat-capture', revision: 1, status: 'pending' as const,
  editable: ['use_now'], created_at: now - 60_000, expires_at: now + 22 * 3600_000 + 30 * 60_000,
  scope: null,
}
const offer = (after: boolean) => ({ field: 'share_offer', label: 'Sharing', after })
const add: Card = {
  ...base, kind: 'mcp.server', risk: 'code_exec',
  title: 'Add server: weather',
  reason: 'You asked for the weather in Paris, and I have no weather tool.',
  params: { action: 'add', server: 'weather', entry, use_now: true },
  changes: [
    { label: 'Command', after: 'npx -y @acme/weather-mcp' },
    { label: 'Environment', add: ['CITY=Paris'] },
    offer(true),
  ],
  plan: { apply: [{ method: 'POST', path: '/api/mcp/custom', body: {} }, { method: 'POST', path: '/api/sessions/restart', body: {} }], undo: null },
}
const folder: Card = {
  ...add,
  title: 'Add server: files',
  reason: 'You asked me to search the files in this project.',
  params: { action: 'add', server: 'files', entry: { command: 'npx', args: ['-y', '@acme/files-mcp', '.'] }, use_now: true, uses_chat_folder: true },
  changes: [{ label: 'Command', after: 'npx -y @acme/files-mcp .' }],
}
const unshared: Card = { ...add, params: { ...add.params, share: false, use_now: false }, changes: [...add.changes.slice(0, 2), offer(false)] }
const change: Card = {
  ...add,
  title: 'Change server: weather',
  reason: 'You want the temperature in Fahrenheit.',
  params: { action: 'change', server: 'weather', entry: { ...entry, env: { CITY: 'Paris', UNITS: 'imperial' } }, use_now: true },
  changes: [
    { label: 'Environment', add: ['UNITS=imperial'], remove: ['UNITS=metric'] },
    offer(true),
  ],
}
const command: Card = {
  ...add,
  title: 'Change server: weather',
  reason: 'The weather server moved to version 2.',
  params: { action: 'change', server: 'weather', entry: { ...entry, args: ['-y', '@acme/weather-mcp@2'] }, use_now: true },
  changes: [
    { label: 'Command', before: 'npx -y @acme/weather-mcp', after: 'npx -y @acme/weather-mcp@2' },
    offer(true),
  ],
}
const remove: Card = {
  ...add, risk: 'tighten',
  title: 'Remove server: weather',
  reason: 'You said you no longer need the weather tool.',
  params: { action: 'remove', server: 'weather', use_now: true },
  changes: [
    { label: 'Command', remove: ['npx -y @acme/weather-mcp'] },
    { label: 'Environment', remove: ['CITY=Paris'] },
  ],
}
const scenes = { add, folder, unshared, change, command, remove }
const card = scenes[scene as keyof typeof scenes] ?? add

initI18n('en')
const queryClient = new QueryClient()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
          <ChangeCard card={card} store={() => {}} refresh={() => {}} remove={() => {}} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
