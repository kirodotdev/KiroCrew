/**
 * Isolated capture entry for the waiting-on-you cards: an agent's pinned ask
 * above the composer, its status on the sidebar row, tab strip and grid pane,
 * its place in the needs-me hover card, and the Questions tab that lists asks.
 *
 * Mounts the REAL ChatPage (or the real SessionGridView / CommandCenterPanel)
 * with a preloaded store whose slot rows carry `waiting_on_you`, the projection
 * the gateway pushes. The driver (scripts/capture-waiting-on-you.mjs) answers
 * every API call by route interception; it reads the slot list and each
 * transcript back from `window.__CAPTURE_SLOTS__` / `__CAPTURE_MESSAGES__` so a
 * refetch returns the same rows the store was seeded with.
 * `window.__CAPTURE_PUSH__(asks)` replays a slot push that sets the active
 * session's pinned asks, the way the gateway retires an ask.
 *
 * Scenes via query string: ?theme=dark|light, ?motion=1 keeps transitions on
 * (for recordings), and
 *   ?scene=decision   one decision ask (three options and a detail)
 *   ?scene=stack      three asks, the newest a decision
 *   ?scene=action     one action ask (no options: Done and a note)
 *   ?scene=uncertain  one decision ask the gateway restored as uncertain
 *   ?scene=sidebar    the active session holds no ask; a second open tab does
 *   ?scene=grid       split view: the active session's pane and a picker
 *   ?scene=questions  the Questions tab of a root session's side panel
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer, { sseSlots } from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import { sessionTabsStorageKey } from '../src/lib/sessionTabs'
import type { RootState } from '../src/store'
import type { ChatSlot, WaitingOnYouAsk } from '../src/types'
import ChatPage from '../src/pages/ChatPage'
import SessionGridView from '../src/components/SessionGridView'
import CommandCenterPanel from '../src/pages/chat/command-center/CommandCenterPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'decision'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)

if (params.get('motion') !== '1') {
  const style = document.createElement('style')
  style.textContent = '*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }'
  document.head.appendChild(style)
}

const HOUR = 3600
const now = Math.floor(Date.now() / 1000)

const ROLLOUT: WaitingOnYouAsk = {
  key: 'rollout', ask_id: 'wy-rollout', state: 'pinned', ts: now - 3 * HOUR,
  title: 'Pick the rollout window for the nightly fix',
  detail: 'The fix PR is green. Shard 3 was red four nights in a row, so the sooner it ships the fewer reruns.',
  options: [
    { label: 'Ship today', description: 'Deploys at 16:00 UTC with the regular train' },
    { label: 'After the weekend', description: 'One more nightly as proof first' },
    { label: 'Hold it', description: '' },
  ],
}
const OVERRIDE: WaitingOnYouAsk = {
  key: 'scope-override', ask_id: 'wy-override', state: 'pinned', ts: now - 5 * HOUR,
  title: 'Post the Security Scope override on the review',
  detail: 'Only a repo admin can post it.',
  options: [],
}
const REVIEWER: WaitingOnYouAsk = {
  key: 'reviewer', ask_id: 'wy-reviewer', state: 'pinned', ts: now - 4 * HOUR,
  title: 'Choose a second reviewer',
  detail: '',
  options: [{ label: 'Bolin', description: '' }, { label: 'Priya', description: '' }],
}
const ACTION: WaitingOnYouAsk = {
  key: 'vpn', ask_id: 'wy-vpn', state: 'pinned', ts: now - 3 * HOUR,
  title: 'Approve the staging deploy in the pipeline console',
  detail: 'The pipeline waits on a manual approval step that only a person can click. Say when it is done, and add anything the agent should know.',
  options: [],
}

const SLOT = 'slot-a'
const OTHER = 'slot-b'
const QUIET = 'slot-c'
const ASKS: Record<string, WaitingOnYouAsk[]> = {
  decision: [ROLLOUT],
  stack: [OVERRIDE, REVIEWER, ROLLOUT],
  action: [ACTION],
  uncertain: [{ ...ROLLOUT, state: 'uncertain' }],
  sidebar: [],
  grid: [ROLLOUT],
  questions: [],
}
const transcript = (title: string) => [
  { role: 'user', content: `Keep an eye on ${title} and tell me what you need.`, cls: '', ts: '2026-11-18T22:00:00Z', meta: { mid: `${title}-u1` } },
  { role: 'assistant', content: 'The fix PR is open and CI is green. I pinned what I need from you above the composer.', cls: '', ts: '2026-11-18T22:00:30Z', meta: { mid: `${title}-a1` } },
]
const messages = transcript('the nightly fix')
const row = (key: string, title: string, asks: WaitingOnYouAsk[], extra: Record<string, unknown> = {}) => (
  { key, title, messages: 2, running: false, mode: '', pending_approval: false, needs_input: false, waiting_on_you: asks, last_message: messages[1].content, ...extra }
)
const SCENE_ROWS: Record<string, ReturnType<typeof row>[]> = {
  sidebar: [row(OTHER, 'Release train watch', [ROLLOUT])],
  grid: [row(OTHER, 'Release train watch', [REVIEWER]), row(QUIET, 'Docs typo sweep', [])],
  questions: [
    row(OTHER, 'Release train watch', [REVIEWER], { created_by: SLOT }),
    row(QUIET, 'Staging deploy', [ACTION, ROLLOUT], { created_by: SLOT }),
  ],
}
let slots = [
  row(SLOT, 'Nightly shard fix', ASKS[scene] ?? []),
  ...(SCENE_ROWS[scene] ?? []),
]
const captureWindow = window as unknown as {
  __CAPTURE_MESSAGES__: unknown
  __CAPTURE_SLOTS__: unknown
  __CAPTURE_PUSH__: (asks: WaitingOnYouAsk[]) => void
}
captureWindow.__CAPTURE_MESSAGES__ = { [SLOT]: messages, [OTHER]: transcript('the release train'), [QUIET]: transcript('the docs sweep') }
captureWindow.__CAPTURE_SLOTS__ = slots
if (scene === 'sidebar') localStorage.setItem(sessionTabsStorageKey(''), JSON.stringify([SLOT, OTHER]))

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  preloadedState: {
    dashboard: {
      status: null,
      // As if the socket were open: an offline sidebar greys every idle row.
      connected: true,
      slots,
      slotsLoaded: true,
      unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: SLOT, messages,
      slotRunning: false, slotStopping: false, slotState: 'idle',
      history: [], historyHasMore: false, pendingInput: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotHasMore: false, slotOldestIndex: 0, loadingOlder: false, slotCursorKey: null,
      slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
      historyOffset: 0, _wsChunkedDuringFetch: false,
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
    notifications: { items: [] } as unknown as RootState['notifications'],
  },
})

captureWindow.__CAPTURE_PUSH__ = asks => {
  slots = slots.map(s => (s.key === SLOT ? { ...s, waiting_on_you: asks } : s))
  captureWindow.__CAPTURE_SLOTS__ = slots
  store.dispatch(sseSlots(slots as unknown as ChatSlot[]))
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Scene() {
  if (scene === 'grid') return <SessionGridView seedSlot={SLOT} onClose={() => {}} onCollapse={() => {}} />
  if (scene === 'questions') {
    return (
      <div className="flex-1 flex justify-end bg-bg">
        <div className="w-[440px] h-full border-l border-border" data-testid="capture-side-panel">
          <CommandCenterPanel slot={SLOT} active />
        </div>
      </div>
    )
  }
  return <ChatPage />
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <div data-capture-root style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
            <Scene />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
