/**
 * Capture page: the conductor mode Board's Status view in each run state.
 *
 * `?state=` picks the board: `working` (the patrol is still on), `waiting` (an item
 * asks the person), `done` (every milestone accepted), `done-some` (one rejected,
 * one abandoned), `stopped` (milestones open, no patrol) or `paused` (Pause all). `?theme=light` for the
 * light palette. The panel is the real component; only the reads are fixtures.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import ConductorModeCrewPanel from '../src/pages/chat/command-center/ConductorModeCrewPanel'
import { PREVIEW_CREW_MODE } from '../src/utils/previewFlags'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')
const state = params.get('state') || 'done'
// Pause all is drawn behind the conductor mode preview, as on a real gateway.
localStorage.setItem(PREVIEW_CREW_MODE, '1')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const item = (id: string, title: string, over: Record<string, unknown>) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'human_approval' }, state: 'accepted', verdict: 'pass', decision: '', round: 1,
  fails: 0, status: 'done', summary: 'Merged and released.', artifacts: {}, pr: null, last_report_at: ago(30), created_at: ago(90),
  closed_at: ago(20), decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: true,
  alive: 'closed', events: [], reason: null, awaits_person: false, ...over,
})
const open = { state: 'open', verdict: null, terminal: false, closed_at: null }
const ITEMS: Record<string, unknown[]> = {
  working: [item('a', 'Rename the chip', {}), item('b', 'Update the docs', { ...open, status: 'progress', summary: 'Editing the guide.', alive: 'running' })],
  waiting: [item('a', 'Rename the chip', {}), item('q', 'Pick the icon', { ...open, status: 'question', reason: 'needs_human', summary: 'Bolt or star?', alive: 'idle', awaits_person: true, outstanding: true })],
  done: [item('a', 'Rename the chip', {}), item('b', 'Update the docs', {}), item('c', 'Fix the tests', {})],
  'done-some': [item('a', 'Rename the chip', {}), item('b', 'Port the old gate', { state: 'rejected', verdict: 'fail', summary: 'Checks still red.' }), item('c', 'Try a new icon', { state: 'abandoned', verdict: null, summary: 'Dropped by the plan.' })],
  stopped: [item('a', 'Rename the chip', {}), item('b', 'Update the docs', { ...open, status: 'progress', summary: 'Half the pages done.', alive: 'idle' })],
  paused: [item('a', 'Rename the chip', {}), item('b', 'Update the docs', { ...open, status: 'progress', summary: 'Half the pages done.', alive: 'idle' })],
}
const RUN: Record<string, unknown> = {
  working: { settled: '', open: 1, accepted: 1, not_done: 0, at_limit: false },
  waiting: { settled: '', open: 1, accepted: 1, not_done: 0, at_limit: false },
  done: { settled: 'done', open: 0, accepted: 3, not_done: 0, at_limit: false },
  'done-some': { settled: 'done', open: 0, accepted: 1, not_done: 2, at_limit: false },
  stopped: { settled: 'stopped', open: 1, accepted: 1, not_done: 0, at_limit: false },
  paused: { settled: '', open: 1, accepted: 1, not_done: 0, at_limit: false },
}
const conductor = { schema: 1, slot_key: 'root', goal: 'Rename the mode in the app. Done when: every surface uses the new name.', round: 1, depth: 0, parent_item: null, created_at: ago(120) }
Object.assign(api, {
  crewWorkers: async () => ({ root: 'root', workers: [], paused: state === 'paused' ? { stopped: [], paused_at: ago(5) } : undefined }),
  crewBoard: async () => ({ conductor, conductor_alive: 'idle', items: ITEMS[state] ?? ITEMS.done, take_over_available: false, run: RUN[state] ?? RUN.done }),
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div style={{ width: 360, height: 640 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
        <ConductorModeCrewPanel root="root" viewing={params.get('viewing') || 'root'} active />
      </div>
    </QueryClientProvider>
  </Provider>,
)
