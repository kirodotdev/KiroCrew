/**
 * Evidence for the composer approval bar's Trust affordance by origin.
 *
 * Mounts the REAL ChatInput against the real store, stylesheet, theme tokens
 * and i18n catalog, seeded with one pending permission row on the active slot.
 * ?kind=native seeds a chat runner's own request (it can take a standing Trust
 * grant); ?kind=coordinator seeds a coordinator approval parked in the chat,
 * which is decided one-shot by its own target, so the bar offers no Trust and
 * says why.
 *
 *   ?kind=native|coordinator&theme=dark|light
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
const kind = params.get('kind') === 'coordinator' ? 'coordinator' : 'native'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'capture-slot'
const identity = kind === 'native'
  ? { mid: 'mid-capture-1' }
  : { registry: 'coordinator', approval_target: { origin: 'coordinator', id: 'ap-capture-1', slot: SLOT, instance: 'inst-capture-1' } }
store.dispatch(setActiveSlot(SLOT))
store.dispatch(sseChatMessage({
  slot: SLOT,
  role: 'permission',
  content: 'Running: npm run build',
  meta: {
    approval_id: 'ap-capture-1',
    request_id: 'req-capture-1',
    tool_input: '{"command":"npm run build"}',
    tool_title: 'Running: npm run build',
    is_shell: '1',
    full_command: 'npm run build',
    base_command: 'npm',
    trust_command_grantable: '1',
    trust_base_grantable: '1',
    trust_grantable: '1',
    tool_call_id: 'tc-capture-1',
    ...identity,
  },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [value, setValue] = useState('')
  return (
    <div className="flex flex-col justify-end h-screen bg-bg text-text" data-capture-root data-kind={kind}>
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Build the project and run the tests.
        </div>
        {/* Stand-in for the transcript's inline tool pill, so the bar keeps
            its inline (non-ghost) form. */}
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
