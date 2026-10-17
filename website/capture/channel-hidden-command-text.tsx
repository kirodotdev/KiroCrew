/**
 * Evidence for the channel approval whose command text the provider removed.
 *
 * WHY THE WHOLE BUBBLE: the marker this change edits is POSTED TEXT. It renders
 * above the card, in the message body, so a frame that mounts the card alone
 * shows none of it. This mounts the REAL MessageBubble (which renders the real
 * body and the real ApprovalCard, and therefore the real TrustDropdown) from a
 * message shaped exactly as `_stream_task` posts it.
 *
 * Both scenes carry the SAME controls, which is the point: the blanket channel
 * grant never needed a command scope. Only the wording differs.
 *   before - "Shell command (allow once)", a restriction the card does not
 *            enforce while `Trust all tools in this channel` sits under it.
 *   after  - "Shell command (exact text unverified)", which names what is
 *            actually missing and promises nothing.
 *
 *   ?theme=dark|light&scene=after|before
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'

import { MessageBubble } from '../src/pages/ChannelPage'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import type { RootState } from '../src/store'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') === 'before' ? 'before' : 'after'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const MARKER =
  scene === 'before' ? 'Shell command (allow once)' : 'Shell command (exact text unverified)'
const CMD = 'curl -H [REDACTED: credential] https://api.example.invalid/v1/orders'
const CONTENT = `⚠️ Approval needed: **${MARKER}: ${CMD}**\n\`\`\`\n{"command": "${CMD}"}\n\`\`\``

await initI18n()

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer },
  preloadedState: {
    // `normal` is the mode that renders the decision controls at all.
    dashboard: { approvalMode: 'normal' } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, messages: [], toolLog: [], slotStatusDetail: {} } as unknown as RootState['chat'],
  },
})

const msg = {
  id: 'm1',
  fromId: 'a1',
  fromRole: 'dev',
  content: CONTENT,
  msgType: 'approval',
  timestamp: '2026-09-09T03:00:00Z',
  replyCount: 0,
} as unknown as Parameters<typeof MessageBubble>[0]['msg']

const agents = [{ id: 'a1', role: 'dev' }] as unknown as Parameters<typeof MessageBubble>[0]['agents']

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <div data-capture-root className="bg-bg text-text p-5 w-[760px]">
      <MessageBubble msg={msg} agents={agents} onApprove={() => Promise.resolve()} />
    </div>
  </Provider>,
)
