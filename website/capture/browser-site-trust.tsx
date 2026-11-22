/**
 * Evidence for the browser card's site grant in the composer's Trust menu.
 *
 * Mounts the REAL ChatInput against the real store, stylesheet, theme tokens
 * and i18n catalog, seeded with one pending `browser` permission row whose
 * server fields carry a site grant: ?cls=read (a snapshot) or ?cls=act (a
 * click), on www.example.com. Open the Trust menu to see the site rows beside
 * "Trust all tools for this session". ?cls=cdp shows instead the card the op route
 * opens for a `cdp` call, on the default agent spec where the host asks nothing:
 * the real QuestionCard with the question `browser_card_ask.card_questions` builds
 * (copied here), Allow or Deny.
 *
 *   ?cls=read|act|cdp&theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatInput from '../src/components/ChatInput'
import QuestionCard from '../src/components/QuestionCard'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { setActiveSlot, sseChatMessage } from '../src/store/chatSlice'
import { registerToolPill } from '../src/store/toolPillRegistry'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const cls = params.get('cls') === 'act' ? 'act' : params.get('cls') === 'cdp' ? 'cdp' : 'read'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'capture-slot'
const input = cls === 'act'
  ? '{"op":"click","args":{"ref":"e3"}}'
  : cls === 'cdp'
    ? '{"op":"cdp","args":{"method":"Storage.getCookies"}}'
    : '{"op":"snapshot","args":{}}'
// The op route's card for one cdp call (browser_card_ask.card_questions).
const CDP_QUESTIONS = [{
  question: 'cdp Storage.getCookies {}',
  header: 'Browser cdp',
  options: [
    { label: 'Allow', description: "This step can read every site's cookies and control every tab. It asks every time, even if you trust all tools." },
    { label: 'Deny' },
  ],
  multiSelect: false,
}]
const siteMeta = {
  full_command: 'www.example.com',
  trust_command_grantable: '1',
  trust_command_key: 'browser-trust:v1:capture',
  browser_trust_class: cls,
  trust_grantable: '1',
}
store.dispatch(setActiveSlot(SLOT))
if (cls !== 'cdp') store.dispatch(sseChatMessage({
  slot: SLOT,
  role: 'permission',
  content: 'Drive browser',
  meta: {
    approval_id: 'ap-capture-1',
    request_id: 'req-capture-1',
    mid: 'mid-capture-1',
    tool_input: input,
    tool_title: 'Drive browser',
    is_shell: '',
    ...siteMeta,
    always_allow_tool_refusal: 'crew_owned',
    tool_call_id: 'tc-capture-1',
  },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  const [value, setValue] = useState('')
  return (
    <div className="flex flex-col justify-end h-screen bg-bg text-text" data-capture-root data-cls={cls}>
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden">
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          Open example.com and look around.
        </div>
        {/* Stand-in for the transcript's inline tool pill, so the bar keeps
            its inline (non-ghost) form. */}
        <div
          className="self-start text-[13px] font-mono text-muted"
          ref={el => { if (el) registerToolPill('tc-capture-1', el) }}
        >
          ▶ Drive browser
        </div>
        {cls === 'cdp' && <QuestionCard questions={CDP_QUESTIONS} askId={'a'.repeat(32)} onSubmit={() => {}} />}
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
