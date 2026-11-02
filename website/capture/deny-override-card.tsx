/**
 * Isolated capture entry for the security-rule override card.
 *
 * Mounts the REAL ChatInput against the real stylesheet, theme tokens and live
 * i18n catalog, with the permission message the gateway emits when a deny rule
 * refused a shell command in a session a person drives: the row carries
 * `deny_override_rule`, so the bar offers the override scopes and a reject.
 *
 * Query string: ?theme=dark|light&lang=en|zh-CN
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { setActiveSlot, setSlotRunning, sseChatMessage, sseToolActivity } from '../src/store/chatSlice'
import ToolCallLine from '../src/pages/chat/ToolCallLine'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
// `data-mode` is what the glass surfaces key their tint on (useTheme sets both in
// the app); without it a light page drew the composer in the dark tint.
document.documentElement.setAttribute('data-mode', theme === 'light' ? 'light' : 'dark')

store.dispatch(setActiveSlot('capture-slot'))
// The turn is live while the card waits, as it is when the gateway asks.
store.dispatch(setSlotRunning(true))
// The real tool pill for the refused call, as the transcript draws it above the card.
const toolRow: ChatMessage = { role: 'tool', content: '🔧 Running: git push origin main', cls: '', ts: 1, meta: { tool_call_id: 'tc-capture-1', is_shell: '1' } }
store.dispatch(sseChatMessage({ slot: 'capture-slot', role: 'tool', content: toolRow.content, meta: toolRow.meta }))
store.dispatch(sseToolActivity({ slot: 'capture-slot', tool: 'Running: git push origin main', kind: 'execute', purpose: '', input_preview: '{"command":"git push origin main"}', tool_call_id: 'tc-capture-1', is_shell: true }))
store.dispatch(sseChatMessage({
  slot: 'capture-slot',
  role: 'permission',
  content: 'Running: git push origin main',
  meta: {
    approval_id: 'ap-capture-1',
    request_id: 'req-capture-1',
    tool_input: '{"command":"git push origin main"}',
    tool_title: 'Running: git push origin main',
    is_shell: '1',
    tool_call_id: 'tc-capture-1',
    deny_override_rule: 'git-publish-push-protected-branch-name',
    deny_override_reason: 'Blocked by security policy: git-publish-push-protected-branch-name',
    deny_override_family: 'git_publish',
    deny_override_detail: "Blocks 'git push' whose refspec targets a protected default branch, including force-push '+' refspecs, preventing unreviewed writes to the trunk.",
    deny_override_command: 'git push origin main',
  },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [value, setValue] = useState('')
  return (
    <div className="flex flex-col justify-end h-screen bg-bg text-text" data-capture-root>
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Push the release notes to main.
        </div>
        <div className="self-start" data-testid="capture-tool-pill">
          <ToolCallLine message={store.getState().chat.messages.find(m => m.role === 'tool') ?? toolRow} running slot="capture-slot" />
        </div>
      </div>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        approvalMode="yolo"
      />
    </div>
  )
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
