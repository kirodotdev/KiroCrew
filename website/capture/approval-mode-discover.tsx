/**
 * Isolated capture entry for the approval-mode discoverability work:
 *
 *   A1 — the approval bar's "adjust approval mode" hint row
 *   A2 — the footer ApprovalModePicker opened + spotlighted by that hint
 *   B2 — the one-time nudge callout anchored above the picker after
 *        repeated manual approvals
 *
 * WHY ISOLATED: the pending-approval state only exists inside a live chat
 * session with an agent parked on an approval. This mounts the REAL ChatInput
 * against the real stylesheet, theme tokens and live i18n catalog, seeding the
 * real store with the same permission message the gateway emits.
 *
 * Scene comes from the query string: ?scene=hint|spotlight|nudge&theme=dark|light
 * The spotlight scene is driven by the harness clicking the REAL hint link —
 * not by forcing component state — so a frame documents the shipped wiring.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { setActiveSlot, sseChatMessage } from '../src/store/chatSlice'
import { registerToolPill } from '../src/store/toolPillRegistry'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'hint'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Seed the real store with the same shape the gateway's WS frames produce:
// an unresolved permission message on the active slot.
store.dispatch(setActiveSlot('capture-slot'))
store.dispatch(sseChatMessage({
  slot: 'capture-slot',
  role: 'permission',
  content: 'Running: npm run build',
  meta: {
    approval_id: 'ap-capture-1',
    request_id: 'req-capture-1',
    tool_input: '{"command":"npm run build"}',
    tool_title: 'Running: npm run build',
    full_command: 'npm run build',
    base_command: 'npm',
    tool_call_id: 'tc-capture-1',
  },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [value, setValue] = useState('')
  return (
    <div className="flex flex-col justify-end h-screen bg-bg text-text" data-capture-root>
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Build the project and run the tests.
        </div>
        <div className="self-start max-w-[80%] rounded-xl bg-card text-card-fg px-3 py-2 text-[13px]">
          Starting with the build.
        </div>
        {/* Stand-in for the transcript's inline tool pill: registering a
            visible node keeps the approval bar in its inline (non-ghost)
            form, where the picker and the A1 hint both exist. */}
        <div
          className="self-start text-[13px] font-mono text-muted"
          ref={el => { if (el) registerToolPill('tc-capture-1', el) }}
        >
          ▶ Running: npm run build
        </div>
      </div>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        approvalMode="normal"
      />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)

// The nudge scene is threshold-driven in production (three manual approvals).
// The driver script replays that for real: it clicks the REAL "Allow once"
// button (the /api call is answered by route interception), then injects the
// next permission frame through this hook — the same dispatch the WS layer
// makes — until the third approval lands and the nudge renders itself.
declare global { interface Window { __capture?: { nextApproval: (n: number) => void } } }
window.__capture = {
  nextApproval: (n: number) => {
    // Keep the next approval in the inline (non-ghost) form too.
    const el = document.createElement('div')
    document.querySelector('[data-capture-root]')?.firstElementChild?.appendChild(el)
    registerToolPill(`tc-capture-${n}`, el)
    store.dispatch(sseChatMessage({
      slot: 'capture-slot',
      role: 'permission',
      content: `Running: npm run step${n}`,
      meta: {
        approval_id: `ap-capture-${n}`,
        request_id: `req-capture-${n}`,
        tool_input: `{"command":"npm run step${n}"}`,
        tool_title: `Running: npm run step${n}`,
        full_command: `npm run step${n}`,
        base_command: 'npm',
        tool_call_id: `tc-capture-${n}`,
      },
    }))
  },
}
document.documentElement.setAttribute('data-capture-scene', scene)
