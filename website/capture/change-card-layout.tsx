/**
 * Isolated capture entry for the shared change card layout: the header row
 * with its expiry, the reason drawn as a grid row and labelled with its proposer, an add shown without
 * "(empty) →", an edit kept as old → new, an Affects row shown only
 * when it names more than the title, and the decline beside the accept.
 *
 * WHY ISOLATED: a pending card exists only while an agent's turn has just
 * proposed it, which no capture run has. This mounts the REAL ChangeCard with
 * the real stylesheet, theme tokens and live i18n catalog.
 *
 * Scene comes from the query string: ?scene=goal|edit|shared&theme=dark|light&width=720|360
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
const scene = params.get('scene') || 'goal'
const width = Number(params.get('width') || 720)

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const now = Date.now()
const base = {
  id: 'card-capture-1', slot_key: 'chat-capture', revision: 1, status: 'pending' as const,
  risk: 'normal' as const, editable: [], created_at: now - 60_000, expires_at: now + 22 * 3600_000 + 30 * 60_000,
}
const goal: Card = {
  ...base, kind: 'crewmate.goal',
  title: 'Add a goal for Pebble',
  reason: 'You told me you want the beta in front of five teams before the end of the month.',
  params: { slug: 'pebble', long_term: 'Get the beta in front of five teams this month.' },
  changes: [
    { field: 'goal', label: 'Goal 1', number: 1, before: null, after: 'Get the beta in front of five teams this month.' },
  ],
  scope: { count: 1, members: ['Pebble'], only_this_member: true },
  plan: { apply: [{ method: 'POST', path: '/api/members/pebble/goals', body: { long_term: 'Get the beta in front of five teams this month.' } }], undo: null },
}
const edit: Card = {
  ...base, kind: 'setting.change',
  title: 'Shorter replies',
  reason: 'You asked me to keep my answers short.',
  params: { path: 'chat.verbosity', value: 'brief' },
  changes: [{ label: 'Reply length', before: 'standard', after: 'brief' }],
  plan: {
    apply: [{ method: 'PATCH', path: '/api/config/kirocrew', body: { path: 'chat.verbosity', value: 'brief' } }],
    undo: [{ method: 'PATCH', path: '/api/config/kirocrew', body: { path: 'chat.verbosity', value: 'standard' } }],
  },
}
// A scope the title does not cover: Affects stays.
const shared: Card = { ...goal, title: 'Add a goal for Pebble', scope: { count: 2, members: ['Pebble', 'Scout'] } }
const card = { goal, edit, shared }[scene as 'goal' | 'edit' | 'shared'] ?? goal

initI18n('en')
const queryClient = new QueryClient()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
          {/* A goal card is proposed by the crewmate Pebble; the setting card by the main assistant. */}
          <CardAuthorContext.Provider value={card.kind === 'crewmate.goal' ? 'Pebble' : CARD_AUTHOR_DEFAULT}>
            <ChangeCard card={card} store={() => {}} refresh={() => {}} remove={() => {}} />
          </CardAuthorContext.Provider>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
