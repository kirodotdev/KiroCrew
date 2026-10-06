/**
 * Isolated capture entry for the close-tree confirm in the sidebar's conductor lane.
 *
 * Query string also selects WHICH of the three cases is on screen:
 *   ?finished=1       every session has finished, the lead included, so the
 *                     press is the non-destructive one
 *   ?leadonly=1       every worker has finished but the lead is still mid-turn,
 *                     so the press refuses for the lead alone
 *   ?confirmclose=1   `Settings > Chat > confirm before closing a session` on,
 *                     which is the only way the finished-subtree prompt appears
 *
 * WHY ISOLATED: the subject is one press on the ✕ of a card that has WORKERS STILL
 * RUNNING under it. On a live gateway that state arrives when some conductor happens
 * to have workers mid-turn, which is not a recordable schedule, and photographing it
 * there would mean actually ending somebody's work to get the frame.
 *
 * What stays faithful is everything the dialog reads. The rows carry the same
 * `parent` edge the backend fold puts on the wire and the same `running` flag, and
 * they are delivered as ordinary slots to the REAL `ChatSidebar`. Nothing stubs the
 * nesting, the plan, or the dialog: the harness supplies the rows and presses the ✕,
 * and the component decides what the prompt says.
 *
 * Query string: ?theme=dark|light
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store, useAppSelector } from '../src/store'
import { sseConnected, sseSlots } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
/** Every worker under the lead has finished: the press that closes silently. */
const finished = params.get('finished') === '1'
/** Only the lead is still running: the press refuses for the lead itself. */
const leadOnly = params.get('leadonly') === '1'
// Preference: seed it the way the product stores it so the preference-on path
// shows the in-app confirm.
if (params.get('confirmclose') === '1') {
  localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
} else {
  localStorage.removeItem('mc-chat-config')
}
// ThemeProvider is the authority: it reads these keys and writes `data-theme`
// itself, so setting the attribute alone is overridden on mount.
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
// The conductor lane, opened directly and fully expanded: the subject is a press on a
// card whose subtree the person can see, so the tree is on screen before the press.
localStorage.setItem('mc-sidebar-lane', 'conductor')
localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['chat-lead', 'chat-babysit']))
// No stale folding — every row in the tree has to stay visible.
localStorage.setItem('mc-session-stale-collapse-ms', '0')
localStorage.setItem('mc-sidebar-width', '520')

const MIN = 60_000
const now = Date.now()
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

/** A row as the slots broadcast sends it; `parent` lives on the sidebar's own
 *  internal `Slot`, which is deliberately not exported, so the field set this
 *  harness uses is typed here and cast once where it reaches the component. */
interface Row {
  key: string
  title: string
  messages: number
  running: boolean
  agent: string
  last_ts: string
  last_message: string
  parent?: { slot?: string; key?: string | null } | null
  history_key?: string
  subagents_running?: boolean
}

type RowSeed = Partial<Row> & Pick<Row, 'key' | 'title' | 'last_ts' | 'last_message'>
// `history_key` is the transcript stem the server serializes for a dashboard slot.
const row = (over: RowSeed): Row => ({ messages: 12, running: false, agent: 'kirocrew', history_key: `dashboard_${over.key}`, ...over })

const LEAD = 'chat-lead'
const BABYSIT = 'chat-babysit'

/** A lead over five sessions, three of them mid-turn: the destructive case. */
const ROWS: Row[] = [
  row({
    key: LEAD,
    title: 'Conductor: dashboard lane',
    running: true,
    last_ts: at(20_000),
    last_message: 'Three workers reporting.',
  }),
  row({
    key: BABYSIT,
    title: 'worker: pull-request babysit',
    agent: 'kirocrew-worker',
    running: true,
    last_ts: at(15_000),
    last_message: 'Waiting on the review lane.',
    parent: { slot: LEAD, key: LEAD },
  }),
  row({
    key: 'chat-rerun',
    title: 'worker: rerun the flaky lane',
    agent: 'kirocrew-worker',
    running: true,
    last_ts: at(8_000),
    last_message: 'Re-running shard two.',
    parent: { slot: BABYSIT, key: BABYSIT },
  }),
  row({
    key: 'chat-logs',
    title: 'worker: fetch the job logs',
    agent: 'kirocrew-worker',
    last_ts: at(12 * MIN),
    last_message: 'Logs attached, nothing left to do.',
    parent: { slot: BABYSIT, key: BABYSIT },
  }),
  row({
    key: 'chat-locale',
    title: 'worker: locale sweep',
    agent: 'kirocrew-worker',
    last_ts: at(26 * MIN),
    last_message: 'All twelve catalogs in parity.',
    parent: { slot: LEAD, key: LEAD },
  }),
  row({
    key: 'chat-solo',
    title: 'Notes: release checklist',
    last_ts: at(50 * MIN),
    last_message: 'Nothing under this one.',
  }),
]

function Harness() {
  // Rows come from the STORE, not from local state, so the close path's own
  // optimistic removal is what takes a row off screen — the same way the live
  // dashboard loses a row. Feeding the component a fixed prop would photograph a
  // tree that cannot react to the thing being captured.
  const slots = useAppSelector(s => s.dashboard.slots)
  const [seeded, setSeeded] = useState(false)
  useEffect(() => {
    // Without this every row takes the `!connected` branch — opacity-50 and no
    // hover affordance — so the frame would show a disabled sidebar.
    store.dispatch(sseConnected())
    // `?finished=1` stands every session down, the lead included; `?leadonly=1`
    // leaves only the lead mid-turn. In the running case the locale worker's own
    // turn is idle while its subagents work, so the notice shows that badge.
    store.dispatch(sseSlots((finished
      ? ROWS.map(r => ({ ...r, running: false }))
      : leadOnly
        ? ROWS.map(r => (r.key === LEAD ? r : { ...r, running: false }))
        : ROWS.map(r => (r.key === 'chat-locale' ? { ...r, subagents_running: true } : r))) as unknown as ChatSlot[]))
    // The gateway follows a resume with a slots frame that carries each session's
    // `parent` edge again. The harness replays that frame after Undo, so the frame
    // shows the tree the person gets back, nested under its lead.
    ;(window as unknown as { __captureRestoreTree?: (skip?: string[]) => void }).__captureRestoreTree = (skip = []) => {
      store.dispatch(sseSlots(ROWS
        .filter(r => !skip.includes(r.key))
        .map(r => ({ ...r, running: false })) as unknown as ChatSlot[]))
    }
    setSeeded(true)
  }, [])
  if (!seeded) return null
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={slots}
        activeSlot={null}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent="kirocrew"
        installedAgents={[{ name: 'kirocrew', source: 'builtin' }, { name: 'kirocrew-worker', source: 'builtin' }]}
      />
    </div>
  )
}

initI18n()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
