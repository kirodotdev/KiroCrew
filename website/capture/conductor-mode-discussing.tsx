/**
 * Capture page: the conductor mode Board with a milestone whose worker hosts a
 * discussion. `?open=1` opens its panel, `?lang=zh-CN` (or any shipped locale)
 * picks the language, `?theme=light` the light palette, `?state=done` a finished
 * talk instead of a live one, `?w=320` and `?h=1200` the panel size (360 by 720 by default). The panel is the real
 * component; only the reads are fixtures.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import ConductorModeCrewPanel from '../src/pages/chat/command-center/ConductorModeCrewPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const item = (id: string, title: string, over: Record<string, unknown>) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'human_approval' }, state: 'open', verdict: null, decision: '', round: 1,
  fails: 0, status: 'progress', summary: 'Round 2 running.', artifacts: {}, pr: null, last_report_at: ago(3), created_at: ago(40),
  closed_at: null, decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false,
  alive: 'running', events: [], reason: null, awaits_person: false, ...over,
})
const discussion = {
  phase: 'talking', round: 2, minutes: 12, direction: 'Multiline box in the card, attach button beside it',
  agreed: ['One click sends text and files together', 'Offline state says so plainly'],
  disputed: [{ point: 'Where the attach button goes', sides: 'A: inside the card · B: reuse the chat box' }],
  checking: ['Paste size limit for images'],
  recent: [{ who: 'A', text: 'The card already owns its own input.' }, { who: 'B', text: 'Two inputs confuse people.' }],
  rounds: [
    { round: 1, lines: [{ who: 'A', text: 'The card already owns its own input.' }, { who: 'B', text: 'Reuse the chat box: one input only.' }, { who: 'C', text: 'Asks: what happens offline?' }] },
    { round: 2, lines: [{ who: 'A', text: 'Offline: say so plainly, keep the draft.' }, { who: 'B', text: 'Concedes the card box; disputes the attach button.' }, { who: 'C', text: 'No new point.' }] },
  ],
  thread: '/chat/worker-grill',
}
const done = params.get('state') === 'done'
const ITEMS = [
  item('a', 'Rename the chip', { state: 'accepted', verdict: 'pass', status: 'done', summary: 'Merged.', terminal: true, alive: 'closed', closed_at: ago(10) }),
  item('d', 'Pick the card input', done ? { discussion: { ...discussion, phase: 'done', round: 3 }, alive: 'idle', status: 'done' } : { discussion }),
  item('b', 'Update the docs', { summary: 'Editing the guide.' }),
]
const conductor = { schema: 1, slot_key: 'root', goal: 'Let people send rich input from the discussion card.', round: 1, depth: 0, parent_item: null, created_at: ago(60) }
Object.assign(api, {
  crewWorkers: async () => ({ root: 'root', workers: [] }),
  crewBoard: async () => ({ conductor, conductor_alive: 'idle', items: ITEMS, take_over_available: false }),
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n(params.get('lang') || 'en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div style={{ width: Number(params.get('w')) || 360, height: Number(params.get('h')) || 720 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
        <ConductorModeCrewPanel root="root" viewing="root" active />
      </div>
    </QueryClientProvider>
  </Provider>,
)
if (params.get('open') === '1') {
  const click = () => {
    const b = document.querySelector<HTMLButtonElement>('[data-testid="conductor-mode-talk-toggle"]')
    if (b) b.click()
    else setTimeout(click, 100)
  }
  click()
}
