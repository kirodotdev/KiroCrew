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
import { setActiveSlot, sseChatMessage } from '../src/store/chatSlice'
import { registerToolPill } from '../src/store/toolPillRegistry'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

store.dispatch(setActiveSlot('capture-slot'))
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
    deny_override_detail: "Blocks 'git push' whose refspec targets a protected default branch, including force-push '+' refspecs, preventing unreviewed writes to the trunk.",
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
        <div
          className="self-start text-[13px] font-mono text-muted"
          ref={el => { if (el) registerToolPill('tc-capture-1', el) }}
        >
          ▶ Running: git push origin main
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
