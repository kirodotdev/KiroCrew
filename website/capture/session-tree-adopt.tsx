/**
 * Isolated capture entry for ADOPT and RELEASE in the sidebar's conductor lane.
 *
 * WHY ISOLATED: the subject is a re-parenting that arrives on its own. `session_adopt`
 * is called by an agent, so on a live gateway the frame that moves a branch lands
 * whenever some conductor decides to take one over, which is not a recordable schedule.
 * Both verbs are also refused for the session that calls them in most shapes, so
 * driving them from the dashboard the recording is OF would need a second gateway.
 *
 * What stays faithful is the one input the lane reads. `parent` here is exactly what
 * the backend fold puts on the wire -- `{slot, key}` after an adoption, `null` after a
 * release -- and the frames are delivered as ordinary slot updates through the REAL
 * `ChatSidebar`. Nothing stubs the nesting, the collapse state, or the expand: the
 * harness supplies the frames and the component decides the outcome.
 *
 * Query string: ?theme=dark|light
 * Window hooks: __treeAdopt()   -- `chat-lane` moves under `chat-wave` (the adoption)
 *               __treeRelease() -- `chat-lane` returns to the top level (the release)
 */
import { useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
// ThemeProvider is the authority: it reads these keys and writes `data-theme` itself,
// so setting the attribute alone is overridden on mount. Both keys are pinned because
// the colour theme drifts between runs when only the light/dark one is.
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The conductor lane, opened directly: the recording is of the lane, not of finding it.
localStorage.setItem('mc-sidebar-lane', 'conductor')
// Every row stays in the one list rather than folding into the stale section -- the
// subject is which rows are nested, so all of them have to remain visible.
localStorage.setItem('mc-session-stale-collapse-ms', '0')
// Collapsed-by-default is the state under test, so nothing is pre-expanded. Cleared
// rather than assumed absent, because the harness reloads against a kept profile.
localStorage.removeItem('mc-sidebar-conductor-expanded')
// Wide enough that titles are not truncated. The nesting is shown by a 14px indent per
// level, and at the sidebar's narrow default an ellipsis eats the shift -- so the rows
// would move in the DOM and read as stationary in the clip, which is the one thing the
// recording exists to show.
localStorage.setItem('mc-sidebar-width', '520')

const MIN = 60_000
const now = Date.now()
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

/**
 * A row as the SLOTS BROADCAST sends it, which is not quite the exported `ChatSlot`:
 * `parent` lives on the sidebar's own internal `Slot`, and that type is deliberately
 * not exported. So the fixtures are typed here, on the field set this harness actually
 * uses, and cast once where they reach the component -- rather than cast per row, which
 * is how a fixture ends up carrying a field the product never reads.
 */
interface Row {
  key: string
  title: string
  messages: number
  running: boolean
  agent: string
  last_ts: string
  last_message: string
  needs_input?: boolean
  parent?: { slot?: string; key?: string | null } | null
  source_links?: NonNullable<ChatSlot['source_links']>
}

/** The four fields every row states for itself; the rest carry a default. */
type RowSeed = Partial<Row> & Pick<Row, 'key' | 'title' | 'last_ts' | 'last_message'>

const row = (over: RowSeed): Row => ({ messages: 12, running: false, agent: 'kirocrew', ...over })

/**
 * The shape this feature exists for: a conductor that already has a branch on screen,
 * and a second conductor picking up the same line of work. Timestamps, status lines and
 * pull-request chips are all populated, so the frames read as a sidebar someone is
 * actually working in rather than a fixture.
 */
const WAVE = 'chat-wave'
const LANE = 'chat-lane'

const ROWS: Row[] = [
  row({
    key: WAVE,
    title: 'Conductor: session-tree wave',
    last_ts: at(40_000),
    last_message: 'Taking over the sidebar line.',
  }),
  row({
    key: LANE,
    title: 'Conductor: sidebar lane',
    last_ts: at(3 * MIN),
    last_message: 'Both workers reported in.',
    source_links: [
      { provider: 'github', number: 12757, url: 'https://example.invalid/pull/12757', label: '#12757', state: 'open', ci: 'passed' },
    ],
  }),
  row({
    key: 'chat-fold',
    title: 'worker: fold parity',
    agent: 'kirocrew-worker',
    running: true,
    last_ts: at(30_000),
    last_message: 'Edge layer applies over the citation.',
    parent: { slot: LANE, key: LANE },
    source_links: [
      { provider: 'github', number: 12962, url: 'https://example.invalid/pull/12962', label: '#12962', state: 'open', ci: 'running' },
    ],
  }),
  row({
    key: 'chat-verbs',
    title: 'worker: adopt + release',
    agent: 'kirocrew-worker',
    last_ts: at(9 * MIN),
    last_message: 'Both verbs refuse an unseeded tree.',
    parent: { slot: LANE, key: LANE },
  }),
  row({
    key: 'chat-cold',
    title: 'worker: cold-scan parity',
    agent: 'kirocrew-worker',
    needs_input: true,
    last_ts: at(16 * MIN),
    last_message: 'Waiting on the tail-window decision.',
    parent: { slot: 'chat-verbs', key: 'chat-verbs' },
  }),
]

/** The adoption: `chat-lane` now cites `chat-wave`. Its own branch says nothing. */
const ADOPTED: Row[] = ROWS.map(r =>
  r.key === LANE ? { ...r, parent: { slot: WAVE, key: WAVE } } : r,
)

/** The release: the citation is cleared, so the row returns to the top level. */
const RELEASED: Row[] = ROWS.map(r => (r.key === LANE ? { ...r, parent: null } : r))

function Harness() {
  const [slots, setSlots] = useState(ROWS)
  const adopt = useCallback(() => setSlots(ADOPTED), [])
  const release = useCallback(() => setSlots(RELEASED), [])
  useEffect(() => {
    // Without this every row takes the `!connected` branch -- opacity-50 and no hover
    // affordance -- so the clip would show a disabled sidebar rather than one a person
    // is working in.
    store.dispatch(sseConnected())
    Object.assign(window as unknown as Record<string, unknown>, {
      __treeAdopt: adopt,
      __treeRelease: release,
    })
  }, [adopt, release])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={slots as unknown as ChatSlot[]}
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
