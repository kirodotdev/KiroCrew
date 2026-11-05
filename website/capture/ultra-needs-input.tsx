/**
 * Capture page: the Ultra Mode Board's Status view with work that waits on the person.
 *
 * One ledger item whose worker asked the person (`question`, `needs_human`), with the
 * conductor's offered answers (`asks`), one item running and one tool approval. The
 * box under the panel stands in for the main chat's composer: a Reply button fills
 * it. `?theme=light` for the light palette. The panel is the real component; only the
 * crew list, the board and the summary reads are fixtures.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import { sseSlots } from '../src/store/dashboardSlice'
import UltraCrewPanel from '../src/pages/chat/command-center/UltraCrewPanel'
import { PREVIEW_CREW_MODE } from '../src/utils/previewFlags'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')
localStorage.setItem(PREVIEW_CREW_MODE, '1')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const ITEM = (over: Record<string, unknown>) => ({
  schema: 1, item_id: 'it_1', title: '', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: ago(4),
  created_at: ago(30), closed_at: null, decided_at: null, orphaned: false, stale: false, acceptance_concrete: true,
  outstanding: false, terminal: false, alive: 'idle', events: [], ...over,
})
const BOARD = {
  conductor: { schema: 1, slot_key: 'root', goal: 'Plan a small tea shop launch.', round: 1, depth: 0, parent_item: null, created_at: ago(40) },
  conductor_alive: 'idle',
  take_over_available: false,
  items: [
    ITEM({ item_id: 'it_tea', title: 'Drinks menu', status: 'question', reason: 'needs_human', awaits_person: true, outstanding: true, summary: 'Which tea leads the menu: oolong or green? Both fit the budget.' }),
    ITEM({ item_id: 'it_poster', title: 'Opening poster', alive: 'running', summary: 'Drafting the poster text', created_at: ago(20) }),
    ITEM({ item_id: 'it_price', title: 'Price list', created_at: ago(10), summary: 'Saving the price list file', last_report_at: ago(2) }),
  ],
  asks: { it_tea: ['Oolong', 'Green', 'Both, oolong first'] },
}
const WORKERS = [
  { key: 'w-tea', title: 'Drinks menu', agent: 'kirocrew-worker', status: 'idle', group: 'with_conductor', last_activity: ago(4), ledger_item: 'it_tea', created_by: 'root' },
  { key: 'w-poster', title: 'Opening poster', agent: 'kirocrew-worker', status: 'working', group: 'working', last_activity: ago(1), ledger_item: 'it_poster', created_by: 'root' },
  { key: 'w-price', title: 'Price list', agent: 'kirocrew-worker', status: 'needs_approval', group: 'approval', last_activity: ago(2), ledger_item: 'it_price', created_by: 'root' },
]
Object.assign(api, {
  crewWorkers: async () => ({ root: 'root', workers: WORKERS }),
  crewBoard: async () => BOARD,
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
})
if (params.get('approval') !== '0') {
  store.dispatch(sseSlots([
    { key: 'root', title: 'Tea shop launch', messages: 3, crew_mode_root: true, agent: 'kirocrew-conductor', agent_kind: 'template' },
    {
      key: 'w-price', title: 'Price list', messages: 2, crew_root: 'root', created_by: 'root', pending_approval: true,
      pending_approval_info: { origin: 'native', tool: 'fs_write', request_id: 'req-1', request_mid: 'mid-1', tool_purpose: 'Save the price list', tool_input: '{"path": "/tmp/prices.md"}' },
    },
  ] as never))
}

function Frame() {
  const [draft, setDraft] = useState('')
  return (
    <div style={{ width: 380 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
      <div style={{ height: 640 }}><UltraCrewPanel root="root" viewing={params.get('viewing') || 'root'} active onAct={setDraft} /></div>
      <div className="border-t border-border p-2">
        <textarea data-testid="capture-composer" readOnly value={draft} placeholder="Main chat composer" rows={2}
          className="w-full rounded-md border border-border bg-bg-elevated p-2 text-[13px]" />
      </div>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n(params.get('lang') === 'zh-CN' ? 'zh-CN' : 'en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}><Frame /></QueryClientProvider>
  </Provider>,
)
