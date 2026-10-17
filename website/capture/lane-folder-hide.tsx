/**
 * Isolated capture entry for the folder hide across every sidebar lane.
 *
 * WHY ISOLATED: the subject is what a lane does NOT draw. On a live gateway the frame
 * depends on that gateway's own sessions and folders, so the one row whose absence is
 * the evidence would differ per machine and per hour. Here the population is three
 * fixed sessions in two fixed folders, so "Hidden Conductor is gone" is a statement
 * about the lane rather than about whoever's box took the picture.
 *
 * Nothing about the hide is stubbed: the checkbox state is seeded in the same
 * localStorage key the filter menu writes, the folders arrive over the same
 * `/api/chat/folders` read, and the REAL `ChatSidebar` decides every row.
 *
 * Query string: ?lane=tree|flat|conductor|board&hide=1|0&theme=dark|light
 *   lane=board is not a `SidebarLane`: it is the separate columns axis that preempts
 *   the union, so it is selected by turning tag columns on rather than by the pref.
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected, sseSlots } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lane = params.get('lane') || 'conductor'
/** Which folder the person unchecked: a folder id, a comma-separated list of them for a
 *  plural frame, or empty for none. */
const hidden = params.get('hide') || ''
const hiddenIds = hidden.split(',').map(s => s.trim()).filter(Boolean)
const light = params.get('theme') === 'light'

/** The ids the fixture folders carry; `HIDDEN` is the one the person unchecks. */
const HIDDEN = 'folder-hidden'
const SHOWN = 'folder-shown'
/** Nested under SHOWN, so a frame can show a hide one level down. */
const NESTED = 'folder-nested'

// ThemeProvider reads these and writes `data-theme` itself, so setting the attribute
// alone is overridden on mount. Both keys are pinned: the colour theme otherwise
// drifts with whatever the machine's gateway last stored.
localStorage.setItem('mc-theme', light ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
// The lane opens directly. The two board renderers are reached through the columns
// switch instead, because columns preempt the union's choice whenever any exist: `board`
// draws folder blocks per column, `board-flat` draws the column's rows with no blocks.
const boardish = lane === 'board' || lane === 'board-flat'
localStorage.setItem('mc-sidebar-lane', lane === 'board' ? 'tree' : lane === 'board-flat' ? 'flat' : lane)
localStorage.setItem('mc-chat-config', JSON.stringify({ tagColumnsEnabled: boardish }))
// The person's folder checkboxes, in the key the filter menu persists them to. This IS
// the input under test, so it is seeded rather than clicked: a click would also have to
// leave the menu open, and an open popover covers the rows the frame is of.
localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(hiddenIds))
// Settled rows would otherwise fold behind a "stale" expander, and a reader could not
// tell that from a row the hide removed -- which is the whole question.
localStorage.setItem('mc-session-stale-collapse-ms', '0')
// Wide enough that no title is truncated: an ellipsis over "Hidden Conductor" would
// make the before frame unreadable as the control it is.
localStorage.setItem('mc-sidebar-width', '520')

const now = Date.now()
/** `last_ts`, not `modified`: the row's timestamp field is an ISO string, and a row
 *  carrying none sorts to the epoch, where the tree and flat lanes file it in their
 *  collapsed older group -- in the DOM, and invisible in a photograph. */
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

/**
 * A conductor INSIDE the hidden folder whose child sits outside it.
 *
 * This is the shape that reaches the conductor lane's anchor rule: the child is not
 * hidden, so the lane wants the parent it hangs from, and that parent is exactly the
 * row the person said not to show. The fourth row sits in a folder NESTED under a
 * visible one, which is the hide a root-only filter misses.
 */
const SLOTS = [
  {
    key: 'k-hidden-conductor', title: 'Hidden Conductor', agent: 'kirocrew',
    running: false, messages: 6, last_ts: at(40_000), folder_id: HIDDEN,
    last_message: 'Both workers reported in.',
  },
  {
    key: 'k-shown-child', title: 'Shown Child', agent: 'kirocrew-worker',
    running: false, messages: 4, last_ts: at(120_000), folder_id: SHOWN,
    parent: { slot: 'k-hidden-conductor', key: 'k-hidden-conductor' },
    last_message: 'Pin covers the lane it names.',
  },
  {
    key: 'k-shown-plain', title: 'Shown Plain', agent: 'kirocrew',
    running: false, messages: 9, last_ts: at(300_000), folder_id: SHOWN,
    last_message: 'Nothing owed here.',
  },
  {
    key: 'k-nested', title: 'Nested Session', agent: 'kirocrew',
    running: false, messages: 3, last_ts: at(420_000), folder_id: NESTED,
    last_message: 'One level down from a visible folder.',
  },
] as unknown as ChatSlot[]

function Harness() {
  useEffect(() => {
    // Without a connected socket every row takes the disabled branch (opacity-50, no
    // hover affordance), so the frames would show a dead sidebar instead of one
    // someone is working in.
    store.dispatch(sseConnected())
    // The board lane reads its population from the store rather than from the prop,
    // so the fixture is published there too. After the connect, which clears the
    // loaded flag the board's own ordering waits on.
    store.dispatch(sseSlots(SLOTS))
  }, [])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={SLOTS}
        activeSlot={null}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent="kirocrew"
        installedAgents={[
          { name: 'kirocrew', source: 'builtin' },
          { name: 'kirocrew-worker', source: 'builtin' },
        ]}
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
