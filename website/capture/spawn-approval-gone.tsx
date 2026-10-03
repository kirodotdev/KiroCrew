/**
 * Evidence for the composer spawn-approval banner's gone state.
 *
 * Mounts the REAL useSpawnApprovals hook and SpawnApprovalCard against a real
 * store, the stylesheet, theme tokens and i18n catalog, with pending sub-agent
 * spawns in the viewed slot. The capture script presses Approve and answers the
 * decide the way the server does for an approval that is gone (a 404), or, with
 * ?targetless=1, leaves the spawn without a target so the press is refused on
 * the client. Nothing here re-implements the banner, its classes or strings.
 *
 *   ?theme=dark|light&count=N&targetless=1
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'

import dashboardReducer from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import { useAppDispatch } from '../src/store'
import { useSpawnApprovals } from '../src/components/chat-input/approval'
import { SpawnApprovalCard } from '../src/components/chat-input/SpawnApprovalCard'
import { initI18n } from '../src/i18n'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const count = Math.max(1, Number(params.get('count') || '1'))
const targetless = params.get('targetless') === '1'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

const SLOT = 'slot-1'
const TASKS = ['Summarise the deploy logs', 'Draft the release notes', 'Check the staging alarms']
const subagents: Record<string, unknown> = {}
for (let i = 1; i <= count; i++) {
  subagents[`a${i}`] = {
    id: `a${i}`, task: TASKS[(i - 1) % TASKS.length], agent: '', status: 'pending',
    streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
    approval_id: `spawn:a${i}`,
    ...(targetless ? {} : { approval_target: { origin: 'coordinator', id: `spawn:a${i}`, slot: SLOT, instance: `inst-a${i}` } }),
  }
}
const initialChat = chatReducer(undefined, { type: '@@capture/init' })
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
  preloadedState: { chat: { ...initialChat, activeSlot: SLOT, subagents } as typeof initialChat },
})

function Banner() {
  const dispatch = useAppDispatch()
  const spawns = useSpawnApprovals({ slotId: SLOT, slotApprovalChrome: true, dispatch })
  return <SpawnApprovalCard {...spawns} hasApproval={false} />
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <div data-capture-root className="bg-bg text-text p-5 w-[720px]">
      <Banner />
    </div>
  </Provider>,
)
