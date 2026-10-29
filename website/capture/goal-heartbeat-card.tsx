/**
 * Isolated capture entry for a crewmate's goal card with its Heartbeat checkbox.
 *
 * WHY ISOLATED: a pending goal card exists only while a mate's turn has just
 * proposed it. This mounts the REAL ChangeCard against the real stylesheet,
 * theme tokens and live i18n catalog, fed the card `build_preview` returns for
 * a `crewmate.goal` proposal while the mate's Heartbeat is off.
 *
 * Scene comes from the query string:
 *   ?scene=checked|unchecked|none&theme=dark|light&lang=en|zh-CN
 * `none` is the card once the Heartbeat is on (or was declined): no checkbox.
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
const scene = params.get('scene') || 'checked'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const goalRow = { label: 'Goal 1', field: 'goal', number: 1, before: null, after: 'Keep the release notes current for every weekly build' }
const offer = (checked: boolean) => ({
  label: 'Heartbeat (each run uses credits)', field: 'heartbeat_offer', after: checked, interval: 60, per_day: 24,
})
const now = Date.now()
const card: Card = {
  id: 'card-capture-goal', slot_key: 'chat-capture', kind: 'crewmate.goal', revision: 1, status: 'pending',
  risk: 'normal', title: 'Add a goal for “Scout”',
  changes: scene === 'none' ? [goalRow] : [goalRow, offer(scene === 'checked')],
  scope: { members: ['Scout'], count: 1 },
  editable: ['long_term', 'heartbeat'],
  params: { member: 'scout', long_term: goalRow.after, ...(scene === 'unchecked' ? { heartbeat: false } : {}) },
  plan: { apply: [], undo: null },
  reason: 'You asked me to own the release notes, so this is what I will work toward.',
  created_at: now - 60_000, expires_at: now + 23 * 3600_000,
}

initI18n(lang)
const queryClient = new QueryClient()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width: 560, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
          <ChangeCard card={card} store={() => {}} refresh={() => {}} remove={() => {}} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
